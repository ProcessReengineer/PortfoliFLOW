# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``InvestmentExtractor.extract_region_weights``.

Pure unit tests — the extractor takes the lookup map as a parameter
and has no DB dependency. Block identification is delegated to
:func:`services.reporting.attributes_partition.partition_attributes`:
the second contiguous block of numeric breakdown rows after the
known scalar attributes is the region block (historically labelled
"country" in the partition module).

Per ADR-0046 region resolution is **strict**: an unknown label is a
hard import error rather than a soft fallback. Range violations
(negative weights, sum > 100) also drop the entire region-split
block for the affected investment.

Excel cells in the import format carry fractional weights in ``[0, 1.05]``; the
extractor multiplies by 100 to convert to the DB-side percentage
unit.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from services.data_normalization import InvestmentExtractor


# Synthetic region display-name → UUID lookup used by every test.
# Mirrors what the service builds from
# ``RegionRepository.list_all()``.
_REGION_ID_DACH: UUID = uuid4()
_REGION_ID_UK: UUID = uuid4()
_REGION_ID_USA: UUID = uuid4()
_REGION_ID_NORDICS: UUID = uuid4()

_REGIONS_BY_DISPLAY_NAME: dict[str, UUID] = {
    "dach": _REGION_ID_DACH,
    "uk & ireland": _REGION_ID_UK,
    "north america — usa": _REGION_ID_USA,
    "nordics": _REGION_ID_NORDICS,
}


def _attributes_with_region_rows(
    investment_names: list[str],
    sector_rows: list[tuple[str, list[object]]],
    region_rows: list[tuple[str, list[object]]],
) -> dict:
    """Build an Attributes-sheet payload carrying scalar attrs, sector
    rows, then region rows.

    The block-identification heuristic in
    :func:`services.reporting.attributes_partition.partition_attributes`
    consumes the first contiguous block of fractional numeric rows
    as sectors and the second as countries (now reinterpreted as
    regions by the Phase-6 model — see ADR-0046).
    """
    rows: list[tuple[str, list[object]]] = [
        ("Investment Type", ["private_equity"] * len(investment_names)),
        ("Manager / Fondsname", ["Mgr X"] * len(investment_names)),
    ]
    rows.extend(sector_rows)
    rows.extend(region_rows)
    return {
        "columns": list(investment_names),
        "index": [label for label, _ in rows],
        "data": [vals for _, vals in rows],
    }


# Sector rows used to anchor the partition so the region block is
# identified as the *second* breakdown block.
_DEFAULT_SECTOR_ROWS = [
    ("Tech", [1.0]),
]

_DEFAULT_SECTOR_ROWS_TWO = [
    ("Tech", [1.0, 1.0]),
]


# ---------------------------------------------------------------------------
# RSPLIT-01: known region labels resolve cleanly
# ---------------------------------------------------------------------------


def test_rsplit01_known_region_labels_resolve() -> None:
    names = ["Fund A", "Fund B"]
    sheets = {
        "attributes": _attributes_with_region_rows(
            names,
            sector_rows=_DEFAULT_SECTOR_ROWS_TWO,
            region_rows=[
                ("DACH", [0.6, 0.0]),
                ("North America — USA", [0.4, 1.0]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_region_weights(sheets, _REGIONS_BY_DISPLAY_NAME)

    assert sorted(w.region_id for w in out["Fund A"]) == sorted([_REGION_ID_DACH, _REGION_ID_USA])
    assert {w.region_id: w.weight_pct for w in out["Fund A"]} == {
        _REGION_ID_DACH: Decimal("60.0"),
        _REGION_ID_USA: Decimal("40.0"),
    }
    assert [w.region_id for w in out["Fund B"]] == [_REGION_ID_USA]
    assert out["Fund B"][0].weight_pct == Decimal("100.0")
    assert extractor.errors == []
    assert extractor.warnings == []


# ---------------------------------------------------------------------------
# RSPLIT-02: unknown region label raises a hard import error
# ---------------------------------------------------------------------------


def test_rsplit02_unknown_label_is_hard_error() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_region_rows(
            names,
            sector_rows=_DEFAULT_SECTOR_ROWS,
            region_rows=[
                ("DACH", [0.6]),
                ("Atlantis", [0.4]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_region_weights(sheets, _REGIONS_BY_DISPLAY_NAME)

    # The DACH row is kept; the Atlantis row is dropped with an error.
    assert {w.region_id: w.weight_pct for w in out["Fund A"]} == {
        _REGION_ID_DACH: Decimal("60.0"),
    }
    assert len(extractor.errors) == 1
    err = extractor.errors[0]
    assert err.investment_name == "Fund A"
    assert "atlantis" in err.message.lower()


# ---------------------------------------------------------------------------
# RSPLIT-03: sum > 100 drops the whole block
# ---------------------------------------------------------------------------


def test_rsplit03_sum_exceeds_100_drops_block() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_region_rows(
            names,
            sector_rows=_DEFAULT_SECTOR_ROWS,
            region_rows=[
                ("DACH", [0.55]),
                ("North America — USA", [0.55]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_region_weights(sheets, _REGIONS_BY_DISPLAY_NAME)

    assert out["Fund A"] == []
    assert len(extractor.errors) == 1
    assert "110.0" in extractor.errors[0].message
    assert extractor.errors[0].investment_name == "Fund A"


# ---------------------------------------------------------------------------
# RSPLIT-04: empty region block leaves an empty list (no error)
# ---------------------------------------------------------------------------


def test_rsplit04_empty_block_no_error() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_region_rows(
            names,
            sector_rows=_DEFAULT_SECTOR_ROWS,
            region_rows=[],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_region_weights(sheets, _REGIONS_BY_DISPLAY_NAME)
    assert out == {"Fund A": []}
    assert extractor.errors == []
    assert extractor.warnings == []


# ---------------------------------------------------------------------------
# RSPLIT-05: per-investment isolation (one investment fails, others fine)
# ---------------------------------------------------------------------------


def test_rsplit05_per_investment_isolation() -> None:
    names = ["Fund A", "Fund B"]
    sheets = {
        "attributes": _attributes_with_region_rows(
            names,
            sector_rows=_DEFAULT_SECTOR_ROWS_TWO,
            region_rows=[
                ("DACH", [0.55, 0.5]),
                ("North America — USA", [0.55, 0.5]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_region_weights(sheets, _REGIONS_BY_DISPLAY_NAME)
    # Fund A's block dropped (sum > 100); Fund B's block kept (sum = 100).
    assert out["Fund A"] == []
    assert {w.region_id for w in out["Fund B"]} == {_REGION_ID_DACH, _REGION_ID_USA}
    assert len(extractor.errors) == 1
    assert extractor.errors[0].investment_name == "Fund A"


# ---------------------------------------------------------------------------
# RSPLIT-06: case-insensitive resolution for Excel label trim
# ---------------------------------------------------------------------------


def test_rsplit06_case_insensitive_matches() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_region_rows(
            names,
            sector_rows=_DEFAULT_SECTOR_ROWS,
            region_rows=[
                ("dach", [0.5]),
                ("UK & Ireland", [0.5]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_region_weights(sheets, _REGIONS_BY_DISPLAY_NAME)

    weights = {w.region_id for w in out["Fund A"]}
    assert weights == {_REGION_ID_DACH, _REGION_ID_UK}
    assert extractor.errors == []
