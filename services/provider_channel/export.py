# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Sealing an order into a downloadable export envelope (ADR-0129 §6, Stage B).

The one place in this package where a payload meets a cipher. Everything on
either side of it stays ignorant of the other:
:mod:`~services.provider_channel.schemas` describes shapes and never
encrypts, :mod:`~services.provider_channel.sealed_box` encrypts bytes and
never learns what an order is, and this module is the seam that puts the two
together exactly once.

**One canonical form, not two.** The plaintext is
:func:`~services.provider_channel.directory.canonical_bytes` over the payload
dict — the directory's canonical form, reused rather than restated. A second
serialisation written here would be a second answer to "which bytes mean this
document", and the whole point of the directory's rule is that there is only
one.

**The artefact is parsed before it is returned.** :func:`seal_export` hands
its own candidate to :func:`~services.provider_channel.schemas.parse_export_envelope`
and returns what comes back, so an envelope this build cannot read is never
produced, and every refusal — a naive ``exported_at`` among them — is the
parser's own, in the parser's words. That is the ``ring.py`` rule applied
here: the refusal vocabulary exists once.

Pure: no repository, no session, no network, and no clock — ``exported_at``
is supplied by the caller (D-clock).
"""

from __future__ import annotations

import base64
from datetime import datetime

from services.provider_channel.directory import (
    KEY_TYPE_X25519_SEALED_BOX,
    ProviderEntry,
    canonical_bytes,
)
from services.provider_channel.schemas import (
    EXPORT_SCHEMA_VERSION,
    ExportEnvelope,
    ExportPayload,
    export_envelope_to_dict,
    export_to_dict,
    parse_export_envelope,
)
from services.provider_channel.sealed_box import seal


class UnsupportedEncryptionKeyType(ValueError):
    """The directory entry names a key type this build cannot seal to.

    Refused before any cipher call: a provider published with a key family
    this build does not implement gets no export at all, rather than one
    sealed by whatever primitive happened to be at hand.
    """


def export_plaintext_bytes(payload: ExportPayload) -> bytes:
    """Serialise a payload to the exact bytes that get sealed.

    Stated as a function of its own because the portal on the other side must
    reproduce *these* bytes to check what it decrypted: sorted keys, no
    whitespace, UTF-8, every optional present as ``null``. A portal that
    re-serialises the decrypted document its own way and compares will find
    the two differ for reasons that have nothing to do with the message.

    Args:
        payload: The plaintext to serialise.

    Returns:
        The canonical UTF-8 bytes of the payload document.
    """
    return canonical_bytes(export_to_dict(payload))


def seal_export(
    payload: ExportPayload,
    *,
    provider: ProviderEntry,
    sender_tenant_handle: str | None,
    exported_at: datetime,
) -> ExportEnvelope:
    """Seal a payload to a directory entry's key and wrap it for download.

    The entry comes from a directory that has already been verified —
    sealing to a key nobody vouched for would be encryption without trust —
    and its ``encryption_key_type`` is checked before the key is touched.

    Args:
        payload: The order's own parameters (ADR-0129 §6).
        provider: The verified directory entry for the recipient. Supplies
            the provider id, the key type and the public key.
        sender_tenant_handle: The sending tenant's channel handle, or
            ``None`` where none is issued yet (version 1 allows the absence).
        exported_at: Offset-bearing timestamp for the artefact; injected,
            never read from a clock here (D-clock).

    Returns:
        The :class:`~services.provider_channel.schemas.ExportEnvelope`, as
        returned by the parser that read this build's own candidate back.

    Raises:
        UnsupportedEncryptionKeyType: If the entry names a key type other
            than :data:`~services.provider_channel.directory.KEY_TYPE_X25519_SEALED_BOX`.
        SchemaError: If the candidate envelope does not parse — a naive
            ``exported_at`` being the refusal a caller is most likely to meet.
        InvalidRecipientKey: If the entry's public key is not 32 bytes.
    """
    if provider.encryption_key_type != KEY_TYPE_X25519_SEALED_BOX:
        raise UnsupportedEncryptionKeyType(
            f"provider {provider.provider_id!r} publishes encryption_key_type "
            f"{provider.encryption_key_type!r}; this build seals to "
            f"{KEY_TYPE_X25519_SEALED_BOX!r} only"
        )

    recipient = bytes.fromhex(provider.encryption_public_key)
    ciphertext = seal(export_plaintext_bytes(payload), recipient)

    candidate = ExportEnvelope(
        schema_version=EXPORT_SCHEMA_VERSION,
        provider_id=provider.provider_id,
        encryption_key_type=provider.encryption_key_type,
        recipient_public_key=provider.encryption_public_key,
        sender_tenant_handle=sender_tenant_handle,
        correlation_id=payload.correlation_id,
        message_type=payload.message_type,
        exported_at=exported_at,
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
    )
    return parse_export_envelope(export_envelope_to_dict(candidate))


__all__ = [
    "UnsupportedEncryptionKeyType",
    "export_plaintext_bytes",
    "seal_export",
]
