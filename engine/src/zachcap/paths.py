"""Where ZachCap keeps its data: %LOCALAPPDATA%\\ZachCap, or ZACHCAP_HOME if set."""

from __future__ import annotations

import os
from pathlib import Path


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
