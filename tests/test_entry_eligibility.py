"""Entry economics and risk must be checked before paid model decisions."""

from datetime import timedelta

import pytest

from tests.test_engine import NOW, buy, cand, enter_f, harness
from tests.test_risk import FeeBroker, account, day, decision, entry
from trader.models import (
    AIDecision,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Quote,
    RiskLimits,
    Side,
    TradeAction,
)
from trader.risk import RiskManager


async def test_fee_ineligible_signals_are_logged_without_paid_calls(db, config) -> None:
    h = await harness(db, config)
    h.scanner.candidates["STK:F"] = [cand(stop=9.99, target=10.05)]
    h.decider.cost = 0.05
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.candidates == []
    assert not h.decider.calls and h.day().llm_cost == 0
    assert not h.broker.placed
    [signal] = db.list_signals()
    assert "round-trip fees" in signal.skip_reason
    assert "expected reward" in signal.skip_reason


async def test_unaffordable_scanner_signals_are_still_journaled(db, config) -> None:
    h = await harness(db, config, stock_universe=["SPY"])
    h.broker.set_price("SPY", 700)
    h.scanner.candidates = {"STK:SPY": [cand("SPY", 700, 695, 715)]}
    await h.engine.run_cycle()
    assert "STK:SPY" in h.scanner.scanned
    assert not h.decider.calls and not h.broker.placed
    [signal] = db.list_signals()
    assert "size rounds to 0" in signal.skip_reason


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"ts": NOW - timedelta(minutes=1)}, "stale"),
        ({"ts": NOW + timedelta(minutes=1)}, "future"),
        ({"bid": 10.02, "ask": 10.01}, "crossed quote"),
        ({"bid": None}, "no two-sided quote"),
        ({"halted": True}, "halted"),
        ({"bid": 9.8, "ask": 10.2}, "spread"),
        ({"ask": float("inf")}, "non-finite ask"),
    ],
)
async def test_bad_quotes_never_trigger_paid_entry_calls(
    db, config, monkeypatch, changes: dict, reason: str
) -> None:
    h = await harness(db, config)
    original = h.broker.quote

    async def quote(inst: Instrument) -> Quote:
        return (await original(inst)).model_copy(update=changes)

    monkeypatch.setattr(h.broker, "quote", quote)
    await h.engine.run_cycle()
    assert not h.decider.calls and not h.broker.placed
    assert reason in db.list_signals()[0].skip_reason
    assert all(not q.valid for q in h.engine.status().quote_health)


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("global_delay", [True, False])
async def test_delayed_feed_or_quote_blocks_every_stock_entry_before_ai(
    db, config, monkeypatch, mode: Mode, global_delay: bool
) -> None:
    h = await harness(db, config, mode=mode)
    if global_delay:
        h.broker.delayed = True
    else:
        original = h.broker.quote

        async def quote(inst: Instrument) -> Quote:
            return (await original(inst)).model_copy(update={"delayed": True})

        monkeypatch.setattr(h.broker, "quote", quote)
    await h.engine.run_cycle()
    assert not h.decider.calls and not h.broker.placed
    assert "delayed market data" in db.list_signals()[0].skip_reason
    status = h.engine.status()
    assert "stock entries are blocked" in status.data_warning
    assert status.quote_health and all(q.delayed for q in status.quote_health)


@pytest.mark.parametrize("blocker", ["held", "working", "position_cap", "pdt", "cash"])
async def test_account_constraints_prevent_paid_calls(db, config, blocker: str) -> None:
    h = await harness(db, config, risk={"max_open_positions": 1})
    if blocker == "held":
        h.broker.pos["STK:F"] = [1, 10]
        reason = "already holding F"
    elif blocker == "working":
        await h.broker.place_order(
            OrderRequest(instrument=Instrument(symbol="F"), side=Side.BUY, qty=1, limit_price=9)
        )
        reason = "already working"
    elif blocker == "position_cap":
        h.broker.pos["STK:T"] = [1, 20]
        reason = "open positions/entries"
    elif blocker == "pdt":
        h.broker.day_trades = 0
        reason = "no day trades remaining"
    else:
        h.broker.cash = 1
        reason = "size rounds to 0"
    await h.engine.run_cycle()
    assert not h.decider.calls
    assert reason in db.list_signals()[0].skip_reason


async def test_disabled_setup_is_logged_and_not_sent_to_ai(db, config) -> None:
    h = await harness(db, config, enabled_setups=["trend_pullback"])
    await h.engine.run_cycle()
    assert not h.decider.calls and not h.broker.placed
    [signal] = db.list_signals()
    assert "setup vwap_reclaim is disabled" in signal.skip_reason


async def test_eligible_candidate_still_reaches_model_when_another_is_blocked(db, config) -> None:
    h = await harness(db, config)
    h.scanner.candidates["STK:T"] = [cand("T", 20, 19.99, 20.05)]
    await h.engine.run_cycle()
    assert [c.instrument.symbol for c in h.decider.calls[0].candidates] == ["F"]
    assert len(db.list_signals()) == 2
    skipped = next(s for s in db.list_signals() if s.instrument.symbol == "T")
    assert "round-trip fees" in skipped.skip_reason


@pytest.mark.parametrize("change", ["delayed", "setup_disabled"])
async def test_final_send_rechecks_changes_during_paid_decision(
    db, config, monkeypatch, change: str
) -> None:
    h = await harness(db, config)
    original = h.decider.decide

    async def decide(ctx):
        batch = await original(ctx)
        if change == "delayed":
            h.broker.delayed = True
        else:
            settings = db.get_settings()
            settings.enabled_setups = []
            db.save_settings(settings)
            h.engine.reload_settings()
        return batch

    monkeypatch.setattr(h.decider, "decide", decide)
    await h.engine.run_cycle()
    assert len(h.decider.calls) == 1
    assert not h.broker.placed
    [order] = h.orders()
    assert order.status == OrderStatus.BLOCKED
    assert ("delayed" if change == "delayed" else "disabled") in order.message


async def test_disabling_setup_invalidates_pending_approval(db, config) -> None:
    h = await harness(db, config, autopilot=False)
    pending = await enter_f(h)
    assert pending.status == OrderStatus.PENDING_APPROVAL
    settings = db.get_settings()
    settings.enabled_setups = []
    db.save_settings(settings)
    h.engine.reload_settings()
    result = await h.engine.approve_order(pending.id)
    assert result.status == OrderStatus.BLOCKED
    assert "setup vwap_reclaim is disabled" in result.message
    assert not h.broker.placed


@pytest.mark.parametrize("filled", [False, True])
async def test_fresh_final_state_prevents_entry_created_during_model_call(
    db, config, monkeypatch, filled: bool
) -> None:
    h = await harness(db, config)
    original = h.decider.decide

    async def decide(ctx):
        assert ctx.candidates and not ctx.positions
        batch = await original(ctx)
        await h.broker.place_order(
            OrderRequest(
                instrument=Instrument(symbol="F"),
                side=Side.BUY,
                qty=1,
                limit_price=10.01 if filled else 9.99,
            )
        )
        return batch

    monkeypatch.setattr(h.decider, "decide", decide)
    await h.engine.run_cycle()
    assert len(h.decider.calls) == 1
    assert len([req for req in h.broker.placed if req.side == Side.BUY]) == 1
    [order] = h.orders()
    assert order.status == OrderStatus.BLOCKED
    assert ("already holding" if filled else "already working") in order.message


async def test_disabled_setup_decision_cannot_be_relabelled_as_an_enabled_setup(db, config) -> None:
    h = await harness(db, config, enabled_setups=["trend_pullback"])
    h.scanner.candidates["STK:F"] = [cand(setup="trend_pullback"), cand()]
    h.decider.decisions = [buy(setup="vwap_reclaim")]
    await h.engine.run_cycle()
    assert len(h.decider.calls) == 1
    assert not h.broker.placed


async def test_journal_account_floor_tracks_sim_restarts_and_initial_paper_bind(db, config) -> None:
    first = await harness(db, config)
    assert db.get_kv("journal_account_since") == NOW.isoformat()
    next_start = NOW + timedelta(hours=1)
    second = await harness(db, config, now=next_start)
    assert db.get_kv("journal_account_since") == next_start.isoformat()
    await first.engine.monitor_tick()
    db.set_kv("broker_account", "OLD")
    await second.engine.monitor_tick()
    assert db.get_kv("journal_account_since") == next_start.isoformat()


async def test_delayed_data_keeps_owned_position_ai_exits_available(db, config) -> None:
    h = await harness(db, config)
    await enter_f(h)
    h.broker.delayed = True
    h.decider.decisions = [
        AIDecision(
            action=TradeAction.CLOSE,
            symbol="F",
            asset_class=buy().asset_class,
            conviction=0.8,
            order_type=OrderType.MARKET,
            rationale="exit",
        )
    ]
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert len(h.decider.calls) == 2
    assert h.decider.calls[-1].candidates == []
    assert h.broker.pos == {}


def test_largest_size_reserves_working_notional_and_commissions() -> None:
    # Without the working order this permits 24 shares. With it, only four fit cash;
    # the working order's fee and the new order's fee make the fifth unaffordable.
    order = OrderRecord(
        request=entry(Instrument(symbol="T"), qty=10, limit=10),
        status=OrderStatus.SUBMITTED,
    )
    risk = RiskManager(RiskLimits())
    qty, notes = risk.size_entry(
        decision=decision(),
        entry=10,
        stop=9.8,
        account=account(500, cash=150),
        positions=[],
        open_orders=[order],
        day=day(),
        broker=FeeBroker(),
        allow_fractional=False,
    )
    assert qty == 4
    assert "available cash $49.65" in notes[-1]


def test_largest_size_reserves_working_exposure() -> None:
    order = OrderRecord(
        request=entry(Instrument(symbol="T"), qty=10, limit=20),
        status=OrderStatus.SUBMITTED,
    )
    risk = RiskManager(RiskLimits(max_total_exposure_pct=50))
    qty, notes = risk.size_entry(
        decision=decision(),
        entry=10,
        stop=9.8,
        account=account(),
        positions=[],
        open_orders=[order],
        day=day(),
        broker=FeeBroker(),
        allow_fractional=False,
    )
    assert qty == 5
    assert "exposure room $50.00" in notes[-1]


async def test_working_market_buy_with_unknown_commitment_blocks_paid_entry(db, config) -> None:
    h = await harness(db, config)
    working = OrderRecord(
        request=OrderRequest(
            instrument=Instrument(symbol="T"), side=Side.BUY, qty=10, order_type=OrderType.MARKET
        ),
        status=OrderStatus.SUBMITTED,
        broker_order_id="manual",
    )
    h.broker.orders["manual"] = working
    h.broker.kind["manual"] = "entry"
    h.broker.parent["manual"] = None
    await h.engine.run_cycle()
    assert not h.decider.calls
    assert "cash commitment is unknown" in db.list_signals()[0].skip_reason


async def test_removed_watchlist_quote_cannot_leave_a_delayed_warning(
    db, config, monkeypatch
) -> None:
    h = await harness(db, config)
    original = h.broker.quote

    async def quote(inst: Instrument) -> Quote:
        return (await original(inst)).model_copy(update={"delayed": inst.symbol == "F"})

    monkeypatch.setattr(h.broker, "quote", quote)
    await h.engine.run_cycle()
    assert "F" in h.engine.status().data_warning
    settings = db.get_settings()
    settings.stock_universe = ["T"]
    db.save_settings(settings)
    h.engine.reload_settings()
    status = h.engine.status()
    assert [q.symbol for q in status.quote_health] == ["T"]
    assert status.data_warning is None


async def test_removed_symbol_keeps_held_quote_until_position_closes(
    db, config, monkeypatch
) -> None:
    h = await harness(db, config, dynamic_universe=True)
    h.broker.cash = 490
    h.broker.pos["STK:F"] = [1, 10]
    original = h.broker.quote

    async def quote(inst: Instrument) -> Quote:
        return (await original(inst)).model_copy(update={"delayed": inst.symbol == "F"})

    monkeypatch.setattr(h.broker, "quote", quote)
    await h.engine.monitor_tick()
    settings = db.get_settings()
    settings.stock_universe = ["T"]
    db.save_settings(settings)
    h.engine.reload_settings()
    assert [q.symbol for q in h.engine.status().quote_health] == ["F"]
    assert "F" in h.engine.status().data_warning
    h.broker.pos.clear()
    h.broker.cash = 500
    await h.engine.monitor_tick()
    assert h.engine.status().quote_health == []
    assert h.engine.status().data_warning is None


async def test_dynamic_quotes_expire_and_disappear_when_mover_is_removed(
    db, config, monkeypatch
) -> None:
    h = await harness(db, config, stock_universe=["T"], dynamic_universe=True)
    h.broker.movers = ["F"]
    original = h.broker.quote

    async def quote(inst: Instrument) -> Quote:
        return (await original(inst)).model_copy(update={"delayed": inst.symbol == "F"})

    monkeypatch.setattr(h.broker, "quote", quote)
    await h.engine.run_cycle()
    assert {q.symbol for q in h.engine.status().quote_health} == {"F", "T"}
    h.clock.advance(seconds=2 * h.engine.settings.cycle_seconds + 1)
    assert [q.symbol for q in h.engine.status().quote_health] == ["T"]
    assert h.engine.status().data_warning is None
    await h.engine.run_cycle()  # the current dynamic scan includes F again
    assert "F" in h.engine.status().data_warning
    h.broker.movers = []
    await h.engine.run_cycle()
    assert [q.symbol for q in h.engine.status().quote_health] == ["T"]
    assert h.engine.status().data_warning is None


async def test_disabling_asset_class_clears_its_unheld_quote_cache(db, config) -> None:
    h = await harness(db, config)
    h.scanner.candidates = {}
    await h.engine.run_cycle()
    assert len(h.engine.status().quote_health) == 2
    settings = db.get_settings()
    settings.trade_stocks = False
    db.save_settings(settings)
    h.engine.reload_settings()
    assert h.engine.status().quote_health == []


async def test_paper_switch_allows_delayed_entries_and_tags_the_trade(db, config) -> None:
    h = await harness(db, config, mode=Mode.PAPER, risk={"allow_delayed_paper_entries": True})
    h.broker.delayed = True
    await h.engine.run_cycle()
    assert h.decider.calls and h.broker.placed  # the AI was asked and the entry went out
    status = h.engine.status()
    assert "paper entries are allowed" in (status.data_warning or "")
    assert status.delayed_paper_entries is True
    h.broker.set_price("F", 9.0)  # through the stop: the trade closes at a loss
    for _ in range(3):
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    [trade] = db.list_trades(limit=5)
    assert trade.delayed_data is True


async def test_live_ignores_the_paper_delayed_switch(db, config) -> None:
    h = await harness(db, config, mode=Mode.LIVE, risk={"allow_delayed_paper_entries": True})
    h.broker.delayed = True
    await h.engine.run_cycle()
    assert not h.broker.placed
    assert "delayed market data" in db.list_signals()[0].skip_reason


def _crypto_candidate(h) -> None:
    from trader.models import AssetClass, Candidate

    btc = Instrument(symbol="BTC", asset_class=AssetClass.CRYPTO)
    h.broker.set_price("BTC", 100.0, asset_class=AssetClass.CRYPTO)
    h.scanner.candidates = {
        btc.key: [
            Candidate(
                instrument=btc,
                setup="trend_pullback",
                score=0.8,
                price=100.0,
                suggested_stop=98.0,
                suggested_target=104.0,
            )
        ]
    }
    h.decider.decisions = [
        AIDecision(
            action=TradeAction.BUY,
            symbol="BTC",
            asset_class=AssetClass.CRYPTO,
            conviction=0.8,
            setup="trend_pullback",
            rationale="pullback",
        )
    ]


async def test_crypto_trades_on_a_delayed_feed_are_not_tagged(db, config) -> None:
    """Review finding: crypto entries were never blocked on delayed data, so they must not
    be tagged as delayed-data paper trades (and dropped from the setup statistics)."""
    from trader.models import AssetClass

    h = await harness(db, config, mode=Mode.PAPER, trade_crypto=True, trade_stocks=False)
    h.broker.delayed = True
    _crypto_candidate(h)
    await h.engine.run_cycle()
    assert [r for r in h.broker.placed if r.side == Side.BUY]
    h.broker.set_price("BTC", 97.0, asset_class=AssetClass.CRYPTO)
    for _ in range(4):
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
    [trade] = db.list_trades(limit=5)
    assert trade.delayed_data is False
    assert h.engine.status().delayed_paper_entries is False  # the switch is off
