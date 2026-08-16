# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the Phase-7 AnlV-classification extractor path (ADR-0057).

Two surfaces:

1. The pure-function normaliser ``_normalise_anlv_code`` — accepts
   the four lenient input forms, returns ``None`` for empty cells,
   raises ``ValueError`` for unrecognised values.
2. The ``InvestmentExtractor.extract`` integration — the AnlV row in
   the ``Attributes`` sheet flows through to
   :attr:`ImportedInvestment.anlv_code`, with row-level error
   collection for catalogue misses.
"""

from __future__ import annotations

import pytest

from services.data_normalization import InvestmentExtractor
from services.data_normalization.investment_extractor import (
    _normalise_anlv_code,
)


# ---------------------------------------------------------------------------
# _normalise_anlv_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Nr. 13", "anlv_13"),
        ("Nr.13", "anlv_13"),
        ("  13 ", "anlv_13"),
        ("anlv_13", "anlv_13"),
        ("ANLV_13", "anlv_13"),
        ("nr.  13", "anlv_13"),
        (13, "anlv_13"),
        ("Nr. 1", "anlv_1"),
        ("Nr. 17", "anlv_17"),
    ],
)
def test_normalise_anlv_code_accepts_documented_forms(raw: object, expected: str) -> None:
    assert _normalise_anlv_code(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
def test_normalise_anlv_code_empty_returns_none(raw: object) -> None:
    assert _normalise_anlv_code(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "Nr. abc",
        "anlv-13",  # hyphen, not underscore
        "pe",
        "XYZ",
        True,
        13.5,
    ],
)
def test_normalise_anlv_code_rejects_unrecognised(raw: object) -> None:
    with pytest.raises(ValueError):
        _normalise_anlv_code(raw)


# ---------------------------------------------------------------------------
# extractor integration: AnlV row → ImportedInvestment.anlv_code
# ---------------------------------------------------------------------------


def _attributes_payload_with_anlv(
    investment_names: list[str],
    *,
    types: list[str],
    asset_classes: list[str],
    anlv_values: list[object | None],
) -> dict:
    """JSONB-shaped Attributes payload mirroring the v21 layout.

    Rows: Investment Type, Investment Sub-Class (synthetic from rows
    2 & 3), Region, Vintage Year, Währung, Asset Class, AnlV,
    Manager / Fondsname.
    """
    return {
        "columns": investment_names,
        "index": [
            "Investment Type",
            "Investment Sub-Class",
            "Region",
            "Vintage Year",
            "Währung",
            "Asset Class",
            "AnlV",
            "Manager / Fondsname",
        ],
        "data": [
            types,
            [None] * len(investment_names),
            ["Europe"] * len(investment_names),
            ["2020"] * len(investment_names),
            ["EUR"] * len(investment_names),
            asset_classes,
            anlv_values,
            [f"GP {i}" for i in range(len(investment_names))],
        ],
    }


def test_extract_anlv_row_resolves_to_canonical_code() -> None:
    """A populated AnlV row maps to the canonical ``"anlv_<n>"`` code."""
    payload = _attributes_payload_with_anlv(
        ["Investment A", "Investment D", "Investment E"],
        types=["Aktien", "Immobilien", "Privates Eigenkapital"],
        asset_classes=["equities", "real_estate", "private_equity"],
        anlv_values=["Nr. 15", "Nr. 14", "Nr. 13"],
    )
    extractor = InvestmentExtractor()
    investments = extractor.extract(
        {"attributes": payload},
        valid_anlv_codes=frozenset({"anlv_13", "anlv_14", "anlv_15"}),
    )

    by_name = {inv.name: inv for inv in investments}
    assert by_name["Investment A"].anlv_code == "anlv_15"
    assert by_name["Investment D"].anlv_code == "anlv_14"
    assert by_name["Investment E"].anlv_code == "anlv_13"
    assert extractor.errors == []


def test_extract_anlv_empty_cell_maps_to_none() -> None:
    """An empty AnlV cell leaves :attr:`ImportedInvestment.anlv_code` ``None``."""
    payload = _attributes_payload_with_anlv(
        ["Investment X"],
        types=["Aktien"],
        asset_classes=["equities"],
        anlv_values=[None],
    )
    extractor = InvestmentExtractor()
    investments = extractor.extract(
        {"attributes": payload},
        valid_anlv_codes=frozenset({"anlv_13", "anlv_14", "anlv_15"}),
    )
    assert len(investments) == 1
    assert investments[0].anlv_code is None
    assert extractor.errors == []


def test_extract_anlv_unknown_value_records_row_error() -> None:
    """Unrecognised AnlV cell content surfaces as a row-level error."""
    payload = _attributes_payload_with_anlv(
        ["Investment X"],
        types=["Aktien"],
        asset_classes=["equities"],
        anlv_values=["Nr. abc"],
    )
    extractor = InvestmentExtractor()
    investments = extractor.extract(
        {"attributes": payload},
        valid_anlv_codes=frozenset({"anlv_13", "anlv_14", "anlv_15"}),
    )
    assert len(investments) == 1
    assert investments[0].anlv_code is None
    assert len(extractor.errors) == 1
    err = extractor.errors[0]
    assert err.investment_name == "Investment X"
    assert err.row_index == "AnlV"
    assert "Nr. abc" in err.message


def test_extract_anlv_code_not_in_catalogue_records_row_error() -> None:
    """A normalised code that misses the catalogue is reported, not persisted."""
    payload = _attributes_payload_with_anlv(
        ["Investment Y"],
        types=["Aktien"],
        asset_classes=["equities"],
        anlv_values=["Nr. 99"],
    )
    extractor = InvestmentExtractor()
    investments = extractor.extract(
        {"attributes": payload},
        valid_anlv_codes=frozenset({"anlv_13", "anlv_14", "anlv_15"}),
    )
    assert len(investments) == 1
    assert investments[0].anlv_code is None
    assert len(extractor.errors) == 1
    err = extractor.errors[0]
    assert "anlv_99" in err.message
    assert "catalogue" in err.message


def test_extract_without_valid_anlv_codes_skips_catalogue_check() -> None:
    """When ``valid_anlv_codes`` is ``None``, only normalisation runs.

    This mode is used by extractor-only unit tests; the catalogue
    check is the service's responsibility when it is supplied with
    a repository.
    """
    payload = _attributes_payload_with_anlv(
        ["Investment X"],
        types=["Aktien"],
        asset_classes=["equities"],
        anlv_values=["Nr. 99"],  # not in any catalogue, but normalises fine
    )
    extractor = InvestmentExtractor()
    investments = extractor.extract({"attributes": payload}, valid_anlv_codes=None)
    assert len(investments) == 1
    assert investments[0].anlv_code == "anlv_99"
    assert extractor.errors == []
