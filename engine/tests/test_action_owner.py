"""An action item's owner as ZachCap shows it (the page, exports, the work log, Ask and the MCP
server): whom its voice is now, once that voice is named or recognized, as ZachCore gets it."""

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, export
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"
AUTH = {"Authorization": "Bearer test-token"}


def promised_by_a_stranger(store: Store, folder: Path) -> tuple[int, int]:
    """A meeting where a voice nobody has named yet promised something."""
    meeting = store.create_meeting("Cutover", "2026-11-22T20:00:00", folder, duration=60.0)
    stranger = store.add_speaker(
        meeting, "system", "Speaker 2", np.ones(4), 6.0, None, Match(None, 0.1, "unknown")
    )
    store.add_segments(meeting, [(stranger, "system", 1.0, 4.0, "I will test the VPN", 0.9)])
    store.replace_action_items(
        meeting,
        [{"owner": "Speaker 2", "task": "Test the VPN", "kind": "commitment", "said_at": 1.0}],
    )
    return meeting, stranger


def owners(store: Store, meeting: int | None = None) -> list[str]:
    return [row["owner"] for row in store.action_items(meeting_id=meeting)]


def test_an_item_follows_its_voice_once_the_voice_is_named(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, stranger = promised_by_a_stranger(store, tmp_path)
    assert owners(store, meeting) == ["Speaker 2"]

    store.set_speaker_person(stranger, store.person_id("Priya Shah"), "confirmed")
    assert owners(store, meeting) == ["Priya Shah"]
    assert owners(store) == ["Priya Shah"]  # the list across meetings too
    assert [row["owner_written"] for row in store.action_items()] == ["Speaker 2"]
    store.close()


def test_a_suggestion_is_not_whom_it_is(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, stranger = promised_by_a_stranger(store, tmp_path)
    store.set_speaker_person(stranger, store.person_id("Priya Shah"), "suggested")
    assert owners(store, meeting) == ["Speaker 2"]
    store.close()


def test_a_voice_recognized_and_then_named_again_takes_its_items_along(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, stranger = promised_by_a_stranger(store, tmp_path)
    store.set_speaker_person(stranger, store.person_id("Dana Lee"), "auto")
    assert owners(store, meeting) == ["Dana Lee"]
    store.set_speaker_person(stranger, store.person_id("Priya Shah"), "confirmed")
    assert owners(store, meeting) == ["Priya Shah"]
    store.close()


def test_without_a_voice_the_written_owner_stays(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, stranger = promised_by_a_stranger(store, tmp_path)
    store.set_speaker_person(stranger, store.person_id("Priya Shah"), "confirmed")
    store.replace_action_items(
        meeting,
        [
            {"owner": "Unassigned", "task": "Tell the branch offices", "kind": "action"},
            {"owner": "Sam from the vendor", "task": "Ship the spare", "kind": "action"},
        ],
    )
    assert sorted(owners(store, meeting)) == ["Sam from the vendor", "Unassigned"]
    store.close()


def test_the_list_has_every_column_of_the_table(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    promised_by_a_stranger(store, tmp_path)
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(action_items)")}
    assert columns <= set(store.action_items()[0].keys())
    store.close()


def test_exports_and_ask_name_whom_it_is_now(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, stranger = promised_by_a_stranger(store, tmp_path)
    store.set_speaker_person(stranger, store.person_id("Priya Shah"), "confirmed")
    assert "Priya Shah" in export.markdown(store, meeting)
    assert "Speaker 2" not in export.markdown(store, meeting).split("Action items")[-1]
    asked = assistant.action_items_list(store, "open", owner="priya")
    assert "Test the VPN" in asked and "Priya Shah" in asked
    store.close()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app("test-token"), base_url=LOCAL)


def test_your_voice_makes_it_yours(client: TestClient, tmp_path: Path):
    store = Store()
    meeting, stranger = promised_by_a_stranger(store, tmp_path)
    store.set_speaker_person(stranger, store.me()["id"], "confirmed")
    store.close()

    listed = client.get("/api/actions", headers=AUTH).json()
    assert [item["owner"] for item in listed["items"]] == [listed["me"]]
    detail = client.get(f"/api/meetings/{meeting}", headers=AUTH).json()
    assert [item["owner"] for item in detail["action_items"]] == [listed["me"]]
