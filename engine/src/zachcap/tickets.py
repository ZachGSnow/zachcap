"""Ticket numbers: the service ticket a meeting was about.

You describe your ticket numbers in Settings, as patterns: # is a digit, ? a letter, and
anything else stands for itself ("#######" for a 7-digit call number, "INC-####"). ZachCap
looks for them in the meeting's title, your notes, the text of its snapshots and the
transcript. One found in what you wrote or captured (the title, a note, a snapshot) is filled
in on its own when it's the only one; numbers only heard in the call are offered on the
meeting's page, those said a few words after "ticket" or "case" first. The ticket then goes
with the meeting: time entries, ticket notes, exports, search, Ask and the MCP server.

A ticket you set or take off is yours: it's never changed on its own. One filled in on its
own is checked again when what it came from changes (a note edited or deleted, a stretch
redacted, new patterns), and goes if it's no longer there.
"""

from __future__ import annotations

import re
from collections import Counter

SETTING = "ticket_patterns"
MAX_PATTERNS = 5
MAX_LENGTH = 30  # characters in one pattern
MIN_PLACES = 4  # digits or letters a pattern must have: "##" would match half the numbers said
MAX_TICKET = 40
AUTO, YOURS = "auto", "user"  # meetings.ticket_source: filled in on its own, or by you
STRONG = ("title", "notes", "snapshots")
# Said a few words before a number: likely a ticket ("the ticket number is 1924086"). Weaker
# words also come before phone and order numbers, so they only count right before it.
NEAR = re.compile(r"\b(ticket|case|incident|request|sr)\b\W*(?:\w+\W+){0,3}$", re.IGNORECASE)
JUST_BEFORE = re.compile(r"\b(call|number|no|order|issue)\b[\s#:.,-]*$", re.IGNORECASE)


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


def _saved_patterns(store) -> list[re.Pattern]:
    try:
        return patterns(store.setting(SETTING))
    except ValueError:  # saved before a stricter check: as if none were set
        return []


def normalized(ticket: str) -> str:
    return ticket.strip().upper()


def _found(text: str, compiled: list[re.Pattern]) -> list[tuple[str, int]]:
    """Every ticket number in ``text``, with where it starts. Where patterns overlap
    ("INC-#######" and "#######"), only the longest match counts."""
    spans = sorted(
        ((match.start(), match.end()) for pattern in compiled for match in pattern.finditer(text)),
        key=lambda span: (span[0], -span[1]),
    )
    found: list[tuple[str, int]] = []
    reach = -1
    for start, end in spans:
        if end <= reach:
            continue  # inside a longer match
        found.append((normalized(text[start:end]), start))
        reach = end
    return found


SPOKEN_DIGITS = re.compile(r"\d+(?:[ ,.-]\d+)+")
PHONE = re.compile(r"\(?\d{3}\)?[ .-]\d{3}[.-]\d{4}|\d{3}[.-]\d{4}")
MAX_GROUPS = 6


def _heard(text: str, compiled: list[re.Pattern]) -> list[tuple[str, int]]:
    """Ticket numbers in a transcript line, including ones Whisper wrote in groups
    ("19 24 086", "1,924,086"): any run of neighbouring groups whose digits together fit a
    pattern. Amounts ("$1,924,086") and phone numbers ("555-1234") are left out."""
    found = _found(text, compiled)
    for run in SPOKEN_DIGITS.finditer(text):
        if text[max(0, run.start() - 1) : run.start()] in ("$", "€", "£"):
            continue
        if PHONE.fullmatch(run.group()):
            continue
        groups = re.findall(r"\d+", run.group())[:MAX_GROUPS]
        for first in range(len(groups)):
            for last in range(first + 2, len(groups) + 1):  # at least two groups
                joined = "".join(groups[first:last])
                if any(pattern.fullmatch(joined) for pattern in compiled):
                    found.append((normalized(joined), run.start()))
    return found


def candidates(store, meeting_id: int, heard: bool = True) -> list[dict]:
    """The ticket numbers in a meeting, best first: {ticket, where (title, notes, snapshots,
    transcript), count}. Empty when no patterns are set. ``heard`` False leaves out the
    transcript."""
    compiled = _saved_patterns(store)
    meeting = store.meeting(meeting_id)
    if not compiled or meeting is None:
        return []
    where: dict[str, set[str]] = {}
    count: Counter[str] = Counter()
    near: Counter[str] = Counter()

    def add(found: list[tuple[str, int]], source: str, text: str) -> None:
        for ticket, at in found:
            where.setdefault(ticket, set()).add(source)
            count[ticket] += 1
            before = text[max(0, at - 40) : at]
            near[ticket] += 2 if NEAR.search(before) else 1 if JUST_BEFORE.search(before) else 0

    add(_found(meeting["title"], compiled), "title", meeting["title"])
    for note in store.notes(meeting_id):
        # An attached file (a log) is full of numbers: what's in it is offered, not filled in.
        source = {"snapshot": "snapshots", "file": "files"}.get(note["kind"], "notes")
        add(_found(note["text"] or "", compiled), source, note["text"] or "")
    for line in store.transcript(meeting_id) if heard else []:
        add(_heard(line["text"], compiled), "transcript", line["text"])

    def rank(ticket: str) -> tuple:
        strong = any(source in STRONG for source in where[ticket])
        return (not strong, -near[ticket], -count[ticket], ticket)

    return [
        {"ticket": ticket, "where": sorted(where[ticket]), "count": count[ticket]}
        for ticket in sorted(where, key=rank)
    ]


def fill(store, meeting_id: int) -> str | None:
    """Brings the meeting's ticket up to date with what's in it: one filled in on its own
    that's no longer the only ticket number in its title, notes or snapshots goes; and with
    no ticket, exactly one there is filled in. A ticket you set (or took off) is left alone.
    Returns the ticket filled in now, if any."""
    meeting = store.meeting(meeting_id)
    if meeting is None:
        return None
    source = store.column(meeting, "ticket_source")
    if source == YOURS:
        return None
    strong = [found["ticket"] for found in candidates(store, meeting_id, heard=False)]
    if source == AUTO and meeting["ticket"]:
        if strong == [meeting["ticket"]]:
            return None
        store.drop_auto_ticket(meeting_id)  # what it came from changed
    if len(strong) != 1:
        return None
    # Only if it's still empty and undecided: a ticket set meanwhile wins.
    return strong[0] if store.fill_ticket(meeting_id, strong[0]) else None


def checked(ticket: str) -> str:
    """A ticket typed on a meeting's page, tidied. Raises ValueError if it can't be one."""
    ticket = " ".join("".join(char for char in ticket if char.isprintable()).split())
    if len(ticket) > MAX_TICKET:
        raise ValueError(f"A ticket is at most {MAX_TICKET} characters")
    if "[" in ticket or "]" in ticket:
        raise ValueError("A ticket can't have [ or ] in it")
    return normalized(ticket) if ticket else ""


def words_of(query: str) -> set[str]:
    """The words of a search, as tickets: the page sends each word as "word"* for the
    full-text search."""
    return {normalized(word) for word in re.findall(r'[^\s"*()]+', query)}
