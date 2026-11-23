import threading
from pathlib import Path

import numpy as np
import pytest

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


def test_a_failed_transaction_saves_nothing(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, _ = make_meeting(store, tmp_path)

    with pytest.raises(ValueError), store.transaction():
        store.clear_results(meeting)
        store.update_meeting(meeting, title="Renamed")
        raise ValueError("processing failed halfway")

    assert len(store.transcript(meeting)) == 2
    assert store.meeting(meeting)["title"] == "Sync"
    store.close()


def test_the_meeting_being_recorded_is_not_listed_yet(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    live = store.create_meeting("Live", "2026-11-22T20:00:00", tmp_path, status="recording")
    assert store.meetings() == [] and store.stats()["meetings"] == 0
    assert [m["id"] for m in store.meetings_with_status("recording")] == [live]
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


def test_correcting_a_name_moves_the_voiceprint(tmp_path: Path):
    from zachcap import pipeline

    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path)
    voice = np.array([0.6, 0.8, 0.0, 0.0], np.float32)
    speaker = store.add_speaker(
        meeting, "system", "Speaker 1", voice, 9.0, None, Match(None, 0.0, "unknown")
    )

    pipeline.name_speaker(store, meeting, str(speaker), "Mark")  # a slip
    pipeline.name_speaker(store, meeting, str(speaker), "Dana")  # corrected

    from zachcap.diarize import EMBEDDING_MODEL

    assert list(store.voice_library(EMBEDDING_MODEL)) == [store.person_id("Dana")]
    store.close()


def test_empty_voiceprints_are_not_learned(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path)
    store.add_voice_sample(store.person_id("Dana"), meeting, np.zeros(4), "test-model", 5.0)
    assert store.voice_library("test-model") == {}
    store.close()


def test_regenerating_a_summary_keeps_what_was_ticked_off(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, _ = make_meeting(store, tmp_path)
    store.replace_action_items(
        meeting,
        [
            {"owner": "Speaker 2", "task": "Send the firewall change window to the team"},
            {"owner": "You", "task": "Book the server room"},
        ],
    )
    first, second = store.action_items(meeting_id=meeting)
    store.set_action_status(first["id"], "done")
    store.set_action_status(second["id"], "done")
    done_at = store.action_items(meeting_id=meeting)[0]["done_at"]

    # Speaker 2 is now named and the task reworded; "Book the server room" was dropped.
    store.replace_action_items(
        meeting,
        [
            {"owner": "Dana", "task": "Send the firewall change window to the team."},
            {"owner": "Mark", "task": "Order the new switch"},
        ],
    )

    items = {row["task"]: row for row in store.action_items(meeting_id=meeting)}
    assert items["Send the firewall change window to the team."]["status"] == "done"
    assert items["Send the firewall change window to the team."]["done_at"] == done_at
    assert items["Order the new switch"]["status"] == "open"
    assert items["Book the server room"]["status"] == "done"  # kept, not lost
    store.close()


def test_a_line_can_move_to_an_unnamed_voice_without_inventing_a_person(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting, _ = make_meeting(store, tmp_path)
    unnamed = store.add_speaker(
        meeting, "system", "Speaker 2", np.ones(4), 3.0, None, Match(None, 0.0, "unknown")
    )
    line = store.transcript(meeting)[0]

    store.reassign_segment(line["id"], speaker_id=unnamed)

    assert store.transcript(meeting)[0]["speaker"] == "Speaker 2"
    assert "Speaker 2" not in [p["name"] for p in store.people()]
    other = store.create_meeting("Other", "2026-11-23T09:00:00", tmp_path)
    stranger = store.add_speaker(
        other, "system", "Speaker 1", None, 1.0, None, Match(None, 0.0, "unknown")
    )
    with pytest.raises(ValueError):
        store.reassign_segment(line["id"], speaker_id=stranger)
    store.close()


def test_voices_learned_from_deleted_meetings_are_forgotten(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.add_voice_sample(store.person_id("Dana"), 99, np.ones(4), "test-model", 5.0)
    assert store.delete_orphan_voice_samples() == 1
    assert store.voice_library("test-model") == {}
    store.close()
