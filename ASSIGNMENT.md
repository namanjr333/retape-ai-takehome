# Take-home: Settlement Feasibility & Fee Engine

**Time budget:** ~5–6 hours. We care about correctness on edge cases, clear
modeling, and tests — not breadth of features.

When something is genuinely ambiguous, **state your assumption in the README and
move on** rather than emailing us. One part of this problem (the payment *shape*)
is deliberately open-ended; the rest is meant to be unambiguous, so if a rule
below seems unclear, read it as a hard requirement and pick the simplest reading.

---

## 1. The problem in plain language

A client saves a fixed amount every month into one escrow account. Out of that
same account, we pay off one of their debts to a creditor — in monthly
installments — while also taking our own fee and covering a small per-payment
bank fee.

Your job: given the account, a settlement offer, and the creditor's rules,

1. decide whether the offer is **affordable** (the account never goes negative),
   and if so, produce a payment schedule; and
2. if it is **not** affordable, compute the **minimum extra money** that would
   make it affordable.

The creditor's rules are **inputs** — different creditors send different values.
Handle them generically; don't hard-code anything for a specific creditor.

---

## 2. Glossary

Read this once; every term below is used precisely throughout.

| Term | Meaning |
|---|---|
| **SDA** | The single escrow account the client deposits into and we pay out of. |
| **Draft** | The client's fixed monthly deposit into the SDA. Drafts are `credit` entries already present in the ledger. |
| **Ledger** | A dated list of `credit` and `debit` entries on the SDA. |
| **Creditor payment** | A payment we make toward settling the debt, in monthly installments. |
| **Cadence** | The monthly recurrence of creditor-payment / fee dates, starting at `first_payment_date`. **Independent** of the draft schedule — so you must evaluate the ledger date by date. |
| **Horizon** | `last_draft_date`. Nothing may be scheduled on or after a date past the horizon; the horizon date itself is allowed. |
| **Program fee** | Our fee. Total = `round(program_fee_pct × original_balance_cents)`. Collected across cadence dates. |
| **Bank fee** | A flat fee charged on each date that carries a creditor payment. |
| **Fee-only month** | A cadence date carrying only program fee and no creditor payment. Incurs **no** bank fee. |
| **Offer total** | What we must pay the creditor = `round(settlement_pct × creditor_balance_cents)`. |
| **Floor** | The minimum allowed size of a creditor payment at a given position (see §5). |
| **Token pay** | A creditor payment sitting exactly at the base minimum (`min_payment_cents`). At most `max_token_pays` of them are allowed. |
| **Tier** | A step-up floor that applies from a given payment number onward (`min_payment_tiers`). |
| **Payment level / segment** | A distinct payment amount used in the schedule. `max_segments` caps how many distinct levels a staircase may use. |
| **Balloon** | A final payment that absorbs the entire remaining balance (small payments early, one large payment at the end). Only allowed when `is_ballooning_allowed` is true. |

---

## 3. Conventions: money, dates, ordering

- **Money** is always **integer cents**. **Dates** are calendar dates.
- **Rounding:** whenever a rule says `round(...)`, use **round-half-up** (a `.5`
  always rounds away from zero). Implement this explicitly — do **not** rely on
  your language's default rounding, which may round half-to-even.
- **Same-day ordering:** on any date, apply **all credits before all debits**.
- **`current_balance_cents` (on the client)** is the SDA balance **as of
  `as_of_date`**. Ledger entries dated on or before `as_of_date` are already
  baked into that balance; entries dated **after** `as_of_date` are the
  modifiable future you simulate.

> **Note on a renamed field:** the offer's balance field is named
> `creditor_balance_cents` (in the previous draft both the client and the offer
> used `current_balance_cents`, which was a footgun). The client's SDA balance
> stays `current_balance_cents`; the creditor's balance is `creditor_balance_cents`.

### Draft schedule

Drafts land on `draft_day` each month from `first_draft_date` through
`last_draft_date` inclusive, and are already in the ledger as credits. The credits
in the ledger **are** the drafts; any debits in the ledger are previously-committed
payments/fees from other settled debts and are **fixed** — respect them, never
modify them.

### Payment cadence (`first_payment_date`)

Creditor payments and fees recur monthly on their own cadence, **independent** of
the draft schedule:

| Situation | Resulting cadence |
|---|---|
| `first_payment_date` **omitted** | Defaults to the **end of the month** of `first_draft_date`. |
| Provided date **is** the last day of its month | True end-of-month every month (e.g. Jan 31 → Feb 28/29 → Mar 31 → …). |
| Provided date is **mid-month** | The same day-of-month each month, **clamped** to month length (e.g. Jan 31-style day 31 on a preserved cadence would clamp; day 15 → Feb 15 → Mar 15 → …). |

Helpers for this are provided in `feasibility/models.py`.

---

## 4. Inputs

Each case is a folder with three files (see `cases/`).

**`client.json`**
```json
{
  "draft_amount_cents": 20000, "draft_day": 1,
  "first_draft_date": "2026-01-01", "last_draft_date": "2026-07-01",
  "as_of_date": "2025-12-31", "current_balance_cents": 0,
  "ledger": [{"date": "2026-01-01", "amount_cents": 20000, "type": "credit"}]
}
```

**`offer.json`**
```json
{
  "creditor": "EvenCo", "creditor_balance_cents": 100000,
  "original_balance_cents": 120000, "settlement_pct": 0.5,
  "first_payment_date": "2026-01-31"
}
```

**`creditor_rules.json`**
```json
{
  "max_terms": 12, "max_payments": 12,
  "min_payment_cents": 2500, "max_token_pays": 6,
  "min_payment_tiers": [[7, 5000]],
  "even_pays": false, "is_ballooning_allowed": false, "max_segments": 2,
  "bank_fee_cents": 500, "program_fee_pct": 0.2
}
```

**Parameter meanings** (each defined once, here):

- `max_terms`, `max_payments` — both cap the number of creditor payments `k`.
  Because payments are consecutive with no gaps, these bind identically:
  `k ≤ min(max_payments, max_terms)`. *(Author note: these are currently
  redundant — consider dropping one or giving them distinct meanings.)*
- `min_payment_cents` — the base minimum per creditor payment.
- `max_token_pays` — the maximum number of payments allowed to sit **at** the base
  minimum. Beyond that count, payments must **exceed** the base minimum.
- `min_payment_tiers` — explicit step-up floors as `[from_payment_number (1-based),
  min_cents]`: from that payment onward, the floor is at least `min_cents`.
- `even_pays` — boolean (default `false`). See §5.
- `is_ballooning_allowed` — boolean (default `false`). See §6.
- `max_segments` — cap on the number of **distinct payment levels** a staircase may
  use. Ignored when `even_pays` or `is_ballooning_allowed` is set.
- `program_fee_pct` — total program fee = `round(program_fee_pct ×
  original_balance_cents)`.
- `bank_fee_cents` — flat fee debited on each date carrying a creditor payment.

---

## 5. Binding constraints (hard requirements)

A schedule is **valid** only if all of these hold. These are not negotiable.

1. **Count & placement.** Creditor payments occupy **consecutive** cadence dates
   (no gaps), starting at `first_payment_date`. You choose the count `k`, with
   `1 ≤ k ≤ min(max_payments, max_terms)`, and every date `≤` horizon.
2. **Exact sum.** The creditor payments sum **exactly** to `offer_total`.
3. **Non-decreasing.** Each creditor payment is `≥` the one before it.
4. **Floors.** Each payment is `≥` the floor that applies at its position. The
   floor is the maximum of: the base `min_payment_cents`; the token-pay rule (at
   most `max_token_pays` payments may equal the base min — any further payment must
   strictly exceed it); and any applicable `min_payment_tiers` step-up.
5. **Bank fee.** `bank_fee_cents` is debited on **each date carrying a creditor
   payment**, and never on a fee-only date.
6. **Program-fee timing.** The total program fee may be split across cadence dates
   in any non-negative amounts, subject to: **(a)** none before the first creditor
   payment date (the same date is allowed); **(b)** fully collected on or before
   the horizon. A fee-only date carries only fee and incurs no bank fee.
7. **Even pays.** If `even_pays` is `true`, all creditor payments are equal. When
   `offer_total` is not divisible by `k`, distribute the remainder cents onto the
   **latest** payments (so the sequence stays non-decreasing) — i.e. "as equal as
   possible." Ballooning is irrelevant in this case.
8. **Balloon.** A balloon (final payment absorbing the remaining balance) is only
   permitted when `is_ballooning_allowed` is `true`.
9. **Segments.** When **not** even and **not** ballooning, the schedule may use at
   most `max_segments` distinct payment levels.
10. **Feasibility.** Simulating the full ledger (committed entries + creditor
    payments + bank fees + program fees) chronologically, with credits-before-debits
    on each date, the running balance is **`≥ 0` at every date**, and nothing is
    scheduled past the horizon.

---

## 6. The objective (the open-ended part — this is the crux)

Within the hard constraints above, **many schedules can be valid.** We do *not*
hand you a fixed shape. The shape — a flat line, a staircase, or a balloon — should
be an **outcome** of one economic objective plus the creditor flags, not something
you hard-code.

**Objective: collect our program fee as early as possible (front-loaded).**

Because the same early dollars are split between paying the creditor and collecting
our fee, this naturally pushes you to **keep creditor payments as low as the rules
allow early on** and defer the larger creditor payments later. The shape falls out
of that.

How the flags constrain the shape:

- **`even_pays = true`** → every payment equal (see constraint 7). Choose the `k`
  that best serves the objective.
- **`is_ballooning_allowed = true`** → the final payment may absorb the remaining
  balance: minimum-ish payments early, one large payment at the end.
- **Neither set** → payments may step up over time, using at most `max_segments`
  distinct levels. We are **not** prescribing where the steps go — that follows
  from the objective.

We describe this loosely **on purpose**. Model the shapes as you see fit and
**document your interpretation in the README** — in particular, how token pays /
tiers interact with a final balloon, and how you place steps under `max_segments`.
We care about your reasoning, not about matching a hidden formula. Report which
shape you produced in `pay_shape_used` (`"even"`, `"staircase"`, or `"balloon"`).

### Worked micro-example (shows the objective)

Horizon = 3 cadence dates; \$100 lands before each date; start \$0;
`offer_total = $250`, `program_fee = $50`, `bank_fee = $0`, flat min \$25.

A valid schedule pays `[$50, $100, $100]`. The fee can't be collected before the
first creditor payment, so the earliest moment is the first date: after paying \$50,
\$50 is free, so we collect the full \$50 fee and land at \$0. The remaining dates
net to \$0. **Feasible, fee fully front-loaded.**

---

## 7. Part 1 — produce a schedule (when feasible)

If the offer is feasible, output one valid schedule that collects the program fee
as early as possible. For each used cadence date, output the date, creditor
payment, program fee, bank fee, and the resulting running balance. Set
`feasible: true` and `pay_shape_used`.

---

## 8. Part 2 — minimum additional funds (when infeasible)

When no valid schedule exists, set `feasible: false`, `schedule: null`, and compute
the minimum extra funding in **two independent forms**:

- **Lump sum** — the smallest single extra `credit` `L`, on a date of your choosing
  (`≤` horizon), that makes a feasible schedule exist. Since an earlier lump is
  weakly more useful, report the smallest `L` and the date you placed it on.
- **Monthly increment** — the smallest uniform amount `X` added to **every future
  draft** (each draft dated after `as_of_date`) that makes it feasible. Report `X`
  and `N` = the number of future drafts affected.

Then apply these guardrails and report pass/fail with a reason:

- Reject the **monthly increment** if `X > max(10000, round(0.40 × draft_amount_cents))`.
- Reject the **lump sum** if `L > round(0.65 × offer_total)`.

The lump and the increment can imply different totals — that's expected, because an
increment near the horizon may add cash that arrives too late to be useful.

---

## 9. Output

`evaluate_offer(client, offer, rules) -> Result`. The serialized shape
(`Result.to_dict()`, already implemented in `feasibility/engine.py`):

```json
{
  "feasible": true,
  "pay_shape_used": "even",
  "schedule": [
    {"date": "2026-01-31", "creditor_payment_cents": 8334, "program_fee_cents": 11166, "bank_fee_cents": 500, "balance_cents": 0}
  ],
  "additional_funds": null
}
```

*(Illustrative: the row is self-consistent — `8334 + 11166 + 500 = 20000`, matching
a single \$200.00 draft with `bank_fee_cents = 500`, leaving balance 0.)*

When infeasible, `schedule` is `null` and `additional_funds` is:

```json
{
  "lump_sum": {"amount_cents": 10000, "date": "2026-01-01", "within_guardrail": true, "reason": ""},
  "monthly_increment": {"amount_cents": 2500, "num_drafts": 5, "within_guardrail": true, "reason": ""}
}
```

---

## 10. Deliverables

- A runnable repo. `python run.py cases/case1_feasible_even` should print a Result.
- A **README** covering: your approach and alternatives considered; **your shape /
  ballooning interpretation**; assumptions; and known edge cases.
- **Tests** (`tests/test_cases.py` is the minimum bar) covering at least: even /
  staircase / balloon shapes; token-pay and tier floors; the `max_segments` cap;
  exact-sum; the date-by-date simulation (same-day ordering, and a balance that
  hits exactly \$0); the horizon limit; fee compliance (no fee before the first
  payment); and both Part 2 minima.

---

## What we evaluate

How you frame and model the problem; correctness on edge cases (dates, cents, exact
sums); the quality of your feasibility and minimum-funds reasoning; your test rigor;
and clear communication. There is no single "right" approach to the payment shape.
