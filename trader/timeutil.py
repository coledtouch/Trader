"""US stock market calendar and session helpers (New York time, NYSE holidays 2025-2028).

All functions take timezone-aware datetimes (naive ones are treated as UTC) and build
New York wall-clock times with zoneinfo, so daylight-saving changes are handled by the
tz database rather than by fixed UTC offsets. Crypto trades around the clock but uses
the same New York midnight as its P&L day boundary.

NYSE session times are defined in New York time, so NY is fixed here instead of being
read from AppConfig at import time (which would also cache the config as a side effect).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
# New entries stop this long before an early close, whatever the configured cutoff says.
EARLY_CLOSE_ENTRY_BUFFER = timedelta(minutes=20)

# NYSE full-day closures (https://www.nyse.com/markets/hours-calendars).
US_MARKET_HOLIDAYS = frozenset(
    {
        # 2025
        date(2025, 1, 1), date(2025, 1, 9),  # Jan 9: national day of mourning (Carter)
        date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18), date(2025, 5, 26),
        date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
        date(2025, 12, 25),
        # 2026 (July 4 is a Saturday: observed Friday July 3)
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027 (Juneteenth and Christmas fall on Saturdays, July 4 on a Sunday)
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
        # 2028 (New Year's Day is a Saturday and is not observed on Dec 31, 2027)
        date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
        date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
        date(2028, 12, 25),
    }
)  # fmt: skip

# 13:00 New York closes: July 3 (when a weekday before the July 4 holiday), the day after
# Thanksgiving, and Christmas Eve when it is a weekday that is not itself the holiday.
US_EARLY_CLOSES = frozenset(
    {
        date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
        date(2026, 11, 27), date(2026, 12, 24),
        date(2027, 11, 26),
        date(2028, 7, 3), date(2028, 11, 24),
    }
)  # fmt: skip

CALENDAR_YEARS = range(2025, 2029)
_warned_years: set[int] = set()


def _utc(now: datetime) -> datetime:
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now


def _check_year(d: date) -> None:
    if d.year not in CALENDAR_YEARS and d.year not in _warned_years:
        _warned_years.add(d.year)
        log.warning(
            "no NYSE holiday table for %d: weekdays are treated as full sessions; "
            "update trader/timeutil.py",
            d.year,
        )


def ny_time(now: datetime) -> datetime:
    """`now` converted to New York local time."""
    return _utc(now).astimezone(NY)


def trading_day(now: datetime) -> str:
    """New York calendar date "YYYY-MM-DD" (the one day boundary for stocks and crypto)."""
    return ny_time(now).date().isoformat()


def _as_date(day: date | str) -> date:
    return date.fromisoformat(day) if isinstance(day, str) else day


def is_us_market_holiday(d: date) -> bool:
    """True on NYSE full-day holidays."""
    _check_year(d)
    return d in US_MARKET_HOLIDAYS


def is_early_close(d: date) -> bool:
    """True on NYSE half days (13:00 New York close)."""
    _check_year(d)
    return d in US_EARLY_CLOSES


def is_trading_day(d: date) -> bool:
    """Weekday that is not an NYSE holiday."""
    return d.weekday() < 5 and not is_us_market_holiday(d)


def market_open_time(day: date | str) -> datetime:
    """09:30 New York on `day` (timezone-aware)."""
    return datetime.combine(_as_date(day), REGULAR_OPEN, tzinfo=NY)


def market_close_time(day: date | str) -> datetime:
    """16:00 New York on `day`, or 13:00 on an early-close day (timezone-aware)."""
    d = _as_date(day)
    return datetime.combine(d, EARLY_CLOSE if is_early_close(d) else REGULAR_CLOSE, tzinfo=NY)


def stock_market_open(now: datetime) -> bool:
    """True during the regular session (09:30 to the close) on NYSE trading days."""
    local = ny_time(now)
    d = local.date()
    if not is_trading_day(d):
        return False
    return market_open_time(d) <= local < market_close_time(d)


def parse_hhmm(text: str) -> time:
    """Parse "HH:MM" (24h). Raises ValueError for anything else."""
    hours, sep, minutes = text.partition(":")
    if not sep or len(hours) != 2 or len(minutes) != 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    return time(int(hours), int(minutes))


def in_entry_window(now: datetime, start: str = "09:45", cutoff: str = "15:30") -> bool:
    """True when new stock entries are allowed: a trading day, between `start` and `cutoff`
    New York time, never before the open, and ending at least 20 minutes before an early
    close."""
    local = ny_time(now)
    d = local.date()
    if not is_trading_day(d):
        return False
    begin = max(datetime.combine(d, parse_hhmm(start), tzinfo=NY), market_open_time(d))
    end = min(
        datetime.combine(d, parse_hhmm(cutoff), tzinfo=NY),
        market_close_time(d) - EARLY_CLOSE_ENTRY_BUFFER,
    )
    return begin <= local < end


def minutes_to_close(now: datetime) -> float | None:
    """Minutes until today's close while the regular session is open, else None."""
    if not stock_market_open(now):
        return None
    close = market_close_time(ny_time(now).date())
    return (close - _utc(now)).total_seconds() / 60
