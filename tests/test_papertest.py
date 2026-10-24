"""`python -m trader paper-test`: a bracket that cannot fill, checked and cancelled."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_ibkr import DONE, FakeIB, log_error, make_broker
from trader import cli
from trader.broker.base import BrokerError
from trader.config import AppConfig
from trader.models import Mode
from trader.papertest import (
    TAG_PREFIX,
    bracket_levels,
    check_bracket,
    paper_test_client_id,
    run_paper_test,
)


async def test_paper_test_passes_against_the_ibkr_adapter() -> None:
    broker, fake = make_broker()
    checks = await run_paper_test(broker, symbol="AAPL", ref_price=100.0, settle_seconds=0.5)
    assert [c.ok for c in checks] == [True] * len(checks), checks
    assert [c.name for c in checks] == [
        "paper account",
        "price for AAPL",
        "entry accepted",
        "stop-loss and take-profit attached at IBKR",
        "bot sees the order",
        "test orders cancelled",
    ]
    parent, stop, target = (order for _, order in fake.placed)
    assert len({o.orderRef for o in (parent, stop, target)}) == 1  # one tag per bracket
    assert parent.orderRef.startswith(TAG_PREFIX)  # never adopted by the engine
    assert (parent.lmtPrice, stop.auxPrice, target.lmtPrice) == (80.0, 75.0, 85.0)
    assert all(t.orderStatus.status in DONE for t in fake.trade_list)  # nothing left
    assert not fake.connected


async def test_paper_test_refuses_a_live_account() -> None:
    broker, fake = make_broker(FakeIB(accounts=("U7654321",)))
    checks = await run_paper_test(broker, symbol="AAPL", ref_price=100.0, settle_seconds=0.2)
    assert [(c.name, c.ok) for c in checks] == [("paper account", False)]
    assert "only runs on paper" in checks[0].detail
    assert fake.placed == []


async def test_a_rejected_stop_loss_fails_the_test_and_nothing_is_left() -> None:
    broker, fake = make_broker()

    def reject_stop(trade) -> None:
        if trade.order.transmit:  # the whole bracket was just acknowledged
            for t in fake.trade_list:
                if t.order.orderType == "STP":
                    t.orderStatus.status = "Inactive"
                    log_error(t, 201, "Order rejected - reason: test")

    fake.on_place = reject_stop
    checks = await run_paper_test(broker, symbol="AAPL", ref_price=100.0, settle_seconds=0.3)
    entry = next(c for c in checks if c.name == "entry accepted")
    assert not entry.ok and "protective order rejected" in entry.detail
    cleanup = checks[-1]
    assert cleanup.name == "test orders cancelled" and cleanup.ok
    assert all(t.orderStatus.status in DONE for t in fake.trade_list)


class _NoQuoteBroker:
    mode = Mode.PAPER
    placed = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def quote(self, instrument):
        raise BrokerError("no market data")

    async def place_order(self, req):
        self.placed += 1
        raise AssertionError("must not place an order without a price")

    async def open_orders(self):
        return []

    async def cancel_order(self, broker_order_id: str) -> None:
        return None

    def orders_tagged(self, trace_id: str):
        return []


async def test_no_quote_asks_for_a_price_and_places_nothing() -> None:
    broker = _NoQuoteBroker()
    checks = await run_paper_test(broker, symbol="f", settle_seconds=0.1)
    assert checks[-1].name == "price for F" and not checks[-1].ok
    assert "--price" in checks[-1].detail
    assert broker.placed == 0


def test_check_bracket_names_what_is_wrong() -> None:
    entry, stop, target = bracket_levels(12.0)
    assert (entry, stop, target) == (9.6, 9.0, 10.2)

    def order(oid, parent, action, kind, tif, *, limit=None, aux=None, oca="g", status="Submitted"):
        return {
            "broker_id": str(oid), "order_id": oid, "parent_id": parent, "action": action,
            "type": kind, "tif": tif, "qty": 1.0, "limit": limit, "stop": aux, "oca": oca,
            "status": status, "done": status in DONE,
        }  # fmt: skip

    good = [
        order(1, 0, "BUY", "LMT", "DAY", limit=entry, oca=""),
        order(2, 1, "SELL", "STP", "GTC", aux=stop, status="PreSubmitted"),
        order(3, 1, "SELL", "LMT", "GTC", limit=target, status="PreSubmitted"),
    ]
    assert check_bracket(good, entry, stop, target) == ""
    day_stop = [good[0], {**good[1], "tif": "DAY"}, good[2]]
    assert "stop-loss is SELL STP DAY" in check_bracket(day_stop, entry, stop, target)
    split = [good[0], good[1], {**good[2], "oca": "other"}]
    assert "one OCA group" in check_bracket(split, entry, stop, target)
    gone = [good[0], {**good[1], "status": "Cancelled", "done": True}, good[2]]
    assert "not working at IBKR: STP Cancelled" in check_bracket(gone, entry, stop, target)
    assert "found 0" in check_bracket([good[0], good[2]], entry, stop, target)


def test_paper_test_client_id_is_free() -> None:
    config = AppConfig(_env_file=None, ib_client_id=17, watchdog_client_id=18)
    assert paper_test_client_id(config) == 19
    config = AppConfig(_env_file=None, ib_client_id=20, watchdog_client_id=21)
    assert paper_test_client_id(config) == 22


def test_cli_paper_test_needs_the_ibkr_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("TRADER_BROKER=sim\n", encoding="utf-8")
    monkeypatch.delenv("TRADER_BROKER", raising=False)
    assert cli.main(["paper-test", "--env-file", str(env)]) == 1
    assert "TRADER_BROKER=ibkr" in capsys.readouterr().err


async def test_a_weekend_warning_status_still_passes_and_children_are_not_double_cancelled() -> (
    None
):
    """On a weekend IBKR accepts the entry with warning 399, which ib_async reports as
    status ValidationError (the order stays live). Cancelling the entry cancels its
    stop-loss and take-profit at IBKR, so they are not cancelled again (error 10148)."""
    broker, fake = make_broker()

    def weekend_warning(trade) -> None:
        if trade.order.transmit:
            for t in fake.trade_list:
                if not t.order.parentId:
                    t.orderStatus.status = "ValidationError"

    original_cancel = fake.cancelOrder

    def cancel_with_children(order):
        trade = original_cancel(order)
        for t in fake.trade_list:  # as IBKR does for a bracket's children
            if t.order.parentId == order.orderId and t.orderStatus.status not in DONE:
                t.orderStatus.status = "Cancelled"
        return trade

    fake.on_place = weekend_warning
    fake.cancelOrder = cancel_with_children
    checks = await run_paper_test(broker, symbol="AAPL", ref_price=100.0, settle_seconds=0.5)
    assert all(c.ok for c in checks), checks
    parent_id = fake.placed[0][1].orderId
    assert fake.cancelled == [parent_id]  # only the entry; IBKR took the children with it
    assert all(t.orderStatus.status in DONE for t in fake.trade_list)
