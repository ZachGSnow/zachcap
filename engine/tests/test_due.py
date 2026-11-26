"""Due dates as days (due.py): from the words that were said and the day of the meeting, or none
when the words don't say one day."""

from datetime import date

import pytest

from zachcap import due

WEDNESDAY = date(2026, 11, 25)


@pytest.mark.parametrize(
    ("words", "day"),
    [
        ("Tuesday", "2026-12-01"),
        ("by Friday", "2026-11-27"),
        ("this Friday", "2026-11-27"),
        ("Friday morning", "2026-11-27"),
        ("by 5pm Friday", "2026-11-27"),
        ("at 3 on Monday", "2026-11-30"),
        ("EOD Friday", "2026-11-27"),
        ("tomorrow", "2026-11-26"),
        ("tomorrow morning", "2026-11-26"),
        ("day after tomorrow", "2026-11-27"),
        ("today", "2026-11-25"),
        ("tonight", "2026-11-25"),
        ("this evening", "2026-11-25"),
        ("end of day", "2026-11-25"),
        ("by end of day", "2026-11-25"),
        ("EOD", "2026-11-25"),
        ("close of business", "2026-11-25"),
        ("end of the week", "2026-11-27"),
        ("by the end of the month", "2026-11-30"),
        ("in two weeks", "2026-12-09"),
        ("in a week", "2026-12-02"),
        ("in 3 days", "2026-11-28"),
        ("December 3", "2026-12-03"),
        ("Dec. 3rd", "2026-12-03"),
        ("3 December", "2026-12-03"),
        ("the 3rd of December", "2026-12-03"),
        ("the 3rd", "2026-12-03"),
        ("the 28th", "2026-11-28"),
        ("January 15", "2027-01-15"),
        ("2026-12-01", "2026-12-01"),
        # Not one day: none.
        ("Wednesday", None),  # today, or in a week?
        ("next Tuesday", None),  # the coming one, or the one after?
        ("November 20", None),  # just gone: spoken of, not set for next year
        ("next week", None),
        ("this week", None),
        ("soon", None),
        ("ASAP", None),
        ("Tuesday or Wednesday", None),
        ("Friday the 5th", None),
        ("12/3", None),  # the third of December, or the twelfth of March?
        ("Feb 30", None),
        ("2026-02-30", None),
        ("morning", None),
        ("3", None),
        ("", None),
        (None, None),
    ],
)
def test_the_words_said_make_one_day_or_none(words, day):
    found = due.day(words, WEDNESDAY)
    assert (found.isoformat() if found else None) == day


def test_the_end_of_the_week_said_at_the_weekend_isnt_clear():
    assert due.day("end of the week", date(2026, 11, 28)) is None


def test_the_3rd_said_late_in_december_is_in_january():
    assert due.day("the 3rd", date(2026, 12, 25)) == date(2027, 1, 3)
