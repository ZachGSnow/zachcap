from pathlib import Path

import pytest

from zachcap import export
from zachcap.store import Store
from zachcap.voiceprint import Match


@pytest.fixture
def meeting(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting_id = store.create_meeting(
        'Cutover: "Phase 2"?', "2026-11-20T09:05:00", tmp_path, duration=125.0
    )
    dana = store.person_id("Dana")
    speaker = store.add_speaker(
        meeting_id, "system", "Dana", None, 10.0, None, Match(dana, 1.0, "confirmed")
    )
    store.add_segments(
        meeting_id,
        [
            (speaker, "system", 1.0, 4.0, "The cutover is Friday.", 0.9),
            (speaker, "system", 61.25, 64.5, "I'll send the notice.", 0.9),
        ],
    )
    store.save_summary(
        meeting_id,
        "claude-opus-5-5",
        {"overview": "Cutover set for Friday.", "decisions": ["Friday 8 pm"], "open_questions": []},
        1,
        1,
        0.0,
    )
    store.replace_action_items(meeting_id, [{"owner": "Dana", "task": "Send the notice"}])
    store.add_note(meeting_id, 61.0, "note", "Check the backup line")
    store.add_note(meeting_id, 90.0, "bookmark")
    yield store, meeting_id
    store.close()


def test_markdown_has_the_summary_notes_and_transcript(meeting):
    store, meeting_id = meeting
    text = export.markdown(store, meeting_id)
    assert text.startswith('# Cutover: "Phase 2"?\n\nFriday November 20, 2026 09:05 | 2 min | Dana')
    for part in (
        "## Summary\n\nCutover set for Friday.",
        "## Decisions\n\n- Friday 8 pm",
        "- [ ] **Dana**: Send the notice",
        "## My notes\n\n- [01:01] Check the backup line\n- [01:30] Bookmarked moment",
        "**[01:01] Dana:** I'll send the notice.",
    ):
        assert part in text


def test_subtitles_and_text(meeting):
    store, meeting_id = meeting
    srt = export.subtitles(store, meeting_id, "srt")
    assert srt.startswith("1\n00:00:01,000 --> 00:00:04,000\nDana: The cutover is Friday.\n\n2\n")
    assert "00:01:01,250 --> 00:01:04,500" in srt
    vtt = export.subtitles(store, meeting_id, "vtt")
    assert vtt.startswith("WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nDana:")
    assert export.text(store, meeting_id).splitlines()[2] == "[00:01] Dana: The cutover is Friday."
    with pytest.raises(ValueError):
        export.render(store, meeting_id, "pdf")


def test_file_names_are_safe_and_auto_export_follows_the_setting(meeting, tmp_path: Path):
    store, meeting_id = meeting
    assert export.file_name(store, meeting_id, "md") == "2026-11-20 0905 Cutover Phase 2.md"
    assert export.auto_export(store, meeting_id) is None  # no folder set

    vault = tmp_path / "vault"
    vault.mkdir()
    store.set_setting("export_folder", str(vault))
    written = export.auto_export(store, meeting_id)
    assert written == vault / "2026-11-20 0905 Cutover Phase 2 (M1).md"
    assert "Send the notice" in written.read_text(encoding="utf-8")
    store.set_setting("export_folder", str(tmp_path / "gone"))
    assert export.auto_export(store, meeting_id) is None


def test_the_export_folder_keeps_one_file_per_meeting(meeting, tmp_path: Path):
    store, meeting_id = meeting
    vault = tmp_path / "vault"
    vault.mkdir()
    store.set_setting("export_folder", str(vault))
    export.auto_export(store, meeting_id)
    store.update_meeting(meeting_id, title="Firewall cutover")
    export.auto_export(store, meeting_id)  # renamed: replaces its old file
    # Same title, same minute: stopping and starting again within a minute does this.
    twin = store.create_meeting('Cutover: "Phase 2"?', "2026-11-20T09:05:00", tmp_path / "b")
    export.auto_export(store, twin)

    assert sorted(p.name for p in vault.iterdir()) == [
        f"2026-11-20 0905 Cutover Phase 2 (M{twin}).md",
        f"2026-11-20 0905 Firewall cutover (M{meeting_id}).md",
    ]
    first = (vault / f"2026-11-20 0905 Firewall cutover (M{meeting_id}).md").read_text("utf-8")
    assert "Send the notice" in first
