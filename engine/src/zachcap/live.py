"""The call as it happens: a rough transcript while a recording runs.

The recorder hands each track's audio over as it writes it (capture.ChunkWriter). A worker
thread turns it into 16 kHz, finds where each person stopped talking (Silero VAD, the filter
Whisper itself uses) and transcribes each stretch of speech as soon as it ends, so a line
shows a second or two after it was said. Your microphone's lines are yours; the call's are
the others'. A mic line that repeats what the call said at the same moment is the mic
hearing the speakers (echo), and is left out.

This is for glancing at during the call: the live panel, its notes, being asked something.
The transcript kept with the meeting is still made after the call by the full pipeline.
Everything here stays in memory, until the next recording starts.
"""

from __future__ import annotations

import gc
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from math import gcd

import numpy as np

from . import transcript

SETTING = "live_transcript"  # "1": a live transcript (and notes) during recordings
RATE = 16000
TICK = 0.5  # how often the worker looks for speech that ended
END_SILENCE = 0.7  # this much quiet after speech: the person stopped talking
LONGEST = 15.0  # someone talking on is transcribed in pieces this long at most
QUIET_KEPT = 1.0  # of a long quiet, only this much is kept before what comes next
SEARCH = 30.0  # speech is looked for in at most this much audio at a time
BEHIND = 120.0  # more audio than this waiting: the GPU can't keep up, skip ahead
CAUGHT_UP = 20.0  # ... to this much
PAD = 3200  # 0.2 s of audio either side of the speech, as Whisper likes it
LINES_KEPT = 2000


@dataclass
class Line:
    id: int
    track: str  # "mic" (you) or "system" (the others on the call)
    start: float  # seconds into the recording
    end: float
    text: str
    echo: bool = False  # the mic hearing the speakers: left out


class Resampler:
    """Turns blocks at a device's rate into 16 kHz with no seams where blocks meet: each
    piece is resampled with some audio either side of it, and only its own part kept."""

    RIGHT = 64  # native samples held back for the right edge (the filter is shorter)

    def __init__(self, rate: int) -> None:
        self.rate = rate
        common = gcd(rate, RATE)
        self.up, self.down = RATE // common, rate // common
        # Left context: a whole number of ``down`` steps, so every piece's output starts on
        # the same grid as resampling the whole recording at once would.
        self._left = self.down * max(1, -(-512 // self.down))
        self._held = np.zeros(0, np.float32)  # native audio whose output isn't out yet
        self._context = np.zeros(0, np.float32)  # native audio already turned, for the left edge

    def push(self, block: np.ndarray) -> np.ndarray:
        if self.rate == RATE:
            return np.asarray(block, np.float32)
        self._held = np.concatenate([self._held, np.asarray(block, np.float32)])
        ready = len(self._held) - self.RIGHT
        ready -= ready % self.down  # whole output samples only
        if ready <= 0:
            return np.zeros(0, np.float32)
        return self._turn(ready)

    def flush(self) -> np.ndarray:
        if self.rate == RATE or not len(self._held):
            return np.zeros(0, np.float32)
        return self._turn(len(self._held), final=True)

    def _turn(self, count: int, final: bool = False) -> np.ndarray:
        from scipy.signal import resample_poly

        window = np.concatenate([self._context, self._held])
        out = resample_poly(window, self.up, self.down).astype(np.float32)
        skip = len(self._context) * self.up // self.down
        keep = len(out) - skip if final else count * self.up // self.down
        piece = out[skip : skip + keep]
        # The next piece's left context: the end of what's turned now, a whole number of
        # ``down`` steps long (``count`` is one, except at the very end).
        turned = np.concatenate([self._context, self._held[:count]])
        take = min(len(turned) - len(turned) % self.down, self._left)
        self._context = turned[len(turned) - take :]
        self._held = self._held[count:]
        return piece


@dataclass
class TrackState:
    name: str
    resampler: Resampler
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # 16 kHz
    offset: int = 0  # the recording's timeline sample where ``audio`` begins
    pending: list[np.ndarray] = field(default_factory=list)  # arrived, not taken yet


class Live:
    """A live transcript of the recording running now. ``feed`` takes audio from the
    recorder's writer threads; ``start`` runs the worker; ``lines`` is what the page reads.

    ``transcribe(audio, track)`` turns a stretch of 16 kHz speech into text, and
    ``speech(audio)`` finds the speech in it as (start, end) sample pairs: given, so tests can
    pretend. ``transcribe`` may have a ``close()`` (it's called at the end)."""

    def __init__(
        self,
        transcribe: Callable[[np.ndarray, str], str],
        speech: Callable[[np.ndarray], list[tuple[int, int]]] | None = None,
    ) -> None:
        self._transcribe = transcribe
        self._speech = speech or find_speech
        self._lock = threading.Lock()  # the pending audio and the lines
        self._tracks: dict[str, TrackState] = {}
        self._lines: list[Line] = []
        self._next_id = 1
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None  # the last thing that went wrong, for the page
        self.skipped = 0.0  # seconds of audio not transcribed to catch up

    # From the recorder --------------------------------------------------------------

    def feed(self, track: str, block: np.ndarray, rate: int, frame: int) -> None:
        """Audio as the recorder writes it: ``frame`` is where the block starts on the
        recording's timeline, at ``rate``. Called from the writer threads, so it only
        queues the audio."""
        with self._lock:
            state = self._tracks.get(track)
            if state is None:
                state = self._tracks[track] = TrackState(track, Resampler(rate))
                state.offset = round(frame * RATE / rate)
            state.pending.append(np.array(block, np.float32, copy=True))

    # The worker -----------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="live-transcript", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ends the worker (after the line it's on) and frees the speech model."""
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
        close = getattr(self._transcribe, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                traceback.print_exc()

    def _run(self) -> None:
        while not self._done.wait(TICK):
            try:
                self.step()
            except Exception as exc:  # the live transcript must never touch the recording
                traceback.print_exc()
                self.error = str(exc)

    def step(self, final: bool = False) -> None:
        """Takes the audio that arrived and transcribes the speech that ended. ``final``:
        the recording stopped, so the speech still open is transcribed too."""
        with self._lock:
            arrived = {name: state.pending[:] for name, state in self._tracks.items()}
            for state in self._tracks.values():
                state.pending.clear()
        for name, blocks in arrived.items():
            state = self._tracks[name]
            pieces = [state.resampler.push(block) for block in blocks]
            if final:
                pieces.append(state.resampler.flush())
            if pieces:
                state.audio = np.concatenate([state.audio, *pieces])
        for state in list(self._tracks.values()):
            if self._done.is_set() and not final:
                return  # stopping: the rest isn't needed
            self._look(state, final)

    def _look(self, state: TrackState, final: bool) -> None:
        """Transcribes this track's speech that has ended (or gone on too long), and lets
        go of the audio before it."""
        if len(state.audio) > BEHIND * RATE:
            # The GPU can't keep up (a laptop on battery): skip ahead rather than fall
            # further behind; the meeting's own transcript still has every word.
            behind = len(state.audio) - int(CAUGHT_UP * RATE)
            self._advance(state, behind)
            self.skipped += behind / RATE
        while len(state.audio):
            window = state.audio[: int(SEARCH * RATE)]
            spans = self._speech(window)
            if not spans:
                # Nothing said: keep only the end, where speech may be starting.
                self._advance(state, max(0, len(state.audio) - int(QUIET_KEPT * RATE)))
                return
            first = spans[0][0]
            # Where what was said ends: the first quiet long enough to be someone stopping,
            # else the end of the speech found so far.
            cut = next(
                (
                    end
                    for (_, end), (start, _) in zip(spans, spans[1:], strict=False)
                    if start - end >= END_SILENCE * RATE
                ),
                None,
            )
            last = cut if cut is not None else spans[-1][1]
            ended = cut is not None or len(window) - last >= END_SILENCE * RATE
            too_long = last - first >= LONGEST * RATE or len(window) >= SEARCH * RATE
            if not (ended or too_long or final):
                if first > QUIET_KEPT * RATE:  # the quiet before it isn't needed
                    self._advance(state, first - int(QUIET_KEPT * RATE))
                return
            cut_short = too_long and not ended
            if cut_short:
                last = min(last, first + int(LONGEST * RATE))
            self._say(state, first, last, pad_end=not cut_short)
            self._advance(state, last)

    def _advance(self, state: TrackState, samples: int) -> None:
        state.audio = state.audio[samples:]
        state.offset += samples

    def _say(self, state: TrackState, start: int, end: int, pad_end: bool = True) -> None:
        # Cut in the middle of someone talking, the next piece starts right there: no
        # padding at that end, or the words at the cut would be in both.
        clip = state.audio[max(0, start - PAD) : end + (PAD if pad_end else 0)]
        try:
            text = self._transcribe(clip, state.name).strip()
        except Exception as exc:  # this line is lost; the next ones aren't
            traceback.print_exc()
            self.error = str(exc)
            return
        if not text:
            return
        line = Line(0, state.name, (state.offset + start) / RATE, (state.offset + end) / RATE, text)
        with self._lock:
            line.id = self._next_id
            self._next_id += 1
            self._lines.append(line)
            del self._lines[:-LINES_KEPT]
            self._mark_echo(line)

    def _mark_echo(self, line: Line) -> None:
        """Mic lines that repeat the call at the same moment are the mic hearing the
        speakers. Whichever of the two arrives second does the check."""
        near = [
            other
            for other in self._lines[-60:]
            if other.track != line.track
            and other.start <= line.end + 1.5
            and other.end >= line.start - 1.5
        ]
        mics = [line] if line.track == "mic" else [o for o in near if o.track == "mic"]
        calls = near if line.track == "mic" else [line]
        if not mics or not calls:
            return
        theirs = [_utterance("system", o) for o in calls]
        for mine in mics:
            _, echo = transcript.drop_echo([_utterance("mic", mine)], theirs)
            mine.echo = mine.echo or bool(echo)

    # What the page reads --------------------------------------------------------------

    def lines(self, after: int = 0) -> list[dict]:
        with self._lock:
            return [
                {
                    "id": line.id,
                    "track": line.track,
                    "start": round(line.start, 1),
                    "end": round(line.end, 1),
                    "text": line.text,
                }
                for line in self._lines
                if line.id > after and not line.echo
            ]

    def text(self, me: str = "You", them: str = "Call") -> str:
        """The live transcript so far as plain lines ("[mm:ss] You: ..."), for the notes."""
        out = []
        for line in self.lines():
            minutes, seconds = divmod(int(line["start"]), 60)
            who = me if line["track"] == "mic" else them
            out.append(f"[{minutes:02d}:{seconds:02d}] {who}: {line['text']}")
        return "\n".join(out)


def _utterance(track: str, line: Line) -> transcript.Utterance:
    word = transcript.Word(line.start, line.end, line.text)
    return transcript.Utterance(track, None, line.start, line.end, [word])


def find_speech(audio: np.ndarray) -> list[tuple[int, int]]:
    """Where people speak in 16 kHz audio, as (start, end) samples (Silero VAD)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    if len(audio) < 512:
        return []
    found = get_speech_timestamps(
        audio,
        VadOptions(min_silence_duration_ms=300, speech_pad_ms=100, min_speech_duration_ms=200),
    )
    return [(span["start"], span["end"]) for span in found]


class Speech:
    """Whisper for the live transcript, loaded when the first speech needs it: the meeting
    model on the GPU, one pass without beam search (a second or less a line), or the small
    dictation model on the CPU when there's no GPU."""

    def __init__(self, language: str | None, hotwords: str | None) -> None:
        self.language = language
        self.hotwords = hotwords or None
        self.device: str | None = None
        self._model = None

    def _load(self):
        from faster_whisper import WhisperModel

        from . import asr, cuda, dictation

        if cuda.prepare():
            self.device = "cuda"
            self._model = WhisperModel(
                asr.DEFAULT_MODEL, device="cuda", compute_type="int8_float16"
            )
        else:
            self.device = "cpu"
            self._model = WhisperModel(
                dictation.model_for(self.language), device="cpu", compute_type="int8"
            )
        return self._model

    def __call__(self, audio: np.ndarray, track: str) -> str:
        model = self._model or self._load()
        segments, _ = model.transcribe(
            audio,
            language=self.language,
            beam_size=1,
            vad_filter=False,  # already cut at the speech
            condition_on_previous_text=False,
            without_timestamps=True,
            hotwords=self.hotwords,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def close(self) -> None:
        """Frees the GPU for the meeting's own transcript, which starts next."""
        self._model = None
        gc.collect()
