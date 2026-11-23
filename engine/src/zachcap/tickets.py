"""Ticket numbers: the service ticket a meeting was about.

You describe your ticket numbers in Settings, as patterns: # is a digit, ? a letter, and
anything else stands for itself ("#######" for a 7-digit call number, "INC-####"). ZachCap
looks for them in the meeting's title, your notes, the text of its snapshots and the
transcript. One found in what you wrote or captured (the title, a note, a snapshot) is filled
in on its own when it's the only one; numbers only heard in the call are offered on the
meeting's page, those said near "ticket" or "case" first. The ticket then goes with the
meeting: time entries, ticket notes, exports, search, Ask and the MCP server.
"""

from __future__ import annotations

import re
from collections import Counter

SETTING = "ticket_patterns"
MAX_PATTERNS = 5
MAX_LENGTH = 30  # characters in one pattern
MIN_PLACES = 4  # digits or letters a pattern must have: "##" would match half the numbers said
MAX_TICKET = 40
# Words near a number that make it likely a ticket (said within a few words before it).
CONTEXT = re.compile(
    r"\b(ticket|case|call|incident|request|work order|order|issue|sr|number|no)\b[\s#:.,-]*$",
    re.IGNORECASE,
)
STRONG = ("title", "notes", "snapshots")


def patterns(text: str | None) -> list[re.Pattern]:
    """The patterns written in Settings (comma-separated), ready to search with. Raises
    ValueError, with a reason to show, for one that can't be used."""
    compiled = []
    written = [part.strip() for part in (text or "").split(",") if part.strip()]
    if len(written) > MAX_PATTERNS:
        raise ValueError(f"At most {MAX_PATTERNS} patterns")
    for pattern in written:
        if len(pattern) > MAX_LENGTH:
            raise ValueError(f'"{pattern}" is too long: at most {MAX_LENGTH} characters')
        if sum(char in "#?" for char in pattern) < MIN_PLACES:
            raise ValueError(
                f'"{pattern}" needs at least {MIN_PLACES} digits (#) or letters (?), or it '
                "would match almost any number"
            )
        body = "".join(
            r"\d" if char == "#" else "[A-Za-z]" if char == "?" else re.escape(char)
            for char in pattern
        )
        # Not part of a longer word or number: "#######" doesn't take 7 digits of 10.
        compiled.append(re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE))
    return compiled


def normalized(ticket: str) -> str:
    return ticket.strip().upper()


def _found(text: str, compiled: list[re.Pattern]) -> list[tuple[str, int]]:
    """Every ticket number in ``text``, with where it starts."""
    return [
        (normalized(match.group()), match.start())
        for pattern in compiled
        for match in pattern.finditer(text)
    ]


SPOKEN_DIGITS = re.compile(r"\d+(?:[ ,.-]\d+)+")


def _heard(text: str, compiled: list[re.Pattern]) -> list[tuple[str, int]]:
    """Ticket numbers in a transcript line, including ones Whisper wrote in groups
    ("19 24 086", "1,924,086") when the digits together fit a pattern."""
    found = _found(text, compiled)
    for group in SPOKEN_DIGITS.finditer(text):
        joined = re.sub(r"\D", "", group.group())
        for pattern in compiled:
            if pattern.fullmatch(joined):
                found.append((normalized(joined), group.start()))
    return found


def candidates(store, meeting_id: int, heard: bool = True) -> list[dict]:
    """The ticket numbers in a meeting, best first: {ticket, where (title, notes, snapshots,
    transcript), count}. Empty when no patterns are set. ``heard`` False leaves out the
    transcript."""
    compiled = patterns(store.setting(SETTING))
    meeting = store.meeting(meeting_id)
    if not compiled or meeting is None:
        return []
    where: dict[str, set[str]] = {}
    count: Counter[str] = Counter()
    near_words: Counter[str] = Counter()

    def add(found: list[tuple[str, int]], source: str, text: str) -> None:
        for ticket, at in found:
            where.setdefault(ticket, set()).add(source)
            count[ticket] += 1
            if CONTEXT.search(text[max(0, at - 30) : at]):
                near_words[ticket] += 1

    add(_found(meeting["title"], compiled), "title", meeting["title"])
    for note in store.notes(meeting_id):
        source = "snapshots" if note["kind"] == "snapshot" else "notes"
        add(_found(note["text"] or "", compiled), source, note["text"] or "")
    for line in store.transcript(meeting_id) if heard else []:
        add(_heard(line["text"], compiled), "transcript", line["text"])

    def rank(ticket: str) -> tuple:
        strong = any(source in STRONG for source in where[ticket])
        return (not strong, -near_words[ticket], -count[ticket], ticket)

    return [
        {"ticket": ticket, "where": sorted(where[ticket]), "count": count[ticket]}
        for ticket in sorted(where, key=rank)
    ]


def fill(store, meeting_id: int) -> str | None:
    """Fills in the meeting's ticket when it has none and exactly one ticket number is in
    what you wrote or captured (its title, notes or snapshots); one only heard is left for
    you to choose. Returns the ticket filled in, if any."""
    meeting = store.meeting(meeting_id)
    if meeting is None or meeting["ticket"]:
        return None
    strong = candidates(store, meeting_id, heard=False)
    if len(strong) != 1:
        return None
    store.update_meeting(meeting_id, ticket=strong[0]["ticket"])
    return strong[0]["ticket"]


def checked(ticket: str) -> str:
    """A ticket typed on a meeting's page, tidied. Raises ValueError if it can't be one."""
    ticket = " ".join(ticket.split())
    if len(ticket) > MAX_TICKET:
        raise ValueError(f"A ticket is at most {MAX_TICKET} characters")
    return normalized(ticket) if ticket else ""
