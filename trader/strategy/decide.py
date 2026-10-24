"""Decision makers: the protocol the engine calls every cycle, plus the rule-based maker.

The rule-based maker is deterministic and deliberately conservative. The engine uses
it when the LLM is disabled, over budget or not worth calling, and the Claude maker
uses its exits when the API is unavailable. Whatever a maker returns, the engine's
risk manager re-checks and sizes every decision before an order is sent.
"""

from __future__ import annotations

import math
from typing import Protocol

from trader.models import (
    AIDecision,
    AssetClass,
    BotSettings,
    Candidate,
    DecisionBatch,
    DecisionContext,
    Mode,
    OrderType,
    Position,
    TradeAction,
)

# Absolute tolerance for reward:risk comparisons (prices like 10.10 / 9.95 are inexact floats).
RR_TOLERANCE = 1e-6

# Long setups that are broken once price loses VWAP.
VWAP_SETUPS = frozenset({"vwap_reclaim", "orb_breakout"})
# Long setups that are broken once the short-term trend flips (EMA9 < EMA21, price < EMA21).
TREND_SETUPS = frozenset({"trend_pullback"})


class DecisionMaker(Protocol):
    name: str

    async def decide(self, ctx: DecisionContext) -> DecisionBatch: ...


def reward_risk(entry: float, stop: float, target: float) -> float | None:
    """(target - entry) / (entry - stop) for a long, or None unless 0 < stop < entry < target."""
    levels = (entry, stop, target)
    if not all(math.isfinite(v) and v > 0 for v in levels) or not stop < entry < target:
        return None
    return (target - entry) / (entry - stop)


def _finite(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) and value > 0 else None


class RuleBasedDecisionMaker:
    """BUY the best qualifying scanner candidate; CLOSE positions whose setup has broken.

    `settings` supplies `llm_min_candidate_score`; the engine may replace the attribute
    when the owner edits settings. Everything else comes from the DecisionContext.
    """

    name = "rules"

    def __init__(self, settings: BotSettings | None = None) -> None:
        self.settings = settings or BotSettings()

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        exits = self._exits(ctx)
        entry, entry_note = self._entry(ctx)
        decisions = [*exits, *([entry] if entry else [])]
        notes = [f"close {d.symbol} ({d.rationale})" for d in exits]
        notes.append(entry_note)
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            market_view="Rules: " + "; ".join(notes),
            decisions=decisions,
            candidates=ctx.candidates,
        )

    # ------------------------------------------------------------------ exits
    def _exits(self, ctx: DecisionContext) -> list[AIDecision]:
        out: list[AIDecision] = []
        for pos in sorted(ctx.positions, key=lambda p: p.instrument.key):
            if pos.qty <= 0:
                continue
            if pos.instrument.asset_class == AssetClass.STOCK and not ctx.stock_market_open:
                continue  # stock orders cannot execute now; broker stops still protect it
            reason = _invalidation(pos, ctx.market.get(pos.instrument.key, {}))
            if reason:
                out.append(
                    AIDecision(
                        action=TradeAction.CLOSE,
                        symbol=pos.instrument.symbol,
                        asset_class=pos.instrument.asset_class,
                        conviction=1.0,
                        order_type=OrderType.MARKET,
                        setup=pos.setup,
                        rationale=reason,
                    )
                )
        return out

    # ------------------------------------------------------------------ entries
    def _entry(self, ctx: DecisionContext) -> tuple[AIDecision | None, str]:
        limits = ctx.limits
        held = {p.instrument.key for p in ctx.positions if p.qty > 0}
        if ctx.day.halted:
            return None, f"no entry: trading halted ({ctx.day.halt_reason or 'halted'})"
        if ctx.day.trades >= limits.max_trades_per_day:
            return (
                None,
                f"no entry: {ctx.day.trades} entries today (max {limits.max_trades_per_day})",
            )
        if len(held) >= limits.max_open_positions:
            return None, f"no entry: {len(held)} open positions (max {limits.max_open_positions})"

        min_score = self.settings.llm_min_candidate_score
        stocks_blocked = not ctx.stock_market_open or (ctx.mode == Mode.LIVE and ctx.data_delayed)
        eligible: list[tuple[Candidate, float]] = []
        for cand in ctx.candidates:
            if cand.score < min_score or cand.instrument.key in held:
                continue
            if cand.instrument.asset_class == AssetClass.STOCK and stocks_blocked:
                continue
            if cand.price < limits.min_price:
                continue
            rr = reward_risk(cand.price, cand.suggested_stop, cand.suggested_target)
            if rr is None or rr < limits.min_reward_risk - RR_TOLERANCE:
                continue
            eligible.append((cand, rr))
        if not eligible:
            return None, f"no entry: no candidate passed the filters (min score {min_score:.2f})"

        # Highest score wins; ties go to the better reward:risk, then the instrument key.
        best, rr = min(
            eligible, key=lambda item: (-item[0].score, -item[1], item[0].instrument.key)
        )
        decision = AIDecision(
            action=TradeAction.BUY,
            symbol=best.instrument.symbol,
            asset_class=best.instrument.asset_class,
            conviction=best.score,
            order_type=OrderType.LIMIT,
            limit_price=best.price,
            stop_loss=best.suggested_stop,
            take_profit=best.suggested_target,
            size_fraction=1.0,
            setup=best.setup,
            rationale=f"Best {best.setup} candidate: score {best.score:.2f}, reward:risk {rr:.1f}.",
        )
        return decision, f"buy {best.instrument.symbol} ({best.setup}, score {best.score:.2f})"


def _invalidation(pos: Position, snap: dict[str, float]) -> str | None:
    """Why the position's setup no longer holds, or None while it is intact (or unknown)."""
    price = _finite(snap.get("price")) or _finite(pos.market_price)
    if price is None:
        return None
    if pos.setup in VWAP_SETUPS:
        vwap = _finite(snap.get("vwap"))
        if vwap is not None and price < vwap:
            return f"price {price:.2f} below VWAP {vwap:.2f}, {pos.setup} invalidated"
    elif pos.setup in TREND_SETUPS:
        ema9, ema21 = _finite(snap.get("ema9")), _finite(snap.get("ema21"))
        if ema9 is not None and ema21 is not None and ema9 < ema21 and price < ema21:
            return f"EMA9 below EMA21 and price {price:.2f} below EMA21, trend_pullback invalidated"
    return None
