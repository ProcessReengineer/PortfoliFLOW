# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Deterministic dedup key for live cashflow ingest (ADR-0092).

``investment_cashflows`` carries **no** unique constraint by design —
multiple same-day flows of the same type are legitimate (ADR-0043 §1).
The live-import write path therefore cannot lean on a DB key for
idempotency; it needs an explicit, deterministic dedup key computed
**rule-based** from the row's identity fields, never formed by a model.
This mirrors the key-forming discipline of Irene's RSS ``subject_key``
(:mod:`services.analytics.rss_bucketing`) and is guarded by the same
kind of purity test (``tests/regression/test_cashflow_dedup_key_pure.py``).

The key is compared **in memory** to decide insert-vs-skip during a live
fetch; it is *not* a stored column, so it imposes no schema change and no
migration. A live cashflow whose key matches an existing ``'excel'`` or
``'manual'`` row is skipped (those origins are authoritative); a match
against a prior ``'live'`` row is a no-op (already present); no match is
an insert.

Canonicalisation is made fully explicit here — the same logical cashflow
must always hash to the same key regardless of incidental representation:

* ``investment_id`` — the UUID's canonical lower-case hyphenated string.
* ``flow_timestamp`` — normalised to **UTC** ISO-8601. A tz-aware value
  is converted to UTC; a naive value is *assumed* UTC (the extractor and
  the live adapters both stamp 12:00 UTC when the wall-clock time is
  unknown, ADR-0043 §3 / ADR-0091 property 3), so a naive and an
  explicit-UTC stamp for the same instant collide.
* ``amount`` — ``str(amount.normalize())``. ``normalize()`` strips
  trailing zeros and exponent scale, so numerically-equal amounts
  (``Decimal("10")``, ``Decimal("10.0")``, ``Decimal("1E+1")``) yield the
  **same** key: a €10 flow is a €10 flow however many trailing zeros the
  source carried. (No float ever enters the computation — the money
  convention forbids it.)
* ``flow_type`` / ``flow_kind`` — the canonical string values as-is (both
  are closed CHECK sets; the caller passes canonical values).
* ``source`` — ``None`` maps to a fixed control-character sentinel
  distinct from every real value, so ``source=None`` (historical /
  Excel), ``source=""`` (an empty but present value) and a real provider
  name are three distinct keys.

The parts are joined with the ASCII unit-separator (``\\x1f``) — a byte
that does not occur in a UUID, an ISO-8601 timestamp, a normalised
Decimal, a canonical flow_type/flow_kind, or a provider name — so the
concatenation is unambiguous. The UTF-8 encoding is hashed with SHA-256
and returned as hex.

This module is import-pure: it touches no database, no LLM, no network,
and no provider SDK — only the standard library.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

# ASCII unit separator — the field delimiter. Absent from every field's
# canonical form, so the joined string is unambiguous.
_FIELD_SEP: str = "\x1f"

# Sentinel standing in for a missing ``source``. The NUL bytes cannot
# occur in a real provider string, keeping ``None`` distinct from ``""``.
_SOURCE_NONE_SENTINEL: str = "\x00source-none\x00"


def _canonical_timestamp(flow_timestamp: datetime) -> str:
    """Return ``flow_timestamp`` as a UTC ISO-8601 string.

    A tz-aware value is converted to UTC; a naive value is assumed to be
    UTC (the 12:00-UTC convention the extractor and live adapters use).
    """
    if flow_timestamp.tzinfo is None:
        aware = flow_timestamp.replace(tzinfo=timezone.utc)
    else:
        aware = flow_timestamp.astimezone(timezone.utc)
    return aware.isoformat()


def _canonical_amount(amount: Decimal) -> str:
    """Return a canonical string for ``amount`` collapsing equal values.

    ``normalize()`` reduces trailing zeros and scale, so ``Decimal("10")``,
    ``Decimal("10.0")`` and ``Decimal("1E+1")`` all canonicalise
    identically. ``normalize()`` of a zero collapses ``-0`` / ``0.00`` to
    ``0`` as well.
    """
    return str(amount.normalize())


def compute_cashflow_dedup_key(
    *,
    investment_id: UUID,
    flow_timestamp: datetime,
    flow_type: str,
    flow_kind: str,
    amount: Decimal,
    source: str | None,
) -> str:
    """Compute the deterministic SHA-256 dedup key for one cashflow.

    Deterministic and order-independent: the same logical cashflow always
    yields the same hex key regardless of keyword ordering or incidental
    representation of the timestamp / amount / source (see the module
    docstring for the exact canonicalisation rules).

    Args:
        investment_id: The investment the flow belongs to.
        flow_timestamp: The flow event timestamp (normalised to UTC).
        flow_type: One of the eight canonical ``flow_type`` values.
        flow_kind: ``'plan'`` or ``'actual'``.
        amount: The signed cashflow amount as a :class:`~decimal.Decimal`.
        source: The free-text provenance, or ``None``.

    Returns:
        The 64-character lower-case SHA-256 hex digest of the canonical
        field concatenation.
    """
    parts: tuple[str, ...] = (
        str(investment_id),
        _canonical_timestamp(flow_timestamp),
        flow_type,
        flow_kind,
        _canonical_amount(amount),
        _SOURCE_NONE_SENTINEL if source is None else source,
    )
    joined = _FIELD_SEP.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
