"""Where your name was said: other people's lines that mention you.

Your name, its first word, and any other names you answer to (Settings, "Also answer to")
are matched as whole words, ignoring case. A name that is also an everyday word (Will,
Mark, May...) only counts when capitalized, so "I will mark it" doesn't count as a mention.
Lines you said yourself are left out. One compiled pattern per call, so hour-long
meetings stay cheap; the meeting list counts with the full-text index first.
"""

from __future__ import annotations

import re
import sqlite3

from .store import SPEAKER_NAME, Store

ALSO_ANSWER_TO = "also_answer_to"
MAX_ALIASES = 10
MAX_ALIAS_LENGTH = 40
SNIPPET = 160

# Never names: the default "You", and words a meeting is full of.
NOT_NAMES = {"you", "me", "i", "we", "us", "he", "she", "him", "her", "they", "them", "it"}

# First names that are also everyday words: these must be capitalized to count.
EVERYDAY = {
    "ace", "amber", "angel", "april", "art", "autumn", "bill", "bob", "buck", "bud", "cash",
    "chase", "cliff", "crystal", "daisy", "dale", "dawn", "drew", "duke", "earl", "faith",
    "frank", "gene", "glen", "grace", "harmony", "holly", "honor", "hope", "hunter", "iris",
    "ivy", "jack", "jay", "joy", "june", "king", "lane", "lily", "major", "mark", "may",
    "melody", "page", "pat", "pearl", "penny", "ray", "reed", "rich", "rob", "robin", "rod",
    "rose", "ruby", "sandy", "sky", "sue", "summer", "wade", "will",
}  # fmt: skip


def aliases(text: str | None) -> list[str]:
    """The "Also answer to" setting: names separated by commas, cleaned up."""
    names = []
    for part in (text or "").split(","):
        name = " ".join(part.split())[:MAX_ALIAS_LENGTH]
        if _usable(name) and name not in names:
            names.append(name)
    return names[:MAX_ALIASES]


def your_names(store: Store) -> list[str]:
    """Every name that means you: your name, its first word, and the ones you answer to."""
    full = " ".join(store.me()["name"].split())
    names = [full]
    if " " in full:
        names.append(full.split(" ")[0])
    names += aliases(store.setting(ALSO_ANSWER_TO))
    seen: set[str] = set()
    kept = []
    for name in names:
        if _usable(name) and name.lower() not in seen:
            seen.add(name.lower())
            kept.append(name)
    return kept


def _usable(name: str) -> bool:
    """At least two characters, a letter or digit among them, and not a pronoun."""
    return len(name) >= 2 and re.search(r"\w", name) is not None and name.lower() not in NOT_NAMES


def pattern(names: list[str]) -> re.Pattern | None:
    """One pattern for all the names, longest first (a full name wins over its first word)."""
    if not names:
        return None
    parts = []
    for name in sorted(names, key=len, reverse=True):
        escaped = re.escape(name).replace(r"\ ", r"\s+")
        if name.lower() in EVERYDAY:
            parts.append(escaped[0].upper() + escaped[1:] if escaped[0].isalpha() else escaped)
        else:
            parts.append(f"(?i:{escaped})")
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w)")


def snippet(text: str, match: re.Match) -> str:
    """The line, or a window of it around the name when it's long."""
    if len(text) <= SNIPPET:
        return text
    start = max(0, match.start() - 60)
    end = min(len(text), start + SNIPPET)
    return ("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def _lines_by_others(store: Store, meeting_id: int) -> list[sqlite3.Row]:
    return store.db.execute(
        f"SELECT s.id, s.start, s.text, {SPEAKER_NAME} AS speaker FROM segments s "
        "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
        "LEFT JOIN people p ON p.id = ms.person_id "
        "WHERE s.meeting_id = ? AND COALESCE(p.is_me, 0) = 0 ORDER BY s.start",
        (meeting_id,),
    ).fetchall()


def find(store: Store, meeting_id: int) -> list[dict]:
    """Lines by other people in this meeting that say one of your names."""
    compiled = pattern(your_names(store))
    if compiled is None:
        return []
    found = []
    for line in _lines_by_others(store, meeting_id):
        matches = list(compiled.finditer(line["text"]))
        if matches:
            found.append(
                {
                    "segment_id": line["id"],
                    "at": line["start"],
                    "speaker": line["speaker"],
                    "snippet": snippet(line["text"], matches[0]),
                    "names": sorted({m.group() for m in matches}),
                }
            )
    return found


def _fts_query(names: list[str]) -> str:
    """The names as full-text phrases ("zach" OR "zach snow"), quotes escaped."""
    return " OR ".join('"' + name.replace('"', '""') + '"' for name in names)


def counts(store: Store) -> dict[int, int]:
    """How many lines in each meeting mention you (for the meeting list). The full-text
    index finds the few candidate lines; the pattern then decides, as in ``find``."""
    names = your_names(store)
    compiled = pattern(names)
    if compiled is None:
        return {}
    try:
        rows = store.db.execute(
            "SELECT s.meeting_id, s.text FROM segments_fts f JOIN segments s ON s.id = f.rowid "
            "LEFT JOIN meeting_speakers ms ON ms.id = s.meeting_speaker_id "
            "LEFT JOIN people p ON p.id = ms.person_id "
            "WHERE segments_fts MATCH ? AND COALESCE(p.is_me, 0) = 0",
            (_fts_query(names),),
        ).fetchall()
    except sqlite3.OperationalError:  # a name the index can't search for: no counts
        return {}
    found: dict[int, int] = {}
    for row in rows:
        if compiled.search(row["text"]):
            found[row["meeting_id"]] = found.get(row["meeting_id"], 0) + 1
    return found
