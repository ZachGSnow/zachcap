"""Dictation: listen while the keys are held, transcribe, type where the cursor was."""

import ctypes
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import capture, dictation, pipeline
from zachcap.api import create_app

AUTH = {"Authorization": "Bearer test-token"}
REAL_WINDOWS = dictation._windows
REAL_TYPE_INTO = dictation.type_into


class FakeMic:
    made: list = []

    def __init__(self, name=None) -> None:
        self.name, self.stopped = name, False
        FakeMic.made.append(self)

    def stop(self) -> np.ndarray:
        self.stopped = True
        return np.zeros(dictation.RATE * 2, np.float32)  # two seconds


class FakeTranscriber:
    def __init__(self, text: str) -> None:
        self.said = text
        self.asked: list = []

    def text(self, audio, language, hotwords):
        self.asked.append((audio.size, language, hotwords))
        if isinstance(self.said, Exception):
            raise self.said
        return self.said

    def warm_up(self, language) -> None:
        pass


class Refused:
    """A Windows library whose typing and clipboard calls fail the test: no test may type
    into the developer's window or empty their clipboard by accident."""

    def __init__(self, library, refused: set[str]) -> None:
        self._library, self._refused = library, refused

    def __getattr__(self, name):
        if name in self._refused:
            raise AssertionError(f"a test called {name} for real")
        return getattr(self._library, name)


@pytest.fixture(autouse=True)
def safe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dictation, "_sound", lambda name: None)  # no listening sounds
    real = REAL_WINDOWS()
    refused = {"SendInput", "OpenClipboard", "EmptyClipboard", "SetClipboardData"}
    windows = SimpleNamespace(
        user32=Refused(real.user32, refused), kernel32=real.kernel32, advapi32=real.advapi32
    )
    monkeypatch.setattr(dictation, "_windows", lambda: windows)
    # Whatever window is in front of the developer, a test says which one is.
    monkeypatch.setattr(dictation, "foreground_window", lambda: None)


def test_text_is_typed_as_characters_never_as_enter_or_tab():
    events = dictation.key_events("Hi\nok\t!")
    assert ctypes.sizeof(dictation.INPUT) == 40  # what SendInput expects on 64-bit Windows
    typed = [(e.union.ki.wVk, e.union.ki.wScan, e.union.ki.dwFlags) for e in events]
    unicode, up = dictation.KEYEVENTF_UNICODE, dictation.KEYEVENTF_KEYUP
    assert typed[:4] == [(0, ord("H"), unicode), (0, ord("H"), unicode | up),
                         (0, ord("i"), unicode), (0, ord("i"), unicode | up)]  # fmt: skip
    # Characters only, no key: Enter would send a chat message, Tab would move on.
    assert [scan for key, scan, flags in typed if not flags & up] == [ord(c) for c in "Hi ok !"]
    assert all(key == 0 for key, _, _ in typed)
    assert len(dictation.key_events("a" + chr(0x1F600))) == 6  # an emoji is two UTF-16 units
    assert dictation.printable(" Call\r\nback\tat  noon. ") == "Call back at noon."


def test_audio_is_resampled_to_16k_mono():
    stereo = np.zeros((48000, 2), np.int16)
    stereo[:, 0] = 1000
    audio = dictation.to_16k_mono(stereo.tobytes(), 2, 48000)
    assert audio.size == 16000 and abs(float(audio[8000]) - 500 / 32768) < 1e-3


def test_detecting_the_language_needs_the_multilingual_model():
    assert dictation.model_for("en") == dictation.ENGLISH_MODEL
    assert (
        dictation.model_for(None) == dictation.ANY_LANGUAGE_MODEL
    )  # the English one hears English
    assert dictation.model_for("es") == dictation.ANY_LANGUAGE_MODEL


def test_what_was_heard_survives_a_mic_unplugged_mid_hold():
    class Gone:
        def stop_stream(self):
            raise OSError("Device unavailable")

    mic = object.__new__(dictation.Microphone)
    mic._stream, mic._pa = Gone(), SimpleNamespace(terminate=lambda: None)
    mic._chunks, mic.channels, mic.rate = [np.zeros(1600, np.int16).tobytes()], 1, 16000
    assert mic.stop().size == 1600


def test_each_hold_is_its_own_dictation_spelled_your_way():
    transcriber = FakeTranscriber("restart the connect wise agent")
    dictator = dictation.Dictation(transcriber, microphone=FakeMic)
    first = dictator.start("Headset mic")
    second = dictator.start()  # a quick second hold, before the first has finished
    assert first != second and [mic.name for mic in FakeMic.made[-2:]] == ["Headset mic", None]

    result = dictator.finish(
        first, "en", "ConnectWise", fix=lambda t: t.replace("connect wise", "ConnectWise"),
        type_it=False,
    )  # fmt: skip

    assert result["text"] == "restart the ConnectWise agent" and result["seconds"] == 2.0
    assert result["typed"] == 0 and result["why"] is None and not result["copied"]
    assert transcriber.asked == [(32000, "en", "ConnectWise")]
    with pytest.raises(dictation.NotListening):
        dictator.finish(first, "en", None)  # already finished
    assert dictator.listening
    dictator.finish(None, "en", None, type_it=False)  # None: the newest one, the second
    assert not dictator.listening and FakeMic.made[-1].stopped


def test_the_mic_is_never_left_open(monkeypatch: pytest.MonkeyPatch):
    dictator = dictation.Dictation(FakeTranscriber("unused"), microphone=FakeMic)
    for _ in range(3):
        dictator.start()
    first, second, third = FakeMic.made[-3:]
    assert first.stopped and not second.stopped  # at most two at once: the oldest was closed
    dictator.cancel()
    assert second.stopped and third.stopped and not dictator.listening

    monkeypatch.setattr(dictation, "MAX_SECONDS", 0)
    monkeypatch.setattr(dictation, "GRACE_SECONDS", 0.05)
    dictator.start()  # and the keys' release never arrives
    deadline = time.monotonic() + 5
    while dictator.listening and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not dictator.listening and FakeMic.made[-1].stopped and not dictator.holding


def test_a_window_run_as_administrator_is_refused_at_the_start(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dictation, "foreground_window", lambda: 7)
    monkeypatch.setattr(dictation, "runs_above_us", lambda window: True)
    dictator = dictation.Dictation(FakeTranscriber("unused"), microphone=FakeMic)
    made = len(FakeMic.made)
    with pytest.raises(dictation.NotHere, match="administrator"):
        dictator.start()
    assert len(FakeMic.made) == made and not dictator.holding  # the mic was never opened


def test_back_to_back_dictations_are_typed_in_order(monkeypatch: pytest.MonkeyPatch):
    typed: list = []
    monkeypatch.setattr(
        dictation, "type_into", lambda window, text: typed.append(text) or (1, None)
    )
    transcriber = FakeTranscriber("first note")
    dictator = dictation.Dictation(transcriber, microphone=FakeMic)
    first = dictator.start()
    second = dictator.start()  # held again before the first is typed

    finishing = threading.Thread(target=dictator.finish, args=(first, "en", None))
    finishing.start()
    time.sleep(0.3)
    assert typed == []  # the second hold's keys are down: the first waits for them
    transcriber.said = "second note"
    dictator.finish(second, "en", None)
    finishing.join(5)
    assert typed == ["first note ", "second note "]


def test_text_is_typed_only_into_the_window_that_was_in_front(monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    user32 = SimpleNamespace(SendInput=lambda count, events, size: sent.append(count) or count)
    monkeypatch.setattr(dictation, "_windows", lambda: SimpleNamespace(user32=user32))
    monkeypatch.setattr(dictation, "modifiers_released", lambda timeout=2.0: True)
    monkeypatch.setattr(dictation, "runs_above_us", lambda window: window == 7)
    in_front = {"window": 5}
    monkeypatch.setattr(dictation, "foreground_window", lambda: in_front["window"])
    type_into = REAL_TYPE_INTO

    assert type_into(5, "ok ") == (6, None) and sent == [6]
    assert type_into(None, "ok ") == (0, "nowhere")
    in_front["window"] = 6
    assert type_into(5, "ok ") == (0, "switched")  # you moved on before it was ready
    in_front["window"] = 7
    assert type_into(7, "ok ") == (0, "admin")  # Windows would drop the keys silently
    monkeypatch.setattr(dictation, "modifiers_released", lambda timeout=2.0: False)
    assert type_into(7, "ok ") == (0, "keys")
    assert sent == [6]  # nothing was sent but the first

    monkeypatch.setattr(dictation, "modifiers_released", lambda timeout=2.0: True)
    user32.SendInput = lambda count, events, size: count - 2
    in_front["window"] = 5
    assert type_into(5, "ok ") == (4, "refused")


def test_text_is_copied_only_when_it_could_not_be_typed(monkeypatch: pytest.MonkeyPatch):
    typed, copied = [], []
    monkeypatch.setattr(dictation, "copy_to_clipboard", lambda text: copied.append(text) or True)
    monkeypatch.setattr(dictation, "foreground_window", lambda: 5)
    monkeypatch.setattr(dictation, "runs_above_us", lambda window: False)
    monkeypatch.setattr(
        dictation, "type_into", lambda window, text: typed.append((window, text)) or (38, None)
    )
    dictator = dictation.Dictation(FakeTranscriber("Call back\nat noon."), microphone=FakeMic)

    dictator.start()
    result = dictator.finish(None, "en", None)
    assert typed == [(5, "Call back at noon. ")] and not copied  # one line, then a space
    assert result["typed"] == 38 and not result["copied"] and result["why"] is None

    monkeypatch.setattr(dictation, "type_into", lambda window, text: (0, "admin"))
    dictator.start()
    result = dictator.finish(None, "en", None)
    assert copied == ["Call back at noon."] and result["copied"] and result["why"] == "admin"


class FakeLibrary:
    def __init__(self, calls: list, answers: dict) -> None:
        self._calls, self._answers = calls, answers

    def __getattr__(self, name):
        return lambda *args: self._calls.append((name, args)) or self._answers.get(name, 1)


def test_the_clipboard_is_private_and_left_alone_when_copying_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list = []
    area = ctypes.create_string_buffer(64)
    answers = {"CreateWindowExW": 9, "GlobalAlloc": 11, "GlobalLock": ctypes.addressof(area)}
    windows = SimpleNamespace(
        user32=FakeLibrary(calls, answers), kernel32=FakeLibrary(calls, answers)
    )
    monkeypatch.setattr(dictation, "_windows", lambda: windows)

    assert dictation.copy_to_clipboard("hi")
    names = [name for name, _ in calls]
    assert names.index("GlobalLock") < names.index("EmptyClipboard")  # filled first
    assert ("OpenClipboard", (9,)) in calls  # owned by a window: with none, Windows may refuse
    assert [name for name, args in calls if name == "RegisterClipboardFormatW"] == [
        "RegisterClipboardFormatW"
    ] * 3
    assert ("RegisterClipboardFormatW", ("CanIncludeInClipboardHistory",)) in calls
    assert names[-2:] == ["CloseClipboard", "DestroyWindow"] and "GlobalFree" not in names

    calls.clear()
    answers["SetClipboardData"] = 0  # refused: the memory is freed, not leaked
    assert not dictation.copy_to_clipboard("hi")
    assert ("GlobalFree", (11,)) in calls and calls[-1][0] == "DestroyWindow"

    calls.clear()
    answers["OpenClipboard"] = 0  # another program holds it: what's there stays
    monkeypatch.setattr(dictation.time, "sleep", lambda seconds: None)
    assert not dictation.copy_to_clipboard("hi")
    names = [name for name, _ in calls]
    assert "EmptyClipboard" not in names and "GlobalFree" in names


def test_a_window_at_our_own_level_takes_typing():
    """The real check, on a hidden window of this process: same level, so typing is let
    through (a window of a program run as administrator is a level above)."""
    windows = dictation._windows()
    window = windows.user32.CreateWindowExW(
        0, "STATIC", None, 0, 0, 0, 0, 0, dictation.HWND_MESSAGE, None, None, None
    )
    try:
        assert window
        ours = dictation._integrity(windows.kernel32.GetCurrentProcess())
        assert ours in (0x1000, 0x2000, 0x2100, 0x3000, 0x4000)  # low to system
        assert dictation.runs_above_us(window) is False
    finally:
        windows.user32.DestroyWindow(window)


class FakeRecorder:
    made: list = []

    def __init__(self, folder: Path, **devices) -> None:
        self.folder, self.elapsed, self.tracks, self.paused = folder, 42.0, {}, False
        self.started_at, self.mic_muted = None, []
        FakeRecorder.made.append(self)

    def start(self) -> None:
        self.folder.mkdir(parents=True)

    def mute_mic(self, muted: bool = True) -> None:
        self.mic_muted.append(muted)

    def levels(self) -> dict:
        return {}

    def errors(self) -> list:
        return []

    def stop(self) -> dict:
        return {"duration": 42.0}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(dictation, "Microphone", FakeMic)
    monkeypatch.setattr(dictation, "Transcriber", lambda: FakeTranscriber("Ticket closed."))
    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    return TestClient(create_app("test-token"))


def test_dictation_through_the_api(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    off = client.post("/api/dictate/start", headers=AUTH)
    assert off.status_code == 400 and "off" in off.json()["detail"]
    assert client.put("/api/settings", headers=AUTH, json={"dictation": True}).json()["dictation"]

    started = client.post("/api/dictate/start", headers=AUTH).json()
    assert started["listening"] and started["id"]
    stop = {"id": started["id"]}  # typing is asked for, never assumed
    done = client.post("/api/dictate/stop", headers=AUTH, json=stop).json()
    assert done["text"] == "Ticket closed." and done["typed"] == 0 and done["why"] is None
    assert client.post("/api/dictate/stop", headers=AUTH, json=stop).status_code == 409

    client.post("/api/dictate/start", headers=AUTH)
    assert client.post("/api/dictate/cancel", headers=AUTH).json() == {"listening": False}
    assert FakeMic.made[-1].stopped

    client.post("/api/dictate/start", headers=AUTH)
    client.put("/api/settings", headers=AUTH, json={"dictation": False})  # off mid-dictation
    assert FakeMic.made[-1].stopped


def test_a_dictation_during_a_recording_is_left_out_of_it(client: TestClient):
    client.put("/api/settings", headers=AUTH, json={"dictation": True})
    client.post("/api/record/start", headers=AUTH, json={})
    recorder = FakeRecorder.made[-1]

    started = client.post("/api/dictate/start", headers=AUTH).json()
    assert recorder.mic_muted == [True]  # the mic track records silence while you dictate
    client.post("/api/dictate/stop", headers=AUTH, json={"id": started["id"]})
    assert recorder.mic_muted == [True, False]
    client.post("/api/record/stop", headers=AUTH)


def test_a_recording_started_mid_dictation_leaves_it_out_from_its_first_second(
    client: TestClient,
):
    client.put("/api/settings", headers=AUTH, json={"dictation": True})
    client.post("/api/dictate/start", headers=AUTH)
    assert client.post("/api/record/start", headers=AUTH, json={}).status_code == 200
    recorder = FakeRecorder.made[-1]
    assert recorder.mic_muted == [True]
    client.post("/api/dictate/cancel", headers=AUTH)
    assert recorder.mic_muted == [True, False]
    client.post("/api/record/stop", headers=AUTH)


def test_failures_say_what_went_wrong(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(dictation, "Microphone", FakeMic)
    broken = FakeTranscriber(RuntimeError("The model couldn't load"))
    monkeypatch.setattr(dictation, "Transcriber", lambda: broken)
    client = TestClient(create_app("test-token"))
    client.put("/api/settings", headers=AUTH, json={"dictation": True})
    started = client.post("/api/dictate/start", headers=AUTH).json()

    failed = client.post("/api/dictate/stop", headers=AUTH, json={"id": started["id"]})
    assert failed.status_code == 500  # not mistaken for "not listening"
    assert failed.json()["detail"] == "Dictation failed: The model couldn't load"


def test_a_mic_that_wont_open_is_reported_and_nothing_is_left_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def no_mic(name=None):
        raise OSError("Device unavailable")

    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(dictation, "Microphone", no_mic)
    monkeypatch.setattr(dictation, "Transcriber", lambda: FakeTranscriber("unused"))
    client = TestClient(create_app("test-token"))
    client.put("/api/settings", headers=AUTH, json={"dictation": True})

    failed = client.post("/api/dictate/start", headers=AUTH)
    assert failed.status_code == 400 and "Couldn't open the mic" in failed.json()["detail"]
    assert client.post("/api/dictate/stop", headers=AUTH, json={}).status_code == 409
