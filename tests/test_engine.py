"""Focused unit tests beyond the four provided cases.

These build inputs directly from the dataclasses so each test isolates one rule.
Coverage (per ASSIGNMENT.md section 10):
  * even / staircase / balloon shapes
  * token-pay and tier floors
  * the max_segments cap
  * exact-sum
  * date-by-date simulation: same-day credits-before-debits, and a balance that
    hits exactly $0
  * the horizon limit
  * fee compliance: no program fee before the first creditor payment
  * both Part 2 minima (lump sum and monthly increment) + guardrails
  * round-half-up (not banker's rounding)
"""

from __future__ import annotations

from datetime import date

import pytest

from feasibility.engine import (
    evaluate_offer,
    offer_total_cents,
    program_fee_cents,
    position_floor,
    round_half_up,
)
from feasibility.models import Client, CreditorRules, LedgerEntry, Offer


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def drafts(amount, day, first_year_month, count, as_of=date(2025, 12, 31)):
    """Monthly credit entries on the 1st, `count` of them starting first_year_month."""
    y, m = first_year_month
    ledger = []
    dates = []
    for _ in range(count):
        d = date(y, m, day)
        ledger.append(LedgerEntry(d, amount, "credit"))
        dates.append(d)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return ledger, dates


def make_client(amount=10000, count=6, day=1, extra_ledger=None, as_of=date(2025, 12, 31)):
    ledger, ds = drafts(amount, day, (2026, 1), count, as_of)
    if extra_ledger:
        ledger = ledger + extra_ledger
    return Client(
        draft_amount_cents=amount,
        draft_day=day,
        first_draft_date=ds[0],
        last_draft_date=ds[-1],
        as_of_date=as_of,
        current_balance_cents=0,
        ledger=ledger,
    )


def make_offer(creditor_balance=60000, original=60000, pct=0.5, fpd=date(2026, 1, 31)):
    return Offer(
        creditor="Test",
        creditor_balance_cents=creditor_balance,
        original_balance_cents=original,
        settlement_pct=pct,
        first_payment_date=fpd,
    )


def make_rules(**kw):
    base = dict(
        max_terms=12,
        max_payments=12,
        min_payment_cents=2500,
        max_token_pays=6,
        min_payment_tiers=[],
        even_pays=False,
        is_ballooning_allowed=False,
        max_segments=4,
        bank_fee_cents=0,
        program_fee_pct=0.0,
    )
    base.update(kw)
    return CreditorRules(**base)


def sched_sum(result):
    return sum(r.creditor_payment_cents for r in result.schedule)


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------


def test_round_half_up_ties_away_from_zero():
    assert round_half_up(0.5) == 1
    assert round_half_up(1.5) == 2  # banker's rounding would give 2 as well
    assert round_half_up(2.5) == 3  # banker's rounding gives 2 -> would be wrong
    assert round_half_up(0.5 * 12345) == 6173  # python round() gives 6172


def test_offer_total_and_fee_use_round_half_up():
    # 0.5 * 12345 = 6172.5 -> 6173 (away from zero), not 6172 (banker's)
    offer = make_offer(creditor_balance=12345, original=12345, pct=0.5)
    assert offer_total_cents(offer) == 6173
    rules = make_rules(program_fee_pct=0.5)
    assert program_fee_cents(offer, rules) == 6173


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def test_even_shape_all_equal_or_plus_one():
    rules = make_rules(even_pays=True, max_segments=1)
    client = make_client(amount=20000, count=6)
    offer = make_offer(creditor_balance=100000, original=120000, pct=0.5)  # total 50000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible and r.pay_shape_used == "even"
    pays = [row.creditor_payment_cents for row in r.schedule]
    assert max(pays) - min(pays) <= 1  # "as equal as possible"
    assert sum(pays) == 50000


def test_balloon_shape_defers_to_final_payment():
    rules = make_rules(is_ballooning_allowed=True, min_payment_cents=2500)
    client = make_client(amount=10000, count=7)
    offer = make_offer(creditor_balance=60000, original=60000, pct=0.5)  # total 30000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible and r.pay_shape_used == "balloon"
    pays = [row.creditor_payment_cents for row in r.schedule]
    # non-decreasing and final payment is the largest (the balloon)
    assert pays == sorted(pays)
    assert pays[-1] == max(pays) and pays[-1] > pays[0]
    assert sum(pays) == 30000


def test_staircase_shape_when_no_flags():
    rules = make_rules(max_segments=2, program_fee_pct=0.2)
    client = make_client(amount=10000, count=13)
    offer = make_offer(creditor_balance=150000, original=150000, pct=0.4)  # total 60000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible and r.pay_shape_used == "staircase"
    pays = [row.creditor_payment_cents for row in r.schedule]
    assert pays == sorted(pays)  # non-decreasing


# ---------------------------------------------------------------------------
# Floors: token pays and tiers
# ---------------------------------------------------------------------------


def test_position_floor_token_and_tier():
    rules = make_rules(min_payment_cents=2500, max_token_pays=3, min_payment_tiers=[(5, 5000)])
    assert position_floor(1, rules) == 2500
    assert position_floor(3, rules) == 2500  # last token pay
    assert position_floor(4, rules) == 2501  # beyond token cap -> must exceed base min
    assert position_floor(5, rules) == 5000  # tier dominates


def test_token_pay_cap_limits_payments_at_base_min():
    # Only 2 payments may sit at the base min; the rest must exceed it.
    rules = make_rules(min_payment_cents=2500, max_token_pays=2, max_segments=4)
    client = make_client(amount=10000, count=8)
    offer = make_offer(creditor_balance=40000, original=40000, pct=0.5)  # total 20000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    at_base = [p.creditor_payment_cents for p in r.schedule if p.creditor_payment_cents == 2500]
    assert len(at_base) <= 2


def test_tier_floor_enforced_from_position():
    rules = make_rules(min_payment_cents=2500, max_token_pays=6,
                       min_payment_tiers=[(7, 5000)], max_segments=2)
    client = make_client(amount=10000, count=13)
    offer = make_offer(creditor_balance=150000, original=150000, pct=0.4)  # total 60000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    pays = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert all(p >= 5000 for p in pays[6:])  # payments 7+ respect the tier


# ---------------------------------------------------------------------------
# max_segments cap
# ---------------------------------------------------------------------------


def test_max_segments_caps_distinct_levels():
    rules = make_rules(max_segments=2, min_payment_cents=2500)
    client = make_client(amount=10000, count=12)
    offer = make_offer(creditor_balance=120000, original=120000, pct=0.5)  # total 60000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    pays = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert len(set(pays)) <= 2


# ---------------------------------------------------------------------------
# Exact sum
# ---------------------------------------------------------------------------


def test_creditor_payments_sum_exactly_to_offer_total():
    for pct, bal in [(0.4, 150000), (0.5, 100000), (0.5, 12345)]:
        rules = make_rules(max_segments=3)
        client = make_client(amount=30000, count=12)
        offer = make_offer(creditor_balance=bal, original=bal, pct=pct)
        r = evaluate_offer(client, offer, rules)
        if r.feasible:
            assert sched_sum(r) == offer_total_cents(offer)


# ---------------------------------------------------------------------------
# Simulation: same-day ordering and exact-zero balance
# ---------------------------------------------------------------------------


def test_same_day_credits_before_debits():
    # A draft and a payment land on the SAME date. With credits-first the balance
    # never dips negative; if debits came first it would.
    fpd = date(2026, 1, 1)  # payment cadence coincides with draft day (day 1)
    rules = make_rules(min_payment_cents=2500, program_fee_pct=0.0)
    client = make_client(amount=10000, count=6, day=1)
    # first_payment on the 1st -> same day as drafts every month
    offer = make_offer(creditor_balance=20000, original=20000, pct=0.5, fpd=fpd)  # total 10000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert all(row.balance_cents >= 0 for row in r.schedule)


def test_balance_hits_exactly_zero():
    # Tight even case engineered so the running balance is exactly 0 early.
    rules = make_rules(even_pays=True, max_segments=1, bank_fee_cents=0, program_fee_pct=0.5)
    # 7 drafts -> 6 usable cadence dates (Jan31..Jun30); the Jul 1 draft is past
    # the last cadence date. total = 30000, fee = 30000 -> the 6 usable drafts
    # (60000) are consumed exactly, driving the balance to 0.
    client = make_client(amount=10000, count=7)
    offer = make_offer(creditor_balance=60000, original=60000, pct=0.5)
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert any(row.balance_cents == 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# Horizon limit
# ---------------------------------------------------------------------------


def test_nothing_scheduled_past_horizon():
    rules = make_rules(max_segments=3)
    client = make_client(amount=10000, count=5)  # horizon = 2026-05-01
    offer = make_offer(creditor_balance=40000, original=40000, pct=0.5)  # total 20000
    r = evaluate_offer(client, offer, rules)
    if r.feasible:
        assert all(row.date <= client.last_draft_date for row in r.schedule)


def test_first_payment_after_horizon_is_infeasible():
    rules = make_rules()
    client = make_client(amount=10000, count=3)  # horizon 2026-03-01
    offer = make_offer(creditor_balance=20000, original=20000, pct=0.5,
                       fpd=date(2026, 6, 30))  # after horizon
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False


# ---------------------------------------------------------------------------
# Fee compliance: no fee before the first creditor payment
# ---------------------------------------------------------------------------


def test_no_program_fee_before_first_payment():
    rules = make_rules(program_fee_pct=0.25, max_segments=2)
    client = make_client(amount=20000, count=6)
    offer = make_offer(creditor_balance=100000, original=120000, pct=0.5)
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    first_payment_date = min(
        row.date for row in r.schedule if row.creditor_payment_cents > 0
    )
    for row in r.schedule:
        if row.program_fee_cents > 0:
            assert row.date >= first_payment_date


# ---------------------------------------------------------------------------
# Part 2 minima
# ---------------------------------------------------------------------------


def test_infeasible_reports_both_minima():
    rules = make_rules(max_terms=4, max_payments=4, min_payment_cents=2500,
                       max_token_pays=4, max_segments=3, program_fee_pct=0.125)
    client = make_client(amount=10000, count=5)  # horizon 2026-05-01
    offer = make_offer(creditor_balance=80000, original=80000, pct=0.5)  # total 40000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    af = r.additional_funds
    assert af.lump_sum.amount_cents == 10000
    assert af.lump_sum.date == date(2026, 1, 1)
    assert af.lump_sum.within_guardrail is True
    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5
    assert af.monthly_increment.within_guardrail is True


def test_lump_sum_makes_it_feasible():
    # Verify the reported lump actually flips feasibility (and one cent less doesn't).
    from feasibility.engine import _is_feasible
    rules = make_rules(max_terms=4, max_payments=4, max_segments=3, program_fee_pct=0.125)
    client = make_client(amount=10000, count=5)
    offer = make_offer(creditor_balance=80000, original=80000, pct=0.5)
    r = evaluate_offer(client, offer, rules)
    L = r.additional_funds.lump_sum.amount_cents
    d = r.additional_funds.lump_sum.date
    assert _is_feasible(client, offer, rules, {d: L})
    assert not _is_feasible(client, offer, rules, {d: L - 1})


def test_monthly_increment_guardrail_rejects_large_x():
    # Make the shortfall huge so the required increment blows past the guardrail.
    rules = make_rules(max_terms=2, max_payments=2, min_payment_cents=2500,
                       max_token_pays=2, max_segments=2, program_fee_pct=0.0)
    client = make_client(amount=1000, count=2)  # tiny drafts, horizon 2026-02-01
    offer = make_offer(creditor_balance=200000, original=200000, pct=0.5)  # total 100000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible is False
    # guardrail = max(10000, 0.40*1000=400) = 10000; required X is far larger
    assert r.additional_funds.monthly_increment.within_guardrail is False
    assert r.additional_funds.monthly_increment.reason != ""


# ---------------------------------------------------------------------------
# ASSIGNMENT sec.6 worked micro-example (fee fully front-loaded)
# ---------------------------------------------------------------------------


def test_assignment_worked_micro_example():
    # Horizon = 3 cadence dates; $100 lands before each date; start $0;
    # offer_total = $250, program_fee = $50, bank_fee = $0, flat min $25.
    # The spec's valid schedule [$50, $100, $100] collects the full $50 fee on the
    # first date. We assert feasibility, exact sum, and full fee on date 1.
    ledger = [
        LedgerEntry(date(2026, 1, 1), 10000, "credit"),
        LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        LedgerEntry(date(2026, 3, 1), 10000, "credit"),
    ]
    client = Client(
        draft_amount_cents=10000,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=date(2026, 3, 1),
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=ledger,
    )
    # payments land on the 1st (same day as drafts) so exactly $100 is available
    # before each of the 3 payments.
    offer = make_offer(creditor_balance=50000, original=25000, pct=0.5,
                       fpd=date(2026, 1, 1))  # total = 25000
    rules = make_rules(min_payment_cents=2500, max_token_pays=3, max_segments=3,
                       bank_fee_cents=0, program_fee_pct=0.20)  # fee = 5000
    r = evaluate_offer(client, offer, rules)
    assert r.feasible
    assert sched_sum(r) == 25000
    first = r.schedule[0]
    assert first.date == date(2026, 1, 1)
    assert first.program_fee_cents == 5000  # full fee front-loaded onto date 1
    assert all(row.balance_cents >= 0 for row in r.schedule)


# ---------------------------------------------------------------------------
# Loader accepts both the spec and legacy offer-balance field names
# ---------------------------------------------------------------------------


def test_loader_accepts_legacy_balance_field(tmp_path):
    import json
    from feasibility.models import load_offer

    new = tmp_path / "new.json"
    new.write_text(json.dumps({
        "creditor": "X", "creditor_balance_cents": 100000,
        "original_balance_cents": 120000, "settlement_pct": 0.5,
    }))
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({
        "creditor": "X", "current_balance_cents": 100000,
        "original_balance_cents": 120000, "settlement_pct": 0.5,
    }))
    assert load_offer(new).creditor_balance_cents == 100000
    assert load_offer(legacy).creditor_balance_cents == 100000


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
