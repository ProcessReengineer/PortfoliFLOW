# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Floor Config calibration write path and per-tenant resolution.

Two functions, one on each side of the ``floor_calibration`` table
(ADR-0116 §5/§7):

* :func:`save_calibration_revision` — the **sanctioned write path**. It
  takes the desired effective values (what the Calibration editor's form
  says), composes them over the code defaults, runs the composed result
  through the full ``FloorConfig`` constructor **and** the pinned
  invariants, reduces it to deviations, and only then persists. An
  invalid combination is rejected at write time: the beat must never be
  the first to discover an inverted configuration.
* :func:`effective_floor_config` — the read the beat performs per run:
  defaults ⊕ the tenant's latest effective revision. Per-subject overlay
  values are resolved separately, subject by subject, and passed to the
  pure layers as plain arguments (ADR-0116 §3).

Why the validation lives here and not in the repository
--------------------------------------------------------
``FloorConfig`` is an analytics object and ``core/`` imports nothing
from within the project, so a repository cannot compose against it. That
is not a compromise: ADR-0116 §5 requires the *write path* to reject a
bad configuration, and this module is it. The repository below enforces
what it can see from ``core/`` — that a key has a column, that
``fund_closure`` is refused, that ``effective_from`` advances — and this
module enforces what the configuration means.

The one asymmetry worth naming: ``warn_default_pct`` is validated and
stored here but is **not** part of ``FloorConfig``. The WARN threshold
has always been a call parameter of the coverage engine rather than a
config field (ADR-0116 §Context), so it is resolved separately by
:func:`effective_warn_threshold_pct` and threaded to the engine by its
caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from core.exceptions import FloorCalibrationInvalid
from core.repositories.floor_calibration_repository import (
    FloorCalibrationDTO,
    FloorCalibrationRepository,
    require_known_calibration_keys,
)
from services.analytics.floor_composition import compose_floor_config
from services.analytics.irene_floor import (
    DEFAULT_FLOOR_CONFIG,
    DEFAULT_WARN_THRESHOLD_PCT,
    FloorConfig,
)

__all__ = [
    "effective_floor_config",
    "effective_warn_threshold_pct",
    "save_calibration_revision",
]

#: Exclusive bounds on the tenant-wide WARN default — the same rule the
#: per-subject override obeys (ADR-0116 §3). Below 50 the "warning" fires
#: at less than half the ceiling; at or above 100 it fires only once the
#: ceiling is already breached, which is not a warning at all.
_WARN_MIN_EXCLUSIVE: Decimal = Decimal("50")
_WARN_MAX_EXCLUSIVE: Decimal = Decimal("100")

#: A structurally valid but meaningless UUID for the not-yet-persisted
#: candidate the validator composes. It never reaches the database.
_PLACEHOLDER_ID: UUID = UUID(int=0)


async def effective_floor_config(
    repository: FloorCalibrationRepository,
    as_of: datetime,
    *,
    defaults: FloorConfig = DEFAULT_FLOOR_CONFIG,
) -> FloorConfig:
    """Resolve one tenant's effective ``FloorConfig`` at ``as_of``.

    Args:
        repository: Calibration repository bound to a tenant-scoped
            session.
        as_of: The evaluation instant (timezone-aware).
        defaults: The code defaults to compose over. Overridable for
            tests; production always passes ``DEFAULT_FLOOR_CONFIG``.

    Returns:
        ``defaults`` for a tenant with no revision, otherwise the composed
        configuration.

    Raises:
        FloorCalibrationInvalid: If the stored revision no longer composes
            into a valid configuration — which a code-default change can
            cause even though the write path vetted the revision when it
            was made. Failing loudly beats running an inverted clamp.
    """
    calibration = await repository.effective_calibration(as_of)
    try:
        return compose_floor_config(defaults, calibration)
    except ValueError as exc:
        raise FloorCalibrationInvalid(
            f"The effective calibration no longer composes into a valid Floor Config: {exc}"
        ) from exc


def effective_warn_threshold_pct(calibration: FloorCalibrationDTO | None) -> Decimal:
    """Return the tenant-wide WARN threshold, defaulting when unset.

    Args:
        calibration: The tenant's effective revision, or ``None``.

    Returns:
        The stored ``warn_default_pct``, or
        :data:`services.analytics.irene_floor.DEFAULT_WARN_THRESHOLD_PCT`.
    """
    if calibration is None or calibration.warn_default_pct is None:
        return DEFAULT_WARN_THRESHOLD_PCT
    return calibration.warn_default_pct


async def save_calibration_revision(
    repository: FloorCalibrationRepository,
    *,
    effective_from: datetime,
    warn_default_pct: Decimal | None = None,
    band_boundaries: tuple[int, int] | None = None,
    options_min_band: str | None = None,
    floor: Mapping[str, int] | None = None,
    cap: Mapping[str, int] | None = None,
    re_trigger_delta: Mapping[str, Decimal] | None = None,
    notes: str | None = None,
    defaults: FloorConfig = DEFAULT_FLOOR_CONFIG,
) -> FloorCalibrationDTO:
    """Validate and persist one calibration revision.

    Arguments carry the **desired effective values**, not deltas: pass
    what the editor's form says, including the fields the operator left
    at their defaults. Anything equal to a code default is stored as NULL,
    so the persisted row is a record of what the tenant changed — which
    is what lets a later change to a default reach every tenant that
    never overrode it, and what lets the editor mark each field
    "default / customised" from the row alone (ADR-0116 §7).

    Args:
        repository: Calibration repository bound to a tenant-scoped
            session.
        effective_from: When the revision takes effect (timezone-aware,
            strictly after the newest existing revision).
        warn_default_pct: Tenant-wide WARN default, in ``(50, 100)``.
        band_boundaries: The two urgency cut points ``(b0, b1)``.
        options_min_band: One of ``informational`` / ``noteworthy`` /
            ``critical``.
        floor: Trigger type → floor. ``fund_closure`` is refused.
        cap: Source or trigger → cap. ``fund_closure`` is refused.
        re_trigger_delta: Subject family → magnitude delta.
        notes: Optional annotation.
        defaults: The code defaults to compose and compare against.

    Returns:
        The persisted :class:`FloorCalibrationDTO`, whose sparse maps show
        exactly what ended up stored.

    Raises:
        FloorCalibrationInvalid: On an unknown or pinned key, a WARN
            default out of bounds, a negative delta, or any composed
            configuration the ``FloorConfig`` constructor or the pinned
            invariants reject.
    """
    floors = dict(floor or {})
    caps = dict(cap or {})
    deltas = dict(re_trigger_delta or {})

    # First, so a typo is reported as a typo rather than as a missing cap.
    require_known_calibration_keys(floor=floors, cap=caps, re_trigger_delta=deltas)

    if warn_default_pct is not None and not (
        _WARN_MIN_EXCLUSIVE < Decimal(str(warn_default_pct)) < _WARN_MAX_EXCLUSIVE
    ):
        raise FloorCalibrationInvalid(
            f"warn_default_pct must lie strictly between {_WARN_MIN_EXCLUSIVE} and "
            f"{_WARN_MAX_EXCLUSIVE}; got {warn_default_pct}.",
            field="warn_default_pct",
        )
    for family, delta in deltas.items():
        if Decimal(str(delta)) < 0:
            raise FloorCalibrationInvalid(
                f"re_trigger_delta[{family!r}] must not be negative; got {delta}. "
                "A negative delta would re-trigger on every observation.",
                field=f"re_trigger_delta_{family}",
            )

    # Composition IS the validation: the candidate has to be a configuration
    # the beat could actually run (ADR-0116 §5). Composed against a throwaway
    # DTO so the constructor and the pinned invariants see exactly the
    # configuration this revision would produce.
    candidate = FloorCalibrationDTO(
        id=_PLACEHOLDER_ID,
        tenant_id=_PLACEHOLDER_ID,
        effective_from=effective_from,
        warn_default_pct=warn_default_pct,
        band_boundaries=band_boundaries,
        options_min_band=options_min_band,
        floor=floors,
        cap=caps,
        re_trigger_delta=deltas,
    )
    try:
        compose_floor_config(defaults, candidate)
    except ValueError as exc:
        raise FloorCalibrationInvalid(
            f"The revision would not be a valid Floor Config: {exc}"
        ) from exc

    return await repository.save_revision(
        effective_from=effective_from,
        warn_default_pct=_deviation(warn_default_pct, DEFAULT_WARN_THRESHOLD_PCT),
        band_boundaries=_deviation(band_boundaries, defaults.band_boundaries),
        options_min_band=_deviation(options_min_band, defaults.options_min_band),
        floor=_deviating_entries(floors, defaults.floor),
        cap=_deviating_entries(caps, defaults.cap),
        re_trigger_delta=_deviating_entries(deltas, defaults.re_trigger_delta),
        notes=notes,
    )


def _deviation(value, default):  # type: ignore[no-untyped-def]
    """Return ``value``, or ``None`` when absent or equal to ``default``.

    The whole "store deviations only" rule, in one place. ``Decimal``
    comparison is numeric, so ``90`` and ``90.0`` both read as "default".
    """
    if value is None:
        return None
    return None if value == default else value


def _deviating_entries(supplied, defaults):  # type: ignore[no-untyped-def]
    """Keep only the map entries that differ from the default map."""
    return {key: value for key, value in supplied.items() if value != defaults.get(key)}
