"""Meetings as files: Markdown (summary, notes and transcript), plain text, and subtitles.

An optional export folder (an Obsidian vault, say) gets a Markdown copy of every meeting,
rewritten whenever the meeting changes. Each copy's name ends with the meeting's id, "(M12)",
so a renamed meeting replaces its old file and two meetings never share one. What other people
said or wrote, and what Claude made of it, goes into Markdown through inert(), so an image or
an HTML tag in it never loads anything when the file is opened.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path

from . import clients
from .store import Store

FORMATS = {"md": "Markdown", "txt": "Text", "srt": "Subtitles (SRT)", "vtt": "Subtitles (WebVTT)"}

# What makes a Markdown app (Obsidian, say) load something when it opens a file: an image,
# inline, by reference or embedded ("![x](url)", "![x][1]", "![[x]]"), or raw HTML, which
# starts with "<" and a letter, "/", "!" or "?" (a tag, a comment, "<https://...>"). The
# backslashes already in front of one are taken along, as they decide whether it is escaped.
# The page's copy of a summary as Markdown (src/lib/markdown.ts) follows the same rule.
_LOADS = re.compile(r"(\\*)(!\[|<(?=[A-Za-z/!?]))")


def inert(text: str) -> str:
    """Text from a meeting or from Claude, for a Markdown file: a backslash goes before each
    "![" and each "<" that could start HTML, so a Markdown app shows them as typed and loads
    nothing (an image's link stays a link), and a text editor shows little change.
    Backslashes already in front are doubled, so they still show and can't cancel it."""
    return _LOADS.sub(lambda found: found[1] * 2 + "\\" + found[2], text)


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


def _note_text(note) -> str:
    if note["kind"] == "file":
        name, text = note["text"].partition("\n")[::2]
        lines = text.count("\n") + 1
        return f"Attached file: {name} ({lines} line{'' if lines == 1 else 's'})"
    if note["kind"] == "snapshot":
        text = " / ".join(line.strip() for line in note["text"].splitlines() if line.strip())
        return f"Snapshot: {text}" if text else "Snapshot (no text)"
    return note["text"] or "Bookmarked moment"


def markdown(store: Store, meeting_id: int) -> str:
    """The meeting as Markdown. Only ZachCap's own headings and marks are written as they
    are; every name and text in between goes through inert()."""
    meeting = store.meeting(meeting_id)
    started = datetime.fromisoformat(meeting["started_at"])
    people = store.participants([meeting_id]).get(meeting_id, [])
    client = clients.of_meeting(store, meeting)["client"]
    lines = [f"# {inert(meeting['title'])}", ""]
    lines.append(
        f"{started:%A %B %d, %Y %H:%M}"
        + (f" | {round((meeting['duration'] or 0) / 60)} min" if meeting["duration"] else "")
        + (f" | {inert(', '.join(people))}" if people else "")
        + (f" | Client {inert(client)}" if client else "")
        + (f" | Ticket {inert(meeting['ticket'])}" if meeting["ticket"] else "")
    )
    summary = store.summary(meeting_id)
    if summary:
        content = summary["content"]
        lines += ["", "## Summary", "", inert(content.get("overview", ""))]
        if content.get("decisions"):
            lines += ["", "## Decisions", ""] + [f"- {inert(d)}" for d in content["decisions"]]
        items = store.action_items(meeting_id=meeting_id)
        if items:
            lines += ["", "## Action items", ""]
            for item in items:
                due = f" (due {inert(item['due'])})" if item["due"] else ""
                lines.append(
                    f"- [{'x' if item['status'] == 'done' else ' '}] **{inert(item['owner'])}**: "
                    f"{inert(item['task'])}{due}"
                )
        if content.get("open_questions"):
            questions = [f"- {inert(q)}" for q in content["open_questions"]]
            lines += ["", "## Open questions", ""] + questions
    notes = store.notes(meeting_id)
    if notes:
        lines += ["", "## My notes", ""]
        lines += [f"- [{_clock(n['at'])}] {inert(_note_text(n))}" for n in notes]
    transcript = store.transcript(meeting_id)
    if transcript:
        lines += ["", "## Transcript", ""]
        lines += [
            f"**[{_clock(r['start'])}] {inert(r['speaker'])}:** {inert(r['text'])}  "
            for r in transcript
        ]
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


def _own_copies(folder: Path, meeting_id: int) -> list[Path]:
    """The files ZachCap wrote for a meeting: named exactly as file_name() makes them
    ("2026-11-20 0900 Title (M12).md", same capitals). A file of the user's own that only
    happens to end in "(M12).md" is never touched (Windows matches names ignoring case)."""
    own = re.compile(rf"\d{{4}}-\d{{2}}-\d{{2}} \d{{4}} .+ \(M{meeting_id}\)\.md")
    return [path for path in folder.glob(f"* (M{meeting_id}).md") if own.fullmatch(path.name)]


def _export_folder(store: Store) -> Path | None:
    folder = store.setting("export_folder")
    return Path(folder) if folder and Path(folder).is_dir() else None


def _write_whole(target: Path, content: str) -> bool:
    """Written whole, then moved into place: a notes app never reads half a file."""
    partial = target.with_name(f"{target.stem}.partial")
    try:
        partial.write_text(content, encoding="utf-8")
        os.replace(partial, target)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        return False


def auto_export(store: Store, meeting_id: int) -> Path | None:
    """Write the meeting's Markdown into the export folder, if one is set, and bring that
    day's and week's work logs up to date if those are kept there too. Never raises: a
    missing folder or a locked file only skips this copy."""
    folder = _export_folder(store)
    meeting = store.meeting(meeting_id)
    if folder is None or meeting is None:
        return None
    target = folder / file_name(store, meeting_id, "md", with_id=True)
    with _writing:
        if not _write_whole(target, markdown(store, meeting_id)):
            return None
        for old in _own_copies(folder, meeting_id):  # the copy under an earlier title
            if old != target:
                with contextlib.suppress(OSError):  # open in another app: it goes next time
                    old.unlink(missing_ok=True)
    refresh_work_logs(store, date.fromisoformat(meeting["started_at"][:10]))
    return target


def forget(store: Store, meeting_id: int, day: date) -> None:
    """After a meeting is deleted: remove its copy from the export folder and take it out
    of that day's and week's work logs."""
    folder = _export_folder(store)
    if folder is None:
        return
    with _writing:
        for old in _own_copies(folder, meeting_id):
            with contextlib.suppress(OSError):
                old.unlink(missing_ok=True)
    refresh_work_logs(store, day)


def save_work_log(store: Store, period: str, day: date, client: str | None = None) -> Path | None:
    """The day's or week's work log (only ``client``'s meetings, if given), written into the
    export folder (None if there's none)."""
    from . import digest

    folder = _export_folder(store)
    if folder is None:
        return None
    target = folder / digest.file_name(period, day, client)
    with _writing:
        written = _write_whole(target, digest.build(store, period, day, client)["markdown"])
    return target if written else None


def refresh_work_logs(store: Store, day: date) -> None:
    """Keep the day's and the week's work logs current, if Settings keeps them, and rewrite
    every other work log of that day and week saved in the export folder (one client's, or
    one saved by hand), so none keeps a meeting that was deleted, redacted or given another
    client."""
    folder = _export_folder(store)
    if folder is None:
        return
    every = store.setting("export_work_log") == "1"
    for period in ("day", "week"):
        if every:
            save_work_log(store, period, day)
        for client in _saved_work_logs(folder, period, day):
            if client or not every:
                save_work_log(store, period, day, clients.canonical(store, client) or None)


def _saved_work_logs(folder: Path, period: str, day: date) -> list[str]:
    """The work logs of this day or week saved in the folder, by the client each is for (""
    for everyone's). A file counts only when it's named and titled as save_work_log writes
    it, so a file of your own is never rewritten; a client's is found from its title, even
    when no meeting or person has that client any more."""
    from . import digest

    title = f"# {digest.title(period, day)}"
    found = []
    for path in folder.glob(f"{Path(digest.file_name(period, day)).stem}*.md"):
        try:
            with path.open(encoding="utf-8") as file:
                first = file.readline().rstrip("\n")
        except (OSError, UnicodeDecodeError):
            continue
        if first != title and not first.startswith(f"{title} for "):
            continue
        client = first[len(title) + len(" for ") :] if first != title else ""
        if digest.file_name(period, day, client).casefold() == path.name.casefold():
            found.append(client)
    return found
