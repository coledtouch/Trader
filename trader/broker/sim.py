"""Offline simulated broker: a deterministic synthetic market with deliberately unkind fills.

Market: each symbol follows a geometric random walk on a one-minute grid, with
intraday volatility/volume profiles, 30-minute volatility and trend regimes, rare
jumps and (stocks) overnight gaps. The path is a pure function of (seed, symbol, sim
start time): it is generated lazily forward and backward from the start minute, so the
order of calls never changes it. Stocks trade 09:30-16:00 New York on weekdays (market
holidays are not modelled); crypto trades around the clock. Within a minute the price
moves open -> low -> high -> close on up-minutes and open -> high -> low -> close on
down-minutes, so quotes and bars always agree.

Fills (lesson 1 in docs/SPEC.md: paper fills on thin names are unrealistically kind):
buys pay the ask and sells hit the bid; market-type fills add slippage that grows as
liquidity shrinks and as the order takes a bigger share of the minute's volume; one
order fills at most MAX_PARTICIPATION of a minute's volume (the rest waits); resting
limit orders fill only when the price trades through the limit. Stock entries carry
native bracket stop/target orders; crypto has none, so the engine's own stop logic is
exercised; `modify_stop` raises those native stops (breakeven/trailing). Commissions
come from `trader.fees` (IBKR Pro Tiered by default, or Fixed), sells included.
Cash-account semantics: no shorting, no borrowing, stock sale proceeds settle T+1
(unsettled cash may still be used, like an IBKR "No Borrow Margin" account). Quotes carry
top-of-book sizes that shrink with liquidity. `set_halted()` (a test hook) halts a symbol:
the quote freezes with `halted=True`, new entries are rejected and nothing fills until it
resumes, optionally with a gap (halts often reopen far away); exits are accepted and wait
for the reopen, as at IBKR. State is in memory only: a restarted SimBroker starts from
`starting_cash` again.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from itertools import count, pairwise
from statistics import NormalDist
from typing import get_args
from zoneinfo import ZoneInfo

from trader import fees
from trader.broker.base import Broker, BrokerError
from trader.fees import Plan
from trader.models import (
    OPEN_ORDER_STATUSES,
    AccountSnapshot,
    AssetClass,
    Bar,
    Fill,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    utcnow,
)

NY = ZoneInfo("America/New_York")
COMMISSION_PLANS: tuple[str, ...] = get_args(Plan)

# --------------------------------------------------------------------------- market model
DAY_MINUTES = 1440
SESSION_MINUTES = 390  # 09:30-16:00
STOCK_OPEN_MINUTE = 9 * 60 + 30  # minutes after midnight, New York
STOCK_CLOSE_MINUTE = 16 * 60

SPREAD_BPS_COEFF = 100.0  # spread (bps) = coeff / sqrt(average daily dollar volume in $M)
MIN_SPREAD_BPS = 0.5
MAX_SPREAD_BPS = 150.0
SLIPPAGE_BPS_COEFF = 2.5  # slippage (bps) at REF_MINUTE_DOLLAR_VOLUME, ~ 1/sqrt(liquidity)
REF_MINUTE_DOLLAR_VOLUME = 1_000_000.0
IMPACT_BPS_PER_PCT = 10.0  # extra slippage per 1% of the minute's volume the order takes
MAX_SLIPPAGE_BPS = 300.0
MAX_PARTICIPATION = 0.10  # one order fills at most this share of a minute's volume
TOP_OF_BOOK_SHARE = 0.05  # displayed size at the best bid/ask, vs a minute's volume
STOCK_LOT = 100  # quoted stock sizes are whole round lots

REGIME_MINUTES = 30
# (probability, volatility multiplier); E[multiplier^2] ~= 1 so daily vol stays calibrated.
REGIMES = ((0.20, 0.5), (0.55, 0.85), (0.18, 1.3), (0.07, 2.0))
TREND_STRENGTH = 0.15  # per-minute drift = TREND_STRENGTH * sigma * N(0, 1), per regime block
JUMP_PROB = 0.002  # chance that a minute's shock is multiplied by JUMP_MULT
JUMP_MULT = 4.0
GAP_VOL = 0.35  # overnight gap stdev as a fraction of the daily volatility (stocks)
WICK = 0.5  # wick length scale in units of the minute's sigma
VOLUME_NOISE = 0.35  # lognormal volume noise

BAR_SIZES_MINUTES = (1, 2, 3, 5, 10, 15, 20, 30, 60, 120, 180, 240, 480)
EPS = 1e-9


@dataclass(frozen=True)
class SymbolProfile:
    price: float  # price at the sim start
    daily_vol: float  # stdev of the daily log return
    daily_volume: float  # average units (shares/coins) traded per day


# Rough October 2026 levels; the default universe plus a few benchmarks.
KNOWN_PROFILES: dict[str, SymbolProfile] = {
    "STK:SPY": SymbolProfile(764.0, 0.010, 60e6),
    "STK:QQQ": SymbolProfile(690.0, 0.013, 35e6),
    "STK:AAPL": SymbolProfile(265.0, 0.016, 50e6),
    "STK:AMD": SymbolProfile(230.0, 0.030, 40e6),
    "STK:F": SymbolProfile(12.0, 0.018, 50e6),
    "STK:SOFI": SymbolProfile(28.0, 0.035, 50e6),
    "STK:INTC": SymbolProfile(38.0, 0.028, 60e6),
    "STK:T": SymbolProfile(27.0, 0.012, 35e6),
    "STK:PFE": SymbolProfile(25.0, 0.013, 40e6),
    "STK:AAL": SymbolProfile(13.0, 0.028, 35e6),
    "STK:SNAP": SymbolProfile(9.0, 0.035, 30e6),
    "STK:RIVN": SymbolProfile(14.0, 0.035, 30e6),
    "STK:NU": SymbolProfile(16.0, 0.025, 40e6),
    "CRYPTO:BTC": SymbolProfile(81_700.0, 0.028, 30_000),
    "CRYPTO:ETH": SymbolProfile(3_900.0, 0.035, 400_000),
}

_NORMAL = NormalDist()


def _hash_unit(*parts: object) -> float:
    """Stable uniform number in (0, 1) derived from `parts` (independent of PYTHONHASHSEED)."""
    digest = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") + 0.5) / 2.0**64


def _log_uniform(u: float, low: float, high: float) -> float:
    return math.exp(math.log(low) + u * (math.log(high) - math.log(low)))


def symbol_profile(instrument: Instrument) -> SymbolProfile:
    """Known profile, or a stable hash-based one for any other symbol."""
    key = instrument.key.upper()
    known = KNOWN_PROFILES.get(key)
    if known is not None:
        return known
    u_price, u_vol, u_volume = (_hash_unit("profile", key, i) for i in range(3))
    if instrument.asset_class == AssetClass.STOCK:
        price = round(_log_uniform(u_price, 3.0, 150.0), 2)
        return SymbolProfile(price, 0.015 + 0.045 * u_vol, _log_uniform(u_volume, 1e5, 3e7))
    price = _log_uniform(u_price, 0.5, 500.0)
    dollar_volume = _log_uniform(u_volume, 5e6, 5e8)
    return SymbolProfile(price, 0.03 + 0.05 * u_vol, dollar_volume / price)


def _normalized(values: list[float], rms: bool = False) -> tuple[float, ...]:
    scale = (
        math.sqrt(sum(v * v for v in values) / len(values)) if rms else sum(values) / len(values)
    )
    return tuple(v / scale for v in values)


# Intraday shapes: busy, volatile open; busy close (stocks); US-hours bump (crypto).
_STOCK_SIGMA = _normalized(
    [0.8 + 1.2 * math.exp(-k / 20) + 0.3 * math.exp(-(389 - k) / 20) for k in range(390)], rms=True
)
_STOCK_VOLUME = _normalized(
    [0.55 + 1.8 * math.exp(-k / 23) + 0.9 * math.exp(-(389 - k) / 20) for k in range(390)]
)
_CRYPTO_SIGMA = _normalized(
    [1 + 0.15 * math.cos(2 * math.pi * (k - 780) / DAY_MINUTES) for k in range(DAY_MINUTES)],
    rms=True,
)
_CRYPTO_VOLUME = _normalized(
    [1 + 0.35 * math.cos(2 * math.pi * (k - 780) / DAY_MINUTES) for k in range(DAY_MINUTES)]
)


def tick_size(price: float) -> float:
    return 0.01 if price >= 1.0 else 0.0001


def round_price(price: float, mode: str = "nearest") -> float:
    """Round to the tick: mode "nearest", "down" or "up"."""
    tick = tick_size(price)
    n = price / tick
    if mode == "down":
        n = math.floor(n + EPS)
    elif mode == "up":
        n = math.ceil(n - EPS)
    else:
        n = round(n)
    return round(n * tick, 4)


def _split(dt: datetime) -> tuple[int, float]:
    """(epoch minute, fraction of that minute elapsed)."""
    ts = dt.timestamp()
    minute = math.floor(ts / 60)
    return minute, (ts - minute * 60) / 60


def _minute_dt(minute: int, frac: float = 0.0) -> datetime:
    return datetime.fromtimestamp(minute * 60 + frac * 60, UTC)


def _ny_date(minute: int) -> date:
    return datetime.fromtimestamp(minute * 60, NY).date()


def _session_open_minute(day: date) -> int:
    return int(datetime.combine(day, time(9, 30), NY).timestamp()) // 60


def _settlement_time(ts: datetime) -> datetime:
    """T+1: proceeds settle at the start of the next weekday (New York)."""
    day = ts.astimezone(NY).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(0, 0), NY).astimezone(UTC)


def _interp(points: list[tuple[float, float]] | tuple[tuple[float, float], ...], f: float) -> float:
    """Price at fraction `f` of a piecewise-linear path [(frac, price), ...]."""
    if f <= points[0][0]:
        return points[0][1]
    for (fa, a), (fb, b) in pairwise(points):
        if f <= fb:
            return a if fb == fa else a + (b - a) * (f - fa) / (fb - fa)
    return points[-1][1]


def _first_cross(
    points: list[tuple[float, float]], level: float, *, below: bool, strict: bool
) -> tuple[float, float] | None:
    """First (frac, level) where the path reaches `level` going down (below) or up."""
    for (fa, a), (fb, b) in pairwise(points):
        if below:
            hit = b < level if strict else b <= level
        else:
            hit = b > level if strict else b >= level
        if hit:
            frac = fb if a == b else fa + (fb - fa) * (a - level) / (a - b)
            return min(max(frac, fa), fb), level
    return None


_BAR_SIZE = re.compile(r"^\s*(\d+)\s*(min|mins|hour|hours|day|days)\s*$")
_DURATION = re.compile(r"^\s*(\d+)\s*([SDWMY])\s*$")


def _parse_bar_size(text: str) -> int:
    """IBKR bar size ("5 mins", "1 hour", "1 day") -> minutes per bar."""
    match = _BAR_SIZE.match(text.lower())
    if match:
        n, unit = int(match.group(1)), match.group(2)
        if unit.startswith("day"):
            if n == 1:
                return DAY_MINUTES
        else:
            minutes = n * (60 if unit.startswith("hour") else 1)
            if minutes in BAR_SIZES_MINUTES:
                return minutes
    raise BrokerError(f"unsupported bar size {text!r}")


def _parse_duration(text: str) -> tuple[int, str]:
    """IBKR duration ("2 D", "1 W", "3600 S") -> (count, unit)."""
    match = _DURATION.match(text.upper())
    if not match or int(match.group(1)) < 1:
        raise BrokerError(f"unsupported lookback {text!r}")
    return int(match.group(1)), match.group(2)


# --------------------------------------------------------------------------- price series
class _Series:
    """Lazily generated one-minute OHLCV path for one instrument.

    Prices are stored rounded to the tick. The raw (unrounded) walk is only kept at its
    two ends, which is all that is needed to extend it in either direction.
    """

    def __init__(self, instrument: Instrument, seed: int, anchor: int, history: int):
        self.instrument = instrument
        self.is_stock = instrument.asset_class == AssetClass.STOCK
        self.profile = profile = symbol_profile(instrument)
        self._tag = f"{seed}|{instrument.key.upper()}"
        self._fwd = random.Random(self._tag + "|fwd")
        self._bwd = random.Random(self._tag + "|bwd")
        per_day = SESSION_MINUTES if self.is_stock else DAY_MINUTES
        # Regime drift adds TREND_STRENGTH^2 * REGIME_MINUTES of variance per minute; scale
        # the noise down so the daily volatility stays near profile.daily_vol.
        drift_var = TREND_STRENGTH**2 * REGIME_MINUTES
        self._sigma = profile.daily_vol / math.sqrt(per_day * (1 + drift_var))
        self._volume = profile.daily_volume / per_day
        self.typical_volume = self._volume  # average units per trading minute
        adv_musd = max(profile.daily_volume * profile.price / 1e6, 1e-9)
        spread = SPREAD_BPS_COEFF / math.sqrt(adv_musd)
        self.spread_bps = min(MAX_SPREAD_BPS, max(MIN_SPREAD_BPS, spread))
        self._history = history
        self.start = anchor  # epoch minute of element 0
        self.floor = anchor - history  # never generate before this minute
        self._raw_before = profile.price  # raw close of minute start - 1
        self._raw_last = profile.price  # raw close of minute end - 1
        self.opens, self.highs, self.lows, self.closes, self.volumes = (
            array("d") for _ in range(5)
        )
        self.trading = bytearray()
        self._regimes: dict[int, tuple[float, float]] = {}
        self._adjust: list[tuple[int, float]] = []
        # Trading halts as [start, end) minute ranges; end is None while still halted.
        self._halts: list[tuple[int, int | None]] = []

    @property
    def end(self) -> int:
        """One past the last generated minute."""
        return self.start + len(self.closes)

    # -------------------------------------------------------------- generation
    def ensure(self, lo: int, hi: int) -> None:
        """Generate minutes lo..hi (inclusive), clamped to the retention floor."""
        if hi >= self.end:
            self._extend_forward(hi)
        lo = max(lo, self.floor)
        if lo < self.start:
            self._extend_backward(max(self.floor, min(lo, self.start - DAY_MINUTES)))

    def _regime(self, minute: int) -> tuple[float, float]:
        block = minute // REGIME_MINUTES
        cached = self._regimes.get(block)
        if cached is None:
            u = _hash_unit(self._tag, "regime", block)
            mult, acc = REGIMES[-1][1], 0.0
            for prob, value in REGIMES:
                acc += prob
                if u < acc:
                    mult = value
                    break
            trend = _NORMAL.inv_cdf(_hash_unit(self._tag, "trend", block))
            cached = self._regimes[block] = (mult, trend)
        return cached

    def _params(self, minute: int) -> tuple[float, float, float, float] | None:
        """(sigma, drift, expected volume, opening gap) for a minute; None when closed."""
        local = datetime.fromtimestamp(minute * 60, NY)
        minute_of_day = local.hour * 60 + local.minute
        gap = 0.0
        if self.is_stock:
            if local.weekday() >= 5 or not STOCK_OPEN_MINUTE <= minute_of_day < STOCK_CLOSE_MINUTE:
                return None
            k = minute_of_day - STOCK_OPEN_MINUTE
            sigma_k, volume_k = _STOCK_SIGMA[k], _STOCK_VOLUME[k]
            if k == 0:
                z_gap = _NORMAL.inv_cdf(_hash_unit(self._tag, "gap", minute))
                gap = GAP_VOL * self.profile.daily_vol * z_gap
        else:
            sigma_k, volume_k = _CRYPTO_SIGMA[minute_of_day], _CRYPTO_VOLUME[minute_of_day]
        mult, trend = self._regime(minute)
        sigma = self._sigma * sigma_k * mult
        return sigma, TREND_STRENGTH * sigma * trend, self._volume * volume_k * mult, gap

    @staticmethod
    def _row(
        open_: float, close: float, sigma: float, volume: float, rng: random.Random, z: float
    ) -> tuple[float, float, float, float, float]:
        """Rounded OHLCV for a minute with raw open/close and shock z (draws wicks, volume)."""
        high = max(open_, close) * math.exp(WICK * sigma * abs(rng.gauss(0.0, 1.0)))
        low = min(open_, close) * math.exp(-WICK * sigma * abs(rng.gauss(0.0, 1.0)))
        noise = math.exp(VOLUME_NOISE * rng.gauss(0.0, 1.0) - VOLUME_NOISE**2 / 2)
        return (
            round_price(open_),
            round_price(high),
            round_price(low),
            round_price(close),
            volume * noise * (1 + 0.5 * abs(z)) / 1.4,
        )

    @staticmethod
    def _shock(rng: random.Random) -> float:
        z = rng.gauss(0.0, 1.0)
        return z * JUMP_MULT if rng.random() < JUMP_PROB else z

    def _extend_forward(self, hi: int) -> None:
        prev = self._raw_last
        columns = (self.opens, self.highs, self.lows, self.closes, self.volumes)
        for minute in range(self.end, hi + 1):
            params = self._params(minute)
            if params is None:
                flat = round_price(prev)
                row, is_open = (flat, flat, flat, flat, 0.0), 0
            else:
                sigma, drift, volume, gap = params
                z = self._shock(self._fwd)
                open_ = prev * math.exp(gap)
                prev = open_ * math.exp(drift + sigma * z)
                row, is_open = self._row(open_, prev, sigma, volume, self._fwd, z), 1
            for column, value in zip(columns, row, strict=True):
                column.append(value)
            self.trading.append(is_open)
        self._raw_last = prev
        excess = len(self.closes) - self._history - DAY_MINUTES
        if excess > 0:  # retention: drop the oldest minutes and never regenerate them
            for column in columns:
                del column[:excess]
            del self.trading[:excess]
            self.start += excess
            self.floor = self.start

    def _extend_backward(self, lo: int) -> None:
        rows: list[tuple[float, float, float, float, float]] = []
        flags = bytearray()
        close = self._raw_before
        for minute in range(self.start - 1, lo - 1, -1):
            params = self._params(minute)
            if params is None:
                flat = round_price(close)
                rows.append((flat, flat, flat, flat, 0.0))
                flags.append(0)
                continue
            sigma, drift, volume, gap = params
            z = self._shock(self._bwd)
            open_ = close / math.exp(drift + sigma * z)
            rows.append(self._row(open_, close, sigma, volume, self._bwd, z))
            flags.append(1)
            close = open_ / math.exp(gap)
        self._raw_before = close
        rows.reverse()
        flags.reverse()
        old = (self.opens, self.highs, self.lows, self.closes, self.volumes)
        new = [
            array("d", col) + prev for col, prev in zip(zip(*rows, strict=True), old, strict=True)
        ]
        self.opens, self.highs, self.lows, self.closes, self.volumes = new
        self.trading = flags + self.trading
        self.start = lo

    # -------------------------------------------------------------- access
    def adjust(self, minute: int, factor: float) -> None:
        """Scale the path from `minute` on (test hook behind SimBroker.set_price)."""
        self._adjust.append((minute, factor))

    def halt(self, minute: int) -> None:
        self._halts.append((minute, None))

    def resume(self, minute: int) -> None:
        start, _ = self._halts.pop()
        if minute > start:
            self._halts.append((start, minute))

    def halted(self, minute: int) -> bool:
        return any(a <= minute and (b is None or minute < b) for a, b in self._halts)

    def is_open(self, minute: int) -> bool:
        """Regular trading in this minute (market hours, not halted)."""
        return bool(self.trading[minute - self.start]) and not (self._halts and self.halted(minute))

    def volume(self, minute: int) -> float:
        return self.volumes[minute - self.start]

    def ohlcv(self, minute: int) -> tuple[float, float, float, float, float]:
        i = minute - self.start
        row = (self.opens[i], self.highs[i], self.lows[i], self.closes[i], self.volumes[i])
        if not self._adjust:
            return row
        factor = 1.0
        for start, f in self._adjust:
            if minute >= start:
                factor *= f
        return (*(round_price(p * factor) for p in row[:4]), row[4])

    def path(self, minute: int) -> tuple[tuple[float, float], ...]:
        o, h, low, c, _ = self.ohlcv(minute)
        if c >= o:
            return (0.0, o), (1 / 3, low), (2 / 3, h), (1.0, c)
        return (0.0, o), (1 / 3, h), (2 / 3, low), (1.0, c)

    def points(self, minute: int, f0: float, f1: float) -> list[tuple[float, float]]:
        """The path of a minute between fractions f0 and f1 (a single point when equal)."""
        path = self.path(minute)
        pts = [(f0, _interp(path, f0))]
        pts += [p for p in path if f0 < p[0] < f1]
        if f1 > f0:
            pts.append((f1, _interp(path, f1)))
        return pts

    def price_at(self, minute: int, frac: float) -> float:
        return round_price(_interp(self.path(minute), frac))

    def partial(self, minute: int, frac: float) -> tuple[float, float, float, float, float]:
        """OHLCV of a minute that is `frac` complete."""
        prices = [round_price(p) for _, p in self.points(minute, 0.0, frac)]
        return prices[0], max(prices), min(prices), prices[-1], self.volume(minute) * frac

    def bars(self, start: int, now_minute: int, now_frac: float, size: int) -> list[Bar]:
        """Aggregate minutes start..now (the last one partial) into bars of `size` minutes
        (DAY_MINUTES: one bar per New York date). Closed minutes are skipped."""
        decimals = 0 if self.is_stock else 4
        out: list[Bar] = []
        key: object = None
        first = 0
        bo = bh = bl = bc = bv = 0.0

        def emit() -> None:
            ts = _minute_dt(first)
            out.append(Bar(ts=ts, open=bo, high=bh, low=bl, close=bc, volume=round(bv, decimals)))

        trading, base, halts = self.trading, self.start, self._halts
        for minute in range(start, now_minute + 1):
            if not trading[minute - base] or (halts and self.halted(minute)):
                continue
            if minute < now_minute:
                mo, mh, ml, mc, mv = self.ohlcv(minute)
            elif now_frac > 0:
                mo, mh, ml, mc, mv = self.partial(minute, now_frac)
            else:
                break
            bucket = _ny_date(minute) if size == DAY_MINUTES else minute // size
            if bucket != key:
                if key is not None:
                    emit()
                key, first, bo, bh, bl, bc, bv = bucket, minute, mo, mh, ml, mc, mv
            else:
                bh, bl, bc, bv = max(bh, mh), min(bl, ml), mc, bv + mv
        if key is not None:
            emit()
        return out

    def bid_ask(self, price: float) -> tuple[float, float]:
        spread = max(tick_size(price), price * self.spread_bps / 1e4)
        bid = max(round_price(price - spread / 2, "down"), tick_size(price))
        ask = round_price(bid + spread, "up")
        return bid, max(ask, round(bid + tick_size(bid), 4))

    def book_sizes(self, minute: int) -> tuple[float, float]:
        """(bid size, ask size) at the top of the book: a share of the minute's volume
        (typical volume when closed), varying per minute; round lots for stocks."""
        base = TOP_OF_BOOK_SHARE * (self.volume(minute) or self.typical_volume)
        sizes = []
        for side in ("bid", "ask"):
            size = base * (0.5 + _hash_unit(self._tag, "book", minute, side))
            if self.is_stock:
                sizes.append(float(max(STOCK_LOT, math.floor(size / STOCK_LOT) * STOCK_LOT)))
            else:
                sizes.append(max(round(size, 4), 0.0001))
        return sizes[0], sizes[1]


# --------------------------------------------------------------------------- broker state
@dataclass
class _Order:
    id: str
    request: OrderRequest
    # "order" (plain order or bracket parent), "stop" (bracket child or standalone protective
    # stop) or "target" (bracket child)
    kind: str
    created_at: datetime
    updated_at: datetime
    status: OrderStatus = OrderStatus.SUBMITTED
    parent: _Order | None = None
    children: list[_Order] = field(default_factory=list)
    filled: float = 0.0
    notional: float = 0.0
    commission: float = 0.0
    message: str = ""
    triggered: bool = False  # stop child whose stop price traded: now a market order
    activated_at: datetime | None = None  # first fill (activates bracket children)
    fill_minute: int = -1
    minute_filled: float = 0.0  # qty filled in fill_minute (participation cap)

    @property
    def working(self) -> bool:
        return self.status in OPEN_ORDER_STATUSES


@dataclass
class _Position:
    qty: float = 0.0
    avg_price: float = 0.0  # average fill price (commissions excluded)
    basis: float = 0.0  # cost incl. buy commissions, for realized P&L
    opened_at: datetime | None = None


class SimBroker(Broker):
    """Offline broker on a synthetic market. Deterministic per (seed, start time).

    Sim time is `clock()` plus everything passed to `advance()`, and never runs
    backwards. Orders are matched lazily against the price path whenever the broker is
    called, so a test can move a shared fake clock or call `advance(seconds)`.
    """

    name = "sim"

    def __init__(
        self,
        seed: int = 7,
        starting_cash: float = 250.0,
        clock: Callable[[], datetime] = utcnow,
        commission_plan: Plan = "tiered",
        *,
        max_history_days: int = 30,
    ):
        if commission_plan not in COMMISSION_PLANS:
            raise ValueError(f"unknown commission plan {commission_plan!r}")
        if starting_cash < 0:
            raise ValueError("starting_cash must be >= 0")
        if max_history_days < 1:
            raise ValueError("max_history_days must be >= 1")
        self.seed = seed
        self.commission_plan: Plan = commission_plan
        self.account_id = f"SIM-{seed}"
        self._clock = clock
        self._offset = 0.0
        self._last_now = self._read_clock()
        self._anchor, frac = _split(self._last_now)
        self._cursor = (self._anchor, frac)  # sim time up to which orders were matched
        self._history = max_history_days * DAY_MINUTES
        self._series: dict[str, _Series] = {}
        self._connected = False
        self._cash = float(starting_cash)
        self._unsettled: list[tuple[datetime, float]] = []  # (settles at, amount)
        self._positions: dict[str, _Position] = {}
        self._orders: dict[str, _Order] = {}
        self._fills: list[Fill] = []
        self._realized: dict[date, float] = {}  # New York day -> realized P&L
        self._halted: dict[str, float] = {}  # instrument key -> price frozen at the halt
        # Ids include the start time so they stay unique across app restarts (the journal
        # dedupes fills by exec_id).
        self._run = format(int(self._last_now.timestamp() * 1000), "x")
        self._order_seq = count(1)
        self._exec_seq = count(1)

    # ------------------------------------------------------------------ time
    def _read_clock(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC) + timedelta(seconds=self._offset)

    def now(self) -> datetime:
        """Current sim time (UTC)."""
        now = self._read_clock()
        if now > self._last_now:
            self._last_now = now
        return self._last_now

    def advance(self, seconds: float) -> None:
        """Move sim time forward (on top of the clock) and match orders up to then."""
        if seconds < 0:
            raise ValueError("cannot move time backwards")
        self._offset += seconds
        self._sync()

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> Mode:
        return Mode.SIM

    def _require(self) -> None:
        if not self._connected:
            raise BrokerError("sim broker is not connected")

    # ------------------------------------------------------------------ account
    async def account(self) -> AccountSnapshot:
        self._require()
        self._sync()
        value = unrealized = 0.0
        for key, pos in self._positions.items():
            last = self._last_price(self._series[key])
            value += pos.qty * last
            unrealized += (last - pos.avg_price) * pos.qty
        unsettled = sum(amount for _, amount in self._unsettled)
        return AccountSnapshot(
            account_id=self.account_id,
            mode=Mode.SIM,
            equity=self._cash + value,
            cash=self._cash,
            settled_cash=max(0.0, self._cash - unsettled),
            buying_power=max(0.0, self._cash),
            realized_pnl_today=self._realized.get(self._last_now.astimezone(NY).date(), 0.0),
            unrealized_pnl=unrealized,
            day_trades_remaining=None,
            ts=self._last_now,
        )

    async def positions(self) -> list[Position]:
        self._require()
        self._sync()
        out: list[Position] = []
        for key, pos in self._positions.items():
            last = self._last_price(self._series[key])
            out.append(
                Position(
                    instrument=Instrument.from_key(key),
                    qty=pos.qty,
                    avg_cost=pos.avg_price,
                    market_price=last,
                    unrealized_pnl=(last - pos.avg_price) * pos.qty,
                    opened_at=pos.opened_at,
                )
            )
        return out

    # ------------------------------------------------------------------ market data
    def _get_series(self, instrument: Instrument) -> _Series:
        series = self._series.get(instrument.key)
        if series is None:
            series = _Series(instrument, self.seed, self._anchor, self._history)
            self._series[instrument.key] = series
        return series

    def _last_price(self, series: _Series) -> float:
        frozen = self._halted.get(series.instrument.key)
        if frozen is not None:
            return frozen
        minute, frac = self._cursor
        series.ensure(minute, minute)
        return series.price_at(minute, frac)

    async def quote(self, instrument: Instrument) -> Quote:
        self._require()
        self._sync()
        series = self._get_series(instrument)
        last = self._last_price(series)
        bid, ask = series.bid_ask(last)
        halted = instrument.key in self._halted
        bid_size, ask_size = (0.0, 0.0) if halted else series.book_sizes(self._cursor[0])
        return Quote(
            instrument=instrument,
            bid=bid,
            ask=ask,
            last=last,
            bid_size=bid_size,
            ask_size=ask_size,
            ts=self._last_now,
            halted=halted,
        )

    async def bars(
        self, instrument: Instrument, bar_size: str = "5 mins", lookback: str = "2 D"
    ) -> list[Bar]:
        """Bars (oldest first) ending with the current, still-forming bar; stocks only
        have regular-hours bars. "N D" means N sessions for stocks, N*24h for crypto."""
        self._require()
        self._sync()
        size = _parse_bar_size(bar_size)
        now_minute, now_frac = self._cursor
        series = self._get_series(instrument)
        start = max(self._lookback_start(series, lookback, now_minute), now_minute - self._history)
        if size < DAY_MINUTES:
            start -= start % size  # whole first bar
        series.ensure(start, now_minute)
        start = max(start, series.start)
        return series.bars(start, now_minute, now_frac, size)

    def _lookback_start(self, series: _Series, lookback: str, now_minute: int) -> int:
        n, unit = _parse_duration(lookback)
        if unit == "S":
            return now_minute - math.ceil(n / 60)
        max_days = self._history // DAY_MINUTES
        if not series.is_stock:
            days = n * {"D": 1, "W": 7, "M": 30, "Y": 365}[unit]
            return now_minute - min(days, max_days) * DAY_MINUTES
        sessions = min(n * {"D": 1, "W": 5, "M": 21, "Y": 252}[unit], max_days)
        local = datetime.fromtimestamp(now_minute * 60, NY)
        day = local.date()
        if local.hour * 60 + local.minute < STOCK_OPEN_MINUTE:
            day -= timedelta(days=1)
        found = 0
        while True:
            if day.weekday() < 5:
                found += 1
                if found >= sessions:
                    return _session_open_minute(day)
            day -= timedelta(days=1)

    # ------------------------------------------------------------------ test hooks
    def set_price(self, instrument: Instrument, price: float) -> None:
        """Test hook: gap the price so the current price is `price` (the path keeps its shape
        from here on, scaled; the current minute's bar is rescaled too). Working orders see
        the gap immediately, e.g. a stop above the new price triggers at the new price."""
        if price <= 0:
            raise ValueError("price must be positive")
        self._sync()
        minute, frac = self._cursor
        series = self._get_series(instrument)
        series.ensure(minute, minute)
        series.adjust(minute, price / _interp(series.path(minute), frac))
        if instrument.key in self._halted:
            self._halted[instrument.key] = round_price(price)
        self._match_now(series)

    def set_halted(
        self, symbol: str | Instrument, halted: bool, *, gap_pct: float | None = None
    ) -> None:
        """Test hook: halt or resume trading (a plain symbol means the stock).

        While halted the quote is frozen at the halt price with `halted=True` and no bid/ask
        size, new buy orders are rejected, nothing fills and no bars print. Sell orders
        (exits, protective stops) are accepted and wait for the reopen, as IBKR queues them,
        so cancelling a stop to exit never leaves a position unprotected. On resume the
        price continues from the random walk, or reopens `gap_pct` percent away from the
        halt price when given (e.g. -35), and working orders are matched against the
        reopening price at once: a stop gapped through fills there.
        """
        if gap_pct is not None and not -100 < gap_pct < math.inf:
            raise ValueError("gap_pct must be above -100")
        instrument = Instrument(symbol=symbol) if isinstance(symbol, str) else symbol
        key = instrument.key
        self._sync()
        minute, frac = self._cursor
        series = self._get_series(instrument)
        series.ensure(minute, minute)
        if halted:
            if key not in self._halted:
                self._halted[key] = self._last_price(series)
                series.halt(minute)
            return
        frozen = self._halted.pop(key, None)
        if frozen is None:
            return
        series.resume(minute)
        if gap_pct is not None:
            reopen = frozen * (1 + gap_pct / 100)
            series.adjust(minute, reopen / _interp(series.path(minute), frac))
        self._match_now(series)

    def _match_now(self, series: _Series) -> None:
        """Match the instrument's working orders against the current price point."""
        minute, frac = self._cursor
        orders = self._working(series.instrument.key)
        if orders and self._tradable(series, minute):
            self._process(series, minute, frac, frac, orders)

    def _tradable(self, series: _Series, minute: int) -> bool:
        return series.instrument.key not in self._halted and series.is_open(minute)

    # ------------------------------------------------------------------ orders
    async def place_order(self, req: OrderRequest) -> OrderRecord:
        self._require()
        self._sync()
        now = self._last_now
        # A SELL carrying stop_loss is a protective stop order (e.g. re-placed by the engine
        # after a reconnect): it sells at market once the price trades at/below stop_loss.
        standalone_stop = req.side == Side.SELL and req.stop_loss is not None
        order = _Order(
            id=f"SIM-{self._run}-{next(self._order_seq)}",
            request=req,
            kind="stop" if standalone_stop else "order",
            created_at=now,
            updated_at=now,
        )
        problem = self._validate(req)
        self._orders[order.id] = order
        if problem:
            order.status, order.message = OrderStatus.REJECTED, problem
            return self._record(order)
        if req.side == Side.BUY and self.supports_native_stops(req.instrument):
            self._attach_bracket(order)
        series = self._get_series(req.instrument)
        minute, frac = self._cursor
        series.ensure(minute, minute)
        if self._tradable(series, minute):
            price = series.price_at(minute, frac)
            self._match(
                order, series, minute, [(frac, price)], series.volume(minute), placement=True
            )
        return self._record(order)

    def _validate(self, req: OrderRequest) -> str | None:
        """Reason to reject the order, or None. Ordinary rejections never raise."""
        if req.order_type == OrderType.LIMIT and (req.limit_price is None or req.limit_price <= 0):
            return "limit order needs a positive limit_price"
        key = req.instrument.key
        if key in self._halted and req.side == Side.BUY:
            return f"trading in {req.instrument.symbol} is halted; no new entries"
        if req.side == Side.SELL:
            if req.stop_loss is not None and req.stop_loss <= 0:
                return "stop_loss must be positive"
            pos = self._positions.get(key)
            held = pos.qty if pos else 0.0
            # Protective stops are not counted: like bracket children they only ever sell
            # what is still held when they trigger.
            selling = 0.0
            if req.stop_loss is None:
                selling = sum(
                    self._remaining(o)
                    for o in self._working(key)
                    if o.kind == "order" and o.request.side == Side.SELL
                )
            if req.qty > held - selling + EPS:
                return (
                    f"sell qty {req.qty:g} exceeds position {held:g} (already selling "
                    f"{selling:g}); shorting is not allowed"
                )
            return None
        series = self._get_series(req.instrument)
        minute, frac = self._cursor
        series.ensure(minute, minute)
        last = series.price_at(minute, frac)
        ask = series.bid_ask(last)[1]
        if req.order_type == OrderType.LIMIT and req.limit_price is not None:
            entry = req.limit_price
        else:
            entry = ask * (1 + self._slippage_bps(series, minute, req.qty, ask) / 1e4)
        if self.supports_native_stops(req.instrument):
            if req.stop_loss is not None and not 0 < req.stop_loss < entry:
                return f"stop_loss {req.stop_loss:g} must be above 0 and below the entry {entry:g}"
            if req.take_profit is not None and req.take_profit <= entry:
                return f"take_profit {req.take_profit:g} must be above the entry {entry:g}"
        cost = req.qty * entry + self.estimate_commission(req.instrument, req.qty, entry)
        available = self._cash - self._reserved_cash()
        if cost > available + EPS:
            return (
                f"insufficient cash: order needs ${cost:,.2f}, ${max(available, 0):,.2f} "
                "available (cash account, no borrowing)"
            )
        return None

    def _attach_bracket(self, parent: _Order) -> None:
        req = parent.request
        common = {
            "instrument": req.instrument,
            "side": Side.SELL,
            "qty": req.qty,
            "is_exit": True,
            "decision_id": req.decision_id,
            "setup": req.setup,
        }
        children: list[tuple[str, OrderRequest]] = []
        if req.stop_loss is not None:
            children.append(
                (
                    "stop",
                    OrderRequest(
                        **common,
                        order_type=OrderType.MARKET,
                        stop_loss=req.stop_loss,
                        reason="protective stop",
                    ),
                )
            )
        if req.take_profit is not None:
            children.append(
                (
                    "target",
                    OrderRequest(
                        **common,
                        order_type=OrderType.LIMIT,
                        limit_price=req.take_profit,
                        take_profit=req.take_profit,
                        reason="take profit",
                    ),
                )
            )
        for kind, child_req in children:
            child = _Order(
                id=f"{parent.id}-{'SL' if kind == 'stop' else 'TP'}",
                request=child_req,
                kind=kind,
                created_at=parent.created_at,
                updated_at=parent.created_at,
                parent=parent,
            )
            parent.children.append(child)
            self._orders[child.id] = child

    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        self._require()
        self._sync()
        order = self._orders.get(broker_order_id)
        return self._record(order) if order else None

    async def open_orders(self) -> list[OrderRecord]:
        """Working orders, including bracket children once their parent has filled."""
        self._require()
        self._sync()
        return [self._record(o) for o in self._orders.values() if o.working and self._active(o)]

    async def cancel_order(self, broker_order_id: str) -> None:
        self._require()
        self._sync()
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerError(f"unknown order {broker_order_id}")
        if not order.working:
            return
        self._finish(order, OrderStatus.CANCELLED, "cancelled")
        if order.filled <= EPS:  # unfilled bracket parent: its children have nothing to protect
            for child in order.children:
                if child.working:
                    self._finish(child, OrderStatus.CANCELLED, "parent cancelled")

    async def cancel_all(self) -> None:
        self._require()
        self._sync()
        for order in self._orders.values():
            if order.working:
                self._finish(order, OrderStatus.CANCELLED, "cancelled (cancel all)")

    async def fills_since(self, since: datetime) -> list[Fill]:
        self._require()
        self._sync()
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        return [f for f in self._fills if f.ts >= since]

    # ------------------------------------------------------------------ capabilities
    def supports_native_stops(self, instrument: Instrument) -> bool:
        return instrument.asset_class == AssetClass.STOCK

    def supports_standalone_stops(self, instrument: Instrument) -> bool:
        # place_order rests a stop for any SELL carrying stop_loss; offered for stocks only,
        # like the native brackets, so crypto keeps exercising the engine-managed path.
        return self.supports_native_stops(instrument)

    def estimate_commission(
        self, instrument: Instrument, qty: float, price: float, side: Side = Side.BUY
    ) -> float:
        return fees.estimate_commission(instrument, qty, price, side, self.commission_plan)

    async def modify_stop(self, instrument: Instrument, new_stop: float) -> bool:
        """Raise the native protective stop(s) of a long stock position to `new_stop`
        (rounded up to the tick). Never lowers a stop: False when asked to, or when there
        is no working stop to move. A stop raised to or above the market triggers at once."""
        if not self._connected or not self.supports_native_stops(instrument):
            return False
        if not math.isfinite(new_stop) or new_stop <= 0:
            return False
        self._sync()
        key = instrument.key
        pos = self._positions.get(key)
        stops = [
            o
            for o in self._working(key)
            if o.kind == "stop" and self._active(o) and not o.triggered
        ]
        if pos is None or pos.qty <= EPS or not stops:
            return False
        target = round_price(new_stop, "up")
        moved_all = True
        for order in stops:
            current = order.request.stop_loss or 0.0
            if target < current - EPS:
                moved_all = False
            elif target > current + EPS:
                order.request = order.request.model_copy(update={"stop_loss": target})
                order.updated_at = self._last_now
        self._match_now(self._series[key])
        return moved_all

    # ------------------------------------------------------------------ matching engine
    def _working(self, key: str) -> list[_Order]:
        return [o for o in self._orders.values() if o.working and o.request.instrument.key == key]

    def _active(self, order: _Order) -> bool:
        return order.parent is None or order.parent.filled > EPS

    def _remaining(self, order: _Order) -> float:
        if order.parent is None:
            return max(0.0, order.request.qty - order.filled)
        parent = order.parent  # children share the parent's filled qty (OCA)
        return max(0.0, parent.filled - sum(c.filled for c in parent.children))

    def _reserved_cash(self) -> float:
        """Cash committed to working buy orders."""
        total = 0.0
        for order in self._orders.values():
            req = order.request
            if order.working and req.side == Side.BUY:
                remaining = self._remaining(order)
                price = req.limit_price
                if price is None or req.order_type == OrderType.MARKET:
                    series = self._series[req.instrument.key]
                    price = series.bid_ask(self._last_price(series))[1]
                total += remaining * price + self.estimate_commission(
                    req.instrument, remaining, price
                )
        return total

    def _slippage_bps(self, series: _Series, minute: int, qty: float, price: float) -> float:
        """Market-order slippage beyond the spread: grows with 1/sqrt(minute dollar volume)
        and with the share of that volume the order takes."""
        volume = series.volume(minute) or series.typical_volume  # closed minute: typical
        dollar_volume = max(volume * price, 1.0)
        bps = SLIPPAGE_BPS_COEFF * math.sqrt(REF_MINUTE_DOLLAR_VOLUME / dollar_volume)
        bps += IMPACT_BPS_PER_PCT * 100 * qty * price / dollar_volume
        return min(bps, MAX_SLIPPAGE_BPS)

    def _sync(self) -> None:
        """Match working orders against the price path from the cursor up to now."""
        now = self.now()
        minute, frac = _split(now)
        cur_minute, cur_frac = self._cursor
        if (minute, frac) <= (cur_minute, cur_frac):
            return
        self._unsettled = [(t, amount) for t, amount in self._unsettled if t > now]
        groups: dict[str, list[_Order]] = {}
        for order in self._orders.values():
            key = order.request.instrument.key
            if order.working and key not in self._halted:  # nothing trades during a halt
                groups.setdefault(key, []).append(order)
        for key in groups:
            self._series[key].ensure(cur_minute, minute)
        for m in range(cur_minute, minute + 1):
            if not groups:
                break
            f0 = cur_frac if m == cur_minute else 0.0
            f1 = frac if m == minute else 1.0
            if f1 <= f0:
                continue
            for key, orders in list(groups.items()):
                series = self._series[key]
                # minutes before series.start were trimmed (a jump longer than the history)
                if m >= series.start and series.is_open(m):
                    self._process(series, m, f0, f1, orders)
                if not any(o.working for o in orders):
                    del groups[key]
        self._cursor = (minute, frac)

    def _process(
        self, series: _Series, minute: int, f0: float, f1: float, orders: list[_Order]
    ) -> None:
        _, high, low, _, volume = series.ohlcv(minute)
        pts: list[tuple[float, float]] | None = None
        for order in orders:
            if not order.working or not self._active(order) or not self._may_fill(order, high, low):
                continue
            if pts is None:
                pts = series.points(minute, f0, f1)
            sub = pts
            parent = order.parent
            if parent is not None and parent.activated_at is not None:
                act_minute, act_frac = _split(parent.activated_at)
                if act_minute == minute and act_frac > f0:  # activated inside this window
                    sub = [(act_frac, _interp(pts, act_frac))] + [p for p in pts if p[0] > act_frac]
            self._match(order, series, minute, sub, volume)

    @staticmethod
    def _may_fill(order: _Order, high: float, low: float) -> bool:
        """Cheap whole-minute pre-check before walking the path."""
        req = order.request
        if order.kind == "stop" and not order.triggered:
            return low <= (req.stop_loss or 0.0)
        if req.order_type == OrderType.MARKET or order.triggered:
            return True
        limit = req.limit_price or 0.0
        return low < limit if req.side == Side.BUY else high > limit

    def _match(
        self,
        order: _Order,
        series: _Series,
        minute: int,
        pts: list[tuple[float, float]],
        volume: float,
        placement: bool = False,
    ) -> None:
        req = order.request
        f0, p0 = pts[0]
        if order.kind == "stop" and not order.triggered:
            stop = req.stop_loss or 0.0
            hit = (f0, p0) if p0 <= stop else _first_cross(pts, stop, below=True, strict=False)
            if hit is not None:
                order.triggered = True
                self._market_fill(order, series, minute, hit[0], hit[1], volume)
            return
        if req.order_type == OrderType.MARKET or order.triggered:
            self._market_fill(order, series, minute, f0, p0, volume)
            return
        limit = req.limit_price or 0.0
        bid, ask = series.bid_ask(p0)
        buy = req.side == Side.BUY
        if placement:  # marketable limit: fills now, never worse than the limit
            if (buy and ask <= limit) or (not buy and bid >= limit):
                self._market_fill(order, series, minute, f0, p0, volume, limit=limit)
            return
        if buy and p0 < limit:  # already traded through (gap): fill at the better price
            self._limit_fill(order, minute, f0, min(limit, ask), volume)
        elif not buy and p0 > limit:
            self._limit_fill(order, minute, f0, max(limit, bid), volume)
        else:
            hit = _first_cross(pts, limit, below=buy, strict=True)
            if hit is not None:
                self._limit_fill(order, minute, hit[0], limit, volume)

    def _market_fill(
        self,
        order: _Order,
        series: _Series,
        minute: int,
        frac: float,
        ref_price: float,
        volume: float,
        limit: float | None = None,
    ) -> None:
        req = order.request
        bid, ask = series.bid_ask(ref_price)
        want = min(self._remaining(order), MAX_PARTICIPATION * volume)
        slip = self._slippage_bps(series, minute, want, ref_price) / 1e4
        if req.side == Side.BUY:
            price = round_price(ask * (1 + slip), "up")
            price = min(price, limit) if limit is not None else price
        else:
            price = max(round_price(bid * (1 - slip), "down"), tick_size(bid))
            price = max(price, limit) if limit is not None else price
        self._limit_fill(order, minute, frac, price, volume)

    def _limit_fill(
        self, order: _Order, minute: int, frac: float, price: float, volume: float
    ) -> None:
        qty = self._fill_qty(order, minute, volume, price)
        if qty > 0:
            self._apply_fill(order, qty, price, _minute_dt(minute, frac), minute)

    def _fill_qty(self, order: _Order, minute: int, volume: float, price: float) -> float:
        """Quantity fillable now: participation cap, held qty (sells), cash (buys)."""
        req = order.request
        remaining = self._remaining(order)
        used = order.minute_filled if order.fill_minute == minute else 0.0
        qty = min(remaining, MAX_PARTICIPATION * volume - used)
        cash_limited = False
        if req.side == Side.SELL:
            pos = self._positions.get(req.instrument.key)
            if pos is None or pos.qty <= EPS:
                self._finish(
                    order, OrderStatus.CANCELLED, "no position to sell (shorting is not allowed)"
                )
                return 0.0
            qty = min(qty, pos.qty)
        elif qty > 0:
            fee = self.estimate_commission(req.instrument, qty, price, Side.BUY)
            affordable = (self._cash - fee) / price
            if affordable < qty:
                qty, cash_limited = affordable, True
        qty = self._round_qty_down(req.instrument, qty, remaining)
        if qty <= 0 and cash_limited:
            self._finish(order, OrderStatus.CANCELLED, "insufficient cash (no borrowing)")
        return qty

    @staticmethod
    def _round_qty_down(instrument: Instrument, qty: float, remaining: float) -> float:
        if qty >= remaining - EPS:
            return remaining
        if qty <= 0:
            return 0.0
        if instrument.asset_class == AssetClass.STOCK:
            whole = abs(remaining - round(remaining)) < EPS
            decimals = 0 if whole else 4
        else:
            decimals = 6
        factor = 10**decimals
        return math.floor(qty * factor + EPS) / factor

    def _apply_fill(
        self, order: _Order, qty: float, price: float, ts: datetime, minute: int
    ) -> None:
        req = order.request
        key = req.instrument.key
        order.filled += qty
        order.notional += qty * price
        total_fee = self.estimate_commission(
            req.instrument, order.filled, order.notional / order.filled, req.side
        )
        fee = max(0.0, total_fee - order.commission)
        order.commission += fee
        order.updated_at = ts
        if order.fill_minute != minute:
            order.fill_minute, order.minute_filled = minute, 0.0
        order.minute_filled += qty
        if order.activated_at is None:
            order.activated_at = ts

        if req.side == Side.BUY:
            pos = self._positions.setdefault(key, _Position(opened_at=ts))
            self._cash -= qty * price + fee
            pos.avg_price = (pos.avg_price * pos.qty + price * qty) / (pos.qty + qty)
            pos.basis += qty * price + fee
            pos.qty += qty
        else:
            pos = self._positions[key]
            proceeds = qty * price - fee
            self._cash += proceeds
            if req.instrument.asset_class == AssetClass.STOCK:
                self._unsettled.append((_settlement_time(ts), proceeds))
            unit_basis = pos.basis / pos.qty
            day = ts.astimezone(NY).date()
            self._realized[day] = self._realized.get(day, 0.0) + proceeds - unit_basis * qty
            pos.qty -= qty
            pos.basis -= unit_basis * qty
        self._fills.append(
            Fill(
                exec_id=f"SIM-{self._run}-E{next(self._exec_seq)}",
                broker_order_id=order.id,
                instrument=req.instrument,
                side=req.side,
                qty=qty,
                price=price,
                commission=round(fee, 4),
                ts=ts,
            )
        )

        done = self._remaining(order) <= EPS
        order.status = OrderStatus.FILLED if done else OrderStatus.PARTIALLY_FILLED
        parent = order.parent
        if parent is not None and done:  # bracket exit complete: OCA-cancel the rest
            for sibling in parent.children:
                if sibling is not order and sibling.working:
                    self._finish(sibling, OrderStatus.CANCELLED, "OCA: other bracket order filled")
            if parent.working:
                self._finish(
                    parent, OrderStatus.CANCELLED, "bracket exit filled; rest of entry cancelled"
                )
        if req.side == Side.SELL and pos.qty <= EPS:
            del self._positions[key]
            for other in self._working(key):
                if other.request.side == Side.SELL:
                    self._finish(other, OrderStatus.CANCELLED, "position closed")

    def _finish(self, order: _Order, status: OrderStatus, message: str) -> None:
        order.status = status
        order.message = message
        order.updated_at = self._last_now

    def _record(self, order: _Order) -> OrderRecord:
        req = order.request
        if order.parent is not None:  # children show the qty they currently protect
            qty = order.filled + self._remaining(order)
            req = req.model_copy(update={"qty": qty if qty > EPS else order.parent.request.qty})
        return OrderRecord(
            request=req,
            status=order.status,
            broker_order_id=order.id,
            filled_qty=order.filled,
            avg_fill_price=round(order.notional / order.filled, 6) if order.filled > 0 else None,
            commission=round(order.commission, 4),
            message=order.message,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )
