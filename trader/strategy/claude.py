"""Claude decision maker: one Anthropic Messages API call per cycle with structured output.

The stable system prompt is cached; the rendered context goes in the user message.
Claude's answer is parsed into a deliberately simple pydantic model (no range
constraints in the schema, so one bad number cannot fail the whole parse), then every
decision is validated here and converted to an AIDecision. Invalid decisions are
dropped and logged. Failure handling (fail safe, never a BUY on a failure):

- refusal (`stop_reason == "refusal"`): `refused=True`, no decisions, no fallback.
- cut off, unparseable output, 4xx errors, unexpected exceptions: `error` set, no decisions.
- transient API errors (429, 408/409, 5xx/overloaded, connection or timeout) that remain
  after the SDK's own retries, and no answer within DECISION_DEADLINE_S: the fallback
  maker's CLOSE decisions only (exits that reduce risk; its BUYs are discarded), with
  `error` set. Broker-side stops protect positions in every case.

The request is streamed: the API keeps a streamed connection busy while Claude thinks,
so a long answer cannot trip the HTTP read timeout, which a single non-streamed response
did. DECISION_DEADLINE_S bounds the whole call instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, NamedTuple

import anthropic
import httpx2
from anthropic import AsyncAnthropic, transform_schema
from pydantic import TypeAdapter, ValidationError

from trader.models import DecisionBatch, DecisionContext
from trader.strategy.common import (
    CHARS_PER_TOKEN,
    MAX_MARKET_VIEW,
    DecisionRejected,
    ModelDecision,
    ModelDecisions,
    clip,
    convert_decisions,
    dropped_error,
    exits_only,
    failed_batch,
)
from trader.strategy.decide import DecisionMaker
from trader.strategy.prompts import SYSTEM_PROMPT, render_context

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MAX_TOKENS = 16000  # thinking counts towards this as well as the JSON answer
REQUEST_TIMEOUT_S = 90.0  # per read; a streamed answer keeps the connection busy
MAX_RETRIES = 2
# The whole call, retries included. The engine drops a BUY decided on data more than
# 3 minutes old (DECISION_MAX_AGE), and the market data fetch comes first.
DECISION_DEADLINE_S = 140.0
# Server-side refusal fallback: the API re-runs a declined request on the model
# Anthropic recommends for the refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
TRANSIENT_STATUS = frozenset({408, 409, 429})
# An `event: error` inside a streamed 200 response arrives as an APIStatusError with status
# 200; these error types are the ones a non-streamed call would have got as 429/5xx/529.
STREAM_TRANSIENT_ERRORS = frozenset(
    {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}
)
# The answer's JSON schema, sent as output_config.format. The bot parses the finished
# answer itself (not the SDK's output_format, which parses every text block as it closes:
# after a mid-answer refusal that includes the declined model's partial text).
OUTPUT_SCHEMA = transform_schema(TypeAdapter(ModelDecisions).json_schema())
# The system prompt is cached for an hour: decision cycles are often more than 5 minutes
# apart (the LLM is skipped when nothing qualifies), so a 5-minute entry would expire.
# A 1-hour write costs 2x the base input price (5-minute: 1.25x), reads are unchanged.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}

# The model-facing schema and validation are shared with the Grok maker; the old names stay.
ClaudeDecision = ModelDecision
ClaudeDecisions = ModelDecisions
_Rejected = DecisionRejected
_clip = clip


# ---------------------------------------------------------------------- pricing
@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache writes (1.25x input)
    cache_write_1h: float  # 1-hour cache writes (2x input), what this maker requests


PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": ModelPrice(4.0, 20.0, 0.20, 5.0, 8.0),
    "claude-sonnet-5-5": ModelPrice(2.0, 10.0, 0.20, 2.5, 4.0),
    "claude-haiku-5-5": ModelPrice(0.10, 0.50, 0.01, 0.125, 0.20),
    # Likely targets of the server-side refusal fallback.
    "claude-opus-5": ModelPrice(5.0, 25.0, 0.50, 6.25, 10.0),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, 0.50, 6.25, 10.0),
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
    cache_write_1h_tokens: int = 0,
) -> float:
    """USD cost of one API attempt. `input_tokens` excludes cached tokens, as in `usage`;
    `cache_write_tokens` are 5-minute writes and `cache_write_1h_tokens` 1-hour writes."""
    p = model_price(model)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_read_tokens * p.cache_read
        + cache_write_tokens * p.cache_write
        + cache_write_1h_tokens * p.cache_write_1h
    ) / 1_000_000


def _cache_writes(attempt: Any) -> tuple[int, int, int]:
    """(all cache-write tokens, 5-minute writes, 1-hour writes) for one usage record.

    `usage.cache_creation` splits writes by TTL. Without that breakdown (or for tokens it
    does not explain) the writes are charged as 1-hour writes, the dearer TTL and the only
    one this maker requests, so the budget errs on the safe side."""
    total = getattr(attempt, "cache_creation_input_tokens", 0) or 0
    split = getattr(attempt, "cache_creation", None)
    five = getattr(split, "ephemeral_5m_input_tokens", None) or 0
    hour = getattr(split, "ephemeral_1h_input_tokens", None) or 0
    hour += max(0, total - five - hour)
    return max(total, five + hour), five, hour


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
        write, write_5m, write_1h = _cache_writes(attempt)
        prompt_tokens += fresh + read + write
        output_tokens += out
        cost += estimate_cost(
            getattr(attempt, "model", None) or model, fresh, out, read, write_5m, write_1h
        )
    return prompt_tokens, output_tokens, cost


class _Spend(NamedTuple):
    model: str  # the model that produced the response (differs after a refusal fallback)
    prompt_tokens: int
    output_tokens: int
    cost: float


def _spend(response: Any, requested_model: str) -> _Spend:
    model = getattr(response, "model", None) or requested_model
    return _Spend(model, *usage_cost(response.usage, model))


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
        deadline_s: float = DECISION_DEADLINE_S,
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
        self.deadline_s = deadline_s
        self._client = client
        self._abandoned_cost = 0.0  # see take_abandoned_cost

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        # Settings can change the model mid-call: this call keeps the one it started with.
        model, effort = self.model, self.effort
        try:
            user_text = render_context(ctx)
        except Exception as exc:
            log.exception("could not render the decision context")
            return self._failed(ctx, f"could not render context: {type(exc).__name__}", model=model)
        opened: list[bool] = []
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._call(user_text, opened, model, effort), self.deadline_s
            )
        except asyncio.CancelledError:
            if opened:  # accepted, then cut off by Stop or shutdown: billed, never journaled
                self._abandoned_cost += self._worst_case(user_text, model).cost
            raise
        except anthropic.AnthropicError as exc:
            error = _describe(exc)
            log.warning("Claude request failed: %s", error)
            if _is_transient(exc):
                return await self._exits_only(ctx, error, model=model)
            return self._failed(ctx, error, model=model)
        except (TimeoutError, httpx2.TransportError, _StreamEnded) as exc:
            # No answer in time (TimeoutError), or the open stream broke off: the SDK maps
            # transport errors only before the response headers, never while reading.
            if isinstance(exc, TimeoutError):
                error = f"Claude did not answer within {self.deadline_s:.0f}s"
            else:
                error = f"Claude's answer broke off ({type(exc).__name__})"
            log.warning("%s; exits only this cycle", error)
            if not opened:  # still connecting or between the SDK's retries: nothing billed
                return await self._exits_only(ctx, error, model=model)
            # Accepted and then cut off: its usage is lost, so charge a worst-case estimate
            # (the daily AI budget must not under-count).
            return await self._exits_only(
                ctx,
                f"{error}; tokens and cost are estimated",
                model=model,
                spend=self._worst_case(user_text, model),
            )
        except Exception as exc:
            log.exception("unexpected error calling Claude")
            return self._failed(ctx, f"unexpected error: {type(exc).__name__}", model=model)

        try:
            spend = _spend(response, model)
        except Exception as exc:
            log.exception("could not read usage from Claude response")
            return self._failed(
                ctx, f"could not process response: {type(exc).__name__}", model=model
            )
        # How close answers come to the deadline (a long candidate list or a high effort
        # slows them down; past the deadline a round opens nothing).
        log.info(
            "Claude answered in %.0fs of %.0fs (%d in, %d out tokens, $%.4f)",
            time.monotonic() - started,
            self.deadline_s,
            spend.prompt_tokens,
            spend.output_tokens,
            spend.cost,
        )
        try:
            return self._from_response(response, ctx, spend, model)
        except Exception as exc:
            log.exception("could not process Claude response")
            return self._failed(
                ctx, f"could not process response: {type(exc).__name__}", model=model, spend=spend
            )

    def take_abandoned_cost(self) -> float:
        """The estimated cost of calls cancelled after Claude accepted them (Stop, shutdown).
        They journal no batch, so the engine adds this to the day's AI spend. Resets it."""
        cost, self._abandoned_cost = self._abandoned_cost, 0.0
        return cost

    # ------------------------------------------------------------------ request
    async def _call(self, user_text: str, opened: list[bool], model: str, effort: str) -> Any:
        request = self._request(user_text, model, effort)
        async with self._client.beta.messages.stream(**request) as stream:
            opened.append(True)  # accepted (HTTP 200): from here on the call may be billed
            try:
                message = await stream.get_final_message()
            except AssertionError:  # the SDK's check: the body ended before message_start
                raise _StreamEnded("the answer ended before it started") from None
        if message is None or message.stop_reason is None:  # ended before message_stop
            raise _StreamEnded("the answer ended without a stop reason")
        return message

    def _worst_case(self, user_text: str, model: str) -> _Spend:
        """An estimate for a call whose usage was lost: the whole output budget spent."""
        prompt_tokens = (len(SYSTEM_PROMPT) + len(user_text)) // CHARS_PER_TOKEN
        cost = estimate_cost(model, prompt_tokens, MAX_TOKENS)
        return _Spend(model, prompt_tokens, MAX_TOKENS, cost)

    def _request(self, user_text: str, model: str, effort: str) -> dict[str, Any]:
        # Thinking stays at the model default (adaptive); no sampling parameters, no
        # prefill. Effort is set explicitly because the default differs per model.
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": dict(CACHE_CONTROL)}
            ],
            "messages": [{"role": "user", "content": user_text}],
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        }
        # Haiku has no server-side fallback; the docs say not to send the parameter.
        if not model.startswith("claude-haiku"):
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    # ------------------------------------------------------------------ response
    def _from_response(
        self, response: Any, ctx: DecisionContext, spend: _Spend, model: str
    ) -> DecisionBatch:
        if spend.model != model:
            log.info("Claude request served by fallback model %s", spend.model)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            log.warning("Claude declined the request (category %s)", category)
            return self._failed(
                ctx,
                f"Claude declined the request (category: {category})",
                model=model,
                refused=True,
                spend=spend,
            )
        if response.stop_reason != "end_turn":
            return self._failed(
                ctx, f"response stopped early ({response.stop_reason})", model=model, spend=spend
            )

        candidates = _answer_texts(response.content)
        if not candidates[0].strip():
            return self._failed(
                ctx, "response contained no structured output", model=model, spend=spend
            )
        output = None
        for text in candidates:
            try:
                output = ModelDecisions.model_validate_json(text)
                break
            except ValidationError as exc:
                log.warning("Claude output failed validation (%d errors)", exc.error_count())
        if output is None:
            return self._failed(
                ctx, "Claude output was not valid JSON for the schema", model=model, spend=spend
            )
        decisions, dropped = convert_decisions(output, ctx, source="Claude")
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model=spend.model,
            market_view=clip(output.market_view, MAX_MARKET_VIEW),
            decisions=decisions,
            candidates=ctx.candidates,
            input_tokens=spend.prompt_tokens,
            output_tokens=spend.output_tokens,
            cost_usd=spend.cost,
            error=dropped_error(dropped),
        )

    def _failed(
        self,
        ctx: DecisionContext,
        error: str,
        *,
        model: str,
        refused: bool = False,
        spend: _Spend | None = None,
    ) -> DecisionBatch:
        """A batch with no decisions (fail safe), still carrying whatever the call cost."""
        spend = spend or _Spend(model, 0, 0, 0.0)
        return failed_batch(
            ctx,
            self.name,
            spend.model,
            error,
            refused=refused,
            prompt_tokens=spend.prompt_tokens,
            output_tokens=spend.output_tokens,
            cost=spend.cost,
        )

    async def _exits_only(
        self, ctx: DecisionContext, error: str, *, model: str, spend: _Spend | None = None
    ) -> DecisionBatch:
        """Transient outage: keep the fallback's risk-reducing CLOSEs, never its BUYs."""
        spend = spend or _Spend(model, 0, 0, 0.0)
        return await exits_only(
            ctx,
            maker=self.name,
            model=spend.model,
            label="Claude",
            fallback=self.fallback,
            error=error,
            prompt_tokens=spend.prompt_tokens,
            output_tokens=spend.output_tokens,
            cost=spend.cost,
        )


# ---------------------------------------------------------------------- helpers
class _StreamEnded(Exception):
    """The streamed answer ended before its stop reason (a cut-off body)."""


def _answer_texts(content: Any) -> list[str]:
    """The answer's JSON, as the candidates to try in order. A refusal part way through a
    streamed answer hands over to the fallback model, which continues from the partial
    text: the answer is every text block joined, across the `fallback` blocks. A fallback
    that restarts instead puts a whole answer after the last `fallback` block."""
    blocks = list(content or [])

    def text(part: list[Any]) -> str:
        return "".join(b.text for b in part if getattr(b, "type", None) == "text")

    whole = text(blocks)
    last = max(
        (i for i, b in enumerate(blocks) if getattr(b, "type", None) == "fallback"), default=-1
    )
    tail = text(blocks[last + 1 :])
    return [whole] if tail == whole else [whole, tail]


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code < 400:  # an `event: error` inside a streamed 200 response
            body = exc.body if isinstance(exc.body, dict) else {}
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            return err.get("type") in STREAM_TRANSIENT_ERRORS
        return exc.status_code in TRANSIENT_STATUS or exc.status_code >= 500
    return False


def _describe(exc: Exception) -> str:
    """Short, secret-free description of an SDK error for logs and the journal."""
    text = f"{type(exc).__name__}: {clip(str(getattr(exc, 'message', exc)), 200)}"
    if isinstance(exc, anthropic.APIStatusError):
        text += f" (HTTP {exc.status_code}, request {exc.request_id or '-'})"
    return text
