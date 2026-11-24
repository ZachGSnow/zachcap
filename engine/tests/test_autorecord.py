"""Calls recorded on their own: started when a call app takes the mic, stopped when it ends."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zachcap import autorecord, capture, pipeline
from zachcap.api import create_app
from zachcap.autorecord import CALL_AFTER, LET_GO, QUIET_FOR, QUIET_STOP_IN, STOP_IN, CallWatch
from zachcap.store import Store
from zachcap.watchdog import Watch

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}


class Laptop:
    """The apps on the mic, the recording with its own watch (the real one), and what the call
    watch did: all pretend, on one clock, looked at every couple of seconds as the engine does."""

    def __init__(self, continuing: bool = False, **settings) -> None:
        self.now = 1000.0
        self.apps: list[str] = []
        self.settings = {"auto": True, "apps": set(autorecord.DEFAULT_APPS), "stop": True}
        self.settings.update(settings)
        self.recording: dict | None = None
        self.meetings = 0
        self.done: list[tuple] = []
        self.talking = False  # someone in the room is speaking: the mic hears it
        self.call_talking = False  # the other side of the call (or music): the computer's audio
        self.watch = CallWatch(
            settings=lambda: self.settings,
            recording=self.now_recording,
            start=self.start,
            stop=self.stop,
            discard=self.discard,
            apps_using_mic=lambda: list(self.apps),
            clock=lambda: self.now,
            continuing=continuing,
        )

    def start(self, app: str) -> int:
        """A recording starts: by the call watch, or by hand (by_hand)."""
        self.meetings += 1
        tracks = {
            "mic": SimpleNamespace(signal_at=self.now, heard_at=None),
            "system": SimpleNamespace(signal_at=self.now, heard_at=None),
        }
        recorder = SimpleNamespace(tracks=tracks, paused=False, resumed_at=None)
        watch = Watch(
            recorder,
            Path("."),
            apps_using_mic=lambda: list(self.apps),
            clock=lambda: self.now,
            disk_free=lambda: 10**12,
        )
        watch.check()  # as Watch.start() does
        self.recording = {"meeting_id": self.meetings, "recorder": recorder, "watch": watch}
        self.recording["notes"] = 0
        self.done.append(("start", app))
        return self.meetings

    def by_hand(self) -> int:
        return self.start("you")

    def stop(self, meeting_id: int | None = None) -> int | None:
        if self.recording is None or meeting_id not in (None, self.recording["meeting_id"]):
            return None
        meeting_id = self.recording["meeting_id"]
        self.recording = None
        self.done.append(("stop", meeting_id))
        return meeting_id

    def stop_by_hand(self) -> None:
        """Stop pressed in ZachCap (or the tray): no word to the call watch."""
        self.stop()

    def discard(self, meeting_id: int, only_if_worthless: bool = False) -> str:
        """As api.py's discard_recording: kept instead when it turns out worth keeping."""
        if self.recording is None or meeting_id != self.recording["meeting_id"]:
            return "gone"
        worth = self.recording["notes"] or self.heard("system")
        self.recording = None
        if only_if_worthless and worth:
            self.done.append(("stop", meeting_id))
            return "kept"
        self.done.append(("discard", meeting_id))
        return "deleted"

    def heard(self, track: str) -> bool:
        return self.recording["recorder"].tracks[track].heard_at is not None

    def now_recording(self) -> dict | None:
        """What api.py's recording_now() says about it."""
        if self.recording is None:
            return None
        watch, tracks = self.recording["watch"], self.recording["recorder"].tracks
        status = watch.status()
        return {
            "meeting_id": self.recording["meeting_id"],
            "paused": self.recording["recorder"].paused,
            "call": status["call"],
            "quiet_for": status["quiet_for"],
            "call_quiet_for": watch.quiet_for("system"),
            "call_seen": list(watch.call_seen),
            "heard": any(track.heard_at is not None for track in tracks.values()),
            "call_heard": self.heard("system"),
            "notes": self.recording["notes"],
        }

    def tick(self, seconds: float, step: float = 2.0) -> None:
        """Time passes, looked at every couple of seconds: the recording's watch, then the
        call watch."""
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = min(end, self.now + step)
            if self.recording is not None:
                tracks = self.recording["recorder"].tracks
                if self.talking:
                    tracks["mic"].heard_at = self.now
                if self.call_talking:
                    tracks["system"].heard_at = self.now
                self.recording["watch"].check()
            self.watch.check()

    def until_countdown(self, at_most: float = 60.0) -> float:
        """Looks until the countdown to stop starts; returns how long that took."""
        waited = 0.0
        while self.watch.status()["stopping"] is None and waited < at_most:
            self.tick(2)
            waited += 2
        return waited

    def starts(self) -> list[str]:
        return [what for kind, what in self.done if kind == "start"]

    def outcomes(self) -> list[str]:
        return [kind for kind, _ in self.done if kind != "start"]

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.watch.events]


def test_a_teams_call_is_recorded_from_its_first_second_and_stops_when_it_ends():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(2)
    assert laptop.starts() == ["Microsoft Teams"]  # at once: the first words count
    assert laptop.watch.status()["auto"] == {
        "app": "Microsoft Teams",
        "meeting_id": 1,
        "confirmed": False,
    }
    laptop.tick(CALL_AFTER)
    assert laptop.watch.status()["auto"]["confirmed"]  # a minute on the mic: a call

    laptop.apps, laptop.call_talking = [], False  # you left the call
    assert laptop.until_countdown() <= 12  # let go, and the call's side quiet a few seconds
    assert laptop.watch.status(1)["stopping"] == {"reason": "call", "in": STOP_IN}
    laptop.tick(STOP_IN)
    assert laptop.done[-1] == ("stop", 1) and laptop.recording is None
    assert laptop.watch.events[-1] == {
        "id": 2,
        "kind": "stopped",
        "app": "Microsoft Teams",
        "meeting_id": 1,
        "reason": "call",
    }
    laptop.tick(20)
    assert laptop.kinds() == ["started", "stopped"]  # not started again


def test_3cx_and_the_apps_you_chose_start_one_but_never_a_browser():
    laptop = Laptop()
    laptop.apps = ["3CX"]
    laptop.tick(2)
    assert laptop.starts() == ["3CX"]

    for apps, chosen in ((["Edge"], {"Edge"}), (["Slack"], set(autorecord.DEFAULT_APPS))):
        other = Laptop(apps=chosen)
        other.apps = apps
        other.tick(10)
        assert other.done == []  # a website using the mic, or an app you didn't choose

    off = Laptop(auto=False)
    off.apps = ["Microsoft Teams"]
    off.tick(10)
    assert off.done == []
    assert autorecord.chosen("Microsoft Teams, Edge, Nonsense") == {"Microsoft Teams"}
    assert "Edge" not in autorecord.choosable() and "3CX" in autorecord.choosable()


def test_a_quick_look_at_the_mic_is_deleted_not_kept():
    laptop = Laptop()
    laptop.talking = True  # you, trying the mic: nobody on the other end
    laptop.apps = ["Microsoft Teams"]  # the preview before joining, or a mic test
    laptop.tick(20)
    laptop.apps = []
    laptop.tick(LET_GO + 3)
    assert laptop.done == [("start", "Microsoft Teams"), ("discard", 1)]
    assert laptop.kinds() == ["started", "discarded"]


def test_joining_from_the_preview_doesnt_end_it():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(10)
    laptop.apps = []  # Teams reopens the mic as you join
    laptop.tick(LET_GO - 1, step=1)
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(CALL_AFTER)
    assert laptop.starts() == ["Microsoft Teams"] and laptop.outcomes() == []
    assert laptop.watch.status()["auto"]["confirmed"]


def test_dont_record_deletes_it_until_the_app_lets_go():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(2)
    event = laptop.watch.decline(1)
    assert event["kind"] == "declined" and laptop.done[-1] == ("discard", 1)
    laptop.tick(CALL_AFTER * 3)
    assert laptop.meetings == 1  # this call isn't recorded again
    laptop.apps = []
    laptop.tick(LET_GO + 3)
    laptop.apps = ["Microsoft Teams"]  # the next call is
    laptop.tick(2)
    assert laptop.done[-1] == ("start", "Microsoft Teams")

    with pytest.raises(LookupError):
        Laptop().watch.decline()  # nothing it started
    with pytest.raises(LookupError):
        laptop.watch.decline(99)  # not the recording the page showed the button for


def test_stopped_by_you_mid_call_isnt_started_again():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(2)
    laptop.stop_by_hand()  # Stop pressed in ZachCap, the call goes on
    laptop.tick(CALL_AFTER)
    assert laptop.meetings == 1


def test_stopped_by_hand_mid_call_isnt_started_again_whoever_started_it():
    laptop = Laptop()
    laptop.talking = laptop.call_talking = True
    laptop.by_hand()  # Ctrl+Alt+R before joining
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(30 * 60)
    laptop.stop_by_hand()  # "thanks, everyone": Stop, still on the call
    laptop.tick(60)
    assert laptop.meetings == 1  # not recorded again behind your back
    laptop.apps = []  # hang up
    laptop.tick(5 * 60)
    laptop.apps = ["Microsoft Teams"]  # the next call is recorded
    laptop.tick(4)
    assert laptop.starts() == ["you", "Microsoft Teams"]


def test_keep_recording_holds_until_the_call_app_is_back():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(CALL_AFTER + 2)
    laptop.apps, laptop.call_talking = [], False
    laptop.until_countdown()
    assert not laptop.watch.keep(99)  # a countdown the page showed for another recording
    assert laptop.watch.keep(1)
    laptop.tick(5 * 60)
    assert laptop.recording is not None and laptop.watch.status()["stopping"] is None

    laptop.apps = ["Microsoft Teams"]  # back on the call
    laptop.tick(10)
    laptop.apps = []  # and this time it ends
    laptop.tick(30)
    assert laptop.recording is None


def test_a_recording_you_started_still_stops_when_the_call_ends():
    laptop = Laptop(auto=False)
    laptop.by_hand()
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(CALL_AFTER + 2)
    laptop.apps = []
    laptop.tick(30)
    assert laptop.done[-1] == ("stop", 1)
    assert laptop.watch.events[-1]["app"] == "Microsoft Teams"

    off = Laptop(auto=False, stop=False)  # Stop when the call ends: off
    off.by_hand()
    off.apps = ["Microsoft Teams"]
    off.tick(CALL_AFTER + 2)
    off.apps = []
    off.tick(30 * 60)
    assert off.recording is not None  # stopping one you started is up to you


def test_nobody_heard_for_ten_minutes_with_no_call_app_stops_a_minute_later():
    laptop = Laptop(auto=False)
    laptop.by_hand()  # an in-room meeting
    laptop.tick(QUIET_FOR)
    assert laptop.watch.status()["stopping"]["reason"] == "quiet"
    laptop.talking = True  # someone spoke
    laptop.tick(2)
    assert laptop.watch.status()["stopping"] is None
    laptop.talking = False
    laptop.tick(QUIET_FOR + QUIET_STOP_IN + 2)
    assert laptop.recording is None and laptop.watch.events[-1]["reason"] == "quiet"


def test_a_call_where_nobody_was_heard_is_deleted():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(CALL_AFTER + 2)  # the preview left open, nobody joined
    laptop.apps = []
    laptop.tick(30)
    assert laptop.done[-1] == ("discard", 1) and laptop.kinds()[-1] == "discarded"


def test_a_nobody_heard_call_with_a_note_is_kept():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(CALL_AFTER + 10)
    laptop.recording["notes"] = 1
    laptop.apps = []
    laptop.tick(60)
    assert laptop.outcomes() == ["stop"]


def test_a_recording_that_wont_start_isnt_retried_all_call_long():
    laptop = Laptop()

    def broken(app):
        raise RuntimeError("No microphone")

    laptop.watch._start = broken
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(60)
    assert laptop.kinds() == ["failed"]
    assert laptop.watch.events[-1]["detail"] == "No microphone"
    assert laptop.watch.failed() == {"Microsoft Teams"}  # the page offers it instead
    laptop.apps = []
    laptop.tick(LET_GO + 3)
    assert laptop.watch.failed() == set()


def test_a_busy_start_is_tried_again_at_the_next_look():
    laptop = Laptop()
    started = laptop.start
    busy = {"looks": 2}

    def start(app: str) -> int:
        if busy["looks"]:
            busy["looks"] -= 1
            raise autorecord.Busy("A sound check is running")
        return started(app)

    laptop.watch._start = start
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(6)
    assert laptop.starts() == ["Microsoft Teams"]
    assert laptop.kinds() == ["started"]


def test_switched_on_mid_call_it_still_stops_when_the_call_ends():
    laptop = Laptop(auto=False)
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(10 * 60)
    laptop.settings["auto"] = True  # "Record calls on their own" switched on mid-call
    laptop.tick(4)
    assert laptop.starts() == ["Microsoft Teams"]
    laptop.tick(20)
    laptop.apps, laptop.call_talking = [], False  # the call ends 24 s later
    laptop.tick(60)
    assert laptop.recording is None and laptop.outcomes() == ["stop"]


def test_a_blip_as_you_join_doesnt_keep_it_recording_after_the_call():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]  # the preview: recording at once
    laptop.tick(40)
    laptop.apps = []  # Teams closes and reopens the mic as you join (one look)
    laptop.tick(2)
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(40)  # a short meeting
    laptop.apps, laptop.call_talking = [], False  # leave
    laptop.talking = True  # people keep talking in the room
    laptop.tick(60)
    assert laptop.recording is None and laptop.outcomes() == ["stop"]


def test_a_short_call_with_a_note_or_the_other_side_heard_is_kept():
    noted = Laptop()
    noted.apps = ["3CX"]
    noted.tick(20)
    noted.recording["notes"] = 1  # "call back Tuesday", typed in the recording bar
    noted.tick(30)
    noted.apps = []  # a 50 s phone call
    noted.tick(60)
    assert noted.outcomes() == ["stop"] and noted.watch.events[-1]["kind"] == "stopped"

    heard = Laptop()
    heard.call_talking = True  # the caller was heard
    heard.apps = ["3CX"]
    heard.tick(50)
    heard.apps, heard.call_talking = [], False
    heard.tick(60)
    assert heard.outcomes() == ["stop"]

    test = Laptop()
    test.talking = True  # a mic test: only you, nobody on the other end
    test.apps = ["Microsoft Teams"]
    test.tick(20)
    test.apps = []
    test.tick(10)
    assert test.outcomes() == ["discard"]  # still not kept


def test_back_from_hold_is_the_same_call_not_a_mic_test():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["3CX"]
    laptop.tick(5 * 60)
    laptop.apps, laptop.call_talking = [], False  # on hold: the softphone closes the mic
    laptop.tick(40)
    laptop.apps = ["3CX"]  # back: the resolution and next steps (you talk, they listen)
    laptop.talking = True
    laptop.tick(45)
    laptop.apps, laptop.talking = [], False  # hang up
    laptop.tick(60)
    assert laptop.outcomes() == ["stop", "stop"]  # both parts kept
    assert laptop.starts() == ["3CX", "3CX"]


def test_only_the_same_app_back_soon_is_the_same_call():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(5 * 60)
    laptop.apps, laptop.call_talking = [], False
    laptop.tick(30)
    laptop.talking = True
    laptop.apps = ["3CX"]  # another app a moment later, trying the mic
    laptop.tick(20)
    laptop.apps = []
    laptop.tick(10)
    assert laptop.outcomes() == ["stop", "discard"]
    laptop.apps = ["Microsoft Teams"]  # Teams back within a few minutes: that call again
    laptop.tick(20)
    laptop.apps, laptop.talking = [], False
    laptop.tick(30)
    assert laptop.outcomes() == ["stop", "discard", "stop"]


def test_after_a_restart_the_call_on_the_mic_is_the_one_that_was_being_recorded():
    laptop = Laptop(continuing=True)  # the engine started again mid-call
    laptop.talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(40)
    laptop.apps, laptop.talking = [], False  # its last 40 s
    laptop.tick(60)
    assert laptop.outcomes() == ["stop"]

    fresh = Laptop()  # started as usual: a mic test then is still a mic test
    fresh.talking = True
    fresh.apps = ["Microsoft Teams"]
    fresh.tick(40)
    fresh.apps = []
    fresh.tick(60)
    assert fresh.outcomes() == ["discard"]


def test_dont_record_after_the_call_ended_doesnt_skip_the_next_call():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(90)
    laptop.apps, laptop.call_talking = [], False
    laptop.until_countdown()
    assert laptop.recording is not None
    laptop.watch.decline(1)  # Don't record, during the countdown
    laptop.tick(3600)
    laptop.apps = ["Microsoft Teams"]  # the next call, an hour later
    laptop.tick(4)
    assert laptop.starts() == ["Microsoft Teams", "Microsoft Teams"]


def test_its_own_recordings_stop_when_the_call_ends_even_with_stop_off():
    laptop = Laptop(stop=False)
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(10 * 60)
    laptop.apps, laptop.call_talking = [], False
    laptop.tick(60)
    assert laptop.recording is None and laptop.outcomes() == ["stop"]

    mine = Laptop(stop=False, auto=False)  # one you started: stopping it is up to you
    mine.by_hand()
    mine.apps = ["Microsoft Teams"]
    mine.tick(10 * 60)
    mine.apps = []
    mine.tick(30 * 60)
    assert mine.recording is not None


def test_paused_when_its_call_ends_it_still_stops():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(5 * 60)
    laptop.recording["recorder"].paused = True  # a private moment, then the call ends
    laptop.apps, laptop.call_talking = [], False
    laptop.tick(60)
    assert laptop.recording is None and laptop.outcomes() == ["stop"]


def test_the_end_of_the_call_waits_while_the_room_keeps_talking():
    laptop = Laptop(auto=False)
    laptop.talking = True  # an in-room meeting, recorded by hand
    laptop.by_hand()
    laptop.apps = ["Microsoft Teams"]  # someone dials in through Teams on this laptop
    laptop.tick(5 * 60)
    laptop.apps = []  # the Teams part ends, the room goes on
    laptop.tick(5 * 60)
    assert laptop.recording is not None
    laptop.talking = False  # the room goes quiet
    laptop.tick(30)
    assert laptop.recording is None and laptop.outcomes() == ["stop"]


def test_a_headset_switch_mid_call_doesnt_split_the_call():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(10 * 60)
    laptop.apps = []  # Teams closes the mic while the headset switches; the others talk on
    laptop.tick(12)
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(10 * 60)
    assert laptop.starts() == ["Microsoft Teams"] and laptop.recording is not None


def test_music_after_the_call_doesnt_keep_it_recording():
    laptop = Laptop()
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams"]
    laptop.tick(10 * 60)
    laptop.apps = []  # the call ends; music plays on
    laptop.tick(60)
    assert laptop.recording is None and laptop.outcomes() == ["stop"]


def test_with_two_call_apps_a_decline_or_stop_doesnt_record_the_other():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams", "3CX"]
    laptop.tick(2)
    laptop.watch.decline(1)  # Don't record
    laptop.tick(60)
    assert laptop.meetings == 1

    stopped = Laptop()
    stopped.apps = ["Microsoft Teams", "3CX"]
    stopped.tick(2)
    stopped.stop_by_hand()  # or Stop
    stopped.tick(60)
    assert stopped.meetings == 1


def test_a_preview_closed_during_a_phone_call_keeps_the_phone_call():
    laptop = Laptop()
    laptop.apps = ["Microsoft Teams"]  # the Teams preview: recording
    laptop.tick(20)
    laptop.call_talking = True
    laptop.apps = ["Microsoft Teams", "3CX"]  # a phone call comes in and is answered
    laptop.tick(10)
    laptop.apps = ["3CX"]  # the preview is closed
    laptop.tick(90)
    assert laptop.starts() == ["Microsoft Teams"] and laptop.recording is not None
    assert laptop.watch.status()["auto"]["app"] == "3CX"
    laptop.apps, laptop.call_talking = [], False
    laptop.tick(60)
    assert laptop.outcomes() == ["stop"]


class FakeRecorder:
    def __init__(self, folder: Path, **devices) -> None:
        self.folder, self.elapsed, self.tracks, self.paused = folder, 42.0, {}, False
        self.started_at = None

    def start(self) -> None:
        self.folder.mkdir(parents=True)

    def mute_mic(self, muted: bool = True) -> None:
        pass

    def levels(self) -> dict:
        return {}

    def errors(self) -> list:
        return []

    def stop(self) -> dict:
        return {"duration": 42.0}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    return TestClient(create_app("test-token"), base_url=LOCAL)


def on_the_mic(client: TestClient, monkeypatch: pytest.MonkeyPatch, apps: list[str]) -> dict:
    """Pretend call apps for the engine's call watch and each recording's watch, and a clock
    for the call watch. Returns both, to change as the test goes."""
    watch = client.app.state.call_watch
    pretend = {"apps": list(apps), "now": 1000.0}
    monkeypatch.setattr(watch, "_apps", lambda: list(pretend["apps"]))
    monkeypatch.setattr(watch, "_clock", lambda: pretend["now"])
    monkeypatch.setattr("zachcap.calls.apps_using_mic", lambda: list(pretend["apps"]))
    return pretend


def test_through_the_api(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    settings = client.get("/api/settings", headers=AUTH).json()
    assert settings["auto_record"] and "3CX" in settings["auto_record_apps"]
    assert "Edge" not in settings["auto_record_choices"]
    refused = client.put("/api/settings", headers=AUTH, json={"auto_record_apps": ["Edge"]})
    assert refused.status_code == 400
    saved = client.put(
        "/api/settings", headers=AUTH, json={"auto_record_apps": ["3CX", "Microsoft Teams"]}
    ).json()
    assert saved["auto_record_apps"] == ["3CX", "Microsoft Teams"]

    on_the_mic(client, monkeypatch, ["Microsoft Teams"])
    watch = client.app.state.call_watch
    assert client.get("/api/call", headers=AUTH).json() == {
        "apps": ["Microsoft Teams"],
        "auto": ["Microsoft Teams"],
    }
    watch.check()  # Teams took the mic: recording
    status = client.get("/api/record", headers=AUTH).json()
    assert status["recording"] and status["auto"]["app"] == "Microsoft Teams"
    assert status["auto"]["meeting_id"] == status["meeting_id"]
    assert status["events"][-1]["kind"] == "started"
    assert status["title"].startswith("Microsoft Teams call")
    folder = Path(next((tmp_path / "meetings").iterdir()))

    declined = client.post("/api/record/decline", headers=AUTH).json()
    assert declined["kind"] == "declined"
    assert not client.get("/api/record", headers=AUTH).json()["recording"]
    assert client.get("/api/meetings", headers=AUTH).json() == [] and not folder.exists()
    assert client.post("/api/record/decline", headers=AUTH).status_code == 409
    assert client.post("/api/record/keep", headers=AUTH).json() == {"kept": False}


def test_through_the_api_a_stop_mid_call_isnt_undone(client: TestClient, monkeypatch):
    on_the_mic(client, monkeypatch, ["Microsoft Teams"])
    watch = client.app.state.call_watch
    assert client.post("/api/record/start", headers=AUTH, json={"title": "Mine"}).status_code == 200
    watch.check()
    assert client.post("/api/record/stop", headers=AUTH).status_code == 200
    watch.check()
    assert not client.get("/api/record", headers=AUTH).json()["recording"]
    assert list(watch.events) == []  # not even tried


def test_through_the_api_a_short_call_with_a_note_is_kept(client: TestClient, monkeypatch):
    pretend = on_the_mic(client, monkeypatch, ["3CX"])
    watch = client.app.state.call_watch
    watch.check()
    meeting_id = client.get("/api/record", headers=AUTH).json()["meeting_id"]
    note = client.post("/api/record/note", headers=AUTH, json={"text": "call back Tuesday"})
    assert note.status_code == 200
    pretend["apps"] = []  # a 20 s phone call
    for _ in range(3):
        pretend["now"] += 2
        watch.check()
    assert client.get(f"/api/meetings/{meeting_id}", headers=AUTH).status_code == 200
    assert client.get("/api/record", headers=AUTH).json()["recording"]  # stops once quiet

    # A note that came in as it was being deleted keeps it too (it is saved instead).
    pretend["apps"] = ["Microsoft Teams"]
    client.post("/api/record/stop", headers=AUTH)
    pretend["now"] += 600
    watch.check()
    started = client.get("/api/record", headers=AUTH).json()
    assert started["recording"] and started["auto"]["app"] == "Microsoft Teams"
    store = Store()
    try:  # straight into the database: the call watch hasn't heard about it
        store.add_note(started["meeting_id"], 1.0, "bookmark", "")
    finally:
        store.close()
    pretend["apps"] = []
    for _ in range(3):
        pretend["now"] += 2
        watch.check()
    kept = client.get(f"/api/meetings/{started['meeting_id']}", headers=AUTH)
    assert kept.status_code == 200 and kept.json()["meeting"]["status"] != "recording"
    assert client.get("/api/record", headers=AUTH).json()["events"][-1]["kind"] == "stopped"


def test_events_come_after_a_cursor_and_a_new_engine_says_so(client: TestClient, monkeypatch):
    on_the_mic(client, monkeypatch, ["Microsoft Teams"])
    watch = client.app.state.call_watch
    first = client.get("/api/record", headers=AUTH).json()
    boot = first["boot"]
    assert first["events"] == []
    watch.check()  # started
    status = client.get(f"/api/record?boot={boot}&after=0", headers=AUTH).json()
    assert [event["kind"] for event in status["events"]] == ["started"]
    seen = status["events"][-1]["id"]
    assert client.get(f"/api/record?boot={boot}&after={seen}", headers=AUTH).json()["events"] == []
    declined = client.post(
        "/api/record/decline", headers=AUTH, json={"meeting_id": status["meeting_id"]}
    )
    assert declined.status_code == 200
    after = client.get(f"/api/record?boot={boot}&after={seen}", headers=AUTH).json()
    assert [event["kind"] for event in after["events"]] == ["declined"]
    # The page's cursor is from an engine that has since stopped: everything is news.
    other = client.get("/api/record?boot=gone&after=99", headers=AUTH).json()
    assert [event["kind"] for event in other["events"]] == ["started", "declined"]


def test_the_recording_bar_only_hears_about_its_own_recording(client: TestClient, monkeypatch):
    on_the_mic(client, monkeypatch, ["Microsoft Teams"])
    watch = client.app.state.call_watch
    watch.check()
    first = client.get("/api/record", headers=AUTH).json()
    assert first["auto"]["meeting_id"] == first["meeting_id"]
    client.post("/api/record/stop", headers=AUTH)
    client.post("/api/record/start", headers=AUTH, json={"title": "Mine"})  # before a look
    mine = client.get("/api/record", headers=AUTH).json()
    assert mine["meeting_id"] != first["meeting_id"]
    assert mine["auto"] is None and mine["stopping"] is None
    refused = client.post(
        "/api/record/decline", headers=AUTH, json={"meeting_id": first["meeting_id"]}
    )
    assert refused.status_code == 409
    assert client.get("/api/record", headers=AUTH).json()["recording"]


def test_a_stop_and_a_start_in_the_same_second_get_folders_of_their_own(
    client: TestClient, tmp_path: Path
):
    for title in ("First", "Second"):
        assert client.post("/api/record/start", headers=AUTH, json={"title": title}).is_success
        assert client.post("/api/record/stop", headers=AUTH).is_success
    folders = {meeting["folder"] for meeting in client.get("/api/meetings", headers=AUTH).json()}
    assert len(folders) == 2


def test_a_call_app_that_couldnt_start_is_offered_instead(client: TestClient, monkeypatch):
    on_the_mic(client, monkeypatch, ["Microsoft Teams"])

    def broken(self) -> None:
        raise RuntimeError("No microphone")

    monkeypatch.setattr(FakeRecorder, "start", broken)
    client.app.state.call_watch.check()
    assert client.get("/api/call", headers=AUTH).json() == {
        "apps": ["Microsoft Teams"],
        "auto": [],
    }
    event = client.get("/api/record", headers=AUTH).json()["events"][-1]
    assert event["kind"] == "failed"
    assert event["detail"] == "Couldn't start recording: No microphone"


def test_a_refused_settings_change_changes_nothing(client: TestClient):
    refused = client.put(
        "/api/settings",
        headers=AUTH,
        json={"auto_record": False, "stop_when_call_ends": False, "auto_record_apps": ["Edge"]},
    )
    assert refused.status_code == 400
    settings = client.get("/api/settings", headers=AUTH).json()
    assert settings["auto_record"] and settings["stop_when_call_ends"]
