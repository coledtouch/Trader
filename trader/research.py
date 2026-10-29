"""Offline research using the production engine, risk manager and decision makers.

Each comparison has an isolated, in-memory account and journal. No real broker or model
client is constructed. Captured model opinions are reused, never generated retrospectively:
this is a *selection replay*, not a simulation of what an AI would have said to a different
portfolio. Missing opinions and unknown costs stay visible in the result.

Orders placed from a snapshot cannot fill until the next observed snapshot. Market fills
pay bid/ask and configurable slippage; limits can remain unfilled. Native stock stops and
targets are modeled by the broker; time, breakeven, trailing, halts and closing-time exits
run through the exact production Engine. Sparse decision-cycle captures consequently miss
intra-cycle price paths and are explicitly labeled as such.

Run ``python -m trader.research --frames captures.jsonl`` or ``--database data/trader.db``.
For an honest, bar-close-only rules test, pass ``--csv F=history.csv`` (repeatable). CSV
timestamps are bar *open* times, UTC unless timezone-qualified, and the bar is revealed
only at its close. The inferred quotes are labeled proxies, not executable observations.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from trader.broker.base import Broker, BrokerError
from trader.config import AppConfig
from trader.db import Database
from trader.engine import Engine
from trader.fees import Plan, estimate_commission
from trader.models import (
    OPEN_ORDER_STATUSES,
    AccountSnapshot,
    AssetClass,
    Bar,
    BotSettings,
    Candidate,
    DecisionBatch,
    DecisionContext,
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
    TradeAction,
    TradeRecord,
)
from trader.risk import quote_problem
from trader.strategy.common import is_failure
from trader.strategy.consensus import ConsensusDecisionMaker
from trader.strategy.decide import RuleBasedDecisionMaker
from trader.strategy.signals import Scanner
from trader.timeutil import stock_market_open, trading_day

ARMS = ("rules", "claude", "grok", "consensus")
BAR_INTERVALS = {
    "1 min": timedelta(minutes=1),
    "5 mins": timedelta(minutes=5),
    "15 mins": timedelta(minutes=15),
}
EPS = 1e-9


class ReplayFrame(BaseModel):
    """A point-in-time observation; maps use Instrument.key, for example STK:F.

    ``candidates=None`` means rescan historical bars with the production Scanner; an
    empty list means the captured scanner found no candidates. ``batch`` accepts the
    engine's recorded DecisionBatch including per-maker vote_costs/vote_failures.
    Explicit ``votes`` accepts full original model batches instead.
    """

    ts: datetime
    quotes: dict[str, Quote] = Field(default_factory=dict)
    # When each quote arrived in production. A round's quotes arrive over its fetch, so
    # each is judged as old as it was on arrival, not as old as at the round's last one.
    quoted_at: dict[str, datetime] = Field(default_factory=dict)
    bars: dict[str, list[Bar]] = Field(default_factory=dict)
    candidates: list[Candidate] | None = None
    batch: DecisionBatch | None = None
    votes: dict[str, DecisionBatch] = Field(default_factory=dict)
    # Explicitly reconstructed votes without original per-model billing must use None.
    vote_costs: dict[str, float | None] = Field(default_factory=dict)
    market: dict[str, dict[str, float]] = Field(default_factory=dict)
    decision_cycle: bool = True
    source: Literal["captured", "historical_bars"] = "captured"
    capture_format: Literal["scanner_snapshot_v1"] | None = None
    settings: BotSettings | None = None
    account_id: str | None = None
    account_mode: Mode | None = None
    starting_equity: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    commission_plan: Plan | None = None
    cohort: str | None = None

    @field_validator("ts")
    @classmethod
    def _aware_ts(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("replay timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _keys_and_costs(self) -> ReplayFrame:
        for key, quote in self.quotes.items():
            if key != quote.instrument.key:
                raise ValueError(f"quote key {key!r} differs from its instrument")
            if quote.ts.tzinfo is None:
                raise ValueError("quote timestamps must include a timezone")
        if any(t.tzinfo is None or t > self.ts for t in self.quoted_at.values()):
            raise ValueError("quote arrival times must include a timezone, at or before frame")
        for key, bars in self.bars.items():
            Instrument.from_key(key)
            if any(bar.ts.tzinfo is None for bar in bars):
                raise ValueError("bar timestamps must include a timezone")
            if any(a.ts >= b.ts for a, b in zip(bars, bars[1:], strict=False)):
                raise ValueError("frame bars must have unique, increasing timestamps")
        if any(c is not None and (not math.isfinite(c) or c < 0) for c in self.vote_costs.values()):
            raise ValueError("captured model costs must be finite and nonnegative")
        batches = [*self.votes.values(), *([self.batch] if self.batch else [])]
        costs = [c for batch in batches for c in [batch.cost_usd, *batch.vote_costs.values()]]
        if any(not math.isfinite(c) or c < 0 for c in costs):
            raise ValueError("captured model costs must be finite and nonnegative")
        if any(batch.ts.tzinfo is None or batch.ts > self.ts for batch in batches):
            raise ValueError(
                "captured votes must have timezone-aware timestamps at or before frame"
            )
        return self

    def replay_quotes(self) -> dict[str, Quote]:
        """Quotes as the frame's clock sees them: each as old as it was when it arrived."""
        quotes = {}
        for key, quote in self.quotes.items():
            arrived = self.quoted_at.get(key)
            later = self.ts - arrived if arrived is not None else timedelta(0)
            quotes[key] = quote.model_copy(update={"ts": quote.ts + later}) if later else quote
        return quotes


class EquityObservation(BaseModel):
    ts: datetime
    trading_equity: float
    all_in_equity: float | None


class ReplayArmResult(BaseModel):
    arm: str
    starting_equity: float
    ending_equity: float
    trading_pnl_usd: float
    realized_pnl_usd: float
    commissions_usd: float
    model_cost_usd: float | None
    known_model_cost_usd: float
    all_in_pnl_usd: float | None
    max_drawdown_usd: float
    max_drawdown_pct: float
    drawdown_basis: Literal["all_in", "trading_only"]
    missing_vote_cycles: int
    observed_vote_cycles: int
    model_calls_replayed: int
    unfilled_entries: int
    invalid_quote_frames: int
    trades: list[TradeRecord]
    fills: list[Fill]
    orders: list[OrderRecord]
    open_positions: list[Position]
    equity_curve: list[EquityObservation]
    warnings: list[str] = Field(default_factory=list)


class ReplayExperiment(BaseModel):
    schema_version: int = 1
    status: Literal["selection_replay"] = "selection_replay"
    starting_equity: float
    frame_count: int
    decision_cycles: int
    source_types: list[str]
    started_at: datetime
    ended_at: datetime
    settings: BotSettings
    commission_plan: Plan
    slippage_bps: float
    arms: dict[str, ReplayArmResult]
    assumptions: list[str]
    warnings: list[str]


class _Clock:
    def __init__(self, ts: datetime):
        self.ts = ts

    def __call__(self) -> datetime:
        return self.ts


def _captured_vote(frame: ReplayFrame, name: str) -> tuple[DecisionBatch | None, float | None]:
    """Return only evidence present in the capture, including failures and exact billing."""
    if name in frame.votes:
        batch = frame.votes[name].model_copy(deep=True)
        return batch, frame.vote_costs.get(name, batch.cost_usd)
    batch = frame.batch
    if batch is None:
        return None, None
    if batch.maker == name:
        return batch.model_copy(deep=True), frame.vote_costs.get(name, batch.cost_usd)
    if name not in batch.votes:
        return None, None
    # Older consensus captures contain votes but no per-model bill or error metadata.
    # Never split the total bill equally or silently turn a failed opinion into a BUY.
    costs = getattr(batch, "vote_costs", {})
    failures = getattr(batch, "vote_failures", {})
    cost = frame.vote_costs.get(name, costs.get(name))
    failure = failures.get(name)
    # A capture with per-model bills lists every failed side in vote_failures; its merged
    # error may only say a side dropped an invalid decision, which is no failure.
    if (batch.error or batch.refused) and not failures and not (costs or frame.vote_costs):
        failure = "original consensus failed; individual failure attribution unavailable"
    return DecisionBatch(
        ts=frame.ts,
        maker=name,
        decisions=batch.votes[name],
        cost_usd=cost or 0.0,
        error=failure,
        candidates=frame.candidates or [],
    ), cost


class _CapturedMaker:
    """An offline reader: missing original votes produce no decisions or paid requests."""

    def __init__(self, name: str, broker: _ReplayBroker):
        self.name = name
        self.broker = broker
        self.calls = 0
        self.known_cost = 0.0
        self.unknown_cost = False
        self.missing: set[datetime] = set()
        self.observed: set[datetime] = set()

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        assert self.broker.frame is not None
        batch, cost = _captured_vote(self.broker.frame, self.name)
        if batch is None:
            self.missing.add(ctx.now)
            return DecisionBatch(
                ts=ctx.now,
                maker=self.name,
                error="missing captured vote; no inferred decision",
                candidates=ctx.candidates,
            )
        self.calls += 1
        self.observed.add(ctx.now)
        if cost is None:
            self.unknown_cost = True
        else:
            self.known_cost += cost
        if is_failure(batch):
            batch.decisions = [d for d in batch.decisions if d.action != TradeAction.BUY]
        # A recorded opinion can select only scanner candidates visible in this frame.
        # Engine revalidates it against this arm's actual cash, positions and fresh quote.
        return batch.model_copy(
            update={
                "id": None,
                "ts": ctx.now,
                "maker": self.name,
                "candidates": ctx.candidates,
                "cost_usd": cost or 0.0,
            }
        )


class _ReplayScanner:
    def __init__(self, broker: _ReplayBroker, settings: BotSettings):
        self.broker, self.settings = broker, settings
        self.scanner = Scanner(settings)

    def scan(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None, now: datetime
    ) -> list[Candidate]:
        assert self.broker.frame is not None
        candidates = self.broker.frame.candidates
        if candidates is None:
            return self.scanner.scan(instrument, bars, quote, now)
        return [c.model_copy(deep=True) for c in candidates if c.instrument.key == instrument.key]

    def snapshot(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None = None
    ) -> dict[str, float]:
        assert self.broker.frame is not None
        if instrument.key in self.broker.frame.market:
            return dict(self.broker.frame.market[instrument.key])
        return self.scanner.snapshot(instrument, bars, quote)


class _ReplayBroker(Broker):
    """A deterministic cash account using only observations available at its clock.

    No network dependency, synthetic price process, live account or external API exists
    here. Unobserved paths, liquidity/partial fills and queue position are not invented.
    Stock sale proceeds become available on the next observed stock trading day.
    """

    name = "offline_research"

    def __init__(
        self,
        clock: _Clock,
        settings: BotSettings,
        cash: float,
        commission_plan: Plan,
        slippage_bps: float,
    ):
        self.clock, self.settings = clock, settings
        self.cash = cash
        self.plan, self.slippage = commission_plan, slippage_bps / 10_000
        self.frame: ReplayFrame | None = None
        self._quotes: dict[str, Quote] = {}
        self._connected = False
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, OrderRecord] = {}
        self._kinds: dict[str, str] = {}
        self._parents: dict[str, str] = {}
        self._fills: list[Fill] = []
        self._ids = itertools.count(1)
        self._unsettled: list[tuple[str, float]] = []
        self._marks: dict[str, float] = {}
        self.invalid_quote_frames = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> Mode:
        # PAPER keeps the engine's production restart/account behavior and live gate.
        return Mode.PAPER

    @property
    def data_delayed(self) -> bool:
        return bool(self.frame and any(q.delayed for q in self.frame.quotes.values()))

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    def _usable(self, quote: Quote | None) -> bool:
        if quote is None or quote.delayed or quote.halted or quote_problem(quote):
            return False
        if not all(v is not None and math.isfinite(v) and v > 0 for v in (quote.bid, quote.ask)):
            return False
        age = (self.clock() - quote.ts).total_seconds()
        return 0 <= age <= self.settings.monitor_seconds * 2

    def advance(self, frame: ReplayFrame) -> None:
        self.frame = frame
        self._quotes = frame.replay_quotes()
        self.clock.ts = frame.ts
        if stock_market_open(frame.ts):
            today = trading_day(frame.ts)
            self._unsettled = [(d, amount) for d, amount in self._unsettled if d >= today]
        bad = False
        for key, quote in self._quotes.items():
            if self._usable(quote):
                assert quote.bid is not None
                self._marks[key] = quote.bid
            else:
                bad = True
        self.invalid_quote_frames += int(bad)
        # Previously resting protection is processed first. A new entry and its children
        # cannot both fill from the same snapshot because its intra-frame path is unknown.
        for kind in ("stop", "target", "exit", "entry"):
            for oid, record in list(self._orders.items()):
                if self._kinds[oid] != kind or record.status not in OPEN_ORDER_STATUSES:
                    continue
                parent = self._parents.get(oid)
                if parent and self._orders[parent].filled_qty <= 0:
                    continue
                if record.created_at >= frame.ts:
                    continue
                req = record.request
                quote = self._quotes.get(req.instrument.key)
                if not self._usable(quote):
                    continue
                assert quote is not None and quote.bid is not None and quote.ask is not None
                if req.instrument.asset_class == AssetClass.STOCK and not stock_market_open(
                    frame.ts
                ):
                    continue
                if kind == "entry":
                    price = quote.ask * (1 + self.slippage)
                    if req.limit_price is not None and price <= req.limit_price + EPS:
                        self._fill(oid, price)
                elif kind == "stop" and req.stop_loss is not None and quote.bid <= req.stop_loss:
                    self._fill(oid, quote.bid * (1 - self.slippage))
                elif kind in {"target", "exit"} and req.limit_price is not None:
                    # Require a trade-through; touching a limit is no proof of queue fill.
                    if quote.bid > req.limit_price + EPS:
                        self._fill(oid, req.limit_price)

    async def account(self) -> AccountSnapshot:
        positions = await self.positions()
        value = sum(p.qty * (p.market_price or p.avg_cost) for p in positions)
        unsettled = sum(amount for _, amount in self._unsettled)
        settled = max(0.0, self.cash - unsettled)
        return AccountSnapshot(
            account_id="OFFLINE-RESEARCH",
            mode=self.mode,
            equity=self.cash + value,
            cash=self.cash,
            settled_cash=settled,
            buying_power=settled,
            ts=self.clock(),
        )

    async def positions(self) -> list[Position]:
        return [
            p.model_copy(
                update={
                    "market_price": self._marks.get(key, p.avg_cost),
                    "unrealized_pnl": (self._marks.get(key, p.avg_cost) - p.avg_cost) * p.qty,
                }
            )
            for key, p in sorted(self._positions.items())
        ]

    async def quote(self, instrument: Instrument) -> Quote:
        if instrument.key not in self._quotes:
            raise BrokerError(f"no captured quote for {instrument.key}")
        return self._quotes[instrument.key].model_copy(deep=True)

    async def bars(
        self, instrument: Instrument, bar_size: str = "5 mins", lookback: str = "2 D"
    ) -> list[Bar]:
        if self.frame is None:
            return []
        interval = BAR_INTERVALS[bar_size]
        return [
            b.model_copy()
            for b in self.frame.bars.get(instrument.key, [])
            if b.ts + interval <= self.clock()
        ]

    def estimate_commission(
        self, instrument: Instrument, qty: float, price: float, side: Side = Side.BUY
    ) -> float:
        return estimate_commission(instrument, qty, price, side, self.plan)

    def supports_native_stops(self, instrument: Instrument) -> bool:
        return instrument.asset_class == AssetClass.STOCK

    def supports_standalone_stops(self, instrument: Instrument) -> bool:
        return self.supports_native_stops(instrument)

    def _new_order(self, req: OrderRequest, kind: str) -> OrderRecord:
        oid = f"research-{next(self._ids)}"
        rec = OrderRecord(
            request=req.model_copy(deep=True),
            status=OrderStatus.SUBMITTED,
            broker_order_id=oid,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        self._orders[oid], self._kinds[oid] = rec, kind
        return rec

    async def place_order(self, req: OrderRequest) -> OrderRecord:
        quote = self._quotes.get(req.instrument.key)
        if not self._usable(quote):
            return OrderRecord(
                request=req,
                status=OrderStatus.REJECTED,
                created_at=self.clock(),
                updated_at=self.clock(),
                message="no fresh, real-time, two-sided captured quote",
            )
        if req.instrument.asset_class == AssetClass.STOCK and not stock_market_open(self.clock()):
            return OrderRecord(
                request=req,
                status=OrderStatus.REJECTED,
                message="stock market closed",
                created_at=self.clock(),
                updated_at=self.clock(),
            )
        if req.side == Side.BUY:
            rec = self._new_order(req, "entry")
            if self.supports_native_stops(req.instrument):
                for kind, price in (("stop", req.stop_loss), ("target", req.take_profit)):
                    if price is None:
                        continue
                    child = self._new_order(
                        OrderRequest(
                            instrument=req.instrument,
                            side=Side.SELL,
                            qty=req.qty,
                            order_type=OrderType.LIMIT if kind == "target" else OrderType.MARKET,
                            stop_loss=price if kind == "stop" else None,
                            limit_price=price if kind == "target" else None,
                            is_exit=True,
                            setup=req.setup,
                            reason=f"{kind}-loss" if kind == "stop" else "take-profit",
                        ),
                        kind,
                    )
                    assert child.broker_order_id is not None and rec.broker_order_id is not None
                    self._parents[child.broker_order_id] = rec.broker_order_id
            return rec.model_copy(deep=True)
        kind = "stop" if req.stop_loss is not None else "exit"
        rec = self._new_order(req, kind)
        if req.order_type == OrderType.MARKET and req.stop_loss is None:
            assert quote is not None and quote.bid is not None and rec.broker_order_id is not None
            self._fill(rec.broker_order_id, quote.bid * (1 - self.slippage))
        return rec.model_copy(deep=True)

    def _fill(self, oid: str, price: float) -> None:
        rec = self._orders[oid]
        req, key = rec.request, rec.request.instrument.key
        pos = self._positions.get(key)
        qty = req.qty if req.side == Side.BUY else min(req.qty, pos.qty if pos else 0.0)
        if qty <= EPS:
            rec.status = OrderStatus.CANCELLED
            return
        commission = self.estimate_commission(req.instrument, qty, price, req.side)
        if req.side == Side.BUY:
            available = max(0.0, self.cash - sum(amount for _, amount in self._unsettled))
            if qty * price + commission > available + EPS:
                rec.status, rec.message = OrderStatus.REJECTED, "insufficient settled cash at fill"
                self._cancel_children(oid)
                return
            held, avg = (pos.qty, pos.avg_cost) if pos else (0.0, 0.0)
            self._positions[key] = Position(
                instrument=req.instrument,
                qty=held + qty,
                avg_cost=(held * avg + qty * price) / (held + qty),
            )
            self.cash -= qty * price + commission
        else:
            assert pos is not None
            proceeds = qty * price - commission
            self.cash += proceeds
            if req.instrument.asset_class == AssetClass.STOCK:
                self._unsettled.append((trading_day(self.clock()), proceeds))
            if pos.qty - qty <= EPS:
                del self._positions[key]
            else:
                pos.qty -= qty
        rec.status, rec.filled_qty = OrderStatus.FILLED, qty
        rec.avg_fill_price, rec.commission, rec.updated_at = price, commission, self.clock()
        self._fills.append(
            Fill(
                exec_id=f"research-exec-{len(self._fills) + 1}",
                broker_order_id=oid,
                instrument=req.instrument,
                side=req.side,
                qty=qty,
                price=price,
                commission=commission,
                ts=self.clock(),
            )
        )
        parent = self._parents.get(oid)
        if parent:
            for sibling, sibling_parent in self._parents.items():
                if sibling_parent == parent and sibling != oid:
                    self._cancel(sibling)

    def _cancel_children(self, oid: str) -> None:
        for child, parent in self._parents.items():
            if parent == oid:
                self._cancel(child)

    def _cancel(self, oid: str) -> None:
        rec = self._orders[oid]
        if rec.status in OPEN_ORDER_STATUSES:
            rec.status, rec.updated_at = OrderStatus.CANCELLED, self.clock()

    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        rec = self._orders.get(broker_order_id)
        return rec.model_copy(deep=True) if rec else None

    async def open_orders(self) -> list[OrderRecord]:
        return [
            r.model_copy(deep=True)
            for r in self._orders.values()
            if r.status in OPEN_ORDER_STATUSES
        ]

    async def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id in self._orders:
            self._cancel(broker_order_id)
            self._cancel_children(broker_order_id)

    async def cancel_all(self) -> None:
        for oid in self._orders:
            self._cancel(oid)

    async def fills_since(self, since: datetime) -> list[Fill]:
        return [f.model_copy() for f in self._fills if f.ts >= since]

    async def modify_stop(self, instrument: Instrument, new_stop: float) -> bool:
        moved = False
        for oid, rec in self._orders.items():
            if (
                self._kinds[oid] == "stop"
                and rec.request.instrument.key == instrument.key
                and rec.status in OPEN_ORDER_STATUSES
            ):
                rec.request.stop_loss, rec.updated_at = new_stop, self.clock()
                moved = True
        return moved


async def _replay_arm(
    name: str,
    records: Sequence[ReplayFrame],
    settings: BotSettings,
    starting_equity: float,
    plan: Plan,
    slippage_bps: float,
) -> ReplayArmResult:
    settings = settings.model_copy(
        deep=True,
        update={
            "autopilot": True,
            "decision_mode": name,
            "llm_enabled": name != "rules",
        },
    )
    clock = _Clock(records[0].ts)
    broker = _ReplayBroker(clock, settings, starting_equity, plan, slippage_bps)
    db = Database(":memory:")
    db.save_settings(settings)
    rules = RuleBasedDecisionMaker(settings)
    models = {n: _CapturedMaker(n, broker) for n in ("claude", "grok")}
    makers = {
        "rules": rules,
        **models,
        "consensus": ConsensusDecisionMaker(models["claude"], models["grok"]),
    }
    selected = (
        [models[name]] if name in models else list(models.values()) if name == "consensus" else []
    )
    # Never construct remote brokers, notification clients or network model clients.
    config = AppConfig(
        _env_file=None,
        broker="sim",
        allow_live=False,
        commission_plan=plan,
        cf_access_team_domain="",
        cf_access_aud="",
        TRADER_ANTHROPIC_API_KEY=None,
        TRADER_XAI_API_KEY=None,
        ntfy_url=None,
        ntfy_token=None,
    )
    engine = Engine(
        config,
        db,
        broker,
        makers[name],
        clock=clock,
        scanner=_ReplayScanner(broker, settings),
        makers=makers,
    )
    curve: list[EquityObservation] = []
    warnings: list[str] = []
    try:
        for frame in records:
            broker.advance(frame)
            if frame.decision_cycle:
                await engine.run_cycle()
            else:
                await engine.monitor_tick()
            account = await broker.account()
            known_cost = sum(m.known_cost for m in selected)
            unknown = any(m.unknown_cost for m in selected)
            curve.append(
                EquityObservation(
                    ts=frame.ts,
                    trading_equity=account.equity,
                    all_in_equity=None if unknown else account.equity - known_cost,
                )
            )
        # Synchronize orders/fills generated by the final engine tick without advancing
        # the clock, inventing a second observation or forcing an end-of-test liquidation.
        await engine.monitor_tick()
        account = await broker.account()
        missing = set().union(*(m.missing for m in selected)) if selected else set()
        observed = set.intersection(*(m.observed for m in selected)) if selected else set()
        known_cost = sum(m.known_cost for m in selected)
        unknown_cost = any(m.unknown_cost for m in selected)
        # Missing votes are unobserved experiments, not zero-cost HOLD observations.
        incomplete = unknown_cost or bool(missing)
        model_cost = None if incomplete else known_cost
        if missing:
            warnings.append(
                f"{len(missing)} decision cycles lack at least one required model vote; "
                "no replacement opinions were generated. All-in comparison is incomplete."
            )
        if unknown_cost:
            warnings.append("Original per-model costs are missing; all-in P&L is unknown.")
        if broker.invalid_quote_frames:
            warnings.append(
                f"{broker.invalid_quote_frames} frames contained unusable quotes; "
                "those observations could not generate fills."
            )
        if incomplete:
            for point in curve:
                point.all_in_equity = None
        # Engine fallbacks remain part of production behavior, e.g. exits during a model
        # budget halt; the report counts only cached model calls that actually occurred.
        peak, max_dd, max_dd_pct = starting_equity, 0.0, 0.0
        for point in curve:
            value = point.trading_equity if incomplete else point.all_in_equity
            assert value is not None
            peak = max(peak, value)
            max_dd = max(max_dd, peak - value)
            max_dd_pct = max(max_dd_pct, (peak - value) / peak * 100)
        positions = await broker.positions()
        if positions:
            warnings.append(
                "Open positions remain marked at the last valid observed bid; "
                "future exit commissions and slippage have not been realized."
            )
        if engine.last_error:
            warnings.append(f"Production engine reported: {engine.last_error}")
        trades = list(reversed(db.list_trades(limit=max(1, len(records) * 10))))
        orders = [r.model_copy(deep=True) for r in broker._orders.values()]
        trading_pnl = account.equity - starting_equity
        return ReplayArmResult(
            arm=name,
            starting_equity=starting_equity,
            ending_equity=account.equity,
            trading_pnl_usd=trading_pnl,
            realized_pnl_usd=sum(t.pnl for t in trades),
            commissions_usd=sum(f.commission for f in broker._fills),
            model_cost_usd=model_cost,
            known_model_cost_usd=known_cost,
            all_in_pnl_usd=None if incomplete else trading_pnl - known_cost,
            max_drawdown_usd=max_dd,
            max_drawdown_pct=max_dd_pct,
            drawdown_basis="trading_only" if incomplete else "all_in",
            missing_vote_cycles=len(missing),
            observed_vote_cycles=len(observed),
            model_calls_replayed=sum(m.calls for m in selected),
            unfilled_entries=sum(r.request.side == Side.BUY and r.filled_qty <= 0 for r in orders),
            invalid_quote_frames=broker.invalid_quote_frames,
            trades=trades,
            fills=broker._fills,
            orders=orders,
            open_positions=positions,
            equity_curve=curve,
            warnings=warnings,
        )
    finally:
        await engine._ledger_done()  # a pending scoring pass must not outlive its database
        await broker.disconnect()
        db.close()


async def replay_experiment(
    records: Sequence[ReplayFrame],
    settings: BotSettings,
    starting_equity: float = 600.0,
    commission_plan: Plan = "tiered",
    slippage_bps: float = 5.0,
    arms: Sequence[str] = ARMS,
) -> ReplayExperiment:
    """Replay one fixed cohort into independent portfolios using actual Engine loops.

    ``records`` must be chronological and from one captured account/settings cohort.
    Settings can intentionally differ from that cohort for a labeled counterfactual.
    Model-free historical bars are valid for ``arms=("rules",)``; an AI comparison with
    missing votes stays incomplete rather than claiming AI did or did not add value.
    """
    if not records:
        raise ValueError("no replay frames: capture real observations or supply historical CSV")
    if not math.isfinite(starting_equity) or starting_equity <= 0:
        raise ValueError("starting equity must be finite and positive")
    if not math.isfinite(slippage_bps) or not 0 <= slippage_bps < 10_000:
        raise ValueError("slippage must be finite and between 0 and 10000 basis points")
    if commission_plan not in {"tiered", "fixed"}:
        raise ValueError("commission plan must be tiered or fixed")
    if not arms or len(set(arms)) != len(arms) or set(arms) - set(ARMS):
        raise ValueError(f"choose unique research arms from {', '.join(ARMS)}")
    if any(a.ts >= b.ts for a, b in zip(records, records[1:], strict=False)):
        raise ValueError("replay frames must have unique, increasing timestamps")
    cohorts = {(r.account_id, r.account_mode) for r in records if r.account_id is not None}
    if len(cohorts) > 1:
        raise ValueError("mixed captured accounts/modes: select one account cohort")
    snapshots = {r.settings.model_dump_json() for r in records if r.settings is not None}
    if len(snapshots) > 1:
        raise ValueError("mixed captured settings: freeze settings and select one cohort")
    if len({r.cohort for r in records if r.cohort is not None}) > 1:
        raise ValueError("mixed captured cohorts: select one uninterrupted experiment")
    warnings = [
        "Captured-vote selection replay reuses opinions generated for the original portfolio; "
        "it cannot establish what a model would have said to each alternative portfolio.",
        "These results are research observations, not evidence that a strategy is profitable. "
        "Freeze settings and evaluate new, out-of-sample paper observations before promotion.",
    ]
    if not any(not r.decision_cycle for r in records):
        warnings.append(
            "Sparse decision-cycle observations: intra-cycle stops, targets and "
            "monitor confirmations cannot be reconstructed. Exit timing may differ "
            "from continuous production monitoring."
        )
    elif any(
        (b.ts - a.ts).total_seconds() > settings.monitor_seconds * 2
        for a, b in zip(records, records[1:], strict=False)
        if trading_day(a.ts) == trading_day(b.ts)
    ):
        warnings.append(
            "Observation gaps exceed the production monitor cadence; unobserved "
            "price paths and exit opportunities are not reconstructed."
        )
    if any(r.source == "historical_bars" for r in records):
        warnings.append(
            "Historical CSV uses inferred bar-close bid/ask proxies. Intrabar path, "
            "spread and liquidity are unavailable; results are approximate."
        )
    if snapshots and settings.model_dump_json() not in snapshots:
        warnings.append(
            "Replay settings differ from captured settings: this is a "
            "counterfactual parameter test, not a reproduction of the original run."
        )
    results = {}
    for name in arms:
        results[name] = await _replay_arm(
            name,
            records,
            settings,
            starting_equity,
            commission_plan,
            slippage_bps,
        )
    return ReplayExperiment(
        starting_equity=starting_equity,
        frame_count=len(records),
        decision_cycles=sum(r.decision_cycle for r in records),
        source_types=sorted({r.source for r in records}),
        started_at=records[0].ts,
        ended_at=records[-1].ts,
        settings=settings,
        commission_plan=commission_plan,
        slippage_bps=slippage_bps,
        arms=results,
        warnings=warnings,
        assumptions=[
            "Production Engine, RiskManager, Scanner, rules and consensus merge are shared; "
            "only the offline execution broker and captured-vote reader are research-specific.",
            "Scanner-snapshot captures replay the original scanner candidates and indicator "
            "features; raw historical bars are not stored and historical rescanning is "
            "unavailable. The historical CSV route recomputes the scanner from completed bars.",
            "Each arm starts with the same equity in an isolated virtual cash account. "
            "Autopilot approves only virtual eligible orders; no real broker or model is called.",
            "Entries cannot fill on their decision snapshot; later limit fills must cover "
            "the observed ask plus slippage. Targets require bid trade-through. "
            "Stops/market exits sell at the observed bid minus slippage.",
            "Full fills assume sufficient displayed liquidity; queue position, partial fills "
            "and unobserved intrabar prices are not reconstructed.",
            "Stock sale proceeds become spendable on the next observed stock trading day. "
            "No borrowing, shorting or leverage is modeled.",
            "Trading P&L includes modeled commissions/spread/slippage; all-in P&L further "
            "subtracts known recorded model costs. Hosting/data subscriptions are excluded.",
            "Delayed, stale, future-dated, halted or invalid quotes never generate fills. "
            "Open positions are not forcibly liquidated at the end of the sample.",
        ],
    )


def load_frames(path: Path) -> list[ReplayFrame]:
    """Read JSONL exports without changing a production journal."""
    frames = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                frames.append(ReplayFrame.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: invalid replay frame: {exc}") from exc
    return frames


def frames_from_bars(
    data: Sequence[tuple[Instrument, Sequence[Bar]]],
    settings: BotSettings,
    spread_bps: float = 10.0,
) -> list[ReplayFrame]:
    """Reveal only completed historical bars, using explicitly inferred close quotes.

    Historical OHLC does not imply a tick path. A frame only sees its current close and
    earlier complete bars; it never spends its high/low as if they happened after entry.
    """
    if not math.isfinite(spread_bps) or not 0 <= spread_bps < 10_000:
        raise ValueError("spread must be finite and between 0 and 10000 basis points")
    interval = BAR_INTERVALS[settings.bar_size]
    events: dict[datetime, list[tuple[Instrument, Bar]]] = {}
    keys = [inst.key for inst, _ in data]
    if len(keys) != len(set(keys)):
        raise ValueError("one historical bar series per instrument is required")
    for inst, bars in data:
        if any(bar.ts.tzinfo is None for bar in bars):
            raise ValueError("historical bar timestamps must include a timezone")
        if any(a.ts >= b.ts for a, b in zip(bars, bars[1:], strict=False)):
            raise ValueError("historical bars must have unique, increasing timestamps")
        for bar in bars:
            values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
            if (
                not all(math.isfinite(v) for v in values)
                or min(values[:4]) <= 0
                or bar.high < max(bar.open, bar.close, bar.low)
                or bar.low > min(bar.open, bar.close)
                or bar.volume < 0
            ):
                raise ValueError("inconsistent historical OHLCV values")
            events.setdefault(bar.ts + interval, []).append((inst, bar))
    frames: list[ReplayFrame] = []
    history: dict[str, list[Bar]] = {}
    for ts, updates in sorted(events.items()):
        quotes = {}
        for inst, bar in updates:
            history.setdefault(inst.key, []).append(bar)
            half = spread_bps / 20_000
            quotes[inst.key] = Quote(
                instrument=inst,
                bid=bar.close * (1 - half),
                ask=bar.close * (1 + half),
                last=bar.close,
                ts=ts,
            )
        # Match production bar lookback without copying an ever-growing full history.
        cutoff = ts - timedelta(days=10)
        for key in history:
            history[key] = [bar for bar in history[key] if bar.ts >= cutoff]
        frames.append(
            ReplayFrame(
                ts=ts,
                quotes=quotes,
                bars={key: list(bars) for key, bars in history.items()},
                source="historical_bars",
                candidates=None,
            )
        )
    return frames


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.research", description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--frames", type=Path, help="captured JSONL observations")
    sources.add_argument("--database", type=Path, help="journal with captured research frames")
    sources.add_argument(
        "--csv",
        action="append",
        metavar="SYMBOL=PATH",
        help="historical OHLCV CSV, repeat per symbol; rules arm only",
    )
    parser.add_argument("--settings", type=Path, help="frozen BotSettings JSON")
    parser.add_argument("--arms", help="comma-separated rules,claude,grok,consensus")
    parser.add_argument(
        "--equity", type=float, help="starting equity (default: captured equity, or 600 for CSV)"
    )
    parser.add_argument(
        "--plan",
        choices=["tiered", "fixed"],
        help="commission plan (default: captured plan, or tiered)",
    )
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument(
        "--spread-bps", type=float, default=10.0, help="inferred spread for historical CSV quotes"
    )
    parser.add_argument("--bar-size", choices=list(BAR_INTERVALS), default="5 mins")
    parser.add_argument("--limit", type=int, default=1000, help="latest captured database frames")
    parser.add_argument("--days", type=int, default=30, help="database observation window (1-90)")
    parser.add_argument(
        "--save-report",
        action="store_true",
        help="save output to database research_report; requires --database",
    )
    args = parser.parse_args(argv)
    db = None
    try:
        if args.save_report and args.database is None:
            raise ValueError("--save-report requires --database")
        if not 1 <= args.limit <= 10_000:
            raise ValueError("--limit must be between 1 and 10000")
        if not 1 <= args.days <= 90:
            raise ValueError("--days must be between 1 and 90")
        if args.frames:
            records = load_frames(args.frames)
        elif args.database:
            if not args.database.is_file():
                raise ValueError("captured research database does not exist")
            db = Database(args.database)
            since = datetime.now(UTC) - timedelta(days=args.days)
            epoch = db.get_kv("journal_account_since")
            if epoch:
                epoch_ts = datetime.fromisoformat(epoch)
                since = max(
                    since, epoch_ts.replace(tzinfo=UTC) if epoch_ts.tzinfo is None else epoch_ts
                )
            account = db.get_kv("broker_account")
            records = [
                ReplayFrame.model_validate(p)
                for p in db.list_research_frames(
                    since=since,
                    limit=args.limit,
                )
                if not account or p.get("account_id") == account
            ]
            # Match the dashboard's frozen experiment: latest uninterrupted cohort.
            if records and records[-1].cohort is not None:
                first = len(records) - 1
                while first > 0 and records[first - 1].cohort == records[-1].cohort:
                    first -= 1
                records = records[first:]
        else:
            records = []
        settings = (
            BotSettings.model_validate_json(args.settings.read_text(encoding="utf-8"))
            if args.settings
            else records[0].settings
            if records and records[0].settings
            else BotSettings(bar_size=args.bar_size)
        )
        arms = tuple(args.arms.split(",")) if args.arms else ("rules",) if args.csv else ARMS
        if args.csv:
            from trader.backtest import load_csv

            if arms != ("rules",):
                raise ValueError("historical CSV has no captured AI votes; choose --arms rules")
            data = []
            for spec in args.csv:
                symbol, sep, filename = spec.partition("=")
                if not sep or not symbol or not filename:
                    raise ValueError("--csv expects SYMBOL=PATH")
                inst = (
                    Instrument.from_key(symbol)
                    if ":" in symbol
                    else Instrument(symbol=symbol.upper())
                )
                data.append((inst, load_csv(Path(filename))))
            settings = settings.model_copy(
                deep=True,
                update={
                    "stock_universe": [
                        inst.symbol for inst, _ in data if inst.asset_class == AssetClass.STOCK
                    ],
                    "crypto_universe": [
                        inst.symbol for inst, _ in data if inst.asset_class == AssetClass.CRYPTO
                    ],
                    "trade_stocks": any(inst.asset_class == AssetClass.STOCK for inst, _ in data),
                    "trade_crypto": any(inst.asset_class == AssetClass.CRYPTO for inst, _ in data),
                    "dynamic_universe": False,
                },
            )
            records = frames_from_bars(data, settings, args.spread_bps)
        result = asyncio.run(
            replay_experiment(
                records,
                settings,
                args.equity
                if args.equity is not None
                else (records[0].starting_equity if records else None) or 600.0,
                args.plan or (records[0].commission_plan if records else None) or "tiered",
                args.slippage_bps,
                arms,
            )
        )
        payload = result.model_dump(mode="json")
        if args.save_report:
            assert db is not None
            db.set_kv(
                "research_report",
                json.dumps(
                    {
                        "status": "ready",
                        "cohort": records[-1].cohort,
                        "days": args.days,
                        "generated_at": datetime.now(UTC).isoformat(),
                        "report": payload,
                    },
                    allow_nan=False,
                ),
            )
        print(json.dumps(payload, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"research: error: {exc}", file=sys.stderr)
        return 2
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
