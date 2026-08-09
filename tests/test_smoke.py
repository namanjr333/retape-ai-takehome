"""Sanity tests for the provided scaffolding. These pass out of the box."""

from __future__ import annotations

from datetime import date

from feasibility.models import (
    default_first_payment_date,
    end_of_month,
    is_end_of_month,
    load_case,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)
from feasibility.engine import FundsOption, Result


def test_loaders_parse_case1():
    client, offer, rules = load_case("cases/case1_feasible_even")
    assert client.draft_amount_cents == 20000
    assert client.last_draft_date == date(2026, 7, 1)
    assert len(client.ledger) == 7
    assert offer.first_payment_date == date(2026, 1, 31)
    assert rules.even_pays is True
    assert rules.is_ballooning_allowed is False
    assert offer_total_cents(offer) == 50000
    assert program_fee_cents(offer, rules) == 30000


def test_eom_helpers():
    assert end_of_month(date(2026, 2, 10)) == date(2026, 2, 28)
    assert is_end_of_month(date(2026, 1, 31))
    assert not is_end_of_month(date(2026, 1, 30))


def test_default_first_payment_is_eom():
    client, _, _ = load_case("cases/case1_feasible_even")
    assert default_first_payment_date(client) == date(2026, 1, 31)


def test_monthly_cadence_follows_eom():
    dates = monthly_payment_dates(date(2026, 1, 31), 4)
    assert dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_monthly_cadence_preserves_day():
    dates = monthly_payment_dates(date(2026, 1, 15), 3)
    assert dates == [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)]


def test_result_serialization_roundtrip():
    r = Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=None,
    )
    d = r.to_dict()
    assert d["feasible"] is False
    assert d["schedule"] is None
    assert d["additional_funds"] is None
