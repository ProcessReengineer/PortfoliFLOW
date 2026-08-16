# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Extractor tests for the optional Units / Units As Of rows (ADR-0097 §7).

Pure unit tests — the extractor has no DB or FastAPI dependency. Snapshots
use the ``to_dict('split')`` shape that
:class:`core.repositories.DataUploadRepository` persists in
``data_upload_sheets.data``. That layer serialises with
``to_json(orient="split", date_format="iso")``, so a date-valued
Attributes cell lands as an ISO string; ``Units As Of`` also parses from a
``datetime`` and from an epoch-ms int, both accepted defensively.

Coverage
--------
* IE-UNIT-01: Units row, no Units As Of → the date defaults to the
  investment's earliest **actual** NAV date.
* IE-UNIT-02/03/04: explicit Units As Of as ISO string / epoch-ms int /
  ``datetime`` all parse to the same date.
* IE-UNIT-05: a fixture **without** the Units rows yields
  ``units is None`` and is otherwise byte-identical to the same fixture
  *with* the rows (backward-compat guarantee).
* IE-UNIT-06..11: validation errors — non-positive units, non-numeric
  units, Units As Of without Units, unparseable date, units without any
  actual NAV, and a date after the NAV series.
* IE-UNIT-12: a Units As Of *before* the first actual NAV is accepted
  (the rule is "within or before the series").
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from services.data_normalization import InvestmentExtractor


# ---------------------------------------------------------------------------
# Fixtures — JSONB-shaped sheet payloads
# ---------------------------------------------------------------------------


def _attributes_payload(
    investment_names: list[str],
    *,
    types: list[str | None],
    currencies: list[str | None] | None = None,
    units: list[object] | None = None,
    units_as_of: list[object] | None = None,
) -> dict:
    """Build an Attributes-sheet payload, optionally with the units rows."""
    n = len(investment_names)

    def _pad(row: list | None) -> list:
        return list(row) if row is not None else [None] * n

    rows: dict[str, list] = {
        "Investment Type": _pad(types),
        "Währung": _pad(currencies or ["EUR"] * n),
    }
    if units is not None:
        rows["Units"] = _pad(units)
    if units_as_of is not None:
        rows["Units As Of"] = _pad(units_as_of)
    return {
        "columns": list(investment_names),
        "index": list(rows.keys()),
        "data": list(rows.values()),
    }


def _navs_payload(
    investment_names: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    return {
        "columns": list(investment_names),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


def _epoch_ms(d: date) -> int:
    """Encode a date as pandas would encode a datetime in an object cell."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


_NAVS_TWO_POINTS = [
    ("2020-01-01T00:00:00.000", [1_000_000.0]),
    ("2021-01-01T00:00:00.000", [1_100_000.0]),
]


def _sheets(**attr_kwargs) -> dict:
    names = attr_kwargs.pop("names", ["Inv"])
    navs = attr_kwargs.pop("navs", _NAVS_TWO_POINTS)
    sheets = {"attributes": _attributes_payload(names, **attr_kwargs)}
    if navs is not None:
        sheets["navs_actual"] = _navs_payload(names, navs)
    return sheets


# ---------------------------------------------------------------------------
# IE-UNIT-01: default Units As Of = earliest actual NAV date
# ---------------------------------------------------------------------------


def test_ieunit01_units_default_as_of_is_earliest_actual_nav() -> None:
    extractor = InvestmentExtractor()
    investments = extractor.extract(_sheets(types=["listed_equity"], units=[250_000]))
    assert extractor.errors == []
    (inv,) = investments
    assert inv.units == Decimal("250000")
    assert inv.units_as_of == date(2020, 1, 1)


# ---------------------------------------------------------------------------
# IE-UNIT-02/03/04: explicit Units As Of in all three arrival shapes
# ---------------------------------------------------------------------------


def test_ieunit02_explicit_as_of_iso_string() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(
        _sheets(
            types=["listed_equity"],
            units=[250_000],
            units_as_of=["2020-06-15"],
        )
    )
    assert extractor.errors == []
    assert inv.units_as_of == date(2020, 6, 15)


def test_ieunit03_explicit_as_of_epoch_ms_int() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(
        _sheets(
            types=["listed_equity"],
            units=[250_000],
            units_as_of=[_epoch_ms(date(2020, 6, 15))],
        )
    )
    assert extractor.errors == []
    assert inv.units_as_of == date(2020, 6, 15)


def test_ieunit04_explicit_as_of_datetime_object() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(
        _sheets(
            types=["listed_equity"],
            units=[250_000],
            units_as_of=[datetime(2020, 6, 15, 0, 0)],
        )
    )
    assert extractor.errors == []
    assert inv.units_as_of == date(2020, 6, 15)


# ---------------------------------------------------------------------------
# IE-UNIT-05: backward-compat — a units-less workbook is unchanged
# ---------------------------------------------------------------------------


def test_ieunit05_no_units_rows_changes_nothing() -> None:
    common = dict(types=["listed_equity"], currencies=["EUR"])
    without = InvestmentExtractor().extract(_sheets(**common))
    with_ = InvestmentExtractor().extract(_sheets(**common, units=[250_000]))

    (inv_without,) = without
    (inv_with,) = with_
    assert inv_without.units is None
    assert inv_without.units_as_of is None
    # Only the units fields differ; everything else is byte-identical.
    assert replace(inv_without, units=None, units_as_of=None) == replace(
        inv_with, units=None, units_as_of=None
    )
    assert inv_with.units == Decimal("250000")


# ---------------------------------------------------------------------------
# IE-UNIT-06..11: validation errors → no units, error recorded
# ---------------------------------------------------------------------------


def _assert_row_error(extractor: InvestmentExtractor, row_index: str) -> None:
    assert extractor.errors, "expected a row-level error"
    assert any(e.sheet == "attributes" and e.row_index == row_index for e in extractor.errors), [
        (e.sheet, e.row_index) for e in extractor.errors
    ]


def test_ieunit06_non_positive_units_errors() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(_sheets(types=["listed_equity"], units=[0]))
    _assert_row_error(extractor, "Units")
    assert inv.units is None and inv.units_as_of is None


def test_ieunit07_non_numeric_units_errors() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(_sheets(types=["listed_equity"], units=["not-a-number"]))
    _assert_row_error(extractor, "Units")
    assert inv.units is None


def test_ieunit08_as_of_without_units_errors() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(_sheets(types=["listed_equity"], units_as_of=["2020-06-15"]))
    _assert_row_error(extractor, "Units As Of")
    assert inv.units is None


def test_ieunit09_unparseable_as_of_errors() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(
        _sheets(
            types=["listed_equity"],
            units=[250_000],
            units_as_of=["last Tuesday"],
        )
    )
    _assert_row_error(extractor, "Units As Of")
    assert inv.units is None


def test_ieunit10_units_without_actual_nav_errors() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(_sheets(types=["listed_equity"], units=[250_000], navs=None))
    _assert_row_error(extractor, "Units")
    assert inv.units is None


def test_ieunit11_as_of_after_nav_series_errors() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(
        _sheets(
            types=["listed_equity"],
            units=[250_000],
            units_as_of=["2099-01-01"],
        )
    )
    _assert_row_error(extractor, "Units As Of")
    assert inv.units is None


# ---------------------------------------------------------------------------
# IE-UNIT-12: Units As Of before the first actual NAV is accepted
# ---------------------------------------------------------------------------


def test_ieunit12_as_of_before_series_is_accepted() -> None:
    extractor = InvestmentExtractor()
    (inv,) = extractor.extract(
        _sheets(
            types=["listed_equity"],
            units=[250_000],
            units_as_of=["2019-06-01"],
        )
    )
    assert extractor.errors == []
    assert inv.units_as_of == date(2019, 6, 1)
