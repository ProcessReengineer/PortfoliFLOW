# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FastAPI dependency wiring for PortfoliFLOW.

Sub-stream 2a only ships the engine accessor. The full
tenant-scoped session dependency (which sets ``app.tenant_id`` and
``app.user_id`` per ADR-0035 and ADR-0036) lands in sub-stream 2b
alongside the auth middleware that supplies the user identity.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine


def get_engine(request: Request) -> AsyncEngine:
    """Return the application-wide async engine attached at startup.

    The lifespan context (``web/main.py``) builds the engine from
    ``DATABASE_URL`` once on startup and disposes it on shutdown.
    Routes that need a session acquire it from this engine via the
    repository layer's ``tenant_context`` (sub-stream 2b).
    """
    return request.app.state.engine
