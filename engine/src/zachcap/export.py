"""Meetings as files: Markdown (summary, notes and transcript), plain text, and subtitles.

An optional export folder (an Obsidian vault, say) gets a Markdown copy of every meeting,
rewritten whenever the meeting changes. Each copy's name ends with the meeting's id, "(M12)",
so a renamed meeting replaces its old file and two meetings never share one.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from .store import Store

FORMATS = {"md": "Markdown", "txt": "Text", "srt": "Subtitles (SRT)", "vtt": "Subtitles (WebVTT)"}


def _clock(seconds: float) -> str:
    s = int(seconds)
    return (
        f"{s // 60:02d}:{s % 60:02d}"
        if s < 3600
        else f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    )


def _stamp(seconds: float, separator: str) -> str:
    """hh:mm:ss,mmm (SRT) or hh:mm:ss.mmm (WebVTT)."""
    ms = round(seconds * 1000)
    hours, minutes, secs = ms // 3_600_000, ms // 60_000 % 60, ms // 1000 % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms % 1000:03d}"


def markdown(store: Store, meeting_id: int) -> str:
    meeting = store.meeting(meeting_id)
    started = datetime.fromisoformat(meeting["started_at"])
    people = store.participants().get(meeting_id, [])
    lines = [f"# {meeting['title']}", ""]
    lines.append(
        f"{started:%A %B %d, %Y %H:%M}"
        + (f" | {round((meeting['duration'] or 0) / 60)} min" if meeting["duration"] else "")
        + (f" | {', '.join(people)}" if people else "")
    )
    summary = store.summary(meeting_id)
    if summary:
        content = summary["content"]
        lines += ["", "## Summary", "", content.get("overview", "")]
        if content.get("decisions"):
            lines += ["", "## Decisions", ""] + [f"- {d}" for d in content["decisions"]]
        items = store.action_items(meeting_id=meeting_id)
        if items:
            lines += ["", "## Action items", ""]
            for item in items:
                due = f" (due {item['due']})" if item["due"] else ""
                lines.append(
                    f"- [{'x' if item['status'] == 'done' else ' '}] **{item['owner']}**: "
                    f"{item['task']}{due}"
                )
        if content.get("open_questions"):
            lines += ["", "## Open questions", ""] + [f"- {q}" for q in content["open_questions"]]
    notes = store.notes(meeting_id)
    if notes:
        lines += ["", "## My notes", ""]
        lines += [f"- [{_clock(n['at'])}] {n['text'] or 'Bookmarked moment'}" for n in notes]
    transcript = store.transcript(meeting_id)
    if transcript:
        lines += ["", "## Transcript", ""]
        lines += [f"**[{_clock(r['start'])}] {r['speaker']}:** {r['text']}  " for r in transcript]
    return "\n".join(lines).rstrip() + "\n"


def text(store: Store, meeting_id: int) -> str:
    meeting = store.meeting(meeting_id)
    lines = [meeting["title"], ""]
    lines += [
        f"[{_clock(r['start'])}] {r['speaker']}: {r['text']}" for r in store.transcript(meeting_id)
    ]
    return "\n".join(lines) + "\n"


def subtitles(store: Store, meeting_id: int, kind: str = "srt") -> str:
    separator = "," if kind == "srt" else "."
    blocks = [] if kind == "srt" else ["WEBVTT", ""]
    for index, row in enumerate(store.transcript(meeting_id), 1):
        timing = f"{_stamp(row['start'], separator)} --> {_stamp(row['end'], separator)}"
        cue = [str(index), timing] if kind == "srt" else [timing]
        blocks += [*cue, f"{row['speaker']}: {row['text']}", ""]
    return "\n".join(blocks)


def render(store: Store, meeting_id: int, fmt: str) -> str:
    if fmt == "md":
        return markdown(store, meeting_id)
    if fmt == "txt":
        return text(store, meeting_id)
    if fmt in ("srt", "vtt"):
        return subtitles(store, meeting_id, fmt)
    raise ValueError(f"Unknown format {fmt!r}")


def file_name(store: Store, meeting_id: int, fmt: str, with_id: bool = False) -> str:
    """A safe file name: date, time and title (and the meeting's id, for the export folder)."""
    meeting = store.meeting(meeting_id)
    started = datetime.fromisoformat(meeting["started_at"])
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", meeting["title"]).strip(" .") or "Meeting"
    tag = f" (M{meeting_id})" if with_id else ""
    return f"{started:%Y-%m-%d %H%M} {title[:80]}{tag}.{fmt}"


_writing = threading.Lock()  # exports run on request threads and in the background


def auto_export(store: Store, meeting_id: int) -> Path | None:
    """Write the meeting's Markdown into the export folder, if one is set. Never raises:
    a missing folder or a locked file only skips this copy."""
    folder = store.setting("export_folder")
    if not folder or not Path(folder).is_dir() or store.meeting(meeting_id) is None:
        return None
    folder = Path(folder)
    target = folder / file_name(store, meeting_id, "md", with_id=True)
    partial = target.with_name(f"{target.stem}.partial")
    with _writing:
        try:
            # Written whole, then moved into place: a notes app never reads half a file.
            partial.write_text(markdown(store, meeting_id), encoding="utf-8")
            os.replace(partial, target)
        except OSError:
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            return None
        for old in folder.glob(f"* (M{meeting_id}).md"):  # the copy under an earlier title
            if old != target:
                with contextlib.suppress(OSError):  # open in another app: it goes next time
                    old.unlink(missing_ok=True)
    return target
