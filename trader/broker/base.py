"""Broker interface. The engine only talks to brokers through this class."""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from datetime import datetime

from trader.models import (
    OPEN_ORDER_STATUSES,
    AccountSnapshot,
    AssetClass,
    Bar,
    Fill,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    order_trace_id,
)

logger = logging.getLogger(__name__)


# In an accepted entry's message: the parent filled but the broker refused its bracket stop.
PROTECTIVE_REJECTED = "protective order rejected"
# In an accepted entry's message: the parent filled and its stop works, but the broker
# refused the bracket's take-profit.
TARGET_REJECTED = "take-profit order rejected"


class BrokerError(Exception):
    """Raised for broker failures the engine should log and survive (not crash on)."""


class PartialFlattenError(BrokerError):
    """flatten_all could not close every position. `records` holds the orders it did send
    (to be journaled); the failed positions had their stops put back where possible."""

    def __init__(self, message: str, records: list[OrderRecord]) -> None:
        super().__init__(message)
        self.records = records


class Broker(ABC):
    name: str = "base"

    # ------------------------------------------------------------------ lifecycle
    @abstractmethod
    async def connect(self) -> None:
        """Connect (idempotent). Raises BrokerError when the broker is unreachable."""

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @property
    @abstractmethod
    def mode(self) -> Mode:
        """SIM, PAPER or LIVE. For IBKR this is detected from the account id ("DU..." = paper)."""

    @property
    def data_delayed(self) -> bool:
        """True when quotes/bars come from the free 15-minute-delayed feed."""
        return False

    @property
    def data_warning(self) -> str | None:
        """A market-data problem the owner should see (e.g. IBKR error 10197), or None.
        Quotes for the affected symbols raise BrokerError, so they get no entries."""
        return None

    # ------------------------------------------------------------------ account
    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def positions(self) -> list[Position]:
        """Open positions (qty != 0). stop_loss/take_profit are left None; the engine fills them."""

    # ------------------------------------------------------------------ market data
    @abstractmethod
    async def quote(self, instrument: Instrument) -> Quote: ...

    @abstractmethod
    async def bars(
        self, instrument: Instrument, bar_size: str = "5 mins", lookback: str = "2 D"
    ) -> list[Bar]:
        """Historical bars, oldest first. bar_size/lookback use IBKR-style strings."""

    # ------------------------------------------------------------------ orders
    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderRecord:
        """Submit an order. For entries with stop_loss/take_profit, attach native protective
        orders (bracket/OCA) when supports_native_stops() is True. A SELL exit that carries
        stop_loss is a standalone protective stop: it must rest as a stop order (guaranteed
        when supports_standalone_stops() is True) or be rejected, never sold at market.
        Returns a record with status SUBMITTED/PARTIALLY_FILLED/FILLED/REJECTED and
        broker_order_id set (id is left None; the engine owns the journal). Must not raise
        for ordinary rejections: return REJECTED with a message instead."""

    @abstractmethod
    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        """Latest status for an order this broker placed, or None if unknown."""

    @abstractmethod
    async def open_orders(self) -> list[OrderRecord]:
        """Working orders at the broker (including protective child orders)."""

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    async def cancel_all(self) -> None: ...

    @abstractmethod
    async def fills_since(self, since: datetime) -> list[Fill]:
        """Executions since `since` (UTC). Each Fill has a unique exec_id."""

    # ------------------------------------------------------------------ capabilities
    def supports_native_stops(self, instrument: Instrument) -> bool:
        """True if place_order attaches broker-side stop-loss/take-profit orders."""
        return False

    def can_cancel(self, order: OrderRecord) -> bool:
        """True if cancel_order can cancel this order. False for an order another client of
        the broker placed (on IBKR: TWS or IBKR Mobile), which only that client can cancel."""
        return True

    def cancel_requested(self, order: OrderRecord) -> bool:
        """True if this app asked the broker to cancel the order, even if the broker has not
        confirmed it yet (IBKR lists it as working until then). Such an order is on its way
        out: never a sell that already covers a position. Brokers whose cancels take effect
        at once never list one, so the default is False."""
        return False

    def holds_untracked_positions(self) -> bool:
        """True when the account holds something positions() leaves out (on IBKR: options,
        futures, bonds, FX): its value moves equity, so the account is not flat."""
        return False

    def supports_standalone_stops(self, instrument: Instrument) -> bool:
        """True if place_order rests a stop order for a SELL exit carrying stop_loss (used to
        re-place a missing protective stop). False means such a request is rejected."""
        return False

    def round_qty(
        self, instrument: Instrument, qty: float, allow_fractional: bool = False
    ) -> float:
        """Round DOWN to a tradable quantity (never rounds up past the risk-based size)."""
        if qty <= 0:
            return 0.0
        if instrument.asset_class == AssetClass.STOCK and not allow_fractional:
            return float(math.floor(qty + 1e-9))
        decimals = 4 if instrument.asset_class == AssetClass.STOCK else 6
        factor = 10**decimals
        return math.floor(qty * factor + 1e-9) / factor

    def estimate_commission(
        self, instrument: Instrument, qty: float, price: float, side: Side = Side.BUY
    ) -> float:
        """One-way commission estimate in USD (used for fee-vs-reward risk checks). Sells
        include regulatory fees where they apply."""
        return 0.0

    async def modify_stop(self, instrument: Instrument, new_stop: float) -> bool:
        """Move the working broker-side protective stop for a long position to new_stop
        (used for breakeven and trailing stops). Returns False when there is no native stop
        to move; the engine then manages the stop itself."""
        return False

    async def scan_movers(self, limit: int = 10) -> list[str]:
        """Optional dynamic universe (e.g. top % gainers). Returns [] when unsupported."""
        return []

    # ------------------------------------------------------------------ convenience
    async def close_position(
        self,
        instrument: Instrument,
        reason: str = "",
        *,
        restore_stop: float | None = None,
        trace_id: str | None = None,
        max_qty: float | None = None,
        keep: float = 0.0,
    ) -> OrderRecord | None:
        """Cancel working orders for the instrument, then market-sell the whole long position,
        or at most `max_qty` of it and never into the last `keep` units (the bot's own
        shares next to ones the owner bought).

        If the broker rejects that sell, the protective stop that was cancelled (or
        `restore_stop`, for callers that cancelled it already) is re-placed when the broker
        supports standalone stops, so a failed exit never leaves the position unprotected.
        Closes of one instrument run one at a time: each sees what the one before it left
        (its sell, or the stop it put back), never a half-finished close."""
        async with self.close_lock(instrument):
            return await self._close_position(
                instrument,
                reason,
                restore_stop=restore_stop,
                trace_id=trace_id,
                max_qty=max_qty,
                keep=keep,
            )

    def close_lock(self, instrument: Instrument) -> asyncio.Lock:
        """Held while a close of the instrument runs. A caller that decides from the
        instrument's orders (e.g. whether a stop is missing) holds it to see no close
        half-done."""
        locks: dict[str, asyncio.Lock] = self.__dict__.setdefault("_close_locks", {})
        return locks.setdefault(instrument.key, asyncio.Lock())

    async def _close_position(
        self,
        instrument: Instrument,
        reason: str,
        *,
        restore_stop: float | None,
        trace_id: str | None,
        max_qty: float | None = None,
        keep: float = 0.0,
    ) -> OrderRecord | None:
        if max_qty is not None and max_qty <= 1e-9:
            return None  # nothing to sell: never cancel the stop of a held position
        positions = {p.instrument.key: p for p in await self.positions()}
        pos = positions.get(instrument.key)
        stops = [] if restore_stop is None else [restore_stop]
        cancelled: set[str] = set()
        held = pos.qty if pos is not None and pos.qty > 0 else 0.0
        qty = held
        if max_qty is not None and held > 0:
            qty = self._capped(instrument, held, max_qty, keep)
            if qty <= 1e-9:
                return None  # none of it is the bot's: its stop stays
        req = OrderRequest(
            instrument=instrument,
            side=Side.SELL,
            qty=qty or 1.0,
            order_type=OrderType.MARKET,
            is_exit=True,
            reason=reason or "close position",
            trace_id=order_trace_id(existing=trace_id),
        )
        filled_before: dict[str, float] = {}
        try:
            for order in await self.open_orders():
                if order.request.instrument.key == instrument.key and order.broker_order_id:
                    if not self.can_cancel(order):
                        continue  # still working: never counted as a stop to put back
                    if order.request.side == Side.SELL:
                        filled_before[order.broker_order_id] = order.filled_qty
                    await self.cancel_order(order.broker_order_id)
                    cancelled.add(order.broker_order_id)
                    if order.request.side == Side.SELL and order.request.stop_loss is not None:
                        stops.append(order.request.stop_loss)
            if pos is None or pos.qty <= 0:
                return None
            working = await self.open_orders()
            if max_qty is not None:
                # A bracket child may have filled during the cancels: size again on a fresh
                # read, so the sell never reaches the owner's units.
                now_held = next(
                    (
                        p.qty
                        for p in await self.positions()
                        if p.instrument.key == instrument.key and p.qty > 0
                    ),
                    0.0,
                )
                # What this app's own sells (a bracket child) filled during the cancels
                # came out of the bot's units, whatever the read shows (it may lag them,
                # or an earlier lag may have caught up).
                sold = 0.0
                for oid, before in filled_before.items():
                    done = await self.order_status(oid)
                    if done is not None:
                        sold += max(0.0, done.filled_qty - before)
                qty = self._capped(instrument, now_held, max(0.0, max_qty - sold), keep)
                if qty <= 1e-9:
                    return None
                req = req.model_copy(update={"qty": qty})
        except BrokerError:
            # A stop may already be cancelled (here, or by flatten_all's cancel_all): never
            # leave a held position without it, nor with two (a stop next to another sell
            # or stop of this app's could sell the shares twice).
            if stops and pos is not None and pos.qty > 0:
                if not await self._covered(instrument, cancelled):
                    await self._put_stop_back(req, max(stops))
            raise
        # Another close of this app (a monitor exit, KILL, an approved close) may have sent
        # its market sell while this one was cancelling: never send a second one (a short).
        if self._own_market_sell(working, instrument):
            return None
        # A stop another close put back after this one's cancels (its own check failed)
        # would sit next to this sell: cancel it first, and keep it for a REJECTED sell.
        for order in working:
            other = order.request
            if (
                other.instrument.key == instrument.key
                and other.side == Side.SELL
                and other.stop_loss is not None
                and order.status in OPEN_ORDER_STATUSES
                and order.broker_order_id
                and self.can_cancel(order)
                and not self.cancel_requested(order)
            ):
                await self.cancel_order(order.broker_order_id)
                stops.append(other.stop_loss)
        record = await self.place_order(req)
        if (
            record.status == OrderStatus.REJECTED
            and stops
            and self.supports_standalone_stops(instrument)
        ):
            stop = max(stops)
            restored = await self.place_order(
                req.model_copy(
                    update={"stop_loss": stop, "reason": "protective stop (exit was rejected)"}
                )
            )
            self._note_put_back(instrument, restored)
            outcome = (
                f"protective stop re-placed at {stop:g}"
                if restored.status != OrderStatus.REJECTED
                else f"re-placing the protective stop at {stop:g} failed: {restored.message}"
            )
            record = record.model_copy(update={"message": f"{record.message}; {outcome}"})
        return record

    async def flatten_all(self, reason: str = "flatten") -> list[OrderRecord]:
        """Cancel every order, then market-sell every long position (re-placing a position's
        protective stop if its exit is rejected, as in close_position)."""
        stops: dict[str, float] = {}
        for order in await self.open_orders():
            req = order.request
            if req.side == Side.SELL and req.stop_loss is not None:
                key = req.instrument.key
                stops[key] = max(stops.get(key, 0.0), req.stop_loss)
        await self.cancel_all()
        results: list[OrderRecord] = []
        errors: list[str] = []
        for pos in await self.positions():
            if pos.qty > 0:
                # One failure must not leave the rest unsold and, since cancel_all, without
                # their stops: carry on, and report every failure at the end.
                try:
                    rec = await self.close_position(
                        pos.instrument, reason, restore_stop=stops.get(pos.instrument.key)
                    )
                except BrokerError as exc:
                    errors.append(f"{pos.instrument.symbol}: {exc}")
                    continue
                if rec is not None:
                    results.append(rec)
        if errors:
            raise PartialFlattenError("; ".join(errors), results)
        return results

    def _capped(self, instrument: Instrument, held: float, max_qty: float, keep: float) -> float:
        raw = min(max_qty, held - keep)
        if raw >= held - 1e-9:
            return held
        if raw <= 1e-9:
            return 0.0
        return self.round_qty(instrument, round(raw, 8), allow_fractional=True)

    def _own_market_sell(self, orders: list[OrderRecord], instrument: Instrument) -> bool:
        """A market sell of this app's for the instrument is working. Only this client's
        orders count: another client's (TWS, IBKR Mobile) is not a close of this app, nor is
        one this app asked to cancel (it will not sell once the cancel is confirmed)."""
        return any(
            order.request.instrument.key == instrument.key
            and order.request.side == Side.SELL
            and order.request.order_type == OrderType.MARKET
            and order.request.stop_loss is None
            and order.status in OPEN_ORDER_STATUSES
            and self.can_cancel(order)
            and not self.cancel_requested(order)
            for order in orders
        )

    async def _put_stop_back(self, req: OrderRequest, stop: float) -> None:
        """Best effort: re-place a protective stop a failed close had already removed."""
        if not self.supports_standalone_stops(req.instrument):
            return
        try:
            placed = await self.place_order(
                req.model_copy(
                    update={"stop_loss": stop, "reason": "protective stop (exit check failed)"}
                )
            )
        except BrokerError as exc:
            logger.warning("re-placing the stop for %s failed: %s", req.instrument.key, exc)
            return
        self._note_put_back(req.instrument, placed)

    def _note_put_back(self, instrument: Instrument, rec: OrderRecord) -> None:
        if rec.status != OrderStatus.REJECTED and rec.broker_order_id:
            self.__dict__.setdefault("_put_back", {})[instrument.key] = rec.broker_order_id

    async def _covered(self, instrument: Instrument, cancelled: set[str]) -> bool:
        """Whether this app already has a sell working for the held position: a market sell
        or a protective stop it has not asked to cancel (here, or in flatten_all's
        cancel_all; IBKR may confirm such a cancel only later). Without a fresh order list,
        the last stop this broker put back for it counts while still working."""
        try:
            listed = await self.open_orders()
        except BrokerError:
            last = self.__dict__.get("_put_back", {}).get(instrument.key)
            if last is None:
                return False
            try:
                rec = await self.order_status(last)
            except BrokerError:
                return False
            return (
                rec is not None
                and rec.status in OPEN_ORDER_STATUSES
                and last not in cancelled
                and not self.cancel_requested(rec)
            )
        return self._own_market_sell(listed, instrument) or any(
            order.request.instrument.key == instrument.key
            and order.request.side == Side.SELL
            and order.request.stop_loss is not None
            and order.status in OPEN_ORDER_STATUSES
            and order.broker_order_id
            and order.broker_order_id not in cancelled
            and self.can_cancel(order)
            and not self.cancel_requested(order)
            for order in listed
        )
