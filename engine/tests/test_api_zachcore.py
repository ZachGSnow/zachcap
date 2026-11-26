"""Settings and a meeting's menu for ZachCore, through the engine's API: where meetings go, with
which key (never given back), what goes, how many wait and why; Send now, Take back, Take
everything back, older meetings, a client none of whose meetings go, and another server."""

import json
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_zachcore_message import call, summarized
from zachcore_fake import Fake

from zachcap import apikeys
from zachcap.api import create_app
from zachcap.store import Store

AUTH = {"Authorization": "Bearer test-token"}
ADDRESS = "https://notes.example.com"


@pytest.fixture
def keys(monkeypatch) -> dict:
    """Credential Manager, as the engine sees it."""
    saved: dict = {}
    monkeypatch.setattr(apikeys, "zachcore", lambda address: saved.get(address))
    monkeypatch.setattr(
        apikeys, "store_zachcore", lambda address, key: saved.update({address: key})
    )
    return saved


@pytest.fixture
def client(tmp_path: Path, monkeypatch, keys) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app("test-token"), base_url="http://127.0.0.1")


@pytest.fixture
def store(client):
    store = Store()
    yield store
    store.close()


def put(client: TestClient, **change):
    return client.put("/api/zachcore", headers=AUTH, json=change)


def sent_to(store: Store, uid: str, target: str) -> None:
    store.db.execute("UPDATE sends SET target = ? WHERE uid = ?", (target, uid))
    store.db.commit()


def whats(store: Store, uid: str) -> list[str]:
    rows = store.db.execute("SELECT what FROM outbox WHERE uid = ? ORDER BY what", (uid,))
    return [row["what"] for row in rows]


def test_off_until_turned_on_with_the_usual_switches(client):
    status = client.get("/api/zachcore", headers=AUTH).json()
    assert status["on"] is False and status["address"] is None and status["key"] is False
    assert status["parts"] == {
        "summaries": True,
        "tasks": True,
        "facts": True,
        "transcripts": False,
    }
    assert status["space"] == "work" and status["waiting"] == 0 and status["never"] == []


@pytest.mark.parametrize(
    "address", ["http://notes.example.com", "ftp://x.example", "notes.example.com", "https://a@b.c"]
)
def test_an_address_is_https_or_this_pc(client, address):
    assert put(client, address=address).status_code == 400


def test_the_key_is_kept_for_its_address_and_never_given_back(client, keys, store):
    key = f"zachcap.{secrets.token_hex(16)}"
    assert put(client, key=key).status_code == 400  # no address yet
    assert put(client, on=True).status_code == 400  # nor a key
    for bad in ("", " ", "two words"):
        assert put(client, address=ADDRESS, key=bad).status_code == 400
    answers = [put(client, address=ADDRESS, key=key), put(client, on=True)]
    assert keys == {ADDRESS: key}
    m = call(store)
    answers += [
        client.get("/api/zachcore", headers=AUTH),
        client.get(f"/api/meetings/{m}", headers=AUTH),
        client.get("/api/settings", headers=AUTH),
    ]
    assert all(answer.status_code == 200 for answer in answers)
    assert all(key not in answer.text for answer in answers)
    assert answers[1].json()["on"] is True and answers[1].json()["key"] is True
    settings = json.dumps([tuple(row) for row in store.db.execute("SELECT * FROM settings")])
    assert key not in settings


def test_what_goes_and_whose_meetings_never(client, store):
    parts = {"summaries": False, "tasks": True, "facts": False, "transcripts": True}
    status = put(client, parts=parts, never=["Contoso", " contoso ", "Fabrikam"]).json()
    assert status["parts"] == parts and status["never"] == ["contoso", "Fabrikam"]
    assert put(client, never=["x" * 81]).status_code == 400


def test_a_client_set_to_never_says_how_many_are_there_and_takes_them_back(client, store):
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    m = call(store)  # Contoso's
    sent_to(store, "meeting-1", f"work {ADDRESS}")
    answer = put(client, never=["Contoso"]).json()
    assert answer["never_there"] == {"Contoso": 1}  # asked whether to take it back
    taken = client.post("/api/zachcore/take-back", headers=AUTH, json={"client": "Contoso"})
    assert taken.json() == {"count": 1} and whats(store, "meeting-1") == ["recall"]
    sent = client.post(f"/api/meetings/{m}/zachcore/send", headers=AUTH)
    assert sent.status_code == 400 and "Contoso" in sent.json()["detail"]


def test_send_now_and_take_back_from_a_meetings_menu(client, store):
    put(client, address=ADDRESS, key="zachcap.k")
    m = call(store)
    assert client.post(f"/api/meetings/{m}/zachcore/send", headers=AUTH).status_code == 400  # off
    put(client, on=True)
    status = client.post(f"/api/meetings/{m}/zachcore/send", headers=AUTH).json()
    assert status["waiting"] and not status["there"] and whats(store, "meeting-1") == ["send"]
    row = store.db.execute("SELECT quiet_until FROM outbox WHERE uid = 'meeting-1'").fetchone()
    now = store.db.execute("SELECT CAST(strftime('%s', 'now') AS INTEGER)").fetchone()[0]
    assert row["quiet_until"] <= now  # at once, not after the half minute
    sent_to(store, "meeting-1", f"work {ADDRESS}")
    status = client.post(f"/api/meetings/{m}/zachcore/take-back", headers=AUTH).json()
    assert status["taken"] and status["taking_back"] and not status["waiting"]
    assert whats(store, "meeting-1") == ["recall"]
    detail = client.get(f"/api/meetings/{m}", headers=AUTH).json()["zachcore"]
    assert detail["there"] and detail["can_take_back"] and detail["place"]["address"] == ADDRESS
    store.update_meeting(m, private=1)
    refused = client.post(f"/api/meetings/{m}/zachcore/send", headers=AUTH)
    assert refused.status_code == 400 and "private" in refused.json()["detail"]


def test_take_everything_back(client, store):
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    call(store)
    call(store, "meeting-2")
    sent_to(store, "meeting-1", f"work {ADDRESS}")
    taken = client.post("/api/zachcore/take-back", headers=AUTH, json={}).json()
    assert taken == {"count": 1}  # the one there; the other won't go either
    assert whats(store, "meeting-1") == ["recall"] and whats(store, "meeting-2") == []
    assert {
        row["taken"]
        for row in store.db.execute("SELECT taken FROM sends WHERE uid LIKE 'meeting-%'")
    } == {1}


def test_older_meetings_go_only_when_asked(client, store):
    m = call(store)  # recorded before sending was on: not wanted
    store.db.execute("UPDATE sends SET wanted = 0")
    store.db.execute("DELETE FROM outbox")
    store.db.commit()
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    assert client.get("/api/zachcore/older", headers=AUTH).json() == {"count": 1}
    assert client.get("/api/zachcore/older?since=2027-01-01", headers=AUTH).json() == {"count": 0}
    assert client.get("/api/zachcore/older?since=soon", headers=AUTH).status_code == 400
    assert client.post("/api/zachcore/older", headers=AUTH, json={}).json() == {"count": 1}
    assert whats(store, "meeting-1") == ["send"] and m
    assert client.get("/api/zachcore/older", headers=AUTH).json() == {"count": 0}


@pytest.mark.parametrize("before", ["take_back", "leave"])
def test_another_server_asks_once_what_of_the_meetings_there(client, store, before):
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    m = call(store)
    summarized(store, m)
    old = f"work {ADDRESS}"
    sent_to(store, "meeting-1", old)
    store.db.execute("DELETE FROM outbox")
    store.db.commit()
    asked = client.get(
        "/api/zachcore/before?address=https://other.example.com&space=work", headers=AUTH
    ).json()
    assert asked == {"before": {"place": old, "address": ADDRESS, "space": "work", "count": 1}}
    refused = put(client, address="https://other.example.com")
    assert refused.status_code == 409 and "take them back" in refused.json()["detail"]
    same = client.get(f"/api/zachcore/before?address={ADDRESS}&space=work", headers=AUTH)
    assert same.json() == {"before": None}
    assert client.get("/api/zachcore", headers=AUTH).json()["address"] == ADDRESS  # not yet
    answer = put(client, address="https://other.example.com", before=before)
    assert answer.status_code == 200
    target = store.db.execute("SELECT target FROM sends WHERE uid = 'meeting-1'").fetchone()[0]
    if before == "take_back":  # from there first; then it goes to the new one as new
        assert whats(store, "meeting-1") == ["recall", "send"] and target == old
    else:  # left there: it goes to the new one as new
        assert whats(store, "meeting-1") == ["send"] and target is None


def test_a_meeting_taken_back_from_the_old_server_stays_to_be_taken_back(client, store):
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    m = call(store)
    sent_to(store, "meeting-1", f"work {ADDRESS}")
    store.update_meeting(m, private=1)  # its recall waits
    put(client, space="home", before="leave")
    assert whats(store, "meeting-1") == ["recall"]
    target = store.db.execute("SELECT target FROM sends WHERE uid = 'meeting-1'").fetchone()[0]
    assert target == f"work {ADDRESS}"  # never dropped


def test_the_connection_test_asks_the_server_and_says_how_it_went(client, keys):
    fake = Fake()
    try:
        ok = client.post(
            "/api/zachcore/test", headers=AUTH, json={"address": fake.address, "key": fake.key}
        )
        assert ok.json() == {"ok": True, "status": 200}
        wrong = client.post(
            "/api/zachcore/test", headers=AUTH, json={"address": fake.address, "key": "zachcap.no"}
        ).json()
        assert wrong["ok"] is False and wrong["status"] == 401 and "key" in wrong["error"]
        keys[fake.address] = fake.key  # saved: it needn't be typed again
        saved = client.post("/api/zachcore/test", headers=AUTH, json={"address": fake.address})
        assert saved.json()["ok"] and fake.key not in saved.text
        space = client.post(
            "/api/zachcore/test", headers=AUTH, json={"address": fake.address, "space": "nowhere"}
        ).json()
        assert space["status"] == 404
    finally:
        fake.close()
    gone = client.post(
        "/api/zachcore/test", headers=AUTH, json={"address": fake.address, "key": "k"}
    )
    assert gone.json()["status"] is None and gone.json()["error"] == "No answer from that address"


def test_how_many_wait_and_why(client, store):
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    call(store)
    store.db.execute("DELETE FROM outbox WHERE uid NOT LIKE 'meeting-%'")  # the call's own
    store.db.commit()
    status = client.get("/api/zachcore", headers=AUTH).json()
    assert status["waiting"] == 1 and status["why"] == {"kind": "quiet"}
    put(client, on=False)  # what waits stays, and doesn't go
    status = client.get("/api/zachcore", headers=AUTH).json()
    assert status["waiting"] == 1 and status["why"] == {"kind": "off"}
    put(client, on=True)
    store.set_setting("zachcore_paused_until", "9999999999")
    assert client.get("/api/zachcore", headers=AUTH).json()["why"] == {
        "kind": "paused", "until": 9999999999.0
    }  # fmt: skip
    store.set_setting("zachcore_stopped", json.dumps({"place": f"work {ADDRESS}", "status": 401}))
    assert client.get("/api/zachcore", headers=AUTH).json()["why"] == {
        "kind": "refused", "status": 401
    }  # fmt: skip
    status = put(client, key="zachcap.other").json()  # another key: it is tried again
    assert status["why"]["kind"] == "paused"
    store.db.execute("UPDATE sends SET target = 'work https://old.example.com'")
    store.db.commit()
    status = client.get("/api/zachcore", headers=AUTH).json()
    assert status["elsewhere"][0]["address"] == "https://old.example.com"


def test_a_meeting_that_becomes_a_never_clients_after_it_went_is_asked_about(client, store):
    """Only clients added to "never" were asked about; a meeting can become one's later (a
    person's company, the meeting's client), and its copy stayed in ZachCore unasked."""
    put(client, address=ADDRESS, key="zachcap.k", on=True)
    m = call(store)
    store.update_meeting(m, client="Northwind", client_source="user")  # Northwind's when it went
    sent_to(store, "meeting-1", f"work {ADDRESS}")
    put(client, never=["Contoso"])

    def asked() -> dict:
        return client.get("/api/zachcore", headers=AUTH).json()["never_there"]

    assert asked() == {}
    store.update_meeting(m, client="Contoso", client_source="user")  # now it is Contoso's
    assert asked() == {"Contoso": 1}
    kept = client.post("/api/zachcore/keep", headers=AUTH, json={"client": "contoso"})
    assert kept.json() == {"kept": 1} and asked() == {}  # kept there, as chosen

    other = call(store, "meeting-2")
    store.update_meeting(other, client="Contoso", client_source="user")
    sent_to(store, "meeting-2", f"work {ADDRESS}")
    assert asked() == {"Contoso": 2}  # one more there since: asked again
    client.post("/api/zachcore/take-back", headers=AUTH, json={"client": "Contoso"})
    assert asked() == {}  # being taken back
