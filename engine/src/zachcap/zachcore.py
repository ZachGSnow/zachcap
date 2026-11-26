"""What ZachCore gets for a meeting: one message, whole, made again whenever something about the
meeting changes (outbox.py says when; ZachCore's docs/sources.md says what each field is). It is
made from the library as it is and from nothing else: never audio, voice samples, snapshots,
attached files or dictation, and nothing at all of a private meeting.

Who was there: a voice that is somebody goes as that person, under their uid, and is sure when
it was named or recognized, never when only suggested. A suggestion of somebody known only from
private meetings isn't made at all. A voice without a name goes as its label, under its own id
in this meeting, until it has one; its tasks and facts then follow it to the name. You aren't
one of them: your part of the talk is the meeting's own share, and your tasks are p-me's. Voices
left out (brief.py), voices too short to tell (minor) and lines with no voice are nobody.

Three switches say what goes besides the title, the time, the client, the ticket, the people
and their talk: summaries (the overview, your notes, decisions, questions and topics), tasks
(with the ones that went), and facts. The fourth, transcripts, is the sender's.

What a message says replaces what ZachCore has of it, even when it is empty; what it leaves out
stays there as it is. So a switch that is on sends its lists, empty too (a new summary without
decisions takes the old ones off), and one that is off leaves them out: turning a switch off
takes nothing out of ZachCore. That is what taking back is for. A meeting without a client or a
ticket sends it as null, which takes off one it had.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Collection

from . import brief, clients, privacy
from .store import IN_SHARED, UNKNOWN, Store

ME = "p-me"  # ZachCore's ref for whoever sends: you
NOBODY = "nobody"  # the owner of a task nobody took, which ZachCore asks you about
PARTS = ("summaries", "tasks", "facts")  # each a switch, on until turned off
SURE = ("auto", "confirmed")
CLIENT = "client"  # a fact's about, when it's about the meeting's client
MY_NOTES = "## My notes"
BOOKMARK = "Bookmarked moment"  # a bookmark's line, as exports write it


def setting(part: str) -> str:
    return f"zachcore_{part}"


def parts(store: Store) -> set[str]:
    """The switches that are on."""
    return {part for part in PARTS if store.setting(setting(part), "1") == "1"}


def message(store: Store, meeting_id: int, said: Collection[str] = PARTS) -> dict | None:
    """A meeting's message as it would go now, without its revision (each body sent gets its
    own), saying what the switches in ``said`` allow. None for a meeting that is private or
    not there."""
    meeting = store.meeting(meeting_id)
    if meeting is None or privacy.is_private(store, meeting) or not meeting["uid"]:
        return None
    voices = _voices(store, meeting_id)
    me = {"you", "me", _folded(store.me()["name"])}
    client = clients.of_meeting(store, meeting)["client"]
    named: set[int] = set()  # voices a task or fact is by or about

    def voice_of(voice_id, name: str) -> sqlite3.Row | None:
        voice = voices.get(voice_id) if isinstance(voice_id, int) else None
        return voice or voices.get(store.voice_named(meeting_id, name))

    def owner(item: sqlite3.Row) -> str:
        voice = voice_of(item["voice_id"], item["owner"])
        if voice is not None:
            named.add(voice["id"])
            who = _who(voice)
            return who["ref"] if isinstance(who, dict) else who
        name = " ".join(item["owner"].split())
        if not name or name.casefold() == "unassigned":
            return NOBODY
        return ME if _folded(name) in me else name

    content = {}
    if {"summaries", "facts"} & set(said):
        summary = store.summary(meeting_id)
        content = summary["content"] if summary and isinstance(summary["content"], dict) else {}
    body: dict = {}
    if "summaries" in said:
        notes = [note for note in store.notes(meeting_id) if note["kind"] in ("note", "bookmark")]
        body["overview"] = _overview(content.get("overview"), notes)
        body["decisions"] = _said(content, "decisions", "d")
        body["questions"] = _said(content, "open_questions", "q")
        topics = content.get("topics") if isinstance(content.get("topics"), list) else []
        topics = [topic for topic in topics if isinstance(topic, dict)]
        body["topics"] = _listed(
            [topic.get("title") for topic in topics], [topic.get("start") for topic in topics], "t"
        )
    if "tasks" in said:
        items = sorted(
            (item for item in store.action_items(meeting_id=meeting_id) if item["uid"]),
            key=lambda item: (item["said_at"] is None, item["said_at"] or 0, item["task"]),
        )
        body["tasks"] = [_task(item, owner(item)) for item in items]
        gone = [
            row["uid"]
            for row in store.db.execute(
                "SELECT uid FROM items_gone WHERE meeting_uid = ? ORDER BY uid", (meeting["uid"],)
            )
        ]
        if gone:  # what to do, not what there is: nothing to say when nothing went
            body["gone"] = gone
    if "facts" in said:
        body["facts"] = _facts(content, client, voice_of, me, named)

    head: dict = {"ref": meeting["uid"], "title": meeting["title"]}
    head["started"] = meeting["started_at"][:16]  # its local time, as recorded
    if meeting["duration"]:
        head["seconds"] = round(meeting["duration"])
    if meeting["source_app"]:
        head["app"] = meeting["source_app"]
    head[CLIENT] = client or None  # None takes off one it had
    head["ticket"] = meeting["ticket"] or None
    head.update(_talk(store, meeting_id, voices, named))
    return {**head, **body}


def transcript(store: Store, meeting_id: int) -> list[dict]:
    """What was said, line by line, as ZachCore keeps it: when, in seconds into the meeting;
    who, as Claude is shown them (never a suggestion of somebody known only from private
    meetings; a voice left out is Unknown); and the words ("[redacted]" where they went)."""
    return [
        {"sec": round(line["start"], 2), "label": line["speaker"], "text": line["text"]}
        for line in store.transcript(meeting_id, private=False)
    ]


def digest(value) -> str:
    """A hash of what a request says, to tell whether it changed since it was sent."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _voices(store: Store, meeting_id: int) -> dict[int, sqlite3.Row]:
    return {
        row["id"]: row
        for row in store.db.execute(
            "SELECT ms.id, ms.label, ms.status, p.uid, p.name, p.org, p.is_me, "
            f"{IN_SHARED} AS shared FROM meeting_speakers ms "
            "LEFT JOIN people p ON p.id = ms.person_id WHERE ms.meeting_id = ?",
            (meeting_id,),
        )
    }


def _who(voice: sqlite3.Row) -> dict | str:
    """Whom a voice is, as ZachCore gets them: ME; one of people (a dict); or the name of one
    who isn't listed, for "Promised by ..."."""
    status, named = voice["status"], voice["name"] is not None
    if status == brief.LEFT_OUT:
        return UNKNOWN
    if status == "minor":
        return voice["label"]
    if named and status in SURE and voice["is_me"]:
        return ME
    if named and (status in SURE or (status == "suggested" and voice["shared"])):
        if not voice["is_me"]:
            person = {"ref": voice["uid"], "name": voice["name"]}
            if voice["org"]:
                person["org"] = voice["org"]
            return {**person, "sure": status in SURE}
    return {"ref": f"v{voice['id']}", "name": voice["label"], "sure": False}


def _talk(store: Store, meeting_id: int, voices: dict, named: set[int]) -> dict:
    """Your share of the talk, and who else was there with theirs: the talk listed in the
    meeting (as on its page), and voices a task or a fact is by or about even if they said
    nothing."""
    said = brief.talk(store, meeting_id)
    out = brief.unlisted(store, meeting_id)
    total = sum(long for voice, long in said.items() if voice not in out)
    mine, people = 0, {}
    for voice in voices.values():
        long = said.get(voice["id"], 0)  # a voice left out is nobody: none of its talk counts
        who = _who(voice)
        if who == ME:
            mine += long
        elif isinstance(who, dict) and (long or voice["id"] in named):
            person = people.setdefault(who["ref"], {**who, "long": 0})
            person["long"] += long
            person["sure"] = person["sure"] or who["sure"]
    listed = sorted(people.values(), key=lambda person: (-person["long"], person["name"]))
    for person in listed:
        long = person.pop("long")
        if total:
            person["share"] = round(long / total, 2)
    talk: dict = {"share": round(mine / total, 2)} if total else {}
    talk["people"] = listed  # nobody now, too: who was there before isn't, any more
    return talk


def _task(item: sqlite3.Row, owner: str) -> dict:
    task = {"ref": item["uid"], "title": item["task"], "owner": owner}
    if day := _day(item["due"]):
        task["due"] = day
    task["done"] = item["status"] == "done"
    if item["said_at"] is not None:
        task["sec"] = _number(item["said_at"])
    return task


def _day(due: str | None) -> str | None:
    """A due date ZachCore can file: only one that is already a day."""
    due = (due or "").strip()
    return due if re.fullmatch(r"\d{4}-\d\d-\d\d", due) else None


def _facts(content: dict, client: str | None, voice_of, me: set[str], named: set[int]) -> list:
    """The summary's lasting facts about the people there (not you) and the client, one per
    whom and what: the one said last."""
    found = content.get("facts") if isinstance(content.get("facts"), list) else []
    best: dict[str, tuple] = {}
    for n, fact in enumerate(found):
        if not isinstance(fact, dict):
            continue
        about, name, value = (_text(fact.get(key)) for key in ("about", "name", "value"))
        if not (about and name and value) or _folded(about) in (*me, _folded(UNKNOWN)):
            continue
        if _folded(about) == CLIENT:
            if not client:
                continue
            about = CLIENT
        elif (voice := voice_of(fact.get("voice"), about)) is not None:
            who = _who(voice)
            if not isinstance(who, dict):
                continue  # you, or a voice that isn't listed
            named.add(voice["id"])
            about = who["ref"]
        key = hashlib.sha256(f"{about}\x1f{name.casefold()}".encode()).hexdigest()[:16]
        said = {"ref": f"f{key}", "about": about, "name": name, "value": value}
        sec = _seconds(fact.get("said_at"))
        if sec is not None:
            said["sec"] = _number(sec)
        if quote := _text(fact.get("quote")):
            said["quote"] = quote
        rank = (-1.0 if sec is None else sec, n)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, said)
    return [said for _, said in sorted(best.values(), key=lambda kept: kept[0])]


def _said(content: dict, key: str, letter: str) -> list[dict]:
    """Decisions or questions, with the seconds kept beside them ("decisions_said_at")."""
    texts = content.get(key) if isinstance(content.get(key), list) else []
    seconds = content.get(f"{key}_said_at")
    return _listed(texts, seconds if isinstance(seconds, list) else [], letter)


def _listed(texts: list, seconds: list, letter: str) -> list[dict]:
    """Each by its place in the list ("d1"): a new summary's first decision is the first."""
    listed = []
    for n, text in enumerate(texts):
        if not (text := _text(text)):
            continue
        entry = {"ref": f"{letter}{n + 1}", "text": text}
        sec = _seconds(seconds[n]) if n < len(seconds) else None
        if sec is not None:
            entry["sec"] = _number(sec)
        listed.append(entry)
    return listed


def _overview(overview, notes: list[sqlite3.Row]) -> str:
    """The summary's overview, then your notes and bookmarks: "- 12:30 text"."""
    parts = [overview.strip()] if isinstance(overview, str) and overview.strip() else []
    if notes:
        parts.append("\n".join([MY_NOTES, *(f"- {_clock(n['at'])} {_note(n)}" for n in notes)]))
    return "\n\n".join(parts)


def _note(note: sqlite3.Row) -> str:
    """A note on one line of the list: its other lines go under it."""
    lines = [line.strip() for line in note["text"].splitlines() if line.strip()]
    return "\n  ".join(lines) if lines else BOOKMARK


def _text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _folded(name: str) -> str:
    return " ".join(name.casefold().split())


def _seconds(value) -> float | None:
    """Seconds into the meeting, from a number or a stamp such as "12:34" or "[1:02:03]"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str) or not re.fullmatch(
        r"\[?\d+(:\d+){0,2}(\.\d+)?\]?", value.strip()
    ):
        return None
    seconds = 0.0
    for part in value.strip().strip("[]").split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def _number(seconds: float) -> int | float:
    seconds = round(seconds, 1)
    return int(seconds) if seconds.is_integer() else seconds


def _clock(seconds: float) -> str:
    s = int(seconds)
    return (
        f"{s // 60:02d}:{s % 60:02d}"
        if s < 3600
        else f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    )
