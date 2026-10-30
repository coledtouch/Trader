"""The owner's coins next to the bot's across app outages, the crypto watchdog's sells and
days IBKR no longer reports (review round 38)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from tests import test_watchdog as tw
from tests.test_engine import _BTC, _hold_btc, _part, _restart_next_morning, _resting_entry
from tests.test_owner_shares import (
    F,
    _watchdog_sells,
    bot_sells,
    owner_buys,
    owner_sells,
    ticks,
)
from tests.test_watchdog_owner import BOT, fill_all, setup
from trader.db import Database
from trader.engine import Engine
from trader.models import (
    AIDecision,
    AssetClass,
    Fill,
    Mode,
    OrderStatus,
    Position,
    Side,
    TradeAction,
)
from trader.ownership import UNREPORTED_EXIT, note_watchdog_sale
from trader.timeutil import trading_day, trading_day_start
from trader.watchdog import position_marker

KEY = "CRYPTO:BTC"
_ids = itertools.count(1)


# ----------------------------------------------------------------------------- engine
async def test_the_owners_sale_on_an_unreported_day_leaves_the_bots_btc_sellable(
    db, config
) -> None:
    """The owner sells his journaled 0.5 BTC while the app is down across midnight (IBKR
    no longer reports it): what is held is the bot's, and its stop sells it."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 29, 3, 30, tzinfo=UTC)  # the app is down
    owner_sells(h, _BTC, 0.5, 60100.0)
    engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(8):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(mine)]


@pytest.mark.parametrize("filled_frac", [0.6, 0.0])
async def test_a_watchdog_sell_of_unknown_fill_never_releases_the_bots_coins(
    db, config, filled_frac: float
) -> None:
    """No owner coins. The watchdog's IOC filled part or nothing and its record still
    reads the asked quantity; after midnight the app keeps managing the bot's coins (its
    stop sells them), never journaling a sale it cannot see."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    h.clock.t = datetime(2026, 10, 29, 2, 30, tzinfo=UTC)  # the app is down
    sold = round(mine * filled_frac, 8)
    if sold > 0:
        h.broker.pos[KEY][0] -= sold
        h.broker.cash += sold * 58990.0
        h.broker.fills.append(
            Fill(exec_id=f"WD{next(_ids)}", broker_order_id=f"perm:{next(_ids)}",
                 instrument=_BTC, side=Side.SELL, qty=sold, price=58990.0, commission=1.0,
                 ts=h.clock())
        )  # fmt: skip
    note_watchdog_sale(db, KEY, position_marker(meta), "77", mine, h.clock())
    h.broker.set_price("BTC", 59500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    h.clock.t = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)  # back the same evening
    settings = db.get_settings()  # no time stop: only the stop may sell
    db.save_settings(settings.model_copy(update={
        "exits": settings.exits.model_copy(update={"time_stop_minutes": 0})
    }))  # fmt: skip
    engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                    scanner=h.scanner)  # fmt: skip
    for _ in range(4):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    h.clock.t = datetime(2026, 10, 29, 4, 0, 30, tzinfo=UTC)  # past midnight New York
    for _ in range(4):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert db.get_position_meta(KEY) is not None
    assert db.list_trades() == []
    h.broker.set_price("BTC", 55000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(4):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert KEY not in h.broker.pos


async def test_an_owner_sale_just_before_the_bots_entry_is_not_its_trade(db, config) -> None:
    """The owner sells his F 40 s before the bot's entry goes out (the symbol is flat, so
    it may): that sale is inside the entry's clock-skew window, yet never the bot's."""
    h = await _f_harness(db, config)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 2)
    h.clock.advance(minutes=45)
    await ticks(h, 1)
    owner_sells(h, F, 50, 10.3)
    h.clock.advance(seconds=40)
    entry, total = await _resting_entry(h)
    await ticks(h, 3, seconds=15)
    h.clock.advance(minutes=4)
    _part(h.broker, entry, 10, 10.01)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=2)
    _part(h.broker, entry, total - 10, 10.01)
    await ticks(h, 3, seconds=15)
    h.clock.advance(minutes=10)
    _part(h.broker, f"{entry}-TP", total, 10.5)
    await ticks(h, 4, seconds=15)
    assert [t.qty for t in db.list_trades()] == [total]


async def _f_harness(db, config):
    from tests.test_engine import harness

    return await harness(db, config, mode=Mode.PAPER)


@pytest.mark.parametrize("owner_sold", [0.001, 0.006])
async def test_a_watchdog_sell_that_filled_nothing_is_no_sale(
    db, config, owner_sold: float
) -> None:
    """The watchdog's IOC was accepted and then cancelled with nothing filled: the owner's
    later sales of his coins stay his, and the bot's BTC keeps its stop."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, 0.01, 60000.0)
    await ticks(h, 1, seconds=15)
    marker = position_marker(db.get_position_meta(KEY))
    h.clock.advance(seconds=30)
    note_watchdog_sale(db, KEY, marker, "77", mine, h.clock())
    h.clock.advance(seconds=10)
    note_watchdog_sale(db, KEY, marker, "77", 0.0, h.clock(), done=True)
    await ticks(h, 2, seconds=15)
    owner_sells(h, _BTC, owner_sold, 60100.0)
    await ticks(h, 3, seconds=15)
    assert db.list_trades() == []
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 4, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(mine)]


# ----------------------------------------------------------------------------- watchdog
async def test_the_watchdog_counts_an_estimated_sale_of_its_own_once(db, config) -> None:
    """The app journaled the watchdog's earlier-day partial sale at an estimate and still
    manages the bot's rest; in the next outage the watchdog sells that rest."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=30)  # the app is down: the watchdog's IOC fills 60%
    part = round(mine * 0.6, 8)
    _watchdog_sells(h, meta, mine, filled=part)
    note_watchdog_sale(db, KEY, position_marker(meta), "B1", part, h.clock(), done=True)
    h.engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 60600.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 6, seconds=15)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    assert any(f.broker_order_id == UNREPORTED_EXIT for f in db.list_fills(since=since))
    rest = round(mine - part, 8)
    assert db.get_position_meta(KEY) is not None
    clock = tw.Clock(h.clock() + timedelta(minutes=5))  # the app is down again
    broker = tw.FakeBroker()
    broker.positions_list.append(Position(instrument=_BTC, qty=h.broker.pos[KEY][0],
                                          avg_cost=60000.0))  # fmt: skip
    broker.quotes[KEY] = tw.q(_BTC, 58000.0, 58000.0)
    tw.beat(db, clock, age=60)
    watchdog, _ = tw.make(db, broker, clock)
    await watchdog.tick()
    clock.advance(seconds=10)
    tw.beat(db, clock, age=70)
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(rest, abs=1e-6)]


async def _close_proposed(h) -> int:
    db = h.db
    db.save_settings(db.get_settings().model_copy(update={"autopilot": False}))
    h.engine.reload_settings()
    h.decider.decisions = [AIDecision(action=TradeAction.CLOSE, symbol="BTC",
                                      asset_class=AssetClass.CRYPTO, conviction=0.6,
                                      rationale="fading")]  # fmt: skip
    await h.engine.run_cycle()
    h.decider.decisions = []
    [pending] = [o for o in db.list_orders(limit=50) if o.status == OrderStatus.PENDING_APPROVAL]
    assert pending.id is not None
    return pending.id


async def test_an_approved_close_sold_just_before_a_crash_keeps_the_owners_coins(
    db, config
) -> None:
    """The owner adds BTC while a CLOSE waits for approval; the approved close sells the
    bot's part and the app dies before the journal sees it: the watchdog sells nothing."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    owner_buys(h, _BTC, 0.01, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=1)
    pending = await _close_proposed(h)
    h.clock.advance(minutes=3)
    owner_buys(h, _BTC, 0.005, 60000.0)
    await ticks(h, 1, seconds=15)
    h.clock.advance(minutes=1)
    await h.engine.approve_order(pending)
    held = h.broker.pos[KEY][0]
    clock = tw.Clock(h.clock() + timedelta(seconds=400))
    broker = tw.FakeBroker()
    broker.positions_list.append(Position(instrument=_BTC, qty=held, avg_cost=60000.0))
    broker.quotes[KEY] = tw.q(_BTC, 60000.0, 60000.0)
    watchdog, _ = tw.make(db, broker, clock, started_at=clock.t - timedelta(hours=1))
    await watchdog.tick()
    assert broker.placed == []


async def test_an_expired_close_proposal_leaves_the_watchdog_guarding(db, config) -> None:
    """A CLOSE proposal that expired was never sent: once the owner sells some of his
    coins in an outage, the watchdog still sells all of the bot's."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    owner_buys(h, _BTC, 0.01, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=5)
    await _close_proposed(h)
    h.clock.advance(minutes=11)
    h.engine._expire_approvals(h.clock())
    await ticks(h, 2, seconds=15)
    held = h.broker.pos[KEY][0]
    clock = tw.Clock(h.clock() + timedelta(seconds=400))
    broker = tw.FakeBroker()
    broker.positions_list.append(Position(instrument=_BTC, qty=held - 0.004, avg_cost=60000.0))
    broker.quotes[KEY] = tw.q(_BTC, 60000.0, 60000.0)
    watchdog, _ = tw.make(db, broker, clock, started_at=clock.t - timedelta(hours=1))
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(mine)]


async def test_the_owner_buying_after_the_watchdogs_full_sale_is_never_sold(
    db: Database,
) -> None:
    clock = tw.Clock()
    broker = tw.FakeBroker()
    setup(db, broker, owner=0.0, journal_owner=False)
    tw.beat(db, clock, age=60)
    watchdog, _ = tw.make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [BOT]
    fill_all(broker)
    # The owner buys 0.003 in IBKR Mobile before two flat reads (less than the entry).
    broker.positions_list[0] = broker.positions_list[0].model_copy(update={"qty": 0.003})
    for _ in range(6):
        clock.advance(seconds=30)
        tw.beat(db, clock, age=60)
        await watchdog.tick()
    assert len(broker.placed) == 1


async def test_the_owner_buying_after_a_partial_watchdog_sale_keeps_his_coins(
    db: Database,
) -> None:
    clock = tw.Clock()
    broker = tw.FakeBroker()
    setup(db, broker, owner=0.0, journal_owner=False)
    tw.beat(db, clock, age=60)
    watchdog, _ = tw.make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    rec = broker.orders.pop()  # the IOC fills 0.004 of 0.01, the rest is cancelled
    rec.status, rec.filled_qty = OrderStatus.CANCELLED, 0.004
    pos = broker.positions_list[0]
    broker.positions_list[0] = pos.model_copy(update={"qty": pos.qty - 0.004 + 0.003})
    for _ in range(6):
        clock.advance(seconds=30)
        tw.beat(db, clock, age=60)
        await watchdog.tick()
    assert [round(o.qty, 6) for o in broker.placed] == [BOT, 0.006]


# ----------------------------------------------------------------------------- review 39
def _watchdog_sold_overnight(h, meta: dict, sold: float, asked: float) -> None:
    """The watchdog's sell of the bot's BTC at 58900, done and recorded as it keeps it."""
    h.broker.pos[KEY][0] -= sold
    h.broker.cash += sold * 58900.0
    h.broker.fills.append(
        Fill(exec_id=f"WD{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=_BTC,
             side=Side.SELL, qty=sold, price=58900.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    marker = position_marker(meta)
    order_id = f"B{next(_ids)}"
    note_watchdog_sale(h.db, KEY, marker, order_id, asked, h.clock())
    note_watchdog_sale(h.db, KEY, marker, order_id, sold, h.clock(), done=True)


@pytest.mark.parametrize("part", [1.0, 0.6])
async def test_a_small_owner_buy_next_to_the_watchdogs_sale_is_never_sold_next_day(
    db, config, part: float
) -> None:
    """The owner buys less BTC than the bot's entry while the app is down; the watchdog
    sells the bot's (all, or 60% of it); the app comes back the next day. The owner's
    coins are never sold: the bot sells only its own rest, if any."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    h.clock.t = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)
    owner = 0.001
    owner_buys(h, _BTC, owner, 59400.0)
    h.clock.t = datetime(2026, 10, 29, 2, 10, tzinfo=UTC)
    sold = round(mine * part, 8)
    _watchdog_sold_overnight(h, meta, sold, mine)
    engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 59500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(12):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    rest = round(mine - sold, 8)
    assert bot_sells(h, KEY) == ([pytest.approx(rest, abs=1e-6)] if rest > 0 else [])
    assert h.broker.pos[KEY][0] == pytest.approx(owner, abs=1e-6)  # crypto rounds down


async def test_a_second_earlier_day_watchdog_sale_is_journaled(db, config) -> None:
    """Two outages, each ending the next day, each with a watchdog sale of the bot's BTC
    next to the owner's: both are journaled and the bot's trade closes."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos[KEY][0]
    meta = db.get_position_meta(KEY)
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=30)
    part = round(mine * 0.6, 8)
    _watchdog_sold_overnight(h, meta, part, mine)
    h.engine = _restart_next_morning(h, config)
    h.broker.set_price("BTC", 60600.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 6, seconds=15)
    assert db.get_position_meta(KEY) is not None  # the bot's rest is still held
    h.clock.t = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)  # Thursday night: down again
    rest = round(mine - part, 8)
    _watchdog_sold_overnight(h, meta, rest, rest)
    h.clock.t = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)  # Friday 10:00 New York
    every_fill = h.broker.fills_since

    async def today_only(since: datetime) -> list[Fill]:
        midnight = trading_day_start(trading_day(h.clock()))
        return [f for f in await every_fill(since) if f.ts >= midnight]

    h.broker.fills_since = today_only
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 20, seconds=15)
    assert h.broker.pos[KEY][0] == pytest.approx(0.5)
    assert db.get_position_meta(KEY) is None
    [trade] = db.list_trades()
    assert trade.qty == pytest.approx(mine)


async def test_an_owner_buy_before_the_first_part_of_an_entry_exits_stays_his(db, config) -> None:
    """7 of the bot's 24 fill and its target sells them while the rest still works; the
    owner bought 50 in between. When the rest has filled, the owner's sale of 20 is his,
    not the bot's trade, and the bot's 17 keep their stop."""
    h = await _f_harness(db, config)
    b = h.broker
    entry, total = await _resting_entry(h)
    h.clock.advance(minutes=1)
    _part(b, entry, 7, 10.01)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 2, seconds=15)
    _part(b, f"{entry}-TP", 7, 10.5)
    await ticks(h, 2, seconds=15)
    assert [t.qty for t in db.list_trades()] == [7]
    h.clock.advance(minutes=5)
    _part(b, entry, total - 7, 10.01)
    await ticks(h, 3, seconds=15)
    h.clock.advance(minutes=5)
    owner_sells(h, F, 20, 10.3)
    await ticks(h, 4, seconds=15)
    assert [t.qty for t in db.list_trades()] == [7]
    assert db.get_position_meta("STK:F") is not None
    assert b.working("STK:F", "stop")


@pytest.mark.parametrize("journaled", [0.0, 0.004])
async def test_a_quick_reentry_after_the_bots_own_exit_is_still_guarded(
    db: Database, journaled: float
) -> None:
    """No other client in BTC. The bot's previous trip sold 30 s before its new entry was
    sent; the new entry filled while the app was down (or in part before): that earlier
    sell is no sale of the new position, and the watchdog sells all of it."""
    btc = tw.crypto()
    opened = tw.NOW - timedelta(hours=1)
    db.insert_fill(Fill(exec_id="P1", broker_order_id="7", instrument=btc, side=Side.BUY,
                        qty=0.02, price=100.0, ts=opened - timedelta(minutes=20)))  # fmt: skip
    db.insert_fill(Fill(exec_id="P2", broker_order_id="8", instrument=btc, side=Side.SELL,
                        qty=0.02, price=101.0, ts=opened - timedelta(seconds=30)))  # fmt: skip
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
    broker.quotes[btc.key] = tw.q(btc, 90.0, 90.0)  # below the 95 stop
    tw.beat(db, clock, age=60)
    watchdog, _ = tw.make(db, broker, clock)
    await watchdog.tick()
    await watchdog.tick()
    assert [o.qty for o in broker.placed] == [pytest.approx(0.01)]
