# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""UserRepository — first concrete repository, used as the layering exemplar.

Demonstrates the pattern that all subsequent repositories follow:

- Constructor accepts a tenant-scoped :class:`AsyncSession`.
- Methods return frozen dataclasses (:class:`UserDTO`), never ORM
  instances. Callers do not see SQLAlchemy.
- ``tenant_id`` is never accepted as a method argument — it is implicit
  in the session context (RLS WITH CHECK derives it from
  ``app.tenant_id``).

The DTO carries the ADR-0063 role model (``roles: tuple[str, ...]``)
and the platform axis (``is_super_admin: bool``). ``password_hash`` is
masked in ``__repr__`` so it cannot leak through accidental logging.

See ADR-0018 (Service / Repository layering), ADR-0034 §3, and
ADR-0063 §2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from core.models.user import User
from core.repositories.base import BaseRepository

# Canonical tenant-role values. Mirrors the ck_users_roles_values
# CHECK constraint installed by migration b012.
ALLOWED_ROLES: frozenset[str] = frozenset({"owner", "member", "auditor"})


@dataclass(frozen=True)
class UserDTO:
    """Plain data-only view of a User row.

    DTOs are returned by repository methods so callers do not depend on
    SQLAlchemy's ORM lifecycle (session attachment, lazy loading,
    expiration on commit). Mutation goes back through the repository.

    ``password_hash`` is included on the DTO because the auth backend
    needs it during verification, but it is masked in ``__repr__`` so
    that accidental logging cannot leak the hash. The hash itself is
    not a plaintext password, but Argon2id hashes are still
    credentials-equivalent and must not appear in log streams or
    exception traces.

    ``roles`` is a tuple to keep the DTO hashable (the dataclass is
    frozen). Values are constrained to :data:`ALLOWED_ROLES` at the
    DB layer; consumers should treat the tuple as a set membership
    test, not as an ordered sequence.
    """

    id: UUID
    tenant_id: UUID
    email: str
    password_hash: str | None
    external_idp: str | None
    external_subject: str | None
    display_name: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)
    is_super_admin: bool = False
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def has_role(self, *allowed: str) -> bool:
        """Return True if the user holds any of ``allowed`` roles."""
        return any(r in self.roles for r in allowed)

    def __repr__(self) -> str:
        masked = "***" if self.password_hash is not None else None
        return (
            f"UserDTO(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"email={self.email!r}, display_name={self.display_name!r}, "
            f"password_hash={masked!r}, "
            f"external_idp={self.external_idp!r}, "
            f"external_subject={self.external_subject!r}, "
            f"roles={self.roles!r}, "
            f"is_super_admin={self.is_super_admin!r}, "
            f"is_active={self.is_active!r}, "
            f"created_at={self.created_at!r}, updated_at={self.updated_at!r})"
        )


def _to_dto(model: User) -> UserDTO:
    return UserDTO(
        id=model.id,
        tenant_id=model.tenant_id,
        email=model.email,
        display_name=model.display_name,
        password_hash=model.password_hash,
        external_idp=model.external_idp,
        external_subject=model.external_subject,
        roles=tuple(model.roles or ()),
        is_super_admin=model.is_super_admin,
        is_active=model.is_active,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class UserRepository(BaseRepository):
    """Read and write users in the active tenant context."""

    async def get_by_id(self, user_id: UUID) -> UserDTO | None:
        """Return the user with the given id, or None if not found.

        "Not found" includes the case where the user exists in another
        tenant — RLS hides them, and the repository correctly reports
        absence rather than raising.
        """
        result = await self._session.execute(select(User).where(User.id == user_id))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def get_by_email(self, email: str) -> UserDTO | None:
        """Return the user matching ``email`` in the current tenant.

        Used by the auth backend during login. The active tenant is
        derived from the session GUC; this method does not accept
        ``tenant_id`` for the same defence-in-depth reason as
        :meth:`get_by_id`.
        """
        result = await self._session.execute(select(User).where(User.email == email))
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    async def list_all(self) -> list[UserDTO]:
        """Return every user visible in the current tenant context."""
        result = await self._session.execute(select(User).order_by(User.created_at))
        return [_to_dto(model) for model in result.scalars().all()]

    async def create(
        self,
        email: str,
        password_hash: str | None = None,
        roles: Sequence[str] = ("member",),
        is_super_admin: bool = False,
        is_active: bool = True,
        display_name: str | None = None,
    ) -> UserDTO:
        """Create a user in the current tenant context.

        ``tenant_id`` is set automatically by the database from
        ``app.tenant_id`` via the RLS WITH CHECK clause and a server-
        side default — the application does not pass it explicitly.
        That keeps every write at the repository layer compatible with
        the cross-tenant guarantees ADR-0035 §6 mandates.

        Args:
            email: The user's email address.
            password_hash: Argon2id-encoded password hash. ``None`` is
                accepted so callers seeding test fixtures or future
                OIDC-only users do not need to invent a hash. The
                CHECK constraint enforces that at least one of
                ``password_hash`` or the OIDC pair is non-NULL.
            roles: Tenant roles to assign. Defaults to ``('member',)``
                per ADR-0063 §2 minimum-privilege. Values are
                validated against :data:`ALLOWED_ROLES` so an
                application bug cannot trigger a database CHECK
                violation with a less helpful error.
            is_super_admin: Platform-level flag. May only be ``True``
                when the active tenant is the system tenant; the
                database CHECK enforces this. Defaults to ``False``.
            is_active: Whether the account is enabled.
            display_name: Optional human display name (ADR-0068).
                ``None`` is the default — the column is nullable and no
                caller is forced to supply it. The Front Office welcome
                header derives a first name from it.
        """
        # The tenant_id NOT NULL constraint requires us to provide a
        # value at INSERT time. We read it back from the active GUC so
        # the application stays one source of truth: the session
        # context is what determines the tenant binding, not method
        # arguments. RLS WITH CHECK then re-validates the value, which
        # is the defence-in-depth ADR-0035 §6 calls for — both layers
        # would have to be wrong for a leak to happen.
        from sqlalchemy import text

        roles_list = list(roles)
        if not roles_list:
            raise ValueError("UserRepository.create: roles must be non-empty")
        invalid = [r for r in roles_list if r not in ALLOWED_ROLES]
        if invalid:
            raise ValueError(
                f"UserRepository.create: unknown role(s) {invalid!r}; "
                f"allowed={sorted(ALLOWED_ROLES)!r}"
            )

        tenant_row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        active_tenant: UUID = tenant_row.scalar_one()

        model = User(
            tenant_id=active_tenant,
            email=email,
            display_name=display_name,
            password_hash=password_hash,
            roles=roles_list,
            is_super_admin=is_super_admin,
            is_active=is_active,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def set_password_hash(self, user_id: UUID, password_hash: str) -> None:
        """Update a user's password hash.

        The session must be tenant-scoped; cross-tenant updates are
        blocked by RLS. The trigger-driven audit log captures the
        change; the column-level value is masked in ``UserDTO.__repr__``
        so that downstream logging cannot leak it.

        Retained as the user-management write seam for roadmap #015
        (Multi-User & Permissions): the forthcoming admin-driven
        password-management surface writes through this method rather
        than the CLI-only rotation paths. It has no in-tree caller yet
        by design; do not flag it as dead code.
        """
        from sqlalchemy import update

        await self._session.execute(
            update(User).where(User.id == user_id).values(password_hash=password_hash)
        )

    async def set_active(self, user_id: UUID, active: bool) -> UserDTO | None:
        """Enable or disable a user in the current tenant context.

        The activation switch behind ADR-0121 §5 — deactivation is how a
        tenant retires an account, since users are never deleted (the
        audit trail and every ``created_by`` reference would lose their
        subject). Session invalidation is the caller's concern: the
        service layer deletes the deactivated user's sessions in the same
        transaction (ADR-0121 §4.5).

        Args:
            user_id: The target user.
            active: The desired ``is_active`` value.

        Returns:
            The updated :class:`UserDTO`, or ``None`` when ``user_id``
            does not resolve in the active tenant context — an unknown
            id, or a user of another tenant that RLS hides. Absence and
            foreignness are deliberately indistinguishable here, exactly
            as in :meth:`get_by_id`.
        """
        result = await self._session.execute(select(User).where(User.id == user_id))
        model = result.scalar_one_or_none()
        if model is None:
            return None
        model.is_active = active
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)

    async def set_roles(self, user_id: UUID, roles: Sequence[str]) -> UserDTO | None:
        """Replace a user's tenant roles in the current tenant context.

        The role set is replaced wholesale, not merged — the caller
        states the roles the user should end up with. Per ADR-0121 §5.

        Args:
            user_id: The target user.
            roles: The roles to assign. Validated against
                :data:`ALLOWED_ROLES` for the same reason
                :meth:`create` validates them: an application bug should
                surface as a clear error rather than a database CHECK
                violation.

        Returns:
            The updated :class:`UserDTO`, or ``None`` when ``user_id``
            does not resolve in the active tenant context (see
            :meth:`set_active`).

        Raises:
            ValueError: If ``roles`` is empty or contains a value outside
                :data:`ALLOWED_ROLES`.
        """
        roles_list = list(roles)
        if not roles_list:
            raise ValueError("UserRepository.set_roles: roles must be non-empty")
        invalid = [r for r in roles_list if r not in ALLOWED_ROLES]
        if invalid:
            raise ValueError(
                f"UserRepository.set_roles: unknown role(s) {invalid!r}; "
                f"allowed={sorted(ALLOWED_ROLES)!r}"
            )

        result = await self._session.execute(select(User).where(User.id == user_id))
        model = result.scalar_one_or_none()
        if model is None:
            return None
        model.roles = roles_list
        await self._session.flush()
        await self._session.refresh(model)
        return _to_dto(model)
