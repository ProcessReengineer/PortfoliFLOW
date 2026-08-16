# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AnlVCategoryRepository — read-only access to the global AnlV stammtabelle.

Backs the ``anlv_categories`` table introduced in migration b010
(per ADR-0057 §Schema). Same shape as ``CountryRepository``: the
table is global, RLS-free, and the repository exposes read methods
only. Updates to the AnlV catalogue come exclusively through
migrations — the application has no write path because the AnlV is a
federal regulation, not operator-curated content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from core.models.anlv_category import AnlVCategory
from core.repositories.base import BaseRepository


@dataclass(frozen=True)
class AnlVCategoryDTO:
    """Plain data-only view of an ``anlv_categories`` row."""

    code: str
    paragraph_label: str
    display_name: str
    description: str | None
    sort_order: int
    created_at: datetime
    updated_at: datetime


def _to_dto(model: AnlVCategory) -> AnlVCategoryDTO:
    return AnlVCategoryDTO(
        code=model.code,
        paragraph_label=model.paragraph_label,
        display_name=model.display_name,
        description=model.description,
        sort_order=model.sort_order,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class AnlVCategoryRepository(BaseRepository):
    """Read-only repository over the global ``anlv_categories`` lookup table."""

    async def list_all(self) -> list[AnlVCategoryDTO]:
        """Return every AnlV category, sorted by ``sort_order``.

        Returns:
            All numbered AnlV categories in deterministic catalogue
            order (driven by the ``sort_order`` column populated from
            the JSON fixture).
        """
        result = await self._session.execute(select(AnlVCategory).order_by(AnlVCategory.sort_order))
        return [_to_dto(model) for model in result.scalars().all()]

    async def get_by_code(self, code: str) -> AnlVCategoryDTO | None:
        """Resolve a category by its snake_case code, or ``None``.

        Lookup is case-insensitive on the input. Empty / whitespace-
        only input returns ``None``.

        Args:
            code: The catalogue code (e.g. ``"anlv_13"``).

        Returns:
            The matching :class:`AnlVCategoryDTO`, or ``None`` if no
            row exists.
        """
        if not isinstance(code, str):
            return None
        cleaned = code.strip()
        if not cleaned:
            return None
        result = await self._session.execute(
            select(AnlVCategory).where(func.lower(AnlVCategory.code) == cleaned.lower())
        )
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_codes(self) -> set[str]:
        """Return the set of all known AnlV codes.

        Used by the Excel-import extractor to validate Excel cells in
        a single round trip rather than one :meth:`get_by_code` per
        row.

        Returns:
            A set of every ``code`` present in the table.
        """
        result = await self._session.execute(select(AnlVCategory.code))
        return {row[0] for row in result.all()}
