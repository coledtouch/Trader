from datetime import UTC, date, datetime, timedelta

import pytest

from trader import timeutil as tu
from trader.timeutil import (
    NY,
    US_EARLY_CLOSES,
    US_MARKET_HOLIDAYS,
    in_entry_window,
    is_early_close,
    is_trading_day,
    is_us_market_holiday,
    market_close_time,
    market_open_time,
    minutes_to_close,
    parse_hhmm,
    stock_market_open,
    trading_day,
)


def ny(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


# ---------------------------------------------------------------------- rule cross-check
def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    return date(year, month, (h + m - 7 * n + 114) % 31 + 1)


def observed(d: date) -> date | None:
    """NYSE: Saturday holidays move to Friday, Sunday ones to Monday, except that a
    Saturday New Year's Day is not observed."""
    if d.weekday() == 5:
        return None if (d.month, d.day) == (1, 1) else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def rule_holidays(year: int) -> set[date]:
    days = {
        observed(date(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),  # MLK
        nth_weekday(year, 2, 0, 3),  # Presidents
        easter(year) - timedelta(days=2),  # Good Friday
        last_weekday(year, 5, 0),  # Memorial
        observed(date(year, 6, 19)),
        observed(date(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),  # Labor
        nth_weekday(year, 11, 3, 4),  # Thanksgiving
        observed(date(year, 12, 25)),
    }
    return {d for d in days if d is not None}


@pytest.mark.parametrize("year", [2025, 2026, 2027, 2028])
def test_holiday_table_matches_nyse_rules(year: int) -> None:
    table = {d for d in US_MARKET_HOLIDAYS if d.year == year}
    expected = rule_holidays(year)
    if year == 2025:
        expected.add(date(2025, 1, 9))  # national day of mourning for President Carter
    assert table == expected


def test_holidays_and_early_closes_are_weekdays() -> None:
    for d in US_MARKET_HOLIDAYS | US_EARLY_CLOSES:
        assert d.weekday() < 5, d
    assert not US_MARKET_HOLIDAYS & US_EARLY_CLOSES


@pytest.mark.parametrize(
    "d",
    [date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24), date(2026, 11, 27)],
)
def test_early_closes(d: date) -> None:
    assert is_early_close(d)
    assert market_close_time(d) == datetime.combine(d, tu.EARLY_CLOSE, tzinfo=NY)


def test_no_early_close_when_july_3_is_the_holiday() -> None:
    assert is_us_market_holiday(date(2026, 7, 3))
    assert not is_early_close(date(2026, 7, 2))
    assert market_close_time("2026-07-02").hour == 16


def test_new_years_day_on_saturday_is_not_observed() -> None:
    assert is_trading_day(date(2027, 12, 31))
    assert not is_trading_day(date(2028, 1, 1))


def test_market_closed_on_holiday_and_weekend() -> None:
    assert not stock_market_open(ny(2026, 11, 26, 11))  # Thanksgiving
    assert not stock_market_open(ny(2026, 10, 24, 11))  # Saturday
    assert not stock_market_open(ny(2026, 4, 3, 11))  # Good Friday
    assert stock_market_open(ny(2026, 10, 21, 11))


def test_session_boundaries() -> None:
    assert not stock_market_open(ny(2026, 10, 21, 9, 29))
    assert stock_market_open(ny(2026, 10, 21, 9, 30))
    assert stock_market_open(ny(2026, 10, 21, 15, 59))
    assert not stock_market_open(ny(2026, 10, 21, 16, 0))


def test_half_day_closes_at_13() -> None:
    assert stock_market_open(ny(2026, 11, 27, 12, 59))
    assert not stock_market_open(ny(2026, 11, 27, 13, 0))
    assert minutes_to_close(ny(2026, 11, 27, 12, 30)) == pytest.approx(30)


# ---------------------------------------------------------------------- daylight saving
def test_open_in_utc_across_spring_forward() -> None:
    # DST starts Sunday 2026-03-08: the open moves from 14:30 UTC to 13:30 UTC.
    assert market_open_time("2026-03-06").astimezone(UTC).hour == 14
    assert market_open_time("2026-03-09").astimezone(UTC).hour == 13
    assert stock_market_open(datetime(2026, 3, 9, 13, 30, tzinfo=UTC))
    assert not stock_market_open(datetime(2026, 3, 6, 13, 30, tzinfo=UTC))


def test_open_in_utc_across_fall_back() -> None:
    # DST ends Sunday 2026-11-01: the close moves from 20:00 UTC to 21:00 UTC.
    assert market_close_time("2026-10-30").astimezone(UTC).hour == 20
    assert market_close_time("2026-11-02").astimezone(UTC).hour == 21
    assert stock_market_open(datetime(2026, 11, 2, 20, 30, tzinfo=UTC))
    assert not stock_market_open(datetime(2026, 10, 30, 20, 30, tzinfo=UTC))
    assert minutes_to_close(datetime(2026, 11, 2, 20, 50, tzinfo=UTC)) == pytest.approx(10)


def test_trading_day_rolls_at_new_york_midnight() -> None:
    assert trading_day(datetime(2026, 10, 22, 3, 59, tzinfo=UTC)) == "2026-10-21"
    assert trading_day(datetime(2026, 10, 22, 4, 0, tzinfo=UTC)) == "2026-10-22"
    # In winter (EST) the boundary is 05:00 UTC.
    assert trading_day(datetime(2026, 12, 2, 4, 30, tzinfo=UTC)) == "2026-12-01"
    assert trading_day(datetime(2026, 12, 2, 5, 0, tzinfo=UTC)) == "2026-12-02"
    assert trading_day(datetime(2026, 12, 2, 5, 0)) == "2026-12-02"  # naive = UTC


# ---------------------------------------------------------------------- entry window
def test_entry_window_regular_day() -> None:
    assert not in_entry_window(ny(2026, 10, 21, 9, 44))
    assert in_entry_window(ny(2026, 10, 21, 9, 45))
    assert in_entry_window(ny(2026, 10, 21, 15, 29))
    assert not in_entry_window(ny(2026, 10, 21, 15, 30))
    assert in_entry_window(ny(2026, 10, 21, 10, 5), start="10:00", cutoff="11:00")


def test_entry_window_never_before_the_open() -> None:
    assert not in_entry_window(ny(2026, 10, 21, 9, 0), start="08:00")
    assert in_entry_window(ny(2026, 10, 21, 9, 30), start="08:00")


def test_entry_window_ends_20_min_before_early_close() -> None:
    assert in_entry_window(ny(2026, 11, 27, 12, 39))
    assert not in_entry_window(ny(2026, 11, 27, 12, 40))


def test_entry_window_closed_on_holiday() -> None:
    assert not in_entry_window(ny(2026, 12, 25, 11))


def test_minutes_to_close_none_when_closed() -> None:
    assert minutes_to_close(ny(2026, 10, 21, 17)) is None
    assert minutes_to_close(ny(2026, 10, 21, 15, 50)) == pytest.approx(10)


def test_parse_hhmm() -> None:
    assert parse_hhmm("09:45").minute == 45
    for bad in ("9:45", "0945", "25:00", "ab:cd"):
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_year_outside_table_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    tu._warned_years.discard(2031)
    assert not is_us_market_holiday(date(2031, 3, 3))
    assert not is_us_market_holiday(date(2031, 3, 4))
    assert sum("2031" in r.getMessage() for r in caplog.records) == 1
