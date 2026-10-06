# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""WatchFamilyScopeRepository — persistence for the per-family switch.

Backs the ``watch_family_scope`` table introduced in migration b036 (per
ADR-0133 §2). Shape mirrors the other tenant-scoped repositories:
tenant-scoped :class:`AsyncSession`, a frozen DTO, ``tenant_id`` taken
from the session context (RLS WITH CHECK derives it from
``app.tenant_id``).

Revisions are immutable (the ADR-0116 §1 model): a change inserts a
later revision and never rewrites one. The advancing rule is **per
family** — another family may share an instant, because a preset is
applied family by family in one act.

:meth:`WatchFamilyScopeRepository.effective_revisions` returns only the
families with a revision in force. The rule "no row = on" belongs to the
resolution, not to storage, so a family without a revision is simply
absent here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text

from core.exceptions import WatchFamilyScopeInvalid
from core.models.watch_family_scope import (
    PRESETLESS_FAMILIES,
    SCOPE_PRESETS,
    WATCHPOINT_FAMILIES,
    WatchFamilyScope,
)
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class WatchFamilyScopeDTO:
    """Plain data-only view of a ``watch_family_scope`` row."""

    id: UUID
    tenant_id: UUID
    family: str
    effective_from: datetime
    enabled: bool
    preset: str | None
    changed_by: UUID
    note: str | None
    created_at: datetime


def _to_dto(model: WatchFamilyScope) -> WatchFamilyScopeDTO:
    return WatchFamilyScopeDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        family=model.family,
        effective_from=model.effective_from,
        enabled=model.enabled,
        preset=model.preset,
        changed_by=model.changed_by,
        note=model.note,
        created_at=model.created_at,
    )


def _family_order(family: str) -> int:
    return WATCHPOINT_FAMILIES.index(family)


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WatchFamilyScopeInvalid(
            f"{field} must be timezone-aware; the column is TIMESTAMPTZ and "
            "a naive instant has no defined position on it.",
            field=field,
        )


def _require_family(family: str) -> None:
    if family not in WATCHPOINT_FAMILIES:
        raise WatchFamilyScopeInvalid(
            f"Unknown watchpoint family {family!r}; expected one of {list(WATCHPOINT_FAMILIES)}.",
            field="family",
        )


class WatchFamilyScopeRepository(BaseRepository):
    """Insert and read family-scope revisions in the active tenant context."""

    async def insert_revision(
        self,
        *,
        family: str,
        effective_from: datetime,
        enabled: bool,
        changed_by: UUID,
        preset: str | None = None,
        note: str | None = None,
    ) -> WatchFamilyScopeDTO:
        """Insert one revision of one family's switch; never updates.

        Args:
            family: One of the seven watchpoint families.
            effective_from: When the revision takes effect (timezone-aware);
                strictly after the family's newest revision.
            enabled: Whether Irene watches the family from then on.
            changed_by: The acting user.
            preset: The sensitivity preset this revision applied, if any —
                audit only. Never on a preset-less family (``rss``).
            note: Optional free text.

        Returns:
            The newly created :class:`WatchFamilyScopeDTO`.

        Raises:
            WatchFamilyScopeInvalid: If ``family`` is unknown,
                ``effective_from`` is naive or does not advance past the
                family's newest revision, or ``preset`` is outside the
                vocabulary or set on a preset-less family.
        """
        _require_family(family)
        _require_aware(effective_from, field="effective_from")
        if preset is not None and preset not in SCOPE_PRESETS:
            raise WatchFamilyScopeInvalid(
                f"Invalid preset {preset!r}; expected one of {list(SCOPE_PRESETS)} or none.",
                field="preset",
            )
        if preset is not None and family in PRESETLESS_FAMILIES:
            raise WatchFamilyScopeInvalid(
                f"The {family!r} family carries no preset; got {preset!r}.",
                field="preset",
            )

        newest = await self._session.execute(
            select(func.max(WatchFamilyScope.effective_from)).where(
                WatchFamilyScope.family == family
            )
        )
        newest_from: datetime | None = newest.scalar_one()
        if newest_from is not None and effective_from <= newest_from:
            raise WatchFamilyScopeInvalid(
                f"effective_from {effective_from.isoformat()} does not advance past "
                f"the newest {family!r} revision's {newest_from.isoformat()}. "
                "Revisions are immutable: a new one takes effect later, it never "
                "rewrites what was in force.",
                field="effective_from",
            )

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = WatchFamilyScope(
            tenant_id=active_tenant,
            family=family,
            effective_from=effective_from,
            enabled=enabled,
            preset=preset,
            changed_by=changed_by,
            note=note,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def effective_revisions(self, as_of: datetime) -> list[WatchFamilyScopeDTO]:
        """Return, per family, the revision in force at ``as_of``.

        For each family the row with the largest ``effective_from <= as_of``
        (the ``effective_watchpoints`` idiom). A family without a revision
        in force is **absent**: the "no row = on" default is the
        resolution's rule, not storage's.

        Args:
            as_of: The evaluation instant (timezone-aware).

        Returns:
            The revisions in force, in ``WATCHPOINT_FAMILIES`` order.

        Raises:
            WatchFamilyScopeInvalid: If ``as_of`` is naive.
        """
        _require_aware(as_of, field="as_of")
        result = await self._session.execute(
            select(WatchFamilyScope)
            .where(WatchFamilyScope.effective_from <= as_of)
            .distinct(WatchFamilyScope.family)
            .order_by(WatchFamilyScope.family, WatchFamilyScope.effective_from.desc())
        )
        rows = [_to_dto(model) for model in result.scalars().all()]
        return sorted(rows, key=lambda row: _family_order(row.family))

    async def list_revisions(self, *, family: str | None = None) -> list[WatchFamilyScopeDTO]:
        """Return every revision, oldest first.

        Args:
            family: Optional family filter.

        Returns:
            The revisions in the active tenant context, ordered by
            ``effective_from`` ascending, then by family order.

        Raises:
            WatchFamilyScopeInvalid: If ``family`` is not a known family.
        """
        stmt = select(WatchFamilyScope)
        if family is not None:
            _require_family(family)
            stmt = stmt.where(WatchFamilyScope.family == family)
        result = await self._session.execute(stmt)
        rows = [_to_dto(model) for model in result.scalars().all()]
        return sorted(rows, key=lambda row: (row.effective_from, _family_order(row.family)))
