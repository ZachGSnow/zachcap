"""How the conversation went, per speaker: pace, longest stretch, questions, interruptions.

Worked out from the transcript alone. Lines by the same person less than STRETCH_GAP apart
count as one stretch. An interruption is a line that starts while someone else's line on
the other side of the call is still going (each side is one track, so overlaps only show up
between your mic and the call audio).
"""

from __future__ import annotations

from .store import Store

STRETCH_GAP = 2.0  # seconds of silence that still counts as the same stretch of talking
OVERLAP = 0.5  # seconds of talking over someone before it counts as an interruption
MIN_PACE_SECONDS = 20  # too little speech gives a meaningless words-per-minute


def meeting_insights(store: Store, meeting_id: int) -> dict:
    rows = store.transcript(meeting_id)
    people: dict[str, dict] = {}
    for row in rows:
        stats = people.setdefault(
            row["speaker"],
            {"speaker": row["speaker"], "words": 0, "seconds": 0.0, "questions": 0,
             "interruptions": 0, "longest": 0.0, "longest_at": None},
        )  # fmt: skip
        stats["words"] += len(row["text"].split())
        stats["seconds"] += max(0.0, row["end"] - row["start"])
        stats["questions"] += row["text"].rstrip().endswith("?")

    # Stretches: one person talking with no more than a short pause.
    stretch: dict | None = None
    for row in [*rows, None]:
        if (
            row is not None
            and stretch is not None
            and row["speaker"] == stretch["speaker"]
            and row["start"] - stretch["end"] <= STRETCH_GAP
        ):
            stretch["end"] = max(stretch["end"], row["end"])
            continue
        if stretch is not None:
            stats = people[stretch["speaker"]]
            length = stretch["end"] - stretch["start"]
            if length > stats["longest"]:
                stats["longest"], stats["longest_at"] = length, stretch["start"]
        if row is not None:
            stretch = {"speaker": row["speaker"], "start": row["start"], "end": row["end"]}

    # Interruptions: starting while someone on the other track is still talking.
    for index, row in enumerate(rows):
        for earlier in reversed(rows[max(0, index - 6) : index]):
            if (
                earlier["speaker"] != row["speaker"]
                and earlier["track"] != row["track"]
                and earlier["start"] < row["start"] < earlier["end"] - OVERLAP
            ):
                people[row["speaker"]]["interruptions"] += 1
                break

    speakers = sorted(people.values(), key=lambda s: -s["seconds"])
    for stats in speakers:
        stats["seconds"] = round(stats["seconds"], 1)
        stats["longest"] = round(stats["longest"], 1)
        enough = stats["seconds"] >= MIN_PACE_SECONDS
        stats["words_per_minute"] = (
            round(stats["words"] / stats["seconds"] * 60) if enough else None
        )
    longest = max(speakers, key=lambda s: s["longest"], default=None)
    return {
        "speakers": speakers,
        "longest": {"speaker": longest["speaker"], "seconds": longest["longest"],
                    "at": longest["longest_at"]} if longest and longest["longest"] else None,
        "questions": sum(s["questions"] for s in speakers),
    }  # fmt: skip
