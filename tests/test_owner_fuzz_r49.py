"""Regression tests for the round 49 fuzz triage fixes: whose units are whose when the
owner trades the bot's symbols by hand (docs/SPEC.md 7.8). Each section names the fuzz
seeds it came from."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_engine import (
    _BTC,
    _hold_btc,
    _part,
    _restart_next_morning,
    _resting_entry,
    enter_f,
    harness,
    hold,
)
from tests.test_owner_edges import _paper
from tests.test_owner_shares import F, bot_sells, owner_buys, owner_sells, ticks
from tests.test_owner_unseen import KEY, _buy_btc, _crypto_paper, _restart_at, _watchdog
from tests.test_watchdog import NOW, Clock, FakeBroker, beat, make
from tests.test_watchdog_owner import BOT, setup
from trader.broker.base import BrokerError
from trader.db import Database
from trader.engine import Engine
from trader.models import (
    OPEN_ORDER_STATUSES,
    AIDecision,
    AssetClass,
    Fill,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TradeAction,
)
from trader.ownership import (
    EXTRA_PREFIX,
    OWNER_HIDDEN,
    WATCHDOG_SALES_PREFIX,
    WATCHDOG_TAG_PREFIX,
    read_extra,
    read_hidden,
    watchdog_sales_end,
)
from trader.timeutil import trading_day, trading_day_start
from trader.watchdog import position_marker, sold_by_watchdog

# ------------------------------------------------------- a stop put back by a refused close
# Fuzz seeds 786, 2320, 2733, 2772, 3690, 3741. The bot's bracket stop sells part of its
# shares while a close cancels it: the quantity the close read before its cancels is more
# than the bot still holds, so neither its sell nor a stop it puts back is sized from that
# read.


NOON = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def _stops(h) -> list[float]:
    """What the app's protective stops working for F still cover."""
    return [
        rec.request.qty - rec.filled_qty
        for oid, rec in h.broker.orders.items()
        if rec.request.instrument.key == "STK:F"
        and rec.request.side == Side.SELL
        and rec.request.stop_loss is not None
        and rec.status in OPEN_ORDER_STATUSES
        and h.broker._active(oid)
    ]


async def test_a_stop_put_back_after_a_failed_close_never_covers_his_shares(db, config) -> None:
    """The bot holds 24 F, the owner 30. Close sells the bot's 24: its bracket stop sells
    12 of them as the close cancels it, then IBKR's open-orders request times out. The stop
    put back covers the bot's 12, not 24 (IBKR takes a 24 stop with 42 held): when F drops
    through it, the owner's 30 are still held."""
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 0})
    await enter_f(h)  # the bot's 24 F, bracket stop 9.8
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    real_cancel = h.broker.cancel_order

    async def cancel(oid: str) -> None:
        if oid == stop and h.broker.orders[stop].filled_qty == 0:
            _part(h.broker, stop, 12, 9.8)  # the stop sells 12 as its cancel goes out
            h.broker.fail.add("open_orders")  # then the open-orders request times out
        await real_cancel(oid)

    h.broker.cancel_order = cancel
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    h.broker.cancel_order = real_cancel
    h.broker.fail.discard("open_orders")
    assert h.broker.pos["STK:F"][0] == 42
    assert sum(_stops(h)) <= 12, "the stop put back covers only the bot's 12"
    h.broker.set_price("F", 9.7)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 30, "the owner's 30 F are never sold"


async def test_a_stop_put_back_for_a_refused_close_covers_only_what_the_bot_holds(
    db, config
) -> None:
    """The bot holds 24 F, no owner's. Close cancels the bracket stop, which sells 12 of
    them meanwhile: the close never sells, nor puts a stop back for, the 24 it read before
    its cancels. The owner buys 5 afterwards; when F drops through the stop, his 5 stay."""
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 0})
    await enter_f(h)  # the bot's 24 F, bracket stop 9.8
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    real_cancel = h.broker.cancel_order

    async def cancel(oid: str) -> None:
        if oid == stop and h.broker.orders[stop].filled_qty == 0:
            _part(h.broker, stop, 12, 9.8)
        await real_cancel(oid)

    h.broker.cancel_order = cancel
    try:
        await h.engine.close_position("STK:F")
    except (BrokerError, ValueError):
        pass
    h.broker.cancel_order = real_cancel
    assert all(r.qty <= 12 for r in h.broker.placed if r.side == Side.SELL and r.is_exit), (
        "nothing is sold, or protected, beyond the bot's 12"
    )
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 5, 9.9)
    await ticks(h, 2, seconds=15)
    h.broker.set_price("F", 9.7)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 5, "the owner's 5 F are never sold"


async def test_a_close_never_sells_the_quantity_it_read_before_its_cancels(db, config) -> None:
    """The bot holds 24 F, no owner's. Close cancels the bracket stop, which sells 12 of
    them meanwhile, and the owner buys 12 in TWS while the target's cancel goes out: 24
    are held again, so IBKR takes a sell of the 24 read before the cancels. The close
    sells the bot's 12 only; the owner's 12 stay."""
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 0})
    await enter_f(h)  # the bot's 24 F, bracket stop 9.8, target 10.5
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    [target] = h.broker.working("STK:F", "target")
    real_cancel = h.broker.cancel_order

    async def cancel(oid: str) -> None:
        if oid == stop and h.broker.orders[stop].filled_qty == 0:
            _part(h.broker, stop, 12, 9.8)
        if oid == target:
            owner_buys(h, F, 12, 9.8)
        await real_cancel(oid)

    h.broker.cancel_order = cancel
    await h.engine.close_position("STK:F")
    h.broker.cancel_order = real_cancel
    assert h.broker.pos["STK:F"][0] == 12, "the owner's 12 F are never sold"


# ----------------------------------- an entry fill the broker saw, next to his hidden coins
# Fuzz seeds 2578, 2007. The bot's BTC entry filled while the app was down, on a day IBKR
# no longer reports, next to coins the owner bought then. The app's order row still says
# what the entry filled, so the owner's coins no fill shows are recorded beyond all of it
# (`owner_hidden`); the exits lessened the entry's unseen part by those coins anyway, and
# its stop sold nothing.


@pytest.mark.parametrize("today", [0.0, 0.002])
async def test_an_entry_the_broker_saw_fill_keeps_its_stop_next_to_his_unreported_coins(
    db,
    config,
    today: float,
) -> None:
    """Wednesday: the bot's BTC entry rests; with the app down that evening the owner buys
    twice its size and the entry fills. Thursday IBKR reports neither, but still knows the
    entry filled; the owner may buy more today. The stop sells the bot's entry, never his
    coins."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)  # the app saw it working
    h.clock.t = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
    his = round(total * 2, 8)
    owner_buys(h, _BTC, his, 60000.0)
    h.clock.t = datetime(2026, 10, 28, 20, 0, 5, tzinfo=UTC)
    _part(h.broker, entry, total, 60000.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 4, seconds=15)
    assert OWNER_HIDDEN in (db.get_kv(EXTRA_PREFIX + KEY) or "")
    if today:
        owner_buys(h, _BTC, today, 60000.0)
        await ticks(h, 10, seconds=15)
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 4, seconds=15)
    assert bot_sells(h, KEY) == [pytest.approx(total, abs=1e-8)]
    assert h.broker.pos[KEY][0] == pytest.approx(his + today, abs=1e-8)


# ------------------------------------ the settled rest after the watchdog's unreported sale
# Fuzz seed 3296. Part of the bot's BTC entry filled while the app was down and the
# crypto watchdog sold it, on a day IBKR no longer reports; the next day the entry's rest
# fills. The position read then matched the journal's bot units, so the rest was settled,
# and the exits took the watchdog's sale off the bot's coins a second time.


def _sells(h) -> list[OrderRequest]:
    return [r for r in h.broker.placed if r.side == Side.SELL and r.instrument.key == KEY
            and r.stop_loss is None]  # fmt: skip


def _wd(req: OrderRequest) -> bool:
    return (req.trace_id or "").startswith(WATCHDOG_TAG_PREFIX)


async def test_a_watchdog_sale_ibkr_no_longer_reports_comes_off_the_bots_coins_once(
    db,
    config,
) -> None:
    """Wednesday: the bot's BTC entry rests; with the app down 30% of it fills and the
    watchdog sells those coins. Thursday IBKR reports neither; the entry's rest fills. The
    stop sells all the bot holds: no owner coins are next to it."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)  # the app saw it working
    h.clock.t = datetime(2026, 10, 28, 18, 20, tzinfo=UTC)
    first = round(total * 0.3, 6)
    _part(h.broker, entry, first, 60000.0)
    wd = _watchdog(h)
    wd._started_at = h.clock() - timedelta(hours=1)
    for _ in range(3):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos, "the watchdog sold what had filled"
    assert db.get_kv(WATCHDOG_SALES_PREFIX + KEY), "its sale is recorded"
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    rest = round(total - first, 8)
    _part(h.broker, entry, rest, 59900.0)
    assert h.broker.orders[entry].status == OrderStatus.FILLED
    for _ in range(10):  # the watchdog keeps polling: it reads its sale done
        await ticks(h, 1, seconds=15)
        await wd.tick()
    assert '"done"' in (db.get_kv(WATCHDOG_SALES_PREFIX + KEY) or "")
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 4, seconds=15)
    assert KEY not in h.broker.pos, "every coin held was the bot's"
    app = [q for q, r in zip(bot_sells(h, KEY), _sells(h), strict=True) if not _wd(r)]
    assert app == [pytest.approx(rest, abs=1e-8)]


# ----------------------------------------------------------- his hidden units found in full
# Fuzz seed 1532. His sale today took far more than his seen units: each look for his
# units no fill shows added only what was still unexplained, three looks found too few,
# and the rest of his sale closed the bot's trade.


async def test_his_hidden_units_are_found_in_full_and_his_sale_keeps_the_bots(
    db,
    config,
) -> None:
    """Wednesday: the bot buys 24 F, its target sells 12 of them, the owner buys 61 (all
    journaled), then 50 more with the app down. Thursday morning he sells all 111 of his.
    The bot's 12 are still its own: no trade closes on his sale, and a Close sells 12."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry = await enter_f(h)
    assert entry.request.qty == 24
    await ticks(h, 2)
    _part(h.broker, f"{entry.broker_order_id}-TP", 12, 10.5)
    await ticks(h, 2)
    owner_buys(h, F, 61, 10.0)
    await ticks(h, 3)
    assert db.list_trades() == []  # 12 of the bot's 24 still held
    h.clock.t = datetime(2026, 10, 28, 21, 2, tzinfo=UTC)  # the app is down
    owner_buys(h, F, 50, 10.0)
    h.clock.t = datetime(2026, 10, 29, 13, 40, tzinfo=UTC)  # Thursday, still down
    owner_sells(h, F, 111, 10.1)
    assert h.broker.pos["STK:F"][0] == 12
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3)
    hidden = read_extra(db, "STK:F").get(OWNER_HIDDEN)
    assert hidden and sum(q for _, q in hidden) == 50, db.get_kv(EXTRA_PREFIX + "STK:F")
    assert db.list_trades() == [], "his sale closed the bot's trade"
    assert db.get_position_meta("STK:F") is not None
    await h.engine.close_position("STK:F")
    assert bot_sells(h, "STK:F")[-1] == 12
    assert "STK:F" not in h.broker.pos


# ------------------------------------------------ the first sync reads the order rows first
# Fuzz seed 2883. On a connection's first tick the journal was replayed (and his hidden
# units looked for) before the order rows were synced: an entry's rest that lapsed while
# the app was down still counted as maybe filled, his units were taken for it, and his
# sale closed the bot's trade.


async def test_a_lapsed_rest_never_hides_his_units_on_the_first_tick(
    db,
    config,
) -> None:
    """Wednesday: the bot's entry of 24 F fills 12, its target sells 9 of them and the owner
    buys 28 (all journaled); with the app down the rest of the entry lapses at the close and
    the owner buys 3 more that evening. Thursday morning he sells his 31. The bot's 3 are
    still its own after the restart: no trade closes on his sale, and a Close sells 3."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    _part(h.broker, entry, 12, 9.98)
    await ticks(h, 2, seconds=15)
    _part(h.broker, f"{entry}-TP", 9, 10.5)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 28, 10.0)
    await ticks(h, 3)
    assert db.list_trades() == []  # 3 of the bot's 12 still held
    h.clock.t = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)  # the app is down: the rest lapses
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    h.clock.t = datetime(2026, 10, 29, 3, 45, tzinfo=UTC)  # Wednesday 23:45 New York
    owner_buys(h, F, 3, 10.0)
    h.clock.t = datetime(2026, 10, 29, 10, 56, tzinfo=UTC)  # Thursday, still down
    owner_sells(h, F, 31, 10.0)
    assert h.broker.pos["STK:F"][0] == 3
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3)
    assert db.list_trades() == [], "his sale closed the bot's trade"
    assert db.get_position_meta("STK:F") is not None
    await h.engine.close_position("STK:F")
    assert bot_sells(h, "STK:F")[-1] == 3
    assert "STK:F" not in h.broker.pos


# --------------------------------------------- what the app saw the entry fill is the bot's
# Fuzz seeds 2833, 1139. An entry that filled on a day IBKR no longer reports is in no
# fill: the exits counted it only as the unseen part of the entry, so the owner's sale or
# buy where no fill shows it shifted the bot's part (it sold 7 of its 20, or none of its
# 24).


async def test_his_unreported_sale_never_shrinks_the_bots_filled_entry(
    db,
    config,
) -> None:
    """Wednesday: the bot's entry of 24 F rests; the owner buys 27 (journaled). With the app
    down the entry fills 20 and lapses at the close, and the owner sells 13 of his. Thursday a
    Close sells the bot's 20 and keeps his 14."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 27, 10.0)
    await ticks(h, 3)
    h.clock.t = datetime(2026, 10, 28, 15, 30, tzinfo=UTC)  # the app is down from here
    _part(h.broker, entry, 20, 9.98)
    h.clock.t = datetime(2026, 10, 28, 15, 31, tzinfo=UTC)
    owner_sells(h, F, 13, 10.0)
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # the rest lapses at the close
    assert h.broker.pos["STK:F"][0] == 34
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    await h.engine.close_position("STK:F")
    assert bot_sells(h, "STK:F")[-1] == 20
    assert h.broker.pos["STK:F"][0] == 14


async def test_his_unreported_buy_never_makes_the_bots_filled_entry_unknown(
    db,
    config,
) -> None:
    """Wednesday: the bot's entry of 24 F fills at once and the owner buys 5 before the app
    journals either (it goes down). Thursday he buys 10 more. A Close sells the bot's 24 and
    keeps his 15."""
    h = await _paper(db, config, datetime(2026, 10, 28, 16, 41, tzinfo=UTC))
    entry = await enter_f(h)
    assert entry.status == OrderStatus.FILLED and entry.request.qty == 24
    h.clock.advance(seconds=2)
    owner_buys(h, F, 5, 10.0)
    h.clock.t = datetime(2026, 10, 29, 13, 30, tzinfo=UTC)  # Thursday, the app still down
    owner_buys(h, F, 10, 10.0)
    assert h.broker.pos["STK:F"][0] == 39
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    await h.engine.close_position("STK:F")
    assert bot_sells(h, "STK:F")[-1] == 24
    assert h.broker.pos["STK:F"][0] == 15


# ---------------------------------------- his sale with the position flat at the first look
# Fuzz seeds 15, 1129. The owner's sale of units no fill shows, with the position already
# flat when the app first looks, is never part of the bot's trade.


@pytest.mark.parametrize("back_after", [15, 300])
async def test_his_unreported_units_sold_before_a_flat_first_look_are_his(
    db,
    config,
    back_after: int,
) -> None:
    """Wednesday the bot's 24 F fill (journaled) and the owner buys 5 that evening with the
    app down: Thursday's executions do not show his buy. Thursday, before the app comes
    back, he sells his 5 and the bot's stop sells its 24, so the app first sees the
    position flat (15 s or 5 minutes later). The journal has one bot trade of 24, never one
    of 29 with his 5 in it."""
    h = await harness(db, config, mode=Mode.PAPER, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                      exits={"time_stop_minutes": 0})  # fmt: skip
    await enter_f(h)
    await ticks(h, 2)
    assert [f.qty for f in db.list_fills() if f.side == Side.BUY] == [24]
    h.clock.t = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
    owner_buys(h, F, 5, 10.0)
    h.engine = _restart_next_morning(h, config)  # Thursday 10:00 New York, no tick yet
    owner_sells(h, F, 5, 10.1)
    h.clock.advance(seconds=30)
    h.broker.set_price("F", 9.5)  # the bot's bracket stop sells its 24
    assert "STK:F" not in h.broker.pos
    h.clock.advance(seconds=back_after)
    await h.engine.monitor_tick()
    await ticks(h, 12, seconds=15)
    assert [t.qty for t in db.list_trades()] == [24]
    assert db.get_position_meta("STK:F") is None
    h.clock.advance(minutes=20)  # the flat-position path finds nothing left to estimate
    await ticks(h, 4)
    assert [t.qty for t in db.list_trades()] == [24]
    assert h.clock() - datetime(2026, 10, 29, 14, 0, tzinfo=UTC) < timedelta(hours=1)


# ------------------------------------------------ the bot's own exit cancelling its bracket
# Fuzz seed 2415. The bot's own exit cancelling its bracket is no sign that a bracket
# child sold its units unseen, so his sale of units no fill shows is not journaled as the
# bot's while his units cannot be looked for yet.


async def test_his_sale_just_before_the_bots_close_is_never_its_trade(
    db,
    config,
) -> None:
    """Wednesday the bot's 24 F fill (journaled) and the owner buys 4 that afternoon with
    the app down: Thursday's executions do not show his buy. Thursday he sells 3 as the
    app comes back, and the bot's position is Closed before his units can be looked for
    (a fill of the last two minutes): the Close sells the 21 the fills surely show as the
    bot's. Its own Close cancelled the bracket; that is no unseen sale of a bracket child,
    so his 3 are never journaled as the bot's: the bot's last 3 keep a stop, and its trade
    is one of 24 once they are sold. His last unit stays."""
    h = await harness(db, config, mode=Mode.PAPER, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                      exits={"time_stop_minutes": 0})  # fmt: skip
    await enter_f(h)
    await ticks(h, 2)
    assert [f.qty for f in db.list_fills() if f.side == Side.BUY] == [24]
    h.clock.t = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
    owner_buys(h, F, 4, 10.0)
    h.engine = _restart_next_morning(h, config)  # Thursday 10:00 New York, no tick yet
    owner_sells(h, F, 3, 10.0)
    await ticks(h, 1, seconds=15)
    await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 4  # 21 sold: the bot's 3 and his 1 are left
    await ticks(h, 12, seconds=15)
    assert db.list_trades() == []  # the bot still holds 3: its trade is not finished
    assert h.broker.pos["STK:F"][0] == 4
    h.broker.set_price("F", 9.5)
    await ticks(h, 10, seconds=15)
    assert h.broker.pos["STK:F"][0] == 1
    assert [t.qty for t in db.list_trades()] == [24]


# --------------------------------- the watchdog's flat-sale estimate, dated after the entry
# Fuzz seeds 614, 781, 448, 2424, 3465, 915. The crypto watchdog sold flat a bot entry the
# journal never saw fill, on a day IBKR no longer reports. The journal's window starts a
# minute before the entry was sent and held a fill from that minute (the owner's round
# trip, the bot's previous trip): the estimate was dated just after it, where the replay
# takes it to have sold nothing of the bot's.


@pytest.mark.parametrize("before", ["owner_round_trip", "previous_trip"])
async def test_a_watchdog_flat_sale_estimate_is_dated_after_the_entry(
    db, config, before: str
) -> None:
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 14, 50, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    first_qty = 0.0
    if before == "previous_trip":
        # Wednesday 10:50 New York: a BTC trip of the bot, stopped out at 10:59:40.
        _buy_btc(h)
        await h.engine.run_cycle()
        h.decider.decisions = []
        await ticks(h, 2, seconds=15)
        first_qty = h.broker.pos[KEY][0]
        h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
        for second in (25, 40):  # two looks below the stop
            h.clock.t = datetime(2026, 10, 28, 14, 59, second, tzinfo=UTC)
            await h.engine.monitor_tick()
        assert KEY not in h.broker.pos
        h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    else:
        # 10:59:30 / 10:59:40 New York: the owner buys BTC and sells it again.
        h.clock.t = datetime(2026, 10, 28, 14, 59, 30, tzinfo=UTC)
        owner_buys(h, _BTC, 0.0012, 60000.0)
        h.clock.t = datetime(2026, 10, 28, 14, 59, 40, tzinfo=UTC)
        owner_sells(h, _BTC, 0.0012, 60000.0)
    await ticks(h, 1, seconds=10)  # 10:59:50: journaled
    assert [t.qty for t in db.list_trades()] == ([pytest.approx(first_qty)] if first_qty else [])
    assert db.get_position_meta(KEY) is None
    # 11:00:00 New York: the bot's next BTC entry rests; the app goes down.
    h.clock.t = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    _buy_btc(h)
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items()
               if k == "entry" and h.broker.orders[oid].status in OPEN_ORDER_STATUSES]  # fmt: skip
    total = h.broker.orders[entry].request.qty
    meta = db.get_position_meta(KEY)
    assert meta is not None
    # While the app is down the entry fills in full and the watchdog flattens it.
    wd = _watchdog(h)
    h.clock.advance(minutes=1)
    _part(h.broker, entry, total, 60005.0)
    assert h.broker.orders[entry].status == OrderStatus.FILLED
    h.broker.set_price("BTC", 59990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    assert sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    # Thursday 10:00 New York: IBKR reports only Thursday's executions.
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 80, seconds=15)
    trades = db.list_trades()
    assert db.get_position_meta(KEY) is None
    assert "Exit of BTC not reported" in h.notes.titles()
    assert sum(t.qty for t in trades) == pytest.approx(first_qty + total), (
        "the watchdog-sold trip is journaled (the owner was told it was)"
    )
    [trip] = [t for t in trades if t.entry_ts >= meta["opened_at"] - timedelta(seconds=1)]
    assert trip.qty == pytest.approx(total)
    assert trip.exit_ts > meta["opened_at"]


async def test_a_watchdog_flat_sale_of_coins_the_entry_never_bought_is_no_bot_trade(
    db, config, monkeypatch
) -> None:
    """The bot's resting BTC entry lapses unfilled (the app saw it done with nothing
    filled); with the app down the owner buys BTC, and a watchdog that cannot tell sells
    his coins flat as the unseen part of that entry. Its estimate, dated after the entry,
    credits the unseen part only for what of the entry may really have filled beyond the
    journal: none, so no trade of the bot's."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    _buy_btc(h)
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items()
               if k == "entry" and h.broker.orders[oid].status in OPEN_ORDER_STATUSES]  # fmt: skip
    await ticks(h, 1, seconds=15)
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # it lapses, nothing filled
    await ticks(h, 1, seconds=15)
    assert db.get_position_meta(KEY) is not None
    # The app goes down; the owner buys BTC; the watchdog sells it as the bot's.
    h.clock.advance(seconds=45)
    owner_buys(h, _BTC, 0.0012, 60000.0)
    h.broker.set_price("BTC", 59990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    wd = _watchdog(h)
    with monkeypatch.context() as m:
        m.setattr("trader.watchdog.entry_held_unjournaled", _cannot_tell, raising=False)
        for _ in range(40):
            h.clock.advance(seconds=10)
            await wd.tick()
    assert KEY not in h.broker.pos
    assert sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    # Thursday 10:00 New York: IBKR reports only Thursday's executions.
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 80, seconds=15)
    assert db.get_position_meta(KEY) is None
    assert db.list_trades() == [], "the bot's entry never bought a coin"


# -------------------------------------------------- a watchdog sale next to a working entry
# Fuzz seed 3716. The watchdog flattened the filled part of the bot's BTC entry while its
# rest still worked, so it never marked the position sold flat; when the rest lapsed the
# engine released the metadata at once and the bot's trip was never journaled.


async def test_a_watchdog_sale_next_to_a_working_entry_is_journaled(db, config) -> None:
    # Wednesday 11:00 New York: the bot's BTC entry rests; the app goes down.
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    _buy_btc(h)
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items()
               if k == "entry" and h.broker.orders[oid].status in OPEN_ORDER_STATUSES]  # fmt: skip
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)
    meta = db.get_position_meta(KEY)
    # Half of it fills while the app is down; the watchdog flattens that half (stale
    # heartbeat) with the other half still working.
    part = round(total / 2, 6)
    h.clock.advance(minutes=1)
    _part(h.broker, entry, part, 60005.0)
    h.broker.set_price("BTC", 59990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    assert h.broker.orders[entry].status == OrderStatus.PARTIALLY_FILLED
    assert not sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    # Thursday 10:00 New York: IBKR reports only Thursday's executions. The entry's rest
    # still works for a while, then lapses.
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 4, seconds=15)
    assert db.get_position_meta(KEY) is not None
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    await ticks(h, 80, seconds=15)
    trades = db.list_trades()
    assert db.get_position_meta(KEY) is None
    assert [t.qty for t in trades] == [pytest.approx(part)], (
        "the watchdog's sale of the bot's coins is journaled"
    )
    [trade] = trades
    assert trade.entry_ts >= meta["opened_at"] - timedelta(seconds=1)
    assert "Exit of BTC not reported" in h.notes.titles()


# ----------------------------------------------------- the watchdog's flat sale marked done
# Fuzz seeds 700, 321. Once the watchdog had sold the bot's BTC flat, no poll recorded its
# sell as done: the app's window in which another client's sells may be the watchdog's
# never ended, and the owner's sale the next day closed the bot's trade at his price.


async def test_the_watchdogs_flat_sale_is_done_and_the_owners_later_sale_is_his(
    db, config, monkeypatch
) -> None:
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 0, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    _buy_btc(h)
    await h.engine.run_cycle()  # the entry fills; the app goes down before it ticks
    h.decider.decisions = []
    bot = h.broker.pos[KEY][0]
    wd = _watchdog(h)
    for _ in range(40):  # the heartbeat goes stale: the watchdog flattens the bot's BTC
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    marker = position_marker(db.get_position_meta(KEY))
    assert watchdog_sales_end(db, KEY, marker) is not None  # its sell is done
    # Thursday, app still down: the owner buys 0.004 BTC and later sells half of it.
    h.clock.t = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
    owner_buys(h, _BTC, 0.004, 60000.0)
    for _ in range(3):
        h.clock.advance(seconds=10)
        await wd.tick()
    h.clock.t = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)
    owner_sells(h, _BTC, 0.002, 59130.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 11, 5, tzinfo=UTC))
    await ticks(h, 80, seconds=15)
    trades = db.list_trades()
    assert [t.qty for t in trades] == [pytest.approx(bot)]
    assert trades[0].exit_ts < datetime(2026, 10, 29, tzinfo=UTC)  # the watchdog's sale
    assert h.broker.pos[KEY][0] == pytest.approx(0.002)  # his coins are left alone
    assert db.get_position_meta(KEY) is None


# ----------------------------- the flat-sale estimate after his round trip before the entry
# Fuzz seeds 321, 626. The owner bought and sold BTC in the minute before the bot's entry
# was sent; the estimate of the watchdog's flat sale was dated just after his sell, before
# the entry, so the bot's trade was never journaled.


async def test_the_watchdogs_flat_sale_is_journaled_after_his_round_trip_before_the_entry(
    db, config, monkeypatch
) -> None:
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 14, 6, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    owner_buys(h, _BTC, 0.0012, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 28, 14, 8, 30, tzinfo=UTC)
    owner_sells(h, _BTC, 0.0012, 60000.0)
    await ticks(h, 1, seconds=5)  # journaled: his round trip ends 5 s before the entry
    _buy_btc(h)
    await h.engine.run_cycle()  # 14:08:35, filled at once; the app goes down
    h.decider.decisions = []
    bot = h.broker.pos[KEY][0]
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    assert sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 9, 0, tzinfo=UTC))
    await ticks(h, 80, seconds=15)
    assert db.get_position_meta(KEY) is None
    trades = db.list_trades()
    assert [t.qty for t in trades] == [pytest.approx(bot)]
    assert trades[0].exit_ts >= trades[0].entry_ts
    assert trades[0].exit_ts - trades[0].entry_ts < timedelta(minutes=10)  # its sale


# ---------------------------------------- no estimate for coins a lapsed entry never bought
# Fuzz seed 915. The bot's BTC entry lapsed unfilled (the app saw it) and the owner
# bought BTC with the app down. The watchdog now leaves his coins alone; one that sold
# them as the unseen part of the entry (as before this round) must not have that sale
# journaled as a bot trade.


async def test_the_watchdogs_flat_sale_after_a_lapsed_entry_is_no_bot_trade(
    db, config, monkeypatch
) -> None:
    """The bot's resting BTC entry is cancelled unfilled and the app sees it; with the app
    down the owner buys BTC. The watchdog leaves his coins alone; one that cannot tell sells
    them flat, and the app journals no bot trade for that sale and releases the metadata."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 21, 17, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # nothing filled
    await ticks(h, 2, seconds=15)  # the app sees it cancelled
    owner_buys(h, _BTC, 0.0012, 58780.0)  # the app is down from here
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.0012), "his coins stay"
    with monkeypatch.context() as m:
        m.setattr("trader.watchdog.entry_held_unjournaled", _cannot_tell, raising=False)
        for _ in range(40):
            h.clock.advance(seconds=10)
            await wd.tick()
    assert KEY not in h.broker.pos
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 13, 19, tzinfo=UTC))
    await ticks(h, 80, seconds=15)
    assert db.list_trades() == []
    assert db.get_position_meta(KEY) is None


def _cannot_tell(*args: object, **kwargs: object) -> None:
    """entry_held_unjournaled of a watchdog whose order journal cannot say what the entry
    filled (as every watchdog before round 49): it credits the entry's whole unseen part."""
    return None


# --------------------------------------------------------- the watchdog's sales on two days
# Fuzz seeds 146, 400. The watchdog sold the first part of the bot's BTC entry on
# Wednesday and the rest, which filled on Thursday, on Thursday: the app waited for
# Thursday's executions to report the flat sale, so Wednesday's part was never journaled.


async def test_the_watchdogs_sales_on_two_days_are_all_journaled(db, config, monkeypatch) -> None:
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 0, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    first = round(total * 0.7, 6)
    _part(h.broker, entry, first, 60005.0)
    await ticks(h, 2, seconds=15)  # journaled; then the app goes down
    wd = _watchdog(h)
    for _ in range(40):  # Wednesday: the watchdog sells the 70% held
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    # Thursday 06:00 UTC, app still down: the rest fills; the watchdog sells it too.
    h.clock.t = datetime(2026, 10, 29, 6, 0, tzinfo=UTC)
    _part(h.broker, entry, round(total - first, 8), 60005.0)
    assert h.broker.orders[entry].status == OrderStatus.FILLED
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 7, 0, tzinfo=UTC))
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(total)
    assert db.get_position_meta(KEY) is None


# -------------------------------------------- his sale after the watchdog's unreported sale
# Fuzz seeds 309, 626. Thursday the owner sold more than the journal shows of his: the
# replay took the rest of his sale from the unseen part of the bot's entry, though the
# watchdog had sold all of it the day before, and closed the bot's trade at his price.


async def test_his_sale_after_the_watchdogs_unreported_sale_never_closes_the_bots_trade(
    db, config, monkeypatch
) -> None:
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 14, 53, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    _buy_btc(h)
    await h.engine.run_cycle()  # filled at once; the app goes down before it ticks
    h.decider.decisions = []
    bot = h.broker.pos[KEY][0]
    h.clock.advance(seconds=4)
    owner_buys(h, _BTC, 0.0096, 59080.0)
    h.clock.advance(seconds=2)
    owner_sells(h, _BTC, 0.0048, 59080.0)
    h.clock.advance(seconds=2)
    owner_sells(h, _BTC, 0.00144, 59080.0)
    wd = _watchdog(h)
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(40):  # its stop: the watchdog sells the bot's coins
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.00336)  # his are left
    # Thursday: he buys 0.0004 and sells 0.001128 just as the app comes back.
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 6, 53, 10, tzinfo=UTC))
    h.broker.set_price("BTC", 59080.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    owner_buys(h, _BTC, 0.0004, 59080.0)
    h.clock.advance(seconds=2)
    owner_sells(h, _BTC, 0.001128, 59080.0)
    await ticks(h, 80, seconds=15)
    trades = db.list_trades()
    assert [t.qty for t in trades] == [pytest.approx(bot)]
    assert trades[0].exit_ts < datetime(2026, 10, 29, tzinfo=UTC)  # the watchdog's sale
    assert db.get_position_meta(KEY) is None
    assert h.broker.pos[KEY][0] == pytest.approx(0.002632)


# ---------------------------------------- a lapsed entry never lends its part to his shares
# Fuzz seed 692. The bot's F entry was cancelled unfilled (the app saw it) and the
# owner's overnight buy is unreported the next day: the replay and the exits credited his
# shares to the unseen part of an entry that bought nothing.


async def test_a_lapsed_entry_never_makes_his_unreported_shares_the_bots(db, config) -> None:
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, _ = await _resting_entry(h)
    owner_buys(h, F, 20, 10.0)
    await ticks(h, 2, seconds=15)
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # lapsed, nothing filled
    await ticks(h, 3, seconds=15)
    assert db.get_position_meta("STK:F") is not None  # kept: the owner holds F
    h.clock.t = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)
    owner_buys(h, F, 24, 10.0)  # app down
    h.engine = _restart_next_morning(h, config)
    owner_sells(h, F, 24, 10.2)
    await ticks(h, 3, seconds=15)
    h.broker.set_price("F", 9.4)  # below the lapsed entry's stop
    await ticks(h, 20, seconds=15)
    assert db.list_trades() == []
    assert h.broker.pos["STK:F"][0] == 20
    assert not [r for r in h.broker.placed if r.side.value == "SELL"]


# ------------------------------------------ the bot's trades never sell more than its entry
# Fuzz seeds 536, 1129. His sales before the bot's own took the unseen part of the entry
# first, and the bot's later sale was counted in full anyway: its trades sold 28 of a
# 24-share entry, or 14 of the 8 shares that filled.


async def test_his_sales_before_the_bots_own_never_add_to_its_trades(db, config) -> None:
    h = await _paper(db, config, datetime(2026, 10, 28, 17, 29, tzinfo=UTC))
    await enter_f(h)  # 24 F, filled at once; the app goes down before it ticks
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    h.clock.advance(seconds=2)
    owner_buys(h, F, 10, 10.15)
    h.clock.advance(seconds=2)
    owner_buys(h, F, 3, 10.15)
    h.clock.advance(seconds=2)
    _part(h.broker, f"{entry}-SL", 9, 9.8)  # its stop sells 9 (IBKR no longer reports it)
    h.engine = _restart_next_morning(h, config)  # Thursday 14:00 UTC
    owner_sells(h, F, 3, 10.15)
    h.clock.advance(seconds=30)
    owner_sells(h, F, 10, 10.15)
    h.clock.advance(seconds=30)
    _part(h.broker, f"{entry}-SL", 15, 9.8)  # the rest of the bot's
    h.clock.advance(seconds=30)
    await ticks(h, 10, seconds=15)
    assert "STK:F" not in h.broker.pos
    assert sum(t.qty for t in db.list_trades()) == 24


async def test_his_sales_never_take_more_of_a_lapsed_entry_than_filled(db, config) -> None:
    h = await _paper(db, config, datetime(2026, 10, 28, 14, 51, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    await ticks(h, 1, seconds=15)
    h.clock.t = datetime(2026, 10, 28, 15, 50, tzinfo=UTC)  # the app is down from here
    owner_buys(h, F, 6, 10.0)
    h.clock.t = datetime(2026, 10, 28, 17, 11, tzinfo=UTC)
    _part(h.broker, entry, 8, 9.98)
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # the rest lapses at the close
    h.engine = _restart_next_morning(h, config)  # Thursday 14:00 UTC
    owner_buys(h, F, 1, 10.0)
    await ticks(h, 1, seconds=10)  # the entry seen lapsed with 8 filled
    owner_sells(h, F, 7, 10.0)  # his 1 and his 6 the journal never saw
    h.clock.advance(seconds=2)
    _part(h.broker, f"{entry}-TP", 8, 10.5)  # the bot's 8 at its target
    await ticks(h, 10, seconds=15)
    assert "STK:F" not in h.broker.pos
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(8)


# ------------------------------------------------------ his large sale of unreported shares
# Fuzz seed 1532. He sells 130 F, 100 of them bought with the app down: after three
# rounds the look for his units stopped at 72 and the rest of his sale closed the bot's
# trade.


async def test_his_large_sale_of_unreported_shares_leaves_the_bots_trade_open(db, config) -> None:
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await enter_f(h)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 54
    h.clock.t = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)
    owner_buys(h, F, 100, 10.0)  # app down
    h.clock.t = datetime(2026, 10, 29, 13, 50, tzinfo=UTC)
    owner_sells(h, F, 130, 10.1)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 4, seconds=15)
    assert db.list_trades() == []
    assert h.broker.pos["STK:F"][0] == 24
    assert db.get_position_meta("STK:F") is not None
    assert h.broker.working("STK:F", "stop")


# ---------------------------------------- the watchdog next to an entry whose fill is known
# Fuzz seeds 88, 915, 954, 3, 32, 719, 676, 1125, 3921. An entry still working that filled
# nothing (or only what the journal shows), or one the app saw lapse, holds nothing
# unseen: the owner's coins bought while the app is down are never taken for that part of
# the entry.


async def _resting_btc_entry(db, config):
    """Thursday 01:00 New York: the bot's BTC entry rests at the broker, unfilled."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 29, 5, 0, tzinfo=UTC))
    _buy_btc(h)
    h.broker._match = lambda key: None  # the limit never fills on its own
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    return h, entry, h.broker.orders[entry].request.qty


async def _app_down_watchdog_polls(h, wd, polls: int = 30) -> None:
    """The app is down: only the watchdog polls (every 10 s), past its flatten timeout."""
    for _ in range(polls):
        h.clock.advance(seconds=10)
        await wd.tick()


@pytest.mark.parametrize("part", [0.0, 0.3])
async def test_the_watchdog_never_sells_his_coins_for_a_working_entrys_unfilled_part(
    db, config, part: float
) -> None:
    """The bot's entry works at the broker, filled only `part` of it (journaled). The app
    goes down and the owner buys 0.0012 BTC in IBKR Mobile, less than the entry's rest.
    The watchdog's flatten sells the bot's part only: the broker says the entry filled no
    more, so his coins are not the entry's rest."""
    h, entry, total = await _resting_btc_entry(db, config)
    bots = round(total * part, 6)
    if bots:
        _part(h.broker, entry, bots, 60005.0)
    await ticks(h, 2, seconds=15)
    assert db.get_position_meta(KEY) is not None
    wd = _watchdog(h)
    h.clock.advance(seconds=20)
    owner_buys(h, _BTC, 0.0012, 60000.0)
    await _app_down_watchdog_polls(h, wd)
    assert h.broker.orders[entry].status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
    assert h.broker.pos[KEY][0] == pytest.approx(0.0012, abs=1e-9)


async def test_the_watchdog_never_credits_a_lapsed_entry_with_his_coins(db, config) -> None:
    """The bot's entry rests unfilled; the owner buys 0.01 BTC next to it (journaled), and
    the app cancels the entry after 10 minutes (it saw it done, nothing filled). The
    metadata stays while he holds BTC. App down, he buys 0.0004 more: the watchdog's
    flatten sells none of his 0.0104 (the lapsed entry holds nothing unseen)."""
    h, entry, _ = await _resting_btc_entry(db, config)
    await ticks(h, 2, seconds=15)
    owner_buys(h, _BTC, 0.01, 60000.0)
    await ticks(h, 12, minutes=1)
    assert h.broker.orders[entry].status == OrderStatus.CANCELLED
    assert db.get_position_meta(KEY) is not None
    wd = _watchdog(h)
    h.clock.t += timedelta(seconds=20)
    owner_buys(h, _BTC, 0.0004, 60000.0)
    await _app_down_watchdog_polls(h, wd)
    assert h.broker.pos[KEY][0] == pytest.approx(0.0104, abs=1e-9)


# ------------------------------------------------- the engine next to an entry it saw lapse
# Fuzz seeds 3921, 371, 447, 956, 1431, 1792, 2261, 307. An entry done with nothing filled
# beyond the journal's fills holds nothing unseen, so the engine's exits never take the
# owner's units no fill shows for it.


async def test_the_engines_stop_never_sells_his_coins_for_an_entry_it_saw_lapse(db, config) -> None:
    """Wednesday: the bot's BTC entry rests unfilled; the owner buys 0.0012 BTC in IBKR
    Mobile and the app goes down before it journals that. Thursday it comes back with BTC
    below the entry's stop and IBKR no longer reporting his buy: it cancels the stale
    entry (nothing filled), and its stop then sells none of his coins."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _buy_btc(h)
    h.broker._match = lambda key: None  # the limit never fills
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    await ticks(h, 2, seconds=15)
    h.clock.t += timedelta(seconds=5)
    owner_buys(h, _BTC, 0.0012, 60000.0)
    h.broker.set_price("BTC", 58800.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 4, seconds=15)
    assert h.broker.orders[entry].status == OrderStatus.CANCELLED
    assert h.broker.pos[KEY][0] == pytest.approx(0.0012, abs=1e-9)


# -------------------------------------------------------- an app exit the journal never saw
# Fuzz seeds 965, 99, 149, 242, 707, 973, 554, 558, 731, 1777. An exit of the app's that
# its own order records show filled, but no fill read shows (the app went down before the
# journal caught up, or the fill fell on a day IBKR no longer reports), next to units the
# owner bought where no fill shows them either: the bot's units are sold, so what is held
# is his.


BTC_KEY = "CRYPTO:BTC"


# ---------------------------------------------------------------------- the crypto watchdog
@pytest.mark.parametrize("owner", [0.5, 0.004])
async def test_the_watchdog_never_sells_his_unjournaled_coins_after_an_app_close(
    db: Database, owner: float
) -> None:
    """The app's close of the bot's 0.01 BTC filled as it went down (its order row says so),
    before the journal saw it; the owner had bought coins the journal never saw (more or
    less than the bot's entry). The watchdog's flatten sells nothing: they are his."""
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=owner, journal_owner=False)
    btc = broker.positions_list[0].instrument
    req = OrderRequest(instrument=btc, side=Side.SELL, qty=BOT, order_type=OrderType.MARKET,
                       is_exit=True, reason="AI close", trace_id="ai-trader-77")  # fmt: skip
    at = NOW - timedelta(minutes=5)
    db.insert_order(OrderRecord(request=req, status=OrderStatus.FILLED, broker_order_id="77",
                                filled_qty=BOT, created_at=at, updated_at=at))  # fmt: skip
    broker.positions_list[0] = broker.positions_list[0].model_copy(update={"qty": owner})
    beat(db, clock, age=400)  # a long outage: flatten
    watchdog, _ = make(db, broker, clock)
    for _ in range(3):
        await watchdog.tick()
        clock.advance(seconds=10)
    assert broker.placed == []


# ------------------------------------------------------------------------------- the engine
@pytest.mark.parametrize("his", [0.01, 0.003])
async def test_an_app_close_ibkr_no_longer_reports_leaves_his_coins_to_him(
    db, config, his: float
) -> None:
    """Wednesday the AI closes the bot's BTC and the app goes down before the journal saw
    the sale; that evening the owner buys more or less than the bot had. Thursday IBKR
    reports neither: what is held is his (recorded as such), and the bot's stop never
    sells it."""
    h = await _hold_btc(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    h.decider.decisions = [
        AIDecision(action=TradeAction.CLOSE, symbol="BTC", asset_class=AssetClass.CRYPTO,
                   conviction=0.8, setup="vwap_reclaim", rationale="fading")
    ]  # fmt: skip
    await h.engine.run_cycle()
    h.decider.decisions = []
    assert BTC_KEY not in h.broker.pos, "the AI close sold the bot's coins"
    assert not any(f.side == Side.SELL for f in db.list_fills(limit=100)), "not journaled"
    h.clock.t = datetime(2026, 10, 28, 23, 0, tzinfo=UTC)  # the app is down
    owner_buys(h, _BTC, his, 60000.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3, seconds=15)
    assert sum(q for _, q in read_hidden(_extra(db))) == pytest.approx(his)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 6, seconds=15)
    assert h.broker.pos[BTC_KEY][0] == pytest.approx(his)


async def test_a_bracket_stop_part_ibkr_no_longer_reports_leaves_his_shares_to_him(
    db, config
) -> None:
    """Wednesday the bot's 24 F; with the app down its stop sells 16 of them and the owner
    buys 50. Thursday IBKR reports neither, but the stop still working says it filled 16: a
    Close sells the bot's 8, never his shares."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry = (await enter_f(h)).broker_order_id
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)  # the app is down
    _part(h.broker, f"{entry}-SL", 16, 9.8)
    h.clock.t = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)
    owner_buys(h, F, 50, 9.9)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3)
    assert sum(q for _, q in read_hidden(_extra(db, "STK:F"))) == pytest.approx(50)
    await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 50


async def test_an_entry_and_its_bracket_parts_ibkr_no_longer_reports_keep_the_bots_rest(
    db, config
) -> None:
    """Wednesday, the app down: the bot's resting entry of 24 F fills, its target sells 9 and
    its stop 8 (each shrinking the other), and the owner buys 24. Thursday morning he sells
    10 of them; IBKR reports only that. The app's records say the entry filled 24 and its
    bracket sold 17: the bot holds 7. His 24 are recorded as his on the first tick, before
    the journal replays his sale (it closes no bot trade: the journal holds the bracket's
    sales, at an estimate), and a Close sells the bot's 7, never his 14."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)  # the app is down
    b = h.broker
    _part(b, entry, 24, 10.01)
    for child, other, qty, price in [("TP", "SL", 9, 10.5), ("SL", "TP", 8, 9.8)]:
        h.clock.advance(minutes=10)
        _part(b, f"{entry}-{child}", qty, price)
        sib = b.orders[f"{entry}-{other}"]  # one cancels (reduces) the other
        sib.request = sib.request.model_copy(update={"qty": sib.request.qty - qty})
    h.clock.t = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)
    owner_buys(h, F, 24, 9.9)
    h.clock.t = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)  # Thursday 09:00 New York
    owner_sells(h, F, 10, 9.9)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3)
    assert sum(q for _, q in read_hidden(_extra(db, "STK:F"))) == pytest.approx(24)
    trades = db.list_trades(limit=10)
    assert sorted(t.qty for t in trades) == [8, 9], "his sale closed no bot trade"
    assert {t.exit_price for t in trades} == {9.8, 10.5}
    await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 14


async def test_a_target_part_netted_by_his_buy_with_the_executions_unread_sells_none_of_his(
    db, config
) -> None:
    """The bot's 24 F; its target sells 3 and the owner buys 3 while IBKR's executions
    cannot be read: the position still reads 24, as the journal says. The target still
    working says it filled 3, so a Close does not sell 24 (3 of them his): it waits for the
    executions, then sells the bot's 21."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry = (await enter_f(h)).broker_order_id
    await ticks(h, 2)
    h.broker.fail.add("fills_since")
    _part(h.broker, f"{entry}-TP", 3, 10.5)
    stop = h.broker.orders[f"{entry}-SL"]  # the target's fill reduces the stop
    stop.request = stop.request.model_copy(update={"qty": stop.request.qty - 3})
    owner_buys(h, F, 3, 10.3)
    assert h.broker.pos["STK:F"][0] == 24
    await ticks(h, 1)
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 24
    h.broker.fail.discard("fills_since")
    await ticks(h, 1)
    await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 3


def _extra(db: Database, key: str = BTC_KEY) -> dict:
    return json.loads(db.get_kv(EXTRA_PREFIX + key) or "{}")


# ------------------------------------------ the unseen part bounded by what may have filled
# Fuzz seeds 956, 1792, 1431, 1978, 3853, 1341, 3729, 3111, 3139. An entry the app saw end
# unfilled, or one still working whose fill the broker reports, never takes the owner's
# units no fill shows.


async def _lapsed_entry_next_to_his(h) -> None:
    """Wednesday: the bot's entry of 24 F rests; the owner buys 5 and the entry lapses
    unfilled with the app watching (its metadata stays: F is held). At 15:45 New York he
    buys 3 more and the app stops before it reads that buy."""
    entry, _ = await _resting_entry(h)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 5, 10.0)
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    await ticks(h, 3, seconds=15)
    assert h.db.get_position_meta("STK:F") is not None
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 3, 10.0)


async def test_a_lapsed_entry_never_protects_his_unreported_buy(db, config) -> None:
    """Thursday the app comes back: the entry filled nothing, so his 3 shares no fill shows
    are not the entry's unseen part. No stop is re-placed over them, and the price falling
    below the bot's stop sells none of his 8."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await _lapsed_entry_next_to_his(h)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3, seconds=15)
    assert not [r for r in h.broker.placed if r.side == Side.SELL]
    h.broker.set_price("F", 9.5)
    await ticks(h, 10, seconds=15)
    assert h.broker.pos["STK:F"][0] == 8


async def test_a_lapsed_entry_never_sells_his_unreported_buy_after_his_sale(db, config) -> None:
    """Thursday he sells 2 as the app comes back (a fill of the last two minutes: his
    units no fill shows are not recorded yet). A Close sells nothing: none of it is the
    bot's."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await _lapsed_entry_next_to_his(h)
    h.engine = _restart_next_morning(h, config)
    owner_sells(h, F, 2, 10.0)
    await ticks(h, 1, seconds=15)
    with pytest.raises(ValueError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 6


@pytest.mark.parametrize("part", [0, 5])
async def test_a_working_entry_never_takes_his_buy_while_the_executions_are_unread(
    db, config, part: float
) -> None:
    """The bot's entry of 24 F works, `part` of it filled (the broker says so); the
    executions read then fails and the owner buys 5. Held is more than the entry filled:
    a Close is undecided (retry), never a sale of his shares."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, _ = await _resting_entry(h)
    if part:
        _part(h.broker, entry, part, 10.01)
    await ticks(h, 2, seconds=15)

    async def unread(since):
        raise BrokerError("IBKR executions timed out")

    h.broker.fills_since = unread
    owner_buys(h, F, 5, 10.0)
    with pytest.raises(BrokerError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == part + 5


async def test_the_watchdog_never_credits_a_lapsed_entry_with_his_unreported_coins(
    db, config
) -> None:
    """The bot's BTC entry lapses unfilled with the app watching, next to the owner's
    0.004; he buys half an entry more and the app stops before it reads that buy. Below
    the bot's stop, the watchdog sells none of his coins."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 29, 5, 0, tzinfo=UTC))
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)
    owner_buys(h, _BTC, 0.004, 60000.0)
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    await ticks(h, 3, seconds=15)
    assert db.get_position_meta(KEY) is not None
    his = round(total * 0.5, 6)
    owner_buys(h, _BTC, his, 60000.0)
    wd = _watchdog(h)
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.004 + his, abs=1e-9)


# ------------------------------------------ the watchdog's earlier sale after today's trips
# Fuzz seed 400 (found while merging the round 49 fixes). The watchdog sold the part of the
# bot's BTC entry that filled on Wednesday; the rest filled on Thursday and its trip was
# journaled from Thursday's executions. The estimate of Wednesday's sale was dated at that
# sale, before the trip already journaled, and the replay never journals a trip before one
# it journaled: it was never journaled, though the metadata was released.


@pytest.mark.parametrize("rest_sold_by", ["watchdog", "stop"])
async def test_an_earlier_day_watchdog_sale_is_journaled_after_todays_trip(
    db, config, monkeypatch, rest_sold_by: str
) -> None:
    """Wednesday: the bot's resting BTC entry fills 70% with the app down, and the watchdog
    sells those coins. Thursday the app is back when the rest fills; the watchdog (in a
    short outage) or the bot's stop sells it, and that trip is journaled. Wednesday's sale
    is journaled too: the bot's trades add up to its entry."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 0, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    first = round(total * 0.7, 6)
    h.clock.advance(seconds=2)
    _part(h.broker, entry, first, 60005.0)  # the app is down from here
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    _part(h.broker, entry, round(total - first, 8), 60005.0)
    await ticks(h, 2, seconds=15)  # the rest is journaled
    if rest_sold_by == "watchdog":
        for _ in range(40):  # the app stops checking: the watchdog sells the rest
            h.clock.advance(seconds=10)
            await wd.tick()
    else:
        h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
        await ticks(h, 3, seconds=15)
    assert KEY not in h.broker.pos
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(total)
    assert db.get_position_meta(KEY) is None


# ----------------------------------------------- the watchdog's sale of a rest since settled
# Fuzz seed 3146 (found while merging the round 49 fixes). The rest of the bot's BTC entry
# filled with the executions unread and the watchdog sold all the bot's coins, next to the
# owner's; the next day the position read is his coins alone, so the rest is settled
# (`rest_settled`: it filled and was sold unseen). The replay still lets the estimate of
# the watchdog's sale sell that rest: settled says nothing is held now, not that it never
# filled.


async def test_the_watchdogs_sale_of_a_settled_rest_is_journaled_in_full(
    db, config, monkeypatch
) -> None:
    """Wednesday: the bot's resting BTC entry fills 30% (journaled) and the owner buys
    0.002; the rest fills while IBKR's executions cannot be read, then the app goes down and
    the watchdog sells the bot's coins. Thursday the bot's trade is journaled for its whole
    entry, and the owner's coins stay."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 20, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    first = round(total * 0.3, 6)
    _part(h.broker, entry, first, 60005.0)
    owner_buys(h, _BTC, 0.002, 60000.0)
    await ticks(h, 2, seconds=15)  # both journaled
    h.broker.fail.add("fills_since")
    _part(h.broker, entry, round(total - first, 8), 60005.0)
    await ticks(h, 1, seconds=15)
    wd = _watchdog(h)
    for _ in range(40):  # the app is down from here: the watchdog sells the bot's coins
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.002)
    h.broker.fail.discard("fills_since")
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 9, 20, tzinfo=UTC))
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(total)
    assert db.get_position_meta(KEY) is None
    assert h.broker.pos[KEY][0] == pytest.approx(0.002)


# -------------------------------------------------- a bracket stop that sold all of it unseen
# Fuzz seeds 1298 and 3447 (found while merging the round 49 fixes). What the app saw the
# bot's stock entry fill is the bot's only up to what its working stops cover: with none
# working and no exit of the app's sent since the entry, the bracket's children are done (a
# day IBKR no longer reports shows neither their sale nor the owner's buy since).


async def test_a_bracket_stop_that_sold_all_unseen_leaves_his_shares_to_him(db, config) -> None:
    """Wednesday: the bot's 24 F fill and the app goes down before journaling them; the
    bracket stop sells all 24 and the owner buys 30. Thursday IBKR reports none of it: the
    app's record that the entry filled says nothing of what the bot still holds. No stop is
    put back over his shares, and neither F's drop nor a Close sells any of his 30."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry = (await enter_f(h)).broker_order_id  # filled at once; the app is down from here
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
    _part(h.broker, f"{entry}-SL", 24, 9.8)
    h.broker.orders[f"{entry}-TP"].status = OrderStatus.CANCELLED  # one cancels the other
    h.clock.t = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)
    owner_buys(h, F, 30, 9.9)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3)
    h.broker.set_price("F", 9.7)
    await ticks(h, 3)
    assert h.broker.pos["STK:F"][0] == 30
    with contextlib.suppress(ValueError):  # "sell it in IBKR": none of it is the bot's
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 30


# ------------------------------------------- a bracket child's sale IBKR no longer reports
# Fuzz seeds 22035 and 3093 (review of the round 49 merge), and 6222, 23256, 731 (found
# while fixing them). What a bracket child of the bot's sold on a day IBKR no longer reports
# was known only while the child worked (it has no order row): once the bot's own close
# cancelled it, or once it was done, the bot was credited again with units it had sold, and
# its stop or exit sold the owner's. Kept, that sale is journaled at an estimate, before
# the replay of the owner's sales: the bot's trade closes with what it sold, never with his.


async def test_a_target_sale_ibkr_no_longer_reports_stays_sold_after_the_bots_close(
    db, config
) -> None:
    """Wednesday: the bot's resting entry of 24 F fills with the app down; its target sells
    9 (the stop shrinks to 15) and the owner buys 25. Thursday the target sells 3 more and
    the owner's Close on the app's first tick sells the bot's 12. The bot holds nothing:
    no stop is re-placed over his 25, and F's drop sells none of them."""
    h = await _paper(db, config, NOON)
    entry, total = await _resting_entry(h)
    assert total == 24
    await ticks(h, 2, seconds=15)  # the app saw it working
    h.clock.t = datetime(2026, 10, 28, 15, 30, tzinfo=UTC)  # the app is down from here
    _part(h.broker, entry, 24, 9.98)
    [target] = h.broker.working("STK:F", "target")
    [stop] = h.broker.working("STK:F", "stop")
    h.clock.t = datetime(2026, 10, 28, 15, 40, tzinfo=UTC)
    _part(h.broker, target, 9, 10.5)
    srec = h.broker.orders[stop]
    srec.request = srec.request.model_copy(update={"qty": 15.0})  # one cancels the other
    h.clock.t = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
    owner_buys(h, F, 25, 10.0)
    h.engine = _restart_next_morning(h, config)
    h.clock.t = datetime(2026, 10, 29, 14, 0, 5, tzinfo=UTC)
    _part(h.broker, target, 3, 10.5)
    srec.request = srec.request.model_copy(update={"qty": 12.0})
    await ticks(h, 1, seconds=10)
    await h.engine.close_position("STK:F")
    assert bot_sells(h, "STK:F") == [12]
    assert h.broker.pos["STK:F"][0] == 25
    await ticks(h, 12, seconds=30)
    assert _stops(h) == []
    h.broker.set_price("F", 9.7)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 25


async def test_an_earlier_day_target_sale_stays_sold_once_the_target_is_done(db, config) -> None:
    """Wednesday: the bot buys 24 F; with the app down its target sells 9 (the stop shrinks
    to 15) and the owner buys 24, neither reported again. Thursday the owner sells his 24
    and the target sells the bot's other 15 (done: the stop goes). He buys 3, and 50
    overnight: Friday no stop of the bot's covers his shares, and neither F's rise to the
    target nor its drop sells any of his 53. The bot's trade of 24 is journaled (the
    target's 9 of Wednesday at what it filled at, an estimate)."""
    h = await _paper(db, config, datetime(2026, 10, 28, 14, 0, tzinfo=UTC))
    await enter_f(h)
    await ticks(h, 5, seconds=15)
    [target] = h.broker.working("STK:F", "target")
    [stop] = h.broker.working("STK:F", "stop")
    h.clock.advance(minutes=1)  # the app is down from here
    _part(h.broker, target, 9, 10.5)
    srec = h.broker.orders[stop]
    srec.request = srec.request.model_copy(update={"qty": 15.0})
    owner_buys(h, F, 24, 10.2)
    h.engine = _restart_next_morning(h, config)  # Thursday 10:00 New York
    await ticks(h, 4, seconds=15)
    owner_sells(h, F, 24, 10.4)
    _part(h.broker, target, 15, 10.5)
    srec.status = OrderStatus.CANCELLED  # one cancels the other
    await ticks(h, 3, seconds=60)
    owner_buys(h, F, 3, 10.4)
    await ticks(h, 3, seconds=60)
    h.clock.t = datetime(2026, 10, 29, 23, 0, tzinfo=UTC)
    owner_buys(h, F, 50, 10.4)  # the app is down
    h.engine = _restart_at(h, config, datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    await ticks(h, 10, seconds=30)
    assert _stops(h) == []
    h.broker.set_price("F", 10.6)
    await ticks(h, 3, seconds=15)
    h.broker.set_price("F", 9.7)
    await ticks(h, 5, seconds=15)
    assert bot_sells(h, "STK:F") == []
    assert h.broker.pos["STK:F"][0] == 53
    assert [(t.qty, t.exit_price) for t in db.list_trades()] == [(24, 10.5)]


def _shrink(h, oid: str, qty: float) -> None:
    """One cancels the other: a bracket child's sale shrinks its sibling."""
    rec = h.broker.orders[oid]
    rec.request = rec.request.model_copy(update={"qty": rec.request.qty - qty})


async def test_an_earlier_day_bracket_sale_is_journaled_before_his_sale_is_replayed(
    db, config
) -> None:
    """Wednesday, the app down after the bot's 24 F: its stop sells 14, the owner buys 29
    and sells 18. Thursday (reported) he sells 5, the target sells 4 and the stop 2, and he
    buys 50 just before the app is back. The stop's 14 are journaled at an estimate before
    the journal replays his sale: it closes no bot trade (the bot still holds 4), and the
    bot's stop later sells its 4, never his 56."""
    h = await _paper(db, config, datetime(2026, 10, 28, 14, 0, tzinfo=UTC))
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    [target] = h.broker.working("STK:F", "target")
    [stop] = h.broker.working("STK:F", "stop")
    h.clock.advance(minutes=5)  # the app is down from here
    _part(h.broker, stop, 14, 9.8)
    _shrink(h, target, 14)
    owner_buys(h, F, 29, 10.0)
    owner_sells(h, F, 18, 10.0)
    h.clock.t = datetime(2026, 10, 29, 9, 52, tzinfo=UTC)
    owner_sells(h, F, 5, 10.0)
    h.clock.t = datetime(2026, 10, 29, 13, 39, tzinfo=UTC)
    _part(h.broker, target, 4, 10.5)
    _shrink(h, stop, 4)
    h.clock.t = datetime(2026, 10, 29, 13, 49, tzinfo=UTC)
    _part(h.broker, stop, 2, 9.8)
    _shrink(h, target, 2)
    h.clock.t = datetime(2026, 10, 29, 14, 15, tzinfo=UTC)
    owner_buys(h, F, 50, 10.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 15, 30, tzinfo=UTC))
    await ticks(h, 12, seconds=15)
    assert db.list_trades() == []
    assert db.get_position_meta("STK:F") is not None
    h.broker.set_price("F", 9.7)
    await ticks(h, 4, seconds=15)
    assert h.broker.pos["STK:F"][0] == 56
    assert sum(t.qty for t in db.list_trades()) == 24


# ------------------------------------------------- a bracket child's fill since the tick
# Fuzz seed 16764 (review of the round 49 merge), and 15 stock seeds that failed before it
# too. The bot's exit credited its entry's fill from a fresh open-orders read, but took the
# app's own sells (a bracket child has no order row) from the tick's older read: a target
# fill since that tick counted as still held, and a Close sold the owner's shares.


async def test_a_target_fill_since_the_tick_is_not_credited_as_held(db, config) -> None:
    """The bot's entry of 24 F rests; the owner holds 30 (journaled). IBKR's executions time
    out from here: half the entry fills and a tick sees it. With the portfolio read lagging
    (still 42), the target sells 6 of the bot's and the owner buys 50: a Close sells no
    more than the bot's 6."""
    h = await _paper(db, config, NOON)
    entry, total = await _resting_entry(h)
    assert total == 24
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 2, seconds=15)
    h.broker.fail.add("fills_since")
    _part(h.broker, entry, 12, 9.98)
    await ticks(h, 1, seconds=15)
    stale = await h.broker.positions()
    [target] = h.broker.working("STK:F", "target")
    _part(h.broker, target, 6, 10.5)
    owner_buys(h, F, 50, 10.0)
    real = h.broker.positions
    reads = 2

    async def lagging():
        nonlocal reads
        if reads:
            reads -= 1
            return stale
        return await real()

    h.broker.positions = lagging
    with contextlib.suppress(BrokerError, ValueError, LookupError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] >= 80


async def test_a_target_fill_between_ticks_keeps_his_shares(db, config) -> None:
    """The bot's 24 F fill with IBKR's executions timing out; before the next tick its
    target sells 4 (the stop shrinks to 20) and the owner buys 3 in TWS. The fresh
    open-orders read of a Close shows the target's 4 sold: it never sells his 3."""
    h = await _paper(db, config, NOON)
    await ticks(h, 1, seconds=15)
    h.broker.fail.add("fills_since")
    await enter_f(h)
    [target] = h.broker.working("STK:F", "target")
    [stop] = h.broker.working("STK:F", "stop")
    _part(h.broker, target, 4, 10.5)
    srec = h.broker.orders[stop]
    srec.request = srec.request.model_copy(update={"qty": 20.0})
    owner_buys(h, F, 3, 10.0)
    assert h.broker.pos["STK:F"][0] == 23
    with contextlib.suppress(BrokerError, ValueError, LookupError):
        await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] >= 3


# -------------------------------------------- the watchdog's earlier sale before the settle
# Fuzz seeds 5373, 24141, 7126, 11481, 13662, 16936, 17567, 20050 and 3527 (review of the
# round 49 merge). The entry's rest was settled (the position read matched the journal,
# less the watchdog's sales no fill shows) before the watchdog's earlier-day sale was
# journaled: the metadata was released and that sale never journaled. Its estimate was also
# dated before the trip of the rest already journaled (which the replay skips), and an
# estimate dated after the watchdog's last sale was taken off the bot's units a second time.


async def _btc_entry_resting(h) -> tuple[str, float]:
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    return entry, h.broker.orders[entry].request.qty


@pytest.mark.parametrize("estimate_read_fails", [False, True])
async def test_the_watchdogs_earlier_sale_is_journaled_when_he_buys_after_the_bots_trip(
    db, config, monkeypatch, estimate_read_fails: bool
) -> None:
    """Wednesday: with the app down 30% of the bot's resting BTC entry fills and the
    watchdog sells it. Thursday the rest fills and the bot's stop sells it; the owner buys
    0.01 right after. The bot's trades add up to its entry: Wednesday's sale too, also when
    the estimate's fresh position read fails at first (the rest is not settled meanwhile)."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _btc_entry_resting(h)
    await ticks(h, 2, seconds=15)  # the app saw it working
    h.clock.t = datetime(2026, 10, 28, 18, 20, tzinfo=UTC)  # the app is down from here
    first = round(total * 0.3, 6)
    _part(h.broker, entry, first, 60000.0)
    wd = _watchdog(h)
    for _ in range(3):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    assert db.get_kv(WATCHDOG_SALES_PREFIX + KEY)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    rest = round(total - first, 8)
    _part(h.broker, entry, rest, 59900.0)
    for _ in range(3):
        await ticks(h, 1, seconds=15)
        await wd.tick()
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 2, seconds=15)
    assert KEY not in h.broker.pos
    assert bot_sells(h, KEY)[-1] == pytest.approx(rest)
    if estimate_read_fails:
        journal = Engine._journal_watchdog_sale
        failing = [3]

        async def first_reads_fail(self, *args):
            if failing[0] <= 0:
                return await journal(self, *args)
            failing[0] -= 1
            h.broker.fail.add("positions")
            try:
                return await journal(self, *args)
            finally:
                h.broker.fail.discard("positions")

        monkeypatch.setattr(Engine, "_journal_watchdog_sale", first_reads_fail)
    owner_buys(h, _BTC, 0.01, 58500.0)
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(total)
    assert h.broker.pos[KEY][0] == pytest.approx(0.01)


@pytest.mark.parametrize("second_sold_by", ["watchdog", "stop"])
async def test_a_capped_earlier_day_watchdog_sale_is_journaled_after_a_trip_of_today(
    db, config, monkeypatch, second_sold_by: str
) -> None:
    """Wednesday: the bot's BTC entry rests and the owner buys 0.002 (journaled). With the
    app down half the entry fills and the watchdog sells those coins (capped: his stay).
    Thursday 30% more fills and the watchdog (the app still down) or the bot's stop sells
    it (a trip of today, journaled); then the rest lapses. The bot's trades add up to what
    its entry bought, and his coins stay."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 0, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    entry, total = await _btc_entry_resting(h)
    if second_sold_by == "stop":
        rec = h.broker.orders[entry]  # its limit is never reached: it fills only below
        rec.request = rec.request.model_copy(update={"limit_price": 1.0})
    owner_buys(h, _BTC, 0.002, 60000.0)
    await ticks(h, 2, seconds=15)  # his buy is journaled
    h.clock.advance(seconds=2)
    _part(h.broker, entry, round(total * 0.5, 6), 60005.0)  # the app is down from here
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.002), "the watchdog sold the bot's part"
    if second_sold_by == "watchdog":
        h.clock.t = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)  # Thursday, the app still down
        _part(h.broker, entry, round(total * 0.3, 6), 60005.0)
        for _ in range(40):
            h.clock.advance(seconds=10)
            await wd.tick()
        h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
        await ticks(h, 4, seconds=15)
    else:
        h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
        await ticks(h, 4, seconds=15)
        _part(h.broker, entry, round(total * 0.3, 6), 60005.0)
        await ticks(h, 4, seconds=15)
        h.clock.advance(minutes=30)
        h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
        await ticks(h, 4, seconds=15)  # the stop sells the bot's 30%
        h.clock.advance(minutes=30)
    await h.broker.cancel_order(entry)  # the rest lapses
    await ticks(h, 80, seconds=15)
    assert h.broker.pos[KEY][0] == pytest.approx(0.002), "his coins stay"
    bought = h.broker.orders[entry].filled_qty
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(bought)
    assert db.get_position_meta(KEY) is None


# ------------------------------------------- his sale after the watchdog's sales are done
# Fuzz seeds 3527 and 15369 (found while fixing the watchdog's earlier sale), and 840. The
# owner sold coins he bought with the app down, right after the app was back: the replay
# could not look for his units no fill shows yet, and went on because the watchdog had sold
# (a sale of the bot's no fill shows), so his sale closed the bot's trade, and the bot's
# own coins were left unmanaged. Once every sell of the watchdog's is done, the split counts
# what it sold: the replay waits for his units, also when the estimate of the watchdog's
# sale is journaled. The watchdog's own reported sells never make it wait.


@pytest.mark.parametrize("rest", ["lapses", "fills"])
async def test_his_sale_after_the_watchdogs_done_sales_waits_for_his_unreported_coins(
    db, config, monkeypatch, rest: str
) -> None:
    """Wednesday: the bot's BTC entry rests and the owner buys 0.002 (journaled). With the
    app down 30% fills and the watchdog sells it (capped: his coins stay); he buys 0.0096.
    Thursday the app is back: 20% more fills and he sells 0.0058 right away (the rest of the
    entry then lapses, or fills). His sale closes no bot trade: the journal holds the
    watchdog's sale, and the bot's stop sells its own coins, never his."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 0, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    entry, total = await _btc_entry_resting(h)
    owner_buys(h, _BTC, 0.002, 60000.0)
    await ticks(h, 2, seconds=15)
    h.clock.advance(seconds=2)
    first = round(total * 0.3, 6)
    _part(h.broker, entry, first, 60005.0)  # the app is down from here
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.002)
    h.clock.t = datetime(2026, 10, 28, 23, 0, tzinfo=UTC)
    owner_buys(h, _BTC, 0.0096, 60000.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 9, 5, tzinfo=UTC))
    await ticks(h, 1, seconds=15)
    second = round(total * 0.2, 6)
    _part(h.broker, entry, second, 60005.0)
    h.clock.advance(seconds=2)
    owner_sells(h, _BTC, 0.0058, 60000.0)
    if rest == "fills":
        h.clock.advance(seconds=2)
        third = round(total - first - second, 8)
        _part(h.broker, entry, third, 60005.0)
        second += third
    await ticks(h, 12, seconds=15)
    await h.broker.cancel_order(entry)  # what is left of it lapses
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(first)
    assert db.get_position_meta(KEY) is not None
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 4, seconds=15)
    assert h.broker.pos[KEY][0] == pytest.approx(0.002 + 0.0096 - 0.0058)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(first + second)


async def test_the_watchdogs_own_sale_never_holds_the_replay(db, config, monkeypatch) -> None:
    """With the app down 30% of the bot's resting BTC entry fills and the watchdog sells it
    (the entry still works: not marked sold); the rest lapses and the owner buys 0.0096. He
    buys 0.004 more just as the app is back: the bot's trip (its buy, the watchdog's sell)
    is journaled on the first tick and the metadata released, so the watchdog never takes
    coins he buys while the app is down again for the bot's."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 17, 20, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    entry, total = await _btc_entry_resting(h)
    await ticks(h, 2, seconds=15)
    h.clock.advance(seconds=30)
    first = round(total * 0.3, 6)
    _part(h.broker, entry, first, 60005.0)  # the app is down from here
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    await h.broker.cancel_order(entry)
    owner_buys(h, _BTC, 0.0096, 60000.0)
    h.engine = _restart_at(h, config, h.clock() + timedelta(minutes=1))
    owner_buys(h, _BTC, 0.004, 60000.0)
    await ticks(h, 1, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(first)
    assert db.get_position_meta(KEY) is None


# ------------------------------------------------------- reading the app's order rows once
# Review of the round 49 merge: each read of what the bot's entry or the app's sells filled
# beyond the journal scanned the whole orders table, several times per held position per
# monitor tick (100 in 20 quiet ticks against 20 before the merge; 132 in one look for the
# owner's units no fill shows), and the crypto watchdog twice per position per poll even
# with the app alive.


def _count_order_reads(monkeypatch) -> list[int]:
    calls = [0]
    real = Database.list_orders_since

    def counting(self, since):
        calls[0] += 1
        return real(self, since)

    monkeypatch.setattr(Database, "list_orders_since", counting)
    return calls


async def test_a_quiet_pure_bot_tick_reads_the_orders_table_once(db, config, monkeypatch) -> None:
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 0})
    await enter_f(h)  # the bot's 24 F; nobody else trades F
    await ticks(h, 10, seconds=15)
    calls = _count_order_reads(monkeypatch)
    await ticks(h, 20, seconds=15)
    assert calls[0] <= 20


async def test_the_look_for_his_unreported_units_reads_the_orders_table_once(
    db, config, monkeypatch
) -> None:
    """The owner's sale of units he bought with the app down makes the engine look for them
    with many splits of the fills (`_note_hidden`): the app's order rows are read once."""
    h = await _paper(db, config, NOON)
    await enter_f(h)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 3, seconds=15)
    h.clock.t = datetime(2026, 10, 28, 19, 30, tzinfo=UTC)
    owner_buys(h, F, 100, 10.0)  # the app is down
    h.clock.t = datetime(2026, 10, 29, 13, 50, tzinfo=UTC)
    owner_sells(h, F, 130, 10.1)
    h.engine = _restart_next_morning(h, config)
    calls = _count_order_reads(monkeypatch)
    looks: list[int] = []
    note_hidden = Engine._note_hidden

    def counted(self, *args, **kwargs):
        before = calls[0]
        try:
            return note_hidden(self, *args, **kwargs)
        finally:
            looks.append(calls[0] - before)

    monkeypatch.setattr(Engine, "_note_hidden", counted)
    await ticks(h, 4, seconds=15)
    assert looks and max(looks) <= 1, looks
    assert calls[0] <= 2 * 4, calls[0]
    assert read_hidden(read_extra(db, "STK:F")), "his units were found"


async def test_the_watchdog_reads_the_app_order_rows_once_a_poll_and_none_while_it_is_alive(
    db: Database, monkeypatch
) -> None:
    clock = Clock()
    broker = FakeBroker()
    setup(db, broker, owner=0.0, journal_owner=False, bid=100.0)  # the bot's coins only
    beat(db, clock, age=5)
    watchdog, _ = make(db, broker, clock)
    await watchdog.tick()
    calls = _count_order_reads(monkeypatch)
    for _ in range(10):
        clock.advance(seconds=30)
        beat(db, clock, age=5)  # the app is alive: nothing to sell
        await watchdog.tick()
    assert calls[0] == 0
    for _ in range(10):
        clock.advance(seconds=30)
        beat(db, clock, age=60)  # its heartbeat stale (the bid above the stop: no sale)
        await watchdog.tick()
    assert calls[0] <= 10
    assert broker.placed == []


# ------------------------------------------- a close beside its stop's sale, then unread
# Fuzz seed 17569 (review pass 2). The bracket stop sells 12 of the bot's 24 F while its
# Close cancels it, and the Close sells the other 12. The Close's order row says 12 sold
# beyond the journal; nothing says the stop's 12 (a bracket child has no row).


async def _close_beside_its_stops_sale(h, stale_status: bool = False) -> None:
    """The bot's 24 F and the owner's 1 (journaled). Close: the bracket stop sells 12 as
    its cancel goes out, the Close sells the bot's other 12; the owner's 1 is held.
    `stale_status`: IBKR's order status still shows the stop unfilled when the Close
    looks (its fill reached the position first), so the Close never sees that sale."""
    await enter_f(h)  # the bot's 24 F, bracket stop 9.8
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 1, 10.0)
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    real_cancel, real_status = h.broker.cancel_order, h.broker.order_status

    async def cancel(oid: str) -> None:
        if oid == stop and h.broker.orders[stop].filled_qty == 0:
            _part(h.broker, stop, 12, 9.8)  # the stop sells 12 as the close cancels it
        await real_cancel(oid)

    async def status(oid: str):
        rec = await real_status(oid)
        if stale_status and rec is not None and oid == stop:
            return rec.model_copy(update={"filled_qty": 0.0})
        return rec

    h.broker.cancel_order, h.broker.order_status = cancel, status
    await h.engine.close_position("STK:F")
    h.broker.cancel_order, h.broker.order_status = real_cancel, real_status
    assert h.broker.pos["STK:F"][0] == 1, "the bot's 24 are sold, the owner's 1 held"


def _f_sells(h) -> list[float]:
    return [r.qty for r in h.broker.placed if r.side == Side.SELL and r.instrument.key == "STK:F"]


@pytest.mark.parametrize("stale_status", [False, True])
async def test_a_close_beside_its_stops_sale_with_the_executions_unread_sells_none_of_his(
    db, config, stale_status: bool
) -> None:
    """IBKR's executions time out before the next tick journals either sale, and the owner
    buys 12 in TWS. The bot's 24 less the Close's 12 and his 1 match the 13 held (with the
    stop's sale unseen by the Close), but the journal is behind: the bot sizes nothing
    until the executions are read, and when F drops below its stop none of his 13 is
    sold."""
    h = await _paper(db, config, NOON)
    await _close_beside_its_stops_sale(h, stale_status)
    h.broker.fail.add("fills_since")
    owner_buys(h, F, 12, 10.0)
    await ticks(h, 3, seconds=15)
    h.broker.set_price("F", 9.7)  # below the bot's stop
    await ticks(h, 4, seconds=15)
    assert h.broker.pos["STK:F"][0] == 13, f"his 13 F are never sold (bot sells {_f_sells(h)})"


async def test_a_stop_sale_during_the_bots_close_is_never_held_on_a_later_day(db, config) -> None:
    """The app goes down right after the Close, before it journals either sale; the owner
    buys 12 that evening (IBKR reports none of it on Thursday). Thursday the Close's row says
    12 sold: what the Close saw its stop sell during its cancels is kept, so the bot holds
    none of the 13 and its stop is never put back over his shares."""
    h = await _paper(db, config, NOON)
    await _close_beside_its_stops_sale(h)
    h.clock.t = datetime(2026, 10, 28, 22, 0, tzinfo=UTC)
    owner_buys(h, F, 12, 10.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 3, seconds=15)
    h.broker.set_price("F", 9.7)
    await ticks(h, 6, seconds=15)
    assert h.broker.pos["STK:F"][0] == 13, f"his 13 F are never sold (bot sells {_f_sells(h)})"


# ---------------------------------------------- a bracket child's sale just before a close
# Fuzz seed 4002 (review pass 2). A child's fill just before the bot's own Close is never
# kept by the journal's next sync (the Close cancels the child first): the Close keeps what
# it saw the child had sold.


async def test_a_stop_sale_just_before_the_bots_close_is_never_held_on_a_later_day(
    db, config
) -> None:
    """Wednesday the bot's 24 F fill and the app goes down before it journals them.
    Thursday its stop sells 12 (journaled); at 11:00 the stop sells 4 more and a Close a
    moment later sells the last 8, and the app goes down before its next tick. That evening
    the owner buys 50. Friday the entry's row says 24 filled: the bot holds none of the 50
    (the stop's 4 are sold, not held), and F below its stop sells none of his."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    await enter_f(h)
    [stop] = h.broker.working("STK:F", "stop")
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 2, seconds=15)
    _part(h.broker, stop, 12, 9.8)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=30)
    _part(h.broker, stop, 4, 9.8)
    h.clock.advance(seconds=2)
    await h.engine.close_position("STK:F")
    assert "STK:F" not in h.broker.pos, "the bot's 24 are all sold"
    h.clock.t = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
    owner_buys(h, F, 50, 9.9)
    h.engine = _restart_at(h, config, datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    h.broker.set_price("F", 9.7)
    await ticks(h, 6, seconds=15)
    assert h.broker.pos["STK:F"][0] == 50, f"his 50 F are never sold (bot sells {_f_sells(h)})"


# ----------------------------------------- his buy between the tick's reads of the broker
# Fuzz seeds 12422, 4126, 17043 (review pass 2). The tick reads the position first and the
# executions later: a buy of his in between can make the journal match that read while
# part of the bot's entry is still held.


async def test_his_buy_after_the_ticks_position_read_never_settles_the_bots_rest(
    db, config
) -> None:
    """Wednesday, with the app down, 8 of the bot's entry of 24 F fill, its target sells 4
    and its stop 1, and the rest lapses: the bot holds 3. Thursday the owner buys 3 just
    after the first tick's position read (3); that tick's executions show his buy. The
    bot's rest is not taken as settled: its 3 keep their stop, and F below it sells them,
    never his 3."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    await ticks(h, 2)
    b = h.broker
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)  # the app is down from here
    _part(b, entry, 8, 10.01)
    for child, qty, price in [("TP", 4, 10.5), ("SL", 1, 9.8)]:
        h.clock.advance(minutes=10)
        _part(b, f"{entry}-{child}", qty, price)
    h.clock.t = datetime(2026, 10, 28, 20, 0, tzinfo=UTC)
    await b.cancel_order(entry)
    for child, other in [("TP", "SL"), ("SL", "TP")]:  # the bracket covers what filled
        rec = b.orders[f"{entry}-{child}"]
        sold = b.orders[f"{entry}-{other}"].filled_qty
        rec.request = rec.request.model_copy(update={"qty": 8 - sold})
    assert b.pos["STK:F"][0] == 3, "the bot holds 3"
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    real = b.positions
    state = {"bought": False}

    async def positions():
        out = await real()
        if not state["bought"]:
            state["bought"] = True
            owner_buys(h, F, 3, 10.0)  # just after the read
        return out

    b.positions = positions
    await ticks(h, 4, seconds=15)
    b.positions = real
    b.set_price("F", 9.7)
    await ticks(h, 6, seconds=15)
    alerts = [t for t, _, _ in h.notes.sent]
    assert b.pos["STK:F"][0] == 3, f"the bot's 3 F keep their exit (alerts {alerts})"


# ------------------------------------------------ the replay next to an entry still working
# Fuzz seeds 24902, 16561, 18813, 23151, 5134 (review pass 2). The journal's replay took the
# unfilled rest of an entry the broker still lists as working as maybe held: his sale was
# charged to it, and once the entry lapsed the bot's real trip was journaled again.


async def test_his_sale_between_the_watchdogs_sales_never_closes_a_bot_trade(
    db, config, monkeypatch
) -> None:
    """The bot's BTC entry rests and half of it fills; the owner holds 0.0132 (journaled).
    The app goes down: the watchdog sells the bot's half, the owner sells 0.0066, part of
    the entry's rest fills and the watchdog sells that too. The app is back the same day
    with the entry still working; later it lapses and the owner buys 0.001. The journal
    holds the bot's two trips at the watchdog's prices: never one closed by his sale, and
    never one of them twice."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    monkeypatch.setattr("trader.db.utcnow", h.clock)  # audit rows on the fake clock
    await ticks(h, 2, seconds=15)
    _buy_btc(h)
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items()
               if k == "entry" and h.broker.orders[oid].status in OPEN_ORDER_STATUSES]  # fmt: skip
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)
    first = round(total / 2, 6)
    _part(h.broker, entry, first, 59950.0)
    await ticks(h, 2, seconds=15)
    owner_buys(h, _BTC, 0.0132, 60000.0)
    await ticks(h, 2, seconds=15)  # the app goes down after this tick
    h.broker.set_price("BTC", 59990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    wd = _watchdog(h)
    for _ in range(40):  # stale heartbeat: the watchdog flattens the bot's half
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.0132)
    h.clock.advance(minutes=2)
    owner_sells(h, _BTC, 0.0066, 60100.0)
    h.clock.advance(minutes=30)
    second = round((total - first) * 0.3, 6)
    _part(h.broker, entry, second, 59950.0)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.0066)
    assert h.broker.orders[entry].status in OPEN_ORDER_STATUSES  # the rest still works
    h.engine = _restart_at(h, config, h.clock() + timedelta(minutes=30))
    await ticks(h, 20, seconds=15)
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # the rest lapses
    await ticks(h, 4, seconds=15)
    owner_buys(h, _BTC, 0.001, 60000.0)
    await ticks(h, 20, seconds=15)
    trades = db.list_trades()
    assert sum(t.qty for t in trades) == pytest.approx(first + second), "no trip twice"
    assert all(t.exit_price < 60100.0 for t in trades), "never at his sale's price"
    assert h.broker.pos[KEY][0] == pytest.approx(0.0076)  # his coins are left alone


# ----------------------------------- a bracket child's sales on two days IBKR never reports
# Review pass 2. What the app kept of a child's sale on a day IBKR no longer reports is
# beyond the real executions, never beyond its own estimate of an earlier day's sale.


def _shrink(h, oid: str, qty: float) -> None:
    rec = h.broker.orders[oid]
    rec.request = rec.request.model_copy(update={"qty": rec.request.qty - qty})


async def test_a_targets_sales_on_two_unreported_days_are_both_journaled(db, config) -> None:
    """Wednesday the bot buys 24 F; with the app down its target sells 9 and the owner
    buys 24 (neither reported again). Thursday the app journals the target's 9 at an
    estimate; then, down again from the afternoon, the target sells 5 more. Friday the
    target sells the bot's last 10: the bot's trade is journaled in full (24) and its
    metadata released; none of the owner's 24 is sold."""
    h = await _paper(db, config, datetime(2026, 10, 28, 14, 0, tzinfo=UTC))
    await enter_f(h)
    await ticks(h, 5, seconds=15)
    [target] = h.broker.working("STK:F", "target")
    [stop] = h.broker.working("STK:F", "stop")
    h.clock.advance(minutes=1)  # the app is down from here
    _part(h.broker, target, 9, 10.5)
    _shrink(h, stop, 9)
    owner_buys(h, F, 24, 10.2)
    h.engine = _restart_next_morning(h, config)  # Thursday 10:00 New York
    await ticks(h, 4, seconds=15)
    estimates = [f for f in db.list_fills() if f.exec_id.startswith("unreported-exit:")]
    assert sum(f.qty for f in estimates) == 9
    h.clock.t = datetime(2026, 10, 29, 17, 0, tzinfo=UTC)  # the app is down from here
    _part(h.broker, target, 5, 10.5)
    _shrink(h, stop, 5)
    h.engine = _restart_at(h, config, datetime(2026, 10, 30, 14, 0, tzinfo=UTC))  # Friday
    await ticks(h, 4, seconds=15)
    _part(h.broker, target, 10, 10.5)  # done: the stop goes (one cancels the other)
    h.broker.orders[stop].status = OrderStatus.CANCELLED
    await ticks(h, 10, seconds=30)
    assert h.broker.pos["STK:F"][0] == 24
    assert bot_sells(h, "STK:F") == []
    assert sum(t.qty for t in db.list_trades()) == 24
    assert db.get_position_meta("STK:F") is None


# --------------------------------------------- a pure-bot exit of a partly filled entry
# Review pass 2 (cost). Nobody but the bot trades the symbol and its entry filled in part:
# sizing its exit reads the position and the executions only, as before the merge (no
# second open-orders read, which costs an IBKR round trip and can time out).


def _record_reads(monkeypatch, h) -> list[list[str]]:
    """The broker reads each `_bot_cap` call makes."""
    log: list[str] = []
    cls = type(h.broker)
    for name in ("open_orders", "positions", "fills_since"):
        real = getattr(cls, name)

        async def wrap(self, *a, __real=real, __name=name, **k):
            log.append(__name)
            return await __real(self, *a, **k)

        monkeypatch.setattr(cls, name, wrap)
    real_cap = Engine._bot_cap
    caps: list[list[str]] = []

    async def cap(self, inst):
        start = len(log)
        try:
            return await real_cap(self, inst)
        finally:
            caps.append(log[start:])

    monkeypatch.setattr(Engine, "_bot_cap", cap)
    return caps


@pytest.mark.parametrize("lapse", [False, True])
async def test_a_pure_bot_flatten_of_a_partly_filled_entry_reads_no_more(
    db, config, monkeypatch, lapse: bool
) -> None:
    """7 of the bot's 24 F fill, the rest working or lapsed: the flatten before the close
    sells the 7 with one positions and one executions read."""
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 0})
    entry, _ = await _resting_entry(h)
    _part(h.broker, entry, 7, 10.01)
    await ticks(h, 2, seconds=15)
    if lapse:
        await h.broker.cancel_order(entry)  # the rest lapses, the app sees it done
        await ticks(h, 2, seconds=15)
    caps = _record_reads(monkeypatch, h)
    h.clock.t = datetime(2026, 10, 28, 19, 51, tzinfo=UTC)  # 15:51 New York: flatten
    await h.engine.monitor_tick()
    sold = [r.qty for r in h.broker.placed if r.side == Side.SELL and r.stop_loss is None]
    assert sold == [7]
    assert "STK:F" not in h.broker.pos
    assert caps, "the flatten sized the bot's part"
    assert all(c == ["positions", "fills_since"] for c in caps), caps


async def test_a_pure_bot_crypto_stop_of_a_partly_filled_entry_reads_no_more(
    db, config, monkeypatch
) -> None:
    """The same for BTC: the engine's own stop sells the bot's half-filled entry."""
    h = await _crypto_paper(db, config, NOON)
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    half = round(h.broker.orders[entry].request.qty / 2, 8)
    _part(h.broker, entry, half, 60000.0)
    await ticks(h, 2, seconds=15)
    await h.broker.cancel_order(entry)  # the rest lapses, the app sees it done
    await ticks(h, 2, seconds=15)
    caps = _record_reads(monkeypatch, h)
    h.broker.set_price("BTC", 58500.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 3, seconds=15)  # the engine's stop (59000) is hit and confirmed
    sold = [r.qty for r in h.broker.placed if r.side == Side.SELL and r.instrument == _BTC]
    assert sold == [half]
    assert KEY not in h.broker.pos
    assert caps, "the stop sized the bot's part"
    assert all(c == ["positions", "fills_since"] for c in caps), caps


async def test_a_failed_open_orders_read_never_holds_up_a_pure_bot_flatten(
    db, config, monkeypatch
) -> None:
    """An open-orders read that would time out while the flatten sizes the bot's part
    never leaves its 7 unsold: none is made."""
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 0})
    entry, _ = await _resting_entry(h)
    _part(h.broker, entry, 7, 10.01)
    await ticks(h, 2, seconds=15)
    await h.broker.cancel_order(entry)  # the rest lapses
    await ticks(h, 2, seconds=15)
    sizing = [False]
    real_cap = Engine._bot_cap

    async def cap(self, inst):
        sizing[0] = True
        try:
            return await real_cap(self, inst)
        finally:
            sizing[0] = False

    monkeypatch.setattr(Engine, "_bot_cap", cap)
    cls = type(h.broker)
    real_orders = cls.open_orders

    async def open_orders(self):
        if sizing[0]:
            raise BrokerError("IBKR open orders timed out")
        return await real_orders(self)

    monkeypatch.setattr(cls, "open_orders", open_orders)
    h.clock.t = datetime(2026, 10, 28, 19, 51, tzinfo=UTC)  # 15:51 New York: flatten
    await h.engine.monitor_tick()
    sold = [r.qty for r in h.broker.placed if r.side == Side.SELL and r.stop_loss is None]
    assert sold == [7]


# ----------------------------------- a stop's sale that a lagging position read still holds
# Fuzz seed 15155 (review pass 3). IBKR's executions time out and its portfolio (the position
# read) lags its order status: the read still holds the units the bot's bracket stop just
# sold. The journal matches that read, and the tick's open orders are older than the sale.


async def _close_with_a_lagging_read(h, stop: str, sells: float) -> None:
    """The bot's stop sells `sells` while IBKR's executions time out and the position read
    still shows what was held before; the owner then presses Close on the bot's position."""
    stale = await h.broker.positions()
    h.clock.advance(seconds=5)
    _part(h.broker, stop, sells, 9.8)
    h.broker.fail.add("fills_since")
    real = h.broker.positions

    async def lagging():
        return stale

    h.broker.positions = lagging
    with contextlib.suppress(BrokerError, ValueError, LookupError):
        await h.engine.close_position("STK:F")
    h.broker.positions = real
    h.broker.fail.discard("fills_since")


async def test_an_earlier_day_stop_sale_then_a_lagging_read_never_sells_his_shares(
    db, config
) -> None:
    """Wednesday: the bot's 24 F and the owner's 10 (journaled); with the app down its stop
    sells 8. Thursday the app journals that sale at an estimate (the bot's 16, his 10). The
    stop then sells 8 more while the executions time out and the position read still shows
    26: the Close sells none of his 10."""
    h = await _paper(db, config, NOON)
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 10, 10.0)
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    h.clock.t = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)  # the app is down from here
    _part(h.broker, stop, 8, 9.8)
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 26
    await _close_with_a_lagging_read(h, stop, 8)
    sold = 26 - 8 - h.broker.pos.get("STK:F", [0.0])[0]
    assert sold <= 8, f"the Close sold {sold:g}: {sold - 8:g} of the owner's 10"


async def test_a_stop_sale_a_lagging_read_hides_never_sells_his_shares(db, config) -> None:
    """The bot's 24 F and the owner's 20 (journaled). The stop sells 12 while the
    executions time out and the position read still shows 44: the Close sells none of
    his 20."""
    h = await _paper(db, config, NOON)
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 20, 10.0)
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    await _close_with_a_lagging_read(h, stop, 12)
    sold = 44 - 12 - h.broker.pos.get("STK:F", [0.0])[0]
    assert sold <= 12, f"the Close sold {sold:g}: {sold - 12:g} of the owner's 20"


# ---------------------------------- an earlier day's stop sale cancelled by the bot's close
# Fuzz seed 6928 (review pass 3; pure bot, and with the owner's shares next to it). The tick's
# open orders are read before the bot's own close cancels its stop, and the stop sells more
# during those cancels: what it sold on the earlier day is kept from what the close saw.


@pytest.mark.parametrize("his", [0, 10])
async def test_an_earlier_day_stop_sale_is_journaled_in_full_when_the_close_cancels_it(
    db, config, his: int
) -> None:
    """Wednesday 11:00 New York: the bot buys 24 F (the owner holds `his`, journaled).
    IBKR's executions time out from here: its stop sells 8 that afternoon. Thursday 09:35
    (the same app; the executions read again on the tick it closes) the time stop closes
    the bot's position: the stop sells 3 more as the close cancels it, the close sells the
    other 13. The bot's trade of 24 is journaled and F is free again."""
    h = await harness(db, config, mode=Mode.PAPER, now=NOON, exits={"time_stop_minutes": 45})
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    if his:
        owner_buys(h, F, his, 10.0)
        await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    [target] = h.broker.working("STK:F", "target")
    h.broker.fail.add("fills_since")  # IBKR's executions time out from here
    h.clock.advance(minutes=1)
    _part(h.broker, stop, 8, 9.8)
    trec = h.broker.orders[target]
    trec.request = trec.request.model_copy(update={"qty": 16.0})  # one cancels the other
    await ticks(h, 4, seconds=15)
    assert db.list_trades() == []
    # Thursday 09:35 New York (IBKR reports only Thursday's executions): the time stop is
    # due, and the executions read again on the tick that closes.
    h.clock.t = datetime(2026, 10, 29, 13, 35, tzinfo=UTC)
    every_fill = h.broker.fills_since

    async def today_only(since):
        midnight = trading_day_start(trading_day(h.clock()))
        return [f for f in await every_fill(since) if f.ts >= midnight]

    h.broker.fills_since = today_only
    await ticks(h, 1, seconds=15)  # the time stop is due (confirmed on the next tick)
    h.broker.fail.discard("fills_since")
    real_cancel = h.broker.cancel_order

    async def cancel(oid: str) -> None:
        if oid == stop and h.broker.orders[stop].filled_qty == 8:
            _part(h.broker, stop, 3, 9.8)  # the stop sells 3 more as the close cancels it
        await real_cancel(oid)

    h.broker.cancel_order = cancel
    await ticks(h, 1, seconds=15)
    h.broker.cancel_order = real_cancel
    sold = [r.qty for r in h.broker.placed if r.side == Side.SELL and r.is_exit]
    assert sold == [13], sold
    assert h.broker.pos.get("STK:F", [0.0])[0] == his
    await ticks(h, 4, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == 24, [
        (t.qty, t.exit_reason) for t in db.list_trades()
    ]
    assert db.get_position_meta("STK:F") is None


# ------------------------------------------------- a trip in the second of a journaled trip
# Fuzz seed 9166 (review pass 3). The replay of a position with a trip journaled skipped
# every trip ending up to that trip's exit: one ending in the same second (the bot's close
# right after the tick that journaled its stop's sale) was never journaled.


async def test_a_close_in_the_second_of_a_journaled_trip_is_journaled(db, config) -> None:
    """Wednesday: the bot buys 24 F and the app goes down before it journals the buy.
    Thursday (IBKR reports only Thursday's executions) its stop sells 9; in the same second
    the AI's close runs: its tick journals the stop's 9 as a trip, then it sells the bot's
    other 15. Both trips are journaled: 24 in all, as the bot sold."""
    h = await _paper(db, config, NOON)
    await enter_f(h)  # the app goes down before a tick journals the buy
    [stop] = h.broker.working("STK:F", "stop")
    [target] = h.broker.working("STK:F", "target")
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2, seconds=15)
    assert db.get_position_meta("STK:F") is not None
    _part(h.broker, stop, 9, 9.8)
    trec = h.broker.orders[target]
    trec.request = trec.request.model_copy(update={"qty": 15.0})  # one cancels the other
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()  # its tick journals the stop's 9; the close sells 15
    h.decider.decisions = []
    assert [r.qty for r in h.broker.placed if r.side == Side.SELL and r.is_exit] == [15]
    assert "STK:F" not in h.broker.pos
    await ticks(h, 4, seconds=15)
    assert sorted(t.qty for t in db.list_trades()) == [9, 15]
    assert db.get_position_meta("STK:F") is None


async def test_a_second_execution_in_the_second_of_a_journaled_trip_is_journaled_once(
    db, config
) -> None:
    """As above, but the target sells 5 and a tick journals them as a trip; in the same
    second it sells 5 more (one exit of the same order). The bot's trades hold 10 in all:
    the first 5 never twice."""
    h = await _paper(db, config, NOON)
    await enter_f(h)  # the app goes down before a tick journals the buy
    [stop] = h.broker.working("STK:F", "stop")
    [target] = h.broker.working("STK:F", "target")
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2, seconds=15)
    srec = h.broker.orders[stop]
    _part(h.broker, target, 5, 10.5)
    srec.request = srec.request.model_copy(update={"qty": 19.0})  # one cancels the other
    await h.engine.monitor_tick()  # journals the target's 5 as a trip
    assert [t.qty for t in db.list_trades()] == [5]
    _part(h.broker, target, 5, 10.5)  # the same second
    srec.request = srec.request.model_copy(update={"qty": 14.0})
    await ticks(h, 4, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == 10, [t.qty for t in db.list_trades()]


async def test_a_trip_finer_than_the_journal_keeps_is_never_journaled_again(db, config) -> None:
    """Fuzz seed 1285 (review pass 3). The bot's BTC entry rests and 0.0030600031 of it fills
    (finer than the 8 decimals a trade keeps); an exit of the bot's sells it, and its trade
    is journaled at 0.00306. More of the entry fills and the position is replayed: that
    trip is never journaled again for the 0.0000000031 its trade rounded off."""
    h = await _crypto_paper(db, config, NOON)
    await ticks(h, 2, seconds=15)
    _buy_btc(h)
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    first = 0.0030600031
    _part(h.broker, entry, first, 60005.0)
    await ticks(h, 2, seconds=15)
    h.broker.fills.append(
        Fill(exec_id="E-exit", broker_order_id="7001", instrument=_BTC, side=Side.SELL,
             qty=first, price=59950.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    del h.broker.pos[KEY]
    await ticks(h, 2, seconds=15)
    assert [t.qty for t in db.list_trades()] == [0.00306]
    _part(h.broker, entry, 0.001, 60005.0)  # the position is replayed
    await ticks(h, 4, seconds=15)
    assert [t.qty for t in db.list_trades()] == [0.00306]


# -------------------------------- the watchdog's unmarked sale of an entry partly journaled
# Review pass 3. The journal saw part of the bot's entry fill; the rest of what the watchdog
# sold (next to the entry's working rest, so it never marked the position sold flat) is the
# bot's too, once the symbol reads flat.


async def test_a_watchdog_sale_next_to_a_working_entry_is_journaled_beyond_its_seen_part(
    db, config, monkeypatch
) -> None:
    """Wednesday 11:00 New York: the bot's BTC entry rests and 30% of it fills (journaled).
    The app goes down: 30% more fills, BTC drops through the stop and the watchdog sells
    the bot's 60% with the entry's rest still working. Thursday the rest lapses and BTC
    reads flat: the bot's trade is journaled for all it sold, not only the 30% the journal
    saw it buy."""
    h = await _crypto_paper(db, config, NOON)
    monkeypatch.setattr("trader.db.utcnow", h.clock)
    await ticks(h, 2, seconds=15)
    _buy_btc(h)
    h.broker._match = lambda key: None  # the entry rests
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items()
               if k == "entry" and h.broker.orders[oid].status in OPEN_ORDER_STATUSES]  # fmt: skip
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)
    first = round(total * 0.3, 6)
    _part(h.broker, entry, first, 60005.0)  # the app sees this part fill
    await ticks(h, 2, seconds=15)
    assert sum(f.qty for f in db.list_fills() if f.side == Side.BUY) == pytest.approx(first)
    # The app goes down: more fills, then the watchdog sells all of it (stale heartbeat),
    # with the entry's rest still working.
    h.clock.advance(minutes=1)
    second = round(total * 0.3, 6)
    _part(h.broker, entry, second, 60005.0)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    wd = _watchdog(h)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    assert not sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    # Thursday 10:00 New York: IBKR reports only Thursday's executions. The entry's rest
    # still works for a while, then lapses.
    h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 4, seconds=15)
    assert db.get_position_meta(KEY) is not None
    h.broker.orders[entry].status = OrderStatus.CANCELLED  # the rest lapses
    await ticks(h, 80, seconds=15)
    assert db.get_position_meta(KEY) is None
    trades = db.list_trades()
    assert sum(t.qty for t in trades) == pytest.approx(first + second), (
        f"journal {[t.qty for t in trades]}; the bot bought and sold {first + second}"
    )


# ----------------------------------------------------------- review pass 3 of round 49
async def _close_lagging(h, stop: str, sells: float) -> None:
    stale = await h.broker.positions()
    h.clock.advance(seconds=5)
    _part(h.broker, stop, sells, 9.8)
    h.broker.fail.add("fills_since")
    real = h.broker.positions

    async def lagging():
        return stale

    h.broker.positions = lagging
    with contextlib.suppress(BrokerError, ValueError, LookupError):
        await h.engine.close_position("STK:F")
    h.broker.positions = real
    h.broker.fail.discard("fills_since")


async def test_a_stop_that_sold_all_the_bots_shares_a_lagging_read_hides_sells_none_of_his(
    db, config
) -> None:
    """The bot's 24 F and the owner's 30 (journaled). The stop sells all 24 while the
    executions time out and the position read still shows 54: the Close sells none of
    his 30."""
    h = await _paper(db, config, NOON)
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    await _close_lagging(h, stop, 24)
    sells = [r.qty for r in h.broker.placed if r.side == Side.SELL and r.is_exit]
    assert h.broker.pos.get("STK:F", [0.0])[0] == 30, (
        f"the Close sold {sells}: {30 - h.broker.pos.get('STK:F', [0.0])[0]:g} of his 30"
    )


async def test_a_stop_that_sold_most_of_the_bots_shares_a_lagging_read_hides(db, config) -> None:
    """Control: the stop sells 23 of 24 (still working for 1): the pass-3 fix holds."""
    h = await _paper(db, config, NOON)
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 2, seconds=15)
    [stop] = h.broker.working("STK:F", "stop")
    await _close_lagging(h, stop, 23)
    held = h.broker.pos.get("STK:F", [0.0])[0]
    assert held >= 30, f"{31 - held:g} of his 30 sold"


async def test_with_the_executions_unread_and_the_stop_working_the_close_still_sells_the_bots(
    db, config
) -> None:
    """Control for the fix: nothing sold unseen (the bracket stop covers all 24), the
    executions time out: the Close still sells the bot's 24 and keeps his 30."""
    h = await _paper(db, config, NOON)
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 30, 10.0)
    await ticks(h, 2, seconds=15)
    h.broker.fail.add("fills_since")
    with contextlib.suppress(BrokerError, ValueError, LookupError):
        await h.engine.close_position("STK:F")
    h.broker.fail.discard("fills_since")
    assert h.broker.pos.get("STK:F", [0.0])[0] == 30


def _bot_rows(db) -> list[tuple[str, float]]:
    return [
        (r.request.side.value, r.filled_qty)
        for r in db.list_orders(limit=50)
        if r.request.instrument.key == "STK:F" and r.status == OrderStatus.FILLED
    ]


async def _round_trip_unreported(h) -> None:
    """Wednesday: the bot buys 24 F and the dashboard's Close sells them seconds later;
    the app goes down before a tick journals either execution."""
    await ticks(h, 2, seconds=15)
    await enter_f(h)  # the bot's 24 F
    h.clock.advance(seconds=2)
    await h.engine.close_position("STK:F")
    assert [r.qty for r in h.broker.placed if r.side == Side.SELL and r.is_exit] == [24]
    assert "STK:F" not in h.broker.pos


async def test_his_shares_after_a_round_trip_only_rows_show_never_drop_the_bots_trade(
    db, config
) -> None:
    """Seed 24088. Wednesday the bot's round trip of 24 (above); the owner buys 50 F, all
    unreported Thursday. Thursday he sells his 50: F reads flat. The app's order rows say
    the bot bought 24 and sold 24: its trade is journaled (or its metadata kept), never
    dropped with its metadata."""
    h = await _paper(db, config, NOON)
    await _round_trip_unreported(h)
    owner_buys(h, F, 50, 10.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 4, seconds=15)
    assert h.broker.pos["STK:F"][0] == 50
    owner_sells(h, F, 50, 10.0)
    await ticks(h, 80, seconds=15)  # 20 minutes flat
    assert _bot_rows(db) == [("SELL", 24.0), ("BUY", 24.0)]
    trades = db.list_trades()
    assert sum(t.qty for t in trades) == 24 or db.get_position_meta("STK:F") is not None, (
        f"meta released, journal {[t.qty for t in trades]}; the bot bought and sold 24"
    )
    h.clock.t = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)  # Friday: his sale is not today's
    await ticks(h, 80, seconds=15)
    assert [t.qty for t in db.list_trades()] == [24]
    assert db.get_position_meta("STK:F") is None


async def test_a_round_trip_only_rows_show_is_never_dropped_with_its_metadata(db, config) -> None:
    """No owner: Thursday F reads flat and nothing is reported. The app's order rows say
    the bot bought 24 and sold 24."""
    h = await _paper(db, config, NOON)
    await _round_trip_unreported(h)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 80, seconds=15)
    trades = db.list_trades()
    assert sum(t.qty for t in trades) == 24 or db.get_position_meta("STK:F") is not None, (
        f"meta released, journal {[t.qty for t in trades]}; the bot bought and sold 24"
    )
    assert [t.qty for t in trades] == [24]
    assert db.get_position_meta("STK:F") is None
    assert "Exit of F not reported" in h.notes.titles()


async def test_a_stop_sale_only_rows_and_the_close_saw_is_journaled_once_flat(db, config) -> None:
    """Seed 22035. Wednesday the bot buys 24 F and the app goes down before a tick journals
    the buy: its stop sells 9 and the owner buys 25, all unreported Thursday. Thursday the
    dashboard's Close sells the bot's other 15 (journaled; it runs before the app's first
    tick, so only the close saw the stop's 9), then he sells his 25: F reads
    flat. The bot sold 24: its trades hold 24 once the stop's 9 can be estimated, and its
    metadata is never dropped with 9 of them unjournaled."""
    from tests.test_engine import _part

    h = await _paper(db, config, NOON)
    await ticks(h, 2, seconds=15)
    await enter_f(h)  # the app goes down before a tick journals the buy
    [stop] = h.broker.working("STK:F", "stop")
    [target] = h.broker.working("STK:F", "target")
    h.clock.advance(minutes=1)
    _part(h.broker, stop, 9, 9.8)
    trec = h.broker.orders[target]
    trec.request = trec.request.model_copy(update={"qty": 15.0})  # one cancels the other
    owner_buys(h, F, 25, 10.0)
    h.engine = _restart_next_morning(h, config)
    await h.engine.close_position("STK:F")  # before the app's first tick
    assert [r.qty for r in h.broker.placed if r.side == Side.SELL and r.is_exit] == [15]
    await ticks(h, 12, seconds=15)
    assert read_hidden(read_extra(db, "STK:F"))[0][1] == 25, "his 25 are found"
    owner_sells(h, F, 25, 10.0)
    await ticks(h, 80, seconds=15)  # 20 minutes flat
    trades = db.list_trades()
    assert sum(t.qty for t in trades) == 24 or db.get_position_meta("STK:F") is not None, (
        f"meta released, journal {[t.qty for t in trades]}; the bot bought and sold 24"
    )
    h.clock.t = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)  # Friday: his sale is not today's
    await ticks(h, 80, seconds=15)
    assert sum(t.qty for t in db.list_trades()) == 24
    assert db.get_position_meta("STK:F") is None
