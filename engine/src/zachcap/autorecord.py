"""Record calls on their own, and stop them when they end.

Windows keeps a record of which apps are using the microphone (calls.py). Every couple of
seconds this looks at it:

- Not recording, and a call app chosen in Settings (Teams and 3CX, by default) took the
  mic: a recording starts at once, so the first words aren't lost. If the call apps let go
  again within a minute (a mic test, a preview closed without joining), that recording is
  deleted, unless something in it is worth keeping: a note, a bookmark or a snapshot, or the
  other side heard on the computer's audio (a short call). "Don't record" deletes it too.
- A recording that ends while call apps hold the mic (Stop, the tray, Don't record): those
  calls aren't recorded again until their apps let go. A call app back on the mic within a
  few minutes of a call (on hold, a headset switching) is that same call, not a mic test.
- Recording, and the call ended: one this started stops once its call apps have let go and
  the call's own audio has been quiet a few seconds (or half a minute on, if music plays),
  whatever "Stop when the call ends" says, since nobody pressed Start for it. One you started
  stops once the call app let go and nobody was heard for a few seconds, if that setting is
  on. Either way after a short countdown, which Keep recording calls off until the call app
  is back. One this started where nobody was heard at all is deleted.
- Recording with no call app, and nobody heard for ten minutes: it stops a minute later
  (an in-room meeting that ended without anyone pressing Stop). Someone speaking cancels it.

Web browsers are never recorded on their own (any website using the mic would start one);
ZachCap offers to record those instead. The engine decides here rather than the page: a
window hidden behind the call runs its timers late, and a call must not wait on them.
"""

from __future__ import annotations

import secrets
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable
from itertools import count

from . import calls
from .watchdog import CALL_AFTER, LET_GO  # held a minute: a call; away 3 s: let go

SETTING = "auto_record"  # "1": start recording when a chosen call app takes the mic
APPS_SETTING = "auto_record_apps"  # the call apps that start one, comma-separated
# Meeting and phone apps; chat apps' huddles (Slack, Discord) only when chosen.
DEFAULT_APPS = ("Microsoft Teams", "3CX", "Zoom", "Webex", "RingCentral")
CHECK_EVERY = 2.0
SAME_CALL_WITHIN = 180.0  # a call app back on the mic this soon after a call: the same call
QUIET_AFTER_CALL = 10.0  # the call app let go and nobody heard this long: the call ended
GONE_AT_MOST = 30.0  # one this started stops this long after its call, whatever plays on
STOP_IN = 4.0  # once the call ended, the recording stops this much later
QUIET_FOR = 600.0  # nobody heard this long, with no call app: the meeting is over
QUIET_STOP_IN = 60.0
EVENTS_KEPT = 20


class Busy(Exception):
    """The recording can't start just now (a sound check, a recording starting): it's
    tried again at the next look rather than given up on for the call."""


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
      {"apps", "ended_for"}, "quiet_for", "call_quiet_for"} (the recording's watch status;
      "call_quiet_for" counts the computer's audio only), "call_seen" (the call apps it heard
      holding the mic), "heard" and "call_heard" (whether anyone, and the call's side, was
      heard) and "notes" (how many notes, bookmarks and snapshots it has);
    - ``start(app)``: starts a recording for that app's call and returns its meeting id
      (raises Busy when it can't just now);
    - ``stop(meeting_id)``: stops and saves that recording (it's then transcribed); None if
      it isn't the one recording any more;
    - ``discard(meeting_id, only_if_worthless)``: stops that recording and deletes it, and
      says "deleted", "gone" (not that recording any more), or "kept" (only_if_worthless, and
      a note came in or the call was heard meanwhile: saved instead).

    ``continuing``: the engine started again mid-recording (a crash, a quit), so a call on
    the mic at the first look is the one that was being recorded, not a mic test.
    """

    def __init__(
        self,
        settings: Callable[[], dict],
        recording: Callable[[], dict | None],
        start: Callable[[str], int],
        stop: Callable[[int], int | None],
        discard: Callable[[int, bool], str],
        apps_using_mic: Callable[[], list[str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        continuing: bool = False,
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
        self._let_go: dict[str, float] = {}  # call app -> when it let go of a call
        self._same_call: set[str] = set()  # back on the mic soon after a call: that call
        self._declined: set[str] = set()  # not started again until the app lets go
        self._failed: set[str] = set()  # couldn't start for this call: the page offers it
        self._continuing = continuing
        self._last: int | None = None  # the recording seen at the last look
        self.auto: dict | None = None  # the recording this started: app, meeting, when
        self.stopping: dict | None = None  # counting down to stop: reason, deadline, meeting
        self._kept = {"call": False, "quiet": False}  # Keep recording, until things change
        # Event ids count from 1 in each run of the engine: the page tells runs apart by this.
        self.boot = secrets.token_hex(8)
        self._ids = count(1)
        self.events: deque[dict] = deque(maxlen=EVENTS_KEPT)
        self._done = threading.Event()

    # The page's buttons ------------------------------------------------------------

    def keep(self, meeting_id: int | None = None) -> bool:
        """Keep recording: the countdown to stop is called off until the call app is back or
        someone speaks (until the meeting changes). ``meeting_id``: the recording the page
        showed the countdown for."""
        with self._lock:
            stopping = self.stopping
            if stopping is None or meeting_id not in (None, stopping["meeting_id"]):
                return False
            self._kept[stopping["reason"]] = True
            self.stopping = None
            return True

    def decline(self, meeting_id: int | None = None) -> dict:
        """Don't record: the recording this started is deleted, and the calls on the mic
        aren't recorded again until their apps let go. ``meeting_id``: the recording the
        page showed the button for. Raises LookupError if this didn't start that one."""
        with self._lock:
            current = self._recording()
            auto = self.auto
            if (
                auto is None
                or current is None
                or current["meeting_id"] != auto["meeting_id"]
                or meeting_id not in (None, auto["meeting_id"])
            ):
                raise LookupError("Only a recording ZachCap started on its own can be undone")
            if self._discard(auto["meeting_id"], False) == "gone":
                raise LookupError("That recording has stopped already")
            # Only the apps holding the mic now: one that let go already is free for its
            # next call.
            self._declined.update(self._holding)
            self.auto = self.stopping = None
            self._last = None
            return self._event("declined", auto["app"], auto["meeting_id"])

    # What the page shows -------------------------------------------------------------

    def status(self, meeting_id: int | None = None) -> dict:
        """For /api/record: the recording this started (and whether it can still be undone),
        and the countdown to stop, when they are about ``meeting_id`` (the one recording)."""
        with self._lock:
            now = self._clock()
            auto, stopping = self.auto, self.stopping
            if meeting_id is not None:
                if auto is not None and auto["meeting_id"] != meeting_id:
                    auto = None  # one it started, stopped since: not this one
                if stopping is not None and stopping["meeting_id"] != meeting_id:
                    stopping = None
            return {
                "auto": None
                if auto is None
                else {
                    "app": auto["app"],
                    "meeting_id": auto["meeting_id"],
                    "confirmed": auto["confirmed"],
                },
                "stopping": None
                if stopping is None
                else {
                    "reason": stopping["reason"],
                    "in": max(0.0, round(stopping["deadline"] - now, 1)),
                },
            }

    def events_after(self, boot: str | None, after: int | None) -> list[dict]:
        """What it did on its own after the page's cursor (``after``, an event id of the run
        ``boot``): everything still kept when the cursor is from another run of the engine,
        or there is none."""
        with self._lock:
            if boot != self.boot or after is None:
                return list(self.events)
            return [event for event in self.events if event["id"] > after]

    def failed(self) -> set[str]:
        """The call apps on the mic whose recording couldn't start: the page offers them."""
        with self._lock:
            return set(self._failed)

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
            held = set(self._holding)  # the calls on the mic at the last look
            self._follow(apps, now)
            settings = self._settings()
            current = self._recording()
            if self._last is not None and (current is None or current["meeting_id"] != self._last):
                # It ended since the last look, and not here (Stop, the tray, a quit): the
                # calls it was recording aren't recorded again until their apps let go.
                self._declined.update(held.intersection(self._holding))
            self._last = None if current is None else current["meeting_id"]
            if current is None:
                self._not_recording(apps, now, settings)
            else:
                self._while_recording(current, now, settings)

    def _follow(self, apps: list[str], now: float) -> None:
        """Which call apps hold the mic: one away for a moment still does, and one back soon
        after a call is that call. Declines and failed starts last until the app lets go."""
        for app in apps:
            if app not in self._holding:
                let_go = self._let_go.pop(app, None)
                if self._continuing or (let_go is not None and now - let_go <= SAME_CALL_WITHIN):
                    self._same_call.add(app)
                self._holding[app] = now
            self._gone.pop(app, None)
        self._continuing = False  # only the calls on the mic at the first look
        for app in list(self._holding):
            if app in apps:
                continue
            gone_at = self._gone.setdefault(app, now)
            if now - gone_at < LET_GO:
                continue
            since = self._holding.pop(app)
            del self._gone[app]
            if gone_at - since >= CALL_AFTER or app in self._same_call:
                self._let_go[app] = gone_at  # a call: back within a few minutes, the same one
            self._same_call.discard(app)
        self._declined.intersection_update(self._holding)
        self._failed.intersection_update(self._holding)
        for app in [app for app, at in self._let_go.items() if now - at > SAME_CALL_WITHIN]:
            del self._let_go[app]

    def _not_recording(self, apps: list[str], now: float, settings: dict) -> None:
        self.auto = self.stopping = None
        self._kept = {"call": False, "quiet": False}
        if not settings["auto"]:
            return
        for app in apps:  # holding the mic right now, not just away for a moment
            if app not in settings["apps"] or app in self._declined or app in calls.BROWSERS:
                continue
            self._declined.add(app)  # one recording per call, even if starting fails
            try:
                meeting_id = self._start(app)
            except Busy:
                self._declined.discard(app)  # a sound check, say: tried again at the next look
                return
            except Exception as exc:
                self._failed.add(app)
                self._event("failed", app, None, detail=str(exc))
                return
            self.auto = {
                "app": app,
                "meeting_id": meeting_id,
                "at": now,
                "confirmed": app in self._same_call,  # back from hold: still that call
                "gone_at": None,  # since when none of its call apps holds the mic
            }
            self._last = meeting_id
            self._event("started", app, meeting_id)
            return

    def _while_recording(self, current: dict, now: float, settings: dict) -> None:
        auto = self.auto
        if auto is not None and auto["meeting_id"] != current["meeting_id"]:
            auto = self.auto = None  # a recording this didn't start
        quiet_for = current.get("quiet_for") or 0.0
        if auto is None:
            # One you started: the call ended once its watch says the call app let go (after
            # a call of a minute or more) and nobody has been heard for a few seconds, so a
            # room still talking after a dial-in isn't cut off.
            ended_for = (current.get("call") or {}).get("ended_for")
            call_on = ended_for is None
            call_over = not call_on and quiet_for >= QUIET_AFTER_CALL
            stop_rules = settings["stop"] and not current.get("paused")
        else:
            call_on = self._follow_call(auto, current, now, settings)
            if call_on is None:
                return  # deleted: not a call after all
            gone_for = 0.0 if call_on else now - auto["gone_at"]
            # The call's own audio: the others still talking (a headset switching) means the
            # call goes on; music after the call stops it at most half a minute later.
            call_quiet = current.get("call_quiet_for")
            call_quiet = quiet_for if call_quiet is None else call_quiet
            call_over = not call_on and (call_quiet >= QUIET_AFTER_CALL or gone_for >= GONE_AT_MOST)
            stop_rules = True  # nobody pressed Start for it: it stops with its call, paused or not
        if call_on:
            self._kept["call"] = False
        if quiet_for < QUIET_FOR:
            self._kept["quiet"] = False
        reason = None
        if stop_rules:
            if call_over and not self._kept["call"]:
                reason = "call"
            elif (
                not (current.get("call") or {}).get("apps")
                and quiet_for >= QUIET_FOR
                and not self._kept["quiet"]
            ):
                reason = "quiet"
        meeting_id = current["meeting_id"]
        stopping = self.stopping
        if reason is None:
            self.stopping = None
            return
        if stopping is None or stopping["reason"] != reason or stopping["meeting_id"] != meeting_id:
            wait = STOP_IN if reason == "call" else QUIET_STOP_IN
            self.stopping = {"reason": reason, "deadline": now + wait, "meeting_id": meeting_id}
            return
        if now < stopping["deadline"]:
            return
        if auto is not None and not current.get("heard", True) and not current.get("notes"):
            self._end(current, auto, "discard")  # nobody spoke at all: nothing worth keeping
            return
        self._end(current, auto, "stop", reason)

    def _follow_call(self, auto: dict, current: dict, now: float, settings: dict) -> bool | None:
        """Whether the call of a recording this started goes on: any chosen call app (or the
        one that started it) holding the mic. A call that turned out to be a mic test is
        deleted here (then None)."""
        on_call = [app for app in self._holding if app == auto["app"] or app in settings["apps"]]
        if on_call and auto["app"] not in on_call:
            auto["app"] = on_call[0]  # the Teams preview closed, the phone call goes on
        if not auto["confirmed"]:
            if any(
                app in self._same_call or now - self._holding[app] >= CALL_AFTER for app in on_call
            ):
                auto["confirmed"] = True  # a minute on the mic (or back from hold): a call
            elif not on_call:
                # Let go within a minute: a mic test or a preview, unless something in it is
                # worth keeping (a short call).
                if not (current.get("notes") or current.get("call_heard")):
                    self._end(current, auto, "discard")
                    return None
                auto["confirmed"] = True
        if on_call:
            auto["gone_at"] = None
        elif auto["gone_at"] is None:
            auto["gone_at"] = now
        return bool(on_call)

    def _end(self, current: dict, auto: dict | None, how: str, reason: str = "call") -> None:
        """Stops the recording, or deletes it ("discard"), and says so once. Ended here, so
        the next look doesn't take it for a Stop pressed mid-call."""
        meeting_id = current["meeting_id"]
        seen = current.get("call_seen") or []
        app = auto["app"] if auto is not None else (seen[0] if seen else None)
        self.auto = self.stopping = None
        self._last = None
        if how == "discard":
            outcome = self._discard(meeting_id, True)
            if outcome == "deleted":
                self._event("discarded", app, meeting_id)
            elif outcome == "kept":  # a note came in just then: saved instead
                self._event("stopped", app, meeting_id, reason=reason)
            return
        if self._stop(meeting_id) is not None:
            self._event("stopped", app, meeting_id, reason=reason)

    def _event(self, kind: str, app: str | None, meeting_id: int | None, **more) -> dict:
        event = {"id": next(self._ids), "kind": kind, "app": app, "meeting_id": meeting_id}
        event.update(more)
        self.events.append(event)
        return event
