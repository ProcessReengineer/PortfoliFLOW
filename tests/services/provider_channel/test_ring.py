# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Selecting the directory's key from the ring (B-D-14, Stage B · SB-3a).

The ring ships in code; a document only *names* the key it was signed with.
These tests pin the selection step between the two: the named key and no other
verifies, an id the ring does not know is refused before the signature is
looked at, and the two notices — a rotation that has happened, an announced
successor measured against the ring — are reported, never enforced. Every
other refusal is the trust gate's own, and the tests check that it arrives
unchanged rather than re-worded on the way.

The document helpers and the first throwaway key pair are imported from the
directory tests rather than copied, so the two modules cannot drift apart on
what a valid document is. The second throwaway pair is generated here; the
production private key never lives in this repository.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import services.provider_channel.ring as ring_module
from services.provider_channel.directory import (
    DirectoryShapeError,
    DirectoryVerificationError,
    InvalidDirectorySignature,
    PublishingKeyNotConfigured,
    canonical_bytes,
    sign_directory,
)
from services.provider_channel.publishing_key import PUBLISHING_KEY_ID, PUBLISHING_KEY_PLACEHOLDER
from services.provider_channel.ring import (
    RingVerification,
    SuccessorStatus,
    UnknownPublishingKeyId,
    verify_directory_with_ring,
)
from tests.services.provider_channel.test_directory import (
    _PRIVATE_BYTES,
    _PUBLIC_BYTES,
    _REAL_KEY_NOW,
    _document,
    _load_fixture,
)

#: A second throwaway pair, generated per module run like the first, so a ring
#: can hold two keys neither of which is the shipped one.
_OTHER_KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.generate()
_OTHER_PRIVATE_BYTES: Final[bytes] = _OTHER_KEY.private_bytes_raw()
_OTHER_PUBLIC_BYTES: Final[bytes] = _OTHER_KEY.public_key().public_bytes_raw()

#: The id the synthetic document names — read from it, not restated.
_CURRENT_ID: Final[str] = str(_document()["publishing_key_id"])
_SUCCESSOR_ID: Final[str] = "portfoliflow-2027-01"

#: A current key and its already-shipped successor, both throwaway.
_RING: Final[Mapping[str, bytes]] = {
    _CURRENT_ID: _PUBLIC_BYTES,
    _SUCCESSOR_ID: _OTHER_PUBLIC_BYTES,
}


def _judging_day(document: Mapping[str, object]) -> date:
    """One day after the document's own ``issued_at``.

    Derived from the document rather than written as a literal, so a re-dated
    helper cannot rot these tests, and inside the window rather than on its
    edge — the same convention ``_REAL_KEY_NOW`` follows for the fixture.
    """
    return date.fromisoformat(str(document["issued_at"])) + timedelta(days=1)


def _sign_and_verify(
    document: dict[str, object],
    *,
    private_key: bytes,
    ring: Mapping[str, bytes] = _RING,
    current_key_id: str = _CURRENT_ID,
) -> RingVerification:
    """Sign ``document`` with ``private_key`` and verify it through ``ring``."""
    payload, signature = sign_directory(document, private_key=private_key)
    return verify_directory_with_ring(
        payload, signature, now=_judging_day(document), ring=ring, current_key_id=current_key_id
    )


def _announcement(publishing_key_id: str, public_key: str) -> dict[str, object]:
    """A ``successor_key`` block. ``valid_from`` is carried, never judged here."""
    return {
        "publishing_key_id": publishing_key_id,
        "public_key": public_key,
        "valid_from": "2027-01-01",
    }


def _document_without_key_id() -> dict[str, object]:
    """A valid document with its ``publishing_key_id`` removed."""
    document = _document()
    del document["publishing_key_id"]
    return document


# ---------------------------------------------------------------------------
# The shipped ring against the first published directory
# ---------------------------------------------------------------------------


def test_real_fixture_verifies_through_the_ring() -> None:
    """The published directory verifies through the shipped ring, defaults only.

    Nothing is passed but the bytes and the day: the ring, the current key id
    and the selection are all the build's own — the path the SB-3b fetch
    client will take.
    """
    payload, signature = _load_fixture()

    result = verify_directory_with_ring(payload, signature, now=_REAL_KEY_NOW)

    assert isinstance(result, RingVerification)
    assert result.key_id == PUBLISHING_KEY_ID
    assert result.successor_in_use is False
    assert result.announced_successor == "in_ring"
    assert result.directory.directory_version == 1


# ---------------------------------------------------------------------------
# Selection by id
# ---------------------------------------------------------------------------


def test_unknown_publishing_key_id_is_refused_before_the_signature() -> None:
    """B-D-14: an id the ring does not know is refused, whatever the signature.

    The genuine signature is by a key that *is* in the ring, under another id,
    so the refusal cannot be a signature failure in disguise. The garbage
    signature proves the order: had the signature been checked first, it
    would have raised :class:`InvalidDirectorySignature` instead.
    """
    document = _document(publishing_key_id="portfoliflow-9999-99")
    payload, genuine = sign_directory(document, private_key=_PRIVATE_BYTES)
    ring = {"portfoliflow-2026-09": _PUBLIC_BYTES}

    for signature in (genuine, b"\x00" * 64):
        with pytest.raises(UnknownPublishingKeyId) as excinfo:
            verify_directory_with_ring(
                payload,
                signature,
                now=_judging_day(document),
                ring=ring,
                current_key_id="portfoliflow-2026-09",
            )
        assert "is not in the shipped key ring" in str(excinfo.value)


def test_ring_selects_the_key_by_id_and_refuses_the_wrong_key() -> None:
    """B-D-14: the id selects one key, and only that key may verify.

    Both keys are in the ring, so a verifier that tried every ring key in turn
    would accept the second document. Selection by id refuses it: the document
    names ``A``, and ``A``'s key did not sign it.
    """
    ring = {"A": _PUBLIC_BYTES, "B": _OTHER_PUBLIC_BYTES}
    document = _document(publishing_key_id="A")

    result = _sign_and_verify(document, private_key=_PRIVATE_BYTES, ring=ring, current_key_id="A")
    assert result.key_id == "A"
    assert result.directory.publishing_key_id == "A"

    with pytest.raises(InvalidDirectorySignature):
        _sign_and_verify(document, private_key=_OTHER_PRIVATE_BYTES, ring=ring, current_key_id="A")


# ---------------------------------------------------------------------------
# Notices — reported, never enforced
# ---------------------------------------------------------------------------


def test_successor_in_use_is_reported_not_refused() -> None:
    """A document signed by the successor verifies, and reports the rotation.

    The successor was in the ring before the directory switched to it — the
    B-D-14 ceremony order — so the document is believed; that the caller's
    current key is now the older one is a notice, not a refusal.
    """
    document = _document(publishing_key_id=_SUCCESSOR_ID)

    result = _sign_and_verify(
        document, private_key=_OTHER_PRIVATE_BYTES, ring=_RING, current_key_id=_CURRENT_ID
    )

    assert result.key_id == _SUCCESSOR_ID
    assert result.successor_in_use is True
    assert result.announced_successor is None


@pytest.mark.parametrize(
    ("successor_key", "expected"),
    [
        pytest.param(
            _announcement(_SUCCESSOR_ID, _OTHER_PUBLIC_BYTES.hex()), "in_ring", id="in-ring"
        ),
        pytest.param(
            _announcement("portfoliflow-2027-07", _OTHER_PUBLIC_BYTES.hex()),
            "not_in_ring",
            id="not-in-ring",
        ),
        pytest.param(
            _announcement(_SUCCESSOR_ID, "c" * 64), "contradicts_ring", id="contradicts-ring"
        ),
        pytest.param(None, None, id="no-announcement"),
        pytest.param(
            _announcement(_SUCCESSOR_ID, _OTHER_PUBLIC_BYTES.hex().upper()),
            "in_ring",
            id="in-ring-upper-case-hex",
        ),
    ],
)
def test_announced_successor_statuses(
    successor_key: dict[str, object] | None, expected: SuccessorStatus | None
) -> None:
    """B-D-14: an announcement is measured against the ring and reported.

    Every document is signed by the current key and verifies, whatever its
    announcement says. ``not_in_ring`` names a key the ring holds, but under
    another id — the ring is keyed by id, so that is still a client update
    required before rotation. The upper-case variant pins that keys are
    compared, not their hex spellings.
    """
    document = _document() if successor_key is None else _document(successor_key=successor_key)

    result = _sign_and_verify(document, private_key=_PRIVATE_BYTES)

    assert result.key_id == _CURRENT_ID
    assert result.successor_in_use is False
    assert result.announced_successor == expected


# ---------------------------------------------------------------------------
# Refusals stay the gate's own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document_bytes", "expected"),
    [
        pytest.param(b"not json", InvalidDirectorySignature, id="not-json"),
        pytest.param(
            json.dumps([_document()], sort_keys=True, separators=(",", ":")).encode("utf-8"),
            InvalidDirectorySignature,
            id="json-list",
        ),
        pytest.param(
            canonical_bytes(_document_without_key_id()),
            DirectoryShapeError,
            id="object-without-id",
        ),
        pytest.param(
            canonical_bytes(_document(publishing_key_id=7)),
            DirectoryShapeError,
            id="object-with-non-string-id",
        ),
    ],
)
def test_unreadable_document_is_refused_by_the_reference_implementation(
    document_bytes: bytes, expected: type[DirectoryVerificationError]
) -> None:
    """A document whose id cannot be read is refused by the gate, in its words.

    Each input is genuinely signed with the current key, so an object reaches
    the gate's shape check (step 7) rather than stopping at the signature:
    even a correctly signed document without a usable id is refused — by the
    reference implementation, never with a second copy of its messages here.
    """
    signature = Ed25519PrivateKey.from_private_bytes(_PRIVATE_BYTES).sign(document_bytes)

    with pytest.raises(DirectoryVerificationError) as excinfo:
        verify_directory_with_ring(
            document_bytes,
            signature,
            now=_judging_day(_document()),
            ring=_RING,
            current_key_id=_CURRENT_ID,
        )

    assert not isinstance(excinfo.value, UnknownPublishingKeyId)
    assert excinfo.type is expected


def test_placeholder_in_ring_still_fails_closed() -> None:
    """A ring entry that is the placeholder is refused at the gate's first step.

    Selection does not route around the sentinel: the key it picks meets the
    same check as a key passed to the gate directly.
    """
    ring = {_CURRENT_ID: PUBLISHING_KEY_PLACEHOLDER}

    with pytest.raises(PublishingKeyNotConfigured):
        _sign_and_verify(
            _document(), private_key=_PRIVATE_BYTES, ring=ring, current_key_id=_CURRENT_ID
        )


def test_misconfigured_current_key_id_is_an_unknown_id_not_a_key_error() -> None:
    """A current key id the ring does not hold is refused by name, never a ``KeyError``.

    Unreadable bytes are the case that would otherwise reach
    ``ring[current_key_id]``. The guard is not confined to that path: the
    published document, readable and genuinely signed, is refused the same
    way rather than verified with a rotation notice every document would then
    carry.
    """
    with pytest.raises(UnknownPublishingKeyId) as excinfo:
        verify_directory_with_ring(b"not json", b"", now=_REAL_KEY_NOW, current_key_id="nope")
    assert "'nope'" in str(excinfo.value)

    payload, signature = _load_fixture()
    with pytest.raises(UnknownPublishingKeyId):
        verify_directory_with_ring(payload, signature, now=_REAL_KEY_NOW, current_key_id="nope")


def test_ring_module_reads_no_clock() -> None:
    """D-clock: the selection step reads no clock; ``now`` only passes through.

    The two literals are spelled by concatenation so that this file never
    carries them either.
    """
    source = inspect.getsource(ring_module)
    for literal in ("date." + "today", "datetime." + "now"):
        assert literal not in source, f"{literal} found in services/provider_channel/ring.py"
