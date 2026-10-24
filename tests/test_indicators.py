from datetime import UTC, datetime, timedelta

import pytest

from trader import indicators as ind
from trader.models import Bar


def bar(ts: datetime, high: float, low: float, close: float, volume: float = 0.0) -> Bar:
    return Bar(ts=ts, open=close, high=high, low=low, close=close, volume=volume)


def approx_series(values):
    return [None if v is None else pytest.approx(v) for v in values]


T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)  # 09:30 New York (EST)


def test_sma():
    assert ind.sma([1, 2, 3, 4, 5], 3) == approx_series([None, None, 2, 3, 4])
    assert ind.sma([1, 2], 3) == [None, None]


def test_ema_seeded_with_sma():
    # alpha = 0.5; seed = mean(2, 4, 6) = 4; then 4 + .5*(8-4) = 6; 6 + .5*(4-6) = 5
    assert ind.ema([2, 4, 6, 8, 4], 3) == approx_series([None, None, 4, 6, 5])
    assert ind.ema([1, 2], 5) == [None, None]


def test_rsi_wilder_hand_computed():
    # changes: +1 +1 -1 +2 -1
    # i=3: gain 2/3, loss 1/3 -> RS 2 -> 66.67
    # i=4: gain (2/3*2+2)/3 = 10/9, loss (1/3*2)/3 = 2/9 -> RS 5 -> 83.33
    # i=5: gain 20/27, loss 13/27 -> RS 20/13 -> 60.61
    values = [10, 11, 12, 11, 13, 12]
    expected = [None, None, None, 100 - 100 / 3, 100 - 100 / 6, 100 - 100 / (33 / 13)]
    assert ind.rsi(values, 3) == approx_series(expected)


def test_rsi_edge_cases():
    assert ind.rsi([1, 2, 3, 4, 5], 3)[-1] == 100.0
    assert ind.rsi([5, 5, 5, 5], 3)[-1] == 50.0
    assert ind.rsi([5, 4, 3, 2], 3)[-1] == 0.0
    assert ind.rsi([1, 2, 3], 3) == [None, None, None]


def test_true_range_and_atr_wilder():
    bars = [
        bar(T0, 10, 8, 9),  # TR 2 (no previous close)
        bar(T0, 11, 9, 10),  # TR 2
        bar(T0, 12, 10, 11),  # TR 2 -> ATR 2
        bar(T0, 15, 11, 14),  # TR 4 -> ATR (2*2+4)/3 = 8/3
        bar(T0, 11, 10, 10.5),  # gap down: TR = |10-14| = 4 -> ATR (8/3*2+4)/3 = 28/9
    ]
    assert ind.true_range(bars) == [2, 2, 2, 4, 4]
    assert ind.atr(bars, 3) == approx_series([None, None, 2, 8 / 3, 28 / 9])


def test_bollinger_population_std():
    mid, upper, lower = ind.bollinger([1, 2, 3, 4, 5], 3, k=2)
    std = (2 / 3) ** 0.5
    assert mid == approx_series([None, None, 2, 3, 4])
    assert upper == approx_series([None, None, 2 + 2 * std, 3 + 2 * std, 4 + 2 * std])
    assert lower == approx_series([None, None, 2 - 2 * std, 3 - 2 * std, 4 - 2 * std])


def test_highest_lowest():
    values = [3, 1, 4, 1, 5, 9, 2, 6]
    assert ind.highest(values, 3) == [None, None, 4, 4, 5, 9, 9, 9]
    assert ind.lowest(values, 3) == [None, None, 1, 1, 1, 1, 2, 2]
    assert ind.highest(values, 1) == values


def test_pct_change_in_percent():
    assert ind.pct_change([100, 110, 99], 1) == approx_series([None, 10.0, -10.0])
    assert ind.pct_change([100, 110, 99], 2) == approx_series([None, None, -1.0])
    assert ind.pct_change([0, 1], 1) == [None, None]


def test_avg_volume():
    bars = [bar(T0, 1, 1, 1, v) for v in (10, 20, 30, 40)]
    assert ind.avg_volume(bars, 2) == approx_series([None, 15, 25, 35])


def test_session_vwap_resets_each_new_york_session():
    bars = [
        bar(T0, 11, 9, 10, 0),  # no volume yet -> None
        bar(T0 + timedelta(minutes=5), 11, 9, 10, 100),  # tp 10
        bar(T0 + timedelta(minutes=10), 13, 11, 12, 300),  # tp 12 -> (1000+3600)/400
        # 02:00 UTC on Mar 3 is still Mar 2 (21:00) in New York: same session
        bar(datetime(2026, 3, 3, 2, 0, tzinfo=UTC), 12, 12, 12, 100),
        # next NY session
        bar(datetime(2026, 3, 3, 14, 30, tzinfo=UTC), 21, 19, 20, 50),
    ]
    out = ind.session_vwap(bars)
    assert out[0] is None
    assert out[1] == pytest.approx(10)
    assert out[2] == pytest.approx(11.5)
    assert out[3] == pytest.approx((1000 + 3600 + 1200) / 500)
    assert out[4] == pytest.approx(20)
    # The same bars keyed by UTC date split differently.
    utc = ind.session_vwap(bars, session_key=lambda b: b.ts.date())
    assert utc[3] == pytest.approx(12)
    assert utc[4] == pytest.approx((1200 + 1000) / 150)


def test_rolling_vwap_window():
    t = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    bars = [
        bar(t, 10, 10, 10, 1),
        bar(t + timedelta(hours=1), 20, 20, 20, 1),
        bar(t + timedelta(hours=2), 30, 30, 30, 2),
    ]
    out = ind.rolling_vwap(bars, timedelta(hours=2))
    assert out == approx_series([10, 15, (20 + 60) / 3])


def test_last_and_validation():
    assert ind.last([]) is None
    assert ind.last([None, 2.5]) == 2.5
    assert ind.last(ind.sma([1, 2], 3)) is None
    with pytest.raises(ValueError):
        ind.sma([1, 2, 3], 0)
    with pytest.raises(ValueError):
        ind.rsi([1, 2, 3], 0)
