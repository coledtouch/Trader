"""FastAPI app: security middleware, owner authentication and the JSON API (SPEC 5.11/5.12).

Every mutating owner action is audited with the client IP. Responses are plain JSON built
with `model_dump(mode="json")`; errors are `{"detail": ...}`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from datetime import time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from trader.config import AppConfig, get_config
from trader.db import Database
from trader.models import (
    BOOL_FALSE_IS_LOOSER,
    BOOL_TRUE_IS_LOOSER,
    HARD_CAPS,
    LOWER_IS_LOOSER,
    BotSettings,
    Mode,
    OrderStatus,
    RiskLimits,
    utcnow,
)
from trader.security import crypto
from trader.security.auth import (
    Authenticator,
    InvalidCredentials,
    RateLimited,
    Session,
    StepUpRequired,
    require_csrf,
    require_session,
)
from trader.security.cf_access import HEADER as CF_ACCESS_HEADER
from trader.security.cf_access import CFAccessError, CFAccessVerifier

if TYPE_CHECKING:
    from trader.engine import Engine

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 64 * 1024
MAX_UNIVERSE = 50
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
_PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trader</title></head>
<body><h1>AI Trader</h1><p>The web interface file {name} is missing from trader/web/static.</p>
</body></html>
"""


# ---------------------------------------------------------------------- middleware
def security_headers(config: AppConfig) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if config.cookie_secure:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


def _apply_headers(headers: MutableHeaders, path: str, base: dict[str, str]) -> None:
    for name, value in base.items():
        headers[name] = value
    if (
        path == "/api"
        or path.startswith("/api/")
        or headers.get("content-type", "").startswith("text/html")
    ):
        headers["Cache-Control"] = "no-store"
    if path == "/static/sw.js":
        headers["Service-Worker-Allowed"] = "/"  # the shell's service worker controls "/"


class SecurityHeadersMiddleware:
    """Adds the security headers to every response that passes through the app."""

    def __init__(self, app: ASGIApp, headers: dict[str, str]):
        self.app = app
        self.headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                _apply_headers(MutableHeaders(scope=message), path, self.headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """Refuses request bodies larger than `max_bytes` with 413. The (small) body is read
    up front and replayed to the app, so chunked uploads are bounded too."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_bytes):
            await _too_large(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await _too_large(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"detail": "request body too large"}, status_code=413)
    await response(scope, receive, send)


class CFAccessMiddleware:
    """Requires a valid Cloudflare Access JWT on every request except /healthz."""

    def __init__(self, app: ASGIApp, verifier: CFAccessVerifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        token = Headers(scope=scope).get(CF_ACCESS_HEADER)
        try:
            if not token:
                raise CFAccessError("missing Cf-Access-Jwt-Assertion header")
            await self.verifier.verify(token)
        except CFAccessError as exc:
            log.warning("Cloudflare Access: refused %s %s: %s", scope["method"], scope["path"], exc)
            await JSONResponse({"detail": "forbidden"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------- request bodies
class LoginBody(BaseModel):
    password: str = Field(default="", max_length=1024)
    totp: str = Field(default="", max_length=16)


class TotpBody(BaseModel):
    totp: str = Field(default="", max_length=16)


class SettingsBody(BaseModel):
    settings: BotSettings
    totp: str | None = Field(default=None, max_length=16)


# ---------------------------------------------------------------------- helpers
def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def settings_diff(old: BotSettings, new: BotSettings) -> dict[str, list[Any]]:
    before = _flatten(old.model_dump(mode="json"))
    after = _flatten(new.model_dump(mode="json"))
    return {k: [before.get(k), after.get(k)] for k in sorted(after) if before.get(k) != after[k]}


def loosened_fields(old: BotSettings, new: BotSettings, live: bool) -> list[str]:
    """Settings changes that increase risk and therefore need a fresh 2FA code.

    Risk limits: LOWER_IS_LOOSER fields loosen when decreased, BOOL_TRUE_IS_LOOSER when
    switched on, BOOL_FALSE_IS_LOOSER when switched off, and every other numeric limit when
    increased (compared after clamping to HARD_CAPS, which is what the engine enforces).
    Autopilot off -> on loosens while the broker is a live account, switching off
    flatten_stocks_before_close (the bot would hold stocks overnight) always loosens, and so
    does leaving consensus decisions (another decision_mode, or llm_enabled off while in
    consensus): one model or the rules alone may then open positions. Exit rules and
    universes never count. An unclassified boolean limit counts on any change (fail safe).
    """
    loosened: list[str] = []
    for name in RiskLimits.model_fields:
        before, after = getattr(old.risk, name), getattr(new.risk, name)
        if isinstance(before, bool) or isinstance(after, bool):
            if name in BOOL_TRUE_IS_LOOSER:
                changed = after and not before
            elif name in BOOL_FALSE_IS_LOOSER:
                changed = before and not after
            else:
                changed = before != after
        else:
            cap = HARD_CAPS.get(name)
            before_n = float(before) if isinstance(before, int | float) else 0.0
            after_n = float(after) if isinstance(after, int | float) else 0.0
            if cap is not None:
                before_n, after_n = min(before_n, cap), min(after_n, cap)
            changed = after_n < before_n if name in LOWER_IS_LOOSER else after_n > before_n
        if changed:
            loosened.append(f"risk.{name}")
    if live and new.autopilot and not old.autopilot:
        loosened.append("autopilot")
    if old.flatten_stocks_before_close and not new.flatten_stocks_before_close:
        loosened.append("flatten_stocks_before_close")
    if _effective_mode(old) == "consensus" and _effective_mode(new) != "consensus":
        loosened.append("decision_mode" if new.decision_mode != "consensus" else "llm_enabled")
    return loosened


def _effective_mode(settings: BotSettings) -> str:
    return settings.decision_mode if settings.llm_enabled else "rules"


def _has_key(key: SecretStr | None) -> bool:
    return bool(key is not None and key.get_secret_value().strip())


def _normalize_universe(name: str, symbols: list[str]) -> list[str]:
    out: list[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol:
            continue
        if not _SYMBOL.match(symbol):
            raise HTTPException(status_code=422, detail=f"{name}: invalid symbol {symbol[:16]!r}")
        if symbol not in out:
            out.append(symbol)
    if len(out) > MAX_UNIVERSE:
        raise HTTPException(status_code=422, detail=f"{name}: at most {MAX_UNIVERSE} symbols")
    return out


def validate_settings(settings: BotSettings) -> BotSettings:
    """Checks beyond the model's own validation. Raises HTTPException(422)."""
    for name, cap in HARD_CAPS.items():
        if getattr(settings.risk, name) > cap:
            raise HTTPException(status_code=422, detail=f"risk.{name} must be <= {cap:g}")
    try:
        start = time.fromisoformat(settings.stock_entry_start)
        cutoff = time.fromisoformat(settings.stock_entry_cutoff)
    except ValueError:
        raise HTTPException(status_code=422, detail="entry window times must be HH:MM") from None
    if start >= cutoff:
        raise HTTPException(
            status_code=422, detail="stock_entry_start must be before stock_entry_cutoff"
        )
    return settings.model_copy(
        update={
            "stock_universe": _normalize_universe("stock_universe", settings.stock_universe),
            "crypto_universe": _normalize_universe("crypto_universe", settings.crypto_universe),
        }
    )


def _html_page(name: str) -> Response:
    path = STATIC_DIR / name
    if path.is_file():
        return FileResponse(path, media_type="text/html; charset=utf-8")
    return HTMLResponse(_PLACEHOLDER.format(name=name))


async def _manual_cycle(engine: Engine) -> None:
    try:
        await engine.run_cycle(manual=True)
    except Exception:
        log.exception("manual decision cycle failed")


def _bind(app: FastAPI, config: AppConfig, db: Database, engine: Engine) -> None:
    app.state.config = config
    app.state.db = db
    app.state.engine = engine
    app.state.auth = Authenticator(config, db)


def _startup_checks(auth: Authenticator) -> None:
    if not auth.has_owner():
        log.warning("no owner credentials yet: run `python -m trader setup` to create them")
        return
    try:
        auth.check_secret_key()
    except crypto.SecretKeyError as exc:
        log.error("logins will fail: %s", exc)


# ---------------------------------------------------------------------- application
def create_app(
    config: AppConfig | None = None,
    db: Database | None = None,
    engine: Engine | None = None,
    start_engine: bool = True,
) -> FastAPI:
    config = config or get_config()
    base_headers = security_headers(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = db if db is not None else Database(config.db_path)
        if engine is None:
            from trader.engine import build_engine  # heavy imports only when serving

            the_engine = build_engine(config, database)
        else:
            the_engine = engine
        if db is None or engine is None:
            _bind(app, config, database, the_engine)
        _startup_checks(app.state.auth)
        started = False
        if start_engine:
            # The bot resumes its previous on/off state. When it was off, the monitor still
            # runs so open positions stay protected (engine-managed stops, halts, flatten
            # before the close), exactly as after pressing Stop.
            try:
                if database.get_flag("engine_running"):
                    await the_engine.start()
                    log.info("trading engine resumed (it was running before the restart)")
                else:
                    await the_engine.start_monitor()
                started = True
            except Exception:
                log.exception("could not start the trading engine; start it from the UI")
        try:
            yield
        finally:
            if engine is None or started:
                try:
                    await the_engine.shutdown()
                except Exception:
                    log.exception("engine shutdown failed")
            if db is None:
                database.close()

    app = FastAPI(
        title="AI Trader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    if db is not None and engine is not None:
        _bind(app, config, db, engine)  # usable without running the lifespan (tests)

    # Requests pass security headers -> trusted host -> CF Access -> body limit -> routes.
    # add_middleware wraps the stack, so the innermost layer is added first.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    if config.cf_access_enabled:
        verifier = CFAccessVerifier(config.cf_access_team_domain, config.cf_access_aud)
        app.add_middleware(CFAccessMiddleware, verifier=verifier)
        log.info(
            "Cloudflare Access check: enabled (team %s, aud %s...)",
            config.cf_access_team_domain,
            config.cf_access_aud[:8],
        )
    else:
        log.warning(
            "Cloudflare Access check: disabled (TRADER_CF_ACCESS_TEAM_DOMAIN and "
            "TRADER_CF_ACCESS_AUD are not set)"
        )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=config.allowed_hosts_list, www_redirect=False
    )
    app.add_middleware(SecurityHeadersMiddleware, headers=base_headers)

    async def unhandled_error(request: Request, exc: Exception) -> Response:
        # Runs outside the middleware stack, so add the security headers here.
        response = JSONResponse({"detail": "internal error"}, status_code=500)
        _apply_headers(response.headers, request.url.path, base_headers)
        return response

    app.add_exception_handler(Exception, unhandled_error)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    # ------------------------------------------------------------ shared route helpers
    def state(request: Request) -> Any:
        return request.app.state

    def audit(request: Request, action: str, detail: dict[str, Any] | None = None) -> None:
        st = state(request)
        st.db.audit(action, detail, ip=st.auth.client_ip(request), actor="owner")

    async def step_up(request: Request, code: str | None, action: str) -> None:
        auth: Authenticator = state(request).auth
        try:
            await run_in_threadpool(auth.step_up, code, auth.client_ip(request))
        except RateLimited:
            audit(request, "step_up_rate_limited", {"action": action})
            raise HTTPException(status_code=429, detail="too many failed attempts") from None
        except StepUpRequired:
            if code:
                audit(request, "step_up_failed", {"action": action})
            raise HTTPException(status_code=403, detail="step_up_required") from None

    async def call_engine(
        request: Request, action: str, call: Awaitable[Any], detail: dict[str, Any] | None = None
    ) -> Any:
        """Await an engine action, audit the outcome and map failures to HTTP errors."""
        try:
            result = await call
        except PermissionError as exc:
            audit(request, f"{action}_denied", {**(detail or {}), "error": str(exc)[:200]})
            raise HTTPException(status_code=403, detail=str(exc) or "not allowed") from None
        except (ValueError, LookupError) as exc:
            audit(request, f"{action}_failed", {**(detail or {}), "error": str(exc)[:200]})
            raise HTTPException(status_code=409, detail=str(exc) or "conflict") from None
        except Exception as exc:
            log.exception("%s failed", action)
            audit(request, f"{action}_failed", {**(detail or {}), "error": str(exc)[:200]})
            # The browser gets a generic message. Broker and runtime text stays in the log
            # and the audit row, not in the HTTP body.
            raise HTTPException(status_code=503, detail=f"{action} failed") from None
        audit(request, action, detail)
        return result

    def engine_status(request: Request) -> dict[str, Any]:
        return state(request).engine.status().model_dump(mode="json")

    def settings_payload(request: Request) -> dict[str, Any]:
        st = state(request)
        claude = _has_key(st.config.anthropic_api_key)
        grok = _has_key(st.config.xai_api_key)
        return {
            "settings": st.db.get_settings().model_dump(mode="json"),
            "hard_caps": dict(HARD_CAPS),
            "llm_available": claude or grok,  # any AI key: otherwise rules-only decisions
            "ai_keys": {"claude": claude, "grok": grok},
            "grok_model": st.config.xai_model,
            "live_allowed": st.config.allow_live,
        }

    def require_pending(request: Request, order_id: int, action: str) -> None:
        rec = state(request).db.get_order(order_id)
        if rec is None:
            code, detail = 404, "order not found"
        elif rec.status != OrderStatus.PENDING_APPROVAL:
            code, detail = 409, "order is not pending approval"
        else:
            return
        audit(request, f"{action}_failed", {"order_id": order_id, "error": detail})
        raise HTTPException(status_code=code, detail=detail)

    # ------------------------------------------------------------ pages
    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/")
    async def index(request: Request) -> Response:
        auth: Authenticator = state(request).auth
        if auth.get_session(request.cookies.get(auth.cookie_name)) is None:
            return RedirectResponse("/login", status_code=302)
        return _html_page("index.html")

    @app.get("/login")
    async def login_page() -> Response:
        return _html_page("login.html")

    # ------------------------------------------------------------ auth
    @app.post("/api/auth/login")
    async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        auth: Authenticator = state(request).auth
        ip = auth.client_ip(request)
        try:
            token, session = await run_in_threadpool(
                auth.login, body.password, body.totp, ip, request.headers.get("user-agent")
            )
        except RateLimited:
            audit(request, "login_rate_limited")
            raise HTTPException(status_code=429, detail="too many failed attempts") from None
        except InvalidCredentials:
            audit(request, "login_failed")
            raise HTTPException(status_code=401, detail="invalid credentials") from None
        auth.end_session(request.cookies.get(auth.cookie_name))  # rotate: drop any old session
        auth.set_session_cookie(response, token)
        audit(request, "login")
        return {"ok": True, "csrf": session.csrf_token}

    @app.post("/api/auth/logout")
    async def logout(
        request: Request, response: Response, session: Session = Depends(require_csrf)
    ) -> dict[str, bool]:
        auth: Authenticator = state(request).auth
        auth.end_session(request.cookies.get(auth.cookie_name))
        auth.clear_session_cookie(response)
        audit(request, "logout")
        return {"ok": True}

    @app.get("/api/session")
    async def session_info(request: Request) -> dict[str, Any]:
        auth: Authenticator = state(request).auth
        session = auth.get_session(request.cookies.get(auth.cookie_name))
        if session is None:
            return {"authenticated": False}
        return {"authenticated": True, "csrf": session.csrf_token}

    # ------------------------------------------------------------ read-only data
    @app.get("/api/status")
    async def status(request: Request, session: Session = Depends(require_session)) -> Any:
        st = state(request)
        engine_state = st.engine.status()
        snapshot = st.engine.snapshot() or {}
        pending = st.db.list_orders(limit=100, statuses=[OrderStatus.PENDING_APPROVAL])
        today = st.db.get_day(engine_state.day) if engine_state.day else None
        return {
            "status": _dump(engine_state),
            "account": _dump(snapshot.get("account")),
            "positions": _dump(snapshot.get("positions") or []),
            "open_orders": _dump(snapshot.get("open_orders") or []),
            "pending": _dump(pending),
            "today": _dump(today),
        }

    @app.get("/api/decisions")
    async def decisions(
        request: Request,
        limit: int = Query(20, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        return _dump(state(request).db.list_decisions(limit=min(limit, 200)))

    @app.get("/api/orders")
    async def orders(
        request: Request,
        limit: int = Query(50, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        return _dump(state(request).db.list_orders(limit=min(limit, 500)))

    @app.get("/api/trades")
    async def trades(
        request: Request,
        limit: int = Query(50, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        return _dump(state(request).db.list_trades(limit=min(limit, 500)))

    @app.get("/api/equity")
    async def equity(
        request: Request,
        days: int = Query(30, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        since = utcnow() - timedelta(days=min(days, 3650))
        return state(request).db.list_equity(since=since)

    @app.get("/api/days")
    async def days_stats(
        request: Request,
        limit: int = Query(30, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        return _dump(state(request).db.list_days(limit=min(limit, 3650)))

    @app.get("/api/audit")
    async def audit_log(
        request: Request,
        limit: int = Query(50, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        return state(request).db.list_audit(limit=min(limit, 500))

    @app.get("/api/signals")
    async def signals(
        request: Request,
        limit: int = Query(100, ge=1),
        setup: str | None = Query(None, max_length=64),
        session: Session = Depends(require_session),
    ) -> Any:
        return _dump(state(request).db.list_signals(limit=min(limit, 500), setup=setup or None))

    @app.get("/api/signals/stats")
    async def signal_stats(
        request: Request,
        days: int = Query(30, ge=1),
        session: Session = Depends(require_session),
    ) -> Any:
        since = utcnow() - timedelta(days=min(days, 3650))
        return _dump(state(request).db.setup_stats(since=since))

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    async def get_settings(request: Request, session: Session = Depends(require_session)) -> Any:
        return settings_payload(request)

    @app.put("/api/settings")
    async def put_settings(
        body: SettingsBody, request: Request, session: Session = Depends(require_csrf)
    ) -> Any:
        st = state(request)
        new = validate_settings(body.settings)
        old = st.db.get_settings()
        live = st.engine.status().mode == Mode.LIVE
        loosened = loosened_fields(old, new, live)
        if loosened:
            await step_up(request, body.totp, "settings")
        st.db.save_settings(new)
        st.engine.reload_settings()
        audit(
            request,
            "settings_update",
            {"changed": settings_diff(old, new), "loosened": loosened},
        )
        return settings_payload(request)

    # ------------------------------------------------------------ bot control
    @app.post("/api/bot/start")
    async def bot_start(request: Request, session: Session = Depends(require_csrf)) -> Any:
        st = state(request)
        await call_engine(request, "bot_start", st.engine.start())
        st.db.set_flag("engine_running", True)  # a restart resumes the running state
        return engine_status(request)

    @app.post("/api/bot/stop")
    async def bot_stop(request: Request, session: Session = Depends(require_csrf)) -> Any:
        st = state(request)
        await call_engine(request, "bot_stop", st.engine.stop())
        st.db.set_flag("engine_running", False)
        return engine_status(request)

    @app.post("/api/bot/kill")
    async def bot_kill(request: Request, session: Session = Depends(require_csrf)) -> Any:
        await call_engine(request, "bot_kill", state(request).engine.kill(actor="owner"))
        return engine_status(request)

    @app.post("/api/bot/resume")
    async def bot_resume(
        request: Request,
        body: TotpBody | None = None,
        session: Session = Depends(require_csrf),
    ) -> Any:
        await step_up(request, body.totp if body else None, "bot_resume")
        await call_engine(request, "bot_resume", state(request).engine.resume(actor="owner"))
        return engine_status(request)

    @app.post("/api/bot/cycle")
    async def bot_cycle(
        request: Request, background: BackgroundTasks, session: Session = Depends(require_csrf)
    ) -> dict[str, bool]:
        background.add_task(_manual_cycle, state(request).engine)
        audit(request, "bot_cycle")
        return {"ok": True}

    # ------------------------------------------------------------ live trading
    @app.post("/api/live/arm")
    async def live_arm(
        request: Request,
        body: TotpBody | None = None,
        session: Session = Depends(require_csrf),
    ) -> Any:
        st = state(request)
        if not st.config.allow_live:
            audit(request, "live_arm_denied", {"reason": "TRADER_ALLOW_LIVE is false"})
            raise HTTPException(
                status_code=403, detail="live trading is not allowed (TRADER_ALLOW_LIVE=false)"
            )
        if st.engine.status().mode != Mode.LIVE:
            audit(request, "live_arm_denied", {"reason": "not a live account"})
            raise HTTPException(status_code=403, detail="the broker is not a live account")
        await step_up(request, body.totp if body else None, "live_arm")
        await call_engine(request, "live_arm", st.engine.arm_live(actor="owner"))
        return engine_status(request)

    @app.post("/api/live/disarm")
    async def live_disarm(request: Request, session: Session = Depends(require_csrf)) -> Any:
        await call_engine(request, "live_disarm", state(request).engine.disarm_live(actor="owner"))
        return engine_status(request)

    # ------------------------------------------------------------ approvals
    @app.post("/api/orders/{order_id}/approve")
    async def approve_order(
        order_id: int, request: Request, session: Session = Depends(require_csrf)
    ) -> Any:
        require_pending(request, order_id, "order_approve")
        call = state(request).engine.approve_order(order_id, actor="owner")
        record = await call_engine(request, "order_approve", call, {"order_id": order_id})
        return _dump(record)

    @app.post("/api/orders/{order_id}/reject")
    async def reject_order(
        order_id: int, request: Request, session: Session = Depends(require_csrf)
    ) -> Any:
        require_pending(request, order_id, "order_reject")
        call = state(request).engine.reject_order(order_id, actor="owner")
        record = await call_engine(request, "order_reject", call, {"order_id": order_id})
        return _dump(record)

    return app
