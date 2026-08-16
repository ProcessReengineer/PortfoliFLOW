# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Correlation lift: merge RSS corroboration into internal edges (ADR-0087).

The "denominator demo" of ADR-0087 Part B §1.5. When an RSS bucket's tag
corresponds to an internal asset class that *also* has a coincident edge
this beat, the two are not two separate cards — the internal finding is the
card, and the RSS item(s) ride along as corroborating basis/context. The
standalone RSS eligible is suppressed so the portfolio manager sees one
corroborated internal card, not two.

The correspondence is deterministic and keyed by **tag ↔ asset-class**, via
the auditable ``DeltaThresholds.tag_asset_class_map`` (never by
``source_name`` — a cross-source event must be able to corroborate). This
module is pure: it takes the two eligible lists plus the mapping and
returns a :class:`CorrelationResult`; it performs no I/O.

This module only **detects and merges**; it applies no urgency cap. By the
time the deterministic floor (:mod:`services.analytics.irene_floor`) runs in
the beat, a corroborated RSS item has already been merged into its internal
finding here, so it reaches the floor as ``source=internal`` (uncapped by
the RSS source cap). The ``standalone_rss`` this module returns are the
genuinely RSS-only findings, which the floor caps at the ``informational``
band. The cap therefore needs no "is-correlated" flag — correlation
resolved it upstream (ADR-0087 §1.5 / ADR-0088).
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from services.irene.internal_delta import EligibleFinding
from services.irene.rss_delta import RssEligibleFinding


@dataclass(frozen=True)
class MergedInternalFinding:
    """One internal eligible plus any RSS eligibles corroborating it.

    Attributes:
        internal: The internal (limit-coverage) eligible finding — the
            card the PM sees.
        corroborating: The RSS eligibles whose tag corresponds to this
            internal finding's asset class, attached as basis/context.
            Empty when nothing external corroborates.
    """

    internal: EligibleFinding
    corroborating: tuple[RssEligibleFinding, ...]


@dataclass(frozen=True)
class CorrelationResult:
    """The outcome of correlating internal and RSS eligibles.

    Attributes:
        merged: Every internal eligible (order preserved), each with its
            corroborating RSS eligibles attached.
        standalone_rss: The RSS eligibles that corresponded to no internal
            edge and therefore stand on their own.
    """

    merged: tuple[MergedInternalFinding, ...]
    standalone_rss: tuple[RssEligibleFinding, ...]


def _class_token(subject_key: str) -> str:
    """The internal class token — the part after the subject-type prefix.

    ``saa:equities`` → ``equities``; ``anlv:anlv_1`` → ``anlv_1``. This is
    the token the tag→asset-class map is matched against.
    """
    return subject_key.split(":", 1)[1] if ":" in subject_key else subject_key


def correlate(
    internal: list[EligibleFinding],
    rss: list[RssEligibleFinding],
    *,
    tag_asset_class_map: Mapping[str, tuple[str, ...]],
) -> CorrelationResult:
    """Merge corroborating RSS eligibles into coincident internal edges.

    Deterministic: iterates internal and RSS eligibles in list order. For
    each RSS eligible, each of its tags maps to a set of internal class
    tokens; any internal eligible whose class token is in that set gets the
    RSS eligible attached, and the RSS eligible is suppressed from the
    standalone list. An RSS eligible matching several internal edges is
    attached to each (deduplicated per internal by ``subject_key``); a broad
    tag that maps to no class (``macro`` / ``regulator`` / ``swiss_finance``)
    never merges and its bucket stands alone.

    Args:
        internal: The internal eligible findings for this beat.
        rss: The RSS eligible findings for this beat.
        tag_asset_class_map: The auditable tag → asset-class correspondence
            (from ``DeltaThresholds.tag_asset_class_map``).

    Returns:
        The :class:`CorrelationResult` — every internal eligible with its
        corroboration, plus the unmatched (standalone) RSS eligibles.
    """
    attach: list[list[RssEligibleFinding]] = [[] for _ in internal]
    attached_keys: list[set[str]] = [set() for _ in internal]
    suppressed: set[int] = set()

    for j, r in enumerate(rss):
        for tag in r.tags:
            classes = tag_asset_class_map.get(tag, ())
            if not classes:
                continue
            for i, e in enumerate(internal):
                if _class_token(e.subject_key) in classes:
                    if r.subject_key not in attached_keys[i]:
                        attach[i].append(r)
                        attached_keys[i].add(r.subject_key)
                    suppressed.add(j)

    merged = tuple(
        MergedInternalFinding(internal=e, corroborating=tuple(attach[i]))
        for i, e in enumerate(internal)
    )
    standalone = tuple(r for j, r in enumerate(rss) if j not in suppressed)
    return CorrelationResult(merged=merged, standalone_rss=standalone)


__all__ = [
    "CorrelationResult",
    "MergedInternalFinding",
    "correlate",
]
