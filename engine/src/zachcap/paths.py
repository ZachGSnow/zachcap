"""Where ZachCap keeps its data (%LOCALAPPDATA%\\ZachCap, or ZACHCAP_HOME if set), and the
check every path the app sends goes through before anything looks at it."""

from __future__ import annotations

import os
import re
from pathlib import Path

# A drive letter, a colon and a backslash (C:\), once / is read as \.
_DRIVE = re.compile(r"[A-Za-z]:\\")
# A server named after two backslashes (\\server\share); \\?\ and \\.\ are Windows' own.
_NETWORK = re.compile(r"\\\\(?![?.]\\)")
# Never part of a file name: wildcards, quotes, a pipe, control characters, and a colon
# after the drive's, which names a hidden stream of a file (notes.txt:hidden).
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


def _device(name: str) -> bool:
    """Whether Windows reads a file or folder name as a device: the part before its first
    dot, without the spaces after it ("NUL", "nul.txt", "Aux .log", "COM1.")."""
    return name.partition(".")[0].rstrip(" ").upper() in _DEVICES


def on_a_drive(path: str, what: str = "file") -> Path:
    """A path the app sent (a file to import, attach or export to, a folder for Settings),
    with its .. worked out. Raises ValueError, with the reason to show, unless it's a plain
    path on a drive letter: one of this PC's own, or a mapped network drive. Only the text
    is read, so a refused path is never opened or looked up.

    A network path can name any server at all: Windows would connect to it and sign in to it
    as you, and an export folder there would send it your meetings. A mapped drive points at
    a server the user chose, and a request can't make one. A device or a stream isn't a file
    at all. The app's file dialogs give plain paths, so the rest only turns away a request
    that didn't come from one."""
    text = path.replace("/", "\\")
    if _NETWORK.match(text):  # a server named in the path: say how to reach it instead
        raise ValueError(
            r"Network paths (\\server\share) aren't used; map the share to a drive letter in "
            f"File Explorer and choose the {what} there"
        )
    refused = f"Choose a {what} on this PC or a mapped drive; device paths aren't used"
    # Turned away as written: anything else that doesn't start with a drive (\\?\ and
    # \\.\ even for C:, \??\, \folder on the current drive, C:folder in that drive's
    # current folder), a character no name has, and a device name in any folder.
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
    return Path(full)


def saved_folder(saved: str | None) -> Path | None:
    """A folder saved in Settings, as the path to use: None when none is saved, or when
    on_a_drive refuses it (a network path saved before Settings checked them isn't used)."""
    try:
        return on_a_drive(saved, "folder") if saved else None
    except ValueError:
        return None
