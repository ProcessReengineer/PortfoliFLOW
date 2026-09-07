# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The directory trust gate (ADR-0129 §2, Stage A).

The directory says which counterparties an instance may talk to, so the only
interesting question about it is whether it can be believed. These tests walk
the gate's seven refusals one at a time — placeholder key, non-canonical
bytes, unreadable format, unsupported scheme, bad signature, stale window,
bad shape — because a gate that fails for the wrong reason is a gate whose
logs cannot be trusted either.

The key pair is generated per module run: no key material is committed, and
the production private key never lives in this repository.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.provider_channel.directory import (
    DIRECTORY_FORMAT_VERSION,
    KEY_TYPE_X25519_SEALED_BOX,
    SIGNATURE_SCHEME_ED25519,
    Directory,
    DirectoryExpired,
    DirectoryShapeError,
    InvalidDirectorySignature,
    ProviderEntry,
    PublishingKeyNotConfigured,
    SuccessorKey,
    UnknownDirectoryFormatVersion,
    UnsupportedSignatureScheme,
    canonical_bytes,
    sign_directory,
    verify_directory,
)
from services.provider_channel.publishing_key import PUBLISHING_KEY, is_placeholder

_SIGNING_KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.generate()
_PRIVATE_BYTES: Final[bytes] = _SIGNING_KEY.private_bytes_raw()
_PUBLIC_BYTES: Final[bytes] = _SIGNING_KEY.public_key().public_bytes_raw()

_OTHER_KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.generate()
_OTHER_PUBLIC_BYTES: Final[bytes] = _OTHER_KEY.public_key().public_bytes_raw()

#: Injected rather than read from a clock (D-clock).
_NOW: Final[date] = date(2026, 9, 7)

_HEX_A: Final[str] = "a" * 64
_HEX_B: Final[str] = "b" * 64
_HEX_C: Final[str] = "c" * 64


def _provider(provider_id: str = "alpha-broker", **overrides: object) -> dict[str, object]:
    """A valid provider entry, with per-test overrides applied."""
    entry: dict[str, object] = {
        "provider_id": provider_id,
        "display_name": "Alpha Broker",
        "provider_type": "broker",
        "ticket_kinds": ["order", "secondary"],
        "engagement_categories": [],
        "asset_classes": ["listed_equity"],
        "jurisdictions": ["DE", "LU"],
        "encryption_key_type": KEY_TYPE_X25519_SEALED_BOX,
        "encryption_public_key": _HEX_A,
    }
    entry.update(overrides)
    return entry


def _document(**overrides: object) -> dict[str, object]:
    """A valid directory document, with per-test overrides applied."""
    document: dict[str, object] = {
        "format_version": DIRECTORY_FORMAT_VERSION,
        "directory_version": 12,
        "issued_at": "2026-09-01",
        "valid_until": "2026-10-01",
        "signature_scheme": SIGNATURE_SCHEME_ED25519,
        "publishing_key_id": "portfoliflow-2026-09",
        "providers": [
            _provider(
                "zeta-secondary",
                display_name="Zeta Secondary Desk",
                provider_type="secondary_desk",
                encryption_public_key=_HEX_B,
            ),
            _provider(),
        ],
    }
    document.update(overrides)
    return document


def _verify(
    document: dict[str, object], *, now: date = _NOW, publishing_key: bytes = _PUBLIC_BYTES
) -> Directory:
    """Sign ``document`` with the module key and verify it."""
    payload, signature = sign_directory(document, private_key=_PRIVATE_BYTES)
    return verify_directory(payload, signature, publishing_key=publishing_key, now=now)


# ---------------------------------------------------------------------------
# D-1
# ---------------------------------------------------------------------------


def test_valid_document_verifies_and_sorts_providers() -> None:
    """D-1: a correctly signed document verifies; entries come back sorted."""
    directory = _verify(_document())
    assert isinstance(directory, Directory)
    assert directory.format_version == DIRECTORY_FORMAT_VERSION
    assert directory.directory_version == 12
    assert directory.issued_at == date(2026, 9, 1)
    assert directory.valid_until == date(2026, 10, 1)
    assert directory.signature_scheme == SIGNATURE_SCHEME_ED25519
    assert directory.publishing_key_id == "portfoliflow-2026-09"
    assert directory.successor_key is None

    assert [entry.provider_id for entry in directory.providers] == [
        "alpha-broker",
        "zeta-secondary",
    ]
    alpha = directory.providers[0]
    assert isinstance(alpha, ProviderEntry)
    assert alpha.display_name == "Alpha Broker"
    assert alpha.provider_type == "broker"
    assert alpha.ticket_kinds == frozenset({"order", "secondary"})
    assert alpha.jurisdictions == frozenset({"DE", "LU"})
    assert alpha.encryption_key_type == KEY_TYPE_X25519_SEALED_BOX
    assert alpha.encryption_public_key == _HEX_A


# ---------------------------------------------------------------------------
# D-2 … D-5: the signature itself
# ---------------------------------------------------------------------------


def test_tampered_document_is_refused() -> None:
    """D-2: one flipped byte inside a display name breaks verification."""
    document = _document()
    payload, signature = sign_directory(document, private_key=_PRIVATE_BYTES)
    tampered = payload.replace(b"Alpha Broker", b"Alpha Brokes")
    assert tampered != payload
    assert len(tampered) == len(payload)

    with pytest.raises(InvalidDirectorySignature):
        verify_directory(tampered, signature, publishing_key=_PUBLIC_BYTES, now=_NOW)


@pytest.mark.parametrize(
    ("signature", "label"),
    [(b"", "empty"), (bytes(range(64)), "sixty-four arbitrary bytes")],
)
def test_unsigned_document_is_refused(signature: bytes, label: str) -> None:
    """D-3: an absent or invented signature never verifies."""
    payload = canonical_bytes(_document())
    with pytest.raises(InvalidDirectorySignature):
        verify_directory(payload, signature, publishing_key=_PUBLIC_BYTES, now=_NOW)


def test_signature_from_another_key_is_refused() -> None:
    """D-4: a valid signature by the wrong key is still the wrong key.

    This is the attack the trust model actually defends against: substituting
    the publishing key, not breaking the cipher.
    """
    payload, signature = sign_directory(_document(), private_key=_PRIVATE_BYTES)
    with pytest.raises(InvalidDirectorySignature):
        verify_directory(payload, signature, publishing_key=_OTHER_PUBLIC_BYTES, now=_NOW)


def test_non_canonical_bytes_are_refused_even_when_correctly_signed() -> None:
    """D-5: only the canonical spelling is verifiable.

    The document below is the same JSON, pretty-printed, and the signature
    over it is genuine — yet it is refused, because "which bytes were signed"
    must have exactly one answer.
    """
    document = _document()
    pretty = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
    assert pretty != canonical_bytes(document)
    signature = Ed25519PrivateKey.from_private_bytes(_PRIVATE_BYTES).sign(pretty)

    with pytest.raises(InvalidDirectorySignature) as excinfo:
        verify_directory(pretty, signature, publishing_key=_PUBLIC_BYTES, now=_NOW)
    assert "not in canonical form" in str(excinfo.value)


def test_signature_member_inside_the_document_is_refused() -> None:
    """The signature travels beside the document, never inside it."""
    payload = canonical_bytes(_document(signature="deadbeef"))
    signature = Ed25519PrivateKey.from_private_bytes(_PRIVATE_BYTES).sign(payload)
    with pytest.raises(InvalidDirectorySignature) as excinfo:
        verify_directory(payload, signature, publishing_key=_PUBLIC_BYTES, now=_NOW)
    assert "beside" in str(excinfo.value)


# ---------------------------------------------------------------------------
# D-6: version and scheme are judged before the signature
# ---------------------------------------------------------------------------


def test_unknown_format_version_is_refused_before_the_signature() -> None:
    """D-6: an unreadable format version is refused without checking the signature."""
    payload = canonical_bytes(_document(format_version=2))
    with pytest.raises(UnknownDirectoryFormatVersion) as excinfo:
        verify_directory(payload, b"not-a-signature", publishing_key=_PUBLIC_BYTES, now=_NOW)
    assert "2" in str(excinfo.value)


def test_unknown_signature_scheme_is_refused_before_the_signature() -> None:
    """D-6: an unsupported scheme is refused even with a bad signature attached.

    Passing deliberate garbage as the signature is what proves the order: had
    the gate verified first, this would raise
    :class:`InvalidDirectorySignature` instead.
    """
    payload = canonical_bytes(_document(signature_scheme="rsa-pss"))
    with pytest.raises(UnsupportedSignatureScheme) as excinfo:
        verify_directory(payload, b"not-a-signature", publishing_key=_PUBLIC_BYTES, now=_NOW)
    assert "rsa-pss" in str(excinfo.value)


# ---------------------------------------------------------------------------
# D-7: the validity window
# ---------------------------------------------------------------------------


def test_expired_document_is_refused() -> None:
    """D-7: past ``valid_until``, the document is stale."""
    with pytest.raises(DirectoryExpired) as excinfo:
        _verify(_document(), now=date(2026, 10, 2))
    assert "expired" in str(excinfo.value)


def test_not_yet_valid_document_is_refused() -> None:
    """D-7: before ``issued_at`` it is stale too — the client is not current."""
    with pytest.raises(DirectoryExpired) as excinfo:
        _verify(_document(), now=date(2026, 8, 31))
    assert "not yet valid" in str(excinfo.value)


@pytest.mark.parametrize("boundary", [date(2026, 9, 1), date(2026, 10, 1)])
def test_boundary_days_verify(boundary: date) -> None:
    """D-7: the window is inclusive at both ends."""
    assert _verify(_document(), now=boundary).directory_version == 12


# ---------------------------------------------------------------------------
# D-8: successor-key announcement
# ---------------------------------------------------------------------------


def test_successor_key_announcement_parses() -> None:
    """D-8: a successor announced inside a still-valid document is read.

    Acting on it is a Stage-B client rule; Stage A only carries it across.
    """
    document = _document(
        successor_key={
            "publishing_key_id": "portfoliflow-2027-01",
            "public_key": _HEX_C,
            "valid_from": "2027-01-01",
        }
    )
    successor = _verify(document).successor_key
    assert isinstance(successor, SuccessorKey)
    assert successor.publishing_key_id == "portfoliflow-2027-01"
    assert successor.public_key == _HEX_C
    assert successor.valid_from == date(2027, 1, 1)


def test_explicit_null_successor_key_parses_as_absent() -> None:
    """D-8: an explicit ``null`` announcement means there is none."""
    assert _verify(_document(successor_key=None)).successor_key is None


@pytest.mark.parametrize(
    "public_key",
    ["zz" * 32, "abc", _HEX_C + "aa"],
    ids=["not-hex", "too-short", "too-long"],
)
def test_malformed_successor_key_is_a_shape_error(public_key: str) -> None:
    """D-8: a successor key that is not 32 bytes of hex is refused."""
    document = _document(
        successor_key={
            "publishing_key_id": "portfoliflow-2027-01",
            "public_key": public_key,
            "valid_from": "2027-01-01",
        }
    )
    with pytest.raises(DirectoryShapeError):
        _verify(document)


# ---------------------------------------------------------------------------
# D-9: entry shape
# ---------------------------------------------------------------------------


def test_duplicate_provider_id_is_refused() -> None:
    """D-9: ``provider_id`` identifies an entry, so it must be unique."""
    document = _document(providers=[_provider(), _provider()])
    with pytest.raises(DirectoryShapeError) as excinfo:
        _verify(document)
    assert "alpha-broker" in str(excinfo.value)


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"provider_type": "custodian"}, "custodian"),
        ({"encryption_key_type": "rsa-oaep"}, "rsa-oaep"),
        ({"encryption_public_key": "aa" * 16}, "encryption_public_key"),
        ({"ticket_kinds": ["order", "swap"]}, "swap"),
        ({"engagement_categories": ["tax"]}, "tax"),
        ({"jurisdictions": ["de"]}, "jurisdictions"),
    ],
    ids=[
        "unknown-provider-type",
        "unknown-encryption-key-type",
        "wrong-length-encryption-key",
        "unknown-ticket-kind",
        "unknown-engagement-category",
        "lower-case-jurisdiction",
    ],
)
def test_malformed_provider_entry_is_a_shape_error(
    override: dict[str, object], needle: str
) -> None:
    """D-9: each closed vocabulary in an entry is enforced, and named on failure."""
    document = _document(providers=[_provider(**override)])
    with pytest.raises(DirectoryShapeError) as excinfo:
        _verify(document)
    assert needle in str(excinfo.value)


def test_unknown_key_in_a_provider_entry_is_refused() -> None:
    """D-9: entries are closed shapes too."""
    document = _document(providers=[_provider(relay_url="https://example.invalid")])
    with pytest.raises(DirectoryShapeError) as excinfo:
        _verify(document)
    assert "relay_url" in str(excinfo.value)


# ---------------------------------------------------------------------------
# D-10: the placeholder tripwire
# ---------------------------------------------------------------------------


def test_placeholder_publishing_key_fails_closed() -> None:
    """D-10: nothing verifies against the un-minted key, however well signed.

    This test is a tripwire. It turns red the day the real publishing key
    replaces the placeholder in
    :mod:`services.provider_channel.publishing_key`, which is intended: flip
    these assertions then, and add a test that the real key verifies a real
    document.
    """
    payload, signature = sign_directory(_document(), private_key=_PRIVATE_BYTES)
    with pytest.raises(PublishingKeyNotConfigured) as excinfo:
        verify_directory(payload, signature, publishing_key=PUBLISHING_KEY, now=_NOW)
    assert "placeholder" in str(excinfo.value)

    assert is_placeholder(PUBLISHING_KEY) is True
    assert is_placeholder(_PUBLIC_BYTES) is False


def test_placeholder_is_refused_before_the_document_is_read() -> None:
    """D-10: the key check precedes every other check — even unparseable bytes."""
    with pytest.raises(PublishingKeyNotConfigured):
        verify_directory(b"not json at all", b"", publishing_key=PUBLISHING_KEY, now=_NOW)


# ---------------------------------------------------------------------------
# canonical_bytes
# ---------------------------------------------------------------------------


def test_canonical_bytes_are_sorted_and_compact() -> None:
    """The signing input is stated once, and it is stable."""
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert canonical_bytes({"a": 2, "b": 1}) == b'{"a":2,"b":1}'


@pytest.mark.parametrize("key", ["issued_at", "valid_until"])
def test_missing_validity_dates_raise_a_shape_error_not_a_key_error(key: str) -> None:
    """A missing window bound is a shape error, never a bare ``KeyError``.

    The validity window is read at step 6, *before* the required-key check
    that step 7 performs, so these two reads have to survive an absent key on
    their own. A caller that catches ``DirectoryVerificationError`` must not
    be surprised by a ``KeyError`` escaping the gate.
    """
    document = _document()
    del document[key]
    with pytest.raises(DirectoryShapeError):
        _verify(document)


def test_malformed_validity_dates_raise_a_shape_error() -> None:
    """A window bound that is not a calendar day is refused the same way."""
    with pytest.raises(DirectoryShapeError):
        _verify(_document(valid_until="not-a-date"))
