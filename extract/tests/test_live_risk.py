# Extracted from the private AEGIS repository: tests/test_live_risk.py at commit fcf0f74 (25 Aug 2026).
# Changes: import path; instrument names replaced by placeholders; the risk limits are illustrative values chosen for this extract, not the live configuration, with the boundary cases moved to match.
"""Live risk kill-switch / guard / reconciliation decisions (LIVE-6/10/11/13)."""
from __future__ import annotations

from aegis_extract import live_risk as lr


def _kw(**over):
    base = dict(read_ok=True, snapshot_fresh=True, expected={}, actual={}, tol_contracts=0,
               equity=1000.0, peak=1000.0, day_open_equity=1000.0, max_dd=0.30, max_daily=0.05,
               margin_used=0.0, available_margin=1000.0, margin_ceiling=0.60)
    base.update(over)
    return base


def test_reconcile_breaks_detects_divergence_and_respects_tolerance():
    assert lr.reconcile_breaks({"COIN_A": 5}, {"COIN_A": 5}) == {}
    assert lr.reconcile_breaks({"COIN_A": 5}, {"COIN_A": 3}) == {"COIN_A": (5, 3)}
    assert lr.reconcile_breaks({"COIN_A": 5}, {"COIN_A": 3}, tol_contracts=2) == {}
    assert lr.reconcile_breaks({}, {"COIN_B": 2}) == {"COIN_B": (0, 2)}


def test_breach_helpers_boundaries():
    assert lr.drawdown_breach(690, 1000, 0.30) is True
    assert lr.drawdown_breach(710, 1000, 0.30) is False
    assert lr.daily_loss_breach(940, 1000, 0.05) is True
    assert lr.daily_loss_breach(960, 1000, 0.05) is False
    assert lr.margin_over_ceiling(61, 39, 0.60) is True     # 61% used
    assert lr.margin_over_ceiling(59, 41, 0.60) is False


def test_stale_or_failed_snapshot_fails_closed_without_flatten():
    v = lr.evaluate(**_kw(read_ok=False))
    assert v.entries_allowed is False and v.flatten is False
    v = lr.evaluate(**_kw(snapshot_fresh=False))
    assert v.entries_allowed is False and v.flatten is False


def test_drawdown_breach_flattens():
    v = lr.evaluate(**_kw(equity=650.0, peak=1000.0))
    assert v.flatten is True and v.entries_allowed is False and "drawdown" in v.halt_reason


def test_daily_loss_breach_flattens():
    v = lr.evaluate(**_kw(equity=900.0, day_open_equity=1000.0, peak=1000.0))
    assert v.flatten is True and "daily loss" in v.halt_reason


def test_reconcile_break_halts_but_does_not_flatten():
    v = lr.evaluate(**_kw(expected={"COIN_A": 5}, actual={"COIN_A": 0}))
    assert v.entries_allowed is False and v.flatten is False and v.breaks == {"COIN_A": (5, 0)}


def test_margin_ceiling_blocks_new_risk_only():
    v = lr.evaluate(**_kw(margin_used=700.0, available_margin=300.0))  # 70% > 60%
    assert v.entries_allowed is False and v.flatten is False


def test_all_clear_allows_entries():
    v = lr.evaluate(**_kw())
    assert v.entries_allowed is True and v.flatten is False and v.halt_reason is None


def test_fat_finger_rejects_oversized_orders():
    assert lr.fat_finger_ok(4500, 1000, 5.0) is True     # 4.5x equity -> ok
    assert lr.fat_finger_ok(5500, 1000, 5.0) is False    # 5.5x equity -> reject
    assert lr.fat_finger_ok(100, 0, 5.0) is False        # no equity -> reject
