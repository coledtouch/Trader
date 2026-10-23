"""Helpers shared by the LLM decision makers (Claude, Grok) and the consensus maker.

Both models answer with the same deliberately simple JSON shape (`ModelDecisions`: no
range constraints, so one bad number cannot fail the whole parse). `convert_decisions`
then validates every decision against the cycle's context and converts it to an
`AIDecision`; invalid ones are dropped with a reason. The engine's risk manager re-checks
and sizes everything again before an order is sent.
"""

from __future__ import annotations

import copy
import logging
import math
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from trader.models import (
    HARD_CAPS,
    AIDecision,
    AssetClass,
    Candidate,
    DecisionBatch,
    DecisionContext,
    Instrument,
    OrderType,
    Position,
    RiskLimits,
    TradeAction,
)
from trader.strategy.decide import RR_TOLERANCE, DecisionMaker, reward_risk

log = logging.getLogger(__name__)

MAX_RATIONALE = 400
MAX_MARKET_VIEW = 600
# Conservative characters-per-token used to estimate the cost of a call whose usage was lost.
CHARS_PER_TOKEN = 3
# Batch errors that only report dropped invalid decisions start with this; every other
# error means the maker failed (its batch then carries no BUY).
DROPPED_PREFIX = "dropped "


# ---------------------------------------------------------------------- output schema
class ModelDecision(BaseModel):
    """One decision as a model writes it. Ranges and levels are validated after parsing."""

    action: Literal["BUY", "CLOSE", "HOLD"]
    symbol: str = Field(description="Symbol exactly as in the context, e.g. AAPL or BTC")
    asset_class: Literal["STK", "CRYPTO"]
    conviction: float = Field(description="Confidence in the edge, 0 to 1")
    limit_price: float | None = Field(description="BUY limit in USD; null = current price")
    stop_loss: float | None = Field(description="BUY stop in USD, below entry; else null")
    take_profit: float | None = Field(description="BUY target in USD, above entry; else null")
    size_fraction: float = Field(description="0 to 1 of the maximum allowed size; 1 if not BUY")
    setup: str | None = Field(description="Candidate setup name for BUY, else position setup")
    rationale: str = Field(description="At most two short sentences")


class ModelDecisions(BaseModel):
    market_view: str = Field(description="One or two sentences on conditions")
    decisions: list[ModelDecision]


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON schema in the form strict structured-output APIs expect: every
    `$ref` inlined, no titles, every property required and `additionalProperties: false`
    on every object (nullable fields stay `anyOf [..., null]`)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return walk(copy.deepcopy(defs[name]))
        out = {key: walk(value) for key, value in node.items() if key != "title"}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    return walk(raw)


# ---------------------------------------------------------------------- validation
class DecisionRejected(Exception):
    """A single decision failed validation; the message says why."""


def convert_decisions(
    output: ModelDecisions, ctx: DecisionContext, *, source: str = "model"
) -> tuple[list[AIDecision], list[str]]:
    """Validate a model's decisions against the context. Returns (kept, reasons for drops)."""
    candidates: dict[str, list[Candidate]] = {}
    for cand in ctx.candidates:
        candidates.setdefault(cand.instrument.key, []).append(cand)
    held = {p.instrument.key: p for p in ctx.positions if p.qty > 0}
    kept: list[AIDecision] = []
    dropped: list[str] = []
    acted: set[str] = set()
    for raw in output.decisions:
        label = f"{raw.action} {clip(raw.symbol, 20)}"
        try:
            decision = _convert_one(raw, candidates, held, ctx.limits)
            key = Instrument(symbol=decision.symbol, asset_class=decision.asset_class).key
            if decision.action != TradeAction.HOLD:
                if key in acted:
                    raise DecisionRejected("more than one BUY/CLOSE for this instrument")
                acted.add(key)
        except (DecisionRejected, ValidationError) as exc:
            reason = str(exc) if isinstance(exc, DecisionRejected) else "invalid field values"
            log.warning("dropping %s decision %s: %s", source, label, reason)
            dropped.append(f"{label} ({reason})")
            continue
        kept.append(decision)
    return kept, dropped


def dropped_error(dropped: list[str]) -> str | None:
    """The batch error for decisions that failed validation (None when nothing dropped)."""
    if not dropped:
        return None
    return f"{DROPPED_PREFIX}{len(dropped)} invalid decision(s): " + "; ".join(dropped)


def is_failure(batch: DecisionBatch) -> bool:
    """True when the maker failed or refused (as opposed to dropping some decisions)."""
    return batch.refused or (batch.error is not None and not batch.error.startswith(DROPPED_PREFIX))


def _convert_one(
    raw: ModelDecision,
    candidates: dict[str, list[Candidate]],
    held: dict[str, Position],
    limits: RiskLimits,
) -> AIDecision:
    symbol = raw.symbol.strip().upper()
    if not symbol:
        raise DecisionRejected("empty symbol")
    asset_class = AssetClass(raw.asset_class)
    key = Instrument(symbol=symbol, asset_class=asset_class).key
    action = TradeAction(raw.action)
    rationale = clip(raw.rationale, MAX_RATIONALE)
    if action != TradeAction.BUY:
        pos = held.get(key)
        if action == TradeAction.CLOSE and pos is None:
            raise DecisionRejected("no open position to close")
        conviction = min(max(raw.conviction, 0.0), 1.0) if math.isfinite(raw.conviction) else 0.0
        return AIDecision(
            action=action,
            symbol=symbol,
            asset_class=asset_class,
            conviction=conviction,
            order_type=OrderType.MARKET if action == TradeAction.CLOSE else OrderType.LIMIT,
            setup=pos.setup if pos else None,
            rationale=rationale,
        )

    cands = candidates.get(key)
    if not cands:
        raise DecisionRejected("not in the candidate list")
    if key in held:
        raise DecisionRejected("already holding this instrument")
    if not 0 <= raw.conviction <= 1:
        raise DecisionRejected(f"conviction {raw.conviction} outside 0..1")
    if not 0 < raw.size_fraction <= 1:
        raise DecisionRejected(f"size_fraction {raw.size_fraction} outside (0, 1]")
    cand = next((c for c in cands if c.setup == raw.setup), max(cands, key=lambda c: c.score))
    limit = raw.limit_price
    if limit is not None:
        if not (math.isfinite(limit) and limit > 0):
            raise DecisionRejected(f"invalid limit_price {limit}")
        # Gross-error guard only; the risk manager applies the real collar against the quote.
        max_gap = HARD_CAPS["price_collar_pct"] / 100
        if abs(limit - cand.price) > cand.price * max_gap:
            raise DecisionRejected(f"limit_price {limit} too far from candidate price {cand.price}")
    entry = limit if limit is not None else cand.price
    if raw.stop_loss is None or raw.take_profit is None:
        raise DecisionRejected("BUY needs both stop_loss and take_profit")
    rr = reward_risk(entry, raw.stop_loss, raw.take_profit)
    if rr is None:
        raise DecisionRejected(
            f"invalid levels stop {raw.stop_loss} / entry {entry} / target {raw.take_profit}"
        )
    if rr < limits.min_reward_risk - RR_TOLERANCE:
        raise DecisionRejected(f"reward:risk {rr:.2f} below minimum {limits.min_reward_risk:.2f}")
    return AIDecision(
        action=TradeAction.BUY,
        symbol=symbol,
        asset_class=asset_class,
        conviction=raw.conviction,
        order_type=OrderType.LIMIT,
        limit_price=limit,
        stop_loss=raw.stop_loss,
        take_profit=raw.take_profit,
        size_fraction=raw.size_fraction,
        setup=cand.setup,
        rationale=rationale,
    )


# ---------------------------------------------------------------------- failure helpers
def failed_batch(
    ctx: DecisionContext,
    maker: str,
    model: str | None,
    error: str,
    *,
    refused: bool = False,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    cost: float = 0.0,
) -> DecisionBatch:
    """A batch with no decisions (fail safe), still carrying whatever the call cost."""
    return DecisionBatch(
        ts=ctx.now,
        maker=maker,
        model=model,
        candidates=ctx.candidates,
        input_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        refused=refused,
        error=error,
    )


async def exits_only(
    ctx: DecisionContext,
    *,
    maker: str,
    model: str | None,
    label: str,
    fallback: DecisionMaker,
    error: str,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    cost: float = 0.0,
) -> DecisionBatch:
    """Transient outage: keep the fallback maker's risk-reducing CLOSEs, never its BUYs."""
    fallback_name = fallback.name
    try:
        batch = await fallback.decide(ctx)
    except Exception:
        log.exception("fallback decision maker failed")
        return failed_batch(
            ctx,
            maker,
            model,
            f"{error}; {fallback_name} fallback failed",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            cost=cost,
        )
    exits = [d for d in batch.decisions if d.action == TradeAction.CLOSE]
    return DecisionBatch(
        ts=ctx.now,
        maker=maker,
        model=model,
        market_view=f"{label} unavailable; {fallback_name} exits only, no new entries.",
        decisions=exits,
        candidates=ctx.candidates,
        input_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        error=f"{error}; used {fallback_name} exits only",
    )


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
