"""What is to be sent to ZachCore, as the library keeps it: ids, revisions and the outbox."""

import re
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from zachcap import outbox, redact
from zachcap.store import Store
from zachcap.voiceprint import Match

OLD = Path(__file__).parent / "fixtures" / "library-0.5.0.sql"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    yield store
    store.close()


@pytest.fixture
def on(store: Store) -> Store:
    store.set_setting(outbox.ON, "1")
    store.set_setting(outbox.TRANSCRIPTS, "1")
    return store


def add(store: Store, title: str = "Sync", folder: Path = Path("somewhere")) -> int:
    """A processed meeting: one voice without a name, three lines and two action items."""
    meeting = store.create_meeting(title, "2026-11-25T09:00:00-05:00", folder, duration=600.0)
    voice = store.add_speaker(
        meeting, "system", "Speaker 1", np.ones(4), 60.0, (1.0, 4.0), Match(None, 0.1, "unknown")
    )
    lines = [(8.0, "Hello there"), (20.0, "I will send it"), (30.0, "See you")]
    store.add_segments(meeting, [(voice, "system", at, at + 5, text, 0.9) for at, text in lines])
    store.replace_action_items(
        meeting,
        [
            {"owner": "Dana", "task": "Send the cutover plan to Tom", "said_at": 20.0},
            {"owner": "Dana", "task": "Book the server room", "said_at": 31.0},
        ],
    )
    store.refresh_status(meeting)
    return meeting


def uid(store: Store, meeting: int) -> str:
    return store.meeting(meeting)["uid"]


def rev(store: Store, meeting: int) -> int:
    row = store.db.execute("SELECT rev FROM sends WHERE uid = ?", (uid(store, meeting),))
    return row.fetchone()["rev"]


def waiting(store: Store, of: str | None = None) -> list[str]:
    rows = store.db.execute(
        "SELECT what FROM outbox WHERE ? IS NULL OR uid = ? ORDER BY what, id", (of, of)
    )
    return [row["what"] for row in rows]


def empty(store: Store) -> None:
    """As after a while in which everything was taken by the server."""
    store.db.execute("DELETE FROM outbox")
    store.db.execute("UPDATE sends SET target = 'somewhere'")
    store.db.commit()


def uids(store: Store, meeting: int) -> dict[str, str]:
    return {item["task"]: item["uid"] for item in store.action_items(meeting_id=meeting)}


# Ids --------------------------------------------------------------------------------------


def test_everything_new_has_a_uid_of_its_own(store: Store):
    meetings = [add(store, f"Meeting {n}") for n in range(3)]
    store.person_id("Dana")
    for table, count in (("meetings", 3), ("people", 2), ("action_items", 6)):
        found = [row["uid"] for row in store.db.execute(f"SELECT uid FROM {table}")]
        assert len(set(found)) == count and all(UUID.fullmatch(u) for u in found), table
    before = uid(store, meetings[2])
    store.delete_meeting(meetings[2])
    assert uid(store, add(store)) != before


def test_a_second_summary_in_other_words_keeps_the_uids(store: Store):
    meeting = add(store)
    plan, room = uids(store, meeting).values()
    store.replace_action_items(
        meeting,
        [
            {"owner": "Dana Lee", "task": "Reserve the server room for Friday", "said_at": 33.0},
            {"owner": "Dana Lee", "task": "Send Tom the cutover plan, with rollback"},
            {"owner": "You", "task": "Order a label printer", "said_at": 400.0},
        ],
    )
    after = uids(store, meeting)
    assert after["Reserve the server room for Friday"] == room
    assert after["Send Tom the cutover plan, with rollback"] == plan
    assert after["Order a label printer"] not in (plan, room)


def test_a_reworded_item_that_was_ticked_off_stays_done_and_isnt_made_again(store: Store):
    meeting = add(store)
    item = store.action_items(meeting_id=meeting)[0]
    store.set_action_status(item["id"], "done")
    store.replace_action_items(
        meeting, [{"owner": "Dana", "task": "Send Tom the cutover plan", "said_at": 21.0}]
    )
    (only,) = store.action_items(meeting_id=meeting)
    assert (only["uid"], only["status"]) == (item["uid"], "done")


def test_different_things_promised_at_the_same_moment_stay_apart(store: Store):
    meeting = add(store)
    plan, room = uids(store, meeting).values()
    store.replace_action_items(
        meeting,
        [
            {"owner": "Dana", "task": "Call the vendor about licences", "said_at": 20.0},
            {"owner": "Dana", "task": "Send the cutover plan to Tom", "said_at": 20.0},
        ],
    )
    after = uids(store, meeting)
    assert after["Send the cutover plan to Tom"] == plan
    assert after["Call the vendor about licences"] not in (plan, room)


def test_an_item_that_went_is_remembered_until_it_comes_back(on: Store):
    meeting = add(on)
    empty(on)
    room = uids(on, meeting)["Book the server room"]
    gone = "SELECT uid, owner, said_at FROM items_gone"
    on.replace_action_items(meeting, [{"owner": "Dana", "task": "Send the cutover plan to Tom"}])
    assert [tuple(r) for r in on.db.execute(gone)] == [(room, "Dana", 31.0)]
    on.replace_action_items(
        meeting, [{"owner": "Dana", "task": "Get a key for the room", "said_at": 31.0}]
    )
    assert uids(on, meeting)["Get a key for the room"] == room
    assert room not in [r["uid"] for r in on.db.execute(gone)]


def test_somebody_elses_item_at_that_moment_is_another_one(on: Store):
    meeting = add(on)
    empty(on)
    room = uids(on, meeting)["Book the server room"]
    on.replace_action_items(meeting, [])
    on.replace_action_items(meeting, [{"owner": "Tom", "task": "Get a key", "said_at": 31.0}])
    assert uids(on, meeting)["Get a key"] != room


def test_what_was_never_sent_leaves_no_trace_when_it_goes(on: Store):
    meeting = add(on)
    on.replace_action_items(meeting, [])
    assert not on.db.execute("SELECT 1 FROM items_gone").fetchone()


# Revisions --------------------------------------------------------------------------------


def change_voice(store: Store, meeting: int) -> None:
    store.set_speaker_person(store.speakers(meeting)[0]["id"], store.person_id("Dana"), "confirmed")


def rename(store: Store, meeting: int) -> None:
    store.rename_person(store.person_id("Dana"), "Dana Lee")


def tick(store: Store, meeting: int) -> None:
    store.set_action_status(store.action_items(meeting_id=meeting)[0]["id"], "done")


def edit_line(store: Store, meeting: int) -> None:
    store.edit_segment(store.transcript(meeting)[0]["id"], "Hello here")


CONTENT = {"overview": "Short.", "decisions": [], "action_items": []}
CHANGES = {
    "title": lambda s, m: s.update_meeting(m, title="Other"),
    "client": lambda s, m: s.update_meeting(m, client="Contoso", client_source="user"),
    "ticket": lambda s, m: s.update_meeting(m, ticket="INC-1"),
    "no ticket": lambda s, m: s.update_meeting(m, ticket=None),
    "a voice named": change_voice,
    "a person renamed": rename,
    "a person's client": lambda s, m: s.set_client(s.person_id("Dana"), "Contoso"),
    "a person removed": lambda s, m: s.delete_person(s.person_id("Dana")),
    "a summary": lambda s, m: s.save_summary(m, "x", CONTENT, 1, 1, 0.0),
    "a second one": lambda s, m: s.save_summary(m, "x", {**CONTENT, "overview": "New."}, 1, 1, 0),
    "a task ticked": tick,
    "new items": lambda s, m: s.replace_action_items(m, [{"owner": "You", "task": "Call Tom"}]),
    "a note": lambda s, m: s.add_note(m, 3.0, "note", "Old firewall"),
    "a bookmark": lambda s, m: s.add_note(m, 4.0, "bookmark"),
    "a line's words": edit_line,
    "processed again": lambda s, m: s.clear_results(m),
}
LINES = {
    "a voice named", "a person renamed", "a person's client", "a person removed",
    "a line's words", "processed again",
}  # fmt: skip


def test_whatever_changes_what_is_sent_raises_the_revision_and_waits(on: Store):
    meeting, other = add(on), add(on, "Other")
    on.update_meeting(meeting, ticket="INC-9")
    for name, change in CHANGES.items():
        if name in ("a person's client", "a person removed", "a person renamed"):
            change_voice(on, meeting)
        empty(on)
        before, others = rev(on, meeting), rev(on, other)
        change(on, meeting)
        assert rev(on, meeting) > before, name
        expected = ["send", "transcript"] if name in LINES else ["send"]
        assert waiting(on, uid(on, meeting)) == expected, name
        assert (rev(on, other), waiting(on, uid(on, other))) == (others, []), name


# Each on its own, as no path through the store alone does it: {m} is the meeting.
ROWS = {
    "voice in": "INSERT INTO meeting_speakers (meeting_id, track, label, status) "
    "VALUES ({m}, 'mic', 'You', 'confirmed')",
    "voice out": "DELETE FROM meeting_speakers WHERE meeting_id = {m} AND label = 'You'",
    "line in": "INSERT INTO segments (meeting_id, track, start, end, text) "
    "VALUES ({m}, 'mic', 90, 91, 'Yes')",
    "line out": "DELETE FROM segments WHERE meeting_id = {m} AND start = 90",
    "summary in": "INSERT INTO summaries (meeting_id, model, content) VALUES ({m}, 'x', '1')",
    "summary changed": "UPDATE summaries SET content = '2' WHERE meeting_id = {m}",
    "summary out": "DELETE FROM summaries WHERE meeting_id = {m}",
    "item in": "INSERT INTO action_items (meeting_id, owner, task) VALUES ({m}, 'You', 'Call')",
    "item out": "DELETE FROM action_items WHERE meeting_id = {m} AND task = 'Call'",
    "note in": "INSERT INTO meeting_notes (meeting_id, at, text) VALUES ({m}, 5, 'Plan')",
    "note changed": "UPDATE meeting_notes SET text = 'Other' WHERE meeting_id = {m}",
    "note out": "DELETE FROM meeting_notes WHERE meeting_id = {m}",
}


def test_every_row_that_comes_changes_or_goes_counts(on: Store):
    meeting = add(on)
    for name, sql in ROWS.items():
        empty(on)
        before = rev(on, meeting)
        on.db.execute(sql.format(m=meeting))
        on.db.commit()
        assert rev(on, meeting) == before + 1, name
        lines = ["send", "transcript"] if name.startswith(("voice", "line")) else ["send"]
        assert waiting(on) == lines, name


def test_what_isnt_sent_changes_nothing(on: Store):
    meeting = add(on)
    change_voice(on, meeting)
    empty(on)
    before = rev(on, meeting)
    on.add_note(meeting, 3.0, "snapshot", "Error 42")
    on.update_meeting(meeting, title="Sync")  # as it is
    on.update_meeting(meeting, audio_removed_at="2026-11-25", template="general")
    on.add_voice_sample(on.person_id("Dana"), meeting, np.ones(4), "demo", 3.0)
    on.set_speaker_sample(on.speakers(meeting)[0]["id"], (5.0, 9.0))
    on.refresh_status(meeting)
    assert (rev(on, meeting), waiting(on)) == (before, [])


def test_changes_made_together_are_queued_together_or_not_at_all(on: Store):
    meeting = add(on)
    empty(on)
    before = rev(on, meeting)
    with pytest.raises(RuntimeError), on.transaction():
        on.update_meeting(meeting, title="Other")
        on.save_summary(meeting, "x", CONTENT, 1, 1, 0.0)
        raise RuntimeError
    assert (rev(on, meeting), waiting(on)) == (before, [])


# The outbox -------------------------------------------------------------------------------


def test_off_nothing_waits_and_revisions_still_go_up(store: Store):
    meeting = add(store)
    before = rev(store, meeting)
    store.update_meeting(meeting, title="Other")
    assert waiting(store) == [] and rev(store, meeting) == before + 1


def test_turned_off_changes_to_what_was_sent_stay_home(on: Store):
    meeting = add(on)
    empty(on)
    on.set_setting(outbox.ON, "0")
    edit_line(on, meeting)
    assert waiting(on) == []


def test_meetings_from_before_it_was_turned_on_stay_home(store: Store):
    meeting = add(store)
    store.set_setting(outbox.ON, "1")
    store.update_meeting(meeting, title="Other")
    assert waiting(store) == []
    add(store, "New")
    assert waiting(store) == ["send"]


def test_transcripts_wait_only_when_they_are_sent(store: Store):
    store.set_setting(outbox.ON, "1")
    meeting = add(store)
    edit_line(store, meeting)
    assert waiting(store) == ["send"]
    store.set_setting(outbox.TRANSCRIPTS, "1")
    change_voice(store, meeting)
    assert waiting(store) == ["send", "transcript"]


def test_a_newer_change_takes_the_place_of_one_that_waits(on: Store):
    meeting = add(on)
    on.db.execute("UPDATE outbox SET quiet_until = 5, retry_at = 99, tries = 4, reason = 'down'")
    on.db.execute("UPDATE outbox SET state = 'sending' WHERE what = 'send'")
    on.db.commit()
    for title in ("A", "B", "C"):
        on.update_meeting(meeting, title=title)
    row = on.db.execute("SELECT * FROM outbox WHERE what = 'send'").fetchone()
    assert waiting(on) == ["send", "transcript"]
    # Quiet again for a while, and no sooner than the server said.
    assert row["state"] == "waiting" and row["quiet_until"] > 5
    assert (row["retry_at"], row["tries"], row["reason"]) == (99, 4, "down")


def test_one_the_server_refused_is_tried_again_once_it_changed(on: Store):
    meeting = add(on)
    on.db.execute("UPDATE outbox SET state = 'stopped', tries = 3, reason = 'not a message'")
    on.db.commit()
    on.update_meeting(meeting, title="Other")
    row = on.db.execute("SELECT * FROM outbox WHERE what = 'send'").fetchone()
    assert (row["state"], row["tries"], row["reason"]) == ("waiting", 0, None)


def test_taken_back_it_stays_back(on: Store):
    meeting = add(on)
    empty(on)
    on.db.execute("UPDATE sends SET taken = 1")
    on.db.commit()
    on.update_meeting(meeting, title="Other")
    assert waiting(on) == []


# Private, deleted, redacted ----------------------------------------------------------------


def test_a_private_meeting_never_waits(on: Store):
    meeting = on.create_meeting("Call", "2026-11-25T09:00:00", Path("x"), status="recording")
    on.update_meeting(meeting, private=1)
    on.update_meeting(meeting, title="Other", status="ready")
    on.save_summary(meeting, "x", CONTENT, 1, 1, 0.0)
    assert waiting(on) == []


def test_made_private_while_it_waits_it_never_goes(on: Store):
    meeting = add(on)
    assert waiting(on) == ["send", "transcript"]
    on.update_meeting(meeting, private=1)
    assert waiting(on) == []


def test_made_private_after_it_was_sent_it_is_taken_back_once(on: Store):
    meeting = add(on)
    empty(on)
    on.update_meeting(meeting, title="Other")
    before = rev(on, meeting)
    on.update_meeting(meeting, private=1)
    on.update_meeting(meeting, private=1, title="Third")
    assert waiting(on) == ["recall"] and rev(on, meeting) > before
    on.update_meeting(meeting, private=0)  # and public again: the recall still goes first
    assert waiting(on) == ["recall", "send", "transcript"]
    on.update_meeting(meeting, private=1)
    assert waiting(on) == ["recall"]


def test_a_deleted_meeting_leaves_its_uid_behind_with_a_recall(on: Store):
    sent, never = add(on), add(on, "Never sent")
    empty(on)
    known = uid(on, sent)
    on.db.execute("UPDATE sends SET target = NULL WHERE uid = ?", (uid(on, never),))
    on.db.commit()
    on.update_meeting(never, title="Other")
    on.replace_action_items(sent, [])
    on.delete_meeting(sent)
    on.delete_meeting(never)
    rows = on.db.execute("SELECT uid, what FROM outbox").fetchall()
    assert [tuple(row) for row in rows] == [(known, "recall")]
    assert [row["uid"] for row in on.db.execute("SELECT uid FROM sends")] == [known]
    assert not on.db.execute("SELECT 1 FROM items_gone").fetchone()


def test_turned_off_what_was_sent_is_still_taken_back(on: Store):
    private, deleted = add(on), add(on, "Other")
    empty(on)
    on.set_setting(outbox.ON, "0")
    on.update_meeting(private, title="Third")
    on.update_meeting(private, private=1)
    on.delete_meeting(deleted)
    assert waiting(on) == ["recall", "recall"]


def test_a_recall_survives_whatever_happens_then(on: Store):
    meeting = add(on)
    empty(on)
    on.update_meeting(meeting, private=1)
    on.db.execute("UPDATE outbox SET tries = 7, retry_at = 99, reason = 'down'")
    on.db.commit()
    on.delete_meeting(meeting)
    row = on.db.execute("SELECT * FROM outbox").fetchone()
    assert (row["what"], row["tries"], row["retry_at"]) == ("recall", 7, 99)


def test_a_redaction_is_queued_from_where_its_first_line_begins(on: Store, tmp_path):
    meeting = add(on, folder=tmp_path)
    empty(on)
    plan, room = uids(on, meeting).values()
    before = rev(on, meeting)
    redact.redact(on, meeting, 22.0, 23.0, with_audio=False)
    redact.redact(on, meeting, 30.0, 36.0, with_audio=False)
    rows = on.db.execute("SELECT * FROM outbox WHERE what = 'redact' ORDER BY id").fetchall()
    # The line at 20 s reaches into the first one; both redactions stay.
    assert [(row["from_sec"], row["to_sec"]) for row in rows] == [(20.0, 23.0), (30.0, 36.0)]
    assert waiting(on)[-2:] == ["send", "transcript"] and rev(on, meeting) > before
    # Both items went with it. The one said outside keeps its uid in the next summary.
    on.replace_action_items(
        meeting,
        [
            {"owner": "Dana", "task": "Do the thing", "said_at": 20.0},
            {"owner": "Dana", "task": "Do another thing", "said_at": 31.0},
        ],
    )
    after = uids(on, meeting)
    assert after["Do the thing"] == plan and after["Do another thing"] != room


def test_a_redaction_of_what_was_never_sent_is_nobodys_business(on: Store, tmp_path):
    meeting = add(on, folder=tmp_path)
    redact.redact(on, meeting, 22.0, 23.0, with_audio=False)
    assert waiting(on) == ["send", "transcript"]


def test_a_redaction_cut_off_changes_nothing(on: Store, tmp_path, monkeypatch):
    meeting = add(on, folder=tmp_path)
    empty(on)
    words = [line["text"] for line in on.transcript(meeting)]

    def fail(*args):
        raise RuntimeError("power cut")

    monkeypatch.setattr(on, "forget_derived", fail)
    with pytest.raises(RuntimeError):
        redact.redact(on, meeting, 22.0, 23.0, with_audio=False)
    assert waiting(on) == [] and [line["text"] for line in on.transcript(meeting)] == words


# A library from before ---------------------------------------------------------------------


@pytest.fixture
def old(tmp_path: Path) -> Path:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(OLD.read_text(encoding="utf-8"))
    db.close()
    return path


def rows(path: Path, skip: tuple[str, ...] = ()) -> dict[str, list]:
    db = sqlite3.connect(path)
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    found = {}
    for name in names:
        if name.startswith(("segments_fts", "sqlite_", *skip)):
            continue
        columns = [r[1] for r in db.execute(f"PRAGMA table_info({name})") if r[1] not in ADDED]
        found[name] = db.execute(f"SELECT {', '.join(columns)} FROM {name}").fetchall()
    db.close()
    return found


NEW = ("sends", "outbox", "items_gone", "changes")  # ("changes" only counts)
ADDED = ("uid", "voice_id")  # columns added to tables a library had


def test_a_library_made_by_0_5_0_keeps_everything_and_sends_nothing(old: Path):
    before = rows(old, NEW)
    store = Store(old)
    meetings = store.meetings()
    assert all(UUID.fullmatch(m["uid"]) for m in meetings) and len(meetings) == 3
    assert len({i["uid"] for i in store.action_items()}) == 6
    assert len({p["uid"] for p in store.people()}) == 2
    store.set_setting(outbox.ON, "1")
    store.update_meeting(1, title="Other")
    assert waiting(store) == [] and rev(store, 1) == 2
    store.update_meeting(1, title="Contoso cutover sync")
    store.delete_setting(outbox.ON)
    assert [hit["title"] for hit in store.search("plan")]  # the search index is as it was
    known = [m["uid"] for m in store.meetings()]
    store.close()
    again = Store(old)
    assert [m["uid"] for m in again.meetings()] == known
    again.close()
    assert rows(old, NEW) == before


def test_0_5_0_can_still_write_to_it(old: Path):
    Store(old).close()
    db = sqlite3.connect(old)  # as 0.5.0 writes: it knows no uid, and no outbox
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("INSERT INTO settings VALUES (?, '1')", (outbox.ON,))
    db.execute(
        "INSERT INTO meetings (id, title, started_at, folder) VALUES (9, 'New', '2026-11-25', 'x')"
    )
    db.execute("INSERT INTO people (name) VALUES ('Elena')")
    db.execute("INSERT INTO action_items (meeting_id, owner, task) VALUES (9, 'You', 'Call')")
    db.execute("INSERT OR REPLACE INTO summaries (meeting_id, model, content) VALUES (9, 'x', '1')")
    db.execute("INSERT OR REPLACE INTO summaries (meeting_id, model, content) VALUES (9, 'x', '2')")
    db.commit()
    for table, where in (("meetings", "id = 9"), ("people", "name = 'Elena'"),
                         ("action_items", "meeting_id = 9")):  # fmt: skip
        assert UUID.fullmatch(db.execute(f"SELECT uid FROM {table} WHERE {where}").fetchone()[0])
    assert db.execute("SELECT what FROM outbox").fetchall() == [("send",)]
    assert db.execute("SELECT rev FROM sends ORDER BY rev DESC").fetchone()[0] == 4
    db.execute("UPDATE sends SET target = 'somewhere'")
    db.execute("DELETE FROM meetings WHERE id = 9")
    assert db.execute("SELECT what FROM outbox").fetchall() == [("recall",)]
    db.close()


def test_cut_off_the_migration_leaves_the_library_as_it_was(old: Path, monkeypatch):
    before = rows(old, NEW)
    monkeypatch.setattr(outbox, "TRIGGERS", outbox.TRIGGERS + "CREATE NONSENSE;\n")
    with pytest.raises(sqlite3.OperationalError):
        Store(old)
    db = sqlite3.connect(old)
    assert not outbox._has_uid(db)
    db.close()
    assert rows(old, NEW) == before
    monkeypatch.undo()
    Store(old).close()
    assert rows(old, NEW) == before
