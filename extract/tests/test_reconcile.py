# Extracted from the private AEGIS repository: tests/test_reconcile.py at commit fcf0f74 (25 Aug 2026).
# Changes: import path; instrument names replaced by placeholders; the band in two tests set to the extract's illustrative value; a dash replaced.
"""Reconciler: given broker ground-truth positions/orders and a FRESHLY computed
desired state, emit the minimal safe action set. This is the recovery brain.

Invariants under test:
- Any held position missing protection gets PROTECT emitted BEFORE anything else
  touches it (never leave naked risk, even for one action).
- Desired is always fresh (computed from current market); the reconciler never
  replays a stale intent: it only compares held-vs-want.
- Orphan orders (no matching known client_oid) are cancelled.
"""
from __future__ import annotations

from aegis_extract.reconcile import Position, OpenOrder, Action, reconcile


def kinds(actions):
    return [(a.kind, a.symbol) for a in actions]


def test_hold_position_we_still_want():
    pos = [Position("COIN_A", qty=10, protected=True)]
    desired = {"COIN_A": 10}
    acts = reconcile(pos, desired, open_orders=[], known_client_oids=set())
    assert kinds(acts) == [("HOLD", "COIN_A")]


def test_exit_position_we_no_longer_want():
    pos = [Position("COIN_A", qty=10, protected=True)]
    acts = reconcile(pos, desired={}, open_orders=[], known_client_oids=set())
    assert kinds(acts) == [("EXIT", "COIN_A")]


def test_unexpected_unprotected_position_is_protected_first_then_exited():
    """The owner's crash scenario: a position exists that we didn't journal and has
    no stop. Make it SAFE first, then bring it to desired (flat)."""
    pos = [Position("COIN_C", qty=5, protected=False)]
    acts = reconcile(pos, desired={}, open_orders=[], known_client_oids=set())
    assert kinds(acts) == [("PROTECT", "COIN_C"), ("EXIT", "COIN_C")]


def test_unprotected_position_we_want_is_protected_then_held():
    pos = [Position("COIN_B", qty=3, protected=False)]
    acts = reconcile(pos, desired={"COIN_B": 3}, open_orders=[], known_client_oids=set())
    assert kinds(acts) == [("PROTECT", "COIN_B"), ("HOLD", "COIN_B")]


def test_enter_new_desired_position():
    acts = reconcile([], desired={"COIN_B": 3}, open_orders=[], known_client_oids=set())
    assert len(acts) == 1 and acts[0].kind == "ENTER"
    assert acts[0].symbol == "COIN_B" and acts[0].qty == 3


def test_reverse_on_sign_flip():
    pos = [Position("COIN_A", qty=10, protected=True)]
    acts = reconcile(pos, desired={"COIN_A": -8}, open_orders=[], known_client_oids=set())
    assert acts[0].kind == "REVERSE" and acts[0].symbol == "COIN_A"
    assert acts[0].qty == -8   # target


def test_adjust_when_outside_band():
    pos = [Position("COIN_A", qty=10, protected=True)]
    acts = reconcile(pos, desired={"COIN_A": 20}, open_orders=[], known_client_oids=set(),
                     band=0.30)
    assert acts[0].kind == "ADJUST" and acts[0].qty == 20


def test_hold_within_band_no_adjust():
    pos = [Position("COIN_A", qty=10, protected=True)]
    acts = reconcile(pos, desired={"COIN_A": 10.5}, open_orders=[], known_client_oids=set(),
                     band=0.30)
    assert kinds(acts) == [("HOLD", "COIN_A")]


def test_orphan_order_cancelled():
    order = OpenOrder(order_id="X1", symbol="COIN_D", client_oid="unknown-oid")
    acts = reconcile([], desired={}, open_orders=[order], known_client_oids={"known-oid"})
    assert kinds(acts) == [("CANCEL_ORPHAN", "COIN_D")]


def test_known_order_not_cancelled():
    order = OpenOrder(order_id="X1", symbol="COIN_D", client_oid="known-oid")
    acts = reconcile([], desired={}, open_orders=[order], known_client_oids={"known-oid"})
    assert acts == []


def test_flat_and_want_flat_is_noop():
    assert reconcile([], desired={}, open_orders=[], known_client_oids=set()) == []


def test_protection_precedes_orphan_cancel_precedes_position_actions():
    """Deterministic safety ordering: protect held risk, cancel stray orders, then trade."""
    pos = [Position("COIN_C", qty=5, protected=False)]
    order = OpenOrder(order_id="X1", symbol="COIN_D", client_oid="stray")
    acts = reconcile(pos, desired={"COIN_B": 2}, open_orders=[order],
                     known_client_oids=set())
    order_of_kinds = [a.kind for a in acts]
    assert order_of_kinds.index("PROTECT") < order_of_kinds.index("CANCEL_ORPHAN")
    assert order_of_kinds.index("CANCEL_ORPHAN") < order_of_kinds.index("ENTER")
