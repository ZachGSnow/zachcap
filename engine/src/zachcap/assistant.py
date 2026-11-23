"""Ask across every meeting. Claude looks things up with read-only tools over the local
library (transcript search, meetings, summaries, action items) and answers with citations
like [M12 04:31] that the app turns into links. Only the text the tools return is sent."""

from __future__ import annotations

import json
import re
from datetime import date, datetime

import anthropic
from anthropic import beta_tool

from . import apikeys, privacy
from .store import Store
from .summarize import (
    MAX_TOKENS,
    _clock,
    notes_text,
    reply_text,
    spellings,
    summary_model,
    to_seconds,
    usage_cost,
)

SYSTEM = """You answer questions about the meetings {me} recorded with ZachCap, their \
private meeting notebook. Today is {today}.

Look things up with the tools before answering. Search the transcripts for the key words \
(try a few phrasings, names and synonyms), and use the summaries and action items. Answer \
only from what the tools return; if you can't find it, say so plainly.

Cite every fact with its meeting and time as [M<meeting id> <mm:ss>], for example \
[M12 04:31], so the app can link to the moment. Be brief and concrete: a few sentences or a \
short list, with names and dates as they were said."""

# Meeting ids have at most 9 digits: a longer one (transcript text can put anything in an
# answer) would overflow SQLite and throw the paid answer away.
CITATION = re.compile(r"\[M(\d{1,9})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\]")
TRANSCRIPT_LIMIT = 60_000  # characters of one transcript a tool returns (about 15k tokens)
MEETINGS_LIMIT = 40
ACTIONS_LIMIT = 80
LOOKUPS = 10  # rounds of tool calls before Claude answers with what it found
OUT_OF_LOOKUPS = (
    "That's all the looking up there's time for. Answer the question now from what you "
    "found, and say what you couldn't find or check."
)
STOPWORDS = set(
    "a an and are as at be but by did do does for from had has have how i in is it me my of "
    "on or our said say says that the their them they this to us was we were what when where "
    "which who why will with you your about any can could would should".split()
)


def fts_query(text: str, any_word: bool = False, names: set[str] | None = None) -> str | None:
    """A full-text query for free text: each word as a prefix, all of them (or any).

    Filler words are left out, except where they are part of someone's name (Will, May).
    """
    words = [
        w for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS or w in (names or ())
    ]
    if not words:
        return None
    return (" OR " if any_word else " ").join(f'"{word}"*' for word in dict.fromkeys(words))


def _day(stamp: str) -> str:
    return stamp[:10]


# The library, read-only, as plain functions. Claude's tools here and the MCP server in
# mcp_server.py both answer through these, so the two always agree.


def search(store: Store, query: str, limit: int = 25, meeting_id: int | None = None) -> str:
    """Transcript lines across meetings (or in one), best matches first; any word if all of
    them find too few."""
    limit = max(1, min(limit, 60))
    # Other people's names ("Will"); not the user's, which is "You" until they set it.
    names = {
        word
        for p in store.people()
        if not p["is_me"]
        for word in re.findall(r"\w+", p["name"].lower())
    }
    found: dict[tuple, object] = {}
    for any_word in (False, True):
        match = fts_query(query, any_word, names)
        if match is None:
            return "Give some words to search for."
        for row in store.search_lines(match, limit + 1, meeting_id):
            found.setdefault((row["meeting_id"], row["start"]), row)
        if len(found) >= 5:
            break
    if not found:
        return "No transcript line matches that."
    rows = list(found.values())
    lines = [
        f"[M{r['meeting_id']} {_clock(r['start'])}] {_day(r['started_at'])} "
        f'"{r["title"]}" {r["speaker"]}: {r["text"]}'
        for r in rows[:limit]
    ]
    if len(rows) > limit:
        lines.append(
            f"(More lines match; these are the best {limit}. Use more specific words, or "
            "search one meeting with meeting_id.)"
        )
    return "\n".join(lines)


def meetings_list(
    store: Store, since: str | None = None, until: str | None = None, person: str | None = None
) -> str:
    participants = store.participants()
    lines = []
    matching = 0
    for m in store.meetings():
        day = _day(m["started_at"])
        names = participants.get(m["id"], [])
        if (since and day < since) or (until and day > until):
            continue
        if person and not any(person.lower() in n.lower() for n in names):
            continue
        matching += 1
        if len(lines) >= MEETINGS_LIMIT:
            continue  # counted, so the note below says how many there are
        summary = store.summary(m["id"])
        overview = summary["content"]["overview"] if summary else "(not summarized)"
        minutes = round((m["duration"] or 0) / 60)
        lines.append(
            f'M{m["id"]} {day} "{m["title"]}" {minutes} min; '
            f"{', '.join(names) or 'speakers not named'}. {overview}"
        )
    if matching > len(lines):
        lines.append(
            f"(The newest {len(lines)} of {matching} meetings that match. Use since, until or "
            "person to see the others.)"
        )
    return "\n".join(lines) or "No meetings match."


def meeting_detail(store: Store, meeting_id: int, start_at: str | None = None) -> str:
    """A meeting's summary, notes and transcript. A long transcript comes in parts: start_at
    (mm:ss) continues from where the last part ended."""
    meeting = store.meeting(meeting_id)
    if meeting is None or meeting["status"] == "recording":
        return f"There is no meeting M{meeting_id}."
    begin = to_seconds(start_at) if start_at else None
    parts = [f'M{meeting_id} "{meeting["title"]}" on {_day(meeting["started_at"])}']
    summary = store.summary(meeting_id)
    if summary and begin is None:  # the first part has it; later parts only go on
        parts.append("Summary: " + json.dumps(summary["content"], ensure_ascii=False))
    lines: list[str] = []
    size = 0
    rest = None
    for r in store.transcript(meeting_id):
        if begin is not None and r["start"] < begin:
            continue
        line = f"[{_clock(r['start'])}] {r['speaker']}: {r['text']}"
        if lines and size + len(line) > TRANSCRIPT_LIMIT:
            rest = _clock(r["start"])
            break
        lines.append(line)
        size += len(line) + 1
    transcript = "\n".join(lines) or (
        "(no transcript)" if begin is None else "(nothing after that)"
    )
    if rest:
        transcript += (
            f'\n[The transcript goes on: get_meeting with start_at "{rest}" for the rest.]'
        )
    heading = "Transcript:" if begin is None else f"Transcript from {_clock(begin)}:"
    parts.append(f"{heading}\n{transcript}")
    return "\n\n".join(parts) + (notes_text(store, meeting_id) if begin is None else "")


def action_items_list(store: Store, status: str = "open", owner: str | None = None) -> str:
    rows = store.action_items(status if status in ("open", "done") else None)
    lines = [
        f"[M{r['meeting_id']} {_clock(r['said_at'] or 0)}] {r['status']}: {r['owner']}, "
        f"{r['kind']}: {r['task']}{f' (due {r["due"]})' if r['due'] else ''} "
        f'from "{r["meeting_title"]}" {_day(r["meeting_started_at"])}'
        for r in rows
        if not owner or owner.lower() in r["owner"].lower()
    ]
    if len(lines) > ACTIONS_LIMIT:
        shown = lines[:ACTIONS_LIMIT]
        shown.append(
            f"(The first {ACTIONS_LIMIT} of {len(lines)} items. Filter by owner or status to "
            "see the others.)"
        )
        lines = shown
    return "\n".join(lines) or "No action items match."


def people_list(store: Store) -> str:
    lines = [
        f"{p['name']}{' (the user)' if p['is_me'] else ''}: {p['meetings']} meetings, "
        f"{round((p['talk_seconds'] or 0) / 60)} min talking"
        + (f", last seen {_day(p['last_seen'])}" if p["last_seen"] else "")
        for p in store.people_stats()
    ]
    return "\n".join(lines) or "Nobody named yet."


def work_log(store: Store, period: str = "day", day: str | None = None) -> str:
    """The day's or week's work log, citing each meeting as [M12]."""
    from . import digest

    if period not in ("day", "week"):
        raise ValueError('The period is "day" or "week".')
    try:
        when = date.fromisoformat(day) if day else date.today()
    except ValueError:
        raise ValueError("Use a date like 2026-11-23.") from None
    return digest.markdown(digest.build(store, period, when), cite=True)


def make_tools(store: Store) -> list:
    """The library, read-only, as tools for Claude."""

    @beta_tool
    def search_transcripts(query: str, limit: int = 25, meeting_id: int | None = None) -> str:
        """Full-text search over every line of every meeting transcript, best matches first.

        Args:
            query: Words to look for, like "firewall cutover" or a name.
            limit: How many lines to return, at most 60.
            meeting_id: Only search this meeting (the number after M).
        """
        return privacy.outgoing(store, search(store, query, limit, meeting_id))

    @beta_tool
    def list_meetings(
        since: str | None = None, until: str | None = None, person: str | None = None
    ) -> str:
        """Meetings, newest first, with date, length, who spoke and the summary's overview.

        Args:
            since: Only meetings on or after this date, YYYY-MM-DD.
            until: Only meetings on or before this date, YYYY-MM-DD.
            person: Only meetings this person spoke in.
        """
        return privacy.outgoing(store, meetings_list(store, since, until, person))

    @beta_tool
    def get_meeting(meeting_id: int, start_at: str | None = None) -> str:
        """One meeting in full: its summary (decisions, action items, who said what) and transcript.

        Args:
            meeting_id: The meeting's id, the number after M.
            start_at: For a long transcript, where to go on from (mm:ss or h:mm:ss), as the
                end of the previous part says.
        """
        return privacy.outgoing(store, meeting_detail(store, meeting_id, start_at))

    @beta_tool
    def list_action_items(status: str = "open", owner: str | None = None) -> str:
        """Action items and commitments from every summarized meeting.

        Args:
            status: "open", "done" or "all".
            owner: Only items owned by this person.
        """
        return privacy.outgoing(store, action_items_list(store, status, owner))

    @beta_tool
    def get_work_log(day: str | None = None, period: str = "day") -> str:
        """A work log: each meeting of a day or a week with its time, people, overview,
        decisions and action items, plus draft time entries (rounded up to 15 minutes).

        Args:
            day: A date in the period, YYYY-MM-DD; today if not given.
            period: "day", or "week" for Monday to Sunday.
        """
        return privacy.outgoing(store, work_log(store, period, day))

    return [search_transcripts, list_meetings, get_meeting, list_action_items, get_work_log]


def ask_everything(store: Store, question: str, history: list[dict] | None = None) -> dict:
    """Answer a question from the whole library. history: earlier {question, answer} turns."""
    key = apikeys.get("anthropic")
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to ask questions.")
    if not store.meetings():
        raise RuntimeError("There are no meetings to ask about yet.")
    model = summary_model(store)
    messages: list[dict] = []
    for turn in (history or [])[-6:]:
        messages += [
            {"role": "user", "content": turn["question"]},
            {"role": "assistant", "content": turn["answer"]},
        ]
    messages.append({"role": "user", "content": question})

    request = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
        "output_config": {"effort": "medium"},
        "system": SYSTEM.format(me=store.me()["name"], today=f"{date.today():%A %B %d, %Y}")
        + spellings(store),
    }
    tools = make_tools(store)
    client = anthropic.Anthropic(api_key=key)
    # The whole conversation goes with every turn (no server-side thread), and is kept here
    # too, so a last turn without tools can follow on from it.
    runner = client.beta.messages.tool_runner(
        **request, tools=tools, messages=messages, max_iterations=LOOKUPS, thread=False
    )
    conversation = list(messages)
    last = None
    cost = 0.0
    for message in runner:
        last = message
        cost += usage_cost(message.usage, model)
        conversation.append({"role": "assistant", "content": message.content})
        if message.stop_reason == "tool_use":
            results = runner.generate_tool_call_response()  # cached: the runner reuses it
            if results is not None:
                conversation.append(results)
    if last is not None and last.stop_reason == "tool_use" and conversation[-1]["role"] == "user":
        # Out of lookups while still looking ("Let me check March too"): one more turn, with
        # the tools off and a word to answer from what it found.
        results = conversation[-1]
        nudge = {"type": "text", "text": OUT_OF_LOOKUPS}
        conversation[-1] = {**results, "content": [*results["content"], nudge]}
        last = client.beta.messages.create(
            **request,
            tools=[tool.to_dict() for tool in tools],
            tool_choice={"type": "none"},
            messages=conversation,
        )
        cost += usage_cost(last.usage, model)
    if last is None or last.stop_reason == "refusal":
        raise RuntimeError("Claude didn't answer that question.")
    answer = reply_text(last) if last.stop_reason != "tool_use" else ""
    if not answer:
        answer = "I couldn't finish looking that up. Try a narrower question."
    cited = {}
    for meeting_id in dict.fromkeys(int(m) for m in CITATION.findall(answer)):
        meeting = store.meeting(meeting_id)
        if meeting is not None:
            cited[meeting_id] = {"title": meeting["title"], "started_at": meeting["started_at"]}
    return {
        "answer": answer,
        "meetings": cited,
        "model": last.model or model,
        "cost_usd": round(cost, 5),
        "asked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
