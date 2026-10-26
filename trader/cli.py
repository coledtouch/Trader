"""Command line interface: `python -m trader <command>`.

Commands: serve, setup, reset-password, reset-2fa, logout-all, disarm, status, notify-test,
paper-test, backup, watchdog, backtest, research.
reset-password, reset-2fa, logout-all and disarm also clear the live-trading arm (the
compromise response): real-money orders then need a fresh Arm with a 2FA code.

Configuration comes from the process environment plus an env file: `.env` in the current
working directory by default, or `--env-file PATH`. `setup` writes TRADER_SECRET_KEY into
that same file when no key exists yet. The file is edited in place (never replaced), so a
bind-mounted file works: inside the Docker container, mount the host's .env (for example
`docker compose run --rm -v "$PWD/.env:/app/.env" trader python -m trader setup`, run from
the directory that contains it) or point --env-file at the mounted path. Recreate the
running container afterwards (`docker compose up -d --force-recreate trader`) so it reads
the new key from the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from pathlib import Path

import qrcode
from pydantic import SecretStr, ValidationError

from trader.config import AppConfig
from trader.db import Database
from trader.models import utcnow
from trader.notify import Notifier
from trader.security import crypto
from trader.security.auth import (
    MIN_PASSWORD_LENGTH,
    Authenticator,
    AuthError,
    PasswordPolicyError,
    check_password_policy,
    match_totp,
    new_totp_secret,
    totp_uri,
)

SECRET_KEY_VAR = "TRADER_SECRET_KEY"  # noqa: S105 - the variable name, not a secret
PROMPT_ATTEMPTS = 3


class CLIError(Exception):
    """A user-facing error: printed without a traceback, exit code 1."""


# ---------------------------------------------------------------------- env file
def _env_line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key


def read_env_value(path: Path, name: str) -> str | None:
    """The value of `name` in an env file (last assignment wins), or None."""
    if not path.is_file():
        return None
    value: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if _env_line_key(line) == name:
            raw = line.split("=", 1)[1].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                raw = raw[1:-1]
            value = raw
    return value


def write_env_value(path: Path, name: str, value: str) -> None:
    """Set `name=value` in the env file in place (replacing the last existing assignment,
    which is the one that wins, else appending) and restrict the file to the owner (0600)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    matches = [i for i, line in enumerate(lines) if _env_line_key(line) == name]
    if matches:
        lines[matches[-1]] = f"{name}={value}"
    else:
        lines.append(f"{name}={value}")
    content = "\n".join(lines) + "\n"
    # Write over the existing inode (rename would break a bind mount), then cut any tail.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.truncate()
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"warning: could not chmod 600 {path}: {exc}", file=sys.stderr)


def write_private_file(path: Path, content: str) -> None:
    """Create or overwrite a file readable only by the owner (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)


def _in_container() -> bool:
    return Path("/.dockerenv").exists()


def ensure_secret_key(config: AppConfig, env_path: Path) -> str:
    """Return a valid TRADER_SECRET_KEY: from the environment, else from the env file,
    else a newly generated key written to the env file."""
    key = config.secret_key.get_secret_value().strip()
    if not key:
        key = (read_env_value(env_path, SECRET_KEY_VAR) or "").strip()
    if key:
        crypto.validate_secret_key(key)
        return key
    if not env_path.exists() and _in_container():
        raise CLIError(
            f"{env_path.resolve()} does not exist inside this container, so a new "
            f"{SECRET_KEY_VAR} would be lost when it exits. Bind-mount the host's .env "
            '(e.g. -v "$PWD/.env:/app/.env") or pass --env-file with the mounted path.'
        )
    key = crypto.generate_secret_key()
    write_env_value(env_path, SECRET_KEY_VAR, key)
    print(f"Generated {SECRET_KEY_VAR} and saved it to {env_path.resolve()} (mode 600).")
    print("Keep a backup of it: without it the stored 2FA secret cannot be decrypted.")
    return key


# ---------------------------------------------------------------------- prompts
def read_new_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        check_password_policy(password)
        return password
    for _ in range(PROMPT_ATTEMPTS):
        password = getpass.getpass(f"New password (at least {MIN_PASSWORD_LENGTH} characters): ")
        try:
            check_password_policy(password)
        except PasswordPolicyError as exc:
            print(exc)
            continue
        if getpass.getpass("Repeat the password: ") != password:
            print("The passwords do not match.")
            continue
        return password
    raise CLIError("no valid password entered; nothing was changed")


def print_qr(data: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    qr.print_ascii(out=sys.stdout, invert=True)


def enroll_totp(secret_out: Path | None) -> tuple[str, int | None]:
    """Create a TOTP secret. With `secret_out` the secret is written there (0600) for
    automation; otherwise it is shown as a QR code + URI and confirmed with a current code.
    Returns (secret, confirmed step or None)."""
    secret = new_totp_secret()
    if secret_out is not None:
        write_private_file(secret_out, secret + "\n")
        print(f"2FA secret written to {secret_out} (mode 600). Add it to your authenticator app.")
        return secret, None
    uri = totp_uri(secret)
    print("\nScan this QR code with your authenticator app:\n")
    print_qr(uri)
    print(f"\nOr add it manually. Secret: {secret}")
    print(f"otpauth URI: {uri}\n")
    for _ in range(PROMPT_ATTEMPTS):
        step = match_totp(secret, input("Enter the current 6-digit code to confirm: "), utcnow())
        if step is not None:
            return secret, step
        print("That code is not valid. Check that the phone's clock is set automatically.")
    raise CLIError("2FA confirmation failed; nothing was changed")


# ---------------------------------------------------------------------- commands
def _config(args: argparse.Namespace) -> AppConfig:
    try:
        return AppConfig(_env_file=args.env_file)
    except ValidationError as exc:
        # Field names and messages only: the offending values may be secrets.
        problems = "; ".join(
            ": ".join(
                part
                for part in (
                    ".".join(str(loc) for loc in err.get("loc", ())),
                    str(err.get("msg", "")).removeprefix("Value error, "),
                )
                if part
            )
            for err in exc.errors()
        )
        raise CLIError(f"invalid configuration: {problems}") from None


# HTTP client libraries log every request URL at INFO. The ntfy topic URL is a secret (anyone
# who knows it can read or fake the alerts), so their logs are limited to warnings.
QUIET_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str) -> None:
    """Root logging for the app; request-URL logging of the HTTP clients is switched off."""
    logging.basicConfig(
        level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from trader.web.app import create_app

    config = _config(args)
    configure_logging(config.log_level)
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        server_header=False,
        proxy_headers=config.trust_proxy_headers,
        forwarded_allow_ips="*" if config.trust_proxy_headers else None,
        log_level=config.log_level.lower(),
    )
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    if args.password_stdin and args.totp_secret_out is None:
        raise CLIError("--password-stdin needs --totp-secret-out (no terminal to confirm 2FA)")
    config = _config(args)
    db = Database(config.db_path)
    try:
        if Authenticator(config, db).has_owner() and not args.force:
            if args.password_stdin:
                raise CLIError("owner credentials already exist; pass --force to replace them")
            answer = input("Owner credentials already exist. Replace them? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing changed.")
                return 1
        key = ensure_secret_key(config, Path(args.env_file))
        config = config.model_copy(update={"secret_key": SecretStr(key)})
        auth = Authenticator(config, db)
        password = read_new_password(args.password_stdin)
        secret, step = enroll_totp(args.totp_secret_out)
        auth.set_owner(password, secret)
        if step is not None:
            auth.mark_totp_used(step)  # the confirmation code cannot be replayed to log in
        db.audit("setup", actor="cli")
    finally:
        db.close()
    print("\nOwner password and 2FA saved; any existing sessions were signed out.")
    if _in_container():
        # A container keeps the environment it was created with: `docker compose restart`
        # would keep an empty TRADER_SECRET_KEY and every login would fail.
        print("Apply it with `docker compose up -d` (it recreates the container when .env")
        print("changed; or `docker compose up -d --force-recreate trader`).")
        print("`docker compose restart` does NOT reload .env.")
    else:
        print("Start the app with `python -m trader serve`.")
    return 0


def disarm_live(db: Database, reason: str) -> bool:
    """Clear the stored live-trading arm (the running app reads it on every order, so this
    takes effect at once). Returns True when it was armed. Re-arming needs the owner to press
    Arm in the app with a fresh 2FA code."""
    if not db.get_flag("live_armed"):
        return False
    db.set_flag("live_armed", False)
    db.audit("live_disarmed", {"reason": reason}, actor="cli")
    return True


def _print_disarmed(was_armed: bool) -> None:
    if was_armed:
        print("Live trading was armed and is now disarmed: press Arm in the app (with a fresh")
        print("2FA code) to send real-money orders again.")


def cmd_reset_password(args: argparse.Namespace) -> int:
    config = _config(args)
    db = Database(config.db_path)
    try:
        auth = Authenticator(config, db)
        if not auth.has_owner():
            raise CLIError("no owner credentials yet: run `python -m trader setup`")
        auth.change_password(read_new_password(args.password_stdin))
        db.audit("reset_password", actor="cli")
        was_armed = disarm_live(db, "password reset (CLI)")
    finally:
        db.close()
    print("Password changed; all sessions were signed out.")
    _print_disarmed(was_armed)
    return 0


def cmd_reset_2fa(args: argparse.Namespace) -> int:
    config = _config(args)
    crypto.validate_secret_key(config.secret_key)
    db = Database(config.db_path)
    try:
        auth = Authenticator(config, db)
        if not auth.has_owner():
            raise CLIError("no owner credentials yet: run `python -m trader setup`")
        secret, step = enroll_totp(args.totp_secret_out)
        auth.change_totp(secret)
        if step is not None:
            auth.mark_totp_used(step)
        db.audit("reset_2fa", actor="cli")
        was_armed = disarm_live(db, "2FA reset (CLI)")
    finally:
        db.close()
    print("2FA secret replaced; all sessions were signed out.")
    _print_disarmed(was_armed)
    return 0


def cmd_logout_all(args: argparse.Namespace) -> int:
    config = _config(args)
    db = Database(config.db_path)
    try:
        Authenticator(config, db).logout_all()
        db.audit("logout_all", actor="cli")
        was_armed = disarm_live(db, "logout-all (CLI)")
    finally:
        db.close()
    print("All sessions were signed out.")
    _print_disarmed(was_armed)
    return 0


def cmd_disarm(args: argparse.Namespace) -> int:
    config = _config(args)
    db = Database(config.db_path)
    try:
        was_armed = disarm_live(db, "disarm (CLI)")
    finally:
        db.close()
    if was_armed:
        _print_disarmed(True)
    else:
        print("Live trading was not armed; nothing changed.")
    return 0


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def cmd_status(args: argparse.Namespace) -> int:
    """Configuration and persisted state. Never prints secrets, only whether they are set."""
    config = _config(args)
    try:
        crypto.validate_secret_key(config.secret_key)
        key_state = "ok"
    except crypto.SecretKeyError:
        key_state = "missing" if not config.secret_key.get_secret_value() else "invalid"
    api_key = config.anthropic_api_key
    xai_key = config.xai_api_key
    rows = [
        ("data dir", str(config.data_dir.resolve())),
        ("broker", config.broker),
        ("IB gateway", f"{config.ib_host}:{config.ib_port} (client id {config.ib_client_id})"),
        ("web", f"{config.host}:{config.port}"),
        ("allowed hosts", ", ".join(config.allowed_hosts_list) or "(none)"),
        ("public origins", ", ".join(config.public_origins_list) or "(none)"),
        ("secure cookies", _yes(config.cookie_secure)),
        ("trust proxy headers", _yes(config.trust_proxy_headers)),
        ("Cloudflare Access", "enabled" if config.cf_access_enabled else "disabled"),
        ("live trading allowed", _yes(config.allow_live)),
        (
            "crypto watchdog",
            "disabled"
            if not config.watchdog_enabled
            else (
                f"enabled, client id {config.watchdog_client_id}, "
                f"stale {config.watchdog_stale_seconds:g}s, "
                f"flatten {config.watchdog_flatten_seconds:g}s, orders {config.watchdog_orders}"
            ),
        ),
        ("Anthropic API key", "set" if api_key and api_key.get_secret_value() else "not set"),
        ("xAI API key", "set" if xai_key and xai_key.get_secret_value() else "not set"),
        ("Grok model", config.xai_model),
        ("ntfy notifications", "set" if config.ntfy_url else "not set"),
        (
            "dead-man's switch",
            f"set, every {config.healthcheck_every_seconds:g}s"
            if config.healthcheck_url and config.healthcheck_url.get_secret_value().strip()
            else "not set",
        ),
        (
            "nightly backups",
            f"on, keeping {config.backup_keep}" if config.backup_enabled else "off",
        ),
        (SECRET_KEY_VAR, key_state),
    ]
    if config.db_path.exists():
        db = Database(config.db_path)
        try:
            auth = Authenticator(config, db)
            owner = auth.has_owner()
            decrypts = "n/a"
            if owner:
                try:
                    auth.check_secret_key()
                    decrypts = "yes"
                except crypto.SecretKeyError:
                    decrypts = "no (wrong or missing key)"
            settings = db.get_settings()
            from trader.watchdog import HEARTBEAT_KEY

            rows += [
                ("owner credentials", "configured" if owner else "not configured (run setup)"),
                ("2FA secret decrypts", decrypts),
                ("engine running flag", _yes(db.get_flag("engine_running"))),
                ("live armed flag", _yes(db.get_flag("live_armed"))),
                ("autopilot", _yes(settings.autopilot)),
                ("LLM enabled", _yes(settings.llm_enabled)),
                ("decision mode", settings.decision_mode),
                ("engine heartbeat", db.get_kv(HEARTBEAT_KEY) or "none"),
                ("phone push devices", str(db.count_push_subscriptions())),
            ]
        finally:
            db.close()
    else:
        rows.append(("database", f"{config.db_path} does not exist yet (run setup)"))
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        print(f"{name.ljust(width)}  {value}")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    """Send one alert to every phone-alert channel (app push and/or ntfy)."""
    config = _config(args)
    configure_logging(config.log_level)  # shows the notifier's warning; never the topic URL
    db = Database(config.db_path) if config.db_path.exists() else None
    try:
        notifier = Notifier(config, db)
        if not notifier.enabled:
            raise CLIError(
                "No phone alerts are set up. Turn them on in the app (Settings > Phone "
                "alerts), or set TRADER_NTFY_URL in .env and run `docker compose up -d`"
            )
        channels = [
            name
            for name, on in (("app push", notifier.push_enabled), ("ntfy", notifier.ntfy_enabled))
            if on
        ]
        sent = asyncio.run(
            notifier.send(
                "AI Trader test alert",
                "Phone alerts work. You will get one when the bot buys, closes a trade, halts "
                "or loses the broker connection.",
                tags=["white_check_mark"],
            )
        )
    finally:
        if db is not None:
            db.close()
    if not sent:
        raise CLIError(
            f"No channel accepted the test alert ({', '.join(channels)}; see the warnings "
            "above). For ntfy, check that TRADER_NTFY_URL looks like https://ntfy.sh/<topic>"
        )
    print(f"Test alert sent ({', '.join(channels)}). It should show within a few seconds.")
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    """Watch crypto stops from a separate process. Never buys. Does not return until stopped."""
    from trader.watchdog import HEARTBEAT_KEY, idle_forever, run_watchdog

    config = _config(args)
    configure_logging(config.log_level)
    if not config.watchdog_enabled:
        log_idle("crypto watchdog is disabled (TRADER_WATCHDOG_ENABLED=false); not connecting")
        asyncio.run(idle_forever())
        return 0
    if config.broker != "ibkr":
        log_idle(
            "crypto watchdog is idle: TRADER_BROKER is not ibkr, so there is no gateway "
            "connection to take over (crypto stops in the simulator live in the app process)"
        )
        asyncio.run(idle_forever())
        return 0
    from trader.broker.ibkr import IBKRBroker

    db = Database(config.db_path)
    broker = IBKRBroker(config.model_copy(update={"ib_client_id": config.watchdog_client_id}))
    logging.getLogger(__name__).info(
        "crypto watchdog starting (client id %s, heartbeat key %s)",
        config.watchdog_client_id,
        HEARTBEAT_KEY,
    )
    try:
        asyncio.run(run_watchdog(config, db, broker, Notifier(config, db)))
    finally:
        db.close()
    return 0


def cmd_paper_test(args: argparse.Namespace) -> int:
    """Check the order plumbing on the paper account with an order that cannot fill."""
    from trader.papertest import paper_test_client_id, run_paper_test

    config = _config(args)
    configure_logging("WARNING")
    if config.broker != "ibkr":
        raise CLIError("paper-test needs TRADER_BROKER=ibkr (it talks to IB Gateway)")
    if args.price is not None and not args.price > 0:
        raise CLIError("--price must be a positive number")
    from trader.broker.ibkr import IBKRBroker

    client_id = paper_test_client_id(config)
    broker = IBKRBroker(config.model_copy(update={"ib_client_id": client_id}))
    print(f"Paper test on {args.symbol.upper()} (API client id {client_id}). Nothing can fill.")
    checks = asyncio.run(run_paper_test(broker, symbol=args.symbol, ref_price=args.price))
    for check in checks:
        print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    if checks and all(check.ok for check in checks):
        print("All checks passed: entries go out with their stop-loss and target at IBKR.")
        return 0
    return 1


def cmd_backup(args: argparse.Namespace) -> int:
    """Back up the database now (the app also does this once a day)."""
    from trader.maintenance import backup_database, backup_dir
    from trader.timeutil import trading_day

    config = _config(args)
    if not config.db_path.exists():
        raise CLIError(f"{config.db_path} does not exist yet (run setup)")
    path = backup_database(
        config.db_path, backup_dir(config), trading_day(utcnow()), config.backup_keep
    )
    print(f"Backed up to {path} (keeping the newest {config.backup_keep}).")
    return 0


def log_idle(message: str) -> None:
    logging.getLogger("trader.watchdog").warning("%s", message)


# ---------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file",
        default=".env",
        metavar="PATH",
        help="env file to read (and, for setup, to write TRADER_SECRET_KEY to); "
        "default: .env in the current directory",
    )
    parser = argparse.ArgumentParser(prog="python -m trader", description="AI Trader")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("serve", parents=[common], help="run the web app").set_defaults(func=cmd_serve)

    setup = sub.add_parser(
        "setup", parents=[common], help="create the owner password and 2FA secret"
    )
    setup.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin (automation)"
    )
    setup.add_argument(
        "--totp-secret-out",
        type=Path,
        metavar="FILE",
        help="write the 2FA secret to FILE (mode 600) instead of showing a QR code",
    )
    setup.add_argument("--force", action="store_true", help="replace existing credentials")
    setup.set_defaults(func=cmd_setup)

    reset_pw = sub.add_parser(
        "reset-password", parents=[common], help="set a new password (signs out all sessions)"
    )
    reset_pw.add_argument("--password-stdin", action="store_true")
    reset_pw.set_defaults(func=cmd_reset_password)

    reset_2fa = sub.add_parser(
        "reset-2fa", parents=[common], help="replace the 2FA secret (signs out all sessions)"
    )
    reset_2fa.add_argument("--totp-secret-out", type=Path, metavar="FILE")
    reset_2fa.set_defaults(func=cmd_reset_2fa)

    sub.add_parser(
        "logout-all", parents=[common], help="sign out every session (also disarms live trading)"
    ).set_defaults(func=cmd_logout_all)
    sub.add_parser(
        "disarm", parents=[common], help="disarm live trading (re-arming needs a fresh 2FA code)"
    ).set_defaults(func=cmd_disarm)
    sub.add_parser("status", parents=[common], help="show configuration (no secrets)").set_defaults(
        func=cmd_status
    )
    sub.add_parser(
        "notify-test", parents=[common], help="send a test alert to your phone"
    ).set_defaults(func=cmd_notify_test)
    paper_test = sub.add_parser(
        "paper-test",
        parents=[common],
        help="check that IBKR accepts the bot's orders with stop-loss and target (paper only)",
    )
    paper_test.add_argument("--symbol", default="F", help="stock to use (default F)")
    paper_test.add_argument(
        "--price", type=float, default=None, help="reference price when no quote is available"
    )
    paper_test.set_defaults(func=cmd_paper_test)
    sub.add_parser(
        "backup", parents=[common], help="back up the database now (also done nightly)"
    ).set_defaults(func=cmd_backup)
    sub.add_parser(
        "watchdog",
        parents=[common],
        help="crypto stop watchdog (separate process; sells only, never buys)",
    ).set_defaults(func=cmd_watchdog)
    sub.add_parser(
        "backtest", add_help=False, help="run the backtester (python -m trader backtest --help)"
    )
    sub.add_parser(
        "research", add_help=False, help="replay real observations without broker access"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] == "backtest":
        from trader.backtest import main as backtest_main

        return backtest_main(args_list[1:])
    if args_list and args_list[0] == "research":
        from trader.research import main as research_main

        return research_main(args_list[1:])
    parser = build_parser()
    args = parser.parse_args(args_list)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except (CLIError, AuthError, PasswordPolicyError, crypto.SecretKeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\naborted; nothing more was changed", file=sys.stderr)
        return 130
