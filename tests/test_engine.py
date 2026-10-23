"""Engine tests on an in-memory FakeBroker with a fake decision maker, scanner and clock."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from trader.broker.base import Broker, BrokerError
from trader.config import AppConfig
from trader.db import Database
from trader.engine import Engine, build_engine, replay_fills, score_signal
from trader.models import (
    OPEN_ORDER_STATUSES,
    AccountSnapshot,
    AIDecision,
    AssetClass,
    Bar,
    BotSettings,
    Candidate,
    DecisionBatch,
    DecisionContext,
    ExitRules,
    Fill,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RiskLimits,
    Side,
    SignalRecord,
    TradeAction,
)
from trader.notify import Notifier
from trader.strategy.decide import RuleBasedDecisionMaker

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # Wednesday 11:00 New York (EDT)
F = Instrument(symbol="F")
T = Instrument(symbol="T")


# =========================================================================== fakes
class FakeClock:
    def __init__(self, t: datetime = NOW):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class FakeBroker(Broker):
    """In-memory broker: fills at the bid/ask, native bracket stops for stocks (toggle),
    controllable prices, halts, failures and late commission reports."""

    name = "fake"

    def __init__(
        self,
        clock: FakeClock,
        *,
        mode: Mode = Mode.SIM,
        cash: float = 500.0,
        native_stops: bool = True,
        fee: float = 0.35,
    ):
        self.clock = clock
        self._mode = mode
        self._connected = False
        self.can_connect = True
        self.connect_calls = 0
        self.cash = cash
        self.native = native_stops
        self.fee = fee
        self.delayed = False
        self.late_commission = False
        self.standalone_stops = True
        self.fail: set[str] = set()
        self.quotes: dict[str, dict[str, float | bool | None]] = {}
        self.pos: dict[str, list[float]] = {}  # key -> [qty, avg]
        self.orders: dict[str, OrderRecord] = {}
        self.kind: dict[str, str] = {}
        self.parent: dict[str, str | None] = {}
        self.fills: list[Fill] = []
        self.pending_commission: dict[str, float] = {}
        self.bar_data: dict[str, list[Bar]] = {}
        self.movers: list[str] = []
        self.placed: list[OrderRequest] = []
        self.quote_calls: list[str] = []
        self.modify_calls: list[tuple[str, float]] = []
        self._ids = itertools.count(1)
        self._execs = itertools.count(1)

    # ------------------------------------------------------------ controls
    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise BrokerError(f"{name} failed")

    def set_price(
        self,
        symbol: str,
        price: float,
        *,
        spread: float = 0.02,
        halted: bool = False,
        asset_class: AssetClass = AssetClass.STOCK,
        bid: float | None | str = "auto",
    ) -> None:
        key = Instrument(symbol=symbol, asset_class=asset_class).key
        self.quotes[key] = {
            "bid": round(price - spread / 2, 4) if bid == "auto" else bid,
            "ask": round(price + spread / 2, 4),
            "last": price,
            "halted": halted,
        }
        self._match(key)

    def settle_commissions(self) -> None:
        for i, fill in enumerate(self.fills):
            if fill.exec_id in self.pending_commission:
                commission = self.pending_commission.pop(fill.exec_id)
                self.fills[i] = fill.model_copy(update={"commission": commission})

    def working(self, key: str, kind: str) -> list[str]:
        return [
            oid
            for oid, rec in self.orders.items()
            if rec.request.instrument.key == key
            and self.kind[oid] == kind
            and rec.status in OPEN_ORDER_STATUSES
            and self._active(oid)
        ]

    # ------------------------------------------------------------ matching
    def _active(self, oid: str) -> bool:
        parent = self.parent.get(oid)
        return parent is None or self.orders[parent].filled_qty > 0

    def _match(self, key: str) -> None:
        q = self.quotes.get(key)
        if q is None:
            return
        bid, ask = q["bid"], q["ask"]
        for oid, rec in list(self.orders.items()):
            rec = self.orders[oid]
            req = rec.request
            if req.instrument.key != key or rec.status not in OPEN_ORDER_STATUSES:
                continue
            if not self._active(oid):
                continue
            kind = self.kind[oid]
            if kind == "stop" and bid is not None and bid <= req.stop_loss:
                self._fill(oid, bid)
            elif kind in ("target", "exit") and bid is not None and bid >= req.limit_price:
                self._fill(oid, bid)
            elif kind == "entry" and ask is not None and ask <= req.limit_price:
                self._fill(oid, ask)

    def _fill(self, oid: str, price: float) -> None:
        rec = self.orders[oid]
        req = rec.request
        key = req.instrument.key
        qty = req.qty - rec.filled_qty
        if req.side == Side.SELL:
            qty = min(qty, self.pos.get(key, [0.0, 0.0])[0])
            if qty <= 0:
                rec.status = OrderStatus.CANCELLED
                return
        exec_id = f"E{next(self._execs)}"
        commission = 0.0 if self.late_commission else self.fee
        if self.late_commission:
            self.pending_commission[exec_id] = self.fee
        self.fills.append(
            Fill(
                exec_id=exec_id,
                broker_order_id=oid,
                instrument=req.instrument,
                side=req.side,
                qty=qty,
                price=price,
                commission=commission,
                ts=self.clock(),
            )
        )
        held, avg = self.pos.get(key, [0.0, 0.0])
        if req.side == Side.BUY:
            self.pos[key] = [held + qty, (held * avg + qty * price) / (held + qty)]
            self.cash -= qty * price + self.fee
        else:
            self.cash += qty * price - self.fee
            if held - qty <= 1e-9:
                del self.pos[key]
            else:
                self.pos[key] = [held - qty, avg]
        rec.filled_qty += qty
        rec.avg_fill_price = price
        rec.commission += commission
        rec.status = (
            OrderStatus.FILLED if rec.filled_qty >= req.qty - 1e-9 else OrderStatus.PARTIALLY_FILLED
        )
        parent = self.parent.get(oid)
        if parent is not None:  # OCA: one child fills, the sibling is cancelled
            for sibling, sib_parent in self.parent.items():
                sib = self.orders[sibling]
                if sib_parent == parent and sibling != oid and sib.status in OPEN_ORDER_STATUSES:
                    sib.status = OrderStatus.CANCELLED

    # ------------------------------------------------------------ Broker API
    async def connect(self) -> None:
        self.connect_calls += 1
        if not self.can_connect:
            raise BrokerError("gateway down")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def data_delayed(self) -> bool:
        return self.delayed

    def _require(self) -> None:
        if not self._connected:
            raise BrokerError("not connected")

    async def account(self) -> AccountSnapshot:
        self._require()
        self._maybe_fail("account")
        value = sum(qty * float(self.quotes[k]["last"]) for k, (qty, _) in self.pos.items())
        return AccountSnapshot(
            account_id="FAKE",
            mode=self._mode,
            equity=self.cash + value,
            cash=self.cash,
            settled_cash=self.cash,
            buying_power=self.cash,
        )

    async def positions(self) -> list[Position]:
        self._require()
        self._maybe_fail("positions")
        return [
            Position(
                instrument=Instrument.from_key(k),
                qty=qty,
                avg_cost=avg,
                market_price=float(self.quotes[k]["last"]),
            )
            for k, (qty, avg) in self.pos.items()
        ]

    async def quote(self, instrument: Instrument) -> Quote:
        self._require()
        self._maybe_fail("quote")
        self.quote_calls.append(instrument.key)
        q = self.quotes.get(instrument.key)
        if q is None:
            raise BrokerError(f"no market data for {instrument.key}")
        return Quote(
            instrument=instrument,
            bid=q["bid"],
            ask=q["ask"],
            last=q["last"],
            ts=self.clock(),
            halted=bool(q["halted"]),
            delayed=self.delayed,
        )

    async def bars(
        self, instrument: Instrument, bar_size: str = "5 mins", lookback: str = "2 D"
    ) -> list[Bar]:
        self._require()
        self._maybe_fail("bars")
        return list(self.bar_data.get(instrument.key, []))

    async def place_order(self, req: OrderRequest) -> OrderRecord:
        self._require()
        self._maybe_fail("place_order")
        self.placed.append(req)
        oid = f"O{next(self._ids)}"
        key = req.instrument.key
        if req.side == Side.SELL:
            kind = "stop" if req.stop_loss is not None else "exit"
            held = self.pos.get(key, [0.0, 0.0])[0]
            if kind == "exit" and req.qty > held + 1e-9:
                return OrderRecord(request=req, status=OrderStatus.REJECTED, message="no shorting")
        else:
            kind = "entry"
        self.orders[oid] = OrderRecord(
            request=req, status=OrderStatus.SUBMITTED, broker_order_id=oid
        )
        self.kind[oid], self.parent[oid] = kind, None
        if kind == "entry" and self.supports_native_stops(req.instrument):
            if req.stop_loss is not None:
                self._child(oid, "stop", req, stop_loss=req.stop_loss)
            if req.take_profit is not None:
                self._child(oid, "target", req, limit=req.take_profit)
        if kind == "exit" and req.order_type == OrderType.MARKET:
            bid = self.quotes[key]["bid"]
            self._fill(oid, float(bid))
        else:
            self._match(key)
        return self.orders[oid].model_copy()

    def _child(
        self,
        parent: str,
        kind: str,
        req: OrderRequest,
        stop_loss: float | None = None,
        limit: float | None = None,
    ) -> None:
        child = OrderRequest(
            instrument=req.instrument,
            side=Side.SELL,
            qty=req.qty,
            order_type=OrderType.MARKET if kind == "stop" else OrderType.LIMIT,
            stop_loss=stop_loss,
            limit_price=limit,
            is_exit=True,
            reason="protective stop" if kind == "stop" else "take profit",
        )
        oid = f"{parent}-{'SL' if kind == 'stop' else 'TP'}"
        self.orders[oid] = OrderRecord(
            request=child, status=OrderStatus.SUBMITTED, broker_order_id=oid
        )
        self.kind[oid], self.parent[oid] = kind, parent

    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        self._require()
        rec = self.orders.get(broker_order_id)
        return rec.model_copy() if rec else None

    async def open_orders(self) -> list[OrderRecord]:
        self._require()
        self._maybe_fail("open_orders")
        return [
            rec.model_copy()
            for oid, rec in self.orders.items()
            if rec.status in OPEN_ORDER_STATUSES and self._active(oid)
        ]

    async def cancel_order(self, broker_order_id: str) -> None:
        self._require()
        rec = self.orders.get(broker_order_id)
        if rec is None or rec.status not in OPEN_ORDER_STATUSES:
            return
        rec.status = OrderStatus.CANCELLED
        if rec.filled_qty <= 0:
            for child, parent in self.parent.items():
                if parent == broker_order_id:
                    self.orders[child].status = OrderStatus.CANCELLED

    async def cancel_all(self) -> None:
        self._require()
        self._maybe_fail("cancel_all")
        for rec in self.orders.values():
            if rec.status in OPEN_ORDER_STATUSES:
                rec.status = OrderStatus.CANCELLED

    async def fills_since(self, since: datetime) -> list[Fill]:
        self._require()
        self._maybe_fail("fills_since")
        return [f.model_copy() for f in self.fills if f.ts >= since]

    def supports_native_stops(self, instrument: Instrument) -> bool:
        return self.native and instrument.asset_class == AssetClass.STOCK

    def supports_standalone_stops(self, instrument: Instrument) -> bool:
        return self.standalone_stops and self.supports_native_stops(instrument)

    def estimate_commission(self, instrument: Instrument, qty: float, price: float) -> float:
        return self.fee

    async def modify_stop(self, instrument: Instrument, new_stop: float) -> bool:
        self.modify_calls.append((instrument.key, new_stop))
        if not self.supports_native_stops(instrument):
            return False
        stops = self.working(instrument.key, "stop")
        for oid in stops:
            rec = self.orders[oid]
            rec.request = rec.request.model_copy(update={"stop_loss": new_stop})
        self._match(instrument.key)
        return bool(stops)

    async def scan_movers(self, limit: int = 10) -> list[str]:
        return list(self.movers[:limit])


class FakeScanner:
    def __init__(self, settings: BotSettings):
        self.settings = settings
        self.candidates: dict[str, list[Candidate]] = {}
        self.atr: dict[str, float] = {}
        self.scanned: list[str] = []

    def scan(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None, now: datetime
    ) -> list[Candidate]:
        self.scanned.append(instrument.key)
        return [c.model_copy() for c in self.candidates.get(instrument.key, [])]

    def snapshot(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None = None
    ) -> dict[str, float]:
        price = quote.price if quote is not None and quote.price else 0.0
        return {"price": price, "atr14": self.atr.get(instrument.key, 0.1)}


class FakeDecisionMaker:
    def __init__(self, name: str = "claude", cost: float = 0.0):
        self.name = name
        self.cost = cost
        self.decisions: list[AIDecision] = []
        self.calls: list[DecisionContext] = []

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        self.calls.append(ctx)
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model="fake-model",
            market_view="fake view",
            decisions=list(self.decisions),
            candidates=ctx.candidates,
            cost_usd=self.cost,
        )


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(
        self, title: str, message: str, priority: str = "default", tags: list[str] | None = None
    ) -> bool:
        self.sent.append((title, message, priority))
        return True

    def titles(self) -> list[str]:
        return [title for title, _, _ in self.sent]


# =========================================================================== harness
def cand(
    symbol: str = "F",
    price: float = 10.0,
    stop: float = 9.8,
    target: float = 10.5,
    score: float = 0.8,
    setup: str = "vwap_reclaim",
) -> Candidate:
    return Candidate(
        instrument=Instrument(symbol=symbol),
        setup=setup,
        score=score,
        price=price,
        suggested_stop=stop,
        suggested_target=target,
    )


def buy(symbol: str = "F", setup: str = "vwap_reclaim", **kw) -> AIDecision:
    return AIDecision(
        action=TradeAction.BUY,
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        conviction=0.8,
        setup=setup,
        rationale="clean reclaim",
        **kw,
    )


def hold(symbol: str) -> AIDecision:
    return AIDecision(
        action=TradeAction.HOLD,
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        conviction=0.5,
        rationale="unclear",
    )


@dataclass
class H:
    engine: Engine
    broker: FakeBroker
    clock: FakeClock
    scanner: FakeScanner
    decider: FakeDecisionMaker
    notes: RecordingNotifier
    db: Database

    def day(self):
        return self.db.get_day(self.engine._today(self.clock()).day)

    def orders(self, status: OrderStatus | None = None) -> list[OrderRecord]:
        return self.db.list_orders(limit=500, statuses=[status] if status else None)


async def harness(
    db: Database,
    config: AppConfig,
    *,
    mode: Mode = Mode.SIM,
    native: bool = True,
    risk: dict | None = None,
    exits: dict | None = None,
    now: datetime = NOW,
    **settings_kw,
) -> H:
    base = {"stock_universe": ["F", "T"], "crypto_universe": ["BTC"], "trade_crypto": False}
    settings = BotSettings(
        risk=RiskLimits(**(risk or {})),
        exits=ExitRules(**(exits or {})),
        **{**base, **settings_kw},
    )
    db.save_settings(settings)
    clock = FakeClock(now)
    broker = FakeBroker(clock, mode=mode, native_stops=native)
    broker.set_price("F", 10.0)
    broker.set_price("T", 20.0)
    await broker.connect()
    scanner = FakeScanner(settings)
    scanner.candidates["STK:F"] = [cand()]
    decider = FakeDecisionMaker()
    decider.decisions = [buy()]
    notes = RecordingNotifier()
    engine = Engine(
        config,
        db,
        broker,
        decider,
        notifier=notes,
        clock=clock,
        scanner=scanner,  # type: ignore[arg-type]
    )
    return H(engine, broker, clock, scanner, decider, notes, db)


async def enter_f(h: H) -> OrderRecord:
    batch = await h.engine.run_cycle()
    assert batch is not None
    orders = [o for o in h.orders() if o.request.side == Side.BUY]
    assert orders, "no entry journaled"
    return orders[0]


# =========================================================================== entries
async def test_entry_placed_with_stop_and_meta(db, config) -> None:
    h = await harness(db, config)
    rec = await enter_f(h)
    assert rec.status == OrderStatus.FILLED
    assert rec.request.qty == 24  # 50% position cap: $250 / $10.01
    assert rec.request.stop_loss == 9.8 and rec.request.take_profit == 10.5
    assert h.broker.working("STK:F", "stop"), "bracket stop must be working at the broker"
    meta = db.get_position_meta("STK:F")
    assert meta is not None
    assert meta["stop_loss"] == 9.8 and meta["entry_price"] == 10.01
    assert meta["initial_risk"] == pytest.approx(0.21 * 24)
    assert h.day().trades == 1
    assert h.day().start_equity == 500.0
    sig = db.list_signals()[0]
    assert sig.taken and sig.order_id == rec.id and sig.decision_id is not None
    assert "BUY F" in h.notes.titles()


async def test_never_adds_to_existing_position(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    buys = [o for o in h.orders() if o.request.side == Side.BUY]
    assert len(buys) == 2
    assert buys[0].status == OrderStatus.BLOCKED and "never add" in buys[0].message
    assert len([r for r in h.broker.placed if r.side == Side.BUY]) == 1


async def test_stop_hit_journals_trade_and_day_stats(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.broker.set_price("F", 9.70)  # bid 9.69 -> broker stop at 9.80 fills
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    trades = db.list_trades()
    assert len(trades) == 1
    tr = trades[0]
    assert tr.qty == 24 and tr.entry_price == 10.01 and tr.exit_price == 9.69
    assert tr.commission == pytest.approx(0.70)
    assert tr.pnl == pytest.approx(24 * (9.69 - 10.01) - 0.70)
    assert tr.r_multiple == pytest.approx(tr.pnl / (0.21 * 24), abs=1e-3)
    assert tr.exit_reason == "stop-loss"
    assert tr.setup == "vwap_reclaim" and tr.decision_id is not None
    day = h.day()
    assert day.realized_pnl == pytest.approx(tr.pnl)
    assert (day.wins, day.losses, day.loss_streak) == (0, 1, 1)
    assert day.fees == pytest.approx(0.70)
    assert db.get_position_meta("STK:F") is None


async def test_unaffordable_and_leveraged_symbols_skipped_before_decision(db, config) -> None:
    h = await harness(db, config, stock_universe=["F", "SPY", "TQQQ"])
    h.broker.set_price("SPY", 764.0)
    h.broker.set_price("TQQQ", 50.0)
    h.scanner.candidates["STK:SPY"] = [cand("SPY", 764, 760, 775)]
    h.scanner.candidates["STK:TQQQ"] = [cand("TQQQ", 50, 49, 53)]
    await h.engine.run_cycle()
    assert "STK:SPY" not in h.scanner.scanned and "STK:TQQQ" not in h.scanner.scanned
    assert "STK:TQQQ" not in h.broker.quote_calls  # skipped without even a quote
    ctx = h.decider.calls[-1]
    assert {c.instrument.symbol for c in ctx.candidates} == {"F"}


async def test_halted_symbol_not_entered_and_cooldown(db, config) -> None:
    h = await harness(db, config)
    h.broker.set_price("F", 10.0, halted=True)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == [] and not h.decider.calls
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    assert db.list_signals()[0].skip_reason == "halted"

    h.broker.set_price("F", 10.0)  # reopened, but inside the 15 min cooldown
    h.clock.advance(minutes=5)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == []
    assert not [r for r in h.broker.placed if r.side == Side.BUY]

    h.clock.advance(minutes=16)
    await h.engine.run_cycle()
    assert [r for r in h.broker.placed if r.side == Side.BUY]


async def test_stock_candidates_outside_entry_window_are_held_back(db, config) -> None:
    h = await harness(db, config, now=datetime(2026, 10, 21, 13, 35, tzinfo=UTC))  # 09:35 ET
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == [] and not h.decider.calls
    assert db.list_signals()[0].skip_reason == "outside the stock entry window"


async def test_buy_for_non_candidate_is_ignored(db, config) -> None:
    h = await harness(db, config)
    h.decider.decisions = [buy("T")]
    await h.engine.run_cycle()
    assert not h.broker.placed


# =========================================================================== halts
async def test_daily_loss_halts_and_flattens(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 8.50)  # equity at the bid ~ $463: -7.4%
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    day = h.day()
    assert day.halted and day.halt_reason.startswith("daily loss limit")
    assert h.broker.pos == {}
    assert "Trading halted" in h.notes.titles()
    assert [p for t, _, p in h.notes.sent if t == "Trading halted"] == ["high"]
    actions = {(a["action"], a["actor"]) for a in db.list_audit()}
    assert ("halt", "engine") in actions and ("flatten", "engine") in actions
    trades = db.list_trades()
    assert len(trades) == 1 and trades[0].exit_reason.startswith("daily loss limit")
    calls = len(h.decider.calls)
    h.clock.advance(minutes=5)
    assert await h.engine.run_cycle() is None
    assert len(h.decider.calls) == calls


async def test_resume_clears_halt_and_rebaselines(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 8.50)
    await h.engine.monitor_tick()
    assert h.day().halted
    await h.engine.resume()
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert not h.day().halted
    assert h.engine.status().halted is False


async def test_target_reached_soft_halt_keeps_managing_stops(db, config) -> None:
    h = await harness(
        db, config, native=False, risk={"daily_profit_target_usd": 5}, exits={"breakeven_at_r": 0}
    )
    await enter_f(h)
    h.broker.set_price("F", 10.30)  # +$6.4 at the bid
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    day = h.day()
    assert day.halted and day.halt_reason.startswith("daily target reached")
    assert h.broker.pos  # a soft halt does not flatten
    calls = len(h.decider.calls)
    assert await h.engine.run_cycle() is None
    assert len(h.decider.calls) == calls  # no new entries
    h.broker.set_price("F", 9.70)  # through the engine-managed stop
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert h.broker.pos == {}
    assert db.list_trades()[0].exit_reason.startswith("stop-loss 9.8 hit")


async def test_kill_switch(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    assert h.broker.working("STK:F", "stop")
    await h.engine.kill()
    day = h.day()
    assert day.halted and day.halt_reason == "kill switch"
    assert h.broker.pos == {}
    assert not h.broker.working("STK:F", "stop") and not h.broker.working("STK:F", "target")
    assert "Kill switch" in h.notes.titles()
    h.clock.advance(minutes=1)
    assert await h.engine.run_cycle() is None


async def test_kill_switch_failure_is_reported_not_raised(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.broker.fail = {"cancel_all"}
    await h.engine.kill()
    assert h.day().halted
    assert "KILL switch FAILED" in h.notes.titles()
    assert "kill switch failed" in (h.engine.status().last_error or "")


async def test_kill_switch_carries_over_to_next_day_but_loss_halt_does_not(db, config) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    await h.engine.kill()
    h.clock.advance(days=1)
    await h.engine.monitor_tick()
    assert h.day().day == "2026-10-22" and h.day().halt_reason == "kill switch"
    await h.engine.resume()
    h.engine._day.halted, h.engine._day.halt_reason = True, "daily loss limit: test"
    h.engine._save_day()
    h.clock.advance(days=1)
    await h.engine.monitor_tick()
    assert h.day().day == "2026-10-23" and not h.day().halted


# =========================================================================== live gating
async def test_shadow_mode_blocks_orders_on_live(db, config) -> None:
    h = await harness(db, config, mode=Mode.LIVE)
    assert h.engine.shadow and h.engine.status().shadow
    await h.engine.run_cycle()
    assert not h.broker.placed
    rec = h.orders()[0]
    assert rec.status == OrderStatus.BLOCKED and "shadow mode" in rec.message
    assert db.list_signals()[0].skip_reason == "shadow mode"
    with pytest.raises(PermissionError):
        await h.engine.arm_live()


async def test_armed_live_sends_orders(db, tmp_path) -> None:
    config = AppConfig(_env_file=None, data_dir=tmp_path, allow_live=True)
    h = await harness(db, config, mode=Mode.LIVE)
    assert h.engine.shadow
    await h.engine.arm_live()
    assert not h.engine.shadow
    await h.engine.run_cycle()
    assert [r for r in h.broker.placed if r.side == Side.BUY]
    await h.engine.disarm_live()
    assert h.engine.shadow


async def test_arm_live_refused_for_paper_account(db, tmp_path) -> None:
    config = AppConfig(_env_file=None, data_dir=tmp_path, allow_live=True)
    h = await harness(db, config, mode=Mode.PAPER)
    with pytest.raises(PermissionError):
        await h.engine.arm_live()


# =========================================================================== approvals
async def test_autopilot_off_pending_then_approve(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    assert not h.broker.placed
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    assert "Approve BUY F?" in h.notes.titles()
    assert db.list_signals()[0].taken
    h.clock.advance(minutes=2)
    approved = await h.engine.approve_order(rec.id)
    assert approved.status == OrderStatus.FILLED and approved.broker_order_id
    assert "approved by owner" in approved.message
    assert db.get_position_meta("STK:F") is not None
    assert h.day().trades == 1
    with pytest.raises(ValueError):
        await h.engine.approve_order(rec.id)


async def test_no_duplicate_pending_orders(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert len(h.orders(OrderStatus.PENDING_APPROVAL)) == 1


async def test_approval_rechecks_risk_with_fresh_data(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    h.broker.set_price("F", 10.0, halted=True)
    result = await h.engine.approve_order(rec.id)
    assert result.status == OrderStatus.BLOCKED and "halted" in result.message
    assert not h.broker.placed
    sig = db.list_signals()[0]
    assert not sig.taken and sig.skip_reason.startswith("risk at approval")


async def test_reject_and_unknown_order(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    rejected = await h.engine.reject_order(rec.id)
    assert rejected.status == OrderStatus.CANCELLED and rejected.message == "rejected by owner"
    assert db.list_signals()[0].skip_reason == "rejected by owner"
    with pytest.raises(ValueError):
        await h.engine.reject_order(rec.id)
    with pytest.raises(LookupError):
        await h.engine.approve_order(9999)


async def test_pending_approval_expires(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    h.clock.advance(minutes=11)
    await h.engine.monitor_tick()
    expired = db.get_order(rec.id)
    assert expired.status == OrderStatus.CANCELLED and expired.message == "approval expired"
    assert db.list_signals()[0].skip_reason == "approval expired"


async def test_approving_an_expired_order_does_not_send(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    h.clock.advance(minutes=10)
    result = await h.engine.approve_order(rec.id)
    assert result.status == OrderStatus.CANCELLED and not h.broker.placed


# =========================================================================== LLM budget
async def test_llm_budget_falls_back_to_rules(db, config) -> None:
    h = await harness(db, config, risk={"llm_daily_budget_usd": 1.0})
    h.decider.cost = 1.5
    h.decider.decisions = [hold("F")]
    first = await h.engine.run_cycle()
    assert first is not None and first.maker == "claude"
    assert h.day().llm_cost == pytest.approx(1.5)
    assert db.list_signals()[0].skip_reason == "AI: HOLD"
    h.clock.advance(minutes=5)
    second = await h.engine.run_cycle()
    assert second is not None and second.maker == "rules"
    assert "LLM budget reached" in second.market_view
    assert len(h.decider.calls) == 1
    assert [r for r in h.broker.placed if r.side == Side.BUY]  # rules still trade


async def test_llm_skipped_when_disabled_or_nothing_worth_it(db, config) -> None:
    h = await harness(db, config, llm_enabled=False)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.maker == "rules" and "LLM disabled" in batch.market_view
    h2 = await harness(Database(":memory:"), config)
    h2.scanner.candidates["STK:F"] = [cand(score=0.3)]
    batch = await h2.engine.run_cycle()
    assert batch is not None and batch.maker == "rules"
    assert not h2.decider.calls
    assert h2.db.list_signals()[0].skip_reason == "score 0.30 < 0.50"


async def test_decision_maker_exception_is_journaled(db, config) -> None:
    h = await harness(db, config)

    async def boom(ctx: DecisionContext) -> DecisionBatch:
        raise RuntimeError("model exploded")

    h.decider.decide = boom  # type: ignore[method-assign]
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.error and "model exploded" in batch.error
    assert not h.broker.placed
    assert db.list_decisions()[0].error


# =========================================================================== signal ledger
def minute_bars(start: datetime, rows: list[tuple[float, float, float, float]]) -> list[Bar]:
    return [
        Bar(ts=start + timedelta(minutes=i), open=o, high=hi, low=lo, close=c, volume=1000)
        for i, (o, hi, lo, c) in enumerate(rows)
    ]


async def test_signal_ledger_taken_skipped_and_evaluated(db, config) -> None:
    h = await harness(db, config, stock_universe=["F", "T", "PFE"])
    h.broker.set_price("PFE", 25.0)
    h.scanner.candidates["STK:T"] = [cand("T", 20.0, 19.6, 21.0, score=0.7)]
    h.scanner.candidates["STK:PFE"] = [cand("PFE", 25.0, 24.5, 26.5, score=0.3)]
    await h.engine.run_cycle()
    sigs = {s.instrument.symbol: s for s in db.list_signals()}
    assert sigs["F"].taken and sigs["F"].order_id is not None
    assert not sigs["T"].taken and sigs["T"].skip_reason == "not top candidate"
    assert sigs["PFE"].skip_reason == "score 0.30 < 0.50"
    assert all(s.decision_id is not None for s in sigs.values())

    rising = [
        (10.0 + 0.02 * i, 10.02 + 0.02 * i, 9.99 + 0.02 * i, 10.02 + 0.02 * i) for i in range(40)
    ]
    h.broker.bar_data["STK:F"] = minute_bars(NOW, rising)
    both = [(20.0, 20.1, 19.9, 20.0)] * 3 + [(20.0, 21.2, 19.5, 20.5)] + [(20.5, 20.6, 20.4, 20.5)]
    h.broker.bar_data["STK:T"] = minute_bars(NOW, both)
    h.clock.t = datetime(2026, 10, 21, 20, 10, tzinfo=UTC)  # after the 16:00 ET close
    await h.engine.monitor_tick()
    sigs = {s.instrument.symbol: s for s in db.list_signals()}
    f = sigs["F"]
    assert f.evaluated and f.first_hit == "target"
    assert f.r_15m == pytest.approx((10.02 + 0.02 * 14 - 10.0) / 0.2, abs=1e-3)
    assert f.mfe_r > 2.5 and f.mae_r <= 0
    t = sigs["T"]
    assert t.evaluated and t.first_hit == "stop"  # stop counts first when both hit in a bar
    assert not sigs["PFE"].evaluated  # no bars yet: retried later


async def test_signal_ledger_dedupes_repeated_setups(db, config) -> None:
    h = await harness(db, config)
    h.decider.decisions = [hold("F")]
    await h.engine.run_cycle()
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert len(db.list_signals()) == 1
    h.clock.advance(minutes=31)
    await h.engine.run_cycle()
    assert len(db.list_signals()) == 2


def test_score_signal_stop_first_and_horizon() -> None:
    sig = SignalRecord(
        ts=NOW, instrument=F, setup="x", score=0.5, price=10.0, stop=9.8, target=10.4
    )
    bars = minute_bars(NOW - timedelta(minutes=2), [(10, 10, 10, 10)] * 2 + [(10, 10.5, 9.7, 10)])
    scored = score_signal(sig, bars, NOW + timedelta(hours=1))
    assert scored is not None and scored.first_hit == "stop"
    assert scored.mfe_r == pytest.approx(2.5) and scored.mae_r == pytest.approx(-1.5)
    assert score_signal(sig, [], NOW + timedelta(hours=1)) is None
    late = minute_bars(NOW + timedelta(hours=2), [(10, 11, 9, 10)])
    assert score_signal(sig, late, NOW + timedelta(hours=1)) is None


# =========================================================================== exit rules
async def test_time_stop(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    assert h.broker.pos == {}
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    trade = db.list_trades()[0]
    assert trade.exit_reason.startswith("time stop")


async def test_breakeven_moves_native_stop(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.broker.set_price("F", 10.25)  # bid 10.24 = +1.1R
    h.clock.advance(minutes=2)
    await h.engine.monitor_tick()
    assert h.broker.modify_calls == [("STK:F", 10.01)]
    assert db.get_position_meta("STK:F")["stop_loss"] == 10.01
    stop_id = h.broker.working("STK:F", "stop")[0]
    assert h.broker.orders[stop_id].request.stop_loss == 10.01
    h.broker.set_price("F", 10.0)  # bid 9.99: the broker stop at breakeven fills
    assert h.broker.pos == {}


async def test_breakeven_engine_side_when_broker_cannot_modify(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 10.25)
    h.clock.advance(minutes=2)
    await h.engine.monitor_tick()
    assert db.get_position_meta("STK:F")["stop_loss"] == 10.01
    h.broker.set_price("F", 10.0)
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert h.broker.pos == {}


async def test_trailing_stop_never_moves_down(db, config) -> None:
    h = await harness(db, config, native=False, exits={"trail_atr_mult": 1.0})
    await enter_f(h)  # the scanner snapshot reports ATR 0.10
    h.broker.set_price("F", 10.40)  # bid 10.39
    h.clock.advance(minutes=2)
    await h.engine.monitor_tick()
    assert db.get_position_meta("STK:F")["stop_loss"] == pytest.approx(10.29)
    h.broker.set_price("F", 10.35)  # bid 10.34: lower high, stop stays
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert db.get_position_meta("STK:F")["stop_loss"] == pytest.approx(10.29)
    assert h.broker.pos


async def test_backup_stop_for_stock_without_working_broker_stop(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await h.broker.cancel_order(h.broker.working("STK:F", "stop")[0])
    h.broker.set_price("F", 9.78)  # bid 9.77: below the stop, but within 0.5%
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert h.broker.pos
    h.broker.set_price("F", 9.73)  # bid 9.72: more than 0.5% through the stop
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert h.broker.pos == {}
    assert h.orders()[0].request.reason.startswith("backup stop")


async def test_reconnect_replaces_missing_protective_stop(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await h.broker.cancel_order(h.broker.working("STK:F", "stop")[0])
    h.broker._connected = False
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    replaced = h.broker.placed[-1]
    assert replaced.side == Side.SELL and replaced.is_exit and replaced.stop_loss == 9.8
    assert replaced.qty == 24
    assert any(a["action"] == "reprotect" for a in db.list_audit())
    assert any(a["action"] == "broker_reconnected" for a in db.list_audit())


async def test_no_replacement_stop_without_broker_support(db, config) -> None:
    h = await harness(db, config)
    h.broker.standalone_stops = False
    await enter_f(h)
    await h.broker.cancel_order(h.broker.working("STK:F", "stop")[0])
    placed = len(h.broker.placed)
    h.broker._connected = False
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert len(h.broker.placed) == placed  # never a market sell disguised as a stop
    assert h.broker.pos
    assert "Unprotected F" in h.notes.titles()


async def test_flatten_stocks_before_close(db, config) -> None:
    h = await harness(db, config, exits={"time_stop_minutes": 0})
    await enter_f(h)
    h.clock.t = datetime(2026, 10, 21, 19, 49, tzinfo=UTC)  # 15:49 ET
    await h.engine.monitor_tick()
    assert h.broker.pos
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)  # 15:51 ET
    await h.engine.monitor_tick()
    assert h.broker.pos == {}
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert db.list_trades()[0].exit_reason == "flatten before close"


async def test_positions_marked_at_bid(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.broker.set_price("F", 10.40, spread=0.20)  # bid 10.30, last 10.40
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    pos = h.engine.snapshot()["positions"][0]
    assert pos.market_price == 10.30
    assert pos.unrealized_pnl == pytest.approx(24 * (10.30 - 10.01))
    assert pos.stop_loss == 9.8 and pos.setup == "vwap_reclaim"
    account = h.engine.snapshot()["account"]
    broker_equity = (await h.broker.account()).equity
    assert account.equity == pytest.approx(broker_equity - 24 * 0.10)


# =========================================================================== journal
def fill(
    exec_id: str, side: Side, qty: float, price: float, minute: int, order: str = "1", fee=1.0
) -> Fill:
    return Fill(
        exec_id=exec_id,
        broker_order_id=order,
        instrument=F,
        side=side,
        qty=qty,
        price=price,
        commission=fee,
        ts=NOW + timedelta(minutes=minute),
    )


def test_replay_fifo_matches_by_instrument_not_order_id() -> None:
    fills = [
        fill("a", Side.BUY, 10, 10.0, 0, "100"),
        fill("b", Side.BUY, 5, 11.0, 1, "100"),
        fill("c", Side.SELL, 8, 12.0, 2, "100-SL"),
        fill("d", Side.SELL, 7, 9.0, 3, "perm:555"),  # another client's order id
    ]
    trips, open_qty = replay_fills(fills)
    assert open_qty == 0 and len(trips) == 1
    trip = trips[0]
    assert trip.qty == 15 and trip.cost == 155 and trip.proceeds == 159
    assert trip.commission == pytest.approx(4.0)
    assert trip.entry_ts == NOW and trip.exit_ts == NOW + timedelta(minutes=3)


def test_replay_partial_exit_and_two_round_trips() -> None:
    trips, open_qty = replay_fills(
        [
            fill("a", Side.BUY, 10, 10.0, 0),
            fill("b", Side.SELL, 10, 11.0, 1),
            fill("c", Side.BUY, 4, 12.0, 2),
            fill("d", Side.SELL, 1, 12.5, 3),
        ]
    )
    assert len(trips) == 1 and trips[0].proceeds == 110
    assert open_qty == 3


def test_replay_ignores_empty_fills() -> None:
    trips, open_qty = replay_fills([fill("z", Side.BUY, 0, 10.0, 0), fill("a", Side.BUY, 2, 10, 1)])
    assert trips == [] and open_qty == 2


def test_replay_uses_fallback_entry_for_unmatched_sells() -> None:
    trips, _ = replay_fills([fill("x", Side.SELL, 5, 11.0, 1)], fallback_entry=(10.0, NOW))
    assert len(trips) == 1 and trips[0].cost == 50
    trips, _ = replay_fills([fill("x", Side.SELL, 5, 11.0, 1)])
    assert trips == []


async def test_late_commissions_update_fills_and_trade(db, config) -> None:
    h = await harness(db, config)
    h.broker.late_commission = True
    await enter_f(h)
    h.broker.set_price("F", 9.70)
    await h.engine.monitor_tick()
    assert db.list_trades() == []  # waiting for the commission reports
    h.broker.settle_commissions()
    h.clock.advance(seconds=30)
    await h.engine.monitor_tick()
    trade = db.list_trades()[0]
    assert trade.commission == pytest.approx(0.70)
    assert {f.commission for f in db.list_fills()} == {0.35}
    assert h.day().fees == pytest.approx(0.70)


async def test_late_commission_grace_expires(db, config) -> None:
    h = await harness(db, config)
    h.broker.late_commission = True
    await enter_f(h)
    h.broker.set_price("F", 9.70)
    await h.engine.monitor_tick()
    h.clock.advance(minutes=3)
    await h.engine.monitor_tick()
    assert len(db.list_trades()) == 1  # journaled without the missing commission


async def test_zero_equity_snapshot_never_halts(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)

    async def empty_account() -> AccountSnapshot:
        return AccountSnapshot(
            account_id="FAKE", mode=Mode.SIM, equity=0, cash=0, settled_cash=0, buying_power=0
        )

    h.broker.account = empty_account  # type: ignore[method-assign]
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert not h.day().halted and h.broker.pos
    assert "equity not available" in (h.engine.status().last_error or "")


async def test_equity_points_at_most_every_5_minutes(db, config) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    h.clock.advance(minutes=5)
    await h.engine.monitor_tick()
    assert len(db.list_equity()) == 2


async def test_day_rollover_at_new_york_midnight(db, config) -> None:
    h = await harness(db, config, now=datetime(2026, 10, 22, 3, 58, tzinfo=UTC))  # 23:58 ET
    await h.engine.monitor_tick()
    h.engine._day.loss_streak = 2
    h.engine._save_day()
    h.broker.cash = 520.0
    h.clock.advance(minutes=3)  # 00:01 ET
    await h.engine.monitor_tick()
    today = h.day()
    assert today.day == "2026-10-22"
    assert today.start_equity == 520.0 and today.loss_streak == 2
    assert db.get_day("2026-10-21").end_equity == 500.0


# =========================================================================== resilience
async def test_loop_survives_broker_exceptions_with_backoff(db, config) -> None:
    h = await harness(db, config)
    h.broker.fail = {"account"}
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 5:
            h.broker.fail.clear()
        if len(delays) >= 6:
            raise asyncio.CancelledError

    h.engine._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await h.engine._loop("monitor", h.engine.monitor_tick, lambda: 15)
    assert delays == [30, 60, 120, 120, 120, 15]
    assert h.engine.status().last_error is None  # cleared after the recovery


async def test_running_loops_keep_going_after_errors(db, config) -> None:
    h = await harness(db, config)
    h.broker.fail = {"positions"}
    ticks = 0

    async def fast_sleep(delay: float) -> None:
        nonlocal ticks
        ticks += 1
        await asyncio.sleep(0)

    h.engine._sleep = fast_sleep
    await h.engine.start()
    for _ in range(50):
        await asyncio.sleep(0)
    assert ticks > 3
    assert h.engine._monitor_task is not None and not h.engine._monitor_task.done()
    assert "positions failed" in (h.engine.status().last_error or "")
    await h.engine.shutdown()


async def test_disconnect_notifies_after_60s_and_on_recovery(db, config) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    h.broker._connected = False
    h.broker.can_connect = False
    with pytest.raises(BrokerError):
        await h.engine.monitor_tick()
    assert "Broker disconnected" not in h.notes.titles()
    h.clock.advance(seconds=61)
    with pytest.raises(BrokerError):
        await h.engine.monitor_tick()
    assert "Broker disconnected" in h.notes.titles()
    h.broker.can_connect = True
    h.clock.advance(seconds=30)
    await h.engine.monitor_tick()
    assert "Broker reconnected" in h.notes.titles()
    assert h.engine.status().connected


async def test_cycle_skipped_when_disconnected(db, config) -> None:
    h = await harness(db, config)
    h.broker._connected = False
    h.broker.can_connect = False
    with pytest.raises(BrokerError):
        await h.engine.run_cycle()
    assert not h.decider.calls


async def test_per_symbol_errors_are_tolerated(db, config) -> None:
    h = await harness(db, config, stock_universe=["F", "NODATA"])
    await h.engine.run_cycle()
    assert h.decider.calls and [r for r in h.broker.placed if r.side == Side.BUY]


async def test_snapshot_and_status_never_call_the_broker(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.fail = {"account", "positions", "open_orders", "quote", "fills_since"}
    snap = h.engine.snapshot()
    assert snap["account"].equity > 0 and len(snap["positions"]) == 1
    assert len(snap["open_orders"]) == 2  # bracket stop + target
    status = h.engine.status()
    assert status.trades_today == 1 and status.start_equity == 500.0
    assert status.mode == Mode.SIM and status.broker == "fake"


async def test_start_stop_persist_running_flag(db, config) -> None:
    h = await harness(db, config)

    async def idle_sleep(delay: float) -> None:
        await asyncio.sleep(3600)

    h.engine._sleep = idle_sleep
    await h.engine.start()
    await h.engine.start()  # idempotent
    assert db.get_flag("engine_running") and h.engine.status().running
    await h.engine.stop()
    assert not db.get_flag("engine_running") and not h.engine.status().running
    assert h.engine._monitor_task is not None and not h.engine._monitor_task.done()
    await h.engine.start()
    await h.engine.shutdown()
    assert db.get_flag("engine_running")  # a restart resumes the previous state
    assert h.engine._monitor_task is None and not h.broker.connected


async def test_reload_settings_applies_new_limits(db, config) -> None:
    h = await harness(db, config)
    settings = db.get_settings()
    settings.risk.max_open_positions = 1
    settings.autopilot = False
    db.save_settings(settings)
    h.engine.reload_settings()
    assert h.engine.risk.effective_limits().max_open_positions == 1
    assert h.engine.scanner.settings.autopilot is False
    assert h.engine.status().autopilot is False


# =========================================================================== wiring
def test_build_engine_sim_and_rules(db, config) -> None:
    from trader.broker.sim import SimBroker

    engine = build_engine(config, db)
    assert isinstance(engine.broker, SimBroker)
    assert isinstance(engine.decider, RuleBasedDecisionMaker)
    assert engine.rules is engine.decider


def test_build_engine_claude_and_ibkr(db, tmp_path) -> None:
    from trader.broker.ibkr import IBKRBroker
    from trader.strategy.claude import ClaudeDecisionMaker

    config = AppConfig(
        _env_file=None,
        data_dir=tmp_path,
        broker="ibkr",
        anthropic_api_key=SecretStr("sk-test-not-a-real-key"),
    )
    engine = build_engine(config, db)
    assert isinstance(engine.broker, IBKRBroker)
    assert isinstance(engine.decider, ClaudeDecisionMaker)
    assert engine.rules is engine.decider.fallback
    assert engine.shadow  # unknown IBKR account counts as live until connected


# =========================================================================== notifier
def notifier(handler, **cfg) -> Notifier:
    config = AppConfig(_env_file=None, ntfy_url="https://ntfy.example/topic-secret", **cfg)
    return Notifier(config, transport=httpx.MockTransport(handler))


async def test_notifier_headers_and_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    n = notifier(handler, ntfy_token=SecretStr("tk_secret"))
    assert await n.send("Trade closed: F", "P&L +1.00", priority="high", tags=["chart", "x"])
    req = seen[0]
    assert req.headers["Title"] == "Trade closed: F"
    assert req.headers["Priority"] == "high"
    assert req.headers["Tags"] == "chart,x"
    assert req.headers["Authorization"] == "Bearer tk_secret"
    assert req.content == b"P&L +1.00"


async def test_notifier_dedupes_titles_within_60s() -> None:
    calls = 0
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    config = AppConfig(_env_file=None, ntfy_url="https://ntfy.example/t")
    n = Notifier(config, transport=httpx.MockTransport(handler), clock=lambda: now[0])
    assert await n.send("Halt", "a")
    assert not await n.send("Halt", "b")
    assert await n.send("Other", "c")
    now[0] = 61.0
    assert await n.send("Halt", "d")
    assert calls == 3


async def test_notifier_never_raises_and_hides_secrets(caplog: pytest.LogCaptureFixture) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    n = notifier(handler, ntfy_token=SecretStr("tk_secret"))
    assert await n.send("Ünïcode\ntitle", "body") is False
    text = caplog.text
    assert "topic-secret" not in text and "tk_secret" not in text

    def bad_status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    assert await notifier(bad_status).send("x", "y") is False


async def test_notifier_disabled_without_url() -> None:
    n = Notifier(AppConfig(_env_file=None, ntfy_url=None))
    assert not n.enabled
    assert await n.send("t", "m") is False


async def test_notifier_sanitizes_header_values() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    assert await notifier(handler).send("Ünïcode\r\nInjected: yes", "m", priority="bogus")
    assert seen[0].headers["Title"] == "?n?code  Injected: yes"
    assert seen[0].headers["Priority"] == "default"
