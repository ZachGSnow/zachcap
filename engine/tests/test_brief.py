"""Brief voices: who is left out of a meeting's speakers, what becomes of their lines, and how
meetings follow the setting."""

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

from zachcap import (
    api,
    autokind,
    brief,
    cli,
    export,
    insights,
    mcp_server,
    mentions,
    pipeline,
    redact,
)
from zachcap.api import create_app
from zachcap.diarize import EMBEDDING_MODEL, Diarization
from zachcap.store import CALL_AUDIO, Store
from zachcap.transcript import Turn, Word
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)
AUTH = {"Authorization": "Bearer test-token"}
VOICE = np.array([1.0, 0.0, 0.0, 0.0], np.float32)


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


def add_meeting(store: Store, *voices: tuple, status: str = "ready", title: str = "Sync") -> int:
    """A meeting with one line from each voice, one after the other: (label, status, seconds),
    and False for a voice without an embedding. The label is the person's name when somebody is
    known to be it; with None, the line has no voice."""
    meeting = store.create_meeting(title, "2026-11-20T09:00:00", Path("somewhere"), duration=600.0)
    at = 0.0
    for label, kind, seconds, *embedded in voices:
        voice = person = None
        if label == "You":
            person = store.me()["id"]
        elif kind in ("auto", "confirmed", "suggested"):
            person = store.person_id(label)
        if label is not None:
            voice = store.add_speaker(
                meeting,
                "system",
                label,
                None if False in embedded else VOICE,
                round(seconds, 1),
                (at, at + seconds),
                Match(person, 0.9, kind),
            )
        said = f"This is line {label}."
        store.add_segments(meeting, [(voice, "system", at, round(at + seconds, 2), said, 0.9)])
        at = round(at + seconds + 2, 2)
    store.update_meeting(meeting, status=status)
    return meeting


def statuses(store: Store, meeting: int) -> dict[str, str]:
    return {voice["label"]: voice["status"] for voice in store.speakers(meeting)}


def out_of(store: Store, meeting: int) -> list[str]:
    """The voices left out, by label."""
    return [label for label, status in statuses(store, meeting).items() if status == "left_out"]


def talk(store: Store, meeting: int) -> dict[str, float]:
    """Each lane of the meeting's page, as the API has it."""
    unlisted = brief.unlisted(store, meeting)
    return {row["speaker"]: row["seconds"] for row in store.talk_time(meeting, unlisted)}


def names(store: Store, meeting: int) -> list[str]:
    return [line["speaker"] for line in store.transcript(meeting)]


# Who is left out --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "seconds", "out"),
    [
        (None, 4.9, True),  # of 100 in all: the usual 5%
        (None, 5.0, False),  # not under it
        ("5", 4.99, True),
        ("2", 1.9, True),
        ("2", 2.0, False),
        ("10", 9.9, True),
        ("10", 10.0, False),
        ("0", 0.1, False),  # off
        ("7", 4.9, True),  # not one of the choices: the usual one
        ("lots", 5.0, False),
    ],
)
def test_a_voice_nobody_knows_with_little_of_the_talk_is_left_out(store, setting, seconds, out):
    if setting is not None:
        store.set_setting(brief.SETTING, setting)
    meeting = add_meeting(store, ("Dana", "auto", 100 - seconds), ("Speaker 1", "unknown", seconds))
    assert brief.apply(store, meeting) is out
    assert out_of(store, meeting) == (["Speaker 1"] if out else [])


def test_the_share_is_exact():
    # Both are 5%, though in seconds 0.29 of 5.8 comes out as a little less.
    for mine, total in ((7, 140), (29, 580)):
        assert not brief.is_brief(2, {1: total - mine, 2: mine}, 5)
        assert brief.is_brief(2, {1: total - mine + 1, 2: mine}, 5)


def test_nobody_is_left_out_where_many_people_each_say_a_little(store):
    crowd = [(f"Speaker {n}", "unknown", 3.0) for n in range(1, 31)]
    meeting = add_meeting(store, *crowd)
    assert not brief.apply(store, meeting)
    # The most talkative one has 12 of 100 seconds: 3 is a quarter of that, 2.9 is less.
    crowd = [(f"Speaker {n}", "unknown", 8.21) for n in range(1, 11)]
    meeting = add_meeting(
        store, ("Dana", "auto", 12.0), *crowd, ("Speaker 11", "unknown", 3.0),
        ("Speaker 12", "unknown", 2.9),
    )  # fmt: skip
    assert brief.apply(store, meeting)
    assert out_of(store, meeting) == ["Speaker 12"]


def test_only_a_voice_nobody_is_known_to_be_is_left_out(store):
    meeting = add_meeting(
        store,
        ("Dana", "auto", 90.0),
        ("Priya", "confirmed", 0.5),
        ("Marcus", "suggested", 0.5),
        ("You", "auto", 0.5),
        ("Speaker 1", "unknown", 0.5),
        ("Speaker 2", "minor", 0.5),
        ("Speaker 3", "left_out", 0.5),
        ("Speaker 4", "left_out", 9.0),
    )
    assert brief.apply(store, meeting)
    assert statuses(store, meeting) == {
        "Dana": "auto",
        "Priya": "confirmed",
        "Marcus": "suggested",
        "You": "auto",
        "Speaker 1": "left_out",
        "Speaker 2": "left_out",
        "Speaker 3": "left_out",
        "Speaker 4": "unknown",  # it's heard enough to be asked about
    }


def test_call_audio_is_everyone_on_the_call_so_it_stays(store):
    meeting = add_meeting(store, ("You", "auto", 99.0), (CALL_AUDIO, "minor", 1.0, False))
    assert not brief.apply(store, meeting)
    assert talk(store, meeting) == {"You": 99.0, "Call audio": 1.0}


def test_a_voice_whose_lines_went_to_others_says_nothing(store):
    meeting = add_meeting(store, ("Dana", "auto", 50.0), ("Speaker 1", "unknown", 20.0))
    line = store.transcript(meeting)[1]
    store.reassign_segment(line["id"], name="Dana")
    assert brief.apply(store, meeting)
    assert out_of(store, meeting) == ["Speaker 1"]
    assert names(store, meeting) == ["Dana", "Dana"]


# What becomes of it -------------------------------------------------------------------------


def test_what_a_left_out_voice_said_stays_as_unknown_and_it_is_no_speaker(store):
    meeting = add_meeting(
        store, ("Dana", "auto", 60.0), ("You", "auto", 30.0), ("Speaker 1", "unknown", 3.0),
        status="needs_names",
    )  # fmt: skip
    assert brief.apply(store, meeting)

    assert names(store, meeting) == ["Dana", "You", "Unknown"]
    assert [line["speaker"] for line in store.transcript(meeting, private=False)][-1] == "Unknown"
    assert store.speakers(meeting)[-1]["display_name"] == "Unknown"
    assert talk(store, meeting) == {"Dana": 60.0, "You": 30.0}
    listed = insights.meeting_insights(store, meeting)["speakers"]
    assert [speaker["speaker"] for speaker in listed] == ["Dana", "You"]
    assert store.meeting(meeting)["status"] == "ready"  # nobody is left to name
    # Found by what it said, and in the meeting's files.
    (hit,) = store.search('"line Speaker 1"')
    (line,) = store.search_lines('"line Speaker 1"')
    assert hit["speaker"] == line["speaker"] == "Unknown"
    assert "Unknown:** This is line Speaker 1." in export.markdown(store, meeting)
    assert "Unknown: This is line Speaker 1." in export.text(store, meeting)


def test_a_left_out_voice_can_still_say_your_name(store):
    store.rename_person(store.me()["id"], "Alex Rivera")
    meeting = add_meeting(store, ("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0))
    store.edit_segment(store.transcript(meeting)[1]["id"], "Alex, are you there?")
    brief.apply(store, meeting)
    (found,) = mentions.find(store, meeting)
    assert found["speaker"] == "Unknown"
    assert mentions.counts(store) == {meeting: 1}


def test_lines_with_no_voice_are_listed_only_when_they_are_many(store):
    few = add_meeting(store, ("Dana", "auto", 60.0), (None, None, 3.0))
    many = add_meeting(store, ("Dana", "auto", 60.0), (None, None, 4.0))
    assert talk(store, few) == {"Dana": 60.0}
    assert talk(store, many) == {"Dana": 60.0, "Unknown": 4.0}
    for meeting, listed in ((few, ["Dana"]), (many, ["Dana", "Unknown"])):
        speakers = insights.meeting_insights(store, meeting)["speakers"]
        assert [speaker["speaker"] for speaker in speakers] == listed
    assert names(store, few) == ["Dana", "Unknown"]  # nothing that was said is lost
    # As for a voice: a quarter of what the most talkative one has is too much.
    crowd = [(f"Speaker {n}", "unknown", 8.0) for n in range(1, 12)]
    assert "Unknown" in talk(store, add_meeting(store, *crowd, (None, None, 2.0)))
    assert "Unknown" not in talk(store, add_meeting(store, *crowd, (None, None, 1.9)))
    # They aren't a voice themselves: where hardly anybody was told apart, those still are.
    mostly = add_meeting(store, ("Speaker 1", "unknown", 4.0), (None, None, 96.0))
    assert not brief.apply(store, mostly)
    assert talk(store, mostly) == {"Unknown": 96.0, "Speaker 1": 4.0}
    assert talk(store, add_meeting(store, (None, None, 9.0))) == {"Unknown": 9.0}
    assert talk(store, add_meeting(store)) == {}
    store.set_setting(brief.SETTING, "0")  # off: all of them, as the voices
    assert talk(store, few) == {"Dana": 60.0, "Unknown": 3.0}


def test_left_out_voices_dont_count_with_the_lines_with_no_voice(store):
    meeting = add_meeting(
        store, ("Dana", "auto", 80.0), ("Speaker 1", "unknown", 4.0), (None, None, 8.0)
    )
    brief.apply(store, meeting)
    assert names(store, meeting) == ["Dana", "Unknown", "Unknown"]
    assert talk(store, meeting) == {"Dana": 80.0, "Unknown": 8.0}


def test_a_meeting_with_you_and_one_other_is_clear_despite_a_brief_voice(store):
    both = [("You", "auto", 40.0), ("Dana", "confirmed", 40.0)]
    meeting = add_meeting(store, *both, ("Speaker 1", "unknown", 3.0), (None, None, 2.0))
    assert not autokind.you_and_one_other(store, meeting)  # not left out yet
    brief.apply(store, meeting)
    assert autokind.you_and_one_other(store, meeting)
    for more in [("Speaker 1", "unknown", 6.0), (None, None, 6.0), ("Priya", "auto", 1.0)]:
        meeting = add_meeting(store, *both, more)
        brief.apply(store, meeting)
        assert not autokind.you_and_one_other(store, meeting), more


# Following the setting, and back ------------------------------------------------------------


def test_the_setting_off_brings_the_voices_back_as_they_were(store):
    meeting = add_meeting(
        store,
        ("Dana", "auto", 200.0),
        ("Speaker 1", "unknown", 3.0),
        ("Speaker 2", "unknown", 1.5),
        ("Speaker 3", "minor", 1.4),
        ("Speaker 4", "minor", 3.0, False),  # no embedding: nothing to recognize it by
        status="needs_names",
    )
    before = statuses(store, meeting)
    assert brief.apply(store, meeting)
    assert out_of(store, meeting) == [f"Speaker {n}" for n in range(1, 5)]
    assert store.meeting(meeting)["status"] == "ready"

    store.set_setting(brief.SETTING, "0")
    assert brief.apply(store, meeting)

    assert statuses(store, meeting) == before
    assert store.meeting(meeting)["status"] == "needs_names"
    assert names(store, meeting) == ["Dana", *(f"Speaker {n}" for n in range(1, 5))]


def test_applying_it_again_writes_nothing(store):
    meeting = add_meeting(
        store, ("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0), ("Speaker 2", "unknown", 20.0)
    )
    assert brief.apply(store, meeting)
    assert store.meeting(meeting)["status"] == "needs_names"  # Speaker 2 is asked about
    for _ in range(2):
        written = store.db.total_changes
        assert not brief.apply(store, meeting)
        assert store.db.total_changes == written
        assert not store.db.in_transaction
    store.set_setting(brief.SETTING, "0")
    assert brief.apply(store, meeting) and not brief.apply(store, meeting)


@pytest.mark.parametrize("status", ["recording", "recorded", "processing", "failed"])
def test_a_meeting_that_isnt_processed_is_left_alone(store, status):
    meeting = add_meeting(
        store, ("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0), status=status
    )
    written = store.db.total_changes
    assert not brief.apply(store, meeting)
    assert brief.apply_all(store) == []
    assert store.meeting(meeting)["status"] == status
    assert statuses(store, meeting)["Speaker 1"] == "unknown"
    assert store.db.total_changes == written + 1  # only that all meetings were gone through
    assert not brief.apply(store, meeting + 1)  # nor one that's gone


def test_naming_a_left_out_voice_brings_that_one_back(store):
    meeting = add_meeting(
        store, ("Dana", "auto", 80.0), ("Speaker 1", "unknown", 3.0), ("Speaker 2", "unknown", 2.0)
    )
    brief.apply(store, meeting)

    pipeline.name_speaker(store, meeting, "Speaker 1", "Priya")

    assert statuses(store, meeting) == {
        "Dana": "auto", "Speaker 1": "confirmed", "Speaker 2": "left_out"
    }  # fmt: skip
    assert names(store, meeting) == ["Dana", "Priya", "Unknown"]
    assert talk(store, meeting) == {"Dana": 80.0, "Priya": 3.0}
    assert store.voice_library(EMBEDDING_MODEL).keys() == {store.person_id("Priya")}
    assert store.participants()[meeting] == ["Dana", "Priya"]
    assert not brief.apply(store, meeting)  # hers to stay
    assert pipeline.confirm_matches(store, meeting) == 1  # Dana; a left-out voice isn't a match
    assert statuses(store, meeting)["Speaker 2"] == "left_out"
    assert store.meeting(meeting)["status"] == "ready"


def test_a_line_of_a_left_out_voice_can_be_given_to_somebody(store):
    meeting = add_meeting(store, ("Dana", "auto", 80.0), ("Speaker 1", "unknown", 3.0))
    brief.apply(store, meeting)
    store.reassign_segment(store.transcript(meeting)[1]["id"], name="Priya")
    assert names(store, meeting) == ["Dana", "Priya"]
    assert talk(store, meeting) == {"Dana": 80.0, "Priya": 3.0}


def test_a_left_out_voices_sample_leaves_what_is_redacted(store):
    meeting = add_meeting(store, ("Dana", "auto", 80.0), ("Speaker 1", "unknown", 3.0))
    (said,) = [line for line in store.transcript(meeting) if line["speaker"] == "Speaker 1"]
    voice = said["meeting_speaker_id"]
    store.add_segments(meeting, [(voice, "system", 100.0, 101.0, "Sorry.", 0.9)])
    brief.apply(store, meeting)

    redact.redact(store, meeting, said["start"], said["end"], with_audio=False)

    moved = store.speaker(voice)
    assert moved["status"] == "left_out"
    assert (moved["sample_start"], moved["sample_end"]) == (100.0, 101.0)
    assert names(store, meeting) == ["Dana", "Unknown"]


def test_a_voice_named_just_before_the_meeting_is_written_stays_named(store, tmp_path, monkeypatch):
    meeting = add_meeting(store, ("Dana", "auto", 80.0), ("Speaker 1", "unknown", 3.0))
    looked = []
    due = brief._due

    def named_after_the_first_look(store, meeting_id):
        found = due(store, meeting_id)
        looked.append((bool(found), store.db.in_transaction))
        if len(looked) == 1:
            other = Store(tmp_path / "test.db")  # the page's request
            pipeline.name_speaker(other, meeting_id, "Speaker 1", "Priya")
            other.close()
        return found

    monkeypatch.setattr(brief, "_due", named_after_the_first_look)
    assert not brief.apply(store, meeting)
    # Looked at again once nobody else can write.
    assert looked == [(True, False), (False, True)]
    assert statuses(store, meeting)["Speaker 1"] == "confirmed"
    assert not store.db.in_transaction


def test_every_meeting_follows_the_setting(store):
    some = [("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0)]
    first = add_meeting(store, *some)
    without = add_meeting(store, ("Dana", "auto", 60.0))
    last = add_meeting(store, *some, status="needs_names")
    live = add_meeting(store, *some, status="recording")
    assert brief.pending(store)

    assert brief.apply_all(store) == [first, last]

    assert not brief.pending(store)
    assert store.meeting(without)["status"] == store.meeting(last)["status"] == "ready"
    assert store.meeting(live)["status"] == "recording" and not out_of(store, live)
    assert brief.apply_all(store) == []  # they do already
    store.set_setting(brief.SETTING, "2")
    assert brief.pending(store)
    assert brief.apply_all(store) == [first, last]
    assert not brief.pending(store) and not out_of(store, first)


def test_a_busy_library_is_tried_again_and_else_left_for_the_next_start(
    store, tmp_path, monkeypatch
):
    meetings = [
        add_meeting(store, ("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0)) for _ in range(2)
    ]
    store.db.execute("PRAGMA busy_timeout = 20")  # each try waits this long (5 s, as it ships)
    writer = sqlite3.connect(tmp_path / "test.db")  # a meeting's results being saved, say
    writer.execute("BEGIN IMMEDIATE")
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)

    assert brief.apply_all(store) == []
    assert len(waits) == 2  # between three tries
    assert brief.pending(store) and not out_of(store, meetings[0])
    assert not store.db.in_transaction

    monkeypatch.setattr(time, "sleep", lambda seconds: writer.rollback())  # it's done by then
    assert brief.apply_all(store) == meetings
    assert not brief.pending(store)
    writer.close()


def test_what_changed_before_the_library_got_busy_is_told(store, monkeypatch):
    meetings = [
        add_meeting(store, ("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0)) for _ in range(3)
    ]
    apply = brief.apply

    def busy_at_the_second(store, meeting_id):
        if meeting_id == meetings[1]:
            raise sqlite3.OperationalError("database is locked")
        return apply(store, meeting_id)

    monkeypatch.setattr(brief, "apply", busy_at_the_second)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert brief.apply_all(store) == meetings[:1]  # exported again, like any that changed
    assert brief.pending(store)


# Pieces of sentences ------------------------------------------------------------------------


def line(voice, start, end, text, track="system"):
    return (voice, track, start, end, text, 0.9)


DANA, PRIYA, ONE, TWO = 1, 2, 3, 4  # the voices: ONE and TWO are left out
OUT = {None, ONE, TWO}


def given(*rows: tuple) -> list[int | None]:
    """Whose each line is, once the pieces of sentences went to whoever says the rest."""
    whole = brief.whole_sentences(list(rows), OUT)
    assert [row[1:] for row in whole] == [row[1:] for row in rows]  # only that changes
    return [row[0] for row in whole]


def test_pieces_of_a_sentence_go_to_whoever_says_the_rest():
    # As it came from one voice.
    assert given(
        line(ONE, 0.0, 0.3, "Good"),
        line(DANA, 0.4, 0.8, "morning."),
        line(ONE, 1.2, 1.4, "This"),
        line(DANA, 1.5, 6.0, "is the weekly sync, and we have an hour."),
    ) == [DANA, DANA, DANA, DANA]
    # The end of a sentence, and the middle of one.
    assert (
        given(
            line(DANA, 0.0, 2.0, "We move the mail server on"),
            line(ONE, 2.1, 2.4, "friday."),
            line(DANA, 3.0, 5.0, "The notice, as always,"),
            line(TWO, 5.1, 5.4, "goes out"),
            line(DANA, 5.5, 6.0, "on Tuesday."),
        )
        == [DANA] * 5
    )
    # A line with no voice as well.
    assert given(line(None, 0.0, 0.3, "So"), line(DANA, 0.9, 2.0, "that's that.")) == [DANA, DANA]


def test_a_line_that_may_be_somebodys_own_stays():
    for text in ("Yes.", "Yes", "yes", "Right!", "Friday?", "Hmm...", "Wait\u2026", '"Yes."'):
        rows = [
            line(DANA, 0.0, 2.0, "Is that all right?"),
            line(ONE, 2.2, 2.5, text),
            line(DANA, 2.6, 4.0, "Then we go on."),
        ]
        assert given(*rows) == [DANA, ONE, DANA], text
    # The end of a sentence that the next one just happens not to start with a capital.
    for text in ("Sure.", "Sure!", "Sure?", "Sure...", "Sure\u2026", 'She said "sure."', "(Sure.)"):
        rows = [line(ONE, 0.0, 0.3, text), line(DANA, 0.4, 2.0, "eBay is up again.")]
        assert given(*rows) == [ONE, DANA], text
    # The start of a new one: a capital, or a digit, where a sentence may start too.
    for text in ("Then we go on.", "I think so.", "10 minutes are left."):
        rows = [line(ONE, 0.0, 0.3, "okay"), line(DANA, 0.4, 2.0, text)]
        assert given(*rows) == [ONE, DANA], text


def test_a_quote_before_a_sentence_or_after_it_doesnt_hide_it():
    rows = [line(ONE, 0.0, 0.3, "He said"), line(DANA, 0.4, 2.0, '"it is down" again.')]
    assert given(*rows) == [DANA, DANA]
    rows = [line(DANA, 0.0, 2.0, 'He said "it is down."'), line(ONE, 2.1, 2.4, "again")]
    assert given(*rows) == [DANA, ONE]


def test_a_text_without_capitals_isnt_moved():
    # Where a sentence starts can't be told in Chinese, Japanese or Korean, or without a letter.
    for text in ("\u8fd9\u662f", "\u4eca\u65e5\u306f", "\ud68c\uc758\ub294", "10", "..."):
        rows = [line(ONE, 0.0, 0.3, text), line(DANA, 0.4, 2.0, "the meeting.")]
        assert given(*rows) == [ONE, DANA], text
    # Where it ends can.
    for mark in ("\u3002", "\uff01", "\uff1f", "\uff0e", "\uff61", "\u3002\u300d", "\u3002\u201d"):
        rows = [line(DANA, 0.0, 2.0, f"Beijing \u5317\u4eac{mark}"), line(ONE, 2.1, 2.4, "really")]
        assert given(*rows) == [DANA, ONE], mark
    # Nor does it take a piece: it starts with no lowercase letter.
    rows = [line(ONE, 0.0, 0.3, "The"), line(DANA, 0.4, 2.0, "\u5317\u4eac office.")]
    assert given(*rows) == [ONE, DANA]


def test_a_piece_is_at_most_a_second_away():
    for gap, whose in ((0.0, DANA), (-0.1, DANA), (1.0, DANA), (1.01, ONE)):
        start = round(2.2 + gap, 2)
        rows = [line(ONE, 2.0, 2.2, "Good"), line(DANA, start, start + 2, "morning.")]
        assert given(*rows) == [whose, DANA], gap
        rows = [line(DANA, 0.0, 2.2, "We start on"), line(ONE, start, start + 0.3, "monday.")]
        assert given(*rows) == [DANA, whose], gap
    # 2.2 - 1.2 is a little more than 1 as a float.
    assert given(line(ONE, 1.0, 1.2, "Good"), line(DANA, 2.2, 3.0, "morning.")) == [DANA, DANA]


def test_a_piece_goes_with_the_nearer_of_two_neighbours():
    def between(before: float, after: float) -> int:
        rows = [
            line(DANA, 0.0, 2.0, "So the plan is"),
            line(ONE, 2.0 + before, 3.0 + before, "to move"),
            line(PRIYA, 3.0 + before + after, 9.0, "it on Friday."),
        ]
        return given(*rows)[1]

    assert between(0.2, 0.5) == DANA
    assert between(0.5, 0.2) == PRIYA
    assert between(1.5, 0.9) == PRIYA  # the only one near
    assert between(0.3, 0.3) == DANA  # whose sentence it goes on with


def test_pieces_only_go_along_their_own_track():
    rows = [
        line(ONE, 0.0, 0.3, "Good"),
        line(DANA, 0.2, 0.9, "morning.", track="mic"),  # your own room, at the same time
        line(PRIYA, 2.0, 4.0, "Hello everybody."),
    ]
    assert given(*rows) == [ONE, DANA, PRIYA]
    rows[2] = line(PRIYA, 1.0, 4.0, "to everybody.")  # the next on the call's own
    assert given(*rows) == [PRIYA, DANA, PRIYA]


def test_a_piece_isnt_given_to_somebody_left_out():
    rows = [line(ONE, 0.0, 0.3, "Good"), line(TWO, 0.4, 0.8, "morning.")]
    assert given(*rows) == [ONE, TWO]
    rows = [line(ONE, 0.0, 0.3, "Good"), line(None, 0.4, 0.8, "morning.")]
    assert given(*rows) == [ONE, None]
    # Unless that one's a piece of somebody's sentence too.
    rows = [
        line(ONE, 0.0, 0.3, "Good"),
        line(TWO, 0.4, 0.8, "morning"),
        line(None, 0.9, 1.2, "to"),
        line(DANA, 1.8, 2.5, "everybody."),
    ]
    assert given(*rows) == [DANA] * 4
    assert given() == []


# While a meeting is processed ----------------------------------------------------------------


SECONDS = 0.3  # each word


def process(store: Store, monkeypatch, meeting: int, *said: tuple, separated: bool = True) -> None:
    """Processes a meeting in which this was said on the call, with stand-ins for the models:
    (speaker, seconds of silence before, text), None being nobody the speaker model found."""
    words, turns = [], []
    at = 0.0
    for speaker, silence, text in said:
        start = at = at + silence
        for word in text.split():
            words.append(Word(at, at + SECONDS, f" {word}"))
            at += SECONDS
        if speaker is not None:
            turns.append(Turn(start, at, speaker))
    result = Diarization(turns, VOICES)
    monkeypatch.setattr(pipeline, "_load_tracks", lambda *args: {"system": np.zeros(16000)})
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: {"system": words})
    monkeypatch.setattr(pipeline, "_diarize", lambda *args: ({"system": result}, separated))
    pipeline.process(store, meeting, log=lambda message: None)


VOICES = {
    "dana": VOICE,
    "someone": np.array([0.0, 1.0, 0.0, 0.0], np.float32),
    "else": np.array([0.0, 0.0, 1.0, 0.0], np.float32),
}
LONG = "We go through the open tickets one by one and say who does what and when. " * 12  # 58 s
CALL = [
    ("else", 0.0, "Good"),
    ("dana", 0.1, "morning."),
    ("else", 0.4, "This"),
    ("dana", 0.1, f"is the weekly sync. {LONG}Is that all right?"),
    ("else", 0.3, "Yes."),
    ("dana", 0.2, "Then I'll send the notes."),
    ("someone", 5.0, "We could also do that on Monday."),
]


@pytest.fixture
def call(store, tmp_path):
    meeting = store.create_meeting("Weekly sync", "2026-11-22T20:00:00", tmp_path)
    store.add_voice_sample(store.person_id("Dana"), 99, VOICE, EMBEDDING_MODEL, 30.0)
    return meeting


def test_a_meeting_is_processed_without_its_brief_voices(store, call, monkeypatch):
    process(store, monkeypatch, call, *CALL)

    said = [(row["speaker"], row["text"]) for row in store.transcript(call)]
    assert said[0][0] == "Dana"
    assert said[0][1].startswith("Good morning. This is the weekly sync. We go through the")
    assert said[1:] == [
        ("Unknown", "Yes."),  # somebody's answer, maybe
        ("Dana", "Then I'll send the notes."),
        ("Unknown", "We could also do that on Monday."),
    ]
    assert statuses(store, call) == {
        "Speaker 1": "left_out", "Dana": "auto", "Speaker 2": "left_out"
    }  # fmt: skip
    assert store.meeting(call)["status"] == "ready"
    assert list(talk(store, call)) == ["Dana"]
    written = store.db.total_changes
    assert not brief.apply(store, call)  # it follows the setting already
    assert store.db.total_changes == written

    store.set_setting(brief.SETTING, "0")  # back to be asked about: too short a voice isn't
    assert brief.apply(store, call)
    assert statuses(store, call) == {"Speaker 1": "minor", "Dana": "auto", "Speaker 2": "unknown"}
    assert store.meeting(call)["status"] == "needs_names"
    assert [row["text"] for row in store.transcript(call)] == [text for _, text in said]


def test_with_the_setting_off_a_meeting_is_processed_as_it_was(store, call, monkeypatch):
    store.set_setting(brief.SETTING, "0")
    process(store, monkeypatch, call, *CALL, (None, 5.0, "and"))

    said = [(row["speaker"], row["text"][:20]) for row in store.transcript(call)]
    assert said == [
        ("Speaker 1", "Good"),
        ("Dana", "morning."),
        ("Speaker 1", "This"),
        ("Dana", "is the weekly sync. "),
        ("Speaker 1", "Yes."),
        ("Dana", "Then I'll send the n"),
        ("Speaker 2", "We could also do tha"),
        ("Unknown", "and"),
    ]
    assert store.meeting(call)["status"] == "needs_names"
    assert "Unknown" in talk(store, call)


def test_a_piece_with_no_voice_goes_to_whoever_says_the_rest(store, call, monkeypatch):
    # Too far from Dana's turns for the speaker model to count it to one of them.
    said = [("dana", 0.0, f"{LONG}And"), (None, 0.6, "that"), ("dana", 0.6, "is all.")]
    process(store, monkeypatch, call, *said)
    (row,) = store.transcript(call)
    assert row["speaker"] == "Dana" and row["text"].endswith("And that is all.")
    store.set_setting(brief.SETTING, "0")
    process(store, monkeypatch, call, *said)
    assert names(store, call) == ["Dana", "Unknown", "Dana"]  # off: as it came


def test_processing_again_goes_by_the_setting_then(store, call, monkeypatch):
    process(store, monkeypatch, call, *CALL)
    store.set_setting(brief.SETTING, "0")
    process(store, monkeypatch, call, *CALL)
    assert out_of(store, call) == [] and "Speaker 1" in names(store, call)
    store.set_setting(brief.SETTING, "2")
    process(store, monkeypatch, call, *CALL)
    assert out_of(store, call) == ["Speaker 1"]


def test_a_failed_reprocess_follows_a_setting_changed_meanwhile(store, call, monkeypatch):
    process(store, monkeypatch, call, *CALL)
    assert store.meeting(call)["status"] == "ready"

    def changed_meanwhile(*args):
        store.set_setting(brief.SETTING, "0")
        assert brief.apply_all(store) == []  # it's being processed
        raise RuntimeError("out of memory")

    monkeypatch.setattr(pipeline, "_transcribe", changed_meanwhile)
    with pytest.raises(RuntimeError, match="out of memory"):
        pipeline.process(store, call, log=lambda message: None)
    assert out_of(store, call) == []  # what it keeps is as the setting has it
    assert store.meeting(call)["status"] == "needs_names"


def test_a_call_whose_speakers_couldnt_be_separated_keeps_call_audio(store, call, monkeypatch):
    process(store, monkeypatch, call, ("system", 0.0, "Hello there."), separated=False)
    assert statuses(store, call) == {CALL_AUDIO: "minor"}
    assert store.meeting(call)["status"] == "ready"
    mine = store.add_speaker(
        call, "mic", "You", None, 90.0, None, Match(store.me()["id"], 0.0, "auto")
    )
    store.add_segments(call, [line(mine, 5.0, 95.0, "Let me explain.", track="mic")])
    assert not brief.apply(store, call)
    assert talk(store, call) == {"You": 90.0, "Call audio": pytest.approx(0.6)}


def test_the_command_line_shows_a_left_out_voice_and_doesnt_ask_for_its_name(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    meeting = add_meeting(store, ("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0))
    brief.apply(store, meeting)
    store.close()

    cli.main(["show", str(meeting)])
    shown = capsys.readouterr().out
    assert "3.0 s  left out" in shown and "Unknown                This is line" in shown
    assert "need names" not in shown

    cli.main(["name", str(meeting), "Speaker 1", "Priya"])
    assert "confirmed" in capsys.readouterr().out


# Through the API ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    yield TestClient(create_app("test-token"), base_url=LOCAL)
    finish()  # nothing goes on in the next test's library


def finish() -> None:
    """Waits for what a request has started: going through the meetings, then exporting some."""
    for name in ("zachcap-brief", "zachcap-export"):
        for thread in threading.enumerate():
            if thread.name == name:
                thread.join(30)


def library(*meetings: list[tuple]) -> list[int]:
    store = Store()
    made = [add_meeting(store, *voices, status="needs_names") for voices in meetings]
    store.close()
    return made


def settled() -> bool:
    """Whether every meeting follows the setting."""
    store = Store()
    try:
        return not brief.pending(store)
    finally:
        store.close()


SOME = [("Dana", "auto", 60.0), ("Speaker 1", "unknown", 3.0), ("Speaker 2", "unknown", 1.0)]


def set_percent(client: TestClient, percent) -> dict:
    response = client.put("/api/settings", headers=AUTH, json={"leave_out_brief": percent})
    assert response.status_code == 200, response.text
    return response.json()


def detail(client: TestClient, meeting: int) -> dict:
    return client.get(f"/api/meetings/{meeting}", headers=AUTH).json()


def test_the_setting(client: TestClient):
    assert client.get("/api/settings", headers=AUTH).json()["leave_out_brief"] == 5
    for percent in (0, 2, 10, 5):
        assert set_percent(client, percent)["leave_out_brief"] == percent
    for wrong in (1, -5, 100, 5.5, "lots"):
        refused = client.put("/api/settings", headers=AUTH, json={"leave_out_brief": wrong})
        assert refused.status_code in (400, 422), wrong
    assert client.get("/api/settings", headers=AUTH).json()["leave_out_brief"] == 5
    finish()
    assert settled()
    store = Store()
    store.set_setting(brief.SETTING, "lots")  # as no ZachCap saves it
    store.close()
    assert client.get("/api/settings", headers=AUTH).json()["leave_out_brief"] == 5


def test_a_meetings_page_has_the_voices_left_out(client: TestClient):
    (meeting,) = library(SOME)
    client.app.state.leave_out_brief()  # as serve() does, on a thread

    page = detail(client, meeting)

    assert page["meeting"]["status"] == "ready"
    assert [row["speaker"] for row in page["talk_time"]] == ["Dana"]
    assert [row["speaker"] for row in page["insights"]["speakers"]] == ["Dana"]
    listed = {s["label"]: (s["status"], s["display_name"]) for s in page["speakers"]}
    assert listed == {
        "Dana": ("auto", "Dana"),
        "Speaker 1": ("left_out", "Unknown"),
        "Speaker 2": ("left_out", "Unknown"),
    }
    lines = [(s["speaker"], s["speaker_status"]) for s in page["segments"]]
    assert lines == [("Dana", "auto"), ("Unknown", "left_out"), ("Unknown", "left_out")]
    voice = page["speakers"][1]
    named = client.post(
        f"/api/meetings/{meeting}/speakers/{voice['id']}/name", headers=AUTH, json={"name": "Priya"}
    )
    assert named.json() == {"status": "ready"}
    page = detail(client, meeting)
    assert [row["speaker"] for row in page["talk_time"]] == ["Dana", "Priya"]
    assert page["speakers"][1]["status"] == "confirmed"


def test_meetings_there_are_follow_the_setting_when_its_changed(client: TestClient, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    first, second = library(SOME, [("Dana", "auto", 60.0), ("Speaker 1", "unknown", 30.0)])
    client.app.state.leave_out_brief()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(vault)})
    copy = {meeting: next(vault.glob(f"*(M{meeting}).md")) for meeting in (first, second)}
    assert "Unknown:** This is line Speaker 1." in copy[first].read_text(encoding="utf-8")
    untouched = copy[second].stat().st_mtime_ns

    set_percent(client, 0)
    finish()
    assert detail(client, first)["meeting"]["status"] == "needs_names"
    assert "Speaker 1:** This is" in copy[first].read_text(encoding="utf-8")
    set_percent(client, 2)
    finish()
    labels = {s["label"]: s["status"] for s in detail(client, first)["speakers"]}
    assert labels == {"Dana": "auto", "Speaker 1": "unknown", "Speaker 2": "left_out"}
    assert "Unknown:** This is line Speaker 2" in copy[first].read_text(encoding="utf-8")
    assert copy[second].stat().st_mtime_ns == untouched  # only what changed is written again


def test_changes_of_the_setting_meanwhile_are_applied_after(client: TestClient, monkeypatch):
    (meeting,) = library(SOME)
    client.app.state.leave_out_brief()
    going, go_on, running, most = threading.Event(), threading.Event(), [], []
    apply_all = brief.apply_all

    def slowly(store):
        running.append(store)
        most.append(len(running))
        going.set()
        try:
            assert go_on.wait(10)
            return apply_all(store)
        finally:
            running.remove(store)

    monkeypatch.setattr(brief, "apply_all", slowly)
    set_percent(client, 0)
    assert going.wait(10)  # going through the meetings...
    for percent in (10, 2, 0, 2):  # ...as it's changed again and again
        set_percent(client, percent)
    go_on.set()
    finish()

    assert settled()
    labels = {s["label"]: s["status"] for s in detail(client, meeting)["speakers"]}
    assert labels == {"Dana": "auto", "Speaker 1": "unknown", "Speaker 2": "left_out"}  # as at 2
    assert max(most) == 1  # one at a time


def test_meetings_from_before_follow_the_default_once(client: TestClient, monkeypatch):
    first, second = library(SOME, SOME)
    gone_through = []
    apply_all = brief.apply_all

    def counted(store):
        gone_through.append(apply_all(store))
        return gone_through[-1]

    monkeypatch.setattr(brief, "apply_all", counted)
    for _ in range(3):  # three starts
        client.app.state.leave_out_brief()
    assert gone_through == [[first, second]]
    assert detail(client, second)["meeting"]["status"] == "ready"
    set_percent(client, 5)  # what it is already
    assert settled() and len(gone_through) == 1


def test_a_pass_that_was_cut_off_is_done_at_the_next_start(client: TestClient, monkeypatch):
    first, second = library(SOME, SOME)
    client.app.state.leave_out_brief()
    apply = brief.apply

    def cut_off(store, meeting_id):
        if meeting_id == second:
            raise KeyError("the engine was killed, say")
        return apply(store, meeting_id)

    store = Store()
    store.set_setting(brief.SETTING, "0")
    with monkeypatch.context() as patch:
        patch.setattr(brief, "apply", cut_off)
        client.app.state.leave_out_brief()
    assert out_of(store, first) == [] and out_of(store, second) != []
    # Set back to what it was: one meeting has changed all the same.
    store.set_setting(brief.SETTING, "5")
    assert brief.pending(store)

    client.app.state.leave_out_brief()

    assert out_of(store, first) == out_of(store, second) == ["Speaker 1", "Speaker 2"]
    store.close()


def test_the_engine_has_meetings_follow_it_as_it_starts(tmp_path, monkeypatch):
    started = []

    class Thread:  # the engine's threads run as long as it does: none is started here
        def __init__(self, *args, target=None, name="", **kwargs) -> None:
            self.target, self.name = target, name

        def start(self) -> None:
            started.append(self)

    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.delenv("ZACHCAP_PARENT_PID", raising=False)
    monkeypatch.delenv("ZACHCAP_PORT_FILE", raising=False)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets: sockets[0].close())
    (meeting,) = library(SOME)
    with monkeypatch.context() as patch:
        patch.setattr(threading, "Thread", Thread)
        api.serve(0, "test-token")

    store = Store()
    assert store.meeting(meeting)["status"] == "needs_names"  # the start doesn't wait for it
    (follow,) = [thread for thread in started if thread.name == "zachcap-brief"]
    follow.target()
    assert store.meeting(meeting)["status"] == "ready"
    assert out_of(store, meeting) == ["Speaker 1", "Speaker 2"]
    store.close()


def test_deleting_a_person_leaves_their_brief_voices_out(client: TestClient, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    short, long, both = library(
        [("Dana", "auto", 60.0), ("Priya", "confirmed", 2.0)],
        [("Dana", "auto", 60.0), ("Priya", "confirmed", 30.0)],
        [("Dana", "auto", 60.0), ("Priya", "confirmed", 2.0), ("Speaker 1", "unknown", 30.0)],
    )
    store = Store()
    for meeting in (short, long):
        store.update_meeting(meeting, status="ready")
    priya = store.person_id("Priya")
    store.close()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(vault)})

    assert client.delete(f"/api/people/{priya}", headers=AUTH).json() == {"ok": True}

    store = Store()
    assert statuses(store, short) == {"Dana": "auto", "Priya": "left_out"}
    assert names(store, short) == ["Dana", "Unknown"]
    assert store.meeting(short)["status"] == "ready"
    assert statuses(store, long) == {"Dana": "auto", "Priya": "unknown"}  # to be named
    assert statuses(store, both)["Priya"] == "left_out"
    assert store.meeting(both)["status"] == "needs_names"
    store.close()
    finish()
    copy = next(vault.glob(f"*(M{short}).md"))
    assert "Unknown:** This is line Priya." in copy.read_text(encoding="utf-8")


def test_the_mcp_server_reads_a_library_with_voices_left_out(tmp_path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    (meeting,) = library(SOME)
    store = Store()
    brief.apply_all(store)
    store.close()
    server = mcp_server.create_server()

    def call(tool: str, arguments: dict) -> str:
        result = asyncio.run(server.call_tool(tool, arguments))
        assert not result.is_error
        return result.content[0].text

    whole = call("get_meeting", {"meeting_id": meeting})
    assert "[01:02] Unknown: This is line Speaker 1." in whole and "Speaker 1:" not in whole
    assert "Unknown: This is line" in call("search_meetings", {"query": "line Speaker"})
    assert "Dana. (not summarized)" in call("list_meetings", {})
    reader = Store(readonly=True)  # SQLite refuses any write
    assert brief.unlisted(reader, meeting) == {2, 3, None}
    assert talk(reader, meeting) == {"Dana": 60.0}
    assert reader.db.total_changes == 0
    reader.close()


def test_a_library_from_before_opens_as_it_is(tmp_path):
    store = Store(tmp_path / "old.db")
    meeting = add_meeting(store, *SOME, (None, None, 1.0), status="needs_names")
    store.close()

    def dump() -> list[str]:
        db = sqlite3.connect(tmp_path / "old.db")
        kept = list(db.iterdump())
        db.close()
        return kept

    before = dump()  # as 0.4.0 wrote it: with neither the setting nor the status
    data = [row for row in before if row.startswith("INSERT")]
    assert not any("left_out" in row or "leave_out" in row for row in data)
    Store(tmp_path / "old.db").close()
    assert dump() == before

    store = Store(tmp_path / "old.db")
    assert statuses(store, meeting)["Speaker 1"] == "unknown"
    assert brief.apply_all(store) == [meeting]
    store.close()
    tables = {row.split()[2].strip('"') for row in set(dump()) ^ set(before) if "INSERT" in row}
    assert tables == {"changes", "meeting_speakers", "meetings", "settings"}
