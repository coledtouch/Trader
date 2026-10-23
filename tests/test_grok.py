"""GrokDecisionMaker against an in-process httpx.MockTransport (offline)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.test_claude import FakeFallback, base_ctx, decision, output
from trader.models import AIDecision, AssetClass, DecisionBatch, OrderType, TradeAction
from trader.strategy import grok as grok_mod
from trader.strategy.common import ModelDecisions, strict_json_schema
from trader.strategy.grok import (
    DECISIONS_SCHEMA,
    DEFAULT_PRICE,
    MAX_OUTPUT_TOKENS,
    GrokDecisionMaker,
    GrokPrice,
    build_request,
    read_response,
    usage_cost,
)
from trader.strategy.prompts import SYSTEM_PROMPT, render_context

KEY = "xai-test-SECRET-abcdef0123456789"
BASE = "https://api.x.ai/v1"


# ---------------------------------------------------------------------- helpers
def body(
    answer: ModelDecisions | str | None = None,
    *,
    status: str = "completed",
    usage: dict[str, Any] | None = None,
    model: str = "grok-4.7",
    content: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if content is None:
        text = answer.model_dump_json() if isinstance(answer, ModelDecisions) else answer
        content = [] if text is None else [{"type": "output_text", "text": text}]
    return {
        "id": "resp_1",
        "object": "response",
        "status": status,
        "model": model,
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "role": "assistant", "content": content},
        ],
        "usage": usage
        if usage is not None
        else {
            "input_tokens": 3000,
            "input_tokens_details": {"cached_tokens": 2000},
            "output_tokens": 400,
            "output_tokens_details": {"reasoning_tokens": 1200},
            "total_tokens": 4600,
        },
        **extra,
    }


class Server:
    """Answers each request with the next queued response (or raises the queued error)."""

    def __init__(self, *answers: httpx.Response | Exception | Callable[[], httpx.Response]):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer() if callable(answer) else answer

    def json(self, i: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def error(code: int, message: str = "nope", **headers: str) -> httpx.Response:
    return httpx.Response(code, json={"code": "x", "error": message}, headers=headers)


def maker(server: Server, fallback: Any = None, **kw: Any) -> tuple[GrokDecisionMaker, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    m = GrokDecisionMaker(
        KEY, "grok-4.7", fallback or FakeFallback(), client=client, sleep=sleep, **kw
    )
    return m, sleeps


def rules_batch_with_close_and_buy() -> DecisionBatch:
    """What the rules fallback might say during an outage: a CLOSE and a BUY."""
    common = {"asset_class": AssetClass.STOCK, "conviction": 1.0, "rationale": "rules"}
    return DecisionBatch(
        maker="rules",
        decisions=[
            AIDecision(
                action=TradeAction.CLOSE, symbol="SPY", order_type=OrderType.MARKET, **common
            ),
            AIDecision(
                action=TradeAction.BUY, symbol="AMD", limit_price=100, stop_loss=98,
                take_profit=104, **common,
            ),
        ],
    )  # fmt: skip


# ---------------------------------------------------------------------- request shape
async def test_request_shape_and_headers() -> None:
    server = Server(ok(body(output())))
    m, _ = maker(server)
    ctx = base_ctx()
    await m.decide(ctx)
    (request,) = server.requests
    assert str(request.url) == f"{BASE}/responses"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    payload = server.json()
    assert payload["model"] == "grok-4.7"
    assert payload["input"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_context(ctx)},
    ]
    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "trading_decisions",
            "schema": DECISIONS_SCHEMA,
            "strict": True,
        }
    }
    assert payload["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert payload["store"] is False
    for forbidden in ("temperature", "top_p", "tools", "response_format"):
        assert forbidden not in payload


def test_build_request_is_the_only_wire_format() -> None:
    path, payload = build_request("grok-x", "sys", "user")
    assert path == "/responses"
    assert payload["input"][0] == {"role": "system", "content": "sys"}
    assert payload["text"]["format"]["schema"] is DECISIONS_SCHEMA


def test_strict_schema_is_flat_and_closed() -> None:
    schema = strict_json_schema(ModelDecisions)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and '"title"' not in text

    def objects(node: Any):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from objects(value)

    found = list(objects(schema))
    assert len(found) == 2  # the answer and one decision
    for obj in found:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    item = schema["properties"]["decisions"]["items"]["properties"]
    assert item["action"]["enum"] == ["BUY", "CLOSE", "HOLD"]
    assert {"type": "null"} in item["limit_price"]["anyOf"]


# ---------------------------------------------------------------------- success
async def test_success_converts_filters_and_prices_usage() -> None:
    answer = output(
        decision("BUY", "amd", conviction=0.8, size_fraction=0.5),
        decision("BUY", "TSLA"),  # not a candidate: dropped
        decision("CLOSE", "SPY"),
        decision("HOLD", "F"),
        view="Grok sees a clean reclaim.",
    )
    server = Server(ok(body(answer)))
    m, sleeps = maker(server)
    ctx = base_ctx()
    batch = await m.decide(ctx)
    assert sleeps == []
    assert batch.maker == "grok" and batch.model == "grok-4.7"
    assert batch.market_view == "Grok sees a clean reclaim."
    assert [(d.action, d.symbol) for d in batch.decisions] == [
        (TradeAction.BUY, "AMD"),
        (TradeAction.CLOSE, "SPY"),
        (TradeAction.HOLD, "F"),
    ]
    assert batch.decisions[0].size_fraction == 0.5
    assert batch.error and batch.error.startswith("dropped 1 invalid decision(s): BUY TSLA")
    assert batch.candidates == ctx.candidates
    # 1000 fresh + 2000 cached input; output billed as total - input = 1600 (incl. reasoning)
    assert (batch.input_tokens, batch.output_tokens) == (3000, 1600)
    assert batch.cost_usd == pytest.approx((1000 * 2 + 2000 * 0.5 + 1600 * 6) / 1e6)


async def test_custom_price_and_served_model() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    server = Server(ok(body(output(), usage=usage, model="grok-4.7-0815")))
    m, _ = maker(server, price=GrokPrice(input=3.0, cached_input=0.75, output=15.0))
    batch = await m.decide(base_ctx())
    assert batch.model == "grok-4.7-0815"
    assert batch.cost_usd == pytest.approx(3.0 + 15.0)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        # total_tokens includes reasoning on top of output_tokens: bill everything past input
        (
            {"input_tokens": 100, "output_tokens": 10, "total_tokens": 160,
             "output_tokens_details": {"reasoning_tokens": 50}},
            (100, 60),
        ),
        # output_tokens already includes reasoning: no double counting
        (
            {"input_tokens": 100, "output_tokens": 60, "total_tokens": 160,
             "output_tokens_details": {"reasoning_tokens": 50}},
            (100, 60),
        ),
        # no total: reasoning added on top (can only over-count)
        ({"input_tokens": 100, "output_tokens": 10,
          "output_tokens_details": {"reasoning_tokens": 50}}, (100, 60)),
        # chat-completions field names
        ({"prompt_tokens": 100, "completion_tokens": 20,
          "prompt_tokens_details": {"cached_tokens": 40}}, (100, 20)),
        # junk values are ignored
        ({"input_tokens": "lots", "output_tokens": -5, "total_tokens": None}, (0, 0)),
    ],
)  # fmt: skip
def test_usage_cost_variants(usage: dict[str, Any], expected: tuple[int, int]) -> None:
    prompt, billed, cost = usage_cost(usage, DEFAULT_PRICE)
    assert (prompt, billed) == expected
    cached = min(
        (usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}).get(
            "cached_tokens", 0
        ),
        prompt,
    )
    assert cost == pytest.approx(((prompt - cached) * 2 + cached * 0.5 + billed * 6) / 1e6)


# ---------------------------------------------------------------------- retries
async def test_429_is_retried_with_retry_after_then_succeeds() -> None:
    server = Server(error(429, "rate limited", **{"retry-after": "3"}), ok(body(output())))
    m, sleeps = maker(server)
    batch = await m.decide(base_ctx())
    assert len(server.requests) == 2
    assert sleeps == [3.0]
    assert batch.error is None


@pytest.mark.parametrize("code", [500, 502, 503, 529])
async def test_5xx_retried_twice_then_exits_only(code: int) -> None:
    fallback = FakeFallback(rules_batch_with_close_and_buy())
    server = Server(error(code, "upstream sad", **{"x-request-id": "req-9"}))
    m, sleeps = maker(server, fallback)
    batch = await m.decide(base_ctx())
    assert len(server.requests) == 3  # first try + 2 retries
    assert sleeps == [1.0, 2.0]  # exponential backoff
    assert fallback.calls == 1
    assert [d.action for d in batch.decisions] == [TradeAction.CLOSE]  # never the rules' BUY
    assert f"HTTP {code}: upstream sad (request req-9)" in (batch.error or "")
    assert "exits only" in (batch.error or "")
    assert batch.market_view.startswith("Grok unavailable")


async def test_retry_after_is_capped() -> None:
    server = Server(error(429, **{"retry-after": "3600"}), ok(body(output())))
    m, sleeps = maker(server)
    await m.decide(base_ctx())
    assert sleeps == [grok_mod.RETRY_MAX_DELAY_S]


async def test_connection_errors_are_retried_then_fail_safe() -> None:
    server = Server(httpx.ConnectError("refused"))
    m, sleeps = maker(server)
    batch = await m.decide(base_ctx())
    assert len(server.requests) == 3 and len(sleeps) == 2
    assert batch.decisions == [] and "connection error (ConnectError)" in (batch.error or "")


async def test_timeout_is_not_retried() -> None:
    server = Server(httpx.ReadTimeout("slow"))
    fallback = FakeFallback(DecisionBatch(maker="rules"))
    m, sleeps = maker(server, fallback)
    batch = await m.decide(base_ctx())
    assert len(server.requests) == 1 and sleeps == []
    assert "timed out" in (batch.error or "") and fallback.calls == 1


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
async def test_client_errors_fail_without_retry_or_fallback(code: int) -> None:
    fallback = FakeFallback(DecisionBatch(maker="rules"))
    server = Server(error(code, f"bad thing {code}"))
    m, sleeps = maker(server, fallback)
    batch = await m.decide(base_ctx())
    assert len(server.requests) == 1 and sleeps == []
    assert fallback.calls == 0
    assert batch.decisions == [] and batch.error == f"HTTP {code}: bad thing {code}"
    assert batch.cost_usd == 0


async def test_failing_fallback_is_safe() -> None:
    server = Server(error(503))
    m, _ = maker(server, FakeFallback(exc=RuntimeError("broken")))
    batch = await m.decide(base_ctx())
    assert batch.decisions == [] and "fallback failed" in (batch.error or "")


# ---------------------------------------------------------------------- bad answers
async def test_non_json_body_fails_safe_and_charges_an_estimate() -> None:
    server = Server(httpx.Response(200, text="<html>gateway</html>"))
    m, _ = maker(server)
    batch = await m.decide(base_ctx())
    assert batch.decisions == []
    assert batch.error == "HTTP 200 response was not JSON"
    assert batch.output_tokens == MAX_OUTPUT_TOKENS and batch.cost_usd > 0


@pytest.mark.parametrize("payload", [[], {"status": "completed"}, {"output": "text"}])
async def test_malformed_body_fails_safe(payload: Any) -> None:
    m, _ = maker(Server(ok(payload)))
    batch = await m.decide(base_ctx())
    assert batch.decisions == [] and (batch.error or "").startswith("malformed response")
    assert batch.cost_usd > 0  # an answer came back, so assume it was billed


@pytest.mark.parametrize(
    "text",
    [
        '{"market_view": "cut off", "decisions": [{"act',  # truncated JSON
        '{"market_view": "x"}',  # schema-invalid: decisions missing
        '{"market_view": "x", "decisions": [{"action": "SELL_SHORT", "symbol": "AMD"}]}',
        "not json at all",
    ],
)
async def test_schema_invalid_output_fails_safe_with_cost(text: str) -> None:
    m, _ = maker(Server(ok(body(text))))
    batch = await m.decide(base_ctx())
    assert batch.decisions == []
    assert batch.error == "Grok output was not valid JSON for the decision schema"
    assert batch.cost_usd == pytest.approx((1000 * 2 + 2000 * 0.5 + 1600 * 6) / 1e6)


async def test_refusal_is_refused_with_no_decisions() -> None:
    content = [{"type": "refusal", "refusal": "I can't help with trading."}]
    fallback = FakeFallback(DecisionBatch(maker="rules"))
    m, _ = maker(Server(ok(body(content=content))), fallback)
    batch = await m.decide(base_ctx())
    assert batch.refused and batch.decisions == []
    assert "I can't help with trading." in (batch.error or "")
    assert fallback.calls == 0


async def test_content_filter_counts_as_refusal() -> None:
    payload = body(None, status="incomplete", incomplete_details={"reason": "content_filter"})
    m, _ = maker(Server(ok(payload)))
    batch = await m.decide(base_ctx())
    assert batch.refused and batch.decisions == []


async def test_cut_off_output_is_an_error() -> None:
    payload = body(
        '{"market_view": "x", "decisions": [',
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    m, _ = maker(Server(ok(payload)))
    batch = await m.decide(base_ctx())
    assert batch.decisions == [] and not batch.refused
    assert batch.error == "response stopped early (max_output_tokens)"


async def test_failed_status_and_empty_output() -> None:
    m, _ = maker(Server(ok(body(None, status="failed", error={"message": "model crashed"}))))
    batch = await m.decide(base_ctx())
    assert batch.error == "response stopped early (model crashed)"
    m, _ = maker(Server(ok(body(None))))
    batch = await m.decide(base_ctx())
    assert batch.error == "response contained no structured output"


def test_read_response_accepts_output_text_and_string_content() -> None:
    reply = read_response({"output": [], "output_text": '{"a": 1}'})
    assert reply.text == '{"a": 1}' and reply.status == "completed" and reply.usage == {}
    reply = read_response({"output": [{"type": "message", "content": "{}"}], "status": "x"})
    assert reply.text == "{}" and reply.status == "x"


async def test_render_failure_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(ctx: Any) -> str:
        raise TypeError("bad context")

    monkeypatch.setattr(grok_mod, "render_context", broken)
    server = Server(ok(body(output())))
    m, _ = maker(server)
    batch = await m.decide(base_ctx())
    assert batch.error == "could not render context: TypeError" and server.requests == []


# ---------------------------------------------------------------------- secrets / setup
async def test_api_key_never_logged_or_journaled(caplog: pytest.LogCaptureFixture) -> None:
    server = Server(error(401, f"Incorrect API key provided: {KEY}"))
    m, _ = maker(server)
    with caplog.at_level(logging.DEBUG):
        batch = await m.decide(base_ctx())
    assert KEY not in caplog.text
    assert KEY not in (batch.error or "") and "***" in (batch.error or "")
    assert KEY not in repr(m) and KEY not in repr(vars(m))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"api_key": " "}, "xAI API key"), ({"model": ""}, "model"), ({"base_url": "ftp://x"}, "URL")],
)
def test_constructor_validation(kwargs: dict[str, str], message: str) -> None:
    args: dict[str, Any] = {"api_key": KEY, "model": "grok-4.7", "fallback": FakeFallback()}
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        GrokDecisionMaker(**args)


def test_default_client_settings() -> None:
    m = GrokDecisionMaker(KEY, "grok-4.7", FakeFallback())
    assert m.name == "grok"
    assert m._client.timeout.read == grok_mod.REQUEST_TIMEOUT_S == 90.0
    assert grok_mod.MAX_RETRIES == 2
    assert grok_mod.DEFAULT_MODEL == "grok-4.7"
    assert grok_mod.DEFAULT_BASE_URL == "https://api.x.ai/v1"
