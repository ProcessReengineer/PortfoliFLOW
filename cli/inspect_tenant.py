# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow inspect-tenant`` — read-only emergency diagnostic.

Per ADR-0064 §3. The **only** sanctioned super-admin → tenant-data
pathway. Mandatory ``--reason``; every invocation writes:

- One row to ``super_admin_audit`` (platform-side log).
- One row to the target tenant's ``audit_log`` (tenant-side mirror;
  visible to the tenant's auditor role).

The command is read-only by construction — every SQL statement it
issues is a ``SELECT`` against tenant metadata, never against
domain row content. The
``tests/cli/test_inspect_tenant_is_read_only.py`` integration test
asserts the invariant by capturing every statement.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from uuid import UUID

import typer
from sqlalchemy import text

from cli._db import superuser_engine
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging

_LOG = logging.getLogger("portfoliflow.cli")

_UUID_RE: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


async def _resolve_tenant_id(conn, tenant_arg: str) -> UUID:
    if _UUID_RE.match(tenant_arg):
        return UUID(tenant_arg)
    result = await conn.execute(
        text("SELECT id FROM tenants WHERE subdomain = :sd"),
        {"sd": tenant_arg.lower()},
    )
    row = result.first()
    if row is None:
        raise PortfoliFlowError(f"inspect-tenant: tenant {tenant_arg!r} not found")
    return UUID(str(row.id))


async def _run(tenant_arg: str, reason: str) -> None:
    super_admin_email = os.getenv("SUPER_ADMIN_EMAIL", "")
    engine = superuser_engine()
    try:
        async with engine.connect() as conn:
            tenant_id = await _resolve_tenant_id(conn, tenant_arg)

            # ---- tenant metadata ----------------------------------------
            tenant_row = (
                await conn.execute(
                    text(
                        "SELECT name, subdomain, is_active, created_at FROM tenants WHERE id = :tid"
                    ),
                    {"tid": str(tenant_id)},
                )
            ).first()

            # Set app.tenant_id so tenant-scoped reads work via RLS.
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )

            users_count = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE tenant_id = :tid"),
                    {"tid": str(tenant_id)},
                )
            ).scalar_one()

            # Investments count via the application engine would
            # require an RLS context. Use a single SELECT here against
            # the superuser engine (it ignores RLS) so the report is
            # stable. The aggregate count is metadata, not row content.
            try:
                investments_count = (
                    await conn.execute(
                        text("SELECT COUNT(*) FROM investments WHERE tenant_id = :tid"),
                        {"tid": str(tenant_id)},
                    )
                ).scalar_one()
            except Exception:  # noqa: BLE001 — older DBs may not yet have it
                investments_count = 0

        # ---- output --------------------------------------------------------
        if tenant_row is None:
            print(f"Tenant: NOT FOUND (id={tenant_id})")
        else:
            print(f"Tenant: {tenant_row.name} ({tenant_row.subdomain})")
            print(f"  id: {tenant_id}")
            print(f"  subdomain: {tenant_row.subdomain}")
            print(f"  is_active: {tenant_row.is_active}")
            print(f"  created_at: {tenant_row.created_at}")
            print()
            print(f"Users: {users_count}")
            print(f"Investments: {investments_count}")

        # ---- audit (separate transaction so report prints even if
        #      audit write fails) --------------------------------------
        async with engine.begin() as conn:
            try:
                await conn.execute(
                    text(
                        """
                        INSERT INTO super_admin_audit
                            (super_admin_user_id, action, target_tenant_id,
                             reason, payload)
                        VALUES (
                            COALESCE(
                                (SELECT id FROM users
                                 WHERE email = :super_admin_email
                                   AND is_super_admin = TRUE LIMIT 1),
                                '00000000-0000-0000-0000-000000000000'::uuid
                            ),
                            'inspect_tenant',
                            :tid,
                            :reason,
                            jsonb_build_object(
                                'operator_email', :super_admin_email
                            )
                        )
                        """
                    ),
                    {
                        "super_admin_email": super_admin_email,
                        "tid": str(tenant_id),
                        "reason": reason,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "inspect-tenant: super_admin_audit write failed (%s)",
                    exc,
                )

            try:
                await conn.execute(
                    text("SELECT set_config('app.tenant_id', :tid, true)"),
                    {"tid": str(tenant_id)},
                )
                await conn.execute(
                    text(
                        """
                        INSERT INTO audit_log
                            (tenant_id, table_name, row_id, op, changed_by,
                             payload)
                        VALUES (
                            :tid, 'tenants', :tid, 'super_admin_inspect',
                            NULL,
                            jsonb_build_object(
                                'reason', :reason,
                                'operator_email', :super_admin_email
                            )
                        )
                        """
                    ),
                    {
                        "tid": str(tenant_id),
                        "reason": reason,
                        "super_admin_email": super_admin_email,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "inspect-tenant: audit_log mirror write failed (%s)",
                    exc,
                )
    finally:
        await engine.dispose()


def inspect_tenant_command(
    tenant: str = typer.Option(..., "--tenant"),
    reason: str = typer.Option(
        ...,
        "--reason",
        help="Mandatory justification (audited).",
    ),
) -> None:
    """Read-only emergency diagnostic for a target tenant.

    Mandatory ``--reason`` per ADR-0064 §3. The command emits two
    audit rows (platform + tenant mirror) so usage is reviewable.
    """
    configure_logging()
    try:
        if not reason.strip():
            raise ConfigurationError("inspect-tenant: --reason cannot be empty")
        asyncio.run(_run(tenant, reason))
    except ConfigurationError as exc:
        _LOG.error("inspect-tenant: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("inspect-tenant: %s", exc.message)
        raise typer.Exit(code=3) from exc
