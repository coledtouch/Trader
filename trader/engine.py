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
(engine-managed crypto stops, halts, flatten before the close) until `shutdown()`, and
`start_monitor()` restores that state after an app restart.
Exit rules only touch positions the engine opened (those with positions_meta); the kill
switch and the daily-loss flatten close everything. In shadow mode (live account, not
armed) no entry is ever sent, but exits still are for positions the engine opened (stops,
targets, time stops, flatten before the close, the daily-loss flatten, AI closes) because
they only reduce risk; the owner-confirmed kill closes everything. Every broker failure is
logged and survived; when in doubt, no order is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from trader.broker.base import Broker, BrokerError
from trader.config import AppConfig
from trader.db import EQUITY_SINCE_KEY, Database
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
    order_trace_id,
    utcnow,
)
from trader.notify import Notifier
from trader.risk import (
    HALT_DAILY_LOSS,
    RiskManager,
    available_cash,
    is_soft_halt,
    max_position_value,
    sane_exit_price,
)
from trader.strategy.consensus import PREFIX as CONSENSUS_PREFIX
from trader.strategy.consensus import label as maker_label
from trader.strategy.decide import DecisionMaker, RuleBasedDecisionMaker
from trader.strategy.signals import Scanner
from trader.timeutil import (
    in_entry_window,
    market_close_time,
    minutes_to_close,
    stock_market_open,
    trading_day,
    trading_day_start,
)
from trader.watchdog import write_heartbeat

log = logging.getLogger(__name__)

KILL_REASON = "kill switch"
# The crypto watchdog's broker tags. They share the ai-trader- prefix with this app's
# order ids, but they are not rows this process should adopt after a restart.
WATCHDOG_TRACE_PREFIX = "ai-trader-wd-"


def _app_order_trace(trace: str | None) -> str | None:
    """An order tag this process may reconcile. Watchdog tags are skipped."""
    if not trace or trace.startswith(WATCHDOG_TRACE_PREFIX):
        return None
    return trace


APPROVAL_TTL = timedelta(minutes=10)
ENTRY_ORDER_TTL = timedelta(minutes=10)  # unfilled entry orders are cancelled after this
EQUITY_POINT_EVERY = timedelta(minutes=5)
# A balance change after the account has been flat (no position, no working order, no fill)
# for this long cannot be trading P&L: it is a deposit, a withdrawal or a paper-account
# reset, and it is kept out of today's P&L and limits. The wait also covers IBKR's account
# values, which can lag a closed trade by a few minutes.
CASH_FLOW_QUIET = timedelta(minutes=10)
CASH_FLOW_MIN_USD = 1.0
CURVE_RESTART_FRACTION = 0.5  # a change this large (e.g. a paper reset) restarts the curve
# The untraded-day rebase only treats a gap this large (of today's start equity) as a
# reset or transfer; a smaller unexplained gap keeps counting as P&L (safe direction).
UNTRADED_REBASE_MIN_FRACTION = 0.5
BROKER_DOWN_NOTIFY_AFTER = timedelta(seconds=60)
BACKUP_STOP_BREACH = 0.005  # stocks: engine sells only 0.5% beyond a stop the broker lacks
FLATTEN_BEFORE_CLOSE_MIN = 10.0
CLOSE_RETRY = timedelta(seconds=60)  # do not resend an engine close sooner than this
# A BUY decided on market data older than this is dropped instead of sent (the decision
# maker took too long: the setup is stale). Younger ones are re-checked on fresh data.
DECISION_MAX_AGE = timedelta(minutes=3)
# IBKR pushes DayTradesRemaining about every 3 minutes: the bot's own day trades younger
# than this are subtracted from it before an entry is checked.
PDT_REFRESH = timedelta(minutes=5)
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
ACCOUNT_KEY = "broker_account"  # kv: the broker account id the journal belongs to
SENT_PREFIX = "order_sent:"  # kv: when an approved order was actually sent (order id)
# Engine-side stops/targets/time stops and the daily halts fire only when the breach is
# seen on this many consecutive monitor ticks (one bad tick never sells or flattens).
# Broker-native stops are not affected.
BREACH_CONFIRM_TICKS = 2
# ...and those ticks must span at least this fraction of the monitor interval, so the tick
# run_cycle runs right after the monitor loop's never counts as a second look.
BREACH_MIN_SPACING = 0.5
# Which API key each LLM decision maker needs (for the "fell back" warning).
KEY_NAMES = {"claude": "Anthropic", "grok": "xAI"}


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


def _refused_exits(records: Iterable[OrderRecord]) -> str:
    """Exit orders the broker rejected, as "SYMBOL: message; ..." ("" when none)."""
    return "; ".join(
        f"{r.request.instrument.symbol}: {r.message or 'rejected'}"
        for r in records
        if r.status == OrderStatus.REJECTED
    )


def _mask(account_id: str) -> str:
    """Account ids in logs, the audit log and notifications: the type prefix (IBKR "DU" =
    paper, "U" = live) and the last 3 characters."""
    text = account_id.strip()
    if len(text) <= 5:
        return text
    letters = len(text) - len(text.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))
    return f"{text[: min(letters, 2)]}...{text[-3:]}"


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
        makers: Mapping[str, DecisionMaker] | None = None,
    ) -> None:
        """`makers` maps decision modes ("rules", "claude", "grok", "consensus") to the makers
        that exist (a mode is missing when its API key is). With it, settings.decision_mode
        picks the maker every cycle; without it, `decider` is used for every AI mode."""
        self.config = config
        self.db = db
        self.broker = broker
        self._decider = decider
        self._makers: dict[str, DecisionMaker] | None = dict(makers) if makers is not None else None
        self.notifier = notifier
        self._clock = clock
        self.settings = db.get_settings()
        self.scanner: SignalScanner = scanner or Scanner(self.settings, tz=config.market_timezone)
        self.risk = risk or RiskManager(self.settings.risk)
        fallback = getattr(decider, "fallback", None)
        listed = (self._makers or {}).get("rules")
        if isinstance(listed, RuleBasedDecisionMaker):
            self.rules = listed
        elif isinstance(decider, RuleBasedDecisionMaker):
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
        # When broker fills were last journaled on the current connection (None: not yet).
        # Balance changes are judged only after that, so a fill made while the app was
        # stopped or disconnected (e.g. a stop-loss) is journaled first, never mistaken for
        # a deposit, withdrawal or reset.
        self._fills_journaled_at: datetime | None = None
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
        # consecutive monitor ticks that saw an engine-side exit / a daily halt condition
        # instrument -> (kind, ticks, first seen)
        self._breaches: dict[str, tuple[str, int, datetime]] = {}
        self._halt_streak: tuple[str, int, datetime] | None = None  # (halt kind, ticks, first)
        self._halt_pending: str | None = None  # halt seen once, waiting for confirmation

        # Journal epoch. The simulator keeps its account in memory, so every process trades
        # a fresh simulated account; a change of broker account (sim -> paper -> live) is
        # the same situation. Journal fills from before the epoch are never matched against
        # the new account, open journal orders from before it are closed out, and today's
        # P&L restarts from the new account's equity at the next snapshot.
        self._epoch: datetime | None = self._now() if broker.mode == Mode.SIM else None
        self._epoch_reason = "simulator restarted"
        self._rebase_pending = self._epoch is not None
        self._busy_at = self._now()  # last time the account held a position or order
        self._enforce_live_gate()
        self._backfill_equity_floor()
        self._backfill_curve_after_cash_flow()

    def _backfill_equity_floor(self) -> None:
        """Journals from before equity-curve restarts existed: start the curve at the last
        recorded broker account change so it does not jump between accounts."""
        if self.db.get_kv(EQUITY_SINCE_KEY) is not None:
            return
        changes = [a for a in self.db.list_audit(limit=1000) if a["action"] == "account_changed"]
        if changes:
            self.db.set_kv(EQUITY_SINCE_KEY, changes[0]["ts"])  # newest first

    def _enforce_live_gate(self) -> None:
        """Arming lasts only while the server gate (TRADER_ALLOW_LIVE) stays open. The gate
        is read from the environment at process start, so a process that starts with it
        closed clears a stored arm: re-opening the gate later always needs a fresh Arm with
        a 2FA code (the documented lockdown must not be undone by re-enabling the gate)."""
        if self.config.allow_live or not self.db.get_flag("live_armed"):
            return
        self.db.set_flag("live_armed", False)
        self.db.audit(
            "live_disarmed", {"reason": "TRADER_ALLOW_LIVE=false at startup"}, actor="engine"
        )
        log.warning("live trading disarmed: TRADER_ALLOW_LIVE is false (a fresh Arm is needed)")

    # ------------------------------------------------------------------ basics
    def _now(self) -> datetime:
        return _aware(self._clock()).astimezone(UTC)

    @property
    def decider(self) -> DecisionMaker:
        """The decision maker the settings select right now (see `_resolve_decider`)."""
        return self._resolve_decider()[0]

    def _resolve_decider(self) -> tuple[DecisionMaker, str | None]:
        """(maker for settings.decision_mode, warning when it is not what the owner chose).

        Consensus needs both API keys; with one, that model decides alone, and with none
        (or a single-model mode without its key) the rule-based maker decides. The engine
        never pretends: every fallback comes with a warning for the dashboard."""
        if self._makers is None:
            return self._decider, None
        mode = self.settings.decision_mode
        makers = self._makers
        if mode == "rules":
            return self.rules, None
        if mode in makers:
            return makers[mode], None
        if mode == "consensus":
            have = [name for name in ("claude", "grok") if name in makers]
            if len(have) == 1:
                alone = have[0]
                missing = "grok" if alone == "claude" else "claude"
                return makers[alone], (
                    f"Consensus mode needs both API keys, but no {KEY_NAMES[missing]} key "
                    f"is set: {maker_label(alone)} decides alone (no second opinion)."
                )
            return self.rules, (
                "Consensus mode needs Anthropic and xAI API keys and neither is set: "
                "rules-only decisions."
            )
        return self.rules, (
            f"{maker_label(mode)} mode needs an {KEY_NAMES.get(mode, mode)} API key, "
            "which is not set: rules-only decisions."
        )

    def _all_makers(self) -> list[DecisionMaker]:
        """Every decision maker the engine may call, including those inside consensus."""
        found: list[DecisionMaker] = []
        pending: list[object] = [self._decider, *(self._makers or {}).values()]
        while pending:
            maker = pending.pop()
            if maker is None or any(maker is m for m in found):
                continue
            found.append(maker)  # type: ignore[arg-type]
            pending += [getattr(maker, attr, None) for attr in ("primary", "secondary")]
        return found

    @property
    def shadow(self) -> bool:
        """Live account without the owner's arming: decisions are logged and entries are
        blocked; exits for positions the engine opened still go out."""
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

    def _external_cash_flow(
        self, previous: AccountSnapshot | None, account: AccountSnapshot, now: datetime
    ) -> float:
        """The equity change since the last snapshot when it cannot be trading P&L (the
        account has been flat for CASH_FLOW_QUIET), else 0.0."""
        if (
            previous is None
            or self._fills_journaled_at is None
            or previous.account_id != account.account_id
            or not math.isfinite(previous.equity)
            or previous.equity <= 0
        ):
            return 0.0
        delta = account.equity - previous.equity
        if abs(delta) < CASH_FLOW_MIN_USD or now - self._busy_at < CASH_FLOW_QUIET:
            return 0.0
        if self.db.list_fills(since=now - CASH_FLOW_QUIET, limit=1):
            return 0.0
        return round(delta, 2)

    async def _apply_cash_flow(
        self, flow: float, account: AccountSnapshot, day: DayStats, now: datetime
    ) -> None:
        """Keep a deposit, withdrawal or paper reset out of today's P&L and limits."""
        if day.start_equity is not None:
            day.start_equity = round(day.start_equity + flow, 2)
        raw = self.db.get_kv(BASELINE_PREFIX + day.day)
        if raw:
            with contextlib.suppress(ValueError):
                self.db.set_kv(BASELINE_PREFIX + day.day, repr(float(raw) + flow))
        previous = account.equity - flow
        if previous > 0 and abs(flow) / previous >= CURVE_RESTART_FRACTION:
            self.db.start_equity_history(now)
            self._last_equity_at = None
        kind = "deposit" if flow > 0 else "withdrawal or reset"
        log.warning("balance change of %+.2f with no positions open: %s", flow, kind)
        self.db.audit(
            "cash_flow", {"amount": flow, "equity": round(account.equity, 2)}, actor="engine"
        )
        await self._notify(
            "Balance change detected",
            f"{'+' if flow > 0 else '-'}${abs(flow):,.2f} while no positions were open: "
            f"treated as a {kind}. Today's P&L and limits leave it out.",
        )

    def _untraded_day_gap(self, day: DayStats, account: AccountSnapshot, now: datetime) -> float:
        """Equity minus today's start equity when today cannot have trading P&L, else 0.0:
        no fills or trades since the day began (with this connection's fills journaled), the
        account flat for CASH_FLOW_QUIET since it last held anything or (re)connected, and a
        gap of at least UNTRADED_REBASE_MIN_FRACTION, i.e. one that looks like a reset or a
        transfer. Catches a balance change the snapshot comparison never saw: one made while
        the app was stopped, or before an upgrade that added the comparison."""
        start = day.start_equity
        if start is None or not math.isfinite(start) or start <= 0:
            return 0.0
        if day.trades or day.wins or day.losses or abs(day.realized_pnl) > EPS:
            return 0.0
        if self._fills_journaled_at is None:
            return 0.0
        gap = account.equity - start
        if abs(gap) / start < UNTRADED_REBASE_MIN_FRACTION:
            return 0.0
        if abs(gap) < CASH_FLOW_MIN_USD or now - self._busy_at < CASH_FLOW_QUIET:
            return 0.0
        if self.db.list_fills(since=trading_day_start(day.day), limit=1):
            return 0.0
        return round(gap, 2)

    async def _rebase_untraded_day(
        self, gap: float, account: AccountSnapshot, day: DayStats, now: datetime
    ) -> None:
        """Restart today's P&L and limits from the current equity (see _untraded_day_gap).
        A halt stays: only the owner clears it."""
        old = day.start_equity or 0.0
        day.start_equity = account.equity
        if self.db.get_kv(BASELINE_PREFIX + day.day):
            self.db.set_kv(BASELINE_PREFIX + day.day, repr(account.equity))
        self._restart_curve_after_jump(account.equity, trading_day_start(day.day), now, now)
        log.warning(
            "no trading today but equity moved %+.2f from the day's start: treated as a "
            "balance change",
            gap,
        )
        self.db.audit(
            "cash_flow",
            {"amount": gap, "equity": round(account.equity, 2), "source": "untraded day"},
            actor="engine",
        )
        await self._notify(
            "Balance change detected",
            f"Today started at ${old:,.2f} and there were no trades, so the "
            f"{'+' if gap > 0 else '-'}${abs(gap):,.2f} difference is a deposit, withdrawal "
            f"or reset. Today's P&L and limits now start from ${account.equity:,.2f}.",
        )

    def _restart_curve_after_jump(
        self, equity: float, since: datetime, before: datetime, now: datetime
    ) -> None:
        """Start the equity curve right after the newest jump of CURVE_RESTART_FRACTION or
        more between two consecutive points from `since` up to (`before`, `equity`), so a
        balance change that reached the curve before it was recognised does not show as a
        cliff. Gradual moves, however large in total, are never hidden (the risk limits keep
        trading far from a 50% move between two points), nor are points from `before` on."""
        series = [
            (_aware(datetime.fromisoformat(p["ts"])), float(p["equity"]))
            for p in self.db.list_equity(since=since)
        ]
        series = [(ts, value) for ts, value in series if ts < before] + [(before, equity)]
        cut: datetime | None = None
        for (_, old), (ts, new) in itertools.pairwise(series):
            if old > 0 and abs(new - old) / old >= CURVE_RESTART_FRACTION:
                cut = ts
        if cut is None:
            return
        self.db.start_equity_history(cut)
        if cut >= before:
            self._last_equity_at = None  # the next tick records the first point

    def _backfill_curve_after_cash_flow(self) -> None:
        """Journals from before the check above: a paper reset recognised only later (the
        untraded-day rebase) left a cliff on the curve. Start the curve after it. Idempotent."""
        flows = [a for a in self.db.list_audit(limit=1000) if a["action"] == "cash_flow"]
        if not flows:
            return
        latest = flows[0]  # newest first
        equity = latest["detail"].get("equity")
        if isinstance(equity, bool) or not isinstance(equity, int | float) or not equity > 0:
            return
        at = _aware(datetime.fromisoformat(latest["ts"]))
        self._restart_curve_after_jump(
            float(equity), trading_day_start(trading_day(at)), at, self._now()
        )

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
        await self._start_monitor()
        self._decision_task = asyncio.create_task(self._decision_loop(), name="engine-decision")
        log.info("engine started (%s, %s)", self.broker.name, self.broker.mode.value)

    async def start_monitor(self) -> None:
        """Connect and run only the monitor loop (idempotent): open positions stay protected
        (engine-managed stops, halts, flatten before the close, journal) while no decisions
        are made. The app calls this at startup when the bot was stopped, which is the same
        state `stop()` leaves behind."""
        await self._start_monitor()
        log.info("engine monitor running; decisions stay off until the bot is started")

    async def _start_monitor(self) -> None:
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

    async def stop(self) -> None:
        """Stop making decisions. Positions, broker stops and the monitor loop (which
        manages engine-side stops) stay in place."""
        self.db.set_flag("engine_running", False)
        await self._stop_decisions()
        log.info("engine stopped (monitor keeps protecting open positions)")

    async def shutdown(self) -> None:
        """Stop everything and disconnect (app shutdown). Broker-side stops remain, and the
        persisted running flag is kept so a restart resumes the previous state."""
        await self._stop_decisions()
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await self.broker.disconnect()
        except Exception as exc:
            log.warning("broker disconnect failed: %s", type(exc).__name__)
        log.info("engine shut down (broker-side stops remain in place)")

    async def _stop_decisions(self) -> None:
        self._running = False
        task, self._decision_task = self._decision_task, None
        self._next_cycle_at = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

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
                records = await self.broker.flatten_all(KILL_REASON)  # cancels everything
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
        for rec in records:
            self._journal_order(rec, now)
        refused = _refused_exits(records)
        if refused:
            log.error("kill switch: the broker refused to close %s", refused)
            self._set_error("kill", f"kill switch failed: the broker refused to close {refused}")
            await self._notify(
                "KILL switch FAILED",
                f"Orders cancelled, but the broker refused to close {_short(refused)}. "
                "Check the broker now.",
                priority="urgent",
                tags=["rotating_light"],
            )
            return
        self._clear_error("kill")
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
        """Back to shadow mode on a live account: no new entries, while the engine keeps
        managing exits for the positions it opened (broker-side stops stay in place)."""
        self.db.set_flag("live_armed", False)
        log.warning("live trading disarmed by %s", actor)
        if self.shadow and any(p.qty > 0 for p in self._positions):
            await self._notify(
                "Live trading disarmed",
                "No new entries. The bot keeps managing exits (stops, targets, time stops, "
                "close before 4 PM) for positions it opened; other positions are yours.",
                priority="high",
                tags=["warning"],
            )

    def reload_settings(self) -> None:
        """Re-read BotSettings from the database and apply them everywhere."""
        self.settings = self.db.get_settings()
        self.risk.set_limits(self.settings.risk)
        self.scanner.settings = self.settings
        self.rules.settings = self.settings
        from trader.strategy.claude import ClaudeDecisionMaker

        for maker in self._all_makers():  # decision_mode itself is read every cycle
            fallback = getattr(maker, "fallback", None)
            if isinstance(fallback, RuleBasedDecisionMaker):
                fallback.settings = self.settings
            if isinstance(maker, ClaudeDecisionMaker):
                maker.model = self.settings.llm_model
                maker.effort = self.settings.llm_effort

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
        # Prices are fetched without the entry lock so Reject can cancel the order while
        # this is in flight. Reject takes the same lock; the check below runs again,
        # still holding it, immediately before anything is sent.
        await self._ensure_connected(now)
        req = rec.request
        quote = await self.broker.quote(req.instrument)
        self._track_halt(req.instrument.key, quote, now)
        account = await self.broker.account()
        positions = await self.broker.positions()
        open_orders = await self.broker.open_orders()
        async with self._entry_lock:
            rec = self._pending(order_id)  # rejected (or already sent) while prices loaded
            now = self._now()
            if now - _aware(rec.created_at) >= APPROVAL_TTL:
                self._expire(rec)
                return self._get_order(order_id)
            req = rec.request
            day = self._today(now)
            verdict = None
            checked_account = self._with_day_trades(account, now)
            known_account = self.db.get_kv(ACCOUNT_KEY)
            if known_account and account.account_id and account.account_id != known_account:
                self.db.update_order(
                    order_id,
                    status=OrderStatus.CANCELLED,
                    message="cancelled: broker account changed since this order was proposed",
                )
                self._revert_signal(order_id, "broker account changed")
                return self._get_order(order_id)
            if req.instrument.key in self._pending_trades:
                reasons = ["previous trade in this symbol is still being journaled"]
            elif self._halt_pending is not None and not req.is_exit:
                reasons = [f"halt condition being confirmed: {self._halt_pending}"]
            else:
                verdict = self.risk.check(
                    req,
                    quote=quote,
                    account=checked_account,
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
            order = self._traced(verdict.order or req, order_id)
            if self.shadow and not order.is_exit:  # exits only reduce risk: still allowed
                self.db.update_order(
                    order_id, status=OrderStatus.BLOCKED, message="shadow mode: live not armed"
                )
                self._revert_signal(order_id, "shadow mode")
                return self._get_order(order_id)
            # Again, immediately before the broker call: Reject holds this lock to cancel.
            self._pending(order_id)
            if order.is_exit:
                placed = await self.broker.close_position(
                    order.instrument, order.reason, trace_id=order.trace_id
                )
                if placed is None:
                    self.db.update_order(
                        order_id, status=OrderStatus.CANCELLED, message="no position to close"
                    )
                    return self._get_order(order_id)
                self._closing[order.instrument.key] = now
                self._close_reasons[order.instrument.key] = order.reason
            else:
                placed = await self._send_entry(order, now)
                # The stale-entry timer runs from now, not from when it was proposed.
                self.db.set_kv(SENT_PREFIX + str(order_id), now.isoformat())
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
        """Cancel a pending order. Raises LookupError / ValueError like approve_order.

        Uses the same lock as approve_order, so a reject that lands while approval is
        fetching prices cancels the order before it can be sent.
        """
        async with self._entry_lock:
            self._pending(order_id)
            self.db.update_order(
                order_id, status=OrderStatus.CANCELLED, message=f"rejected by {actor}"
            )
            self._revert_signal(order_id, f"rejected by {actor}")
            return self._get_order(order_id)

    def _traced(self, order: OrderRequest, order_id: int | None) -> OrderRequest:
        """Copy of `order` carrying a unique broker tag, the journal id when we have one."""
        trace = order_trace_id(order_id, order.trace_id)
        if order.trace_id == trace:
            return order
        return order.model_copy(update={"trace_id": trace})

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
        maker, warning = self._resolve_decider()
        equity = self._account.equity if self._account is not None else None
        start = day.start_equity if day is not None else None
        loss_limit: float | None = None
        if day is not None and start:
            baseline = self._halt_eval_day(day).start_equity or start
            floor = baseline * (1 - self.risk.limits.max_daily_loss_pct / 100)
            loss_limit = round(max(0.0, start - floor), 2)
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
            pnl_target=self.risk.limits.profit_target_usd(start),
            loss_limit_usd=loss_limit,
            trades_today=day.trades if day else 0,
            llm_cost_today=round(day.llm_cost, 4) if day else 0.0,
            data_delayed=self.broker.data_delayed,
            decision_maker=maker.name if self.settings.llm_enabled else self.rules.name,
            decision_warning=warning if self.settings.llm_enabled else None,
            data_warning=self.broker.data_warning,
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
        self._busy_at = now  # what happened while disconnected is not known to be quiet
        self._fills_journaled_at = None

    # ------------------------------------------------------------------ monitor
    async def monitor_tick(self) -> None:
        """One monitor pass. Raises BrokerError when the broker is unreachable."""
        async with self._monitor_lock:
            now = self._now()
            day = self._today(now)
            self._expire_approvals(now)
            await self._ensure_connected(now)
            account = await self.broker.account()
            await self._check_account(account, now)
            raw_positions = await self.broker.positions()
            open_orders = await self.broker.open_orders()
            metas = self.db.all_position_meta()
            quotes = await self._quotes([p.instrument for p in raw_positions if p.qty > 0], now)
            self._note_data_warning()
            positions = self._mark_positions(raw_positions, quotes, metas)
            account = self._mark_account(account, raw_positions, positions)
            previous = self._account
            if any(p.qty > 0 for p in raw_positions) or open_orders:
                self._busy_at = now
            self._account, self._positions, self._open_orders = account, positions, open_orders

            if math.isfinite(account.equity) and account.equity > 0:
                changed = False
                if not self._rebase_pending:
                    flow = self._external_cash_flow(previous, account, now)
                    if flow:
                        await self._apply_cash_flow(flow, account, day, now)
                        changed = True
                if self._rebase_pending:
                    self._rebase_pending = False
                    if day.start_equity is not None and day.start_equity != account.equity:
                        log.info(
                            "%s: today's P&L restarts from $%.2f",
                            self._epoch_reason,
                            account.equity,
                        )
                        day.start_equity, changed = account.equity, True
                        self.db.set_kv(BASELINE_PREFIX + day.day, "")
                        if is_soft_halt(day.halt_reason):  # the target was the old account's
                            day.halted, day.halt_reason = False, None
                        # The equity curve restarts too, with a first point right now.
                        self.db.start_equity_history(now)
                        self._last_equity_at = None
                if not changed:
                    gap = self._untraded_day_gap(day, account, now)
                    if gap:
                        await self._rebase_untraded_day(gap, account, day, now)
                        changed = True
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
                # e.g. IBKR before the account summary arrives: never halt or flatten on it.
                # A pending halt stays pending (no entries) but must be seen again twice.
                self._halt_streak = None
                log.warning("account equity %r is not usable; halts not evaluated", account.equity)
                self._set_error("monitor:halts", "account equity not available")
            # Also in shadow mode: these only exit or protect positions the engine opened.
            await self._step("exits", self._manage_positions(now, positions, quotes, metas))
            # Only a pass that actually checked stops refreshes the heartbeat. A broker
            # failure leaves it stale so the crypto watchdog can sell on its own connection.
            if "monitor:exits" not in self._errors:
                write_heartbeat(self.db, now)
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

    def _note_data_warning(self) -> None:
        """Mirror the broker's market-data warning (e.g. IBKR 10197) into last_error."""
        warning = self.broker.data_warning
        if not warning:
            self._clear_error("market data")
        elif self._errors.get("market data", (None, None))[1] != _short(warning, 300):
            log.warning("market data: %s", warning)
            self._set_error("market data", warning)

    # ------------------------------------------------------------------ day trades
    def _with_day_trades(self, account: AccountSnapshot, now: datetime) -> AccountSnapshot:
        """`account` with DayTradesRemaining lowered by the bot's own recent day trades.

        IBKR pushes DayTradesRemaining only every few minutes, so a stock round trip that
        just closed may not be counted yet and one more entry could become the day trade
        that flags the account. Same-day round trips whose closing fill is younger than
        PDT_REFRESH are subtracted. This is conservative: for a few minutes a trade IBKR
        already counted may be subtracted twice, which can only block an entry."""
        remaining = account.day_trades_remaining
        if remaining is None:
            return account
        recent = self._recent_day_trades(now)
        if recent <= 0:
            return account
        return account.model_copy(update={"day_trades_remaining": max(0, remaining - recent)})

    def _recent_day_trades(self, now: datetime) -> int:
        """Stock exit orders (by order id) filled within PDT_REFRESH that closed shares
        bought earlier the same New York day, from the fills journal."""
        today = trading_day(now)
        fills = [
            f
            for f in self.db.list_fills(since=now - timedelta(days=1), limit=FILL_QUERY_LIMIT)
            if f.instrument.asset_class == AssetClass.STOCK
            and trading_day(_aware(f.ts)) == today
            and (self._epoch is None or _aware(f.ts) >= self._epoch)
        ]
        fills.sort(key=lambda f: (_aware(f.ts), f.exec_id))
        bought: set[str] = set()
        closed: set[tuple[str, str]] = set()
        for fill in fills:
            key = fill.instrument.key
            if fill.side == Side.BUY:
                bought.add(key)
            elif key in bought and now - _aware(fill.ts) <= PDT_REFRESH:
                closed.add((key, fill.broker_order_id or fill.exec_id))
        return len(closed)

    # ------------------------------------------------------------------ account changes
    async def _check_account(self, account: AccountSnapshot, now: datetime) -> None:
        """Remember which broker account the journal belongs to. When it changes (sim ->
        paper "DU..." -> live "U..."), start a new journal epoch so the engine never manages
        positions or orders it did not open on this account."""
        current = (account.account_id or "").strip()
        if not current:
            return
        known = self.db.get_kv(ACCOUNT_KEY)
        if known == current:
            return
        self.db.set_kv(ACCOUNT_KEY, current)
        if not known:
            log.info("journal belongs to broker account %s", _mask(current))
            return
        await self._on_account_changed(known, current, now)

    async def _on_account_changed(self, old: str, new: str, now: datetime) -> None:
        reason = "broker account changed"
        log.warning("%s: %s -> %s; starting a new journal epoch", reason, _mask(old), _mask(new))
        self._epoch, self._epoch_reason, self._rebase_pending = now, reason, True
        self._cancel_pending(f"cancelled: {reason}")
        stale = self.db.list_orders(limit=500, statuses=OPEN_ORDER_STATUSES)
        for rec in stale:
            if rec.id is not None:
                self.db.update_order(
                    rec.id,
                    status=OrderStatus.CANCELLED,
                    message=_short(f"{rec.message}; {reason}: left with the previous account"),
                )
        metas = self.db.all_position_meta()
        for key in metas:
            self.db.delete_position_meta(key)
            self.db.set_kv(EXTRA_PREFIX + key, "{}")
        for cache in (self._closing, self._close_reasons, self._open_qty, self._breaches):
            cache.clear()
        self._pending_trades.clear()
        self._halt_streak, self._halt_pending = None, None
        disarmed = self.db.get_flag("live_armed")
        if disarmed:
            self.db.set_flag("live_armed", False)  # arming was for the previous account
        self.db.audit(
            "account_changed",
            {
                "from": _mask(old),
                "to": _mask(new),
                "orders_closed_out": len(stale),
                "positions_forgotten": len(metas),
                "live_disarmed": disarmed,
            },
            actor="engine",
        )
        await self._notify(
            "Broker account changed",
            f"Now on {_mask(new)} (was {_mask(old)}). The bot forgot {len(metas)} position(s) "
            f"and {len(stale)} working order(s) of the previous account and will not manage "
            "them; today's P&L restarts from this account's equity."
            + (" Live trading was disarmed." if disarmed else ""),
            priority="high",
            tags=["warning"],
        )

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
            # Implausible quotes (crossed, bid far from last) are ignored: the broker's own
            # mark is used instead, so one bad tick cannot move equity or trigger a halt.
            mark = sane_exit_price(quote) or pos.market_price
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
            self._halt_streak, self._halt_pending = None, None
            return
        hard = reason.startswith(HALT_DAILY_LOSS)
        if day.halted and not (hard and is_soft_halt(day.halt_reason)):
            self._halt_streak, self._halt_pending = None, None
            return  # already halted; only a soft halt can escalate to a hard one
        # A halt (and the flatten that comes with a loss halt) needs the condition on
        # consecutive ticks, so one bad equity print cannot sell everything. Meanwhile no
        # new entry goes out (run_cycle and approvals check _halt_pending).
        kind = "loss" if hard else "target"
        streak = self._halt_streak
        ticks, first = (streak[1] + 1, streak[2]) if streak and streak[0] == kind else (1, now)
        self._halt_streak = (kind, ticks, first)
        if not self._confirms(ticks, first, now):
            if self._halt_pending != reason:
                log.warning("halt condition seen (%s); confirming on the next tick", reason)
            self._halt_pending = reason
            return
        self._halt_streak, self._halt_pending = None, None
        day.halted, day.halt_reason = True, reason
        self._save_day()
        log.warning("trading halted: %s", reason)
        self.db.audit("halt", {"reason": reason}, actor="engine")
        self._cancel_pending(f"cancelled: trading halted ({reason})")
        flatten = hard and self.risk.limits.flatten_on_halt
        detail = reason
        if flatten:
            try:
                if self.shadow:
                    records = await self._flatten_engine_positions(reason, now)
                    detail += f"; shadow mode: closing {len(records)} bot position(s) only"
                else:
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
            records = await self.broker.flatten_all(reason)  # cancels everything first
        for rec in records:
            self._journal_order(rec, now)
        self.db.audit("flatten", {"reason": reason, "orders": len(records)}, actor="engine")
        refused = _refused_exits(records)
        if refused:
            raise BrokerError(f"the broker refused to close {refused}")
        self._clear_error("flatten")
        return records

    async def _flatten_engine_positions(self, reason: str, now: datetime) -> list[OrderRecord]:
        """Shadow-mode flatten: close only the positions the engine opened (with
        positions_meta). Positions the owner holds on the live account are never touched."""
        metas = self.db.all_position_meta()
        records: list[OrderRecord] = []
        async with self._entry_lock:
            for pos in list(self._positions):
                key = pos.instrument.key
                if pos.qty <= 0 or key not in metas:
                    continue
                self._closing[key] = now
                self._close_reasons[key] = reason
                rec = await self.broker.close_position(pos.instrument, reason=reason)
                if rec is not None:
                    records.append(rec)
        for rec in records:
            self._journal_order(rec, now)
        self.db.audit(
            "flatten", {"reason": reason, "orders": len(records), "scope": "bot"}, actor="engine"
        )
        refused = _refused_exits(records)
        if refused:
            raise BrokerError(f"the broker refused to close {refused}")
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
    def _queued_exits(open_orders: list[OrderRecord]) -> dict[str, float]:
        """Unfilled quantity of working market SELL exits per instrument: closes the broker
        holds until it can execute them (e.g. a kill or flatten sent after hours, or an exit
        queued during a halt). A protective stop next to one could sell the shares twice.
        Bracket take-profit children are limit orders and do not count."""
        queued: dict[str, float] = {}
        for order in open_orders:
            req = order.request
            if (
                order.status in OPEN_ORDER_STATUSES
                and req.side == Side.SELL
                and req.stop_loss is None
                and req.order_type == OrderType.MARKET
            ):
                key = req.instrument.key
                queued[key] = queued.get(key, 0.0) + max(0.0, req.qty - order.filled_qty)
        return queued

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
        breached: set[str] = set()
        for pos in positions:
            key = pos.instrument.key
            meta = metas.get(key)
            mark = sane_exit_price(quotes.get(key))  # None: no quote, or an implausible one
            if pos.qty <= 0 or meta is None or mark is None or self._closing_now(key, now):
                continue
            if pos.instrument.asset_class == AssetClass.STOCK and not stocks_open:
                continue  # stock orders cannot execute now; broker stops still protect
            try:
                await self._manage_one(pos, meta, mark, native_stops.get(key), now, breached)
            except BrokerError as exc:
                failures.append(f"{key}: {exc}")
        # A breach must be seen on consecutive ticks; anything not seen this tick restarts.
        self._breaches = {k: v for k, v in self._breaches.items() if k in breached}
        if failures:
            raise BrokerError("; ".join(failures))

    def _confirms(self, ticks: int, first_seen: datetime, now: datetime) -> bool:
        """Whether a condition seen on `ticks` consecutive monitor ticks since `first_seen`
        is confirmed: BREACH_CONFIRM_TICKS ticks that span at least half a monitor interval.
        run_cycle runs its own monitor tick right after (or even at the same moment as) the
        monitor loop's, and two looks at one bad print must not count as two."""
        spacing = timedelta(seconds=self.settings.monitor_seconds * BREACH_MIN_SPACING)
        return ticks >= BREACH_CONFIRM_TICKS and now - first_seen >= spacing

    def _confirmed(self, key: str, kind: str, breached: set[str], now: datetime) -> bool:
        """Count one more tick with an engine-side exit condition; True once it is
        confirmed (see `_confirms`)."""
        breached.add(key)
        last_kind, ticks, first = self._breaches.get(key, (kind, 0, now))
        ticks, first = (ticks + 1, first) if last_kind == kind else (1, now)
        self._breaches[key] = (kind, ticks, first)
        return self._confirms(ticks, first, now)

    async def _manage_one(
        self,
        pos: Position,
        meta: dict[str, Any],
        mark: float,
        broker_stop: float | None,
        now: datetime,
        breached: set[str] | None = None,
    ) -> None:
        """Exit rules for one engine position valued at `mark` (the bid). `broker_stop` is
        the highest protective stop working at the broker, if any. Exits fire only after
        the condition held on BREACH_CONFIRM_TICKS consecutive ticks (`breached` collects
        the instruments that saw one this tick)."""
        exits = self.settings.exits
        key = pos.instrument.key
        stop, target = meta.get("stop_loss"), meta.get("take_profit")
        native = self.broker.supports_native_stops(pos.instrument)
        reason: str | None = None
        kind = "stop"
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
            reason, kind = f"take-profit {target:g} hit (engine-managed, bid {mark:g})", "target"

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
            reason, kind = (
                (
                    f"time stop: {exits.time_stop_minutes} min with {r_now:.2f}R "
                    f"(< {exits.time_stop_min_r:g}R)"
                ),
                "time",
            )
        if reason is not None:
            if not self._confirmed(key, kind, breached if breached is not None else set(), now):
                log.info("%s: %s; confirming on the next tick", key, reason)
                return
            self._breaches.pop(key, None)
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
        owner is notified and the engine's backup stop guards the position. Shares a working
        market exit will already sell are not protected again (never sell twice)."""
        if not self._reprotect_pending:
            return
        self._reprotect_pending = False
        protected = self._native_stops(open_orders)
        buying = self._working_buys(open_orders)
        queued = self._queued_exits(open_orders)
        for pos in positions:
            key = pos.instrument.key
            meta = metas.get(key)
            stop = meta.get("stop_loss") if meta else None
            qty = round(pos.qty - queued.get(key, 0.0), 8)
            if (
                qty <= EPS
                or stop is None
                or pos.instrument.asset_class != AssetClass.STOCK
                or not self.broker.supports_native_stops(pos.instrument)
                or key in protected
                or key in buying
                or self._closing_now(key, now)
            ):
                continue
            if not self.broker.supports_standalone_stops(pos.instrument):
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
                qty=qty,
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
        await self._cancel_stale_entries(now)  # cancelling only reduces risk (shadow too)
        await self._sync_fills(now)
        await self._cleanup_meta(now, positions, open_orders)

    async def _sync_orders(self, now: datetime) -> None:
        traced: dict[str, OrderRecord] | None = None

        async def by_trace(trace: str | None) -> OrderRecord | None:
            """Working broker order whose tag matches, fetched once per sync.

            Watchdog closes use ai-trader-wd-... and belong to the other process.
            Adopting one of those onto an app order would point the journal at the
            wrong broker id.
            """
            nonlocal traced
            trace = _app_order_trace(trace)
            if not trace:
                return None
            if traced is None:
                traced = {}
                for live in await self.broker.open_orders():
                    live_trace = _app_order_trace(live.request.trace_id)
                    if live_trace and live.broker_order_id:
                        traced.setdefault(live_trace, live)
            return traced.get(trace)

        for rec in self.db.list_orders(limit=200, statuses=OPEN_ORDER_STATUSES):
            if rec.id is None or not rec.broker_order_id:
                continue
            latest = await self.broker.order_status(rec.broker_order_id)
            if latest is None:
                adopted = await by_trace(rec.request.trace_id)
                if adopted is not None:
                    self.db.update_order(
                        rec.id,
                        status=adopted.status,
                        broker_order_id=adopted.broker_order_id,
                        filled_qty=adopted.filled_qty,
                        avg_fill_price=adopted.avg_fill_price,
                        commission=adopted.commission,
                    )
                    log.info(
                        "reconciled order %s via %s -> broker %s",
                        rec.id,
                        rec.request.trace_id,
                        adopted.broker_order_id,
                    )
                    continue
                created = _aware(rec.created_at)
                restarted = self._epoch is not None and created < self._epoch
                if restarted or now - created > ORDER_FORGET_AFTER:
                    self.db.update_order(
                        rec.id,
                        status=OrderStatus.CANCELLED,
                        message=self._epoch_reason
                        if restarted
                        else "no longer known to the broker",
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
                or now - self._sent_at(rec) < ENTRY_ORDER_TTL
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

    def _sent_at(self, rec: OrderRecord) -> datetime:
        """When an order went to the broker: its journal time, or the approval time for an
        order that waited for the owner."""
        created = _aware(rec.created_at)
        raw = self.db.get_kv(SENT_PREFIX + str(rec.id)) if rec.id is not None else None
        if raw:
            with contextlib.suppress(ValueError):
                return max(created, _aware(datetime.fromisoformat(raw)))
        return created

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
            replaced = self.db.correct_fill(fill)
            if replaced is not None:
                # IBKR corrected an execution: the new revision replaces the old one (never
                # both: that would count the shares twice).
                log.warning(
                    "execution %s corrected by %s (%s %g @ %g -> %g @ %g)",
                    replaced.exec_id,
                    fill.exec_id,
                    key,
                    replaced.qty,
                    replaced.price,
                    fill.qty,
                    fill.price,
                )
                self._fill_commissions.pop(replaced.exec_id, None)
                self._fill_commissions[fill.exec_id] = (fill.commission, fill.ts)
                self._add_fee(replaced.ts, -replaced.commission)
                self._add_fee(fill.ts, fill.commission)
                touched.add(key)
                continue
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
        self._fills_journaled_at = now

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
        epoch = self._epoch  # fills before it belong to an earlier (simulated) account
        since = last_exit or now - TRADE_LOOKBACK
        if epoch is not None:
            since = max(since, epoch)
        fills = [
            f
            for f in self.db.list_fills(since=since, limit=FILL_QUERY_LIMIT)
            if f.instrument.key == key
            and (last_exit is None or _aware(f.ts) > last_exit)
            and (epoch is None or _aware(f.ts) >= epoch)
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
                    req.instrument.key == key
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
            if self._halt_pending is not None:
                log.info("cycle skipped: halt condition being confirmed (%s)", self._halt_pending)
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

    def _pick_maker(self, ctx: DecisionContext) -> tuple[DecisionMaker, str | None, bool]:
        """(maker, why it is not the configured one, whether it may open positions).

        The configured maker, or the rule-based one (with the reason) when the LLM is
        disabled, over budget, or has nothing worth deciding. In consensus mode the rules
        never open a position on their own (a BUY needs both models), so when they stand in
        for consensus they only manage exits."""
        configured, _ = self._resolve_decider()
        if configured is self.rules:
            return self.rules, None, True
        if not self.settings.llm_enabled:
            return self.rules, "LLM disabled", True
        entries = configured.name != "consensus"
        budget = self.risk.limits.llm_daily_budget_usd
        if ctx.day.llm_cost >= budget:
            if self._fallback_logged != ctx.day.day:
                self._fallback_logged = ctx.day.day
                log.warning(
                    "LLM budget reached ($%.2f of $%.2f): rules-only decisions for today",
                    ctx.day.llm_cost,
                    budget,
                )
            why = f"LLM budget reached (${ctx.day.llm_cost:.2f} of ${budget:.2f})"
            return self.rules, why, entries
        threshold = self.settings.llm_min_candidate_score
        managed = self.db.all_position_meta()  # positions the owner holds are not managed
        worth = any(c.score >= threshold for c in ctx.candidates) or any(
            p.qty > 0 and p.instrument.key in managed for p in ctx.positions
        )
        if not worth:
            return self.rules, "no candidate worth an LLM call", entries
        return configured, None, True

    async def _cycle(
        self, now: datetime, day: DayStats, universe: list[Instrument], manual: bool
    ) -> DecisionBatch:
        assert self._account is not None
        data = await self._fetch(universe, now)
        self._note_data_warning()
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
            account=self._with_day_trades(self._account, now),
            positions=positions,
            candidates=actionable,
            day=day.model_copy(),
            loss_baseline=self._halt_eval_day(day).start_equity,
            commission_plan=self.config.commission_plan,
            limits=self.risk.effective_limits(),
            recent_trades=self.db.list_trades(limit=10),
            market=market,
            stock_market_open=stock_market_open(now),
            data_delayed=self.broker.data_delayed,
        )
        maker, why, entries_ok = self._pick_maker(ctx)
        try:
            batch = await maker.decide(ctx)
        except Exception as exc:
            log.exception("decision maker %s failed", maker.name)
            batch = DecisionBatch(maker=maker.name, error=f"{type(exc).__name__}: {exc}")
        batch.ts = now
        batch.candidates = actionable
        if not entries_ok:
            batch.decisions = [d for d in batch.decisions if d.action != TradeAction.BUY]
            why = f"{why}; consensus mode: rules manage exits only"
        if why:
            batch.market_view = f"[{why}] {batch.market_view}".strip()
        if manual:
            batch.market_view = f"[manual] {batch.market_view}".strip()
        batch.id = self.db.insert_decision(batch)
        day.llm_cost = round(day.llm_cost + batch.cost_usd, 6)
        self._save_day()
        # Orders must not be interrupted halfway (e.g. by stop()): shield the execution.
        outcomes, holds = await asyncio.shield(self._execute(batch, ctx, now))

        consensus = batch.maker == "consensus"
        label = "rules" if maker is self.rules else "consensus" if consensus else "AI"
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
                note = holds[key]
                # Consensus HOLDs say who stood in the way ("consensus: Grok said HOLD").
                skip = note if consensus and note.startswith(CONSENSUS_PREFIX) else f"{label}: HOLD"
            elif cand.score < threshold:
                skip = f"score {cand.score:.2f} < {threshold:.2f}"
            elif batch.refused or batch.error:
                skip = f"{label}: {'refused' if batch.refused else 'error'}"
            elif not entries_ok:
                skip = "consensus mode: no entries without both models"
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
        self, batch: DecisionBatch, ctx: DecisionContext, now: datetime
    ) -> tuple[dict[str, _BuyOutcome], dict[str, str]]:
        outcomes: dict[str, _BuyOutcome] = {}
        holds: dict[str, str] = {}  # instrument -> the HOLD's rationale
        seen: set[str] = set()
        placed: list[OrderRecord] = []  # entries sent earlier in this batch
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
                        decision, inst, batch, ctx, placed, now
                    )
                elif decision.action == TradeAction.CLOSE:
                    await self._handle_close(decision, inst, batch, ctx, now)
                else:
                    holds[inst.key] = decision.rationale
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
        placed: list[OrderRecord],
        now: datetime,
    ) -> _BuyOutcome:
        """Size, risk-check and send (or queue for approval) one BUY.

        The decision maker may have thought for minutes, and meanwhile the monitor loop kept
        running: an earlier entry may have filled, the owner may have approved an order, the
        symbol may have halted or the entry window may have closed. So under the entry lock
        the clock, the quote, the account, positions and working orders are read again and
        the BUY is sized and checked on that fresh state, never on the context the models
        saw (`now` is when that context was built)."""
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
        stop = decision.stop_loss or cand.suggested_stop
        target = decision.take_profit or cand.suggested_target
        async with self._entry_lock:
            send_now = self._now()
            age = send_now - now
            if age > DECISION_MAX_AGE:
                log.warning("BUY %s dropped: decided on data %.0fs old", key, age.total_seconds())
                return _BuyOutcome(
                    cand.setup,
                    False,
                    skip_reason=f"decision too old ({age.total_seconds():.0f}s after its data)",
                )
            quote = await self.broker.quote(inst)
            self._track_halt(key, quote, send_now)
            account = self._with_day_trades(await self.broker.account(), send_now)
            positions = await self.broker.positions()
            open_orders = self._merge_orders(await self.broker.open_orders(), placed)
            day = self._today(send_now)
            entry = decision.limit_price or quote.ask or quote.last
            if not entry or not math.isfinite(entry) or entry <= 0:
                return _BuyOutcome(cand.setup, False, skip_reason="no entry price")
            qty, notes = self.risk.size_entry(
                decision=decision,
                entry=entry,
                stop=stop,
                account=account,
                positions=positions,
                day=day,
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
                now=send_now,
                settings=self.settings,
                last_halt_at=self._last_halt_at.get(key),
            )
            reasons = list(verdict.reasons) if not verdict.approved else []
            if self._halt_pending is not None:
                reasons.append(f"halt condition being confirmed: {self._halt_pending}")
            if not reasons and self.db.get_position_meta(key) is not None:
                # Metadata is written before every entry is sent and removed only once the
                # position is flat: an entry is in flight or the position is still held.
                reasons.append(f"an entry for {inst.symbol} is in flight or still held")
            if reasons:
                message = "; ".join(reasons)
                order_id = self._journal_order(
                    OrderRecord(request=req, status=OrderStatus.BLOCKED, message=message), send_now
                )
                log.info("BUY %s blocked: %s", key, message)
                return _BuyOutcome(cand.setup, False, order_id, skip_reason=f"risk: {reasons[0]}")
            order = verdict.order or req
            sizing = "; ".join([*notes, *verdict.reasons])
            if self.shadow:
                order_id = self._journal_order(
                    OrderRecord(
                        request=order,
                        status=OrderStatus.BLOCKED,
                        message=f"shadow mode: live trading is not armed ({sizing})",
                    ),
                    send_now,
                )
                return _BuyOutcome(cand.setup, False, order_id, skip_reason="shadow mode")
            if not self.settings.autopilot:
                order_id = self._journal_order(
                    OrderRecord(
                        request=order,
                        status=OrderStatus.PENDING_APPROVAL,
                        message=f"waiting for approval, expires in 10 min ({sizing})",
                    ),
                    send_now,
                )
                await self._notify(
                    f"Approve BUY {inst.symbol}?",
                    f"{order.qty:g} @ {entry:g}, stop {stop:g}, target {target:g}. "
                    "Expires in 10 minutes.",
                    tags=["question"],
                )
                return _BuyOutcome(cand.setup, True, order_id)
            sent = await self._send_entry(order, send_now)
            order_id = self._journal_order(
                sent.model_copy(update={"message": _short(f"{sizing}; {sent.message}")}), send_now
            )
            if sent.status not in _ACCEPTED:
                return _BuyOutcome(
                    cand.setup, False, order_id, skip_reason=f"broker rejected: {sent.message}"
                )
            placed.append(
                sent.model_copy(update={"status": OrderStatus.SUBMITTED, "filled_qty": 0.0})
            )
            return _BuyOutcome(cand.setup, True, order_id)

    @staticmethod
    def _merge_orders(fresh: list[OrderRecord], placed: list[OrderRecord]) -> list[OrderRecord]:
        """The broker's working orders plus entries sent earlier in this batch that it does
        not list (yet), without counting one order twice."""
        known = {o.broker_order_id for o in fresh if o.broker_order_id}
        extra = [o for o in placed if not o.broker_order_id or o.broker_order_id not in known]
        return fresh + extra

    async def _send_entry(self, order: OrderRequest, now: datetime) -> OrderRecord:
        """Place an entry. Position metadata is written first, so a position that appears
        is always managed even if the call fails halfway (stale metadata is cleaned up)."""
        order = self._traced(order, None)
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
        if self.db.get_position_meta(inst.key) is None:
            # The engine never manages positions it did not open on this account (the
            # owner's own holdings, or positions forgotten after an account change). The
            # owner can still close them at the broker.
            log.warning("CLOSE %s refused: the bot did not open this position", inst.key)
            self._journal_order(
                OrderRecord(
                    request=OrderRequest(
                        instrument=inst,
                        side=Side.SELL,
                        qty=pos.qty,
                        order_type=OrderType.MARKET,
                        is_exit=True,
                        reason=_short(f"AI close: {decision.rationale}"),
                        decision_id=batch.id,
                    ),
                    status=OrderStatus.BLOCKED,
                    message="not opened by the bot: the engine never closes it",
                ),
                now,
            )
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
        # Shadow mode still closes positions the bot opened: exits only reduce risk.
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
            if rec.request.trace_id:
                req = req.model_copy(update={"trace_id": rec.request.trace_id})
            self._journal_order(rec.model_copy(update={"request": req}), now)


_ACCEPTED = frozenset({OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED})


def build_engine(config: AppConfig, db: Database) -> Engine:
    """Engine wired from config: SimBroker or IBKRBroker, plus every decision maker the
    configured API keys allow: Claude (Anthropic key), Grok (xAI key), consensus (both),
    each with the rule-based maker as its outage fallback, and the rule-based maker itself.
    settings.decision_mode and settings.llm_enabled are read every cycle, so the owner can
    switch modes without a restart; a mode whose key is missing falls back with a warning
    in the status (see Engine._resolve_decider)."""
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
    makers: dict[str, DecisionMaker] = {"rules": rules}
    anthropic_key = config.anthropic_api_key.get_secret_value() if config.anthropic_api_key else ""
    if anthropic_key.strip():
        from trader.strategy.claude import ClaudeDecisionMaker

        makers["claude"] = ClaudeDecisionMaker(
            api_key=anthropic_key,
            model=settings.llm_model,
            effort=settings.llm_effort,
            fallback=rules,
        )
    xai_key = config.xai_api_key.get_secret_value() if config.xai_api_key else ""
    if xai_key.strip():
        from trader.strategy.grok import GrokDecisionMaker, GrokPrice

        makers["grok"] = GrokDecisionMaker(
            api_key=xai_key,
            model=config.xai_model,
            fallback=rules,
            base_url=config.xai_base_url,
            price=GrokPrice(
                input=config.xai_price_input,
                cached_input=config.xai_price_cached_input,
                output=config.xai_price_output,
            ),
        )
    if "claude" in makers and "grok" in makers:
        from trader.strategy.consensus import ConsensusDecisionMaker

        makers["consensus"] = ConsensusDecisionMaker(makers["claude"], makers["grok"])
    engine = Engine(config, db, broker, rules, notifier=Notifier(config), makers=makers)
    _, warning = engine._resolve_decider()
    if warning:
        log.warning("decision mode: %s", warning)
    return engine
