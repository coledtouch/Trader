import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader import fees
from trader.backtest import (
    BacktestConfig,
    last_session_close,
    load_csv,
    main,
    run_backtest,
)
from trader.models import Bar, BotSettings, Instrument, RiskLimits, Side
from trader.strategy.signals import Scanner

NY = ZoneInfo("America/New_York")
STOCK = Instrument(symbol="TEST")
FIVE = timedelta(minutes=5)
TODAY = date(2026, 10, 21)
BIG = BotSettings(risk=RiskLimits(max_order_notional_usd=5000))  # fees must not block the trade
CONFIG = BacktestConfig(starting_equity=10_000, slippage_bps=5.0)


def at(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), NY).astimezone(UTC)


def bars_from(start: datetime, rows) -> list[Bar]:
    return [
        Bar(ts=start + FIVE * i, open=o, high=h, low=lo, close=c, volume=v)
        for i, (o, h, lo, c, v) in enumerate(rows)
    ]


def orb_setup() -> list[Bar]:
    """A prior quiet session, then an opening-range breakout signalled by the 09:55 bar."""
    prior = bars_from(at(date(2026, 10, 20), 9, 30), [(100, 100.1, 99.9, 100, 10_000)] * 78)
    closes = [100.2, 100.8, 100.5, 100.6, 100.7, 101.1]
    vols = [40_000, 30_000, 25_000, 20_000, 22_000, 45_000]
    rows, prev = [], 100.0
    for c, v in zip(closes, vols, strict=True):
        rows.append((prev, max(prev, c) + 0.1, min(prev, c) - 0.1, c, v))
        prev = c
    return prior + bars_from(at(TODAY, 9, 30), rows)


def with_after(rows) -> list[Bar]:
    """The ORB setup followed by `rows` from 10:00 on."""
    return orb_setup() + bars_from(at(TODAY, 10, 0), rows)


def signal():
    bars = orb_setup()
    [cand] = Scanner(BIG).scan(STOCK, bars, None, at(TODAY, 10, 0))
    return cand


def test_scenario_produces_an_orb_signal():
    cand = signal()
    assert cand.setup == "orb_breakout"
    assert cand.suggested_stop < 101.1 < cand.suggested_target


def test_entry_next_open_and_stop_first_when_bar_hits_both():
    cand = signal()
    result = run_backtest(
        [(STOCK, with_after([(101.1, 103.0, 100.0, 101.0, 30_000)]))], BIG, CONFIG
    )
    [trade] = result.trades
    entry = 101.1 * 1.0005  # next bar's open plus 5 bps
    assert trade.entry_ts == at(TODAY, 10, 0)
    assert trade.entry_price == pytest.approx(entry)
    assert trade.exit_reason == "stop (bar also hit target)"
    assert trade.exit_price == pytest.approx(cand.suggested_stop * 0.9995)
    # 2% of $10,000 risk is capped by max position (50%) and max notional ($5,000)
    assert trade.qty == int(5000 / entry)
    # IBKR Pro Tiered by default; the sale also pays the SEC fee and FINRA TAF
    commission = fees.estimate_commission(STOCK, trade.qty, entry, Side.BUY, "tiered")
    commission += fees.estimate_commission(STOCK, trade.qty, trade.exit_price, Side.SELL, "tiered")
    assert trade.commission == pytest.approx(commission)
    assert trade.gross_pnl == pytest.approx((trade.exit_price - entry) * trade.qty)
    assert trade.pnl == pytest.approx(trade.gross_pnl - commission)
    assert trade.slippage == pytest.approx(
        (entry - 101.1 + cand.suggested_stop - trade.exit_price) * trade.qty
    )
    assert trade.r_multiple < -1  # a full stop plus costs
    stats = result.stats()
    assert stats["trades"] == 1 and stats["losses"] == 1 and stats["win_rate_pct"] == 0
    assert stats["fees"] == stats["fees_per_trade"] == pytest.approx(commission)
    assert stats["gross_pnl"] == pytest.approx(trade.gross_pnl)
    assert stats["fees_pct_of_gross_profit"] is None  # a loss: no gross profit to share
    assert stats["pre_cost_pnl"] == pytest.approx(trade.pnl + trade.commission + trade.slippage)
    assert stats["total_pnl"] == pytest.approx(trade.pnl)
    assert stats["ending_equity"] == pytest.approx(10_000 + trade.pnl)
    assert stats["max_drawdown"] >= -trade.pnl - 1e-9
    assert stats["profit_factor"] == 0.0


def test_target_fill_is_a_limit_without_slippage():
    cand = signal()
    rows = [(101.1, cand.suggested_target + 0.3, 100.9, 101.8, 30_000)]
    result = run_backtest(
        [(STOCK, with_after(rows))], BIG, BacktestConfig(starting_equity=10_000, target_usd=10)
    )
    [trade] = result.trades
    assert trade.exit_reason == "target"
    assert trade.exit_price == cand.suggested_target
    assert trade.pnl > 0 and trade.r_multiple > 1
    stats = result.stats()
    assert stats["win_rate_pct"] == 100 and stats["profit_factor"] is None
    assert stats["days_hitting_target"] == 1
    assert stats["fees_pct_of_gross_profit"] == pytest.approx(
        trade.commission / trade.gross_pnl * 100
    )
    assert result.daily_pnl[TODAY] == pytest.approx(trade.pnl)


def test_gap_through_stop_fills_at_the_open():
    cand = signal()
    gap_open = cand.suggested_stop - 0.5
    rows = [
        (101.1, 101.2, 101.0, 101.1, 30_000),  # entry bar: nothing hit
        (gap_open, gap_open + 0.1, gap_open - 0.2, gap_open, 30_000),
    ]
    [trade] = run_backtest([(STOCK, with_after(rows))], BIG, CONFIG).trades
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(gap_open * 0.9995)


def test_stocks_are_flat_before_the_close():
    quiet = [(101.1, 101.2, 101.0, 101.1, 5_000)] * 72  # 10:00 .. 15:55
    result = run_backtest([(STOCK, with_after(quiet))], BIG, CONFIG)
    [trade] = result.trades
    assert trade.exit_reason == "flatten before close"
    assert trade.exit_ts == at(TODAY, 15, 50)
    assert trade.exit_price == pytest.approx(101.1 * 0.9995)


def test_small_account_fees_block_the_trade_honestly():
    # Defaults: $500 equity, $250 max order -> 2 shares; ~$0.72 tiered round trip is more
    # than 25% of the ~$1.7 reward.
    result = run_backtest(
        [(STOCK, with_after([(101.1, 103.0, 100.0, 101.0, 30_000)]))],
        BotSettings(),
        BacktestConfig(),
    )
    assert result.trades == []
    assert result.skipped == {"round-trip fees too high vs reward": 1}


def test_commission_plan_changes_the_fees():
    data = [(STOCK, with_after([(101.1, 103.0, 100.0, 101.0, 30_000)]))]
    by_plan = {}
    for plan in ("tiered", "fixed"):
        cfg = BacktestConfig(starting_equity=10_000, commission_plan=plan)
        [trade] = run_backtest(data, BIG, cfg).trades
        expected = fees.estimate_commission(STOCK, trade.qty, trade.entry_price, Side.BUY, plan)
        expected += fees.estimate_commission(STOCK, trade.qty, trade.exit_price, Side.SELL, plan)
        assert trade.commission == pytest.approx(expected)
        by_plan[plan] = trade
    assert by_plan["tiered"].qty == by_plan["fixed"].qty
    assert by_plan["tiered"].commission != by_plan["fixed"].commission


def test_size_never_depends_on_the_daily_target():
    data = [(STOCK, with_after([(101.1, 103.0, 100.0, 101.0, 30_000)]))]
    qtys = set()
    for target in (1.0, 100.0, 10_000.0):
        settings = BIG.model_copy(
            update={"risk": BIG.risk.model_copy(update={"daily_profit_target_usd": target})}
        )
        cfg = BacktestConfig(starting_equity=10_000, target_usd=target)
        qtys.add(run_backtest(data, settings, cfg).trades[0].qty)
    assert len(qtys) == 1


def test_threshold_filters_candidates():
    data = [(STOCK, with_after([(101.1, 103.0, 100.0, 101.0, 30_000)]))]
    cfg = BacktestConfig(starting_equity=10_000, threshold=0.99)
    assert run_backtest(data, BIG, cfg).trades == []


def test_empty_data():
    result = run_backtest([], BotSettings(), BacktestConfig())
    stats = result.stats()
    assert (
        stats["trades"] == 0 and stats["avg_pnl_per_day"] is None and stats["win_rate_pct"] is None
    )


# ----------------------------------------------------------------------------- data
def test_load_csv(tmp_path):
    path = tmp_path / "test.csv"
    path.write_text(
        "TS,Open,High,Low,Close,Volume\n"
        "2026-10-21T09:35:00-04:00,10,11,9,10.5,100\n"
        "2026-10-21 09:30:00,10,10.5,9.5,10,\n"  # naive -> --csv-tz; empty volume -> 0
        "2026-10-21T09:35:00-04:00,10,11,9,10.5,100\n",  # duplicate timestamp
        encoding="utf-8",
    )
    bars = load_csv(path, "America/New_York")
    assert [b.ts for b in bars] == [at(TODAY, 9, 30), at(TODAY, 9, 35)]
    assert bars[0].volume == 0 and bars[1].close == 10.5


@pytest.mark.parametrize(
    "content",
    [
        "ts,open,high,close\n2026-10-21T13:30:00Z,1,2,1\n",  # missing column
        "ts,open,high,low,close\n2026-10-21T13:30:00Z,10,9,8,10\n",  # high < open
        "ts,open,high,low,close\nnot-a-date,1,2,1,1\n",
    ],
)
def test_load_csv_rejects_bad_input(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_csv(path)


def test_last_session_close():
    saturday = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
    assert last_session_close(saturday) == datetime(2026, 10, 23, 16, 0, tzinfo=NY)
    wed_morning = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)
    assert last_session_close(wed_morning) == datetime(2026, 10, 20, 16, 0, tzinfo=NY)
    wed_evening = datetime(2026, 10, 21, 22, 0, tzinfo=UTC)
    assert last_session_close(wed_evening) == datetime(2026, 10, 21, 16, 0, tzinfo=NY)


# ----------------------------------------------------------------------------- CLI
def test_main_with_csv_prints_costs(tmp_path, capsys):
    path = tmp_path / "test.csv"
    lines = ["ts,open,high,low,close,volume"]
    for bar in with_after([(101.1, 103.0, 100.0, 101.0, 30_000)]):
        lines.append(
            f"{bar.ts.isoformat()},{bar.open},{bar.high},{bar.low},{bar.close},{bar.volume}"
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    code = main(["--csv", str(path), "--equity", "10000", "--max-notional", "5000", "--trades"])
    out = capsys.readouterr().out
    assert code == 0
    assert "TEST" in out and "orb_breakout" in out
    for label in ("P&L before costs", "Commissions", "Slippage", "Net P&L", "Days >= $100.00"):
        assert label in out
    assert "IBKR Pro Tiered fees" in out
    assert "per trade" in out and "no gross profit to pay them from" in out  # a stopped trade
    assert "too few to tell skill from luck" in out

    cand = signal()
    win = [(101.1, cand.suggested_target + 0.3, 100.9, 101.8, 30_000)]
    lines = ["ts,open,high,low,close,volume"]
    lines += [
        f"{b.ts.isoformat()},{b.open},{b.high},{b.low},{b.close},{b.volume}"
        for b in with_after(win)
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    args = ["--csv", str(path), "--equity", "10000", "--max-notional", "5000", "--plan", "fixed"]
    assert main([*args, "--trades"]) == 0
    out = capsys.readouterr().out
    assert "IBKR Pro Fixed fees" in out
    assert "gross profit (before commissions)" in out and "of gross)" in out


def test_main_synthetic_json(capsys):
    code = main(
        ["--symbols", "F,SOFI", "--days", "2", "--end", "2026-10-22", "--json", "--target", "50"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    stats = payload["stats"]
    assert stats["trading_days"] == 2 and stats["target_usd"] == 50
    for key in (
        "trades",
        "win_rate_pct",
        "avg_r",
        "profit_factor",
        "fees",
        "fees_per_trade",
        "fees_pct_of_gross_profit",
        "gross_pnl",
        "slippage",
    ):
        assert key in stats
    assert stats["max_drawdown"] >= 0


def test_main_reports_bad_input(tmp_path, capsys):
    assert main(["--csv", str(tmp_path / "missing.csv")]) == 2
    assert "error" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--equity", "-5"])
    with pytest.raises(SystemExit):
        main(["--plan", "lite"])  # IBKR Lite has no API access


def test_daily_loss_limit_halts_and_flattens():
    # A tight 0.1% daily loss limit trips while the price is still above the stop.
    settings = BIG.model_copy(
        update={"risk": BIG.risk.model_copy(update={"max_daily_loss_pct": 0.1})}
    )
    cand = signal()
    dip = (101.1 + cand.suggested_stop) / 2  # between entry and stop
    rows = [(101.1, 101.2, 101.0, 101.1, 30_000), (101.1, 101.1, dip, dip, 30_000)]
    rows += [(dip, dip + 0.05, dip - 0.05, dip, 30_000)] * 3
    result = run_backtest([(STOCK, with_after(rows))], settings, CONFIG)
    [trade] = result.trades
    assert trade.exit_reason == "daily loss limit"
    assert trade.exit_price == pytest.approx(dip * 0.9995)
    assert result.halted_days == {"daily loss limit": 1}
