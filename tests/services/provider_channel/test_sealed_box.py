# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The libsodium sealed box (B-D-12, Stage B).

Every key in this module is thrown away: generated inside the test, or read
from a fixture whose private half is published on purpose (B-D-20). No
test-provider key and no production key appears here, so a leaked repository
leaks nothing that ever sealed anything real.

What the tests pin is the primitive's *shape* — a fixed 48-byte overhead, a
fresh ephemeral key per call, and a refusal that says nothing about why —
plus the known-answer fixture, which is the standing check that a ciphertext
sealed by an earlier build still opens under this one. That fixture is also
the artefact the B-3 libsodium.js parity check will run against.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Final

import pytest
from nacl.public import PrivateKey

from services.provider_channel.schemas import ExportPayload, parse_export
from services.provider_channel.sealed_box import (
    SEALED_BOX_OVERHEAD,
    InvalidRecipientKey,
    SealedBoxOpenFailed,
    open_sealed,
    public_key_from_private,
    seal,
)

_FIXTURE: Final[Path] = Path(__file__).parent / "fixtures" / "throwaway-recipient.json"

_PLAINTEXT: Final[bytes] = b"the provider channel seals bytes, not meaning"


@pytest.fixture
def private_key() -> bytes:
    """A throwaway X25519 private key, minted per test (B-D-20)."""
    return bytes(PrivateKey.generate())


@pytest.fixture
def public_key(private_key: bytes) -> bytes:
    """The public half of :func:`private_key`."""
    return public_key_from_private(private_key)


@pytest.fixture
def throwaway() -> dict[str, str]:
    """The known-answer fixture: a key pair, its plaintext and its ciphertext."""
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_sealed_box_opens_to_the_plaintext(private_key: bytes, public_key: bytes) -> None:
    """What was sealed to a key is what that key recovers."""
    assert open_sealed(seal(_PLAINTEXT, public_key), private_key) == _PLAINTEXT


def test_an_empty_plaintext_round_trips(private_key: bytes, public_key: bytes) -> None:
    """An empty message is a legal message; nothing special-cases its length."""
    assert open_sealed(seal(b"", public_key), private_key) == b""


@pytest.mark.parametrize("length", [0, 1, 45, 1000])
def test_a_ciphertext_is_the_plaintext_plus_the_fixed_overhead(
    public_key: bytes, length: int
) -> None:
    """The overhead is constant, so a message's length is visible to an observer.

    Pinned because it is the reason a payload must not be padded to carry
    meaning: the size of a sealed export is public whatever is inside it.
    """
    plaintext = b"x" * length
    assert len(seal(plaintext, public_key)) == length + SEALED_BOX_OVERHEAD


def test_two_seals_of_one_plaintext_differ_and_both_open(
    private_key: bytes, public_key: bytes
) -> None:
    """A fresh ephemeral key per call: identical messages do not look identical."""
    first = seal(_PLAINTEXT, public_key)
    second = seal(_PLAINTEXT, public_key)

    assert first != second
    assert open_sealed(first, private_key) == _PLAINTEXT
    assert open_sealed(second, private_key) == _PLAINTEXT


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_different_private_key_does_not_open_the_box(public_key: bytes) -> None:
    """Only the recipient opens it; the refusal names no key and no bytes."""
    ciphertext = seal(_PLAINTEXT, public_key)
    stranger = bytes(PrivateKey.generate())

    with pytest.raises(SealedBoxOpenFailed) as excinfo:
        open_sealed(ciphertext, stranger)

    message = str(excinfo.value)
    assert "wrong key or altered ciphertext" in message
    assert stranger.hex() not in message
    assert base64.b64encode(ciphertext).decode("ascii") not in message


@pytest.mark.parametrize("index", [0, 32, -1], ids=["ephemeral_key", "mac", "last_byte"])
def test_a_flipped_byte_does_not_open_the_box(
    private_key: bytes, public_key: bytes, index: int
) -> None:
    """Altering one byte anywhere — ephemeral key, MAC or body — is fatal.

    Index 0 lands in the ephemeral public key, 32 in the Poly1305 tag, and
    -1 in the encrypted body: all three are covered, and the failure is the
    same one, which is the point of :class:`SealedBoxOpenFailed`.
    """
    ciphertext = bytearray(seal(_PLAINTEXT, public_key))
    ciphertext[index] ^= 0x01

    with pytest.raises(SealedBoxOpenFailed):
        open_sealed(bytes(ciphertext), private_key)


@pytest.mark.parametrize("length", [0, 31, 33, 64])
def test_a_recipient_key_of_the_wrong_length_is_refused(length: int) -> None:
    """The length is checked in this package's words, before libsodium sees it."""
    with pytest.raises(InvalidRecipientKey) as excinfo:
        seal(_PLAINTEXT, b"\x00" * length)
    assert "recipient public key" in str(excinfo.value)


@pytest.mark.parametrize("length", [31, 33])
def test_a_private_key_of_the_wrong_length_is_refused(length: int) -> None:
    """``open_sealed`` checks its key the same way ``seal`` checks its own."""
    with pytest.raises(InvalidRecipientKey) as excinfo:
        open_sealed(b"\x00" * (SEALED_BOX_OVERHEAD + 1), b"\x00" * length)
    assert "private key" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_the_derived_public_key_is_libsodiums_own(private_key: bytes) -> None:
    """``public_key_from_private`` adds no arithmetic of its own."""
    assert public_key_from_private(private_key) == bytes(PrivateKey(private_key).public_key)


# ---------------------------------------------------------------------------
# The known-answer fixture
# ---------------------------------------------------------------------------


def test_the_fixture_public_key_derives_from_its_private_key(throwaway: dict[str, str]) -> None:
    """The fixture is internally consistent: the pair is a pair."""
    derived = public_key_from_private(bytes.fromhex(throwaway["private_key_hex"]))
    assert derived.hex() == throwaway["public_key_hex"]


def test_the_fixture_ciphertext_opens_to_its_recorded_plaintext(
    throwaway: dict[str, str],
) -> None:
    """A ciphertext sealed by an earlier build still opens under this one.

    This is the standing regression against a change of primitive, encoding
    or overhead, and the artefact the B-3 libsodium.js parity check reads.
    """
    opened = open_sealed(
        base64.b64decode(throwaway["ciphertext_base64"], validate=True),
        bytes.fromhex(throwaway["private_key_hex"]),
    )
    assert opened == throwaway["plaintext_utf8"].encode("utf-8")


def test_the_fixture_plaintext_is_a_readable_export_payload(throwaway: dict[str, str]) -> None:
    """What the box carries is not opaque bytes but a payload this build parses."""
    parsed = parse_export(json.loads(throwaway["plaintext_utf8"]))
    assert isinstance(parsed, ExportPayload)
    assert parsed.correlation_id == "throwaway-0001"
    assert parsed.identifier_value == "DE0001234567"
