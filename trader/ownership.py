"""Whose shares are they? The owner may trade the bot's IBKR account by hand (TWS, IBKR
Mobile). IBKR reports one position per symbol, so after the owner buys a symbol the bot
holds, the bot's own quantity comes from the fills, never from the position.

Every bot exit (engine stops and targets, time stop, flatten before the close, AI and
approved closes, the dashboard's Close, re-placed stops, the crypto watchdog) sells at most
`sell_cap(...)`. When nothing says the owner bought since the bot's entry was sent, the cap
is None and the exit sells the whole position, exactly as before. With the cap, an exit
sells at most the bot's own units (`Cap.qty`) and never into the owner's known units
(`Cap.keep`): the broker sizes the sell again on a fresh position read after its cancels,
so a bracket child that fills meanwhile only shrinks it.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trader.db import Database
from trader.models import Fill, Side, exec_revision

# IBKRBroker.fills_since: an execution of another API client (the owner's TWS or IBKR
# Mobile, or the crypto watchdog's own client id). This app's own executions (entries,
# bracket children, closes, re-placed stops) carry their order id.
OTHER_CLIENT_PREFIX = "perm:"
# IBKR stamps executions to the second, and this server's clock may run ahead of or behind
# IBKR's: a bot entry that fills at once can carry a time before its metadata's opened_at.
FILL_TIME_SKEW = timedelta(seconds=60)
EXTRA_PREFIX = "position_extra:"  # the engine's per-position kv (same key as engine.py)
# kv: {"marker", "first", "orders": {broker order id: qty}}, the crypto watchdog's sells of
# one bot position (an IOC sell may fill in part: the watchdog reads what each one filled).
WATCHDOG_SALES_PREFIX = "watchdog_sales:"
EPS = 1e-9


@dataclass(frozen=True)
class Split:
    bot: float = 0.0  # the bot's own units still held
    owner: float = 0.0  # the owner's units still held
    owner_bought: float = 0.0  # bought by another client in the window
    bot_bought: float = 0.0  # bought by this app in the window (its entry's fills)
    unseen: float = 0.0  # of the bot's entry quantity, what no fill shows and none sold
    bot_fills: list[Fill] = field(default_factory=list)  # the bot's part of each fill
    flat_at: datetime | None = None  # last fill after which neither side held anything


@dataclass(frozen=True)
class Cap:
    qty: float  # most the bot may sell
    keep: float  # the owner's known units: never sell below held - keep


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def is_other_client(fill: Fill) -> bool:
    return fill.broker_order_id.startswith(OTHER_CLIENT_PREFIX)


def merge_fills(*sources: Iterable[Fill]) -> list[Fill]:
    """One fill per execution (the latest revision of an IBKR correction)."""
    latest: dict[str, tuple[int, Fill]] = {}
    for source in sources:
        for fill in source:
            base, rev = exec_revision(fill.exec_id) or (fill.exec_id, 0)
            if base not in latest or latest[base][0] < rev:
                latest[base] = (rev, fill)
    return [fill for _, fill in latest.values()]


def split_fills(
    fills: Iterable[Fill],
    *,
    bot_first_from: datetime | None = None,
    entry: float | None = None,
) -> Split:
    """Who holds what of one instrument after `fills` (the window from the bot's entry, less
    FILL_TIME_SKEW), in time order. The bot's buys are this app's own executions (the bot
    never adds to a position, so they are its entry); another client's buys are the
    owner's. This app's own sell comes out of the bot's units first. Another client's sell
    comes out of the owner's first (the owner's round trip leaves the bot's shares alone),
    except from `bot_first_from` on: the crypto watchdog sold this position then, and its
    sells are not told apart from the owner's. Errs low on the bot's side, never high.

    `entry` (the bot's entry quantity): the part of it no buy here shows may have filled
    unseen (on a day IBKR no longer reports). A sell of the bot's beyond its seen units
    comes out of that part before the owner's (`Split.unseen` is what is left of it)."""
    bot = owner = owner_bought = bot_bought = 0.0
    ordered = sorted(fills, key=lambda f: (_aware(f.ts), f.exec_id))
    seen = sum(f.qty for f in ordered if f.side == Side.BUY and not is_other_client(f))
    unseen = max(0.0, entry - seen) if entry is not None else 0.0
    out: list[Fill] = []
    flat_at: datetime | None = None
    start = _aware(bot_first_from) if bot_first_from is not None else None
    for f in ordered:
        q = f.qty
        if q <= EPS:
            continue
        other = is_other_client(f)
        if f.side == Side.BUY:
            if other:
                owner += q
                owner_bought += q
            else:
                bot += q
                bot_bought += q
                out.append(f)
            continue
        if not other or (start is not None and _aware(f.ts) >= start):
            a = min(q, bot)
            bot -= a
            u = min(q - a, unseen)
            unseen -= u
            b = min(q - a - u, owner)
            owner -= b
            on_bot = q - b
        else:
            o = min(q, owner)
            owner -= o
            a = min(q - o, bot)
            bot -= a
            on_bot = q - o
        if on_bot > EPS:
            out.append(
                f
                if on_bot >= q - EPS
                else f.model_copy(update={"qty": on_bot, "commission": f.commission * on_bot / q})
            )
        if bot <= EPS and owner <= EPS:
            flat_at = _aware(f.ts)
    return Split(
        bot=bot,
        owner=owner,
        owner_bought=owner_bought,
        bot_bought=bot_bought,
        unseen=unseen,
        bot_fills=out,
        flat_at=flat_at,
    )


def less_sold(split: Split, sold: float) -> Split:
    """`split` after sales of the bot's that no fill in it shows (the crypto watchdog's):
    out of its seen units first, then out of the unseen part of its entry."""
    from_bot = min(max(0.0, sold), split.bot)
    from_unseen = min(max(0.0, sold) - from_bot, split.unseen)
    return dataclasses.replace(split, bot=split.bot - from_bot, unseen=split.unseen - from_unseen)


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
    if isinstance(qty, int | float) and not isinstance(qty, bool) and qty > 0:
        return float(qty)
    risk, unit = meta.get("initial_risk"), extra.get("unit_risk")
    if isinstance(risk, int | float) and isinstance(unit, int | float) and risk > 0 and unit > 0:
        return round(risk / unit, 6)
    return None


def _tol(qty: float) -> float:
    return max(EPS, 1e-6 * abs(qty))


def owner_evidence(held: float, limit: float | None, split: Split) -> bool:
    """Something says the owner bought next to the bot: another client's buy in the
    window, or more held than the bot's entry order asked for."""
    return split.owner_bought > EPS or (limit is not None and held > limit + _tol(limit))


def sell_cap(held: float, limit: float | None, split: Split) -> Cap | None:
    """How much of `held` a bot exit may sell. None: nothing says the owner bought since
    the bot's entry was sent, so everything held is the bot's (sell it all, as before).

    Part of the bot's entry may be in no fill read (it filled on a day IBKR no longer
    reports, or while the app was down; `split` from split_fills(..., entry=limit)): units
    no fill explains count as the bot's when they fit in that part, so its own position
    keeps its exits. More than that, and whose they are is unknown: the bot sells only
    what its fills show."""
    if not owner_evidence(held, limit, split):
        return None
    mine = split.bot
    if limit is not None:
        unexplained = held - split.bot - split.owner
        tol = _tol(limit)
        if split.unseen > tol and tol < unexplained <= split.unseen + tol:
            mine += unexplained
        mine = min(mine, limit + tol)
    return Cap(qty=max(0.0, min(mine, held)), keep=max(0.0, split.owner))


def read_sales_record(db: Database, key: str, marker: str) -> dict[str, Any] | None:
    """The watchdog's sales record for the bot position `marker` names, if any."""
    try:
        rec = json.loads(db.get_kv(WATCHDOG_SALES_PREFIX + key) or "null")
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("marker") != marker:
        return None
    return rec


def note_watchdog_sale(
    db: Database,
    key: str,
    marker: str,
    order_id: str,
    qty: float,
    at: datetime,
    *,
    done: bool = False,
) -> None:
    """Record a sell of the watchdog's for this bot position: what it asked for while the
    order works, what filled once it is `done` (then never read from the broker again)."""
    rec = read_sales_record(db, key, marker) or {
        "marker": marker,
        "first": _aware(at).isoformat(),
        "orders": {},
    }
    rec["orders"][order_id] = qty
    if done:
        rec["done"] = sorted({*rec.get("done", []), order_id})
    db.set_kv(WATCHDOG_SALES_PREFIX + key, json.dumps(rec))


def watchdog_sales(
    db: Database, key: str, marker: str, opened: datetime | None
) -> tuple[datetime, float] | None:
    """When the crypto watchdog first sent a sell for this bot position, and how much it
    sold in all (what filled of an order that is done, what it asked of one still
    working). The kv record first; audit rows of a watchdog from before the record existed
    otherwise (by marker, or for rows without one, from the position's opened_at on)."""
    rec = read_sales_record(db, key, marker)
    if rec is not None:
        first = _aware(datetime.fromisoformat(rec["first"]))
        return first, float(sum(float(q) for q in rec.get("orders", {}).values()))
    first: datetime | None = None
    total = 0.0
    for row in db.list_audit(limit=None, action="watchdog_close"):
        detail = row["detail"]
        if detail.get("instrument") != key:
            continue
        filled = detail.get("filled")
        if detail.get("status") in ("rejected", "cancelled"):
            if not isinstance(filled, int | float) or filled <= EPS:
                continue
            detail = {**detail, "qty": filled}  # an IOC sell that filled in part
        stamp = _aware(datetime.fromisoformat(row["ts"]))
        if detail.get("marker") is not None:
            if detail["marker"] != marker:
                continue
        elif opened is None or stamp < _aware(opened):
            continue
        qty = detail.get("qty")
        total += float(qty) if isinstance(qty, int | float) else 0.0
        first = stamp if first is None or stamp < first else first
    return None if first is None else (first, total)


def unreported_sales(sold: float, fills: Iterable[Fill], first: datetime) -> float:
    """The part of the watchdog's sales (`sold`, from `first` on) no fill shows: another
    client's sells since then are at least that much of it. An earlier day's sale IBKR no
    longer reports still came out of the bot's units."""
    start = _aware(first)
    seen = sum(
        f.qty for f in fills if f.side == Side.SELL and is_other_client(f) and _aware(f.ts) >= start
    )
    return max(0.0, sold - seen)
