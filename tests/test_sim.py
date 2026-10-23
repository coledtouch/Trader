from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader import fees
from trader.broker import sim
from trader.broker.base import BrokerError
from trader.broker.sim import SimBroker, SymbolProfile
from trader.models import (
    AssetClass,
    Instrument,
    Mode,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)

NY = ZoneInfo("America/New_York")
WED_1100 = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # Wednesday 11:00 New York
SPY = Instrument(symbol="SPY")
F = Instrument(symbol="F")
BTC = Instrument(symbol="BTC", asset_class=AssetClass.CRYPTO)
THIN = Instrument(symbol="THIN")


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


async def make_broker(start: datetime = WED_1100, cash: float = 500.0, seed: int = 7):
    clock = FakeClock(start)
    broker = SimBroker(seed=seed, starting_cash=cash, clock=clock)
    await broker.connect()
    return broker, clock


def buy(inst, qty, order_type=OrderType.MARKET, **kw):
    return OrderRequest(instrument=inst, side=Side.BUY, qty=qty, order_type=order_type, **kw)


def sell(inst, qty, order_type=OrderType.MARKET, **kw):
    return OrderRequest(
        instrument=inst, side=Side.SELL, qty=qty, order_type=order_type, is_exit=True, **kw
    )


@pytest.fixture
def thin_symbol(monkeypatch):
    # ~51 shares/minute on average: a single order may take at most 10% of a minute.
    monkeypatch.setitem(sim.KNOWN_PROFILES, "STK:THIN", SymbolProfile(5.0, 0.03, 20_000))


# ---------------------------------------------------------------------------- commissions
@pytest.mark.parametrize(
    ("plan", "inst", "qty", "price", "side", "expected"),
    [
        ("fixed", F, 100, 50.0, Side.BUY, 1.00),  # $0.50 per-share -> $1.00 minimum
        ("fixed", F, 1000, 20.0, Side.BUY, 5.00),  # $0.005/share
        ("fixed", F, 10, 5.0, Side.BUY, 0.50),  # 1% of $50 caps below the minimum
        ("tiered", F, 20, 12.0, Side.BUY, 0.35 + 20 * 0.0032),  # $0.35 min + exchange/clearing
        ("tiered", F, 1000, 20.0, Side.BUY, 3.5 + 1000 * 0.0032),
        # sells add the SEC fee and FINRA TAF
        ("tiered", F, 20, 12.0, Side.SELL, 0.35 + 20 * 0.0032 + 240 * 0.0000278 + 20 * 0.000166),
        ("tiered", BTC, 1, 10_000.0, Side.BUY, 18.0),  # crypto 0.18%
        ("fixed", BTC, 1, 500.0, Side.SELL, 1.75),  # minimum
        ("tiered", BTC, 1, 100.0, Side.BUY, 1.00),  # 1% cap below the minimum
        ("tiered", F, 0, 10.0, Side.BUY, 0.0),
    ],
)
def test_commissions_come_from_the_fee_module(plan, inst, qty, price, side, expected):
    broker = SimBroker(commission_plan=plan, clock=FakeClock(WED_1100))
    assert broker.estimate_commission(inst, qty, price, side) == pytest.approx(expected, abs=1e-4)
    assert broker.estimate_commission(inst, qty, price, side) == fees.estimate_commission(
        inst, qty, price, side, plan
    )


async def test_estimate_commission_and_capabilities():
    broker, _ = await make_broker()
    assert broker.commission_plan == "tiered"
    assert broker.estimate_commission(F, 20, 11.0) == pytest.approx(0.35 + 20 * 0.0032)
    assert broker.supports_native_stops(SPY) is True
    assert broker.supports_native_stops(BTC) is False
    with pytest.raises(ValueError):
        SimBroker(commission_plan="ibkr_pro_fixed")  # type: ignore[arg-type]


async def test_fixed_plan_charges_fixed_commissions():
    clock = FakeClock(WED_1100)
    broker = SimBroker(starting_cash=500.0, clock=clock, commission_plan="fixed")
    await broker.connect()
    rec = await broker.place_order(buy(F, 20))
    assert rec.commission == pytest.approx(1.0)
    sale = await broker.place_order(sell(F, 20))
    expected = fees.estimate_commission(F, 20, sale.avg_fill_price, Side.SELL, "fixed")
    assert sale.commission == pytest.approx(expected) and expected > 1.0  # + regulatory fees


# ---------------------------------------------------------------------------- account
async def test_account_and_connection():
    broker = SimBroker(seed=1, starting_cash=500.0, clock=FakeClock(WED_1100))
    assert broker.mode == Mode.SIM
    with pytest.raises(BrokerError):
        await broker.account()
    await broker.connect()
    assert broker.connected
    acct = await broker.account()
    assert acct.mode == Mode.SIM
    assert acct.equity == acct.cash == acct.settled_cash == acct.buying_power == 500.0
    assert acct.day_trades_remaining is None
    assert await broker.positions() == []
    await broker.disconnect()
    assert not broker.connected
    with pytest.raises(BrokerError):
        await broker.quote(SPY)


# ---------------------------------------------------------------------------- market data
async def test_deterministic_per_seed_and_base_prices():
    a, _ = await make_broker(seed=3)
    b, _ = await make_broker(seed=3)
    c, _ = await make_broker(seed=4)
    bars_a = await a.bars(BTC, "5 mins", "1 D")
    assert bars_a == await b.bars(BTC, "5 mins", "1 D")
    assert bars_a != await c.bars(BTC, "5 mins", "1 D")
    assert (await a.quote(SPY)).last == 764.0
    assert (await a.quote(BTC)).last == 81_700.0
    assert (await a.quote(F)).last == 12.0
    unknown = Instrument(symbol="ZZZQ")
    price = (await a.quote(unknown)).last
    assert price == (await c.quote(unknown)).last  # stable hash-based base price
    assert 3.0 <= price <= 150.0


async def test_path_does_not_depend_on_call_order():
    a, clock_a = await make_broker()
    b, clock_b = await make_broker()
    # a looks far back first, b never does: the shared window must be identical.
    await a.bars(SPY, "1 min", "5 D")
    for clock in (clock_a, clock_b):
        clock.t += timedelta(minutes=90)
    assert await a.bars(SPY, "1 min", "1 D") == await b.bars(SPY, "1 min", "1 D")


async def test_bars_consistent_with_quotes_and_time():
    broker, clock = await make_broker()
    clock.t += timedelta(minutes=7, seconds=30)  # in the middle of a 5-minute bar
    now = clock.t
    for inst in (SPY, BTC):
        quote = await broker.quote(inst)
        bars = await broker.bars(inst, "5 mins", "2 D")
        assert bars[-1].close == quote.last
        assert quote.bid < quote.ask and quote.bid <= quote.last <= quote.ask
        assert quote.ts == now
        assert bars[-1].ts == datetime(2026, 10, 21, 15, 5, tzinfo=UTC)
        for prev, cur in zip(bars, bars[1:], strict=False):
            assert cur.ts > prev.ts
        for bar in bars:
            assert bar.ts.minute % 5 == 0 and bar.ts <= now
            assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
            assert bar.volume > 0
    # stocks: regular-hours bars only; "2 D" = two sessions
    stock_bars = await broker.bars(SPY, "5 mins", "2 D")
    days = {b.ts.astimezone(NY).date() for b in stock_bars}
    assert len(days) == 2
    for bar in stock_bars:
        local = bar.ts.astimezone(NY)
        assert (9, 30) <= (local.hour, local.minute) <= (15, 55)
    assert len(await broker.bars(BTC, "5 mins", "2 D")) == 2 * 288 + 1


async def test_bars_one_minute_aggregate_to_five():
    broker, clock = await make_broker()
    clock.t += timedelta(minutes=20)
    ones = await broker.bars(BTC, "1 min", "3600 S")
    fives = await broker.bars(BTC, "5 mins", "3600 S")
    by_bucket: dict[datetime, list] = {}
    for bar in ones:
        bucket = bar.ts.replace(minute=bar.ts.minute - bar.ts.minute % 5)
        by_bucket.setdefault(bucket, []).append(bar)
    for five in fives:
        group = by_bucket[five.ts]
        assert five.open == group[0].open and five.close == group[-1].close
        assert five.high == max(b.high for b in group) and five.low == min(b.low for b in group)


async def test_stock_lookback_counts_sessions_over_weekend():
    monday_1000 = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
    broker, _ = await make_broker(start=monday_1000)
    bars = await broker.bars(SPY, "15 mins", "2 D")
    assert bars[0].ts == datetime(2026, 10, 16, 13, 30, tzinfo=UTC)  # Friday 09:30
    daily = await broker.bars(SPY, "1 day", "5 D")
    assert [b.ts.astimezone(NY).weekday() for b in daily] == [1, 2, 3, 4, 0]


async def test_bad_bar_size_or_lookback():
    broker, _ = await make_broker()
    with pytest.raises(BrokerError):
        await broker.bars(SPY, "7 mins", "1 D")
    with pytest.raises(BrokerError):
        await broker.bars(SPY, "5 mins", "two days")


async def test_time_advances_prices_and_never_runs_backwards():
    broker, clock = await make_broker()
    first = await broker.quote(BTC)
    broker.advance(3600)
    later = await broker.quote(BTC)
    assert later.ts == first.ts + timedelta(hours=1)
    assert later.last != first.last
    clock.t -= timedelta(hours=5)
    assert (await broker.quote(BTC)).ts == later.ts
    with pytest.raises(ValueError):
        broker.advance(-1)


# ---------------------------------------------------------------------------- orders
async def test_market_buy_pays_ask_plus_slippage_and_commission():
    broker, _ = await make_broker()
    quote = await broker.quote(F)
    rec = await broker.place_order(buy(F, 20))
    assert rec.status == OrderStatus.FILLED
    assert rec.broker_order_id and rec.id is None
    assert rec.avg_fill_price >= quote.ask
    fee = fees.estimate_commission(F, 20, rec.avg_fill_price)  # IBKR Pro Tiered
    assert rec.commission == pytest.approx(fee)
    acct = await broker.account()
    assert acct.cash == pytest.approx(500.0 - 20 * rec.avg_fill_price - fee)
    [pos] = await broker.positions()
    assert pos.instrument == F and pos.qty == 20 and pos.avg_cost == rec.avg_fill_price
    assert pos.stop_loss is None
    [fill] = await broker.fills_since(WED_1100)
    assert fill.side == Side.BUY and fill.qty == 20 and fill.commission == pytest.approx(fee)


async def test_slippage_grows_as_liquidity_shrinks(thin_symbol):
    broker, _ = await make_broker(cash=5000.0)
    paid = {}
    for inst, qty in ((SPY, 1), (THIN, 2)):
        ask = (await broker.quote(inst)).ask
        rec = await broker.place_order(buy(inst, qty))
        paid[inst.symbol] = rec.avg_fill_price / ask - 1
    assert paid["SPY"] < 0.0005
    assert paid["THIN"] > 5 * paid["SPY"]
    thin_quote = await broker.quote(THIN)
    assert (thin_quote.ask - thin_quote.bid) / thin_quote.last > 0.005  # wide spread


async def test_partial_fills_on_thin_volume(thin_symbol):
    broker, _ = await make_broker()
    rec = await broker.place_order(buy(THIN, 30))
    assert rec.status == OrderStatus.PARTIALLY_FILLED
    assert 0 < rec.filled_qty < 30
    broker.advance(60 * 60)
    done = await broker.order_status(rec.broker_order_id)
    assert done.status == OrderStatus.FILLED and done.filled_qty == 30
    fills = await broker.fills_since(WED_1100)
    assert len(fills) > 1
    assert len({f.exec_id for f in fills}) == len(fills)
    assert sum(f.qty for f in fills) == 30
    # Per-order commission: the minimum/maximum applies to the whole order, not each fill.
    assert sum(f.commission for f in fills) == pytest.approx(done.commission)
    expected = fees.estimate_commission(THIN, 30, done.avg_fill_price)
    assert done.commission == pytest.approx(expected, abs=1e-3)


async def test_no_shorting():
    broker, _ = await make_broker()
    rec = await broker.place_order(sell(F, 1))
    assert rec.status == OrderStatus.REJECTED and "short" in rec.message
    await broker.place_order(buy(F, 5))
    assert (await broker.place_order(sell(F, 6))).status == OrderStatus.REJECTED
    resting = await broker.place_order(sell(F, 3, OrderType.LIMIT, limit_price=50.0))
    assert resting.status == OrderStatus.SUBMITTED
    rec = await broker.place_order(sell(F, 3))  # 3 already committed to the resting sell
    assert rec.status == OrderStatus.REJECTED
    assert (await broker.place_order(sell(F, 2))).status == OrderStatus.FILLED


async def test_no_borrowing():
    broker, _ = await make_broker(cash=500.0)
    rec = await broker.place_order(buy(SPY, 1))
    assert rec.status == OrderStatus.REJECTED and "insufficient cash" in rec.message
    first = await broker.place_order(buy(F, 30, OrderType.LIMIT, limit_price=10.0))
    assert first.status == OrderStatus.SUBMITTED  # resting below the market, reserves $300+
    rec = await broker.place_order(buy(F, 20, OrderType.LIMIT, limit_price=10.0))
    assert rec.status == OrderStatus.REJECTED  # reserved cash is not available twice


async def test_resting_limit_fills_only_when_price_trades_through():
    broker, _ = await make_broker()
    rec = await broker.place_order(buy(SPY, 0.5, OrderType.LIMIT, limit_price=590.0))
    assert rec.status == OrderStatus.SUBMITTED
    broker.set_price(SPY, 590.30)
    assert (await broker.order_status(rec.broker_order_id)).status == OrderStatus.SUBMITTED
    broker.set_price(SPY, 589.50)  # gaps through the limit: fills at the better of limit/ask
    done = await broker.order_status(rec.broker_order_id)
    assert done.status == OrderStatus.FILLED
    assert done.avg_fill_price <= 590.0


async def test_resting_limit_fill_matches_the_bars():
    broker, _ = await make_broker(cash=100_000.0)
    start_price = (await broker.quote(BTC)).last
    limit = round(start_price * 0.999, 2)
    rec = await broker.place_order(buy(BTC, 0.01, OrderType.LIMIT, limit_price=limit))
    assert rec.status == OrderStatus.SUBMITTED
    for _ in range(24 * 60):
        broker.advance(60)
        status = await broker.order_status(rec.broker_order_id)
        if status.status == OrderStatus.FILLED:
            break
    assert status.status == OrderStatus.FILLED
    assert status.avg_fill_price == limit
    [fill] = await broker.fills_since(WED_1100)
    bars = await broker.bars(BTC, "1 min", "1 D")
    after = [b for b in bars if b.ts >= WED_1100.replace(second=0)]
    first_through = next(b for b in after if b.low < limit)
    assert first_through.ts <= fill.ts < first_through.ts + timedelta(minutes=1)


async def test_native_bracket_stop_for_stocks():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    stop, target = round(price - 0.30, 2), round(price + 0.60, 2)
    rec = await broker.place_order(buy(F, 20, stop_loss=stop, take_profit=target, setup="orb"))
    assert rec.status == OrderStatus.FILLED
    working = await broker.open_orders()
    assert {o.broker_order_id for o in working} == {
        f"{rec.broker_order_id}-SL",
        f"{rec.broker_order_id}-TP",
    }
    protective = next(o for o in working if o.request.stop_loss == stop)
    assert protective.request.side == Side.SELL and protective.request.is_exit
    assert protective.request.qty == 20

    broker.set_price(F, stop - 0.20)  # gap below the stop
    sl = await broker.order_status(f"{rec.broker_order_id}-SL")
    tp = await broker.order_status(f"{rec.broker_order_id}-TP")
    assert sl.status == OrderStatus.FILLED
    assert sl.avg_fill_price <= stop - 0.20  # gap fill: worse than the stop price
    assert tp.status == OrderStatus.CANCELLED  # OCA
    assert await broker.positions() == []
    assert await broker.open_orders() == []
    acct = await broker.account()
    assert acct.realized_pnl_today < -20 * 0.30
    assert acct.equity == pytest.approx(acct.cash)


async def test_native_bracket_target_fills_at_limit():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    rec = await broker.place_order(
        buy(F, 20, stop_loss=round(price - 0.30, 2), take_profit=round(price + 0.20, 2))
    )
    broker.set_price(F, price + 0.25)
    tp = await broker.order_status(f"{rec.broker_order_id}-TP")
    assert tp.status == OrderStatus.FILLED
    assert tp.avg_fill_price >= round(price + 0.20, 2)
    assert (await broker.order_status(f"{rec.broker_order_id}-SL")).status == OrderStatus.CANCELLED
    assert (await broker.account()).realized_pnl_today > 0


async def test_stop_triggers_on_path_and_matches_bars():
    broker, _ = await make_broker(cash=100_000.0)
    price = (await broker.quote(SPY)).last
    stop = round(price * 0.998, 2)
    rec = await broker.place_order(buy(SPY, 10, stop_loss=stop))
    assert rec.status == OrderStatus.FILLED
    for _ in range(3 * 24 * 60):
        broker.advance(60)
        if not await broker.positions():
            break
    sl = await broker.order_status(f"{rec.broker_order_id}-SL")
    assert sl.status == OrderStatus.FILLED
    assert sl.avg_fill_price < stop
    exit_fill = (await broker.fills_since(WED_1100))[-1]
    minute = exit_fill.ts.replace(second=0, microsecond=0)
    bars = await broker.bars(SPY, "1 min", "3 D")
    bar = next(b for b in bars if b.ts == minute)
    assert bar.low <= stop


async def test_stops_keep_working_while_disconnected():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    rec = await broker.place_order(buy(F, 20, stop_loss=round(price - 0.30, 2)))
    await broker.disconnect()
    broker.set_price(F, price - 1.0)
    await broker.connect()
    assert (await broker.order_status(f"{rec.broker_order_id}-SL")).status == OrderStatus.FILLED
    assert await broker.positions() == []


async def test_invalid_bracket_rejected():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    rec = await broker.place_order(buy(F, 5, stop_loss=price + 0.5))
    assert rec.status == OrderStatus.REJECTED and "stop_loss" in rec.message
    rec = await broker.place_order(buy(F, 5, take_profit=price - 0.5, stop_loss=price - 0.5))
    assert rec.status == OrderStatus.REJECTED and "take_profit" in rec.message
    rec = await broker.place_order(buy(F, 5, OrderType.LIMIT))
    assert rec.status == OrderStatus.REJECTED


async def test_crypto_has_no_native_stops():
    broker, _ = await make_broker(cash=1000.0)
    price = (await broker.quote(BTC)).last
    rec = await broker.place_order(
        buy(BTC, 0.005, stop_loss=price * 0.98, take_profit=price * 1.04)
    )
    assert rec.status == OrderStatus.FILLED
    assert await broker.open_orders() == []
    fill = (await broker.fills_since(WED_1100))[0]
    assert fill.commission == pytest.approx(1.75)  # crypto minimum
    broker.set_price(BTC, price * 0.9)  # nothing protects it: the engine must act
    [pos] = await broker.positions()
    assert pos.qty == pytest.approx(0.005)


async def test_cancel_orders():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    rec = await broker.place_order(
        buy(F, 5, OrderType.LIMIT, limit_price=round(price - 1, 2), stop_loss=round(price - 2, 2))
    )
    assert rec.status == OrderStatus.SUBMITTED
    assert [o.broker_order_id for o in await broker.open_orders()] == [rec.broker_order_id]
    await broker.cancel_order(rec.broker_order_id)
    assert (await broker.order_status(rec.broker_order_id)).status == OrderStatus.CANCELLED
    sl = await broker.order_status(f"{rec.broker_order_id}-SL")
    assert sl.status == OrderStatus.CANCELLED  # unfilled parent takes its children along
    with pytest.raises(BrokerError):
        await broker.cancel_order("nope")
    assert await broker.order_status("nope") is None


async def test_close_position_and_flatten_all():
    broker, _ = await make_broker(cash=2000.0)
    price = (await broker.quote(F)).last
    await broker.place_order(buy(F, 20, stop_loss=round(price - 0.5, 2)))
    await broker.place_order(buy(BTC, 0.01))
    rec = await broker.close_position(F, "test")
    assert rec.status == OrderStatus.FILLED and rec.request.is_exit
    assert [p.instrument for p in await broker.positions()] == [BTC]
    assert await broker.open_orders() == []
    results = await broker.flatten_all()
    assert [r.status for r in results] == [OrderStatus.FILLED]
    assert await broker.positions() == []
    acct = await broker.account()
    assert acct.equity == pytest.approx(acct.cash) and acct.cash < 2000.0  # fees + spread


async def test_stock_orders_wait_for_the_open():
    evening = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)  # 19:00 New York
    broker, clock = await make_broker(start=evening)
    rec = await broker.place_order(buy(F, 10))
    assert rec.status == OrderStatus.SUBMITTED
    clock.t = datetime(2026, 10, 22, 13, 29, tzinfo=UTC)
    assert (await broker.order_status(rec.broker_order_id)).status == OrderStatus.SUBMITTED
    clock.t = datetime(2026, 10, 22, 13, 31, tzinfo=UTC)
    assert (await broker.order_status(rec.broker_order_id)).status == OrderStatus.FILLED
    [fill] = await broker.fills_since(evening)
    assert fill.ts == datetime(2026, 10, 22, 13, 30, tzinfo=UTC)  # at the opening print


async def test_stock_sale_proceeds_settle_next_business_day():
    friday_1100 = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    broker, clock = await make_broker(start=friday_1100)
    await broker.place_order(buy(F, 20))
    await broker.place_order(sell(F, 20))
    acct = await broker.account()
    assert acct.settled_cash < acct.cash  # proceeds unsettled
    assert acct.buying_power == acct.cash  # but usable (no-borrow margin semantics)
    clock.t = datetime(2026, 10, 26, 4, 1, tzinfo=UTC)  # Monday 00:01 New York
    acct = await broker.account()
    assert acct.settled_cash == acct.cash


async def test_fills_since_filters_by_time_and_ids_are_unique():
    broker, clock = await make_broker(cash=1000.0)
    await broker.place_order(buy(F, 5))
    clock.t += timedelta(minutes=10)
    mark = clock.t
    await broker.place_order(buy(F, 5))
    assert len(await broker.fills_since(WED_1100)) == 2
    [late] = await broker.fills_since(mark)
    assert late.ts == mark
    other, _ = await make_broker(start=WED_1100 + timedelta(seconds=1))
    await other.place_order(buy(F, 5))
    ids = {f.exec_id for f in await broker.fills_since(WED_1100)}
    assert not ids & {f.exec_id for f in await other.fills_since(WED_1100)}


async def test_standalone_protective_stop():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    await broker.place_order(buy(F, 20))
    take_profit = await broker.place_order(
        sell(F, 20, OrderType.LIMIT, limit_price=round(price + 1, 2))
    )
    stop = await broker.place_order(sell(F, 20, stop_loss=round(price - 0.30, 2)))
    assert take_profit.status == stop.status == OrderStatus.SUBMITTED
    assert len(await broker.open_orders()) == 2
    broker.set_price(F, price - 0.50)
    assert (await broker.order_status(stop.broker_order_id)).status == OrderStatus.FILLED
    tp = await broker.order_status(take_profit.broker_order_id)
    assert tp.status == OrderStatus.CANCELLED and tp.message == "position closed"
    assert await broker.positions() == []


async def test_long_time_jump_with_working_orders():
    clock = FakeClock(WED_1100)
    broker = SimBroker(seed=7, starting_cash=1000.0, clock=clock, max_history_days=2)
    await broker.connect()
    rec = await broker.place_order(buy(F, 5, OrderType.LIMIT, limit_price=1.0))
    clock.t += timedelta(days=10)  # longer than the retained history
    status = await broker.order_status(rec.broker_order_id)
    assert status.status == OrderStatus.SUBMITTED
    bars = await broker.bars(F, "5 mins", "5 D")
    assert bars[0].ts >= clock.t - timedelta(days=3)  # clamped to the retained history


# ---------------------------------------------------------------------------- quote sizes
async def test_quotes_carry_top_of_book_sizes(thin_symbol):
    broker, _ = await make_broker()
    liquid, thin, btc = [await broker.quote(i) for i in (F, THIN, BTC)]
    for quote in (liquid, thin):
        assert quote.bid_size >= 100 and quote.bid_size % 100 == 0  # round lots
        assert quote.ask_size >= 100 and quote.ask_size % 100 == 0
        assert not quote.halted
    assert liquid.bid_size > 10 * thin.bid_size  # thin names show little size
    assert 0 < btc.bid_size < 100 and 0 < btc.ask_size < 100
    assert liquid.spread_pct < 0.3 < thin.spread_pct


# ---------------------------------------------------------------------------- halts
async def test_halt_freezes_the_quote_and_rejects_entries():
    broker, clock = await make_broker()
    before = await broker.quote(F)
    broker.set_halted("F", True)
    clock.t += timedelta(minutes=12)
    quote = await broker.quote(F)
    assert quote.halted and quote.last == before.last
    assert quote.bid_size == quote.ask_size == 0
    rec = await broker.place_order(buy(F, 5))
    assert rec.status == OrderStatus.REJECTED and "halted" in rec.message
    assert not (await broker.quote(SPY)).halted  # other symbols keep trading
    bars = await broker.bars(F, "1 min", "3600 S")
    assert bars[-1].ts < WED_1100  # no prints during the halt
    broker.set_halted(F, False)
    resumed = await broker.quote(F)
    assert not resumed.halted and resumed.bid_size > 0
    assert (await broker.place_order(buy(F, 5))).status == OrderStatus.FILLED


async def test_stops_wait_for_the_reopen_and_fill_at_the_gap():
    broker, clock = await make_broker()
    price = (await broker.quote(F)).last
    stop = round(price - 0.30, 2)
    rec = await broker.place_order(buy(F, 20, stop_loss=stop))
    broker.set_halted(F, True)
    broker.set_price(F, price - 1.0)  # the book moves, but nothing trades while halted
    clock.t += timedelta(minutes=10)
    sl = await broker.order_status(f"{rec.broker_order_id}-SL")
    assert sl.status == OrderStatus.SUBMITTED
    assert [p.qty for p in await broker.positions()] == [20]
    broker.set_halted(F, False, gap_pct=-35)  # reopens 35% below the halt price
    sl = await broker.order_status(f"{rec.broker_order_id}-SL")
    assert sl.status == OrderStatus.FILLED
    assert sl.avg_fill_price <= (price - 1.0) * 0.65  # far worse than the stop
    assert await broker.positions() == []
    assert (await broker.account()).realized_pnl_today < -20 * (price - stop)


async def test_exits_during_a_halt_wait_for_the_reopen():
    broker, clock = await make_broker()
    price = (await broker.quote(F)).last
    await broker.place_order(buy(F, 20, stop_loss=round(price - 0.30, 2)))
    broker.set_halted(F, True)
    exit_order = await broker.close_position(F, "kill switch")
    assert exit_order.status == OrderStatus.SUBMITTED  # queued, not rejected
    clock.t += timedelta(minutes=5)
    assert [p.qty for p in await broker.positions()] == [20]
    broker.set_halted(F, False, gap_pct=-30)
    done = await broker.order_status(exit_order.broker_order_id)
    assert done.status == OrderStatus.FILLED
    assert done.avg_fill_price <= price * 0.70
    assert await broker.positions() == []


def test_set_halted_validates_the_gap():
    broker = SimBroker(clock=FakeClock(WED_1100))
    with pytest.raises(ValueError):
        broker.set_halted(F, False, gap_pct=-100)


# ---------------------------------------------------------------------------- modify_stop
async def test_modify_stop_raises_the_native_stop():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    stop, target = round(price - 0.30, 2), round(price + 0.60, 2)
    rec = await broker.place_order(buy(F, 20, stop_loss=stop, take_profit=target))
    sl_id = f"{rec.broker_order_id}-SL"
    assert await broker.modify_stop(F, price - 0.104) is True
    moved = round(price - 0.10, 2)  # rounded UP to the tick
    assert (await broker.order_status(sl_id)).request.stop_loss == pytest.approx(moved)
    assert await broker.modify_stop(F, stop) is False  # never lowered
    assert (await broker.order_status(sl_id)).request.stop_loss == pytest.approx(moved)
    assert await broker.modify_stop(F, moved) is True  # already there
    broker.set_price(F, price - 0.20)  # below the new stop, above the original one
    sl = await broker.order_status(sl_id)
    assert sl.status == OrderStatus.FILLED
    assert (await broker.order_status(f"{rec.broker_order_id}-TP")).status == (
        OrderStatus.CANCELLED
    )


async def test_modify_stop_above_the_market_exits_at_once():
    broker, _ = await make_broker()
    price = (await broker.quote(F)).last
    rec = await broker.place_order(buy(F, 20, stop_loss=round(price - 0.30, 2)))
    assert await broker.modify_stop(F, price + 0.05) is True
    assert (await broker.order_status(f"{rec.broker_order_id}-SL")).status == OrderStatus.FILLED
    assert await broker.positions() == []


async def test_modify_stop_needs_a_native_stop_on_a_long():
    broker, _ = await make_broker(cash=2000.0)
    price = (await broker.quote(F)).last
    assert await broker.modify_stop(F, price - 0.1) is False  # no position
    await broker.place_order(buy(F, 5))  # no stop attached
    assert await broker.modify_stop(F, price - 0.1) is False
    await broker.place_order(buy(BTC, 0.005, stop_loss=1000.0))
    assert await broker.modify_stop(BTC, 2000.0) is False  # crypto stops are engine-managed
    pending = await broker.place_order(
        buy(SPY, 1, OrderType.LIMIT, limit_price=700.0, stop_loss=690.0)
    )
    assert pending.status == OrderStatus.SUBMITTED
    assert await broker.modify_stop(SPY, 695.0) is False  # entry not filled yet
    assert await broker.modify_stop(F, float("nan")) is False
    await broker.disconnect()
    assert await broker.modify_stop(F, price - 0.1) is False
