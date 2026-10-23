"""End to end: the real SimBroker, rule-based decision maker, Engine and web app together.

Everything runs offline on a fake clock that starts on Wednesday 2026-10-21 at 10:00 New
York. The owner's credentials are created through the security module, and every owner
action goes through the HTTP API the way the browser sends it (session cookie, CSRF token
and Origin). The engine's own loops are started by POST /api/bot/start but sleep after
their first pass, so the test moves time and runs monitor ticks itself, inside the app's
event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from trader.broker.sim import SimBroker
from trader.config import AppConfig
from trader.db import Database
from trader.engine import Engine
from trader.models import BotSettings, ExitRules, Instrument, Mode
from trader.notify import Notifier
from trader.security import cf_access, crypto
from trader.security.auth import Authenticator, new_totp_secret
from trader.strategy.decide import RuleBasedDecisionMaker
from trader.web.app import CONTENT_SECURITY_POLICY, create_app

START = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # Wednesday 10:00 New York
LAST_MANAGED_MINUTE = datetime(2026, 10, 21, 19, 45, tzinfo=UTC)  # 15:45, before the flatten
AFTER_CLOSE = datetime(2026, 10, 21, 20, 10, tzinfo=UTC)  # 16:10: stock signals can be scored
PASSWORD = "correct horse battery staple"
ORIGIN = "http://testserver"
STARTING_CASH = 600.0
MAX_CYCLES = 12


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class LiveSimBroker(SimBroker):
    """The simulator posing as a real-money account: the engine must run in shadow mode."""

    @property
    def mode(self) -> Mode:
        return Mode.LIVE


@dataclass
class Stack:
    client: TestClient
    engine: Engine
    broker: SimBroker
    db: Database
    clock: FakeClock
    totp: pyotp.TOTP
    csrf: str = ""

    def call(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run `func` in the app's event loop (where the engine and its locks live)."""
        assert self.client.portal is not None
        return self.client.portal.call(func, *args)

    def get(self, path: str) -> Any:
        response = self.client.get(path)
        assert response.status_code == 200, (path, response.text)
        return response.json()

    def send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"X-CSRF-Token": self.csrf, "Origin": ORIGIN}
        return self.client.request(method, path, json=body, headers=headers)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self.send("POST", path, body)
        assert response.status_code == 200, (path, response.text)
        return response.json()

    def login(self) -> None:
        response = self.client.post(
            "/api/auth/login", json={"password": PASSWORD, "totp": self.totp.now()}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        self.csrf = body["csrf"]

    def next_code(self) -> str:
        """A code from the next 30 s step: login used the current one (replay-protected)."""
        return self.totp.at(datetime.now(UTC) + timedelta(seconds=30))

    def cycle_until(self, done: Callable[[], Any]) -> Any:
        """Run decision cycles 5 minutes apart until `done()` returns something truthy."""
        for _ in range(MAX_CYCLES):
            assert self.post("/api/bot/cycle") == {"ok": True}
            result = done()
            if result:
                return result
            self.clock.advance(minutes=5)
        pytest.fail(f"nothing happened in {MAX_CYCLES} decision cycles")

    def entry(self) -> dict[str, Any] | None:
        """The first BUY order the broker accepted, from the journal."""
        for order in reversed(self.get("/api/orders?limit=50")):
            if order["request"]["side"] == "BUY" and order["status"] in (
                "submitted",
                "partially_filled",
                "filled",
            ):
                return order
        return None


@contextmanager
def open_stack(
    tmp_path: Path,
    *,
    broker_cls: type[SimBroker] = SimBroker,
    settings: BotSettings | None = None,
    **config_overrides: Any,
) -> Iterator[Stack]:
    config = AppConfig(
        _env_file=None,
        data_dir=tmp_path,
        broker="sim",
        secret_key=SecretStr(crypto.generate_secret_key()),
        cookie_secure=False,
        allowed_hosts="testserver",
        allow_live=False,
        anthropic_api_key=None,
        ntfy_url=None,
        sim_starting_cash=STARTING_CASH,
        **config_overrides,
    )
    db = Database(config.db_path)
    # Stocks only, and no time stop or breakeven: the trade runs to its stop or target.
    settings = settings or BotSettings(
        trade_crypto=False,
        autopilot=True,  # the product default is off; this stack sends orders on its own
        exits=ExitRules(time_stop_minutes=0, breakeven_at_r=0),
    )
    db.save_settings(settings)
    secret = new_totp_secret()
    Authenticator(config, db).set_owner(PASSWORD, secret)

    clock = FakeClock(START)
    broker = broker_cls(seed=config.sim_seed, starting_cash=STARTING_CASH, clock=clock)
    engine = Engine(
        config,
        db,
        broker,
        RuleBasedDecisionMaker(settings),
        notifier=Notifier(config),
        clock=clock,
    )

    async def idle(_delay: float) -> None:
        await asyncio.Event().wait()  # the loops run one pass, then the test drives time

    engine._sleep = idle
    app = create_app(config, db, engine, start_engine=False)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            stack = Stack(client, engine, broker, db, clock, pyotp.TOTP(secret))
            try:
                yield stack
            finally:
                stack.call(engine.shutdown)
    finally:
        db.close()


def test_trading_day_end_to_end(tmp_path: Path) -> None:
    with open_stack(tmp_path) as st:
        # Signed out: the page redirects to the login and the API refuses.
        page = st.client.get("/", follow_redirects=False)
        assert page.status_code == 302 and page.headers["location"] == "/login"
        assert st.client.get("/api/status").status_code == 401
        assert st.get("/api/session") == {"authenticated": False}

        st.login()
        assert st.get("/api/session") == {"authenticated": True, "csrf": st.csrf}
        page = st.client.get("/")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert page.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert st.client.post("/api/bot/start").status_code == 403  # no CSRF token
        foreign = {"X-CSRF-Token": st.csrf, "Origin": "https://evil.example"}
        assert st.client.post("/api/bot/start", headers=foreign).status_code == 403
        assert not st.db.get_flag("engine_running")

        status = st.post("/api/bot/start")
        assert status["running"] and status["connected"] and status["mode"] == "sim"
        assert status["day"] == "2026-10-21" and not status["shadow"]
        assert st.db.get_flag("engine_running")

        # Entry: the rule-based maker buys the best candidate with a broker-side bracket.
        entry = st.cycle_until(st.entry)
        req = entry["request"]
        symbol = req["instrument"]["symbol"]
        inst = Instrument(symbol=symbol)
        assert req["stop_loss"] < req["limit_price"] < req["take_profit"]
        assert entry["status"] == "filled"

        st.call(st.engine.monitor_tick)
        data = st.get("/api/status")
        [pos] = data["positions"]
        quote = st.call(st.broker.quote, inst)
        assert pos["instrument"]["symbol"] == symbol and pos["qty"] == req["qty"]
        assert pos["market_price"] == quote.bid  # valued at the bid
        assert (pos["stop_loss"], pos["take_profit"]) == (req["stop_loss"], req["take_profit"])
        working = {(o["request"]["side"], o["request"]["stop_loss"]) for o in data["open_orders"]}
        assert ("SELL", req["stop_loss"]) in working  # the protective stop lives at the broker
        assert data["status"]["trades_today"] == 1 and data["today"]["trades"] == 1
        assert data["status"]["loss_limit_usd"] == pytest.approx(STARTING_CASH * 0.06)
        assert data["account"]["equity"] < STARTING_CASH  # spread and commission paid

        [batch, *_] = st.get("/api/decisions?limit=20")
        assert batch["maker"] == "rules" and batch["market_view"].startswith("[manual]")
        assert any(d["action"] == "BUY" and d["symbol"] == symbol for d in batch["decisions"])
        assert req["decision_id"] == batch["id"]

        # Let the market run, minute by minute, until the stop or the target is hit.
        while not st.db.list_trades() and st.clock() < LAST_MANAGED_MINUTE:
            st.clock.advance(minutes=1)
            st.call(st.engine.monitor_tick)
        if not st.db.list_trades():  # the path never reached a level: gap through the stop
            st.call(st.broker.set_price, inst, req["stop_loss"] * 0.99)
            st.clock.advance(minutes=1)
            st.call(st.engine.monitor_tick)

        [trade] = st.get("/api/trades?limit=50")
        assert trade["instrument"]["symbol"] == symbol and trade["qty"] == req["qty"]
        assert trade["exit_reason"] in ("stop-loss", "take-profit")
        assert trade["setup"] == req["setup"] and trade["decision_id"] == batch["id"]
        assert trade["commission"] > 0
        gross = (trade["exit_price"] - trade["entry_price"]) * trade["qty"]
        assert trade["pnl"] == pytest.approx(gross - trade["commission"], abs=0.01)
        assert trade["r_multiple"] is not None and (trade["r_multiple"] > 0) == (trade["pnl"] > 0)
        if trade["exit_reason"] == "stop-loss":
            assert trade["exit_price"] <= req["stop_loss"]
        else:
            assert trade["exit_price"] >= req["take_profit"]

        st.clock.advance(minutes=1)
        st.call(st.engine.monitor_tick)
        data = st.get("/api/status")
        assert data["positions"] == [] and data["open_orders"] == []  # the OCO sibling is gone
        assert st.db.all_position_meta() == {}

        [today, *_] = st.get("/api/days?limit=30")
        assert today["day"] == "2026-10-21" and today["start_equity"] == STARTING_CASH
        assert today["trades"] == 1 and today["wins"] + today["losses"] == 1
        assert today["realized_pnl"] == pytest.approx(trade["pnl"], abs=1e-6)
        assert today["fees"] == pytest.approx(trade["commission"], abs=1e-6)
        assert today["loss_streak"] == (1 if trade["pnl"] < 0 else 0)
        assert today["end_equity"] == pytest.approx(STARTING_CASH + trade["pnl"], abs=0.01)
        assert len(st.get("/api/equity?days=3650")) >= 2

        # After the close every ledger signal is scored, taken or not.
        st.clock.now = AFTER_CLOSE
        st.call(st.engine.monitor_tick)
        signals = st.get("/api/signals?limit=500")
        assert signals and all(s["evaluated"] for s in signals)
        assert all(s["first_hit"] in ("target", "stop", "neither") for s in signals)
        [taken] = [s for s in signals if s["taken"]]
        assert taken["order_id"] == entry["id"] and taken["instrument"]["symbol"] == symbol
        assert all(s["skip_reason"] for s in signals if not s["taken"])
        only = st.get(f"/api/signals?limit=500&setup={taken['setup']}")
        assert only and {s["setup"] for s in only} == {taken["setup"]}

        stats = st.get("/api/signals/stats?days=3650")
        assert sum(s["signals"] for s in stats) == len(signals)
        assert sum(s["evaluated"] for s in stats) == len(signals)
        assert sum(s["taken"] for s in stats) == 1
        assert not any(s["enough_data"] for s in stats)  # N < 30: too few to judge

        status = st.post("/api/bot/stop")
        assert status["running"] is False and not st.db.get_flag("engine_running")
        actions = [a["action"] for a in st.get("/api/audit?limit=50")]
        assert {"login", "bot_start", "bot_cycle", "bot_stop"} <= set(actions)


def test_kill_switch_flattens_and_halts(tmp_path: Path) -> None:
    with open_stack(tmp_path) as st:
        st.login()
        st.post("/api/bot/start")
        entry = st.cycle_until(st.entry)
        symbol = entry["request"]["instrument"]["symbol"]
        st.call(st.engine.monitor_tick)
        assert st.get("/api/status")["positions"]

        status = st.post("/api/bot/kill")
        assert status["halted"] and status["halt_reason"] == "kill switch"
        assert st.call(st.broker.positions) == []
        assert st.call(st.broker.open_orders) == []

        st.clock.advance(minutes=1)
        st.call(st.engine.monitor_tick)
        [trade] = st.get("/api/trades")
        assert trade["instrument"]["symbol"] == symbol and trade["exit_reason"] == "kill switch"
        data = st.get("/api/status")
        assert data["positions"] == [] and data["today"]["halted"]

        # Halted: a cycle makes no decision and sends nothing.
        decisions, orders = len(st.get("/api/decisions")), len(st.get("/api/orders"))
        st.clock.advance(minutes=5)
        assert st.post("/api/bot/cycle") == {"ok": True}
        assert len(st.get("/api/decisions")) == decisions
        assert len(st.get("/api/orders")) == orders

        # Resuming needs a fresh 2FA code.
        refused = st.send("POST", "/api/bot/resume", {})
        assert refused.status_code == 403 and refused.json() == {"detail": "step_up_required"}
        status = st.post("/api/bot/resume", {"totp": st.next_code()})
        assert status["halted"] is False
        actions = {a["action"] for a in st.get("/api/audit")}
        assert {"bot_kill", "bot_resume"} <= actions


def test_approval_flow_and_settings_step_up(tmp_path: Path) -> None:
    with open_stack(tmp_path) as st:
        st.login()
        payload = st.get("/api/settings")
        settings = payload["settings"]
        assert payload["hard_caps"]["max_risk_per_trade_pct"] == 10.0

        # Loosening a risk limit needs a fresh 2FA code; tightening does not.
        looser = {**settings, "risk": {**settings["risk"], "max_risk_per_trade_pct": 3.0}}
        refused = st.send("PUT", "/api/settings", {"settings": looser})
        assert refused.status_code == 403 and refused.json() == {"detail": "step_up_required"}
        saved = st.send("PUT", "/api/settings", {"settings": looser, "totp": st.next_code()})
        assert saved.status_code == 200
        assert saved.json()["settings"]["risk"]["max_risk_per_trade_pct"] == 3.0
        manual = {**saved.json()["settings"], "autopilot": False}
        assert st.send("PUT", "/api/settings", {"settings": manual}).status_code == 200
        assert st.engine.settings.autopilot is False  # applied to the running engine

        st.post("/api/bot/start")
        pending = st.cycle_until(lambda: st.get("/api/status")["pending"])
        [order] = pending
        assert order["status"] == "pending_approval"
        assert st.call(st.broker.positions) == []  # nothing is sent before approval

        approved = st.post(f"/api/orders/{order['id']}/approve")
        assert approved["status"] in ("submitted", "partially_filled", "filled")
        assert "approved by owner" in approved["message"]
        [pos] = st.call(st.broker.positions)
        assert pos.instrument.symbol == order["request"]["instrument"]["symbol"]
        again = st.send("POST", f"/api/orders/{order['id']}/approve")
        assert again.status_code == 409


def test_shadow_mode_on_a_live_account_blocks_every_order(tmp_path: Path) -> None:
    with open_stack(tmp_path, broker_cls=LiveSimBroker) as st:
        st.login()
        status = st.post("/api/bot/start")
        assert status["mode"] == "live" and status["shadow"]
        assert not status["live_armed"] and not status["live_allowed"]

        orders = st.cycle_until(lambda: st.get("/api/orders?limit=50"))
        assert all(o["status"] == "blocked" for o in orders)
        assert any(o["message"].startswith("shadow mode") for o in orders)
        assert st.call(st.broker.positions) == []
        assert st.call(st.broker.open_orders) == []
        signals = st.get("/api/signals")
        assert any(s["skip_reason"] == "shadow mode" for s in signals)
        assert not any(s["taken"] for s in signals)

        # Arming is refused while TRADER_ALLOW_LIVE is false (before the code is used).
        refused = st.send("POST", "/api/live/arm", {"totp": st.next_code()})
        assert refused.status_code == 403
        assert st.get("/api/status")["status"]["shadow"] is True


def test_cloudflare_access_rejects_requests_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_network(url: str) -> dict[str, Any]:
        raise AssertionError(f"unexpected JWKS fetch: {url}")

    monkeypatch.setattr(cf_access, "fetch_jwks", no_network)
    with open_stack(
        tmp_path, cf_access_team_domain="team.cloudflareaccess.com", cf_access_aud="aud-tag"
    ) as st:
        assert st.get("/healthz") == {"ok": True}  # the container health check stays open
        for path in ("/", "/login", "/api/session", "/api/status", "/static/app.js"):
            response = st.client.get(path, follow_redirects=False)
            assert response.status_code == 403, path
            assert response.json() == {"detail": "forbidden"}
            assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        login = st.client.post("/api/auth/login", json={"password": PASSWORD, "totp": "000000"})
        assert login.status_code == 403
        assert st.db.count_failed_logins(datetime.now(UTC) - timedelta(hours=1)) == 0
        assert st.db.get_flag("engine_running") is False
