from zachcap.pipeline import merge_same_person


def row(speaker, start, end, text, track="system"):
    return (speaker, track, start, end, text, 0.9)


def test_a_voice_split_across_two_clusters_reads_as_one_line():
    who = {1: ("person", 7), 2: ("person", 7), 3: ("person", 8)}
    rows = [
        row(1, 15.0, 15.2, "The"),
        row(2, 15.3, 20.0, "printer issue is fixed."),
        row(3, 20.5, 22.0, "Great."),
        row(2, 26.0, 27.0, "Later, after a pause."),
    ]

    merged = merge_same_person(rows, who)

    assert [(r[0], r[2], r[3], r[4]) for r in merged] == [
        (1, 15.0, 20.0, "The printer issue is fixed."),
        (3, 20.5, 22.0, "Great."),
        (2, 26.0, 27.0, "Later, after a pause."),
    ]


def test_unknown_speakers_tracks_and_gaps_keep_lines_apart():
    who = {1: ("speaker", 1), 2: ("speaker", 2), 4: ("person", 9), 5: ("person", 9)}
    rows = [
        row(1, 0.0, 1.0, "One"),
        row(2, 1.2, 2.0, "Two"),  # a different unknown speaker
        row(4, 3.0, 4.0, "Three", track="mic"),
        row(5, 4.2, 5.0, "Four", track="system"),  # same person, other track
        row(None, 5.1, 5.5, "Five"),
        row(None, 5.6, 6.0, "Six"),  # unassigned words are never merged
    ]
    assert len(merge_same_person(rows, who)) == 6
