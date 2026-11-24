"""The kind of meeting from what a meeting itself makes clear: its title, its client, and who
said what. Anything less clear is left to Claude (see test_summarize.py)."""

from pathlib import Path

import pytest

from zachcap import autokind, clients
from zachcap.store import Store
from zachcap.voiceprint import Match


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


def call(store: Store, folder: Path, *voices: tuple[str | None, str], title="Weekly sync") -> int:
    """A meeting with a line from each voice: (who, status), who being a name, "You", or None
    for a voice that isn't anyone (yet)."""
    meeting = store.create_meeting(title, "2026-11-24T09:00:00", folder)
    for number, (who, status) in enumerate(voices):
        person = store.me()["id"] if who == "You" else store.person_id(who) if who else None
        label = who or f"Speaker {number + 1}"
        speaker = store.add_speaker(
            meeting, "system", label, None, 4.0, None, Match(person, 0.9, status)
        )
        start = number * 5.0
        store.add_segments(meeting, [(speaker, "system", start, start + 4.0, "Hello", 0.9)])
    return meeting


def kind_of(store: Store, meeting_id: int) -> str | None:
    return autokind.clue(store, store.meeting(meeting_id))


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Daily standup", "standup"),
        ("Platform Stand-Up", "standup"),
        ("Daily scrum", "standup"),
        ("1:1 Dana / Sam", "one_on_one"),
        ("Dana one-on-one", "one_on_one"),
        ("1-on-1 with Dana", "one_on_one"),
        ("Interview: Dana Lee", "interview"),
        ("Mail outage", "incident"),
        ("Incident review", "incident"),
        ("Troubleshooting the VPN", "incident"),
    ],
)
def test_a_title_that_names_a_kind(title, kind):
    assert autokind.from_title(title) == kind


@pytest.mark.parametrize(
    "title",
    [
        "Cutover sync",
        "Meeting Nov 24 11:15",  # a time, not a 1:1
        "Teams call Nov 24 01:10",
        "Stand up the new file server",  # setting one up
        "Interviewer training",
        "Standup: the mail outage",  # two kinds named
        "",
    ],
)
def test_a_title_that_names_no_kind_or_two_is_unclear(title):
    assert autokind.from_title(title) is None


def test_you_and_one_other_named_person_is_a_one_on_one(store, tmp_path):
    two = call(store, tmp_path, ("You", "auto"), ("Dana", "confirmed"))
    assert kind_of(store, two) == "one_on_one"
    # Anyone else, or anyone not named for sure, makes it unclear.
    for voices in [
        [("You", "auto"), ("Dana", "auto"), ("Sam", "auto")],
        [("You", "auto"), ("Dana", "auto"), (None, "unknown")],
        [("You", "auto"), ("Dana", "suggested")],
        [("You", "auto"), (None, "minor")],  # "Call audio": several people not told apart
        [("Dana", "auto"), ("Sam", "confirmed")],
        [("Dana", "auto")],
    ]:
        assert kind_of(store, call(store, tmp_path, *voices)) is None, voices
    store.add_segments(two, [(None, "system", 20.0, 21.0, "Can you hear me?", 0.9)])
    assert kind_of(store, two) is None  # a line no voice was found for


def test_a_meeting_with_a_client_is_a_customer_call(store, tmp_path):
    meeting = call(store, tmp_path, ("You", "auto"), ("Dana", "confirmed"))
    store.set_client(store.person_id("Dana"), "Contoso")
    assert kind_of(store, meeting) == "customer"  # the client Dana works for
    store.update_meeting(meeting, client=None, client_source=clients.YOURS)  # no client
    assert kind_of(store, meeting) == "one_on_one"
    several = call(store, tmp_path, ("You", "auto"), ("Sam", "auto"), (None, "unknown"))
    store.update_meeting(several, client="Fabrikam", client_source=clients.YOURS)
    assert kind_of(store, several) == "customer"  # set on it, whoever was there


def test_a_title_that_names_a_kind_comes_first(store, tmp_path):
    meeting = call(
        store, tmp_path, ("You", "auto"), ("Dana", "confirmed"), title="Contoso mail outage"
    )
    store.set_client(store.person_id("Dana"), "Contoso")
    assert kind_of(store, meeting) == "incident"
