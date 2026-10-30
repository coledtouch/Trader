"""Background upkeep that runs inside the app process: a dead-man's switch and backups.

Dead-man's switch. An outside service (for example healthchecks.io, which can alert
through the same ntfy topic) expects a ping every few minutes and alerts when they stop.
The app pings only while the engine's heartbeat is fresh (written after every successful
monitor tick), so the alert fires when the droplet is down, the app has crashed or hung,
or the broker connection has been lost for longer than the service's grace time. The ping
URL is a secret (whoever has it can fake pings) and is never logged.

Nightly backup. A consistent copy of the SQLite database (the trade journal, signals,
settings and the encrypted login secrets) is written once per New York trading day to
`<data dir>/backups/trader-YYYY-MM-DD.db`, mode 600, keeping the newest `backup_keep`
copies. Copies live on the same server: they protect against a corrupted or damaged
database, not against losing the droplet (see DEPLOY.md for an off-server copy).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from trader.config import AppConfig
from trader.db import Database
from trader.timeutil import trading_day
from trader.watchdog import read_heartbeat

log = logging.getLogger(__name__)

PING_TIMEOUT_S = 10.0
# The heartbeat counts as fresh for this many monitor intervals (at least the floor).
FRESH_INTERVALS = 3
FRESH_FLOOR_S = 60.0
BACKUP_CHECK_EVERY_S = 3600.0
BACKUP_PREFIX = "trader-"


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------- dead-man's switch
def heartbeat_fresh(db: Database, now: datetime) -> bool:
    """True when the engine finished a monitor tick recently enough."""
    beat = read_heartbeat(db)
    if beat is None:
        return False
    try:
        monitor = float(db.get_settings().monitor_seconds)
    except Exception:
        monitor = 15.0
    limit = max(FRESH_INTERVALS * monitor, FRESH_FLOOR_S)
    return (now - beat).total_seconds() <= limit


async def ping_once(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """One ping. Never raises; failures are logged without the URL."""
    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT_S, transport=transport) as client:
            response = await client.get(url)
        if response.status_code >= 400:
            log.warning("dead-man's switch ping refused: HTTP %d", response.status_code)
            return False
        return True
    except Exception as exc:
        log.warning("dead-man's switch ping failed: %s", type(exc).__name__)
        return False


async def healthcheck_loop(
    config: AppConfig,
    db: Database,
    *,
    clock: Callable[[], datetime] = _utc_now,
    stop: asyncio.Event | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    max_rounds: int | None = None,
) -> None:
    """Ping `healthcheck_url` every `healthcheck_every_seconds` while the heartbeat is
    fresh. Does nothing when no URL is configured."""
    secret = config.healthcheck_url
    url = secret.get_secret_value().strip() if secret is not None else ""
    if not url:
        return
    stop = stop if stop is not None else asyncio.Event()
    rounds = 0
    skipped_logged = False
    while not stop.is_set():
        if heartbeat_fresh(db, clock()):
            await ping_once(url, transport=transport)
            skipped_logged = False
        elif not skipped_logged:
            log.warning("dead-man's switch: engine heartbeat is stale, not pinging")
            skipped_logged = True
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.healthcheck_every_seconds)
        except TimeoutError:
            continue


# ---------------------------------------------------------------------- backups
def backup_dir(config: AppConfig) -> Path:
    return config.data_dir / "backups"


def backup_database(db_path: Path, dest_dir: Path, day: str, keep: int) -> Path:
    """Write a consistent copy of the database as trader-<day>.db (mode 600) through the
    SQLite backup API, check it, and keep the newest `keep` copies. Returns the path."""
    dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    final = dest_dir / f"{BACKUP_PREFIX}{day}.db"
    tmp = dest_dir / f".{final.name}.tmp"
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            row = target.execute("PRAGMA integrity_check").fetchone()
        finally:
            target.close()
    finally:
        source.close()
    if not row or row[0] != "ok":
        tmp.unlink(missing_ok=True)
        raise sqlite3.DatabaseError(f"backup failed its integrity check: {row}")
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)
    copies = sorted(dest_dir.glob(f"{BACKUP_PREFIX}*.db"))
    for old in copies[: max(0, len(copies) - keep)]:
        old.unlink(missing_ok=True)
    return final


def backup_due(config: AppConfig, now: datetime) -> bool:
    return not (backup_dir(config) / f"{BACKUP_PREFIX}{trading_day(now)}.db").exists()


async def backup_loop(
    config: AppConfig,
    *,
    clock: Callable[[], datetime] = _utc_now,
    stop: asyncio.Event | None = None,
    check_every: float = BACKUP_CHECK_EVERY_S,
    max_rounds: int | None = None,
) -> None:
    """Once per New York day (checked hourly, and right after a start), back up the
    database. A failure is logged and retried at the next check."""
    if not config.backup_enabled:
        return
    stop = stop if stop is not None else asyncio.Event()
    rounds = 0
    while not stop.is_set():
        now = clock()
        if config.db_path.exists() and backup_due(config, now):
            try:
                path = await asyncio.to_thread(
                    backup_database,
                    config.db_path,
                    backup_dir(config),
                    trading_day(now),
                    config.backup_keep,
                )
                log.info("database backed up to %s", path)
            except Exception:
                log.exception("database backup failed; retrying in an hour")
        rounds += 1
        if max_rounds is not None and rounds >= max_rounds:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=check_every)
        except TimeoutError:
            continue


def start_background(config: AppConfig, db: Database) -> tuple[asyncio.Event, list[asyncio.Task]]:
    """Start both loops in the running event loop. Set the event to stop them."""
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(healthcheck_loop(config, db, stop=stop), name="deadman"),
        asyncio.create_task(backup_loop(config, stop=stop), name="backup"),
    ]
    return stop, tasks


async def stop_background(stop: asyncio.Event, tasks: list[asyncio.Task]) -> None:
    stop.set()
    for task in tasks:
        try:
            await asyncio.wait_for(task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception:
            log.exception("background task %s failed", task.get_name())
