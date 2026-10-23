"""Trading engine: the loops that tie the broker, scanner, decision maker, risk manager and
journal together (docs/SPEC.md 5.7 and 7.2).

- Monitor loop (every `monitor_seconds`): keep the broker connected, refresh the
  account/positions/orders cache (positions marked at the bid), evaluate the daily halts,
  manage exits (engine-side stops for positions without native stops, a backup stop for
  stocks, time stop, breakeven and trailing stops), re-place missing stock stops after a
  reconnect, flatten stocks before the close, then journal order statuses and fills and
  rebuild round-trip trades FIFO, expire approvals and score the signal ledger.
- Decision loop (every `cycle_seconds`): refresh state, build and prefilter the universe,
  fetch quotes and bars, scan, log every candidate in the signal ledger, ask the decision
  maker (Claude or rules), then size and risk-check each BUY before it becomes an order.

`stop()` ends the decision loop only: the monitor keeps protecting open positions
(engine-managed crypto stops, halts, flatten before the close) until `shutdown()`.
Exit rules only touch positions the engine opened (those with positions_meta); the kill
switch and the daily-loss flatten close everything. In shadow mode (live account, not
armed) nothing is sent except an owner-confirmed kill. Every broker failure is logged and
survived; when in doubt, no order is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from trader.broker.base import Broker, BrokerError
from trader.config import AppConfig
from trader.db import Database
from trader.models import (
    LEVERAGED_ETFS,
    OPEN_ORDER_STATUSES,
    AccountSnapshot,
    AIDecision,
    AssetClass,
    Bar,
    BotSettings,
    Candidate,
    DayStats,
    DecisionBatch,
    DecisionContext,
    EngineStatus,
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
    SignalRecord,
    TradeAction,
    TradeRecord,
    utcnow,
)
from trader.notify import Notifier
from trader.risk import (
    HALT_DAILY_LOSS,
    RiskManager,
    available_cash,
    is_soft_halt,
    max_position_value,
)
from trader.strategy.decide import DecisionMaker, RuleBasedDecisionMaker
from trader.strategy.signals import Scanner
from trader.timeutil import (
    in_entry_window,
    market_close_time,
    minutes_to_close,
    stock_market_open,
    trading_day,
)

log = logging.getLogger(__name__)

KILL_REASON = "kill switch"
APPROVAL_TTL = timedelta(minutes=10)
ENTRY_ORDER_TTL = timedelta(minutes=10)  # unfilled entry orders are cancelled after this
EQUITY_POINT_EVERY = timedelta(minutes=5)
BROKER_DOWN_NOTIFY_AFTER = timedelta(seconds=60)
BACKUP_STOP_BREACH = 0.005  # stocks: engine sells only 0.5% beyond a stop the broker lacks
FLATTEN_BEFORE_CLOSE_MIN = 10.0
CLOSE_RETRY = timedelta(seconds=60)  # do not resend an engine close sooner than this
COMMISSION_GRACE = timedelta(minutes=2)  # wait this long for late commission reports
STALE_META_GRACE = timedelta(minutes=2)
ORDER_FORGET_AFTER = timedelta(days=1)
FILL_LOOKBACK = timedelta(days=2)
FIRST_FILL_LOOKBACK = timedelta(days=7)
TRADE_LOOKBACK = timedelta(days=30)
FILL_QUERY_LIMIT = 5000
SIGNAL_DEDUPE = timedelta(minutes=30)  # one ledger row per instrument+setup per window
LEDGER_EVERY = timedelta(minutes=5)
LEDGER_MAX_INSTRUMENTS = 5  # bar requests per ledger pass
LEDGER_SETTLE = timedelta(minutes=5)  # wait for the last bars after the horizon
LEDGER_GIVE_UP = timedelta(days=5)
LEDGER_RETRY = timedelta(hours=1)  # wait before re-requesting bars that were missing
CRYPTO_HORIZON = timedelta(hours=24)
FETCH_CONCURRENCY = 4
FIRST_CYCLE_DELAY = 5.0
MAX_BACKOFF = 120.0
EPS = 1e-9
BAR_LOOKBACK = {"1 min": "2 D", "5 mins": "5 D", "15 mins": "10 D"}
EXTRA_PREFIX = "position_extra:"
BASELINE_PREFIX = "halt_baseline:"


class SignalScanner(Protocol):
    settings: BotSettings

    def scan(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None, now: datetime
    ) -> list[Candidate]: ...

    def snapshot(
        self, instrument: Instrument, bars: Sequence[Bar], quote: Quote | None = None
    ) -> dict[str, float]: ...


# --------------------------------------------------------------------------- round trips
@dataclass
class _Lot:
    qty: float
    price: float
    commission: float  # buy commission not yet attributed to a closed quantity
    ts: datetime


@dataclass
class _Trip:
    qty: float = 0.0
    cost: float = 0.0
    proceeds: float = 0.0
    commission: float = 0.0
    entry_ts: datetime | None = None
    exit_ts: datetime | None = None
    last_sell: Fill | None = None
    fills: list[Fill] = field(default_factory=list)


def replay_fills(
    fills: Iterable[Fill], fallback_entry: tuple[float, datetime] | None = None
) -> tuple[list[_Trip], float]:
    """FIFO-match one instrument's fills (oldest first) into completed round trips.

    Exits are matched by instrument, never by order id (IBKR bracket children and orders
    from other clients carry their own ids). A sell with no open lot uses `fallback_entry`
    (price, time) from the position metadata when given, else it is skipped. Returns the
    completed trips and the quantity still open.
    """
    trips: list[_Trip] = []
    lots: deque[_Lot] = deque()
    trip = _Trip()
    for fill in fills:
        if fill.qty <= EPS:
            continue
        trip.fills.append(fill)
        if fill.side == Side.BUY:
            lots.append(_Lot(fill.qty, fill.price, fill.commission, fill.ts))
            trip.entry_ts = trip.entry_ts or fill.ts
            continue
        remaining = fill.qty
        while remaining > EPS:
            if not lots:
                if fallback_entry is None:
                    break
                price, ts = fallback_entry
                lots.append(_Lot(remaining, price, 0.0, ts))
                trip.entry_ts = trip.entry_ts or ts
            lot = lots[0]
            take = min(lot.qty, remaining)
            share = lot.commission * take / lot.qty
            trip.cost += take * lot.price
            trip.commission += share
            trip.qty += take
            lot.commission -= share
            lot.qty -= take
            remaining -= take
            if lot.qty <= EPS:
                lots.popleft()
        matched = fill.qty - remaining
        if matched > EPS:
            trip.proceeds += matched * fill.price
            trip.commission += fill.commission * matched / fill.qty
            trip.exit_ts = fill.ts
            trip.last_sell = fill
        if remaining > EPS:
            log.warning(
                "sell of %g %s has no matching buy in the journal; %g left unmatched",
                fill.qty,
                fill.instrument.key,
                remaining,
            )
        if not lots:
            if trip.qty > EPS:
                trips.append(trip)
            trip = _Trip()
    return trips, sum(lot.qty for lot in lots)


# --------------------------------------------------------------------------- signal ledger
def score_signal(
    sig: SignalRecord, bars: Sequence[Bar], end: datetime, bar_seconds: float = 60.0
) -> SignalRecord | None:
    """Outcome of a ledger signal in R multiples (1R = price - stop) over the bars from the
    signal to `end` (the session close for stocks, +24h for crypto). When one bar touches
    both levels, the stop counts as hit first. None when there are no bars to score."""
    risk = sig.price - sig.stop
    length = timedelta(seconds=bar_seconds)
    window = [b for b in bars if b.ts >= sig.ts and b.ts + length <= end + timedelta(seconds=1)]
    if risk <= 0 or not window:
        return None

    def r(price: float) -> float:
        return round((price - sig.price) / risk, 3)

    def r_at(minutes: int) -> float:
        horizon = min(sig.ts + timedelta(minutes=minutes), end)
        done = [b for b in window if b.ts + length <= horizon + timedelta(seconds=1)]
        return r(done[-1].close if done else window[0].open)

    first_hit: str = "neither"
    for bar in window:
        if bar.low <= sig.stop:
            first_hit = "stop"
            break
        if bar.high >= sig.target:
            first_hit = "target"
            break
    return sig.model_copy(
        update={
            "r_15m": r_at(15),
            "r_30m": r_at(30),
            "r_60m": r_at(60),
            "r_close": r(window[-1].close),
            "mfe_r": max(0.0, r(max(b.high for b in window))),
            "mae_r": min(0.0, r(min(b.low for b in window))),
            "first_hit": first_hit,
            "evaluated": True,
        }
    )


@dataclass
class _BuyOutcome:
    setup: str | None
    taken: bool
    order_id: int | None = None
    skip_reason: str | None = None


@dataclass
class _Logged:
    """A candidate's signal-ledger row during one cycle."""

    cand: Candidate
    signal_id: int
    new: bool  # False when it reuses a recent row for the same instrument+setup
    blocked: str | None  # why it was kept from the decision maker, if it was


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _rests_standalone_stops(broker: Broker, instrument: Instrument) -> bool:
    """Whether `place_order(SELL, is_exit, stop_loss=X)` rests a stop order at this broker
    (the SimBroker convention) instead of selling at market. Brokers opt in with a
    `supports_standalone_stops(instrument)` method; without it the engine never re-places
    stops, because a broker that ignores stop_loss on exits would sell immediately."""
    check = getattr(broker, "supports_standalone_stops", None)
    return bool(callable(check) and check(instrument))


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Engine:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        broker: Broker,
        decider: DecisionMaker,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utcnow,
        *,
        scanner: SignalScanner | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.broker = broker
        self.decider = decider
        self.notifier = notifier
        self._clock = clock
        self.settings = db.get_settings()
        self.scanner: SignalScanner = scanner or Scanner(self.settings, tz=config.market_timezone)
        self.risk = risk or RiskManager(self.settings.risk)
        fallback = getattr(decider, "fallback", None)
        if isinstance(decider, RuleBasedDecisionMaker):
            self.rules = decider
        elif isinstance(fallback, RuleBasedDecisionMaker):
            self.rules = fallback
        else:
            self.rules = RuleBasedDecisionMaker(self.settings)
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

        self._running = False
        self._monitor_task: asyncio.Task[None] | None = None
        self._decision_task: asyncio.Task[None] | None = None
        self._cycle_lock = asyncio.Lock()
        self._monitor_lock = asyncio.Lock()
        self._entry_lock = asyncio.Lock()

        # cached state (snapshot() and status() never call the broker)
        self._account: AccountSnapshot | None = None
        self._positions: list[Position] = []
        self._open_orders: list[OrderRecord] = []
        self._market: dict[str, dict[str, float]] = {}
        self._day: DayStats | None = None
        self._errors: dict[str, tuple[datetime, str]] = {}
        self._last_cycle_at: datetime | None = None
        self._next_cycle_at: datetime | None = None
        self._last_equity_at: datetime | None = None

        # connectivity
        self._down_since: datetime | None = None
        self._down_notified = False
        self._reprotect_pending = True

        # journal bookkeeping
        self._fill_commissions: dict[str, tuple[float, datetime]] | None = None
        self._fills_synced = False
        self._last_exit: dict[str, datetime] | None = None
        self._open_qty: dict[str, float] = {}
        self._pending_trades: set[str] = set()

        # exits and halts
        self._closing: dict[str, datetime] = {}
        self._close_reasons: dict[str, str] = {}
        self._last_halt_at: dict[str, datetime] = {}
        self._skip_logged: set[tuple[str, str]] = set()
        self._mismatch_logged: set[str] = set()
        self._ledger_retry: dict[str, datetime] = {}  # instrument -> no bar requests until
        self._fallback_logged: str | None = None
        self._last_ledger_at: datetime | None = None

    # ------------------------------------------------------------------ basics
    def _now(self) -> datetime:
        return _aware(self._clock()).astimezone(UTC)

    @property
    def shadow(self) -> bool:
        """Live account without the owner's arming: decisions are logged, orders blocked."""
        armed = self.config.allow_live and self.db.get_flag("live_armed")
        return self.broker.mode == Mode.LIVE and not armed

    def _set_error(self, source: str, message: str) -> None:
        self._errors[source] = (self._now(), _short(message, 300))

    def _clear_error(self, source: str) -> None:
        self._errors.pop(source, None)

    @property
    def last_error(self) -> str | None:
        if not self._errors:
            return None
        return max(self._errors.values(), key=lambda item: item[0])[1]

    async def _notify(
        self, title: str, message: str, priority: str = "default", tags: list[str] | None = None
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.send(title, message, priority=priority, tags=tags)
        except Exception as exc:  # Notifier never raises; this guards custom notifiers
            log.warning("notification failed: %s", type(exc).__name__)

    async def _step(self, name: str, work: Awaitable[object]) -> None:
        """Run one monitor step; a failure is logged and the remaining steps still run."""
        source = f"monitor:{name}"
        try:
            await work
        except BrokerError as exc:
            log.warning("monitor step %s: broker error: %s", name, exc)
            self._set_error(source, f"{name}: {exc}")
        except Exception as exc:
            log.exception("monitor step %s failed", name)
            self._set_error(source, f"{name}: {type(exc).__name__}: {exc}")
        else:
            self._clear_error(source)

    # ------------------------------------------------------------------ day stats
    def _today(self, now: datetime) -> DayStats:
        day = trading_day(now)
        if self._day is not None and self._day.day == day:
            return self._day
        stats = self.db.get_day(day)
        if stats is None:
            before = self.db.latest_day_before(day)
            stats = DayStats(day=day, loss_streak=before.loss_streak if before else 0)
            # The kill switch stays on across days until the owner resumes.
            if before and before.halted and (before.halt_reason or "").startswith(KILL_REASON):
                stats.halted, stats.halt_reason = True, before.halt_reason
            self.db.upsert_day(stats)
        self._day = stats
        self._skip_logged.clear()
        return stats

    def _save_day(self) -> None:
        if self._day is not None:
            self.db.upsert_day(self._day)

    def _stats_for(self, day: str) -> DayStats:
        if self._day is not None and self._day.day == day:
            return self._day
        return self.db.get_day(day) or DayStats(day=day)

    def _add_fee(self, ts: datetime, amount: float) -> None:
        if abs(amount) < EPS:
            return
        stats = self._stats_for(trading_day(ts))
        stats.fees = round(stats.fees + amount, 4)
        self.db.upsert_day(stats)

    def _halt_eval_day(self, day: DayStats) -> DayStats:
        """After a resume, halts are measured from the equity at the resume."""
        raw = self.db.get_kv(BASELINE_PREFIX + day.day)
        if raw:
            with contextlib.suppress(ValueError):
                return day.model_copy(update={"start_equity": float(raw)})
        return day

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Connect and start both loops (idempotent). Connection errors are retried by the
        monitor loop."""
        if self._decision_task is not None and not self._decision_task.done():
            return
        self._running = True
        self.db.set_flag("engine_running", True)
        self._reprotect_pending = True
        try:
            await self.broker.connect()
        except BrokerError as exc:
            log.warning("broker connect failed at start: %s", exc)
            self._set_error("monitor", f"connect: {exc}")
            self._down_since = self._down_since or self._now()
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(
                self._loop("monitor", self.monitor_tick, lambda: self.settings.monitor_seconds),
                name="engine-monitor",
            )
        self._decision_task = asyncio.create_task(self._decision_loop(), name="engine-decision")
        log.info("engine started (%s, %s)", self.broker.name, self.broker.mode.value)

    async def stop(self) -> None:
        """Stop making decisions. Positions, broker stops and the monitor loop (which
        manages engine-side stops) stay in place."""
        self._running = False
        self.db.set_flag("engine_running", False)
        task, self._decision_task = self._decision_task, None
        self._next_cycle_at = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        log.info("engine stopped (monitor keeps protecting open positions)")

    async def shutdown(self) -> None:
        """Stop everything and disconnect (app shutdown). Broker-side stops remain."""
        running = self._running
        await self.stop()
        self.db.set_flag("engine_running", running)  # restart resumes the previous state
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await self.broker.disconnect()
        except Exception as exc:
            log.warning("broker disconnect failed: %s", type(exc).__name__)

    async def _decision_loop(self) -> None:
        await self._sleep(FIRST_CYCLE_DELAY)
        await self._loop("decision", self.run_cycle, lambda: self.settings.cycle_seconds)

    async def _loop(
        self, name: str, step: Callable[[], Awaitable[object]], interval: Callable[[], float]
    ) -> None:
        """Run `step` forever: errors are logged and recorded, never raised, with
        exponential backoff while they persist."""
        failures = 0
        while True:
            try:
                await step()
            except BrokerError as exc:
                failures += 1
                log.warning("%s loop: broker error: %s", name, exc)
                self._set_error(name, f"{name}: {exc}")
            except Exception as exc:
                failures += 1
                log.exception("%s loop failed", name)
                self._set_error(name, f"{name}: {type(exc).__name__}: {exc}")
            else:
                failures = 0
                self._clear_error(name)
            base = float(interval())
            delay = base
            if failures:
                delay = min(base * 2 ** min(failures, 6), max(MAX_BACKOFF, base))
            if name == "decision":
                self._next_cycle_at = self._now() + timedelta(seconds=delay)
            await self._sleep(delay)

    # ------------------------------------------------------------------ owner actions
    async def kill(self, actor: str = "owner") -> None:
        """Halt, cancel every order and close every position (also in shadow mode: the
        owner confirmed it). Failures are recorded and notified, never raised."""
        now = self._now()
        day = self._today(now)
        day.halted, day.halt_reason = True, KILL_REASON
        self._save_day()
        log.warning("KILL switch pressed by %s", actor)
        self._cancel_pending("cancelled: kill switch")
        for pos in self._positions:
            self._closing[pos.instrument.key] = now
            self._close_reasons[pos.instrument.key] = KILL_REASON
        try:
            async with self._entry_lock:  # no entry can slip in after the flatten
                if not self.broker.connected:
                    await self.broker.connect()
                await self.broker.cancel_all()
                records = await self.broker.flatten_all(KILL_REASON)
        except Exception as exc:
            log.exception("kill switch could not finish")
            self._set_error("kill", f"kill switch failed: {exc}")
            await self._notify(
                "KILL switch FAILED",
                f"Could not cancel/close everything: {_short(str(exc))}. Check the broker now.",
                priority="urgent",
                tags=["rotating_light"],
            )
            return
        self._clear_error("kill")
        for rec in records:
            self._journal_order(rec, now)
        await self._notify(
            "Kill switch",
            f"Trading halted, orders cancelled, {len(records)} position(s) being closed.",
            priority="high",
            tags=["stop_sign"],
        )

    async def resume(self, actor: str = "owner") -> None:
        """Clear today's halt. Halt limits are re-measured from the current equity."""
        now = self._now()
        day = self._today(now)
        previous = day.halt_reason
        day.halted, day.halt_reason = False, None
        self._save_day()
        equity = self._account.equity if self._account is not None else None
        if equity is not None and equity > 0:
            self.db.set_kv(BASELINE_PREFIX + day.day, repr(equity))
        self._clear_error("kill")
        log.warning("trading resumed by %s (was: %s)", actor, previous)
        await self._notify("Trading resumed", f"Halt cleared by {actor} (was: {previous}).")

    async def arm_live(self, actor: str = "owner") -> None:
        """Allow real-money orders. The caller has already verified a fresh TOTP code."""
        if not self.config.allow_live:
            raise PermissionError("live trading is disabled on the server (TRADER_ALLOW_LIVE)")
        if self.broker.mode != Mode.LIVE:
            raise PermissionError("the broker is not connected to a live account")
        self.db.set_flag("live_armed", True)
        log.warning("live trading ARMED by %s", actor)
        await self._notify("Live trading armed", f"Real-money orders enabled by {actor}.", "high")

    async def disarm_live(self, actor: str = "owner") -> None:
        """Back to shadow mode on a live account: the engine sends nothing, so the owner
        manages any open positions (broker-side stops stay in place)."""
        self.db.set_flag("live_armed", False)
        log.warning("live trading disarmed by %s", actor)
        if self.shadow and any(p.qty > 0 for p in self._positions):
            await self._notify(
                "Live trading disarmed",
                "Engine exits are paused for open positions; broker stops remain. "
                "Manage or close them yourself.",
                priority="high",
                tags=["warning"],
            )

    def reload_settings(self) -> None:
        """Re-read BotSettings from the database and apply them everywhere."""
        self.settings = self.db.get_settings()
        self.risk.set_limits(self.settings.risk)
        self.scanner.settings = self.settings
        self.rules.settings = self.settings
        fallback = getattr(self.decider, "fallback", None)
        if isinstance(fallback, RuleBasedDecisionMaker):
            fallback.settings = self.settings
        from trader.strategy.claude import ClaudeDecisionMaker

        if isinstance(self.decider, ClaudeDecisionMaker):
            self.decider.model = self.settings.llm_model
            self.decider.effort = self.settings.llm_effort

    # ------------------------------------------------------------------ approvals
    async def approve_order(self, order_id: int, actor: str = "owner") -> OrderRecord:
        """Send a pending order after a fresh risk check. Raises LookupError (unknown id),
        ValueError (not pending) or BrokerError (no fresh data: the order stays pending)."""
        self._pending(order_id)
        async with self._entry_lock:
            rec = self._pending(order_id)  # again: a concurrent approval may have sent it
            now = self._now()
            if now - _aware(rec.created_at) >= APPROVAL_TTL:
                self._expire(rec)
                return self._get_order(order_id)
            await self._ensure_connected(now)
            req = rec.request
            quote = await self.broker.quote(req.instrument)
            self._track_halt(req.instrument.key, quote, now)
            account = await self.broker.account()
            positions = await self.broker.positions()
            open_orders = await self.broker.open_orders()
            day = self._today(now)
            verdict = None
            if req.instrument.key in self._pending_trades:
                reasons = ["previous trade in this symbol is still being journaled"]
            else:
                verdict = self.risk.check(
                    req,
                    quote=quote,
                    account=account,
                    positions=positions,
                    open_orders=open_orders,
                    day=day,
                    mode=self.broker.mode,
                    data_delayed=self.broker.data_delayed,
                    broker=self.broker,
                    now=now,
                    settings=self.settings,
                    last_halt_at=self._last_halt_at.get(req.instrument.key),
                )
                reasons = verdict.reasons
            if verdict is None or not verdict.approved:
                message = "re-check at approval failed: " + "; ".join(reasons)
                self.db.update_order(order_id, status=OrderStatus.BLOCKED, message=message)
                self._revert_signal(order_id, f"risk at approval: {reasons[0]}")
                return self._get_order(order_id)
            if self.shadow:
                self.db.update_order(
                    order_id, status=OrderStatus.BLOCKED, message="shadow mode: live not armed"
                )
                self._revert_signal(order_id, "shadow mode")
                return self._get_order(order_id)
            order = verdict.order or req
            if order.is_exit:
                placed = await self.broker.close_position(order.instrument, order.reason)
                if placed is None:
                    self.db.update_order(
                        order_id, status=OrderStatus.CANCELLED, message="no position to close"
                    )
                    return self._get_order(order_id)
                self._closing[order.instrument.key] = now
                self._close_reasons[order.instrument.key] = order.reason
            else:
                placed = await self._send_entry(order, now)
            self.db.update_order(
                order_id,
                request=order,
                status=placed.status,
                broker_order_id=placed.broker_order_id,
                filled_qty=placed.filled_qty,
                avg_fill_price=placed.avg_fill_price,
                commission=placed.commission,
                message=_short(f"approved by {actor}; {placed.message}".rstrip("; ")),
            )
            if not order.is_exit and placed.status not in _ACCEPTED:
                self._revert_signal(order_id, f"broker rejected: {placed.message}")
            log.info("order %d approved by %s -> %s", order_id, actor, placed.status.value)
            return self._get_order(order_id)

    async def reject_order(self, order_id: int, actor: str = "owner") -> OrderRecord:
        """Cancel a pending order. Raises LookupError / ValueError like approve_order."""
        self._pending(order_id)
        self.db.update_order(order_id, status=OrderStatus.CANCELLED, message=f"rejected by {actor}")
        self._revert_signal(order_id, f"rejected by {actor}")
        return self._get_order(order_id)

    def _pending(self, order_id: int) -> OrderRecord:
        rec = self.db.get_order(order_id)
        if rec is None:
            raise LookupError(f"order {order_id} not found")
        if rec.status != OrderStatus.PENDING_APPROVAL:
            raise ValueError(f"order {order_id} is not pending approval ({rec.status.value})")
        return rec

    def _get_order(self, order_id: int) -> OrderRecord:
        rec = self.db.get_order(order_id)
        if rec is None:  # pragma: no cover - rows are never deleted
            raise LookupError(f"order {order_id} not found")
        return rec

    def _awaiting_approval(self) -> set[str]:
        return {
            rec.request.instrument.key
            for rec in self.db.list_orders(limit=200, statuses=[OrderStatus.PENDING_APPROVAL])
        }

    def _expire(self, rec: OrderRecord) -> None:
        if rec.id is None:
            return
        self.db.update_order(rec.id, status=OrderStatus.CANCELLED, message="approval expired")
        self._revert_signal(rec.id, "approval expired")
        self.db.audit(
            "approval_expired",
            {"order_id": rec.id, "symbol": rec.request.instrument.symbol},
            actor="engine",
        )

    def _expire_approvals(self, now: datetime) -> None:
        for rec in self.db.list_orders(limit=200, statuses=[OrderStatus.PENDING_APPROVAL]):
            if now - _aware(rec.created_at) >= APPROVAL_TTL:
                self._expire(rec)

    def _cancel_pending(self, message: str) -> None:
        for rec in self.db.list_orders(limit=200, statuses=[OrderStatus.PENDING_APPROVAL]):
            if rec.id is not None:
                self.db.update_order(rec.id, status=OrderStatus.CANCELLED, message=message)
                self._revert_signal(rec.id, message)

    # ------------------------------------------------------------------ status
    def status(self) -> EngineStatus:
        now = self._now()
        day = self._day
        equity = self._account.equity if self._account is not None else None
        start = day.start_equity if day is not None else None
        return EngineStatus(
            running=self._running,
            mode=self.broker.mode,
            broker=self.broker.name,
            connected=self.broker.connected,
            halted=bool(day and day.halted),
            halt_reason=day.halt_reason if day else None,
            autopilot=self.settings.autopilot,
            live_armed=self.db.get_flag("live_armed"),
            live_allowed=self.config.allow_live,
            shadow=self.shadow,
            stock_market_open=stock_market_open(now),
            last_cycle_at=self._last_cycle_at,
            next_cycle_at=self._next_cycle_at,
            last_error=self.last_error,
            day=day.day if day else trading_day(now),
            start_equity=start,
            equity=equity,
            pnl_today=round(equity - start, 2) if equity is not None and start else 0.0,
            pnl_target=self.risk.limits.daily_profit_target_usd,
            trades_today=day.trades if day else 0,
            llm_cost_today=round(day.llm_cost, 4) if day else 0.0,
            data_delayed=self.broker.data_delayed,
        )

    def snapshot(self) -> dict[str, Any]:
        """Cached {"account": AccountSnapshot | None, "positions": [Position],
        "open_orders": [OrderRecord]} from the last monitor tick. Never calls the broker."""
        return {
            "account": self._account,
            "positions": list(self._positions),
            "open_orders": list(self._open_orders),
        }

    # ------------------------------------------------------------------ connectivity
    async def _ensure_connected(self, now: datetime) -> None:
        if self.broker.connected:
            if self._down_since is not None:
                await self._on_reconnected(now)
            return
        if self._down_since is None:
            self._down_since = now
            log.warning("broker %s is disconnected", self.broker.name)
            self.db.audit("broker_disconnected", {"broker": self.broker.name}, actor="engine")
        if not self._down_notified and now - self._down_since >= BROKER_DOWN_NOTIFY_AFTER:
            self._down_notified = True
            await self._notify(
                "Broker disconnected",
                f"{self.broker.name} has been unreachable since "
                f"{self._down_since:%H:%M:%S} UTC. Broker-side stops still protect stocks.",
                priority="high",
                tags=["warning"],
            )
        await self.broker.connect()
        if not self.broker.connected:
            raise BrokerError("broker did not report connected after connect()")
        await self._on_reconnected(now)

    async def _on_reconnected(self, now: datetime) -> None:
        down_for = (now - self._down_since).total_seconds() if self._down_since else 0.0
        log.info("broker connected again after %.0fs", down_for)
        self.db.audit("broker_reconnected", {"down_seconds": round(down_for)}, actor="engine")
        if self._down_notified:
            await self._notify("Broker reconnected", f"Connection restored after {down_for:.0f}s.")
        self._down_since = None
        self._down_notified = False
        self._reprotect_pending = True

    # ------------------------------------------------------------------ monitor
    async def monitor_tick(self) -> None:
        """One monitor pass. Raises BrokerError when the broker is unreachable."""
        async with self._monitor_lock:
            now = self._now()
            day = self._today(now)
            self._expire_approvals(now)
            await self._ensure_connected(now)
            account = await self.broker.account()
            raw_positions = await self.broker.positions()
            open_orders = await self.broker.open_orders()
            metas = self.db.all_position_meta()
            quotes = await self._quotes([p.instrument for p in raw_positions if p.qty > 0], now)
            positions = self._mark_positions(raw_positions, quotes, metas)
            account = self._mark_account(account, raw_positions, positions)
            self._account, self._positions, self._open_orders = account, positions, open_orders

            if math.isfinite(account.equity) and account.equity > 0:
                changed = False
                if day.start_equity is None:
                    day.start_equity, changed = account.equity, True
                if day.end_equity != account.equity:
                    day.end_equity, changed = account.equity, True
                if changed:
                    self._save_day()
                if self._last_equity_at is None or now - self._last_equity_at >= EQUITY_POINT_EVERY:
                    self.db.add_equity_point(account.equity, ts=now)
                    self._last_equity_at = now
                await self._step("halts", self._check_halts(now, account))
            else:
                # e.g. IBKR before the account summary arrives: never halt or flatten on it
                log.warning("account equity %r is not usable; halts not evaluated", account.equity)
                self._set_error("monitor:halts", "account equity not available")
            if not self.shadow:
                await self._step("exits", self._manage_positions(now, positions, quotes, metas))
                await self._step("flatten", self._flatten_before_close(now, positions, metas))
                await self._step("re-protect", self._reprotect(now, positions, open_orders, metas))
            await self._step("journal", self._sync_journal(now, positions, open_orders))
            await self._step("signals", self._evaluate_signals(now))

    async def _quotes(self, instruments: Sequence[Instrument], now: datetime) -> dict[str, Quote]:
        """Quotes for several instruments (concurrency-limited); failures are skipped."""
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(inst: Instrument) -> tuple[str, Quote | None]:
            async with sem:
                try:
                    return inst.key, await self.broker.quote(inst)
                except Exception as exc:
                    log.warning("quote for %s failed: %s", inst.key, exc)
                    return inst.key, None

        out: dict[str, Quote] = {}
        for key, quote in await asyncio.gather(*(one(i) for i in instruments)):
            if quote is not None:
                self._track_halt(key, quote, now)
                out[key] = quote
        return out

    def _track_halt(self, key: str, quote: Quote, now: datetime) -> None:
        if quote.halted:
            if key not in self._last_halt_at or now - self._last_halt_at[key] > CLOSE_RETRY:
                log.info("%s is halted", key)
            self._last_halt_at[key] = now

    def _mark_positions(
        self, raw: list[Position], quotes: dict[str, Quote], metas: dict[str, dict[str, Any]]
    ) -> list[Position]:
        """Positions valued at the bid (what they could be sold for) with engine metadata."""
        out: list[Position] = []
        for pos in raw:
            key = pos.instrument.key
            quote = quotes.get(key)
            mark = quote.exit_price if quote is not None and quote.exit_price else pos.market_price
            meta = metas.get(key, {})
            out.append(
                pos.model_copy(
                    update={
                        "market_price": mark,
                        "unrealized_pnl": (
                            (mark - pos.avg_cost) * pos.qty if mark else pos.unrealized_pnl
                        ),
                        "stop_loss": meta.get("stop_loss"),
                        "take_profit": meta.get("take_profit"),
                        "opened_at": meta.get("opened_at") or pos.opened_at,
                        "setup": meta.get("setup"),
                    }
                )
            )
        return out

    @staticmethod
    def _mark_account(
        account: AccountSnapshot, raw: list[Position], marked: list[Position]
    ) -> AccountSnapshot:
        """Re-value the broker's equity with positions at the bid."""
        adjust = 0.0
        for before, after in zip(raw, marked, strict=True):
            if before.market_price and after.market_price:
                adjust += before.qty * (after.market_price - before.market_price)
        return account.model_copy(
            update={
                "equity": account.equity + adjust,
                "unrealized_pnl": sum(p.unrealized_pnl for p in marked),
            }
        )

    # ------------------------------------------------------------------ halts
    async def _check_halts(self, now: datetime, account: AccountSnapshot) -> None:
        day = self._today(now)
        reason = self.risk.halt_reason(account=account, day=self._halt_eval_day(day))
        if reason is None:
            return
        hard = reason.startswith(HALT_DAILY_LOSS)
        if day.halted and not (hard and is_soft_halt(day.halt_reason)):
            return  # already halted; only a soft halt can escalate to a hard one
        day.halted, day.halt_reason = True, reason
        self._save_day()
        log.warning("trading halted: %s", reason)
        self.db.audit("halt", {"reason": reason}, actor="engine")
        self._cancel_pending(f"cancelled: trading halted ({reason})")
        flatten = hard and self.risk.limits.flatten_on_halt
        detail = reason
        if flatten and self.shadow:
            detail += " (shadow mode: nothing flattened)"
        elif flatten:
            try:
                records = await self._flatten_all(reason, now)
                detail += f"; closing {len(records)} position(s)"
            except Exception as exc:
                log.exception("flatten after halt failed")
                self._set_error("flatten", f"flatten failed: {exc}")
                await self._notify(
                    "Flatten FAILED",
                    f"Halted ({reason}) but could not close positions: {_short(str(exc))}",
                    priority="urgent",
                    tags=["rotating_light"],
                )
        await self._notify(
            "Trading halted",
            detail,
            priority="high" if hard else "default",
            tags=["warning"] if hard else ["white_check_mark"],
        )

    async def _flatten_all(self, reason: str, now: datetime) -> list[OrderRecord]:
        for pos in self._positions:
            self._closing[pos.instrument.key] = now
            self._close_reasons[pos.instrument.key] = reason
        async with self._entry_lock:  # no entry can slip in after the flatten
            await self.broker.cancel_all()
            records = await self.broker.flatten_all(reason)
        for rec in records:
            self._journal_order(rec, now)
        self.db.audit("flatten", {"reason": reason, "orders": len(records)}, actor="engine")
        self._clear_error("flatten")
        return records

    # ------------------------------------------------------------------ exits
    def _closing_now(self, key: str, now: datetime) -> bool:
        since = self._closing.get(key)
        return since is not None and now - since < CLOSE_RETRY

    async def _engine_close(self, inst: Instrument, reason: str, now: datetime) -> None:
        """Close a position for a protective reason (never waits for approval)."""
        self._closing[inst.key] = now
        self._close_reasons[inst.key] = reason
        log.warning("closing %s: %s", inst.key, reason)
        rec = await self.broker.close_position(inst, reason=reason)
        if rec is not None:
            self._journal_order(rec, now)
        self.db.audit("engine_close", {"instrument": inst.key, "reason": reason}, actor="engine")
        await self._notify(f"Closing {inst.symbol}", reason)

    @staticmethod
    def _native_stops(open_orders: list[OrderRecord]) -> dict[str, float]:
        """Highest working broker-side protective stop per instrument."""
        stops: dict[str, float] = {}
        for order in open_orders:
            req = order.request
            if (
                order.status in OPEN_ORDER_STATUSES
                and req.side == Side.SELL
                and req.stop_loss is not None
            ):
                key = req.instrument.key
                stops[key] = max(stops.get(key, 0.0), req.stop_loss)
        return stops

    @staticmethod
    def _working_buys(open_orders: list[OrderRecord]) -> set[str]:
        return {
            o.request.instrument.key
            for o in open_orders
            if o.status in OPEN_ORDER_STATUSES and o.request.side == Side.BUY
        }

    def _extra(self, key: str) -> dict[str, float]:
        """Engine-side position data the positions_meta table has no columns for
        (initial stop, planned risk per unit, best price since entry)."""
        raw = self.db.get_kv(EXTRA_PREFIX + key)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _set_extra(self, key: str, **values: float) -> None:
        data = self._extra(key)
        data.update(values)
        self.db.set_kv(EXTRA_PREFIX + key, json.dumps(data))

    def _unit_risk(self, key: str, entry: float, meta: dict[str, Any] | None) -> float | None:
        """Initial risk per unit (1R) for a position entered at `entry`."""
        extra = self._extra(key)
        initial = extra.get("initial_stop")
        if initial is not None and entry > initial:
            return entry - initial
        planned = extra.get("unit_risk")
        if planned is not None and planned > 0:
            return planned
        stop = meta.get("stop_loss") if meta else None
        if stop is not None and entry > stop:
            return entry - stop
        return None

    async def _manage_positions(
        self,
        now: datetime,
        positions: list[Position],
        quotes: dict[str, Quote],
        metas: dict[str, dict[str, Any]],
    ) -> None:
        """Engine-side stops/targets, time stop, breakeven and trailing for positions the
        engine opened (those with positions_meta). Stops only ever move up."""
        native_stops = self._native_stops(self._open_orders)
        stocks_open = stock_market_open(now)
        failures: list[str] = []
        for pos in positions:
            key = pos.instrument.key
            meta = metas.get(key)
            quote = quotes.get(key)
            mark = quote.exit_price if quote is not None else None
            if pos.qty <= 0 or meta is None or mark is None or self._closing_now(key, now):
                continue
            if pos.instrument.asset_class == AssetClass.STOCK and not stocks_open:
                continue  # stock orders cannot execute now; broker stops still protect
            try:
                await self._manage_one(pos, meta, mark, native_stops.get(key), now)
            except BrokerError as exc:
                failures.append(f"{key}: {exc}")
        if failures:
            raise BrokerError("; ".join(failures))

    async def _manage_one(
        self,
        pos: Position,
        meta: dict[str, Any],
        mark: float,
        broker_stop: float | None,
        now: datetime,
    ) -> None:
        """Exit rules for one engine position valued at `mark` (the bid). `broker_stop` is
        the highest protective stop working at the broker, if any."""
        exits = self.settings.exits
        key = pos.instrument.key
        stop, target = meta.get("stop_loss"), meta.get("take_profit")
        native = self.broker.supports_native_stops(pos.instrument)
        reason: str | None = None
        if stop is not None:
            if not native:
                if mark <= stop:
                    reason = f"stop-loss {stop:g} hit (engine-managed, bid {mark:g})"
            elif broker_stop is None:
                if mark <= stop * (1 - BACKUP_STOP_BREACH):
                    reason = (
                        f"backup stop: bid {mark:g} is >0.5% below the stop {stop:g} "
                        "and no broker stop is working"
                    )
            elif stop > broker_stop + EPS and mark <= stop:
                reason = f"stop-loss {stop:g} hit (engine-managed, bid {mark:g})"
        if reason is None and not native and target is not None and mark >= target:
            reason = f"take-profit {target:g} hit (engine-managed, bid {mark:g})"

        unit = self._unit_risk(key, pos.avg_cost, meta)
        r_now = (mark - pos.avg_cost) / unit if unit else None
        opened = meta.get("opened_at")
        if (
            reason is None
            and exits.time_stop_minutes > 0
            and opened is not None
            and r_now is not None
            and now - _aware(opened) >= timedelta(minutes=exits.time_stop_minutes)
            and r_now < exits.time_stop_min_r
        ):
            reason = (
                f"time stop: {exits.time_stop_minutes} min with {r_now:.2f}R "
                f"(< {exits.time_stop_min_r:g}R)"
            )
        if reason is not None:
            await self._engine_close(pos.instrument, reason, now)
            return

        new_stop: float | None = None
        why = ""
        if (
            exits.breakeven_at_r > 0
            and r_now is not None
            and r_now >= exits.breakeven_at_r
            and (stop is None or stop < pos.avg_cost)
        ):
            new_stop, why = pos.avg_cost, f"breakeven at {r_now:.2f}R"
        extra = self._extra(key)
        best = max(extra.get("best_price", mark), mark)
        if best != extra.get("best_price"):
            self._set_extra(key, best_price=best)
        current = new_stop if new_stop is not None else stop
        atr = self._market.get(key, {}).get("atr14")
        trailing = exits.trail_atr_mult > 0 and (
            exits.breakeven_at_r == 0 or (current is not None and current >= pos.avg_cost)
        )
        if trailing and atr and atr > 0:
            trail = best - exits.trail_atr_mult * atr
            if current is None or trail > current + EPS:
                new_stop, why = trail, f"trailing {exits.trail_atr_mult:g} ATR below {best:g}"
        if new_stop is not None and (stop is None or new_stop > stop + EPS):
            await self._raise_stop(pos, meta, round(new_stop, 6), why)

    async def _raise_stop(
        self, pos: Position, meta: dict[str, Any], new_stop: float, why: str
    ) -> None:
        inst = pos.instrument
        moved = False
        if self.broker.supports_native_stops(inst):
            try:
                moved = await self.broker.modify_stop(inst, new_stop)
            except BrokerError as exc:
                log.warning("modify_stop for %s failed: %s", inst.key, exc)
        self.db.set_position_meta(
            inst.key,
            stop_loss=new_stop,
            take_profit=meta.get("take_profit"),
            entry_price=meta.get("entry_price"),
            initial_risk=meta.get("initial_risk"),
            opened_at=meta.get("opened_at"),
            decision_id=meta.get("decision_id"),
            setup=meta.get("setup"),
        )
        meta["stop_loss"] = new_stop
        log.info(
            "%s stop raised to %g (%s; %s)",
            inst.key,
            new_stop,
            why,
            "broker stop moved" if moved else "engine-managed",
        )

    async def _flatten_before_close(
        self, now: datetime, positions: list[Position], metas: dict[str, dict[str, Any]]
    ) -> None:
        """Day-trade rule: close the engine's stock positions 10 minutes before the close.
        Positions the engine did not open (no positions_meta) are left alone."""
        if not self.settings.flatten_stocks_before_close:
            return
        left = minutes_to_close(now)
        if left is None or left > FLATTEN_BEFORE_CLOSE_MIN:
            return
        failures: list[str] = []
        for pos in positions:
            key = pos.instrument.key
            if (
                pos.instrument.asset_class == AssetClass.STOCK
                and pos.qty > 0
                and key in metas
                and not self._closing_now(key, now)
            ):
                try:
                    await self._engine_close(pos.instrument, "flatten before close", now)
                except BrokerError as exc:
                    failures.append(f"{key}: {exc}")
        if failures:
            raise BrokerError("; ".join(failures))

    async def _reprotect(
        self,
        now: datetime,
        positions: list[Position],
        open_orders: list[OrderRecord],
        metas: dict[str, dict[str, Any]],
    ) -> None:
        """After a (re)connect: re-place a broker stop for engine stock positions that have
        none working. Only brokers that rest a stop for a SELL request carrying stop_loss
        (`supports_standalone_stops`) get one; otherwise, or when the broker rejects it, the
        owner is notified and the engine's backup stop guards the position."""
        if not self._reprotect_pending:
            return
        self._reprotect_pending = False
        protected = self._native_stops(open_orders)
        buying = self._working_buys(open_orders)
        for pos in positions:
            key = pos.instrument.key
            meta = metas.get(key)
            stop = meta.get("stop_loss") if meta else None
            if (
                pos.qty <= 0
                or stop is None
                or pos.instrument.asset_class != AssetClass.STOCK
                or not self.broker.supports_native_stops(pos.instrument)
                or key in protected
                or key in buying
                or self._closing_now(key, now)
            ):
                continue
            if not _rests_standalone_stops(self.broker, pos.instrument):
                await self._notify(
                    f"Unprotected {pos.instrument.symbol}",
                    f"No broker stop is working (stop {stop:g}) and this broker cannot re-place "
                    "one automatically. The engine's backup stop watches it while the app runs.",
                    priority="high",
                    tags=["warning"],
                )
                continue
            req = OrderRequest(
                instrument=pos.instrument,
                side=Side.SELL,
                qty=pos.qty,
                order_type=OrderType.MARKET,
                stop_loss=stop,
                is_exit=True,
                reason="protective stop (re-placed)",
                decision_id=meta.get("decision_id") if meta else None,
                setup=meta.get("setup") if meta else None,
            )
            try:
                rec = await self.broker.place_order(req)
            except BrokerError as exc:
                log.warning("re-placing the stop for %s failed: %s", key, exc)
                self._reprotect_pending = True
                continue
            self._journal_order(rec, now)
            if rec.status in _ACCEPTED:
                log.warning("re-placed missing protective stop for %s at %g", key, stop)
                self.db.audit("reprotect", {"instrument": key, "stop": stop}, actor="engine")
            else:
                await self._notify(
                    f"Unprotected {pos.instrument.symbol}",
                    f"Broker rejected the re-placed stop at {stop:g}: {rec.message}. "
                    "The engine's backup stop still watches it.",
                    priority="high",
                    tags=["warning"],
                )

    # ------------------------------------------------------------------ journal
    def _journal_order(self, rec: OrderRecord, now: datetime) -> int:
        return self.db.insert_order(
            rec.model_copy(update={"id": None, "created_at": now, "updated_at": now})
        )

    async def _sync_journal(
        self, now: datetime, positions: list[Position], open_orders: list[OrderRecord]
    ) -> None:
        await self._sync_orders(now)
        if not self.shadow:
            await self._cancel_stale_entries(now)
        await self._sync_fills(now)
        await self._cleanup_meta(now, positions, open_orders)

    async def _sync_orders(self, now: datetime) -> None:
        for rec in self.db.list_orders(limit=200, statuses=OPEN_ORDER_STATUSES):
            if rec.id is None or not rec.broker_order_id:
                continue
            latest = await self.broker.order_status(rec.broker_order_id)
            if latest is None:
                if now - _aware(rec.created_at) > ORDER_FORGET_AFTER:
                    self.db.update_order(
                        rec.id,
                        status=OrderStatus.CANCELLED,
                        message="no longer known to the broker",
                    )
                continue
            updates: dict[str, Any] = {}
            for name in ("status", "filled_qty", "avg_fill_price", "commission"):
                if getattr(latest, name) != getattr(rec, name):
                    updates[name] = getattr(latest, name)
            if (
                latest.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
                and latest.message
                and latest.message != rec.message
            ):
                updates["message"] = _short(f"{rec.message}; {latest.message}".lstrip("; "))
            if updates:
                self.db.update_order(rec.id, **updates)

    async def _cancel_stale_entries(self, now: datetime) -> None:
        """Cancel engine entry orders that did not fill at all within ENTRY_ORDER_TTL: the
        setup that justified them is stale."""
        for rec in self.db.list_orders(limit=100, statuses=OPEN_ORDER_STATUSES):
            req = rec.request
            if (
                rec.id is None
                or not rec.broker_order_id
                or req.side != Side.BUY
                or req.is_exit
                or rec.filled_qty > 0
                or now - _aware(rec.created_at) < ENTRY_ORDER_TTL
            ):
                continue
            latest = await self.broker.order_status(rec.broker_order_id)
            if latest is None or latest.status not in OPEN_ORDER_STATUSES or latest.filled_qty > 0:
                continue
            await self.broker.cancel_order(rec.broker_order_id)
            final = await self.broker.order_status(rec.broker_order_id) or latest
            minutes = int(ENTRY_ORDER_TTL.total_seconds() // 60)
            self.db.update_order(
                rec.id,
                status=final.status,
                filled_qty=final.filled_qty,
                message=_short(f"{rec.message}; not filled within {minutes} min, cancel sent"),
            )
            log.info("cancelled stale entry order %s for %s", rec.id, req.instrument.key)

    async def _sync_fills(self, now: datetime) -> None:
        """Journal new fills (deduped by exec_id), apply late commission reports, and
        rebuild round trips for every instrument that changed."""
        if self._fill_commissions is None:
            self._fill_commissions = {
                f.exec_id: (f.commission, f.ts)
                for f in self.db.list_fills(since=now - FIRST_FILL_LOOKBACK, limit=FILL_QUERY_LIMIT)
            }
        lookback = FILL_LOOKBACK if self._fills_synced else FIRST_FILL_LOOKBACK
        fills = await self.broker.fills_since(now - lookback)
        self._fills_synced = True
        touched = set(self._pending_trades)
        for fill in sorted(fills, key=lambda f: (f.ts, f.exec_id)):
            key = fill.instrument.key
            if self.db.insert_fill(fill):
                self._fill_commissions[fill.exec_id] = (fill.commission, fill.ts)
                self._add_fee(fill.ts, fill.commission)
                touched.add(key)
                continue
            known = self._fill_commissions.get(fill.exec_id)
            if fill.commission > 0 and (known is None or abs(known[0] - fill.commission) > EPS):
                self.db.update_fill_commission(fill.exec_id, fill.commission)
                if known is not None:
                    self._add_fee(fill.ts, fill.commission - known[0])
                self._fill_commissions[fill.exec_id] = (fill.commission, fill.ts)
                touched.add(key)
        cutoff = now - FIRST_FILL_LOOKBACK
        self._fill_commissions = {
            k: v for k, v in self._fill_commissions.items() if _aware(v[1]) >= cutoff
        }
        for key in sorted(touched):
            await self._reconstruct(key, now)

    def _last_exit_for(self, key: str) -> datetime | None:
        if self._last_exit is None:
            self._last_exit = {}
            for tr in self.db.list_trades(limit=1000):
                k = tr.instrument.key
                ts = _aware(tr.exit_ts)
                if k not in self._last_exit or ts > self._last_exit[k]:
                    self._last_exit[k] = ts
        return self._last_exit.get(key)

    async def _reconstruct(self, key: str, now: datetime) -> None:
        """Journal every completed round trip for `key` since its last journaled exit."""
        last_exit = self._last_exit_for(key)
        since = last_exit or now - TRADE_LOOKBACK
        fills = [
            f
            for f in self.db.list_fills(since=since, limit=FILL_QUERY_LIMIT)
            if f.instrument.key == key and (last_exit is None or _aware(f.ts) > last_exit)
        ]
        fills.sort(key=lambda f: (f.ts, f.exec_id))
        meta = self.db.get_position_meta(key)
        fallback = None
        if meta and meta.get("entry_price"):
            fallback = (float(meta["entry_price"]), meta.get("opened_at") or now)
        trips, open_qty = replay_fills(fills, fallback)
        self._open_qty[key] = open_qty
        self._pending_trades.discard(key)
        for trip in trips:
            if any(f.commission <= 0 and now - _aware(f.ts) < COMMISSION_GRACE for f in trip.fills):
                self._pending_trades.add(key)  # wait briefly for the commission report
                return
            await self._journal_trade(key, trip, meta)

    async def _journal_trade(
        self, key: str, trip: _Trip, meta: dict[str, Any] | None
    ) -> TradeRecord:
        assert trip.entry_ts is not None and trip.exit_ts is not None
        qty = trip.qty
        entry = trip.cost / qty
        pnl = trip.proceeds - trip.cost - trip.commission
        unit = self._unit_risk(key, entry, meta)
        r_multiple = round(pnl / (unit * qty), 3) if unit else None
        record = TradeRecord(
            instrument=Instrument.from_key(key),
            qty=round(qty, 8),
            entry_price=round(entry, 6),
            exit_price=round(trip.proceeds / qty, 6),
            entry_ts=trip.entry_ts,
            exit_ts=trip.exit_ts,
            pnl=round(pnl, 4),
            commission=round(trip.commission, 4),
            setup=meta.get("setup") if meta else None,
            decision_id=meta.get("decision_id") if meta else None,
            r_multiple=r_multiple,
            exit_reason=self._exit_reason(key, trip, meta),
        )
        record.id = self.db.insert_trade(record)
        assert self._last_exit is not None
        self._last_exit[key] = _aware(trip.exit_ts)

        stats = self._stats_for(trading_day(record.exit_ts))
        stats.realized_pnl = round(stats.realized_pnl + record.pnl, 4)
        if record.pnl > 0:
            stats.wins += 1
        elif record.pnl < 0:
            stats.losses += 1
        self.db.upsert_day(stats)
        if self._day is not None:  # the streak is a running counter used for sizing now
            if record.pnl > 0:
                self._day.loss_streak = 0
            elif record.pnl < 0:
                self._day.loss_streak += 1
            self._save_day()
        r_text = f", {r_multiple:+.2f}R" if r_multiple is not None else ""
        log.info(
            "trade closed: %s %g @ %.4f -> %.4f, P&L %+.2f%s (%s)",
            key,
            record.qty,
            record.entry_price,
            record.exit_price,
            record.pnl,
            r_text,
            record.exit_reason,
        )
        await self._notify(
            f"Trade closed: {record.instrument.symbol}",
            f"P&L {record.pnl:+.2f} USD{r_text} ({record.exit_reason})",
        )
        return record

    def _exit_reason(self, key: str, trip: _Trip, meta: dict[str, Any] | None) -> str:
        sell = trip.last_sell
        reason = self._close_reasons.pop(key, None)
        if sell is not None:
            rec = self.db.get_order_by_broker_id(sell.broker_order_id)
            if rec is not None and rec.request.side == Side.SELL and rec.request.reason:
                return rec.request.reason
        if reason:
            return reason
        if sell is not None and meta:
            target, stop = meta.get("take_profit"), meta.get("stop_loss")
            if target and sell.price >= target * 0.998:
                return "take-profit"
            if stop and sell.price <= stop * 1.002:
                return "stop-loss"
        return "closed at broker"

    async def _cleanup_meta(
        self, now: datetime, positions: list[Position], open_orders: list[OrderRecord]
    ) -> None:
        """Forget engine metadata once a position is flat at the broker AND in the fills
        journal, and cancel exit orders it left behind (e.g. a re-placed stop)."""
        held = {p.instrument.key for p in positions if p.qty > 0}
        buying = self._working_buys(open_orders)
        for key, meta in self.db.all_position_meta().items():
            if key in held or key in buying or key in self._pending_trades:
                continue
            opened = meta.get("opened_at")
            last_exit = self._last_exit_for(key)
            closed = last_exit is not None and opened is not None and last_exit >= _aware(opened)
            # Without a journaled exit this may be an entry whose fills are still in flight.
            if not closed and opened is not None and now - _aware(opened) < STALE_META_GRACE:
                continue
            if key not in self._open_qty:
                await self._reconstruct(key, now)
                if key in self._pending_trades:
                    continue
            if self._open_qty.get(key, 0.0) > EPS:
                if key not in self._mismatch_logged:
                    self._mismatch_logged.add(key)
                    log.warning("%s: broker shows no position but the fills journal does", key)
                continue
            self._mismatch_logged.discard(key)
            for order in open_orders:
                req = order.request
                if (
                    not self.shadow
                    and req.instrument.key == key
                    and req.side == Side.SELL
                    and order.status in OPEN_ORDER_STATUSES
                    and order.broker_order_id
                ):
                    await self.broker.cancel_order(order.broker_order_id)
                    log.info("cancelled leftover exit order %s for %s", order.broker_order_id, key)
            self.db.delete_position_meta(key)
            self.db.set_kv(EXTRA_PREFIX + key, "{}")
            self._closing.pop(key, None)
            self._open_qty.pop(key, None)

    # ------------------------------------------------------------------ signal ledger
    def _update_signal(self, signal_id: int | None, **fields: Any) -> None:
        if signal_id is None:
            return
        sig = self.db.get_signal(signal_id)
        if sig is not None:
            self.db.update_signal(sig.model_copy(update=fields))

    def _revert_signal(self, order_id: int, reason: str) -> None:
        """An order that never reached the broker: its signal counts as skipped."""
        for sig in self.db.list_signals(limit=500):
            if sig.order_id == order_id:
                self.db.update_signal(
                    sig.model_copy(update={"taken": False, "skip_reason": _short(reason, 160)})
                )
                return

    async def _evaluate_signals(self, now: datetime) -> None:
        """Score matured signals (stocks after their session close, crypto after 24h).
        Bounded to a few bar requests per pass; data errors are retried later."""
        if self._last_ledger_at is not None and now - self._last_ledger_at < LEDGER_EVERY:
            return
        self._last_ledger_at = now
        groups: dict[str, list[tuple[SignalRecord, datetime]]] = {}
        for sig in self.db.unevaluated_signals(before=now, limit=500):
            ts = _aware(sig.ts)
            if sig.instrument.asset_class == AssetClass.STOCK:
                end = market_close_time(trading_day(ts))
            else:
                end = ts + CRYPTO_HORIZON
            if now < end + LEDGER_SETTLE:
                continue
            key = sig.instrument.key
            if now < self._ledger_retry.get(key, now):
                continue
            if key in groups or len(groups) < LEDGER_MAX_INSTRUMENTS:
                groups.setdefault(key, []).append((sig, end))
        for key, items in groups.items():
            oldest = min(_aware(s.ts) for s, _ in items)
            days = min((now - oldest).days + 2, LEDGER_GIVE_UP.days + 1)
            try:
                bars = await self.broker.bars(Instrument.from_key(key), "1 min", f"{days} D")
            except Exception as exc:
                log.warning("ledger bars for %s failed: %s", key, exc)
                bars = []
            missing = False
            for sig, end in items:
                scored = score_signal(sig, bars, end)
                if scored is not None:
                    self.db.update_signal(scored)
                elif now - _aware(sig.ts) > LEDGER_GIVE_UP:
                    self.db.update_signal(sig.model_copy(update={"evaluated": True}))
                else:
                    missing = True
            if missing:  # let other instruments' signals through meanwhile
                self._ledger_retry[key] = now + LEDGER_RETRY

    # ------------------------------------------------------------------ decision cycle
    async def run_cycle(self, manual: bool = False) -> DecisionBatch | None:
        """One decision cycle. Returns the journaled batch, or None when skipped (halted,
        disconnected, or nothing tradable now). Raises BrokerError when the broker is down."""
        async with self._cycle_lock:
            await self.monitor_tick()  # fresh account, positions, fills and halts
            now = self._now()
            self._last_cycle_at = now
            day = self._today(now)
            if day.halted:
                log.info("cycle skipped: trading halted (%s)", day.halt_reason)
                return None
            if not self.broker.connected or self._account is None:
                return None
            universe = await self._universe(now)
            if not universe:
                log.info("cycle skipped: nothing tradable now")
                return None
            return await self._cycle(now, day, universe, manual)

    async def _universe(self, now: datetime) -> list[Instrument]:
        settings = self.settings
        stocks_open = stock_market_open(now)
        symbols: list[Instrument] = []
        if settings.trade_stocks and stocks_open:
            names = list(settings.stock_universe)
            if settings.dynamic_universe:
                try:
                    names += await self.broker.scan_movers(10)
                except Exception as exc:
                    log.warning("scan_movers failed: %s", exc)
            symbols += [Instrument(symbol=s, asset_class=AssetClass.STOCK) for s in names]
        if settings.trade_crypto:
            symbols += [
                Instrument(symbol=s, asset_class=AssetClass.CRYPTO)
                for s in settings.crypto_universe
            ]
        out: dict[str, Instrument] = {}
        for inst in symbols:
            symbol = inst.symbol.strip().upper()
            if not symbol:
                continue
            inst = Instrument(symbol=symbol, asset_class=inst.asset_class)
            if (
                inst.asset_class == AssetClass.STOCK
                and symbol in LEVERAGED_ETFS
                and not self.risk.limits.allow_leveraged_etfs
            ):
                self._log_skip_once(symbol, "leveraged/inverse ETF (not allowed in settings)")
                continue
            out.setdefault(inst.key, inst)
        for pos in self._positions:  # always look at held positions so they can be managed
            stock = pos.instrument.asset_class == AssetClass.STOCK
            if pos.qty > 0 and (stocks_open or not stock):
                out.setdefault(pos.instrument.key, pos.instrument)
        return list(out.values())

    def _log_skip_once(self, symbol: str, reason: str) -> None:
        if (symbol, reason) not in self._skip_logged:
            self._skip_logged.add((symbol, reason))
            log.info("universe: skipping %s today: %s", symbol, reason)

    def _affordable(self, inst: Instrument, quote: Quote, account: AccountSnapshot) -> bool:
        """Whether one tradable unit fits the position cap and the cash. Crypto (and
        fractional stocks) trade in small fractions, so only whole shares can fail."""
        if inst.asset_class != AssetClass.STOCK or self.settings.allow_fractional_stocks:
            return True
        price = quote.ask or quote.price
        if price is None:
            return False
        limit = min(max_position_value(self.risk.limits, account.equity), available_cash(account))
        if price > limit:
            self._log_skip_once(
                inst.symbol, f"1 share costs ${price:,.2f}, more than the ${limit:,.2f} allowed"
            )
            return False
        return True

    async def _fetch(
        self, universe: list[Instrument], now: datetime
    ) -> list[tuple[Instrument, Quote, list[Bar]]]:
        """Quote, affordability prefilter, then bars, per instrument. Errors skip a symbol."""
        assert self._account is not None
        account = self._account
        held = {p.instrument.key for p in self._positions if p.qty > 0}
        lookback = BAR_LOOKBACK.get(self.settings.bar_size, "5 D")
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def one(inst: Instrument) -> tuple[Instrument, Quote, list[Bar]] | None:
            async with sem:
                try:
                    quote = await self.broker.quote(inst)
                    self._track_halt(inst.key, quote, now)
                    if inst.key not in held and not self._affordable(inst, quote, account):
                        return None
                    bars = await self.broker.bars(inst, self.settings.bar_size, lookback)
                except Exception as exc:
                    log.warning("market data for %s failed: %s", inst.key, exc)
                    return None
                return inst, quote, bars

        results = await asyncio.gather(*(one(i) for i in universe))
        return [r for r in results if r is not None]

    def _not_actionable(self, cand: Candidate, quote: Quote | None, now: datetime) -> str | None:
        """Why a candidate cannot be traded right now (kept out of the decision maker)."""
        key = cand.instrument.key
        if quote is not None and quote.halted:
            return "halted"
        last_halt = self._last_halt_at.get(key)
        cooldown = timedelta(minutes=self.risk.limits.halt_cooldown_minutes)
        if last_halt is not None and now - last_halt < cooldown:
            return "halt cooldown"
        if cand.instrument.asset_class == AssetClass.STOCK and not in_entry_window(
            now, self.settings.stock_entry_start, self.settings.stock_entry_cutoff
        ):
            return "outside the stock entry window"
        return None

    def _pick_maker(self, ctx: DecisionContext) -> tuple[DecisionMaker, str | None]:
        """The configured maker, or the rule-based one (with the reason) when the LLM is
        disabled, over budget, or has nothing worth deciding."""
        if self.decider is self.rules:
            return self.rules, None
        if not self.settings.llm_enabled:
            return self.rules, "LLM disabled"
        budget = self.risk.limits.llm_daily_budget_usd
        if ctx.day.llm_cost >= budget:
            if self._fallback_logged != ctx.day.day:
                self._fallback_logged = ctx.day.day
                log.warning(
                    "LLM budget reached ($%.2f of $%.2f): rules-only decisions for today",
                    ctx.day.llm_cost,
                    budget,
                )
            return self.rules, f"LLM budget reached (${ctx.day.llm_cost:.2f} of ${budget:.2f})"
        threshold = self.settings.llm_min_candidate_score
        worth = any(c.score >= threshold for c in ctx.candidates) or any(
            p.qty > 0 for p in ctx.positions
        )
        if not worth:
            return self.rules, "no candidate worth an LLM call"
        return self.decider, None

    async def _cycle(
        self, now: datetime, day: DayStats, universe: list[Instrument], manual: bool
    ) -> DecisionBatch:
        assert self._account is not None
        data = await self._fetch(universe, now)
        quotes = {inst.key: quote for inst, quote, _ in data}
        market: dict[str, dict[str, float]] = {}
        candidates: list[Candidate] = []
        for inst, quote, bars in data:
            try:
                market[inst.key] = self.scanner.snapshot(inst, bars, quote)
                candidates.extend(self.scanner.scan(inst, bars, quote, now))
            except Exception:
                log.exception("scanner failed for %s", inst.key)
        candidates.sort(key=lambda c: c.score, reverse=True)
        self._market = market

        # Signal ledger: one row per instrument+setup per SIGNAL_DEDUPE window.
        recent = {
            (s.instrument.key, s.setup): s
            for s in reversed(self.db.list_signals(limit=500, since=now - SIGNAL_DEDUPE))
        }
        logged: list[_Logged] = []
        for cand in candidates:
            blocked = self._not_actionable(cand, quotes.get(cand.instrument.key), now)
            existing = recent.get((cand.instrument.key, cand.setup))
            if existing is not None and existing.id is not None:
                logged.append(_Logged(cand, existing.id, False, blocked))
                continue
            sig = SignalRecord(
                ts=now,
                instrument=cand.instrument,
                setup=cand.setup,
                score=cand.score,
                price=cand.price,
                stop=cand.suggested_stop,
                target=cand.suggested_target,
            )
            logged.append(_Logged(cand, self.db.insert_signal(sig), True, blocked))
        actionable = [item.cand for item in logged if item.blocked is None]
        positions = [p for p in self._positions if p.qty > 0]
        ctx = DecisionContext(
            now=now,
            mode=self.broker.mode,
            account=self._account,
            positions=positions,
            candidates=actionable,
            day=day.model_copy(),
            limits=self.risk.effective_limits(),
            recent_trades=self.db.list_trades(limit=10),
            market=market,
            stock_market_open=stock_market_open(now),
            data_delayed=self.broker.data_delayed,
        )
        maker, why = self._pick_maker(ctx)
        try:
            batch = await maker.decide(ctx)
        except Exception as exc:
            log.exception("decision maker %s failed", maker.name)
            batch = DecisionBatch(maker=maker.name, error=f"{type(exc).__name__}: {exc}")
        batch.ts = now
        batch.candidates = actionable
        if why:
            batch.market_view = f"[{why}] {batch.market_view}".strip()
        if manual:
            batch.market_view = f"[manual] {batch.market_view}".strip()
        batch.id = self.db.insert_decision(batch)
        day.llm_cost = round(day.llm_cost + batch.cost_usd, 6)
        self._save_day()
        # Orders must not be interrupted halfway (e.g. by stop()): shield the execution.
        outcomes, holds = await asyncio.shield(self._execute(batch, ctx, quotes, now))

        label = "AI" if maker is not self.rules else "rules"
        prefix = "LLM budget; " if why and why.startswith("LLM budget") else ""
        threshold = self.settings.llm_min_candidate_score
        bought = any(o.taken for o in outcomes.values())
        for item in logged:
            cand = item.cand
            key = cand.instrument.key
            outcome = outcomes.get(key)
            if item.blocked is not None:
                skip: str | None = item.blocked
            elif outcome is not None and outcome.setup == cand.setup:
                if outcome.taken:
                    self._update_signal(
                        item.signal_id,
                        taken=True,
                        order_id=outcome.order_id,
                        decision_id=batch.id,
                        skip_reason=None,
                    )
                    continue
                skip = outcome.skip_reason
            elif outcome is not None:
                skip = f"same symbol traded via {outcome.setup}"
            elif key in holds:
                skip = f"{label}: HOLD"
            elif cand.score < threshold:
                skip = f"score {cand.score:.2f} < {threshold:.2f}"
            elif batch.refused or batch.error:
                skip = f"{label}: {'refused' if batch.refused else 'error'}"
            elif bought:
                skip = "not top candidate"
            else:
                skip = f"{label}: no entry"
            if item.new:
                self._update_signal(
                    item.signal_id,
                    decision_id=batch.id,
                    skip_reason=_short(prefix + (skip or ""), 160),
                )
        return batch

    async def _execute(
        self, batch: DecisionBatch, ctx: DecisionContext, quotes: dict[str, Quote], now: datetime
    ) -> tuple[dict[str, _BuyOutcome], set[str]]:
        outcomes: dict[str, _BuyOutcome] = {}
        holds: set[str] = set()
        seen: set[str] = set()
        open_orders = list(self._open_orders)
        for decision in batch.decisions:
            symbol = decision.symbol.strip().upper()
            inst = Instrument(symbol=symbol, asset_class=decision.asset_class)
            if inst.key in seen:
                log.info("ignoring a second decision for %s in one batch", inst.key)
                continue
            seen.add(inst.key)
            try:
                if decision.action == TradeAction.BUY:
                    outcomes[inst.key] = await self._handle_buy(
                        decision, inst, batch, ctx, quotes, open_orders, now
                    )
                elif decision.action == TradeAction.CLOSE:
                    await self._handle_close(decision, inst, batch, ctx, now)
                else:
                    holds.add(inst.key)
            except BrokerError as exc:
                log.warning("decision for %s failed: %s", inst.key, exc)
                self._set_error("decision", f"{inst.symbol}: {exc}")
                if decision.action == TradeAction.BUY:
                    outcomes[inst.key] = _BuyOutcome(None, False, skip_reason=f"error: {exc}")
        return outcomes, holds

    async def _handle_buy(
        self,
        decision: AIDecision,
        inst: Instrument,
        batch: DecisionBatch,
        ctx: DecisionContext,
        quotes: dict[str, Quote],
        open_orders: list[OrderRecord],
        now: datetime,
    ) -> _BuyOutcome:
        key = inst.key
        mine = [c for c in ctx.candidates if c.instrument.key == key]
        cand = next((c for c in mine if c.setup == decision.setup), None) or next(
            iter(sorted(mine, key=lambda c: c.score, reverse=True)), None
        )
        if cand is None:
            log.warning("BUY %s ignored: not a scanner candidate this cycle", key)
            return _BuyOutcome(decision.setup, False, skip_reason="not a scanner candidate")
        if key in self._pending_trades:
            return _BuyOutcome(cand.setup, False, skip_reason="previous trade still settling")
        if key in self._awaiting_approval():
            return _BuyOutcome(cand.setup, False, skip_reason="already waiting for approval")
        quote = quotes.get(key) or await self.broker.quote(inst)
        entry = decision.limit_price or quote.ask or quote.last
        stop = decision.stop_loss or cand.suggested_stop
        target = decision.take_profit or cand.suggested_target
        if not entry or not math.isfinite(entry) or entry <= 0:
            return _BuyOutcome(cand.setup, False, skip_reason="no entry price")
        qty, notes = self.risk.size_entry(
            decision=decision,
            entry=entry,
            stop=stop,
            account=ctx.account,
            positions=ctx.positions,
            day=self._today(now),
            broker=self.broker,
            allow_fractional=self.settings.allow_fractional_stocks,
        )
        if qty <= 0:
            log.info("BUY %s skipped: %s", key, notes[-1])
            return _BuyOutcome(cand.setup, False, skip_reason=f"risk: {notes[-1]}")
        req = OrderRequest(
            instrument=inst,
            side=Side.BUY,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=entry,
            stop_loss=stop,
            take_profit=target,
            reason=_short(decision.rationale),
            decision_id=batch.id,
            setup=decision.setup or cand.setup,
        )
        async with self._entry_lock:
            verdict = self.risk.check(
                req,
                quote=quote,
                account=ctx.account,
                positions=ctx.positions,
                open_orders=open_orders,
                day=self._today(now),
                mode=self.broker.mode,
                data_delayed=self.broker.data_delayed,
                broker=self.broker,
                now=now,
                settings=self.settings,
                last_halt_at=self._last_halt_at.get(key),
            )
            if not verdict.approved:
                message = "; ".join(verdict.reasons)
                order_id = self._journal_order(
                    OrderRecord(request=req, status=OrderStatus.BLOCKED, message=message), now
                )
                log.info("BUY %s blocked: %s", key, message)
                return _BuyOutcome(
                    cand.setup, False, order_id, skip_reason=f"risk: {verdict.reasons[0]}"
                )
            order = verdict.order or req
            sizing = "; ".join([*notes, *verdict.reasons])
            if self.shadow:
                order_id = self._journal_order(
                    OrderRecord(
                        request=order,
                        status=OrderStatus.BLOCKED,
                        message=f"shadow mode: live trading is not armed ({sizing})",
                    ),
                    now,
                )
                return _BuyOutcome(cand.setup, False, order_id, skip_reason="shadow mode")
            if not self.settings.autopilot:
                order_id = self._journal_order(
                    OrderRecord(
                        request=order,
                        status=OrderStatus.PENDING_APPROVAL,
                        message=f"waiting for approval, expires in 10 min ({sizing})",
                    ),
                    now,
                )
                await self._notify(
                    f"Approve BUY {inst.symbol}?",
                    f"{order.qty:g} @ {entry:g}, stop {stop:g}, target {target:g}. "
                    "Expires in 10 minutes.",
                    tags=["question"],
                )
                return _BuyOutcome(cand.setup, True, order_id)
            placed = await self._send_entry(order, now)
            order_id = self._journal_order(
                placed.model_copy(update={"message": _short(f"{sizing}; {placed.message}")}), now
            )
            if placed.status not in _ACCEPTED:
                return _BuyOutcome(
                    cand.setup, False, order_id, skip_reason=f"broker rejected: {placed.message}"
                )
            open_orders.append(
                placed.model_copy(update={"status": OrderStatus.SUBMITTED, "filled_qty": 0.0})
            )
            return _BuyOutcome(cand.setup, True, order_id)

    async def _send_entry(self, order: OrderRequest, now: datetime) -> OrderRecord:
        """Place an entry. Position metadata is written first, so a position that appears
        is always managed even if the call fails halfway (stale metadata is cleaned up)."""
        assert order.limit_price is not None and order.stop_loss is not None
        key = order.instrument.key
        entry, stop = order.limit_price, order.stop_loss
        self.db.set_position_meta(
            key,
            stop_loss=stop,
            take_profit=order.take_profit,
            entry_price=entry,
            initial_risk=(entry - stop) * order.qty,
            opened_at=now,
            decision_id=order.decision_id,
            setup=order.setup,
        )
        self.db.set_kv(
            EXTRA_PREFIX + key,
            json.dumps({"initial_stop": stop, "unit_risk": entry - stop, "best_price": entry}),
        )
        try:
            placed = await self.broker.place_order(order)
        except Exception as exc:
            # Outcome unknown: keep the metadata so a position that did open is managed.
            log.exception("placing the entry for %s failed", key)
            return OrderRecord(
                request=order,
                status=OrderStatus.REJECTED,
                message=_short(f"error (outcome unknown, check the broker): {exc}"),
            )
        if placed.status not in _ACCEPTED:
            log.warning("entry for %s not accepted: %s", key, placed.message)
            self.db.delete_position_meta(key)
            self.db.set_kv(EXTRA_PREFIX + key, "{}")
            return placed
        day = self._today(now)
        day.trades += 1
        self._save_day()
        log.info(
            "BUY %s %g @ %g (stop %g, target %s) -> %s",
            key,
            order.qty,
            entry,
            stop,
            order.take_profit,
            placed.status.value,
        )
        await self._notify(
            f"BUY {order.instrument.symbol}",
            f"{order.qty:g} @ {entry:g}, stop {stop:g}, target {order.take_profit}",
        )
        return placed

    async def _handle_close(
        self,
        decision: AIDecision,
        inst: Instrument,
        batch: DecisionBatch,
        ctx: DecisionContext,
        now: datetime,
    ) -> None:
        pos = next((p for p in ctx.positions if p.instrument.key == inst.key and p.qty > 0), None)
        if pos is None:
            log.info("CLOSE %s ignored: no position", inst.key)
            return
        if inst.asset_class == AssetClass.STOCK and not stock_market_open(now):
            log.info("CLOSE %s deferred: the stock market is closed", inst.key)
            return
        req = OrderRequest(
            instrument=inst,
            side=Side.SELL,
            qty=pos.qty,
            order_type=OrderType.MARKET,
            is_exit=True,
            reason=_short(f"AI close: {decision.rationale}"),
            decision_id=batch.id,
            setup=pos.setup,
        )
        if self.shadow:
            self._journal_order(
                OrderRecord(request=req, status=OrderStatus.BLOCKED, message="shadow mode"), now
            )
            return
        if not self.settings.autopilot:
            if inst.key in self._awaiting_approval():
                return
            self._journal_order(
                OrderRecord(
                    request=req,
                    status=OrderStatus.PENDING_APPROVAL,
                    message="waiting for approval, expires in 10 min",
                ),
                now,
            )
            await self._notify(f"Approve CLOSE {inst.symbol}?", req.reason, tags=["question"])
            return
        if self._closing_now(inst.key, now):
            return
        self._closing[inst.key] = now
        self._close_reasons[inst.key] = req.reason
        rec = await self.broker.close_position(inst, reason=req.reason)
        if rec is not None:
            self._journal_order(rec.model_copy(update={"request": req}), now)


_ACCEPTED = frozenset({OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED})


def build_engine(config: AppConfig, db: Database) -> Engine:
    """Engine wired from config: SimBroker or IBKRBroker; Claude (with the rule-based maker
    as its fallback) when an Anthropic API key is configured, else rules only. The engine
    uses Claude only while settings.llm_enabled is on, so the owner can toggle it live."""
    settings = db.get_settings()
    broker: Broker
    if config.broker == "sim":
        from trader.broker.sim import SimBroker

        broker = SimBroker(
            seed=config.sim_seed,
            starting_cash=config.sim_starting_cash,
            commission_plan=config.commission_plan,
        )
    else:
        from trader.broker.ibkr import IBKRBroker

        broker = IBKRBroker(config)
    rules = RuleBasedDecisionMaker(settings)
    decider: DecisionMaker = rules
    api_key = config.anthropic_api_key.get_secret_value() if config.anthropic_api_key else ""
    if api_key.strip():
        from trader.strategy.claude import ClaudeDecisionMaker

        decider = ClaudeDecisionMaker(
            api_key=api_key,
            model=settings.llm_model,
            effort=settings.llm_effort,
            fallback=rules,
        )
    return Engine(config, db, broker, decider, notifier=Notifier(config))
