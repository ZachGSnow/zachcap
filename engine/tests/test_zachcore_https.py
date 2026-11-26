"""ZachCore over https: the certificate and the server's name are checked against what Windows
trusts, before anything is sent. A real check of good and bad certificates was made by hand
(example.com, and badssl.com's self-signed, expired, wrong-name and untrusted-root hosts);
these tests hold the settings that made it pass, with no network."""

import http.client
import ssl

import pytest

from zachcap import sender

WHERE = sender.Place("https://notes.example.com", "work")


def opened(monkeypatch: pytest.MonkeyPatch, fails: BaseException) -> list[dict]:
    """Stands in for the HTTPS connection: keeps how it was made, then fails as told."""
    made: list[dict] = []

    class Connection:
        def __init__(self, host, port=None, **kwargs):
            made.append({"host": host, "port": port, **kwargs})

        def request(self, *args, **kwargs):
            raise fails

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return made


def untrusted(message: str) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] {message}")
    error.verify_message = message
    error.verify_code = 18
    return error


def test_the_certificate_and_the_name_are_checked(monkeypatch: pytest.MonkeyPatch):
    made = opened(monkeypatch, ConnectionRefusedError())
    assert sender.request(WHERE, "the-key", "in/state").status is None
    (connection,) = made
    assert connection["host"] == "notes.example.com"
    context = connection["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_a_certificate_that_isnt_trusted_is_said_so(monkeypatch: pytest.MonkeyPatch):
    opened(monkeypatch, untrusted("self-signed certificate"))
    answer = sender.request(WHERE, "the-key", "in/state")
    assert answer.status is None and answer.untrusted == "self-signed certificate"
    checked = sender.check(WHERE, "the-key")
    assert checked["ok"] is False
    assert checked["error"] == (
        "That address's certificate isn't trusted on this PC (self-signed certificate)"
    )
    assert "the-key" not in repr(checked)


def test_no_answer_is_still_no_answer(monkeypatch: pytest.MonkeyPatch):
    opened(monkeypatch, ConnectionRefusedError())
    assert sender.check(WHERE, "the-key")["error"] == "No answer from that address"
    opened(monkeypatch, http.client.RemoteDisconnected("closed"))
    assert sender.request(WHERE, "the-key", "in/state").untrusted is None
