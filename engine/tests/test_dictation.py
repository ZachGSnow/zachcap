"""Dictation: listen while the keys are held, transcribe, type where the cursor is."""

import ctypes
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import dictation
from zachcap.api import create_app

AUTH = {"Authorization": "Bearer test-token"}


class FakeMic:
    opened: list = []

    def __init__(self, name=None) -> None:
        FakeMic.opened.append(name)

    def stop(self) -> np.ndarray:
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


def test_text_is_typed_as_characters_with_enter_for_line_breaks():
    events = dictation.key_events("Hi\nok")
    assert ctypes.sizeof(dictation.INPUT) == 40  # what SendInput expects on 64-bit Windows
    typed = [(e.union.ki.wVk, e.union.ki.wScan, e.union.ki.dwFlags) for e in events]
    unicode, up = dictation.KEYEVENTF_UNICODE, dictation.KEYEVENTF_KEYUP
    assert typed[:4] == [(0, ord("H"), unicode), (0, ord("H"), unicode | up),
                         (0, ord("i"), unicode), (0, ord("i"), unicode | up)]  # fmt: skip
    assert typed[4:6] == [(dictation.VK_RETURN, 0, 0), (dictation.VK_RETURN, 0, up)]
    assert len(dictation.key_events("a" + chr(0x1F600))) == 6  # an emoji is two UTF-16 units


def test_audio_is_resampled_to_16k_mono():
    stereo = np.zeros((48000, 2), np.int16)
    stereo[:, 0] = 1000
    audio = dictation.to_16k_mono(stereo.tobytes(), 2, 48000)
    assert audio.size == 16000 and abs(float(audio[8000]) - 500 / 32768) < 1e-3


def test_a_dictation_is_spelled_your_way_and_not_typed_when_asked_not_to():
    transcriber = FakeTranscriber("restart the connect wise agent")
    dictator = dictation.Dictation(transcriber, microphone=FakeMic)
    dictator.start("Headset mic")
    assert dictator.listening and FakeMic.opened[-1] == "Headset mic"
    with pytest.raises(RuntimeError):
        dictator.start()  # one at a time

    result = dictator.finish(
        "en", "ConnectWise", fix=lambda t: t.replace("connect wise", "ConnectWise"), type_it=False
    )

    assert result["text"] == "restart the ConnectWise agent" and result["seconds"] == 2.0
    assert result["typed"] == 0 and transcriber.asked == [(32000, "en", "ConnectWise")]
    assert not dictator.listening
    with pytest.raises(RuntimeError):
        dictator.finish("en", None)


def test_dictation_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(dictation, "Microphone", FakeMic)
    monkeypatch.setattr(dictation, "Transcriber", lambda: FakeTranscriber("Ticket closed."))
    client = TestClient(create_app("test-token"))

    off = client.post("/api/dictate/start", headers=AUTH)
    assert off.status_code == 400 and "off" in off.json()["detail"]
    assert client.put("/api/settings", headers=AUTH, json={"dictation": True}).json()["dictation"]

    assert client.post("/api/dictate/start", headers=AUTH).json() == {"listening": True}
    assert client.post("/api/dictate/start", headers=AUTH).status_code == 409
    done = client.post("/api/dictate/stop", headers=AUTH, json={"type": False}).json()
    assert done["text"] == "Ticket closed." and done["typed"] == 0
    assert client.post("/api/dictate/stop", headers=AUTH, json={"type": False}).status_code == 409
    assert client.post("/api/dictate/cancel", headers=AUTH).json() == {"listening": False}
