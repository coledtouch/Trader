"""Shared domain types used by the broker, strategy, risk, engine and web layers.

Every module codes against these types, so change them deliberately and update
docs/SPEC.md at the same time.
"""

from __future__ import annotations

import re
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
    bid_size: float | None = None
    ask_size: float | None = None
    ts: datetime = Field(default_factory=utcnow)
    delayed: bool = False  # True when the data is the free 15-minute-delayed feed
    halted: bool = False  # trading halt (LULD or news); no entries while True

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

    @property
    def spread_pct(self) -> float | None:
        """Bid/ask spread as a percent of mid, or None without a two-sided quote."""
        mid = self.mid
        if mid is None or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid * 100

    @property
    def exit_price(self) -> float | None:
        """What a long position could actually be sold for now: the bid, else last/mid.
        Thin names often print far above the bid, so P&L and stops use this."""
        if self.bid and self.bid > 0:
            return self.bid
        return self.price


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
    exec_id: str  # unique per execution at the broker (see exec_revision for corrections)
    broker_order_id: str
    instrument: Instrument
    side: Side
    qty: float
    price: float
    commission: float = 0.0
    ts: datetime = Field(default_factory=utcnow)


_EXEC_REVISION = re.compile(r"^(?P<base>[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)+)\.(?P<rev>[0-9]+)$")


def exec_revision(exec_id: str) -> tuple[str, int] | None:
    """(base, revision) of an IBKR execution id, or None for other id formats.

    IBKR reports a corrected execution as a new execution whose id repeats the original
    except for a higher number after the last period ("0000e0d5.65a1b2c3.01.01" is
    corrected by "...01.02"): the journal keeps only the latest revision of each base."""
    match = _EXEC_REVISION.match(exec_id)
    return (match["base"], int(match["rev"])) if match else None


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
    maker: str  # "claude", "grok", "consensus", "rules", ...
    model: str | None = None
    market_view: str = ""
    decisions: list[AIDecision] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    refused: bool = False
    error: str | None = None
    # Consensus batches: each model's own (validated) decisions, keyed by maker name
    # ("claude", "grok"), so the journal shows how each one voted. Empty otherwise.
    votes: dict[str, list[AIDecision]] = Field(default_factory=dict)


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
    max_spread_pct: float = Field(default=0.3, gt=0)  # skip entries with wider bid/ask spreads
    halt_cooldown_minutes: int = Field(default=15, ge=0)  # no entries this long after a halt
    allow_leveraged_etfs: bool = False  # leveraged/inverse ETPs (TQQQ, SQQQ, UVXY, ...)


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
    "max_spread_pct": 2.0,
    # Size is halved after this many losses in a row; a huge value would switch the
    # anti-tilt halving off, so it can never be raised past this.
    "loss_streak_halve_after": 10,
}

# Fields where a LOWER value is riskier (loosening means decreasing them). For every other
# numeric risk field, a HIGHER value is riskier (e.g. loss_streak_halve_after: a higher N
# halves the size later, or never).
LOWER_IS_LOOSER = {
    "min_price",
    "min_reward_risk",
    "halt_cooldown_minutes",
}
# Boolean risk fields where switching to True loosens, and where switching to False loosens.
BOOL_TRUE_IS_LOOSER = {"allow_leveraged_etfs"}
BOOL_FALSE_IS_LOOSER = {"stop_trading_at_target", "flatten_on_halt"}

# Leveraged and inverse exchange-traded products. Blocked unless allow_leveraged_etfs is True.
# Not exhaustive: the IBKR broker also refuses anything that is not a plain stock/ETF or
# crypto contract, and this list catches the common ones by symbol.
LEVERAGED_ETFS = frozenset(
    {
        "TQQQ", "SQQQ", "QLD", "QID", "SPXL", "SPXS", "SPXU", "UPRO", "SSO", "SDS", "SH",
        "SOXL", "SOXS", "TNA", "TZA", "UDOW", "SDOW", "DDM", "DXD", "LABU", "LABD",
        "FAS", "FAZ", "TECL", "TECS", "FNGU", "FNGD", "BULZ", "BERZ", "NUGT", "DUST",
        "JNUG", "JDST", "GUSH", "DRIP", "ERX", "ERY", "YINN", "YANG", "TMF", "TMV", "TBT",
        "UVXY", "SVXY", "UVIX", "SVIX", "VIXY", "VXX", "TSLL", "TSLQ", "TSLZ", "NVDL",
        "NVDU", "NVDD", "NVDQ", "AAPU", "AAPD", "MSFU", "AMZU", "GGLL", "CONL", "MSTU",
        "MSTX", "MSTZ", "BITX", "BITU", "SBIT", "ETHU", "SPYQ", "QQQU", "WEBL", "WEBS",
        "DPST", "CURE", "BOIL", "KOLD", "UCO", "SCO", "AGQ", "ZSL", "USD", "SSG",
    }
)  # fmt: skip


class ExitRules(BaseModel):
    """Engine-managed exits on top of the stop-loss and target placed with every entry."""

    # Time stop: close a position that has not reached time_stop_min_r after N minutes.
    # 0 disables. Stale trades tie up the account's small capital.
    time_stop_minutes: int = Field(default=45, ge=0, le=1440)
    time_stop_min_r: float = Field(default=0.5, ge=0)
    # Move the stop to the entry price once the trade is up this many R. 0 disables.
    breakeven_at_r: float = Field(default=1.0, ge=0)
    # After breakeven, trail the stop this many ATRs below the best price. 0 disables.
    trail_atr_mult: float = Field(default=0.0, ge=0)


class BotSettings(BaseModel):
    """Runtime settings, editable in the UI and stored in the database."""

    autopilot: bool = True  # False: every order waits for approval in the UI
    cycle_seconds: int = Field(default=300, ge=30, le=3600)  # AI decision cadence
    monitor_seconds: int = Field(default=15, ge=5, le=300)  # stop/target/loss-limit checks
    trade_stocks: bool = True
    # Off by default: at $100-250 per trade the ~1.4% round-trip crypto fee fails the
    # fee-vs-reward check almost every time, while 24/7 candidates would still cost LLM calls.
    trade_crypto: bool = False
    # Liquid, mostly lower-priced names: with ~$500 equity and a 50% position cap a single
    # share must cost < ~$250 (whole shares). The engine skips anything unaffordable.
    stock_universe: list[str] = Field(
        default_factory=lambda: ["F", "SOFI", "INTC", "T", "PFE", "AAL", "SNAP", "RIVN", "NU"]
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
    llm_enabled: bool = True  # master switch: off means rules-only decisions
    # Who decides. "consensus" (default): a BUY goes out only when Claude AND Grok both
    # BUY the same symbol; either one can CLOSE. "claude" / "grok": that model alone.
    # "rules": the deterministic rule-based maker. A mode whose API key is missing falls
    # back (consensus -> the model that has a key; a single model -> rules) and the
    # dashboard shows a warning.
    decision_mode: Literal["consensus", "claude", "grok", "rules"] = "consensus"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_min_candidate_score: float = Field(default=0.5, ge=0, le=1)  # skip the LLM below this
    risk: RiskLimits = Field(default_factory=RiskLimits)
    exits: ExitRules = Field(default_factory=ExitRules)


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
    # How far today's P&L (equity - start_equity) may fall before the daily loss limit halts
    # trading, as a positive amount. Differs from start_equity x limit after a resume,
    # because halts are then measured from the equity at the resume.
    loss_limit_usd: float | None = None
    trades_today: int = 0
    llm_cost_today: float = 0.0
    data_delayed: bool = False
    # Who makes decisions right now ("consensus", "claude", "grok" or "rules"), and why it
    # differs from the configured decision_mode (e.g. an API key is missing), if it does.
    decision_maker: str = "rules"
    decision_warning: str | None = None
    # Broker market-data problem worth the owner's attention, e.g. IBKR error 10197 (no
    # data while another session of the same login holds it). Affected symbols get no entries.
    data_warning: str | None = None


class DecisionContext(BaseModel):
    """Everything the decision maker sees in one cycle."""

    now: datetime
    mode: Mode
    account: AccountSnapshot
    positions: list[Position]
    candidates: list[Candidate]
    day: DayStats
    # Equity the daily loss limit is measured from: the day's start equity, or the equity
    # when the owner last resumed trading today. None = day.start_equity.
    loss_baseline: float | None = None
    # IBKR pricing plan, for the per-candidate fee estimates in the prompt.
    commission_plan: Literal["tiered", "fixed"] = "tiered"
    limits: RiskLimits
    recent_trades: list[TradeRecord] = Field(default_factory=list)
    # Per-instrument indicator snapshot (instrument key -> {"price": .., "rsi14": .., ...})
    market: dict[str, dict[str, float]] = Field(default_factory=dict)
    stock_market_open: bool = False
    data_delayed: bool = False


class SignalRecord(BaseModel):
    """One qualifying scanner signal, logged whether or not it was traded.

    This is the forward-test ledger: write the rule down, log every signal that
    qualifies (including the ones the bot skipped), and score them afterwards. A month
    of this says far more than one great day.
    """

    id: int | None = None
    ts: datetime = Field(default_factory=utcnow)
    instrument: Instrument
    setup: str
    score: float
    price: float  # reference entry price when the signal fired
    stop: float
    target: float
    taken: bool = False
    skip_reason: str | None = None  # why it was not traded (AI held, risk block, ...)
    decision_id: int | None = None
    order_id: int | None = None
    # Outcomes in R multiples (1R = price - stop), filled in by the engine later.
    r_15m: float | None = None
    r_30m: float | None = None
    r_60m: float | None = None
    r_close: float | None = None  # at the session close (stocks) or +24h (crypto)
    mfe_r: float | None = None  # max favorable excursion
    mae_r: float | None = None  # max adverse excursion
    first_hit: Literal["target", "stop", "neither"] | None = None
    evaluated: bool = False


class SetupStats(BaseModel):
    setup: str
    signals: int
    taken: int
    evaluated: int
    target_first_pct: float | None = None  # of evaluated signals
    avg_r_close: float | None = None
    avg_mfe_r: float | None = None
    avg_mae_r: float | None = None
    enough_data: bool = False  # False below 30 evaluated signals: too few to judge
