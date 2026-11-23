"""Record the mic and the speakers (loopback) into a meeting folder.

Each track is written as mono FLAC chunks (5 minutes by default), so a crash or a
forced reboot loses at most the chunk in progress. A silent output stream keeps
the Windows audio engine rendering, so loopback keeps delivering audio through
silences on machines where it otherwise wouldn't. The time each track's first
buffer arrived is saved, so the pipeline can line the two tracks up.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soundfile as sf

FRAMES_PER_BUFFER = 1024


class TrackRecorder:
    """One input stream feeding a writer thread that rotates FLAC chunks."""

    def __init__(self, name: str, device: dict, folder: Path, chunk_seconds: int) -> None:
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
        self.chunks: list[str] = []
        self._queue: queue.Queue[bytes | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write, name=f"{name}-writer", daemon=True)
        self._stream = None

    def _callback(self, in_data, frame_count, time_info, status):
        if self.first_buffer_at is None:
            self.first_buffer_at = time.perf_counter()
        self._queue.put(in_data)
        return (None, pyaudio.paContinue)

    def _write(self) -> None:
        current: sf.SoundFile | None = None
        written = 0
        while (data := self._queue.get()) is not None:
            samples = np.frombuffer(data, dtype=np.int16).reshape(-1, self.channels)
            mono = samples.mean(axis=1).astype(np.int16)
            self.level_dbfs = float(20 * np.log10(np.sqrt(np.mean((mono / 32768.0) ** 2)) + 1e-9))
            if current is None or written >= self.chunk_frames:
                if current is not None:
                    current.close()
                path = self.folder / f"{self.name}-{len(self.chunks):03d}.flac"
                current = sf.SoundFile(
                    path, mode="w", samplerate=self.rate, channels=1, subtype="PCM_16"
                )
                self.chunks.append(path.name)
                written = 0
            current.write(mono)
            written += len(mono)
            self.frames += len(mono)
        if current is not None:
            current.close()

    def start(self, pa: pyaudio.PyAudio) -> None:
        self._writer.start()
        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=self.device["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
            stream_callback=self._callback,
        )

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError as exc:  # device vanished mid-meeting
                self.errors.append(str(exc))
            self._stream = None
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
        self.tracks: dict[str, TrackRecorder] = {}

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._pa = pyaudio.PyAudio()
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
            "mic": TrackRecorder("mic", mic, self.folder, self.chunk_seconds),
            "system": TrackRecorder("system", loopback, self.folder, self.chunk_seconds),
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

    def stop(self) -> dict:
        duration = self.elapsed
        for track in self.tracks.values():
            track.stop()
        if self._keepalive is not None:
            self._keepalive.stop_stream()
            self._keepalive.close()
        if self._pa is not None:
            self._pa.terminate()
        info = {
            "started_at": self.started_at.isoformat(timespec="seconds")
            if self.started_at
            else None,
            "duration": round(duration, 3),
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
        (self.folder / "recording.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        return info
