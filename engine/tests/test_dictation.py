"""Dictation: listen while the keys are held, transcribe, type where the cursor was."""

import ctypes
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import dictation
from zachcap.api import create_app

AUTH = {"Authorization": "Bearer test-token"}


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
        return self.said

    def warm_up(self, language) -> None:
        pass


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dictation, "_sound", lambda name: None)  # no listening sounds in tests


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
    with pytest.raises(RuntimeError):
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
    assert not dictator.listening and FakeMic.made[-1].stopped


def test_text_is_typed_only_into_the_window_that_was_in_front(monkeypatch: pytest.MonkeyPatch):
    sent: list = []
    user32 = SimpleNamespace(SendInput=lambda count, events, size: sent.append(count) or count)
    monkeypatch.setattr(dictation, "_windows", lambda: SimpleNamespace(user32=user32))
    monkeypatch.setattr(dictation, "modifiers_released", lambda timeout=2.0: True)
    monkeypatch.setattr(dictation, "runs_above_us", lambda window: window == 7)
    in_front = {"window": 5}
    monkeypatch.setattr(dictation, "foreground_window", lambda: in_front["window"])

    assert dictation.type_into(5, "ok ") == (6, None) and sent == [6]
    assert dictation.type_into(None, "ok ") == (0, "nowhere")
    in_front["window"] = 6
    assert dictation.type_into(5, "ok ") == (0, "switched")  # you moved on before it was ready
    in_front["window"] = 7
    assert dictation.type_into(7, "ok ") == (0, "admin")  # Windows would drop the keys silently
    monkeypatch.setattr(dictation, "modifiers_released", lambda timeout=2.0: False)
    assert dictation.type_into(7, "ok ") == (0, "keys")
    assert sent == [6]  # nothing was sent but the first

    monkeypatch.setattr(dictation, "modifiers_released", lambda timeout=2.0: True)
    user32.SendInput = lambda count, events, size: count - 2
    in_front["window"] = 5
    assert dictation.type_into(5, "ok ") == (4, "refused")


def test_text_is_copied_only_when_it_could_not_be_typed(monkeypatch: pytest.MonkeyPatch):
    typed, copied = [], []
    monkeypatch.setattr(dictation, "copy_to_clipboard", lambda text: copied.append(text) or True)
    monkeypatch.setattr(dictation, "foreground_window", lambda: 5)
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


def test_the_clipboard_has_an_owner_and_its_memory_is_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list = []
    area = ctypes.create_string_buffer(64)
    answers = {"CreateWindowExW": 9, "GlobalAlloc": 11, "GlobalLock": ctypes.addressof(area)}

    class Library:
        def __getattr__(self, name):
            return lambda *args: calls.append((name, args)) or answers.get(name, 1)

    windows = SimpleNamespace(user32=Library(), kernel32=Library())
    monkeypatch.setattr(dictation, "_windows", lambda: windows)

    assert dictation.copy_to_clipboard("hi")
    assert area.raw[:6] == "hi".encode("utf-16-le") + b"\0\0"
    assert ("OpenClipboard", (9,)) in calls  # owned by a window: with none, Windows may refuse
    assert [name for name, _ in calls[-2:]] == ["CloseClipboard", "DestroyWindow"]
    assert "GlobalFree" not in [name for name, _ in calls]  # the clipboard owns it now

    calls.clear()
    answers["SetClipboardData"] = 0  # refused: the memory is freed, not leaked
    assert not dictation.copy_to_clipboard("hi")
    assert ("GlobalFree", (11,)) in calls and calls[-1][0] == "DestroyWindow"


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


def test_dictation_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(dictation, "Microphone", FakeMic)
    monkeypatch.setattr(dictation, "Transcriber", lambda: FakeTranscriber("Ticket closed."))
    client = TestClient(create_app("test-token"))

    off = client.post("/api/dictate/start", headers=AUTH)
    assert off.status_code == 400 and "off" in off.json()["detail"]
    assert client.put("/api/settings", headers=AUTH, json={"dictation": True}).json()["dictation"]

    started = client.post("/api/dictate/start", headers=AUTH).json()
    assert started["listening"] and started["id"]
    stop = {"type": False, "id": started["id"]}
    done = client.post("/api/dictate/stop", headers=AUTH, json=stop).json()
    assert done["text"] == "Ticket closed." and done["typed"] == 0 and done["why"] is None
    assert client.post("/api/dictate/stop", headers=AUTH, json=stop).status_code == 409

    client.post("/api/dictate/start", headers=AUTH)
    assert client.post("/api/dictate/cancel", headers=AUTH).json() == {"listening": False}
    assert FakeMic.made[-1].stopped

    client.post("/api/dictate/start", headers=AUTH)
    client.put("/api/settings", headers=AUTH, json={"dictation": False})  # off mid-dictation
    assert FakeMic.made[-1].stopped


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
    stop = client.post("/api/dictate/stop", headers=AUTH, json={"type": False})
    assert stop.status_code == 409  # not listening
