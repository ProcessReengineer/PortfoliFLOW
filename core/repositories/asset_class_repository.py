# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AssetClassRepository — persistence for the per-tenant asset-class catalogue.

Backs the ``asset_classes`` table introduced in migration b005 (per
ADR-0042 §1). The shape mirrors the other Phase-2/3 repositories:
a tenant-scoped :class:`AsyncSession` is passed in, methods return
frozen DTOs, ``tenant_id`` is implicit in the session context (RLS
WITH CHECK derives it from ``app.tenant_id``).

Phase 3 is repository-flavoured: this module deliberately does not
expose ORM ``relationship()`` traversals to ``SAAAssetClassInput`` or
``SAACorrelation``. Cross-table reads are orchestrated in
``services/saa/saa_service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text, update

from core.models.asset_class import AssetClass
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class AssetClassDTO:
    """Plain data-only view of an ``asset_classes`` row."""

    id: UUID
    tenant_id: UUID
    code: str
    display_name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


def _to_dto(model: AssetClass) -> AssetClassDTO:
    return AssetClassDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        code=model.code,
        display_name=model.display_name,
        description=model.description,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AssetClassRepository(BaseRepository):
    """Read and write asset-class definitions in the active tenant context."""

    async def create(
        self,
        code: str,
        display_name: str,
        description: str | None = None,
    ) -> AssetClassDTO:
        """Create a new asset class in the current tenant context.

        ``tenant_id`` is read from ``app.tenant_id`` so the session
        context is the single source of truth for tenant binding;
        RLS WITH CHECK re-validates the value as defence in depth
        (per ADR-0035 §6).

        Args:
            code: Short, tenant-unique identifier (e.g. ``"global_equity"``).
            display_name: Human-readable label rendered in the SAA UI.
            description: Optional longer description.

        Returns:
            The newly created :class:`AssetClassDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = AssetClass(
            tenant_id=active_tenant,
            code=code,
            display_name=display_name,
            description=description,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def get_by_id(self, asset_class_id: UUID) -> AssetClassDTO | None:
        """Return the asset class with the given id, or ``None`` if absent.

        Cross-tenant rows are invisible (RLS hides them); the
        repository correctly reports absence rather than raising.
        """
        result = await self._session.execute(
            select(AssetClass).where(AssetClass.id == asset_class_id)
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_by_code(self, code: str) -> AssetClassDTO | None:
        """Resolve an asset class by its tenant-scoped code, or ``None``.

        Lookup is **case-insensitive** and trims surrounding whitespace.
        Excel-import inputs (sub-stream 4c, ADR-0043 §4) frequently
        carry inconsistent casing (``"Private Equity"`` vs
        ``"private_equity"``) so the resolver normalises both sides
        before comparing. Codes are still stored in their original
        casing — the comparison is one-directional.

        Args:
            code: The tenant-scoped asset-class code (e.g.
                ``"private_equity"``, ``"unclassified"``). Empty input
                always misses (returns ``None``).

        Returns:
            The matching :class:`AssetClassDTO` if found in the active
            tenant, otherwise ``None``.
        """
        if not isinstance(code, str):
            return None
        normalised = code.strip()
        if not normalised:
            return None
        result = await self._session.execute(
            select(AssetClass).where(func.lower(AssetClass.code) == normalised.lower())
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[AssetClassDTO]:
        """Return every asset class visible in the current tenant context.

        Sorted by ``display_name`` for stable rendering in the SAA UI.
        """
        result = await self._session.execute(select(AssetClass).order_by(AssetClass.display_name))
        return [_to_dto(model) for model in result.scalars().all()]

    async def update(
        self,
        asset_class_id: UUID,
        *,
        display_name: str | None = None,
        description: str | None = None,
    ) -> AssetClassDTO:
        """Update mutable fields on an asset class.

        Only fields whose argument is not ``None`` are modified. The
        ``code`` field is intentionally not updatable through this
        method — codes are referenced from configurations and changing
        them mid-stream would be confusing; create a new asset class
        and migrate the references instead.

        Args:
            asset_class_id: The asset class to update.
            display_name: New display name (keeps the existing value if
                ``None``).
            description: New description (keeps the existing value if
                ``None``).

        Returns:
            The updated :class:`AssetClassDTO`.

        Raises:
            ValueError: If no asset class with this id exists in the
                active tenant context.
        """
        values: dict[str, object] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if description is not None:
            values["description"] = description
        if values:
            values["updated_at"] = text("NOW()")
            await self._session.execute(
                update(AssetClass).where(AssetClass.id == asset_class_id).values(**values)
            )
            await self._session.flush()

        refreshed = await self.get_by_id(asset_class_id)
        if refreshed is None:
            raise ValueError(f"AssetClass {asset_class_id} not found in active tenant.")
        return refreshed

    async def delete(self, asset_class_id: UUID) -> None:
        """Hard-delete an asset class.

        The b005 foreign keys ``saa_asset_class_inputs.asset_class_id``
        and ``saa_correlations.asset_class_*_id`` use ``ON DELETE
        RESTRICT``; deleting an asset class that is referenced by any
        configuration raises an :class:`IntegrityError` at flush time.
        Callers must remove references first.
        """
        from sqlalchemy import delete as sa_delete

        await self._session.execute(sa_delete(AssetClass).where(AssetClass.id == asset_class_id))
        await self._session.flush()
