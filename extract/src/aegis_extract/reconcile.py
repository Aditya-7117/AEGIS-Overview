# Extracted from the private AEGIS repository: aegis/reconcile.py at commit fcf0f74 (25 Aug 2026).
# Changes: dashes in the docstring replaced; the default rebalance band (0.30) is an illustrative value, not the private default. Logic unchanged.
"""Reconciler: the recovery brain. Pure function; no I/O.

On every startup and every bar, the engine computes DESIRED positions fresh from
current market data, reads GROUND-TRUTH positions/orders from the broker, and calls
reconcile() to get the minimal safe action set. Because desired is always fresh,
stale intents are never replayed: the reconciler only compares held-vs-want.

Safety ordering (deterministic): PROTECT unprotected risk first, CANCEL orphan
orders next, THEN bring positions to desired. This guarantees that no code path can
leave a naked position or act on a stray order before risk is contained.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float                 # signed: +long / -short, in contracts
    protected: bool = True     # is a broker-side protective stop attached?


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    symbol: str
    client_oid: str


@dataclass(frozen=True)
class Action:
    kind: str                  # PROTECT | EXIT | ENTER | HOLD | ADJUST | REVERSE | CANCEL_ORPHAN
    symbol: str
    qty: float = 0.0           # target qty (ENTER/ADJUST/REVERSE) or held qty (EXIT/PROTECT)
    reason: str = ""


def _same_sign(a: float, b: float) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)


def reconcile(
    positions: list[Position],
    desired: dict[str, float],
    open_orders: list[OpenOrder],
    known_client_oids: set[str],
    band: float = 0.30,
) -> list[Action]:
    actions: list[Action] = []
    pos_by_sym = {p.symbol: p for p in positions}

    # 1) SAFETY FIRST: protect any held position lacking a broker-side stop.
    for p in positions:
        if p.qty != 0 and not p.protected:
            actions.append(Action("PROTECT", p.symbol, qty=p.qty,
                                   reason="unprotected position found on reconcile"))

    # 2) Cancel orphan orders (open at broker, no matching known intent).
    for o in open_orders:
        if o.client_oid not in known_client_oids:
            actions.append(Action("CANCEL_ORPHAN", o.symbol, reason=f"orphan order {o.order_id}"))

    # 3) Bring positions to desired (fresh target vs ground-truth held).
    symbols = sorted(set(pos_by_sym) | set(desired))
    for sym in symbols:
        held = pos_by_sym[sym].qty if sym in pos_by_sym else 0.0
        want = desired.get(sym, 0.0)

        if held == 0 and want != 0:
            actions.append(Action("ENTER", sym, qty=want, reason="new desired position"))
        elif held != 0 and want == 0:
            actions.append(Action("EXIT", sym, qty=held, reason="no longer desired"))
        elif held != 0 and want != 0:
            if not _same_sign(held, want):
                actions.append(Action("REVERSE", sym, qty=want, reason="direction flip"))
            else:
                drift = abs(want - held)
                ref = max(abs(want), 1e-9)
                if drift / ref > band:
                    actions.append(Action("ADJUST", sym, qty=want, reason="size drift > band"))
                else:
                    actions.append(Action("HOLD", sym, qty=held, reason="within band"))
    return actions
