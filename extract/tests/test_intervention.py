# Extracted from the private AEGIS repository: tests/test_intervention.py at commit fcf0f74 (25 Aug 2026).
# Changes: import path; instrument names replaced by placeholders; docstring wording and dashes.
"""Intervention classifier: the bullet-proofing. Distinguishes the owner's
manual force-exit (benign, absorb it) from a genuine system/broker failure (critical,
alarm). Pure function over last-known book vs broker ground-truth.

Assumption encoded (owner's): signals are the system's monopoly. The owner NEVER opens
a position manually; he only ever exits/reduces/cancels. So a shrinking position is a
manual exit; a growing/flipped/new one is an anomaly worth alarming.
"""
from __future__ import annotations

from aegis_extract.intervention import classify_interventions, Severity


def find(events, kind):
    return [e for e in events if e.kind == kind]


def test_no_change_no_events():
    events = classify_interventions(
        expected={"COIN_A": 10}, actual={"COIN_A": 10},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": True},
        in_flight=set(),
    )
    assert events == []


def test_manual_full_exit_is_benign_and_books_out():
    """Owner sold-to-close the whole long via the app. Accept it, don't re-enter."""
    events = classify_interventions(
        expected={"COIN_A": 10}, actual={"COIN_A": 0},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": False},
        in_flight=set(),
    )
    e = find(events, "MANUAL_EXIT")
    assert len(e) == 1
    assert e[0].severity == Severity.INFO
    assert e[0].directive == "ADOPT_AND_COOLDOWN"
    assert e[0].symbol == "COIN_A"


def test_manual_partial_exit_is_benign():
    events = classify_interventions(
        expected={"COIN_B": 20}, actual={"COIN_B": 8},
        expected_protected={"COIN_B": True}, actual_protected={"COIN_B": True},
        in_flight=set(),
    )
    e = find(events, "MANUAL_EXIT")
    assert len(e) == 1 and e[0].severity == Severity.INFO


def test_manual_exit_of_short_is_benign():
    """Short covered by owner buying-to-close -> position toward zero from negative."""
    events = classify_interventions(
        expected={"COIN_A": -10}, actual={"COIN_A": 0},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": False},
        in_flight=set(),
    )
    assert find(events, "MANUAL_EXIT") and events[0].severity == Severity.INFO


def test_vanished_stop_on_open_position_is_naked_risk():
    """Owner cancelled the protective stop but LEFT the position -> critical."""
    events = classify_interventions(
        expected={"COIN_A": 10}, actual={"COIN_A": 10},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": False},
        in_flight=set(),
    )
    e = find(events, "NAKED_RISK")
    assert len(e) == 1
    assert e[0].severity == Severity.CRITICAL
    assert e[0].directive == "REPROTECT"


def test_unexpected_grown_position_is_critical():
    """Position bigger than we hold -> violates 'user only exits' -> alarm."""
    events = classify_interventions(
        expected={"COIN_A": 10}, actual={"COIN_A": 18},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": True},
        in_flight=set(),
    )
    e = find(events, "UNEXPECTED_POSITION")
    assert len(e) == 1 and e[0].severity == Severity.CRITICAL


def test_flipped_position_is_unexpected_critical():
    events = classify_interventions(
        expected={"COIN_A": 10}, actual={"COIN_A": -5},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": True},
        in_flight=set(),
    )
    assert find(events, "UNEXPECTED_POSITION")[0].severity == Severity.CRITICAL


def test_appeared_position_we_never_opened_is_unexpected():
    events = classify_interventions(
        expected={}, actual={"COIN_D": 5},
        expected_protected={}, actual_protected={"COIN_D": True},
        in_flight=set(),
    )
    assert find(events, "UNEXPECTED_POSITION")


def test_our_inflight_order_changes_are_not_flagged():
    """A symbol we are actively trading this cycle is our own doing, never flagged."""
    events = classify_interventions(
        expected={"COIN_A": 10}, actual={"COIN_A": 0},
        expected_protected={"COIN_A": True}, actual_protected={"COIN_A": False},
        in_flight={"COIN_A"},
    )
    assert events == []


def test_multiple_symbols_classified_independently():
    events = classify_interventions(
        expected={"COIN_A": 10, "COIN_B": 5},
        actual={"COIN_A": 0, "COIN_B": 5},
        expected_protected={"COIN_A": True, "COIN_B": False},
        actual_protected={"COIN_A": False, "COIN_B": False},
        in_flight=set(),
    )
    assert find(events, "MANUAL_EXIT")[0].symbol == "COIN_A"
    assert find(events, "NAKED_RISK")[0].symbol == "COIN_B"
