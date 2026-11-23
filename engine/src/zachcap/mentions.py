"""Where your name was said: other people's lines that mention you.

Your name, its first word, and any other names you answer to (Settings, "Also answer to")
are matched as whole words, ignoring case, but never inside a contraction ("Don" isn't in
"don't"; "Dana's" still counts). A name that is also an everyday word (Will, Mark, May...)
only counts when capitalized, and at the start of a sentence only when a comma follows,
as when someone is addressed: "Will, can you check?" counts, "Will that work?" doesn't. A
full name made only of such words counts only capitalized ("Will Grant", not "will grant
access"). An alias in capitals (initials such as "AR") matches only in capitals. Lines you said
yourself are left out. One compiled pattern per call, so hour-long meetings stay cheap;
the meeting list counts with the full-text index first, and remembers its counts until a
line, a voice or a name changes.
"""

from __future__ import annotations

import re
import sqlite3
import threading

from .store import SPEAKER_NAME, Store

ALSO_ANSWER_TO = "also_answer_to"
MAX_ALIASES = 10
MAX_ALIAS_LENGTH = 40
SNIPPET = 160

# Never names: the default "You", and words a meeting is full of.
NOT_NAMES = {"you", "me", "i", "we", "us", "he", "she", "him", "her", "they", "them", "it"}

# First names that are also everyday words: these must be capitalized to count, and at
# the start of a sentence need a comma after them.
EVERYDAY = {
    "ace", "amber", "angel", "april", "art", "august", "autumn", "bill", "bob", "brook",
    "buck", "bud", "cash", "chase", "chuck", "cliff", "crystal", "daisy", "dale", "dawn",
    "dean", "don", "dot", "drew", "duke", "earl", "ernest", "faith", "frank", "gale", "gene",
    "glen", "grace", "grant", "gray", "grey", "guy", "harmony", "heather", "holly", "honor",
    "hope", "hunter", "iris", "ivy", "jack", "jade", "jay", "joy", "june", "king", "kit",
    "lance", "lane", "lily", "major", "mark", "max", "may", "melody", "mike", "miles",
    "nick", "olive", "page", "pat", "pearl", "penny", "ray", "reed", "rex", "rich", "rob",
    "robin", "rod", "rose", "ruby", "rusty", "sandy", "sky", "sonny", "storm", "sue",
    "summer", "sunny", "wade", "will", "win", "winter",
}  # fmt: skip
# After a name: not a contraction ("don't", "Will'll"); a possessive "'s" is fine.
NOT_CONTRACTION = r"(?!['\u2019](?:t|ll|re|ve|d|m)\b)"
SENTENCE_END = re.compile(r"[.!?][\"'\u201d)\]]*\s*$")


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


def _capitalized(escaped: str) -> str:
    return escaped[0].upper() + escaped[1:] if escaped[:1].isalpha() else escaped


def _initials(name: str) -> bool:
    """An alias in capitals ("AR", "ZS"): matched only in capitals, so "am" isn't "AM"."""
    return name.isupper() and " " not in name


def pattern(names: list[str]) -> re.Pattern | None:
    """One pattern for all the names, longest first (a full name wins over its first word)."""
    if not names:
        return None
    parts = []
    for name in sorted(names, key=len, reverse=True):
        words = name.split(" ")
        if all(word.lower() in EVERYDAY for word in words):
            # Only capitalized, word by word: "Will Grant", never "will grant access".
            parts.append(r"\s+".join(_capitalized(re.escape(word)) for word in words))
        elif _initials(name):
            parts.append(re.escape(name))
        else:
            escaped = r"\s+".join(re.escape(word) for word in words)
            parts.append(f"(?i:{escaped})")
    return re.compile(rf"(?<!\w)(?:{'|'.join(parts)})(?!\w){NOT_CONTRACTION}")


def _counts(match: re.Match, text: str) -> bool:
    """An everyday-word name starting a sentence counts only when someone is addressed:
    "Will, are you there?", not "Will that work?" or "May I share?"."""
    if match.group().lower() not in EVERYDAY:
        return True
    before = text[: match.start()]
    if before.strip() and not SENTENCE_END.search(before):
        return True  # capitalized mid-sentence: a name
    return text[match.end() :].lstrip().startswith(",")


def matches(compiled: re.Pattern, text: str) -> list[re.Match]:
    """Where a line says one of your names, by the rules above."""
    return [match for match in compiled.finditer(text) if _counts(match, text)]


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
    """Lines by other people in this meeting that say one of your names, with where in the
    line (``spans``, character offsets) so the app marks exactly what was counted."""
    compiled = pattern(your_names(store))
    if compiled is None:
        return []
    found = []
    for line in _lines_by_others(store, meeting_id):
        said = matches(compiled, line["text"])
        if said:
            found.append(
                {
                    "segment_id": line["id"],
                    "at": line["start"],
                    "speaker": line["speaker"],
                    "snippet": snippet(line["text"], said[0]),
                    "names": sorted({m.group() for m in said}),
                    "spans": [[m.start(), m.end()] for m in said],
                }
            )
    return found


def _fts_query(names: list[str]) -> str:
    """The names as full-text phrases ("zach" OR "zach snow"), quotes escaped."""
    return " OR ".join('"' + name.replace('"', '""') + '"' for name in names)


# The meeting list asks every few seconds: its counts are kept until a line, a voice or a
# name changes (the store's lines version), or the names you answer to do.
_counted: dict[tuple, dict[int, int]] = {}
_counted_lock = threading.Lock()


def counts(store: Store) -> dict[int, int]:
    """How many lines in each meeting mention you (for the meeting list). The full-text
    index finds the few candidate lines; the pattern then decides, as in ``find``."""
    names = your_names(store)
    compiled = pattern(names)
    if compiled is None:
        return {}
    version = store.lines_version()
    key = (store.path, version, tuple(names))
    with _counted_lock:
        if version is not None and key in _counted:
            return dict(_counted[key])
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
        if matches(compiled, row["text"]):
            found[row["meeting_id"]] = found.get(row["meeting_id"], 0) + 1
    if version is not None:
        with _counted_lock:
            _counted.clear()  # only the latest is ever asked for again
            _counted[key] = dict(found)
    return found
