"""What one failing server, request or answer may hold back of the outbox: only its own. From a
security review of sending to ZachCore: a server gone with a take-back waiting held back every
other meeting; a broken or hostile server could stop the sender with an answer it couldn't
read, a revision out of reason, or one byte a minute; two refusing servers made it ask in a
loop with no wait."""

import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_zachcore_sender import Clock, due, meeting, paths
from zachcore_fake import Fake

from zachcap import apikeys, outbox, sender, zachcore
from zachcap.api import create_app
from zachcap.store import Store

LOCAL = "http://127.0.0.1"


@pytest.fixture
def fake():
    fake = Fake()
    yield fake
    fake.close()


@pytest.fixture
def other():
    other = Fake()
    yield other
    other.close()


@pytest.fixture
def keys(fake: Fake, other: Fake, monkeypatch) -> dict:
    keys = {fake.address: fake.key, other.address: other.key}
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


def steps(send: sender.Sender, clock: Clock, seconds: float, every: float = 31) -> int:
    """Lets the sender go for ``seconds`` of its clock, a look every ``every``. How many did
    something."""
    went = 0
    end = clock.now + seconds
    while clock.now < end:
        while send.step():
            went += 1
            if went > 200:
                raise AssertionError("it never stops")
        clock.on(every)
    return went


def test_a_server_that_is_gone_holds_back_only_what_goes_there(store, fake, other, send, clock):
    first = meeting(store, clock)
    steps(send, clock, 62)
    assert [uid for uid, _ in fake.stored] == ["meeting-1"]
    old = sender.place(store).target
    fake.down = True  # the old server is gone...
    store.update_meeting(first, private=1)  # ... with this to take back from it
    store.set_setting(sender.ADDRESS, other.address)
    with store.transaction():
        outbox.move(store.db, old, False)  # Leave them there
    meeting(store, clock, "meeting-2")

    steps(send, clock, 120)
    assert [uid for uid, _ in other.stored] == ["meeting-2"]  # it went meanwhile
    assert [row["what"] for row in store.db.execute("SELECT what FROM outbox")] == ["recall"]
    fake.down = False
    steps(send, clock, sender.LONGEST_WAIT + 62)
    assert fake.messages["meeting-1"]["result"] == "private"  # never dropped
    assert store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_what_goes_wrong_in_one_request_holds_back_only_that_one(
    store, fake, send, clock, monkeypatch
):
    meeting(store, clock)
    meeting(store, clock, "meeting-2")
    made = zachcore.message

    def broken(store_, meeting_id, said=zachcore.PARTS):
        if store_.meeting(meeting_id)["uid"] == "meeting-1":
            raise RecursionError("an answer too deep to read")
        return made(store_, meeting_id, said)

    monkeypatch.setattr(zachcore, "message", broken)
    steps(send, clock, 62)
    assert [uid for uid, _ in fake.stored] == ["meeting-2"]
    row = store.db.execute("SELECT * FROM outbox WHERE uid = 'meeting-1'").fetchone()
    assert row["reason"] == "Something went wrong sending it" and row["tries"] == 1
    assert row["retry_at"] > clock.now - 31  # it waits: the next look doesn't take it again
    monkeypatch.setattr(zachcore, "message", made)
    steps(send, clock, sender.LONGEST_WAIT)
    assert sorted(uid for uid, _ in fake.stored) == ["meeting-1", "meeting-2"]


class Raw:
    """A server that answers whatever bytes it is given, a few at a time when told to."""

    def __init__(self, answer: bytes, every: float = 0.0) -> None:
        self.answer, self.every = answer, every
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.address = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                try:
                    if not self.every:
                        conn.sendall(self.answer)
                        continue
                    for i in range(len(self.answer)):
                        conn.sendall(self.answer[i : i + 1])
                        time.sleep(self.every)
                except OSError:
                    continue

    def close(self) -> None:
        self.listener.close()


def answered(body: bytes) -> sender.Answer:
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    raw = Raw(head + b"Content-Length: %d\r\n\r\n" % len(body) + body)
    try:
        return sender.request(sender.Place(raw.address, "work"), "the-key", "in/state")
    finally:
        raw.close()


def test_answers_that_arent_plain_json_are_none():
    assert answered(b"[" * 200_000 + b"]" * 200_000) == sender.Answer(200)  # too deep
    assert answered(b'[{"ref": "m", "rev": NaN}]') == sender.Answer(200)
    assert answered(b'[{"ref": "m", "rev": Infinity}]') == sender.Answer(200)
    assert answered(b'[{"ref": "m", "rev": 5}]').body == [{"ref": "m", "rev": 5}]


@pytest.mark.parametrize(
    ("revs", "kept"),
    [
        ([5, 7], 7),
        ([True, 1.5, "9", None, -3, 0, 6], 6),
        ([2**63 - 1, 2**64, 4], 4),  # one more would not fit in the library
        ([2**53 + 1], None),
        ([], None),
    ],
)
def test_a_revision_out_of_reason_isnt_taken(revs, kept):
    said = [{"ref": "m-1", "rev": rev} for rev in revs]
    send = sender.Sender(send=lambda where, key, path, body=None: sender.Answer(200, said))
    row = {"uid": "m-1"}
    assert send._state(None, row, sender.Place("https://notes.example.com", "work"), "k") == kept


def test_a_server_that_drips_is_cut_off_at_the_deadline(monkeypatch):
    monkeypatch.setattr(sender, "DEADLINE", 1.0)
    raw = Raw(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 100, every=0.25)
    try:
        began = time.monotonic()
        answer = sender.request(sender.Place(raw.address, "work"), "the-key", "in/state")
    finally:
        raw.close()
    assert answer == sender.Answer(None) and time.monotonic() - began < 10


def test_two_refusing_servers_wait_instead_of_asking_in_a_loop(store, fake, other, send, clock):
    first = meeting(store, clock)
    steps(send, clock, 62)
    old = sender.place(store).target
    store.update_meeting(first, private=1)  # a take-back for the old server...
    fake.answer = [403] * 50  # ... which no longer takes the key
    store.set_setting(sender.ADDRESS, other.address)
    with store.transaction():
        outbox.move(store.db, old, False)
    meeting(store, clock, "meeting-2")
    other.answer = [404] * 50  # and the new one has no such space

    steps(send, clock, 600, every=1)
    asked = len(fake.requests) + len(other.requests)
    assert asked <= 6  # each tried once or so, then waiting: not a request a second
    reasons = {row["reason"] for row in store.db.execute("SELECT reason FROM outbox")}
    assert all(reason.startswith(sender.REFUSED) for reason in reasons)

    # A new key (or address, or space) and they go at once.
    fake.answer, other.answer = [], []
    with store.transaction():
        store.delete_setting(sender.STOPPED)
        sender.unrefuse(store.db)
    assert due(send) <= clock.now
    steps(send, clock, 62)
    assert fake.messages["meeting-1"]["result"] == "private"
    assert [uid for uid, _ in other.stored] == ["meeting-2"]
    assert "in/recall" in paths(fake)


def test_a_typed_key_with_a_line_break_isnt_tried(tmp_path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    tried = []
    monkeypatch.setattr(sender, "check", lambda where, key: tried.append(key) or {"ok": True})
    for typed in ("zachcap.abc\r\nX-Other: 1", "two words", "x" * 1001):
        said = client.post(
            "/api/zachcore/test",
            headers={"Authorization": "Bearer test-token"},
            json={"address": "https://notes.example.com", "key": typed},
        )
        assert said.status_code == 400 and typed not in said.text
    assert tried == []
