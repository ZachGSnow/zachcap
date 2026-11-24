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
import traceback
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
# Levels the watchdog reads: any signal at all (a muted or broken device delivers exact
# zeros), and speech-level sound on the mic and on the computer's audio (loopback is
# digital, so silence there is far below any voice).
SIGNAL_DBFS = -100.0
MIC_SPEECH_DBFS = -45.0
CALL_SPEECH_DBFS = -70.0  # a call played at a low Windows volume still counts
LOOK_FOR_DEVICES = 2.0  # seconds between looks for outputs plugged in or unplugged
RETRY_FAILED_DEVICE = 30.0
WRITE_EVERY = 0.1

# Hears each track's audio as it's written: (track name, mono block, rate, the block's first
# frame on the recording's timeline). The live transcript listens (live.py).
Listener = Callable[[str, np.ndarray, int, int], object]


def _note_level(track, level_dbfs: float) -> None:
    """Remembers when a track last had any signal, and last had speech-level sound."""
    now = time.perf_counter()
    if level_dbfs > SIGNAL_DBFS:
        track.signal_at = now
    if level_dbfs > track.speech_dbfs:
        track.heard_at = now


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
        listener: Listener | None = None,
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
        self._listener = listener
        self._heard = 0  # frames handed to the listener: the timeline, even after a disk error

    def write(self, mono: np.ndarray) -> None:
        if not self.failed:
            self._save(mono)
        if self._listener is not None:
            # After the disk, so whatever listens never holds the recording up; and one that
            # fails is let go, so it fails once, not ten times a second.
            try:
                self._listener(self.name, mono, self.rate, self._heard)
            except Exception:
                traceback.print_exc()
                self._listener = None
        self._heard += len(mono)

    def _save(self, mono: np.ndarray) -> None:
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
    """One input stream (PortAudio) feeding a writer thread that rotates FLAC chunks.

    Each buffer is placed by when it was captured, on the recording's timeline (frame 0 is
    its start, as on the computer-audio track), so a device that drops or delays audio
    (input overflows, standby) leaves a silent gap instead of shifting everything after it
    out of step with the other track. On its own (no ``t0``), its first buffer is frame 0.
    """

    def __init__(
        self,
        name: str,
        device: dict,
        folder: Path,
        chunk_seconds: int,
        on_first_buffer: Callable[[], object] | None = None,
        speech_dbfs: float = MIC_SPEECH_DBFS,
        t0: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
        listener: Listener | None = None,
    ) -> None:
        self.name = name
        self.device = device
        self.folder = folder
        self.speech_dbfs = speech_dbfs
        self.signal_at: float | None = None  # when it last delivered anything but zeros
        self.heard_at: float | None = None  # when it last had speech-level sound
        self.channels = int(device["maxInputChannels"])
        self.rate = int(device["defaultSampleRate"])
        self.first_buffer_at: float | None = None
        self.level_dbfs = -120.0
        self.errors: list[str] = []
        self.paused = False
        self.muted = False  # silence on this track only: a dictation while recording
        self._t0 = t0
        self._given_t0 = t0 is not None
        self._clock = clock
        self._mixer = Mixer(self.rate, t0, early_follows_on=True) if t0 is not None else None
        self._chunks = ChunkWriter(
            folder, name, self.rate, chunk_seconds, self.errors, listener=listener
        )
        self._on_first_buffer = on_first_buffer
        self._queue: queue.Queue[tuple[float, bytes] | None] = queue.Queue()
        self._writer = threading.Thread(target=self._write, name=f"{name}-writer", daemon=True)
        self._stream = None
        self._broken = False

    def _callback(self, in_data, frame_count, time_info, status):
        # When its first frame was captured: the buffer just filled up.
        captured = self._clock() - frame_count / self.rate
        # A private pause: silence instead of sound, decided as the audio arrives (audio
        # still queued when Resume is pressed was said during the pause). Both tracks keep
        # their length, so they stay in sync.
        silent = self.paused or self.muted
        self._queue.put((captured, bytes(len(in_data)) if silent else in_data))
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

    def _receive(self, captured: float, data: bytes) -> None:
        samples = np.frombuffer(data, dtype=np.int16).reshape(-1, self.channels)
        mono = samples.mean(axis=1).astype(np.float32) / 32768.0
        self.level_dbfs = float(
            20 * np.log10(np.sqrt(np.mean(mono.astype(np.float64) ** 2)) + 1e-9)
        )
        _note_level(self, self.level_dbfs)
        if self._mixer is None:  # on its own: the first buffer starts the timeline
            self._t0 = captured
            self._mixer = Mixer(self.rate, captured, early_follows_on=True)
        self._mixer.add(self.name, mono, self.rate, captured, self._clock())

    def _drain(self, until: int | None = None) -> None:
        """Writes what is ready now (at the end: everything up to ``until``), a second at a
        time. The target is fixed when it starts, so audio that keeps arriving can't keep
        it writing. After a disk error nothing is written, and buffers keep draining, so a
        dead disk doesn't also fill memory."""
        if self._mixer is None:
            return
        target = self._mixer.horizon(self._clock()) if until is None else until
        while True:
            block = self._mixer.flush(target, most=self.rate)
            if not block.size:
                return
            if self.first_buffer_at is None:
                self.first_buffer_at = self._t0  # frame 0 is the start of the timeline
                if self._on_first_buffer is not None:
                    try:
                        self._on_first_buffer()
                    except Exception as exc:
                        self.errors.append(f"Couldn't save the recording details: {exc}")
            self._chunks.write(block)

    def _write(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=WRITE_EVERY)
            except queue.Empty:
                item = ()  # nothing arrived: the silence up to now is still written
            if item is None:
                return
            try:
                if item:
                    self._receive(*item)
                self._drain()
            except Exception as exc:  # never let one bad buffer end the track
                if not self._broken:
                    self._broken = True
                    self.errors.append(f"Couldn't record the {self.name} audio: {exc}")

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

    def stop(self, at: float | None = None) -> None:
        """Stops the stream and writes the end. ``at``: when the recording stopped (both
        tracks end there), else now."""
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
        else:  # never started (buffers may still be queued)
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if item:
                    self._receive(*item)
        # The end: part of a recording, when it stopped (silence up to there if the device
        # went quiet); on its own, everything received.
        if self._mixer is not None:
            if self._given_t0:
                end = round(((self._clock() if at is None else at) - self._t0) * self.rate)
            else:
                end = self._mixer.end()
            try:
                self._drain(until=end)
            except Exception as exc:
                self.errors.append(f"Couldn't finish the {self.name} audio: {exc}")
        self._chunks.close()


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
    speech_dbfs = CALL_SPEECH_DBFS

    def __init__(
        self,
        folder: Path,
        chunk_seconds: int,
        t0: float,
        on_first_buffer: Callable[[], object] | None = None,
        clock: Callable[[], float] = time.perf_counter,
        api=None,
        listener: Listener | None = None,
    ) -> None:
        self.rate = EVERY_OUTPUT_RATE
        self.device = {"name": "Every speaker and headset"}
        self.errors: list[str] = []
        self.paused = False
        self.level_dbfs = -120.0
        self.signal_at: float | None = None
        self.heard_at: float | None = None
        self.first_buffer_at: float | None = None
        self._t0 = t0
        self._clock = clock
        self._api = api if api is not None else _loopback_api()
        self._on_first_buffer = on_first_buffer
        self._chunks = ChunkWriter(
            folder,
            self.name,
            self.rate,
            chunk_seconds,
            self.errors,
            subtype="PCM_24",
            listener=listener,
        )
        self._mixer = Mixer(self.rate, t0)
        self._lock = threading.Lock()  # the mixer: capture threads, the writer, stop
        self._writing = threading.Lock()  # the files: the writer and stop never write at once
        self._polling = threading.Lock()  # one look at the devices at a time
        self._captures: dict[str, object] = {}
        self._failed: dict[str, float] = {}  # device id -> when it last failed to open
        self._deaths: dict[str, list[float]] = {}  # device id -> when its capture ended
        self._listing_error: str | None = None
        self._broken = False
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

    def _backing_off(self, device_id: str, now: float) -> bool:
        """A device that refused to open, or whose capture keeps ending (three times in a
        minute), is left alone for a while; one that ended once is reopened at once (a
        Bluetooth headset switching to its call profile, the audio service restarting)."""
        if now - self._failed.get(device_id, float("-inf")) < RETRY_FAILED_DEVICE:
            return True
        recent = [when for when in self._deaths.get(device_id, []) if now - when < 60]
        self._deaths[device_id] = recent
        return len(recent) >= 3 and now - recent[-1] < RETRY_FAILED_DEVICE

    def poll_devices(self) -> None:
        """Captures outputs that appeared, reopens ones whose capture ended, and forgets ones
        that went away. Nothing new is opened once the track is stopping."""
        with self._polling:
            if self._stop.is_set():
                return
            try:
                devices = self._api.output_devices()
            except Exception as exc:
                if self._listing_error is None:
                    self._listing_error = f"Couldn't list the output devices: {exc}"
                    self.errors.append(self._listing_error)
                return
            if self._listing_error is not None:  # it works again: the warning goes
                if self._listing_error in self.errors:
                    self.errors.remove(self._listing_error)
                self._listing_error = None
            now = self._clock()
            present = {device["id"]: device["name"] for device in devices}
            for device_id, capture in list(self._captures.items()):
                if not capture.alive:  # unplugged, or its stream broke
                    del self._captures[device_id]
                    self._deaths.setdefault(device_id, []).append(now)
            for device_id, name in present.items():
                if device_id in self._captures or self._backing_off(device_id, now):
                    continue
                capture = self._api.LoopbackCapture(device_id, name, self._on_packet)
                try:
                    capture.start()
                except Exception as exc:
                    if device_id not in self._failed:  # said once, not on every retry
                        self.errors.append(f"Couldn't record {name}: {exc}")
                    self._failed[device_id] = now
                    continue
                self._failed.pop(device_id, None)
                if self._stop.is_set():  # stopping began while it opened
                    capture.stop()
                    return
                self._captures[device_id] = capture
            names = [capture.name for capture in self._captures.values()]
            self.device = {"name": ", ".join(names) if names else "No output device"}

    def _watch(self) -> None:
        while not self._stop.wait(LOOK_FOR_DEVICES):
            try:
                self.poll_devices()
            except Exception:  # a bad look must never end the watching
                traceback.print_exc()

    def _write(self) -> None:
        while not self._stop.wait(WRITE_EVERY):
            try:
                self.pump()
            except Exception as exc:  # never let one bad write end the track
                if not self._broken:
                    self._broken = True
                    self.errors.append(f"Couldn't record the computer audio: {exc}")

    def pump(self, until: int | None = None) -> None:
        """Writes the mixed audio that is ready, a second at a time (after a stall, in slices,
        never all at once); at the end, everything up to ``until``."""
        with self._writing:
            self._pump(until)

    def _pump(self, until: int | None) -> None:
        with self._lock:  # the target is fixed now: audio still arriving waits for next time
            target = self._mixer.horizon(self._clock()) if until is None else until
        while True:
            with self._lock:
                block = self._mixer.flush(target, most=self.rate)
            if block.size == 0:
                return
            power = float(np.mean(block.astype(np.float64) ** 2))
            self.level_dbfs = float(20 * np.log10(np.sqrt(power) + 1e-9))
            if until is None:  # the final flush says nothing about now
                _note_level(self, self.level_dbfs)
            if self.first_buffer_at is None:
                self.first_buffer_at = self._t0  # frame 0 is the start of the recording
                if self._on_first_buffer is not None:
                    try:
                        self._on_first_buffer()
                    except Exception as exc:
                        self.errors.append(f"Couldn't save the recording details: {exc}")
            self._chunks.write(block)

    def stop(self, at: float | None = None) -> None:
        """Stops every capture and writes the end: ``at``, when the recording stopped (both
        tracks end there), else now. Never raises: the recording is saved."""
        if not self._started:
            return
        end = round(((self._clock() if at is None else at) - self._t0) * self.rate)
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)
        with self._polling:  # a look still opening a device finishes (and stops it) first
            captures = list(self._captures.values())
            self._captures.clear()
        for capture in captures:
            try:
                capture.stop()
            except Exception as exc:
                self.errors.append(f"Couldn't stop {getattr(capture, 'name', 'an output')}: {exc}")
        try:
            self.pump(until=end)
        except Exception as exc:
            self.errors.append(f"Couldn't finish the computer audio: {exc}")
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
        listener: Listener | None = None,
    ) -> None:
        self.folder = folder
        self.chunk_seconds = chunk_seconds
        self.mic_name = mic_name
        self.output_name = output_name
        self.listener = listener
        self.started_at: datetime | None = None
        self._t0 = 0.0
        self._pa: pyaudio.PyAudio | None = None
        self._keepalive = None
        self._info_lock = threading.Lock()
        self.tracks: dict[str, TrackRecorder] = {}
        self.resumed_at: float | None = None

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
            system = AllOutputsTrack(
                self.folder,
                self.chunk_seconds,
                self._t0,
                self._save_info,
                listener=self.listener,
            )
        else:
            system = TrackRecorder(
                "system",
                loopback,
                self.folder,
                self.chunk_seconds,
                self._save_info,
                speech_dbfs=CALL_SPEECH_DBFS,
                t0=self._t0,
                listener=self.listener,
            )
        mic_track = TrackRecorder(
            "mic",
            mic,
            self.folder,
            self.chunk_seconds,
            self._save_info,
            t0=self._t0,
            listener=self.listener,
        )
        self.tracks = {"mic": mic_track, "system": system}
        for track in self.tracks.values():
            track.start(self._pa)

    @property
    def every_output(self) -> bool:
        """The default: the computer's audio from every speaker and headset."""
        return not self.output_name

    def _open_one_output(self, wasapi: dict) -> dict | None:
        """The loopback device for the chosen output (or the Windows default), with a silent
        stream on that output, so loopback keeps delivering through silences. None when the
        chosen device isn't plugged in: then every speaker and headset is recorded, which
        includes wherever the call plays now."""
        assert self._pa is not None
        name = None if self.output_name == WINDOWS_DEFAULT_OUTPUT else self.output_name
        speakers = _find_device(self._pa, wasapi, name, "output")
        if speakers is None and name is not None:
            return None
        speakers = speakers or self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
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

    def mute_mic(self, muted: bool = True) -> None:
        """The mic track records silence while you dictate (a ticket note isn't part of the
        meeting); the call goes on being recorded, and it isn't a pause."""
        mic = self.tracks.get("mic")
        if mic is not None:
            mic.muted = muted

    def pause(self, paused: bool = True) -> None:
        """Pause (or resume) privately: both tracks record silence until resumed."""
        if self.paused and not paused:
            self.resumed_at = time.perf_counter()  # quiet is counted from here again
        for track in self.tracks.values():
            track.paused = paused

    def _close(self, at: float | None = None) -> list[str]:
        """Stop every stream and writer that was started (both tracks ending at ``at``, when
        the recording stopped). Never raises."""
        errors = []
        for name, track in self.tracks.items():
            try:
                track.stop(at)
            except Exception as exc:  # the other track, the files and the audio system still close
                errors.append(f"{name}: {exc}")
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
        errors = self._close(at=self._t0 + duration)
        info = self._save_info(complete=True, duration=duration)
        if errors:
            info["errors"] = errors
        return info
