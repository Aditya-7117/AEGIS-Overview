# Extracted from the private AEGIS repository: tests/test_state_store.py at commit fcf0f74 (25 Aug 2026).
# Changes: import path; the equity-ledger test removed with the omitted methods, and its pytest import; a dash replaced.
"""State store: the durable spine. Live-armed flag and journals MUST survive restart.

These tests encode the owner's hard requirement: pressing LIVE arms the system
system-wide, and that state persists across process restarts / BAT re-runs / reboots
until explicitly disarmed.
"""
from __future__ import annotations

import sqlite3

from aegis_extract.state_store import StateStore


def test_fresh_store_defaults_to_paper(tmp_path):
    store = StateStore(tmp_path / "aegis.db")
    assert store.is_live_armed() is False


def test_arming_persists_across_restart(tmp_path):
    db = tmp_path / "aegis.db"
    s1 = StateStore(db)
    s1.set_live_armed(True, actor="ui_button")
    s1.close()

    # simulate full process restart: brand-new StateStore on same file
    s2 = StateStore(db)
    assert s2.is_live_armed() is True


def test_disarming_persists_across_restart(tmp_path):
    db = tmp_path / "aegis.db"
    s1 = StateStore(db)
    s1.set_live_armed(True, actor="ui_button")
    s1.set_live_armed(False, actor="ui_button")
    s1.close()

    s2 = StateStore(db)
    assert s2.is_live_armed() is False


def test_mode_transitions_are_audited(tmp_path):
    """Every arm/disarm is logged with actor + timestamp: non-repudiation."""
    db = tmp_path / "aegis.db"
    s = StateStore(db)
    s.set_live_armed(True, actor="ui_button")
    s.set_live_armed(False, actor="watchdog_halt")
    log = s.mode_audit_log()
    assert len(log) == 2
    assert log[0]["armed"] is True and log[0]["actor"] == "ui_button"
    assert log[1]["armed"] is False and log[1]["actor"] == "watchdog_halt"
    assert log[0]["ts"] is not None


def test_wal_mode_enabled(tmp_path):
    """WAL mode is required for crash-safety on power loss."""
    db = tmp_path / "aegis.db"
    StateStore(db)
    con = sqlite3.connect(db)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    con.close()
    assert mode.lower() == "wal"


def test_kv_state_roundtrip_and_persist(tmp_path):
    db = tmp_path / "aegis.db"
    s1 = StateStore(db)
    s1.set_state("last_bar_utc", "2026-07-04T08:00:00Z")
    s1.close()
    s2 = StateStore(db)
    assert s2.get_state("last_bar_utc") == "2026-07-04T08:00:00Z"
    assert s2.get_state("missing", default="fallback") == "fallback"
