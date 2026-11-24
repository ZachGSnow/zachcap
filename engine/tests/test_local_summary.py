"""Summaries and documents written by a model on this PC. The model is a stand-in: it records what
it is asked and answers what the test says, so nothing is downloaded and no model runs."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

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
        return ollama.Reply(text.strip(), read=1000, written=100, cut_off=self.cut_off(call))


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
        ("Sam", "Meet", "5 PM", "action", "00:03"),
    ]
    assert kept["decisions"] == ["It's on Friday."] and kept["open_questions"] == []
    points = ["One.", "Two.", "Three.", "Four."]
    assert kept["who_said_what"] == [{"speaker": "Dana", "points": points}]
    assert (kept["overview"], kept["title"]) == ("It is on Friday.", "")
    # What isn't there is empty; what isn't even a list is an error.
    assert set(local_summary.held({}, part, ["Sam"], local_summary.WHOLE)) == set(kept)
    for odd in ({"topics": ["The cutover"]}, {"action_items": 3}, {"who_said_what": "Dana"}):
        with pytest.raises(ollama.Failed, match="didn't answer as asked"):
            local_summary.held(odd, part, ["Sam"], local_summary.WHOLE)


@pytest.mark.parametrize(
    ("one", "other", "same"),
    [
        ("Send the change notice", "Send change notice", True),
        ("Send the change notice", "Send the change notice to Priya", True),
        ("Replace switch 1", "Replace switch 2", False),
        ("Send the report to Dana", "Send the report to Mark", False),
        ("Call", "Call the vendor about the firewall firmware", False),
        ("", "", False),
    ],
)
def test_a_task_said_again_is_the_same_one(one, other, same):
    assert local_summary._same_task(one, other) is local_summary._same_task(other, one) is same


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
    # Everyone's points are their own, some of every part.
    points = [f"Sam's {n}." for n in range(1, of + 1)]
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


@pytest.mark.parametrize("answer", ["Sure! Here is the summary.", "[1, 2]", "{", {"topics": [1]}])
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


def test_no_transcript_no_model_no_summary(store, model):
    empty = add_meeting(store, [])
    with pytest.raises(RuntimeError, match="no transcript"):
        local_summary.summary(store, empty, "small:4b")
    meeting = add_meeting(store, SHORT)
    with pytest.raises(ollama.Failed, match="doesn't have gone:7b"):
        local_summary.summary(store, meeting, "gone:7b")
    assert model.calls == []


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
    monkeypatch.setattr(local_summary, "PART_CHARS", 2000)
    few = local_summary.lines_about(
        store, meeting, local_summary.lines_of(store, meeting), "word line"
    )
    assert 0 < sum(len(line.said()) + 1 for line in few) <= 2000 + len(few)


# Through the API ----------------------------------------------------------------------------


@pytest.fixture
def api(store, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(create_app("test-token"), base_url=LOCAL)


def put(api: TestClient, **change):
    return api.put("/api/settings", headers=AUTH, json=change)


def summarize_(api: TestClient, meeting: int, **body):
    return api.post(f"/api/meetings/{meeting}/summary", headers=AUTH, json=body)


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
    written = summarize_(api, meeting, template="standup", by="local").json()
    assert written["model"] == "ollama/small:4b" and len(model.calls) == 1
    put(api, writer="local")
    summarize_(api, meeting)
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
    assert summarize_(api, meeting).json()["model"] == "ollama/small:4b"  # though Claude is usual
    assert summarize_(api, meeting, by="claude").status_code == 400
    model.answer = lambda call: "Notes."
    recipe = {"recipe": "ticket"}
    document = api.post(f"/api/meetings/{meeting}/recipe", headers=AUTH, json=recipe).json()
    assert (document["text"], document["model"]) == ("Notes.", "ollama/small:4b")
    refused = api.post(
        f"/api/meetings/{meeting}/recipe", headers=AUTH, json={**recipe, "by": "claude"}
    )
    assert refused.status_code == 400
    model.answer = lambda call: "On Tuesday [01:01]."
    answered = ask(api, meeting, "When?").json()
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


def in_background(call) -> tuple[threading.Thread, list]:
    got: list = []
    thread = threading.Thread(target=lambda: got.append(call()), daemon=True)
    thread.start()
    return thread, got


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
    thread, got = in_background(lambda: ask(api, meeting, "When?"))
    assert held.entered.wait(10)
    assert writing(api, meeting)["what"] == "answer"
    busy = ask(api, meeting, "And who?")
    assert busy.status_code == 409 and "answering a question" in busy.json()["detail"]
    api.post(f"/api/meetings/{meeting}/writing/stop", headers=AUTH)
    thread.join(10)
    assert got[0].status_code == 409 and got[0].json()["detail"] == "Stopped."
    assert len(claude) == 1


def test_it_says_how_far_it_is_and_is_stopped(api, store, model):
    meeting = add_meeting(store, SHORT)
    store.save_summary(meeting, "claude-opus-5-5", {"title": "Kept"}, 1, 1, 0.1)
    put(api, local_model="small:4b")
    stop = f"/api/meetings/{meeting}/writing/stop"
    assert writing(api, meeting) is None
    assert api.post(stop, headers=AUTH).status_code == 409  # nothing to stop
    held = Held(model)
    thread, got = in_background(lambda: summarize_(api, meeting, by="local"))
    assert held.entered.wait(10)

    assert writing(api, meeting) == {"what": "summary", "by": "small:4b", "part": 1, "of": 1}
    # One thing at a time for a meeting: words redacted now would come back with it.
    busy = summarize_(api, meeting, by="local")
    assert busy.status_code == 409 and "writing its summary" in busy.json()["detail"]
    redact = {"start": 0.0, "end": 5.0}
    refused = api.post(f"/api/meetings/{meeting}/redact", headers=AUTH, json=redact)
    assert refused.status_code == 409
    assert api.post(stop, headers=AUTH).json() == {"stopping": True}
    thread.join(10)

    assert got[0].status_code == 409 and got[0].json()["detail"] == "Stopped."
    assert store.summary(meeting)["content"] == {"title": "Kept"}
    assert writing(api, meeting) is None and model.unloaded == ["small:4b"]
    held.go.set()
    assert summarize_(api, meeting, by="local").is_success  # free again


def test_it_waits_its_turn_after_a_meeting_being_processed(api, store, model, monkeypatch):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b")
    processing, go = threading.Event(), threading.Event()

    def process(store, meeting_id, log):
        processing.set()
        assert go.wait(20)

    monkeypatch.setattr(pipeline, "process", process)
    api.app.state.submit(add_meeting(store, SHORT))
    assert processing.wait(10)
    thread, got = in_background(lambda: summarize_(api, meeting, by="local"))
    until(lambda: writing(api, meeting) is not None)

    assert writing(api, meeting)["part"] == 0 and model.calls == []  # waiting
    # Stopped while it waits, it's over at once, and never starts.
    api.post(f"/api/meetings/{meeting}/writing/stop", headers=AUTH)
    thread.join(10)
    assert got[0].status_code == 409
    thread, got = in_background(lambda: summarize_(api, meeting, by="local"))
    until(lambda: writing(api, meeting) is not None)
    go.set()
    thread.join(20)
    assert got[0].is_success and len(model.calls) == 1
    until(lambda: not api.app.state.worker_gpu.wanted())  # the live transcript may go on


def test_a_meeting_deleted_while_it_is_written_is_left_alone(api, store, model):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b")
    held = Held(model)
    thread, got = in_background(lambda: summarize_(api, meeting, by="local"))
    assert held.entered.wait(10)
    assert api.delete(f"/api/meetings/{meeting}", headers=AUTH).is_success
    thread.join(10)
    assert got[0].status_code == 409 and store.summary(meeting) is None


def test_a_model_that_fails_says_why(api, store, model):
    meeting = add_meeting(store, SHORT)
    put(api, local_model="small:4b")
    model.answer = lambda call: ollama.Failed("Ollama: model requires more system memory")
    failed = summarize_(api, meeting, by="local")
    assert failed.status_code == 400 and "more system memory" in failed.json()["detail"]
    model.answer = lambda call: ValueError("odd")
    assert summarize_(api, meeting, by="local").json()["detail"] == "Couldn't write it: odd"
    # The worker goes on after either.
    model.answer = lambda call: ANSWER
    assert summarize_(api, meeting, by="local").is_success


def test_clients_still_make_it_a_customer_call(store, model):
    meeting = add_meeting(store, SHORT)
    store.set_client(store.person_id("Dana"), "Contoso")
    assert clients.of_meeting(store, store.meeting(meeting))["client"] == "Contoso"
    local_summary.summary(store, meeting, "small:4b", "auto")
    assert store.meeting(meeting)["template"] == "customer" and len(model.calls) == 1
