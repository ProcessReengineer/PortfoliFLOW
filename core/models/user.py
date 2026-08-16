# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""User ORM model — accounts that authenticate against a tenant.

ADR-0063 §2 replaces the Phase-2 ``is_tenant_owner: bool``
approximation with ``roles: TEXT[]`` (CHECK-constrained to
``{'owner', 'member', 'auditor'}``) and adds the orthogonal
``is_super_admin: bool`` platform axis bound to the system tenant.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class User(Base):
    """A user account belonging to exactly one tenant."""

    __tablename__ = "users"
    # The OIDC-uniqueness rule is a partial unique index: it fires only
    # when both columns are non-NULL. This expresses the actual intent
    # ("the OIDC subject is unique among rows that have one") rather
    # than the SQL-standard NULL-distinct behaviour of plain UNIQUE.
    # The CHECK constraint enforces ADR-0036 §2: every active user must
    # be authenticatable through at least one configured backend.
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        Index(
            "uq_users_external_identity",
            "external_idp",
            "external_subject",
            unique=True,
            postgresql_where=text("external_idp IS NOT NULL AND external_subject IS NOT NULL"),
        ),
        CheckConstraint(
            "password_hash IS NOT NULL "
            "OR (external_idp IS NOT NULL AND external_subject IS NOT NULL)",
            name="ck_users_authenticatable",
        ),
        CheckConstraint(
            "array_length(roles, 1) >= 1 AND roles <@ ARRAY['owner', 'member', 'auditor']::text[]",
            name="ck_users_roles_values",
        ),
        # Super-admins live structurally only in the system tenant
        # (00000000-0000-0000-0000-000000000000). Encoded in the
        # schema so a route-layer bug cannot create a super-admin row
        # in a normal tenant. See ADR-0063 §3.
        CheckConstraint(
            "is_super_admin = FALSE OR tenant_id = '00000000-0000-0000-0000-000000000000'::uuid",
            name="ck_users_super_admin_in_system_tenant",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    # Optional human display name (ADR-0068). Nullable and never
    # required — the Front Office welcome header derives a first name
    # from it, falling back gracefully when it is NULL.
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_idp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    roles: Mapped[list[str]] = mapped_column(
        PG_ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY['member']::text[]"),
    )
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("FALSE"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
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
