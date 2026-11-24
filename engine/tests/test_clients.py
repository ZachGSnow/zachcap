"""Clients: from the people in a meeting, or set on it, and carried with it."""

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, clients, digest, export, pipeline
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}


def meeting(store: Store, folder: Path, title: str, started: str, people=()) -> int:
    """A meeting with these people named in it, each saying one line: a name, or (name,
    status) for one recognized ("auto") or only suggested."""
    meeting_id = store.create_meeting(title, started, folder / title, duration=600)
    for at, who in enumerate(people):
        name, status = who if isinstance(who, tuple) else (who, "confirmed")
        person = store.person_id(name)
        speaker = store.add_speaker(
            meeting_id, "system", name, None, 5.0, None, Match(person, 1.0, status)
        )
        store.add_segments(meeting_id, [(speaker, "system", at * 5.0, at * 5.0 + 4, "Hi.", 0.9)])
    store.refresh_status(meeting_id)
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
        "kept": False,
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


def test_only_meetings_where_they_count_keep_their_client(store: Store, tmp_path: Path):
    """Moving Dana on keeps the meetings she counts in (named, or recognized by voice, even
    in one that still needs other names), not one where she was only suggested, which still
    follows its people. You never count, so a change of yours keeps nothing."""
    maybe = meeting(
        store, tmp_path, "Maybe Dana", "2026-11-24T09:00:00-05:00", ["Mark", ("Dana", "suggested")]
    )
    heard = meeting(
        store, tmp_path, "Dana by voice", "2026-11-24T10:00:00-05:00", [("Dana", "auto"), "Mark"]
    )
    yours = meeting(store, tmp_path, "With you", "2026-11-24T11:00:00-05:00", ["You", "Mark"])
    unnamed = meeting(
        store,
        tmp_path,
        "Not all named",
        "2026-11-24T12:00:00-05:00",
        ["Dana", ("Lee", "suggested")],
    )
    assert store.meeting(maybe)["status"] == store.meeting(unnamed)["status"] == "needs_names"

    kept = clients.keep_history(store, store.person_id("Dana"))

    assert sorted(kept) == sorted([store.ids["cutover"], heard, unnamed])
    found = clients.of_meeting(store, store.meeting(maybe))
    assert (found["client"], found["inferred"], found["kept"]) == ("Contoso", True, False)
    assert clients.keep_history(store, store.me()["id"]) == []
    assert clients.of_meeting(store, store.meeting(yours))["kept"] is False


def test_a_kept_client_is_told_apart_from_its_peoples(store: Store):
    dana = store.person_id("Dana")
    clients.keep_history(store, dana)
    store.set_client(dana, "Fabrikam")

    found = clients.of_meeting(store, store.meeting(store.ids["cutover"]))

    # Not from its people as they are now: Dana works for Fabrikam.
    assert (found["client"], found["inferred"], found["set"]) == ("Contoso", False, False)
    assert found["kept"] is True
    assert sorted(found["clients"]) == ["Contoso", "Fabrikam"]


def test_reprocessing_lets_a_kept_client_follow_its_people_again(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    """A reprocess finds the meeting's people again, so a client kept from the old ones
    goes; one you set stays."""
    from zachcap.diarize import Diarization
    from zachcap.transcript import Turn, Word

    ids = store.ids
    dana = store.person_id("Dana")
    clients.keep_history(store, dana)
    store.set_client(dana, "Fabrikam")
    store.update_meeting(ids["both"], client="Northwind", client_source=clients.YOURS)
    monkeypatch.setattr(pipeline, "_load_tracks", lambda folder, log: {"system": np.zeros(16000)})
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: {"system": [Word(0.0, 0.5, " Hi.")]})
    monkeypatch.setattr(
        pipeline,
        "_diarize",
        lambda tracks, words, log: ({"system": Diarization([Turn(0.0, 0.5, "system")], {})}, False),
    )

    for meeting_id in (ids["cutover"], ids["both"]):
        pipeline.process(store, meeting_id, log=lambda message: None)

    assert store.meeting(ids["cutover"])["client_source"] is None
    cutover = clients.of_meeting(store, store.meeting(ids["cutover"]))
    assert (cutover["client"], cutover["kept"]) == (None, False)  # nobody named in it yet
    assert clients.of_meeting(store, store.meeting(ids["both"]))["client"] == "Northwind"


def test_you_never_have_a_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    call = meeting(store, tmp_path, "Call", "2026-11-23T09:00:00-05:00", ["You", "Dana"])
    store.set_client(store.person_id("Dana"), "Contoso")
    me = store.me()["id"]

    refused = client.put(f"/api/people/{me}/client", headers=AUTH, json={"client": "Fabrikam"})

    assert refused.status_code == 400
    cleared = client.put(f"/api/people/{me}/client", headers=AUTH, json={"client": ""})
    assert cleared.status_code == 200
    assert store.person(me)["org"] is None and store.meeting(call)["client_source"] is None
    store.close()


def test_a_meeting_can_follow_its_people_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    call = meeting(store, tmp_path, "Cutover", "2026-11-23T09:00:00-05:00", ["Dana"])
    store.set_client(store.person_id("Dana"), "Contoso")
    url = f"/api/meetings/{call}"

    client.patch(url, headers=AUTH, json={"client": "Fabrikam"})
    renamed = client.patch(url, headers=AUTH, json={"title": "Cutover call"}).json()
    assert (renamed["client"], renamed["client_set"]) == ("Fabrikam", True)  # left as it was
    back = client.patch(url, headers=AUTH, json={"client": None}).json()

    assert (back["client"], back["client_inferred"], back["client_set"]) == ("Contoso", True, False)
    assert back["client_kept"] is False
    assert store.meeting(call)["client_source"] is None
    store.close()


def test_changing_only_the_capitals_renames_the_client_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    first = meeting(store, tmp_path, "Cutover", "2026-11-23T09:00:00-05:00", ["Dana", "Mark"])
    second = meeting(store, tmp_path, "Review", "2026-11-23T11:00:00-05:00")
    dana, mark, lee = (store.person_id(name) for name in ("Dana", "Mark", "Lee"))
    for person in (dana, mark):
        client.put(f"/api/people/{person}/client", headers=AUTH, json={"client": "contoso"})
    client.patch(f"/api/meetings/{second}", headers=AUTH, json={"client": "contoso"})

    fixed = client.put(f"/api/people/{dana}/client", headers=AUTH, json={"client": "Contoso"})

    assert fixed.json() == {"client": "Contoso"}
    assert store.person(mark)["org"] == "Contoso" and store.meeting(second)["client"] == "Contoso"
    assert client.get("/api/clients", headers=AUTH).json() == ["Contoso"]
    # From a meeting whose client comes from its people: it still does, renamed.
    shown = client.patch(f"/api/meetings/{first}", headers=AUTH, json={"client": "CONTOSO"})
    assert (shown.json()["client"], shown.json()["client_inferred"]) == ("CONTOSO", True)
    assert store.person(dana)["org"] == "CONTOSO" and store.meeting(second)["client"] == "CONTOSO"
    # Someone new typed in other capitals takes the spelling in use and renames nothing.
    client.put(f"/api/people/{lee}/client", headers=AUTH, json={"client": "contoso"})
    assert store.person(lee)["org"] == "CONTOSO" and store.person(dana)["org"] == "CONTOSO"
    store.close()


def test_merging_people_leaves_their_meetings_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    together = meeting(store, tmp_path, "Review", "2026-11-23T09:00:00-05:00", ["Mark", "Lee"])
    alone = meeting(store, tmp_path, "One to one", "2026-11-23T10:00:00-05:00", ["Mark"])
    smiths = meeting(store, tmp_path, "Smith call", "2026-11-23T11:00:00-05:00", ["Mark Smith"])
    smith, lee = store.person_id("Mark Smith"), store.person_id("Lee")
    client.put(f"/api/people/{lee}/client", headers=AUTH, json={"client": "Fabrikam"})
    client.put(f"/api/people/{smith}/client", headers=AUTH, json={"client": "Contoso"})

    client.patch(f"/api/people/{smith}", headers=AUTH, json={"name": "Mark"})  # a merge

    assert store.person(store.person_id("Mark"))["org"] == "Contoso"  # the client went along
    found = clients.of_meetings(store, store.meetings())
    assert found[together]["client"] == "Fabrikam" and found[together]["kept"] is True
    assert found[alone]["client"] == "Contoso"  # it had none: filled in
    assert found[smiths]["client"] == "Contoso"
    store.close()


def test_merging_with_you_gives_you_no_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    dana_call = meeting(store, tmp_path, "Dana call", "2026-11-23T09:00:00-05:00", ["Dana"])
    lee_call = meeting(store, tmp_path, "Lee call", "2026-11-23T10:00:00-05:00", ["Lee"])
    dana, lee = store.person_id("Dana"), store.person_id("Lee")
    client.put(f"/api/people/{dana}/client", headers=AUTH, json={"client": "Contoso"})
    client.put(f"/api/people/{lee}/client", headers=AUTH, json={"client": "Fabrikam"})

    client.patch(f"/api/people/{dana}", headers=AUTH, json={"name": "You"})  # Dana was you

    assert store.me()["org"] is None
    found = clients.of_meeting(store, store.meeting(dana_call))
    assert found["client"] == "Contoso" and found["kept"] is True  # you don't count

    me = store.me()["id"]
    client.patch(f"/api/people/{me}", headers=AUTH, json={"name": "Lee"})  # you were Lee

    assert (store.me()["name"], store.me()["org"]) == ("Lee", None)
    found = clients.of_meeting(store, store.meeting(lee_call))
    assert found["client"] == "Fabrikam" and found["kept"] is True
    assert client.get("/api/clients", headers=AUTH).json() == ["Contoso", "Fabrikam"]
    store.close()


def test_saved_client_work_logs_forget_deleted_and_redacted_meetings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    cutover = meeting(store, tmp_path, "Cutover", "2026-11-23T09:00:00-05:00", ["Dana"])
    review = meeting(store, tmp_path, "Review", "2026-11-23T11:00:00-05:00", ["Dana"])
    prospect = meeting(store, tmp_path, "Prospect", "2026-11-23T14:00:00-05:00")
    store.set_client(store.person_id("Dana"), "Contoso")
    store.update_meeting(prospect, client="Northwind", client_source=clients.YOURS)
    said = {"overview": "Moving the vault server.", "decisions": [], "open_questions": []}
    store.save_summary(review, "claude-opus-5-5", said, 1, 1, 0.0)
    folder = tmp_path / "exports"
    folder.mkdir()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(folder)})
    mine = folder / "Work log 2026-11-23 Notes.md"  # a file of your own, never touched
    mine.write_text("# My notes\n", encoding="utf-8")
    for name in ("Contoso", "Northwind"):
        for period in ("day", "week"):
            saved = {"day": "2026-11-23", "period": period, "client": name}
            assert client.post("/api/worklog/save", headers=AUTH, json=saved).status_code == 200
    day = folder / "Work log 2026-11-23 Contoso.md"
    assert "Cutover" in day.read_text(encoding="utf-8")

    client.delete(f"/api/meetings/{cutover}", headers=AUTH)
    client.delete(f"/api/meetings/{prospect}", headers=AUTH)  # Northwind's only meeting
    client.post(f"/api/meetings/{review}/redact", headers=AUTH, json={"start": 0, "end": 1})

    for log in (day, folder / "Work log 2026-W48 Contoso.md"):
        text = log.read_text(encoding="utf-8")
        assert "Cutover" not in text and "vault server" not in text and "Review" in text
    northwind = (folder / "Work log 2026-11-23 Northwind.md").read_text(encoding="utf-8")
    assert "Prospect" not in northwind and "No meetings recorded." in northwind
    assert mine.read_text(encoding="utf-8") == "# My notes\n"
    store.close()


def test_a_clients_name_keeps_its_words_apart():
    assert clients.checked("Contoso\u00a0Ltd") == "Contoso Ltd"  # a non-breaking space
    assert clients.checked(" Contoso\tLtd\u2003Group\n") == "Contoso Ltd Group"
    assert clients.checked("Con\u200btoso\x07") == "Contoso"  # characters that don't print
    with pytest.raises(ValueError):
        clients.checked("x" * 81)


def test_keeping_history_is_one_change(store: Store, tmp_path: Path):
    """However many meetings keep their client, it's one transaction: it was a commit per
    meeting (about 2 s for 1,500)."""
    for number in range(20):
        meeting(store, tmp_path, f"Call {number}", "2026-11-24T09:00:00-05:00", ["Dana"])
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)

    kept = clients.keep_history(store, store.person_id("Dana"))

    store.db.set_trace_callback(None)
    assert len(kept) == 21  # and the cutover
    assert statements.count("COMMIT") == 1


def test_a_change_that_fails_halfway_keeps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL, raise_server_exceptions=False)
    store = Store()
    call = meeting(store, tmp_path, "Call", "2026-11-23T09:00:00-05:00", ["Dana"])
    dana = store.person_id("Dana")
    store.set_client(dana, "Contoso")

    def broken(self, person_id, client):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "set_client", broken)
    failed = client.put(f"/api/people/{dana}/client", headers=AUTH, json={"client": "Fabrikam"})

    assert failed.status_code == 500
    assert store.meeting(call)["client_source"] is None  # kept nothing: all of it or none
    store.close()


def test_cached_clients_are_spelled_as_written_now(store: Store, tmp_path: Path):
    """The clients per meeting are kept until a line, a voice or a person changes; how a
    client is written can change without any of those, and shows at once."""
    ids = store.ids
    assert clients.of_meeting(store, store.meeting(ids["cutover"]))["clients"] == ["Contoso"]
    spare = meeting(store, tmp_path, "Spare", "2026-11-24T09:00:00-05:00")
    for meeting_id in (ids["nobody"], spare):  # as an older version may have written them
        store.update_meeting(meeting_id, client="contoso", client_source=clients.YOURS)

    assert clients.of_meeting(store, store.meeting(ids["cutover"]))["clients"] == ["contoso"]

    listed = store.participants()
    named = list(listed[ids["cutover"]])
    listed[ids["cutover"]].append("Somebody")  # what's handed out is a copy
    assert store.participants()[ids["cutover"]] == named
