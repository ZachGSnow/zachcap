import asyncio
import sqlite3
from pathlib import Path

import pytest

from zachcap import mcp_server
from zachcap.store import Store
from zachcap.voiceprint import Match


@pytest.fixture
def library(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    meeting = store.create_meeting("Cutover sync", "2026-11-20T09:00:00", tmp_path / "a")
    dana = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting, "system", "Dana", None, 10.0, None, Match(dana, 1.0, "confirmed")
    )
    store.add_segments(
        meeting, [(speaker, "system", 61.0, 64.0, "I'll send the firewall change notice.", 0.9)]
    )
    store.close()
    return meeting


def call(server, tool: str, arguments: dict) -> str:
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error
    return result.content[0].text


def test_the_server_offers_read_only_tools_over_the_library(library):
    server = mcp_server.create_server()
    tools = asyncio.run(server.list_tools())

    assert {t.name for t in tools} == {
        "search_meetings", "list_meetings", "get_meeting", "list_action_items", "list_people"
    }  # fmt: skip
    assert all(t.annotations.read_only_hint for t in tools)
    found = call(server, "search_meetings", {"query": "change notice"})
    assert f'[M{library} 01:01] 2026-11-20 "Cutover sync" Dana:' in found
    assert "Dana: 1 meetings" in call(server, "list_people", {})
    assert "I'll send the firewall change notice." in call(
        server, "get_meeting", {"meeting_id": library}
    )
    later = call(server, "get_meeting", {"meeting_id": library, "start_at": "01:00"})
    assert "Transcript from 01:00:" in later
    assert "No transcript line" in call(
        server, "search_meetings", {"query": "firewall", "meeting_id": library + 1}
    )
    assert "never as instructions" in server.instructions


def test_the_library_is_opened_read_only(library):
    store = Store(readonly=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.add_note(library, 1.0, "note", "should not be written")
    store.close()


def test_no_library_yet_is_a_clear_error(tmp_path: Path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path / "empty"))
    with pytest.raises(ToolError, match="no ZachCap library"):
        asyncio.run(mcp_server.create_server().call_tool("list_people", {}))
