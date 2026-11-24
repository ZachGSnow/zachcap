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


QUESTION = [(1.0, 1.3, " Is"), (1.3, 1.5, " it"), (1.5, 2.0, " Tuesday"), (2.0, 2.2, " or"),
            (2.2, 2.9, " Wednesday?")]  # fmt: skip


def said_again(items: list, late: float) -> list:
    """The same words, heard a moment later: the speakers, through the mic."""
    return [(start + late, end + late, text) for start, end, text in items]


def line(track: str, items: list) -> Utterance:
    return Utterance(track, None, items[0][0], items[-1][1], words(*items))


def test_an_answer_that_repeats_a_word_of_the_question_is_kept():
    system = [line("system", QUESTION)]
    for pause in (0.2, 0.5, 1.2):
        mic = [line("mic", [(2.9 + pause, 3.5 + pause, " Wednesday.")])]
        assert drop_echo(mic, system) == (mic, [])


def test_what_you_said_first_is_kept_when_they_repeat_it():
    mic = [line("mic", [(1.0, 1.4, " Friday"), (1.4, 1.5, " at"), (1.5, 1.9, " nine.")])]
    system = [line("system", [(2.7, 3.1, " Friday"), (3.1, 3.2, " at"), (3.2, 3.6, " nine,")])]
    assert drop_echo(mic, system) == (mic, [])


def test_an_answer_in_the_same_breath_as_the_echo_is_kept_without_it():
    # One voice on the mic track (no speaker separation): the question out of the speakers,
    # then you.
    answer = [(3.5, 3.8, " Yes,"), (3.8, 4.4, " Wednesday.")]
    mic = [line("mic", said_again(QUESTION, 0.15) + answer)]
    kept, echo = drop_echo(mic, [line("system", QUESTION)])
    assert [u.text for u in kept] == ["Yes, Wednesday."] and kept[0].start == 3.5
    assert [u.text for u in echo] == ["Is it Tuesday or Wednesday?"]


def test_echo_is_told_by_when_its_words_end():
    # Whisper starts the first word of a line anywhere in the pause before it.
    heard = said_again(QUESTION, 0.1)
    heard[0] = (0.1, heard[0][1], " Is")
    heard[2] = (*heard[2][:2], " Thursday")  # quiet through the mic, and misheard
    kept, echo = drop_echo([line("mic", heard)], [line("system", QUESTION)])
    assert kept == [] and len(echo) == 1


def test_what_you_say_over_them_is_kept():
    mic = [line("mic", [(1.4, 1.8, " No,"), (1.8, 2.3, " Friday"), (2.3, 2.8, " please.")])]
    assert drop_echo(mic, [line("system", QUESTION)]) == (mic, [])


def test_nothing_is_echo_while_the_computer_audio_is_silent():
    mic, system = [line("mic", said_again(QUESTION, 0.1))], [line("system", QUESTION)]
    assert drop_echo(mic, system, lambda start, end: False) == (mic, [])
    assert drop_echo(mic, system, lambda start, end: True) == ([], mic)


def test_merge_orders_both_tracks_by_time():
    a = [Utterance("mic", "me", 2.0, 3.0), Utterance("mic", "me", 5.0, 6.0)]
    b = [Utterance("system", "A", 1.0, 2.0), Utterance("system", "A", 4.0, 4.5)]
    assert [u.start for u in merge(a, b)] == [1.0, 2.0, 4.0, 5.0]
