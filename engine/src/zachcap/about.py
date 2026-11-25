"""What the About page shows of this installation."""

from __future__ import annotations

import sys
from pathlib import Path


def build() -> str | None:
    """The installer's BUILD stamp: the version, the commit and when it was built. It's beside
    the engine's venv; None when ZachCap runs from its source."""
    try:
        stamp = (Path(sys.prefix).parent / "BUILD").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return stamp or None


def windows() -> str:
    """Windows by the name it goes by, and its build."""
    found = sys.getwindowsversion()
    name = "Windows Server"
    if found.product_type == 1:  # a workstation
        name = "Windows 11" if found.build >= 22000 else "Windows 10"
    return f"{name} (build {found.build})"
