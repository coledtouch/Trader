"""Engine tests on an in-memory FakeBroker with a fake decision maker, scanner and clock."""

from __future__ import annotations

import asyncio
import contextlib
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
from trader.research import ReplayFrame
from trader.strategy.decide import RuleBasedDecisionMaker
from trader.watchdog import HEARTBEAT_KEY

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
        self.day_trades: int | None = None  # DayTradesRemaining as IBKR last pushed it
        self.late_commission = False
        self.standalone_stops = True
        self.reject_exits = False  # refuse market exits (e.g. no liquidity, halted)
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
        # When set, quote() signals quote_started and waits on quote_release (race tests).
        self.quote_started: asyncio.Event | None = None
        self.quote_release: asyncio.Event | None = None
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
            day_trades_remaining=self.day_trades,
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
        if self.quote_started is not None:
            self.quote_started.set()
        if self.quote_release is not None:
            await self.quote_release.wait()
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
            if kind == "exit" and self.reject_exits:
                return OrderRecord(request=req, status=OrderStatus.REJECTED, message="no bid")
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

    def estimate_commission(
        self, instrument: Instrument, qty: float, price: float, side: Side = Side.BUY
    ) -> float:
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
    # Autopilot is off in the product. These tests opt in so an entry is sent on its own.
    base = {
        "stock_universe": ["F", "T"],
        "crypto_universe": ["BTC"],
        "trade_crypto": False,
        "autopilot": True,
    }
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


def live_config(tmp_path, **kw) -> AppConfig:
    """allow_live requires Cloudflare Access or AppConfig refuses to build."""
    return AppConfig(
        _env_file=None,
        data_dir=tmp_path,
        allow_live=True,
        cf_access_team_domain="team.cloudflareaccess.com",
        cf_access_aud="test-aud",
        **kw,
    )


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
    assert len(buys) == 1
    assert "never add" in db.list_decisions()[0].market_view
    assert h.decider.calls[-1].candidates == []  # owned-position exit review remains allowed
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


async def test_restarted_simulator_starts_a_fresh_journal_epoch(db, config) -> None:
    first = await harness(db, config)
    await enter_f(first)  # BUY F filled on the first simulated account, then the app restarts
    await first.engine.monitor_tick()
    stats = first.day()
    stats.start_equity = 450.0
    stats.halted, stats.halt_reason = True, "daily target reached: +$100.00 today"
    db.upsert_day(stats)
    stale_id = db.insert_order(
        OrderRecord(
            request=OrderRequest(
                instrument=Instrument(symbol="T"), side=Side.BUY, qty=1, limit_price=20.0
            ),
            status=OrderStatus.SUBMITTED,
            broker_order_id="old-1",
            created_at=NOW,
        )
    )

    second = await harness(db, config, now=NOW + timedelta(minutes=10))  # fresh $500 account
    second.broker._ids, second.broker._execs = itertools.count(100), itertools.count(100)
    await second.engine.monitor_tick()
    day = second.day()
    assert day.start_equity == 500.0  # today's P&L restarts with the new account
    assert not day.halted  # the old account's target no longer applies (a loss halt would)
    assert db.get_position_meta("STK:F") is None  # the old position no longer exists
    stale = db.get_order(stale_id)
    assert stale.status is OrderStatus.CANCELLED and stale.message == "simulator restarted"

    await enter_f(second)
    second.broker.set_price("F", 9.70)
    second.clock.advance(minutes=1)
    await second.engine.monitor_tick()
    [trade] = db.list_trades()  # matched against this account's BUY, not the old lot
    assert trade.qty == 24 and trade.entry_ts >= NOW + timedelta(minutes=10)
    assert trade.pnl == pytest.approx(24 * (9.69 - 10.01) - 0.70)


async def test_unaffordable_and_leveraged_symbols_skipped_before_decision(db, config) -> None:
    h = await harness(db, config, stock_universe=["F", "SPY", "TQQQ"])
    h.broker.set_price("SPY", 764.0)
    h.broker.set_price("TQQQ", 50.0)
    h.scanner.candidates["STK:SPY"] = [cand("SPY", 764, 760, 775)]
    h.scanner.candidates["STK:TQQQ"] = [cand("TQQQ", 50, 49, 53)]
    await h.engine.run_cycle()
    assert "STK:SPY" in h.scanner.scanned and "STK:TQQQ" not in h.scanner.scanned
    assert any(
        s.instrument.symbol == "SPY" and "size rounds to 0" in s.skip_reason
        for s in db.list_signals()
    )
    assert "STK:TQQQ" not in h.broker.quote_calls  # skipped without even a quote
    ctx = h.decider.calls[-1]
    assert {c.instrument.symbol for c in ctx.candidates} == {"F"}


async def test_halted_symbol_not_entered_and_cooldown(db, config) -> None:
    h = await harness(db, config)
    h.broker.set_price("F", 10.0, halted=True)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == [] and not h.decider.calls
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    assert "F is halted" in db.list_signals()[0].skip_reason
    [f] = [q for q in h.engine.status().quote_health if q.symbol == "F"]
    assert not f.valid and f.problem == "halted" and f.market_block

    h.broker.set_price("F", 10.0)  # reopened, but inside the 15 min cooldown
    h.clock.advance(minutes=5)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == []
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    # The dashboard says why, instead of calling the fresh quote usable.
    [f] = [q for q in h.engine.status().quote_health if q.symbol == "F"]
    assert not f.valid and f.problem == "halted 5 min ago (cooldown 15 min)"
    assert f.market_block  # the market's doing, not a data problem

    h.clock.advance(minutes=16)
    await h.engine.run_cycle()
    assert [r for r in h.broker.placed if r.side == Side.BUY]


async def test_stock_candidates_outside_entry_window_are_held_back(db, config) -> None:
    h = await harness(db, config, now=datetime(2026, 10, 21, 13, 35, tzinfo=UTC))  # 09:35 ET
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == [] and not h.decider.calls
    assert "outside the stock entry window 09:45-15:30 ET" in db.list_signals()[0].skip_reason


async def test_buy_for_non_candidate_is_ignored(db, config) -> None:
    h = await harness(db, config)
    h.decider.decisions = [buy("T")]
    await h.engine.run_cycle()
    assert not h.broker.placed


# ------------------------------------------------- fresh state when the BUY is sent
class SlowMaker(FakeDecisionMaker):
    """A decision maker that thinks for a while: `during` runs inside decide(), i.e. while
    the LLM call is in flight (the monitor loop keeps ticking meanwhile)."""

    def __init__(self, during) -> None:
        super().__init__()
        self.during = during

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        await self.during(ctx)
        return await super().decide(ctx)


async def test_entry_that_fills_during_the_llm_call_is_not_doubled(db, config) -> None:
    """ENG-1: the resting F entry fills while the models think and the monitor loop ticks.
    The context the models saw says F is not held, but the BUY is checked against the
    broker's state at send time, so no second bracket goes out."""
    h = await harness(db, config)
    h.decider.decisions = [buy(limit_price=9.99)]  # ask 10.01: the entry rests
    await h.engine.run_cycle()
    assert h.broker.working("STK:F", "entry")
    h.clock.advance(minutes=5)

    async def fill_and_tick(ctx: DecisionContext) -> None:
        assert not any(p.instrument.key == "STK:F" for p in ctx.positions)
        h.broker.set_price("F", 9.97)  # ask 9.98 <= 9.99: the entry fills
        h.clock.advance(seconds=30)
        await h.engine.monitor_tick()  # the independent monitor loop

    maker = SlowMaker(fill_and_tick)
    maker.decisions = [buy(limit_price=9.99)]
    h.engine._decider = maker
    await h.engine.run_cycle()
    assert len([r for r in h.broker.placed if r.side == Side.BUY]) == 1
    assert not h.broker.pos and not maker.calls
    blocked = h.orders(OrderStatus.BLOCKED)
    assert blocked == [] and "already working" in db.list_decisions()[0].market_view


async def test_order_approved_during_the_llm_call_is_not_doubled(db, config) -> None:
    """ENG-1: the owner approves an older pending F order while the models think (no
    monitor tick in between): the new F BUY sees the approved entry working."""
    h = await harness(db, config, autopilot=False)
    h.decider.decisions = [buy(limit_price=9.99)]
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    db.save_settings(db.get_settings().model_copy(update={"autopilot": True}))
    h.engine.reload_settings()
    h.clock.advance(minutes=2)

    async def approve(ctx: DecisionContext) -> None:
        await h.engine.approve_order(pending.id)

    maker = SlowMaker(approve)
    maker.decisions = [buy(limit_price=9.99)]
    h.engine._decider = maker
    await h.engine.run_cycle()
    assert len([r for r in h.broker.placed if r.side == Side.BUY]) == 0
    assert len(h.broker.working("STK:F", "entry")) == 0 and not maker.calls
    assert "already waiting for approval" in db.list_decisions()[0].market_view


async def test_entry_in_flight_blocks_a_second_entry(db, config) -> None:
    """ENG-1: position metadata is written before every send and kept while an entry may
    still open a position (e.g. the broker call failed with an unknown outcome): no second
    entry goes out for that symbol meanwhile."""
    h = await harness(db, config)
    h.broker.fail.add("place_order")
    await h.engine.run_cycle()  # outcome unknown: metadata kept
    assert db.get_position_meta("STK:F") is not None
    h.broker.fail.clear()
    h.clock.advance(seconds=30)  # within the metadata grace period
    await h.engine.run_cycle()
    assert not h.broker.placed
    assert "in flight or still held" in db.list_decisions()[0].market_view
    assert len(h.decider.calls) == 1


async def test_buy_is_checked_at_send_time_not_when_the_llm_was_asked(db, config) -> None:
    """ENG-2: the cycle starts at 15:29 ET, the models answer at 15:32 and F halted
    meanwhile. The entry window, the halt and the quote are judged at send time."""
    h = await harness(db, config, now=datetime(2026, 10, 21, 19, 29, tzinfo=UTC))

    async def slow_and_halted(ctx: DecisionContext) -> None:
        h.clock.advance(minutes=3)
        h.broker.set_price("F", 10.0, halted=True)

    maker = SlowMaker(slow_and_halted)
    maker.decisions = [buy()]
    h.engine._decider = maker
    await h.engine.run_cycle()
    assert not h.broker.placed
    [rec] = h.orders(OrderStatus.BLOCKED)
    assert "outside the stock entry window" in rec.message and "F is halted" in rec.message
    assert rec.created_at.replace(tzinfo=UTC) == h.clock()  # journaled at send time


async def test_buy_decided_on_too_old_data_is_dropped(db, config) -> None:
    """ENG-2: a decision that took longer than DECISION_MAX_AGE is not sent at all."""
    h = await harness(db, config)

    async def very_slow(ctx: DecisionContext) -> None:
        h.clock.advance(minutes=4)

    maker = SlowMaker(very_slow)
    maker.decisions = [buy()]
    h.engine._decider = maker
    await h.engine.run_cycle()
    assert not h.broker.placed
    assert "decision too old" in (db.list_signals()[0].skip_reason or "")


async def test_entry_uses_the_ask_at_send_time(db, config) -> None:
    """ENG-2: without a limit from the model, the entry is priced off the fresh quote."""
    h = await harness(db, config)

    async def moves(ctx: DecisionContext) -> None:
        h.clock.advance(seconds=40)
        h.broker.set_price("F", 10.04)  # ask 10.05

    maker = SlowMaker(moves)
    maker.decisions = [buy()]
    h.engine._decider = maker
    await h.engine.run_cycle()
    [sent] = [r for r in h.broker.placed if r.side == Side.BUY]
    assert sent.limit_price == 10.05


async def test_recent_day_trade_counts_before_ibkr_reports_it(db, config) -> None:
    """BRK-2: IBKR pushes DayTradesRemaining every few minutes. Right after the bot's own
    round trip closes, the stale value (1) must not allow a stock entry; once the refresh
    window has passed, IBKR's value is trusted again."""
    h = await harness(db, config)
    h.broker.day_trades = 3
    await enter_f(h)
    h.broker.set_price("F", 9.70)  # the bracket stop fills: a same-day round trip
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert db.list_trades()
    h.broker.day_trades = 1  # stale: IBKR has not counted the new day trade yet
    h.broker.set_price("T", 20.0)
    h.scanner.candidates = {"STK:T": [cand("T", price=20.0, stop=19.6, target=21.0)]}
    h.decider.decisions = [buy("T")]
    h.clock.advance(minutes=1)
    await h.engine.run_cycle()
    assert not [r for r in h.broker.placed if r.instrument.symbol == "T" and r.side == Side.BUY]
    assert "no day trades remaining" in db.list_decisions()[0].market_view
    h.clock.advance(minutes=5)  # IBKR's value is fresh again (still 1: one left)
    await h.engine.run_cycle()
    assert [r for r in h.broker.placed if r.instrument.symbol == "T" and r.side == Side.BUY]


# =========================================================================== halts
async def test_daily_loss_halts_and_flattens(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 8.50)  # equity at the bid ~ $463: -7.4%
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert not h.day().halted and h.broker.pos  # one tick is not enough
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # the breach held for two consecutive ticks
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


async def flat_for_a_while(h: H) -> None:
    """Monitor ticks over 11 quiet minutes: no position, order or fill."""
    await h.engine.monitor_tick()
    h.clock.advance(minutes=11)
    await h.engine.monitor_tick()


async def test_deposit_while_flat_is_not_profit(db, config) -> None:
    h = await harness(db, config)
    await flat_for_a_while(h)
    start = h.day().start_equity
    h.broker.cash += 150.0  # a deposit: more than the $100 daily target
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.day().start_equity == pytest.approx(start + 150.0)
    assert not h.day().halted, "a deposit must not count toward the daily target"
    assert h.engine.status().pnl_today == pytest.approx(0.0)
    [flow] = [a for a in db.list_audit() if a["action"] == "cash_flow"]
    assert flow["detail"]["amount"] == pytest.approx(150.0)
    assert "Balance change detected" in h.notes.titles()


async def test_paper_reset_while_flat_is_not_a_loss(db, config) -> None:
    h = await harness(db, config)
    await flat_for_a_while(h)
    h.broker.cash = 50.0  # e.g. a paper account reset to a smaller balance
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert not h.day().halted, "a reset must not trip the daily loss limit"
    assert h.day().start_equity == pytest.approx(50.0)
    curve = db.list_equity()
    assert curve and all(p["equity"] == pytest.approx(50.0) for p in curve)  # restarted


def _start_day_on_old_balance(h: H, start: float) -> None:
    """Today's start equity as recorded before a balance change the snapshot comparison
    never saw (e.g. a paper reset made before an upgrade), plus a resume baseline."""
    day = h.engine._today(h.clock())
    day.start_equity = start
    h.engine._save_day()
    h.db.set_kv("halt_baseline:" + day.day, repr(h.engine._account.equity))


async def test_missed_balance_change_on_an_untraded_day_is_rebased(db, config) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    _start_day_on_old_balance(h, 1_000_000.0)
    before = db.list_equity()
    assert h.engine.status().pnl_today < -999_000
    h.clock.advance(minutes=5)
    await h.engine.monitor_tick()
    assert h.day().start_equity == 1_000_000.0  # not yet: the account must be quiet first
    h.clock.advance(minutes=6)
    await h.engine.monitor_tick()
    day = h.day()
    assert day.start_equity == pytest.approx(500.0) and not day.halted
    assert float(db.get_kv("halt_baseline:" + day.day)) == pytest.approx(500.0)
    assert h.engine.status().pnl_today == pytest.approx(0.0)
    [flow] = [a for a in db.list_audit() if a["action"] == "cash_flow"]
    assert flow["detail"]["amount"] == pytest.approx(-999_500.0)
    assert flow["detail"]["source"] == "untraded day"
    assert "Balance change detected" in h.notes.titles()
    assert db.list_equity()[: len(before)] == before  # the curve had no jump: kept
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert len([a for a in db.list_audit() if a["action"] == "cash_flow"]) == 1


async def test_missed_balance_change_restarts_a_curve_that_shows_the_jump(db, config) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    _start_day_on_old_balance(h, 1_000_000.0)
    db.add_equity_point(1_000_000.0, ts=h.clock())
    h.clock.advance(minutes=11)
    await h.engine.monitor_tick()
    curve = db.list_equity()
    assert curve and all(p["equity"] == pytest.approx(500.0) for p in curve)


_TUE_11_NY = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
_WED_0005_NY = datetime(2026, 10, 21, 4, 5, tzinfo=UTC)


async def _overnight_stop_while_away(db, config) -> tuple[H, float]:
    """F held overnight with a broker stop; the broker becomes unreachable and the stop
    fills at the open, losing more than the daily limit. Returns (harness, start equity)."""
    h = await harness(
        db,
        config,
        mode=Mode.PAPER,
        now=_TUE_11_NY,
        flatten_stocks_before_close=False,
        exits={"time_stop_minutes": 0},
    )
    await enter_f(h)
    h.clock.t = _WED_0005_NY
    await h.engine.monitor_tick()  # today's start equity is recorded with F open
    assert h.broker.pos and h.day().trades == 0
    h.broker._connected = False
    h.broker.can_connect = False
    h.clock.t = datetime(2026, 10, 21, 13, 35, tzinfo=UTC)
    h.broker.set_price("F", 8.50)  # gaps through the stop
    assert h.broker.pos == {}
    return h, h.day().start_equity


async def _connect_late_and_run(h: H, engine: Engine, wait: timedelta) -> None:
    with pytest.raises(BrokerError):
        await engine.monitor_tick()
    h.clock.advance(seconds=wait.total_seconds())
    h.broker.can_connect = True
    for _ in range(60):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)


@pytest.mark.parametrize("wait", [timedelta(minutes=2), timedelta(minutes=12)])
async def test_stop_filled_while_the_app_was_restarting_still_halts(db, config, wait) -> None:
    """Review finding: with a slow gateway after a restart, the untraded-day check ran
    before the stop fill was journaled and booked the loss as a balance change."""
    h, start = await _overnight_stop_while_away(db, config)
    h.clock.t = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # the app starts again
    engine = Engine(
        config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock, scanner=h.scanner
    )
    await _connect_late_and_run(h, engine, wait)
    day = db.get_day("2026-10-21")
    assert day.start_equity == pytest.approx(start)
    assert day.halted and day.halt_reason.startswith("daily loss limit")
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]


async def test_stop_filled_during_a_long_disconnect_still_halts(db, config) -> None:
    h, start = await _overnight_stop_while_away(db, config)
    await _connect_late_and_run(h, h.engine, timedelta(minutes=12))
    day = h.day()
    assert day.start_equity == pytest.approx(start)
    assert day.halted and day.halt_reason.startswith("daily loss limit")
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]


async def test_small_unexplained_gap_on_an_untraded_day_still_counts(db, config) -> None:
    """Only a gap that looks like a reset or transfer (50%+) is rebased."""
    h = await harness(db, config)
    await h.engine.monitor_tick()
    _start_day_on_old_balance(h, 700.0)  # equity 500: a 29% gap
    h.clock.advance(minutes=11)
    await h.engine.monitor_tick()
    assert h.day().start_equity == 700.0
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]


async def test_a_real_loss_is_never_rebased_away(db, config) -> None:
    """A day with trades keeps its start equity however long the account then sits flat."""
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 9.0)
    for _ in range(2):  # the engine stop closes F at a loss
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    assert h.broker.pos == {}
    start = h.day().start_equity
    for _ in range(4):
        h.clock.advance(minutes=15)
        await h.engine.monitor_tick()
    assert h.day().start_equity == pytest.approx(start)
    assert h.engine.status().pnl_today < 0
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]


async def test_balance_change_soon_after_a_trade_still_counts(db, config) -> None:
    """IBKR's account values can lag a closed trade: a change within the quiet window of a
    fill is trading P&L and must still count toward the loss limit."""
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 9.0)
    for _ in range(2):  # the engine stop closes F at a loss
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    assert h.broker.pos == {}
    start = h.day().start_equity
    h.broker.cash -= 40.0  # a late loss adjustment shows up a few minutes later
    h.clock.advance(minutes=3)
    await h.engine.monitor_tick()
    assert h.day().start_equity == pytest.approx(start)
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]


async def test_balance_change_with_a_position_open_still_counts(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=11)
    await h.engine.monitor_tick()
    start = h.day().start_equity
    h.broker.cash += 150.0
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.day().start_equity == pytest.approx(start)
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]


async def test_resume_clears_halt_and_rebaselines(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 8.50)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)  # the next monitor tick
    await h.engine.monitor_tick()
    assert h.day().halted
    await h.engine.resume()
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert not h.day().halted
    assert h.engine.status().halted is False


async def test_status_reports_the_loss_limit_in_dollars(db, config) -> None:
    h = await harness(db, config, native=False)
    await h.engine.monitor_tick()
    status = h.engine.status()
    assert status.start_equity == 500.0 and status.loss_limit_usd == 30.0  # 6% of $500
    await enter_f(h)
    h.broker.set_price("F", 8.50)
    await h.engine.monitor_tick()
    await h.engine.resume()  # halts are now measured from the equity at the resume
    status = h.engine.status()
    floor = status.equity * 0.94
    assert status.loss_limit_usd == pytest.approx(500.0 - floor, abs=0.01)


async def test_target_reached_soft_halt_keeps_managing_stops(db, config) -> None:
    h = await harness(
        db, config, native=False, risk={"daily_profit_target_usd": 5}, exits={"breakeven_at_r": 0}
    )
    await enter_f(h)
    h.broker.set_price("F", 10.30)  # +$6.4 at the bid
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)  # the next monitor tick
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
    assert h.broker.pos  # an engine-side stop needs a second tick
    h.clock.advance(seconds=15)
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


async def test_kill_switch_with_refused_exit_restores_the_stop_and_alerts(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.reject_exits = True
    await h.engine.kill()
    assert h.day().halted and h.broker.pos  # still held: the broker refused the sell
    [stop] = h.broker.working("STK:F", "stop")  # protection re-placed after the cancel-all
    assert h.broker.orders[stop].request.stop_loss == 9.8
    assert "KILL switch FAILED" in h.notes.titles()
    assert "refused to close F" in (h.engine.status().last_error or "")
    rejected = [o for o in h.orders(OrderStatus.REJECTED) if o.request.side == Side.SELL]
    assert rejected and "protective stop re-placed at 9.8" in rejected[0].message


# =========================================================================== manual close
async def test_owner_closes_one_position_and_the_bot_stays_out_today(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    assert h.broker.working("STK:F", "stop") and h.broker.working("STK:F", "target")
    rec = await h.engine.close_position("STK:F")
    assert rec.request.side == Side.SELL and rec.request.is_exit
    assert rec.request.reason == "closed by the owner"
    assert h.broker.pos == {}
    assert not h.broker.working("STK:F", "stop") and not h.broker.working("STK:F", "target")
    sells = [o for o in h.orders() if o.request.side == Side.SELL]
    assert [o.request.reason for o in sells] == ["closed by the owner"]
    assert "Closing F" in h.notes.titles()
    # The scanner still proposes F and the model still says BUY: no new entry today.
    h.clock.advance(minutes=5)
    batch = await h.engine.run_cycle()
    assert batch is not None and "you closed F today" in batch.market_view
    assert len([r for r in h.broker.placed if r.side == Side.BUY]) == 1
    # Tomorrow the symbol is tradable again.
    h.clock.advance(days=1)
    await h.engine.run_cycle()
    assert len([r for r in h.broker.placed if r.side == Side.BUY]) == 2


async def test_buy_decided_before_a_manual_close_is_not_sent_after_it(db, config) -> None:
    """Send-time check: F was flat when the models were asked; meanwhile F was bought
    outside the bot and the owner closed it here. The BUY F decided on the older context
    must not go out after the close."""
    h = await harness(db, config)

    async def owner_buys_and_closes(ctx: DecisionContext) -> None:
        h.broker.pos["STK:F"] = [5.0, 10.0]
        await h.engine.close_position("STK:F")

    maker = SlowMaker(owner_buys_and_closes)
    maker.decisions = [buy()]
    h.engine._decider = maker
    await h.engine.run_cycle()
    assert maker.calls
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    assert "you closed it today" in [s.skip_reason for s in db.list_signals()]


async def test_monitor_exit_during_the_close_checks_is_not_doubled(db, config) -> None:
    """Review finding (regression): the TWS-order check awaits the broker between the
    "already closing" check and the stamp. A monitor exit (15:50 flatten, engine stop)
    starting in that gap must make the owner's close back off, not send a second sell."""
    h = await harness(db, config)
    await enter_f(h)
    real_open_orders = h.broker.open_orders
    calls = 0

    async def monitor_exit_meanwhile():
        nonlocal calls
        calls += 1
        if calls == 1:  # the owner's close is waiting on this broker call
            await h.engine._engine_close(Instrument(symbol="F"), "flatten before close", h.clock())
        return await real_open_orders()

    h.broker.open_orders = monitor_exit_meanwhile
    with pytest.raises(ValueError, match="already being closed"):
        await h.engine.close_position("STK:F")
    sells = [r for r in h.broker.placed if r.side == Side.SELL and r.order_type == OrderType.MARKET]
    assert len(sells) == 1 and sells[0].reason == "flatten before close"
    assert db.get_kv("owner_closed:STK:F") is None


async def _refused_then_owner_closes(h: H) -> None:
    """After a refused close nothing is selling: the owner's Close goes out at once."""
    assert h.broker.pos  # still held
    h.broker.reject_exits = False
    h.clock.advance(seconds=5)
    rec = await h.engine.close_position("STK:F")
    assert rec.status == OrderStatus.FILLED and h.broker.pos == {}


async def test_owner_close_after_a_refused_kill(db, config) -> None:
    """Review finding: a refused close elsewhere left its marker for 60 s, so Close said
    "already being closed" though nothing was selling."""
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.reject_exits = True
    await h.engine.kill()
    assert "KILL switch FAILED" in h.notes.titles()
    await _refused_then_owner_closes(h)


async def test_owner_close_after_a_refused_ai_close(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    h.broker.reject_exits = True
    await h.engine.run_cycle()
    await _refused_then_owner_closes(h)


async def test_owner_close_after_a_refused_flatten_before_close(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)  # 15:51 New York
    h.broker.reject_exits = True
    await h.engine.monitor_tick()
    await _refused_then_owner_closes(h)


async def test_a_close_that_was_taken_still_blocks_a_second_one(db, config) -> None:
    """The other side: a sell the broker took is in flight, so Close is refused."""
    h = await harness(db, config)
    await enter_f(h)
    await h.engine._engine_close(Instrument(symbol="F"), "time stop", h.clock())
    h.broker.pos["STK:F"] = [24.0, 10.01]  # the sell has not filled yet
    with pytest.raises(ValueError, match="already being closed"):
        await h.engine.close_position("STK:F")


class RestingBroker(FakeBroker):
    """Market sells for `rest` keys are accepted but do not fill yet (a halted stock, an
    order IBKR has not filled), with scripted open_orders failures and a gate that parks
    one close_position call and then fails it (a request timeout)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.rest: set[str] = set()
        self.open_orders_calls = 0
        self.fail_open_orders_on: set[int] = set()
        self.gate: asyncio.Event | None = None
        self.gate_key: str | None = None
        self.gate_entered = asyncio.Event()

    async def place_order(self, req: OrderRequest) -> OrderRecord:
        key = req.instrument.key
        if (
            req.side == Side.SELL
            and req.order_type == OrderType.MARKET
            and req.stop_loss is None
            and key in self.rest
            and req.qty <= self.pos.get(key, [0.0, 0.0])[0] + 1e-9
            and not self.reject_exits
        ):
            self._require()
            self.placed.append(req)
            oid = f"O{next(self._ids)}"
            self.orders[oid] = OrderRecord(
                request=req, status=OrderStatus.SUBMITTED, broker_order_id=oid
            )
            self.kind[oid], self.parent[oid] = "rest", None
            return self.orders[oid].model_copy()
        return await super().place_order(req)

    async def open_orders(self):
        self.open_orders_calls += 1
        if self.open_orders_calls in self.fail_open_orders_on:
            raise BrokerError("IBKR open orders timed out after 10s")
        return await super().open_orders()

    async def close_position(self, instrument, reason="", **kw):
        if self.gate is not None and instrument.key == self.gate_key:
            gate, self.gate = self.gate, None
            self.gate_entered.set()
            await gate.wait()
            raise BrokerError("IBKR open orders timed out after 10s")
        return await super().close_position(instrument, reason, **kw)


async def _resting(db, config) -> H:
    h = await harness(db, config)
    broker = RestingBroker(h.clock)
    broker.quotes = h.broker.quotes
    await broker.connect()
    h.engine.broker = broker
    h.broker = broker
    return h


def _market_sells(h: H, key: str) -> list[OrderRequest]:
    return [
        r
        for r in h.broker.placed
        if r.instrument.key == key
        and r.side == Side.SELL
        and r.order_type == OrderType.MARKET
        and r.stop_loss is None
    ]


async def _second_close_is_refused(h: H) -> None:
    h.clock.advance(seconds=5)
    with pytest.raises(ValueError, match="already being closed"):
        await h.engine.close_position("STK:F")
    assert len(_market_sells(h, "STK:F")) == 1


@pytest.mark.parametrize("via", ["kill", "loss_flatten"])
async def test_a_flatten_that_fails_partway_keeps_its_sent_sell_in_flight(db, config, via) -> None:
    """Review finding: flatten_all sent F's sell, then failed on T; every position was
    marked refused, so Close sent a second sell for F while the first was working."""
    h = await _resting(db, config)
    await enter_f(h)
    h.broker.pos["STK:T"] = [10.0, 20.0]
    await h.engine.monitor_tick()
    h.broker.rest = {"STK:F", "STK:T"}
    h.broker.open_orders_calls = 0
    # flatten_all: #1 its own, F: #2 cancels and #3 the last check before the sell,
    # T: #4 times out
    h.broker.fail_open_orders_on = {4}
    if via == "kill":
        await h.engine.kill()
        assert "KILL switch FAILED" in h.notes.titles()
    else:
        with pytest.raises(BrokerError):
            await h.engine._flatten_all("daily loss limit", h.clock())
    assert len(_market_sells(h, "STK:F")) == 1
    await _second_close_is_refused(h)


async def test_an_older_close_failing_after_a_newer_one_sent_is_ignored(db, config) -> None:
    """Review finding: a monitor exit failed after KILL had stamped F and sent its sell;
    the stale failure marked F refused while the kill's sell was working."""
    h = await _resting(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.rest = {"STK:F"}
    gate = asyncio.Event()
    h.broker.gate, h.broker.gate_key = gate, "STK:F"
    monitor = asyncio.create_task(
        h.engine._engine_close(Instrument(symbol="F"), "time stop", h.clock())
    )
    await h.broker.gate_entered.wait()
    await h.engine.kill()
    gate.set()  # the monitor's broker call now fails
    with pytest.raises(BrokerError):
        await monitor
    assert "STK:F" not in h.engine._close_refused
    await _second_close_is_refused(h)


async def test_an_ai_close_failing_after_the_kill_stamped_is_ignored(db, config) -> None:
    h = await _resting(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    h.broker.rest = {"STK:F"}
    gate = asyncio.Event()
    h.broker.gate, h.broker.gate_key = gate, "STK:F"
    cycle = asyncio.create_task(h.engine.run_cycle())
    await h.broker.gate_entered.wait()  # the AI close holds the entry lock
    kill = asyncio.create_task(h.engine.kill())
    await asyncio.sleep(0)  # kill stamps F and waits for the lock
    gate.set()
    with contextlib.suppress(BrokerError):
        await cycle
    await kill
    assert len(_market_sells(h, "STK:F")) == 1
    await _second_close_is_refused(h)


def _working_market_sells(h: H, key: str) -> list[OrderRecord]:
    return [
        o
        for o in h.broker.orders.values()
        if o.request.instrument.key == key
        and o.status == OrderStatus.SUBMITTED
        and o.request.side == Side.SELL
        and o.request.stop_loss is None
        and o.request.order_type == OrderType.MARKET
    ]


async def _pending_ai_close(db, config) -> tuple[H, OrderRecord]:
    h = await _resting(db, config)
    h.engine.settings.exits.time_stop_minutes = 0
    await enter_f(h)
    await h.engine.monitor_tick()
    h.engine.settings.autopilot = False
    h.clock.t = datetime(2026, 10, 21, 19, 45, tzinfo=UTC)  # 15:45 New York
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    [waiting] = h.orders(OrderStatus.PENDING_APPROVAL)
    h.broker.rest = {"STK:F"}  # market sells are accepted but have not filled yet
    return h, waiting


def _slow_first_cancel(h: H) -> tuple[asyncio.Event, asyncio.Event]:
    """The first cancel waits, as IBKR's cancel confirmation can (up to seconds)."""
    gate, entered = asyncio.Event(), asyncio.Event()
    real_cancel = h.broker.cancel_order
    armed = [True]

    async def slow_cancel(oid):
        if armed[0]:
            armed[0] = False
            entered.set()
            await gate.wait()
        return await real_cancel(oid)

    h.broker.cancel_order = slow_cancel
    return gate, entered


async def test_flatten_during_an_approved_close_sells_once(db, config) -> None:
    """Review finding: approve sent its close without checking or stamping the marker
    first, so the 15:50 flatten running meanwhile sent a second full-size sell."""
    h, waiting = await _pending_ai_close(db, config)
    gate, entered = _slow_first_cancel(h)
    approve = asyncio.create_task(h.engine.approve_order(waiting.id))
    await entered.wait()
    h.clock.t = datetime(2026, 10, 21, 19, 50, 1, tzinfo=UTC)  # 15:50:01 New York
    tick = asyncio.create_task(h.engine.monitor_tick())  # flatten before close
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await approve
    await tick
    assert len(_working_market_sells(h, "STK:F")) == 1


async def test_approve_during_a_monitor_exit_is_cancelled(db, config) -> None:
    h, waiting = await _pending_ai_close(db, config)
    gate, entered = _slow_first_cancel(h)
    h.clock.t = datetime(2026, 10, 21, 19, 50, 30, tzinfo=UTC)
    tick = asyncio.create_task(h.engine.monitor_tick())
    await entered.wait()
    rec = await h.engine.approve_order(waiting.id)
    assert rec.status == OrderStatus.CANCELLED and rec.message == "already being closed"
    gate.set()
    await tick
    assert len(_working_market_sells(h, "STK:F")) == 1


async def test_kill_during_a_monitor_exit_sells_once(db, config) -> None:
    """KILL sold F while the monitor's exit waited on a cancel; the monitor's close must
    see that sell before sending its own (the broker's last check before the sell)."""
    h = await _resting(db, config)
    h.engine.settings.exits.time_stop_minutes = 0
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.rest = {"STK:F"}
    gate, entered = _slow_first_cancel(h)
    monitor = asyncio.create_task(
        h.engine._engine_close(Instrument(symbol="F"), "time stop", h.clock())
    )
    await entered.wait()
    kill = asyncio.create_task(h.engine.kill())  # its close of F waits for the monitor's
    for _ in range(20):
        await asyncio.sleep(0)
    gate.set()
    await monitor
    await kill
    assert len(_working_market_sells(h, "STK:F")) == 1


async def test_kill_that_fails_on_one_position_still_closes_and_journals_the_rest(
    db, config
) -> None:
    """Review finding: KILL stopped at the first position whose close failed, leaving the
    others unsold and (after the cancel-all) without their stops; what it had sent was
    not journaled."""
    h = await _resting(db, config)
    await enter_f(h)
    h.broker.pos["STK:T"] = [10.0, 20.0]
    await h.engine.monitor_tick()
    h.broker.rest = {"STK:F", "STK:T"}
    h.broker.open_orders_calls = 0
    # flatten_all: #1 its own read; F: #2 cancels, #3 its last check times out
    h.broker.fail_open_orders_on = {3}
    await h.engine.kill()
    assert "KILL switch FAILED" in h.notes.titles()
    assert h.broker.working("STK:F", "stop")  # F's stop put back
    assert len(_market_sells(h, "STK:T")) == 1  # T still sold
    journaled = [o.request.instrument.key for o in h.orders() if o.request.side == Side.SELL]
    assert "STK:T" in journaled


async def test_a_failed_check_puts_no_stop_next_to_a_kill_sell(db, config) -> None:
    """Review finding: a monitor exit's last check failed after KILL had sent its market
    sell; putting the stop back left a stop and a market sell on one position."""
    h = await _resting(db, config)
    h.engine.settings.exits.time_stop_minutes = 0
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.rest = {"STK:F"}
    gate, entered = _slow_first_cancel(h)
    monitor = asyncio.create_task(
        h.engine._engine_close(Instrument(symbol="F"), "time stop", h.clock())
    )
    await entered.wait()
    kill = asyncio.create_task(h.engine.kill())  # its close of F waits for the monitor's
    for _ in range(20):
        await asyncio.sleep(0)
    h.broker.fail_open_orders_on = {h.broker.open_orders_calls + 1}  # the monitor's check
    gate.set()
    with pytest.raises(BrokerError):
        await monitor
    await kill
    # The monitor put the stop back; KILL's close then cancelled it and sold once.
    assert len(_working_market_sells(h, "STK:F")) == 1
    assert h.broker.working("STK:F", "stop") == []


async def test_two_failed_closes_put_back_only_one_stop(db, config) -> None:
    """Review finding: KILL during a monitor exit, then IBKR stalls (every open-orders read
    times out): both closes put a full-size stop back, so two stops could sell twice the
    position. Closes of one instrument now run one at a time, and a stop already put back
    counts as cover."""
    h = await _resting(db, config)
    h.engine.settings.exits.time_stop_minutes = 0
    await enter_f(h)
    await h.engine.monitor_tick()
    gate, entered = _slow_first_cancel(h)
    monitor = asyncio.create_task(
        h.engine._engine_close(Instrument(symbol="F"), "time stop", h.clock())
    )
    await entered.wait()
    kill = asyncio.create_task(h.engine.kill())
    for _ in range(20):
        await asyncio.sleep(0)  # KILL has cancelled everything; its close of F waits
    n = h.broker.open_orders_calls
    h.broker.fail_open_orders_on = set(range(n + 1, n + 50))  # the gateway stalls
    gate.set()
    with pytest.raises(BrokerError):
        await monitor
    await kill
    assert "KILL switch FAILED" in h.notes.titles()
    assert len(h.broker.working("STK:F", "stop")) == 1
    assert _working_market_sells(h, "STK:F") == []


async def test_a_close_cancels_a_stop_put_back_after_its_own_cancels(db, config) -> None:
    """The other ordering: a concurrent close put its stop back between this close's
    cancels and its last check; this close's market sell must not sit next to it."""
    h = await _resting(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.rest = {"STK:F"}
    real = h.broker.open_orders
    calls = 0

    async def stop_put_back_before_the_last_check():
        nonlocal calls
        calls += 1
        if calls == 2:  # the last check: another close has just re-placed its stop
            stop = OrderRequest(
                instrument=Instrument(symbol="F"),
                side=Side.SELL,
                qty=24,
                order_type=OrderType.MARKET,
                stop_loss=9.8,
                is_exit=True,
                reason="protective stop (exit check failed)",
            )
            await h.broker.place_order(stop)
        return await real()

    h.broker.open_orders = stop_put_back_before_the_last_check
    rec = await h.broker.close_position(Instrument(symbol="F"), "time stop")
    assert rec is not None and rec.status == OrderStatus.SUBMITTED
    assert h.broker.working("STK:F", "stop") == []
    assert len(_working_market_sells(h, "STK:F")) == 1


async def test_shadow_loss_flatten_carries_on_and_journals_what_it_sent(db, tmp_path) -> None:
    """Review finding: the shadow-mode flatten stopped at the first failed close and never
    journaled the sells it had already sent."""
    h = await disarmed_with_bot_position(db, tmp_path)
    h.broker.pos["STK:T"] = [5.0, 20.0]
    db.set_position_meta(
        "STK:T", stop_loss=19.0, take_profit=None, entry_price=20.0, initial_risk=5.0,
        opened_at=h.clock(), decision_id=None, setup=None,
    )  # fmt: skip
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    real = h.broker.open_orders
    calls = 0

    async def fails_on_the_first_close():
        nonlocal calls
        calls += 1
        if calls == 1:  # the first position's first read
            raise BrokerError("IBKR open orders timed out after 10s")
        return await real()

    h.broker.open_orders = fails_on_the_first_close
    before = {o.id for o in h.orders()}
    with pytest.raises(BrokerError, match="timed out"):
        await h.engine._flatten_engine_positions("daily loss limit", h.clock())
    new = [o for o in h.orders() if o.id not in before and o.request.side == Side.SELL]
    assert len(new) == 1  # the second position was still closed and journaled
    assert len(h.broker.pos) == 1  # the failed one is still held


async def test_partial_kill_alert_names_the_refused_positions(db, config) -> None:
    """Review finding: the alert for a partly failed KILL only named the failure, not the
    positions the broker refused to close (left unsold, maybe without a stop)."""
    h = await _resting(db, config)
    await enter_f(h)
    h.broker.pos["STK:T"] = [10.0, 20.0]
    await h.engine.monitor_tick()
    h.broker.reject_exits = True
    h.broker.open_orders_calls = 0
    h.broker.fail_open_orders_on = {5}  # F: #2, #3 (sell refused); T: #4, #5 times out
    await h.engine.kill()
    [(_, body, _)] = [n for n in h.notes.sent if n[0] == "KILL switch FAILED"]
    assert "refused to close F" in body and "T:" in body


async def test_manual_close_refusals(db, config) -> None:
    h = await harness(db, config)
    with pytest.raises(LookupError, match="no open F position"):
        await h.engine.close_position("STK:F")
    with pytest.raises(LookupError, match="unknown instrument"):
        await h.engine.close_position("nonsense")
    await enter_f(h)
    await h.engine.close_position("STK:F")
    h.broker.pos["STK:F"] = [5.0, 10.0]  # bought back elsewhere a moment later
    with pytest.raises(ValueError, match="already being closed"):
        await h.engine.close_position("STK:F")  # a double click sells once
    h.clock.advance(hours=7)  # 18:00 New York
    with pytest.raises(ValueError, match="stock market is closed"):
        await h.engine.close_position("STK:F")


async def test_manual_close_of_the_owners_own_position(db, config) -> None:
    """The bot never manages positions the owner opened, but the owner may close them."""
    h = await harness(db, config)
    h.broker.pos["STK:T"] = [3.0, 19.0]
    await h.engine.monitor_tick()
    assert db.get_position_meta("STK:T") is None
    await h.engine.close_position("STK:T")
    assert "STK:T" not in h.broker.pos


async def test_manual_close_cancels_a_waiting_close_approval(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.engine.settings.autopilot = False
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    [waiting] = h.orders(OrderStatus.PENDING_APPROVAL)
    await h.engine.close_position("STK:F")
    assert h.db.get_order(waiting.id).status == OrderStatus.CANCELLED
    with pytest.raises(ValueError):
        await h.engine.approve_order(waiting.id)


async def test_refused_manual_close_restores_the_stop_and_alerts(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker.reject_exits = True
    with pytest.raises(BrokerError, match="refused to close F"):
        await h.engine.close_position("STK:F")
    assert h.broker.pos  # still held
    [stop] = h.broker.working("STK:F", "stop")
    assert h.broker.orders[stop].request.stop_loss == 9.8
    assert "Close F FAILED" in h.notes.titles()
    assert not h.engine._closing_now("STK:F", h.clock())  # the monitor manages it again
    assert db.get_kv("owner_closed:STK:F") is None  # not closed: entries not blocked
    h.broker.reject_exits = False
    await h.engine.close_position("STK:F")  # the owner can try again at once
    assert h.broker.pos == {}


def _tws_stop(qty: float, price: float):
    """A GTC sell stop the owner placed in TWS or IBKR Mobile (another API client)."""
    from ib_async import Order, Trade
    from ib_async import OrderStatus as IBOrderStatus

    from tests.test_ibkr import PAPER, stock

    order = Order(
        orderId=0, clientId=0, permId=555, action="SELL", totalQuantity=qty,
        orderType="STP", auxPrice=price, tif="GTC", account=PAPER,
    )  # fmt: skip
    return Trade(stock(), order, IBOrderStatus(status="Submitted"))


async def test_close_refused_while_an_order_from_tws_would_outlive_it(db, config) -> None:
    """Review finding: the bot cannot cancel another client's order, so the close sold the
    shares and left the owner's TWS stop working on a flat account (a short if it fired).
    Now nothing is sent until the owner cancels it there."""
    from tests.test_ibkr import connected_broker, hold, stock

    broker, fake = await connected_broker()
    hold(fake, stock(), 3)
    fake.all_open = [_tws_stop(3, 95.0)]
    notes = RecordingNotifier()
    engine = Engine(config, db, broker, FakeDecisionMaker(), notifier=notes, clock=FakeClock(NOW))
    with pytest.raises(ValueError, match="placed in TWS or IBKR Mobile"):
        await engine.close_position("STK:AAPL")
    assert fake.placed == [] and fake.cancelled == []
    assert not engine._closing_now("STK:AAPL", NOW)
    assert db.get_kv("owner_closed:STK:AAPL") is None
    fake.all_open = []  # cancelled in TWS: now it closes
    rec = await engine.close_position("STK:AAPL")
    assert rec.request.side == Side.SELL and rec.request.qty == 3


async def test_refused_close_never_puts_back_another_clients_stop(db, config) -> None:
    """Base close_position: a stop it could not cancel is not "restored" as a second one."""
    from tests.test_ibkr import connected_broker, hold, log_error, stock

    broker, fake = await connected_broker()
    hold(fake, stock(), 3)
    fake.all_open = [_tws_stop(3, 95.0)]

    def reject_market(trade) -> None:
        if trade.order.orderType == "MKT":
            trade.orderStatus.status = "Cancelled"
            log_error(trade, 201, "Order rejected - reason: test")

    fake.on_place = reject_market
    rec = await broker.close_position(Instrument(symbol="AAPL"))
    assert rec is not None and rec.status == OrderStatus.REJECTED
    assert [o.orderType for _, o in fake.placed] == ["MKT"]  # no second stop
    assert "protective stop" not in (rec.message or "")


async def test_refused_close_alert_says_when_no_stop_was_put_back(db, config) -> None:
    """Review finding: the alert always claimed "The stop was put back"."""
    h = await harness(db, config, native=False)  # engine-managed stops: nothing at IBKR
    await enter_f(h)
    h.broker.reject_exits = True
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    [(title, body, priority)] = [n for n in h.notes.sent if n[0] == "Close F FAILED"]
    assert "No stop is working at IBKR" in body and "put back" not in body
    assert priority == "urgent"


async def test_close_click_during_an_ai_close_after_a_long_think_sells_once(db, config) -> None:
    """Review finding: the AI close stamped its marker with the context time (minutes old
    after a long LLM call) and sent outside the entry lock, so a Close pressed meanwhile
    sent a second full-size market sell."""
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=5)
    gate = asyncio.Event()
    entered = asyncio.Event()
    real_open_orders = h.broker.open_orders

    async def slow_open_orders():
        if not entered.is_set():
            entered.set()
            await gate.wait()  # the AI close is mid-flight at the broker
        return await real_open_orders()

    async def think(ctx: DecisionContext) -> None:
        h.clock.advance(seconds=90)
        h.broker.open_orders = slow_open_orders  # the AI close's broker call will stall

    maker = SlowMaker(think)
    maker.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    h.engine._decider = maker
    cycle = asyncio.create_task(h.engine.run_cycle())
    await entered.wait()
    owner = asyncio.create_task(h.engine.close_position("STK:F"))
    await asyncio.sleep(0)
    gate.set()
    await cycle
    with pytest.raises((ValueError, LookupError)):
        await owner
    sells = [r for r in h.broker.placed if r.side == Side.SELL and r.order_type == OrderType.MARKET]
    assert len(sells) == 1


async def test_two_looks_at_one_bad_equity_print_do_not_halt(db, config) -> None:
    """ENG-5: run_cycle runs its own monitor tick right after the monitor loop's. A single
    bad equity snapshot seen by both at the same moment is not a confirmed loss halt."""
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=2)
    await h.engine.monitor_tick()
    h.broker.cash -= 40  # one bad snapshot: -8%, past the 6% loss limit
    await h.engine.monitor_tick()  # the monitor loop
    assert await h.engine.run_cycle() is None  # same instant: pending, no decisions
    assert not h.day().halted and h.broker.pos["STK:F"][0] == 24
    h.broker.cash += 40  # the next snapshot is fine
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert not h.day().halted and h.engine._halt_pending is None
    # A loss that is still there one monitor interval later is confirmed as before.
    h.broker.cash -= 40
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.day().halted and h.day().halt_reason.startswith("daily loss limit")


async def test_engine_stop_needs_two_ticks_apart(db, config) -> None:
    """ENG-5: one bad bid looked at twice in the same instant does not fire an engine-side
    stop; the next monitor tick does."""
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.set_price("F", 9.70)  # bid 9.69 below the 9.80 stop
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    await h.engine.monitor_tick()
    assert h.broker.pos
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.broker.pos == {}


async def test_refused_flatten_after_loss_halt_alerts(db, config) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    h.broker.reject_exits = True
    h.broker.set_price("F", 8.50)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)  # the next monitor tick
    await h.engine.monitor_tick()
    assert h.day().halted and h.day().halt_reason.startswith("daily loss limit")
    assert "Flatten FAILED" in h.notes.titles()
    assert "refused to close F" in (h.engine.status().last_error or "")


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
    config = live_config(tmp_path)
    h = await harness(db, config, mode=Mode.LIVE)
    assert h.engine.shadow
    await h.engine.arm_live()
    assert not h.engine.shadow
    await h.engine.run_cycle()
    assert [r for r in h.broker.placed if r.side == Side.BUY]
    await h.engine.disarm_live()
    assert h.engine.shadow


async def disarmed_with_bot_position(db, tmp_path, **kw) -> H:
    """Live account, armed, the bot opens F, then the owner disarms (shadow mode)."""
    config = live_config(tmp_path)
    h = await harness(db, config, mode=Mode.LIVE, **kw)
    await h.engine.arm_live()
    await enter_f(h)
    assert "STK:F" in h.broker.pos
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # the position cache now shows F
    await h.engine.disarm_live()
    assert h.engine.shadow
    return h


async def test_disarmed_live_keeps_exiting_bot_positions_but_never_enters(db, tmp_path) -> None:
    """Owner decision: disarming blocks new entries, but the bot keeps closing the positions
    it opened (exits only reduce risk)."""
    h = await disarmed_with_bot_position(db, tmp_path)
    [message] = [m for t, m, _ in h.notes.sent if t == "Live trading disarmed"]
    assert "No new entries" in message
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)  # the next monitor tick confirms the time stop
    await h.engine.monitor_tick()
    assert h.broker.pos == {}, "the time stop must still close the bot's position"
    sent = len(h.broker.placed)
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert not [r for r in h.broker.placed[sent:] if r.side == Side.BUY]
    assert h.orders()[0].status == OrderStatus.BLOCKED
    assert "shadow mode" in h.orders()[0].message


async def test_disarmed_loss_flatten_closes_only_bot_positions(db, tmp_path) -> None:
    h = await disarmed_with_bot_position(db, tmp_path, native=False)
    h.broker.pos["STK:T"] = [5.0, 20.0]  # the owner's own shares: the bot never opened them
    h.broker.cash -= 100.0  # same equity, so only F's drop moves the day's P&L
    h.broker.set_price("F", 8.50)
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.day().halted and h.day().halt_reason.startswith("daily loss limit")
    assert "STK:F" not in h.broker.pos
    assert h.broker.pos["STK:T"][0] == 5.0, "the owner's position must not be touched"
    [detail] = [m for t, m, _ in h.notes.sent if t == "Trading halted"]
    assert "bot position(s) only" in detail
    flatten = [a for a in db.list_audit() if a["action"] == "flatten"]
    assert flatten and flatten[0]["detail"]["scope"] == "bot"


async def test_disarmed_ai_close_and_approved_close_are_sent(db, tmp_path) -> None:
    h = await disarmed_with_bot_position(db, tmp_path)
    settings = db.get_settings()
    settings.autopilot = False
    db.save_settings(settings)
    h.engine.reload_settings()
    h.decider.decisions = [
        AIDecision(
            action=TradeAction.CLOSE,
            symbol="F",
            asset_class=AssetClass.STOCK,
            conviction=0.7,
            rationale="setup failed",
        )
    ]
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    assert pending.request.is_exit
    rec = await h.engine.approve_order(pending.id)
    assert rec.status != OrderStatus.BLOCKED
    assert h.broker.pos == {}


async def test_closing_the_server_gate_clears_the_arm(db, tmp_path) -> None:
    """TRADER_ALLOW_LIVE true -> false -> true across restarts (the documented lockdown):
    the arm does not survive, so the bot stays in shadow mode until a fresh Arm."""
    on = live_config(tmp_path)
    off = AppConfig(_env_file=None, data_dir=tmp_path, allow_live=False)
    h = await harness(db, on, mode=Mode.LIVE)
    await h.engine.arm_live()
    assert not h.engine.shadow

    def restart(config: AppConfig) -> Engine:
        return Engine(
            config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock, scanner=h.scanner
        )

    locked = restart(off)
    assert locked.shadow and not db.get_flag("live_armed")
    [audit] = [a for a in db.list_audit() if a["action"] == "live_disarmed"]
    assert audit["actor"] == "engine" and "TRADER_ALLOW_LIVE=false" in audit["detail"]["reason"]

    reopened = restart(on)
    assert reopened.shadow and reopened.status().live_armed is False
    await reopened.run_cycle()
    assert not h.broker.placed, "no real order without a fresh arm"
    assert h.orders()[0].status == OrderStatus.BLOCKED
    await reopened.arm_live()  # the web layer verifies a fresh 2FA code first
    assert not reopened.shadow
    # An arm survives a restart only while the gate stays open.
    assert not restart(on).shadow


async def test_arm_live_refused_for_paper_account(db, tmp_path) -> None:
    config = live_config(tmp_path)
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


async def test_late_approval_gets_the_full_entry_ttl(db, config) -> None:
    """ENG-6: an entry approved 9.5 minutes after it was proposed rests for the full 10
    minutes from the approval, not 30 seconds."""
    h = await harness(db, config, autopilot=False)
    h.decider.decisions = [buy(limit_price=9.99)]  # ask 10.01: rests after approval
    await h.engine.run_cycle()
    [rec] = h.orders(OrderStatus.PENDING_APPROVAL)
    h.clock.advance(minutes=9, seconds=30)
    assert (await h.engine.approve_order(rec.id)).status == OrderStatus.SUBMITTED
    h.clock.advance(seconds=30)
    await h.engine.monitor_tick()
    assert db.get_order(rec.id).status == OrderStatus.SUBMITTED
    assert h.broker.working("STK:F", "entry")
    h.clock.advance(minutes=9, seconds=31)  # 10 min after the approval
    await h.engine.monitor_tick()
    final = db.get_order(rec.id)
    assert final.status == OrderStatus.CANCELLED and "not filled within 10 min" in final.message


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


def test_autopilot_defaults_to_manual_approval() -> None:
    assert BotSettings().autopilot is False


async def test_approved_order_is_tagged_with_its_journal_id(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    approved = await h.engine.approve_order(rec.id)
    assert approved.request.trace_id == f"ai-trader-{rec.id}"
    assert h.broker.placed[-1].trace_id == f"ai-trader-{rec.id}"


async def test_reject_during_approval_price_fetch_does_not_send(db, config) -> None:
    """Approve then Reject while quotes are loading must not send, and must not
    overwrite the cancelled status."""
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    rec = h.orders(OrderStatus.PENDING_APPROVAL)[0]
    started = asyncio.Event()
    release = asyncio.Event()
    h.broker.quote_started = started
    h.broker.quote_release = release

    approve_task = asyncio.create_task(h.engine.approve_order(rec.id))
    await asyncio.wait_for(started.wait(), timeout=2)
    rejected = await h.engine.reject_order(rec.id)
    assert rejected.status == OrderStatus.CANCELLED
    assert rejected.message == "rejected by owner"
    release.set()
    with pytest.raises(ValueError, match="not pending"):
        await approve_task
    saved = db.get_order(rec.id)
    assert saved is not None
    assert saved.status == OrderStatus.CANCELLED
    assert saved.message == "rejected by owner"
    assert not h.broker.placed


async def test_restarted_order_is_reconciled_by_its_trace_id(db, config) -> None:
    h = await harness(db, config)
    h.decider.decisions = [buy(limit_price=9.99)]  # ask is 10.01: the entry rests
    await h.engine.run_cycle()
    [rec] = h.orders(OrderStatus.SUBMITTED)
    assert rec.request.trace_id and rec.id is not None
    db.update_order(rec.id, broker_order_id="GONE")
    revived = rec.model_copy(
        update={"broker_order_id": "RESTARTED-1", "status": OrderStatus.SUBMITTED}
    )

    async def order_status(broker_order_id: str) -> OrderRecord | None:
        return None

    async def open_orders() -> list[OrderRecord]:
        return [revived]

    h.broker.order_status = order_status  # type: ignore[method-assign]
    h.broker.open_orders = open_orders  # type: ignore[method-assign]
    await h.engine._sync_orders(h.clock())
    saved = db.get_order(rec.id)
    assert saved is not None
    assert saved.broker_order_id == "RESTARTED-1"
    assert saved.status == OrderStatus.SUBMITTED


async def test_watchdog_tag_is_not_adopted_as_an_app_order(db, config) -> None:
    """ai-trader-wd-... belongs to the crypto watchdog, not this process."""
    h = await harness(db, config)
    h.decider.decisions = [buy(limit_price=9.99)]
    await h.engine.run_cycle()
    [rec] = h.orders(OrderStatus.SUBMITTED)
    assert rec.id is not None
    db.update_order(rec.id, broker_order_id="GONE")
    tagged = rec.request.model_copy(update={"trace_id": "ai-trader-wd-BTC-stop-abc"})
    db.update_order(rec.id, request=tagged)
    revived = rec.model_copy(
        update={
            "request": tagged,
            "broker_order_id": "WD-1",
            "status": OrderStatus.SUBMITTED,
        }
    )

    async def order_status(broker_order_id: str) -> OrderRecord | None:
        return None

    async def open_orders() -> list[OrderRecord]:
        return [revived]

    h.broker.order_status = order_status  # type: ignore[method-assign]
    h.broker.open_orders = open_orders  # type: ignore[method-assign]
    await h.engine._sync_orders(h.clock())
    saved = db.get_order(rec.id)
    assert saved is not None
    assert saved.broker_order_id == "GONE"
    assert saved.status == OrderStatus.SUBMITTED


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


class StalledMaker(FakeDecisionMaker):
    """An AI call the provider accepted and is still answering when the cycle is cancelled;
    it reports `abandoned` as the cut-off call's estimated cost (once)."""

    def __init__(self, name: str = "claude", abandoned: float = 0.33) -> None:
        super().__init__(name)
        self.abandoned = abandoned
        self.started = asyncio.Event()

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        self.calls.append(ctx)
        self.started.set()
        await asyncio.Event().wait()  # never answers
        raise AssertionError("unreachable")

    def take_abandoned_cost(self) -> float:
        cost, self.abandoned = self.abandoned, 0.0
        return cost


@pytest.mark.parametrize("how", ["stop", "shutdown"])
async def test_stopping_mid_ai_call_still_counts_its_cost(db, config, how) -> None:
    """Review finding: Stop (or an app restart) mid-answer journaled no batch, so the cut-off
    call's cost never reached the daily AI budget."""
    h = await harness(db, config)
    h.decider.cost = 0.10
    await h.engine.run_cycle()
    assert h.day().llm_cost == pytest.approx(0.10)
    h.clock.advance(minutes=5)
    stalled = StalledMaker()
    h.engine._decider = stalled
    h.engine._decision_task = asyncio.create_task(h.engine.run_cycle())
    await stalled.started.wait()
    await getattr(h.engine, how)()
    assert h.day().llm_cost == pytest.approx(0.43)
    assert len(db.list_decisions()) == 1  # the cancelled cycle journaled nothing
    assert stalled.take_abandoned_cost() == 0  # charged once


async def test_stopping_mid_consensus_counts_both_calls(db, config) -> None:
    from trader.strategy.consensus import ConsensusDecisionMaker

    h = await harness(db, config)
    claude, grok = StalledMaker("claude", 0.33), StalledMaker("grok", 0.05)
    h.engine._decider = ConsensusDecisionMaker(claude, grok)
    h.engine._decision_task = asyncio.create_task(h.engine.run_cycle())
    await claude.started.wait()
    await grok.started.wait()
    await h.engine.stop()
    assert h.day().llm_cost == pytest.approx(0.38)
    assert db.list_decisions() == []


async def test_stopping_mid_consensus_counts_the_side_that_already_answered(db, config) -> None:
    """Review finding: Grok had answered (and was billed) while Claude was still thinking;
    Stop threw Grok's batch away with its cost."""
    from trader.strategy.consensus import ConsensusDecisionMaker

    h = await harness(db, config)
    claude, grok = StalledMaker("claude", 0.33), FakeDecisionMaker("grok", 0.05)
    consensus = ConsensusDecisionMaker(claude, grok)
    h.engine._decider = consensus
    h.engine._decision_task = asyncio.create_task(h.engine.run_cycle())
    await claude.started.wait()
    await asyncio.sleep(0)
    assert grok.calls  # answered
    await h.engine.stop()
    assert h.day().llm_cost == pytest.approx(0.38)
    assert consensus.take_abandoned_cost() == 0  # charged once


class CrossMidnight(FakeDecisionMaker):
    """A call made at 23:58 New York that the monitor loop's day rollover overtakes."""

    def __init__(self, h: H, stall: bool, cost: float = 0.30) -> None:
        super().__init__("claude", cost)
        self.h, self.stall = h, stall
        self.started = asyncio.Event()
        self.abandoned = 0.0

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        self.h.clock.advance(minutes=2.5)
        await self.h.engine.monitor_tick()  # the concurrent monitor loop rolls the day over
        if self.stall:
            self.abandoned = self.cost
            self.started.set()
            await asyncio.Event().wait()
        return await super().decide(ctx)

    def take_abandoned_cost(self) -> float:
        cost, self.abandoned = self.abandoned, 0.0
        return cost


@pytest.mark.parametrize("stall", [False, True])
async def test_ai_cost_of_a_call_across_midnight_lands_on_its_day(db, config, stall) -> None:
    """Review finding: the cost went onto the captured day object, but the save wrote the
    new day, so neither day's AI spend counted the call."""
    late = datetime(2026, 10, 22, 3, 58, tzinfo=UTC)  # 23:58 New York, Wednesday Oct 21
    h = await harness(db, config, now=late, trade_crypto=True)
    h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO)
    btc = cand("BTC", price=60000.0, stop=59000.0, target=62000.0)
    crypto = Instrument(symbol="BTC", asset_class=AssetClass.CRYPTO)
    h.scanner.candidates["CRYPTO:BTC"] = [btc.model_copy(update={"instrument": crypto})]
    maker = CrossMidnight(h, stall)
    h.engine._decider = maker
    if stall:
        h.engine._decision_task = asyncio.create_task(h.engine.run_cycle())
        await maker.started.wait()
        await h.engine.stop()
    else:
        batch = await h.engine.run_cycle()
        assert batch is not None and batch.cost_usd == pytest.approx(0.30)
    old, new = db.get_day("2026-10-21"), db.get_day("2026-10-22")
    assert old is not None and old.llm_cost == pytest.approx(0.30)
    assert new is None or new.llm_cost == 0


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
    assert h.broker.pos
    h.clock.advance(seconds=15)  # the next monitor tick
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
    h.clock.advance(seconds=15)  # the next monitor tick
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


async def test_let_winners_run_sends_no_target_and_the_trailing_stop_sells(db, config) -> None:
    h = await harness(db, config, exits={"trail_atr_mult": 1.0, "let_winners_run": True})
    rec = await enter_f(h)  # the scanner snapshot reports ATR 0.10
    assert rec.request.take_profit is None  # journaled as sent
    assert h.broker.working("STK:F", "stop") and not h.broker.working("STK:F", "target")
    assert db.get_position_meta("STK:F")["take_profit"] is None
    # The planned target was 10.50: the winner keeps running past it.
    h.broker.set_price("F", 10.80)  # bid 10.79: breakeven, then the trail 1 ATR below
    h.clock.advance(minutes=2)
    await h.engine.monitor_tick()
    await h.engine.monitor_tick()
    assert h.broker.pos
    assert db.get_position_meta("STK:F")["stop_loss"] == pytest.approx(10.69)
    h.broker.set_price("F", 10.65)  # bid 10.64: through the trailed stop at IBKR
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    assert h.broker.pos == {}


@pytest.mark.parametrize(
    "exits",
    [{"let_winners_run": True}, {"trail_atr_mult": 1.0}, {}],
    ids=["no-trail", "off", "default"],
)
async def test_target_is_sent_unless_winners_run_with_a_trailing_stop(db, config, exits) -> None:
    h = await harness(db, config, exits=exits)
    rec = await enter_f(h)
    assert rec.request.take_profit == 10.5
    assert h.broker.working("STK:F", "target")
    assert db.get_position_meta("STK:F")["take_profit"] == 10.5


async def test_let_winners_run_on_an_approved_entry(db, config) -> None:
    """With autopilot off the approval card shows the planned target (the risk check needs
    it); the order sent on approval follows the setting at that moment."""
    h = await harness(db, config, autopilot=False, exits={"trail_atr_mult": 1.0})
    await h.engine.run_cycle()
    [waiting] = h.orders(OrderStatus.PENDING_APPROVAL)
    assert waiting.request.take_profit == 10.5
    settings = h.engine.settings.model_copy(deep=True)
    settings.exits.let_winners_run = True
    db.save_settings(settings)
    h.engine.reload_settings()
    sent = await h.engine.approve_order(waiting.id)
    assert sent.status == OrderStatus.FILLED
    assert sent.request.take_profit is None  # journaled as sent (review finding)
    assert any("target none (trailing stop)" in body for _, body, _ in h.notes.sent)
    assert not h.broker.working("STK:F", "target") and h.broker.working("STK:F", "stop")


async def test_trailing_stop_keeps_moving_after_a_restart_while_halted(db, config) -> None:
    """Review finding: the trail read ATR only from the last decision cycle, and none runs
    while halted (a reached daily target) or right after a restart. With no target, the
    winner then had no profit exit. The ATR is saved with the position."""
    h = await harness(
        db,
        config,
        mode=Mode.PAPER,
        risk={"daily_profit_target_usd": 5},
        exits={"trail_atr_mult": 1.0, "let_winners_run": True, "time_stop_minutes": 0},
    )
    await enter_f(h)  # ATR 0.10
    h.broker.set_price("F", 10.30)  # +$6.96: the daily target is reached
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.day().halted
    engine = Engine(
        config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock, scanner=h.scanner
    )
    h.clock.advance(minutes=1)
    await engine.monitor_tick()
    assert await engine.run_cycle() is None and engine._market == {}
    h.broker.set_price("F", 11.50)  # bid 11.49
    h.clock.advance(seconds=15)
    await engine.monitor_tick()
    assert db.get_position_meta("STK:F")["stop_loss"] == pytest.approx(11.39)


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
    assert h.broker.pos  # confirmed on the next tick, not on one print
    h.clock.advance(seconds=15)
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


class QueueingBroker(FakeBroker):
    """Holds market exits outside regular hours (IBKR queues a DAY MKT for the open)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rth = True

    async def place_order(self, req: OrderRequest) -> OrderRecord:
        if self.rth or req.side != Side.SELL or req.stop_loss is not None:
            return await super().place_order(req)
        self._require()
        self.placed.append(req)
        oid = f"Q{len(self.placed)}"
        self.orders[oid] = OrderRecord(
            request=req, status=OrderStatus.SUBMITTED, broker_order_id=oid
        )
        self.kind[oid], self.parent[oid] = "exit", None
        return self.orders[oid].model_copy()


async def test_reconnect_never_adds_a_stop_next_to_a_queued_exit(db, config) -> None:
    """ENG-4: a kill after hours cancels the bracket stop and queues a market SELL for the
    open. The nightly gateway restart (a reconnect) must not add a full-size stop next to
    it: both could execute on a gap down and sell the shares twice."""
    h = await harness(db, config, flatten_stocks_before_close=False)
    broker = QueueingBroker(h.clock)
    broker.set_price("F", 10.0)
    broker.set_price("T", 20.0)
    await broker.connect()
    h.engine.broker = h.broker = broker
    await enter_f(h)
    held = broker.pos["STK:F"][0]
    h.clock.t = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)  # 20:00 ET
    broker.rth = False
    await h.engine.monitor_tick()
    await h.engine.kill()
    assert broker.working("STK:F", "exit") and not broker.working("STK:F", "stop")
    h.clock.advance(hours=4)
    await broker.disconnect()
    await h.engine.monitor_tick()  # reconnects and runs the re-protect step
    assert not broker.working("STK:F", "stop")
    working = broker.working("STK:F", "exit") + broker.working("STK:F", "stop")
    assert sum(broker.orders[o].request.qty for o in working) == held


@pytest.mark.parametrize(
    ("order_type", "fields"),
    [
        ("MIT", {"auxPrice": 110.0}),
        ("MKT", {}),
        ("STP", {"auxPrice": 97.5}),
        ("STP LMT", {"auxPrice": 97.5, "lmtPrice": 97.0}),
        ("TRAIL LIMIT", {"auxPrice": 2.0, "trailStopPrice": 97.5, "lmtPrice": 97.0}),
    ],
)
async def test_reconnect_replaces_the_stop_next_to_an_owners_sell(
    db, config, order_type, fields
) -> None:
    """Review finding: IBKR read an owner's MIT sell from IBKR Mobile back as a market
    sell, so after a reconnect the re-protect step took it for a queued exit of the bot's
    and left the position without a stop (an MIT may never trigger). Only this app's own
    market exits count: the bot's stop is re-placed next to the owner's order."""
    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import av, connected_broker, hold, owner_sell, stock

    broker, fake = await connected_broker()
    fake.summary = [av("NetLiquidation", "1000"), av("TotalCashValue", "700")]
    hold(fake, stock(), 3)
    db.set_position_meta(
        "STK:AAPL", stop_loss=95.0, take_profit=110.0, entry_price=100.0, opened_at=IB_NOW
    )  # a bot position whose bracket stop is gone
    fake.all_open = [owner_sell(order_type, 3, **fields)]
    notes, clock = RecordingNotifier(), FakeClock(IB_NOW)
    engine = Engine(config, db, broker, FakeDecisionMaker(), notifier=notes, clock=clock)
    fake.disconnect()
    await engine.monitor_tick()  # reconnects, then runs the re-protect step
    assert [(o.orderType, o.action, o.totalQuantity, o.auxPrice) for _, o in fake.placed] == [
        ("STP", "SELL", 3, 95.0)
    ]
    assert any(a["action"] == "reprotect" for a in db.list_audit())


def _owner_buy():
    """A GTC buy the owner placed in TWS or IBKR Mobile (another API client)."""
    from ib_async import Order, Trade
    from ib_async import OrderStatus as IBOrderStatus

    from tests.test_ibkr import PAPER, stock

    order = Order(
        orderId=0, clientId=0, permId=888, action="BUY", totalQuantity=1,
        orderType="LMT", lmtPrice=90.0, tif="GTC", account=PAPER,
    )  # fmt: skip
    return Trade(stock(), order, IBOrderStatus(status="Submitted"))


async def test_reconnect_replaces_the_stop_next_to_an_owners_buy(db, config) -> None:
    """Review finding: any working BUY counted as an entry of the bot's still on its way
    (its bracket would protect the shares), so an owner's buy from TWS left a bot position
    with no stop re-placed and no alert."""
    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import av, connected_broker, hold, stock

    broker, fake = await connected_broker()
    fake.summary = [av("NetLiquidation", "1000"), av("TotalCashValue", "700")]
    hold(fake, stock(), 3)
    db.set_position_meta(
        "STK:AAPL", stop_loss=95.0, take_profit=110.0, entry_price=100.0, opened_at=IB_NOW
    )  # a bot position whose bracket stop is gone
    fake.all_open = [_owner_buy()]
    engine = Engine(
        config, db, broker, FakeDecisionMaker(), notifier=RecordingNotifier(),
        clock=FakeClock(IB_NOW),
    )  # fmt: skip
    fake.disconnect()
    await engine.monitor_tick()  # reconnects, then runs the re-protect step
    assert [(o.orderType, o.action, o.totalQuantity, o.auxPrice) for _, o in fake.placed] == [
        ("STP", "SELL", 3, 95.0)
    ]


@pytest.mark.parametrize(
    ("order_type", "fields"),
    [
        ("STP LMT", {"auxPrice": 97.5, "lmtPrice": 97.0}),
        ("TRAIL LIMIT", {"auxPrice": 2.0, "trailStopPrice": 97.5, "lmtPrice": 97.0}),
    ],
)
async def test_backup_stop_fires_next_to_an_owners_stop_limit(
    db, config, order_type, fields
) -> None:
    """Review finding: an owner's stop-limit (or trailing limit) sell from TWS counted as
    the position's broker stop. Triggered above the bot's stop and resting unfilled after
    a gap down, it switched off the engine's backup stop: nothing could sell the shares."""
    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import av, connected_broker, hold, owner_sell, stock

    broker, fake = await connected_broker()
    fake.summary = [av("NetLiquidation", "1000"), av("TotalCashValue", "700")]
    hold(fake, stock(), 3)
    db.set_position_meta(
        "STK:AAPL", stop_loss=95.0, take_profit=110.0, entry_price=100.0, opened_at=IB_NOW
    )  # a bot position whose bracket stop is gone
    fake.all_open = [owner_sell(order_type, 3, **fields)]
    fake.ticker_values[stock().conId] = {"bid": 90.0, "ask": 90.05, "last": 90.0}
    clock = FakeClock(IB_NOW)
    engine = Engine(
        config, db, broker, FakeDecisionMaker(), notifier=RecordingNotifier(), clock=clock
    )
    engine._reprotect_pending = False  # only the monitor's exit rules
    for _ in range(3):
        await engine.monitor_tick()
        clock.advance(seconds=30)
    assert [(o.orderType, o.action, o.totalQuantity) for _, o in fake.placed] == [
        ("MKT", "SELL", 3)
    ]


async def test_leftover_stop_is_cancelled_next_to_an_owners_buy(db, config) -> None:
    """Review finding: an owner's working buy from TWS held back the cleanup of a flat bot
    position, so its leftover stop stayed working (it would sell shares not held)."""
    from ib_async import CommissionReport
    from ib_async import Fill as IBFill

    from tests.test_ibkr import CLIENT_ID, av, connected_broker, execution, hold, stock
    from tests.test_ibkr import NOW as IB_NOW

    broker, fake = await connected_broker()
    fake.summary = [av("NetLiquidation", "10000"), av("TotalCashValue", "7000")]
    hold(fake, stock(), 3)
    opened = IB_NOW - timedelta(minutes=30)
    bought = execution(
        "e1", "BOT", opened, shares=3, price=100.0, orderId=50, clientId=CLIENT_ID, permId=9
    )
    fake.executions = [IBFill(stock(), bought, CommissionReport(), opened)]
    stop = await broker.place_order(
        OrderRequest(
            instrument=Instrument(symbol="AAPL"),
            side=Side.SELL,
            qty=3,
            order_type=OrderType.MARKET,
            stop_loss=95.0,
            is_exit=True,
            reason="protective stop",
        )
    )
    db.set_position_meta(
        "STK:AAPL", stop_loss=95.0, take_profit=110.0, entry_price=100.0, opened_at=opened
    )
    fake.all_open = [_owner_buy()]
    fake.ticker_values[stock().conId] = {"bid": 98.0, "ask": 98.05, "last": 98.0}
    clock = FakeClock(IB_NOW)
    engine = Engine(
        config, db, broker, FakeDecisionMaker(), notifier=RecordingNotifier(), clock=clock
    )
    await engine.monitor_tick()
    clock.advance(minutes=1)
    fake.position_items.clear()  # the owner sold the 3 shares in TWS
    sold = execution("e2", "SLD", clock(), shares=3, price=98.0, orderId=0, clientId=0, permId=77)
    fake.executions.append(IBFill(stock(), sold, CommissionReport(), clock()))
    for _ in range(3):
        await engine.monitor_tick()
        clock.advance(minutes=1)
    assert int(stop.broker_order_id or 0) in fake.cancelled
    assert db.get_position_meta("STK:AAPL") is None


async def _time_stop_with_own_stop(db, config):
    """A bot position past its time stop at 0R, with its own IBKR stop at 95 working."""
    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import av, connected_broker, hold, stock

    broker, fake = await connected_broker()
    fake.summary = [av("NetLiquidation", "10000"), av("TotalCashValue", "7000")]
    hold(fake, stock(), 3)
    db.set_position_meta(
        "STK:AAPL",
        stop_loss=95.0,
        take_profit=110.0,
        entry_price=100.0,
        opened_at=IB_NOW - timedelta(minutes=50),
    )
    await broker.place_order(
        OrderRequest(
            instrument=Instrument(symbol="AAPL"), side=Side.SELL, qty=3,
            order_type=OrderType.MARKET, stop_loss=95.0, is_exit=True,
            reason="protective stop",
        )
    )  # fmt: skip
    fake.ticker_values[stock().conId] = {"bid": 100.0, "ask": 100.05, "last": 100.0}
    clock = FakeClock(IB_NOW)
    engine = Engine(
        config, db, broker, FakeDecisionMaker(), notifier=RecordingNotifier(), clock=clock
    )
    engine._reprotect_pending = False  # the start-up pass is done
    return broker, fake, engine, clock


def _set_bid(broker, fake, bid: float) -> None:
    from tests.test_ibkr import stock

    values = {"bid": bid, "ask": bid + 0.05, "last": bid}
    fake.ticker_values[stock().conId] = values
    sub = broker._tickers.get("STK:AAPL")
    if sub is not None:
        for name, value in values.items():
            setattr(sub.ticker, name, value)


def _stops(fake) -> list[float]:
    from tests.test_ibkr import DONE

    return [
        t.order.auxPrice
        for t in fake.trade_list
        if t.order.action == "SELL"
        and t.order.orderType == "STP"
        and t.orderStatus.status not in DONE
    ]


async def test_stop_comes_back_after_an_exit_lost_to_a_quick_reconnect(db, config) -> None:
    """Review finding: a monitor exit cancelled the stop, then the gateway socket dropped
    before the sell. The next tick reconnected and ran re-protect, which skipped the
    position (its close marker) and never ran again: once the exit condition cleared,
    the position kept no IBKR stop until the next reconnect."""
    broker, fake, engine, clock = await _time_stop_with_own_stop(db, config)
    place = fake.placeOrder

    def drop_on_market_sell(contract, order):
        if order.orderType == "MKT" and order.action == "SELL":
            fake.disconnect()
            raise ConnectionError("Not connected")
        return place(contract, order)

    fake.placeOrder = drop_on_market_sell
    await engine.monitor_tick()  # time stop seen
    clock.advance(seconds=15)
    await engine.monitor_tick()  # confirmed: stop cancelled, then the sell fails
    assert _stops(fake) == []
    fake.placeOrder = place
    _set_bid(broker, fake, 103.0)  # 0.6R: the time stop no longer applies
    for _ in range(6):  # reconnects, then 75 s of ticks
        clock.advance(seconds=15)
        await engine.monitor_tick()
    assert broker.connected
    assert _stops(fake) == [95.0]


async def test_stop_comes_back_after_an_exit_lost_to_a_link_blip(db, config) -> None:
    """Review finding: IBKR error 1100 (the gateway lost its link) right after the stop's
    cancel refused the sell, and 1102 restored it before any tick saw a disconnect, so
    re-protect was never armed. Every close attempt arms it now."""
    broker, fake, engine, clock = await _time_stop_with_own_stop(db, config)
    cancel = fake.cancelOrder

    def cancel_then_link_lost(order):
        trade = cancel(order)
        fake.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
        return trade

    fake.cancelOrder = cancel_then_link_lost
    await engine.monitor_tick()
    clock.advance(seconds=15)
    await engine.monitor_tick()  # close: stop cancelled, sell refused (link lost)
    fake.cancelOrder = cancel
    fake.errorEvent.emit(-1, 1102, "Connectivity between IB and TWS has been restored", None)
    assert _stops(fake) == [] and broker.connected
    _set_bid(broker, fake, 103.0)
    for _ in range(6):
        clock.advance(seconds=15)
        await engine.monitor_tick()
    assert _stops(fake) == [95.0]


async def test_refused_owner_close_during_a_tick_leaves_one_stop(db, config) -> None:
    """Review finding: the owner pressed Close while a monitor tick was between its order
    read and re-protect. IBKR refused the sell, so the close put the stop back and dropped
    its marker; re-protect then worked from the tick's older order list (the first stop
    cancelled, the put-back one not listed yet) and placed a second stop."""
    from tests.test_ibkr import log_error

    broker, fake, engine, clock = await _time_stop_with_own_stop(db, config)
    _set_bid(broker, fake, 103.0)  # 0.6R: no exit of its own
    engine._reprotect_pending = False

    def reject_market(trade) -> None:
        if trade.order.orderType == "MKT":
            trade.orderStatus.status = "Cancelled"
            log_error(trade, 201, "Order rejected - reason: test")

    fake.on_place = reject_market
    manage = engine._manage_positions

    async def manage_then_owner_close(*args, **kwargs):
        await manage(*args, **kwargs)
        with pytest.raises(BrokerError):
            await engine.close_position("STK:AAPL")

    engine._manage_positions = manage_then_owner_close
    await engine.monitor_tick()
    assert _stops(fake) == [95.0]  # put back by the refused close
    engine._manage_positions = manage
    for _ in range(6):
        clock.advance(seconds=15)
        await engine.monitor_tick()
    assert _stops(fake) == [95.0]


async def test_refused_owner_close_started_before_a_tick_leaves_one_stop(db, config) -> None:
    """Review finding: as above, with the owner's Close stamped (its stop cancelled) just
    before the tick took its snapshot, and refused while the tick ran. Re-protect now
    decides under the broker's close lock, from a fresh read of the orders."""
    from tests.test_ibkr import log_error

    broker, fake, engine, clock = await _time_stop_with_own_stop(db, config)
    _set_bid(broker, fake, 103.0)

    def reject_market(trade) -> None:
        if trade.order.orderType == "MKT":
            trade.orderStatus.status = "Cancelled"
            log_error(trade, 201, "Order rejected - reason: test")

    fake.on_place = reject_market
    read, reads = broker.open_orders, 0
    parked, resume = asyncio.Event(), asyncio.Event()

    async def open_orders():
        nonlocal reads
        reads += 1
        if reads == 3:  # the close's last check, after it cancelled the stop
            parked.set()
            await resume.wait()
        return await read()

    broker.open_orders = open_orders
    owner = asyncio.create_task(engine.close_position("STK:AAPL"))
    await parked.wait()
    assert _stops(fake) == []
    manage = engine._manage_positions

    async def manage_while_the_close_finishes(*args, **kwargs):
        await manage(*args, **kwargs)
        resume.set()
        with pytest.raises(BrokerError):
            await owner

    engine._manage_positions = manage_while_the_close_finishes
    await engine.monitor_tick()
    assert _stops(fake) == [95.0]  # the stop the refused close put back, and no other
    engine._manage_positions = manage
    for _ in range(6):
        clock.advance(seconds=15)
        await engine.monitor_tick()
    assert _stops(fake) == [95.0]


async def test_crypto_the_watchdog_sold_is_not_managed_after_a_restart(db, config) -> None:
    """Review finding: the watchdog sold the bot's BTC while the app was down and the
    owner bought BTC since. The app came back with the old position's meta and managed the
    owner's BTC as the bot's (its stop, its time stop)."""
    from trader.watchdog import WATCHDOG_CLOSED_PREFIX, position_marker

    h = await harness(db, config, trade_crypto=True)
    h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO)
    key = "CRYPTO:BTC"
    db.set_position_meta(
        key,
        stop_loss=59000.0,
        take_profit=62000.0,
        entry_price=60000.0,
        opened_at=NOW - timedelta(hours=2),
    )
    db.set_kv(WATCHDOG_CLOSED_PREFIX + key, position_marker(db.all_position_meta()[key]))
    h.broker.pos[key] = [0.5, 58000.0]  # the owner's buy, after the watchdog's sale
    h.broker.set_price("BTC", 50000.0, asset_class=AssetClass.CRYPTO)  # far below the stop
    for _ in range(3):
        await h.engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert not [r for r in h.broker.placed if r.instrument.key == key]
    assert h.broker.pos[key][0] == 0.5
    assert key not in db.all_position_meta()  # forgotten: the owner's position
    assert not db.get_kv(WATCHDOG_CLOSED_PREFIX + key)


async def test_an_entry_row_never_adopts_its_own_bracket_stop(db, config) -> None:
    """Review finding: an entry filled while the app was down and the app came back on a
    later day, when IBKR no longer reported the filled parent. The journal row was matched
    by its tag to a working order: the bracket's stop, which carries the same tag. The
    stale-entry timer then cancelled that stop as an unfilled entry."""
    from tests.test_ibkr import DONE, av, connected_broker, entry, hold, stock

    day1 = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)  # Thursday 14:00 New York
    broker, fake = await connected_broker()
    fake.summary = [av("NetLiquidation", "10000"), av("TotalCashValue", "7000")]
    engine = Engine(
        config, db, broker, FakeDecisionMaker(), notifier=RecordingNotifier(),
        clock=FakeClock(day1),
    )  # fmt: skip
    req = entry()
    row_id = db.insert_order(
        OrderRecord(
            request=req, status=OrderStatus.PENDING_APPROVAL, created_at=day1, updated_at=day1
        )
    )
    traced = engine._traced(req, row_id)
    placed = await engine._send_entry(traced, day1)
    db.update_order(
        row_id, request=traced, status=placed.status, broker_order_id=placed.broker_order_id
    )
    db.set_position_meta(
        "STK:AAPL", stop_loss=98.11, take_profit=104.0, entry_price=100.23, opened_at=day1
    )
    parent, stop, target = fake.trade_list
    # The entry filled while the app was down. Next day IBKR reports the parent only as a
    # completed order (no orderId or clientId) and no executions; its children work.
    parent.order.permId, parent.order.orderId, parent.order.clientId = 555, 0, 0
    parent.orderStatus.status, parent.orderStatus.filled = "Filled", 3
    stop.orderStatus.status = target.orderStatus.status = "Submitted"
    hold(fake, stock(), 3)
    fake.ticker_values[stock().conId] = {"bid": 101.0, "ask": 101.05, "last": 101.0}
    broker, _ = await connected_broker(fake)
    clock = FakeClock(datetime(2026, 10, 23, 15, 0, tzinfo=UTC))  # Friday 11:00
    engine = Engine(
        config, db, broker, FakeDecisionMaker(), notifier=RecordingNotifier(), clock=clock
    )
    for _ in range(44):  # 11 minutes: past the stale-entry timer
        await engine.monitor_tick()
        clock.advance(seconds=15)
    assert stop.order.orderId not in fake.cancelled
    assert stop.orderStatus.status not in DONE
    assert db.get_order(row_id).broker_order_id != str(stop.order.orderId)


async def test_an_approved_close_that_failed_can_be_approved_again(db, config) -> None:
    """Review finding: an IBKR timeout during an approved close left its marker, so
    approving it again within a minute cancelled it as "already being closed" while
    nothing was selling."""
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.engine.settings.autopilot = False
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    [waiting] = h.orders(OrderStatus.PENDING_APPROVAL)
    read, reads = h.broker.open_orders, 0

    async def flaky():
        nonlocal reads
        reads += 1
        if reads == 2:  # the read inside the broker's close, before anything is sent
            raise BrokerError("IBKR open orders timed out after 10s")
        return await read()

    h.broker.open_orders = flaky
    with pytest.raises(BrokerError):
        await h.engine.approve_order(waiting.id)
    h.broker.open_orders = read
    h.clock.advance(seconds=10)
    rec = await h.engine.approve_order(waiting.id)
    assert rec.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED)
    assert "STK:F" not in h.broker.pos


async def test_a_refused_engine_exit_alerts_failed_not_closing(db, config) -> None:
    """Review finding: a refused flatten-before-close sent "Closing F" every minute and
    never a failure alert, and the position stayed open overnight."""
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)  # 15:51 New York
    h.broker.reject_exits = True
    for _ in range(9):
        await h.engine.monitor_tick()
        h.clock.advance(minutes=1)
    titles = h.notes.titles()
    assert "Closing F" not in titles
    assert titles.count("Close F FAILED") == 1  # retried every minute, alerted once


async def test_an_exit_ibkr_no_longer_reports_frees_the_symbol(db, config) -> None:
    """Review finding: a stop filled while the app was down, and the app came back on a
    later day, when IBKR reports only that day's executions. The fills journal still
    showed the shares, so the position's meta stayed for good and blocked every new
    entry in the symbol. After UNREPORTED_EXIT_GRACE the exit is journaled at an
    estimate, on the day of the entry, and the owner is told."""
    tuesday = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # 11:00 New York
    h = await harness(
        db, config, mode=Mode.PAPER, now=tuesday, flatten_stocks_before_close=False,
        exits={"time_stop_minutes": 0},
    )  # fmt: skip
    today_only = h.broker.fills_since

    async def fills_since(since):  # IB Gateway: only executions since midnight
        midnight = h.clock().replace(hour=0, minute=0, second=0, microsecond=0)
        return [f for f in await today_only(since) if f.ts >= midnight]

    h.broker.fills_since = fills_since
    await enter_f(h)
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    h.clock.t = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)  # the app is down: the stop fills
    h.broker.set_price("F", 9.70)
    assert h.broker.pos == {}
    h.clock.t = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # Wednesday: the app is back
    h.broker.set_price("F", 10.0)
    engine = Engine(
        config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock, scanner=h.scanner
    )
    for _ in range(4):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert db.get_position_meta("STK:F") is not None  # still waiting for the fill
    for _ in range(60):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert db.get_position_meta("STK:F") is None
    [trade] = db.list_trades()
    assert "price estimated" in (trade.exit_reason or "")
    assert trade.exit_ts.date() == datetime(2026, 10, 20).date()  # on the entry's day
    assert "Exit of F not reported" in h.notes.titles()


async def test_two_single_flat_reads_a_day_apart_never_fake_an_exit(db, config) -> None:
    """Review finding: the unreported-exit timer ran from the first flat read the process
    ever saw, so one empty positions read on Tuesday and another on Wednesday journaled
    an estimated exit for shares still held, cancelled their stop and forgot them."""
    tuesday = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # 11:00 New York
    h = await harness(
        db, config, mode=Mode.PAPER, now=tuesday, flatten_stocks_before_close=False,
        exits={"time_stop_minutes": 0},
    )  # fmt: skip
    await enter_f(h)
    stops = h.broker.working("STK:F", "stop")
    h.clock.advance(minutes=3)
    await h.engine.monitor_tick()
    read, empty = h.broker.positions, 0

    async def positions():
        nonlocal empty
        if empty:
            empty -= 1
            return []
        return await read()

    h.broker.positions = positions
    empty = 1  # one empty read on Tuesday (e.g. right after a gateway reconnect)
    await h.engine.monitor_tick()
    for _ in range(30):  # held for the rest of the day and overnight
        h.clock.advance(minutes=30)
        await h.engine.monitor_tick()
    h.clock.t = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # Wednesday 10:00 New York
    await h.engine.monitor_tick()
    empty = 1  # and one on Wednesday
    await h.engine.monitor_tick()
    assert db.get_position_meta("STK:F") is not None
    assert h.broker.working("STK:F", "stop") == stops
    assert db.list_trades() == []


async def test_no_entry_once_the_price_fell_through_its_stop(db, config) -> None:
    """Review finding: while Claude thought, the price fell through the stop but stayed
    within the 1% collar. The BUY still went out, filled, and its bracket stop (already
    above the bid) sold it at once."""
    h = await harness(db, config)
    h.scanner.candidates["STK:F"] = [cand(price=10.0, stop=9.93, target=10.20)]

    async def drop(ctx: DecisionContext) -> None:
        h.clock.advance(seconds=90)
        h.broker.set_price("F", 9.92)  # bid 9.91, ask 9.93: below the stop at 9.95

    maker = SlowMaker(drop)
    maker.decisions = [buy(limit_price=10.01, stop_loss=9.95, take_profit=10.20)]
    h.engine._decider = maker
    await h.engine.run_cycle()
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    assert h.broker.fills == []


async def test_an_approved_entry_whose_stop_the_price_fell_through_is_blocked(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    h.scanner.candidates["STK:F"] = [cand(price=10.0, stop=9.95, target=10.20)]
    h.decider.decisions = [buy()]  # limit = the ask, 10.01
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    h.clock.advance(minutes=3)
    h.broker.set_price("F", 9.92)
    await h.engine.monitor_tick()
    rec = await h.engine.approve_order(pending.id)
    assert rec.status == OrderStatus.BLOCKED and "at/above the market" in (rec.message or "")
    assert h.broker.fills == []


async def test_open_positions_of_today_count_against_day_trades_left(db, config) -> None:
    """Review finding: with one day trade left the bot opened F and then T while F was
    still open. The flatten before the close sold both: two day trades with one left."""
    h = await harness(db, config)
    h.broker.day_trades = 1
    await h.engine.run_cycle()
    assert "STK:F" in h.broker.pos
    h.scanner.candidates = {"STK:T": [cand("T", price=20.0, stop=19.8, target=20.6)]}
    h.decider.decisions = [buy("T")]
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert "STK:T" not in h.broker.pos


async def test_a_second_buy_in_one_answer_is_not_squeezed_by_the_first(db, config) -> None:
    """Review finding: the first BUY of a batch filled at once and was counted twice, as a
    position and as a working buy, so the second BUY sized to 0 shares."""
    h = await harness(db, config)
    h.scanner.candidates = {
        "STK:F": [cand()],
        "STK:T": [cand("T", price=20.0, stop=19.8, target=20.6)],
    }
    h.decider.decisions = [buy("F"), buy("T")]
    await h.engine.run_cycle()
    assert "STK:F" in h.broker.pos and "STK:T" in h.broker.pos


async def test_a_refused_bracket_stop_alerts_and_is_placed_again(db, config) -> None:
    """Review finding: IBKR refused the bracket stop after the entry filled. The owner got
    the usual "BUY" alert naming a stop that did not exist, and nothing re-placed it."""
    from ib_async import Trade

    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import av, connected_broker, log_error, stock
    from tests.test_ibkr import hold as hold_ib

    broker, fake = await connected_broker(ib_market_data_type=1)
    fake.summary = [
        av("NetLiquidation", "10000"),
        av("TotalCashValue", "10000"),
        av("BuyingPower", "10000"),
    ]
    fake.ticker_values[stock().conId] = {"bid": 100.0, "ask": 100.02, "last": 100.01}
    settings = BotSettings(
        stock_universe=["AAPL"], trade_crypto=False, autopilot=True,
        risk=RiskLimits(max_order_notional_usd=1000),
    )  # fmt: skip
    db.save_settings(settings)
    scanner = FakeScanner(settings)
    scanner.candidates["STK:AAPL"] = [cand("AAPL", price=100.01, stop=99.5, target=101.5)]
    maker = FakeDecisionMaker()
    maker.decisions = [buy("AAPL")]
    notes, clock = RecordingNotifier(), FakeClock(IB_NOW)
    engine = Engine(config, db, broker, maker, notifier=notes, clock=clock, scanner=scanner)

    def fill_parent_refuse_stop(trade: Trade) -> None:
        if trade.order.orderType == "STP" and trade.order.parentId:
            trade.orderStatus.status = "Inactive"
            log_error(trade, 201, "Order rejected - reason: stop price invalid")
            parent = fake.trade_list[0]
            parent.orderStatus.status = "Filled"
            parent.orderStatus.filled = parent.order.totalQuantity
            hold_ib(fake, stock(), parent.order.totalQuantity, 100.02)

    fake.on_place = fill_parent_refuse_stop
    await engine.run_cycle()
    fake.on_place = None
    assert "Unprotected AAPL" in notes.titles() and "BUY AAPL" not in notes.titles()
    clock.advance(seconds=15)
    await engine.monitor_tick()
    assert [
        (t.order.action, t.order.auxPrice) for t in fake.openTrades() if t.order.orderType == "STP"
    ] == [("SELL", 99.5)]


async def _ibkr_entry_engine(db, config, maker=None):
    """An IBKR-backed engine about to BUY AAPL (stop 99.5, target 101.5) on its next cycle."""
    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import av, connected_broker, stock

    broker, fake = await connected_broker(ib_market_data_type=1)
    fake.summary = [
        av("NetLiquidation", "10000"),
        av("TotalCashValue", "10000"),
        av("BuyingPower", "10000"),
    ]
    fake.ticker_values[stock().conId] = {"bid": 100.0, "ask": 100.02, "last": 100.01}
    settings = BotSettings(
        stock_universe=["AAPL"], trade_crypto=False, autopilot=True,
        risk=RiskLimits(max_order_notional_usd=1000),
    )  # fmt: skip
    db.save_settings(settings)
    scanner = FakeScanner(settings)
    scanner.candidates["STK:AAPL"] = [cand("AAPL", price=100.01, stop=99.5, target=101.5)]
    maker = maker or FakeDecisionMaker()
    maker.decisions = [buy("AAPL")]
    notes, clock = RecordingNotifier(), FakeClock(IB_NOW)
    engine = Engine(config, db, broker, maker, notifier=notes, clock=clock, scanner=scanner)
    return engine, broker, fake, notes, clock


def _refuse_child(fake, order_type: str, filled: float | None = None):
    """on_place hook: IBKR refuses the bracket's `order_type` child; the parent fills
    (`filled` of it, the rest still working, when given)."""
    from ib_async import Position as IBPosition

    from tests.test_ibkr import PAPER, log_error, stock

    def hook(trade) -> None:
        if trade.order.orderType == order_type and trade.order.parentId:
            trade.orderStatus.status = "Inactive"
            log_error(trade, 163, "Order rejected - reason: refused by test")
            parent = fake.trade_list[0]
            qty = parent.order.totalQuantity if filled is None else filled
            parent.orderStatus.status = "Filled" if filled is None else "Submitted"
            parent.orderStatus.filled = qty
            fake.position_items[:] = [IBPosition(PAPER, stock(), qty, 100.02)]

    return hook


def _stops_working(fake) -> list[tuple[str, float]]:
    return [
        (t.order.action, t.order.auxPrice) for t in fake.openTrades() if t.order.orderType == "STP"
    ]


async def test_a_refused_stop_of_a_part_filled_entry_is_placed_once_it_fills(db, config) -> None:
    """Review finding: the entry filled only in part and IBKR refused its stop. Re-protect
    skipped the position while the rest of the entry worked and never looked again."""
    from ib_async import Position as IBPosition

    from tests.test_ibkr import PAPER, stock

    engine, _broker, fake, notes, clock = await _ibkr_entry_engine(db, config)
    fake.on_place = _refuse_child(fake, "STP", filled=4)
    await engine.run_cycle()
    fake.on_place = None
    [(title, body, _)] = [n for n in notes.sent if n[0] == "Unprotected AAPL"]
    assert "Bought 4 of 9" in body
    clock.advance(seconds=15)
    await engine.monitor_tick()
    assert _stops_working(fake) == []  # still buying
    parent = fake.trade_list[0]
    parent.orderStatus.status, parent.orderStatus.filled = "Filled", parent.order.totalQuantity
    fake.position_items[:] = [IBPosition(PAPER, stock(), parent.order.totalQuantity, 100.02)]
    clock.advance(seconds=15)
    await engine.monitor_tick()
    assert _stops_working(fake) == [("SELL", 99.5)]


async def test_a_tick_already_running_does_not_use_up_a_refused_stops_rearm(db, config) -> None:
    """Review finding: a monitor tick read the positions (no AAPL yet), then the entry
    filled and IBKR refused its stop, arming re-protect; the tick then cleared the flag
    looking at its old positions, and no later tick looked again."""
    gate = asyncio.Event()
    hold_next = False
    ticks: list[asyncio.Task] = []
    box: dict[str, Engine] = {}

    async def during(ctx) -> None:
        nonlocal hold_next
        hold_next = True
        ticks.append(asyncio.create_task(box["engine"].monitor_tick()))
        await asyncio.sleep(0.01)  # that tick has read the positions: flat

    engine, broker, fake, _notes, clock = await _ibkr_entry_engine(db, config, SlowMaker(during))
    box["engine"] = engine
    read = broker.open_orders

    async def open_orders():
        nonlocal hold_next
        if hold_next:
            hold_next = False
            await gate.wait()  # a slow reply to that tick's read
        return await read()

    broker.open_orders = open_orders
    fake.on_place = _refuse_child(fake, "STP")
    await engine.run_cycle()
    fake.on_place = None
    gate.set()
    await ticks[0]
    clock.advance(seconds=15)
    await engine.monitor_tick()
    assert _stops_working(fake) == [("SELL", 99.5)]


async def test_a_refused_take_profit_is_no_refused_stop(db, config) -> None:
    """Review finding: IBKR refused the bracket's take-profit (its stop working) and the
    owner got a false "Unprotected" alert about a refused stop, and nothing about the
    missing target."""
    engine, _broker, fake, notes, _clock = await _ibkr_entry_engine(db, config)
    fake.on_place = _refuse_child(fake, "LMT")
    await engine.run_cycle()
    fake.on_place = None
    assert "Unprotected AAPL" not in notes.titles()
    [(title, body, _)] = [n for n in notes.sent if n[0] == "BUY AAPL"]
    assert "refused by the broker" in body
    assert _stops_working(fake) == [("SELL", 99.5)]


async def test_the_engine_sells_at_the_target_when_no_take_profit_works(db, config) -> None:
    """Review finding: a refused close put back the stop but not the take-profit, and the
    engine checked targets only for positions without broker stops: the price passed the
    target and nothing sold."""
    h = await harness(db, config)
    await enter_f(h)  # bracket: stop 9.80, target 10.50
    h.broker.reject_exits = True
    h.clock.advance(minutes=46)  # 0R after 45 min: time stop
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # the close is refused: only the stop is put back
    assert h.broker.working("STK:F", "stop") and not h.broker.working("STK:F", "target")
    h.broker.reject_exits = False
    h.broker.set_price("F", 10.15)  # +0.7R: no time stop
    for _ in range(4):
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    assert h.broker.pos
    h.broker.set_price("F", 10.60)  # past the 10.50 target
    for _ in range(4):
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    assert not h.broker.pos


async def test_a_stop_out_between_two_ticks_is_no_withdrawal(db, config) -> None:
    """Review finding: after a quiet spell, an entry that filled and was stopped out before
    the next monitor tick looked like a withdrawal: the loss left the day's P&L and the
    daily loss limit."""
    h = await harness(db, config)
    await h.engine.monitor_tick()
    h.clock.advance(minutes=11)  # flat and quiet, e.g. before the first entry
    await enter_f(h)
    start = h.day().start_equity
    h.broker.set_price("F", 9.50)  # the bracket stop at 9.80 fills before the next tick
    assert h.broker.pos == {}
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]
    assert h.day().start_equity == pytest.approx(start)


@pytest.mark.parametrize("kind", ["option", "short"])
async def test_holdings_the_bot_does_not_trade_are_no_cash_flow(db, config, kind) -> None:
    """Review finding: an option (or a short) in the account was invisible to the bot, so
    the account looked flat and every move of its value was taken for a deposit or a
    withdrawal: an alert every few minutes and a drifting start of day."""
    from ib_async import Option, PortfolioItem
    from ib_async import Position as IBPosition

    from tests.test_ibkr import NOW as IB_NOW
    from tests.test_ibkr import PAPER, av, connected_broker, stock

    broker, fake = await connected_broker()
    held = Option("AAPL", "20261120", 200, "C", conId=7) if kind == "option" else stock()
    qty = 2.0 if kind == "option" else -10.0
    fake.portfolio_items = [PortfolioItem(held, qty, 5.0, 1000.0, 4.0, 200.0, 0.0, PAPER)]
    fake.position_items = [IBPosition(PAPER, held, qty, 400.0)]
    equity = 50_000.0
    fake.summary = [av("NetLiquidation", str(equity)), av("TotalCashValue", "49000")]
    notes, clock = RecordingNotifier(), FakeClock(IB_NOW)
    engine = Engine(config, db, broker, FakeDecisionMaker(), notifier=notes, clock=clock)
    await engine.monitor_tick()
    for _ in range(10):  # its value moves; IBKR pushes NetLiquidation every 3 minutes
        clock.advance(minutes=3)
        equity -= 150.0
        fake.summary = [av("NetLiquidation", str(equity)), av("TotalCashValue", "49000")]
        await engine.monitor_tick()
    assert not [a for a in db.list_audit() if a["action"] == "cash_flow"]
    assert "Balance change detected" not in notes.titles()


async def test_the_loss_limit_after_a_resume_while_up_is_where_the_halt_fires(db, config) -> None:
    """Review finding: after a resume on a day up more than the loss limit, the status
    reported no loss limit (clamped at 0) and the dashboard fell back to one measured
    from the day's start: twice the real room before the halt closes everything."""
    h = await harness(db, config, native=False)
    await enter_f(h)  # 24 F @ 10.01
    h.broker.set_price("F", 11.60)  # up about 7.6% of the day's start
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    await h.engine.kill()
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    await h.engine.resume()
    h.clock.advance(minutes=1)
    await h.engine.monitor_tick()
    s = h.engine.status()
    baseline = float(db.get_kv("halt_baseline:" + s.day))
    floor = baseline * (1 - h.engine.risk.limits.max_daily_loss_pct / 100)
    assert s.loss_limit_usd == pytest.approx(s.start_equity - floor, abs=0.01)
    assert s.loss_limit_usd < 0  # the halt fires while the day is still up


async def test_held_symbols_get_quote_lines_first_and_never_more_than_there_are(db, config) -> None:
    """Review finding: a held symbol outside a full universe made each cycle ask for more
    symbols than quote lines, so every line was reopened every cycle."""
    from trader.models import MAX_QUOTE_STREAMS

    h = await harness(db, config, stock_universe=[f"S{i}" for i in range(MAX_QUOTE_STREAMS)])
    held = Instrument(symbol="HELD")
    h.engine._positions = [Position(instrument=held, qty=5, avg_cost=10.0)]
    universe = await h.engine._universe(h.clock())
    assert len(universe) == MAX_QUOTE_STREAMS
    assert universe[0].key == held.key


@pytest.mark.parametrize("restart_app", [False, True])
async def test_a_paper_reset_while_away_never_halts(db, config, restart_app) -> None:
    """Review finding: a paper reset that landed while the gateway (or the app) was away
    tripped the daily loss halt, with a flatten and an alert, ten minutes before the
    untraded-day check booked it as a balance change; the day then stayed halted."""
    night = datetime(2026, 10, 21, 4, 10, tzinfo=UTC)  # Wednesday 00:10 New York
    h = await harness(db, config, mode=Mode.PAPER, now=night)
    h.broker.cash = 1_000_000.0
    await h.engine.monitor_tick()
    h.clock.advance(minutes=11)
    await h.engine.monitor_tick()
    h.broker._connected = h.broker.can_connect = False  # the gateway goes away
    with pytest.raises(BrokerError):
        await h.engine.monitor_tick()
    h.broker.cash = 50_000.0  # the reset lands meanwhile
    h.clock.advance(minutes=5)
    h.broker.can_connect = True
    engine = h.engine
    if restart_app:
        engine = Engine(
            config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock, scanner=h.scanner
        )
    for _ in range(60):  # 15 minutes
        with contextlib.suppress(BrokerError):
            await engine.monitor_tick()
        h.clock.advance(seconds=15)
    day = db.get_day("2026-10-21")
    assert not day.halted
    assert day.start_equity == pytest.approx(50_000.0)
    assert "Trading halted" not in h.notes.titles()


async def test_resume_waits_for_the_account(db, config) -> None:
    """Review finding: Resume pressed before the broker reconnected wrote no new loss
    baseline, so once connected the bot halted (and flattened) again from the day's start."""
    h = await harness(db, config)
    await h.engine.kill()
    h.engine._account = None  # restarted, the gateway still logging in
    with pytest.raises(ValueError, match="not connected"):
        await h.engine.resume()
    assert h.day().halted


async def test_an_approved_exit_needs_no_quote(db, config) -> None:
    """Review finding: approving the AI's CLOSE fetched a quote first, so with no market
    data (IBKR 10197: another session of the login) every approval failed until it
    expired, although an exit never uses the quote."""
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.engine.settings.autopilot = False
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    [waiting] = h.orders(OrderStatus.PENDING_APPROVAL)

    async def no_data(instrument):
        raise BrokerError("no IBKR market data for F: another session holds it (10197)")

    h.broker.quote = no_data
    rec = await h.engine.approve_order(waiting.id)
    assert rec.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED)
    assert "STK:F" not in h.broker.pos


async def test_a_kill_that_never_reached_the_broker_leaves_close_available(db, config) -> None:
    """Review finding: a KILL whose connect failed kept every position marked closing for a
    minute: Close was refused as "already being closed" with nothing selling."""
    h = await harness(db, config)
    await enter_f(h)
    await h.engine.monitor_tick()
    h.broker._connected = h.broker.can_connect = False
    await h.engine.kill()
    assert "STK:F" in h.broker.pos
    h.broker.can_connect = True
    await h.broker.connect()
    h.clock.advance(seconds=20)
    rec = await h.engine.close_position("STK:F")
    assert rec.request.side == Side.SELL


async def test_the_pnl_is_unknown_while_the_account_is(db, config) -> None:
    """Review finding: with the account unknown (gateway down after a restart) the status
    said P&L $0.00, and the dashboard "No daily loss budget used"."""
    h = await harness(db, config)
    await h.engine.monitor_tick()
    h.engine._account = None
    assert h.engine.status().pnl_today is None


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


async def test_execution_correction_replaces_the_journaled_fill(db, config) -> None:
    """BRK-6: IBKR corrects an execution with a new id that differs only after the last
    period. The journal replaces the stored fill instead of adding a second one, so the
    round trip is 24 shares (not 48) at the corrected price and the fees count once."""
    h = await harness(db, config)
    base = "0000e0d5.65a1b2c3.01"
    await enter_f(h)
    original = h.broker.fills[0].model_copy(update={"exec_id": f"{base}.01"})
    h.broker.fills[0] = original
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert [f.exec_id for f in db.list_fills()] == [f"{base}.01"]
    h.broker.fills.append(original.model_copy(update={"exec_id": f"{base}.02", "price": 10.02}))
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    [buy_fill] = [f for f in db.list_fills() if f.side == Side.BUY]
    assert buy_fill.exec_id == f"{base}.02" and buy_fill.price == 10.02
    h.broker.set_price("F", 9.70)  # the bracket stop fills
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    [trade] = db.list_trades()
    assert trade.qty == 24 and trade.entry_price == pytest.approx(10.02)
    assert h.day().fees == pytest.approx(0.70)
    h.clock.advance(minutes=3)
    await h.engine.monitor_tick()
    assert db.get_position_meta("STK:F") is None  # no phantom lot keeps it alive


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


async def test_monitor_tick_writes_a_heartbeat_after_checking_exits(db, config) -> None:
    h = await harness(db, config)
    assert db.get_kv(HEARTBEAT_KEY) is None
    await h.engine.monitor_tick()
    assert db.get_kv(HEARTBEAT_KEY) == h.clock().isoformat()


async def test_a_failed_exit_check_does_not_refresh_the_heartbeat(db, config) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    first = db.get_kv(HEARTBEAT_KEY)
    h.clock.advance(seconds=30)

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise BrokerError("exits down")

    h.engine._manage_positions = boom  # type: ignore[method-assign]
    await h.engine.monitor_tick()  # the monitor records the failure and continues
    assert db.get_kv(HEARTBEAT_KEY) == first


async def test_watchdog_sell_from_another_client_is_in_the_trade_history(db, config) -> None:
    """The watchdog sells with its own IB client (id 18). That order is not in this
    process's journal. IBKR's execution list is for the whole account, so the fill
    comes back with the other client's perm id and the round trip is still recorded.
    """
    h = await harness(db, config)
    await enter_f(h)
    [buy_fill] = [f for f in h.broker.fills if f.side == Side.BUY]
    qty = buy_fill.qty
    sell_price = 9.7
    h.clock.advance(seconds=30)
    # Client 18 already sold. This process only sees the execution and a flat position.
    h.broker.cash += qty * sell_price
    h.broker.pos.clear()
    h.broker.fills.append(
        Fill(
            exec_id="wd-client-18",
            broker_order_id="perm:90018",
            instrument=buy_fill.instrument,
            side=Side.SELL,
            qty=qty,
            price=sell_price,
            commission=h.broker.fee,
            ts=h.clock(),
        )
    )
    await h.engine.monitor_tick()
    [trade] = db.list_trades()
    expected = (sell_price - buy_fill.price) * qty - buy_fill.commission - h.broker.fee
    assert trade.qty == pytest.approx(qty)
    assert trade.exit_price == pytest.approx(sell_price)
    assert trade.pnl == pytest.approx(round(expected, 4), abs=0.001)
    assert db.get_order_by_broker_id("perm:90018") is None
    assert any(f.exec_id == "wd-client-18" and f.side == Side.SELL for f in db.list_fills())


async def test_start_monitor_protects_without_deciding(db, config) -> None:
    h = await harness(db, config)

    async def idle_sleep(delay: float) -> None:
        await asyncio.sleep(3600)

    h.engine._sleep = idle_sleep
    await h.broker.disconnect()
    await h.engine.start_monitor()
    await h.engine.start_monitor()  # idempotent
    assert h.broker.connected and not h.engine.status().running
    assert not db.get_flag("engine_running")
    assert h.engine._monitor_task is not None and h.engine._decision_task is None
    await h.engine.start()  # adds the decision loop to the running monitor
    assert h.engine.status().running and h.engine._decision_task is not None
    await h.engine.shutdown()
    assert h.engine._monitor_task is None


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


async def test_quotes_that_tick_during_the_fetch_are_not_from_the_future(db, config) -> None:
    """On IBKR a quote's timestamp is when its latest tick arrived, so a live quote fetched
    a few seconds into a round is newer than the round's start. The eligibility check, the
    send check and the dashboard must judge it by when it arrived, not reject it as a quote
    from the future (which blocked every entry fetched late in the round)."""
    h = await harness(db, config)
    original = h.broker.quote

    async def slow_quote(inst):
        h.clock.advance(seconds=2)  # other symbols' bars, a fresh subscription...
        return await original(inst)  # stamped with the arrival time

    h.broker.quote = slow_quote
    rec = await enter_f(h)
    assert rec.status == OrderStatus.FILLED
    assert not any("in the future" in (s.skip_reason or "") for s in db.list_signals())
    health = h.engine.status().quote_health
    assert health and all(q.valid for q in health), health


async def test_a_slow_fetch_of_another_symbol_does_not_make_a_quote_stale(db, config) -> None:
    """Review finding on the fix above: judging every quote at the end of the whole fetch
    made an early quote look stale when another symbol's bars were slow."""
    h = await harness(db, config)
    original = h.broker.bars

    async def slow_bars(inst, *args, **kwargs):
        if inst.symbol == "T":
            h.clock.advance(seconds=35)  # one slow historical-data request
        return await original(inst, *args, **kwargs)

    h.broker.bars = slow_bars
    rec = await enter_f(h)
    assert rec.status == OrderStatus.FILLED
    assert not any("stale" in (s.skip_reason or "") for s in db.list_signals())


async def test_research_replays_a_round_quote_as_old_as_it_was_on_arrival(db, config) -> None:
    """Review finding: the captured frame takes the round's last arrival as its time, so
    the replay must not judge an earlier quote as if it had arrived then too."""
    h = await harness(db, config)
    original = h.broker.quote

    async def late_quote(inst):
        if inst.symbol == "T":
            h.clock.advance(seconds=35)  # T's quote arrives late in the round
        return await original(inst)

    h.broker.quote = late_quote
    rec = await enter_f(h)
    assert rec.status == OrderStatus.FILLED
    frame = ReplayFrame.model_validate(db.list_research_frames()[-1])
    assert frame.ts - frame.quoted_at["STK:F"] >= timedelta(seconds=35)
    seen = frame.replay_quotes()["STK:F"]
    assert frame.ts - seen.ts == frame.quoted_at["STK:F"] - frame.quotes["STK:F"].ts
    assert frame.ts - seen.ts <= timedelta(seconds=2 * h.engine.settings.monitor_seconds)


async def test_a_monitor_requote_during_the_decision_keeps_the_round_arrival(db, config) -> None:
    """Review finding: the monitor runs beside the round and can re-quote a symbol while
    the AI decides. The captured frame keeps when the round's own quote arrived."""
    h = await harness(db, config)
    original = h.decider.decide

    async def slow_decide(ctx):
        h.clock.advance(seconds=40)  # a long AI call; the monitor re-quotes T meanwhile
        quote = await h.broker.quote(Instrument(symbol="T"))
        h.engine._track_halt("STK:T", quote, h.clock())
        return await original(ctx)

    h.decider.decide = slow_decide  # type: ignore[method-assign]
    await enter_f(h)
    frame = ReplayFrame.model_validate(db.list_research_frames()[-1])
    assert frame.quoted_at["STK:T"] == NOW
    assert frame.ts - frame.replay_quotes()["STK:T"].ts == timedelta(0)
