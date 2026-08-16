# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""ScopedSettingRepository tests against the live compose Postgres.

``scoped_settings`` is tenant-scoped (RLS-policed, ADR-0112 §2 / ADR-0035)
and carries a second, *repository-enforced* axis: user-scope rows are
filtered on ``user_id`` in code, not by the policy. These tests pin both
boundaries plus the shape invariants the schema CHECKs express.

Coverage
--------
* SS-01: create / read-back / update through :meth:`upsert`; values are
  carried opaquely (the repository never encrypts or decrypts).
* SS-02: unique-tuple behaviour, including the NULLS-NOT-DISTINCT case —
  two tenant-scope rows with the same provider/key and ``user_id IS
  NULL`` collide: the second ``upsert`` updates, a plain INSERT raises.
* SS-03: the user-filter idiom — user X cannot read user Y's user-scope
  rows through the repository API, and a user-scope read must name a user.
* SS-04: ``list_for_tenant`` returns tenant-scope rows only, ordered,
  optionally narrowed to one provider.
* SS-05: shape violations surface as typed :class:`ValidationError`s
  before any SQL runs — and the DB CHECKs are really there underneath.
* SS-06: RLS isolation — tenant A cannot see tenant B's rows.
* SS-07: ``delete`` and ``set_enabled``.
* SS-08: the DTO's ``repr`` masks both value columns.
"""

from __future__ import annotations

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import ValidationError
from core.models.scoped_setting import ScopedSetting
from core.repositories import (
    ScopedSettingRepository,
    UserRepository,
    tenant_context,
)

_CIPHERTEXT = b"gAAAAABtest-token-not-a-real-fernet-payload"
_OTHER_CIPHERTEXT = b"gAAAAABtest-token-rotated"


async def _seed_user(app_engine: AsyncEngine, tenant_id, email: str):
    """Insert a user in ``tenant_id`` and return its id (for user-scope rows)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


# ---------------------------------------------------------------------------
# SS-01: create, read back, update
# ---------------------------------------------------------------------------


async def test_ss01_upsert_creates_a_config_row_and_get_reads_it_back(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-01")

    async with tenant_context(app_engine, tenant_id) as session:
        created = await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="anthropic/claude-sonnet-4.5",
        )

    assert created.scope == "tenant"
    assert created.tenant_id == tenant_id
    assert created.user_id is None
    assert created.is_secret is False
    assert created.value_plain == "anthropic/claude-sonnet-4.5"
    assert created.value_ciphertext is None
    assert created.enabled is True

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await ScopedSettingRepository(session).get("tenant", "openrouter", "model")

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.value_plain == "anthropic/claude-sonnet-4.5"


async def test_ss01_secret_row_round_trips_the_ciphertext_verbatim(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The repository is value-opaque: bytes in, the same bytes out."""
    tenant_id = await seed_tenant("SS-01b")

    async with tenant_context(app_engine, tenant_id) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
            secret_hint="cdef",
        )

    async with tenant_context(app_engine, tenant_id) as session:
        fetched = await ScopedSettingRepository(session).get("tenant", "openrouter", "api_key")

    assert fetched is not None
    assert fetched.is_secret is True
    assert fetched.value_ciphertext == _CIPHERTEXT
    assert fetched.value_plain is None
    # The hint is displayable by design (ADR-0112 §6) — it is not the value.
    assert fetched.secret_hint == "cdef"


async def test_ss01_get_returns_none_for_an_absent_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-01c")
    async with tenant_context(app_engine, tenant_id) as session:
        assert await ScopedSettingRepository(session).get("tenant", "openfigi", "api_key") is None


# ---------------------------------------------------------------------------
# SS-02: unique tuple, NULLS NOT DISTINCT
# ---------------------------------------------------------------------------


async def test_ss02_second_upsert_updates_the_same_row(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Both rows carry ``user_id IS NULL`` — NULLS NOT DISTINCT makes them collide."""
    tenant_id = await seed_tenant("SS-02")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        first = await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
            secret_hint="1234",
        )
        second = await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_OTHER_CIPHERTEXT,
            secret_hint="5678",
        )

    assert second.id == first.id, "the unique tuple must update in place, not insert a twin"
    assert second.value_ciphertext == _OTHER_CIPHERTEXT
    assert second.secret_hint == "5678"

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await ScopedSettingRepository(session).list_for_tenant()
    assert len(rows) == 1


async def test_ss02_upsert_can_switch_a_row_between_config_and_secret(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-02b")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="tenant",
            provider="telegram",
            key="bot_token",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
        )
        flipped = await repo.upsert(
            scope="tenant",
            provider="telegram",
            key="bot_token",
            is_secret=False,
            value_plain="not-actually-a-secret",
        )

    assert flipped.is_secret is False
    assert flipped.value_ciphertext is None
    assert flipped.value_plain == "not-actually-a-secret"


async def test_ss02_plain_insert_of_the_same_tuple_violates_the_constraint(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The constraint is real, and NULL ``user_id`` participates in the key."""
    tenant_id = await seed_tenant("SS-02c")

    async with tenant_context(app_engine, tenant_id) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openfigi",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
        )

    with pytest.raises(IntegrityError) as excinfo:
        async with tenant_context(app_engine, tenant_id) as session:
            await session.execute(
                insert(ScopedSetting).values(
                    scope="tenant",
                    tenant_id=tenant_id,
                    user_id=None,
                    provider="openfigi",
                    key="api_key",
                    is_secret=True,
                    value_ciphertext=_OTHER_CIPHERTEXT,
                )
            )
    assert "uq_scoped_settings_scope_tenant_user_provider_key" in str(excinfo.value)


async def test_ss02_same_provider_key_at_two_scopes_coexists(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """``scope`` is part of the key: a user override sits beside the tenant row."""
    tenant_id = await seed_tenant("SS-02d")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ss02d.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="tenant-model",
        )
        await repo.upsert(
            scope="user",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="user-model",
            user_id=user_id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        tenant_rows = await repo.list_for_tenant()
        user_rows = await repo.list_for_user(user_id)

    assert [r.value_plain for r in tenant_rows] == ["tenant-model"]
    assert [r.value_plain for r in user_rows] == ["user-model"]


# ---------------------------------------------------------------------------
# SS-03: the user-filter idiom
# ---------------------------------------------------------------------------


async def test_ss03_user_cannot_read_another_users_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Same tenant, so RLS admits both rows — the repository draws the line."""
    tenant_id = await seed_tenant("SS-03")
    user_x = await _seed_user(app_engine, tenant_id, "x@ss03.example")
    user_y = await _seed_user(app_engine, tenant_id, "y@ss03.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="user",
            provider="telegram",
            key="chat_id",
            is_secret=False,
            value_plain="1111",
            user_id=user_x,
        )
        await repo.upsert(
            scope="user",
            provider="telegram",
            key="chat_id",
            is_secret=False,
            value_plain="2222",
            user_id=user_y,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        x_rows = await repo.list_for_user(user_x)
        y_rows = await repo.list_for_user(user_y)
        x_get = await repo.get("user", "telegram", "chat_id", user_id=user_x)

    assert [r.value_plain for r in x_rows] == ["1111"]
    assert [r.value_plain for r in y_rows] == ["2222"]
    assert x_get is not None and x_get.value_plain == "1111"


async def test_ss03_user_scope_read_must_name_a_user(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("SS-03b")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        with pytest.raises(ValidationError) as excinfo:
            await repo.get("user", "telegram", "chat_id")
    assert excinfo.value.field == "user_id"


async def test_ss03_list_for_user_filters_by_provider(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("SS-03c")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ss03c.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="user",
            provider="telegram",
            key="chat_id",
            is_secret=False,
            value_plain="1111",
            user_id=user_id,
        )
        await repo.upsert(
            scope="user",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="user-model",
            user_id=user_id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        rows = await ScopedSettingRepository(session).list_for_user(user_id, provider="telegram")

    assert [(r.provider, r.key) for r in rows] == [("telegram", "chat_id")]


# ---------------------------------------------------------------------------
# SS-04: list_for_tenant
# ---------------------------------------------------------------------------


async def test_ss04_list_for_tenant_excludes_user_rows_and_orders(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-04")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ss04.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="m",
        )
        await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="base_url",
            is_secret=False,
            value_plain="https://openrouter.ai/api/v1",
        )
        await repo.upsert(
            scope="tenant",
            provider="openfigi",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
        )
        await repo.upsert(
            scope="user",
            provider="telegram",
            key="chat_id",
            is_secret=False,
            value_plain="1111",
            user_id=user_id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        all_tenant_rows = await repo.list_for_tenant()
        openrouter_rows = await repo.list_for_tenant(provider="openrouter")

    assert [(r.provider, r.key) for r in all_tenant_rows] == [
        ("openfigi", "api_key"),
        ("openrouter", "base_url"),
        ("openrouter", "model"),
    ]
    assert [r.key for r in openrouter_rows] == ["base_url", "model"]


# ---------------------------------------------------------------------------
# SS-05: shape invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        # Scope vocabulary.
        (
            {"scope": "global", "is_secret": False, "value_plain": "v"},
            "scope",
        ),
        # Application scope is unreachable through a tenant-scoped session.
        (
            {"scope": "application", "is_secret": False, "value_plain": "v"},
            "scope",
        ),
        # user scope without a user_id …
        (
            {"scope": "user", "is_secret": False, "value_plain": "v"},
            "user_id",
        ),
        # … and a user_id without user scope.
        (
            {
                "scope": "tenant",
                "is_secret": False,
                "value_plain": "v",
                "user_id": "00000000-0000-0000-0000-000000000001",
            },
            "user_id",
        ),
        # Secret row without ciphertext.
        (
            {"scope": "tenant", "is_secret": True, "value_plain": "v"},
            "value_ciphertext",
        ),
        # Config row carrying ciphertext.
        (
            {"scope": "tenant", "is_secret": False, "value_ciphertext": _CIPHERTEXT},
            "value_ciphertext",
        ),
        # Both columns set.
        (
            {
                "scope": "tenant",
                "is_secret": True,
                "value_plain": "v",
                "value_ciphertext": _CIPHERTEXT,
            },
            "value_plain",
        ),
        # Neither column set.
        (
            {"scope": "tenant", "is_secret": False},
            "value_plain",
        ),
    ],
)
async def test_ss05_shape_violations_raise_typed_errors_and_write_nothing(
    app_engine: AsyncEngine, seed_tenant, kwargs: dict, field: str
) -> None:
    from uuid import UUID

    tenant_id = await seed_tenant("SS-05")
    if isinstance(kwargs.get("user_id"), str):
        kwargs = {**kwargs, "user_id": UUID(kwargs["user_id"])}

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        with pytest.raises(ValidationError) as excinfo:
            await repo.upsert(provider="openrouter", key="api_key", **kwargs)
        assert excinfo.value.field == field

    # Nothing reached the database.
    async with tenant_context(app_engine, tenant_id) as session:
        assert await ScopedSettingRepository(session).list_for_tenant() == []


async def test_ss05_the_schema_checks_exist_underneath(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """Defence in depth: bypass the repository and the CHECK still bites."""
    tenant_id = await seed_tenant("SS-05b")

    with pytest.raises(IntegrityError) as excinfo:
        async with tenant_context(app_engine, tenant_id) as session:
            await session.execute(
                insert(ScopedSetting).values(
                    scope="tenant",
                    tenant_id=tenant_id,
                    provider="openrouter",
                    key="api_key",
                    # Claims to be a secret while carrying plaintext.
                    is_secret=True,
                    value_plain="leaked",
                )
            )
    assert "ck_scoped_settings_secret_value_exclusivity" in str(excinfo.value)


async def test_ss05_scope_vocabulary_check_exists_underneath(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-05c")

    with pytest.raises(IntegrityError) as excinfo:
        async with tenant_context(app_engine, tenant_id) as session:
            await session.execute(
                insert(ScopedSetting).values(
                    scope="deployment",
                    tenant_id=tenant_id,
                    provider="openrouter",
                    key="model",
                    is_secret=False,
                    value_plain="m",
                )
            )
    assert "ck_scoped_settings_scope_vocabulary" in str(excinfo.value)


# ---------------------------------------------------------------------------
# SS-06: RLS isolation
# ---------------------------------------------------------------------------


async def test_ss06_tenant_a_cannot_read_tenant_b_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant("SS-06a")
    tenant_b = await seed_tenant("SS-06b")

    async with tenant_context(app_engine, tenant_a) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
            secret_hint="aaaa",
        )
    async with tenant_context(app_engine, tenant_b) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_OTHER_CIPHERTEXT,
            secret_hint="bbbb",
        )

    async with tenant_context(app_engine, tenant_a) as session:
        repo = ScopedSettingRepository(session)
        rows = await repo.list_for_tenant()
        fetched = await repo.get("tenant", "openrouter", "api_key")
        # The raw table is filtered too, not just the repository's queries.
        raw = (await session.execute(select(ScopedSetting))).scalars().all()

    assert [r.secret_hint for r in rows] == ["aaaa"]
    assert fetched is not None and fetched.secret_hint == "aaaa"
    assert {row.tenant_id for row in raw} == {tenant_a}


async def test_ss06_tenant_a_cannot_delete_tenant_b_rows(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_a = await seed_tenant("SS-06c")
    tenant_b = await seed_tenant("SS-06d")

    async with tenant_context(app_engine, tenant_b) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openfigi",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
        )

    async with tenant_context(app_engine, tenant_a) as session:
        deleted = await ScopedSettingRepository(session).delete(
            scope="tenant", provider="openfigi", key="api_key"
        )
    assert deleted is False

    async with tenant_context(app_engine, tenant_b) as session:
        assert len(await ScopedSettingRepository(session).list_for_tenant()) == 1


async def test_ss06_row_count_is_visible_only_through_the_superuser_engine(
    app_engine: AsyncEngine, superuser_engine: AsyncEngine, seed_tenant
) -> None:
    """What ``vault-rotate-key`` relies on: the cross-tenant read needs the superuser."""
    tenant_a = await seed_tenant("SS-06e")
    tenant_b = await seed_tenant("SS-06f")

    for tenant_id in (tenant_a, tenant_b):
        async with tenant_context(app_engine, tenant_id) as session:
            await ScopedSettingRepository(session).upsert(
                scope="tenant",
                provider="openrouter",
                key="api_key",
                is_secret=True,
                value_ciphertext=_CIPHERTEXT,
            )

    async with superuser_engine.connect() as conn:
        total = (
            await conn.execute(text("SELECT count(*) FROM scoped_settings WHERE is_secret"))
        ).scalar_one()
    assert total == 2


# ---------------------------------------------------------------------------
# SS-07: delete and set_enabled
# ---------------------------------------------------------------------------


async def test_ss07_delete_removes_the_row_and_reports_whether_it_did(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-07")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="tenant",
            provider="openfigi",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        assert await repo.delete(scope="tenant", provider="openfigi", key="api_key") is True
        assert await repo.delete(scope="tenant", provider="openfigi", key="api_key") is False

    async with tenant_context(app_engine, tenant_id) as session:
        assert await ScopedSettingRepository(session).list_for_tenant() == []


async def test_ss07_delete_does_not_touch_the_user_row_of_the_same_key(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-07b")
    user_id = await _seed_user(app_engine, tenant_id, "pm@ss07b.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="tenant-model",
        )
        await repo.upsert(
            scope="user",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="user-model",
            user_id=user_id,
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        assert await repo.delete(scope="tenant", provider="openrouter", key="model") is True

    async with tenant_context(app_engine, tenant_id) as session:
        assert len(await ScopedSettingRepository(session).list_for_user(user_id)) == 1


async def test_ss07_set_enabled_flips_the_flag_without_touching_the_value(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-07c")

    async with tenant_context(app_engine, tenant_id) as session:
        created = await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
            secret_hint="1234",
        )

    async with tenant_context(app_engine, tenant_id) as session:
        disabled = await ScopedSettingRepository(session).set_enabled(
            scope="tenant", provider="openrouter", key="api_key", enabled=False
        )

    assert disabled is not None
    assert disabled.id == created.id
    assert disabled.enabled is False
    assert disabled.value_ciphertext == _CIPHERTEXT
    assert disabled.secret_hint == "1234"


async def test_ss07_user_scope_delete_and_set_enabled_stay_on_their_user(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    """The user filter applies to writes too, not only reads."""
    tenant_id = await seed_tenant("SS-07e")
    user_x = await _seed_user(app_engine, tenant_id, "x@ss07e.example")
    user_y = await _seed_user(app_engine, tenant_id, "y@ss07e.example")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        for user_id, value in ((user_x, "1111"), (user_y, "2222")):
            await repo.upsert(
                scope="user",
                provider="telegram",
                key="chat_id",
                is_secret=False,
                value_plain=value,
                user_id=user_id,
            )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        disabled = await repo.set_enabled(
            scope="user",
            provider="telegram",
            key="chat_id",
            enabled=False,
            user_id=user_x,
        )
        assert disabled is not None and disabled.user_id == user_x
        assert (
            await repo.delete(scope="user", provider="telegram", key="chat_id", user_id=user_x)
            is True
        )

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        assert await repo.list_for_user(user_x) == []
        y_rows = await repo.list_for_user(user_y)

    assert [(r.value_plain, r.enabled) for r in y_rows] == [("2222", True)]


async def test_ss07_set_enabled_returns_none_when_no_row_matches(
    app_engine: AsyncEngine, seed_tenant
) -> None:
    tenant_id = await seed_tenant("SS-07d")

    async with tenant_context(app_engine, tenant_id) as session:
        result = await ScopedSettingRepository(session).set_enabled(
            scope="tenant", provider="openfigi", key="api_key", enabled=False
        )
    assert result is None


# ---------------------------------------------------------------------------
# SS-08: the DTO masks its value columns
# ---------------------------------------------------------------------------


async def test_ss08_dto_repr_masks_both_value_columns(app_engine: AsyncEngine, seed_tenant) -> None:
    tenant_id = await seed_tenant("SS-08")

    async with tenant_context(app_engine, tenant_id) as session:
        repo = ScopedSettingRepository(session)
        secret = await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="api_key",
            is_secret=True,
            value_ciphertext=_CIPHERTEXT,
            secret_hint="1234",
        )
        config = await repo.upsert(
            scope="tenant",
            provider="openrouter",
            key="model",
            is_secret=False,
            value_plain="anthropic/claude-sonnet-4.5",
        )

    for rendered in (repr(secret), str(secret), f"{secret}"):
        assert _CIPHERTEXT.decode("utf-8") not in rendered
        assert "value_ciphertext=<set; masked>" in rendered
        assert "value_plain=<unset; masked>" in rendered
        # The hint is meant to be shown.
        assert "'1234'" in rendered

    for rendered in (repr(config), str(config), f"{config}"):
        assert "anthropic/claude-sonnet-4.5" not in rendered
        assert "value_plain=<set; masked>" in rendered
        assert "value_ciphertext=<unset; masked>" in rendered
