# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SQLAlchemy declarative base for PortfoliFLOW ORM models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common base class for all ORM models.

    Subclasses live under ``core/models/`` and are re-exported from
    ``core.models.__init__`` so Alembic autogenerate sees them.
    """
