# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The portfoliflow.com directory publishing keys (ADR-0129 §2).

The provider directory is trusted because it is *signed*, not because of where
it was fetched from. That makes this file the root of the channel's trust: a
client verifies every directory against a key below, so substituting one of
them — not breaking the cipher — is the attack worth defending against. The
keys ship in the AGPL repository precisely so that substitution is a visible
diff rather than a silent server-side swap.

**The real key landed on 2026-09-10** (Stage B, SB-1). It was minted offline
together with its successor in the ceremony of B-D-5 §3.3; the private halves
never touch this repository or the server. The Stage-A placeholder value is
retained, but no longer as the shipped key: it is now only the fail-closed
sentinel that
:func:`services.provider_channel.directory.verify_directory` refuses before it
reads a single byte of a document, so a build that ever loses its key still
trusts nothing by accident.

**Rotation is a code release** (B-D-14). The ring below is the whole set of
keys a client may verify a directory against, and it grows only by shipping a
new version of this file. A ``successor_key`` announcement inside a verified
directory is therefore a *notice* that a rotation is coming — never itself a
key the client starts trusting, because a document may only ever be believed
on the strength of a key that was already in the ring.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

#: The all-zero stand-in that Stage A shipped as the key.
#:
#: No longer the shipped key — :data:`PUBLISHING_KEY` below is real — but kept
#: as the fail-closed sentinel: ``verify_directory`` refuses this value before
#: it reads the document.
PUBLISHING_KEY_PLACEHOLDER: Final[bytes] = b"\x00" * 32

#: The current portfoliflow.com publishing key (minted 2026-09, key
#: ceremony recorded in the operator's private operations notes).
PUBLISHING_KEY: Final[bytes] = bytes.fromhex(
    "de7cb38995756192201ed32b568f9c5115b78dbb4127f667611ee8eb1812ba35"
)
#: Identifier matched against a document's ``publishing_key_id``
#: (B-D-19: ``portfoliflow-YYYY-MM``, the minting month).
PUBLISHING_KEY_ID: Final[str] = "portfoliflow-2026-09"

#: The announced successor (B-D-14): shipped here *before* the directory
#: switches to it, so rotation is a routine update. Its id carries the
#: planned ``valid_from`` month; ``valid_from`` in the directory is
#: authoritative.
SUCCESSOR_KEY: Final[bytes] = bytes.fromhex(
    "095cb28132333f9187bd472ce3aff39f5bf66b9a14b49a140e5d252417e37670"
)
SUCCESSOR_KEY_ID: Final[str] = "portfoliflow-2027-01"

#: The key ring (B-D-14): every key a client may verify a directory with,
#: by id. Selection by the document's ``publishing_key_id`` happens above
#: :func:`~services.provider_channel.directory.verify_directory`, which
#: still takes exactly one key. Read-only.
PUBLISHING_KEY_RING: Final[Mapping[str, bytes]] = MappingProxyType(
    {PUBLISHING_KEY_ID: PUBLISHING_KEY, SUCCESSOR_KEY_ID: SUCCESSOR_KEY}
)


def is_placeholder(key: bytes) -> bool:
    """Report whether ``key`` is the un-minted stand-in.

    The test is by **value**, not by validity: an all-zero byte string is an
    acceptable Ed25519 public-key encoding to some libraries and not to
    others, and the trust gate must not depend on which. Comparing the value
    also means a test that deliberately signs with an all-zero seed still
    cannot verify against the placeholder.

    Args:
        key: Raw Ed25519 public-key bytes.

    Returns:
        ``True`` if this is :data:`PUBLISHING_KEY_PLACEHOLDER`.
    """
    return key == PUBLISHING_KEY_PLACEHOLDER


__all__ = [
    "PUBLISHING_KEY",
    "PUBLISHING_KEY_ID",
    "PUBLISHING_KEY_PLACEHOLDER",
    "PUBLISHING_KEY_RING",
    "SUCCESSOR_KEY",
    "SUCCESSOR_KEY_ID",
    "is_placeholder",
]
