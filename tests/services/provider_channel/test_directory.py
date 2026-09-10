# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The directory trust gate (ADR-0129 §2, Stage A).

The directory says which counterparties an instance may talk to, so the only
interesting question about it is whether it can be believed. These tests walk
the gate's seven refusals one at a time — placeholder key, non-canonical
bytes, unreadable format, unsupported scheme, bad signature, stale window,
bad shape — because a gate that fails for the wrong reason is a gate whose
logs cannot be trusted either.

The ad-hoc key pair is generated per module run. Since SB-1 the module also
exercises the *shipped* publishing key against the first published document
in ``fixtures/`` — public key material and a signature, never a private key:
the production private key never lives in this repository.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

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
from services.provider_channel.publishing_key import (
    PUBLISHING_KEY,
    PUBLISHING_KEY_ID,
    PUBLISHING_KEY_PLACEHOLDER,
    PUBLISHING_KEY_RING,
    SUCCESSOR_KEY,
    SUCCESSOR_KEY_ID,
    is_placeholder,
)

_SIGNING_KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.generate()
_PRIVATE_BYTES: Final[bytes] = _SIGNING_KEY.private_bytes_raw()
_PUBLIC_BYTES: Final[bytes] = _SIGNING_KEY.public_key().public_bytes_raw()

_OTHER_KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.generate()
_OTHER_PUBLIC_BYTES: Final[bytes] = _OTHER_KEY.public_key().public_bytes_raw()

#: Injected rather than read from a clock (D-clock).
_NOW: Final[date] = date(2026, 9, 7)

#: The first published directory, as the signing tool wrote it (2026-09-10).
_FIXTURES: Final[Path] = Path(__file__).parent / "fixtures"

#: One day after the fixture's ``issued_at``, so the real-key tests judge it
#: from inside its window. ``_NOW`` above predates the publication and would
#: fail closed against it; ``date.today()`` would make the suite expire.
_REAL_KEY_NOW: Final[date] = date(2026, 9, 11)

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


def _load_fixture() -> tuple[bytes, bytes]:
    """Return the published document bytes and its detached signature.

    The document is read as **bytes**: it is the canonical byte string the
    signature covers, and re-encoding it through ``json`` would check a
    spelling nobody published. The ``.sig`` encoding is B-D-23 — 128 lowercase
    hex characters followed by exactly one newline — and it is decoded
    strictly rather than with a forgiving ``.strip()``, because a signature
    that only verifies after the reader tidies it up is not the signature the
    operator published.

    Returns:
        ``(document_bytes, signature)``, ready for :func:`verify_directory`.
    """
    payload = (_FIXTURES / "directory-1.json").read_bytes()
    raw = (_FIXTURES / "directory-1.sig").read_bytes()
    assert raw.endswith(b"\n"), "the published signature ends with exactly one newline"
    hex_text = raw[:-1].decode("ascii")
    assert len(hex_text) == 128, f"expected 128 hex characters, got {len(hex_text)}"
    return payload, bytes.fromhex(hex_text)


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
# D-10 / OP-30: the shipped key ring, and the placeholder as sentinel
# ---------------------------------------------------------------------------


def test_shipped_publishing_key_is_real_and_the_placeholder_still_fails_closed() -> None:
    """SB-1 flipped the Stage A tripwire (OP-30); the placeholder value remains
    the fail-closed sentinel.

    Two claims in one test because they are one claim: the module ships a real
    key *and* has not thereby lost the guard that refuses an un-minted one.
    """
    assert is_placeholder(PUBLISHING_KEY) is False
    assert is_placeholder(SUCCESSOR_KEY) is False
    assert len(PUBLISHING_KEY) == 32
    assert len(SUCCESSOR_KEY) == 32
    assert PUBLISHING_KEY != SUCCESSOR_KEY

    # B-D-19: portfoliflow-YYYY-MM, the minting month.
    assert re.fullmatch(r"portfoliflow-\d{4}-\d{2}", PUBLISHING_KEY_ID)
    assert re.fullmatch(r"portfoliflow-\d{4}-\d{2}", SUCCESSOR_KEY_ID)

    assert PUBLISHING_KEY_RING == {
        PUBLISHING_KEY_ID: PUBLISHING_KEY,
        SUCCESSOR_KEY_ID: SUCCESSOR_KEY,
    }
    # The ring is the root of trust: a caller must not be able to add a key to
    # it at runtime, which is what makes "rotation is a code release" (B-D-14)
    # a property of the build rather than a convention.
    with pytest.raises(TypeError):
        PUBLISHING_KEY_RING[SUCCESSOR_KEY_ID] = PUBLISHING_KEY

    payload, signature = sign_directory(_document(), private_key=_PRIVATE_BYTES)
    with pytest.raises(PublishingKeyNotConfigured) as excinfo:
        verify_directory(payload, signature, publishing_key=PUBLISHING_KEY_PLACEHOLDER, now=_NOW)
    assert "placeholder" in str(excinfo.value)

    assert is_placeholder(PUBLISHING_KEY_PLACEHOLDER) is True
    assert is_placeholder(_PUBLIC_BYTES) is False


def test_placeholder_is_refused_before_the_document_is_read() -> None:
    """D-10: the key check precedes every other check — even unparseable bytes."""
    with pytest.raises(PublishingKeyNotConfigured):
        verify_directory(
            b"not json at all", b"", publishing_key=PUBLISHING_KEY_PLACEHOLDER, now=_NOW
        )


# ---------------------------------------------------------------------------
# SB-1: the shipped key against the first published directory
# ---------------------------------------------------------------------------


def test_real_key_verifies_the_first_published_directory() -> None:
    """The shipped key verifies the document the operator actually published.

    This is the end of the chain the ceremony builds: an offline key, a
    signing run, two files on portfoliflow.com, and this constant. Checking
    them against each other here means a key substituted in this repository —
    the attack ADR-0129 §2 names — cannot pass CI silently.
    """
    payload, signature = _load_fixture()

    directory = verify_directory(
        payload, signature, publishing_key=PUBLISHING_KEY, now=_REAL_KEY_NOW
    )

    assert directory.directory_version == 1
    assert directory.publishing_key_id == PUBLISHING_KEY_ID

    # The successor announcement (B-D-14) names the key already in the ring;
    # the dataclass stores it as hex, not bytes.
    assert directory.successor_key is not None
    assert directory.successor_key.publishing_key_id == SUCCESSOR_KEY_ID
    assert directory.successor_key.public_key == SUCCESSOR_KEY.hex()

    assert {entry.provider_id for entry in directory.providers} == {
        "test-broker-01",
        "test-secondary-01",
    }
    # B-D-2: the first publication carries test entries, marked by name alone.
    for entry in directory.providers:
        assert entry.display_name.startswith("[TEST] ")


def test_real_key_refuses_a_document_signed_by_another_key() -> None:
    """The right bytes signed by the wrong key are still refused.

    The published document is not a secret, so an attacker's problem is never
    obtaining it — only signing it. This pins that having the bytes buys
    nothing.
    """
    payload, _ = _load_fixture()
    forged_payload, forged_signature = sign_directory(
        json.loads(payload), private_key=_PRIVATE_BYTES
    )
    assert forged_payload == payload, "the fixture is canonical: same bytes, other key"

    with pytest.raises(InvalidDirectorySignature):
        verify_directory(
            payload, forged_signature, publishing_key=PUBLISHING_KEY, now=_REAL_KEY_NOW
        )


@pytest.mark.parametrize("key_id", sorted(PUBLISHING_KEY_RING))
def test_ring_keys_are_valid_ed25519_public_keys(key_id: str) -> None:
    """Every ring entry is a loadable Ed25519 public key.

    Only that. When each key becomes acceptable — the overlap rule around a
    successor's ``valid_from`` — is SB-3's question, not this one's.
    """
    key = PUBLISHING_KEY_RING[key_id]
    assert Ed25519PublicKey.from_public_bytes(key).public_bytes_raw() == key


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
