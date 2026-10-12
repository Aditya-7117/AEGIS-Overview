# Extracted from the private AEGIS repository: aegis/intervention.py at commit fcf0f74 (25 Aug 2026).
# Changes: an internal document reference removed; docstring wording and dashes in comments. Logic unchanged.
"""Manual-intervention classifier, pure. Distinguishes the owner's manual
force-exit (benign) from a system/broker failure (critical), so the system never
conflates the two.

Encoded assumption (owner's): the owner never OPENS positions manually; signals are
the system's monopoly. He only exits/reduces/cancels. Therefore a position moving
toward zero (no in-flight order of ours) is a manual exit; a position that grows,
flips, or appears is an anomaly worth alarming.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    INFO = "INFO"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class InterventionEvent:
    symbol: str
    kind: str          # MANUAL_EXIT | NAKED_RISK | UNEXPECTED_POSITION
    severity: Severity
    directive: str     # ADOPT_AND_COOLDOWN | REPROTECT | PROTECT_AND_REVIEW
    detail: str = ""


def _toward_zero(exp: float, act: float) -> bool:
    """True if `act` is same-sign-or-zero as `exp` and strictly smaller magnitude."""
    if exp == 0:
        return False
    if act == 0:
        return True
    same_sign = (exp > 0) == (act > 0)
    return same_sign and abs(act) < abs(exp)


def classify_interventions(
    expected: dict[str, float],
    actual: dict[str, float],
    expected_protected: dict[str, bool],
    actual_protected: dict[str, bool],
    in_flight: set[str],
) -> list[InterventionEvent]:
    """Compare our last-known book to broker ground-truth and tag every unexplained
    delta. Symbols with an in-flight router order are our own doing and skipped."""
    events: list[InterventionEvent] = []
    symbols = sorted(set(expected) | set(actual))

    for sym in symbols:
        if sym in in_flight:
            continue
        exp = expected.get(sym, 0.0)
        act = actual.get(sym, 0.0)

        if exp == act:
            # position unchanged, but did its protection disappear?
            if act != 0 and not actual_protected.get(sym, False):
                events.append(InterventionEvent(
                    sym, "NAKED_RISK", Severity.CRITICAL, "REPROTECT",
                    "protective stop missing on an open position"))
            continue

        if _toward_zero(exp, act):
            # owner booked out (full or partial): accept, don't fight, cooldown
            events.append(InterventionEvent(
                sym, "MANUAL_EXIT", Severity.INFO, "ADOPT_AND_COOLDOWN",
                f"position reduced {exp} -> {act} with no order of ours"))
        else:
            # grew, flipped, or appeared: violates 'owner only exits'
            events.append(InterventionEvent(
                sym, "UNEXPECTED_POSITION", Severity.CRITICAL, "PROTECT_AND_REVIEW",
                f"position moved {exp} -> {act} in a way the system did not initiate"))
    return events
