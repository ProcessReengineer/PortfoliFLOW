# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for ``extract_fx_rates_from_snapshot`` (ADR-0099 §5).

Pure unit tests — the extractor has no DB / FastAPI dependency. The
fixtures construct the ``DataFrame.to_json(orient="split")`` shape
persisted by :class:`DataUploadRepository` for the ``FX rates``
market-reference sheet.

Coverage:

* Happy path: two currencies (``USD/EUR``, ``GBP/EUR``), sparse rows
  → typed :class:`ImportedFxRate` DTOs with the correct
  ``(currency, reference_currency, rate)`` triples.
* Missing ``fx_rates`` key → ``([], [])`` (optional sheet, silent).
* Malformed header (not ``XXX/YYY``) → :class:`ValidationError`.
* Mixed quote sides (two reference currencies) → :class:`ValidationError`.
* Identity pair (``EUR/EUR``) → :class:`ValidationError`.
* Non-numeric cell → :class:`ImportRowError`, cell dropped.
* Non-positive cell (``<= 0``) → :class:`ImportRowError`, cell dropped.
* Blank cells silently absent (sparse series, no error).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.exceptions import ValidationError
from services.data_normalization import (
    ImportedFxRate,
    extract_fx_rates_from_snapshot,
)


def _fx_rates_payload(
    headers: list[str],
    rows: list[tuple[str, list[object]]],
) -> dict:
    """Build the JSONB split-payload shape for the ``FX rates`` sheet."""
    return {
        "columns": list(headers),
        "index": [iso for iso, _ in rows],
        "data": [vals for _, vals in rows],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_fx01_happy_path_two_currencies_sparse_rows() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["USD/EUR", "GBP/EUR"],
            [
                # GBP is blank on day 1 — a sparse series, not an error.
                ("2026-01-01", [0.92, None]),
                ("2026-01-02", [0.93, 1.17]),
            ],
        ),
    }
    rates, errors = extract_fx_rates_from_snapshot(snapshot)

    assert errors == []
    # Blank GBP cell absent ⇒ three observations, not four.
    assert len(rates) == 3
    assert ImportedFxRate(date(2026, 1, 1), "USD", Decimal("0.92"), "EUR") in rates
    assert ImportedFxRate(date(2026, 1, 2), "USD", Decimal("0.93"), "EUR") in rates
    assert ImportedFxRate(date(2026, 1, 2), "GBP", Decimal("1.17"), "EUR") in rates
    # The blank cell produced no GBP observation on day 1.
    assert not any(r.currency == "GBP" and r.as_of_date == date(2026, 1, 1) for r in rates)
    # Every observation quotes against the single declared reference.
    assert {r.reference_currency for r in rates} == {"EUR"}


def test_fx01b_non_eur_reference_currency_is_honoured() -> None:
    """The quote side declares the reference; it need not be EUR."""
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["EUR/USD", "GBP/USD"],
            [("2026-01-01", [1.08, 1.27])],
        ),
    }
    rates, errors = extract_fx_rates_from_snapshot(snapshot)
    assert errors == []
    assert set(rates) == {
        ImportedFxRate(date(2026, 1, 1), "EUR", Decimal("1.08"), "USD"),
        ImportedFxRate(date(2026, 1, 1), "GBP", Decimal("1.27"), "USD"),
    }


# ---------------------------------------------------------------------------
# Optional-sheet property
# ---------------------------------------------------------------------------


def test_fx02_missing_sheet_returns_empty_silently() -> None:
    # A snapshot with unrelated sheets and no ``fx_rates`` key.
    rates, errors = extract_fx_rates_from_snapshot(
        {"navs_actual": {"columns": [], "index": [], "data": []}}
    )
    assert rates == []
    assert errors == []


def test_fx02b_empty_snapshot_returns_empty_silently() -> None:
    rates, errors = extract_fx_rates_from_snapshot({})
    assert rates == []
    assert errors == []


# ---------------------------------------------------------------------------
# Header validation — hard ValidationError
# ---------------------------------------------------------------------------


def test_fx03_malformed_header_raises() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["USDEUR"],  # missing the slash separator
            [("2026-01-01", [0.92])],
        ),
    }
    with pytest.raises(ValidationError) as exc_info:
        extract_fx_rates_from_snapshot(snapshot)
    assert "USDEUR" in str(exc_info.value)
    assert "XXX/YYY" in str(exc_info.value)


def test_fx03b_lowercase_or_wrong_length_header_raises() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["usd/eur"],  # not uppercase three-letter codes
            [("2026-01-01", [0.92])],
        ),
    }
    with pytest.raises(ValidationError):
        extract_fx_rates_from_snapshot(snapshot)


def test_fx04_mixed_quote_sides_raises() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["USD/EUR", "GBP/USD"],  # two different reference currencies
            [("2026-01-01", [0.92, 1.27])],
        ),
    }
    with pytest.raises(ValidationError) as exc_info:
        extract_fx_rates_from_snapshot(snapshot)
    msg = str(exc_info.value)
    assert "reference" in msg.lower()


def test_fx05_identity_pair_raises() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["EUR/EUR"],
            [("2026-01-01", [1.0])],
        ),
    }
    with pytest.raises(ValidationError) as exc_info:
        extract_fx_rates_from_snapshot(snapshot)
    assert "identity" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Cell validation — soft ImportRowError, row dropped
# ---------------------------------------------------------------------------


def test_fx06_non_numeric_cell_is_row_error() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["USD/EUR"],
            [
                ("2026-01-01", ["not-a-number"]),
                ("2026-01-02", [0.93]),
            ],
        ),
    }
    rates, errors = extract_fx_rates_from_snapshot(snapshot)
    # The good day still lands; the bad cell is a collected error.
    assert rates == [ImportedFxRate(date(2026, 1, 2), "USD", Decimal("0.93"), "EUR")]
    assert len(errors) == 1
    err = errors[0]
    assert err.sheet == "fx_rates"
    assert err.column == "USD/EUR"
    assert err.row_index == "2026-01-01"


@pytest.mark.parametrize("bad_value", [0, -0.5, "-1.2"])
def test_fx07_non_positive_cell_is_row_error(bad_value: object) -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["USD/EUR"],
            [("2026-01-01", [bad_value])],
        ),
    }
    rates, errors = extract_fx_rates_from_snapshot(snapshot)
    assert rates == []
    assert len(errors) == 1
    assert errors[0].column == "USD/EUR"
    assert "positive" in errors[0].message.lower()


def test_fx08_blank_cells_are_silently_absent() -> None:
    snapshot = {
        "fx_rates": _fx_rates_payload(
            ["USD/EUR", "GBP/EUR"],
            [
                ("2026-01-01", [None, None]),
                ("2026-01-02", ["", 1.17]),  # empty string is blank too
            ],
        ),
    }
    rates, errors = extract_fx_rates_from_snapshot(snapshot)
    # Only the single populated (2026-01-02, GBP) cell survives.
    assert rates == [ImportedFxRate(date(2026, 1, 2), "GBP", Decimal("1.17"), "EUR")]
    assert errors == []
