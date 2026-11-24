"""Summaries and documents written by a model on this PC. The model is a stand-in: it records what
it is asked and answers what the test says, so nothing is downloaded and no model runs."""

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zachcap import api as engine_api
from zachcap import clients, local_summary, ollama, pipeline, prompts, summarize
from zachcap.api import create_app
from zachcap.local_summary import Line
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)
AUTH = {"Authorization": "Bearer test-token"}
MODEL = ollama.Model("small:4b", 4, "4.2B", 8192, thinks=False)

ANSWER = {
    "topics": [{"title": "Change notice", "start": "01:00"}],
    "action_items": [
        {
            "owner": "Dana",
            "task": "send the change notice",
            "due": "Tuesday",
            "kind": "commitment",
            "said_at": "01:01",
        }
    ],
    "decisions": ["The cutover is on Friday evening."],
    "open_questions": ["Who covers the phones?"],
    "who_said_what": [{"speaker": "Dana", "points": ["Owns the notice."]}],
    "overview": "Dana sends the notice on Tuesday.",
    "title": "Cutover sync",
}


class Model:
    """Stands in for Ollama: each call as (system, user, shape, most), answered by ``answer``."""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.unloaded: list[str] = []
        self.answer = lambda call: ANSWER
        self.cut_off = lambda call: False
        self.read = 1000  # tokens of what it's asked

    def chat(self, on, system, user, shape, cancel, context, most) -> ollama.Reply:
        cancel.check()
        call = SimpleNamespace(
            on=on, system=system, user=user, shape=shape, context=context, most=most, cancel=cancel
        )
        self.calls.append(call)
        answer = self.answer(call)
        if isinstance(answer, Exception):
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return ollama.Reply(text.strip(), self.read, written=100, cut_off=self.cut_off(call))


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Model:
    model = Model()

    def known(name: str) -> ollama.Model:
        if name != MODEL.name:
            raise ollama.Failed(f"Ollama doesn't have {name} on this PC.")
        return MODEL

    monkeypatch.setattr(ollama, "model", known)
    monkeypatch.setattr(ollama, "chat", model.chat)
    monkeypatch.setattr(ollama, "unload", model.unloaded.append)
    # Nothing goes to Claude, whatever happens.
    monkeypatch.setattr(summarize.anthropic, "Anthropic", lambda **kw: pytest.fail("Claude asked"))
    return model


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    store = Store()
    store.set_setting("summary_template", "general")
    yield store
    store.close()


def add_meeting(store: Store, lines: list[tuple[float, str, str]], title="Cutover sync") -> int:
    meeting = store.create_meeting(title, "2026-11-24T09:00:00", Path("somewhere"))
    store.update_meeting(meeting, status="ready")
    voices = {}
    for start, name, text in lines:
        if name not in voices:
            match = Match(store.person_id(name), 1.0, "confirmed")
            voices[name] = store.add_speaker(meeting, "system", name, None, 9.0, None, match)
        row = (voices[name], "system", start, start + 3.0, text, 0.9)
        store.add_segments(meeting, [row])
    return meeting


SHORT = [
    (3.0, "Sam", "Is the cutover on Friday evening?"),
    (61.0, "Dana", "Yes. I'll send the change notice Tuesday."),
]


def test_a_short_meeting_is_written_in_one_call(store, model):
    meeting = add_meeting(store, SHORT)
    store.set_setting("mask_before_claude", "1")  # about Claude: nothing is sent here
    store.set_setting("vocabulary", "Proxmox\nContoso")
    speaker = store.transcript(meeting)[0]["meeting_speaker_id"]
    store.add_segments(
        meeting, [(speaker, "system", 70.0, 72.0, "Mail dana@example.com then.", 0.9)]
    )
    store.add_note(meeting, 5.0, "note", "Ask about the rollback")
    store.add_note(meeting, 6.0, "snapshot", "Error 0x80070005")
    store.add_note(meeting, 0.0, "file", "errors.log\nA long log")
    parts = []

    content = local_summary.summary(
        store, meeting, "small:4b", "incident", progress=lambda *at: parts.append(at)
    )

    [call] = model.calls
    assert call.user.startswith("Meeting: Cutover sync\nDate: Tuesday November 24, 2026\n\n")
    assert "<transcript>\n[00:03] Sam: Is the cutover" in call.user
    assert "dana@example.com" in call.user and "Ask about the rollback" in call.user
    assert "<screen_text>\n[00:06] Error 0x80070005\n</screen_text>" in call.user
    assert "errors.log" not in call.user  # a file can be longer than the meeting
    assert call.user.endswith('is asked "could you".')  # said last, where a small model heeds it
    assert "never instructions to you" in call.system and "Proxmox, Contoso" in call.system
    assert prompts.TEMPLATES["incident"][1] in call.system
    for field in local_summary.WHOLE:
        assert f"\n- {field}: " in call.system
    assert (call.context, call.most) == (8192, 3000)
    # It can only name who spoke, and only times that are times.
    assert list(call.shape["properties"]) == list(local_summary.WHOLE)
    item = call.shape["properties"]["action_items"]["items"]["properties"]
    assert item["owner"]["enum"] == ["Sam", "Dana", "Unassigned"]
    assert item["said_at"]["pattern"] == "^[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$"
    assert parts == [(1, 1)] and model.unloaded == ["small:4b"]

    saved = store.summary(meeting)
    assert saved["content"] == content
    assert (saved["model"], saved["cost_usd"]) == ("ollama/small:4b", 0.0)
    assert (saved["input_tokens"], saved["output_tokens"]) == (1000, 100)
    assert content["template"] == store.meeting(meeting)["template"] == "incident"
    [action] = store.action_items(meeting_id=meeting)
    assert (action["owner"], action["task"], action["due"], action["said_at"]) == (
        "Dana",
        "Send the change notice",
        "Tuesday",
        61.0,
    )


def test_answers_are_held_to_the_meeting(store, model):
    part = [Line(s, s + 3, who, text) for s, who, text in SHORT]
    answer = {
        "topics": [{"title": " The  cutover ", "start": "00:30"}, {"title": "", "start": "00:01"}],
        "action_items": [
            {"owner": "Somebody", "task": "order it", "due": "next month", "kind": "x"},
            {"owner": "Dana", "task": "Send it", "due": "by Tuesday", "said_at": "9:99:99"},
            {"owner": "Sam", "task": "Call", "due": "None", "kind": "action", "said_at": "01:20"},
            {"owner": "Sam", "task": "  ", "due": None, "kind": "action", "said_at": "01:20"},
            {"owner": "Sam", "task": "Meet", "due": "5 PM", "kind": "action", "said_at": "00:00"},
        ],
        "decisions": ["It's on  Friday.", "it's on friday.", ""],
        "open_questions": None,
        "who_said_what": [
            {"speaker": "Dana", "points": ["One.", "Two.", "Three."]},
            {"speaker": "Nobody", "points": ["Made up."]},
            {"speaker": "Dana", "points": ["Two.", "Four.", "Five."]},
            {"speaker": "Sam", "points": []},
        ],
        "overview": " It is\non Friday. ",
        "title": None,
    }

    kept = local_summary.held(answer, part, ["Sam", "Dana"], local_summary.WHOLE)

    assert kept["topics"] == [{"title": "The cutover", "start": "00:03"}]
    items = [
        (i["owner"], i["task"], i["due"], i["kind"], i["said_at"]) for i in kept["action_items"]
    ]
    assert items == [
        ("Unassigned", "Order it", None, "action", "00:03"),  # "month" was never said
        ("Dana", "Send it", "by Tuesday", "action", "01:01"),
        ("Sam", "Call", None, "action", "01:01"),
        ("Sam", "Meet", None, "action", "00:03"),  # nor a time
    ]
    assert kept["decisions"] == ["It's on Friday."] and kept["open_questions"] == []
    points = ["One.", "Two.", "Three.", "Four."]
    assert kept["who_said_what"] == [{"speaker": "Dana", "points": points}]
    assert (kept["overview"], kept["title"]) == ("It is on Friday.", "")
    # What isn't there is empty; what isn't even a list is an error.
    assert set(local_summary.held({}, part, ["Sam"], local_summary.WHOLE)) == set(kept)
    for odd in (
        {"topics": ["The cutover"]},
        {"action_items": 3},
        {"who_said_what": "Dana"},
        {"decisions": {"text": "Keep it."}},
        {"open_questions": [{"text": "Who?"}]},
    ):
        with pytest.raises(ollama.Failed, match="didn't answer as asked"):
            local_summary.held(odd, part, ["Sam"], local_summary.WHOLE)


MAIL = {"title": "Mail", "start": "00:03"}
DANA = {"speaker": "Dana", "points": ["One."]}


@pytest.mark.parametrize(
    ("field", "odd", "right"),
    [
        ("decisions", "Keep it.", ["Keep it."]),
        ("decisions", [{"a": 1}, "Keep it.", ["Keep that."], 5, None], ["Keep it."]),
        ("open_questions", "Who?", ["Who?"]),
        ("topics", MAIL, [MAIL]),
        ("topics", ["Phones", {**MAIL, "start": {}}], [MAIL]),
        ("who_said_what", [{**DANA, "points": "One."}, {**DANA, "points": 3}, 7], [DANA]),
        ("who_said_what", {**DANA, "speaker": ["Dana"]}, []),
        ("overview", ["One.", "Two."], "One. Two."),
        ("overview", {"text": "One."}, ""),
        ("title", 5, ""),
    ],
)
def test_an_odd_answer_is_taken_as_far_as_it_is_right(field, odd, right):
    part = [Line(s, s + 3, who, text) for s, who, text in SHORT]
    names = ["Sam", "Dana"]
    assert local_summary.held({field: odd}, part, names, local_summary.WHOLE)[field] == right


def test_an_odd_action_item_is_left_out():
    part = [Line(s, s + 3, who, text) for s, who, text in SHORT]
    odd = [7, {"owner": ["Dana"], "task": "send it", "due": ["Tuesday"], "said_at": 1}, {"task": 5}]
    kept = local_summary.held({"action_items": odd}, part, ["Sam", "Dana"], ["action_items"])
    item = {"owner": "Unassigned", "task": "Send it", "due": "Tuesday", "kind": "action"}
    assert kept == {"action_items": [{**item, "said_at": "00:03"}]}


@pytest.mark.parametrize(
    ("odd", "settled"),
    [
        (["D1", ["Q1"], {"n": 1}, 5, None, "D1"], ["D1"]),
        ("D1", ["D1"]),
        ([["D1"]], []),
        ({"D1": True}, []),
        (5, []),
        (None, []),
    ],
)
def test_odd_notes_of_what_is_settled_are_left_out(odd, settled):
    before = {"D1": "The mail moves tonight at nine.", "Q1": "Who is on call?"}
    said = local_summary.settled({"settled": odd}, before, ["The mail moves tonight at ten."])
    assert said == {"settled": settled, "changed": []}


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("Send the change notice", "Send change notice", True),
        ("Send the change notice", "Send the change notice to Priya", True),
        ("Replace switch 1", "Replace switch 2", False),
        ("Send the report to Dana", "Send the report to Mark", False),
        ("Call", "Call the vendor about the firewall firmware", False),
        ("Migrate the mail tonight", "Not migrate the mail tonight", False),
        ("We will migrate the mail tonight", "We won't migrate the mail tonight", False),
        ("Turn on the alerts of the mail queue", "Turn off the alerts of the mail queue", False),
        ("We cancel the move of the mail", "We do not cancel the move of the mail", False),
        ("We can't move the mail tonight", "We cannot move the mail tonight", True),
        ("The relay isn't moved tonight", "The relay is not moved tonight", True),
        ("", "", False),
    ],
)
def test_a_task_said_again_is_the_same_one(one, other, same):
    assert local_summary._same_task(one, other) is local_summary._same_task(other, one) is same


@pytest.mark.parametrize(
    ("before", "now"),
    [
        ("We will not migrate the mail tonight.", "We will migrate the mail tonight."),
        ("We will migrate the mail tonight.", "We won't migrate the mail tonight."),
        ("The cutover will happen on Friday.", "The cutover will no longer happen on Friday."),
        ("Reboot the mail server tonight.", "Don't reboot the mail server tonight."),
        ("The Friday cutover is on.", "The Friday cutover is cancelled."),
        ("We will cancel the move.", "We will not cancel the move."),
        ("We can move the mail tonight.", "We can't move the mail tonight."),
    ],
)
def test_a_decision_that_a_later_part_turns_round_is_replaced(store, model, before, now):
    meeting = add_meeting(store, LONG[:80])
    decided = iter(([before, "Keep the old relay."], [now], []))
    model.answer = lambda call: {**ANSWER, "decisions": next(decided), "settled": []}

    content = local_summary.summary(store, meeting, "small:4b")

    _, second, together = model.calls
    assert content["decisions"] == ["Keep the old relay.", now]
    assert f"D1 (decided): {before}" in second.user  # it was told, and named nothing
    assert f"Decided: {before}" not in together.user and f"Decided: {now}" in together.user


def test_a_decision_is_turned_round_by_a_later_part_only():
    both = ["If the backup has not run by nine, wait.", "If the backup has run by nine, wait."]
    assert local_summary._still([kept(decisions=both)], "D") == both
    many = [f"Decision {n} of these {n}." for n in range(40)]  # more than a part is told of
    answers = [kept(decisions=["We move it tonight.", *many])]
    answers.append(kept(decisions=["We never move it tonight."]))
    assert local_summary._still(answers, "D") == [*many, "We never move it tonight."]
    back = ["We move it tonight.", "We never move it tonight.", "We move it tonight."]
    assert local_summary._still([kept(decisions=[said]) for said in back], "D") == back[-1:]
    asked = [kept(open_questions=["Is it moved?"]), kept(open_questions=["Is it not moved?"])]
    assert len(local_summary._still(asked, "Q")) == 2
    # And only by the same words: a "not" about something else leaves it alone.
    for before, now in (
        ("Replace switch 1 tonight.", "Don't replace switch 2 tonight."),
        ("Dana takes the phones on Friday.", "Dana takes Friday off."),
        ("The lab gets new phones this week.", "The lab gets no new phones."),
    ):
        answers = [kept(decisions=[before]), kept(decisions=[now])]
        assert local_summary._still(answers, "D") == [before, now]


def test_what_is_said_again_is_as_it_was_said_last():
    said = "Move the mail server tonight at ten with Dana and Marcus on call."
    again = kept(decisions=[said])
    # Said again after it was changed, it holds; changed after it was said again, it doesn't.
    answers = [again, kept(decisions=["Order it."], changed=["D1"]), again]
    assert local_summary._still(answers, "D") == [said, "Order it."]
    answers = [again, again, kept(changed=["D2"])]
    assert local_summary._still(answers, "D") == [f"{said} (Changed later.)"]
    # Its words are the last ones, unless it was said in more detail before.
    short, longer = "Send Priya and Sam the new list.", "Send Priya and Sam the new list by noon."
    for first, last, stays in (
        ("Dana, not Priya, calls the vendor.", "Priya, not Dana, calls the vendor.", 1),
        (longer, short, 0),
        (short, longer, 1),
    ):
        answers = [kept(decisions=[first]), kept(decisions=[last])]
        assert local_summary._still(answers, "D") == [(first, last)[stays]]


def test_an_action_item_is_due_when_it_was_said_last():
    def items(*said) -> list[tuple]:
        answers = [
            kept(
                action_items=[
                    {"owner": owner, "task": task, "due": due, "kind": "commitment", "said_at": at}
                ]
            )
            for at, owner, task, due in said
        ]
        listed = local_summary._action_items(answers)
        return [(i["said_at"], i["owner"], i["task"], i["due"]) for i in listed]

    friday = ("05:00", "Dana", "Send the report", "Friday")
    assert items(friday, ("45:00", "Dana", "Send the report", "Monday")) == [
        ("45:00", "Dana", "Send the report", "Monday")  # and where that was said
    ]
    assert items(friday, ("45:00", "Dana", "Send the report", None)) == [friday]
    assert items(friday, ("45:00", "Dana", "Send the report", "Friday")) == [friday]
    detailed = ("05:00", "Dana", "Send the report to Priya and Sam", None)
    assert items(detailed, ("45:00", "Dana", "Send the report to Priya", "Monday")) == [
        ("45:00", "Dana", "Send the report to Priya and Sam", "Monday")
    ]
    # Taken back, only that is left; somebody else's is theirs.
    reboot = ("05:00", "Dana", "Reboot the mail server", "tonight")
    back = ("45:00", "Dana", "Not reboot the mail server", None)
    other = ("06:00", "Sam", "Reboot the mail server", None)
    assert items(reboot, other, back) == [other, back]
    assert items(reboot, back, ("50:00", *reboot[1:])) == [("50:00", *reboot[1:])]


def lines(count: int, size: int = 100, pause: float = 1.0) -> list[Line]:
    return [Line(i * (4 + pause), i * (4 + pause) + 4, "Dana", "x" * size) for i in range(count)]


def test_a_transcript_is_cut_at_pauses():
    assert local_summary.parts([]) == []
    assert local_summary.parts(lines(10), 5000) == [lines(10)]  # it fits
    talk = lines(100)
    # A long pause in the last third of what fits: that's where the part ends.
    talk[:] = [
        line._replace(start=line.start + 60, end=line.end + 60) if i >= 36 else line
        for i, line in enumerate(talk)
    ]
    cut = local_summary.parts(talk, 5000)
    assert [line for part in cut for line in part] == talk  # nothing lost, in order
    assert len(cut[0]) == 36
    for part in cut[:-1]:
        assert sum(len(line.said()) + 1 for line in part) <= 5000
    # A short rest stays with the part before it, and one long line is a part of its own.
    assert [len(p) for p in local_summary.parts(lines(48), 5000)] == [48]
    assert [len(p) for p in local_summary.parts(lines(3, size=6000), 5000)] == [1, 1, 1]


@pytest.mark.parametrize(
    ("due", "said"),
    [
        ("before the weekend", True),
        ("Before the Weekend.", True),
        ("by the weekend", True),  # its words, but for a small one
        ("Monday, next week", True),
        ("5 p.m.", True),
        ("5 PM", True),
        ("the  3rd", True),
        ("\u91d1\u66dc\u65e5\u307e\u3067", True),
        ("\u91d1\u66dc\u65e5", True),
        ("\u6708\u66dc\u65e5", False),
        ("2026-12-01", False),
        ("11/27", False),
        ("Q3", False),
        ("by the end of the quarter", False),
        ("before Christmas", False),
        ("next month", False),
        ("now", False),  # not in "know"
        ("Mon", False),
        ("on it", False),
        ("none", False),
        ("", False),
        (None, False),
    ],
)
def test_a_due_date_is_one_that_was_said(due, said):
    talk = (
        "You know the report: I'll look at it before",
        "the weekend, or by 5 p.m. on Monday the 3rd, next week.",
        "\u91d1\u66dc\u65e5\u307e\u3067\u306b\u5831\u544a\u66f8\u3092\u9001\u308a\u307e\u3059\u3002",
    )
    part = [Line(i * 5.0, i * 5.0 + 4, "Dana", text) for i, text in enumerate(talk)]
    written = " ".join((due or "").split()).rstrip(".")
    assert local_summary._due(due, part) == (written if said else None)


def add_talk(store: Store, meeting: int, text: str, start: float, end: float) -> None:
    """Adds a line of Dana's, who speaks in the meeting, with an end of its own."""
    speaker = next(r for r in store.transcript(meeting) if r["speaker"] == "Dana")
    row = (speaker["meeting_speaker_id"], "system", start, end, text, 0.9)
    store.add_segments(meeting, [row])


def test_a_line_without_a_pause_is_several_lines(store):
    meeting = add_meeting(store, [(600.0, "Sam", "Right."), (1502.0, "Dana", "So much for that.")])
    talk = " ".join(f"Sentence {i} is about the mail move, v1.{i} of it." for i in range(700))
    add_talk(store, meeting, talk, 0.0, 1500.0)

    said = local_summary.lines_of(store, meeting)

    *pieces, last = [line for line in said if line.speaker == "Dana"]
    assert last == Line(1502.0, 1505.0, "Dana", "So much for that.")
    assert len(talk) > 30_000 and " ".join(piece.text for piece in pieces) == talk  # all of it
    for piece in pieces:
        assert len(piece.text) <= local_summary.LINE_CHARS and piece.text.endswith(" of it.")
        at = 1500.0 * talk.index(piece.text) / len(talk)
        assert abs(piece.start - at) < 0.1 and piece.start < piece.end
    assert (pieces[0].start, pieces[-1].end) == (0.0, pytest.approx(1500.0))
    # Somebody else's word is where it was said, between two of them.
    assert [line.start for line in said] == sorted(line.start for line in said)
    right = next(i for i, line in enumerate(said) if line.text == "Right.")
    assert said[right - 1].start <= 600.0 <= said[right + 1].start
    # A line that fits is as it was.
    assert local_summary.lines_of(store, add_meeting(store, SHORT)) == [
        Line(start, start + 3.0, who, text) for start, who, text in SHORT
    ]


@pytest.mark.parametrize(
    ("text", "ends"),
    [
        (" ".join(f"word{i}" for i in range(900)), "0123456789"),  # no sentence ends: whole words
        ("Yes. " + " ".join(f"word{i}" for i in range(900)), "0123456789"),
        ("\u91d1\u66dc\u65e5\u307e\u3067\u306b\u9001\u308a\u307e\u3059\u3002" * 300, "\u3002"),
        ("\u3042" * 2500, "\u3042"),  # nowhere to cut
    ],
)
def test_a_long_line_is_cut_where_it_can_be(text, ends):
    pieces = Line(60.0, 660.0, "Dana", text).pieces()
    assert len(pieces) > 2 and all(len(p.text) <= local_summary.LINE_CHARS for p in pieces)
    assert "".join(p.text for p in pieces).replace(" ", "") == text.replace(" ", "")
    assert all(piece.text[-1] in ends and piece.text == piece.text.strip() for piece in pieces)
    assert all(len(piece.text) >= local_summary.LINE_CHARS // 2 for piece in pieces[:-1])


def test_a_summary_of_a_talk_without_a_pause_is_of_all_of_it(store, model):
    meeting = add_meeting(store, [(610.0, "Sam", "Right."), (1502.0, "Dana", "That's it.")])
    talk = " ".join(f"word{i}" for i in range(3600)) + " Dana resets the passwords on Friday."
    add_talk(store, meeting, talk, 0.0, 1500.0)
    noted_at = (0.0, 300.0, 605.0, 615.0, 900.0, 1499.0, 1600.0)
    for at in noted_at:
        store.add_note(meeting, at, "note", f"Note at {at}.")

    local_summary.summary(store, meeting, "small:4b")

    *noted, _ = model.calls
    sent = "\n".join(call.user for call in noted)
    assert len(noted) > 2 and all(sent.split().count(f"word{i}") == 1 for i in range(1, 3599))
    assert "Dana resets the passwords on Friday." in sent and "cut off" not in sent
    assert all(sent.count(f"Note at {at}.") == 1 for at in noted_at)
    assert sent.count("[10:10] Sam: Right.") == 1


LONG = [(i * 10.0, "Sam" if i % 2 else "Dana", f"Line {i} " + "word " * 40) for i in range(120)]


def test_a_long_meeting_is_noted_part_by_part_and_put_together(store, model):
    meeting = add_meeting(store, LONG)
    store.add_note(meeting, 15.0, "note", "Early note")
    store.add_note(meeting, 1190.0, "bookmark", "")
    cut = len(local_summary.parts(local_summary.lines_of(store, meeting))[0])
    store.add_note(meeting, LONG[cut][0] - 1.0, "note", "In the pause")
    parts = []

    def answer(call):
        if "Notes taken from the meeting" in call.user:
            return {"overview": "Everything.", "title": "The whole call"}
        number = len(model.calls)
        same = {"owner": "Dana", "task": "Send the notice", "due": None, "kind": "action"}
        return {
            "topics": [{"title": "Cutover" if number < 3 else "Phones", "start": "00:00"}],
            "action_items": [
                {**same, "said_at": "00:10", "due": "line" if number == 2 else None},
                {**same, "task": f"Do part {number} of the long list", "said_at": "00:10"},
            ],
            "decisions": [f"Decision {number}."],
            "open_questions": [],
            "who_said_what": [{"speaker": "Sam", "points": [f"Sam's {number}.", "More."]}],
        }

    model.answer = answer
    content = local_summary.summary(
        store, meeting, "small:4b", progress=lambda *at: parts.append(at)
    )

    *noted, together = model.calls
    of = len(noted)
    assert of >= 3 and parts == [(n, of) for n in range(1, of + 2)]
    assert noted[0].shape["required"] == list(local_summary.PART)
    assert all(c.shape["required"] == list(local_summary.LATER_PART) for c in noted[1:])
    assert together.shape["required"] == ["overview", "title"]
    said = "".join(c.user for c in noted)
    assert all(f"Line {i} word" in said for i in range(120))  # all of it, once
    assert said.count("Line 7 word") == 1
    assert "Part 1 of " in noted[0].user and "Early note" in noted[0].user
    assert "Early note" not in noted[1].user and "marked this moment" in noted[-1].user
    assert sum("In the pause" in c.user for c in noted) == 1  # between two parts: in one
    assert "<summary>\nPart 1\nTopics: Cutover\nDecided: Decision 1." in together.user
    assert "To do: Dana: Send the notice" in together.user and "Line 7" not in together.user
    # The same thing said in several parts is one; the overview is of all of it.
    tasks = [item["task"] for item in content["action_items"]]
    assert tasks.count("Send the notice") == 1 and len(tasks) == 1 + of
    assert content["action_items"][0]["due"] == "line"  # said in the second part
    assert [t["title"] for t in content["topics"]] == ["Cutover", "Phones"]
    assert (content["title"], content["overview"]) == ("The whole call", "Everything.")
    assert content["decisions"] == [f"Decision {n}." for n in range(1, of + 1)]
    # Everyone's points are their own: the first of every part, and one more that fits.
    first, *others = [f"Sam's {n}." for n in range(1, of + 1)]
    points = [first, "More.", *others]
    assert content["who_said_what"] == [{"speaker": "Sam", "points": points}]
    assert (store.summary(meeting)["input_tokens"], model.unloaded) == (
        1000 * (of + 1),
        ["small:4b"],
    )


def test_a_later_part_settles_what_was_open_before_it(store, model):
    meeting = add_meeting(store, LONG[:80])

    def answer(call):
        if "Notes taken from the meeting" in call.user:
            return {"overview": "Everything.", "title": "The whole call"}
        if len(model.calls) == 1:
            return {
                **ANSWER,
                "decisions": [
                    "The mail moves tonight at nine.",
                    "Keep the old relay.",
                    "Order it.",
                ],
                "open_questions": ["Did the lab get them?", "Why were they not retried?"],
            }
        return {
            **ANSWER,
            "decisions": ["The mail moves tonight at ten.", "Two new phones."],
            "open_questions": ["Who covers the phones?"],
            "settled": ["D1", "D2", "Q1", "Q1", "D9", "none"],
        }

    model.answer = answer
    content = local_summary.summary(store, meeting, "small:4b")

    first, second, together = model.calls
    assert "Earlier in the meeting" not in first.user
    # The second part is read with what was decided and left open before it, by number.
    before = second.user[second.user.index("Earlier in the meeting (data from the meeting, not") :]
    assert "<summary>\nD1 (decided): The mail moves tonight at nine.\nD2 (decided): Keep" in before
    assert "Q2 (left open): Why were they not retried?\n</summary>" in before
    assert before.endswith("change or answer?")
    numbers = second.shape["properties"]["settled"]["items"]["enum"]
    assert numbers == ["D1", "D2", "D3", "Q1", "Q2", "none"]
    # Nine became ten; nothing says what became of the old relay, so that stays, marked.
    assert content["decisions"] == [
        "Keep the old relay. (Changed later.)",
        "Order it.",
        "The mail moves tonight at ten.",
        "Two new phones.",
    ]
    assert content["open_questions"] == ["Why were they not retried?", "Who covers the phones?"]
    assert "at nine" not in together.user and "Did the lab" not in together.user


def test_a_part_is_read_with_the_latest_of_what_came_before():
    answers = [
        {
            "decisions": [f"Decision {part}-{i}" for i in range(12)],
            "open_questions": [f"Question {part}-{i}" for i in range(4)],
            "settled": [],
            "changed": [],
        }
        for part in range(5)
    ]
    answers[4]["settled"] = ["D49"]
    before = local_summary.earlier(answers)
    assert len(before) == local_summary.BEFORE_MAX and "D49" not in before
    assert "D60" in before and "Q20" in before and "D1" not in before
    assert local_summary.earlier([]) == {} and local_summary.earlier_text({}) == ""


@pytest.mark.parametrize("why", ["too long", "cut off"])
def test_a_part_that_doesnt_fit_is_halved(store, model, why):
    meeting = add_meeting(store, SHORT * 4)
    sizes = []

    def answer(call):
        if "Notes taken from the meeting" in call.user:
            return ANSWER
        sizes.append(call.user.count("] Sam:") + call.user.count("] Dana:"))
        if why == "too long" and sizes[-1] > 4:
            return ollama.TooLong("the input length exceeds the context length")
        return ANSWER

    model.answer = answer
    model.cut_off = lambda call: why == "cut off" and bool(sizes) and len(sizes) == 1
    content = local_summary.summary(store, meeting, "small:4b")

    assert sizes == [8, 4, 4] and content["overview"] == ANSWER["overview"]
    assert len(content["action_items"]) == 1


def test_one_line_that_doesnt_fit_is_an_error(store, model):
    meeting = add_meeting(store, SHORT[:1])
    model.answer = lambda call: ollama.TooLong("the input length exceeds the context length")
    with pytest.raises(ollama.Failed, match="exceeds the context"):
        local_summary.summary(store, meeting, "small:4b")
    assert store.summary(meeting) is None and model.unloaded == ["small:4b"]


@pytest.mark.parametrize(
    "answer", ["Sure! Here is the summary.", "[1, 2]", "{", "[" * 3000, {"topics": [1]}]
)
def test_an_answer_out_of_shape_is_an_error_and_keeps_the_last_summary(store, model, answer):
    meeting = add_meeting(store, SHORT)
    store.save_summary(meeting, "claude-opus-5-5", {"title": "Kept"}, 1, 1, 0.1)
    model.answer = lambda call: answer
    with pytest.raises(ollama.Failed, match="didn't answer as asked"):
        local_summary.summary(store, meeting, "small:4b")
    assert store.summary(meeting)["content"] == {"title": "Kept"}


def test_a_summary_without_a_title_has_the_meetings(store, model):
    meeting = add_meeting(store, SHORT)
    model.answer = lambda call: {**ANSWER, "title": " "}
    assert local_summary.summary(store, meeting, "small:4b")["title"] == "Cutover sync"


def test_the_kind_is_settled_by_clues_or_by_the_model(store, model):
    meeting = add_meeting(store, SHORT, title="Mail outage")
    local_summary.summary(store, meeting, "small:4b", "auto")
    assert len(model.calls) == 1  # the title says it
    assert store.meeting(meeting)["template"] == "incident"

    unclear = add_meeting(store, [*SHORT, (90.0, "Mark", "Hello.")])
    model.calls.clear()
    picks = iter(["project", "poem"])
    model.answer = lambda call: ANSWER if call.most > 60 else {"kind": next(picks)}
    local_summary.summary(store, unclear, "small:4b", "auto")
    pick, written = model.calls
    assert pick.shape["properties"]["kind"]["enum"] == list(prompts.TEMPLATES)
    assert "- customer (Customer call): " in pick.system
    assert prompts.TEMPLATES["project"][1] in written.system
    kept = store.meeting(unclear)
    assert (kept["template"], kept["template_source"]) == ("project", "auto")
    local_summary.summary(store, unclear, "small:4b", "auto")
    assert store.meeting(unclear)["template"] == "general"  # not one of the kinds


BROKEN = [
    "The call was about the move.",
    '{"overview": "The the the the',
    {"overview": " ", "title": "T"},
    {"overview": {"text": "x"}, "title": []},
    ollama.TooLong("Ollama: the input length exceeds the context length"),
    ollama.Failed("Ollama: the runner stopped"),
]
STOPS = [
    ollama.Cancelled("Stopped."),
    ollama.NotRunning("Ollama isn't answering on this PC."),
    ollama.NotHere("Ollama: model 'small:4b' not found"),
]


def about_parts(model: Model, *overviews) -> list[SimpleNamespace]:
    """Answers each part, and then the calls for the overview, which are returned."""
    asked: list[SimpleNamespace] = []

    def answer(call):
        if "topics" in call.shape["properties"]:
            number = len(model.calls)
            return {
                **ANSWER,
                "topics": [{"title": f"Topic {number}", "start": "00:00"}],
                "decisions": [f"Decision {number}."],
                "settled": [],
            }
        asked.append(call)
        return overviews[len(asked) - 1]

    model.answer = answer
    return asked


@pytest.mark.parametrize("broken", BROKEN)
def test_an_overview_that_fails_is_asked_for_again_with_less(store, model, broken):
    meeting = add_meeting(store, LONG)
    asked = about_parts(model, broken, {"overview": "Everything.", "title": "The call"})
    content = local_summary.summary(store, meeting, "small:4b")
    assert (content["overview"], content["title"]) == ("Everything.", "The call")
    first, second = [call.user.split("<summary>")[1].split("</summary>")[0] for call in asked]
    assert len(second) < len(first) * 0.6 and "Part 1" in second and "Part 4" in second
    assert first.startswith(second[:20]) and model.unloaded == ["small:4b"]


@pytest.mark.parametrize("broken", BROKEN)
def test_an_overview_that_isnt_written_is_made_of_the_notes(store, model, broken):
    meeting = add_meeting(store, LONG)
    asked = about_parts(model, broken, broken)
    content = local_summary.summary(store, meeting, "small:4b")
    assert len(asked) == 2 and len(model.calls) == 6
    # Said to be so, with nothing that isn't in the notes; the meeting keeps its title.
    assert content["title"] == "Cutover sync"
    assert content["overview"] == (
        "The model didn't write an overview. Topics: Topic 1; Topic 2; Topic 3; Topic 4. "
        "Decided: Decision 1. Decision 3. Decision 4."
    )
    assert content["decisions"] == [f"Decision {n}." for n in (1, 2, 3, 4)]
    assert store.summary(meeting)["content"] == content and model.unloaded == ["small:4b"]
    assert len(store.action_items(meeting_id=meeting)) == 1


def test_an_overview_cut_off_is_one_that_fails(store, model):
    meeting = add_meeting(store, LONG)
    asked = about_parts(
        model, {"overview": "Half", "title": "T"}, {"overview": "Half", "title": "T"}
    )
    model.cut_off = lambda call: "topics" not in call.shape["properties"]
    content = local_summary.summary(store, meeting, "small:4b")
    assert len(asked) == 2 and content["overview"].startswith("The model didn't write an overview.")


def test_an_overview_of_nothing_noted_says_so():
    assert local_summary.outline([kept(), kept()]) == {
        "overview": "The model didn't write an overview.",
        "title": "",
    }


@pytest.mark.parametrize("stop", STOPS)
def test_what_stops_a_summary_stops_its_overview(store, model, stop):
    meeting = add_meeting(store, LONG)
    for overviews in ((stop,), ("Not JSON.", stop)):
        asked = about_parts(model, *overviews)
        with pytest.raises(type(stop), match=str(stop)):
            local_summary.summary(store, meeting, "small:4b")
        assert len(asked) == len(overviews) and model.unloaded.pop() == "small:4b"
    # Stopped while it failed, it isn't asked again.
    asked = about_parts(model, "Not JSON.", ANSWER)
    answer = model.answer
    model.answer = lambda call: (
        answer(call)
        if "topics" in call.shape["properties"]
        else call.cancel.cancel() or answer(call)
    )
    with pytest.raises(ollama.Cancelled):
        local_summary.summary(store, meeting, "small:4b")
    assert len(asked) == 1 and store.summary(meeting) is None


@pytest.mark.parametrize(
    ("odd", "stays"),
    [
        ("Sure, it is a project.", "general"),
        ("[1]", "general"),
        ({"kind": ["project"]}, "general"),
        ({}, "general"),
        (ollama.TooLong("Ollama: the input length exceeds the context length"), "auto"),
        (ollama.Failed("Ollama: the runner stopped"), "auto"),
    ],
)
def test_a_kind_that_isnt_picked_is_general(store, model, odd, stays):
    unclear = add_meeting(store, [*SHORT, (90.0, "Mark", "Hello.")])
    model.answer = lambda call: odd if call.most == 60 else ANSWER
    content = local_summary.summary(store, unclear, "small:4b", "auto")
    pick, written = model.calls
    assert content["template"] == "general" and content["title"] == "Cutover sync"
    assert prompts.TEMPLATES["general"][1] in written.system
    # A pick that couldn't be made leaves the meeting Automatic: the next summary picks.
    assert store.meeting(unclear)["template"] == stays
    model.cut_off = lambda call: call.most == 60
    assert local_summary.summary(store, unclear, "small:4b", "auto")["template"] == "general"


@pytest.mark.parametrize("stop", STOPS)
def test_what_stops_a_summary_stops_the_pick(store, model, stop):
    unclear = add_meeting(store, [*SHORT, (90.0, "Mark", "Hello.")])
    model.answer = lambda call: stop if call.most == 60 else ANSWER
    with pytest.raises(type(stop), match=str(stop)):
        local_summary.summary(store, unclear, "small:4b", "auto")
    assert len(model.calls) == 1 and model.unloaded == ["small:4b"]


def test_the_first_part_is_under_way_while_the_kind_is_picked(store, model):
    for talk, of in ((SHORT, 1), (LONG, 4)):
        unclear = add_meeting(store, [*talk, (talk[-1][0] + 5.0, "Mark", "Hello.")])
        seen: list = []

        def answer(call, seen=seen):
            seen.append("pick" if call.most == 60 else "part")
            return {"kind": "project"} if call.most == 60 else {**ANSWER, "settled": []}

        model.answer = answer
        local_summary.summary(
            store, unclear, "small:4b", "auto", progress=lambda *at, seen=seen: seen.append(at)
        )
        assert seen[:3] == [(1, of), "pick", "part"]
        later = [(n, of) for n in range(1, of + 2)] if of > 1 else [(1, 1)]
        assert [at for at in seen if isinstance(at, tuple)] == later


def test_no_transcript_no_model_no_summary(store, model):
    empty = add_meeting(store, [])
    with pytest.raises(RuntimeError, match="no transcript"):
        local_summary.summary(store, empty, "small:4b")
    meeting = add_meeting(store, SHORT)
    with pytest.raises(ollama.Failed, match="doesn't have gone:7b"):
        local_summary.summary(store, meeting, "gone:7b")
    assert model.calls == []


def kept(**fields) -> dict:
    """What is kept of the answer for one part."""
    empty = {field: [] for field in (*local_summary.PART, "settled", "changed")}
    return {**empty, **fields}


@pytest.mark.parametrize(
    ("count", "topics", "points"),
    [
        (2, [0, 0, 0, 0, 1, 1, 1, 1], [0, 0, 0, 1, 1]),
        (6, [0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 5], [0, 1, 2, 4, 5]),
        (18, [0, 1, 3, 4, 5, 7, 8, 9, 10, 12, 13, 14, 16, 17], [0, 4, 8, 13, 17]),
    ],
)
def test_topics_and_points_are_from_every_part_of_a_long_meeting(count, topics, points):
    answers = [
        kept(
            topics=[{"title": f"Topic {n}.{i}", "start": f"{n}:0{i}"} for i in range(4)],
            who_said_what=[{"speaker": "Dana", "points": [f"{n}.{i}" for i in range(4)]}],
        )
        for n in range(count)
    ]
    starts = [tuple(map(int, t["start"].split(":"))) for t in local_summary._topics(answers)]
    assert [part for part, _ in starts] == topics and starts == sorted(starts)
    [entry] = local_summary._points(answers, ["Dana", "Sam"])
    said = [tuple(map(int, point.split("."))) for point in entry["points"]]
    assert [part for part, _ in said] == points and said == sorted(said)


def test_a_topic_that_goes_on_in_the_next_part_is_one_topic():
    topics = [["Mail", "Phones"], ["phones", "Mail"], [], ["Mail", "Wrap-up"]]
    answers = [kept(topics=[{"title": t, "start": "00:00"} for t in part]) for part in topics]
    titles = [topic["title"] for topic in local_summary._topics(answers)]
    assert titles == ["Mail", "Phones", "Mail", "Wrap-up"]


def test_the_notes_put_together_never_grow_past_what_fits():
    answers = [
        {
            "topics": [{"title": "T", "start": "00:00"}],
            "decisions": ["D."],
            "open_questions": [],
            "action_items": [{"owner": "Dana", "task": "Do it"}],
            "who_said_what": [{"speaker": "Dana", "points": ["Point " * 30] * 4}],
            "settled": [],
            "changed": [],
        }
    ] * 18  # three hours
    text = local_summary.notes_text(answers, 4000)
    assert len(text) <= 4000 and text.count("To do: Dana: Do it") == 18
    assert len(local_summary.notes_text(answers, 100)) <= 100


def test_the_notes_put_together_keep_the_end_of_a_long_meeting():
    def part(n: int) -> dict:
        task = f"Check server {n} and send the report to the client team"
        return kept(
            topics=[{"title": f"Topic {n}.{i} of the move", "start": "00:00"} for i in (1, 2)],
            decisions=[f"The mail relay of site {n}.{i} moves to the new server." for i in (1, 2)],
            open_questions=[f"Who covers site {n}?"],
            action_items=[{"owner": "Dana", "task": f"{task} ({i})"} for i in range(13)],
            who_said_what=[{"speaker": "Dana", "points": [f"Moves site {n}."] * 4}],
        )

    answers = [part(n) for n in range(1, 19)]  # three hours
    whole = local_summary.notes_text(answers, 10**6).splitlines()
    text = local_summary.notes_text(answers)
    assert local_summary.NOTES_CHARS * 0.8 < len(text) <= local_summary.NOTES_CHARS
    assert set(text.splitlines()) <= set(whole)  # no line is cut
    todo = []
    for n, said in enumerate(text.split("\n\n"), 1):
        assert said.startswith(f"Part {n}\nTopics: Topic {n}.1 of the move; Topic {n}.2 of")
        assert said.count("Decided: ") == 2 and f"Open: Who covers site {n}?" in said
        assert "Moves site" not in said  # what people said goes first
        todo.append(said.count("To do: "))
    assert len(todo) == 18 and 0 < min(todo) == max(todo) < 13  # then as much of every part
    assert local_summary.notes_text(answers, 40) == ""  # not even their numbers fit


def test_a_document_is_written_from_the_transcript_and_the_summary(store, model):
    meeting = add_meeting(store, SHORT)
    store.update_meeting(meeting, ticket="INC-4471")
    store.set_client(store.person_id("Dana"), "Contoso")
    local_summary.summary(store, meeting, "small:4b")
    model.calls.clear()
    model.answer = lambda call: "Subject: Cutover\n\nThanks, all."

    document = local_summary.document(store, meeting, "email", "small:4b")

    [call] = model.calls
    assert document["text"].startswith("Subject: Cutover") and document["cost_usd"] == 0
    assert document["model"] == "ollama/small:4b" and document["title"] == "Follow-up email"
    assert call.shape is None and call.system.startswith("You write documents from a meeting")
    for said in ("Client: Contoso", "Ticket: INC-4471", "<summary>", "[01:01] Dana: Yes. I'll"):
        assert said in call.user
    assert call.user.endswith("Write in You's voice.")

    local_summary.document(
        store, meeting, "email", "small:4b", previous="Hi {all},", change="shorter"
    )
    assert "Hi {all}," in model.calls[-1].user and "Rewrite it: shorter" in model.calls[-1].user
    with pytest.raises(RuntimeError, match="Say what to change"):
        local_summary.document(store, meeting, "email", "small:4b", previous="Hi", change=" ")
    model.cut_off = lambda call: True
    assert "Cut off" in local_summary.document(store, meeting, "email", "small:4b")["text"]


def test_a_document_of_a_long_meeting_needs_its_summary(store, model):
    meeting = add_meeting(store, LONG)
    with pytest.raises(RuntimeError, match="write the summary first"):
        local_summary.document(store, meeting, "ticket", "small:4b")
    store.save_summary(meeting, "claude-opus-5-5", {"title": "Long"}, 1, 1, 0.1)
    model.answer = lambda call: "Notes."
    assert local_summary.document(store, meeting, "ticket", "small:4b")["text"] == "Notes."
    assert "<transcript>" not in model.calls[-1].user and "Long" in model.calls[-1].user


def busy(parts: int) -> dict:
    """A saved summary of a long and busy meeting."""
    return {
        "title": "Mail relay move",
        "overview": "The call was about moving the mail relay. It ended with a plan for Friday.",
        "decisions": [
            f"Decided {n}: the relay of site {n} moves on Friday." for n in range(parts * 2)
        ],
        "action_items": [
            {
                "owner": ("Dana", "Priya")[n % 2],
                "task": f"Task {n}: send the list of the mailboxes of site {n} to the client",
                "due": "Friday" if n % 2 else None,
                "kind": "commitment",
                "said_at": f"{n // 60}:{n % 60:02d}:00",
            }
            for n in range(parts * 13)
        ],
        "who_said_what": [
            {"speaker": who, "points": [f"{who} said {n} about the list." for n in range(5)]}
            for who in ("Dana", "Priya", "Marcus")
        ],
        "open_questions": [f"Open {n}: who covers the phones of site {n}?" for n in range(parts)],
        "topics": [{"title": f"Topic {n} of the move", "start": f"{n}:00"} for n in range(14)],
        "template": "incident",
    }


def size(content: dict) -> int:
    return len(json.dumps(content, ensure_ascii=False))


@pytest.mark.parametrize("parts", [1, 6, 18])
@pytest.mark.parametrize("most", [1000, 4000, 9500, 16_000])
def test_a_summary_that_is_too_long_is_shortened(parts, most):
    whole = busy(parts)
    short = local_summary.shortened(whole, most)
    assert whole == busy(parts)  # it's kept as it was
    assert short == whole if size(whole) <= most else most - 200 < size(short) <= most
    assert all(short[f] == whole[f] for f in ("title", "overview", "template"))
    # One list is cut into: those needed less are gone, those needed more are whole.
    order = ("who_said_what", *local_summary.LEAST_NEEDED)
    left = [len(short[field]) / len(whole[field]) for field in order]
    assert left == sorted(left) and sum(0 < share < 1 for share in left) <= 1
    # What stays of it is from all of the meeting.
    for field in local_summary.LEAST_NEEDED:
        at = [whole[field].index(entry) for entry in short[field]]
        steps = {later - earlier for earlier, later in zip(at, at[1:], strict=False)}
        assert len(at) < 2 or (at[0], at[-1]) == (0, len(whole[field]) - 1)
        assert not steps or max(steps) - min(steps) <= 1


def test_what_people_said_is_shortened_evenly():
    whole = busy(1)
    short = local_summary.shortened(whole, size(whole) - 200)
    points = [[point.split()[2] for point in entry["points"]] for entry in short["who_said_what"]]
    assert points == [["0", "4"]] * 3  # some of everyone's
    assert all(short[field] == whole[field] for field in whole if field != "who_said_what")
    # Summaries of other versions may lack some of it.
    for content in ({"title": "Long"}, {"overview": "x" * 900}, {"decisions": None, "topics": []}):
        kept = local_summary.shortened(content, 100)
        assert {field: kept[field] for field in content} == content


ASKED = "What about the relay and the list for the client?"


@pytest.mark.parametrize("room", [30_000, 16_000, 9_000, 5_000])
def test_a_long_meetings_documents_and_answers_fit(store, model, room):
    talk = [
        (i * 10.0, "Dana", f"The relay and the list, line {i}, for the client.") for i in range(720)
    ]
    meeting = add_meeting(store, talk)
    store.save_summary(meeting, "claude-opus-5-5", busy(12), 1, 1, 0.1)

    def answer(call):
        if len(call.system) + len(call.user) > room:  # as Ollama says it, in characters here
            return ollama.TooLong("Ollama: the input length exceeds the context length")
        return "Text [00:00]."

    model.answer = answer
    document = local_summary.document(store, meeting, "ticket", "small:4b")
    asked = model.calls[-1]
    assert document["text"].startswith("Text") and "<transcript>" not in asked.user
    told = local_summary.answer(store, meeting, ASKED, "small:4b")
    assert told["answer"] == "Text [00:00]." and "Lines of the transcript" in model.calls[-1].user
    for call in (asked, model.calls[-1]):
        assert (
            "The call was about moving the mail relay." in call.user and "Decided 0:" in call.user
        )
        assert room / 4 < len(call.system) + len(call.user) <= room
    for call in (call for call in model.calls if room >= 9_000):  # the first that was asked
        assert "Decided 23:" in call.user and len(call.user) < 40_000
    assert model.unloaded == ["small:4b"] * 2


def test_a_document_or_an_answer_that_cant_fit_says_so(store, model):
    meeting = add_meeting(store, LONG)
    store.save_summary(meeting, "claude-opus-5-5", busy(6), 1, 1, 0.1)
    model.answer = lambda call: ollama.TooLong("Ollama: the input length exceeds the context")
    for write in (
        lambda: local_summary.document(store, meeting, "ticket", "small:4b"),
        lambda: local_summary.answer(store, meeting, ASKED, "small:4b"),
    ):
        model.calls.clear()
        with pytest.raises(ollama.Failed, match="small:4b can't read this much at once") as failed:
            write()
        assert "exceeds" not in str(failed.value)
        sizes = [len(call.user) for call in model.calls]
        assert 3 < len(sizes) < 7 and sizes == sorted(sizes, reverse=True)
        assert model.unloaded.pop() == "small:4b"
    # Without a summary, that is what to write first.
    short = add_meeting(store, [(i * 10.0, "Dana", "word " * 100) for i in range(20)])
    with pytest.raises(RuntimeError, match="write the summary first"):
        local_summary.document(store, short, "ticket", "small:4b")
    assert model.unloaded.pop() == "small:4b"


def test_a_transcript_too_dense_to_fit_leaves_the_summary_to_write_from(store, model):
    meeting = add_meeting(store, [(i * 10.0, "Dana", "word " * 100) for i in range(20)])
    store.save_summary(meeting, "claude-opus-5-5", busy(1), 1, 1, 0.1)
    model.answer = lambda call: (
        ollama.TooLong("too long") if "<transcript>" in call.user else "From the summary."
    )
    document = local_summary.document(store, meeting, "ticket", "small:4b")
    assert document["text"] == "From the summary." and "Decided 1:" in model.calls[-1].user


def test_text_cut_off_for_lack_of_room_is_asked_for_with_less(store, model):
    meeting = add_meeting(store, LONG)
    store.save_summary(meeting, "claude-opus-5-5", busy(6), 1, 1, 0.1)
    for write in (
        lambda: local_summary.document(store, meeting, "ticket", "small:4b")["text"],
        lambda: local_summary.answer(store, meeting, ASKED, "small:4b")["answer"],
    ):
        model.calls.clear()
        model.read = 7900  # of the 8192 it holds: it runs into the end of that
        model.cut_off = lambda call: len(model.calls) == 1

        def answer(call):
            if len(model.calls) == 2:
                model.read = 4000
            return "Half" if len(model.calls) == 1 else "All of it."

        model.answer = answer
        assert write() == "All of it." and len(model.calls) == 2
        assert len(model.calls[1].user) < len(model.calls[0].user)


def test_text_that_never_began_is_an_error(store, model):
    meeting = add_meeting(store, SHORT)
    model.answer = lambda call: ""
    model.cut_off = lambda call: True
    for write in (
        lambda: local_summary.document(store, meeting, "email", "small:4b"),
        lambda: local_summary.answer(store, meeting, "Who?", "small:4b"),
    ):
        with pytest.raises(ollama.Failed, match="small:4b wrote nothing before it ran out of room"):
            write()
        assert model.unloaded.pop() == "small:4b"
    model.cut_off = lambda call: False
    with pytest.raises(ollama.Failed, match="didn't write that"):
        local_summary.document(store, meeting, "email", "small:4b")


def test_a_long_meetings_documents_and_answers_have_your_notes(store, model):
    meeting = add_meeting(store, LONG)
    store.save_summary(meeting, "claude-opus-5-5", busy(1), 1, 1, 0.1)
    store.add_note(meeting, 30.0, "note", "The RMA number is 8841")
    store.add_note(meeting, 40.0, "snapshot", "Event ID 4625 Audit Failure")
    model.answer = lambda call: "Text."
    for write in (
        lambda: local_summary.document(store, meeting, "ticket", "small:4b"),
        lambda: local_summary.answer(store, meeting, ASKED, "small:4b"),
    ):
        write()
        said = model.calls[-1].user
        assert "[00:30] The RMA number is 8841" in said
        assert "<screen_text>\n[00:40] Event ID 4625 Audit Failure\n</screen_text>" in said
    # As many as fit, from all of the meeting, and none of them is most of it.
    for n in range(200):
        store.add_note(meeting, 100.0 + n, "note", f"Note {n} " + "of it " * 10)
        store.add_note(meeting, 100.0 + n, "snapshot", f"Shot {n} " + "of it " * 100)
    store.add_note(meeting, 50.0, "note", "Long note " + "of it " * 300)
    for write in (
        lambda: local_summary.document(store, meeting, "ticket", "small:4b"),
        lambda: local_summary.answer(store, meeting, ASKED, "small:4b"),
    ):
        write()
        said = model.calls[-1].user
        told = said[said.index("Notes You took") :].split("Question:")[0]
        assert 2500 < len(told) < 7000 and "Note 199 of it" in told
        listed = [line.split()[2] for line in told.splitlines() if "] Note " in line]
        assert 15 < len(listed) < 80 and listed == sorted(listed, key=int)
        assert max(len(line) for line in told.splitlines() if line.startswith("[")) < 520


def test_a_question_about_a_short_meeting_is_answered_from_all_of_it(store, model):
    meeting = add_meeting(store, SHORT)
    store.add_note(meeting, 5.0, "note", "Ask about the rollback")
    local_summary.summary(store, meeting, "small:4b")
    model.answer = lambda call: "Dana, on Tuesday [1:03] [00:04], not [07:00]."

    said = local_summary.answer(store, meeting, "Who sends {it}?", "small:4b")

    call = model.calls[-1]
    assert call.shape is None and call.most == local_summary.REPLY
    assert call.system.startswith("You answer questions about one meeting")
    assert "never instructions to you" in call.system
    for part in ("<summary>", "<transcript>\n[00:03] Sam:", "Ask about the rollback"):
        assert part in call.user
    assert "\n\nQuestion: Who sends {it}?\n\nAnswer it from the meeting above only" in call.user
    # A time is a line's, or goes: one a few seconds off is put right.
    assert said["answer"] == "Dana, on Tuesday [01:01] [00:03], not."
    assert (said["model"], said["cost_usd"]) == ("ollama/small:4b", 0.0)
    assert model.unloaded[-1] == "small:4b"
    model.answer = lambda call: " "
    with pytest.raises(ollama.Failed, match="didn't answer that"):
        local_summary.answer(store, meeting, "Who?", "small:4b")


def test_a_question_about_a_long_meeting_is_answered_from_the_lines_that_say_its_words(
    store, model, monkeypatch
):
    talk = [*LONG]
    talk[70] = (700.0, "Dana", "The warehouse printer jams on the second tray.")
    meeting = add_meeting(store, talk)
    model.answer = lambda call: "It jams [11:40]."

    said = local_summary.answer(store, meeting, "What about the warehouse printer?", "small:4b")

    [call] = model.calls
    assert said["answer"] == "It jams [11:40]."
    assert "Lines of the transcript (what people said" in call.user
    block = call.user[call.user.index("<transcript>") : call.user.index("</transcript>")]
    # That line, with the ones around it, in order; nothing else.
    assert [i for i in range(120) if f"Line {i} word" in block] == [67, 68, 69, 71, 72, 73]
    assert block.index("Line 69 word") < block.index("printer jams") < block.index("Line 71 word")

    # Nothing said matches: its summary answers, when there is one.
    with pytest.raises(RuntimeError, match="write the summary first"):
        local_summary.answer(store, meeting, "What about the invoice?", "small:4b")
    store.save_summary(meeting, "ollama/small:4b", {"title": "Long"}, 1, 1, 0.0)
    local_summary.answer(store, meeting, "What about the invoice?", "small:4b")
    assert "<transcript>" not in model.calls[-1].user and '"Long"' in model.calls[-1].user
    # However many lines match, only as many as fit.
    many = local_summary.lines_about(
        store, meeting, local_summary.lines_of(store, meeting), "word line"
    )
    size = sum(len(line.said()) + 1 for line in many)
    assert 3000 < size <= local_summary.PART_CHARS + len(many) and many == sorted(many)
    few = local_summary.lines_about(
        store, meeting, local_summary.lines_of(store, meeting), "word line", 2000
    )
    assert 0 < sum(len(line.said()) + 1 for line in few) <= 2000 + len(few)


def test_the_line_that_says_it_comes_before_the_lines_around_it(store):
    turns = [f"Turn {i} " + "word " * 170 for i in range(40)]
    turns[20] = "The warehouse printer jams. " + "word " * 170
    turns[30] = "The printer is fine. " + "word " * 170
    meeting = add_meeting(store, [(i * 100.0, "Dana", text) for i, text in enumerate(turns)])
    lines = local_summary.lines_of(store, meeting)

    def about(most: int) -> list[int]:
        question = "What about the warehouse printer?"
        found = local_summary.lines_about(store, meeting, lines, question, most)
        return [int(line.start) // 100 for line in found]

    assert about(1000) == [20]  # the one that says more of it
    assert about(2000) == [20, 30]
    assert about(4000) == [19, 20, 21, 30]  # then those around the best, the nearest first
    assert about(9500) == [17, 18, 19, 20, 21, 22, 23, 29, 30, 31]


def test_a_question_about_a_talk_without_a_pause_finds_where_it_was_said(store, model):
    meeting = add_meeting(store, [(1502.0, "Dana", "That's it."), (1510.0, "Sam", "Thanks.")])
    words = " ".join(f"word{i}" for i in range(3600)).split("word1800 ")
    said = "We reset every password on Friday."
    talk = f"{words[0]}{said} {words[1]} And the password list goes to Priya."
    add_talk(store, meeting, talk, 0.0, 1500.0)
    model.answer = lambda call: "On Friday."

    local_summary.answer(store, meeting, "When is the password reset?", "small:4b")

    [call] = model.calls
    assert said in call.user and "And the password list goes to Priya." in call.user
    assert len(call.user) < local_summary.PART_CHARS * 1.25
    lines = local_summary.lines_of(store, meeting)
    best = local_summary.lines_about(store, meeting, lines, "When is the password reset?", 1200)
    assert said in best[0].text  # the piece that says both words
    # What the search finds and these words don't (an accent) is still there.
    add_talk(store, meeting, "The caf\u00e9 is closed. " + "word " * 900, 2000.0, 2400.0)
    lines = local_summary.lines_of(store, meeting)
    best = local_summary.lines_about(store, meeting, lines, "What about the cafe?", 1200)
    assert best[-1].start == 2000.0 and len(best[-1].text) > 900


def test_a_question_about_someone_is_answered_from_what_they_said(store, model):
    talk = [*LONG]
    for i in (20, 80):
        talk[i] = (talk[i][0], "Will", f"The switch in closet {i} is mine.")
    meeting = add_meeting(store, talk)
    found = local_summary.lines_about(
        store, meeting, local_summary.lines_of(store, meeting), "What did Will say?"
    )
    assert [line.text for line in found if line.speaker == "Will"] == [
        "The switch in closet 20 is mine.",
        "The switch in closet 80 is mine.",
    ]
    # An everyday word that is nobody's name here isn't looked for.
    assert not local_summary.lines_about(
        store, meeting, local_summary.lines_of(store, meeting), "What can you do?"
    )


def test_a_question_whose_lines_dont_fit_is_asked_with_fewer(store, model):
    meeting = add_meeting(store, SHORT * 30)
    sizes: list[int] = []

    def answer(call):
        sizes.append(len(call.user))
        return ollama.TooLong("too long") if len(sizes) < 3 else "On Friday [00:03]."

    model.answer = answer
    said = local_summary.answer(store, meeting, "When is the cutover?", "small:4b")
    assert said["answer"] == "On Friday [00:03]."
    assert len(sizes) == 3 and sizes[0] >= sizes[1] > sizes[2]
    model.answer = lambda call: ollama.TooLong("too long")
    with pytest.raises(ollama.TooLong):
        local_summary.answer(store, meeting, "When is the cutover?", "small:4b")
    assert model.unloaded[-1] == "small:4b"


def test_an_answers_times_are_lines_times_and_the_rest_is_as_written():
    said = [Line(5.0, 9.0, "Dana", "x"), Line(65.4, 70.0, "Priya", "y")]
    for text, right in (
        ("They put .NET 8 on it [00:05].", "They put .NET 8 on it [00:05]."),
        ("Dana [00:07], Priya [01:05].", "Dana [00:05], Priya [01:05]."),
        ("Nobody [09:20] said .. that [09:21].", "Nobody said .. that."),
        ("Run:\n-  .\\setup.ps1 [09:20]\n- 1 , 2", "Run:\n-  .\\setup.ps1\n- 1 , 2"),
        ("[09:20] Nobody.", "Nobody."),
    ):
        assert local_summary.cited(text, said) == right


# Through the API ----------------------------------------------------------------------------


@pytest.fixture
def api(store, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(create_app("test-token"), base_url=LOCAL)


def put(api: TestClient, **change):
    return api.put("/api/settings", headers=AUTH, json=change)


def summarize_(api: TestClient, meeting: int, **body):
    return api.post(f"/api/meetings/{meeting}/summary", headers=AUTH, json=body)


def started(response) -> str:
    """The id of what the model on this PC was asked for."""
    assert response.status_code == 202, response.text
    return response.json()["writing"]


def over(api: TestClient, response) -> dict:
    """What came of it, once it's over."""
    run = f"/api/writing/{started(response)}"
    until(lambda: api.get(run, headers=AUTH).json()["state"] != "writing")
    return api.get(run, headers=AUTH).json()


def test_settings_choose_the_model_and_who_writes(api, store, model):
    settings = api.get("/api/settings", headers=AUTH).json()
    assert (settings["writer"], settings["local_model"]) == ("claude", "")
    assert put(api, writer="local").status_code == 400  # no model yet
    assert put(api, writer="gpt").status_code == 422
    refused = put(api, local_model="gone:7b")
    assert refused.status_code == 400 and "doesn't have gone:7b" in refused.json()["detail"]
    assert put(api, local_model=" small:4b ").json()["local_model"] == "small:4b"
    assert put(api, writer="local").json()["writer"] == "local"
    cleared = put(api, local_model="").json()
    assert (cleared["writer"], cleared["local_model"]) == ("claude", "")


def test_the_models_to_choose_from_or_why_there_are_none(api, monkeypatch):
    monkeypatch.setattr(ollama, "models", lambda: [MODEL])
    listed = api.get("/api/local-models", headers=AUTH).json()
    assert listed["running"] and listed["suggested"] == "qwen3.5:4b"
    assert listed["models"] == [{"name": "small:4b", "size": 4, "parameters": "4.2B"}]

    def none():
        raise ollama.NotRunning("Ollama isn't answering on this PC.")

    monkeypatch.setattr(ollama, "models", none)
    listed = api.get("/api/local-models", headers=AUTH).json()
    assert (listed["running"], listed["models"]) == (False, [])
    assert "isn't answering" in listed["reason"]
    assert api.get("/api/local-models").status_code == 401


def test_who_writes_is_as_asked_else_the_usual_one(api, store, model, monkeypatch):
    meeting = add_meeting(store, SHORT)
    claude = []
    monkeypatch.setattr(
        summarize, "summarize", lambda store, meeting, template: claude.append(template)
    )
    assert summarize_(api, meeting, by="local").status_code == 400  # no model chosen
    put(api, local_model="small:4b")
    summarize_(api, meeting, template="standup")
    assert claude == ["standup"] and model.calls == []
    written = over(api, summarize_(api, meeting, template="standup", by="local"))
    assert (written["state"], written["what"], written["by"]) == ("done", "summary", "small:4b")
    assert store.summary(meeting)["model"] == "ollama/small:4b" and len(model.calls) == 1
    put(api, writer="local")
    over(api, summarize_(api, meeting))
    assert len(model.calls) == 2
    summarize_(api, meeting, by="claude")
    assert len(claude) == 2
    assert summarize_(api, meeting, by="gpt").status_code == 422


def test_a_private_meeting_is_only_written_on_this_pc(api, store, model):
    meeting = add_meeting(store, SHORT)
    store.update_meeting(meeting, private=1)
    refused = summarize_(api, meeting)
    assert refused.status_code == 400 and "A model on this PC can write" in refused.json()["detail"]
    assert ask(api, meeting, "When?").status_code == 400  # nobody answers either
    put(api, local_model="small:4b")
    assert over(api, summarize_(api, meeting))["state"] == "done"  # though Claude is usual
    assert store.summary(meeting)["model"] == "ollama/small:4b"
    assert summarize_(api, meeting, by="claude").status_code == 400
    model.answer = lambda call: "Notes."
    recipe = {"recipe": "ticket"}
    document = over(api, api.post(f"/api/meetings/{meeting}/recipe", headers=AUTH, json=recipe))
    assert (document["what"], document["about"]) == ("document", "ticket")
    document = document["result"]
    assert (document["text"], document["model"]) == ("Notes.", "ollama/small:4b")
    refused = api.post(
        f"/api/meetings/{meeting}/recipe", headers=AUTH, json={**recipe, "by": "claude"}
    )
    assert refused.status_code == 400
    model.answer = lambda call: "On Tuesday [01:01]."
    answered = over(api, ask(api, meeting, "When?"))["result"]
    assert (answered["answer"], answered["model"]) == ("On Tuesday [01:01].", "ollama/small:4b")


class Held:
    """Holds the stand-in model in a call until let go of, as a real one takes minutes."""

    def __init__(self, model: Model) -> None:
        self.entered, self.go = threading.Event(), threading.Event()
        answer = model.answer

        def waiting(call):
            self.entered.set()
            while not self.go.wait(0.02):
                call.cancel.check()
            return answer(call)

        model.answer = waiting


def ask(api: TestClient, meeting: int, question: str):
    asked = {"question": question}
    return api.post(f"/api/meetings/{meeting}/ask", headers=AUTH, json=asked)


def writing(api: TestClient, meeting: int):
    return api.get(f"/api/meetings/{meeting}", headers=AUTH).json()["writing"]


def until(check, seconds: float = 10.0) -> None:
    end = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < end
        time.sleep(0.02)


def test_only_a_private_meetings_questions_are_answered_here(api, store, model, monkeypatch):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b", writer="local")
    claude = []
    monkeypatch.setattr(summarize, "ask", lambda *asked: claude.append(asked[1:]) or {})
    assert ask(api, meeting, " When? ").is_success
    assert claude == [(meeting, "When?")] and model.calls == []
    assert ask(api, meeting, "  ").status_code == 400

    store.update_meeting(meeting, private=1)
    held = Held(model)
    asked = ask(api, meeting, "When?")
    assert held.entered.wait(10)
    now = writing(api, meeting)
    assert (now["what"], now["about"]) == ("answer", "When?")
    busy = ask(api, meeting, "And who?")
    assert busy.status_code == 409 and "answering a question" in busy.json()["detail"]
    api.post(f"/api/meetings/{meeting}/writing/stop", headers=AUTH)
    got = over(api, asked)
    assert (got["state"], got["error"]) == ("stopped", "Stopped.")
    assert len(claude) == 1


def test_it_says_how_far_it_is_and_is_stopped(api, store, model):
    meeting = add_meeting(store, SHORT)
    store.save_summary(meeting, "claude-opus-5-5", {"title": "Kept"}, 1, 1, 0.1)
    put(api, local_model="small:4b")
    stop = f"/api/meetings/{meeting}/writing/stop"
    assert writing(api, meeting) is None
    assert api.post(stop, headers=AUTH).status_code == 409  # nothing to stop
    held = Held(model)
    asked = summarize_(api, meeting, by="local")
    assert held.entered.wait(10)

    assert writing(api, meeting) == {
        "id": started(asked),
        "what": "summary",
        "about": None,
        "by": "small:4b",
        "part": 1,
        "of": 1,
        "state": "writing",
        "result": None,
        "error": None,
    }
    # One thing at a time for a meeting: words redacted now would come back with it.
    busy = summarize_(api, meeting, by="local")
    assert busy.status_code == 409 and "writing its summary" in busy.json()["detail"]
    redact = {"start": 0.0, "end": 5.0}
    refused = api.post(f"/api/meetings/{meeting}/redact", headers=AUTH, json=redact)
    assert refused.status_code == 409
    assert api.post(stop, headers=AUTH).json() == {"stopping": True}

    got = over(api, asked)
    assert (got["state"], got["error"]) == ("stopped", "Stopped.")
    assert store.summary(meeting)["content"] == {"title": "Kept"}
    assert writing(api, meeting) is None and model.unloaded == ["small:4b"]
    held.go.set()
    assert over(api, summarize_(api, meeting, by="local"))["state"] == "done"  # free again


class Processing:
    """Stands in for processing a meeting, as slow as the test wants: which ones, in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entered, self.go = threading.Event(), threading.Event()
        self.order: list[int] = []
        monkeypatch.setattr(pipeline, "process", self.process)

    def process(self, store, meeting_id, log) -> None:
        self.order.append(meeting_id)
        self.entered.set()
        assert self.go.wait(20)


def test_it_waits_its_turn_after_a_meeting_being_processed(api, store, model, monkeypatch):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b")
    processing = Processing(monkeypatch)
    api.app.state.submit(add_meeting(store, SHORT))
    assert processing.entered.wait(10)
    asked = summarize_(api, meeting, by="local")

    assert writing(api, meeting)["part"] == 0 and model.calls == []  # waiting
    # Stopped while it waits, it's over at once, and never starts.
    api.post(f"/api/meetings/{meeting}/writing/stop", headers=AUTH)
    got = api.get(f"/api/writing/{started(asked)}", headers=AUTH).json()
    assert (got["state"], got["error"]) == ("stopped", "Stopped.")
    assert writing(api, meeting) is None
    asked = summarize_(api, meeting, by="local")
    processing.go.set()
    assert over(api, asked)["state"] == "done" and len(model.calls) == 1
    until(lambda: not api.app.state.worker_gpu.wanted())  # the live transcript may go on


def test_meetings_to_process_go_before_what_is_to_be_written(api, store, model, monkeypatch):
    put(api, local_model="small:4b")
    order: list = []
    processing = Processing(monkeypatch)
    processing.order = order
    first, later = add_meeting(store, SHORT), add_meeting(store, SHORT)
    to_write = [add_meeting(store, SHORT, f"To write {n}") for n in range(8)]
    model.answer = lambda call: order.append(call.user.split("\n")[0]) or ANSWER
    api.app.state.submit(first)
    assert processing.entered.wait(10)
    # A page has six requests at a time at most: none of these is kept waiting.
    asked = [summarize_(api, meeting, by="local") for meeting in to_write]
    api.app.state.submit(later)
    processing.go.set()
    assert all(over(api, one)["state"] == "done" for one in asked)
    assert order == [first, later, *(f"Meeting: To write {n}" for n in range(8))]


def test_a_meeting_recorded_meanwhile_is_processed_between_two_parts(
    api, store, model, monkeypatch
):
    put(api, local_model="small:4b")
    meeting, recorded = add_meeting(store, LONG), add_meeting(store, SHORT)
    processing = Processing(monkeypatch)
    said: list[str] = []

    def answer(call):
        if processing.order and not said:
            said.append(f"asked after {model.unloaded}")
        if not processing.order:
            api.app.state.submit(recorded)
            until(lambda: api.app.state.worker_gpu.wanted())
        return ANSWER if "Notes taken" not in call.user else {"overview": "O.", "title": "T"}

    model.answer = answer
    asked = summarize_(api, meeting, by="local")
    assert processing.entered.wait(10)
    # It waits meanwhile, with its model out of the way.
    assert writing(api, meeting)["part"] == 0 and len(model.calls) == 1
    assert model.unloaded == ["small:4b"]
    processing.go.set()
    assert over(api, asked)["state"] == "done"
    assert processing.order == [recorded] and said == ["asked after ['small:4b']"]
    assert store.summary(meeting)["content"]["overview"] == "O."


def test_stopped_while_a_meeting_goes_first_it_is_over_at_once(api, store, model, monkeypatch):
    put(api, local_model="small:4b")
    meeting, recorded = add_meeting(store, LONG), add_meeting(store, SHORT)
    processing = Processing(monkeypatch)

    def answer(call):
        if not processing.order:
            api.app.state.submit(recorded)
        return ANSWER

    model.answer = answer
    asked = summarize_(api, meeting, by="local")
    assert processing.entered.wait(10)
    api.post(f"/api/meetings/{meeting}/writing/stop", headers=AUTH)
    got = api.get(f"/api/writing/{started(asked)}", headers=AUTH).json()
    assert (got["state"], got["error"]) == ("stopped", "Stopped.")
    again = summarize_(api, meeting, by="local")  # the meeting isn't busy any more
    processing.go.set()
    assert over(api, again)["state"] == "done"
    # The stopped one went no further.
    assert api.get(f"/api/writing/{started(asked)}", headers=AUTH).json()["state"] == "stopped"
    assert store.summary(meeting) is not None


def test_what_was_written_is_kept_for_the_page(api, store, model):
    meeting = add_meeting(store, SHORT)
    store.update_meeting(meeting, private=1)
    put(api, local_model="small:4b")

    def written() -> list[tuple]:
        kept = api.get(f"/api/meetings/{meeting}", headers=AUTH).json()["written"]
        return [
            (run["what"], run["about"], run["state"], (run["result"] or {}).get("model"))
            for run in kept
        ]

    over(api, summarize_(api, meeting))  # a summary is the meeting's
    assert written() == []
    model.answer = lambda call: "Notes."
    recipe = {"recipe": "ticket"}
    over(api, api.post(f"/api/meetings/{meeting}/recipe", headers=AUTH, json=recipe))
    over(api, ask(api, meeting, "When?"))
    model.answer = lambda call: ollama.Failed("Ollama: model requires more system memory")
    over(api, ask(api, meeting, "Who?"))
    by = "ollama/small:4b"
    assert written() == [
        ("document", "ticket", "done", by),
        ("answer", "When?", "done", by),
        ("answer", "Who?", "failed", None),
    ]
    assert api.get("/api/writing/gone", headers=AUTH).status_code == 404
    last = api.get(f"/api/meetings/{meeting}", headers=AUTH).json()["written"][-1]
    assert api.get(f"/api/writing/{last['id']}").status_code == 401
    assert api.get(f"/api/writing/{last['id']}", headers=AUTH).json() == last

    # They go with the meeting's words.
    redact = {"start": 0.0, "end": 5.0}
    assert api.post(f"/api/meetings/{meeting}/redact", headers=AUTH, json=redact).is_success
    assert written() == []
    model.answer = lambda call: "Notes."
    run = started(ask(api, meeting, "When?"))
    until(lambda: written() != [])
    assert api.delete(f"/api/meetings/{meeting}", headers=AUTH).is_success
    assert api.get(f"/api/writing/{run}", headers=AUTH).status_code == 404


def test_a_meeting_deleted_while_it_is_written_is_left_alone(api, store, model):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b")
    held = Held(model)
    asked = summarize_(api, meeting, by="local")
    assert held.entered.wait(10)
    assert api.delete(f"/api/meetings/{meeting}", headers=AUTH).is_success
    assert over(api, asked)["state"] == "stopped" and store.summary(meeting) is None


def test_a_model_that_fails_says_why(api, store, model, monkeypatch):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b")
    model.answer = lambda call: ollama.Failed("Ollama: model requires more system memory")
    failed = over(api, summarize_(api, meeting, by="local"))
    assert failed["state"] == "failed" and "more system memory" in failed["error"]
    model.answer = lambda call: ValueError("odd")
    failed = over(api, summarize_(api, meeting, by="local"))
    assert (failed["state"], failed["error"]) == ("failed", "Couldn't write it: odd")

    opened = engine_api.Store

    def locked(*args, **options):
        if threading.current_thread().name == "zachcap-worker":
            raise sqlite3.OperationalError("database is locked")
        return opened(*args, **options)

    monkeypatch.setattr(engine_api, "Store", locked)
    failed = over(api, summarize_(api, meeting, by="local"))
    assert failed["error"] == "Couldn't write it: database is locked"
    monkeypatch.setattr(engine_api, "Store", opened)
    # The worker goes on after each.
    model.answer = lambda call: ANSWER
    assert over(api, summarize_(api, meeting, by="local"))["state"] == "done"


def test_clients_still_make_it_a_customer_call(store, model):
    meeting = add_meeting(store, SHORT)
    store.set_client(store.person_id("Dana"), "Contoso")
    assert clients.of_meeting(store, store.meeting(meeting))["client"] == "Contoso"
    local_summary.summary(store, meeting, "small:4b", "auto")
    assert store.meeting(meeting)["template"] == "customer" and len(model.calls) == 1
