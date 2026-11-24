"""Turn per-track words and speaker turns into one readable transcript.

Whisper gives timed words, diarization gives timed speaker turns. Each word goes
to the turn it falls in, consecutive words by the same speaker become an
utterance, and the mic and computer-audio tracks are merged by time. What the mic heard
from the speakers (the computer audio's words, at the moment it said them) is echo and dropped.
"""

from __future__ import annotations

import bisect
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher


@dataclass
class Word:
    start: float
    end: float
    text: str
    probability: float = 1.0


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


@dataclass
class Utterance:
    track: str
    speaker: str | None
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(word.text for word in self.words).strip()

    @property
    def confidence(self) -> float:
        return sum(w.probability for w in self.words) / len(self.words) if self.words else 0.0


def assign_speakers(
    words: list[Word], turns: list[Turn], tolerance: float = 0.5
) -> list[str | None]:
    """Speaker for each word: the turn holding its midpoint, else the nearest one nearby."""
    turns = sorted(turns, key=lambda t: t.start)
    starts = [t.start for t in turns]
    speakers: list[str | None] = []
    for word in words:
        mid = (word.start + word.end) / 2
        i = bisect.bisect_right(starts, mid) - 1
        candidates = turns[max(i - 1, 0) : i + 2]
        inside = [t for t in candidates if t.start <= mid <= t.end]
        if inside:
            speakers.append(min(inside, key=lambda t: t.end - t.start).speaker)
            continue
        nearest, distance = None, tolerance
        for t in candidates:
            d = min(abs(mid - t.start), abs(mid - t.end))
            if d <= distance:
                nearest, distance = t.speaker, d
        speakers.append(nearest)
    return speakers


def build_utterances(
    track: str, words: list[Word], turns: list[Turn], max_gap: float = 1.5
) -> list[Utterance]:
    utterances: list[Utterance] = []
    for word, speaker in zip(words, assign_speakers(words, turns), strict=True):
        last = utterances[-1] if utterances else None
        if last and last.speaker == speaker and word.start - last.end <= max_gap:
            last.words.append(word)
            last.end = word.end
        else:
            utterances.append(Utterance(track, speaker, word.start, word.end, [word]))
    return utterances


# The mic hears a word from the speakers at the moment they say it: its end is at most this
# many seconds before or after the word's end on the computer audio (up to 0.2 s of delay and
# Whisper's own error: 98% of 759 words measured were from 0.16 before to 0.35 after). Ends,
# since Whisper starts a line's first word anywhere in the pause before it.
ECHO_EARLY = 0.3
ECHO_LATE = 0.6
ECHO_SHARE = 0.6  # of the words the mic heard while the computer audio spoke, this many its own


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def repeats(mine: str, theirs: str, threshold: float = 0.6) -> bool:
    """Whether what the mic heard is mostly what the computer audio said."""
    a, b = _tokens(mine), _tokens(theirs)
    return bool(a and b) and (
        SequenceMatcher(None, a, b).ratio() >= threshold or _covered(a, b) >= 0.8
    )


class Spoken:
    """What the computer audio said, to tell its words when the mic hears them from the
    speakers (see ECHO_EARLY). ``playing`` says whether it had any sound in a stretch of
    seconds, when that's known."""

    def __init__(
        self, utterances: list[Utterance], playing: Callable[[float, float], bool] | None = None
    ) -> None:
        self.words = sorted((w for u in utterances for w in u.words), key=lambda w: w.end)
        self.ends = [w.end for w in self.words]
        self.playing = playing

    def during(self, word: Word) -> bool:
        """Whether it was speaking as the mic heard a word: the word may be its echo."""
        next_ = bisect.bisect_left(self.ends, word.end - ECHO_LATE)
        if next_ == len(self.words) or self.words[next_].start > word.end + ECHO_EARLY:
            return False
        return self.playing is None or self.playing(max(word.start, word.end - 0.2), word.end)

    def said(self, word: Word) -> bool:
        """Whether it said that very word then."""
        first = bisect.bisect_left(self.ends, word.end - ECHO_LATE)
        last = bisect.bisect_right(self.ends, word.end + ECHO_EARLY)
        theirs = {token for w in self.words[first:last] for token in _tokens(w.text)}
        return any(token in theirs for token in _tokens(word.text))


def drop_echo(
    mic: list[Utterance],
    system: list[Utterance],
    playing: Callable[[float, float], bool] | None = None,
) -> tuple[list[Utterance], list[Utterance]]:
    """Split the mic's utterances into (kept, echo). Echo is the mic hearing the speakers:
    words in a row said while the computer audio spoke, most of them its own at that very
    moment. The words before and after those are yours, in the same utterance or not: an
    answer often repeats a word of the question, and they repeat yours."""
    call = Spoken(system, playing)
    kept, echo = [], []
    for utt in mic:
        parts: list[tuple[bool, list[Word]]] = []
        for during, row in itertools.groupby(utt.words, key=call.during):
            words = list(row)
            heard = during and sum(map(call.said, words)) >= ECHO_SHARE * len(words)
            if parts and parts[-1][0] == heard:
                parts[-1][1].extend(words)
            else:
                parts.append((heard, words))
        for heard, words in parts:
            part = Utterance(utt.track, utt.speaker, words[0].start, words[-1].end, words)
            (echo if heard else kept).append(part)
    return kept, echo


def _covered(mine: list[str], theirs: list[str]) -> float:
    """Share of the mic words that appear, in order, in the computer audio."""
    matcher = SequenceMatcher(None, mine, theirs)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(mine)


def merge(*tracks: list[Utterance]) -> list[Utterance]:
    return sorted((u for track in tracks for u in track), key=lambda u: (u.start, u.track))
