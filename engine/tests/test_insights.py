from pathlib import Path

from zachcap.insights import meeting_insights
from zachcap.store import Store
from zachcap.voiceprint import Match


def test_pace_stretches_questions_and_interruptions(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-20T09:00:00", tmp_path)
    me = store.add_speaker(meeting, "mic", "You", None, 0, None, Match(store.me()["id"], 1, "auto"))
    dana = store.add_speaker(
        meeting, "system", "Dana", None, 0, None, Match(store.person_id("Dana"), 1, "auto")
    )
    store.add_segments(
        meeting,
        [
            (dana, "system", 0.0, 10.0, "one two three four five six seven eight nine ten", 0.9),
            (dana, "system", 11.0, 30.0, "still Dana talking after a short pause here", 0.9),
            (me, "mic", 25.0, 28.0, "Sorry, can I jump in?", 0.9),  # over Dana: interruption
            (me, "mic", 40.0, 42.0, "When is the cutover?", 0.9),
            (dana, "system", 43.0, 45.0, "Friday.", 0.9),
        ],
    )

    stats = meeting_insights(store, meeting)

    dana_stats, my_stats = stats["speakers"]
    assert dana_stats["speaker"] == "Dana" and dana_stats["longest"] == 30.0
    assert dana_stats["longest_at"] == 0.0 and dana_stats["interruptions"] == 0
    assert dana_stats["words_per_minute"] == round(19 / 31 * 60)  # 19 words in 31 s of talking
    assert (my_stats["questions"], my_stats["interruptions"]) == (2, 1)
    assert my_stats["words_per_minute"] is None  # 5 seconds is too little to judge
    assert stats["longest"] == {"speaker": "Dana", "seconds": 30.0, "at": 0.0}
    assert stats["questions"] == 2
    store.close()


def test_an_empty_meeting_has_no_insights(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting = store.create_meeting("Sync", "2026-11-20T09:00:00", tmp_path)
    assert meeting_insights(store, meeting) == {"speakers": [], "longest": None, "questions": 0}
    store.close()
