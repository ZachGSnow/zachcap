"""The prompt library: kinds of meeting (what a summary focuses on) and one-click documents.

Both come with built-in ones, which can be edited and reset but not deleted, and the user
can add their own. Only the instructions are the user's: the summary's sections and its
JSON shape stay fixed in summarize.py, so no edit can break how a summary is read back.
A built-in that was never edited follows this file, so better wording in a new version of
ZachCap reaches it; an edited one keeps the user's text until they reset it.
"""

from __future__ import annotations

import secrets
from typing import Literal

from .store import Store

Kind = Literal["template", "recipe"]
KINDS: tuple[Kind, ...] = ("template", "recipe")

LABEL_MAX = 40
INSTRUCTIONS_MAX = 2000
# Plenty for anyone's own formats, and it keeps the pickers usable.
CUSTOM_MAX = 30

# Kinds of meeting. The summary keeps the same fields, so the action item tracker works
# for all of them, but its focus follows what matters in that kind of meeting.
TEMPLATES = {
    "general": ("General", ""),
    "one_on_one": (
        "1:1",
        "This is a one-on-one. Focus on the other person's updates and concerns, feedback in "
        "both directions, workload and growth, and what each of you will do before the next one.",
    ),
    "standup": (
        "Standup",
        "This is a standup. In who said what, give each person's done, next and blockers. "
        "Keep everything short.",
    ),
    "customer": (
        "Customer call",
        "This is a call with a customer or client. Focus on their needs, problems and requests, "
        "what was promised to them (by whom and by when), scope or pricing discussed, risks to "
        "the relationship and next steps.",
    ),
    "project": (
        "Project status",
        "This is a project or status meeting. Focus on status by workstream, decisions, risks "
        "and blockers, and dates with owners.",
    ),
    "incident": (
        "Incident or troubleshooting",
        "This is an incident or troubleshooting call. Cover what broke and its impact, the "
        "timeline, what was checked or tried, the cause if found, the fix or workaround, and "
        "follow-ups with owners.",
    ),
    "interview": (
        "Interview",
        "This is an interview. Cover the candidate's background, strengths, concerns and "
        "answers to the key questions, and the interviewers' impressions. Don't invent ratings.",
    ),
}
DEFAULT_TEMPLATE = "general"

# One-click documents made from a meeting. "{me}" becomes the user's name.
RECIPES = {
    "email": (
        "Follow-up email",
        "Write the follow-up email {me} would send to the others after this meeting. Start "
        "with 'Subject: ...', then a one-line thanks, a short recap of what was decided, the "
        "action items with owners and dates, and any open questions. Write in {me}'s voice.",
    ),
    "status": (
        "Status update",
        "Write a short status update {me} could post to their team or manager about this "
        "meeting: 3 to 6 bullet points covering what happened, decisions, risks and next "
        "steps with owners.",
    ),
    "ticket": (
        "Ticket notes",
        "Write notes for an IT service ticket from this call, in past tense: the problem "
        "reported, what was checked or tried, the resolution or current state, next steps "
        "with owners, and any times promised to the customer. Keep it concise.",
    ),
}

BUILT_IN: dict[Kind, dict[str, tuple[str, str]]] = {"template": TEMPLATES, "recipe": RECIPES}
NOUN: dict[Kind, str] = {"template": "kind of meeting", "recipe": "document"}


class UnknownPrompt(LookupError):
    """No kind of meeting or document with that id."""


def _check_kind(kind: str) -> Kind:
    if kind not in KINDS:
        raise ValueError("Unknown kind of prompt")
    return kind  # type: ignore[return-value]


def _entry(kind: Kind, prompt_id: str, row, builtin: tuple[str, str] | None) -> dict:
    if builtin is not None:
        label = row["label"] if row is not None and row["label"] is not None else builtin[0]
        text = (
            row["instructions"]
            if row is not None and row["instructions"] is not None
            else builtin[1]
        )
        edited = (label, text) != builtin
    else:
        label, text, edited = row["label"], row["instructions"], False
    return {
        "id": prompt_id,
        "kind": kind,
        "label": label,
        "instructions": text,
        "builtin": builtin is not None,
        "edited": edited,
    }


def library(store: Store, kind: str) -> list[dict]:
    """Every kind of meeting (or document): built-ins first, in their usual order, then the
    user's own, oldest first."""
    kind = _check_kind(kind)
    rows = {row["id"]: row for row in store.prompt_rows(kind)}
    entries = [
        _entry(kind, prompt_id, rows.get(prompt_id), builtin)
        for prompt_id, builtin in BUILT_IN[kind].items()
    ]
    entries += [
        _entry(kind, prompt_id, row, None)
        for prompt_id, row in rows.items()
        if prompt_id not in BUILT_IN[kind]
    ]
    return entries


def get(store: Store, kind: str, prompt_id: str | None) -> dict | None:
    """One kind of meeting or document, or None if there is no such one (any more)."""
    if not prompt_id:
        return None
    kind = _check_kind(kind)
    builtin = BUILT_IN[kind].get(prompt_id)
    row = store.prompt_row(prompt_id)
    if row is not None and row["kind"] != kind:
        return None
    if builtin is None and row is None:
        return None
    return _entry(kind, prompt_id, row, builtin)


def find(store: Store, prompt_id: str) -> dict:
    """A prompt of either kind by its id; raises UnknownPrompt."""
    for kind in KINDS:
        entry = get(store, kind, prompt_id)
        if entry is not None:
            return entry
    raise UnknownPrompt(prompt_id)


def _clean(
    kind: Kind, label: str, instructions: str, others: list[dict], may_be_empty: bool
) -> tuple[str, str]:
    label = " ".join(label.split())
    instructions = instructions.strip()
    if not label:
        raise ValueError("Give it a name")
    if len(label) > LABEL_MAX:
        raise ValueError(f"Keep the name to {LABEL_MAX} characters or fewer")
    if len(instructions) > INSTRUCTIONS_MAX:
        raise ValueError(f"Keep the instructions to {INSTRUCTIONS_MAX:,} characters or fewer")
    # The general kind of meeting is the one with no instructions; every other kind or
    # document needs some, or it would do nothing different.
    if not instructions and not may_be_empty:
        raise ValueError("Say what it should do")
    same = next((o for o in others if o["label"].casefold() == label.casefold()), None)
    if same is not None:
        raise ValueError(f"There is already a {NOUN[kind]} called {same['label']}")
    return label, instructions


def add(store: Store, kind: str, label: str, instructions: str) -> dict:
    kind = _check_kind(kind)
    existing = library(store, kind)
    if sum(not entry["builtin"] for entry in existing) >= CUSTOM_MAX:
        raise ValueError(f"That's the most you can add ({CUSTOM_MAX})")
    label, instructions = _clean(kind, label, instructions, existing, may_be_empty=False)
    prompt_id = f"custom-{secrets.token_hex(4)}"
    while store.prompt_row(prompt_id) is not None:  # never reuse an id meetings may point to
        prompt_id = f"custom-{secrets.token_hex(4)}"
    store.save_prompt(prompt_id, kind, label, instructions)
    return find(store, prompt_id)


def update(
    store: Store, prompt_id: str, label: str | None = None, instructions: str | None = None
) -> dict:
    entry = find(store, prompt_id)
    kind: Kind = entry["kind"]
    others = [other for other in library(store, kind) if other["id"] != prompt_id]
    label, instructions = _clean(
        kind,
        entry["label"] if label is None else label,
        entry["instructions"] if instructions is None else instructions,
        others,
        may_be_empty=entry["builtin"] and kind == "template" and prompt_id == DEFAULT_TEMPLATE,
    )
    builtin = BUILT_IN[kind].get(prompt_id)
    if builtin is not None:
        # Only what differs from the built-in is kept, so the rest follows new versions.
        if (label, instructions) == builtin:
            store.delete_prompt(prompt_id)
            return find(store, prompt_id)
        store.save_prompt(
            prompt_id,
            kind,
            None if label == builtin[0] else label,
            None if instructions == builtin[1] else instructions,
        )
    else:
        store.save_prompt(prompt_id, kind, label, instructions)
    return find(store, prompt_id)


def reset(store: Store, prompt_id: str) -> dict:
    entry = find(store, prompt_id)
    if not entry["builtin"]:
        raise ValueError("Only the built-in ones can be reset; delete this one instead")
    kind: Kind = entry["kind"]
    name = BUILT_IN[kind][prompt_id][0]
    taken = [
        other
        for other in library(store, kind)
        if other["id"] != prompt_id and other["label"].casefold() == name.casefold()
    ]
    if taken:
        raise ValueError(
            f"Rename your {NOUN[kind]} called {name} first: the reset one is called that"
        )
    store.delete_prompt(prompt_id)
    return find(store, prompt_id)


def delete(store: Store, prompt_id: str) -> None:
    """Delete one of the user's own. Meetings summarized as a deleted kind of meeting go back
    to the usual kind, and so does the usual kind if it was this one."""
    entry = find(store, prompt_id)
    if entry["builtin"]:
        raise ValueError("The built-in ones can't be deleted, only reset")
    with store.transaction():
        store.delete_prompt(prompt_id)
        if entry["kind"] == "template":
            store.forget_template(prompt_id)
            if store.setting("summary_template") == prompt_id:
                store.delete_setting("summary_template")


def choices(store: Store, kind: str) -> list[dict]:
    """Ids and names, for the pickers."""
    return [{"id": entry["id"], "label": entry["label"]} for entry in library(store, kind)]
