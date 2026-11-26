"""Sending to ZachCore (sender.py) against a small server of the tests' own that answers as
ZachCore does (zachcore_fake.py): the cases ZachCore's docs/zachcap.md says matter, each here,
and what has to hold around them."""

import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from test_zachcore_message import call, summarized
from zachcore_fake import Fake

from zachcap import apikeys, outbox, redact, sender, zachcore
from zachcap.store import Store

AGAIN = [
    {"owner": "You", "task": "Send the rollback steps to Dana", "said_at": 452.0},
    {"owner": "Speaker 2", "task": "Check the rules on the firewall", "said_at": 300.0},
    {"owner": "Dana Lee", "task": "Book the server room", "said_at": 620.0},
]


class Clock:
    """The sender's time: now, and moved on by the tests."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def on(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake():
    fake = Fake()
    yield fake
    fake.close()


@pytest.fixture
def keys(fake: Fake, monkeypatch) -> dict:
    """Credential Manager, as the sender sees it: a key per address."""
    keys = {fake.address: fake.key}
    monkeypatch.setattr(apikeys, "zachcore", lambda address: keys.get(address))
    return keys


@pytest.fixture
def store(tmp_path: Path, fake: Fake, keys: dict):
    store = Store(tmp_path / "test.db")
    store.set_setting(outbox.ON, "1")
    store.set_setting(sender.ADDRESS, fake.address)
    yield store
    store.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def send(store: Store, clock: Clock) -> sender.Sender:
    return sender.Sender(Path(store.path), clock=clock)


def meeting(store: Store, clock: Clock, uid: str = "meeting-1") -> int:
    """The call of the message's tests, summarized and recorded today. The meetings it needs
    beside it (a private one, one Tom was named in) aren't sent."""
    m = call(store, uid)
    summarized(store, m)
    today = datetime.fromtimestamp(clock.now).astimezone().isoformat(timespec="seconds")
    store.update_meeting(m, started_at=today)
    store.db.execute("DELETE FROM outbox WHERE uid NOT LIKE 'meeting-%'")
    store.db.execute("UPDATE sends SET wanted = 0 WHERE uid NOT LIKE 'meeting-%'")
    store.db.commit()
    clock.now = max(clock.now, time.time())  # however long making it took
    return m


def ready(store: Store, m: int) -> None:
    """Every voice named: its transcript may go."""
    store.db.execute("UPDATE meetings SET status = 'ready' WHERE id = ?", (m,))
    store.db.commit()


def due(send: sender.Sender) -> float:
    """When the last row waiting in the outbox is due: the half minute after a change runs
    on the library's clock, which went on while a test worked (more of it on a slow PC)."""
    store = Store(send.path)
    try:
        found = store.db.execute(
            "SELECT MAX(MAX(quiet_until, retry_at)) FROM outbox WHERE state <> 'stopped'"
        ).fetchone()[0]
    finally:
        store.close()
    return float(found or 0)


def drain(send: sender.Sender, clock: Clock, steps: int = 40) -> int:
    """Lets it send until nothing waits that can go: past the half minute without changes,
    and past each wait after no answer. How many steps did something."""
    went = 0
    for _ in range(steps):
        if send.step():
            went += 1
            continue
        clock.now = max(clock.now + 31, send.held_until, due(send) + 1)
        if not send.step():
            return went
        went += 1
    raise AssertionError("it never stopped")


def waiting(store: Store) -> list[str]:
    return [row["what"] for row in store.db.execute("SELECT what FROM outbox ORDER BY id")]


def sent(fake: Fake, path: str = "in") -> list[dict]:
    return [body for where, body in fake.requests if where == path]


def paths(fake: Fake) -> list[str]:
    return [where for where, body in fake.requests if body is not None]


def body(fake: Fake, uid: str = "meeting-1") -> dict:
    return fake.messages[uid]["body"]


# The cases ZachCore's zachcap.md lists ---------------------------------------------------


def test_an_answer_that_is_lost_is_sent_again_and_kept_once(store, fake, send, clock):
    meeting(store, clock)
    fake.lose = 1
    drain(send, clock)
    first, again = sent(fake)
    assert first == again  # the same request, the same revision
    assert fake.stored == [("meeting-1", first["rev"])] and fake.answers == [0, 200]
    assert waiting(store) == []


@pytest.mark.parametrize("trouble", ["down", 500])
def test_a_server_down_or_a_pc_asleep_waits_then_sends_the_newest_once(
    store, fake, send, clock, trouble
):
    m = meeting(store, clock)
    if trouble == "down":
        fake.down = True
    else:
        fake.answer = [500] * 3
    clock.on(31)
    assert send.step() and not send.step()  # tried, and now it waits
    for title in ("Cutover sync", "Cutover sync, Friday"):
        store.update_meeting(m, title=title)
    clock.on(86_400)  # a day asleep
    fake.down = False
    drain(send, clock)
    assert [uid for uid, _ in fake.stored] == ["meeting-1"]  # once
    assert body(fake)["title"] == "Cutover sync, Friday" and waiting(store) == []


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_key_or_space_refused_stops_and_says_so_and_loses_nothing(
    store, fake, send, clock, status
):
    meeting(store, clock)
    if status == 404:
        store.set_setting(sender.SPACE, "nowhere")
    else:
        fake.answer = [status]
    drain(send, clock)
    assert sender.stopped(store) == {"place": sender.place(store).target, "status": status}
    asked = len(fake.requests)
    clock.on(7 * 86_400)
    assert drain(send, clock) == 0 and len(fake.requests) == asked  # nothing more
    assert waiting(store) == ["send"]  # and nothing lost
    reason = store.db.execute("SELECT reason FROM outbox").fetchone()[0]
    assert str(status) in reason
    store.delete_setting(sender.STOPPED)  # the key or the space was put right
    store.set_setting(sender.SPACE, "work")
    drain(send, clock)
    assert waiting(store) == [] and len(fake.stored) == 1


def test_409_for_a_message_counts_as_done(store, fake, send, clock, monkeypatch):
    meeting(store, clock)
    # The server says it has a newer one, of the revisions this library gave out.
    monkeypatch.setattr(sender.Sender, "_state", lambda self, *_: 1)
    fake.answer = [409]
    drain(send, clock)
    assert fake.stored == [] and len(fake.requests) == 1 and waiting(store) == []


def test_409_above_any_revision_it_gave_goes_again_above_it(store, fake, send, clock):
    """A library restored from a backup, or a clock put back."""
    meeting(store, clock)
    above = int(clock.now * 1000) + 10**9
    fake.messages["meeting-1"] = {"rev": above, "result": "stored", "body": {}}
    drain(send, clock)
    assert fake.answers.count(409) == 1
    ((uid, rev),) = fake.stored
    assert rev > above and waiting(store) == []


def test_409_for_a_transcript_reads_the_lines_again_with_the_newer_revision(
    store, fake, send, clock
):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    ready(store, m)
    drain(send, clock)
    # Taken back there since, under a revision above this library's (it was restored).
    above = int(clock.now * 1000) + 10**9
    fake.taken["meeting-1"] = above
    fake.messages["meeting-1"]["rev"] = above
    store.edit_segment(store.transcript(m)[0]["id"], "Other words")
    drain(send, clock)
    first, *_, last = sent(fake, "in/transcript")
    assert fake.answers.count(409) == 1 and last["rev"] >= above > first["rev"]
    assert fake.lines["meeting-1"][0]["text"] == "Other words" and waiting(store) == []


def test_a_transcript_of_a_meeting_made_private_goes_no_more(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    ready(store, m)
    drain(send, clock)
    store.edit_segment(store.transcript(m)[0]["id"], "Other words")
    store.update_meeting(m, private=1)
    drain(send, clock)
    assert paths(fake)[-1] == "in/recall" and len(sent(fake, "in/transcript")) == 1
    assert fake.lines["meeting-1"] == {}


def test_paused_in_zachcore_it_waits_and_says_so_but_taking_back_goes(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    fake.paused = True
    store.update_meeting(m, title="Changed while paused")
    drain(send, clock)
    assert fake.answers[-1] == 423 and float(store.setting(sender.PAUSED)) > clock.now
    assert [row["reason"] for row in store.db.execute("SELECT reason FROM outbox")] == [
        "Paused in ZachCore"
    ]
    asked = len(fake.requests)
    assert drain(send, clock) == 0 and len(fake.requests) == asked  # it waits
    store.update_meeting(m, private=1)
    drain(send, clock)
    assert paths(fake)[-1] == "in/recall" and fake.messages["meeting-1"]["result"] == "private"
    fake.paused = False
    store.update_meeting(m, private=0)
    clock.on(sender.PAUSED_WAIT)
    drain(send, clock)
    assert body(fake)["title"] == "Changed while paused"


@pytest.mark.parametrize(("retry_after", "waits"), [(7200, 7200), (10 * 86_400, 86_400)])
def test_out_of_writing_it_waits_retry_after_at_most_a_day(
    store, fake, send, clock, retry_after, waits
):
    meeting(store, clock)
    fake.full_for = retry_after
    clock.on(31)
    assert send.step()
    assert float(store.setting(sender.FULL)) == pytest.approx(clock.now + waits, abs=1)
    asked = len(fake.requests)
    clock.on(waits - 60)
    assert not send.step() and len(fake.requests) == asked
    fake.full_for = None
    clock.on(61)
    drain(send, clock)
    assert len(fake.stored) == 1 and waiting(store) == []


def test_a_new_summary_in_other_words_makes_no_second_task(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    before = set(fake.tasks["meeting-1"])
    store.replace_action_items(m, AGAIN)
    drain(send, clock)
    tasks = fake.tasks["meeting-1"]
    assert set(tasks) == before  # the same refs, none new
    assert tasks["task-1"]["title"] == "Send the rollback steps to Dana"


def test_an_item_a_new_summary_leaves_out_goes(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.replace_action_items(m, AGAIN)
    drain(send, clock)
    gone = {task for task, said in fake.tasks["meeting-1"].items() if said.get("gone")}
    assert set(sent(fake)[-1]["gone"]) == gone and "task-7" in gone and "task-1" not in gone


def test_a_voice_named_after_the_first_message_is_one_new_message(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    first = body(fake)
    voice = next(v["id"] for v in store.speakers(m) if v["label"] == "Speaker 2")
    store.set_speaker_person(voice, store.person_id("Erin Moss"), "confirmed")
    drain(send, clock)
    assert len(fake.stored) == 2
    now = body(fake)
    erin = next(p["ref"] for p in now["people"] if p["name"] == "Erin Moss")
    owners = {task["ref"]: task["owner"] for task in now["tasks"]}
    assert [t["ref"] for t in now["tasks"]] == [t["ref"] for t in first["tasks"]]
    assert owners["task-3"] == erin and "Speaker 2" not in [p["name"] for p in now["people"]]


def test_a_client_or_ticket_taken_off_comes_off_with_the_next_message(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    assert body(fake)["ticket"] == "T-20931" and body(fake)["client"] == "Contoso"
    store.update_meeting(m, client=None, client_source="user", ticket=None)
    drain(send, clock)
    assert body(fake)["client"] is None and body(fake)["ticket"] is None
    assert sent(fake)[-1]["client"] is None  # sent as null: left out, it would stay there


def test_a_switch_turned_off_takes_nothing_out_of_zachcore(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    decisions = body(fake)["decisions"]
    store.set_setting(zachcore.setting("summaries"), "0")
    store.update_meeting(m, title="Sent without its summary")
    drain(send, clock)
    assert "decisions" not in sent(fake)[-1] and "overview" not in sent(fake)[-1]
    assert body(fake)["decisions"] == decisions  # still there


def test_made_private_while_its_message_waits_it_never_goes(store, fake, send, clock):
    m = meeting(store, clock)
    store.update_meeting(m, private=1)
    drain(send, clock)
    assert fake.requests == []  # never sent: nothing to take back either


def test_made_private_while_a_change_waits_only_the_recall_goes(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.update_meeting(m, title="Waits")
    store.update_meeting(m, private=1)
    drain(send, clock)
    assert paths(fake) == ["in", "in/recall"]
    assert fake.messages["meeting-1"]["result"] == "private"
    assert (
        store.db.execute("SELECT target FROM sends WHERE uid = 'meeting-1'").fetchone()[0] is None
    )


def test_a_redaction_while_a_message_is_on_its_way(store, fake, send, clock):
    """The message's answer doesn't come, and it reaches the server only after the rest."""
    m = meeting(store, clock)
    drain(send, clock)
    store.update_meeting(m, title="Cutover, rescheduled")
    fake.keep_late = 1
    clock.on(31)
    assert send.step()  # on its way
    late = fake.late[0][1]
    redact.redact(store, m, 250.0, 300.0, with_audio=False)
    drain(send, clock)
    redacted, clean = sent(fake, "in/redact")[-1], sent(fake)[-1]
    assert late["rev"] < redacted["rev"] < clean["rev"]
    assert fake.late_ones() == [409]  # the old one, arriving after: refused
    assert fake.messages["meeting-1"]["rev"] == clean["rev"]
    assert body(fake)["title"] == "Cutover, rescheduled" and "overview" in body(fake)


def test_sending_off_a_meeting_made_private_is_taken_back_and_nothing_else(
    store, fake, send, clock
):
    m = meeting(store, clock)
    drain(send, clock)
    store.set_setting(outbox.ON, "0")
    store.update_meeting(m, title="Not sent")
    store.update_meeting(m, private=1)
    drain(send, clock)
    assert paths(fake) == ["in", "in/recall"]


# Around them ---------------------------------------------------------------------------


def test_a_meeting_made_private_right_before_its_request_isnt_sent(
    store, fake, send, clock, monkeypatch
):
    m = meeting(store, clock)
    real = sender._ready

    def then_private(*args):
        rev = real(*args)
        other = Store(Path(store.path))
        other.update_meeting(m, private=1)
        other.close()
        return rev

    monkeypatch.setattr(sender, "_ready", then_private)
    drain(send, clock)
    assert paths(fake) == ["in/recall"]  # it might have been there: taken back


def test_nothing_goes_while_it_is_recorded_or_processed(store, fake, send, clock):
    m = meeting(store, clock)
    for status in sender.BUSY:
        store.update_meeting(m, status=status)
        assert drain(send, clock) == 0
    store.refresh_status(m)
    drain(send, clock)
    assert len(fake.stored) == 1


def test_all_four_switches_off_sends_nothing(store, fake, send, clock):
    meeting(store, clock)
    for part in zachcore.PARTS:
        store.set_setting(zachcore.setting(part), "0")
    assert drain(send, clock) == 0 and fake.requests == []
    store.set_setting(outbox.TRANSCRIPTS, "1")
    drain(send, clock)
    assert set(body(fake)) == {
        "ref", "rev", "title", "started", "seconds", "app", "client", "ticket", "share", "people"
    }  # fmt: skip


def test_a_transcript_goes_in_parts_once_the_voices_have_names(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    you = store.speakers(m)[0]["id"]
    store.add_segments(
        m, [(you, "mic", 1000.0 + i, 1000.5 + i, f"Line {i}", 0.9) for i in range(4500)]
    )
    store.refresh_status(m)
    assert store.meeting(m)["status"] == "needs_names"
    drain(send, clock)
    assert sent(fake, "in/transcript") == []  # not without the names
    for voice in store.speakers(m):
        if voice["status"] in ("unknown", "suggested"):
            name = store.person_id(f"Person {voice['id']}")
            store.set_speaker_person(voice["id"], name, "confirmed")
    store.refresh_status(m)
    drain(send, clock)
    parts = sent(fake, "in/transcript")
    assert [(p["part"], p["parts"]) for p in parts] == [(0, 3), (1, 3), (2, 3)]
    assert len(fake.lines["meeting-1"]) == len(store.transcript(m)) == 4510
    assert fake.lines["meeting-1"][4509]["text"] == "Line 4499"
    store.update_meeting(m, title="Only the title changed")
    drain(send, clock)  # the same lines: not again
    assert len(sent(fake, "in/transcript")) == 3


def test_a_transcript_without_names_goes_after_a_day(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    meeting(store, clock)
    drain(send, clock)
    assert sent(fake, "in/transcript") == []
    clock.on(sender.NAMES)
    drain(send, clock)
    assert len(sent(fake, "in/transcript")) == 1


def test_after_a_redaction_the_other_lines_arent_sent_again(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    ready(store, m)
    drain(send, clock)
    written = fake.written
    redact.redact(store, m, 250.0, 300.0, with_audio=False)
    drain(send, clock)
    assert len(sent(fake, "in/transcript")) == 1 and fake.written == written
    assert all(not 250 <= line["sec"] <= 300 for line in fake.lines["meeting-1"].values())
    store.edit_segment(store.transcript(m)[0]["id"], "Changed afterwards")
    drain(send, clock)
    assert len(sent(fake, "in/transcript")) == 2


def test_after_a_redaction_lines_not_there_yet_are_sent(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    drain(send, clock)  # the message went; its lines wait for the names
    redact.redact(store, m, 250.0, 300.0, with_audio=False)
    ready(store, m)
    drain(send, clock)
    assert len(fake.lines["meeting-1"]) == len(store.transcript(m))


def test_a_deleted_meeting_is_taken_back_and_then_forgotten(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.delete_meeting(m)
    drain(send, clock)
    assert fake.messages["meeting-1"]["result"] == "private"
    assert store.db.execute("SELECT 1 FROM sends WHERE uid = 'meeting-1'").fetchone() is None


def test_taking_back_goes_where_it_was_sent(store, fake, send, clock, keys):
    m = meeting(store, clock)
    drain(send, clock)
    other = Fake()
    keys[other.address] = other.key
    try:
        store.set_setting(sender.ADDRESS, other.address)
        store.update_meeting(m, private=1)
        drain(send, clock)
        assert paths(fake)[-1] == "in/recall" and other.requests == []
    finally:
        other.close()


def test_a_meeting_sent_to_another_server_isnt_sent_to_this_one(store, fake, send, clock, keys):
    m = meeting(store, clock)
    drain(send, clock)
    other = Fake()
    keys[other.address] = other.key
    try:
        store.set_setting(sender.ADDRESS, other.address)
        store.update_meeting(m, title="Changed")
        meeting(store, clock, "meeting-2")
        drain(send, clock)
        assert [uid for uid, _ in other.stored] == ["meeting-2"]
        assert waiting(store) == ["send"]
    finally:
        other.close()


def test_without_a_key_nothing_goes_and_nothing_is_lost(store, fake, send, clock, keys):
    meeting(store, clock)
    keys.clear()
    assert drain(send, clock) == 0 and waiting(store) == ["send"]


def test_the_key_is_nowhere_in_the_library(store, fake, send, clock):
    m = meeting(store, clock)
    fake.answer = [401]
    drain(send, clock)
    store.delete_setting(sender.STOPPED)
    drain(send, clock)
    store.update_meeting(m, private=1)
    drain(send, clock)
    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for file in Path(store.path).parent.iterdir():
        assert fake.key.encode() not in file.read_bytes(), file.name


def test_revisions_only_go_up(store, fake, send, clock):
    m = meeting(store, clock)
    for title in ("One", "Two", "Three"):
        store.update_meeting(m, title=title)
        drain(send, clock)
    store.update_meeting(m, private=1)
    drain(send, clock)
    revs = [said["rev"] for _, said in fake.requests if said]
    assert revs == sorted(revs) and len(set(revs)) == len(revs) == 4


def test_the_same_message_again_isnt_sent(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.edit_segment(store.transcript(m)[0]["id"], "Other words")  # not in the message
    drain(send, clock)
    assert len(fake.requests) == 1 and waiting(store) == []


def test_a_message_refused_waits_until_it_changes(store, fake, send, clock):
    m = meeting(store, clock)
    fake.answer = [400]
    drain(send, clock)
    assert [tuple(row) for row in store.db.execute("SELECT state, reason FROM outbox")] == [
        ("stopped", "ZachCore refused it (400)")
    ]
    clock.on(86_400)
    assert drain(send, clock) == 0
    store.update_meeting(m, title="Changed")
    drain(send, clock)
    assert len(fake.stored) == 1 and waiting(store) == []


def test_a_server_that_kept_nothing_has_nothing_taken_back(store, fake, send, clock):
    m = meeting(store, clock)
    fake.answer = [401]
    drain(send, clock)
    assert (
        store.db.execute("SELECT target FROM sends WHERE uid = 'meeting-1'").fetchone()[0] is None
    )
    store.update_meeting(m, private=1)
    store.delete_setting(sender.STOPPED)
    drain(send, clock)
    assert paths(fake) == ["in"]  # the one refused, and no recall


def test_turned_off_what_waits_doesnt_go(store, fake, send, clock):
    meeting(store, clock)
    store.set_setting(outbox.ON, "0")
    assert drain(send, clock) == 0 and waiting(store) == ["send"]
    store.set_setting(outbox.ON, "1")
    drain(send, clock)
    assert len(fake.stored) == 1


def test_nothing_of_a_meeting_goes_while_taking_some_of_it_back_waits(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    fake.answer = [500]
    redact.redact(store, m, 250.0, 300.0, with_audio=False)
    clock.on(31)
    assert send.step()  # the redaction, unanswered
    send.held_until = 0  # another meeting's request may go; this one's message may not
    assert not send.step() and paths(fake)[-1] == "in/redact"
    drain(send, clock)
    assert paths(fake)[-2:] == ["in/redact", "in"]


def test_after_no_answer_nothing_goes_for_a_while(store, fake, send, clock):
    meeting(store, clock)
    meeting(store, clock, "meeting-2")
    fake.down = True
    clock.on(31)
    assert send.step()
    asked = len(fake.requests)
    assert not send.step()  # the other meeting waits too
    clock.on(sender.FIRST_WAIT)
    fake.down = False
    drain(send, clock)
    assert asked == 0 and len(fake.stored) == 2


def test_a_recall_whose_answer_was_lost_is_done(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.update_meeting(m, private=1)
    fake.lose = 1
    drain(send, clock)
    assert paths(fake) == ["in", "in/recall", "in/recall"] and waiting(store) == []
    assert fake.answers[-2:] == [409, 200]  # the same again: there already, so done


def test_after_a_redaction_the_message_goes_again_even_the_same(store, fake, send, clock):
    """ZachCore took the title out until the next message: that comes, though it is as it was."""
    for part in zachcore.PARTS:
        store.set_setting(zachcore.setting(part), "0")
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    drain(send, clock)
    redact.redact(store, m, 860.0, 870.0, with_audio=False)  # a voice that isn't listed
    drain(send, clock)
    assert paths(fake)[-2:] == ["in/redact", "in"] and body(fake)["title"] != "(redacted)"


def test_a_change_while_its_message_is_made_waits_for_it(store, fake, send, clock, monkeypatch):
    m = meeting(store, clock)
    made = zachcore.message

    def and_meanwhile(*args):
        found = made(*args)
        other = Store(Path(store.path))
        other.update_meeting(m, title="Changed meanwhile")
        other.close()
        return found

    monkeypatch.setattr(zachcore, "message", and_meanwhile)
    clock.on(31)
    send.step()
    monkeypatch.setattr(zachcore, "message", made)
    assert fake.requests == []  # what it made is older than the library: not sent
    drain(send, clock)
    assert len(fake.stored) == 1 and body(fake)["title"] == "Changed meanwhile"


def test_a_change_while_its_message_is_on_its_way_goes_after_it(store, fake, send, clock):
    m = meeting(store, clock)
    real = send.send

    def and_meanwhile(*args):
        other = Store(Path(store.path))
        other.update_meeting(m, title="Changed meanwhile")
        other.close()
        send.send = real
        return real(*args)

    send.send = and_meanwhile
    drain(send, clock)
    assert len(fake.stored) == 2 and body(fake)["title"] == "Changed meanwhile"


def test_a_redaction_takes_out_lines_to_the_hundredth(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    you = store.speakers(m)[0]["id"]
    store.add_segments(m, [(you, "mic", 950.004, 951.996, "A code: 4 4 1 7", 0.9)])
    ready(store, m)
    drain(send, clock)
    redact.redact(store, m, 950.004, 951.996, with_audio=False)
    drain(send, clock)
    assert "A code: 4 4 1 7" not in json.dumps(fake.lines["meeting-1"])


def test_a_transcript_made_private_between_its_parts_stops(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    you = store.speakers(m)[0]["id"]
    store.add_segments(m, [(you, "mic", 1000.0 + i, 1000.5 + i, "Line", 0.9) for i in range(2500)])
    ready(store, m)
    real = send.send

    def private_after_the_first_part(where, key, path, said=None):
        answer = real(where, key, path, said)
        if path == "in/transcript":
            other = Store(Path(store.path))
            other.update_meeting(m, private=1)
            other.close()
        return answer

    send.send = private_after_the_first_part
    drain(send, clock)
    assert [p["part"] for p in sent(fake, "in/transcript")] == [0]
    assert paths(fake)[-1] == "in/recall" and not fake.lines["meeting-1"]


def test_taking_back_answered_paused_waits_as_if_no_answer(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.update_meeting(m, private=1)
    fake.answer = [423]
    clock.on(31)
    assert send.step() and not send.step()  # it waits, and doesn't ask again at once
    assert store.setting(sender.PAUSED) is None
    drain(send, clock)
    assert fake.messages["meeting-1"]["result"] == "private"


def test_taking_back_goes_before_what_other_meetings_send(store, fake, send, clock):
    one = meeting(store, clock)
    two = meeting(store, clock, "meeting-2")
    drain(send, clock)
    store.update_meeting(one, title="Waits since before")
    store.update_meeting(two, private=1)
    drain(send, clock)
    assert paths(fake)[-2:] == ["in/recall", "in"]


def test_taking_back_refused_waits_too_until_put_right(store, fake, send, clock):
    m = meeting(store, clock)
    drain(send, clock)
    store.update_meeting(m, private=1)
    fake.answer = [401]
    drain(send, clock)
    assert sender.stopped(store)["status"] == 401
    asked = len(fake.requests)
    clock.on(86_400)
    assert drain(send, clock) == 0 and len(fake.requests) == asked
    assert waiting(store) == ["recall"]  # never dropped
    store.delete_setting(sender.STOPPED)
    drain(send, clock)
    assert fake.messages["meeting-1"]["result"] == "private" and waiting(store) == []


def test_a_client_set_to_never_after_it_went_sends_nothing_more(store, fake, send, clock):
    store.set_setting(outbox.TRANSCRIPTS, "1")
    m = meeting(store, clock)
    ready(store, m)
    drain(send, clock)
    asked = len(fake.requests)  # its message and its transcript
    store.set_setting(zachcore.NEVER, '["contoso"]')
    store.update_meeting(m, title="Changed after")
    store.edit_segment(store.transcript(m)[0]["id"], "Other words")
    drain(send, clock)
    assert len(fake.requests) == asked and waiting(store) == []  # it stays as it was there


def test_send_now_goes_at_once_and_take_back_takes_it_out(store, fake, send, clock):
    m = meeting(store, clock)
    store.db.execute("UPDATE sends SET wanted = 0 WHERE uid = 'meeting-1'")  # from before
    store.db.execute("DELETE FROM outbox")
    store.db.commit()
    with store.transaction():
        outbox.send_now(store.db, m)
    assert send.step() and len(fake.stored) == 1  # at once, no half minute
    with store.transaction():
        outbox.take_back(store.db, "meeting-1")
    drain(send, clock)
    assert fake.messages["meeting-1"]["result"] == "private"
    store.update_meeting(m, title="Changed while taken back")
    assert drain(send, clock) == 0  # taken back: nothing goes until Send now
    with store.transaction():
        outbox.send_now(store.db, m)
    drain(send, clock)
    assert body(fake)["title"] == "Changed while taken back" and len(fake.stored) == 2
    store.update_meeting(m, title="And it goes on going")  # no longer taken back
    drain(send, clock)
    assert body(fake)["title"] == "And it goes on going"


def test_send_now_never_takes_a_private_meeting(store, fake, send, clock):
    m = meeting(store, clock)
    store.update_meeting(m, private=1)
    with pytest.raises(ValueError), store.transaction():
        outbox.send_now(store.db, m)
    assert waiting(store) == [] and drain(send, clock) == 0


@pytest.mark.parametrize("first", [True, False])
def test_another_server_gets_the_meetings_as_new(store, fake, send, clock, keys, first):
    """Taken back from the old one first (the usual), or left there."""
    m = meeting(store, clock)
    drain(send, clock)
    old = sender.place(store).target
    other = Fake()
    keys[other.address] = other.key
    try:
        store.set_setting(sender.ADDRESS, other.address)
        with store.transaction():
            assert outbox.move(store.db, old, first) == 1
        drain(send, clock)
        assert [uid for uid, _ in other.stored] == ["meeting-1"]
        assert fake.messages["meeting-1"]["result"] == ("private" if first else "stored")
        store.update_meeting(m, private=1)
        drain(send, clock)
        assert other.messages["meeting-1"]["result"] == "private"  # taken back where it is now
    finally:
        other.close()


def test_a_redaction_for_the_old_server_goes_there_whatever_is_chosen(
    store, fake, send, clock, keys
):
    m = meeting(store, clock)
    drain(send, clock)
    old = sender.place(store).target
    redact.redact(store, m, 250.0, 300.0, with_audio=False)
    other = Fake()
    keys[other.address] = other.key
    try:
        store.set_setting(sender.ADDRESS, other.address)
        with store.transaction():
            assert outbox.move(store.db, old, False) == 0  # it stays where it is, to be redacted
        drain(send, clock)
        assert "in/redact" in paths(fake) and other.requests == []
    finally:
        other.close()


def test_a_server_that_doesnt_answer_any_more_can_be_left(store, fake, send, clock, keys):
    """Taking back from a test server that is gone holds them up; leaving them there doesn't."""
    meeting(store, clock)
    drain(send, clock)
    old = sender.place(store).target
    fake.down = True
    other = Fake()
    keys[other.address] = other.key
    try:
        store.set_setting(sender.ADDRESS, other.address)
        with store.transaction():
            outbox.move(store.db, old, True)
        clock.on(31)
        assert send.step() and not send.step()  # the old one doesn't answer: it waits
        assert other.stored == [] and waiting(store) == ["recall", "send"]
        with store.transaction():
            assert outbox.move(store.db, old, False) == 1  # left there after all
        send.held_until = 0
        drain(send, clock)
        assert [uid for uid, _ in other.stored] == ["meeting-1"]
    finally:
        other.close()


def test_an_outbox_from_before_it_sent_gets_its_columns(tmp_path):
    store = Store(tmp_path / "old.db")
    for column in outbox.SENT:
        store.db.execute(f"ALTER TABLE outbox DROP COLUMN {column.split()[0]}")
    store.db.commit()
    store.close()
    store = Store(tmp_path / "old.db")
    columns = {row["name"] for row in store.db.execute("PRAGMA table_info(outbox)")}
    store.close()
    assert {column.split()[0] for column in outbox.SENT} <= columns


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("https://notes.example.com/", "https://notes.example.com"),
        (" HTTPS://Notes.Example.com:8443/zc ", "https://notes.example.com:8443/zc"),
        ("http://127.0.0.1:8787", "http://127.0.0.1:8787"),
        ("http://notes.example.com", None),
        ("http://localhost:8787", None),
        ("ftp://notes.example.com", None),
        ("https://name:secret@notes.example.com", None),
        ("https://notes.example.com/?key=1", None),
        ("notes.example.com", None),
        ("https://notes.example.com:99999", None),
    ],
)
def test_an_address_is_https_or_this_pc(typed, kept):
    if kept is None:
        with pytest.raises(ValueError):
            sender.checked_address(typed)
    else:
        assert sender.checked_address(typed) == kept


def test_settings_say_nothing_of_the_key(store, fake, send, clock):
    meeting(store, clock)
    drain(send, clock)
    settings = json.dumps([tuple(row) for row in store.db.execute("SELECT * FROM settings")])
    assert fake.key not in settings
