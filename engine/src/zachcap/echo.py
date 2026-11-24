"""Telling the speakers heard through the mic from you, by how loud the two tracks are.

Everything here is RMS per 10 ms of 16 kHz audio, on the recording's timeline: the mic's
over some stretch, and the call's over the same stretch and up to ECHO_DELAY before it,
since the speakers reach the mic that late at most.
"""

from __future__ import annotations

from collections import deque

import numpy as np

RATE = 16000
HOP = RATE // 100  # the samples in 10 ms: a frame
ECHO_DELAY = 0.25  # the speakers reach the mic at most this late (the audio buffers)
LATE = round(ECHO_DELAY * 100)  # ... in frames
ECHO_LIKE = 0.7  # a mic stretch's loudness following the call's this closely: the speakers
# A loudness that varies less than this (over its mean) is a tone's or a hum's, which nothing
# can be told to follow: speech's varies more, even as echo in as much noise (0.1 and up).
FLAT = 0.1
# Your own voice can follow the call's by chance for a few syllables: 0.6 s of it reached 0.7
# in 8% of tries and 0.8 in 3%, 0.8 s reached 0.7 in 2%, and 1.5 s in none of 500. So a
# stretch under SHORT seconds has to follow it more closely, and one under LONG has to be as
# quiet as the speakers are in this mic too (Speakers).
ECHO_LIKE_SHORT = 0.8
SHORT = 0.8
LONG = 1.5
# Over the speakers' usual level in the mic by 4 dB: your voice over them. Short stretches of
# theirs were within 3.3 dB of it in 95% of tries (two voices, a small speaker, a room).
LOUDER = 1.6
PLAYING_DBFS = -70.0  # the call plays something (capture.CALL_SPEECH_DBFS: a quiet call too)
NOISE_BELOW = 26.0  # dB below the loudness of what it plays, the call has only noise
CALL_PAUSE = 0.3  # the call has stopped playing once it's quiet this long (words have gaps)
PIECE = 0.3  # a mic stretch is cut into pieces no shorter than this to be judged


def loudness(audio: np.ndarray) -> np.ndarray:
    """RMS per 10 ms of 16 kHz audio (whole frames only)."""
    frames = np.asarray(audio[: len(audio) // HOP * HOP], np.float32).reshape(-1, HOP)
    return np.sqrt(np.einsum("ij,ij->i", frames, frames) / HOP)


class Loudness:
    """A track's loudness on the recording's timeline, for its last ``seconds``: the call's,
    which mic stretches are compared with. NaN where nothing arrived (audio let go when the
    live transcript couldn't keep up)."""

    def __init__(self, seconds: float) -> None:
        self.most = int(seconds * 100)
        self.first = 0  # the timeline frame (10 ms) of values[0]
        self.values = np.zeros(0, np.float32)
        self._rest = np.zeros(0, np.float32)  # the samples of a frame not whole yet
        self._next: int | None = None  # the timeline sample the next audio should start at

    def add(self, audio: np.ndarray, at: int) -> None:
        """16 kHz audio starting at timeline sample ``at``."""
        if at != self._next:
            # The first audio, or the first after a gap: frames from the next 10 ms on.
            skip = -at % HOP
            audio, at = audio[skip:], at + skip
            self._rest = np.zeros(0, np.float32)
            frame, end = at // HOP, self.first + len(self.values)
            if not len(self.values) or frame < end:
                self.first, self.values = frame, np.zeros(0, np.float32)
            else:
                gap = np.full(frame - end, np.nan, np.float32)
                self.values = np.concatenate([self.values, gap])
        self._next = at + len(audio)
        rest = np.concatenate([self._rest, np.asarray(audio, np.float32)])
        self.values = np.concatenate([self.values, loudness(rest)])
        self._rest = rest[len(rest) // HOP * HOP :]
        excess = len(self.values) - self.most
        if excess > 0:
            self.values = self.values[excess:]
            self.first += excess

    def span(self, start: int, end: int) -> np.ndarray:
        """Frames ``start`` to ``end`` (exclusive), NaN for any not known."""
        out = np.full(end - start, np.nan, np.float32)
        low, high = max(start, self.first), min(end, self.first + len(self.values))
        if low < high:
            out[low - start : high - start] = self.values[low - self.first : high - self.first]
        return out


def measure(mic: np.ndarray, call: np.ndarray) -> float:
    """How closely a mic stretch's loudness follows the call's, allowing for the speakers'
    delay into the mic: the best correlation over the delays the call allows (``call`` starts
    up to ECHO_DELAY earlier, NaN where it isn't known). Near 1 for the speakers heard through
    the mic; low for someone speaking, alone or over the call; 0 with nothing to compare (the
    call quiet)."""
    delays = len(call) - len(mic)
    best = 0.0
    for delay in range(delays + 1):
        theirs = call[delays - delay : delays - delay + len(mic)]
        known = ~np.isnan(theirs)
        if known.sum() < max(10, len(mic) // 2):
            continue
        mine, theirs = mic[known], theirs[known]
        if any(track.std() <= FLAT * track.mean() for track in (mine, theirs)):
            continue
        best = max(best, float(np.corrcoef(mine, theirs)[0, 1]))
    return best


def call_playing(call: np.ndarray, frames: int) -> bool:
    """Whether the call played something during most of a mic stretch ``frames`` long (its
    sound could reach the mic up to ECHO_DELAY later). ``call`` as for measure."""
    if frames <= 0:
        return False
    on = np.nan_to_num(call, nan=0.0) > 10 ** (PLAYING_DBFS / 20)
    window = len(call) - frames + 1
    heard = np.lib.stride_tricks.sliding_window_view(on, window)[:frames].any(axis=1)
    return bool(heard.mean() >= 0.5)


def level(loudness_: np.ndarray) -> float:
    """How loud a stretch gets: its speech, not the gaps in it."""
    known = loudness_[~np.isnan(loudness_)]
    return float(np.percentile(known, 90)) if known.size else 0.0


class Speakers:
    """Tells which mic stretches are the speakers, learning how loud they are in this mic from
    the long ones (nothing to learn with a headset: your short replies over the call are then
    always yours)."""

    def __init__(self) -> None:
        self._leaks: deque[float] = deque(maxlen=9)  # the mic's level over the call's

    def heard(self, mic: np.ndarray, call: np.ndarray) -> bool:
        """Whether a mic stretch is the speakers: its loudness follows the call's closely
        while that plays, and, for one under LONG, it's no louder than they have been."""
        if not call_playing(call, len(mic)):
            return False
        seconds = len(mic) / 100
        if measure(mic, call) < (ECHO_LIKE if seconds >= SHORT else ECHO_LIKE_SHORT):
            return False
        leak = level(mic) / max(level(call), 1e-9)
        if seconds >= LONG:
            self._leaks.append(leak)
            return True
        return len(self._leaks) > 1 and leak <= LOUDER * float(np.median(self._leaks))


def call_turns(call: np.ndarray) -> list[tuple[int, int]]:
    """When the call plays, as (start, end) frames: whatever is louder than a quiet call's
    silence, and than the noise under its speech, the gaps between words included. A frame
    not known is as the last one known before it."""
    floor = 10 ** (PLAYING_DBFS / 20)
    known = ~np.isnan(call)
    level = np.nan_to_num(call, nan=0.0)
    heard = level[level > floor]
    if not heard.size:
        return []
    limit = max(floor, float(np.percentile(heard, 90)) * 10 ** (-NOISE_BELOW / 20))
    on = (level > limit)[np.maximum.accumulate(np.where(known, np.arange(len(call)), 0))]
    edges = np.flatnonzero(np.diff(on, prepend=False, append=False)).tolist()
    turns: list[tuple[int, int]] = []
    for start, end in zip(edges[::2], edges[1::2], strict=True):
        if turns and start - turns[-1][1] < CALL_PAUSE * 100:
            turns[-1] = (turns[-1][0], end)
        else:
            turns.append((start, end))
    return turns


def cuts(mic: np.ndarray, call: np.ndarray) -> list[int]:
    """Where to cut a mic stretch so that what it heard while the call played is judged apart
    from the rest (frames of ``mic``): near each moment the call started or stopped playing,
    at the mic's quietest while the speakers' sound could still be on its way. No piece comes
    out shorter than PIECE."""
    shortest = round(PIECE * 100)
    smooth = np.convolve(mic, np.ones(5) / 5, mode="same")
    found: list[int] = []
    for turn in call_turns(call):
        # A moment of the call is LATE frames earlier in the mic's numbering, and sounds in
        # the mic up to LATE later than that. None outside the stretch.
        edges = [
            edge - LATE + int(np.argmin(smooth[edge - LATE : edge + 1]))
            for edge in turn
            if 0 < edge - LATE < len(mic)
        ]
        if len(edges) == 2 and edges[1] - edges[0] < shortest:
            continue  # a word or a click: too short to judge on its own
        for edge in edges:
            if edge - (found[-1] if found else 0) >= shortest <= len(mic) - edge:
                found.append(edge)
    return found
