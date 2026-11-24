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

# Code anyone in a meeting could say, show or write, for the notes app's plugins to run when
# they open the files: Dataview's inline JavaScript and inline query, Templater commands (run
# when a new file appears), code blocks a plugin runs (dataviewjs on its own lines and indented
# in a list, a Mermaid diagram), and an indented code block, which Dataview reads too. The
# page's tests (src/lib/markdown.test.ts) use the same text.
PLUGINS = "\n".join(
    (
        '`$= dv.page()` `= this.file.name` <% tp.file.create_new() %> <%* tR += "x" %>',
        "```dataviewjs",
        "dv.paragraph(1)",
        "```",
        "- Steps",
        "  ```dataviewjs",
        "  dv.paragraph(2)",
        "  ```",
        "~~~mermaid",
        "graph TD",
        "~~~",
        "",
        "    $= dv.pages().length",
    )
)
# The same as the Markdown writes it: a backslash before the "=" of each query, inside each
# "<%" and before each mark of a fence.
PLUGINS_ESCAPED = "\n".join(
    (
        '`$\\= dv.page()` `\\= this.file.name` <\\% tp.file.create_new() %> <\\%* tR += "x" %>',
        "\\`\\`\\`dataviewjs",
        "dv.paragraph(1)",
        "\\`\\`\\`",
        "- Steps",
        "  \\`\\`\\`dataviewjs",
        "  dv.paragraph(2)",
        "  \\`\\`\\`",
        "\\~\\~\\~mermaid",
        "graph TD",
        "\\~\\~\\~",
        "",
        "    $\\= dv.pages().length",
    )
)
# More of the same, as written and as inert() writes it: a code's text after a space Dataview
# trims (a non-breaking one too); on the next line, in a quote too; a line after a backtick left
# open; an indented code block in a list item; fences after a footnote, a quote or a list
# marker; Templater's other tags; and "<%" next to HTML, each with one backslash.
BROKEN = (
    ("`` $= x `` `\xa0= y`", "`` $\\= x `` `\xa0\\= y`"),
    ("`\n= z` and > `\n> $= w`", "`\n\\= z` and > `\n> $\\= w`"),
    ("Press the ` key\n= this.file.name", "Press the ` key\n\\= this.file.name"),
    ("-     $= dv.pages()", "-     $\\= dv.pages()"),
    (
        "[^1]: ```dataviewjs\n> ````tasks\n1. ~~~button",
        "[^1]: \\`\\`\\`dataviewjs\n> \\`\\`\\`\\`tasks\n1. \\~\\~\\~button",
    ),
    ("<%_ x _%> <%- x -%> <%+ x %>", "<\\%_ x _%> <\\%- x -%> <\\%+ x %>"),
    ("<%<b> \\<% <<%", "<\\%\\<b> \\<\\% <<\\%"),
)
# Ordinary words, times and code no plugin runs, which inert() leaves as they are.
UNCHANGED = (
    "The cutover is Friday at 8 pm.",
    "Ping me! [01:05] x <5 and 3 < 4",
    "`x = 1` and a = b, x == y, 2 + 2 = 4",
    "use `>=` or `!=`, `$x` and `$$=`",
    "<5% of 100% %> 50%",
    "a ``code`` span, ~~struck~~, $5 = price",
    "- [ ] 09:05-09:07\n> 1. step",
    "C:\\Users\\Public",
)


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


def runs(markdown: str) -> list[str]:
    """Code a notes app's plugins would run from the file: a Templater tag ("<%" anywhere, as
    Templater reads the raw text), a code fence (three backticks or tildes), and Dataview's "="
    or "$=" where a code's text starts (after backticks, first on a line, or after a code
    block's indentation)."""
    return re.findall(
        r"<%|```|~~~|`[ \t]*\$?=|^[ \t>]*\$?=|(?:\t| {4})[ \t]*\$?=", markdown, re.MULTILINE
    )


def test_inert_breaks_what_plugins_run_and_nothing_else():
    assert export.inert(PLUGINS) == PLUGINS_ESCAPED
    assert runs(PLUGINS)
    assert runs(PLUGINS_ESCAPED) == []
    for text, escaped in BROKEN:
        assert export.inert(text) == escaped, text
        assert runs(escaped) == [], text
    for text in UNCHANGED:
        assert export.inert(text) == text


def test_other_peoples_words_run_no_plugin_code_from_the_files(meeting, tmp_path: Path):
    store, meeting_id = meeting
    client = "Contoso `$= dv.page()` <% tp.file.create_new() %>"
    store.update_meeting(meeting_id, title=f"Cutover {PLUGINS}", ticket=PLUGINS, client=client)
    mallory = store.person_id(f"Mallory {PLUGINS}")
    speaker = store.add_speaker(
        meeting_id, "system", "Mallory", None, 2.0, None, Match(mallory, 1.0, "confirmed")
    )
    store.add_segments(meeting_id, [(speaker, "system", 70.0, 72.0, PLUGINS, 0.9)])
    content = {"overview": PLUGINS, "decisions": [PLUGINS], "open_questions": [PLUGINS]}
    store.save_summary(meeting_id, "claude-opus-5-5", content, 1, 1, 0.0)
    store.replace_action_items(
        meeting_id,
        [{"owner": "You", "task": PLUGINS, "due": PLUGINS}, {"owner": PLUGINS, "task": PLUGINS}],
    )
    store.add_note(meeting_id, 80.0, "note", PLUGINS)
    store.add_note(meeting_id, 81.0, "snapshot", PLUGINS)
    store.add_note(meeting_id, 82.0, "file", f"{PLUGINS}\nA log line")
    store.set_setting("topics", "`$= dv.page()` <% x %>: cutover")
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
        assert runs(text) == [], path.name
        assert PLUGINS_ESCAPED in text, path.name
    md = export.markdown(store, meeting_id)
    first = PLUGINS_ESCAPED.split("\n")[0]  # a snapshot's lines are joined, a file's name is one
    client_escaped = "Contoso `$\\= dv.page()` <\\% tp.file.create_new() %>"
    assert md.startswith(f"# Cutover {PLUGINS_ESCAPED}\n\nFriday November 20, 2026 09:05 | 2 min")
    for part in (
        f" | Client {client_escaped} | Ticket {PLUGINS_ESCAPED}",
        f"## Summary\n\n{PLUGINS_ESCAPED}\n\n## Decisions\n\n- {PLUGINS_ESCAPED}\n\n## Action",
        f"- [ ] **You**: {PLUGINS_ESCAPED} (due {PLUGINS_ESCAPED})",
        f"- [ ] **{PLUGINS_ESCAPED}**: {PLUGINS_ESCAPED}",
        f"## Open questions\n\n- {PLUGINS_ESCAPED}",
        f"- [01:20] {PLUGINS_ESCAPED}\n",
        f"- [01:21] Snapshot: {first} / \\`\\`\\`dataviewjs / dv.paragraph(1) / \\`\\`\\` / - ",
        # Mid-line, where no code starts, "$=" stays as it is.
        " / \\~\\~\\~mermaid / graph TD / \\~\\~\\~ / $= dv.pages().length\n",
        f"- [01:22] Attached file: {first} (",
        f"**[01:10] Mallory {PLUGINS_ESCAPED}:** {PLUGINS_ESCAPED}",
        # ZachCap's own Markdown, ordinary words and times are written as before.
        "## My notes\n\n- [01:01] Check the backup line\n",
        "- [01:30] Bookmarked moment\n",
        "## Transcript\n\n**[00:01] Dana:** The cutover is Friday.  \n",
    ):
        assert part in md, part

    log = digest.build(store, "day", date(2026, 11, 20))
    # The page shows the words as they are; only the Markdown escapes them.
    assert log["days"][0]["meetings"][0]["title"] == f"Cutover {PLUGINS}"
    work = digest.markdown(log, cite=True)
    about = f"({client_escaped}, ticket {PLUGINS_ESCAPED})"
    assert f"### 09:05-09:07 Cutover {PLUGINS_ESCAPED} {about} [M{meeting_id}]" in work
    assert "Topics: `$\\= dv.page()` <\\% x %> (" in work
    mine = f"## Your open items (You)\n\n- [ ] {PLUGINS_ESCAPED} (due {PLUGINS_ESCAPED}), from"
    assert mine in work
    table = "| Time | Hours | Client | Ticket | Meeting | Note |\n|---|---|---|---|---|---|\n"
    assert f"{table}| 09:05-09:07 | 0.25 | {client_escaped} | " in work


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
