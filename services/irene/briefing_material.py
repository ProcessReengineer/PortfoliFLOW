# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The briefing writer's material — what Irene is handed (ADR-0133 §1.2).

Two parts, assembled once per check:

* **Part 1 — markets.** The market read (instrument, currency and
  asset-class moves over the fixed window) plus the press clusters: those
  on ``macro`` and ``regulator`` always, those on every other tag only while
  Press coverage is on and the tag is not muted.
* **Part 2 — watch.** The live status of every switched-on family, the
  findings this check appended with their final band, and how many remain
  open per switched-on family. A switched-off family is absent, not marked.

The result is persisted verbatim as ``irene_briefing.inputs`` (JSONB), so
every value in it is JSON-native: decimals as exact fixed-point strings,
dates and instants in ISO form, ids as strings. :data:`MATERIAL_VERSION`
travels in the payload so readers of old rows can branch on its shape.

Read-only, and no model call: nothing here writes and nothing here prompts.
Status and market arrive through
:func:`~services.watch_desk.family_status.read_family_status` and
:func:`~services.watch_desk.market_read.read_market` alone, so the briefing
and the watch strip share one derivation — this module classifies nothing,
resolves nothing and observes nothing itself. The resolution is handed in by
the caller.

``nothing_to_write`` follows ADR-0133 §1.3: no instrument move, no currency
move, no briefed cluster and no switched-on family means there is nothing
to brief, and no call is made.

The beat is the caller: :func:`gather_briefing_material` performs the three
reads on its tenant-scoped session, :func:`compose_briefing_material` is the
pure assembly beneath it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.models.watchpoint import WATCHPOINT_FAMILIES
from core.repositories.irene_finding_repository import (
    IreneFindingDTO,
    IreneFindingRepository,
)
from services.irene.rss_clustering import RssBucket
from services.watch_desk.family_status import (
    FamilyStatus,
    LimitSubjectStatus,
    SignalSubjectStatus,
    read_family_status,
)
from services.watch_desk.market_read import (
    AssetClassMove,
    FxMove,
    InstrumentMove,
    MarketRead,
    read_market,
)
from services.watch_desk.overlay import WatchDeskResolution, rss_overlay_subject_key

#: Bump when the shape of ``inputs`` changes; readers of old rows branch on it.
MATERIAL_VERSION: int = 1

#: Press tags handed to the writer regardless of edge state, mute or the Press switch (D-WD-2).
ALWAYS_BRIEFED_TAGS: tuple[str, ...] = ("macro", "regulator")

#: Where each figure was read from — the Sources line (R12) renders these.
SOURCE_INSTRUMENT_PRICES: str = "instrument_prices"
SOURCE_FX_RATES: str = "fx_rates"
SOURCE_DERIVED: str = "derived"  # value-weighted asset-class move
SOURCE_LIMIT_COVERAGE: str = "limit_coverage"
SOURCE_WATCHPOINT_OBSERVATION: str = "watchpoint_observation"
SOURCE_RSS: str = "rss"

#: The unit a limit subject's coverage is stated in.
_LIMIT_UNIT: str = "pct"


@dataclass(frozen=True)
class BriefingMaterial:
    """The material for one check's briefing.

    Attributes:
        inputs: JSON-native; persisted verbatim as ``irene_briefing.inputs``.
        nothing_to_write: ADR-0133 §1.3 — no call is made when ``True``.
    """

    inputs: dict[str, Any]
    nothing_to_write: bool


def _json_decimal(value: Decimal | None) -> str | None:
    """State a decimal exactly, never in exponent notation."""
    return None if value is None else format(value, "f")


def _json_date(value: date | datetime | None) -> str | None:
    """State a date or an instant in ISO form."""
    return None if value is None else value.isoformat()


def _market_figure(move: InstrumentMove | FxMove | AssetClassMove, source: str) -> dict[str, Any]:
    """Project one move onto the market figure record; levels stay behind."""
    return {
        "id": move.id,
        "label": move.label,
        "value": _json_decimal(move.value),
        "unit": move.unit,
        "as_of": _json_date(move.as_of),
        "window_start": _json_date(move.window_start),
        "window_days": move.window_days,
        "source": source,
    }


def _briefed(bucket: RssBucket, resolution: WatchDeskResolution) -> bool:
    """Return whether one press cluster reaches Part 1 (D-WD-19).

    ``macro`` and ``regulator`` always do. Every other tag — the
    ``untagged`` residue included, which can never be muted — does only
    while Press coverage is on and the tag is not muted.
    """
    if bucket.tag in ALWAYS_BRIEFED_TAGS:
        return True
    return resolution.is_family_enabled("rss") and not resolution.is_muted(
        rss_overlay_subject_key(bucket.tag)
    )


def _cluster(bucket: RssBucket) -> dict[str, Any]:
    """Project one press cluster, its members in their canonical order."""
    return {
        "id": bucket.subject_key,
        "tag": bucket.tag,
        "day": _json_date(bucket.day_bucket),
        "source": SOURCE_RSS,
        "items": [
            {
                "title": member.title,
                "source": member.source_name,
                "url": member.url,
                "published_at": _json_date(member.published_at),
            }
            for member in bucket.members
        ],
    }


def _limit_figure(subject: LimitSubjectStatus, coverage_as_of: date | None) -> dict[str, Any]:
    """Project one limit subject as the engine classified it."""
    return {
        "id": subject.subject_key,
        "label": subject.class_key,
        "family": subject.family,
        "value": _json_decimal(subject.coverage_pct),
        "unit": _LIMIT_UNIT,
        "max_pct": _json_decimal(subject.max_pct),
        "utilisation_pct": _json_decimal(subject.utilisation_pct),
        "warn_threshold_pct": _json_decimal(subject.warn_threshold_pct),
        "status": subject.status,
        "muted": subject.muted,
        "as_of": _json_date(coverage_as_of),
        "source": SOURCE_LIMIT_COVERAGE,
    }


def _signal_figure(subject: SignalSubjectStatus) -> dict[str, Any]:
    """Project one signal subject in the words the monitor prints."""
    figure = subject.figure
    return {
        "id": subject.subject_key,
        "label": subject.display_name,
        "family": subject.family,
        "value": _json_decimal(figure.value) if figure is not None else None,
        "threshold": _json_decimal(figure.threshold) if figure is not None else None,
        "unit": figure.unit if figure is not None else None,
        "status": subject.status_label,
        "window_days": subject.window_days,
        "as_of": _json_date(subject.as_of),
        "muted": subject.muted,
        "no_data_reason": subject.no_data_reason,
        "source": SOURCE_WATCHPOINT_OBSERVATION,
    }


def _finding(finding: IreneFindingDTO) -> dict[str, Any]:
    """Project one finding of this check: its band and its own words only."""
    trigger = finding.payload.get("trigger")
    text = finding.payload.get("finding")
    return {
        "id": str(finding.id),
        "subject_key": finding.subject_key,
        "band": finding.band,
        "urgency": finding.urgency,
        "trigger": trigger if isinstance(trigger, str) else None,
        "finding": text if isinstance(text, str) else None,
    }


def compose_briefing_material(
    *,
    market: MarketRead,
    status: FamilyStatus,
    rss_buckets: Sequence[RssBucket],
    findings: Sequence[IreneFindingDTO],
    open_counts: Mapping[str, int],
    resolution: WatchDeskResolution,
    now: datetime,
) -> BriefingMaterial:
    """Assemble the briefing material from what the check already read.

    Pure and deterministic: no I/O and no clock read. Every order is the
    order of the records handed in.

    Args:
        market: The market read at the check's date.
        status: The live status of every switched-on family.
        rss_buckets: The press clusters the check formed, in their order.
        findings: The findings this check appended, with their final band.
        open_counts: Open findings per watchpoint family.
        resolution: The resolution the check ran on; decides which families
            and press tags are briefed.
        now: The check's instant.

    Returns:
        The :class:`BriefingMaterial`, its ``inputs`` JSON-native.
    """
    markets: dict[str, Any] = {
        "as_of": _json_date(market.as_of),
        "window_start": _json_date(market.window_start),
        "window_days": market.window_days,
        "instruments": [
            _market_figure(move, SOURCE_INSTRUMENT_PRICES) for move in market.instruments
        ],
        "asset_classes": [
            {
                **_market_figure(move, SOURCE_DERIVED),
                "instrument_count": move.instrument_count,
            }
            for move in market.asset_classes
        ],
        "currencies": [_market_figure(move, SOURCE_FX_RATES) for move in market.fx],
        "press_clusters": [
            _cluster(bucket) for bucket in rss_buckets if _briefed(bucket, resolution)
        ],
    }

    families_on = [family for family in WATCHPOINT_FAMILIES if resolution.is_family_enabled(family)]
    watch: dict[str, Any] = {
        "families_on": families_on,
        "evaluated_at": _json_date(status.evaluated_at),
        "limits": [
            {
                "family": group.family,
                "as_of": _json_date(status.coverage_as_of),
                "subjects": [
                    _limit_figure(subject, status.coverage_as_of) for subject in group.subjects
                ],
            }
            for group in status.limits
        ],
        "signals": [_signal_figure(subject) for subject in status.signals],
        "findings_this_check": [_finding(finding) for finding in findings],
        "open_findings": {family: open_counts.get(family, 0) for family in families_on},
    }
    if status.press is not None:
        watch["press_tags"] = [{"tag": tag.tag, "muted": tag.muted} for tag in status.press]

    nothing_to_write = (
        not market.instruments
        and not market.fx
        and not markets["press_clusters"]
        and not resolution.enabled_families
    )
    return BriefingMaterial(
        inputs={
            "version": MATERIAL_VERSION,
            "check_at": _json_date(now),
            "markets": markets,
            "watch": watch,
        },
        nothing_to_write=nothing_to_write,
    )


async def gather_briefing_material(
    session: AsyncSession,
    *,
    resolution: WatchDeskResolution,
    now: datetime,
    rss_buckets: Sequence[RssBucket],
    findings: Sequence[IreneFindingDTO],
) -> BriefingMaterial:
    """Read the market, the family status and the open counts, then compose.

    Three reads, each once, on the caller's tenant-scoped session. Nothing
    is caught here: the step that calls this owns failure handling.

    Args:
        session: The tenant-scoped session opened by the caller.
        resolution: The resolution the check ran on, resolved by the caller.
        now: The check's instant; its date is the market read's date.
        rss_buckets: The press clusters the check formed.
        findings: The findings this check appended.

    Returns:
        The :class:`BriefingMaterial`.
    """
    market = await read_market(session, as_of=now.date())
    status = await read_family_status(session, resolution=resolution, now=now)
    open_counts = await IreneFindingRepository(session).count_open_by_family()
    return compose_briefing_material(
        market=market,
        status=status,
        rss_buckets=rss_buckets,
        findings=findings,
        open_counts=open_counts,
        resolution=resolution,
        now=now,
    )


__all__ = [
    "ALWAYS_BRIEFED_TAGS",
    "MATERIAL_VERSION",
    "SOURCE_DERIVED",
    "SOURCE_FX_RATES",
    "SOURCE_INSTRUMENT_PRICES",
    "SOURCE_LIMIT_COVERAGE",
    "SOURCE_RSS",
    "SOURCE_WATCHPOINT_OBSERVATION",
    "BriefingMaterial",
    "compose_briefing_material",
    "gather_briefing_material",
]
