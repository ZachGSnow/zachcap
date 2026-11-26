"""API keys live in Windows Credential Manager, never in files or the repo.

Each key can also come from an environment variable, which is handy for CI.
"""

from __future__ import annotations

import getpass
import os

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "ZachCap"
KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "huggingface": "HF_TOKEN",
}


def get(name: str) -> str | None:
    """Return the key from Credential Manager, falling back to its environment variable."""
    return keyring.get_password(SERVICE, name) or os.environ.get(KEYS[name]) or None


def source(name: str) -> str:
    if keyring.get_password(SERVICE, name):
        return "Credential Manager"
    if os.environ.get(KEYS[name]):
        return f"environment ({KEYS[name]})"
    return "not set"


def store(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def prompt_and_store(name: str) -> None:
    value = getpass.getpass(f"Paste your {name} key (input is hidden), then press Enter: ").strip()
    if not value:
        raise SystemExit("Nothing entered; key not saved.")
    store(name, value)


def delete(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass


# ZachCore's keys: one per server address, so a test server never gets the real key, and an
# address typed differently never gets one meant for another. No environment variable.
def zachcore_name(address: str) -> str:
    return f"zachcore {address}"


def zachcore(address: str) -> str | None:
    return keyring.get_password(SERVICE, zachcore_name(address)) or None


def store_zachcore(address: str, key: str) -> None:
    keyring.set_password(SERVICE, zachcore_name(address), key)


def delete_zachcore(address: str) -> None:
    delete(zachcore_name(address))
