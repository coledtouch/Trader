"""Shared domain types used by the broker, strategy, risk, engine and web layers.

Every module codes against these types, so change them deliberately and update
docs/SPEC.md at the same time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Mode(StrEnum):
    SIM = "sim"  # offline simulator: fake money, synthetic prices
    PAPER = "paper"  # IBKR paper account (account id starts with "DU")
    LIVE = "live"  # IBKR live account: real money


class AssetClass(StrEnum):
    STOCK = "STK"
    CRYPTO = "CRYPTO"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MKT"
    LIMIT = "LMT"


class OrderStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"  # waiting for the owner to approve in the UI
    SUBMITTED = "submitted"  # accepted by the broker, working
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"  # rejected by the broker
    BLOCKED = "blocked"  # stopped locally (risk manager, shadow mode, halt); never sent


OPEN_ORDER_STATUSES = {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}


class Instrument(BaseModel):
    symbol: str
    asset_class: AssetClass = AssetClass.STOCK

    @property
    def key(self) -> str:
        """Stable string key, e.g. "STK:AAPL" or "CRYPTO:BTC"."""
        return f"{self.asset_class.value}:{self.symbol}"

    @classmethod
    def from_key(cls, key: str) -> Instrument:
        asset_class, symbol = key.split(":", 1)
        return cls(symbol=symbol, asset_class=AssetClass(asset_class))


class Bar(BaseModel):
    ts: datetime  # bar open time, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Quote(BaseModel):
    instrument: Instrument
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    ts: datetime = Field(default_factory=utcnow)
    delayed: bool = False  # True when the data is the free 15-minute-delayed feed

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return None

    @property
    def price(self) -> float | None:
        """Best available reference price: last trade, else mid."""
        if self.last and self.last > 0:
            return self.last
        return self.mid


class Position(BaseModel):
    instrument: Instrument
    qty: float  # positive = long. Shorts are never opened by this app.
    avg_cost: float
    market_price: float | None = None
    unrealized_pnl: float = 0.0
    # Protective levels managed by the engine (from the positions_meta table).
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: datetime | None = None
    setup: str | None = None


class AccountSnapshot(BaseModel):
    account_id: str
    mode: Mode
    equity: float  # net liquidation value
    cash: float  # total cash
    settled_cash: float  # cash usable without good-faith violations in a cash account
    buying_power: float
    realized_pnl_today: float = 0.0
    unrealized_pnl: float = 0.0
    # IBKR "DayTradesRemaining" for sub-$25k margin accounts under the old PDT rule.
    # None = unknown or unlimited. Stock entries are blocked when this is known and < 1.
    day_trades_remaining: int | None = None
    currency: str = "USD"
    ts: datetime = Field(default_factory=utcnow)


class OrderRequest(BaseModel):
    instrument: Instrument
    side: Side
    qty: float = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    stop_loss: float | None = None  # required for entries (enforced by RiskManager)
    take_profit: float | None = None
    is_exit: bool = False  # True when closing or reducing an existing position
    reason: str = ""  # short human-readable reason (from the AI or a rule)
    decision_id: int | None = None  # journal link to the AI decision batch
    setup: str | None = None  # scanner setup name, if any


class OrderRecord(BaseModel):
    id: int | None = None  # local journal id
    request: OrderRequest
    status: OrderStatus
    broker_order_id: str | None = None
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    commission: float = 0.0
    message: str = ""  # rejection reason, risk-check notes, broker messages
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Fill(BaseModel):
    exec_id: str  # unique per execution at the broker
    broker_order_id: str
    instrument: Instrument
    side: Side
    qty: float
    price: float
    commission: float = 0.0
    ts: datetime = Field(default_factory=utcnow)


class Candidate(BaseModel):
    """A setup found by the rule-based scanner. The AI may act on it or ignore it."""

    instrument: Instrument
    setup: str  # e.g. "orb_breakout", "vwap_reclaim", "rsi_reversion", "trend_pullback"
    direction: Literal["long"] = "long"
    score: float = Field(ge=0, le=1)
    price: float
    suggested_stop: float
    suggested_target: float
    features: dict[str, float] = Field(default_factory=dict)
    note: str = ""


class TradeAction(StrEnum):
    BUY = "BUY"  # open a new long position
    CLOSE = "CLOSE"  # close an existing position fully
    HOLD = "HOLD"  # do nothing (explicitly logged so the reasoning is visible)


class AIDecision(BaseModel):
    action: TradeAction
    symbol: str
    asset_class: AssetClass
    conviction: float = Field(ge=0, le=1)
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    # Fraction of the maximum size the risk manager would allow (0..1). The AI can
    # only scale size DOWN from the risk-based maximum, never up.
    size_fraction: float = Field(default=1.0, ge=0, le=1)
    setup: str | None = None
    rationale: str


class DecisionBatch(BaseModel):
    id: int | None = None
    ts: datetime = Field(default_factory=utcnow)
    maker: str  # "claude", "rules", ...
    model: str | None = None
    market_view: str = ""
    decisions: list[AIDecision] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    refused: bool = False
    error: str | None = None


class RiskLimits(BaseModel):
    """User-adjustable risk settings. Values are clamped to HARD_CAPS in code."""

    daily_profit_target_usd: float = Field(default=100.0, ge=0)
    stop_trading_at_target: bool = True  # lock in the day once the target is reached
    max_daily_loss_pct: float = Field(default=6.0, gt=0)  # of start-of-day equity
    max_risk_per_trade_pct: float = Field(default=2.0, gt=0)  # equity lost if the stop is hit
    max_position_pct: float = Field(default=50.0, gt=0)  # of equity in one position
    max_total_exposure_pct: float = Field(default=100.0, gt=0)  # no leverage by default
    max_open_positions: int = Field(default=2, ge=1)
    max_trades_per_day: int = Field(default=10, ge=1)  # entries per trading day
    max_order_notional_usd: float = Field(default=250.0, gt=0)
    min_price: float = Field(default=1.0, ge=0)  # skip sub-$1 penny stocks
    price_collar_pct: float = Field(default=1.0, gt=0)  # limit must be within X% of last
    min_reward_risk: float = Field(default=1.5, gt=0)  # (target-entry)/(entry-stop)
    max_fee_to_reward_pct: float = Field(default=25.0, gt=0)  # round-trip fees vs reward
    loss_streak_halve_after: int = Field(default=3, ge=1)  # halve size after N losses in a row
    llm_daily_budget_usd: float = Field(default=3.0, ge=0)
    flatten_on_halt: bool = True  # close everything when the daily loss limit trips


# Absolute ceilings enforced in code no matter what the settings say. They exist so
# that a typo, a bug, or a misbehaving model can never blow far past sane limits.
HARD_CAPS: dict[str, float] = {
    "max_daily_loss_pct": 25.0,
    "max_risk_per_trade_pct": 10.0,
    "max_position_pct": 100.0,
    "max_total_exposure_pct": 100.0,
    "max_open_positions": 10,
    "max_trades_per_day": 50,
    "price_collar_pct": 5.0,
    "llm_daily_budget_usd": 50.0,
}

# Fields where a LOWER value is riskier (loosening means decreasing them).
LOWER_IS_LOOSER = {"min_price", "min_reward_risk", "loss_streak_halve_after"}


class BotSettings(BaseModel):
    """Runtime settings, editable in the UI and stored in the database."""

    autopilot: bool = True  # False: every order waits for approval in the UI
    cycle_seconds: int = Field(default=300, ge=30, le=3600)  # AI decision cadence
    monitor_seconds: int = Field(default=15, ge=5, le=300)  # stop/target/loss-limit checks
    trade_stocks: bool = True
    trade_crypto: bool = True
    stock_universe: list[str] = Field(
        default_factory=lambda: ["SPY", "QQQ", "AAPL", "AMD", "F", "SOFI", "PLTR", "INTC"]
    )
    crypto_universe: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    bar_size: Literal["1 min", "5 mins", "15 mins"] = "5 mins"
    # New stock entries only inside this window (America/New_York, HH:MM). The open is
    # chaotic and thin names fade after the morning; see docs/STRATEGY.md.
    stock_entry_start: str = Field(default="09:45", pattern=r"^\d{2}:\d{2}$")
    stock_entry_cutoff: str = Field(default="15:30", pattern=r"^\d{2}:\d{2}$")
    flatten_stocks_before_close: bool = True  # day-trade only: flat by 15:50 ET
    min_avg_volume: float = Field(default=500_000, ge=0)  # skip illiquid names (shares/day)
    dynamic_universe: bool = False  # add IBKR scanner top movers (needs real-time data)
    allow_fractional_stocks: bool = False
    llm_enabled: bool = True
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_min_candidate_score: float = Field(default=0.5, ge=0, le=1)  # skip the LLM below this
    risk: RiskLimits = Field(default_factory=RiskLimits)


class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)  # why it was blocked or adjusted
    order: OrderRequest | None = None  # possibly adjusted (e.g. qty reduced)


class DayStats(BaseModel):
    day: str  # trading day, YYYY-MM-DD in America/New_York
    start_equity: float | None = None
    end_equity: float | None = None
    realized_pnl: float = 0.0
    trades: int = 0  # entries placed today
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0  # consecutive losing round trips (carries across days)
    fees: float = 0.0
    llm_cost: float = 0.0
    halted: bool = False
    halt_reason: str | None = None


class TradeRecord(BaseModel):
    """A completed round trip (entry and exit)."""

    id: int | None = None
    instrument: Instrument
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl: float  # after commissions
    commission: float = 0.0
    setup: str | None = None
    decision_id: int | None = None
    r_multiple: float | None = None  # pnl / initial risk
    exit_reason: str = ""


class EngineStatus(BaseModel):
    running: bool = False
    mode: Mode = Mode.SIM
    broker: str = "sim"
    connected: bool = False
    halted: bool = False
    halt_reason: str | None = None
    autopilot: bool = True
    live_armed: bool = False  # owner armed live orders (with a fresh 2FA code)
    live_allowed: bool = False  # TRADER_ALLOW_LIVE env gate
    shadow: bool = False  # live account but not armed: decisions are logged, orders blocked
    stock_market_open: bool = False
    last_cycle_at: datetime | None = None
    next_cycle_at: datetime | None = None
    last_error: str | None = None
    day: str = ""
    start_equity: float | None = None
    equity: float | None = None
    pnl_today: float = 0.0
    pnl_target: float = 0.0
    trades_today: int = 0
    llm_cost_today: float = 0.0
    data_delayed: bool = False


class DecisionContext(BaseModel):
    """Everything the decision maker sees in one cycle."""

    now: datetime
    mode: Mode
    account: AccountSnapshot
    positions: list[Position]
    candidates: list[Candidate]
    day: DayStats
    limits: RiskLimits
    recent_trades: list[TradeRecord] = Field(default_factory=list)
    # Per-instrument indicator snapshot (instrument key -> {"price": .., "rsi14": .., ...})
    market: dict[str, dict[str, float]] = Field(default_factory=dict)
    stock_market_open: bool = False
    data_delayed: bool = False
