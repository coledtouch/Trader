"""Account-scoped economic reporting; signal excursions are never treated as filled profit."""

from __future__ import annotations

import calendar
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trader.db import Database
from trader.models import BotSettings, EngineStatus, Mode, TradeRecord

NY = ZoneInfo("America/New_York")


def _at(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def monthly_fraction(start: datetime, end: datetime) -> float:
    """Accrue a monthly estimate by elapsed calendar time, including weekends."""
    total = 0.0
    cursor = start.astimezone(NY)
    end = end.astimezone(NY)
    while cursor < end:
        year, month = cursor.year + (cursor.month == 12), cursor.month % 12 + 1
        boundary = datetime(year, month, 1, tzinfo=NY)
        stop = min(boundary, end)
        total += (stop - cursor).total_seconds() / (
            calendar.monthrange(cursor.year, cursor.month)[1] * 86400
        )
        cursor = stop
    return total


def _setup_report(trades: list[TradeRecord]) -> list[dict[str, Any]]:
    """Per-setup results of trades entered on real-time data. Paper trades entered on
    delayed data (RiskLimits.allow_delayed_paper_entries) only test the order plumbing,
    so they are left out (counted separately by performance_report)."""
    grouped: dict[str, list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        if trade.delayed_data:
            continue
        grouped[trade.setup or "unclassified"].append(trade)
    result = []
    for setup, items in sorted(grouped.items()):
        by_day: dict[str, list[float]] = defaultdict(list)
        for item in items:
            by_day[item.exit_ts.astimezone(NY).date().isoformat()].append(item.pnl)
        groups = list(by_day.values())
        low = high = None
        # Resample trading days, not correlated individual entries. A short history
        # remains explicitly exploratory regardless of the number of signal rows.
        if len(groups) >= 5:
            rng = random.Random(731)  # noqa: S311 - reproducible statistical bootstrap
            estimates = []
            for _ in range(500):
                sample = [v for group in rng.choices(groups, k=len(groups)) for v in group]
                estimates.append(sum(sample) / len(sample))
            estimates.sort()
            low, high = round(estimates[12], 4), round(estimates[487], 4)
        pnl = sum(t.pnl for t in items)
        result.append(
            {
                "setup": setup,
                "trades": len(items),
                "net_pnl": round(pnl, 4),
                "net_expectancy": round(pnl / len(items), 4),
                "days_observed": len(groups),
                "confidence_low": low,
                "confidence_high": high,
                "evidence": "exploratory; no sample count certifies an edge",
            }
        )
    return result


def performance_report(
    db: Database, settings: BotSettings, current: EngineStatus, now: datetime, days: int = 30
) -> dict[str, Any]:
    """Daily equity changes include open P&L and broker commissions, not external costs.

    Engine-adjusted start equity removes detected cash flows. Trade expectancy uses
    filled round trips only; subtracting commissions again would double-count them.
    """
    now = now.astimezone(UTC)
    days = max(1, min(days, 90))
    start_day = now.astimezone(NY).date() - timedelta(days=days - 1)
    since = datetime.combine(start_day, datetime.min.time(), NY).astimezone(UTC)
    epoch_raw = db.get_kv("journal_account_since")
    if epoch_raw is None:
        # Upgrade existing journals without merging an old simulator/account history.
        switched = [a for a in db.list_audit(limit=1000) if a["action"] == "account_changed"]
        epoch_raw = switched[0]["ts"] if switched else None
    epoch = _at(epoch_raw) if epoch_raw else None
    if epoch is not None:
        since = max(since, epoch)
    day_rows = sorted(
        (
            d
            for d in db.list_days(limit=100)
            if d.day >= since.astimezone(NY).date().isoformat()
            and d.day <= now.astimezone(NY).date().isoformat()
        ),
        key=lambda d: d.day,
    )
    warnings = [
        "AI costs are recorded token-price estimates; reconcile provider invoices.",
        "Filled-trade expectancy is after commissions; operating costs are reported separately.",
    ]
    if current.mode == Mode.SIM:
        warnings.append("Synthetic simulator results test mechanics, not market profitability.")
    if current.data_delayed:
        if current.mode == Mode.PAPER and settings.risk.allow_delayed_paper_entries:
            warnings.append(
                "Delayed data: paper entries are allowed by your setting. Those trades test "
                "the order plumbing and are left out of the per-setup results."
            )
        else:
            warnings.append(
                "Delayed data: stock entry analysis is paused; signals are diagnostics only."
            )
    if epoch is not None and epoch > datetime.combine(
        epoch.astimezone(NY).date(), datetime.min.time(), NY
    ):
        warnings.append(
            "History starts within a trading day; the first daily aggregate may be partial."
        )
    flows = db.cash_flows(since, now)
    valid = []
    previous_day: str | None = None
    previous_end: float | None = None
    for row in day_rows:
        end = (
            current.equity
            if row.day == current.day and current.equity is not None
            else row.end_equity
        )
        if row.start_equity is not None and end is not None:
            pnl = end - row.start_equity
            if previous_end is not None and previous_day is not None:
                # The first quote of a new day is not yesterday's closing mark.
                # Bridge overnight/offline moves, subtracting recognized transfers
                # already folded into the new day's baseline by the engine.
                transfers = sum(
                    f.get("amount", 0.0)
                    for f in flows
                    if previous_day < _at(f["ts"]).astimezone(NY).date().isoformat() <= row.day
                )
                pnl += row.start_equity - previous_end - transfers
            valid.append((row, pnl))
            previous_day, previous_end = row.day, end
    trading_pnl = sum(pnl for _, pnl in valid)
    trades = [t for t in db.list_trades(limit=100_000, since=since) if t.exit_ts <= now]
    delayed_trades = sum(1 for t in trades if t.delayed_data)
    if delayed_trades:
        warnings.append(
            f"{delayed_trades} trade(s) were entered on delayed data (paper): they count in the "
            "account totals but are left out of the expectancy, profit factor and per-setup "
            "results."
        )
    ai_cost = db.decision_cost(since, now)
    history_start = (
        datetime.combine(
            datetime.fromisoformat(day_rows[0].day).date(), datetime.min.time(), NY
        ).astimezone(UTC)
        if day_rows
        else now
    )
    covered_start = max(since, history_start)
    fraction = monthly_fraction(min(covered_start, now), now)
    costs = settings.operating_costs
    configured = costs.hosting_monthly_usd is not None and costs.data_monthly_usd is not None
    hosting = (costs.hosting_monthly_usd or 0.0) * fraction
    data = (costs.data_monthly_usd or 0.0) * fraction
    if not configured:
        warnings.append(
            "Hosting or data costs are unknown. Enter estimates, including explicit zero."
        )
    else:
        warnings.append(
            "Hosting/data are calendar-prorated estimates using current monthly settings."
        )
    if not valid:
        warnings.append("No complete account equity observations in this period.")
    # Rebuild a continuous daily P&L path from adjusted baselines, not deposits.
    # This intentionally reports daily observation drawdown, not unobserved intraday lows.
    base = valid[0][0].start_equity if valid else None
    running = peak = base or 0.0
    drawdown = 0.0
    for _, pnl in valid:
        running += pnl
        peak = max(peak, running)
        if peak > 0:
            drawdown = max(drawdown, (peak - running) / peak * 100)
    warnings.append("Drawdown uses daily equity observations; intraday losses can be larger.")
    warnings.append(
        "Day-to-day equity gaps include overnight moves; only recognized cash flows are excluded."
    )
    # Edge measures use trades entered on real-time data only (delayed-data paper trades
    # test the plumbing); the account totals above include every trade.
    edge_trades = [t for t in trades if not t.delayed_data]
    positive = sum(max(t.pnl, 0.0) for t in edge_trades)
    negative = -sum(min(t.pnl, 0.0) for t in edge_trades)
    signal_count, evaluated_signals = db.signal_counts(since, now)
    known_net = trading_pnl - ai_cost - hosting - data
    return {
        "days": days,
        "mode": current.mode.value,
        "as_of": now.isoformat(),
        "covered_from": covered_start.isoformat(),
        "trading_pnl": round(trading_pnl, 4),
        "ai_cost": round(ai_cost, 4),
        "hosting_cost": round(hosting, 4) if costs.hosting_monthly_usd is not None else None,
        "data_cost": round(data, 4) if costs.data_monthly_usd is not None else None,
        "operating_cost": round(ai_cost + hosting + data, 4),
        "costs_configured": configured,
        "known_cost_pnl": round(known_net, 4),
        "all_in_pnl": round(known_net, 4) if configured and valid else None,
        "closed_trades": len(trades),
        "delayed_data_trades": delayed_trades,
        "net_expectancy": (
            round(sum(t.pnl for t in edge_trades) / len(edge_trades), 4) if edge_trades else None
        ),
        "profit_factor": round(positive / negative, 4) if negative else None,
        "max_drawdown_pct": round(drawdown, 4) if valid else None,
        "drawdown_basis": "daily equity observations",
        "signal_count": signal_count,
        "evaluated_signals": evaluated_signals,
        "warnings": warnings,
        "setup_stats": _setup_report(trades),
        "annualized_return": None,  # no extrapolation from a tiny paper sample
    }
