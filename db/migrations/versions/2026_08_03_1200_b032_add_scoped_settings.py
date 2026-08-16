# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the scoped_settings table — settings and credentials in one shape.

Revision ID: b032_add_scoped_settings
Revises: b031_add_case_workflow
Create Date: 2026-08-03 12:00:00 UTC

The storage substrate for ADR-0112 §2: **one** tenant-scoped table
absorbing both general settings and provider credentials, keyed
per field (``provider``, ``key``) across three scopes
(``application`` / ``tenant`` / ``user``). Non-secret configuration
lives in ``value_plain`` and stays greppable for support; secret
fields live in ``value_ciphertext`` as Fernet tokens produced by
``services/credential_vault`` under a master key that is only ever
in the environment (never in this table, never in any other).

This migration formally lands the **supersession of ADR-0095 §4**:
the ``provider_credentials`` table specified there (one encrypted
JSONB payload per ``(tenant, provider)``) is *never created*. Its
concerns are folded in here as per-field rows plus ``is_secret``.
ADR-0095 §1–§3 — the resolution contract, the per-provider
``env_fallback`` policy, and the environment source — remain
authoritative and are untouched by this change.

Shape decisions carried from the ADR:

* Three CHECKs pin the scope shape and the value exclusivity: an
  ``application`` row is exactly the one with a NULL ``tenant_id``, a
  ``user`` row is exactly the one with a ``user_id``, and ``is_secret``
  is equivalent to "ciphertext present, plain absent".
* ``UNIQUE NULLS NOT DISTINCT`` on
  ``(scope, tenant_id, user_id, provider, key)`` — PostgreSQL 15+ (the
  images in use are 16). The NULL-bearing columns are part of the key
  by design: two tenant-scope rows for the same provider/key both
  carry ``user_id IS NULL`` and must collide. No separate lookup index
  is added: the resolver reads on the ``(scope, tenant_id, user_id,
  provider)`` prefix of this constraint's own index.
* ``apply_tenant_rls('scoped_settings')`` attaches the standard
  ``tenant_isolation`` policy. ``application``-scope rows
  (``tenant_id IS NULL``) are consequently unreachable by the
  application role **by construction** — deliberate: in v1 the
  application scope's source is the environment and no application
  rows are written; the scope value exists from day one so a future
  ADR can wire it through the superuser path without a table change.
* **No audit trigger.** Deliberate, and the one place in this schema
  where the omission is a security requirement rather than the b019
  convention: ``audit_trigger_function()`` captures *full row images*
  (``to_jsonb(NEW)`` / ``to_jsonb(OLD)``, no column exclusion), so
  attaching it would copy every secret's ciphertext — and its
  ``secret_hint`` — into ``audit_log``, where the auditor role reads
  it and the vault's encryption boundary does not apply. That
  contradicts ADR-0112 §6 (secret values never appear in logs, error
  messages, or audit rows) and the ADR-0095 §4 write-only/masked
  contract it absorbs. The ``created_at`` / ``updated_at`` house
  columns still apply; per-row provenance beyond them is the F3
  surface's concern, not a full-image copy.

No data is seeded: the taxonomy (ADR-0112 §3) is validated in code at
the write path, and v1 writes no ``application``-scope rows.

The migration is fully reversible: ``downgrade`` drops the table, and
Postgres drops its RLS policy and row-security state along with it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b032_add_scoped_settings"
down_revision: str | None = "b031_add_case_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    op.create_table(
        "scoped_settings",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # 'application' | 'tenant' | 'user'. TEXT with a CHECK, not a SQL
        # enum — the codebase's TEXT-for-status convention. Closed set, so
        # unlike `provider` it is CHECK-enforced here.
        sa.Column("scope", sa.Text(), nullable=False),
        # NULL exactly for application-scope rows (see the CHECK below).
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # Set exactly for user-scope rows (see the CHECK below).
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # Taxonomy key (ADR-0112 §3) — validated in code at the write path,
        # not by a CHECK: the provider set grows with adapters, and a CHECK
        # would force a migration per adapter without adding a second source
        # of truth worth having.
        sa.Column("provider", sa.Text(), nullable=False),
        # Field name within the provider, e.g. 'api_key', 'model', 'bot_token'.
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        # Config rows only.
        sa.Column("value_plain", sa.Text(), nullable=True),
        # Secret rows only — a Fernet token from services/credential_vault.
        sa.Column("value_ciphertext", sa.LargeBinary(), nullable=True),
        # At most the last 4 characters, captured at write time for the
        # masked display (ADR-0112 §6). Never the value.
        sa.Column("secret_hint", sa.Text(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "scope IN ('application', 'tenant', 'user')",
            name="ck_scoped_settings_scope_vocabulary",
        ),
        # Equivalence, not implication: an application row has no tenant AND
        # a tenant-less row is an application row.
        sa.CheckConstraint(
            "(scope = 'application') = (tenant_id IS NULL)",
            name="ck_scoped_settings_application_scope_null_tenant",
        ),
        sa.CheckConstraint(
            "(scope = 'user') = (user_id IS NOT NULL)",
            name="ck_scoped_settings_user_scope_requires_user",
        ),
        # A row carries exactly one of the two value columns, and which one
        # is exactly what is_secret says.
        sa.CheckConstraint(
            "is_secret = (value_ciphertext IS NOT NULL) AND is_secret = (value_plain IS NULL)",
            name="ck_scoped_settings_secret_value_exclusivity",
        ),
        sa.UniqueConstraint(
            "scope",
            "tenant_id",
            "user_id",
            "provider",
            "key",
            name="uq_scoped_settings_scope_tenant_user_provider_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.execute("SELECT apply_tenant_rls('scoped_settings');")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres drops the table's RLS policy, row-security state, CHECKs and
    # unique constraint together with the table, so no explicit policy drop
    # is required (the b031 idiom).
    op.drop_table("scoped_settings")
