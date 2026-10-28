import asyncio
import json
import logging
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import TypeAdapter

from tests.test_decide import NOW, cand, make_ctx, pos
from trader.models import (
    AssetClass,
    Candidate,
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
class FakeStream:
    """What `client.beta.messages.stream(...)` returns: an async context manager."""

    def __init__(self, result: Any, delay: float = 0.0, open_delay: float = 0.0):
        self.result, self.delay, self.open_delay = result, delay, open_delay
        self.closed = False

    async def __aenter__(self) -> "FakeStream":
        if self.open_delay:  # still connecting (or in the SDK's retry backoff)
            await asyncio.sleep(self.open_delay)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.closed = True

    async def get_final_message(self) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeMessages:
    def __init__(self, result: Any, delay: float = 0.0, open_delay: float = 0.0):
        self.result, self.delay, self.open_delay = result, delay, open_delay
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        self.calls.append(kwargs)
        self.streams.append(FakeStream(self.result, self.delay, self.open_delay))
        return self.streams[-1]


class FakeClient:
    def __init__(self, result: Any, delay: float = 0.0, open_delay: float = 0.0):
        self.messages = FakeMessages(result, delay, open_delay)
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
    text = [SimpleNamespace(type="text", text=parsed.model_dump_json())] if parsed else []
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=use or usage(),
        content=text,
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
    # The schema the SDK's output_format helper would send; the bot parses the answer.
    schema = anthropic.transform_schema(TypeAdapter(ClaudeDecisions).json_schema())
    assert kwargs["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": schema},
    }
    assert kwargs["betas"] == [FALLBACK_BETA] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["system"] == [
        {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert kwargs["messages"] == [{"role": "user", "content": render_context(ctx)}]
    for forbidden in ("temperature", "top_p", "top_k", "thinking", "tool_choice", "output_format"):
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


async def test_invalid_json_answer_fails_safe_with_its_real_cost():
    bad = response()
    bad.content = [SimpleNamespace(type="text", text='{"market_view": "cut')]
    batch = await maker(FakeClient(bad)).decide(base_ctx())
    assert batch.decisions == []
    assert "not valid JSON" in (batch.error or "")
    assert batch.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


async def test_deadline_while_still_connecting_charges_nothing():
    """Review finding: no stream ever opened (connecting, or the SDK's retry backoff), so
    nothing was billed and the daily AI budget must not lose a phantom worst case."""
    client = FakeClient(response(output(decision("BUY", "AMD"))), open_delay=5)
    m = ClaudeDecisionMaker(
        API_KEY,
        "claude-opus-5-5",
        "high",
        FakeFallback(DecisionBatch(maker="rules")),
        client,
        deadline_s=0.05,
    )
    batch = await m.decide(base_ctx())
    assert "did not answer within" in (batch.error or "")
    assert batch.cost_usd == 0 and batch.output_tokens == 0


async def test_no_answer_within_the_deadline_keeps_exits_and_charges_worst_case():
    """A Claude answer later than the deadline would be stale: the stream is cancelled,
    the fallback's exits still go out, and the unknown cost is charged as a worst case."""
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
    client = FakeClient(response(output(decision("BUY", "AMD"))), delay=5)
    m = ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "high", fallback, client, deadline_s=0.05)
    batch = await m.decide(ctx)
    assert "did not answer within" in (batch.error or "")
    assert [d.action for d in batch.decisions] == [TradeAction.CLOSE]  # no BUY
    assert fallback.calls == 1
    assert client.messages.streams[0].closed  # the stream was cancelled and closed
    assert batch.output_tokens == MAX_TOKENS and batch.cost_usd >= MAX_TOKENS * 20 / 1e6


@pytest.mark.parametrize("delay", [0.2, 5])  # answers, or passes the deadline
async def test_model_change_mid_call_keeps_the_calls_model(caplog, delay):
    """Review finding: Settings swapped the model while a call ran, and the call was priced
    and labelled at the new model. The call keeps the model and effort it started with."""
    client = FakeClient(response(output(decision("BUY", "AMD"))), delay=delay)
    fallback = FakeFallback(DecisionBatch(maker="rules"))
    m = ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "high", fallback, client, deadline_s=0.5)
    opened = client.messages.stream

    def open_then_switch(**kwargs: Any) -> FakeStream:
        stream = opened(**kwargs)
        m.model, m.effort = "claude-haiku-5-5", "low"  # saved in Settings mid-call
        return stream

    client.messages.stream = open_then_switch
    with caplog.at_level(logging.INFO):
        batch = await m.decide(base_ctx())
    request = client.messages.calls[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "high"
    assert "fallbacks" in request  # Opus, so the server-side fallback stays on
    assert batch.model == "claude-opus-5-5"
    assert "fallback model" not in caplog.text
    if delay > m.deadline_s:
        assert "did not answer within" in (batch.error or "")
        prompt_tokens = batch.input_tokens
        assert batch.cost_usd == pytest.approx(
            estimate_cost("claude-opus-5-5", prompt_tokens, MAX_TOKENS)
        )
    else:
        assert batch.error is None and len(batch.decisions) == 1
        assert batch.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


@pytest.mark.parametrize("accepted", [True, False])
async def test_cancelled_call_leaves_its_estimate_for_the_engine(accepted):
    """Review finding: Stop or shutdown cancels decide() mid-answer, so no batch carries the
    cost. A call Claude had accepted leaves its worst-case estimate (taken once) for the
    engine to charge; one still connecting was never billed and leaves nothing."""
    client = FakeClient(
        response(output(decision("BUY", "AMD"))),
        delay=5 if accepted else 0,
        open_delay=0 if accepted else 5,
    )
    m = maker(client)
    ctx = base_ctx()
    task = asyncio.create_task(m.decide(ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.messages.streams[0].closed == accepted  # an opened stream is closed
    if accepted:
        expected = m._worst_case(render_context(ctx), "claude-opus-5-5").cost
        assert expected >= MAX_TOKENS * 20 / 1e6
        assert m.take_abandoned_cost() == pytest.approx(expected)
    assert m.take_abandoned_cost() == 0


async def test_answer_time_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger="trader.strategy.claude"):
        await maker(FakeClient(response(output(decision("BUY", "AMD"))))).decide(base_ctx())
    assert re.search(r"Claude answered in \d+s of 140s \(1000 in, 500 out tokens", caplog.text)


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
    monkeypatch.setattr(real._client.beta.messages, "stream", fake.stream)
    with caplog.at_level(logging.DEBUG):
        batch = await real.decide(base_ctx())
    assert batch.decisions == []
    assert API_KEY not in caplog.text
    assert API_KEY not in (batch.error or "")
    assert API_KEY not in repr(real) and API_KEY not in repr(vars(real))


# ---------------------------------------------------------------------- real SDK, mocked HTTP
def sse(message: dict[str, Any]) -> bytes:
    """A Messages API answer as the server-sent events of a streamed response."""
    usage = message["usage"]
    start = {**message, "content": [], "stop_reason": None, "stop_details": None}
    start["usage"] = {**usage, "output_tokens": 1}
    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": start})
    ]
    for i, block in enumerate(message["content"]):
        if block["type"] == "thinking":
            empty = {"type": "thinking", "thinking": "", "signature": ""}
            delta = {"type": "signature_delta", "signature": block["signature"]}
        elif block["type"] == "text":
            empty = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:  # e.g. a fallback hand-off: whole in its start event
            empty, delta = block, None
        start_block = {"type": "content_block_start", "index": i, "content_block": empty}
        events.append(("content_block_start", start_block))
        if delta is not None:
            body = {"type": "content_block_delta", "index": i, "delta": delta}
            events.append(("content_block_delta", body))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    final = {
        "stop_reason": message["stop_reason"],
        "stop_sequence": None,
        "stop_details": message.get("stop_details"),
    }
    out = {"output_tokens": usage["output_tokens"]}
    events += [
        ("message_delta", {"type": "message_delta", "delta": final, "usage": out}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def sdk_maker(body: dict[str, Any], seen: dict[str, Any]) -> ClaudeDecisionMaker:
    """A maker whose real AsyncAnthropic client talks to an in-process mock transport."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            content=sse(body),
            headers={"request-id": "req_1", "content-type": "text/event-stream"},
        )

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
        "stream",
        "system",
    ]
    assert body["stream"] is True  # a long answer must not trip the HTTP read timeout
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
    assert "max_tokens" in (batch.error or "")
    assert batch.cost_usd == pytest.approx((2000 * 4 + 900 * 20 + 1500 * 0.2) / 1e6)


def _hand_off(to: str) -> dict[str, Any]:
    return {
        "type": "fallback",
        "from": {"model": "claude-opus-5-5"},
        "to": {"model": to},
        "trigger": {"type": "refusal", "category": "cyber"},
    }


@pytest.mark.parametrize("shape", ["continuation", "two_hops", "restart"])
async def test_real_sdk_refusal_mid_answer_keeps_the_fallback_models_answer(shape):
    """Review findings: with server-side fallback, a refusal part way through a streamed
    answer hands over to the fallback model, which continues from the partial text (so the
    answer is every text block joined). A fallback that restarts instead sends a whole answer
    after the `fallback` block. Both must keep the answer."""
    answer = output(decision("BUY", "AMD", size_fraction=0.5), view="Constructive.")
    raw = answer.model_dump_json()
    k1, k2 = len(raw) // 3, 2 * len(raw) // 3

    def text(part: str) -> dict[str, Any]:
        return {"type": "text", "text": part}

    content = {
        "continuation": [text(raw[:k1]), _hand_off("claude-opus-4-8"), text(raw[k1:])],
        "two_hops": [
            text(raw[:k1]),
            _hand_off("claude-opus-5"),
            text(raw[k1:k2]),
            _hand_off("claude-opus-4-8"),
            text(raw[k2:]),
        ],
        "restart": [text(raw[:k1]), _hand_off("claude-opus-4-8"), text(raw)],
    }[shape]
    batch = await sdk_maker(api_message(content, "end_turn"), {}).decide(base_ctx())
    assert batch.error is None, batch.error
    assert batch.model == "claude-opus-4-8"
    assert batch.market_view == "Constructive."
    assert [(d.action, d.symbol) for d in batch.decisions] == [(TradeAction.BUY, "AMD")]


def broken_sdk_maker(
    chunks: list[bytes], exc: Exception | None, fallback: Any
) -> ClaudeDecisionMaker:
    """The SSE body yields `chunks`, then raises `exc` (a connection lost mid-answer)."""

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                yield chunk
            if exc is not None:
                raise exc

    def handler(request: httpx2.Request) -> httpx2.Response:
        headers = {"request-id": "req_1", "content-type": "text/event-stream"}
        return httpx2.Response(200, stream=Body(), headers=headers)

    client = anthropic.AsyncAnthropic(
        api_key=API_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "medium", fallback, client=client)


def _start_event() -> bytes:
    start = api_message([], "end_turn")
    start.update(content=[], stop_reason=None)
    event = {"type": "message_start", "message": start}
    return f"event: message_start\ndata: {json.dumps(event)}\n\n".encode()


async def _exits_fallback() -> tuple[DecisionContext, FakeFallback]:
    ctx = base_ctx()
    rules_batch = await RuleBasedDecisionMaker().decide(
        make_ctx(
            candidates=ctx.candidates,
            positions=[pos("AMD")],
            market={"STK:AMD": {"price": 99.0, "vwap": 100.0}},
        )
    )
    return ctx, FakeFallback(rules_batch)


async def test_real_sdk_stop_mid_answer_leaves_the_estimate():
    """Review finding, with the real SDK: message_start arrived (the call is billed), then
    Stop cancels the cycle. The stream closes and the estimate waits for the engine."""
    asked_for_more = asyncio.Event()

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield _start_event()
            asked_for_more.set()  # the SDK read message_start and waits for the answer
            await asyncio.Event().wait()
            yield b""  # unreachable

    def handler(request: httpx2.Request) -> httpx2.Response:
        headers = {"request-id": "req_1", "content-type": "text/event-stream"}
        return httpx2.Response(200, stream=Body(), headers=headers)

    client = anthropic.AsyncAnthropic(
        api_key=API_KEY,
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    m = ClaudeDecisionMaker(API_KEY, "claude-opus-5-5", "medium", FakeFallback(), client=client)
    ctx = base_ctx()
    task = asyncio.create_task(m.decide(ctx))
    await asyncio.wait_for(asked_for_more.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    expected = m._worst_case(render_context(ctx), "claude-opus-5-5").cost
    assert m.take_abandoned_cost() == pytest.approx(expected)


async def test_real_sdk_overloaded_error_mid_stream_keeps_exits():
    """Review finding: an `event: error` after the 200 arrives as APIStatusError(200); it
    must count as transient, like the 529 it replaces."""
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    chunks = [_start_event(), f"event: error\ndata: {json.dumps(error)}\n\n".encode()]
    ctx, fallback = await _exits_fallback()
    batch = await broken_sdk_maker(chunks, None, fallback).decide(ctx)
    assert fallback.calls == 1
    assert [d.action for d in batch.decisions] == [TradeAction.CLOSE]
    assert "exits only" in (batch.error or "")


@pytest.mark.parametrize(
    ("started", "exc"),
    [
        (True, httpx2.RemoteProtocolError("peer closed connection")),
        (True, httpx2.ReadTimeout("read timed out")),
        (True, None),  # the body just ends after message_start
        (False, None),  # the body ends before message_start (the SDK asserts)
    ],
)
async def test_real_sdk_answer_broken_off_mid_stream_keeps_exits_and_charges(started, exc):
    """Review findings: a connection lost, stalled or ended while reading the open stream
    came out as an 'unexpected error' (no exits, no cost)."""
    ctx, fallback = await _exits_fallback()
    chunks = [_start_event()] if started else []
    batch = await broken_sdk_maker(chunks, exc, fallback).decide(ctx)
    assert fallback.calls == 1
    assert [d.action for d in batch.decisions] == [TradeAction.CLOSE]
    assert "broke off" in (batch.error or "")
    assert batch.output_tokens == MAX_TOKENS and batch.cost_usd >= MAX_TOKENS * 20 / 1e6


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


def test_render_context_with_no_profit_target():
    text = render_context(rich_ctx())  # the default: no target
    assert "profit target: none (only the daily loss limit stops new entries)" in text
    assert "$0.00 (0% of start equity)" not in text


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
    assert "(+10 lower-scored candidates omitted)" in text


def test_market_rows_put_positions_and_candidates_first():
    """With a large universe the row limit cut alphabetically, so a held ZION or a
    candidate XOM never reached the model. Held, then shown candidates, then index ETFs."""
    snap = {"price": 10.0, "ema9": 10.0}
    market = {f"STK:A{i:02d}": dict(snap) for i in range(60)}
    for key in ("STK:ZION", "STK:XOM", "STK:SPY", "STK:YUM"):
        market[key] = dict(snap)
    ctx = make_ctx(
        positions=[pos("ZION")],
        candidates=[cand("XOM", score=0.9), cand("YUM", score=0.1)],
        market=market,
    )
    rows = render_context(ctx).split("## Market")[1].splitlines()[1:]
    keys = [row.split()[0] for row in rows if row.startswith("STK:")]
    assert keys[:4] == ["STK:ZION", "STK:XOM", "STK:YUM", "STK:SPY"]
    assert len(keys) == 30 and keys[4] == "STK:A00"
    assert "(+34 more instruments omitted)" in rows


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
    assert "at most 8 sh (~$240.16): round-trip fees ~$0.76 = 67.8% of the $1.12 reward" in text
    assert "(over the 25% limit: the risk manager rejects it)" in text
    ok = cand("F", price=12.00, stop=11.80, target=12.60)  # 20 sh: $0.84 vs a $12 reward
    assert "= 7.0% of the $12.00 reward (limit 25%)" in render_context(
        ctx.model_copy(update={"candidates": [ok]})
    )
    fixed = render_context(ctx.model_copy(update={"commission_plan": "fixed"}))
    assert "round-trip fees ~$2.0" in fixed
    assert "at least $1 per order" not in SYSTEM_PROMPT


def test_fee_share_that_rounds_to_the_limit_is_flagged_as_over_it():
    """Review finding: 25.18% printed as "25% (limit 25%)" although the risk check rejects
    it. The flag uses the risk check's own comparison."""
    eth = Candidate(
        instrument=Instrument(symbol="ETH", asset_class=AssetClass.CRYPTO),
        setup="x",
        score=0.7,
        price=2500.0,
        suggested_stop=2410.0,
        suggested_target=2639.0,
    )
    ctx = make_ctx(candidates=[eth], equity=600.0, day=DayStats(day="2026-10-21", start_equity=600))
    text = render_context(ctx)
    assert "round-trip fees ~$3.50 = 25.2% of the $13.90 reward" in text
    assert "(over the 25% limit: the risk manager rejects it)" in text


def test_recent_delayed_data_trades_are_marked_for_the_models():
    from datetime import timedelta

    trade = TradeRecord(
        instrument=Instrument(symbol="F"),
        qty=10,
        entry_price=10,
        exit_price=9.8,
        entry_ts=NOW - timedelta(hours=2),
        exit_ts=NOW - timedelta(hours=1),
        pnl=-2.8,
        setup="vwap_reclaim",
        delayed_data=True,
    )
    ctx = make_ctx().model_copy(update={"recent_trades": [trade]})
    assert "paper on delayed data: not evidence for the setup" in render_context(ctx)
