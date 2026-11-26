"""When a task is due, as a day: worked out from the words that were said for it ("Tuesday",
"by the end of the month", "December 3") and the day of the meeting. None when the words don't
name one day ("next week", "soon", "Tuesday or Wednesday"), or could name two: "next Tuesday"
(the coming one, or the one after?) and a weekday said on that weekday (today, or in a week?).
A wrong day would be worse than none."""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

_WEEKDAYS = (
    ("monday", "mon"),
    ("tuesday", "tue", "tues"),
    ("wednesday", "wed"),
    ("thursday", "thu", "thur", "thurs"),
    ("friday", "fri"),
    ("saturday", "sat"),
    ("sunday", "sun"),
)
WEEKDAYS = {name: n for n, names in enumerate(_WEEKDAYS) for name in names}
_MONTHS = (
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
    ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
    ("october", "oct"), ("november", "nov"), ("december", "dec"),
)  # fmt: skip
MONTHS = {name: n for n, names in enumerate(_MONTHS, 1) for name in names}
NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
RECENT = 30  # days: a date this recently past was spoken of, not set for next year

# When on the day: it is still that day.
_TIME = re.compile(
    r"\b(at\s+)?\d{1,2}(:\d{2})?\s*(am|pm)\b|\bat\s+\d{1,2}(:\d{2})?\b|"
    r"\b(in\s+the\s+)?(morning|afternoon|evening)\b|\b(at\s+)?(noon|midday|lunch(time)?)\b|"
    r"\bfirst\s+thing\b"
)
# What means the day it was said, on its own.
_THAT_DAY = re.compile(r"\b(end\s+of\s+(the\s+)?day|eod|cob|close\s+of\s+business)\b")
_BEFORE = re.compile(r"^(by|on|before|until|till|til|due|no\s+later\s+than)(\s+|$)")
_ORDINAL = r"(\d{1,2})(st|nd|rd|th)?"


def day(words: str | None, said_on: date) -> date | None:
    """The day a task is due, from the words said for it on ``said_on``; None if not clear."""
    text = " ".join(re.sub(r"[,.;:!?](?!\d)", " ", (words or "").casefold()).split())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    that_day = bool(_THAT_DAY.search(text))
    text = _THAT_DAY.sub(" ", _TIME.sub(" ", text))
    text = re.sub(r"\b(at\s+the\s+latest|the\s+latest|latest)\b", " ", text)
    text = " ".join(word for word in text.split() if word != "the")
    while _BEFORE.match(text):
        text = _BEFORE.sub("", text)
    text = " ".join(text.split())
    if not text:
        return said_on if that_day else None
    if text in ("today", "tonight", "this"):  # "this" was "this morning", "this evening"...
        return said_on
    if text == "tomorrow":
        return said_on + timedelta(days=1)
    if text == "day after tomorrow":
        return said_on + timedelta(days=2)
    if found := re.fullmatch(r"(this\s+)?(\w+)", text):
        if found.group(2) in WEEKDAYS:
            ahead = (WEEKDAYS[found.group(2)] - said_on.weekday()) % 7
            return said_on + timedelta(days=ahead) if ahead else None
    if text in ("end of week", "end of this week"):
        return said_on + timedelta(days=4 - said_on.weekday()) if said_on.weekday() <= 4 else None
    if text in ("end of month", "end of this month"):
        last = calendar.monthrange(said_on.year, said_on.month)[1]
        return said_on.replace(day=last)
    if found := re.fullmatch(r"in\s+(\d{1,2}|\w+)\s+(days?|weeks?)", text):
        count = int(found.group(1)) if found.group(1).isdigit() else NUMBERS.get(found.group(1))
        if count is not None:
            return said_on + timedelta(days=count * (7 if found.group(2).startswith("week") else 1))
    month_day = re.fullmatch(rf"(\w+)\s+{_ORDINAL}", text)
    day_month = re.fullmatch(rf"{_ORDINAL}\s+(of\s+)?(\w+)", text)
    if month_day and month_day.group(1) in MONTHS:
        return _next(said_on, MONTHS[month_day.group(1)], int(month_day.group(2)))
    if day_month and day_month.group(4) in MONTHS:
        return _next(said_on, MONTHS[day_month.group(4)], int(day_month.group(1)))
    if found := re.fullmatch(rf"{_ORDINAL}", text):
        if found.group(2):  # "the 3rd": of this month, or the next if it has passed
            month = said_on.month if int(found.group(1)) >= said_on.day else said_on.month % 12 + 1
            year = said_on.year + (month < said_on.month)
            return _valid(year, month, int(found.group(1)))
    return None


def _next(said_on: date, month: int, number: int) -> date | None:
    """A month's day, this year; next year once it has passed, unless only just."""
    found = _valid(said_on.year, month, number)
    if found is None or found >= said_on:
        return found
    if (said_on - found).days <= RECENT:
        return None  # it was spoken of, not set
    return _valid(said_on.year + 1, month, number)


def _valid(year: int, month: int, number: int) -> date | None:
    try:
        return date(year, month, number)
    except ValueError:
        return None
