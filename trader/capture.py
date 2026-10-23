"""Bounded, local research observations. No model calls or broker orders."""

from __future__ import annotations

import hashlib
import json
import logging

from trader.db import Database
from trader.models import Bar, BotSettings, DecisionBatch, DecisionContext, Quote

log = logging.getLogger(__name__)


def capture_cycle(
    db: Database,
    ctx: DecisionContext,
    quotes: dict[str, Quote | None],
    bars: dict[str, list[Bar]],
    batch: DecisionBatch,
    settings: BotSettings,
) -> None:
    """Persist what was observed, not revised history or fabricated historical AI votes.

    The replay explicitly reports sparse monitoring and original-account conditioning.
    Store original scanner proposals and indicator features, not repeated historical
    bars. The paired replay consumes those snapshots directly; historical rescanning
    requires the separate CSV route. Retention is 2,500 cycles. A capture failure must
    never interrupt position protection.
    """
    try:
        encoded = settings.model_dump(mode="json")
        cohort_data = {
            "account": ctx.account.account_id,
            "mode": ctx.mode.value,
            "settings": encoded,
            "commission_plan": ctx.commission_plan,
            "account_epoch": db.get_kv("journal_account_since"),
        }
        cohort = hashlib.sha256(json.dumps(cohort_data, sort_keys=True).encode()).hexdigest()
        payload = {
            "ts": ctx.now.isoformat(),
            "source": "captured",
            "capture_format": "scanner_snapshot_v1",
            "decision_cycle": True,
            "account_id": ctx.account.account_id,
            "account_mode": ctx.mode.value,
            "starting_equity": ctx.account.equity,
            "commission_plan": ctx.commission_plan,
            "cohort": cohort,
            "settings": encoded,
            "quotes": {
                key: quote.model_dump(mode="json")
                for key, quote in quotes.items()
                if quote is not None
            },
            "bars": {},
            "candidates": [c.model_dump(mode="json") for c in ctx.candidates],
            "batch": batch.model_dump(mode="json"),
            "market": ctx.market,
        }
        db.add_research_frame(payload)
    except Exception:
        log.exception("research capture failed; trading protection continues")
