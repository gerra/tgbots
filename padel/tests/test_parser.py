from datetime import datetime, time

import pytest

from parser import LONDON, NextQuery, ParseError, SlotQuery, parse, parse_time

# Monday 19 Oct 2026, 12:00 London (BST)
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=LONDON)


def at(text, now=NOW):
    q = parse(text, now)
    assert isinstance(q, SlotQuery)
    return q.when


@pytest.mark.parametrize("tok,expected", [
    ("18:00", time(18)), ("18", time(18)), ("6pm", time(18)), ("6:30pm", time(18, 30)),
    ("7pm", time(19)), ("19:30", time(19, 30)), ("18.30", time(18, 30)), ("12am", time(0)),
    ("12pm", time(12)), ("9am", time(9)), ("0", time(0)),
])
def test_times(tok, expected):
    assert parse_time(tok) == expected


@pytest.mark.parametrize("tok", ["24", "25:00", "18:60", "13pm", "0pm", "six", "1830", ""])
def test_bad_times(tok):
    assert parse_time(tok) is None


def test_today_tomorrow():
    assert at("today 20:00") == datetime(2026, 10, 19, 20, tzinfo=LONDON)
    assert at("tomorrow 7pm") == datetime(2026, 10, 20, 19, tzinfo=LONDON)
    assert at("TOMORROW 7 pm") == datetime(2026, 10, 20, 19, tzinfo=LONDON)


def test_weekday_next_occurrence():
    assert at("sat 18:00") == datetime(2026, 10, 24, 18, tzinfo=LONDON)
    assert at("saturday 18") == datetime(2026, 10, 24, 18, tzinfo=LONDON)
    assert at("sun 10am").date().isoformat() == "2026-10-25"
    assert at("tue 9").date().isoformat() == "2026-10-20"


def test_weekday_today_until_time_passes():
    assert at("mon 18:00") == datetime(2026, 10, 19, 18, tzinfo=LONDON)  # later today
    assert at("mon 11:00") == datetime(2026, 10, 26, 11, tzinfo=LONDON)  # gone: next week


def test_ddmm():
    assert at("24/10 19:30") == datetime(2026, 10, 24, 19, 30, tzinfo=LONDON)
    assert at("5/1 18:00").date().isoformat() == "2027-01-05"  # past date rolls to next year
    assert at("19/10 20:00").date().isoformat() == "2026-10-19"  # today stays today
    assert at("24/10/2026 18").date().isoformat() == "2026-10-24"


def test_either_order():
    assert at("18:00 sat") == at("sat 18:00")


def test_bst_and_gmt_offsets():
    assert at("sat 18:00").utcoffset().total_seconds() == 3600  # 24 Oct: BST
    assert at("mon 18:00", NOW.replace(day=20)).utcoffset().total_seconds() == 0  # 26 Oct: GMT


def test_next():
    assert isinstance(parse("next", NOW), NextQuery)
    assert isinstance(parse(["NEXT"], NOW), NextQuery)


@pytest.mark.parametrize("text", ["", "sat", "18:00", "sat 18:00 extra", "funday 18:00", "sat 25:00",
                                  "31/02 18:00", "32/10 18:00", "next week", "blah"])
def test_bad_input(text):
    with pytest.raises(ParseError):
        parse(text, NOW)


# --- indoor/outdoor and ranges ----------------------------------------------

from parser import RangeQuery  # noqa: E402


def rng(text, now=NOW):
    q = parse(text, now)
    assert isinstance(q, RangeQuery)
    return q.start.strftime("%a %H:%M"), q.end.strftime("%H:%M"), q.kind


@pytest.mark.parametrize("text,expected", [
    ("tomorrow 4pm-6pm indoor", ("Tue 16:00", "18:00", "indoor")),
    ("tomorrow 4pm - 6pm", ("Tue 16:00", "18:00", None)),
    ("tomorrow 4pm to 6pm", ("Tue 16:00", "18:00", None)),
    ("sat 16:00-18:00 outside", ("Sat 16:00", "18:00", "outdoor")),
    ("sat 16:00–18:00", ("Sat 16:00", "18:00", None)),
    ("sat 4-6pm", ("Sat 16:00", "18:00", None)),
    ("sat 11-1pm", ("Sat 11:00", "13:00", None)),
    ("sat 6pm-8", ("Sat 18:00", "20:00", None)),
    ("indoors 16-18 sat", ("Sat 16:00", "18:00", "indoor")),
    ("mon 10am-8pm", ("Mon 10:00", "20:00", None)),  # ends after now: still today
    ("mon 9am-11am", ("Mon 09:00", "11:00", None)),  # ended: next week
])
def test_ranges(text, expected):
    assert rng(text) == expected


def test_mon_range_rolls_over_when_ended():
    assert parse("mon 9am-11am", NOW).start.date().isoformat() == "2026-10-26"


def test_kind_on_slot_and_next():
    assert parse("tomorrow 6pm indoor", NOW).kind == "indoor"
    assert parse("outdoor sat 18:00", NOW).kind == "outdoor"
    assert parse("next indoor", NOW) == NextQuery("indoor")
    assert parse("next", NOW) == NextQuery()


@pytest.mark.parametrize("text", ["sat 6pm-4pm", "sat 18-16", "sat 4pm-6pm indoor outdoor", "indoor", "sat 4pm-"])
def test_bad_ranges(text):
    with pytest.raises(ParseError):
        parse(text, NOW)
