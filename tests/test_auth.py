import io
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from trader import cli
from trader.config import AppConfig
from trader.db import Database
from trader.security import crypto
from trader.security.auth import (
    Authenticator,
    InvalidCredentials,
    PasswordPolicyError,
    RateLimited,
    StepUpRequired,
    check_password_policy,
    hash_password,
    match_totp,
    new_totp_secret,
    normalize_origin,
    totp_uri,
    verify_password,
)

PASSWORD = "correct horse battery"
FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def make_config(**overrides) -> AppConfig:
    values = {
        "secret_key": SecretStr(crypto.generate_secret_key()),
        "cookie_secure": False,
        "login_max_attempts_per_ip": 3,
        "login_lockout_failures": 6,
    }
    values.update(overrides)
    return AppConfig(_env_file=None, **values)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def secret() -> str:
    return new_totp_secret()


@pytest.fixture
def auth(db, clock, secret) -> Authenticator:
    authenticator = Authenticator(make_config(), db, hasher=FAST, clock=clock)
    authenticator.set_owner(PASSWORD, secret)
    return authenticator


def code(secret: str, clock: Clock, offset: int = 0) -> str:
    totp = pyotp.TOTP(secret)
    return totp.generate_otp(totp.timecode(clock.now) + offset)


def make_request(headers: dict[str, str] | None = None, client=("10.0.0.5", 5555)) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    raw.append((b"host", b"testserver"))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/bot/start",
        "headers": raw,
        "client": client,
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
    }
    return Request(scope)


# ---------------------------------------------------------------------- passwords
def test_password_policy_and_hashing():
    with pytest.raises(PasswordPolicyError):
        check_password_policy("short")
    with pytest.raises(PasswordPolicyError):
        hash_password("x" * 11, FAST)
    hashed = hash_password(PASSWORD, FAST)
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, PASSWORD, FAST)
    assert not verify_password(hashed, PASSWORD + "!", FAST)
    assert not verify_password("not-a-hash", PASSWORD, FAST)


def test_rehash_on_login(db, clock, secret):
    config = make_config()
    Authenticator(config, db, hasher=FAST, clock=clock).set_owner(PASSWORD, secret)
    stronger = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    auth = Authenticator(config, db, hasher=stronger, clock=clock)
    auth.login(PASSWORD, code(secret, clock), "10.0.0.1")
    new_hash = db.get_auth()["password_hash"]
    assert not stronger.check_needs_rehash(new_hash)
    assert verify_password(new_hash, PASSWORD, stronger)


# ---------------------------------------------------------------------- TOTP
def test_match_totp_window(secret, clock):
    step = pyotp.TOTP(secret).timecode(clock.now)
    for offset in (-1, 0, 1):
        assert match_totp(secret, code(secret, clock, offset), clock.now) == step + offset
    for offset in (-2, 2):
        assert match_totp(secret, code(secret, clock, offset), clock.now) is None
    spaced = code(secret, clock)[:3] + " " + code(secret, clock)[3:]
    assert match_totp(secret, spaced, clock.now) == step
    for bad in ("", None, "12345", "abcdef", "1234567"):
        assert match_totp(secret, bad, clock.now) is None


def test_totp_uri_names_the_app(secret):
    uri = totp_uri(secret)
    assert uri.startswith("otpauth://totp/") and "issuer=AI%20Trader" in uri


# ---------------------------------------------------------------------- login
def test_login_success_opens_session_and_blocks_replay(auth, secret, clock, db):
    current = code(secret, clock)
    token, session = auth.login(PASSWORD, current, "10.0.0.1", "pytest")
    assert len(token) >= 40 and session.csrf_token
    assert auth.get_session(token).csrf_token == session.csrf_token
    # The raw token is never stored, only its SHA-256.
    assert db.get_session(token) is None
    with pytest.raises(InvalidCredentials):
        auth.login(PASSWORD, current, "10.0.0.1")


def test_login_rejects_older_step_after_newer_one(auth, secret, clock):
    auth.login(PASSWORD, code(secret, clock, 1), "10.0.0.1")
    with pytest.raises(InvalidCredentials):
        auth.login(PASSWORD, code(secret, clock, 0), "10.0.0.1")


def test_wrong_password_does_not_consume_the_code(auth, secret, clock, db):
    current = code(secret, clock)
    with pytest.raises(InvalidCredentials):
        auth.login("wrong password!!", current, "10.0.0.1")
    with pytest.raises(InvalidCredentials):
        auth.login(PASSWORD, "000000" if current != "000000" else "111111", "10.0.0.1")
    since = clock.now - timedelta(minutes=15)
    assert db.count_failed_logins(since, ip="10.0.0.1") == 2
    auth.login(PASSWORD, current, "10.0.0.1")


def test_login_without_owner_fails(db, clock, secret):
    auth = Authenticator(make_config(), db, hasher=FAST, clock=clock)
    with pytest.raises(InvalidCredentials):
        auth.login(PASSWORD, code(secret, clock), "10.0.0.1")


def test_login_with_wrong_secret_key_fails(db, clock, secret):
    Authenticator(make_config(), db, hasher=FAST, clock=clock).set_owner(PASSWORD, secret)
    other = Authenticator(make_config(), db, hasher=FAST, clock=clock)  # different key
    with pytest.raises(crypto.SecretKeyError):
        other.check_secret_key()
    with pytest.raises(InvalidCredentials):
        other.login(PASSWORD, code(secret, clock), "10.0.0.1")


def test_per_ip_rate_limit(auth, secret, clock, db):
    for _ in range(3):
        with pytest.raises(InvalidCredentials):
            auth.login("wrong password!!", "123456", "10.0.0.9")
    with pytest.raises(RateLimited):
        auth.login(PASSWORD, code(secret, clock), "10.0.0.9")  # even with the right credentials
    # Rate-limited attempts are not recorded, so they do not extend the lockout.
    assert db.count_failed_logins(clock.now - timedelta(minutes=15), ip="10.0.0.9") == 3
    auth.login(PASSWORD, code(secret, clock), "10.0.0.10")  # other IPs are unaffected


def test_concurrent_attempts_cannot_burst_past_the_limit(auth):
    def attempt(_: int) -> str:
        try:
            auth.login("wrong password!!", "123456", "10.0.0.66")
        except RateLimited:
            return "limited"
        except InvalidCredentials:
            return "invalid"
        return "ok"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))
    assert results.count("invalid") == 3 and results.count("limited") == 7


def test_global_lockout_and_window_expiry(auth, secret, clock):
    for i in range(6):
        with pytest.raises(InvalidCredentials):
            auth.login("wrong password!!", "123456", f"10.1.0.{i}")
    with pytest.raises(RateLimited):
        auth.login(PASSWORD, code(secret, clock), "10.2.0.1")
    clock.advance(minutes=16)
    auth.login(PASSWORD, code(secret, clock), "10.2.0.1")


# ---------------------------------------------------------------------- code-only sign-in
def _wrong_code(secret: str, clock: Clock) -> str:
    valid = {code(secret, clock, offset) for offset in (-1, 0, 1)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in valid)


def test_code_only_login_needs_no_password_and_keeps_the_hash(db, clock, secret):
    config = make_config(login_password_required=False)
    Authenticator(config, db, hasher=FAST, clock=clock).set_owner(PASSWORD, secret)
    stronger = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    auth = Authenticator(config, db, hasher=stronger, clock=clock)
    before = db.get_auth()["password_hash"]
    current = code(secret, clock)
    token, _ = auth.login("", current, "10.0.0.1")
    assert auth.get_session(token) is not None
    # An outdated hash is never "upgraded" to a hash of the empty password sent.
    assert db.get_auth()["password_hash"] == before
    with pytest.raises(InvalidCredentials):  # the code is still checked, and works once
        auth.login("", current, "10.0.0.1")
    with pytest.raises(InvalidCredentials):
        auth.login("", _wrong_code(secret, clock), "10.0.0.1")


def test_code_only_login_caps_failures_per_day(db, clock, secret):
    """The 15-minute lockout alone would let a guesser try ~960 codes a day."""
    config = make_config(login_password_required=False, login_daily_failures=8)
    auth = Authenticator(config, db, hasher=FAST, clock=clock)
    auth.set_owner(PASSWORD, secret)
    for i in range(8):  # spread out, so the per-IP and 15-minute limits never trip
        with pytest.raises(InvalidCredentials):
            auth.login("", _wrong_code(secret, clock), f"10.3.0.{i}")
        clock.advance(minutes=20)
    with pytest.raises(RateLimited) as limited:
        auth.login("", code(secret, clock), "10.4.0.1")  # even the right code, from anywhere
    assert limited.value.daily
    clock.advance(hours=24)  # the failures age out
    auth.login("", code(secret, clock), "10.4.0.1")


def test_a_strangers_guesses_do_not_block_the_owners_step_ups(db, clock, secret):
    """Review finding: the daily cap also refused step-ups in the owner's open session, so
    ~20 guesses a day from anyone kept resume and loosening limits locked."""
    config = make_config(login_password_required=False, login_daily_failures=4)
    auth = Authenticator(config, db, hasher=FAST, clock=clock)
    auth.set_owner(PASSWORD, secret)
    for i in range(4):
        with pytest.raises(InvalidCredentials):
            auth.login("", _wrong_code(secret, clock), f"10.7.0.{i}")
        clock.advance(minutes=20)
    with pytest.raises(RateLimited):
        auth.login("", code(secret, clock), "10.8.0.1")
    auth.step_up(code(secret, clock), "10.8.0.1")  # the owner, signed in, is not blocked
    clock.advance(seconds=30)
    assert auth.unlock() == 4  # the CLI `unlock`: sign-in works again at once
    auth.login("", code(secret, clock), "10.8.0.1")


@pytest.mark.parametrize(
    ("overrides", "account", "required"),
    [
        ({}, None, True),  # the default keeps the password
        ({"login_password_required": False}, None, False),  # simulator
        ({"login_password_required": False, "broker": "ibkr"}, "DU1234567", False),  # paper
        ({"login_password_required": False, "broker": "ibkr"}, "U1234567", True),  # live
        ({"login_password_required": False, "broker": "ibkr"}, None, True),  # unknown yet
        (
            {
                "login_password_required": False,
                "broker": "ibkr",
                "cf_access_team_domain": "team.cloudflareaccess.com",
                "cf_access_aud": "aud",
            },
            "U1234567",
            False,  # behind Cloudflare Access, the live account may use the code alone
        ),
    ],
)
def test_code_only_applies_only_behind_access_or_on_paper(
    db, clock, secret, overrides, account, required
):
    """Review finding: shadow mode (a live account, TRADER_ALLOW_LIVE=false) starts without
    Cloudflare Access, so the code alone must not open a live account's dashboard there."""
    auth = Authenticator(make_config(**overrides), db, hasher=FAST, clock=clock)
    auth.set_owner(PASSWORD, secret)
    if account:
        db.set_kv("broker_account", account)
    assert auth.password_required() is required
    if required:
        with pytest.raises(InvalidCredentials):
            auth.login("", code(secret, clock), "10.9.0.1")
    else:
        auth.login("", code(secret, clock), "10.9.0.1")


def test_cli_unlock_forgets_failed_attempts(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    key = crypto.generate_secret_key()
    env_file.write_text(f"TRADER_SECRET_KEY={key}\nTRADER_BROKER=sim\n", encoding="utf-8")
    monkeypatch.delenv("TRADER_SECRET_KEY", raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    db = Database(tmp_path / "data" / "trader.db")
    for _ in range(3):
        db.record_login_attempt("10.0.0.1", success=False)
    db.record_login_attempt("10.0.0.1", success=True)
    db.close()
    assert cli.main(["unlock", "--env-file", str(env_file)]) == 0
    assert "3 failed attempt(s) forgotten" in capsys.readouterr().out
    db = Database(tmp_path / "data" / "trader.db")
    assert db.count_failed_logins(datetime.now(UTC) - timedelta(days=1)) == 0
    assert [row["action"] for row in db.list_audit(5)][0] == "unlock"
    db.close()


def test_the_daily_cap_applies_only_without_a_password(auth, secret, clock):
    for i in range(25):  # more than the default daily cap of 20, spread out
        with pytest.raises(InvalidCredentials):
            auth.login("wrong password!!", code(secret, clock), f"10.5.0.{i}")
        clock.advance(minutes=20)
    auth.login(PASSWORD, code(secret, clock), "10.6.0.1")


# ---------------------------------------------------------------------- step-up
def test_step_up(auth, secret, clock, db):
    with pytest.raises(StepUpRequired):
        auth.step_up(None, "10.0.0.1")
    with pytest.raises(StepUpRequired):
        auth.step_up("  ", "10.0.0.1")
    assert db.count_failed_logins(clock.now - timedelta(minutes=15)) == 0  # missing != failure
    current = code(secret, clock)
    auth.step_up(current, "10.0.0.1")
    with pytest.raises(StepUpRequired):
        auth.step_up(current, "10.0.0.1")  # replay
    assert db.count_failed_logins(clock.now - timedelta(minutes=15)) == 1
    clock.advance(seconds=30)
    auth.step_up(code(secret, clock), "10.0.0.1")


def test_step_up_shares_the_login_rate_limit(auth, secret, clock):
    for _ in range(3):
        with pytest.raises(StepUpRequired):
            auth.step_up("999999" if code(secret, clock) != "999999" else "888888", "10.0.0.7")
    with pytest.raises(RateLimited):
        auth.step_up(code(secret, clock), "10.0.0.7")


# ---------------------------------------------------------------------- sessions
def test_session_idle_expiry(auth, secret, clock):
    token, _ = auth.login(PASSWORD, code(secret, clock), "10.0.0.1")
    clock.advance(minutes=29)
    assert auth.get_session(token) is not None
    clock.advance(minutes=32)  # idle is measured from last_seen (stored in real time)
    assert auth.get_session(token) is None


def test_session_absolute_expiry(db, clock, secret):
    config = make_config(session_idle_minutes=100_000, session_max_hours=1)
    auth = Authenticator(config, db, hasher=FAST, clock=clock)
    auth.set_owner(PASSWORD, secret)
    token, _ = auth.login(PASSWORD, code(secret, clock), "10.0.0.1")
    clock.advance(minutes=59)
    assert auth.get_session(token) is not None
    clock.advance(minutes=2)
    assert auth.get_session(token) is None


def test_logout_and_credential_changes_end_sessions(auth, secret, clock):
    token, _ = auth.login(PASSWORD, code(secret, clock), "10.0.0.1")
    auth.end_session(token)
    assert auth.get_session(token) is None
    assert auth.get_session(None) is None and auth.get_session("x" * 300) is None

    clock.advance(seconds=30)
    token, _ = auth.login(PASSWORD, code(secret, clock), "10.0.0.1")
    auth.change_password("another long password")
    assert auth.get_session(token) is None

    clock.advance(seconds=30)
    token, _ = auth.login("another long password", code(secret, clock), "10.0.0.1")
    new_secret = new_totp_secret()
    auth.change_totp(new_secret)
    assert auth.get_session(token) is None
    clock.advance(seconds=30)
    with pytest.raises(InvalidCredentials):
        auth.login("another long password", code(secret, clock), "10.0.0.1")
    auth.login("another long password", code(new_secret, clock), "10.0.0.1")


# ---------------------------------------------------------------------- request checks
def test_client_ip(db):
    plain = Authenticator(make_config(), db)
    proxied = Authenticator(make_config(trust_proxy_headers=True), db)
    headers = {"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.1, 10.0.0.2"}
    assert plain.client_ip(make_request(headers)) == "10.0.0.5"
    assert proxied.client_ip(make_request(headers)) == "203.0.113.7"
    xff_only = {"X-Forwarded-For": "198.51.100.1, 10.0.0.2"}
    # X-Forwarded-For is spoofable, so a missing or invalid Cloudflare header falls back
    # to the socket peer rather than the forwarded address.
    assert proxied.client_ip(make_request(xff_only)) == "10.0.0.5"
    assert proxied.client_ip(make_request({"CF-Connecting-IP": "junk", **xff_only})) == ("10.0.0.5")
    assert proxied.client_ip(make_request({})) == "10.0.0.5"
    assert plain.client_ip(make_request({}, client=None)) == "unknown"


def test_normalize_origin():
    assert normalize_origin("https://Desk.Example.dev:443/") == "https://desk.example.dev"
    assert normalize_origin("http://localhost:8000/x?y") == "http://localhost:8000"
    for bad in ("null", "", "ftp://x", "https://user@host", "/relative"):
        assert normalize_origin(bad) is None


def test_origin_allowed(db):
    auth = Authenticator(make_config(public_origins="https://desk.example.dev"), db)
    assert auth.origin_allowed(make_request({"Origin": "http://testserver"}))
    assert auth.origin_allowed(make_request({"Origin": "https://desk.example.dev"}))
    assert auth.origin_allowed(make_request({"Origin": "https://desk.example.dev:443"}))
    assert auth.origin_allowed(make_request({"Referer": "http://testserver/settings"}))
    assert not auth.origin_allowed(make_request({"Origin": "https://evil.example"}))
    assert not auth.origin_allowed(make_request({"Origin": "null"}))
    assert not auth.origin_allowed(make_request({"Referer": "https://evil.example/x"}))
    assert not auth.origin_allowed(make_request({}))
    # Origin wins over Referer when both are present.
    both = {"Origin": "https://evil.example", "Referer": "http://testserver/"}
    assert not auth.origin_allowed(make_request(both))


def test_check_csrf(auth, secret, clock):
    _, session = auth.login(PASSWORD, code(secret, clock), "10.0.0.1")
    good = {"X-CSRF-Token": session.csrf_token, "Origin": "http://testserver"}
    auth.check_csrf(make_request(good), session)
    for headers in (
        {"Origin": "http://testserver"},
        {"X-CSRF-Token": "nope", "Origin": "http://testserver"},
        {"X-CSRF-Token": session.csrf_token, "Origin": "https://evil.example"},
        {"X-CSRF-Token": session.csrf_token},
    ):
        with pytest.raises(HTTPException) as err:
            auth.check_csrf(make_request(headers), session)
        assert err.value.status_code == 403


# ---------------------------------------------------------------------- crypto
def test_crypto_roundtrip_and_errors():
    key = crypto.generate_secret_key()
    token = crypto.encrypt(key, "JBSWY3DPEHPK3PXP")
    assert token != "JBSWY3DPEHPK3PXP"
    assert crypto.decrypt(SecretStr(key), token) == "JBSWY3DPEHPK3PXP"
    with pytest.raises(crypto.SecretKeyError, match="not set"):
        crypto.encrypt("", "x")
    with pytest.raises(crypto.SecretKeyError, match="invalid"):
        crypto.encrypt("not-a-fernet-key", "x")
    with pytest.raises(crypto.SecretKeyError, match="does not match"):
        crypto.decrypt(crypto.generate_secret_key(), token)


# ---------------------------------------------------------------------- CLI setup
def test_cli_setup_non_interactive(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nTRADER_SECRET_KEY=\nTRADER_BROKER=sim\n", encoding="utf-8")
    inode = env_file.stat().st_ino
    secret_out = tmp_path / "totp.txt"
    monkeypatch.delenv("TRADER_SECRET_KEY", raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    args = ["--env-file", str(env_file), "--password-stdin", "--totp-secret-out", str(secret_out)]

    assert cli.main(["setup", *args]) == 0

    key = cli.read_env_value(env_file, "TRADER_SECRET_KEY")
    crypto.validate_secret_key(key)
    text = env_file.read_text(encoding="utf-8")
    assert "TRADER_BROKER=sim" in text and text.count("TRADER_SECRET_KEY=") == 1
    assert env_file.stat().st_ino == inode  # edited in place (bind mounts keep working)
    # Windows reports DOS mode bits, not Unix ownership permissions. These same
    # functional setup checks still run there; Linux CI verifies the private modes.
    if os.name != "nt":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(secret_out.stat().st_mode) == 0o600
    totp_secret = secret_out.read_text(encoding="utf-8").strip()
    assert key not in capsys.readouterr().out  # the key itself is never printed

    config = AppConfig(_env_file=env_file)
    db = Database(config.db_path)
    try:
        auth = Authenticator(config, db)
        token, _ = auth.login(PASSWORD, pyotp.TOTP(totp_secret).now(), "127.0.0.1")
        assert auth.get_session(token) is not None
    finally:
        db.close()

    # A second setup without --force refuses; the existing key is reused with --force.
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["setup", *args]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "2\n"))
    assert cli.main(["setup", *args, "--force"]) == 0
    assert cli.read_env_value(env_file, "TRADER_SECRET_KEY") == key


def test_cli_setup_interactive(tmp_path, monkeypatch, capsys):
    key = crypto.generate_secret_key()
    env_file = tmp_path / ".env"
    env_file.write_text(f"TRADER_SECRET_KEY={key}\n", encoding="utf-8")
    secret = new_totp_secret()
    monkeypatch.delenv("TRADER_SECRET_KEY", raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "new_totp_secret", lambda: secret)
    passwords = iter(["too short", PASSWORD, PASSWORD + "x", PASSWORD, PASSWORD])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(passwords))
    typed: list[str] = []

    def type_code(prompt: str = "") -> str:
        current = pyotp.TOTP(secret).now()
        typed.append(current if typed else ("000000" if current != "000000" else "111111"))
        return typed[-1]

    monkeypatch.setattr("builtins.input", type_code)

    assert cli.main(["setup", "--env-file", str(env_file)]) == 0

    out = capsys.readouterr().out
    assert "otpauth://totp/" in out and secret in out  # shown once, on the owner's terminal
    assert "at least 12 characters" in out and "do not match" in out
    config = AppConfig(_env_file=env_file)
    db = Database(config.db_path)
    try:
        assert verify_password(db.get_auth()["password_hash"], PASSWORD)
        assert crypto.decrypt(key, db.get_auth()["totp_secret_enc"]) == secret
        # The confirmation code was consumed and cannot be replayed to log in.
        auth = Authenticator(config, db)
        assert len(typed) == 2  # one wrong code, then the right one
        with pytest.raises(InvalidCredentials):
            auth.login(PASSWORD, typed[-1], "127.0.0.1")
    finally:
        db.close()


def test_cli_reset_password_logout_all_and_status(tmp_path, monkeypatch, capsys):
    key = crypto.generate_secret_key()
    env_file = tmp_path / ".env"
    xai_key = "xai-cli-secret-0123456789"
    env_file.write_text(f"TRADER_SECRET_KEY={key}\nXAI_API_KEY={xai_key}\n", encoding="utf-8")
    for name in ("TRADER_SECRET_KEY", "XAI_API_KEY", "TRADER_XAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    config = AppConfig(_env_file=env_file)
    db = Database(config.db_path)
    secret = new_totp_secret()
    auth = Authenticator(config, db, hasher=FAST)
    auth.set_owner(PASSWORD, secret)
    token, _ = auth.create_session("127.0.0.1", "pytest")

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["reset-password", "--env-file", str(env_file), "--password-stdin"]) == 1
    monkeypatch.setattr("sys.stdin", io.StringIO("a brand new password\n"))
    assert cli.main(["reset-password", "--env-file", str(env_file), "--password-stdin"]) == 0
    assert auth.get_session(token) is None
    assert verify_password(db.get_auth()["password_hash"], "a brand new password")

    token, _ = auth.create_session("127.0.0.1", "pytest")
    assert cli.main(["logout-all", "--env-file", str(env_file)]) == 0
    assert auth.get_session(token) is None

    capsys.readouterr()
    assert cli.main(["status", "--env-file", str(env_file)]) == 0
    out = capsys.readouterr().out
    assert "owner credentials" in out and "configured" in out
    assert key not in out and secret not in out
    assert xai_key not in out
    lines = {
        line.split("  ", 1)[0].strip(): line.split("  ", 1)[-1].strip() for line in out.splitlines()
    }
    assert lines["xAI API key"] == "set" and lines["decision mode"] == "consensus"
    db.close()


def test_cli_reset_2fa_writes_new_secret(tmp_path, monkeypatch):
    key = crypto.generate_secret_key()
    env_file = tmp_path / ".env"
    env_file.write_text(f"TRADER_SECRET_KEY={key}\n", encoding="utf-8")
    monkeypatch.delenv("TRADER_SECRET_KEY", raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    config = AppConfig(_env_file=env_file)
    db = Database(config.db_path)
    Authenticator(config, db, hasher=FAST).set_owner(PASSWORD, new_totp_secret())
    out = tmp_path / "new-totp.txt"
    assert cli.main(["reset-2fa", "--env-file", str(env_file), "--totp-secret-out", str(out)]) == 0
    new_secret = out.read_text(encoding="utf-8").strip()
    assert crypto.decrypt(key, db.get_auth()["totp_secret_enc"]) == new_secret
    if os.name != "nt":
        assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    db.close()


def test_cli_setup_requires_secret_out_with_password_stdin(tmp_path, capsys):
    assert cli.main(["setup", "--env-file", str(tmp_path / ".env"), "--password-stdin"]) == 1
    assert "--totp-secret-out" in capsys.readouterr().err


def test_cli_compromise_commands_disarm_live(tmp_path, monkeypatch, capsys):
    """logout-all, reset-2fa, reset-password and disarm clear a stored live arm, so a phone
    thief's arming (or the owner's) never outlives the lockdown: re-arming needs a fresh
    2FA code in the app."""
    key = crypto.generate_secret_key()
    env_file = tmp_path / ".env"
    env_file.write_text(f"TRADER_SECRET_KEY={key}\n", encoding="utf-8")
    monkeypatch.delenv("TRADER_SECRET_KEY", raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    config = AppConfig(_env_file=env_file)
    db = Database(config.db_path)
    Authenticator(config, db, hasher=FAST).set_owner(PASSWORD, new_totp_secret())
    base = ["--env-file", str(env_file)]
    try:
        commands = [
            ["logout-all", *base],
            ["disarm", *base],
            ["reset-2fa", *base, "--totp-secret-out", str(tmp_path / "totp.txt")],
            ["reset-password", *base, "--password-stdin"],
        ]
        for argv in commands:
            db.set_flag("live_armed", True)
            monkeypatch.setattr("sys.stdin", io.StringIO("a brand new password\n"))
            capsys.readouterr()
            assert cli.main(argv) == 0, argv
            assert db.get_flag("live_armed") is False, argv
            assert "now disarmed" in capsys.readouterr().out, argv
        reasons = [a["detail"]["reason"] for a in db.list_audit() if a["action"] == "live_disarmed"]
        assert len(reasons) == 4 and all(a.endswith("(CLI)") for a in reasons)
        assert cli.main(["disarm", *base]) == 0
        assert "was not armed" in capsys.readouterr().out
    finally:
        db.close()


def test_serve_logging_never_writes_the_ntfy_topic(caplog):
    """httpx logs every request URL at INFO; the ntfy topic (and any ?auth= token) in that
    URL is a secret, so the app's logging setup keeps the HTTP clients at WARNING."""
    import asyncio
    import logging

    import httpx

    from trader.notify import Notifier

    url = "https://ntfy.sh/supersecret-topic-abc123?auth=TOKENINQUERY"
    config = AppConfig(_env_file=None, ntfy_url=url, ntfy_token=SecretStr("tk_header"))
    saved = {name: logging.getLogger(name).level for name in cli.QUIET_LOGGERS}
    try:
        cli.configure_logging("INFO")
        with caplog.at_level(logging.INFO):
            notifier = Notifier(
                config, transport=httpx.MockTransport(lambda request: httpx.Response(200))
            )
            assert asyncio.run(notifier.send("Trade filled", "BUY F 24 @ 10.01")) is True
        for secret in ("supersecret-topic-abc123", "TOKENINQUERY", "tk_header"):
            assert secret not in caplog.text
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def test_cli_notify_test(tmp_path, monkeypatch, capsys, caplog):
    """`notify-test` posts one alert to the configured topic, fails clearly without a topic
    or when ntfy refuses it, and never prints the secret topic URL."""
    import logging

    import httpx

    from trader.notify import Notifier

    topic = "https://ntfy.sh/supersecret-topic-abc123"
    for name in ("TRADER_NTFY_URL", "TRADER_NTFY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    saved = {name: logging.getLogger(name).level for name in cli.QUIET_LOGGERS}
    try:
        assert cli.main(["notify-test", "--env-file", str(env_file)]) == 1
        assert "No phone alerts are set up" in capsys.readouterr().err

        env_file.write_text(f"TRADER_NTFY_URL={topic}\n", encoding="utf-8")
        status = [200]
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(status[0])

        monkeypatch.setattr(
            cli,
            "Notifier",
            lambda config, db=None: Notifier(config, db, transport=httpx.MockTransport(handler)),
        )
        with caplog.at_level(logging.INFO):
            assert cli.main(["notify-test", "--env-file", str(env_file)]) == 0
            out = capsys.readouterr()
            assert "Test alert sent" in out.out
            assert len(sent) == 1 and str(sent[0].url) == topic
            assert sent[0].headers["Title"] == "AI Trader test alert"

            status[0] = 403
            assert cli.main(["notify-test", "--env-file", str(env_file)]) == 1
            err = capsys.readouterr().err
            assert "No channel accepted the test alert" in err
        assert "supersecret-topic-abc123" not in out.out + out.err + err + caplog.text
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
