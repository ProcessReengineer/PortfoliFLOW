# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The portfoliflow.com directory publishing key (ADR-0129 §2).

The provider directory is trusted because it is *signed*, not because of where
it was fetched from. That makes this file the root of the channel's trust: a
client verifies every directory against the key below, so substituting this
key — not breaking the cipher — is the attack worth defending against. The key
ships in the AGPL repository precisely so that substitution is a visible diff
rather than a silent server-side swap.

Stage A ships a **placeholder**, and verification fails closed against it:
:func:`services.provider_channel.directory.verify_directory` refuses the
placeholder before it reads a single byte of the document. Nothing can be
trusted by accident in the window between this contract landing and the real
key being minted.
"""

from __future__ import annotations

from typing import Final

#: The all-zero stand-in for the real Ed25519 public key.
#:
#: PLACEHOLDER — the real key is minted by the operator in Stage B (concept
#: chat "directory format & publishing-key lifecycle") and lands here as a
#: one-line change. Until then verification MUST fail closed:
#: ``verify_directory`` refuses this value before reading the document.
PUBLISHING_KEY_PLACEHOLDER: Final[bytes] = b"\x00" * 32

#: The key callers pass to :func:`verify_directory`. Identical to the
#: placeholder until Stage B replaces it.
PUBLISHING_KEY: Final[bytes] = PUBLISHING_KEY_PLACEHOLDER

#: Identifier of the key above, matched against a document's
#: ``publishing_key_id``. Renamed together with the key it names.
PUBLISHING_KEY_ID: Final[str] = "portfoliflow-publishing-key-placeholder"


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
    "is_placeholder",
]
