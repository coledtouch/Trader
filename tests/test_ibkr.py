"""IBKRBroker against a small fake of ib_async.IB (offline: no gateway, no network)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from ib_async import (
    AccountValue,
    BarData,
    CommissionReport,
    Contract,
    ContractDetails,
    Crypto,
    Event,
    Execution,
    Option,
    Order,
    PortfolioItem,
    ScanData,
    ScanDataList,
    Stock,
    Ticker,
    Trade,
    TradeLogEntry,
)
from ib_async import Fill as IBFill
from ib_async import OrderStatus as IBOrderStatus
from ib_async import Position as IBPosition

from trader import fees
from trader.broker.base import BrokerError
from trader.broker.ibkr import (
    IBKRBroker,
    MarketDataUnavailable,
    ib_bar_size,
    ib_duration,
    map_ib_status,
    mode_from_account,
    parse_account,
)
from trader.config import AppConfig
from trader.models import (
    AssetClass,
    Instrument,
    Mode,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)

CLIENT_ID = 17
PAPER = "DU1234567"
AAPL = Instrument(symbol="AAPL")
BTC = Instrument(symbol="BTC", asset_class=AssetClass.CRYPTO)
NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
DONE = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def stock(symbol: str = "AAPL", con_id: int = 265598) -> Contract:
    return Stock(symbol, "SMART", "USD", conId=con_id, primaryExchange="NASDAQ")


def crypto(symbol: str = "BTC", con_id: int = 541686651) -> Contract:
    return Crypto(symbol, "ZEROHASH", "USD", conId=con_id)


def log_error(trade: Trade, code: int, text: str) -> None:
    trade.log.append(TradeLogEntry(NOW, trade.orderStatus.status, f"Error {code}: {text}", code))


class FakeClient:
    def __init__(self) -> None:
        self.ready = False
        self.next_id = 100

    def getReqId(self) -> int:
        if not self.ready:
            raise ConnectionError("Not connected")
        self.next_id += 1
        return self.next_id


class FakeIB:
    """Implements just the parts of ib_async.IB that IBKRBroker uses."""

    def __init__(self, accounts: tuple[str, ...] = (PAPER,)) -> None:
        self.errorEvent = Event("errorEvent")
        self.disconnectedEvent = Event("disconnectedEvent")
        self.orderStatusEvent = Event("orderStatusEvent")
        self.client = FakeClient()
        self.accounts = list(accounts)
        self.connected = False
        self.connect_error: BaseException | None = None
        self.connect_hangs = False
        self.connect_calls = 0
        self.market_data_type: int | None = None
        self.summary: list[AccountValue] = []
        self.values: list[AccountValue] = []
        self.portfolio_items: list[PortfolioItem] = []
        self.position_items: list[IBPosition] = []
        self.details = {
            "AAPL": [ContractDetails(contract=stock(), minTick=0.01)],
            "BTC": [
                ContractDetails(
                    contract=crypto(), minTick=0.25, minSize=0.0001, sizeIncrement=0.0001
                )
            ],
        }
        self.ticker_values: dict[int, dict[str, float]] = {}
        self.mkt_requests = 0
        self.mkt_cancels = 0
        self.hist: list[BarData] = []
        self.hist_calls: list[dict[str, object]] = []
        self.placed: list[tuple[Contract, Order]] = []
        self.modified: list[tuple[Contract, Order]] = []
        self.modify_error: tuple[int, str] | None = None  # IB refuses modifications
        self.modify_silent = False  # IB never confirms modifications
        self.restore_on_refresh: dict[int, str] = {}  # orderId -> status IB really has
        self.open_order_refreshes = 0
        self.trade_list: list[Trade] = []
        self.on_place: Callable[[Trade], None] | None = None
        self.cancelled: list[int] = []
        self.global_cancels = 0
        self.all_open: list[Trade] = []
        self.executions: list[IBFill] = []
        self.known_fills: list[IBFill] = []
        self.exec_filter = None
        self.scan_rows: list[ScanData] = []
        self.scan_error: Exception | None = None
        self.scan_cancelled = 0

    # connection
    def isConnected(self) -> bool:
        return self.connected

    async def connectAsync(self, host, port, clientId, **_options):
        self.connect_calls += 1
        if self.connect_hangs:
            await asyncio.sleep(3600)
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = self.client.ready = True
        return self

    def disconnect(self) -> None:
        if self.connected:
            self.connected = self.client.ready = False
            self.disconnectedEvent.emit()

    def managedAccounts(self) -> list[str]:
        return list(self.accounts)

    def reqMarketDataType(self, data_type: int) -> None:
        self.market_data_type = data_type

    # account
    async def accountSummaryAsync(self, account: str = "") -> list[AccountValue]:
        return [v for v in self.summary if v.account == account]

    def accountValues(self, account: str = "") -> list[AccountValue]:
        return [v for v in self.values if v.account == account]

    def portfolio(self, account: str = "") -> list[PortfolioItem]:
        return [p for p in self.portfolio_items if p.account == account]

    def positions(self, account: str = "") -> list[IBPosition]:
        return [p for p in self.position_items if p.account == account]

    # market data
    async def reqContractDetailsAsync(self, contract: Contract) -> list[ContractDetails]:
        return self.details.get(contract.symbol, [])

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False):
        self.mkt_requests += 1
        ticker = Ticker(contract=contract)
        for name, value in self.ticker_values.get(contract.conId, {}).items():
            setattr(ticker, name, value)
        ticker.time = NOW
        return ticker

    def cancelMktData(self, contract: Contract) -> bool:
        self.mkt_cancels += 1
        return True

    async def reqHistoricalDataAsync(self, contract, **kwargs):
        self.hist_calls.append({"contract": contract, **kwargs})
        return list(self.hist)

    # orders
    def placeOrder(self, contract: Contract, order: Order) -> Trade:
        if not self.client.ready:
            raise ConnectionError("Not connected")
        existing = next(
            (t for t in self.trade_list if order.orderId and t.order.orderId == order.orderId),
            None,
        )
        if existing is not None:  # a modification, handled like ib_async + IB would
            assert existing.orderStatus.status not in DONE
            existing.log.append(TradeLogEntry(NOW, existing.orderStatus.status, "Modify"))
            self.modified.append((contract, order))
            if self.modify_error is not None:
                # ib_async marks the order cancelled locally; IB keeps it live.
                self.restore_on_refresh[order.orderId] = existing.orderStatus.status
                existing.orderStatus.status = "Cancelled"
                log_error(existing, *self.modify_error)
            elif not self.modify_silent:
                existing.order.auxPrice = order.auxPrice  # IB echoes openOrder
            return existing
        order.orderId = order.orderId or self.client.getReqId()
        order.clientId = CLIENT_ID
        status = IBOrderStatus(orderId=order.orderId, status="PendingSubmit")
        trade = Trade(contract, order, status, [], [])
        self.trade_list.append(trade)
        self.placed.append((contract, order))
        if order.transmit:  # IB acknowledges the whole group once it is transmitted
            for t in self.trade_list:
                if t.orderStatus.status == "PendingSubmit":
                    t.orderStatus.status = "PreSubmitted" if t.order.parentId else "Submitted"
        if self.on_place is not None:
            self.on_place(trade)
        return trade

    def cancelOrder(self, order: Order) -> Trade | None:
        self.cancelled.append(order.orderId)
        for trade in self.trade_list:
            if trade.order is order and trade.orderStatus.status not in DONE:
                trade.orderStatus.status = "Cancelled"
                log_error(trade, 202, "Order Canceled - reason:")
                return trade
        return None

    def reqGlobalCancel(self) -> None:
        self.global_cancels += 1
        for trade in self.trade_list:
            if trade.orderStatus.status not in DONE:
                trade.orderStatus.status = "Cancelled"

    def trades(self) -> list[Trade]:
        return [*self.trade_list, *self.all_open]

    def openTrades(self) -> list[Trade]:
        return [t for t in self.trades() if t.orderStatus.status not in DONE]

    async def reqAllOpenOrdersAsync(self) -> list[Trade]:
        self.open_order_refreshes += 1
        for trade in self.trade_list:  # IB re-sends orderStatus with the real state
            if trade.order.orderId in self.restore_on_refresh:
                trade.orderStatus.status = self.restore_on_refresh.pop(trade.order.orderId)
        return [*[t for t in self.trade_list if t.orderStatus.status not in DONE], *self.all_open]

    async def reqExecutionsAsync(self, execFilter=None) -> list[IBFill]:
        self.exec_filter = execFilter
        return list(self.executions)

    def fills(self) -> list[IBFill]:
        return list(self.known_fills)

    # scanner
    def reqScannerSubscription(self, subscription) -> ScanDataList:
        if self.scan_error is not None:
            raise self.scan_error
        self.scan_subscription = subscription
        data = ScanDataList()
        data.reqId = 999
        data.extend(self.scan_rows)
        asyncio.get_running_loop().call_soon(data.updateEvent.emit, data)
        return data

    def cancelScannerSubscription(self, data: ScanDataList) -> None:
        self.scan_cancelled += 1


def make_config(**overrides) -> AppConfig:
    return AppConfig(_env_file=None, broker="ibkr", ib_client_id=CLIENT_ID, **overrides)


def make_broker(fake: FakeIB | None = None, **overrides) -> tuple[IBKRBroker, FakeIB]:
    fake = fake or FakeIB()
    broker = IBKRBroker(make_config(**overrides), ib=fake)  # type: ignore[arg-type]
    broker.ORDER_ACK_TIMEOUT = 0.2
    broker.CANCEL_TIMEOUT = 0.2
    broker.QUOTE_TIMEOUT = 0.05
    broker.CONNECT_TIMEOUT = 0.05
    return broker, fake


async def connected_broker(fake: FakeIB | None = None, **overrides) -> tuple[IBKRBroker, FakeIB]:
    broker, fake = make_broker(fake, **overrides)
    await broker.connect()
    return broker, fake


def hold(fake: FakeIB, contract: Contract, qty: float, avg: float = 100.0) -> None:
    fake.position_items.append(IBPosition(PAPER, contract, qty, avg))


def entry(**kw) -> OrderRequest:
    base = dict(
        instrument=AAPL,
        side=Side.BUY,
        qty=3,
        order_type=OrderType.LIMIT,
        limit_price=100.239,
        stop_loss=98.111,
        take_profit=104.004,
    )
    return OrderRequest(**{**base, **kw})


# ---------------------------------------------------------------------- connection / mode
@pytest.mark.parametrize(
    ("account", "mode"),
    [("DU1234567", Mode.PAPER), ("du7654321", Mode.PAPER), ("U1234567", Mode.LIVE)],
)
def test_mode_from_account(account: str, mode: Mode) -> None:
    assert mode_from_account(account) is mode


async def test_connect_paper_sets_market_data_type_and_is_idempotent() -> None:
    broker, fake = await connected_broker(ib_market_data_type=3)
    assert broker.connected
    assert broker.mode is Mode.PAPER
    assert fake.market_data_type == 3
    assert broker.data_delayed
    await broker.connect()
    assert fake.connect_calls == 1


async def test_connect_live_account_is_live_mode() -> None:
    broker, _ = await connected_broker(FakeIB(accounts=("U7654321",)))
    assert broker.mode is Mode.LIVE


def test_mode_before_connect_is_fail_safe_live_unless_pinned() -> None:
    assert make_broker()[0].mode is Mode.LIVE
    assert make_broker(ib_account="DU5550001")[0].mode is Mode.PAPER


async def test_connect_failure_raises_broker_error() -> None:
    broker, fake = make_broker()
    fake.connect_error = ConnectionRefusedError(111, "Connection refused")
    with pytest.raises(BrokerError, match="cannot connect"):
        await broker.connect()
    assert not broker.connected


async def test_connect_timeout_raises_broker_error() -> None:
    broker, fake = make_broker()
    fake.connect_hangs = True
    with pytest.raises(BrokerError, match="TimeoutError"):
        await broker.connect()
    assert not broker.connected


async def test_pinned_account_must_exist() -> None:
    broker, fake = make_broker(ib_account="DU0000001")
    with pytest.raises(BrokerError, match="TRADER_IB_ACCOUNT"):
        await broker.connect()
    assert not fake.connected


async def test_several_accounts_need_a_pin() -> None:
    broker, fake = make_broker(FakeIB(accounts=(PAPER, "DU7654321")))
    with pytest.raises(BrokerError, match="several accounts"):
        await broker.connect()
    pinned, _ = await connected_broker(FakeIB(accounts=(PAPER, "U7654321")), ib_account="U7654321")
    assert pinned.mode is Mode.LIVE


async def test_connectivity_codes_gate_orders_and_reconnect() -> None:
    broker, fake = await connected_broker()
    fake.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
    assert not broker.connected
    with pytest.raises(BrokerError, match="lost its link"):
        await broker.place_order(entry())
    with pytest.raises(BrokerError, match="lost its link"):
        await broker.connect()  # still inside the grace period
    fake.errorEvent.emit(-1, 1102, "Connectivity restored - data maintained.", None)
    assert broker.connected

    fake.errorEvent.emit(-1, 2110, "Connectivity between TWS and server is broken.", None)
    broker.UPSTREAM_GRACE = 0.0
    await broker.connect()  # gives up waiting and reconnects
    assert fake.connect_calls == 2
    assert broker.connected


async def test_data_lost_1101_forces_a_full_resync() -> None:
    """BRK-1: after 1101 ("restored - data lost") IB has dropped the account, position and
    market data subscriptions, so the cached account and positions are frozen. The broker
    reports itself disconnected and refuses data until connect() has reconnected (which
    re-runs ib_async's startup syncs); 1102 ("data maintained") needs no reconnect."""
    broker, fake = await connected_broker()
    fake.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
    fake.errorEvent.emit(-1, 1101, "Connectivity restored - data lost.", None)
    assert not broker.connected
    with pytest.raises(BrokerError, match="1101"):
        await broker.positions()
    with pytest.raises(BrokerError, match="1101"):
        await broker.place_order(entry())
    await broker.connect()
    assert fake.connect_calls == 2  # disconnected and connected again
    assert broker.connected
    await broker.positions()
    await broker.connect()
    assert fake.connect_calls == 2  # resynced once; connected again means idempotent


async def test_delayed_data_codes_mark_data_delayed() -> None:
    """10167 marks that instrument's quotes delayed (which blocks LIVE stock entries in it);
    the whole feed is reported delayed only for market data types 3/4."""
    broker, fake = await connected_broker(ib_market_data_type=1)
    assert not broker.data_delayed
    fake.errorEvent.emit(5, 10167, "Displaying delayed market data.", stock())
    assert not broker.data_delayed  # only AAPL is affected
    fake.ticker_values[stock().conId] = {"last": 101.0}
    quote = await broker.quote(AAPL)
    assert quote.delayed
    delayed_feed, _ = await connected_broker(ib_market_data_type=3)
    assert delayed_feed.data_delayed


async def test_one_unsubscribed_symbol_does_not_block_every_entry() -> None:
    """BRK-4: a single 354 ("not subscribed") for an unrelated symbol under live data
    (type 1) must not mark the whole broker delayed until the next reconnect."""
    broker, fake = await connected_broker(ib_market_data_type=1)
    fake.errorEvent.emit(7, 354, "Requested market data is not subscribed.", stock("ZZZZ", 1))
    assert not broker.data_delayed
    fake.ticker_values[stock().conId] = {"bid": 100.9, "ask": 101.1, "last": 101.0}
    quote = await broker.quote(AAPL)
    assert not quote.delayed and not broker.data_delayed


async def test_disconnect_event_drops_tickers() -> None:
    broker, fake = await connected_broker()
    fake.ticker_values[stock().conId] = {"last": 101.0}
    await broker.quote(AAPL)
    await broker.quote(AAPL)
    assert fake.mkt_requests == 1  # streaming ticker is reused
    fake.disconnect()
    assert not broker.connected
    with pytest.raises(BrokerError):
        await broker.quote(AAPL)
    await broker.connect()
    await broker.quote(AAPL)
    assert fake.mkt_requests == 2


# ---------------------------------------------------------------------- account
def av(tag: str, value: str, currency: str = "USD", account: str = PAPER) -> AccountValue:
    return AccountValue(account, tag, value, currency, "")


async def test_account_parsing() -> None:
    broker, fake = await connected_broker()
    fake.summary = [
        av("NetLiquidation", "512.34"),
        av("NetLiquidation", "470.00", "EUR"),  # other currencies are ignored
        av("TotalCashValue", "300"),
        av("SettledCash", "250.5"),
        av("BuyingPower", "2049.36"),
        av("AvailableFunds", "300"),
        av("DayTradesRemaining", "3", ""),
        av("NetLiquidation", "1.0", account="DU9999999"),  # another account
    ]
    fake.values = [
        av("RealizedPnL", "12.5", "BASE"),
        av("RealizedPnL", "12.5"),
        av("UnrealizedPnL", "-4.25", "BASE"),
        av("NetLiquidation", ""),  # blanks are skipped, summary wins anyway
    ]
    snap = await broker.account()
    assert snap.account_id == PAPER
    assert snap.mode is Mode.PAPER
    assert snap.equity == pytest.approx(512.34)
    assert snap.cash == 300
    assert snap.settled_cash == pytest.approx(250.5)
    assert snap.buying_power == 300  # min(BuyingPower, AvailableFunds): never margin
    assert snap.realized_pnl_today == pytest.approx(12.5)
    assert snap.unrealized_pnl == pytest.approx(-4.25)
    assert snap.day_trades_remaining == 3


@pytest.mark.parametrize(("raw", "expected"), [("-1", None), (None, None), ("0", 0), ("2", 2)])
def test_day_trades_remaining(raw: str | None, expected: int | None) -> None:
    summary = [av("NetLiquidation", "500")]
    if raw is not None:
        summary.append(av("DayTradesRemaining", raw, ""))
    assert parse_account(PAPER, Mode.PAPER, summary).day_trades_remaining == expected


def test_account_without_net_liquidation_fails() -> None:
    with pytest.raises(BrokerError, match="NetLiquidation"):
        parse_account(PAPER, Mode.PAPER, [av("TotalCashValue", "100")])


async def test_account_requires_connection() -> None:
    broker, _ = make_broker()
    with pytest.raises(BrokerError, match="not connected"):
        await broker.account()


async def test_positions_from_portfolio_and_position_stream() -> None:
    broker, fake = await connected_broker()
    fake.portfolio_items = [
        PortfolioItem(stock(), 5.0, 101.5, 507.5, 100.0, 7.5, 0.0, PAPER),
        PortfolioItem(Option("AAPL", "20261120", 200, "C", conId=7), 1.0, 1, 1, 1, 0, 0, PAPER),
        PortfolioItem(stock("F", 9), 0.0, 11.0, 0.0, 11.0, 0.0, 0.0, PAPER),
    ]
    hold(fake, stock(), 5.0)
    hold(fake, crypto(), 0.01, 60000.0)
    positions = {p.instrument.key: p for p in await broker.positions()}
    assert set(positions) == {"STK:AAPL", "CRYPTO:BTC"}
    assert positions["STK:AAPL"].market_price == 101.5
    assert positions["STK:AAPL"].unrealized_pnl == 7.5
    assert positions["CRYPTO:BTC"].qty == 0.01
    assert positions["CRYPTO:BTC"].market_price is None


# ---------------------------------------------------------------------- market data
async def test_quote_handles_nan_and_empty_prices() -> None:
    broker, fake = await connected_broker(ib_market_data_type=3)
    fake.ticker_values[stock().conId] = {"bid": math.nan, "ask": -1.0, "last": 101.5}
    quote = await broker.quote(AAPL)
    assert quote.bid is None and quote.ask is None
    assert quote.last == 101.5 and quote.price == 101.5
    assert quote.delayed
    assert quote.ts == NOW


async def test_quote_live_data_type_and_no_data() -> None:
    broker, fake = await connected_broker(ib_market_data_type=1)
    fake.ticker_values[stock().conId] = {"bid": 100.0, "ask": 100.02}
    quote = await broker.quote(AAPL)
    assert not quote.delayed
    assert quote.mid == pytest.approx(100.01)
    fake.ticker_values[crypto().conId] = {"marketDataType": 3}  # delayed feed, no prices yet
    btc = await broker.quote(BTC)
    assert btc.price is None and btc.delayed


@pytest.mark.parametrize(
    ("halted", "expected"),
    [(math.nan, False), (-1.0, False), (0.0, False), (1.0, True), (2.0, True)],
)
async def test_quote_sizes_and_halt_flag(halted: float, expected: bool) -> None:
    broker, fake = await connected_broker(ib_market_data_type=1)
    fake.ticker_values[stock().conId] = {
        "bid": 100.0,
        "ask": 100.02,
        "bidSize": 300.0,
        "askSize": 500.0,
        "halted": halted,
    }
    quote = await broker.quote(AAPL)
    assert (quote.bid_size, quote.ask_size) == (300.0, 500.0)
    assert quote.halted is expected


async def test_quote_sizes_missing() -> None:
    broker, fake = await connected_broker()
    fake.ticker_values[stock().conId] = {"last": 101.0, "bidSize": math.nan, "askSize": -1.0}
    quote = await broker.quote(AAPL)
    assert quote.bid_size is None and quote.ask_size is None and not quote.halted


async def test_unknown_symbol_raises() -> None:
    broker, _ = await connected_broker()
    with pytest.raises(BrokerError, match="unknown"):
        await broker.quote(Instrument(symbol="ZZZZ"))


@pytest.mark.parametrize(
    ("raw", "ib"),
    [("5 mins", "5 mins"), ("1 min", "1 min"), ("1m", "1 min"), ("15m", "15 mins"),
     ("1 hour", "1 hour"), ("1d", "1 day"), ("30 secs", "30 secs")],
)  # fmt: skip
def test_bar_size_mapping(raw: str, ib: str) -> None:
    assert ib_bar_size(raw) == ib


@pytest.mark.parametrize(
    ("raw", "ib"),
    [("2 D", "2 D"), ("2D", "2 D"), ("3 days", "3 D"), ("90 min", "5400 S"), ("1 W", "1 W"),
     ("1 M", "1 M"), ("2 hours", "7200 S")],
)  # fmt: skip
def test_duration_mapping(raw: str, ib: str) -> None:
    assert ib_duration(raw) == ib


@pytest.mark.parametrize("bad", ["7 mins", "fast", "2 days", "1 parsec"])
def test_bad_bar_size_raises(bad: str) -> None:
    with pytest.raises(BrokerError, match="bar size"):
        ib_bar_size(bad)


@pytest.mark.parametrize("bad", ["0 D", "soon", "1 parsec"])
def test_bad_lookback_raises(bad: str) -> None:
    with pytest.raises(BrokerError, match="lookback"):
        ib_duration(bad)


async def test_bars_convert_to_utc_and_use_asset_specific_settings() -> None:
    broker, fake = await connected_broker()
    t0 = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
    fake.hist = [
        BarData(date=t0 + timedelta(minutes=5), open=2, high=3, low=1, close=2.5, volume=-1),
        BarData(date=t0, open=1, high=2, low=0.5, close=1.5, volume=1000),
        BarData(date=t0 + timedelta(minutes=10), open=math.nan, high=3, low=1, close=2),
    ]
    bars = await broker.bars(AAPL, "5 mins", "2 D")
    assert [b.ts for b in bars] == [t0, t0 + timedelta(minutes=5)]
    assert bars[1].volume == 0.0
    call = fake.hist_calls[-1]
    assert call["whatToShow"] == "TRADES" and call["useRTH"] is True
    assert call["durationStr"] == "2 D" and call["barSizeSetting"] == "5 mins"
    assert call["formatDate"] == 2

    await broker.bars(BTC, "1 min", "1 D")
    call = fake.hist_calls[-1]
    assert call["whatToShow"] == "AGGTRADES" and call["useRTH"] is False
    assert call["contract"].exchange == "ZEROHASH"


@pytest.mark.parametrize(("configured", "venue"), [("ZEROHASH", "ZEROHASH"), (" paxos ", "PAXOS")])
async def test_crypto_contract_uses_the_configured_exchange(configured: str, venue: str) -> None:
    broker, fake = await connected_broker(ib_crypto_exchange=configured)
    looked_up: list[Contract] = []
    lookup = fake.reqContractDetailsAsync

    async def spy(contract: Contract) -> list[ContractDetails]:
        looked_up.append(contract)
        return await lookup(contract)

    fake.reqContractDetailsAsync = spy  # type: ignore[method-assign]
    await broker.quote(BTC)
    assert [(c.secType, c.exchange) for c in looked_up] == [("CRYPTO", venue)]


# ---------------------------------------------------------------------- order construction
async def test_stock_entry_is_a_protected_bracket() -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(entry())
    assert rec.status is OrderStatus.SUBMITTED
    assert len(fake.placed) == 3
    (contract, parent), (_, stop), (_, target) = fake.placed
    assert contract.exchange == "SMART" and contract.conId == stock().conId
    assert rec.broker_order_id == str(parent.orderId)

    assert (parent.action, parent.orderType, parent.tif) == ("BUY", "LMT", "DAY")
    assert parent.lmtPrice == 100.23  # rounded DOWN to the tick: never pay more
    assert parent.totalQuantity == 3 and not parent.transmit and parent.parentId == 0

    assert (stop.action, stop.orderType, stop.tif) == ("SELL", "STP", "GTC")
    assert stop.auxPrice == 98.12  # rounded UP: never risk more than planned
    assert stop.parentId == parent.orderId and not stop.transmit

    assert (target.action, target.orderType, target.tif) == ("SELL", "LMT", "GTC")
    assert target.lmtPrice == 104.01
    assert target.parentId == parent.orderId and target.transmit  # last one transmits

    assert stop.ocaGroup and stop.ocaGroup == target.ocaGroup
    assert stop.ocaType == target.ocaType == 2
    assert {o.account for _, o in fake.placed} == {PAPER}
    assert broker.supports_native_stops(AAPL)


async def test_stock_entry_without_target_transmits_with_the_stop() -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(
        entry(order_type=OrderType.MARKET, limit_price=None, take_profit=None)
    )
    assert rec.status is OrderStatus.SUBMITTED
    (_, parent), (_, stop) = fake.placed
    assert parent.orderType == "MKT" and not parent.transmit
    assert stop.orderType == "STP" and stop.transmit and stop.parentId == parent.orderId


async def test_stock_exit_is_a_single_order() -> None:
    broker, fake = await connected_broker()
    hold(fake, stock(), 5)
    rec = await broker.place_order(
        OrderRequest(
            instrument=AAPL, side=Side.SELL, qty=5, order_type=OrderType.MARKET, is_exit=True
        )
    )
    assert rec.status is OrderStatus.SUBMITTED
    [(_, order)] = fake.placed
    assert (order.action, order.orderType, order.tif) == ("SELL", "MKT", "DAY")
    assert order.transmit and order.parentId == 0 and not order.ocaGroup


async def test_exit_with_stop_loss_is_a_resting_gtc_stop_not_a_market_sell() -> None:
    broker, fake = await connected_broker()
    hold(fake, stock(), 5)
    assert broker.supports_standalone_stops(AAPL) and not broker.supports_standalone_stops(BTC)
    rec = await broker.place_order(
        OrderRequest(
            instrument=AAPL,
            side=Side.SELL,
            qty=5,
            order_type=OrderType.MARKET,
            stop_loss=98.123,
            is_exit=True,
            reason="protective stop (re-placed)",
        )
    )
    assert rec.status is OrderStatus.SUBMITTED
    [(_, order)] = fake.placed
    assert (order.action, order.orderType, order.tif) == ("SELL", "STP", "GTC")
    assert order.auxPrice == 98.13  # rounded UP to the tick: never risk more
    assert order.transmit and order.parentId == 0
    [working] = await broker.open_orders()
    assert working.request.stop_loss == 98.123 and working.request.is_exit


@pytest.mark.parametrize(
    ("instrument", "stop", "fragment"),
    [(BTC, 59000.0, "no stop orders for crypto"), (AAPL, -1.0, "positive stop_loss")],
)
async def test_standalone_stop_that_cannot_rest_is_rejected(
    instrument: Instrument, stop: float, fragment: str
) -> None:
    broker, fake = await connected_broker()
    hold(fake, crypto() if instrument == BTC else stock(), 1)
    rec = await broker.place_order(
        OrderRequest(
            instrument=instrument,
            side=Side.SELL,
            qty=1,
            order_type=OrderType.MARKET,
            stop_loss=stop,
            is_exit=True,
        )
    )
    assert rec.status is OrderStatus.REJECTED and fragment in rec.message
    assert fake.placed == []


async def test_exit_larger_than_position_is_rejected() -> None:
    broker, fake = await connected_broker()
    hold(fake, stock(), 2)
    rec = await broker.place_order(
        OrderRequest(
            instrument=AAPL, side=Side.SELL, qty=5, order_type=OrderType.MARKET, is_exit=True
        )
    )
    assert rec.status is OrderStatus.REJECTED and "short" in rec.message
    assert fake.placed == []


async def test_exit_guard_uses_the_smaller_of_lagging_position_streams() -> None:
    broker, fake = await connected_broker()
    fake.portfolio_items = [PortfolioItem(stock(), 5.0, 101.0, 505.0, 100.0, 5.0, 0.0, PAPER)]
    hold(fake, stock(), 3)

    def sell(qty: float) -> OrderRequest:
        return OrderRequest(
            instrument=AAPL, side=Side.SELL, qty=qty, order_type=OrderType.MARKET, is_exit=True
        )

    assert (await broker.place_order(sell(5))).status is OrderStatus.REJECTED
    assert (await broker.place_order(sell(3))).status is OrderStatus.SUBMITTED


@pytest.mark.parametrize(
    ("request_kw", "fragment"),
    [
        ({"side": Side.SELL}, "short selling"),
        ({"side": Side.BUY, "is_exit": True}, "must be SELL"),
        ({"stop_loss": None}, "stop_loss is required"),
        ({"stop_loss": 101.0}, "below the limit"),
        ({"take_profit": 99.0}, "above the limit"),
        ({"qty": 1.5}, "fractional"),
        ({"limit_price": None}, "positive limit_price"),
        ({"stop_loss": 100.235}, "within one tick"),
    ],
)
async def test_invalid_requests_are_rejected_without_sending(
    request_kw: dict, fragment: str
) -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(entry(**request_kw))
    assert rec.status is OrderStatus.REJECTED
    assert fragment in rec.message
    assert fake.placed == []


async def test_broker_rejection_of_parent_maps_to_rejected() -> None:
    broker, fake = await connected_broker()

    def reject_parent(trade: Trade) -> None:
        if trade.order.transmit:
            parent = fake.trade_list[0]
            parent.orderStatus.status = "Cancelled"
            log_error(parent, 201, "Order rejected - reason: insufficient funds")

    fake.on_place = reject_parent
    rec = await broker.place_order(entry())
    assert rec.status is OrderStatus.REJECTED
    assert "insufficient funds" in rec.message


async def test_rejected_protective_order_cancels_the_entry() -> None:
    broker, fake = await connected_broker()

    def reject_stop(trade: Trade) -> None:
        if trade.order.orderType == "STP":
            trade.orderStatus.status = "Inactive"
            log_error(trade, 201, "Order rejected - reason: invalid stop price")

    fake.on_place = reject_stop
    rec = await broker.place_order(entry())
    assert rec.status is OrderStatus.REJECTED
    assert "protective order rejected" in rec.message and "invalid stop price" in rec.message
    assert fake.cancelled == [fake.placed[0][1].orderId]


async def test_order_placement_disconnect_raises_broker_error() -> None:
    broker, fake = await connected_broker()
    fake.connected = fake.client.ready = False
    with pytest.raises(BrokerError):
        await broker.place_order(entry())


async def test_unknown_contract_order_is_rejected() -> None:
    broker, _ = await connected_broker()
    rec = await broker.place_order(entry(instrument=Instrument(symbol="ZZZZ")))
    assert rec.status is OrderStatus.REJECTED and "unknown" in rec.message


async def test_crypto_orders_follow_ibkr_crypto_rules() -> None:
    broker, fake = await connected_broker()
    assert not broker.supports_native_stops(BTC)
    rec = await broker.place_order(
        OrderRequest(
            instrument=BTC, side=Side.BUY, qty=0.0123, limit_price=60010.6, stop_loss=59000.0
        )
    )
    assert rec.status is OrderStatus.SUBMITTED
    [(contract, order)] = fake.placed  # no native children: the engine manages crypto stops
    assert contract.exchange == "ZEROHASH"
    assert (order.action, order.orderType, order.tif) == ("BUY", "LMT", "DAY")
    assert order.lmtPrice == 60010.5 and order.totalQuantity == 0.0123

    fake.placed.clear()
    fake.ticker_values[crypto().conId] = {"bid": 59990.0, "ask": 60000.0}
    await broker.place_order(
        OrderRequest(
            instrument=BTC, side=Side.BUY, qty=0.01, order_type=OrderType.MARKET, stop_loss=59000.0
        )
    )
    [(_, mkt)] = fake.placed
    assert (mkt.orderType, mkt.tif) == ("MKT", "IOC")
    assert mkt.cashQty == 600.0 and mkt.totalQuantity == 0

    fake.placed.clear()
    hold(fake, crypto(), 0.01, 60000.0)
    await broker.place_order(
        OrderRequest(
            instrument=BTC, side=Side.SELL, qty=0.01, order_type=OrderType.MARKET, is_exit=True
        )
    )
    [(_, sell)] = fake.placed
    assert (sell.action, sell.orderType, sell.tif, sell.totalQuantity) == (
        "SELL",
        "MKT",
        "IOC",
        0.01,
    )


async def test_crypto_entry_below_minimum_size_is_rejected() -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(
        OrderRequest(instrument=BTC, side=Side.BUY, qty=0.00005, limit_price=60000, stop_loss=59000)
    )
    assert rec.status is OrderStatus.REJECTED and "minimum size" in rec.message
    assert fake.placed == []


# ---------------------------------------------------------------------- status / orders
def make_trade(status: str, filled: float = 0.0, error: tuple[int, str] | None = None) -> Trade:
    trade = Trade(
        stock(),
        Order(orderId=5, clientId=CLIENT_ID, action="BUY", totalQuantity=3, orderType="LMT"),
        IBOrderStatus(orderId=5, status=status, filled=filled),
    )
    if error:
        log_error(trade, *error)
    return trade


@pytest.mark.parametrize(
    ("status", "filled", "error", "cancel_requested", "expected"),
    [
        ("PendingSubmit", 0, None, False, OrderStatus.SUBMITTED),
        ("PreSubmitted", 0, None, False, OrderStatus.SUBMITTED),
        ("Submitted", 0, None, False, OrderStatus.SUBMITTED),
        ("Submitted", 1, None, False, OrderStatus.PARTIALLY_FILLED),
        ("PendingCancel", 0, None, True, OrderStatus.SUBMITTED),
        ("Filled", 3, None, False, OrderStatus.FILLED),
        ("Cancelled", 0, None, False, OrderStatus.CANCELLED),
        ("Cancelled", 0, (202, "Order Canceled - reason:"), False, OrderStatus.CANCELLED),
        ("Cancelled", 0, (201, "Order rejected - reason: x"), False, OrderStatus.REJECTED),
        ("Cancelled", 0, (201, "Order rejected - reason: x"), True, OrderStatus.CANCELLED),
        ("Cancelled", 1, (201, "Order rejected - reason: x"), False, OrderStatus.CANCELLED),
        ("ApiCancelled", 0, None, True, OrderStatus.CANCELLED),
        ("Inactive", 0, None, False, OrderStatus.REJECTED),
    ],
)
def test_status_mapping(status, filled, error, cancel_requested, expected) -> None:
    mapped, message = map_ib_status(make_trade(status, filled, error), cancel_requested)
    assert mapped is expected
    if expected is OrderStatus.REJECTED:
        assert message


async def test_open_orders_include_children_and_other_clients() -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(entry())
    manual = Trade(
        stock(),
        Order(orderId=0, clientId=0, permId=555, action="SELL", totalQuantity=3,
              orderType="STP", auxPrice=95.0, account=PAPER),
        IBOrderStatus(status="Submitted"),
    )  # fmt: skip
    other_account = Trade(
        stock(),
        Order(orderId=0, clientId=0, permId=556, action="SELL", totalQuantity=1,
              orderType="LMT", lmtPrice=120.0, account="DU9999999"),
        IBOrderStatus(status="Submitted"),
    )  # fmt: skip
    fake.all_open = [manual, other_account]
    orders = {o.broker_order_id: o for o in await broker.open_orders()}
    parent_id = int(rec.broker_order_id or 0)
    assert set(orders) == {str(parent_id), str(parent_id + 1), str(parent_id + 2), "perm:555"}
    parent = orders[str(parent_id)]
    assert parent.request.side is Side.BUY and not parent.request.is_exit
    stop, target = orders[str(parent_id + 1)], orders[str(parent_id + 2)]
    assert stop.request.is_exit and stop.request.side is Side.SELL
    assert stop.request.stop_loss == 98.12
    assert target.request.is_exit and target.request.limit_price == 104.01
    assert orders["perm:555"].request.is_exit and orders["perm:555"].request.stop_loss == 95.0


async def test_order_status_and_cancel() -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(entry())
    assert rec.broker_order_id is not None
    status = await broker.order_status(rec.broker_order_id)
    assert status is not None and status.status is OrderStatus.SUBMITTED
    assert status.request == rec.request
    await broker.cancel_order(rec.broker_order_id)
    assert fake.cancelled == [int(rec.broker_order_id)]
    status = await broker.order_status(rec.broker_order_id)
    assert status is not None and status.status is OrderStatus.CANCELLED
    assert await broker.order_status("424242") is None
    assert await broker.order_status("not-an-id") is None


async def test_order_status_fill() -> None:
    broker, fake = await connected_broker()
    rec = await broker.place_order(entry())
    parent = fake.trade_list[0]
    parent.orderStatus.status = "Filled"
    parent.orderStatus.filled = 3
    parent.orderStatus.avgFillPrice = 100.2
    status = await broker.order_status(rec.broker_order_id or "")
    assert status is not None
    assert status.status is OrderStatus.FILLED
    assert status.filled_qty == 3 and status.avg_fill_price == 100.2


async def test_cancel_all_uses_global_cancel() -> None:
    broker, fake = await connected_broker()
    await broker.place_order(entry())
    await broker.cancel_all()
    assert fake.global_cancels == 1
    assert fake.openTrades() == []


# ---------------------------------------------------------------------- fills
def execution(exec_id: str, side: str, ts: datetime, **kw) -> Execution:
    return Execution(execId=exec_id, time=ts, acctNumber=PAPER, side=side, **kw)


async def test_fills_since() -> None:
    broker, fake = await connected_broker()
    since = NOW - timedelta(hours=2)
    own = IBFill(
        stock(),
        execution(
            "e1", "BOT", NOW, shares=3, price=100.2, orderId=101, clientId=CLIENT_ID, permId=9
        ),
        CommissionReport(),
        NOW,
    )
    fake.executions = [
        own,
        IBFill(
            stock(),
            execution("e2", "SLD", NOW + timedelta(minutes=1), shares=3, price=101.0,
                      orderId=0, clientId=0, permId=77),
            CommissionReport(execId="e2", commission=1.0),
            NOW,
        ),
        IBFill(stock(), execution("e0", "BOT", since - timedelta(minutes=1), shares=1, price=99),
               CommissionReport(), NOW),
        IBFill(Option("AAPL", "20261120", 200, "C"), execution("e3", "BOT", NOW, shares=1, price=2),
               CommissionReport(), NOW),
    ]  # fmt: skip
    # The commission report arrived on ib_async's own fill object for e1.
    fake.known_fills = [
        own._replace(commissionReport=CommissionReport(execId="e1", commission=1.0))
    ]
    fills = await broker.fills_since(since)
    assert [f.exec_id for f in fills] == ["e1", "e2"]
    first, second = fills
    assert (first.side, first.qty, first.price, first.commission) == (Side.BUY, 3, 100.2, 1.0)
    assert first.broker_order_id == "101"
    assert second.side is Side.SELL and second.broker_order_id == "perm:77"
    assert first.ts.tzinfo is not None
    assert fake.exec_filter.acctCode == PAPER
    assert fake.exec_filter.time == (since - timedelta(hours=1)).strftime("%Y%m%d-%H:%M:%S")


async def test_fills_since_keeps_only_the_latest_correction() -> None:
    """BRK-6: IBKR reports a corrected execution with the same id except a higher number
    after the last period; only the latest revision is a fill (never both)."""
    broker, fake = await connected_broker()
    base = "0000e0d5.65a1b2c3.01"

    def ex(rev: str, price: float) -> IBFill:
        return IBFill(
            stock(),
            execution(f"{base}.{rev}", "BOT", NOW, shares=10, price=price, orderId=101,
                      clientId=CLIENT_ID, permId=9),
            CommissionReport(),
            NOW,
        )  # fmt: skip

    fake.executions = [ex("01", 100.0), ex("02", 100.05)]
    [fill] = await broker.fills_since(NOW - timedelta(hours=1))
    assert fill.exec_id == f"{base}.02" and fill.price == 100.05 and fill.qty == 10


async def test_order_completed_while_the_app_was_down_is_found() -> None:
    """BRK-5: after a restart, an order that filled while the app was down comes back only
    as a completed order without orderId/clientId (ib_async keys it by permId). Its
    executions carry all three ids, so order_status finds it as FILLED, not unknown."""
    broker, fake = await connected_broker()
    completed = Trade(
        stock(),
        Order(orderId=0, clientId=0, permId=777, action="BUY", totalQuantity=3,
              orderType="LMT", lmtPrice=100.0, account=PAPER),
        IBOrderStatus(status="Filled", filled=3, avgFillPrice=99.9),
    )  # fmt: skip
    fake.trade_list = [completed]
    fake.known_fills = [
        IBFill(
            stock(),
            execution("e9", "BOT", NOW, shares=3, price=99.9, orderId=123, clientId=CLIENT_ID,
                      permId=777),
            CommissionReport(),
            NOW,
        )
    ]  # fmt: skip
    status = await broker.order_status("123")
    assert status is not None and status.status is OrderStatus.FILLED
    assert status.filled_qty == 3
    assert await broker.order_status("124") is None


# ---------------------------------------------------------------------- costs / sizing
@pytest.mark.parametrize(
    ("plan", "qty", "price", "side", "expected"),
    [
        # Tiered: max($0.35, $0.0035/share) + ~$0.0032/share exchange and clearing
        ("tiered", 10, 100.0, Side.BUY, 0.35 + 10 * 0.0032),
        ("tiered", 1000, 50.0, Side.BUY, 3.5 + 1000 * 0.0032),
        # sells add the SEC fee and FINRA TAF
        ("tiered", 10, 100.0, Side.SELL, 0.35 + 10 * 0.0032 + 1000 * 0.0000278 + 10 * 0.000166),
        # Fixed: $0.005/share, min $1.00, max 1% of value
        ("fixed", 100, 10.0, Side.BUY, 1.0),
        ("fixed", 1000, 50.0, Side.BUY, 5.0),
        ("fixed", 10, 5.0, Side.BUY, 0.5),
        ("fixed", 0, 10.0, Side.BUY, 0.0),
    ],
)
async def test_stock_commission_uses_the_configured_plan(
    plan: str, qty: float, price: float, side: Side, expected: float
) -> None:
    broker, _ = make_broker(commission_plan=plan)
    assert broker.estimate_commission(AAPL, qty, price, side) == pytest.approx(expected, abs=1e-4)
    assert broker.estimate_commission(AAPL, qty, price, side) == fees.estimate_commission(
        AAPL, qty, price, side, plan
    )


@pytest.mark.parametrize(
    ("qty", "price", "expected"),
    [(0.01, 60000.0, 1.75), (1.0, 10000.0, 18.0), (0.001, 100000.0, 1.0)],
)
def test_crypto_commission(qty: float, price: float, expected: float) -> None:
    broker, _ = make_broker()
    assert broker.estimate_commission(BTC, qty, price) == pytest.approx(expected)
    assert broker.estimate_commission(BTC, qty, price, Side.SELL) == pytest.approx(expected)


async def test_estimate_commission_and_round_qty() -> None:
    broker, _ = await connected_broker()
    assert broker.estimate_commission(AAPL, 10, 100.0) == pytest.approx(0.382)  # tiered default
    assert broker.estimate_commission(BTC, 0.01, 60000.0) == pytest.approx(1.75)
    assert broker.round_qty(AAPL, 3.99, allow_fractional=True) == 3.0  # whole shares only
    assert broker.round_qty(BTC, 0.0123456) == pytest.approx(0.012345)  # default 6 dp
    await broker.quote(BTC)  # qualifies the contract: size increment 0.0001 now known
    assert broker.round_qty(BTC, 0.0123456) == pytest.approx(0.0123)
    assert broker.round_qty(BTC, 0.00009) == 0.0
    assert broker.round_qty(BTC, -1) == 0.0


# ---------------------------------------------------------------------- scanner
async def test_scan_movers() -> None:
    broker, fake = await connected_broker()
    fake.scan_rows = [
        ScanData(1, ContractDetails(contract=stock("SOFI", 2)), "", "", "", ""),
        ScanData(0, ContractDetails(contract=stock("PLTR", 3)), "", "", "", ""),
        ScanData(2, ContractDetails(contract=Option("X", "20261120", 1, "C")), "", "", "", ""),
        ScanData(3, ContractDetails(contract=stock("PLTR", 3)), "", "", "", ""),
    ]
    assert await broker.scan_movers(limit=5) == ["PLTR", "SOFI"]
    sub = fake.scan_subscription
    assert (sub.scanCode, sub.locationCode, sub.instrument) == (
        "TOP_PERC_GAIN",
        "STK.US.MAJOR",
        "STK",
    )
    assert (sub.abovePrice, sub.belowPrice, sub.aboveVolume) == (2.0, 50.0, 1_000_000)
    assert fake.scan_cancelled == 1


async def test_scan_movers_returns_empty_on_errors() -> None:
    broker, fake = make_broker()
    assert await broker.scan_movers() == []  # not connected
    await broker.connect()
    assert await broker.scan_movers(limit=0) == []
    assert not hasattr(fake, "scan_subscription")  # nothing requested
    fake.scan_error = ConnectionError("Not connected")
    assert await broker.scan_movers() == []


# ---------------------------------------------------------------------- stop modification
async def filled_bracket(**overrides) -> tuple[IBKRBroker, FakeIB, Trade]:
    """A filled stock entry whose GTC stop child (at 98.12) is working; returns its trade."""
    broker, fake = await connected_broker(**overrides)
    await broker.place_order(entry())
    parent, stop, _ = fake.trade_list
    parent.orderStatus.status = "Filled"
    parent.orderStatus.filled = 3
    hold(fake, stock(), 3)
    assert stop.order.orderType == "STP" and stop.order.auxPrice == 98.12
    return broker, fake, stop


async def test_modify_stop_moves_the_bracket_stop_up_in_place() -> None:
    broker, fake, stop = await filled_bracket()
    assert await broker.modify_stop(AAPL, 100.234) is True
    [(contract, sent)] = fake.modified
    assert contract.conId == stock().conId
    assert sent.orderId == stop.order.orderId and sent is not stop.order  # same id: a modify
    assert (sent.orderType, sent.action, sent.tif) == ("STP", "SELL", "GTC")
    assert sent.auxPrice == 100.24  # rounded UP to the tick
    assert sent.transmit  # the staged child must be transmitted
    assert (sent.parentId, sent.ocaGroup, sent.totalQuantity) == (
        stop.order.parentId,
        stop.order.ocaGroup,
        3,
    )
    assert stop.order.auxPrice == 100.24  # IB's echo
    orders = {o.broker_order_id: o for o in await broker.open_orders()}
    assert orders[str(stop.order.orderId)].request.stop_loss == 100.24


async def test_modify_stop_never_lowers_and_skips_no_ops() -> None:
    broker, fake, stop = await filled_bracket()
    assert await broker.modify_stop(AAPL, 97.5) is False  # below the current stop
    assert await broker.modify_stop(AAPL, 98.115) is True  # rounds to the current 98.12
    assert fake.modified == []
    assert stop.order.auxPrice == 98.12


async def test_modify_stop_refused_by_ibkr_keeps_the_old_stop() -> None:
    broker, fake, stop = await filled_bracket()
    fake.modify_error = (201, "Order rejected - reason: invalid stop price")
    refreshes = fake.open_order_refreshes
    assert await broker.modify_stop(AAPL, 100.0) is False
    assert stop.order.auxPrice == 98.12
    assert fake.open_order_refreshes == refreshes + 1  # resynced the real order state
    orders = {o.broker_order_id: o for o in await broker.open_orders()}
    assert orders[str(stop.order.orderId)].request.stop_loss == 98.12  # still protecting


async def test_modify_stop_unconfirmed_is_false() -> None:
    broker, fake, stop = await filled_bracket()
    fake.modify_silent = True
    assert await broker.modify_stop(AAPL, 100.0) is False
    assert len(fake.modified) == 1 and stop.order.auxPrice == 98.12


async def test_modify_stop_needs_an_own_working_stop_on_a_long() -> None:
    broker, fake = await connected_broker()
    assert await broker.modify_stop(AAPL, 100.0) is False  # nothing held, no stop
    hold(fake, stock(), 3)
    fake.all_open = [
        Trade(
            stock(),
            Order(orderId=0, clientId=0, permId=555, action="SELL", totalQuantity=3,
                  orderType="STP", auxPrice=95.0, account=PAPER),
            IBOrderStatus(status="PreSubmitted"),
        )
    ]  # fmt: skip
    assert await broker.modify_stop(AAPL, 100.0) is False  # placed by another client
    assert await broker.modify_stop(BTC, 60000.0) is False  # no native crypto stops
    assert await broker.modify_stop(AAPL, math.nan) is False
    assert fake.modified == []


async def test_modify_stop_without_a_position_or_connection() -> None:
    broker, fake = await connected_broker()
    await broker.place_order(entry())  # parent not filled, nothing held
    assert await broker.modify_stop(AAPL, 100.0) is False
    _, stop, _ = fake.trade_list
    hold(fake, stock(), 3)
    stop.orderStatus.status = "Cancelled"
    assert await broker.modify_stop(AAPL, 100.0) is False  # no working stop left
    fake.disconnect()
    assert await broker.modify_stop(AAPL, 100.0) is False  # fail safe, no exception
    assert fake.modified == []


# ---------------------------------------------------------------------- error 10197
def flag_competing(fake: FakeIB, contract: Contract | None = None) -> None:
    fake.errorEvent.emit(9, 10197, "No market data during competing live session", contract)


async def test_competing_session_makes_quotes_unusable_until_fresh_data() -> None:
    broker, fake = await connected_broker(ib_market_data_type=1)
    broker._clock = lambda: NOW + timedelta(seconds=1)
    fake.ticker_values[stock().conId] = {"bid": 100.0, "ask": 100.02, "last": 100.01}
    assert (await broker.quote(AAPL)).bid == 100.0
    assert broker.data_warning is None

    flag_competing(fake, stock())  # the ticker still holds the old (now stale) prices
    with pytest.raises(MarketDataUnavailable, match="error 10197") as caught:
        await broker.quote(AAPL)
    assert isinstance(caught.value, BrokerError)  # the engine skips the symbol
    warning = broker.data_warning or ""
    assert "10197" in warning and "no entries for AAPL" in warning and "Log out" in warning

    broker._tickers["STK:AAPL"].ticker.time = NOW + timedelta(seconds=5)  # a fresh tick
    quote = await broker.quote(AAPL)
    assert quote.bid == 100.0
    assert broker.data_warning is None


async def test_competing_session_resubscribes_to_find_out_when_data_is_back() -> None:
    broker, fake = await connected_broker(ib_market_data_type=1)
    broker._clock = lambda: NOW + timedelta(seconds=1)
    fake.ticker_values[stock().conId] = {"last": 100.0}
    await broker.quote(AAPL)
    flag_competing(fake, stock())
    broker.TICKER_RETRY_AFTER = 0.0
    with pytest.raises(MarketDataUnavailable):
        await broker.quote(AAPL)  # re-subscribed, but nothing new arrived yet
    assert fake.mkt_requests == 2 and fake.mkt_cancels == 1
    broker.TICKER_RETRY_AFTER = 60.0
    broker._tickers["STK:AAPL"].ticker.time = NOW + timedelta(minutes=1)
    assert (await broker.quote(AAPL)).last == 100.0


async def test_competing_session_without_contract_flags_every_subscription() -> None:
    broker, fake = await connected_broker(ib_market_data_type=1)
    broker._clock = lambda: NOW + timedelta(seconds=1)
    fake.ticker_values[stock().conId] = {"last": 100.0}
    fake.ticker_values[crypto().conId] = {"last": 81_700.0}
    await broker.quote(AAPL)
    await broker.quote(BTC)
    flag_competing(fake, None)
    for inst in (AAPL, BTC):
        with pytest.raises(MarketDataUnavailable):
            await broker.quote(inst)
    assert "AAPL, BTC" in (broker.data_warning or "")


async def test_competing_warning_expires_and_reconnect_clears_it() -> None:
    broker, fake = await connected_broker(ib_market_data_type=1)
    now = {"t": NOW + timedelta(seconds=1)}
    broker._clock = lambda: now["t"]
    fake.ticker_values[stock().conId] = {"last": 100.0}
    await broker.quote(AAPL)
    flag_competing(fake, stock())
    now["t"] += timedelta(minutes=16)
    assert broker.data_warning is None  # old news for the dashboard...
    with pytest.raises(MarketDataUnavailable):
        await broker.quote(AAPL)  # ...but still no fresh data, so still unusable
    fake.disconnect()
    await broker.connect()
    now["t"] = NOW - timedelta(seconds=1)
    assert (await broker.quote(AAPL)).last == 100.0  # new session, new subscription
