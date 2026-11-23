"""The global shortcuts and the keys picked for them (Settings > Shortcuts).

Stored here with the other settings; the shell registers them with Windows and says which
ones another app already has. Keys are written like "Ctrl+Alt+R": modifiers in the order
Ctrl, Alt, Shift, Win, then one key (a letter, a digit, or F1 to F24).
"""

from __future__ import annotations

import json
import re

SETTING = "shortcuts"
DEFAULTS = {
    "record": "Ctrl+Alt+R",
    "bookmark": "Ctrl+Alt+B",
    "snapshot": "Ctrl+Alt+S",
    "dictate": "Ctrl+Alt+D",
}
NAMES = {
    "record": "Start or stop recording",
    "bookmark": "Bookmark this moment",
    "snapshot": "Snapshot the call",
    "dictate": "Dictate",
}
MODIFIERS = {
    "ctrl": "Ctrl",
    "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win",
    "super": "Win",
    "meta": "Win",
}
ORDER = ("Ctrl", "Alt", "Shift", "Win")
KEY = re.compile(r"[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])")
ALONE = re.compile(r"F(?:1[3-9]|2[0-4])")  # keys no app uses (macro keys send them)


def normalized(keys: str) -> str:
    """``keys`` written the one way ("ctrl + alt + p" is "Ctrl+Alt+P"). Raises ValueError,
    with a reason to show, for keys that can't be a global shortcut."""
    parts = [part.strip() for part in keys.split("+") if part.strip()]
    if not parts:
        raise ValueError("No keys given")
    *modifiers, key = parts
    held = set()
    for modifier in modifiers:
        name = MODIFIERS.get(modifier.lower())
        if name is None:
            raise ValueError(f'"{modifier}" isn\'t Ctrl, Alt, Shift or Win')
        held.add(name)
    key = key.upper()
    if not KEY.fullmatch(key):
        raise ValueError("The last key must be a letter, a digit, or F1 to F24")
    if not held & {"Ctrl", "Alt", "Win"} and not ALONE.fullmatch(key):
        raise ValueError(
            "Hold Ctrl, Alt or Win with it: on its own (or with Shift) it would be taken "
            "from every app"
        )
    return "+".join([name for name in ORDER if name in held] + [key])


def _saved(store) -> dict[str, str]:
    try:
        saved = json.loads(store.setting(SETTING) or "{}")
    except ValueError:
        return {}
    return saved if isinstance(saved, dict) else {}


def current(store) -> dict[str, str]:
    """The keys for each shortcut: the ones picked in Settings, or the defaults."""
    saved = _saved(store)
    return {
        action: saved[action] if isinstance(saved.get(action), str) else keys
        for action, keys in DEFAULTS.items()
    }


def change(store, changes: dict[str, str]) -> dict[str, str]:
    """Saves new keys for some shortcuts ("" puts back the default) and returns them all.
    Raises ValueError, with a reason to show, for an unknown shortcut, keys that can't be
    used, or keys another shortcut already has; nothing is saved then."""
    keys = current(store)
    for action, wanted in changes.items():
        if action not in DEFAULTS:
            raise ValueError(f'There is no "{action}" shortcut')
        keys[action] = normalized(wanted) if wanted.strip() else DEFAULTS[action]
    taken: dict[str, str] = {}
    for action, combination in keys.items():
        if combination in taken:
            raise ValueError(f'{combination} is already "{NAMES[taken[combination]]}"')
        taken[combination] = action
    changed = {action: keys[action] for action in DEFAULTS if keys[action] != DEFAULTS[action]}
    store.set_setting(SETTING, json.dumps(changed) if changed else "")
    return keys
