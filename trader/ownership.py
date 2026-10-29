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
from trader.models import OPEN_ORDER_STATUSES, Fill, OrderStatus, Side, exec_revision

# IBKRBroker.fills_since: an execution of another API client (the owner's TWS or IBKR
# Mobile, or the crypto watchdog's own client id). This app's own executions (entries,
# bracket children, closes, re-placed stops) carry their order id.
OTHER_CLIENT_PREFIX = "perm:"
# IBKR stamps executions to the second, and this server's clock may run ahead of or behind
# IBKR's: a bot entry that fills at once can carry a time before its metadata's opened_at.
FILL_TIME_SKEW = timedelta(seconds=60)
EXTRA_PREFIX = "position_extra:"  # the engine's per-position kv (same key as engine.py)
WATCHDOG_TAG_PREFIX = "ai-trader-wd-"  # trace ids of the crypto watchdog's own orders
# kv: {"marker", "first", "orders": {broker order id: qty}}, the crypto watchdog's sells of
# one bot position (an IOC sell may fill in part: the watchdog reads what each one filled).
WATCHDOG_SALES_PREFIX = "watchdog_sales:"
# broker_order_id of an exit fill the engine journals at an estimate (its real execution
# fell on a day IBKR no longer reports): this app's own sell of the bot's units.
UNREPORTED_EXIT = "unreported-exit"
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
    unread: bool = False  # qty 0 only because the broker's executions could not be read


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
    bot_first_qty: float | None = None,
    entry: float | None = None,
    entry_sent: datetime | None = None,
) -> Split:
    """Who holds what of one instrument after `fills` (the window from the bot's entry, less
    FILL_TIME_SKEW), in time order. The bot's buys are this app's own executions (the bot
    never adds to a position, so they are its entry); another client's buys are the
    owner's. This app's own sell comes out of the bot's units first. Another client's sell
    comes out of the owner's first (the owner's round trip leaves the bot's shares alone),
    except from `bot_first_from` on: the crypto watchdog sold this position then, and its
    sells are not told apart from the owner's. At most `bot_first_qty` (what the watchdog
    may have sold in all; None: no bound) of them is taken that way: beyond it, another
    client's sells cannot be the watchdog's and come out of the owner's units first.
    Another client's sell beyond both sides' units sold what was held before the window:
    nobody's here. Errs low on the bot's side, never high.

    `entry` (the bot's entry quantity): the part of it no buy here shows may have filled
    unseen (on a day IBKR no longer reports). A sell of the bot's beyond its seen units
    comes out of that part before the owner's (`Split.unseen` is what is left of it); so
    does another client's beyond the owner's and the bot's seen units. A sell before the
    entry was sent (`entry_sent`) and any buy of the bot's sold nothing of the bot's
    position: the symbol was flat at the broker when the entry went out."""
    bot = owner = owner_bought = bot_bought = 0.0
    ordered = sorted(fills, key=lambda f: (_aware(f.ts), f.exec_id))
    seen = sum(f.qty for f in ordered if f.side == Side.BUY and not is_other_client(f))
    unseen = max(0.0, entry - seen) if entry is not None else 0.0
    out: list[Fill] = []
    flat_at: datetime | None = None
    start = _aware(bot_first_from) if bot_first_from is not None else None
    budget = None if bot_first_qty is None else max(0.0, bot_first_qty) + _tol(bot_first_qty)
    sent_at = _aware(entry_sent) if entry_sent is not None else None
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
        if sent_at is not None and bot_bought <= EPS and _aware(f.ts) < sent_at:
            # Before the entry was sent (and any buy of the bot's): the symbol was flat at
            # the broker when it went out, so this sold nothing of the bot's position (the
            # bot's previous trip, or units held from before the window).
            owner -= min(q, owner)
            on_bot = 0.0
        else:
            lead = 0.0  # the part sold out of the bot's units first
            if not other:
                lead = q
            elif start is not None and _aware(f.ts) >= start:
                lead = q if budget is None else min(q, budget)
                if budget is not None:
                    budget -= lead
            a = min(lead, bot)
            bot -= a
            u = min(lead - a, unseen)
            unseen -= u
            b = min(lead - a - u, owner)
            owner -= b
            on_bot = lead - b
            rest = q - lead  # another client's: out of the owner's units first
            if rest > EPS:
                o = min(rest, owner)
                owner -= o
                a = min(rest - o, bot)
                bot -= a
                u = min(rest - o - a, unseen)
                unseen -= u
                # Beyond those, units held from before the window were sold: nobody's here.
                on_bot += a + u
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
        return round(risk / unit, 10)  # as fine as a crypto quantity, without float noise
    return None


def _tol(qty: float) -> float:
    return max(EPS, 1e-6 * abs(qty))


def owner_evidence(held: float, limit: float | None, split: Split) -> bool:
    """Something says the owner bought next to the bot: another client's buy in the
    window, or more held than the bot's entry order asked for."""
    return split.owner_bought > EPS or (limit is not None and held > limit + _tol(limit))


def bought_since(held: float, limit: float | None, after: Split) -> bool:
    """More held than the bot can still hold (`after`: the split less the crypto
    watchdog's sales known to have filled that no fill in it shows): someone else bought."""
    return limit is not None and held > after.bot + after.unseen + after.owner + _tol(limit)


def sell_cap(
    held: float, limit: float | None, split: Split, *, evidence: bool = False
) -> Cap | None:
    """How much of `held` a bot exit may sell. None: nothing says the owner bought since
    the bot's entry was sent, so everything held is the bot's (sell it all, as before).

    Part of the bot's entry may be in no fill read (it filled on a day IBKR no longer
    reports, or while the app was down; `split` from split_fills(..., entry=limit)): units
    no fill explains count as the bot's when they fit in that part, so its own position
    keeps its exits. More than that, and whose they are is unknown: the bot sells only
    what its fills show. `evidence`: the caller found evidence of its own."""
    if not evidence and not owner_evidence(held, limit, split):
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
    order works, what filled once it is `done` (then never read from the broker again),
    and when it was sent (`at`)."""
    rec = read_sales_record(db, key, marker) or {
        "marker": marker,
        "first": _aware(at).isoformat(),
        "orders": {},
    }
    sent = rec.setdefault("at", {})
    if order_id not in rec["orders"]:
        sent[order_id] = _aware(at).isoformat()
    rec["orders"][order_id] = qty
    if done:
        rec["done"] = sorted({*rec.get("done", []), order_id})
    db.set_kv(WATCHDOG_SALES_PREFIX + key, json.dumps(rec))


def _record_sales(rec: dict[str, Any], *, done_only: bool) -> tuple[datetime, float] | None:
    """(first send, quantity) of the record's sells that may have sold something: a done
    order by what filled (none: not a sale), one still working by what it asked (unless
    `done_only`)."""
    finished = set(rec.get("done", []))
    sent = rec.get("at", {})
    first: datetime | None = None
    total = 0.0
    for order_id, qty in rec.get("orders", {}).items():
        q = float(qty)
        if order_id in finished:
            if q <= EPS:
                continue  # it sold nothing
        elif done_only:
            continue
        stamp = _aware(datetime.fromisoformat(sent.get(order_id, rec["first"])))
        first = stamp if first is None or stamp < first else first
        total += q
    return None if first is None else (first, total)


def watchdog_last_filled(db: Database, key: str, marker: str, before: datetime) -> datetime | None:
    """When the watchdog sent the latest of its sells for this bot position that is done
    and filled something, among those sent before `before`."""
    rec = read_sales_record(db, key, marker)
    if rec is None:
        return None
    finished = set(rec.get("done", []))
    sent = rec.get("at", {})
    last: datetime | None = None
    for order_id, qty in rec.get("orders", {}).items():
        if order_id not in finished or float(qty) <= EPS:
            continue
        stamp = _aware(datetime.fromisoformat(sent.get(order_id, rec["first"])))
        if stamp < _aware(before) and (last is None or stamp > last):
            last = stamp
    return last


def watchdog_filled(db: Database, key: str, marker: str) -> tuple[datetime, float] | None:
    """(first send, quantity) of what the watchdog's sells for this bot position are known
    to have filled (orders it saw done). None without a record."""
    rec = read_sales_record(db, key, marker)
    return None if rec is None else _record_sales(rec, done_only=True)


def watchdog_sales(
    db: Database, key: str, marker: str, opened: datetime | None
) -> tuple[datetime, float] | None:
    """When the crypto watchdog first sent a sell for this bot position that may have sold
    something, and how much it sold in all (what filled of an order that is done, what it
    asked of one still working; a done order that filled nothing is no sale). The kv
    record first; audit rows of a watchdog from before the record existed otherwise (by
    marker, or for rows without one, from the position's opened_at on)."""
    rec = read_sales_record(db, key, marker)
    if rec is not None:
        return _record_sales(rec, done_only=False)
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
        f.qty
        for f in fills
        if f.side == Side.SELL
        and (is_other_client(f) or f.broker_order_id == UNREPORTED_EXIT)
        and _aware(f.ts) >= start
    )
    return max(0.0, sold - seen)


def app_sold_unjournaled(db: Database, key: str, opened: datetime, fills: Iterable[Fill]) -> bool:
    """A sell of the app's for this bot position the journal may not show yet: one still
    working (its fills may come), or one that filled beyond what the journal shows of it.
    A done sell whose fills the journal shows sold nothing unseen; rows never sent (a
    rejected, expired or cancelled approval) sold nothing. The crypto watchdog's own sells
    are not the app's."""
    own = [f for f in fills if not is_other_client(f)]
    journaled: dict[str, float] = {}
    for f in own:
        journaled[f.broker_order_id] = journaled.get(f.broker_order_id, 0.0) + f.qty
    since = _aware(opened) - FILL_TIME_SKEW
    for rec in db.list_orders(limit=200):
        req = rec.request
        if (
            req.instrument.key != key
            or req.side != Side.SELL
            or (req.trace_id or "").startswith(WATCHDOG_TAG_PREFIX)
            or not rec.broker_order_id
            or rec.status == OrderStatus.REJECTED
        ):
            continue
        if _aware(rec.created_at) >= since and (
            rec.status in OPEN_ORDER_STATUSES
            or rec.filled_qty > journaled.get(rec.broker_order_id, 0.0) + EPS
        ):
            return True
    return False
