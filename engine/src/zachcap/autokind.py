"""The kind of meeting, picked for a summary when the kind is Automatic: first from what the
meeting itself makes clear, which is free.

A title that names one kind ("Daily standup", "1:1 Dana", "Mail outage") is that kind; a
meeting with a client (set on it, or the one its people work for) is a customer call; and one
where every line was said by you or one other person, both named, is a one-on-one. Anything
less clear is left to Claude, which reads the title and the start of the transcript
(summarize.pick_kind).
"""

from __future__ import annotations

import re

from . import brief, clients
from .store import Store

PICKED = "auto"  # meetings.template_source: the kind was picked on its own (else chosen)

# Words in a title that name a kind of meeting, as whole words in any capitals. "Stand up" as
# two words is left out: "stand up the new server" is setting one up.
TITLE_WORDS = {
    "standup": r"stand-?up|daily\s+scrum",
    "one_on_one": r"(?<![\w:])1:1(?![\w:])|1[- ]on[- ]1|one[- ]on[- ]one",
    "interview": r"interview(?:s|ing)?",
    "incident": r"incidents?|outages?|troubleshoot(?:ing)?",
}
_TITLE = {
    kind: re.compile(rf"(?<!\w)(?:{words})(?!\w)", re.IGNORECASE)
    for kind, words in TITLE_WORDS.items()
}
COUNTS = ("auto", "confirmed")  # a voice someone is, for sure: recognized, or named by you


def from_title(title: str | None) -> str | None:
    """The kind of meeting a title names, when it names exactly one."""
    named = [kind for kind, words in _TITLE.items() if words.search(title or "")]
    return named[0] if len(named) == 1 else None


def you_and_one_other(store: Store, meeting_id: int) -> bool:
    """Whether every line was said by you or by one other person, both named for sure. A
    voice not named yet (or only suggested), "Call audio" (several people not told apart) or
    lines with no voice make it unclear, unless they are too brief to be listed (brief.py)."""
    voices = {speaker["id"]: speaker for speaker in store.speakers(meeting_id)}
    people = set()
    for line in brief.listed_lines(store, meeting_id):
        voice = voices.get(line["meeting_speaker_id"])
        if voice is None or voice["person_id"] is None or voice["status"] not in COUNTS:
            return False
        people.add(voice["person_id"])
    return len(people) == 2 and store.me()["id"] in people


def clue(store: Store, meeting) -> str | None:
    """The kind of meeting when the meeting makes it clear (its title first), else None."""
    named = from_title(meeting["title"])
    if named is not None:
        return named
    if clients.of_meeting(store, meeting)["client"]:
        return "customer"
    if you_and_one_other(store, meeting["id"]):
        return "one_on_one"
    return None
