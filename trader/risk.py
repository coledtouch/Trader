"""Risk manager: position sizing, pre-trade checks and daily halt rules.

Every entry goes through `size_entry` and then `check`; any failed check means no order.
Limits are clamped to HARD_CAPS, the decision maker can only scale size down
(`size_fraction` <= 1), and nothing here looks at how far the day is from its profit
target: size depends on the start-of-day equity, the stop distance, the caps and the
losing streak (which can only halve it). There is no "make it back" path.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from trader.broker.base import Broker
from trader.models import (
    HARD_CAPS,
    LEVERAGED_ETFS,
    OPEN_ORDER_STATUSES,
    AccountSnapshot,
    AIDecision,
    AssetClass,
    BotSettings,
    DayStats,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderType,
    Position,
    Quote,
    RiskLimits,
    RiskVerdict,
    Side,
)
from trader.timeutil import in_entry_window, stock_market_open

HALT_DAILY_LOSS = "daily loss limit"
HALT_TARGET = "daily target reached"
STREAK_FACTOR = 0.5  # size multiplier after loss_streak_halve_after losses in a row
STALE_QUOTE_INTERVALS = 2  # a live quote older than this many monitor intervals is stale
DELAYED_QUOTE_GRACE = timedelta(minutes=15)  # extra age allowed for delayed-feed quotes
RR_TOLERANCE = 1e-6
CASH_TOLERANCE = 0.01  # dollars


def clamp_limits(limits: RiskLimits) -> RiskLimits:
    """Copy of `limits` with every HARD_CAPS field clamped to its ceiling."""
    data = limits.model_dump()
    for name, cap in HARD_CAPS.items():
        value = data.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > cap:
            data[name] = int(cap) if isinstance(value, int) else float(cap)
    return RiskLimits.model_validate(data)


def is_soft_halt(reason: str | None) -> bool:
    """True for the "daily target reached" halt: no new entries, positions keep their stops."""
    return bool(reason) and reason.startswith(HALT_TARGET)


def available_cash(account: AccountSnapshot) -> float:
    """Cash usable for a new purchase without borrowing: the lowest of cash, buying power
    and equity. The broker's buying power already reflects settlement rules for the
    account type (IBKR computes it per account), so unsettled-cash rules come through it."""
    return max(0.0, min(account.cash, account.buying_power, account.equity))


def max_position_value(limits: RiskLimits, equity: float) -> float:
    """Largest single position allowed in dollars (position % of equity and order cap)."""
    return max(0.0, min(limits.max_position_pct / 100 * equity, limits.max_order_notional_usd))


def position_value(pos: Position) -> float:
    price = pos.market_price if pos.market_price and pos.market_price > 0 else pos.avg_cost
    return max(0.0, pos.qty) * max(0.0, price)


def working_buy_notional(open_orders: list[OrderRecord]) -> float:
    """Cash committed to working buy orders (limit price x unfilled quantity)."""
    total = 0.0
    for order in open_orders:
        req = order.request
        if order.status not in OPEN_ORDER_STATUSES or req.side != Side.BUY:
            continue
        price = req.limit_price or order.avg_fill_price or 0.0
        total += max(0.0, req.qty - order.filled_qty) * price
    return total


def _aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _finite_positive(*values: float | None) -> bool:
    return all(v is not None and math.isfinite(v) and v > 0 for v in values)


def _units(instrument: Instrument) -> str:
    return "shares" if instrument.asset_class == AssetClass.STOCK else "units"


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = clamp_limits(limits)

    def set_limits(self, limits: RiskLimits) -> None:
        """Replace the limits (clamped again), e.g. after the owner edits settings."""
        self.limits = clamp_limits(limits)

    def effective_limits(self) -> RiskLimits:
        return self.limits.model_copy()

    # ------------------------------------------------------------------ sizing
    def size_entry(
        self,
        *,
        decision: AIDecision,
        entry: float,
        stop: float,
        account: AccountSnapshot,
        positions: list[Position],
        day: DayStats,
        broker: Broker,
        allow_fractional: bool,
    ) -> tuple[float, list[str]]:
        """Largest quantity every limit allows, rounded down. Returns (qty, notes); qty is 0
        when nothing fits, and the last note says why."""
        lim = self.limits
        inst = Instrument(symbol=decision.symbol, asset_class=decision.asset_class)
        if not _finite_positive(entry, stop) or stop >= entry:
            return 0.0, [f"invalid levels: entry {entry}, stop {stop}"]
        notes: list[str] = []
        base = day.start_equity if day.start_equity and day.start_equity > 0 else account.equity
        equity = account.equity
        if not _finite_positive(base, equity):
            return 0.0, ["account equity is not positive"]

        risk_usd = base * lim.max_risk_per_trade_pct / 100 * decision.size_fraction
        if decision.size_fraction < 1:
            notes.append(f"size fraction {decision.size_fraction:.2f}")
        if day.loss_streak >= lim.loss_streak_halve_after:
            risk_usd *= STREAK_FACTOR
            notes.append(f"{day.loss_streak} losses in a row: size halved")

        cash = available_cash(account)
        exposure_room = lim.max_total_exposure_pct / 100 * equity - sum(
            position_value(p) for p in positions
        )
        limits_qty = {
            f"risk ${risk_usd:.2f}": risk_usd / (entry - stop),
            f"max position {lim.max_position_pct:g}% of equity": (
                lim.max_position_pct / 100 * equity / entry
            ),
            f"max order ${lim.max_order_notional_usd:.2f}": lim.max_order_notional_usd / entry,
            f"available cash ${cash:.2f}": cash / entry,
            f"exposure room ${max(0.0, exposure_room):.2f}": max(0.0, exposure_room) / entry,
        }
        binding, raw = min(limits_qty.items(), key=lambda item: item[1])
        qty = broker.round_qty(inst, raw, allow_fractional)
        # Commissions are paid from the same cash.
        for _ in range(5):
            fee = broker.estimate_commission(inst, qty, entry)
            if qty <= 0 or qty * entry + fee <= cash + CASH_TOLERANCE:
                break
            qty = broker.round_qty(inst, min(qty, (cash - fee) / entry), allow_fractional)
        if qty > 0 and qty * entry + broker.estimate_commission(inst, qty, entry) > (
            cash + CASH_TOLERANCE
        ):
            qty = 0.0
        if qty <= 0:
            notes.append(
                f"size rounds to 0: {binding} allows {raw:.4g} {_units(inst)} at ${entry:g}"
            )
            return 0.0, notes
        notes.append(f"{qty:g} {_units(inst)} (limited by {binding})")
        return qty, notes

    # ------------------------------------------------------------------ checks
    def check(
        self,
        req: OrderRequest,
        *,
        quote: Quote | None,
        account: AccountSnapshot,
        positions: list[Position],
        open_orders: list[OrderRecord],
        day: DayStats,
        mode: Mode,
        data_delayed: bool,
        broker: Broker,
        now: datetime,
        settings: BotSettings,
        last_halt_at: datetime | None = None,
    ) -> RiskVerdict:
        """Approve, adjust or block an order. Exits are allowed when they reduce a held long
        (quantity clamped to the position); entries must pass every check below."""
        if req.is_exit:
            return self._check_exit(req, positions)
        return self._check_entry(
            req,
            quote=quote,
            account=account,
            positions=positions,
            open_orders=open_orders,
            day=day,
            mode=mode,
            data_delayed=data_delayed,
            broker=broker,
            now=now,
            settings=settings,
            last_halt_at=last_halt_at,
        )

    def _check_exit(self, req: OrderRequest, positions: list[Position]) -> RiskVerdict:
        if req.side != Side.SELL:
            return RiskVerdict(approved=False, reasons=["exit orders must be SELL (long-only)"])
        held = sum(p.qty for p in positions if p.instrument.key == req.instrument.key)
        if held <= 0:
            return RiskVerdict(
                approved=False, reasons=[f"no long position in {req.instrument.symbol} to exit"]
            )
        if req.qty > held + 1e-9:
            return RiskVerdict(
                approved=True,
                reasons=[f"exit qty reduced from {req.qty:g} to the {held:g} held (no shorting)"],
                order=req.model_copy(update={"qty": held}),
            )
        return RiskVerdict(approved=True, order=req)

    def _check_entry(
        self,
        req: OrderRequest,
        *,
        quote: Quote | None,
        account: AccountSnapshot,
        positions: list[Position],
        open_orders: list[OrderRecord],
        day: DayStats,
        mode: Mode,
        data_delayed: bool,
        broker: Broker,
        now: datetime,
        settings: BotSettings,
        last_halt_at: datetime | None,
    ) -> RiskVerdict:
        lim = self.limits
        inst = req.instrument
        sym = inst.symbol
        is_stock = inst.asset_class == AssetClass.STOCK
        reasons: list[str] = []

        # --- account state -----------------------------------------------------------
        if req.side != Side.BUY:
            reasons.append("entries must be BUY (long-only, no shorting)")
        if is_stock and not settings.trade_stocks:
            reasons.append("stock trading is disabled in settings")
        if not is_stock and not settings.trade_crypto:
            reasons.append("crypto trading is disabled in settings")
        if day.halted:
            reasons.append(f"trading halted: {day.halt_reason or 'halted'}")
        if day.trades >= lim.max_trades_per_day:
            reasons.append(f"{day.trades} entries today (max {lim.max_trades_per_day})")
        held = {p.instrument.key for p in positions if p.qty > 0}
        entries = {
            o.request.instrument.key
            for o in open_orders
            if o.status in OPEN_ORDER_STATUSES
            and o.request.side == Side.BUY
            and not o.request.is_exit
        }
        if inst.key in held:
            reasons.append(f"already holding {sym}: never add to a position")
        elif inst.key in entries:
            reasons.append(f"an entry order for {sym} is already working")
        occupied = len((held | entries) - {inst.key})
        if occupied >= lim.max_open_positions:
            reasons.append(f"{occupied} open positions/entries (max {lim.max_open_positions})")
        if sym.upper() in LEVERAGED_ETFS and not lim.allow_leveraged_etfs:
            reasons.append(f"{sym} is a leveraged/inverse ETF (not allowed in settings)")
        if is_stock and account.day_trades_remaining is not None:
            if account.day_trades_remaining < 1:
                reasons.append("no day trades remaining (pattern day trader rule)")

        # --- market data -------------------------------------------------------------
        ref = quote.price if quote is not None else None
        if quote is None or ref is None:
            reasons.append(f"no quote for {sym}")
        else:
            max_age = timedelta(seconds=STALE_QUOTE_INTERVALS * settings.monitor_seconds)
            if quote.delayed:
                max_age += DELAYED_QUOTE_GRACE
            age = _aware(now) - _aware(quote.ts)
            if age > max_age:
                reasons.append(f"quote for {sym} is stale ({age.total_seconds():.0f}s old)")
            if quote.halted:
                reasons.append(f"{sym} is halted")
            spread = quote.spread_pct
            if spread is None and is_stock:
                reasons.append(f"no two-sided quote for {sym}: cannot check the spread")
            elif spread is not None and spread > lim.max_spread_pct:
                reasons.append(f"spread {spread:.2f}% > {lim.max_spread_pct:.2f}%")
        if last_halt_at is not None and lim.halt_cooldown_minutes > 0:
            since = _aware(now) - _aware(last_halt_at)
            if since < timedelta(minutes=lim.halt_cooldown_minutes):
                reasons.append(
                    f"{sym} was halted {since.total_seconds() / 60:.0f} min ago "
                    f"(cooldown {lim.halt_cooldown_minutes} min)"
                )
        if mode == Mode.LIVE and is_stock and (data_delayed or (quote and quote.delayed)):
            reasons.append("LIVE mode with delayed market data: stock entries are blocked")
        if is_stock:
            if not stock_market_open(now):
                reasons.append("US stock market is closed")
            elif not in_entry_window(now, settings.stock_entry_start, settings.stock_entry_cutoff):
                reasons.append(
                    f"outside the stock entry window "
                    f"{settings.stock_entry_start}-{settings.stock_entry_cutoff} ET"
                )

        # --- price levels ------------------------------------------------------------
        if req.order_type == OrderType.LIMIT:
            entry = req.limit_price
        else:
            entry = (quote.ask if quote is not None and quote.ask else None) or ref
        stop, target = req.stop_loss, req.take_profit
        levels_ok = False
        if entry is None or not _finite_positive(entry):
            reasons.append("no valid entry price")
        elif stop is None:
            reasons.append("no stop-loss: every entry needs one")
        elif not _finite_positive(stop) or stop >= entry:
            reasons.append(f"stop {stop:g} must be above 0 and below the entry {entry:g}")
        elif target is None:
            reasons.append("no take-profit target: reward:risk cannot be checked")
        elif not math.isfinite(target) or target <= entry:
            reasons.append(f"target {target:g} must be above the entry {entry:g}")
        else:
            levels_ok = True
            rr = (target - entry) / (entry - stop)
            if rr < lim.min_reward_risk - RR_TOLERANCE:
                reasons.append(f"reward:risk {rr:.2f} < {lim.min_reward_risk:.2f}")
        if entry is not None and _finite_positive(entry):
            if entry < lim.min_price:
                reasons.append(f"price ${entry:g} is below the ${lim.min_price:g} minimum")
            if req.order_type == OrderType.LIMIT and ref:
                deviation = abs(entry - ref) / ref * 100
                if deviation > lim.price_collar_pct:
                    reasons.append(
                        f"limit {entry:g} is {deviation:.2f}% from the last price {ref:g} "
                        f"(collar {lim.price_collar_pct:g}%)"
                    )

        # --- size, exposure, cash, fees ----------------------------------------------
        allow_fractional = settings.allow_fractional_stocks
        qty = broker.round_qty(inst, req.qty, allow_fractional)
        order = req
        adjusted: list[str] = []
        if qty <= 0:
            reasons.append("quantity rounds to 0")
        elif qty < req.qty - 1e-12:
            order = req.model_copy(update={"qty": qty})
            adjusted.append(f"quantity rounded down from {req.qty:g} to {qty:g}")
        if qty > 0 and entry is not None and _finite_positive(entry):
            equity = account.equity
            notional = qty * entry
            if notional > lim.max_order_notional_usd + CASH_TOLERANCE:
                reasons.append(
                    f"order ${notional:.2f} > max order ${lim.max_order_notional_usd:.2f}"
                )
            if levels_ok and stop is not None:
                base = day.start_equity if day.start_equity and day.start_equity > 0 else equity
                max_risk = lim.max_risk_per_trade_pct / 100 * base
                at_stop = (entry - stop) * qty
                if at_stop > max_risk + CASH_TOLERANCE:
                    reasons.append(
                        f"risk at the stop ${at_stop:.2f} > {lim.max_risk_per_trade_pct:g}% "
                        f"of ${base:.2f} (${max_risk:.2f})"
                    )
            if equity <= 0:
                reasons.append("account equity is not positive")
            else:
                cap = lim.max_position_pct / 100 * equity
                if notional > cap + CASH_TOLERANCE:
                    reasons.append(
                        f"position ${notional:.2f} > {lim.max_position_pct:g}% of equity "
                        f"(${cap:.2f})"
                    )
                committed = working_buy_notional(open_orders)
                exposure = sum(position_value(p) for p in positions) + committed + notional
                max_exposure = lim.max_total_exposure_pct / 100 * equity
                if exposure > max_exposure + CASH_TOLERANCE:
                    reasons.append(
                        f"exposure after fill ${exposure:.2f} > {lim.max_total_exposure_pct:g}% "
                        f"of equity (${max_exposure:.2f})"
                    )
                fee_buy = broker.estimate_commission(inst, qty, entry)
                need = notional + fee_buy
                cash = available_cash(account) - committed
                if need > cash + CASH_TOLERANCE:
                    reasons.append(
                        f"insufficient cash: needs ${need:.2f}, ${max(0.0, cash):.2f} available"
                    )
                if levels_ok and target is not None:
                    fees = fee_buy + broker.estimate_commission(inst, qty, target)
                    reward = (target - entry) * qty
                    if fees > lim.max_fee_to_reward_pct / 100 * reward:
                        reasons.append(
                            f"round-trip fees ${fees:.2f} are {fees / reward * 100:.0f}% of the "
                            f"${reward:.2f} expected reward (max {lim.max_fee_to_reward_pct:g}%)"
                        )

        if reasons:
            return RiskVerdict(approved=False, reasons=reasons, order=None)
        return RiskVerdict(approved=True, reasons=adjusted, order=order)

    # ------------------------------------------------------------------ halts
    def halt_reason(self, *, account: AccountSnapshot, day: DayStats) -> str | None:
        """ "daily loss limit: ..." (hard halt), "daily target reached: ..." (soft halt), or
        None. Needs the day's start equity; P&L is equity minus that start."""
        start = day.start_equity
        if not start or start <= 0:
            return None
        lim = self.limits
        equity = account.equity
        floor = start * (1 - lim.max_daily_loss_pct / 100)
        if equity <= floor:
            down = (1 - equity / start) * 100
            return (
                f"{HALT_DAILY_LOSS}: equity ${equity:.2f} is down {down:.1f}% from "
                f"${start:.2f} (limit {lim.max_daily_loss_pct:g}%)"
            )
        pnl = equity - start
        target = lim.daily_profit_target_usd
        if lim.stop_trading_at_target and target > 0 and pnl >= target:
            return f"{HALT_TARGET}: +${pnl:.2f} today (target ${target:.2f})"
        return None
