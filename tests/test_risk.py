from datetime import UTC, datetime, timedelta

import pytest

from trader.broker.base import Broker
from trader.models import (
    HARD_CAPS,
    AccountSnapshot,
    AIDecision,
    AssetClass,
    BotSettings,
    DayStats,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    RiskLimits,
    Side,
    TradeAction,
)
from trader.risk import (
    HALT_DAILY_LOSS,
    HALT_TARGET,
    RiskManager,
    available_cash,
    clamp_limits,
    is_soft_halt,
    quote_problem,
    sane_exit_price,
)

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # Wednesday 11:00 New York
F = Instrument(symbol="F")
BTC = Instrument(symbol="BTC", asset_class=AssetClass.CRYPTO)


class FeeBroker:
    """Only what the risk manager uses: quantity rounding and fee estimates."""

    def __init__(self, fee: float = 0.35):
        self.fee = fee
        self.fee_calls: list[tuple[str, float, float]] = []
        self.fee_sides: list[Side] = []

    def round_qty(self, instrument: Instrument, qty: float, allow_fractional: bool = False):
        return Broker.round_qty(self, instrument, qty, allow_fractional)  # type: ignore[arg-type]

    def estimate_commission(
        self, instrument: Instrument, qty: float, price: float, side: Side = Side.BUY
    ) -> float:
        self.fee_calls.append((instrument.key, qty, price))
        self.fee_sides.append(side)
        return self.fee


def account(equity: float = 500.0, cash: float | None = None, **kw) -> AccountSnapshot:
    cash = equity if cash is None else cash
    return AccountSnapshot(
        account_id="T",
        mode=Mode.PAPER,
        equity=equity,
        cash=cash,
        settled_cash=cash,
        buying_power=cash,
        **kw,
    )


def day(**kw) -> DayStats:
    return DayStats(day="2026-10-21", **{"start_equity": 500.0, **kw})


def quote(inst: Instrument = F, bid=9.99, ask=10.01, last=10.0, ts=NOW, **kw) -> Quote:
    return Quote(instrument=inst, bid=bid, ask=ask, last=last, ts=ts, **kw)


def entry(inst: Instrument = F, qty=10.0, limit=10.0, stop=9.8, target=10.5, **kw) -> OrderRequest:
    return OrderRequest(
        instrument=inst,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.LIMIT,
        limit_price=limit,
        stop_loss=stop,
        take_profit=target,
        **kw,
    )


def pos(symbol: str, qty: float = 10.0, price: float = 10.0) -> Position:
    return Position(
        instrument=Instrument(symbol=symbol), qty=qty, avg_cost=price, market_price=price
    )


def decision(symbol: str = "F", fraction: float = 1.0) -> AIDecision:
    return AIDecision(
        action=TradeAction.BUY,
        symbol=symbol,
        asset_class=AssetClass.STOCK,
        conviction=0.8,
        size_fraction=fraction,
        rationale="test",
    )


def run_check(
    req: OrderRequest | None = None,
    *,
    limits: RiskLimits | None = None,
    q: Quote | None | str = "default",
    acct: AccountSnapshot | None = None,
    positions: list[Position] | None = None,
    open_orders: list[OrderRecord] | None = None,
    stats: DayStats | None = None,
    mode: Mode = Mode.PAPER,
    delayed: bool = False,
    broker: FeeBroker | None = None,
    now: datetime = NOW,
    settings: BotSettings | None = None,
    last_halt_at: datetime | None = None,
):
    rm = RiskManager(limits or RiskLimits())
    return rm.check(
        req or entry(),
        quote=quote() if q == "default" else q,  # type: ignore[arg-type]
        account=acct or account(),
        positions=positions or [],
        open_orders=open_orders or [],
        day=stats or day(),
        mode=mode,
        data_delayed=delayed,
        broker=broker or FeeBroker(),  # type: ignore[arg-type]
        now=now,
        settings=settings or BotSettings(trade_crypto=True),
        last_halt_at=last_halt_at,
    )


def blocked_with(verdict, fragment: str) -> bool:
    return not verdict.approved and any(fragment in r for r in verdict.reasons)


# ---------------------------------------------------------------------- limits
def test_limits_are_clamped_to_hard_caps() -> None:
    loose = RiskLimits(
        max_risk_per_trade_pct=50,
        max_daily_loss_pct=90,
        max_open_positions=99,
        max_trades_per_day=500,
        max_spread_pct=9,
        llm_daily_budget_usd=1000,
        price_collar_pct=20,
    )
    eff = RiskManager(loose).effective_limits()
    for name, cap in HARD_CAPS.items():
        assert getattr(eff, name) <= cap
    assert eff.max_open_positions == 10 and isinstance(eff.max_open_positions, int)
    assert eff.max_risk_per_trade_pct == 10.0
    assert clamp_limits(RiskLimits()) == RiskLimits()  # defaults are within the caps


def test_set_limits_clamps_again() -> None:
    rm = RiskManager(RiskLimits())
    rm.set_limits(RiskLimits(max_spread_pct=5))
    assert rm.effective_limits().max_spread_pct == HARD_CAPS["max_spread_pct"]


def test_available_cash_never_borrows() -> None:
    acct = AccountSnapshot(
        account_id="T", mode=Mode.PAPER, equity=500, cash=300, settled_cash=200, buying_power=2000
    )
    assert available_cash(acct) == 300


# ---------------------------------------------------------------------- sizing
def size(
    *,
    entry_px=10.0,
    stop=9.0,
    acct=None,
    stats=None,
    limits=None,
    fraction=1.0,
    positions=None,
    broker=None,
    symbol="F",
):
    rm = RiskManager(limits or RiskLimits())
    return rm.size_entry(
        decision=decision(symbol, fraction),
        entry=entry_px,
        stop=stop,
        account=acct or account(),
        positions=positions or [],
        day=stats or day(),
        broker=broker or FeeBroker(),
        allow_fractional=False,
    )


def test_sizing_risk_based() -> None:
    qty, notes = size()  # $10 risk (2% of 500) / $1 per share
    assert qty == 10
    assert "risk $10.00" in notes[-1]


def test_sizing_capped_by_order_notional() -> None:
    # risk allows 100 shares, 50% of $1000 equity 50 shares, the $250 order cap 25
    qty, notes = size(stop=9.9, acct=account(equity=1000))
    assert qty == 25
    assert "max order" in notes[-1]


def test_sizing_size_fraction_only_scales_down() -> None:
    assert size(fraction=0.5)[0] == 5
    with pytest.raises(ValueError):
        decision(fraction=1.5)  # the model cannot ask for more than the maximum


def test_sizing_halves_after_loss_streak() -> None:
    qty, notes = size(stats=day(loss_streak=3))
    assert qty == 5
    assert any("halved" in n for n in notes)
    assert size(stats=day(loss_streak=2))[0] == 10


def test_loss_streak_halving_cannot_be_switched_off() -> None:
    """A huge loss_streak_halve_after would disable the anti-tilt halving: the hard cap
    keeps it at most 10 losses in a row."""
    never = RiskLimits(loss_streak_halve_after=1_000_000)
    assert RiskManager(never).effective_limits().loss_streak_halve_after == 10
    qty, notes = size(stats=day(loss_streak=10), limits=never)
    assert qty == 5 and any("halved" in n for n in notes)


def test_max_order_notional_is_hard_capped() -> None:
    """A single order cannot be raised past $5,000, whatever the settings say."""
    huge = RiskLimits(
        max_order_notional_usd=50_000, max_position_pct=100, max_risk_per_trade_pct=10
    )
    assert RiskManager(huge).effective_limits().max_order_notional_usd == 5_000
    assert HARD_CAPS["max_order_notional_usd"] == 5_000
    qty, notes = size(
        entry_px=100.0,
        stop=99.0,
        acct=account(equity=100_000, cash=100_000),
        stats=day(start_equity=100_000),
        limits=huge,
    )
    assert qty == 50  # $5,000 cap / $100, not the $50,000 the settings asked for
    assert "max order" in notes[-1]


def test_sizing_hard_cap_on_risk_per_trade() -> None:
    limits = RiskLimits(
        max_risk_per_trade_pct=50, max_order_notional_usd=10_000, max_position_pct=100
    )
    qty, _ = size(limits=limits, acct=account(equity=10_000), stats=day(start_equity=500))
    assert qty == 50  # 10% hard cap of the $500 start = $50 risk, not 50% = $250


def test_sizing_leaves_cash_for_commission() -> None:
    limits = RiskLimits(max_risk_per_trade_pct=10, max_position_pct=100)
    qty, _ = size(stop=9.9, limits=limits, acct=account(equity=500, cash=100), broker=FeeBroker(1))
    assert qty == 9  # 10 shares + $1 fee would need $101


def test_sizing_exposure_room() -> None:
    held = [pos("T", qty=40, price=10.0)]  # $400 of $500 exposure used
    qty, notes = size(stop=9.9, positions=held, acct=account(equity=500, cash=100))
    assert qty <= 10
    assert qty * 10 <= 100


def test_sizing_rounds_to_zero_when_one_share_is_too_expensive() -> None:
    qty, notes = size(entry_px=300.0, stop=290.0)
    assert qty == 0
    assert "rounds to 0" in notes[-1]


def test_sizing_invalid_levels() -> None:
    assert size(entry_px=10.0, stop=10.0)[0] == 0
    assert size(entry_px=10.0, stop=-1.0)[0] == 0


@pytest.mark.parametrize("pnl", [-60.0, -20.0, 0.0, 50.0, 99.0, 150.0, 400.0])
def test_no_martingale_size_ignores_progress_to_target(pnl: float) -> None:
    """Being far behind or far ahead of the daily target never changes the size."""
    stats = day(realized_pnl=pnl, losses=0 if pnl >= 0 else 1)
    qty_now, _ = size(stats=stats, acct=account(equity=500))
    qty_flat, _ = size(stats=day(), acct=account(equity=500))
    assert qty_now == qty_flat == 10


def test_no_martingale_behind_never_sizes_up() -> None:
    behind, _ = size(stop=9.95, acct=account(equity=460), stats=day(realized_pnl=-40))
    ahead, _ = size(stop=9.95, acct=account(equity=650), stats=day(realized_pnl=150))
    flat, _ = size(stop=9.95, acct=account(equity=500), stats=day())
    assert behind <= flat <= ahead


# ---------------------------------------------------------------------- entry checks
def test_baseline_entry_is_approved() -> None:
    verdict = run_check()
    assert verdict.approved, verdict.reasons
    assert verdict.order is not None and verdict.order.qty == 10


def test_block_halted_day() -> None:
    assert blocked_with(run_check(stats=day(halted=True, halt_reason="kill switch")), "halted")


def test_block_max_trades_per_day() -> None:
    assert blocked_with(run_check(stats=day(trades=10)), "entries today")


def test_block_max_open_positions() -> None:
    held = [pos("T", 1), pos("PFE", 1)]
    assert blocked_with(run_check(positions=held), "open positions")


def test_block_already_holding() -> None:
    assert blocked_with(run_check(positions=[pos("F", 1)]), "never add")


def test_block_entry_already_working() -> None:
    working = OrderRecord(request=entry(qty=1), status=OrderStatus.SUBMITTED, broker_order_id="1")
    assert blocked_with(run_check(open_orders=[working]), "already working")


def test_working_entries_count_toward_open_positions() -> None:
    working = [
        OrderRecord(request=entry(Instrument(symbol=s), qty=1), status=OrderStatus.SUBMITTED)
        for s in ("T", "PFE")
    ]
    assert blocked_with(run_check(open_orders=working), "open positions")


def test_block_missing_stop() -> None:
    assert blocked_with(run_check(entry(stop=None)), "no stop-loss")


def test_block_stop_at_or_above_entry() -> None:
    assert blocked_with(run_check(entry(stop=10.0)), "below the entry")


def test_block_missing_target() -> None:
    assert blocked_with(run_check(entry(target=None)), "no take-profit")


def test_block_target_not_above_entry() -> None:
    assert blocked_with(run_check(entry(target=9.9)), "must be above the entry")


def test_block_reward_risk() -> None:
    assert blocked_with(run_check(entry(target=10.2)), "reward:risk 1.00")


def test_block_min_price() -> None:
    q = quote(bid=0.899, ask=0.901, last=0.9)
    req = entry(qty=100, limit=0.9, stop=0.85, target=1.0)
    assert blocked_with(run_check(req, q=q), "minimum")


def test_block_price_collar() -> None:
    assert blocked_with(run_check(entry(limit=10.2, stop=10.0, target=10.8)), "collar")


def test_block_order_notional() -> None:
    limits = RiskLimits(max_position_pct=100)
    assert blocked_with(run_check(entry(qty=30), limits=limits), "max order")


def test_block_risk_at_stop() -> None:
    verdict = run_check(entry(qty=25, stop=9.5, target=11.0))  # $12.50 at risk > 2% of $500
    assert blocked_with(verdict, "risk at the stop $12.50")
    assert len(verdict.reasons) == 1


def test_block_position_pct() -> None:
    assert blocked_with(run_check(limits=RiskLimits(max_position_pct=10)), "% of equity")


def test_block_total_exposure() -> None:
    held = [pos("T", qty=45, price=10.0)]
    verdict = run_check(positions=held, acct=account(equity=500, cash=50))
    assert blocked_with(verdict, "exposure after fill")


def test_block_insufficient_cash() -> None:
    assert blocked_with(run_check(acct=account(equity=500, cash=50)), "insufficient cash")


def test_cash_reserved_by_working_buys() -> None:
    working = OrderRecord(
        request=entry(Instrument(symbol="T"), qty=45, limit=10, stop=9.8, target=10.5),
        status=OrderStatus.SUBMITTED,
    )
    verdict = run_check(open_orders=[working], acct=account(equity=500, cash=500))
    assert blocked_with(verdict, "insufficient cash")


def test_block_fees_vs_reward_using_both_legs() -> None:
    broker = FeeBroker(fee=1.5)  # $3 round trip vs $5 reward = 60%
    assert blocked_with(run_check(broker=broker), "round-trip fees")
    calls = zip(broker.fee_calls, broker.fee_sides, strict=True)
    legs = {price: side for (_, _, price), side in calls}
    # Buy at the entry, sell at the target (the sell leg carries the regulatory fees).
    assert legs[10.0] is Side.BUY and legs[10.5] is Side.SELL


def test_block_outside_entry_window() -> None:
    early = datetime(2026, 10, 21, 13, 35, tzinfo=UTC)  # 09:35 New York
    q = quote(ts=early)
    assert blocked_with(run_check(now=early, q=q), "entry window")


def test_block_market_closed() -> None:
    saturday = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    assert blocked_with(run_check(now=saturday, q=quote(ts=saturday)), "market is closed")


def test_block_live_with_delayed_data_for_stocks() -> None:
    assert blocked_with(run_check(mode=Mode.LIVE, delayed=True), "delayed market data")
    assert blocked_with(run_check(mode=Mode.LIVE, q=quote(delayed=True)), "delayed market data")
    assert run_check(mode=Mode.PAPER, delayed=True).approved


def test_crypto_allowed_live_with_delayed_data() -> None:
    q = quote(BTC, bid=99.99, ask=100.01, last=100.0)
    req = entry(BTC, qty=1, limit=100.0, stop=98.0, target=105.0)
    saturday = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    verdict = run_check(req, q=q.model_copy(update={"ts": saturday}), now=saturday, delayed=True)
    assert verdict.approved, verdict.reasons


def test_block_no_day_trades_remaining() -> None:
    assert blocked_with(run_check(acct=account(day_trades_remaining=0)), "day trades")
    assert run_check(acct=account(day_trades_remaining=1)).approved


def test_block_missing_quote() -> None:
    assert blocked_with(run_check(q=None), "no quote")


def test_block_stale_quote() -> None:
    stale = quote(ts=NOW - timedelta(seconds=31))  # > 2 x 15 s monitor interval
    assert blocked_with(run_check(q=stale), "stale")
    assert run_check(q=quote(ts=NOW - timedelta(seconds=29))).approved


def test_delayed_quote_gets_extra_age() -> None:
    q = quote(ts=NOW - timedelta(minutes=10), delayed=True)
    assert run_check(q=q).approved


def test_block_qty_rounds_to_zero() -> None:
    assert blocked_with(run_check(entry(qty=0.5)), "rounds to 0")


def test_fractional_qty_rounded_down() -> None:
    verdict = run_check(entry(qty=10.7))
    assert verdict.approved and verdict.order is not None and verdict.order.qty == 10
    assert any("rounded down" in r for r in verdict.reasons)


def test_block_leveraged_etf() -> None:
    tqqq = Instrument(symbol="TQQQ")
    q = quote(tqqq)
    assert blocked_with(run_check(entry(tqqq), q=q), "leveraged")
    assert run_check(entry(tqqq), q=q, limits=RiskLimits(allow_leveraged_etfs=True)).approved


def test_block_halted_quote() -> None:
    assert blocked_with(run_check(q=quote(halted=True)), "is halted")


def test_block_halt_cooldown() -> None:
    assert blocked_with(run_check(last_halt_at=NOW - timedelta(minutes=5)), "cooldown")
    assert run_check(last_halt_at=NOW - timedelta(minutes=16)).approved


def test_block_missing_spread_for_stocks() -> None:
    assert blocked_with(run_check(q=quote(bid=None)), "two-sided")


def test_block_wide_spread() -> None:
    assert blocked_with(run_check(q=quote(bid=9.9, ask=10.1)), "spread 2.00% > 0.30%")


def test_crypto_without_bid_is_not_spread_blocked() -> None:
    q = quote(BTC, bid=None, ask=None, last=100.0)
    req = entry(BTC, qty=1, limit=100.0, stop=98.0, target=105.0)
    assert run_check(req, q=q).approved


def test_block_short_entry() -> None:
    req = entry().model_copy(update={"side": Side.SELL})
    assert blocked_with(run_check(req), "long-only")


def test_block_disabled_asset_class() -> None:
    assert blocked_with(run_check(settings=BotSettings(trade_stocks=False)), "disabled")
    btc = entry(BTC, qty=1, limit=100.0, stop=98.0, target=105.0)
    q = quote(BTC, bid=99.99, ask=100.01, last=100.0)
    # Crypto is off by default (its fees fail the fee check at this account size).
    assert blocked_with(run_check(btc, q=q, settings=BotSettings()), "crypto trading is disabled")


def test_all_reasons_are_reported() -> None:
    verdict = run_check(entry(stop=None), stats=day(halted=True), q=quote(halted=True))
    assert len(verdict.reasons) >= 3


# ---------------------------------------------------------------------- exits
def exit_req(qty: float) -> OrderRequest:
    return OrderRequest(
        instrument=F, side=Side.SELL, qty=qty, order_type=OrderType.MARKET, is_exit=True
    )


def test_exit_never_exceeds_held_qty() -> None:
    verdict = run_check(exit_req(15), positions=[pos("F", 10)])
    assert verdict.approved and verdict.order is not None
    assert verdict.order.qty == 10


def test_exit_allowed_while_halted_and_market_closed() -> None:
    saturday = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    verdict = run_check(exit_req(5), positions=[pos("F", 10)], stats=day(halted=True), now=saturday)
    assert verdict.approved and verdict.order is not None and verdict.order.qty == 5


def test_exit_without_position_is_blocked() -> None:
    assert blocked_with(run_check(exit_req(1)), "no long position")


def test_exit_must_be_sell() -> None:
    req = exit_req(1).model_copy(update={"side": Side.BUY})
    assert blocked_with(run_check(req, positions=[pos("F", 10)]), "must be SELL")


# ---------------------------------------------------------------------- halts
def test_halt_on_daily_loss() -> None:
    rm = RiskManager(RiskLimits())
    reason = rm.halt_reason(account=account(equity=470), day=day())
    assert reason is not None and reason.startswith(HALT_DAILY_LOSS)
    assert not is_soft_halt(reason)
    assert rm.halt_reason(account=account(equity=471), day=day()) is None


def test_daily_loss_is_hard_capped() -> None:
    rm = RiskManager(RiskLimits(max_daily_loss_pct=90))
    assert rm.halt_reason(account=account(equity=380), day=day()) is None  # -24%
    assert rm.halt_reason(account=account(equity=375), day=day()) is not None  # -25%


def test_soft_halt_at_target() -> None:
    rm = RiskManager(RiskLimits(daily_profit_target_usd=100))
    reason = rm.halt_reason(account=account(equity=600), day=day())
    assert reason is not None and reason.startswith(HALT_TARGET) and is_soft_halt(reason)
    assert rm.halt_reason(account=account(equity=599), day=day()) is None


def test_profit_target_defaults_to_a_percent_of_start_equity() -> None:
    """1.5% of start equity, not a fixed $100 (~16% of a $600 account)."""
    fresh = RiskLimits()
    assert fresh.daily_profit_target_pct == 1.5
    assert fresh.daily_profit_target_usd is None
    assert fresh.profit_target_usd(600) == pytest.approx(9.0)
    assert fresh.profit_target_usd(None) == 0.0
    rm = RiskManager(fresh)
    # 1.5% of the $500 start is $7.50.
    reason = rm.halt_reason(account=account(equity=507.5), day=day())
    assert reason is not None and reason.startswith(HALT_TARGET) and "1.5%" in reason
    assert rm.halt_reason(account=account(equity=507.49), day=day()) is None
    # A config saved with only a dollar target keeps that amount.
    legacy = RiskLimits.model_validate({"daily_profit_target_usd": 80, "max_daily_loss_pct": 6})
    assert legacy.daily_profit_target_pct is None
    assert legacy.profit_target_usd(600) == 80
    again = RiskLimits.model_validate_json(fresh.model_dump_json())
    assert again.daily_profit_target_pct == 1.5
    assert again.profit_target_usd(600) == pytest.approx(9.0)


def test_no_target_halt_when_disabled() -> None:
    rm = RiskManager(RiskLimits(stop_trading_at_target=False))
    assert rm.halt_reason(account=account(equity=900), day=day()) is None
    assert (
        RiskManager(RiskLimits(daily_profit_target_usd=0)).halt_reason(
            account=account(equity=900), day=day()
        )
        is None
    )


def test_no_halt_without_start_equity() -> None:
    rm = RiskManager(RiskLimits())
    assert rm.halt_reason(account=account(equity=1), day=day(start_equity=None)) is None


# ---------------------------------------------------------------------- quote sanity
@pytest.mark.parametrize(
    ("q", "problem"),
    [
        (quote(), None),
        (quote(bid=10.02, ask=10.01), "crossed quote"),
        (quote(bid=9.40, ask=9.45, last=10.0), "bid 9.4 is 6.0% from the last 10"),
        (quote(bid=9.40, ask=9.40, last=10.0), "locked quote 9.4 is 6.0% from the last 10"),
        (quote(bid=9.60, ask=9.62, last=10.0), None),  # 4% is still plausible for a stock
        (quote(bid=9.60, ask=10.40, last=None), None),  # no last: compared with the mid
        (quote(bid=9.40, ask=10.60, last=None), "bid 9.4 is 6.0% from the mid 10"),
        (quote(bid=None, ask=10.01, last=10.0), None),  # no bid: nothing to judge
        (quote(BTC, bid=93.0, ask=93.1, last=100.0), None),  # crypto allows 8%
        (quote(BTC, bid=91.0, ask=91.1, last=100.0), "9.0% from the last 100 (max 8%)"),
    ],
)
def test_quote_problem(q: Quote, problem: str | None) -> None:
    found = quote_problem(q)
    if problem is None:
        assert found is None
        assert sane_exit_price(q) == (q.bid or q.last)
    else:
        assert found is not None and problem in found
        assert sane_exit_price(q) is None


def test_sane_exit_price_without_quote() -> None:
    assert sane_exit_price(None) is None
    assert sane_exit_price(quote(bid=None, ask=None, last=None)) is None


def test_block_implausible_quote() -> None:
    crossed = run_check(q=quote(bid=10.03, ask=10.01))
    assert blocked_with(crossed, "quote for F looks wrong: crossed quote")
    far = run_check(q=quote(bid=9.0, ask=9.02, last=10.0), req=entry(limit=10.0))
    assert blocked_with(far, "looks wrong: bid 9")
