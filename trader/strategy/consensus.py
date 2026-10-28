"""Consensus decision maker: two models must agree before money goes in.

The owner's rule: a BUY goes out only when Claude AND Grok both BUY the same symbol.
Both makers get the same context and run concurrently. Their (already validated)
decisions are merged symbol by symbol:

- BUY only when both BUY the same instrument. The primary's (Claude's) order levels are
  used (limit, stop, target, setup); conviction and size_fraction are the smaller of the
  two, so a lukewarm second opinion can only shrink the position.
- CLOSE when EITHER says CLOSE: an exit reduces risk, so one vote is enough. That includes
  the rule-based exits a maker returns during an API outage.
- Otherwise HOLD, with a short reason ("consensus: Grok said HOLD") that the engine writes
  into the signal ledger, so a month of skipped signals shows who blocked what.
- If either maker fails or refuses, there are no BUYs at all this cycle (the other's CLOSEs
  are still honoured).

The merged batch costs the sum of both calls (the engine's daily AI budget sees the
combined figure), and keeps each model's own decisions in ``votes`` for the journal/UI.
"""

from __future__ import annotations

import asyncio
import logging

from trader.models import (
    AIDecision,
    DecisionBatch,
    DecisionContext,
    Instrument,
    OrderType,
    TradeAction,
)
from trader.strategy.common import MAX_MARKET_VIEW, MAX_RATIONALE, clip, is_failure
from trader.strategy.decide import DecisionMaker

log = logging.getLogger(__name__)

LABELS = {"claude": "Claude", "grok": "Grok", "rules": "Rules"}
PREFIX = "consensus: "  # every HOLD reason this maker writes starts with this
_VIEW_SHARE = (MAX_MARKET_VIEW - 20) // 2


def label(name: str) -> str:
    return LABELS.get(name, name.title())


def _key(d: AIDecision) -> str:
    return Instrument(symbol=d.symbol.strip().upper(), asset_class=d.asset_class).key


class _Side:
    """One maker's batch, indexed by instrument."""

    def __init__(self, name: str, batch: DecisionBatch) -> None:
        self.name = name
        self.label = label(name)
        self.batch = batch
        self.failed = is_failure(batch)
        self.buys: dict[str, AIDecision] = {}
        self.closes: dict[str, AIDecision] = {}
        self.holds: dict[str, AIDecision] = {}
        for d in batch.decisions:
            bucket = {
                TradeAction.BUY: self.buys,
                TradeAction.CLOSE: self.closes,
                TradeAction.HOLD: self.holds,
            }[d.action]
            bucket.setdefault(_key(d), d)

    def why_not(self) -> str:
        """Why this side gives no BUY vote on an instrument the other side wants."""
        if self.batch.refused:
            return f"{self.label} refused"
        if self.failed:
            return f"{self.label} failed"
        return ""

    def view(self) -> str:
        if self.batch.market_view:
            return clip(self.batch.market_view, _VIEW_SHARE)
        if self.batch.refused:
            return "refused"
        return "unavailable" if self.failed else "no view"


class ConsensusDecisionMaker:
    name = "consensus"

    def __init__(self, primary: DecisionMaker, secondary: DecisionMaker) -> None:
        if primary.name == secondary.name:
            raise ValueError("consensus needs two different decision makers")
        self.primary = primary
        self.secondary = secondary

    @property
    def model(self) -> str:
        return "+".join(
            str(getattr(m, "model", None) or m.name) for m in (self.primary, self.secondary)
        )

    async def decide(self, ctx: DecisionContext) -> DecisionBatch:
        first, second = await asyncio.gather(_ask(self.primary, ctx), _ask(self.secondary, ctx))
        return merge(_Side(self.primary.name, first), _Side(self.secondary.name, second), ctx)


async def _ask(maker: DecisionMaker, ctx: DecisionContext) -> DecisionBatch:
    """The maker's batch; an exception becomes a failed batch (no decisions)."""
    model = getattr(maker, "model", None)  # before the call: Settings may change it meanwhile
    try:
        return await maker.decide(ctx)
    except Exception as exc:
        log.exception("decision maker %s failed inside consensus", maker.name)
        return DecisionBatch(
            ts=ctx.now,
            maker=maker.name,
            model=model,
            candidates=ctx.candidates,
            error=f"unexpected error: {type(exc).__name__}",
        )


def merge(p: _Side, s: _Side, ctx: DecisionContext) -> DecisionBatch:
    """Combine two makers' batches by the consensus rules (see the module docstring)."""
    decisions: list[AIDecision] = []
    done: set[str] = set()

    # Exits: one vote is enough.
    for key in [*p.closes, *(k for k in s.closes if k not in p.closes)]:
        sides = [side for side in (p, s) if key in side.closes]
        votes = [side.closes[key] for side in sides]
        why = " | ".join(f"{side.label}: {side.closes[key].rationale}" for side in sides)
        decisions.append(
            votes[0].model_copy(
                update={
                    "order_type": OrderType.MARKET,
                    "conviction": max(v.conviction for v in votes),
                    "rationale": clip(why, MAX_RATIONALE),
                }
            )
        )
        done.add(key)

    # Entries: both must BUY the same instrument, and neither may have failed.
    blocked = p.why_not() or s.why_not()
    for key, mine in p.buys.items():
        theirs = s.buys.get(key)
        if key in done or theirs is None or blocked:
            continue
        decisions.append(
            mine.model_copy(
                update={
                    "conviction": min(mine.conviction, theirs.conviction),
                    "size_fraction": min(mine.size_fraction, theirs.size_fraction),
                    "rationale": clip(
                        f"{p.label}: {mine.rationale} | {s.label}: {theirs.rationale}",
                        MAX_RATIONALE,
                    ),
                }
            )
        )
        done.add(key)

    # Everything else either side voted on becomes a HOLD that says who stood in the way.
    for key in [*p.buys, *s.buys, *p.holds, *s.holds]:
        if key in done:
            continue
        done.add(key)
        decisions.append(_hold(key, p, s))

    errors = [
        f"{side.label}: {side.batch.error}" for side in (p, s) if side.batch.error is not None
    ]
    return DecisionBatch(
        ts=ctx.now,
        maker=ConsensusDecisionMaker.name,
        model="+".join(m for m in (p.batch.model, s.batch.model) if m) or None,
        market_view=clip(f"{p.label}: {p.view()} | {s.label}: {s.view()}", MAX_MARKET_VIEW),
        decisions=decisions,
        candidates=ctx.candidates,
        input_tokens=p.batch.input_tokens + s.batch.input_tokens,
        output_tokens=p.batch.output_tokens + s.batch.output_tokens,
        cost_usd=p.batch.cost_usd + s.batch.cost_usd,
        refused=p.batch.refused or s.batch.refused,
        error="; ".join(errors) or None,
        votes={p.name: list(p.batch.decisions), s.name: list(s.batch.decisions)},
        vote_costs={p.name: p.batch.cost_usd, s.name: s.batch.cost_usd},
        vote_failures={
            side.name: side.batch.error or ("refused" if side.batch.refused else "failed")
            for side in (p, s)
            if side.failed
        },
    )


def _hold(key: str, p: _Side, s: _Side) -> AIDecision:
    votes = [d for side in (p, s) for d in (side.buys.get(key), side.holds.get(key)) if d]
    base = votes[0]
    buyers = [side for side in (p, s) if key in side.buys]
    if len(buyers) == 2:  # both wanted it, but a failure blocks every BUY
        reason = p.why_not() or s.why_not()
    elif buyers:
        buyer = buyers[0]
        other = s if buyer is p else p
        if other.why_not() or buyer.why_not():
            reason = other.why_not() or buyer.why_not()
        elif key in other.holds:
            reason = f"{other.label} said HOLD"
        else:
            reason = f"{other.label} did not BUY"
    else:
        holders = [side.label for side in (p, s) if key in side.holds]
        reason = "both said HOLD" if len(holders) == 2 else f"{holders[0]} said HOLD"
    return AIDecision(
        action=TradeAction.HOLD,
        symbol=base.symbol,
        asset_class=base.asset_class,
        conviction=min(v.conviction for v in votes),
        setup=base.setup,
        rationale=PREFIX + reason,
    )
