"""Where ZachCap keeps its data (%LOCALAPPDATA%\\ZachCap, or ZACHCAP_HOME if set), and the
check every path the app sends goes through before anything looks at it."""

from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path

DRIVE_REMOTE = 4  # what GetDriveTypeW says of a drive letter mapped to a network share
# A drive letter, a colon and a backslash (C:\), once / is read as \.
_DRIVE = re.compile(r"[A-Za-z]:\\")
# Never part of a file name: wildcards, quotes, a pipe, control characters, and a colon
# after the drive's, which names a hidden stream of a file (notes.txt:secret).
_NOT_IN_NAMES = re.compile(r'[\x00-\x1f<>"|?*:]')
# Names Windows opens as a device instead of a file, in any folder and with any extension
# (NUL.txt is NUL); COM and LPT with 0-9 and superscript 1-3, as Windows' own list has them.
_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789\u00b9\u00b2\u00b3"}
)


def home() -> Path:
    root = os.environ.get("ZACHCAP_HOME")
    path = Path(root) if root else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ZachCap"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database() -> Path:
    return home() / "zachcap.db"


def meetings_dir() -> Path:
    path = home() / "meetings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def drive_type(root: str) -> int:
    """What Windows says the drive at ``root`` (C:\\) is: DRIVE_REMOTE for a letter mapped
    to a network share."""
    return ctypes.windll.kernel32.GetDriveTypeW(root)


def _device(name: str) -> bool:
    """Whether Windows reads a file or folder name as a device: the part before its first
    dot, without the spaces after it ("NUL", "nul.txt", "Aux .log", "COM1.")."""
    return name.partition(".")[0].rstrip(" ").upper() in _DEVICES


def on_this_pc(path: str, what: str = "file") -> Path:
    """A path the app sent (a file to import, attach or export to, a folder for Settings),
    with its .. worked out. Raises ValueError, with the reason to show, unless it's a plain
    path on one of this PC's drives. Only the text and the drive's type are read, so a
    refused path is never opened or looked up.

    The app's file dialogs give plain paths, so this only turns away a request that didn't
    come from one: a path on another computer makes Windows connect to that computer and
    sign in to it as you, and a device or a stream isn't a file at all."""
    refused = f"Choose a {what} on this PC; network and device paths aren't used"
    text = path.replace("/", "\\")
    # Turned away as written: anything that doesn't start with a drive (\\server\share,
    # \\?\ and \\.\ even for C:, \??\, \folder on the current drive, C:folder in that
    # drive's current folder), a character no name has, and a device name in any folder.
    if (
        not _DRIVE.match(text)
        or _NOT_IN_NAMES.search(text, 2)
        or any(_device(name) for name in text[3:].split("\\"))
    ):
        raise ValueError(refused)
    # As Windows reads it: .. worked out (never above the drive), trailing dots and spaces
    # dropped. Checked again, in case Windows turns it into a device path (\\.\NUL).
    full = os.path.abspath(text)
    if not _DRIVE.match(full):
        raise ValueError(refused)
    if drive_type(full[:3]) == DRIVE_REMOTE:
        raise ValueError(f"{full[:2].upper()} is a network drive; choose a {what} on this PC")
    return Path(full)


def saved_folder(saved: str | None) -> Path | None:
    """A folder saved in Settings, as the path to use: None when none is saved, or when
    on_this_pc refuses it (one saved before Settings checked them is never used)."""
    try:
        return on_this_pc(saved, "folder") if saved else None
    except ValueError:
        return None
