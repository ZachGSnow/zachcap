from zachcap.transcript import (
    Turn,
    Utterance,
    Word,
    assign_speakers,
    build_utterances,
    drop_echo,
    merge,
)


def words(*items: tuple[float, float, str]) -> list[Word]:
    return [Word(start, end, text) for start, end, text in items]


def test_words_go_to_the_turn_they_fall_in():
    turns = [Turn(0.0, 2.0, "A"), Turn(2.5, 5.0, "B")]
    ws = words((0.2, 0.5, " hi"), (2.6, 3.0, " hello"), (2.1, 2.3, " um"), (9.0, 9.2, " late"))
    assert assign_speakers(ws, turns) == ["A", "B", "A", None]


def test_utterances_split_on_speaker_change_and_long_pauses():
    turns = [Turn(0.0, 10.0, "A"), Turn(10.0, 20.0, "B")]
    ws = words((0, 1, " one"), (1.2, 2, " two"), (5, 6, " three"), (10.5, 11, " four"))
    utts = build_utterances("system", ws, turns)
    assert [(u.speaker, u.text) for u in utts] == [("A", "one two"), ("A", "three"), ("B", "four")]


def test_mic_lines_that_repeat_the_speakers_are_echo():
    system = [Utterance("system", "A", 1.0, 4.0, words((1, 4, " the cutover is friday night")))]
    mic = [
        Utterance("mic", "me", 1.2, 4.1, words((1.2, 4.1, " cutover is friday night"))),
        Utterance("mic", "me", 6.0, 8.0, words((6, 8, " sounds good to me"))),
    ]
    kept, echo = drop_echo(mic, system)
    assert [u.text for u in kept] == ["sounds good to me"]
    assert [u.text for u in echo] == ["cutover is friday night"]


def test_merge_orders_both_tracks_by_time():
    a = [Utterance("mic", "me", 2.0, 3.0), Utterance("mic", "me", 5.0, 6.0)]
    b = [Utterance("system", "A", 1.0, 2.0), Utterance("system", "A", 4.0, 4.5)]
    assert [u.start for u in merge(a, b)] == [1.0, 2.0, 4.0, 5.0]
