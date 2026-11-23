"""Turn per-track words and speaker turns into one readable transcript.

Whisper gives timed words, diarization gives timed speaker turns. Each word goes
to the turn it falls in, consecutive words by the same speaker become an
utterance, and the mic and computer-audio tracks are merged by time. Mic lines
that repeat what the computer audio said at the same moment are echo and dropped.
"""

from __future__ import annotations

import bisect
import re
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


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def drop_echo(
    mic: list[Utterance], system: list[Utterance], window: float = 1.5, threshold: float = 0.6
) -> tuple[list[Utterance], list[Utterance]]:
    """Split mic utterances into (kept, echo). Echo = mostly the same words as the
    computer audio at the same time, i.e. the mic hearing the speakers."""
    kept, echo = [], []
    for utt in mic:
        mine = _tokens(utt.text)
        nearby = [s for s in system if s.start <= utt.end + window and s.end >= utt.start - window]
        theirs = [token for s in nearby for token in _tokens(s.text)]
        is_echo = bool(mine and theirs) and (
            SequenceMatcher(None, mine, theirs).ratio() >= threshold
            or _covered(mine, theirs) >= 0.8
        )
        (echo if is_echo else kept).append(utt)
    return kept, echo


def _covered(mine: list[str], theirs: list[str]) -> float:
    """Share of the mic words that appear, in order, in the computer audio."""
    matcher = SequenceMatcher(None, mine, theirs)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(mine)


def merge(*tracks: list[Utterance]) -> list[Utterance]:
    return sorted((u for track in tracks for u in track), key=lambda u: (u.start, u.track))
