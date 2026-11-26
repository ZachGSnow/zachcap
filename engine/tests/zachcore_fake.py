"""A small ZachCore for the tests, on 127.0.0.1: it answers the sender as ZachCore's own server
does (server/src/intake.ts and host.ts at its commit 1eb45f6), and keeps what it was sent.

It knows one app key and the spaces work and home. A message older than the one it has is 409,
the same revision again is 200 with "same"; what a message says replaces what is there, even
empty, and what it leaves out stays; recall and redact are remembered, so nothing older gets in
behind them; a transcript read before one, or of a meeting taken back, is 409; paused, messages
and transcripts are 423; out of writing, all of it is 503 with Retry-After.

It can be made to fail: answer one status next (``answer``), take a request in and close the
connection without answering (``lose``: done, ``late``: kept, to be done later), not be there at
all (``down``), or hold a request until told (``hold``)."""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PART = 2000
KEPT_RECALLED = ("ref", "title", "started", "seconds")
KEPT_REDACTED = ("ref", "started", "seconds", "app", "client", "ticket", "people")


class Fake:
    def __init__(self) -> None:
        self.key = f"zachcap.{secrets.token_hex(16)}"  # a key for nothing but this
        self.spaces = {"work", "home"}
        self.lock = threading.RLock()
        self.messages: dict[str, dict] = {}  # ref: {"rev", "result", "body"}
        self.taken: dict[str, int] = {}  # ref: the revision of the last recall or redact
        self.lines: dict[str, dict[int, dict]] = {}
        self.tasks: dict[str, dict[str, dict]] = {}  # ref: task ref: task (and "gone")
        self.stored: list[tuple[str, int]] = []  # each message it kept, in order
        self.requests: list[tuple[str, dict | None]] = []  # (path, body) of each it took in
        self.answers: list[int] = []  # what it answered, in order (0: nothing)
        self.written = 0  # transcript lines it wrote
        self.unanswered = 0  # answers that found nobody to take them (a client killed)
        self.paused = False
        self.full_for: int | None = None  # seconds of Retry-After while out of writing
        self.answer: list[int] = []  # statuses to answer next, before anything is done
        self.lose = 0  # requests to do and close without answering
        self.late: list[tuple[str, dict]] = []  # requests kept to be done later
        self.keep_late = 0
        self.down = False
        self.hold = threading.Event()
        self.hold.set()
        self.holding = threading.Event()  # set while a request is held
        handler = type("Handler", (_Handler,), {"fake": self})
        self.server = _Quiet(("127.0.0.1", 0), handler)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.hold.set()
        self.server.shutdown()
        self.server.server_close()

    # What ZachCore does ----------------------------------------------------------------

    def take(self, path: str, body: dict | None, query: dict) -> tuple[int, object, dict]:
        """(status, answer, headers) for one request that got in."""
        if path == "in/state":
            ref = (query.get("ref") or [""])[0]
            found = self.messages.get(ref)
            if found is None:
                return 200, [], {}
            tasks = [{"ref": f"{ref}#{r}", **t} for r, t in self.tasks.get(ref, {}).items()]
            return 200, [{"ref": ref, "rev": found["rev"], "tasks": json.dumps(tasks)}], {}
        if self.full_for is not None:
            return 503, {"error": "out of writing"}, {"retry-after": str(self.full_for)}
        if not isinstance(body, dict):
            return 400, {"error": "not JSON"}, {}
        if path == "in":
            return self.message(body)
        if path in ("in/recall", "in/redact"):
            return self.recall(body, path == "in/redact")
        if path == "in/transcript":
            return self.transcript(body)
        return 404, {"error": "not here"}, {}

    def message(self, msg: dict) -> tuple[int, object, dict]:
        ref, rev = msg.get("ref"), msg.get("rev")
        if not isinstance(ref, str) or not ref or len(ref) > 120 or not isinstance(rev, int):
            return 400, {"error": "A message has a ref and a rev."}, {}
        if self.paused:
            return 423, {"error": "Paused here."}, {}
        last = self.messages.get(ref)
        if last and rev < last["rev"]:
            return 409, {"error": "An older one than is here."}, {}
        if last and rev == last["rev"]:
            return 200, {"same": True}, {}
        # What it says replaces what is here, even empty; what it leaves out stays as it is.
        here = {**(last or {}).get("body", {}), **msg}
        here.pop("gone", None)  # what to do, not what there is
        self.messages[ref] = {"rev": rev, "result": "stored", "body": here}
        self.stored.append((ref, rev))
        tasks = self.tasks.setdefault(ref, {})
        for task in msg.get("tasks", []):
            tasks[task["ref"]] = {"title": task["title"], "owner": task.get("owner")}
        for gone in msg.get("gone", []):
            if gone in tasks:
                tasks[gone]["gone"] = True
        return 200, {"id": f"zachcap.{ref}", "stored": 1}, {}

    def recall(self, msg: dict, redact: bool) -> tuple[int, object, dict]:
        ref, rev = msg.get("ref"), msg.get("rev")
        if not isinstance(ref, str) or not ref or not isinstance(rev, int):
            return 400, {"error": "A ref and a rev."}, {}
        last = self.messages.get(ref)
        if last and not rev > last["rev"]:
            return 409, {"error": "An older one than is here."}, {}
        lines = self.lines.setdefault(ref, {})
        if redact:
            gone = [n for n, line in lines.items() if msg["from"] <= line["sec"] <= msg["to"]]
        else:
            gone = list(lines)
        for n in gone:
            del lines[n]
        for task in self.tasks.get(ref, {}).values():
            task["gone"] = True
        # A recall leaves its time and title; a redaction its people, client and ticket, and the
        # title says "(redacted)" until the next message. All that was made from the words goes.
        was = (last or {}).get("body", {})
        stays = KEPT_REDACTED if redact else KEPT_RECALLED
        body = {key: value for key, value in was.items() if key in stays}
        if redact:
            body["title"] = "(redacted)"
        self.messages[ref] = {
            "rev": rev,
            "result": "redacted" if redact else "private",
            "body": body,
        }
        self.taken[ref] = rev
        return 200, {"gone": len(gone), "lines": len(gone)}, {}

    def transcript(self, msg: dict) -> tuple[int, object, dict]:
        ref, rows, part = msg.get("ref"), msg.get("rows"), msg.get("part", 0)
        if not ref or not isinstance(rows, list) or len(rows) > PART:
            return 400, {"error": "A part has at most 2000 lines."}, {}
        if self.paused:
            return 423, {"error": "Paused here."}, {}
        rev = msg.get("rev")
        if ref in self.taken and isinstance(rev, int) and rev < self.taken[ref]:
            return 409, {"error": "Something was taken back since. Read the lines again."}, {}
        if self.messages.get(ref, {}).get("result") == "private":
            return 409, {"error": "It was taken back."}, {}
        lines = self.lines.setdefault(ref, {})
        stored = 0
        for i, row in enumerate(rows):
            n = part * PART + i
            if lines.get(n) != row:
                lines[n] = row
                stored += 1
        parts = msg.get("parts")
        if parts is not None and part == parts - 1:
            for n in [n for n in lines if n >= part * PART + len(rows)]:
                del lines[n]
        self.written += stored
        return 200, {"stored": stored}, {}

    def late_ones(self) -> list[int]:
        """The requests kept to be done later, done now: what each would have answered."""
        with self.lock:
            done = [self.take(path, body, {})[0] for path, body in self.late]
            self.late.clear()
        return done


class _Quiet(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        pass  # a client killed while it was answered: nothing to say about it


class _Handler(BaseHTTPRequestHandler):
    fake: Fake

    def log_message(self, *_) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        self._serve(None)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        self._serve(body)

    def _serve(self, body) -> None:
        fake = self.fake
        url = urlsplit(self.path)
        parts = url.path.split("/")  # "", "v1", "s", space, rest...
        if fake.down:
            self._close()
            return
        if not fake.hold.is_set():
            fake.holding.set()
            fake.hold.wait()
            fake.holding.clear()
        with fake.lock:
            path = "/".join(parts[4:])
            fake.requests.append((path, body))
            if fake.answer:
                status = fake.answer.pop(0)
                fake.answers.append(status)
                self._send(status, {"error": "as asked"}, {})
                return
            if self.headers.get("Authorization") != f"Bearer {fake.key}":
                fake.answers.append(401)
                self._send(401, {"error": "This device is not known here."}, {})
                return
            if len(parts) < 5 or parts[:3] != ["", "v1", "s"] or parts[3] not in fake.spaces:
                fake.answers.append(404)
                self._send(404, {"error": "not here"}, {})
                return
            if fake.keep_late and body is not None:
                fake.keep_late -= 1
                fake.late.append((path, body))
                fake.answers.append(0)
                self._close()
                return
            status, answer, headers = fake.take(path, body, parse_qs(url.query))
            if fake.lose and body is not None:
                fake.lose -= 1
                fake.answers.append(0)
                self._close()
                return
            fake.answers.append(status)
        self._send(status, answer, headers)

    def _send(self, status: int, answer, headers: dict) -> None:
        data = json.dumps(answer).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.fake.unanswered += 1

    def _close(self) -> None:
        """Gone without a word: the connection closes and no answer comes."""
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass
