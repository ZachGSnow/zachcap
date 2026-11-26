"""The call as it happens: a rough transcript while a recording runs.

The recorder hands each track's audio over as it writes it (capture.ChunkWriter). A worker
thread turns it into 16 kHz, finds where each person stopped talking (Silero VAD, the filter
Whisper itself uses) and transcribes each stretch of speech as soon as it ends, so a line
shows a second or two after it was said. Your microphone's lines are yours; the call's are
the others'. The speech models run in a child process (live_child).

The mic can hear the speakers (echo), and none of that may show as something you said, while
everything you did say must. So a mic stretch is cut where the call starts or stops playing
(you answer right after them, and they right after you), and each piece is judged by itself:
one whose loudness follows the call's at the same moments, allowing the speakers' delay into
the mic, is left out before it's transcribed (echo.py). Some laptops' audio chips cancel most
of the echo, and what's left doesn't follow the call closely; Whisper hears stock phrases in
it ("Thank you."). So while the call plays, a mic line that both Whisper and Silero doubt is
speech is left out too: only real speech over the call gets through, and with the call quiet
every word said into the mic does. A mic line that repeats what the call said meanwhile is
left out as well, unless you started it first.

This is for glancing at during the call: the live panel, its notes, being asked something.
The transcript kept with the meeting is still made after the call by the full pipeline.
Everything here stays in memory, until the next recording starts.

None of it may put the recording at risk. The recorder's threads only queue audio here,
never waiting on the worker, and at most BEHIND seconds of a track wait (the oldest goes
when the worker can't keep up); whatever goes wrong is caught and shown on the panel, the
speech models' process crashing or hanging included; the worker needs nothing but numpy, so
it imports nothing while a recording runs; and meetings being processed come first on the
GPU (GpuTurns).
"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from math import gcd

import numpy as np

from . import asr, cpu, dictation, echo, live_child, transcript

SETTING = "live_transcript"  # "1": a live transcript during recordings; unset: see api.live_on
GPU_SETTING = "live_gpu"  # "1": the last look found a CUDA GPU (GpuCheck), until the next one
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
REDACT_PAD = 1.0  # seconds either side of a redacted moment whose lines go as well
GPU_WAIT = 30.0  # a meeting waits this long at most for the live transcript to let go
LINGER = 5.0  # ... whose line under way has this long to come out
ANSWER = 120.0  # the speech child has this long to answer, or it hangs
FIRST = 120.0  # ... and this much more while it starts, and while its model loads
RESTARTS = 5  # times it's started again in one recording after crashing, hanging or failing
NONSENSE = "an answer that makes no sense"
HISTORY = BEHIND + SEARCH  # seconds of the call's loudness kept to compare the mic with
OWN_VOICE = 10.0  # dB: a piece quieter than your voice beside it by this isn't your voice


@dataclass
class Line:
    id: int
    track: str  # "mic" (you) or "system" (the others on the call)
    start: float  # seconds into the recording
    end: float
    text: str
    gone: bool = False  # left out: the mic hearing the speakers, or redacted


def low_pass(up: int, down: int) -> np.ndarray:
    """The filter scipy.signal.resample_poly uses: a Kaiser-windowed sinc."""
    half = 10 * max(up, down)
    taps = np.sinc(np.arange(-half, half + 1) / max(up, down)) * np.kaiser(2 * half + 1, 5.0)
    return (taps * up / taps.sum()).astype(np.float32)


def resample(audio: np.ndarray, up: int, down: int, taps: np.ndarray) -> np.ndarray:
    """Audio at up/down times its rate, as scipy.signal.resample_poly makes it. In numpy alone:
    importing scipy takes most of a second and holds up the recorder's threads meanwhile."""
    half, width = len(taps) // 2, (len(taps) - 1) // up + 1
    at = np.arange(-(-len(audio) * up // down)) * down  # each output sample, at up times the rate
    first = -((half - at) // up)  # the first audio sample its filter reaches...
    over = first * up - at + half  # ... and how far past the filter's start that one is
    reach = half // up + 1
    windows = np.lib.stride_tricks.sliding_window_view(np.pad(audio, (reach, width)), width)
    index = 2 * half - np.arange(up)[:, None] - up * np.arange(width)
    table = np.where(index >= 0, taps[index], 0)
    return np.einsum("ij,ij->i", windows[first + reach], table[over])


class Resampler:
    """Turns blocks at a device's rate into 16 kHz with no seams where blocks meet: each
    piece is resampled with some audio either side of it, and only its own part kept."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        common = gcd(rate, RATE)
        self.up, self.down = RATE // common, rate // common
        self._taps = low_pass(self.up, self.down)
        # Native samples held back for the right edge: what the filter reaches, and some.
        self._right = max(64, len(self._taps) // 2 // self.up + 1)
        # Left context: a whole number of ``down`` steps, so every piece's output starts on
        # the same grid as resampling the whole recording at once would.
        self._left = self.down * max(1, -(-512 // self.down))
        self._held = np.zeros(0, np.float32)  # native audio whose output isn't out yet
        self._context = np.zeros(0, np.float32)  # native audio already turned, for the left edge

    def push(self, block: np.ndarray) -> np.ndarray:
        if self.rate == RATE:
            return np.asarray(block, np.float32)
        self._held = np.concatenate([self._held, np.asarray(block, np.float32)])
        ready = len(self._held) - self._right
        ready -= ready % self.down  # whole output samples only
        if ready <= 0:
            return np.zeros(0, np.float32)
        return self._turn(ready)

    def flush(self) -> np.ndarray:
        if self.rate == RATE or not len(self._held):
            return np.zeros(0, np.float32)
        return self._turn(len(self._held), final=True)

    def _turn(self, count: int, final: bool = False) -> np.ndarray:
        window = np.concatenate([self._context, self._held])
        out = resample(window, self.up, self.down, self._taps)
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


@dataclass
class Piece:
    """Part of a stretch of speech, in samples of the recording's timeline."""

    start: int
    end: int
    playing: bool = False  # in the mic, while the call played: it may be the speakers
    speakers: bool = False  # it's the speakers: not transcribed
    level: float = 0.0  # how loud it gets (RMS)


class GpuTurns:
    """The GPU, shared by the meetings being processed (the worker in api.py) and the live
    transcript, with one model on it at a time: a 4 GB GPU can't hold both. Meetings come
    first: while one waits or is processed, the live transcript lets go of its model, and it
    takes the GPU again once none is left. With no GPU it's the processor they take turns at:
    a meeting uses every core, so the live transcript waits then too (it holds no turn)."""

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


class GpuCheck:
    """Whether a CUDA GPU is usable, looked up once: the live transcript runs on it, and is on
    by default only then (see api.py). The look takes seconds where there's an NVIDIA driver
    (live_child.gpu_usable), so the engine starts it as it starts, and nothing waits for it
    but a live transcript's own worker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._usable: bool | None = None

    def look(self) -> bool:
        """Whether there's one, waiting for the look if it's under way."""
        with self._lock:
            if self._usable is None:
                try:
                    self._usable = live_child.gpu_usable()
                except Exception:  # a broken CUDA install, or driver: no GPU, then
                    traceback.print_exc()
                    self._usable = False
            return self._usable

    def known(self) -> bool | None:
        """What the look found, without waiting: None until it's done."""
        return self._usable


class NotNow(Exception):
    """The speech models had to go before they answered (a meeting is to be processed, or the
    recording stopped): the audio waits."""


class Live:
    """A live transcript of the recording running now. ``feed`` takes audio from the
    recorder's writer threads; ``start`` runs the worker; ``lines`` is what the page reads.

    ``transcribe(audio, track)`` turns a stretch of 16 kHz speech into text (a Heard, or a
    plain str), and ``speech(audio)`` finds the speech in it as (start, end) sample pairs (its
    ``find``, unless given): tests pretend. ``transcribe`` may have a ``ready()`` (False: not
    now, the audio waits), ``failed`` (why there will be no more lines), a ``stop()`` (not to
    go on past the line it's on) and a ``close()``, which the worker calls as it ends."""

    def __init__(
        self,
        transcribe: Callable[[np.ndarray, str], str],
        speech: Callable[[np.ndarray], list[tuple[int, int]]] | None = None,
    ) -> None:
        self._transcribe = transcribe
        self._speech = speech or transcribe.find
        self._feeding = threading.Lock()  # the audio arriving: the recorder's threads
        self._arriving: dict[str, Arriving] = {}
        self._lock = threading.Lock()  # the lines
        self._tracks: dict[str, TrackState] = {}  # the worker's own
        self._call = echo.Loudness(HISTORY)  # the call's, to tell the speakers in the mic
        self._speakers = echo.Speakers()
        self._lines: list[Line] = []
        self._next_id = 1
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.error: str | None = None  # what goes wrong, for the page: until a line comes out
        self.waiting = False  # for a meeting being processed
        # Seconds of each track let go before the worker took them (feed), and skipped to
        # catch up (the worker).
        self._dropped: dict[str, float] = {}
        self._behind: dict[str, float] = {}

    @property
    def skipped(self) -> float:
        """Seconds of the call not transcribed, to keep up with it: the track's that lost the
        most, as the two mostly lose the same ones."""
        tracks = {*self._dropped, *self._behind}
        return max(
            (self._dropped.get(name, 0.0) + self._behind.get(name, 0.0) for name in tracks),
            default=0.0,
        )

    @property
    def on(self) -> bool:
        """Running, or about to: not stopped."""
        return not self._done.is_set()

    @property
    def missing(self) -> str | None:
        """The speech model it couldn't load: to be downloaded once the call is over."""
        return getattr(self._transcribe, "missing", None)

    # From the recorder --------------------------------------------------------------

    def feed(self, track: str, block: np.ndarray, rate: int, frame: int) -> None:
        """Audio as the recorder writes it: ``frame`` is where the block starts on the
        recording's timeline, at ``rate``. Called from the writer threads, so it only queues
        the audio, and never waits on the worker: at most BEHIND seconds of a track wait,
        and the oldest goes first. Once stopped (or failed for good), nothing is kept."""
        if self._done.is_set() or self._closed:
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
                lost = self._dropped.get(track, 0.0)
                self._dropped[track] = lost + len(gone) / arriving.rate

    # The worker -----------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="live-transcript", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ends the worker after the line it's on; it frees the speech model as it goes.
        Doesn't wait for it: called with the recording's lock held."""
        self._done.set()
        stop = getattr(self._transcribe, "stop", None)
        if stop is not None:
            stop()
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
                except NotNow:  # the audio waits
                    self.waiting = True
                except Exception as exc:  # the live transcript must never touch the recording
                    self._went_wrong(exc)
                if getattr(self._transcribe, "failed", None) is not None:
                    self.error, self.waiting = self._transcribe.failed, False
                    return  # for good: the recording goes on without it
        finally:
            self._close()

    def _close(self) -> None:
        """Frees the speech model (on the worker's thread, which alone uses it), and lets go
        of the audio."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self._transcribe, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                traceback.print_exc()
        with self._feeding:
            self._arriving.clear()
        self._tracks.clear()

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
        """Adds what arrived to the track's 16 kHz audio, on the recording's timeline; the
        call's loudness is kept for the echo check as well."""
        pieces = [state.audio]
        at = state.offset + len(state.audio)  # where the next piece goes on the timeline
        for frame, block in blocks:
            if state.next_frame is None or frame != state.next_frame:
                # The first audio of the track, or the first after some was let go (feed):
                # it starts over there, without what was waiting from before the gap.
                if state.next_frame is not None:
                    self._skip(state, sum(len(piece) for piece in pieces))
                state.resampler = Resampler(state.resampler.rate)
                state.offset = at = round(frame * RATE / state.resampler.rate)
                pieces = []
            pieces.append(state.resampler.push(block))
            state.next_frame = frame + len(block)
            if state.name == "system":
                self._call.add(pieces[-1], at)
            at += len(pieces[-1])
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
            self._skip(state, behind)
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
            pieces = self._pieces(state, spans, first, last)
            for piece in pieces:
                start, end = piece.start - state.offset, piece.end - state.offset
                if not piece.speakers:
                    whole = piece is pieces[-1] and not cut_short
                    self._say(state, start, end, pad_end=whole, while_call=piece.playing)
                self._advance(state, end)

    def _pieces(
        self, state: TrackState, spans: list[tuple[int, int]], first: int, last: int
    ) -> list[Piece]:
        """A stretch of the speech found (``spans``) as it's transcribed. The call's is whole.
        A mic stretch is cut where the call started or stopped playing (echo.cuts) and each
        piece judged on its own: the speakers heard through the mic are left out, and what
        you said right before or after them isn't. Pieces that are both yours are joined
        again, so a word the other side puts in doesn't split your sentence."""
        stretch = Piece(state.offset + first, state.offset + last)
        hop = echo.HOP
        low, high = -(-stretch.start // hop), stretch.end // hop  # its whole 10 ms frames
        if state.name != "mic" or high - low < 10:  # under 0.1 s: nothing to tell
            return [stretch]
        mic = echo.loudness(state.audio[low * hop - state.offset : high * hop - state.offset])
        call = self._call.span(low - echo.LATE, high)
        edges = [0, *echo.cuts(mic, call), len(mic)]
        pieces: list[Piece] = []
        for a, b in zip(edges, edges[1:], strict=False):
            piece = Piece((low + a) * hop, (low + b) * hop)
            spoken = [
                state.offset + start
                for start, end in spans
                if state.offset + start < piece.end and state.offset + end > piece.start
            ]
            before = pieces[-1] if pieces else None
            if not spoken and before:  # nothing said in it
                before.end = piece.end
                continue
            piece.playing = echo.call_playing(call[a : b + echo.LATE], b - a)
            piece.speakers = self._speakers.heard(mic[a:b], call[a : b + echo.LATE])
            piece.level = echo.level(mic[a:b])
            if before and _both_yours(before, piece):
                start = before.start // hop - low
                before.playing = echo.call_playing(call[start : b + echo.LATE], b - start)
                before.end, before.level = piece.end, max(before.level, piece.level)
            else:
                piece.start = max([piece.start, *spoken[:1]])  # cut a moment before it, maybe
                pieces.append(piece)
        pieces[0].start, pieces[-1].end = stretch.start, stretch.end
        return pieces

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

    def _skip(self, state: TrackState, samples: int) -> None:
        self._behind[state.name] = self._behind.get(state.name, 0.0) + samples / RATE

    def _say(
        self,
        state: TrackState,
        start: int,
        end: int,
        pad_end: bool = True,
        while_call: bool = False,
    ) -> None:
        # Cut in the middle of someone talking, the next piece starts right there: no
        # padding at that end, or the words at the cut would be in both.
        clip = state.audio[max(0, start - PAD) : end + (PAD if pad_end else 0)]
        try:
            heard = self._transcribe(clip, state.name)
        except NotNow:
            raise
        except Exception as exc:  # this line is lost; the next ones aren't
            self._went_wrong(exc)
            return
        self.error = None  # it works (again)
        text = str(heard).strip()
        if not text:
            return
        if getattr(heard, "doubtful", False) and (while_call or state.name == "system"):
            # Neither Whisper nor Silero takes it for speech (Heard). In the mic while the
            # call plays, it's what's left of the speakers once the audio chip cancelled the
            # echo; on the call, music or noise. Real speech over the call convinces them,
            # and with the call quiet nothing in the mic can be echo, so every word said
            # into it is kept then.
            return
        line = Line(0, state.name, (state.offset + start) / RATE, (state.offset + end) / RATE, text)
        with self._lock:
            line.id = self._next_id
            self._next_id += 1
            self._lines.append(line)
            del self._lines[:-LINES_KEPT]
            self._mark_echo(line)

    def _mark_echo(self, line: Line) -> None:
        """Mic lines that repeat what the call said meanwhile are the mic hearing the
        speakers: only while the call played, and never one you started first. Whichever of
        the two lines arrives second does the check."""
        recent = self._lines[-60:]
        for mine in [line] if line.track == "mic" else [o for o in recent if o.track == "mic"]:
            theirs = [
                other
                for other in recent
                if other.track == "system"
                and other.start <= mine.end
                and other.end + echo.ECHO_DELAY >= mine.start
            ]
            if line not in (mine, *theirs) or not theirs or not self._call_played(mine):
                continue
            if mine.start >= min(other.start for other in theirs) - transcript.ECHO_EARLY:
                said = " ".join(other.text for other in theirs)
                mine.gone = mine.gone or transcript.repeats(mine.text, said)

    def _call_played(self, line: Line) -> bool:
        """Whether the call played during most of a line."""
        low, high = round(line.start * 100), round(line.end * 100)
        return echo.call_playing(self._call.span(low - echo.LATE, high), high - low)

    # What the page reads --------------------------------------------------------------

    def lines(self, after: int = 0) -> list[dict]:
        """The lines after ``after`` (an id), in the order they were said: a line comes when
        its speaker stops, so one that started earlier can come later."""
        with self._lock:
            found = [line for line in self._lines if line.id > after and not line.gone]
        return [
            {
                "id": line.id,
                "track": line.track,
                "start": round(line.start, 1),
                "end": round(line.end, 1),
                "text": line.text,
            }
            for line in sorted(found, key=lambda line: (line.start, line.id))
        ]

    def dropped(self, upto: int) -> list[int]:
        """The lines up to ``upto`` left out. One can be found to be echo after the panel read
        it (the call's line came after the mic's), or be redacted: the panel takes it away."""
        with self._lock:
            return [line.id for line in self._lines if line.gone and line.id <= upto]

    def forget(self, start: float, end: float) -> None:
        """Lets go of what was said from ``start`` to ``end`` (seconds), redacted from the
        meeting, and a little either side: a line's times aren't exact."""
        with self._lock:
            for line in self._lines:
                if line.start < end + REDACT_PAD and line.end > start - REDACT_PAD:
                    line.gone, line.text = True, ""

    def text(self, me: str = "You", them: str = "Call") -> str:
        """The live transcript so far as plain lines ("[mm:ss] You: ..."), for the notes."""
        return as_text(self.lines(), me, them)


def as_text(lines: list[dict], me: str = "You", them: str = "Call") -> str:
    """Live lines (Live.lines) as plain lines, "[mm:ss] You: ...", the call's as ``them``."""
    out = []
    for line in lines:
        minutes, seconds = divmod(int(line["start"]), 60)
        who = me if line["track"] == "mic" else them
        out.append(f"[{minutes:02d}:{seconds:02d}] {who}: {line['text']}")
    return "\n".join(out)


def _both_yours(before: Piece, piece: Piece) -> bool:
    """Whether two pieces of a mic stretch, one after the other, are both your voice. One
    while the call played that's much quieter than the one beside it isn't: it's the
    speakers, though not following the call closely enough to be told by that."""
    if before.speakers or piece.speakers:
        return False
    if before.playing == piece.playing:
        return True
    quiet, over = (piece, before) if before.playing else (before, piece)
    return over.level > quiet.level * 10 ** (-OWN_VOICE / 20)


class Heard(str):
    """The text Speech made of a stretch, knowing whether it's doubtful that it was speech at
    all (``doubtful``, see live_child.DOUBT_LOGPROB); a transcriber may return a plain str."""

    doubtful = False


class Speech:
    """The speech models for the live transcript, in a child process (live_child) that's
    started when there's audio to look at: the meeting model on the GPU, one pass without beam
    search (a second or less a line), or the small dictation model on the CPU when there's no
    GPU. Used by the live worker's thread only (but for stop).

    While a meeting is being processed there's no child (ending it is what frees the GPU's
    memory) and it waits (``ready()`` is False); it's started again after. On the GPU it's only
    there while this holds its turn (``gpu``). A child that crashes, hangs or fails a line is
    ended and started again, RESTARTS times at most (then ``failed``). It only loads a model
    already on this PC; ``missing`` names one that didn't load, to be downloaded after the
    call. Each line says whether it's doubtful that it was speech at all (Heard)."""

    def __init__(
        self,
        language: str | None,
        hotwords: str | None,
        gpu: GpuTurns | None = None,
        check: GpuCheck | None = None,
        start: Callable[..., live_child.Child] | None = None,
    ) -> None:
        self.language = language
        self.hotwords = hotwords or None
        self.gpu = gpu
        self.device: str | None = None
        self.failed: str | None = None  # why there will be no more lines: not tried again
        self.missing: str | None = None  # the model, when it didn't load
        self._check = check
        self._start = start or live_child.Child
        self._child: live_child.Child | None = None
        self._asked: set[str] = set()  # what this child has been asked before
        self._stops = 0  # times a child stopped, hung or failed
        self._turn = False  # holds the GPU's turn
        self._stopping = threading.Event()
        self._closed = False

    def ready(self) -> bool:
        if self._closed or self.failed is not None:
            return False
        if self.gpu is not None and self.gpu.wanted():
            self._let_go()  # a meeting waits for the GPU, or uses every core without one
            return False
        if self.device is None:
            self.device = "cuda" if self._check is not None and self._check.look() else "cpu"
        if self.device == "cpu" or self.gpu is None:
            return True
        if not self._turn:
            self._turn = self.gpu.take()
        return self._turn

    def find(self, audio: np.ndarray) -> list[tuple[int, int]]:
        """Where people speak in 16 kHz audio, as (start, end) samples."""
        answer = self._ask("find", audio)
        if "spans" not in answer:
            raise self._stopped(answer.get("error", NONSENSE))
        return [(int(start), int(end)) for start, end in answer["spans"]]

    def __call__(self, audio: np.ndarray, track: str) -> Heard:
        answer = self._ask("hear", audio, **self._settings())
        if "load" in answer:
            self.missing = self._settings()["model"]
            self.failed = (
                "its speech model isn't on this PC yet; it's downloaded after this call, "
                "for the next one"
                if answer["load"] == "missing"
                else f"its speech model didn't load ({answer.get('error', '')}); what's missing "
                "of it is downloaded after this call"
            )
            raise RuntimeError(self.failed)
        if "text" not in answer:  # after a GPU error every line fails: a fresh one has the next
            raise self._stopped(answer.get("error", NONSENSE))
        heard = Heard(answer["text"])
        heard.doubtful = bool(answer.get("doubtful"))
        return heard

    def stop(self) -> None:
        """Not to go on past the line it's on. From any thread."""
        self._stopping.set()

    def close(self) -> None:
        """Frees the GPU for the meeting's own transcript, which starts next. Never starts a
        child again after this."""
        self._closed = True
        self._let_go()

    def _settings(self) -> dict:
        """The model, and how it's used, for the child."""
        if self.device == "cuda":
            model = asr.DEFAULT_MODEL
            options = {"device": "cuda", "compute_type": "int8_float16"}
        else:
            # Half the cores: the call app and the recorder run on the rest, and the call
            # matters more than its live transcript.
            model = dictation.model_for(self.language)
            threads = max(1, cpu.physical_cores() // 2)
            options = {"device": "cpu", "compute_type": "int8", "cpu_threads": threads}
        return {
            "model": model,
            "options": options,
            "language": self.language,
            "hotwords": self.hotwords,
        }

    def _ask(self, do: str, audio: np.ndarray, **more) -> dict:
        """What the child (started now if there's none) answers. Raises NotNow when it has to
        go before it does, and RuntimeError when it crashed, hung or couldn't be started."""
        if self._closed or self.failed is not None:
            raise RuntimeError(self.failed or "The live transcript has stopped")
        if self.device is None and not self.ready():
            raise NotNow
        try:
            if self._child is None:
                self._child = self._start(*(["cuda"] if self.device == "cuda" else []))
                self._asked.clear()
            limit = ANSWER + (0 if do in self._asked else FIRST)
            self._asked.add(do)
            self._child.send(do, audio, **more)
            return self._answer(limit)
        except (live_child.Gone, OSError) as exc:
            raise self._stopped(str(exc)) from exc

    def _answer(self, limit: float) -> dict:
        asked, hurry = time.monotonic(), None
        while (answer := self._child.answer(0.1)) is None:
            now = time.monotonic()
            if self._stopping.is_set() or (self.gpu is not None and self.gpu.wanted()):
                hurry = hurry or now
                if now - hurry > LINGER:
                    self._let_go()
                    raise NotNow
            if now - asked > limit:
                raise live_child.Gone(f"no answer for {limit:.0f} seconds")
        return answer

    def _stopped(self, why: str) -> RuntimeError:
        """The child crashed, hung or failed: it's ended, and the next thing to do starts
        another, a few times. What to say about it."""
        if self._child is not None:
            self._child.end()
            self._child = None
        self._stops += 1
        if self._stops <= RESTARTS:
            return RuntimeError(f"its speech model stopped ({why}) and is starting again")
        self.failed = (
            f"its speech model stopped {self._stops} times ({why}), so there are no more lines "
            "for this call"
        )
        return RuntimeError(self.failed)

    def _let_go(self) -> None:
        """Ends the child, and gives the GPU's turn back once it's gone."""
        if self._child is not None:
            self._child.end()
            self._child = None
        if self._turn:
            self._turn = False
            self.gpu.give_back()
