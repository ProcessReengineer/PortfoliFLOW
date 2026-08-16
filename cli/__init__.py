# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfoliFLOW operator CLI.

The ``portfoliflow`` command is the canonical operator surface for
deployment and maintenance tasks that the running application is not
responsible for. Subcommands:

- ``portfoliflow bootstrap`` — idempotent sentinel-tenant /
  sentinel-user initialisation (ADR-0040).
- ``portfoliflow set-password`` — rotate the password of an existing
  user (Phase-2 substitute for the deferred email-based password
  reset flow).
- ``portfoliflow reset-dev`` — destructive, dev-only: truncate every
  domain table, re-bootstrap the sentinel and re-run the full
  bootstrap seed pipeline (seed parity with ``bootstrap``). Schema
  state (``alembic_version``) is preserved. Sub-stream 3a, Task 2.
- ``portfoliflow status`` — non-destructive diagnostic snapshot of
  schema head, sentinel state, user count, and AIService
  configuration. Sub-stream 3a, Task 3.
- ``portfoliflow create-tenant`` — provision a new tenant
  idempotently: tenant + initial owner, then the per-tenant default
  seed (ADR-0064 §3).
- ``portfoliflow create-super-admin`` — create a super-admin user in
  the system tenant (ADR-0064 §3).
- ``portfoliflow create-user`` — create a user in a target tenant
  with the given roles.
- ``portfoliflow inspect-tenant`` — read-only emergency diagnostic
  for a target tenant. Mandatory ``--reason``; every invocation is
  audited platform-side and mirrored into the tenant (ADR-0064 §3).
- ``portfoliflow irene-tick`` — beat every tenant whose Irene
  schedule is due; tenant-blind, fired by a systemd timer (ADR-0086).
- ``portfoliflow market-data-tick`` — refresh every tenant whose
  market-data schedule is due; tenant-blind, no AI dependency
  (ADR-0093).
- ``portfoliflow seed-watchpoints`` — idempotently install any default
  watchpoints a tenant is missing (ADR-0116 §8). Provisioning runs the
  same installer, but before any book exists; re-run this once after the
  first workbook import so the fx pairs (and, for the demo tenant, the
  price watchpoints) actually materialise.
- ``portfoliflow vault-generate-key`` — emit a fresh Fernet master key
  for the credential vault (ADR-0112 §2). One line on stdout.
- ``portfoliflow vault-rotate-key`` — re-encrypt every vault secret
  under a new master key, cross-tenant, in one transaction
  (ADR-0112 §2).

The CLI connects to Postgres as the **superuser** (the only code path
in PortfoliFLOW permitted to do so — see ADR-0040 §2). Application
code (FastAPI web app, Telegram bot) always connects as the
unprivileged ``portfoliflow_app`` role.
"""

from __future__ import annotations

import typer

from cli.bootstrap import bootstrap_command, set_password_command
from cli.create_super_admin import create_super_admin_command
from cli.create_tenant import create_tenant_command
from cli.create_user import create_user_command
from cli.inspect_tenant import inspect_tenant_command
from cli.irene_tick import irene_tick_command
from cli.market_data_tick import market_data_tick_command
from cli.reset_dev import reset_dev_command
from cli.seed_watchpoints import seed_watchpoints_command
from cli.status import status_command
from cli.vault import vault_generate_key_command, vault_rotate_key_command

app: typer.Typer = typer.Typer(
    name="portfoliflow",
    help="PortfoliFLOW operator CLI (deployment and maintenance).",
    no_args_is_help=True,
    add_completion=False,
)

app.command(name="bootstrap")(bootstrap_command)
app.command(name="set-password")(set_password_command)
app.command(name="reset-dev")(reset_dev_command)
app.command(name="status")(status_command)
app.command(name="create-tenant")(create_tenant_command)
app.command(name="create-super-admin")(create_super_admin_command)
app.command(name="create-user")(create_user_command)
app.command(name="inspect-tenant")(inspect_tenant_command)
app.command(name="irene-tick")(irene_tick_command)
app.command(name="market-data-tick")(market_data_tick_command)
app.command(name="seed-watchpoints")(seed_watchpoints_command)
app.command(name="vault-generate-key")(vault_generate_key_command)
app.command(name="vault-rotate-key")(vault_rotate_key_command)


__all__ = ["app"]
