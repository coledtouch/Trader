"""Shared domain types used by the broker, strategy, risk, engine and web layers.

Every module codes against these types, so change them deliberately and update
docs/SPEC.md at the same time.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


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
    # Only read back from the broker, never sent: a type this app does not place (an IBKR
    # MIT, LIT, LOC, MOC, REL or trailing order from TWS or IBKR Mobile). Never a market
    # exit or a limit; a protective stop only when it carries stop_loss.
    OTHER = "OTHER"


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
    # Broker order tag (IBKR orderRef). "ai-trader-<journal id>" when that id is known
    # before the send, otherwise a unique "ai-trader-<random>" stored with the order so
    # it can be matched again after a restart. The crypto watchdog sets its own
    # "ai-trader-wd-..." tag. Empty until the engine, broker, or watchdog stamps it.
    trace_id: str | None = None


def order_trace_id(order_id: int | None = None, existing: str | None = None) -> str:
    """Stable broker tag for one order. Keeps `existing` when it is set, otherwise the
    journal id, otherwise a random id. Never the same tag for every order."""
    if existing and existing.strip():
        return existing.strip()
    if order_id is not None:
        return f"ai-trader-{order_id}"
    return f"ai-trader-{uuid4().hex}"


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
    # Preserve individual call accounting for paired, offline selection comparisons.
    # Missing keys on older rows mean unknown, never a free model call.
    vote_costs: dict[str, float] = Field(default_factory=dict)
    vote_failures: dict[str, str] = Field(default_factory=dict)


# Old built-in dollar target. Saved settings that name some other dollar amount and no
# percent keep that amount. The untouched default of exactly $100 is migrated to the
# percent below. The owner chose no default target (2026-10-23): 0 means off, and only the
# daily loss limit stops new entries. A target never adds risk.
DEFAULT_DAILY_PROFIT_TARGET_PCT = 0.0
LEGACY_DEFAULT_PROFIT_TARGET_USD = 100.0


class RiskLimits(BaseModel):
    """User-adjustable risk settings. Values are clamped to HARD_CAPS in code."""

    # Percent of start-of-day equity. None only for a saved config that still uses a
    # fixed dollar target (daily_profit_target_usd).
    daily_profit_target_pct: float | None = Field(default=DEFAULT_DAILY_PROFIT_TARGET_PCT, ge=0)
    # Fixed dollar target. None means "use the percent". Set (and the percent left empty)
    # only so a config saved before the percent target still halts at the same dollar amount.
    daily_profit_target_usd: float | None = Field(default=None, ge=0)
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
    # PAPER only: allow stock entries on delayed (~15 min) market data. Off by default:
    # such fills happen at current prices on decisions made from stale ones, so the
    # trades test the plumbing (brackets, stop moves) and are kept out of the edge
    # statistics (TradeRecord.delayed_data). LIVE always blocks entries on delayed data.
    allow_delayed_paper_entries: bool = False

    @model_validator(mode="before")
    @classmethod
    def _keep_legacy_dollar_target(cls, data: Any) -> Any:
        """A config saved before the percent target has daily_profit_target_usd and no
        percent. Keep that dollar amount. A new config (no dollar key, or a null one)
        uses the percent default."""
        if not isinstance(data, dict) or "daily_profit_target_pct" in data:
            return data
        usd = data.get("daily_profit_target_usd")
        if usd is None:
            return data
        return {**data, "daily_profit_target_pct": None}

    @property
    def percent_target(self) -> bool:
        """True when a positive percent sets the daily target. A percent of 0 (the
        default, off) or an empty one leaves the dollar amount in charge, so a dollar
        target the owner types in is never silently ignored."""
        pct = self.daily_profit_target_pct
        return pct is not None and pct > 0

    def profit_target_usd(self, start_equity: float | None) -> float:
        """Dollar profit target for the day, 0 for none.

        A positive percent is of start-of-day equity (0 while that equity is not known
        yet). Otherwise the saved dollar amount applies: a legacy config, or one where the
        owner set dollars and left the percent at 0 or empty.
        """
        if not self.percent_target:
            return float(self.daily_profit_target_usd or 0.0)
        if not start_equity or start_equity <= 0:
            return 0.0
        return start_equity * self.daily_profit_target_pct / 100.0


def migrate_legacy_profit_target(raw: str) -> str | None:
    """Rewrite a saved settings blob that still has the untouched $100 default.

    Settings saved before the percent target have `daily_profit_target_usd` and no
    percent key. Exactly 100 was the built-in default, not a choice, so it becomes
    the default percent (0, off). Any other dollar amount is left alone, and a blob that
    already has the percent key (including an explicit null) is left alone so a
    later choice of $100 stays a dollar target. Returns the new JSON, or None when
    nothing changes.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    risk = data.get("risk")
    if not isinstance(risk, dict) or "daily_profit_target_pct" in risk:
        return None
    usd = risk.get("daily_profit_target_usd")
    if (
        isinstance(usd, bool)
        or not isinstance(usd, int | float)
        or float(usd) != (LEGACY_DEFAULT_PROFIT_TARGET_USD)
    ):
        return None
    updated = {
        **data,
        "risk": {
            **risk,
            "daily_profit_target_pct": DEFAULT_DAILY_PROFIT_TARGET_PCT,
            "daily_profit_target_usd": None,
        },
    }
    return BotSettings.model_validate(updated).model_dump_json()


# Absolute ceilings enforced in code no matter what the settings say. They exist so
# that a typo, a bug, or a misbehaving model can never blow far past sane limits.
HARD_CAPS: dict[str, float] = {
    "max_daily_loss_pct": 25.0,
    "max_risk_per_trade_pct": 10.0,
    "max_position_pct": 100.0,
    "max_total_exposure_pct": 100.0,
    "max_open_positions": 10,
    "max_trades_per_day": 50,
    # Largest single order, in dollars. A typo or a loosened setting cannot send more.
    "max_order_notional_usd": 5000.0,
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
BOOL_TRUE_IS_LOOSER = {"allow_leveraged_etfs", "allow_delayed_paper_entries"}
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


# Live quotes streamed at once (IBKR's default is 100 market data lines for the account; the
# rest is left for the crypto watchdog and paper-test), and the scanner's top movers added
# to the stock universe each round when dynamic_universe is on. Settings keep the
# universes within the streams (trader/web/app.py validate_settings).
MAX_QUOTE_STREAMS = 95
SCANNER_MOVERS = 10
# Bar history the engine requests per bar size; the backtest shows the scanner as much.
BAR_LOOKBACK = {"1 min": "2 D", "5 mins": "5 D", "15 mins": "10 D"}


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
    # With the trailing stop on, new entries get no take-profit order: the trailing stop
    # decides when a winner is sold. Trades already open keep their target.
    let_winners_run: bool = False

    @property
    def no_target(self) -> bool:
        """Whether new entries go out without a take-profit order (see let_winners_run)."""
        return self.let_winners_run and self.trail_atr_mult > 0


SetupName = Literal[
    "orb_breakout", "vwap_reclaim", "trend_pullback", "rsi_reversion", "volume_spike"
]
SETUP_NAMES: tuple[SetupName, ...] = (
    "orb_breakout",
    "vwap_reclaim",
    "trend_pullback",
    "rsi_reversion",
    "volume_spike",
)


class OperatingCosts(BaseModel):
    """Owner-supplied monthly expense estimates; None means not configured, not free."""

    hosting_monthly_usd: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)
    data_monthly_usd: float | None = Field(default=None, ge=0, le=100_000, allow_inf_nan=False)


class BotSettings(BaseModel):
    """Runtime settings, editable in the UI and stored in the database."""

    autopilot: bool = False  # True: orders go out on their own. Off: each one waits for approval
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
    enabled_setups: list[SetupName] = Field(default_factory=lambda: list(SETUP_NAMES), max_length=5)
    operating_costs: OperatingCosts = Field(default_factory=OperatingCosts)
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
    # Entered on delayed market data (paper only, see RiskLimits): not evidence of an edge.
    delayed_data: bool = False


class QuoteHealth(BaseModel):
    symbol: str
    age_seconds: float | None = None
    delayed: bool = False
    has_price: bool = True
    delay_blocks_entry: bool = False
    valid: bool = False
    problem: str | None = None  # why the bot would not enter on it (None when usable)
    # Blocked by the market (a halt or a wide spread), not by a problem with the data.
    market_block: bool = False


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
    pnl_today: float | None = None  # None while the equity (or the day's start) is unknown
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
    quote_health: list[QuoteHealth] = Field(default_factory=list)
    # PAPER with RiskLimits.allow_delayed_paper_entries on: stock entries on delayed data
    # are allowed (the dashboard must not call them blocked).
    delayed_paper_entries: bool = False


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
