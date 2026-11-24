"""Watch a recording while it runs, and keep the computer awake meanwhile.

Recorders lose meetings quietly: a muted mic, call audio on a device nobody records, a
laptop that dozes off during a long listen-only call, a full disk, a recording left running
after the call. This says so while there is still time to fix it, and tells the app when the
call app let go of the microphone (the call ended), so it can offer to stop.

Two levels per track (kept by the tracks themselves): any signal at all (a muted or broken
mic delivers exact zeros) and speech-level sound (for "nothing heard" and quiet stretches).
"""

from __future__ import annotations

import ctypes
import shutil
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

MIC_OFF_AFTER = 60.0  # no signal at all from the mic for this long: muted or broken
NO_CALL_AUDIO_AFTER = 120.0  # in a call this long without ever hearing the other side
LOW_DISK_BYTES = 500 * 1024 * 1024  # about three hours of recording
# A meeting app (Teams, Zoom, a browser...) holding the mic this long is a call; Windows
# Settings' input meter, a quick mic test or a screen recorder never count.
CALL_AFTER = 60.0
# A meeting app gone from the mic this long has let go of it: Teams closes and reopens the
# mic for a moment when you join from the preview, and that is neither the end of the call
# nor the start of a new one.
LET_GO = 3.0
CHECK_EVERY = 2.0

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Keeps Windows from going to sleep while a recording runs (the screen may still turn
    off). The request belongs to the thread that made it, so it lives on its own thread."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._hold, name="keep-awake", daemon=True)
        self._thread.start()

    def _hold(self) -> None:
        try:
            set_state = ctypes.windll.kernel32.SetThreadExecutionState
        except AttributeError:  # not Windows
            return
        set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self._done.wait()
        set_state(ES_CONTINUOUS)

    def stop(self) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


class Watch:
    """Checks a running recorder every few seconds. ``status()`` is what /api/record shows."""

    def __init__(
        self,
        recorder,
        folder: Path,
        apps_using_mic: Callable[[], list[str]] | None = None,
        clock: Callable[[], float] = time.perf_counter,
        disk_free: Callable[[], int] | None = None,
        meeting_app: Callable[[str], bool] | None = None,
    ) -> None:
        from . import calls

        self.recorder = recorder
        self._apps = apps_using_mic or calls.apps_using_mic
        self._meeting_app = meeting_app or calls.is_meeting_app
        self._clock = clock
        self._disk_free = disk_free or (lambda: shutil.disk_usage(folder).free)
        self.started = clock()
        self.call_apps: list[str] = []
        self.call_seen: list[str] = []  # meeting apps that held the mic long enough
        self.call_ended_at: float | None = None
        self._holding: dict[str, float] = {}  # meeting app -> since when it holds the mic
        self._gone: dict[str, float] = {}  # meeting app -> since when it's away (not yet let go)
        self.warnings: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._awake = KeepAwake()

    def start(self) -> None:
        self._awake.start()
        self.check()
        self._thread = threading.Thread(target=self._run, name="recording-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._awake.stop()

    def _run(self) -> None:
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.check()
            except Exception:  # a check must never end the recording
                traceback.print_exc()

    def _since(self, track_name: str, attribute: str) -> float:
        """Seconds since the track last had that level (since the start if never)."""
        track = getattr(self.recorder, "tracks", {}).get(track_name)
        when = getattr(track, attribute, None)
        return self._clock() - (when if when is not None else self.started)

    def check(self) -> None:
        now = self._clock()
        try:
            apps = self._apps()
        except OSError:
            apps = []
        self.call_apps = apps
        meeting = [app for app in apps if self._meeting_app(app)]
        for app in meeting:
            self._holding.setdefault(app, now)
            self._gone.pop(app, None)
        let_go_at = None
        for app in list(self._holding):
            if app in meeting:
                continue
            gone_at = self._gone.setdefault(app, now)
            if now - gone_at >= LET_GO:  # away a moment still holds it
                del self._holding[app], self._gone[app]
                let_go_at = gone_at if let_go_at is None else max(let_go_at, gone_at)
        # Held a minute or more (until it went away, for one away now): a call.
        calls = [
            app
            for app, since in self._holding.items()
            if self._gone.get(app, now) - since >= CALL_AFTER
        ]
        self.call_seen += [app for app in calls if app not in self.call_seen]
        if self._holding:
            self.call_ended_at = None  # on a call (again)
        elif self.call_seen and self.call_ended_at is None:
            # The call app let go of the microphone: counted from when it went away.
            self.call_ended_at = now if let_go_at is None else let_go_at

        paused = bool(getattr(self.recorder, "paused", False))
        self._warn(
            "mic-off",
            not paused and self._since("mic", "signal_at") >= MIC_OFF_AFTER,
            "Nothing at all from your microphone for a minute. Is it muted (the mic key on "
            "the keyboard), or is another microphone picked in Settings?",
        )
        system = getattr(self.recorder, "tracks", {}).get("system")
        never_heard = system is not None and getattr(system, "heard_at", None) is None
        self._warn(
            "no-call-audio",
            not paused
            and bool(calls)
            and never_heard
            and now - self.started >= NO_CALL_AUDIO_AFTER,
            "No call audio yet, two minutes into the call with "
            f"{calls[0] if calls else 'the app'}. If you can hear the others, check Settings, "
            "Recording, Computer audio from.",
        )
        try:
            free = self._disk_free()
        except OSError:
            free = LOW_DISK_BYTES  # unknown: don't cry wolf
        self._warn(
            "low-disk",
            free < LOW_DISK_BYTES,
            f"Only {free // (1024 * 1024)} MB free on the drive with your recordings. Free up "
            "some space, or the recording will stop when it's full.",
        )

    def _warn(self, key: str, on: bool, text: str) -> None:
        if on:
            self.warnings[key] = text
        else:
            self.warnings.pop(key, None)

    def quiet_for(self, *tracks: str) -> float:
        """Seconds since anyone was heard on those tracks (both when none are named): "system"
        alone is the call's side, which talk in the room doesn't reach."""
        now = self._clock()
        quiet = min(self._since(name, "heard_at") for name in tracks or ("mic", "system"))
        # A private pause is silent on purpose: quiet starts again when it ends.
        if getattr(self.recorder, "paused", False):
            quiet = 0.0
        resumed = getattr(self.recorder, "resumed_at", None)
        if resumed is not None:
            quiet = min(quiet, now - resumed)
        return round(quiet, 1)

    def status(self) -> dict:
        now = self._clock()
        ended_at = self.call_ended_at  # read once: the watch's own look may change it meanwhile
        return {
            "warnings": [{"key": key, "text": text} for key, text in self.warnings.items()],
            "call": {
                "apps": self.call_apps,
                "ended_for": None if ended_at is None else round(now - ended_at, 1),
            },
            "quiet_for": self.quiet_for(),
        }
