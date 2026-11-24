"""A language model on this PC, through Ollama (ollama.com), which the user installs.

Nothing sent here leaves the PC: the address is this PC's own (127.0.0.1), no proxy is used
(http.client takes none from Windows or the environment), a redirect isn't followed, and a
model that Ollama runs in its cloud is refused before anything is sent to it.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import socket
import threading
from dataclasses import dataclass

HOST = "127.0.0.1"
PORT = 11434  # Ollama's own
QUICK = 4.0  # seconds for a question about models, all in all
# A model is loaded and reads the whole prompt before its first word, minutes on a CPU; then
# the words come one by one.
FIRST_WORD = 20 * 60.0
NEXT_WORD = 3 * 60.0
REPLY_MAX = 4_000_000  # bytes of one answer about models, and characters of one written
NOT_OLLAMA = "What answers on Ollama's port isn't Ollama."
# What Ollama and what it runs models with say of a text that is too long for the model. A
# "context deadline exceeded" is about time.
TOO_LONG = re.compile(
    r"(?is)(?:exceed|longer|larger|too long).*context (?:length|size|window)"
    r"|context (?:length|size|window).*exceed"
)


class NotRunning(RuntimeError):
    """There is no Ollama on this PC to talk to."""


class Failed(RuntimeError):
    """Ollama answered with an error, or with something that isn't an answer."""


class TooLong(Failed):
    """The text doesn't fit in the model's memory of one call."""


class NotHere(Failed):
    """The model isn't on this PC (any more), or Ollama runs it in its cloud."""


class Cancelled(RuntimeError):
    """The user stopped it."""


@dataclass(frozen=True)
class Model:
    name: str
    size: int  # bytes on disk, about what it takes of memory
    parameters: str  # "4.2B"
    context: int | None  # tokens it can read at most, when Ollama says
    thinks: bool  # it can think before it answers, which is turned off


@dataclass(frozen=True)
class Reply:
    text: str
    read: int  # tokens
    written: int  # tokens
    cut_off: bool  # it ran into the limit on what it may write


class Cancel:
    """Ends a call from another thread: its connection is closed, and Ollama stops writing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self.asked = False

    def cancel(self) -> None:
        with self._lock:
            self.asked = True
            if self._socket is not None:
                # Closed under the call that waits on it, which fails then (on Windows, shutting
                # it down doesn't end a wait). Detached first, so it's never closed twice.
                with contextlib.suppress(OSError):
                    socket.close(self._socket.detach())
                self._socket = None

    def check(self) -> None:
        if self.asked:
            raise Cancelled("Stopped.")

    @contextlib.contextmanager
    def watching(self, sock: socket.socket | None):
        with self._lock:
            self.check()
            self._socket = sock
        try:
            yield
        finally:
            with self._lock:
                self._socket = None


def port() -> int:
    """Ollama's port: its own, or the one in OLLAMA_HOST, which it reads too. An address there
    that isn't this PC's is not used: nothing goes to another computer."""
    here = r"(?:https?://)?(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1?\])?(?::(\d{1,5}))?/?"
    named = re.fullmatch(here, os.environ.get("OLLAMA_HOST", "").strip(), re.IGNORECASE)
    number = int(named[1]) if named and named[1] else PORT
    return number if 0 < number < 65536 else PORT


def in_cloud(entry: dict) -> bool:
    """Whether Ollama runs this model in its cloud: it says so, or the model's name does
    ("gpt-oss:120b-cloud", "gemma4:cloud")."""
    if entry.get("remote_host") or entry.get("remote_model"):
        return True
    name = str(entry.get("name") or entry.get("model") or "")
    return re.search(r"(?:^|[:-])cloud$", name, re.IGNORECASE) is not None


def _connection(timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(HOST, port(), timeout=timeout)


def _not_running(exc: BaseException) -> NotRunning:
    return NotRunning(f"Ollama isn't answering on this PC ({exc or type(exc).__name__}).")


def _reason(status: int, data: bytes) -> Failed:
    """What Ollama said went wrong."""
    try:
        said = json.loads(data).get("error")
    except (ValueError, AttributeError):
        said = None
    said = str(said or f"it answered {status}")[:500]
    kind = NotHere if status == 404 else TooLong if TOO_LONG.search(said) else Failed
    return kind(f"Ollama: {said}")


def _object(data: bytes) -> dict:
    """The JSON object an answer is."""
    try:
        answer = json.loads(data)
    except (ValueError, RecursionError):
        answer = None
    if not isinstance(answer, dict):
        raise Failed(NOT_OLLAMA)
    return answer


def _number(said) -> int:
    return said if isinstance(said, int) else 0


def _ask(method: str, path: str, body: dict | None = None) -> dict:
    connection = _connection(QUICK)
    # The timeout is for each read, and a byte at a time would go on past it: it's hung up then.
    late = Cancel()
    timer = threading.Timer(QUICK, late.cancel)
    timer.daemon = True
    timer.start()
    try:
        connection.connect()
        with late.watching(connection.sock):
            sent = json.dumps(body) if body is not None else None
            connection.request(method, path, sent, {"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read(REPLY_MAX)
    except (OSError, http.client.HTTPException, Cancelled) as exc:
        raise _not_running(TimeoutError("timed out") if late.asked else exc) from exc
    finally:
        timer.cancel()
        connection.close()
    if response.status != 200:
        raise _reason(response.status, data)
    return _object(data)


def models() -> list[Model]:
    """The models on this PC that can write, smallest first. Raises NotRunning."""
    answer = _ask("GET", "/api/tags")
    try:
        found = [_model(entry) for entry in answer.get("models") or []]
    except (AttributeError, TypeError) as exc:
        raise Failed(NOT_OLLAMA) from exc
    return sorted(filter(None, found), key=lambda model: (model.size, model.name))


def _model(entry: dict) -> Model | None:
    """A model as Ollama lists it, if it is on this PC and can write."""
    can = entry.get("capabilities")
    details = entry.get("details") or {}
    if in_cloud(entry) or (can is not None and "completion" not in can):
        return None
    if can is None and "bert" in str(details.get("family", "")):
        return None  # an older Ollama doesn't say what a model can do; these only embed
    return Model(
        name=str(entry.get("name") or entry.get("model")),
        size=_number(entry.get("size")),
        parameters=str(details.get("parameter_size") or ""),
        context=details.get("context_length") or None,
        thinks="thinking" in (can or ()),
    )


def model(name: str) -> Model:
    """A model that is on this PC, asked for by name. Refuses one that isn't, or that Ollama
    runs in its cloud."""
    known = {m.name: m for m in models()}
    found = known.get(name) or known.get(f"{name}:latest")
    if found is None:
        raise NotHere(f"Ollama doesn't have {name} on this PC. Choose another model in Settings.")
    if in_cloud({**_ask("POST", "/api/show", {"model": found.name}), "name": found.name}):
        raise NotHere(f"{name} runs in Ollama's cloud, not on this PC.")
    return found


def chat(
    on: Model,
    system: str,
    user: str,
    shape: dict | None,
    cancel: Cancel,
    context: int,
    most: int,
) -> Reply:
    """One answer: ``shape`` is the JSON schema it keeps to (None: plain text), ``context`` the
    tokens the model holds at once (the same for every call of a run, or it loads again),
    ``most`` the tokens it may write."""
    body: dict = {
        "model": on.name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        # Text that doesn't fit is an error, never cut off without a word.
        "truncate": False,
        "shift": False,
        "keep_alive": "5m",
        # The likeliest words, and no penalty for repeating names and field names.
        "options": {
            "num_ctx": context,
            "num_predict": most,
            "temperature": 0,
            "top_p": 1,
            "seed": 1,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "repeat_penalty": 1,
        },
    }
    if shape is not None:
        body["format"] = shape
    if on.thinks:
        body["think"] = False
    connection = _connection(QUICK)
    text: list[str] = []
    size = 0
    last: dict = {}
    try:
        connection.connect()
        sock = connection.sock
        sock.settimeout(FIRST_WORD)
        with cancel.watching(sock):
            sent = json.dumps(body)
            connection.request("POST", "/api/chat", sent, {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise _reason(response.status, response.read(REPLY_MAX))
            while line := response.readline(REPLY_MAX):
                sock.settimeout(NEXT_WORD)
                part = _object(line)
                if "error" in part:
                    raise _reason(200, line)
                if part.get("remote_host"):
                    raise NotHere(f"{on.name} runs in Ollama's cloud, not on this PC.")
                message = part.get("message") or {}
                said = (message.get("content") or "") if isinstance(message, dict) else None
                if not isinstance(said, str):
                    raise Failed(NOT_OLLAMA)
                text.append(said)
                size += len(said)
                last = part
                if size > REPLY_MAX:
                    raise Failed(f"{on.name} doesn't stop writing.")
    except TimeoutError as exc:
        raise Failed(f"{on.name} took too long to answer.") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        cancel.check()  # the connection was closed on purpose
        raise _not_running(exc) from exc
    finally:
        connection.close()
    cancel.check()
    if not last.get("done"):
        raise Failed(f"{on.name} stopped before it was done.")
    return Reply(
        text="".join(text).strip(),
        read=_number(last.get("prompt_eval_count")),
        written=_number(last.get("eval_count")),
        cut_off=last.get("done_reason") == "length",
    )


def unload(name: str) -> None:
    """Takes the model out of memory now (Ollama would in five minutes): a meeting to process
    may need it. Never raises."""
    with contextlib.suppress(RuntimeError):
        _ask("POST", "/api/generate", {"model": name, "keep_alive": 0})
