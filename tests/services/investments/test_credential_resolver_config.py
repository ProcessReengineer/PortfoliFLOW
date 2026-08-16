# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Config-chain tests for the credential façade (ADR-0112 §4b).

The twin of ``test_credential_resolver.py``, for the *other* half of the
façade. Where :meth:`CredentialResolver.resolve` chains a provider's secret
fields **as a unit** and fails loudly, :meth:`resolve_config` chains **one**
config field at a time and returns ``None`` when it is set nowhere — the
caller owns the default.

Two halves, mirroring the credential file. The first is pure: the chain's
shape, the ``scopes`` restriction, and the typed refusals (an undeclared
provider or key, and — the security property — a *secret* field asked for as
config, which must never resolve through a path that returns plaintext to the
caller). The second runs against the live compose Postgres, because the vault
sources are exactly reads of ``scoped_settings`` through the tenant-scoped
repository: it pins ``user`` → ``tenant`` → ``env`` precedence, the
user-invisible-without-``user_id`` rule, and that a disabled row counts as
absent.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    ScopedSettingRepository,
    UserRepository,
    tenant_context,
)
from services.credential_vault import ProviderDeclaration, ProviderField
from services.investments.credential_resolver import (
    _ENV_CONFIG_FIELDS,
    CredentialResolver,
)
from services.market_data.factory import CapabilityMatrix
from services.market_data.provider import UnsupportedCapabilityError


def _matrix() -> CapabilityMatrix:
    """A matrix with no policies — ``resolve_config`` never consults one."""
    return CapabilityMatrix(providers=(), credential_policies={})


# ---------------------------------------------------------------------------
# Pure half: chain shape, scope restriction, typed refusals
# ---------------------------------------------------------------------------


class TestEnvSource:
    async def test_env_serves_a_declared_config_field(self) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(),
            environ={"SHIRLEY_MODEL": "anthropic/claude-sonnet-4.5"},
        )
        assert await resolver.resolve_config("openrouter", "model") == "anthropic/claude-sonnet-4.5"

    async def test_unset_everywhere_is_none_not_an_error(self) -> None:
        # The caller applies its own default; "nothing configured" is a
        # legitimate answer, not a failure.
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        assert await resolver.resolve_config("openrouter", "model") is None

    async def test_empty_environment_value_counts_as_absent(self) -> None:
        # An empty model id is not a configured model — the same
        # ``os.getenv(...) or ...`` convention the tick always used.
        resolver = CredentialResolver(matrix=_matrix(), environ={"SHIRLEY_MODEL": ""})
        assert await resolver.resolve_config("openrouter", "model") is None

    async def test_every_env_link_resolves(self) -> None:
        # Guards the table itself: each declared link reads its own variable.
        env = {var: f"value-of-{var}" for var in _ENV_CONFIG_FIELDS["openrouter"].values()}
        resolver = CredentialResolver(matrix=_matrix(), environ=env)
        for key, var in _ENV_CONFIG_FIELDS["openrouter"].items():
            assert await resolver.resolve_config("openrouter", key) == f"value-of-{var}"

    async def test_scraper_model_reads_its_own_variable(self) -> None:
        # ADR-0123's one new link. Named explicitly rather than left to the
        # table-driven test above so a *removed* entry fails here too — the
        # Report Scraper's env chain would otherwise go quietly dead.
        resolver = CredentialResolver(
            matrix=_matrix(),
            environ={
                "SCRAPER_MODEL": "anthropic/claude-opus-4-7",
                "SHIRLEY_MODEL": "anthropic/claude-sonnet-4.5",
            },
        )
        assert (
            await resolver.resolve_config("openrouter", "scraper_model", scopes=("env",))
            == "anthropic/claude-opus-4-7"
        )


class TestScopeRestriction:
    async def test_env_only_restriction_skips_the_vault(self) -> None:
        resolver = CredentialResolver(matrix=_matrix(), environ={"IRENE_MODEL": "irene/model"})
        assert (
            await resolver.resolve_config("openrouter", "irene_model", scopes=("env",))
            == "irene/model"
        )

    async def test_tenant_only_restriction_ignores_the_environment(self) -> None:
        # The tick's scope-major chain depends on this: asking at tenant
        # scope must not silently fall through to the environment, or
        # ``tenant model`` could never outrank ``env IRENE_MODEL``.
        resolver = CredentialResolver(
            matrix=_matrix(), environ={"SHIRLEY_MODEL": "shirley/env-model"}
        )
        assert await resolver.resolve_config("openrouter", "model", scopes=("tenant",)) is None

    async def test_unknown_scope_is_a_caller_bug(self) -> None:
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        with pytest.raises(ValueError, match="unknown scope"):
            await resolver.resolve_config("openrouter", "model", scopes=("deployment",))


class TestTypedRefusals:
    async def test_undeclared_provider_raises_the_house_error(self) -> None:
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        with pytest.raises(UnsupportedCapabilityError):
            await resolver.resolve_config("nonesuch", "model")

    async def test_undeclared_key_raises_the_house_error(self) -> None:
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        with pytest.raises(UnsupportedCapabilityError, match="no field"):
            await resolver.resolve_config("openrouter", "temperature")

    async def test_a_secret_field_is_refused_as_config(self) -> None:
        # The security property: config values are returned to the caller in
        # plain text, so a secret must never be reachable this way — not even
        # when the environment happens to hold it.
        resolver = CredentialResolver(matrix=_matrix(), environ={"OPENROUTER_API_KEY": "sk-secret"})
        with pytest.raises(UnsupportedCapabilityError, match="is a secret"):
            await resolver.resolve_config("openrouter", "api_key")

    async def test_injected_taxonomy_is_honoured(self) -> None:
        taxonomy = {
            "custom": ProviderDeclaration(
                provider="custom",
                fields=(
                    ProviderField(name="flavour", is_secret=False, scopes=frozenset({"tenant"})),
                ),
                managed_by_matrix=False,
                env_fallback=True,
                optional=False,
            )
        }
        resolver = CredentialResolver(matrix=_matrix(), environ={}, taxonomy=taxonomy)
        # Declared but unset anywhere, and with no env link declared.
        assert await resolver.resolve_config("custom", "flavour") is None
        with pytest.raises(UnsupportedCapabilityError):
            await resolver.resolve_config("openrouter", "model")


class TestLogging:
    async def test_the_debug_line_names_the_source_and_no_value(self, caplog) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(), environ={"SHIRLEY_MODEL": "anthropic/claude-sonnet-4.5"}
        )
        with caplog.at_level("DEBUG", logger="portfoliflow.market_data.credentials"):
            await resolver.resolve_config("openrouter", "model")

        lines = [r.getMessage() for r in caplog.records if "config resolve" in r.getMessage()]
        assert lines == ["config resolve: provider=openrouter key=model source=env"]
        assert "anthropic/claude-sonnet-4.5" not in lines[0]


# ---------------------------------------------------------------------------
# Vault half: live Postgres
# ---------------------------------------------------------------------------


async def _seed_user(app_engine: AsyncEngine, tenant_id: UUID, email: str) -> UUID:
    """Insert a user in ``tenant_id`` and return its id (for user-scope rows)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


async def _seed_config(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    *,
    scope: str,
    key: str,
    value: str,
    provider: str = "openrouter",
    user_id: UUID | None = None,
    enabled: bool = True,
) -> None:
    """Write one plain config row."""
    async with tenant_context(app_engine, tenant_id) as session:
        await ScopedSettingRepository(session).upsert(
            scope=scope,
            provider=provider,
            key=key,
            is_secret=False,
            value_plain=value,
            user_id=user_id,
            enabled=enabled,
        )


class TestVaultConfigChain:
    async def test_tenant_row_beats_the_environment(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        tenant_id = await seed_tenant("F4-config-tenant-beats-env")
        await _seed_config(app_engine, tenant_id, scope="tenant", key="model", value="tenant/model")

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(), environ={"SHIRLEY_MODEL": "env/model"}, session=session
            )
            assert await resolver.resolve_config("openrouter", "model") == "tenant/model"

    async def test_user_row_beats_the_tenant_row(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        tenant_id = await seed_tenant("F4-config-user-beats-tenant")
        user_id = await _seed_user(app_engine, tenant_id, "f4-user@example.test")
        await _seed_config(app_engine, tenant_id, scope="tenant", key="model", value="tenant/model")
        await _seed_config(
            app_engine, tenant_id, scope="user", key="model", value="user/model", user_id=user_id
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(), environ={"SHIRLEY_MODEL": "env/model"}, session=session
            )
            assert (
                await resolver.resolve_config("openrouter", "model", user_id=user_id)
                == "user/model"
            )

    async def test_a_user_row_is_invisible_without_a_user_id(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        # The beat and the bot resolve for a tenant, not a person: one user's
        # model preference must not leak into a tenant-wide resolution.
        tenant_id = await seed_tenant("F4-config-user-needs-id")
        user_id = await _seed_user(app_engine, tenant_id, "f4-invisible@example.test")
        await _seed_config(
            app_engine, tenant_id, scope="user", key="model", value="user/model", user_id=user_id
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(), environ={"SHIRLEY_MODEL": "env/model"}, session=session
            )
            assert await resolver.resolve_config("openrouter", "model") == "env/model"

    async def test_another_users_row_never_serves_this_user(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        tenant_id = await seed_tenant("F4-config-user-isolation")
        owner = await _seed_user(app_engine, tenant_id, "f4-owner@example.test")
        other = await _seed_user(app_engine, tenant_id, "f4-other@example.test")
        await _seed_config(
            app_engine, tenant_id, scope="user", key="model", value="owner/model", user_id=owner
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(matrix=_matrix(), environ={}, session=session)
            assert await resolver.resolve_config("openrouter", "model", user_id=other) is None

    async def test_a_disabled_row_counts_as_absent(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        tenant_id = await seed_tenant("F4-config-disabled")
        await _seed_config(
            app_engine,
            tenant_id,
            scope="tenant",
            key="model",
            value="tenant/model",
            enabled=False,
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(), environ={"SHIRLEY_MODEL": "env/model"}, session=session
            )
            assert await resolver.resolve_config("openrouter", "model") == "env/model"

    async def test_a_tenant_row_of_another_tenant_is_invisible(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        # RLS does the work; the assertion is that resolve_config does not
        # somehow reach around it.
        one = await seed_tenant("F4-config-rls-one")
        two = await seed_tenant("F4-config-rls-two")
        await _seed_config(app_engine, one, scope="tenant", key="model", value="one/model")

        async with tenant_context(app_engine, two) as session:
            resolver = CredentialResolver(matrix=_matrix(), environ={}, session=session)
            assert await resolver.resolve_config("openrouter", "model") is None

    async def test_base_url_declares_no_user_scope_so_a_user_row_cannot_serve(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        # ``base_url`` is tenant-only in the taxonomy, so the chain skips the
        # user source entirely even when a user_id is supplied.
        tenant_id = await seed_tenant("F4-config-base-url-scope")
        user_id = await _seed_user(app_engine, tenant_id, "f4-baseurl@example.test")
        await _seed_config(
            app_engine,
            tenant_id,
            scope="tenant",
            key="base_url",
            value="https://tenant.example/api/v1",
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(matrix=_matrix(), environ={}, session=session)
            assert (
                await resolver.resolve_config("openrouter", "base_url", user_id=user_id)
                == "https://tenant.example/api/v1"
            )

    async def test_a_resolver_without_a_session_has_no_vault_sources(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        tenant_id = await seed_tenant("F4-config-no-session")
        await _seed_config(app_engine, tenant_id, scope="tenant", key="model", value="tenant/model")

        # No session: the environment is the only source, exactly as for
        # ``resolve`` (a CLI probe, a DB-less test rig).
        resolver = CredentialResolver(matrix=_matrix(), environ={"SHIRLEY_MODEL": "env/model"})
        assert await resolver.resolve_config("openrouter", "model") == "env/model"

    async def test_the_vault_source_needs_no_master_key(
        self, app_engine: AsyncEngine, seed_tenant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Config rows are plaintext, so an unconfigured vault is irrelevant
        # to this half of the façade — no cipher is ever constructed.
        from services.credential_vault import MASTER_KEY_ENV_VAR

        monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
        tenant_id = await seed_tenant("F4-config-no-master-key")
        await _seed_config(app_engine, tenant_id, scope="tenant", key="model", value="tenant/model")

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(matrix=_matrix(), environ={}, session=session)
            assert await resolver.resolve_config("openrouter", "model") == "tenant/model"

    async def test_a_secret_row_squatting_a_config_key_is_declined(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        # Belt and braces. The repository is taxonomy-blind, so a secret row
        # *can* be written under a config key; the config source must decline
        # it rather than treat the row as configured — otherwise a squatting
        # row would shadow the environment and break the chain silently.
        tenant_id = await seed_tenant("F4-config-secret-row")
        async with tenant_context(app_engine, tenant_id) as session:
            await ScopedSettingRepository(session).upsert(
                scope="tenant",
                provider="openrouter",
                key="model",
                is_secret=True,
                value_ciphertext=b"not-a-real-token",
                secret_hint="oken",
            )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(), environ={"SHIRLEY_MODEL": "env/model"}, session=session
            )
            # Declined, and the chain continues to the environment.
            assert await resolver.resolve_config("openrouter", "model") == "env/model"
