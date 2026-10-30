from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_web import build, login
from trader.models import (
    BotSettings,
    DayStats,
    DecisionBatch,
    EngineStatus,
    Instrument,
    OperatingCosts,
    SignalRecord,
    TradeRecord,
)
from trader.performance import monthly_fraction, performance_report
from trader.web.app import loosened_fields

NOW = datetime(2026, 10, 23, 20, tzinfo=UTC)


def trade(pnl=8.0, at=NOW, setup="trend_pullback"):
    return TradeRecord(
        instrument=Instrument(symbol="F"),
        qty=10,
        entry_price=10,
        exit_price=11,
        entry_ts=at - timedelta(hours=1),
        exit_ts=at,
        pnl=pnl,
        commission=2,
        setup=setup,
    )


def test_all_in_subtracts_external_costs_without_double_counting_commission(db):
    db.upsert_day(DayStats(day="2026-10-23", start_equity=600, end_equity=608))
    db.insert_trade(trade())
    db.insert_decision(DecisionBatch(ts=NOW, maker="claude", cost_usd=2))
    settings = BotSettings(
        operating_costs=OperatingCosts(hosting_monthly_usd=31, data_monthly_usd=0)
    )
    status = EngineStatus(day="2026-10-23", mode="paper", equity=608)
    report = performance_report(db, settings, status, NOW)
    assert report["trading_pnl"] == 8
    assert report["hosting_cost"] == pytest.approx(16 / 24, abs=0.0001)
    assert report["all_in_pnl"] == pytest.approx(6 - 16 / 24, abs=0.0001)
    assert report["net_expectancy"] == 8
    assert report["setup_stats"][0]["trades"] == 1


def test_unknown_expenses_and_unfilled_signals_cannot_claim_profit(db):
    db.upsert_day(DayStats(day="2026-10-23", start_equity=600, end_equity=600))
    db.insert_signal(
        SignalRecord(
            ts=NOW,
            instrument=Instrument(symbol="F"),
            setup="vwap_reclaim",
            score=0.7,
            price=10,
            stop=9,
            target=12,
            taken=True,
            evaluated=True,
            r_close=5,
        )
    )
    report = performance_report(db, BotSettings(), EngineStatus(mode="paper"), NOW)
    assert report["all_in_pnl"] is None
    assert report["costs_configured"] is False
    assert report["closed_trades"] == 0
    assert report["net_expectancy"] is None
    assert report["trading_pnl"] == 0
    assert report["signal_count"] == 1
    assert report["setup_stats"] == []


def test_cash_flow_adjusted_days_and_account_epoch_do_not_mix_returns(db):
    db.upsert_day(DayStats(day="2026-10-22", start_equity=1000000, end_equity=999000))
    # A deposit is already added to the day's baseline by the production engine.
    db.upsert_day(DayStats(day="2026-10-23", start_equity=700, end_equity=705))
    db.set_kv("journal_account_since", "2026-10-23T04:00:00+00:00")
    db.insert_trade(trade(at=NOW - timedelta(days=1), pnl=-1000))
    db.insert_trade(trade(pnl=5))
    report = performance_report(db, BotSettings(), EngineStatus(mode="paper"), NOW)
    assert report["trading_pnl"] == 5
    assert report["closed_trades"] == 1
    assert report["max_drawdown_pct"] == 0


def test_daily_drawdown_uses_continuous_pnl_instead_of_deposit_size(db, monkeypatch):
    db.upsert_day(DayStats(day="2026-10-22", start_equity=600, end_equity=660))
    db.upsert_day(DayStats(day="2026-10-23", start_equity=1660, end_equity=1594))
    monkeypatch.setattr("trader.db.utcnow", lambda: NOW - timedelta(hours=1))
    db.audit("cash_flow", {"amount": 1000})
    report = performance_report(db, BotSettings(), EngineStatus(mode="paper"), NOW)
    assert report["trading_pnl"] == -6
    assert report["max_drawdown_pct"] == 10
    assert report["drawdown_basis"] == "daily equity observations"


def test_overnight_position_gap_is_not_erased_by_new_day_baseline(db):
    db.upsert_day(DayStats(day="2026-10-22", start_equity=600, end_equity=600))
    db.upsert_day(DayStats(day="2026-10-23", start_equity=550, end_equity=550))
    settings = BotSettings(
        operating_costs=OperatingCosts(hosting_monthly_usd=0, data_monthly_usd=0)
    )
    report = performance_report(db, settings, EngineStatus(mode="paper"), NOW)
    assert report["trading_pnl"] == -50
    assert report["all_in_pnl"] == -50
    assert report["max_drawdown_pct"] == pytest.approx(100 * 50 / 600, abs=0.0001)


def test_monthly_costs_include_weekends_and_calendar_month_lengths():
    assert (
        monthly_fraction(datetime(2026, 10, 1, 4, tzinfo=UTC), datetime(2026, 11, 1, 4, tzinfo=UTC))
        == 1
    )


def test_setup_uncertainty_requires_multiple_days_not_many_clustered_entries(db):
    for _ in range(40):
        db.insert_trade(trade())
    report = performance_report(db, BotSettings(), EngineStatus(), NOW)
    setup = report["setup_stats"][0]
    assert setup["days_observed"] == 1
    assert setup["confidence_low"] is None
    assert "exploratory" in setup["evidence"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_expense_settings_reject_invalid_amounts(value):
    with pytest.raises(ValidationError):
        OperatingCosts(hosting_monthly_usd=value)


def test_setup_enable_is_step_up_but_disabling_is_not():
    enabled = BotSettings()
    disabled = enabled.model_copy(update={"enabled_setups": []})
    assert "enabled_setups" in loosened_fields(disabled, enabled, live=False)
    assert "enabled_setups" not in loosened_fields(enabled, disabled, live=True)
    with pytest.raises(ValidationError):
        BotSettings(enabled_setups=["made_up"])


def test_performance_and_research_remain_authenticated_and_replay_needs_csrf():
    ctx = build()
    try:
        with TestClient(ctx.app) as client:
            ctx.client = client
            for path in ("/api/performance", "/api/research"):
                assert client.get(path).status_code == 401
            headers = login(ctx)
            assert client.get("/api/performance").status_code == 200
            assert client.get("/api/research").json()["status"] == "not_run"
            assert client.post("/api/research/run", json={}).status_code == 403
            assert client.post("/api/research/run", json={}, headers=headers).status_code == 409
            assert client.get("/api/performance?days=9999").status_code == 422
    finally:
        ctx.db.close()


def test_consensus_cost_metadata_survives_journal_round_trip(db):
    db.insert_decision(
        DecisionBatch(
            ts=NOW,
            maker="consensus",
            vote_costs={"claude": 0.02, "grok": 0.03},
            vote_failures={"grok": "refused"},
        )
    )
    restored = db.list_decisions()[0]
    assert restored.vote_costs == {"claude": 0.02, "grok": 0.03}
    assert restored.vote_failures == {"grok": "refused"}


def test_dashboard_replay_uses_only_virtual_accounts_and_periods_do_not_mix():
    from tests.test_research import candidate, frame

    ctx = build()
    try:
        with TestClient(ctx.app) as client:
            ctx.client = client
            headers = login(ctx)
            for observed in (
                frame(candidates=[candidate()]),
                frame(1, price=19.9, decision=False),
                frame(46, price=20.1, decision=False),
                frame(47, price=20.1, decision=False),
            ):
                payload = observed.model_dump(mode="json")
                payload.update(cohort="frozen-test", settings=BotSettings().model_dump(mode="json"))
                ctx.db.add_research_frame(payload)
            response = client.post("/api/research/run", json={"days": 30}, headers=headers)
            assert response.status_code == 202
            comparison = client.get("/api/research?days=30").json()
            assert comparison["status"] == "ready", comparison
            assert set(comparison["report"]["arms"]) == {"rules", "claude", "grok", "consensus"}
            assert ctx.db.list_orders() == []
            assert ctx.db.list_fills() == []
            assert ctx.engine.cycles == []
            assert client.get("/api/research?days=7").json()["report"] is None
    finally:
        ctx.db.close()


def test_research_status_reads_indexed_metadata_without_loading_payloads(monkeypatch):
    ctx = build()
    try:
        ctx.db.add_research_frame({"ts": NOW.isoformat(), "cohort": "frozen", "quotes": {}})

        def forbidden(*args, **kwargs):
            raise AssertionError("status polling must never decode observation payloads")

        monkeypatch.setattr(ctx.db, "list_research_frames", forbidden)
        monkeypatch.setattr(ctx.db, "research_frames_in_window", forbidden)
        with TestClient(ctx.app) as client:
            ctx.client = client
            login(ctx)
            payload = client.get("/api/research").json()
            assert payload["frame_count"] == 1
            assert payload["status"] == "not_run"
    finally:
        ctx.db.close()


def test_metadata_cohort_does_not_recombine_separate_experiments(db):
    for index, cohort in enumerate(("a", "a", "b", "a", "a")):
        db.add_research_frame(
            {
                "ts": (NOW + timedelta(minutes=index)).isoformat(),
                "account_id": "PAPER",
                "cohort": cohort,
            }
        )
    window = db.research_window(NOW - timedelta(days=1), "PAPER")
    assert window["frame_count"] == 2
    assert window["first_id"] == 4
    assert window["last_id"] == 5
    assert db.research_window(NOW, "OTHER") is None


def test_delayed_data_trades_are_kept_out_of_setup_stats(db):
    """Paper trades entered on delayed data test the plumbing, not the edge."""
    db.upsert_day(DayStats(day="2026-10-23", start_equity=600, end_equity=615))
    db.insert_trade(trade(pnl=8.0))
    db.insert_trade(trade(pnl=7.0).model_copy(update={"delayed_data": True}))
    stored = db.list_trades(limit=10)
    assert sorted(t.delayed_data for t in stored) == [False, True]
    settings = BotSettings()
    settings.risk.allow_delayed_paper_entries = True
    status = EngineStatus(day="2026-10-23", mode="paper", equity=615, data_delayed=True)
    report = performance_report(db, settings, status, NOW)
    assert report["closed_trades"] == 2 and report["delayed_data_trades"] == 1
    assert report["net_expectancy"] == 8.0  # the delayed-data trade is not edge evidence
    assert [(s["setup"], s["trades"], s["net_pnl"]) for s in report["setup_stats"]] == [
        ("trend_pullback", 1, 8.0)
    ]
    assert any("entered on delayed data" in w for w in report["warnings"])
    assert any("paper entries are allowed" in w for w in report["warnings"])


def test_old_trades_table_gains_the_delayed_column(tmp_path):
    import sqlite3

    from trader.db import Database

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, instrument TEXT NOT NULL,"
        " qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,"
        " entry_ts TEXT NOT NULL, exit_ts TEXT NOT NULL, pnl REAL NOT NULL,"
        " commission REAL NOT NULL DEFAULT 0, setup TEXT, decision_id INTEGER,"
        " r_multiple REAL, exit_reason TEXT NOT NULL DEFAULT '')"
    )
    old.execute(
        "INSERT INTO trades(instrument, qty, entry_price, exit_price, entry_ts, exit_ts, pnl)"
        " VALUES('STK:F', 10, 10, 11, '2026-10-22T14:00:00+00:00',"
        " '2026-10-22T15:00:00+00:00', 8)"
    )
    old.commit()
    old.close()
    db = Database(path)
    try:
        [kept] = db.list_trades(limit=5)
        assert kept.pnl == 8 and kept.delayed_data is False
        db.insert_trade(trade().model_copy(update={"delayed_data": True}))
        assert sum(t.delayed_data for t in db.list_trades(limit=5)) == 1
    finally:
        db.close()
