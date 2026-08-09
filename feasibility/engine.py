"""Settlement feasibility & fee engine.

Implements ``evaluate_offer`` per ASSIGNMENT.md.

High-level design
-----------------
The problem decomposes into three independent pieces that compose cleanly:

1. **A date-by-date simulator** (`_simulate`).  Given a starting balance and a
   set of dated credits/debits, it walks the timeline (credits-before-debits on
   each date) and reports the balance after every event date.  This is the one
   place feasibility (`balance >= 0` everywhere) is decided.

2. **A payment-shape generator.**  The creditor flags select a *family* of
   candidate creditor-payment vectors:

   * ``even_pays``            -> equal payments ("as equal as possible").
   * ``is_ballooning_allowed``-> minimal floors early, one final balloon.
   * neither                  -> staircase: <= ``max_segments`` distinct levels,
                                 kept minimal early and stepping up late.

   Every candidate already satisfies the hard structural constraints (count,
   exact sum, non-decreasing, floors, segment cap).

3. **A greedy fee front-loader** (`_evaluate_vector`).  For a fixed payment
   vector we first check creditor feasibility, then collect the program fee as
   early as the balance allows.  Front-loading is optimal for the stated
   objective, and using the *suffix-minimum* of the balance guarantees we never
   collect fee that would push a later date negative.

We enumerate the candidate family, keep the feasible ones, and pick the schedule
that collects the fee earliest (lexicographic on fee-collected-per-date).  If
nothing is feasible we fall to Part 2 (minimum additional funds), solved by a
monotone binary search that reuses the very same feasibility oracle.

See README.md for the shape/ballooning interpretation and assumptions.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from feasibility.models import (
    Client,
    CreditorRules,
    Offer,
    LedgerEntry,
    default_first_payment_date,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
    round_half_up,
)

# Re-exported from models so callers/tests can import them from the engine too.
__all__ = [
    "evaluate_offer",
    "Result",
    "ScheduleRow",
    "FundsOption",
    "AdditionalFunds",
    "offer_total_cents",
    "program_fee_cents",
    "round_half_up",
    "position_floor",
]

# ---------------------------------------------------------------------------
# Output dataclasses (serialized shape is fixed by ASSIGNMENT.md)
# ---------------------------------------------------------------------------


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:

            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


# ---------------------------------------------------------------------------
# Floors
# ---------------------------------------------------------------------------


def position_floor(i_one_based: int, rules: CreditorRules) -> int:
    """Minimum allowed creditor payment at position ``i`` (1-based).

    Combines three floors (constraint 4), taking the max:
      * base ``min_payment_cents``;
      * the token-pay rule: only the first ``max_token_pays`` payments may sit
        AT the base minimum, so from position ``max_token_pays + 1`` on the floor
        rises to ``min_payment_cents + 1`` (must strictly exceed the base min);
      * any ``min_payment_tiers`` step-up whose start position is <= ``i``.

    Because payments are non-decreasing and we want early payments as small as
    possible, the token pays are necessarily the earliest ones — so a per-position
    floor captures the token rule exactly.
    """
    floor = rules.min_payment_cents
    if i_one_based > rules.max_token_pays:
        floor = rules.min_payment_cents + 1
    for start, min_cents in rules.min_payment_tiers:
        if i_one_based >= start:
            floor = max(floor, min_cents)
    return floor


def _floors(k: int, rules: CreditorRules) -> list[int]:
    return [position_floor(i, rules) for i in range(1, k + 1)]


# ---------------------------------------------------------------------------
# Cadence / horizon
# ---------------------------------------------------------------------------


def first_payment_date(offer: Offer, client: Client) -> date:
    return offer.first_payment_date or default_first_payment_date(client)


def cadence_dates(offer: Offer, client: Client) -> list[date]:
    """All cadence dates on/after the first payment date and <= horizon."""
    start = first_payment_date(offer, client)
    horizon = client.last_draft_date
    if start > horizon:
        return []
    # Generate generously, then clip at the horizon.
    span_months = (horizon.year - start.year) * 12 + (horizon.month - start.month) + 2
    dates = monthly_payment_dates(start, max(span_months, 1))
    return [d for d in dates if d <= horizon]


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


def _simulate(
    client: Client,
    debits_by_date: dict[date, int],
    extra_credits_by_date: dict[date, int] | None = None,
    checkpoints: tuple[date, ...] = (),
) -> tuple[dict[date, int], bool]:
    """Walk the timeline; return (balance_after_each_date, ok).

    ``ok`` is True iff the balance is >= 0 at every date.  Only ledger entries
    dated strictly after ``as_of_date`` are simulated (earlier ones are already
    baked into ``current_balance_cents``).  Credits are applied before debits on
    each date.  ``checkpoints`` forces a balance readout on those dates even when
    nothing is scheduled there (used for fee-only cadence dates).
    """
    from collections import defaultdict

    credits: dict[date, int] = defaultdict(int)
    debits: dict[date, int] = defaultdict(int)

    for e in client.ledger:
        if e.date > client.as_of_date:
            if e.type == "credit":
                credits[e.date] += e.amount_cents
            else:
                debits[e.date] += e.amount_cents

    if extra_credits_by_date:
        for d, a in extra_credits_by_date.items():
            credits[d] += a
    for d, a in debits_by_date.items():
        debits[d] += a

    all_dates = sorted(set(credits) | set(debits) | set(checkpoints))
    balance = client.current_balance_cents
    balance_after: dict[date, int] = {}
    ok = balance >= 0
    for d in all_dates:
        balance += credits.get(d, 0)
        balance -= debits.get(d, 0)
        balance_after[d] = balance
        if balance < 0:
            ok = False
    return balance_after, ok


# ---------------------------------------------------------------------------
# Evaluate one candidate payment vector
# ---------------------------------------------------------------------------


@dataclass
class _Evaluated:
    rows: list[ScheduleRow]
    fee_vector: tuple[int, ...]  # fee collected per cadence date (chronological)
    total_bank_fee: int


def _evaluate_vector(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    cad: list[date],
    payments: list[int],
    total_fee: int,
    extra_credits: dict[date, int] | None = None,
) -> _Evaluated | None:
    """Check feasibility of a payment vector and, if feasible, front-load the fee.

    Returns None if the creditor payments themselves make the account go negative,
    or if the full program fee cannot be collected on/before the horizon.
    """
    bank = rules.bank_fee_cents
    k = len(payments)

    # Forced debits: creditor payments + bank fee on each payment date.
    forced_debits: dict[date, int] = {}
    for i, amt in enumerate(payments):
        d = cad[i]
        forced_debits[d] = forced_debits.get(d, 0) + amt + bank

    # Fee-eligible cadence dates are every cadence date from the first payment
    # onward (constraint 6a); fee-only months beyond the k-th payment count too.
    fee_dates = list(cad)

    base_bal, ok = _simulate(client, forced_debits, extra_credits, checkpoints=tuple(fee_dates))
    if not ok:
        return None

    # Suffix minimum of the (fee-free) balance timeline.  Collecting F of fee at a
    # date permanently lowers every later balance by F, so the most we can pull at
    # date d without any FUTURE date going negative is (suffix-min from d) minus
    # what we've already collected.
    ordered = sorted(base_bal)
    suffix_min: dict[date, int] = {}
    running = None
    for d in reversed(ordered):
        running = base_bal[d] if running is None else min(running, base_bal[d])
        suffix_min[d] = running

    remaining = total_fee
    collected = 0
    fee_on: dict[date, int] = {}
    for d in fee_dates:
        if remaining <= 0:
            break
        room = suffix_min[d] - collected
        take = max(0, min(remaining, room))
        if take:
            fee_on[d] = take
            collected += take
            remaining -= take
    if remaining > 0:
        return None  # cannot collect the full program fee by the horizon

    # Recompute the true running balance including the fee, for the output rows.
    final_debits = dict(forced_debits)
    for d, f in fee_on.items():
        final_debits[d] = final_debits.get(d, 0) + f
    final_bal, ok2 = _simulate(
        client, final_debits, extra_credits, checkpoints=tuple(fee_dates)
    )
    assert ok2, "fee front-loading must preserve feasibility"

    payment_by_date = {cad[i]: payments[i] for i in range(k)}
    rows: list[ScheduleRow] = []
    for d in cad:
        pay = payment_by_date.get(d, 0)
        fee = fee_on.get(d, 0)
        if pay == 0 and fee == 0:
            continue
        rows.append(
            ScheduleRow(
                date=d,
                creditor_payment_cents=pay,
                program_fee_cents=fee,
                bank_fee_cents=bank if pay > 0 else 0,
                balance_cents=final_bal[d],
            )
        )

    fee_vector = tuple(fee_on.get(d, 0) for d in cad)
    return _Evaluated(rows=rows, fee_vector=fee_vector, total_bank_fee=k * bank)


# ---------------------------------------------------------------------------
# Candidate payment-vector generators (one family per shape)
# ---------------------------------------------------------------------------


def _valid_vector(payments: list[int], total: int, rules: CreditorRules) -> bool:
    """Structural validity independent of feasibility (constraints 1-4, 9)."""
    k = len(payments)
    if k == 0 or sum(payments) != total:
        return False
    floors = _floors(k, rules)
    prev = 0
    for i, p in enumerate(payments):
        if p < floors[i] or p < prev:  # floor + non-decreasing
            return False
        prev = p
    return True


def _even_candidates(total: int, kmax: int, rules: CreditorRules):
    """Equal payments, remainder cents on the latest payments ("as equal as possible")."""
    for k in range(1, kmax + 1):
        q, r = divmod(total, k)
        payments = [q] * (k - r) + [q + 1] * r  # non-decreasing
        if _valid_vector(payments, total, rules):
            yield payments


def _balloon_candidates(total: int, kmax: int, rules: CreditorRules):
    """Minimal floors on payments 1..k-1; the final payment absorbs the remainder."""
    for k in range(1, kmax + 1):
        floors = _floors(k, rules)
        head = floors[: k - 1]
        last = total - sum(head)
        payments = head + [last]
        if _valid_vector(payments, total, rules):
            yield payments


def _compositions(k: int, parts: int):
    """Yield contiguous group sizes splitting k positions into ``parts`` positive groups."""
    if parts == 1:
        yield (k,)
        return
    for cuts in combinations(range(1, k), parts - 1):
        prev = 0
        sizes = []
        for c in cuts:
            sizes.append(c - prev)
            prev = c
        sizes.append(k - prev)
        yield tuple(sizes)


def _staircase_candidates(total: int, kmax: int, rules: CreditorRules):
    """Non-decreasing vectors using <= max_segments distinct levels, minimal early.

    For each count k and each contiguous grouping of positions into `s` segments,
    fix every non-final segment at the lowest level its floors and the previous
    segment allow, then let the final segment absorb whatever remains "as equal as
    possible".  Keeping earlier segments minimal is exactly what front-loads the
    fee; the final segment carries the deferred mass.
    """
    seen: set[tuple[int, ...]] = set()
    for k in range(1, kmax + 1):
        floors = _floors(k, rules)
        if sum(floors) > total:
            continue
        smax = min(rules.max_segments, k)
        for s in range(1, smax + 1):
            for sizes in _compositions(k, s):
                # group floor = max floor within each contiguous group
                bounds = []
                idx = 0
                for sz in sizes:
                    bounds.append((idx, idx + sz))
                    idx += sz
                levels: list[int] = []
                ok = True
                prev_level = 0
                used = 0
                for gi in range(s - 1):
                    lo, hi = bounds[gi]
                    gf = max(floors[lo:hi])
                    level = max(gf, prev_level)
                    levels.append(level)
                    used += level * (hi - lo)
                    prev_level = level
                lo, hi = bounds[s - 1]
                size_last = hi - lo
                remainder = total - used
                if remainder < 0:
                    continue
                base_last = remainder // size_last
                extra = remainder % size_last
                # "as equal as possible": (size_last - extra) at base_last, then +1
                last_group = [base_last] * (size_last - extra) + [base_last + 1] * extra
                gf_last = max(floors[lo:hi])
                if last_group and last_group[0] < max(gf_last, prev_level):
                    continue
                payments = []
                for gi in range(s - 1):
                    lo2, hi2 = bounds[gi]
                    payments.extend([levels[gi]] * (hi2 - lo2))
                payments.extend(last_group)
                if not _valid_vector(payments, total, rules):
                    continue
                if len(set(payments)) > rules.max_segments:
                    continue
                key = tuple(payments)
                if key in seen:
                    continue
                seen.add(key)
                yield payments


# ---------------------------------------------------------------------------
# Core solver: pick the best feasible schedule (or report infeasible)
# ---------------------------------------------------------------------------


def _best_schedule(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credits: dict[date, int] | None = None,
) -> tuple[str, list[ScheduleRow]] | None:
    """Return (pay_shape_used, rows) for the best feasible schedule, or None."""
    total = offer_total_cents(offer)
    fee = program_fee_cents(offer, rules)
    cad = cadence_dates(offer, client)
    if not cad:
        return None
    kmax = min(rules.max_payments, rules.max_terms, len(cad))
    if kmax < 1:
        return None

    def pick_best(shape: str, candidates) -> tuple[str, _Evaluated] | None:
        best: _Evaluated | None = None
        for payments in candidates:
            ev = _evaluate_vector(client, offer, rules, cad, payments, fee, extra_credits)
            if ev is None:
                continue
            # Objective: collect fee as early as possible => lexicographically
            # MAX fee-per-date vector; tie-break: fewer total bank fees.
            key = (ev.fee_vector, -ev.total_bank_fee)
            if best is None or key > (best.fee_vector, -best.total_bank_fee):
                best = ev
        return (shape, best) if best is not None else None

    # Shape selection follows the creditor flags (see README).
    if rules.even_pays:
        chosen = pick_best("even", _even_candidates(total, kmax, rules))
    elif rules.is_ballooning_allowed:
        # Prefer a balloon (it defers the most, best serving the objective);
        # fall back to a staircase only if no balloon is feasible.
        chosen = pick_best("balloon", _balloon_candidates(total, kmax, rules))
        if chosen is None:
            chosen = pick_best("staircase", _staircase_candidates(total, kmax, rules))
    else:
        chosen = pick_best("staircase", _staircase_candidates(total, kmax, rules))

    if chosen is None:
        return None
    shape, ev = chosen
    return shape, ev.rows


def _is_feasible(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credits: dict[date, int] | None = None,
) -> bool:
    return _best_schedule(client, offer, rules, extra_credits) is not None


# ---------------------------------------------------------------------------
# Part 2: minimum additional funds
# ---------------------------------------------------------------------------


def _future_draft_dates(client: Client) -> list[date]:
    """Dates of drafts (credit entries) landing strictly after as_of_date."""
    return [e.date for e in client.ledger if e.type == "credit" and e.date > client.as_of_date]


def _funding_upper_bound(offer: Offer, rules: CreditorRules, client: Client) -> int:
    total = offer_total_cents(offer)
    fee = program_fee_cents(offer, rules)
    cad = cadence_dates(offer, client)
    kmax = max(1, min(rules.max_payments, rules.max_terms, len(cad) or 1))
    debits = sum(e.amount_cents for e in client.ledger if e.type == "debit" and e.date > client.as_of_date)
    return total + fee + kmax * rules.bank_fee_cents + debits + 1


def _min_lump_sum(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """Smallest single extra credit (placed as early as useful) that makes it feasible."""
    # Place the lump as early as possible: earlier money is weakly more useful, so
    # the earliest sensible date minimizes the required amount.
    lump_date = max(client.first_draft_date, client.as_of_date + timedelta(days=1))
    hi = _funding_upper_bound(offer, rules, client)

    guardrail_cap = round_half_up(Decimal("0.65") * Decimal(offer_total_cents(offer)))

    if not _is_feasible(client, offer, rules, {lump_date: hi}):
        return FundsOption(
            amount_cents=hi,
            within_guardrail=False,
            reason="no feasible schedule exists at any lump sum (structural)",
            date=lump_date,
        )

    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_feasible(client, offer, rules, {lump_date: mid}):
            hi = mid
        else:
            lo = mid + 1
    amount = lo
    within = amount <= guardrail_cap
    reason = "" if within else f"lump sum {amount} exceeds guardrail {guardrail_cap} (0.65 * offer_total)"
    return FundsOption(amount_cents=amount, within_guardrail=within, reason=reason, date=lump_date)


def _min_monthly_increment(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    """Smallest uniform amount added to every future draft that makes it feasible."""
    future_dates = _future_draft_dates(client)
    n = len(future_dates)
    guardrail_cap = max(10000, round_half_up(Decimal("0.40") * Decimal(client.draft_amount_cents)))

    def with_increment(x: int) -> Client:
        c = copy.deepcopy(client)
        new_ledger = []
        for e in c.ledger:
            if e.type == "credit" and e.date > c.as_of_date:
                new_ledger.append(LedgerEntry(e.date, e.amount_cents + x, e.type))
            else:
                new_ledger.append(e)
        c.ledger = new_ledger
        return c

    if n == 0:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no future drafts to increment",
            num_drafts=0,
        )

    hi = _funding_upper_bound(offer, rules, client)  # per-draft is more than enough
    if not _is_feasible(with_increment(hi), offer, rules):
        return FundsOption(
            amount_cents=hi,
            within_guardrail=False,
            reason="no feasible schedule exists at any monthly increment (structural)",
            num_drafts=n,
        )

    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if _is_feasible(with_increment(mid), offer, rules):
            hi = mid
        else:
            lo = mid + 1
    amount = lo
    within = amount <= guardrail_cap
    reason = (
        ""
        if within
        else f"increment {amount} exceeds guardrail {guardrail_cap} "
        f"(max(10000, 0.40 * draft_amount))"
    )
    return FundsOption(amount_cents=amount, within_guardrail=within, reason=reason, num_drafts=n)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification."""
    best = _best_schedule(client, offer, rules)
    if best is not None:
        shape, rows = best
        return Result(feasible=True, pay_shape_used=shape, schedule=rows, additional_funds=None)

    additional = AdditionalFunds(
        lump_sum=_min_lump_sum(client, offer, rules),
        monthly_increment=_min_monthly_increment(client, offer, rules),
    )
    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=additional,
    )
