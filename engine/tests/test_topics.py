"""Topics: words you track, found in each meeting and across them."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import digest, topics
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}
TOPICS = "Renewals: renewal, contract\nWindows 10: windows 10, win10\nSecurity: MFA, phishing"


def meeting(store: Store, folder: Path, title: str, started: str, *said: str) -> int:
    meeting_id = store.create_meeting(title, started, folder / title, duration=600)
    person = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting_id, "system", "Dana", None, 5.0, None, Match(person, 1.0, "confirmed")
    )
    store.add_segments(
        meeting_id,
        [(speaker, "system", i * 5.0, i * 5.0 + 4, text, 0.9) for i, text in enumerate(said)],
    )
    store.update_meeting(meeting_id, status="ready")
    return meeting_id


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.set_setting("export_folder", "")
    store.set_setting(topics.SETTING, TOPICS)
    yield store
    store.close()


def test_topics_are_written_one_per_line():
    written = topics.parse(TOPICS + "\n\n")
    assert [t["name"] for t in written] == ["Renewals", "Windows 10", "Security"]
    assert written[1]["words"] == ["windows 10", "win10"]


@pytest.mark.parametrize(
    ("written", "reason"),
    [
        ("Renewals", "the topic, a colon"),
        ("Renewals:", "the topic, a colon"),
        ("Renewals: renewal\nrenewals: quote", "already a topic"),
        ("Odd: ---", "a letter or a digit"),
        ("Long: " + "x" * 41, "at most 40"),
        ("\n".join(f"T{i}: word" for i in range(21)), "At most 20"),
    ],
)
def test_topics_that_cant_be_used_are_refused(written: str, reason: str):
    with pytest.raises(ValueError, match=reason):
        topics.parse(written)


def test_a_meeting_shows_the_topics_said_in_it(store: Store, tmp_path: Path):
    meeting_id = meeting(
        store,
        tmp_path,
        "Quarterly review",
        "2026-11-23T09:00:00-05:00",
        "The contract renewal is in March, and the Renewal quote is ready.",
        "Three PCs still run Windows 10; one more is on Win10 too.",
        "Contractors don't count here.",  # whole words only
    )
    found = topics.find(store, meeting_id)
    assert [(t["topic"], t["count"]) for t in found] == [("Renewals", 3), ("Windows 10", 2)]
    first = found[0]["lines"][0]
    assert first["spans"] == [[4, 12], [13, 20], [42, 49]]  # contract, renewal, Renewal
    assert topics.of_meeting(store, meeting_id) == {"Renewals": 3, "Windows 10": 2}


def test_the_topics_page_and_the_work_log(store: Store, tmp_path: Path):
    older = meeting(store, tmp_path, "Old", "2026-11-20T09:00:00-05:00", "MFA for everyone.")
    newer = meeting(
        store, tmp_path, "New", "2026-11-23T09:00:00-05:00", "A phishing test, and MFA."
    )
    store.update_meeting(newer, client="Contoso")
    security = next(t for t in topics.overview(store) if t["topic"] == "Security")
    assert security["count"] == 3
    assert [(m["id"], m["count"], m["client"]) for m in security["meetings"]] == [
        (newer, 2, "Contoso"),
        (older, 1, None),
    ]
    log = digest.build(store, "day", date(2026, 11, 23))
    assert log["days"][0]["meetings"][0]["topics"] == [{"topic": "Security", "count": 2}]
    assert "Topics: Security (2)." in log["markdown"]


def test_topics_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    meeting_id = meeting(
        store, tmp_path, "Call", "2026-11-23T09:00:00-05:00", "Renew the contract."
    )
    refused = client.put("/api/settings", headers=AUTH, json={"topics": "no colon"})
    assert refused.status_code == 400 and "Line 1" in refused.json()["detail"]
    saved = client.put("/api/settings", headers=AUTH, json={"topics": "Renewals: contract"})
    assert saved.json()["topics"] == "Renewals: contract"
    detail = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert [t["topic"] for t in detail["topics"]] == ["Renewals"]
    listed = client.get("/api/topics", headers=AUTH).json()
    assert listed[0]["count"] == 1 and listed[0]["meetings"][0]["id"] == meeting_id
    store.close()
