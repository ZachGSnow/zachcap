"""What keeps data private: meetings kept from Claude, masking contact details before text
goes to Claude, and deleting old recordings while keeping their transcripts."""

from __future__ import annotations

import re
import shutil
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from .store import Store

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
# A run of 13 to 19 digits, optionally split by spaces or dashes, that passes the Luhn check.
CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
# Phone numbers: 10 or 11 digits with the usual separators, or 7 digits as 555-0123.
PHONE = re.compile(
    r"(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
    r"|(?<![\w-])\d{3}-\d{4}(?![\w-])"
)
# ".redacting": a rewrite a crash interrupted, holding audio like the file it replaces.
AUDIO_SUFFIXES = {".flac", ".wav", ".mp3", ".m4a", ".ogg", ".redacting"}


# A private meeting: none of it is sent to Claude. It isn't summarized, asked about or
# written from, and the library Ask and MCP clients read (assistant.py) doesn't have it.
PRIVATE = (
    "This meeting is private: nothing in it is sent to Claude. A model on this PC can write "
    "from it (Settings > Summaries), or let Claude read it in the meeting's menu."
)


def is_private(store: Store, meeting) -> bool:
    return bool(store.column(meeting, "private"))


def refuse_private(store: Store, meeting) -> None:
    """Called before anything of one meeting is sent to Claude, each time: it can be made
    private (or deleted) while Claude is asked."""
    if meeting is None:
        raise RuntimeError("This meeting isn't there any more.")
    if is_private(store, meeting):
        raise RuntimeError(PRIVATE)


# When each meeting was last redacted, as far as this run of the engine knows.
_redacted: dict[int, float] = {}
_started = time.time()


def redacted(meeting_id: int) -> None:
    _redacted[meeting_id] = time.time()


def may_send(store: Store, meeting_ids: Iterable[int], since: float) -> bool:
    """Whether what was found in these meetings, at ``since`` or later, may go to Claude now:
    none was made private, deleted or redacted meanwhile."""
    ids = set(meeting_ids)
    if not ids:
        return True
    return (
        since >= _started  # before that, there's no telling what was redacted
        and ids.isdisjoint(store.private_meetings())
        and all(store.meeting(i) is not None and _redacted.get(i, 0.0) < since for i in ids)
    )


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def mask(text: str) -> str:
    """Emails, card numbers and phone numbers replaced with placeholders."""
    text = EMAIL.sub("[email]", text)
    text = CARD.sub(
        lambda m: "[card number]" if _luhn(re.sub(r"\D", "", m.group())) else m.group(), text
    )
    return PHONE.sub("[phone]", text)


MASKS = ("[email]", "[phone]", "[card number]")  # what mask() writes instead


def masked(text: str | None) -> bool:
    """Whether a text has words that mask() took out."""
    return any(mask_ in (text or "") for mask_ in MASKS)


def outgoing(store: Store, text: str) -> str:
    """Text about to be sent to Claude, masked if the user turned that on."""
    return mask(text) if store.setting("mask_before_claude") == "1" else text


def remove_old_audio(store: Store, now: datetime | None = None) -> list[int]:
    """Delete the recordings of meetings older than the "keep audio" setting, and their
    snapshot pictures. The transcript, summary, notes (a snapshot's text included) and
    voiceprints stay. Only folders ZachCap itself created are touched, and never a meeting
    being recorded or processed. Returns the meetings cleaned."""
    days = int(store.setting("keep_audio_days", "0") or 0)
    if days <= 0:
        return []
    cutoff = (now or datetime.now().astimezone()) - timedelta(days=days)
    root = paths.meetings_dir().resolve()
    cleaned = []
    for meeting in store.meetings_with_status("ready", "needs_names", "failed"):
        if meeting["audio_removed_at"]:
            continue
        started = datetime.fromisoformat(meeting["started_at"])
        if started.tzinfo is None:
            started = started.astimezone()
        folder = Path(meeting["folder"]).resolve()
        if started > cutoff or not folder.is_relative_to(root) or not folder.is_dir():
            continue
        for file in folder.iterdir():
            if file.suffix.lower() in AUDIO_SUFFIXES and file.is_file():
                file.unlink(missing_ok=True)
        shutil.rmtree(folder / "snapshots", ignore_errors=True)  # pictures; the text stays
        store.update_meeting(
            meeting["id"],
            audio_removed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        cleaned.append(meeting["id"])
    return cleaned
