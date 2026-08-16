# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Stateful orchestration for Irene's internal delta (ADR-0087).

This module turns the deterministic limit-coverage snapshot into a list
of *eligible findings* — the internal half of Irene's delta layer. It is
the DB-aware counterpart to the pure comparison in
:mod:`services.analytics.irene_delta`: it reads the coverage bundle and
the ``irene_watch_state`` rows, runs the pure edge/re-trigger decision
per subject, and writes the acknowledgement / reset that suppresses a
level from re-firing on the next beat.

Because it reads and writes the database it lives here under
``services/irene/`` — deliberately **not** under ``services/analytics/``,
whose purity guard forbids any DB session. It imports only from
``core/`` and ``services/`` (CLAUDE.md layering) and is Qt-free.

Three separable stages sit behind the delta layer (ADR-0087): the delta
layer decides *what is worth showing Irene* (here); Irene decides *how to
phrase and whether to surface* (synthesis); the deterministic floor
(:mod:`services.analytics.irene_floor`) decides *final urgency/band*. This
module is only the first stage — it emits eligible findings with a
**non-binding** urgency hint for context, never the final urgency.

Calibration reaches it as one argument
---------------------------------------
Since ADR-0116 the thresholds are per tenant, so this module takes a
:class:`~services.watch_desk.overlay.WatchDeskResolution` rather than
reading ``DEFAULT_FLOOR_CONFIG``: the effective ``FloorConfig``, the
tenant WARN default, and the per-subject overlays arrive already
resolved. The beat resolves once per run and the monitor route resolves
the same way for its live render, so the two cannot disagree about what
a subject was measured against.

Source of world state
----------------------
The internal world state is the structured
:class:`~services.limits.LimitsCoverageService` output, **not** the
prose summary emitted by ``services/tools/analysis_tools.py`` (that is
Shirley's LLM-facing tool and is unusable for a deterministic diff). We
read the long-format coverage DataFrame directly (ADR-0087 §0.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
)
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateDTO,
    IreneWatchStateRepository,
)
from core.repositories.limits_repository import LimitsRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.irene_delta import (
    KIND_FALLING_EDGE,
    KIND_MAGNITUDE_RETRIGGER,
    KIND_RISING_EDGE,
    AcknowledgedState,
    DeltaDecision,
    SubjectObservation,
    decide_delta,
    edge_band_from_status,
    mute_suppresses,
)
from services.analytics.limit_coverage import (
    FamilyCoverageResult,
    classify_coverage_status,
)
from services.watch_desk.overlay import WatchDeskResolution
from services.limits import LimitsCoverageService

_LOG = logging.getLogger(__name__)

# Coverage statuses. Constrained rows (OK/WARN/BREACH) are the only ones
# with a ceiling to breach; UNALLOCATED / NO_LIMIT carry none and are
# skipped (ADR-0087 §0.2).
_STATUS_BREACH: str = "BREACH"
_STATUS_UNALLOCATED: str = "UNALLOCATED"
_STATUS_NO_LIMIT: str = "NO_LIMIT"
_SKIP_STATUSES: frozenset[str] = frozenset({_STATUS_UNALLOCATED, _STATUS_NO_LIMIT})


@dataclass(frozen=True)
class EligibleFinding:
    """One internal change the delta layer deems worth showing Irene.

    Everything the beat needs both to render structured context for
    synthesis and to persist a finding. The numeric basis fields come
    straight from analytics (Irene interprets, never invents them —
    ADR-0013 / ADR-0087 grounding rule).

    Attributes:
        subject_key: The rule-formed subject identifier
            (``saa:{class_key}`` / ``anlv:{class_key}``).
        kind: The delta kind — ``rising_edge`` / ``falling_edge`` /
            ``magnitude_retrigger``.
        reason: The deterministic explanation from the delta decision
            (the finding basis + audit trail).
        coverage_pct: Coverage ratio at the latest Stichtag (pp).
        max_pct: The ceiling at the latest Stichtag (pp).
        headroom_eur: Remaining headroom to the ceiling (EUR); may be
            negative on a breach.
        status: The raw coverage status (``OK`` / ``WARN`` / ``BREACH``).
        band: The **edge band** derived from ``status`` (the delta's
            internal severity ordering, not the card's final band).
        current_magnitude: The observed magnitude (equals
            ``coverage_pct`` for limit subjects).
        acknowledged_magnitude: The previously acknowledged magnitude, or
            ``None`` when there was no acknowledged state.
        provisional_urgency_hint: A deterministic, **non-binding** urgency
            hint the beat may pass as context. The model suggests and the
            deterministic floor decides the final urgency; this hint is
            neither.
    """

    subject_key: str
    kind: str
    reason: str
    coverage_pct: Decimal | None
    max_pct: Decimal | None
    headroom_eur: Decimal | None
    status: str
    band: str
    current_magnitude: Decimal | None
    acknowledged_magnitude: Decimal | None
    provisional_urgency_hint: int | None


def _build_service(session: AsyncSession) -> LimitsCoverageService:
    """Compose the coverage service from the five tenant-scoped repos.

    Mirrors ``web/routes/limits.py:_build_service`` (ADR-0087 §0.2); the
    repositories are tenant-scoped via the session's RLS context.
    """
    return LimitsCoverageService(
        investments=InvestmentRepository(session),
        navs=InvestmentNavRepository(session),
        limits=LimitsRepository(session),
        asset_classes=AssetClassRepository(session),
        tenants=TenantRepository(session),
        fx_rates=FxRateRepository(session),
    )


def _acknowledged_state(
    prior: IreneWatchStateDTO | None,
    *,
    max_pct: Decimal | None,
    warn_threshold_pct: Decimal,
) -> AcknowledgedState | None:
    """Reconstruct the acknowledged state for one subject, or ``None``.

    ``acknowledged_at IS NULL`` (or no row at all) means there is no
    acknowledged non-benign state — the subject was never surfaced, or
    was reset on a prior falling edge. Otherwise the acknowledged band is
    derived by re-classifying ``acknowledged_magnitude`` against the
    *current* ceiling, so a ceiling change alone does not manufacture a
    spurious edge (see :class:`AcknowledgedState`).

    ``warn_threshold_pct`` is the **subject's** effective threshold, not a
    global one (ADR-0116 §3): the re-classification has to use the same
    threshold the live observation was classified with, or lowering a
    subject's WARN would manufacture an edge out of an unchanged figure.
    """
    if prior is None or prior.acknowledged_at is None:
        return None
    ack_mag = prior.acknowledged_magnitude
    if ack_mag is None or max_pct is None:
        # Defensive: internal subjects always carry both a magnitude and
        # a ceiling, so this branch is unreachable in practice. Falling
        # back to the benign band keeps the comparison well-defined.
        return AcknowledgedState(magnitude=ack_mag, band=edge_band_from_status("OK"))
    ack_status = classify_coverage_status(ack_mag, max_pct, warn_threshold_pct)
    return AcknowledgedState(magnitude=ack_mag, band=edge_band_from_status(ack_status))


def _urgency_hint(*, kind: str, status: str, all_clear: bool) -> int:
    """Return a deterministic, **non-binding** urgency hint for context.

    This hint is *only* context the beat may render for the model to read;
    it does **not** influence the persisted urgency. The final urgency is
    decided by the deterministic floor
    (:func:`services.analytics.irene_floor.final_urgency`) over the model's
    own ``urgency_suggestion`` — the model suggests, the floor decides, and
    this hint is neither. Kept low and coarse on purpose so it nudges rather
    than anchors.
    """
    if all_clear:
        return 0
    if kind == KIND_MAGNITUDE_RETRIGGER:
        return 3
    if status == _STATUS_BREACH:
        return 4
    return 2  # rising edge into WARN


def _make_eligible(
    *,
    decision: DeltaDecision,
    status: str,
    band: str,
    max_pct: Decimal | None,
    headroom_eur: Decimal | None,
    all_clear: bool,
) -> EligibleFinding:
    """Assemble an :class:`EligibleFinding` from a delta decision + basis."""
    return EligibleFinding(
        subject_key=decision.subject_key,
        kind=decision.kind,
        reason=decision.reason,
        coverage_pct=decision.current_magnitude,
        max_pct=max_pct,
        headroom_eur=headroom_eur,
        status=status,
        band=band,
        current_magnitude=decision.current_magnitude,
        acknowledged_magnitude=decision.acknowledged_magnitude,
        provisional_urgency_hint=_urgency_hint(
            kind=decision.kind, status=status, all_clear=all_clear
        ),
    )


async def _evaluate_family(
    *,
    family_result: FamilyCoverageResult,
    latest_ts: pd.Timestamp,
    watch: IreneWatchStateRepository,
    now: datetime,
    resolution: WatchDeskResolution,
) -> list[EligibleFinding]:
    """Evaluate every constrained class row of one family at the Stichtag.

    For each row: resolve the subject's effective WARN threshold and
    re-trigger delta from ``resolution``, re-classify the engine's status
    against that threshold, form the observation, capture the acknowledged
    state **before** the upsert overwrites ``magnitude``/``band``, upsert
    the observation, decide the delta, and on a rising edge / re-trigger
    acknowledge the new level (so it does not re-fire), or on a falling
    edge reset the acknowledgement (mandatory per ADR-0087). A muted
    subject runs the whole sequence and is filtered at the very end
    (:func:`services.analytics.irene_delta.mute_suppresses`).

    The engine classified every row against the *tenant* WARN default in
    one pass. A subject carrying an override is re-classified here rather
    than by a second engine run: only ``status`` depends on the threshold —
    ``coverage_pct``, ``max_pct`` and ``headroom_eur`` do not — so
    re-classification is exact, and it uses ``classify_coverage_status``,
    the same pure function the engine itself classifies with.
    """
    df = family_result.coverage
    if df.empty:
        return []

    prefix = family_result.family  # "saa" / "anlv"
    latest_rows = df[df["as_of_date"] == latest_ts]

    eligible: list[EligibleFinding] = []
    for row in latest_rows.itertuples(index=False):
        status = row.status
        if status in _SKIP_STATUSES:
            continue

        subject_key = f"{prefix}:{row.class_key}"
        magnitude: Decimal = row.coverage_pct
        max_pct: Decimal | None = row.max_pct
        headroom_eur: Decimal | None = row.headroom_eur

        warn_threshold_pct = resolution.warn_threshold_for(subject_key)
        if max_pct is not None:
            status = classify_coverage_status(magnitude, max_pct, warn_threshold_pct)
        band = edge_band_from_status(status)
        obs = SubjectObservation(
            subject_key=subject_key,
            magnitude=magnitude,
            status=status,
            band=band,
        )

        # Capture the acknowledged state BEFORE the upsert overwrites
        # magnitude/band — order matters (ADR-0087 §1A A2 step 4). The
        # upsert deliberately leaves acknowledged_* untouched.
        prior = await watch.get_by_subject(subject_key)
        acknowledged = _acknowledged_state(
            prior, max_pct=max_pct, warn_threshold_pct=warn_threshold_pct
        )

        await watch.upsert(
            subject_key=subject_key,
            magnitude=magnitude,
            band=band,
            last_seen_at=now,
        )

        decision = decide_delta(
            obs,
            acknowledged,
            resolution.config,
            re_trigger_delta=resolution.re_trigger_delta_for(subject_key),
        )

        if decision.kind in (KIND_RISING_EDGE, KIND_MAGNITUDE_RETRIGGER):
            # Acknowledge the newly surfaced level so it does not re-fire
            # on the next beat (edge triggering, not level triggering).
            # Written before the mute gate: a muted subject's state
            # advances exactly as an unmuted one's does.
            await watch.acknowledge(
                subject_key=subject_key,
                acknowledged_at=now,
                acknowledged_magnitude=magnitude,
            )
            all_clear = False
        elif decision.kind == KIND_FALLING_EDGE:
            # Reset acknowledgement so a later re-entry edge-triggers
            # afresh — mandatory, never optional (ADR-0087).
            await watch.reset_acknowledgement(subject_key)
            all_clear = True
        else:
            # KIND_NONE: nothing material — stay silent for this subject.
            continue

        if resolution.is_muted(subject_key) and mute_suppresses(decision, status=status):
            _LOG.info(
                "irene internal-delta: subject %r is muted — %s suppressed (watch-state advanced).",
                subject_key,
                decision.kind,
            )
            continue

        eligible.append(
            _make_eligible(
                decision=decision,
                status=status,
                band=band,
                max_pct=max_pct,
                headroom_eur=headroom_eur,
                all_clear=all_clear,
            )
        )

    return eligible


async def evaluate_internal_deltas(
    session: AsyncSession,
    *,
    now: datetime,
    resolution: WatchDeskResolution,
) -> list[EligibleFinding]:
    """Return the internal eligible findings for the active tenant.

    Reads the latest limit-coverage snapshot, diffs each constrained
    class against ``irene_watch_state.acknowledged_*``, and writes the
    resulting acknowledgements / resets. Must run on a **tenant-scoped**
    session (opened by the tick via ``tenant_context``); every read and
    write is RLS-policed for the active tenant, and the watch-state repo
    self-sources ``tenant_id`` from ``app.tenant_id``.

    Silence is the common, correct outcome:

    * An empty universe — no investment carries a NAV — ⇒ the coverage
      service returns ``None`` ⇒ ``[]`` ("nothing to monitor"). Since
      ADR-0103 §2 the coverage denominator is Σ NAV over the book itself,
      so a book with NAVs always has a baseline to diff against.
    * A range with no month-end Stichtag ⇒ ``latest_as_of_date is None``
      ⇒ ``[]``.
    * Incomplete limit / NAV configuration ⇒ the coverage engine raises;
      that is treated as "cannot assess this beat", i.e. ``[]`` (not a
      beat error), so a misconfigured tenant does not error every tick.
      The config gap surfaces through the Limits surface, not Irene.

    Args:
        session: A tenant-scoped session.
        now: The beat clock (timezone-aware UTC), stamped as
            ``last_seen_at`` / ``acknowledged_at``.
        resolution: This tenant's effective calibration, resolved **once**
            per beat by :func:`services.watch_desk.overlay.resolve_watch_desk`
            and threaded in as a plain argument. There is no default: a
            second resolution path is exactly what ADR-0116 §1 forbids, and
            a default here would be one.

    Returns:
        The eligible findings, in ``(saa, anlv)`` family order then
        DataFrame row order. Empty on any silence path.
    """
    service = _build_service(session)
    try:
        bundle = await service.get_coverage(warn_threshold_pct=resolution.warn_default_pct)
    except (
        LimitSetNotEffective,
        CoverageInputMissing,
        CoverageInputOutOfRange,
    ) as exc:
        _LOG.info(
            "irene internal-delta: coverage unavailable (%s) — silence.",
            exc,
        )
        return []

    if bundle is None or bundle.latest_as_of_date is None:
        return []

    watch = IreneWatchStateRepository(session)
    latest_ts = pd.Timestamp(bundle.latest_as_of_date)

    eligible: list[EligibleFinding] = []
    for family_result in (bundle.saa, bundle.anlv):
        eligible.extend(
            await _evaluate_family(
                family_result=family_result,
                latest_ts=latest_ts,
                watch=watch,
                now=now,
                resolution=resolution,
            )
        )
    return eligible


__all__ = ["EligibleFinding", "evaluate_internal_deltas"]
