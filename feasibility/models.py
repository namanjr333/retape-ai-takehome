"""Data models, JSON loaders, and date helpers for the feasibility take-home.

Everything money-related is in integer cents. Dates are ``datetime.date``.
You are free to use, ignore, or rewrite anything in this file. The only hard
requirements are the input/output shapes described in ASSIGNMENT.md.
"""

from __future__ import annotations

import json
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

EntryType = Literal["credit", "debit"]


@dataclass(frozen=True)
class LedgerEntry:
    date: date
    amount_cents: int
    type: EntryType


@dataclass
class Client:
    draft_amount_cents: int
    draft_day: int
    first_draft_date: date
    last_draft_date: date
    as_of_date: date
    current_balance_cents: int
    ledger: list[LedgerEntry] = field(default_factory=list)


@dataclass
class Offer:
    creditor: str
    # The creditor's outstanding balance. Per ASSIGNMENT.md this field is named
    # `creditor_balance_cents` (the client's SDA balance keeps `current_balance_cents`).
    # The loader also accepts the legacy `current_balance_cents` spelling.
    creditor_balance_cents: int
    original_balance_cents: int
    settlement_pct: float
    # Optional. When omitted, default to the end of the month of first_draft_date
    # (see default_first_payment_date()).
    first_payment_date: date | None = None


@dataclass
class CreditorRules:
    max_terms: int
    max_payments: int
    min_payment_cents: int
    max_token_pays: int
    min_payment_tiers: list[tuple[int, int]]  # [(from_payment_1based, min_cents), ...]
    # Two independent creditor flags (both default False):
    #   even_pays            -> every creditor payment must be equal (ballooning is irrelevant).
    #   is_ballooning_allowed -> the final payment may absorb the remainder (a "balloon").
    # When NOT ballooning (and not even), the payment structure is bounded to at most
    # `max_segments` distinct payment levels so it can't fan out into an arbitrarily
    # complex staircase. The actual shape is whatever the objective produces
    # (maximize fee collected upfront / keep creditor payments low early).
    even_pays: bool
    is_ballooning_allowed: bool
    max_segments: int
    bank_fee_cents: int
    program_fee_pct: float


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def end_of_month(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def is_end_of_month(d: date) -> bool:
    return d.day == monthrange(d.year, d.month)[1]


def add_months(d: date, n: int) -> date:
    """Shift a date by ``n`` whole months, clamping the day to month length."""
    total = (d.year * 12 + (d.month - 1)) + n
    year, month = divmod(total, 12)
    month += 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def default_first_payment_date(client: Client) -> date:
    """Default creditor first-payment date: end of the first draft's month (EOM)."""
    return end_of_month(client.first_draft_date)


def monthly_payment_dates(start: date, count: int) -> list[date]:
    """Generate ``count`` monthly dates from ``start``.

    If ``start`` is the last day of its month, every generated date is the last
    day of its month (true EOM cadence). Otherwise the day-of-month is preserved
    (clamped to month length).
    """
    if count <= 0:
        return []
    eom = is_end_of_month(start)
    out: list[date] = []
    for i in range(count):
        d = add_months(start, i)
        out.append(end_of_month(d) if eom else d)
    return out


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _d(s: str) -> date:
    return date.fromisoformat(s)


def load_client(path: str | Path) -> Client:
    raw = json.loads(Path(path).read_text())
    return Client(
        draft_amount_cents=int(raw["draft_amount_cents"]),
        draft_day=int(raw["draft_day"]),
        first_draft_date=_d(raw["first_draft_date"]),
        last_draft_date=_d(raw["last_draft_date"]),
        as_of_date=_d(raw["as_of_date"]),
        current_balance_cents=int(raw["current_balance_cents"]),
        ledger=[
            LedgerEntry(_d(e["date"]), int(e["amount_cents"]), e["type"])
            for e in raw.get("ledger", [])
        ],
    )


def load_offer(path: str | Path) -> Offer:
    raw = json.loads(Path(path).read_text())
    fpd = raw.get("first_payment_date")
    # Accept the spec field name `creditor_balance_cents` and fall back to the
    # legacy `current_balance_cents` so older case files still load.
    if "creditor_balance_cents" in raw:
        creditor_balance = int(raw["creditor_balance_cents"])
    else:
        creditor_balance = int(raw["current_balance_cents"])
    return Offer(
        creditor=raw["creditor"],
        creditor_balance_cents=creditor_balance,
        original_balance_cents=int(raw["original_balance_cents"]),
        settlement_pct=float(raw["settlement_pct"]),
        first_payment_date=_d(fpd) if fpd else None,
    )


def load_creditor_rules(path: str | Path) -> CreditorRules:
    raw = json.loads(Path(path).read_text())
    return CreditorRules(
        max_terms=int(raw["max_terms"]),
        max_payments=int(raw["max_payments"]),
        min_payment_cents=int(raw["min_payment_cents"]),
        max_token_pays=int(raw["max_token_pays"]),
        min_payment_tiers=[(int(a), int(b)) for a, b in raw.get("min_payment_tiers", [])],
        even_pays=bool(raw.get("even_pays", False)),
        is_ballooning_allowed=bool(raw.get("is_ballooning_allowed", False)),
        max_segments=int(raw.get("max_segments", 4)),
        bank_fee_cents=int(raw["bank_fee_cents"]),
        program_fee_pct=float(raw["program_fee_pct"]),
    )


def load_case(case_dir: str | Path) -> tuple[Client, Offer, CreditorRules]:
    p = Path(case_dir)
    return (
        load_client(p / "client.json"),
        load_offer(p / "offer.json"),
        load_creditor_rules(p / "creditor_rules.json"),
    )


def round_half_up(value) -> int:
    """Round to the nearest integer, ties away from zero.

    Implemented explicitly with ``Decimal`` because the spec requires round-half-up
    and Python's built-in ``round`` uses banker's rounding (half-to-even), e.g.
    ``round(0.5 * 12345)`` is ``6172`` where the spec wants ``6173``.
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def offer_total_cents(offer: Offer) -> int:
    """What we must pay the creditor = round(settlement_pct x creditor_balance_cents)."""
    return round_half_up(Decimal(str(offer.settlement_pct)) * Decimal(offer.creditor_balance_cents))


def program_fee_cents(offer: Offer, rules: CreditorRules) -> int:
    """Total program fee = round(program_fee_pct x original_balance_cents)."""
    return round_half_up(
        Decimal(str(rules.program_fee_pct)) * Decimal(offer.original_balance_cents)
    )
