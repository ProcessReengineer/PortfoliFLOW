# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Alembic migration environment for PortfoliFLOW.

Reads the connection URL from ``DATABASE_URL_SUPERUSER`` in ``.env``.
Migrations always run as the Postgres superuser because DDL needs
ownership; the application's ``portfoliflow_app`` role does not have
the privileges to apply DDL.

The environment is async-native: ``run_async_migrations`` runs the
SQLAlchemy migration callable inside an asyncio event loop, which
matches the rest of the application stack and avoids spinning up a
synchronous engine just for migrations.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make the project root importable so `core.models` resolves.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env from the project root before reading any environment.
load_dotenv(_PROJECT_ROOT / ".env")

from core.models import Base  # noqa: E402  (must follow sys.path injection)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.getenv("DATABASE_URL_SUPERUSER")
    if not url:
        raise RuntimeError(
            "DATABASE_URL_SUPERUSER is not set. Alembic migrations need the "
            "Postgres superuser URL because DDL requires ownership. Define "
            "DATABASE_URL_SUPERUSER in .env (see .env.example)."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations against a URL only, without an active connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against a live async connection."""
    cfg_section = config.get_section(config.config_ini_section, {})
    cfg_section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
