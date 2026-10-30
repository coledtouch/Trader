"""Pure-python technical indicators.

Every series function returns a list aligned with its input: element ``i`` is the
indicator value using data up to and including input ``i``, or ``None`` while the
indicator is still warming up. ``last()`` turns a series into its latest scalar.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Hashable, Sequence
from datetime import timedelta, tzinfo
from zoneinfo import ZoneInfo

from trader.models import Bar

NY = ZoneInfo("America/New_York")

Series = list[float | None]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def last(values: Sequence[float | None]) -> float | None:
    """Latest value of a series (None when empty or still warming up)."""
    return values[-1] if values else None


def sma(values: Sequence[float], period: int) -> Series:
    """Simple moving average."""
    _check_period(period)
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Exponential moving average, alpha = 2/(period+1), seeded with the SMA of the first
    `period` values."""
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = prev + alpha * (values[i] - prev)
        out[i] = prev
    return out


def rsi(values: Sequence[float], period: int = 14) -> Series:
    """Wilder's RSI. The first value (index `period`) uses simple averages of the first
    `period` changes; later values use Wilder smoothing. Flat prices give 50."""
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out

    def value(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 50.0 if avg_gain == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    avg_gain = sum(max(c, 0.0) for c in changes[:period]) / period
    avg_loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = changes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def true_range(bars: Sequence[Bar]) -> list[float]:
    """True range per bar; the first bar has no previous close, so it is high - low."""
    out: list[float] = []
    prev_close: float | None = None
    for b in bars:
        if prev_close is None:
            out.append(b.high - b.low)
        else:
            out.append(max(b.high - b.low, abs(b.high - prev_close), abs(b.low - prev_close)))
        prev_close = b.close
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> Series:
    """Wilder's ATR: the mean of the first `period` true ranges (index period-1), then
    ATR = (prev * (period - 1) + TR) / period."""
    _check_period(period)
    tr = true_range(bars)
    out: Series = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def bollinger(
    values: Sequence[float], period: int = 20, k: float = 2.0
) -> tuple[Series, Series, Series]:
    """Bollinger bands -> (mid, upper, lower); population standard deviation."""
    _check_period(period)
    mid: Series = [None] * len(values)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    if not values:
        return mid, upper, lower
    # Rolling sums of (v - shift) and its square: O(n), and the shift keeps the squares
    # small so the variance does not lose precision on large prices.
    shift = values[0]
    total = total_sq = 0.0
    for i, v in enumerate(values):
        d = v - shift
        total += d
        total_sq += d * d
        if i >= period:
            old = values[i - period] - shift
            total -= old
            total_sq -= old * old
        if i >= period - 1:
            mean = total / period
            std = math.sqrt(max(0.0, total_sq / period - mean * mean))
            m = mean + shift
            mid[i], upper[i], lower[i] = m, m + k * std, m - k * std
    return mid, upper, lower


def highest(values: Sequence[float], period: int) -> Series:
    """Rolling maximum over the last `period` values (including the current one)."""
    _check_period(period)
    out: Series = [None] * len(values)
    window: deque[int] = deque()  # indices with decreasing values
    for i, v in enumerate(values):
        while window and values[window[-1]] <= v:
            window.pop()
        window.append(i)
        if window[0] <= i - period:
            window.popleft()
        if i >= period - 1:
            out[i] = values[window[0]]
    return out


def lowest(values: Sequence[float], period: int) -> Series:
    """Rolling minimum over the last `period` values (including the current one)."""
    neg = highest([-v for v in values], period)
    return [None if v is None else -v for v in neg]


def pct_change(values: Sequence[float], periods: int = 1) -> Series:
    """Percent change vs `periods` values earlier, in percent (5.0 means +5%)."""
    _check_period(periods)
    out: Series = [None] * len(values)
    for i in range(periods, len(values)):
        base = values[i - periods]
        if base != 0:
            out[i] = (values[i] / base - 1.0) * 100.0
    return out


def avg_volume(bars: Sequence[Bar], period: int) -> Series:
    """Rolling mean of bar volume."""
    return sma([b.volume for b in bars], period)


def ny_session_key(tz: tzinfo = NY) -> Callable[[Bar], Hashable]:
    """Session key = calendar date of the bar in `tz` (one key per trading day)."""
    return lambda bar: bar.ts.astimezone(tz).date()


def session_vwap(
    bars: Sequence[Bar],
    tz: tzinfo | str = NY,
    session_key: Callable[[Bar], Hashable] | None = None,
) -> Series:
    """VWAP of the typical price (h+l+c)/3, reset whenever the session key changes.

    By default the key is the calendar date in `tz` (America/New_York), i.e. it resets
    each NY session. Pass `session_key` to define other sessions. None until the
    session has traded some volume.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    key_of = session_key or ny_session_key(zone)
    out: Series = [None] * len(bars)
    current: Hashable = object()
    pv = vol = 0.0
    for i, b in enumerate(bars):
        key = key_of(b)
        if key != current:
            current, pv, vol = key, 0.0, 0.0
        pv += (b.high + b.low + b.close) / 3.0 * b.volume
        vol += b.volume
        if vol > 0:
            out[i] = pv / vol
    return out


def rolling_vwap(bars: Sequence[Bar], window: timedelta = timedelta(hours=24)) -> Series:
    """VWAP of the typical price over a sliding time window ending at each bar (bars whose
    open time is within `window` of the current bar's). Used for 24h markets (crypto)."""
    out: Series = [None] * len(bars)
    start = 0
    pv = vol = 0.0
    for i, b in enumerate(bars):
        pv += (b.high + b.low + b.close) / 3.0 * b.volume
        vol += b.volume
        while bars[start].ts <= b.ts - window:
            old = bars[start]
            pv -= (old.high + old.low + old.close) / 3.0 * old.volume
            vol -= old.volume
            start += 1
        if vol > 1e-12:
            out[i] = pv / vol
    return out
