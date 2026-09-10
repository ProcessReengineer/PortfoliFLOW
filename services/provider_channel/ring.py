# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Selecting the directory's publishing key from the ring (B-D-14, Stage B).

:func:`~services.provider_channel.directory.verify_directory` takes exactly one
key, and the ring in :mod:`services.provider_channel.publishing_key` holds
several. This module is the *selection* step B-D-14 leaves between "a document
arrives" and that gate: the document names its key by ``publishing_key_id``,
the id is looked up in the ring that ships in code, and only a key that was
already in the ring may verify. An id the ring does not know is refused before
any signature arithmetic happens — there is no key the client already believes
to check it against.

**Selecting is not trusting.** The id is read from bytes nobody has verified
yet, and that is safe for one reason only: it chooses *which* shipped key
checks the signature, nothing more. A forged id buys a signature check against
a key the forger does not hold. Every refusal after the selection is the
gate's own, passed through unchanged, so the refusal vocabulary exists once —
even a document whose id cannot be read at all is handed to the gate to be
refused in its own words.

**An announced successor is a notice, never a key.** Whether a verified
document's ``successor_key`` announcement matches the ring is *reported*, not
enforced, and nothing here adds a key to the ring: that happens only by
shipping a new version of :mod:`~services.provider_channel.publishing_key`.
Likewise a document verified by a ring key other than the current one is
reported as a rotation that has happened, not refused.

Pure: no repository, no session, no network, and no clock — ``now`` is
injected and handed straight to the gate (D-clock).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from services.provider_channel.directory import (
    Directory,
    DirectoryVerificationError,
    verify_directory,
)
from services.provider_channel.publishing_key import PUBLISHING_KEY_ID, PUBLISHING_KEY_RING

#: How a verified document's ``successor_key`` announcement compares to the
#: ring: ``"in_ring"`` — the announced id is in the ring with the same key;
#: ``"not_in_ring"`` — the id is absent, so a client update is required before
#: rotation; ``"contradicts_ring"`` — the id is present with a different key.
SuccessorStatus = Literal["in_ring", "not_in_ring", "contradicts_ring"]


class UnknownPublishingKeyId(DirectoryVerificationError):
    """The document names a publishing key id that is not in the ring."""


@dataclass(frozen=True, slots=True)
class RingVerification:
    """A directory verified by a key selected from the ring, with its notices.

    The two notices are reported, never enforced: the document has already
    verified against a key that was in the ring, which is the whole of what
    B-D-14 requires before a document is believed.

    Attributes:
        directory: The verified directory, exactly as
            :func:`~services.provider_channel.directory.verify_directory`
            returned it.
        key_id: The ring id whose key verified the document — the document's
            own ``publishing_key_id``.
        successor_in_use: ``True`` when ``key_id`` is not the caller's current
            key id: the rotation has happened.
        announced_successor: How the document's ``successor_key`` compares to
            the ring (:data:`SuccessorStatus`), or ``None`` when the document
            announces no successor.
    """

    directory: Directory
    key_id: str
    successor_in_use: bool
    announced_successor: SuccessorStatus | None


def _declared_key_id(document_bytes: bytes) -> str | None:
    """Read ``publishing_key_id`` from unverified bytes, believing nothing else.

    Returns ``None`` for bytes that are not UTF-8 JSON, for JSON that is not an
    object, and for an object without a string ``publishing_key_id`` — the
    caller hands all three to the gate to refuse.
    """
    try:
        decoded = json.loads(document_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    key_id = decoded.get("publishing_key_id")
    return key_id if isinstance(key_id, str) else None


def _announced_successor(directory: Directory, ring: Mapping[str, bytes]) -> SuccessorStatus | None:
    """Compare the document's successor announcement with the ring.

    Keys are compared as bytes rather than as hex text: the v1 shape accepts
    either case of hex, and a different spelling of the same key is not a
    contradiction.
    """
    successor = directory.successor_key
    if successor is None:
        return None
    ring_key = ring.get(successor.publishing_key_id)
    if ring_key is None:
        return "not_in_ring"
    if ring_key == bytes.fromhex(successor.public_key):
        return "in_ring"
    return "contradicts_ring"


def verify_directory_with_ring(
    document_bytes: bytes,
    signature: bytes,
    *,
    now: date,
    ring: Mapping[str, bytes] = PUBLISHING_KEY_RING,
    current_key_id: str = PUBLISHING_KEY_ID,
) -> RingVerification:
    """Select the document's key from the ring, verify with it, report notices.

    The steps, in order:

    1. ``current_key_id`` must be in ``ring`` — a misconfigured caller is
       told so, never handed a ``KeyError`` or a false rotation notice;
    2. ``publishing_key_id`` is read from the still-unverified bytes. If it
       cannot be read, the document goes to the gate with the current key and
       is refused there, in the gate's own words: at its step 2 for bytes
       that are not a JSON object, at step 5 or 7 for an object without a
       usable id;
    3. an id that is not in ``ring`` is refused before the signature;
    4. the ring key for that id verifies the document, and every refusal
       passes through unchanged;
    5. the notices are read off the verified document.

    Args:
        document_bytes: The canonical document bytes, exactly as published.
        signature: The detached signature over ``document_bytes``.
        now: The day to judge validity against; injected, never read from a
            clock (D-clock).
        ring: The keys a document may be verified with, by id. Defaults to
            the ring that ships in code; tests pass their own.
        current_key_id: The id of the key the client considers current;
            ``successor_in_use`` is judged against it.

    Returns:
        The verified directory, the ring id that verified it, and the two
        notices.

    Raises:
        UnknownPublishingKeyId: If the document names an id that is not in
            ``ring``, or ``current_key_id`` is not in ``ring``.
        DirectoryVerificationError: Everything
            :func:`~services.provider_channel.directory.verify_directory`
            raises, unchanged — including ``PublishingKeyNotConfigured`` if
            the selected ring entry is the placeholder.
    """
    if current_key_id not in ring:
        raise UnknownPublishingKeyId(
            f"current_key_id {current_key_id!r} is not in the key ring {sorted(ring)}; "
            "the caller's current key must be one of the keys it verifies with"
        )

    declared_key_id = _declared_key_id(document_bytes)
    if declared_key_id is None:
        # The gate below never returns for a document without a usable id;
        # the current key is only the vehicle of its refusal.
        key_id = current_key_id
    elif declared_key_id not in ring:
        raise UnknownPublishingKeyId(
            f"publishing_key_id {declared_key_id!r} is not in the shipped key ring "
            f"{sorted(ring)}; a document may only be believed on the strength of a key "
            "that was already in the ring (B-D-14)"
        )
    else:
        key_id = declared_key_id

    directory = verify_directory(document_bytes, signature, publishing_key=ring[key_id], now=now)
    return RingVerification(
        directory=directory,
        key_id=key_id,
        successor_in_use=key_id != current_key_id,
        announced_successor=_announced_successor(directory, ring),
    )


__all__ = [
    "RingVerification",
    "SuccessorStatus",
    "UnknownPublishingKeyId",
    "verify_directory_with_ring",
]
