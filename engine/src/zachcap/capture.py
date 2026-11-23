"""Record the mic and the speakers (loopback) into a meeting folder.

Each track is written as mono FLAC chunks (5 minutes by default), so a crash or a
forced reboot loses at most the chunk in progress (the engine reads even that one
back after a crash). A silent output stream keeps the Windows audio engine
rendering, so loopback keeps delivering audio through silences on machines where it
otherwise wouldn't. The time each track's first buffer arrived is saved to
recording.json as soon as it is known, so the pipeline can line the two tracks up
even if the recording never stopped cleanly.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

FRAMES_PER_BUFFER = 1024


class TrackRecorder:
    """One input stream feeding a writer thread that rotates FLAC chunks."""

    def __init__(
        self,
        name: str,
        device: dict,
        folder: Path,
        chunk_seconds: int,
        on_first_buffer: Callable[[], object] | None = None,
    ) -> None:
        self.name = name
        self.device = device
        self.folder = folder
        self.channels = int(device["maxInputChannels"])
        self.rate = int(device["defaultSampleRate"])
        self.chunk_frames = chunk_seconds * self.rate
        self.first_buffer_at: float | None = None
        self.frames = 0
        self.level_dbfs = -120.0
        self.errors: list[str] = []
        self.failed = False
        self.paused = False
        self.chunks: list[str] = []
        self._on_first_buffer = on_first_buffer
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write, name=f"{name}-writer", daemon=True)
        self._stream = None

    def _callback(self, in_data, frame_count, time_info, status):
        if self.first_buffer_at is None:
            self.first_buffer_at = time.perf_counter()
        # A private pause: silence instead of sound, decided as the audio arrives (audio
        # still queued when Resume is pressed was said during the pause). Both tracks keep
        # their length, so they stay in sync.
        self._queue.put(bytes(len(in_data)) if self.paused else in_data)
        return (None, pyaudio.paContinue)

    def _write(self) -> None:
        current: sf.SoundFile | None = None
        written = 0
        first = True
        while (data := self._queue.get()) is not None:
            samples = np.frombuffer(data, dtype=np.int16).reshape(-1, self.channels)
            mono = samples.mean(axis=1).astype(np.int16)
            self.level_dbfs = float(20 * np.log10(np.sqrt(np.mean((mono / 32768.0) ** 2)) + 1e-9))
            if first:
                first = False
                if self._on_first_buffer is not None:
                    try:
                        self._on_first_buffer()
                    except Exception as exc:
                        self.errors.append(f"Couldn't save the recording details: {exc}")
            if self.failed:
                continue  # keep draining, so a dead disk doesn't also fill memory
            try:
                if current is None or written >= self.chunk_frames:
                    if current is not None:
                        current.close()
                        current = None
                    path = self.folder / f"{self.name}-{len(self.chunks):03d}.flac"
                    current = sf.SoundFile(
                        path, mode="w", samplerate=self.rate, channels=1, subtype="PCM_16"
                    )
                    self.chunks.append(path.name)
                    written = 0
                current.write(mono)
                written += len(mono)
                self.frames += len(mono)
            except Exception as exc:  # disk full, file locked: report it, stop writing
                self.errors.append(f"Couldn't save the {self.name} audio: {exc}")
                self.failed = True
        if current is not None:
            try:
                current.close()
            except Exception as exc:
                self.errors.append(f"Couldn't finish the {self.name} audio: {exc}")

    def start(self, pa: pyaudio.PyAudio) -> None:
        # The stream starts delivering buffers as soon as it opens; they wait in the
        # queue until the writer runs. A failed open leaves no thread behind.
        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=self._callback,
        )
        self._writer.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError as exc:  # device vanished mid-meeting
                self.errors.append(str(exc))
            self._stream = None
        if self._writer.is_alive():
            self._queue.put(None)
            self._writer.join()


def _wasapi_devices(pa: pyaudio.PyAudio, wasapi: dict, kind: str) -> list[dict]:
    key = "maxInputChannels" if kind == "input" else "maxOutputChannels"
    devices = []
    for index in range(pa.get_device_count()):
        device = pa.get_device_info_by_index(index)
        if device["hostApi"] == wasapi["index"] and device[key] > 0:
            if not device.get("isLoopbackDevice"):
                devices.append(device)
    return devices


def _find_device(pa: pyaudio.PyAudio, wasapi: dict, name: str | None, kind: str) -> dict | None:
    """A device by its saved name; None (use the Windows default) if unset or unplugged."""
    if not name:
        return None
    return next((d for d in _wasapi_devices(pa, wasapi, kind) if d["name"] == name), None)


def list_devices() -> dict:
    """Microphones and outputs Windows offers, with the current defaults."""
    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_in = pa.get_device_info_by_index(wasapi["defaultInputDevice"])["name"]
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
        return {
            "inputs": [
                {"name": d["name"], "default": d["name"] == default_in}
                for d in _wasapi_devices(pa, wasapi, "input")
            ],
            "outputs": [
                {"name": d["name"], "default": d["name"] == default_out}
                for d in _wasapi_devices(pa, wasapi, "output")
            ],
        }
    finally:
        pa.terminate()


class Recorder:
    """Records a meeting into ``folder``: mic-NNN.flac, system-NNN.flac and recording.json."""

    def __init__(
        self,
        folder: Path,
        chunk_seconds: int = 300,
        mic_name: str | None = None,
        output_name: str | None = None,
    ) -> None:
        self.folder = folder
        self.chunk_seconds = chunk_seconds
        self.mic_name = mic_name
        self.output_name = output_name
        self.started_at: datetime | None = None
        self._t0 = 0.0
        self._pa: pyaudio.PyAudio | None = None
        self._keepalive = None
        self._info_lock = threading.Lock()
        self.tracks: dict[str, TrackRecorder] = {}

    def start(self) -> None:
        """Open both tracks. If anything fails, everything opened so far is closed again."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self._pa = pyaudio.PyAudio()
        try:
            self._open()
        except BaseException:
            self._close()
            raise
        self._save_info()

    def _open(self) -> None:
        assert self._pa is not None
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        mic = _find_device(self._pa, wasapi, self.mic_name, "input") or (
            self._pa.get_device_info_by_index(wasapi["defaultInputDevice"])
        )
        speakers = _find_device(self._pa, wasapi, self.output_name, "output") or (
            self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        )
        loopback = next(
            (
                d
                for d in self._pa.get_loopback_device_info_generator()
                if speakers["name"] in d["name"]
            ),
            None,
        )
        if loopback is None:
            raise RuntimeError(f"No loopback device for {speakers['name']!r}")

        silence = bytes(FRAMES_PER_BUFFER * int(speakers["maxOutputChannels"]) * 2)
        self._keepalive = self._pa.open(
            format=pyaudio.paInt16,
            channels=int(speakers["maxOutputChannels"]),
            rate=int(speakers["defaultSampleRate"]),
            output=True,
            output_device_index=speakers["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=lambda *_: (silence, pyaudio.paContinue),
        )
        self.tracks = {
            "mic": TrackRecorder("mic", mic, self.folder, self.chunk_seconds, self._save_info),
            "system": TrackRecorder(
                "system", loopback, self.folder, self.chunk_seconds, self._save_info
            ),
        }
        self.started_at = datetime.now().astimezone()
        self._t0 = time.perf_counter()
        for track in self.tracks.values():
            track.start(self._pa)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._t0 if self._t0 else 0.0

    def levels(self) -> dict[str, float]:
        return {name: track.level_dbfs for name, track in self.tracks.items()}

    def errors(self) -> list[str]:
        return [error for track in self.tracks.values() for error in track.errors]

    @property
    def paused(self) -> bool:
        return any(track.paused for track in self.tracks.values())

    def pause(self, paused: bool = True) -> None:
        """Pause (or resume) privately: both tracks record silence until resumed."""
        for track in self.tracks.values():
            track.paused = paused

    def _close(self) -> list[str]:
        """Stop every stream and writer that was started. Never raises."""
        errors = []
        for track in self.tracks.values():
            track.stop()
        if self._keepalive is not None:
            try:
                self._keepalive.stop_stream()
                self._keepalive.close()
            except OSError as exc:  # the output device vanished
                errors.append(f"output: {exc}")
            self._keepalive = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except OSError as exc:
                errors.append(f"audio system: {exc}")
            self._pa = None
        return errors

    def _save_info(self, complete: bool = False, duration: float | None = None) -> dict:
        """Write recording.json: at start, once each track's offset is known, and at stop.
        "complete" stays false until stop, so a recording cut off by a crash is recognizable.
        """
        with self._info_lock:
            info = {
                "started_at": self.started_at.isoformat(timespec="seconds")
                if self.started_at
                else None,
                "duration": round(self.elapsed if duration is None else duration, 3),
                "complete": complete,
                "tracks": {
                    name: {
                        "device": track.device["name"],
                        "rate": track.rate,
                        "channels": track.channels,
                        "frames": track.frames,
                        "offset": round((track.first_buffer_at or self._t0) - self._t0, 4),
                        "chunks": track.chunks,
                        "errors": track.errors,
                    }
                    for name, track in self.tracks.items()
                },
            }
            path = self.folder / "recording.json"
            path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        return info

    def stop(self) -> dict:
        duration = self.elapsed
        errors = self._close()
        info = self._save_info(complete=True, duration=duration)
        if errors:
            info["errors"] = errors
        return info
