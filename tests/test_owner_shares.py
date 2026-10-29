"""The owner adds to a symbol the bot holds: no bot exit ever sells the owner's shares,
and the bot's own position keeps its protection and management."""

from __future__ import annotations

import contextlib
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_engine import (
    _BTC,
    NOW,
    H,
    _hold_btc,
    _part,
    _restart_next_morning,
    _resting_entry,
    buy,
    enter_f,
    harness,
    hold,
)
from trader.broker.base import BrokerError
from trader.engine import Engine
from trader.models import (
    AssetClass,
    Fill,
    Instrument,
    Mode,
    OrderStatus,
    Side,
    TradeAction,
)

F = Instrument(symbol="F")
_ids = itertools.count(1)


def owner_buys(h: H, inst: Instrument, qty: float, price: float, *, journal: bool = True) -> None:
    """The owner buys in TWS / IBKR Mobile (another API client: perm:<id>)."""
    key = inst.key
    h.broker.fills.append(
        Fill(exec_id=f"X{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=inst,
             side=Side.BUY, qty=qty, price=price, commission=1.0, ts=h.clock())
    )  # fmt: skip
    held, avg = h.broker.pos.get(key, [0.0, 0.0])
    h.broker.pos[key] = [held + qty, (held * avg + qty * price) / (held + qty)]
    h.broker.cash -= qty * price


def owner_sells(h: H, inst: Instrument, qty: float, price: float) -> None:
    key = inst.key
    h.broker.fills.append(
        Fill(exec_id=f"X{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=inst,
             side=Side.SELL, qty=qty, price=price, commission=1.0, ts=h.clock())
    )  # fmt: skip
    held, avg = h.broker.pos[key]
    if held - qty <= 1e-9:
        del h.broker.pos[key]
    else:
        h.broker.pos[key] = [held - qty, avg]
    h.broker.cash += qty * price


def bot_sells(h: H, key: str) -> list[float]:
    return [r.qty for r in h.broker.placed if r.side == Side.SELL and r.instrument.key == key
            and r.stop_loss is None]  # fmt: skip


async def ticks(h: H, n: int, **step: float) -> None:
    for _ in range(n):
        h.clock.advance(**(step or {"minutes": 1}))
        await h.engine.monitor_tick()


# ----------------------------------------------------------------------------- stocks
async def test_flatten_before_close_sells_only_the_bots_shares(db, config) -> None:
    h = await harness(db, config, exits={"time_stop_minutes": 0})
    await enter_f(h)  # 24 shares, bracket stop + target
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)  # 15:51 New York
    await h.engine.monitor_tick()
    assert bot_sells(h, "STK:F") == [24]
    assert h.broker.pos["STK:F"][0] == 50
    await ticks(h, 5)
    assert bot_sells(h, "STK:F") == [24], "the owner's 50 shares are never sold"
    assert any("yours" in m or "owner" in m.lower() for _, m, _ in h.notes.sent)


async def test_time_stop_sells_only_the_bots_shares_when_the_owner_buys_between_ticks(
    db, config
) -> None:
    """The owner's buy is not journaled yet when the confirmed time stop fires."""
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()  # time stop seen once
    owner_buys(h, F, 50, 10.0)  # between the two ticks
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert bot_sells(h, "STK:F") == [24]
    assert h.broker.pos["STK:F"][0] == 50


async def test_ai_close_and_dashboard_close_sell_only_the_bots_shares(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    assert bot_sells(h, "STK:F") == [24]
    assert h.broker.pos["STK:F"][0] == 50
    await ticks(h, 2)
    # The bot's trade is journaled from its own 24 and its position released: F is the
    # owner's now (the dashboard's Close of it is the owner's own, as for any owner row).
    [trade] = db.list_trades()
    assert trade.qty == 24
    assert db.get_position_meta("STK:F") is None
    assert h.broker.pos["STK:F"][0] == 50


async def test_dashboard_close_of_a_mixed_position_sells_the_bots_part(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    rec = await h.engine.close_position("STK:F")
    assert rec.request.qty == 24
    assert h.broker.pos["STK:F"][0] == 50


async def test_reprotect_covers_only_the_bots_shares(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    await h.broker.cancel_order(h.broker.working("STK:F", "stop")[0])
    h.broker._connected = False
    await ticks(h, 2)
    stops = [r for r in h.broker.placed if r.side == Side.SELL and r.stop_loss is not None]
    assert stops and stops[-1].qty == 24


async def test_backup_stop_sells_only_the_bots_shares(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    await h.broker.cancel_order(h.broker.working("STK:F", "stop")[0])
    h.broker.standalone_stops = False  # no re-placed stop: the engine's backup stop acts
    h.broker.set_price("F", 9.70)  # >0.5% below the 9.8 stop
    await ticks(h, 3)
    assert bot_sells(h, "STK:F") == [24]
    assert h.broker.pos["STK:F"][0] == 50


async def test_partly_filled_entry_then_owner_buy_then_rest_fills(db, config) -> None:
    """7 of 24 fill, the owner buys 10, the other 17 fill: the bot's exit sells its 24."""
    h = await harness(db, config, exits={"time_stop_minutes": 0})
    b = h.broker
    entry, total = await _resting_entry(h)
    _part(b, entry, 7, 10.01)
    await ticks(h, 1)
    owner_buys(h, F, 10, 10.0)
    await ticks(h, 1)
    _part(b, entry, total - 7, 10.01)
    await ticks(h, 1)
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)
    await h.engine.monitor_tick()
    assert bot_sells(h, "STK:F") == [24]
    assert b.pos["STK:F"][0] == 10


async def test_without_the_owner_the_bots_exit_still_sells_everything(db, config) -> None:
    h = await harness(db, config, exits={"time_stop_minutes": 0})
    b = h.broker
    entry, total = await _resting_entry(h)
    _part(b, entry, 7, 10.01)
    await ticks(h, 1)
    _part(b, entry, total - 7, 10.01)  # fills right before the flatten tick
    h.clock.t = datetime(2026, 10, 21, 19, 51, tzinfo=UTC)
    await h.engine.monitor_tick()
    assert bot_sells(h, "STK:F") == [24]
    assert "STK:F" not in b.pos


# ----------------------------------------------------------------------------- crypto
async def test_engine_stop_sells_only_the_bots_btc(db, config) -> None:
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 1, seconds=15)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 3, seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == [mine]
    assert h.broker.pos["CRYPTO:BTC"][0] == 0.5
    await ticks(h, 8, seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == [mine]


async def test_owner_round_trip_in_btc_keeps_the_bots_stop(db, config) -> None:
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 1, seconds=15)
    owner_sells(h, _BTC, 0.5, 60010.0)
    await ticks(h, 1, seconds=15)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 3, seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == [pytest.approx(mine)]
    assert "CRYPTO:BTC" not in h.broker.pos


# ----------------------------------------------------------------------------- engine edges
async def test_an_unreported_bot_entry_is_still_managed_in_full(db, config) -> None:
    """The bot's own BTC fills are not in the journal (filled while the app was down, on a
    day IBKR no longer reports): nothing says the owner bought, so the bot's stop sells it
    all, as before."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    db._exec("DELETE FROM fills")
    h.broker.fills.clear()
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 3, seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == [pytest.approx(mine)]
    assert "CRYPTO:BTC" not in h.broker.pos


async def test_owner_buy_with_the_executions_read_failing_still_caps(db, config) -> None:
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 1, seconds=15)  # journaled
    real = h.broker.fills_since
    calls = {"n": 0}

    async def flaky(since):
        calls["n"] += 1
        if calls["n"] % 2 == 0:  # every second read fails (the cap's own read among them)
            raise BrokerError("executions timed out")
        return await real(since)

    h.broker.fills_since = flaky
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 6, seconds=15)
    assert all(q == pytest.approx(mine) for q in bot_sells(h, "CRYPTO:BTC"))
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


async def test_bracket_stop_fills_and_the_time_stop_fires_on_the_owners_shares(db, config) -> None:
    """The owner added 50; the bot's bracket stop sells its 24 between two ticks while the
    engine's time stop is being confirmed: the time stop must not sell the owner's 50 on a
    journal that does not have the stop's fill yet."""
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()  # time stop seen once
    stop_id = h.broker.working("STK:F", "stop")[0]
    h.broker._fill(stop_id, 9.8)  # the bracket stop sells the bot's 24 (owner's 50 stay)
    assert h.broker.pos["STK:F"][0] == 50
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    await ticks(h, 4)
    assert bot_sells(h, "STK:F") == []
    assert h.broker.pos["STK:F"][0] == 50


async def test_the_watchdogs_sale_then_the_owners_buy_is_never_sold_by_the_app(db, config) -> None:
    """The watchdog sold the bot's BTC while the app was down; the owner bought BTC right
    after (before the watchdog's flat check). The app comes back with the old metadata."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    meta = db.get_position_meta("CRYPTO:BTC")
    from trader.watchdog import position_marker

    mine = h.broker.pos.pop("CRYPTO:BTC")[0]
    h.broker.fills.append(
        Fill(exec_id="WD1", broker_order_id="perm:77", instrument=_BTC, side=Side.SELL,
             qty=mine, price=59000.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    db.audit("watchdog_close", {"instrument": "CRYPTO:BTC", "qty": mine, "status": "submitted",
             "marker": position_marker(meta)}, actor="watchdog")  # fmt: skip
    h.clock.advance(seconds=20)
    owner_buys(h, _BTC, 0.5, 59000.0)
    h.broker._connected = False
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 10, seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == []
    assert h.broker.pos["CRYPTO:BTC"][0] == 0.5


async def test_after_the_watchdog_sold_the_bots_part_the_app_leaves_the_owners_btc(
    db, config
) -> None:
    """Mixed BTC; the app is down and the watchdog sold only the bot's part (its audit row
    names the position). The app comes back with the metadata still there."""
    from trader.watchdog import position_marker

    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 1, seconds=15)
    meta = db.get_position_meta("CRYPTO:BTC")
    h.clock.advance(minutes=5)  # the app is down
    h.broker.pos["CRYPTO:BTC"][0] -= mine
    h.broker.fills.append(
        Fill(exec_id="WD1", broker_order_id="perm:77", instrument=_BTC, side=Side.SELL,
             qty=mine, price=58990.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    db.audit("watchdog_close", {"instrument": "CRYPTO:BTC", "qty": mine, "status": "submitted",
             "marker": position_marker(meta)}, actor="watchdog")  # fmt: skip
    h.broker._connected = False
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 10, seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == []
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


# ----------------------------------------------------------------------------- pure parts
def _f(side: Side, qty: float, minute: int, other: bool = False) -> Fill:
    return Fill(exec_id=f"P{next(_ids)}", broker_order_id=("perm:9" if other else "41"),
                instrument=F, side=side, qty=qty, price=10.0,
                ts=NOW + timedelta(minutes=minute))  # fmt: skip


def test_split_attribution() -> None:
    from trader.ownership import Cap, entry_qty, sell_cap, split_fills

    B, S = Side.BUY, Side.SELL
    # the owner adds 50 to the bot's 24
    s = split_fills([_f(B, 24, 1), _f(B, 50, 2, True)])
    assert (s.bot, s.owner, s.owner_bought) == (24, 50, 50)
    assert sell_cap(74, 24, s) == Cap(qty=24, keep=50)
    # the owner sells his own first; past them, the bot's
    s = split_fills([_f(B, 24, 1), _f(B, 50, 2, True), _f(S, 30, 3, True)])
    assert (s.bot, s.owner) == (24, 20)
    s = split_fills([_f(B, 24, 1), _f(B, 50, 2, True), _f(S, 60, 3, True)])
    assert (s.bot, s.owner) == (14, 0)
    # the owner's round trip leaves the bot's 24
    s = split_fills([_f(B, 24, 1), _f(B, 10, 2, True), _f(S, 10, 3, True)])
    assert s.bot == 24 and sell_cap(24, 24, s) == Cap(qty=24, keep=0)
    # the owner sells 5 of the bot's (he bought none): the bot holds the 19 left
    s = split_fills([_f(B, 24, 1), _f(S, 5, 2, True)])
    assert s.bot == 19 and sell_cap(19, 24, s) is None  # nothing of the owner's: sell all
    # the owner sells before he buys: the sale came out of the bot's shares
    s = split_fills([_f(B, 24, 1), _f(S, 5, 2, True), _f(B, 5, 3, True)])
    assert sell_cap(24, 24, s) == Cap(qty=19, keep=5)
    # this app's own sell (KILL of a mixed position) comes out of the bot's first
    s = split_fills([_f(B, 24, 1), _f(B, 50, 2, True), _f(S, 74, 3)])
    assert (s.bot, s.owner) == (0, 0) and [f.qty for f in s.bot_fills] == [24, 24]
    # once the watchdog sold, another client's sell comes out of the bot's first
    s = split_fills(
        [_f(B, 24, 1), _f(B, 50, 2, True), _f(S, 24, 3, True)],
        bot_first_from=NOW + timedelta(minutes=3),
    )
    assert (s.bot, s.owner) == (0, 50) and sell_cap(50, 24, s) == Cap(qty=0, keep=50)
    # more held than the bot's entry bought: the owner's, even with no owner fill journaled
    s = split_fills([_f(B, 24, 1)])
    assert sell_cap(30, 24, s) == Cap(qty=24, keep=0)
    # held within the entry and no owner fill: the bot's (e.g. its fills not reported)
    assert sell_cap(24, 24, split_fills([])) is None
    # no other client bought: the bot's fills come back unchanged
    plain = [_f(B, 24, 1), _f(S, 10, 2, True), _f(S, 14, 3)]
    assert split_fills(plain).bot_fills == plain
    # entry quantity: recorded, or from the planned risk of an older row
    assert entry_qty({"qty": 24}, {}) == 24
    assert entry_qty({"qty": True}, {}) is None
    assert entry_qty({"unit_risk": 0.21}, {"initial_risk": 0.21 * 24}) == 24
    assert entry_qty({}, {"initial_risk": 5.04}) is None


async def test_restart_next_day_with_the_owners_btc_next_to_the_bots(db, config) -> None:
    """The bot bought BTC Wednesday evening (journaled then); the app restarts Thursday,
    when IBKR reports only Thursday's executions, among them the owner's buy."""
    from tests.test_engine import _restart_next_morning

    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))  # Wed 21:00
    mine = h.broker.pos["CRYPTO:BTC"][0]
    engine = _restart_next_morning(h, config)
    owner_buys(h, _BTC, 0.5, 60000.0)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(6):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == [pytest.approx(mine)]
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


# ----------------------------------------------------------------------------- races
async def test_bracket_stop_filling_while_the_exit_is_sized_never_sells_the_owners(
    db, config
) -> None:
    """The bot's bracket stop fills after the executions read that sized the exit, before
    the close reads the position: the close must not sell the owner's shares instead."""
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)  # the owner's buy is journaled
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()  # time stop seen once
    real = h.broker.fills_since
    armed = {"on": True}

    async def read_then_stop_fills(since):
        out = await real(since)
        if armed["on"]:
            armed["on"] = False
            h.broker.set_price("F", 9.7)  # the bracket stop fills right after the read
        return out

    h.broker.fills_since = read_then_stop_fills
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # confirmed: the time stop closes
    h.broker.fills_since = real
    await ticks(h, 3)
    assert h.broker.pos["STK:F"][0] == 50, bot_sells(h, "STK:F")


async def test_owner_buy_inside_the_confirming_tick_is_not_sold(db, config) -> None:
    """The owner buys after the tick read the positions, before its exit is sent."""
    h = await harness(db, config)
    await enter_f(h)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()  # time stop seen once
    real = h.broker.quote

    async def owner_buys_during_quotes(inst):
        q = await real(inst)
        if inst.key == "STK:F" and h.broker.pos.get("STK:F", [0])[0] == 24:
            owner_buys(h, F, 50, 10.0)
        return q

    h.broker.quote = owner_buys_during_quotes
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    h.broker.quote = real
    await ticks(h, 3)
    assert h.broker.pos["STK:F"][0] == 50, bot_sells(h, "STK:F")


async def test_a_refused_sized_close_puts_back_a_stop_for_the_bots_shares_only(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    h.broker.reject_exits = True
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # the time stop's sell is refused; the stop goes back
    stops = [r for r in h.broker.placed if r.side == Side.SELL and r.stop_loss is not None]
    assert stops and stops[-1].qty == 24


async def test_kill_still_closes_everything(db, config) -> None:
    """KILL is the owner's own command: it sells the owner's shares too (SPEC 7.3)."""
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    await h.engine.kill()
    assert "STK:F" not in h.broker.pos


# ----------------------------------------------------------------------------- journal
async def test_the_bots_trade_is_journaled_while_the_owner_still_holds(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 1)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # the time stop sells the bot's 24
    await ticks(h, 3)
    [trade] = db.list_trades()
    assert trade.qty == 24 and trade.entry_price == pytest.approx(10.01)
    assert db.get_position_meta("STK:F") is None
    assert h.broker.pos["STK:F"][0] == 50 and bot_sells(h, "STK:F") == [24]
    owner_sells(h, F, 50, 10.4)
    await ticks(h, 3)
    assert len(db.list_trades()) == 1  # the owner's round trip is not the bot's


async def test_a_restart_between_the_bots_exit_and_the_owners_sale_adds_no_trade(
    db, config
) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 1)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    await ticks(h, 2)
    assert len(db.list_trades()) == 1
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    owner_sells(h, F, 50, 10.4)
    await ticks(h, 3)
    assert len(db.list_trades()) == 1


async def test_the_owner_selling_everything_closes_the_bots_trade_at_his_price(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 1)
    for oid in h.broker.working("STK:F", "stop") + h.broker.working("STK:F", "target"):
        await h.broker.cancel_order(oid)
    owner_sells(h, F, 74, 10.4)
    await ticks(h, 3)
    [trade] = db.list_trades()
    assert trade.qty == 24 and trade.exit_price == pytest.approx(10.4)
    assert db.get_position_meta("STK:F") is None


# ----------------------------------------------------------------------------- review 36/37
def _stops(h: H, key: str = "STK:F") -> list[float]:
    return [h.broker.orders[oid].request.qty for oid in h.broker.working(key, "stop")]


async def _f_with_owner(db, config) -> H:
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.0)
    await ticks(h, 1)
    return h


async def test_an_owner_sale_during_a_capped_close_has_the_bot_guarded_next_tick(
    db, config
) -> None:
    """The owner's own TWS stop sells his 50 while the bot's close cancels its stop: the
    close, sized again, sells nothing. The bot's 24 are looked at again on the next tick
    (stopped or sold), not left bare for CLOSE_RETRY."""
    h = await _f_with_owner(db, config)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()  # time stop seen once
    real = h.broker.cancel_order
    armed = {"on": True}

    async def cancel_then_owner_sells(oid: str) -> None:
        await real(oid)
        if armed["on"]:
            armed["on"] = False
            owner_sells(h, F, 50, 9.95)

    h.broker.cancel_order = cancel_then_owner_sells
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    h.broker.cancel_order = real
    await ticks(h, 2, seconds=15)  # well inside CLOSE_RETRY
    assert "STK:F" not in h.broker.pos or _stops(h) == [24]


async def test_an_approved_close_asks_for_and_journals_the_bots_part(db, config) -> None:
    h = await _f_with_owner(db, config)
    h.db.save_settings(h.db.get_settings().model_copy(update={"autopilot": False}))
    h.engine.reload_settings()
    h.clock.advance(minutes=5)
    h.decider.decisions = [
        hold("F").model_copy(update={"action": TradeAction.CLOSE, "rationale": "fading"})
    ]
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    assert pending.request.qty == 24
    await h.engine.approve_order(pending.id)
    await ticks(h, 2)
    row = h.db.get_order(pending.id)
    assert (row.request.qty, row.filled_qty) == (24, 24)
    assert h.broker.pos["STK:F"][0] == 50


async def test_a_bot_sale_and_an_equal_owner_buy_between_ticks_are_told_apart(db, config) -> None:
    """The bracket stop sells the bot's 24 and the owner buys 24 before the journal sees
    either: the journal alone reads 24 held as the bot's. The executions say otherwise."""
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()  # time stop seen once
    h.broker._fill(h.broker.working("STK:F", "stop")[0], 9.8)
    owner_buys(h, F, 24, 10.0)
    await ticks(h, 4)
    assert bot_sells(h, "STK:F") == [] and h.broker.pos["STK:F"][0] == 24


async def test_a_failed_executions_read_never_sizes_from_a_stale_journal(db, config) -> None:
    """Time stop pending; the bracket stop sells the bot's 24 and the owner buys 50, and
    the executions read fails: the journal (bot 24, nothing of the owner's) cannot say whose
    the 50 are. Nothing is sold until it can."""
    h = await harness(db, config)
    await enter_f(h)
    await ticks(h, 1)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    owner_buys(h, F, 50, 10.0)
    h.broker._fill(h.broker.working("STK:F", "stop")[0], 9.8)
    real = h.broker.fills_since

    async def failing(since: datetime) -> list[Fill]:
        raise BrokerError("IBKR executions timed out after 10s")

    h.broker.fills_since = failing
    h.clock.advance(seconds=15)
    with contextlib.suppress(BrokerError):
        await h.engine.monitor_tick()
    h.broker.fills_since = real
    await ticks(h, 2)
    assert bot_sells(h, "STK:F") == [] and h.broker.pos["STK:F"][0] == 50


def _watchdog_sells(
    h: H, meta: dict, qty: float, *, filled: float | None = None, record: bool = True
) -> None:
    """The crypto watchdog (another client) sells `qty` of the bot's BTC, as it records it
    (`record` False: an older watchdog, its audit row only)."""
    from trader.ownership import note_watchdog_sale
    from trader.watchdog import position_marker

    sold = qty if filled is None else filled
    h.broker.pos["CRYPTO:BTC"][0] -= sold
    h.broker.fills.append(
        Fill(exec_id=f"WD{next(_ids)}", broker_order_id=f"perm:{next(_ids)}", instrument=_BTC,
             side=Side.SELL, qty=sold, price=58900.0, commission=1.0, ts=h.clock())
    )  # fmt: skip
    marker = position_marker(meta)
    if filled is None and record:
        note_watchdog_sale(h.db, "CRYPTO:BTC", marker, "77", qty, h.clock())
    h.db.audit("watchdog_close", {"instrument": "CRYPTO:BTC", "kind": "stop", "qty": qty,
               "filled": sold, "status": "submitted" if filled is None else "cancelled",
               "marker": marker}, actor="watchdog")  # fmt: skip


@pytest.mark.parametrize("owner_journaled", [True, False])
async def test_a_watchdog_sale_ibkr_no_longer_reports_still_came_out_of_the_bots(
    db, config, owner_journaled: bool
) -> None:
    """The watchdog sold the bot's BTC overnight next to the owner's (bought before the app
    went down, or while it was down); the app comes back the next day, when IBKR no longer
    reports that sale. The owner's coins are never sold."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))  # Wed 21:00
    mine = h.broker.pos["CRYPTO:BTC"][0]
    meta = db.get_position_meta("CRYPTO:BTC")
    if owner_journaled:
        owner_buys(h, _BTC, 0.5, 60000.0)
        await ticks(h, 2, seconds=15)
    h.clock.t = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)  # the app is down
    if not owner_journaled:
        owner_buys(h, _BTC, 0.5, 60000.0)
    h.clock.t = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
    _watchdog_sells(h, meta, mine)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    engine = _restart_next_morning(h, config)
    for _ in range(8):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == []
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


async def test_a_watchdog_ioc_sale_cancelled_with_a_fill_came_out_of_the_bots(db, config) -> None:
    """An older watchdog's IOC sell filled 60% and came back cancelled (audit row only):
    that part is the bot's, so the app sells only the bot's other 40%."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 15, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    owner_buys(h, _BTC, 0.5, 60000.0)
    await ticks(h, 1, seconds=15)
    meta = db.get_position_meta("CRYPTO:BTC")
    h.clock.advance(minutes=5)  # the app is down
    _watchdog_sells(h, meta, mine, filled=round(mine * 0.6, 8))
    h.broker.set_price("BTC", 58000.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    await ticks(h, 10, seconds=15)
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


async def test_a_watchdog_sale_long_back_in_the_audit_still_counts(db, config) -> None:
    """An older watchdog (audit row only, no sales record) sold the bot's BTC overnight next
    to the owner's; the app comes back the next day after more than 1000 other audit rows.
    That sale is still the bot's, so the owner's BTC is never sold."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    meta = db.get_position_meta("CRYPTO:BTC")
    h.clock.t = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)  # the app is down
    owner_buys(h, _BTC, 0.5, 60000.0)
    h.clock.t = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
    _watchdog_sells(h, meta, mine, record=False)
    for _ in range(1200):
        db.audit("login", {}, actor="owner")
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    engine = _restart_next_morning(h, config)
    for _ in range(8):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == []
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


async def test_an_unjournaled_bot_entry_next_to_the_owners_btc_keeps_its_stop(db, config) -> None:
    """The bot's BTC entry filled while the app was down on an earlier day (never
    journaled); the owner buys 0.5 today. The units no fill explains fit the bot's entry:
    they are the bot's, and its stop still sells them (only them)."""
    h = await _hold_btc(db, config, datetime(2026, 10, 29, 1, 0, tzinfo=UTC))
    mine = h.broker.pos["CRYPTO:BTC"][0]
    db._exec("DELETE FROM fills")
    engine = _restart_next_morning(h, config)
    owner_buys(h, _BTC, 0.5, 60000.0)
    h.broker.set_price("BTC", 58900.0, asset_class=AssetClass.CRYPTO, spread=10.0)
    for _ in range(8):
        await engine.monitor_tick()
        h.clock.advance(seconds=15)
    assert bot_sells(h, "CRYPTO:BTC") == [pytest.approx(mine)]
    assert h.broker.pos["CRYPTO:BTC"][0] == pytest.approx(0.5)


async def test_a_restart_in_a_symbol_the_owner_traded_journals_the_bots_next_trades(
    db, config
) -> None:
    h = await harness(db, config, mode=Mode.PAPER)
    await enter_f(h)
    await ticks(h, 1)
    owner_buys(h, F, 50, 10.2)
    await ticks(h, 1)
    h.clock.advance(minutes=46)
    await h.engine.monitor_tick()
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # the time stop sells the bot's 24
    await ticks(h, 3)
    owner_sells(h, F, 50, 10.4)
    await ticks(h, 3)
    assert len(db.list_trades()) == 1
    losses = db.get_day("2026-10-21").losses
    h.engine = Engine(config, db, h.broker, h.decider, notifier=h.notes, clock=h.clock,
                      scanner=h.scanner)  # fmt: skip
    await ticks(h, 2)
    h.decider.decisions = [buy()]
    await h.engine.run_cycle()  # the bot enters F again, no owner involved
    h.decider.decisions = []
    await ticks(h, 2)
    h.broker.set_price("F", 9.7)  # its bracket stop fills
    await ticks(h, 5)
    h.broker.set_price("F", 10.0)
    h.clock.advance(minutes=20)
    h.decider.decisions = [buy()]
    await h.engine.run_cycle()  # and once more
    h.decider.decisions = []
    await ticks(h, 3)
    assert len(db.list_trades()) == 2
    assert db.get_day("2026-10-21").losses == losses + 1
