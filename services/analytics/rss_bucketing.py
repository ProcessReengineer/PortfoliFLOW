# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure, deterministic RSS bucket key-forming (ADR-0087 Part B).

This module holds the *key-forming core* of Irene's RSS delta: given a
day-bucket, a curated tag, and the stable membership of a semantic
cluster, it forms the cluster's ``subject_key`` — deterministically, with
no I/O and no dependence on any model.

It lives under :mod:`services.analytics` and is therefore held to the
analytics purity contract (ADR-0013 / ADR-0045 §3), enforced by
``tests/regression/test_analytics_layer_pure.py`` (no database, no ORM,
no FastAPI, no Qt) **and** by
``tests/regression/test_irene_key_forming_pure.py`` (no model call of any
kind — the key is formed before any model sees the items, and never by
one). The *impure* orchestration that vectorises items, reads/writes the
database, and computes cluster memberships lives in
:mod:`services.irene.rss_clustering`; it hands this module
already-computed plain data (a day-bucket, a tag, and a frozen set of
stable item identities) and this module forms the key with no I/O.

Determinism is the whole point (ADR-0087 §Compliance). The key is:

* independent of the model that produced the semantic clustering — it is
  a hash over the cluster's *membership*, never over any vector, so a
  model change cannot re-form or re-key an existing cluster;
* order-independent — the membership is a set, hashed via its sorted
  members, so the same cluster hashes identically regardless of the order
  items arrived in;
* prefix-compatible with the internal delta axis — the ``rss:cluster:``
  prefix means :func:`services.analytics.irene_delta.subject_type_from_key`
  already resolves these keys to ``subject_type == "rss"`` with no change.

The ``rss`` subject_type this prefix resolves to feeds the ADR-0088 floor,
which caps a standalone RSS finding at the ``informational`` band
(:mod:`services.analytics.irene_floor`). This module's key contract is
unaffected by that: the key stays a membership hash, formed before any
model or floor runs.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime

# The stable prefix for every RSS cluster subject key. Kept as a module
# constant so callers compare against one source of truth and the delta
# axis (``subject_type == "rss"``) stays stable.
SUBJECT_KEY_PREFIX: str = "rss:cluster:"

# Field separator used when serialising the key material. A control
# character that cannot occur in a URL, a tag, or an ISO date, so the
# serialisation is unambiguous (no member can be confused with a
# separator).
_SEP: str = "\x1f"

# Hex digest length kept for the key. 16 hex chars = 64 bits of the
# SHA-256 digest — ample to avoid collisions across a tenant's live
# clusters while keeping the key short and human-scannable in logs.
_DIGEST_CHARS: int = 16


def item_identity(*, published_at: datetime, url: str) -> str:
    """Return the stable identity string for one feed item.

    The identity is the ``(published_at, url)`` pair rendered
    deterministically. It is the atom of cluster membership: two runs that
    see the same item produce the same identity, and the identity never
    depends on any vector or model. Both the orchestration (when it builds
    a cluster's membership and its persisted ``member_ids``) and
    :func:`form_subject_key` (when it hashes that membership) use this one
    function, so the key material is formed one way only.

    Args:
        published_at: The item's timezone-aware publication timestamp.
        url: The item's canonical URL.

    Returns:
        A stable identity string, ``"<iso-timestamp>|<url>"``.
    """
    return f"{published_at.isoformat()}|{url}"


def _hash(day_bucket: date, tag: str, membership: frozenset[str]) -> str:
    """Hash the cluster's key material into a short, stable hex digest.

    Order-independent: the membership is serialised via its *sorted*
    members, so the digest is a function of the set, not of insertion
    order. Uses SHA-256 (not the salted built-in ``hash``) so the digest
    is stable across processes and runs — a requirement for reproducible,
    auditable keys.
    """
    parts = [day_bucket.isoformat(), tag, *sorted(membership)]
    joined = _SEP.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def form_subject_key(*, day_bucket: date, tag: str, membership: frozenset[str]) -> str:
    """Form the deterministic ``subject_key`` for one RSS cluster.

    Pure and total: no database, no network, no model. The returned key is
    ``rss:cluster:<hash>`` where ``<hash>`` is a stable, order-independent
    digest of ``(day_bucket, tag, membership)``. ``membership`` is the set
    of stable item identities (see :func:`item_identity`) — **never** a
    vector, which is exactly what makes the key survive a model change.

    Args:
        day_bucket: The UTC calendar day the cluster belongs to.
        tag: The curated bucket-dimension tag (or the reserved
            ``"untagged"`` sentinel).
        membership: The frozen set of stable item identities in the
            cluster.

    Returns:
        The cluster's ``subject_key`` (``rss:cluster:<hash>``).
    """
    return f"{SUBJECT_KEY_PREFIX}{_hash(day_bucket, tag, membership)}"


__all__ = ["SUBJECT_KEY_PREFIX", "form_subject_key", "item_identity"]
