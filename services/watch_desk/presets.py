# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Sensitivity presets: values, the displayed preset, and the atomic apply.

A preset is a named pair of values for one scalar family (ADR-0133 §3.2):
the WARN step — when Irene notices, as a percentage of the ceiling or
trigger threshold — and the re-trigger delta — how far a magnitude must
move before she speaks again. Presets decide **when Irene notices**, never
**how urgent** a finding is: floors, caps, band boundaries and the options
gate are not theirs to touch.

The delta's unit is the family's own: percentage points for ``saa``,
``anlv``, ``price`` and ``fx``; days of NAV age for ``freshness``; points on
the 100-scale for ``liquidity``. Press coverage (``rss``) has no scalar
magnitude and so no preset (D-WD-13).

Three functions, one per concern of ADR-0133 §3.3:

* :func:`preset_values` — the mapping. **Standard** is defined by reference
  to the code defaults rather than restating them, so it moves with them.
* :func:`displayed_preset` — read back from a family's *effective* values
  in the resolution, never from the preset recorded on a scope revision.
  A family whose values match no preset shows ``custom``, a display state
  that is never stored (D-WD-21).
* :func:`apply_presets` — writes the values into one calibration revision
  and records each preset on a scope revision, both halves in one
  savepoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core.exceptions import WatchFamilyScopeInvalid
from core.models.watch_family_scope import (
    PRESETLESS_FAMILIES,
    SCOPE_PRESETS,
    WATCHPOINT_FAMILIES,
)
from core.repositories import (
    FloorCalibrationDTO,
    FloorCalibrationRepository,
    WatchFamilyScopeDTO,
    WatchFamilyScopeRepository,
)
from services.analytics.irene_floor import (  # noqa: PLC2701 — ADR-0133 §3.2 defines Standard by the private delta map
    _DEFAULT_RE_TRIGGER_DELTA,
    DEFAULT_WARN_THRESHOLD_PCT,
)
from services.watch_desk.calibration import save_calibration_revision
from services.watch_desk.overlay import WatchDeskResolution, resolve_watch_desk

__all__ = [
    "CUSTOM",
    "NERVOUS_RE_TRIGGER_DELTA",
    "NERVOUS_WARN_PCT",
    "PRESET_NERVOUS",
    "PRESET_QUIET",
    "PRESET_STANDARD",
    "QUIET_RE_TRIGGER_DELTA",
    "QUIET_WARN_PCT",
    "SCALAR_FAMILIES",
    "PresetApplication",
    "PresetValues",
    "apply_presets",
    "displayed_preset",
    "preset_values",
]

PRESET_QUIET: str = "quiet"
PRESET_STANDARD: str = "standard"
PRESET_NERVOUS: str = "nervous"

#: The display state of a family whose values match no preset. Never stored:
#: the scope table's CHECK refuses it, and :func:`preset_values` refuses it.
CUSTOM: str = "custom"

QUIET_WARN_PCT: Decimal = Decimal("95")
QUIET_RE_TRIGGER_DELTA: Decimal = Decimal("8")
NERVOUS_WARN_PCT: Decimal = Decimal("80")
NERVOUS_RE_TRIGGER_DELTA: Decimal = Decimal("3")

#: The families a preset can set, in ``WATCHPOINT_FAMILIES`` order.
SCALAR_FAMILIES: tuple[str, ...] = tuple(
    family for family in WATCHPOINT_FAMILIES if family not in PRESETLESS_FAMILIES
)


@dataclass(frozen=True)
class PresetValues:
    """The two values one preset sets on one family.

    Attributes:
        warn_pct: The family's WARN step, as a percentage of the ceiling or
            trigger threshold.
        re_trigger_delta: The family's magnitude re-trigger delta, in the
            family's own unit.
    """

    warn_pct: Decimal
    re_trigger_delta: Decimal


@dataclass(frozen=True)
class PresetApplication:
    """What one :func:`apply_presets` call wrote.

    Attributes:
        calibration: The new calibration revision, as stored (deviations
            only).
        scope: One scope revision per family applied, in
            ``WATCHPOINT_FAMILIES`` order.
    """

    calibration: FloorCalibrationDTO
    scope: tuple[WatchFamilyScopeDTO, ...]


def preset_values(preset: str, family: str) -> PresetValues:
    """Return the values one preset sets on one scalar family.

    Args:
        preset: One of ``SCOPE_PRESETS``. ``custom`` is a display state and
            is refused.
        family: One of :data:`SCALAR_FAMILIES`.

    Returns:
        The family's :class:`PresetValues` under that preset. Standard's
        are the code defaults themselves, not copies of them.

    Raises:
        WatchFamilyScopeInvalid: With ``field="family"`` for a family that
            is unknown or carries no preset, with ``field="preset"`` for a
            preset outside the vocabulary.
    """
    _require_scalar_family(family)
    if preset not in SCOPE_PRESETS:
        raise WatchFamilyScopeInvalid(
            f"Invalid preset {preset!r}; expected one of {list(SCOPE_PRESETS)}.",
            field="preset",
        )
    if preset == PRESET_QUIET:
        return PresetValues(warn_pct=QUIET_WARN_PCT, re_trigger_delta=QUIET_RE_TRIGGER_DELTA)
    if preset == PRESET_NERVOUS:
        return PresetValues(warn_pct=NERVOUS_WARN_PCT, re_trigger_delta=NERVOUS_RE_TRIGGER_DELTA)
    return PresetValues(
        warn_pct=DEFAULT_WARN_THRESHOLD_PCT,
        re_trigger_delta=_DEFAULT_RE_TRIGGER_DELTA[family],
    )


def displayed_preset(resolution: WatchDeskResolution, family: str) -> str | None:
    """Return the preset a family's effective values amount to.

    Reads the family's values only — the family WARN step and the family
    delta of the effective ``FloorConfig`` — so a per-subject override
    changes its subject, never the family's preset.

    Args:
        resolution: The tenant's resolution.
        family: One of ``WATCHPOINT_FAMILIES``.

    Returns:
        ``None`` for a preset-less family; else the first preset, in
        ``SCOPE_PRESETS`` order, whose values both equal the effective ones
        (numerically); else :data:`CUSTOM`.

    Raises:
        WatchFamilyScopeInvalid: With ``field="family"`` for an unknown
            family.
    """
    if family not in WATCHPOINT_FAMILIES:
        raise _unknown_family(family)
    if family in PRESETLESS_FAMILIES:
        return None
    effective = PresetValues(
        warn_pct=resolution.family_warn_threshold_for(family),
        re_trigger_delta=resolution.config.re_trigger_delta[family],
    )
    for preset in SCOPE_PRESETS:
        if preset_values(preset, family) == effective:
            return preset
    return CUSTOM


async def apply_presets(
    session: AsyncSession,
    *,
    presets: Mapping[str, str],
    actor: UUID,
    effective_from: datetime,
) -> PresetApplication:
    """Apply presets to families: one calibration revision, one scope row each.

    Every pair is validated before anything is read or written. The reads
    and writes then run in one savepoint, so a refusal rolls back both
    halves even when the caller catches it and its transaction commits.

    The new calibration revision states the whole calibration (ADR-0116
    §7): everything the newest revision carries is carried forward, and
    only the applied families' WARN step and delta change. Each family's
    switch is carried from the resolution, so applying a preset never
    switches a family back on.

    The caller opens ``session`` with ``tenant_context(..., user_id=actor)``
    so the audit trigger names the actor. Nothing is committed here.

    Args:
        session: A tenant-scoped session.
        presets: Scalar family → preset. Must not be empty.
        actor: The acting user, recorded as ``changed_by``.
        effective_from: When both revisions take effect (timezone-aware;
            strictly after the newest calibration revision and after each
            applied family's newest scope revision).

    Returns:
        The :class:`PresetApplication` written.

    Raises:
        WatchFamilyScopeInvalid: On an empty mapping, a family that is
            unknown or carries no preset, a preset outside the vocabulary
            (``custom`` included), or a scope revision that does not
            advance.
        FloorCalibrationInvalid: If the calibration revision is refused by
            the write path, e.g. on a naive or non-advancing
            ``effective_from``.
    """
    if not presets:
        raise WatchFamilyScopeInvalid("No family to apply a preset to.", field="presets")
    values = {family: preset_values(preset, family) for family, preset in presets.items()}
    families = tuple(family for family in WATCHPOINT_FAMILIES if family in values)

    async with session.begin_nested():
        resolution = await resolve_watch_desk(session, as_of=effective_from)
        repository = FloorCalibrationRepository(session)
        current = await repository.newest_revision()

        # A revision's stored deviations are its desired values; the write
        # path reduces them again against the defaults.
        re_trigger_delta = dict(current.re_trigger_delta) if current is not None else {}
        warn_pct = dict(current.warn_pct) if current is not None else {}
        for family in families:
            warn_pct[family] = values[family].warn_pct
            re_trigger_delta[family] = values[family].re_trigger_delta

        calibration = await save_calibration_revision(
            repository,
            effective_from=effective_from,
            warn_default_pct=current.warn_default_pct if current is not None else None,
            band_boundaries=current.band_boundaries if current is not None else None,
            options_min_band=current.options_min_band if current is not None else None,
            floor=dict(current.floor) if current is not None else {},
            cap=dict(current.cap) if current is not None else {},
            re_trigger_delta=re_trigger_delta,
            warn_pct=warn_pct,
            notes=None,
        )

        scope_repository = WatchFamilyScopeRepository(session)
        scope = tuple(
            [
                await scope_repository.insert_revision(
                    family=family,
                    effective_from=effective_from,
                    enabled=resolution.is_family_enabled(family),
                    changed_by=actor,
                    preset=presets[family],
                )
                for family in families
            ]
        )

    return PresetApplication(calibration=calibration, scope=scope)


def _require_scalar_family(family: str) -> None:
    """Refuse a family that is unknown or carries no preset."""
    if family not in WATCHPOINT_FAMILIES:
        raise _unknown_family(family)
    if family in PRESETLESS_FAMILIES:
        raise WatchFamilyScopeInvalid(
            f"The {family!r} family has no scalar magnitude and carries no preset.",
            field="family",
        )


def _unknown_family(family: str) -> WatchFamilyScopeInvalid:
    """Build the refusal for a family outside ``WATCHPOINT_FAMILIES``."""
    return WatchFamilyScopeInvalid(
        f"Unknown watchpoint family {family!r}; expected one of {list(WATCHPOINT_FAMILIES)}.",
        field="family",
    )
