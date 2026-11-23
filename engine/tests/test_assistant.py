from pathlib import Path
from types import SimpleNamespace

import pytest

from zachcap import assistant
from zachcap.store import Store
from zachcap.voiceprint import Match


@pytest.fixture
def library(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    cutover = store.create_meeting("Cutover sync", "2026-11-20T09:00:00", tmp_path / "a")
    budget = store.create_meeting("Budget review", "2026-11-22T14:00:00", tmp_path / "b")
    for meeting, name, lines in (
        (cutover, "Dana", [(61.0, "I'll send the firewall change notice Tuesday.")]),
        (budget, "Mark", [(5.0, "The switch budget is approved."), (9.0, "Order it Monday.")]),
    ):
        person = store.person_id(name)
        speaker = store.add_speaker(
            meeting, "system", name, None, 10.0, None, Match(person, 1.0, "confirmed")
        )
        store.add_segments(meeting, [(speaker, "system", t, t + 2, text, 0.9) for t, text in lines])
    store.save_summary(
        budget,
        "claude-opus-5-5",
        {"title": "Budget", "overview": "The switch budget was approved.", "decisions": []},
        100,
        10,
        0.0,
    )
    store.replace_action_items(
        budget,
        [{"owner": "Mark", "task": "Order the switch", "said_at": 9.0, "kind": "commitment"}],
    )
    yield store, cutover, budget
    store.close()


def tools(store: Store) -> dict:
    return {tool.name: tool for tool in assistant.make_tools(store)}


def test_free_text_becomes_a_prefix_query_without_filler_words():
    assert assistant.fts_query("What did Dana say about the cutover?") == '"dana"* "cutover"*'
    assert assistant.fts_query("budget switch", any_word=True) == '"budget"* OR "switch"*'
    assert assistant.fts_query("what was it") is None


def test_search_returns_cited_lines_from_every_meeting(library):
    store, cutover, budget = library
    found = tools(store)["search_transcripts"].call({"query": "firewall notice"})
    assert f'[M{cutover} 01:01] 2026-11-20 "Cutover sync" Dana:' in found
    # Falls back to any word when all of them never appear in one line.
    either = tools(store)["search_transcripts"].call({"query": "firewall budget"})
    assert f"[M{cutover} " in either and f"[M{budget} " in either


def test_meetings_summaries_and_action_items_are_readable(library):
    store, cutover, budget = library
    listed = tools(store)["list_meetings"].call({"since": "2026-11-21"})
    assert f"M{budget} 2026-11-22" in listed and f"M{cutover} " not in listed
    assert "switch budget was approved" in listed
    assert f"M{cutover} " in tools(store)["list_meetings"].call({"person": "dana"})

    meeting = tools(store)["get_meeting"].call({"meeting_id": budget})
    assert "Summary:" in meeting and "[00:09] Mark: Order it Monday." in meeting
    assert "no meeting M999" in tools(store)["get_meeting"].call({"meeting_id": 999})

    items = tools(store)["list_action_items"].call({"owner": "mark"})
    assert f"[M{budget} 00:09] open: Mark, commitment: Order the switch" in items


def test_the_answer_comes_back_with_the_meetings_it_cites(library, monkeypatch):
    store, cutover, budget = library
    calls = {}

    def tool_runner(**kwargs):
        calls.update(kwargs)
        found = {t.name: t for t in kwargs["tools"]}["search_transcripts"].call({"query": "notice"})
        assert f"[M{cutover} 01:01]" in found  # the tools read this library
        usage = SimpleNamespace(input_tokens=2000, output_tokens=100)
        yield SimpleNamespace(model="claude-opus-5-5", stop_reason="tool_use", usage=usage,
                              content=[SimpleNamespace(type="tool_use")])  # fmt: skip
        yield SimpleNamespace(
            model="claude-opus-5-5",
            stop_reason="end_turn",
            usage=usage,
            content=[
                SimpleNamespace(type="text", text=f"Dana sends it Tuesday [M{cutover} 01:01].")
            ],
        )

    class Client:
        def __init__(self, api_key):
            self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=tool_runner))

    monkeypatch.setattr(assistant.anthropic, "Anthropic", Client)
    monkeypatch.setattr(assistant.apikeys, "get", lambda name: "test-key")

    result = assistant.ask_everything(
        store, "Who sends the notice?", history=[{"question": "Hi", "answer": "Hello"}]
    )

    assert result["answer"].startswith("Dana sends it Tuesday")
    assert result["meetings"] == {
        cutover: {"title": "Cutover sync", "started_at": "2026-11-20T09:00:00"}
    }
    assert result["cost_usd"] == pytest.approx(2 * (2000 * 4 + 100 * 20) / 1_000_000)
    assert [m["role"] for m in calls["messages"]] == ["user", "assistant", "user"]
    assert calls["fallbacks"] == "default" and "Cite every fact" in calls["system"]
