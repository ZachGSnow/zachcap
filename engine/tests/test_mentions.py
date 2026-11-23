"""Where your name was said: other people's lines that mention you."""

from pathlib import Path

from zachcap import mentions
from zachcap.store import Store
from zachcap.voiceprint import Match


def meeting_with(store: Store, folder: Path, lines: list[tuple[str, str]]) -> int:
    """A meeting where each (speaker, text) is one line; "me" is you."""
    meeting = store.create_meeting("Sync", "2026-11-22T20:00:00", folder, duration=120.0)
    speakers: dict[str, int] = {}
    rows = []
    for index, (who, text) in enumerate(lines):
        if who not in speakers:
            person = store.me()["id"] if who == "me" else store.person_id(who)
            speakers[who] = store.add_speaker(
                meeting, "mic" if who == "me" else "system", who, None, 5.0, None,
                Match(person, 0.9, "confirmed"),
            )  # fmt: skip
        rows.append((speakers[who], "system", index * 10.0, index * 10.0 + 5, text, 0.9))
    store.add_segments(meeting, rows)
    return meeting


def test_other_peoples_lines_that_say_your_name(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Alex Rivera")
    meeting = meeting_with(
        store,
        tmp_path,
        [
            ("Dana", "Alex, can you take the firewall change?"),
            ("me", "Sure, Alex here, I'll do it."),  # you, saying your own name
            ("Mark", "I think alex's team owns that."),
            ("Dana", "Nothing about anyone here."),
            ("Mark", "Thanks, Alex Rivera, for the notes."),
            ("Dana", "Alexander is a different person."),
        ],
    )

    found = mentions.find(store, meeting)

    assert [(m["speaker"], m["at"]) for m in found] == [
        ("Dana", 0.0),
        ("Mark", 20.0),
        ("Mark", 40.0),
    ]
    assert found[1]["names"] == ["alex"] and found[2]["names"] == ["Alex Rivera"]
    assert found[0]["snippet"] == "Alex, can you take the firewall change?"
    assert mentions.counts(store) == {meeting: 3}
    store.close()


def test_other_names_you_answer_to(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Alexandra Rivera")
    store.set_setting(mentions.ALSO_ANSWER_TO, "Z, AR,  Lexi , you, AR")
    assert mentions.aliases(store.setting(mentions.ALSO_ANSWER_TO)) == ["AR", "Lexi"]
    meeting = meeting_with(
        store,
        tmp_path,
        [("Dana", "Ask Lexi."), ("Mark", "AR said so"), ("Mark", "the ar team")],
    )

    # Initials in capitals match only in capitals ("ar" in lowercase is something else).
    assert [m["at"] for m in mentions.find(store, meeting)] == [0.0, 10.0]
    store.close()


def test_a_name_that_is_also_a_word_counts_only_as_a_name(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Will Turner")
    meeting = meeting_with(
        store,
        tmp_path,
        [("Dana", "I will mark it as done."), ("Dana", "Will, are you there?")],
    )

    assert [m["at"] for m in mentions.find(store, meeting)] == [10.0]
    assert mentions.counts(store) == {meeting: 1}
    store.close()


def test_the_default_name_you_is_never_a_mention(tmp_path: Path):
    store = Store(tmp_path / "test.db")  # your name is still "You"
    meeting = meeting_with(store, tmp_path, [("Dana", "Can you send it to you know who?")])

    assert mentions.your_names(store) == []
    assert mentions.find(store, meeting) == [] and mentions.counts(store) == {}
    store.close()


def test_a_long_line_is_cut_around_the_name(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Dana")
    text = "word " * 60 + "then Dana should look at it " + "more " * 60
    meeting = meeting_with(store, tmp_path, [("Mark", text)])

    (found,) = mentions.find(store, meeting)
    assert "Dana should look" in found["snippet"] and found["snippet"].startswith("...")
    assert len(found["snippet"]) <= mentions.SNIPPET + 6
    store.close()


def test_names_inside_contractions_and_sentence_starts_dont_count(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Don Grant")
    store.set_setting(mentions.ALSO_ANSWER_TO, "Will, Max, AM")
    meeting = meeting_with(
        store,
        tmp_path,
        [
            ("Dana", "I don't think that's right."),  # 0: inside a contraction
            ("Dana", "Will that work for everyone?"),  # 10: "Will" starting a question
            ("Dana", "May I share my screen? Mark it as resolved."),  # 20: other everyday words
            ("Dana", "Please grant me admin and set the max size."),  # 30: lowercase words
            ("Dana", "We start at 9 am, I am sure."),  # 40: "am" isn't the initials AM
            ("Dana", "Will, can you check the logs?"),  # 50: Will addressed: counts
            ("Dana", "Thanks Max, that helps."),  # 60: capitalized mid-sentence: counts
            ("Dana", "AM, are you there?"),  # 70: the initials: counts
            ("Dana", "That is Don's laptop."),  # 80: a possessive still counts
        ],
    )

    found = mentions.find(store, meeting)

    assert [m["at"] for m in found] == [50.0, 60.0, 70.0, 80.0]
    assert found[0]["spans"] == [[0, 4]] and found[3]["spans"] == [[8, 11]]
    assert mentions.counts(store) == {meeting: 4}
    store.close()


def test_a_full_name_of_everyday_words_counts_only_capitalized(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Will Grant")
    meeting = meeting_with(
        store,
        tmp_path,
        [
            ("Dana", "The vendor will grant us access on Monday."),  # 0: two everyday words
            ("Dana", "I asked Will Grant about the access."),  # 10: the name: counts
        ],
    )

    found = mentions.find(store, meeting)
    assert [(m["at"], m["spans"]) for m in found] == [(10.0, [[8, 18]])]
    assert mentions.counts(store) == {meeting: 1}
    store.close()


def test_the_meeting_list_counts_are_kept_until_something_changes(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.rename_person(store.me()["id"], "Dana Lee")
    meeting = meeting_with(store, tmp_path, [("Mark", "Dana, take a look.")])
    assert mentions.counts(store) == {meeting: 1}

    version = store.lines_version()
    assert mentions.counts(store) == {meeting: 1} and store.lines_version() == version

    line = store.transcript(meeting)[0]
    store.db.execute("UPDATE segments SET text = 'Nobody here.' WHERE id = ?", (line["id"],))
    store.db.commit()
    assert store.lines_version() > version
    assert mentions.counts(store) == {}  # worked out again after the change
    store.close()
