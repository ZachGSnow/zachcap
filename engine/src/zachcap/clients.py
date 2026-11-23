"""Clients: the companies the people you meet with work for.

A person's client is set on the People page. A meeting is for the client of the people named
in it when they all have the same one, or for the client set on the meeting itself, which
wins (a call with a new contact, or with people from two clients). The meeting list, the work
log, exports, Ask and the MCP server show it, and the meeting list, the work log, Ask and the
MCP server can be narrowed to one client.
"""

from __future__ import annotations

MAX_NAME = 80


def checked(name: str) -> str:
    """A client's name as typed, tidied ("" for none). Raises ValueError if it's too long."""
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise ValueError(f"A client's name is at most {MAX_NAME} characters")
    return name


def of_meetings(store, meetings: list) -> dict[int, dict]:
    """Each meeting's client: {"client": the name or None, "inferred": True when it comes
    from the people in it, "clients": the clients of the people named in it, most talkative
    first}."""
    heard = store.meeting_clients([m["id"] for m in meetings])
    found = {}
    for meeting in meetings:
        named = heard.get(meeting["id"], [])
        own = store.column(meeting, "client")  # absent in a library not yet opened to write
        if own:
            found[meeting["id"]] = {
                "client": own,
                "inferred": False,
                "clients": named,
            }
        else:
            one = named[0] if len(named) == 1 else None
            found[meeting["id"]] = {"client": one, "inferred": one is not None, "clients": named}
    return found


def of_meeting(store, meeting) -> dict:
    return of_meetings(store, [meeting])[meeting["id"]]


def known(store) -> list[str]:
    """Every client named so far, on people or meetings, in alphabetical order."""
    return store.client_names()


def matches(found: dict, client: str | None) -> bool:
    """Whether a meeting's client (from of_meetings) is ``client`` (any, when None)."""
    return client is None or (found["client"] or "").lower() == client.strip().lower()
