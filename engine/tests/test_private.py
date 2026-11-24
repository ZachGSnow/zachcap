"""Private meetings: nothing in one is sent to Claude, by any of the ways there are. The page
goes on showing it like any other."""

import asyncio
import json
import sqlite3
import time
from datetime import date
from pathlib import Path

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from zachcap import assistant, capture, digest, mcp_server, pipeline, privacy, summarize
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


def said_to_claude(store: Store, shared: int, private: int) -> dict[str, str]:
    """All that the library tells Claude, through Ask's tools and the MCP server's."""
    tools = {tool.name: tool for tool in assistant.make_tools(store)}
    asked = {
        "search_transcripts": [{"query": "breach Sunday agree"}],
        "list_meetings": [{}, {"client": "Fabrikam"}, {"ticket": "INC-4471"}],
        "get_meeting": [{"meeting_id": private}],
        "related_meetings": [{"meeting_id": shared}, {"meeting_id": private}],
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

    said = said_to_claude(store, shared, private)

    for what, text in said.items():
        for word in ONLY_PRIVATE:
            assert word not in text, (what, text)
    assert "I agree." in said["search_transcripts {'query': 'breach Sunday agree'}"]
    kept = f"M{private} is a private meeting: the user keeps it from Claude."
    assert said[f"get_meeting {{'meeting_id': {private}}}"] == kept
    assert said[f"mcp search_meetings {{'query': 'breach', 'meeting_id': {private}}}"] == kept
    assert said[f"related_meetings {{'meeting_id': {private}}}"] == kept
    alone = f"No other meeting seems to be about the same thing as M{shared}."
    assert said[f"mcp related_meetings {{'meeting_id': {shared}}}"] == alone
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
    said = said_to_claude(store, shared, private)
    assert "The breach began on Sunday." in said[f"get_meeting {{'meeting_id': {private}}}"]
    assert "Priya: 1 meetings" in said["mcp list_people {}"]
    assert "Incident bridge" in said[f"related_meetings {{'meeting_id': {shared}}}"]
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
    related = client.get(f"/api/meetings/{shared}/related", headers=AUTH).json()
    assert [row["id"] for row in related] == [private]

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


# While Claude is asked ---------------------------------------------------------------------------


def answer(text: str = "", **use) -> dict:
    """What the API answers: text, or a tool for the engine to run."""
    content = [{"type": "text", "text": text}] if text else []
    if use:
        content.append({"type": "tool_use", "id": "toolu_1", **use})
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": "tool_use" if use else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


SUMMARY = {
    "title": "Incident bridge",
    "overview": "It began on Sunday.",
    "decisions": [],
    "action_items": [],
    "who_said_what": [],
    "open_questions": [],
    "topics": [],
}


class Api:
    """Anthropic's API, for the real client: what is sent to it, and what it answers. A reply
    can be a function, called once the request is in (as the user does something meanwhile);
    None is an overloaded API, which the client tries again."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.replies: list = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(request.content.decode())
        reply = self.replies.pop(0)
        reply = reply() if callable(reply) else reply
        if reply is None:
            error = {"type": "error", "error": {"type": "overloaded_error", "message": "Busy"}}
            return httpx2.Response(529, json=error, headers={"retry-after": "0.01"})
        return httpx2.Response(200, json=reply)


@pytest.fixture
def claude_api(monkeypatch: pytest.MonkeyPatch) -> Api:
    api, client = Api(), anthropic.Anthropic

    def to_api(**options) -> anthropic.Anthropic:
        http = httpx2.Client(transport=httpx2.MockTransport(api))
        return client(http_client=http, **options)

    monkeypatch.setattr(summarize.anthropic, "Anthropic", to_api)
    monkeypatch.setattr(summarize.apikeys, "get", lambda name: "test-key")
    return api


@pytest.mark.parametrize("busy", [False, True], ids=["between requests", "before a retry"])
@pytest.mark.parametrize("gone", [False, True], ids=["made private", "deleted"])
@pytest.mark.parametrize(
    "send",
    [
        lambda store, meeting: summarize.summarize(store, meeting),  # picks the kind first
        lambda store, meeting: summarize.summarize(store, meeting, "general"),
        lambda store, meeting: summarize.recipe(store, meeting, "email"),
        lambda store, meeting: summarize.ask(store, meeting, "What happened?"),
    ],
    ids=["a kind", "a summary", "a document", "a question"],
)
def test_once_a_meeting_is_private_nothing_more_is_sent(library, claude_api, send, gone, busy):
    store, shared, private = library
    # A title and people that don't tell the kind of meeting: Claude is asked for it.
    store.update_meeting(private, private=0, title="Catch up", client=None)

    def meanwhile():
        user = Store()  # the page's own request
        user.delete_meeting(private) if gone else user.update_meeting(private, private=1)
        user.close()
        return None if busy else answer(json.dumps({"kind": "general"}))

    # Were another request sent, it would be answered.
    claude_api.replies = [meanwhile, answer(json.dumps(SUMMARY)), answer(json.dumps(SUMMARY))]

    try:
        send(store, private)
    except RuntimeError as exc:
        assert str(exc) in (privacy.PRIVATE, "This meeting isn't there any more.")
    except Exception as exc:  # the answer wasn't a summary
        assert not busy and "kind" in str(exc), exc

    assert len(claude_api.sent) == 1 and "The breach began" in claude_api.sent[0]


def test_a_summary_is_written_as_before(library, claude_api):
    store, shared, private = library
    claude_api.replies = [None, answer(json.dumps({"kind": "general"})), None]
    claude_api.replies.append(answer(json.dumps(SUMMARY)))  # busy twice, tried again

    summarize.summarize(store, shared)

    assert len(claude_api.sent) == 4
    assert store.summary(shared)["content"]["overview"] == "It began on Sunday."


def test_a_run_of_ask_ends_when_a_meeting_it_read_is_made_private(library, claude_api):
    store, shared, private = library
    store.update_meeting(private, private=0)
    look = {"name": "search_transcripts", "input": {"query": "breach"}}

    def meanwhile():
        store.update_meeting(private, private=1)
        return answer(name="list_meetings", input={})

    claude_api.replies = [answer(**look), meanwhile, answer("On Sunday.")]

    with pytest.raises(RuntimeError, match="made private, deleted or redacted meanwhile"):
        assistant.ask_everything(store, "When did it begin?")

    assert "The breach began" in claude_api.sent[1] and len(claude_api.sent) == 2
    # One that has read nothing of it goes on.
    claude_api.replies = [answer(**look), answer("Nobody said.")]
    said = assistant.ask_everything(store, "When did it begin?")
    assert said["answer"] == "Nobody said." and said["used"] == []


@pytest.mark.parametrize("how", ["made private", "deleted", "redacted", "a restart"])
def test_an_answer_isnt_sent_again_once_its_meeting_is_private(
    library, claude_api, monkeypatch, how
):
    store, shared, private = library
    store.update_meeting(private, private=0)
    look = {"name": "search_transcripts", "input": {"query": "breach"}}
    claude_api.replies = [answer(**look), answer("The breach began on Sunday.")]
    asked = {"question": "When did the breach begin?"}
    first = assistant.ask_everything(store, asked["question"])
    asked.update(answer=first["answer"], used=first["used"], since=first["since"])
    assert first["used"] == [private]

    # A follow-up has it along, and is made from it too.
    claude_api.replies = [answer("Priya did.")]
    follow = assistant.ask_everything(store, "Who said so?", [asked])
    assert "began on Sunday" in claude_api.sent[-1] and follow["used"] == [private]
    followed = {"question": "Who said so?", "answer": follow["answer"]}
    followed.update(used=follow["used"], since=follow["since"])
    hello = {"question": "Hello?", "answer": "Hello.", "used": [shared], "since": time.time()}

    if how == "made private":
        store.update_meeting(private, private=1)
    elif how == "deleted":
        store.delete_meeting(private)
    elif how == "redacted":
        privacy.redacted(private)
    else:  # what was redacted before it isn't known any more
        for turn in (asked, followed):
            turn["since"] -= 100
        monkeypatch.setattr(privacy, "_started", time.time() - 50)
    claude_api.replies = [answer("Fine.")]
    assistant.ask_everything(store, "And now?", [asked, followed, hello])

    sent = json.loads(claude_api.sent[-1])["messages"]
    assert [m["content"] for m in sent[:2]] == ["Hello?", "Hello."] and len(sent) == 3
    assert "breach" not in claude_api.sent[-1] and "Priya" not in claude_api.sent[-1]


def test_what_ask_was_answered_from_goes_through_the_api(library, claude_api):
    store, shared, private = library
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store.update_meeting(private, private=0)
    look = {"name": "get_meeting", "input": {"meeting_id": private}}
    claude_api.replies = [answer(**look), answer("The breach began on Sunday.")]
    ask = {"question": "When did it begin?"}
    first = client.post("/api/ask", headers=AUTH, json=ask).json()
    turn = {**ask, **{key: first[key] for key in ("answer", "used", "since")}}
    client.patch(f"/api/meetings/{private}", headers=AUTH, json={"private": True})

    claude_api.replies = [answer("Fine.")]
    more = {"question": "And now?", "history": [turn]}
    assert client.post("/api/ask", headers=AUTH, json=more).json()["answer"] == "Fine."
    assert "breach" not in claude_api.sent[-1] and len(claude_api.sent) == 3


# Names and words that only a private meeting has --------------------------------------------------


def test_names_only_private_meetings_know_arent_told(library):
    store, shared, private = library
    # Claude knows Dana from the shared one. A voice there is suggested to be Priya, and one Dana.
    for name, label, at in (("Priya", "Speaker 3", 200.0), ("Dana", "Speaker 4", 210.0)):
        voice = Match(store.person_id(name), 0.6, "suggested")
        speaker = store.add_speaker(shared, "system", label, None, 10.0, None, voice)
        store.add_segments(shared, [(speaker, "system", at, at + 4, f"Hi from {label}.", 0.9)])

    said = said_to_claude(store, shared, private)
    for what, text in said.items():
        assert "Priya" not in text, (what, text)
    found = assistant.search(store, "hi from")
    assert "Speaker 3: Hi from Speaker 3." in found and "Speaker 4 (Dana?): Hi" in found
    for text in (
        assistant.meeting_detail(store, shared),
        summarize.transcript_text(store, shared),
    ):
        assert "Speaker 3: Hi" in text and "Priya" not in text
    # The page goes on suggesting it.
    assert "Speaker 3 (Priya?)" in {row["speaker"] for row in store.transcript(shared)}

    # Nor when the private meeting isn't there any more (deleted, or processed again).
    store.delete_meeting(private)
    assert "Priya" not in assistant.people_list(store) + assistant.search(store, "hi from")
    # Once named in a meeting Claude may read, it's a name like any other.
    voice = next(s["id"] for s in store.speakers(shared) if s["label"] == "Speaker 3")
    store.set_speaker_person(voice, store.person_id("Priya"), "confirmed")
    assert "Priya: 1 meetings" in assistant.people_list(store)


def test_a_name_only_a_private_meeting_knows_doesnt_change_a_search(library):
    store, shared, private = library
    nothing = "Give some words to search for."
    assert assistant.search(store, "will") == nothing  # a filler word
    voice = Match(store.person_id("Will"), 1.0, "confirmed")
    store.add_speaker(private, "system", "Will", None, 10.0, None, voice)
    assert assistant.search(store, "will") == nothing
    store.add_speaker(shared, "system", "Will", None, 10.0, None, voice)
    assert assistant.search(store, "will") != nothing


def test_the_page_is_told_who_to_not_ask_claude_about(library):
    store, shared, private = library
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    people = client.get("/api/people", headers=AUTH).json()
    assert {p["name"]: p["private"] for p in people} == {
        "You": False,
        "Dana": False,
        "Marcus": False,
        "Priya": True,
    }
    store.update_meeting(private, private=0)
    people = client.get("/api/people", headers=AUTH).json()
    assert not any(p["private"] for p in people)


def test_a_private_line_put_right_offers_no_word_to_know(library):
    store, shared, private = library
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    for meeting, offered in ((private, False), (shared, True)):
        speaker = store.speakers(meeting)[0]["id"]
        line = "We buy Black birch corp in May."
        store.add_segments(meeting, [(speaker, "system", 300.0, 304.0, line, 0.9)])
        row = store.transcript(meeting)[-1]
        right = {"text": "We buy Blackbirch corp in May."}
        fixed = client.patch(f"/api/segments/{row['id']}", headers=AUTH, json=right).json()
        assert fixed["text"] == right["text"]
        assert (fixed["suggestion"] is not None) is offered


def test_keeping_a_recording_that_just_ended_from_claude(library):
    """The button was pressed as it ended: it's that meeting's, whatever records now."""
    store, shared, private = library
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    body = {"private": True, "meeting_id": shared}
    assert client.post("/api/record/private", headers=AUTH, json=body).json() == {"private": True}
    assert store.private_meetings() == {shared, private}
    body["meeting_id"] = 999
    assert client.post("/api/record/private", headers=AUTH, json=body).status_code == 404


def test_masking_reaches_every_way_text_leaves(library):
    store, shared, private = library
    store.set_setting("mask_before_claude", "1")
    voice = Match(store.person_id("dana.lee@example.com"), 1.0, "confirmed")
    store.add_speaker(shared, "system", "dana.lee@example.com", None, 10.0, None, voice)
    # A line break right before a number: JSON has it as "\\n".
    overview = "Who to call:\n555-867-5309\nor dana@example.com"
    store.save_summary(
        shared, "claude-opus-5-5", {"title": "Cutover sync", "overview": overview}, 1, 1, 0.0
    )

    said = said_to_claude(store, shared, private)
    tools = {tool.name: tool for tool in assistant.make_tools(store)}
    said["meeting"] = tools["get_meeting"].call({"meeting_id": shared})
    said["document"] = privacy.outgoing(
        store, summarize.document_context(store, store.meeting(shared))
    )
    for what, text in said.items():
        for word in ("555-867-5309", "dana@example.com", "dana.lee@example.com"):
            assert word not in text, (what, text)
    for what in ("meeting", "document"):
        assert "Who to call: [phone] or [email]" in said[what]
    assert "[email]: 1 meetings" in said["mcp list_people {}"]
