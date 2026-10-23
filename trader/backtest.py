"""Backtester for the rule-based strategy. Its job is honesty: commissions and slippage
are charged on every fill and reported on their own lines, and the live risk caps apply.

Rule: while flat, take the best Scanner candidate scoring >= the threshold. A signal is
only known when its bar closes, so the entry fills at the NEXT bar's open plus slippage.
Size with fixed fractional risk of the day's starting equity. Exit at the candidate's
stop (market: minus slippage, at the open if the bar gapped through) or its target
(limit: no slippage, only when the high trades through it); when one bar touches both,
the stop is assumed to fill first. Stocks use regular-hours bars only and are flat
before the close (the bar ending after 15:50 ET exits at its open); crypto runs 24h.

Mirrored from the live RiskManager so the numbers reflect what the bot could actually
do: max position %, max order notional, cash (no borrowing), round-trip fees vs reward,
minimum reward:risk after slippage, trades per day, the daily loss limit (halt, and
flatten when configured), stop-at-target, and half size after a losing streak. Size
never depends on being behind the daily target. Commissions come from `trader.fees`
(IBKR Pro Tiered by default, or Fixed; sells include regulatory fees) and are reported
per trade and as a share of the gross (pre-commission) profit.

Data: a CSV (columns ts,open,high,low,close,volume; ISO timestamps; one instrument) or
SimBroker synthetic history. Synthetic data is a random walk: it exercises mechanics
and cost drag, not whether the strategy has an edge.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trader import fees
from trader.broker.base import BrokerError
from trader.broker.sim import SimBroker
from trader.fees import Plan
from trader.models import (
    HARD_CAPS,
    AssetClass,
    Bar,
    BotSettings,
    Candidate,
    Instrument,
    Side,
    utcnow,
)
from trader.strategy.signals import Scanner

NY = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
FLATTEN_BEFORE_CLOSE = timedelta(minutes=10)
SMALL_SAMPLE = 30  # fewer trades than this cannot separate skill from luck


@dataclass
class BacktestConfig:
    starting_equity: float = 500.0
    risk_pct: float = 2.0  # of the day's starting equity, lost if the stop is hit
    threshold: float = 0.5  # minimum candidate score to take a trade
    slippage_bps: float = 5.0  # per market-type fill (entries, stops, flattening)
    target_usd: float = 100.0  # daily target for the report; never affects sizing
    allow_fractional: bool = False  # fractional stock shares
    lookback_sessions: int = 2  # history the scanner sees, like the engine's "2 D" request
    commission_plan: Plan = "tiered"  # IBKR Pro pricing plan for stock commissions


@dataclass(frozen=True)
class Trade:
    instrument: Instrument
    setup: str
    entry_ts: datetime
    exit_ts: datetime
    qty: float
    entry_price: float  # fill price, slippage included
    exit_price: float
    stop: float
    target: float
    commission: float  # both fills
    slippage: float  # USD lost to slippage on both fills (already in the fill prices)
    exit_reason: str

    @property
    def gross_pnl(self) -> float:
        """Fill-to-fill P&L before commissions (slippage is already in the fill prices)."""
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def pnl(self) -> float:
        """Net P&L: fill-to-fill minus commissions."""
        return self.gross_pnl - self.commission

    @property
    def r_multiple(self) -> float:
        risk = (self.entry_price - self.stop) * self.qty
        return self.pnl / risk if risk > 0 else 0.0


@dataclass
class BacktestResult:
    config: BacktestConfig
    settings: BotSettings
    instruments: list[Instrument]
    trades: list[Trade]
    days: list[date]  # New York trading days covered by the data
    daily_pnl: dict[date, float]  # net realized P&L by exit day
    equity_curve: list[tuple[datetime, float]]  # mark-to-market after each timestamp
    skipped: dict[str, int]  # why qualifying signals were not traded
    halted_days: dict[str, int]  # days that stopped early, by reason
    bar_interval: timedelta | None

    def stats(self) -> dict[str, float | int | None]:
        trades = self.trades
        pnls = [t.pnl for t in trades]
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p <= 0)
        commissions = sum(t.commission for t in trades)
        slippage = sum(t.slippage for t in trades)
        net = sum(pnls)
        gross = net + commissions
        peak = self.config.starting_equity
        max_dd = max_dd_pct = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak - equity > max_dd:
                max_dd, max_dd_pct = peak - equity, (peak - equity) / peak * 100
        day_pnls = [self.daily_pnl.get(d, 0.0) for d in self.days]
        start = self.config.starting_equity
        return {
            "trades": len(trades),
            "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p <= 0),
            "win_rate_pct": sum(1 for p in pnls if p > 0) / len(trades) * 100 if trades else None,
            "avg_r": statistics.fmean(t.r_multiple for t in trades) if trades else None,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
            "pre_cost_pnl": gross + slippage,
            "gross_pnl": gross,
            "fees": commissions,
            "fees_per_trade": commissions / len(trades) if trades else None,
            "fees_pct_of_gross_profit": commissions / gross * 100 if gross > 0 else None,
            "slippage": slippage,
            "total_pnl": net,
            "starting_equity": start,
            "ending_equity": start + net,
            "return_pct": net / start * 100 if start > 0 else None,
            "max_drawdown": max_dd,
            "max_drawdown_pct": max_dd_pct,
            "trading_days": len(self.days),
            "avg_pnl_per_day": net / len(self.days) if self.days else None,
            "target_usd": self.config.target_usd,
            "days_hitting_target": sum(1 for p in day_pnls if p >= self.config.target_usd),
            "best_day": max(day_pnls) if day_pnls else None,
            "worst_day": min(day_pnls) if day_pnls else None,
        }


# --------------------------------------------------------------------------- data
def load_csv(path: Path, tz: str | tzinfo = "UTC") -> list[Bar]:
    """Bars from a CSV with columns ts,open,high,low,close[,volume] (header names are
    case-insensitive). Timestamps are ISO-8601; naive ones are read in `tz`."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    bars: dict[datetime, Bar] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        names = {name.strip().lower(): name for name in reader.fieldnames or []}
        missing = {"ts", "open", "high", "low", "close"} - names.keys()
        if missing:
            raise ValueError(f"{path}: missing CSV columns {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            try:
                ts = datetime.fromisoformat(row[names["ts"]].strip())
                o, h, low, c = (float(row[names[k]]) for k in ("open", "high", "low", "close"))
                raw_volume = (row.get(names["volume"]) or "").strip() if "volume" in names else ""
                volume = float(raw_volume) if raw_volume else 0.0
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
            if min(o, h, low, c) <= 0 or h < max(o, c, low) or low > min(o, c) or volume < 0:
                raise ValueError(f"{path}:{line}: inconsistent OHLCV values")
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=zone)
            ts = ts.astimezone(UTC)
            bars[ts] = Bar(ts=ts, open=o, high=h, low=low, close=c, volume=volume)
    return [bars[ts] for ts in sorted(bars)]


def synthetic_history(
    instruments: Sequence[Instrument], days: int, bar_size: str, seed: int, end: datetime
) -> list[tuple[Instrument, list[Bar]]]:
    """SimBroker bars ending at `end`: `days` sessions for stocks, days*24h for crypto."""

    async def load() -> list[tuple[Instrument, list[Bar]]]:
        broker = SimBroker(
            seed=seed, starting_cash=0.0, clock=lambda: end, max_history_days=days * 7 // 5 + 5
        )
        await broker.connect()
        return [(inst, await broker.bars(inst, bar_size, f"{days} D")) for inst in instruments]

    return asyncio.run(load())


def last_session_close(now: datetime) -> datetime:
    """16:00 New York of the most recent weekday whose session has closed."""
    local = now.astimezone(NY)
    day = local.date() if local.time() >= SESSION_CLOSE else local.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, SESSION_CLOSE, NY)


# --------------------------------------------------------------------------- simulation
def _ny_day(ts: datetime) -> date:
    return ts.astimezone(NY).date()


@dataclass
class _Feed:
    instrument: Instrument
    bars: list[Bar]
    interval: timedelta = timedelta(minutes=5)
    index: dict[datetime, int] = field(default_factory=dict)
    times: list[datetime] = field(default_factory=list)
    session_starts: list[int] = field(default_factory=list)  # stocks: first bar of each day

    def __post_init__(self) -> None:
        gaps = [(b.ts - a.ts) for a, b in pairwise(self.bars)]
        self.interval = statistics.median(gaps) if gaps else timedelta(minutes=5)
        self.times = [b.ts for b in self.bars]
        self.index = {ts: i for i, ts in enumerate(self.times)}
        self.session_starts = [
            i
            for i, b in enumerate(self.bars)
            if i == 0 or _ny_day(b.ts) != _ny_day(self.bars[i - 1].ts)
        ]

    @property
    def is_stock(self) -> bool:
        return self.instrument.asset_class == AssetClass.STOCK

    def window(self, i: int, sessions: int) -> list[Bar]:
        """Bars the live engine would have at bar i: the last `sessions` sessions (stocks)
        or sessions*24h (crypto)."""
        if self.is_stock:
            s = bisect.bisect_right(self.session_starts, i) - 1
            start = self.session_starts[max(0, s - sessions + 1)]
        else:
            cutoff = self.bars[i].ts - timedelta(days=sessions)
            start = bisect.bisect_right(self.times, cutoff, hi=i)
        return self.bars[start : i + 1]

    def same_session_next(self, i: int) -> bool:
        """True if another bar of the same New York day follows bar i."""
        return i + 1 < len(self.bars) and _ny_day(self.bars[i + 1].ts) == _ny_day(self.bars[i].ts)


@dataclass
class _Open:
    feed: _Feed
    setup: str
    qty: float
    entry_ts: datetime
    entry_price: float
    entry_ref: float  # price before slippage
    stop: float
    target: float
    entry_fee: float


class _Backtest:
    def __init__(
        self,
        data: Sequence[tuple[Instrument, Sequence[Bar]]],
        settings: BotSettings,
        config: BacktestConfig,
    ):
        self.settings, self.config, self.limits = settings, config, settings.risk
        self.scanner = Scanner(settings)
        self.slip = config.slippage_bps / 1e4
        self.feeds: list[_Feed] = []
        for inst, bars in data:
            clean = sorted({b.ts: b for b in bars}.values(), key=lambda b: b.ts)
            if inst.asset_class == AssetClass.STOCK:
                clean = [b for b in clean if self._regular_hours(b.ts)]
            if clean:
                self.feeds.append(_Feed(inst, clean))
        self.cash = config.starting_equity
        self.position: _Open | None = None
        self.pending: tuple[_Feed, Candidate] | None = None
        self.last_close: dict[str, float] = {}
        self.trades: list[Trade] = []
        self.daily_pnl: dict[date, float] = {}
        self.equity_curve: list[tuple[datetime, float]] = []
        self.skipped: Counter[str] = Counter()
        self.halted_days: Counter[str] = Counter()
        self.day: date | None = None
        self.day_start_equity = config.starting_equity
        self.trades_today = 0
        self.halted: str | None = None
        self.loss_streak = 0

    @staticmethod
    def _regular_hours(ts: datetime) -> bool:
        local = ts.astimezone(NY)
        return local.weekday() < 5 and SESSION_OPEN <= local.time() < SESSION_CLOSE

    def _equity(self) -> float:
        pos = self.position
        if pos is None:
            return self.cash
        return self.cash + pos.qty * self.last_close.get(pos.feed.instrument.key, pos.entry_price)

    def run(self) -> BacktestResult:
        timeline = sorted({b.ts for feed in self.feeds for b in feed.bars})
        for ts in timeline:
            day = _ny_day(ts)
            if day != self.day:
                self.day, self.day_start_equity = day, self._equity()
                self.trades_today, self.halted = 0, None
            for feed in self.feeds:
                i = feed.index.get(ts)
                if i is None:
                    continue
                if self.pending is not None and self.pending[0] is feed:
                    self._enter(feed, i)
                if self.position is not None and self.position.feed is feed:
                    self._manage(feed, i)
                self.last_close[feed.instrument.key] = feed.bars[i].close
            self._mark(ts)
            if self.position is None and self.pending is None and self.halted is None:
                self._look_for_entry(ts)
        if self.position is not None:
            feed = self.position.feed
            last = feed.bars[-1]
            self._exit(last.ts + feed.interval, last.close, market=True, reason="end of data")
            self.equity_curve.append((last.ts + feed.interval, self._equity()))
        return BacktestResult(
            config=self.config,
            settings=self.settings,
            instruments=[f.instrument for f in self.feeds],
            trades=self.trades,
            days=sorted({_ny_day(ts) for ts in timeline}),
            daily_pnl=self.daily_pnl,
            equity_curve=self.equity_curve,
            skipped=dict(self.skipped),
            halted_days=dict(self.halted_days),
            bar_interval=min((f.interval for f in self.feeds), default=None),
        )

    # ------------------------------------------------------------------ entries
    def _entry_window_open(self, feed: _Feed, signal_time: datetime) -> bool:
        if not feed.is_stock:
            return True
        start = time.fromisoformat(self.settings.stock_entry_start)
        cutoff = time.fromisoformat(self.settings.stock_entry_cutoff)
        return start <= signal_time.astimezone(NY).time() < cutoff

    def _look_for_entry(self, ts: datetime) -> None:
        best: tuple[_Feed, Candidate] | None = None
        for feed in self.feeds:
            i = feed.index.get(ts)
            if i is None:
                continue
            signal_time = ts + feed.interval  # the bar has just closed
            if feed.is_stock and (
                not self._entry_window_open(feed, signal_time) or not feed.same_session_next(i)
            ):
                continue
            window = feed.window(i, self.config.lookback_sessions)
            candidates = self.scanner.scan(feed.instrument, window, None, signal_time)
            if candidates and candidates[0].score >= self.config.threshold:
                if best is None or candidates[0].score > best[1].score:
                    best = (feed, candidates[0])
        if best is None:
            return
        if self.trades_today >= self.limits.max_trades_per_day:
            self.skipped["max trades per day reached"] += 1
            return
        self.pending = best

    def _size(self, instrument: Instrument, entry: float, stop: float) -> float:
        lim = self.limits
        equity = self._equity()
        risk_usd = (
            self.day_start_equity
            * min(self.config.risk_pct, HARD_CAPS["max_risk_per_trade_pct"])
            / 100
        )
        if self.loss_streak >= min(
            lim.loss_streak_halve_after, HARD_CAPS["loss_streak_halve_after"]
        ):
            risk_usd *= 0.5
        position_pct = min(lim.max_position_pct, HARD_CAPS["max_position_pct"])
        exposure_pct = min(lim.max_total_exposure_pct, HARD_CAPS["max_total_exposure_pct"])
        qty = min(
            risk_usd / (entry - stop),
            equity * position_pct / 100 / entry,
            equity * exposure_pct / 100 / entry,
            lim.max_order_notional_usd / entry,
            self.cash / entry,
        )
        if instrument.asset_class == AssetClass.STOCK and not self.config.allow_fractional:
            step = 1.0
        else:
            step = 1e-4 if instrument.asset_class == AssetClass.STOCK else 1e-6
        qty = math.floor(qty / step + 1e-9) * step
        while qty > 0 and qty * entry + self._fee(instrument, qty, entry, Side.BUY) > (
            self.cash + 1e-9
        ):
            qty = round(qty - step, 6)
        return max(qty, 0.0)

    def _fee(self, instrument: Instrument, qty: float, price: float, side: Side) -> float:
        return fees.estimate_commission(instrument, qty, price, side, self.config.commission_plan)

    def _enter(self, feed: _Feed, i: int) -> None:
        pending, self.pending = self.pending, None
        if pending is None:
            return
        cand = pending[1]
        bar = feed.bars[i]
        ref = bar.open
        entry = ref * (1 + self.slip)
        stop, target = cand.suggested_stop, cand.suggested_target
        if not stop < entry < target:
            self.skipped["next open gapped past stop or target"] += 1
            return
        if (target - entry) / (entry - stop) < self.limits.min_reward_risk:
            self.skipped["reward:risk below minimum after slippage"] += 1
            return
        qty = self._size(feed.instrument, entry, stop)
        if qty <= 0:
            self.skipped["position size rounds to zero"] += 1
            return
        fee = self._fee(feed.instrument, qty, entry, Side.BUY)
        round_trip = fee + self._fee(feed.instrument, qty, target, Side.SELL)
        if round_trip > self.limits.max_fee_to_reward_pct / 100 * (target - entry) * qty:
            self.skipped["round-trip fees too high vs reward"] += 1
            return
        self.cash -= qty * entry + fee
        self.position = _Open(feed, cand.setup, qty, bar.ts, entry, ref, stop, target, fee)
        self.trades_today += 1

    # ------------------------------------------------------------------ exits
    def _manage(self, feed: _Feed, i: int) -> None:
        pos = self.position
        if pos is None:
            return
        bar = feed.bars[i]
        end = bar.ts + feed.interval
        if feed.is_stock:
            close = datetime.combine(_ny_day(bar.ts), SESSION_CLOSE, NY)
            if end > close - FLATTEN_BEFORE_CLOSE and bar.ts > pos.entry_ts:
                self._exit(bar.ts, bar.open, market=True, reason="flatten before close")
                return
        if bar.low <= pos.stop:  # also when the target traded too: assume the stop came first
            both = bar.high > pos.target
            self._exit(
                end,
                min(pos.stop, bar.open),
                market=True,
                reason="stop (bar also hit target)" if both else "stop",
            )
        elif bar.high > pos.target:
            self._exit(end, max(pos.target, bar.open), market=False, reason="target")
        elif feed.is_stock and not feed.same_session_next(i):
            self._exit(end, bar.close, market=True, reason="session end")

    def _exit(self, ts: datetime, ref: float, *, market: bool, reason: str) -> None:
        pos = self.position
        if pos is None:
            return
        price = ref * (1 - self.slip) if market else ref
        fee = self._fee(pos.feed.instrument, pos.qty, price, Side.SELL)
        self.cash += pos.qty * price - fee
        trade = Trade(
            instrument=pos.feed.instrument,
            setup=pos.setup,
            entry_ts=pos.entry_ts,
            exit_ts=ts,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=price,
            stop=pos.stop,
            target=pos.target,
            commission=pos.entry_fee + fee,
            slippage=(pos.entry_price - pos.entry_ref + ref - price) * pos.qty,
            exit_reason=reason,
        )
        self.trades.append(trade)
        day = _ny_day(ts)
        self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + trade.pnl
        self.loss_streak = self.loss_streak + 1 if trade.pnl <= 0 else 0
        self.position = None

    def _mark(self, ts: datetime) -> None:
        """Apply the daily halts (like RiskManager.halt_reason), then record mark-to-market
        equity."""
        if self.halted is None:
            self.halted = self._halt_reason(self._equity())
            if self.halted is not None:
                self.halted_days[self.halted] += 1
                self.pending = None
                pos = self.position
                if self.halted == "daily loss limit" and self.limits.flatten_on_halt and pos:
                    price = self.last_close[pos.feed.instrument.key]
                    self._exit(ts, price, market=True, reason="daily loss limit")
        self.equity_curve.append((ts, self._equity()))

    def _halt_reason(self, equity: float) -> str | None:
        lim = self.limits
        loss_pct = min(lim.max_daily_loss_pct, HARD_CAPS["max_daily_loss_pct"])
        if equity <= self.day_start_equity * (1 - loss_pct / 100):
            return "daily loss limit"
        gain = equity - self.day_start_equity
        target = lim.profit_target_usd(self.day_start_equity)
        if lim.stop_trading_at_target and target > 0 and gain >= target:
            return "daily target reached"  # soft: open positions keep their exits
        return None


def run_backtest(
    data: Sequence[tuple[Instrument, Sequence[Bar]]],
    settings: BotSettings | None = None,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Replay the scanner + rule over the bars (see the module docstring)."""
    return _Backtest(data, settings or BotSettings(), config or BacktestConfig()).run()


# --------------------------------------------------------------------------- report
def _usd(value: float, signed: bool = True) -> str:
    sign = ("+" if value >= 0 else "-") if signed else ("-" if value < 0 else "")
    return f"{sign}${abs(value):,.2f}"


def _share_of(cost: float, gross: float) -> str:
    """Cost relative to a positive gross profit: "16% of" or, beyond it, "8.5x"."""
    ratio = cost / gross
    return f"{ratio:.0%} of" if ratio <= 1 else f"{ratio:,.1f}x"


def format_report(
    result: BacktestResult, source: str, *, synthetic: bool, show_trades: bool = False
) -> str:
    """Human-readable report; costs get their own lines."""
    s = result.stats()
    cfg, lim = result.config, result.settings.risk
    trades = int(s["trades"] or 0)
    interval = result.bar_interval
    bars = f"{int(interval.total_seconds() // 60)}-minute bars" if interval else "no bars"
    names = ", ".join(i.symbol for i in result.instruments) or "none"
    out = ["AI Trader backtest: rule-based strategy", ""]
    out.append(f"Data       {source}; {bars}; {names}")
    if result.days:
        span = f"{result.days[0]} .. {result.days[-1]}"
        out.append(f"Period     {span} ({len(result.days)} trading days)")
    out.append(
        f"Rules      score >= {cfg.threshold:.2f}; risk {cfg.risk_pct:g}% of day-start equity; "
        "entry at the next bar's open"
    )
    out.append(
        f"           max order {_usd(lim.max_order_notional_usd, False)}; max position "
        f"{lim.max_position_pct:g}% of equity; daily loss limit {lim.max_daily_loss_pct:g}%; "
        f"{lim.max_trades_per_day} trades/day"
    )
    out.append(
        f"           slippage {cfg.slippage_bps:g} bps per market fill; IBKR Pro "
        f"{cfg.commission_plan.title()} fees; stop first if a bar hits both; stocks flat by "
        "15:50 ET"
    )

    out += ["", "Results"]
    if trades:
        out.append(
            f"  Trades             {trades} (won {s['wins']}, lost {s['losses']}: "
            f"win rate {s['win_rate_pct']:.1f}%)"
        )
        out.append(f"  Average R          {s['avg_r']:+.2f}R")
        pf = s["profit_factor"]
        out.append(
            f"  Profit factor      {pf:.2f}" if pf is not None else "  Profit factor      n/a"
        )
    else:
        out.append("  Trades             0 (no signal passed the threshold and the risk caps)")
    out.append(f"  P&L before costs   {_usd(s['pre_cost_pnl'])}")
    out.append(f"  Commissions        {_usd(-s['fees'])}")
    if trades:
        gross = s["gross_pnl"]
        out.append(
            f"    per trade        {_usd(s['fees_per_trade'], False)} round trip; "
            + (
                f"{_share_of(s['fees'], gross)} the {_usd(gross)} gross profit (before commissions)"
                if gross > 0
                else f"no gross profit to pay them from ({_usd(gross)} before commissions)"
            )
        )
    out.append(f"  Slippage           {_usd(-s['slippage'])}")
    out.append(
        f"  Net P&L            {_usd(s['total_pnl'])} ({s['return_pct']:+.2f}%: "
        f"{_usd(s['starting_equity'], False)} -> {_usd(s['ending_equity'], False)})"
    )
    out.append(f"  Max drawdown       {_usd(-s['max_drawdown'])} ({-s['max_drawdown_pct']:.2f}%)")
    if s["avg_pnl_per_day"] is not None:
        out.append(f"  Average per day    {_usd(s['avg_pnl_per_day'])}")
        out.append(f"  Best / worst day   {_usd(s['best_day'])} / {_usd(s['worst_day'])}")
    target = _usd(cfg.target_usd, False)
    out.append(f"  Days >= {target:<10} {s['days_hitting_target']} of {s['trading_days']}")

    out += ["", "Cost check"]
    costs = s["fees"] + s["slippage"]
    pre, net = s["pre_cost_pnl"], s["total_pnl"]
    if not trades:
        out.append("  No trades, no costs.")
    elif pre > 0 and net <= 0:
        out.append(
            f"  Costs ({_usd(costs, False)}) turned a pre-cost profit of {_usd(pre, False)} "
            f"into a net loss of {_usd(-net, False)}."
        )
    elif pre > 0:
        out.append(f"  Costs ({_usd(costs, False)}) took {costs / pre:.0%} of the pre-cost profit.")
    else:
        out.append(
            f"  The strategy lost {_usd(-pre, False)} before costs; "
            f"costs added {_usd(costs, False)}."
        )
    if trades:
        notional = statistics.fmean(t.entry_price * t.qty for t in result.trades)
        per_trade = costs / trades
        out.append(
            f"  Average round trip: {_usd(per_trade, False)} of costs on "
            f"{_usd(notional, False)} notional = {per_trade / notional * 1e4:.0f} bps"
        )
        out.append("  that the price must move just to break even.")
    if result.skipped:
        out += ["", "Signals not taken"]
        for reason, count in sorted(result.skipped.items(), key=lambda kv: -kv[1]):
            out.append(f"  {reason}: {count}")
    if result.halted_days:
        out += ["", "Days stopped early"]
        out += [f"  {reason}: {count}" for reason, count in sorted(result.halted_days.items())]

    out += ["", "Notes"]
    if trades < SMALL_SAMPLE:
        out.append(f"  ! Only {trades} trades: far too few to tell skill from luck.")
    if cfg.starting_equity > 0:
        pct = cfg.target_usd / cfg.starting_equity * 100
        out.append(
            f"  ! {target}/day on {_usd(cfg.starting_equity, False)} is {pct:+.1f}% per day; "
            f"reached on {s['days_hitting_target']} of {s['trading_days']} days here."
        )
        out.append("    Never size up to chase it.")
    if synthetic:
        out.append(
            "  ! Synthetic data is a random walk with no edge to find: this measures the "
            "mechanics and the cost drag, not profitability."
        )
    out.append("  ! Past or simulated results do not predict live results.")

    if show_trades and result.trades:
        out += ["", "Trades"]
        for t in result.trades:
            when = t.entry_ts.astimezone(NY).strftime("%Y-%m-%d %H:%M")
            share = f" ({_share_of(t.commission, t.gross_pnl)} gross)" if t.gross_pnl > 0 else ""
            out.append(
                f"  {when} {t.instrument.key:<12} {t.setup:<15} qty {t.qty:g} "
                f"{t.entry_price:.6g} -> {t.exit_price:.6g} ({t.exit_reason}) {_usd(t.pnl)} "
                f"{t.r_multiple:+.2f}R fees {_usd(t.commission, False)}{share} "
                f"slip {_usd(t.slippage, False)}"
            )
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI
def _positive(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _non_negative(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _unit(text: str) -> float:
    value = float(text)
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _days(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 120:
        raise argparse.ArgumentTypeError("must be between 1 and 120")
    return value


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m trader backtest",
        description="Backtest the rule-based strategy with commissions, slippage and the "
        "live risk caps. Uses SimBroker synthetic history unless --csv is given.",
    )
    p.add_argument(
        "--csv", type=Path, help="CSV with ts,open,high,low,close,volume (one instrument)"
    )
    p.add_argument("--symbol", help="symbol for --csv (default: the file name)")
    p.add_argument(
        "--csv-tz", default="UTC", help="time zone of naive CSV timestamps (default UTC)"
    )
    p.add_argument("--crypto", action="store_true", help="the instruments are crypto (24h)")
    p.add_argument(
        "--symbols", help="comma-separated symbols for synthetic data (default: universe)"
    )
    p.add_argument("--days", type=_days, default=20, help="synthetic sessions/days (default 20)")
    p.add_argument("--bar-size", default="5 mins", choices=["1 min", "5 mins", "15 mins"])
    p.add_argument("--seed", type=int, default=7, help="synthetic market seed (default 7)")
    p.add_argument("--end", type=date.fromisoformat, help="last synthetic day YYYY-MM-DD")
    p.add_argument("--settings", type=Path, help="BotSettings JSON (default: built-in defaults)")
    p.add_argument("--equity", type=_positive, default=500.0, help="starting equity (default 500)")
    p.add_argument("--risk-pct", type=_positive, default=2.0, help="risk per trade %% (default 2)")
    p.add_argument("--threshold", type=_unit, help="minimum candidate score (default: settings)")
    p.add_argument(
        "--slippage-bps", type=_non_negative, default=5.0, help="per market fill (default 5)"
    )
    p.add_argument(
        "--target",
        type=_non_negative,
        default=None,
        help="fixed daily profit target in USD (default: the settings' percent of start equity)",
    )
    p.add_argument(
        "--max-notional", type=_positive, help="max order notional USD (default: settings)"
    )
    p.add_argument("--fractional", action="store_true", help="allow fractional stock shares")
    p.add_argument(
        "--plan",
        choices=["tiered", "fixed"],
        default="tiered",
        help="IBKR Pro commission plan (default tiered)",
    )
    p.add_argument("--trades", action="store_true", help="list every trade")
    p.add_argument("--json", action="store_true", help="print the statistics as JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry: `python -m trader backtest ...`. Returns a process exit code."""
    args = _parser().parse_args(argv)
    try:
        settings = (
            BotSettings.model_validate_json(args.settings.read_text(encoding="utf-8"))
            if args.settings
            else BotSettings()
        )
        risk_update: dict[str, float | None] = {}
        if args.target is not None:
            # Explicit dollar target: the percent target would otherwise win.
            risk_update["daily_profit_target_usd"] = args.target
            risk_update["daily_profit_target_pct"] = None
        if args.max_notional:
            risk_update["max_order_notional_usd"] = args.max_notional
        if risk_update:
            settings = settings.model_copy(
                update={"risk": settings.risk.model_copy(update=risk_update)}
            )
        asset = AssetClass.CRYPTO if args.crypto else AssetClass.STOCK
        if args.csv:
            inst = Instrument(symbol=(args.symbol or args.csv.stem).upper(), asset_class=asset)
            data = [(inst, load_csv(args.csv, args.csv_tz))]
            source = f"CSV {args.csv}"
        else:
            default = settings.crypto_universe if args.crypto else settings.stock_universe
            symbols = [s.strip().upper() for s in (args.symbols or ",".join(default)).split(",")]
            end = (
                datetime.combine(args.end, SESSION_CLOSE, NY)
                if args.end
                else last_session_close(utcnow())
            )
            insts = [Instrument(symbol=s, asset_class=asset) for s in symbols if s]
            data = synthetic_history(insts, args.days, args.bar_size, args.seed, end)
            source = f"synthetic SimBroker history (seed {args.seed})"
    except (OSError, ValueError, ZoneInfoNotFoundError, BrokerError) as exc:
        print(f"backtest: error: {exc}", file=sys.stderr)
        return 2
    config = BacktestConfig(
        starting_equity=args.equity,
        risk_pct=args.risk_pct,
        threshold=settings.llm_min_candidate_score if args.threshold is None else args.threshold,
        slippage_bps=args.slippage_bps,
        target_usd=100.0 if args.target is None else args.target,
        allow_fractional=args.fractional,
        commission_plan=args.plan,
    )
    result = run_backtest(data, settings, config)
    if args.json:
        payload = {
            "stats": result.stats(),
            "skipped": result.skipped,
            "halted_days": result.halted_days,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_report(result, source, synthetic=not args.csv, show_trades=args.trades))
    return 0
