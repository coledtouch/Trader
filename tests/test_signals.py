from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.models import AssetClass, Bar, BotSettings, Instrument, Quote, RiskLimits
from trader.strategy.signals import MIN_BARS, Scanner

NY = ZoneInfo("America/New_York")
STOCK = Instrument(symbol="TEST")
CRYPTO = Instrument(symbol="TEST", asset_class=AssetClass.CRYPTO)
PRIOR_DAY = date(2026, 10, 20)
TODAY = date(2026, 10, 21)
T0 = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
FIVE = timedelta(minutes=5)


def make_bars(start: datetime, closes, volumes, wick: float = 0.1) -> list[Bar]:
    vols = volumes if isinstance(volumes, list) else [volumes] * len(closes)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(
            Bar(
                ts=(start + FIVE * i).astimezone(UTC),
                open=prev,
                high=max(prev, c) + wick,
                low=min(prev, c) - wick,
                close=c,
                volume=vols[i],
            )
        )
        prev = c
    return out


def session(day: date, closes, volumes, **kw) -> list[Bar]:
    return make_bars(datetime.combine(day, time(9, 30), NY), closes, volumes, **kw)


def after(bars: list[Bar], minutes: float = 3) -> datetime:
    """A moment inside the newest bar."""
    return bars[-1].ts + timedelta(minutes=minutes)


def scan(bars, inst=STOCK, now=None, quote=None, settings=None):
    return Scanner(settings or BotSettings()).scan(inst, bars, quote, now or after(bars))


def setups(candidates) -> list[str]:
    return [c.setup for c in candidates]


# ----------------------------------------------------------------------------- scenarios
def orb_bars(last_close: float = 101.1, volume_today=None) -> list[Bar]:
    prior = session(PRIOR_DAY, [100.0] * 78, 10_000)
    vols = volume_today or [40_000, 30_000, 25_000, 20_000, 22_000, 45_000]
    # opening range 09:30-09:45 tops at 100.9; the 09:55 bar breaks it
    return prior + session(TODAY, [100.2, 100.8, 100.5, 100.6, 100.7, last_close], vols)


def vwap_reclaim_bars(closes=None) -> list[Bar]:
    prior = session(PRIOR_DAY, [50.0] * 78, 10_000)
    closes = closes or [50.2, 50.6, 51.0, 51.1, 50.9, 50.5, 50.3, 50.25, 50.3, 50.45, 50.75]
    return prior + session(TODAY, closes, 15_000)


def trend_pullback_bars(direction: float = 1.0) -> list[Bar]:
    closes = [100.0]
    for i in range(49):  # choppy uptrend: +0.30 / -0.15
        closes.append(closes[-1] + direction * (0.3 if i % 2 == 0 else -0.15))
    top = closes[-1]
    closes += [top - direction * 0.2, top - direction * 0.4, top - direction * 0.6]  # pullback
    closes.append(closes[-1] + direction * 0.25)  # bullish close off the EMA21
    return make_bars(T0, closes, 5.0, wick=0.08)


def rsi_reversion_bars(drop=None, turn: float = 98.4) -> list[Bar]:
    flat = [100 + (0.05 if i % 2 else -0.05) for i in range(40)]
    drop = drop or [99.7, 99.4, 99.1, 98.8, 98.5, 98.2]
    return make_bars(T0, [*flat, *drop, turn], 5.0, wick=0.45)


def assert_sane(candidate, min_rr: float = 2.0):
    assert candidate.suggested_stop < candidate.price < candidate.suggested_target
    reward = candidate.suggested_target - candidate.price
    risk = candidate.price - candidate.suggested_stop
    assert reward / risk >= min_rr - 1e-9
    assert 0 <= candidate.score <= 1
    assert candidate.direction == "long"
    assert candidate.note


# ----------------------------------------------------------------------------- orb
def test_orb_breakout_triggers():
    [cand] = scan(orb_bars())
    assert cand.setup == "orb_breakout"
    assert_sane(cand)
    assert cand.features["or_high"] == pytest.approx(100.9)
    assert cand.features["rel_volume"] > 1.5
    atr = cand.features["atr14"]
    assert cand.suggested_stop == pytest.approx(cand.price - 1.5 * atr, abs=0.011)


@pytest.mark.parametrize(
    "bars",
    [
        orb_bars(volume_today=[10_000] * 6),  # no relative volume
        orb_bars(last_close=101.3),  # more than 1 ATR above the range: chasing
        orb_bars(last_close=100.85),  # no break
    ],
)
def test_orb_breakout_does_not_trigger(bars):
    assert "orb_breakout" not in setups(scan(bars))


def test_orb_is_stocks_only_and_needs_a_complete_opening_range():
    assert scan(orb_bars(), inst=CRYPTO) == []
    early = orb_bars()[:-3]  # only the 09:30-09:40 bars exist
    assert "orb_breakout" not in setups(scan(early, now=datetime(2026, 10, 21, 9, 44, tzinfo=NY)))


def test_extended_hours_bars_are_ignored_for_stocks():
    premarket = make_bars(datetime.combine(TODAY, time(8, 0), NY), [100.0] * 6, 5_000_000)
    bars = orb_bars()
    bars = bars[:78] + premarket + bars[78:]
    assert setups(scan(bars)) == ["orb_breakout"]


# ----------------------------------------------------------------------------- vwap
def test_vwap_reclaim_triggers():
    [cand] = scan(vwap_reclaim_bars())
    assert cand.setup == "vwap_reclaim"
    assert_sane(cand)
    assert cand.suggested_stop < cand.features["vwap"] < cand.price


def test_vwap_reclaim_needs_prior_trading_below_vwap():
    rising = [50.2 + 0.05 * i for i in range(11)]
    assert "vwap_reclaim" not in setups(scan(vwap_reclaim_bars(rising)))
    still_below = [50.2, 50.6, 51.0, 51.1, 50.9, 50.5, 50.3, 50.25, 50.3, 50.45, 50.4]
    assert "vwap_reclaim" not in setups(scan(vwap_reclaim_bars(still_below)))


# ----------------------------------------------------------------------------- trend
def test_trend_pullback_triggers_for_crypto():
    [cand] = scan(trend_pullback_bars(), inst=CRYPTO)
    assert cand.setup == "trend_pullback"
    assert_sane(cand)
    assert cand.suggested_stop < cand.features["ema21"]


def test_trend_pullback_not_in_downtrend():
    assert "trend_pullback" not in setups(scan(trend_pullback_bars(-1.0), inst=CRYPTO))


# ----------------------------------------------------------------------------- rsi
def test_rsi_reversion_triggers_with_smaller_score():
    [cand] = scan(rsi_reversion_bars(), inst=CRYPTO)
    assert cand.setup == "rsi_reversion"
    assert_sane(cand)
    assert cand.score <= 0.5
    assert cand.features["rsi14"] < 35


def test_rsi_reversion_not_in_steep_downtrend_or_while_falling():
    steep = [99.0, 98.0, 97.0, 96.0, 95.0, 94.0]
    assert scan(rsi_reversion_bars(drop=steep, turn=94.2), inst=CRYPTO) == []
    assert scan(rsi_reversion_bars(turn=98.0), inst=CRYPTO) == []  # RSI still falling


# ----------------------------------------------------------------------------- filters
def test_filters_min_price_volume_bars_spread_and_staleness():
    bars = orb_bars()
    assert scan(bars, settings=BotSettings(risk=RiskLimits(min_price=150))) == []
    assert scan(bars, settings=BotSettings(min_avg_volume=1_000_000)) == []
    assert scan(bars[-(MIN_BARS - 1) :], now=after(bars)) == []
    wide = Quote(instrument=STOCK, bid=100.6, ask=101.4, last=101.1, ts=after(bars))
    assert scan(bars, quote=wide) == []
    assert scan(bars, now=bars[-1].ts + timedelta(hours=3)) == []
    # the volume filter is stocks only
    crypto = trend_pullback_bars()
    assert scan(crypto, inst=CRYPTO, settings=BotSettings(min_avg_volume=1e12))


def test_quote_price_and_min_reward_risk_shape_the_candidate():
    bars = vwap_reclaim_bars()
    quote = Quote(instrument=STOCK, bid=50.73, ask=50.75, last=50.74, ts=after(bars))
    settings = BotSettings(risk=RiskLimits(min_reward_risk=3.0))
    [cand] = scan(bars, quote=quote, settings=settings)
    assert cand.price == 50.74
    assert cand.features["spread_pct"] == pytest.approx(0.02 / 50.74 * 100)
    assert_sane(cand, min_rr=3.0)


# ----------------------------------------------------------------------------- snapshot
def test_snapshot_features():
    scanner = Scanner(BotSettings())
    snap = scanner.snapshot(STOCK, orb_bars())
    for key in (
        "price",
        "ema9",
        "ema21",
        "rsi14",
        "atr14",
        "atr_pct",
        "vwap",
        "dist_vwap_pct",
        "rel_volume",
        "day_change_pct",
        "high_of_day",
        "low_of_day",
        "avg_daily_volume",
        "bb_upper",
        "bb_lower",
    ):
        assert isinstance(snap[key], float), key
    assert snap["price"] == 101.1
    assert snap["day_change_pct"] == pytest.approx(1.1)  # vs the prior session's close
    assert snap["high_of_day"] == pytest.approx(101.2)
    assert snap["low_of_day"] == pytest.approx(100.1)
    assert snap["avg_daily_volume"] == 780_000
    assert snap["rel_volume"] == pytest.approx(182_000 / 60_000)
    assert scanner.snapshot(STOCK, []) == {}
    few = scanner.snapshot(CRYPTO, make_bars(T0, [10.0, 10.5], 1.0))
    assert few["price"] == 10.5 and "rsi14" not in few
