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
    recorder = capture.Recorder(tmp_path / "meeting")

    with pytest.raises(OSError, match="Device unavailable"):
        recorder.start()

    pa = fake.instances[-1]
    assert pa.terminated and pa.streams and all(s.closed for s in pa.streams)
    assert not any(t._writer.is_alive() for t in recorder.tracks.values())


def test_recording_details_are_saved_from_the_start(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(capture, "pyaudio", fake_pyaudio())
    folder = tmp_path / "meeting"
    recorder = capture.Recorder(folder)
    recorder.start()
    saved = json.loads((folder / "recording.json").read_text(encoding="utf-8"))
    assert saved["complete"] is False and set(saved["tracks"]) == {"mic", "system"}

    for track in recorder.tracks.values():
        feed(track, 3)
    info = recorder.stop()

    assert info["complete"] is True
    assert (folder / "mic-000.flac").exists() and (folder / "system-000.flac").exists()
    assert json.loads((folder / "recording.json").read_text(encoding="utf-8"))["complete"]


def test_a_disk_error_is_reported_and_the_queue_keeps_draining(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(capture, "pyaudio", fake_pyaudio())
    recorder = capture.Recorder(tmp_path / "meeting")
    recorder.start()

    def disk_full(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(capture.sf, "SoundFile", disk_full)
    mic = recorder.tracks["mic"]
    feed(mic, 5)
    drained(mic)

    assert mic.failed and mic._queue.qsize() == 0
    assert any("No space left" in error for error in recorder.errors())
    recorder.stop()
