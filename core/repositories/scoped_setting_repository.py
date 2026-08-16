# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ScopedSettingRepository — scoped settings and credentials (ADR-0112 §2).

Backs the ``scoped_settings`` table introduced in migration b032. Shape
mirrors the other tenant-scoped repositories: a tenant-scoped
:class:`~sqlalchemy.ext.asyncio.AsyncSession` is passed in, methods return
frozen DTOs, and ``tenant_id`` is implicit in the session context (RLS
derives it from ``app.tenant_id``).

Two properties define this seam:

* **Value-opaque.** The repository neither encrypts nor decrypts. Secret
  rows carry a Fernet token in ``value_ciphertext``; producing and
  consuming it is the caller's job, through
  :mod:`services.credential_vault`. No master key is read here, and no
  code path here can turn ciphertext into plaintext.
* **User rows are always user-filtered.** The ``tenant_isolation`` RLS
  policy scopes rows to the tenant; the *user* axis is the repository's
  responsibility (ADR-0112 §2 — the house tenant-policy-plus-repository-
  filter idiom). Every user-scope read and write names its ``user_id``
  and filters on it, so one user's session cannot reach another's rows
  through this API.

There is deliberately **no application-scope path**. An application row
is exactly the one with a NULL ``tenant_id``, and this repository always
binds the ambient tenant — so the generic shape validation below refuses
``scope='application'`` before any SQL runs, and the RLS policy would
refuse it after. In v1 the application scope's source is the environment
(ADR-0112 §1); a future ADR wiring application rows through the superuser
path needs no table change.

Writes validate the schema's three CHECK invariants in Python first, so a
caller sees a typed :class:`~core.exceptions.ValidationError` naming the
offending field rather than an ``IntegrityError``. Provider-taxonomy
validation (ADR-0112 §3) is **not** here — it belongs at the write-path
seam above this layer (F2/F3), where the taxonomy lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.exceptions import ValidationError
from core.models.scoped_setting import ScopedSetting
from core.repositories.base import BaseRepository

#: The closed scope vocabulary (ADR-0112 §1). Mirrors the
#: ``ck_scoped_settings_scope_vocabulary`` CHECK.
_VALID_SCOPES: frozenset[str] = frozenset({"application", "tenant", "user"})

#: The named unique constraint the upsert conflicts on. ``NULLS NOT
#: DISTINCT``, so a NULL ``user_id`` participates in the key rather than
#: making every tenant-scope row unique by accident.
_UNIQUE_CONSTRAINT = "uq_scoped_settings_scope_tenant_user_provider_key"


@dataclass(frozen=True, repr=False)
class ScopedSettingDTO:
    """Plain data-only view of a ``scoped_settings`` row.

    Both value fields are carried **opaquely**: ``value_plain`` as stored,
    ``value_ciphertext`` as the raw Fernet token. Neither is decrypted
    here, and :func:`repr` (hence :func:`str`, hence any log line, f-string
    or traceback that renders this object) shows only whether each is set —
    mirroring
    :class:`services.investments.credential_resolver.ProviderCredential`.

    ``secret_hint`` is *not* masked: it is at most the last four characters
    and exists precisely to be displayed (ADR-0112 §6).
    """

    id: UUID
    scope: str
    tenant_id: UUID | None
    user_id: UUID | None
    provider: str
    key: str
    is_secret: bool
    value_plain: str | None
    value_ciphertext: bytes | None
    secret_hint: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        # repr=False on the decorator, so this is the only __repr__ there
        # is — a generated one would print both value columns.
        return (
            f"ScopedSettingDTO(id={self.id!r}, scope={self.scope!r}, "
            f"tenant_id={self.tenant_id!r}, user_id={self.user_id!r}, "
            f"provider={self.provider!r}, key={self.key!r}, "
            f"is_secret={self.is_secret!r}, "
            f"value_plain=<{_set_or_unset(self.value_plain)}; masked>, "
            f"value_ciphertext=<{_set_or_unset(self.value_ciphertext)}; masked>, "
            f"secret_hint={self.secret_hint!r}, enabled={self.enabled!r})"
        )

    __str__ = __repr__


def _set_or_unset(value: object) -> str:
    return "set" if value is not None else "unset"


def _validate_scope_and_user(scope: str, user_id: UUID | None) -> None:
    """Validate the scope vocabulary and the user-scope equivalence.

    The read-side half of the shape rules — everything that can be checked
    without knowing the row's ``tenant_id``.

    Args:
        scope: The scope value under validation.
        user_id: The user the row belongs to, or ``None``.

    Raises:
        ValidationError: If ``scope`` is outside the vocabulary, or the
            ``user``-scope ⇔ ``user_id``-present equivalence is broken.
    """
    if scope not in _VALID_SCOPES:
        raise ValidationError(
            f"Invalid scope {scope!r}; expected one of {sorted(_VALID_SCOPES)}.",
            field="scope",
        )
    if (scope == "user") != (user_id is not None):
        raise ValidationError(
            "A user-scope row requires a user_id, and only a user-scope row "
            f"may carry one (scope={scope!r}, user_id={'set' if user_id else 'unset'}).",
            field="user_id",
        )


def _validate_write_shape(
    *,
    scope: str,
    user_id: UUID | None,
    is_secret: bool,
    value_plain: str | None,
    value_ciphertext: bytes | None,
) -> None:
    """Validate every schema CHECK in Python before any SQL runs.

    Args:
        scope: The scope value under validation.
        user_id: The user the row belongs to, or ``None``.
        is_secret: Whether this row holds an encrypted value.
        value_plain: The plain value, for config rows.
        value_ciphertext: The Fernet token, for secret rows.

    Raises:
        ValidationError: On any broken shape invariant, naming the
            offending field.
    """
    _validate_scope_and_user(scope, user_id)
    if scope == "application":
        # Not a special case — the generic rule "(scope='application') =
        # (tenant_id IS NULL)" applied to a repository that always binds
        # the ambient tenant. The application scope's source is the
        # environment in v1 (ADR-0112 §1).
        raise ValidationError(
            "Application-scope rows carry no tenant_id and are unreachable "
            "through a tenant-scoped session (ADR-0112 §2). The application "
            "scope's source is the environment.",
            field="scope",
        )
    if is_secret != (value_ciphertext is not None):
        raise ValidationError(
            "A secret row carries value_ciphertext and a config row does not "
            f"(is_secret={is_secret}, value_ciphertext="
            f"{_set_or_unset(value_ciphertext)}).",
            field="value_ciphertext",
        )
    if is_secret != (value_plain is None):
        raise ValidationError(
            "A config row carries value_plain and a secret row does not "
            f"(is_secret={is_secret}, value_plain={_set_or_unset(value_plain)}).",
            field="value_plain",
        )


class ScopedSettingRepository(BaseRepository):
    """Read and write scoped settings in the active tenant context."""

    async def _active_tenant_id(self) -> UUID:
        """Return the tenant bound to the active session (``app.tenant_id``)."""
        row = await self._session.execute(
            text("SELECT current_setting('app.tenant_id')::uuid AS tid")
        )
        return row.scalar_one()

    # -- reads --------------------------------------------------------------

    async def list_for_tenant(self, provider: str | None = None) -> list[ScopedSettingDTO]:
        """Return the tenant-scope rows of the active tenant.

        User-scope rows are excluded: they are read through
        :meth:`list_for_user`, which names its user.

        Args:
            provider: When given, restrict to one provider's fields.

        Returns:
            Tenant-scope rows ordered by ``provider`` then ``key``.
        """
        stmt = select(ScopedSetting).where(ScopedSetting.scope == "tenant")
        if provider is not None:
            stmt = stmt.where(ScopedSetting.provider == provider)
        result = await self._session.execute(
            stmt.order_by(ScopedSetting.provider.asc(), ScopedSetting.key.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def list_for_user(
        self,
        user_id: UUID,
        provider: str | None = None,
    ) -> list[ScopedSettingDTO]:
        """Return one user's user-scope rows in the active tenant.

        Always filtered on ``user_id`` — the user axis is the repository's
        responsibility, not the RLS policy's (ADR-0112 §2).

        Args:
            user_id: The user whose rows to return.
            provider: When given, restrict to one provider's fields.

        Returns:
            That user's user-scope rows, ordered by ``provider`` then ``key``.
        """
        stmt = select(ScopedSetting).where(
            ScopedSetting.scope == "user",
            ScopedSetting.user_id == user_id,
        )
        if provider is not None:
            stmt = stmt.where(ScopedSetting.provider == provider)
        result = await self._session.execute(
            stmt.order_by(ScopedSetting.provider.asc(), ScopedSetting.key.asc())
        )
        return [_to_dto(model) for model in result.scalars().all()]

    async def get(
        self,
        scope: str,
        provider: str,
        key: str,
        user_id: UUID | None = None,
    ) -> ScopedSettingDTO | None:
        """Return one row by its natural key, or ``None``.

        Args:
            scope: ``tenant`` or ``user`` (``application`` rows are
                unreachable from a tenant-scoped session and always
                resolve to ``None``).
            provider: The taxonomy key, e.g. ``openrouter``.
            key: The field name, e.g. ``api_key``.
            user_id: Required for ``scope='user'``, forbidden otherwise.

        Returns:
            The matching :class:`ScopedSettingDTO`, or ``None``.

        Raises:
            ValidationError: If the scope vocabulary or the user-scope
                equivalence is broken.
        """
        _validate_scope_and_user(scope, user_id)
        stmt = select(ScopedSetting).where(
            ScopedSetting.scope == scope,
            ScopedSetting.provider == provider,
            ScopedSetting.key == key,
        )
        stmt = (
            stmt.where(ScopedSetting.user_id == user_id)
            if user_id is not None
            else stmt.where(ScopedSetting.user_id.is_(None))
        )
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return _to_dto(model) if model is not None else None

    # -- writes -------------------------------------------------------------

    async def upsert(
        self,
        *,
        scope: str,
        provider: str,
        key: str,
        is_secret: bool,
        value_plain: str | None = None,
        value_ciphertext: bytes | None = None,
        secret_hint: str | None = None,
        user_id: UUID | None = None,
        enabled: bool = True,
    ) -> ScopedSettingDTO:
        """Create or update one setting field, keyed on the unique tuple.

        The conflict target is the named ``NULLS NOT DISTINCT`` constraint,
        so a second write of the same ``(scope, tenant, user, provider,
        key)`` updates the existing row — including the common case where
        both rows carry ``user_id IS NULL``.

        The value arguments are **opaque**: pass a Fernet token from
        :mod:`services.credential_vault` for a secret row, or a plain string
        for a config row. This method never encrypts, decrypts, or derives
        ``secret_hint`` — the caller owns all three.

        Args:
            scope: ``tenant`` or ``user``.
            provider: The taxonomy key (validated above this layer,
                ADR-0112 §3).
            key: The field name.
            is_secret: Whether this row holds an encrypted value.
            value_plain: The plain value; required iff ``is_secret`` is False.
            value_ciphertext: The Fernet token; required iff ``is_secret``.
            secret_hint: At most the last four characters, for the masked
                display. Never the value.
            user_id: Required for ``scope='user'``, forbidden otherwise.
            enabled: Whether the row participates in resolution.

        Returns:
            The created or updated :class:`ScopedSettingDTO`.

        Raises:
            ValidationError: On any broken shape invariant.
        """
        _validate_write_shape(
            scope=scope,
            user_id=user_id,
            is_secret=is_secret,
            value_plain=value_plain,
            value_ciphertext=value_ciphertext,
        )
        active_tenant = await self._active_tenant_id()

        stmt = (
            pg_insert(ScopedSetting)
            .values(
                scope=scope,
                tenant_id=active_tenant,
                user_id=user_id,
                provider=provider,
                key=key,
                is_secret=is_secret,
                value_plain=value_plain,
                value_ciphertext=value_ciphertext,
                secret_hint=secret_hint,
                enabled=enabled,
            )
            .on_conflict_do_update(
                constraint=_UNIQUE_CONSTRAINT,
                set_={
                    "is_secret": is_secret,
                    "value_plain": value_plain,
                    "value_ciphertext": value_ciphertext,
                    "secret_hint": secret_hint,
                    "enabled": enabled,
                    # pg_insert is not a Core update(), so bump the house
                    # column explicitly.
                    "updated_at": text("NOW()"),
                },
            )
            .returning(ScopedSetting.id)
        )
        row_id: UUID = (await self._session.execute(stmt)).scalar_one()
        await self._session.flush()

        refreshed = await self._session.execute(
            select(ScopedSetting).where(ScopedSetting.id == row_id)
        )
        return _to_dto(refreshed.scalar_one())

    async def delete(
        self,
        *,
        scope: str,
        provider: str,
        key: str,
        user_id: UUID | None = None,
    ) -> bool:
        """Delete one setting field by its natural key.

        Args:
            scope: ``tenant`` or ``user``.
            provider: The taxonomy key.
            key: The field name.
            user_id: Required for ``scope='user'``, forbidden otherwise.

        Returns:
            ``True`` if a row was deleted, ``False`` if none matched in the
            active tenant context.

        Raises:
            ValidationError: If the scope vocabulary or the user-scope
                equivalence is broken.
        """
        _validate_scope_and_user(scope, user_id)
        stmt = sa_delete(ScopedSetting).where(
            ScopedSetting.scope == scope,
            ScopedSetting.provider == provider,
            ScopedSetting.key == key,
        )
        stmt = (
            stmt.where(ScopedSetting.user_id == user_id)
            if user_id is not None
            else stmt.where(ScopedSetting.user_id.is_(None))
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return (result.rowcount or 0) > 0

    async def set_enabled(
        self,
        *,
        scope: str,
        provider: str,
        key: str,
        enabled: bool,
        user_id: UUID | None = None,
    ) -> ScopedSettingDTO | None:
        """Flip one row's ``enabled`` flag without touching its value.

        Args:
            scope: ``tenant`` or ``user``.
            provider: The taxonomy key.
            key: The field name.
            enabled: The new flag value.
            user_id: Required for ``scope='user'``, forbidden otherwise.

        Returns:
            The updated :class:`ScopedSettingDTO`, or ``None`` when no such
            row exists in the active tenant context (RLS may have hidden
            it).

        Raises:
            ValidationError: If the scope vocabulary or the user-scope
                equivalence is broken.
        """
        _validate_scope_and_user(scope, user_id)
        stmt = (
            update(ScopedSetting)
            .where(
                ScopedSetting.scope == scope,
                ScopedSetting.provider == provider,
                ScopedSetting.key == key,
            )
            .values(enabled=enabled, updated_at=text("NOW()"))
            .returning(ScopedSetting.id)
        )
        stmt = (
            stmt.where(ScopedSetting.user_id == user_id)
            if user_id is not None
            else stmt.where(ScopedSetting.user_id.is_(None))
        )
        row_id = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.flush()
        if row_id is None:
            return None
        refreshed = await self._session.execute(
            select(ScopedSetting).where(ScopedSetting.id == row_id)
        )
        return _to_dto(refreshed.scalar_one())


def _to_dto(model: ScopedSetting) -> ScopedSettingDTO:
    return ScopedSettingDTO(
        id=model.id,
        scope=model.scope,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        provider=model.provider,
        key=model.key,
        is_secret=model.is_secret,
        value_plain=model.value_plain,
        value_ciphertext=model.value_ciphertext,
        secret_hint=model.secret_hint,
        enabled=model.enabled,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
