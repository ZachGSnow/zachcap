"""A sound check: a few seconds recorded exactly as a meeting would be (same devices, same
code), with a short sound played halfway through, to show that both your microphone and the
computer's audio are heard before a call depends on it. The sound plays where the computer's
audio is recorded from: a short quiet tone on the chosen output device, or the Windows
notification sound on the default one. Nothing is kept: the test recording is deleted as
soon as it has been measured.
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
TONE_SECONDS = 0.4
TONE_HZ = 660.0
TONE_LEVEL = 0.1  # -20 dBFS: clearly heard by the recording, quiet in the room


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


def tone(rate: int, channels: int) -> bytes:
    """TONE_SECONDS of a quiet sine, faded in and out (no click), as 16-bit frames."""
    t = np.arange(int(TONE_SECONDS * rate)) / rate
    wave = TONE_LEVEL * np.sin(2 * np.pi * TONE_HZ * t)
    fade = min(len(wave) // 2, int(0.02 * rate))
    ramp = np.linspace(0.0, 1.0, fade)
    wave[:fade] *= ramp
    wave[len(wave) - fade :] *= ramp[::-1]
    frames = np.repeat((wave * 32767).astype(np.int16)[:, None], channels, axis=1)
    return frames.tobytes()


def _play_tone_on(name: str) -> bool:
    """The tone through one output device (the one being recorded). False if it isn't
    plugged in or won't play."""
    from .capture import _find_device, pyaudio

    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        device = _find_device(pa, wasapi, name, "output")
        if device is None:
            return False
        rate, channels = int(device["defaultSampleRate"]), int(device["maxOutputChannels"])
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            output=True,
            output_device_index=device["index"],
        )
        try:
            stream.write(tone(rate, channels))
        finally:
            stream.stop_stream()
            stream.close()
        return True
    except OSError:
        return False
    finally:
        pa.terminate()


def _play_test_sound(output_name: str | None) -> bool:
    """Where the computer's audio is recorded from: a chosen device gets the tone; every
    output, or the Windows default, gets the notification sound on the default device (a
    chosen device that isn't plugged in is recorded as every output, which includes it)."""
    from .capture import WINDOWS_DEFAULT_OUTPUT

    if output_name and output_name != WINDOWS_DEFAULT_OUTPUT and _play_tone_on(output_name):
        return True
    return _play(sound_file())


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
        started = time.perf_counter()
        recorder.start()
        time.sleep(SOUND_AT)
        played = _play_test_sound(output_name)
        time.sleep(max(0.5, SECONDS - (time.perf_counter() - started)))
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
