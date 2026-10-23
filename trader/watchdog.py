"""Independent crypto stop watchdog.

IBKR accepts only limit and market orders for crypto, so a crypto stop lives in this app,
not at the broker. If the app process dies, nothing was selling. This process is separate
(its own container, its own IB API client id) and only reduces positions the bot opened:

- a fresh engine heartbeat means the app is enforcing those stops; the watchdog stays quiet
- a stale heartbeat means the app is unhealthy: sell when the bid is at or below the stop,
  and, past a longer timeout, sell every such crypto position
- a persisted kill switch means flatten those positions even while the heartbeat is fresh

It never buys. Paper accounts may be reduced; a live account is reduced only when
TRADER_ALLOW_LIVE is true (the same gate as the app; shadow mode still exits). Watch-only
(TRADER_WATCHDOG_ORDERS=off) alerts and does not send.

What this cannot do: if IB Gateway itself is down, or IBKR is unreachable, there is still
no broker-side crypto stop to fall back on. The watchdog then alerts and retries its own
connection. A second client id is the most robust option IBKR offers — it survives the app
crashing, and it does not survive the gateway being down.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from trader.broker.base import BrokerError
from trader.config import AppConfig
from trader.db import Database
from trader.models import (
    OPEN_ORDER_STATUSES,
    AssetClass,
    DayStats,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    Side,
)
from trader.notify import Notifier
from trader.risk import sane_exit_price
from trader.timeutil import trading_day

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "engine_heartbeat"
KILL_REASON = "kill switch"
TAG_PREFIX = "ai-trader-wd-"
# One bad bid must not sell. The app uses the same idea (two monitor ticks).
STOP_CONFIRM_TICKS = 2
QTY_EPS = 1e-8


def write_heartbeat(db: Database, now: datetime) -> None:
    """Record that the app just finished checking engine-managed exits."""
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    db.set_kv(HEARTBEAT_KEY, stamp.astimezone(UTC).isoformat())


def read_heartbeat(db: Database) -> datetime | None:
    raw = db.get_kv(HEARTBEAT_KEY)
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


class _Broker(Protocol):
    @property
    def mode(self) -> Mode: ...

    @property
    def connected(self) -> bool: ...

    def connect(self) -> Awaitable[None]: ...

    def disconnect(self) -> Awaitable[None]: ...

    def positions(self) -> Awaitable[list[Position]]: ...

    def open_orders(self) -> Awaitable[list[OrderRecord]]: ...

    def quote(self, instrument: Instrument) -> Awaitable[Quote]: ...

    def place_order(self, req: OrderRequest) -> Awaitable[OrderRecord]: ...

    def round_qty(
        self, instrument: Instrument, qty: float, allow_fractional: bool = False
    ) -> float: ...


class _Alerter(Protocol):
    def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Iterable[str] | None = None,
    ) -> Awaitable[bool]: ...


@dataclass(frozen=True)
class WatchdogSettings:
    """The knobs a tick needs. Built from AppConfig in the process entrypoint."""

    stale_seconds: float = 45
    flatten_seconds: float = 180
    retry_seconds: float = 20
    orders: str = "auto"  # "auto" or "off"
    allow_live: bool = False

    @classmethod
    def from_config(cls, config: AppConfig) -> WatchdogSettings:
        return cls(
            stale_seconds=config.watchdog_stale_seconds,
            flatten_seconds=config.watchdog_flatten_seconds,
            retry_seconds=config.watchdog_retry_seconds,
            orders=config.watchdog_orders,
            allow_live=config.allow_live,
        )


@dataclass(frozen=True)
class Close:
    instrument: Instrument
    qty: float
    kind: str  # "stop", "flatten" or "kill"
    reason: str


@dataclass
class TickResult:
    closes: list[OrderRequest] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def orders_allowed(mode: Mode, settings: WatchdogSettings) -> tuple[bool, str]:
    """Whether this process may send a reduce. It never opens a position either way.

    Paper is allowed (fake money, same gateway port as the app). Live is allowed only
    when the live gate is open, including shadow mode, because the app itself still
    exits there. Watch-only and the simulator send nothing.
    """
    if settings.orders == "off":
        return False, "watch-only (TRADER_WATCHDOG_ORDERS=off)"
    if mode == Mode.SIM:
        return False, "the simulator has no broker connection a second process can use"
    if mode == Mode.PAPER:
        return True, "paper"
    if mode == Mode.LIVE:
        if not settings.allow_live:
            return False, "live trading is not allowed (TRADER_ALLOW_LIVE=false)"
        return True, "live"
    return False, f"unknown broker mode {mode.value}"


def kill_switch_on(db: Database, now: datetime) -> bool:
    """True when the owner pressed KILL and has not resumed.

    The engine copies the halt onto the next trading day. If the app died before that
    roll, the previous day's row still carries it.
    """
    day = trading_day(now)
    stats = db.get_day(day)
    if _killed(stats):
        return True
    if stats is None:
        return _killed(db.latest_day_before(day))
    return False


def _killed(stats: DayStats | None) -> bool:
    return bool(stats and stats.halted and (stats.halt_reason or "").startswith(KILL_REASON))


def _working_sell_qty(orders: list[OrderRecord], key: str) -> float:
    total = 0.0
    for order in orders:
        req = order.request
        if (
            order.status in OPEN_ORDER_STATUSES
            and req.instrument.key == key
            and req.side == Side.SELL
        ):
            total += max(0.0, req.qty - order.filled_qty)
    return total


def new_order_tag(symbol: str, kind: str) -> str:
    """A tag that is unique per send and names the symbol and why."""
    safe = "".join(ch for ch in symbol.upper() if ch.isalnum())[:12] or "X"
    return f"{TAG_PREFIX}{safe}-{kind}-{uuid4().hex[:8]}"


class CryptoWatchdog:
    """One tick reads the shared database and the broker, then maybe sells."""

    def __init__(
        self,
        db: Database,
        broker: _Broker,
        settings: WatchdogSettings,
        notifier: _Alerter,
        clock: Callable[[], datetime],
        *,
        started_at: datetime | None = None,
    ) -> None:
        self.db = db
        self.broker = broker
        self.settings = settings
        self.notifier = notifier
        self._clock = clock
        started = started_at if started_at is not None else clock()
        self._started_at = started if started.tzinfo else started.replace(tzinfo=UTC)
        self._below_stop: dict[str, int] = {}
        self._attempt_at: dict[str, datetime] = {}
        self._active_alerts: set[str] = set()
        self._was_unhealthy = False

    def _now(self) -> datetime:
        now = self._clock()
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    async def tick(self) -> TickResult:
        now = self._now()
        result = TickResult()
        positions = await self.broker.positions()
        open_orders = await self.broker.open_orders()
        metas = self.db.all_position_meta()
        managed = [
            pos
            for pos in positions
            if pos.qty > QTY_EPS
            and pos.instrument.asset_class == AssetClass.CRYPTO
            and pos.instrument.key in metas
        ]
        marks = await self._marks(managed)
        heartbeat = read_heartbeat(self.db)
        age = self._heartbeat_age(heartbeat, now)
        unhealthy = age > self.settings.stale_seconds
        flatten = unhealthy and age >= self.settings.flatten_seconds
        killed = kill_switch_on(self.db, now)
        if not unhealthy and not killed:
            self._below_stop.clear()

        closes: list[Close] = []
        for pos in managed:
            key = pos.instrument.key
            uncovered = pos.qty - _working_sell_qty(open_orders, key)
            if uncovered <= QTY_EPS:
                self._attempt_at.pop(key, None)
                result.skipped.append(f"{key}: a sell is already working")
                continue
            kind, reason = self._why(pos, metas[key], marks.get(key), unhealthy, flatten, killed)
            if kind is None:
                continue
            if self._in_retry_window(key, now):
                result.skipped.append(f"{key}: close already sent, waiting to retry")
                continue
            qty = self.broker.round_qty(pos.instrument, uncovered)
            if qty <= QTY_EPS:
                result.skipped.append(f"{key}: quantity rounds to 0")
                continue
            closes.append(Close(pos.instrument, qty, kind, reason))

        allowed, why = orders_allowed(self.broker.mode, self.settings)
        await self._alert(age, unhealthy, flatten, killed, closes, allowed, why, result)
        if not allowed:
            if closes:
                result.skipped.append(f"not sending: {why}")
            return result
        for close in closes:
            sent = await self._send(close, now, result)
            if sent is not None:
                result.closes.append(sent)
        return result

    def _heartbeat_age(self, heartbeat: datetime | None, now: datetime) -> float:
        """Seconds since the app last checked exits.

        No heartbeat yet (the app is starting, or has never run) is not treated as a
        long outage until this process itself has been up for that long. A clock slightly
        ahead of the app counts as fresh.
        """
        if heartbeat is None:
            return max(0.0, (now - self._started_at).total_seconds())
        return max(0.0, (now - heartbeat).total_seconds())

    def _why(
        self,
        pos: Position,
        meta: dict[str, object],
        mark: float | None,
        unhealthy: bool,
        flatten: bool,
        killed: bool,
    ) -> tuple[str | None, str]:
        key = pos.instrument.key
        symbol = pos.instrument.symbol
        if killed:
            self._below_stop.pop(key, None)
            return "kill", f"kill switch: flatten {symbol}"
        if not unhealthy:
            self._below_stop.pop(key, None)
            return None, ""
        if flatten:
            self._below_stop.pop(key, None)
            return "flatten", f"app heartbeat stale: flatten {symbol}"
        stop = meta.get("stop_loss")
        if not isinstance(stop, int | float) or isinstance(stop, bool) or stop <= 0:
            self._below_stop.pop(key, None)
            return None, ""
        if mark is None or mark > float(stop):
            self._below_stop.pop(key, None)
            return None, ""
        seen = self._below_stop.get(key, 0) + 1
        self._below_stop[key] = seen
        if seen < STOP_CONFIRM_TICKS:
            log.info(
                "%s bid %g is at or below stop %g; confirming on the next poll", key, mark, stop
            )
            return None, ""
        return "stop", f"stop-loss {float(stop):g} hit (watchdog, bid {mark:g})"

    def _in_retry_window(self, key: str, now: datetime) -> bool:
        sent = self._attempt_at.get(key)
        return sent is not None and now - sent < timedelta(seconds=self.settings.retry_seconds)

    async def _marks(self, positions: list[Position]) -> dict[str, float | None]:
        marks: dict[str, float | None] = {}
        for pos in positions:
            key = pos.instrument.key
            try:
                quote = await self.broker.quote(pos.instrument)
            except BrokerError as exc:
                log.warning("watchdog quote %s failed: %s", key, exc)
                marks[key] = None
                continue
            marks[key] = sane_exit_price(quote)
        return marks

    async def _send(self, close: Close, now: datetime, result: TickResult) -> OrderRequest | None:
        tag = new_order_tag(close.instrument.symbol, close.kind)
        req = OrderRequest(
            instrument=close.instrument,
            side=Side.SELL,
            qty=close.qty,
            order_type=OrderType.MARKET,
            is_exit=True,
            reason=close.reason,
            trace_id=tag,
        )
        if req.side != Side.SELL or not req.is_exit:
            raise RuntimeError("watchdog refuses to open or increase a position")
        self._attempt_at[close.instrument.key] = now
        try:
            placed = await self.broker.place_order(req)
        except BrokerError as exc:
            log.warning("watchdog close %s failed: %s", close.instrument.key, exc)
            result.skipped.append(f"{close.instrument.key}: {exc}")
            return None
        self.db.audit(
            "watchdog_close",
            {
                "instrument": close.instrument.key,
                "kind": close.kind,
                "tag": tag,
                "qty": close.qty,
                "status": placed.status.value,
            },
            actor="watchdog",
        )
        log.warning(
            "watchdog %s %s qty %g tag %s -> %s",
            close.kind,
            close.instrument.key,
            close.qty,
            tag,
            placed.status.value,
        )
        return req

    async def _alert(
        self,
        age: float,
        unhealthy: bool,
        flatten: bool,
        killed: bool,
        closes: list[Close],
        allowed: bool,
        why: str,
        result: TickResult,
    ) -> None:
        wanted: list[tuple[str, str, str]] = []
        symbols = ", ".join(close.instrument.symbol for close in closes) or "none"
        if killed and closes:
            wanted.append(
                (
                    "Crypto watchdog: kill switch",
                    f"Kill switch is on. Crypto the bot opened: {symbols}.",
                    "urgent",
                )
            )
        elif flatten and closes:
            wanted.append(
                (
                    "Crypto watchdog: flattening crypto",
                    f"App heartbeat is {age:.0f}s old (limit {self.settings.flatten_seconds:g}s). "
                    f"Selling {symbols}.",
                    "urgent",
                )
            )
        elif unhealthy and any(close.kind == "stop" for close in closes):
            wanted.append(
                (
                    "Crypto watchdog: crypto stop",
                    f"App heartbeat is {age:.0f}s old. Selling {symbols} at the stop.",
                    "high",
                )
            )
        elif unhealthy and not closes and self._was_unhealthy:
            pass
        if closes and not allowed:
            wanted.append(
                (
                    "Crypto watchdog: not sending orders",
                    f"Would sell {symbols}, but this process is not allowed to: {why}.",
                    "urgent",
                )
            )
        if not unhealthy and not killed and self._was_unhealthy:
            wanted.append(
                (
                    "Crypto watchdog: app heartbeat restored",
                    "The app is checking exits again. The watchdog is standing by.",
                    "default",
                )
            )
        self._was_unhealthy = unhealthy or killed
        titles = {title for title, _, _ in wanted}
        for title, message, priority in wanted:
            if title in self._active_alerts:
                continue
            self._active_alerts.add(title)
            result.alerts.append(title)
            await self.notifier.send(title, message, priority=priority, tags=["warning"])
        self._active_alerts &= titles


async def run_watchdog(
    config: AppConfig,
    db: Database,
    broker: _Broker,
    notifier: Notifier,
    *,
    clock: Callable[[], datetime] | None = None,
    stop: asyncio.Event | None = None,
    max_ticks: int | None = None,
) -> None:
    """Connect and poll until `stop` is set or `max_ticks` ticks have run.

    A broker error is logged and retried on the next poll. The process does not exit,
    so a container restart policy does not flap while the gateway is down.
    """
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    watchdog = CryptoWatchdog(
        db, broker, WatchdogSettings.from_config(config), notifier, clock or _utc_now
    )
    ticks = 0
    while not stop.is_set():
        try:
            if not broker.connected:
                await broker.connect()
            await watchdog.tick()
        except BrokerError as exc:
            log.warning("crypto watchdog: %s", exc)
        except Exception:
            log.exception("crypto watchdog tick failed")
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.watchdog_poll_seconds)
        except TimeoutError:
            continue
    if broker.connected:
        await broker.disconnect()


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def idle_forever(stop: asyncio.Event | None = None) -> None:
    """Stay up without connecting to a broker (disabled, or not an IBKR deployment)."""
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()
