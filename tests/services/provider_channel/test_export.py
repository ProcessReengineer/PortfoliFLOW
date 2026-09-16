# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Sealing an order into an export envelope (ADR-0129 §6, Stage B).

Two things are being pinned here, and the second matters more than the first.

The first is that the seam works: a payload sealed to a directory entry's key
opens, with that entry's private key, to exactly the bytes
``export_plaintext_bytes`` produced — which is what makes a portal able to
check what it decrypted.

The second is the **leak guard**. ADR-0129 §6 says the export carries the
order's own parameters and nothing else, and an artefact is only as private
as its outside: every payload value except the three the envelope is entitled
to repeat must be absent from the envelope's fields *and* absent as a
substring of its base64 ciphertext. The second half of that would catch the
worst version of the mistake — a "convenience" plaintext copy appended
somewhere — which no amount of reading the dataclass would.

Keys are generated per test and thrown away (B-D-20).
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Final

import pytest
from nacl.public import PrivateKey

from services.provider_channel.directory import (
    KEY_TYPE_X25519_SEALED_BOX,
    ProviderEntry,
    canonical_bytes,
)
from services.provider_channel.export import (
    UnsupportedEncryptionKeyType,
    export_plaintext_bytes,
    seal_export,
)
from services.provider_channel.schemas import (
    EXPORT_DIRECTION_BUY,
    EXPORT_SCHEMA_VERSION,
    MESSAGE_TYPE_ORDER,
    ExportPayload,
    SchemaError,
    export_envelope_to_dict,
    export_to_dict,
    parse_export,
    parse_export_envelope,
)
from services.provider_channel.sealed_box import open_sealed

_EXPORTED_AT: Final[datetime] = datetime(2026, 9, 16, 9, 30, tzinfo=timezone.utc)

#: The envelope is entitled to restate exactly these: without them a returning
#: confirmation matches nothing, and none of them says what is being traded.
_ROUTING_KEYS: Final[frozenset[str]] = frozenset(
    {"correlation_id", "message_type", "schema_version"}
)

#: Shortest payload value the substring half of the leak guard is asserted on.
#: A ciphertext is ~670 characters of base64, so a short value drawn from that
#: same alphabet turns up in one by chance often enough to matter: measured
#: over 20,000 seals, ``"EUR"``, ``"500"`` and ``"buy"`` each appeared in about
#: 0.22% of them. At six characters the chance is below one in a hundred
#: million, so a hit is a leak rather than an accident. Nothing is lost by the
#: threshold: every value, short or long, is still covered by the
#: set-membership half above, values containing ``.`` or ``-`` could never
#: collide anyway (neither character is in the standard base64 alphabet), and
#: a plaintext copy appended to an artefact would carry the long values too.
_SUBSTRING_GUARD_MIN_LENGTH: Final[int] = 6


def _payload(**overrides: object) -> ExportPayload:
    """A complete order payload, with per-test overrides applied."""
    fields: dict[str, object] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "message_type": MESSAGE_TYPE_ORDER,
        "correlation_id": "6f1b7f5e-1f5a-4a6f-9d3a-2a1f0c9b8e77",
        "ticket_kind": "order",
        "direction": EXPORT_DIRECTION_BUY,
        "identifier_scheme": "isin",
        "identifier_value": "DE0001234567",
        "instrument_name": None,
        "units": Decimal("500"),
        "price_per_unit": Decimal("101.25"),
        "gross_amount": Decimal("50625.00"),
        "fees": Decimal("7.50"),
        "taxes": Decimal("0.00"),
        "currency": "EUR",
        "trade_date": date(2026, 9, 16),
        "settlement_date": date(2026, 9, 18),
        "message": None,
    }
    fields.update(overrides)
    return ExportPayload(**fields)  # type: ignore[arg-type]


def _provider(public_key_hex: str, **overrides: object) -> ProviderEntry:
    """A directory entry for the recipient, built directly rather than parsed.

    The dataclass validates nothing — that is the parser's job — which is
    exactly what lets a test hand :func:`seal_export` an entry no directory
    would ever carry, and see it refused.
    """
    fields: dict[str, object] = {
        "provider_id": "alpha-broker",
        "display_name": "Alpha Broker AG",
        "provider_type": "broker",
        "ticket_kinds": frozenset({"order"}),
        "engagement_categories": frozenset(),
        "asset_classes": frozenset({"listed_equity"}),
        "jurisdictions": frozenset({"DE"}),
        "encryption_key_type": KEY_TYPE_X25519_SEALED_BOX,
        "encryption_public_key": public_key_hex,
    }
    fields.update(overrides)
    return ProviderEntry(**fields)  # type: ignore[arg-type]


@pytest.fixture
def private_key() -> bytes:
    """A throwaway X25519 private key, minted per test (B-D-20)."""
    return bytes(PrivateKey.generate())


@pytest.fixture
def provider(private_key: bytes) -> ProviderEntry:
    """A directory entry whose key is the public half of :func:`private_key`."""
    return _provider(bytes(PrivateKey(private_key).public_key).hex())


# ---------------------------------------------------------------------------
# The envelope's routing
# ---------------------------------------------------------------------------


def test_the_envelope_routes_by_the_entry_and_the_payload(provider: ProviderEntry) -> None:
    """Recipient facts come from the directory entry, correlation from the payload."""
    payload = _payload()
    envelope = seal_export(
        payload,
        provider=provider,
        sender_tenant_handle="minathena-capital",
        exported_at=_EXPORTED_AT,
    )

    assert envelope.schema_version == EXPORT_SCHEMA_VERSION
    assert envelope.provider_id == provider.provider_id
    assert envelope.encryption_key_type == provider.encryption_key_type
    assert envelope.recipient_public_key == provider.encryption_public_key
    assert envelope.correlation_id == payload.correlation_id
    assert envelope.message_type == payload.message_type
    assert envelope.exported_at == _EXPORTED_AT
    assert envelope.sender_tenant_handle == "minathena-capital"


# ---------------------------------------------------------------------------
# The seal itself
# ---------------------------------------------------------------------------


def test_the_ciphertext_opens_to_the_plaintext_bytes(
    provider: ProviderEntry, private_key: bytes
) -> None:
    """The recipient recovers exactly the bytes this build sealed."""
    payload = _payload()
    envelope = seal_export(
        payload, provider=provider, sender_tenant_handle=None, exported_at=_EXPORTED_AT
    )

    opened = open_sealed(base64.b64decode(envelope.ciphertext, validate=True), private_key)
    assert opened == export_plaintext_bytes(payload)


def test_the_plaintext_is_the_directorys_canonical_form() -> None:
    """One canonical form for the whole package, reused rather than restated."""
    payload = _payload()
    assert export_plaintext_bytes(payload) == canonical_bytes(export_to_dict(payload))


def test_the_opened_bytes_parse_back_to_an_equal_payload(
    provider: ProviderEntry, private_key: bytes
) -> None:
    """Nothing is lost in the round trip — not a digit, not an absent optional."""
    payload = _payload()
    envelope = seal_export(
        payload, provider=provider, sender_tenant_handle=None, exported_at=_EXPORTED_AT
    )

    opened = open_sealed(base64.b64decode(envelope.ciphertext, validate=True), private_key)
    assert parse_export(json.loads(opened.decode("utf-8"))) == payload


# ---------------------------------------------------------------------------
# The leak guard (ADR-0129 §6)
# ---------------------------------------------------------------------------


def test_the_envelope_leaks_no_payload_value(provider: ProviderEntry) -> None:
    """Nothing the payload states appears outside the ciphertext, or inside it in clear.

    The substring half of this assertion is the one that earns its keep: it
    fails if anyone ever appends a readable copy of the payload to the
    ciphertext for convenience.
    """
    # ``ticket_kind`` is deliberately not "order" here: that literal is also
    # the ``message_type`` the envelope is entitled to restate, and one
    # spelling shared by a guarded field and an exempt one would un-guard the
    # field rather than exempt the value.
    payload = _payload(
        ticket_kind="secondary",
        message="Please work this order at the stated limit.",
    )
    envelope = seal_export(
        payload,
        provider=provider,
        sender_tenant_handle="minathena-capital",
        exported_at=_EXPORTED_AT,
    )

    guarded = {
        value
        for key, value in export_to_dict(payload).items()
        if value is not None and key not in _ROUTING_KEYS
    }
    assert guarded, "the guard would pass vacuously on an empty payload"

    envelope_values = set(export_envelope_to_dict(envelope).values())
    assert not (guarded & envelope_values)

    checked = 0
    for value in guarded:
        if isinstance(value, str) and len(value) >= _SUBSTRING_GUARD_MIN_LENGTH:
            assert value not in envelope.ciphertext
            checked += 1
    assert checked >= 5, "the substring half of the guard must not go vacuous"


# ---------------------------------------------------------------------------
# Refusals and optionals
# ---------------------------------------------------------------------------


def test_an_absent_sender_handle_is_emitted_as_null(provider: ProviderEntry) -> None:
    """Version 1 allows the absence: no handle is issued before B-2."""
    envelope = seal_export(
        _payload(), provider=provider, sender_tenant_handle=None, exported_at=_EXPORTED_AT
    )

    assert envelope.sender_tenant_handle is None
    assert export_envelope_to_dict(envelope)["sender_tenant_handle"] is None
    assert parse_export_envelope(export_envelope_to_dict(envelope)) == envelope


def test_a_naive_exported_at_is_refused_in_the_parsers_words(provider: ProviderEntry) -> None:
    """The refusal comes from the parser, not from a second check written here."""
    with pytest.raises(SchemaError) as excinfo:
        seal_export(
            _payload(),
            provider=provider,
            sender_tenant_handle=None,
            exported_at=datetime(2026, 9, 16, 9, 30),
        )
    assert "exported_at" in str(excinfo.value)


def test_an_unsupported_key_type_is_refused_before_any_cipher_call() -> None:
    """A key family this build cannot seal to gets no export at all."""
    entry = _provider("00" * 32, encryption_key_type="rsa-oaep")

    with pytest.raises(UnsupportedEncryptionKeyType) as excinfo:
        seal_export(_payload(), provider=entry, sender_tenant_handle=None, exported_at=_EXPORTED_AT)

    message = str(excinfo.value)
    assert "rsa-oaep" in message
    assert KEY_TYPE_X25519_SEALED_BOX in message


def test_the_envelope_round_trips_through_its_dict(provider: ProviderEntry) -> None:
    """The artefact on disk reads back as the artefact in memory."""
    envelope = seal_export(
        _payload(),
        provider=provider,
        sender_tenant_handle="minathena-capital",
        exported_at=_EXPORTED_AT,
    )
    assert parse_export_envelope(export_envelope_to_dict(envelope)) == envelope
