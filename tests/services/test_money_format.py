# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``services.money_format`` (ADR-0101 §3).

Two obligations:

1. **The EUR branch is frozen.** Every threshold case that
   ``_format_eur_compact`` satisfied before ADR-0101 must produce a
   byte-identical string now. This is the formatter half of the §4
   invisibility guarantee — a EUR tenant's Overview reads exactly as it did.
2. **The other branches are correct.** Symbols for USD / GBP, an ISO-code
   prefix for everything else, and sign handling that survives both.
"""

from __future__ import annotations

import pytest

from services.money_format import currency_prefix, format_money_compact


class TestCurrencyPrefix:
    def test_symbol_currencies(self) -> None:
        assert currency_prefix("EUR") == "€"
        assert currency_prefix("USD") == "$"
        assert currency_prefix("GBP") == "£"

    def test_iso_prefix_carries_its_own_separating_space(self) -> None:
        """``CHF`` + ``1.2M`` must not collide into ``CHF1.2M``."""
        assert currency_prefix("CHF") == "CHF "
        assert currency_prefix("JPY") == "JPY "

    def test_case_insensitive(self) -> None:
        assert currency_prefix("eur") == "€"
        assert currency_prefix("chf") == "CHF "

    def test_empty_falls_back_to_eur(self) -> None:
        assert currency_prefix("") == "€"


class TestFormatMoneyCompactEur:
    """The pre-ADR-0101 ``_format_eur_compact`` cases, unchanged."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (342_600_000, "€342.6M"),
            (1_240_000_000, "€1.24B"),
            (12_345, "€12k"),
            (1_000_000, "€1.0M"),
            (1_000, "€1k"),
            (500, "€500"),
            (0, "€0"),
        ],
    )
    def test_thresholds_are_byte_identical_to_the_pre_block_output(
        self, value: float, expected: str
    ) -> None:
        assert format_money_compact(value, "EUR") == expected


class TestFormatMoneyCompactOtherCurrencies:
    def test_dollar_and_sterling_symbols(self) -> None:
        assert format_money_compact(342_600_000, "USD") == "$342.6M"
        assert format_money_compact(1_240_000_000, "GBP") == "£1.24B"
        assert format_money_compact(500, "USD") == "$500"

    def test_iso_prefixed_currencies(self) -> None:
        assert format_money_compact(1_200_000, "CHF") == "CHF 1.2M"
        assert format_money_compact(1_240_000_000, "CHF") == "CHF 1.24B"
        assert format_money_compact(12_345, "SEK") == "SEK 12k"
        assert format_money_compact(500, "JPY") == "JPY 500"

    def test_thresholds_are_currency_independent(self) -> None:
        """Only the prefix changes across currencies — never the banding."""
        for currency in ("EUR", "USD", "GBP", "CHF"):
            prefix = currency_prefix(currency)
            assert format_money_compact(342_600_000, currency) == (f"{prefix}342.6M")
            assert format_money_compact(12_345, currency) == f"{prefix}12k"


class TestFormatMoneyCompactNegatives:
    def test_minus_sign_leads_the_prefix(self) -> None:
        assert format_money_compact(-1_500_000, "EUR") == "-€1.5M"
        assert format_money_compact(-1_500_000, "USD") == "-$1.5M"
        assert format_money_compact(-1_500_000, "CHF") == "-CHF 1.5M"
        assert format_money_compact(-500, "EUR") == "-€500"
