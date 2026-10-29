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


def closed(bars: list[Bar]) -> datetime:
    """The moment the newest bar closes. The scanner judges complete bars (a forming one is
    left out), as the backtest does when it scans at each bar's close."""
    return bars[-1].ts + (bars[-1].ts - bars[-2].ts)


def scan(bars, inst=STOCK, now=None, quote=None, settings=None):
    return Scanner(settings or BotSettings()).scan(inst, bars, quote, now or closed(bars))


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


def _with_forming_bar(bars: list[Bar], seconds: float = 30) -> tuple[list[Bar], datetime]:
    """The bars plus the next bar as a broker returns it `seconds` after it opened: flat at
    the last close, with a sliver of volume."""
    last = bars[-1]
    step = last.ts - bars[-2].ts
    c = last.close
    forming = Bar(ts=last.ts + step, open=c, high=c, low=c, close=c, volume=last.volume * 0.05)
    return [*bars, forming], forming.ts + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("make", "inst", "setup"),
    [
        (lambda: vwap_reclaim_bars(), STOCK, "vwap_reclaim"),
        (lambda: trend_pullback_bars(), CRYPTO, "trend_pullback"),
        (lambda: rsi_reversion_bars(), CRYPTO, "rsi_reversion"),
        (lambda: orb_bars(), STOCK, "orb_breakout"),
    ],
    ids=["vwap_reclaim", "trend_pullback", "rsi_reversion", "orb_breakout"],
)
def test_patterns_are_judged_on_the_last_complete_bar(make, inst, setup):
    """Review finding: every setup tested its pattern on the broker's still-forming bar, so
    a pattern that completed on the bar before was never seen once the next bar opened (the
    backtest, which scans complete bars, took it)."""
    bars = make()
    assert setup in setups(scan(bars, inst=inst))
    with_forming, now = _with_forming_bar(bars)
    assert setup in setups(scan(with_forming, inst=inst, now=now))


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


# ----------------------------------------------------------------------------- volume spike
def spike_bars(
    *, multiple: float = 6.0, first: float = 20.05, drift: float = 0.02, today: int = 8, **kw
) -> list[Bar]:
    """A quiet prior session at 20.00, `today` bars drifting from `first`, then one bullish
    bar (to 20.45) on `multiple` times the usual volume."""
    prior = session(PRIOR_DAY, [20.0] * 78, 10_000, **kw)
    closes = [first + drift * i for i in range(today)] + [20.45]
    volumes = [10_000] * today + [10_000 * multiple]
    return prior + session(TODAY, closes, volumes, **kw)


def spike(candidates):
    return next((c for c in candidates if c.setup == "volume_spike"), None)


def test_volume_spike_triggers_for_stocks():
    bars = spike_bars()
    cand = spike(scan(bars))
    assert cand is not None
    assert_sane(cand)
    f = cand.features
    assert f["volume_multiple"] == pytest.approx(6.0)
    assert cand.price > f["sma20"] and cand.price > f["sma50"]
    assert f["session_open"] == pytest.approx(20.05)
    assert "perf_20d_pct" not in f  # two sessions of history: the trend filter is skipped
    assert cand.suggested_stop == pytest.approx(cand.price - 1.5 * f["atr14"], abs=0.011)
    assert 0.5 <= cand.score <= 0.85
    assert "6.0x" in cand.note


def test_volume_spike_is_found_once_its_bar_has_completed():
    """Review finding: brokers return the newest bar while it forms, with part of its
    volume. A 6x spike seen two minutes in (40% of its volume, 2.4x) was missed, and on the
    next cycle it was no longer the newest bar, so it was never seen at all."""
    bars = spike_bars()
    spike_bar = bars[-1]
    partial = spike_bar.model_copy(update={"volume": spike_bar.volume * 0.4})
    assert spike(scan([*bars[:-1], partial], now=after(bars, 2))) is None  # too early to tell
    quiet = Bar(
        ts=spike_bar.ts + timedelta(minutes=5),
        open=spike_bar.close,
        high=spike_bar.close + 0.02,
        low=spike_bar.close - 0.02,
        close=spike_bar.close,
        volume=2_000,
    )
    cand = spike(scan([*bars, quiet], now=quiet.ts + timedelta(minutes=2)))  # next cycle
    assert cand is not None
    assert cand.features["volume_multiple"] == pytest.approx(6.0)
    assert spike(scan([*bars, quiet], now=quiet.ts + timedelta(minutes=5))) is None  # now old


def test_yesterdays_closing_bar_is_no_spike_at_the_open():
    """Yesterday's 15:55 bar (closing-auction volume) is the last complete bar while
    today's first bar forms; it must not count as a spike in today's session."""
    prior = session(PRIOR_DAY, [20.0 + 0.01 * i for i in range(77)] + [20.9], 10_000)
    prior[-1] = prior[-1].model_copy(update={"volume": 80_000})
    first = session(TODAY, [21.0], [10_000])
    now = first[0].ts + timedelta(minutes=2)
    quote = Quote(instrument=STOCK, bid=21.19, ask=21.21, last=21.2, ts=now)  # above the open
    assert spike(scan([*prior, *first], now=now, quote=quote)) is None


def test_a_spike_given_back_since_its_bar_closed_is_no_entry():
    """Review finding: the spike bar was judged complete while the next bar had already
    fallen below the spike's open, and the rules maker bought the failed spike."""
    bars = spike_bars()  # the 10:10 spike bar opens at 20.19 and closes at 20.45
    forming = bars[-1].model_copy(
        update={"ts": bars[-1].ts + timedelta(minutes=5), "open": 20.45, "close": 20.10}
    )
    now = forming.ts + timedelta(minutes=2)

    def at(last: float) -> Quote:
        return Quote(instrument=STOCK, bid=last - 0.01, ask=last + 0.01, last=last, ts=now)

    assert spike(scan([*bars, forming], now=now, quote=at(20.10))) is None  # below its open
    assert spike(scan([*bars, forming], now=now, quote=at(20.40))) is not None  # held


def test_volume_spike_needs_three_times_the_volume():
    assert spike(scan(spike_bars(multiple=2.5))) is None
    assert spike(scan(spike_bars(multiple=3.0))) is not None


def test_volume_spike_waits_for_the_open_to_settle():
    early = spike_bars(today=2)  # the spike is the 09:40 bar
    assert early[-1].ts.astimezone(NY).time() == time(9, 40)
    assert spike(scan(early)) is None


def test_volume_spike_needs_price_above_the_session_open():
    faded = spike_bars(first=20.6, drift=-0.05)  # gapped up to 20.60, faded to 20.25
    cand_features = Scanner(BotSettings()).snapshot(STOCK, faded)
    assert cand_features["price"] > cand_features["sma20"]  # only the open check fails
    assert spike(scan(faded)) is None


def test_volume_spike_needs_price_above_sma20():
    prior = session(PRIOR_DAY, [20.0] * 66 + [21.0] * 12, 10_000)
    today = session(TODAY, [19.5, 19.6, 19.7, 19.8, 19.9, 19.95, 20.0, 20.05, 20.3],
                    [10_000] * 8 + [60_000])  # fmt: skip
    snap = Scanner(BotSettings()).snapshot(STOCK, prior + today)
    assert snap["price"] < snap["sma20"]
    assert spike(scan(prior + today)) is None


@pytest.mark.parametrize(("wick", "inside"), [(0.1, True), (1.2, False), (0.005, False)])
def test_volume_spike_needs_a_sane_atr(wick: float, inside: bool):
    bars = spike_bars(wick=wick, drift=0.001)
    atr_pct = Scanner(BotSettings()).snapshot(STOCK, bars)["atr_pct"]
    assert (0.3 <= atr_pct <= 8.0) is inside
    assert (spike(scan(bars)) is not None) is inside


def crypto_spike_bars(start: float) -> list[Bar]:
    """21 days of hourly bars trending from `start` to 20, a quiet day, then a spike."""
    trend = [start + (20.0 - start) * i / 479 for i in range(480)]
    quiet = [20.0 + 0.01 * i for i in range(24)]
    closes = [*trend, *quiet, 20.73]
    volumes = [100.0] * (len(closes) - 1) + [600.0]
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        ts = T0 + timedelta(hours=i)
        out.append(Bar(ts=ts, open=prev, high=max(prev, c) + 0.2, low=min(prev, c) - 0.2,
                       close=c, volume=v))  # fmt: skip
        prev = c
    return out


def test_volume_spike_for_crypto_uses_the_20_day_trend():
    rising = crypto_spike_bars(15.0)
    cand = spike(scan(rising, inst=CRYPTO, now=closed(rising)))
    assert cand is not None
    assert_sane(cand)
    assert cand.features["perf_20d_pct"] > 0
    assert "24h open" in cand.note
    falling = crypto_spike_bars(25.0)
    now = closed(falling)
    snap = Scanner(BotSettings()).snapshot(CRYPTO, falling)
    assert snap["perf_20d_pct"] < 0 and snap["price"] > snap["sma50"]
    assert spike(scan(falling, inst=CRYPTO, now=now)) is None


def stock_history(start: float, sessions: int = 21) -> list[Bar]:
    """`sessions` quiet weekday sessions before PRIOR_DAY's close, stepping from `start` to
    20.00, followed by the spike scenario."""
    days, day = [], PRIOR_DAY
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    levels = [start + (20.0 - start) * i / (sessions - 1) for i in range(sessions)]
    history = [b for d, level in zip(days[:-1], levels, strict=False) for b in
               session(d, [level] * 78, 10_000)]  # fmt: skip
    return history + spike_bars()


@pytest.mark.parametrize(("start", "triggers"), [(15.0, True), (25.0, False)])
def test_volume_spike_for_stocks_uses_the_20_session_trend(start: float, triggers: bool):
    bars = stock_history(start)
    snap = Scanner(BotSettings()).snapshot(STOCK, bars)
    assert (snap["perf_20d_pct"] > 0) is triggers
    assert (spike(scan(bars)) is not None) is triggers
