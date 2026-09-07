# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Envelope and confirmation parsing (ADR-0129 §3, Stage A).

The parsers are the channel's border control: everything that arrives from
outside this instance passes through them, and what they refuse can never
reach a ticket. These tests pin the four refusals that matter — an unreadable
version, an unknown key, a JSON number where money belongs, and a naive
timestamp — plus the round trip that keeps the serialiser honest against the
parser.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services.provider_channel.schemas import (
    ENVELOPE_SCHEMA_VERSION,
    ENVELOPE_STATUS_EXECUTED,
    ENVELOPE_STATUS_SENT,
    ENVELOPE_STATUSES,
    FILL_SCHEMA_VERSION,
    MESSAGE_TYPE_ORDER,
    Envelope,
    FillPayload,
    SchemaError,
    UnknownSchemaVersion,
    envelope_to_dict,
    fill_to_dict,
    parse_envelope,
    parse_fill,
)

_CREATED_AT = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
_UPDATED_AT = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc)


def _envelope_dict(**overrides: object) -> dict[str, object]:
    """A valid envelope document, with per-test overrides applied."""
    document: dict[str, object] = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "sender_tenant_handle": "minathena-capital",
        "provider_id": "alpha-broker",
        "message_type": MESSAGE_TYPE_ORDER,
        "correlation_id": "6f1b7f5e-1f5a-4a6f-9d3a-2a1f0c9b8e77",
        "status": ENVELOPE_STATUS_SENT,
        "created_at": _CREATED_AT.isoformat(),
        "updated_at": _UPDATED_AT.isoformat(),
    }
    document.update(overrides)
    return document


def _fill_dict(**overrides: object) -> dict[str, object]:
    """A valid confirmation document, with per-test overrides applied."""
    document: dict[str, object] = {
        "schema_version": FILL_SCHEMA_VERSION,
        "units": "1250.00000000",
        "price_per_unit": "98.7650",
        "fees": "12.50",
        "taxes": "0.00",
        "currency": "EUR",
        "trade_date": "2026-09-04",
        "settlement_date": "2026-09-08",
        "isin": "DE0001234567",
        "message": "Filled in two tranches.",
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# S-1
# ---------------------------------------------------------------------------


def test_envelope_round_trips_through_dict() -> None:
    """S-1: ``envelope_to_dict`` and ``parse_envelope`` are mutual inverses."""
    parsed = parse_envelope(_envelope_dict())
    assert isinstance(parsed, Envelope)
    assert parsed.schema_version == ENVELOPE_SCHEMA_VERSION
    assert parsed.sender_tenant_handle == "minathena-capital"
    assert parsed.provider_id == "alpha-broker"
    assert parsed.message_type == MESSAGE_TYPE_ORDER
    assert parsed.status == ENVELOPE_STATUS_SENT
    assert parsed.created_at == _CREATED_AT
    assert parsed.updated_at == _UPDATED_AT

    round_tripped = parse_envelope(envelope_to_dict(parsed))
    assert round_tripped == parsed


@pytest.mark.parametrize("status", sorted(ENVELOPE_STATUSES))
def test_envelope_accepts_every_declared_status(status: str) -> None:
    """S-1: each of the four envelope statuses parses."""
    assert parse_envelope(_envelope_dict(status=status)).status == status


def test_envelope_refuses_a_book_state() -> None:
    """S-1: the envelope never carries a book state (ADR-0129 §3).

    The channel *informs*; booking stays a reviewed act on this side, so
    ``booked`` has no meaning on a message and is refused outright.
    """
    with pytest.raises(SchemaError) as excinfo:
        parse_envelope(_envelope_dict(status="booked"))
    assert "booked" in str(excinfo.value)


# ---------------------------------------------------------------------------
# S-2
# ---------------------------------------------------------------------------


def test_envelope_refuses_missing_schema_version() -> None:
    """S-2: an undeclared version is refused rather than assumed."""
    document = _envelope_dict()
    del document["schema_version"]
    with pytest.raises(UnknownSchemaVersion) as excinfo:
        parse_envelope(document)
    assert excinfo.value.shape == "envelope"
    assert excinfo.value.version is None
    assert "schema_version" in str(excinfo.value)


def test_envelope_refuses_unknown_schema_version() -> None:
    """S-2: an unreadable version is refused loudly, naming the version."""
    with pytest.raises(UnknownSchemaVersion) as excinfo:
        parse_envelope(_envelope_dict(schema_version=7))
    assert excinfo.value.shape == "envelope"
    assert excinfo.value.version == 7
    assert "7" in str(excinfo.value)


def test_envelope_refuses_unknown_message_type() -> None:
    """S-2: ``message_type`` is a closed vocabulary."""
    with pytest.raises(SchemaError) as excinfo:
        parse_envelope(_envelope_dict(message_type="settlement"))
    assert "settlement" in str(excinfo.value)


def test_envelope_refuses_unknown_extra_key() -> None:
    """S-2: an unknown key is refused and named.

    A forward-compatible reader is a Stage-B decision with its own
    compatibility story — not a Stage-A default that drops fields silently.
    """
    with pytest.raises(SchemaError) as excinfo:
        parse_envelope(_envelope_dict(payload_ciphertext="AAAA"))
    assert "payload_ciphertext" in str(excinfo.value)


def test_envelope_refuses_naive_timestamps() -> None:
    """S-2: a timestamp without an offset is refused."""
    naive = datetime(2026, 9, 7, 10, 30).isoformat()
    with pytest.raises(SchemaError) as excinfo:
        parse_envelope(_envelope_dict(created_at=naive))
    assert "created_at" in str(excinfo.value)

    with pytest.raises(SchemaError):
        parse_envelope(_envelope_dict(updated_at=naive))


# ---------------------------------------------------------------------------
# S-3
# ---------------------------------------------------------------------------


def test_fill_round_trips_through_dict() -> None:
    """S-3: ``fill_to_dict`` and ``parse_fill`` are mutual inverses."""
    parsed = parse_fill(_fill_dict())
    assert isinstance(parsed, FillPayload)
    assert parsed.units == Decimal("1250.00000000")
    assert parsed.price_per_unit == Decimal("98.7650")
    assert parsed.fees == Decimal("12.50")
    assert parsed.taxes == Decimal("0.00")
    assert parsed.currency == "EUR"
    assert parsed.trade_date == date(2026, 9, 4)
    assert parsed.settlement_date == date(2026, 9, 8)
    assert parsed.isin == "DE0001234567"
    assert parsed.message == "Filled in two tranches."

    assert parse_fill(fill_to_dict(parsed)) == parsed


def test_fill_preserves_decimal_digits_exactly() -> None:
    """S-3: eight decimal places survive a round trip unchanged.

    The string encoding exists so a provider's digits reach the ticket
    unrounded; a float would have lost them before this assertion ran.
    """
    parsed = parse_fill(_fill_dict(units="0.12345678"))
    assert parsed.units == Decimal("0.12345678")
    assert str(parsed.units) == "0.12345678"
    assert fill_to_dict(parsed)["units"] == "0.12345678"


def test_fill_refuses_a_json_number_for_money() -> None:
    """S-3: money must arrive as a string; a JSON number is refused."""
    with pytest.raises(SchemaError) as excinfo:
        parse_fill(_fill_dict(units=1250.0))
    assert "units" in str(excinfo.value)

    with pytest.raises(SchemaError):
        parse_fill(_fill_dict(price_per_unit=98))


def test_fill_refuses_settlement_before_trade() -> None:
    """S-3: a settlement date may not precede the trade date."""
    with pytest.raises(SchemaError) as excinfo:
        parse_fill(_fill_dict(trade_date="2026-09-04", settlement_date="2026-09-03"))
    assert "settlement_date" in str(excinfo.value)


def test_fill_accepts_settlement_equal_to_trade_date() -> None:
    """S-3: same-day settlement is legal — the rule is 'not before'."""
    parsed = parse_fill(_fill_dict(trade_date="2026-09-04", settlement_date="2026-09-04"))
    assert parsed.settlement_date == date(2026, 9, 4)


def test_fill_refuses_lower_case_currency() -> None:
    """S-3: the currency code is three upper-case letters."""
    with pytest.raises(SchemaError) as excinfo:
        parse_fill(_fill_dict(currency="eur"))
    assert "currency" in str(excinfo.value)


def test_fill_optional_fields_may_be_absent_or_null() -> None:
    """S-3: ``settlement_date``, ``isin`` and ``message`` are optional."""
    document = _fill_dict()
    for key in ("settlement_date", "isin", "message"):
        del document[key]
    parsed = parse_fill(document)
    assert parsed.settlement_date is None
    assert parsed.isin is None
    assert parsed.message is None

    explicit_nulls = parse_fill(_fill_dict(settlement_date=None, isin=None, message=None))
    assert explicit_nulls == parsed


# ---------------------------------------------------------------------------
# S-4
# ---------------------------------------------------------------------------


def test_fill_refuses_unknown_schema_version() -> None:
    """S-4: the confirmation carries its own version gate."""
    with pytest.raises(UnknownSchemaVersion) as excinfo:
        parse_fill(_fill_dict(schema_version=99))
    assert excinfo.value.shape == "fill"
    assert excinfo.value.version == 99
    assert "99" in str(excinfo.value)


def test_fill_refuses_missing_schema_version() -> None:
    """S-4: an undeclared confirmation version is refused too."""
    document = _fill_dict()
    del document["schema_version"]
    with pytest.raises(UnknownSchemaVersion) as excinfo:
        parse_fill(document)
    assert excinfo.value.version is None


def test_executed_status_is_readable_on_an_envelope() -> None:
    """The status that arms the pre-fill parses on an envelope like any other."""
    assert parse_envelope(_envelope_dict(status=ENVELOPE_STATUS_EXECUTED)).status == "executed"
