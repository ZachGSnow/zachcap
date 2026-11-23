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


def fake_claude(
    monkeypatch,
    result: summarize.Summary,
    model: str = "claude-opus-5-5",
    usage=None,
    written_usage=None,
    written_stop: str = "end_turn",
):
    """Stands in for anthropic.Anthropic, returning the real response's shape."""
    calls = {}
    response = SimpleNamespace(
        parsed_output=result,
        stop_reason="end_turn",
        model=model,
        usage=usage or SimpleNamespace(input_tokens=1000, output_tokens=200),
    )

    written = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Subject: Cutover\n\nThanks, all.")],
        stop_reason=written_stop,
        model=model,
        usage=written_usage or SimpleNamespace(input_tokens=1000, output_tokens=100),
    )

    class Client:
        def __init__(self, api_key):
            calls["api_key"] = api_key
            self.beta = SimpleNamespace(
                messages=SimpleNamespace(parse=self.parse, create=self.create)
            )

        def parse(self, **kwargs):
            calls.update(kwargs)
            return response

        def create(self, **kwargs):
            calls.update(kwargs)
            return written

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


def test_the_kind_of_meeting_sets_the_focus_and_is_remembered(meeting, monkeypatch):
    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())

    summarize.summarize(store, meeting_id, template="customer")

    assert "call with a customer or client" in calls["system"]
    assert store.summary(meeting_id)["content"]["template"] == "customer"
    calls.clear()
    summarize.summarize(store, meeting_id)  # Regenerate keeps the meeting's kind
    assert "call with a customer or client" in calls["system"]
    store.set_setting("summary_template", "standup")
    assert summarize.meeting_template(store, store.meeting(meeting_id)) == "customer"


def test_a_recipe_writes_a_document_from_the_meeting(meeting, monkeypatch):
    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())
    summarize.summarize(store, meeting_id)

    written = summarize.recipe(store, meeting_id, "email")

    assert written["title"] == "Follow-up email" and written["text"].startswith("Subject:")
    context, ask = calls["messages"][0]["content"]
    assert context["cache_control"] == {"type": "ephemeral"}
    assert "Summary:" in context["text"] and "Dana: I'll send the change notice" in context["text"]
    assert ask["text"].startswith("Write the follow-up email You would send")
    assert written["cost_usd"] == pytest.approx((1000 * 4 + 100 * 20) / 1_000_000)
    with pytest.raises(RuntimeError, match="Unknown recipe"):
        summarize.recipe(store, meeting_id, "poem")


def test_a_document_is_rewritten_the_way_you_ask(meeting, monkeypatch):
    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())

    written = summarize.recipe(
        store, meeting_id, "email", previous="Subject: Cutover (edited)", change="shorter"
    )

    assert written["text"].startswith("Subject:")
    first, mine, ask = calls["messages"]
    assert first["content"][0]["cache_control"] == {"type": "ephemeral"}  # the meeting, cached
    assert mine == {"role": "assistant", "content": "Subject: Cutover (edited)"}
    assert ask["role"] == "user" and ask["content"].startswith("Rewrite it: shorter")
    assert "edits made by hand: keep what they added" in ask["content"]

    store.set_setting("mask_before_claude", "1")  # an edit that added contact details
    summarize.recipe(
        store, meeting_id, "email", previous="Call dana@example.com", change="add 555-123-4567"
    )
    first, mine, ask = calls["messages"]
    assert "dana@example.com" not in mine["content"] and "555-123-4567" not in ask["content"]
    for previous, change, reason in [
        ("Text", None, "together"),
        ("Text", " ", "Say what to change"),
        ("Text", "x" * 501, "at most 500"),
        (" ", "shorter", "must be 1 to"),
    ]:
        with pytest.raises(RuntimeError, match=reason):
            summarize.recipe(store, meeting_id, "email", previous, change)


def test_summaries_cover_the_notes_taken_during_the_meeting(meeting, monkeypatch):
    store, meeting_id = meeting
    store.add_note(meeting_id, 61.5, "note", "Confirm the change window")
    store.add_note(meeting_id, 90.0, "bookmark")
    calls = fake_claude(monkeypatch, sample_summary())

    summarize.summarize(store, meeting_id)

    content = calls["messages"][0]["content"]
    assert "Notes You took during the meeting" in content
    assert "[01:01] Confirm the change window" in content
    assert "[01:30] marked this moment as important" in content


def test_timestamps_become_seconds():
    assert summarize.to_seconds("01:01") == 61
    assert summarize.to_seconds("[1:02:03]") == 3723
    assert summarize.to_seconds("soon") is None


def attempt(model, input_tokens, output_tokens, kind="message", cache_read=0):
    """One entry of usage.iterations."""
    return SimpleNamespace(
        type=kind,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=0,
    )


def test_a_fallback_is_priced_at_each_model_that_ran(meeting, monkeypatch):
    store, meeting_id = meeting
    # Opus 5.5 declined before answering; Opus 4.8 ($5/$25) wrote the summary.
    tries = [attempt(None, 1000, 0), attempt("claude-opus-4-8", 1000, 200, "fallback_message")]
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, iterations=tries)
    fake_claude(monkeypatch, sample_summary(), model="claude-opus-4-8", usage=usage)

    summarize.summarize(store, meeting_id)

    saved = store.summary(meeting_id)
    assert saved["model"] == "claude-opus-4-8"
    assert saved["cost_usd"] == pytest.approx((1000 * 4 + 1000 * 5 + 200 * 25) / 1e6)


def test_cache_reads_are_priced_per_model(meeting, monkeypatch):
    store, meeting_id = meeting
    cached = SimpleNamespace(
        input_tokens=100, output_tokens=100, cache_read_input_tokens=10_000,
        cache_creation_input_tokens=0,
    )  # fmt: skip
    fake_claude(monkeypatch, sample_summary(), written_usage=cached)

    written = summarize.recipe(store, meeting_id, "status")

    # Opus 5.5 reads its cache at $0.20 per million tokens (0.05x), not a tenth of input.
    assert written["cost_usd"] == pytest.approx((100 * 4 + 10_000 * 0.20 + 100 * 20) / 1e6)
    assert summarize.usage_cost(cached, "claude-fable-5-1") == pytest.approx(
        (100 * 10 + 10_000 * 0.25 + 100 * 50) / 1e6
    )


def test_a_document_cut_off_at_the_limit_says_so(meeting, monkeypatch):
    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary(), written_stop="max_tokens")

    written = summarize.recipe(store, meeting_id, "email")

    assert calls["max_tokens"] == summarize.MAX_TOKENS == 16_000
    assert written["text"].startswith("Subject: Cutover")
    assert written["text"].endswith(summarize.CUT_OFF)


def test_your_own_kind_of_meeting_sets_the_focus(meeting, monkeypatch):
    from zachcap import prompts

    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())
    mine = prompts.add(store, "template", "Vendor call", "Focus on quotes and lead times.")
    prompts.update(store, "customer", instructions="Focus on the customer's SLA.")

    summarize.summarize(store, meeting_id, template=mine["id"])

    assert calls["system"].count("Focus on quotes and lead times.") == 1
    assert store.meeting(meeting_id)["template"] == mine["id"]
    summarize.summarize(store, meeting_id, template="customer")
    assert "Focus on the customer's SLA." in calls["system"]
    assert "call with a customer or client" not in calls["system"]


def test_a_deleted_kind_falls_back_to_the_usual_one(meeting, monkeypatch):
    from zachcap import prompts

    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())
    mine = prompts.add(store, "template", "Vendor call", "Focus on quotes and lead times.")
    summarize.summarize(store, meeting_id, template=mine["id"])
    store.set_setting("summary_template", "incident")

    prompts.delete(store, mine["id"])
    summarize.summarize(store, meeting_id)  # Regenerate

    assert "Focus on quotes" not in calls["system"]
    assert "incident or troubleshooting call" in calls["system"]
    assert store.meeting(meeting_id)["template"] == "incident"


def test_your_own_document_with_braces_in_it(meeting, monkeypatch):
    from zachcap import prompts

    store, meeting_id = meeting
    calls = fake_claude(monkeypatch, sample_summary())
    mine = prompts.add(
        store,
        "recipe",
        "Time entry",
        'Write a time entry for {me} as JSON like {"minutes": 30, "note": "..."}.',
    )

    written = summarize.recipe(store, meeting_id, mine["id"])

    assert written["title"] == "Time entry"
    ask = calls["messages"][0]["content"][1]["text"]
    assert ask == 'Write a time entry for You as JSON like {"minutes": 30, "note": "..."}.'
