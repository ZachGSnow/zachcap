"""Topics: words and phrases you track across your meetings.

Written in Settings one per line, a name and the words that mean it ("Renewals: renewal,
contract, quote"). Each meeting shows the topics said in it, with the lines (a filter on its
transcript marks them); the Topics page lists every topic with the meetings and clients it
came up in; and the work log notes each meeting's topics. Matched as whole words or phrases,
ignoring case, in anyone's lines. Worked out on this PC from the full-text index; nothing is
sent anywhere.
"""

from __future__ import annotations

import re
import sqlite3
import threading

from .store import SPEAKER_NAME, Store

SETTING = "topics"
MAX_TOPICS = 20
MAX_WORDS = 20
MAX_LENGTH = 40  # characters in a topic's name or one of its words
SNIPPET = 160


def parse(text: str | None) -> list[dict]:
    """The topics as written in Settings: [{"name", "words"}]. Raises ValueError, with a
    reason to show, for a line that can't be used."""
    topics: list[dict] = []
    for number, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        name, colon, said = line.partition(":")
        name = " ".join(name.split())
        words = [" ".join(word.split()) for word in said.split(",") if word.strip()]
        if not colon or not name or not words:
            raise ValueError(f"Line {number}: write the topic, a colon, then its words")
        if len(name) > MAX_LENGTH or any(len(word) > MAX_LENGTH for word in words):
            raise ValueError(f"Line {number}: names and words are at most {MAX_LENGTH} characters")
        if len(words) > MAX_WORDS:
            raise ValueError(f"Line {number}: at most {MAX_WORDS} words for a topic")
        if not all(re.search(r"\w", word) for word in words):
            raise ValueError(f"Line {number}: every word needs a letter or a digit")
        if any(topic["name"].casefold() == name.casefold() for topic in topics):
            raise ValueError(f'Line {number}: "{name}" is already a topic')
        topics.append({"name": name, "words": list(dict.fromkeys(words))})
    if len(topics) > MAX_TOPICS:
        raise ValueError(f"At most {MAX_TOPICS} topics")
    return topics


def saved(store: Store) -> list[dict]:
    try:
        return parse(store.setting(SETTING))
    except ValueError:  # saved before a stricter check: as if none were set
        return []


def _pattern(words: list[str]) -> re.Pattern:
    """The words as whole words or phrases, ignoring case; longer ones first, so "windows 10"
    wins over "windows"."""
    alternatives = sorted({re.escape(word) for word in words}, key=len, reverse=True)
    return re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.IGNORECASE)


def _snippet(text: str, start: int) -> str:
    if len(text) <= SNIPPET:
        return text
    begin = max(0, start - 60)
    end = min(len(text), begin + SNIPPET)
    return ("..." if begin else "") + text[begin:end].strip() + ("..." if end < len(text) else "")


def find(store: Store, meeting_id: int) -> list[dict]:
    """The topics said in a meeting, most said first: {"topic", "count", "lines": [{"segment_id",
    "at", "speaker", "snippet", "spans"}]}, the spans marking exactly what was counted."""
    topics = saved(store)
    if not topics:
        return []
    lines = store.db.execute(
        f"SELECT s.id, s.start, s.text, {SPEAKER_NAME} AS speaker FROM segments s "
        "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
        "LEFT JOIN people p ON p.id = ms.person_id "
        "WHERE s.meeting_id = ? ORDER BY s.start",
        (meeting_id,),
    ).fetchall()
    found = []
    for topic in topics:
        compiled = _pattern(topic["words"])
        said = []
        count = 0
        for line in lines:
            hits = list(compiled.finditer(line["text"]))
            if hits:
                count += len(hits)
                said.append(
                    {
                        "segment_id": line["id"],
                        "at": line["start"],
                        "speaker": line["speaker"],
                        "snippet": _snippet(line["text"], hits[0].start()),
                        "spans": [[hit.start(), hit.end()] for hit in hits],
                    }
                )
        if said:
            found.append({"topic": topic["name"], "count": count, "lines": said})
    return sorted(found, key=lambda topic: -topic["count"])


def _fts_query(words: list[str]) -> str:
    """The words as full-text phrases ("renewal" OR "windows 10"), quotes escaped."""
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words)


# The Topics page and the work log ask often: counts are kept until a line, a voice or a
# person changes (the store's lines version), or the topics do.
_counted: dict[tuple, dict[str, dict[int, int]]] = {}
_counted_lock = threading.Lock()


def counts(store: Store) -> dict[str, dict[int, int]]:
    """How many times each topic was said in each meeting: {topic: {meeting_id: count}}.
    The full-text index finds the candidate lines; the pattern then counts, as in ``find``."""
    topics = saved(store)
    version = store.lines_version()
    key = (store.path, version, repr(topics))
    with _counted_lock:
        if version is not None and key in _counted:
            return {name: dict(per) for name, per in _counted[key].items()}
    result: dict[str, dict[int, int]] = {}
    for topic in topics:
        per: dict[int, int] = {}
        try:
            rows = store.db.execute(
                "SELECT s.meeting_id, s.text FROM segments_fts f "
                "JOIN segments s ON s.id = f.rowid WHERE segments_fts MATCH ?",
                (_fts_query(topic["words"]),),
            ).fetchall()
        except sqlite3.OperationalError:  # words the index can't search for: none counted
            rows = []
        compiled = _pattern(topic["words"])
        for row in rows:
            hits = len(compiled.findall(row["text"]))
            if hits:
                per[row["meeting_id"]] = per.get(row["meeting_id"], 0) + hits
        result[topic["name"]] = per
    if version is not None:
        with _counted_lock:
            _counted.clear()  # only the latest is ever asked for again
            _counted[key] = result
    return {name: dict(per) for name, per in result.items()}


def overview(store: Store) -> list[dict]:
    """Every topic with how often it came up and where, newest meetings first: {"topic",
    "words", "count", "meetings": [{"id", "title", "started_at", "client", "count"}]}."""
    from . import clients

    per_topic = counts(store)
    meetings = {m["id"]: m for m in store.meetings() if m["status"] != "recording"}
    client_of = clients.of_meetings(store, list(meetings.values()))
    listed = []
    for topic in saved(store):
        per = {mid: n for mid, n in per_topic.get(topic["name"], {}).items() if mid in meetings}
        ordered = sorted(per, key=lambda mid: meetings[mid]["started_at"], reverse=True)
        listed.append(
            {
                "topic": topic["name"],
                "words": topic["words"],
                "count": sum(per.values()),
                "meetings": [
                    {
                        "id": mid,
                        "title": meetings[mid]["title"],
                        "started_at": meetings[mid]["started_at"],
                        "client": client_of[mid]["client"],
                        "count": per[mid],
                    }
                    for mid in ordered
                ],
            }
        )
    return listed


def of_meeting(store: Store, meeting_id: int) -> dict[str, int]:
    """How many times each topic was said in one meeting (only the ones said)."""
    return {name: per[meeting_id] for name, per in counts(store).items() if meeting_id in per}
