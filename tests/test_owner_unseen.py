"""The owner's shares next to the bot's when no fill shows some of them (review round 48):
units of his bought on a day IBKR no longer reports, an entry's rest the app settled, an
exit of an unseen entry in several executions, and the crypto watchdog's sales."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_engine import (
    _BTC,
    _part,
    _restart_next_morning,
    _resting_entry,
    cand,
    enter_f,
    harness,
)
from tests.test_owner_edges import _paper, _partly_filled_entry_then_outage
from tests.test_owner_shares import F, bot_sells, owner_buys, owner_sells, ticks
from tests.test_watchdog import Notes, settings
from trader.broker.base import BrokerError
from trader.engine import Engine
from trader.models import (
    OPEN_ORDER_STATUSES,
    AIDecision,
    AssetClass,
    Mode,
    OrderRecord,
    OrderStatus,
    Side,
    TradeAction,
)
from trader.ownership import EXTRA_PREFIX, REST_SETTLED
from trader.timeutil import trading_day, trading_day_start
from trader.watchdog import CryptoWatchdog, sold_by_watchdog

KEY = "CRYPTO:BTC"


def _restart_at(h, config, at: datetime) -> Engine:
    """The app comes back at `at`; IBKR reports only that day's executions."""
    h.clock.t = at
    every = getattr(h.broker.fills_since, "every_fill", h.broker.fills_since)

    async def today_only(since):
        midnight = trading_day_start(trading_day(h.clock()))
        return [f for f in await every(since) if f.ts >= midnight]

    today_only.every_fill = every  # type: ignore[attr-defined]
    h.broker.fills_since = today_only
    return Engine(config, h.db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                  scanner=h.scanner)  # fmt: skip


def _buy_btc(h) -> None:
    h.scanner.candidates = {
        KEY: [cand("BTC", price=60000.0, stop=59000.0, target=62000.0)
              .model_copy(update={"instrument": _BTC})]
    }  # fmt: skip
    h.decider.decisions = [
        AIDecision(action=TradeAction.BUY, symbol="BTC", asset_class=AssetClass.CRYPTO,
                   conviction=0.8, setup="vwap_reclaim", rationale="reclaim")
    ]  # fmt: skip


async def _crypto_paper(db, config, now: datetime):
    h = await harness(db, config, mode=Mode.PAPER, now=now, trade_crypto=True,
                      trade_stocks=False, exits={"time_stop_minutes": 0})  # fmt: skip
    h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    return h


class _AsOtherClient:
    """The engine's FakeBroker as the crypto watchdog's own connection: its executions are
    another client's (perm:) to the app."""

    def __init__(self, broker) -> None:
        self.b = broker
        self.perm = itertools.count(900)

    def __getattr__(self, name):
        return getattr(self.b, name)

    async def place_order(self, req):
        n = len(self.b.fills)
        rec = await self.b.place_order(req)
        for i in range(n, len(self.b.fills)):
            self.b.fills[i] = self.b.fills[i].model_copy(
                update={"broker_order_id": f"perm:{next(self.perm)}"}
            )
        return rec


def _watchdog(h) -> CryptoWatchdog:
    return CryptoWatchdog(h.db, _AsOtherClient(h.broker), settings(), Notes(), h.clock,
                          started_at=h.clock() - timedelta(hours=1))  # fmt: skip


# ----------------------------------------------------------------- lapsed rest, lagging cap
async def _lapsed_rest_no_owner(db, config):
    """Wednesday: the bot's entry of 24 F fills 20 and its rest lapses."""
    h = await _paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    entry, total = await _resting_entry(h)
    assert total == 24
    _part(h.broker, entry, 20, 10.01)
    await ticks(h, 2, seconds=15)
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    await ticks(h, 2, seconds=15)
    return h, entry


@pytest.mark.parametrize("wait", [1, 3])
async def test_a_part_fill_never_lets_a_lapsed_rest_take_an_unreported_owner_buy(
    db, config, wait: int
) -> None:
    """The owner bought 6 on Wednesday evening (no fill shows them). Thursday the target
    sells 4 of the bot's 20: a Close 1 or 3 minutes later sells the bot's 16, never his 6
    for the entry's lapsed rest."""
    h, entry = await _lapsed_rest_no_owner(db, config)
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 6, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    _part(h.broker, f"{entry}-TP", 4, 10.5)
    await ticks(h, wait)
    await h.engine.close_position("STK:F")
    assert h.broker.pos["STK:F"][0] == 6


async def test_the_flatten_after_a_part_fill_keeps_an_unreported_owner_buy(db, config) -> None:
    h, entry = await _lapsed_rest_no_owner(db, config)
    h.clock.t = datetime(2026, 10, 28, 19, 45, tzinfo=UTC)
    owner_buys(h, F, 6, 10.4)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 29, 19, 49, tzinfo=UTC)
    _part(h.broker, f"{entry}-TP", 4, 10.5)
    await ticks(h, 1, seconds=40)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 6


async def test_a_crypto_ioc_retry_never_credits_a_lapsed_rest_over_his_coins(db, config) -> None:
    """The bot's BTC entry fills 60% and its rest lapses; the owner buys half an entry on
    Wednesday evening, app down. Thursday the stop's first IOC fills half; its retry sells
    the bot's rest, never his coins."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    _buy_btc(h)
    match = h.broker._match
    h.broker._match = lambda key: None
    await h.engine.run_cycle()
    h.broker._match = match
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    seen = round(total * 0.6, 6)
    _part(h.broker, entry, seen, 60005.0)
    await ticks(h, 2, seconds=15)
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)
    his = round(total * 0.5, 8)
    owner_buys(h, _BTC, his, 60000.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3, seconds=15)
    real = h.broker.place_order
    first: list[float] = []

    async def ioc(req):
        if not first and req.side == Side.SELL and req.stop_loss is None:
            first.append(req.qty)
            h.broker.placed.append(req)
            oid = f"IOC{len(h.broker.placed)}"
            h.broker.orders[oid] = OrderRecord(request=req, status=OrderStatus.SUBMITTED,
                                               broker_order_id=oid)  # fmt: skip
            h.broker.kind[oid], h.broker.parent[oid] = "exit", None
            _part(h.broker, oid, round(req.qty / 2, 8), 58990.0)
            h.broker.orders[oid].status = OrderStatus.CANCELLED  # IOC: the rest cancelled
            return h.broker.orders[oid].model_copy()
        return await real(req)

    h.broker.place_order = ioc
    h.broker.set_price("BTC", 58990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 8, seconds=15)
    assert first
    assert h.broker.pos[KEY][0] == pytest.approx(his, abs=1e-8)


# ----------------------------------------------------------- one exit, several executions
@pytest.mark.parametrize("parts", [[17], [10, 7]])
async def test_an_unreported_rest_sold_in_several_executions_is_one_trade(
    db, config, parts: list[float]
) -> None:
    """The rest (17) of a partly filled entry filled while the app was down; Thursday its
    stop sells it in one or two executions: one bot trade of 17, after the first 7."""
    h = await _partly_filled_entry_then_outage(db, config)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    for q in parts:
        _part(h.broker, f"{entry}-SL", q, 9.5)
    await ticks(h, 20, seconds=15)
    assert sorted(t.qty for t in db.list_trades()) == [7, 17]


@pytest.mark.parametrize("parts", [[24], [14, 10]])
async def test_an_unseen_entry_sold_in_several_executions_is_one_trade(
    db, config, parts: list[float]
) -> None:
    """Pure bot: the whole entry filled Wednesday after the app went down; Thursday its
    stop sells the 24 in one or two executions: one trade, one loss."""
    h = await harness(db, config, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC), mode=Mode.PAPER,
                      exits={"time_stop_minutes": 0})  # fmt: skip
    entry, total = await _resting_entry(h)
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
    _part(h.broker, entry, total, 10.01)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3, seconds=15)
    for q in parts:
        _part(h.broker, f"{entry}-SL", q, 9.5)
    await ticks(h, 20, seconds=15)
    assert [t.qty for t in db.list_trades()] == [24]


async def test_a_late_commission_never_splits_an_exit_or_drops_its_rest(db, config) -> None:
    h = await _partly_filled_entry_then_outage(db, config)
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    _part(h.broker, f"{entry}-SL", 10, 9.5)
    _part(h.broker, f"{entry}-SL", 7, 9.5)
    late = h.broker.fills[-1]
    h.broker.fills[-1] = late.model_copy(update={"commission": 0.0})
    await ticks(h, 1, seconds=15)
    h.broker.fills[-1] = late  # the commission report arrives
    await ticks(h, 20, seconds=15)
    assert sorted(t.qty for t in db.list_trades()) == [7, 17]


# ----------------------------------------------- the owner's sale of units no fill shows
async def test_his_sale_of_an_unreported_buy_never_closes_the_bots_stock(db, config) -> None:
    """The bot holds 24 F; the owner buys 50 while the app is down through the close.
    Thursday he sells his 50: the bot's trade stays open, with its stop, and is sold when
    the price falls below it."""
    h = await harness(db, config, mode=Mode.PAPER, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                      exits={"time_stop_minutes": 0})  # fmt: skip
    await enter_f(h)
    await ticks(h, 2)
    h.clock.t = datetime(2026, 10, 28, 19, 0, tzinfo=UTC)
    owner_buys(h, F, 50, 10.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 3, seconds=15)
    owner_sells(h, F, 50, 10.1)
    await ticks(h, 4, seconds=15)
    assert h.broker.pos["STK:F"][0] == 24
    assert db.get_position_meta("STK:F") is not None
    assert h.broker.working("STK:F", "stop")
    assert db.list_trades() == []
    h.broker.set_price("F", 9.5)
    await ticks(h, 10, seconds=15)
    assert "STK:F" not in h.broker.pos


@pytest.mark.parametrize("mult", [2.0, 1.0, 0.5])
async def test_his_sale_of_an_unreported_btc_buy_never_closes_the_bots(
    db, config, mult: float
) -> None:
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 19, 0, tzinfo=UTC))
    _buy_btc(h)
    await h.engine.run_cycle()
    h.decider.decisions = []
    await ticks(h, 2, seconds=15)
    bot = h.broker.pos[KEY][0]
    h.clock.t = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    his = round(bot * mult, 8)
    owner_buys(h, _BTC, his, 60000.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 4, seconds=15)
    owner_sells(h, _BTC, his, 60100.0)
    await ticks(h, 4, seconds=15)
    assert db.list_trades() == []
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 10, seconds=15)
    assert KEY not in h.broker.pos
    [trade] = db.list_trades()
    assert trade.qty == pytest.approx(bot)


async def test_his_sale_before_the_app_came_back_is_his(db, config) -> None:
    """He sold his unreported buy on Thursday morning before the app came back."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 29, 0, 0, tzinfo=UTC))
    _buy_btc(h)
    await h.engine.run_cycle()
    h.decider.decisions = []
    await ticks(h, 2, seconds=15)
    bot = h.broker.pos[KEY][0]
    h.broker.set_price("BTC", 60700.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    his = round(bot * 2, 8)
    owner_buys(h, _BTC, his, 60100.0)
    h.broker.set_price("BTC", 61000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    h.clock.t = datetime(2026, 10, 29, 13, 0, tzinfo=UTC)
    owner_sells(h, _BTC, his, 61000.0)
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 4, seconds=15)
    assert db.list_trades() == []
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 10, seconds=15)
    assert KEY not in h.broker.pos
    assert bot_sells(h, KEY) == [pytest.approx(bot)]


# ------------------------------------------------------------------ the settled rest
@pytest.mark.parametrize("forgotten", [True, False])
async def test_a_settled_rest_is_never_credited_to_an_exit(db, config, forgotten: bool) -> None:
    """Wednesday: the bot's entry of 24 F fills 7, the owner buys 50 (journaled), the rest
    lapses with the app down. Thursday evening he buys 10 more, app down. Friday a Close
    sells the bot's 7, never his 10 as the rest."""
    h = await harness(db, config, now=datetime(2026, 10, 28, 14, 0, tzinfo=UTC),
                      mode=Mode.PAPER, exits={"time_stop_minutes": 0})  # fmt: skip
    entry, _ = await _resting_entry(h)
    _part(h.broker, entry, 7, 10.01)
    await ticks(h, 2, seconds=15)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 28, 17, 0, tzinfo=UTC)
    h.broker.orders[entry].status = OrderStatus.CANCELLED
    if forgotten:
        known = h.broker.order_status

        async def order_status(oid):
            return None if oid == entry else await known(oid)

        h.broker.order_status = order_status
    h.engine = _restart_at(h, config, datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    await ticks(h, 3, seconds=15)
    if forgotten:
        assert REST_SETTLED in (db.get_kv(EXTRA_PREFIX + "STK:F") or "")
    h.clock.t = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)
    owner_buys(h, F, 10, 10.0)
    h.engine = _restart_at(h, config, datetime(2026, 10, 30, 14, 0, tzinfo=UTC))
    await ticks(h, 3, seconds=15)
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 7
    assert h.broker.pos["STK:F"][0] == 60


# ------------------------------------------------------------- the crypto watchdog's sales
@pytest.mark.parametrize("earlier_trip", [False, True])
async def test_a_watchdog_flat_sale_estimate_is_journaled_after_an_earlier_trip(
    db, config, earlier_trip: bool
) -> None:
    """The bot's resting BTC entry fills while the app is down and the watchdog flattens
    it; an earlier BTC trip of the bot is in the journal. Thursday the estimate of the
    watchdog's sale is dated in this position's window, so its trip is journaled."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 28, 14, 0, tzinfo=UTC))
    first_qty = 0.0
    if earlier_trip:
        _buy_btc(h)
        await h.engine.run_cycle()
        h.decider.decisions = []
        await ticks(h, 2, seconds=15)
        first_qty = h.broker.pos[KEY][0]
        h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
        await ticks(h, 8, seconds=15)
        h.broker.set_price("BTC", 60000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
        assert db.get_position_meta(KEY) is None
    h.clock.t = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    _buy_btc(h)
    h.broker._match = lambda key: None
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items()
               if k == "entry" and h.broker.orders[oid].status in OPEN_ORDER_STATUSES]  # fmt: skip
    total = h.broker.orders[entry].request.qty
    await ticks(h, 2, seconds=15)
    wd = _watchdog(h)
    h.clock.advance(minutes=30)
    _part(h.broker, entry, total, 60005.0)
    h.broker.set_price("BTC", 59990.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(40):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert sold_by_watchdog(db, KEY, db.get_position_meta(KEY))
    h.engine = _restart_next_morning(h, config)
    await ticks(h, 80, seconds=15)
    assert db.get_position_meta(KEY) is None
    assert sum(t.qty for t in db.list_trades()) == pytest.approx(first_qty + total)


async def test_his_sale_between_two_watchdog_sales_never_costs_him_coins(db, config) -> None:
    """The watchdog flattens 70% of the bot's resting entry; back up, the app sees the
    owner do a BTC round trip and then buy 0.01; the entry's rest fills and the watchdog
    sells the bot's part. His round trip used none of the watchdog's sale: all of his
    0.01 stays, and the app's Close of BTC sells none of it."""
    h = await _crypto_paper(db, config, datetime(2026, 10, 29, 5, 0, tzinfo=UTC))
    _buy_btc(h)
    h.broker._match = lambda key: None
    await h.engine.run_cycle()
    h.decider.decisions = []
    [entry] = [oid for oid, k in h.broker.kind.items() if k == "entry"]
    total = h.broker.orders[entry].request.qty
    first = round(total * 0.7, 6)
    await ticks(h, 2, seconds=15)
    wd = _watchdog(h)
    h.clock.t = datetime(2026, 10, 29, 5, 25, tzinfo=UTC)
    _part(h.broker, entry, first, 59990.0)
    for _ in range(30):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert KEY not in h.broker.pos
    h.clock.t = datetime(2026, 10, 29, 8, 50, tzinfo=UTC)
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 4, seconds=15)
    owner_buys(h, _BTC, 0.0004, 60000.0)
    await ticks(h, 4, seconds=15)
    await wd.tick()
    owner_sells(h, _BTC, 0.0004, 60000.0)
    await ticks(h, 2, seconds=15)
    owner_buys(h, _BTC, 0.01, 60000.0)
    await ticks(h, 2, seconds=15)
    _part(h.broker, entry, round(total - first, 6), 59990.0)
    await ticks(h, 2, seconds=15)
    h.clock.advance(minutes=1)
    for _ in range(30):
        h.clock.advance(seconds=10)
        await wd.tick()
    assert h.broker.pos[KEY][0] == pytest.approx(0.01)
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 4, seconds=15)
    if db.get_position_meta(KEY) is not None:
        with pytest.raises((ValueError, LookupError)):
            await h.engine.close_position(KEY)
    assert h.broker.pos[KEY][0] == pytest.approx(0.01)


# ---------------------------------------------------------------- leftover exit orders
async def test_a_stop_placed_in_the_releasing_tick_is_cancelled(db, config) -> None:
    """The owner's Close sells the bot's 24, the executions read fails three times, and he
    buys 3 + 10 F meanwhile. Re-protect places a stop for the 13 held before the journal
    shows the Close; the tick that then releases the metadata cancels that stop too."""
    h = await harness(db, config, mode=Mode.PAPER, now=datetime(2026, 10, 28, 15, 0, tzinfo=UTC),
                      exits={"time_stop_minutes": 0})  # fmt: skip
    await enter_f(h)
    await ticks(h, 2, seconds=15)
    real = h.broker.fills_since
    failures = {"n": 0}

    async def flaky(since):
        if failures["n"] > 0:
            failures["n"] -= 1
            raise BrokerError("IBKR executions timed out")
        return await real(since)

    h.broker.fills_since = flaky
    h.clock.advance(seconds=5)
    await h.engine.close_position("STK:F")
    failures["n"] = 3
    h.clock.advance(seconds=2)
    owner_buys(h, F, 3, 10.0)
    h.clock.advance(seconds=4)
    owner_buys(h, F, 10, 10.0)
    for step in (15, 15, 36):
        h.clock.advance(seconds=step)
        await h.engine.monitor_tick()
    await ticks(h, 10, seconds=15)
    assert db.get_position_meta("STK:F") is None
    assert h.broker.working("STK:F", "stop") == []
    h.broker.set_price("F", 9.7)
    await ticks(h, 3, seconds=15)
    assert h.broker.pos["STK:F"][0] == 13


# ------------------------------------------------- a lagging read, the executions unread
@pytest.mark.parametrize("lagged", [2, 3])
async def test_an_owner_buy_a_lagging_read_hides_is_never_sold_with_the_executions_unread(
    db, config, lagged: int
) -> None:
    """The bot's 24 F, not journaled yet; the owner buys 50 seconds before he presses
    Close. The executions read fails and the position reads up to the cap's still show the
    bot's 24 (they lag): nothing says he bought. The close's own read holds 74: Close sells
    the bot's 24, never his 50."""
    h = await harness(db, config, exits={"time_stop_minutes": 0})
    await enter_f(h)
    h.clock.advance(seconds=5)
    owner_buys(h, F, 50, 10.0)

    async def failing(since):
        raise BrokerError("IBKR executions timed out after 10s")

    h.broker.fills_since = failing
    real = h.broker.positions
    calls = {"n": 0}

    async def lagging():
        calls["n"] += 1
        out = await real()
        if calls["n"] <= lagged:
            return [p.model_copy(update={"qty": 24.0}) for p in out]
        return out

    h.broker.positions = lagging
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 24
    assert h.broker.pos["STK:F"][0] == 50
