# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow create-super-admin`` — create a super-admin user.

Per ADR-0064 §3. Delegates to
:func:`services.super_admin.create_super_admin_idempotent`. On the
bootstrap path (no existing super-admin to attribute) the audit row
records a NULL actor — see migration b014.

External behaviour preserved: same flags, same exit codes, same log
lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import typer
from sqlalchemy import text

from cli._db import superuser_engine
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging
from core.tenant_constants import SYSTEM_TENANT_ID
from services.auth.password_policy import validate_password_strength
from services.super_admin import (
    EmailInvalidError,
    SuperAdminOperationError,
    create_super_admin_idempotent,
)

_LOG = logging.getLogger("portfoliflow.cli")


async def _ensure_system_tenant_present(conn) -> None:
    """Upsert the system tenant row.

    Bootstrap normally seeds it, but :func:`create_super_admin_command`
    must work even on a fresh DB (it is the first step in
    rolling out a brand new platform). The migration b012 also
    seeds it, so this is a redundant safety net.
    """
    await conn.execute(
        text(
            "INSERT INTO tenants (id, name, subdomain) "
            "VALUES (:id, :n, :s) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": str(SYSTEM_TENANT_ID),
            "n": "Platform Administration",
            "s": "admin",
        },
    )


async def _run(email: str, password: str, display_name: str | None = None) -> None:
    engine = superuser_engine()
    try:
        async with engine.begin() as conn:
            await _ensure_system_tenant_present(conn)
            await create_super_admin_idempotent(
                conn,
                email=email,
                password=password,
                actor_super_admin_id=None,  # bootstrap path
                actor_ip=None,
                actor_user_agent="cli/create-super-admin",
                display_name=display_name,
            )
    finally:
        await engine.dispose()


def create_super_admin_command(
    email: str = typer.Option(..., "--email"),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
    display_name: str | None = typer.Option(
        None, "--display-name", help="Optional human display name"
    ),
) -> None:
    """Create a super-admin user in the system tenant."""
    configure_logging()
    try:
        if password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = os.getenv("SUPER_ADMIN_PASSWORD", "")
        if not password:
            raise ConfigurationError(
                "create-super-admin: --password-stdin or SUPER_ADMIN_PASSWORD required"
            )
        # Enforce the set-time password policy before any DB work: a weak
        # password fails loudly with a non-zero exit (see PortfoliFlowError
        # handler below).
        validate_password_strength(password)
        asyncio.run(_run(email, password, display_name))
    except EmailInvalidError as exc:
        _LOG.error("create-super-admin: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except SuperAdminOperationError as exc:
        _LOG.error("create-super-admin: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except ConfigurationError as exc:
        _LOG.error("create-super-admin: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("create-super-admin: %s", exc.message)
        raise typer.Exit(code=3) from exc
