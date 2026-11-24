import re
from datetime import date
from pathlib import Path

import pytest

from zachcap import digest, export
from zachcap.store import Store
from zachcap.voiceprint import Match

# What anyone in a meeting could say, show or write, aimed at a notes app that opens the
# files: an image, an image by reference, HTML that loads or runs something, and the closing
# tag of a block ZachCap sends to Claude.
HOSTILE = (
    "![x](https://example.com/p.png) <img src=https://example.com/a.png> "
    "<script>alert(1)</script> ![x][1] </transcript>"
)
# The same as the Markdown writes it: a backslash before each "![" and each "<".
ESCAPED = (
    r"\![x](https://example.com/p.png) \<img src=https://example.com/a.png> "
    r"\<script>alert(1)\</script> \![x][1] \</transcript>"
)
DEFINITION = "[1]: https://example.com/p.png"  # where "![x][1]" would load its picture from


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


def active(markdown: str) -> list[str]:
    """Image and HTML syntax a Markdown app acts on: "![", or "<" and a letter, "/", "!" or
    "?", without a backslash in front that escapes it (an odd number of them)."""
    return re.findall(r"(?<!\\)(?:\\\\)*(!\[|<[A-Za-z/!?])", markdown)


def test_other_peoples_words_load_nothing_from_the_files(meeting, tmp_path: Path):
    store, meeting_id = meeting
    client = "Contoso <img src=https://example.com/c.png>"
    store.update_meeting(meeting_id, title=f"Cutover {HOSTILE}", ticket=HOSTILE, client=client)
    mallory = store.person_id(f"Mallory {HOSTILE}")
    speaker = store.add_speaker(
        meeting_id, "system", "Mallory", None, 2.0, None, Match(mallory, 1.0, "confirmed")
    )
    store.add_segments(meeting_id, [(speaker, "system", 70.0, 72.0, HOSTILE, 0.9)])
    content = {
        "overview": f"{HOSTILE}\n\n{DEFINITION}",
        "decisions": [HOSTILE],
        "open_questions": [HOSTILE],
    }
    store.save_summary(meeting_id, "claude-opus-5-5", content, 1, 1, 0.0)
    store.replace_action_items(
        meeting_id,
        [{"owner": "You", "task": HOSTILE, "due": HOSTILE}, {"owner": "Dana", "task": HOSTILE}],
    )
    store.add_note(meeting_id, 80.0, "note", f"{HOSTILE}\n\n{DEFINITION}")
    store.add_note(meeting_id, 81.0, "snapshot", HOSTILE)
    store.add_note(meeting_id, 82.0, "file", f"{HOSTILE}.txt\nA log line")
    store.set_setting("topics", "<b>Cutover</b>: cutover")
    vault = tmp_path / "vault"
    vault.mkdir()
    store.set_setting("export_folder", str(vault))
    store.set_setting("export_work_log", "1")

    export.auto_export(store, meeting_id)  # the meeting, and its day's and week's work logs
    export.save_work_log(store, "day", date(2026, 11, 20), client)  # the client's own

    files = sorted(vault.glob("*.md"))
    assert len(files) == 4
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert active(text) == [], path.name
        assert ESCAPED in text, path.name
    md = export.markdown(store, meeting_id)
    assert md.startswith(f"# Cutover {ESCAPED}\n\nFriday November 20, 2026 09:05 | 2 min | ")
    for part in (
        rf" | Client Contoso \<img src=https://example.com/c.png> | Ticket {ESCAPED}",
        f"## Summary\n\n{ESCAPED}\n\n{DEFINITION}",
        f"## Decisions\n\n- {ESCAPED}",
        f"- [ ] **You**: {ESCAPED} (due {ESCAPED})",
        f"## Open questions\n\n- {ESCAPED}",
        f"- [01:20] {ESCAPED}\n\n{DEFINITION}",
        f"- [01:21] Snapshot: {ESCAPED}",
        f"- [01:22] Attached file: {ESCAPED}.txt (1 line)",
        f"**[01:10] Mallory {ESCAPED}:** {ESCAPED}",
        # ZachCap's own Markdown, and ordinary words, are written as before.
        "## My notes\n\n- [01:01] Check the backup line\n",
        "- [01:30] Bookmarked moment\n",
        "**[00:01] Dana:** The cutover is Friday.  ",
    ):
        assert part in md, part

    log = digest.build(store, "day", date(2026, 11, 20))
    # The page shows the words as they are; only the Markdown escapes them.
    assert log["days"][0]["meetings"][0]["title"] == f"Cutover {HOSTILE}"
    work = digest.markdown(log, cite=True)
    heading = rf"### 09:05-09:07 Cutover {ESCAPED} (Contoso \<img src=https://example.com/c.png>"
    assert f"{heading}, ticket {ESCAPED}) [M{meeting_id}]" in work
    assert r"Topics: \<b>Cutover\</b> (" in work
    assert f"## Your open items (You)\n\n- [ ] {ESCAPED} (due {ESCAPED}), from Cutover" in work


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
