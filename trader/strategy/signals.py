"""Rule-based setup scanner: bars + quote -> scored, long-only trade candidates.

Setups (docs/SPEC.md 5.3 and 7): orb_breakout (stocks only), vwap_reclaim,
trend_pullback, rsi_reversion and volume_spike (momentum ignition). Each candidate carries
an ATR-based stop below the price and a target at least max(TARGET_RR,
risk.min_reward_risk) times the risk above it. The scanner only proposes; the decision
maker and the risk manager decide.

Stocks are evaluated on regular-hours bars with a VWAP that resets each New York
session; crypto uses a rolling 24h window for VWAP, the day's range and day change.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from trader import indicators as ind
from trader.models import AssetClass, Bar, BotSettings, Candidate, Instrument, Quote

MIN_BARS = 30  # warm-up for EMA21, RSI14, ATR14 and Bollinger(20)
TARGET_RR = 2.0  # default reward:risk of suggested targets
MAX_SPREAD_PCT = 0.5  # skip quotes whose bid/ask spread is wider than this (% of price)
STALE_BARS = 3  # no candidates when the newest bar ended more than this many bars ago
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
OPENING_RANGE = timedelta(minutes=15)
ORB_MIN_REL_VOLUME = 1.5
CRYPTO_WINDOW = timedelta(hours=24)
# volume_spike: the bar's volume vs the average of the 20 bars before it; stock spikes
# only count once the open has settled; ATR % of price must sit in a sane band (scaled
# from the 5-minute band by sqrt(bar minutes / 5), upper end capped).
SPIKE_MIN_MULTIPLE = 3.0
SPIKE_LOOKBACK = 20
SPIKE_EARLIEST = time(9, 45)
SPIKE_ATR_PCT_5MIN = (0.3, 8.0)
PERF_DAYS = 20  # sessions (stocks) or days (crypto) for the trend filter

# Stop distance in ATRs per setup.
STOP_ATR = {
    "orb_breakout": 1.5,
    "vwap_reclaim": 1.5,
    "trend_pullback": 1.5,
    "rsi_reversion": 1.0,
    "volume_spike": 1.5,
}


def _interval(bars: Sequence[Bar]) -> timedelta:
    """The bar size: the median gap between the last 21 bars (at least a minute)."""
    gaps = [(b.ts - a.ts).total_seconds() for a, b in pairwise(bars[-21:])]
    return timedelta(seconds=max(60.0, statistics.median(gaps) if gaps else 60.0))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _tick(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def _round_down(price: float) -> float:
    tick = _tick(price)
    return round(math.floor(price / tick + 1e-9) * tick, 4)


def _round_up(price: float) -> float:
    tick = _tick(price)
    return round(math.ceil(price / tick - 1e-9) * tick, 4)


@dataclass
class _Context:
    """Indicator series and features for one instrument at the latest bar."""

    instrument: Instrument
    bars: list[Bar]
    price: float
    interval: timedelta
    session: list[Bar]  # stocks: today's regular-hours bars; crypto: the last 24h
    ema9: ind.Series
    ema21: ind.Series
    rsi: ind.Series
    atr: float | None
    bb_lower: ind.Series
    vwap: ind.Series
    features: dict[str, float] = field(default_factory=dict)

    @property
    def is_stock(self) -> bool:
        return self.instrument.asset_class == AssetClass.STOCK


@dataclass(frozen=True)
class _Signal:
    setup: str
    score: float
    stop: float
    note: str
    extra: dict[str, float] = field(default_factory=dict)


class Scanner:
    def __init__(self, settings: BotSettings, tz: str = "America/New_York"):
        self.settings = settings
        self.tz = ZoneInfo(tz)

    # ------------------------------------------------------------------ public API
    def snapshot(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None = None
    ) -> dict[str, float]:
        """Indicator snapshot for the decision maker / UI (empty when there are no bars)."""
        ctx = self._context(instrument, bars, quote)
        return dict(ctx.features) if ctx else {}

    def scan(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None, now: datetime
    ) -> list[Candidate]:
        """Candidates for the latest complete bar, best score first. Empty when filters fail.

        Brokers return the newest bar while it is still forming (part of its volume and
        range), so patterns are judged on complete bars, as the backtest judges them; the
        live quote still gives the price that stops, targets and "still valid now" checks
        use."""
        rows = self._regular_hours(bars) if instrument.asset_class == AssetClass.STOCK else bars
        if len(rows) > 1 and rows[-1].ts + _interval(rows) > now:
            if quote is None or not quote.price:  # the forming bar still gives the price
                quote = Quote(instrument=instrument, last=rows[-1].close, ts=now)
            bars = rows[:-1]
        ctx = self._context(instrument, bars, quote)
        if ctx is None or not self._passes_filters(ctx, now):
            return []
        detectors: list[Callable[[_Context, datetime], _Signal | None]] = [
            self._vwap_reclaim,
            self._trend_pullback,
            self._rsi_reversion,
            self._volume_spike,
        ]
        if ctx.is_stock:
            detectors.insert(0, self._orb_breakout)
        out = []
        for detect in detectors:
            signal = detect(ctx, now)
            if signal is not None:
                candidate = self._candidate(ctx, signal)
                if candidate is not None:
                    out.append(candidate)
        return sorted(out, key=lambda c: c.score, reverse=True)

    # ------------------------------------------------------------------ context
    def _regular_hours(self, bars: Sequence[Bar]) -> list[Bar]:
        out = []
        for bar in bars:
            local = bar.ts.astimezone(self.tz)
            if local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE:
                out.append(bar)
        return out

    def _context(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None
    ) -> _Context | None:
        is_stock = instrument.asset_class == AssetClass.STOCK
        bars = self._regular_hours(bars) if is_stock else list(bars)
        if not bars:
            return None
        last = bars[-1]
        price = quote.price if quote is not None and quote.price else last.close
        closes = [b.close for b in bars]
        interval = _interval(bars)

        if is_stock:
            day = last.ts.astimezone(self.tz).date()
            session = [b for b in bars if b.ts.astimezone(self.tz).date() == day]
            vwap = ind.session_vwap(bars, self.tz)
        else:
            session = [b for b in bars if b.ts > last.ts - CRYPTO_WINDOW]
            vwap = ind.rolling_vwap(bars, CRYPTO_WINDOW)
        _, bb_upper, bb_lower = ind.bollinger(closes, 20, 2.0)
        ctx = _Context(
            instrument=instrument,
            bars=bars,
            price=price,
            interval=interval,
            session=session,
            ema9=ind.ema(closes, 9),
            ema21=ind.ema(closes, 21),
            rsi=ind.rsi(closes, 14),
            atr=ind.last(ind.atr(bars, 14)),
            bb_lower=bb_lower,
            vwap=vwap,
        )

        f = ctx.features
        f["price"] = price
        f["bars"] = float(len(bars))
        for name, value in (
            ("ema9", ind.last(ctx.ema9)),
            ("ema21", ind.last(ctx.ema21)),
            ("sma20", ind.last(ind.sma(closes, 20))),
            ("sma50", ind.last(ind.sma(closes, 50))),
            ("rsi14", ind.last(ctx.rsi)),
            ("atr14", ctx.atr),
            ("bb_upper", ind.last(bb_upper)),
            ("bb_lower", ind.last(bb_lower)),
            ("vwap", ind.last(vwap)),
            ("volume_multiple", self._volume_multiple(bars)),
            ("perf_20d_pct", self._perf_20d_pct(ctx)),
        ):
            if value is not None:
                f[name] = value
        if ctx.atr is not None and price > 0:
            f["atr_pct"] = ctx.atr / price * 100
        if f.get("vwap"):
            f["dist_vwap_pct"] = (price / f["vwap"] - 1) * 100
        f["high_of_day"] = max(max(b.high for b in session), price)
        f["low_of_day"] = min(min(b.low for b in session), price)
        reference = self._day_reference(ctx)
        if reference:
            f["day_change_pct"] = (price / reference - 1) * 100
        f["rel_volume"] = self._rel_volume(ctx)
        f["avg_daily_volume"] = self._avg_daily_volume(ctx)
        if quote is not None and quote.bid and quote.ask and quote.ask >= quote.bid > 0:
            f["spread_pct"] = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 100
        return ctx

    @staticmethod
    def _volume_multiple(bars: Sequence[Bar]) -> float | None:
        """The newest bar's volume vs the average of the SPIKE_LOOKBACK bars before it."""
        prior = bars[-SPIKE_LOOKBACK - 1 : -1]
        if len(prior) < SPIKE_LOOKBACK:
            return None
        average = sum(b.volume for b in prior) / len(prior)
        return bars[-1].volume / average if average > 0 else None

    def _perf_20d_pct(self, ctx: _Context) -> float | None:
        """Change vs the close PERF_DAYS sessions (stocks) or days (crypto) ago; None when
        the bars do not reach that far back."""
        bars = ctx.bars
        if ctx.is_stock:
            days = sorted({b.ts.astimezone(self.tz).date() for b in bars})
            if len(days) <= PERF_DAYS:
                return None
            then = days[-PERF_DAYS - 1]
            reference = [b for b in bars if b.ts.astimezone(self.tz).date() == then][-1].close
        else:
            cutoff = bars[-1].ts - timedelta(days=PERF_DAYS)
            i = bisect.bisect_right([b.ts for b in bars], cutoff) - 1
            if i < 0:
                return None
            reference = bars[i].close
        return (ctx.price / reference - 1) * 100

    def _day_reference(self, ctx: _Context) -> float | None:
        """Previous session close (stocks) or the price 24h ago (crypto)."""
        first = ctx.session[0]
        if ctx.is_stock:
            earlier = [b for b in ctx.bars if b.ts < first.ts]
            return earlier[-1].close if earlier else first.open
        return first.open

    def _rel_volume(self, ctx: _Context) -> float:
        """Stocks: today's volume so far vs the average of prior sessions up to the same time
        of day. Crypto: the last hour's volume per bar vs the rest of the window. 1.0 (neutral)
        when there is no history to compare with."""
        if ctx.is_stock:
            last_tod = ctx.bars[-1].ts.astimezone(self.tz).time()
            today = sum(b.volume for b in ctx.session)
            prior: dict[object, float] = {}
            for bar in ctx.bars[: len(ctx.bars) - len(ctx.session)]:
                local = bar.ts.astimezone(self.tz)
                if local.time() <= last_tod:
                    prior[local.date()] = prior.get(local.date(), 0.0) + bar.volume
            baseline = sum(prior.values()) / len(prior) if prior else 0.0
            return today / baseline if baseline > 0 else 1.0
        per_hour = max(1, round(timedelta(hours=1) / ctx.interval))
        recent, rest = ctx.bars[-per_hour:], ctx.bars[:-per_hour]
        if len(rest) < per_hour:
            return 1.0
        base = sum(b.volume for b in rest) / len(rest)
        return (sum(b.volume for b in recent) / len(recent)) / base if base > 0 else 1.0

    def _avg_daily_volume(self, ctx: _Context) -> float:
        """Stocks: mean volume of the complete prior sessions in the bars (today's partial
        volume when there are none, which errs low). Crypto: the last 24h volume."""
        if not ctx.is_stock:
            return sum(b.volume for b in ctx.session)
        totals: dict[object, float] = {}
        for bar in ctx.bars[: len(ctx.bars) - len(ctx.session)]:
            day = bar.ts.astimezone(self.tz).date()
            totals[day] = totals.get(day, 0.0) + bar.volume
        if totals:
            return sum(totals.values()) / len(totals)
        return sum(b.volume for b in ctx.session)

    # ------------------------------------------------------------------ filters
    def _passes_filters(self, ctx: _Context, now: datetime) -> bool:
        f = ctx.features
        if len(ctx.bars) < MIN_BARS or ctx.atr is None or ctx.atr <= 0:
            return False
        if ind.last(ctx.ema21) is None or ind.last(ctx.rsi) is None:
            return False
        if now - (ctx.bars[-1].ts + ctx.interval) > STALE_BARS * ctx.interval:
            return False  # stale data (e.g. stocks outside regular hours)
        if ctx.price < self.settings.risk.min_price:
            return False
        if ctx.is_stock and f["avg_daily_volume"] < self.settings.min_avg_volume:
            return False
        return f.get("spread_pct", 0.0) <= MAX_SPREAD_PCT

    # ------------------------------------------------------------------ setups
    def _orb_breakout(self, ctx: _Context, now: datetime) -> _Signal | None:
        """Break of the first 15 minutes' high on strong relative volume, above VWAP, with
        EMA9 > EMA21. Only fresh breaks (first close above within the last 3 bars) and not
        more than 1 ATR extended."""
        open_dt = datetime.combine(
            ctx.session[0].ts.astimezone(self.tz).date(), SESSION_OPEN, self.tz
        )
        range_end = open_dt + OPENING_RANGE
        if ctx.session[0].ts != open_dt or now < range_end:
            return None
        opening = [b for b in ctx.session if b.ts < range_end]
        after = [b for b in ctx.session if b.ts >= range_end]
        if not opening or not after:
            return None
        or_high = max(b.high for b in opening)
        or_low = min(b.low for b in opening)
        f, atr, price = ctx.features, ctx.atr or 0.0, ctx.price
        if not (
            or_high < price <= or_high + atr
            and f["rel_volume"] > ORB_MIN_REL_VOLUME
            and price > f.get("vwap", math.inf)
            and f["ema9"] > f["ema21"]
        ):
            return None
        earlier_closes = [b.close for b in after[:-3]]
        if any(c > or_high for c in earlier_closes):
            return None  # the break happened a while ago; do not chase it
        extension = (price - or_high) / atr
        trend = (f["ema9"] - f["ema21"]) / atr
        score = (
            0.6
            + min(0.15, (f["rel_volume"] - ORB_MIN_REL_VOLUME) * 0.1)
            + 0.05 * min(trend, 2.0)
            - 0.1 * extension
        )
        return _Signal(
            "orb_breakout",
            score,
            price - STOP_ATR["orb_breakout"] * atr,
            f"broke opening-range high {or_high:g} on {f['rel_volume']:.1f}x volume",
            {"or_high": or_high, "or_low": or_low},
        )

    def _vwap_reclaim(self, ctx: _Context, now: datetime) -> _Signal | None:
        """Crosses back above VWAP after trading below it, with a rising EMA9."""
        bars, vwap, ema9 = ctx.bars, ctx.vwap, ctx.ema9
        if len(ctx.session) < 6 or any(v is None for v in vwap[-6:]) or ema9[-2] is None:
            return None
        f, atr, price = ctx.features, ctx.atr or 0.0, ctx.price
        below_before = sum(1 for b, v in zip(bars[-6:-1], vwap[-6:-1], strict=True) if b.close < v)
        if not (
            price > f["vwap"]
            and bars[-1].close > vwap[-1]
            and bars[-2].close < vwap[-2]
            and below_before >= 2
            and ema9[-1] > ema9[-2]
            and price - f["vwap"] <= 0.75 * atr
        ):
            return None
        score = 0.55 + 0.1 * _clamp(f["rel_volume"] - 1.0)
        score += 0.05 if 45 <= f["rsi14"] <= 65 else 0.0
        score += 0.05 if f["ema9"] > f["ema21"] else 0.0
        return _Signal(
            "vwap_reclaim",
            score,
            price - STOP_ATR["vwap_reclaim"] * atr,
            f"reclaimed VWAP {f['vwap']:g} after {below_before} closes below",
        )

    def _trend_pullback(self, ctx: _Context, now: datetime) -> _Signal | None:
        """EMA9 > EMA21, both rising; pullback to the EMA21 that holds, then a bullish close."""
        bars, ema9, ema21 = ctx.bars, ctx.ema9, ctx.ema21
        if len(bars) < 6 or ema21[-6] is None or ema9[-6] is None:
            return None
        f, atr, price = ctx.features, ctx.atr or 0.0, ctx.price
        e21 = f["ema21"]
        last, prev = bars[-1], bars[-2]
        touched = min(last.low, prev.low) <= e21 + 0.25 * atr
        held = min(last.low, prev.low) >= e21 - 0.5 * atr and last.close > e21
        if not (
            f["ema9"] > e21
            and ema9[-1] > ema9[-6]
            and ema21[-1] > ema21[-6]
            and touched
            and held
            and last.close > last.open
            and last.close > prev.close
            and f["rsi14"] < 70
        ):
            return None
        slope = (ema21[-1] - ema21[-6]) / atr
        score = 0.6 + 0.1 * _clamp(slope) + (0.05 if price > f.get("vwap", math.inf) else 0.0)
        stop = min(price - STOP_ATR["trend_pullback"] * atr, e21 - 0.5 * atr)
        return _Signal("trend_pullback", score, stop, f"uptrend pullback held EMA21 {e21:.4g}")

    def _rsi_reversion(self, ctx: _Context, now: datetime) -> _Signal | None:
        """RSI14 below 30 turning up at/below the lower Bollinger band, but not in a steep
        downtrend (price within 2 ATR of EMA21). Lower score: counter-trend."""
        bars, rsi, lower = ctx.bars, ctx.rsi, ctx.bb_lower
        if rsi[-2] is None or lower[-2] is None or lower[-1] is None:
            return None
        f, atr, price = ctx.features, ctx.atr or 0.0, ctx.price
        oversold = min(v for v in rsi[-3:-1] if v is not None)
        at_band = bars[-1].low <= lower[-1] or bars[-2].low <= lower[-2]
        if not (oversold < 30 and rsi[-1] > rsi[-2] and at_band and price >= f["ema21"] - 2 * atr):
            return None
        score = (
            0.35
            + 0.1 * _clamp((30 - oversold) / 10)
            + (0.05 if bars[-1].close > bars[-1].open else 0)
        )
        recent_low = min(b.low for b in bars[-3:])
        stop = min(recent_low - 0.25 * atr, price - STOP_ATR["rsi_reversion"] * atr)
        return _Signal("rsi_reversion", score, stop, f"RSI {oversold:.0f} turning up at lower band")

    def _volume_spike(self, ctx: _Context, now: datetime) -> _Signal | None:
        """Momentum ignition: a bullish bar on >= 3x the 20-bar average volume, closing above
        SMA20 and SMA50 (when there are 50 bars), with the price above the session open
        (stocks) or the 24h open (crypto) and a positive 20-day trend when the history
        reaches that far. ATR % must be sane for the bar size. Stocks: not before 09:45.
        The price must still be above the spike bar's open and the averages: a spike that
        has been given back since its bar closed is no entry."""
        f, atr, price = ctx.features, ctx.atr or 0.0, ctx.price
        last = ctx.bars[-1]
        multiple, sma20, sma50 = f.get("volume_multiple"), f.get("sma20"), f.get("sma50")
        if multiple is None or multiple < SPIKE_MIN_MULTIPLE or sma20 is None:
            return None
        if ctx.is_stock and last.ts.astimezone(self.tz).time() < SPIKE_EARLIEST:
            return None  # the opening bars are always busy: wait for the open to settle
        low, high = self._spike_atr_band(ctx.interval)
        perf = f.get("perf_20d_pct")
        session_open = ctx.session[0].open
        held = min(last.close, price)  # the spike bar's close and where the price is now
        if not (
            last.close > last.open
            and price > last.open
            and held > sma20
            and (sma50 is None or held > sma50)
            and price > session_open
            and low <= f["atr_pct"] <= high
            and (perf is None or perf > 0)
        ):
            return None
        bar_range = last.high - last.low
        close_strength = (last.close - last.low) / bar_range if bar_range > 0 else 1.0
        extension = (price - sma20) / atr
        score = (
            0.55
            + 0.1 * _clamp((multiple - SPIKE_MIN_MULTIPLE) / SPIKE_MIN_MULTIPLE)
            + (0.05 if sma50 is not None else 0.0)
            + (0.05 if perf is not None else 0.0)
            + 0.05 * close_strength
            - 0.05 * max(0.0, extension - 2.0)  # far above the mean: late to the move
        )
        averages = "SMA20/50" if sma50 is not None else "SMA20"
        where = "session open" if ctx.is_stock else "24h open"
        return _Signal(
            "volume_spike",
            score,
            price - STOP_ATR["volume_spike"] * atr,
            f"volume {multiple:.1f}x the 20-bar average, above {averages} and the {where}",
            {"session_open": session_open},
        )

    @staticmethod
    def _spike_atr_band(interval: timedelta) -> tuple[float, float]:
        scale = math.sqrt(interval.total_seconds() / 300)
        low, high = SPIKE_ATR_PCT_5MIN
        return low * scale, min(high, high * scale)

    # ------------------------------------------------------------------ candidates
    def _candidate(self, ctx: _Context, signal: _Signal) -> Candidate | None:
        price = ctx.price
        stop = _round_down(signal.stop)
        if stop <= 0 or stop >= price:
            return None
        rr = max(TARGET_RR, self.settings.risk.min_reward_risk)
        target = _round_up(price + rr * (price - stop))
        return Candidate(
            instrument=ctx.instrument,
            setup=signal.setup,
            score=round(_clamp(signal.score), 3),
            price=price,
            suggested_stop=stop,
            suggested_target=target,
            features={**ctx.features, **signal.extra},
            note=signal.note,
        )
