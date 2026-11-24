"""The model on this PC is asked through Ollama's server here and nowhere else. A stand-in server
answers in Ollama's shapes; nothing is downloaded, and no model runs."""

import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zachcap import ollama

WRITES = ["completion", "tools"]


class Server:
    """Stands in for Ollama: what it was asked, and what it answers."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict | None]] = []
        self.tags: list[dict] = []
        self.shown: dict = {}
        self.stream: list[dict] = []
        self.status = 200
        self.raw: bytes | None = None  # answered to everything, as it is
        self.headers: dict[str, str] = {}
        self.pause = 0.0  # before each line of a stream
        self.gone = threading.Event()  # the caller hung up while it wrote
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def answer(self, body: dict | None) -> None:
                outer.asked.append((self.path, body))
                if outer.raw is not None or outer.status != 200:
                    data = outer.raw if outer.raw is not None else b"{}"
                    self.send_response(outer.status)
                    for name, value in outer.headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/api/chat":
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for part in outer.stream:
                            time.sleep(outer.pause)
                            data = json.dumps(part).encode() + b"\n"
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                            self.wfile.flush()
                        self.wfile.write(b"0\r\n\r\n")
                    except OSError:
                        outer.gone.set()
                else:
                    answers = {"/api/tags": {"models": outer.tags}, "/api/show": outer.shown}
                    data = json.dumps(answers.get(self.path, {})).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            def do_GET(self) -> None:
                self.answer(None)

            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length") or 0)
                self.answer(json.loads(self.rfile.read(size)))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self) -> list[str]:
        return [path for path, _ in self.asked]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch):
    server = Server()
    monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{server.port}")
    server.tags = [entry("big:9b", size=9), entry("small:4b", size=4)]
    yield server
    server.close()


def entry(name: str, size: int = 4, **more) -> dict:
    details = {"parameter_size": "4.2B", "family": "qwen35", "context_length": 8192}
    known = {"name": name, "model": name, "size": size, "details": details}
    return {**known, "capabilities": WRITES, **more}


SMALL = ollama.Model("small:4b", 4, "4.2B", 8192, thinks=False)


def ask(on: ollama.Model = SMALL, shape: dict | None = None, cancel=None) -> ollama.Reply:
    return ollama.chat(on, "Instructions.", "Text.", shape, cancel or ollama.Cancel(), 8192, 3000)


def wrote(text: str, **more) -> dict:
    return {"model": "small:4b", "message": {"role": "assistant", "content": text}, **more}


def done(reason: str = "stop") -> dict:
    return wrote("", done=True, done_reason=reason, prompt_eval_count=120, eval_count=30)


def test_only_models_on_this_pc_that_write_are_listed(server):
    server.tags += [
        entry("gpt-oss:120b-cloud", size=1),
        entry("gemma4:cloud", size=1),
        entry("mine", size=1, remote_host="https://ollama.com:443", remote_model="gpt-oss:120b"),
        entry("embeddinggemma", size=1, capabilities=["embedding"]),
        entry("old-embedder", size=1, capabilities=None, details={"family": "nomic-bert"}),
        entry("old-writer", size=2, capabilities=None, details={"family": "llama"}),
        entry("thinker", size=3, capabilities=[*WRITES, "thinking"]),
    ]
    found = ollama.models()
    assert [m.name for m in found] == ["old-writer", "thinker", "small:4b", "big:9b"]
    assert [m.thinks for m in found] == [False, True, False, False]
    assert found[2] == SMALL


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("", 11434),
        ("127.0.0.1:11555", 11555),
        ("http://localhost:11555/", 11555),
        ("0.0.0.0", 11434),  # Ollama listens everywhere; it's still asked here
        ("0.0.0.0:11555", 11555),
        (":11555", 11555),
        ("[::1]:11555", 11555),
        ("192.168.1.20:11555", 11434),  # another computer's: not used
        ("http://models.example.com:80", 11434),
        ("https://127.0.0.1.example.com:80", 11434),
        ("127.0.0.1:99999", 11434),
        ("127.0.0.1:0", 11434),
    ],
)
def test_only_this_pcs_address_is_asked(monkeypatch, host, port):
    monkeypatch.setenv("OLLAMA_HOST", host)
    assert ollama.port() == port
    assert ollama._connection(1.0).host == "127.0.0.1"


def test_a_model_is_asked_for_by_name(server):
    server.tags.append(entry("mine:latest"))
    assert ollama.model("small:4b") == SMALL
    assert ollama.model("mine").name == "mine:latest"
    with pytest.raises(ollama.NotHere, match="doesn't have gone:7b on this PC"):
        ollama.model("gone:7b")


@pytest.mark.parametrize("says", [{"remote_host": "https://ollama.com"}, {"remote_model": "x"}])
def test_a_model_in_ollamas_cloud_is_refused_before_anything_is_sent(server, says):
    server.shown = says
    with pytest.raises(ollama.NotHere, match="runs in Ollama's cloud"):
        ollama.model("small:4b")
    assert server.paths() == ["/api/tags", "/api/show"]


def test_an_answer_comes_in_pieces(server):
    server.stream = [wrote('{"a"'), wrote(": 1}"), done()]
    shape = {"type": "object"}

    reply = ask(shape=shape)

    assert reply == ollama.Reply('{"a": 1}', read=120, written=30, cut_off=False)
    [(path, body)] = server.asked
    assert path == "/api/chat" and body["model"] == "small:4b" and body["format"] == shape
    assert body["messages"] == [
        {"role": "system", "content": "Instructions."},
        {"role": "user", "content": "Text."},
    ]
    # Too long is an error, never text cut off without a word; the same memory for every call.
    assert (body["truncate"], body["shift"]) == (False, False)
    assert (body["options"]["num_ctx"], body["options"]["num_predict"]) == (8192, 3000)
    assert body["options"]["temperature"] == 0 and "think" not in body


def test_a_model_that_thinks_is_asked_not_to(server):
    server.stream = [wrote("Hi."), done("length")]
    assert ask(ollama.Model("small:4b", 4, "4.2B", 8192, thinks=True)).cut_off
    body = server.asked[0][1]
    assert body["think"] is False and "format" not in body


@pytest.mark.parametrize(
    ("said", "kind", "match"),
    [
        (b'{"error": "model requires more system memory (5 GiB)"}', ollama.Failed, "more system"),
        (
            b'{"error": "the input length exceeds the context length"}',
            ollama.TooLong,
            "exceeds the context",
        ),
        (b"<html>", ollama.Failed, "answered 500"),
    ],
)
def test_what_went_wrong_is_said(server, said, kind, match):
    server.status, server.raw = 500, said
    with pytest.raises(kind, match=match):
        ask()


@pytest.mark.parametrize(
    ("said", "too_long"),
    [
        ("the input length exceeds the context length", True),
        ("the input length (9418 tokens) exceeds the context length (8192)", True),
        ("input length (9 tokens) exceeds the model's maximum context length (8 tokens)", True),
        ("request (9418 tokens) exceeds the available context size (8192 tokens)", True),
        ("the prompt is longer than the context length currently available", True),
        ("The prompt is too long: 9418, model maximum context length: 8192", True),
        ("input (9418 tokens) is larger than the max context size (8192 tokens)", True),
        ("Context size has been exceeded.", True),
        ("timed out waiting for the runner to start: context deadline exceeded", False),
        ("context canceled", False),
        ("requested context size too large for model", False),
        ("server busy, please try again.  maximum pending requests exceeded", False),
        ("model requires more system memory (8 GiB) than is available (5 GiB)", False),
    ],
)
def test_only_a_text_that_is_too_long_is_said_to_be(server, said, too_long):
    server.status, server.raw = 500, json.dumps({"error": said}).encode()
    with pytest.raises(ollama.Failed) as failed:
        ask()
    assert isinstance(failed.value, ollama.TooLong) is too_long
    assert str(failed.value) == f"Ollama: {said}"
    server.status, server.raw = 200, None
    server.stream = [wrote("One "), {"error": said}]
    with pytest.raises(ollama.Failed) as failed:
        ask()
    assert isinstance(failed.value, ollama.TooLong) is too_long


def test_a_model_that_is_gone_is_said(server):
    server.status, server.raw = 404, b'{"error": "model \'small:4b\' not found"}'
    with pytest.raises(ollama.NotHere, match="model 'small:4b' not found"):
        ask()


@pytest.mark.parametrize(
    "line",
    [
        ["Hi."],
        "Hi.",
        5,
        None,
        {"message": "Hi."},
        {"message": ["Hi."]},
        {"message": {"content": 5}},
        {"message": {"content": ["Hi."]}},
    ],
)
def test_a_line_out_of_shape_is_said(server, line):
    server.stream = [wrote("One "), line, done()]
    with pytest.raises(ollama.Failed, match="isn't Ollama"):
        ask()


def test_a_line_that_isnt_json_is_said(server):
    for raw in (b"Hi.", b"\x1f\x8b\x08\n", b"[" * 3000, b'{"message": {"content": "Hi.'):
        server.raw = raw
        with pytest.raises(ollama.Failed, match="isn't Ollama"):
            ask()


def test_counts_out_of_shape_are_none(server):
    server.stream = [{**wrote("Hi.", done=True), "prompt_eval_count": "many", "eval_count": [1]}]
    assert ask() == ollama.Reply("Hi.", read=0, written=0, cut_off=False)
    server.tags = [entry("small:4b", size="4 GB")]
    assert [(m.name, m.size) for m in ollama.models()] == [("small:4b", 0)]


@pytest.mark.parametrize("listed", [{"a": 1}, ["small:4b"], [{"name": "a", "details": "b"}], 5])
def test_a_list_out_of_shape_is_said(server, listed):
    server.raw = json.dumps({"models": listed}).encode()
    with pytest.raises(ollama.Failed, match="isn't Ollama"):
        ollama.models()


def test_an_error_in_the_middle_is_said(server):
    server.stream = [wrote("One "), {"error": "the runner stopped"}]
    with pytest.raises(ollama.Failed, match="the runner stopped"):
        ask()
    server.stream = [wrote("One ")]  # and no end
    with pytest.raises(ollama.Failed, match="stopped before it was done"):
        ask()


def test_an_answer_from_the_cloud_is_thrown_away(server):
    server.stream = [wrote("One ", remote_host="https://ollama.com"), done()]
    with pytest.raises(ollama.NotHere, match="runs in Ollama's cloud"):
        ask()


def test_a_redirect_is_not_followed(server):
    other = Server()
    server.status = 302
    server.headers = {"Location": f"http://127.0.0.1:{other.port}/api/chat"}
    with pytest.raises(ollama.Failed, match="answered 302"):
        ask()
    with pytest.raises(ollama.Failed):
        ollama.models()
    assert other.asked == []
    other.close()


def test_no_proxy_is_used(server, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing answers there
    server.stream = [wrote("Hi."), done()]
    assert ask().text == "Hi." and len(ollama.models()) == 2


def test_no_ollama_is_said(server):
    server.close()
    with pytest.raises(ollama.NotRunning, match="isn't answering on this PC"):
        ollama.models()
    with pytest.raises(ollama.NotRunning):
        ask()
    ollama.unload("small:4b")  # never a reason to fail


def test_something_else_on_its_port_is_said(server):
    server.raw = b"<html>A router</html>"
    with pytest.raises(ollama.Failed, match="isn't Ollama"):
        ollama.models()


@pytest.mark.parametrize("start", [b"", b"HTTP/1.1 200 OK\r\nContent-Length: 600\r\n\r\n"])
def test_a_server_that_answers_a_byte_at_a_time_is_given_up_on(monkeypatch, start):
    monkeypatch.setattr(ollama, "QUICK", 0.5)
    with socket.create_server(("127.0.0.1", 0)) as listening:
        monkeypatch.setenv("OLLAMA_HOST", f"127.0.0.1:{listening.getsockname()[1]}")

        def drip() -> None:
            connection, _ = listening.accept()
            with connection, contextlib.suppress(OSError):
                connection.sendall(start)
                for _ in range(60):
                    time.sleep(0.1)  # well within the time one read may take
                    connection.sendall(b"H")

        dripping = threading.Thread(target=drip, daemon=True)
        dripping.start()
        started = time.monotonic()
        with pytest.raises(ollama.NotRunning, match="timed out"):
            ollama.model("small:4b")
        assert time.monotonic() - started < 3
        dripping.join(10)  # it was hung up on
        assert time.monotonic() - started < 5


def test_it_is_stopped_while_it_writes(server):
    server.stream = [wrote("word ")] * 200
    server.pause = 0.05
    cancel = ollama.Cancel()
    threading.Timer(0.4, cancel.cancel).start()

    started = time.monotonic()
    with pytest.raises(ollama.Cancelled):
        ask(cancel=cancel)

    assert time.monotonic() - started < 5
    assert server.gone.wait(5)  # it hung up, so Ollama stops
    with pytest.raises(ollama.Cancelled):  # and nothing more is asked
        ask(cancel=cancel)
    assert len(server.asked) == 1


def test_it_is_stopped_before_its_first_word(server):
    server.stream = [wrote("word ")]
    server.pause = 3.0
    cancel = ollama.Cancel()
    threading.Timer(0.3, cancel.cancel).start()
    started = time.monotonic()
    with pytest.raises(ollama.Cancelled):
        ask(cancel=cancel)
    assert time.monotonic() - started < 2.5


def test_it_doesnt_wait_forever(server, monkeypatch):
    server.stream = [wrote("word "), wrote("word "), done()]
    server.pause = 1.0
    monkeypatch.setattr(ollama, "FIRST_WORD", 0.2)
    with pytest.raises(ollama.Failed, match="took too long"):
        ask()
    monkeypatch.setattr(ollama, "FIRST_WORD", 5.0)
    monkeypatch.setattr(ollama, "NEXT_WORD", 0.2)
    with pytest.raises(ollama.Failed, match="took too long"):
        ask()


def test_it_doesnt_read_forever(server, monkeypatch):
    monkeypatch.setattr(ollama, "REPLY_MAX", 300)
    server.stream = [wrote("word " * 20)] * 10
    with pytest.raises(ollama.Failed, match="doesn't stop writing"):
        ask()


def test_the_model_leaves_memory_when_told(server):
    ollama.unload("small:4b")
    assert server.asked == [("/api/generate", {"model": "small:4b", "keep_alive": 0})]
