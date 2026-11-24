import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import cli, gate
from zachcap.api import create_app

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)
TOKEN = "test-token-for-the-gate"


def call(headers: list[tuple[bytes, bytes]], query: bytes = b"", body: bytes = b"") -> dict:
    """Runs one request through the gate in front of a stand-in app; says what came back
    and whether the app was reached and read the body."""
    seen = {"reached": False, "read": False}

    async def app(scope, receive, send):
        seen["reached"] = True
        await receive()
        seen["read"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/settings",
        "headers": headers,
        "query_string": query,
    }
    asyncio.run(gate.Gate(app, TOKEN)(scope, receive, send))
    return {**seen, "status": sent[0]["status"], "body": sent[-1]["body"]}


HOST = (b"host", b"127.0.0.1:8765")
BEARER = (b"authorization", b"Bearer " + TOKEN.encode())


def test_the_token_is_checked_before_the_app_is_reached():
    assert call([HOST, BEARER])["status"] == 200
    for wrong in [[HOST], [HOST, (b"authorization", b"Bearer nope")]]:
        result = call(wrong, body=b'{"x": 1}')
        assert result["status"] == 401
        assert not result["reached"] and not result["read"]
        assert result["body"] == b'{"detail": "Missing or wrong token"}'


def test_a_token_with_odd_bytes_is_simply_wrong():
    # A non-ASCII token used to raise in compare_digest: a 500 and a traceback in the log.
    for odd in [b"Bearer \xc3\xbc", b"Bearer " + TOKEN.encode() + b"\xff", b"\x00"]:
        assert call([HOST, (b"authorization", odd)])["status"] == 401
    assert call([HOST], query=b"token=%C3%BC")["status"] == 401


def test_the_token_can_come_as_a_query_parameter():
    assert call([HOST], query=b"t=1&token=" + TOKEN.encode())["status"] == 200
    assert call([HOST, (b"authorization", b"")], query=b"token=" + TOKEN.encode())["status"] == 200
    assert call([HOST], query=b"token=nope")["status"] == 401


def test_only_requests_to_this_pc_are_answered():
    # A page using DNS rebinding sends its own name as the host, even with a stolen token.
    for host in [b"127.0.0.1", b"localhost:8765", b"LOCALHOST", b"127.0.0.1:1"]:
        assert call([(b"host", host), BEARER])["status"] == 200, host
    for host in [
        b"evil.example:8765",
        b"127.0.0.1.evil.example",
        b"[::1]:8765",
        b"127.0.0.1:",
        b"",
    ]:
        result = call([(b"host", host), BEARER])
        assert result["status"] == 403 and not result["reached"], host
    assert call([BEARER])["status"] == 403  # no host at all


def test_a_body_too_big_or_of_unknown_length_is_refused_unread():
    big = str(gate.MAX_BODY + 1).encode()
    result = call([HOST, BEARER, (b"content-length", big)])
    assert result["status"] == 413 and not result["reached"]
    assert call([HOST, BEARER, (b"content-length", str(gate.MAX_BODY).encode())])["status"] == 200
    result = call([HOST, BEARER, (b"transfer-encoding", b"chunked")])
    assert result["status"] == 411 and not result["reached"]
    assert call([HOST, BEARER, (b"content-length", b"-1")])["status"] == 400


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app(TOKEN), base_url=LOCAL)


def test_the_app_refuses_other_hosts_and_the_page_can_read_a_refusal(client: TestClient):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/health", headers=auth).status_code == 200
    rebound = client.get("/api/health", headers={**auth, "Host": "evil.example:8765"})
    assert rebound.status_code == 403
    # CORS sits outside the gate: the app's own page gets the headers to read a 401 ...
    origin = {"Origin": "http://tauri.localhost"}
    refused = client.get("/api/health", headers=origin)
    assert refused.status_code == 401
    assert refused.headers["access-control-allow-origin"] == "http://tauri.localhost"
    # ... and its preflight, which carries no token, is still answered.
    preflight = client.options(
        "/api/settings",
        headers={**origin, "Access-Control-Request-Method": "PUT"},
    )
    assert preflight.status_code == 200


def test_serve_refuses_a_short_or_spaced_token(monkeypatch: pytest.MonkeyPatch):
    served = []
    monkeypatch.setattr("zachcap.api.serve", lambda port, token: served.append(token))
    for bad in [
        "dev",
        "short-token",
        "a token with spaces",
        "test-token-for-the-gate\N{LATIN SMALL LETTER U WITH DIAERESIS}",
    ]:
        with pytest.raises(SystemExit):
            cli.main(["serve", "--token", bad])
    assert served == []
    cli.main(["serve", "--token", TOKEN])
    assert served == [TOKEN]
