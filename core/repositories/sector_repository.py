# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SectorRepository — persistence for the per-tenant sector catalogue.

Backs the ``sectors`` table introduced in migration b007 (per
ADR-0045 §2). Mirrors the :class:`AssetClassRepository` shape: a
tenant-scoped :class:`AsyncSession` is passed in, methods return
frozen DTOs, ``tenant_id`` is implicit in the session context (RLS
WITH CHECK derives it from ``app.tenant_id``).

Per-tenant ``unclassified`` rows are installed by
``portfoliflow bootstrap`` (not by this module).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete, func, select, text, update

from core.models.sector import Sector
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class SectorDTO:
    """Plain data-only view of a ``sectors`` row."""

    id: UUID
    tenant_id: UUID
    code: str
    display_name: str
    is_active: bool
    created_by: UUID
    created_at: datetime
    updated_at: datetime


def _to_dto(model: Sector) -> SectorDTO:
    return SectorDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        code=model.code,
        display_name=model.display_name,
        is_active=model.is_active,
        created_by=model.created_by,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SectorRepository(BaseRepository):
    """Read and write sector definitions in the active tenant context."""

    async def create(
        self,
        code: str,
        display_name: str,
        created_by: UUID,
        *,
        is_active: bool = True,
    ) -> SectorDTO:
        """Create a new sector in the current tenant context.

        ``tenant_id`` is read from ``app.tenant_id`` so the session
        context is the single source of truth for tenant binding;
        RLS WITH CHECK re-validates the value as defence in depth
        (per ADR-0035 §6).

        Args:
            code: Short, tenant-unique identifier (e.g.
                ``"tech_software"``).
            display_name: Human-readable label.
            created_by: UUID of the user creating the sector.
            is_active: Active flag. Defaults to ``True``.

        Returns:
            The newly created :class:`SectorDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = Sector(
            tenant_id=active_tenant,
            code=code,
            display_name=display_name,
            is_active=is_active,
            created_by=created_by,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def get_by_id(self, sector_id: UUID) -> SectorDTO | None:
        """Return the sector with the given id, or ``None`` if absent.

        Cross-tenant rows are invisible (RLS hides them); the
        repository correctly reports absence rather than raising.
        """
        result = await self._session.execute(select(Sector).where(Sector.id == sector_id))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_by_code(self, code: str) -> SectorDTO | None:
        """Resolve a sector by its tenant-scoped code, or ``None``.

        Lookup is case-insensitive and trims surrounding whitespace
        — matching the :class:`AssetClassRepository.get_by_code`
        convention so the Excel-import path can pass raw cell values.

        Args:
            code: The tenant-scoped sector code (e.g.
                ``"tech_software"``, ``"unclassified"``). Empty
                input always misses (returns ``None``).

        Returns:
            The matching :class:`SectorDTO` if found in the active
            tenant, otherwise ``None``.
        """
        if not isinstance(code, str):
            return None
        normalised = code.strip()
        if not normalised:
            return None
        result = await self._session.execute(
            select(Sector).where(func.lower(Sector.code) == normalised.lower())
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[SectorDTO]:
        """Return every sector visible in the current tenant.

        Sorted by ``display_name`` for stable rendering in UI
        dropdowns.
        """
        result = await self._session.execute(select(Sector).order_by(Sector.display_name))
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_active(self) -> list[SectorDTO]:
        """Return only sectors where ``is_active = TRUE``."""
        result = await self._session.execute(
            select(Sector).where(Sector.is_active.is_(True)).order_by(Sector.display_name)
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def update(
        self,
        sector_id: UUID,
        *,
        display_name: str | None = None,
    ) -> SectorDTO:
        """Update mutable fields on a sector.

        Only fields whose argument is not ``None`` are modified.
        ``code`` is intentionally not updatable through this method —
        codes are referenced from weight rows; create a new sector
        and migrate the references instead.

        Args:
            sector_id: The sector to update.
            display_name: New display name (keeps existing if ``None``).

        Returns:
            The refreshed :class:`SectorDTO`.

        Raises:
            ValueError: If no sector with this id exists in the active
                tenant context.
        """
        values: dict[str, object] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if values:
            values["updated_at"] = text("NOW()")
            await self._session.execute(
                update(Sector).where(Sector.id == sector_id).values(**values)
            )
            await self._session.flush()

        refreshed = await self.get_by_id(sector_id)
        if refreshed is None:
            raise ValueError(f"Sector {sector_id} not found in active tenant.")
        return refreshed

    async def deactivate(self, sector_id: UUID) -> None:
        """Toggle the active flag to ``FALSE`` on a sector.

        Used in place of hard-delete when weight rows still reference
        the sector — the FK uses ``ON DELETE RESTRICT``, so
        ``deactivate`` is the operationally safer path.
        """
        await self._session.execute(
            update(Sector)
            .where(Sector.id == sector_id)
            .values(is_active=False, updated_at=text("NOW()"))
        )
        await self._session.flush()

    async def delete(self, sector_id: UUID) -> None:
        """Hard-delete a sector.

        Raises ``IntegrityError`` at flush time if the sector is
        still referenced by ``investment_sector_weights`` rows
        (FK ``ON DELETE RESTRICT``).
        """
        await self._session.execute(sa_delete(Sector).where(Sector.id == sector_id))
        await self._session.flush()
