"""The sender killed at random moments while it sends to ZachCore, in a process of its own as in
the engine, with meetings changing meanwhile: started again, it goes on where it was, and at
the end the server has every meeting once, in its newest form.

ZACHCAP_KILLS says how many times it is killed: a few by default, 200 for the run the docs
report. ZACHCAP_KILLS_SEED picks another sequence of kills and changes."""

import os
import random
import subprocess
import sys
import time
from pathlib import Path

from test_zachcore_message import AGAIN, call, summarized
from zachcore_fake import Fake

from zachcap import outbox, redact, sender, zachcore
from zachcap.store import REDACTED, Store

KILLS = int(os.environ.get("ZACHCAP_KILLS", "5"))
SEED = int(os.environ.get("ZACHCAP_KILLS_SEED", "1"))
RUNNER = Path(__file__).parent / "zachcore_runner.py"
MEETINGS = 4
MAIN = "uid LIKE 'meeting-%'"


def start(library: Path, fake: Fake) -> subprocess.Popen:
    env = {
        **os.environ,
        "ZACHCAP_TEST_LIBRARY": str(library),
        "ZACHCAP_TEST_ADDRESS": fake.address,
        "ZACHCAP_TEST_KEY": fake.key,
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    return subprocess.Popen(
        [sys.executable, str(RUNNER)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def waits(store: Store) -> int:
    """How much of the meetings' waits to go."""
    return store.db.execute(
        f"SELECT COUNT(*) FROM outbox WHERE {MAIN} AND state <> 'stopped'"
    ).fetchone()[0]


def kill(process: subprocess.Popen) -> None:
    process.kill()
    process.wait(30)


def due(store: Store) -> None:
    """As if time went by: what waits for quiet, or after no answer, may go."""
    store.db.execute(f"UPDATE outbox SET quiet_until = 0, retry_at = 0 WHERE {MAIN}")
    store.db.commit()


def change(store: Store, rng: random.Random, meetings: list[int], n: int) -> str:
    """Something done to one of the meetings, as a person would, while it sends or is down."""
    m = rng.choice(meetings)
    what = rng.choice(
        ["title", "title", "name", "tick", "summary", "line", "redact", "private", "public"]
    )
    if what == "title":
        store.update_meeting(m, title=f"Meeting, take {n}")
    elif what == "name":
        voice = rng.choice([v["id"] for v in store.speakers(m) if v["label"] != "You"])
        store.set_speaker_person(voice, store.person_id(f"Person {n}"), "confirmed")
    elif what == "tick":
        items = store.action_items(meeting_id=m)
        if items:
            item = rng.choice(items)
            store.set_action_status(item["id"], "open" if item["status"] == "done" else "done")
    elif what == "summary":
        store.replace_action_items(m, AGAIN[: rng.randint(1, len(AGAIN))])
    elif what == "line":
        line = rng.choice(store.transcript(m))
        store.edit_segment(line["id"], f"Said again, {n}")
    elif what == "redact":
        start = rng.uniform(0, 850)
        redact.redact(store, m, start, start + rng.uniform(1, 40), with_audio=False)
    elif what == "private":
        store.update_meeting(m, private=1)
    else:
        store.update_meeting(m, private=0)
    due(store)
    return what


def test_killed_at_any_moment_it_goes_on_and_the_server_has_each_meeting_once(tmp_path):
    rng = random.Random(SEED)
    fake = Fake()
    library = tmp_path / "test.db"
    store = Store(library)
    try:
        store.set_setting(outbox.ON, "1")
        store.set_setting(outbox.TRANSCRIPTS, "1")
        store.set_setting(sender.ADDRESS, fake.address)
        meetings = []
        for n in range(MEETINGS):
            m = call(store, f"meeting-{n + 1}")
            summarized(store, m)
            meetings.append(m)
        store.db.execute(f"DELETE FROM outbox WHERE NOT {MAIN}")  # the call's own helpers
        store.db.execute("UPDATE meetings SET status = 'ready' WHERE uid LIKE 'meeting-%'")
        store.db.commit()
        due(store)
        done, kinds = [], {"anytime": 0, "mid-request": 0}
        for n in range(KILLS):
            if not waits(store):
                done.append(change(store, rng, meetings, n))
            mid = rng.random() < 0.5
            fake.holding.clear()
            if mid:
                fake.hold.clear()  # the next request waits in the server: killed meanwhile
            process = start(library, fake)
            held = mid and fake.holding.wait(15)
            time.sleep(rng.uniform(0, 0.01) if held else rng.uniform(0.2, 2.0))
            kill(process)
            fake.hold.set()  # what it held goes in now, and its answer goes nowhere
            kinds["mid-request" if held else "anytime"] += 1
            if rng.random() < 0.6:
                done.append(change(store, rng, meetings, n))
            if n == KILLS - 2:
                store.delete_meeting(meetings.pop())
                due(store)
            due(store)
        # Then it runs until it has sent all of it.
        process = start(library, fake)
        try:
            for _ in range(600):
                left = waits(store)
                if not left:
                    break
                due(store)
                time.sleep(0.2)
            assert not left, "the outbox never emptied"
        finally:
            kill(process)

        # Every meeting once, in its newest form.
        assert len(set(fake.stored)) == len(fake.stored)  # no revision kept twice
        last: dict[str, int] = {}
        for uid, rev in fake.stored:
            assert rev > last.get(uid, 0)  # each newer than the one before
            last[uid] = rev
        for m in meetings:
            uid = store.meeting(m)["uid"]
            there = fake.messages.get(uid)
            if store.meeting(m)["private"]:  # taken back, if it was ever there
                assert there is None or there["result"] == "private"
                assert not fake.lines.get(uid)
                continue
            message = dict(there["body"])  # what the server has of it, put together
            assert message.pop("rev") == there["rev"]
            now = zachcore.message(store, m)
            now.pop("gone", None)  # what was to be done, not what is there
            assert message == now, uid
            said = [fake.lines[uid][n] for n in sorted(fake.lines.get(uid, {}))]
            here = zachcore.transcript(store, m)
            # The server took out what was redacted; the lines around it weren't sent again.
            assert said in (here, [line for line in here if line["text"] != REDACTED]), uid
        if "meeting-4" in fake.messages:  # the one deleted: taken back, if it was there
            assert fake.messages["meeting-4"]["result"] == "private"
        assert not store.db.execute("SELECT 1 FROM sends WHERE uid = 'meeting-4'").fetchone()
        print(
            f"\n{KILLS} kills ({kinds['mid-request']} with a request on its way), "
            f"{len(done)} changes ({', '.join(sorted(set(done)))}); "
            f"{len(fake.requests)} requests, {fake.unanswered} answers that found nobody, "
            f"{len(fake.stored)} messages kept, {MEETINGS} meetings: each once, the newest"
        )
    finally:
        store.close()
        fake.close()
