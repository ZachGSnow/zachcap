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

None of it may put the recording at risk. The recorder's threads only queue audio here,
never waiting on the worker, and at most BEHIND seconds of a track wait (the oldest goes
when the worker can't keep up); whatever goes wrong is caught and shown on the panel; and
meetings being processed come first on the GPU (GpuTurns).
"""

from __future__ import annotations

import gc
import threading
import traceback
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from math import gcd

import numpy as np

from . import cuda, transcript

SETTING = "live_transcript"  # "1" (the default): a live transcript during recordings
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
GPU_WAIT = 30.0  # a meeting waits this long at most for the live transcript to let go


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
class Arriving:
    """A track's audio from the recorder, not taken by the worker yet: (frame, block)
    pairs at the track's own rate, ``frames`` long in all."""

    rate: int
    blocks: deque = field(default_factory=deque)
    frames: int = 0


@dataclass
class TrackState:
    name: str
    resampler: Resampler
    audio: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))  # 16 kHz
    offset: int = 0  # the recording's timeline sample where ``audio`` begins
    next_frame: int | None = None  # where the next block starts (native rate), if no gap


class GpuTurns:
    """The GPU, shared by the meetings being processed (the worker in api.py) and the live
    transcript, with one model on it at a time: a 4 GB GPU can't hold both. Meetings come
    first: while one waits or is processed, the live transcript lets go of its model, and it
    takes the GPU again once none is left."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # held by whoever has a model on the GPU now
        self._meetings = threading.Event()  # one is waiting or being processed

    def meeting_waiting(self) -> None:
        self._meetings.set()

    def meetings_done(self) -> None:
        self._meetings.clear()

    @contextmanager
    def for_meeting(self, wait: float = GPU_WAIT) -> Iterator[None]:
        """A meeting's turn. The live transcript lets go within a tick, or after the line
        it's on; past ``wait`` the meeting goes ahead anyway, so it never waits long."""
        self._meetings.set()
        taken = self._lock.acquire(timeout=wait)
        try:
            yield
        finally:
            if taken:
                self._lock.release()

    def wanted(self) -> bool:
        """Whether a meeting is waiting for the GPU, or has it."""
        return self._meetings.is_set()

    def take(self) -> bool:
        """The live transcript's turn, if no meeting wants the GPU: kept until give_back."""
        return not self._meetings.is_set() and self._lock.acquire(blocking=False)

    def give_back(self) -> None:
        self._lock.release()


class Live:
    """A live transcript of the recording running now. ``feed`` takes audio from the
    recorder's writer threads; ``start`` runs the worker; ``lines`` is what the page reads.

    ``transcribe(audio, track)`` turns a stretch of 16 kHz speech into text, and
    ``speech(audio)`` finds the speech in it as (start, end) sample pairs: given, so tests can
    pretend. ``transcribe`` may have a ``ready()`` (False: not now, the audio waits) and a
    ``close()``, which the worker calls as it ends."""

    def __init__(
        self,
        transcribe: Callable[[np.ndarray, str], str],
        speech: Callable[[np.ndarray], list[tuple[int, int]]] | None = None,
    ) -> None:
        self._transcribe = transcribe
        self._speech = speech or find_speech
        self._feeding = threading.Lock()  # the audio arriving: the recorder's threads
        self._arriving: dict[str, Arriving] = {}
        self._lock = threading.Lock()  # the lines
        self._tracks: dict[str, TrackState] = {}  # the worker's own
        self._lines: list[Line] = []
        self._next_id = 1
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.error: str | None = None  # the last thing that went wrong, for the page
        self.waiting = False  # for the GPU: a meeting is being processed
        self._dropped = 0.0  # seconds let go before the worker took them (feed)
        self._behind = 0.0  # seconds skipped to catch up (the worker)

    @property
    def skipped(self) -> float:
        """Seconds of audio not transcribed, to keep up with the call."""
        return self._dropped + self._behind

    @property
    def on(self) -> bool:
        """Running, or about to: not stopped."""
        return not self._done.is_set()

    # From the recorder --------------------------------------------------------------

    def feed(self, track: str, block: np.ndarray, rate: int, frame: int) -> None:
        """Audio as the recorder writes it: ``frame`` is where the block starts on the
        recording's timeline, at ``rate``. Called from the writer threads, so it only queues
        the audio, and never waits on the worker: at most BEHIND seconds of a track wait,
        and the oldest goes first. Once stopped, nothing is kept."""
        if self._done.is_set():
            return
        audio = np.array(block, np.float32, copy=True)
        with self._feeding:
            arriving = self._arriving.get(track)
            if arriving is None:
                arriving = self._arriving[track] = Arriving(rate)
            arriving.blocks.append((frame, audio))
            arriving.frames += len(audio)
            while arriving.frames > BEHIND * arriving.rate and len(arriving.blocks) > 1:
                _, gone = arriving.blocks.popleft()
                arriving.frames -= len(gone)
                self._dropped += len(gone) / arriving.rate

    # The worker -----------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="live-transcript", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ends the worker after the line it's on; it frees the speech model as it goes.
        Doesn't wait for it: called with the recording's lock held."""
        self._done.set()
        if self._thread is None:  # never started
            self._close()

    def join(self, timeout: float | None = None) -> bool:
        """Waits for the worker to end (tests). True once it has."""
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _run(self) -> None:
        try:
            while not self._done.wait(TICK):
                try:
                    self.step()
                except Exception as exc:  # the live transcript must never touch the recording
                    self._went_wrong(exc)
        finally:
            self._close()

    def _close(self) -> None:
        """Frees the speech model (on the worker's thread, which alone uses it)."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._transcribe, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                traceback.print_exc()

    def _went_wrong(self, exc: Exception) -> None:
        """Said on the panel; in the log once, not on every tick it happens again."""
        if str(exc) != self.error:
            traceback.print_exc()
        self.error = str(exc)

    def step(self, final: bool = False) -> None:
        """Takes the audio that arrived and transcribes the speech that ended. ``final``:
        the recording stopped, so the speech still open is transcribed too."""
        with self._feeding:
            arrived = {}
            for name, arriving in self._arriving.items():
                arrived[name] = (arriving.rate, list(arriving.blocks))
                arriving.blocks.clear()
                arriving.frames = 0
        for name, (rate, blocks) in arrived.items():
            state = self._tracks.get(name)
            if state is None:
                state = self._tracks[name] = TrackState(name, Resampler(rate))
            if blocks or final:
                self._take(state, blocks, final)
        # Asked every tick, audio or not, so the GPU goes to a meeting as soon as one waits.
        self._may_say(final)
        # The call's first: a mic line that echoes it is then left out before it's ever read.
        for state in sorted(self._tracks.values(), key=lambda state: state.name != "system"):
            self._look(state, final)

    def _take(self, state: TrackState, blocks: list, final: bool) -> None:
        """Adds what arrived to the track's 16 kHz audio, on the recording's timeline."""
        pieces = [state.audio]
        for frame, block in blocks:
            if state.next_frame is None or frame != state.next_frame:
                # The first audio of the track, or the first after some was let go (feed):
                # it starts over there, without what was waiting from before the gap.
                if state.next_frame is not None:
                    self._behind += sum(len(piece) for piece in pieces) / RATE
                state.resampler = Resampler(state.resampler.rate)
                state.offset = round(frame * RATE / state.resampler.rate)
                pieces = []
            pieces.append(state.resampler.push(block))
            state.next_frame = frame + len(block)
        if final:
            pieces.append(state.resampler.flush())
        state.audio = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)

    def _look(self, state: TrackState, final: bool) -> None:
        """Transcribes this track's speech that has ended (or gone on too long), and lets
        go of the audio before it."""
        if len(state.audio) > BEHIND * RATE:
            # The GPU can't keep up (a laptop on battery), or a meeting has it: skip ahead
            # rather than fall further behind; the meeting's own transcript has every word.
            behind = len(state.audio) - int(CAUGHT_UP * RATE)
            self._advance(state, behind)
            self._behind += behind / RATE
        while len(state.audio):
            if not self._may_say(final):
                return  # the audio waits: stopping, or the GPU is a meeting's for now
            window = state.audio[: int(SEARCH * RATE)]
            # A full window may have more audio after it (a backlog, after waiting for the
            # GPU): what it can't settle is looked at again with the audio that follows.
            full = len(window) >= SEARCH * RATE
            spans = self._speech(window)
            if not spans:
                # Nothing said: keep only the end, where speech may be starting.
                self._advance(state, max(0, len(window) - int(QUIET_KEPT * RATE)))
                if full:
                    continue
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
            too_long = last - first >= LONGEST * RATE
            if not (ended or too_long or final):
                if first > QUIET_KEPT * RATE:  # the quiet before it isn't needed
                    self._advance(state, first - int(QUIET_KEPT * RATE))
                    if full:
                        continue
                return
            cut_short = too_long and not ended
            if cut_short:
                last = min(last, first + int(LONGEST * RATE))
            self._say(state, first, last, pad_end=not cut_short)
            self._advance(state, last)

    def _may_say(self, final: bool) -> bool:
        """Whether to go on transcribing now: not once stopping (the rest isn't needed),
        and not while the speech model can't (``ready()``)."""
        if self._done.is_set() and not final:
            return False
        ready = getattr(self._transcribe, "ready", None)
        self.waiting = ready is not None and not ready()
        return not self.waiting

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
            self._went_wrong(exc)
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

    def dropped(self, upto: int) -> list[int]:
        """The lines up to ``upto`` left out as echo. One can be found to be echo after the
        panel read it (the call's line came after the mic's): the panel takes it away."""
        with self._lock:
            return [line.id for line in self._lines if line.echo and line.id <= upto]

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
    dictation model on the CPU when there's no GPU. Used by the live worker's thread only.

    On the GPU it takes its turn (``gpu``): while a meeting is being processed it lets go of
    its model and waits (``ready()`` is False), and loads it again after."""

    def __init__(
        self, language: str | None, hotwords: str | None, gpu: GpuTurns | None = None
    ) -> None:
        self.language = language
        self.hotwords = hotwords or None
        self.gpu = gpu
        self.device: str | None = None
        self._model = None
        self._turn = False  # holds the GPU's turn
        self._failed: str | None = None  # why the model didn't load: not tried again
        self._closed = False

    def ready(self) -> bool:
        if self._closed:
            return False
        if self.device is None:
            try:
                self.device = "cuda" if cuda.prepare() else "cpu"
            except Exception:  # a broken CUDA install: the CPU, then
                traceback.print_exc()
                self.device = "cpu"
        if self.device == "cpu" or self.gpu is None:
            return True
        if self.gpu.wanted():
            self._let_go()  # a meeting is waiting for the GPU
            return False
        if not self._turn:
            self._turn = self.gpu.take()
        return self._turn

    def _load(self):
        from . import asr, cpu, dictation, models

        if self._failed is not None:  # one failed load a recording, not one a line
            raise RuntimeError(self._failed)
        try:
            if self.device == "cuda":
                self._model = models.whisper(
                    asr.DEFAULT_MODEL, device="cuda", compute_type="int8_float16"
                )
            else:
                # Half the cores: the call app and the recorder run on the rest, and the call
                # matters more than its live transcript.
                self._model = models.whisper(
                    dictation.model_for(self.language),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=max(1, cpu.physical_cores() // 2),
                )
        except Exception as exc:
            self._failed = f"The live transcript's speech model didn't load: {exc}"
            raise
        return self._model

    def __call__(self, audio: np.ndarray, track: str) -> str:
        if self._closed:
            raise RuntimeError("The live transcript has stopped")
        if self.device is None and not self.ready():
            raise RuntimeError("The GPU is busy with a meeting")
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

    def _let_go(self) -> None:
        """Frees the model, and the GPU's turn with it."""
        if self._model is not None:
            self._model = None
            gc.collect()
        if self._turn:
            self._turn = False
            self.gpu.give_back()

    def close(self) -> None:
        """Frees the GPU for the meeting's own transcript, which starts next. Never loads
        again after this."""
        self._closed = True
        self._let_go()
