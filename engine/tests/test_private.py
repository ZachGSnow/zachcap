"""Private meetings: nothing in one is sent to Claude, by any of the ways there are. The page
goes on showing it like any other."""

import asyncio
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, capture, digest, mcp_server, pipeline, summarize
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)
AUTH = {"Authorization": "Bearer test-token"}
# What only the private meeting has: nothing Claude gets may hold any of it.
ONLY_PRIVATE = ("breach", "Breach", "Priya", "Incident bridge", "INC-4471", "Fabrikam", "10:00")


def add_meeting(store: Store, title: str, started: str, name: str, line: str) -> int:
    meeting = store.create_meeting(title, started, Path("somewhere"))
    store.update_meeting(meeting, status="ready", duration=600.0)
    voice = Match(store.person_id(name), 1.0, "confirmed")
    speaker = store.add_speaker(meeting, "system", name, None, 10.0, None, voice)
    store.add_segments(meeting, [(speaker, "system", 61.0, 64.0, line, 0.9)])
    overview = f"{name} talked about it."
    store.save_summary(
        meeting, "claude-opus-5-5", {"title": title, "overview": overview}, 1, 1, 0.0
    )
    item = {"owner": name, "task": f"Follow up on: {line}", "said_at": 61.0}
    store.replace_action_items(meeting, [item])
    return meeting


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    shared = add_meeting(
        store, "Cutover sync", "2026-11-20T09:00:00", "Dana", "The notice goes out Tuesday."
    )
    private = add_meeting(
        store, "Incident bridge", "2026-11-20T10:00:00", "Priya", "The breach began on Sunday."
    )
    store.update_meeting(private, private=1, ticket="INC-4471", client="Fabrikam")
    # A person in both, and what was said of the private one in a note.
    for meeting in (shared, private):
        voice = Match(store.person_id("Marcus"), 1.0, "confirmed")
        speaker = store.add_speaker(meeting, "system", "Marcus", None, 10.0, None, voice)
        store.add_segments(meeting, [(speaker, "system", 70.0, 130.0, "I agree.", 0.9)])
    store.add_note(private, 5.0, "note", "The breach is contained")
    yield store, shared, private
    store.close()


def said_to_claude(store: Store, private: int) -> dict[str, str]:
    """All that the library tells Claude, through Ask's tools and the MCP server's."""
    tools = {tool.name: tool for tool in assistant.make_tools(store)}
    asked = {
        "search_transcripts": [{"query": "breach Sunday agree"}],
        "list_meetings": [{}, {"client": "Fabrikam"}, {"ticket": "INC-4471"}],
        "get_meeting": [{"meeting_id": private}],
        "list_action_items": [{"status": "all"}, {"owner": "Priya"}],
        "get_work_log": [{"day": "2026-11-20"}, {"day": "2026-11-20", "period": "week"}],
    }
    asked["search_transcripts"].append({"query": "breach", "meeting_id": private})
    said = {f"{name} {a}": tools[name].call(a) for name, calls in asked.items() for a in calls}
    server = mcp_server.create_server()
    asked["search_meetings"] = asked.pop("search_transcripts")
    asked["list_people"] = [{}]
    for name, calls in asked.items():
        for arguments in calls:
            result = asyncio.run(server.call_tool(name, arguments))
            said[f"mcp {name} {arguments}"] = result.content[0].text
    return said


def test_the_library_claude_reads_has_no_private_meeting(library):
    store, shared, private = library

    said = said_to_claude(store, private)

    for what, text in said.items():
        for word in ONLY_PRIVATE:
            assert word not in text, (what, text)
    assert "I agree." in said["search_transcripts {'query': 'breach Sunday agree'}"]
    kept = f"M{private} is a private meeting: the user keeps it from Claude."
    assert said[f"get_meeting {{'meeting_id': {private}}}"] == kept
    assert said[f"mcp search_meetings {{'query': 'breach', 'meeting_id': {private}}}"] == kept
    # What was left out is said, so an answer doesn't claim there is nothing more.
    left_out = "\n(1 private meeting isn't listed: the user keeps them from Claude.)"
    assert said["list_meetings {}"].endswith(f"</meetings>{left_out}")
    assert said["list_meetings {'client': 'Fabrikam'}"] == "No meetings match." + left_out
    log = said["get_work_log {'day': '2026-11-20'}"]
    assert log.endswith(
        "</work_log>\n(1 private meeting isn't in it: the user keeps them from Claude.)"
    )
    assert "Cutover sync" in log and "0.25 h to bill" in log
    # People: as if the private meeting weren't there.
    people = said["mcp list_people {}"]
    assert "Dana: 1 meetings" in people and "Marcus: 1 meetings, 1 min" in people

    store.update_meeting(private, private=0)
    said = said_to_claude(store, private)
    assert "The breach began on Sunday." in said[f"get_meeting {{'meeting_id': {private}}}"]
    assert "Priya: 1 meetings" in said["mcp list_people {}"]
    assert "private" not in said["list_meetings {}"] + said["get_work_log {'day': '2026-11-20'}"]


@pytest.mark.parametrize(
    "send",
    [
        lambda store, meeting: summarize.summarize(store, meeting),
        lambda store, meeting: summarize.recipe(store, meeting, "email"),
        lambda store, meeting: summarize.ask(store, meeting, "What happened?"),
    ],
    ids=["summary", "document", "question"],
)
@pytest.mark.parametrize("key", ["test-key", None])
def test_nothing_of_a_private_meeting_is_sent(library, monkeypatch, send, key):
    store, shared, private = library

    def sent(**options):
        raise AssertionError("a client was made")

    monkeypatch.setattr(summarize.anthropic, "Anthropic", sent)
    monkeypatch.setattr(summarize.apikeys, "get", lambda name: key)

    with pytest.raises(RuntimeError, match="This meeting is private"):
        send(store, private)


def test_the_page_shows_a_private_meeting_like_any_other(library):
    store, shared, private = library
    client = TestClient(create_app("test-token"), base_url=LOCAL)

    listed = {m["id"]: m for m in client.get("/api/meetings", headers=AUTH).json()}
    assert (listed[private]["private"], listed[shared]["private"]) == (True, False)
    detail = client.get(f"/api/meetings/{private}", headers=AUTH).json()
    assert detail["meeting"]["private"] is True and detail["segments"]
    found = client.get("/api/search?q=breach", headers=AUTH).json()
    assert [row["meeting_id"] for row in found] == [private]
    log = digest.build(store, "day", date(2026, 11, 20))
    assert log["totals"]["meetings"] == 2
    owners = {i["owner"] for i in client.get("/api/actions", headers=AUTH).json()["items"]}
    assert owners == {"Dana", "Priya"}
    assert {p["name"] for p in store.people_stats()} == {"You", "Dana", "Priya", "Marcus"}

    refused = client.post(f"/api/meetings/{private}/summary", headers=AUTH, json={})
    assert refused.status_code == 400 and "private" in refused.json()["detail"]
    changed = client.patch(f"/api/meetings/{private}", headers=AUTH, json={"private": False})
    assert changed.json()["private"] is False and changed.json()["title"] == "Incident bridge"
    assert (
        client.get(f"/api/meetings/{private}", headers=AUTH).json()["meeting"]["private"] is False
    )
    changed = client.patch(f"/api/meetings/{shared}", headers=AUTH, json={"private": True})
    assert changed.json()["private"] is True
    # A new title leaves it as it is.
    client.patch(f"/api/meetings/{shared}", headers=AUTH, json={"title": "Cutover"})
    assert store.private_meetings() == {shared}


def test_a_meeting_is_made_private_while_it_is_recorded(tmp_path: Path, monkeypatch):
    class Recorder:
        def __init__(self, folder: Path, **devices) -> None:
            self.folder, self.tracks, self.paused = folder, {}, False
            self.started_at, self.elapsed = None, 3.0

        def start(self) -> None:
            self.folder.mkdir(parents=True)

        def levels(self) -> dict:
            return {}

        def errors(self) -> list:
            return []

        def stop(self) -> dict:
            return {"duration": 3.0}

    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", Recorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    private = {"headers": AUTH, "json": {"private": True}}
    assert client.post("/api/record/private", **private).status_code == 409  # not recording

    first = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    assert client.get("/api/record", headers=AUTH).json()["private"] is False
    assert client.post("/api/record/private", **private).json() == {"private": True}
    assert client.get("/api/record", headers=AUTH).json()["private"] is True
    client.post("/api/record/stop", headers=AUTH)
    second = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    # The next recording isn't, unless it's made so.
    assert client.get("/api/record", headers=AUTH).json()["private"] is False
    client.post("/api/record/private", **private)
    client.post("/api/record/private", headers=AUTH, json={"private": False})
    client.post("/api/record/stop", headers=AUTH)

    store = Store()
    assert store.private_meetings() == {first} and second != first
    store.close()


def test_a_library_from_before_meetings_could_be_private(library, tmp_path: Path):
    store, shared, private = library
    store.close()
    old = sqlite3.connect(tmp_path / "zachcap.db")
    old.execute("ALTER TABLE meetings DROP COLUMN private")
    old.commit()
    old.close()

    # Read as it is by the MCP server, which adds nothing to it.
    readonly = Store(readonly=True)
    assert readonly.private_meetings() == set() and not readonly.has_private()
    assert "M1 " in assistant.meetings_list(readonly)
    assert "breach" in assistant.search(readonly, "breach")
    assert "Priya: 1 meetings" in assistant.people_list(readonly)
    assert "breach" in assistant.meeting_detail(readonly, private)
    assert "Incident bridge" in assistant.work_log(readonly, "day", "2026-11-20")
    readonly.close()

    opened = Store()
    assert opened.has_private() and opened.private_meetings() == set()
    opened.close()
