"""Files attached to a meeting: read as text, kept as a note, and given to Claude labelled."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import attachments, export, summarize, tickets
from zachcap.api import create_app
from zachcap.store import Store

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}


def test_text_files_in_their_encodings(tmp_path: Path):
    plain = tmp_path / "error.log"
    plain.write_bytes(b"Event 7031: The Print Spooler service terminated.\r\n\r\n\r\n\r\nRetried.")
    assert attachments.read(plain) == (
        "error.log",
        "Event 7031: The Print Spooler service terminated.\n\nRetried.",
    )
    wide = tmp_path / "export.txt"
    wide.write_bytes("Gateway down".encode("utf-16"))  # with its byte-order mark
    assert attachments.read(wide)[1] == "Gateway down"
    legacy = tmp_path / "notes.txt"
    legacy.write_bytes("Caf\xe9 printer".encode("cp1252"))
    assert attachments.read(legacy)[1] == "Café printer"
    page = tmp_path / "status.html"
    page.write_text("<html><style>p{}</style><p>All <b>good</b></p><script>x()</script></html>")
    assert attachments.read(page)[1] == "All  good"


def test_an_exported_email_reads_as_who_when_and_what(tmp_path: Path):
    message = tmp_path / "thread.eml"
    message.write_bytes(
        b"From: Dana <dana@example.com>\r\nTo: You <you@example.com>\r\n"
        b"Subject: RE: VPN drops\r\nDate: Mon, 23 Nov 2026 09:00:00 -0500\r\n"
        b"Content-Type: text/plain\r\n\r\nIt drops every hour since Friday.\r\n"
    )
    name, text = attachments.read(message)
    assert text.startswith("From: Dana <dana@example.com>\nTo: You <you@example.com>")
    assert "Subject: RE: VPN drops" in text and text.endswith("It drops every hour since Friday.")


def test_files_that_cant_be_attached_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    picture = tmp_path / "screen.png"
    picture.write_bytes(b"\x89PNG")
    with pytest.raises(attachments.NotText, match="isn't a text file"):
        attachments.read(picture)
    binary = tmp_path / "dump.log"
    binary.write_bytes(b"MZ\x00\x00\x00binary")
    with pytest.raises(attachments.NotText, match="doesn't hold text"):
        attachments.read(binary)
    with pytest.raises(attachments.NotText, match="Couldn't read"):
        attachments.read(tmp_path / "missing.txt")
    monkeypatch.setattr(attachments, "MAX_BYTES", 10)
    big = tmp_path / "big.txt"
    big.write_text("x" * 11)
    with pytest.raises(attachments.NotText, match="too big"):
        attachments.read(big)


def test_a_long_file_is_cut_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(attachments, "MAX_TEXT", 20)
    long = tmp_path / "long.txt"
    long.write_text("a" * 50)
    assert attachments.read(long)[1] == "a" * 20 + "\n[... 30 more characters left out]"


def test_attached_files_go_to_claude_labelled_and_offer_tickets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    client = TestClient(create_app("test-token"), base_url=LOCAL)
    store = Store()
    store.set_setting(tickets.SETTING, "#######")
    meeting_id = store.create_meeting("Printer down", "2026-11-23T09:00:00", tmp_path / "m")
    log = tmp_path / "spooler.log"
    log.write_text("Ticket 1924086\nPID 7654321 crashed")

    note = client.post(f"/api/meetings/{meeting_id}/attach", headers=AUTH, json={"path": str(log)})
    assert note.status_code == 200 and note.json()["kind"] == "file"
    assert note.json()["text"].startswith("spooler.log\nTicket 1924086")
    refused = client.post(
        f"/api/meetings/{meeting_id}/attach", headers=AUTH, json={"path": "x.png"}
    )
    assert refused.status_code == 400

    text = summarize.notes_text(store, meeting_id)
    assert "Files You attached to the meeting" in text and "--- spooler.log ---" in text
    assert "Notes You took" not in text  # a file isn't one of your notes
    found = tickets.candidates(store, meeting_id)
    assert {c["ticket"] for c in found} == {"1924086", "7654321"}
    assert all(c["where"] == ["files"] for c in found)
    assert store.meeting(meeting_id)["ticket"] is None  # offered, never filled in from a file
    assert "Attached file: spooler.log (2 lines)" in export.markdown(store, meeting_id)

    monkeypatch.setattr(attachments, "MAX_FILES", 1)
    full = client.post(f"/api/meetings/{meeting_id}/attach", headers=AUTH, json={"path": str(log)})
    assert full.status_code == 400 and "at most 1" in full.json()["detail"]
    store.close()
