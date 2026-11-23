"""ZachCap's meetings for Claude Desktop, Claude Code and other MCP clients.

A read-only server over the local library (SQLite opened read-only), started by the client
over stdio: `zachcap mcp`. It runs only if you add it to a client (Settings shows how), and
whatever the tools return goes to that client and its model, like any other context.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__, assistant, paths
from .store import Store

INSTRUCTIONS = """Meetings recorded with ZachCap, a local meeting recorder: transcripts with \
named speakers, Claude summaries, action items and commitments, and the user's own notes. \
Lines are cited as [M<meeting id> <mm:ss>]; keep those citations in answers so the user can \
find the moment in ZachCap."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


@contextmanager
def library() -> Iterator[Store]:
    if not paths.database().exists():
        # A ToolError reaches the client as a message; other errors are hidden as crashes.
        raise ToolError("There's no ZachCap library on this computer yet.")
    store = Store(readonly=True)
    try:
        yield store
    finally:
        store.close()


def create_server() -> MCPServer:
    server = MCPServer(name="ZachCap", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    def search_meetings(
        query: Annotated[str, Field(description='Words to look for, like "firewall cutover"')],
        limit: Annotated[int, Field(description="How many lines, at most 60", ge=1, le=60)] = 25,
    ) -> str:
        """Full-text search over every line of every meeting transcript, best matches first."""
        with library() as store:
            return assistant.search(store, query, limit)

    @server.tool(annotations=READ_ONLY)
    def list_meetings(
        since: Annotated[str | None, Field(description="On or after, YYYY-MM-DD")] = None,
        until: Annotated[str | None, Field(description="On or before, YYYY-MM-DD")] = None,
        person: Annotated[
            str | None, Field(description="Only meetings this person spoke in")
        ] = None,
    ) -> str:
        """Meetings, newest first, with date, length, who spoke and the summary's overview."""
        with library() as store:
            return assistant.meetings_list(store, since, until, person)

    @server.tool(annotations=READ_ONLY)
    def get_meeting(
        meeting_id: Annotated[int, Field(description="The meeting's id, the number after M")],
    ) -> str:
        """One meeting in full: summary, action items, the user's notes and the transcript."""
        with library() as store:
            return assistant.meeting_detail(store, meeting_id)

    @server.tool(annotations=READ_ONLY)
    def list_action_items(
        status: Annotated[str, Field(description='"open", "done" or "all"')] = "open",
        owner: Annotated[str | None, Field(description="Only items this person owns")] = None,
    ) -> str:
        """Action items and commitments from every summarized meeting, with where they were said."""
        with library() as store:
            return assistant.action_items_list(store, status, owner)

    @server.tool(annotations=READ_ONLY)
    def list_people() -> str:
        """Everyone ZachCap knows by voice, with how many meetings and how long they talked."""
        with library() as store:
            return assistant.people_list(store)

    return server


def run() -> None:
    create_server().run("stdio")
