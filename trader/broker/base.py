"""Broker interface. The engine only talks to brokers through this class."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime

from trader.models import (
    AccountSnapshot,
    AssetClass,
    Bar,
    Fill,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    Side,
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
        orders (bracket/OCA) when supports_native_stops() is True. Returns a record with
        status SUBMITTED/FILLED/REJECTED and broker_order_id set (id is left None; the
        engine owns the journal). Must not raise for ordinary rejections: return REJECTED
        with a message instead."""

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

    def estimate_commission(self, instrument: Instrument, qty: float, price: float) -> float:
        """One-way commission estimate in USD (used for fee-vs-reward risk checks)."""
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
    async def close_position(self, instrument: Instrument, reason: str = "") -> OrderRecord | None:
        """Cancel working orders for the instrument, then market-sell the whole long position."""
        positions = {p.instrument.key: p for p in await self.positions()}
        pos = positions.get(instrument.key)
        for order in await self.open_orders():
            if order.request.instrument.key == instrument.key and order.broker_order_id:
                await self.cancel_order(order.broker_order_id)
        if pos is None or pos.qty <= 0:
            return None
        req = OrderRequest(
            instrument=instrument,
            side=Side.SELL,
            qty=pos.qty,
            order_type=OrderType.MARKET,
            is_exit=True,
            reason=reason or "close position",
        )
        return await self.place_order(req)

    async def flatten_all(self, reason: str = "flatten") -> list[OrderRecord]:
        await self.cancel_all()
        results: list[OrderRecord] = []
        for pos in await self.positions():
            if pos.qty > 0:
                rec = await self.close_position(pos.instrument, reason)
                if rec is not None:
                    results.append(rec)
        return results
