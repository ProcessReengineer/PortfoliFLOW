# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The bank-detail filter of the Reading Desk (ADR-0139 §4, RD-D-24).

The IBANs below are the published example IBANs of their countries; the
account numbers are invented.
"""

from __future__ import annotations

import time

import pytest

from services.scraper.bank_filter import (
    _IBAN_LENGTHS,
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
    # Print forms and wordings the first issue of the filter let through (P-RD-1b2 report, F-2).
    ("Account number:\n0532013000", "Account number:\n[bank detail removed]"),
    ("Sort code\n404784", "Sort code\n[bank detail removed]"),
    ("BIC:\nCOBADEFFXXX", "BIC:\n[bank detail removed]"),
    ("IBAN: DE89 3704 0044\n0532 0130 00", "IBAN: [bank detail removed]"),
    ("account number is 0532013000", "account number is [bank detail removed]"),
    ("Kontonummer lautet 0532013000", "Kontonummer lautet [bank detail removed]"),
    ("Account number (EUR): 0532013000", "Account number (EUR): [bank detail removed]"),
    ("Account number \u2013 0532013000", "Account number \u2013 [bank detail removed]"),
    ("Bankkonto 0532013000", "Bankkonto [bank detail removed]"),
    ("Konto/Depot 0532013000", "Konto/Depot [bank detail removed]"),
    (
        "Account numbers: 0532013000 and 70872490",
        "Account numbers: [bank detail removed] and [bank detail removed]",
    ),
    ("account_number: 0532013000", "account_number: [bank detail removed]"),
    ("Account\xa0number: 0532013000", "Account\xa0number: [bank detail removed]"),
    (
        "The sort code and account number are 40-47-84 and 70872490.",
        "The sort code and account number are [bank detail removed] and [bank detail removed].",
    ),
    ("de89 3704 0044 0532 0130 00", "[bank detail removed]"),
    ("DE89.3704.0044.0532.0130.00", "[bank detail removed]"),
    ("DE89  3704  0044  0532  0130  00", "[bank detail removed]"),
    ("DE89\u20093704\u20090044\u20090532\u20090130\u200900", "[bank detail removed]"),
    ("DE89\u200b3704\u200b0044\u200b0532\u200b0130\u200b00", "[bank detail removed]"),
    ("DE89 37040044 0532013000", "[bank detail removed]"),
    ("IBANDE89370400440532013000", "IBAN[bank detail removed]"),
    ("IBAN: DE89370400440532013000EUR", "IBAN: [bank detail removed]EUR"),
    ("Account number: 0532013000EUR", "Account number: [bank detail removed]EUR"),
    (
        "IBAN of the beneficiary is DE89 3704 0044 0532 0130 01",
        "IBAN of the beneficiary is [bank detail removed]",
    ),
    (
        "IBAN XX12 3456 7890 1234 5678 BIC COBADEFFXXX",
        "IBAN [bank detail removed] BIC [bank detail removed]",
    ),
    ("IBAN: 3704 0044 0532 0130 00", "IBAN: [bank detail removed]"),
    ("account 012 345 678", "account [bank detail removed]"),
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
    # Figures and dates the first issue removed (P-RD-1b2 report, F-3).
    "paid to the account 2024-09-30",
    "SWIFT 2024-09-30",
    "LP account 12500000",
    "current account 12500000",
    "capital-account 12500000",
    "capital  account 12500000",
    "ISIN account LU0123456789",
    "FY24 7591 5609 1296 3571",
    "FY24 7591 5609 1296 3571 1234 5678 9012 3456",
    "The SWIFT transfer of EUR 12500000 was received",
    "BIC Capital invested 12500000 in 2024",
    "Account number of investors: see page 12",
    "IBAN and BIC are stated in the notice",
    "CY24 12 500 000 13 200 000",
    "Seite 12, Kapitalkonto",
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
        "IBANs",
        "account_number",
        "sort_code",
        "Account numbers",
        "Kontonummern",
        "Kontoverbindung",
        "Zahlungsverbindung",
        "Zahlungsanweisungen",
        "Payment details",
        "Settlement instructions",
        "Banking details",
        "Bankdaten",
        "DE89 3704 0044 0532 0130 00",
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


# The example IBANs their countries publish (ISO 13616 registry and national sites).
_PUBLISHED = [
    "GB29 NWBK 6016 1331 9268 19",
    "FR14 2004 1010 0505 0001 3M02 606",
    "CH93 0076 2011 6238 5295 7",
    "NL91 ABNA 0417 1643 00",
    "LU28 0019 4006 4475 0000",
    "AT61 1904 3002 3457 3201",
    "BE68 5390 0754 7034",
    "ES91 2100 0418 4502 0005 1332",
    "IT60 X054 2811 1010 0000 0123 456",
    "IE29 AIBK 9311 5212 3456 78",
    "SE45 5000 0000 0583 9825 7466",
    "DK50 0040 0440 1162 43",
    "NO93 8601 1117 947",
    "FI21 1234 5600 0007 85",
    "PL61 1090 1014 0000 0712 1981 2874",
    "PT50 0002 0123 1234 5678 9015 4",
    "LI21 0881 0000 2324 013A A",
    "MT84 MALT 0110 0001 2345 MTLC AST0 01S",
    "CZ65 0800 0000 1920 0014 5399",
    "HU42 1177 3016 1111 1018 0000 0000",
    "GR16 0110 1250 0000 0001 2300 695",
]


@pytest.mark.parametrize("iban", _PUBLISHED)
def test_published_example_ibans_are_removed(iban: str) -> None:
    assert remove_bank_details(f"Pay to {iban} by Friday") == f"Pay to {REPLACEMENT} by Friday"
    assert remove_bank_details(iban.replace(" ", "")) == REPLACEMENT


def _iban_for(country: str, length: int) -> str:
    """Build an IBAN of the country's length with valid check digits."""
    bban = ("1234567890" * 4)[: length - 4]
    remainder = int(bban + "".join(str(int(char, 36)) for char in country) + "00") % 97
    return f"{country}{98 - remainder:02d}{bban}"


@pytest.mark.parametrize(("country", "length"), sorted(_IBAN_LENGTHS.items()))
def test_every_registered_country_is_recognised_at_its_length(country: str, length: int) -> None:
    iban = _iban_for(country, length)

    assert remove_bank_details(iban) == REPLACEMENT
    # One character short is not that country's IBAN, and no bank term stands beside it.
    assert remove_bank_details(iban[:-1]) == iban[:-1]


def test_an_unregistered_country_code_is_not_an_iban() -> None:
    iban = _iban_for("FY", 22)

    assert remove_bank_details(iban) == iban
    assert remove_bank_details(f"IBAN {iban}") == f"IBAN {REPLACEMENT}"


@pytest.mark.parametrize("term", ["account", "Account number", "IBAN", "BIC", "Konto", "sort code"])
@pytest.mark.parametrize("filler", [" ", "\n", ". ", "9 ", "A1 "])
def test_the_filter_runs_in_linear_time(term: str, filler: str) -> None:
    """P-RD-1b2 report, F-1: 16,000 blanks after a term once took 8.8 s, quadratically."""
    text = term + filler * 100_000

    started = time.perf_counter()
    remove_bank_details(text)

    assert time.perf_counter() - started < 5.0
