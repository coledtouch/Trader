"""Single-owner authentication: argon2id passwords, TOTP with replay protection,
server-side sessions, CSRF + Origin checks, login rate limiting and step-up codes.

Sessions are random 32-byte tokens kept in a cookie; the database stores only their
SHA-256. Every TOTP code is accepted at most once (`auth.totp_last_step`), whether it was
used to log in or to confirm a sensitive action (step-up).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response

from trader.config import AppConfig
from trader.db import Database
from trader.models import utcnow
from trader.security import crypto

log = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
LOGIN_WINDOW = timedelta(minutes=15)  # rate limit and lockout window
LOGIN_DAY = timedelta(days=1)  # daily cap on failed codes when no password is required
TOTP_VALID_WINDOW = 1  # accept the previous, current and next 30 s step
TOTP_ISSUER = "AI Trader"
TOTP_ACCOUNT = "owner"
CSRF_HEADER = "X-CSRF-Token"
SECURE_COOKIE = "__Host-trader"
DEV_COOKIE = "trader_session"  # plain-http local development only (cookie_secure=False)
# Sent by the dashboard's automatic status refresh: such a GET is not owner activity, so it
# does not keep an idle session alive (it is still refused once the session has expired).
BACKGROUND_HEADER = "X-Background"
UNKNOWN_IP = "unknown"


class AuthError(Exception):
    """Base class for authentication failures (messages are safe to show)."""


class InvalidCredentials(AuthError):
    """Wrong password and/or 2FA code. Deliberately says nothing about which."""


class RateLimited(AuthError):
    """Too many failed attempts from this IP, or the global lockout is active."""


class StepUpRequired(AuthError):
    """A fresh 2FA code is required (missing, wrong or already used)."""


class PasswordPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf_token: str
    created_at: datetime
    last_seen: datetime


# ---------------------------------------------------------------------- passwords
_DEFAULT_HASHER = PasswordHasher()  # argon2id with the library's recommended parameters


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")


def hash_password(password: str, hasher: PasswordHasher = _DEFAULT_HASHER) -> str:
    check_password_policy(password)
    return hasher.hash(password)


def verify_password(
    password_hash: str, password: str, hasher: PasswordHasher = _DEFAULT_HASHER
) -> bool:
    try:
        return hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------- TOTP
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str) -> str:
    """otpauth:// URI for authenticator apps (contains the secret: show, never log)."""
    return pyotp.TOTP(secret).provisioning_uri(name=TOTP_ACCOUNT, issuer_name=TOTP_ISSUER)


def match_totp(
    secret: str, code: str | None, now: datetime, valid_window: int = TOTP_VALID_WINDOW
) -> int | None:
    """Return the time step `code` belongs to (within +-valid_window steps of `now`), else
    None. Replay protection is the caller's job: accept only steps above the last used."""
    digits = "".join((code or "").split())
    if len(digits) != 6 or not digits.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = totp.timecode(now)
    matched: int | None = None
    for step in range(current - valid_window, current + valid_window + 1):
        # Compare every candidate (no early exit) in constant time.
        if hmac.compare_digest(totp.generate_otp(step).encode(), digits.encode()):
            matched = step
    return matched


# ---------------------------------------------------------------------- helpers
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def normalize_origin(url: str) -> str | None:
    """scheme://host[:port] in lower case with default ports removed, or None if `url`
    is not an absolute http(s) URL."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme not in ("http", "https") or not netloc or "@" in netloc:
        return None
    default_port = ":443" if scheme == "https" else ":80"
    if netloc.endswith(default_port):
        netloc = netloc[: -len(default_port)]
    return f"{scheme}://{netloc}"


# ---------------------------------------------------------------------- authenticator
class Authenticator:
    """Owner credentials, login, sessions and request checks for one app instance."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        hasher: PasswordHasher | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.config = config
        self.db = db
        self._hasher = hasher or _DEFAULT_HASHER
        self._clock = clock
        # Serializes login and step-up attempts: rate-limit checks, TOTP check-and-consume
        # and recording the attempt happen as one step.
        self._lock = threading.Lock()
        self._dummy_hash: str | None = None

    # ------------------------------------------------------------ owner credentials (CLI)
    def has_owner(self) -> bool:
        return self.db.get_auth() is not None

    def set_owner(self, password: str, totp_secret: str) -> None:
        """Create or replace the owner's password and TOTP secret; ends every session."""
        password_hash = hash_password(password, self._hasher)
        secret_enc = crypto.encrypt(self.config.secret_key, totp_secret)
        self.db.set_auth(password_hash, secret_enc)
        self.db.delete_all_sessions()

    def change_password(self, password: str) -> None:
        if not self.has_owner():
            raise AuthError("no owner credentials yet: run `python -m trader setup` first")
        self.db.update_password_hash(hash_password(password, self._hasher))
        self.db.delete_all_sessions()

    def change_totp(self, totp_secret: str) -> None:
        auth = self.db.get_auth()
        if auth is None:
            raise AuthError("no owner credentials yet: run `python -m trader setup` first")
        self.db.set_auth(auth["password_hash"], crypto.encrypt(self.config.secret_key, totp_secret))
        self.db.delete_all_sessions()

    def mark_totp_used(self, step: int) -> None:
        """Record a step as used (e.g. the code typed to confirm setup)."""
        with self._lock:
            self.db.set_totp_last_step(step)

    def logout_all(self) -> None:
        self.db.delete_all_sessions()

    def check_secret_key(self) -> None:
        """Raise crypto.SecretKeyError if the stored TOTP secret cannot be decrypted."""
        auth = self.db.get_auth()
        if auth is not None:
            crypto.decrypt(self.config.secret_key, auth["totp_secret_enc"])

    # ------------------------------------------------------------ login and step-up
    def rate_limited(self, ip: str, now: datetime | None = None) -> bool:
        now = now or self._clock()
        since = now - LOGIN_WINDOW
        if self.db.count_failed_logins(since) >= self.config.login_lockout_failures:
            return True
        # Without a password a guesser needs only the code: 3 of a million are valid at
        # any time. The 15-minute lockout alone would allow ~960 guesses a day.
        if not self.config.login_password_required and (
            self.db.count_failed_logins(now - LOGIN_DAY) >= self.config.login_daily_failures
        ):
            return True
        return self.db.count_failed_logins(since, ip=ip) >= self.config.login_max_attempts_per_ip

    def login(
        self, password: str, totp: str, ip: str, user_agent: str | None = None
    ) -> tuple[str, Session]:
        """Verify password + TOTP (the code alone when login_password_required is off) and
        open a new session. Returns (cookie token, session).

        Raises RateLimited before checking anything when limited (such attempts are not
        recorded, so the lockout ends 15 minutes after the last real failure, or once the
        day's failures fall below the daily cap), and InvalidCredentials for every other
        failure. When a password is required its hash is always computed, even when no
        owner exists, so timing does not reveal why a login failed.
        """
        need_password = self.config.login_password_required
        with self._lock:  # check, verify and record atomically (no burst past the limits)
            now = self._clock()
            self.db.purge_login_attempts(now - LOGIN_DAY)
            if self.rate_limited(ip, now):
                raise RateLimited("too many failed attempts; try again later")
            auth = self.db.get_auth()
            password_ok = self._check_password(auth, password) if need_password else True
            secret = self._totp_secret(auth)
            step = match_totp(secret, totp, now) if secret else None
            ok = (
                auth is not None
                and password_ok
                and step is not None
                and step > auth["totp_last_step"]
            )
            if ok:
                self.db.set_totp_last_step(step)
            self.db.record_login_attempt(ip, success=ok)
        if not ok or auth is None:
            raise InvalidCredentials("invalid credentials")
        # Only a verified password may replace the stored hash (never the empty one sent
        # by a code-only sign-in).
        if need_password and self._hasher.check_needs_rehash(auth["password_hash"]):
            self.db.update_password_hash(self._hasher.hash(password))
        return self.create_session(ip, user_agent)

    def step_up(self, code: str | None, ip: str) -> None:
        """Consume a fresh TOTP code for a sensitive action.

        A missing code raises StepUpRequired without counting as a failure (the UI asks
        first, then retries with the code). A wrong or reused code counts toward the same
        per-IP limit and global lockout as failed logins.
        """
        if not (code or "").strip():
            raise StepUpRequired("step_up_required")
        with self._lock:
            now = self._clock()
            if self.rate_limited(ip, now):
                raise RateLimited("too many failed attempts; try again later")
            auth = self.db.get_auth()
            secret = self._totp_secret(auth)
            step = match_totp(secret, code, now) if secret else None
            if auth is not None and step is not None and step > auth["totp_last_step"]:
                self.db.set_totp_last_step(step)
                return
            self.db.record_login_attempt(ip, success=False)
        raise StepUpRequired("step_up_required")

    def _check_password(self, auth: dict[str, Any] | None, password: str) -> bool:
        if auth is None:
            if self._dummy_hash is None:
                self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))
            verify_password(self._dummy_hash, password, self._hasher)
            return False
        return verify_password(auth["password_hash"], password, self._hasher)

    def _totp_secret(self, auth: dict[str, Any] | None) -> str | None:
        if auth is None:
            return None
        try:
            return crypto.decrypt(self.config.secret_key, auth["totp_secret_enc"])
        except crypto.SecretKeyError as exc:
            log.error("cannot verify 2FA codes: %s", exc)
            return None

    # ------------------------------------------------------------ sessions
    def create_session(self, ip: str | None, user_agent: str | None) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        self.db.create_session(token_hash, csrf, ip, user_agent)
        now = utcnow()
        return token, Session(token_hash, csrf, created_at=now, last_seen=now)

    def get_session(self, token: str | None, *, touch: bool = True) -> Session | None:
        """The live session for a cookie token, else None. Expired sessions are purged on
        every lookup. `touch` refreshes the idle timer (off for background polling)."""
        if not token or len(token) > 256:
            return None
        now = self._clock()
        idle_before = now - timedelta(minutes=self.config.session_idle_minutes)
        created_before = now - timedelta(hours=self.config.session_max_hours)
        self.db.purge_sessions(idle_before=idle_before, created_before=created_before)
        token_hash = hash_token(token)
        row = self.db.get_session(token_hash)
        if row is None:
            return None
        if row["last_seen"] < idle_before or row["created_at"] < created_before:
            self.db.delete_session(token_hash)
            return None
        if touch:
            self.db.touch_session(token_hash)
        return Session(token_hash, row["csrf_token"], row["created_at"], now)

    def end_session(self, token: str | None) -> None:
        if token:
            self.db.delete_session(hash_token(token))

    @property
    def cookie_name(self) -> str:
        return SECURE_COOKIE if self.config.cookie_secure else DEV_COOKIE

    def set_session_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            self.cookie_name,
            token,
            max_age=self.config.session_max_hours * 3600,
            path="/",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.cookie_name,
            path="/",
            secure=self.config.cookie_secure,
            httponly=True,
            samesite="strict",
        )

    # ------------------------------------------------------------ request checks
    def client_ip(self, request: Request) -> str:
        """Socket peer, unless trust_proxy_headers: then only Cloudflare's CF-Connecting-IP.

        X-Forwarded-For is not used. A client can set it, so a missing Cloudflare header
        must not let a spoofed address pick the login rate-limit bucket. With no usable
        CF-Connecting-IP the socket peer (cloudflared, on this deployment) is the fallback.
        """
        if self.config.trust_proxy_headers:
            ip = _valid_ip(request.headers.get("cf-connecting-ip"))
            if ip:
                return ip
        return request.client.host if request.client and request.client.host else UNKNOWN_IP

    def origin_allowed(self, request: Request) -> bool:
        """Origin (or, without one, Referer) must be this server's own origin or one of
        public_origins. Requests carrying neither are refused."""
        origin = request.headers.get("origin")
        source = origin if origin is not None else request.headers.get("referer")
        candidate = normalize_origin(source) if source else None
        if candidate is None:
            return False
        allowed = {o for o in map(normalize_origin, self.config.public_origins_list) if o}
        host = request.headers.get("host")
        if host:
            own = normalize_origin(f"{request.url.scheme}://{host}")
            if own:
                allowed.add(own)
        return candidate in allowed

    def check_csrf(self, request: Request, session: Session) -> None:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent.encode("utf-8"), session.csrf_token.encode("utf-8")):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        if not self.origin_allowed(request):
            raise HTTPException(status_code=403, detail="origin not allowed")


# ---------------------------------------------------------------------- FastAPI dependencies
def get_authenticator(request: Request) -> Authenticator:
    return request.app.state.auth


def require_session(request: Request) -> Session:
    auth = get_authenticator(request)
    background = request.method == "GET" and request.headers.get(BACKGROUND_HEADER) == "1"
    session = auth.get_session(request.cookies.get(auth.cookie_name), touch=not background)
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return session


def require_csrf(request: Request, session: Session = Depends(require_session)) -> Session:
    get_authenticator(request).check_csrf(request, session)
    return session
