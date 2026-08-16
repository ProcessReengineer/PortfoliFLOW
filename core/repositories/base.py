# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Base class for all PortfoliFLOW repositories."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Common base class for repositories.

    Subclasses MUST accept an :class:`AsyncSession` in their constructor.
    The session is expected to be tenant-scoped — i.e. acquired via
    :func:`core.repositories._session.tenant_context`. Repositories
    never set ``app.tenant_id`` themselves; they rely on the session
    being correctly scoped by the caller.

    Repository methods return plain dataclasses, never
    :class:`AsyncSession` instances or ORM model instances. The domain
    layer must remain ignorant of SQLAlchemy lifecycle concerns
    (per ADR-0034 Implementation Notes).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
