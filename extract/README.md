# AEGIS extract

Six infrastructure modules copied from the private AEGIS repository, with 69 of the tests that
guard them. They run on their own: no network, no exchange, no third-party packages.

| Module | What it decides | Private source |
|---|---|---|
| `order_fsm.py` | The ten-state order lifecycle, which transitions are legal, the intent-derived client order id, and what the router does next: place, wait, ask the broker, attach a stop or escalate | `aegis/order_fsm.py` |
| `reconcile.py` | The minimal safe action set between the broker's positions and a target: protect first, cancel orphan orders next, then enter, exit, adjust, reverse or hold | `aegis/reconcile.py` |
| `intervention.py` | Whether an unexplained change at the broker is the owner's manual exit, to accept, or a fault, to alarm on | `aegis/intervention.py` |
| `live_risk.py` | Whether new risk is allowed on a live tick and whether the kill switch must flatten, plus the single-order size check | `aegis/live_risk.py` |
| `ledger.py` | Double-entry postings that must balance, decimal amounts, the hash chain, reversals, the trial balance and chain verification | `aegis/ledger.py` |
| `state_store.py` | The SQLite store in write-ahead-logging mode: the live-armed flag and its audit trail, the order journal, the alarm queue, the paper event log and the ledger tables | `aegis/state_store.py` |

Every file comes from commit `fcf0f74` (25 August 2026), and its first two lines list exactly
what changed. In short:

- Instrument names in the tests are placeholders (`COIN_A` to `COIN_D`), and the exchange's name
  is replaced by `venue`.
- The risk limits in `test_live_risk.py` and the reconciler's default rebalance band are
  illustrative values, not the live configuration. The live limits are configuration and are not
  published.
- Left out: the ledger's live-fill import, which is tied to the exchange's API, with its two
  tests; and the store's equity, trade and inception tables with their methods and one test.
  Those tables are performance bookkeeping.
- The ledger uses the standard library logger in place of the project's logging helper.
- Comment and docstring punctuation, and one internal document reference.

## Run it

```bash
python -m pip install -e ".[test]"
python -m pytest
```

69 tests, standard library only, Python 3.10 or later. GitHub Actions runs them on every push.

## What is not here

No signal logic, sleeve definitions, sizing, instrument lists, configuration or exchange
transport. The line is drawn in [DISCLOSURE.md](../DISCLOSURE.md).
