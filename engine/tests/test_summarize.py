from pathlib import Path
from types import SimpleNamespace

import pytest

from zachcap import summarize
from zachcap.store import Store
from zachcap.voiceprint import Match


@pytest.fixture
def meeting(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting_id = store.create_meeting("Cutover sync", "2026-11-22T20:00:00", tmp_path)
    dana = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting_id, "system", "Dana", None, 10.0, None, Match(dana, 1.0, "confirmed")
    )
    store.add_segments(
        meeting_id, [(speaker, "system", 61.0, 64.0, "I'll send the change notice Tuesday.", 0.9)]
    )
    yield store, meeting_id
    store.close()


def fake_claude(monkeypatch, result: summarize.Summary, model: str = "claude-opus-5-5"):
    """Stands in for anthropic.Anthropic, returning the real response's shape."""
    calls = {}
    response = SimpleNamespace(
        parsed_output=result,
        stop_reason="end_turn",
        model=model,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
    )

    class Client:
        def __init__(self, api_key):
            calls["api_key"] = api_key
            self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

        def parse(self, **kwargs):
            calls.update(kwargs)
            return response

    monkeypatch.setattr(summarize.anthropic, "Anthropic", Client)
    monkeypatch.setattr(summarize.apikeys, "get", lambda name: "test-key")
    return calls


def sample_summary() -> summarize.Summary:
    return summarize.Summary(
        title="Cutover sync",
        overview="Dana will send the change notice.",
        decisions=["Cutover is Friday evening."],
        action_items=[
            summarize.ActionItem(
                owner="Dana",
                task="Send the change notice",
                due="Tuesday",
                kind="commitment",
                said_at="01:01",
            )
        ],
        who_said_what=[summarize.SpeakerPoints(speaker="Dana", points=["Owns the notice"])],
        open_questions=[],
        topics=[summarize.Topic(title="Change notice", start="01:01")],
    )


def test_summary_sends_named_text_and_stores_cost_and_action_items(meeting, monkeypatch):
    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())

    summarize.summarize(store, meeting_id)

    assert calls["api_key"] == "test-key"
    assert calls["model"] == summarize.DEFAULT_MODEL == "claude-opus-5-5"
    assert calls["fallbacks"] == "default"
    assert "[01:01] Dana: I'll send the change notice Tuesday." in calls["messages"][0]["content"]
    saved = store.summary(meeting_id)
    assert saved["content"]["who_said_what"][0]["speaker"] == "Dana"
    assert saved["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1_000_000)
    [item] = store.action_items(meeting_id=meeting_id)
    assert (item["owner"], item["kind"], item["said_at"], item["status"]) == (
        "Dana",
        "commitment",
        61.0,
        "open",
    )


def test_regenerating_keeps_items_already_marked_done(meeting, monkeypatch):
    store, meeting_id = meeting
    fake_claude(monkeypatch, sample_summary())
    summarize.summarize(store, meeting_id)
    [item] = store.action_items(meeting_id=meeting_id)
    store.set_action_status(item["id"], "done")

    summarize.summarize(store, meeting_id)

    assert [row["status"] for row in store.action_items(meeting_id=meeting_id)] == ["done"]


def test_model_setting_and_pricing_of_the_model_that_answered(meeting, monkeypatch):
    store, meeting_id = meeting
    store.set_setting("summary_model", "claude-sonnet-5-5")
    calls = fake_claude(monkeypatch, sample_summary(), model="claude-sonnet-5-5")

    summarize.summarize(store, meeting_id)

    assert calls["model"] == "claude-sonnet-5-5"
    assert store.summary(meeting_id)["cost_usd"] == pytest.approx((1000 * 2 + 200 * 10) / 1e6)
    store.set_setting("summary_model", "not-a-model")
    assert summarize.summary_model(store) == summarize.DEFAULT_MODEL


def test_summaries_spell_the_words_to_know(meeting, monkeypatch):
    store, meeting_id = meeting
    store.set_setting("vocabulary", "Contoso\nProxmox: prox mocks")
    calls = fake_claude(monkeypatch, sample_summary())

    summarize.summarize(store, meeting_id)

    assert "Spell these names and terms exactly as written: Contoso, Proxmox." in calls["system"]


def test_timestamps_become_seconds():
    assert summarize.to_seconds("01:01") == 61
    assert summarize.to_seconds("[1:02:03]") == 3723
    assert summarize.to_seconds("soon") is None
