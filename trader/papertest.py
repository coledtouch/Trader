"""`python -m trader paper-test`: check the order plumbing on the IBKR paper account.

Sends one stock entry exactly the way the engine does: a DAY limit buy with a GTC
stop-loss and a GTC take-profit in one OCA group (`IBKRBroker.place_order`). The limit is
20% below the market so it cannot fill. It then checks that IBKR accepted all three
orders as built, that the bot's open-orders view sees the entry, and cancels everything.
It works outside market hours too (IBKR holds the orders until the next session).

Paper accounts only: it refuses a live ("U...") account. It uses its own API client id,
so it never disconnects the app or the crypto watchdog. The orders are tagged
`paper-test-...`, which the engine does not adopt (it only adopts `ai-trader-...`), and
nothing can fill, so the trade journal is not touched.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from trader.broker.base import BrokerError
from trader.config import AppConfig
from trader.models import Instrument, Mode, OrderRequest, OrderStatus, OrderType, Side

TAG_PREFIX = "paper-test-"
# Fractions of the reference price: far enough below the market that the entry cannot
# fill during the few seconds it exists, close enough that IBKR treats it as a normal
# resting order. stop < entry < target, as the engine's brackets are.
ENTRY_FRACTION = 0.80
STOP_FRACTION = 0.75
TARGET_FRACTION = 0.85
SETTLE_SECONDS = 15.0
POLL_SECONDS = 0.5
# Statuses of a correctly accepted bracket that is waiting (in or outside market hours).
WAITING_STATES = frozenset({"PreSubmitted", "Submitted", "ApiPending", "PendingSubmit"})


class _PaperBroker(Protocol):
    @property
    def mode(self) -> Mode: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def quote(self, instrument: Instrument) -> Any: ...
    async def place_order(self, req: OrderRequest) -> Any: ...
    async def open_orders(self) -> list[Any]: ...
    async def cancel_order(self, broker_order_id: str) -> None: ...
    def orders_tagged(self, trace_id: str) -> list[dict[str, object]]: ...


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def paper_test_client_id(config: AppConfig) -> int:
    """An API client id used by neither the app nor the watchdog."""
    taken = {config.ib_client_id, config.watchdog_client_id}
    candidate = max(taken) + 1
    while candidate in taken:
        candidate += 1
    return candidate


def bracket_levels(ref: float) -> tuple[float, float, float]:
    """(entry, stop, target) rounded to cents."""
    return (
        round(ref * ENTRY_FRACTION, 2),
        round(ref * STOP_FRACTION, 2),
        round(ref * TARGET_FRACTION, 2),
    )


async def _wait_for(
    predicate: Callable[[], bool], seconds: float, sleep: Callable[[float], Awaitable[None]]
) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await sleep(POLL_SECONDS)


def _close(a: object, b: float) -> bool:
    return isinstance(a, int | float) and math.isfinite(a) and abs(a - b) < 0.011


def check_bracket(orders: list[dict[str, object]], entry: float, stop: float, target: float) -> str:
    """'' when the three orders are the bracket the engine relies on, else what is wrong."""
    parents = [o for o in orders if not o["parent_id"]]
    if len(parents) != 1:
        return f"expected 1 entry order, found {len(parents)}"
    parent = parents[0]
    children = [o for o in orders if o["parent_id"] == parent["order_id"]]
    stops = [o for o in children if o["type"] == "STP"]
    targets = [o for o in children if o["type"] == "LMT"]
    problems: list[str] = []
    if parent["action"] != "BUY" or parent["type"] != "LMT" or parent["tif"] != "DAY":
        problems.append(
            f"entry is {parent['action']} {parent['type']} {parent['tif']}, not BUY LMT DAY"
        )
    elif not _close(parent["limit"], entry):
        problems.append(f"entry limit {parent['limit']} is not {entry:.2f}")
    if len(stops) != 1:
        problems.append(f"expected 1 stop-loss attached to the entry, found {len(stops)}")
    else:
        s = stops[0]
        if s["action"] != "SELL" or s["tif"] != "GTC" or not _close(s["stop"], stop):
            problems.append(
                f"stop-loss is {s['action']} STP {s['tif']} at {s['stop']}, "
                f"not SELL STP GTC at {stop:.2f}"
            )
    if len(targets) != 1:
        problems.append(f"expected 1 take-profit attached to the entry, found {len(targets)}")
    else:
        t = targets[0]
        if t["action"] != "SELL" or t["tif"] != "GTC" or not _close(t["limit"], target):
            problems.append(
                f"take-profit is {t['action']} LMT {t['tif']} at {t['limit']}, "
                f"not SELL LMT GTC at {target:.2f}"
            )
    if len(stops) == 1 and len(targets) == 1:
        oca = {stops[0]["oca"], targets[0]["oca"]}
        if len(oca) != 1 or "" in oca:
            problems.append("stop-loss and take-profit are not in one OCA group")
    rejected = [
        f"{o['type']} {o['status']}"
        for o in orders
        if o["done"] or o["status"] not in WAITING_STATES
    ]
    if rejected:
        problems.append("not working at IBKR: " + ", ".join(rejected))
    return "; ".join(problems)


async def run_paper_test(
    broker: _PaperBroker,
    *,
    symbol: str = "F",
    ref_price: float | None = None,
    settle_seconds: float = SETTLE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[Check]:
    """Run the checks in order and always clean up. Never raises for a broker problem:
    it becomes a failed check."""
    checks: list[Check] = []
    tag = f"{TAG_PREFIX}{uuid4().hex[:12]}"
    instrument = Instrument(symbol=symbol.upper())
    placed = False
    try:
        try:
            await broker.connect()
        except BrokerError as exc:
            checks.append(Check("connect to IB Gateway", False, str(exc)))
            return checks
        if broker.mode != Mode.PAPER:
            checks.append(
                Check(
                    "paper account",
                    False,
                    f"connected to a {broker.mode.value} account; this test only runs on paper",
                )
            )
            return checks
        checks.append(Check("paper account", True, "connected to the IBKR paper account"))

        ref = ref_price
        if ref is None:
            try:
                ref = (await broker.quote(instrument)).price
            except BrokerError as exc:
                ref = None
                quote_error = str(exc)
            else:
                quote_error = "no price in the quote"
            if not ref or ref <= 0:
                checks.append(
                    Check(
                        f"price for {instrument.symbol}",
                        False,
                        f"{quote_error}. Run it again with --price <recent price>",
                    )
                )
                return checks
        checks.append(Check(f"price for {instrument.symbol}", True, f"reference ${ref:.2f}"))

        entry, stop, target = bracket_levels(ref)
        req = OrderRequest(
            instrument=instrument,
            side=Side.BUY,
            qty=1,
            order_type=OrderType.LIMIT,
            limit_price=entry,
            stop_loss=stop,
            take_profit=target,
            reason="paper test: cannot fill, cancelled right away",
            trace_id=tag,
        )
        try:
            record = await broker.place_order(req)
        except BrokerError as exc:
            checks.append(Check("entry accepted", False, str(exc)))
            return checks
        placed = True
        if record.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            checks.append(Check("entry accepted", False, record.message or record.status.value))
            return checks
        checks.append(
            Check(
                "entry accepted",
                True,
                f"BUY 1 {instrument.symbol} limit ${entry:.2f} (cannot fill), "
                f"stop ${stop:.2f}, target ${target:.2f}",
            )
        )

        def settled() -> bool:
            orders = broker.orders_tagged(tag)
            return len(orders) == 3 and all(
                o["status"] not in ("ApiPending", "PendingSubmit") for o in orders
            )

        await _wait_for(settled, settle_seconds, sleep)
        problem = check_bracket(broker.orders_tagged(tag), entry, stop, target)
        checks.append(
            Check(
                "stop-loss and take-profit attached at IBKR",
                not problem,
                problem or "GTC stop-loss and GTC take-profit, one OCA group, both working",
            )
        )

        try:
            visible = any(
                o.request.instrument.key == instrument.key and o.request.side == Side.BUY
                for o in await broker.open_orders()
            )
        except BrokerError as exc:
            visible, why = False, str(exc)
        else:
            why = "the entry is missing from the open-orders list"
        checks.append(
            Check(
                "bot sees the order",
                visible,
                "listed in open orders, as the bot reads them" if visible else why,
            )
        )
    finally:
        if placed:
            checks.append(await _cancel_all(broker, tag, settle_seconds, sleep))
        try:
            await broker.disconnect()
        except BrokerError:
            pass
    return checks


async def _cancel_all(
    broker: _PaperBroker,
    tag: str,
    settle_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
) -> Check:
    """Cancel the entry (IBKR cancels its children with it), then anything still working."""

    def working() -> list[dict[str, object]]:
        return [o for o in broker.orders_tagged(tag) if not o["done"]]

    try:
        for order in sorted(working(), key=lambda o: bool(o["parent_id"])):  # parent first
            await broker.cancel_order(str(order["broker_id"]))
        await _wait_for(lambda: not working(), settle_seconds, sleep)
        left = working()
        for order in left:
            await broker.cancel_order(str(order["broker_id"]))
        if left:
            await _wait_for(lambda: not working(), settle_seconds, sleep)
    except BrokerError as exc:
        return Check("test orders cancelled", False, f"{exc}. Cancel {tag} in IBKR Mobile")
    left = working()
    if left:
        return Check(
            "test orders cancelled",
            False,
            f"{len(left)} order(s) tagged {tag} still working: cancel them in IBKR Mobile",
        )
    return Check("test orders cancelled", True, "nothing left at IBKR")
