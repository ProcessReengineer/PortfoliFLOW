# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow create-user`` — create a user in a target tenant.

Per ADR-0064 §3. Delegates to
:func:`services.super_admin.create_user_idempotent` so the CLI shares
the validation / audit pathway with the web admin surface.

External behaviour preserved: same flags, same exit codes, same log
lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from uuid import UUID

import typer
from sqlalchemy import text

from cli._db import superuser_engine
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging
from core.tenant_constants import SYSTEM_TENANT_ID
from services.super_admin import (
    EmailInvalidError,
    RoleInvalidError,
    SuperAdminOperationError,
    TenantNotFoundError,
    create_user_idempotent,
    resolve_tenant_id_by_subdomain,
)

_LOG = logging.getLogger("portfoliflow.cli")

_UUID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


async def _resolve_actor_id(conn) -> UUID | None:
    """Resolve the acting super-admin from ``SUPER_ADMIN_EMAIL``.

    Same convention as :func:`cli.create_tenant._resolve_actor_id`:
    best-effort attribution, ``None`` when the env var is unset or
    does not match a super-admin row.
    """
    email = os.getenv("SUPER_ADMIN_EMAIL", "").strip()
    if not email:
        return None
    result = await conn.execute(
        text(
            "SELECT id FROM users "
            "WHERE tenant_id = :sys AND email = :email "
            "  AND is_super_admin = TRUE"
        ),
        {"sys": str(SYSTEM_TENANT_ID), "email": email},
    )
    row = result.first()
    return UUID(str(row.id)) if row is not None else None


async def _resolve_tenant_arg(conn, tenant_arg: str) -> UUID:
    """Accept either a UUID or a subdomain."""
    if _UUID_RE.match(tenant_arg):
        return UUID(tenant_arg)
    return await resolve_tenant_id_by_subdomain(conn, tenant_arg)


async def _run(
    tenant_arg: str,
    email: str,
    roles: list[str],
    password: str,
    display_name: str | None = None,
) -> None:
    engine = superuser_engine()
    try:
        async with engine.begin() as conn:
            actor_id = await _resolve_actor_id(conn)
            if actor_id is None:
                raise PortfoliFlowError(
                    "create-user: SUPER_ADMIN_EMAIL is unset or does not "
                    "match an existing super-admin. Either:\n"
                    "  (a) Set SUPER_ADMIN_EMAIL in .env to an existing "
                    "super-admin email, or\n"
                    "  (b) Create the first super-admin with:\n"
                    "        echo -n '<password>' | portfoliflow create-super-admin \\\n"
                    "            --email <email> --password-stdin\n"
                    "      then re-run this command."
                )
            tenant_id = await _resolve_tenant_arg(conn, tenant_arg)
            await create_user_idempotent(
                conn,
                tenant_id=tenant_id,
                email=email,
                roles=roles,
                password=password,
                actor_super_admin_id=actor_id,
                actor_ip=None,
                actor_user_agent="cli/create-user",
                display_name=display_name,
            )
    finally:
        await engine.dispose()


def create_user_command(
    tenant: str = typer.Option(..., "--tenant"),
    email: str = typer.Option(..., "--email"),
    roles: str = typer.Option("member", "--roles", help="Comma-separated list"),
    password_stdin: bool = typer.Option(False, "--password-stdin"),
    display_name: str | None = typer.Option(
        None, "--display-name", help="Optional human display name"
    ),
) -> None:
    """Create a user in a target tenant."""
    configure_logging()
    try:
        roles_list = [r.strip() for r in roles.split(",") if r.strip()]
        if password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = os.getenv("USER_PASSWORD", "")
        if not password:
            raise ConfigurationError("create-user: --password-stdin or USER_PASSWORD required")
        asyncio.run(_run(tenant, email, roles_list, password, display_name))
    except (EmailInvalidError, RoleInvalidError) as exc:
        _LOG.error("create-user: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except TenantNotFoundError as exc:
        _LOG.error("create-user: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except SuperAdminOperationError as exc:
        _LOG.error("create-user: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except ConfigurationError as exc:
        _LOG.error("create-user: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("create-user: %s", exc.message)
        raise typer.Exit(code=3) from exc
