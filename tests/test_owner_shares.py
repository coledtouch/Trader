"""The owner adds to a symbol the bot holds: no bot exit ever sells the owner's shares,
and the bot's own position keeps its protection and management."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_engine import (
    _BTC,
    NOW,
    H,
    _hold_btc,
    _part,
    _resting_entry,
    enter_f,
    harness,
    hold,
)
from trader.broker.base import BrokerError
from trader.models import (
    AssetClass,
    Fill,
    Instrument,
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
    # The bot holds nothing of F now: the dashboard's Close refuses the owner's shares.
    import pytest

    with pytest.raises(ValueError):
        await h.engine.close_position("STK:F")
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


def test_bot_holding_attribution() -> None:
    from trader.ownership import bot_holding, entry_qty, sell_cap

    since = NOW
    B, S = Side.BUY, Side.SELL
    # the owner adds 50 to the bot's 24
    h = bot_holding([_f(B, 24, 1), _f(B, 50, 2, True)], since, watchdog_sold=False)
    assert (h.bot, h.owner_bought) == (24, 50)
    assert sell_cap(74, 24, h) == 24
    # the owner's round trip leaves the bot's 24
    h = bot_holding([_f(B, 24, 1), _f(B, 10, 2, True), _f(S, 10, 3, True)], since,
                    watchdog_sold=False)  # fmt: skip
    assert h.bot == 24 and sell_cap(24, 24, h) == 24
    # the owner sells 5 of the bot's (he bought none): the bot holds the 19 left
    h = bot_holding([_f(B, 24, 1), _f(S, 5, 2, True)], since, watchdog_sold=False)
    assert h.bot == 19 and sell_cap(19, 24, h) is None  # nothing of the owner's: sell all
    # the owner sells before he buys: the sale came out of the bot's shares
    h = bot_holding([_f(B, 24, 1), _f(S, 5, 2, True), _f(B, 5, 3, True)], since,
                    watchdog_sold=False)  # fmt: skip
    assert h.bot == 19 and sell_cap(24, 24, h) == 19
    # once the watchdog sold, another client's sell comes out of the bot's first
    h = bot_holding([_f(B, 24, 1), _f(B, 50, 2, True), _f(S, 24, 3, True)], since,
                    watchdog_sold=True)  # fmt: skip
    assert h.bot == 0 and sell_cap(50, 24, h) == 0
    # more held than the bot's entry bought: the owner's, even with no owner fill journaled
    h = bot_holding([_f(B, 24, 1)], since, watchdog_sold=False)
    assert sell_cap(30, 24, h) == 24
    # held within the entry and no owner fill: the bot's (e.g. its fills not reported)
    assert sell_cap(24, 24, bot_holding([], since, watchdog_sold=False)) is None
    # fills before the entry was sent are not this position's
    h = bot_holding([_f(B, 50, -10, True), _f(B, 24, 1)], since, watchdog_sold=False)
    assert h.owner_bought == 0
    # entry quantity: recorded, or from the planned risk of an older row
    assert entry_qty({"qty": 24}, {}) == 24
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
