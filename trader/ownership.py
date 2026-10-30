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
from trader.models import (
    OPEN_ORDER_STATUSES,
    Fill,
    OrderRecord,
    OrderStatus,
    Side,
    exec_revision,
    order_trace_id,
)

# IBKRBroker.fills_since: an execution of another API client (the owner's TWS or IBKR
# Mobile, or the crypto watchdog's own client id). This app's own executions (entries,
# bracket children, closes, re-placed stops) carry their order id.
OTHER_CLIENT_PREFIX = "perm:"
# IBKR stamps executions to the second, and this server's clock may run ahead of or behind
# IBKR's: a bot entry that fills at once can carry a time before its metadata's opened_at.
FILL_TIME_SKEW = timedelta(seconds=60)
# An order that waited for the owner's approval: its journal row is as old as the proposal,
# at most APPROVAL_TTL before it was sent; the send time is in kv SENT_PREFIX + order id.
APPROVAL_TTL = timedelta(minutes=10)
SENT_PREFIX = "order_sent:"  # kv: when an approved order was actually sent (order id)
EXTRA_PREFIX = "position_extra:"  # the engine's per-position kv (same key as engine.py)
WATCHDOG_TAG_PREFIX = "ai-trader-wd-"  # trace ids of the crypto watchdog's own orders
# kv: {"marker", "first", "orders": {broker order id: qty}}, the crypto watchdog's sells of
# one bot position (an IOC sell may fill in part: the watchdog reads what each one filled).
WATCHDOG_SALES_PREFIX = "watchdog_sales:"
# broker_order_id of an exit fill the engine journals at an estimate (its real execution
# fell on a day IBKR no longer reports): this app's own sell of the bot's units.
UNREPORTED_EXIT = "unreported-exit"
# Message of an app order row the engine stopped tracking (the broker no longer knows it):
# what it filled is unknown.
ORDER_FORGOTTEN = "no longer known to the broker"
EPS = 1e-9
# Position extra: [[seen at (ISO), qty], ...] units held beyond what the fills explain and
# beyond the unseen part of the bot's entry: the owner's, bought where IBKR no longer
# reports (the bot never adds to a position, and its entry went out with the symbol flat).
OWNER_HIDDEN = "owner_hidden"
# Position extra: the unseen rest of the bot's entry is settled (not held, none can come).
REST_SETTLED = "rest_settled"
# Position extra: {broker order id: qty} what a working sell of the app's (a bracket child has
# no order row) was seen to have filled beyond the journal's fills of it, right after the
# journal caught up with today's executions: sold on a day IBKR no longer reports, which no
# read shows once the order is done.
APP_SOLD_UNREPORTED = "app_sold_unreported"
# Position extra: {broker order id: qty} what each sell of the app's had filled when a close
# of the app's cancelled it, before or during its cancels (a bracket child has no order row,
# and a crash before the next journal leaves its fill on a day IBKR no longer reports).
APP_SELLS_SEEN = "app_sells_seen"


@dataclass(frozen=True)
class Split:
    bot: float = 0.0  # the bot's own units still held
    owner: float = 0.0  # the owner's units still held
    owner_bought: float = 0.0  # bought by another client in the window
    bot_bought: float = 0.0  # bought by this app in the window (its entry's fills)
    unseen: float = 0.0  # of the bot's entry quantity, what no fill shows and none sold
    hidden: float = 0.0  # the owner's units no fill shows (`split_fills(hidden=)`) still held
    bot_fills: list[Fill] = field(default_factory=list)  # the bot's part of each fill
    flat_at: datetime | None = None  # last fill after which neither side held anything
    entry_seen: float = 0.0  # the bot's buys of its entry that a fill shows
    unseen_other: float = 0.0  # of the unseen part, what another client's sells took


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
    bot_first_until: datetime | None = None,
    entry: float | None = None,
    entry_sent: datetime | None = None,
    hidden: Iterable[tuple[datetime, float]] = (),
    unreported: tuple[datetime, float] | None = None,
    unseen_cap: float | None = None,
    reserve: bool = False,
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
    position: the symbol was flat at the broker when the entry went out.

    `hidden`: units of the owner's that no fill shows (he bought them where IBKR no longer
    reports), each seen held at a time (`OWNER_HIDDEN`): another client's sells after it
    come out of them after his seen units, before the bot's.

    `unreported`: (when, qty) of sales of the bot's that no fill shows (the crypto watchdog's
    done sales on a day IBKR no longer reports, before the app journals them at an
    estimate): at that time they come out of the bot's units, then the unseen part of its
    entry, so another client's sell after them never takes what they sold. They make no
    fill of the bot's.

    `unseen_cap`: the most of the entry that may really have filled beyond these fills (an
    entry the app saw done holds no more than it saw fill). `reserve`: the bot's own sells
    still to come sell units it holds until then (it never adds to a position), so another
    client's sell before them never takes those."""
    bot = owner = owner_bought = bot_bought = unseen_other = 0.0
    pools = sorted([_aware(at), max(0.0, qty)] for at, qty in hidden)
    ordered = sorted(fills, key=lambda f: (_aware(f.ts), f.exec_id))
    sent_at = _aware(entry_sent) if entry_sent is not None else None
    # The bot's previous trip: what it bought and sold back to flat before this entry was
    # sent (the symbol was flat at the broker when it went out) is none of this entry's.
    prior, reset = 0.0, -1
    if sent_at is not None:
        run = bought = 0.0
        for i, f in enumerate(ordered):
            if f.qty <= EPS or is_other_client(f):
                continue
            if f.side == Side.BUY:
                run += f.qty
                bought += f.qty
                continue
            run = max(0.0, run - f.qty)
            if run <= EPS and bought > EPS and _aware(f.ts) < sent_at:
                prior, reset = bought, i
    seen = sum(f.qty for f in ordered if f.side == Side.BUY and not is_other_client(f)) - prior
    unseen = max(0.0, entry - seen) if entry is not None else 0.0
    if unseen_cap is not None:
        unseen = min(unseen, max(0.0, unseen_cap))
    # What the bot's own fills from each one on need of the units it holds just before it.
    need = [0.0] * (len(ordered) + 1)
    if reserve:
        for j in range(len(ordered) - 1, reset, -1):
            g = ordered[j]
            d = 0.0 if g.qty <= EPS or is_other_client(g) else g.qty
            need[j] = max(0.0, (d if g.side == Side.SELL else -d) + need[j + 1])
    out: list[Fill] = []
    flat_at: datetime | None = None
    start = _aware(bot_first_from) if bot_first_from is not None else None
    until = _aware(bot_first_until) if bot_first_until is not None else None
    budget = None if bot_first_qty is None else max(0.0, bot_first_qty) + _tol(bot_first_qty)
    trip = 0.0  # the bot's buys since its previous trip
    pending = None if unreported is None else (_aware(unreported[0]), max(0.0, unreported[1]))
    for i, f in enumerate([*ordered, None]):
        if pending is not None and (f is None or _aware(f.ts) > pending[0]):
            at, q = pending
            pending = None
            a = min(q, bot)
            bot -= a
            u = min(q - a, unseen)
            unseen -= u
            if start is not None and budget is not None and at >= start:
                budget -= min(q, budget)  # part of what the watchdog sold, as its estimate
        if f is None:
            break
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
                trip += q
                out.append(f)
            continue
        if sent_at is not None and trip <= EPS and _aware(f.ts) < sent_at:
            # Before the entry was sent (and any buy of the bot's): the symbol was flat at
            # the broker when it went out, so this sold nothing of the bot's position (the
            # bot's previous trip, or units held from before the window).
            owner -= min(q, owner)
            on_bot = 0.0
        else:
            lead = 0.0  # the part sold out of the bot's units first
            charge = False  # another client's sell inside the watchdog's window
            if not other:
                lead = q
                if (
                    f.broker_order_id == UNREPORTED_EXIT
                    and start is not None
                    and budget is not None
                    and _aware(f.ts) >= start
                ):
                    # The app's estimate of the watchdog's sale no fill shows: part of what
                    # the watchdog sold, not left for another client's sells.
                    budget -= min(q, budget)
            elif (
                start is not None
                and _aware(f.ts) >= start
                and (until is None or _aware(f.ts) <= until)
            ):
                lead = q if budget is None else min(q, budget)
                charge = True
            # Another client's sell never takes what the bot's own later sells sold.
            room = max(0.0, bot + unseen - need[i + 1]) if other else bot + unseen
            a = min(lead, bot, room)
            bot -= a
            u = min(lead - a, unseen, room - a)
            unseen -= u
            room -= a + u
            if charge:
                unseen_other += u
            if charge and budget is not None:
                budget -= a + u  # only what it took of the bot's may have been the watchdog's
            b = min(lead - a - u, owner)
            owner -= b
            on_bot = lead - b
            if charge and entry is not None:
                # The bot never holds more than its entry: beyond its units (seen and unseen)
                # and the owner's, units held from before the window were sold: nobody's.
                on_bot = a + u
            rest = q - lead  # another client's: out of the owner's units first
            if rest > EPS:
                o = min(rest, owner)
                owner -= o
                for pool in pools:  # his units no fill shows, held before this sell
                    if pool[0] <= _aware(f.ts) and rest - o > EPS:
                        h = min(rest - o, pool[1])
                        pool[1] -= h
                        o += h
                a = min(rest - o, bot, room)
                bot -= a
                u = min(rest - o - a, unseen, room - a)
                unseen -= u
                unseen_other += u
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
        if i == reset:
            trip = 0.0
    return Split(
        bot=bot,
        owner=owner,
        owner_bought=owner_bought,
        bot_bought=bot_bought,
        unseen=unseen,
        hidden=sum(pool[1] for pool in pools),
        bot_fills=out,
        flat_at=flat_at,
        entry_seen=seen,
        unseen_other=unseen_other,
    )


def read_hidden(extra: Mapping[str, Any]) -> list[tuple[datetime, float]]:
    """The owner's units no fill shows, as the engine recorded them (`OWNER_HIDDEN`)."""
    out: list[tuple[datetime, float]] = []
    for item in extra.get(OWNER_HIDDEN) or []:
        try:
            at, qty = item
            out.append((_aware(datetime.fromisoformat(at)), float(qty)))
        except (TypeError, ValueError):
            continue
    return out


def for_sizing(split: Split, extra: Mapping[str, Any]) -> Split:
    """The split an exit of the bot's is sized from: the owner's units no fill shows
    (`Split.hidden`) are his like his seen ones, and once the entry's rest is settled
    (`REST_SETTLED`) none of it is the bot's. The unseen part is lessened by those units,
    so sell_cap's test that all the units no fill explains fit in it is the same as before
    they were counted as his."""
    unseen = 0.0 if extra.get(REST_SETTLED) else max(0.0, split.unseen - split.hidden)
    return dataclasses.replace(
        split,
        owner=split.owner + split.hidden,
        owner_bought=split.owner_bought + split.hidden,  # he bought them: evidence
        hidden=0.0,
        unseen=unseen,
    )


def less_sold(split: Split, sold: float) -> Split:
    """`split` after sales of the bot's that no fill in it shows (the crypto watchdog's):
    out of its seen units first, then out of the unseen part of its entry."""
    from_bot = min(max(0.0, sold), split.bot)
    from_unseen = min(max(0.0, sold) - from_bot, split.unseen)
    return dataclasses.replace(split, bot=split.bot - from_bot, unseen=split.unseen - from_unseen)


def less_app_sold(split: Split, sold: float, filled: float) -> tuple[Split, float]:
    """`split` after the app's own sales no fill in it shows (`sold`:
    app_sold_beyond_journal): they came out of the bot's units, among them the part of its
    entry the app saw fill beyond those fills (`filled`: app_entry_unjournaled's sure part),
    which counts as the bot's first. Returns the split and that part (taken from
    `Split.unseen`)."""
    late = min(split.unseen, max(0.0, filled))
    moved = dataclasses.replace(split, bot=split.bot + late, unseen=split.unseen - late)
    return less_sold(moved, sold), late


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


def watchdog_sales_end(db: Database, key: str, marker: str) -> datetime | None:
    """The latest send of the watchdog's sells for this bot position when every one of
    them is done (an IOC sell fills when it is sent or never): another client's sells
    later than that (and the clock skew) cannot be its own. None: one may still fill, or
    the record does not say when each was sent."""
    rec = read_sales_record(db, key, marker)
    if rec is None:
        return None
    finished = set(rec.get("done", []))
    sent = rec.get("at", {})
    last: datetime | None = None
    for order_id in rec.get("orders", {}):
        if order_id not in finished or order_id not in sent:
            return None
        stamp = _aware(datetime.fromisoformat(sent[order_id]))
        last = stamp if last is None or stamp > last else last
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


def unreported_sales(
    sold: float, fills: Iterable[Fill], first: datetime, until: datetime | None = None
) -> float:
    """The part of the watchdog's sales (`sold`, from `first` on) no fill shows: another
    client's sells since then (until `until`) are at least that much of it, and so is the
    app's estimate of them (dated after a trip of the position journaled since, it may be
    later than `until`). An earlier day's sale IBKR no longer reports still came out of the
    bot's units."""
    start = _aware(first)
    end = _aware(until) if until is not None else None
    seen = sum(
        f.qty
        for f in fills
        if f.side == Side.SELL
        and _aware(f.ts) >= start
        and (
            f.broker_order_id == UNREPORTED_EXIT
            or (is_other_client(f) and (end is None or _aware(f.ts) <= end))
        )
    )
    return max(0.0, sold - seen)


def sent_at(db: Database, rec: OrderRecord) -> datetime:
    """When an order went to the broker: its journal time, or the approval time for an
    order that waited for the owner."""
    created = _aware(rec.created_at)
    raw = db.get_kv(SENT_PREFIX + str(rec.id)) if rec.id is not None else None
    if raw:
        try:
            return max(created, _aware(datetime.fromisoformat(raw)))
        except ValueError:
            pass
    return created


def order_rows(db: Database, opened: datetime, key: str | None = None) -> list[OrderRecord]:
    """The app's order rows (of `key`, or of every instrument) that the reads below may need
    for a bot position opened at `opened`, its entry's included (an approved entry's row is
    as old as its proposal): one read of the orders table, handed to each of them as
    `rows=`."""
    since = _aware(opened) - FILL_TIME_SKEW - APPROVAL_TTL
    return [
        rec
        for rec in db.list_orders_since(since)
        if key is None or rec.request.instrument.key == key
    ]


def rows_since(
    db: Database, rows: Iterable[OrderRecord] | None, since: datetime
) -> list[OrderRecord]:
    """The app's order rows created at or after `since`: out of `rows` (`order_rows`, read
    once for several reads), else read from the orders table."""
    if rows is None:
        return db.list_orders_since(since)
    bound = _aware(since)
    return [rec for rec in rows if _aware(rec.created_at) >= bound]


def app_entry_unjournaled(
    db: Database,
    key: str,
    opened: datetime,
    fills: Iterable[Fill],
    working: Iterable[OrderRecord],
    *,
    rows: Iterable[OrderRecord] | None = None,
) -> tuple[float, float]:
    """What the bot's entry for this position filled beyond the journal's fills of it, as
    (sure, unknown). Sure: what the broker reports filled of an entry still working (by
    its tag), or what the app saw filled of one it saw done. Unknown: the rest of an entry
    the app last saw working that the broker no longer lists (it filled, lapsed or was
    cancelled while the app was down), or that the engine forgot before it was done."""
    return _entry_unjournaled(key, _app_entries(db, key, opened, rows), fills, working)


def _entry_unjournaled(
    key: str,
    entries: Iterable[OrderRecord],
    fills: Iterable[Fill],
    working: Iterable[OrderRecord],
) -> tuple[float, float]:
    """app_entry_unjournaled of the entry's order rows (`_app_entries`)."""
    journaled: dict[str, float] = {}
    for f in fills:
        if f.side == Side.BUY and not is_other_client(f):
            journaled[f.broker_order_id] = journaled.get(f.broker_order_id, 0.0) + f.qty
    listed: dict[str, float] = {}
    for order in working:
        req = order.request
        if (
            order.status in OPEN_ORDER_STATUSES
            and req.side == Side.BUY
            and req.instrument.key == key
            and req.trace_id
        ):
            listed[req.trace_id] = listed.get(req.trace_id, 0.0) + order.filled_qty
    sure = unknown = 0.0
    for rec in entries:
        if not rec.broker_order_id:
            continue
        req = rec.request
        seen = journaled.get(rec.broker_order_id, 0.0)
        tag = order_trace_id(rec.id, req.trace_id)
        if tag in listed:
            filled = listed[tag]
        else:
            filled = rec.filled_qty
            if rec.status in OPEN_ORDER_STATUSES or rec.message == ORDER_FORGOTTEN:
                # Last seen working, or forgotten by the engine before it was done.
                unknown += max(0.0, req.qty - max(seen, filled))
        sure += max(0.0, filled - seen)
    return sure, unknown


def _app_entries(
    db: Database, key: str, opened: datetime, rows: Iterable[OrderRecord] | None = None
) -> list[OrderRecord]:
    """The app's order rows of the bot's entry for this position (and of its previous trip
    inside the clock skew), the crypto watchdog's own orders left out."""
    since = _aware(opened) - FILL_TIME_SKEW
    # An approved entry's row is as old as its proposal: read back to the oldest that could
    # still be sent, and keep the rows sent since the position opened.
    return [
        rec
        for rec in rows_since(db, rows, since - APPROVAL_TTL)
        if rec.request.instrument.key == key
        and rec.request.side == Side.BUY
        and not rec.request.is_exit
        and not (rec.request.trace_id or "").startswith(WATCHDOG_TAG_PREFIX)
        and sent_at(db, rec) >= since
    ]


def entry_held_unjournaled(
    db: Database,
    key: str,
    opened: datetime,
    fills: Iterable[Fill],
    working: Iterable[OrderRecord],
    *,
    rows: Iterable[OrderRecord] | None = None,
) -> float | None:
    """The most of the bot's entry that may be held beyond the journal's fills of it: what
    the broker reports filled of it still working, what the app saw filled of it done, and
    the part whose fill is unknown (`app_entry_unjournaled`). An entry still working that
    filled nothing, or one the app saw lapse, holds nothing unseen, whatever its quantity.
    None when the app's order journal cannot say: no row of its entry sent, or one whose
    send failed with no broker order id (its outcome unknown). Rows never sent (blocked,
    waiting for approval, an approval cancelled or expired) say nothing either way."""
    entries = _app_entries(db, key, opened, rows)
    sent = [rec for rec in entries if rec.broker_order_id or rec.status == OrderStatus.REJECTED]
    if not sent or any(not rec.broker_order_id for rec in sent):
        return None
    sure, unknown = _entry_unjournaled(key, entries, fills, working)
    return sure + unknown


def app_sold_beyond_journal(
    db: Database,
    key: str,
    opened: datetime,
    fills: Iterable[Fill],
    working: Iterable[OrderRecord] = (),
    *,
    own: Iterable[OrderRecord] = (),
    rows: Iterable[OrderRecord] | None = None,
    unreported: Mapping[str, Any] | None = None,
    seen: Mapping[str, Any] | None = None,
) -> float:
    """What the app's own sells for this bot position are known to have filled beyond the
    fills read of them (`fills`): what the app saw filled of one (its order row), or what
    the broker reports filled of one still working. The app went down before the journal
    caught up, or the fill fell on a day IBKR no longer reports: those units of the bot's
    are sold all the same. `working`: the broker's open orders as another client sees them
    (the crypto watchdog: the app's orders by their tag); `own`: the app's working orders on
    its own connection (by their order id: a bracket child has no order row of its own);
    `unreported`: what the app saw such an order fill beyond the journal while it worked
    (`APP_SOLD_UNREPORTED`, by order id: kept once the order is done and no read shows it);
    `seen`: what a close of the app's saw such an order had filled when it cancelled it
    (`APP_SELLS_SEEN`, by order id, counted like its order row). A sell whose fill is
    unknown (last seen working, or forgotten) counts only for what was seen filled of it
    (see app_sold_unjournaled). The crypto watchdog's sells are not the app's."""
    journaled: dict[str, float] = {}
    estimated: dict[str, float] = {}  # of those, what the app journaled of `unreported`
    for f in fills:
        if f.side == Side.SELL and not is_other_client(f):
            journaled[f.broker_order_id] = journaled.get(f.broker_order_id, 0.0) + f.qty
            if f.exec_id.startswith(f"{UNREPORTED_EXIT}:"):
                estimated[f.broker_order_id] = estimated.get(f.broker_order_id, 0.0) + f.qty
    filled: dict[str, float] = {}
    tags: dict[str, str] = {}
    since = _aware(opened) - FILL_TIME_SKEW
    for rec in rows_since(db, rows, since):
        req = rec.request
        if (
            req.instrument.key != key
            or req.side != Side.SELL
            or (req.trace_id or "").startswith(WATCHDOG_TAG_PREFIX)
            or not rec.broker_order_id
            or rec.status == OrderStatus.REJECTED
        ):
            continue
        filled[rec.broker_order_id] = max(filled.get(rec.broker_order_id, 0.0), rec.filled_qty)
        tags[order_trace_id(rec.id, req.trace_id)] = rec.broker_order_id
    for order, by_tag in [*((o, True) for o in working), *((o, False) for o in own)]:
        req = order.request
        if (
            order.status not in OPEN_ORDER_STATUSES
            or req.side != Side.SELL
            or req.instrument.key != key
            or (req.trace_id or "").startswith(WATCHDOG_TAG_PREFIX)
        ):
            continue
        oid = tags.get(req.trace_id or "") if by_tag else order.broker_order_id
        if oid and not oid.startswith(OTHER_CLIENT_PREFIX):
            filled[oid] = max(filled.get(oid, 0.0), order.filled_qty)
    for oid, qty in (seen or {}).items():
        # What a close's cancels saw a sell of the app's had filled (`APP_SELLS_SEEN`).
        if isinstance(qty, int | float) and not str(oid).startswith(OTHER_CLIENT_PREFIX):
            filled[str(oid)] = max(filled.get(str(oid), 0.0), float(qty))
    kept: dict[str, float] = {}
    for oid, qty in (unreported or {}).items():
        if isinstance(qty, int | float) and not str(oid).startswith(OTHER_CLIENT_PREFIX):
            kept[str(oid)] = float(qty)
    return sum(
        max(
            0.0,
            filled.get(oid, 0.0) - journaled.get(oid, 0.0),
            kept.get(oid, 0.0) - estimated.get(oid, 0.0),
        )
        for oid in {*filled, *kept}
    )


def app_sold_unjournaled(
    db: Database,
    key: str,
    opened: datetime,
    fills: Iterable[Fill],
    *,
    rows: Iterable[OrderRecord] | None = None,
) -> bool:
    """A sell of the app's for this bot position the journal may not show yet: one still
    working (its fills may come), one that filled beyond what the journal shows of it, or
    one the engine stopped tracking before the journal showed all of it (its fill unknown).
    A done sell whose fills the journal shows sold nothing unseen; rows never sent (a
    rejected, expired or cancelled approval) sold nothing. The crypto watchdog's own sells
    are not the app's."""
    own = [f for f in fills if not is_other_client(f)]
    journaled: dict[str, float] = {}
    for f in own:
        journaled[f.broker_order_id] = journaled.get(f.broker_order_id, 0.0) + f.qty
    since = _aware(opened) - FILL_TIME_SKEW
    for rec in rows_since(db, rows, since):
        req = rec.request
        if (
            req.instrument.key != key
            or req.side != Side.SELL
            or (req.trace_id or "").startswith(WATCHDOG_TAG_PREFIX)
            or not rec.broker_order_id
            or rec.status == OrderStatus.REJECTED
        ):
            continue
        seen = journaled.get(rec.broker_order_id, 0.0)
        if (
            rec.status in OPEN_ORDER_STATUSES
            or rec.filled_qty > seen + EPS
            or (rec.message == ORDER_FORGOTTEN and seen < rec.request.qty - EPS)
        ):
            return True
    return False
