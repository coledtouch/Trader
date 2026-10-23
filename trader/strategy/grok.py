"""Grok decision maker: one xAI API call per cycle with JSON-schema structured output.

Grok sees exactly what Claude sees (prompts.SYSTEM_PROMPT + render_context) and its answer
goes through the same validation (trader.strategy.common), so the two makers are directly
comparable and the consensus maker can line their votes up symbol by symbol.

Wire format: xAI's OpenAI-compatible REST API. This module uses the Responses API
(``POST {base_url}/responses`` with ``text.format = {"type": "json_schema", "name",
"schema", "strict": true}``), which xAI documents as its recommended interface (Chat
Completions is listed as legacy). Only `build_request` and `read_response` know that
shape; switching to ``/chat/completions`` with ``response_format`` is a change to those
two functions. No new dependency: plain ``httpx``.

Failure handling mirrors the Claude maker (fail safe, never a BUY on a failure):
- refusal (a ``refusal`` content part, or ``incomplete_details.reason == "content_filter"``):
  ``refused=True``, no decisions.
- cut off, unparseable or schema-invalid output, other 4xx errors, unexpected exceptions:
  ``error`` set, no decisions.
- transient errors (connection errors, timeouts, 408/409/429, 5xx): 408/409/429/5xx and
  connection errors are retried up to MAX_RETRIES times (honouring ``Retry-After``); what
  remains returns the fallback maker's CLOSE decisions only.
The API key only ever travels in the Authorization header; it is never logged, journaled
or kept in a readable attribute, and error text is scrubbed of it.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from trader.models import DecisionBatch, DecisionContext
from trader.strategy.common import (
    CHARS_PER_TOKEN,
    MAX_MARKET_VIEW,
    ModelDecisions,
    clip,
    convert_decisions,
    dropped_error,
    exits_only,
    failed_batch,
    strict_json_schema,
)
from trader.strategy.decide import DecisionMaker
from trader.strategy.prompts import SYSTEM_PROMPT, render_context

log = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-4.7"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
REQUEST_TIMEOUT_S = 90.0
CONNECT_TIMEOUT_S = 10.0
MAX_RETRIES = 2  # extra attempts after the first, for 408/409/429/5xx and connection errors
RETRY_BASE_DELAY_S = 1.0
RETRY_MAX_DELAY_S = 20.0
MAX_OUTPUT_TOKENS = 16000  # reasoning counts towards this as well as the JSON answer
RETRY_STATUS = frozenset({408, 409, 429})
SCHEMA_NAME = "trading_decisions"
DECISIONS_SCHEMA: dict[str, Any] = strict_json_schema(ModelDecisions)


# ---------------------------------------------------------------------- pricing
@dataclass(frozen=True)
class GrokPrice:
    """USD per million tokens."""

    input: float
    cached_input: float
    output: float  # also applied to reasoning tokens


# grok-4.7 for prompts under 200k tokens: $2 input, $0.50 cached input, $6 output per 1M.
# Third-party figures, not confirmed against xAI's price page; override them with
# TRADER_XAI_PRICE_INPUT / _CACHED_INPUT / _OUTPUT when console.x.ai says otherwise.
DEFAULT_PRICE = GrokPrice(input=2.0, cached_input=0.50, output=6.0)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value) if math.isfinite(value) and value > 0 else 0


def usage_cost(usage: Mapping[str, Any], price: GrokPrice) -> tuple[int, int, float]:
    """(prompt tokens, billed output tokens incl. reasoning, USD) for a response's usage.

    Accepts Responses (input_tokens/output_tokens) and Chat Completions (prompt_tokens/
    completion_tokens) field names. Cached prompt tokens are a subset of the prompt tokens.
    Reasoning tokens are billed as output: when ``total_tokens`` is reported, everything
    beyond the prompt is billed as output (correct whether or not output_tokens already
    includes reasoning); without it reasoning is added on top, which can only over-count.
    """
    prompt = _count(usage.get("input_tokens", usage.get("prompt_tokens")))
    in_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached = min(_count(in_details.get("cached_tokens")), prompt)
    output = _count(usage.get("output_tokens", usage.get("completion_tokens")))
    out_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    reasoning = _count(out_details.get("reasoning_tokens"))
    total = _count(usage.get("total_tokens"))
    billed = max(output, total - prompt) if total >= prompt and total > 0 else output + reasoning
    cost = (prompt - cached) * price.input + cached * price.cached_input + billed * price.output
    return prompt, billed, cost / 1_000_000


# ---------------------------------------------------------------------- wire format
def build_request(model: str, system: str, user: str) -> tuple[str, dict[str, Any]]:
    """(path, JSON body) for one decision request. The only place that knows the request
    shape of xAI's Responses API."""
    return "/responses", {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": SCHEMA_NAME,
                "schema": DECISIONS_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "store": False,  # do not keep the account snapshot on xAI's side
    }


@dataclass
class GrokReply:
    """The parts of a Responses API body this maker uses."""

    model: str | None
    status: str
    text: str | None
    refusal: str | None
    incomplete_reason: str | None
    error: str | None
    usage: dict[str, Any] = field(default_factory=dict)


def read_response(body: Any) -> GrokReply:
    """Extract the answer from a Responses API body. Raises ValueError when the body does
    not have the documented shape. The only place that knows the response shape."""
    if not isinstance(body, dict):
        raise ValueError("response body is not a JSON object")
    output = body.get("output")
    if not isinstance(output, list):
        raise ValueError("response has no output list")
    texts: list[str] = []
    refusals: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue  # reasoning items, tool calls, ...
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("output_text", "text") and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif kind == "refusal":
                refusals.append(str(part.get("refusal") or "declined"))
    text = "".join(texts) if texts else None
    if text is None and isinstance(body.get("output_text"), str):
        text = body["output_text"]
    incomplete = body.get("incomplete_details")
    reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
    error = body.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    usage = body.get("usage")
    return GrokReply(
        model=str(body["model"]) if body.get("model") else None,
        status=str(body.get("status") or "completed"),
        text=text,
        refusal="; ".join(refusals) or None,
        incomplete_reason=str(reason) if reason else None,
        error=str(error) if error else None,
        usage=usage if isinstance(usage, dict) else {},
    )


# ---------------------------------------------------------------------- errors
class _Transient(Exception):
    """Rate limit, server or connection error that remained after the retries."""


class _Permanent(Exception):
    """Any other request failure (4xx, a non-JSON body)."""

    def __init__(self, message: str, *, answered: bool = False) -> None:
        super().__init__(message)
        self.answered = answered  # a 2xx answer came back, so tokens were probably billed


@dataclass
class _Spend:
    model: str
    prompt_tokens: int
    output_tokens: int
    cost: float


# ---------------------------------------------------------------------- maker
class GrokDecisionMaker:
    name = "grok"

    def __init__(
        self,
        api_key: str,
        model: str,
        fallback: DecisionMaker,
        *,
        base_url: str = DEFAULT_BASE_URL,
        price: GrokPrice = DEFAULT_PRICE,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("an xAI API key is required")
        if not model:
            raise ValueError("model is required")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must be an http(s) URL")
        self.model = model
        self.fallback = fallback
        self.price = price
        self._key = SecretStr(api_key.strip())  # repr shows '**********'
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        self._client = client or httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        self._sleep = sleep

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        try:
            user_text = render_context(ctx)
        except Exception as exc:
            log.exception("could not render the decision context")
            return self._failed(ctx, f"could not render context: {type(exc).__name__}")
        path, payload = build_request(self.model, SYSTEM_PROMPT, user_text)
        try:
            body = await self._post(path, payload)
        except _Transient as exc:
            error = self._scrub(str(exc))
            log.warning("Grok request failed: %s", error)
            return await exits_only(
                ctx,
                maker=self.name,
                model=self.model,
                label="Grok",
                fallback=self.fallback,
                error=error,
            )
        except _Permanent as exc:
            error = self._scrub(str(exc))
            log.warning("Grok request failed: %s", error)
            spend = self._estimate(user_text) if exc.answered else None
            return self._failed(ctx, error, spend=spend)
        except Exception as exc:
            log.exception("unexpected error calling Grok")
            return self._failed(ctx, f"unexpected error: {type(exc).__name__}")
        try:
            reply = read_response(body)
        except ValueError as exc:
            log.warning("Grok response was malformed: %s", exc)
            return self._failed(ctx, f"malformed response: {exc}", spend=self._estimate(user_text))
        try:
            return self._from_reply(reply, ctx, user_text)
        except Exception as exc:
            log.exception("could not process Grok response")
            return self._failed(ctx, f"could not process response: {type(exc).__name__}")

    # ------------------------------------------------------------------ HTTP
    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST with retries for 408/409/429/5xx and connection errors. Returns the JSON
        body of a 2xx answer; raises _Transient or _Permanent otherwise."""
        url = self._base_url + path
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        error = "no attempt made"
        for attempt in range(MAX_RETRIES + 1):
            delay = min(RETRY_BASE_DELAY_S * 2**attempt, RETRY_MAX_DELAY_S)
            try:
                resp = await self._client.post(
                    url, json=payload, headers=headers, timeout=self._timeout
                )
            except httpx.TimeoutException as exc:
                # A 90 s timeout is not retried: a decision that late would be stale.
                raise _Transient(f"request timed out ({type(exc).__name__})") from None
            except httpx.TransportError as exc:
                error = f"connection error ({type(exc).__name__})"
            else:
                if 200 <= resp.status_code < 300:
                    try:
                        return resp.json()
                    except ValueError:
                        raise _Permanent(
                            f"HTTP {resp.status_code} response was not JSON", answered=True
                        ) from None
                error = _describe(resp)
                if resp.status_code not in RETRY_STATUS and resp.status_code < 500:
                    raise _Permanent(error)
                delay = _retry_after(resp, delay)
            if attempt < MAX_RETRIES:
                log.info("Grok request failed (%s); retry %d in %.1fs", error, attempt + 1, delay)
                await self._sleep(delay)
        raise _Transient(error)

    # ------------------------------------------------------------------ response
    def _from_reply(self, reply: GrokReply, ctx: DecisionContext, user_text: str) -> DecisionBatch:
        if reply.usage:
            prompt, output, cost = usage_cost(reply.usage, self.price)
            spend = _Spend(reply.model or self.model, prompt, output, cost)
        else:
            spend = self._estimate(user_text, model=reply.model)
        if spend.model != self.model:
            log.info("Grok request served by model %s", spend.model)
        if reply.refusal is not None or reply.incomplete_reason == "content_filter":
            detail = clip(reply.refusal or "content filter", 200)
            log.warning("Grok declined the request: %s", detail)
            return self._failed(
                ctx, f"Grok declined the request ({detail})", refused=True, spend=spend
            )
        if reply.status != "completed" or reply.incomplete_reason:
            why = reply.incomplete_reason or reply.error or reply.status
            return self._failed(
                ctx, f"response stopped early ({clip(self._scrub(why), 120)})", spend=spend
            )
        if not reply.text:
            return self._failed(ctx, "response contained no structured output", spend=spend)
        try:
            output_model = ModelDecisions.model_validate_json(reply.text)
        except ValidationError as exc:
            log.warning("Grok output failed validation (%d errors)", exc.error_count())
            return self._failed(
                ctx, "Grok output was not valid JSON for the decision schema", spend=spend
            )
        decisions, dropped = convert_decisions(output_model, ctx, source="Grok")
        return DecisionBatch(
            ts=ctx.now,
            maker=self.name,
            model=spend.model,
            market_view=clip(output_model.market_view, MAX_MARKET_VIEW),
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
        refused: bool = False,
        spend: _Spend | None = None,
    ) -> DecisionBatch:
        spend = spend or _Spend(self.model, 0, 0, 0.0)
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

    def _estimate(self, user_text: str, *, model: str | None = None) -> _Spend:
        """Worst-case spend for a 2xx answer without usable usage (the tokens were probably
        billed): the whole prompt at the input price plus the output cap."""
        prompt = (len(SYSTEM_PROMPT) + len(user_text)) // CHARS_PER_TOKEN
        cost = (prompt * self.price.input + MAX_OUTPUT_TOKENS * self.price.output) / 1_000_000
        return _Spend(model or self.model, prompt, MAX_OUTPUT_TOKENS, cost)

    def _scrub(self, text: str) -> str:
        key = self._key.get_secret_value()
        return text.replace(key, "***") if key else text


# ---------------------------------------------------------------------- helpers
def _describe(resp: httpx.Response) -> str:
    """Short description of an HTTP error answer (status, xAI's message, request id)."""
    message = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("code")
        message = str(err or body.get("message") or body.get("code") or "")
    text = f"HTTP {resp.status_code}"
    if message:
        text += f": {clip(message, 200)}"
    request_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
    if request_id:
        text += f" (request {clip(request_id, 64)})"
    return text


def _retry_after(resp: httpx.Response, default: float) -> float:
    raw = resp.headers.get("retry-after")
    try:
        seconds = float(raw) if raw is not None else default
    except ValueError:
        return default
    return min(max(seconds, 0.0), RETRY_MAX_DELAY_S) if math.isfinite(seconds) else default
