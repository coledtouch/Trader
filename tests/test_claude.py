import json
import logging
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import ValidationError

from tests.test_decide import NOW, cand, make_ctx, pos
from trader.models import (
    AssetClass,
    DayStats,
    DecisionBatch,
    DecisionContext,
    Instrument,
    Mode,
    OrderType,
    RiskLimits,
    TradeAction,
    TradeRecord,
)
from trader.strategy import claude as claude_mod
from trader.strategy.claude import (
    FALLBACK_BETA,
    MAX_TOKENS,
    ClaudeDecision,
    ClaudeDecisionMaker,
    ClaudeDecisions,
    estimate_cost,
    usage_cost,
)
from trader.strategy.decide import RuleBasedDecisionMaker
from trader.strategy.prompts import SYSTEM_PROMPT, render_context

API_KEY = "sk-ant-test-SECRET-0123456789"
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


# ---------------------------------------------------------------------- fakes
class FakeMessages:
    def __init__(self, result: Any):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeClient:
    def __init__(self, result: Any):
        self.messages = FakeMessages(result)
        self.beta = SimpleNamespace(messages=self.messages)


class FakeFallback:
    name = "rules"

    def __init__(self, batch: DecisionBatch | None = None, exc: Exception | None = None):
        self.batch = batch
        self.exc = exc
        self.calls = 0

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        self.calls += 1
        if self.exc:
            raise self.exc
        assert self.batch is not None
        return self.batch


def usage(inp=1000, out=500, read=0, write=0, iterations=None) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
        iterations=iterations,
    )


def response(
    parsed: ClaudeDecisions | None = None,
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    use: SimpleNamespace | None = None,
    stop_details: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=use or usage(),
        content=[],
    )


def decision(action: str = "BUY", symbol: str = "AMD", **overrides: Any) -> ClaudeDecision:
    fields: dict[str, Any] = {
        "action": action,
        "symbol": symbol,
        "asset_class": "STK",
        "conviction": 0.7,
        "limit_price": None,
        "stop_loss": None,
        "take_profit": None,
        "size_fraction": 1.0,
        "setup": None,
        "rationale": "Reason.",
    }
    if action == "BUY":
        fields.update(limit_price=100.0, stop_loss=98.0, take_profit=104.0, setup="vwap_reclaim")
    fields.update(overrides)
    return ClaudeDecision(**fields)


def output(*decisions: ClaudeDecision, view: str = "Choppy tape.") -> ClaudeDecisions:
    return ClaudeDecisions(market_view=view, decisions=list(decisions))


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    resp = httpx2.Response(code, request=REQ, headers={"request-id": "req_123"})
    return cls("upstream said no", response=resp, body=None)


def maker(client: Any, fallback: Any = None, model: str = "claude-opus-5-5") -> ClaudeDecisionMaker:
    return ClaudeDecisionMaker(
        api_key=API_KEY,
        model=model,
        effort="high",
        fallback=fallback or FakeFallback(),
        client=client,
    )


def base_ctx() -> DecisionContext:
    return make_ctx(
        candidates=[
            cand("AMD", score=0.7, price=100, stop=98, target=104),
            cand("BTC", score=0.6, price=65000, stop=64000, target=67000),
        ],
        positions=[pos("SPY", avg=600, last=601, stop=595, target=612)],
        market={"STK:AMD": {"price": 100.0, "vwap": 99.5}, "STK:SPY": {"price": 601.0}},
    )


# ---------------------------------------------------------------------- request shape
async def test_request_parameters():
    client = FakeClient(response(output()))
    ctx = base_ctx()
    await maker(client).decide(ctx)

    (kwargs,) = client.messages.calls
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == MAX_TOKENS == 16000
    assert kwargs["output_format"] is ClaudeDecisions
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert kwargs["messages"] == [{"role": "user", "content": render_context(ctx)}]
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert forbidden not in kwargs


async def test_haiku_requests_omit_server_side_fallback():
    client = FakeClient(response(output(), model="claude-haiku-5-5"))
    await maker(client, model="claude-haiku-5-5").decide(base_ctx())
    (kwargs,) = client.messages.calls
    assert "fallbacks" not in kwargs and "betas" not in kwargs


# ---------------------------------------------------------------------- success path
async def test_successful_parse_converts_and_filters():
    parsed = output(
        decision("BUY", "amd", conviction=0.8, size_fraction=0.5, rationale="  Reclaimed VWAP. "),
        decision("BUY", "TSLA"),  # not a candidate
        decision(
            "BUY", "BTC", asset_class="CRYPTO", limit_price=65000, stop_loss=65500
        ),  # stop>entry
        decision("CLOSE", "SPY", conviction=1.4),  # held: kept, conviction clamped
        decision("CLOSE", "QQQ"),  # not held
        decision("HOLD", "F", conviction=0.2),
    )
    fallback = FakeFallback()
    client = FakeClient(response(parsed, use=usage(inp=2000, out=1000, read=1100, write=0)))
    ctx = base_ctx()
    batch = await maker(client, fallback).decide(ctx)

    assert fallback.calls == 0
    assert batch.maker == "claude"
    assert batch.model == "claude-opus-5-5"
    assert batch.candidates == ctx.candidates
    assert batch.ts == NOW
    assert batch.market_view == "Choppy tape."
    assert not batch.refused

    buy, close, hold = batch.decisions
    assert buy.action == TradeAction.BUY
    assert (buy.symbol, buy.asset_class, buy.order_type) == (
        "AMD",
        AssetClass.STOCK,
        OrderType.LIMIT,
    )
    assert (buy.limit_price, buy.stop_loss, buy.take_profit) == (100.0, 98.0, 104.0)
    assert (buy.conviction, buy.size_fraction, buy.setup) == (0.8, 0.5, "vwap_reclaim")
    assert buy.rationale == "Reclaimed VWAP."
    assert (close.action, close.symbol, close.order_type) == (
        TradeAction.CLOSE,
        "SPY",
        OrderType.MARKET,
    )
    assert close.conviction == 1.0
    assert close.setup == "vwap_reclaim"  # from the position
    assert close.stop_loss is None and close.limit_price is None
    assert (hold.action, hold.symbol) == (TradeAction.HOLD, "F")

    assert batch.error is not None
    assert batch.error.startswith("dropped 3 invalid decision(s)")
    assert "BUY TSLA (not in the candidate list)" in batch.error
    assert "BUY BTC (invalid levels" in batch.error
    assert "CLOSE QQQ (no open position to close)" in batch.error

    assert batch.input_tokens == 3100
    assert batch.output_tokens == 1000
    assert batch.cost_usd == pytest.approx((2000 * 4 + 1000 * 20 + 1100 * 0.2) / 1e6)


async def test_clean_batch_has_no_error():
    client = FakeClient(response(output(decision("HOLD", "AMD"))))
    batch = await maker(client).decide(base_ctx())
    assert batch.error is None
    assert [d.action for d in batch.decisions] == [TradeAction.HOLD]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"stop_loss": None}, "needs both stop_loss and take_profit"),
        ({"take_profit": None}, "needs both stop_loss and take_profit"),
        ({"stop_loss": 101.0}, "invalid levels"),
        ({"take_profit": 99.0}, "invalid levels"),
        ({"take_profit": 102.0}, "reward:risk 1.00 below minimum 1.50"),
        ({"size_fraction": 1.5}, "size_fraction"),
        ({"size_fraction": 0.0}, "size_fraction"),
        ({"conviction": 7.0}, "conviction"),
        ({"limit_price": 0.0}, "invalid limit_price"),
        ({"limit_price": 110.0, "stop_loss": 105, "take_profit": 120}, "too far from candidate"),
    ],
)
async def test_invalid_buys_are_dropped(overrides, reason):
    client = FakeClient(response(output(decision("BUY", "AMD", **overrides))))
    batch = await maker(client).decide(base_ctx())
    assert batch.decisions == []
    assert reason in (batch.error or "")


async def test_buy_without_limit_uses_candidate_price_for_validation():
    ok = decision("BUY", "AMD", limit_price=None, stop_loss=98.0, take_profit=103.0)  # R:R 1.5
    batch = await maker(FakeClient(response(output(ok)))).decide(base_ctx())
    assert len(batch.decisions) == 1
    assert batch.decisions[0].limit_price is None


async def test_buy_of_held_instrument_and_duplicates_are_dropped():
    ctx = make_ctx(
        candidates=[cand("AMD"), cand("SPY", price=600, stop=595, target=612)],
        positions=[pos("SPY", avg=600)],
    )
    parsed = output(
        decision("BUY", "SPY", limit_price=600, stop_loss=595, take_profit=612),
        decision("BUY", "AMD"),
        decision("BUY", "AMD"),
    )
    batch = await maker(FakeClient(response(parsed))).decide(ctx)
    assert [d.symbol for d in batch.decisions] == ["AMD"]
    assert "already holding" in (batch.error or "")
    assert "more than one BUY/CLOSE" in (batch.error or "")


async def test_unknown_setup_falls_back_to_candidate_setup():
    ctx = make_ctx(
        candidates=[
            cand("AMD", score=0.6, setup="trend_pullback"),
            cand("AMD", score=0.8, setup="orb_breakout"),
        ]
    )
    named = decision("BUY", "AMD", setup="trend_pullback")
    unnamed = decision("BUY", "AMD", setup="made_up")
    first = await maker(FakeClient(response(output(named)))).decide(ctx)
    second = await maker(FakeClient(response(output(unnamed)))).decide(ctx)
    assert first.decisions[0].setup == "trend_pullback"
    assert second.decisions[0].setup == "orb_breakout"  # highest score


async def test_served_by_fallback_model_sums_attempts():
    iterations = [
        SimpleNamespace(
            type="message",
            model="claude-opus-5-5",
            input_tokens=3000,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        SimpleNamespace(
            type="fallback_message",
            model="claude-opus-4-8",
            input_tokens=3000,
            output_tokens=800,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    ]
    resp = response(
        output(decision("HOLD", "AMD")),
        model="claude-opus-4-8",
        use=usage(inp=3000, out=800, iterations=iterations),
    )
    batch = await maker(FakeClient(resp)).decide(base_ctx())
    assert batch.model == "claude-opus-4-8"
    assert batch.input_tokens == 6000
    assert batch.output_tokens == 800
    assert batch.cost_usd == pytest.approx((3000 * 4 + 3000 * 5 + 800 * 25) / 1e6)


# ---------------------------------------------------------------------- failures
async def test_refusal_returns_no_decisions_and_skips_fallback():
    fallback = FakeFallback(DecisionBatch(maker="rules"))
    resp = response(
        None,
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="general_harms", explanation="nope"),
        use=usage(inp=1500, out=0),
    )
    batch = await maker(FakeClient(resp), fallback).decide(base_ctx())
    assert batch.refused is True
    assert batch.decisions == []
    assert "general_harms" in (batch.error or "")
    assert fallback.calls == 0
    assert batch.cost_usd == pytest.approx(1500 * 4 / 1e6)
    assert batch.candidates == base_ctx().candidates


async def test_refusal_after_fallback_chain_records_last_model():
    resp = response(None, stop_reason="refusal", model="claude-opus-4-8")
    batch = await maker(FakeClient(resp)).decide(base_ctx())
    assert batch.refused and batch.decisions == []
    assert batch.model == "claude-opus-4-8"


async def test_refusal_wins_even_with_parsed_output():
    resp = response(output(decision("BUY", "AMD")), stop_reason="refusal", stop_details=None)
    batch = await maker(FakeClient(resp)).decide(base_ctx())
    assert batch.refused and batch.decisions == []
    assert "unspecified" in (batch.error or "")


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
async def test_early_stop_is_an_error(stop_reason):
    resp = response(output(decision("BUY", "AMD")), stop_reason=stop_reason)
    batch = await maker(FakeClient(resp)).decide(base_ctx())
    assert batch.decisions == []
    assert not batch.refused
    assert stop_reason in (batch.error or "")
    assert batch.cost_usd > 0


async def test_missing_structured_output_is_an_error():
    batch = await maker(FakeClient(response(None))).decide(base_ctx())
    assert batch.decisions == []
    assert "no structured output" in (batch.error or "")


@pytest.mark.parametrize(
    "exc",
    [
        status_error(anthropic.BadRequestError, 400),
        status_error(anthropic.AuthenticationError, 401),
        status_error(anthropic.NotFoundError, 404),
    ],
)
async def test_client_errors_fail_safe_without_fallback(exc):
    fallback = FakeFallback(DecisionBatch(maker="rules"))
    batch = await maker(FakeClient(exc), fallback).decide(base_ctx())
    assert batch.decisions == []
    assert type(exc).__name__ in (batch.error or "")
    assert f"HTTP {exc.status_code}" in (batch.error or "")
    assert fallback.calls == 0
    assert batch.cost_usd == 0


@pytest.mark.parametrize(
    "exc",
    [
        status_error(anthropic.RateLimitError, 429),
        status_error(anthropic.InternalServerError, 500),
        status_error(anthropic.OverloadedError, 529),
        anthropic.APIConnectionError(request=REQ),
        anthropic.APITimeoutError(request=REQ),
    ],
)
async def test_transient_errors_use_fallback_exits_only(exc):
    ctx = base_ctx()
    rules_batch = await RuleBasedDecisionMaker().decide(
        make_ctx(
            candidates=ctx.candidates,
            positions=[pos("AMD")],
            market={"STK:AMD": {"price": 99.0, "vwap": 100.0}},
        )
    )
    assert {d.action for d in rules_batch.decisions} == {TradeAction.CLOSE, TradeAction.BUY}
    fallback = FakeFallback(rules_batch)

    batch = await maker(FakeClient(exc), fallback).decide(ctx)
    assert fallback.calls == 1
    assert batch.maker == "claude"
    assert [d.action for d in batch.decisions] == [TradeAction.CLOSE]
    assert "exits only" in (batch.error or "")
    assert type(exc).__name__ in (batch.error or "")
    assert batch.candidates == ctx.candidates


async def test_transient_error_with_failing_fallback_is_safe():
    fallback = FakeFallback(exc=RuntimeError("fallback broke"))
    exc = status_error(anthropic.RateLimitError, 429)
    batch = await maker(FakeClient(exc), fallback).decide(base_ctx())
    assert batch.decisions == []
    assert "fallback failed" in (batch.error or "")


async def test_validation_error_inside_parse_charges_worst_case():
    try:
        ClaudeDecisions.model_validate_json('{"market_view": "cut')
    except ValidationError as exc:
        truncated = exc
    batch = await maker(FakeClient(truncated)).decide(base_ctx())
    assert batch.decisions == []
    assert "estimated" in (batch.error or "")
    assert batch.output_tokens == MAX_TOKENS
    assert batch.cost_usd >= MAX_TOKENS * 20 / 1e6


async def test_unexpected_exception_is_safe():
    batch = await maker(FakeClient(KeyError("weird"))).decide(base_ctx())
    assert batch.decisions == []
    assert batch.error == "unexpected error: KeyError"


async def test_malformed_response_object_is_safe_and_keeps_cost():
    bad = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", usage=usage())
    batch = await maker(FakeClient(bad)).decide(base_ctx())  # no parsed_output attribute
    assert batch.decisions == []
    assert "could not process response" in (batch.error or "")
    assert batch.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


async def test_response_without_usage_is_safe():
    bad = SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", parsed_output=None)
    batch = await maker(FakeClient(bad)).decide(base_ctx())
    assert batch.decisions == []
    assert batch.error == "could not process response: AttributeError"


async def test_render_failure_is_safe(monkeypatch):
    def broken(ctx):
        raise TypeError("bad context")

    monkeypatch.setattr(claude_mod, "render_context", broken)
    client = FakeClient(response(output(decision("BUY", "AMD"))))
    batch = await maker(client).decide(base_ctx())
    assert batch.decisions == []
    assert batch.error == "could not render context: TypeError"
    assert client.messages.calls == []


async def test_api_key_never_logged_or_journaled(caplog, monkeypatch):
    real = ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "medium", FakeFallback())
    fake = FakeMessages(status_error(anthropic.AuthenticationError, 401))
    monkeypatch.setattr(real._client.beta.messages, "parse", fake.parse)
    with caplog.at_level(logging.DEBUG):
        batch = await real.decide(base_ctx())
    assert batch.decisions == []
    assert API_KEY not in caplog.text
    assert API_KEY not in (batch.error or "")
    assert API_KEY not in repr(real) and API_KEY not in repr(vars(real))


# ---------------------------------------------------------------------- real SDK, mocked HTTP
def sdk_maker(body: dict[str, Any], seen: dict[str, Any]) -> ClaudeDecisionMaker:
    """A maker whose real AsyncAnthropic client talks to an in-process mock transport."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=body, headers={"request-id": "req_1"})

    client = anthropic.AsyncAnthropic(
        api_key=API_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "medium", FakeFallback(), client=client)


def api_message(content: list[dict[str, Any]], stop_reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 900,
            "cache_read_input_tokens": 1500,
            "cache_creation_input_tokens": 0,
        },
        **extra,
    }


async def test_real_sdk_request_body_and_parse():
    answer = output(decision("BUY", "AMD", size_fraction=0.5), view="Constructive.")
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": answer.model_dump_json()},
    ]
    seen: dict[str, Any] = {}
    batch = await sdk_maker(api_message(content, "end_turn"), seen).decide(base_ctx())

    body = seen["body"]
    assert seen["headers"]["anthropic-beta"] == FALLBACK_BETA
    assert sorted(body) == [
        "fallbacks",
        "max_tokens",
        "messages",
        "model",
        "output_config",
        "system",
    ]
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    assert batch.error is None and batch.market_view == "Constructive."
    assert [(d.action, d.symbol, d.size_fraction) for d in batch.decisions] == [
        (TradeAction.BUY, "AMD", 0.5)
    ]
    assert batch.input_tokens == 3500
    assert batch.cost_usd == pytest.approx((2000 * 4 + 900 * 20 + 1500 * 0.2) / 1e6)


async def test_real_sdk_truncated_output_fails_safe():
    content = [{"type": "text", "text": '{"market_view": "Strong tape", "decisions": [{"act'}]
    batch = await sdk_maker(api_message(content, "max_tokens"), {}).decide(base_ctx())
    assert batch.decisions == []
    assert "likely cut off" in (batch.error or "")
    assert batch.cost_usd >= MAX_TOKENS * 20 / 1e6


async def test_real_sdk_refusal():
    details = {"category": "cyber", "explanation": "declined"}
    body = api_message([], "refusal", stop_details=details)
    batch = await sdk_maker(body, {}).decide(base_ctx())
    assert batch.refused and batch.decisions == []
    assert "category: cyber" in (batch.error or "")


# ---------------------------------------------------------------------- construction
def test_real_client_settings():
    m = ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "low", FakeFallback())
    assert isinstance(m._client, anthropic.AsyncAnthropic)
    assert m._client.timeout == 90.0
    assert m._client.max_retries == 2
    assert m.name == "claude"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"api_key": "  "}, "API key"),
        ({"effort": "extreme"}, "effort"),
        ({"model": ""}, "model"),
    ],
)
def test_constructor_validation(kwargs, message):
    args = {"api_key": API_KEY, "model": "claude-opus-5-5", "effort": "medium"}
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        ClaudeDecisionMaker(fallback=FakeFallback(), **args)


# ---------------------------------------------------------------------- cost math
def test_cost_table():
    m = 1_000_000
    assert estimate_cost("claude-opus-5-5", m, m, m, m) == pytest.approx(4 + 20 + 0.2 + 5)
    assert estimate_cost("claude-sonnet-5-5", m, m, m, m) == pytest.approx(2 + 10 + 0.2 + 2.5)
    assert estimate_cost("claude-haiku-5-5", m, m, m, m) == pytest.approx(0.1 + 0.5 + 0.01 + 0.125)
    assert estimate_cost("claude-opus-5-5", 2500, 1200) == pytest.approx(0.01 + 0.024)


def test_cost_unknown_and_dated_models():
    m = 1_000_000
    opus = estimate_cost("claude-opus-5-5", m, m)
    assert estimate_cost("some-future-model", m, m) == pytest.approx(opus)
    assert estimate_cost("claude-sonnet-5-5-20261001", m, m) == pytest.approx(12.0)
    assert estimate_cost("claude-opus-5-20260601", m, m) == pytest.approx(30.0)  # Opus 5, not 5.5


def test_usage_cost_handles_missing_cache_fields():
    use = SimpleNamespace(input_tokens=100, output_tokens=50, iterations=None)
    use.cache_read_input_tokens = None
    use.cache_creation_input_tokens = None
    assert usage_cost(use, "claude-opus-5-5") == (100, 50, pytest.approx((400 + 1000) / 1e6))


# ---------------------------------------------------------------------- prompts
def test_system_prompt_is_stable_and_cacheable():
    assert isinstance(SYSTEM_PROMPT, str)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", SYSTEM_PROMPT)  # no dates
    assert not re.search(r"\b\d{1,2}:\d{2}\b", SYSTEM_PROMPT)  # no clock times
    assert "UTC" not in SYSTEM_PROMPT
    assert not re.search(r"\b20\d{2}\b", SYSTEM_PROMPT)  # no years
    assert len(SYSTEM_PROMPT) > 2048 * 1.5  # comfortably above the 512-token cache minimum
    for phrase in ("Long only", "Candidates", "stop_loss", "HOLD", "CLOSE", "informational"):
        assert phrase in SYSTEM_PROMPT


def rich_ctx(**overrides: Any) -> DecisionContext:
    symbols = ["SPY", "QQQ", "AAPL", "AMD", "F", "SOFI", "PLTR", "INTC", "BTC", "ETH"]
    candidates = [
        cand(
            s,
            score=0.5 + i * 0.04,
            price=100 + i,
            stop=98 + i,
            target=104 + i,
            rsi14=55.0,
            orb_high=101.2 + i,
        )
        for i, s in enumerate(symbols)
    ]
    market = {
        Instrument(symbol=s, asset_class=c.instrument.asset_class).key: {
            "price": 100.1234 + i,
            "ema9": 99.8,
            "ema21": 99.1,
            "rsi14": 55.0,
            "atr14": 1.2,
            "atr_pct": 0.31,
            "vwap": 99.5,
            "dist_vwap_pct": 0.6,
            "rel_volume": 1.7,
            "day_change_pct": 1.2,
            "high_of_day": 101.0 + i,
            "low_of_day": 98.0 + i,
        }
        for i, (s, c) in enumerate(zip(symbols, candidates, strict=True))
    }
    trades = [
        TradeRecord(
            instrument=Instrument(symbol="AMD"),
            qty=1,
            entry_price=159,
            exit_price=161,
            entry_ts=NOW - timedelta(hours=2),
            exit_ts=NOW - timedelta(hours=1, minutes=i),
            pnl=1.5,
            setup="vwap_reclaim",
            r_multiple=0.8,
            exit_reason="target",
        )
        for i in range(8)
    ]
    args: dict[str, Any] = {
        "candidates": candidates,
        "positions": [pos("SPY", avg=600, last=602), pos("BTC", avg=65000, stop=None)],
        "market": market,
        "day": DayStats(day="2026-10-21", start_equity=500.0, realized_pnl=-4.0, trades=2),
        "equity": 488.0,
    }
    args.update(overrides)
    ctx = make_ctx(**args)
    return ctx.model_copy(update={"recent_trades": trades})


def test_render_context_includes_target_and_loss_limit():
    ctx = rich_ctx(limits=RiskLimits(daily_profit_target_usd=100.0, max_daily_loss_pct=6.0))
    text = render_context(ctx)
    assert "profit target $100.00" in text
    assert "informational" in text
    assert "daily loss limit -$30.00" in text  # 6% of the $500 start equity
    assert "$18.00 left before the halt" in text  # -$12 P&L against a $30 limit
    assert "P&L -$12.00" in text
    assert "-12% of target" in text
    assert "min reward:risk 1.5" in text
    assert "2026-10-21 15:00 UTC (11:00 New York)" in text


def test_render_context_measures_the_loss_limit_from_the_resume():
    """After the owner resumes a halted day, the engine measures the loss limit from the
    equity at the resume, so the AI must not be told that nothing is left."""
    ctx = rich_ctx(limits=RiskLimits(max_daily_loss_pct=6.0), equity=460.0)
    text = render_context(ctx)
    assert "$0.00 left before the halt" in text  # down $40 against a $30 limit
    text = render_context(ctx.model_copy(update={"loss_baseline": 465.0}))
    assert "P&L -$40.00" in text  # the day's P&L still counts from the $500 start
    assert "daily loss limit -$27.90 (6% of $465.00, the equity when the owner resumed" in text
    assert "$22.90 left before the halt" in text  # floor $437.10
    same = render_context(ctx.model_copy(update={"loss_baseline": 500.0}))
    assert "(6% of start equity, binding): $0.00 left" in same


def test_render_context_lists_positions_candidates_and_market():
    text = render_context(rich_ctx())
    assert "STK:SPY qty 1 avg 600 | last 602" in text
    assert "stop NONE" in text  # BTC position without a stop is flagged
    # ...as one the bot did not open (ENG-3): the models are told never to CLOSE it.
    [btc] = [line for line in text.splitlines() if line.startswith("CRYPTO:BTC qty")]
    assert btc.endswith("not opened by the bot: never CLOSE")
    [spy] = [line for line in text.splitlines() if line.startswith("STK:SPY qty")]
    assert "not opened by the bot" not in spy
    assert "## Candidates (BUY only from this list; 10)" in text
    assert "CRYPTO:ETH vwap_reclaim score 0.86 | price 109 stop 107 target 113 R:R 2.00" in text
    assert "orb_high=" in text  # candidate-only features are shown
    assert "## Market (instrument price ema9" in text
    assert "atr14=1.2" in text  # non-standard snapshot keys appear as key=value
    assert text.count("STK:AMD +$1.50 (+0.8R)") == 5  # recent trades capped at 5


def test_render_context_is_compact_and_deterministic():
    ctx = rich_ctx()
    text = render_context(ctx)
    assert text == render_context(ctx)
    assert len(text) < 9000  # roughly < 3k tokens for 10 symbols


def test_render_context_flags_delayed_data_and_halt():
    halted = DayStats(day="2026-10-21", start_equity=500.0, halted=True, halt_reason="daily loss")
    text = render_context(rich_ctx(mode=Mode.LIVE, data_delayed=True, day=halted))
    assert "TRADING HALTED: daily loss" in text
    assert "DELAYED ~15 min" in text
    assert "stock BUYs are blocked in LIVE mode" in text
    assert "mode LIVE" in text


def test_render_context_empty_sections():
    ctx = make_ctx(day=DayStats(day="2026-10-21"))
    text = render_context(ctx)
    assert "## Candidates: none" in text
    assert "## Open positions (0/2): none" in text
    assert "## Recent trades: none" in text
    assert "## Market: no snapshots" in text
    assert "start equity $500.00" in text  # falls back to current equity


def test_render_context_marks_limits_reached_and_tolerates_naive_datetimes():
    naive = pos("AMD").model_copy(
        update={"opened_at": NOW.replace(tzinfo=None) - timedelta(hours=1)}
    )
    ctx = make_ctx(
        positions=[naive, pos("SPY", avg=600)],
        day=DayStats(day="2026-10-21", start_equity=500.0, trades=10),
    )
    text = render_context(ctx)
    assert "## Open positions (2/2) (limit reached: no new BUYs)" in text
    assert "entries 10/10 (limit reached: no new BUYs)" in text
    assert "held 60m" in text


def test_render_context_caps_candidates():
    many = [cand(f"S{i:02d}", score=0.5) for i in range(20)]
    text = render_context(make_ctx(candidates=many))
    assert "(+5 lower-scored candidates omitted)" in text


def test_module_constants():
    assert claude_mod.DEFAULT_MODEL == "claude-opus-5-5"
    assert claude_mod.REQUEST_TIMEOUT_S == 90.0
    assert claude_mod.MAX_RETRIES == 2


# ---------------------------------------------------------------------- 1-hour prompt cache
def test_one_hour_cache_writes_cost_twice_the_input_price() -> None:
    m = 1_000_000
    for model, base in [
        ("claude-opus-5-5", 4.0),
        ("claude-sonnet-5-5", 2.0),
        ("claude-haiku-5-5", 0.10),
        ("claude-opus-5", 5.0),
        ("claude-opus-4-8", 5.0),
    ]:
        assert claude_mod.PRICES[model].cache_write_1h == pytest.approx(2 * base)
        assert estimate_cost(model, 0, 0, cache_write_1h_tokens=m) == pytest.approx(2 * base)
    assert claude_mod.CACHE_CONTROL == {"type": "ephemeral", "ttl": "1h"}


def test_usage_cost_splits_cache_writes_by_ttl() -> None:
    split = SimpleNamespace(ephemeral_5m_input_tokens=1000, ephemeral_1h_input_tokens=3000)
    use = usage(inp=500, out=100, write=4000)
    use.cache_creation = split
    prompt, out, cost = usage_cost(use, "claude-opus-5-5")
    assert (prompt, out) == (4500, 100)
    assert cost == pytest.approx((500 * 4 + 100 * 20 + 1000 * 5 + 3000 * 8) / 1e6)


def test_usage_cost_without_breakdown_charges_one_hour_writes() -> None:
    use = usage(inp=500, out=100, write=4000)  # no usage.cache_creation
    _, _, cost = usage_cost(use, "claude-opus-5-5")
    assert cost == pytest.approx((500 * 4 + 100 * 20 + 4000 * 8) / 1e6)


async def test_real_sdk_one_hour_cache_usage_is_priced() -> None:
    answer = output(decision("HOLD", "AMD"))
    body = api_message([{"type": "text", "text": answer.model_dump_json()}], "end_turn")
    body["usage"].update(
        cache_read_input_tokens=0,
        cache_creation_input_tokens=2500,
        cache_creation={"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 2500},
    )
    batch = await sdk_maker(body, {}).decide(base_ctx())
    assert batch.input_tokens == 2000 + 2500
    assert batch.cost_usd == pytest.approx((2000 * 4 + 900 * 20 + 2500 * 8) / 1e6)


def test_candidates_show_fee_estimates_for_the_pricing_plan():
    """The fee note uses the owner's IBKR plan and the largest size the limits allow (the
    $250 order cap here), so the models weigh real fees instead of a guessed $1 minimum."""
    pfe = cand("PFE", price=30.02, stop=29.90, target=30.16)
    ctx = make_ctx(candidates=[pfe], equity=600.0, day=DayStats(day="2026-10-23", start_equity=600))
    text = render_context(ctx)
    assert "at most 8 sh (~$240.16): round-trip fees ~$0.76 = 68% of the $1.12 reward" in text
    assert "(limit 25%)" in text
    fixed = render_context(ctx.model_copy(update={"commission_plan": "fixed"}))
    assert "round-trip fees ~$2.0" in fixed
    assert "at least $1 per order" not in SYSTEM_PROMPT
