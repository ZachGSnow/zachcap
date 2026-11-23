"""The work log: a day's or a week's meetings, action items and time entries."""

from datetime import date
from pathlib import Path

import pytest

from zachcap import assistant, digest, export
from zachcap.store import Store
from zachcap.voiceprint import Match


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.set_setting("export_folder", "")

    def meeting(title, started, minutes, people=(), overview=None, items=()):
        meeting_id = store.create_meeting(title, started, tmp_path / title, duration=minutes * 60)
        for name in people:
            person = store.person_id(name)
            speaker = store.add_speaker(
                meeting_id, "system", name, None, 5.0, None, Match(person, 1.0, "confirmed")
            )
            store.add_segments(meeting_id, [(speaker, "system", 1.0, 3.0, "Hello.", 0.9)])
        if overview:
            content = {"title": title, "overview": overview, "decisions": ["Go Friday"]}
            store.save_summary(meeting_id, "claude-opus-5-5", content, 1, 1, 0.0)
            store.replace_action_items(meeting_id, list(items))
        store.update_meeting(meeting_id, status="ready")
        return meeting_id

    ids = {
        "cutover": meeting(
            "Contoso | cutover sync",
            "2026-11-23T09:00:00-05:00",
            37,
            people=("Dana", "Mark"),
            overview="Planned the Friday cutover. Dana owns the change notice.",
            items=(
                {"owner": "You", "task": "Push the VPN profile", "due": "Wednesday"},
                {"owner": "Dana", "task": "Send the change notice"},
            ),
        ),
        "standup": meeting("Standup", "2026-11-23T13:30:00-05:00", 15),
        "tuesday": meeting("Vendor call", "2026-11-24T10:00:00-05:00", 61, overview="Quotes."),
        "last week": meeting("Old sync", "2026-11-22T10:00:00-05:00", 30),
    }
    store.ids = ids
    yield store
    store.close()


def test_a_day_lists_its_meetings_with_time_entries_and_open_items(store: Store):
    log = digest.build(store, "day", date(2026, 11, 23))

    meetings = log["days"][0]["meetings"]
    assert [m["title"] for m in meetings] == ["Contoso | cutover sync", "Standup"]
    first = meetings[0]
    assert (first["time"], first["hours"]) == ("09:00-09:37", 0.75)
    assert sorted(first["people"]) == ["Dana", "Mark"]  # you aren't listed
    assert first["note"] == "Planned the Friday cutover."
    assert meetings[1]["note"] == "" and meetings[1]["hours"] == 0.25
    assert log["totals"] == {"meetings": 2, "seconds": 52 * 60, "hours": 1.0}
    assert [i["task"] for i in log["your_items"]] == ["Push the VPN profile"]
    assert [i["owner"] for i in log["waiting_on"]] == ["Dana"]

    md = log["markdown"]
    assert md.startswith("# Work log: Monday, November 23, 2026")
    assert "2 meetings, 52 min, 1 h to bill." in md
    assert r"| 09:00-09:37 | 0.75 | Contoso \| cutover sync | Planned the Friday cutover. |" in md
    assert "- [ ] Push the VPN profile (due Wednesday), from Contoso | cutover sync" in md
    assert "Not summarized yet." in md


def test_a_week_runs_monday_to_sunday(store: Store):
    log = digest.build(store, "week", date(2026, 11, 25))

    assert (log["start"], log["end"]) == ("2026-11-23", "2026-11-29")
    assert [d["day"] for d in log["days"]] == ["2026-11-23", "2026-11-24"]
    assert log["totals"]["hours"] == 1.0 + 1.25  # 61 minutes bill as an hour and a quarter
    assert "| Day | Time | Hours | Meeting | Note |" in log["markdown"]
    assert "### Tuesday, November 24" in log["markdown"]


def test_an_empty_day_says_so(store: Store):
    log = digest.build(store, "day", date(2026, 11, 21))
    assert log["totals"]["meetings"] == 0 and "No meetings recorded." in log["markdown"]


def test_rounding_sentences_and_file_names():
    assert [digest.billable_hours(s) for s in (None, 0, 900, 960, 3600)] == [
        0.25,
        0.25,
        0.25,
        0.5,
        1.0,
    ]
    assert digest.first_sentence("Fixed it. Then lunch.") == "Fixed it."
    long = digest.first_sentence("word " * 80)
    assert len(long) <= digest.NOTE_LENGTH and long.endswith("...")
    assert digest.file_name("day", date(2026, 11, 23)) == "Work log 2026-11-23.md"
    assert digest.file_name("week", date(2026, 11, 23)) == "Work log 2026-W48.md"


def test_ask_and_mcp_get_it_with_citations(store: Store):
    cited = assistant.work_log(store, "day", "2026-11-23")
    assert f"### 09:00-09:37 Contoso | cutover sync [M{store.ids['cutover']}]" in cited
    with pytest.raises(ValueError):
        assistant.work_log(store, "month", None)
    with pytest.raises(ValueError):
        assistant.work_log(store, "day", "Monday")


def test_the_export_folder_keeps_work_logs_and_forgets_deleted_meetings(
    store: Store, tmp_path: Path
):
    folder = tmp_path / "vault"
    folder.mkdir()
    store.set_setting("export_folder", str(folder))
    export.auto_export(store, store.ids["cutover"])
    assert not list(folder.glob("Work log*"))  # only when asked for

    store.set_setting("export_work_log", "1")
    export.auto_export(store, store.ids["standup"])
    assert {p.name for p in folder.glob("Work log*")} == {
        "Work log 2026-11-23.md",
        "Work log 2026-W48.md",
    }

    meeting = store.meeting(store.ids["standup"])
    store.delete_meeting(store.ids["standup"])
    export.forget(store, store.ids["standup"], date.fromisoformat(meeting["started_at"][:10]))
    assert not list(folder.glob(f"* (M{store.ids['standup']}).md"))
    assert "Standup" not in (folder / "Work log 2026-11-23.md").read_text(encoding="utf-8")
