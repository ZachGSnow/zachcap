"""The engine's front door: each request is checked before FastAPI reads or routes it.

The engine listens on 127.0.0.1 only, but any program on this PC can reach that port, and a
web page can try to through DNS rebinding (a name of its own that it points at 127.0.0.1).
So every request must:

- name this PC as its host (127.0.0.1 or localhost), which a rebinding page can't;
- carry the token the shell gave the engine at launch, as a Bearer header, or as a ``token``
  query parameter for <audio> and <img> elements, which can't send headers. It's compared
  as bytes, so a token with odd characters in it is simply wrong (it used to be a 500);
- send a body of at most MAX_BODY, with its length given up front. The page sends small
  JSON (files are passed by path, never uploaded), so nothing bigger is read into memory.

A browser's CORS preflight carries no token by design: CORSMiddleware, which sits outside
this gate, answers it, and adds its headers to a refusal so the page can read it.
"""

from __future__ import annotations

import hmac
import json
from urllib.parse import parse_qs

MAX_BODY = 1024 * 1024  # 1 MB; the largest real request (Settings) is a few KB
HOSTS = {b"127.0.0.1", b"localhost"}


def host_allowed(host: bytes) -> bool:
    """127.0.0.1 or localhost, with or without a port."""
    name, _, port = host.rpartition(b":")
    if not name or not port.isdigit():
        name = host
    return name.lower() in HOSTS


def supplied_token(headers: dict[bytes, bytes], query_string: bytes) -> bytes:
    """The token a request carries: its Bearer header, else its ``token`` parameter."""
    authorization = headers.get(b"authorization", b"")
    if authorization[:7].lower() == b"bearer ":
        authorization = authorization[7:]
    token = authorization.strip()
    if not token:
        token = (parse_qs(query_string).get(b"token") or [b""])[0]
    return token


class Gate:
    """ASGI middleware that refuses a request before anything reads it (see the module)."""

    def __init__(self, app, token: str, max_body: int = MAX_BODY) -> None:
        self.app = app
        self.token = token.encode()
        self.max_body = max_body

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":  # lifespan: startup and shutdown
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if not host_allowed(headers.get(b"host", b"")):
            await refuse(send, 403, "This engine only answers requests to 127.0.0.1")
            return
        token = supplied_token(headers, scope.get("query_string", b""))
        if not hmac.compare_digest(token, self.token):
            await refuse(send, 401, "Missing or wrong token")
            return
        if b"transfer-encoding" in headers:
            await refuse(send, 411, "Send the body's length up front")
            return
        length = headers.get(b"content-length", b"0")
        if not length.isdigit():
            await refuse(send, 400, "Bad Content-Length")
            return
        if int(length) > self.max_body:
            await refuse(send, 413, "Request too large")
            return
        await self.app(scope, receive, send)


async def refuse(send, status: int, detail: str) -> None:
    """Answers with ``{"detail": ...}``, like FastAPI's own errors, without reading the body."""
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
