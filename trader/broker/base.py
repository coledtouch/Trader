"""Broker interface. The engine only talks to brokers through this class."""

from __future__ import annotations

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


class BrokerError(Exception):
    """Raised for broker failures the engine should log and survive (not crash on)."""


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
    ) -> OrderRecord | None:
        """Cancel working orders for the instrument, then market-sell the whole long position.

        If the broker rejects that sell, the protective stop that was cancelled (or
        `restore_stop`, for callers that cancelled it already) is re-placed when the broker
        supports standalone stops, so a failed exit never leaves the position unprotected."""
        positions = {p.instrument.key: p for p in await self.positions()}
        pos = positions.get(instrument.key)
        stops = [] if restore_stop is None else [restore_stop]
        for order in await self.open_orders():
            if order.request.instrument.key == instrument.key and order.broker_order_id:
                if not self.can_cancel(order):
                    continue  # still working: never counted as a stop to put back
                await self.cancel_order(order.broker_order_id)
                if order.request.side == Side.SELL and order.request.stop_loss is not None:
                    stops.append(order.request.stop_loss)
        if pos is None or pos.qty <= 0:
            return None
        # Another close (a monitor exit, KILL, an approved close) may have sent its market
        # sell while this one was cancelling: never send a second one (a short).
        if any(
            order.request.instrument.key == instrument.key
            and order.request.side == Side.SELL
            and order.request.order_type == OrderType.MARKET
            and order.request.stop_loss is None
            and order.status in OPEN_ORDER_STATUSES
            for order in await self.open_orders()
        ):
            return None
        req = OrderRequest(
            instrument=instrument,
            side=Side.SELL,
            qty=pos.qty,
            order_type=OrderType.MARKET,
            is_exit=True,
            reason=reason or "close position",
            trace_id=order_trace_id(existing=trace_id),
        )
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
        for pos in await self.positions():
            if pos.qty > 0:
                rec = await self.close_position(
                    pos.instrument, reason, restore_stop=stops.get(pos.instrument.key)
                )
                if rec is not None:
                    results.append(rec)
        return results
