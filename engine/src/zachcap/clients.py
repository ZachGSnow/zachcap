"""Clients: the companies the people you meet with work for.

A person's client is set on the People page; you never have one, and never count. A meeting
is for the client of the people named in it when they all have the same one, or for the
client set on the meeting itself, which wins (a call with a new contact, or "no client" for a
personal call) until the meeting is set to follow its people again. The meeting list, the
work log, exports, Ask and the MCP server show it, and the meeting list, the work log, Ask
and the MCP server can be narrowed to one client.

A meeting's client is history: when someone who counts in it (named, or recognized by voice;
not only suggested) moves to another client, is merged into someone else or removed, the
meeting keeps the client it had, until it's processed again. Client names are matched
without regard to case, and written the way most use them; changing only the capitals of a
client's name renames it everywhere.
"""

from __future__ import annotations

MAX_NAME = 80
YOURS, KEPT = "user", "auto"  # meetings.client_source: set by you; kept as it was


def checked(name: str) -> str:
    """A client's name as typed, tidied ("" for none): any run of spaces, tabs or other
    blanks is one space, and characters that don't print are dropped. Raises ValueError if
    it's too long."""
    shown = "".join(
        " " if char.isspace() else char for char in name if char.isprintable() or char.isspace()
    )
    name = " ".join(shown.split())
    if len(name) > MAX_NAME:
        raise ValueError(f"A client's name is at most {MAX_NAME} characters")
    return name


def canonical(store, name: str) -> str:
    """The spelling already in use for this client, if there is one ("contoso" is written
    "Contoso" when that's how most write it)."""
    wanted = name.casefold()
    return next((known for known in store.client_names() if known.casefold() == wanted), name)


def recased(before: str | None, after: str | None) -> bool:
    """Whether ``after`` is ``before`` with other capitals ("contoso" and "Contoso")."""
    return bool(before and after) and before != after and before.casefold() == after.casefold()


def of_meetings(store, meetings: list) -> dict[int, dict]:
    """Each meeting's client: {"client": the name or None, "inferred": True when it comes
    from its people as they are now, "kept": True when it was kept as it was when one of
    them moved on, "set": True when you set it (None then means "no client"), "clients": the
    clients of the people named in it now, most talkative first}."""
    # Worked out for every meeting once per change; only these ones are read.
    heard = store.meeting_clients([meeting["id"] for meeting in meetings])
    found = {}
    for meeting in meetings:
        named = heard.get(meeting["id"], [])
        source = store.column(meeting, "client_source")  # absent in an older library
        own = store.column(meeting, "client")
        if source or own:
            found[meeting["id"]] = {
                "client": own,
                "inferred": False,
                "kept": source == KEPT,
                "set": source != KEPT,
                "clients": named,
            }
        else:
            one = named[0] if len(named) == 1 else None
            found[meeting["id"]] = {
                "client": one,
                "inferred": one is not None,
                "kept": False,
                "set": False,
                "clients": named,
            }
    return found


def of_meeting(store, meeting) -> dict:
    return of_meetings(store, [meeting])[meeting["id"]]


def fields(found: dict) -> dict:
    """A meeting's client (from of_meetings) as the page gets it with the meeting."""
    return {
        "client": found["client"],
        "client_inferred": found["inferred"],
        "client_kept": found["kept"],
        "client_set": found["set"],
    }


def known(store) -> list[str]:
    """Every client named so far, on people (never you) or meetings, in alphabetical order."""
    return store.client_names()


def matches(found: dict, client: str | None) -> bool:
    """Whether a meeting's client (from of_meetings) is ``client`` (any, when None or "")."""
    if client is None or not client.strip():
        return True
    return (found["client"] or "").casefold() == client.strip().casefold()


def keep_history(store, person_id: int) -> list[int]:
    """Before a person's client changes, or they're merged or removed: the meetings they
    count in (named or recognized by voice, not only suggested) that follow their people
    keep the client they have now, all in one transaction. You never count, so nothing of
    yours is kept. Returns those meetings."""
    person = store.person(person_id)
    if person is None or person["is_me"]:
        return []
    meetings = store.meetings_counting(person_id)
    kept = {
        meeting_id: found["client"]
        for meeting_id, found in of_meetings(store, meetings).items()
        if found["client"]
    }
    store.keep_clients(kept)
    return list(kept)


def before_merge(store, person_id: int, into: int) -> None:
    """Before one person is merged into another (``into``, who stays): the client goes with
    them to someone who had none, and the meetings either one counts in keep the client they
    have now when what that person counts for changes. You never have a client: merged into
    you, or you into them, the other one stops counting, and has none."""
    merged, kept = store.person(person_id), store.person(into)
    if merged is None or kept is None:
        return
    after = None if merged["is_me"] or kept["is_me"] else (kept["org"] or merged["org"] or None)
    for person in (merged, kept):
        before = None if person["is_me"] else person["org"]
        if (before or "").casefold() != (after or "").casefold():
            keep_history(store, person["id"])
    if (kept["org"] or None) != after:
        store.set_client(into, after)


def rename(store, name: str, spelling: str) -> list[int]:
    """Writes a client's name another way (other capitals: "contoso" -> "Contoso") on every
    person and meeting that has it. Returns the meetings that show it."""
    store.rename_client(name, spelling)
    return [
        meeting_id
        for meeting_id, found in of_meetings(store, store.meetings()).items()
        if matches(found, spelling)
    ]
