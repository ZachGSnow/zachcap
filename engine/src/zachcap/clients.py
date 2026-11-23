"""Clients: the companies the people you meet with work for.

A person's client is set on the People page. A meeting is for the client of the people named
in it when they all have the same one, or for the client set on the meeting itself, which
wins (a call with a new contact, or "no client" for a personal call). The meeting list, the
work log, exports, Ask and the MCP server show it, and the meeting list, the work log, Ask
and the MCP server can be narrowed to one client.

A meeting's client is history: when someone moves to another client (or is merged into
someone else, or removed), the meetings they were in keep the client they had. Client names
are matched without regard to case, and written the way they were first written.
"""

from __future__ import annotations

MAX_NAME = 80
YOURS, KEPT = "user", "auto"  # meetings.client_source: set by you; kept as it was


def checked(name: str) -> str:
    """A client's name as typed, tidied ("" for none). Raises ValueError if it's too long."""
    name = " ".join("".join(char for char in name if char.isprintable()).split())
    if len(name) > MAX_NAME:
        raise ValueError(f"A client's name is at most {MAX_NAME} characters")
    return name


def canonical(store, name: str) -> str:
    """The spelling already in use for this client, if there is one ("contoso" is written
    "Contoso" when that's how it was first written)."""
    wanted = name.casefold()
    return next((known for known in store.client_names() if known.casefold() == wanted), name)


def of_meetings(store, meetings: list) -> dict[int, dict]:
    """Each meeting's client: {"client": the name or None, "inferred": True when it comes
    from its people, "set": True when you set it (None then means "no client"), "clients":
    the clients of the people named in it, most talkative first}."""
    heard = store.meeting_clients()  # every meeting, worked out once per change
    found = {}
    for meeting in meetings:
        named = heard.get(meeting["id"], [])
        source = store.column(meeting, "client_source")  # absent in an older library
        own = store.column(meeting, "client")
        if source or own:
            found[meeting["id"]] = {
                "client": own,
                "inferred": source == KEPT,
                "set": source != KEPT,
                "clients": named,
            }
        else:
            one = named[0] if len(named) == 1 else None
            found[meeting["id"]] = {
                "client": one,
                "inferred": one is not None,
                "set": False,
                "clients": named,
            }
    return found


def of_meeting(store, meeting) -> dict:
    return of_meetings(store, [meeting])[meeting["id"]]


def known(store) -> list[str]:
    """Every client named so far, on people or meetings, in alphabetical order."""
    return store.client_names()


def matches(found: dict, client: str | None) -> bool:
    """Whether a meeting's client (from of_meetings) is ``client`` (any, when None or "")."""
    if client is None or not client.strip():
        return True
    return (found["client"] or "").casefold() == client.strip().casefold()


def keep_history(store, person_id: int) -> list[int]:
    """Before a person's client changes, or they're merged or removed: the meetings they were
    in that follow their people keep the client they have now. Returns those meetings."""
    meetings = [m for m in map(store.meeting, store.person_meetings(person_id)) if m is not None]
    kept = []
    for meeting_id, found in of_meetings(store, meetings).items():
        meeting = store.meeting(meeting_id)
        if store.column(meeting, "client_source") or store.column(meeting, "client"):
            continue  # set already: not theirs to change
        if found["client"]:
            store.update_meeting(meeting_id, client=found["client"], client_source=KEPT)
            kept.append(meeting_id)
    return kept
