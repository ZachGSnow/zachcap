"""Brief voices: someone heard only for a moment, whom ZachCap can't name (background chatter, a
stray word), isn't listed as one of a meeting's speakers.

A voice is left out when nobody is known to be it, it has less of the meeting's talk than the
share picked in Settings, and it talked less than a quarter as long as the most talkative voice
(so nobody is left out of a meeting where many people each say a little). It has no lane, no talk
time and no conversation numbers, and you aren't asked who it was. What it said stays in the
transcript, as Unknown. Lines with no voice at all aren't listed either, by the same rule. It can
be undone: turning the setting off brings the voices back, and naming one brings that one back.

While a meeting is processed, a line of theirs that is plainly a piece of somebody's sentence
("Good" just before Dana's "morning.") goes to that person.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
import time
import traceback
from collections.abc import Callable, Collection

from .store import CALL_AUDIO, Store

SETTING = "leave_out_brief"  # the percent of a meeting's talk a voice is left out under
CHOICES = (0, 2, 5, 10)  # 0: none is
DEFAULT = 5
APPLIED = "leave_out_brief_applied"  # the percent the meetings here were last gone through with

LEFT_OUT = "left_out"  # the status of a voice that is
NOBODY_KNOWN = ("unknown", "minor", LEFT_OUT)  # the statuses of a voice that can be
PROCESSED = ("ready", "needs_names")  # the statuses of a meeting that has its transcript

# How long each voice of a meeting talked, in hundredths of a second: lines have times with two
# decimals, so these sums are exact. None: the lines with no voice.
Talk = dict[int | None, int]


def percent(store: Store) -> int:
    """The share of a meeting's talk, in percent, that a voice is left out under."""
    saved = store.setting(SETTING)
    return next((choice for choice in CHOICES if str(choice) == saved), DEFAULT)


def _heard(store: Store, meeting_id: int) -> list[sqlite3.Row]:
    """Each voice that has lines in a meeting, its status, and how long it talked."""
    return store.db.execute(
        "SELECT said.voice, said.long, ms.status FROM (SELECT s.meeting_speaker_id AS voice, "
        "SUM(ROUND((s.end - s.start) * 100)) AS long FROM segments s WHERE s.meeting_id = ? "
        "GROUP BY s.meeting_speaker_id) said "
        "LEFT JOIN meeting_speakers ms ON ms.id = said.voice",
        (meeting_id,),
    ).fetchall()


def talk(store: Store, meeting_id: int) -> Talk:
    return {row["voice"]: int(row["long"]) for row in _heard(store, meeting_id)}


def talk_of(rows: list[tuple]) -> Talk:
    """The same from lines that aren't stored yet (see pipeline.merge_same_person)."""
    said: Talk = {}
    for voice, _track, start, end, *_rest in rows:
        said[voice] = said.get(voice, 0) + round((end - start) * 100)
    return said


def is_brief(voice: int | None, said: Talk, share: int) -> bool:
    """Whether a voice (None: the lines with no voice) has less than ``share`` percent of the
    talk, and less than a quarter of what the most talkative voice has."""
    mine = said.get(voice, 0)
    most = max((long for other, long in said.items() if other is not None), default=0)
    return mine * 100 < share * sum(said.values()) and mine * 4 < most


def to_leave_out(voice: sqlite3.Row, said: Talk, share: int) -> bool:
    """Whether a voice is one to leave out. "Call audio" never is: that is everyone on a call
    whose speakers couldn't be separated."""
    nobody = voice["status"] in NOBODY_KNOWN and voice["label"] != CALL_AUDIO
    return nobody and is_brief(voice["id"], said, share)


def left_out(store: Store, meeting_id: int, said: Talk) -> set[int]:
    """The voices to leave out of a meeting, going by how long each talked."""
    share = percent(store)
    voices = store.speakers(meeting_id)
    return {voice["id"] for voice in voices if to_leave_out(voice, said, share)}


def mark(store: Store, meeting_id: int) -> bool:
    """Marks the voices to leave out of a meeting, and brings back those that aren't to be.
    Returns whether any changed."""
    return _write(store, _changes(store, meeting_id))


def _changes(store: Store, meeting_id: int) -> list[tuple[sqlite3.Row, str]]:
    """The voices that should have another status than they have, each with that one."""
    said, share = talk(store, meeting_id), percent(store)
    changes = []
    for voice in store.speakers(meeting_id):
        if to_leave_out(voice, said, share):
            status = LEFT_OUT
        elif voice["status"] == LEFT_OUT:
            status = _brought_back(voice)
        else:
            continue
        if status != voice["status"]:
            changes.append((voice, status))
    return changes


def _brought_back(voice: sqlite3.Row) -> str:
    """The status that processing gave a voice nobody is known to be."""
    from .pipeline import MIN_SPEAKER_SECONDS

    enough = (voice["speech_seconds"] or 0) >= MIN_SPEAKER_SECONDS
    return "unknown" if enough and voice["embedding"] is not None else "minor"


def _write(store: Store, changes: list[tuple[sqlite3.Row, str]]) -> bool:
    for voice, status in changes:
        store.set_speaker_person(voice["id"], voice["person_id"], status)
    return bool(changes)


# Meetings that are processed already ------------------------------------------------------------


def apply(store: Store, meeting_id: int) -> bool:
    """Has a processed meeting follow the setting as it is now. Returns whether anything changed.
    Any other meeting is left alone: processing it sees to that."""
    # Voices have no index by meeting: with room to keep them all, one meeting after another
    # looks through them quickly.
    store.db.execute("PRAGMA cache_size = -65536")
    if not _due(store, meeting_id):  # looked at first: mostly nothing is, so no lock is taken
        return False
    with store.transaction():
        if not store.db.in_transaction:
            store.db.execute("BEGIN IMMEDIATE")  # the write lock first, so what's read stays so
        changed = _write(store, _due(store, meeting_id))
        if changed:
            store.refresh_status(meeting_id)
    return changed


def _due(store: Store, meeting_id: int) -> list[tuple[sqlite3.Row, str]]:
    meeting = store.meeting(meeting_id)
    if meeting is None or meeting["status"] not in PROCESSED:
        return []
    return _changes(store, meeting_id)


def pending(store: Store) -> bool:
    """Whether the meetings here don't all follow the setting yet: it was changed, they are from
    before ZachCap had it, or going through them was cut off."""
    return store.setting(APPLIED) != str(percent(store))


def apply_all(store: Store) -> list[int]:
    """Has every processed meeting follow the setting, each in a transaction of its own. Returns
    the ones that changed. When the library stays busy, the rest is left for the next start."""
    share = percent(store)
    changed = []
    try:
        _patiently(store, lambda: store.delete_setting(APPLIED))  # cut off, it's all done again
        for meeting in store.meetings_with_status(*PROCESSED):
            if _patiently(store, lambda meeting=meeting: apply(store, meeting["id"])):
                changed.append(meeting["id"])
        _patiently(store, lambda: store.set_setting(APPLIED, str(share)))
    except sqlite3.OperationalError:
        traceback.print_exc()
    return changed


def _patiently(
    store: Store, write: Callable[[], object], tries: int = 3, wait: float = 0.2
) -> object:
    """Writes, and tries again when another thread or program is writing to the library (which
    each try waits for some seconds anyway)."""
    for left in reversed(range(tries)):
        try:
            with store.transaction():  # nothing of a failed try stays open
                return write()
        except sqlite3.OperationalError:
            if not left:
                raise
            time.sleep(wait)


# Who is listed ----------------------------------------------------------------------------------


def unlisted(store: Store, meeting_id: int) -> set[int | None]:
    """The voices that aren't listed as a meeting's speakers: the ones left out, and None when
    its lines with no voice are as brief."""
    heard = _heard(store, meeting_id)
    said = {row["voice"]: int(row["long"]) for row in heard}
    out = {row["voice"] for row in heard if row["status"] == LEFT_OUT}
    return out | {None} if is_brief(None, said, percent(store)) else out


def listed_lines(store: Store, meeting_id: int) -> list[sqlite3.Row]:
    """What the speakers listed said."""
    out = unlisted(store, meeting_id)
    return [line for line in store.transcript(meeting_id) if line["meeting_speaker_id"] not in out]


# Pieces of sentences ----------------------------------------------------------------------------

NEAR = 1.0  # seconds, at most, between two pieces of a sentence
# The end of a sentence (an ellipsis counts, and so do the Chinese and Japanese marks), with the
# quotes it closes.
ENDED = re.compile(
    r"[.!?\u2026\u3002\uff01\uff0e\uff1f\uff61][\"'\u2019\u201d\u00bb\u300d\u300f)\]]*\s*$"
)


def whole_sentences(rows: list[tuple], out: Collection[int | None]) -> list[tuple]:
    """Lines (as pipeline.merge_same_person takes them) with each one by the voices ``out`` that
    is plainly a piece of the sentence beside it, on the same track, given to the voice that says
    the rest. Any other stays as it is: "Yes." between two sentences of Dana's may be somebody's
    answer."""
    tracks: dict[str, list[int]] = {}
    for at, row in enumerate(rows):
        tracks.setdefault(row[1], []).append(at)
    # Each line that is one sentence with the one before or after it: (the gap, that one).
    beside: dict[int, list[tuple[float, int]]] = {}
    for places in tracks.values():
        for first, then in itertools.pairwise(places):
            gap = _gap(rows[first], rows[then])
            if gap is not None:
                beside.setdefault(first, []).append((gap, then))
                beside.setdefault(then, []).append((gap, first))
    voices = [row[0] for row in rows]
    pieces = [at for at in beside if voices[at] in out and _has_case(rows[at][4])]
    while pieces:
        given = {}
        for at in pieces:
            near = [(gap, voices[other]) for gap, other in beside[at] if voices[other] not in out]
            if near:
                given[at] = min(near, key=lambda found: found[0])[1]
        if not given:
            break
        for at, voice in given.items():
            voices[at] = voice
        pieces = [at for at in pieces if at not in given]  # some may be beside these
    return [(voice, *row[1:]) for voice, row in zip(voices, rows, strict=True)]


def _gap(first: tuple, then: tuple) -> float | None:
    """The seconds between two lines that are one sentence in two pieces: the first doesn't end
    it, and the other goes on in lowercase. None when they aren't."""
    gap = round(then[2] - first[3], 2)
    start = next((char for char in then[4] if char.isalnum()), "")
    return gap if gap <= NEAR and start.islower() and not ENDED.search(first[4]) else None


def _has_case(text: str) -> bool:
    """Whether a text's first letter is of a script with capitals. In one without (Chinese), the
    start of a sentence can't be told."""
    letter = next((char for char in text if char.isalpha()), "")
    return letter.lower() != letter.upper()
