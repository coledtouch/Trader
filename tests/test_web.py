import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.test_cf_access import AUD, TEAM, FakeJwks, jwk, make_token, new_key
from trader.config import AppConfig
from trader.db import Database
from trader.models import (
    AccountSnapshot,
    BotSettings,
    EngineStatus,
    Instrument,
    Mode,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    Position,
    SignalRecord,
)
from trader.security import cf_access, crypto
from trader.security.auth import Authenticator, new_totp_secret
from trader.web import app as app_module
from trader.web.app import CONTENT_SECURITY_POLICY, create_app, loosened_fields

PASSWORD = "correct horse battery"
ORIGIN = "http://testserver"
FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class FakeEngine:
    """Implements the Engine surface the web layer uses (SPEC 5.7)."""

    def __init__(self, db: Database, mode: Mode = Mode.SIM):
        self.db = db
        self.mode = mode
        self.running = False
        self.halted = False
        self.armed = False
        self.calls: list[str] = []
        self.cycles: list[bool] = []
        self.reloads = 0
        self.fail: str | None = None  # name of a method that should raise

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError(f"{name} exploded")

    async def start(self) -> None:
        self._call("start")
        self.running = True

    async def start_monitor(self) -> None:
        self._call("start_monitor")

    async def stop(self) -> None:
        self._call("stop")
        self.running = False

    async def shutdown(self) -> None:
        self._call("shutdown")
        self.running = False

    async def kill(self, actor: str = "owner") -> None:
        self._call("kill")
        self.halted = True

    async def resume(self, actor: str = "owner") -> None:
        self._call("resume")
        self.halted = False

    async def run_cycle(self, manual: bool = False):
        self.cycles.append(manual)
        return None

    async def monitor_tick(self) -> None:
        return None

    async def approve_order(self, order_id: int, actor: str = "owner") -> OrderRecord:
        self._call("approve")
        self.db.update_order(order_id, status=OrderStatus.SUBMITTED, broker_order_id="b-1")
        return self.db.get_order(order_id)

    async def reject_order(self, order_id: int, actor: str = "owner") -> OrderRecord:
        self._call("reject")
        self.db.update_order(order_id, status=OrderStatus.CANCELLED, message="rejected by owner")
        return self.db.get_order(order_id)

    async def arm_live(self, actor: str = "owner") -> None:
        self._call("arm")
        if self.mode != Mode.LIVE:
            raise PermissionError("not a live account")
        self.armed = True

    async def disarm_live(self, actor: str = "owner") -> None:
        self._call("disarm")
        self.armed = False

    def reload_settings(self) -> None:
        self.reloads += 1

    def status(self) -> EngineStatus:
        return EngineStatus(
            running=self.running,
            mode=self.mode,
            halted=self.halted,
            live_armed=self.armed,
            day="2026-10-23",
            equity=510.0,
        )

    def snapshot(self) -> dict:
        account = AccountSnapshot(
            account_id="SIM", mode=self.mode, equity=510.0, cash=300.0, settled_cash=300.0,
            buying_power=300.0,
        )  # fmt: skip
        position = Position(instrument=Instrument(symbol="F"), qty=10, avg_cost=11.0)
        return {"account": account, "positions": [position], "open_orders": []}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def make_config(**overrides) -> AppConfig:
    values = {
        "secret_key": SecretStr(crypto.generate_secret_key()),
        "cookie_secure": False,
        "allowed_hosts": "testserver,localhost",
        "login_max_attempts_per_ip": 5,
        "login_lockout_failures": 10,
        "anthropic_api_key": None,
        "xai_api_key": None,
    }
    values.update(overrides)
    return AppConfig(_env_file=None, **values)


def build(mode: Mode = Mode.SIM, start_engine: bool = False, **config_overrides):
    config = make_config(**config_overrides)
    db = Database(":memory:")
    clock = Clock()
    secret = new_totp_secret()
    engine = FakeEngine(db, mode)
    app = create_app(config, db, engine, start_engine=start_engine)
    app.state.auth = Authenticator(config, db, hasher=FAST, clock=clock)
    app.state.auth.set_owner(PASSWORD, secret)
    totp = pyotp.TOTP(secret)

    def code(offset: int = 0) -> str:
        return totp.generate_otp(totp.timecode(clock.now) + offset)

    def tick() -> None:
        clock.now += timedelta(seconds=30)

    return SimpleNamespace(app=app, db=db, engine=engine, config=config, code=code, tick=tick)


@pytest.fixture
def env():
    ctx = build()
    with TestClient(ctx.app) as client:
        ctx.client = client
        yield ctx
    ctx.db.close()


def login(ctx) -> dict[str, str]:
    """Log in and return headers for mutating requests."""
    response = ctx.client.post("/api/auth/login", json={"password": PASSWORD, "totp": ctx.code()})
    assert response.status_code == 200, response.text
    ctx.tick()  # the next 2FA code must come from a later step
    return {"X-CSRF-Token": response.json()["csrf"], "Origin": ORIGIN}


def pending_order(db: Database) -> int:
    request = OrderRequest(
        instrument=Instrument(symbol="F"), side="BUY", qty=5, limit_price=11.0, stop_loss=10.5,
        take_profit=12.0,
    )  # fmt: skip
    return db.insert_order(OrderRecord(request=request, status=OrderStatus.PENDING_APPROVAL))


# ---------------------------------------------------------------------- basics
def test_healthz_and_no_docs(env):
    assert env.client.get("/healthz").json() == {"ok": True}
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert env.client.get(path).status_code == 404


@pytest.mark.parametrize("asset", ["app.js?v=13", "login.js?v=13", "styles.css?v=13", "sw.js"])
def test_executable_shell_assets_revalidate_on_deploy(env, asset):
    response = env.client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache"


def test_security_headers(env):
    for path in ("/healthz", "/login", "/api/session", "/api/status"):
        response = env.client.get(path)
        headers = response.headers
        assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert "strict-transport-security" not in headers  # cookie_secure=False
    assert env.client.get("/api/session").headers["cache-control"] == "no-store"
    assert env.client.get("/login").headers["cache-control"] == "no-store"
    assert "cache-control" not in env.client.get("/healthz").headers


def test_hsts_and_secure_cookie_when_cookie_secure():
    ctx = build(cookie_secure=True)
    with TestClient(ctx.app, base_url="https://testserver") as client:
        assert "max-age=" in client.get("/healthz").headers["strict-transport-security"]
        response = client.post("/api/auth/login", json={"password": PASSWORD, "totp": ctx.code()})
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-trader=")
        for attribute in ("HttpOnly", "Secure", "Path=/", "SameSite=strict"):
            assert attribute.lower() in cookie.lower()
        assert "domain" not in cookie.lower()


def test_untrusted_host_is_rejected(env):
    assert env.client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400


def test_body_size_limit(env):
    response = env.client.post(
        "/api/auth/login",
        content=b'{"password": "' + b"x" * (70 * 1024) + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}

    def chunks():  # no Content-Length: the limit is enforced while reading
        for _ in range(70):
            yield b"x" * 1024

    response = env.client.post(
        "/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_unhandled_errors_are_json_with_headers():
    ctx = build()
    ctx.engine.snapshot = lambda: 1 / 0
    with TestClient(ctx.app, raise_server_exceptions=False) as client:
        ctx.client = client
        login(ctx)
        response = client.get("/api/status")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------- pages
def test_index_requires_login(env):
    response = env.client.get("/", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/login"
    assert env.client.get("/login").headers["content-type"].startswith("text/html")
    login(env)
    response = env.client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


def test_static_files_and_service_worker_scope(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>", encoding="utf-8")
    (tmp_path / "sw.js").write_text("self.addEventListener('fetch', () => {});", encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path)
    ctx = build()
    with TestClient(ctx.app) as client:
        ctx.client = client
        worker = client.get("/static/sw.js")
        assert worker.status_code == 200
        assert worker.headers["service-worker-allowed"] == "/"
        assert worker.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert client.get("/static/missing.js").status_code == 404
        login(ctx)
        assert "<title>shell</title>" in client.get("/").text


def test_missing_static_files_get_a_placeholder(env, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path)
    response = env.client.get("/login")
    assert response.status_code == 200 and "login.html" in response.text
    assert "<script" not in response.text and "style=" not in response.text


# ---------------------------------------------------------------------- auth flow
def test_background_polling_does_not_keep_an_idle_session_alive(monkeypatch):
    """SEC-5: the dashboard polls /api/status every few seconds while visible. Those polls
    (marked X-Background) must not reset the idle timer: an unattended tab signs out after
    session_idle_minutes. Real requests (no header) still count as activity."""
    ctx = build()
    clock = ctx.app.state.auth._clock
    monkeypatch.setattr("trader.db.utcnow", lambda: clock.now)  # last_seen on the fake clock
    background = {"X-Background": "1"}
    with TestClient(ctx.app) as client:
        ctx.client = client
        login(ctx)
        statuses = []
        for _ in range(40):  # one poll a minute, nobody touching the app
            clock.now += timedelta(minutes=1)
            statuses.append(client.get("/api/status", headers=background).status_code)
        assert statuses[:28] == [200] * 28 and statuses[-1] == 401
        login(ctx)
        for _ in range(40):  # the owner keeps using the app: the session stays
            clock.now += timedelta(minutes=1)
            assert client.get("/api/status").status_code == 200
    ctx.db.close()


def test_dashboard_marks_its_status_poll_as_background():
    """SEC-5 (client side): automatic dashboard/replay polls do not extend idle sessions."""
    from trader.security.auth import BACKGROUND_HEADER

    app_js = (Path(__file__).parent.parent / "trader/web/static/app.js").read_text("utf-8")
    assert f"headers['{BACKGROUND_HEADER}'] = '1'" in app_js
    assert "api('GET', '/api/status', undefined, { background: true })" in app_js
    assert "loadPerformance({ background: true })" in app_js
    assert (
        "`/api/performance?days=${encodeURIComponent(days)}`, undefined, { background }" in app_js
    )
    assert (
        "`/api/research?days=${encodeURIComponent(days)}`, undefined, "
        "{ background: state.researchRunning }" in app_js
    )


def test_login_flow(env):
    assert env.client.get("/api/session").json() == {"authenticated": False}
    bad = env.client.post("/api/auth/login", json={"password": PASSWORD, "totp": "000000"})
    wrong = env.client.post("/api/auth/login", json={"password": "nope", "totp": env.code()})
    empty = env.client.post("/api/auth/login", json={})
    for response in (bad, wrong, empty):
        assert response.status_code == 401
        assert response.json() == {"detail": "invalid credentials"}

    response = env.client.post("/api/auth/login", json={"password": PASSWORD, "totp": env.code()})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["csrf"]
    assert "trader_session" in response.cookies
    session = env.client.get("/api/session").json()
    assert session == {"authenticated": True, "csrf": body["csrf"]}

    headers = {"X-CSRF-Token": body["csrf"], "Origin": ORIGIN}
    assert env.client.post("/api/auth/logout", headers=headers).json() == {"ok": True}
    assert env.client.get("/api/session").json() == {"authenticated": False}
    assert env.client.get("/api/status").status_code == 401

    actions = [row["action"] for row in env.db.list_audit(20)]
    assert actions.count("login_failed") == 3
    assert "login" in actions and "logout" in actions


def test_login_rotates_the_session(env):
    login(env)
    old_cookie = env.client.cookies.get("trader_session")
    login(env)
    new_cookie = env.client.cookies.get("trader_session")
    assert new_cookie != old_cookie
    assert env.app.state.auth.get_session(old_cookie) is None


def test_login_rate_limit_returns_429(env):
    for _ in range(5):
        env.client.post("/api/auth/login", json={"password": "wrong password", "totp": "1"})
    response = env.client.post("/api/auth/login", json={"password": PASSWORD, "totp": env.code()})
    assert response.status_code == 429
    assert env.db.list_audit(1)[0]["action"] == "login_rate_limited"


def test_trusted_proxy_ip_is_audited():
    ctx = build(trust_proxy_headers=True)
    with TestClient(ctx.app) as client:
        client.post(
            "/api/auth/login",
            json={"password": "nope", "totp": "1"},
            headers={"CF-Connecting-IP": "203.0.113.9"},
        )
    assert ctx.db.list_audit(1)[0]["ip"] == "203.0.113.9"


GET_ROUTES = [
    "/api/status",
    "/api/decisions",
    "/api/orders",
    "/api/trades",
    "/api/equity",
    "/api/days",
    "/api/audit",
    "/api/settings",
    "/api/signals",
    "/api/signals/stats",
]
POST_ROUTES = [
    "/api/auth/logout",
    "/api/bot/start",
    "/api/bot/stop",
    "/api/bot/kill",
    "/api/bot/resume",
    "/api/bot/cycle",
    "/api/live/arm",
    "/api/live/disarm",
    "/api/orders/1/approve",
    "/api/orders/1/reject",
]


def test_api_requires_a_session(env):
    for path in GET_ROUTES:
        response = env.client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"detail": "not authenticated"}
    for path in POST_ROUTES:
        assert env.client.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 401, path
    put = env.client.put("/api/settings", json={"settings": {}}, headers={"Origin": ORIGIN})
    assert put.status_code == 401
    assert env.engine.calls == []


def test_csrf_and_origin_checks(env):
    headers = login(env)
    no_token = env.client.post("/api/bot/kill", headers={"Origin": ORIGIN})
    wrong_token = env.client.post(
        "/api/bot/kill", headers={"X-CSRF-Token": "wrong", "Origin": ORIGIN}
    )
    bad_origin = env.client.post(
        "/api/bot/kill", headers={**headers, "Origin": "https://evil.example"}
    )
    no_origin = env.client.post("/api/bot/kill", headers={"X-CSRF-Token": headers["X-CSRF-Token"]})
    for response in (no_token, wrong_token, bad_origin, no_origin):
        assert response.status_code == 403
    assert env.engine.calls == []
    referer = {"X-CSRF-Token": headers["X-CSRF-Token"], "Referer": f"{ORIGIN}/"}
    assert env.client.post("/api/bot/kill", headers=referer).status_code == 200
    assert env.engine.calls == ["kill"]


def test_public_origin_is_accepted():
    ctx = build(public_origins="https://desk.example.dev")
    with TestClient(ctx.app) as client:
        ctx.client = client
        headers = login(ctx)
        headers["Origin"] = "https://desk.example.dev"
        assert client.post("/api/bot/stop", headers=headers).status_code == 200


# ---------------------------------------------------------------------- data endpoints
def test_status_payload(env):
    login(env)
    order_id = pending_order(env.db)
    body = env.client.get("/api/status").json()
    assert set(body) == {"status", "account", "positions", "open_orders", "pending", "today"}
    assert body["status"]["mode"] == "sim" and body["status"]["day"] == "2026-10-23"
    assert body["account"]["equity"] == 510.0
    assert body["positions"][0]["instrument"]["symbol"] == "F"
    assert [o["id"] for o in body["pending"]] == [order_id]
    assert body["today"] is None
    assert isinstance(body["pending"][0]["created_at"], str)


def test_list_endpoints(env):
    login(env)
    env.db.audit("something", {"x": 1}, ip="1.2.3.4")
    env.db.add_equity_point(500.0)
    for path in GET_ROUTES[1:]:
        assert env.client.get(path).status_code == 200, path
    assert env.client.get("/api/equity?days=7").json()[0]["equity"] == 500.0
    audit = env.client.get("/api/audit?limit=5").json()
    assert set(audit[0]) == {"id", "ts", "actor", "action", "detail", "ip"}
    assert env.client.get("/api/decisions?limit=0").status_code == 422
    assert env.client.get("/api/decisions?limit=100000").status_code == 200  # clamped


def test_signals_endpoints(env):
    login(env)
    now = datetime.now(UTC)
    for i, setup in enumerate(["vwap_reclaim", "vwap_reclaim", "orb_breakout"]):
        env.db.insert_signal(
            SignalRecord(
                ts=now - timedelta(minutes=i), instrument=Instrument(symbol="F"), setup=setup,
                score=0.6, price=11.0, stop=10.5, target=12.0, evaluated=True, r_close=0.5,
                first_hit="target",
            )
        )  # fmt: skip
    env.db.insert_signal(
        SignalRecord(
            ts=now - timedelta(days=60), instrument=Instrument(symbol="F"), setup="old_setup",
            score=0.5, price=11.0, stop=10.5, target=12.0,
        )
    )  # fmt: skip
    signals = env.client.get("/api/signals").json()
    assert len(signals) == 4 and signals[0]["setup"] == "vwap_reclaim"
    assert {s["setup"] for s in env.client.get("/api/signals?setup=orb_breakout").json()} == {
        "orb_breakout"
    }
    assert len(env.client.get("/api/signals?limit=2&setup=").json()) == 2
    assert env.client.get("/api/signals?limit=99999").status_code == 200  # clamped to 500
    stats = {s["setup"]: s for s in env.client.get("/api/signals/stats?days=30").json()}
    assert set(stats) == {"vwap_reclaim", "orb_breakout"}  # the 60-day-old one is excluded
    assert stats["vwap_reclaim"]["signals"] == 2
    assert stats["vwap_reclaim"]["target_first_pct"] == 100.0
    assert stats["vwap_reclaim"]["enough_data"] is False


# ---------------------------------------------------------------------- settings
def put_settings(ctx, headers, settings: BotSettings, totp: str | None = None):
    body: dict = {"settings": settings.model_dump(mode="json")}
    if totp is not None:
        body["totp"] = totp
    return ctx.client.put("/api/settings", json=body, headers=headers)


def test_get_settings(env):
    login(env)
    body = env.client.get("/api/settings").json()
    assert set(body) == {
        "settings",
        "hard_caps",
        "llm_available",
        "ai_keys",
        "grok_model",
        "live_allowed",
    }
    assert body["hard_caps"]["max_daily_loss_pct"] == 25.0
    assert body["llm_available"] is False and body["live_allowed"] is False
    assert body["ai_keys"] == {"claude": False, "grok": False}
    assert body["grok_model"] == "grok-4.7"
    assert body["settings"]["decision_mode"] == "consensus"


def test_tightening_saves_without_2fa(env):
    headers = login(env)
    settings = BotSettings()
    settings.risk.max_daily_loss_pct = 3.0
    settings.exits.time_stop_minutes = 120  # exit rules never need step-up
    settings.stock_universe = [*settings.stock_universe, "amd", " AMD "]  # growing is fine
    response = put_settings(env, headers, settings)
    assert response.status_code == 200, response.text
    saved = env.db.get_settings()
    assert saved.risk.max_daily_loss_pct == 3.0 and saved.exits.time_stop_minutes == 120
    assert saved.stock_universe[-1] == "AMD" and saved.stock_universe.count("AMD") == 1
    assert response.json()["settings"]["risk"]["max_daily_loss_pct"] == 3.0
    assert env.engine.reloads == 1
    entry = env.db.list_audit(1)[0]
    assert entry["action"] == "settings_update"
    assert entry["detail"]["changed"]["risk.max_daily_loss_pct"] == [6.0, 3.0]
    assert entry["detail"]["loosened"] == []


def test_loosening_requires_2fa(env):
    headers = login(env)
    settings = BotSettings()
    settings.risk.max_risk_per_trade_pct = 4.0
    missing = put_settings(env, headers, settings)
    assert missing.status_code == 403 and missing.json() == {"detail": "step_up_required"}
    wrong = put_settings(env, headers, settings, totp="000000" if env.code() != "000000" else "1")
    assert wrong.status_code == 403
    assert env.db.get_settings().risk.max_risk_per_trade_pct == 2.0
    assert env.engine.reloads == 0
    ok = put_settings(env, headers, settings, totp=env.code())
    assert ok.status_code == 200
    assert env.db.get_settings().risk.max_risk_per_trade_pct == 4.0
    assert env.db.list_audit(1)[0]["detail"]["loosened"] == ["risk.max_risk_per_trade_pct"]


def test_leaving_consensus_requires_2fa_and_returning_does_not(env):
    headers = login(env)
    single = BotSettings(decision_mode="claude")
    missing = put_settings(env, headers, single)
    assert missing.status_code == 403 and missing.json() == {"detail": "step_up_required"}
    assert env.db.get_settings().decision_mode == "consensus"
    ok = put_settings(env, headers, single, totp=env.code())
    assert ok.status_code == 200 and ok.json()["settings"]["decision_mode"] == "claude"
    assert env.db.list_audit(1)[0]["detail"]["loosened"] == ["decision_mode"]
    # Back to consensus (stricter) needs no code; the setting round-trips through the db.
    back = put_settings(env, headers, BotSettings(decision_mode="consensus"))
    assert back.status_code == 200
    assert env.db.get_settings().decision_mode == "consensus"
    assert env.engine.reloads == 2
    # Switching the AI off while in consensus is also leaving consensus.
    off = put_settings(env, headers, BotSettings(llm_enabled=False))
    assert off.status_code == 403
    # Between single-model modes nothing changes in strictness.
    env.tick()
    grok = put_settings(env, headers, BotSettings(decision_mode="grok"), totp=env.code())
    assert grok.status_code == 200
    swap = put_settings(env, headers, BotSettings(decision_mode="claude"))
    assert swap.status_code == 200


def test_invalid_decision_mode_is_refused(env):
    headers = login(env)
    body = {"settings": {**BotSettings().model_dump(mode="json"), "decision_mode": "yolo"}}
    response = env.client.put("/api/settings", json=body, headers=headers)
    assert response.status_code == 422
    assert env.db.get_settings().decision_mode == "consensus"


def test_settings_payload_reports_which_ai_keys_exist():
    ctx = build(anthropic_api_key=SecretStr("sk-ant-x"), xai_api_key=SecretStr("  "))
    with TestClient(ctx.app) as client:
        ctx.client = client
        login(ctx)
        body = client.get("/api/settings").json()
    assert body["ai_keys"] == {"claude": True, "grok": False}  # a blank key is no key
    assert body["llm_available"] is True


def test_hard_caps_and_validation(env):
    headers = login(env)
    settings = BotSettings()
    settings.risk.max_daily_loss_pct = 30.0
    response = put_settings(env, headers, settings, totp=env.code())
    assert response.status_code == 422
    assert response.json() == {"detail": "risk.max_daily_loss_pct must be <= 25"}
    settings.risk.max_daily_loss_pct = 6.0
    settings.risk.max_order_notional_usd = 5001
    too_big = put_settings(env, headers, settings, totp=env.code())
    assert too_big.status_code == 422
    assert too_big.json() == {"detail": "risk.max_order_notional_usd must be <= 5000"}

    bad_window = BotSettings(stock_entry_start="15:00", stock_entry_cutoff="10:00")
    assert put_settings(env, headers, bad_window).status_code == 422
    bad_time = BotSettings(stock_entry_start="25:00")
    assert put_settings(env, headers, bad_time).status_code == 422
    bad_symbol = BotSettings(stock_universe=["F", "DROP TABLE;"])
    assert put_settings(env, headers, bad_symbol).status_code == 422
    too_many = BotSettings(stock_universe=[f"S{i}" for i in range(51)])
    assert put_settings(env, headers, too_many).status_code == 422

    invalid = env.client.put(
        "/api/settings", json={"settings": {"cycle_seconds": 1}}, headers=headers
    )
    assert invalid.status_code == 422
    assert env.engine.reloads == 0


def test_loosened_fields_rules():
    base = BotSettings()

    def changed(live: bool = False, **risk) -> list[str]:
        new = base.model_copy(deep=True)
        for name, value in risk.items():
            setattr(new.risk, name, value)
        return loosened_fields(base, new, live)

    assert changed(max_daily_loss_pct=8.0) == ["risk.max_daily_loss_pct"]
    assert changed(max_daily_loss_pct=4.0) == []
    assert changed(min_price=0.5) == ["risk.min_price"]  # lower is looser
    assert changed(min_price=2.0) == []
    assert changed(min_reward_risk=1.0) == ["risk.min_reward_risk"]
    assert changed(halt_cooldown_minutes=0) == ["risk.halt_cooldown_minutes"]
    assert changed(allow_leveraged_etfs=True) == ["risk.allow_leveraged_etfs"]
    assert changed(allow_delayed_paper_entries=True) == ["risk.allow_delayed_paper_entries"]
    assert changed(stop_trading_at_target=False) == ["risk.stop_trading_at_target"]
    assert changed(flatten_on_halt=False) == ["risk.flatten_on_halt"]
    assert changed(max_spread_pct=0.2) == []
    assert changed(daily_profit_target_usd=200.0) == ["risk.daily_profit_target_usd"]
    # A higher N halves the size later (or never): raising it loosens, lowering tightens.
    assert changed(loss_streak_halve_after=5) == ["risk.loss_streak_halve_after"]
    assert changed(loss_streak_halve_after=2) == []
    # Above the hard cap the engine clamps anyway, so cap -> above-cap is not loosening.
    capped = base.model_copy(deep=True)
    capped.risk.max_total_exposure_pct = 100.0
    above = capped.model_copy(deep=True)
    above.risk.max_total_exposure_pct = 150.0
    assert loosened_fields(capped, above, False) == []
    # Autopilot only counts on a live account. The product default is off.
    assert base.autopilot is False
    manual = base.model_copy(update={"autopilot": False})
    auto = base.model_copy(update={"autopilot": True})
    assert loosened_fields(manual, auto, live=False) == []
    assert loosened_fields(manual, auto, live=True) == ["autopilot"]
    assert loosened_fields(auto, manual, live=True) == []
    # Holding stocks overnight loosens the day-trade rule; turning it back on does not.
    overnight = base.model_copy(update={"flatten_stocks_before_close": False})
    assert loosened_fields(base, overnight, live=False) == ["flatten_stocks_before_close"]
    assert loosened_fields(overnight, base, live=False) == []


def test_enabling_autopilot_on_live_requires_2fa():
    ctx = build(mode=Mode.LIVE)
    ctx.db.save_settings(BotSettings(autopilot=False))
    with TestClient(ctx.app) as client:
        ctx.client = client
        headers = login(ctx)
        enabled = BotSettings(autopilot=True)
        assert put_settings(ctx, headers, enabled).status_code == 403
        assert put_settings(ctx, headers, enabled, totp=ctx.code()).status_code == 200
        assert ctx.db.get_settings().autopilot is True


def test_switching_off_loss_streak_halving_needs_2fa(env):
    headers = login(env)
    never = BotSettings()
    never.risk.loss_streak_halve_after = 1_000_000  # would disable the halving
    assert put_settings(env, headers, never).status_code == 422  # above the hard cap
    later = BotSettings()
    later.risk.loss_streak_halve_after = 6
    missing = put_settings(env, headers, later)
    assert missing.status_code == 403 and missing.json() == {"detail": "step_up_required"}
    assert put_settings(env, headers, later, totp=env.code()).status_code == 200
    sooner = BotSettings()
    sooner.risk.loss_streak_halve_after = 2  # tightening needs no code
    assert put_settings(env, headers, sooner).status_code == 200
    assert env.db.get_settings().risk.loss_streak_halve_after == 2


def test_enabling_autopilot_in_sim_needs_no_2fa(env):
    env.db.save_settings(BotSettings(autopilot=False))
    headers = login(env)
    assert put_settings(env, headers, BotSettings(autopilot=True)).status_code == 200


# ---------------------------------------------------------------------- bot control
def test_start_stop_kill_cycle(env):
    headers = login(env)
    response = env.client.post("/api/bot/start", headers=headers)
    assert response.status_code == 200 and response.json()["running"] is True
    assert env.db.get_flag("engine_running") is True
    response = env.client.post("/api/bot/stop", headers=headers)
    assert response.json()["running"] is False
    assert env.db.get_flag("engine_running") is False
    assert env.client.post("/api/bot/kill", headers=headers).json()["halted"] is True
    assert env.client.post("/api/bot/cycle", headers=headers).json() == {"ok": True}
    assert env.engine.cycles == [True]
    actions = [row["action"] for row in env.db.list_audit(10)]
    for action in ("bot_start", "bot_stop", "bot_kill", "bot_cycle"):
        assert action in actions
    assert all(row["actor"] == "owner" for row in env.db.list_audit(10))


def test_engine_failure_is_reported(env, caplog):
    headers = login(env)
    env.engine.fail = "kill"
    with caplog.at_level(logging.ERROR):
        response = env.client.post("/api/bot/kill", headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "bot_kill failed"}
    assert "exploded" not in response.text
    assert "kill exploded" in caplog.text
    audit = env.db.list_audit(1)[0]
    assert audit["action"] == "bot_kill_failed"
    assert "kill exploded" in audit["detail"]["error"]


def test_resume_requires_2fa(env):
    headers = login(env)
    env.client.post("/api/bot/kill", headers=headers)
    response = env.client.post("/api/bot/resume", headers=headers, json={})
    assert response.status_code == 403 and response.json() == {"detail": "step_up_required"}
    assert env.client.post("/api/bot/resume", headers=headers).status_code == 403  # no body
    assert "resume" not in env.engine.calls
    response = env.client.post("/api/bot/resume", headers=headers, json={"totp": env.code()})
    assert response.status_code == 200 and response.json()["halted"] is False


def test_arm_live_not_allowed(env):
    headers = login(env)
    response = env.client.post("/api/live/arm", headers=headers, json={"totp": env.code()})
    assert response.status_code == 403
    assert "TRADER_ALLOW_LIVE" in response.json()["detail"]
    assert "arm" not in env.engine.calls


def test_arm_live_needs_live_account_and_2fa(monkeypatch):
    # Live mode cannot start without Cloudflare Access, so this client presents a valid token.
    key = new_key()
    monkeypatch.setattr(cf_access, "fetch_jwks", FakeJwks(jwk(key, "k1")))
    access = {"Cf-Access-Jwt-Assertion": make_token(key)}
    ctx = build(
        mode=Mode.PAPER,
        allow_live=True,
        cf_access_team_domain=TEAM,
        cf_access_aud=AUD,
    )
    with TestClient(ctx.app, headers=access) as client:
        ctx.client = client
        headers = login(ctx)
        paper = client.post("/api/live/arm", headers=headers, json={"totp": ctx.code()})
        assert paper.status_code == 403 and "arm" not in ctx.engine.calls

    ctx = build(
        mode=Mode.LIVE,
        allow_live=True,
        cf_access_team_domain=TEAM,
        cf_access_aud=AUD,
    )
    with TestClient(ctx.app, headers=access) as client:
        ctx.client = client
        headers = login(ctx)
        missing = client.post("/api/live/arm", headers=headers, json={})
        assert missing.status_code == 403 and missing.json() == {"detail": "step_up_required"}
        armed = client.post("/api/live/arm", headers=headers, json={"totp": ctx.code()})
        assert armed.status_code == 200 and armed.json()["live_armed"] is True
        replay = client.post("/api/live/arm", headers=headers, json={"totp": ctx.code()})
        assert replay.status_code == 403  # the same code cannot be used twice
        disarmed = client.post("/api/live/disarm", headers=headers)
        assert disarmed.status_code == 200 and disarmed.json()["live_armed"] is False
        actions = [row["action"] for row in ctx.db.list_audit(10)]
        assert "live_arm" in actions and "live_disarm" in actions


def test_approve_and_reject(env):
    headers = login(env)
    order_id = pending_order(env.db)
    response = env.client.post(f"/api/orders/{order_id}/approve", headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "submitted"
    assert env.client.post(f"/api/orders/{order_id}/approve", headers=headers).status_code == 409
    assert env.client.post(f"/api/orders/{order_id}/reject", headers=headers).status_code == 409

    other = pending_order(env.db)
    response = env.client.post(f"/api/orders/{other}/reject", headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert env.client.post("/api/orders/999/approve", headers=headers).status_code == 404
    assert env.engine.calls == ["approve", "reject"]
    audit = {row["action"]: row for row in env.db.list_audit(10)}
    assert audit["order_approve"]["detail"] == {"order_id": order_id}
    assert "order_approve_failed" in audit


# ---------------------------------------------------------------------- lifespan
def test_lifespan_resumes_engine_when_flag_set():
    ctx = build(start_engine=True)
    ctx.db.set_flag("engine_running", True)
    with TestClient(ctx.app):
        assert ctx.engine.calls == ["start"]
    assert ctx.engine.calls == ["start", "shutdown"]


def test_lifespan_leaves_bot_stopped_but_protects_positions_by_default():
    ctx = build(start_engine=True)
    with TestClient(ctx.app):
        assert ctx.engine.calls == ["start_monitor"]  # monitor only: no decisions
        assert not ctx.engine.running
    assert ctx.engine.calls == ["start_monitor", "shutdown"]


def test_lifespan_without_start_engine_touches_nothing():
    ctx = build(start_engine=False)
    ctx.db.set_flag("engine_running", True)
    with TestClient(ctx.app):
        assert ctx.engine.calls == []
    assert ctx.engine.calls == []


def test_lifespan_survives_engine_start_failure():
    ctx = build(start_engine=True)
    ctx.db.set_flag("engine_running", True)
    ctx.engine.fail = "start"
    with TestClient(ctx.app) as client:
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------- Cloudflare Access
def test_cf_access_middleware(monkeypatch):
    key = new_key()
    fetch = FakeJwks(jwk(key, "k1"))
    monkeypatch.setattr(cf_access, "fetch_jwks", fetch)
    ctx = build(cf_access_team_domain=TEAM, cf_access_aud=AUD)
    with TestClient(ctx.app) as client:
        assert client.get("/healthz").status_code == 200  # exempt (container health check)
        missing = client.get("/api/session")
        assert missing.status_code == 403 and missing.json() == {"detail": "forbidden"}
        assert missing.headers["x-frame-options"] == "DENY"
        assert client.get("/login").status_code == 403
        bad = client.get("/api/session", headers={"Cf-Access-Jwt-Assertion": "x.y.z"})
        assert bad.status_code == 403
        wrong_aud = make_token(key, aud=["other-app"])
        assert (
            client.get("/api/session", headers={"Cf-Access-Jwt-Assertion": wrong_aud}).status_code
            == 403
        )
        good = {"Cf-Access-Jwt-Assertion": make_token(key)}
        assert client.get("/api/session", headers=good).json() == {"authenticated": False}
        assert client.get("/login", headers=good).status_code == 200
    assert fetch.calls == [f"https://{TEAM}/cdn-cgi/access/certs"]


def test_cf_access_off_by_default(env, monkeypatch):
    async def unexpected(url: str) -> dict:
        raise AssertionError("JWKS must not be fetched when Cloudflare Access is disabled")

    monkeypatch.setattr(cf_access, "fetch_jwks", unexpected)
    assert env.client.get("/api/session").status_code == 200


def test_startup_log_says_whether_the_access_check_is_on(caplog):
    """SEC-6: the log states the Cloudflare Access check's state at startup, so a missing
    or mistyped value is visible (DEPLOY step 9 also checks `python -m trader status`)."""
    with caplog.at_level(logging.INFO, logger="trader.web.app"):
        on = build(cf_access_team_domain=TEAM, cf_access_aud=AUD)
        off = build()
    assert f"Cloudflare Access check: enabled (team {TEAM}, aud {AUD[:8]}...)" in caplog.text
    assert "Cloudflare Access check: disabled" in caplog.text
    on.db.close()
    off.db.close()


def test_equity_chart_covers_the_whole_window_thinned(env, monkeypatch):
    """A point every 5 minutes is ~288 a day: a 90-day chart must start ~90 days back, not
    stop at the newest rows, and must stay small."""
    login(env)
    monkeypatch.setattr(app_module, "EQUITY_CHART_BUCKETS", 200)
    now = datetime.now(UTC)
    start = now - timedelta(days=89, hours=23)
    for i in range(0, 90 * 288 - 12, 3):  # every 15 minutes keeps the test fast
        value = 100.0 if i == 6000 else 600.0 + i % 97  # one deep dip
        env.db.add_equity_point(value, start + timedelta(minutes=5 * i))
    curve = env.client.get("/api/equity?days=90").json()
    first = datetime.fromisoformat(curve[0]["ts"])
    assert first - start < timedelta(hours=12)  # the whole window, not its newest weeks
    assert len(curve) <= 2 * 200 + 1
    assert min(p["equity"] for p in curve) == 100.0  # the dip survives thinning
    assert [p["ts"] for p in curve] == sorted(p["ts"] for p in curve)
