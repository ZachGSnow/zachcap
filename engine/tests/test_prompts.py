import sqlite3
from pathlib import Path

import pytest

from zachcap import prompts
from zachcap.store import Store


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


def labels(store: Store, kind: str) -> list[str]:
    return [entry["label"] for entry in prompts.library(store, kind)]


def test_the_built_ins_come_first_then_your_own(store: Store):
    assert labels(store, "template")[:2] == ["General", "1:1"]
    assert labels(store, "recipe") == ["Follow-up email", "Status update", "Ticket notes"]

    mine = prompts.add(store, "template", "  Client QBR ", "Focus on renewals and risks.")
    second = prompts.add(store, "recipe", "Time entry", "Write a time entry for {me}.")

    assert mine["id"].startswith("custom-") and mine["label"] == "Client QBR"
    assert not mine["builtin"] and not mine["edited"]
    assert labels(store, "template")[-1] == "Client QBR"
    assert labels(store, "recipe")[-1] == "Time entry"
    assert prompts.get(store, "recipe", second["id"])["instructions"].endswith("for {me}.")
    # Kinds don't mix: a document isn't a kind of meeting.
    assert prompts.get(store, "template", second["id"]) is None


def test_editing_a_built_in_keeps_only_what_changed_and_reset_undoes_it(store: Store):
    edited = prompts.update(store, "customer", instructions="Focus on the asks and the SLA.")

    assert edited["label"] == "Customer call" and edited["edited"]
    row = store.prompt_row("customer")
    assert row["label"] is None  # the name still follows the built-in
    assert row["instructions"] == "Focus on the asks and the SLA."

    back = prompts.reset(store, "customer")
    assert not back["edited"] and back["instructions"] == prompts.TEMPLATES["customer"][1]
    assert store.prompt_row("customer") is None

    # Editing it back to the built-in text by hand counts as not edited, too.
    prompts.update(store, "email", label="Recap email")
    same = prompts.update(store, "email", label="Follow-up email")
    assert not same["edited"] and store.prompt_row("email") is None


def test_names_and_instructions_are_checked(store: Store):
    with pytest.raises(ValueError, match="name"):
        prompts.add(store, "template", "   ", "Something")
    with pytest.raises(ValueError, match="to 40 characters"):
        prompts.add(store, "template", "x" * 41, "Something")
    with pytest.raises(ValueError, match="to 2,000 characters"):
        prompts.add(store, "recipe", "Long", "x" * 2001)
    with pytest.raises(ValueError, match="already a kind of meeting called Standup"):
        prompts.add(store, "template", "standup", "Short.")
    with pytest.raises(ValueError, match="Say what it should do"):
        prompts.add(store, "recipe", "Empty", "  ")
    with pytest.raises(ValueError, match="Unknown kind"):
        prompts.add(store, "poem", "Haiku", "Write a haiku.")
    # The general kind is the one that may say nothing; a document may not.
    assert prompts.update(store, "general", instructions="")["instructions"] == ""
    with pytest.raises(ValueError, match="Say what it should do"):
        prompts.update(store, "ticket", instructions="")
    with pytest.raises(prompts.UnknownPrompt):
        prompts.update(store, "custom-nothere", label="Hm")


def test_there_is_a_limit_on_your_own(store: Store, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(prompts, "CUSTOM_MAX", 2)
    prompts.add(store, "recipe", "One", "Write one.")
    prompts.add(store, "recipe", "Two", "Write two.")
    with pytest.raises(ValueError, match="most you can add"):
        prompts.add(store, "recipe", "Three", "Write three.")
    # The other list has its own limit.
    prompts.add(store, "template", "One", "Focus on one thing.")


def test_built_ins_are_reset_not_deleted(store: Store):
    with pytest.raises(ValueError, match="can't be deleted"):
        prompts.delete(store, "incident")
    mine = prompts.add(store, "recipe", "Mine", "Write mine.")
    with pytest.raises(ValueError, match="Only the built-in"):
        prompts.reset(store, mine["id"])


def test_a_reset_name_cant_collide_with_your_own(store: Store):
    prompts.update(store, "standup", label="Daily")
    prompts.add(store, "template", "Standup", "Mine: blockers first.")
    with pytest.raises(ValueError, match="Rename your kind of meeting called Standup first"):
        prompts.reset(store, "standup")


def test_deleting_a_kind_sends_its_meetings_back_to_the_usual_one(store: Store, tmp_path: Path):
    mine = prompts.add(store, "template", "Vendor call", "Focus on quotes and lead times.")
    meeting = store.create_meeting("Vendor", "2026-11-23T09:00:00", tmp_path)
    store.update_meeting(meeting, template=mine["id"], template_source="auto")
    store.set_setting("summary_template", mine["id"])

    prompts.delete(store, mine["id"])

    assert store.meeting(meeting)["template"] is None
    assert store.meeting(meeting)["template_source"] is None
    # A usual kind you chose stays one you chose: General, not Automatic.
    assert store.setting("summary_template") == "general"
    assert prompts.get(store, "template", mine["id"]) is None
    with pytest.raises(prompts.UnknownPrompt):
        prompts.delete(store, mine["id"])


def test_an_older_database_gets_the_prompts_table(tmp_path: Path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    old.commit()
    old.close()

    store = Store(tmp_path / "old.db")

    assert prompts.add(store, "recipe", "Works", "Write it.")["label"] == "Works"
    store.close()


def test_the_kind_pickers_start_with_automatic(store: Store):
    assert prompts.choices(store, "template")[:2] == [
        {"id": "auto", "label": "Automatic"},
        {"id": "general", "label": "General"},
    ]
    assert [choice["id"] for choice in prompts.choices(store, "recipe")] == [
        "email",
        "status",
        "ticket",
    ]
    assert "auto" not in [entry["id"] for entry in prompts.library(store, "template")]
    with pytest.raises(ValueError, match="already a kind of meeting called Automatic"):
        prompts.add(store, "template", "automatic", "Focus on everything.")
    with pytest.raises(ValueError, match="already a kind of meeting called Automatic"):
        prompts.update(store, "standup", label="Automatic")


def test_only_the_general_kind_may_have_no_instructions(store: Store):
    with pytest.raises(ValueError, match="what it should do"):
        prompts.update(store, "standup", instructions="   ")
    assert prompts.update(store, "general", instructions="")["instructions"] == ""
