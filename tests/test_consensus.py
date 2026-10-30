"""ConsensusDecisionMaker: a BUY only when both models BUY the same symbol."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.test_decide import NOW, cand, make_ctx, pos
from trader.models import (
    AIDecision,
    AssetClass,
    DecisionBatch,
    DecisionContext,
    OrderType,
    TradeAction,
)
from trader.strategy.consensus import ConsensusDecisionMaker


# ---------------------------------------------------------------------- fakes
class Maker:
    """A decision maker returning a fixed batch (or raising)."""

    def __init__(
        self,
        name: str,
        decisions: list[AIDecision] | None = None,
        *,
        model: str | None = None,
        cost: float = 0.0,
        error: str | None = None,
        refused: bool = False,
        raises: Exception | None = None,
        view: str = "",
    ) -> None:
        self.name = name
        self.model = model or f"{name}-model"
        self.decisions = decisions or []
        self.cost = cost
        self.error = error
        self.refused = refused
        self.raises = raises
        self.view = view or f"{name} view"
        self.calls = 0

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model=self.model,
            market_view=self.view,
            decisions=list(self.decisions),
            candidates=ctx.candidates,
            input_tokens=1000,
            output_tokens=200,
            cost_usd=self.cost,
            refused=self.refused,
            error=self.error,
        )


def buy(symbol: str = "AMD", **kw: Any) -> AIDecision:
    fields: dict[str, Any] = {
        "action": TradeAction.BUY,
        "symbol": symbol,
        "asset_class": AssetClass.STOCK,
        "conviction": 0.8,
        "limit_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "size_fraction": 1.0,
        "setup": "vwap_reclaim",
        "rationale": f"buy {symbol}",
    }
    fields.update(kw)
    return AIDecision(**fields)


def close(symbol: str = "SPY", why: str = "setup broke") -> AIDecision:
    return AIDecision(
        action=TradeAction.CLOSE,
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        conviction=0.9,
        order_type=OrderType.MARKET,
        setup="vwap_reclaim",
        rationale=why,
    )


def hold(symbol: str = "AMD") -> AIDecision:
    return AIDecision(
        action=TradeAction.HOLD,
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        conviction=0.4,
        rationale="unclear",
    )


def ctx() -> DecisionContext:
    return make_ctx(
        candidates=[cand("AMD"), cand("F", price=10, stop=9.8, target=10.5)],
        positions=[pos("SPY", avg=600)],
    )


async def run(claude: Maker, grok: Maker) -> DecisionBatch:
    return await ConsensusDecisionMaker(claude, grok).decide(ctx())


def actions(batch: DecisionBatch) -> list[tuple[str, str]]:
    return [(d.action.value, d.symbol) for d in batch.decisions]


def reason(batch: DecisionBatch, symbol: str) -> str:
    [d] = [d for d in batch.decisions if d.symbol == symbol and d.action == TradeAction.HOLD]
    return d.rationale


# ---------------------------------------------------------------------- truth table
async def test_both_buy_same_symbol_buys_with_primary_levels_and_smaller_size() -> None:
    claude = Maker("claude", [buy(conviction=0.9, size_fraction=1.0)], cost=0.04)
    grok = Maker(
        "grok",
        [
            buy(
                limit_price=100.5,
                stop_loss=97.0,
                take_profit=106.0,
                conviction=0.6,
                size_fraction=0.5,
            )
        ],
        cost=0.01,
    )
    batch = await run(claude, grok)
    [d] = batch.decisions
    assert (d.action, d.symbol) == (TradeAction.BUY, "AMD")
    assert (d.limit_price, d.stop_loss, d.take_profit, d.setup) == (
        100.0,
        98.0,
        104.0,
        "vwap_reclaim",
    )
    assert d.conviction == 0.6 and d.size_fraction == 0.5  # the smaller of the two
    assert d.rationale == "Claude: buy AMD | Grok: buy AMD"
    assert batch.maker == "consensus"
    assert batch.model == "claude-model+grok-model"
    assert batch.cost_usd == pytest.approx(0.05)  # both calls are paid for
    assert (batch.input_tokens, batch.output_tokens) == (2000, 400)
    assert batch.market_view == "Claude: claude view | Grok: grok view"
    assert set(batch.votes) == {"claude", "grok"}
    assert batch.votes["grok"][0].limit_price == 100.5  # each model's own vote is kept
    assert batch.error is None and not batch.refused
    assert batch.ts == NOW and batch.candidates == ctx().candidates


async def test_buy_and_hold_is_a_hold_naming_the_dissenter() -> None:
    batch = await run(Maker("claude", [buy()]), Maker("grok", [hold()]))
    assert actions(batch) == [("HOLD", "AMD")]
    assert reason(batch, "AMD") == "consensus: Grok said HOLD"
    batch = await run(Maker("claude", [hold()]), Maker("grok", [buy()]))
    assert reason(batch, "AMD") == "consensus: Claude said HOLD"


async def test_buy_without_a_second_vote_is_not_bought() -> None:
    batch = await run(Maker("claude", [buy()]), Maker("grok", []))
    assert actions(batch) == [("HOLD", "AMD")]
    assert reason(batch, "AMD") == "consensus: Grok did not BUY"


async def test_buys_of_different_symbols_buy_nothing() -> None:
    batch = await run(Maker("claude", [buy("AMD")]), Maker("grok", [buy("F", limit_price=10.0,
        stop_loss=9.8, take_profit=10.5)]))  # fmt: skip
    assert actions(batch) == [("HOLD", "AMD"), ("HOLD", "F")]
    assert reason(batch, "AMD") == "consensus: Grok did not BUY"
    assert reason(batch, "F") == "consensus: Claude did not BUY"


async def test_both_hold() -> None:
    batch = await run(Maker("claude", [hold()]), Maker("grok", [hold()]))
    assert reason(batch, "AMD") == "consensus: both said HOLD"
    batch = await run(Maker("claude", [hold()]), Maker("grok", []))
    assert reason(batch, "AMD") == "consensus: Claude said HOLD"


@pytest.mark.parametrize("side", ["claude", "grok", "both"])
async def test_close_from_either_side_closes(side: str) -> None:
    claude = Maker("claude", [close(why="lost VWAP")] if side in ("claude", "both") else [])
    grok = Maker(
        "grok", [close(why="momentum gone")] if side in ("grok", "both") else [hold("SPY")]
    )
    batch = await run(claude, grok)
    [d] = batch.decisions
    assert (d.action, d.symbol, d.order_type) == (TradeAction.CLOSE, "SPY", OrderType.MARKET)
    expected = {
        "claude": "Claude: lost VWAP",
        "grok": "Grok: momentum gone",
        "both": "Claude: lost VWAP | Grok: momentum gone",
    }[side]
    assert d.rationale == expected


async def test_close_and_buy_in_one_batch() -> None:
    batch = await run(Maker("claude", [close(), buy()]), Maker("grok", [buy()]))
    assert actions(batch) == [("CLOSE", "SPY"), ("BUY", "AMD")]  # exits first


# ---------------------------------------------------------------------- failures
async def test_exception_on_one_side_blocks_buys_but_keeps_the_other_sides_closes() -> None:
    claude = Maker("claude", [buy(), close()])
    grok = Maker("grok", raises=RuntimeError("boom"))
    batch = await run(claude, grok)
    assert actions(batch) == [("CLOSE", "SPY"), ("HOLD", "AMD")]
    assert reason(batch, "AMD") == "consensus: Grok failed"
    assert batch.error == "Grok: unexpected error: RuntimeError"
    assert batch.votes["grok"] == []
    assert "Grok: unavailable" in batch.market_view


async def test_refusal_blocks_buys() -> None:
    grok = Maker("grok", refused=True, error="Grok declined the request", view="")
    batch = await run(Maker("claude", [buy()]), grok)
    assert actions(batch) == [("HOLD", "AMD")]
    assert reason(batch, "AMD") == "consensus: Grok refused"
    assert batch.refused is True
    assert batch.error == "Grok: Grok declined the request"


async def test_a_failure_blocks_even_a_matching_buy() -> None:
    # A failed batch should never carry a BUY; if one ever did, it still cannot count.
    grok = Maker("grok", [buy()], error="HTTP 500: overloaded; used rules exits only")
    batch = await run(Maker("claude", [buy()]), grok)
    assert actions(batch) == [("HOLD", "AMD")]
    assert reason(batch, "AMD") == "consensus: Grok failed"


async def test_outage_exits_from_the_failing_side_are_honoured() -> None:
    claude = Maker("claude", [close()], error="RateLimitError: slow down; used rules exits only")
    batch = await run(claude, Maker("grok", [buy()]))
    assert actions(batch) == [("CLOSE", "SPY"), ("HOLD", "AMD")]
    assert reason(batch, "AMD") == "consensus: Claude failed"


async def test_dropped_invalid_decisions_are_not_a_failure() -> None:
    claude = Maker("claude", [buy()], error="dropped 1 invalid decision(s): BUY TSLA (...)")
    batch = await run(claude, Maker("grok", [buy()]))
    assert actions(batch) == [("BUY", "AMD")]
    assert batch.error == "Claude: dropped 1 invalid decision(s): BUY TSLA (...)"


async def test_both_fail_no_decisions() -> None:
    batch = await run(Maker("claude", raises=TimeoutError()), Maker("grok", raises=ValueError("x")))
    assert batch.decisions == []
    assert batch.error == (
        "Claude: unexpected error: TimeoutError; Grok: unexpected error: ValueError"
    )


async def test_cost_is_summed_even_when_one_side_fails() -> None:
    grok = Maker("grok", cost=0.02, error="Grok output was not valid JSON for the decision schema")
    batch = await run(Maker("claude", [buy()], cost=0.05), grok)
    assert batch.cost_usd == pytest.approx(0.07)
    assert not [d for d in batch.decisions if d.action == TradeAction.BUY]


# ---------------------------------------------------------------------- mechanics
async def test_both_makers_run_concurrently() -> None:
    started: list[str] = []
    both = asyncio.Event()

    class Slow(Maker):
        async def decide(self, c: DecisionContext) -> DecisionBatch:
            started.append(self.name)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1.0)  # deadlocks if run one after the other
            return await super().decide(c)

    batch = await run(Slow("claude", [buy()]), Slow("grok", [buy()]))
    assert sorted(started) == ["claude", "grok"]
    assert actions(batch) == [("BUY", "AMD")]


def test_needs_two_different_makers() -> None:
    with pytest.raises(ValueError, match="two different"):
        ConsensusDecisionMaker(Maker("claude"), Maker("claude"))
    maker = ConsensusDecisionMaker(
        Maker("claude", model="claude-opus-5-5"), Maker("grok", model="grok-4.7")
    )
    assert maker.name == "consensus"
    assert maker.model == "claude-opus-5-5+grok-4.7"
