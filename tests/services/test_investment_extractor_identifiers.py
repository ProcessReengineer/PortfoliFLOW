# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Extractor tests for the optional ISIN / Ticker identifier rows (ADR-0090).

Pure unit tests — the extractor has no DB or FastAPI dependency. Test
snapshots use the ``DataFrame.to_json(orient="split")`` shape that
:class:`core.repositories.DataUploadRepository` persists in
``data_upload_sheets.data``.

Coverage
--------
* IE-ID-01: ISIN / Ticker rows → correct :class:`ImportedIdentifier`
  tuples per investment (both, ISIN-only, Ticker-only, neither).
* IE-ID-02: blank and whitespace-only identifier cells produce no rows.
* IE-ID-03: a fixture **without** the identifier rows yields
  ``identifiers == ()`` and leaves every other extraction result
  identical to the same fixture *with* the rows (backward-compat
  guarantee, ADR-0090 §Consequences).
"""

from __future__ import annotations

from dataclasses import replace

from services.data_normalization import (
    ImportedIdentifier,
    InvestmentExtractor,
)


# ---------------------------------------------------------------------------
# Fixtures — JSONB-shaped sheet payloads
# ---------------------------------------------------------------------------


def _attributes_payload(
    investment_names: list[str],
    *,
    types: list[str | None],
    asset_classes: list[str | None] | None = None,
    currencies: list[str | None] | None = None,
    isins: list[object] | None = None,
    tickers: list[object] | None = None,
) -> dict:
    """Build an Attributes-sheet payload, optionally with ISIN / Ticker rows.

    The two identifier rows are appended only when supplied, so the same
    helper drives both the with-identifiers and identifier-less fixtures.
    """
    n = len(investment_names)

    def _pad(row: list | None) -> list:
        return list(row) if row is not None else [None] * n

    rows: dict[str, list] = {
        "Investment Type": _pad(types),
        "Asset Class": _pad(asset_classes),
        "Währung": _pad(currencies),
    }
    if isins is not None:
        rows["ISIN"] = _pad(isins)
    if tickers is not None:
        rows["Ticker"] = _pad(tickers)
    return {
        "columns": list(investment_names),
        "index": list(rows.keys()),
        "data": list(rows.values()),
    }


def _timeseries_payload(
    investment_names: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    """Build a date-indexed JSONB payload."""
    return {
        "columns": list(investment_names),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


# ---------------------------------------------------------------------------
# IE-ID-01: correct identifier tuples per investment
# ---------------------------------------------------------------------------


def test_ieid01_identifier_rows_parsed_per_investment() -> None:
    names = ["Both", "IsinOnly", "TickerOnly", "Neither"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=[
                "listed_equity",
                "listed_bonds",
                "listed_equity",
                "private_equity",
            ],
            # Deliberately messy input: the extractor trims but does NOT
            # upper-case (the repository owns normalisation).
            isins=["  de000basf111 ", "US0378331005", None, None],
            tickers=["BAS", None, "aapl", None],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []

    by_name = {i.name: i for i in investments}

    assert by_name["Both"].identifiers == (
        ImportedIdentifier(scheme="isin", value="de000basf111"),
        ImportedIdentifier(scheme="ticker", value="BAS"),
    )
    assert by_name["IsinOnly"].identifiers == (
        ImportedIdentifier(scheme="isin", value="US0378331005"),
    )
    assert by_name["TickerOnly"].identifiers == (ImportedIdentifier(scheme="ticker", value="aapl"),)
    # Blank cells produce no identifier — the illiquid-instrument state.
    assert by_name["Neither"].identifiers == ()


# ---------------------------------------------------------------------------
# IE-ID-02: blank / whitespace-only cells produce no rows
# ---------------------------------------------------------------------------


def test_ieid02_blank_and_whitespace_cells_produce_no_rows() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=["listed_equity"],
            isins=["   "],  # whitespace only
            tickers=[""],  # empty string
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    assert investments[0].identifiers == ()


def test_ieid02b_none_cells_produce_no_rows() -> None:
    names = ["Inv"]
    sheets = {
        "attributes": _attributes_payload(
            names,
            types=["listed_equity"],
            isins=[None],
            tickers=[None],
        ),
    }
    extractor = InvestmentExtractor()
    investments = extractor.extract(sheets)
    assert extractor.errors == []
    assert investments[0].identifiers == ()


# ---------------------------------------------------------------------------
# IE-ID-03: backward-compat — an identifier-less workbook is unchanged
# ---------------------------------------------------------------------------


def test_ieid03_no_identifier_rows_changes_nothing() -> None:
    """A workbook without ISIN / Ticker rows extracts exactly as before.

    Proves the additive, backward-compatible property from ADR-0090
    §Consequences: the *only* difference the identifier rows make is the
    populated ``identifiers`` tuple. With the rows removed, that tuple is
    empty and every other extracted field is byte-for-byte identical.
    """
    names = ["Fund A", "Fund B"]
    common = dict(
        types=["listed_equity", "private_equity"],
        asset_classes=["listed_equity", "private_equity"],
        currencies=["EUR", "USD"],
    )
    navs = _timeseries_payload(
        names,
        [
            ("2024-01-01T00:00:00.000", [100.0, 200.0]),
            ("2024-07-01T00:00:00.000", [110.0, 210.0]),
        ],
    )
    cashflows = _timeseries_payload(names, [("2024-03-01T00:00:00.000", [-10.0, -20.0])])

    without_sheets = {
        "attributes": _attributes_payload(names, **common),
        "navs_actual": navs,
        "cash_flow_out_actual": cashflows,
    }
    with_sheets = {
        "attributes": _attributes_payload(
            names,
            **common,
            isins=["DE000BASF111", None],
            tickers=["BAS", "XYZ"],
        ),
        "navs_actual": navs,
        "cash_flow_out_actual": cashflows,
    }

    inv_without = InvestmentExtractor().extract(without_sheets)
    inv_with = InvestmentExtractor().extract(with_sheets)

    # Without the rows, every investment carries an empty tuple.
    assert all(i.identifiers == () for i in inv_without)

    # And every other field is identical to the with-identifiers run —
    # compare the two sets after zeroing out the identifiers field.
    by_without = {i.name: i for i in inv_without}
    by_with = {i.name: i for i in inv_with}
    assert set(by_without) == set(by_with) == set(names)
    for name in names:
        assert replace(by_without[name], identifiers=()) == replace(by_with[name], identifiers=())

    # Sanity: the with-identifiers run did populate the tuples.
    assert by_with["Fund A"].identifiers == (
        ImportedIdentifier(scheme="isin", value="DE000BASF111"),
        ImportedIdentifier(scheme="ticker", value="BAS"),
    )
    assert by_with["Fund B"].identifiers == (ImportedIdentifier(scheme="ticker", value="XYZ"),)
