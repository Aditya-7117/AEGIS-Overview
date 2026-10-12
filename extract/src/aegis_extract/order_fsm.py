# Extracted from the private AEGIS repository: aegis/order_fsm.py at commit fcf0f74 (25 Aug 2026).
# Changes: dashes in docstrings replaced. Logic unchanged.
"""Order-reliability finite state machine. Pure; the router drives it with I/O.

Guarantees (owner's non-negotiables):
  1. Idempotent placement: client_oid is a deterministic hash of the intent, so a
     crash between "sent" and "recorded" can never double-place: on recovery we query
     the broker by client_oid and adopt whatever exists.
  2. No naked positions: an entry is not DONE until its protective stop is confirmed
     (needs_protection flag). decide() forces ATTACH_STOP first, always.
  3. No blind retries: an unacked/errored order is never resent; we QUERY_BROKER to
     learn the truth, then place fresh only if it truly never landed.
  4. No auto-retry on broker rejection: REJECTED escalates (margin/validation issue).
  5. Loud on bugs: illegal state transitions raise, they don't silently pass.

Persist ManagedOrder to the journal BEFORE the network call (write-ahead), so the
truth is always recoverable after any crash.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum


class OrderState(str, Enum):
    PENDING_NEW = "PENDING_NEW"     # intent journaled, not yet sent
    SENT = "SENT"                   # API call made, awaiting ack
    CONFIRMED = "CONFIRMED"         # broker acked with order_id
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    FAILED = "FAILED"              # network/transport error before ack (unknown outcome)
    REJECTED = "REJECTED"          # broker explicitly rejected (known outcome)
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class RouterAction(str, Enum):
    PLACE = "PLACE"
    WAIT = "WAIT"
    QUERY_BROKER = "QUERY_BROKER"
    ATTACH_STOP = "ATTACH_STOP"
    ESCALATE = "ESCALATE"
    DONE = "DONE"


@dataclass(frozen=True)
class ManagedOrder:
    client_oid: str
    symbol: str
    side: str                       # "buy" | "sell"
    qty: float
    kind: str                       # "entry" | "exit" | "protect" | "cancel"
    state: OrderState
    needs_protection: bool = False  # entries: True until bracket stop confirmed
    order_id: str | None = None
    attempts: int = 0


def make_client_oid(bar_iso: str, sleeve: str, symbol: str, side: str, seq: int = 0) -> str:
    """Deterministic, broker-safe (<=36 ascii). Same intent -> same id -> idempotent."""
    raw = f"{bar_iso}|{sleeve}|{symbol}|{side}|{seq}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return f"aeg-{h}"               # 4 + 24 = 28 chars


# legal transitions: state -> {event -> new_state}
_TRANSITIONS: dict[OrderState, dict[str, OrderState]] = {
    OrderState.PENDING_NEW: {
        "SENT_OK": OrderState.SENT,
        "ERROR": OrderState.FAILED,
        "REJECT": OrderState.REJECTED,
    },
    OrderState.SENT: {
        "ACK": OrderState.CONFIRMED,
        "FILL": OrderState.FILLED,
        "ERROR": OrderState.FAILED,
        "REJECT": OrderState.REJECTED,
        "RECONCILE_FOUND": OrderState.CONFIRMED,
        "RECONCILE_NOT_FOUND": OrderState.PENDING_NEW,
    },
    OrderState.CONFIRMED: {
        "FILL": OrderState.FILLED,
        "PARTIAL": OrderState.PARTIALLY_FILLED,
        "CANCEL_REQUEST": OrderState.CANCEL_PENDING,
        "PROTECT_CONFIRMED": OrderState.CONFIRMED,
    },
    OrderState.PARTIALLY_FILLED: {
        "FILL": OrderState.FILLED,
        "PROTECT_CONFIRMED": OrderState.PARTIALLY_FILLED,
        "CANCEL_REQUEST": OrderState.CANCEL_PENDING,
    },
    OrderState.FILLED: {
        "PROTECT_CONFIRMED": OrderState.FILLED,
        "CLOSE": OrderState.CLOSED,
    },
    OrderState.FAILED: {
        "RECONCILE_FOUND": OrderState.CONFIRMED,
        "RECONCILE_NOT_FOUND": OrderState.PENDING_NEW,
    },
    OrderState.REJECTED: {},                     # terminal without human/escalation
    OrderState.CANCEL_PENDING: {
        "CANCEL_ACK": OrderState.CANCELLED,
        "FILL": OrderState.FILLED,               # raced a fill before cancel landed
    },
    OrderState.CANCELLED: {},
    OrderState.CLOSED: {},
}


def transition(order: ManagedOrder, event: str, order_id: str | None = None) -> ManagedOrder:
    """Apply an event. Raises ValueError on an illegal transition (loud bugs)."""
    legal = _TRANSITIONS.get(order.state, {})
    if event not in legal:
        raise ValueError(
            f"illegal transition: {order.state.value} --{event}--> ? "
            f"(client_oid={order.client_oid})"
        )
    new_state = legal[event]
    changes: dict = {"state": new_state}
    if order_id is not None:
        changes["order_id"] = order_id
    if event == "PROTECT_CONFIRMED":
        changes["needs_protection"] = False
    if event in ("FILL", "ACK", "RECONCILE_FOUND"):
        changes.setdefault("needs_protection", order.needs_protection)
    return replace(order, **changes)


def decide(order: ManagedOrder, seconds_since_send: float, timeout: float) -> RouterAction:
    """Given current state + how long we've waited, what should the router do next?

    Safety precedence: a filled-but-unprotected entry ALWAYS attaches its stop before
    anything else. Never assume an unacked order's fate: query the broker."""
    st = order.state

    # naked-risk guard wins over everything
    if order.needs_protection and st in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
        return RouterAction.ATTACH_STOP

    if st == OrderState.PENDING_NEW:
        return RouterAction.PLACE
    if st == OrderState.SENT:
        return RouterAction.QUERY_BROKER if seconds_since_send >= timeout else RouterAction.WAIT
    if st == OrderState.FAILED:
        return RouterAction.QUERY_BROKER          # unknown outcome -> ask, don't resend
    if st == OrderState.REJECTED:
        return RouterAction.ESCALATE              # known NO -> human/alert, no auto-retry
    if st in (OrderState.CONFIRMED, OrderState.FILLED, OrderState.CANCELLED, OrderState.CLOSED):
        return RouterAction.DONE
    return RouterAction.WAIT
