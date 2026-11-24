"""A watch folder: recordings saved there import themselves.

For a softphone's or phone system's exports, or Teams recordings downloaded from the web.
Every 30 seconds ZachCap looks at the folder (not its subfolders) for audio and video files
that have stopped changing: the same size and modified time on two looks, and untouched for
30 seconds, so a file still being written or copied is left alone. Each one is imported once
and queued for processing.

What was seen is remembered by path, size and modified time: a file exported again with new
content imports again, and a meeting deleted in ZachCap doesn't come back. A file that isn't
a recording ZachCap can read is noted with the reason (shown in Settings, with a Retry) and
not tried again until it changes. So is one whose reading never finished: each file is
noted before it is read, so one that crashes or hangs FFmpeg isn't read again on every look
or every start. One that can't be opened right now (open in another program, a network
share or cloud file offline) is simply tried again on the next look. Turning the folder
off, or choosing another, stops a run between files. The user's files are only ever read:
never moved, renamed or deleted.
"""

from __future__ import annotations

import os
import re
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from .imports import MEDIA, import_recording
from .store import Store

SETTING = "watch_folder"
CHECK_EVERY = 30.0
SETTLED_AFTER = 30.0  # seconds since the file last changed
# The reason a file is noted with before it is read. What happens next replaces it, so it
# stays only when ZachCap stopped part-way: FFmpeg crashed or hung, or ZachCap was quit.
UNFINISHED = "ZachCap stopped while reading it"


def media_files(folder: Path) -> dict[str, tuple[int, int]]:
    """Audio and video files directly in the folder: path -> (size, modified time in ns).
    One directory listing (os.scandir has each file's size and time from it on Windows), so
    a big folder or a network share costs one round trip, not two per file."""
    found = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                if os.path.splitext(entry.name)[1].lower() in MEDIA and entry.is_file():
                    stat = entry.stat()
                    found[entry.path] = (stat.st_size, stat.st_mtime_ns)
            except OSError:  # removed while looking, or no access
                continue
    return found


def reason(error: Exception, path: str) -> str:
    """Why a file couldn't be imported, short enough for Settings: the decoder's message
    without the error number or the file's full path."""
    text = re.sub(r"^\[Errno -?\d+\]\s*", "", str(error))
    for form in (path, path.replace("\\", "\\\\")):
        text = text.replace(f": '{form}'", "").replace(form, Path(path).name)
    return text.strip() or type(error).__name__


def skip_existing(store: Store, folder: Path) -> int:
    """Turned on with "only new recordings": the files there now are never imported (noted
    in one transaction, so an archive of thousands takes a moment, not seconds)."""
    skipped = 0
    with store.transaction():
        for path, (size, mtime_ns) in media_files(folder).items():
            if store.watched_file(path, size, mtime_ns) is None:
                store.record_watched_file(path, size, mtime_ns, "skipped")
                skipped += 1
    return skipped


def retry_existing(store: Store, folder: Path) -> None:
    """Turned on with "import what's there": files skipped or failed before get another go."""
    store.forget_watched_files(list(media_files(folder)), ("skipped", "failed"))


class FolderWatch:
    """Looks at the watch folder; ``check`` imports what has settled since the last look.
    ``failures`` is what Settings shows, worked out on each look (Settings never reads the
    folder itself, which on an unreachable share could take most of a minute)."""

    TROUBLE_SHOWN_AFTER = 3  # looks in a row a file couldn't be opened before it's shown

    def __init__(
        self,
        submit: Callable[[int], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._submit = submit
        self._clock = clock
        self._last: dict[str, tuple[int, int]] = {}
        # Files that couldn't be opened, by version: how many looks in a row, and why.
        self._troubled: dict[tuple[str, int, int], tuple[int, str]] = {}
        self.failures: list[dict] = []

    def reset(self) -> None:
        """The folder changed or was turned off: start over."""
        self._last, self._troubled, self.failures = {}, {}, []

    def check(self, store: Store) -> list[int]:
        """Imports the files that have stopped changing; returns the new meetings' ids."""
        folder = store.setting(SETTING)
        if not folder or not Path(folder).is_dir():  # off, or a drive that isn't there now
            self.reset()
            return []
        now = self._clock()
        current = media_files(Path(folder))
        imported = []
        for path, (size, mtime_ns) in sorted(current.items()):
            if store.setting(SETTING) != folder:
                break  # turned off, or another folder chosen: stop between files
            if store.watched_file(path, size, mtime_ns) is not None:
                continue  # imported, skipped or failed already: this version is done
            settled = self._last.get(path) == (size, mtime_ns)
            if not settled or now - mtime_ns / 1e9 < SETTLED_AFTER:
                continue  # still being written or copied (or seen for the first time)
            version = (path, size, mtime_ns)
            # Noted before it is read: a file that crashes or hangs FFmpeg is then listed
            # (with Retry), not read again on every look and every start.
            store.record_watched_file(path, size, mtime_ns, "failed", error=UNFINISHED)
            try:
                meeting_id, _ = import_recording(store, Path(path))
            except OSError as exc:  # open elsewhere, offline: just try again next look
                store.forget_watched_files([path], ("failed",))  # the note is taken back
                looks, _ = self._troubled.get(version, (0, ""))
                self._troubled[version] = (looks + 1, reason(exc, path))
                continue
            except Exception as exc:  # not a recording this can read: noted, not retried
                self._troubled.pop(version, None)
                store.record_watched_file(path, size, mtime_ns, "failed", error=reason(exc, path))
                continue
            self._troubled.pop(version, None)
            store.record_watched_file(path, size, mtime_ns, "imported", meeting_id=meeting_id)
            self._submit(meeting_id)
            imported.append(meeting_id)
        if store.setting(SETTING) != folder:  # what this look saw is of the old folder
            self.reset()
            return imported
        self._last = current
        self.failures = self._failures(store, Path(folder), current)
        return imported

    def _failures(self, store: Store, folder: Path, current: dict) -> list[dict]:
        """Files that couldn't be imported, as they are now (a file fixed, replaced or
        removed drops off; newest first), then ones that keep failing to open (still being
        tried on every look)."""
        failed = [
            {"name": Path(row["path"]).name, "error": row["error"], "retrying": False}
            for row in store.watched_failures(str(folder))
            if current.get(row["path"]) == (row["size"], row["mtime_ns"])
        ]
        stuck = [
            {"name": Path(path).name, "error": why, "retrying": True}
            for (path, size, mtime_ns), (looks, why) in self._troubled.items()
            if looks >= self.TROUBLE_SHOWN_AFTER and current.get(path) == (size, mtime_ns)
        ]
        return failed + stuck

    def retry_failed(self, store: Store) -> int:
        """Settings' Retry: the files that failed get another go on the next look."""
        folder = store.setting(SETTING)
        if not folder:
            return 0
        rows = store.watched_failures(str(Path(folder)))
        store.forget_watched_files([row["path"] for row in rows], ("failed",))
        self._troubled = {}
        self.failures = []
        return len(rows)

    def run_forever(self, interval: float = CHECK_EVERY) -> None:
        """The engine's watch-folder thread."""
        while True:
            store = None
            try:
                store = Store()
                self.check(store)
            except Exception:  # never let one bad look end the watching
                traceback.print_exc()
            finally:
                if store is not None:
                    store.close()
            time.sleep(interval)
