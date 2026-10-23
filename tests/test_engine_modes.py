"""Engine: decision modes (consensus / claude / grok / rules) and their fallbacks, consensus
through the decision cycle, quote sanity with two-tick confirmation, the broker account
change guard and market-data warnings."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from tests.test_engine import (
    NOW,
    FakeBroker,
    FakeDecisionMaker,
    H,
    buy,
    cand,
    enter_f,
    harness,
    hold,
)
from trader.config import AppConfig
from trader.db import Database
from trader.engine import ACCOUNT_KEY, Engine, _mask, build_engine
from trader.models import (
    AIDecision,
    AssetClass,
    BotSettings,
    Instrument,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    TradeAction,
)
from trader.strategy.consensus import ConsensusDecisionMaker
from trader.strategy.decide import RuleBasedDecisionMaker


# =========================================================================== wiring
def make_config(tmp_path, *, claude: bool, grok: bool) -> AppConfig:
    return AppConfig(
        _env_file=None,
        data_dir=tmp_path,
        broker="sim",
        anthropic_api_key=SecretStr("sk-ant-test-not-real") if claude else None,
        xai_api_key=SecretStr("xai-test-not-real") if grok else None,
        xai_model="grok-4.7",
        xai_price_input=2.5,
        ntfy_url=None,
    )


@pytest.mark.parametrize(
    ("claude", "grok", "mode", "maker", "warning"),
    [
        (True, True, "consensus", "consensus", None),
        (True, False, "consensus", "claude", "no xAI key is set: Claude decides alone"),
        (False, True, "consensus", "grok", "no Anthropic key is set: Grok decides alone"),
        (False, False, "consensus", "rules", "neither is set: rules-only decisions"),
        (True, True, "claude", "claude", None),
        (True, True, "grok", "grok", None),
        (True, True, "rules", "rules", None),
        (False, True, "claude", "rules", "Claude mode needs an Anthropic API key"),
        (True, False, "grok", "rules", "Grok mode needs an xAI API key"),
        (False, False, "rules", "rules", None),
    ],
)
def test_build_engine_decision_modes_and_fallbacks(
    db: Database, tmp_path, claude: bool, grok: bool, mode: str, maker: str, warning: str | None
) -> None:
    db.save_settings(BotSettings(decision_mode=mode))
    engine = build_engine(make_config(tmp_path, claude=claude, grok=grok), db)
    assert engine.decider.name == maker
    status = engine.status()
    assert status.decision_maker == maker
    if warning is None:
        assert status.decision_warning is None
    else:
        assert warning in (status.decision_warning or "")


def test_build_engine_wires_consensus_from_the_same_makers(db: Database, tmp_path) -> None:
    from trader.strategy.claude import ClaudeDecisionMaker
    from trader.strategy.grok import GrokDecisionMaker

    engine = build_engine(make_config(tmp_path, claude=True, grok=True), db)
    consensus = engine.decider
    assert isinstance(consensus, ConsensusDecisionMaker)
    assert isinstance(consensus.primary, ClaudeDecisionMaker)
    assert isinstance(consensus.secondary, GrokDecisionMaker)
    assert consensus.primary.fallback is engine.rules is consensus.secondary.fallback
    assert consensus.secondary.model == "grok-4.7"
    assert consensus.secondary.price.input == 2.5  # prices come from the config
    # Settings reach the Claude maker inside consensus too (no restart needed).
    settings = db.get_settings()
    settings.llm_model, settings.llm_effort = "claude-sonnet-5-5", "high"
    db.save_settings(settings)
    engine.reload_settings()
    assert (consensus.primary.model, consensus.primary.effort) == ("claude-sonnet-5-5", "high")


def test_decision_mode_is_honoured_after_a_settings_reload(db: Database, tmp_path) -> None:
    engine = build_engine(make_config(tmp_path, claude=True, grok=True), db)
    assert engine.decider.name == "consensus"
    for mode in ("grok", "claude", "rules", "consensus"):
        db.save_settings(BotSettings(decision_mode=mode))
        engine.reload_settings()
        assert engine.decider.name == mode
        assert engine.status().decision_maker == mode
    db.save_settings(BotSettings(llm_enabled=False))  # the master switch wins
    engine.reload_settings()
    status = engine.status()
    assert status.decision_maker == "rules" and status.decision_warning is None


# =========================================================================== consensus cycle
async def consensus_harness(
    db: Database,
    config: AppConfig,
    claude: list[AIDecision],
    grok: list[AIDecision],
    **kw: Any,
) -> tuple[H, FakeDecisionMaker, FakeDecisionMaker]:
    h = await harness(db, config, **kw)
    first, second = FakeDecisionMaker("claude", cost=0.03), FakeDecisionMaker("grok", cost=0.01)
    first.decisions, second.decisions = claude, grok
    rules = RuleBasedDecisionMaker(db.get_settings())
    makers = {
        "rules": rules,
        "claude": first,
        "grok": second,
        "consensus": ConsensusDecisionMaker(first, second),
    }
    engine = Engine(
        config,
        db,
        h.broker,
        rules,
        notifier=h.notes,
        clock=h.clock,
        scanner=h.scanner,  # type: ignore[arg-type]
        makers=makers,
    )
    return dataclasses.replace(h, engine=engine), first, second


async def test_consensus_buy_when_both_agree(db: Database, config: AppConfig) -> None:
    h, claude, grok = await consensus_harness(db, config, [buy()], [buy(size_fraction=0.25)])
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.maker == "consensus"
    assert len(claude.calls) == len(grok.calls) == 1
    [order] = [o for o in h.orders() if o.request.side == Side.BUY]
    assert order.status == OrderStatus.FILLED
    # Grok's smaller size_fraction wins: 0.25 x $10 risk = $2.50 / $0.21 per share = 11
    assert order.request.qty == 11
    assert h.day().llm_cost == pytest.approx(0.04)  # both calls count towards the budget
    [journaled] = db.list_decisions()
    assert journaled.maker == "consensus" and journaled.model == "fake-model+fake-model"
    assert set(journaled.votes) == {"claude", "grok"}
    assert journaled.votes["grok"][0].size_fraction == 0.25
    assert db.list_signals()[0].taken


@pytest.mark.parametrize(
    ("grok", "skip"),
    [
        ([hold("F")], "consensus: Grok said HOLD"),
        ([], "consensus: Grok did not BUY"),
        ([buy("T")], "consensus: Grok did not BUY"),
    ],
)
async def test_consensus_disagreement_is_logged_in_the_signal_ledger(
    db: Database, config: AppConfig, grok: list[AIDecision], skip: str
) -> None:
    h, _, _ = await consensus_harness(db, config, [buy()], grok)
    await h.engine.run_cycle()
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    [sig] = db.list_signals()
    assert not sig.taken and sig.skip_reason == skip


async def test_consensus_close_from_one_model_is_enough(db: Database, config: AppConfig) -> None:
    h, claude, grok = await consensus_harness(db, config, [buy()], [buy()])
    await enter_f(h)
    assert h.broker.pos
    close = AIDecision(
        action=TradeAction.CLOSE,
        symbol="F",
        asset_class=AssetClass.STOCK,
        conviction=0.9,
        order_type=OrderType.MARKET,
        rationale="momentum gone",
    )
    claude.decisions, grok.decisions = [hold("F")], [close]
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert h.broker.pos == {}
    assert any(o.request.reason == "AI close: Grok: momentum gone" for o in h.orders())


async def test_ai_close_never_sells_a_position_the_bot_did_not_open(
    db: Database, config: AppConfig
) -> None:
    """ENG-3: the owner holds 3 T that the bot never opened (no positions_meta). One model's
    CLOSE vote must not sell it, and that holding alone is not worth an LLM call."""
    h, claude, grok = await consensus_harness(db, config, [hold("F")], [hold("F")])
    h.scanner.candidates = {}
    h.broker.pos["STK:T"] = [3.0, 18.0]
    h.broker.cash -= 54
    close = AIDecision(
        action=TradeAction.CLOSE,
        symbol="T",
        asset_class=AssetClass.STOCK,
        conviction=0.6,
        order_type=OrderType.MARKET,
        rationale="setup unknown",
    )
    claude.decisions, grok.decisions = [hold("T")], [close]
    batch = await h.engine.run_cycle()
    assert batch is not None and "no candidate worth an LLM call" in batch.market_view
    assert not claude.calls and not grok.calls

    h.scanner.candidates = {"STK:F": [cand()]}  # now the models are asked
    h.clock.advance(minutes=5)
    await h.engine.run_cycle()
    assert claude.calls and grok.calls
    assert h.broker.pos["STK:T"][0] == 3
    assert not [r for r in h.broker.placed if r.side == Side.SELL]
    [blocked] = [o for o in h.orders(OrderStatus.BLOCKED) if o.request.instrument.symbol == "T"]
    assert blocked.request.side == Side.SELL and "not opened by the bot" in blocked.message


async def test_budget_spent_in_consensus_mode_never_lets_rules_buy(
    db: Database, config: AppConfig
) -> None:
    h, claude, grok = await consensus_harness(
        db, config, [buy()], [hold("F")], risk={"llm_daily_budget_usd": 0.04}
    )
    await h.engine.run_cycle()  # $0.03 + $0.01: the budget is now spent
    assert h.day().llm_cost == pytest.approx(0.04)
    h.clock.advance(minutes=31)  # past the ledger's dedupe window: a new signal row
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.maker == "rules"
    assert "LLM budget reached" in batch.market_view
    assert "consensus mode: rules manage exits only" in batch.market_view
    assert not [d for d in batch.decisions if d.action == TradeAction.BUY]
    assert not [r for r in h.broker.placed if r.side == Side.BUY]
    assert len(claude.calls) == 1 and len(grok.calls) == 1
    newest = db.list_signals()[0]
    assert newest.skip_reason == "LLM budget; consensus mode: no entries without both models"


async def test_budget_spent_in_single_model_mode_falls_back_to_full_rules(
    db: Database, config: AppConfig
) -> None:
    h, claude, _ = await consensus_harness(
        db, config, [hold("F")], [], risk={"llm_daily_budget_usd": 0.03}, decision_mode="claude"
    )
    await h.engine.run_cycle()
    h.clock.advance(minutes=5)
    batch = await h.engine.run_cycle()
    assert batch is not None and batch.maker == "rules"
    assert [r for r in h.broker.placed if r.side == Side.BUY]  # as before: rules trade


# =========================================================================== quote sanity
async def test_implausible_quotes_are_ignored_for_marks_stops_and_halts(
    db: Database, config: AppConfig
) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    for bid in (5.0, 10.5):  # far below last, then crossed above the ask
        h.broker.set_price("F", 10.0, bid=bid)
        for _ in range(3):
            h.clock.advance(seconds=15)
            await h.engine.monitor_tick()
        pos = h.engine.snapshot()["positions"][0]
        assert pos.market_price == 10.0  # the broker's own mark, not the bad bid
        assert h.broker.pos and not h.day().halted


async def test_engine_side_stop_needs_two_consecutive_ticks(
    db: Database, config: AppConfig
) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    for price in (9.70, 10.0, 9.70):  # breach, recovery (resets), breach
        h.broker.set_price("F", price)
        h.clock.advance(seconds=15)
        await h.engine.monitor_tick()
        assert h.broker.pos
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()  # second consecutive breach
    assert h.broker.pos == {}


async def test_halt_condition_blocks_entries_until_confirmed_or_gone(
    db: Database, config: AppConfig
) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)
    calls = len(h.decider.calls)
    h.broker.set_price("F", 8.50)  # -7.4%: over the 6% daily loss limit
    h.clock.advance(minutes=5)
    assert await h.engine.run_cycle() is None  # first tick: being confirmed, no decisions
    assert len(h.decider.calls) == calls and not h.day().halted
    h.broker.set_price("F", 10.0)  # it was a blip
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    assert h.engine._halt_pending is None and not h.day().halted
    h.clock.advance(minutes=5)
    assert await h.engine.run_cycle() is not None
    assert len(h.decider.calls) == calls + 1


async def test_approval_blocked_while_a_halt_is_being_confirmed(
    db: Database, config: AppConfig
) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    h.engine._halt_pending = "daily loss limit: test"
    result = await h.engine.approve_order(pending.id)
    assert result.status == OrderStatus.BLOCKED
    assert "halt condition being confirmed" in result.message
    assert not h.broker.placed


# =========================================================================== account changes
def switch_account(broker: FakeBroker, account_id: str, cash: float | None = None) -> None:
    original = broker.account

    async def account():
        snap = await original()
        return snap.model_copy(update={"account_id": account_id})

    broker.account = account  # type: ignore[method-assign]
    if cash is not None:
        broker.cash = cash


async def test_first_account_is_remembered(db: Database, config: AppConfig) -> None:
    h = await harness(db, config)
    await h.engine.monitor_tick()
    assert db.get_kv(ACCOUNT_KEY) == "FAKE"
    assert not any(a["action"] == "account_changed" for a in db.list_audit())


async def test_account_change_starts_a_new_journal_epoch(db: Database, config: AppConfig) -> None:
    h = await harness(db, config, native=False)
    await enter_f(h)  # the bot opened F on the first account
    assert db.get_position_meta("STK:F") is not None
    stale = db.insert_order(
        OrderRecord(
            request=OrderRequest(
                instrument=Instrument(symbol="T"), side=Side.BUY, qty=1, limit_price=20.0
            ),
            status=OrderStatus.SUBMITTED,
            broker_order_id="O1",
            created_at=NOW,
        )
    )
    waiting = db.insert_order(
        OrderRecord(
            request=OrderRequest(
                instrument=Instrument(symbol="T"), side=Side.BUY, qty=1, limit_price=20.0
            ),
            status=OrderStatus.PENDING_APPROVAL,
            created_at=NOW + timedelta(minutes=1),
        )
    )
    db.set_flag("live_armed", True)

    switch_account(h.broker, "DUX123456", cash=900.0)
    h.clock.advance(minutes=2)
    await h.engine.monitor_tick()

    assert db.get_kv(ACCOUNT_KEY) == "DUX123456"
    assert db.get_position_meta("STK:F") is None  # never managed on the new account
    assert db.get_order(stale).status == OrderStatus.CANCELLED
    assert "broker account changed" in db.get_order(stale).message
    assert db.get_order(waiting).status == OrderStatus.CANCELLED
    assert not db.get_flag("live_armed")
    [audit] = [a for a in db.list_audit() if a["action"] == "account_changed"]
    assert audit["actor"] == "engine"
    assert audit["detail"]["from"] == "FAKE" and audit["detail"]["to"] == "DU...456"
    assert audit["detail"]["live_disarmed"] is True
    assert "Broker account changed" in h.notes.titles()
    assert h.day().start_equity == pytest.approx(h.engine.snapshot()["account"].equity)
    # The equity curve restarts with the new account (no jump from the old balance).
    curve = db.list_equity()
    assert curve and all(p["equity"] == pytest.approx(curve[0]["equity"]) for p in curve)
    assert curve[0]["equity"] == pytest.approx(h.engine.snapshot()["account"].equity)
    assert db._one("SELECT COUNT(*) AS n FROM equity")["n"] > len(curve)  # old points kept
    # The old position is left alone: no time stop, no engine stop, even when breached.
    h.broker.set_price("F", 9.0)
    for _ in range(3):
        h.clock.advance(minutes=30)
        await h.engine.monitor_tick()
    assert h.broker.pos


async def test_approval_after_an_account_change_is_cancelled(
    db: Database, config: AppConfig
) -> None:
    h = await harness(db, config, autopilot=False)
    await h.engine.run_cycle()
    [pending] = h.orders(OrderStatus.PENDING_APPROVAL)
    switch_account(h.broker, "U7654321")
    result = await h.engine.approve_order(pending.id)
    assert result.status == OrderStatus.CANCELLED
    assert "account changed" in result.message
    assert not h.broker.placed


@pytest.mark.parametrize(
    ("raw", "masked"),
    [("DUX123456", "DU...456"), ("U1234567", "U...567"), ("SIM-7", "SIM-7"), ("FAKE", "FAKE")],
)
def test_account_ids_are_masked(raw: str, masked: str) -> None:
    assert _mask(raw) == masked


# =========================================================================== data warnings
async def test_broker_data_warning_reaches_status_and_last_error(
    db: Database, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    warning: dict[str, str | None] = {"text": "IBKR error 10197: no market data; no entries for F"}
    monkeypatch.setattr(FakeBroker, "data_warning", property(lambda self: warning["text"]))
    h = await harness(db, config)
    await h.engine.monitor_tick()
    status = h.engine.status()
    assert status.data_warning == warning["text"]
    assert "10197" in (status.last_error or "")
    warning["text"] = None
    h.clock.advance(seconds=15)
    await h.engine.monitor_tick()
    status = h.engine.status()
    assert status.data_warning is None and status.last_error is None


async def test_equity_curve_backfills_from_an_earlier_account_change(
    db: Database, config: AppConfig
) -> None:
    """Journals written before the curve restart existed: the curve starts at the last
    recorded account change, so it does not jump from the simulator's balance."""
    real_now = datetime.now(UTC)  # the audit log is stamped with the real clock
    db.add_equity_point(600.0, ts=real_now - timedelta(hours=2))
    db.audit("account_changed", {"from": "SIM-7", "to": "DU...456"}, actor="engine")
    db.add_equity_point(1_000_000.0, ts=real_now + timedelta(minutes=5))
    await harness(db, config)
    assert [p["equity"] for p in db.list_equity()] == [1_000_000.0]
