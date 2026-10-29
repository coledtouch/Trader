"""Crypto watchdog with the owner's coins next to the bot's: it sells only the bot's."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.test_watchdog import NOW, Clock, FakeBroker, beat, crypto, make, q
from trader.db import Database
from trader.models import Fill, OrderStatus, Position, Side
from trader.watchdog import WATCHDOG_CLOSED_PREFIX, sold_by_watchdog

BOT = 0.01
OPENED = NOW - timedelta(hours=1)


def setup(db: Database, broker: FakeBroker, *, owner: float, journal_owner: bool,
          bid: float = 90.0) -> None:  # fmt: skip
    btc = crypto()
    db.set_position_meta(btc.key, stop_loss=95.0, take_profit=110.0, entry_price=100.0,
                         initial_risk=5.0 * BOT, opened_at=OPENED)  # fmt: skip
    db.set_kv("position_extra:" + btc.key, json.dumps({"unit_risk": 5.0, "qty": BOT}))
    db.insert_fill(Fill(exec_id="E1", broker_order_id="12", instrument=btc, side=Side.BUY,
                        qty=BOT, price=100.0, ts=OPENED + timedelta(seconds=2)))  # fmt: skip
    if owner and journal_owner:
        db.insert_fill(Fill(exec_id="X1", broker_order_id="perm:5", instrument=btc,
                            side=Side.BUY, qty=owner, price=99.0,
                            ts=OPENED + timedelta(minutes=10)))  # fmt: skip
    broker.positions_list.append(Position(instrument=btc, qty=BOT + owner, avg_cost=99.0))
    broker.quotes[btc.key] = q(btc, bid, bid)


def fill_all(broker: FakeBroker) -> None:
    for rec in broker.orders:
        rec.status = OrderStatus.FILLED
        rec.filled_qty = rec.request.qty
        pos = broker.positions_list[0]
        broker.positions_list[0] = pos.model_copy(update={"qty": pos.qty - rec.request.qty})
    broker.orders.clear()


@pytest.mark.parametrize("journal_owner", [True, False])
async def test_watchdog_stop_sells_only_the_bots_part(db: Database, journal_owner) -> None:
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.5, journal_owner=journal_owner)
    beat(db, clock, age=60)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(BOT)]
    fill_all(broker)
    for _ in range(5):
        clock.advance(seconds=30)
        beat(db, clock, age=60)
        await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(BOT)]
    assert broker.positions_list[0].qty == pytest.approx(0.5)
    # Not marked sold: the app decides from its journal when it is back (a late fill of
    # the bot's entry stays the bot's).
    assert not sold_by_watchdog(db, "CRYPTO:BTC", db.get_position_meta("CRYPTO:BTC"))


async def test_watchdog_flatten_and_kill_sell_only_the_bots_part(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.5, journal_owner=False, bid=100.0)
    beat(db, clock, age=400)  # long outage: flatten
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(BOT)]


async def test_watchdog_without_the_owner_still_sells_everything(db: Database) -> None:
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.0, journal_owner=False)
    beat(db, clock, age=60)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(BOT)]


async def test_owner_buys_after_the_watchdog_sold_everything(db: Database) -> None:
    """Pure bot position sold in full; the owner buys before the two flat reads."""
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.0, journal_owner=False)
    beat(db, clock, age=60)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert len(broker.placed) == 1
    fill_all(broker)
    broker.positions_list[0] = broker.positions_list[0].model_copy(update={"qty": 0.5})
    for _ in range(5):
        clock.advance(seconds=30)
        beat(db, clock, age=60)
        await watchdog.tick()
    assert len(broker.placed) == 1, "the owner's 0.5 BTC is never sold"
    assert db.get_kv(WATCHDOG_CLOSED_PREFIX + "CRYPTO:BTC") is None


async def test_bots_entry_filling_after_the_app_stopped_is_still_sold(db: Database) -> None:
    """The app stopped with its BTC entry resting; 0.004 of 0.01 was journaled, the other
    0.006 filled since. Within the bot's entry quantity and with no owner buy anywhere, the
    unexplained part is taken as the bot's: protection first."""
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.0, journal_owner=False)
    db._exec("UPDATE fills SET qty = 0.004")
    beat(db, clock, age=60)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(BOT)]


async def test_an_ioc_sale_filled_in_part_is_finished_but_never_into_the_owners(
    db: Database,
) -> None:
    """The watchdog's IOC sell of the bot's 0.01 fills 0.004 and the rest is cancelled: it
    sells the bot's other 0.006 next, and never the owner's 0.5."""
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.5, journal_owner=True)
    beat(db, clock, age=60)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(BOT)]
    rec = broker.orders.pop()  # IOC: 0.004 filled, the rest cancelled
    rec.status, rec.filled_qty = OrderStatus.CANCELLED, 0.004
    pos = broker.positions_list[0]
    broker.positions_list[0] = pos.model_copy(update={"qty": pos.qty - 0.004})
    for _ in range(4):
        clock.advance(seconds=30)
        beat(db, clock, age=60)
        await watchdog.tick()
        for r in broker.orders:  # later sells fill in full
            r.status, r.filled_qty = OrderStatus.FILLED, r.request.qty
            p = broker.positions_list[0]
            broker.positions_list[0] = p.model_copy(update={"qty": p.qty - r.request.qty})
        broker.orders.clear()
    assert [round(o.qty, 6) for o in broker.placed] == [BOT, 0.006]
    assert broker.positions_list[0].qty == pytest.approx(0.5)
