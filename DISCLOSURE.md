# Disclosure

This repository is an architecture overview of a private system. The strategy logic is
deliberately excluded.

AEGIS is a working automated trading system for crypto derivatives, currently running in paper
mode. What is published here is the engineering: the order finite state machine, the
idempotency model, the write-ahead journal, the reconciliation algorithm and its discrepancy
taxonomy, and the test structure. What is not published is anything describing what the system
trades or why.

## Published here

- The order-lifecycle state machine, its states, transitions and guarantees
- Idempotency key derivation from intent
- Write-ahead journalling and recovery by querying the broker
- The reconciliation algorithm, its safety ordering and its discrepancy taxonomy
- The test tree: module names, counts and what each module covers
- Code excerpts quoted from the private repository, chosen because they carry no strategy

## Never published, here or anywhere

- Signal generation logic of any kind
- Entry and exit trigger conditions
- Feature engineering, tuned parameter values and model weights
- Sleeve definitions and instrument selection
- Broker credentials, API keys, account identifiers, capital amounts and profit or loss
- Real positions or trade history
- Any performance figure, including from paper trading

## The line I drew

If a reader could reproduce the trading results from an artefact, it is strategy and it stays
private. If it only shows that the system is correct, recoverable and auditable, it is
engineering and it is published.

Where a document elides a value, it says so rather than quietly omitting it.
