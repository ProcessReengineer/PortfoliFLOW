# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pre-fill mapping, field by field (ADR-0129 §3, Stage A).

``fill_to_prefill`` maps and never computes (D-amounts). These tests pin the
mapping itself; ``test_contract.py`` C-3 pins what it deliberately omits.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from services.provider_channel.prefill import (
    IDENTIFIER_SCHEME_ISIN,
    MD_IDENTIFIER_SCHEME,
    MD_IDENTIFIER_VALUE,
    PREFILL_FIELD_CURRENCY,
    PREFILL_FIELD_FEES,
    PREFILL_FIELD_MASTER_DATA,
    PREFILL_FIELD_PRICE_PER_UNIT,
    PREFILL_FIELD_SETTLEMENT_DATE,
    PREFILL_FIELD_TAXES,
    PREFILL_FIELD_TRADE_DATE,
    PREFILL_FIELD_UNITS,
    fill_to_prefill,
)
from services.provider_channel.schemas import FILL_SCHEMA_VERSION, FillPayload


def _fill(**overrides: object) -> FillPayload:
    """A confirmation with every field populated, unless overridden."""
    values: dict[str, object] = {
        "schema_version": FILL_SCHEMA_VERSION,
        "units": Decimal("1250.00000000"),
        "price_per_unit": Decimal("98.7650"),
        "fees": Decimal("12.50"),
        "taxes": Decimal("3.25"),
        "currency": "EUR",
        "trade_date": date(2026, 9, 4),
        "settlement_date": date(2026, 9, 8),
        "isin": "DE0001234567",
        "message": "Filled in two tranches.",
    }
    values.update(overrides)
    return FillPayload(**values)  # type: ignore[arg-type]


def test_every_field_maps_across_unchanged() -> None:
    """Values pass through untouched — no rounding, no arithmetic, no defaults."""
    fill = _fill()
    prefill = fill_to_prefill(fill)

    assert prefill[PREFILL_FIELD_UNITS] == Decimal("1250.00000000")
    assert str(prefill[PREFILL_FIELD_UNITS]) == "1250.00000000"
    assert prefill[PREFILL_FIELD_PRICE_PER_UNIT] == Decimal("98.7650")
    assert str(prefill[PREFILL_FIELD_PRICE_PER_UNIT]) == "98.7650"
    assert prefill[PREFILL_FIELD_FEES] == Decimal("12.50")
    assert prefill[PREFILL_FIELD_TAXES] == Decimal("3.25")
    assert prefill[PREFILL_FIELD_CURRENCY] == "EUR"
    assert prefill[PREFILL_FIELD_TRADE_DATE] == date(2026, 9, 4)
    assert prefill[PREFILL_FIELD_SETTLEMENT_DATE] == date(2026, 9, 8)


def test_decimals_are_the_very_objects_from_the_confirmation() -> None:
    """No copy, no quantise: the parsed Decimal is what the composer receives."""
    fill = _fill(units=Decimal("0.12345678"))
    prefill = fill_to_prefill(fill)
    assert prefill[PREFILL_FIELD_UNITS] is fill.units
    assert str(prefill[PREFILL_FIELD_UNITS]) == "0.12345678"


def test_absent_settlement_date_maps_to_none() -> None:
    """The one default the mapping allows itself, and it is ``None``."""
    prefill = fill_to_prefill(_fill(settlement_date=None))
    assert PREFILL_FIELD_SETTLEMENT_DATE in prefill
    assert prefill[PREFILL_FIELD_SETTLEMENT_DATE] is None


def test_isin_becomes_a_master_data_identifier_pair() -> None:
    """A stated ISIN lands where the ticket layer looks for an identifier."""
    prefill = fill_to_prefill(_fill())
    assert prefill[PREFILL_FIELD_MASTER_DATA] == {
        MD_IDENTIFIER_SCHEME: IDENTIFIER_SCHEME_ISIN,
        MD_IDENTIFIER_VALUE: "DE0001234567",
    }


def test_absent_isin_omits_master_data_entirely() -> None:
    """No ISIN means no master-data key — not an empty one."""
    prefill = fill_to_prefill(_fill(isin=None))
    assert PREFILL_FIELD_MASTER_DATA not in prefill


def test_each_call_returns_a_fresh_dict() -> None:
    """The mapping holds no state; a caller may mutate what it is handed."""
    fill = _fill()
    first = fill_to_prefill(fill)
    second = fill_to_prefill(fill)
    assert first == second
    assert first is not second
    assert first[PREFILL_FIELD_MASTER_DATA] is not second[PREFILL_FIELD_MASTER_DATA]

    first[PREFILL_FIELD_UNITS] = Decimal("0")
    assert fill_to_prefill(fill)[PREFILL_FIELD_UNITS] == Decimal("1250.00000000")
