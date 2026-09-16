# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The libsodium sealed box, X25519 + XSalsa20-Poly1305 (B-D-12, Stage B).

The provider channel's one confidentiality primitive, and deliberately the
smallest one that does the job. A sealed box needs no handshake, no session
and no key of the sender's own: the instance holds the recipient's public key
from the signed directory, mints an **ephemeral** key pair per message, and
throws it away. What travels is the ephemeral public key, the ciphertext and
a Poly1305 tag — and the portal on the other side opens it with libsodium.js,
which is why the primitive is libsodium's rather than one of this repository's
own devising.

**Bytes in, bytes out.** Nothing here knows what an order is. The module takes
``bytes`` and returns ``bytes``; the shape of the plaintext, its canonical
form and the envelope it rides in are
:mod:`~services.provider_channel.export`'s business. Keeping the cipher
ignorant of the payload is what lets the B-3 parity check against libsodium.js
be a check of *this* function rather than of a whole export path.

**Failure is deliberately uninformative.** A wrong recipient key and an
altered ciphertext are the same event to a sealed box — the MAC simply does
not verify — and :class:`SealedBoxOpenFailed` says so rather than guessing.
No refusal echoes key or ciphertext bytes.

Pure: no repository, no session, no clock, no network. Nothing here reads a
setting, and nothing on the instance side decrypts in production —
:func:`open_sealed` exists for the test suite and the B-3 parity check.
"""

from __future__ import annotations

from typing import Final

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

#: Length of an X25519 key, public or private, in bytes.
SEALED_BOX_KEY_BYTES: Final[int] = 32

#: libsodium ``crypto_box_SEALBYTES`` — the ephemeral public key (32) plus the
#: Poly1305 MAC (16). A sealed ciphertext is always ``len(plaintext) + 48``,
#: which is what makes the length of a message visible to an observer and the
#: reason a payload should not be padded to carry meaning.
SEALED_BOX_OVERHEAD: Final[int] = 48


class SealedBoxError(ValueError):
    """A sealed-box operation was refused. Base of both refusals below."""


class InvalidRecipientKey(SealedBoxError):
    """A key was not :data:`SEALED_BOX_KEY_BYTES` long.

    Raised for the recipient's public key in :func:`seal` and for the private
    key in :func:`open_sealed` and :func:`public_key_from_private`. The length
    is checked before any ``nacl`` object is built, so a malformed directory
    entry is refused in this package's words rather than in libsodium's.
    """


class SealedBoxOpenFailed(SealedBoxError):
    """A sealed box did not open.

    Wrong key and altered ciphertext are indistinguishable by construction:
    the Poly1305 tag either verifies or it does not, and the box says nothing
    about *why* it did not. The message reports both possibilities rather than
    claiming one, and it echoes neither ciphertext nor key.
    """


def _check_key_length(key: bytes, *, role: str) -> None:
    """Refuse a key of the wrong length, naming its role but never its bytes.

    Args:
        key: The raw key material.
        role: What the key was being used as, for the message.

    Raises:
        InvalidRecipientKey: If ``key`` is not :data:`SEALED_BOX_KEY_BYTES`.
    """
    if len(key) != SEALED_BOX_KEY_BYTES:
        raise InvalidRecipientKey(
            f"{role} must be exactly {SEALED_BOX_KEY_BYTES} bytes, got {len(key)}"
        )


def seal(plaintext: bytes, recipient_public_key: bytes) -> bytes:
    """Seal a message to a recipient's X25519 public key.

    An ephemeral key pair is minted inside libsodium for this one message and
    discarded, so two seals of the same plaintext differ and neither can be
    linked to the other or back to the sender.

    Args:
        plaintext: The bytes to encrypt. May be empty.
        recipient_public_key: Raw 32-byte X25519 public key, as carried in
            hex by a directory entry's ``encryption_public_key``.

    Returns:
        The sealed box: ``len(plaintext) + SEALED_BOX_OVERHEAD`` bytes.

    Raises:
        InvalidRecipientKey: If the public key is not 32 bytes.
    """
    _check_key_length(recipient_public_key, role="recipient public key")
    return bytes(SealedBox(PublicKey(recipient_public_key)).encrypt(plaintext))


def open_sealed(ciphertext: bytes, private_key: bytes) -> bytes:
    """Open a sealed box with the recipient's X25519 private key.

    Nothing on the instance side calls this in production: the instance seals
    to a provider and never holds a provider's private key. It exists for the
    test suite and for the B-3 parity check against libsodium.js, where one
    side must be able to open what the other sealed.

    Args:
        ciphertext: The sealed box.
        private_key: Raw 32-byte X25519 private key.

    Returns:
        The recovered plaintext.

    Raises:
        InvalidRecipientKey: If the private key is not 32 bytes.
        SealedBoxOpenFailed: If the box does not open — a wrong key and an
            altered ciphertext are the same failure here.
    """
    _check_key_length(private_key, role="private key")
    try:
        return bytes(SealedBox(PrivateKey(private_key)).decrypt(ciphertext))
    except CryptoError:
        raise SealedBoxOpenFailed(
            "sealed box did not open: wrong key or altered ciphertext"
        ) from None


def public_key_from_private(private_key: bytes) -> bytes:
    """Derive the X25519 public key belonging to a private key.

    Args:
        private_key: Raw 32-byte X25519 private key.

    Returns:
        The raw 32-byte public key.

    Raises:
        InvalidRecipientKey: If the private key is not 32 bytes.
    """
    _check_key_length(private_key, role="private key")
    return bytes(PrivateKey(private_key).public_key)


__all__ = [
    "SEALED_BOX_KEY_BYTES",
    "SEALED_BOX_OVERHEAD",
    "InvalidRecipientKey",
    "SealedBoxError",
    "SealedBoxOpenFailed",
    "open_sealed",
    "public_key_from_private",
    "seal",
]
