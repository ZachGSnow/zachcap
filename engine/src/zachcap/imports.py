"""Bring an existing recording into the library. The Import dialog, `zachcap import` and the
watch folder all come through here, so they name, date and store recordings the same way."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from . import audio, paths
from .store import Store

# The kinds of file the Import dialog offers (audio, or video with an audio track). FFmpeg
# reads a file only as one of these (audio.IMPORT_FORMATS), whatever it is named.
MEDIA = {".mp4", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".webm", ".mkv", ".aac", ".wma", ".mov"}

# Teams names its recordings "<title>-20261027_090709-Meeting Recording.mp4".
TEAMS_SUFFIX = re.compile(r"-\d{8}_\d{6}-Meeting Recording$")


def title_for(source: Path) -> str:
    """A meeting title from a file name, without Teams' date-and-"Meeting Recording" tail."""
    return TEAMS_SUFFIX.sub("", source.stem).strip() or source.stem


def new_folder(now: datetime | None = None) -> Path:
    """A folder of its own for an imported recording (two in the same second get -2, -3...)."""
    base = paths.meetings_dir() / f"{(now or datetime.now()):%Y-%m-%d_%H%M%S}-import"
    folder, number = base, 1
    while True:
        try:
            folder.mkdir(parents=True)
            return folder
        except FileExistsError:
            number += 1
            folder = base.with_name(f"{base.name}-{number}")


def import_recording(
    store: Store,
    source: Path,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[int, float]:
    """Decode the file into a new meeting folder (the user's file is only read) and add the
    meeting, dated from the file. Returns the meeting id and the seconds kept. Raises
    audio.Refused for a file of another kind, or one too big or too long."""
    folder = new_folder()
    try:
        # A finished recording is one mixed track: everyone, you included, goes on the
        # "system" track and is told apart by voice.
        seconds = audio.import_media(source, folder / "system.flac", start, end)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    meeting_id = store.create_meeting(
        title or title_for(source),
        audio.recorded_at(source).isoformat(timespec="seconds"),
        folder,
        duration=round(seconds, 3),
    )
    return meeting_id, seconds
