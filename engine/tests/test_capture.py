"""The recorder against a fake audio system: no devices, no sound."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zachcap import capture

MIC = {"index": 0, "name": "Mic", "hostApi": 0, "maxInputChannels": 1, "maxOutputChannels": 0,
       "defaultSampleRate": 16000}  # fmt: skip
SPEAKERS = {"index": 1, "name": "Speakers", "hostApi": 0, "maxInputChannels": 0,
            "maxOutputChannels": 2, "defaultSampleRate": 48000}  # fmt: skip
# The tests of PortAudio loopback pick the Windows default output, the old behavior; the
# default records every output (tested below with a fake Core Audio).
ONE_OUTPUT = capture.WINDOWS_DEFAULT_OUTPUT
LOOPBACK = {"index": 2, "name": "Speakers [Loopback]", "hostApi": 0, "maxInputChannels": 2,
            "maxOutputChannels": 0, "defaultSampleRate": 48000,
            "isLoopbackDevice": True}  # fmt: skip


class FakeStream:
    def __init__(self) -> None:
        self.closed = False

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def fake_pyaudio(fail_loopback: bool = False) -> SimpleNamespace:
    instances = []

    class FakePyAudio:
        def __init__(self) -> None:
            self.streams: list[FakeStream] = []
            self.terminated = False
            instances.append(self)

        def get_host_api_info_by_type(self, kind):
            return {"index": 0, "defaultInputDevice": 0, "defaultOutputDevice": 1}

        def get_device_info_by_index(self, index):
            return [MIC, SPEAKERS, LOOPBACK][index]

        def get_device_count(self):
            return 3

        def get_loopback_device_info_generator(self):
            yield LOOPBACK

        def open(self, **options):
            if fail_loopback and options.get("input_device_index") == LOOPBACK["index"]:
                raise OSError("Device unavailable")
            stream = FakeStream()
            self.streams.append(stream)
            return stream

        def terminate(self) -> None:
            self.terminated = True

    return SimpleNamespace(
        PyAudio=FakePyAudio, instances=instances, paInt16=8, paContinue=0, paWASAPI=13
    )


def feed(track: capture.TrackRecorder, buffers: int = 1) -> None:
    for _ in range(buffers):
        track._callback(bytes(1024 * track.channels * 2), 1024, None, 0)


def drained(track: capture.TrackRecorder) -> None:
    deadline = time.monotonic() + 5
    while track._queue.qsize() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)  # the writer finishes the buffer it took last


def test_a_failed_start_closes_everything_it_opened(tmp_path: Path, monkeypatch):
    fake = fake_pyaudio(fail_loopback=True)
    monkeypatch.setattr(capture, "pyaudio", fake)
    recorder = capture.Recorder(tmp_path / "meeting", output_name=ONE_OUTPUT)

    with pytest.raises(OSError, match="Device unavailable"):
        recorder.start()

    pa = fake.instances[-1]
    assert pa.terminated and pa.streams and all(s.closed for s in pa.streams)
    assert not any(t._writer.is_alive() for t in recorder.tracks.values())


def test_recording_details_are_saved_from_the_start(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(capture, "pyaudio", fake_pyaudio())
    folder = tmp_path / "meeting"
    recorder = capture.Recorder(folder, output_name=ONE_OUTPUT)
    recorder.start()
    saved = json.loads((folder / "recording.json").read_text(encoding="utf-8"))
    assert saved["complete"] is False and set(saved["tracks"]) == {"mic", "system"}

    for track in recorder.tracks.values():
        feed(track, 3)
    info = recorder.stop()

    assert info["complete"] is True
    assert (folder / "mic-000.flac").exists() and (folder / "system-000.flac").exists()
    assert json.loads((folder / "recording.json").read_text(encoding="utf-8"))["complete"]


def test_a_private_pause_records_silence_and_keeps_time(tmp_path: Path, monkeypatch):
    import numpy as np
    import soundfile as sf

    fake = fake_pyaudio()
    monkeypatch.setattr(capture, "pyaudio", fake)
    clock = FakeClock(100.0)
    mic = capture.TrackRecorder("mic", MIC, tmp_path, 300, t0=100.0, clock=clock)
    mic.start(fake.PyAudio())
    loud = np.full(1024, 8000, np.int16).tobytes()

    for paused in (False, True, False):
        clock.now += 1024 / 16000  # one buffer's worth of time
        mic.paused = paused
        mic._callback(loud, 1024, None, 0)
        drained(mic)
        if paused:
            assert mic.level_dbfs < -100
    mic.stop()

    audio, _ = sf.read(tmp_path / "mic-000.flac", dtype="int16")
    assert len(audio) == 3 * 1024  # the paused stretch is kept, as silence
    assert audio[:1024].min() > 0 and not audio[1024:2048].any() and audio[2048:].min() > 0


def test_a_disk_error_is_reported_and_the_queue_keeps_draining(tmp_path: Path, monkeypatch):
    fake = fake_pyaudio()
    monkeypatch.setattr(capture, "pyaudio", fake)

    def disk_full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(capture.sf, "SoundFile", disk_full)
    clock = FakeClock(100.0)
    mic = capture.TrackRecorder("mic", MIC, tmp_path, 300, t0=100.0, clock=clock)
    mic.start(fake.PyAudio())
    for _ in range(5):
        clock.now += 1024 / 16000
        mic._callback(bytes(2048), 1024, None, 0)
    clock.now += 1.0  # past the short delay before audio is written
    deadline = time.monotonic() + 5
    while not mic.failed and time.monotonic() < deadline:
        time.sleep(0.02)

    assert mic.failed and mic._queue.qsize() == 0
    assert any("No space left" in error for error in mic.errors)
    mic.stop()


def test_audio_captured_during_a_pause_stays_silent_after_resume(tmp_path: Path, monkeypatch):
    import numpy as np
    import soundfile as sf

    fake = fake_pyaudio()
    monkeypatch.setattr(capture, "pyaudio", fake)
    track = capture.TrackRecorder("mic", MIC, tmp_path, chunk_seconds=300)
    loud = np.full(1024, 8000, np.int16).tobytes()

    # Two buffers arrive while paused and are written only after Resume (a slow disk).
    track.paused = True
    track._callback(loud, 1024, None, 0)
    track._callback(loud, 1024, None, 0)
    track.paused = False
    track.start(fake.PyAudio())
    track.stop()

    audio, _ = sf.read(tmp_path / "mic-000.flac", dtype="int16")
    assert len(audio) == 2 * 1024 and not audio.any()


# ---- every speaker and headset (the default), against a fake Core Audio ----


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeOutputs:
    """Stands in for zachcap.loopback: a device list the test changes, and captures the
    test plays audio through."""

    def __init__(self, *names: str) -> None:
        self.devices = [{"id": name.lower(), "name": name, "default": False} for name in names]
        self.captures: dict[str, object] = {}
        self.refuse: set[str] = set()
        self.listing_error: str | None = None
        outputs = self

        class Capture:
            def __init__(self, device_id, name, on_packet) -> None:
                self.device_id, self.name, self.on_packet = device_id, name, on_packet
                self.alive = False
                self.error = None

            def start(self) -> None:
                if self.device_id in outputs.refuse:
                    raise RuntimeError("Device in use")
                self.alive = True
                outputs.captures[self.device_id] = self

            def stop(self) -> None:
                self.alive = False

        self.LoopbackCapture = Capture

    def output_devices(self) -> list[dict]:
        if self.listing_error:
            raise OSError(self.listing_error)
        return [dict(device) for device in self.devices]

    def play(self, device_id, audio, rate, start, clock) -> None:
        """10 ms packets of ``audio`` from ``start``, with the clock moving along."""
        capture = self.captures[device_id]
        step = rate // 100
        for index in range(0, audio.size, step):
            stamp = start + index / rate
            clock.now = stamp + 0.01
            capture.on_packet(device_id, audio[index : index + step], rate, stamp, False)


def tone(seconds: float, rate: int = 48000, freq: float = 440.0):
    import numpy as np

    t = np.arange(int(seconds * rate)) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_every_output_is_recorded_and_a_headset_joins_mid_call(tmp_path: Path):
    import numpy as np
    import soundfile as sf

    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    outputs.play("speakers", tone(1.0), 48000, 100.0, clock)
    outputs.devices.append({"id": "headset", "name": "Headset", "default": False})
    track.poll_devices()  # plugged in at 1 s
    assert track.device["name"] == "Speakers, Headset"
    outputs.play("headset", tone(1.0, 44100, 300), 44100, 101.5, clock)
    clock.now = 103.0
    track.stop()

    audio, rate = sf.read(tmp_path / "system-000.flac", dtype="float32")
    assert rate == 48000 and len(audio) == 3 * 48000  # real time, start to stop
    assert sf.info(tmp_path / "system-000.flac").subtype == "PCM_24"
    loud = np.abs(audio) > 0.01
    assert loud[:47000].mean() > 0.9  # the speakers, 0-1 s
    assert not loud[48500:71500].any()  # nothing between
    assert loud[72500:119500].mean() > 0.9  # the headset, 1.5-2.5 s
    assert not loud[121000:].any() and not track.errors


def test_an_output_that_cant_be_recorded_is_reported_once_and_retried(tmp_path: Path):
    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers", "Dock")
    outputs.refuse.add("dock")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    clock.now = 110.0
    track.poll_devices()  # too soon to retry
    assert track.errors == ["Couldn't record Dock: Device in use"]
    assert "dock" not in outputs.captures

    outputs.refuse.clear()
    clock.now = 131.0
    track.poll_devices()  # retried after 30 s, and now it works
    assert "dock" in outputs.captures and track.device["name"] == "Speakers, Dock"
    track.stop()
    assert track.errors == ["Couldn't record Dock: Device in use"]


def test_an_unplugged_output_is_dropped_and_the_track_keeps_time(tmp_path: Path):
    import soundfile as sf

    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers", "Headset")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    outputs.play("headset", tone(0.5), 48000, 100.0, clock)
    outputs.captures["headset"].alive = False  # unplugged
    outputs.devices = [d for d in outputs.devices if d["id"] != "headset"]
    clock.now = 102.0
    track.poll_devices()
    assert track.device["name"] == "Speakers"
    clock.now = 104.0
    track.stop()

    assert sf.info(tmp_path / "system-000.flac").frames == 4 * 48000


def test_no_device_list_still_records_silence_in_time(tmp_path: Path):
    import soundfile as sf

    clock, outputs = FakeClock(100.0), FakeOutputs()
    outputs.listing_error = "Audio service not running"
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    track.poll_devices()
    clock.now = 101.5
    track.stop()

    assert track.errors == ["Couldn't list the output devices: Audio service not running"]
    assert sf.info(tmp_path / "system-000.flac").frames == round(1.5 * 48000)
    assert track.device["name"] == "Every speaker and headset"  # unknown, not "none"


def test_a_private_pause_silences_every_output(tmp_path: Path):
    import numpy as np
    import soundfile as sf

    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    outputs.play("speakers", tone(1.0), 48000, 100.0, clock)
    track.paused = True
    outputs.play("speakers", tone(1.0), 48000, 101.0, clock)
    track.paused = False
    clock.now = 102.0
    track.stop()

    audio, _ = sf.read(tmp_path / "system-000.flac", dtype="float32")
    assert (np.abs(audio[:47000]) > 0.01).mean() > 0.9
    assert np.abs(audio[48500:]).max() < 1e-4  # the paused second is silent


def test_the_default_records_every_output_with_the_mic(tmp_path: Path, monkeypatch):
    import soundfile as sf

    monkeypatch.setattr(capture, "pyaudio", fake_pyaudio())
    outputs = FakeOutputs("Speakers", "Headset")
    monkeypatch.setattr(capture, "_loopback_api", lambda: outputs)
    folder = tmp_path / "meeting"
    recorder = capture.Recorder(folder)
    recorder.start()
    assert recorder.every_output and set(outputs.captures) == {"speakers", "headset"}
    started = time.perf_counter()
    outputs.captures["headset"].on_packet("headset", tone(0.2), 48000, started, False)
    feed(recorder.tracks["mic"], 3)
    time.sleep(0.4)
    info = recorder.stop()

    system = info["tracks"]["system"]
    assert system["offset"] == 0.0 and system["rate"] == 48000
    # Both tracks end when Stop was pressed, so they line up to the last moment.
    lengths = [sf.info(folder / f"{name}-000.flac").duration for name in ("mic", "system")]
    assert abs(lengths[0] - lengths[1]) < 0.01
    assert system["chunks"] == ["system-000.flac"]
    assert system["device"] == "Speakers, Headset"
    assert sf.info(folder / "system-000.flac").duration >= 0.4
    # PortAudio only opened the mic: no loopback stream and no silent keep-alive stream.
    streams = capture.pyaudio.instances[-1].streams
    assert len(streams) == 1 and streams[0].closed


def test_tracks_remember_when_they_last_had_any_sound_and_speech(tmp_path: Path, monkeypatch):
    import numpy as np

    fake = fake_pyaudio()
    monkeypatch.setattr(capture, "pyaudio", fake)
    track = capture.TrackRecorder("mic", MIC, tmp_path, chunk_seconds=300)
    track.start(fake.PyAudio())
    track._callback(bytes(2048), 1024, None, 0)  # exact zeros: a muted mic
    drained(track)
    assert track.signal_at is None and track.heard_at is None

    track._callback(np.full(1024, 3, np.int16).tobytes(), 1024, None, 0)  # room hiss
    drained(track)
    assert track.signal_at is not None and track.heard_at is None

    track._callback(np.full(1024, 4000, np.int16).tobytes(), 1024, None, 0)  # a voice
    drained(track)
    assert track.heard_at is not None
    track.stop()


def test_a_mic_that_stalls_keeps_its_place_on_the_timeline(tmp_path: Path, monkeypatch):
    # Before, a mic that delivered nothing for a while (standby, overflows) made everything
    # after it early by that much, out of step with the computer-audio track.
    import numpy as np
    import soundfile as sf

    fake = fake_pyaudio()
    monkeypatch.setattr(capture, "pyaudio", fake)
    clock = FakeClock(100.0)
    mic = capture.TrackRecorder("mic", MIC, tmp_path, 300, t0=100.0, clock=clock)
    mic.start(fake.PyAudio())
    loud = np.full(1600, 8000, np.int16).tobytes()  # 0.1 s
    clock.now += 0.1
    mic._callback(loud, 1600, None, 0)  # 0.0-0.1 s
    clock.now += 2.0  # nothing for two seconds
    mic._callback(loud, 1600, None, 0)  # 2.0-2.1 s
    drained(mic)
    mic.stop()

    audio, _ = sf.read(tmp_path / "mic-000.flac", dtype="int16")
    sounding = np.flatnonzero(np.abs(audio) > 100)
    assert abs(len(audio) - round(2.1 * 16000)) <= 1
    assert sounding[0] < 50 and abs(sounding[-1] - round(2.1 * 16000)) < 50
    assert not audio[round(0.15 * 16000) : round(1.95 * 16000)].any()


def test_a_capture_that_ends_while_its_device_is_listed_is_reopened_at_once(tmp_path: Path):
    clock, outputs = FakeClock(100.0), FakeOutputs("Headset")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    first = outputs.captures["headset"]
    first.alive = False  # e.g. a Bluetooth headset switching to its call profile
    clock.now = 102.0
    track.poll_devices()
    assert outputs.captures["headset"] is not first and outputs.captures["headset"].alive

    for when in (104.0, 106.0):  # it keeps ending: three times in a minute
        outputs.captures["headset"].alive = False
        clock.now = when
        track.poll_devices()
    outputs.captures["headset"].alive = False
    clock.now = 108.0
    track.poll_devices()
    assert "headset" not in track._captures  # left alone for a while now
    clock.now = 140.0
    track.poll_devices()
    assert "headset" in track._captures
    track.stop()


def test_nothing_new_opens_once_stopping_began(tmp_path: Path):
    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    track.stop()
    outputs.devices.append({"id": "headset", "name": "Headset", "default": False})
    track.poll_devices()
    assert "headset" not in outputs.captures


def test_a_listing_that_works_again_clears_its_warning(tmp_path: Path):
    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers")
    outputs.listing_error = "Audio service restarting"
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    assert track.errors
    outputs.listing_error = None
    track.poll_devices()
    assert track.errors == [] and "speakers" in outputs.captures
    track.stop()


def test_a_saved_output_thats_unplugged_records_every_output(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(capture, "pyaudio", fake_pyaudio())
    outputs = FakeOutputs("Speakers")
    monkeypatch.setattr(capture, "_loopback_api", lambda: outputs)
    recorder = capture.Recorder(tmp_path / "meeting", output_name="Headset (unplugged)")
    recorder.start()
    assert isinstance(recorder.tracks["system"], capture.AllOutputsTrack)
    recorder.stop()


def test_resuming_a_pause_is_remembered(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(capture, "pyaudio", fake_pyaudio())
    recorder = capture.Recorder(tmp_path / "meeting", output_name=ONE_OUTPUT)
    recorder.start()
    recorder.pause()
    assert recorder.resumed_at is None
    recorder.pause(False)
    assert recorder.resumed_at is not None
    recorder.stop()


def test_a_track_ends_when_stop_was_pressed_not_when_stopping_finished(tmp_path: Path):
    # Stopping takes a moment (captures closing, the last writes); audio that arrives
    # meanwhile must not make the call track longer than the mic track.
    import soundfile as sf

    clock, outputs = FakeClock(100.0), FakeOutputs("Speakers")
    track = capture.AllOutputsTrack(tmp_path, 300, t0=100.0, clock=clock, api=outputs)
    track.start()
    outputs.play("speakers", tone(3.0), 48000, 100.0, clock)  # the last second after Stop
    track.stop(at=102.0)  # Stop was pressed 2 s in

    assert sf.info(tmp_path / "system-000.flac").frames == 2 * 48000
