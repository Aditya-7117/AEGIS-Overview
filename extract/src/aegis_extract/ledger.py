# Extracted from the private AEGIS repository: aegis/ledger.py at commit fcf0f74 (25 Aug 2026).
# Changes: the exchange's name in account strings replaced by "venue"; the exchange-specific post_live_fills() omitted; the project's logging helper replaced by the standard library logger plus a copy of its kv() formatter; an internal path removed and dashes replaced in docstrings and one log message. Logic unchanged.
"""Financial audit ledger (LIVE-14): dual-entry, append-only, hash-chained accounting,
built to the project's ledger specification (rules LGR-1 to LGR-15). Every unit of value
has a from-account and a to-account, history is immutable, and the books prove themselves.

Sign convention (standard double-entry: matches beancount/hledger/ledger-cli, not a local
invention): debit-normal accounts (assets:, expenses:, positions:) increase with a POSITIVE
delta; credit-normal accounts (income:, liabilities:, equity:, suspense:) increase (more
income/liability/equity/suspense) with a NEGATIVE delta. Every entry's legs must sum to
EXACTLY zero per unit (LGR-1). This is the accounting identity, not a modeling choice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

log = logging.getLogger("aegis.ledger")


def kv(**fields) -> str:
    """Render structured key=value tail for greppable log lines."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


GENESIS_HASH = "0" * 64


class LedgerError(RuntimeError):
    """An entry violated a core invariant (LGR-1 unbalanced, LGR-7 non-decimal amount).
    Raised at WRITE time, never silently logged for later cleanup (per spec, LGR-1)."""


@dataclass(frozen=True)
class Leg:
    account: str
    delta: Decimal
    unit: str = "USD"
    qty: Decimal | None = None
    qty_unit: str | None = None

    def to_json(self) -> dict:
        d = {"account": self.account, "delta": str(self.delta), "unit": self.unit}
        if self.qty is not None:
            d["qty"], d["qty_unit"] = str(self.qty), self.qty_unit
        return d


def _dec(x) -> Decimal:
    """LGR-7: float is forbidden end-to-end, including serialization. Convert via str()
    so an accidental float argument doesn't silently lose precision through Decimal(float)
    binary representation (Decimal(0.1) != Decimal('0.1'))."""
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except InvalidOperation as e:
        raise LedgerError(f"non-decimal amount: {x!r}") from e


def leg(account: str, delta, unit: str = "USD", qty=None, qty_unit: str | None = None) -> Leg:
    return Leg(account, _dec(delta), unit, _dec(qty) if qty is not None else None, qty_unit)


def _validate_balance(legs: list[Leg]) -> None:
    """LGR-1: legs must sum to exactly zero, per unit."""
    if len(legs) < 2:
        raise LedgerError(f"entry must have >=2 legs, got {len(legs)}")
    totals: dict[str, Decimal] = {}
    for lg in legs:
        totals[lg.unit] = totals.get(lg.unit, Decimal("0")) + lg.delta
    bad = {u: str(t) for u, t in totals.items() if t != 0}
    if bad:
        raise LedgerError(f"unbalanced entry: {bad} (legs must sum to exactly zero per unit)")


def _entry_hash(entry_id, mode, ts_event, ts_recorded, source_system, source_event_id,
                kind, legs_json, hash_prev) -> str:
    payload = "|".join([entry_id, mode, ts_event, ts_recorded, source_system,
                        source_event_id, kind, legs_json, hash_prev])
    return hashlib.sha256(payload.encode()).hexdigest()


def post_entry(store, *, mode: str, ts_event: str, source_system: str, source_event_id: str,
               kind: str, legs: list[Leg], memo: str = "",
               reverses: str | None = None) -> str | None:
    """Append one balanced, hash-chained entry (LGR-1/2/3/4/14/15).

    Returns the new entry_id, or None if source_event_id was already ingested for this
    (mode, source_system, kind): idempotent re-ingestion is a no-op (LGR-4), not an error.
    Raises LedgerError if the legs don't balance: crash-level, at write time (LGR-1).
    """
    _validate_balance(legs)
    if store.ledger_find_by_source(mode, source_system, source_event_id, kind) is not None:
        log.debug("ledger dedup no-op %s", kv(mode=mode, event=source_event_id, kind=kind))
        return None

    entry_id = str(uuid.uuid4())
    ts_recorded = datetime.now(timezone.utc).isoformat()
    prev_hash = store.ledger_last_hash(mode) or GENESIS_HASH
    legs_json = json.dumps([l.to_json() for l in legs], sort_keys=True)
    self_hash = _entry_hash(entry_id, mode, ts_event, ts_recorded, source_system,
                            source_event_id, kind, legs_json, prev_hash)

    store.ledger_append(entry_id=entry_id, mode=mode, ts_event=ts_event,
                        ts_recorded=ts_recorded, source_system=source_system,
                        source_event_id=source_event_id, kind=kind, legs_json=legs_json,
                        memo=memo, hash_prev=prev_hash, hash_self=self_hash,
                        reverses=reverses)
    log.info("ledger entry %s", kv(mode=mode, kind=kind, entry=entry_id[:8], legs=len(legs)))
    return entry_id


def reverse_entry(store, *, mode: str, entry_id: str, memo: str = "") -> str:
    """LGR-2 correction mechanism: post a reversal with negated legs, referencing the
    original. The original is NEVER edited or deleted: history shows the mistake AND
    the fix, in that order."""
    row = store.ledger_get(mode, entry_id)
    if row is None:
        raise LedgerError(f"cannot reverse unknown entry {entry_id}")
    orig = [leg(l["account"], Decimal(l["delta"]), l["unit"],
               Decimal(l["qty"]) if l.get("qty") is not None else None, l.get("qty_unit"))
            for l in json.loads(row["legs_json"])]
    negated = [leg(l.account, -l.delta, l.unit,
                   (-l.qty if l.qty is not None else None), l.qty_unit) for l in orig]
    new_id = post_entry(store, mode=mode, ts_event=datetime.now(timezone.utc).isoformat(),
                        source_system="aegis", source_event_id=f"reversal:{entry_id}",
                        kind="reversal", legs=negated,
                        memo=memo or f"reversal of {entry_id}", reverses=entry_id)
    if new_id is None:
        raise LedgerError(f"entry {entry_id} was already reversed")
    return new_id


def trial_balance(store, mode: str) -> dict[str, Decimal]:
    """LGR-10: re-sum every entry's legs by unit. Each individual entry already balances
    by construction (LGR-1), so a nonzero total here means the stored data was corrupted
    or tampered with outside this API. Failure = halt and investigate, never 'adjust'."""
    totals: dict[str, Decimal] = {}
    for row in store.ledger_all(mode):
        for l in json.loads(row["legs_json"]):
            totals[l["unit"]] = totals.get(l["unit"], Decimal("0")) + Decimal(l["delta"])
    return totals


def verify_chain(store, mode: str) -> list[dict]:
    """LGR-3: recompute the hash chain over every entry in append order; report any break
    or tampered entry. Empty list = chain intact. Run as a scheduled job and before any
    audit (LGR-13)."""
    breaks: list[dict] = []
    prev = GENESIS_HASH
    for row in store.ledger_all(mode):
        if row["hash_prev"] != prev:
            breaks.append({"entry_id": row["entry_id"], "expected_prev": prev,
                          "found_prev": row["hash_prev"]})
        recomputed = _entry_hash(row["entry_id"], mode, row["ts_event"], row["ts_recorded"],
                                 row["source_system"], row["source_event_id"], row["kind"],
                                 row["legs_json"], row["hash_prev"])
        if recomputed != row["hash_self"]:
            breaks.append({"entry_id": row["entry_id"], "tampered": True})
        prev = row["hash_self"]
    return breaks


# ---- economic-event posting helpers (the call sites trading code actually uses) ----

def post_fill_entry(store, *, mode: str, ts_event: str, event_id: str, symbol: str,
                    cost: float, realized_pnl: float = 0.0,
                    alert_fn: Callable[[str], None] | None = None) -> str | None:
    """Fee + (if closing) realized-PnL legs for one fill (LGR-1/5). Unclassifiable
    economics (e.g. a non-Decimal amount slipping through) route to suspense: with an
    alert, never dropped (LGR-6)."""
    try:
        cost_d, pnl_d = _dec(cost), _dec(realized_pnl)
    except LedgerError:
        log.exception("fill economics unparseable, routing to suspense")
        if alert_fn:
            alert_fn(f"LEDGER_SUSPENSE {symbol}: unparseable fill economics cost={cost!r} "
                     f"pnl={realized_pnl!r}")
        return post_entry(store, mode=mode, ts_event=ts_event, source_system="venue",
                          source_event_id=event_id, kind="adjustment",
                          legs=[leg(f"suspense:venue:{symbol}", "0"),
                                leg("assets:exchange:venue:USD", "0")],
                          memo="LGR-6: unparseable fill economics")
    legs: list[Leg] = []
    if cost_d != 0:
        legs += [leg(f"expenses:fees:venue:{symbol}", cost_d),
                leg("assets:exchange:venue:USD", -cost_d)]
    if pnl_d != 0:
        legs += [leg(f"income:realized:venue:{symbol}", -pnl_d),
                leg("assets:exchange:venue:USD", pnl_d)]
    if not legs:
        return None   # a zero-cost, zero-pnl fill has nothing to book
    return post_entry(store, mode=mode, ts_event=ts_event, source_system="venue",
                      source_event_id=event_id, kind="fill", legs=legs)


def post_funding_entry(store, *, mode: str, ts_event: str, event_id: str, symbol: str,
                       flow: float) -> str | None:
    """Funding cashflow leg (LGR-1/5). flow>0 = funding received (income); flow<0 = funding
    paid (expense). One formula covers both signs (see module docstring convention)."""
    flow_d = _dec(flow)
    if flow_d == 0:
        return None
    account = (f"income:funding:venue:{symbol}" if flow_d > 0
              else f"expenses:funding:venue:{symbol}")
    return post_entry(store, mode=mode, ts_event=ts_event, source_system="venue",
                      source_event_id=event_id, kind="funding",
                      legs=[leg(account, -flow_d), leg("assets:exchange:venue:USD", flow_d)])
