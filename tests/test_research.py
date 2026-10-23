"""Research must inherit production mechanics and never manufacture AI evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from trader.db import Database
from trader.models import (
    AIDecision,
    Bar,
    BotSettings,
    Candidate,
    DecisionBatch,
    ExitRules,
    Instrument,
    OrderStatus,
    Quote,
    Side,
    TradeAction,
)
from trader.research import (
    ReplayFrame,
    frames_from_bars,
    load_frames,
    main,
    replay_experiment,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
F = Instrument(symbol="F")
T = Instrument(symbol="T")
NU = Instrument(symbol="NU")


def candidate(inst=F, *, price=20.0, stop=19.0, target=23.0):
    return Candidate(
        instrument=inst,
        setup="rsi_reversion",
        score=0.9,
        price=price,
        suggested_stop=stop,
        suggested_target=target,
    )


def vote(
    inst=F, *, maker="claude", cost=0.01, action=TradeAction.BUY, price=20.0, stop=19.0, target=23.0
):
    return DecisionBatch(
        ts=NOW,
        maker=maker,
        cost_usd=cost,
        decisions=[
            AIDecision(
                action=action,
                symbol=inst.symbol,
                asset_class=inst.asset_class,
                conviction=0.9,
                limit_price=price,
                stop_loss=stop,
                take_profit=target,
                setup="rsi_reversion",
                rationale="captured original opinion",
            )
        ],
    )


def settings(**updates):
    return BotSettings(
        stock_universe=["F", "T", "NU"],
        dynamic_universe=False,
        decision_mode="rules",
        exits=ExitRules(time_stop_minutes=45),
        **updates,
    )


def frame(
    minutes=0,
    *,
    price=20.0,
    candidates=None,
    votes=None,
    decision=True,
    instruments=(F,),
    delayed=False,
    quote_ts=None,
    batch=None,
):
    ts = NOW + timedelta(minutes=minutes)
    quotes = {
        inst.key: Quote(
            instrument=inst, bid=price, ask=price, last=price, ts=quote_ts or ts, delayed=delayed
        )
        for inst in instruments
    }
    return ReplayFrame(
        ts=ts,
        quotes=quotes,
        candidates=candidates or [],
        votes=votes or {},
        decision_cycle=decision,
        batch=batch,
    )


async def test_production_time_stop_waits_for_two_separated_monitor_observations():
    frames = [
        frame(candidates=[candidate()]),
        frame(1, price=19.9, decision=False),  # next observation fills the limit
        frame(46, price=19.9, decision=False),  # first time-stop observation
        frame(46.25, price=19.9, decision=False),  # production confirmation interval
    ]
    report = await replay_experiment(frames, settings(), arms=("rules",), slippage_bps=0)
    result = report.arms["rules"]
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_ts == frames[-1].ts
    assert trade.exit_reason.startswith("time stop: 45 min")
    assert trade.entry_ts == frames[1].ts
    assert result.commissions_usd > 0
    assert result.all_in_pnl_usd == pytest.approx(result.trading_pnl_usd)


async def test_production_breakeven_moves_native_stop_and_closes_on_next_observation():
    frames = [
        frame(candidates=[candidate()]),
        frame(1, price=19.9, decision=False),
        frame(2, price=21.0, decision=False),
        frame(3, price=19.8, decision=False),
    ]
    report = await replay_experiment(frames, settings(), arms=("rules",), slippage_bps=0)
    result = report.arms["rules"]
    assert len(result.trades) == 1
    stop = next(
        o for o in result.orders if o.request.stop_loss is not None and o.request.side == Side.SELL
    )
    assert stop.request.stop_loss == pytest.approx(19.9)
    assert stop.status == OrderStatus.FILLED
    assert result.trades[0].exit_ts == frames[-1].ts


async def test_two_position_whole_share_caps_and_independent_accounts():
    frames = []
    for index, inst in enumerate((F, T, NU)):
        frames.extend(
            [
                frame(
                    index * 2,
                    candidates=[candidate(inst)],
                    instruments=(F, T, NU),
                    votes={"claude": vote(inst), "grok": vote(inst, maker="grok")},
                ),
                frame(index * 2 + 1, price=19.9, instruments=(F, T, NU), decision=False),
            ]
        )
    report = await replay_experiment(frames, settings(), slippage_bps=0)
    for arm in report.arms.values():
        buys = [f for f in arm.fills if f.side == Side.BUY]
        assert len(buys) == 2
        assert all(f.qty == int(f.qty) for f in buys)
        assert all(f.qty * f.price <= 250 for f in buys)
        assert sum(f.qty * f.price + f.commission for f in buys) <= 600
        assert len(arm.open_positions) == 2
    assert report.arms["rules"].known_model_cost_usd == 0
    # The third cycle still legitimately asks about managing the two held positions.
    assert report.arms["claude"].known_model_cost_usd == pytest.approx(0.03)
    assert report.arms["consensus"].known_model_cost_usd == pytest.approx(0.06)
    assert report.arms["rules"].trading_pnl_usd == report.arms["consensus"].trading_pnl_usd


@pytest.mark.parametrize("bad", ["delayed", "stale", "future", "crossed", "missing_ask"])
async def test_invalid_market_observations_never_fill(bad):
    frames = [frame(candidates=[candidate()]), frame(1, price=19.9, decision=False)]
    if bad == "delayed":
        frames[1].quotes[F.key].delayed = True
    elif bad == "stale":
        frames[1].quotes[F.key].ts = NOW
    elif bad == "future":
        frames[1].quotes[F.key].ts = NOW + timedelta(hours=1)
    elif bad == "crossed":
        frames[1].quotes[F.key].bid = 21
    else:
        frames[1].quotes[F.key].ask = None
    report = await replay_experiment(frames, settings(), arms=("rules",), slippage_bps=0)
    assert report.arms["rules"].fills == []
    assert report.arms["rules"].invalid_quote_frames == 1


async def test_delayed_data_cannot_generate_any_arm_entries():
    frames = [
        frame(
            candidates=[candidate()],
            delayed=True,
            votes={"claude": vote(), "grok": vote(maker="grok")},
        ),
        frame(1, price=19.9, decision=False, delayed=True),
    ]
    report = await replay_experiment(frames, settings(), slippage_bps=0)
    assert all(not arm.fills for arm in report.arms.values())


async def test_missing_votes_never_become_model_holds_or_invented_successful_experiments():
    frames = [
        frame(candidates=[candidate()], votes={"claude": vote()}),
        frame(1, price=19.9, decision=False),
    ]
    report = await replay_experiment(frames, settings(), slippage_bps=0)
    assert report.arms["claude"].model_calls_replayed == 1
    assert report.arms["claude"].model_cost_usd == pytest.approx(0.01)
    for name in ("grok", "consensus"):
        result = report.arms[name]
        assert result.missing_vote_cycles == 1
        assert result.all_in_pnl_usd is None
        assert not result.fills
        assert all(p.all_in_equity is None for p in result.equity_curve)


async def test_consensus_keeps_real_merge_rule_and_model_costs():
    hold = vote(maker="grok", cost=0.02, action=TradeAction.HOLD)
    frames = [
        frame(candidates=[candidate()], votes={"claude": vote(), "grok": hold}),
        frame(1, price=19.9, decision=False),
    ]
    report = await replay_experiment(frames, settings(), slippage_bps=0)
    assert len(report.arms["claude"].fills) == 1
    assert not report.arms["grok"].fills
    assert not report.arms["consensus"].fills
    assert report.arms["consensus"].model_cost_usd == pytest.approx(0.03)
    assert report.arms["consensus"].all_in_pnl_usd == pytest.approx(-0.03)


async def test_old_consensus_votes_preserve_unknown_per_model_cost():
    batch = DecisionBatch(
        ts=NOW,
        maker="consensus",
        cost_usd=0.03,
        votes={"claude": vote().decisions, "grok": vote().decisions},
    )
    frames = [frame(candidates=[candidate()], batch=batch), frame(1, price=19.9, decision=False)]
    report = await replay_experiment(frames, settings(), slippage_bps=0)
    assert report.arms["claude"].model_cost_usd is None
    assert report.arms["grok"].model_cost_usd is None
    assert report.arms["consensus"].model_cost_usd is None
    assert "missing" in " ".join(report.arms["claude"].warnings)


async def test_captured_consensus_metadata_preserves_exact_bills_and_individual_failure():
    batch = DecisionBatch(
        ts=NOW,
        maker="consensus",
        cost_usd=0.03,
        refused=True,
        votes={"claude": vote().decisions, "grok": vote().decisions},
        vote_costs={"claude": 0.01, "grok": 0.02},
        vote_failures={"grok": "refused"},
    )
    report = await replay_experiment(
        [frame(candidates=[candidate()], batch=batch), frame(1, price=19.9, decision=False)],
        settings(),
        slippage_bps=0,
    )
    assert len(report.arms["claude"].fills) == 1
    assert not report.arms["grok"].fills
    assert not report.arms["consensus"].fills
    assert report.arms["consensus"].model_cost_usd == pytest.approx(0.03)


@pytest.mark.parametrize("cost", [-0.1, float("nan"), float("inf")])
def test_invalid_model_cost_cannot_become_fictitious_profit(cost):
    with pytest.raises(ValueError, match="costs must be finite and nonnegative"):
        frame(votes={"claude": vote(cost=cost)})


async def test_limit_order_can_remain_unfilled_and_no_forced_final_liquidation():
    frames = [frame(candidates=[candidate()]), frame(1, price=20.1, decision=False)]
    result = (await replay_experiment(frames, settings(), arms=("rules",), slippage_bps=0)).arms[
        "rules"
    ]
    assert result.unfilled_entries == 1
    assert result.fills == []
    assert result.trading_pnl_usd == 0
    result = (
        await replay_experiment(
            [frames[0], frame(1, price=19.9, decision=False)],
            settings(),
            arms=("rules",),
            slippage_bps=0,
        )
    ).arms["rules"]
    assert len(result.open_positions) == 1
    assert not result.trades
    assert "Open positions" in " ".join(result.warnings)


async def test_new_experiment_isolated_from_previous_run_and_original_settings():
    config = settings(autopilot=False)
    frames = [frame(candidates=[candidate()]), frame(1, price=19.9, decision=False)]
    first = await replay_experiment(frames, config, arms=("rules",), slippage_bps=0)
    second = await replay_experiment(frames, config, arms=("rules",), slippage_bps=0)
    assert first.arms["rules"].fills == second.arms["rules"].fills
    assert config.autopilot is False


def test_historical_bars_revealed_only_at_close_and_no_future_bar_data(tmp_path):
    bars = [
        Bar(
            ts=NOW + timedelta(minutes=5 * i),
            open=20,
            high=21,
            low=19,
            close=20 + i / 10,
            volume=1000,
        )
        for i in range(3)
    ]
    config = settings()
    records = frames_from_bars([(F, bars)], config)
    assert records[0].ts == NOW + timedelta(minutes=5)
    assert len(records[0].bars[F.key]) == 1
    assert len(records[-1].bars[F.key]) == 3
    assert records[0].candidates is None
    assert all(r.source == "historical_bars" for r in records)
    path = tmp_path / "capture.jsonl"
    path.write_text("\n".join(r.model_dump_json() for r in records), encoding="utf-8")
    assert load_frames(path) == records


@pytest.mark.parametrize("case", ["empty", "unordered", "mixed_account", "mixed_settings"])
async def test_reject_invalid_capture_cohorts(case):
    frames = [frame(), frame(1)]
    if case == "empty":
        frames = []
    elif case == "unordered":
        frames.reverse()
    elif case == "mixed_account":
        frames[0].account_id, frames[1].account_id = "DU1", "DU2"
    else:
        frames[0].settings, frames[1].settings = settings(), settings(llm_enabled=False)
    with pytest.raises(ValueError):
        await replay_experiment(frames, settings(), arms=("rules",))


def test_cli_csv_uses_production_rules_and_emits_approximation_label(tmp_path, capsys):
    path = tmp_path / "bars.csv"
    path.write_text(
        f"ts,open,high,low,close,volume\n{NOW.isoformat()},20,21,19,20,1000\n", encoding="utf-8"
    )
    assert main(["--csv", f"F={path}"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert list(result["arms"]) == ["rules"]
    assert result["source_types"] == ["historical_bars"]
    assert "inferred" in " ".join(result["warnings"])


def test_cli_rejects_historical_ai_votes_and_missing_database(tmp_path, capsys):
    assert main(["--csv", "F=unused.csv", "--arms", "claude"]) == 2
    assert "no captured AI votes" in capsys.readouterr().err
    assert main(["--database", str(tmp_path / "not-a-journal.db")]) == 2
    assert not (tmp_path / "not-a-journal.db").exists()


def test_cli_database_uses_latest_frozen_cohort_and_saves_dashboard_report(tmp_path, capsys):
    path = tmp_path / "journal.db"
    db = Database(path)
    start = datetime.now(UTC) - timedelta(minutes=3)
    for index, cohort in enumerate(("older", "current", "current")):
        observation = ReplayFrame(
            ts=start + timedelta(minutes=index),
            candidates=[],
            cohort=cohort,
            starting_equity=750,
            commission_plan="fixed",
            settings=settings(),
            account_id="DU1",
        )
        db.add_research_frame(observation.model_dump(mode="json"))
    db.close()
    assert main(["--database", str(path), "--save-report"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["frame_count"] == 2
    assert payload["starting_equity"] == 750
    assert payload["commission_plan"] == "fixed"
    db = Database(path)
    cached = json.loads(db.get_kv("research_report"))
    assert cached["status"] == "ready"
    assert cached["cohort"] == "current"
    assert cached["days"] == 30
    assert cached["report"] == payload
    # Replay journals are isolated: the original database receives no synthetic fills.
    assert db.list_fills() == []
    # A simulator restart can retain the same settings/account cohort; the epoch floor
    # still excludes observations from its previous virtual account.
    db.set_kv("journal_account_since", (start + timedelta(minutes=2)).isoformat())
    db.set_kv("broker_account", "DU1")
    db.close()
    assert main(["--database", str(path), "--arms", "rules"]) == 0
    assert json.loads(capsys.readouterr().out)["frame_count"] == 1
