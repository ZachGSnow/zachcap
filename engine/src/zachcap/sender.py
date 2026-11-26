"""Sends meetings to ZachCore from the outbox in the library (outbox.py): one thread in the
engine, one request at a time, so nothing is lost when the engine stops in the middle of one,
and recording, transcribing and the page never wait for it.

Every request goes with a revision. A body that wasn't sent before gets one of its own, larger
than any before (the time in milliseconds, or the last one and 1), saved with its row before
the request goes: sent again after no answer or a restart, it is the same request, and ZachCore
keeps it once. A meeting is checked for being private when it is queued and again right before
each request. Taking something back goes first, also while sending is off or ZachCore is
paused, and nothing else of that meeting goes while it waits.

What each answer means (ZachCore's docs/sources.md):

- 200: done.
- 400, 413: that message or transcript waits until it changes.
- 401, 403, 404: nothing goes to that server until its key, address or space changes.
- 409: the server has a newer revision. When in/state says ours went backwards, it goes again
  above it. A transcript is read again; taking back that is there already is done.
- 423: paused in ZachCore. Messages and transcripts wait.
- 503: out of writing for today. All of it waits as long as Retry-After says, a day at most.
- No answer, or another: the same again later, waiting longer each time, up to an hour.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__, apikeys, outbox, zachcore
from .store import Store

ADDRESS = "zachcore_address"  # the server's, as checked_address keeps it
SPACE = "zachcore_space"
STOPPED = "zachcore_stopped"  # {"place": ..., "status": 401}: nothing goes there
PAUSED = "zachcore_paused_until"  # seconds since 1970: paused in ZachCore (423)
FULL = "zachcore_full_until"  # ... out of writing for today (503)
WORK = "work"
PART = 2000  # lines in one part of a transcript, at most
TIMEOUT = 60  # seconds for one read of a request
DEADLINE = 300  # seconds for a whole request: one sending a byte a minute is cut off
MOST_REV = 2**53  # the highest revision ZachCore (JavaScript) keeps exactly
REFUSED = "ZachCore refused the key or the space"
MOST_ANSWER = 1_000_000  # bytes of an answer read, at most
FIRST_WAIT, LONGEST_WAIT = 30, 3600  # after no answer, then twice as long each time
PAUSED_WAIT = 15 * 60
LONGEST_FULL = 86_400
NAMES = 86_400  # how long a transcript waits for its voices' names
BUSY = ("recording", "recorded", "processing")
TAKE_BACK = ("recall", "redact")
NOT_KEPT = (400, 401, 403, 404, 413, 423, 503)  # answers that say nothing was kept
IDLE = 10  # seconds between looks at the outbox while nothing is due


@dataclass(frozen=True)
class Place:
    """A server, and the space in it."""

    address: str
    space: str

    @property
    def target(self) -> str:
        return f"{self.space} {self.address}"

    @staticmethod
    def of(target: str) -> Place:
        space, _, address = target.partition(" ")
        return Place(address, space)


def checked_address(text: str) -> str:
    """A server's address as it is kept: https, or plain http to this PC (127.0.0.1) only, with
    no slash at the end. ValueError for anything else."""
    try:
        parts = urllib.parse.urlsplit(text.strip())
        port = parts.port
    except ValueError:
        raise ValueError("That isn't an address") from None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password:
        raise ValueError("The address starts with https:// and has no name or password in it")
    if parts.query or parts.fragment:
        raise ValueError("The address has no ? or # in it")
    if parts.scheme == "http" and host != "127.0.0.1":
        raise ValueError("Use https: plain http goes only to this PC (127.0.0.1)")
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}{parts.path.rstrip('/')}"


def checked_space(text: str) -> str:
    space = text.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", space):
        raise ValueError("A space is a short name such as work or home")
    return space


def place(store: Store) -> Place | None:
    """Where meetings go now; None until an address is set."""
    address = store.setting(ADDRESS)
    return Place(address, store.setting(SPACE) or WORK) if address else None


def stopped(store: Store) -> dict | None:
    """Where nothing goes, and why: {"place": target, "status": 401}."""
    try:
        found = json.loads(store.setting(STOPPED) or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


@dataclass(frozen=True)
class Answer:
    status: int | None  # None: none came
    body: object = None
    retry_after: float | None = None
    untrusted: str | None = None  # none came: the server's certificate didn't verify (why)


def request(where: Place, key: str, path: str, body: dict | None = None) -> Answer:
    """One request to ZachCore: POST with a body, GET without. Nothing of it is logged, and the
    key goes only in its header. No redirect is followed."""
    parts = urllib.parse.urlsplit(where.address)
    if parts.scheme == "https":
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=TIMEOUT, context=context
        )
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=TIMEOUT)
    headers = {"Authorization": f"Bearer {key}", "User-Agent": f"ZachCap/{__version__}"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        data = data.encode("utf-8")
        headers["Content-Type"] = "application/json"
    timer = threading.Timer(DEADLINE, _cut, (conn,))
    timer.daemon = True
    try:
        timer.start()
        conn.request(
            "GET" if body is None else "POST",
            f"{parts.path}/v1/s/{where.space}/{path}",
            body=data,
            headers=headers,
        )
        response = conn.getresponse()
        raw = response.read(MOST_ANSWER)
        status, wait = response.status, response.getheader("Retry-After")
    except ssl.SSLCertVerificationError as error:
        # Never sent: certificate and name are checked against Windows' trusted roots.
        return Answer(None, untrusted=error.verify_message or "not trusted")
    except (OSError, http.client.HTTPException):
        return Answer(None)
    finally:
        timer.cancel()
        conn.close()
    said = _parsed(raw)
    try:
        seconds = float(wait) if wait is not None else None
    except ValueError:
        seconds = None
    return Answer(status, said, seconds if seconds is not None and math.isfinite(seconds) else None)


def _cut(conn: http.client.HTTPConnection) -> None:
    """A request past its deadline: its socket is shut, so the read waiting on it ends."""
    sock = conn.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def _parsed(raw: bytes):
    """An answer's JSON. None when there is none, or it isn't plain JSON: NaN and Infinity
    aren't, and a nesting too deep to read is nothing ZachCore says."""
    if not raw:
        return None
    try:
        return json.loads(raw, parse_constant=_not_json)
    except (ValueError, RecursionError):
        return None


def _not_json(name: str):
    raise ValueError(f"{name} isn't JSON")


def _backoff(tries: int) -> float:
    """How long after ``tries`` failures in a row: half a minute, twice as long each time,
    up to an hour."""
    return min(FIRST_WAIT * 2 ** min(tries, 20), LONGEST_WAIT)


def unrefuse(db) -> None:
    """A new key, address or space: what was refused may be taken now, so it goes at once."""
    db.execute("UPDATE outbox SET retry_at = 0 WHERE reason LIKE ?", (f"{REFUSED}%",))


class Sender:
    """The outbox's one sender. ``step`` sends what is due next; the engine runs
    ``run_forever`` on a thread of its own. ``wake`` makes it look at once (Send now)."""

    def __init__(
        self,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
        send: Callable[..., Answer] = request,
    ) -> None:
        self.path = path
        self.clock = clock
        self.send = send
        self.wake = threading.Event()
        # Per server address (None: anywhere, after a fault outside any one request): how
        # many requests in a row had no answer, and when the next may go. A server that is
        # gone holds back only what goes there.
        self.held: dict[str | None, tuple[int, float]] = {}

    @property
    def held_until(self) -> float:
        """When the last hold ends."""
        return max((until for _, until in self.held.values()), default=0.0)

    @held_until.setter
    def held_until(self, until: float) -> None:
        self.held = {
            where: (failures, min(was, until)) for where, (failures, was) in self.held.items()
        }

    def _held(self, where: Place, now: float) -> bool:
        return any(now < self.held.get(at, (0, 0.0))[1] for at in (None, where.address))

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                went = self.step()
            except Exception:  # what it was is not kept: it could say what was sent
                self._no_answer(self.clock(), None)
                went = False
            if not went:
                self.wake.wait(IDLE)
                self.wake.clear()

    def step(self) -> bool:
        """One request, the next that is due. Whether there was one."""
        store = Store(self.path)
        try:
            return self._step(store)
        finally:
            store.close()

    def _step(self, store: Store) -> bool:
        now = self.clock()
        if now < self.held.get(None, (0, 0.0))[1] or now < _seconds(store.setting(FULL)):
            return False
        here = place(store)
        halted = (stopped(store) or {}).get("place")
        said = zachcore.parts(store)
        lines = store.setting(outbox.TRANSCRIPTS) == "1"
        on = store.setting(outbox.ON) == "1" and bool(said or lines)  # all four off: nothing
        paused = now < _seconds(store.setting(PAUSED))
        keys: dict[str, str | None] = {}

        def key_of(where: Place) -> str | None:
            if where.address not in keys:
                keys[where.address] = apikeys.zachcore(where.address)
            return keys[where.address]

        taking = {
            row["uid"]
            for row in store.db.execute("SELECT uid FROM outbox WHERE what IN ('recall', 'redact')")
        }
        rows = store.db.execute(
            "SELECT o.*, s.target, s.rev AS version, m.id AS meeting, m.private, m.status, "
            "m.started_at "
            "FROM outbox o JOIN sends s ON s.uid = o.uid LEFT JOIN meetings m ON m.uid = o.uid "
            "WHERE o.state <> 'stopped' AND o.quiet_until <= ? AND o.retry_at <= ? "
            "ORDER BY o.what NOT IN ('recall', 'redact'), o.what = 'transcript', o.id",
            (now, now),
        ).fetchall()
        for row in rows:
            if row["what"] in TAKE_BACK:
                if row["target"] is None:  # taken back whole since: nothing of it is there
                    with _writing(store):
                        _done(store, row, row["version"])
                    return True
                where = Place.of(row["target"])
                if where.target != halted and not self._held(where, now) and (key := key_of(where)):
                    self._guarded(store, row, now, self._take_back, store, row, where, key, now)
                    return True
                continue
            if not on or here is None or here.target == halted or paused or self._held(here, now):
                continue
            if row["uid"] in taking or row["target"] not in (None, here.target):
                continue  # taking back goes first; sent to another server, asked about there
            if row["meeting"] is None or row["private"]:
                with _writing(store):
                    _done(store, row, row["version"])  # gone, or private: taken back
                return True
            if zachcore.kept_from_zachcore(store, store.meeting(row["meeting"])):
                with _writing(store):
                    _done(store, row, row["version"])  # nothing of its client goes
                return True
            if row["status"] in BUSY or not (key := key_of(here)):
                continue
            if row["what"] == "send":
                self._guarded(store, row, now, self._message, store, row, here, key, said, now)
                return True
            if lines and (row["status"] == "ready" or now - _started(row["started_at"]) > NAMES):
                self._guarded(store, row, now, self._transcript, store, row, here, key, now)
                return True
        return False

    def _guarded(self, store: Store, row, now: float, go: Callable[..., None], *args) -> None:
        """One row's request. Should something go wrong in it (an answer that can't be read,
        a fault of ours), that row waits, longer each time, and the rows after it go."""
        try:
            go(*args)
        except Exception:  # what it was is not kept: it could say what was sent
            with _writing(store):
                _later(store, row, "Something went wrong sending it", now + _backoff(row["tries"]))

    # Messages ---------------------------------------------------------------------------

    def _message(self, store: Store, row, here: Place, key: str, said, now: float) -> None:
        with _reading(store):
            version = _sends(store, row["uid"])["rev"]
            body = zachcore.message(store, row["meeting"], said)
        if body is None:
            with _writing(store):
                _done(store, row, version)
            return
        digest = zachcore.digest(body)
        ready = _ready(store, row, here, digest, now, "sent_hash", version)
        if ready is None or _private(store, row):
            return
        rev, before = ready
        answer = self.send(here, key, "in", {**body, "rev": rev})
        if answer.status == 409:
            there = self._state(store, row, here, key)
            if there is None:
                answer = Answer(None)  # it couldn't be told: as if no answer came
            elif there > (_sends(store, row["uid"])["sent_rev"] or 0):
                # Above any revision this library gave out (it was restored from a backup, or
                # the clock was put back): the next goes above it.
                with _writing(store):
                    _raise_rev(store, row["uid"], there)
                    _later(store, row, None, _soon(row, now), fresh=True)
                return
            else:
                answer, digest = Answer(200), None  # the server has a newer one of these
        if answer.status == 200:
            self._answered(store, row, here, rev, "sent_hash", digest, version)
            return
        if answer.status in NOT_KEPT:
            _put_back(store, row, here, before)
        self._failed(store, row, here, answer, now)

    # Transcripts ------------------------------------------------------------------------

    def _transcript(self, store: Store, row, here: Place, key: str, now: float) -> None:
        with _reading(store):
            version = _sends(store, row["uid"])["rev"]
            lines = zachcore.transcript(store, row["meeting"])
        digest = zachcore.digest(lines)
        ready = _ready(store, row, here, digest, now, "lines_hash", version)
        if ready is None:
            return
        rev, before = ready
        parts = max(1, math.ceil(len(lines) / PART))
        for part in range(parts):
            if _private(store, row):
                return
            body = {"ref": row["uid"], "rev": rev, "part": part, "parts": parts}
            body["rows"] = lines[part * PART : (part + 1) * PART]
            answer = self.send(here, key, "in/transcript", body)
            if answer.status == 409:
                # Something was taken back after the lines were read, or the meeting is taken
                # back there: read again, with a revision as new as the server's.
                there = self._state(store, row, here, key)
                with _writing(store):
                    _raise_rev(store, row["uid"], there or 0)
                    _later(store, row, "ZachCore took something back", _soon(row, now))
                return
            if answer.status != 200:
                if answer.status in NOT_KEPT and part == 0:
                    _put_back(store, row, here, before)
                self._failed(store, row, here, answer, now)
                return
        self._answered(store, row, here, rev, "lines_hash", digest, version)

    # Taking back ------------------------------------------------------------------------

    def _take_back(self, store: Store, row, where: Place, key: str, now: float) -> None:
        with _writing(store):
            sent = _sends(store, row["uid"])
            rev = row["rev"] if row["rev"] and row["rev"] == sent["sent_rev"] else None
            rev = rev or _next_rev(sent, now)
            store.db.execute("UPDATE sends SET sent_rev = ? WHERE uid = ?", (rev, row["uid"]))
            store.db.execute(
                "UPDATE outbox SET rev = ?, state = 'sending' WHERE id = ?", (rev, row["id"])
            )
        body: dict = {"ref": row["uid"], "rev": rev}
        if row["what"] == "redact":
            # Lines are found by when they begin, to two places: a little wider, never narrower.
            body["from"] = math.floor(row["from_sec"] * 100) / 100
            body["to"] = math.ceil(row["to_sec"] * 100) / 100
        answer = self.send(where, key, f"in/{row['what']}", body)
        if answer.status == 409:
            there = self._state(store, row, where, key)
            if there == rev:
                answer = Answer(200)  # it went before, and its answer didn't come
            elif there is not None and there > rev:  # ours went backwards: again, above it
                with _writing(store):
                    _raise_rev(store, row["uid"], there)
                    _later(store, row, None, _soon(row, now), fresh=True)
                return
            else:
                answer = Answer(None)
        if answer.status != 200:
            self._failed(store, row, where, answer, now)
            return
        self.held.pop(where.address, None)
        with _writing(store):
            store.db.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
            if row["what"] == "recall":
                store.db.execute(
                    "UPDATE sends SET target = NULL, sent_hash = NULL, lines_hash = NULL "
                    "WHERE uid = ?",
                    (row["uid"],),
                )
                _forget_if_gone(store, row["uid"])
            else:
                # The next message brings back what is clean; the server kept the other lines.
                store.db.execute(
                    "UPDATE sends SET sent_hash = NULL, lines_hash = CASE "
                    "WHEN lines_hash = ? THEN ? ELSE lines_hash END WHERE uid = ?",
                    (row["lines_before"], row["lines_after"], row["uid"]),
                )

    # Answers ----------------------------------------------------------------------------

    def _state(self, store: Store, row, where: Place, key: str) -> int | None:
        """The revision ZachCore has of a meeting (in/state), or None when it didn't say."""
        answer = self.send(where, key, f"in/state?ref={urllib.parse.quote(row['uid'], safe='')}")
        if answer.status != 200 or not isinstance(answer.body, list):
            return None
        revs = [
            item.get("rev")
            for item in answer.body
            if isinstance(item, dict) and item.get("ref") == row["uid"]
        ]
        # A plain integer ZachCore could have been sent: no bool, float, NaN or giant, which
        # would make the next revision fail or never fit.
        found = [rev for rev in revs if type(rev) is int and 0 < rev <= MOST_REV]
        return max(found) if found else None

    def _answered(
        self,
        store: Store,
        row,
        where: Place,
        rev: int,
        column: str,
        digest: str | None,
        version: int,
    ) -> None:
        """Taken: the server has what it said (``digest``; None: something newer), which was
        the meeting at its revision ``version``."""
        self.held.pop(where.address, None)
        with _writing(store):
            store.db.execute(
                f"UPDATE sends SET {column} = ?, sent_at = ? WHERE uid = ? AND sent_rev = ?",
                (digest, datetime.now().astimezone().isoformat(timespec="seconds"),
                 row["uid"], rev),
            )  # fmt: skip
            _done(store, row, version)

    def _failed(self, store: Store, row, where: Place, answer: Answer, now: float) -> None:
        status, at = answer.status, now
        if status in (401, 403, 404):
            store.set_setting(STOPPED, json.dumps({"place": where.target, "status": status}))
            why, at = f"{REFUSED} ({status})", now + LONGEST_WAIT  # or a new key (unrefuse)
        elif status == 423 and row["what"] not in TAKE_BACK:
            store.set_setting(PAUSED, str(int(now + PAUSED_WAIT)))
            why = "Paused in ZachCore"
        elif status == 503:
            wait = min(answer.retry_after or LONGEST_WAIT, LONGEST_FULL)
            store.set_setting(FULL, str(int(now + max(wait, 1))))
            why = "ZachCore is out of writing for today"
        elif status in (400, 413) and row["what"] not in TAKE_BACK:
            with _writing(store):
                store.db.execute(
                    "UPDATE outbox SET state = 'stopped', reason = ? WHERE id = ?",
                    (f"ZachCore refused it ({status})", row["id"]),
                )
            return
        else:
            at = now + self._no_answer(now, where.address)
            why = "ZachCore didn't answer" if status is None else f"ZachCore answered {status}"
        with _writing(store):
            _later(store, row, why, at)

    def _no_answer(self, now: float, address: str | None) -> float:
        """Nothing goes to that server (None: anywhere) for a while: longer each time in a row,
        up to an hour."""
        failures = self.held.get(address, (0, 0.0))[0] + 1
        wait = _backoff(failures - 1)
        self.held[address] = (failures, now + wait)
        return wait


# The library's side -------------------------------------------------------------------


@contextmanager
def _reading(store: Store) -> Iterator[None]:
    """One read transaction: what is read in it is as it was at one moment."""
    store.db.execute("BEGIN")
    try:
        yield
    finally:
        store.db.commit()


@contextmanager
def _writing(store: Store) -> Iterator[None]:
    """One write transaction, holding the write lock from its start: what it read can't
    change before it writes."""
    store.db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        store.db.rollback()
        raise
    store.db.commit()


def _sends(store: Store, uid: str):
    return store.db.execute("SELECT * FROM sends WHERE uid = ?", (uid,)).fetchone()


def _next_rev(sent, now: float) -> int:
    """A revision above any given out before: the time in milliseconds, or the last and 1."""
    return max((sent["sent_rev"] or 0) + 1, int(now * 1000))


def _ready(
    store: Store, row, here: Place, digest: str, now: float, column: str, version: int
) -> tuple[int, str | None] | None:
    """Before a message or a transcript goes: its revision, saved with its row, and that the
    meeting may be on this server now. Returns the revision, and where the meeting was before.
    None when it needn't go: what it says is what the server has, or the meeting changed since
    it was read at its revision ``version`` (it goes when it is due again)."""
    with _writing(store):
        sent = _sends(store, row["uid"])
        if sent["rev"] != version or not _there(store, row):
            return None
        if digest == sent[column]:
            store.db.execute("DELETE FROM outbox WHERE id = ?", (row["id"],))
            return None
        if column == "lines_hash":  # the lines go with the meeting's revision as it is now
            rev = sent["sent_rev"] or _next_rev(sent, now)
        elif row["rev"] and row["rev"] == sent["sent_rev"] and row["hash"] == digest:
            rev = row["rev"]  # the same request again: no answer came to it
        else:
            rev = _next_rev(sent, now)
        store.db.execute(
            f"UPDATE sends SET sent_rev = ?, target = ?, {column} = NULL WHERE uid = ?",
            (rev, here.target, row["uid"]),
        )
        store.db.execute(
            "UPDATE outbox SET rev = ?, hash = ?, state = 'sending' WHERE id = ?",
            (rev, digest, row["id"]),
        )
    return rev, sent["target"]


def _put_back(store: Store, row, here: Place, before: str | None) -> None:
    """The server kept nothing of a request: the meeting is where it was before it (nowhere,
    if that was its first), so nothing is taken back from a server that never had it."""
    with _writing(store):
        store.db.execute(
            "UPDATE sends SET target = ? WHERE uid = ? AND target = ?",
            (before, row["uid"], here.target),
        )


def _there(store: Store, row) -> bool:
    """Whether a row is still in the outbox: made private or deleted meanwhile, it isn't."""
    return (
        store.db.execute("SELECT 1 FROM outbox WHERE id = ?", (row["id"],)).fetchone() is not None
    )


def _private(store: Store, row) -> bool:
    """Right before a request: whether the meeting is private now, or gone."""
    found = store.db.execute("SELECT private FROM meetings WHERE uid = ?", (row["uid"],)).fetchone()
    return found is None or bool(found["private"])


def _done(store: Store, row, version: int) -> None:
    """A row that needn't wait any more, unless the meeting changed since its revision
    ``version``: then it waits for that, from the start."""
    gone = store.db.execute(
        "DELETE FROM outbox WHERE id = ? AND (SELECT rev FROM sends WHERE uid = ?) = ?",
        (row["id"], row["uid"], version),
    ).rowcount
    if not gone:
        store.db.execute(
            "UPDATE outbox SET state = 'waiting', tries = 0, reason = NULL WHERE id = ?",
            (row["id"],),
        )


def _later(store: Store, row, why: str | None, at: float, fresh: bool = False) -> None:
    """A row that goes again at ``at`` (``fresh``: with a new revision)."""
    store.db.execute(
        "UPDATE outbox SET state = 'waiting', tries = tries + 1, reason = ?, retry_at = ?"
        + (", rev = NULL, hash = NULL" if fresh else "")
        + " WHERE id = ?",
        (why, int(at), row["id"]),
    )


def _soon(row, now: float) -> float:
    """When a row goes again after the server said it has newer: at once, and after a few
    times only later, should it go on saying so."""
    return now if row["tries"] < 2 else now + min(FIRST_WAIT * 2 ** row["tries"], LONGEST_WAIT)


def _raise_rev(store: Store, uid: str, there: int) -> None:
    """The server has a revision this high: the next one goes above it."""
    store.db.execute(
        "UPDATE sends SET sent_rev = MAX(COALESCE(sent_rev, 0), ?) WHERE uid = ?", (there, uid)
    )


def _forget_if_gone(store: Store, uid: str) -> None:
    """A deleted meeting, taken back: nothing of it is left to send or take back."""
    if store.db.execute("SELECT 1 FROM meetings WHERE uid = ?", (uid,)).fetchone():
        return
    if not store.db.execute("SELECT 1 FROM outbox WHERE uid = ?", (uid,)).fetchone():
        store.db.execute("DELETE FROM sends WHERE uid = ?", (uid,))
        store.db.execute("DELETE FROM items_gone WHERE meeting_uid = ?", (uid,))


def _seconds(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _started(started_at: str | None) -> float:
    try:
        return datetime.fromisoformat(started_at or "").timestamp()
    except ValueError:
        return 0.0


# What Settings and a meeting say ------------------------------------------------------


def status(store: Store, now: float | None = None) -> dict:
    """Sending as Settings shows it: on or not, where to, what goes, how many meetings wait and
    why, how many are being taken back, and when the server last took one. Never the key:
    only whether one is saved. ``why`` is the first thing that holds them all up, or what
    holds up most of them."""
    now = time.time() if now is None else now
    here = place(store)
    halted = stopped(store)
    key = bool(here and apikeys.zachcore(here.address))
    on = store.setting(outbox.ON) == "1"
    rows = store.db.execute(
        "SELECT o.uid, o.what, o.state, o.reason, o.retry_at, o.quiet_until, s.target, "
        "m.status FROM outbox o JOIN sends s ON s.uid = o.uid LEFT JOIN meetings m ON m.uid = o.uid"
    ).fetchall()
    going = [row for row in rows if row["what"] not in TAKE_BACK and row["status"] is not None]
    taking = [row for row in rows if row["what"] in TAKE_BACK]
    full, paused = _seconds(store.setting(FULL)), _seconds(store.setting(PAUSED))

    def why(waits: list, back: bool = False) -> dict | None:
        if not waits:
            return None
        if halted and here and halted.get("place") == here.target and not back:
            return {"kind": "refused", "status": halted.get("status")}
        if here is None and not back:
            return {"kind": "no_server"}
        if back:
            places = {Place.of(row["target"]) for row in waits if row["target"]}
            without = sorted(
                where.address for where in places if not apikeys.zachcore(where.address)
            )
            if without:
                return {"kind": "no_key", "address": without[0]}
        elif not key:
            return {"kind": "no_key", "address": here.address}
        if not on and not back:
            return {"kind": "off"}
        if now < full:
            return {"kind": "full", "until": full}
        if now < paused and not back:
            return {"kind": "paused", "until": paused}
        late = [row["retry_at"] for row in waits if row["retry_at"] > now and row["reason"]]
        if late:
            return {"kind": "no_answer", "until": min(late)}
        refused = [row for row in waits if row["state"] == "stopped"]
        if refused:
            return {"kind": "refused_one", "count": len({row["uid"] for row in refused})}
        if not back:
            elsewhere = [row for row in waits if row["target"] not in (None, here.target)]
            if elsewhere:
                return {"kind": "elsewhere", "count": len({row["uid"] for row in elsewhere})}
            if any(row["status"] in BUSY for row in waits):
                return {"kind": "busy"}
            if any(row["what"] == "transcript" and row["status"] != "ready" for row in waits):
                return {"kind": "names"}
            if any(row["quiet_until"] > now for row in waits):
                return {"kind": "quiet"}
        return {"kind": "soon"}

    targets = store.db.execute(
        "SELECT target, COUNT(*) AS count FROM sends WHERE target IS NOT NULL GROUP BY target"
    ).fetchall()
    return {
        "on": on,
        "address": here.address if here else None,
        "space": here.space if here else WORK,
        "key": key,
        "parts": {
            **{part: part in zachcore.parts(store) for part in zachcore.PARTS},
            "transcripts": store.setting(outbox.TRANSCRIPTS) == "1",
        },
        "never": zachcore.never(store),
        "waiting": len({row["uid"] for row in going}),
        "why": why(going),
        "taking_back": len({row["uid"] for row in taking}),
        "taking_back_why": why(taking, back=True),
        "there": sum(row["count"] for row in targets),
        "elsewhere": [
            {**_where(row["target"]), "count": row["count"]}
            for row in targets
            if not here or row["target"] != here.target
        ],
        "last_sent": store.db.execute("SELECT MAX(sent_at) FROM sends").fetchone()[0],
    }


def meeting_status(store: Store, meeting_id: int) -> dict | None:
    """What a meeting says of ZachCore: whether the server may have it and since when, whether
    something of it waits to go or to be taken back, and why it doesn't go, if it doesn't."""
    meeting = store.meeting(meeting_id)
    if meeting is None or not store.column(meeting, "uid"):
        return None
    sent = _sends(store, meeting["uid"])
    if sent is None:  # a library from before ZachCap sent anything
        return None
    whats = {row["what"] for row in store.db.execute(
        "SELECT what FROM outbox WHERE uid = ?", (meeting["uid"],)
    )}  # fmt: skip
    target = sent["target"]
    return {
        "there": target is not None,
        "place": _where(target) if target else None,
        "sent_at": sent["sent_at"],
        "waiting": bool(whats & {"send", "transcript"}),
        "taking_back": bool(whats & set(TAKE_BACK)),
        "can_take_back": bool(target and apikeys.zachcore(Place.of(target).address)),
        "taken": bool(sent["taken"]),
        "wanted": bool(sent["wanted"]),
        "on": store.setting(outbox.ON) == "1",
        "kept": zachcore.kept_from_zachcore(store, meeting),
    }


def _where(target: str) -> dict:
    where = Place.of(target)
    return {"place": target, "address": where.address, "space": where.space}


def check(where: Place, key: str) -> dict:
    """Whether a server takes this key for this space: it is asked what it has (nothing is
    written). The answer says how it went, never with the key in it."""
    answer = request(where, key, "in/state")
    if answer.status == 200:
        return {"ok": True, "status": 200}
    words = {
        401: "ZachCore doesn't know this key",
        403: "This key may not send to this space, or its app was removed",
        404: "ZachCore has no such space there",
        503: "ZachCore is out of writing for today; the key is fine",
        None: "No answer from that address",
    }
    status = answer.status
    if answer.untrusted:
        words[None] = f"That address's certificate isn't trusted on this PC ({answer.untrusted})"
    return {
        "ok": False,
        "status": status,
        "error": words.get(status, f"ZachCore answered {status}"),
    }
