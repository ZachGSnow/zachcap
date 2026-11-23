"""Meeting summaries with Claude. Only the transcript text is sent, never audio."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import apikeys, privacy, prompts, vocabulary
from .prompts import DEFAULT_TEMPLATE, RECIPES, TEMPLATES  # noqa: F401 (re-exported)
from .store import Store

# The models to choose from in Settings.
MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-fable-5-1": "Claude Fable 5.1",
}
# USD per million tokens: input, output and cache reads. List prices, used only for the cost
# shown next to each answer; writing the cache costs 1.25x input. The last three are what a
# server-side fallback can answer with when the chosen model declines a request.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
CACHE_WRITE = 1.25
# Room for the answer and the thinking before it, which Opus 5.5 always does.
MAX_TOKENS = 16_000
CUT_OFF = "\n\n(Cut off: this ran longer than ZachCap allows.)"
# In a side-by-side test Opus 5.5 caught an implicit commitment Sonnet 5.5 missed,
# for about $0.08 per meeting hour.
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM = """You summarize meeting transcripts for the person who recorded them, shown \
as "{me}" in the transcript.

Speaker names come from voice recognition. "Speaker N" is someone not identified yet, and \
"Call audio" is several remote people who couldn't be told apart; never guess who they are.

Be specific and plain: concrete decisions, owners and dates exactly as stated. Catch \
offhand promises ("I'll send it over", "I can bring the cable") as commitments too. \
Don't invent anything that isn't in the transcript. Leave a list empty when there is \
nothing for it."""


RECIPE_SYSTEM = """You write documents from a meeting for {me}, who recorded it. Use only \
facts from the meeting, in plain text without Markdown headings, ready to paste."""


class ActionItem(BaseModel):
    owner: str = Field(
        description="Who will do it, using a name from the transcript, or 'Unassigned'"
    )
    task: str = Field(description="What will be done, starting with a verb")
    due: str | None = Field(description="When, as said in the meeting (e.g. 'Tuesday'), or null")
    kind: Literal["commitment", "action"] = Field(
        description="'commitment' when the owner said they would do it themselves; "
        "'action' when it was assigned to them or agreed by the group"
    )
    said_at: str = Field(
        description="Timestamp of the line where it was agreed, as in the transcript"
    )


class SpeakerPoints(BaseModel):
    speaker: str = Field(description="Speaker name exactly as in the transcript")
    points: list[str] = Field(description="Their main points, asks and updates; 1 to 4 short items")


class Topic(BaseModel):
    title: str
    start: str = Field(description="Timestamp where the topic starts, as in the transcript")


class Summary(BaseModel):
    title: str = Field(description="A short title for the meeting, at most 8 words")
    overview: str = Field(description="Two to four sentences: what it was about and the outcome")
    decisions: list[str]
    action_items: list[ActionItem]
    who_said_what: list[SpeakerPoints] = Field(
        description="One entry per person who spoke meaningfully, most active first"
    )
    open_questions: list[str]
    topics: list[Topic]


def spellings(store: Store) -> str:
    """The user's words to know, for prompts, so names and jargon are spelled their way."""
    terms = vocabulary.parse(store.setting("vocabulary")).terms
    return (
        f"\n\nSpell these names and terms exactly as written: {', '.join(terms)}." if terms else ""
    )


def to_seconds(stamp: str | None) -> float | None:
    try:
        parts = [float(p) for p in str(stamp).strip().strip("[]").split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _clock(seconds: float) -> str:
    s = int(seconds)
    return (
        f"{s // 60:02d}:{s % 60:02d}"
        if s < 3600
        else f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    )


def transcript_text(store: Store, meeting_id: int) -> str:
    """The transcript as Claude gets it (masked if the user asked for that)."""
    return privacy.outgoing(
        store,
        "\n".join(
            f"[{_clock(row['start'])}] {row['speaker']}: {row['text']}"
            for row in store.transcript(meeting_id)
        ),
    )


SNAPSHOT_IN_PROMPT = 1500  # characters of a snapshot's text that go into a prompt


def _screen_text(note) -> str:
    text = " / ".join(line.strip() for line in note["text"].splitlines() if line.strip())
    if not text:
        return "(no text on it)"
    return text[:SNAPSHOT_IN_PROMPT] + "..." if len(text) > SNAPSHOT_IN_PROMPT else text


def notes_text(store: Store, meeting_id: int) -> str:
    """The user's own notes and bookmarks, for prompts: they show what mattered to them.
    Snapshots come apart from them, labelled as text read off the screen: someone else's
    words (a shared error message, a slide), not the user's."""
    me = store.me()["name"]
    notes = store.notes(meeting_id)
    own = [
        f"[{_clock(n['at'])}] {n['text'] or 'marked this moment as important'}"
        for n in notes
        if n["kind"] != "snapshot"
    ]
    screens = [f"[{_clock(n['at'])}] {_screen_text(n)}" for n in notes if n["kind"] == "snapshot"]
    parts = []
    if own:
        parts.append(
            f"\n\nNotes {me} took during the meeting (they show what mattered to {me}; "
            "make sure the summary covers them):\n" + "\n".join(own)
        )
    if screens:
        parts.append(
            f"\n\nText read from screen snapshots {me} took during the meeting (what was "
            "shown, such as a shared screen or an error message; written by whoever made it, "
            f"not by {me}, and possibly misread):\n" + "\n".join(screens)
        )
    return privacy.outgoing(store, "".join(parts)) if parts else ""


ASK_SYSTEM = """You answer questions about one meeting, for the person who recorded it \
(shown as "{me}"). Use only the transcript below. Quote timestamps in square brackets, \
like [12:34], next to the facts you use so they can jump to them. If the transcript \
doesn't say, answer that it doesn't. Be brief: a few sentences or a short list.

Meeting: {title}

Transcript:
{transcript}"""


def ask(store: Store, meeting_id: int, question: str) -> dict:
    """Answer a question about one meeting from its transcript."""
    key = apikeys.get("anthropic")
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to ask questions.")
    text = transcript_text(store, meeting_id)
    if not text:
        raise RuntimeError("This meeting has no transcript yet.")
    meeting = store.meeting(meeting_id)
    model = summary_model(store)
    client = anthropic.Anthropic(api_key=key)
    response = client.beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        # The transcript is the stable prefix, so follow-up questions read it from cache.
        system=[
            {
                "type": "text",
                "text": ASK_SYSTEM.format(
                    me=store.me()["name"],
                    title=meeting["title"],
                    transcript=text + notes_text(store, meeting_id),
                )
                + spellings(store),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": question}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude didn't answer that question.")
    return {
        "answer": reply_text(response) or "Claude didn't answer that question.",
        "model": response.model or model,
        "cost_usd": usage_cost(response.usage, model),
    }


def reply_text(response) -> str:
    """The text of a reply, marked when it was cut off at the token limit."""
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text + CUT_OFF if text and response.stop_reason == "max_tokens" else text


def summary_model(store: Store) -> str:
    model = store.setting("summary_model")
    return model if model in MODELS else DEFAULT_MODEL


def _prices(model: str | None) -> tuple[float, float, float] | None:
    """A model's prices. A dated id ("claude-opus-5-5-20261101") goes by its base id."""
    known = [m for m in PRICES if model and (model == m or model.startswith(f"{m}-"))]
    return PRICES[max(known, key=len)] if known else None


def usage_cost(usage, model: str) -> float:
    """What one response cost in USD, each attempt at the prices of the model that ran it.

    With a server-side fallback, usage.iterations has an entry per attempt (a declined one
    too, so this is the most it can have cost); top-level usage covers only the attempt
    that answered. model: the model that was asked.
    """
    total = 0.0
    for attempt in getattr(usage, "iterations", None) or [usage]:
        per_in, per_out, per_read = (
            _prices(getattr(attempt, "model", None)) or _prices(model) or PRICES[DEFAULT_MODEL]
        )
        written = getattr(attempt, "cache_creation_input_tokens", 0) or 0
        read = getattr(attempt, "cache_read_input_tokens", 0) or 0
        total += (
            attempt.input_tokens * per_in
            + written * per_in * CACHE_WRITE
            + read * per_read
            + attempt.output_tokens * per_out
        )
    return total / 1_000_000


def meeting_template(store: Store, meeting, requested: str | None = None) -> str:
    """The kind of meeting to summarize as: asked for, else this meeting's, else the usual
    kind, else the general one. A kind the user has since deleted is skipped."""
    for template in (requested, meeting["template"], store.setting("summary_template")):
        if prompts.get(store, "template", template) is not None:
            return template
    return DEFAULT_TEMPLATE


def summarize(store: Store, meeting_id: int, template: str | None = None) -> dict:
    key = apikeys.get("anthropic")
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to create summaries.")
    meeting = store.meeting(meeting_id)
    text = transcript_text(store, meeting_id)
    if not text:
        raise RuntimeError("This meeting has no transcript yet.")

    template = meeting_template(store, meeting, template)
    # The user's own words (built-in or edited): they set the focus, while the summary's
    # sections and shape stay fixed by the Summary model, so no edit can break parsing.
    focus = prompts.get(store, "template", template)["instructions"]
    started = datetime.fromisoformat(meeting["started_at"])
    model = summary_model(store)
    client = anthropic.Anthropic(api_key=key)
    response = client.beta.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        # If a safety classifier declines, Anthropic re-runs the request on its
        # recommended fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=SYSTEM.format(me=store.me()["name"])
        + (f"\n\n{focus}" if focus else "")
        + spellings(store),
        messages=[
            {
                "role": "user",
                "content": f"Meeting: {meeting['title']}\nDate: {started:%A %B %d, %Y}\n\n"
                f"Transcript:\n{text}" + notes_text(store, meeting_id),
            }
        ],
        output_format=Summary,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError("Claude didn't return a summary for this meeting.")

    usage = response.usage
    # A server-side fallback can answer with another model; price what actually ran.
    cost = usage_cost(usage, model)
    result = {**response.parsed_output.model_dump(), "template": template}
    store.update_meeting(meeting_id, template=template)
    store.save_summary(
        meeting_id, response.model, result, usage.input_tokens, usage.output_tokens, cost
    )
    store.replace_action_items(
        meeting_id,
        [{**item, "said_at": to_seconds(item["said_at"])} for item in result["action_items"]],
    )
    return result


MAX_CHANGE = 500  # characters in one "change it" request
MAX_DOCUMENT = 20_000  # characters of a document sent back to be changed


def recipe(
    store: Store,
    meeting_id: int,
    name: str,
    previous: str | None = None,
    change: str | None = None,
) -> dict:
    """A document made from one meeting (a follow-up email, a status update, or one of the
    user's own). With ``previous`` (the document as it stands, edits included) and
    ``change`` ("shorter, for the client"), Claude rewrites it that way instead, still from
    the meeting."""
    if (previous is None) != (change is None):
        raise RuntimeError("Send the document and the change together.")
    if change is not None:
        change = change.strip()
        if not change or len(change) > MAX_CHANGE:
            raise RuntimeError(f"Say what to change, in at most {MAX_CHANGE} characters.")
        if not previous.strip() or len(previous) > MAX_DOCUMENT:
            raise RuntimeError(f"The document to change must be 1 to {MAX_DOCUMENT} characters.")
    document = prompts.get(store, "recipe", name)
    if document is None:
        raise RuntimeError(f"Unknown recipe {name!r}")
    key = apikeys.get("anthropic")
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to use this.")
    meeting = store.meeting(meeting_id)
    text = transcript_text(store, meeting_id)
    if not text:
        raise RuntimeError("This meeting has no transcript yet.")
    me = store.me()["name"]
    started = datetime.fromisoformat(meeting["started_at"])
    summary = store.summary(meeting_id)
    context = f"Meeting: {meeting['title']}\nDate: {started:%A %B %d, %Y}\n"
    if meeting["ticket"]:
        context += f"Ticket: {meeting['ticket']}\n"
    context += "\n"
    if summary:
        context += f"Summary:\n{json.dumps(summary['content'], ensure_ascii=False)}\n\n"
    context += f"Transcript:\n{text}" + notes_text(store, meeting_id)
    model = summary_model(store)
    messages = [
        {
            "role": "user",
            "content": [
                # The meeting is the stable part: a second recipe reads it from cache.
                {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
                # A plain replace, not str.format: the user's own text may hold braces.
                {"type": "text", "text": document["instructions"].replace("{me}", me)},
            ],
        }
    ]
    if change is not None:  # the document as it stands, then what to change
        messages += [
            {"role": "assistant", "content": previous.strip()},
            {
                "role": "user",
                "content": f"Rewrite it: {change}\n\nStill use only facts from the meeting. "
                "Answer with the whole new version and nothing else.",
            },
        ]
    client = anthropic.Anthropic(api_key=key)
    response = client.beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        system=RECIPE_SYSTEM.format(me=me) + spellings(store),
        messages=messages,
    )
    text = reply_text(response)
    if response.stop_reason == "refusal" or not text:
        raise RuntimeError("Claude didn't write that.")
    return {
        "title": document["label"],
        "text": text,
        "model": response.model or model,
        "cost_usd": usage_cost(response.usage, model),
    }
