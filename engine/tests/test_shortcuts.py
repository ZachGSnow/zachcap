"""The global shortcuts' keys: picked in Settings, checked, and kept apart from each other."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import shortcuts
from zachcap.api import create_app
from zachcap.store import Store

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    yield store
    store.close()


def test_every_form_saved_is_one_the_shell_reads():
    # The shell's own test (lib.rs, keys_the_engine_saves_parse_in_both_shell_parsers) parses
    # this same list with both of its parsers; keep the two lists the same.
    written = ["ctrl+alt+r", "alt+ctrl+f9", "shift+alt+ctrl+9", "win+shift+d", "win+ctrl+f24",
               "win+alt+0", "f13"]  # fmt: skip
    assert [shortcuts.normalized(keys) for keys in written] == [
        "Ctrl+Alt+R",
        "Ctrl+Alt+F9",
        "Ctrl+Alt+Shift+9",
        "Shift+Win+D",
        "Ctrl+Win+F24",
        "Alt+Win+0",
        "F13",
    ]
    assert shortcuts.normalized("Win+D") == "Win+D"  # Win counts on its own


def test_saved_keys_are_checked_again(store: Store):
    store.set_setting(shortcuts.SETTING, '{"record": "Ctrl+R", "bookmark": "Ctrl+Alt+S"}')
    keys = shortcuts.current(store)
    assert keys["record"] == "Ctrl+Alt+R"  # can't be used any more: the default again
    assert keys["bookmark"] == "Ctrl+Alt+S" and keys["snapshot"] == ""  # its default is taken


def test_keys_are_written_one_way():
    assert shortcuts.normalized("alt + ctrl + p") == "Ctrl+Alt+P"
    assert shortcuts.normalized("Win+Shift+9") == "Shift+Win+9"
    assert shortcuts.normalized("control+alt+f9") == "Ctrl+Alt+F9"
    assert shortcuts.normalized("F13") == "F13"  # a macro key no app uses may stand alone


@pytest.mark.parametrize(
    ("keys", "reason"),
    [
        ("", "No keys"),
        ("P", "Hold Ctrl, Alt or Win"),  # would take P from every app
        ("Shift+P", "Hold Ctrl, Alt or Win"),
        ("F5", "Hold Ctrl, Alt or Win"),  # every browser's reload
        ("Ctrl+Alt+Tab", "letter, a digit"),
        ("Ctrl+Hyper+P", "isn't Ctrl, Alt, Shift or Win"),
        ("Ctrl+R", "two of Ctrl, Alt and Win"),  # every browser's reload
        ("Alt+F4", "two of Ctrl, Alt and Win"),
        ("Ctrl+Shift+T", "two of Ctrl, Alt and Win"),
    ],
)
def test_keys_that_cant_be_a_shortcut_are_refused(keys: str, reason: str):
    with pytest.raises(ValueError, match=reason):
        shortcuts.normalized(keys)


def test_only_changed_keys_are_saved_and_two_shortcuts_never_share_keys(store: Store):
    assert shortcuts.current(store) == shortcuts.DEFAULTS

    keys = shortcuts.change(store, {"snapshot": "ctrl+alt+p"})
    assert keys["snapshot"] == "Ctrl+Alt+P" and keys["record"] == "Ctrl+Alt+R"
    assert store.setting(shortcuts.SETTING) == '{"snapshot": "Ctrl+Alt+P"}'

    with pytest.raises(ValueError, match='Ctrl\\+Alt\\+P is already "Snapshot the call"'):
        shortcuts.change(store, {"dictate": "Ctrl+Alt+P"})
    with pytest.raises(ValueError, match='no "pause" shortcut'):
        shortcuts.change(store, {"pause": "Ctrl+Alt+Q"})
    assert shortcuts.current(store)["dictate"] == "Ctrl+Alt+D"  # nothing saved by a refusal

    # Swapping two shortcuts' keys in one go is fine.
    keys = shortcuts.change(store, {"record": "Ctrl+Alt+P", "snapshot": "Ctrl+Alt+R"})
    assert (keys["record"], keys["snapshot"]) == ("Ctrl+Alt+P", "Ctrl+Alt+R")

    shortcuts.change(store, {"record": "", "snapshot": ""})  # "" puts the defaults back
    assert shortcuts.current(store) == shortcuts.DEFAULTS and not store.setting(shortcuts.SETTING)

    store.set_setting(shortcuts.SETTING, "not json")
    assert shortcuts.current(store) == shortcuts.DEFAULTS


def test_shortcuts_through_the_settings_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"))
    assert client.get("/api/settings", headers=AUTH).json()["shortcuts"] == shortcuts.DEFAULTS

    changed = client.put(
        "/api/settings", headers=AUTH, json={"shortcuts": {"bookmark": "Ctrl+Alt+M"}}
    )
    assert changed.json()["shortcuts"]["bookmark"] == "Ctrl+Alt+M"

    # Keys that can't be used change nothing, not even the other settings sent with them.
    refused = client.put(
        "/api/settings", headers=AUTH, json={"shortcuts": {"record": "Q"}, "language": "fr"}
    )
    assert refused.status_code == 400 and "Hold Ctrl, Alt or Win" in refused.json()["detail"]
    after = client.get("/api/settings", headers=AUTH).json()
    assert after["language"] == "en" and after["shortcuts"]["record"] == "Ctrl+Alt+R"
