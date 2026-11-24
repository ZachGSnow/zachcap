"""Related meetings: what another meeting shares with one, ranked, and the reasons given."""

import random
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import mentions, related, topics
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}
TOPICS = "Firewall: firewall, fortigate\nBackups: backup, restore\nWindows 10: windows 10, win10"
# A call about a cutover: what it's about are words, not its people or topics.
CUTOVER = [
    "The wildcard certificate expires before the cutover.",
    "Push the profile after the cutover, and the wildcard.",
    "The intermediate chain was imported.",
]
SAME_CUTOVER = ["The wildcard certificate and the cutover.", "Then push the profile."]


def meeting(
    store: Store,
    folder: Path,
    title: str,
    day: str = "2026-11-20",
    said=("Hello.",),
    people=(),
    status: str = "ready",
    ticket: str | None = None,
    client: str | None = None,
    client_source: str = "user",
) -> int:
    """A meeting with these lines, said in turn by these people: a name, or (name, status) for
    one recognized by voice ("auto") or only suggested; "You" is you; with nobody named, one
    unknown voice says them. Its client, if given, is set on it ("" for no client)."""
    meeting_id = store.create_meeting(title, f"{day}T09:00:00", folder / title, duration=600)
    voices = []
    for who in people:
        name, how = who if isinstance(who, tuple) else (who, "confirmed")
        person = store.me()["id"] if name == "You" else store.person_id(name)
        match = Match(person, 1.0, how)
        voices.append(store.add_speaker(meeting_id, "system", name, None, 30.0, None, match))
    if not voices:
        match = Match(None, None, "unknown")
        voices.append(store.add_speaker(meeting_id, "system", "Voice", None, 30.0, None, match))
    lines = [*said, *["Hello."] * (len(voices) - len(said))]  # each of them says something
    store.add_segments(
        meeting_id,
        [
            (voices[at % len(voices)], "system", at * 5.0, at * 5.0 + 4, text, 0.9)
            for at, text in enumerate(lines)
        ],
    )
    fields: dict = {"status": status}
    if ticket is not None:
        fields |= {"ticket": ticket, "ticket_source": "user"}
    if client is not None:
        fields |= {"client": client or None, "client_source": client_source}
    store.update_meeting(meeting_id, **fields)
    return meeting_id


def fill(store: Store, folder: Path, meetings: int = 20) -> None:
    """Other meetings with nothing in common with any of a test's: the rest of a library, for a
    word's weight (few of many lines) and for what a third of the other meetings is."""
    for number in range(meetings):
        meeting(
            store,
            folder,
            f"Filler {number}",
            f"2026-01-{1 + number % 28:02d}",
            said=[f"Alpha{number} beta{number} gamma{number} delta{number}."] * 3,
        )


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    store.set_setting("export_folder", "")
    store.set_setting(topics.SETTING, TOPICS)
    yield store
    store.close()


def titles(store: Store, meeting_id: int, limit: int = 99) -> list[str]:
    return [found["title"] for found in related.find(store, meeting_id, limit)]


def reasons(store: Store, meeting_id: int) -> dict[str, list[str]]:
    return {found["title"]: found["reasons"] for found in related.find(store, meeting_id, 99)}


# ranking -------------------------------------------------------------------------------


def test_one_thing_in_common_ranks_by_what_it_is(store: Store, tmp_path: Path):
    """The same ticket, then client, person, topic, and words: one meeting for each, however
    long ago, and one with nothing in common."""
    this = meeting(
        store,
        tmp_path,
        "This",
        said=["The firewall rules are ready.", *CUTOVER],
        people=["Dana Lee"],
        ticket="1234567",
        client="Contoso",
    )
    words = meeting(store, tmp_path, "Words", "2026-11-19", said=SAME_CUTOVER)
    topic = meeting(store, tmp_path, "Topic", "2026-11-18", said=["The FortiGate again."])
    person = meeting(store, tmp_path, "Person", "2026-11-17", people=["Dana Lee"])
    client = meeting(store, tmp_path, "Client", "2026-11-16", client="contoso")
    ticket = meeting(store, tmp_path, "Ticket", "2026-11-15", ticket="1234567")
    meeting(store, tmp_path, "Nothing", "2026-11-21", said=["Toner for the copier."])
    fill(store, tmp_path)

    found = related.find(store, this)

    assert [f["id"] for f in found] == [ticket, client, person, topic, words]
    assert [f["reasons"] for f in found[:4]] == [
        ["Ticket 1234567"],
        ["contoso"],
        ["Dana Lee"],
        ["Topic: Firewall"],
    ]
    assert len(found[4]["reasons"]) == 1 and found[4]["reasons"][0].startswith("Also about: ")
    scores = [f["score"] for f in found]
    assert scores[:4] == [32, 16, 4, 1] and 0 < scores[4] < 1


def test_each_kind_outweighs_everything_after_it(store: Store, tmp_path: Path):
    """However many people, topics or words another meeting shares, they don't make up for
    the ticket or the client, nor topics and words for one person."""
    people = ["Dana Lee", "Mark Ruiz", "Contact 3", "Contact 4", "Contact 5"]
    said = ["Firewall, backup, and windows 10.", *SAME_CUTOVER]
    this = meeting(
        store,
        tmp_path,
        "This",
        said=["Firewall, backup, and windows 10.", "FortiGate, restore, win10.", *CUTOVER],
        people=people,
        ticket="1234567",
        client="Contoso",
    )
    meeting(store, tmp_path, "All but the ticket", "2026-11-22", said, people, client="Contoso")
    meeting(store, tmp_path, "Only the ticket", "2026-11-09", ticket="1234567")
    meeting(store, tmp_path, "All but the client", "2026-11-21", said, people, ticket="")
    meeting(store, tmp_path, "Only the client", "2026-11-10", client="Contoso")
    meeting(store, tmp_path, "One person", "2026-11-08", people=["Dana Lee"])
    meeting(store, tmp_path, "Topics and words", "2026-11-24", said)
    fill(store, tmp_path)

    found = related.find(store, this, 99)

    assert [f["title"] for f in found] == [
        "Only the ticket",
        "All but the ticket",
        "Only the client",
        "All but the client",
        "One person",
        "Topics and words",
    ]
    # Five people count as three, and three topics as three.
    assert int(found[3]["score"]) == 4 * 3 + 3
    assert 3 < found[5]["score"] < 4


def test_equal_scores_are_the_newest_first(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", client="Contoso")
    for day in ("2026-10-05", "2026-11-25", "2026-11-12", "2025-12-30"):
        meeting(store, tmp_path, f"On {day}", day, client="Contoso")

    assert titles(store, this) == [
        "On 2026-11-25",  # a later call is related too
        "On 2026-11-12",
        "On 2026-10-05",
        "On 2025-12-30",
    ]


def test_at_most_five(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", client="Contoso")
    for day in range(10, 18):
        meeting(store, tmp_path, f"On {day}", f"2026-11-{day}", client="Contoso")

    newest = ["On 17", "On 16", "On 15", "On 14", "On 13"]
    assert [found["title"] for found in related.find(store, this)] == newest
    assert [found["title"] for found in related.find(store, this, 2)] == newest[:2]


# reasons -------------------------------------------------------------------------------


def test_the_reasons_are_in_plain_words(store: Store, tmp_path: Path):
    same = dict(
        said=[
            "The VPN profile is pushed before the cutover.",
            "The VPN profile needs the cutover, and the firewall needs a backup.",
            "Windows 10 gets the VPN profile after the cutover, with a backup.",
        ],
        people=["Mark Ruiz", "Dana Lee"],
        ticket="1234567",
        client="Contoso",
    )
    this = meeting(store, tmp_path, "This", **same)
    meeting(store, tmp_path, "The same", "2026-11-01", **same)
    fill(store, tmp_path)

    ticket, client, people, said, words = reasons(store, this)["The same"]

    assert (ticket, client) == ("Ticket 1234567", "Contoso")
    assert people == "Dana Lee, Mark Ruiz"
    assert said == "Topics: Backups, Firewall, Windows 10"  # the most said here first
    # The VPN profile is a phrase, the firewall and the backups are topics.
    assert words == "Also about: cutover, VPN profile, pushed"


def test_a_reason_names_the_first_three(store: Store, tmp_path: Path):
    names = [f"Contact {n}" for n in range(1, 6)]
    this = meeting(store, tmp_path, "This", people=names)
    meeting(store, tmp_path, "Others", "2026-11-01", people=names)

    assert reasons(store, this)["Others"] == ["Contact 1, Contact 2, Contact 3 and 2 more"]
    assert related._listed(("Contoso", "Northwind")) == "Contoso, Northwind"


def test_people_are_named_the_most_heard_here_first(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", people=["Dana Lee", "Mark Ruiz"])
    speaker = store.speakers(this)[1]["id"]
    store.db.execute("UPDATE meeting_speakers SET speech_seconds = 300 WHERE id = ?", (speaker,))
    store.db.commit()
    meeting(store, tmp_path, "Others", "2026-11-01", people=["Dana Lee", "Mark Ruiz"])

    assert reasons(store, this)["Others"] == ["Mark Ruiz, Dana Lee"]


# what doesn't count ---------------------------------------------------------------------


def test_you_and_people_only_suggested_dont_count(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", people=["You", "Dana Lee", ("Mark Ruiz", "auto")])
    meeting(store, tmp_path, "Only you", "2026-11-01", people=["You"])
    meeting(store, tmp_path, "Suggested", "2026-11-02", people=[("Dana Lee", "suggested")])
    voice = meeting(store, tmp_path, "By voice", "2026-11-03", people=[("Dana Lee", "auto")])
    meeting(store, tmp_path, "Named", "2026-11-04", people=["You", "Mark Ruiz"])
    meeting(store, tmp_path, "Nobody", "2026-11-05", people=[("Contact 9", "unknown")])

    assert reasons(store, this) == {"Named": ["Mark Ruiz"], "By voice": ["Dana Lee"]}
    assert titles(store, voice) == ["This"]

    # Nor when it's this meeting's own people that are only suggested.
    suggested = meeting(
        store, tmp_path, "Only suggested", "2026-11-06", people=[("Dana Lee", "suggested")]
    )
    assert titles(store, suggested) == []


def test_someone_whose_lines_were_all_moved_to_others_isnt_there(store: Store, tmp_path: Path):
    """A voice that was named, then had every line given to someone else, says nothing: the
    meeting isn't with that person (the meeting list, and its client, don't count it either)."""
    this = meeting(store, tmp_path, "This", people=["Dana Lee", "Mark Ruiz"])
    moved = meeting(store, tmp_path, "Moved", "2026-11-01", people=["Dana Lee"])
    meeting(store, tmp_path, "Kept", "2026-11-02", people=["Dana Lee"])
    assert reasons(store, this) == {"Moved": ["Dana Lee"], "Kept": ["Dana Lee"]}

    line = store.transcript(moved)[0]["id"]
    store.reassign_segment(line, name="Mark Ruiz")

    assert reasons(store, this) == {"Moved": ["Mark Ruiz"], "Kept": ["Dana Lee"]}
    assert reasons(store, moved) == {"This": ["Mark Ruiz"]}


def test_unfinished_meetings_and_the_meeting_itself_are_left_out(store: Store, tmp_path: Path):
    same = dict(ticket="1234567", client="Contoso", people=["Dana Lee"])
    this = meeting(store, tmp_path, "This", **same)
    for day, status in (("19", "recording"), ("18", "recorded"), ("17", "processing")):
        meeting(store, tmp_path, status, f"2026-11-{day}", status=status, **same)
    meeting(store, tmp_path, "needs_names", "2026-11-16", status="needs_names", **same)
    meeting(store, tmp_path, "failed", "2026-11-15", status="failed", **same)

    assert titles(store, this) == ["needs_names", "failed"]

    # A meeting still being recorded is about something too, but isn't listed itself.
    recording = store.meetings_with_status("recording")[0]["id"]
    assert titles(store, recording) == ["This", "needs_names", "failed"]


def test_a_deleted_meeting_is_gone_from_the_next_answer(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", client="Contoso")
    gone = meeting(store, tmp_path, "Gone", "2026-11-01", client="Contoso")
    assert titles(store, this) == ["Gone"]

    store.delete_meeting(gone)

    assert titles(store, this) == [] and related.find(store, gone) == []


def test_the_client_is_the_one_the_clients_feature_gives(store: Store, tmp_path: Path):
    for name, org in (("Dana Lee", "Contoso"), ("Mark Ruiz", "Contoso"), ("Contact 3", "Fabrikam")):
        store.set_client(store.person_id(name), org)
    inferred = meeting(store, tmp_path, "Inferred", people=["Dana Lee"])
    meeting(store, tmp_path, "Also inferred", "2026-11-05", people=["Mark Ruiz"])
    meeting(store, tmp_path, "Set on it", "2026-11-04", client="CONTOSO")
    meeting(store, tmp_path, "Kept", "2026-11-03", client="Contoso", client_source="auto")
    meeting(store, tmp_path, "No client", "2026-11-02", client="", people=["Mark Ruiz"])
    meeting(store, tmp_path, "Two clients", "2026-11-01", people=["Mark Ruiz", "Contact 3"])
    meeting(store, tmp_path, "Another", "2026-10-31", client="Northwind")

    assert reasons(store, inferred) == {
        "Also inferred": ["Contoso"],
        "Set on it": ["CONTOSO"],
        "Kept": ["Contoso"],
    }


# words ---------------------------------------------------------------------------------


def test_names_numbers_everyday_words_and_topics_dont_make_meetings_alike(
    store: Store, tmp_path: Path
):
    store.set_client(store.person_id("Dana Lee"), "Northwind")
    store.set_setting(mentions.ALSO_ANSWER_TO, "Patty")
    said = [
        "Dana Lee called from Northwind at 7:30 pm on 2026-11-20 about ticket 1924086.",
        "Patty, you said you would be there by 10am, but we are still waiting for the update.",
        "So the firewall and the backup, and Windows 10, are what we talked about today.",
    ]
    this = meeting(store, tmp_path, "This", said=said)
    meeting(store, tmp_path, "The same words", "2026-11-01", said=said)
    fill(store, tmp_path)

    # Only the topics (said once each, in their own order), not as words.
    assert reasons(store, this) == {"The same words": ["Topics: Firewall, Backups, Windows 10"]}
    skip = related._counted_already(store, topics.saved(store))
    assert {"dana", "lee", "patty", "you", "northwind", "firewall", "windows"} <= skip


def test_one_word_in_common_is_a_coincidence(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", said=CUTOVER)
    meeting(store, tmp_path, "One word", "2026-11-01", said=["The wildcard came up."])
    meeting(store, tmp_path, "Two words", "2026-11-02", said=["The wildcard, then the cutover."])
    meeting(store, tmp_path, "Five words", "2026-11-03", said=SAME_CUTOVER)
    fill(store, tmp_path)

    assert titles(store, this) == ["Five words"]  # two aren't enough when nothing else is shared

    store.set_speaker_person(
        store.speakers(this)[0]["id"], store.person_id("Dana Lee"), "confirmed"
    )
    shared = meeting(
        store,
        tmp_path,
        "Two words, and Dana",
        "2026-11-04",
        said=["The wildcard, then the cutover."],
        people=["Dana Lee"],
    )
    assert reasons(store, this)["Two words, and Dana"] == [
        "Dana Lee",
        "Also about: cutover, wildcard",
    ]
    assert titles(store, shared)[0] == "This"

    meeting(
        store,
        tmp_path,
        "One word, and Dana",
        "2026-11-05",
        said=["The wildcard came up."],
        people=["Dana Lee"],
    )
    assert reasons(store, this)["One word, and Dana"] == ["Dana Lee"]


def test_words_said_all_over_the_library_are_left_out(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", said=CUTOVER)
    fill(store, tmp_path, 10)
    for number in range(4):  # four of the others, at most a third of them (10 + 4)
        meeting(store, tmp_path, f"Same {number}", "2026-11-01", said=SAME_CUTOVER)
    assert sorted(titles(store, this)) == ["Same 0", "Same 1", "Same 2", "Same 3"]

    for number in range(4, 8):  # more than a third of the others say the words
        meeting(store, tmp_path, f"Same {number}", "2026-11-01", said=SAME_CUTOVER)
    assert titles(store, this) == []


def test_words_are_shown_as_written_and_as_phrases(store: Store, tmp_path: Path):
    this = meeting(
        store,
        tmp_path,
        "This",
        said=[
            "The VPN profile is ready. Cutover next.",
            "The VPN profile again, and a vpn tunnel.",
            "A cutover. A cutover later. Wildcard first, then the Veeam job.",
        ],
    )
    terms = {"vpn", "profile", "cutover", "tunnel", "wildcard", "veeam"}
    written, together = related._as_written(store, this, terms)

    # A capital that only starts a sentence says nothing about the word; one mid-sentence does.
    assert written == {
        "vpn": "VPN",
        "profile": "profile",
        "cutover": "cutover",
        "tunnel": "tunnel",
        "wildcard": "wildcard",
        "veeam": "Veeam",
    }
    assert together == {("vpn", "profile"): 2}  # "vpn tunnel" is once only
    assert related._named(["cutover", "profile", "vpn"], written, together) == (
        "cutover",
        "VPN profile",
    )
    # A pair is named only when the other meeting says both, and each word once; three at most.
    assert related._named(["cutover", "profile"], written, together) == ("cutover", "profile")
    assert related._named(["profile", "tunnel", "vpn", "cutover"], written, together) == (
        "VPN profile",
        "tunnel",
        "cutover",
    )
    assert len(related._named(["cutover", "tunnel", "profile", "vpn"], written, {})) == 3


def test_words_match_as_the_full_text_index_has_them(store: Store, tmp_path: Path):
    # The index folds case and accents, and splits at anything but a letter or a digit.
    assert related._split("VPN-profile's caf\u00e9, Windows-10") == [
        "vpn",
        "profile",
        "s",
        "cafe",
        "windows",
        "10",
    ]
    skip = frozenset({"dana"})
    usable = {
        word: related._usable(word, skip)
        for word in ("cutover", "win11", "vpn", "dana", "the", "ab", "10am", "2026", "3rd", "5gb")
    }
    assert [word for word, ok in usable.items() if ok] == ["cutover", "win11", "vpn"]

    this = meeting(store, tmp_path, "This", said=["The caf\u00e9, the cutover and the wildcard."])
    meeting(store, tmp_path, "Plain", "2026-11-01", said=["The cafe, the cutover, the wildcard."])
    fill(store, tmp_path)
    assert titles(store, this) == ["Plain"]


def test_a_meeting_with_no_lines_or_an_unreadable_index_has_no_words(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    this = meeting(store, tmp_path, "This", said=[], client="Contoso")
    meeting(store, tmp_path, "Others", "2026-11-01", client="Contoso")
    assert reasons(store, this) == {"Others": ["Contoso"]}

    def broken(*args):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(related, "_words", broken)
    again = meeting(store, tmp_path, "Again", "2026-11-02", said=CUTOVER, client="Contoso")
    assert titles(store, again) == ["This", "Others"]


# topics --------------------------------------------------------------------------------


def test_the_topics_in_common_are_the_ones_the_counts_have(store: Store, tmp_path: Path):
    lines = [
        ["Windows 10 and the firewall.", "More about Windows 10, and a backup."],
        ["windows-10", "The FortiGate's firewall."],  # not the phrase, but the firewall
        ["Windows, 10 laptops.", "Unfirewalled."],  # neither is the topic
        ["WIN10 now.", "Restore the backup, then the backup."],
        ["Nothing about any of it."],
    ]
    ids = [
        meeting(store, tmp_path, f"M{n}", f"2026-11-0{n + 1}", said) for n, said in enumerate(lines)
    ]
    counts = topics.counts(store)

    for meeting_id in ids:
        expected = {name: per for name, per in counts.items() if meeting_id in per}
        said = topics.in_common(store, meeting_id, 4000)
        assert list(said) == sorted(expected, key=lambda name: -expected[name][meeting_id])
        assert said == {name: frozenset(per) for name, per in expected.items()}

    # A topic on more lines than asked for is said all over the library, so it's left out: the
    # index finds "Windows 10" on five lines, the others on two each.
    assert set(topics.in_common(store, ids[0], 4)) == {"Firewall", "Backups"}
    assert topics.in_common(store, ids[0], 1) == {}
    assert topics.in_common(store, ids[4], 4000) == {}


def test_a_topic_said_between_a_meetings_own_lines_isnt_its_own(store: Store, tmp_path: Path):
    """The index finds a meeting's lines by their id range, which can hold other meetings' lines
    (a meeting recorded while another was)."""
    this = meeting(store, tmp_path, "This", said=["Nothing about it."])
    meeting(store, tmp_path, "Others", "2026-11-01", said=["The firewall, and the backup."])
    speaker = store.speakers(this)[0]["id"]
    store.add_segments(this, [(speaker, "system", 50.0, 54.0, "Still nothing.", 0.9)])

    assert topics.in_common(store, this, 4000) == {}
    assert titles(store, this) == []


# kept ----------------------------------------------------------------------------------


@pytest.fixture
def worked_out(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The meetings whose words were worked out (not kept from before), as it happens."""
    calls: list[int] = []
    real = related._words

    def words(store, meeting_id, skip, library):
        calls.append(meeting_id)
        return real(store, meeting_id, skip, library)

    monkeypatch.setattr(related, "_words", words)
    return calls


def test_what_meetings_share_is_kept_until_a_line_a_voice_or_a_person_changes(
    store: Store, tmp_path: Path, worked_out: list[int]
):
    this = meeting(store, tmp_path, "This", said=CUTOVER, people=["Dana Lee"])
    other = meeting(store, tmp_path, "Others", "2026-11-01", said=SAME_CUTOVER, people=["Dana Lee"])
    fill(store, tmp_path)
    first = related.find(store, this)
    assert first and worked_out == [this]

    assert related.find(store, this) == first and worked_out == [this]  # kept
    related.find(store, other)
    assert worked_out == [this, other]  # per meeting

    # The lines of another meeting change.
    for line in store.transcript(other):
        store.edit_segment(line["id"], "Budget for the lease.")
    assert reasons(store, this)["Others"] == ["Dana Lee"]  # no words in common
    assert worked_out[-1] == this and len(worked_out) == 3

    # A voice is named.
    later = meeting(store, tmp_path, "Named later", "2026-11-02", said=["Hello."])
    assert "Named later" not in reasons(store, this)
    speaker = store.speakers(later)[0]["id"]
    store.set_speaker_person(speaker, store.person_id("Dana Lee"), "auto")
    assert reasons(store, this)["Named later"] == ["Dana Lee"]

    # A person's client (theirs follows their people).
    store.set_client(store.person_id("Dana Lee"), "Contoso")
    assert "Contoso" in reasons(store, this)["Named later"]


def test_tickets_clients_and_statuses_show_at_once_without_working_anything_out_again(
    store: Store, tmp_path: Path, worked_out: list[int]
):
    this = meeting(store, tmp_path, "This", ticket="1234567", client="Contoso")
    other = meeting(store, tmp_path, "Others", "2026-11-01", said=["Hello."])
    related.find(store, this)
    assert worked_out == [this]

    store.update_meeting(other, ticket="1234567", ticket_source="user")
    assert reasons(store, this) == {"Others": ["Ticket 1234567"]}
    store.update_meeting(other, client="Contoso", client_source="user")
    assert reasons(store, this) == {"Others": ["Ticket 1234567", "Contoso"]}
    store.update_meeting(this, ticket=None, client=None)
    assert reasons(store, this) == {}
    store.update_meeting(this, ticket="1234567", client="Contoso", client_source="user")
    store.update_meeting(other, status="processing")
    assert reasons(store, this) == {}
    store.update_meeting(other, status="ready")
    assert reasons(store, this) == {"Others": ["Ticket 1234567", "Contoso"]}
    assert worked_out == [this]  # nothing worked out again: the lines didn't change


def test_new_topics_are_seen_at_once(store: Store, tmp_path: Path, worked_out: list[int]):
    this = meeting(store, tmp_path, "This", said=["The copier lease."], people=["Dana Lee"])
    others = dict(said=["The lease of the copier is up."], people=["Dana Lee"])
    meeting(store, tmp_path, "Others", "2026-11-01", **others)
    fill(store, tmp_path)
    assert reasons(store, this) == {"Others": ["Dana Lee", "Also about: copier, lease"]}

    store.set_setting(topics.SETTING, TOPICS + "\nLeases: lease, copier")

    # They're the topic now, not words of their own.
    assert reasons(store, this) == {"Others": ["Dana Lee", "Topic: Leases"]}
    store.set_setting(topics.SETTING, TOPICS)
    assert reasons(store, this) == {"Others": ["Dana Lee", "Also about: copier, lease"]}
    assert len(worked_out) == 2  # ones worked out before for the same topics are kept


def test_only_the_latest_library_and_a_few_meetings_are_kept(store: Store, tmp_path: Path):
    this = meeting(store, tmp_path, "This", said=CUTOVER)
    related.find(store, this)
    before = related._kept.copy()
    assert list(before) == [(store.path, store.lines_version())]

    meeting(store, tmp_path, "Others", "2026-11-01", said=SAME_CUTOVER)
    related.find(store, this)
    assert list(related._kept) == [(store.path, store.lines_version())] and before != related._kept

    ids = [
        meeting(store, tmp_path, f"More {number}", "2026-10-01")
        for number in range(related.KEPT_MEETINGS + 5)
    ]
    for meeting_id in ids:
        related.find(store, meeting_id)
    library = related._kept[(store.path, store.lines_version())]
    assert len(library.signals) == related.KEPT_MEETINGS


def test_a_new_client_name_is_a_name_at_once(store: Store, tmp_path: Path):
    """A client set on any meeting makes its name one of the words left out, though no line
    changed."""
    said = ["The wildcard and the fabrikam portal.", "The portal, the wildcard and fabrikam."]
    this = meeting(store, tmp_path, "This", said=said)
    meeting(store, tmp_path, "Others", "2026-11-01", said=said)
    someone = meeting(store, tmp_path, "Someone else", "2026-11-02")
    fill(store, tmp_path)
    assert list(reasons(store, this)) == ["Others"]  # three words in common
    before = store.lines_version()

    store.update_meeting(someone, client="Fabrikam", client_source="user")

    assert store.lines_version() == before and reasons(store, this) == {}


def test_regrouped_topics_are_worked_out_again(store: Store, tmp_path: Path):
    """The same words, as other topics: what's left out of the words is the same, the topics in
    common aren't."""
    store.set_setting(topics.SETTING, "Firewall: firewall, backup")
    this = meeting(store, tmp_path, "This", said=["The firewall.", "The backup."])
    meeting(store, tmp_path, "Others", "2026-11-01", said=["The backup."])
    assert reasons(store, this) == {"Others": ["Topic: Firewall"]}

    store.set_setting(topics.SETTING, "Firewall: firewall\nBackup: backup")

    assert reasons(store, this) == {"Others": ["Topic: Backup"]}


def test_the_meetings_saying_a_topic_are_looked_up_once_for_all_of_them(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    looked_up: list[list[str]] = []
    real = topics._meetings_saying

    def saying(store, words, most_lines):
        looked_up.append(words)
        return real(store, words, most_lines)

    monkeypatch.setattr(topics, "_meetings_saying", saying)
    first = meeting(store, tmp_path, "First", "2026-11-01", said=["The firewall."])
    second = meeting(store, tmp_path, "Second", "2026-11-02", said=["The firewall again."])

    assert titles(store, first) == ["Second"] and titles(store, second) == ["First"]
    assert looked_up == [["firewall", "fortigate"]]

    meeting(store, tmp_path, "Third", "2026-11-03", said=["Hello."])  # a line changed
    related.find(store, first)
    assert len(looked_up) == 2


def test_a_library_that_cant_tell_its_version_is_worked_out_each_time(
    store: Store, tmp_path: Path, worked_out: list[int], monkeypatch: pytest.MonkeyPatch
):
    this = meeting(store, tmp_path, "This", said=CUTOVER)
    meeting(store, tmp_path, "Others", "2026-11-01", said=SAME_CUTOVER)
    kept = dict(related._kept)
    monkeypatch.setattr(Store, "lines_version", lambda self: None)

    assert related.find(store, this) == related.find(store, this)
    assert worked_out == [this, this] and related._kept == kept


# the app -------------------------------------------------------------------------------


def test_related_meetings_through_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    app = create_app("test-token")
    client = TestClient(app, base_url=LOCAL)
    store = Store()
    this = meeting(store, tmp_path, "This", said=CUTOVER, ticket="1234567", client="Contoso")
    other = meeting(store, tmp_path, "Others", "2026-11-01", ticket="1234567")

    answer = client.get(f"/api/meetings/{this}/related", headers=AUTH)

    assert answer.status_code == 200
    assert answer.json() == [
        {
            "id": other,
            "title": "Others",
            "started_at": "2026-11-01T09:00:00",
            "score": 32,
            "reasons": ["Ticket 1234567"],
        }
    ]
    assert client.get(f"/api/meetings/{other + 99}/related", headers=AUTH).status_code == 404
    assert client.get(f"/api/meetings/{this}/related").status_code == 401  # needs the token
    # And the route itself checks it, not only the gate in front of the app.
    route = next(
        r for r in app.routes if getattr(r, "path", "") == "/api/meetings/{meeting_id}/related"
    )
    assert [d.dependency.__name__ for d in route.dependencies] == ["authorized"]
    store.delete_meeting(other)
    assert client.get(f"/api/meetings/{this}/related", headers=AUTH).json() == []
    store.close()


# speed ---------------------------------------------------------------------------------


def large_library(store: Store, folder: Path, meetings: int = 1500, lines: int = 12) -> list[int]:
    """Meetings of many clients and people, tickets, topics and words (a long tail, as in
    speech), all in one transaction."""
    rng = random.Random(5)
    words = [f"word{n}" for n in range(4000)]
    likely = [1 / (n + 10) for n in range(len(words))]
    spoken = "the and so we need you that is on it for this with okay yeah".split()
    work = "cutover tunnel profile wildcard toner mailbox repository extent gateway spooler".split()
    clients_named = ["Contoso", "Northwind", "Fabrikam"] + [f"Client {n}" for n in range(4, 31)]
    contacts = {
        client: [f"Contact {n} of {client}" for n in range(1, 4)] for client in clients_named
    }
    contacts["Contoso"][:2] = ["Dana Lee", "Mark Ruiz"]
    with store.transaction():
        people = {name: store.person_id(name) for names in contacts.values() for name in names}
        for client, names in contacts.items():
            for name in names:
                store.set_client(people[name], client)
        for number in range(meetings):
            client = rng.choice(clients_named)
            meeting_id = store.create_meeting(
                f"Call {number}",
                f"2026-{1 + number % 11:02d}-{1 + number % 28:02d}T{9 + number % 8:02d}:00:00",
                folder / f"c{number}",
                duration=600,
            )
            speakers = [
                store.add_speaker(
                    meeting_id, "system", name, None, 60.0, None, Match(people[name], 1.0, status)
                )
                for name, status in zip(
                    rng.sample(contacts[client], 2), ("confirmed", "auto"), strict=True
                )
            ]
            rows = []
            for line in range(lines):
                said = rng.choices(words, likely, k=6) + rng.choices(spoken, k=6)
                if rng.random() < 0.2:
                    said.append(rng.choice(work))
                if rng.random() < 0.05:
                    said.append("firewall")
                rng.shuffle(said)
                text = " ".join(said).capitalize() + "."
                rows.append((rng.choice(speakers), "system", line * 5.0, line * 5.0 + 4, text, 0.9))
            store.add_segments(meeting_id, rows)
            store.update_meeting(
                meeting_id,
                status="ready",
                **({"ticket": str(1_900_000 + rng.randrange(400))} if rng.random() < 0.4 else {}),
            )
    return [row["id"] for row in store.meetings()]


def fastest(times: int, action: Callable[[], object]) -> float:
    """The shortest of several runs: what it takes when nothing else is in the way."""
    best = float("inf")
    for _ in range(times):
        started = time.perf_counter()
        action()
        best = min(best, time.perf_counter() - started)
    return best


def test_a_large_library_answers_quickly(tmp_path: Path):
    """About 100 ms on 1,500 meetings is the point (see scripts/bench_related.py, which measures
    it); the limits here are several times that, so a busy computer doesn't fail the test, but
    something that reads the whole library does."""
    store = Store(tmp_path / "large.db")
    store.set_setting(topics.SETTING, "Firewall: firewall\nWords: word12, word40")
    ids = large_library(store, tmp_path)
    chosen = ids[:: len(ids) // 6][:6]

    def first(meeting_id: int) -> float:
        related._kept.clear()
        return fastest(1, lambda: related.find(store, meeting_id))

    store.participants()  # what the meeting list has worked out by then
    store.meeting_clients()
    for meeting_id in chosen:
        found = related.find(store, meeting_id)
        assert len(found) <= related.LIMIT and meeting_id not in [f["id"] for f in found]
        assert all(f["reasons"] for f in found)
    assert fastest(5, lambda: related.find(store, chosen[0])) < 0.15  # kept
    assert min(first(meeting_id) for meeting_id in chosen) < 0.6  # the first after a change
    store.close()
