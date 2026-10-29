"""SQLite persistence: settings, auth, sessions, audit log and the trading journal.

One connection guarded by a lock. Every operation is a short statement, so it is
fine to call these methods from the asyncio event loop directly.
All timestamps are stored as ISO-8601 strings in UTC.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trader.models import (
    BotSettings,
    DayStats,
    DecisionBatch,
    Fill,
    Instrument,
    OrderRecord,
    OrderRequest,
    OrderStatus,
    SetupStats,
    SignalRecord,
    TradeRecord,
    exec_revision,
    migrate_legacy_profit_target,
    utcnow,
)

# kv: equity points before this time belong to an earlier broker account (or an earlier
# simulator run) and are left out of the equity curve.
EQUITY_SINCE_KEY = "equity_since"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    totp_secret_enc TEXT NOT NULL,
    totp_last_step INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ip TEXT,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ts ON login_attempts(ts);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    maker TEXT NOT NULL,
    model TEXT,
    market_view TEXT,
    decisions TEXT NOT NULL,
    candidates TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    refused INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    votes TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    request TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    filled_qty REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    commission REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE INDEX IF NOT EXISTS idx_orders_broker ON orders(broker_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS fills (
    exec_id TEXT PRIMARY KEY,
    broker_order_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
CREATE TABLE IF NOT EXISTS positions_meta (
    instrument TEXT PRIMARY KEY,
    stop_loss REAL,
    take_profit REAL,
    entry_price REAL,
    initial_risk REAL,
    opened_at TEXT,
    decision_id INTEGER,
    setup TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_ts TEXT NOT NULL,
    pnl REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    setup TEXT,
    decision_id INTEGER,
    r_multiple REAL,
    exit_reason TEXT NOT NULL DEFAULT '',
    delayed_data INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    instrument TEXT NOT NULL,
    setup TEXT NOT NULL,
    evaluated INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_eval ON signals(evaluated, ts);
CREATE TABLE IF NOT EXISTS research_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cohort TEXT,
    account_id TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_frames_ts ON research_frames(ts);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_ok_at TEXT,
    failures INTEGER NOT NULL DEFAULT 0
);
"""


# (table, column, declaration) added after the first release; applied at startup.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("decisions", "votes", "TEXT"),
    ("decisions", "vote_costs", "TEXT"),
    ("decisions", "vote_failures", "TEXT"),
    ("research_frames", "cohort", "TEXT"),
    ("research_frames", "account_id", "TEXT"),
    ("trades", "delayed_data", "INTEGER NOT NULL DEFAULT 0"),
)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def thin_curve(points: list[dict[str, Any]], buckets: int) -> list[dict[str, Any]]:
    """At most about 2 * `buckets` points that still span the whole curve: the lowest and
    highest point of each run of consecutive points (so dips and spikes survive), in time
    order, plus the first and the last point (the period's change is measured between
    them)."""
    if buckets < 1 or len(points) <= buckets * 2:
        return points
    size = -(-len(points) // buckets)
    out: list[dict[str, Any]] = []
    for i in range(0, len(points), size):
        chunk = points[i : i + size]
        lo = min(chunk, key=lambda p: p["equity"])
        hi = max(chunk, key=lambda p: p["equity"])
        if lo is hi:
            out.append(lo)
        else:
            out.extend((lo, hi) if lo["ts"] < hi["ts"] else (hi, lo))
    if out[0] is not points[0]:
        out.insert(0, points[0])
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
            if not Path(self.path).exists():
                Path(self.path).touch(mode=0o600)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Columns added after the first release (CREATE TABLE IF NOT EXISTS skips them)."""
        for table, column, decl in MIGRATIONS:
            names = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in names:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._conn.execute(
            "UPDATE research_frames SET cohort=COALESCE(json_extract(data, '$.cohort'), ''), "
            "account_id=COALESCE(json_extract(data, '$.account_id'), '') "
            "WHERE cohort IS NULL OR account_id IS NULL"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_research_account_cohort "
            "ON research_frames(account_id, cohort, id)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ key/value
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete_kv(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key = ?", (key,))

    def add_kv_if_absent(self, key: str, value: str) -> None:
        """Store `value` unless `key` already has one (first writer wins)."""
        self._exec("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (key, value))

    def get_flag(self, name: str, default: bool = False) -> bool:
        value = self.get_kv(f"flag:{name}")
        return default if value is None else value == "1"

    def set_flag(self, name: str, value: bool) -> None:
        self.set_kv(f"flag:{name}", "1" if value else "0")

    def get_settings(self) -> BotSettings:
        raw = self.get_kv("settings")
        if raw is None:
            return BotSettings()
        migrated = migrate_legacy_profit_target(raw)
        if migrated is not None:
            self.set_kv("settings", migrated)
            raw = migrated
        return BotSettings.model_validate_json(raw)

    def save_settings(self, settings: BotSettings) -> None:
        self.set_kv("settings", settings.model_dump_json())

    # ------------------------------------------------------------------ auth
    def get_auth(self) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM auth WHERE id = 1")
        return dict(row) if row else None

    def set_auth(self, password_hash: str, totp_secret_enc: str) -> None:
        now = _iso(utcnow())
        self._exec(
            "INSERT INTO auth(id, password_hash, totp_secret_enc, totp_last_step, created_at,"
            " updated_at) VALUES(1, ?, ?, 0, ?, ?) ON CONFLICT(id) DO UPDATE SET"
            " password_hash = excluded.password_hash, totp_secret_enc = excluded.totp_secret_enc,"
            " totp_last_step = 0, updated_at = excluded.updated_at",
            (password_hash, totp_secret_enc, now, now),
        )

    def update_password_hash(self, password_hash: str) -> None:
        self._exec(
            "UPDATE auth SET password_hash = ?, updated_at = ? WHERE id = 1",
            (password_hash, _iso(utcnow())),
        )

    def set_totp_last_step(self, step: int) -> None:
        self._exec("UPDATE auth SET totp_last_step = ? WHERE id = 1", (step,))

    # ------------------------------------------------------------------ sessions
    def create_session(
        self, token_hash: str, csrf_token: str, ip: str | None, user_agent: str | None
    ) -> None:
        now = _iso(utcnow())
        self._exec(
            "INSERT INTO sessions(token_hash, csrf_token, created_at, last_seen, ip, user_agent)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (token_hash, csrf_token, now, now, ip, (user_agent or "")[:300]),
        )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
        if not row:
            return None
        data = dict(row)
        data["created_at"] = _dt(data["created_at"])
        data["last_seen"] = _dt(data["last_seen"])
        return data

    def touch_session(self, token_hash: str) -> None:
        self._exec(
            "UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (_iso(utcnow()), token_hash)
        )

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_sessions(self) -> None:
        self._exec("DELETE FROM sessions")

    def purge_sessions(self, idle_before: datetime, created_before: datetime) -> None:
        self._exec(
            "DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
            (_iso(idle_before), _iso(created_before)),
        )

    # ------------------------------------------------------------------ login attempts
    def record_login_attempt(self, ip: str | None, success: bool) -> None:
        self._exec(
            "INSERT INTO login_attempts(ts, ip, success) VALUES(?, ?, ?)",
            (_iso(utcnow()), ip, 1 if success else 0),
        )

    def count_failed_logins(self, since: datetime, ip: str | None = None) -> int:
        if ip is None:
            row = self._one(
                "SELECT COUNT(*) AS n FROM login_attempts WHERE success = 0 AND ts >= ?",
                (_iso(since),),
            )
        else:
            row = self._one(
                "SELECT COUNT(*) AS n FROM login_attempts WHERE success = 0 AND ts >= ? AND ip = ?",
                (_iso(since), ip),
            )
        return int(row["n"]) if row else 0

    def delete_failed_logins(self) -> int:
        """Remove every failed attempt (the CLI `unlock`). Returns how many were removed."""
        return self._exec("DELETE FROM login_attempts WHERE success = 0").rowcount

    def purge_login_attempts(self, before: datetime) -> None:
        self._exec("DELETE FROM login_attempts WHERE ts < ?", (_iso(before),))

    # ------------------------------------------------------------------ audit
    def audit(
        self,
        action: str,
        detail: dict[str, Any] | None = None,
        ip: str | None = None,
        actor: str = "owner",
    ) -> None:
        self._exec(
            "INSERT INTO audit(ts, actor, action, detail, ip) VALUES(?, ?, ?, ?, ?)",
            (_iso(utcnow()), actor, action, json.dumps(detail or {}, default=str), ip),
        )

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "action": r["action"],
                "detail": json.loads(r["detail"] or "{}"),
                "ip": r["ip"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ decisions
    def insert_decision(self, batch: DecisionBatch) -> int:
        cur = self._exec(
            "INSERT INTO decisions(ts, maker, model, market_view, decisions, candidates,"
            " input_tokens, output_tokens, cost_usd, refused, error, votes,"
            " vote_costs, vote_failures)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(batch.ts),
                batch.maker,
                batch.model,
                batch.market_view,
                json.dumps([d.model_dump(mode="json") for d in batch.decisions]),
                json.dumps([c.model_dump(mode="json") for c in batch.candidates]),
                batch.input_tokens,
                batch.output_tokens,
                batch.cost_usd,
                1 if batch.refused else 0,
                batch.error,
                json.dumps(
                    {
                        maker: [d.model_dump(mode="json") for d in decisions]
                        for maker, decisions in batch.votes.items()
                    }
                )
                if batch.votes
                else None,
                json.dumps(batch.vote_costs),
                json.dumps(batch.vote_failures),
            ),
        )
        return int(cur.lastrowid)

    def list_decisions(self, limit: int = 20) -> list[DecisionBatch]:
        rows = self._all("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))
        return [
            DecisionBatch(
                id=r["id"],
                ts=_dt(r["ts"]),
                maker=r["maker"],
                model=r["model"],
                market_view=r["market_view"] or "",
                decisions=json.loads(r["decisions"]),
                candidates=json.loads(r["candidates"]),
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                cost_usd=r["cost_usd"],
                refused=bool(r["refused"]),
                error=r["error"],
                votes=json.loads(r["votes"]) if r["votes"] else {},
                vote_costs=json.loads(r["vote_costs"]) if r["vote_costs"] else {},
                vote_failures=json.loads(r["vote_failures"]) if r["vote_failures"] else {},
            )
            for r in rows
        ]

    def decision_cost(self, since: datetime, until: datetime) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost FROM decisions WHERE ts >= ? AND ts <= ?",
            (_iso(since), _iso(until)),
        )
        return float(row["cost"]) if row else 0.0

    def signal_counts(self, since: datetime, until: datetime) -> tuple[int, int]:
        row = self._one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(evaluated), 0) AS done "
            "FROM signals WHERE ts >= ? AND ts <= ?",
            (_iso(since), _iso(until)),
        )
        return (int(row["n"]), int(row["done"])) if row else (0, 0)

    # ------------------------------------------------------------------ orders
    def _order_from_row(self, r: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=r["id"],
            request=OrderRequest.model_validate_json(r["request"]),
            status=OrderStatus(r["status"]),
            broker_order_id=r["broker_order_id"],
            filled_qty=r["filled_qty"],
            avg_fill_price=r["avg_fill_price"],
            commission=r["commission"],
            message=r["message"],
            created_at=_dt(r["created_at"]),
            updated_at=_dt(r["updated_at"]),
        )

    def insert_order(self, rec: OrderRecord) -> int:
        cur = self._exec(
            "INSERT INTO orders(created_at, updated_at, request, status, broker_order_id,"
            " filled_qty, avg_fill_price, commission, message) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(rec.created_at),
                _iso(rec.updated_at),
                rec.request.model_dump_json(),
                rec.status.value,
                rec.broker_order_id,
                rec.filled_qty,
                rec.avg_fill_price,
                rec.commission,
                rec.message,
            ),
        )
        return int(cur.lastrowid)

    _ORDER_FIELDS = {
        "status",
        "broker_order_id",
        "filled_qty",
        "avg_fill_price",
        "commission",
        "message",
        "request",
    }

    def update_order(self, order_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._ORDER_FIELDS
        if unknown:
            raise ValueError(f"unknown order fields: {sorted(unknown)}")
        if not fields:
            return
        values: list[Any] = []
        assignments: list[str] = []
        for name, value in fields.items():
            if name == "status" and isinstance(value, OrderStatus):
                value = value.value
            if name == "request" and isinstance(value, OrderRequest):
                value = value.model_dump_json()
            assignments.append(f"{name} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.append(_iso(utcnow()))
        values.append(order_id)
        self._exec(f"UPDATE orders SET {', '.join(assignments)} WHERE id = ?", values)  # noqa: S608

    def get_order(self, order_id: int) -> OrderRecord | None:
        row = self._one("SELECT * FROM orders WHERE id = ?", (order_id,))
        return self._order_from_row(row) if row else None

    def get_order_by_broker_id(self, broker_order_id: str) -> OrderRecord | None:
        row = self._one(
            "SELECT * FROM orders WHERE broker_order_id = ? ORDER BY id DESC LIMIT 1",
            (broker_order_id,),
        )
        return self._order_from_row(row) if row else None

    def list_orders(
        self, limit: int = 50, statuses: Iterable[OrderStatus] | None = None
    ) -> list[OrderRecord]:
        if statuses:
            status_values = [s.value for s in statuses]
            marks = ",".join("?" for _ in status_values)
            rows = self._all(
                f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",  # noqa: S608
                [*status_values, limit],
            )
        else:
            rows = self._all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
        return [self._order_from_row(r) for r in rows]

    # ------------------------------------------------------------------ fills
    def insert_fill(self, fill: Fill) -> bool:
        """Store a fill. Returns False if this exec_id was already recorded, or when a later
        revision of the same execution (an IBKR correction) is already stored."""
        with self._lock:
            revision = exec_revision(fill.exec_id)
            if revision is not None and any(
                rev >= revision[1] for _, rev in self._revisions(revision[0])
            ):
                return False
            return self._insert_fill(fill)

    def _insert_fill(self, fill: Fill) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO fills(exec_id, broker_order_id, instrument, side, qty, price,"
            " commission, ts) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.exec_id,
                fill.broker_order_id,
                fill.instrument.key,
                fill.side.value,
                fill.qty,
                fill.price,
                fill.commission,
                _iso(fill.ts),
            ),
        )
        return cur.rowcount == 1

    def _revisions(self, base: str) -> list[tuple[Fill, int]]:
        """Stored fills that are revisions of the execution `base` (see exec_revision)."""
        rows = self._all("SELECT * FROM fills WHERE exec_id LIKE ?", (base + ".%",))
        out: list[tuple[Fill, int]] = []
        for row in rows:
            fill = self._fill_from_row(row)
            revision = exec_revision(fill.exec_id)
            if revision is not None and revision[0] == base:
                out.append((fill, revision[1]))
        return out

    def correct_fill(self, fill: Fill) -> Fill | None:
        """Apply an IBKR execution correction: when `fill` is a later revision of a stored
        execution (same id up to the last period), it replaces every earlier revision and
        the replaced fill is returned (the latest of them). None when it corrects nothing,
        e.g. a new execution, a non-IBKR id or a revision already stored."""
        revision = exec_revision(fill.exec_id)
        if revision is None:
            return None
        base, number = revision
        with self._lock:
            stored = self._revisions(base)
            older = [(f, rev) for f, rev in stored if rev < number]
            if not older or any(rev >= number for _, rev in stored):
                return None
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for old, _ in older:
                    self._conn.execute("DELETE FROM fills WHERE exec_id = ?", (old.exec_id,))
                self._insert_fill(fill)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
            return max(older, key=lambda item: item[1])[0]

    def update_fill_commission(self, exec_id: str, commission: float) -> None:
        self._exec("UPDATE fills SET commission = ? WHERE exec_id = ?", (commission, exec_id))

    def list_fills(self, since: datetime | None = None, limit: int = 500) -> list[Fill]:
        if since is None:
            rows = self._all("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))
        else:
            rows = self._all(
                "SELECT * FROM fills WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (_iso(since), limit),
            )
        return [self._fill_from_row(r) for r in rows]

    @staticmethod
    def _fill_from_row(r: sqlite3.Row) -> Fill:
        return Fill(
            exec_id=r["exec_id"],
            broker_order_id=r["broker_order_id"],
            instrument=Instrument.from_key(r["instrument"]),
            side=r["side"],
            qty=r["qty"],
            price=r["price"],
            commission=r["commission"],
            ts=_dt(r["ts"]),
        )

    # ------------------------------------------------------------------ position metadata
    def set_position_meta(
        self,
        instrument_key: str,
        *,
        stop_loss: float | None,
        take_profit: float | None,
        entry_price: float | None = None,
        initial_risk: float | None = None,
        opened_at: datetime | None = None,
        decision_id: int | None = None,
        setup: str | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO positions_meta(instrument, stop_loss, take_profit, entry_price,"
            " initial_risk, opened_at, decision_id, setup) VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(instrument) DO UPDATE SET stop_loss = excluded.stop_loss,"
            " take_profit = excluded.take_profit, entry_price = excluded.entry_price,"
            " initial_risk = excluded.initial_risk, opened_at = excluded.opened_at,"
            " decision_id = excluded.decision_id, setup = excluded.setup",
            (
                instrument_key,
                stop_loss,
                take_profit,
                entry_price,
                initial_risk,
                _iso(opened_at or utcnow()),
                decision_id,
                setup,
            ),
        )

    def get_position_meta(self, instrument_key: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM positions_meta WHERE instrument = ?", (instrument_key,))
        if not row:
            return None
        data = dict(row)
        data["opened_at"] = _dt(data["opened_at"])
        return data

    def all_position_meta(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for row in self._all("SELECT * FROM positions_meta"):
            data = dict(row)
            data["opened_at"] = _dt(data["opened_at"])
            out[data["instrument"]] = data
        return out

    def delete_position_meta(self, instrument_key: str) -> None:
        self._exec("DELETE FROM positions_meta WHERE instrument = ?", (instrument_key,))

    # ------------------------------------------------------------------ trades
    def insert_trade(self, tr: TradeRecord) -> int:
        cur = self._exec(
            "INSERT INTO trades(instrument, qty, entry_price, exit_price, entry_ts, exit_ts, pnl,"
            " commission, setup, decision_id, r_multiple, exit_reason, delayed_data)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tr.instrument.key,
                tr.qty,
                tr.entry_price,
                tr.exit_price,
                _iso(tr.entry_ts),
                _iso(tr.exit_ts),
                tr.pnl,
                tr.commission,
                tr.setup,
                tr.decision_id,
                tr.r_multiple,
                tr.exit_reason,
                int(tr.delayed_data),
            ),
        )
        return int(cur.lastrowid)

    def list_trades(self, limit: int = 50, since: datetime | None = None) -> list[TradeRecord]:
        if since is None:
            rows = self._all("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._all(
                "SELECT * FROM trades WHERE exit_ts >= ? ORDER BY id DESC LIMIT ?",
                (_iso(since), limit),
            )
        return [
            TradeRecord(
                id=r["id"],
                instrument=Instrument.from_key(r["instrument"]),
                qty=r["qty"],
                entry_price=r["entry_price"],
                exit_price=r["exit_price"],
                entry_ts=_dt(r["entry_ts"]),
                exit_ts=_dt(r["exit_ts"]),
                pnl=r["pnl"],
                commission=r["commission"],
                setup=r["setup"],
                decision_id=r["decision_id"],
                r_multiple=r["r_multiple"],
                exit_reason=r["exit_reason"],
                delayed_data=bool(r["delayed_data"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ daily stats
    def get_day(self, day: str) -> DayStats | None:
        row = self._one("SELECT data FROM daily_stats WHERE day = ?", (day,))
        return DayStats.model_validate_json(row["data"]) if row else None

    def upsert_day(self, stats: DayStats) -> None:
        self._exec(
            "INSERT INTO daily_stats(day, data) VALUES(?, ?)"
            " ON CONFLICT(day) DO UPDATE SET data = excluded.data",
            (stats.day, stats.model_dump_json()),
        )

    def list_days(self, limit: int = 30) -> list[DayStats]:
        rows = self._all("SELECT data FROM daily_stats ORDER BY day DESC LIMIT ?", (limit,))
        return [DayStats.model_validate_json(r["data"]) for r in rows]

    def latest_day_before(self, day: str) -> DayStats | None:
        row = self._one(
            "SELECT data FROM daily_stats WHERE day < ? ORDER BY day DESC LIMIT 1", (day,)
        )
        return DayStats.model_validate_json(row["data"]) if row else None

    # ------------------------------------------------------------------ equity curve
    def add_equity_point(self, equity: float, ts: datetime | None = None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO equity(ts, equity) VALUES(?, ?)",
            (_iso(ts or utcnow()), equity),
        )

    def start_equity_history(self, ts: datetime) -> None:
        """Hide earlier equity points (they belong to another broker account) from now on."""
        self.set_kv(EQUITY_SINCE_KEY, _iso(ts))

    def list_equity(
        self, since: datetime | None = None, limit: int | None = 5000
    ) -> list[dict[str, Any]]:
        """The newest `limit` points since `since` (all of them when `limit` is None),
        oldest first."""
        floor = _dt(self.get_kv(EQUITY_SINCE_KEY))
        if floor is not None and (since is None or since < floor):
            since = floor
        sql, params = "SELECT * FROM equity", []
        if since is not None:
            sql += " WHERE ts >= ?"
            params.append(_iso(since))
        sql += " ORDER BY ts DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._all(sql, params)
        return [{"ts": r["ts"], "equity": r["equity"]} for r in reversed(rows)]

    # ------------------------------------------------------------------ push subscriptions
    def save_push_subscription(
        self, sub_id: str, endpoint: str, p256dh: str, auth: str, label: str
    ) -> None:
        """Add a device, or refresh its keys and label (same endpoint)."""
        self._exec(
            "INSERT INTO push_subscriptions(id, endpoint, p256dh, auth, label, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "p256dh = excluded.p256dh, auth = excluded.auth, label = excluded.label, "
            "failures = 0",
            (sub_id, endpoint, p256dh, auth, label, _iso(utcnow())),
        )

    def list_push_subscriptions(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM push_subscriptions ORDER BY created_at, id")
        return [dict(r) for r in rows]

    def count_push_subscriptions(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM push_subscriptions")
        return int(row["n"]) if row else 0

    def delete_all_push_subscriptions(self) -> int:
        return self._exec("DELETE FROM push_subscriptions").rowcount

    def delete_push_subscription(self, sub_id: str) -> bool:
        return self._exec("DELETE FROM push_subscriptions WHERE id = ?", (sub_id,)).rowcount > 0

    def note_push_result(self, sub_id: str, ok: bool) -> None:
        if ok:
            self._exec(
                "UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE id = ?",
                (_iso(utcnow()), sub_id),
            )
        else:
            self._exec(
                "UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?", (sub_id,)
            )

    # ------------------------------------------------------------------ signal ledger
    def add_research_frame(self, payload: dict[str, Any]) -> None:
        """Local observations only; bounded history, never a broker submission queue."""
        ts = datetime.fromisoformat(payload["ts"])
        self._exec(
            "INSERT INTO research_frames(ts, cohort, account_id, data) VALUES(?, ?, ?, ?)",
            (
                _iso(ts),
                payload.get("cohort") or "",
                payload.get("account_id") or "",
                json.dumps(payload, allow_nan=False),
            ),
        )
        self._exec(
            "DELETE FROM research_frames WHERE id <= (SELECT MAX(id) - 2500 FROM research_frames)"
        )

    def research_window(
        self, since: datetime, account_id: str | None = None, limit: int = 1000
    ) -> dict[str, Any] | None:
        """Indexed metadata only: status polls must not deserialize market snapshots."""
        params = [_iso(since), *([account_id] if account_id else [])]
        query = (
            "SELECT id, cohort, account_id FROM research_frames WHERE ts >= ? "
            "AND account_id = ? ORDER BY id DESC LIMIT 1"
            if account_id
            else "SELECT id, cohort, account_id FROM research_frames WHERE ts >= ? "
            "ORDER BY id DESC LIMIT 1"
        )
        latest = self._one(
            query,
            params,
        )
        if latest is None:
            return None
        boundary = self._one(
            "SELECT COALESCE(MAX(id), 0) AS n FROM research_frames WHERE id < ? "
            "AND (cohort != ? OR account_id != ?)",
            (latest["id"], latest["cohort"], latest["account_id"]),
        )
        window = self._one(
            "SELECT COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id FROM "
            "(SELECT id FROM research_frames WHERE ts >= ? AND id > ? AND id <= ? "
            "AND cohort = ? AND account_id = ? ORDER BY id DESC LIMIT ?)",
            (
                _iso(since),
                boundary["n"],
                latest["id"],
                latest["cohort"],
                latest["account_id"],
                max(1, min(limit, 1000)),
            ),
        )
        return {
            "frame_count": int(window["n"]),
            "first_id": window["first_id"],
            "last_id": window["last_id"],
            "cohort": latest["cohort"] or None,
        }

    def research_frames_in_window(self, first_id: int, last_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT data FROM research_frames WHERE id >= ? AND id <= ? ORDER BY id",
            (first_id, last_id),
        )
        return [json.loads(r["data"]) for r in rows]

    def cash_flows(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT ts, detail FROM audit WHERE action='cash_flow' AND ts >= ? AND ts <= ? "
            "ORDER BY ts",
            (_iso(since), _iso(until)),
        )
        return [{"ts": r["ts"], **json.loads(r["detail"] or "{}")} for r in rows]

    def list_research_frames(
        self, since: datetime | None = None, limit: int = 10000
    ) -> list[dict[str, Any]]:
        where, params = ("WHERE ts >= ?", [_iso(since)]) if since else ("", [])
        rows = self._all(
            f"SELECT data FROM research_frames {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
            [*params, max(1, min(limit, 10000))],
        )
        return [json.loads(r["data"]) for r in reversed(rows)]

    def research_frame_count(self, since: datetime | None = None) -> int:
        if since is None:
            row = self._one("SELECT COUNT(*) AS n FROM research_frames")
        else:
            row = self._one(
                "SELECT COUNT(*) AS n FROM research_frames WHERE ts >= ?", (_iso(since),)
            )
        return int(row["n"]) if row else 0

    def insert_signal(self, sig: SignalRecord) -> int:
        cur = self._exec(
            "INSERT INTO signals(ts, instrument, setup, evaluated, data) VALUES(?, ?, ?, ?, ?)",
            (
                _iso(sig.ts),
                sig.instrument.key,
                sig.setup,
                1 if sig.evaluated else 0,
                sig.model_dump_json(exclude={"id"}),
            ),
        )
        return int(cur.lastrowid)

    def update_signal(self, sig: SignalRecord) -> None:
        if sig.id is None:
            raise ValueError("signal has no id")
        self._exec(
            "UPDATE signals SET evaluated = ?, data = ? WHERE id = ?",
            (1 if sig.evaluated else 0, sig.model_dump_json(exclude={"id"}), sig.id),
        )

    def _signal_from_row(self, r: sqlite3.Row) -> SignalRecord:
        sig = SignalRecord.model_validate_json(r["data"])
        sig.id = r["id"]
        return sig

    def get_signal(self, signal_id: int) -> SignalRecord | None:
        row = self._one("SELECT * FROM signals WHERE id = ?", (signal_id,))
        return self._signal_from_row(row) if row else None

    def list_signals(
        self, limit: int = 100, setup: str | None = None, since: datetime | None = None
    ) -> list[SignalRecord]:
        clauses, params = [], []
        if setup:
            clauses.append("setup = ?")
            params.append(setup)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT * FROM signals {where} ORDER BY ts DESC LIMIT ?",  # noqa: S608
            [*params, limit],
        )
        return [self._signal_from_row(r) for r in rows]

    def unevaluated_signals(self, before: datetime, limit: int = 200) -> list[SignalRecord]:
        """Signals that fired before `before` and still need their outcome scored."""
        rows = self._all(
            "SELECT * FROM signals WHERE evaluated = 0 AND ts < ? ORDER BY ts LIMIT ?",
            (_iso(before), limit),
        )
        return [self._signal_from_row(r) for r in rows]

    def setup_stats(self, since: datetime | None = None, min_samples: int = 30) -> list[SetupStats]:
        """Per-setup forward-test statistics from the signal ledger."""
        sigs = self.list_signals(limit=100_000, since=since)
        by_setup: dict[str, list[SignalRecord]] = {}
        for sig in sigs:
            by_setup.setdefault(sig.setup, []).append(sig)

        def avg(values: list[float]) -> float | None:
            return round(sum(values) / len(values), 3) if values else None

        stats: list[SetupStats] = []
        for setup, items in sorted(by_setup.items()):
            done = [x for x in items if x.evaluated]
            hits = [x for x in done if x.first_hit is not None]
            stats.append(
                SetupStats(
                    setup=setup,
                    signals=len(items),
                    taken=sum(1 for x in items if x.taken),
                    evaluated=len(done),
                    target_first_pct=(
                        round(100 * sum(1 for x in hits if x.first_hit == "target") / len(hits), 1)
                        if hits
                        else None
                    ),
                    avg_r_close=avg([x.r_close for x in done if x.r_close is not None]),
                    avg_mfe_r=avg([x.mfe_r for x in done if x.mfe_r is not None]),
                    avg_mae_r=avg([x.mae_r for x in done if x.mae_r is not None]),
                    enough_data=len(done) >= min_samples,
                )
            )
        return stats
