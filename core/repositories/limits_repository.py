# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""LimitsRepository — persistence for limit sets and per-class ceilings.

Backs the ``limit_sets`` and ``limits`` tables introduced in
migration b010 (per ADR-0056 §Schema). The repository exposes a
single transactional writer (``create_set_with_limits``) and a
small set of read methods used by the engine and the future limit-
set browser UI.

A limit set, once persisted, is immutable. The b001 audit trigger
captures any future operator-driven label/notes edits (Phase-V2 UI);
the per-class ceilings are never modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text

from core.models.limit import Limit
from core.models.limit_set import LimitSet
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class LimitSetDTO:
    """Plain data-only view of a ``limit_sets`` row."""

    id: UUID
    tenant_id: UUID
    family: str
    effective_from: date
    label: str
    notes: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class LimitDTO:
    """Plain data-only view of a ``limits`` row."""

    id: UUID
    tenant_id: UUID
    limit_set_id: UUID
    class_key: str
    max_pct: Decimal
    created_at: datetime
    updated_at: datetime


def _set_to_dto(model: LimitSet) -> LimitSetDTO:
    return LimitSetDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        family=model.family,
        effective_from=model.effective_from,
        label=model.label,
        notes=model.notes,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _limit_to_dto(model: Limit) -> LimitDTO:
    return LimitDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        limit_set_id=model.limit_set_id,
        class_key=model.class_key,
        max_pct=model.max_pct,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class LimitsRepository(BaseRepository):
    """Read and write limit sets in the active tenant context."""

    async def create_set_with_limits(
        self,
        *,
        family: str,
        effective_from: date,
        label: str,
        notes: str | None,
        limits: dict[str, Decimal],
        created_by: UUID,
    ) -> LimitSetDTO:
        """Persist a new limit set together with all its class ceilings.

        Single API surface for the importer. The operation is
        transactional inside the caller's session: either all rows
        commit or none do. The unique constraint
        ``(tenant_id, family, effective_from)`` raises
        :class:`sqlalchemy.exc.IntegrityError` on conflict, which
        the importer translates to
        :class:`core.exceptions.LimitValidationError` per ADR-0056
        §Immutability.

        Args:
            family: One of ``'saa'`` or ``'anlv'`` (CHECK-constrained).
            effective_from: The first calendar day this set is in
                force.
            label: Operator-readable label.
            notes: Optional free-text annotation.
            limits: Mapping ``class_key -> max_pct`` (percentage
                points, e.g. ``Decimal("30.0")``). Must be non-empty;
                the schema accepts an empty set but the importer
                rejects it via sum-to-100 validation.
            created_by: UUID of the user persisting these rows.

        Returns:
            The newly created :class:`LimitSetDTO`. Per-class rows
            can be fetched via :meth:`list_limits`.

        Raises:
            ValueError: If ``limits`` is empty.
            sqlalchemy.exc.IntegrityError: On UNIQUE conflict or
                CHECK constraint violation (importer should translate).
        """
        if not limits:
            raise ValueError("create_set_with_limits requires at least one class entry")

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        set_model = LimitSet(
            tenant_id=active_tenant,
            family=family,
            effective_from=effective_from,
            label=label,
            notes=notes,
            created_by=created_by,
        )
        self._session.add(set_model)
        await self._session.flush()
        await self._session.refresh(set_model)

        for class_key, max_pct in limits.items():
            self._session.add(
                Limit(
                    tenant_id=active_tenant,
                    limit_set_id=set_model.id,
                    class_key=class_key,
                    max_pct=max_pct,
                )
            )
        await self._session.flush()
        return _set_to_dto(set_model)

    async def get_set_by_id(self, set_id: UUID) -> LimitSetDTO | None:
        """Return the limit set with the given id, or ``None`` if absent.

        Cross-tenant rows are invisible (RLS hides them); the
        repository correctly reports absence rather than raising. Used
        by the history-browser web route to fetch a set's metadata
        before its per-class ceilings.
        """
        result = await self._session.execute(select(LimitSet).where(LimitSet.id == set_id))
        model = result.scalar_one_or_none()
        return _set_to_dto(model) if model is not None else None

    async def get_effective_set(self, family: str, as_of_date: date) -> LimitSetDTO | None:
        """Return the set in force for ``(family, as_of_date)``, or ``None``.

        Implements ADR-0056 §Selection: pick the row with the largest
        ``effective_from <= as_of_date`` for the given family in the
        active tenant context.

        Args:
            family: One of ``'saa'`` or ``'anlv'``.
            as_of_date: The evaluation date.

        Returns:
            The applicable :class:`LimitSetDTO`, or ``None`` if no set
            has yet taken effect on or before this date.
        """
        result = await self._session.execute(
            select(LimitSet)
            .where(LimitSet.family == family)
            .where(LimitSet.effective_from <= as_of_date)
            .order_by(LimitSet.effective_from.desc())
            .limit(1)
        )
        model = result.scalar_one_or_none()
        return _set_to_dto(model) if model is not None else None

    async def list_sets(self, family: str | None = None) -> list[LimitSetDTO]:
        """Return every limit set visible in the active tenant context.

        Args:
            family: Optional filter; if provided, restricts to one
                family (``'saa'`` or ``'anlv'``).

        Returns:
            All matching limit sets, ordered by ``family`` then by
            ``effective_from`` (oldest first).
        """
        stmt = select(LimitSet)
        if family is not None:
            stmt = stmt.where(LimitSet.family == family)
        stmt = stmt.order_by(LimitSet.family, LimitSet.effective_from)
        result = await self._session.execute(stmt)
        return [_set_to_dto(model) for model in result.scalars().all()]

    async def list_limits(self, set_id: UUID) -> list[LimitDTO]:
        """Return every per-class ceiling belonging to one limit set.

        Args:
            set_id: The ``limit_sets.id`` to fetch ceilings for.

        Returns:
            All :class:`LimitDTO` rows for the given set, ordered by
            ``class_key`` for deterministic display.
        """
        result = await self._session.execute(
            select(Limit).where(Limit.limit_set_id == set_id).order_by(Limit.class_key)
        )
        return [_limit_to_dto(model) for model in result.scalars().all()]
