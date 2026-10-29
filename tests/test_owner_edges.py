"""The owner's shares next to the bot's: edge cases from review round 40 (failed
executions reads, sales on days IBKR no longer reports, the watchdog's bot-first window,
older position rows)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_engine import _BTC, _hold_btc, _restart_next_morning, hold
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
from trader.ownership import note_watchdog_sale
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
