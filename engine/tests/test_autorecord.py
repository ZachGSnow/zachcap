"""Calls recorded on their own: started when a call app takes the mic, stopped when it ends."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import autorecord, capture, pipeline
from zachcap.api import create_app
from zachcap.autorecord import CALL_AFTER, LET_GO, QUIET_FOR, QUIET_STOP_IN, STOP_IN, CallWatch

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}


class Phone:
    """The apps using the mic, the recording, and what the watch did: all pretend."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.apps: list[str] = []
        self.settings = {"auto": True, "apps": set(autorecord.DEFAULT_APPS), "stop": True}
        self.recording: dict | None = None
        self.done: list[tuple] = []
        self.meetings = 0
        self.watch = CallWatch(
            settings=lambda: self.settings,
            recording=lambda: self.recording,
            start=self.start,
            stop=self.stop,
            discard=self.discard,
            apps_using_mic=lambda: list(self.apps),
            clock=lambda: self.now,
        )

    def start(self, app: str) -> int:
        self.meetings += 1
        self.recording = {
            "meeting_id": self.meetings,
            "paused": False,
            "call": {"apps": [], "ended_for": None},
            "quiet_for": 0.0,
            "call_seen": [],
            "heard": True,
        }
        self.done.append(("start", app))
        return self.meetings

    def stop(self) -> int | None:
        meeting_id = self.recording["meeting_id"] if self.recording else None
        self.recording = None
        self.done.append(("stop", meeting_id))
        return meeting_id

    def discard(self) -> None:
        self.done.append(("discard", self.recording["meeting_id"]))
        self.recording = None

    def wait(self, seconds: float, step: float = 2.0) -> None:
        """Time passes, looked at every couple of seconds, as the engine does."""
        end = self.now + seconds
        while self.now < end:
            self.now = min(end, self.now + step)
            self.watch.check()

    def call_ended(self, ago: float) -> None:
        """What the recording's watchdog says once the call app let go ``ago`` seconds back."""
        self.recording["call"] = {"apps": [], "ended_for": ago}
        self.recording["call_seen"] = ["Microsoft Teams"]

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.watch.events]


def test_a_teams_call_is_recorded_from_its_first_second_and_stops_when_it_ends():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]
    phone.watch.check()
    assert phone.done == [("start", "Microsoft Teams")]  # at once: the first words count
    assert phone.watch.status()["auto"] == {
        "app": "Microsoft Teams",
        "meeting_id": 1,
        "confirmed": False,
    }
    phone.wait(CALL_AFTER + 2)
    assert phone.watch.status()["auto"]["confirmed"]  # a minute on the mic: a call

    phone.apps = []  # you left the call
    phone.call_ended(LET_GO)
    phone.watch.check()
    assert phone.watch.status()["stopping"] == {"reason": "call", "in": STOP_IN}
    phone.now += STOP_IN
    phone.watch.check()
    assert phone.done[-1] == ("stop", 1) and phone.recording is None
    assert phone.watch.events[-1] == {
        "id": 2,
        "kind": "stopped",
        "app": "Microsoft Teams",
        "meeting_id": 1,
        "reason": "call",
    }
    phone.wait(20)
    assert phone.kinds() == ["started", "stopped"]  # not started again


def test_3cx_and_the_apps_you_chose_start_one_but_never_a_browser():
    phone = Phone()
    phone.apps = ["3CX"]
    phone.watch.check()
    assert phone.done == [("start", "3CX")]

    for apps, chosen in ((["Edge"], {"Edge"}), (["Slack"], set(autorecord.DEFAULT_APPS))):
        other = Phone()
        other.settings["apps"] = chosen
        other.apps = apps
        other.wait(10)
        assert other.done == []  # a website using the mic, or an app you didn't choose

    off = Phone()
    off.settings["auto"] = False
    off.apps = ["Microsoft Teams"]
    off.wait(10)
    assert off.done == []
    assert autorecord.chosen("Microsoft Teams, Edge, Nonsense") == {"Microsoft Teams"}
    assert "Edge" not in autorecord.choosable() and "3CX" in autorecord.choosable()


def test_a_quick_look_at_the_mic_is_deleted_not_kept():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]  # the preview before joining, or a mic test
    phone.wait(20)
    phone.apps = []
    phone.wait(LET_GO + 2)
    assert phone.done == [("start", "Microsoft Teams"), ("discard", 1)]
    assert phone.kinds() == ["started", "discarded"]


def test_joining_from_the_preview_doesnt_end_it():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]
    phone.wait(10)
    phone.apps = []  # Teams reopens the mic as you join
    phone.wait(LET_GO - 1, step=1)
    phone.apps = ["Microsoft Teams"]
    phone.wait(CALL_AFTER)
    assert phone.done == [("start", "Microsoft Teams")]
    assert phone.watch.status()["auto"]["confirmed"]


def test_dont_record_deletes_it_until_the_app_lets_go():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]
    phone.watch.check()
    event = phone.watch.decline()
    assert event["kind"] == "declined" and phone.done[-1] == ("discard", 1)
    phone.wait(CALL_AFTER * 3)
    assert phone.meetings == 1  # this call isn't recorded again
    phone.apps = []
    phone.wait(LET_GO + 2)
    phone.apps = ["Microsoft Teams"]  # the next call is
    phone.watch.check()
    assert phone.done[-1] == ("start", "Microsoft Teams")

    with pytest.raises(LookupError):
        Phone().watch.decline()  # nothing it started


def test_stopped_by_you_mid_call_isnt_started_again():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]
    phone.watch.check()
    phone.recording = None  # Stop pressed in ZachCap, the call goes on
    phone.wait(CALL_AFTER)
    assert phone.meetings == 1


def test_keep_recording_holds_until_the_call_app_is_back():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]
    phone.wait(CALL_AFTER + 2)
    phone.apps = []
    phone.call_ended(LET_GO)
    phone.watch.check()
    assert phone.watch.keep()
    phone.call_ended(LET_GO + 60)
    phone.wait(60)
    assert phone.recording is not None and phone.watch.status()["stopping"] is None

    phone.recording["call"] = {"apps": ["Microsoft Teams"], "ended_for": None}  # back on
    phone.watch.check()
    phone.call_ended(LET_GO)  # and this time it ends
    phone.wait(STOP_IN + 2)
    assert phone.recording is None


def test_a_recording_you_started_still_stops_when_the_call_ends():
    phone = Phone()
    phone.settings["auto"] = False
    phone.start("you")
    phone.call_ended(LET_GO)
    phone.wait(STOP_IN + 2)
    assert phone.done[-1] == ("stop", 1)
    assert phone.watch.events[-1]["app"] == "Microsoft Teams"

    off = Phone()
    off.settings["stop"] = False  # Stop when the call ends: off
    off.start("you")
    off.call_ended(LET_GO)
    off.wait(60)
    assert off.recording is not None


def test_nobody_heard_for_ten_minutes_with_no_call_app_stops_a_minute_later():
    phone = Phone()
    phone.start("you")  # an in-room meeting
    phone.recording["quiet_for"] = QUIET_FOR
    phone.watch.check()
    assert phone.watch.status()["stopping"]["reason"] == "quiet"
    phone.recording["quiet_for"] = 30.0  # someone spoke
    phone.watch.check()
    assert phone.watch.status()["stopping"] is None
    phone.recording["quiet_for"] = QUIET_FOR
    phone.wait(QUIET_STOP_IN + 2)
    assert phone.recording is None


def test_a_call_where_nobody_was_heard_is_deleted():
    phone = Phone()
    phone.apps = ["Microsoft Teams"]
    phone.wait(CALL_AFTER + 2)  # the preview left open, nobody joined
    phone.recording["heard"] = False
    phone.apps = []
    phone.call_ended(LET_GO)
    phone.wait(STOP_IN + 2)
    assert phone.done[-1] == ("discard", 1) and phone.kinds()[-1] == "discarded"


def test_a_recording_that_wont_start_isnt_retried_all_call_long():
    phone = Phone()
    phone.start = None  # replaced below

    def broken(app):
        raise RuntimeError("No microphone")

    phone.watch._start = broken
    phone.apps = ["Microsoft Teams"]
    phone.wait(60)
    assert phone.kinds() == ["failed"]
    assert phone.watch.events[-1]["detail"] == "No microphone"


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

    watch = client.app.state.call_watch
    using = {"apps": ["Microsoft Teams"]}
    monkeypatch.setattr(watch, "_apps", lambda: list(using["apps"]))
    monkeypatch.setattr("zachcap.calls.apps_using_mic", lambda: list(using["apps"]))
    assert client.get("/api/call", headers=AUTH).json() == {
        "apps": ["Microsoft Teams"],
        "auto": ["Microsoft Teams"],
    }
    watch.check()  # Teams took the mic: recording
    status = client.get("/api/record", headers=AUTH).json()
    assert status["recording"] and status["auto"]["app"] == "Microsoft Teams"
    assert status["event"]["kind"] == "started" and status["title"].startswith(
        "Microsoft Teams call"
    )
    folder = Path(next((tmp_path / "meetings").iterdir()))

    declined = client.post("/api/record/decline", headers=AUTH).json()
    assert declined["kind"] == "declined"
    assert not client.get("/api/record", headers=AUTH).json()["recording"]
    assert client.get("/api/meetings", headers=AUTH).json() == [] and not folder.exists()
    assert client.post("/api/record/decline", headers=AUTH).status_code == 409
    assert client.post("/api/record/keep", headers=AUTH).json() == {"kept": False}
