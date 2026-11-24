from pathlib import Path

import numpy as np
import pytest

from zachcap import pipeline
from zachcap.pipeline import merge_same_person
from zachcap.store import Store
from zachcap.voiceprint import Match


def test_a_failed_reprocess_keeps_the_last_good_transcript(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path / "moved-away")
    speaker = store.add_speaker(
        meeting, "system", "Speaker 1", np.ones(4), 6.0, None, Match(None, 0.0, "unknown")
    )
    store.add_segments(meeting, [(speaker, "system", 1.0, 4.0, "Kept", 0.9)])
    store.refresh_status(meeting)

    with pytest.raises(RuntimeError, match="audio is missing"):
        pipeline.process(store, meeting, log=lambda message: None)

    assert [row["text"] for row in store.transcript(meeting)] == ["Kept"]
    assert store.meeting(meeting)["status"] == "needs_names"
    store.close()


def test_a_meeting_that_never_worked_is_marked_failed(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path / "empty")

    with pytest.raises(RuntimeError, match="audio is missing"):
        pipeline.process(store, meeting, log=lambda message: None)

    assert store.meeting(meeting)["status"] == "failed"
    store.close()


def row(speaker, start, end, text, track="system"):
    return (speaker, track, start, end, text, 0.9)


def test_a_voice_split_across_two_clusters_reads_as_one_line():
    who = {1: ("person", 7), 2: ("person", 7), 3: ("person", 8)}
    rows = [
        row(1, 15.0, 15.2, "The"),
        row(2, 15.3, 20.0, "printer issue is fixed."),
        row(3, 20.5, 22.0, "Great."),
        row(2, 26.0, 27.0, "Later, after a pause."),
    ]

    merged = merge_same_person(rows, who)

    assert [(r[0], r[2], r[3], r[4]) for r in merged] == [
        (1, 15.0, 20.0, "The printer issue is fixed."),
        (3, 20.5, 22.0, "Great."),
        (2, 26.0, 27.0, "Later, after a pause."),
    ]


def test_unknown_speakers_tracks_and_gaps_keep_lines_apart():
    who = {1: ("speaker", 1), 2: ("speaker", 2), 4: ("person", 9), 5: ("person", 9)}
    rows = [
        row(1, 0.0, 1.0, "One"),
        row(2, 1.2, 2.0, "Two"),  # a different unknown speaker
        row(4, 3.0, 4.0, "Three", track="mic"),
        row(5, 4.2, 5.0, "Four", track="system"),  # same person, other track
        row(None, 5.1, 5.5, "Five"),
        row(None, 5.6, 6.0, "Six"),  # unassigned words are never merged
    ]
    assert len(merge_same_person(rows, who)) == 6


def test_words_to_know_are_spelled_before_the_database_is_locked(tmp_path: Path, monkeypatch):
    from zachcap import vocabulary
    from zachcap.diarize import Diarization
    from zachcap.transcript import Turn, Word

    store = Store(tmp_path / "test.db")
    store.set_setting("vocabulary", "Proxmox: prox mocks")
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", tmp_path)
    said = [Word(0.0, 0.3, " The"), Word(0.3, 0.6, " prox"), Word(0.6, 0.9, " mocks"),
            Word(0.9, 1.2, " host.")]  # fmt: skip
    monkeypatch.setattr(pipeline, "_load_tracks", lambda folder, log: {"system": np.zeros(16000)})
    monkeypatch.setattr(pipeline, "_transcribe", lambda *args: {"system": said})
    monkeypatch.setattr(
        pipeline,
        "_diarize",
        lambda *args: ({"system": Diarization([Turn(0.0, 1.2, "system")], {})}, False),
    )
    while_locked = []
    correct = vocabulary.Vocabulary.correct

    def watched(self, text):
        while_locked.append(store._batch > 0)
        return correct(self, text)

    monkeypatch.setattr(vocabulary.Vocabulary, "correct", watched)

    pipeline.process(store, meeting, log=lambda message: None)

    assert [row["text"] for row in store.transcript(meeting)] == ["The Proxmox host."]
    assert while_locked and not any(while_locked)
    store.close()
