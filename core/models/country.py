# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Country ORM model — global ISO 3166-1 alpha-2 stammtabelle.

Backs the ``countries`` table introduced in migration b007 (per
ADR-0045 §2). Unlike every other domain table, ``countries`` is
**global**: it carries no ``tenant_id`` and is **not** RLS-protected.
Every tenant reads the same set of countries.

The reserved ISO code ``XX`` is the sentinel for unallocated splits
(per ADR-0045 §2). Excel imports route an unrecognised country cell
to ``XX`` rather than failing the row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class Country(Base):
    """One ISO 3166-1 alpha-2 country, plus the ``XX`` sentinel."""

    __tablename__ = "countries"

    iso_code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    region_default: Mapped[str] = mapped_column(Text, nullable=False)
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
