"""The message ZachCore gets for a meeting (zachcore.py), against files with what it has to be:
voices without a name and left out, you, the client and ticket, notes, tasks and facts."""

import json
from pathlib import Path

import numpy as np
import pytest

from zachcap import outbox, zachcore
from zachcap.store import Store
from zachcap.voiceprint import Match

EXPECTED = Path(__file__).parent / "fixtures" / "zachcore"
STAGES = ("bare", "whole", "named", "again")

# The call's voices, each with its lines (start, end) in seconds: 900 in all, 880 of them
# listed (the voice left out and the lines with no voice aren't).
LINES = {
    "You": [(0.0, 100.0), (400.0, 600.0)],
    "Speaker 1": [(100.0, 250.0), (600.0, 700.0)],
    "Speaker 2": [(250.0, 400.0)],
    "Speaker 3": [(700.0, 800.0)],
    "Speaker 4": [(800.0, 860.0)],
    "Speaker 5": [(860.0, 870.0)],
    "Speaker 6": [(870.0, 890.0)],
    None: [(890.0, 900.0)],
}

# As Claude writes them, owners by the names it saw.
ITEMS = [
    {"owner": "You", "task": "Send Dana the rollback steps", "due": "Tuesday",
     "kind": "commitment", "said_at": 450.0},
    {"owner": "Dana Lee", "task": "Book the server room", "due": "2026-11-27",
     "kind": "commitment", "said_at": 620.0},
    {"owner": "Speaker 2", "task": "Check the firewall rules", "due": None,
     "kind": "action", "said_at": 300.0},
    {"owner": "Speaker 3 (Tom Avery?)", "task": "Share the floor plan", "due": None,
     "kind": "commitment", "said_at": 750.0},
    {"owner": "Speaker 4", "task": "Ask the landlord about access", "due": None,
     "kind": "commitment", "said_at": 820.0},
    {"owner": "Unassigned", "task": "Tell the help desk", "due": "Friday",
     "kind": "action", "said_at": 512.0},
    {"owner": "Priya", "task": "Order the new cables", "due": None,
     "kind": "action", "said_at": 680.0},
    {"owner": "Unknown", "task": "Call back about the badge", "due": None,
     "kind": "commitment", "said_at": 865.0},
]  # fmt: skip

CONTENT = {
    "title": "Cutover on Friday",
    "overview": "The cutover stays on Friday at 8. Dana books the room; the rollback steps go "
    "out first.",
    "decisions": ["Cut over on Friday at 8", "Keep the old server for a week"],
    "decisions_said_at": [296.0, "10:02"],
    "action_items": ITEMS,
    "who_said_what": [],
    "open_questions": ["Who tells the help desk?"],
    "open_questions_said_at": [512.0],
    "topics": [{"title": "Cutover", "start": "00:10"}, {"title": "Rollback", "start": "06:40"}],
    "facts": [
        {"about": "Dana Lee", "voice": 2, "name": "title", "value": "IT director",
         "said_at": 640.0, "quote": "I run IT there now"},
        {"about": "client", "name": "Size", "value": "About 120 people", "said_at": 260.0,
         "quote": "we are about 120 people"},
        {"about": "Speaker 2", "name": "Role", "value": "Network engineer", "said_at": 300.0,
         "quote": "I look after the network"},
        {"about": "Dana Lee", "voice": 2, "name": "Title", "value": "IT manager",
         "said_at": 110.0, "quote": "I manage IT there"},  # said before: the one after counts
        {"about": "You", "name": "Title", "value": "Consultant", "said_at": 50.0},
        {"about": "Speaker 6", "name": "Team", "value": "Facilities", "said_at": 880.0},
        {"about": "Unknown", "name": "Badge", "value": "Lost", "said_at": 865.0},
    ],
    "template": "general",
}  # fmt: skip

# The next summary: the same tasks in other words, but for the cables, which went.
AGAIN = [
    {"owner": "You", "task": "Send the rollback steps to Dana", "due": "Tuesday",
     "kind": "commitment", "said_at": 452.0},
    {"owner": "Speaker 2", "task": "Check the rules on the firewall", "due": None,
     "kind": "action", "said_at": 300.0},
    {"owner": "Speaker 3 (Tom Avery?)", "task": "Share the floor plan", "due": None,
     "kind": "commitment", "said_at": 750.0},
    {"owner": "Speaker 4", "task": "Ask the landlord about building access", "due": None,
     "kind": "commitment", "said_at": 820.0},
    {"owner": "Unassigned", "task": "Tell the help desk", "due": "Friday",
     "kind": "action", "said_at": 512.0},
    {"owner": "Unknown", "task": "Call back about the badge", "due": None,
     "kind": "commitment", "said_at": 865.0},
]  # fmt: skip


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


def person(store: Store, name: str, org: str | None = None) -> int:
    person_id = store.person_id(name)
    store.set_client(person_id, org)
    store.db.execute(
        "UPDATE people SET uid = ? WHERE id = ?",
        (f"person-{name.lower().replace(' ', '-')}", person_id),
    )
    store.db.commit()
    return person_id


def heard(store: Store, title: str, who: int, private: bool = False) -> None:
    """Another meeting, in which ``who`` was named."""
    meeting = store.create_meeting(title, "2026-11-20T10:00:00-05:00", Path(title))
    store.add_speaker(meeting, "system", "Speaker 1", None, 60.0, None, Match(who, 1, "confirmed"))
    store.update_meeting(meeting, private=int(private))


def call(store: Store) -> int:
    """A 15-minute call, processed: you; Dana, recognized; a voice nobody named; Tom, only
    suggested; somebody suggested who is known only from a private meeting; a voice left out;
    one too short to tell; lines with no voice. With notes, a snapshot, a file and a ticket."""
    me = store.me()["id"]
    dana = person(store, "Dana Lee", "Contoso")
    tom = person(store, "Tom Avery", "Contoso")
    rita = person(store, "Rita Kim", "Fabrikam")
    meeting = store.create_meeting(
        "Contoso cutover sync", "2026-11-25T13:00:00-05:00", Path("call"),
        duration=900.0, source_app="Teams",
    )  # fmt: skip
    old = store.meeting(meeting)["uid"]
    store.db.execute("UPDATE meetings SET uid = 'meeting-1' WHERE id = ?", (meeting,))
    store.db.execute("UPDATE sends SET uid = 'meeting-1' WHERE uid = ?", (old,))
    store.db.commit()
    matches = {
        "You": Match(me, 0.9, "auto"),
        "Speaker 1": Match(dana, 0.8, "auto"),
        "Speaker 2": Match(None, 0.0, "unknown"),
        "Speaker 3": Match(tom, 0.5, "suggested"),
        "Speaker 4": Match(rita, 0.5, "suggested"),
        "Speaker 5": Match(None, 0.0, "left_out"),
        "Speaker 6": Match(None, 0.0, "minor"),
    }
    voices = {
        label: store.add_speaker(
            meeting, "mic" if label == "You" else "system", label, np.ones(4), 60.0, None, match
        )
        for label, match in matches.items()
    }
    voices[None] = None
    store.add_segments(
        meeting,
        [
            (voices[label], "mic" if label == "You" else "system", start, end, "Words", 0.9)
            for label, said in LINES.items()
            for start, end in said
        ],
    )
    store.refresh_status(meeting)
    heard(store, "Northwind review", tom)
    heard(store, "A private call", rita, private=True)
    store.add_note(meeting, 450.0, "note", "Ask about the rollback\n  and the change window ")
    store.add_note(meeting, 620.0, "bookmark", "")
    store.add_note(meeting, 700.0, "snapshot", "Error 0x80070005")
    store.add_note(meeting, 710.0, "file", "cutover.log\nline one")
    store.update_meeting(meeting, ticket="T-20931", ticket_source="user")
    return meeting


def summarized(store: Store, meeting: int) -> None:
    store.save_summary(meeting, "test", CONTENT, 1, 1, 0.0)
    store.replace_action_items(meeting, ITEMS)
    store.db.execute("UPDATE action_items SET uid = 'task-' || id")
    store.db.commit()
    done = next(i for i in store.action_items(meeting_id=meeting) if i["task"].startswith("Book"))
    store.set_action_status(done["id"], "done")


def named(store: Store, meeting: int) -> None:
    """The voice nobody named gets a name, and Dana's turns out to be somebody else."""
    voices = {voice["label"]: voice["id"] for voice in store.speakers(meeting)}
    store.set_speaker_person(voices["Speaker 2"], person(store, "Erin Moss"), "confirmed")
    store.set_speaker_person(
        voices["Speaker 1"], person(store, "Maya Chen", "Contoso"), "confirmed"
    )


def again(store: Store, meeting: int) -> None:
    """Sent before; then summarized again."""
    store.db.execute("UPDATE sends SET target = 'http://127.0.0.1:9' WHERE uid = 'meeting-1'")
    store.db.commit()
    content = {**CONTENT, "action_items": AGAIN, "decisions": ["Cut over on Friday at 8"]}
    store.save_summary(meeting, "test", {**content, "decisions_said_at": [298.0]}, 1, 1, 0.0)
    store.replace_action_items(meeting, AGAIN)


def stages(store: Store):
    """The call at each point of its life, as (name, meeting)."""
    meeting = call(store)
    yield "bare", meeting
    for name, step in (("whole", summarized), ("named", named), ("again", again)):
        step(store, meeting)
        yield name, meeting


def expected(name: str) -> dict:
    return json.loads((EXPECTED / f"{name}.json").read_text(encoding="utf-8"))


def test_each_point_of_a_meetings_life_says_what_the_file_does(store: Store):
    seen = []
    for name, meeting in stages(store):
        assert zachcore.message(store, meeting) == expected(name), name
        seen.append(name)
    assert tuple(seen) == STAGES


def test_the_files_say_what_this_test_is_about():
    """The expectations themselves, read for what they're there for."""
    bare, whole, named_, again_ = (expected(name) for name in STAGES)
    people = {person["name"]: person for person in whole["people"]}
    # You aren't among them; your talk is the meeting's share. Voices left out or too short to
    # tell, and lines with no voice, aren't anybody.
    assert set(people) == {"Dana Lee", "Speaker 2", "Tom Avery", "Speaker 4"}
    assert whole["share"] == 0.34
    assert people["Dana Lee"]["sure"] and not people["Tom Avery"]["sure"]
    # Rita is known only from a private meeting: her voice goes as its label.
    assert people["Speaker 4"]["ref"].startswith("v") and "Rita" not in json.dumps(whole)
    owners = {task["title"]: task["owner"] for task in whole["tasks"]}
    assert owners["Send Dana the rollback steps"] == zachcore.ME
    assert owners["Tell the help desk"] == zachcore.NOBODY
    assert owners["Check the firewall rules"] == people["Speaker 2"]["ref"]
    assert owners["Order the new cables"] == "Priya"
    assert owners["Call back about the badge"] == "Unknown"
    # A note's lines and a bookmark under My notes; no snapshot, no file.
    assert bare["overview"].startswith(zachcore.MY_NOTES)
    assert "0x80070005" not in json.dumps(whole) and "cutover.log" not in json.dumps(whole)
    # Named: the person instead of the label, the same tasks with the new owner.
    renamed = {person["name"]: person["ref"] for person in named_["people"]}
    assert [task["ref"] for task in named_["tasks"]] == [task["ref"] for task in whole["tasks"]]
    assert {task["title"]: task["owner"] for task in named_["tasks"]}[
        "Check the firewall rules"
    ] == renamed["Erin Moss"]
    # Summarized again: the same refs for the same tasks, and the one that went in gone.
    cables = next(task["ref"] for task in whole["tasks"] if "cables" in task["title"])
    assert again_["gone"] == [cables]
    assert {task["ref"] for task in again_["tasks"]} == {
        task["ref"] for task in whole["tasks"] if task["ref"] != cables
    }


def test_nothing_of_a_private_meeting(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    store.update_meeting(meeting, private=1)
    assert zachcore.message(store, meeting) is None
    assert zachcore.message(store, 999) is None


def test_making_it_writes_nothing(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    before = store.db.total_changes
    zachcore.message(store, meeting)
    assert store.db.total_changes == before and not store.db.in_transaction


@pytest.mark.parametrize(
    ("said", "keys"),
    [
        ((), set()),
        (("summaries",), {"overview", "decisions", "questions", "topics"}),
        (("tasks",), {"tasks"}),
        (("facts",), {"facts"}),
    ],
)
def test_the_switches_say_what_goes_with_the_meeting(store: Store, said, keys):
    meeting = call(store)
    summarized(store, meeting)
    head = {"ref", "title", "started", "seconds", "app", "client", "ticket", "share", "people"}
    assert set(zachcore.message(store, meeting, said)) == head | keys


def test_without_its_client_or_ticket_both_go_as_null_to_take_them_off(store: Store):
    meeting = call(store)
    assert zachcore.message(store, meeting)["client"] == "Contoso"
    store.update_meeting(meeting, client=None, client_source="user", ticket=None)
    sent = zachcore.message(store, meeting)
    assert sent["client"] is None and sent["ticket"] is None


def test_a_switch_on_sends_its_lists_even_empty_and_one_off_leaves_them_out(store: Store):
    """Sent, a list replaces what ZachCore has, empty too; left out, it stays as it is there."""
    meeting = call(store)
    sent = zachcore.message(store, meeting)  # all on, and nothing summarized yet
    for key in ("decisions", "questions", "topics", "tasks", "facts"):
        assert sent[key] == [], key
    assert "gone" not in sent  # nothing went: nothing to do
    store.delete_note(store.notes(meeting)[0]["id"])
    store.delete_note(store.notes(meeting)[0]["id"])
    assert zachcore.message(store, meeting)["overview"] == ""  # no notes left, no summary
    for part, keys in (("summaries", ("overview", "decisions")), ("tasks", ("tasks",))):
        others = [other for other in zachcore.PARTS if other != part]
        assert not set(keys) & set(zachcore.message(store, meeting, others)), part
    store.db.execute(
        "UPDATE meeting_speakers SET status = 'left_out' WHERE meeting_id = ?", (meeting,)
    )
    store.db.commit()
    assert zachcore.message(store, meeting)["people"] == []  # nobody listed now: none there


def test_a_client_set_on_the_meeting_wins(store: Store):
    meeting = call(store)
    store.update_meeting(meeting, client="Northwind", client_source="user")
    assert zachcore.message(store, meeting)["client"] == "Northwind"


def test_a_task_follows_its_voice_until_the_voice_is_gone(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    voices = {voice["label"]: voice["id"] for voice in store.speakers(meeting)}
    # Left out after it promised: nobody named; then processed again, so the voice is gone and
    # the name it was given counts.
    store.set_speaker_person(voices["Speaker 2"], None, "left_out")
    assert owner(store, meeting, "Check the firewall rules") == "Unknown"
    store.db.execute("DELETE FROM meeting_speakers WHERE id = ?", (voices["Speaker 2"],))
    store.db.commit()
    assert owner(store, meeting, "Check the firewall rules") == "Speaker 2"


def test_an_item_from_before_voices_were_kept_is_found_by_its_owners_name(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    store.db.execute("UPDATE action_items SET voice_id = NULL")
    store.db.commit()
    assert owner(store, meeting, "Book the server room") == "person-dana-lee"
    assert owner(store, meeting, "Share the floor plan") == "person-tom-avery"
    assert owner(store, meeting, "Send Dana the rollback steps") == zachcore.ME


def test_your_task_is_yours_without_your_voice(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    store.db.execute("DELETE FROM meeting_speakers WHERE label = 'You'")
    store.db.commit()
    assert owner(store, meeting, "Send Dana the rollback steps") == zachcore.ME


def test_an_owner_without_a_name_is_nobody(store: Store):
    meeting = call(store)
    store.replace_action_items(meeting, [{"owner": " ", "task": "Clean up", "said_at": 5.0}])
    assert owner(store, meeting, "Clean up") == zachcore.NOBODY


def test_a_voice_nobody_speaks_with_is_there_for_what_it_promised(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    voices = {voice["label"]: voice["id"] for voice in store.speakers(meeting)}
    store.db.execute("DELETE FROM segments WHERE meeting_speaker_id = ?", (voices["Speaker 2"],))
    store.db.commit()
    sent = zachcore.message(store, meeting)
    unnamed = next(person for person in sent["people"] if person["name"] == "Speaker 2")
    assert unnamed["share"] == 0
    store.replace_action_items(meeting, [])
    store.save_summary(meeting, "test", {**CONTENT, "facts": []}, 1, 1, 0.0)
    assert "Speaker 2" not in [
        person["name"] for person in zachcore.message(store, meeting)["people"]
    ]


def test_facts_about_the_client_need_one(store: Store):
    meeting = call(store)
    summarized(store, meeting)
    store.update_meeting(meeting, client=None, client_source="user")
    assert all(fact["about"] != "client" for fact in zachcore.message(store, meeting)["facts"])


@pytest.mark.parametrize(
    ("said_at", "sec"),
    [(296.0, 296), ("04:56", 296), ("[1:02:03]", 3723), (12.25, 12.2), ("soon", None),
     (-1, None), (float("inf"), None), (float("nan"), None), (True, None), (None, None)],
)  # fmt: skip
def test_a_moment_is_seconds_into_the_meeting_or_nothing(store: Store, said_at, sec):
    meeting = call(store)
    store.save_summary(
        meeting, "test", {"decisions": ["Go"], "decisions_said_at": [said_at]}, 1, 1, 0.0
    )
    (decision,) = zachcore.message(store, meeting)["decisions"]
    assert decision.get("sec") == sec
    json.dumps(decision, allow_nan=False)


def test_the_switches_are_on_until_turned_off(store: Store):
    assert zachcore.parts(store) == set(zachcore.PARTS)
    store.set_setting(zachcore.setting("facts"), "0")
    assert zachcore.parts(store) == {"summaries", "tasks"}
    assert outbox.TRANSCRIPTS == zachcore.setting("transcripts")


# Which voice a name from a summary is ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("Speaker 2", "Speaker 2"),
        ("speaker  2", "Speaker 2"),
        ("Dana Lee", "Speaker 1"),
        ("Dana", "Speaker 1"),
        ("Speaker 3 (Tom Avery?)", "Speaker 3"),
        ("Tom", "Speaker 3"),
        ("Speaker 4", "Speaker 4"),
        ("Speaker 4 (Rita Kim?)", "Speaker 4"),  # by its label; Claude never saw Rita
        ("You", "You"),
        ("me", "You"),
        ("Rita Kim", None),  # Claude never saw her: anybody else of that name
        ("Unknown", None),
        ("Speaker 5", None),  # left out: Claude saw it as Unknown
        ("Unassigned", None),
        ("Priya", None),
        ("", None),
    ],
)
def test_a_name_from_a_summary_is_the_voice_claude_saw_it_as(store: Store, name, label):
    meeting = call(store)
    voices = {voice["id"]: voice["label"] for voice in store.speakers(meeting)}
    assert voices.get(store.voice_named(meeting, name)) == label


def test_a_name_two_people_have_is_neither(store: Store):
    meeting = call(store)
    store.add_speaker(
        meeting, "system", "Speaker 7", None, 30.0, None,
        Match(person(store, "Dana Moss"), 0.9, "confirmed"),
    )  # fmt: skip
    assert store.voice_named(meeting, "Dana") is None
    assert store.voice_named(meeting, "Dana Moss") is not None


def test_one_person_on_two_voices_is_the_one_that_talked_more(store: Store):
    meeting = call(store)
    dana = store.person_id("Dana Lee")
    more = store.add_speaker(meeting, "mic", "Speaker 8", None, 600.0, None, Match(dana, 1, "auto"))
    assert store.voice_named(meeting, "Dana Lee") == more


def owner(store: Store, meeting: int, task: str) -> str:
    sent = zachcore.message(store, meeting)
    return next(item["owner"] for item in sent["tasks"] if item["title"] == task)
