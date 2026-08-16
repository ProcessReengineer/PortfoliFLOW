# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ScopedSetting ORM model — settings and credentials in one shape (ADR-0112 §2).

Backs the ``scoped_settings`` table introduced in migration b032. One row
per **field**: a multi-field credential (e.g. ``openrouter`` with
``api_key`` + ``model`` + ``base_url``) is several rows sharing
``(scope, tenant_id, user_id, provider)``. That is the shape decision
ADR-0112 §2 took over ADR-0095 §4's single encrypted JSONB payload — it
makes the completeness rule (§1) and the write-only/masked display (§6)
directly expressible, and keeps non-secret fields greppable.

The table absorbs the ``provider_credentials`` design of ADR-0095 §4,
which is therefore **never created**. ADR-0095 §1–§3 remain the
authoritative resolution contract.

Three shape invariants live in the schema as CHECKs and are mirrored in
:mod:`core.repositories.scoped_setting_repository` so callers get a typed
:class:`~core.exceptions.ValidationError` rather than an
``IntegrityError``:

* an ``application`` row is exactly the one with a NULL ``tenant_id``;
* a ``user`` row is exactly the one with a ``user_id``;
* ``is_secret`` is equivalent to "ciphertext present, plain absent".

``provider`` is deliberately **not** CHECK-constrained: the taxonomy
(ADR-0112 §3) is validated in code at the write path so a new adapter does
not need a migration. ``scope`` is, because its set is closed.

No ``relationship()`` is declared (the house Phase-3 idiom): repositories
return DTOs and callers never traverse the ORM graph.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class ScopedSetting(Base):
    """One setting or credential field at one scope (ADR-0112 §2).

    Secret rows carry a Fernet token in :attr:`value_ciphertext`, produced
    by :mod:`services.credential_vault` under a master key that lives only
    in the environment. This layer is **value-opaque**: it neither
    encrypts nor decrypts, and no code path here ever sees a master key.

    ``application``-scope rows (``tenant_id IS NULL``) are unreachable by
    the application role under the ``tenant_isolation`` RLS policy, by
    construction — in v1 the application scope's source is the environment
    and no application rows are written. The scope value exists in the
    model from day one so a future ADR can wire application rows through
    the superuser path without a table change.

    Deliberately **not** audit-triggered: the generic audit trigger
    captures full row images, which would copy every secret's ciphertext
    and hint into ``audit_log`` (see the b032 migration docstring).
    """

    __tablename__ = "scoped_settings"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('application', 'tenant', 'user')",
            name="ck_scoped_settings_scope_vocabulary",
        ),
        CheckConstraint(
            "(scope = 'application') = (tenant_id IS NULL)",
            name="ck_scoped_settings_application_scope_null_tenant",
        ),
        CheckConstraint(
            "(scope = 'user') = (user_id IS NOT NULL)",
            name="ck_scoped_settings_user_scope_requires_user",
        ),
        CheckConstraint(
            "is_secret = (value_ciphertext IS NOT NULL) AND is_secret = (value_plain IS NULL)",
            name="ck_scoped_settings_secret_value_exclusivity",
        ),
        # NULLS NOT DISTINCT (PostgreSQL 15+): the NULL-bearing columns are
        # part of the key, so two tenant-scope rows for the same
        # provider/key — both with user_id NULL — collide as intended.
        UniqueConstraint(
            "scope",
            "tenant_id",
            "user_id",
            "provider",
            "key",
            name="uq_scoped_settings_scope_tenant_user_provider_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    # 'application' | 'tenant' | 'user' — TEXT with a CHECK, never a SQL
    # enum (the codebase's TEXT-for-status convention).
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL exactly for application-scope rows.
    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Set exactly for user-scope rows.
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Taxonomy key (ADR-0112 §3) — validated in code at the write path.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # Field name within the provider, e.g. 'api_key', 'model', 'bot_token'.
    key: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Config rows only.
    value_plain: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Secret rows only — a Fernet token (services/credential_vault).
    value_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # At most the last 4 characters, captured at write time for the masked
    # display (ADR-0112 §6). Never the value.
    secret_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("TRUE"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
