# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""What the instance can say about the directory it holds (SB-3b).

Provenance is the honest answer to "where does this list of counterparties
come from, and may I act on it?" — version, signing key, dates, and the two
rotation notices, assembled from the *verified* copy rather than from the
sidecar. It exists as its own shape so the surfaces that display it (SB-6)
cannot each invent their own version of the truth, and so the arming layer
can hand a caller the facts without also handing it opinions.

**Facts, not wording.** Nothing here formats a sentence, chooses a colour or
decides what an operator should be told. :attr:`Provenance.valid` says whether
the cached bytes verified at read time; what a surface *does* about an
expired directory is that surface's decision.

**Expired still has provenance.** A directory outside its window keeps its
version, its key and its dates — that is precisely the information needed to
say "this instance last saw version 7, published on the 10th, and it has gone
stale". The fields that only a verification can supply
(:attr:`announced_successor`) are ``None`` there, rather than guessed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from services.provider_channel.ring import SuccessorStatus
from services.provider_directory.cache import CachedDirectory


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where the held directory came from and whether it may be acted on.

    Attributes:
        directory_version: The monotonic publication counter of the held copy.
        publishing_key_id: The key id that signed it.
        fetched_at: When this instance last obtained or re-confirmed it.
        issued_at: First day of its validity window.
        valid_until: Last day of its validity window.
        etag: The etag it answers to, if the server sent one.
        valid: ``True`` when the copy verified at read time; ``False`` when it
            is signed but outside its window.
        successor_in_use: ``True`` when the signing key is not the one this
            build considers current — the rotation has happened.
        announced_successor: How the document's own successor announcement
            compares to the shipped ring, or ``None`` when it announces none —
            and always ``None`` for an expired copy, whose announcement was
            never read.
        provider_count: How many entries the document carries.
        source_url: Where the copy was fetched from.
    """

    directory_version: int
    publishing_key_id: str
    fetched_at: datetime
    issued_at: date
    valid_until: date
    etag: str | None
    valid: bool
    successor_in_use: bool
    announced_successor: SuccessorStatus | None
    provider_count: int
    source_url: str


def provenance_from_cache(cached: CachedDirectory, *, current_key_id: str) -> Provenance:
    """Describe a cached copy.

    A verified copy is described from its verification — the parsed document
    is the one reading of the bytes that has been proved. An expired copy is
    described from the signed bytes directly: its dates were read by the trust
    gate before it refused the window, and its provider list is published
    material, so ``json.loads`` here reads facts rather than claims. No shape
    parsing is attempted beyond the three members needed, because an expired
    document never reached the shape parser and is not owed a second opinion.

    Args:
        cached: The copy, as :func:`~services.provider_directory.cache.read_cache`
            returned it.
        current_key_id: The id this build considers current.

    Returns:
        The provenance of the held copy.
    """
    verification = cached.verification
    if verification is not None:
        directory = verification.directory
        return Provenance(
            directory_version=cached.directory_version,
            publishing_key_id=cached.publishing_key_id,
            fetched_at=cached.meta.fetched_at,
            issued_at=directory.issued_at,
            valid_until=directory.valid_until,
            etag=cached.meta.etag,
            valid=True,
            successor_in_use=verification.successor_in_use,
            announced_successor=verification.announced_successor,
            provider_count=len(directory.providers),
            source_url=cached.meta.source_url,
        )

    issued_at, valid_until, provider_count = _expired_facts(cached.document_bytes)
    return Provenance(
        directory_version=cached.directory_version,
        publishing_key_id=cached.publishing_key_id,
        fetched_at=cached.meta.fetched_at,
        issued_at=issued_at,
        valid_until=valid_until,
        etag=cached.meta.etag,
        valid=False,
        successor_in_use=cached.publishing_key_id != current_key_id,
        announced_successor=None,
        provider_count=provider_count,
        source_url=cached.meta.source_url,
    )


def provenance_to_dict(provenance: Provenance) -> dict[str, object]:
    """Render provenance as JSON-ready primitives.

    Dates and the timestamp become ISO-8601 strings. SB-6 renders this; the
    wording of what an operator reads is decided there, not here.

    Args:
        provenance: The provenance to render.

    Returns:
        A ``json.dumps``-able mapping.
    """
    return {
        "directory_version": provenance.directory_version,
        "publishing_key_id": provenance.publishing_key_id,
        "fetched_at": provenance.fetched_at.isoformat(),
        "issued_at": provenance.issued_at.isoformat(),
        "valid_until": provenance.valid_until.isoformat(),
        "etag": provenance.etag,
        "valid": provenance.valid,
        "successor_in_use": provenance.successor_in_use,
        "announced_successor": provenance.announced_successor,
        "provider_count": provenance.provider_count,
        "source_url": provenance.source_url,
    }


def _expired_facts(document_bytes: bytes) -> tuple[date, date, int]:
    """Read the dates and the entry count out of an expired document.

    The window the gate refused was read from these same two members, so they
    are well-formed by construction. ``providers`` is not: the shape parser
    never ran, so a list is counted and anything else counts as none rather
    than raising over a document the caller already knows it cannot act on.
    """
    decoded = json.loads(document_bytes.decode("utf-8"))
    providers = decoded.get("providers")
    return (
        date.fromisoformat(decoded["issued_at"]),
        date.fromisoformat(decoded["valid_until"]),
        len(providers) if isinstance(providers, list) else 0,
    )


__all__ = [
    "Provenance",
    "provenance_from_cache",
    "provenance_to_dict",
]
