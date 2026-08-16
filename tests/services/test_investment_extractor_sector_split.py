# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``InvestmentExtractor.extract_sector_weights``.

Block identification is delegated to
:func:`services.reporting.attributes_partition.partition_attributes`:
the first contiguous block of numeric breakdown rows after the known
scalar attributes is the sector block. Excel cells carry fractional
weights in ``[0, 1.05]``; the extractor multiplies by 100 to produce
the DB-side percentage value.

Sector resolution is **lookup-only** at this layer — the service
layer auto-creates missing tenant sectors before calling the
extractor (see ``test_investment_service_transform_with_weights``).
Labels that still miss after that pre-pass produce a row-level
error and are dropped.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from services.data_normalization import InvestmentExtractor


# Synthetic sector lookup: lower-cased display name OR canonical code
# → UUID. The service caller mirrors this convention when building
# the dict from ``SectorRepository.list_active``.
_TECH_ID = uuid4()
_HEALTH_ID = uuid4()
_SECTORS_BY_LABEL = {
    "technology — software": _TECH_ID,
    "tech_software": _TECH_ID,
    "healthcare": _HEALTH_ID,
}


def _attributes_with_sector_rows(
    investment_names: list[str],
    sector_rows: list[tuple[str, list[object]]],
    *,
    country_rows: list[tuple[str, list[object]]] | None = None,
) -> dict:
    """Build an Attributes-sheet payload carrying scalar attrs +
    sector rows + (optionally) country rows.

    The country block is included only when needed to anchor the
    block-boundary heuristic — the sector block is the *first*
    contiguous breakdown block after the known scalar attributes.
    """
    rows: list[tuple[str, list[object]]] = [
        ("Investment Type", ["private_equity"] * len(investment_names)),
        ("Manager / Fondsname", ["Mgr X"] * len(investment_names)),
    ]
    rows.extend(sector_rows)
    if country_rows is not None:
        rows.extend(country_rows)
    return {
        "columns": list(investment_names),
        "index": [label for label, _ in rows],
        "data": [vals for _, vals in rows],
    }


# ---------------------------------------------------------------------------
# SSPLIT-01: known sector (display-name and code variants) resolve to UUID
# ---------------------------------------------------------------------------


def test_ssplit01_known_sector_resolves() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_sector_rows(
            names,
            [
                ("Technology — Software", [0.70]),
                ("Healthcare", [0.30]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_sector_weights(sheets, _SECTORS_BY_LABEL)

    weights = {w.sector_id: w.weight_pct for w in out["Fund A"]}
    assert weights == {
        _TECH_ID: Decimal("70.00"),
        _HEALTH_ID: Decimal("30.00"),
    }
    assert extractor.errors == []


# ---------------------------------------------------------------------------
# SSPLIT-02: unknown sector triggers an error and drops only that row
# ---------------------------------------------------------------------------


def test_ssplit02_unknown_sector_drops_row_only() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_sector_rows(
            names,
            [
                ("Technology — Software", [0.70]),
                ("Mystery Sector", [0.30]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_sector_weights(sheets, _SECTORS_BY_LABEL)

    # Only the known sector remains. The unknown row is dropped but
    # the investment overall is still represented.
    assert len(out["Fund A"]) == 1
    assert out["Fund A"][0].sector_id == _TECH_ID
    assert len(extractor.errors) == 1
    assert "mystery sector" in extractor.errors[0].message.lower()
    assert extractor.errors[0].investment_name == "Fund A"


# ---------------------------------------------------------------------------
# SSPLIT-04: sum > 100 drops the block
# ---------------------------------------------------------------------------


def test_ssplit04_sum_exceeds_100_drops_block() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_sector_rows(
            names,
            [
                ("Technology — Software", [0.60]),
                ("Healthcare", [0.50]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_sector_weights(sheets, _SECTORS_BY_LABEL)
    assert out["Fund A"] == []
    assert len(extractor.errors) == 1
    assert "110" in extractor.errors[0].message


# ---------------------------------------------------------------------------
# SSPLIT-05: empty sector block leaves an empty list
# ---------------------------------------------------------------------------


def test_ssplit05_empty_block_no_error() -> None:
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_sector_rows(names, []),
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_sector_weights(sheets, _SECTORS_BY_LABEL)
    assert out == {"Fund A": []}
    assert extractor.errors == []


# ---------------------------------------------------------------------------
# SSPLIT-06: country block does not bleed into the sector results
# ---------------------------------------------------------------------------


def test_ssplit06_country_block_isolated_from_sectors() -> None:
    """The first breakdown block is sectors; the second is countries.
    The extractor must consume only the first block here, even if a
    country label collides with a known sector lookup key.
    """
    names = ["Fund A"]
    sheets = {
        "attributes": _attributes_with_sector_rows(
            names,
            sector_rows=[
                ("Technology — Software", [1.0]),
            ],
            country_rows=[
                # Bogus country label that just happens to match a
                # sector lookup key. The extractor must NOT pick this
                # row up via :meth:`extract_sector_weights`.
                ("Healthcare", [1.0]),
            ],
        )
    }
    extractor = InvestmentExtractor()
    out = extractor.extract_sector_weights(sheets, _SECTORS_BY_LABEL)
    assert {w.sector_id for w in out["Fund A"]} == {_TECH_ID}
    assert extractor.errors == []
