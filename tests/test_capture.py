"""Compact capture preserves replay decisions without duplicating historical bars."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.test_research import NOW, F, candidate, frame, settings
from trader.capture import capture_cycle
from trader.db import Database
from trader.models import AccountSnapshot, Bar, DayStats, DecisionBatch, DecisionContext, Mode
from trader.research import ReplayFrame, replay_experiment


def context(observation: ReplayFrame, config):
    return DecisionContext(
        now=observation.ts,
        mode=Mode.PAPER,
        account=AccountSnapshot(
            account_id="DU1",
            mode=Mode.PAPER,
            equity=600,
            cash=600,
            settled_cash=600,
            buying_power=600,
            ts=observation.ts,
        ),
        positions=[],
        candidates=observation.candidates or [],
        day=DayStats(day="2026-10-21", start_equity=600),
        limits=config.risk,
        market=observation.market,
        stock_market_open=True,
    )


def test_capture_omits_history_and_keeps_original_scanner_features():
    db = Database(":memory:")
    config = settings()
    observed = frame(candidates=[candidate()])
    observed.market = {F.key: {"price": 20.0, "atr14": 0.5, "rsi14": 35.0}}
    bars = [
        Bar(
            ts=NOW - timedelta(minutes=5 * (512 - i)),
            open=20,
            high=21,
            low=19,
            close=20,
            volume=1000,
        )
        for i in range(512)
    ]
    batch = DecisionBatch(ts=NOW, maker="rules", candidates=observed.candidates)
    capture_cycle(db, context(observed, config), observed.quotes, {F.key: bars}, batch, config)
    payload = db.list_research_frames()[0]
    assert payload["capture_format"] == "scanner_snapshot_v1"
    assert payload["bars"] == {}
    assert payload["candidates"] == [c.model_dump(mode="json") for c in observed.candidates]
    assert payload["market"] == observed.market
    assert payload["quotes"][F.key] == observed.quotes[F.key].model_dump(mode="json")
    assert len(json.dumps(payload)) < 10_000
    raw = {**payload, "bars": {F.key: [b.model_dump(mode="json") for b in bars]}}
    assert len(json.dumps(payload)) < len(json.dumps(raw)) / 10
    db.close()


@pytest.mark.parametrize("exit_kind", ["time_stop", "breakeven"])
async def test_compact_and_raw_bar_captures_have_same_production_rules_and_exits(exit_kind):
    config = settings()
    if exit_kind == "time_stop":
        raw = [
            frame(candidates=[candidate()]),
            frame(1, price=19.9, decision=False),
            frame(46, price=19.9, decision=False),
            frame(46.25, price=19.9, decision=False),
        ]
    else:
        raw = [
            frame(candidates=[candidate()]),
            frame(1, price=19.9, decision=False),
            frame(2, price=21, decision=False),
            frame(3, price=19.8, decision=False),
        ]
    db = Database(":memory:")
    for observed in raw:
        observed.market = {F.key: {"price": observed.quotes[F.key].last, "atr14": 0.5}}
        observed.bars = {
            F.key: [
                Bar(
                    ts=observed.ts - timedelta(minutes=5),
                    open=20,
                    high=21,
                    low=19,
                    close=20,
                    volume=1000,
                )
            ]
        }
        capture_cycle(
            db,
            context(observed, config),
            observed.quotes,
            observed.bars,
            DecisionBatch(ts=observed.ts, maker="rules"),
            config,
        )
    compact = [ReplayFrame.model_validate(p) for p in db.list_research_frames()]
    for old, new in zip(raw, compact, strict=True):
        new.decision_cycle = old.decision_cycle
    expected = (await replay_experiment(raw, config, arms=("rules",), slippage_bps=0)).arms["rules"]
    result = await replay_experiment(compact, config, arms=("rules",), slippage_bps=0)
    actual = result.arms["rules"]
    assert actual.fills == expected.fills
    assert actual.trades == expected.trades
    assert len(actual.trades) == 1
    assert actual.ending_equity == expected.ending_equity
    assert "historical rescanning is unavailable" in " ".join(result.assumptions)
    db.close()
