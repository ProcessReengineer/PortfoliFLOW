# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The one per-tenant Watch Desk resolution the beat and the monitor share.

ADR-0116 §1 makes ``effective_watchpoints`` "*the* read — the one the beat
and the web surface share, so 'what was effective when this finding fired'
is the same query in both places". This module is that promise made
structural: **one** function resolves, per tenant and per instant,

1. the effective :class:`~services.analytics.irene_floor.FloorConfig`
   (defaults ⊕ the tenant's latest ``floor_calibration`` revision, via
   :func:`services.watch_desk.calibration.effective_floor_config`);
2. the tenant-wide WARN default (via
   :func:`services.watch_desk.calibration.effective_warn_threshold_pct` —
   the WARN threshold is a call parameter of the coverage engine, never a
   ``FloorConfig`` field);
3. the per-subject **overlay** map for the derived families (``saa`` /
   ``anlv`` / ``rss``), keyed by ``subject_key``;
4. the effective **signal watchpoints** for the four defined families,
   also keyed by ``subject_key``.

Three call sites consume it — ``services.irene.internal_delta`` and
``services.irene.signal_delta`` on the beat side, ``web.routes.watch_desk``
on the monitor side — and none of them resolves anything of its own. That
is the point: a monitor row and the beat's edge classification for the same
subject cannot disagree about the threshold they were measured against,
because there is nowhere for a second answer to come from.

Overlays define nothing; signal watchpoints define everything
-------------------------------------------------------------
The asymmetry of ADR-0116 §3 survives into the resolution as two maps
rather than one. An **overlay** carries sensitivity only — the subject and
its ceiling belong to the limit set — while a **signal watchpoint** is the
subject: it carries the instrument or the pair, the trigger threshold and
the window, without which nothing would be observed at all. Collapsing
them into one map would need a type that is mostly ``None`` for whichever
half it is not, which is precisely the params-JSONB shape the ADR rejected
for the table.

All four defined families resolve here since P5. The promise this module's
own docstring made — "when those producers land they extend this same
resolution rather than growing a second one" — was kept by appending to
:data:`_RESOLVED_SIGNAL_FAMILIES` and describing two more shapes in
:data:`_SHAPE_BY_FAMILY`; nothing downstream of those two constants
learned a family name.

One subject, or many, from one row
----------------------------------
``freshness`` and ``liquidity`` are **singletons**: at most one identity
per tenant, enforced on write. ``liquidity`` is also a single subject, so
its watchpoint key *is* its subject key. ``freshness`` is not: its one row
states a rule for the whole book and the beat enumerates
``freshness:{investment_id}`` per active investment, exactly as the quota
families enumerate subjects from their limit sets. Those enumerated
subjects carry no registry row, so :meth:`WatchDeskResolution.is_muted`,
:meth:`~WatchDeskResolution.warn_threshold_for` and
:meth:`~WatchDeskResolution.re_trigger_delta_for` fall back to the
singleton's own key. Muting the freshness watchpoint therefore silences
the whole family, which is what muting a rule that applies to everything
must mean; per-investment overrides are a commissioned successor
(ADR-0116 §Commissions), not a half-built one here.

Layering
--------
Impure by construction — it reads two tenant-scoped tables — so it lives
under ``services/watch_desk/`` beside the calibration write path and not
under ``services/analytics/``. The values it returns are handed to the
pure layers as plain arguments (ADR-0116 §5), which is what keeps the
analytics purity guard green.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.models.watchpoint import OVERLAY_FAMILIES
from core.repositories.floor_calibration_repository import FloorCalibrationRepository
from core.repositories.watchpoint_repository import WatchpointDTO, WatchpointRepository
from services.analytics.cash_coverage_watch import COVERAGE_THRESHOLD_PCT
from services.analytics.irene_delta import subject_type_from_key
from services.analytics.irene_floor import DEFAULT_FLOOR_CONFIG, FloorConfig
from services.analytics.signal_watch import (
    FAMILY_FRESHNESS,
    FAMILY_FX,
    FAMILY_LIQUIDITY,
    FAMILY_PRICE,
    SINGLETON_SUBJECT_KEY_BY_FAMILY,
)
from services.watch_desk.calibration import (
    effective_floor_config,
    effective_warn_threshold_pct,
)

_LOG = logging.getLogger(__name__)

#: The defined families this resolution answers for — the ones with a pure
#: producer (ADR-0116 §4). All four since P5. The resolution, the
#: sensitivity lookups and the beat's evaluator are family-blind below this
#: constant and :data:`_SHAPE_BY_FAMILY`.
_RESOLVED_SIGNAL_FAMILIES: tuple[str, ...] = (
    FAMILY_PRICE,
    FAMILY_FX,
    FAMILY_FRESHNESS,
    FAMILY_LIQUIDITY,
)


#: What a family's shape function answers: the trigger threshold in the
#: family's own badness unit, and the observation window in days — or
#: ``None`` when a defining column the b033 CHECKs require is absent, which
#: means the schema changed under us and the subject must be skipped rather
#: than half-evaluated.
_SignalShape = tuple[Decimal, int | None] | None


def _price_shape(row: WatchpointDTO) -> _SignalShape:
    """``price``: the decline threshold, over a window stated in days."""
    if row.drop_pct is None or row.window_days is None:
        return None
    return row.drop_pct, row.window_days


def _fx_shape(row: WatchpointDTO) -> _SignalShape:
    """``fx``: the absolute-move threshold, over a window stated in days."""
    if row.move_pct is None or row.window_days is None:
        return None
    return row.move_pct, row.window_days


def _freshness_shape(row: WatchpointDTO) -> _SignalShape:
    """``freshness``: the age limit, which is threshold and window at once.

    "Restated within the last 120 days" is one statement; the magnitude is
    measured in the same days it bounds, so there is nothing to state twice
    and nothing for a second column to disagree about.
    """
    if row.max_age_days is None:
        return None
    return Decimal(row.max_age_days), row.max_age_days


def _liquidity_shape(row: WatchpointDTO) -> _SignalShape:
    """``liquidity``: the fixed 100-point scale, over a horizon in months.

    The threshold is not read off the row: what the operator calibrates is
    ``min_coverage_ratio``, and 100 is what "at the floor" is worth once
    the ratio has been restated as a badness figure (see
    :mod:`services.analytics.cash_coverage_watch`). The window is ``None``
    because a horizon in months resolves to days only against an evaluation
    date, which the resolution does not have and the producer does.
    """
    if row.horizon_months is None or row.min_coverage_ratio is None:
        return None
    return COVERAGE_THRESHOLD_PCT, None


#: The four projections from typed columns onto the two shared scalars —
#: the only place this module tells the families apart. ADR-0116 §1 types
#: every parameter rather than pooling them in a JSONB blob, which is why a
#: projection is needed at all and why it is four named functions rather
#: than a column-name lookup that would have nowhere to put the two
#: families whose threshold is not a column.
_SHAPE_BY_FAMILY: Mapping[str, Callable[[WatchpointDTO], _SignalShape]] = MappingProxyType(
    {
        FAMILY_PRICE: _price_shape,
        FAMILY_FX: _fx_shape,
        FAMILY_FRESHNESS: _freshness_shape,
        FAMILY_LIQUIDITY: _liquidity_shape,
    }
)

__all__ = [
    "SignalWatchpoint",
    "SubjectOverlay",
    "WatchDeskResolution",
    "resolve_watch_desk",
    "rss_overlay_subject_key",
]


def rss_overlay_subject_key(tag: str) -> str:
    """Return the overlay ``subject_key`` for one curated RSS tag.

    The beat's RSS subjects are *buckets* — ``rss:cluster:<hash>``, formed
    per ``(day, tag)`` from item membership and therefore ephemeral. There
    is nothing durable to hang a mute on there. What ADR-0116 §3 says is
    enumerated for the family is the closed ``_KNOWN_TAGS`` vocabulary, and
    that is exactly what the monitor's press group lists, so the tag is the
    overlay's subject.

    ``rss:{tag}`` cannot collide with a bucket key: every bucket key
    carries the literal ``cluster`` segment
    (:data:`services.analytics.rss_bucketing.SUBJECT_KEY_PREFIX`), and
    ``cluster`` is not a curated tag.

    Args:
        tag: A curated RSS tag (a member of ``_KNOWN_TAGS``).

    Returns:
        The overlay subject key, e.g. ``rss:equities``.
    """
    return f"rss:{tag}"


@dataclass(frozen=True)
class SubjectOverlay:
    """One derived subject's sensitivity overlay, as currently in force.

    A *sensitivity overlay only* (ADR-0116 §3): it never carries the
    subject's identity or its ceiling — those stay with the limit set, and
    the schema's per-family CHECKs make that structural rather than
    conventional.

    Attributes:
        watchpoint_id: The stable identity, for the editor to revise.
        subject_key: The derived subject this row overlays.
        family: ``saa`` / ``anlv`` / ``rss``.
        display_name: Operator-readable label.
        muted: Suppresses *finding creation* only (ADR-0116 §3) — the
            watch-state upserts, the delta computation and the monitor row
            all continue.
        warn_threshold_pct: Per-subject WARN override, or ``None`` for the
            tenant default.
        re_trigger_delta: Per-subject magnitude re-trigger override, or
            ``None`` for the family default.
        notes: The operator's free-text annotation, if any.
    """

    watchpoint_id: UUID
    subject_key: str
    family: str
    display_name: str
    muted: bool
    warn_threshold_pct: Decimal | None
    re_trigger_delta: Decimal | None
    notes: str | None


@dataclass(frozen=True)
class SignalWatchpoint:
    """One defined signal family's watchpoint, as currently in force.

    Unlike a :class:`SubjectOverlay` this *defines* its subject (ADR-0116
    §3): without the row there is no subject, no threshold and nothing
    observed. It carries the same three sensitivity fields, because a
    defined subject can be muted and re-calibrated exactly like a derived
    one — that half of the registry is family-agnostic.

    Attributes:
        watchpoint_id: The stable identity, for the editor to revise.
        subject_key: The key the producer emits (``price:{instrument_id}``
            / ``fx:{BASE}/{QUOTE}`` / ``liquidity:cash_coverage``), taken
            from the registry rather than re-derived, per ADR-0116 §1. For
            ``freshness`` it is the wildcard
            :data:`~services.analytics.signal_watch.FRESHNESS_WILDCARD_SUBJECT_KEY`
            — the one row states a rule, and the beat enumerates a subject
            per active investment under it.
        family: ``price`` / ``fx`` / ``freshness`` / ``liquidity``.
        display_name: Operator-readable label — the instrument or pair
            name a finding's note is written around. For ``freshness`` the
            beat substitutes each investment's own name as it enumerates.
        muted: Suppresses *finding creation* only (ADR-0116 §3). Unlike a
            quota subject, a **triggered** signal subject can be muted:
            no regulatory floor stands behind a threshold the operator
            chose (see
            :func:`services.analytics.irene_delta.mute_suppresses`).
        warn_threshold_pct: Per-subject WARN override, or ``None`` for the
            tenant default. For a signal family it is the fraction *of the
            trigger threshold* at which the subject reads Approaching.
        re_trigger_delta: Per-subject magnitude re-trigger override, or
            ``None`` for the family default.
        instrument_id: The watched instrument (``price`` only).
        currency_pair: The watched ``BASE/QUOTE`` pair (``fx`` only).
        threshold_pct: The trigger threshold in the family's own badness
            unit — ``drop_pct`` for ``price``, ``move_pct`` for ``fx``,
            ``max_age_days`` for ``freshness``, and the fixed
            :data:`~services.analytics.cash_coverage_watch.COVERAGE_THRESHOLD_PCT`
            for ``liquidity``, whose calibrated number is the ratio floor
            rather than the threshold.
        window_days: The observation window in days, for the families
            whose watchpoint states one — ``price`` / ``fx`` directly,
            ``freshness`` as its age limit. ``None`` for ``liquidity``:
            a horizon in months resolves to days only against an
            evaluation date, so its producer derives the window rather
            than the registry approximating one.
        max_age_days: The NAV age limit (``freshness`` only).
        horizon_months: The forward horizon in months (``liquidity`` only).
        min_coverage_ratio: The coverage floor the operator calibrated
            (``liquidity`` only) — what its findings speak in.
        notes: The operator's free-text annotation, if any.
    """

    watchpoint_id: UUID
    subject_key: str
    family: str
    display_name: str
    muted: bool
    warn_threshold_pct: Decimal | None
    re_trigger_delta: Decimal | None
    instrument_id: UUID | None
    currency_pair: str | None
    threshold_pct: Decimal
    window_days: int | None
    notes: str | None
    max_age_days: int | None = None
    horizon_months: int | None = None
    min_coverage_ratio: Decimal | None = None


#: What the sensitivity lookups read. The two halves of the registry share
#: these three fields and nothing else, which is exactly the amount of
#: commonality the lookups need.
_Sensitivity = SubjectOverlay | SignalWatchpoint


@dataclass(frozen=True)
class WatchDeskResolution:
    """Everything one tenant's beat and monitor need, resolved once.

    Attributes:
        config: The effective ``FloorConfig`` (defaults ⊕ the tenant's
            calibration revision).
        warn_default_pct: The tenant-wide WARN threshold, as a percentage
            of the ceiling — or, for a signal family, of the trigger
            threshold (ADR-0116 §4). The value to pass the coverage
            engine.
        overlays: Per-subject overlays keyed by ``subject_key``, for the
            derived families only.
        signals: The effective signal watchpoints keyed by ``subject_key``,
            for the defined families with a producer. Empty by default so
            a caller that predates them — or a test stating only the
            quota half — constructs a resolution unchanged.
    """

    config: FloorConfig
    warn_default_pct: Decimal
    overlays: Mapping[str, SubjectOverlay]
    signals: Mapping[str, SignalWatchpoint] = field(default_factory=lambda: MappingProxyType({}))

    def overlay_for(self, subject_key: str) -> SubjectOverlay | None:
        """Return the subject's overlay, or ``None`` when it has none.

        ``None`` is the ordinary case: a subject without an overlay row
        behaves exactly as it did before ADR-0116 (§3).
        """
        return self.overlays.get(subject_key)

    def signals_for(self, family: str) -> tuple[SignalWatchpoint, ...]:
        """Return this tenant's effective watchpoints for one signal family.

        In the repository's stable order, so the beat evaluates subjects
        in the same sequence the monitor lists them.

        Args:
            family: One of the four defined families. A family with no
                producer yields an empty tuple — it is filtered at
                resolution. The two singleton families yield at most one
                watchpoint, which the repository enforces on write.

        Returns:
            The effective watchpoints of that family.
        """
        return tuple(
            watchpoint for watchpoint in self.signals.values() if watchpoint.family == family
        )

    def warn_threshold_for(self, subject_key: str) -> Decimal:
        """Return the WARN threshold in force for one subject.

        The per-subject override when there is one, else the tenant
        default. This is the value that replaces the former global 90% at
        *every* point it was read — the coverage classification, the
        re-classification of an acknowledged magnitude, the signal
        producers' Approaching band, and the monitor's gauge mark.
        """
        sensitivity = self._sensitivity_for(subject_key)
        if sensitivity is not None and sensitivity.warn_threshold_pct is not None:
            return sensitivity.warn_threshold_pct
        return self.warn_default_pct

    def re_trigger_delta_for(self, subject_key: str) -> Decimal:
        """Return the magnitude re-trigger delta in force for one subject.

        The per-subject override when there is one, else the family
        default from the effective ``FloorConfig``. An unknown family
        yields ``0``, matching the lookup the delta layer would otherwise
        perform.
        """
        sensitivity = self._sensitivity_for(subject_key)
        if sensitivity is not None and sensitivity.re_trigger_delta is not None:
            return sensitivity.re_trigger_delta
        family = subject_type_from_key(subject_key)
        return self.config.re_trigger_delta.get(family, Decimal("0"))

    def is_muted(self, subject_key: str) -> bool:
        """Return whether finding creation is suppressed for one subject."""
        sensitivity = self._sensitivity_for(subject_key)
        return sensitivity is not None and sensitivity.muted

    def _sensitivity_for(self, subject_key: str) -> _Sensitivity | None:
        """Return whichever half of the registry answers for this subject.

        The two maps are disjoint by family, so "overlay first" is an
        ordering, not a precedence rule: no subject can appear in both.

        The last lookup is the **singleton fallback** (ADR-0116 §4): an
        enumerated ``freshness:{investment_id}`` subject has no registry
        row of its own, so its mute, WARN override and re-trigger delta
        come from the one row that states the family's rule. It is written
        as a fallback rather than as a family branch so that a subject
        which *does* carry its own row — should per-investment overrides
        ever be commissioned — would win here without this method changing.
        """
        overlay = self.overlays.get(subject_key)
        if overlay is not None:
            return overlay
        signal = self.signals.get(subject_key)
        if signal is not None:
            return signal
        singleton_key = SINGLETON_SUBJECT_KEY_BY_FAMILY.get(subject_type_from_key(subject_key))
        if singleton_key is None:
            return None
        return self.signals.get(singleton_key)


async def resolve_watch_desk(
    session: AsyncSession,
    *,
    as_of: datetime,
    defaults: FloorConfig = DEFAULT_FLOOR_CONFIG,
) -> WatchDeskResolution:
    """Resolve one tenant's effective Watch Desk calibration at ``as_of``.

    Must run on a **tenant-scoped** session: both reads are RLS-policed,
    so the resolution is the active tenant's and nobody else's.

    Args:
        session: A tenant-scoped :class:`AsyncSession`.
        as_of: The evaluation instant (timezone-aware). The beat passes
            its clock; the monitor passes request time.
        defaults: The code defaults to compose over. Overridable for
            tests; production always passes ``DEFAULT_FLOOR_CONFIG``.
            This is the **only** place the defaults enter — neither the
            beat nor the monitor reads them directly (ADR-0116 §5).

    Returns:
        The :class:`WatchDeskResolution` for this tenant.

    Raises:
        FloorCalibrationInvalid: If the stored revision no longer composes
            into a valid configuration. Deliberately propagated rather
            than degraded to defaults: ADR-0116 §5 requires a loud failure,
            because silently running a configuration the operator did not
            choose is worse than not running.
    """
    calibration_repo = FloorCalibrationRepository(session)
    # Two reads of a one-row table. `effective_floor_config` owns the
    # composition and the typed error; `effective_warn_threshold_pct` needs
    # the DTO because the WARN threshold is not a FloorConfig field. Forking
    # either contract to save one indexed read would be a poor trade.
    config = await effective_floor_config(calibration_repo, as_of, defaults=defaults)
    warn_default = effective_warn_threshold_pct(await calibration_repo.effective_calibration(as_of))

    overlays: dict[str, SubjectOverlay] = {}
    signals: dict[str, SignalWatchpoint] = {}
    # A retired identity never reaches this loop: `effective_watchpoints`
    # excludes it, so retirement stops evaluation on the very next beat.
    # Findings it already raised are neither closed nor deleted by that —
    # they are immutable history (ADR-0085), closed by a falling edge only
    # if the subject is still watched when it eases.
    for row in await WatchpointRepository(session).effective_watchpoints(as_of):
        if row.family in OVERLAY_FAMILIES:
            existing_overlay = overlays.get(row.subject_key)
            if existing_overlay is not None:
                _warn_duplicate_subject(row, kept=existing_overlay.watchpoint_id)
                continue
            overlays[row.subject_key] = _to_overlay(row)
            continue

        if row.family not in _RESOLVED_SIGNAL_FAMILIES:
            # No producer, nothing observed — and answering for a subject
            # nothing observes would be a promise this resolution cannot
            # keep. Unreached since P5 landed the last two producers; kept
            # because it is what makes adding a fifth family safe.
            continue

        existing_signal = signals.get(row.subject_key)
        if existing_signal is not None:
            _warn_duplicate_subject(row, kept=existing_signal.watchpoint_id)
            continue
        signal = _to_signal(row)
        if signal is not None:
            signals[row.subject_key] = signal

    return WatchDeskResolution(
        config=config,
        warn_default_pct=warn_default,
        overlays=MappingProxyType(overlays),
        signals=MappingProxyType(signals),
    )


def _warn_duplicate_subject(row: WatchpointDTO, *, kept: UUID) -> None:
    """Report two identities naming one subject, and which one won.

    Nothing in the schema forbids the pair. Keeping the first in the
    repository's stable order makes the resolution deterministic; saying
    so makes it visible — a silent second answer is exactly what this
    module exists to prevent.
    """
    _LOG.warning(
        "watch-desk resolution: subject %r carries more than one watchpoint "
        "(keeping watchpoint %s, ignoring %s).",
        row.subject_key,
        kept,
        row.watchpoint_id,
    )


def _to_overlay(row: WatchpointDTO) -> SubjectOverlay:
    """Project one effective watchpoint version into its overlay view."""
    return SubjectOverlay(
        watchpoint_id=row.watchpoint_id,
        subject_key=row.subject_key,
        family=row.family,
        display_name=row.display_name,
        muted=row.muted,
        warn_threshold_pct=row.warn_threshold_pct,
        re_trigger_delta=row.re_trigger_delta,
        notes=row.notes,
    )


def _to_signal(row: WatchpointDTO) -> SignalWatchpoint | None:
    """Project one effective watchpoint version into its signal view.

    Returns ``None`` for a row missing a defining parameter. The b033
    per-family CHECKs make that unreachable — a ``price`` row without a
    ``drop_pct`` cannot be written — so this is the defensive branch for a
    schema that changed under us, and it drops the one subject with a
    warning rather than failing the tenant's whole beat.
    """
    shape = _SHAPE_BY_FAMILY[row.family](row)
    if shape is None:  # pragma: no cover - CHECK-guarded
        _LOG.warning(
            "watch-desk resolution: %s watchpoint %s (%r) is missing a defining "
            "parameter and cannot be evaluated; skipping the subject.",
            row.family,
            row.watchpoint_id,
            row.subject_key,
        )
        return None
    threshold, window_days = shape
    return SignalWatchpoint(
        watchpoint_id=row.watchpoint_id,
        subject_key=row.subject_key,
        family=row.family,
        display_name=row.display_name,
        muted=row.muted,
        warn_threshold_pct=row.warn_threshold_pct,
        re_trigger_delta=row.re_trigger_delta,
        instrument_id=row.instrument_id,
        currency_pair=row.currency_pair,
        threshold_pct=threshold,
        window_days=window_days,
        notes=row.notes,
        max_age_days=row.max_age_days,
        horizon_months=row.horizon_months,
        min_coverage_ratio=row.min_coverage_ratio,
    )
