"""Meeting summaries with Claude. Only the transcript text is sent, never audio."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from . import apikeys, vocabulary
from .store import Store

# Model id -> (label, USD per million input tokens, per million output tokens).
# Prices are list prices, used only for the cost shown next to each summary.
MODELS = {
    "claude-opus-5-5": ("Claude Opus 5.5", 4.00, 20.00),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5", 2.00, 10.00),
    "claude-fable-5-1": ("Claude Fable 5.1", 10.00, 50.00),
}
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
    return "\n".join(
        f"[{_clock(row['start'])}] {row['speaker']}: {row['text']}"
        for row in store.transcript(meeting_id)
    )


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
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low"},
        # The transcript is the stable prefix, so follow-up questions read it from cache.
        system=[
            {
                "type": "text",
                "text": ASK_SYSTEM.format(
                    me=store.me()["name"], title=meeting["title"], transcript=text
                )
                + spellings(store),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": question}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude didn't answer that question.")
    answer = "".join(block.text for block in response.content if block.type == "text").strip()
    served_by = response.model if response.model in MODELS else model
    usage = response.usage
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    # Cache reads cost a tenth of input; writes 1.25x (5-minute cache).
    effective_input = usage.input_tokens + cached * 0.1 + created * 1.25
    return {
        "answer": answer,
        "model": served_by,
        "cost_usd": cost_usd(served_by, round(effective_input), usage.output_tokens),
    }


def summary_model(store: Store) -> str:
    model = store.setting("summary_model")
    return model if model in MODELS else DEFAULT_MODEL


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    _, per_input, per_output = MODELS.get(model, MODELS[DEFAULT_MODEL])
    return (input_tokens * per_input + output_tokens * per_output) / 1_000_000


def summarize(store: Store, meeting_id: int) -> dict:
    key = apikeys.get("anthropic")
    if not key:
        raise RuntimeError("Add your Anthropic API key in Settings to create summaries.")
    meeting = store.meeting(meeting_id)
    text = transcript_text(store, meeting_id)
    if not text:
        raise RuntimeError("This meeting has no transcript yet.")

    started = datetime.fromisoformat(meeting["started_at"])
    model = summary_model(store)
    client = anthropic.Anthropic(api_key=key)
    response = client.beta.messages.parse(
        model=model,
        max_tokens=8000,
        # If a safety classifier declines, Anthropic re-runs the request on its
        # recommended fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=SYSTEM.format(me=store.me()["name"]) + spellings(store),
        messages=[
            {
                "role": "user",
                "content": f"Meeting: {meeting['title']}\nDate: {started:%A %B %d, %Y}\n\n"
                f"Transcript:\n{text}",
            }
        ],
        output_format=Summary,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError("Claude didn't return a summary for this meeting.")

    usage = response.usage
    # A server-side fallback can answer with another model; price what actually ran.
    served_by = response.model if response.model in MODELS else model
    cost = cost_usd(served_by, usage.input_tokens, usage.output_tokens)
    result = response.parsed_output.model_dump()
    store.save_summary(
        meeting_id, response.model, result, usage.input_tokens, usage.output_tokens, cost
    )
    store.replace_action_items(
        meeting_id,
        [{**item, "said_at": to_seconds(item["said_at"])} for item in result["action_items"]],
    )
    return result
