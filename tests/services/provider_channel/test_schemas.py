# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Envelope and confirmation parsing (ADR-0129 §3, Stage A).

The parsers are the channel's border control: everything that arrives from
outside this instance passes through them, and what they refuse can never
reach a ticket. These tests pin the four refusals that matter — an unreadable
version, an unknown key, a JSON number where money belongs, and a naive
timestamp — plus the round trip that keeps the serialiser honest against the
parser.

The sealed export's two shapes (ADR-0129 §6) are held to the same standard,
and add refusals of their own: a payload that names no instrument, half an
identifier, a key that is not 64 lower-case hex characters, and a ciphertext
too short to be a sealed box.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from services.provider_channel.directory import KEY_TYPE_X25519_SEALED_BOX
from services.provider_channel.schemas import (
    ENVELOPE_SCHEMA_VERSION,
    ENVELOPE_STATUS_EXECUTED,
    ENVELOPE_STATUS_SENT,
    ENVELOPE_STATUSES,
    EXPORT_DIRECTION_BUY,
    EXPORT_SCHEMA_VERSION,
    FILL_SCHEMA_VERSION,
    MESSAGE_TYPE_ORDER,
    Envelope,
    ExportEnvelope,
    ExportPayload,
    FillPayload,
    SchemaError,
    UnknownSchemaVersion,
    envelope_to_dict,
    export_envelope_to_dict,
    export_to_dict,
    fill_to_dict,
    parse_envelope,
    parse_export,
    parse_export_envelope,
    parse_fill,
)

_CREATED_AT = datetime(2026, 9, 7, 10, 30, tzinfo=timezone.utc)
_UPDATED_AT = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc)
_EXPORTED_AT = datetime(2026, 9, 16, 9, 30, tzinfo=timezone.utc)

#: A throwaway recipient key. Only its *shape* is under test here — this
#: module parses documents and seals nothing — so 64 hex characters that were
#: never a key will do.
_RECIPIENT_KEY = "a4ed736c37fe8a061f44389a143a7dc02d3de83905d2eb889e1edb17ed619e38"

#: The shortest legal ciphertext: an empty sealed box, all zero bytes.
_MINIMUM_CIPHERTEXT = base64.b64encode(b"\x00" * 48).decode("ascii")


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


def _export_dict(**overrides: object) -> dict[str, object]:
    """A valid sealed-export plaintext, with per-test overrides applied."""
    document: dict[str, object] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "message_type": MESSAGE_TYPE_ORDER,
        "correlation_id": "6f1b7f5e-1f5a-4a6f-9d3a-2a1f0c9b8e77",
        "ticket_kind": "order",
        "direction": EXPORT_DIRECTION_BUY,
        "identifier_scheme": "isin",
        "identifier_value": "DE0001234567",
        "instrument_name": None,
        "units": "500",
        "price_per_unit": "101.25",
        "gross_amount": "50625.00",
        "fees": "7.50",
        "taxes": "0.00",
        "currency": "EUR",
        "trade_date": "2026-09-16",
        "settlement_date": "2026-09-18",
        "message": None,
    }
    document.update(overrides)
    return document


def _export_envelope_dict(**overrides: object) -> dict[str, object]:
    """A valid sealed-export envelope, with per-test overrides applied."""
    document: dict[str, object] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "provider_id": "alpha-broker",
        "encryption_key_type": KEY_TYPE_X25519_SEALED_BOX,
        "recipient_public_key": _RECIPIENT_KEY,
        "sender_tenant_handle": "minathena-capital",
        "correlation_id": "6f1b7f5e-1f5a-4a6f-9d3a-2a1f0c9b8e77",
        "message_type": MESSAGE_TYPE_ORDER,
        "exported_at": _EXPORTED_AT.isoformat(),
        "ciphertext": _MINIMUM_CIPHERTEXT,
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


# ---------------------------------------------------------------------------
# S-5 — the sealed export's plaintext
# ---------------------------------------------------------------------------


def test_export_round_trips_through_dict() -> None:
    """S-5: ``export_to_dict`` and ``parse_export`` are mutual inverses."""
    parsed = parse_export(_export_dict())
    assert isinstance(parsed, ExportPayload)
    assert parsed.schema_version == EXPORT_SCHEMA_VERSION
    assert parsed.message_type == MESSAGE_TYPE_ORDER
    assert parsed.ticket_kind == "order"
    assert parsed.direction == EXPORT_DIRECTION_BUY
    assert parsed.identifier_scheme == "isin"
    assert parsed.identifier_value == "DE0001234567"
    assert parsed.units == Decimal("500")
    assert parsed.gross_amount == Decimal("50625.00")
    assert parsed.currency == "EUR"
    assert parsed.trade_date == date(2026, 9, 16)
    assert parsed.settlement_date == date(2026, 9, 18)

    assert parse_export(export_to_dict(parsed)) == parsed


def test_export_preserves_decimal_digits_exactly() -> None:
    """S-5: the payload's digits are the ticket's digits, not a float's."""
    parsed = parse_export(_export_dict(units="0.12345678"))
    assert parsed.units == Decimal("0.12345678")
    assert export_to_dict(parsed)["units"] == "0.12345678"


def test_export_refuses_a_json_number_for_money() -> None:
    """S-5: money arrives as a string here too; a JSON number is refused."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export(_export_dict(units=500.0))
    assert "units" in str(excinfo.value)


def test_export_refuses_a_direction_outside_the_vocabulary() -> None:
    """S-5: an order is bought or sold, and the parser knows no third verb."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export(_export_dict(direction="short"))
    assert "direction" in str(excinfo.value)
    assert "short" in str(excinfo.value)


def test_export_refuses_a_ticket_kind_outside_the_vocabulary() -> None:
    """S-5: ``ticket_kind`` is the directory's closed vocabulary, not free text."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export(_export_dict(ticket_kind="rebalance"))
    assert "ticket_kind" in str(excinfo.value)
    assert "rebalance" in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"identifier_value": None},
        {"identifier_scheme": None},
    ],
    ids=["scheme_without_value", "value_without_scheme"],
)
def test_export_refuses_half_an_identifier(overrides: dict[str, object]) -> None:
    """S-5: an identifier is a pair. Half of one names nothing and is refused."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export(_export_dict(instrument_name="Alpha Fund SICAV", **overrides))
    assert "identifier_scheme" in str(excinfo.value)
    assert "identifier_value" in str(excinfo.value)


def test_export_refuses_a_payload_that_names_no_instrument() -> None:
    """S-5: an order nobody can act on is not an order.

    Neither an identifier pair nor a name leaves the provider with a
    direction, a quantity and no idea what to trade.
    """
    with pytest.raises(SchemaError) as excinfo:
        parse_export(
            _export_dict(identifier_scheme=None, identifier_value=None, instrument_name=None)
        )
    assert "names no instrument" in str(excinfo.value)


def test_export_accepts_an_instrument_name_alone() -> None:
    """S-5: an instrument no identifier reaches may be named in words."""
    parsed = parse_export(
        _export_dict(
            identifier_scheme=None,
            identifier_value=None,
            instrument_name="Alpha Secondaries Fund III",
        )
    )
    assert parsed.identifier_scheme is None
    assert parsed.instrument_name == "Alpha Secondaries Fund III"


def test_export_refuses_settlement_before_trade() -> None:
    """S-5: the same rule the confirmation obeys, on the way out."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export(_export_dict(trade_date="2026-09-16", settlement_date="2026-09-15"))
    assert "settlement_date" in str(excinfo.value)


def test_export_optional_fields_may_be_absent_or_null() -> None:
    """S-5: everything but routing, kind and currency may be left unsaid.

    An indication of interest states a direction and an instrument and no
    numbers at all; the shape has to allow that without a second schema.
    """
    document = _export_dict(instrument_name="Alpha Secondaries Fund III")
    for key in (
        "direction",
        "identifier_scheme",
        "identifier_value",
        "units",
        "price_per_unit",
        "gross_amount",
        "fees",
        "taxes",
        "trade_date",
        "settlement_date",
        "message",
    ):
        del document[key]
    parsed = parse_export(document)
    assert parsed.units is None
    assert parsed.gross_amount is None
    assert parsed.trade_date is None

    explicit_nulls = parse_export(
        _export_dict(
            instrument_name="Alpha Secondaries Fund III",
            direction=None,
            identifier_scheme=None,
            identifier_value=None,
            units=None,
            price_per_unit=None,
            gross_amount=None,
            fees=None,
            taxes=None,
            trade_date=None,
            settlement_date=None,
            message=None,
        )
    )
    assert explicit_nulls == parsed


def test_export_refuses_unknown_extra_key() -> None:
    """S-5: an unknown key is refused and named, as everywhere else here.

    ``net_amount`` is the one chosen on purpose: there is no such field, and
    a payload that carried one would be arithmetic done in the wrong layer
    (D-amounts).
    """
    with pytest.raises(SchemaError) as excinfo:
        parse_export(_export_dict(net_amount="50632.50"))
    assert "net_amount" in str(excinfo.value)


def test_export_refuses_missing_schema_version() -> None:
    """S-5: an undeclared export version is refused rather than assumed."""
    document = _export_dict()
    del document["schema_version"]
    with pytest.raises(UnknownSchemaVersion) as excinfo:
        parse_export(document)
    assert excinfo.value.shape == "export"
    assert excinfo.value.version is None


def test_export_refuses_unknown_schema_version() -> None:
    """S-5: a version this build cannot read is refused, naming it."""
    with pytest.raises(UnknownSchemaVersion) as excinfo:
        parse_export(_export_dict(schema_version=2))
    assert excinfo.value.shape == "export"
    assert excinfo.value.version == 2


# ---------------------------------------------------------------------------
# S-6 — the sealed export's envelope
# ---------------------------------------------------------------------------


def test_export_envelope_round_trips_through_dict() -> None:
    """S-6: the artefact on disk reads back as the artefact in memory."""
    parsed = parse_export_envelope(_export_envelope_dict())
    assert isinstance(parsed, ExportEnvelope)
    assert parsed.provider_id == "alpha-broker"
    assert parsed.encryption_key_type == KEY_TYPE_X25519_SEALED_BOX
    assert parsed.recipient_public_key == _RECIPIENT_KEY
    assert parsed.sender_tenant_handle == "minathena-capital"
    assert parsed.exported_at == _EXPORTED_AT
    assert parsed.ciphertext == _MINIMUM_CIPHERTEXT

    assert parse_export_envelope(export_envelope_to_dict(parsed)) == parsed


def test_export_envelope_refuses_upper_case_hex() -> None:
    """S-6: a key has one spelling, so a portal needs no normalisation step."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export_envelope(_export_envelope_dict(recipient_public_key=_RECIPIENT_KEY.upper()))
    assert "recipient_public_key" in str(excinfo.value)


def test_export_envelope_refuses_a_short_key() -> None:
    """S-6: 63 hex characters is not a 32-byte key, however plausible it looks."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export_envelope(_export_envelope_dict(recipient_public_key=_RECIPIENT_KEY[:63]))
    assert "recipient_public_key" in str(excinfo.value)


def test_export_envelope_refuses_a_ciphertext_that_is_not_base64() -> None:
    """S-6: the ciphertext is standard-alphabet base64 or it is nothing."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export_envelope(_export_envelope_dict(ciphertext="not base64!"))
    assert "ciphertext" in str(excinfo.value)


def test_export_envelope_refuses_a_ciphertext_too_short_to_be_a_sealed_box() -> None:
    """S-6: 47 bytes cannot hold an ephemeral key and a MAC, let alone a message."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export_envelope(
            _export_envelope_dict(ciphertext=base64.b64encode(b"\x00" * 47).decode("ascii"))
        )
    assert "ciphertext" in str(excinfo.value)
    assert "47" in str(excinfo.value)


def test_export_envelope_refuses_a_naive_exported_at() -> None:
    """S-6: the artefact crosses time zones; its one timestamp carries an offset."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export_envelope(
            _export_envelope_dict(exported_at=datetime(2026, 9, 16, 9, 30).isoformat())
        )
    assert "exported_at" in str(excinfo.value)


def test_export_envelope_refuses_an_unknown_encryption_key_type() -> None:
    """S-6: a key family this build does not implement is refused at the border."""
    with pytest.raises(SchemaError) as excinfo:
        parse_export_envelope(_export_envelope_dict(encryption_key_type="rsa-oaep"))
    assert "rsa-oaep" in str(excinfo.value)


def test_export_envelope_accepts_a_null_sender_handle() -> None:
    """S-6: version 1 allows the absence — no handle is issued before B-2."""
    parsed = parse_export_envelope(_export_envelope_dict(sender_tenant_handle=None))
    assert parsed.sender_tenant_handle is None
    assert export_envelope_to_dict(parsed)["sender_tenant_handle"] is None
