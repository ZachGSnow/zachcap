"""Privacy settings that act on data: masking contact details before text goes to Claude,
and deleting old recordings while keeping their transcripts."""

from __future__ import annotations

import re
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


def outgoing(store: Store, text: str) -> str:
    """Text about to be sent to Claude, masked if the user turned that on."""
    return mask(text) if store.setting("mask_before_claude") == "1" else text


def remove_old_audio(store: Store, now: datetime | None = None) -> list[int]:
    """Delete the recordings of meetings older than the "keep audio" setting. The transcript,
    summary, notes and voiceprints stay. Only folders ZachCap itself created are touched,
    and never a meeting being recorded or processed. Returns the meetings cleaned."""
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
        store.update_meeting(
            meeting["id"],
            audio_removed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        cleaned.append(meeting["id"])
    return cleaned
