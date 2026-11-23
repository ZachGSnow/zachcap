"""A sound check: a few seconds recorded exactly as a meeting would be (same devices, same
code), with a short Windows sound played halfway through, to show that both your microphone
and the computer's audio are heard before a call depends on it. Nothing is kept: the test
recording is deleted as soon as it has been measured.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import time
from pathlib import Path

import numpy as np

from . import audio

SECONDS = 4.0
SOUND_AT = 1.5  # seconds in: after both tracks have started
MIC_HEARD_DBFS = -50.0  # a voice; room hiss stays well below
CALL_HEARD_DBFS = -70.0  # the test sound, even at a low Windows volume
SETTING = "soundcheck_passed"


def sound_file() -> Path:
    return (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "Media"
        / "Windows Notify System Generic.wav"
    )


def _play(path: Path) -> bool:
    try:
        import winsound

        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        return True
    except (ImportError, RuntimeError):
        return False


def peak_dbfs(track: np.ndarray | None) -> float | None:
    if track is None or track.size == 0:
        return None
    peak = float(np.max(np.abs(track)))
    return round(20 * math.log10(peak), 1) if peak > 0 else -180.0


def run(mic_name: str | None = None, output_name: str | None = None, recorder_class=None) -> dict:
    """Records SECONDS from the microphone and the computer's audio, playing the test sound
    at SOUND_AT, and says what each track heard and from which device."""
    if recorder_class is None:
        from .capture import Recorder as recorder_class
    folder = Path(tempfile.mkdtemp(prefix="zachcap-soundcheck-"))
    try:
        recorder = recorder_class(folder / "check", mic_name=mic_name, output_name=output_name)
        recorder.start()
        time.sleep(SOUND_AT)
        played = _play(sound_file())
        time.sleep(SECONDS - SOUND_AT)
        info = recorder.stop()
        errors = recorder.errors()
        results = {}
        for name, threshold in (("mic", MIC_HEARD_DBFS), ("system", CALL_HEARD_DBFS)):
            peak = peak_dbfs(audio.load_track(folder / "check", name))
            results[name] = {
                "device": info["tracks"].get(name, {}).get("device", ""),
                "peak_dbfs": peak,
                "heard": peak is not None and peak > threshold,
            }
        return {**results, "played": played, "errors": errors}
    finally:
        shutil.rmtree(folder, ignore_errors=True)
