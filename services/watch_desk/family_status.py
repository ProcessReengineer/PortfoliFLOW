# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Watch Desk monitor's family status — "What Irene watches" (ADR-0089).

Lifted out of :mod:`web.routes.watch_desk` (D-WD-14, ADR-0133 §1.2) so the
family status the Briefing renders is a service a second reader can share
rather than a projection only the route can reach.

One observation step, two projections: :func:`build_monitor` (the route's
template context) and :func:`read_family_status` (typed records for the
briefing writer's Part 2, ADR-0133 §1.2). Both classify through the same
helpers, so the briefing and the watch strip cannot state two statuses for
one subject.

A pure request-time projection: live coverage from the limits engine, joined
to the acknowledged watch-state each subject carries, plus the four signal
families observed through
:func:`~services.watch_desk.signal_observation.observe_signal_families` —
the same resolution, the same batched fetch and the same pure producers the
beat runs on (ADR-0116 §6). Nothing here is persisted and nothing is
generated — Irene's per-subject note is *assembled* from status +
watch-state + Floor Config, never written by a model. Nothing here writes
watch-state: rendering a row must never advance a subject's state machine.

**The resolution is handed in, never built here.** :func:`build_monitor`
takes it as an argument, resolved by the caller through
:func:`services.watch_desk.overlay.resolve_watch_desk` — the function the
beat resolves through (ADR-0116 §1). Which caller resolved it is not this
module's concern.

A family switched off through the watch-point scope (ADR-0133 §2.4) is not
rendered at all: no group, no footer line. The scope arrives on the
resolution like everything else.

The structural guard in ``tests/regression/test_watch_desk_single_resolution.py``
pins that this module observes through the shared path and holds none of the
fetch-only repositories.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import (
    CoverageInputMissing,
    CoverageInputOutOfRange,
    LimitSetNotEffective,
    MissingFxRateError,
)
from core.repositories.asset_class_repository import AssetClassRepository
from core.repositories.fx_rate_repository import FxRateRepository
from core.repositories.investment_nav_repository import InvestmentNavRepository
from core.repositories.investment_repository import InvestmentRepository
from core.repositories.irene_finding_repository import IreneFindingDTO
from core.repositories.irene_schedule_repository import IreneScheduleDTO
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateDTO,
    IreneWatchStateRepository,
)
from core.repositories.limits_repository import LimitsRepository
from core.repositories.tenant_repository import TenantRepository
from services.analytics.cash_coverage_watch import coverage_ratio, projected_calls_of
from services.analytics.limit_coverage import classify_coverage_status
from services.analytics.signal_watch import (
    FAMILY_FRESHNESS,
    FAMILY_FX,
    FAMILY_LIQUIDITY,
    FAMILY_PRICE,
    STATUS_OK,
    STATUS_TRIGGERED,
    STATUS_WARN,
    NoObservation,
    SignalObservation,
    SignalResult,
    signal_status_label,
)
from services.limits import LimitsCoverageBundle, LimitsCoverageService
from services.watch_desk.overlay import (
    SignalWatchpoint,
    WatchDeskResolution,
    rss_overlay_subject_key,
)
from services.watch_desk.signal_observation import (
    SIGNAL_FAMILY_ORDER,
    observe_signal_families,
)
from services.web_research.allowlist import _KNOWN_TAGS

logger = logging.getLogger(__name__)

# The two limit families the internal delta forms subject keys from — the
# beat watches exactly these. Also the order the Calibration section states
# its per-family re-trigger deltas in.
_LIMIT_FAMILIES: tuple[str, ...] = ("saa", "anlv")

# Human labels for the monitor's two internal groups, keyed by family.
_FAMILY_GROUP_NAMES: dict[str, str] = {
    "saa": "SAA limits",
    "anlv": "AnlV quotas",
}

# Constrained coverage statuses — the only ones carrying a ceiling to gauge
# against. ``NO_LIMIT`` / ``UNALLOCATED`` rows have none and are skipped
# (the same rule the internal delta applies, ADR-0087 §0.2).
_CONSTRAINED_STATUSES: tuple[str, ...] = ("OK", "WARN", "BREACH")

# Status → pill / gauge modifier suffix, matching the mock's class names.
_STATUS_MODIFIER: dict[str, str] = {
    "OK": "ok",
    "WARN": "warn",
    "BREACH": "breach",
}

# Severity order for the group's aggregate badges; only non-zero counts are
# rendered, most severe first.
_STATUS_SEVERITY: tuple[str, ...] = ("BREACH", "WARN", "OK")

# ---------------------------------------------------------------------------
# Signal families on the monitor (ADR-0116 §4, §6)
#
# Four groups beneath the quota ones, rendered from the *same* observation
# the next beat will classify: `observe_signal_families` on the resolution
# the whole render already holds. Nothing is read from `irene_watch_state`
# except the acknowledged figures the note quotes, and nothing is written.
# ---------------------------------------------------------------------------

# Group headings, by family.
_SIGNAL_GROUP_NAMES: dict[str, str] = {
    FAMILY_PRICE: "Price moves",
    FAMILY_FX: "FX moves",
    FAMILY_FRESHNESS: "NAV freshness",
    FAMILY_LIQUIDITY: "Cash coverage",
}

# What each family measures — stated once in the group header so the Value
# and Threshold cells can stay bare figures in the family's own unit. The
# alternative (a unit suffix in every cell) would put the same six words on
# thirty rows and still leave the direction unsaid.
_SIGNAL_GROUP_MEASURES: dict[str, str] = {
    FAMILY_PRICE: "adverse move over each watchpoint's window",
    FAMILY_FX: "absolute move over each watchpoint's window",
    FAMILY_FRESHNESS: "age of the newest actual NAV",
    FAMILY_LIQUIDITY: "cash cover of the calls projected inside the horizon",
}

# Signal status → pill / gauge modifier. Deliberately **not** reusing
# ``_STATUS_MODIFIER``: that map spells ``breach``, which is regulatory
# language reserved for the quota families (ADR-0116 §4). Rendering it as a
# class name would put the word into the markup of a family whose every
# visible string avoids it — and the string assertion reads the markup.
_SIGNAL_MODIFIER: dict[str, str] = {
    STATUS_OK: "calm",
    STATUS_WARN: "approaching",
    STATUS_TRIGGERED: "triggered",
}

# Badge order for a signal group: worst first, then the no-data bucket last
# — absence of data is not a severity, it is a different axis.
_SIGNAL_SEVERITY: tuple[str, ...] = (STATUS_TRIGGERED, STATUS_WARN, STATUS_OK)

# The fourth rendered state, which has no internal status behind it: a
# subject the producer refused to guess about (ADR-0116 §4). Visually
# distinct from Calm, and it draws no gauge at all — a gauge is a claim.
_NO_DATA_LABEL: str = "No data"
_NO_DATA_MODIFIER: str = "nodata"

# The families whose group header offers "+ Add watchpoint". The two
# singleton families are absent by construction: their group only renders
# when a live singleton already exists, and a second one is refused by the
# repository — so the affordance would be an offer the write path declines.
# Adding a singleton happens from the empty-state footer instead.
_ADDABLE_FROM_GROUP_HEADER: tuple[str, ...] = (FAMILY_PRICE, FAMILY_FX)

# Freshness subjects grow one-for-one with the book, so the group lists its
# exceptions openly and collapses the calm remainder behind a summary line
# (ADR-0116 §6 honesty rule: visibility on demand, never omission). The
# other three families are bounded by what the operator defined and are
# listed whole.
_EXCEPTION_FIRST_FAMILIES: tuple[str, ...] = (FAMILY_FRESHNESS,)

# ---------------------------------------------------------------------------
# Typed family status (ADR-0133 §1.2)
#
# The records the briefing writer's Part 2 material is built from. They hold
# native values — ``Decimal`` and ``date`` — and no display string other
# than the status label the monitor prints; serialising them is the
# assembler's concern, not this module's.
# ---------------------------------------------------------------------------

#: The unit a signal figure is stated in, per family.
FIGURE_UNIT_PCT: str = "pct"  # price, fx: the magnitude and threshold, in percent
FIGURE_UNIT_DAYS: str = "days"  # freshness: NAV age and maximum age, in whole days
FIGURE_UNIT_RATIO: str = "ratio"  # liquidity: coverage ratio and its floor


@dataclass(frozen=True)
class LimitSubjectStatus:
    """One constrained limit subject, classified at the Stichtag.

    Attributes:
        subject_key: ``{family}:{class_key}``.
        family: ``'saa'`` or ``'anlv'``.
        class_key: The asset-class key the subject is about.
        status: ``OK`` / ``WARN`` / ``BREACH``, after the re-classification
            against the subject's own WARN threshold.
        coverage_pct: Live coverage in percentage points.
        max_pct: The ceiling in percentage points, if any.
        utilisation_pct: Coverage as a percentage of the ceiling, or
            ``None`` without a ceiling.
        warn_threshold_pct: The subject's effective WARN threshold, as a
            percentage of the ceiling.
        muted: Whether the subject's overlay mutes it.
    """

    subject_key: str
    family: str
    class_key: str
    status: str
    coverage_pct: Decimal
    max_pct: Decimal | None
    utilisation_pct: Decimal | None
    warn_threshold_pct: Decimal
    muted: bool


@dataclass(frozen=True)
class LimitFamilyStatus:
    """One switched-on limit family and its constrained subjects.

    Attributes:
        family: ``'saa'`` or ``'anlv'``.
        subjects: The family's subjects in the coverage frame's order;
            empty when coverage is unavailable.
    """

    family: str
    subjects: tuple[LimitSubjectStatus, ...]


@dataclass(frozen=True)
class SignalFigure:
    """A signal subject's measured value against its threshold.

    Attributes:
        value: The measured figure in ``unit``; ``None`` only for
            ``liquidity`` with no calls projected.
        threshold: The trigger threshold in ``unit``; ``None`` only for
            ``liquidity`` without a calibrated floor.
        unit: One of :data:`FIGURE_UNIT_PCT`, :data:`FIGURE_UNIT_DAYS` or
            :data:`FIGURE_UNIT_RATIO`.
    """

    value: Decimal | None
    threshold: Decimal | None
    unit: str


@dataclass(frozen=True)
class SignalSubjectStatus:
    """One observed signal subject.

    Attributes:
        subject_key: The key the producer emits.
        family: ``price`` / ``fx`` / ``freshness`` / ``liquidity``.
        display_name: The watchpoint's operator-readable label.
        status: The internal ``OK`` / ``WARN`` / ``BREACH``; ``None``
            without an observation.
        status_label: Exactly what the monitor row prints in its status
            cell.
        figure: The value against the threshold; ``None`` without an
            observation.
        window_days: The observation window in days; ``None`` without an
            observation.
        as_of: The date the observation's latest value was observed;
            ``None`` without an observation.
        no_data_reason: The producer's reason for refusing to observe,
            else ``None``.
        muted: Whether the watchpoint is muted.
    """

    subject_key: str
    family: str
    display_name: str
    status: str | None
    status_label: str
    figure: SignalFigure | None
    window_days: int | None
    as_of: date | None
    no_data_reason: str | None
    muted: bool


@dataclass(frozen=True)
class PressTagStatus:
    """One curated press tag and whether it is muted.

    Attributes:
        tag: The curated RSS tag.
        subject_key: The tag's ``rss`` overlay subject key.
        muted: Whether the tag's overlay mutes it.
    """

    tag: str
    subject_key: str
    muted: bool


@dataclass(frozen=True)
class FamilyStatus:
    """The live status of every switched-on family, as typed records.

    Attributes:
        evaluated_at: The read's ``now``.
        coverage_as_of: The Stichtag the limit subjects were sliced at;
            ``None`` when coverage is unavailable.
        limits: The switched-on limit families, in the order the beat
            watches them.
        signals: The observed signal subjects, in the order the observation
            layer returned them.
        press: The curated press tags; ``None`` while Press coverage
            (``rss``) is off.
    """

    evaluated_at: datetime
    coverage_as_of: date | None
    limits: tuple[LimitFamilyStatus, ...]
    signals: tuple[SignalSubjectStatus, ...]
    press: tuple[PressTagStatus, ...] | None


# ---------------------------------------------------------------------------
# Monitor — "What Irene watches" (ADR-0089)
#
# A pure request-time projection: live coverage from the limits engine, joined
# to the acknowledged watch-state each subject carries. Nothing here is
# persisted and nothing is generated — Irene's per-subject note is *assembled*
# from status + watch-state + Floor Config, never written by a model.
# ---------------------------------------------------------------------------


def _build_coverage_service(db_session: AsyncSession) -> LimitsCoverageService:
    """Compose the coverage service from the six tenant-scoped repos.

    Identical wiring to ``web/routes/limits.py:_build_service`` and
    ``services/irene/internal_delta.py:_build_service``. The monitor
    computes coverage **live** through the same engine the Limits section
    and the beat use, so the three can never disagree about a figure.
    """
    return LimitsCoverageService(
        investments=InvestmentRepository(db_session),
        navs=InvestmentNavRepository(db_session),
        limits=LimitsRepository(db_session),
        asset_classes=AssetClassRepository(db_session),
        tenants=TenantRepository(db_session),
        fx_rates=FxRateRepository(db_session),
    )


@dataclass(frozen=True)
class _Observed:
    """What one observation step read: live coverage and the signal pairs.

    Attributes:
        bundle: The coverage bundle, or ``None`` when coverage is
            unavailable.
        latest_as_of_date: The bundle's Stichtag, or ``None``.
        coverage_available: Whether there is a bundle with a Stichtag.
        pairs: The ``(watchpoint, result)`` pairs in the order the
            observation layer returned them.
    """

    bundle: LimitsCoverageBundle | None
    latest_as_of_date: date | None
    coverage_available: bool
    pairs: tuple[tuple[SignalWatchpoint, SignalResult], ...]

    def coverage_for(self, family: str) -> pd.DataFrame | None:
        """Return one limit family's coverage frame, or ``None`` without coverage."""
        return getattr(self.bundle, family).coverage if self.coverage_available else None


async def _observe_book(
    db_session: AsyncSession,
    *,
    resolution: WatchDeskResolution,
    now: datetime,
) -> _Observed:
    """Read live coverage and observe the signal families, once per reader.

    The one place this module computes coverage and observes the signal
    families, so the monitor and the typed read classify the same
    observation. Reads only: nothing here writes watch-state.

    Coverage unavailability is never an error here: an empty universe, a
    range without a month-end Stichtag, and an engine-level refusal all
    degrade to no bundle, and the internal groups stay empty.

    Args:
        db_session: The tenant-scoped session opened by the caller.
        resolution: The tenant's effective calibration and per-subject
            overlays.
        now: The read instant. Its date is the signal families' evaluation
            date.

    Returns:
        The coverage bundle with its Stichtag, and the signal pairs.
    """
    service = _build_coverage_service(db_session)
    bundle: LimitsCoverageBundle | None
    try:
        bundle = await service.get_coverage(warn_threshold_pct=resolution.warn_default_pct)
    except (
        LimitSetNotEffective,
        CoverageInputMissing,
        CoverageInputOutOfRange,
        MissingFxRateError,
    ) as exc:
        logger.info(
            "watch-desk family status: coverage unavailable (%s) — the internal groups are empty.",
            exc,
        )
        bundle = None

    pairs = tuple(
        await observe_signal_families(db_session, as_of=now.date(), resolution=resolution)
    )

    latest_as_of_date = bundle.latest_as_of_date if bundle is not None else None
    return _Observed(
        bundle=bundle,
        latest_as_of_date=latest_as_of_date,
        coverage_available=bundle is not None and latest_as_of_date is not None,
        pairs=pairs,
    )


def _as_decimal(value: Any) -> Decimal | None:
    """Coerce a coverage-frame cell to ``Decimal``; ``None`` for a null."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _utilisation_pct(coverage_pct: Decimal, max_pct: Decimal | None) -> Decimal | None:
    """Return coverage as a percentage of the ceiling, or ``None``.

    The gauge's shared scale is 0 → ceiling, so utilisation is the only
    quantity the bar encodes. A zero ceiling permits no allocation at all:
    any positive coverage is fully utilised (and classified ``BREACH``
    upstream), zero coverage is not.
    """
    if max_pct is None:
        return None
    if max_pct == 0:
        return Decimal("100") if coverage_pct > 0 else Decimal("0")
    return coverage_pct / max_pct * Decimal("100")


def _dom_id(prefix: str, subject_key: str) -> str:
    """Return a CSS-selector-safe DOM id for one subject's row widgets.

    ``subject_key`` carries a colon (``saa:equities``), which is legal in
    an HTML id but has to be escaped in every CSS selector — and HTMX
    targets *are* CSS selectors. Slugging the key here keeps the templates
    free of escaping rules; the mapping is injective enough for one page
    because class keys are ``[a-z0-9_]``.
    """
    slug = "".join(char if char.isalnum() else "-" for char in subject_key)
    return f"{prefix}-{slug}"


def _irene_note(
    *,
    status: str,
    coverage_pct: Decimal,
    max_pct: Decimal | None,
    watch: IreneWatchStateDTO | None,
    fired: bool,
    re_trigger_delta: Decimal,
    warn_threshold_pct: Decimal,
) -> str:
    """Assemble Irene's deterministic per-subject note (ADR-0089).

    Never generated by a model and never parsed out of a finding's prose:
    the note is derived from the live coverage status, the subject's
    acknowledged watch-state, and the Floor Config re-trigger Δ.

    The **fired** note deliberately states the current status only. The
    FROM→TO edge that actually fired lives transiently in the beat's delta
    ``reason`` and is not persisted as structured data, so a "rising edge
    WARN → BREACH" claim cannot be sourced from persistence — the note
    asserts what is certain and no more (see the DC2 closure note).

    Args:
        status: The live coverage status (``OK`` / ``WARN`` / ``BREACH``).
        coverage_pct: Live coverage in percentage points.
        max_pct: The ceiling in percentage points, if any.
        watch: The subject's watch-state row, or ``None`` when the beat
            has never observed it.
        fired: Whether an open finding for this subject was created in
            the current beat.
        re_trigger_delta: This subject's magnitude re-trigger threshold in
            percentage points — its overlay override when it carries one,
            else the family value from the tenant's effective Floor Config.
        warn_threshold_pct: This subject's effective WARN threshold, as a
            percentage of the ceiling. Per-subject since ADR-0116 §3, so
            the note states the threshold the subject was actually
            measured against rather than a house default.

    Returns:
        The rendered note string.
    """
    if fired:
        return f"Fired this beat — now at {status}."

    if status == "OK":
        return f"Calm — below the {float(warn_threshold_pct):.0f}% WARN threshold."

    # Non-benign and did not fire. If the subject carries an acknowledged
    # magnitude close enough to the live one, the silence is *explained* —
    # the delta layer suppressed it below the re-trigger Δ.
    ack_magnitude = watch.acknowledged_magnitude if watch is not None else None
    ack_at = watch.acknowledged_at if watch is not None else None
    if ack_magnitude is not None and ack_at is not None:
        move = coverage_pct - ack_magnitude
        if abs(move) < re_trigger_delta:
            # The acknowledged band is re-derived by re-classifying the
            # acknowledged magnitude against the *current* ceiling — the
            # same rule the delta layer applies, so the note agrees with
            # the mechanism that produced the silence.
            ack_status = (
                classify_coverage_status(ack_magnitude, max_pct, warn_threshold_pct)
                if max_pct is not None
                else status
            )
            return (
                f"Silent — {ack_status} acknowledged at "
                f"{float(ack_magnitude):.2f}% on "
                f"{ack_at.strftime('%d %b %Y')}; {float(move):+.2f} pp is "
                f"below the {float(re_trigger_delta):.1f} pp re-trigger."
            )

    # Non-benign, no acknowledgement, no firing this beat — e.g. the first
    # render before any beat has run. Never invent an acknowledgement.
    return f"{status} — not yet reviewed."


def _limit_subjects(
    *,
    family: str,
    coverage: pd.DataFrame | None,
    latest_as_of_date: Any,
    resolution: WatchDeskResolution,
) -> tuple[LimitSubjectStatus, ...]:
    """Classify one family's constrained coverage rows at the Stichtag.

    Only constrained rows (``OK`` / ``WARN`` / ``BREACH``) are kept:
    ``NO_LIMIT`` and ``UNALLOCATED`` carry no ceiling, so there is nothing
    to gauge utilisation against.

    The engine classified the frame against the tenant WARN default in one
    pass; a subject carrying an overlay override is re-classified here with
    ``classify_coverage_status`` — the same pure function, and the same
    correction the beat applies, so monitor, typed read and beat agree on
    the status by construction rather than by coincidence.

    Args:
        family: ``'saa'`` or ``'anlv'``.
        coverage: The family's long-format coverage frame, or ``None``
            when coverage is unavailable (empty universe / no Stichtag).
        latest_as_of_date: The Stichtag (a ``date``) to slice, or ``None``.
        resolution: The tenant's effective calibration and per-subject
            overlays.

    Returns:
        The family's subjects in the frame's record order.
    """
    if coverage is None or latest_as_of_date is None:
        return ()

    # The engine stores as_of_date as datetime64, so the Stichtag must be
    # compared as a Timestamp — a bare date silently matches nothing (same
    # conversion as web/routes/limits.py and internal_delta.py).
    slice_ = coverage.loc[coverage["as_of_date"] == pd.Timestamp(latest_as_of_date)]
    subjects: list[LimitSubjectStatus] = []
    for record in slice_.to_dict("records"):
        status = str(record["status"])
        if status not in _CONSTRAINED_STATUSES:
            continue
        coverage_pct = _as_decimal(record["coverage_pct"])
        if coverage_pct is None:
            continue
        max_pct = _as_decimal(record["max_pct"])
        subject_key = f"{family}:{record['class_key']}"

        warn_threshold_pct = resolution.warn_threshold_for(subject_key)
        if max_pct is not None:
            status = classify_coverage_status(coverage_pct, max_pct, warn_threshold_pct)
        overlay = resolution.overlay_for(subject_key)
        subjects.append(
            LimitSubjectStatus(
                subject_key=subject_key,
                family=family,
                class_key=str(record["class_key"]),
                status=status,
                coverage_pct=coverage_pct,
                max_pct=max_pct,
                utilisation_pct=_utilisation_pct(coverage_pct, max_pct),
                warn_threshold_pct=warn_threshold_pct,
                muted=overlay is not None and overlay.muted,
            )
        )
    return tuple(subjects)


def _build_family_group(
    *,
    family: str,
    coverage: pd.DataFrame | None,
    latest_as_of_date: Any,
    watch_by_subject: dict[str, IreneWatchStateDTO],
    fired: dict[str, str],
    effective_label: str | None,
    resolution: WatchDeskResolution,
) -> dict[str, Any]:
    """Project one family's constrained coverage rows into a monitor group.

    The subjects and their statuses come from :func:`_limit_subjects` — the
    classification the typed read shares; this function adds only what the
    template needs on top of them.

    Args:
        family: ``'saa'`` or ``'anlv'``.
        coverage: The family's long-format coverage frame, or ``None``
            when coverage is unavailable (empty universe / no Stichtag).
        latest_as_of_date: The Stichtag (a ``date``) to slice, or ``None``.
        watch_by_subject: Watch-state rows indexed by ``subject_key``.
        fired: ``subject_key`` → finding id for subjects that fired in
            the current beat.
        effective_label: The effective limit set's label, or ``None``.
        resolution: The tenant's effective calibration and per-subject
            overlays — the same object the beat runs on.

    Returns:
        The group's template context.
    """
    rows: list[dict[str, Any]] = []

    for subject in _limit_subjects(
        family=family,
        coverage=coverage,
        latest_as_of_date=latest_as_of_date,
        resolution=resolution,
    ):
        subject_key = subject.subject_key
        overlay = resolution.overlay_for(subject_key)
        finding_id = fired.get(subject_key)
        utilisation = subject.utilisation_pct
        rows.append(
            {
                "subject_key": subject_key,
                "family": family,
                "status": subject.status,
                "modifier": _STATUS_MODIFIER[subject.status],
                "coverage_pct": float(subject.coverage_pct),
                "max_pct": float(subject.max_pct) if subject.max_pct is not None else None,
                "utilisation": (float(utilisation) if utilisation is not None else None),
                # Rendered width only — clamped so a breach cannot
                # overflow the bar. The figures above stay honest.
                "fill_pct": (min(float(utilisation), 100.0) if utilisation is not None else 0.0),
                # The mark's position on the shared 0 → ceiling scale.
                # Per-subject *positioned* since ADR-0116 §6, never
                # per-subject rescaled: the axis is still the ceiling on
                # every row, and the mark still sits at the WARN fraction
                # of it — that fraction is now the subject's own.
                "warn_threshold_pct": float(subject.warn_threshold_pct),
                "muted": subject.muted,
                # A BREACH cannot be muted (ADR-0116 §3). Disabling the
                # toggle here is the *mirror* of the beat-side rule, for
                # legibility; the beat enforces it whatever this says.
                "mute_locked": subject.status == "BREACH",
                "warn_customised": (overlay is not None and overlay.warn_threshold_pct is not None),
                "delta_customised": (overlay is not None and overlay.re_trigger_delta is not None),
                "editor_id": _dom_id("dc-overlay", subject_key),
                "fired_this_beat": finding_id is not None,
                "finding_anchor": (f"#dc-card-{finding_id}" if finding_id is not None else None),
                "note": _irene_note(
                    status=subject.status,
                    coverage_pct=subject.coverage_pct,
                    max_pct=subject.max_pct,
                    watch=watch_by_subject.get(subject_key),
                    fired=finding_id is not None,
                    re_trigger_delta=resolution.re_trigger_delta_for(subject_key),
                    warn_threshold_pct=subject.warn_threshold_pct,
                ),
            }
        )

    status_counts = Counter(row["status"] for row in rows)
    muted_count = sum(1 for row in rows if row["muted"])
    return {
        "family": family,
        "name": _FAMILY_GROUP_NAMES[family],
        "badges": [
            {
                "text": f"{status_counts[status]} {status}",
                "modifier": _STATUS_MODIFIER[status],
            }
            for status in _STATUS_SEVERITY
            if status_counts.get(status)
        ],
        "subject_count": len(rows),
        # Counted in the header so a muted subject is visible as a choice
        # someone made, never as an absence (ADR-0116 §3).
        "muted_count": muted_count,
        "effective_label": effective_label,
        "rows": rows,
    }


def _press_tags(resolution: WatchDeskResolution) -> tuple[PressTagStatus, ...]:
    """Return every curated press tag with its overlay subject and mute.

    Args:
        resolution: The tenant's effective calibration, for the ``rss``
            overlays.

    Returns:
        One record per curated tag, in sorted tag order.
    """
    press: list[PressTagStatus] = []
    for tag in sorted(_KNOWN_TAGS):
        subject_key = rss_overlay_subject_key(tag)
        press.append(
            PressTagStatus(tag=tag, subject_key=subject_key, muted=resolution.is_muted(subject_key))
        )
    return tuple(press)


def _build_rss_group(
    fired_tags: dict[str, str], *, resolution: WatchDeskResolution
) -> dict[str, Any]:
    """Project the curated RSS tags into the monitor's third group.

    Three columns only — tag, what it corroborates, Irene's note. The
    mock's "Clusters today" column is deliberately **not** rendered (D8):
    a per-tag cluster count for tags that produced no finding is not
    recoverable from persisted data, and a silent-cluster claim would be
    an invention. A tag with no corroborating finding this beat carries
    no note, which is the honest v1 output.

    Args:
        fired_tags: RSS ``tag`` → finding id for findings created in the
            current beat that carry that tag's enrichment.
        resolution: The tenant's effective calibration, for the ``rss``
            overlays. An ``rss`` overlay carries **mute alone** — the
            schema forbids the rest, because a cluster subject is
            non-scalar and has no threshold to move.

    Returns:
        The RSS group's template context.
    """
    tag_map = resolution.config.tag_asset_class_map
    rows: list[dict[str, Any]] = []
    for press_tag in _press_tags(resolution):
        finding_id = fired_tags.get(press_tag.tag)
        rows.append(
            {
                "tag": press_tag.tag,
                "subject_key": press_tag.subject_key,
                "family": "rss",
                "corroborates": list(tag_map.get(press_tag.tag, ())),
                "muted": press_tag.muted,
                # No breach exception: a press cluster has no ceiling to
                # violate, so a muted dimension is simply silent.
                "mute_locked": False,
                "editor_id": _dom_id("dc-overlay", press_tag.subject_key),
                "fired_this_beat": finding_id is not None,
                "finding_anchor": (f"#dc-card-{finding_id}" if finding_id is not None else None),
                "note": (
                    "Folded into an open finding as corroboration."
                    if finding_id is not None
                    else ""
                ),
            }
        )
    return {
        "name": "Press dimensions (RSS)",
        "meta": (
            f"{len(_KNOWN_TAGS)} curated tags · corroboration only, never a source of figures"
        ),
        "muted_count": sum(1 for row in rows if row["muted"]),
        "rows": rows,
    }


def _plural(count: int, unit: str) -> str:
    """Render ``count`` of ``unit``, pluralising the ordinary way."""
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _signal_figure(watchpoint: SignalWatchpoint, result: SignalObservation) -> SignalFigure:
    """Return one observation's value and threshold in the family's own unit.

    Each family speaks the unit its operator calibrated (ADR-0116 §6):
    percent for a move, whole days for an age, and **ratios** for coverage.
    The 100-scale ``liquidity`` computes on is arithmetic, not communication
    (see :mod:`services.analytics.cash_coverage_watch`), and never reaches a
    figure — the ratio is recovered from the observation's own window pair
    through the two functions the producer exports for it, so the figure on
    the row, the figure in a finding's note and the figure in the typed read
    come from one derivation.

    Args:
        watchpoint: The subject's effective watchpoint.
        result: The observation to state.

    Returns:
        The figure, with its unit.
    """
    if watchpoint.family == FAMILY_FRESHNESS:
        return SignalFigure(
            value=result.magnitude, threshold=result.threshold_pct, unit=FIGURE_UNIT_DAYS
        )

    if watchpoint.family == FAMILY_LIQUIDITY:
        return SignalFigure(
            value=coverage_ratio(
                liquid_balance=result.reference_value, calls=projected_calls_of(result)
            ),
            threshold=watchpoint.min_coverage_ratio,
            unit=FIGURE_UNIT_RATIO,
        )

    return SignalFigure(
        value=result.magnitude, threshold=result.threshold_pct, unit=FIGURE_UNIT_PCT
    )


def _signal_figures(watchpoint: SignalWatchpoint, result: SignalObservation) -> tuple[str, str]:
    """Return one row's ``(value, threshold)`` in the family's own language.

    Formats the figure :func:`_signal_figure` derives: percentage points for
    a move, whole days for an age, and ratios for coverage.

    Args:
        watchpoint: The row's effective watchpoint.
        result: The observation to render.

    Returns:
        The value cell and the threshold cell, as display strings.
    """
    figure = _signal_figure(watchpoint, result)

    if watchpoint.family == FAMILY_LIQUIDITY:
        # `None` means "nothing to cover", which is a sentence rather than a
        # number: printing 0.00× there would state a shortfall the book does
        # not have, and printing ∞ would state a cover it was never asked for.
        return (
            "no calls projected" if figure.value is None else f"{float(figure.value):.2f}×",
            "—" if figure.threshold is None else f"{float(figure.threshold):.2f}×",
        )

    # Unreachable for the three other families, whose figures are always
    # set; stated so the figure narrows without an assertion.
    if figure.value is None or figure.threshold is None:
        return "—", "—"

    if watchpoint.family == FAMILY_FRESHNESS:
        return _plural(int(figure.value), "day"), _plural(int(figure.threshold), "day")

    return f"{float(figure.value):.2f}%", f"{float(figure.threshold):.2f}%"


def _status_label(result: SignalResult) -> str:
    """Return the status cell a signal row prints for ``result``."""
    if isinstance(result, SignalObservation):
        return signal_status_label(result.status)
    return _NO_DATA_LABEL


def _signal_status(watchpoint: SignalWatchpoint, result: SignalResult) -> SignalSubjectStatus:
    """Project one observed signal subject into its typed record.

    Args:
        watchpoint: The subject's effective watchpoint.
        result: The live observation, or the producer's refusal to make one.

    Returns:
        The subject's record; without an observation it carries the
        producer's reason and no status, figure, window or date.
    """
    if isinstance(result, SignalObservation):
        return SignalSubjectStatus(
            subject_key=watchpoint.subject_key,
            family=watchpoint.family,
            display_name=watchpoint.display_name,
            status=result.status,
            status_label=_status_label(result),
            figure=_signal_figure(watchpoint, result),
            window_days=result.window_days,
            as_of=result.latest_date,
            no_data_reason=None,
            muted=watchpoint.muted,
        )
    return SignalSubjectStatus(
        subject_key=watchpoint.subject_key,
        family=watchpoint.family,
        display_name=watchpoint.display_name,
        status=None,
        status_label=_status_label(result),
        figure=None,
        window_days=None,
        as_of=None,
        no_data_reason=result.reason,
        muted=watchpoint.muted,
    )


def _signal_note(
    *,
    watchpoint: SignalWatchpoint,
    result: SignalResult,
    watch: IreneWatchStateDTO | None,
    fired: bool,
    re_trigger_delta: Decimal,
    warn_threshold_pct: Decimal,
) -> str:
    """Assemble one signal row's deterministic note.

    The signal-family sibling of :func:`_irene_note`, on the same four
    branches and with the same rule: route-assembled from the live
    observation, the subject's acknowledged watch-state and the effective
    calibration, never generated and never parsed out of a finding. The
    template renders the string and composes nothing.

    Two departures from the quota note, both deliberate:

    * every branch speaks Calm / Approaching / Triggered — "breach" is
      regulatory language reserved for the quota families (ADR-0116 §4);
    * the *silent* branch states that the subject was acknowledged and when,
      but **not** the acknowledged figure. Three of the four families could
      restate it natively; ``liquidity`` stores it on the internal 100-scale
      and would have to invert a clamped magnitude to reach a ratio. One
      sentence that is true for four families beats four sentences of which
      one is reconstructed.

    Args:
        watchpoint: The row's effective watchpoint.
        result: The live observation, or the producer's refusal to make one.
        watch: The subject's watch-state row, or ``None`` when the beat has
            never observed it.
        fired: Whether an open finding for this subject was created in the
            current beat.
        re_trigger_delta: The subject's effective magnitude re-trigger
            threshold, in the family's own unit.
        warn_threshold_pct: The subject's effective WARN fraction, as a
            percentage of the trigger threshold.

    Returns:
        The rendered note string.
    """
    if isinstance(result, NoObservation):
        # The producer's own words. It is the half that knows *why* nothing
        # could be measured, and a second phrasing here would eventually
        # disagree with the one the beat logged.
        return f"No data — {result.reason}."

    label = signal_status_label(result.status)
    if fired:
        return f"Fired this beat — now {label}."

    if result.status == STATUS_OK:
        return f"Calm — below the {float(warn_threshold_pct):.0f}% Approaching mark."

    # Non-calm and did not fire. If the subject carries an acknowledged
    # magnitude close enough to the live one, the silence is *explained* —
    # the delta layer suppressed it below the re-trigger Δ.
    ack_magnitude = watch.acknowledged_magnitude if watch is not None else None
    ack_at = watch.acknowledged_at if watch is not None else None
    if (
        ack_magnitude is not None
        and ack_at is not None
        and abs(result.magnitude - ack_magnitude) < re_trigger_delta
    ):
        return (
            f"Silent — acknowledged on {ack_at.strftime('%d %b %Y')}; it has "
            "not moved past the re-trigger since."
        )

    # Non-calm, no acknowledgement, no firing this beat — e.g. the first
    # render before any beat has run. Never invent an acknowledgement.
    return f"{label} — not yet reviewed."


def _signal_row(
    *,
    watchpoint: SignalWatchpoint,
    result: SignalResult,
    watch_by_subject: dict[str, IreneWatchStateDTO],
    fired: dict[str, str],
    resolution: WatchDeskResolution,
) -> dict[str, Any]:
    """Project one observed signal subject into a monitor row.

    The gauge obeys the generalised honesty rules verbatim (ADR-0116 §6):
    the scale runs 0 → the subject's **trigger threshold** on every row of
    every family, the mark sits at that subject's effective warn fraction of
    it, and a crossing clamps the rendered fill while the printed Value and
    Threshold stay honest. A no-data row draws no gauge at all — a bar at
    zero would claim calm, which is the one thing the producer refused to
    say.
    """
    subject_key = watchpoint.subject_key
    finding_id = fired.get(subject_key)
    warn_threshold_pct = resolution.warn_threshold_for(subject_key)
    fill_pct = 0.0
    calm = False

    if isinstance(result, SignalObservation):
        value, threshold = _signal_figures(watchpoint, result)
        modifier = _SIGNAL_MODIFIER[result.status]
        calm = result.status == STATUS_OK
        # Percent of the way to the trigger threshold — the one quantity the
        # bar encodes, on the same 0 → threshold scale for every row, and
        # clamped so a crossing cannot overflow it. It is deliberately the
        # bar's *geometry* only and is never printed: for ``liquidity`` the
        # proportion and the internal 100-scale magnitude are the same
        # number, and that number is not one the operator ever set.
        fill_pct = min(float(result.magnitude / result.threshold_pct * Decimal("100")), 100.0)
        observed = True
    else:
        value = threshold = "—"
        modifier = _NO_DATA_MODIFIER
        observed = False

    return {
        "subject_key": subject_key,
        "family": watchpoint.family,
        "watchpoint_id": str(watchpoint.watchpoint_id),
        "display_name": watchpoint.display_name,
        "status": _status_label(result),
        "modifier": modifier,
        "has_data": observed,
        "calm": calm,
        "value": value,
        "threshold": threshold,
        "fill_pct": fill_pct,
        # Per-subject *positioned*, never per-subject rescaled.
        "warn_threshold_pct": float(warn_threshold_pct),
        "muted": watchpoint.muted,
        # No lock here: the un-mutable-breach rule is quota-only (ADR-0116
        # §3 as scoped in P4). A watchpoint the operator set themselves may
        # be silenced at any status — no regulatory floor stands behind it.
        "mute_locked": False,
        "warn_customised": watchpoint.warn_threshold_pct is not None,
        "delta_customised": watchpoint.re_trigger_delta is not None,
        "editor_id": _dom_id("dc-watchpoint", subject_key),
        "fired_this_beat": finding_id is not None,
        "finding_anchor": (f"#dc-card-{finding_id}" if finding_id is not None else None),
        "note": _signal_note(
            watchpoint=watchpoint,
            result=result,
            watch=watch_by_subject.get(subject_key),
            fired=finding_id is not None,
            re_trigger_delta=resolution.re_trigger_delta_for(subject_key),
            warn_threshold_pct=warn_threshold_pct,
        ),
    }


def _build_signal_group(
    family: str,
    observations: list[tuple[SignalWatchpoint, SignalResult]],
    *,
    watch_by_subject: dict[str, IreneWatchStateDTO],
    fired: dict[str, str],
    resolution: WatchDeskResolution,
) -> dict[str, Any]:
    """Project one signal family's observations into a monitor group.

    Args:
        family: One of the four defined families.
        observations: The family's ``(watchpoint, result)`` pairs, in the
            order :func:`~services.watch_desk.signal_observation.observe_signal_families`
            returned them — which is the order the beat evaluated them in.
        watch_by_subject: Watch-state rows indexed by ``subject_key``.
        fired: ``subject_key`` → finding id for subjects that fired in the
            current beat.
        resolution: The tenant's effective calibration — the same object the
            beat runs on.

    Returns:
        The group's template context. For an exception-first family the
        calm rows are moved to ``collapsed_rows`` behind a summary line;
        they are **collapsed, never dropped**, and both halves are counted
        in the header.
    """
    rows = [
        _signal_row(
            watchpoint=watchpoint,
            result=result,
            watch_by_subject=watch_by_subject,
            fired=fired,
            resolution=resolution,
        )
        for watchpoint, result in observations
    ]

    if family in _EXCEPTION_FIRST_FAMILIES:
        listed = [row for row in rows if not row["calm"]]
        collapsed = [row for row in rows if row["calm"]]
    else:
        listed, collapsed = rows, []

    status_counts = Counter(row["status"] for row in rows)
    badges: list[dict[str, str]] = []
    for signal_status in _SIGNAL_SEVERITY:
        label = signal_status_label(signal_status)
        if status_counts.get(label):
            badges.append(
                {
                    "text": f"{status_counts[label]} {label}",
                    "modifier": _SIGNAL_MODIFIER[signal_status],
                }
            )
    if status_counts.get(_NO_DATA_LABEL):
        badges.append(
            {
                "text": f"{status_counts[_NO_DATA_LABEL]} {_NO_DATA_LABEL.lower()}",
                "modifier": _NO_DATA_MODIFIER,
            }
        )

    return {
        "family": family,
        "name": _SIGNAL_GROUP_NAMES[family],
        "measure": _SIGNAL_GROUP_MEASURES[family],
        "badges": badges,
        "subject_count": len(rows),
        "muted_count": sum(1 for row in rows if row["muted"]),
        "rows": listed,
        "collapsed_rows": collapsed,
        "collapsed_summary": (f"{len(collapsed)} fresh — show all" if collapsed else None),
        "can_add": family in _ADDABLE_FROM_GROUP_HEADER,
    }


def _signal_footer(rendered: set[str], *, resolution: WatchDeskResolution) -> list[dict[str, Any]]:
    """Describe the families that rendered no group, and what may be done.

    An empty group is noise, so a family nobody watches is skipped entirely
    (ADR-0116 §6) — but skipping it silently would hide the capability, so
    the section footer carries one compact line per absent family. The
    house empty-state idiom: prose in ``.pf-empty-state``, with the action
    beside it (see ``_cases_body.html``).

    A family switched off through the watch-point scope (ADR-0133 §2.4) is
    skipped as well: it is not watched, so it is neither a group nor an
    "Add watchpoint" invitation.

    The two singleton families are offered an "Add" only when **no live
    identity** exists. ``signals_for`` answers about the *effective* set, so
    a version dated in the future would read as absent here while the
    repository still refuses a second identity — which is exactly why the
    create path renders that refusal inline rather than trusting this.

    Args:
        rendered: The families that produced a group this render.
        resolution: The tenant's effective calibration.

    Returns:
        One entry per absent, switched-on family, in the shared family order.
    """
    footer: list[dict[str, Any]] = []
    for family in SIGNAL_FAMILY_ORDER:
        if family in rendered or not resolution.is_family_enabled(family):
            continue
        watched = bool(resolution.signals_for(family))
        footer.append(
            {
                "family": family,
                "name": _SIGNAL_GROUP_NAMES[family],
                "measure": _SIGNAL_GROUP_MEASURES[family],
                # A watched singleton with nothing to show is not an
                # invitation to add a second one — say what is true instead.
                "can_add": not watched,
                "watched": watched,
            }
        )
    return footer


def _fired_this_beat(
    findings: list[IreneFindingDTO], last_beat_at: datetime | None
) -> tuple[dict[str, str], dict[str, str]]:
    """Split the open findings created in the current beat by subject and tag.

    Without a completed beat there is no "since" to test against, so the
    fired sets are empty rather than guessed.

    Args:
        findings: The open findings already loaded for the feed.
        last_beat_at: The schedule's last beat instant, or ``None``.

    Returns:
        A ``(by_subject, by_tag)`` pair, each mapping to the finding id
        the monitor anchors to. ``findings`` arrives in urgency-then-
        recency order, so the first hit per key is the one to link.
    """
    by_subject: dict[str, str] = {}
    by_tag: dict[str, str] = {}
    if last_beat_at is None:
        return by_subject, by_tag
    for finding in findings:
        if finding.created_at < last_beat_at:
            continue
        by_subject.setdefault(finding.subject_key, str(finding.id))
        tag = (finding.payload or {}).get("tag")
        if isinstance(tag, str) and tag in _KNOWN_TAGS:
            by_tag.setdefault(tag, str(finding.id))
    return by_subject, by_tag


async def build_monitor(
    db_session: AsyncSession,
    *,
    findings: list[IreneFindingDTO],
    schedule: IreneScheduleDTO | None,
    effective_labels: dict[str, str],
    resolution: WatchDeskResolution,
    now: datetime,
) -> dict[str, Any]:
    """Build the "What Irene watches" monitor context (ADR-0089).

    Runs inside the caller's ``tenant_context``: one coverage computation,
    one batched fetch per watched signal family, one bulk watch-state read,
    and the findings the feed already loaded — no per-subject lookup loop.
    Coverage is computed **now** (D4); the beat's timestamp feeds only the
    acknowledged-magnitude / fired-this-beat arithmetic — the monitor head
    carries no observation-time claim.

    The signal groups are derived the same way and from the same code the
    beat runs (ADR-0116 §6): one resolution, then
    :func:`~services.watch_desk.signal_observation.observe_signal_families`,
    which is read-only — rendering a row must never advance a subject's
    state machine. Nothing here reads ``irene_watch_state`` for a *status*;
    the acknowledged figures it does read serve the note alone.

    Coverage unavailability is never an error here: an empty universe, a
    range without a month-end Stichtag, and an engine-level refusal all
    degrade to the same empty internal groups. The Briefing is the primary
    surface and must render.

    Args:
        db_session: The tenant-scoped session opened by the caller.
        findings: The open findings already loaded for the feed.
        schedule: The tenant's schedule row, or ``None``.
        effective_labels: ``family`` → effective limit-set label, for the
            families that have one.
        resolution: The tenant's effective calibration and per-subject
            overlays, resolved once by the caller through
            :func:`services.watch_desk.overlay.resolve_watch_desk` — the
            same function the beat resolves through (ADR-0116 §1).
        now: The render instant. Its date is the signal families' evaluation
            date, which is why a row and the next beat's classification of
            the same subject agree by construction.

    Returns:
        The ``monitor`` template context. A switched-off family has no group;
        ``rss`` is ``None`` while Press coverage is off.
    """
    observed = await _observe_book(db_session, resolution=resolution, now=now)

    watch_by_subject = {
        row.subject_key: row for row in await IreneWatchStateRepository(db_session).list_all()
    }
    fired_by_subject, fired_by_tag = _fired_this_beat(
        findings, schedule.last_beat_at if schedule is not None else None
    )

    groups = [
        _build_family_group(
            family=family,
            coverage=observed.coverage_for(family),
            latest_as_of_date=observed.latest_as_of_date,
            watch_by_subject=watch_by_subject,
            fired=fired_by_subject,
            effective_label=effective_labels.get(family),
            resolution=resolution,
        )
        for family in _LIMIT_FAMILIES
        if resolution.is_family_enabled(family)
    ]

    # Grouped in the order the observation layer returned them, which is the
    # order the beat evaluated them in — so the two surfaces never present
    # the same subjects in two orders, and there is no second family list
    # here to drift from that one.
    observations: dict[str, list[tuple[SignalWatchpoint, SignalResult]]] = {}
    for watchpoint, result in observed.pairs:
        observations.setdefault(watchpoint.family, []).append((watchpoint, result))

    signal_groups = [
        _build_signal_group(
            family,
            family_observations,
            watch_by_subject=watch_by_subject,
            fired=fired_by_subject,
            resolution=resolution,
        )
        for family, family_observations in observations.items()
    ]

    return {
        "coverage_available": observed.coverage_available,
        "groups": groups,
        "rss": (
            _build_rss_group(fired_by_tag, resolution=resolution)
            if resolution.is_family_enabled("rss")
            else None
        ),
        "signal_groups": signal_groups,
        "signal_footer": _signal_footer(set(observations), resolution=resolution),
    }


async def read_family_status(
    db_session: AsyncSession,
    *,
    resolution: WatchDeskResolution,
    now: datetime,
) -> FamilyStatus:
    """Read the live status of every switched-on family as typed records.

    The briefing writer's Part 2 material (ADR-0133 §1.2): the same
    observation step and the same classifying helpers the monitor renders
    from, so the briefing cannot state a status the watch strip does not
    show. No production caller yet; the briefing-material assembler is the
    first.

    It takes no findings, schedule or labels and reads no watch-state: notes,
    fired anchors, DOM ids, badges, fills and labels stay the monitor's
    projection. A switched-off family is absent, exactly as on the monitor.

    Args:
        db_session: The tenant-scoped session opened by the caller.
        resolution: The tenant's effective calibration and per-subject
            overlays, resolved once by the caller through
            :func:`services.watch_desk.overlay.resolve_watch_desk`.
        now: The read instant. Its date is the signal families' evaluation
            date.

    Returns:
        The family status. A switched-on limit family without coverage keeps
        an empty ``subjects`` tuple; ``press`` is ``None`` while Press
        coverage is off.
    """
    observed = await _observe_book(db_session, resolution=resolution, now=now)

    limits = tuple(
        LimitFamilyStatus(
            family=family,
            subjects=_limit_subjects(
                family=family,
                coverage=observed.coverage_for(family),
                latest_as_of_date=observed.latest_as_of_date,
                resolution=resolution,
            ),
        )
        for family in _LIMIT_FAMILIES
        if resolution.is_family_enabled(family)
    )

    return FamilyStatus(
        evaluated_at=now,
        coverage_as_of=observed.latest_as_of_date if observed.coverage_available else None,
        limits=limits,
        signals=tuple(_signal_status(watchpoint, result) for watchpoint, result in observed.pairs),
        press=_press_tags(resolution) if resolution.is_family_enabled("rss") else None,
    )


__all__ = [
    "FIGURE_UNIT_DAYS",
    "FIGURE_UNIT_PCT",
    "FIGURE_UNIT_RATIO",
    "FamilyStatus",
    "LimitFamilyStatus",
    "LimitSubjectStatus",
    "PressTagStatus",
    "SignalFigure",
    "SignalSubjectStatus",
    "build_monitor",
    "read_family_status",
]
