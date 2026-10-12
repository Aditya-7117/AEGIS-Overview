# Extracted from the private AEGIS repository: tests/test_order_fsm.py at commit fcf0f74 (25 Aug 2026).
# Changes: import path; instrument and sleeve names replaced by placeholders; an internal milestone reference removed; dashes in docstrings replaced.
"""Order-reliability FSM: encodes the owner's non-negotiables:
  * an order never silently fails to place
  * the initial stop-loss is never left unplaced (no naked position, ever)
  * nothing is cancelled/retried 'randomly': every action follows from state + broker truth
  * idempotent: a crash mid-send can never double-place (deterministic client_oid)

The FSM is pure (no network). The router drives it with real broker calls.
"""
from __future__ import annotations

import pytest

from aegis_extract.order_fsm import (
    ManagedOrder, OrderState, make_client_oid, transition, decide, RouterAction,
)


# ---- idempotency ----
def test_client_oid_is_deterministic():
    a = make_client_oid("2026-07-04T08:00:00Z", "sleeve_a", "COIN_A", "buy", 0)
    b = make_client_oid("2026-07-04T08:00:00Z", "sleeve_a", "COIN_A", "buy", 0)
    assert a == b


def test_client_oid_differs_by_intent():
    base = ("2026-07-04T08:00:00Z", "sleeve_a", "COIN_A", "buy", 0)
    assert make_client_oid(*base) != make_client_oid("2026-07-04T12:00:00Z", *base[1:])
    assert make_client_oid(*base) != make_client_oid(*base[:3], "sell", 0)


def test_client_oid_within_broker_length_limit():
    oid = make_client_oid("2026-07-04T08:00:00Z", "sleeve_a", "COIN_A", "buy", 0)
    assert len(oid) <= 36 and oid.isascii()


# ---- naked-position prevention (THE critical invariant) ----
def test_filled_entry_without_stop_demands_attach_stop():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.FILLED, needs_protection=True)
    assert decide(o, seconds_since_send=1, timeout=10) == RouterAction.ATTACH_STOP


def test_filled_entry_with_confirmed_stop_is_done():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.FILLED, needs_protection=False)
    assert decide(o, seconds_since_send=1, timeout=10) == RouterAction.DONE


# ---- never assume, never blind-retry ----
def test_sent_but_unacked_past_timeout_queries_broker():
    """No ack in time -> we do NOT assume failure and we do NOT blindly resend
    (would risk a double). We ask the broker what actually happened."""
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.SENT, needs_protection=True)
    assert decide(o, seconds_since_send=15, timeout=10) == RouterAction.QUERY_BROKER


def test_sent_within_timeout_waits():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.SENT, needs_protection=True)
    assert decide(o, seconds_since_send=2, timeout=10) == RouterAction.WAIT


def test_network_error_queries_broker_not_resend():
    """A network error before ack might still have landed the order -> reconcile,
    don't resend blindly."""
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.FAILED, needs_protection=True)
    assert decide(o, seconds_since_send=1, timeout=10) == RouterAction.QUERY_BROKER


def test_broker_rejection_escalates_no_autoretry():
    """Broker said NO (margin/validation). Auto-retrying is dangerous -> escalate."""
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.REJECTED, needs_protection=True)
    assert decide(o, seconds_since_send=1, timeout=10) == RouterAction.ESCALATE


def test_pending_new_places_order():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.PENDING_NEW, needs_protection=True)
    assert decide(o, seconds_since_send=0, timeout=10) == RouterAction.PLACE


# ---- transition legality (bugs must be loud) ----
def test_legal_transition_pending_to_sent():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.PENDING_NEW, needs_protection=True)
    o2 = transition(o, "SENT_OK")
    assert o2.state == OrderState.SENT


def test_illegal_transition_raises():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.FILLED, needs_protection=False)
    with pytest.raises(ValueError):
        transition(o, "SENT_OK")   # can't send an already-filled order


def test_ack_carries_order_id():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.SENT, needs_protection=True)
    o2 = transition(o, "ACK", order_id="OID-99")
    assert o2.state == OrderState.CONFIRMED and o2.order_id == "OID-99"


def test_protect_confirmed_clears_needs_protection():
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.FILLED, needs_protection=True)
    o2 = transition(o, "PROTECT_CONFIRMED")
    assert o2.needs_protection is False


def test_reconcile_found_marks_confirmed():
    """QUERY_BROKER discovered the order did land -> adopt it, don't resend."""
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.SENT, needs_protection=True)
    o2 = transition(o, "RECONCILE_FOUND", order_id="OID-7")
    assert o2.state == OrderState.CONFIRMED and o2.order_id == "OID-7"


def test_reconcile_not_found_returns_to_pending():
    """QUERY_BROKER confirmed it never landed -> safe to place fresh."""
    o = ManagedOrder(client_oid="c1", symbol="COIN_A", side="buy", qty=3,
                     kind="entry", state=OrderState.FAILED, needs_protection=True)
    o2 = transition(o, "RECONCILE_NOT_FOUND")
    assert o2.state == OrderState.PENDING_NEW
