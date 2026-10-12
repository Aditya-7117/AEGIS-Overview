# Extracted from the private AEGIS repository: aegis/live_risk.py at commit fcf0f74 (25 Aug 2026).
# Changes: a configured limit removed from one docstring; dashes replaced in docstrings and halt messages. Logic unchanged.
"""Live risk controls: pure decision functions for the kill switch, guards, and
reconciliation (LIVE-6/10/11/13). Kept pure so the money-critical thresholds are
property-testable without a broker; the supervisor feeds them live broker snapshots.

Design stance (owner mandate): never hallucinate. Decisions run on the live broker
snapshot, never on assumed state; and never take a catastrophic action on uncertain data:
a failed/stale read HALTS new risk but does NOT flatten (only a CONFIRMED breach does).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskVerdict:
    entries_allowed: bool          # may we open NEW risk this bar?
    flatten: bool                  # must we flatten everything NOW (kill switch fired)?
    halt_reason: str | None        # human-readable reason, for alarm + audit
    breaks: dict                   # symbol -> (expected, actual) reconciliation divergences


def reconcile_breaks(expected: dict[str, int], actual: dict[str, int],
                     tol_contracts: int = 0) -> dict[str, tuple[int, int]]:
    """Positions where our ledger and the venue disagree beyond tolerance (QT-ORD-4).
    Venue is truth; any unexplained break is surfaced for halt + page."""
    out: dict[str, tuple[int, int]] = {}
    for s in set(expected) | set(actual):
        e, a = int(expected.get(s, 0)), int(actual.get(s, 0))
        if abs(e - a) > tol_contracts:
            out[s] = (e, a)
    return out


def drawdown_breach(equity: float, peak: float, max_dd: float) -> bool:
    return peak > 0 and equity < (1.0 - max_dd) * peak


def daily_loss_breach(equity: float, day_open_equity: float, max_daily: float) -> bool:
    return day_open_equity > 0 and equity < (1.0 - max_daily) * day_open_equity


def margin_over_ceiling(margin_used: float, available: float, ceiling: float) -> bool:
    total = margin_used + available
    return total > 0 and (margin_used / total) > ceiling


def fat_finger_ok(order_notional: float, equity: float, max_notional_frac: float) -> bool:
    """A single order's notional must not exceed max_notional_frac x equity: a last-line
    guard against a sizing bug or bad tick placing an absurd order (LIVE-12). Generous by
    default (well above the portfolio gross cap) so it only ever catches true faults."""
    if equity <= 0:
        return False
    return order_notional <= max_notional_frac * equity


def evaluate(*, read_ok: bool, snapshot_fresh: bool,
             expected: dict[str, int], actual: dict[str, int], tol_contracts: int,
             equity: float, peak: float, day_open_equity: float,
             max_dd: float, max_daily: float,
             margin_used: float, available_margin: float, margin_ceiling: float) -> RiskVerdict:
    """Single decision point for a live bar. Order matters: an unusable snapshot fails
    CLOSED (no new risk, no flatten); a confirmed loss breach flattens; a reconciliation
    break or margin ceiling blocks new risk without flattening."""
    if not read_ok or not snapshot_fresh:
        return RiskVerdict(False, False,
                           "broker snapshot unavailable/stale: new risk suspended", {})

    if drawdown_breach(equity, peak, max_dd):
        return RiskVerdict(False, True,
                           f"max drawdown breached: equity {equity:.2f} < {(1-max_dd)*peak:.2f} "
                           f"({max_dd:.0%} of peak {peak:.2f})", {})
    if daily_loss_breach(equity, day_open_equity, max_daily):
        return RiskVerdict(False, True,
                           f"daily loss limit breached: equity {equity:.2f} < "
                           f"{(1-max_daily)*day_open_equity:.2f} ({max_daily:.0%} of day open)", {})

    breaks = reconcile_breaks(expected, actual, tol_contracts)
    if breaks:
        return RiskVerdict(False, False,
                           f"reconciliation break vs venue: {breaks}; new risk halted, page", breaks)

    if margin_over_ceiling(margin_used, available_margin, margin_ceiling):
        return RiskVerdict(False, False,
                           f"margin utilization over {margin_ceiling:.0%} ceiling: new risk blocked", {})

    return RiskVerdict(True, False, None, {})
