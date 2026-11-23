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
