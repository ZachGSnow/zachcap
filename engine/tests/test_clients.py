"""Clients: from the people in a meeting, or set on it, and carried with it."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, clients, digest, export
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

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
    assert found[ids["cutover"]] == {
        "client": "Contoso",
        "inferred": True,
        "set": False,
        "clients": ["Contoso"],
    }
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


def test_a_library_not_yet_opened_to_write_still_lists(store: Store, tmp_path: Path):
    """The MCP server reads without adding columns: a library from before tickets and
    clients still lists and shows its meetings."""
    for column in ("ticket", "client", "ticket_source"):
        store.db.execute(f"ALTER TABLE meetings DROP COLUMN {column}")
    store.db.commit()
    old = Store(tmp_path / "test.db", readonly=True)
    try:
        assert "Cutover" in assistant.meetings_list(old)
        assert assistant.meeting_detail(old, store.ids["cutover"]).startswith(
            f"M{store.ids['cutover']}"
        )
        assert digest.build(old, "day", date(2026, 11, 23))["totals"]["meetings"] == 3
    finally:
        old.close()


def test_clients_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
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
    none = client.patch(f"/api/meetings/{meeting_id}", headers=AUTH, json={"client": ""})
    assert (none.json()["client"], none.json()["client_set"]) == (None, True)  # "no client"
    again = client.patch(f"/api/meetings/{meeting_id}", headers=AUTH, json={"client": "contoso"})
    assert again.json()["client"] == "Contoso"  # the spelling already in use

    log = client.get(
        "/api/worklog", headers=AUTH, params={"day": "2026-11-23", "client": "Fabrikam"}
    )
    assert log.json()["totals"]["meetings"] == 0
    store.close()


def test_a_meeting_keeps_its_client_when_its_people_move_on(store: Store, tmp_path: Path):
    ids = store.ids
    dana = store.person_id("Dana")
    clients.keep_history(store, dana)  # what moving Dana to another client does first
    store.set_client(dana, "Fabrikam")
    assert clients.of_meeting(store, store.meeting(ids["cutover"]))["client"] == "Contoso"
    later = meeting(store, tmp_path, "Later", "2026-11-24T09:00:00-05:00", ["Dana"])
    assert clients.of_meeting(store, store.meeting(later))["client"] == "Fabrikam"


def test_one_spelling_per_client_whoever_speaks_first(store: Store, tmp_path: Path):
    # Dana "Contoso" and Mark "contoso" (the fixture): one client, spelled the common way.
    store.set_client(store.person_id("Lee"), "CONTOSO")
    early = meeting(store, tmp_path, "Mark first", "2026-11-24T09:00:00-05:00", ["Mark", "Dana"])
    assert clients.of_meeting(store, store.meeting(early))["client"] == "Contoso"
    assert clients.known(store) == ["Contoso"]
    store.set_client(store.person_id("Lee"), "\u00c9lectricit\u00e9 Nord")
    store.set_client(store.person_id("Mark"), "\u00e9lectricit\u00e9 nord")
    joint = meeting(store, tmp_path, "Lee and Mark", "2026-11-24T11:00:00-05:00", ["Lee", "Mark"])
    assert clients.of_meeting(store, store.meeting(joint))["clients"] == [
        "\u00c9lectricit\u00e9 Nord"
    ]
    assert clients.matches({"client": "Contoso"}, "") and clients.matches({"client": None}, " ")


def test_merging_removing_and_saving_keep_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    first = meeting(store, tmp_path, "Cutover", "2026-11-23T09:00:00-05:00", ["Mark Smith"])
    second = meeting(store, tmp_path, "Review", "2026-11-23T11:00:00-05:00", ["Lee"])
    smith, lee = store.person_id("Mark Smith"), store.person_id("Lee")
    client.put(f"/api/people/{smith}/client", headers=AUTH, json={"client": "Contoso"})
    store.person_id("Mark")  # someone already called Mark, with no client

    client.patch(f"/api/people/{smith}", headers=AUTH, json={"name": "Mark"})  # a merge
    assert store.person(store.person_id("Mark"))["org"] == "Contoso"  # the client went along
    assert client.get("/api/meetings", headers=AUTH).json()[-1]["client"] == "Contoso"

    client.put(f"/api/people/{lee}/client", headers=AUTH, json={"client": "Fabrikam"})
    client.delete(f"/api/people/{lee}", headers=AUTH)
    review = next(m for m in client.get("/api/meetings", headers=AUTH).json() if m["id"] == second)
    assert review["client"] == "Fabrikam"  # removed, but the meeting keeps its client

    folder = tmp_path / "exports"
    folder.mkdir()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(folder)})
    saved = client.post(
        "/api/worklog/save", headers=AUTH, json={"day": "2026-11-23", "client": "Fabrikam"}
    ).json()["path"]
    assert Path(saved).name == "Work log 2026-11-23 Fabrikam.md"
    text = Path(saved).read_text(encoding="utf-8")
    assert "Review" in text and "Cutover" not in text  # only that client's meetings
    assert first
    store.close()
