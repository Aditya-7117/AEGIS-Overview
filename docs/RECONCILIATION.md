# Reconciliation

The question this module answers: the system has been down for an unknown length of time, the
market has moved, and the broker holds whatever it holds. What is the minimal safe set of
actions that gets from the truth to the intent?

`aegis/reconcile.py` is a pure function. It takes what the broker says is real, takes what the
strategy currently wants, and returns a list of actions. It performs no input or output at all,
which means the hardest recovery logic in the system can be tested exhaustively with plain data
structures.

## The design decision that removes a whole class of bug

Quoted from the module docstring:

> On every startup and every bar, the engine computes DESIRED positions fresh from current
> market data, reads GROUND-TRUTH positions/orders from the broker, and calls `reconcile()` to
> get the minimal safe action set. Because desired is always fresh, stale intents are never
> replayed, the reconciler only compares held-vs-want.

This is the important part. A system that recovers by replaying a queue of intents it had saved
earlier will act on decisions that were made against a market that no longer exists. Recomputing
desired state from current data means there is no queue to replay and nothing stale to act on.
Recovery and normal operation then run the same code path, so the recovery path is exercised on
every single bar rather than only after a crash, which is when nobody wants to discover a bug in
it.

## Safety ordering

> Safety ordering (deterministic): PROTECT unprotected risk first, CANCEL orphan orders next,
> THEN bring positions to desired. This guarantees that no code path can leave a naked position
> or act on a stray order before risk is contained.

The order is fixed rather than emergent. Containing risk cannot be something that happens to
occur first because of how a loop was written.

## The discrepancy taxonomy

Every difference between broker truth and desired state is classified into exactly one action
kind before anything is sent:

```python
@dataclass(frozen=True)
class Action:
    kind: str                  # PROTECT | EXIT | ENTER | HOLD | ADJUST | REVERSE | CANCEL_ORPHAN
    symbol: str
    qty: float = 0.0           # target qty (ENTER/ADJUST/REVERSE) or held qty (EXIT/PROTECT)
    reason: str = ""
```

| Kind | The discrepancy it names |
|---|---|
| `PROTECT` | A position is held with no broker-side protective stop attached |
| `CANCEL_ORPHAN` | An order is open at the broker with no matching known intent |
| `ENTER` | Wanted, not held |
| `EXIT` | Held, not wanted |
| `ADJUST` | Held and wanted, in the same direction, outside the tolerance band |
| `REVERSE` | Held and wanted, in opposite directions |
| `HOLD` | Held and wanted, close enough that acting would only pay spread and fees |

Every action carries a `reason` string. When an operator asks why the system placed an order at
startup, the answer is in the action rather than reconstructed from logs.

`HOLD` matters more than it looks. Without a tolerance band, a position that is fractionally off
target generates an order on every bar, and the strategy pays costs forever to correct rounding.
The band's value is a tuned parameter and is not published here.

## The inputs

```python
def reconcile(
    positions: list[Position],
    desired: dict[str, float],
    open_orders: list[OpenOrder],
    known_client_oids: set[str],
    band: float = ...,          # tolerance band, value elided
) -> list[Action]:
```

`known_client_oids` is how an orphan is identified. Because order ids are derived from intent
(see [ORDER-FSM.md](ORDER-FSM.md)), the system can recognise its own orders at the broker after
a total restart, with no local state surviving at all. An open order whose id is not in that set
was not placed by this version of the system's intent, so it is cancelled rather than adopted.

## Position

```python
@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float                 # signed: +long / -short, in contracts
    protected: bool = True     # is a broker-side protective stop attached?
```

`protected` defaults to `True` and is set false only when the system has confirmed there is no
stop attached. The frozen dataclass is deliberate: reconciliation inputs are a snapshot of
truth, and code that could mutate them mid-decision would be able to reconcile against a
position that changed underneath it.
