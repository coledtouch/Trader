"""Crypto stop watchdog. Offline: a fake broker, no gateway, no network."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trader.broker.base import BrokerError
from trader.cli import build_parser, cmd_watchdog
from trader.config import AppConfig
from trader.db import Database
from trader.models import (
    AssetClass,
    BotSettings,
    DayStats,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)
from trader.watchdog import (
    FLAT_CONFIRM_POLLS,
    HEARTBEAT_KEY,
    STALE_IDLE_TITLE,
    STOP_CONFIRM_TICKS,
    TAG_PREFIX,
    WATCHDOG_CLOSED_PREFIX,
    CryptoWatchdog,
    WatchdogSettings,
    effective_timeouts,
    new_order_tag,
    orders_allowed,
    read_heartbeat,
    run_watchdog,
    write_heartbeat,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # Wednesday, New York afternoon


class Clock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.t = moment

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class Notes:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: object = None,
    ) -> bool:
        self.sent.append(title)
        return True


class FakeBroker:
    """Just enough of a broker for the watchdog: positions, quotes and sells."""

    def __init__(self, mode: Mode = Mode.PAPER) -> None:
        self._mode = mode
        self._connected = True
        self.positions_list: list[Position] = []
        self.orders: list[OrderRecord] = []
        self.quotes: dict[str, Quote] = {}
        self.placed: list[OrderRequest] = []
        self.records: dict[str, OrderRecord] = {}  # every placed order, by broker id
        self.fail_quote = False
        self.reject = False
        self.connects = 0

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connects += 1
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def positions(self) -> list[Position]:
        return list(self.positions_list)

    async def open_orders(self) -> list[OrderRecord]:
        return list(self.orders)

    async def quote(self, instrument: Instrument) -> Quote:
        if self.fail_quote:
            raise BrokerError("no quote")
        return self.quotes[instrument.key]

    async def place_order(self, req: OrderRequest) -> OrderRecord:
        self.placed.append(req)
        status = OrderStatus.REJECTED if self.reject else OrderStatus.SUBMITTED
        record = OrderRecord(
            request=req,
            status=status,
            broker_order_id=f"B{len(self.placed)}",
            message="rejected" if self.reject else "working",
        )
        self.records[record.broker_order_id or ""] = record
        if status == OrderStatus.SUBMITTED:
            self.orders.append(record)
        return record

    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        return self.records.get(broker_order_id)

    def round_qty(
        self, instrument: Instrument, qty: float, allow_fractional: bool = False
    ) -> float:
        if qty <= 0:
            return 0.0
        return math.floor(qty * 1_000_000 + 1e-9) / 1_000_000


def settings(**overrides: object) -> WatchdogSettings:
    base: dict[str, object] = {
        "stale_seconds": 45,
        "flatten_seconds": 180,
        "retry_seconds": 20,
        "orders": "auto",
        "allow_live": False,
    }
    base.update(overrides)
    return WatchdogSettings(**base)  # type: ignore[arg-type]


def crypto(symbol: str = "BTC") -> Instrument:
    return Instrument(symbol=symbol, asset_class=AssetClass.CRYPTO)


def q(instrument: Instrument, bid: float, last: float) -> Quote:
    return Quote(instrument=instrument, bid=bid, ask=bid + 0.05, last=last, ts=NOW)


def hold(
    db: Database,
    broker: FakeBroker,
    symbol: str = "BTC",
    *,
    qty: float = 0.01,
    stop: float = 95.0,
    bid: float = 100.0,
    last: float = 100.0,
    meta: bool = True,
    asset_class: AssetClass = AssetClass.CRYPTO,
) -> Instrument:
    instrument = Instrument(symbol=symbol, asset_class=asset_class)
    broker.positions_list.append(Position(instrument=instrument, qty=qty, avg_cost=100.0))
    broker.quotes[instrument.key] = q(instrument, bid, last)
    if meta:
        db.set_position_meta(instrument.key, stop_loss=stop, take_profit=110.0, entry_price=100.0)
    return instrument


def beat(db: Database, clock: Clock, age: float) -> None:
    write_heartbeat(db, clock.t - timedelta(seconds=age))


def make(
    db: Database,
    broker: FakeBroker,
    clock: Clock,
    *,
    started_at: datetime | None = None,
    **overrides: object,
) -> tuple[CryptoWatchdog, Notes]:
    notes = Notes()
    watchdog = CryptoWatchdog(
        db,
        broker,
        settings(**overrides),
        notes,
        clock,
        started_at=started_at if started_at is not None else clock.t - timedelta(hours=1),
    )
    return watchdog, notes


def sells(broker: FakeBroker) -> list[OrderRequest]:
    return broker.placed


# ---------------------------------------------------------------------- heartbeat
def test_heartbeat_round_trip(db: Database) -> None:
    assert read_heartbeat(db) is None
    write_heartbeat(db, NOW)
    assert read_heartbeat(db) == NOW
    db.set_kv(HEARTBEAT_KEY, "not-a-timestamp")
    assert read_heartbeat(db) is None


def test_effective_timeouts_follow_the_monitor_interval() -> None:
    assert effective_timeouts(45, 180, 15) == (45.0, 180.0, None)
    stale, flatten, warning = effective_timeouts(45, 180, 300)
    assert stale == 900
    assert flatten == 960
    assert warning is not None and "900" in warning and "960" in warning


def test_order_tags_are_unique_and_named() -> None:
    one, two = new_order_tag("BTC", "stop"), new_order_tag("ETH", "flatten")
    assert one != two
    assert one.startswith(f"{TAG_PREFIX}BTC-stop-")
    assert two.startswith(f"{TAG_PREFIX}ETH-flatten-")
    assert one != "ai-trader" and two != "ai-trader"


# ---------------------------------------------------------------------- healthy app
async def test_fresh_heartbeat_does_not_sell_through_a_stop(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    beat(db, clock, age=5)
    watchdog, notes = make(db, broker, clock)
    for _ in range(STOP_CONFIRM_TICKS):
        await watchdog.tick()
    assert sells(broker) == []
    assert notes.sent == []


async def test_stale_heartbeat_sells_only_after_the_stop_is_confirmed(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    beat(db, clock, age=60)  # past stale (45s), not yet the flatten timeout (180s)
    watchdog, notes = make(db, broker, clock)
    first = await watchdog.tick()
    assert sells(broker) == [] and first.closes == []
    second = await watchdog.tick()
    assert len(sells(broker)) == 1
    order = sells(broker)[0]
    assert order.side is Side.SELL and order.is_exit and order.order_type is OrderType.MARKET
    assert order.qty == pytest.approx(0.01)
    assert order.trace_id is not None and order.trace_id.startswith(f"{TAG_PREFIX}BTC-stop-")
    assert "stop-loss" in order.reason
    assert second.closes == [order]
    assert notes.sent == ["Crypto watchdog: crypto stop"]
    audit = db.list_audit(5)
    assert audit[0]["actor"] == "watchdog" and audit[0]["action"] == "watchdog_close"
    assert audit[0]["detail"]["tag"] == order.trace_id


async def test_stale_heartbeat_above_the_stop_waits(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=100.0, last=100.0, stop=95.0)
    beat(db, clock, age=60)
    watchdog, _notes = make(db, broker, clock)
    for _ in range(3):
        await watchdog.tick()
    assert sells(broker) == []


async def test_long_outage_flattens_even_above_the_stop(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, symbol="BTC", bid=100.0, last=100.0, stop=95.0)
    hold(db, broker, symbol="ETH", bid=100.0, last=100.0, stop=90.0)
    beat(db, clock, age=200)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()
    assert len(sells(broker)) == 2
    tags = {order.trace_id for order in sells(broker)}
    assert len(tags) == 2
    assert all(tag and tag.startswith(TAG_PREFIX) and "-flatten-" in tag for tag in tags)
    assert {order.instrument.symbol for order in sells(broker)} == {"BTC", "ETH"}
    assert all(order.side is Side.SELL and order.is_exit for order in sells(broker))
    assert notes.sent == ["Crypto watchdog: flattening crypto"]


async def test_missing_heartbeat_is_grace_until_this_process_has_waited(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    watchdog, _notes = make(db, broker, clock, started_at=clock.t)
    for _ in range(STOP_CONFIRM_TICKS):
        await watchdog.tick()
    assert sells(broker) == []


async def test_missing_heartbeat_past_grace_flattens(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=100.0, last=100.0, stop=95.0)
    watchdog, _notes = make(db, broker, clock, started_at=clock.t - timedelta(seconds=200))
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert sells(broker)[0].trace_id and "-flatten-" in sells(broker)[0].trace_id


# ---------------------------------------------------------------------- kill / modes
def arm_kill(db: Database, day: str = "2026-10-21") -> None:
    db.upsert_day(DayStats(day=day, halted=True, halt_reason="kill switch"))


async def test_kill_switch_flattens_while_the_app_heartbeat_is_fresh(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=100.0, last=100.0, stop=95.0)
    beat(db, clock, age=1)
    arm_kill(db)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert "-kill-" in (sells(broker)[0].trace_id or "")
    assert notes.sent == ["Crypto watchdog: kill switch"]


async def test_kill_from_the_previous_day_still_flattens_if_today_has_no_row(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker)
    beat(db, clock, age=1)
    arm_kill(db, day="2026-10-20")
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert len(sells(broker)) == 1


async def test_a_normal_halt_today_does_not_inherit_yesterdays_kill(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=100.0, last=100.0, stop=95.0)
    beat(db, clock, age=1)
    arm_kill(db, day="2026-10-20")
    db.upsert_day(DayStats(day="2026-10-21", halted=True, halt_reason="daily loss limit"))
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert sells(broker) == []


async def test_a_working_sell_is_not_duplicated(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    instrument = hold(db, broker)
    beat(db, clock, age=1)
    arm_kill(db)
    broker.orders.append(
        OrderRecord(
            request=OrderRequest(
                instrument=instrument,
                side=Side.SELL,
                qty=0.01,
                order_type=OrderType.MARKET,
                is_exit=True,
                trace_id="ai-trader-wd-BTC-kill-already",
            ),
            status=OrderStatus.SUBMITTED,
            broker_order_id="WORKING",
        )
    )
    watchdog, _notes = make(db, broker, clock)
    result = await watchdog.tick()
    assert sells(broker) == []
    assert any("already working" in line for line in result.skipped)


async def test_a_partial_working_sell_reduces_only_the_remainder(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    instrument = hold(db, broker, qty=0.02)
    beat(db, clock, age=200)
    broker.orders.append(
        OrderRecord(
            request=OrderRequest(
                instrument=instrument,
                side=Side.SELL,
                qty=0.01,
                order_type=OrderType.MARKET,
                is_exit=True,
                trace_id="ai-trader-42",
            ),
            status=OrderStatus.SUBMITTED,
            broker_order_id="WORKING",
        )
    )
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert sells(broker)[0].qty == pytest.approx(0.01)


async def test_an_owners_sell_covers_nothing(db: Database) -> None:
    """Review finding: any working sell counted as cover, so an owner's limit sell from IBKR
    Mobile far above the market kept the watchdog from selling a bot position at its stop
    (or on flatten or kill) while the app was down."""
    clock = Clock()
    broker = FakeBroker()
    instrument = hold(db, broker, qty=0.02)
    beat(db, clock, age=200)
    broker.orders.append(
        OrderRecord(
            request=OrderRequest(
                instrument=instrument,
                side=Side.SELL,
                qty=0.02,
                order_type=OrderType.LIMIT,
                limit_price=150_000.0,
                is_exit=True,
            ),
            status=OrderStatus.SUBMITTED,
            broker_order_id="perm:555",
        )
    )
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert sells(broker)[0].qty == pytest.approx(0.02)


async def test_rejected_close_is_not_retried_until_the_window_passes(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    broker.reject = True
    hold(db, broker)
    beat(db, clock, age=200)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert notes.sent.count("Crypto watchdog: flattening crypto") == 1
    clock.advance(seconds=20)
    await watchdog.tick()
    assert len(sells(broker)) == 2
    assert sells(broker)[0].trace_id != sells(broker)[1].trace_id
    assert notes.sent.count("Crypto watchdog: flattening crypto") == 2


async def test_paper_sells_when_live_trading_is_not_allowed(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker(Mode.PAPER)
    hold(db, broker)
    beat(db, clock, age=200)
    watchdog, _notes = make(db, broker, clock, allow_live=False)
    await watchdog.tick()
    assert len(sells(broker)) == 1


async def test_live_without_the_gate_alerts_and_does_not_sell(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker(Mode.LIVE)
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    beat(db, clock, age=200)
    watchdog, notes = make(db, broker, clock, allow_live=False)
    await watchdog.tick()
    assert sells(broker) == []
    assert notes.sent == [
        "Crypto watchdog: flattening crypto",
        "Crypto watchdog: not sending orders",
    ]


async def test_live_with_the_gate_open_sells_in_shadow_too(db: Database) -> None:
    """Shadow mode (live, not armed) still exits. The watchdog does not read the arm flag."""
    clock = Clock()
    broker = FakeBroker(Mode.LIVE)
    hold(db, broker)
    beat(db, clock, age=200)
    db.set_flag("live_armed", False)
    watchdog, _notes = make(db, broker, clock, allow_live=True)
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert sells(broker)[0].side is Side.SELL


async def test_watch_only_never_sends(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker(Mode.PAPER)
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    beat(db, clock, age=200)
    arm_kill(db)
    watchdog, notes = make(db, broker, clock, orders="off")
    await watchdog.tick()
    assert sells(broker) == []
    assert "Crypto watchdog: kill switch" in notes.sent
    assert "Crypto watchdog: not sending orders" in notes.sent


async def test_simulator_mode_never_sends(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker(Mode.SIM)
    hold(db, broker)
    beat(db, clock, age=200)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()
    assert sells(broker) == []
    assert "Crypto watchdog: not sending orders" in notes.sent


def test_orders_allowed_matches_the_modes() -> None:
    auto = settings()
    assert orders_allowed(Mode.PAPER, auto)[0] is True
    assert orders_allowed(Mode.SIM, auto)[0] is False
    assert orders_allowed(Mode.LIVE, auto)[0] is False
    assert orders_allowed(Mode.LIVE, settings(allow_live=True))[0] is True
    assert orders_allowed(Mode.PAPER, settings(orders="off"))[0] is False


# ---------------------------------------------------------------------- what it will not touch
async def test_stocks_and_unmanaged_crypto_are_left_alone(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, symbol="F", asset_class=AssetClass.STOCK, bid=9.0, last=9.0, stop=10.0)
    hold(db, broker, symbol="ETH", meta=False, bid=1.0, last=1.0, stop=2.0)
    beat(db, clock, age=500)
    arm_kill(db)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert sells(broker) == []


async def test_a_bad_tick_does_not_trip_the_stop(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=50.0, last=100.0, stop=95.0)  # bid is far from the last trade
    beat(db, clock, age=60)
    watchdog, _notes = make(db, broker, clock)
    for _ in range(STOP_CONFIRM_TICKS + 1):
        await watchdog.tick()
    assert sells(broker) == []


async def test_flatten_does_not_need_a_quote(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    broker.fail_quote = True
    hold(db, broker, bid=100.0, last=100.0, stop=95.0)
    beat(db, clock, age=200)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert len(sells(broker)) == 1


async def test_a_slow_monitor_does_not_look_like_a_dead_app(db: Database, caplog) -> None:
    """The dashboard can set the exit check to 300s. A 200s-old heartbeat is then healthy."""
    db.save_settings(BotSettings(monitor_seconds=300))
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    beat(db, clock, age=200)  # past the configured 45s and 180s
    watchdog, notes = make(db, broker, clock)
    caplog.set_level("WARNING")
    for _ in range(3):
        await watchdog.tick()
    assert sells(broker) == []
    assert notes.sent == []
    warnings = [r.getMessage() for r in caplog.records if "raised to match" in r.getMessage()]
    assert warnings == [warnings[0]]


async def test_flatten_waits_until_the_monitor_floor_plus_a_margin(db: Database) -> None:
    db.save_settings(BotSettings(monitor_seconds=100))  # stale floor 300s, flatten floor 360s
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=100.0, last=100.0, stop=95.0)
    beat(db, clock, age=320)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    assert sells(broker) == []
    beat(db, clock, age=400)
    await watchdog.tick()
    assert len(sells(broker)) == 1
    assert "-flatten-" in (sells(broker)[0].trace_id or "")


async def test_stale_heartbeat_with_nothing_to_sell_alerts_once(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    beat(db, clock, age=60)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()  # first look: noted, not yet repeated
    assert notes.sent == []
    await watchdog.tick()
    await watchdog.tick()
    assert notes.sent == [STALE_IDLE_TITLE]
    beat(db, clock, age=1)
    await watchdog.tick()
    assert notes.sent.count("Crypto watchdog: app heartbeat restored") == 1
    beat(db, clock, age=60)
    await watchdog.tick()
    await watchdog.tick()
    assert notes.sent.count(STALE_IDLE_TITLE) == 2


async def test_heartbeat_restored_is_reported_once(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker)
    beat(db, clock, age=200)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()
    beat(db, clock, age=1)
    broker.positions_list.clear()
    await watchdog.tick()
    await watchdog.tick()
    assert notes.sent.count("Crypto watchdog: app heartbeat restored") == 1


# ---------------------------------------------------------------------- process
async def test_run_watchdog_retries_a_down_gateway(db: Database) -> None:
    class Down:
        mode = Mode.PAPER
        connected = False

        async def connect(self) -> None:
            raise BrokerError("gateway down")

    config = AppConfig(_env_file=None, broker="ibkr", watchdog_poll_seconds=10)
    await run_watchdog(
        config,
        db,
        Down(),
        Notes(),
        clock=lambda: NOW,
        max_ticks=2,  # type: ignore[arg-type]
    )


async def test_run_watchdog_one_tick_then_stops(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    broker._connected = False
    hold(db, broker)
    beat(db, clock, age=1)
    config = AppConfig(_env_file=None, broker="ibkr", watchdog_poll_seconds=10)
    await run_watchdog(config, db, broker, Notes(), clock=clock, max_ticks=1)  # type: ignore[arg-type]
    assert broker.connects == 1
    assert sells(broker) == []


def test_watchdog_command_is_registered() -> None:
    args = build_parser().parse_args(["watchdog", "--env-file", "missing.env"])
    assert args.func is cmd_watchdog


def test_flatten_timeout_must_follow_the_stale_timeout() -> None:
    with pytest.raises(ValidationError, match="TRADER_WATCHDOG_FLATTEN_SECONDS"):
        AppConfig(_env_file=None, watchdog_stale_seconds=50, watchdog_flatten_seconds=40)


def test_watchdog_client_id_must_differ_from_the_app() -> None:
    with pytest.raises(ValidationError, match="TRADER_WATCHDOG_CLIENT_ID"):
        AppConfig(_env_file=None, ib_client_id=7, watchdog_client_id=7)


# ---------------------------------------------------------------------- gateway outages
async def test_gateway_outage_gives_the_app_the_flatten_window_after_reconnect(
    db: Database,
) -> None:
    """Review finding: after an IB Gateway outage longer than the flatten timeout, the
    watchdog reconnected before the app (whose reconnect backoff can be 120s) and
    market-sold healthy crypto far above its stop. A blanket flatten now also waits the
    flatten window from when this process's connection came back."""
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=110.0, last=110.0, stop=95.0)
    beat(db, clock, age=0)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    watchdog.note_broker_down()  # what run_watchdog does on a BrokerError
    clock.advance(seconds=245)  # the gateway is back; the app has not ticked yet
    for _ in range(18):  # polls every 10s for 170s after the reconnect
        await watchdog.tick()
        clock.advance(seconds=10)
    assert sells(broker) == []
    clock.advance(seconds=10)  # 190s since the reconnect and still no heartbeat: app dead
    await watchdog.tick()
    assert len(sells(broker)) == 1 and "-flatten-" in (sells(broker)[0].trace_id or "")


async def test_missed_polls_count_as_a_reconnect(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=110.0, last=110.0, stop=95.0)
    beat(db, clock, age=0)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    clock.advance(seconds=245)  # no successful poll for minutes (e.g. IBKR 1100/1102)
    await watchdog.tick()
    assert sells(broker) == []


async def test_a_stop_breach_is_not_delayed_by_a_reconnect(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    hold(db, broker, bid=90.0, last=90.0, stop=95.0)
    beat(db, clock, age=0)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    watchdog.note_broker_down()
    clock.advance(seconds=245)
    for _ in range(STOP_CONFIRM_TICKS):
        await watchdog.tick()
        clock.advance(seconds=10)
    assert len(sells(broker)) == 1 and "-stop-" in (sells(broker)[0].trace_id or "")


def _filled(broker: FakeBroker) -> None:
    """The watchdog's sells filled: the bot's crypto is gone."""
    for order_id, record in broker.records.items():
        if record.status == OrderStatus.SUBMITTED:
            broker.records[order_id] = record.model_copy(
                update={"status": OrderStatus.FILLED, "filled_qty": record.request.qty}
            )
    broker.positions_list.clear()
    broker.orders.clear()


@pytest.mark.parametrize(("age", "bid"), [(60, 94.0), (200, 100.0)])  # stop, then flatten
async def test_a_buy_after_the_watchdog_sold_the_bot_position_is_not_the_bots(
    db: Database, age: float, bid: float
) -> None:
    """Review finding: while the app was down the watchdog sold the bot's BTC, but the
    position's meta stayed (only the app deletes it). The owner then bought BTC in IBKR
    Mobile, and the next poll sold it as the bot's, without even confirming the stop."""
    clock = Clock()
    broker = FakeBroker()
    instrument = hold(db, broker, qty=0.02, bid=bid, last=bid)
    beat(db, clock, age=age)
    watchdog, _notes = make(db, broker, clock)
    for _ in range(STOP_CONFIRM_TICKS):
        await watchdog.tick()
        clock.advance(seconds=10)
    assert [s.qty for s in sells(broker)] == [pytest.approx(0.02)]
    _filled(broker)
    for _ in range(FLAT_CONFIRM_POLLS):  # flat: the watchdog notes it sold the position
        await watchdog.tick()
        clock.advance(seconds=10)
    broker.positions_list.append(Position(instrument=instrument, qty=0.5, avg_cost=94.0))
    for _ in range(3):
        await watchdog.tick()
        clock.advance(seconds=30)
    assert len(sells(broker)) == 1


async def test_a_new_bot_entry_after_a_watchdog_sale_is_managed_again(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    instrument = hold(db, broker, qty=0.02, bid=94.0, last=94.0)
    beat(db, clock, age=60)
    watchdog, _notes = make(db, broker, clock)
    for _ in range(STOP_CONFIRM_TICKS):
        await watchdog.tick()
        clock.advance(seconds=10)
    _filled(broker)
    await watchdog.tick()
    clock.advance(seconds=10)
    # The app came back and bought again: its new entry rewrites the meta.
    db.set_position_meta(
        instrument.key, stop_loss=95.0, take_profit=110.0, entry_price=100.0, opened_at=NOW
    )
    broker.positions_list.append(Position(instrument=instrument, qty=0.03, avg_cost=100.0))
    for _ in range(STOP_CONFIRM_TICKS):
        await watchdog.tick()
        clock.advance(seconds=10)
    assert [s.qty for s in sells(broker)] == [pytest.approx(0.02), pytest.approx(0.03)]


def _app_entry(instrument: Instrument, qty: float) -> OrderRecord:
    """The rest of a part-filled crypto entry of the app's, still working."""
    return OrderRecord(
        request=OrderRequest(
            instrument=instrument,
            side=Side.BUY,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=100.0,
            trace_id="ai-trader-17",
        ),
        status=OrderStatus.SUBMITTED,
        broker_order_id="perm:17",
    )


async def test_one_flat_read_never_marks_a_bot_position_sold(db: Database) -> None:
    """Review finding: one poll that read the position as flat marked it sold for good.
    The rest of a part-filled entry filled between the reads, so the bot held new coins
    that neither the watchdog nor the app would ever sell again."""
    clock = Clock()
    broker = FakeBroker()
    instrument = hold(db, broker, qty=0.02)
    entry = _app_entry(instrument, 0.03)
    broker.orders.append(entry)
    beat(db, clock, age=200)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()  # flattens the 0.02 held
    _filled(broker)  # the sell filled; the entry's rest fills too, not yet in the positions
    clock.advance(seconds=10)
    await watchdog.tick()
    broker.positions_list.append(Position(instrument=instrument, qty=0.03, avg_cost=100.0))
    clock.advance(seconds=30)
    await watchdog.tick()
    assert not db.get_kv(WATCHDOG_CLOSED_PREFIX + instrument.key)
    assert [s.qty for s in sells(broker)] == [pytest.approx(0.02), pytest.approx(0.03)]


async def test_a_refused_sell_never_marks_the_position_sold(db: Database) -> None:
    """Review finding: a send that failed or was refused still counted as a sale, so empty
    positions reads (e.g. right after a reconnect) marked a held bot position sold."""
    clock = Clock()
    broker = FakeBroker()
    broker.reject = True
    instrument = hold(db, broker, qty=0.02)
    beat(db, clock, age=200)
    watchdog, _notes = make(db, broker, clock)
    await watchdog.tick()
    held = list(broker.positions_list)
    broker.positions_list.clear()  # bad reads: the positions are not there yet
    for _ in range(FLAT_CONFIRM_POLLS + 1):
        clock.advance(seconds=10)
        await watchdog.tick()
    assert not db.get_kv(WATCHDOG_CLOSED_PREFIX + instrument.key)
    broker.positions_list.extend(held)
    broker.reject = False
    clock.advance(seconds=30)
    await watchdog.tick()
    assert sells(broker)[-1].qty == pytest.approx(0.02)
    assert len(sells(broker)) == 2


async def test_kill_then_resume_never_reports_a_restored_heartbeat(db: Database) -> None:
    """Review finding: a kill-switch poll counted as an outage, so pressing Resume after
    KILL sent "app heartbeat restored" although the heartbeat was never stale."""
    clock = Clock()
    broker = FakeBroker()
    beat(db, clock, age=5)
    arm_kill(db)
    watchdog, notes = make(db, broker, clock)
    await watchdog.tick()
    db.upsert_day(DayStats(day="2026-10-21", halted=False, halt_reason=None))
    clock.advance(seconds=10)
    beat(db, clock, age=5)
    await watchdog.tick()
    assert notes.sent == []
