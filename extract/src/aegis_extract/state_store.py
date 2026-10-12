# Extracted from the private AEGIS repository: aegis/state_store.py at commit fcf0f74 (25 Aug 2026).
# Changes: the equity, trade and inception tables and their methods omitted (performance bookkeeping); the docstring trimmed to match; dashes replaced. The kept methods are unchanged.
"""Durable state store: the crash-safe spine of AEGIS.

SQLite in WAL mode: single file, survives power loss, zero administration.
Holds the live-armed flag (persists across restarts), key/value runtime state and
the mode-change audit log. Journals for orders/positions live here too (added as
execution lands).

Design: one StateStore instance per process; all writes go through it. WAL lets
the UI reader see committed state without blocking the writer.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS mode_audit (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    armed INTEGER NOT NULL,
    actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alarms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    symbol       TEXT,
    message      TEXT,
    emailed      INTEGER NOT NULL DEFAULT 0,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alarms_active ON alarms(acknowledged, id);
CREATE TABLE IF NOT EXISTS order_journal (
    client_oid TEXT PRIMARY KEY,
    ts         REAL NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    state      TEXT NOT NULL,
    order_id   TEXT,
    needs_protection INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON order_journal(state);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id        TEXT NOT NULL,
    mode            TEXT NOT NULL,          -- journal scope (LGR-15): paper | live
    ts_event        TEXT NOT NULL,
    ts_recorded     TEXT NOT NULL,
    source_system   TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    kind            TEXT NOT NULL,
    legs_json       TEXT NOT NULL,          -- amounts are JSON strings, never numbers (LGR-7)
    memo            TEXT,
    hash_prev       TEXT NOT NULL,
    hash_self       TEXT NOT NULL,
    reverses        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_entry_id ON ledger_entries(entry_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_dedup
    ON ledger_entries(mode, source_system, source_event_id, kind);
CREATE INDEX IF NOT EXISTS idx_ledger_mode_id ON ledger_entries(mode, id);
CREATE TABLE IF NOT EXISTS paper_fills (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    mode     TEXT NOT NULL,
    symbol   TEXT NOT NULL,
    side     TEXT NOT NULL,
    coin_qty REAL NOT NULL,
    price    REAL NOT NULL,
    maker    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_mode_id ON paper_fills(mode, id);
CREATE TABLE IF NOT EXISTS paper_funding (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    mode   TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rate   REAL NOT NULL,
    mark   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funding_mode_id ON paper_funding(mode, id);
"""

_LIVE_ARMED_KEY = "live_armed"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: async tasks share the connection; writes are serialized
        # by the single-writer discipline at the app layer.
        self._con = sqlite3.connect(self.path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA foreign_keys=ON")
        self._con.executescript(_SCHEMA)
        self._con.commit()

    # ---- lifecycle ----
    def close(self) -> None:
        self._con.commit()
        self._con.close()

    # ---- live-armed flag ----
    def is_live_armed(self) -> bool:
        row = self._con.execute(
            "SELECT value FROM kv_state WHERE key=?", (_LIVE_ARMED_KEY,)
        ).fetchone()
        return bool(row and row["value"] == "1")

    def set_live_armed(self, armed: bool, actor: str) -> None:
        val = "1" if armed else "0"
        with self._con:
            self._con.execute(
                "INSERT INTO kv_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_LIVE_ARMED_KEY, val),
            )
            self._con.execute(
                "INSERT INTO mode_audit(ts, armed, actor) VALUES(?, ?, ?)",
                (_utcnow(), 1 if armed else 0, actor),
            )

    def mode_audit_log(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT ts, armed, actor FROM mode_audit ORDER BY id"
        ).fetchall()
        return [{"ts": r["ts"], "armed": bool(r["armed"]), "actor": r["actor"]} for r in rows]

    # ---- generic kv state ----
    def set_state(self, key: str, value: str) -> None:
        with self._con:
            self._con.execute(
                "INSERT INTO kv_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._con.execute(
            "SELECT value FROM kv_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    # ---- alarms (frontend alert queue; survives restart) ----
    def add_alarm(self, ts: float, kind: str, severity: str, symbol: str | None,
                  message: str, emailed: bool) -> int:
        with self._con:
            cur = self._con.execute(
                "INSERT INTO alarms(ts, kind, severity, symbol, message, emailed) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (ts, kind, severity, symbol, message, 1 if emailed else 0),
            )
            return int(cur.lastrowid)

    def active_alarms(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT id, ts, kind, severity, symbol, message, emailed FROM alarms "
            "WHERE acknowledged=0 ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def acknowledge_alarm(self, alarm_id: int) -> None:
        with self._con:
            self._con.execute("UPDATE alarms SET acknowledged=1 WHERE id=?", (alarm_id,))

    def last_emailed_alarm_ts(self, kind: str, symbol: str | None) -> float | None:
        row = self._con.execute(
            "SELECT ts FROM alarms WHERE kind=? AND IFNULL(symbol,'')=IFNULL(?, '') "
            "AND emailed=1 ORDER BY id DESC LIMIT 1",
            (kind, symbol),
        ).fetchone()
        return float(row["ts"]) if row else None

    # ---- order journal (write-ahead; the recovery source of truth) ----
    def journal_order(self, ts: float, client_oid: str, symbol: str, side: str, qty: int,
                      kind: str, state: str, needs_protection: bool,
                      order_id: str | None = None) -> None:
        with self._con:
            self._con.execute(
                "INSERT INTO order_journal(client_oid, ts, symbol, side, qty, kind, state, "
                "order_id, needs_protection) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(client_oid) DO UPDATE SET state=excluded.state, "
                "order_id=excluded.order_id, needs_protection=excluded.needs_protection",
                (client_oid, ts, symbol, side, qty, kind, state, order_id,
                 1 if needs_protection else 0),
            )

    def known_client_oids(self) -> set[str]:
        rows = self._con.execute("SELECT client_oid FROM order_journal").fetchall()
        return {r["client_oid"] for r in rows}

    def open_orders_journal(self) -> list[dict]:
        rows = self._con.execute(
            "SELECT client_oid, symbol, side, qty, kind, state, order_id, needs_protection "
            "FROM order_journal WHERE state NOT IN "
            "('FILLED','CANCELLED','CLOSED','REJECTED') ORDER BY ts"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- paper fills event-log (the replay source for crash recovery, QT-ARCH-3) ----
    def log_fill(self, mode: str, symbol: str, side: str, coin_qty: float, price: float,
                 maker: bool = False) -> None:
        """Append one primitive fill event. Replaying these in order through a fresh ledger
        reconstructs cash/positions/avg EXACTLY, so a paper crash is fully recoverable."""
        with self._con:
            self._con.execute(
                "INSERT INTO paper_fills(ts, mode, symbol, side, coin_qty, price, maker) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (time.time(), mode, symbol, side, float(coin_qty), float(price),
                 1 if maker else 0))

    def all_fills(self, mode: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT symbol, side, coin_qty, price, maker FROM paper_fills WHERE mode=? "
            "ORDER BY id", (mode,)).fetchall()
        return [dict(r) for r in rows]

    def log_funding(self, mode: str, symbol: str, rate: float, mark: float) -> None:
        """Append one funding event. Merged with fills for exact replay (funding uses the
        position held at that instant, so ordering vs fills matters)."""
        with self._con:
            self._con.execute(
                "INSERT INTO paper_funding(ts, mode, symbol, rate, mark) VALUES(?, ?, ?, ?, ?)",
                (time.time(), mode, symbol, float(rate), float(mark)))

    def all_events(self, mode: str) -> list[dict]:
        """Fills + funding merged in chronological order: the full replay stream (QT-ARCH-3).
        Each row has a 'kind' of 'fill' or 'funding'."""
        fills = self._con.execute(
            "SELECT ts, 'fill' AS kind, symbol, side, coin_qty, price, maker, "
            "NULL AS rate, NULL AS mark FROM paper_fills WHERE mode=?", (mode,)).fetchall()
        fund = self._con.execute(
            "SELECT ts, 'funding' AS kind, symbol, NULL AS side, NULL AS coin_qty, "
            "NULL AS price, NULL AS maker, rate, mark FROM paper_funding WHERE mode=?",
            (mode,)).fetchall()
        rows = [dict(r) for r in fills] + [dict(r) for r in fund]
        rows.sort(key=lambda r: r["ts"])
        return rows

    # ---- financial audit ledger (LGR-1..15): APPEND-ONLY, no update/delete method exists ----
    def ledger_append(self, *, entry_id: str, mode: str, ts_event: str, ts_recorded: str,
                      source_system: str, source_event_id: str, kind: str, legs_json: str,
                      memo: str, hash_prev: str, hash_self: str, reverses: str | None) -> None:
        with self._con:
            self._con.execute(
                "INSERT INTO ledger_entries(entry_id, mode, ts_event, ts_recorded, "
                "source_system, source_event_id, kind, legs_json, memo, hash_prev, "
                "hash_self, reverses) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, mode, ts_event, ts_recorded, source_system, source_event_id,
                 kind, legs_json, memo, hash_prev, hash_self, reverses))

    def ledger_find_by_source(self, mode: str, source_system: str, source_event_id: str,
                              kind: str) -> dict | None:
        row = self._con.execute(
            "SELECT entry_id FROM ledger_entries WHERE mode=? AND source_system=? "
            "AND source_event_id=? AND kind=?",
            (mode, source_system, source_event_id, kind)).fetchone()
        return dict(row) if row else None

    def ledger_last_hash(self, mode: str) -> str | None:
        row = self._con.execute(
            "SELECT hash_self FROM ledger_entries WHERE mode=? ORDER BY id DESC LIMIT 1",
            (mode,)).fetchone()
        return row["hash_self"] if row else None

    def ledger_get(self, mode: str, entry_id: str) -> dict | None:
        row = self._con.execute(
            "SELECT * FROM ledger_entries WHERE mode=? AND entry_id=?",
            (mode, entry_id)).fetchone()
        return dict(row) if row else None

    def ledger_all(self, mode: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT * FROM ledger_entries WHERE mode=? ORDER BY id", (mode,)).fetchall()
        return [dict(r) for r in rows]
