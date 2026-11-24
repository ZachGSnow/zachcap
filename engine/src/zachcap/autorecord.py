"""Record calls on their own, and stop them when they end.

Windows keeps a record of which apps are using the microphone (calls.py). Every couple of
seconds this looks at it:

- Not recording, and a call app chosen in Settings (Teams and 3CX, by default) took the
  mic: a recording starts at once, so the first words aren't lost. If the app lets go again
  within a minute (a mic test, a preview closed without joining), that recording is
  deleted. "Don't record" deletes it too, and that app isn't recorded again until it lets go.
- Recording, and the call app let go of the mic after a call (it ended): the recording
  stops a few seconds later. "Keep recording" holds it until the call app is back. One this
  started where nobody was heard at all (a preview left open, nobody joined) is deleted.
- Recording with no call app, and nobody heard for ten minutes: it stops a minute later
  (an in-room meeting that ended without anyone pressing Stop). Someone speaking cancels it.

Web browsers are never recorded on their own (any website using the mic would start one);
ZachCap offers to record those instead. The engine decides here rather than the page: a
window hidden behind the call runs its timers late, and a call must not wait on them.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from itertools import count

from . import calls

SETTING = "auto_record"  # "1": start recording when a chosen call app takes the mic
APPS_SETTING = "auto_record_apps"  # the call apps that start one, comma-separated
# Meeting and phone apps; chat apps' huddles (Slack, Discord) only when chosen.
DEFAULT_APPS = ("Microsoft Teams", "3CX", "Zoom", "Webex", "RingCentral")
CHECK_EVERY = 2.0
# An app gone from the microphone this long has let go of it: Teams closes and reopens the
# mic for a moment when you join from the preview.
LET_GO = 3.0
CALL_AFTER = 60.0  # held this long: a call, not a mic test (as watchdog.CALL_AFTER)
STOP_IN = 4.0  # once the call app has let go, the recording stops this much later
QUIET_FOR = 600.0  # nobody heard this long, with no call app: the meeting is over
QUIET_STOP_IN = 60.0
EVENTS_KEPT = 20


def choosable() -> list[str]:
    """The call apps that can start a recording on their own: every known one but browsers."""
    return [app for app in dict.fromkeys(calls.KNOWN.values()) if app not in calls.BROWSERS]


def chosen(value: str | None) -> set[str]:
    """The apps a saved setting names (unknown names and browsers are left out)."""
    if value is None:
        return set(DEFAULT_APPS)
    return {name.strip() for name in value.split(",")} & set(choosable())


class CallWatch:
    """Starts and stops recordings for calls. ``check()`` runs every couple of seconds; the
    rest are what the page's buttons ask for. Everything it acts with is passed in:

    - ``settings()``: {"auto": bool, "apps": set of app names, "stop": bool} (Stop when the
      call ends);
    - ``recording()``: None when not recording, else {"meeting_id", "paused", "call":
      {"apps", "ended_for"}, "quiet_for"} (the recording's watchdog status), "call_seen"
      (the call apps it heard holding the mic) and "heard" (whether anyone was heard);
    - ``start(app)``: starts a recording for that app's call and returns its meeting id;
    - ``stop()``: stops and saves the recording (it's then transcribed);
    - ``discard()``: stops the recording and deletes it.
    """

    def __init__(
        self,
        settings: Callable[[], dict],
        recording: Callable[[], dict | None],
        start: Callable[[str], int],
        stop: Callable[[], int | None],
        discard: Callable[[], object],
        apps_using_mic: Callable[[], list[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._recording = recording
        self._start = start
        self._stop = stop
        self._discard = discard
        self._apps = apps_using_mic or calls.apps_using_mic
        self._clock = clock
        self._lock = threading.RLock()  # checks, and the page's buttons, one at a time
        self._holding: dict[str, float] = {}  # call app -> since when it holds the mic
        self._gone: dict[str, float] = {}  # call app -> since when it's away (not yet let go)
        self._declined: set[str] = set()  # not started again until the app lets go
        self.auto: dict | None = None  # the recording this started: app, meeting, when
        self.stopping: dict | None = None  # counting down to stop: reason, deadline
        self._kept = {"call": False, "quiet": False}  # Keep recording, until things change
        self._ids = count(1)
        self.events: deque[dict] = deque(maxlen=EVENTS_KEPT)
        self._done = threading.Event()

    # The page's buttons ------------------------------------------------------------

    def keep(self) -> bool:
        """Keep recording: the countdown to stop is called off until the call app is back or
        someone speaks (until the meeting changes)."""
        with self._lock:
            if self.stopping is None:
                return False
            self._kept[self.stopping["reason"]] = True
            self.stopping = None
            return True

    def decline(self) -> dict:
        """Don't record: the recording this started is deleted, and its app isn't recorded
        again until it lets go of the mic. Raises LookupError if this didn't start one."""
        with self._lock:
            current = self._recording()
            auto = self.auto
            if auto is None or current is None or current["meeting_id"] != auto["meeting_id"]:
                raise LookupError("Only a recording ZachCap started on its own can be undone")
            self._declined.add(auto["app"])
            self._discard()
            self.auto = self.stopping = None
            return self._event("declined", auto["app"], auto["meeting_id"])

    # What the page shows -------------------------------------------------------------

    def status(self) -> dict:
        """For /api/record: the recording this started (and whether it can still be undone),
        the countdown to stop, and the latest thing done on its own."""
        with self._lock:
            now = self._clock()
            return {
                "auto": None
                if self.auto is None
                else {
                    "app": self.auto["app"],
                    "meeting_id": self.auto["meeting_id"],
                    "confirmed": self.auto["confirmed"],
                },
                "stopping": None
                if self.stopping is None
                else {
                    "reason": self.stopping["reason"],
                    "in": max(0.0, round(self.stopping["deadline"] - now, 1)),
                },
                "event": self.events[-1] if self.events else None,
            }

    # The look every couple of seconds ---------------------------------------------------

    def run_forever(self) -> None:
        while not self._done.wait(CHECK_EVERY):
            try:
                self.check()
            except Exception:  # one bad look must never end the watching
                traceback.print_exc()

    def close(self) -> None:
        self._done.set()

    def check(self) -> None:
        with self._lock:
            now = self._clock()
            try:
                apps = [app for app in self._apps() if calls.is_meeting_app(app)]
            except OSError:
                apps = []
            self._follow(apps, now)
            current = self._recording()
            if current is None:
                self._not_recording(apps, now)
            else:
                self._while_recording(current, now)

    def _follow(self, apps: list[str], now: float) -> None:
        """Which call apps hold the mic: one away for a moment still does."""
        for app in apps:
            self._holding.setdefault(app, now)
            self._gone.pop(app, None)
        for app in list(self._holding):
            if app in apps:
                continue
            if now - self._gone.setdefault(app, now) >= LET_GO:
                del self._holding[app], self._gone[app]
                self._declined.discard(app)  # its next call is a new one

    def _not_recording(self, apps: list[str], now: float) -> None:
        if self.auto is not None:
            # Stopped from the page (or the tray) while the call goes on: not started again
            # for the rest of this call.
            if self.auto["app"] in self._holding:
                self._declined.add(self.auto["app"])
            self.auto = None
        self.stopping = None
        self._kept = {"call": False, "quiet": False}
        settings = self._settings()
        if not settings["auto"]:
            return
        for app in apps:  # holding the mic right now, not just away for a moment
            if app not in settings["apps"] or app in self._declined or app in calls.BROWSERS:
                continue
            self._declined.add(app)  # one recording per call, even if starting fails
            try:
                meeting_id = self._start(app)
            except Exception as exc:
                self._event("failed", app, None, detail=str(exc))
                return
            self.auto = {"app": app, "meeting_id": meeting_id, "at": now, "confirmed": False}
            self._event("started", app, meeting_id)
            return

    def _while_recording(self, current: dict, now: float) -> None:
        auto = self.auto
        if auto is not None and auto["meeting_id"] != current["meeting_id"]:
            auto = self.auto = None  # a recording this didn't start
        if auto is not None and not auto["confirmed"]:
            if auto["app"] not in self._holding:
                # Let go within a minute: a mic test or a preview, not a call.
                self._discard()
                self.auto = self.stopping = None
                self._event("discarded", auto["app"], auto["meeting_id"])
                return
            if now - self._holding[auto["app"]] >= CALL_AFTER:
                auto["confirmed"] = True
        call = current.get("call") or {}
        ended_for = call.get("ended_for")
        quiet_for = current.get("quiet_for") or 0.0
        if ended_for is None:
            self._kept["call"] = False
        if quiet_for < QUIET_FOR:
            self._kept["quiet"] = False
        reason = None
        if self._settings()["stop"] and not current.get("paused"):
            if ended_for is not None and ended_for >= LET_GO and not self._kept["call"]:
                reason = "call"
            elif not call.get("apps") and quiet_for >= QUIET_FOR and not self._kept["quiet"]:
                reason = "quiet"
        if reason is None:
            self.stopping = None
            return
        if self.stopping is None or self.stopping["reason"] != reason:
            wait = STOP_IN if reason == "call" else QUIET_STOP_IN
            self.stopping = {"reason": reason, "deadline": now + wait}
            return
        if now < self.stopping["deadline"]:
            return
        self.stopping = None
        seen = current.get("call_seen") or []
        app = auto["app"] if auto is not None else (seen[0] if seen else None)
        self.auto = None
        if auto is not None and not current.get("heard", True):
            self._discard()  # nobody spoke at all: nothing worth keeping
            self._event("discarded", app, auto["meeting_id"])
            return
        meeting_id = self._stop()
        if meeting_id is not None:
            self._event("stopped", app, meeting_id, reason=reason)

    def _event(self, kind: str, app: str | None, meeting_id: int | None, **more) -> dict:
        event = {"id": next(self._ids), "kind": kind, "app": app, "meeting_id": meeting_id}
        event.update(more)
        self.events.append(event)
        return event
