"""The live transcript: lines a moment after each person stops talking, echo left out, and
never a risk to the recording (bounded, never waiting, the GPU handed to meetings first).

The speech models' process is a stand-in here (test_live_child.py has the real one), so
these run without the GPU stack (CI doesn't install it)."""

import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import api, capture, cpu, dictation, echo, live, live_child, pipeline
from zachcap.api import create_app
from zachcap.live import BEHIND, END_SILENCE, LONGEST, RATE, GpuTurns, Live, Resampler, Speech

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

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


def said(seconds: float = 2.0) -> np.ndarray:
    """Someone saying something, then stopping."""
    return np.concatenate([tone(seconds), quiet(1)])


def talk(seconds: float, seed: int, rate: int = 48000) -> np.ndarray:
    """Speech-like sound: noise whose loudness rises and falls with each syllable (five a
    second), a different person for each seed."""
    rng = np.random.default_rng(seed)
    syllables = np.repeat(rng.uniform(0.1, 1.0, int(seconds * 5) + 1), rate // 5)
    smooth = np.convolve(syllables, np.ones(rate // 50) / (rate // 50), mode="same")
    loudness_ = smooth[: int(seconds * rate)]
    return (0.4 * loudness_ * rng.standard_normal(len(loudness_))).astype(np.float32)


def heard_late(audio: np.ndarray, delay: float, gain: float, rate: int = 48000) -> np.ndarray:
    """The speakers heard through the mic: that audio, later and quieter, with a little hiss."""
    late = np.concatenate([np.zeros(int(delay * rate), np.float32), gain * audio])[: len(audio)]
    hiss = 0.001 * np.random.default_rng(9).standard_normal(len(audio))
    return (late + hiss).astype(np.float32)


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


def wait_for(condition, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


# Lines --------------------------------------------------------------------------------


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
    mic.play(quiet(1))  # the mic's audio came first, yet the call's line is made first
    call.play(quiet(1))
    call.play(tone(2))
    mic.play(tone(2))
    call.play(quiet(1))
    mic.play(quiet(1))
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system"]  # never read

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


def test_a_mic_line_found_to_be_echo_later_is_taken_away():
    transcript, _ = make(
        {"system": "the firewall cutover is friday", "mic": "the firewall cutover is friday"}
    )
    call, mic = Feeder(transcript, "system"), Feeder(transcript, "mic")
    mic.play(np.concatenate([quiet(1), said()]))
    transcript.step()
    [mine] = transcript.lines()  # the panel shows it: the call's line isn't there yet
    call.play(np.concatenate([quiet(1), said()]))
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system"]
    assert transcript.dropped(upto=mine["id"]) == [mine["id"]]  # and takes it away again


def test_the_speakers_heard_through_the_mic_are_never_transcribed():
    transcript, heard = make({"system": "the cutover is friday", "mic": "follow me"})
    call_audio = np.concatenate([quiet(1), talk(3, seed=1), quiet(2)])
    Feeder(transcript, "system").play(call_audio)
    Feeder(transcript, "mic").play(heard_late(call_audio, delay=0.12, gain=0.3))
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system"]
    assert [track for track, _ in heard] == ["system"]  # not even transcribed


def test_someone_speaking_over_the_call_is_heard():
    transcript, heard = make({"system": "the cutover is friday", "mic": "no, saturday"})
    call_audio = np.concatenate([quiet(1), talk(3, seed=1), quiet(2)])
    mine = np.concatenate([quiet(1.5), 1.5 * talk(2, seed=2), quiet(2.5)])
    Feeder(transcript, "system").play(call_audio)
    Feeder(transcript, "mic").play(heard_late(call_audio, delay=0.12, gain=0.3) + mine)
    transcript.step()
    assert [line["text"] for line in transcript.lines()] == [
        "the cutover is friday",
        "no, saturday",
    ]


def test_an_answer_right_after_the_call_is_told_from_its_echo():
    transcript, heard = make({"system": "do you want friday", "mic": "friday"})
    call_audio = np.concatenate([quiet(1), talk(3, seed=1), quiet(4)])
    answer = np.concatenate([quiet(4.4), 1.5 * talk(1, seed=2), quiet(2.6)])
    Feeder(transcript, "system").play(call_audio)
    Feeder(transcript, "mic").play(heard_late(call_audio, delay=0.12, gain=0.15) + answer)
    transcript.step()
    lines = transcript.lines()
    assert [line["track"] for line in lines] == ["system", "mic"]
    assert abs(lines[1]["start"] - 4.4) < 0.2 and abs(lines[1]["end"] - 5.4) < 0.2
    assert [track for track, _ in heard] == ["system", "mic"]  # the echo before it wasn't read


def test_what_you_say_right_before_the_call_is_told_from_its_echo():
    transcript, heard = make({"system": "friday then", "mic": "friday"})
    call_audio = np.concatenate([quiet(2.4), talk(3, seed=1), quiet(3)])
    mine = np.concatenate([quiet(1), 1.5 * talk(1, seed=2), quiet(6.4)])
    Feeder(transcript, "system").play(call_audio)
    Feeder(transcript, "mic").play(heard_late(call_audio, delay=0.12, gain=0.15) + mine)
    transcript.step()
    lines = transcript.lines()
    assert [line["track"] for line in lines] == ["mic", "system"]
    assert abs(lines[0]["start"] - 1.0) < 0.2 and abs(lines[0]["end"] - 2.0) < 0.5
    assert [track for track, _ in heard] == ["system", "mic"]


def test_a_short_reply_over_the_call_is_kept_on_a_headset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(echo, "measure", lambda mic, call: 0.9)  # by chance
    transcript, _ = make({"system": "the cutover is friday", "mic": "right"})
    Feeder(transcript, "system").play(np.concatenate([quiet(1), talk(5, seed=1), quiet(2)]))
    Feeder(transcript, "mic").play(np.concatenate([quiet(3), talk(0.6, seed=2), quiet(4.4)]))
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system", "mic"]


def test_a_word_they_put_in_doesnt_split_your_sentence():
    transcript, heard = make({"system": "right", "mic": "the cutover is on friday at nine"})
    # A headset: nothing of the call reaches the mic.
    Feeder(transcript, "system").play(np.concatenate([quiet(3), talk(0.6, seed=1), quiet(4.4)]))
    Feeder(transcript, "mic").play(np.concatenate([quiet(1), talk(5, seed=2), quiet(2)]))
    transcript.step()
    [yours] = [line for line in transcript.lines() if line["track"] == "mic"]
    assert abs(yours["start"] - 1.0) < 0.2 and abs(yours["end"] - 6.0) < 0.2
    assert len(heard) == 2  # in one go, not one clip for each piece of it


def test_a_leak_unlike_the_call_is_told_from_your_voice_by_its_level():
    def transcribe(audio, track):
        if track == "system":
            return "do you want friday"
        return "friday" if np.abs(audio).max() > 0.5 else Doubtful("Thank you.")

    transcript = Live(transcribe, speech=loud_spans)
    Feeder(transcript, "system").play(np.concatenate([quiet(1), talk(3, seed=1), quiet(4)]))
    leak = np.concatenate([quiet(1), 0.2 * talk(3, seed=3), quiet(4)])  # nothing like the call
    answer = np.concatenate([quiet(4.4), 1.5 * talk(1, seed=2), quiet(2.6)])
    Feeder(transcript, "mic").play(leak + answer)
    transcript.step()
    lines = transcript.lines()
    assert [line["text"] for line in lines] == ["do you want friday", "friday"]
    assert abs(lines[1]["start"] - 4.4) < 0.3


@pytest.mark.parametrize("pause", [0.4, 1.2])
def test_an_answer_that_repeats_a_word_of_the_question_is_kept(pause: float):
    transcript, _ = make({"system": "do you want tuesday or wednesday", "mic": "wednesday"})
    Feeder(transcript, "system").play(np.concatenate([quiet(1), tone(2), quiet(5)]))
    answer = np.concatenate([quiet(3 + pause), tone(0.6), quiet(4.4 - pause)])
    Feeder(transcript, "mic").play(answer)  # the call quiet: nothing of it in the mic
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system", "mic"]


def test_what_you_said_first_is_kept_when_they_repeat_it():
    transcript, _ = make({"system": "friday at nine, got it", "mic": "friday at nine"})
    Feeder(transcript, "mic").play(np.concatenate([quiet(1), tone(1.5), quiet(4)]))
    Feeder(transcript, "system").play(np.concatenate([quiet(1.5), tone(2.5), quiet(2.5)]))
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["mic", "system"]


def test_the_echo_measure_follows_the_speakers_not_the_people():
    call_audio = talk(4, seed=1, rate=RATE)
    call = echo.loudness(call_audio)
    span = slice(100, 300)  # 2 s of the mic, compared with the call from 250 ms before
    theirs = call[span.start - echo.LATE : span.stop]
    leak = echo.loudness(heard_late(call_audio, delay=0.12, gain=0.2, rate=RATE))[span]
    assert echo.measure(leak, theirs) > 0.95 and echo.Speakers().heard(leak, theirs)
    person = echo.loudness(talk(4, seed=2, rate=RATE))[span]
    assert echo.measure(person, theirs) < echo.ECHO_LIKE
    over = echo.loudness(heard_late(call_audio, 0.12, 0.2, RATE) + talk(4, seed=2, rate=RATE))
    assert echo.measure(over[span], theirs) < echo.ECHO_LIKE  # double-talk
    hum = echo.loudness(tone(4, rate=RATE))  # the same tone on both: nothing to tell by
    assert echo.measure(hum[span], hum[span.start - echo.LATE : span.stop]) == 0
    assert echo.measure(leak, np.zeros_like(theirs)) == 0  # the call quiet
    assert echo.measure(leak, np.full_like(theirs, np.nan)) == 0  # nothing known of it


def test_a_short_stretch_must_also_be_as_quiet_as_the_speakers(monkeypatch: pytest.MonkeyPatch):
    # Over a few syllables your own voice can follow the call's by chance.
    follows = {"now": 0.75}
    monkeypatch.setattr(echo, "measure", lambda mic, call: follows["now"])
    speakers = echo.Speakers()

    def heard(seconds: float, level: float, call: float = 0.1) -> bool:
        frames = int(seconds * 100)
        theirs = np.full(frames + echo.LATE, call, np.float32)
        return speakers.heard(np.full(frames, level, np.float32), theirs)

    assert not heard(1.0, 0.01)  # nothing is known of the speakers in this mic yet
    assert heard(2, 0.01) and heard(3, 0.01)  # long ones need nothing more, and show their level
    assert heard(1.0, 0.015) and not heard(1.0, 0.1)  # ... your voice over them is louder
    assert not heard(0.6, 0.01)  # not followed closely enough for one this short
    follows["now"] = 0.85
    assert heard(0.6, 0.01) and not heard(0.6, 0.1)
    assert not heard(3, 0.01, call=0)  # the call quiet: nothing to hear


def test_the_calls_loudness_is_kept_on_the_timeline():
    history = echo.Loudness(live.HISTORY)
    history.add(np.full(1680, 0.5, np.float32), 0)  # ten frames and a half
    history.add(np.full(80, 0.5, np.float32), 1680)  # the eleventh, whole
    assert np.allclose(history.span(0, 11), 0.5) and np.isnan(history.span(11, 12)).all()
    history.add(np.full(1600, 0.25, np.float32), RATE)  # a gap: audio from 1 s on
    known = history.span(0, 110)
    assert np.isnan(known[11:100]).all() and np.allclose(known[100:], 0.25)
    history.add(np.zeros(int(live.HISTORY * RATE), np.float32), RATE + 1600)
    assert np.isnan(history.span(0, 110)).all()  # older than HISTORY: let go
    assert history.span(history.first, history.first + 1)[0] == 0


def test_the_calls_turns_have_word_gaps_closed_and_noise_left_out():
    call = np.zeros(400, np.float32)
    call[50:100] = 0.1
    call[120:200] = 0.1  # 0.2 s between words: the same turn
    call[260:300] = 0.1  # 0.6 s later: another
    assert echo.call_turns(call) == [(50, 200), (260, 300)]
    assert echo.call_turns(call + 0.001) == [(50, 200), (260, 300)]  # noise on the line, -60 dBFS
    call[280:] = np.nan  # not known yet: as it was, not a stop
    assert echo.call_turns(call) == [(50, 200), (260, 400)]
    assert echo.call_turns(np.full(100, np.nan, np.float32)) == []


class Doubtful(str):
    """Text neither Whisper nor Silero takes for speech, as Speech marks it (live.Heard)."""

    doubtful = True


class Doubting:
    """A transcriber that hears stock phrases nobody said, as Whisper does in what's left of
    the speakers in the mic once an audio chip cancelled the echo."""

    def __call__(self, audio, track):
        return "the cutover is friday" if track == "system" else Doubtful("Thank you.")


def test_a_doubtful_mic_line_while_the_call_plays_is_left_out():
    transcript = Live(Doubting(), speech=loud_spans)
    call_audio = np.concatenate([quiet(1), talk(3, seed=1), quiet(2)])
    leak = np.concatenate([quiet(2), 0.5 * talk(1, seed=3), quiet(3)])  # nothing like the call
    Feeder(transcript, "system").play(call_audio)
    Feeder(transcript, "mic").play(leak)
    transcript.step()
    assert [line["track"] for line in transcript.lines()] == ["system"]


def test_a_doubtful_line_on_the_call_is_left_out():
    transcript = Live(lambda audio, track: Doubtful("Thank you."), speech=loud_spans)
    Feeder(transcript, "system").play(said())  # hold music, say
    transcript.step()
    assert transcript.lines() == []


def test_with_the_call_quiet_every_mic_line_is_kept():
    transcript = Live(Doubting(), speech=loud_spans)
    Feeder(transcript, "system").play(quiet(6))
    Feeder(transcript, "mic").play(np.concatenate([quiet(2), 0.5 * talk(1, seed=3), quiet(3)]))
    transcript.step()
    assert [line["text"] for line in transcript.lines()] == ["Thank you."]  # maybe said


def test_lines_come_in_the_order_they_were_said():
    transcript, _ = make({"system": "is it friday", "mic": "yes the cutover is on friday at nine"})
    # You start first and talk on; the call's short line ends first, so it's made first.
    Feeder(transcript, "mic").play(np.concatenate([quiet(1), tone(4), quiet(1)]))
    Feeder(transcript, "system").play(np.concatenate([quiet(2), tone(1), quiet(3)]))
    transcript.step()
    lines = transcript.lines()
    assert [line["track"] for line in lines] == ["mic", "system"]
    assert lines[0]["id"] > lines[1]["id"]


def test_a_track_starting_late_is_placed_on_the_timeline():
    transcript, _ = make()
    late = Feeder(transcript, "mic", rate=16000)
    late.frame = 16000 * 60  # the mic's first audio a minute in
    late.play(np.concatenate([tone(1, 16000), quiet(1, 16000)]))
    transcript.step()
    assert abs(transcript.lines()[0]["start"] - 60.0) < 0.15


def test_speech_after_a_long_wait_is_still_found():
    transcript, _ = make()
    call = Feeder(transcript, "system")
    call.play(quiet(40))  # more than one look's worth: the worker was busy, or waiting
    call.play(said())
    transcript.step()
    [line] = transcript.lines()
    assert abs(line["start"] - 40.0) < 0.15


def test_a_redacted_moment_takes_its_lines_away():
    said_then = iter(["the old password", "zebra fortytwo", "thanks"])
    transcript = Live(lambda audio, track: next(said_then), speech=loud_spans)
    call = Feeder(transcript, "system")
    for _ in range(3):  # at 0 to 2 s, 6 to 8 s and 12 to 14 s
        call.play(np.concatenate([tone(2), quiet(4)]))
    transcript.step()
    assert [line["id"] for line in transcript.lines()] == [1, 2, 3]
    transcript.forget(8.5, 9.0)  # just after the line's end: a line's times aren't exact
    assert [line["text"] for line in transcript.lines()] == ["the old password", "thanks"]
    assert transcript.dropped(upto=3) == [2]  # the panel takes it away
    assert "zebra" not in transcript.text() and "zebra" not in str(transcript._lines)


# Never a risk to the recording ---------------------------------------------------------


def test_audio_waiting_for_the_worker_is_capped_and_the_oldest_goes():
    transcript, _ = make()
    call = Feeder(transcript, "system")
    call.play(quiet(BEHIND + 30))  # the worker is stuck (a model loading): nothing taken
    call.play(said())
    assert transcript._arriving["system"].frames <= BEHIND * 48000
    assert abs(transcript.skipped - 33) < 0.2
    transcript.step()
    [line] = transcript.lines()
    assert abs(line["start"] - (BEHIND + 30)) < 0.15  # still on the recording's timeline


def test_a_wait_counts_once_though_both_tracks_waited():
    class Waits:
        def ready(self):
            return False

    transcript = Live(Waits(), speech=loud_spans)
    call, mic = Feeder(transcript, "system", 16000), Feeder(transcript, "mic", 16000)
    for _ in range(10):  # ten minutes of a call, while a meeting is processed
        call.play(quiet(60, 16000))
        mic.play(quiet(60, 16000))
        transcript.step()
    waiting = len(transcript._tracks["system"].audio) / RATE
    assert abs(transcript.skipped + waiting - 600) < 1  # of the call, not of its two tracks


def test_nothing_is_kept_once_stopped():
    transcript, heard = make()
    transcript.stop()
    Feeder(transcript, "system").play(said())
    assert transcript._arriving == {}
    transcript.step()
    assert heard == [] and not transcript.on


def test_a_failing_transcription_never_stops_the_worker(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def broken(audio, track):
        calls.append(track)
        raise RuntimeError("CUDA out of memory")

    transcript = Live(broken, speech=loud_spans)
    monkeypatch.setattr(live, "TICK", 0.01)
    transcript.start()
    feeder = Feeder(transcript, "system")
    feeder.play(said())
    assert wait_for(lambda: transcript.error)
    feeder.play(said())
    assert wait_for(lambda: len(calls) == 2)  # the next line is tried all the same
    transcript.stop()
    assert transcript.join(5)
    assert transcript.error == "CUDA out of memory"


def test_an_error_is_said_until_a_line_comes_out_again():
    fails = {"now": True}

    def transcribe(audio, track):
        if fails["now"]:
            raise RuntimeError("CUDA out of memory")
        return "it works again"

    transcript = Live(transcribe, speech=loud_spans)
    call = Feeder(transcript, "system")
    call.play(said())
    transcript.step()
    assert transcript.error == "CUDA out of memory"
    fails["now"] = False
    call.play(said())
    transcript.step()
    assert [line["text"] for line in transcript.lines()] == ["it works again"]
    assert transcript.error is None


def test_stopping_never_waits_and_the_worker_frees_the_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "TICK", 0.01)
    working, finish = threading.Event(), threading.Event()
    lines, closed_on = [], []

    class Slow:
        """A line that takes a while (the CPU, a long piece)."""

        def __call__(self, audio, track):
            lines.append(track)
            working.set()
            finish.wait(5)
            return "a long line"

        def close(self):
            closed_on.append(threading.current_thread().name)

    transcript = Live(Slow(), speech=loud_spans)
    transcript.start()
    feeder = Feeder(transcript, "system")
    for _ in range(3):  # three lines waiting
        feeder.play(said())
    assert working.wait(5)
    started = time.monotonic()
    transcript.stop()  # with the recording's lock held: it must not wait for the line
    assert time.monotonic() - started < 0.5
    assert closed_on == [] and not transcript.on  # the worker still has the model
    finish.set()
    assert transcript.join(5)
    assert closed_on == ["live-transcript"]  # freed by the worker, on its own thread
    assert lines == ["system"]  # the line it was on, not the rest


IN_THE_ENGINE = """
import sys
sys.path.insert(0, sys.argv[1])
import numpy as np
from zachcap import api, live
transcript = live.Live(lambda audio, track: "said", speech=lambda audio: [(0, 8000)])
for track, rate in (("mic", 44100), ("system", 48000)):
    for frame in range(0, rate * 3, rate // 10):
        transcript.feed(track, np.zeros(rate // 10, np.float32), rate, frame)
transcript.step()
print(bool(transcript.lines()), [name for name in HEAVY if name in sys.modules])
"""


def test_the_worker_loads_nothing_heavy_into_the_engine():
    # Importing these holds up the recorder's threads for up to 0.2 s at a time, mid-call.
    heavy = ("scipy.signal", "torch", "faster_whisper", "ctranslate2", "onnxruntime")
    ran = subprocess.run(
        [sys.executable, "-c", IN_THE_ENGINE.replace("HEAVY", repr(heavy))]
        + [str(Path(live.__file__).parents[1])],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert ran.stdout.strip() == "True []", ran.stderr


def test_a_listener_hears_the_audio_once_its_on_disk_and_a_failing_one_is_let_go(
    tmp_path: Path,
):
    errors: list[str] = []
    heard: list = []

    def listener(track, block, rate, frame):
        heard.append((track, len(block), rate, frame, writer.frames))
        raise RuntimeError("the live transcript broke")

    writer = capture.ChunkWriter(tmp_path, "mic", 16000, 300, errors, listener=listener)
    for _ in range(3):
        writer.write(np.zeros(1600, np.float32))
    writer.close()
    assert heard == [("mic", 1600, 16000, 0, 1600)]  # after it was written; then let go
    assert writer.frames == 4800 and errors == []  # the recording didn't notice


def test_the_listener_keeps_the_timeline_after_a_disk_error(tmp_path: Path):
    errors: list[str] = []
    frames: list[int] = []
    writer = capture.ChunkWriter(
        tmp_path / "gone",
        "system",
        48000,
        300,
        errors,
        listener=lambda t, b, r, f: frames.append(f),
    )
    writer.write(np.zeros(4800, np.float32))  # the folder isn't there: a disk error
    writer.write(np.zeros(4800, np.float32))
    assert writer.failed and len(errors) == 1
    assert frames == [0, 4800]


# The GPU: meetings being processed come first ------------------------------------------


class FakeChild:
    """The speech models' process, as Speech sees it: what it's asked is answered at once, by
    ``replies`` (a dict, an exception to raise, or None: no answer yet), else as it should be."""

    made: list = []
    replies: dict = {}

    def __init__(self, *args: str) -> None:
        self.args, self.asked, self.ended = args, [], 0
        FakeChild.made.append(self)

    def send(self, do: str, audio=None, **more) -> None:
        assert not self.ended
        self.asked.append((do, more))

    def answer(self, timeout: float) -> dict | None:
        do = self.asked[-1][0]
        usual = {"find": {"spans": [[0, RATE]]}, "hear": {"text": "said", "doubtful": False}}
        reply = FakeChild.replies.get(do, usual[do])
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            time.sleep(0.01)
        return reply

    def end(self) -> None:
        self.ended += 1


class Check:
    """A GPU check (live.GpuCheck) that's done."""

    def __init__(self, usable: bool) -> None:
        self.usable = usable

    def look(self) -> bool:
        return self.usable


@pytest.fixture
def children() -> list[FakeChild]:
    """The speech children started."""
    FakeChild.made, FakeChild.replies = [], {}
    return FakeChild.made


def speech_on(gpu: bool, turns: GpuTurns | None = None, language: str | None = "en") -> Speech:
    return Speech(language, "Contoso", gpu=turns, check=Check(gpu), start=FakeChild)


CLIP = np.zeros(RATE, np.float32)


def test_the_live_model_is_the_meetings_on_the_gpu(children: list):
    speech = speech_on(True, GpuTurns())
    assert speech.ready() and children == []  # nothing until there's audio to look at
    assert speech.find(CLIP) == [(0, RATE)]
    heard = speech(CLIP, "system")
    assert heard == "said" and not heard.doubtful
    [child] = children  # one for everything
    assert child.args == ("cuda",)
    assert child.asked[1] == (
        "hear",
        {
            "model": "large-v3-turbo",
            "options": {"device": "cuda", "compute_type": "int8_float16"},
            "language": "en",
            "hotwords": "Contoso",
        },
    )


def test_on_the_cpu_half_the_cores_are_left_for_the_call(
    children: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(cpu, "physical_cores", lambda: 6)
    speech = speech_on(False, GpuTurns())
    assert speech.ready()
    speech(CLIP, "mic")
    assert children[0].args == ()
    asked = children[0].asked[0][1]
    assert asked["model"] == dictation.model_for("en")
    assert asked["options"] == {"device": "cpu", "compute_type": "int8", "cpu_threads": 3}
    monkeypatch.setattr(cpu, "physical_cores", lambda: 1)
    single = speech_on(False, language=None)  # one core, and any language
    assert single.ready()
    single(CLIP, "mic")
    asked = children[1].asked[0][1]
    assert asked["model"] == dictation.model_for(None) and asked["options"]["cpu_threads"] == 1


def test_a_meeting_takes_the_gpu_from_the_live_transcript(children: list):
    turns = GpuTurns()
    speech = speech_on(True, turns)
    assert speech.ready()
    speech(CLIP, "system")
    turns.meeting_waiting()  # a recording stopped: its meeting is to be transcribed
    assert not speech.ready()  # its model's process is ended, and its turn given back
    assert children[0].ended == 1
    started = time.monotonic()
    with turns.for_meeting(wait=5):
        assert time.monotonic() - started < 0.5  # it had let go already
        assert not speech.ready()
    turns.meetings_done()
    assert speech.ready()
    speech(CLIP, "system")
    assert len(children) == 2 and not children[1].ended  # started again after the meeting


def test_the_turn_is_given_back_only_once_the_models_process_is_gone(children: list, monkeypatch):
    turns = GpuTurns()
    speech = speech_on(True, turns)
    assert speech.ready()
    speech(CLIP, "system")
    held = []
    monkeypatch.setattr(FakeChild, "end", lambda self: held.append(turns._lock.locked()))
    turns.meeting_waiting()
    assert not speech.ready()
    assert held == [True] and not turns._lock.locked()


def test_a_line_under_way_gives_way_to_a_meeting_and_its_audio_waits(
    children: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(live, "LINGER", 0.3)
    turns = GpuTurns()
    speech = speech_on(True, turns)
    transcript = Live(speech, speech=loud_spans)
    FakeChild.replies["hear"] = None  # the line takes forever (the model is loading, say)
    Feeder(transcript, "system").play(said())
    threading.Timer(0.2, turns.meeting_waiting).start()
    started = time.monotonic()
    with pytest.raises(live.NotNow):
        transcript.step()
    assert 0.4 < time.monotonic() - started < 6  # it had its LINGER seconds
    assert children[0].ended == 1 and not turns._lock.locked()
    assert speech.failed is None and transcript.error is None  # nothing went wrong
    FakeChild.replies.clear()
    turns.meetings_done()
    transcript.step()
    assert [line["text"] for line in transcript.lines()] == ["said"]  # after the meeting


def test_letting_go_for_a_meeting_is_a_wait_not_an_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "TICK", 0.01)
    tried: list = []

    def transcribe(audio, track):
        tried.append(track)
        raise live.NotNow

    transcript = Live(transcribe, speech=loud_spans)
    transcript.start()
    Feeder(transcript, "system").play(said())
    assert wait_for(lambda: len(tried) > 2)  # the same line is tried again: it isn't lost
    assert transcript.error is None and transcript.waiting
    transcript.stop()
    assert transcript.join(5)


def test_stopping_doesnt_wait_for_a_line_that_takes_forever(
    children: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(live, "LINGER", 0.1)
    monkeypatch.setattr(live, "TICK", 0.01)
    FakeChild.replies["hear"] = None
    transcript = Live(speech_on(False), speech=loud_spans)
    transcript.start()
    Feeder(transcript, "system").play(said())
    assert wait_for(lambda: children and children[0].asked)
    transcript.stop()
    assert transcript.join(5) and children[0].ended


def test_on_the_cpu_it_waits_while_a_meeting_is_processed(children: list):
    turns = GpuTurns()
    speech = speech_on(False, turns)
    assert speech.ready()
    speech(CLIP, "system")
    turns.meeting_waiting()  # it will use every core
    assert not speech.ready() and children[0].ended
    started = time.monotonic()
    with turns.for_meeting(wait=5):  # which needn't wait: there's no turn to give back
        assert time.monotonic() - started < 0.5
    turns.meetings_done()
    assert speech.ready()


def test_a_meeting_never_waits_long_for_the_live_transcript():
    turns = GpuTurns()
    assert turns.take()  # the live transcript's, and it's stuck (a driver that hangs)
    started = time.monotonic()
    with turns.for_meeting(wait=0.2):
        waited = time.monotonic() - started
    assert 0.15 < waited < 2
    assert not turns.take()  # still a meeting's until none is left
    turns.meetings_done()


def test_a_crash_is_a_notice_and_the_models_process_is_started_again(children: list):
    transcript = Live(speech_on(False), speech=loud_spans)
    call = Feeder(transcript, "system")
    FakeChild.replies["hear"] = live_child.Gone("0xC0000409")
    call.play(said())
    transcript.step()  # nothing is raised: that line is lost
    assert transcript.error == "its speech model stopped (0xC0000409) and is starting again"
    assert children[0].ended and transcript.lines() == []
    FakeChild.replies.clear()
    call.play(said())
    transcript.step()
    assert [line["text"] for line in transcript.lines()] == ["said"] and len(children) == 2
    assert transcript.error is None  # it works again


def test_one_that_keeps_crashing_is_let_go_of(children: list, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "TICK", 0.01)
    transcript = Live(speech_on(False))
    FakeChild.replies["find"] = live_child.Gone("0xC0000005")  # even looking for speech
    transcript.start()
    call = Feeder(transcript, "system")
    call.play(said())
    assert transcript.join(10)  # its worker ended by itself
    assert len(children) == live.RESTARTS + 1 and all(child.ended for child in children)
    assert "stopped 6 times (0xC0000005)" in transcript.error and not transcript.waiting
    assert transcript.on  # the panel stays, and says so
    call.play(said())
    assert transcript._arriving == {}  # nothing more is kept for it


def test_one_that_hangs_is_ended(children: list, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live, "ANSWER", 0.1)
    monkeypatch.setattr(live, "FIRST", 0.2)
    speech = speech_on(False)
    assert speech.ready()
    FakeChild.replies["hear"] = None
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="no answer for 0 seconds"):
        speech(CLIP, "mic")
    assert 0.25 < time.monotonic() - started < 6 and children[0].ended  # it may be loading
    FakeChild.replies.clear()
    assert speech(CLIP, "mic") == "said" and len(children) == 2


def test_a_line_that_fails_gets_a_fresh_model_for_the_next(children: list):
    speech = speech_on(False)
    assert speech.ready()
    FakeChild.replies["hear"] = {"error": "CUDA failed with error unspecified launch failure"}
    with pytest.raises(RuntimeError, match="unspecified launch failure"):
        speech(CLIP, "system")
    FakeChild.replies.clear()
    assert speech(CLIP, "system") == "said"
    assert children[0].ended and len(children) == 2


def test_one_that_cant_be_started_is_a_notice_too():
    def refused(*args):
        raise OSError("blocked by your administrator")

    speech = Speech("en", None, start=refused)
    assert speech.ready()
    for _ in range(live.RESTARTS + 1):
        with pytest.raises(RuntimeError, match="blocked by your administrator"):
            speech.find(CLIP)
    assert speech.failed and not speech.ready()


@pytest.mark.parametrize(
    ("load", "said"), [("missing", "isn't on this PC yet"), ("failed", "didn't load .*model.bin")]
)
def test_a_model_that_doesnt_load_is_tried_once_a_recording(children: list, load: str, said: str):
    # "failed": what a download that was cut off looks like, or a disk that's full.
    FakeChild.replies["hear"] = {"load": load, "error": "Unable to open file 'model.bin'"}
    speech = speech_on(True, GpuTurns())
    assert speech.ready()
    for _ in range(3):
        with pytest.raises(RuntimeError, match=said):
            speech(CLIP, "mic")
    assert len(children) == 1 and len(children[0].asked) == 1
    assert speech.missing == "large-v3-turbo"  # to be downloaded, or finished, after the call


def test_a_closed_speech_model_never_starts_again(children: list):
    turns = GpuTurns()
    speech = speech_on(True, turns)
    assert speech.ready()
    speech(CLIP, "system")
    speech.close()
    assert children[0].ended and not speech.ready()
    with pytest.raises(RuntimeError):
        speech(CLIP, "system")
    with pytest.raises(RuntimeError):
        speech.find(CLIP)
    assert len(children) == 1
    assert turns.take()  # its turn was given back


def test_a_summary_written_on_this_pc_takes_the_gpu_from_the_live_transcript(
    children: list, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(live, "TICK", 0.01)
    worker = api.Worker()
    transcript = Live(speech_on(True, worker.gpu), speech=loud_spans)
    transcript.start()
    call = Feeder(transcript, "system")
    call.play(said())
    assert wait_for(transcript.lines)
    seen: list = []
    written = threading.Event()

    def write() -> None:
        seen.append((children[0].ended, wait_for(lambda: transcript.waiting)))
        call.play(said())  # the call goes on meanwhile
        time.sleep(0.1)
        seen.append(len(transcript.lines()))
        written.set()

    worker.in_turn(write)
    assert written.wait(10)
    assert seen == [(1, True), 1]  # the live model was gone before it started, and stayed so
    assert wait_for(lambda: len(transcript.lines()) == 2)  # and is back after it
    assert len(children) == 2 and not transcript.waiting
    transcript.stop()
    assert transcript.join(5)


def test_the_gpu_has_one_model_at_a_time_however_meetings_and_calls_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real worker, turns, Live and Speech: recordings back to back, and meetings and
    summaries written here handed over at any moment."""
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(live, "TICK", 0.01)
    monkeypatch.setattr(live, "LINGER", 0.05)
    on_gpu: set = set()
    most, processed = [0], []

    def take_up(model) -> None:
        on_gpu.add(model)
        most[0] = max(most[0], len(on_gpu))

    class LoadingChild(FakeChild):
        def answer(self, timeout: float) -> dict | None:
            if self.asked[-1][0] == "hear":
                take_up(self)
                time.sleep(0.02)
            return super().answer(timeout)

        def end(self) -> None:
            super().end()
            on_gpu.discard(self)

    def process(store, meeting_id, log=print):
        take_up(meeting_id)
        time.sleep(0.03)
        on_gpu.discard(meeting_id)
        processed.append(meeting_id)
        return "ready"

    monkeypatch.setattr(pipeline, "process", process)
    monkeypatch.setattr(api.tickets, "fill", lambda *args: None)
    monkeypatch.setattr(api.export, "auto_export", lambda *args: None)
    worker = api.Worker()

    def write(name: str) -> None:  # in two steps: the meetings recorded meanwhile go first
        for _ in range(2):
            take_up(name)
            time.sleep(0.01)
            on_gpu.discard(name)
            worker.meetings_first()
        processed.append(name)

    recordings, writing, rng = [], 0, np.random.default_rng(5)
    for number in range(1, 13):
        speech = Speech("en", None, gpu=worker.gpu, check=Check(True), start=LoadingChild)
        transcript = Live(speech, speech=loud_spans)
        transcript.start()
        recordings.append(transcript)
        call = Feeder(transcript, "system", 16000)
        for _ in range(4):
            call.play(np.concatenate([tone(0.5, 16000), quiet(1, 16000)]))
            time.sleep(float(rng.uniform(0.005, 0.05)))
            if rng.random() < 0.3:
                worker.submit(100 + number)
            if rng.random() < 0.2:
                worker.in_turn(partial(write, f"summary {writing}"))
                writing += 1
        transcript.stop()
        worker.submit(number)
    assert all(transcript.join(10) for transcript in recordings)
    assert wait_for(lambda: not worker.gpu.wanted(), 20)
    assert most[0] == 1 and set(range(1, 13)) <= set(processed)
    assert writing and {f"summary {n}" for n in range(writing)} <= set(processed)
    assert not on_gpu and not worker.gpu._lock.locked()  # nothing left behind


def test_while_a_meeting_has_the_gpu_the_audio_waits(monkeypatch: pytest.MonkeyPatch):
    gpu_free = {"now": False}
    heard: list = []

    class Waits:
        def ready(self):
            return gpu_free["now"]

        def __call__(self, audio, track):
            heard.append(track)
            return "after the meeting"

    transcript = Live(Waits(), speech=loud_spans)
    call = Feeder(transcript, "system")
    call.play(np.concatenate([quiet(5), said()]))
    transcript.step()
    assert transcript.waiting and heard == [] and transcript.lines() == []
    gpu_free["now"] = True
    transcript.step()
    [line] = transcript.lines()
    assert not transcript.waiting and abs(line["start"] - 5.0) < 0.15
    # Waiting longer than BEHIND: it skips ahead, and says how much.
    gpu_free["now"] = False
    for _ in range(3):
        call.play(quiet(BEHIND / 2))
        transcript.step()
    waiting = len(transcript._tracks["system"].audio) / RATE
    assert waiting <= BEHIND and transcript.skipped > BEHIND - live.CAUGHT_UP
    # Every second since the line is still waiting, or counted as skipped.
    assert abs(waiting + transcript.skipped - (1 + 1.5 * BEHIND)) < 0.1


# Through the API ------------------------------------------------------------------------


class FakeLive:
    made: list = []

    def __init__(self, speech) -> None:
        self.speech, self.started, self.on, self.fed = speech, False, True, []
        self.error, self.skipped, self.waiting, self.missing = None, 0.0, False, None
        self.forgotten: list = []
        FakeLive.made.append(self)

    def feed(self, track, block, rate, frame) -> None:
        self.fed.append((track, len(block), rate, frame))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.on = False

    def lines(self, after: int = 0) -> list[dict]:
        said = [{"id": 1, "track": "system", "start": 1.0, "end": 2.0, "text": "Is it Friday?"}]
        return [line for line in said if line["id"] > after]

    def dropped(self, upto: int) -> list[int]:
        return []

    def forget(self, start: float, end: float) -> None:
        self.forgotten.append((start, end))


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


def with_gpu(monkeypatch: pytest.MonkeyPatch, usable: bool) -> None:
    monkeypatch.setattr(live_child, "gpu_usable", lambda: usable)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The engine with a pretend recorder and live transcript, on a PC without a CUDA GPU
    unless a test says otherwise (with_gpu): never this PC's own."""
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", ListeningRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    monkeypatch.setattr(live, "Live", FakeLive)
    monkeypatch.setattr(live, "Speech", lambda language, hotwords, **gpu: ("speech", language))
    with_gpu(monkeypatch, False)
    FakeLive.made = []
    return TestClient(create_app("test-token"), base_url=LOCAL)


@pytest.fixture
def client(engine: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The engine on a PC with a CUDA GPU, which has been looked for (as at startup)."""
    with_gpu(monkeypatch, True)
    engine.app.state.look_for_gpu()
    return engine


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
    body = now.json()
    assert body["lines"][0]["text"] == "Is it Friday?" and body["recording"] and body["on"]
    assert body["elapsed"] == 3.0 and body["me"] == "You" and body["paused"] is False
    assert body["meeting_id"] == started["meeting_id"] and body["dropped"] == []
    cursor = f"/api/live?run={body['run']}&after=1"
    assert client.get(cursor, headers=AUTH).json()["lines"] == []
    # A cursor from another live transcript (an earlier recording's) counts for nothing.
    assert client.get("/api/live?run=earlier&after=1", headers=AUTH).json()["lines"]

    client.post("/api/record/stop", headers=AUTH)
    assert not made.on  # its speech model leaves the GPU before the meeting is processed
    after = client.get("/api/live", headers=AUTH).json()
    assert not after["recording"] and not after["on"] and after["lines"]  # still readable
    assert after["elapsed"] is None


def test_a_second_recording_gets_a_live_transcript_of_its_own(client: TestClient):
    first = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    first_run = client.get("/api/live", headers=AUTH).json()["run"]
    client.post("/api/record/stop", headers=AUTH)
    second = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    assert len(FakeLive.made) == 2 and FakeLive.made[0] is not FakeLive.made[1]
    assert not FakeLive.made[0].on and FakeLive.made[1].on and FakeLive.made[1].started
    body = client.get("/api/live", headers=AUTH).json()
    assert body["meeting_id"] == second != first and body["on"]
    assert body["run"] and body["run"] != first_run
    client.post("/api/record/stop", headers=AUTH)


def test_a_recording_that_isnt_kept_takes_its_live_lines_with_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    watch = client.app.state.call_watch
    monkeypatch.setattr(watch, "_apps", lambda: ["Microsoft Teams"])
    watch.check()  # Teams took the mic: recording, with a live transcript
    assert client.get("/api/live", headers=AUTH).json()["on"]
    made = FakeLive.made[-1]
    client.post("/api/record/decline", headers=AUTH)
    assert not made.on
    assert client.get("/api/live", headers=AUTH).json() == {
        "recording": False,
        "run": None,
        "meeting_id": None,
        "on": False,
        "elapsed": None,
        "paused": False,
        "me": None,
        "lines": [],
        "dropped": [],
        "waiting": False,
        "error": None,
        "skipped": 0,
    }


def test_a_deleted_meeting_takes_its_live_lines_with_it(client: TestClient):
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    client.post("/api/record/stop", headers=AUTH)
    assert client.get("/api/live", headers=AUTH).json()["lines"]  # still readable after it
    client.delete("/api/meetings/999", headers=AUTH)
    assert client.get("/api/live", headers=AUTH).json()["lines"]  # another meeting's: kept
    assert client.delete(f"/api/meetings/{meeting_id}", headers=AUTH).status_code == 200
    body = client.get("/api/live", headers=AUTH).json()
    assert body["lines"] == [] and body["run"] is None


def test_a_redacted_moment_leaves_the_live_lines_too(client: TestClient):
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    client.post("/api/record/stop", headers=AUTH)
    redacted = {}

    def redact() -> bool:  # refused while the worker has the meeting
        redacted["answer"] = client.post(
            f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 1, "end": 2.5}
        )
        return redacted["answer"].status_code != 409

    assert wait_for(redact) and redacted["answer"].status_code == 200
    assert FakeLive.made[-1].forgotten == [(1.0, 2.5)]


def test_turning_it_off_stops_the_one_running_and_the_next_has_none(client: TestClient):
    assert client.get("/api/settings", headers=AUTH).json()["live_transcript"]
    client.post("/api/record/start", headers=AUTH, json={})
    made = FakeLive.made[-1]
    saved = client.put("/api/settings", headers=AUTH, json={"live_transcript": False}).json()
    assert saved["live_transcript"] is False
    assert not made.on  # its model leaves the GPU now, not after the call
    assert client.get("/api/record", headers=AUTH).json()["live"] is False  # the panel closes
    client.post("/api/record/stop", headers=AUTH)
    client.post("/api/record/start", headers=AUTH, json={})
    assert FakeLive.made == [made]
    assert client.get("/api/record", headers=AUTH).json()["live"] is False
    client.post("/api/record/stop", headers=AUTH)


def test_on_by_default_only_with_a_cuda_gpu(engine: TestClient, monkeypatch):
    with_gpu(monkeypatch, False)
    engine.app.state.look_for_gpu()  # as the engine does as it starts
    assert client_setting(engine) is False
    engine.post("/api/record/start", headers=AUTH, json={})
    assert FakeLive.made == []  # on the CPU it waits to be turned on
    engine.post("/api/record/stop", headers=AUTH)
    engine.put("/api/settings", headers=AUTH, json={"live_transcript": True})
    engine.post("/api/record/start", headers=AUTH, json={})
    assert len(FakeLive.made) == 1  # turned on, the CPU does it
    engine.post("/api/record/stop", headers=AUTH)


def test_on_by_default_with_a_cuda_gpu_unless_turned_off(engine: TestClient, monkeypatch):
    with_gpu(monkeypatch, True)
    engine.app.state.look_for_gpu()
    assert client_setting(engine) is True
    engine.post("/api/record/start", headers=AUTH, json={})
    assert len(FakeLive.made) == 1
    engine.post("/api/record/stop", headers=AUTH)
    engine.put("/api/settings", headers=AUTH, json={"live_transcript": False})
    assert client_setting(engine) is False
    engine.post("/api/record/start", headers=AUTH, json={})
    assert len(FakeLive.made) == 1
    engine.post("/api/record/stop", headers=AUTH)


def test_nothing_waits_for_the_gpu_check(engine: TestClient, monkeypatch):
    looking, done = threading.Event(), threading.Event()

    def slow_look():
        looking.set()
        done.wait(5)  # importing torch for the first time
        return True

    monkeypatch.setattr(live_child, "gpu_usable", slow_look)
    check = threading.Thread(target=engine.app.state.look_for_gpu)
    check.start()
    assert looking.wait(5)
    started = time.monotonic()
    assert client_setting(engine) is False  # Settings, which the dictation keys need as well
    engine.post("/api/record/start", headers=AUTH, json={})
    assert time.monotonic() - started < 2
    assert FakeLive.made == []  # started before the look was done: none this time
    engine.post("/api/record/stop", headers=AUTH)
    done.set()
    check.join(5)
    assert client_setting(engine) is True
    engine.post("/api/record/start", headers=AUTH, json={})
    assert len(FakeLive.made) == 1
    engine.post("/api/record/stop", headers=AUTH)


def test_until_this_starts_look_is_done_the_last_ones_answer_counts(
    engine: TestClient, monkeypatch
):
    with_gpu(monkeypatch, True)
    engine.app.state.look_for_gpu()
    # The engine starts again (mid-call, say), and its look hasn't finished.
    again = TestClient(create_app("test-token"), base_url=LOCAL)
    assert client_setting(again) is True
    again.post("/api/record/start", headers=AUTH, json={})
    assert len(FakeLive.made) == 1
    again.post("/api/record/stop", headers=AUTH)
    with_gpu(monkeypatch, False)  # no GPU any more
    again.app.state.look_for_gpu()
    assert client_setting(again) is False
    assert client_setting(TestClient(create_app("test-token"), base_url=LOCAL)) is False


def test_a_check_that_fails_means_no_gpu(engine: TestClient, monkeypatch):
    def crashed():
        raise live_child.Gone("0xC0000005")

    monkeypatch.setattr(live_child, "gpu_usable", crashed)
    engine.app.state.look_for_gpu()
    assert client_setting(engine) is False


def test_turned_off_needs_no_gpu_check(engine: TestClient, monkeypatch):
    looked = []
    monkeypatch.setattr(live_child, "gpu_usable", lambda: looked.append(1) or True)
    engine.put("/api/settings", headers=AUTH, json={"live_transcript": False})
    engine.app.state.look_for_gpu()
    assert client_setting(engine) is False and looked == []
    # Turned on, the look says what it runs on.
    engine.put("/api/settings", headers=AUTH, json={"live_transcript": True})
    engine.app.state.look_for_gpu()
    assert looked == [1]


class FakeDownload:
    """The download of a model between calls: what was asked of it."""

    def __init__(self) -> None:
        self.asked: list = []
        FakeDownload.made = self

    def start(self, model: str) -> None:
        self.asked.append(model)

    def stop(self) -> None:
        self.asked.append("stopped")


def test_a_missing_model_is_downloaded_once_no_call_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(live_child, "Download", FakeDownload)
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", ListeningRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    monkeypatch.setattr(live, "Live", FakeLive)
    monkeypatch.setattr(live, "Speech", lambda language, hotwords, **gpu: ("speech", language))
    with_gpu(monkeypatch, False)
    FakeLive.made = []
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    asked = FakeDownload.made.asked

    def call() -> None:
        client.post("/api/record/start", headers=AUTH, json={})
        FakeLive.made[-1].missing = "small.en"

    client.put("/api/settings", headers=AUTH, json={"live_transcript": True})
    call()
    assert asked == ["stopped"]  # nothing is downloaded during the call
    client.post("/api/record/stop", headers=AUTH)
    assert asked == ["stopped", "small.en"]  # now it is
    call()
    assert asked[2:] == ["stopped"]  # the next call has the bandwidth to itself again
    # Turned off during the call: its model isn't wanted any more.
    client.put("/api/settings", headers=AUTH, json={"live_transcript": False})
    client.post("/api/record/stop", headers=AUTH)
    assert "small.en" not in asked[2:]


def client_setting(client: TestClient) -> bool:
    return client.get("/api/settings", headers=AUTH).json()["live_transcript"]


def test_a_meeting_is_processed_only_once_the_live_transcript_let_go(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """The real worker and turns: the meeting of a recording that stopped takes the GPU."""
    gpu = client.app.state.worker_gpu
    processed = threading.Event()
    held_by_live = []

    def process(store, meeting_id, log=print):
        held_by_live.append(gpu.take())  # False: the meeting has it
        processed.set()
        return "ready"

    monkeypatch.setattr(pipeline, "process", process)
    assert gpu.take()  # the live transcript of the recording running now
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    client.post("/api/record/stop", headers=AUTH)
    assert gpu.wanted()  # asked to let go as the recording stopped
    assert not processed.wait(0.3)  # not while the live transcript still has it
    gpu.give_back()  # it lets go (Speech.ready() sees the meeting waiting)
    assert processed.wait(5) and held_by_live == [False]
    assert wait_for(lambda: not gpu.wanted())  # none left: the live transcript's again
    assert client.get(f"/api/meetings/{meeting_id}", headers=AUTH).status_code == 200
