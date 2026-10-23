"""Claude decision maker: one Anthropic Messages API call per cycle with structured output.

The stable system prompt is cached; the rendered context goes in the user message.
Claude's answer is parsed into a deliberately simple pydantic model (no range
constraints in the schema, so one bad number cannot fail the whole parse), then every
decision is validated here and converted to an AIDecision. Invalid decisions are
dropped and logged. Failure handling (fail safe, never a BUY on a failure):

- refusal (`stop_reason == "refusal"`): `refused=True`, no decisions, no fallback.
- cut off, unparseable output, 4xx errors, unexpected exceptions: `error` set, no decisions.
- transient API errors (429, 408/409, 5xx/overloaded, connection or timeout) that remain
  after the SDK's own retries: the fallback maker's CLOSE decisions only (exits that
  reduce risk; its BUYs are discarded), with `error` set. Broker-side stops protect
  positions in every case.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import anthropic
from anthropic import AsyncAnthropic
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
from trader.strategy.prompts import SYSTEM_PROMPT, render_context

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16000  # thinking counts towards this as well as the JSON answer
REQUEST_TIMEOUT_S = 90.0
MAX_RETRIES = 2
# Server-side refusal fallback: the API re-runs a declined request on the model
# Anthropic recommends for the refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TRANSIENT_STATUS = frozenset({408, 409, 429})
MAX_RATIONALE = 400
MAX_MARKET_VIEW = 600
# Conservative characters-per-token used to estimate the cost of a call whose usage was lost.
CHARS_PER_TOKEN = 3


# ---------------------------------------------------------------------- pricing
@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache writes (the default ephemeral TTL used here)


PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, 0.01, 0.125),
    # Likely targets of the server-side refusal fallback.
    "claude-opus-5": ModelPrice(5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 0.50, 6.25),
}


def model_price(model: str) -> ModelPrice:
    """Exact match, else the longest table key the id starts with (dated ids), else Opus 5.5."""
    if model in PRICES:
        return PRICES[model]
    matches = [key for key in PRICES if model.startswith(key + "-")]
    return PRICES[max(matches, key=len)] if matches else PRICES[DEFAULT_MODEL]


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """USD cost of one API attempt. `input_tokens` excludes cached tokens, as in `usage`."""
    p = model_price(model)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
    ) / 1_000_000


def usage_cost(usage: Any, model: str) -> tuple[int, int, float]:
    """(prompt tokens incl. cached, output tokens, USD) for a response's `usage`.

    With server-side fallbacks the top-level usage covers only the attempt that produced
    the message, so every attempt listed in `usage.iterations` is summed (each at its own
    model's prices). Over-counting a declined attempt errs on the safe side of the budget.
    """
    attempts = [
        it
        for it in (getattr(usage, "iterations", None) or [])
        if getattr(it, "type", None) in ("message", "fallback_message")
    ] or [usage]
    prompt_tokens = output_tokens = 0
    cost = 0.0
    for attempt in attempts:
        fresh = getattr(attempt, "input_tokens", 0) or 0
        out = getattr(attempt, "output_tokens", 0) or 0
        read = getattr(attempt, "cache_read_input_tokens", 0) or 0
        write = getattr(attempt, "cache_creation_input_tokens", 0) or 0
        prompt_tokens += fresh + read + write
        output_tokens += out
        cost += estimate_cost(getattr(attempt, "model", None) or model, fresh, out, read, write)
    return prompt_tokens, output_tokens, cost


class _Spend(NamedTuple):
    model: str  # the model that produced the response (differs after a refusal fallback)
    prompt_tokens: int
    output_tokens: int
    cost: float


def _spend(response: Any, requested_model: str) -> _Spend:
    model = getattr(response, "model", None) or requested_model
    return _Spend(model, *usage_cost(response.usage, model))


# ---------------------------------------------------------------------- output schema
class ClaudeDecision(BaseModel):
    """One decision as Claude writes it. Ranges and levels are validated after parsing."""

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


class ClaudeDecisions(BaseModel):
    market_view: str = Field(description="One or two sentences on conditions")
    decisions: list[ClaudeDecision]


class _Rejected(Exception):
    """A single decision failed validation; the message says why."""


# ---------------------------------------------------------------------- maker
class ClaudeDecisionMaker:
    name = "claude"

    def __init__(
        self,
        api_key: str,
        model: str,
        effort: str,
        fallback: DecisionMaker,
        client: AsyncAnthropic | None = None,
    ) -> None:
        if effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}, got {effort!r}")
        if not model:
            raise ValueError("model is required")
        if client is None:
            if not api_key or not api_key.strip():
                raise ValueError("an Anthropic API key is required")
            # The key lives only inside the SDK client; it is never stored or logged here.
            client = AsyncAnthropic(
                api_key=api_key.strip(), timeout=REQUEST_TIMEOUT_S, max_retries=MAX_RETRIES
            )
        self.model = model
        self.effort = effort
        self.fallback = fallback
        self._client = client

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        try:
            user_text = render_context(ctx)
        except Exception as exc:
            log.exception("could not render the decision context")
            return self._failed(ctx, f"could not render context: {type(exc).__name__}")
        try:
            response = await self._client.beta.messages.parse(**self._request(user_text))
        except ValidationError as exc:
            # The SDK validates the JSON inside parse(); with constrained decoding this only
            # fails on incomplete JSON (cut off at max_tokens). Usage is lost with the
            # response, so charge a worst-case estimate rather than nothing.
            log.warning("Claude output failed validation (%d errors)", exc.error_count())
            prompt_tokens = (len(SYSTEM_PROMPT) + len(user_text)) // CHARS_PER_TOKEN
            cost = estimate_cost(self.model, prompt_tokens, MAX_TOKENS)
            return self._failed(
                ctx,
                "Claude output was not valid JSON for the schema (likely cut off); "
                "tokens and cost are estimated",
                spend=_Spend(self.model, prompt_tokens, MAX_TOKENS, cost),
            )
        except anthropic.AnthropicError as exc:
            error = _describe(exc)
            log.warning("Claude request failed: %s", error)
            if _is_transient(exc):
                return await self._exits_only(ctx, error)
            return self._failed(ctx, error)
        except Exception as exc:
            log.exception("unexpected error calling Claude")
            return self._failed(ctx, f"unexpected error: {type(exc).__name__}")

        try:
            spend = _spend(response, self.model)
        except Exception as exc:
            log.exception("could not read usage from Claude response")
            return self._failed(ctx, f"could not process response: {type(exc).__name__}")
        try:
            return self._from_response(response, ctx, spend)
        except Exception as exc:
            log.exception("could not process Claude response")
            return self._failed(
                ctx, f"could not process response: {type(exc).__name__}", spend=spend
            )

    # ------------------------------------------------------------------ request
    def _request(self, user_text: str) -> dict[str, Any]:
        # Thinking stays at the model default (adaptive); no sampling parameters, no
        # prefill. Effort is set explicitly because the default differs per model.
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": user_text}],
            "output_format": ClaudeDecisions,
            "output_config": {"effort": self.effort},
        }
        # Haiku has no server-side fallback; the docs say not to send the parameter.
        if not self.model.startswith("claude-haiku"):
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    # ------------------------------------------------------------------ response
    def _from_response(self, response: Any, ctx: DecisionContext, spend: _Spend) -> DecisionBatch:
        if spend.model != self.model:
            log.info("Claude request served by fallback model %s", spend.model)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            log.warning("Claude declined the request (category %s)", category)
            return self._failed(
                ctx,
                f"Claude declined the request (category: {category})",
                refused=True,
                spend=spend,
            )
        if response.stop_reason != "end_turn":
            return self._failed(
                ctx, f"response stopped early ({response.stop_reason})", spend=spend
            )

        parsed = response.parsed_output
        if parsed is None:
            return self._failed(ctx, "response contained no structured output", spend=spend)
        output = ClaudeDecisions.model_validate(
            parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
        )
        decisions, dropped = convert_decisions(output, ctx)
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model=spend.model,
            market_view=_clip(output.market_view, MAX_MARKET_VIEW),
            decisions=decisions,
            candidates=ctx.candidates,
            input_tokens=spend.prompt_tokens,
            output_tokens=spend.output_tokens,
            cost_usd=spend.cost,
            error=f"dropped {len(dropped)} invalid decision(s): " + "; ".join(dropped)
            if dropped
            else None,
        )

    def _failed(
        self,
        ctx: DecisionContext,
        error: str,
        *,
        refused: bool = False,
        spend: _Spend | None = None,
    ) -> DecisionBatch:
        """A batch with no decisions (fail safe), still carrying whatever the call cost."""
        spend = spend or _Spend(self.model, 0, 0, 0.0)
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model=spend.model,
            candidates=ctx.candidates,
            input_tokens=spend.prompt_tokens,
            output_tokens=spend.output_tokens,
            cost_usd=spend.cost,
            refused=refused,
            error=error,
        )

    async def _exits_only(self, ctx: DecisionContext, error: str) -> DecisionBatch:
        """Transient outage: keep the fallback's risk-reducing CLOSEs, never its BUYs."""
        fallback_name = self.fallback.name
        try:
            batch = await self.fallback.decide(ctx)
        except Exception:
            log.exception("fallback decision maker failed")
            return self._failed(ctx, f"{error}; {fallback_name} fallback failed")
        exits = [d for d in batch.decisions if d.action == TradeAction.CLOSE]
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model=self.model,
            market_view=f"Claude unavailable; {fallback_name} exits only, no new entries.",
            decisions=exits,
            candidates=ctx.candidates,
            error=f"{error}; used {fallback_name} exits only",
        )


# ---------------------------------------------------------------------- validation
def convert_decisions(
    output: ClaudeDecisions, ctx: DecisionContext
) -> tuple[list[AIDecision], list[str]]:
    """Validate Claude's decisions against the context. Returns (kept, reasons for drops)."""
    candidates: dict[str, list[Candidate]] = {}
    for cand in ctx.candidates:
        candidates.setdefault(cand.instrument.key, []).append(cand)
    held = {p.instrument.key: p for p in ctx.positions if p.qty > 0}
    kept: list[AIDecision] = []
    dropped: list[str] = []
    acted: set[str] = set()
    for raw in output.decisions:
        label = f"{raw.action} {_clip(raw.symbol, 20)}"
        try:
            decision = _convert_one(raw, candidates, held, ctx.limits)
            key = Instrument(symbol=decision.symbol, asset_class=decision.asset_class).key
            if decision.action != TradeAction.HOLD:
                if key in acted:
                    raise _Rejected("more than one BUY/CLOSE for this instrument")
                acted.add(key)
        except (_Rejected, ValidationError) as exc:
            reason = str(exc) if isinstance(exc, _Rejected) else "invalid field values"
            log.warning("dropping Claude decision %s: %s", label, reason)
            dropped.append(f"{label} ({reason})")
            continue
        kept.append(decision)
    return kept, dropped


def _convert_one(
    raw: ClaudeDecision,
    candidates: dict[str, list[Candidate]],
    held: dict[str, Position],
    limits: RiskLimits,
) -> AIDecision:
    symbol = raw.symbol.strip().upper()
    if not symbol:
        raise _Rejected("empty symbol")
    asset_class = AssetClass(raw.asset_class)
    key = Instrument(symbol=symbol, asset_class=asset_class).key
    action = TradeAction(raw.action)
    rationale = _clip(raw.rationale, MAX_RATIONALE)
    if action != TradeAction.BUY:
        pos = held.get(key)
        if action == TradeAction.CLOSE and pos is None:
            raise _Rejected("no open position to close")
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
        raise _Rejected("not in the candidate list")
    if key in held:
        raise _Rejected("already holding this instrument")
    if not 0 <= raw.conviction <= 1:
        raise _Rejected(f"conviction {raw.conviction} outside 0..1")
    if not 0 < raw.size_fraction <= 1:
        raise _Rejected(f"size_fraction {raw.size_fraction} outside (0, 1]")
    cand = next((c for c in cands if c.setup == raw.setup), max(cands, key=lambda c: c.score))
    limit = raw.limit_price
    if limit is not None:
        if not (math.isfinite(limit) and limit > 0):
            raise _Rejected(f"invalid limit_price {limit}")
        # Gross-error guard only; the risk manager applies the real collar against the quote.
        max_gap = HARD_CAPS["price_collar_pct"] / 100
        if abs(limit - cand.price) > cand.price * max_gap:
            raise _Rejected(f"limit_price {limit} too far from candidate price {cand.price}")
    entry = limit if limit is not None else cand.price
    if raw.stop_loss is None or raw.take_profit is None:
        raise _Rejected("BUY needs both stop_loss and take_profit")
    rr = reward_risk(entry, raw.stop_loss, raw.take_profit)
    if rr is None:
        raise _Rejected(
            f"invalid levels stop {raw.stop_loss} / entry {entry} / target {raw.take_profit}"
        )
    if rr < limits.min_reward_risk - RR_TOLERANCE:
        raise _Rejected(f"reward:risk {rr:.2f} below minimum {limits.min_reward_risk:.2f}")
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


# ---------------------------------------------------------------------- helpers
def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in TRANSIENT_STATUS or exc.status_code >= 500
    return False


def _describe(exc: Exception) -> str:
    """Short, secret-free description of an SDK error for logs and the journal."""
    text = f"{type(exc).__name__}: {_clip(str(getattr(exc, 'message', exc)), 200)}"
    if isinstance(exc, anthropic.APIStatusError):
        text += f" (HTTP {exc.status_code}, request {exc.request_id or '-'})"
    return text


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
