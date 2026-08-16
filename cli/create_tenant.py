# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow create-tenant`` — provision a new tenant.

Per ADR-0064 §3. Delegates to
:func:`services.super_admin.create_tenant_idempotent` so the CLI and
the web admin surface share a single implementation. After the
atomic tenant + owner + audit transaction, runs the seed-installation
sequence via :func:`services.super_admin.seed_tenant_defaults`
mirroring the bootstrap pattern.

External behaviour preserved from the pre-refactor CLI: same flags,
same exit codes, same log lines.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
    SubdomainInvalidError,
    SubdomainReservedError,
    SubdomainTakenError,
    SuperAdminOperationError,
    create_tenant_idempotent,
    seed_tenant_defaults,
)

_LOG = logging.getLogger("portfoliflow.cli")


async def _resolve_actor_id(conn) -> UUID | None:
    """Resolve the acting super-admin from ``SUPER_ADMIN_EMAIL``.

    The CLI runs outside the web request lifecycle; there is no
    authenticated user. The audit-row actor is best-effort: if
    ``SUPER_ADMIN_EMAIL`` names an existing super-admin, attribute
    to them. Otherwise return ``None`` so the caller can decide
    what to do (typically: raise, because the audit table's FK is
    NOT NULL).
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


async def _run(
    name: str,
    subdomain: str,
    owner_email: str,
    owner_password: str,
    owner_display_name: str | None = None,
) -> None:
    engine = superuser_engine()
    try:
        async with engine.begin() as conn:
            actor_id = await _resolve_actor_id(conn)
            if actor_id is None:
                raise PortfoliFlowError(
                    "create-tenant: SUPER_ADMIN_EMAIL is unset or does not "
                    "match an existing super-admin. Either:\n"
                    "  (a) Set SUPER_ADMIN_EMAIL in .env to an existing "
                    "super-admin email, or\n"
                    "  (b) Create the first super-admin with:\n"
                    "        echo -n '<password>' | portfoliflow create-super-admin \\\n"
                    "            --email <email> --password-stdin\n"
                    "      then re-run this command."
                )
            summary = await create_tenant_idempotent(
                conn,
                name=name,
                subdomain=subdomain,
                owner_email=owner_email,
                owner_password=owner_password,
                actor_super_admin_id=actor_id,
                actor_ip=None,
                actor_user_agent="cli/create-tenant",
                owner_display_name=owner_display_name,
            )
            tenant_id = summary.id
            # Resolve the owner user id for the seed step's attribution.
            owner_row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE tenant_id = :tid AND email = :email"),
                    {"tid": str(tenant_id), "email": owner_email},
                )
            ).first()
            owner_id = UUID(str(owner_row.id)) if owner_row else actor_id

        _LOG.info(
            "create-tenant: %s (subdomain=%r) user_count=%d",
            tenant_id,
            summary.subdomain,
            summary.user_count,
        )

        # Independent post-creation step — failures here do not roll
        # back the already-committed tenant + owner + audit.
        try:
            await seed_tenant_defaults(engine, tenant_id=tenant_id, actor_user_id=owner_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "create-tenant: seed installation failed (%s); "
                "tenant exists but seeds may be incomplete. Re-run the "
                "seed installer or use the SAA UI to fill in.",
                exc,
            )
    finally:
        await engine.dispose()


def create_tenant_command(
    name: str = typer.Option(..., "--name", help="Display name"),
    subdomain: str = typer.Option(..., "--subdomain", help="URL subdomain"),
    owner_email: str = typer.Option(..., "--owner-email", help="Owner's email address"),
    owner_password_stdin: bool = typer.Option(
        False,
        "--owner-password-stdin",
        help="Read owner password from stdin",
    ),
    owner_display_name: str | None = typer.Option(
        None,
        "--owner-display-name",
        help="Optional human display name for the initial owner",
    ),
) -> None:
    """Provision a new tenant idempotently."""
    configure_logging()
    try:
        if owner_password_stdin:
            password = sys.stdin.readline().rstrip("\n")
            if not password:
                raise ConfigurationError("create-tenant: empty password on stdin")
        else:
            password = os.getenv("OWNER_PASSWORD", "")
            if not password:
                raise ConfigurationError(
                    "create-tenant: --owner-password-stdin or OWNER_PASSWORD env var required"
                )
        asyncio.run(_run(name, subdomain, owner_email, password, owner_display_name))
    except (
        SubdomainInvalidError,
        SubdomainReservedError,
        SubdomainTakenError,
        EmailInvalidError,
        SuperAdminOperationError,
    ) as exc:
        _LOG.error("create-tenant: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except ConfigurationError as exc:
        _LOG.error("create-tenant: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("create-tenant: %s", exc.message)
        raise typer.Exit(code=3) from exc
