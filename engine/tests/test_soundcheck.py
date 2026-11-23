"""The sound check: a short recording through the real recorder code, measured, deleted."""

from pathlib import Path

import numpy as np
import soundfile as sf

from zachcap import soundcheck


class LoudRecorder:
    """Writes a mic track with a voice and a call track with the test sound."""

    made: list[Path] = []

    def __init__(self, folder: Path, mic_name=None, output_name=None) -> None:
        self.folder = folder
        LoudRecorder.made.append(folder)

    def start(self) -> None:
        self.folder.mkdir(parents=True)

    def stop(self) -> dict:
        t = np.arange(16000) / 16000
        sf.write(
            self.folder / "mic.flac", (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), 16000
        )
        sf.write(self.folder / "system.flac", np.zeros(16000, np.float32), 16000)
        return {"tracks": {"mic": {"device": "Mic array"}, "system": {"device": "Speakers"}}}

    def errors(self) -> list:
        return []


def test_the_check_says_what_each_side_heard_and_leaves_nothing(monkeypatch):
    monkeypatch.setattr(soundcheck, "SECONDS", 0.2)
    monkeypatch.setattr(soundcheck, "SOUND_AT", 0.1)
    monkeypatch.setattr(soundcheck, "_play", lambda path: True)

    result = soundcheck.run(recorder_class=LoudRecorder)

    assert result["mic"] == {"device": "Mic array", "peak_dbfs": -14.0, "heard": True}
    assert result["system"] == {"device": "Speakers", "peak_dbfs": -180.0, "heard": False}
    assert result["played"] and not LoudRecorder.made[-1].parent.exists()  # deleted


def test_peaks():
    assert soundcheck.peak_dbfs(None) is None
    assert soundcheck.peak_dbfs(np.zeros(10, np.float32)) == -180.0
    assert soundcheck.peak_dbfs(np.array([0.5], np.float32)) == -6.0
