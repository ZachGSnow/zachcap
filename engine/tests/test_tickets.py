"""Ticket numbers: found in a meeting, filled in or offered, and carried with it."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, digest, export, tickets
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

AUTH = {"Authorization": "Bearer test-token"}


def lines(store: Store, meeting_id: int, *said: str) -> None:
    person = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting_id, "system", "Dana", None, 5.0, None, Match(person, 1.0, "confirmed")
    )
    store.add_segments(
        meeting_id,
        [(speaker, "system", float(i), i + 1.0, text, 0.9) for i, text in enumerate(said)],
    )


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.set_setting("export_folder", "")
    store.set_setting(tickets.SETTING, "#######, INC-####")
    yield store
    store.close()


def test_patterns_find_whole_ticket_numbers_only():
    seven, incident = tickets.patterns("#######, INC-####")
    assert seven.search("call 1924086 today").group() == "1924086"
    assert not seven.search("19240861") and not seven.search("A1924086")  # part of more
    assert incident.search("see inc-0042 too").group() == "inc-0042"
    assert tickets.patterns("") == [] and tickets.patterns(None) == []
    assert tickets.patterns("T-??##").pop().search("ticket T-AB12")


@pytest.mark.parametrize(
    ("written", "reason"),
    [
        ("###", "at least 4"),  # would match almost any number
        ("#" * 31, "too long"),
        (", ".join(["####"] * 6), "At most 5"),
    ],
)
def test_patterns_that_cant_be_used_are_refused(written: str, reason: str):
    with pytest.raises(ValueError, match=reason):
        tickets.patterns(written)


def test_tickets_in_your_notes_fill_in_and_ones_only_heard_are_offered(store: Store, tmp_path):
    meeting_id = store.create_meeting("Printer down", "2026-11-23T09:00:00", tmp_path / "a")
    lines(store, meeting_id, "It's on ticket 19 24 086, I think.", "My cell is 5551234.")

    found = tickets.candidates(store, meeting_id)
    assert [c["ticket"] for c in found] == ["1924086", "5551234"]  # said near "ticket" first
    assert found[0]["where"] == ["transcript"]
    assert tickets.fill(store, meeting_id) is None  # only heard: offered, not filled in

    store.add_note(meeting_id, 3.0, "note", "Logged as INC-0042")
    assert tickets.fill(store, meeting_id) == "INC-0042"
    assert store.meeting(meeting_id)["ticket"] == "INC-0042"
    assert tickets.candidates(store, meeting_id)[0]["where"] == ["notes"]  # yours rank first

    store.add_note(meeting_id, 4.0, "note", "Also 7654321")
    store.update_meeting(meeting_id, ticket=None)
    assert tickets.fill(store, meeting_id) is None  # two in your notes: you choose


def test_a_ticket_you_set_is_never_replaced(store: Store, tmp_path):
    meeting_id = store.create_meeting("Call about 1111111", "2026-11-23T09:00:00", tmp_path / "b")
    store.update_meeting(meeting_id, ticket="2222222")
    assert tickets.fill(store, meeting_id) is None
    assert store.meeting(meeting_id)["ticket"] == "2222222"


def test_the_ticket_goes_with_the_meeting(store: Store, tmp_path):
    meeting_id = store.create_meeting(
        "VPN drops", "2026-11-23T09:00:00-05:00", tmp_path / "c", duration=600
    )
    lines(store, meeting_id, "Thanks for calling.")
    store.update_meeting(meeting_id, status="ready", ticket="1924086")
    other = store.create_meeting("Standup", "2026-11-23T10:00:00-05:00", tmp_path / "d")
    store.update_meeting(other, status="ready")

    log = digest.markdown(digest.build(store, "day", date(2026, 11, 23)))
    assert "| Time | Hours | Ticket | Meeting | Note |" in log
    assert "| 1924086 | VPN drops |" in log and "VPN drops (ticket 1924086)" in log
    assert "| Ticket 1924086" in export.markdown(store, meeting_id)

    listed = assistant.meetings_list(store, ticket="1924086")
    assert f"M{meeting_id} " in listed and "ticket 1924086;" in listed and "Standup" not in listed
    assert ", ticket 1924086" in assistant.meeting_detail(store, meeting_id).splitlines()[0]


def test_a_log_without_tickets_keeps_its_columns(store: Store, tmp_path):
    meeting_id = store.create_meeting("Standup", "2026-11-23T10:00:00-05:00", tmp_path / "e")
    store.update_meeting(meeting_id, status="ready")
    log = digest.markdown(digest.build(store, "day", date(2026, 11, 23)))
    assert "| Time | Hours | Meeting | Note |" in log and "Ticket" not in log


def test_tickets_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"))
    store = Store()
    meeting_id = store.create_meeting("Printer down", "2026-11-23T09:00:00", tmp_path / "m")
    lines(store, meeting_id, "Ticket 1924086 is the one.")
    store.update_meeting(meeting_id, status="ready")

    refused = client.put("/api/settings", headers=AUTH, json={"ticket_patterns": "##"})
    assert refused.status_code == 400 and "at least 4" in refused.json()["detail"]
    saved = client.put("/api/settings", headers=AUTH, json={"ticket_patterns": " ####### ,"})
    assert saved.json()["ticket_patterns"] == "#######"

    detail = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert detail["meeting"]["ticket"] is None
    assert detail["ticket_suggestions"][0]["ticket"] == "1924086"

    chosen = client.patch(f"/api/meetings/{meeting_id}", headers=AUTH, json={"ticket": "1924086"})
    assert (chosen.json()["title"], chosen.json()["ticket"]) == ("Printer down", "1924086")
    detail = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert detail["ticket_suggestions"] == []  # the one it has isn't offered again

    hits = client.get("/api/search", headers=AUTH, params={"q": "1924086"}).json()
    assert hits[0] == {
        "meeting_id": meeting_id,
        "title": "Printer down",
        "start": 0,
        "speaker": "Ticket",
        "snippet": "[1924086]",
    }

    cleared = client.patch(f"/api/meetings/{meeting_id}", headers=AUTH, json={"ticket": ""})
    assert cleared.json()["ticket"] is None
    renamed = client.patch(
        f"/api/meetings/{meeting_id}", headers=AUTH, json={"title": "Printer down 7654321"}
    )
    assert renamed.json()["ticket"] == "7654321"  # filled in from the new title
    store.close()
