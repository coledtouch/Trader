"""Default behavior of the Broker base class (close_position, flatten_all, round_qty)."""

from __future__ import annotations

from datetime import datetime

import pytest

from trader.broker.base import Broker, BrokerError
from trader.models import (
    AccountSnapshot,
    AssetClass,
    Bar,
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
)

F = Instrument(symbol="F")
BTC = Instrument(symbol="BTC", asset_class=AssetClass.CRYPTO)


class StubBroker(Broker):
    """Holds 10 F with a working protective stop; can be told to reject market exits."""

    name = "stub"

    def __init__(self, *, reject_exits: bool = False, standalone: bool = True) -> None:
        self.reject_exits = reject_exits
        self.standalone = standalone
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        stop = OrderRequest(
            instrument=F,
            side=Side.SELL,
            qty=10,
            order_type=OrderType.MARKET,
            stop_loss=9.5,
            is_exit=True,
            reason="protective stop",
        )
        self.working = [
            OrderRecord(request=stop, status=OrderStatus.SUBMITTED, broker_order_id="S1")
        ]

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    @property
    def connected(self) -> bool:
        return True

    @property
    def mode(self) -> Mode:
        return Mode.SIM

    async def account(self) -> AccountSnapshot:
        raise NotImplementedError

    async def positions(self) -> list[Position]:
        return [Position(instrument=F, qty=10, avg_cost=10.0)]

    async def quote(self, instrument: Instrument) -> Quote:
        raise NotImplementedError

    async def bars(
        self, instrument: Instrument, bar_size: str = "5 mins", lookback: str = "2 D"
    ) -> list[Bar]:
        return []

    async def place_order(self, req: OrderRequest) -> OrderRecord:
        self.placed.append(req)
        if self.reject_exits and req.stop_loss is None:
            return OrderRecord(request=req, status=OrderStatus.REJECTED, message="no liquidity")
        return OrderRecord(request=req, status=OrderStatus.SUBMITTED, broker_order_id="N1")

    async def order_status(self, broker_order_id: str) -> OrderRecord | None:
        return None

    async def open_orders(self) -> list[OrderRecord]:
        return [o for o in self.working if o.broker_order_id not in self.cancelled]

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)

    async def cancel_all(self) -> None:
        self.cancelled += [o.broker_order_id or "" for o in self.working]

    async def fills_since(self, since: datetime) -> list[Fill]:
        return []

    def supports_standalone_stops(self, instrument: Instrument) -> bool:
        return self.standalone


async def test_close_position_cancels_protection_then_sells_everything() -> None:
    broker = StubBroker()
    rec = await broker.close_position(F, "time stop")
    assert broker.cancelled == ["S1"]
    assert rec is not None and rec.status is OrderStatus.SUBMITTED
    [sell] = broker.placed
    assert (sell.side, sell.qty, sell.order_type, sell.is_exit) == (
        Side.SELL,
        10,
        OrderType.MARKET,
        True,
    )
    assert sell.stop_loss is None and sell.reason == "time stop"


async def test_rejected_exit_re_places_the_cancelled_stop() -> None:
    broker = StubBroker(reject_exits=True)
    rec = await broker.close_position(F, "time stop")
    assert rec is not None and rec.status is OrderStatus.REJECTED
    assert "protective stop re-placed at 9.5" in rec.message
    _, restored = broker.placed
    assert restored.stop_loss == 9.5 and restored.qty == 10 and restored.side is Side.SELL


async def test_rejected_exit_without_standalone_stops_leaves_it_to_the_engine() -> None:
    broker = StubBroker(reject_exits=True, standalone=False)
    rec = await broker.close_position(F)
    assert rec is not None and rec.status is OrderStatus.REJECTED
    assert len(broker.placed) == 1 and "re-placed" not in rec.message


async def test_flatten_all_cancels_everything_first() -> None:
    broker = StubBroker()
    records = await broker.flatten_all("kill switch")
    assert broker.cancelled[0] == "S1" and len(records) == 1
    assert records[0].request.reason == "kill switch"


async def test_flatten_all_restores_the_stop_when_an_exit_is_refused() -> None:
    broker = StubBroker(reject_exits=True)
    [rec] = await broker.flatten_all("kill switch")
    assert rec.status is OrderStatus.REJECTED and "re-placed at 9.5" in rec.message
    assert broker.placed[-1].stop_loss == 9.5


async def test_flatten_all_fails_loudly_when_orders_cannot_be_listed() -> None:
    broker = StubBroker()

    async def broken() -> list[OrderRecord]:
        raise BrokerError("open orders timed out")

    broker.open_orders = broken  # type: ignore[method-assign]
    with pytest.raises(BrokerError):  # selling blind could double-sell with a live stop
        await broker.flatten_all("kill switch")
    assert broker.placed == []


@pytest.mark.parametrize(
    ("instrument", "qty", "fractional", "expected"),
    [(F, 3.999, False, 3.0), (F, 3.999, True, 3.999), (BTC, 0.0012349, False, 0.001234)],
)
def test_round_qty_never_rounds_up(
    instrument: Instrument, qty: float, fractional: bool, expected: float
) -> None:
    assert StubBroker().round_qty(instrument, qty, fractional) == pytest.approx(expected)


def test_default_capabilities_are_conservative() -> None:
    broker = StubBroker()
    assert broker.supports_native_stops(F) is False
    assert Broker.supports_standalone_stops(broker, F) is False
    assert broker.estimate_commission(F, 10, 10.0, Side.SELL) == 0.0
