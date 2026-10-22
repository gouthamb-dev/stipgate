"""Synthetic trailing-document builders shared by the benchmark, the tests and the web API.

All borrower, employer and account data is fictitious. Dates are generated relative to an
``as_of`` reference date so the statutory recency windows hold whenever the samples are built.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

__all__ = [
    "BenchmarkDoc",
    "paystub_text",
    "w2_text",
    "utility_text",
    "bank_statement_text",
    "corrupt_ocr",
    "build_core_documents",
    "core_samples",
]


@dataclass(frozen=True)
class BenchmarkDoc:
    document_id: str
    category: str
    condition_code: str
    snippet: str
    expected_action: str
    ground_truth_compliant: bool


def _money(value: float) -> str:
    return f"${value:,.2f}"


def paystub_text(
    borrower: str,
    employer: str,
    period_end: date,
    hourly_rate: float,
    hours: float = 80.0,
    ytd_periods: int = 12,
    *,
    include_employer: bool = True,
    include_ytd: bool = True,
) -> str:
    gross = round(hourly_rate * hours, 2)
    federal = round(gross * 0.12, 2)
    deductions = round(gross * 0.125, 2)
    net = round(gross - federal - deductions, 2)
    lines = ["EARNINGS STATEMENT"]
    if include_employer:
        lines.append(f"Employer: {employer}")
    lines += [
        f"Employee: {borrower}",
        f"Pay Period Start: {(period_end - timedelta(days=13)):%m/%d/%Y}",
        f"Pay Period End: {period_end:%m/%d/%Y}",
        f"Pay Date: {(period_end + timedelta(days=3)):%m/%d/%Y}",
        f"Hours Worked: {hours:.2f}    Rate: {_money(hourly_rate)}",
        f"Gross Pay: {_money(gross)}",
        f"Federal Withholding: {_money(federal)}",
        f"Deductions (Retirement, Medical): {_money(deductions)}",
        f"Net Pay: {_money(net)}",
    ]
    if include_ytd:
        lines.append(f"YTD Gross: {_money(gross * ytd_periods)}")
    return "\n".join(lines)


def w2_text(borrower: str, employer: str, ein: str, wages_by_year: dict[int, float]) -> str:
    blocks: list[str] = []
    for year in sorted(wages_by_year, reverse=True):
        wages = wages_by_year[year]
        blocks.append(
            "\n".join(
                [
                    f"{year} Form W-2 Wage and Tax Statement    OMB No. 1545-0008",
                    f"Tax Year {year}",
                    f"Employer: {employer}",
                    f"Employer identification number (EIN): {ein}",
                    f"Employee: {borrower}",
                    f"Box 1 Wages, tips, other compensation: {_money(wages)}",
                    f"Box 2 Federal income tax withheld: {_money(wages * 0.12)}",
                    f"Box 3 Social security wages: {_money(wages)}",
                    f"Box 5 Medicare wages and tips: {_money(wages)}",
                ]
            )
        )
    return "\n\n".join(blocks)


def utility_text(borrower: str, as_of: date, amount: float, kwh: int) -> str:
    return "\n".join(
        [
            "Pacific Northwest Electric and Gas Utility",
            "Residential Billing Summary",
            f"Customer: {borrower}",
            "Service Address: 1842 Alder Creek Rd, Tacoma, WA 98402",
            "Meter Number: 55102937",
            f"Billing Period: {(as_of - timedelta(days=34)):%m/%d/%Y} to {(as_of - timedelta(days=4)):%m/%d/%Y}",
            f"Electricity Usage: {kwh} kWh",
            "Natural Gas Service: 46 therms",
            f"Water and Sewer: {_money(amount * 0.27)}",
            f"Total Amount Due: {_money(amount)}",
            f"Due Date: {(as_of + timedelta(days=17)):%m/%d/%Y}",
        ]
    )


def bank_statement_text(borrower: str, as_of: date, opening: float, deposits: float, withdrawals: float) -> str:
    return "\n".join(
        [
            "Evergreen Federal Credit Union - Account Statement",
            f"Account Holder: {borrower}",
            f"Statement Period End: {(as_of - timedelta(days=6)):%m/%d/%Y}",
            f"Beginning Balance: {_money(opening)}",
            f"Deposits and Credits: {_money(deposits)}",
            f"Withdrawals and Debits: {_money(withdrawals)}",
            f"Ending Balance: {_money(opening + deposits - withdrawals)}",
            "Routing Number: 325081403",
        ]
    )


_OCR_SUBSTITUTIONS: Final = {"o": "0", "O": "0", "i": "1", "I": "1", "e": "3", "E": "3", "a": "@", "s": "5", "S": "5"}
_OCR_GARBAGE: Final = "@#~^*|\ufffd"


def corrupt_ocr(text: str, rng: random.Random, rate: float = 0.35) -> str:
    """Simulate a damaged OCR scan: glyph substitutions, garbage bytes and truncation."""
    out: list[str] = []
    for ch in text:
        if ch in _OCR_SUBSTITUTIONS and rng.random() < rate:
            out.append(_OCR_SUBSTITUTIONS[ch])
        else:
            out.append(ch)
        if rng.random() < 0.04:
            out.append(rng.choice(_OCR_GARBAGE))
    cut = max(80, int(len(out) * rng.uniform(0.45, 0.7)))
    return "".join(out[:cut]) + "\n[OCR TRUNCATED]"


def build_core_documents(as_of: date) -> list[BenchmarkDoc]:
    """The four representative documents for the default benchmark run."""
    year = as_of.year
    corrupted = (
        "EARN1NGS STAT3M3NT  P@y P3ri0d End: 0?/1?/" + str(year) + " \ufffd\ufffd\n"
        "Empl0y33: M@r1a Ch3n  ~~#@~ Empl0y3r: C@5c@d3 Fr31ght\n"
        "Gr0ss P@y: $3,3?0.0| Fed3r@l W1thh0ld1ng: $#9?.00 ^^ N3t P@y: \ufffd\ufffd\ufffd\n"
        "YTD Gr0ss: $39,6..\n[OCR TRUNCATED]"
    )
    return [
        BenchmarkDoc(
            "SG-001",
            "Valid 30-day Paystub",
            "STIP_INCOME_30DAY",
            paystub_text("Maria L. Chen", "Cascade Freight Logistics LLC", as_of - timedelta(days=5), 41.25),
            "PROCEED",
            True,
        ),
        BenchmarkDoc(
            "SG-002",
            "Unrelated Utility Bill",
            "STIP_INCOME_30DAY",
            utility_text("Maria L. Chen", as_of, 214.67, 812),
            "HALTED",
            False,
        ),
        BenchmarkDoc(
            "SG-003",
            "Valid Multi-Year W-2",
            "STIP_W2_2YR",
            w2_text(
                "Maria L. Chen",
                "Cascade Freight Logistics LLC",
                "12-3456789",
                {year - 1: 81240.00, year - 2: 78900.00},
            ),
            "PROCEED",
            True,
        ),
        BenchmarkDoc(
            "SG-004",
            "Truncated/Corrupted OCR Scan",
            "STIP_INCOME_30DAY",
            corrupted,
            "HALTED",
            False,
        ),
    ]


def core_samples(as_of: date | None = None) -> list[dict[str, str]]:
    """The four core benchmark documents as JSON-ready dicts (used by the web API and UI)."""
    reference = as_of if as_of is not None else date.today()
    return [
        {
            "document_id": doc.document_id,
            "category": doc.category,
            "condition_code": doc.condition_code,
            "snippet": doc.snippet,
            "expected_action": doc.expected_action,
        }
        for doc in build_core_documents(reference)
    ]
