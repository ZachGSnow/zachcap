"""The recording watch: warnings while it runs, and when the call ended."""

from pathlib import Path
from types import SimpleNamespace

from zachcap.watchdog import KeepAwake, Watch


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def setup(tmp_path: Path, apps: list[str] | None = None, free: int = 50 * 2**30):
    clock = Clock()
    tracks = {
        "mic": SimpleNamespace(signal_at=None, heard_at=None),
        "system": SimpleNamespace(signal_at=None, heard_at=None),
    }
    recorder = SimpleNamespace(tracks=tracks, paused=False)
    calls = {"apps": list(apps or []), "free": free}
    watch = Watch(
        recorder,
        tmp_path,
        apps_using_mic=lambda: list(calls["apps"]),
        clock=clock,
        disk_free=lambda: calls["free"],
    )
    return watch, clock, tracks, recorder, calls


def keys(watch: Watch) -> set[str]:
    return {warning["key"] for warning in watch.status()["warnings"]}


def test_a_muted_microphone_is_noticed_after_a_minute_and_cleared(tmp_path: Path):
    watch, clock, tracks, recorder, _ = setup(tmp_path)
    clock.now += 59
    watch.check()
    assert "mic-off" not in keys(watch)
    clock.now += 2
    watch.check()
    assert "mic-off" in keys(watch)

    tracks["mic"].signal_at = clock.now  # unmuted: sound again
    watch.check()
    assert "mic-off" not in keys(watch)

    recorder.paused = True  # a private pause is silent on purpose
    clock.now += 120
    watch.check()
    assert not keys(watch)


def test_a_call_with_no_call_audio_at_all_is_flagged(tmp_path: Path):
    watch, clock, tracks, _, calls = setup(tmp_path, apps=["Microsoft Teams"])
    watch.check()  # the call app takes the mic
    tracks["mic"].signal_at = tracks["mic"].heard_at = clock.now + 100
    clock.now += 121
    watch.check()
    status = watch.status()
    assert keys(watch) == {"no-call-audio"}
    assert "Microsoft Teams" in status["warnings"][0]["text"]

    tracks["system"].heard_at = clock.now  # the other side was heard once: fine from now on
    clock.now += 600
    tracks["mic"].signal_at = clock.now
    watch.check()
    assert not keys(watch)

    other, other_clock, _, _, _ = setup(tmp_path)  # no call app: an in-room meeting
    other_clock.now += 300
    other.recorder.tracks["mic"].signal_at = other_clock.now
    other.check()
    assert not keys(other)


def test_the_end_of_the_call_is_when_the_app_lets_go_of_the_mic(tmp_path: Path):
    watch, clock, _, _, calls = setup(tmp_path)
    watch.check()
    assert watch.status()["call"] == {"apps": [], "ended_for": None}  # no call yet

    calls["apps"] = ["Microsoft Teams"]
    watch.check()
    clock.now += 60
    watch.check()  # held for a minute: a call
    assert watch.status()["call"] == {"apps": ["Microsoft Teams"], "ended_for": None}

    calls["apps"] = []
    clock.now += 5
    watch.check()
    clock.now += 12.5
    assert watch.status()["call"]["ended_for"] == 12.5

    calls["apps"] = ["Microsoft Teams"]  # back on a call: not ended after all
    watch.check()
    assert watch.status()["call"]["ended_for"] is None


def test_quiet_is_the_time_since_anyone_was_heard(tmp_path: Path):
    watch, clock, tracks, _, _ = setup(tmp_path)
    tracks["mic"].heard_at = clock.now + 10
    tracks["system"].heard_at = clock.now + 40
    clock.now += 100
    assert watch.status()["quiet_for"] == 60.0


def test_a_nearly_full_disk_is_flagged(tmp_path: Path):
    watch, _, tracks, _, calls = setup(tmp_path, free=100 * 2**20)
    tracks["mic"].signal_at = watch.started
    watch.check()
    assert keys(watch) == {"low-disk"} and "100 MB" in watch.status()["warnings"][0]["text"]
    calls["free"] = 10 * 2**30
    watch.check()
    assert not keys(watch)


def test_keeping_awake_starts_and_stops(tmp_path: Path):
    awake = KeepAwake()
    awake.start()
    awake.stop()
    assert awake._thread is not None and not awake._thread.is_alive()


def test_a_quick_look_at_the_mic_is_not_a_call(tmp_path: Path):
    # Windows' Sound settings, a mic test or a screen recorder letting go of the mic must
    # never count as a call ending (that would start a countdown to stop the meeting).
    watch, clock, _, _, calls = setup(tmp_path)
    for apps in (["SystemSettings"], ["obs64"], []):
        calls["apps"] = apps
        clock.now += 300
        watch.check()
    calls["apps"] = ["Microsoft Teams"]  # Teams for 20 s only
    watch.check()
    clock.now += 20
    watch.check()
    calls["apps"] = []
    clock.now += 30
    watch.check()
    assert watch.status()["call"]["ended_for"] is None


def test_a_private_pause_is_not_quiet(tmp_path: Path):
    watch, clock, tracks, recorder, _ = setup(tmp_path)
    tracks["mic"].heard_at = tracks["system"].heard_at = clock.now
    recorder.paused = True
    clock.now += 900
    assert watch.status()["quiet_for"] == 0.0
    recorder.paused = False
    recorder.resumed_at = clock.now
    clock.now += 30
    assert watch.status()["quiet_for"] == 30.0  # counted from the end of the pause
