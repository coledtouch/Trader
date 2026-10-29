"""Whose shares are they? The owner may trade the bot's IBKR account by hand (TWS, IBKR
Mobile). IBKR reports one position per symbol, so after the owner buys a symbol the bot
holds, the bot's own quantity comes from the fills journal, never from the position.

Every bot exit (engine stops and targets, time stop, flatten before the close, AI and
approved closes, the dashboard's Close, re-placed stops, the crypto watchdog) sells at most
`sell_cap(...)`. When nothing says the owner bought since the bot's entry was sent, the cap
is None and the exit sells the whole position, exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from trader.db import Database
from trader.models import Fill, Side, exec_revision

# IBKRBroker.fills_since: an execution of another API client (the owner's TWS or IBKR
# Mobile, or the crypto watchdog's own client id). This app's own executions (entries,
# bracket children, closes, re-placed stops) carry their order id.
OTHER_CLIENT_PREFIX = "perm:"
# IBKR stamps executions to the second, and this server's clock may lead IBKR's a little:
# a bot entry that fills at once can carry a time just before its metadata's opened_at.
FILL_TIME_SKEW = timedelta(seconds=5)
EXTRA_PREFIX = "position_extra:"  # the engine's per-position kv (same key as engine.py)
EPS = 1e-9


@dataclass(frozen=True)
class Holding:
    bot: float  # the bot's own quantity
    owner_bought: float  # bought by another client since the bot's entry was sent


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def merge_fills(*sources: Iterable[Fill]) -> list[Fill]:
    """One fill per execution (the latest revision of an IBKR correction)."""
    latest: dict[str, tuple[int, Fill]] = {}
    for source in sources:
        for fill in source:
            base, rev = exec_revision(fill.exec_id) or (fill.exec_id, 0)
            if base not in latest or latest[base][0] < rev:
                latest[base] = (rev, fill)
    return [fill for _, fill in latest.values()]


def bot_holding(fills: Iterable[Fill], since: datetime, *, watchdog_sold: bool) -> Holding:
    """The bot's own quantity of one instrument from its fills since `since` (its entry's
    send time less FILL_TIME_SKEW), in time order. The bot's buys are this app's own
    executions (the bot never adds to a position, so they are its entry). Another client's
    sell comes out of that client's own buys first (the owner's round trip leaves the bot's
    shares alone) and the rest out of the bot's; once the crypto watchdog sold this
    position, every other client's sell comes out of the bot's first (the watchdog's are
    not told apart from the owner's). Errs low, never high, so a cap from it never sells
    the owner's shares."""
    bot = owner = owner_bought = 0.0
    start = _aware(since)
    for fill in sorted(fills, key=lambda f: (_aware(f.ts), f.exec_id)):
        if _aware(fill.ts) < start or fill.qty <= EPS:
            continue
        other = fill.broker_order_id.startswith(OTHER_CLIENT_PREFIX)
        if fill.side == Side.BUY:
            if other:
                owner += fill.qty
                owner_bought += fill.qty
            else:
                bot += fill.qty
            continue
        if not other:  # this app's own exit (a close, a bracket child, a re-placed stop)
            bot = max(0.0, bot - fill.qty)
            continue
        if watchdog_sold:
            from_bot = min(bot, fill.qty)
            bot -= from_bot
            owner = max(0.0, owner - (fill.qty - from_bot))
        else:
            from_owner = min(owner, fill.qty)
            owner -= from_owner
            bot = max(0.0, bot - (fill.qty - from_owner))
    return Holding(bot=bot, owner_bought=owner_bought)


def read_extra(db: Database, key: str) -> dict[str, Any]:
    try:
        data = json.loads(db.get_kv(EXTRA_PREFIX + key) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def entry_qty(extra: Mapping[str, Any], meta: Mapping[str, Any]) -> float | None:
    """Quantity of the bot's entry order for this metadata: the most the bot can hold (it
    never adds to a position). Older rows: the planned risk over the risk per unit."""
    qty = extra.get("qty")
    if isinstance(qty, int | float) and qty > 0:
        return float(qty)
    risk, unit = meta.get("initial_risk"), extra.get("unit_risk")
    if isinstance(risk, int | float) and isinstance(unit, int | float) and risk > 0 and unit > 0:
        return round(risk / unit, 6)
    return None


def sell_cap(
    held: float,
    limit: float | None,
    holding: Holding,
    *,
    unexplained_is_owner: bool = False,
) -> float | None:
    """The most a bot exit may sell of `held`. None: nothing says the owner bought since the
    bot's entry was sent, so everything held is the bot's (sell it all, as before). With
    `unexplained_is_owner` (the watchdog: the app is down, the journal stops when it did) a
    position the journal does not explain is treated as the owner's buy too."""
    owner = holding.owner_bought > EPS or (limit is not None and held > limit + EPS)
    if unexplained_is_owner and held > holding.bot + EPS:
        owner = True
    if not owner:
        return None
    cap = holding.bot if limit is None else min(holding.bot, limit)
    return max(0.0, min(cap, held))


def watchdog_sales(db: Database, key: str, marker: str, opened: datetime | None) -> float | None:
    """What the crypto watchdog sent to sell of this bot position (its `watchdog_close` audit
    rows: the requested quantity, at least what filled), None when it sent nothing. Rows from
    before the marker was recorded count when written after the position was opened."""
    total: float | None = None
    for row in db.list_audit(limit=1000):
        detail = row["detail"]
        if row["action"] != "watchdog_close" or detail.get("instrument") != key:
            continue
        if detail.get("status") in ("rejected", "cancelled"):
            continue
        if detail.get("marker") is not None:
            if detail["marker"] != marker:
                continue
        elif opened is None or _aware(datetime.fromisoformat(row["ts"])) < _aware(opened):
            continue
        qty = detail.get("qty")
        total = (total or 0.0) + (float(qty) if isinstance(qty, int | float) else 0.0)
    return total
