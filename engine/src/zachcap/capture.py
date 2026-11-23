"""Record the mic and the computer's audio into a meeting folder.

Each track is written as mono FLAC chunks (5 minutes by default), so a crash or a
forced reboot loses at most the chunk in progress (the engine reads even that one
back after a crash). The time each track's first buffer arrived is saved to
recording.json as soon as it is known, so the pipeline can line the two tracks up
even if the recording never stopped cleanly.

The computer's audio comes from every speaker and headset by default (AllOutputsTrack),
so a call playing on a headset that isn't the Windows default, or a headset plugged in
mid-call, is still recorded. Picking one output device records only that one (loopback
through PortAudio, with a silent output stream that keeps the Windows audio engine
rendering, so loopback keeps delivering audio through silences).
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

from .mixer import Mixer

FRAMES_PER_BUFFER = 1024
# The output setting's value for the Windows default output only (the old behavior); empty
# means every speaker and headset, any other value is one device's name.
WINDOWS_DEFAULT_OUTPUT = "default"
EVERY_OUTPUT_RATE = 48_000
LOOK_FOR_DEVICES = 2.0  # seconds between looks for outputs plugged in or unplugged
RETRY_FAILED_DEVICE = 30.0
WRITE_EVERY = 0.1


class ChunkWriter:
    """Writes one mono track as numbered FLAC chunks (name-000.flac, ...), starting a new
    one every ``chunk_seconds``. After a disk error it reports once and stops writing, so
    callers can keep draining their buffers."""

    def __init__(
        self,
        folder: Path,
        name: str,
        rate: int,
        chunk_seconds: int,
        errors: list[str],
        subtype: str = "PCM_16",
    ) -> None:
        self.folder = folder
        self.name = name
        self.rate = rate
        self.subtype = subtype
        self.chunk_frames = chunk_seconds * rate
        self.errors = errors
        self.frames = 0
        self.failed = False
        self.chunks: list[str] = []
        self._current: sf.SoundFile | None = None
        self._written = 0

    def write(self, mono: np.ndarray) -> None:
        if self.failed:
            return
        try:
            if self._current is None or self._written >= self.chunk_frames:
                if self._current is not None:
                    self._current.close()
                    self._current = None
                path = self.folder / f"{self.name}-{len(self.chunks):03d}.flac"
                self._current = sf.SoundFile(
                    path, mode="w", samplerate=self.rate, channels=1, subtype=self.subtype
                )
                self.chunks.append(path.name)
                self._written = 0
            self._current.write(mono)
            self._written += len(mono)
            self.frames += len(mono)
        except Exception as exc:  # disk full, file locked: report it, stop writing
            self.errors.append(f"Couldn't save the {self.name} audio: {exc}")
            self.failed = True

    def close(self) -> None:
        if self._current is not None:
            try:
                self._current.close()
            except Exception as exc:
                self.errors.append(f"Couldn't finish the {self.name} audio: {exc}")
            self._current = None


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
        self.first_buffer_at: float | None = None
        self.level_dbfs = -120.0
        self.errors: list[str] = []
        self.paused = False
        self._chunks = ChunkWriter(folder, name, self.rate, chunk_seconds, self.errors)
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

    @property
    def frames(self) -> int:
        return self._chunks.frames

    @property
    def chunks(self) -> list[str]:
        return self._chunks.chunks

    @property
    def failed(self) -> bool:
        return self._chunks.failed

    def _write(self) -> None:
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
            # After a disk error this writes nothing, and the queue keeps draining, so a
            # dead disk doesn't also fill memory.
            self._chunks.write(mono)
        self._chunks.close()

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


def _loopback_api():
    """The Core Audio loopback module, imported when first needed (Windows only)."""
    from . import loopback

    return loopback


class AllOutputsTrack:
    """The computer-audio track from every speaker and headset at once.

    Teams can play a call on a headset that isn't the Windows default, and people plug in or
    switch headsets mid-call; recording one device loses the far side then. So every active
    output is captured, outputs that appear later join, unplugged ones drop out, and the
    mixer keeps the track at real-time length, in sync with the mic. Written as 24-bit FLAC:
    loopback comes after the Windows volume, so a call at low volume keeps its detail.
    """

    name = "system"
    channels = 1

    def __init__(
        self,
        folder: Path,
        chunk_seconds: int,
        t0: float,
        on_first_buffer: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.perf_counter,
        api=None,
    ) -> None:
        self.rate = EVERY_OUTPUT_RATE
        self.device = {"name": "Every speaker and headset"}
        self.errors: list[str] = []
        self.paused = False
        self.level_dbfs = -120.0
        self.first_buffer_at: float | None = None
        self._t0 = t0
        self._clock = clock
        self._api = api if api is not None else _loopback_api()
        self._on_first_buffer = on_first_buffer
        self._chunks = ChunkWriter(
            folder, self.name, self.rate, chunk_seconds, self.errors, subtype="PCM_24"
        )
        self._mixer = Mixer(self.rate, t0)
        self._lock = threading.Lock()
        self._captures: dict[str, object] = {}
        self._failed: dict[str, float] = {}  # device id -> when it last failed to open
        self._listing_failed = False
        self._started = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    @property
    def frames(self) -> int:
        return self._chunks.frames

    @property
    def chunks(self) -> list[str]:
        return self._chunks.chunks

    @property
    def failed(self) -> bool:
        return self._chunks.failed

    def start(self, pa=None) -> None:
        """Starts capturing the outputs there are now, then keeps looking for new ones."""
        self._started = True
        self.poll_devices()
        for target, name in ((self._watch, "outputs-watch"), (self._write, "system-writer")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _on_packet(self, device_id, mono, rate, stamp, discontinuity) -> None:
        if self.paused:  # a private pause, decided as the audio arrives (as on the mic)
            mono = np.zeros_like(mono)
        with self._lock:
            self._mixer.add(device_id, mono, rate, stamp, self._clock(), discontinuity)

    def poll_devices(self) -> None:
        """Captures outputs that appeared and forgets ones that went away."""
        try:
            devices = self._api.output_devices()
        except Exception as exc:
            if not self._listing_failed:
                self._listing_failed = True
                self.errors.append(f"Couldn't list the output devices: {exc}")
            return
        now = self._clock()
        present = {device["id"]: device["name"] for device in devices}
        for device_id, capture in list(self._captures.items()):
            if not capture.alive:  # unplugged, or it stopped on an error
                del self._captures[device_id]
                if device_id in present:  # still there but broken: try again later
                    self._failed[device_id] = now
        for device_id, name in present.items():
            if device_id in self._captures:
                continue
            if now - self._failed.get(device_id, float("-inf")) < RETRY_FAILED_DEVICE:
                continue
            capture = self._api.LoopbackCapture(device_id, name, self._on_packet)
            try:
                capture.start()
            except Exception as exc:
                if device_id not in self._failed:  # said once, not on every retry
                    self.errors.append(f"Couldn't record {name}: {exc}")
                self._failed[device_id] = now
                continue
            self._captures[device_id] = capture
        names = [capture.name for capture in self._captures.values()]
        self.device = {"name": ", ".join(names) if names else "No output device"}

    def _watch(self) -> None:
        while not self._stop.wait(LOOK_FOR_DEVICES):
            self.poll_devices()

    def _write(self) -> None:
        while not self._stop.wait(WRITE_EVERY):
            self.pump()

    def pump(self, until: int | None = None) -> None:
        """Writes the mixed audio that is ready (at the end: everything up to ``until``)."""
        with self._lock:
            if until is None:
                block = self._mixer.drain(self._clock())
            else:
                block = self._mixer.flush(until)
        if block.size == 0:
            return
        power = float(np.mean(block.astype(np.float64) ** 2))
        self.level_dbfs = float(20 * np.log10(np.sqrt(power) + 1e-9))
        if self.first_buffer_at is None:
            self.first_buffer_at = self._t0  # frame 0 is the start of the recording
            if self._on_first_buffer is not None:
                try:
                    self._on_first_buffer()
                except Exception as exc:
                    self.errors.append(f"Couldn't save the recording details: {exc}")
        self._chunks.write(block)

    def stop(self) -> None:
        if not self._started:
            return
        end = round((self._clock() - self._t0) * self.rate)
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        for capture in list(self._captures.values()):
            capture.stop()
        self._captures.clear()
        self.pump(until=end)
        self._chunks.close()


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
        loopback = None if self.every_output else self._open_one_output(wasapi)
        self.started_at = datetime.now().astimezone()
        self._t0 = time.perf_counter()
        if loopback is None:
            system = AllOutputsTrack(self.folder, self.chunk_seconds, self._t0, self._save_info)
        else:
            system = TrackRecorder(
                "system", loopback, self.folder, self.chunk_seconds, self._save_info
            )
        self.tracks = {
            "mic": TrackRecorder("mic", mic, self.folder, self.chunk_seconds, self._save_info),
            "system": system,
        }
        for track in self.tracks.values():
            track.start(self._pa)

    @property
    def every_output(self) -> bool:
        """The default: the computer's audio from every speaker and headset."""
        return not self.output_name

    def _open_one_output(self, wasapi: dict) -> dict:
        """The loopback device for the chosen output (or the Windows default), with a silent
        stream on that output, so loopback keeps delivering through silences."""
        assert self._pa is not None
        name = None if self.output_name == WINDOWS_DEFAULT_OUTPUT else self.output_name
        speakers = _find_device(self._pa, wasapi, name, "output") or (
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
        return loopback

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
