# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""CountryRepository — read-only access to the global country stammtabelle.

Backs the ``countries`` table introduced in migration b007 (per
ADR-0045 §2). Unlike every other repository in this layer,
``CountryRepository`` is **read-only** and operates against a
**non-tenant-scoped** session. The ``countries`` table has no
``tenant_id`` column and no RLS policy — every tenant reads the same
set of countries — so a session acquired without
:func:`tenant_context` is acceptable here. In practice the repository
is also called from inside a tenant-scoped session for convenience;
the absence of RLS on the table makes the choice immaterial.

The reserved ISO code ``XX`` is the sentinel for unallocated splits.
:meth:`list_active_iso_codes` returns the full set of valid codes
including ``XX`` so the Excel-import path can validate Excel cells in
one round trip.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from core.models.country import Country
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class CountryDTO:
    """Plain data-only view of a ``countries`` row."""

    iso_code: str
    display_name: str
    region_default: str
    created_at: datetime
    updated_at: datetime


def _to_dto(model: Country) -> CountryDTO:
    return CountryDTO(
        iso_code=model.iso_code,
        display_name=model.display_name,
        region_default=model.region_default,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class CountryRepository(BaseRepository):
    """Read-only repository over the global ``countries`` lookup table."""

    async def get_by_iso_code(self, iso_code: str) -> CountryDTO | None:
        """Resolve a country by its ISO 3166-1 alpha-2 code.

        Lookup is **case-insensitive on the input**: the canonical
        code in the table is stored uppercase, so the input is
        upper-cased before comparison. Empty input returns ``None``.

        Args:
            iso_code: Two-letter ISO 3166-1 alpha-2 code (or the
                ``XX`` sentinel).

        Returns:
            The matching :class:`CountryDTO`, or ``None`` if no
            country with this code exists.
        """
        if not isinstance(iso_code, str):
            return None
        cleaned = iso_code.strip().upper()
        if not cleaned:
            return None
        result = await self._session.execute(select(Country).where(Country.iso_code == cleaned))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[CountryDTO]:
        """Return every country, sorted by ``display_name``.

        Returns:
            All ~250 countries plus the ``XX`` sentinel, sorted
            alphabetically by display name for stable rendering in
            UI dropdowns.
        """
        result = await self._session.execute(select(Country).order_by(Country.display_name))
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_active_iso_codes(self) -> set[str]:
        """Return the set of all known ISO codes.

        Used by the Excel-import extractor to validate Excel cells
        in a single round trip rather than one
        :meth:`get_by_iso_code` per row.

        Returns:
            A frozenset-shaped (in semantics) set of every
            ``iso_code`` present in the table, including the ``XX``
            sentinel.
        """
        result = await self._session.execute(select(Country.iso_code))
        return {row[0] for row in result.all()}
