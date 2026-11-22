# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The bank-detail filter of the Reading Desk (ADR-0139 §4, RD-D-24).

The IBANs below are the published example IBANs of their countries; the
account numbers are invented.
"""

from __future__ import annotations

import pytest

from services.scraper.bank_filter import (
    REPLACEMENT,
    asks_for_bank_details,
    filter_parsed_output,
    remove_bank_details,
)

_IBAN = "DE89 3704 0044 0532 0130 00"

# (input, expected output)
_REMOVED: list[tuple[str, str]] = [
    (_IBAN, REPLACEMENT),
    ("DE89370400440532013000", REPLACEMENT),
    ("NL91 ABNA 0417 1643 00", REPLACEMENT),
    ("CH93 0076 2011 6238 5295 7", REPLACEMENT),
    ("FR14 2004 1010 0505 0001 3M02 606", REPLACEMENT),
    (f"Text before {_IBAN} and after", f"Text before {REPLACEMENT} and after"),
    (f"AB12 CDEF GHIJ {_IBAN}", f"AB12 CDEF GHIJ {REPLACEMENT}"),
    (f"{_IBAN} {_IBAN}", f"{REPLACEMENT} {REPLACEMENT}"),
    ("Bankverbindung: DE89-3704-0044-0532-0130-00", f"Bankverbindung: {REPLACEMENT}"),
    (
        f"Please wire to IBAN {_IBAN}, BIC COBADEFFXXX by 15 March 2025.",
        f"Please wire to IBAN {REPLACEMENT}, BIC {REPLACEMENT} by 15 March 2025.",
    ),
    (
        "IBAN: GB82 WEST 1234 5698 7654 32 SWIFT: WESTGB2L",
        f"IBAN: {REPLACEMENT} SWIFT: {REPLACEMENT}",
    ),
    ("IBAN GB82WEST12345698765432", f"IBAN {REPLACEMENT}"),
    (f"IBAN\n{_IBAN}", f"IBAN\n{REPLACEMENT}"),
    (f"IBAN {_IBAN} Commerzbank AG", f"IBAN {REPLACEMENT} Commerzbank AG"),
    # A BIC directly after a removed identifier goes too.
    ("LU28 0019 4006 4475 0000 BGLLLULL", f"{REPLACEMENT} {REPLACEMENT}"),
    (f"IBAN {_IBAN} / COBADEFFXXX", f"IBAN {REPLACEMENT} / {REPLACEMENT}"),
    # After a bank term the check digits do not matter (…01 is invalid).
    (
        "IBAN DE89 3704 0044 0532 0130 01 BIC COBADEFFXXX",
        f"IBAN {REPLACEMENT} BIC {REPLACEMENT}",
    ),
    ("Account No./IBAN DE89370400440532013001", f"Account No./IBAN {REPLACEMENT}"),
    (f"Account No. / IBAN: {_IBAN}", f"Account No. / IBAN: {REPLACEMENT}"),
    (f"Account: {_IBAN}", f"Account: {REPLACEMENT}"),
    ("SWIFT/BIC: COBADEFFXXX", f"SWIFT/BIC: {REPLACEMENT}"),
    ("BIC/SWIFT COBADEFF", f"BIC/SWIFT {REPLACEMENT}"),
    ("Swift Code COBADEFFXXX", f"Swift Code {REPLACEMENT}"),
    ("Account number: 000123456789", f"Account number: {REPLACEMENT}"),
    (
        "Account No.: 70872490, Sort Code: 40-47-84",
        f"Account No.: {REPLACEMENT}, Sort Code: {REPLACEMENT}",
    ),
    ("Account No. 123 456 789", f"Account No. {REPLACEMENT}"),
    ("account #000123456789", f"account #{REPLACEMENT}"),
    ("Payment to account 12 345 678 9012", f"Payment to account {REPLACEMENT}"),
    (
        "ABA routing number 021000021; Account #: 4426123456",
        f"ABA routing number {REPLACEMENT}; Account #: {REPLACEMENT}",
    ),
    ("Routing No. 021000021", f"Routing No. {REPLACEMENT}"),
    ("Sort-Code 404784", f"Sort-Code {REPLACEMENT}"),
    ("sort code: 40 47 84", f"sort code: {REPLACEMENT}"),
    ("acct 12345678", f"acct {REPLACEMENT}"),
    ("A/C: 12-345678-9", f"A/C: {REPLACEMENT}"),
    (
        "Konto 0532013000 bei der Commerzbank, BLZ 370 400 44",
        f"Konto {REPLACEMENT} bei der Commerzbank, BLZ {REPLACEMENT}",
    ),
    ("Kontonummer 0532013000", f"Kontonummer {REPLACEMENT}"),
    ("Konto-Nr. 532 013 000", f"Konto-Nr. {REPLACEMENT}"),
    ("Kto.-Nr.: 532013000", f"Kto.-Nr.: {REPLACEMENT}"),
]

_KEPT: list[str] = [
    "",
    "1250000",
    "125000000.50",
    "-4750000",
    "12.5%",
    "2024-09-30",
    "Q3 2024",
    "NAV of EUR 125,000,000 as at 30 September 2024",
    "Called capital 87 500 000, distributions 12 250 000",
    "Fund size EUR 2,000,000,000; hard cap 2500000000",
    "Commitment 25000000, unfunded 8750000",
    "Vintage 2019, TVPI 1.45x, net IRR 14.2%",
    "Page 12, capital account statement",
    "Capital account 12 500 000",
    "capital account: 125000000",
    "capital account number 12345678",
    "The capital account stood at 12500000 at year end",
    "The distribution of 4 750 000 is paid to the account on 2025-03-15",
    "account 123 456 789",
    "account 2024",
    "Account name: Example Fund I SCSp",
    "The account manager changed in 2024",
    "Accounting policies are unchanged; accounts for 2024 were audited",
    "Kapitalkonto 12 500 000",
    "Kontoauszug vom 30.09.2024",
    "ISIN LU0123456789",
    "LU0123456789 12500000",
    "LEI 5493001KJTIIGC8Y1R12",
    "FY24 12 500 000 13 200 000 14 100 000",
    "FY24 RESULTS WERE STRONG ACROSS THE PORTFOLIO",
    "SWIFT transfer of the proceeds is expected in Q1",
    "BIC Capital Partners acquired the company",
    "Routing of capital calls through the feeder fund",
    "Tel. +49 511 12345678",
    # Not valid, and no bank term beside it.
    "DE89 3704 0044 0532 0130 01",
]


@pytest.mark.parametrize(("text", "expected"), _REMOVED)
def test_bank_details_are_removed(text: str, expected: str) -> None:
    assert remove_bank_details(text) == expected


@pytest.mark.parametrize("text", [expected for _, expected in _REMOVED])
def test_filtering_twice_changes_nothing(text: str) -> None:
    assert remove_bank_details(text) == text


@pytest.mark.parametrize("text", _KEPT)
def test_everything_else_survives(text: str) -> None:
    assert remove_bank_details(text) == text


def test_no_digit_of_the_account_survives() -> None:
    filtered = remove_bank_details(f"Wire EUR 4,750,000 to IBAN {_IBAN} (account 0532013000).")
    assert "4,750,000" in filtered
    assert "0532" not in filtered
    assert "3704" not in filtered


def test_filter_walks_the_parsed_output() -> None:
    parsed = {
        "fund_name": "Example Fund I",
        "page_count": 12,
        "fields": {"amount_lp": {"value": "4750000", "quote": f"to IBAN {_IBAN}", "page": None}},
        "note": {"highlights": ["Call of 4 750 000", f"Pay to {_IBAN}"], "outlook": 1.5},
    }

    filtered = filter_parsed_output(parsed)

    assert filtered == {
        "fund_name": "Example Fund I",
        "page_count": 12,
        "fields": {
            "amount_lp": {"value": "4750000", "quote": f"to IBAN {REPLACEMENT}", "page": None}
        },
        "note": {"highlights": ["Call of 4 750 000", f"Pay to {REPLACEMENT}"], "outlook": 1.5},
    }
    # A new structure: the input is not changed in place.
    assert _IBAN in parsed["fields"]["amount_lp"]["quote"]


@pytest.mark.parametrize(
    "name",
    [
        "IBAN",
        "BIC",
        "SWIFT code",
        "ABA",
        "Sort code",
        "Routing number",
        "Account number",
        "Account No.",
        "Bank account",
        "bank details",
        "Wire instructions",
        "Payment instructions",
        "Konto",
        "Kontonummer",
        "Bankverbindung",
    ],
)
def test_keyword_names_that_ask_for_bank_details(name: str) -> None:
    assert asks_for_bank_details(name)


@pytest.mark.parametrize(
    "name",
    [
        "NAV",
        "Net IRR",
        "Capital Called",
        "Capital account",
        "Kapitalkonto",
        "Bank debt",
        "Custodian bank",
        "Banking covenants",
        "Account manager",
        "Accounting standard",
        "Fund Name",
    ],
)
def test_keyword_names_that_do_not(name: str) -> None:
    assert not asks_for_bank_details(name)
