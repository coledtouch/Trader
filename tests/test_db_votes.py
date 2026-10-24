"""Database: consensus votes are journaled, and older databases gain the column."""

from __future__ import annotations

import sqlite3

from trader.db import Database
from trader.models import AIDecision, AssetClass, BotSettings, DecisionBatch, TradeAction


def vote(action: TradeAction, symbol: str = "F") -> AIDecision:
    return AIDecision(
        action=action,
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        conviction=0.5,
        rationale="why",
    )


def test_votes_round_trip(db: Database) -> None:
    batch = DecisionBatch(
        maker="consensus",
        model="claude-opus-5-5+grok-4.7",
        decisions=[vote(TradeAction.HOLD)],
        votes={"claude": [vote(TradeAction.BUY)], "grok": [vote(TradeAction.HOLD)]},
        cost_usd=0.05,
    )
    db.insert_decision(batch)
    db.insert_decision(DecisionBatch(maker="rules"))
    rules, consensus = db.list_decisions()
    assert rules.votes == {}
    assert consensus.votes["claude"][0].action == TradeAction.BUY
    assert consensus.votes["grok"][0].action == TradeAction.HOLD
    assert consensus.model == "claude-opus-5-5+grok-4.7"


def test_old_database_gains_the_votes_column(tmp_path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
        " maker TEXT NOT NULL, model TEXT, market_view TEXT, decisions TEXT NOT NULL,"
        " candidates TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,"
        " output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,"
        " refused INTEGER NOT NULL DEFAULT 0, error TEXT)"
    )
    conn.execute(
        "INSERT INTO decisions(ts, maker, decisions, candidates)"
        " VALUES('2026-10-20T15:00:00+00:00', 'claude', '[]', '[]')"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    try:
        [old] = db.list_decisions()
        assert old.maker == "claude" and old.votes == {}
        db.insert_decision(DecisionBatch(maker="consensus", votes={"grok": []}))
        assert db.list_decisions()[0].votes == {"grok": []}
    finally:
        db.close()
    Database(path).close()  # reopening does not try to add the column twice


def test_decision_mode_settings_round_trip(db: Database) -> None:
    assert db.get_settings().decision_mode == "consensus"
    db.save_settings(BotSettings(decision_mode="grok"))
    assert db.get_settings().decision_mode == "grok"
