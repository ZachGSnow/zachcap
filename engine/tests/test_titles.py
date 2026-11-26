"""A meeting still called what ZachCap named it when it began takes its summary's title."""

from datetime import datetime
from pathlib import Path

import pytest

from zachcap import summarize, titles
from zachcap.store import Store

SUMMARY = {
    "title": "Contoso firewall cutover plan",
    "overview": "The cutover moves to Friday.",
    "decisions": [],
    "action_items": [],
    "open_questions": [],
    "topics": [],
}
KIND = summarize.Kind("general", "general", None)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    yield store
    store.close()


def summarized(store: Store, title: str, summary_title) -> str:
    meeting = store.create_meeting(title, "2026-11-26T09:00:00", Path("somewhere"))
    content = {**SUMMARY, "title": summary_title}
    summarize.keep(store, meeting, "claude-opus-5-5", content, KIND, (0, 0), 0.0)
    return store.meeting(meeting)["title"]


def test_the_titles_zachcap_makes_are_known_as_its_own():
    when = datetime(2026, 11, 6, 9, 5)
    assert titles.made_up("Meeting", when) == "Meeting Nov 06 09:05"
    assert titles.made_up("Microsoft Teams call", when) == "Microsoft Teams call Nov 06 09:05"
    for made in (titles.made_up("Meeting"), titles.made_up("3CX call", when)):
        assert titles.is_made_up(made)
    for given in ("Weekly sync", "Meeting with Dana", "Teams call", "Meeting Nov 06", "", None):
        assert not titles.is_made_up(given)


def test_a_made_up_title_becomes_the_summarys(store):
    assert summarized(store, "Teams call Nov 26 09:00", SUMMARY["title"]) == SUMMARY["title"]
    assert summarized(store, "Meeting Nov 26 09:00", " Two  lines\nin one. ") == "Two lines in one"


def test_a_title_you_gave_stays(store):
    assert summarized(store, "Weekly sync", SUMMARY["title"]) == "Weekly sync"
    assert summarized(store, "Imported call", SUMMARY["title"]) == "Imported call"


def test_no_title_or_a_ticket_number_leaves_it(store):
    assert summarized(store, "Meeting Nov 26 09:00", "") == "Meeting Nov 26 09:00"
    assert summarized(store, "Meeting Nov 26 09:00", None) == "Meeting Nov 26 09:00"
    store.set_setting("ticket_patterns", "#######")
    made = "Teams call Nov 26 10:00"
    assert summarized(store, made, "Ticket 1924086 follow-up") == made
    assert summarized(store, made, "VPN follow-up") == "VPN follow-up"


def test_a_long_title_is_cut_at_a_word():
    long = "Word " * 30
    cut = titles.tidy(long)
    assert len(cut) <= titles.MOST and cut.endswith("Word") and "  " not in cut
