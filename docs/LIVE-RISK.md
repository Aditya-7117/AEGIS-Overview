# Live risk decisions

`aegis/live_risk.py` decides, once per bar in live mode, whether the system may open new
risk and whether it must flatten everything. It is a set of pure functions. The supervisor reads
the broker's balances and positions and passes them in; the functions only decide. That is why
the rules that matter most for money can be tested with plain numbers and no broker.

## The stance

From the module docstring:

> Design stance (owner mandate): never hallucinate. Decisions run on the live broker snapshot,
> never on assumed state; and never take a catastrophic action on uncertain data: a failed/stale
> read HALTS new risk but does NOT flatten (only a CONFIRMED breach does).

Flattening is the most drastic action the system has. Taking it because a request timed out
would turn a network blip into a forced exit at whatever price the market offers. So bad data
stops new trades and nothing more.

## The decision order

`evaluate()` checks in a fixed order and returns at the first rule that fires:

| Order | Condition | New risk | Flatten |
|---|---|---|---|
| 1 | The broker snapshot could not be read, or the caller marks it stale | Blocked | No |
| 2 | Equity is below the drawdown limit, measured from its peak | Blocked | Yes |
| 3 | Equity is below the daily loss limit, measured from the UTC day open | Blocked | Yes |
| 4 | A position differs from the venue's by more than the tolerance | Blocked | No |
| 5 | Margin in use, as a share of used plus available margin, is above the ceiling | Blocked | No |
| 6 | None of the above | Allowed | No |

The order is the design. The data check comes first, so no later rule ever acts on numbers that
may be wrong. The two loss limits come next because they are the only rules allowed to flatten.
A reconciliation break or a margin ceiling stops new orders but leaves open positions, and their
protective stops, where they are. Every blocking verdict carries a reason that names the rule and
the numbers, and that reason is what the alarm, the halt record or the warning log shows.

## The single-order check

`fat_finger_ok()` runs separately, on each order. One order's notional may not exceed a multiple
of equity, and with zero or negative equity every order is refused. The multiple is generous on
purpose, well above the portfolio's gross exposure cap, so it only catches a sizing bug or a bad
price, never a normal trade.

## How it is wired

In live mode the supervisor runs this step at the start of every bar, before any new decision:

1. It reads the wallet balance, margin in use and positions from the broker. If any read fails,
   it returns the fail-closed verdict itself, without calling `evaluate()`.
2. It books fees for any new live fills in the audit ledger (see [AUDIT-LEDGER.md](AUDIT-LEDGER.md)).
3. It builds the expected positions from the system's own open trades, and takes the equity peak
   and the day-open equity from the state store.
4. It calls `evaluate()` with the limits from configuration. A read that succeeded is passed in
   as fresh.

What happens next depends on the verdict. A flatten fires the kill switch, which cancels every
order and closes every position at the venue, and then sets a halt on new risk. A reconciliation
break raises a critical alarm and sets the same halt without flattening. The halt lives in the
state store, so it survives a restart, and it stays until the owner clears it: a breach never
resumes trading on its own. A margin ceiling or a failed read blocks new risk for that bar only.

The limits are configuration, not code, and they are not published. The tests in
[`extract/`](../extract/) use illustrative limits.

## Tests

The nine tests run in [`extract/tests/test_live_risk.py`](../extract/tests/test_live_risk.py).
They pin break detection and its tolerance, the exact boundary of each limit, the fail-closed
behaviour of a failed or stale snapshot, flattening on each loss breach, a reconciliation break
and a margin ceiling that block without flattening, the all-clear case, and the single-order
check.
