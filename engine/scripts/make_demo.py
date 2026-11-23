"""Build a demo library from the synthetic meetings, for screenshots and trying the app.

Imports the generated test meetings into ZACHCAP_HOME (set it to a scratch folder),
processes them, names speakers from each meeting's reference.json, and with
--summarize asks Claude for summaries (a few cents; synthetic content only).

    $env:ZACHCAP_HOME = "$env:TEMP\\zachcap-demo"
    uv run python scripts/make_demo.py --summarize
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from zachcap import paths, pipeline
from zachcap.store import Store

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "generated"
MEETINGS = [
    ("meeting1", "Contoso firewall cutover sync", timedelta(days=1, hours=2)),
    ("meeting2", "Helpdesk weekly review", timedelta(hours=3)),
]


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def name_from_reference(store: Store, meeting_id: int, reference: dict) -> None:
    """Name each detected speaker after the scripted speaker they overlap most."""
    lines = store.transcript(meeting_id)
    for speaker in store.speakers(meeting_id):
        if speaker["status"] in ("auto", "confirmed"):
            continue
        spans = [
            (row["start"], row["end"])
            for row in lines
            if row["meeting_speaker_id"] == speaker["id"]
        ]
        if not spans:
            continue
        votes: dict[str, float] = {}
        for ref in reference["segments"]:
            for span in spans:
                votes[ref["speaker"]] = votes.get(ref["speaker"], 0.0) + overlap(
                    span, (ref["start"], ref["end"])
                )
        best = max(votes, key=votes.get)
        pipeline.name_speaker(store, meeting_id, str(speaker["id"]), best)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summarize", action="store_true", help="ask Claude for summaries")
    args = parser.parse_args()
    if not os.environ.get("ZACHCAP_HOME"):
        raise SystemExit("Set ZACHCAP_HOME to a scratch folder first; this is demo data.")

    store = Store()
    now = datetime.now().astimezone()
    for folder_name, title, ago in MEETINGS:
        source = FIXTURES / folder_name
        if not source.exists():
            raise SystemExit(f"Generate {source} first with scripts/make_meeting.py")
        folder = paths.meetings_dir() / folder_name
        shutil.copytree(source, folder, dirs_exist_ok=True)
        reference = json.loads((folder / "reference.json").read_text(encoding="utf-8"))
        started = (now - ago).isoformat(timespec="seconds")
        meeting_id = store.create_meeting(title, started, folder, duration=reference["duration"])
        pipeline.process(store, meeting_id, log=lambda message: print("   ", message))
        name_from_reference(store, meeting_id, reference)
        store.refresh_status(meeting_id)
        print(f"{title}: {[s['display_name'] for s in store.speakers(meeting_id)]}")
        if args.summarize:
            from zachcap.summarize import summarize

            summary = summarize(store, meeting_id)
            print(f"    summary: {len(summary['action_items'])} action items")
    items = store.action_items()
    if items:
        store.set_action_status(items[-1]["id"], "done")
    store.close()


if __name__ == "__main__":
    main()
