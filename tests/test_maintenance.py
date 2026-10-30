"""Dead-man's switch pings and nightly database backups."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from trader import cli
from trader.config import AppConfig
from trader.db import Database
from trader.maintenance import (
    backup_database,
    backup_dir,
    backup_loop,
    healthcheck_loop,
    heartbeat_fresh,
    ping_once,
)
from trader.watchdog import write_heartbeat

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
PING = "https://hc-ping.com/secret-uuid-0123"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(_env_file=None, data_dir=tmp_path, healthcheck_url=SecretStr(PING))


@pytest.fixture
def filedb(config: AppConfig):
    db = Database(config.db_path)
    yield db
    db.close()


# ---------------------------------------------------------------------- dead-man's switch
def test_heartbeat_freshness_follows_the_monitor_interval(filedb: Database) -> None:
    assert not heartbeat_fresh(filedb, NOW)  # no heartbeat yet
    write_heartbeat(filedb, NOW - timedelta(seconds=50))
    assert heartbeat_fresh(filedb, NOW)  # within the 60 s floor
    write_heartbeat(filedb, NOW - timedelta(seconds=90))
    assert not heartbeat_fresh(filedb, NOW)
    settings = filedb.get_settings().model_copy(update={"monitor_seconds": 60})
    filedb.save_settings(settings)
    assert heartbeat_fresh(filedb, NOW)  # 3 x 60 s


async def test_pings_only_while_the_engine_is_alive(
    config: AppConfig, filedb: Database, caplog: pytest.LogCaptureFixture
) -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    clock = [NOW]
    write_heartbeat(filedb, NOW - timedelta(seconds=5))
    fast = config.model_copy(update={"healthcheck_every_seconds": 0.01})
    saved = {name: logging.getLogger(name).level for name in cli.QUIET_LOGGERS}
    try:
        cli.configure_logging("INFO")  # as in production: request URLs are not logged
        with caplog.at_level(logging.INFO):
            await healthcheck_loop(
                fast, filedb, clock=lambda: clock[0], transport=transport, max_rounds=2
            )
            assert hits == [PING, PING]
            clock[0] = NOW + timedelta(minutes=10)  # the engine stopped writing heartbeats
            await healthcheck_loop(
                fast, filedb, clock=lambda: clock[0], transport=transport, max_rounds=3
            )
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
    assert hits == [PING, PING]
    assert "heartbeat is stale" in caplog.text
    assert "secret-uuid" not in caplog.text


async def test_no_url_means_no_pings(tmp_path: Path, filedb: Database) -> None:
    config = AppConfig(_env_file=None, data_dir=tmp_path)
    await asyncio.wait_for(healthcheck_loop(config, filedb), timeout=1)  # returns at once


async def test_ping_failures_never_raise_or_leak_the_url(caplog: pytest.LogCaptureFixture) -> None:
    refused = httpx.MockTransport(lambda request: httpx.Response(500))

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with caplog.at_level(logging.WARNING):
        assert await ping_once(PING, transport=refused) is False
        assert await ping_once(PING, transport=httpx.MockTransport(boom)) is False
    assert "HTTP 500" in caplog.text and "ConnectError" in caplog.text
    assert "secret-uuid" not in caplog.text


# ---------------------------------------------------------------------- backups
def test_backup_is_a_private_checked_copy_with_retention(
    config: AppConfig, filedb: Database
) -> None:
    filedb.set_kv("probe", "journal row")
    dest = backup_dir(config)
    for day in ("2026-10-20", "2026-10-21", "2026-10-22"):
        backup_database(config.db_path, dest, day, keep=2)
    names = sorted(p.name for p in dest.glob("trader-*.db"))
    assert names == ["trader-2026-10-21.db", "trader-2026-10-22.db"]  # the oldest pruned
    copy = sqlite3.connect(dest / "trader-2026-10-22.db")
    try:
        row = copy.execute("SELECT value FROM kv WHERE key = 'probe'").fetchone()
    finally:
        copy.close()
    assert row == ("journal row",)
    assert not list(dest.glob(".*.tmp"))
    if sys.platform != "win32":
        assert stat.S_IMODE(dest.stat().st_mode) == 0o700
        assert stat.S_IMODE((dest / "trader-2026-10-22.db").stat().st_mode) == 0o600


async def test_backup_loop_makes_one_copy_per_new_york_day(
    config: AppConfig, filedb: Database
) -> None:
    clock = [NOW]  # 11:00 New York on 2026-10-23
    await backup_loop(config, clock=lambda: clock[0], check_every=0.01, max_rounds=3)
    assert [p.name for p in backup_dir(config).glob("trader-*.db")] == ["trader-2026-10-23.db"]
    clock[0] = NOW + timedelta(hours=14)  # 01:00 New York on the 24th
    await backup_loop(config, clock=lambda: clock[0], check_every=0.01, max_rounds=1)
    assert sorted(p.name for p in backup_dir(config).glob("trader-*.db")) == [
        "trader-2026-10-23.db",
        "trader-2026-10-24.db",
    ]


async def test_backup_loop_waits_for_a_database_and_can_be_off(tmp_path: Path) -> None:
    config = AppConfig(_env_file=None, data_dir=tmp_path)
    await backup_loop(config, clock=lambda: NOW, check_every=0.01, max_rounds=1)
    assert not backup_dir(config).exists()  # no database yet: nothing to copy
    Database(config.db_path).close()
    off = config.model_copy(update={"backup_enabled": False})
    await asyncio.wait_for(backup_loop(off, clock=lambda: NOW), timeout=1)
    assert not backup_dir(config).exists()


def test_cli_backup_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["backup", "--env-file", str(env)]) == 1  # no database yet
    assert "does not exist yet" in capsys.readouterr().err
    (tmp_path / "data").mkdir()
    Database(tmp_path / "data" / "trader.db").close()
    assert cli.main(["backup", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "Backed up to" in out and "trader-" in out
    assert list((tmp_path / "data" / "backups").glob("trader-*.db"))
