from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.timeparse import humanize, next_occurrence, parse_period, parse_recurrence, parse_when, strip_spans
from tests.conftest import NOW, TZ

IST = ZoneInfo(TZ)


def at(day_offset, h, m=0):
    d = NOW.date() + timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("tomorrow at 8 PM", at(1, 20)),
        ("tomorrow at 10 AM", at(1, 10)),
        ("at 3 PM", at(0, 15)),  # later today
        ("at 10 AM", at(1, 10)),  # already passed today → tomorrow
        ("tonight", at(0, 20)),
        ("tomorrow morning", at(1, 9)),
        ("at noon tomorrow", at(1, 12)),
        ("day after tomorrow at 7:30 pm", at(2, 19, 30)),
        ("next Friday at 5pm", at(6, 17)),  # NOW is Saturday
        ("Monday 9am", at(2, 9)),
        ("on 15 march at 6 pm", datetime(2027, 3, 15, 18, tzinfo=IST)),
        ("december 1st at 11:15am", datetime(2026, 12, 1, 11, 15, tzinfo=IST)),
        ("2026-11-20 at 18:30", datetime(2026, 11, 20, 18, 30, tzinfo=IST)),
        ("at 21:45", at(0, 21, 45)),
    ],
)
def test_absolute_times(text, expected):
    p = parse_when(text, NOW)
    assert p is not None and p.has_time
    assert p.dt == expected


@pytest.mark.parametrize(
    "text, delta",
    [
        ("after two hours", timedelta(hours=2)),
        ("in 30 minutes", timedelta(minutes=30)),
        ("in half an hour", timedelta(minutes=30)),
        ("remind me about this after two hours", timedelta(hours=2)),
        ("in an hour", timedelta(hours=1)),
        ("45 minutes from now", timedelta(minutes=45)),
        ("after three days", timedelta(days=3)),
        ("in a couple of weeks", timedelta(weeks=2)),
    ],
)
def test_relative_times(text, delta):
    p = parse_when(text, NOW)
    assert p is not None and p.relative and p.has_time
    assert abs((p.dt - (NOW + delta)).total_seconds()) < 61


def test_relative_days_with_explicit_time():
    p = parse_when("after three days at 5 pm", NOW)
    assert p.dt == at(3, 17)


def test_date_only_has_no_time():
    for text, offset in [("tomorrow", 1), ("next Monday", 2), ("next week", 2)]:
        p = parse_when(text, NOW)
        assert p is not None and p.has_date and not p.has_time, text
        assert p.dt.date() == at(offset, 0).date(), text


def test_next_weekday_same_day_means_a_week_later():
    p = parse_when("next saturday at 10am", NOW)
    assert p.dt == at(7, 10)


def test_ambiguous_meridiem_is_flagged():
    p = parse_when("meeting at 3", NOW)
    assert p.ambiguous_meridiem and p.dt.hour == 15
    p = parse_when("at 8 in the morning", NOW)
    assert not p.ambiguous_meridiem and p.dt.hour == 8
    p = parse_when("at 3 PM", NOW)
    assert not p.ambiguous_meridiem


def test_no_time_expression():
    assert parse_when("remember this website", NOW) is None
    assert parse_when("save this pdf", NOW) is None


def test_requires_aware_now():
    with pytest.raises(ValueError):
        parse_when("tomorrow", datetime(2026, 1, 1))


def test_utc_conversion():
    p = parse_when("tomorrow at 8 PM", NOW)
    assert p.utc == datetime(2026, 11, 8, 14, 30, tzinfo=timezone.utc)  # IST = UTC+5:30


def test_timezone_conversion_new_york():
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 7, 1, 9, 0, tzinfo=ny)
    p = parse_when("tomorrow at 8 PM", now)
    assert p.utc == datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)  # EDT = UTC-4


def test_strip_spans_removes_time_words():
    text = "watch this video tomorrow at 8 PM"
    p = parse_when(text, NOW)
    assert strip_spans(text, p.spans) == "watch this video"


def test_recurrence():
    assert parse_recurrence("remind me every day at 9") == "daily"
    assert parse_recurrence("on weekdays") == "weekdays"
    assert parse_recurrence("every monday") == "weekly"
    assert parse_recurrence("monthly rent") == "monthly"
    assert parse_recurrence("tomorrow") is None


def test_next_occurrence_preserves_wall_clock_across_dst():
    ny = "America/New_York"
    # 8:00 AM EDT on Sat 31 Oct 2026; DST ends Sun 1 Nov 2026
    first = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    nxt = next_occurrence(first, "daily", ny, after=first)
    assert nxt.astimezone(ZoneInfo(ny)).hour == 8
    assert nxt == datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)  # now EST = UTC-5


def test_next_occurrence_weekdays_and_monthly():
    fri = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    assert next_occurrence(fri, "weekdays", "UTC", after=fri).weekday() == 0
    jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=timezone.utc)
    assert next_occurrence(jan31, "monthly", "UTC", after=jan31).day == 28


def test_parse_period():
    start, end, label = parse_period("the website I saved yesterday", NOW)
    assert label == "yesterday" and start == at(-1, 0) and end == at(0, 0)
    start, end, label = parse_period("last week", NOW)
    assert start == datetime(2026, 10, 26, tzinfo=IST) and (end - start).days == 7
    assert parse_period("3 days ago", NOW)[0] == at(-3, 0)
    assert parse_period("nothing here", NOW) is None


def test_humanize():
    assert humanize(at(1, 20), NOW) == "tomorrow at 8:00 PM"
    assert humanize(at(0, 15), NOW) == "today at 3:00 PM"
