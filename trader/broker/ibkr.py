"""Interactive Brokers broker (TWS API through IB Gateway) built on ib_async.

Design notes:
- Fully async: only the ``*Async`` request methods of ``ib_async.IB`` are used, every
  network call has a timeout, and connectivity problems surface as ``BrokerError``.
  The event loop is never patched (no ``util.startLoop``/``nest_asyncio``).
- ``connect()`` is idempotent and reconnect-safe. ib_async wipes its session state
  (tickers, trades) on disconnect, so this class drops its tickers too; qualified
  contracts are kept because IBKR contract ids are stable across sessions.
- Stock entries go out as a bracket: parent LMT/MKT (DAY) plus a protective STP and an
  optional take-profit LMT, both GTC and in one OCA group, so protection lives at the
  broker and survives disconnects and the day boundary. ``modify_stop`` raises that STP
  in place (same orderId, new auxPrice) for breakeven and trailing stops.
- IBKR crypto routes through ``config.ib_crypto_exchange`` (ZEROHASH; older accounts
  PAXOS) and only accepts LMT and MKT orders: buy MKT uses ``cashQty`` and MKT orders
  must be IOC. There are no native crypto stops, so ``supports_native_stops`` is False
  for crypto and the engine manages those exits.
- Commission estimates come from ``trader.fees`` with ``config.commission_plan``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from ib_async import (
    IB,
    AccountValue,
    Contract,
    Crypto,
    ExecutionFilter,
    LimitOrder,
    MarketOrder,
    Order,
    ScannerSubscription,
    Stock,
    StopOrder,
    Ticker,
    Trade,
)

from trader import fees
from trader.broker.base import Broker, BrokerError
from trader.config import AppConfig
from trader.models import (
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
    exec_revision,
    order_trace_id,
    utcnow,
)

logger = logging.getLogger(__name__)

# Error codes: https://www.interactivebrokers.com/campus/ibkr-api-page/tws-api-error-codes/
CONNECTIVITY_LOST_CODES = frozenset({1100, 2110})  # gateway <-> IBKR servers link is down
CONNECTIVITY_RESTORED_CODES = frozenset({1101, 1102})  # 1101 = restored, subscriptions lost
DATA_LOST_CODE = 1101
DELAYED_DATA_CODE = 10167  # not subscribed: delayed data shown for this instrument
NOT_SUBSCRIBED_CODE = 354  # not subscribed: no data at all for this instrument
FARM_BROKEN_CODES = frozenset({2103, 2105, 2157})
CANCEL_CONFIRM_CODE = 202  # "Order Canceled - reason: ..." (a cancel, not a rejection)
CONTRACT_UNKNOWN_CODE = 200  # "No security definition has been found"
# "No market data during competing live session": the paper and live logins share one set
# of market data subscriptions and only one session gets the data. While the owner is logged
# in to the live account elsewhere (TWS, IBKR Mobile, Client Portal), the bot's quotes for
# the affected symbols are unusable: possibly stale prices on a ticker that stopped updating.
COMPETING_SESSION_CODE = 10197
COMPETING_WARN_SECONDS = 900.0  # keep the dashboard warning this long after the last 10197

PENDING_IB_STATES = frozenset({"PendingSubmit", "ApiPending"})
WORKING_IB_STATES = frozenset(
    {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "ApiUpdate", "ValidationError"}
)
CANCELLED_IB_STATES = frozenset({"Cancelled", "ApiCancelled"})
DONE_IB_STATES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})
# Acknowledged, still-live orders that may be modified (IB holds untriggered stops as
# PreSubmitted; ValidationError is a refused modification of an order that is still live).
MODIFIABLE_IB_STATES = frozenset({"PreSubmitted", "Submitted", "ApiUpdate", "ValidationError"})
DEFAULT_CRYPTO_EXCHANGE = "ZEROHASH"

PREFERRED_CURRENCIES = ("USD", "BASE", "")  # first match wins for each account tag
VALID_BAR_SIZES = frozenset(
    {
        "1 secs", "5 secs", "10 secs", "15 secs", "30 secs",
        "1 min", "2 mins", "3 mins", "5 mins", "10 mins", "15 mins", "20 mins", "30 mins",
        "1 hour", "2 hours", "3 hours", "4 hours", "8 hours",
        "1 day", "1 week", "1 month",
    }
)  # fmt: skip
_BAR_UNITS = {
    "s": "secs", "sec": "secs", "secs": "secs", "second": "secs", "seconds": "secs",
    "m": "mins", "min": "mins", "mins": "mins", "minute": "mins", "minutes": "mins",
    "h": "hours", "hour": "hours", "hours": "hours",
    "d": "day", "day": "day", "days": "day",
    "w": "week", "week": "week", "weeks": "week",
    "mo": "month", "month": "month", "months": "month",
}  # fmt: skip
# Lookback units -> (IB duration unit, multiplier). "M" (upper case) is a month, as in IB.
_DURATION_UNITS = {
    "S": ("S", 1), "s": ("S", 1), "sec": ("S", 1), "secs": ("S", 1),
    "second": ("S", 1), "seconds": ("S", 1),
    "m": ("S", 60), "min": ("S", 60), "mins": ("S", 60), "minute": ("S", 60), "minutes": ("S", 60),
    "h": ("S", 3600), "hour": ("S", 3600), "hours": ("S", 3600),
    "D": ("D", 1), "d": ("D", 1), "day": ("D", 1), "days": ("D", 1),
    "W": ("W", 1), "w": ("W", 1), "week": ("W", 1), "weeks": ("W", 1),
    "M": ("M", 1), "mo": ("M", 1), "month": ("M", 1), "months": ("M", 1),
    "Y": ("Y", 1), "y": ("Y", 1), "year": ("Y", 1), "years": ("Y", 1),
}  # fmt: skip
_SIZE_RE = re.compile(r"\s*(\d+)\s*([A-Za-z]+)\s*")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9 .]{0,11}$")


# ---------------------------------------------------------------------- pure helpers
def mode_from_account(account_id: str) -> Mode:
    """IBKR paper accounts start with "DU"; anything else is treated as real money."""
    return Mode.PAPER if account_id.strip().upper().startswith("DU") else Mode.LIVE


def _to_float(value: object) -> float | None:
    """Parse an IB numeric value (often a string); None for blanks, NaN, inf and junk."""
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        return None
    try:
        num = float(value)
    except ValueError:
        return None
    return num if math.isfinite(num) else None


def _price(value: object) -> float | None:
    """A usable market price: finite and > 0 (IB uses NaN and -1 for "no value")."""
    num = _to_float(value)
    return num if num is not None and 0 < num < 1e12 else None


def _size(value: object) -> float | None:
    """A quoted bid/ask size: finite and >= 0 (IB uses NaN and -1 for "no value")."""
    num = _to_float(value)
    return num if num is not None and 0 <= num < 1e12 else None


def _is_halted(value: object) -> bool:
    """Ticker.halted (generic tick 49): 1 = halted, 2 = volatility (LULD) pause; 0 = trading,
    -1 = unknown and NaN = not reported yet, which both count as not halted."""
    num = _to_float(value)
    return num is not None and num > 0


def _commission(value: object) -> float:
    """Commission from a CommissionReport; IB's "unset" double and junk become 0."""
    num = _to_float(value)
    return num if num is not None and abs(num) < 1e8 else 0.0


def _round_to_increment(value: float, increment: float, *, up: bool) -> float:
    """Round to a multiple of ``increment`` (exact decimal math, so IB sees clean prices)."""
    try:
        val, inc = Decimal(str(value)), Decimal(str(increment))
        steps = (val / inc).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    except (InvalidOperation, ZeroDivisionError):
        return value
    return float(steps * inc)


def account_tag_values(values: Iterable[AccountValue]) -> dict[str, float]:
    """Numeric value per tag, preferring USD, then BASE, then currency-less tags."""
    best: dict[str, tuple[int, float]] = {}
    for item in values:
        if item.modelCode or item.currency not in PREFERRED_CURRENCIES:
            continue
        num = _to_float(item.value)
        if num is None:
            continue
        rank = PREFERRED_CURRENCIES.index(item.currency)
        if item.tag not in best or rank < best[item.tag][0]:
            best[item.tag] = (rank, num)
    return {tag: num for tag, (_, num) in best.items()}


def parse_account(
    account_id: str,
    mode: Mode,
    summary: Iterable[AccountValue],
    updates: Iterable[AccountValue] = (),
) -> AccountSnapshot:
    """Build an AccountSnapshot from accountSummary values (preferred) and accountValues."""
    vals = {**account_tag_values(updates), **account_tag_values(summary)}
    equity = vals.get("NetLiquidation")
    if equity is None:
        raise BrokerError("IBKR account data unavailable (no NetLiquidation yet)")
    cash = vals.get("TotalCashValue", 0.0)
    settled = vals.get("SettledCash", cash)
    # Conservative: the smaller of BuyingPower and AvailableFunds (a "no borrow" account
    # must not size off margin buying power). The risk manager also caps it at equity.
    power_candidates = [
        v for v in (vals.get("BuyingPower"), vals.get("AvailableFunds")) if v is not None
    ]
    buying_power = min(power_candidates) if power_candidates else 0.0
    day_trades = vals.get("DayTradesRemaining")  # -1 = unlimited / rule does not apply
    day_trades_remaining = int(day_trades) if day_trades is not None and day_trades >= 0 else None
    return AccountSnapshot(
        account_id=account_id,
        mode=mode,
        equity=equity,
        cash=cash,
        settled_cash=settled,
        buying_power=max(buying_power, 0.0),
        realized_pnl_today=vals.get("RealizedPnL", 0.0),
        unrealized_pnl=vals.get("UnrealizedPnL", 0.0),
        day_trades_remaining=day_trades_remaining,
        currency="USD",
    )


def ib_bar_size(bar_size: str) -> str:
    """Normalise "5 mins", "5m", "1 hour", "1d" ... to an IB barSizeSetting."""
    match = _SIZE_RE.fullmatch(bar_size)
    unit = _BAR_UNITS.get(match[2].lower()) if match else None
    if match is None or unit is None:
        raise BrokerError(f"unsupported bar size {bar_size!r}")
    n = int(match[1])
    if unit == "mins" and n == 1:
        text = "1 min"
    elif unit == "hours" and n == 1:
        text = "1 hour"
    else:
        text = f"{n} {unit}"
    if text not in VALID_BAR_SIZES:
        raise BrokerError(f"unsupported bar size {bar_size!r}")
    return text


def ib_duration(lookback: str) -> str:
    """Normalise "2 D", "2 days", "90 min", "1 W" ... to an IB durationStr."""
    match = _SIZE_RE.fullmatch(lookback)
    spec = (
        (_DURATION_UNITS.get(match[2]) or _DURATION_UNITS.get(match[2].lower())) if match else None
    )
    if match is None or spec is None or int(match[1]) < 1:
        raise BrokerError(f"unsupported lookback {lookback!r}")
    unit, factor = spec
    return f"{int(match[1]) * factor} {unit}"


def instrument_from_contract(contract: Contract | None) -> Instrument | None:
    """Map an IB contract to our Instrument; None for unsupported security types."""
    if contract is None or not contract.symbol:
        return None
    if contract.secType == "STK":
        return Instrument(symbol=contract.symbol, asset_class=AssetClass.STOCK)
    if contract.secType == "CRYPTO":
        return Instrument(symbol=contract.symbol, asset_class=AssetClass.CRYPTO)
    return None


def _trade_error(trade: Trade, since: int = 0) -> tuple[int, str] | None:
    """Most recent (code, message) error that IB attached to the order, if any; only log
    entries from index ``since`` on are considered."""
    for entry in reversed(trade.log[since:]):
        if entry.errorCode:
            message = entry.message.split(", contract:", 1)[0]
            return entry.errorCode, message[:300]
    return None


def map_ib_status(trade: Trade, cancel_requested: bool = False) -> tuple[OrderStatus, str]:
    """Translate an ib_async Trade's status into our OrderStatus plus a message."""
    status = trade.orderStatus.status
    filled = _to_float(trade.orderStatus.filled) or 0.0
    error = _trade_error(trade)
    error_msg = error[1] if error else ""
    if status == "Filled":
        return OrderStatus.FILLED, ""
    if status == "PendingCancel":
        return (
            OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.SUBMITTED
        ), "cancel pending"
    if status in WORKING_IB_STATES:
        message = error_msg if status == "ValidationError" else ""
        return (OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.SUBMITTED), message
    if status == "Inactive":
        return OrderStatus.REJECTED, error_msg or "order inactive at IBKR (rejected or not allowed)"
    if status in CANCELLED_IB_STATES:
        is_rejection = (
            error is not None and error[0] != CANCEL_CONFIRM_CODE and not cancel_requested
        )
        if is_rejection and filled <= 0:
            return OrderStatus.REJECTED, error_msg
        return OrderStatus.CANCELLED, error_msg or ("cancelled" if cancel_requested else "")
    return OrderStatus.SUBMITTED, f"unrecognised IBKR status {status!r}"


async def _wait_until(predicate: Callable[[], bool], within: float, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it is true or ``within`` seconds pass."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not predicate():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
    return True


class ContractNotFound(BrokerError):
    """IBKR does not know the instrument (an ordinary rejection, not a connectivity error)."""


class MarketDataUnavailable(BrokerError):
    """IBKR delivers no usable market data for the instrument right now (error 10197)."""


class _Reject(Exception):
    """Internal: the order request cannot be sent; becomes a REJECTED OrderRecord."""


@dataclass
class _ContractInfo:
    contract: Contract
    min_tick: float = 0.0
    min_size: float = 0.0
    size_increment: float = 0.0


@dataclass
class _TickerSub:
    ticker: Ticker
    started: float  # time.monotonic() when the subscription was made


# ---------------------------------------------------------------------- the broker
class IBKRBroker(Broker):
    name = "ibkr"

    # Timeouts in seconds (instance-overridable, e.g. by tests).
    CONNECT_TIMEOUT = 15.0
    REQUEST_TIMEOUT = 10.0
    HISTORICAL_TIMEOUT = 45.0
    QUOTE_TIMEOUT = 5.0
    ORDER_ACK_TIMEOUT = 5.0
    CANCEL_TIMEOUT = 5.0
    SCANNER_TIMEOUT = 8.0
    UPSTREAM_GRACE = 90.0  # force a reconnect if 1100/2110 is not cleared by then
    TICKER_RETRY_AFTER = 60.0  # resubscribe a ticker that never delivered data
    MAX_TICKERS = 60  # streaming market data lines held at once (IB default limit 100)

    def __init__(self, config: AppConfig, ib: IB | None = None) -> None:
        self._config = config
        self._ib = ib if ib is not None else IB()
        self._client_id = int(config.ib_client_id)
        self._crypto_exchange = config.ib_crypto_exchange.strip().upper() or DEFAULT_CRYPTO_EXCHANGE
        self._account: str | None = None
        self._syncing = False
        self._connect_lock = asyncio.Lock()
        self._open_orders_lock = asyncio.Lock()
        self._contract_locks: dict[str, asyncio.Lock] = {}
        self._contracts: dict[str, _ContractInfo] = {}
        self._tickers: OrderedDict[str, _TickerSub] = OrderedDict()
        self._upstream_lost_at: float | None = None
        # Error 1101 ("connectivity restored - data lost"): IB dropped every subscription
        # (account updates, positions, the account summary, market data), so the cached
        # account and positions are frozen. Nothing is trusted until a full reconnect.
        self._resync_needed = False
        self._delayed_keys: set[str] = set()
        # instrument key -> wall-clock time of the last error 10197 for it
        self._no_data: dict[str, datetime] = {}
        self._competing_at: datetime | None = None
        self._clock: Callable[[], datetime] = utcnow  # wall clock, comparable to Ticker.time
        self._requests: dict[int, OrderRequest] = {}  # orderId -> request placed by us
        self._perm_ids: dict[int, int] = {}  # orderId -> permId (survives ib_async resets)
        self._cancel_requested: set[int] = set()
        self._ib.errorEvent += self._on_error
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.orderStatusEvent += self._on_order_status

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        async with self._connect_lock:
            if self._ib.isConnected():
                if self._account is not None:
                    if self._resync_needed:
                        logger.warning(
                            "IBKR restored its link but lost the data subscriptions (1101); "
                            "reconnecting to resync account, positions and orders"
                        )
                    elif self._upstream_lost_at is None:
                        return
                    else:
                        lost_for = time.monotonic() - self._upstream_lost_at
                        if lost_for < self.UPSTREAM_GRACE:
                            raise BrokerError(
                                "IB Gateway is up but has lost its link to IBKR servers"
                            )
                        logger.warning("IBKR link still down after %.0fs; reconnecting", lost_for)
                self._safe_disconnect()
            self._syncing = True
            try:
                await self._connect_once()
            finally:
                self._syncing = False

    async def _connect_once(self) -> None:
        cfg = self._config
        self._reset_session()
        try:
            await asyncio.wait_for(
                self._ib.connectAsync(
                    cfg.ib_host,
                    cfg.ib_port,
                    clientId=self._client_id,
                    timeout=self.CONNECT_TIMEOUT,
                    account=cfg.ib_account.strip(),
                ),
                self.CONNECT_TIMEOUT * 4,
            )
        except Exception as exc:
            self._safe_disconnect()
            raise BrokerError(
                f"cannot connect to IB Gateway at {cfg.ib_host}:{cfg.ib_port} "
                f"(clientId {self._client_id}): {type(exc).__name__}: {exc}"[:300]
            ) from exc
        try:
            account = self._select_account(self._ib.managedAccounts())
            self._ib.reqMarketDataType(cfg.ib_market_data_type)
        except (BrokerError, ConnectionError) as exc:
            self._safe_disconnect()
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError(f"IB Gateway connection dropped during setup: {exc}") from exc
        previous = self._account
        self._account = account
        if previous is not None and mode_from_account(previous) != mode_from_account(account):
            logger.warning("IBKR account mode changed to %s after reconnect", self.mode.value)
        logger.info(
            "connected to IBKR %s account %s (market data type %d)",
            self.mode.value,
            _mask_account(account),
            cfg.ib_market_data_type,
        )

    def _select_account(self, accounts: list[str]) -> str:
        pinned = self._config.ib_account.strip()
        if pinned:
            if pinned not in accounts:
                raise BrokerError(
                    "TRADER_IB_ACCOUNT is not managed by this IB login "
                    f"({len(accounts)} account(s) available)"
                )
            return pinned
        if len(accounts) == 1:
            return accounts[0]
        if not accounts:
            raise BrokerError("IB Gateway reported no managed accounts")
        raise BrokerError("this IB login manages several accounts; set TRADER_IB_ACCOUNT")

    async def disconnect(self) -> None:
        self._safe_disconnect()
        self._tickers.clear()

    def _safe_disconnect(self) -> None:
        try:
            self._ib.disconnect()
        except Exception as exc:  # disconnect must never raise
            logger.debug("ignoring error while disconnecting from IBKR: %r", exc)

    def _reset_session(self) -> None:
        self._tickers.clear()
        self._upstream_lost_at = None
        self._resync_needed = False
        self._delayed_keys.clear()
        self._no_data.clear()  # a new session re-subscribes; IB repeats 10197 if it still applies

    @property
    def connected(self) -> bool:
        return (
            self._ib.isConnected()
            and not self._syncing
            and self._account is not None
            and self._upstream_lost_at is None
            and not self._resync_needed
        )

    @property
    def mode(self) -> Mode:
        account = self._account or self._config.ib_account.strip()
        # Unknown account (never connected) is treated as LIVE: fail safe (shadow mode).
        return mode_from_account(account) if account else Mode.LIVE

    @property
    def data_delayed(self) -> bool:
        """The whole feed is delayed (market data type 3/4). Delayed data for a single
        instrument (error 10167) is reported on its quotes instead (Quote.delayed)."""
        return self._config.ib_market_data_type in (3, 4)

    @property
    def data_warning(self) -> str | None:
        """Error 10197 seen recently: which symbols have no usable data and what to do."""
        now = self._clock()

        def recent(at: datetime) -> bool:
            return (now - at).total_seconds() < COMPETING_WARN_SECONDS

        symbols = sorted(k.split(":", 1)[-1] for k, at in self._no_data.items() if recent(at))
        if not symbols and not (self._competing_at and recent(self._competing_at)):
            return None
        shown = ", ".join(symbols[:8]) + (f" +{len(symbols) - 8} more" if len(symbols) > 8 else "")
        return (
            "IBKR error 10197: no market data while another session of this login holds it "
            "(the live account in TWS, IBKR Mobile or Client Portal)"
            + (f"; no entries for {shown}" if shown else "")
            + ". Log out of the other session to restore data."
        )

    def _require_connected(self) -> str:
        if self._syncing or self._account is None or not self._ib.isConnected():
            raise BrokerError("not connected to IB Gateway")
        if self._resync_needed:
            raise BrokerError("IBKR data was lost (error 1101): waiting for a reconnect")
        return self._account

    # ------------------------------------------------------------------ events
    def _on_error(self, req_id: int, code: int, message: str, contract: Contract | None) -> None:
        try:
            self._handle_error(req_id, code, message, contract)
        except Exception:  # an event handler must never break ib_async's dispatch
            logger.exception("error handling IBKR message %s", code)

    def _handle_error(
        self, req_id: int, code: int, message: str, contract: Contract | None
    ) -> None:
        if code in CONNECTIVITY_LOST_CODES:
            if self._upstream_lost_at is None:
                self._upstream_lost_at = time.monotonic()
            logger.warning("IBKR connectivity lost (code %d): %s", code, message)
        elif code in CONNECTIVITY_RESTORED_CODES:
            self._upstream_lost_at = None
            if code == DATA_LOST_CODE:
                # Every subscription is gone, including account updates and positions that
                # ib_async does not resubmit on 1101: `connected` stays False until connect()
                # has reconnected, which re-runs all of ib_async's startup syncs.
                self._tickers.clear()
                self._resync_needed = True
                logger.warning("IBKR connectivity restored but data lost (code %d)", code)
            else:
                logger.info("IBKR connectivity restored (code %d)", code)
        elif code == COMPETING_SESSION_CODE:
            now = self._clock()
            self._competing_at = now
            inst = instrument_from_contract(contract)
            # Without a contract the request is unknown: treat every subscription as affected.
            keys = [inst.key] if inst is not None else list(self._tickers)
            for key in keys:
                self._no_data[key] = now
            logger.warning(
                "IBKR has no market data for %s: a competing live session holds it (code %d)",
                ", ".join(keys) or "an unknown request",
                code,
            )
        elif code == DELAYED_DATA_CODE:
            # Delayed data for this instrument only: its quotes carry delayed=True (which
            # blocks LIVE stock entries in it), other instruments are unaffected. Without a
            # contract the request is unknown: every current subscription is marked.
            inst = instrument_from_contract(contract)
            keys = [inst.key] if inst is not None else list(self._tickers)
            self._delayed_keys.update(keys)
            logger.info(
                "IBKR shows delayed market data for %s (code %d)",
                ", ".join(keys) or "an unknown request",
                code,
            )
        elif code == NOT_SUBSCRIBED_CODE:
            # No data at all (not delayed data): the quote stays empty, so entries in it are
            # blocked as "no quote" while every other instrument trades normally.
            inst = instrument_from_contract(contract)
            logger.warning(
                "IBKR market data not subscribed for %s (code %d): %s",
                inst.key if inst is not None else "a request",
                code,
                message,
            )
        elif code == CONTRACT_UNKNOWN_CODE and contract is not None:
            inst = instrument_from_contract(contract)
            if inst is not None:
                self._contracts.pop(inst.key, None)
            logger.info("IBKR has no contract for reqId %d: %s", req_id, message)
        elif code in FARM_BROKEN_CODES:
            logger.warning("IBKR data farm problem (code %d): %s", code, message)
        elif 2100 <= code < 2200:
            logger.debug("IBKR notice %d: %s", code, message)
        else:
            logger.info("IBKR error %d (reqId %d): %s", code, req_id, message)

    def _on_disconnected(self) -> None:
        self._tickers.clear()
        self._upstream_lost_at = None
        logger.warning("disconnected from IB Gateway")

    def _on_order_status(self, trade: Trade) -> None:
        order, status = trade.order, trade.orderStatus
        if order.orderId and order.permId:
            self._perm_ids[order.orderId] = order.permId
        logger.info(
            "IBKR order %s %s %s %s: %s (filled %s)",
            order.orderId,
            order.action,
            order.orderType,
            trade.contract.symbol,
            status.status,
            status.filled,
        )

    # ------------------------------------------------------------------ plumbing
    async def _call[T](self, start: Callable[[], Awaitable[T]], limit: float, what: str) -> T:
        """Run an ib_async request with a timeout, mapping failures to BrokerError."""
        try:
            return await asyncio.wait_for(start(), limit)
        except TimeoutError as exc:
            raise BrokerError(f"IBKR {what} timed out after {limit:.0f}s") from exc
        except (ConnectionError, OSError) as exc:
            raise BrokerError(f"IBKR {what} failed: {exc}") from exc

    async def _contract_info(self, instrument: Instrument) -> _ContractInfo:
        """Qualified contract (with tick/size rules) for an instrument; cached."""
        key = instrument.key
        cached = self._contracts.get(key)
        if cached is not None:
            return cached
        lock = self._contract_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._contracts.get(key)
            if cached is not None:
                return cached
            if instrument.asset_class == AssetClass.STOCK:
                template: Contract = Stock(instrument.symbol, "SMART", "USD")
            else:
                template = Crypto(instrument.symbol, self._crypto_exchange, "USD")
            details = await self._call(
                lambda: self._ib.reqContractDetailsAsync(template),
                self.REQUEST_TIMEOUT,
                f"contract lookup for {key}",
            )
            matches = [d for d in details if d.contract and d.contract.secType == template.secType]
            if len(matches) != 1:
                raise ContractNotFound(
                    f"IBKR contract for {key} is {'ambiguous' if matches else 'unknown'}"
                )
            detail = matches[0]
            contract = detail.contract
            assert contract is not None  # filtered above; narrows the type
            contract.exchange = template.exchange  # keep SMART routing / the crypto venue
            info = _ContractInfo(
                contract=contract,
                min_tick=_to_float(detail.minTick) or 0.0,
                min_size=_to_float(detail.minSize) or 0.0,
                size_increment=_to_float(detail.sizeIncrement) or 0.0,
            )
            self._contracts[key] = info
            return info

    def _subscribe(self, key: str, contract: Contract) -> _TickerSub:
        old = self._tickers.pop(key, None)
        if old is not None:
            with contextlib.suppress(Exception):
                self._ib.cancelMktData(contract)
        while len(self._tickers) >= self.MAX_TICKERS:
            _, evicted = self._tickers.popitem(last=False)
            with contextlib.suppress(Exception):
                if evicted.ticker.contract is not None:
                    self._ib.cancelMktData(evicted.ticker.contract)
        try:
            ticker = self._ib.reqMktData(contract, "", False, False)
        except ConnectionError as exc:
            raise BrokerError(f"IBKR market data request failed: {exc}") from exc
        sub = _TickerSub(ticker=ticker, started=time.monotonic())
        self._tickers[key] = sub
        return sub

    @staticmethod
    def _has_data(ticker: Ticker) -> bool:
        return any(_price(v) is not None for v in (ticker.bid, ticker.ask, ticker.last))

    def _tick_size(self, info: _ContractInfo, instrument: Instrument, price: float) -> float:
        if instrument.asset_class == AssetClass.STOCK:
            return 0.01 if price >= 1.0 else 0.0001  # Reg NMS sub-penny rule
        return info.min_tick if info.min_tick > 0 else 0.01

    def _held_qty(self, account: str, instrument: Instrument) -> float:
        """Long qty held, as the smaller of what the portfolio and position streams say
        (either can lag a fill by a moment; the smaller one can never make us short)."""
        sources = (
            [(p.contract, p.position) for p in self._ib.portfolio(account)],
            [(p.contract, p.position) for p in self._ib.positions(account)],
        )
        seen: list[float] = []
        for source in sources:
            qtys = [
                _to_float(qty) or 0.0
                for contract, qty in source
                if (inst := instrument_from_contract(contract)) is not None
                and inst.key == instrument.key
            ]
            if qtys:
                seen.append(sum(qtys))
        return min(seen) if seen else 0.0

    # ------------------------------------------------------------------ account
    async def account(self) -> AccountSnapshot:
        acct = self._require_connected()
        summary = await self._call(
            lambda: self._ib.accountSummaryAsync(acct), self.REQUEST_TIMEOUT, "account summary"
        )
        return parse_account(acct, self.mode, summary, self._ib.accountValues(acct))

    async def positions(self) -> list[Position]:
        acct = self._require_connected()
        result: dict[str, Position] = {}
        for item in self._ib.portfolio(acct):
            inst = instrument_from_contract(item.contract)
            qty = _to_float(item.position) or 0.0
            if inst is None or qty == 0:
                continue
            result[inst.key] = Position(
                instrument=inst,
                qty=qty,
                avg_cost=_to_float(item.averageCost) or 0.0,
                market_price=_price(item.marketPrice),
                unrealized_pnl=_to_float(item.unrealizedPNL) or 0.0,
            )
        # reqPositions data covers anything the portfolio stream has not delivered yet.
        for pos in self._ib.positions(acct):
            inst = instrument_from_contract(pos.contract)
            qty = _to_float(pos.position) or 0.0
            if inst is None or qty == 0 or inst.key in result:
                continue
            result[inst.key] = Position(
                instrument=inst, qty=qty, avg_cost=_to_float(pos.avgCost) or 0.0
            )
        return list(result.values())

    # ------------------------------------------------------------------ market data
    async def quote(self, instrument: Instrument) -> Quote:
        self._require_connected()
        info = await self._contract_info(instrument)
        key = instrument.key
        sub = self._tickers.get(key)
        blocked_at = self._no_data.get(key)
        retry_due = sub is not None and time.monotonic() - sub.started > self.TICKER_RETRY_AFTER
        stale = sub is not None and not self._has_data(sub.ticker) and retry_due
        if sub is None or stale or (blocked_at is not None and retry_due):
            # (10197: re-subscribe now and then to find out whether the data is back)
            sub = self._subscribe(key, info.contract)
            await _wait_until(
                lambda: self._has_data(sub.ticker) or self._no_data.get(key) != blocked_at,
                self.QUOTE_TIMEOUT,
            )
        else:
            self._tickers.move_to_end(key)
        ticker = sub.ticker
        self._check_competing(instrument, ticker)
        ts = ticker.time if isinstance(ticker.time, datetime) else utcnow()
        delayed = (
            self._config.ib_market_data_type in (3, 4)
            or ticker.marketDataType in (3, 4)
            or key in self._delayed_keys
        )
        return Quote(
            instrument=instrument,
            bid=_price(ticker.bid),
            ask=_price(ticker.ask),
            last=_price(ticker.last),
            bid_size=_size(ticker.bidSize),
            ask_size=_size(ticker.askSize),
            ts=ts if ts.tzinfo else ts.replace(tzinfo=UTC),
            delayed=delayed,
            halted=_is_halted(ticker.halted),
        )

    def _check_competing(self, instrument: Instrument, ticker: Ticker) -> None:
        """Raise MarketDataUnavailable while error 10197 applies to the instrument: data is
        usable again only once a tick arrives after the last 10197."""
        key = instrument.key
        blocked_at = self._no_data.get(key)
        if blocked_at is None:
            return
        tick = ticker.time if isinstance(ticker.time, datetime) else None
        if tick is not None and self._has_data(ticker):
            tick = tick if tick.tzinfo else tick.replace(tzinfo=UTC)
            if tick > blocked_at:
                del self._no_data[key]
                if not self._no_data:
                    self._competing_at = None  # every affected symbol has data again
                logger.info("IBKR market data for %s is flowing again", key)
                return
        raise MarketDataUnavailable(
            f"no IBKR market data for {instrument.symbol}: another session of this login "
            "holds it (error 10197)"
        )

    async def bars(
        self, instrument: Instrument, bar_size: str = "5 mins", lookback: str = "2 D"
    ) -> list[Bar]:
        self._require_connected()
        size, duration = ib_bar_size(bar_size), ib_duration(lookback)
        info = await self._contract_info(instrument)
        is_stock = instrument.asset_class == AssetClass.STOCK
        raw = await self._call(
            lambda: self._ib.reqHistoricalDataAsync(
                info.contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=size,
                whatToShow="TRADES" if is_stock else "AGGTRADES",
                useRTH=is_stock,
                formatDate=2,
                timeout=self.HISTORICAL_TIMEOUT,
            ),
            self.HISTORICAL_TIMEOUT + 5,
            f"historical data for {instrument.key}",
        )
        by_ts: dict[datetime, Bar] = {}
        for item in raw:
            stamp = item.date
            if isinstance(stamp, datetime):
                ts = (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).astimezone(UTC)
            elif isinstance(stamp, date):
                ts = datetime(stamp.year, stamp.month, stamp.day, tzinfo=UTC)
            else:
                continue
            prices = [_price(v) for v in (item.open, item.high, item.low, item.close)]
            if any(p is None for p in prices):
                continue
            o, h, low, c = (p for p in prices if p is not None)
            volume = max(_to_float(item.volume) or 0.0, 0.0)
            by_ts[ts] = Bar(ts=ts, open=o, high=h, low=low, close=c, volume=volume)
        return [by_ts[ts] for ts in sorted(by_ts)]

    async def scan_movers(self, limit: int = 10) -> list[str]:
        """Top % gainers among liquid US stocks priced $2-$50; [] on any error."""
        limit = min(int(limit), 50)
        if limit < 1 or not self.connected:
            return []
        sub = ScannerSubscription(
            numberOfRows=limit,
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode="TOP_PERC_GAIN",
            abovePrice=2.0,
            belowPrice=50.0,
            aboveVolume=1_000_000,
        )
        ready = asyncio.Event()

        def on_update(*_args: object) -> None:
            ready.set()

        data_list = None
        try:
            data_list = self._ib.reqScannerSubscription(sub)
            data_list.updateEvent += on_update
            await asyncio.wait_for(ready.wait(), self.SCANNER_TIMEOUT)
            rows = sorted(data_list, key=lambda row: row.rank)
        except Exception as exc:
            logger.info("IBKR scanner unavailable: %s", exc or type(exc).__name__)
            return []
        finally:
            if data_list is not None:
                with contextlib.suppress(Exception):
                    data_list.updateEvent -= on_update
                    self._ib.cancelScannerSubscription(data_list)
        symbols: list[str] = []
        for row in rows:
            contract = row.contractDetails.contract if row.contractDetails else None
            if contract is None or contract.secType != "STK":
                continue
            symbol = contract.symbol.strip().upper()
            if _SYMBOL_RE.match(symbol) and symbol not in symbols:
                symbols.append(symbol)
        return symbols[:limit]

    # ------------------------------------------------------------------ orders
    async def place_order(self, req: OrderRequest) -> OrderRecord:
        req = req.model_copy(update={"trace_id": order_trace_id(existing=req.trace_id)})
        try:
            self._check_request(req)
        except _Reject as exc:
            return _rejected(req, str(exc))
        acct = self._require_connected()
        if self._upstream_lost_at is not None:
            raise BrokerError("IB Gateway has lost its link to IBKR servers; not sending orders")
        try:
            info = await self._contract_info(req.instrument)
        except ContractNotFound as exc:
            return _rejected(req, str(exc))
        try:
            if req.is_exit:
                held = self._held_qty(acct, req.instrument)
                if req.qty > held + 1e-9:
                    raise _Reject(
                        f"exit qty {req.qty:g} exceeds the long position at IBKR ({held:g}); "
                        "refusing to go short"
                    )
            if req.instrument.asset_class == AssetClass.STOCK:
                orders = self._stock_orders(req, info, acct)
            else:
                orders = await self._crypto_orders(req, info, acct)
        except _Reject as exc:
            return _rejected(req, str(exc))
        return await self._submit(req, info.contract, orders)

    def _check_request(self, req: OrderRequest) -> None:
        if req.side == Side.SELL and not req.is_exit:
            raise _Reject("short selling is not allowed (long-only)")
        if req.side == Side.BUY and req.is_exit:
            raise _Reject("exit orders must be SELL (long-only)")
        if req.order_type == OrderType.LIMIT and _price(req.limit_price) is None:
            raise _Reject("limit order needs a positive limit_price")
        if req.instrument.asset_class == AssetClass.STOCK and req.qty != math.floor(req.qty):
            raise _Reject("fractional share quantities are not supported for IBKR stock orders")
        if req.is_exit:
            if req.stop_loss is not None:  # a standalone protective stop, never a market sell
                if req.instrument.asset_class != AssetClass.STOCK:
                    raise _Reject("IBKR has no stop orders for crypto; the engine manages it")
                if _price(req.stop_loss) is None:
                    raise _Reject("protective stop needs a positive stop_loss")
            return
        stop = _price(req.stop_loss)
        if stop is None:
            raise _Reject("entry rejected: a stop_loss is required")
        if req.order_type == OrderType.LIMIT and req.limit_price is not None:
            if stop >= req.limit_price:
                raise _Reject("entry rejected: stop_loss must be below the limit price")
            if req.take_profit is not None and req.take_profit <= req.limit_price:
                raise _Reject("entry rejected: take_profit must be above the limit price")
        if req.take_profit is not None and req.take_profit <= stop:
            raise _Reject("entry rejected: take_profit must be above stop_loss")

    def _base_kwargs(self, acct: str, req: OrderRequest) -> dict[str, object]:
        # One tag per order (the journal id when the engine stamped it, or the watchdog's
        # own tag). The same tag on every order made them impossible to tell apart.
        return {"account": acct, "orderRef": order_trace_id(existing=req.trace_id)}

    def _stock_orders(self, req: OrderRequest, info: _ContractInfo, acct: str) -> list[Order]:
        """Entry -> bracket [parent, stop, (take-profit)]; exit with stop_loss -> standalone
        GTC protective stop; other exits -> single DAY order."""
        qty = float(req.qty)
        kwargs = self._base_kwargs(acct, req)
        if req.is_exit:
            if req.stop_loss is not None:
                tick = self._tick_size(info, req.instrument, req.stop_loss)
                stop_price = _round_to_increment(req.stop_loss, tick, up=True)
                return [StopOrder("SELL", qty, stop_price, tif="GTC", **kwargs)]
            if req.order_type == OrderType.MARKET:
                return [MarketOrder("SELL", qty, tif="DAY", **kwargs)]
            assert req.limit_price is not None  # checked in _check_request
            tick = self._tick_size(info, req.instrument, req.limit_price)
            price = _round_to_increment(req.limit_price, tick, up=True)
            return [LimitOrder("SELL", qty, price, tif="DAY", **kwargs)]

        assert req.stop_loss is not None  # checked in _check_request
        ref_price = req.limit_price or req.stop_loss
        tick = self._tick_size(info, req.instrument, ref_price)
        stop_price = _round_to_increment(req.stop_loss, tick, up=True)  # never risk more
        parent_id = self._next_order_id()
        if req.order_type == OrderType.LIMIT:
            assert req.limit_price is not None
            entry_price = _round_to_increment(req.limit_price, tick, up=False)  # never pay more
            if stop_price >= entry_price:
                raise _Reject("entry rejected: stop is within one tick of the entry price")
            parent: Order = LimitOrder(
                "BUY", qty, entry_price, orderId=parent_id, tif="DAY", transmit=False, **kwargs
            )
        else:
            parent = MarketOrder("BUY", qty, orderId=parent_id, tif="DAY", transmit=False, **kwargs)

        oca = f"ai-trader-oca-{parent_id}"
        child_kwargs = {
            **kwargs,
            "parentId": parent_id,
            "tif": "GTC",  # protection survives disconnects and the day boundary
            "ocaGroup": oca,
            "ocaType": 2,  # reduce the sibling on (partial) fill, with overfill block
        }
        stop = StopOrder(
            "SELL",
            qty,
            stop_price,
            orderId=self._next_order_id(),
            transmit=req.take_profit is None,
            **child_kwargs,
        )
        orders: list[Order] = [parent, stop]
        if req.take_profit is not None:
            target_price = _round_to_increment(req.take_profit, tick, up=True)
            orders.append(
                LimitOrder(
                    "SELL",
                    qty,
                    target_price,
                    orderId=self._next_order_id(),
                    transmit=True,  # the last order transmits the whole bracket
                    **child_kwargs,
                )
            )
        return orders

    async def _crypto_orders(
        self, req: OrderRequest, info: _ContractInfo, acct: str
    ) -> list[Order]:
        """IBKR crypto: LMT (DAY) by quantity; MKT is IOC, and a MKT buy uses cashQty."""
        kwargs = self._base_kwargs(acct, req)
        qty = float(req.qty)
        if not req.is_exit and info.min_size > 0 and qty < info.min_size:
            raise _Reject(f"crypto qty {qty:g} is below IBKR's minimum size {info.min_size:g}")
        action = req.side.value
        if req.order_type == OrderType.LIMIT:
            assert req.limit_price is not None
            tick = self._tick_size(info, req.instrument, req.limit_price)
            price = _round_to_increment(req.limit_price, tick, up=req.side == Side.SELL)
            return [LimitOrder(action, qty, price, tif="DAY", **kwargs)]
        if req.side == Side.SELL:
            return [MarketOrder("SELL", qty, tif="IOC", **kwargs)]
        quote = await self.quote(req.instrument)
        ref = quote.ask or quote.price
        if ref is None:
            raise _Reject("no crypto quote to size a market buy (IBKR needs a cash amount)")
        cash = _round_to_increment(qty * ref, 0.01, up=False)
        if cash <= 0:
            raise _Reject("crypto market buy amount rounds to $0")
        return [MarketOrder("BUY", 0, cashQty=cash, tif="IOC", **kwargs)]

    def _next_order_id(self) -> int:
        try:
            return self._ib.client.getReqId()
        except ConnectionError as exc:
            raise BrokerError(f"IBKR order id unavailable: {exc}") from exc

    async def _submit(
        self, req: OrderRequest, contract: Contract, orders: list[Order]
    ) -> OrderRecord:
        trades: list[Trade] = []
        try:
            for order in orders:
                trades.append(self._ib.placeOrder(contract, order))
        except ConnectionError as exc:
            # Nothing transmits until the last order (transmit=True) is accepted locally.
            for trade in trades:
                self._quiet_cancel(trade.order)
            raise BrokerError(f"IBKR connection lost while placing an order: {exc}") from exc
        parent = trades[0]
        self._requests[parent.order.orderId] = req
        await _wait_until(
            lambda: all(t.orderStatus.status not in PENDING_IB_STATES for t in trades),
            self.ORDER_ACK_TIMEOUT,
        )
        record = self._record(parent, req)
        if record.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            return record
        if (
            record.status == OrderStatus.SUBMITTED
            and parent.orderStatus.status in PENDING_IB_STATES
        ):
            record.message = "sent; awaiting IBKR acknowledgement"
        failed_child = next(
            (t for t in trades[1:] if t.orderStatus.status in DONE_IB_STATES - {"Filled"}), None
        )
        if failed_child is not None:
            error = _trade_error(failed_child)
            reason = error[1] if error else failed_child.orderStatus.status
            if (_to_float(parent.orderStatus.filled) or 0.0) <= 0:
                self._quiet_cancel(parent.order)
                return _rejected(
                    req,
                    f"protective order rejected by IBKR ({reason}); entry cancelled",
                    broker_order_id=str(parent.order.orderId),
                )
            record.message = f"WARNING: protective order rejected by IBKR ({reason})"
        return record

    def _quiet_cancel(self, order: Order) -> None:
        self._cancel_requested.add(order.orderId)
        try:
            self._ib.cancelOrder(order)
        except Exception as exc:
            logger.warning("could not cancel IBKR order %s: %r", order.orderId, exc)

    def _is_own(self, order: Order) -> bool:
        """True for orders placed through this API client id (cancellable by us)."""
        return order.orderId > 0 and order.clientId == self._client_id

    def _broker_id(self, trade: Trade) -> str:
        order = trade.order
        return str(order.orderId) if self._is_own(order) else f"perm:{order.permId}"

    def _find_trade(self, broker_order_id: str) -> Trade | None:
        trades = self._ib.trades()
        if broker_order_id.startswith("perm:"):
            perm = _to_int(broker_order_id.removeprefix("perm:"))
            return next((t for t in trades if perm and t.order.permId == perm), None)
        order_id = _to_int(broker_order_id)
        if not order_id:
            return None
        perm = self._perm_ids.get(order_id) or self._perm_from_executions(order_id)
        for trade in trades:
            if (self._is_own(trade.order) and trade.order.orderId == order_id) or (
                perm and trade.order.permId == perm
            ):
                return trade
        return None

    def _perm_from_executions(self, order_id: int) -> int | None:
        """The permId of one of our orders, from the executions ib_async loads at connect.

        An order that completed while this process was down comes back only as a completed
        order, which IBKR reports without orderId/clientId (ib_async keys it by permId), and
        the orderId -> permId map of the previous process is gone. Its executions still carry
        all three ids, so a filled order is found again instead of being marked cancelled."""
        for fill in self._ib.fills():
            ex = fill.execution
            if ex.orderId == order_id and ex.clientId == self._client_id and ex.permId:
                self._perm_ids[order_id] = ex.permId
                return ex.permId
        return None

    def _request_for(self, trade: Trade) -> OrderRequest | None:
        stored = self._requests.get(trade.order.orderId) if self._is_own(trade.order) else None
        return stored or _request_from_trade(trade)

    def _record(self, trade: Trade, request: OrderRequest) -> OrderRecord:
        order = trade.order
        status, message = map_ib_status(trade, order.orderId in self._cancel_requested)
        fills_qty = sum(_to_float(f.execution.shares) or 0.0 for f in trade.fills)
        return OrderRecord(
            request=request,
            status=status,
            broker_order_id=self._broker_id(trade),
            filled_qty=max(_to_float(trade.orderStatus.filled) or 0.0, fills_qty),
            avg_fill_price=_price(trade.orderStatus.avgFillPrice),
            commission=sum(_commission(f.commissionReport.commission) for f in trade.fills),
            message=message,
        )

    def orders_tagged(self, trace_id: str) -> list[dict[str, object]]:
        """Every order this client placed in this session whose orderRef is `trace_id`,
        working or done, as plain fields (the paper test checks the bracket with it)."""
        out: list[dict[str, object]] = []
        for trade in self._ib.trades():
            order = trade.order
            if not self._is_own(order) or (getattr(order, "orderRef", "") or "") != trace_id:
                continue
            out.append(
                {
                    "broker_id": self._broker_id(trade),
                    "order_id": order.orderId,
                    "parent_id": order.parentId,
                    "action": order.action,
                    "type": order.orderType,
                    "tif": order.tif,
                    "qty": _to_float(order.totalQuantity),
                    "limit": _price(order.lmtPrice),
                    "stop": _price(order.auxPrice),
                    "oca": order.ocaGroup or "",
                    "status": trade.orderStatus.status,
                    "done": trade.isDone(),
                }
            )
        return out

    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        self._require_connected()
        trade = self._find_trade(broker_order_id)
        request = self._request_for(trade) if trade is not None else None
        if trade is None or request is None:
            return None
        return self._record(trade, request)

    async def open_orders(self) -> list[OrderRecord]:
        acct = self._require_connected()
        async with self._open_orders_lock:
            # A fresh snapshot across all clients (incl. orders placed in TWS/mobile), plus
            # our own working orders that the snapshot may not include yet.
            snapshot = await self._call(
                self._ib.reqAllOpenOrdersAsync, self.REQUEST_TIMEOUT, "open orders"
            )
        seen: set[str] = set()
        records: list[OrderRecord] = []
        own = [t for t in self._ib.openTrades() if self._is_own(t.order)]
        for trade in [*own, *snapshot]:
            if trade.order.account and trade.order.account != acct:
                continue
            if trade.orderStatus.status in DONE_IB_STATES:
                continue
            broker_id = self._broker_id(trade)
            request = self._request_for(trade)
            if broker_id in seen or request is None:
                continue
            seen.add(broker_id)
            records.append(self._record(trade, request))
        return records

    def can_cancel(self, order: OrderRecord) -> bool:
        # Other clients' orders (TWS, IBKR Mobile) are listed as "perm:<permId>".
        return not (order.broker_order_id or "").startswith("perm:")

    async def cancel_order(self, broker_order_id: str) -> None:
        self._require_connected()
        trade = self._find_trade(broker_order_id)
        if trade is None or trade.isDone():
            return
        if not self._is_own(trade.order):
            logger.warning(
                "order %s was placed by another IB client; cancel it in TWS/IBKR Mobile",
                broker_order_id,
            )
            return
        self._cancel_requested.add(trade.order.orderId)
        try:
            self._ib.cancelOrder(trade.order)
        except ConnectionError as exc:
            raise BrokerError(f"IBKR cancel failed: {exc}") from exc
        if not await _wait_until(trade.isDone, self.CANCEL_TIMEOUT):
            logger.warning("IBKR has not confirmed the cancel of order %s yet", broker_order_id)

    async def cancel_all(self) -> None:
        self._require_connected()
        for trade in self._ib.openTrades():
            if self._is_own(trade.order):
                self._cancel_requested.add(trade.order.orderId)
        try:
            self._ib.reqGlobalCancel()  # cancels every open order, incl. other clients'
        except ConnectionError as exc:
            raise BrokerError(f"IBKR global cancel failed: {exc}") from exc
        done = await _wait_until(
            lambda: not any(self._is_own(t.order) for t in self._ib.openTrades()),
            self.CANCEL_TIMEOUT,
        )
        if not done:
            logger.warning("IBKR has not confirmed every cancel after a global cancel")

    async def fills_since(self, since: datetime) -> list[Fill]:
        acct = self._require_connected()
        since_utc = (since if since.tzinfo else since.replace(tzinfo=UTC)).astimezone(UTC)
        # "YYYYMMDD-HH:MM:SS" is UTC for the TWS API; an hour of slack covers clock skew
        # and the exact cut-off is applied below.
        start = since_utc - timedelta(hours=1)
        flt = ExecutionFilter(acctCode=acct, time=start.strftime("%Y%m%d-%H:%M:%S"))
        raw = await self._call(
            lambda: self._ib.reqExecutionsAsync(flt), self.REQUEST_TIMEOUT, "executions"
        )
        # Commission reports land on ib_async's own fill objects, often after the
        # executions; prefer those so the commission is filled in when known.
        known = {f.execution.execId: f for f in self._ib.fills()}
        fills: dict[str, Fill] = {}
        for item in raw:
            ex = item.execution
            inst = instrument_from_contract(item.contract)
            side = {"BOT": Side.BUY, "SLD": Side.SELL}.get(ex.side)
            qty, price = _to_float(ex.shares) or 0.0, _price(ex.price)
            if inst is None or side is None or qty <= 0 or price is None or not ex.execId:
                continue
            ts = ex.time if ex.time.tzinfo else ex.time.replace(tzinfo=UTC)
            if ts.astimezone(UTC) < since_utc:
                continue
            report = known.get(ex.execId, item).commissionReport
            own = ex.orderId > 0 and ex.clientId == self._client_id
            fills[ex.execId] = Fill(
                exec_id=ex.execId,
                broker_order_id=str(ex.orderId) if own else f"perm:{ex.permId}",
                instrument=inst,
                side=side,
                qty=qty,
                price=price,
                commission=_commission(report.commission),
                ts=ts.astimezone(UTC),
            )
        # A corrected execution repeats the id with a higher revision: keep only the latest
        # (the journal replaces an earlier revision it already stored, see exec_revision).
        latest: dict[str, tuple[int, Fill]] = {}
        for fill in fills.values():
            base, rev = exec_revision(fill.exec_id) or (fill.exec_id, 0)
            if base not in latest or latest[base][0] < rev:
                latest[base] = (rev, fill)
        return sorted((fill for _, fill in latest.values()), key=lambda f: f.ts)

    # ------------------------------------------------------------------ capabilities
    def supports_native_stops(self, instrument: Instrument) -> bool:
        # IBKR crypto accepts only LMT/MKT orders, so crypto stops are engine-managed.
        return instrument.asset_class == AssetClass.STOCK

    def supports_standalone_stops(self, instrument: Instrument) -> bool:
        # A SELL exit carrying stop_loss becomes a GTC STP order (stocks only).
        return instrument.asset_class == AssetClass.STOCK

    def round_qty(
        self, instrument: Instrument, qty: float, allow_fractional: bool = False
    ) -> float:
        if qty <= 0:
            return 0.0
        if instrument.asset_class == AssetClass.STOCK:
            # Whole shares only: brackets with fractional shares are not supported here.
            return float(math.floor(qty + 1e-9))
        info = self._contracts.get(instrument.key)
        if info is None or info.size_increment <= 0:
            return super().round_qty(instrument, qty, allow_fractional)
        rounded = _round_to_increment(qty + 1e-12, info.size_increment, up=False)
        return rounded if rounded >= info.min_size else 0.0

    def estimate_commission(
        self, instrument: Instrument, qty: float, price: float, side: Side = Side.BUY
    ) -> float:
        return fees.estimate_commission(instrument, qty, price, side, self._config.commission_plan)

    # ------------------------------------------------------------------ stop management
    async def modify_stop(self, instrument: Instrument, new_stop: float) -> bool:
        """Raise the working GTC protective stop(s) this client placed for a long stock
        position to ``new_stop`` (rounded up to the tick): same orderId, new auxPrice. Never
        lowers a stop. True only when IBKR acknowledged the new price on every stop; False
        when there is nothing to move, the move was refused or not confirmed in time (the
        engine then manages the stop itself)."""
        requested = _price(new_stop)
        if requested is None or instrument.asset_class != AssetClass.STOCK:
            return False
        try:
            acct = self._require_connected()
            if self._upstream_lost_at is not None:
                raise BrokerError("IB Gateway has lost its link to IBKR servers")
            info = await self._contract_info(instrument)
        except BrokerError as exc:
            logger.warning("cannot move the IBKR stop for %s: %s", instrument.key, exc)
            return False
        if self._held_qty(acct, instrument) <= 0:
            return False
        stops = self._protective_stops(acct, instrument)
        if not stops:
            return False
        tick = self._tick_size(info, instrument, requested)
        target = _round_to_increment(requested, tick, up=True)
        results = [await self._raise_stop(info.contract, trade, target, tick) for trade in stops]
        return all(results)

    def _protective_stops(self, acct: str, instrument: Instrument) -> list[Trade]:
        """Working SELL STP orders for the instrument that this client placed (bracket
        children or re-placed protective stops)."""
        return [
            trade
            for trade in self._ib.openTrades()
            if self._is_own(trade.order)
            and trade.order.action == "SELL"
            and trade.order.orderType == "STP"
            and trade.orderStatus.status in MODIFIABLE_IB_STATES
            and trade.order.orderId not in self._cancel_requested
            and (not trade.order.account or trade.order.account == acct)
            and (inst := instrument_from_contract(trade.contract)) is not None
            and inst.key == instrument.key
        ]

    async def _raise_stop(
        self, contract: Contract, trade: Trade, target: float, tick: float
    ) -> bool:
        order = trade.order
        current = _price(order.auxPrice)
        if current is None:
            logger.warning(
                "IBKR stop order %s has no known stop price; not modifying", order.orderId
            )
            return False
        if target <= current + tick / 2:
            if target < current - tick / 2:
                logger.info(
                    "not moving the IBKR stop %s down from %g to %g", order.orderId, current, target
                )
                return False
            return True  # already there
        modified = copy.copy(order)  # IB modifies an order re-sent with the same orderId
        modified.auxPrice = target
        modified.transmit = True  # bracket children were staged with transmit=False
        mark = len(trade.log)
        try:
            self._ib.placeOrder(contract, modified)
        except (ConnectionError, AssertionError) as exc:  # ib_async asserts on done orders
            logger.warning("could not modify IBKR stop %s: %r", order.orderId, exc)
            return False

        def confirmed() -> bool:
            return abs((_price(trade.order.auxPrice) or 0.0) - target) < tick / 2

        def settled() -> bool:
            return trade.isDone() or _trade_error(trade, mark) is not None or confirmed()

        await _wait_until(settled, self.ORDER_ACK_TIMEOUT)
        error = _trade_error(trade, mark)
        if error is None and not trade.isDone() and confirmed():
            logger.info(
                "moved IBKR stop %s for %s from %g to %g",
                order.orderId,
                contract.symbol,
                current,
                target,
            )
            return True
        if error is not None:
            reason = error[1]
        else:
            reason = "order no longer working" if trade.isDone() else "no confirmation in time"
        logger.warning(
            "IBKR did not confirm moving stop %s from %g to %g: %s",
            order.orderId,
            current,
            target,
            reason,
        )
        # ib_async marks an order whose modification was refused as cancelled although IB
        # keeps it live; a fresh open-orders snapshot restores the real status.
        with contextlib.suppress(BrokerError):
            async with self._open_orders_lock:
                await self._call(
                    self._ib.reqAllOpenOrdersAsync, self.REQUEST_TIMEOUT, "open orders"
                )
        return False


# ---------------------------------------------------------------------- module helpers
def _to_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _mask_account(account: str) -> str:
    return f"{account[:2]}***{account[-3:]}" if len(account) > 5 else "***"


def _rejected(req: OrderRequest, message: str, broker_order_id: str | None = None) -> OrderRecord:
    logger.info("IBKR order for %s rejected: %s", req.instrument.key, message)
    return OrderRecord(
        request=req, status=OrderStatus.REJECTED, broker_order_id=broker_order_id, message=message
    )


def _request_from_trade(trade: Trade) -> OrderRequest | None:
    """Reconstruct an OrderRequest for an order this process did not place."""
    inst = instrument_from_contract(trade.contract)
    order = trade.order
    qty = _to_float(order.totalQuantity) or 0.0
    if qty <= 0:
        status = trade.orderStatus
        qty = (_to_float(status.filled) or 0.0) + (_to_float(status.remaining) or 0.0)
    if inst is None or qty <= 0 or order.action not in ("BUY", "SELL"):
        return None
    side = Side(order.action)
    is_stop = order.orderType.startswith(("STP", "TRAIL"))
    is_limit = order.orderType in ("LMT", "STP LMT")
    ref = getattr(order, "orderRef", None) or ""
    trace_id = ref if isinstance(ref, str) and ref.startswith("ai-trader-") else None
    if side == Side.SELL:
        reason = "protective stop" if is_stop else ("take profit" if order.parentId else "exit")
    else:
        reason = "broker order"
    return OrderRequest(
        instrument=inst,
        side=side,
        qty=qty,
        order_type=OrderType.LIMIT if is_limit else OrderType.MARKET,
        limit_price=_price(order.lmtPrice) if is_limit else None,
        stop_loss=_price(order.auxPrice) if is_stop else None,
        is_exit=side == Side.SELL,  # long-only: every SELL reduces a long position
        reason=reason,
        trace_id=trace_id,
    )
