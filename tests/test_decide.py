from datetime import UTC, datetime, timedelta

import pytest

from trader.models import (
    AccountSnapshot,
    AssetClass,
    BotSettings,
    Candidate,
    DayStats,
    DecisionContext,
    Instrument,
    Mode,
    OrderType,
    Position,
    RiskLimits,
    TradeAction,
)
from trader.strategy.decide import DecisionMaker, RuleBasedDecisionMaker, reward_risk

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # Wednesday 11:00 New York


def instrument(symbol: str) -> Instrument:
    crypto = symbol in {"BTC", "ETH"}
    return Instrument(symbol=symbol, asset_class=AssetClass.CRYPTO if crypto else AssetClass.STOCK)


def cand(
    symbol: str,
    score: float = 0.7,
    price: float = 100.0,
    stop: float = 98.0,
    target: float = 104.0,
    setup: str = "vwap_reclaim",
    **features: float,
) -> Candidate:
    return Candidate(
        instrument=instrument(symbol),
        setup=setup,
        score=score,
        price=price,
        suggested_stop=stop,
        suggested_target=target,
        features=features,
    )


def pos(
    symbol: str,
    qty: float = 1.0,
    avg: float = 100.0,
    last: float | None = None,
    setup: str | None = "vwap_reclaim",
    stop: float | None = 98.0,
    target: float | None = 104.0,
) -> Position:
    return Position(
        instrument=instrument(symbol),
        qty=qty,
        avg_cost=avg,
        market_price=last if last is not None else avg,
        unrealized_pnl=((last or avg) - avg) * qty,
        stop_loss=stop,
        take_profit=target,
        opened_at=NOW - timedelta(minutes=30),
        setup=setup,
    )


def make_ctx(
    *,
    candidates: list[Candidate] | None = None,
    positions: list[Position] | None = None,
    market: dict[str, dict[str, float]] | None = None,
    mode: Mode = Mode.PAPER,
    stock_market_open: bool = True,
    data_delayed: bool = False,
    day: DayStats | None = None,
    limits: RiskLimits | None = None,
    equity: float = 500.0,
) -> DecisionContext:
    return DecisionContext(
        now=NOW,
        mode=mode,
        account=AccountSnapshot(
            account_id="DU123",
            mode=mode,
            equity=equity,
            cash=equity,
            settled_cash=equity,
            buying_power=equity,
            day_trades_remaining=3,
        ),
        positions=positions or [],
        candidates=candidates or [],
        day=day or DayStats(day="2026-10-21", start_equity=500.0),
        limits=limits or RiskLimits(),
        market=market or {},
        stock_market_open=stock_market_open,
        data_delayed=data_delayed,
    )


def test_reward_risk():
    assert reward_risk(100, 98, 104) == pytest.approx(2.0)
    assert reward_risk(100, 101, 104) is None  # stop above entry
    assert reward_risk(100, 98, 99) is None  # target below entry
    assert reward_risk(100, 0, 104) is None  # non-positive level
    assert reward_risk(100, 98, float("inf")) is None


def test_rule_maker_satisfies_protocol():
    maker: DecisionMaker = RuleBasedDecisionMaker()
    assert maker.name == "rules"


async def test_buys_highest_score_candidate_with_its_levels():
    candidates = [
        cand("AMD", score=0.6, price=160, stop=158, target=164),
        cand("PLTR", score=0.8, price=40, stop=39.5, target=41.2, setup="orb_breakout"),
        cand("F", score=0.4, price=11, stop=10.9, target=11.3),
    ]
    ctx = make_ctx(candidates=candidates)
    batch = await RuleBasedDecisionMaker().decide(ctx)

    assert batch.maker == "rules"
    assert batch.model is None
    assert batch.candidates == candidates
    assert batch.ts == NOW
    assert len(batch.decisions) == 1
    d = batch.decisions[0]
    assert d.action == TradeAction.BUY
    assert (d.symbol, d.asset_class) == ("PLTR", AssetClass.STOCK)
    assert d.order_type == OrderType.LIMIT
    assert (d.limit_price, d.stop_loss, d.take_profit) == (40, 39.5, 41.2)
    assert d.setup == "orb_breakout"
    assert d.conviction == pytest.approx(0.8)
    assert d.size_fraction == 1.0
    assert "buy PLTR" in batch.market_view


async def test_respects_min_candidate_score_from_settings():
    ctx = make_ctx(candidates=[cand("AMD", score=0.6)])
    strict = RuleBasedDecisionMaker(BotSettings(llm_min_candidate_score=0.65))
    batch = await strict.decide(ctx)
    assert batch.decisions == []
    assert "min score 0.65" in batch.market_view
    assert (await RuleBasedDecisionMaker().decide(ctx)).decisions  # default 0.5


async def test_tie_break_is_deterministic():
    ctx = make_ctx(
        candidates=[
            cand("QQQ", score=0.7, price=100, stop=98, target=104),  # R:R 2
            cand("AAPL", score=0.7, price=100, stop=98, target=106),  # R:R 3
            cand("AMD", score=0.7, price=100, stop=98, target=106),  # R:R 3, loses on key order
        ]
    )
    for _ in range(3):
        batch = await RuleBasedDecisionMaker().decide(ctx)
        assert batch.decisions[0].symbol == "AAPL"


@pytest.mark.parametrize(
    ("candidate", "why"),
    [
        (cand("AMD", price=100, stop=98, target=102.5), "reward:risk 1.25 < 1.5"),
        (cand("AMD", price=100, stop=101, target=104), "stop above entry"),
        (cand("AMD", price=0.8, stop=0.75, target=0.9), "below min price"),
    ],
)
async def test_skips_invalid_candidates(candidate, why):
    batch = await RuleBasedDecisionMaker().decide(make_ctx(candidates=[candidate]))
    assert batch.decisions == [], why


async def test_skips_already_held_instrument():
    ctx = make_ctx(
        candidates=[cand("AMD", score=0.9), cand("F", score=0.6, price=11, stop=10.9, target=11.3)],
        positions=[pos("AMD")],
    )
    batch = await RuleBasedDecisionMaker().decide(ctx)
    assert [d.symbol for d in batch.decisions] == ["F"]


async def test_stock_candidates_skipped_when_market_closed_but_crypto_allowed():
    candidates = [
        cand("AMD", score=0.9),
        cand("BTC", score=0.6, price=65000, stop=64000, target=67000),
    ]
    batch = await RuleBasedDecisionMaker().decide(
        make_ctx(candidates=candidates, stock_market_open=False)
    )
    assert [(d.symbol, d.asset_class) for d in batch.decisions] == [("BTC", AssetClass.CRYPTO)]


async def test_live_delayed_data_blocks_stock_entries():
    candidates = [cand("AMD", score=0.9)]
    live = make_ctx(candidates=candidates, mode=Mode.LIVE, data_delayed=True)
    assert (await RuleBasedDecisionMaker().decide(live)).decisions == []
    paper = make_ctx(candidates=candidates, mode=Mode.PAPER, data_delayed=True)
    assert (await RuleBasedDecisionMaker().decide(paper)).decisions


@pytest.mark.parametrize(
    ("kwargs", "note"),
    [
        (
            {"day": DayStats(day="2026-10-21", halted=True, halt_reason="daily loss limit")},
            "halted",
        ),
        ({"day": DayStats(day="2026-10-21", trades=10)}, "10 entries today"),
        ({"positions": [pos("SPY"), pos("QQQ")]}, "2 open positions"),
    ],
)
async def test_no_entry_when_blocked(kwargs, note):
    ctx = make_ctx(candidates=[cand("AMD", score=0.9)], **kwargs)
    batch = await RuleBasedDecisionMaker().decide(ctx)
    assert all(d.action != TradeAction.BUY for d in batch.decisions)
    assert note in batch.market_view


@pytest.mark.parametrize("setup", ["vwap_reclaim", "orb_breakout"])
async def test_closes_vwap_setups_below_vwap(setup):
    ctx = make_ctx(
        positions=[pos("AMD", setup=setup)],
        market={"STK:AMD": {"price": 99.5, "vwap": 100.0}},
    )
    batch = await RuleBasedDecisionMaker().decide(ctx)
    assert len(batch.decisions) == 1
    d = batch.decisions[0]
    assert (d.action, d.symbol, d.order_type, d.setup) == (
        TradeAction.CLOSE,
        "AMD",
        OrderType.MARKET,
        setup,
    )
    assert "below VWAP" in d.rationale
    assert "close AMD" in batch.market_view


async def test_keeps_vwap_setup_above_vwap_and_ignores_rsi_reversion():
    ctx = make_ctx(
        positions=[pos("AMD", setup="vwap_reclaim"), pos("F", setup="rsi_reversion", avg=11)],
        market={
            "STK:AMD": {"price": 100.5, "vwap": 100.0},
            "STK:F": {"price": 10.5, "vwap": 11.0},  # below VWAP, but reversion setups ignore it
        },
    )
    assert (await RuleBasedDecisionMaker().decide(ctx)).decisions == []


async def test_closes_broken_trend_pullback():
    broken = {"price": 97.0, "ema9": 98.0, "ema21": 99.0}
    intact = {"price": 97.0, "ema9": 99.5, "ema21": 99.0}  # dipped under EMA21, trend still up
    for snap, expected in ((broken, 1), (intact, 0)):
        ctx = make_ctx(positions=[pos("AMD", setup="trend_pullback")], market={"STK:AMD": snap})
        assert len((await RuleBasedDecisionMaker().decide(ctx)).decisions) == expected


async def test_uses_position_price_when_snapshot_has_no_price():
    ctx = make_ctx(positions=[pos("AMD", last=99.0)], market={"STK:AMD": {"vwap": 100.0}})
    batch = await RuleBasedDecisionMaker().decide(ctx)
    assert [d.action for d in batch.decisions] == [TradeAction.CLOSE]


async def test_no_stock_exit_while_market_closed():
    ctx = make_ctx(
        positions=[pos("AMD"), pos("BTC", avg=65000, setup="vwap_reclaim")],
        market={
            "STK:AMD": {"price": 99.0, "vwap": 100.0},
            "CRYPTO:BTC": {"price": 64000.0, "vwap": 64500.0},
        },
        stock_market_open=False,
    )
    batch = await RuleBasedDecisionMaker().decide(ctx)
    assert [(d.action, d.symbol) for d in batch.decisions] == [(TradeAction.CLOSE, "BTC")]


async def test_exit_and_entry_in_same_cycle():
    ctx = make_ctx(
        candidates=[cand("PLTR", score=0.75, price=40, stop=39.5, target=41.2)],
        positions=[pos("AMD")],
        market={"STK:AMD": {"price": 99.0, "vwap": 100.0}},
    )
    batch = await RuleBasedDecisionMaker().decide(ctx)
    assert [(d.action, d.symbol) for d in batch.decisions] == [
        (TradeAction.CLOSE, "AMD"),
        (TradeAction.BUY, "PLTR"),
    ]
