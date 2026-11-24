"""The live transcript: lines a moment after each person stops talking, echo left out."""

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import capture, live, pipeline
from zachcap.api import create_app
from zachcap.live import END_SILENCE, LONGEST, RATE, Live, Resampler

AUTH = {"Authorization": "Bearer test-token"}


def loud_spans(audio: np.ndarray) -> list[tuple[int, int]]:
    """Pretend speech detection: where the audio is loud, gaps under 0.2 s joined."""
    loud = np.flatnonzero(np.abs(audio) > 0.05)
    if not loud.size:
        return []
    spans, start, last = [], int(loud[0]), int(loud[0])
    for i in loud[1:]:
        if i - last > 0.2 * RATE:
            spans.append((start, last + 1))
            start = int(i)
        last = int(i)
    spans.append((start, last + 1))
    return spans


def tone(seconds: float, rate: int = 48000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def quiet(seconds: float, rate: int = 48000) -> np.ndarray:
    return np.zeros(int(seconds * rate), np.float32)


class Feeder:
    """Feeds a track the way the recorder's writer does: 0.1 s blocks on the timeline."""

    def __init__(self, live_: Live, track: str, rate: int = 48000) -> None:
        self.live, self.track, self.rate, self.frame = live_, track, rate, 0

    def play(self, audio: np.ndarray) -> None:
        step = self.rate // 10
        for i in range(0, len(audio), step):
            block = audio[i : i + step]
            self.live.feed(self.track, block, self.rate, self.frame)
            self.frame += len(block)


def make(texts: dict | None = None) -> tuple[Live, list]:
    heard: list = []

    def transcribe(audio: np.ndarray, track: str) -> str:
        heard.append((track, round(len(audio) / RATE, 1)))
        return (texts or {}).get(track, f"words on {track}")

    return Live(transcribe, speech=loud_spans), heard


def test_resampling_in_blocks_matches_resampling_it_all():
    from scipy.signal import resample_poly

    signal = (np.random.default_rng(1).standard_normal(48000 * 3) * 0.1).astype(np.float32)
    whole = resample_poly(signal, 1, 3).astype(np.float32)
    resampler = Resampler(48000)
    pieces, i = [], 0
    for size in [700, 4800, 13, 9600, 4800] * 40:
        pieces.append(resampler.push(signal[i : i + size]))
        i += size
        if i >= len(signal):
            break
    pieces.append(resampler.flush())
    joined = np.concatenate(pieces)
    assert len(joined) == len(whole)
    # Away from the very ends, the same samples (no clicks where blocks meet).
    assert np.max(np.abs(joined[100:-100] - whole[100:-100])) < 1e-3
    assert Resampler(16000).push(signal[:10]).tolist() == signal[:10].tolist()


def test_a_line_shows_once_the_person_stops_talking():
    transcript, heard = make()
    call = Feeder(transcript, "system")
    call.play(quiet(2))
    call.play(tone(3))
    transcript.step()
    assert transcript.lines() == []  # still talking, as far as anyone knows
    call.play(quiet(END_SILENCE + 0.3))
    transcript.step()
    [line] = transcript.lines()
    assert line["track"] == "system" and line["text"] == "words on system"
    assert abs(line["start"] - 2.0) < 0.15 and abs(line["end"] - 5.0) < 0.15
    assert transcript.lines(after=line["id"]) == []
    call.play(quiet(20))
    transcript.step()
    assert len(heard) == 1  # silence isn't transcribed


def test_someone_talking_on_is_transcribed_in_pieces():
    transcript, heard = make()
    call = Feeder(transcript, "system")
    call.play(tone(LONGEST * 2 + 4))
    transcript.step()
    assert len(transcript.lines()) == 2 and all(seconds <= LONGEST + 0.5 for _, seconds in heard)
    transcript.step(final=True)  # the recording stopped: the rest is transcribed too
    assert len(transcript.lines()) == 3


def test_the_mic_hearing_the_speakers_is_left_out():
    transcript, _ = make(
        {"system": "the firewall cutover is friday", "mic": "the firewall cutover is friday"}
    )
    call, mic = Feeder(transcript, "system"), Feeder(transcript, "mic")
    call.play(quiet(1))
    mic.play(quiet(1))
    call.play(tone(2))
    mic.play(tone(2))
    call.play(quiet(1))
    mic.play(quiet(1))
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system"]

    other, _ = make({"system": "is it friday", "mic": "yes the cutover is on friday at nine"})
    call, mic = Feeder(other, "system"), Feeder(other, "mic")
    call.play(tone(2))
    mic.play(quiet(2))
    call.play(quiet(1))
    mic.play(tone(2))
    call.play(quiet(2))
    mic.play(quiet(1))
    other.step()
    assert [line["track"] for line in other.lines()] == ["system", "mic"]  # an answer stays


def test_a_track_starting_late_is_placed_on_the_timeline():
    transcript, _ = make()
    late = Feeder(transcript, "mic", rate=16000)
    late.frame = 16000 * 60  # the mic's first audio a minute in
    late.play(np.concatenate([tone(1, 16000), quiet(1, 16000)]))
    transcript.step()
    assert abs(transcript.lines()[0]["start"] - 60.0) < 0.15


def test_a_failing_transcription_never_stops_the_worker(monkeypatch):
    calls = []

    def broken(audio, track):
        calls.append(track)
        raise RuntimeError("CUDA out of memory")

    transcript = Live(broken, speech=loud_spans)
    monkeypatch.setattr(live, "TICK", 0.01)
    transcript.start()
    Feeder(transcript, "system").play(np.concatenate([tone(1), quiet(1)]))
    for _ in range(200):
        if transcript.error:
            break
        __import__("time").sleep(0.01)
    transcript.stop()
    assert transcript.error == "CUDA out of memory" and calls


class FakeLive:
    made: list = []

    def __init__(self, speech) -> None:
        self.speech, self.started, self.stopped, self.fed = speech, False, False, []
        self.error, self.skipped = None, 0.0
        FakeLive.made.append(self)

    def feed(self, track, block, rate, frame) -> None:
        self.fed.append((track, len(block), rate, frame))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def lines(self, after: int = 0) -> list[dict]:
        said = [{"id": 1, "track": "system", "start": 1.0, "end": 2.0, "text": "Is it Friday?"}]
        return [line for line in said if line["id"] > after]


class ListeningRecorder:
    """A recorder that hands the listener one block per track when it starts."""

    def __init__(self, folder: Path, listener=None, **devices) -> None:
        self.folder, self.listener = folder, listener
        self.elapsed, self.tracks, self.paused, self.started_at = 3.0, {}, False, None

    def start(self) -> None:
        self.folder.mkdir(parents=True)
        if self.listener is not None:
            self.listener("mic", np.zeros(480, np.float32), 48000, 0)

    def mute_mic(self, muted: bool = True) -> None:
        pass

    def levels(self) -> dict:
        return {}

    def errors(self) -> list:
        return []

    def stop(self) -> dict:
        return {"duration": 3.0}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", ListeningRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    monkeypatch.setattr(live, "Live", FakeLive)
    monkeypatch.setattr(live, "Speech", lambda language, hotwords: ("speech", language))
    return TestClient(create_app("test-token"))


def test_a_recording_has_a_live_transcript_until_the_next_one(client: TestClient):
    assert client.get("/api/live", headers=AUTH).json()["on"] is False
    started = client.post("/api/record/start", headers=AUTH, json={}).json()
    made = FakeLive.made[-1]
    assert made.started and made.speech == ("speech", "en")
    assert made.fed == [("mic", 480, 48000, 0)]  # the recorder's audio reaches it
    status = client.get("/api/record", headers=AUTH).json()
    assert status["live"] and status["meeting_id"] == started["meeting_id"]

    now = client.get("/api/live", headers=AUTH)
    assert now.headers["cache-control"] == "no-store"
    assert now.json()["lines"][0]["text"] == "Is it Friday?" and now.json()["recording"]
    assert client.get("/api/live?after=1", headers=AUTH).json()["lines"] == []

    client.post("/api/record/stop", headers=AUTH)
    assert made.stopped  # its speech model leaves the GPU before the meeting is processed
    after = client.get("/api/live", headers=AUTH).json()
    assert not after["recording"] and after["lines"]  # still there to read after the call


def test_a_recording_that_isnt_kept_takes_its_live_lines_with_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    watch = client.app.state.call_watch
    monkeypatch.setattr(watch, "_apps", lambda: ["Microsoft Teams"])
    watch.check()  # Teams took the mic: recording, with a live transcript
    assert client.get("/api/live", headers=AUTH).json()["on"]
    client.post("/api/record/decline", headers=AUTH)
    assert client.get("/api/live", headers=AUTH).json() == {
        "recording": False,
        "meeting_id": None,
        "on": False,
        "lines": [],
        "error": None,
        "skipped": 0,
    }


def test_the_live_transcript_can_be_turned_off(client: TestClient):
    assert client.get("/api/settings", headers=AUTH).json()["live_transcript"]
    client.put("/api/settings", headers=AUTH, json={"live_transcript": False})
    made = len(FakeLive.made)
    client.post("/api/record/start", headers=AUTH, json={})
    assert len(FakeLive.made) == made
    assert client.get("/api/record", headers=AUTH).json()["live"] is False
    client.post("/api/record/stop", headers=AUTH)
