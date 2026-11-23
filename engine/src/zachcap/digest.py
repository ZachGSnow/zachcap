"""A work log: a day's or a week's meetings, who was there, what happened, the action items,
and a draft time entry for each meeting.

Built from what ZachCap already has (summaries, action items, named speakers), without a
call to Claude, so it is free, instant and can't invent anything. Time entries round each
meeting up to the quarter hour, the usual billing increment.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

from .store import Store

BILLING_MINUTES = 15
NOTE_LENGTH = 160


def period_range(period: str, day: date) -> tuple[date, date]:
    """The first and last day of the period containing ``day`` (weeks start on Monday)."""
    if period == "week":
        monday = day - timedelta(days=day.weekday())
        return monday, monday + timedelta(days=6)
    return day, day


def billable_hours(seconds: float | None) -> float:
    """Meeting time rounded up to the billing increment (a quarter hour), at least one."""
    minutes = max((seconds or 0) / 60, 1)
    return math.ceil(minutes / BILLING_MINUTES) * BILLING_MINUTES / 60


ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "st", "vs", "etc", "e.g", "i.e", "approx", "no", "jr"}


def first_sentence(text: str, limit: int = NOTE_LENGTH) -> str:
    """The overview's first sentence, for a one-line time entry ("Met Dr. Lee about the
    rollout." is one sentence, and so is one with an initial, "J. Smith")."""
    text = " ".join(text.split())
    sentence = text
    for end in re.finditer(r"[.!?](?=\s|$)", text):
        words = text[: end.start()].split()
        word = words[-1].lower() if words else ""
        if end.group() == "." and (word in ABBREVIATIONS or len(word) == 1):
            continue
        sentence = text[: end.end()]
        break
    return sentence if len(sentence) <= limit else sentence[: limit - 3].rstrip(" ,;:") + "..."


def _time_range(started: datetime, seconds: float | None) -> str:
    if not seconds:
        return f"{started:%H:%M}"
    return f"{started:%H:%M}-{started + timedelta(seconds=seconds):%H:%M}"


def build(store: Store, period: str, day: date, client: str | None = None) -> dict:
    """The work log for the day or week containing ``day`` (only ``client``'s meetings, if
    given)."""
    from . import clients, topics

    start, end = period_range(period, day)
    said = topics.counts(store)
    me = store.me()["name"]
    # Only the period's meetings are read: the log is rebuilt on every change to one of them.
    meetings = store.meetings_between(start.isoformat(), end.isoformat())
    client_of = clients.of_meetings(store, meetings)
    meetings = [m for m in meetings if clients.matches(client_of[m["id"]], client)]
    participants = store.participants([m["id"] for m in meetings])
    days: dict[str, list[dict]] = {}
    your_items: list[dict] = []
    waiting_on: list[dict] = []
    for meeting in meetings:
        started = datetime.fromisoformat(meeting["started_at"])
        summary = store.summary(meeting["id"])
        content = summary["content"] if summary else {}
        overview = content.get("overview") or ""
        items = [
            {key: item[key] for key in ("id", "owner", "task", "due", "status", "kind", "said_at")}
            for item in store.action_items(meeting_id=meeting["id"])
        ]
        for item in items:
            if item["status"] == "open":
                placed = {**item, "meeting_id": meeting["id"], "meeting_title": meeting["title"]}
                (your_items if item["owner"] == me else waiting_on).append(placed)
        days.setdefault(meeting["started_at"][:10], []).append(
            {
                "id": meeting["id"],
                "title": meeting["title"],
                "started_at": meeting["started_at"],
                "time": _time_range(started, meeting["duration"]),
                "seconds": round(meeting["duration"] or 0, 1),
                "hours": billable_hours(meeting["duration"]),
                "people": [p for p in participants.get(meeting["id"], []) if p != me],
                "overview": overview,
                "note": first_sentence(overview) if overview else "",
                "decisions": list(content.get("decisions") or []),
                "items": items,
                "status": meeting["status"],
                "ticket": store.column(meeting, "ticket"),
                "client": client_of[meeting["id"]]["client"],
                # Topics said in it, most said first.
                "topics": sorted(
                    (
                        {"topic": name, "count": per[meeting["id"]]}
                        for name, per in said.items()
                        if meeting["id"] in per
                    ),
                    key=lambda topic: -topic["count"],
                ),
            }
        )
    every = [m for listed in days.values() for m in listed]
    log = {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "title": _title(period, start) + (f" for {client}" if client else ""),
        "client": client,
        "me": me,
        "totals": {
            "meetings": len(every),
            "seconds": round(sum(m["seconds"] for m in every), 1),
            "hours": sum(m["hours"] for m in every),
        },
        "days": [
            {
                "day": key,
                "label": f"{date.fromisoformat(key):%A, %B} {date.fromisoformat(key).day}",
                "meetings": listed,
            }
            for key, listed in days.items()
        ],  # fmt: skip
        "your_items": your_items,
        "waiting_on": waiting_on,
    }
    log["markdown"] = markdown(log)
    return log


def _title(period: str, start: date) -> str:
    if period == "week":
        return f"Work log: week of {start:%B} {start.day}, {start.year}"
    return f"Work log: {start:%A, %B} {start.day}, {start.year}"


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _item(item: dict, with_owner: bool = True, with_meeting: bool = False) -> str:
    box = "x" if item["status"] == "done" else " "
    owner = f"**{item['owner']}**: " if with_owner else ""
    due = f" (due {item['due']})" if item.get("due") else ""
    where = f", from {item['meeting_title']}" if with_meeting else ""
    return f"- [{box}] {owner}{item['task']}{due}{where}"


def markdown(log: dict, cite: bool = False) -> str:
    """The log as Markdown. ``cite`` adds each meeting's id ([M12]) for Claude to cite."""
    totals = log["totals"]
    lines = [f"# {log['title']}", ""]
    if not totals["meetings"]:
        return "\n".join(lines + ["No meetings recorded."]) + "\n"
    count = f"{totals['meetings']} meeting{'' if totals['meetings'] == 1 else 's'}"
    lines += [f"{count}, {_duration(totals['seconds'])}, {totals['hours']:g} h to bill.", ""]

    week = log["period"] == "week"
    # Client and Ticket columns once any meeting has one (a log without stays as it was);
    # a log for one client doesn't repeat it on every line.
    every = [m for day in log["days"] for m in day["meetings"]]
    ticketed = any(m.get("ticket") for m in every)
    for_clients = not log.get("client") and any(m.get("client") for m in every)
    columns = [
        "Time",
        "Hours",
        *(["Client"] if for_clients else []),
        *(["Ticket"] if ticketed else []),
        "Meeting",
        "Note",
    ]
    if week:
        columns.insert(0, "Day")
    lines += ["## Time entries", ""]
    lines += ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for day in log["days"]:
        for m in day["meetings"]:
            row = [
                m["time"],
                f"{m['hours']:g}",
                *([_cell(m.get("client") or "")] if for_clients else []),
                *([_cell(m.get("ticket") or "")] if ticketed else []),
                _cell(m["title"]),
                _cell(m["note"]),
            ]
            if week:
                row.insert(0, f"{date.fromisoformat(day['day']):%a %d}")
            lines.append("| " + " | ".join(row) + " |")

    lines += ["", "## Meetings"]
    for day in log["days"]:
        if week:
            lines += ["", f"### {day['label']}"]
        for m in day["meetings"]:
            about = [
                *([m["client"]] if m.get("client") and not log.get("client") else []),
                *([f"ticket {m['ticket']}"] if m.get("ticket") else []),
            ]
            more = f" ({', '.join(about)})" if about else ""
            heading = f"{'####' if week else '###'} {m['time']} {m['title']}{more}"
            lines += ["", heading + (f" [M{m['id']}]" if cite else ""), ""]
            if m["people"]:
                lines += [f"With {', '.join(m['people'])}.", ""]
            lines.append(m["overview"] or "Not summarized yet.")
            if m.get("topics"):
                named = ", ".join(f"{t['topic']} ({t['count']})" for t in m["topics"])
                lines += ["", f"Topics: {named}."]
            if m["decisions"]:
                lines += ["", "Decisions:"] + [f"- {d}" for d in m["decisions"]]
            if m["items"]:
                lines += ["", "Action items:"] + [_item(i) for i in m["items"]]

    if log["your_items"]:
        lines += ["", f"## Your open items ({log['me']})", ""]
        lines += [_item(i, with_owner=False, with_meeting=True) for i in log["your_items"]]
    if log["waiting_on"]:
        lines += ["", "## Waiting on others", ""]
        lines += [_item(i, with_meeting=True) for i in log["waiting_on"]]
    return "\n".join(lines).rstrip() + "\n"


def file_name(period: str, day: date, client: str | None = None) -> str:
    """ "Work log 2026-11-23.md" for a day, "Work log 2026-W48.md" for a week (ISO weeks);
    one client's is "Work log 2026-11-23 Contoso.md"."""
    if period == "week":
        year, week, _ = day.isocalendar()
        when = f"{year}-W{week:02d}"
    else:
        when = day.isoformat()
    # Characters Windows won't have in a file name are left out of the client's.
    for_client = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", client or "").strip(" .")
    return f"Work log {when}{' ' + for_client if for_client else ''}.md"
