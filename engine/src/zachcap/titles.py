"""What ZachCap calls a recording when it begins ("Teams call Nov 26 09:00", "Meeting Nov 26
09:00"), before you or a summary give it a name. A meeting still called that takes the title of
its summary (summarize.keep)."""

from __future__ import annotations

import re
from datetime import datetime

MOST = 80  # characters of a title taken from a summary
MADE_UP = re.compile(r"(?:Meeting|.{1,60} call) [A-Z][a-z]{2} \d{2} \d{2}:\d{2}")


def made_up(prefix: str, when: datetime | None = None) -> str:
    """A recording's first title: "Meeting" or "<call app> call", then when it began."""
    return f"{prefix} {(when or datetime.now()):%b %d %H:%M}"


def is_made_up(title: str | None) -> bool:
    return bool(title) and MADE_UP.fullmatch(title) is not None


def tidy(title) -> str:
    """A title from a summary as a meeting can have it: one line, at most MOST characters."""
    words = " ".join(str(title or "").split())
    if len(words) > MOST:
        words = words[:MOST].rsplit(" ", 1)[0] or words[:MOST]
    return words.strip(" .")
