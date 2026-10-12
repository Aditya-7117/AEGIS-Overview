# The audit ledger

AEGIS keeps its own books in `aegis/ledger.py`. In paper trading it posts the fee and any
realised profit or loss of every fill, and every funding payment. In live trading it posts the
fee of every fill. Each posting is a double-entry record in one of two journals, paper or live.
The rules come from a private ledger specification, and the code tags each rule it enforces as
`LGR-n`. This page describes what those rules do.

## The rules

- **Every entry balances.** An entry needs at least two legs, and its legs must sum to exactly
  zero in each unit. One that does not balance raises an error at write time. It is never
  written and cleaned up later. (LGR-1)
- **No floats, anywhere.** Amounts are `Decimal` from the call site to the database. A float
  argument is converted through its string form, so `0.1` stays `0.1` instead of becoming a
  binary approximation, and amounts are stored as JSON strings rather than numbers. (LGR-7)
- **History is append-only.** The store has no method to update or delete a ledger entry. A
  mistake is corrected by a reversal: a new entry with every leg negated that names the entry it
  reverses. Both stay visible, the mistake and then the fix. Reversing the same entry twice is
  refused. (LGR-2)
- **Each entry is chained to the one before it.** An entry's SHA-256 hash covers its own fields
  and the previous entry's hash. The first entry in each journal chains from a genesis value of
  64 zeros, and the paper and live journals have independent chains. (LGR-3, LGR-15)
- **Ingesting the same event twice changes nothing.** A unique index on journal, source system,
  source event and kind makes a repeated posting a no-op that returns nothing, rather than an
  error or a duplicate. (LGR-4)
- **Nothing is dropped.** If a fill's economics cannot be parsed, the entry goes to a suspense
  account and an alert is raised. (LGR-6)

## Sign convention

Standard double entry, as in beancount, hledger and ledger-cli. Asset, expense and position
accounts increase with a positive amount. Income, liability, equity and suspense accounts
increase with a negative amount. That is what lets every balanced entry sum to zero.

A paper fill with a fee of 1.00 that closes a position at a profit of 50.00 posts four legs
(illustrative numbers):

| Account | Amount |
|---|---|
| `expenses:fees:venue:COIN_A` | 1.00 |
| `assets:exchange:venue:USD` | -1.00 |
| `income:realized:venue:COIN_A` | -50.00 |
| `assets:exchange:venue:USD` | 50.00 |

Funding received of 10.00 posts `income:funding:venue:COIN_A` at -10.00 against the exchange
account at 10.00. Funding paid uses an expense account instead, and one formula covers both
signs.

## Proving the books

Two checks run over the stored data:

- `trial_balance()` re-sums every leg of every entry by unit. Each entry balanced when it was
  written, so a total other than zero can only mean the stored data was changed outside the
  ledger's API.
- `verify_chain()` recomputes every hash in order and reports any entry whose stored hash no
  longer matches its contents, or whose link to the previous entry is broken.

A scheduled job runs both checks daily on the paper and live journals and raises a critical alarm
on any failure. The instruction in the code is to halt and investigate, never to adjust the books
to fit. (LGR-3, LGR-10)

## How it is wired

- Paper fills and funding payments post through hooks that the supervisor attaches after it has
  replayed the paper event log on startup. A restart therefore never posts old events a second
  time; they are already in the append-only table from when they happened.
- Live fills book their fees once per bar, in the same step as the live risk check, keyed by the
  venue's fill id, so seeing the same fills again posts nothing.

## Limits

- Live fills book fees only. Attributing realised profit to each live fill needs a persistent
  live cost-basis tracker, which is not built yet. The code records this as a follow-up rather
  than guessing with unverified API fields.
- The hash chain detects changes made outside the ledger's API. It is not anchored anywhere
  outside the database, so someone able to rewrite the whole file could recompute every hash.
  Catching that would need the latest hash published or signed somewhere else.

## Tests

The 16 ledger tests run in [`extract/tests/test_ledger.py`](../extract/tests/test_ledger.py):
unbalanced and single-leg entries rejected, the specification's worked example, idempotent
re-ingestion, chain links, tamper detection, independent paper and live chains, reversal without
mutating the original, a zero trial balance after mixed entries, the `0.1 + 0.2` float trap,
non-decimal input, and the fill and funding helpers. The two tests of the live-fill import, which
is tied to the exchange's API, are not in the extract.
