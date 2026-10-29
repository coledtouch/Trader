"""The owner's shares next to the bot's: edge cases from review round 40 (failed
executions reads, sales on days IBKR no longer reports, the watchdog's bot-first window,
older position rows)."""

from __future__ import annotations

import contextlib
import itertools
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_engine import _BTC, _hold_btc, _restart_next_morning, enter_f, harness, hold
from tests.test_owner_shares import (
    F,
    _f_with_owner,
    _stops,
    bot_sells,
    owner_buys,
    owner_sells,
    ticks,
)
from tests.test_watchdog import NOW, Clock, FakeBroker, beat, make
from tests.test_watchdog_owner import BOT, setup
from trader.broker.base import BrokerError
from trader.db import Database
from trader.engine import Engine
from trader.models import (
    AssetClass,
    Fill,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TradeAction,
)
from trader.ownership import UNREPORTED_EXIT, note_watchdog_sale
from trader.timeutil import trading_day, trading_day_start
from trader.watchdog import position_marker

KEY = "CRYPTO:BTC"
_ids = itertools.count(1)


async def _failing(since: datetime) -> list[Fill]:
    raise BrokerError("IBKR executions timed out after 10s")


# ----------------------------------------------------------------------------- failed reads
@pytest.mark.parametrize("fail_read", [False, True])
async def test_a_failed_read_after_a_close_sold_nothing_still_reprotects(
    db, config, fail_read: bool
) -> None:
    """The owner presses Close for the bot's 24 while his own TWS stop sells his 50: sized
    again after the cancels, it sells nothing. The next tick's executions read fails once:
    the bot's stop still comes back."""
    h = await _f_with_owner(db, config)
    h.clock.advance(minutes=1)
    real = h.broker.cancel_order
    armed = {"on": True}

    async def cancel_then_owner_sells(oid: str) -> None:
        await real(oid)
        if armed["on"]:
            armed["on"] = False
            owner_sells(h, F, 50, 9.95)

    h.broker.cancel_order = cancel_then_owner_sells
    with pytest.raises(LookupError):
        await h.engine.close_position("STK:F")
    h.broker.cancel_order = real
    if fail_read:
        h.broker.fail.add("fills_since")
    await ticks(h, 1, seconds=15)
    h.broker.fail.discard("fills_since")
    await ticks(h, 3)
    assert _stops(h) == [24]


async def test_close_with_a_failed_read_is_retried_not_called_the_owners(db, config) -> None:
    h = await _f_with_owner(db, config)  # bot 24, owner 50 journaled
    owner_buys(h, F, 10, 10.0)  # not journaled yet
    real = h.broker.fills_since
    h.broker.fills_since = _failing
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    h.broker.fills_since = real
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 24


async def test_an_approval_with_a_failed_read_stays_pending(db, config) -> None:
    h = await _f_with_owner(db, config)
    h.db.save_settings(h.db.get_settings().model_copy(update={"autopilot": False}))
    h.engine.reload_settings()
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    owner_buys(h, F, 10, 10.0)
    real = h.broker.fills_since
    h.broker.fills_since = _failing
    h.clock.advance(seconds=20)
    with pytest.raises(BrokerError):
        await h.engine.approve_order(pending.id)
    assert h.db.get_order(pending.id).status == OrderStatus.PENDING_APPROVAL
    h.broker.fills_since = real
    await h.engine.approve_order(pending.id)
    assert bot_sells(h, "STK:F") == [24]


# ----------------------------------------------------------------------------- unreported days
@pytest.mark.parametrize("own", [0.003, 0.004166])
async def test_the_owners_buy_today_is_never_counted_in_an_earlier_days_sale(
    db, config, own: float
) -> None:
    """The owner closed the bot's BTC by hand overnight (IBKR no longer reports it) and
    bought some for himself this morning: the bot sells none of his new coins."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    h.clock.t = datetime(2026, 10, 29, 3, 30, tzinfo=UTC)
    owner_sells(h, _BTC, mine, 59800.0)
    h.clock.t = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    owner_buys(h, _BTC, own, 60000.0)
    engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 59500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(12):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert bot_sells(h, KEY) == []
    assert h.broker.pos[KEY][0] == pytest.approx(own)


# ----------------------------------------------------------------------------- bot-first window
@pytest.mark.parametrize("owner_sale", [True, False])
async def test_the_owners_sale_after_a_partial_watchdog_sale_leaves_the_bots_rest(
    db, config, owner_sale: bool
) -> None:
    """The watchdog's IOC sold 60% of the bot's BTC; the app came back the same day. A
    later sale of the owner's own coins is his: the bot's rest keeps its stop."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 4, seconds=15)
    h.clock.advance(seconds=70)
    part = round(mine * 0.6, 6)
    h.broker.pos[KEY][0] -= part
    h.broker.cash += part * 58900.0
    h.broker.fills.append(
        Fill(exec_id=f"WD{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=_BTC,
             side=Side.SELL, qty=part, price=58900.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    note_watchdog_sale(db, KEY, position_marker(meta), "B1", part, h.clock(), done=True)
    h.clock.advance(seconds=40)
    h.broker.set_price("BTC", 60100.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 4, seconds=15)
    assert db.get_position_meta(KEY) is not None
    h.broker.set_price("BTC", 60700.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 20, seconds=15)
    if owner_sale:
        owner_sells(h, _BTC, 0.1, 60700.0)
    await ticks(h, 4, seconds=15)
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 6, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(round(mine - part, 6), abs=1e-6)]
    assert h.broker.pos[KEY][0] == pytest.approx(0.4 if owner_sale else 0.5)


# ----------------------------------------------------------------------------- the watchdog
@pytest.mark.parametrize("filled", [0.0, 0.004])
@pytest.mark.parametrize("age,bid", [(60, 90.0), (400, 100.0)])
async def test_an_app_exit_the_journal_fully_shows_leaves_the_watchdog_guarding(
    db: Database, filled: float, age: int, bid: float
) -> None:
    """An earlier IOC exit of the app's sold nothing (or part, journaled); later, with the
    app down, the owner sells some of his coins: the watchdog still sells the bot's rest."""
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.5, journal_owner=True, bid=bid)
    btc = broker.positions_list[0].instrument
    at = NOW - timedelta(minutes=30)
    req = OrderRequest(instrument=btc, side=Side.SELL, qty=BOT, order_type=OrderType.MARKET,
                       is_exit=True, reason="stop-loss", trace_id="ai-trader-77")  # fmt: skip
    db.insert_order(OrderRecord(request=req, status=OrderStatus.CANCELLED, broker_order_id="77",
                                filled_qty=filled, created_at=at, updated_at=at))  # fmt: skip
    if filled:
        db.insert_fill(Fill(exec_id="A1", broker_order_id="77", instrument=btc, side=Side.SELL,
                            qty=filled, price=95.0, ts=at + timedelta(seconds=1)))  # fmt: skip
    rest = round(BOT - filled, 8)
    broker.positions_list[0] = broker.positions_list[0].model_copy(
        update={"qty": round(rest + 0.5 - 0.2, 8)}
    )
    beat(db, clock, age=age)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [round(o.qty, 6) for o in broker.placed] == [round(rest, 6)]


# ----------------------------------------------------------------------------- older rows
async def test_an_older_btc_row_held_across_the_upgrade_is_sold_in_full(db, config) -> None:
    """A position the engine opened before the upgrade (no "qty" in its extra) with an
    8-decimal IBKR quantity: no owner activity, so the stop sells all of it at once."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    qty = 0.00416649
    h.broker.pos[KEY][0] = qty
    db._exec("UPDATE fills SET qty = ? WHERE instrument = ?", (qty, KEY))
    for i, f in enumerate(h.broker.fills):
        if f.instrument.key == KEY:
            h.broker.fills[i] = f.model_copy(update={"qty": qty})
    meta = db.get_position_meta(KEY)
    extra = json.loads(db.get_kv("position_extra:" + KEY))
    unit = extra.pop("unit_risk")
    extra.pop("qty", None)
    db.set_kv("position_extra:" + KEY, json.dumps({**extra, "unit_risk": unit}))
    db.set_position_meta(KEY, stop_loss=meta["stop_loss"], take_profit=meta["take_profit"],
                         entry_price=meta["entry_price"], initial_risk=unit * qty,
                         opened_at=meta["opened_at"], decision_id=meta["decision_id"],
                         setup=meta["setup"])  # fmt: skip
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(4):
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    assert bot_sells(h, KEY) == [pytest.approx(qty, abs=1e-12)]
    assert KEY not in h.broker.pos


# ----------------------------------------------------------------------------- review 41
@pytest.mark.parametrize("own", [0.003, 0.002])
async def test_a_bot_exit_the_broker_forgot_is_never_read_as_the_owners_sale(
    db, config, own: float
) -> None:
    """The bot's crypto exit filled but its ack was lost (row SUBMITTED, fill never
    journaled); the app is back a day later, when the row is forgotten and IBKR no longer
    reports the sale. The shortfall is maybe the bot's: the owner's coins are never sold."""
    h = await _hold_btc(db, config, datetime(2026, 10, 28, 13, 0, tzinfo=UTC))  # Wed 09:00 NY
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, own, 60000.0)
    await ticks(h, 4, seconds=15)  # the owner's buy is journaled
    real = h.broker.place_order
    sent: list[str] = []

    async def lost_ack(req):
        rec = await real(req)
        if req.side == Side.SELL and req.stop_loss is None and not sent:
            sent.append(rec.broker_order_id)
            # IBKR filled it; the gateway dropped before the ack reached the app.
            h.broker._connected = False
            h.broker.can_connect = False
            return rec.model_copy(
                update={
                    "status": OrderStatus.SUBMITTED,
                    "filled_qty": 0.0,
                    "avg_fill_price": None,
                    "message": "sent; awaiting IBKR acknowledgement",
                }
            )
        return rec

    h.broker.place_order = lost_ack
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(8):
        h.clock.advance(seconds=15)
        with contextlib.suppress(Exception):
            await h.engine.monitor_tick()
    assert sent, "the bot's stop sent its exit"
    assert bot_sells(h, KEY) == [pytest.approx(mine)]
    assert h.broker.pos[KEY][0] == pytest.approx(own)  # only the owner's coins are left
    row = [o for o in db.list_orders(limit=50) if o.broker_order_id == sent[0]]
    assert row and row[0].status == OrderStatus.SUBMITTED and row[0].filled_qty == 0
    # Back Thursday 10:00 New York: IBKR reports only today's executions and no longer
    # knows Wednesday's IOC.
    del h.broker.orders[sent[0]]
    h.broker.place_order = real
    h.broker.can_connect = True
    h.broker._connected = True
    h.clock.t = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
    every_fill = h.broker.fills_since

    async def today_only(since):
        midnight = trading_day_start(trading_day(h.clock()))
        return [f for f in await every_fill(since) if f.ts >= midnight]

    h.broker.fills_since = today_only
    h.engine = Engine(config, h.db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 12, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(mine)], "the owner's coins were sold"
    assert h.broker.pos[KEY][0] == pytest.approx(own)


@pytest.mark.parametrize("o", [0.001, 0.0025, 0.005])
async def test_the_owners_sale_after_an_estimated_watchdog_sale_leaves_the_bots_rest(
    db, config, o: float
) -> None:
    """An earlier-day partial watchdog sale journaled at an estimate, then a sale of the
    owner's own coins: the bot's rest keeps its stop (the estimate used up the budget)."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))  # Wed 21:00 NY
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    owner_buys(h, _BTC, 0.006, 60000.0)  # journaled
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=30)  # app down; the watchdog's IOC fills 60% of the bot's part
    part = round(mine * 0.6, 6)
    h.broker.pos[KEY][0] -= part
    h.broker.cash += part * 58900.0
    h.broker.fills.append(
        Fill(exec_id=f"WD{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=_BTC,
             side=Side.SELL, qty=part, price=58900.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    marker = position_marker(meta)
    note_watchdog_sale(db, KEY, marker, "B1", mine, h.clock())
    note_watchdog_sale(db, KEY, marker, "B1", part, h.clock(), done=True)
    h.engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 60600.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 6, seconds=15)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert any(f.broker_order_id == UNREPORTED_EXIT for f in db.list_fills(since=since))
    assert db.get_position_meta(KEY) is not None
    assert db.list_trades() == []
    owner_sells(h, _BTC, o, 60600.0)  # the owner sells some of his own coins
    await ticks(h, 4, seconds=15)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 8, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(round(mine - part, 8), abs=1e-6)]
    assert h.broker.pos[KEY][0] == pytest.approx(0.006 - o, abs=1e-9)


# ----------------------------------------------------------------------------- review 42
@pytest.mark.parametrize("owner_sale", [0.0, 0.001])
async def test_an_owner_round_trip_after_the_watchdogs_last_sale_is_his(
    db, config, owner_sale: float
) -> None:
    """The watchdog sold half the bot's BTC on Wednesday (IBKR no longer reports it). On
    Thursday the owner buys 0.004 and sells part of it: the watchdog's done IOC could not
    have sold then, so that sale is his and the bot's stop sells all of its rest."""
    h = await _hold_btc(db, config, datetime(2026, 10, 28, 13, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    h.clock.t = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
    sold = round(mine / 2, 8)
    h.broker.pos[KEY][0] -= sold
    h.broker.cash += sold * 58990.0
    h.broker.fills.append(
        Fill(exec_id=f"WD{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=_BTC,
             side=Side.SELL, qty=sold, price=58990.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    note_watchdog_sale(db, KEY, position_marker(meta), "77", sold, h.clock(), done=True)
    h.clock.t = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    owner_buys(h, _BTC, 0.004, 60000.0)
    if owner_sale:
        h.clock.t = datetime(2026, 10, 29, 13, 30, tzinfo=UTC)
        owner_sells(h, _BTC, owner_sale, 60050.0)
    h.engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 59500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 4, seconds=15)
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 8, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(mine - sold)]
    assert h.broker.pos[KEY][0] == pytest.approx(0.004 - owner_sale)


async def _enter_with_refused_stop(h) -> None:
    from trader.broker.base import PROTECTIVE_REJECTED

    real = h.broker.place_order

    async def place(req: OrderRequest) -> OrderRecord:
        rec = await real(req)
        if req.side == Side.BUY and rec.filled_qty > 0:
            h.broker.orders[f"{rec.broker_order_id}-SL"].status = OrderStatus.REJECTED
            rec = rec.model_copy(
                update={"message": f"WARNING: {PROTECTIVE_REJECTED} by IBKR (price limits)"}
            )
        return rec

    h.broker.place_order = place
    await h.engine.run_cycle()
    h.broker.place_order = real


async def test_a_reprotected_stop_never_sits_next_to_the_brackets_target(db, config) -> None:
    """IBKR refused the bracket's stop and kept its target. The stop put back is not in the
    target's OCA group, so the target is cancelled and the engine takes the profit itself:
    a part fill of the target can never leave the stop larger than the bot's units."""
    h = await harness(db, config, exits={"time_stop_minutes": 0})
    await _enter_with_refused_stop(h)
    await ticks(h, 1)
    assert _stops(h) == [24.0]
    assert h.broker.working("STK:F", "target") == []
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 2)
    h.broker.set_price("F", 10.6)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 50
    assert bot_sells(h, "STK:F") == [24]


async def test_a_shadow_halt_flatten_with_a_failed_read_alerts(db, tmp_path) -> None:
    """Shadow mode, the daily-loss halt, and the executions read fails while the journal
    does not explain the position: the flatten reports the failure, never 'nothing'."""
    from tests.test_engine import disarmed_with_bot_position

    h = await disarmed_with_bot_position(db, tmp_path)
    owner_buys(h, F, 150, 10.0)
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    h.broker.set_price("F", 9.83)
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    owner_buys(h, F, 5, 9.83)
    h.broker.fail.add("fills_since")
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert "Flatten FAILED" in h.notes.titles()
    assert bot_sells(h, "STK:F") == []
    assert "STK:F" not in h.engine._closing


@pytest.mark.parametrize("owner", [0, 50])
async def test_a_trip_waiting_for_its_commission_is_journaled_after_a_restart(
    db, config, owner: int
) -> None:
    """The bot's target sells its 24 F in two executions, the second with commission 0, and
    the app restarts inside COMMISSION_GRACE while the owner holds F too."""
    from tests.test_engine import _part
    from trader.models import Mode

    h = await harness(db, config, mode=Mode.PAPER, exits={"time_stop_minutes": 0})
    await enter_f(h)
    await ticks(h, 1)
    if owner:
        owner_buys(h, F, owner, 10.0)
    await ticks(h, 1)
    [target] = [oid for oid, k in h.broker.kind.items() if k == "target"]
    _part(h.broker, target, 10, 10.5)
    h.broker.fee = 0.0
    _part(h.broker, target, 14, 10.5)
    h.broker.fee = 0.35
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert db.list_trades() == []
    h.clock.advance(seconds=30)
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 10, seconds=30)
    assert [t.qty for t in db.list_trades()] == [24]
    assert db.get_position_meta("STK:F") is None


@pytest.mark.parametrize("journaled", [0.0, 0.004])
@pytest.mark.parametrize("prev_qty", [0.02, 0.005])
async def test_the_bots_quick_previous_trip_is_none_of_its_new_entry(
    db: Database, prev_qty: float, journaled: float
) -> None:
    """No other client in BTC. The bot's previous trip bought 50 s and sold 30 s before
    its new entry was sent; the new entry filled while the app was down (or in part
    before). The watchdog's stop and flatten sell all of the new position."""
    from tests import test_watchdog as tw
    from trader.models import Position

    btc = tw.crypto()
    opened = tw.NOW - timedelta(hours=1)
    db.insert_fill(Fill(exec_id="P1", broker_order_id="7", instrument=btc, side=Side.BUY,
                        qty=prev_qty, price=100.0, ts=opened - timedelta(seconds=50)))  # fmt: skip
    db.insert_fill(Fill(exec_id="P2", broker_order_id="8", instrument=btc, side=Side.SELL,
                        qty=prev_qty, price=98.0, ts=opened - timedelta(seconds=30)))  # fmt: skip
    db.set_position_meta(btc.key, stop_loss=95.0, take_profit=110.0, entry_price=100.0,
                         initial_risk=5.0 * 0.01, opened_at=opened)  # fmt: skip
    db.set_kv("position_extra:" + btc.key, '{"unit_risk": 5.0, "qty": 0.01}')
    if journaled:
        at = opened + timedelta(seconds=2)
        db.insert_fill(Fill(exec_id="E1", broker_order_id="12", instrument=btc, side=Side.BUY,
                            qty=journaled, price=100.0, ts=at))  # fmt: skip
    clock = tw.Clock()
    broker = tw.FakeBroker()
    broker.positions_list.append(Position(instrument=btc, qty=0.01, avg_cost=100.0))
    broker.quotes[btc.key] = tw.q(btc, 90.0, 90.0)
    tw.beat(db, clock, age=400)
    watchdog, _ = tw.make(db, broker, clock)
    for _ in range(4):
        await watchdog.tick()
        clock.advance(seconds=10)
        tw.beat(db, clock, age=400)
    assert [o.qty for o in broker.placed] == [pytest.approx(0.01)]


# ----------------------------------------------------------------------------- review 43
async def test_an_undecided_crypto_stop_alerts_and_hands_over_to_the_watchdog(db, config) -> None:
    """The owner bought BTC next to the bot's, and IBKR's executions keep timing out: the
    stop cannot be sized. That is no 'none of it is the bot's': the owner is told once, the
    heartbeat goes stale so the watchdog (which sells only the bot's part) takes over, and
    the bot sells its own once the read recovers."""
    from trader.watchdog import read_heartbeat

    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, 0.004, 60000.0)
    every_fill = h.broker.fills_since
    failing = True

    async def flaky(since):
        if failing:
            raise BrokerError("IBKR executions timed out after 10s")
        return await every_fill(since)

    h.broker.fills_since = flaky
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    before = list(h.notes.titles())
    await ticks(h, 40, seconds=15)
    new = [t for t in h.notes.titles() if t not in before]
    assert bot_sells(h, KEY) == []
    assert new.count("Close BTC FAILED") == 1  # one alert a stretch
    assert h.clock() - read_heartbeat(db) > timedelta(seconds=45)
    failing = False
    await ticks(h, 3, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(mine)]
    assert h.broker.pos[KEY][0] == pytest.approx(0.004)


async def test_an_undecided_stock_flatten_alerts(db, config) -> None:
    """Flatten before the close with the executions read failing and the journal not
    explaining the position: nothing is sold, and the owner is told."""
    h = await _f_with_owner(db, config)
    owner_buys(h, F, 5, 10.0)

    async def failing(since):
        raise BrokerError("IBKR executions timed out after 10s")

    h.broker.fills_since = failing
    before = list(h.notes.titles())
    h.clock.t = datetime(2026, 10, 21, 19, 50, tzinfo=UTC)
    await ticks(h, 40, seconds=15)
    new = [t for t in h.notes.titles() if t not in before]
    assert bot_sells(h, "STK:F") == []
    assert "Close F FAILED" in new


@pytest.mark.parametrize("stale", [0.0, 0.003, 0.5])
async def test_a_watchdog_flat_sale_estimate_is_the_bots_lot_only(db, config, stale: float) -> None:
    """The owner's BTC bought on the 20th and sold on a day IBKR no longer reports stays
    open in the journal. The watchdog sold the bot's later position flat on Wednesday: the
    estimate the app journals on Thursday is the bot's lot, never his stale one too."""
    from trader.watchdog import WATCHDOG_CLOSED_PREFIX

    db.set_kv("broker_account", "FAKE")
    db.set_kv("journal_account_since", datetime(2026, 10, 1, tzinfo=UTC).isoformat())
    if stale:
        db.insert_fill(
            Fill(exec_id="OWN1", broker_order_id="perm:900", instrument=_BTC, side=Side.BUY,
                 qty=stale, price=50000.0, commission=1.0,
                 ts=datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
        )  # fmt: skip
    h = await _hold_btc(db, config, datetime(2026, 10, 28, 22, 0, tzinfo=UTC))
    marker = position_marker(db.get_position_meta(KEY))
    h.clock.advance(seconds=600)
    qty = h.broker.pos.pop(KEY)[0]
    h.broker.cash += qty * 59000.0
    note_watchdog_sale(db, KEY, marker, "WD1", qty, h.clock(), done=True)
    record = {"instrument": KEY, "qty": qty, "filled": qty, "status": "Filled", "marker": marker}
    db.audit("watchdog_close", record, actor="watchdog")
    db.set_kv(WATCHDOG_CLOSED_PREFIX + KEY, marker)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 70, seconds=15)
    assert db.get_position_meta(KEY) is None
    [trade] = db.list_trades()
    assert trade.qty == pytest.approx(qty)


_ENTRY_TRACE = "ai-trader-0123456789abcdef"


def _entry_outage(db: Database, broker: FakeBroker, *, journaled: float, held: float,
                  working_filled: float | None,
                  proposed_before: timedelta = timedelta(0)) -> None:  # fmt: skip
    """The bot's BTC entry of 0.01 (its journal row as the engine writes it), `journaled`
    of it in the journal; the owner's 0.5 journaled; `held` at the broker now, the bid
    below the bot's stop. `working_filled`: the entry still works there with that fill.
    `proposed_before`: an entry that waited for approval (its row is as old as the
    proposal, its tag the journal id's, its send time in kv)."""
    from tests.test_watchdog import crypto, q
    from trader.models import Position

    btc = crypto()
    opened = NOW - timedelta(hours=1)
    db.set_position_meta(btc.key, stop_loss=95.0, take_profit=110.0, entry_price=100.0,
                         initial_risk=5.0 * BOT, opened_at=opened)  # fmt: skip
    db.set_kv("position_extra:" + btc.key, json.dumps({"unit_risk": 5.0, "qty": BOT}))
    approved = proposed_before > timedelta(0)
    req = OrderRequest(instrument=btc, side=Side.BUY, qty=BOT, order_type=OrderType.LIMIT,
                       limit_price=100.0, stop_loss=95.0, take_profit=110.0,
                       trace_id=None if approved else _ENTRY_TRACE)  # fmt: skip
    status = OrderStatus.PARTIALLY_FILLED if journaled else OrderStatus.SUBMITTED
    oid = db.insert_order(OrderRecord(request=req, status=status, broker_order_id="12",
                                      filled_qty=journaled,
                                      created_at=opened - proposed_before))  # fmt: skip
    if approved:
        db.set_kv(f"order_sent:{oid}", opened.isoformat())
        req = req.model_copy(update={"trace_id": f"ai-trader-{oid}"})  # as IBKR reads it back
    if journaled:
        at = opened + timedelta(seconds=5)
        db.insert_fill(Fill(exec_id="E1", broker_order_id="12", instrument=btc, side=Side.BUY,
                            qty=journaled, price=100.0, ts=at))  # fmt: skip
    db.insert_fill(Fill(exec_id="X1", broker_order_id="perm:5", instrument=btc, side=Side.BUY,
                        qty=0.5, price=99.0, ts=opened + timedelta(minutes=2)))  # fmt: skip
    broker.positions_list.append(Position(instrument=btc, qty=round(held, 8), avg_cost=99.0))
    if working_filled is not None:
        broker.orders.append(
            OrderRecord(
                request=req,
                status=OrderStatus.PARTIALLY_FILLED if working_filled else OrderStatus.SUBMITTED,
                broker_order_id="perm:777",
                filled_qty=working_filled,
            )
        )
    broker.quotes[btc.key] = q(btc, 90.0, 90.0)


async def _watchdog_outage(db: Database, broker: FakeBroker) -> tuple[list[float], list[str]]:
    clock = Clock()
    beat(db, clock, age=60)
    watchdog, notes = make(db, broker, clock)
    for _ in range(3):
        await watchdog.tick()
        for rec in [o for o in broker.orders if o.request.side == Side.SELL]:
            rec.status, rec.filled_qty = OrderStatus.FILLED, rec.request.qty
            pos = broker.positions_list[0]
            broker.positions_list[0] = pos.model_copy(update={"qty": pos.qty - rec.request.qty})
            broker.orders.remove(rec)
        clock.advance(seconds=10)
        beat(db, clock, age=60)
    return [round(o.qty, 8) for o in broker.placed], notes.sent


@pytest.mark.parametrize("journaled", [0.0, 0.004])
async def test_the_watchdog_sells_what_the_broker_says_the_entry_filled(
    db: Database, journaled: float
) -> None:
    """The bot's entry still works at IBKR and filled 0.007 by now, more than the journal
    shows; the owner sold 0.2 of his journaled 0.5 meanwhile. The stop sells the bot's
    0.007, and none of the owner's."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=journaled, held=0.007 + 0.3, working_filled=0.007)
    placed, sent = await _watchdog_outage(db, broker)
    assert placed == [0.007]
    assert "Crypto watchdog: the bot's part unknown" not in sent


async def test_a_resting_entry_unfilled_is_none_of_the_bots_coins(db: Database) -> None:
    """The entry still rests with only its journaled 0.004 filled; the owner sold 0.194 of
    his: selling more than 0.004 would sell his coins."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=0.004, held=BOT + 0.3, working_filled=0.004)
    placed, sent = await _watchdog_outage(db, broker)
    assert placed == [0.004]
    assert "Crypto watchdog: the bot's part unknown" not in sent


@pytest.mark.parametrize("journaled", [0.0, 0.004])
async def test_an_entry_gone_in_the_outage_with_an_owner_sale_alerts(
    db: Database, journaled: float
) -> None:
    """The entry the app last saw working is gone at IBKR (filled, or lapsed), and less is
    held than the bot's and the owner's journaled coins: whose coins are left is unknown.
    The watchdog sells only what is surely the bot's, and the owner is told (urgent)."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=journaled, held=BOT + 0.3, working_filled=None)
    placed, sent = await _watchdog_outage(db, broker)
    assert placed == ([journaled] if journaled else [])
    assert "Crypto watchdog: the bot's part unknown" in sent


# ----------------------------------------------------------------------------- review 44
async def test_the_engine_waits_while_the_watchdogs_sale_is_open(db, config) -> None:
    """The owner bought less BTC than the bot's entry and the executions read keeps
    failing: the engine's stop is undecided and the watchdog takes over, selling the bot's
    part. Until its next poll marks that IOC done, the engine must not take what is left
    (the owner's coins) for the bot's."""
    from trader.watchdog import CryptoWatchdog, WatchdogSettings

    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, 0.004, 60000.0)

    async def failing(since):
        raise BrokerError("IBKR executions timed out after 10s")

    h.broker.fills_since = failing
    _, notes = make(db, h.broker, h.clock)
    wd = CryptoWatchdog(db, h.broker, WatchdogSettings(), notes, h.clock,
                        started_at=h.clock() - timedelta(hours=1))  # fmt: skip
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 5, seconds=15)
    assert bot_sells(h, KEY) == []
    await wd.tick()
    h.clock.advance(seconds=5)
    await wd.tick()
    assert [r.qty for r in h.broker.placed if r.side == Side.SELL] == [pytest.approx(mine)]
    h.clock.advance(seconds=5)
    await h.engine.monitor_tick()  # before the watchdog's next poll
    assert h.broker.pos[KEY][0] == pytest.approx(0.004)
    h.clock.advance(seconds=5)
    await wd.tick()  # marks its IOC done
    await ticks(h, 2, seconds=15)
    assert h.broker.pos[KEY][0] == pytest.approx(0.004)


async def test_an_undecided_ai_close_leaves_the_crypto_stop_watched(db, config) -> None:
    """The AI keeps asking to close the owner-shared BTC (autopilot) while the executions
    read fails and the bid is under the bot's stop: each CLOSE is undecided, so it must
    not mark the position closing (no stop check, a fresh heartbeat): the watchdog still
    takes over and sells the bot's part."""
    from trader.models import AIDecision
    from trader.watchdog import CryptoWatchdog, WatchdogSettings

    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, 0.004, 60000.0)

    async def failing(since):
        raise BrokerError("IBKR executions timed out after 10s")

    h.broker.fills_since = failing
    _, notes = make(db, h.broker, h.clock)
    wd = CryptoWatchdog(db, h.broker, WatchdogSettings(), notes, h.clock,
                        started_at=h.clock() - timedelta(hours=1))  # fmt: skip
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for i in range(12):
        h.clock.advance(seconds=15)
        if i % 4 == 0:
            h.decider.decisions = [
                AIDecision(action=TradeAction.CLOSE, symbol="BTC", asset_class=AssetClass.CRYPTO,
                           conviction=0.8, rationale="breaking down")
            ]  # fmt: skip
            await h.engine.run_cycle()
            h.decider.decisions = []
        else:
            await h.engine.monitor_tick()
        await wd.tick()
    assert [r.qty for r in h.broker.placed if r.side == Side.SELL] == [pytest.approx(mine)]
    assert h.broker.pos[KEY][0] == pytest.approx(0.004)


@pytest.mark.parametrize("journaled", [0.0, 0.004])
async def test_the_watchdog_reads_an_approved_entry_too(db: Database, journaled: float) -> None:
    """The entry waited 3 minutes for the owner's approval (its journal row is as old as
    the proposal): the watchdog still sells what IBKR says it filled."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=journaled, held=0.007 + 0.3, working_filled=0.007,
                  proposed_before=timedelta(minutes=3))  # fmt: skip
    placed, _ = await _watchdog_outage(db, broker)
    assert placed == [0.007]


async def test_an_approved_entry_gone_in_the_outage_alerts(db: Database) -> None:
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=0.004, held=BOT + 0.3, working_filled=None,
                  proposed_before=timedelta(minutes=3))  # fmt: skip
    placed, sent = await _watchdog_outage(db, broker)
    assert placed == [0.004]
    assert "Crypto watchdog: the bot's part unknown" in sent


@pytest.mark.parametrize("before", [timedelta(minutes=4), timedelta(minutes=9)])
def test_a_previous_trips_entry_is_none_of_this_one(db: Database, before: timedelta) -> None:
    """An earlier trip's entry, sent minutes before this position opened, is no part of
    it: neither sure nor unknown."""
    from tests.test_watchdog import crypto
    from trader.ownership import app_entry_unjournaled

    btc = crypto()
    opened = NOW - timedelta(hours=1)
    req = OrderRequest(instrument=btc, side=Side.BUY, qty=BOT, order_type=OrderType.LIMIT,
                       limit_price=100.0, stop_loss=95.0, take_profit=110.0,
                       trace_id="ai-trader-old")  # fmt: skip
    db.insert_order(OrderRecord(request=req, status=OrderStatus.SUBMITTED, broker_order_id="9",
                                created_at=opened - before))  # fmt: skip
    assert app_entry_unjournaled(db, btc.key, opened, [], []) == (0.0, 0.0)


@pytest.mark.parametrize("owner_sold", [0.003, 0.006, 0.2])
async def test_an_owner_sale_smaller_than_the_unseen_fill_alerts(
    db: Database, owner_sold: float
) -> None:
    """The entry (0.004 of 0.01 journaled) filled in full in the outage and is gone at
    IBKR; the owner sold some of his. Whatever he sold, whose coins are left is unknown:
    the owner is told (urgent) unless the bot's whole entry is sold."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=0.004, held=BOT + 0.5 - owner_sold, working_filled=None)
    placed, sent = await _watchdog_outage(db, broker)
    assert sum(placed) <= BOT + 1e-9
    assert "Crypto watchdog: the bot's part unknown" in sent


async def test_an_entry_gone_after_filling_in_full_needs_no_alert(db: Database) -> None:
    """Held is all of the entry and the owner's coins: the watchdog sells the bot's 0.01."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=0.004, held=BOT + 0.5, working_filled=None)
    placed, sent = await _watchdog_outage(db, broker)
    assert placed == [BOT]
    assert "Crypto watchdog: the bot's part unknown" not in sent


# ----------------------------------------------------------------------------- review 45
async def _bot_sold_part(db, config, now: datetime | None = None):
    """The bot's 24 F, 14 of them sold by its bracket target (journaled): 10 left."""
    from tests.test_engine import _part

    kwargs = {"now": now} if now is not None else {}
    h = await harness(db, config, exits={"time_stop_minutes": 0}, **kwargs)
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 1)
    _part(h.broker, f"{entry}-TP", 14, 10.5)
    await ticks(h, 2)
    assert h.broker.pos["STK:F"][0] == 10
    return h, entry


async def _executions_fail(since):
    raise BrokerError("IBKR executions timed out after 10s")


async def test_after_a_partial_exit_an_unseen_owner_buy_waits_for_the_read(db, config) -> None:
    """The bot sold 14 of its 24 F; the owner buys 5 and the executions read fails: 15
    held, under the entry's 24, but more than the bot's 10 left. Close sells nothing until
    the read works, then only the bot's 10."""
    h, _ = await _bot_sold_part(db, config)
    owner_buys(h, F, 5, 10.4)
    real = h.broker.fills_since
    h.broker.fills_since = _executions_fail
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 15
    h.broker.fills_since = real
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 10


async def test_after_a_partial_exit_the_flatten_waits_for_the_read(db, config) -> None:
    h, _ = await _bot_sold_part(db, config)
    owner_buys(h, F, 5, 10.4)
    real = h.broker.fills_since
    h.broker.fills_since = _executions_fail
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)
    await h.engine.monitor_tick()
    assert bot_sells(h, "STK:F") == []
    assert h.broker.working("STK:F", "stop")
    h.broker.fills_since = real
    await ticks(h, 3, seconds=30)
    assert bot_sells(h, "STK:F") == [10]


async def test_after_a_partial_exit_an_owner_buy_on_an_unreported_day_is_his(db, config) -> None:
    """Wednesday: the bot's target sold 14 of its 24 F; the owner buys 5 while the app is
    down. Thursday IBKR reports only Thursday's executions: Close sells the bot's 10."""
    h, _ = await _bot_sold_part(db, config, now=datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 5, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 10


async def test_a_bot_sale_the_position_read_does_not_show_yet_is_no_owner_buy(db, config) -> None:
    """Pure bot: the target's 14 are journaled seconds ago, but the position read still
    shows 24 (it lags). That is no evidence the owner bought: Close sells the bot's 10."""
    from tests.test_engine import _part

    h = await harness(db, config, exits={"time_stop_minutes": 0})
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 2)
    _part(h.broker, f"{entry}-TP", 14, 10.5)
    await ticks(h, 1, seconds=15)  # journaled
    real = h.broker.positions
    calls = {"n": 0}

    async def lagging():
        calls["n"] += 1
        out = await real()
        if calls["n"] <= 2:
            return [p.model_copy(update={"qty": 24.0}) for p in out]
        return out

    h.broker.positions = lagging
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 10
    assert "STK:F" not in h.broker.pos


async def test_a_target_fill_during_the_sizing_reads_is_no_owner_buy(db, config) -> None:
    """Pure bot: the target fills 14 between the cap's position read and its executions
    read. Close still sells the bot's 10."""
    from tests.test_engine import _part

    h = await harness(db, config, exits={"time_stop_minutes": 0})
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 2)
    real = h.broker.fills_since
    raced = []

    async def racing(since):
        if not raced:
            raced.append(True)
            _part(h.broker, f"{entry}-TP", 14, 10.5)
        return await real(since)

    h.broker.fills_since = racing
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 10


@pytest.mark.parametrize("journaled", [0.0, 0.004])
@pytest.mark.parametrize("bought", [0.001, 0.1])
async def test_an_entry_gone_in_the_outage_with_an_owner_buy_alerts(
    db: Database, journaled: float, bought: float
) -> None:
    """The entry (journaled part `journaled` of 0.01) is gone at IBKR, and the owner (0.5
    journaled) bought more in the outage: more is held than if it all filled. Whose coins
    are left is unknown: only the sure part is sold, and the owner is told."""
    broker = FakeBroker()
    _entry_outage(db, broker, journaled=journaled, held=BOT + 0.5 + bought, working_filled=None)
    placed, sent = await _watchdog_outage(db, broker)
    assert placed == ([journaled] if journaled else [])
    assert "Crypto watchdog: the bot's part unknown" in sent


# ----------------------------------------------------------------------------- review 46
async def _paper(db, config, now: datetime):
    from trader.models import Mode

    return await harness(db, config, mode=Mode.PAPER, exits={"time_stop_minutes": 0}, now=now)


async def test_a_target_part_fill_never_hides_an_unreported_owner_buy(db, config) -> None:
    """Wednesday: the bot's 24 F; the owner buys 3 while the app is down. Thursday the
    target fills 4 of the bot's: a Close a minute later sells the bot's 20, not his 3."""
    from tests.test_engine import _part

    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 3, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    _part(h.broker, f"{entry}-TP", 4, 10.5)
    await ticks(h, 1)
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 20
    assert h.broker.pos["STK:F"][0] == 3


async def test_the_flatten_right_after_a_part_fill_keeps_the_owners_shares(db, config) -> None:
    from tests.test_engine import _part

    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 1)
    _part(h.broker, f"{entry}-TP", 14, 10.5)
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 5, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 29, 19, 49, tzinfo=UTC)
    await h.engine.monitor_tick()
    _part(h.broker, f"{entry}-TP", 6, 10.5)
    h.clock.t = datetime(2026, 10, 29, 19, 50, 30, tzinfo=UTC)
    await h.engine.monitor_tick()
    assert h.broker.pos["STK:F"][0] == 5
    assert bot_sells(h, "STK:F") == [4]  # the flatten: the bot's rest only


async def test_an_unseen_owner_buy_a_minute_after_a_partial_exit_waits(db, config) -> None:
    """Same day: the target sold 14 of 24, the owner buys 5 a minute later, and the
    executions read fails: Close sells nothing until it can tell."""
    from tests.test_engine import _part

    h = await _paper(db, config, datetime(2026, 10, 21, 15, 0, tzinfo=UTC))
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 1)
    _part(h.broker, f"{entry}-TP", 14, 10.5)
    await ticks(h, 1)
    owner_buys(h, F, 5, 10.4)
    h.broker.fills_since = _executions_fail
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 15


async def test_a_crypto_close_retried_after_a_part_fill_keeps_the_owners_coins(db, config) -> None:
    """The owner bought BTC on Wednesday evening, app down. Thursday the bot's stop sells
    with an IOC that fills 40%; its retry a minute later sells the bot's rest, not his."""
    h = await _hold_btc(db, config, datetime(2026, 10, 28, 19, 0, tzinfo=UTC))
    bot = h.broker.pos[KEY][0]
    h.clock.t = datetime(2026, 10, 28, 23, 0, tzinfo=UTC)
    his = round(bot * 0.3, 8)
    owner_buys(h, _BTC, his, 60000.0)
    real = h.broker.place_order
    first: list[float] = []

    async def place(req: OrderRequest) -> OrderRecord:
        if not first and req.side == Side.SELL and req.stop_loss is None:
            first.append(req.qty)
            rec = await real(req.model_copy(update={"qty": round(req.qty * 0.4, 8)}))
            order = h.broker.orders[rec.broker_order_id]
            order.request, order.status = req, OrderStatus.CANCELLED  # IOC: part, rest gone
            return order.model_copy()
        return await real(req)

    h.broker.place_order = place
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 16, seconds=15)
    assert first
    assert his - 1e-9 <= h.broker.pos[KEY][0] <= his + 1e-6  # his, and rounding dust


async def _partly_filled_entry_then_outage(db, config, *, owner: float = 0.0,
                                           rest: bool = True, forgotten: bool = False,
                                           time_stop: bool = False):  # fmt: skip
    """Wednesday: the bot's entry of 24 F fills 7, its target sells those 7 (journaled)
    and the entry keeps working. The app goes down; the rest (17) fills that afternoon
    (or lapses). Thursday IBKR reports only Thursday's executions (and with `forgotten`,
    no longer knows the completed entry)."""
    from tests.test_engine import _part, _resting_entry
    from trader.models import Mode

    exits = {} if time_stop else {"time_stop_minutes": 0}
    h = await harness(db, config, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC), mode=Mode.PAPER,
                      exits=exits)  # fmt: skip
    entry, total = await _resting_entry(h)
    _part(h.broker, entry, 7, 10.01)
    await ticks(h, 2, seconds=15)
    if owner:
        owner_buys(h, F, owner, 10.2)
        await ticks(h, 2, seconds=15)
    _part(h.broker, f"{entry}-TP", 7, 10.5)
    stop = h.broker.orders[f"{entry}-SL"]  # the OCA group shrinks the sibling stop
    stop.request = stop.request.model_copy(update={"qty": stop.request.qty - 7})
    await ticks(h, 2, seconds=15)
    assert [t.qty for t in db.list_trades()] == [7]
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
    if rest:
        _part(h.broker, entry, total - 7, 10.01)
    else:
        for oid in (entry, f"{entry}-SL", f"{entry}-TP"):
            h.broker.orders[oid].status = OrderStatus.CANCELLED
    if forgotten:
        known = h.broker.order_status

        async def order_status(oid):
            return None if oid == entry else await known(oid)

        h.broker.order_status = order_status
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3, seconds=15)
    return h


async def test_the_unreported_rest_of_a_partly_filled_entry_keeps_its_stop(db, config) -> None:
    h = await _partly_filled_entry_then_outage(db, config)
    assert h.broker.pos["STK:F"][0] == 17
    assert db.get_position_meta("STK:F") is not None
    assert h.broker.working("STK:F", "stop")
    h.broker.set_price("F", 9.0)
    await ticks(h, 20, seconds=15)
    assert "STK:F" not in h.broker.pos
    assert sorted(t.qty for t in db.list_trades()) == [7, 17]
    await ticks(h, 80, seconds=15)
    assert db.get_position_meta("STK:F") is None


async def test_the_unreported_rest_next_to_the_owners_shares_is_still_the_bots(db, config) -> None:
    h = await _partly_filled_entry_then_outage(db, config, owner=10, time_stop=True)
    await ticks(h, 80, seconds=15)
    assert h.broker.pos["STK:F"][0] == 10
    assert sorted(t.qty for t in db.list_trades()) == [7, 17]
    assert db.get_position_meta("STK:F") is None


async def test_an_entry_the_broker_forgot_keeps_its_unreported_rest(db, config) -> None:
    h = await _partly_filled_entry_then_outage(db, config, forgotten=True)
    await ticks(h, 80, seconds=15)  # past a day since the entry's journal row
    assert db.get_position_meta("STK:F") is not None
    assert h.broker.working("STK:F", "stop")


async def test_a_lapsed_rest_leaves_the_owners_shares_alone(db, config) -> None:
    h = await _partly_filled_entry_then_outage(db, config, owner=50, rest=False, forgotten=True)
    assert h.broker.pos["STK:F"][0] == 50
    await ticks(h, 80, seconds=15)
    assert h.broker.pos["STK:F"][0] == 50


# ----------------------------------------------------------------------------- review 47
@pytest.mark.parametrize("wait_ticks", [1, 12])
async def test_a_target_fill_during_the_close_cancels_keeps_the_owners_unseen_shares(
    db, config, wait_ticks: int
) -> None:
    """Thursday: the bot's target fills 4 of its 24, the owner's 3 bought on Wednesday
    evening are in no fill. A Close (1 or 12 minutes later) cancels the bracket, and the
    target fills 2 more before its cancel lands: those come off the bot's part."""
    from tests.test_engine import _part

    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await enter_f(h)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 3, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    _part(h.broker, f"{entry}-TP", 4, 10.5)
    await ticks(h, wait_ticks)
    real = h.broker.cancel_order
    raced: list[str] = []

    async def cancel(oid: str) -> None:
        if not raced and oid == f"{entry}-TP":
            raced.append(oid)
            _part(h.broker, oid, 2, 10.5)  # fills just before the cancel lands
        await real(oid)

    h.broker.cancel_order = cancel
    await h.engine.close_position("STK:F")
    assert raced
    assert h.broker.pos["STK:F"][0] == 3
    await ticks(h, 20)
    assert h.broker.pos["STK:F"][0] == 3
    assert sum(t.qty for t in db.list_trades()) == 24


async def _lapsed_rest_with_owner(db, config):
    """Wednesday: the bot's entry of 24 F fills 20 and its rest lapses; the owner's 10
    are journaled. Returns the harness and the entry's order id."""
    from tests.test_engine import _part, _resting_entry

    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    _part(h.broker, entry, 20, 10.01)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 10, 10.2)
    await ticks(h, 2, seconds=15)
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # the rest lapses
    await ticks(h, 2, seconds=15)
    return h, entry


async def test_a_lagging_read_never_credits_a_lapsed_rest_over_the_owners_shares(
    db, config
) -> None:
    """The target sells 6 of the bot's 20 and the position read still shows them: the
    entry's lapsed rest (4) is not the bot's; the owner's journaled 10 stay."""
    from tests.test_engine import _part

    h, entry = await _lapsed_rest_with_owner(db, config)
    _part(h.broker, f"{entry}-TP", 6, 10.5)
    await ticks(h, 1, seconds=15)
    real = h.broker.positions

    async def lagging():
        return [p.model_copy(update={"qty": p.qty + 6}) for p in await real()]

    h.broker.positions = lagging
    await h.engine.close_position("STK:F")
    h.broker.positions = real
    assert h.broker.pos["STK:F"][0] == 10


@pytest.mark.parametrize("wait", [1, 3])
async def test_a_close_after_a_part_fill_keeps_an_unreported_owner_buy(
    db, config, wait: int
) -> None:
    """With the lapsed rest above, the owner also bought 6 on Wednesday evening (no fill
    shows them). Thursday the target sells 4 of the bot's 20: a Close 1 or 3 minutes later
    sells the bot's 16 and keeps his 16."""
    from tests.test_engine import _part

    h, entry = await _lapsed_rest_with_owner(db, config)
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 6, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    _part(h.broker, f"{entry}-TP", 4, 10.5)
    await ticks(h, wait)
    await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 16


async def test_a_lagging_close_of_an_unreported_rest_sells_all_of_the_bots(db, config) -> None:
    """Pure bot: the rest (17) filled while the app was down; Thursday the target sells 5
    and a Close follows while the position read still shows them: it sells the bot's 12."""
    from tests.test_engine import _part

    h = await _partly_filled_entry_then_outage(db, config)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    _part(h.broker, f"{entry}-TP", 5, 10.5)
    await ticks(h, 1, seconds=15)
    real = h.broker.positions

    async def lagging():
        return [p.model_copy(update={"qty": p.qty + 5}) for p in await real()]

    h.broker.positions = lagging
    await h.engine.close_position("STK:F")
    h.broker.positions = real
    assert "STK:F" not in h.broker.pos


@pytest.mark.parametrize("forgotten", [True, False])
async def test_a_settled_rest_never_takes_a_later_owner_buy(db, config, forgotten: bool) -> None:
    """Wednesday: the bot's entry of 24 F fills 7, the owner buys 50 (journaled), the
    target sells the bot's 7 and the rest lapses while the app is down. Thursday the read
    is exactly the owner's 50: the rest is settled. Friday, after the owner bought 10 more
    while the app was down, nothing of his is sold and no bot trade is journaled."""
    from tests.test_engine import _part, _resting_entry
    from trader.models import Mode

    h = await harness(db, config, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC), mode=Mode.PAPER)
    entry, _ = await _resting_entry(h)
    _part(h.broker, entry, 7, 10.01)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 2, seconds=15)
    _part(h.broker, f"{entry}-TP", 7, 10.5)
    stop = h.broker.orders[f"{entry}-SL"]
    stop.request = stop.request.model_copy(update={"qty": stop.request.qty - 7})
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
    for oid in (entry, f"{entry}-SL", f"{entry}-TP"):
        h.broker.orders[oid].status = OrderStatus.CANCELLED
    if forgotten:
        known = h.broker.order_status

        async def order_status(oid):
            return None if oid == entry else await known(oid)

        h.broker.order_status = order_status
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 80, seconds=15)
    assert h.broker.pos["STK:F"][0] == 50
    h.clock.t = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
    owner_buys(h, F, 10, 10.0)
    h.clock.t = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)
    every_fill = h.broker.fills_since

    async def today_only(since):
        midnight = trading_day_start(trading_day(h.clock()))
        return [f for f in await every_fill(since) if f.ts >= midnight]

    h.broker.fills_since = today_only
    h.engine = Engine(config, h.db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 16, seconds=15)
    assert h.broker.pos["STK:F"][0] == 60
    assert [t.qty for t in db.list_trades()] == [7]


@pytest.mark.parametrize("first", [0.001, 0.0])
async def test_a_watchdog_flat_sale_of_a_late_filled_entry_is_journaled_in_full(
    db, config, first: float
) -> None:
    """The bot's BTC entry fills `first` (journaled), the app goes down, the watchdog
    flattens what is held; the entry's rest fills while the app is down and the watchdog
    sells that too (sold flat). Thursday the app journals the bot's whole entry."""
    from tests.test_engine import _part, cand
    from tests.test_watchdog import Notes, settings
    from trader.models import AIDecision, Mode
    from trader.watchdog import CryptoWatchdog, sold_by_watchdog

    perm = itertools.count(900)

    class AsOtherClient:
        """The engine's FakeBroker as the watchdog's connection: its executions are
        another client's (perm:) to the app."""

        def __init__(self, broker) -> None:
            self.b = broker

        def __getattr__(self, name):
            return getattr(self.b, name)

        async def place_order(self, req):
            n = len(self.b.fills)
            rec = await self.b.place_order(req)
            for i in range(n, len(self.b.fills)):
                self.b.fills[i] = self.b.fills[i].model_copy(
                    update={"broker_order_id": f"perm:{next(perm)}"}
                )
            return rec

    h = await harness(db, config, mode=Mode.PAPER, now=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
                      trade_crypto=True, trade_stocks=False,
                      exits={"time_stop_minutes": 0})  # fmt: skip
    h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    h.scanner.candidates = {
        KEY: [cand("BTC", price=60000.0, stop=59000.0, target=62000.0)
              .model_copy(update={"instrument": _BTC})]
    }  # fmt: skip
    h.decider.decisions = [
        AIDecision(action=TradeAction.BUY, symbol="BTC", asset_class=AssetClass.CRYPTO,
                   conviction=0.8, setup="vwap_reclaim", rationale="reclaim")
    ]  # fmt: skip
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    if first:
        _part(h.broker, entry, first, 60005.0)
    await ticks(h, 3, seconds=15)
    wd = CryptoWatchdog(db, AsOtherClient(h.broker), settings(), Notes(), h.clock,
                        started_at=h.clock() - timedelta(hours=1))  # fmt: skip
    h.broker.set_price("BTC", 59990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    h.clock.advance(minutes=30)
    _part(h.broker, entry, round(total - first, 8), 60005.0)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(total)
    assert db.get_position_meta(KEY) is None


def _watchdog_after_owner_sale(db: Database, broker: FakeBroker, *, owner_sale: float,
                               app_row: OrderStatus, app_filled: float,
                               bid: float) -> None:  # fmt: skip
    """The bot holds 0.01 BTC, the owner's 0.005 journaled. An outage: the watchdog's IOC
    for 0.01 filled 0.006 (done, journaled). The owner later sells `owner_sale` of his
    (journaled). The app's own exit of the bot's 0.004 is in its journal as `app_row`,
    `app_filled` of it filled with no fill journaled; the app is down again."""
    from tests.test_watchdog import crypto, q
    from trader.models import Position

    btc = crypto()
    opened = NOW - timedelta(hours=3)
    db.set_position_meta(btc.key, stop_loss=95.0, take_profit=110.0, entry_price=100.0,
                         initial_risk=5.0 * BOT, opened_at=opened)  # fmt: skip
    db.set_kv("position_extra:" + btc.key, json.dumps({"unit_risk": 5.0, "qty": BOT}))
    marker = position_marker(db.get_position_meta(btc.key))
    db.insert_fill(Fill(exec_id="E1", broker_order_id="12", instrument=btc, side=Side.BUY,
                        qty=BOT, price=100.0, ts=opened + timedelta(seconds=2)))  # fmt: skip
    db.insert_fill(Fill(exec_id="X1", broker_order_id="perm:5", instrument=btc, side=Side.BUY,
                        qty=0.005, price=99.0, ts=opened + timedelta(minutes=10)))  # fmt: skip
    t1 = opened + timedelta(minutes=30)
    note_watchdog_sale(db, btc.key, marker, "B1", BOT, t1)
    note_watchdog_sale(db, btc.key, marker, "B1", 0.006, t1, done=True)
    db.insert_fill(Fill(exec_id="W1", broker_order_id="perm:900", instrument=btc,
                        side=Side.SELL, qty=0.006, price=95.0,
                        ts=t1 + timedelta(seconds=1)))  # fmt: skip
    held = BOT - 0.006 + 0.005
    if owner_sale:
        db.insert_fill(Fill(exec_id="X2", broker_order_id="perm:6", instrument=btc,
                            side=Side.SELL, qty=owner_sale, price=101.0,
                            ts=t1 + timedelta(minutes=30)))  # fmt: skip
        held -= owner_sale
    at = t1 + timedelta(hours=1)
    req = OrderRequest(instrument=btc, side=Side.SELL, qty=0.004, order_type=OrderType.MARKET,
                       is_exit=True, reason="stop-loss", trace_id="ai-trader-77")  # fmt: skip
    db.insert_order(OrderRecord(request=req, status=app_row, broker_order_id="77",
                                filled_qty=app_filled, created_at=at, updated_at=at))  # fmt: skip
    held -= app_filled
    broker.positions_list.append(Position(instrument=btc, qty=round(held, 8), avg_cost=99.0))
    broker.quotes[btc.key] = q(btc, bid, bid)


@pytest.mark.parametrize("owner_sale", [0.0, 0.003])
@pytest.mark.parametrize(
    ("app_row", "app_filled", "bid", "age", "sells"),
    [
        (OrderStatus.CANCELLED, 0.001, 100.0, 400, [0.003]),  # flatten
        (OrderStatus.CANCELLED, 0.001, 90.0, 60, [0.003]),  # stop
        (OrderStatus.SUBMITTED, 0.0, 90.0, 60, [0.004]),  # the app's exit still working
    ],
)
async def test_the_owners_later_sale_is_his_once_the_watchdogs_sales_are_done(
    db: Database,
    owner_sale: float,
    app_row: OrderStatus,
    app_filled: float,
    bid: float,
    age: int,
    sells: list[float],
) -> None:
    """The owner's sale long after the watchdog's last (done) sale cannot be the
    watchdog's: it comes out of his coins, and the watchdog sells the bot's rest."""
    broker = FakeBroker()
    _watchdog_after_owner_sale(db, broker, owner_sale=owner_sale, app_row=app_row,
                               app_filled=app_filled, bid=bid)  # fmt: skip
    clock = Clock()
    beat(db, clock, age=age)
    watchdog, _ = make(db, broker, clock)
    for _ in range(4):
        await watchdog.tick()
        clock.advance(seconds=10)
        beat(db, clock, age=age)
    assert [round(o.qty, 8) for o in broker.placed] == sells
