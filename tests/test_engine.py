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

    h.broker.set_price("F", 10.0)  # reopened, but inside the 15 min cooldown
    h.clock.advance(minutes=5)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == []
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    # The dashboard says why, instead of calling the fresh quote usable.
    [f] = [q for q in h.engine.status().quote_health if q.symbol == "F"]
    assert not f.valid and f.problem == "halted 5 min ago (cooldown 15 min)"

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
