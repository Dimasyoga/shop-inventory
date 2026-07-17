from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import pytest

from app import get_date_range, build_date_filter, _client_tz

JAKARTA = ZoneInfo("Asia/Jakarta")
# A Friday, 09:00 local.
NOW = datetime(2026, 7, 17, 9, 0, tzinfo=JAKARTA)


def test_day_spans_exactly_one_day():
    """Regression: the range used to be (ds, ds), so the day view could never match a row."""
    start, end = get_date_range("day", 0, JAKARTA, NOW)
    assert start.date() == date(2026, 7, 17)
    assert end.date() == date(2026, 7, 18)
    assert (end - start).days == 1


def test_day_offset_walks_backwards():
    start, end = get_date_range("day", 1, JAKARTA, NOW)
    assert start.date() == date(2026, 7, 16)
    assert end.date() == date(2026, 7, 17)


def test_week_includes_sunday():
    """Regression: the range ended on Sunday and was end-exclusive, dropping Sunday."""
    start, end = get_date_range("week", 0, JAKARTA, NOW)
    assert start.date() == date(2026, 7, 13)  # Monday
    assert end.date() == date(2026, 7, 20)    # next Monday
    sunday = datetime(2026, 7, 19, 12, 0, tzinfo=JAKARTA)
    assert start <= sunday < end


def test_week_offset_walks_backwards():
    start, _ = get_date_range("week", 1, JAKARTA, NOW)
    assert start.date() == date(2026, 7, 6)


def test_month_range():
    start, end = get_date_range("month", 0, JAKARTA, NOW)
    assert start.date() == date(2026, 7, 1)
    assert end.date() == date(2026, 8, 1)


def test_month_offset_underflows_year():
    start, end = get_date_range("month", 7, JAKARTA, NOW)
    assert start.date() == date(2025, 12, 1)
    assert end.date() == date(2026, 1, 1)


def test_month_negative_offset_overflows_year():
    start, end = get_date_range("month", -6, JAKARTA, NOW)
    assert start.date() == date(2027, 1, 1)
    assert end.date() == date(2027, 2, 1)


def test_year_includes_dec_31():
    """Regression: the range ended Dec 31 and was end-exclusive, dropping Dec 31."""
    start, end = get_date_range("year", 0, JAKARTA, NOW)
    assert start.date() == date(2026, 1, 1)
    assert end.date() == date(2027, 1, 1)
    nye = datetime(2026, 12, 31, 23, 0, tzinfo=JAKARTA)
    assert start <= nye < end


def test_year_offset():
    start, _ = get_date_range("year", 1, JAKARTA, NOW)
    assert start.date() == date(2025, 1, 1)


def test_unknown_unit():
    assert get_date_range("fortnight", 0, JAKARTA, NOW) == (None, None)


@pytest.mark.parametrize("unit", ["day", "week", "month", "year"])
def test_all_units_are_half_open_midnight_ranges(unit):
    start, end = get_date_range(unit, 0, JAKARTA, NOW)
    assert start < end
    for d in (start, end):
        assert (d.hour, d.minute, d.second) == (0, 0, 0)
        assert d.tzinfo is not None


def test_build_date_filter_converts_local_boundaries_to_utc():
    start, end = get_date_range("day", 0, JAKARTA, NOW)
    sql, params = build_date_filter(start, end)
    # Jakarta is UTC+7, so local midnight is 17:00 UTC the previous day.
    assert params == ("2026-07-16 17:00:00", "2026-07-17 17:00:00")
    assert "< ?" in sql
    assert "date(?)" not in sql


def test_build_date_filter_column_is_parameterizable():
    start, end = get_date_range("day", 0, JAKARTA, NOW)
    sql, _ = build_date_filter(start, end, "created_at")
    assert "o.created_at" not in sql
    assert "created_at >= ?" in sql


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/etc/passwd", "Not/AZone", "", None])
def test_client_tz_falls_back_to_utc_on_bad_input(bad):
    assert _client_tz(bad) is timezone.utc


def test_client_tz_accepts_valid_zone():
    assert _client_tz("Asia/Jakarta") == JAKARTA
