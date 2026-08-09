# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   ├── test_cases.py        # example expectations for the four provided cases
│   └── test_engine.py       # 20 focused unit tests (shapes, floors, sim, Part 2, rounding)
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

---

# Solution write-up

## Approach

The engine is built from three small, independent pieces that compose:

1. **A date-by-date simulator** (`_simulate`). Given the starting balance and a
   set of dated credits/debits, it walks the timeline applying **all credits
   before all debits** on each date and records the balance after every event.
   This is the single place feasibility (`balance >= 0` everywhere) is decided —
   every other part of the engine defers to it. Only ledger entries dated
   **after** `as_of_date` are simulated; earlier ones are already baked into
   `current_balance_cents`.

2. **A payment-shape generator.** The creditor flags select a *family* of
   candidate creditor-payment vectors. Every generated candidate already
   satisfies the hard structural constraints (consecutive count `k`, exact sum,
   non-decreasing, per-position floors, and the `max_segments` cap). Nothing
   downstream has to re-check structure — only feasibility.

3. **A greedy program-fee front-loader** (`_evaluate_vector`). For a fixed
   payment vector, we first confirm the creditor payments + bank fees keep the
   balance non-negative, then collect the program fee as early as the balance
   allows.

We enumerate the candidate family, keep the feasible ones, and pick the schedule
that collects the fee **earliest** — formalised as the lexicographically largest
vector of *(fee collected on cadence date 1, date 2, …)*, tie-broken by fewer
total bank fees. If nothing is feasible, Part 2 kicks in.

### Why greedy fee front-loading is correct (and optimal)

The program fee is the only *flexible* debit — creditor payments, bank fees, and
committed ledger debits are all forced. Collecting `f` of fee at a date
permanently lowers **every later** balance by `f`. So the most fee we can pull at
cadence date `d` without pushing any *future* date negative is

```
room(d) = (minimum balance from d to the horizon) − (fee already collected)
```

i.e. the **suffix-minimum** of the fee-free balance timeline. Taking `min(remaining_fee, room(d))`
at each date in chronological order both (a) never violates feasibility and (b)
collects as much as possible as early as possible — which is exactly the stated
objective. If the greedy pass can't collect the full fee by the horizon, no
schedule can (greedy extracts the maximum total collectible), so the vector is
infeasible. This lets me treat "keep early creditor payments small" (the shape)
and "grab fee early" (the fee pass) as separate concerns rather than one big
joint optimisation.

## Payment-shape interpretation (the open-ended part)

The shape is an **outcome** of one objective — *collect the program fee as early
as possible* — plus the creditor flags. Because early dollars are split between
the creditor and our fee, the objective always pushes toward **minimal creditor
payments early, deferring the larger ones**. Each flag constrains how that
deferral is expressed:

- **`even_pays = true` → `"even"`.** All `k` payments equal, with any indivisible
  remainder cents placed on the **latest** payments ("as equal as possible",
  keeping the sequence non-decreasing). We search all feasible `k` and keep the
  one that front-loads the fee best. Floors still apply, so a `k` whose equal
  payment would fall below a floor (or would need more than `max_token_pays`
  payments sitting at the base minimum) is rejected.

- **`is_ballooning_allowed = true` → `"balloon"`.** Payments `1..k-1` sit at their
  **floors** (the smallest the rules allow), and the final payment absorbs the
  entire remainder. This defers the most and therefore front-loads the fee the
  most, so when ballooning is allowed we **prefer** it. If no balloon is feasible
  (the final payment can't be afforded at its date for any `k`), we fall back to a
  staircase and report `"staircase"`.
  - **Token pays / tiers vs. the balloon:** floors bind *before* the balloon. The
    early minimal payments respect the base minimum, the `max_token_pays` cap
    (only the first `max_token_pays` may equal the base min), and any tier
    step-up; the balloon is simply the residual dropped on the last payment.

- **Neither flag → `"staircase"`.** A non-decreasing schedule using **at most
  `max_segments` distinct payment levels**. Interpretation: partition the `k`
  positions into up to `max_segments` **contiguous** segments; fix every
  non-final segment at the lowest level its floors and the previous segment
  allow (minimal-early = maximal fee front-loading); let the **final** segment
  absorb the remaining mass "as equal as possible". A one-cent remainder inside a
  segment is allowed and still counts as that single level *only if* the total
  distinct-value count stays `≤ max_segments` (checked explicitly). We enumerate
  every `(k, segment-count, grouping)` — small (`k ≤ 12`, `segments ≤ 4`) — and
  keep the feasible candidate that front-loads the fee best.

**Per-position floor.** The floor at payment position `i` (1-based) is the max of:
the base `min_payment_cents`; `min_payment_cents + 1` when `i > max_token_pays`
(so only the earliest `max_token_pays` payments may equal the base min — valid
because payments are non-decreasing, so the token pays are necessarily the
earliest); and any `min_payment_tiers` step-up whose start position is `≤ i`.

## Part 2 — minimum additional funds

Both minima reuse the exact same feasibility oracle (`_is_feasible`), and both are
**monotone** in the amount of money added, so each is a clean binary search:

- **Lump sum `L`.** Placed on the **earliest useful date** (`first_draft_date`),
  since earlier money is weakly more useful and therefore minimises the required
  amount. Binary-search the smallest `L` that flips feasibility. Guardrail:
  reject if `L > round(0.65 × offer_total)`.
- **Monthly increment `X`.** Added to **every** future draft (each credit dated
  after `as_of_date`); `N` is that count. Binary-search the smallest `X`.
  Guardrail: reject if `X > max(10000, round(0.40 × draft_amount_cents))`.

The two can imply different totals — an increment near the horizon adds cash that
may arrive *after the last usable cadence date* and so does nothing (see case 2:
the increment is applied to all 5 drafts, `N=5`, but only the first 4 actually
help, so `X=2500` where the lump is `10000`).

## Assumptions

- **Round-half-up is implemented explicitly** via `Decimal(...).quantize(ROUND_HALF_UP)`;
  the provided `models.py` helpers use Python's built-in `round` (banker's /
  half-to-even), which the spec forbids — e.g. `0.5 × 12345` must be `6173`, not
  `6172`. The engine defines its own `offer_total_cents` / `program_fee_cents`.
- The offer balance field is read as `current_balance_cents` because that is what
  the provided loaders and `cases/*/offer.json` actually use. (The spec mentions a
  rename to `creditor_balance_cents`, but the shipped scaffolding was not renamed;
  the engine follows the shipped data. Swapping the loader is a one-line change if
  the field is ever renamed.)
- **Ledger credits are drafts; ledger debits are committed and fixed.** The
  monthly-increment adds `X` to every credit dated after `as_of_date`, never to
  debits, and never modifies committed entries.
- **Fee-only cadence dates** (cadence dates past the last creditor payment, up to
  the horizon) are valid places to finish collecting the fee and incur **no** bank
  fee. Schedule rows are emitted only for cadence dates carrying a payment and/or
  fee.
- The lump sum is placed at `first_draft_date` (or `as_of_date + 1 day` if the
  first draft is not after `as_of_date`).
- Within a staircase, earlier segments are held at their minimal level; a grouping
  that is feasible only with *non-minimal* early levels may be missed, but the
  full sweep over `k` and groupings makes this a non-issue in practice, and
  minimal-early is what the objective wants anyway.

## Known edge cases / limitations

- **Draft after the last cadence date is wasted.** Because the cadence is
  independent of the draft schedule, a draft landing after the final usable
  cadence date can never be spent (case 2). Handled correctly by feasibility, and
  it's the reason the two Part-2 minima diverge.
- **`first_payment_date` after the horizon** ⇒ zero cadence dates ⇒ structurally
  infeasible for any amount of money; Part 2 reports `within_guardrail = false`
  with a "structural" reason instead of a spurious number.
- **No feasible schedule at any funding** (e.g. floors force a sum exceeding what
  any timing can support): the binary search detects infeasibility at its upper
  bound and reports it rather than returning a misleading amount.
- **`max_segments = 1` with a non-`even` creditor** means exactly one distinct
  payment level, i.e. all payments equal — so a `k` is usable only when the offer
  total divides evenly. This is a deliberate strict reading of "distinct payment
  levels": the `even_pays` flag is the mechanism that permits an "as equal as
  possible" ±1-cent remainder, so a non-`even` creditor that also caps levels at 1
  does not get that leniency. (All provided cases avoid this combination.)
- The candidate sweep is exhaustive but relies on the small rule sizes in scope
  (`k ≤ 12`, `max_segments ≤ 4`). For much larger caps this would want a DP or an
  LP/CP solver; the current enumeration is deliberately simple and easy to audit.

## Running

```bash
python run.py cases/case1_feasible_even     # prints the Result as JSON
python -m pytest -q                          # 30 tests: 6 smoke + 4 cases + 20 unit
```
