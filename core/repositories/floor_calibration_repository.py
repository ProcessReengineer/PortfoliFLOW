# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FloorCalibrationRepository — the tenant's Floor Config deviations.

Backs the ``floor_calibration`` table introduced in migration b033 (per
ADR-0116 §7). Same historisation as ``watchpoints`` and ``limit_sets``:
immutable version rows keyed ``(tenant_id, effective_from)``, the
effective revision being the latest ``effective_from <= as_of``.

**Deviations only.** Every calibration column is nullable and NULL means
"code default". A revision therefore records what a tenant changed, not
a frozen copy of ``DEFAULT_FLOOR_CONFIG`` — so a later change to a code
default reaches every tenant that never overrode that field, and the
Calibration editor can mark each field "default / customised" from the
stored row alone. An absent row means pure defaults, which is why no
tenant is ever seeded with one.

The DTO exposes ``floor`` / ``cap`` / ``re_trigger_delta`` as **sparse
maps** rather than one attribute per column: "key present" is exactly
"field customised", and that is the shape
:func:`services.analytics.floor_composition.compose_floor_config`
composes with.

Where the validation is, and why it is not here
-----------------------------------------------
This repository validates only what it can see from ``core/``: that a
supplied key has a column at all, that ``effective_from`` is
timezone-aware and advances past the newest revision, and that
``fund_closure`` — a pinned level with no column by design — is refused
with its reason.

Whether the *resulting configuration* is coherent (``floor <= cap``, a
covered band scale, and the ADR-0116 §7 pinned invariants) is decided by
:func:`services.watch_desk.calibration.save_calibration_revision`, the
sanctioned write path, which composes the candidate over the defaults
and runs the full ``FloorConfig`` validation before calling
:meth:`FloorCalibrationRepository.save_revision`. That split is forced
by the layering contract — ``core/`` imports nothing from within the
project, and ``FloorConfig`` lives in ``services/analytics`` — and it
costs nothing, because ADR-0116 §5's requirement is that the *write
path* reject an inverted configuration, not that the ORM does. Call the
service; this class is its storage half.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import select, text

from core.exceptions import FloorCalibrationInvalid
from core.models.floor_calibration import FloorCalibration
from core.repositories.base import BaseRepository

#: Trigger type → column, for the trigger-type floors. ``fund_closure`` is
#: absent by design: a pinned level has nowhere to be stored, which is the
#: strongest possible way of making it non-editable (ADR-0116 §7
#: invariant 1). The keys restate the ``TRIGGER_*`` vocabulary of
#: ``services.analytics.irene_floor`` as literals because ``core/`` imports
#: nothing from within the project; a regression test pins the two sets
#: together so the restatement cannot drift.
FLOOR_COLUMNS: Mapping[str, str] = MappingProxyType(
    {
        "limit_breach": "floor_limit_breach",
        "limit_escalation": "floor_limit_escalation",
        "all_clear": "floor_all_clear",
        "rss_cluster": "floor_rss_cluster",
        "price_trigger": "floor_price_trigger",
        "fx_trigger": "floor_fx_trigger",
        "freshness_trigger": "floor_freshness_trigger",
        "liquidity_trigger": "floor_liquidity_trigger",
    }
)

#: Cap key → column. Caps are keyed by **both** source and trigger (the
#: effective cap is ``min(cap[source], cap[trigger])``), so both axes appear
#: here. ``fund_closure`` is absent for the same reason as above.
CAP_COLUMNS: Mapping[str, str] = MappingProxyType(
    {
        "internal": "cap_source_internal",
        "rss": "cap_source_rss",
        "limit_breach": "cap_limit_breach",
        "limit_escalation": "cap_limit_escalation",
        "all_clear": "cap_all_clear",
        "rss_cluster": "cap_rss_cluster",
        "price_trigger": "cap_price_trigger",
        "fx_trigger": "cap_fx_trigger",
        "freshness_trigger": "cap_freshness_trigger",
        "liquidity_trigger": "cap_liquidity_trigger",
    }
)

#: Subject family → column, for the per-family magnitude re-trigger delta.
#: All seven families, matching ``FloorConfig.re_trigger_delta``.
DELTA_COLUMNS: Mapping[str, str] = MappingProxyType(
    {
        "saa": "re_trigger_delta_saa",
        "anlv": "re_trigger_delta_anlv",
        "rss": "re_trigger_delta_rss",
        "price": "re_trigger_delta_price",
        "fx": "re_trigger_delta_fx",
        "freshness": "re_trigger_delta_freshness",
        "liquidity": "re_trigger_delta_liquidity",
    }
)

#: The one key a caller might plausibly supply that is refused on purpose
#: rather than for being unknown.
_PINNED_KEY: str = "fund_closure"


@dataclass(frozen=True)
class FloorCalibrationDTO:
    """One tenant's stored deviations from ``DEFAULT_FLOOR_CONFIG``.

    The three maps are **sparse**: a key is present only where the tenant
    deviates. A tenant that never opened the editor has no row at all, so
    ``None`` from :meth:`FloorCalibrationRepository.effective_calibration`
    reads as "pure defaults".

    Attributes:
        id: Primary key of this revision.
        tenant_id: Owning tenant.
        effective_from: The instant this revision took effect.
        warn_default_pct: Tenant-wide WARN default override, or ``None``.
        band_boundaries: The two urgency cut points, or ``None`` for the
            default pair. Set together or not at all (schema CHECK).
        options_min_band: Options-gate override, or ``None``.
        floor: Sparse trigger type → floor overrides.
        cap: Sparse source/trigger → cap overrides.
        re_trigger_delta: Sparse family → delta overrides.
        notes: Optional free-text annotation.
        created_at: Row insertion timestamp.
        updated_at: Row update timestamp (rows are never updated; present
            for house-column parity).
    """

    id: UUID
    tenant_id: UUID
    effective_from: datetime
    warn_default_pct: Decimal | None = None
    band_boundaries: tuple[int, int] | None = None
    options_min_band: str | None = None
    floor: Mapping[str, int] = field(default_factory=dict)
    cap: Mapping[str, int] = field(default_factory=dict)
    re_trigger_delta: Mapping[str, Decimal] = field(default_factory=dict)
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _to_dto(model: FloorCalibration) -> FloorCalibrationDTO:
    """Fold the wide nullable row back into three sparse maps."""
    boundaries: tuple[int, int] | None = None
    if model.band_boundary_0 is not None and model.band_boundary_1 is not None:
        boundaries = (model.band_boundary_0, model.band_boundary_1)

    return FloorCalibrationDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        effective_from=model.effective_from,
        warn_default_pct=model.warn_default_pct,
        band_boundaries=boundaries,
        options_min_band=model.options_min_band,
        floor=MappingProxyType(_sparse(model, FLOOR_COLUMNS)),
        cap=MappingProxyType(_sparse(model, CAP_COLUMNS)),
        re_trigger_delta=MappingProxyType(_sparse(model, DELTA_COLUMNS)),
        notes=model.notes,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sparse(model: FloorCalibration, columns: Mapping[str, str]) -> dict:  # type: ignore[type-arg]
    """Collect the non-NULL columns of one group, keyed by config key."""
    return {
        key: getattr(model, column)
        for key, column in columns.items()
        if getattr(model, column) is not None
    }


class FloorCalibrationRepository(BaseRepository):
    """Read and write floor calibration in the active tenant context."""

    async def effective_calibration(self, as_of: datetime) -> FloorCalibrationDTO | None:
        """Return the calibration in force at ``as_of``, or ``None``.

        ``None`` is the ordinary case, not an error: a tenant that has
        never opened the Calibration editor runs on code defaults, and
        ADR-0116 §7 makes an absent row mean exactly that.

        Args:
            as_of: The evaluation instant (timezone-aware).

        Returns:
            The applicable :class:`FloorCalibrationDTO`, or ``None``.

        Raises:
            FloorCalibrationInvalid: If ``as_of`` is naive.
        """
        _require_aware(as_of, field="as_of")
        result = await self._session.execute(
            select(FloorCalibration)
            .where(FloorCalibration.effective_from <= as_of)
            .order_by(FloorCalibration.effective_from.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_revisions(self) -> list[FloorCalibrationDTO]:
        """Return every revision for the active tenant, oldest first."""
        result = await self._session.execute(
            select(FloorCalibration).order_by(FloorCalibration.effective_from)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def newest_revision(self) -> FloorCalibrationDTO | None:
        """Return the most recent revision regardless of its start instant."""
        result = await self._session.execute(
            select(FloorCalibration).order_by(FloorCalibration.effective_from.desc()).limit(1)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def save_revision(
        self,
        *,
        effective_from: datetime,
        warn_default_pct: Decimal | None = None,
        band_boundaries: tuple[int, int] | None = None,
        options_min_band: str | None = None,
        floor: Mapping[str, int] | None = None,
        cap: Mapping[str, int] | None = None,
        re_trigger_delta: Mapping[str, Decimal] | None = None,
        notes: str | None = None,
    ) -> FloorCalibrationDTO:
        """Persist one calibration revision from its **deviations**.

        Every argument is an override: absent or ``None`` stores NULL,
        which the reader interprets as "code default". Reducing a desired
        configuration to its deviations — and checking that the result is
        a configuration the beat could actually run — belongs to
        :func:`services.watch_desk.calibration.save_calibration_revision`,
        which is the sanctioned write path and calls this method last.

        Args:
            effective_from: When the revision takes effect (timezone-aware,
                strictly after the newest existing revision).
            warn_default_pct: Tenant-wide WARN default override.
            band_boundaries: The two urgency cut points ``(b0, b1)``.
            options_min_band: Options-gate override.
            floor: Trigger type → floor overrides. ``fund_closure`` is
                refused: it is pinned and has no column.
            cap: Source or trigger → cap overrides. Same refusal.
            re_trigger_delta: Subject family → delta overrides.
            notes: Optional annotation.

        Returns:
            The persisted :class:`FloorCalibrationDTO`.

        Raises:
            FloorCalibrationInvalid: On a naive or non-advancing
                ``effective_from``, an unknown key, or a ``fund_closure``
                key.
        """
        _require_aware(effective_from, field="effective_from")
        floors = dict(floor or {})
        caps = dict(cap or {})
        deltas = dict(re_trigger_delta or {})

        require_known_calibration_keys(floor=floors, cap=caps, re_trigger_delta=deltas)

        newest = await self.newest_revision()
        if newest is not None and effective_from <= newest.effective_from:
            raise FloorCalibrationInvalid(
                f"effective_from {effective_from.isoformat()} does not advance past "
                f"the newest revision's {newest.effective_from.isoformat()}. "
                "Revisions are immutable: a new one takes effect later, it never "
                "rewrites what was in force.",
                field="effective_from",
            )

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        columns: dict[str, object] = {"warn_default_pct": warn_default_pct}
        for key, column in FLOOR_COLUMNS.items():
            columns[column] = floors.get(key)
        for key, column in CAP_COLUMNS.items():
            columns[column] = caps.get(key)
        for key, column in DELTA_COLUMNS.items():
            columns[column] = deltas.get(key)
        columns["band_boundary_0"] = band_boundaries[0] if band_boundaries else None
        columns["band_boundary_1"] = band_boundaries[1] if band_boundaries else None
        columns["options_min_band"] = options_min_band

        model = FloorCalibration(
            tenant_id=active_tenant,
            effective_from=effective_from,
            notes=notes,
            **columns,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)


def require_known_calibration_keys(
    *,
    floor: Mapping[str, object] | None = None,
    cap: Mapping[str, object] | None = None,
    re_trigger_delta: Mapping[str, object] | None = None,
) -> None:
    """Assert every supplied key has a column, before anything else runs.

    Public because the validating write path
    (:func:`services.watch_desk.calibration.save_calibration_revision`)
    calls it *first*: composing an unknown key over the defaults would
    fail with a confusing "trigger has a floor but no cap" message
    instead of naming the typo. The repository calls it too, so the
    guarantee holds however the storage layer is reached.

    Args:
        floor: Supplied trigger-type → floor overrides.
        cap: Supplied source/trigger → cap overrides.
        re_trigger_delta: Supplied family → delta overrides.

    Raises:
        FloorCalibrationInvalid: On an unknown key, or on ``fund_closure``
            — refused with its own reason, since it is pinned rather than
            merely unknown.
    """
    _require_known_keys(floor or {}, FLOOR_COLUMNS, kind="floor")
    _require_known_keys(cap or {}, CAP_COLUMNS, kind="cap")
    _require_known_keys(re_trigger_delta or {}, DELTA_COLUMNS, kind="re_trigger_delta")


def _require_known_keys(
    supplied: Mapping[str, object], columns: Mapping[str, str], *, kind: str
) -> None:
    """Reject unknown keys, and ``fund_closure`` with its own reason."""
    for key in supplied:
        if key in columns:
            continue
        if key == _PINNED_KEY:
            raise FloorCalibrationInvalid(
                "fund_closure is a pinned level (floor = cap = 10), not "
                "calibration, and has no column to be stored in. It is not a "
                "tenant knob under any framing (ADR-0116 §7).",
                field=f"{kind}_{_PINNED_KEY}",
            )
        raise FloorCalibrationInvalid(
            f"Unknown {kind} key {key!r}; expected one of {sorted(columns)}.",
            field=kind,
        )


def _require_aware(value: datetime, *, field: str) -> None:
    """Reject a naive instant against a TIMESTAMPTZ column."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise FloorCalibrationInvalid(
            f"{field} must be timezone-aware; the column is TIMESTAMPTZ and a "
            "naive instant has no defined position on it.",
            field=field,
        )
