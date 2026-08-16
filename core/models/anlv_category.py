# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""AnlV-category ORM model — global § 2 Abs. 1 AnlV stammtabelle.

Backs the ``anlv_categories`` table introduced in migration b010
(per ADR-0057 §Schema). Unlike per-tenant catalogues such as
``asset_classes`` or ``sectors``, this table is **global**: it
carries no ``tenant_id`` and is **not** RLS-protected. Every tenant
reads the same numbered AnlV categories.

The ``code`` column is the primary key and matches the snake_case
identifier from the JSON fixture (e.g. ``"anlv_13"``). Updates come
exclusively through migrations — the application has no write path.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class AnlVCategory(Base):
    """One numbered category of § 2 Abs. 1 AnlV."""

    __tablename__ = "anlv_categories"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    paragraph_label: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
