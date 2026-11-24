"""Time how long a person's move to another client takes on a large library.

Builds a synthetic library of 1,500 meetings in a scratch folder, each with Dana Lee and
Mark Ruiz (both Contoso) named, then times clients.keep_history for Dana (every meeting she
was in keeps its client), and the whole change through the API. Three runs each, on a new
library every time; nothing outside the scratch folders is touched.

    uv run python scripts/bench_clients.py
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time
from pathlib import Path

MEETINGS = 1500


def build(folder: Path):
    from zachcap.store import Store
    from zachcap.voiceprint import Match

    store = Store(folder / "zachcap.db")
    with store.transaction():
        people = {name: store.person_id(name) for name in ("Dana Lee", "Mark Ruiz")}
        for number in range(MEETINGS):
            day = f"2026-{1 + number % 11:02d}-{1 + number % 28:02d}"
            meeting = store.create_meeting(
                f"Call {number}", f"{day}T09:00:00", folder / f"m{number}", duration=600
            )
            rows = []
            for at, (name, person) in enumerate(people.items()):
                speaker = store.add_speaker(
                    meeting, "system", name, None, 5.0, None, Match(person, 1.0, "confirmed")
                )
                rows.append((speaker, "system", at * 5.0, at * 5.0 + 4, "Hello.", 0.9))
            store.add_segments(meeting, rows)
            store.update_meeting(meeting, status="ready")
        for person in people.values():
            store.set_client(person, "Contoso")
    return store, people["Dana Lee"]


def main() -> None:
    from fastapi.testclient import TestClient

    from zachcap import clients
    from zachcap.api import create_app

    runs: dict[str, list[float]] = {"keep_history": [], "PUT /api/people/{id}/client": []}
    for _ in range(3):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
            store, dana = build(Path(scratch))
            started = time.perf_counter()
            kept = clients.keep_history(store, dana)
            runs["keep_history"].append(time.perf_counter() - started)
            assert len(kept) == MEETINGS, len(kept)
            store.close()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
            os.environ["ZACHCAP_HOME"] = scratch
            store, dana = build(Path(scratch))
            store.close()
            api = TestClient(create_app("bench"))
            started = time.perf_counter()
            answer = api.put(
                f"/api/people/{dana}/client",
                headers={"Authorization": "Bearer bench"},
                json={"client": "Fabrikam"},
            )
            runs["PUT /api/people/{id}/client"].append(time.perf_counter() - started)
            assert answer.status_code == 200, answer.text
            api.close()
    for name, times in runs.items():
        print(
            f"{name}: median {statistics.median(times):.3f} s "
            f"({', '.join(f'{t:.3f}' for t in times)}) for {MEETINGS} meetings"
        )


if __name__ == "__main__":
    main()
