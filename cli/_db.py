# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Superuser database connection helper for the CLI.

This module is the **single source of truth** for reading the
``DATABASE_URL_SUPERUSER`` environment variable. No other code path in
PortfoliFLOW is permitted to read this variable — application code
(FastAPI web app, Telegram bot) connects exclusively through
the unprivileged ``portfoliflow_app`` role using ``DATABASE_URL`` (see
ADR-0040 §2).

Loading ``.env`` here mirrors the discipline in
``core/config.py``: callers do not need a ``.env`` to be exported in
their shell, and the variable is read lazily on the first
``superuser_engine()`` call.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.exceptions import ConfigurationError

_ENV_PATH: Path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_PATH)


def superuser_engine() -> AsyncEngine:
    """Construct an :class:`AsyncEngine` from ``DATABASE_URL_SUPERUSER``.

    The engine is the caller's responsibility to dispose of (typically
    via ``await engine.dispose()`` in a ``finally`` block).

    Returns:
        A configured async engine bound to the Postgres superuser
        connection URL.

    Raises:
        ConfigurationError: If ``DATABASE_URL_SUPERUSER`` is not set.
    """
    url = os.getenv("DATABASE_URL_SUPERUSER")
    if not url:
        raise ConfigurationError(
            "DATABASE_URL_SUPERUSER is not set. Configure it in .env "
            "(see .env.example) before running the CLI."
        )
    return create_async_engine(url, future=True)
