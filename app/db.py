"""SQLite in WAL mode. Small repository layer so a later PostgreSQL move is contained."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT NOT NULL, correlation_id TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL,
  decision TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT 'RECORDED');
CREATE TABLE IF NOT EXISTS reports (
  report_id TEXT PRIMARY KEY, proposal_id TEXT, department TEXT NOT NULL,
  as_of_utc TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL,
  approved INTEGER NOT NULL, reasons TEXT NOT NULL, checks TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
  request_id TEXT PRIMARY KEY, proposal_id TEXT, state TEXT NOT NULL,
  payload TEXT NOT NULL, ticket INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
-- One broker ticket can belong to exactly one order row. Reconciliation correlates
-- orders to the broker by ticket (_mark_reconciled updates WHERE ticket=?), so a
-- duplicated ticket would let one order's evidence rewrite another order's state.
-- SQLite allows many NULLs in a unique index, so rows that have no ticket yet (a
-- submission still in flight) are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS orders_ticket_unique
  ON orders(ticket) WHERE ticket IS NOT NULL;
CREATE TABLE IF NOT EXISTS trades (
  ticket INTEGER PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
  volume TEXT NOT NULL, open_price TEXT NOT NULL, close_price TEXT NOT NULL,
  open_time TEXT NOT NULL, close_time TEXT NOT NULL, pnl TEXT NOT NULL,
  payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER NOT NULL UNIQUE,
  created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, version TEXT NOT NULL,
  config TEXT NOT NULL, status TEXT NOT NULL, metrics TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_versions (
  version TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, title TEXT NOT NULL, currency TEXT NOT NULL,
  ts_utc TEXT NOT NULL, actual TEXT, forecast TEXT, previous TEXT,
  impact TEXT NOT NULL DEFAULT 'UNKNOWN',
  source_url TEXT NOT NULL, retrieved_at TEXT NOT NULL, quality TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news (
  article_id TEXT PRIMARY KEY, headline TEXT NOT NULL, source TEXT NOT NULL,
  url TEXT, published_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS market_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
  payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kill_switch (
  id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT NOT NULL,
  updated_at TEXT NOT NULL, actor TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db(reset_armed: bool = True) -> None:
    with tx() as con:
        con.executescript(SCHEMA)
        _migrate(con)
        con.execute(
            "INSERT OR IGNORE INTO kill_switch(id, state, updated_at, actor) VALUES(1,'PAUSED',?,'system')",
            (now_iso(),),
        )
        # A restart never resumes trading. Emergency stop stays sticky.
        if reset_armed:
            con.execute(
                "UPDATE kill_switch SET state='PAUSED', updated_at=?, actor='startup' "
                "WHERE id=1 AND state='ARMED'",
                (now_iso(),),
            )


# Columns added after the first release. Additive only: existing rows are preserved
# and get the declared default.
MIGRATIONS = (("events", "impact", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),)


def _migrate(con: sqlite3.Connection) -> None:
    for table, column, declaration in MIGRATIONS:
        columns = {row["name"] for row in
                   con.execute(f"PRAGMA table_info({table})").fetchall()}
        if columns and column not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def audit(con: sqlite3.Connection, actor: str, action: str, detail, correlation_id: str | None = None) -> None:
    con.execute(
        "INSERT INTO audit_log(ts, actor, action, detail, correlation_id) VALUES(?,?,?,?,?)",
        (now_iso(), actor, action, json.dumps(detail, default=str)[:8000], correlation_id),
    )


def log(actor: str, action: str, detail, correlation_id: str | None = None) -> None:
    with tx() as con:
        audit(con, actor, action, detail, correlation_id)


def get_setting(key: str, default: str | None = None) -> str | None:
    with tx() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, actor: str = "owner") -> None:
    with tx() as con:
        con.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )
        audit(con, actor, "setting_changed", {"key": key})


def kill_switch_state() -> str:
    with tx() as con:
        row = con.execute("SELECT state FROM kill_switch WHERE id=1").fetchone()
    return row["state"] if row else "PAUSED"


def set_kill_switch(state: str, actor: str) -> str:
    if state not in ("ARMED", "PAUSED", "EMERGENCY_STOP"):
        raise ValueError("invalid kill switch state")
    with tx() as con:
        con.execute(
            "INSERT INTO kill_switch(id,state,updated_at,actor) VALUES(1,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "updated_at=excluded.updated_at, actor=excluded.actor",
            (state, now_iso(), actor),
        )
        audit(con, actor, "kill_switch", {"state": state})
    return state


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with tx() as con:
        return con.execute(sql, params).fetchall()


def ping() -> bool:
    try:
        with tx() as con:
            con.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def backup(destination: Path) -> Path:
    """Consistent SQLite backup (safe with WAL)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = connect()
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return destination