"""Ask across every meeting. Claude looks things up with read-only tools over the local
library (transcript search, meetings, summaries, action items) and answers with citations
like [M12 04:31] that the app turns into links. Only the text the tools return is sent."""

from __future__ import annotations

import json
import re
from datetime import date, datetime

import anthropic
from anthropic import beta_tool

from . import apikeys
from .store import Store
from .summarize import MODELS, _clock, cost_usd, spellings, summary_model

SYSTEM = """You answer questions about the meetings {me} recorded with ZachCap, their \
private meeting notebook. Today is {today}.

Look things up with the tools before answering. Search the transcripts for the key words \
(try a few phrasings, names and synonyms), and use the summaries and action items. Answer \
only from what the tools return; if you can't find it, say so plainly.

Cite every fact with its meeting and time as [M<meeting id> <mm:ss>], for example \
[M12 04:31], so the app can link to the moment. Be brief and concrete: a few sentences or a \
short list, with names and dates as they were said."""

CITATION = re.compile(r"\[M(\d+)(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\]")
TRANSCRIPT_LIMIT = 60_000  # characters of one transcript a tool returns (about 15k tokens)
STOPWORDS = set(
    "a an and are as at be but by did do does for from had has have how i in is it me my of "
    "on or our said say says that the their them they this to us was we were what when where "
    "which who why will with you your about any can could would should".split()
)


def fts_query(text: str, any_word: bool = False) -> str | None:
    """A full-text query for free text: each word as a prefix, all of them (or any)."""
    words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS]
    if not words:
        return None
    return (" OR " if any_word else " ").join(f'"{word}"*' for word in dict.fromkeys(words))


def _day(stamp: str) -> str:
    return stamp[:10]


def make_tools(store: Store) -> list:
    """The library, read-only, as tools for Claude."""

    @beta_tool
    def search_transcripts(query: str, limit: int = 25) -> str:
        """Full-text search over every line of every meeting transcript, best matches first.

        Args:
            query: Words to look for, like "firewall cutover" or a name.
            limit: How many lines to return, at most 60.
        """
        limit = max(1, min(limit, 60))
        found: dict[tuple, object] = {}
        for any_word in (False, True):
            match = fts_query(query, any_word)
            if match is None:
                return "Give some words to search for."
            for row in store.search_lines(match, limit):
                found.setdefault((row["meeting_id"], row["start"]), row)
            if len(found) >= 5:
                break
        if not found:
            return "No transcript line matches that."
        return "\n".join(
            f"[M{r['meeting_id']} {_clock(r['start'])}] {_day(r['started_at'])} "
            f'"{r["title"]}" {r["speaker"]}: {r["text"]}'
            for r in list(found.values())[:limit]
        )

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
        participants = store.participants()
        lines = []
        for m in store.meetings():
            day = _day(m["started_at"])
            names = participants.get(m["id"], [])
            if (since and day < since) or (until and day > until):
                continue
            if person and not any(person.lower() in n.lower() for n in names):
                continue
            summary = store.summary(m["id"])
            overview = summary["content"]["overview"] if summary else "(not summarized)"
            minutes = round((m["duration"] or 0) / 60)
            lines.append(
                f'M{m["id"]} {day} "{m["title"]}" {minutes} min; '
                f"{', '.join(names) or 'speakers not named'}. {overview}"
            )
            if len(lines) >= 40:
                break
        return "\n".join(lines) or "No meetings match."

    @beta_tool
    def get_meeting(meeting_id: int) -> str:
        """One meeting in full: its summary (decisions, action items, who said what) and transcript.

        Args:
            meeting_id: The meeting's id, the number after M.
        """
        meeting = store.meeting(meeting_id)
        if meeting is None or meeting["status"] == "recording":
            return f"There is no meeting M{meeting_id}."
        parts = [f'M{meeting_id} "{meeting["title"]}" on {_day(meeting["started_at"])}']
        summary = store.summary(meeting_id)
        if summary:
            parts.append("Summary: " + json.dumps(summary["content"], ensure_ascii=False))
        lines = [
            f"[{_clock(r['start'])}] {r['speaker']}: {r['text']}"
            for r in store.transcript(meeting_id)
        ]
        transcript = "\n".join(lines)
        if len(transcript) > TRANSCRIPT_LIMIT:
            transcript = (
                transcript[:TRANSCRIPT_LIMIT] + "\n[... transcript trimmed; search for more]"
            )
        parts.append("Transcript:\n" + (transcript or "(no transcript)"))
        return "\n\n".join(parts)

    @beta_tool
    def list_action_items(status: str = "open", owner: str | None = None) -> str:
        """Action items and commitments from every summarized meeting.

        Args:
            status: "open", "done" or "all".
            owner: Only items owned by this person.
        """
        rows = store.action_items(status if status in ("open", "done") else None)
        lines = [
            f"[M{r['meeting_id']} {_clock(r['said_at'] or 0)}] {r['status']}: {r['owner']}, "
            f"{r['kind']}: {r['task']}{f' (due {r["due"]})' if r['due'] else ''} "
            f'from "{r["meeting_title"]}" {_day(r["meeting_started_at"])}'
            for r in rows
            if not owner or owner.lower() in r["owner"].lower()
        ]
        return "\n".join(lines[:80]) or "No action items match."

    return [search_transcripts, list_meetings, get_meeting, list_action_items]


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

    client = anthropic.Anthropic(api_key=key)
    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=4000,
        max_iterations=10,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=SYSTEM.format(me=store.me()["name"], today=f"{date.today():%A %B %d, %Y}")
        + spellings(store),
        tools=make_tools(store),
        messages=messages,
    )
    last = None
    cost = 0.0
    for message in runner:
        last = message
        served = message.model if message.model in MODELS else model
        usage = message.usage
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        created = getattr(usage, "cache_creation_input_tokens", 0) or 0
        effective_input = usage.input_tokens + cached * 0.1 + created * 1.25
        cost += cost_usd(served, round(effective_input), usage.output_tokens)
    if last is None or last.stop_reason == "refusal":
        raise RuntimeError("Claude didn't answer that question.")
    answer = "".join(b.text for b in last.content if b.type == "text").strip()
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
        "model": last.model if last.model in MODELS else model,
        "cost_usd": round(cost, 5),
        "asked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
