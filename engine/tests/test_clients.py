"""Clients: from the people in a meeting, or set on it, and carried with it."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, clients, digest, export
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

AUTH = {"Authorization": "Bearer test-token"}


def meeting(store: Store, folder: Path, title: str, started: str, people=()) -> int:
    meeting_id = store.create_meeting(title, started, folder / title, duration=600)
    for at, name in enumerate(people):
        person = store.person_id(name)
        speaker = store.add_speaker(
            meeting_id, "system", name, None, 5.0, None, Match(person, 1.0, "confirmed")
        )
        store.add_segments(meeting_id, [(speaker, "system", at * 5.0, at * 5.0 + 4, "Hi.", 0.9)])
    store.update_meeting(meeting_id, status="ready")
    return meeting_id


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.set_setting("export_folder", "")
    store.ids = {
        "cutover": meeting(
            store, tmp_path, "Cutover", "2026-11-23T09:00:00-05:00", ["Dana", "Mark"]
        ),
        "both": meeting(
            store, tmp_path, "Joint call", "2026-11-23T11:00:00-05:00", ["Dana", "Lee"]
        ),
        "nobody": meeting(store, tmp_path, "New prospect", "2026-11-23T14:00:00-05:00"),
    }
    for name, client in (("Dana", "Contoso"), ("Mark", "contoso"), ("Lee", "Fabrikam")):
        store.set_client(store.person_id(name), client)
    yield store
    store.close()


def test_a_meeting_is_for_the_client_its_people_work_for(store: Store):
    found = clients.of_meetings(store, store.meetings())
    ids = store.ids
    assert found[ids["cutover"]] == {"client": "Contoso", "inferred": True, "clients": ["Contoso"]}
    both = found[ids["both"]]
    assert both["client"] is None and sorted(both["clients"]) == ["Contoso", "Fabrikam"]
    assert found[ids["nobody"]]["client"] is None

    store.update_meeting(ids["both"], client="Fabrikam")  # set on it: it wins
    assert clients.of_meeting(store, store.meeting(ids["both"]))["client"] == "Fabrikam"
    assert clients.known(store) == ["Contoso", "Fabrikam"]  # one spelling each


def test_the_client_goes_with_the_meeting(store: Store):
    ids = store.ids
    store.update_meeting(ids["nobody"], client="Northwind")

    log = digest.build(store, "day", date(2026, 11, 23))
    text = digest.markdown(log)
    assert "| Time | Hours | Client | Meeting | Note |" in text
    assert "| Contoso | Cutover |" in text and "Cutover (Contoso)" in text

    contoso = digest.build(store, "day", date(2026, 11, 23), client="contoso")
    titles = [m["title"] for day in contoso["days"] for m in day["meetings"]]
    assert titles == ["Cutover"] and contoso["title"].endswith("for contoso")
    assert "| Client |" not in digest.markdown(contoso)  # one client: not on every line

    assert "| Client Contoso" in export.markdown(store, ids["cutover"])
    listed = assistant.meetings_list(store, client="Northwind")
    assert "New prospect" in listed and "client Northwind;" in listed and "Cutover" not in listed


def test_clients_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"))
    store = Store()
    meeting_id = meeting(store, tmp_path, "Cutover", "2026-11-23T09:00:00-05:00", ["Dana"])
    dana = store.person_id("Dana")

    assert client.put(
        f"/api/people/{dana}/client", headers=AUTH, json={"client": " Contoso "}
    ).json() == {"client": "Contoso"}
    assert (
        client.put("/api/people/999/client", headers=AUTH, json={"client": "X"}).status_code == 404
    )
    too_long = client.put(f"/api/people/{dana}/client", headers=AUTH, json={"client": "x" * 81})
    assert too_long.status_code == 400
    assert client.get("/api/clients", headers=AUTH).json() == ["Contoso"]

    listed = client.get("/api/meetings", headers=AUTH).json()[0]
    assert (listed["client"], listed["client_inferred"]) == ("Contoso", True)
    detail = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert detail["clients_heard"] == ["Contoso"] and detail["clients_known"] == ["Contoso"]

    changed = client.patch(f"/api/meetings/{meeting_id}", headers=AUTH, json={"client": "Fabrikam"})
    assert (changed.json()["client"], changed.json()["client_inferred"]) == ("Fabrikam", False)
    back = client.patch(f"/api/meetings/{meeting_id}", headers=AUTH, json={"client": ""})
    assert (back.json()["client"], back.json()["client_inferred"]) == ("Contoso", True)

    log = client.get(
        "/api/worklog", headers=AUTH, params={"day": "2026-11-23", "client": "Fabrikam"}
    )
    assert log.json()["totals"]["meetings"] == 0
    store.close()
