# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""RegionRepository — per-tenant region catalogue + country memberships.

Backs the ``regions`` and ``region_country_memberships`` tables
introduced in migration b009 (per ADR-0046). The repository exposes a
read-only surface for the Excel-import path (lookup by display name)
and the aggregation path (list-all sorted by ``sort_order``); writes
are owned by the bootstrap step in :mod:`cli.bootstrap`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text

from core.models.region import Region
from core.models.region_country_membership import RegionCountryMembership
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class RegionDTO:
    """Plain data-only view of a ``regions`` row."""

    id: UUID
    tenant_id: UUID
    code: str
    display_name: str
    description: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RegionCountryMembershipDTO:
    """Plain data-only view of a ``region_country_memberships`` row."""

    id: UUID
    tenant_id: UUID
    region_id: UUID
    country_iso_code: str
    created_at: datetime


def _to_region_dto(model: Region) -> RegionDTO:
    return RegionDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        code=model.code,
        display_name=model.display_name,
        description=model.description,
        sort_order=model.sort_order,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _to_membership_dto(
    model: RegionCountryMembership,
) -> RegionCountryMembershipDTO:
    return RegionCountryMembershipDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        region_id=model.region_id,
        country_iso_code=model.country_iso_code,
        created_at=model.created_at,
    )


class RegionRepository(BaseRepository):
    """Read and write region definitions in the active tenant context."""

    async def create(
        self,
        code: str,
        display_name: str,
        *,
        description: str | None = None,
        sort_order: int = 0,
    ) -> RegionDTO:
        """Create a new region in the current tenant context.

        Args:
            code: Short, tenant-unique identifier (e.g. ``"dach"``).
            display_name: Human-readable label (e.g. ``"DACH"``).
            description: Optional longer description.
            sort_order: Display order for UI rendering. Lower values
                appear first.

        Returns:
            The newly created :class:`RegionDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = Region(
            tenant_id=active_tenant,
            code=code,
            display_name=display_name,
            description=description,
            sort_order=sort_order,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_region_dto(model)

    async def get_by_id(self, region_id: UUID) -> RegionDTO | None:
        """Return the region with the given id, or ``None`` if absent."""
        result = await self._session.execute(select(Region).where(Region.id == region_id))
        model = result.scalar_one_or_none()
        return _to_region_dto(model) if model is not None else None

    async def get_by_code(self, code: str) -> RegionDTO | None:
        """Resolve a region by its tenant-scoped code, or ``None``."""
        if not isinstance(code, str):
            return None
        normalised = code.strip()
        if not normalised:
            return None
        result = await self._session.execute(
            select(Region).where(func.lower(Region.code) == normalised.lower())
        )
        model = result.scalar_one_or_none()
        return _to_region_dto(model) if model is not None else None

    async def list_all(self) -> list[RegionDTO]:
        """Return every region visible in the current tenant.

        Sorted by ``sort_order`` ascending then by ``display_name`` so
        the ordering is stable when ``sort_order`` collisions occur.
        """
        result = await self._session.execute(
            select(Region).order_by(Region.sort_order, Region.display_name)
        )
        return [_to_region_dto(model) for model in result.scalars().all()]

    async def list_memberships_by_region(self, region_id: UUID) -> list[RegionCountryMembershipDTO]:
        """Return every country membership for a region.

        Args:
            region_id: The region whose ISO-country members to load.

        Returns:
            Memberships sorted by ``country_iso_code`` for stable
            rendering. Empty list for an unknown region.
        """
        result = await self._session.execute(
            select(RegionCountryMembership)
            .where(RegionCountryMembership.region_id == region_id)
            .order_by(RegionCountryMembership.country_iso_code.asc())
        )
        return [_to_membership_dto(m) for m in result.scalars().all()]

    async def add_membership(
        self, region_id: UUID, country_iso_code: str
    ) -> RegionCountryMembershipDTO:
        """Attach an ISO country to a region in the active tenant.

        The strict-partition invariant is enforced at the DB level by
        ``uq_region_country_memberships_tenant_iso_unique``: a country
        already attached to another region in the tenant raises
        :class:`IntegrityError` at flush time.

        Args:
            region_id: The region to attach the country to.
            country_iso_code: ISO 3166-1 alpha-2 code. Upper-cased
                before persistence.

        Returns:
            The newly created :class:`RegionCountryMembershipDTO`.
        """
        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = RegionCountryMembership(
            tenant_id=active_tenant,
            region_id=region_id,
            country_iso_code=country_iso_code.strip().upper(),
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_membership_dto(model)
