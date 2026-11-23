import threading
from pathlib import Path

import numpy as np

from zachcap.store import Store
from zachcap.voiceprint import Match


def test_meeting_round_trip_with_search_and_voiceprints(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    assert store.me()["name"] == "You"

    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path)
    dana = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting, "system", "Speaker 1", np.ones(4), 12.0, (1.0, 4.0), Match(None, 0.1, "unknown")
    )
    store.add_segments(meeting, [(speaker, "system", 1.0, 4.0, "The cutover is Friday night", 0.9)])
    assert store.refresh_status(meeting) == "needs_names"
    assert store.transcript(meeting)[0]["speaker"] == "Speaker 1"

    store.set_speaker_person(speaker, dana, "confirmed")
    store.add_voice_sample(dana, meeting, np.ones(4), "test-model", 12.0)
    assert store.refresh_status(meeting) == "ready"
    assert store.transcript(meeting)[0]["speaker"] == "Dana"
    assert list(store.voice_library("test-model")) == [dana]

    hits = store.search("cutover")
    assert hits and hits[0]["speaker"] == "Dana"
    store.close()


def make_meeting(store: Store, folder: Path) -> tuple[int, int]:
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", folder, duration=60.0)
    dana = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting, "system", "Dana", np.ones(4), 6.0, None, Match(dana, 0.9, "auto")
    )
    store.add_segments(
        meeting,
        [
            (speaker, "system", 1.0, 4.0, "First line", 0.9),
            (speaker, "system", 5.0, 8.0, "Second line", 0.9),
        ],
    )
    store.add_voice_sample(dana, meeting, np.ones(4), "test-model", 6.0)
    return meeting, dana


def test_people_stats_rename_and_merge(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, dana = make_meeting(store, tmp_path)
    row = next(p for p in store.people_stats() if p["name"] == "Dana")
    assert (row["meetings"], row["talk_seconds"], row["samples"]) == (1, 6.0, 1)
    assert store.participants() == {meeting: ["Dana"]}

    # Renaming to a name someone already has merges the two people.
    other = store.person_id("Dana Lee")
    assert store.rename_person(dana, "Dana Lee") == other
    assert store.transcript(meeting)[0]["speaker"] == "Dana Lee"
    assert list(store.voice_library("test-model")) == [other]
    store.close()


def test_moving_one_line_to_another_person(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, _ = make_meeting(store, tmp_path)
    second = store.transcript(meeting)[1]

    store.reassign_segment(second["id"], "Mark")

    assert [row["speaker"] for row in store.transcript(meeting)] == ["Dana", "Mark"]
    store.reassign_segment(store.transcript(meeting)[0]["id"], "me")
    assert store.transcript(meeting)[0]["speaker"] == "You"
    store.close()


def test_deleting_a_meeting_forgets_what_was_learned_from_it(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, _ = make_meeting(store, tmp_path)
    store.replace_action_items(meeting, [{"owner": "Dana", "task": "Send it", "kind": "action"}])

    store.delete_meeting(meeting)

    assert store.meetings() == [] and store.action_items() == []
    assert store.voice_library("test-model") == {}
    assert store.search("line") == []
    store.close()


def test_a_connection_can_serve_a_request_on_another_thread(tmp_path: Path):
    # FastAPI opens a request's database connection on one worker thread and runs the
    # endpoint on another. SQLite's default check broke audio playback in the app.
    store = Store(tmp_path / "test.db")
    result: dict = {}
    worker = threading.Thread(target=lambda: result.update(meetings=store.meetings()))
    worker.start()
    worker.join()
    assert result == {"meetings": []}
    store.close()


def test_settings_and_stats(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    assert store.setting("language", "en") == "en"
    store.set_setting("language", "auto")
    store.set_setting("language", "de")
    assert store.setting("language") == "de"
    meeting, _ = make_meeting(store, tmp_path)
    store.replace_action_items(meeting, [{"owner": "Dana", "task": "Send it"}])
    stats = store.stats()
    assert (stats["meetings"], stats["seconds"], stats["open_actions"], stats["people"]) == (
        1,
        60.0,
        1,
        1,
    )
    store.close()
