"""Example expectations for the four provided cases.

These FAIL until you implement feasibility/engine.py::evaluate_offer. Treat them
as the minimum bar — your own test suite should go well beyond these. They do not
pin an exact schedule (several valid schedules may exist); they assert the
verdict, the pay shape, and the Part 2 minima.
"""

from __future__ import annotations

from datetime import date

import pytest

from feasibility.engine import evaluate_offer
from feasibility.models import load_case


def _run(case: str):
    client, offer, rules = load_case(f"cases/{case}")
    return evaluate_offer(client, offer, rules)


def test_case1_feasible_even():
    r = _run("case1_feasible_even")
    assert r.feasible is True
    assert r.pay_shape_used == "even"
    assert r.schedule is not None
    # balance must never go negative
    assert all(row.balance_cents >= 0 for row in r.schedule)


def test_case2_infeasible_minima():
    r = _run("case2_infeasible_minima")
    assert r.feasible is False
    af = r.additional_funds
    assert af is not None
    assert af.lump_sum.amount_cents == 10000
    assert af.lump_sum.within_guardrail is True
    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5
    assert af.monthly_increment.within_guardrail is True


def test_case3_requires_balloon():
    r = _run("case3_balloon")
    assert r.feasible is True
    # this creditor allows ballooning; the solver defers payment into a final balloon
    assert r.pay_shape_used == "balloon"


def test_case4_tiered_minimums():
    r = _run("case4_tiers")
    assert r.feasible is True
    assert r.pay_shape_used == "staircase"
    # payments 7+ must respect the $50 tier floor
    payments = [row.creditor_payment_cents for row in r.schedule if row.creditor_payment_cents > 0]
    assert all(p >= 5000 for p in payments[6:])
