# Extracted from the private AEGIS repository: tests/test_ledger.py at commit fcf0f74 (25 Aug 2026).
# Changes: import paths; exchange and instrument names replaced by placeholders; the two tests of the omitted post_live_fills() removed; a dash replaced.
"""Financial audit ledger (LIVE-14, LGR-1..11): dual-entry, append-only, hash-chained."""
from __future__ import annotations

from decimal import Decimal

import pytest

from aegis_extract.ledger import (LedgerError, Leg, leg, post_entry, reverse_entry, trial_balance,
                          verify_chain, post_fill_entry, post_funding_entry)
from aegis_extract.state_store import StateStore


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "aegis.db")


# ---- LGR-1: dual-entry, balanced ----
def test_unbalanced_entry_rejected_at_write_time(store):
    with pytest.raises(LedgerError):
        post_entry(store, mode="paper", ts_event="t", source_system="test",
                  source_event_id="e1", kind="fill",
                  legs=[leg("assets:exchange:venue:USD", "-10"),
                        leg("expenses:fees:venue:COIN_A", "9")])   # -10+9 = -1, not zero


def test_single_leg_entry_rejected(store):
    with pytest.raises(LedgerError):
        post_entry(store, mode="paper", ts_event="t", source_system="test",
                  source_event_id="e1", kind="fill",
                  legs=[leg("assets:exchange:venue:USD", "0")])


def test_spec_example_balances_and_posts(store):
    """The worked example from the ledger specification."""
    eid = post_entry(store, mode="paper", ts_event="t", source_system="venue",
                     source_event_id="fill-1", kind="fill", legs=[
        leg("assets:exchange:venue:USDT", "-1000.00"),
        leg("positions:COIN_A:cost", "999.00", qty="0.01523000", qty_unit="COIN_A"),
        leg("expenses:fees:venue:taker", "1.00"),
    ])
    assert eid is not None
    row = store.ledger_get("paper", eid)
    assert row["hash_prev"] == "0" * 64          # genesis for a fresh journal


# ---- LGR-4: idempotent ingestion ----
def test_reingesting_same_source_event_is_a_noop(store):
    legs = [leg("expenses:fees:venue:COIN_A", "1"), leg("assets:exchange:venue:USD", "-1")]
    first = post_entry(store, mode="paper", ts_event="t", source_system="venue",
                       source_event_id="fill-9", kind="fill", legs=legs)
    second = post_entry(store, mode="paper", ts_event="t", source_system="venue",
                        source_event_id="fill-9", kind="fill", legs=legs)
    assert first is not None and second is None
    assert len(store.ledger_all("paper")) == 1


# ---- LGR-3: hash chain ----
def test_hash_chain_links_sequential_entries(store):
    ids = [post_entry(store, mode="paper", ts_event="t", source_system="test",
                      source_event_id=f"e{i}", kind="fill",
                      legs=[leg("expenses:fees:venue:COIN_A", "1"),
                            leg("assets:exchange:venue:USD", "-1")]) for i in range(3)]
    rows = store.ledger_all("paper")
    assert rows[0]["hash_prev"] == "0" * 64
    assert rows[1]["hash_prev"] == rows[0]["hash_self"]
    assert rows[2]["hash_prev"] == rows[1]["hash_self"]
    assert verify_chain(store, "paper") == []


def test_verify_chain_detects_tampering(store):
    post_entry(store, mode="paper", ts_event="t", source_system="test", source_event_id="e1",
              kind="fill", legs=[leg("expenses:fees:venue:COIN_A", "1"),
                                 leg("assets:exchange:venue:USD", "-1")])
    # simulate tampering: directly mutate the stored row outside the ledger API
    store._con.execute("UPDATE ledger_entries SET legs_json='[]' WHERE source_event_id='e1'")
    store._con.commit()
    breaks = verify_chain(store, "paper")
    assert any(b.get("tampered") for b in breaks)


def test_two_modes_have_independent_chains(store):
    post_entry(store, mode="paper", ts_event="t", source_system="test", source_event_id="e1",
              kind="fill", legs=[leg("expenses:fees:venue:COIN_A", "1"),
                                 leg("assets:exchange:venue:USD", "-1")])
    post_entry(store, mode="live", ts_event="t", source_system="test", source_event_id="e1",
              kind="fill", legs=[leg("expenses:fees:venue:COIN_A", "2"),
                                 leg("assets:exchange:venue:USD", "-2")])
    assert store.ledger_all("live")[0]["hash_prev"] == "0" * 64   # own genesis, not paper's


# ---- LGR-2: append-only correction via reversal ----
def test_reverse_entry_negates_without_mutating_original(store):
    eid = post_entry(store, mode="paper", ts_event="t", source_system="test",
                     source_event_id="e1", kind="fill",
                     legs=[leg("expenses:fees:venue:COIN_A", "5"),
                           leg("assets:exchange:venue:USD", "-5")])
    rid = reverse_entry(store, mode="paper", entry_id=eid, memo="oops")
    original = store.ledger_get("paper", eid)
    reversal = store.ledger_get("paper", rid)
    assert original is not None and reversal["reverses"] == eid
    assert trial_balance(store, "paper")["USD"] == Decimal("0")
    assert len(store.ledger_all("paper")) == 2      # both visible: mistake AND fix


def test_reversing_unknown_entry_raises(store):
    with pytest.raises(LedgerError):
        reverse_entry(store, mode="paper", entry_id="not-a-real-id")


# ---- LGR-10: trial balance ----
def test_trial_balance_zero_after_many_mixed_entries(store):
    post_fill_entry(store, mode="paper", ts_event="t", event_id="f1", symbol="COIN_A",
                    cost=1.23, realized_pnl=45.67)
    post_fill_entry(store, mode="paper", ts_event="t", event_id="f2", symbol="COIN_B",
                    cost=0.5, realized_pnl=-12.0)
    post_funding_entry(store, mode="paper", ts_event="t", event_id="fund1", symbol="COIN_A",
                       flow=3.3)
    post_funding_entry(store, mode="paper", ts_event="t", event_id="fund2", symbol="COIN_A",
                       flow=-1.1)
    assert trial_balance(store, "paper")["USD"] == Decimal("0")


# ---- LGR-7: Decimal precision, no float ----
def test_decimal_precision_survives_the_classic_float_trap(store):
    """0.1 + 0.2 != 0.3 in binary float; Decimal-via-string must not lose this."""
    post_entry(store, mode="paper", ts_event="t", source_system="test", source_event_id="e1",
              kind="fill", legs=[leg("expenses:fees:venue:COIN_A", "0.1"),
                                 leg("expenses:fees:venue:COIN_A", "0.2"),
                                 leg("assets:exchange:venue:USD", "-0.3")])
    assert trial_balance(store, "paper")["USD"] == Decimal("0")


def test_non_decimal_amount_raises():
    with pytest.raises(LedgerError):
        leg("assets:exchange:venue:USD", "not-a-number")


# ---- fill/funding economic-event helpers ----
def test_post_fill_entry_fee_only_two_legs(store):
    eid = post_fill_entry(store, mode="paper", ts_event="t", event_id="f1", symbol="COIN_A",
                          cost=2.5)
    row = store.ledger_get("paper", eid)
    import json
    legs = json.loads(row["legs_json"])
    assert len(legs) == 2
    assert {l["account"] for l in legs} == {"expenses:fees:venue:COIN_A",
                                            "assets:exchange:venue:USD"}


def test_post_fill_entry_with_pnl_four_legs_balance(store):
    eid = post_fill_entry(store, mode="paper", ts_event="t", event_id="f1", symbol="COIN_A",
                          cost=1.0, realized_pnl=50.0)
    row = store.ledger_get("paper", eid)
    import json
    legs = [Decimal(l["delta"]) for l in json.loads(row["legs_json"])]
    assert sum(legs) == Decimal("0")
    assert len(legs) == 4


def test_post_fill_entry_zero_economics_posts_nothing(store):
    assert post_fill_entry(store, mode="paper", ts_event="t", event_id="f1",
                           symbol="COIN_A", cost=0.0, realized_pnl=0.0) is None
    assert store.ledger_all("paper") == []


def test_funding_income_and_expense_signs(store):
    income_id = post_funding_entry(store, mode="paper", ts_event="t", event_id="f1",
                                   symbol="COIN_A", flow=10.0)
    expense_id = post_funding_entry(store, mode="paper", ts_event="t", event_id="f2",
                                    symbol="COIN_A", flow=-4.0)
    import json
    income_legs = json.loads(store.ledger_get("paper", income_id)["legs_json"])
    expense_legs = json.loads(store.ledger_get("paper", expense_id)["legs_json"])
    assert any(l["account"].startswith("income:funding") for l in income_legs)
    assert any(l["account"].startswith("expenses:funding") for l in expense_legs)
    assert trial_balance(store, "paper")["USD"] == Decimal("0")
