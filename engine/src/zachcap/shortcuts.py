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
    strong = held & {"Ctrl", "Alt", "Win"}
    if not strong and not ALONE.fullmatch(key):
        raise ValueError(
            "Hold Ctrl, Alt or Win with it: on its own (or with Shift) it would be taken "
            "from every app"
        )
    if strong and len(strong) < 2 and "Win" not in strong and not ALONE.fullmatch(key):
        raise ValueError(
            "Hold two of Ctrl, Alt and Win with it: with just one it would take a shortcut "
            "apps use (Ctrl+R reloads, Alt+F4 closes)"
        )
    return "+".join([name for name in ORDER if name in held] + [key])


def _saved(store) -> dict[str, str]:
    try:
        saved = json.loads(store.setting(SETTING) or "{}")
    except ValueError:
        return {}
    return saved if isinstance(saved, dict) else {}


def current(store) -> dict[str, str]:
    """The keys for each shortcut: the ones picked in Settings, or the defaults. Saved keys
    are checked again (saved by hand, or before a stricter check): ones that can't be used go
    back to the default, and of two on the same keys the later goes back to its own ("" when
    that's taken too, which Settings shows as keys that can't be used)."""
    saved = _saved(store)
    keys: dict[str, str] = {}
    for action, default in DEFAULTS.items():
        wanted = saved.get(action)
        try:
            chosen = normalized(wanted) if isinstance(wanted, str) and wanted else default
        except ValueError:
            chosen = default
        if chosen in keys.values():
            chosen = default if default not in keys.values() else ""
        keys[action] = chosen
    return keys


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
