# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Credential-resolver tests (ADR-0095 §1, §3; ADR-0112 §1, §4a).

Two halves. The first is pure — no DB, no network: the resolver reads an
injected ``environ`` mapping and the per-provider credential policy, and returns
one of three outcomes (a masked :class:`ProviderCredential`, an explicit
:class:`NoCredential`, or a typed :class:`CredentialUnavailableError`). Those
tests pin each outcome, the env-only source list a resolver built **without a
session** still has, and — the security property — that the payload never
reaches ``repr`` or the log line.

The second half exercises the Stage-2 vault sources against the live compose
Postgres, because that is what they are: reads of ``scoped_settings`` through
the tenant-scoped repository inside the caller's ``tenant_context``. They pin
the resolution order (``vault-user`` → ``vault-tenant`` → ``env``), the ADR-0112
§1 completeness rule and the no-cross-scope-mixing property that falls out of
it, the two operator situations a vault has (unconfigured → one WARNING and
fall-through; wrong key → a loud, propagating :class:`VaultDecryptError`), and
the openfigi path end to end including RLS isolation between tenants.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories import (
    ScopedSettingRepository,
    UserRepository,
    tenant_context,
)
from services.credential_vault import (
    MASTER_KEY_ENV_VAR,
    ProviderDeclaration,
    ProviderField,
    VaultCipher,
    VaultDecryptError,
)
from services.investments.credential_resolver import (
    _ENV_CREDENTIAL_FIELDS,
    CredentialResolver,
    CredentialUnavailableError,
    NoCredential,
    ProviderCredential,
)
from services.market_data.factory import CapabilityMatrix, CredentialPolicy
from services.market_data.provider import UnsupportedCapabilityError

_LOGGER_NAME = "portfoliflow.market_data.credentials"

_MASTER_KEY = Fernet.generate_key().decode()
_OTHER_KEY = Fernet.generate_key().decode()

#: A two-secret-field provider that does not exist in v1 — the completeness and
#: no-cross-scope-mixing rules of ADR-0112 §1 need one, and inventing a
#: declaration is honest where inventing a *shipped* provider would not be.
_TWO_FIELD_TAXONOMY: dict[str, ProviderDeclaration] = {
    "twofield": ProviderDeclaration(
        provider="twofield",
        fields=(
            ProviderField(name="account", is_secret=True, scopes=frozenset({"tenant"})),
            ProviderField(name="secret", is_secret=True, scopes=frozenset({"tenant"})),
        ),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    )
}

_TWO_FIELD_ENV = {"account": "TWOFIELD_ACCOUNT", "secret": "TWOFIELD_SECRET"}


def _matrix(**policies: CredentialPolicy) -> CapabilityMatrix:
    """Build a matrix carrying only the given credential policies.

    ``providers`` is empty: the resolver consults only ``credential_policy``,
    never routing, so no routing entry is needed to exercise it.
    """
    return CapabilityMatrix(providers=(), credential_policies=policies)


def _openfigi_policy() -> CredentialPolicy:
    return CredentialPolicy(provider="openfigi", requires=True, env_fallback=True, optional=True)


# ---------------------------------------------------------------------------
# Pure half: outcomes, order, masking
# ---------------------------------------------------------------------------


class TestOutcomes:
    async def test_env_credential_delivered(self) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(openfigi=_openfigi_policy()),
            environ={"OPENFIGI_API_KEY": "secret-key"},
        )
        result = await resolver.resolve("openfigi")
        assert isinstance(result, ProviderCredential)
        assert result.provider == "openfigi"
        assert result.payload == {"api_key": "secret-key"}

    async def test_optional_credential_absent_yields_no_credential(self) -> None:
        # OpenFIGI's key is optional (keyless = lower rate limit): absent is a
        # clean no-credential result, NOT an error (ADR-0095 §3 nuance).
        resolver = CredentialResolver(
            matrix=_matrix(openfigi=_openfigi_policy()),
            environ={},
        )
        result = await resolver.resolve("openfigi")
        assert isinstance(result, NoCredential)
        assert result.provider == "openfigi"

    async def test_no_credentials_required_yields_no_credential(self) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(yahoo=CredentialPolicy.none("yahoo")),
            # An ambient key must not turn a no-credential provider into one.
            environ={"OPENFIGI_API_KEY": "secret-key"},
        )
        result = await resolver.resolve("yahoo")
        assert isinstance(result, NoCredential)

    async def test_forbidden_fallback_without_tenant_source_raises(self) -> None:
        # A hypothetical tenant-licensed provider: env fallback forbidden and no
        # vault source bound → the typed failure, never a silent skip or
        # global-key substitution (ADR-0095 §1).
        forbidden = CredentialPolicy(
            provider="bloomberg",
            requires=True,
            env_fallback=False,
            optional=False,
        )
        resolver = CredentialResolver(matrix=_matrix(bloomberg=forbidden), environ={})
        with pytest.raises(CredentialUnavailableError):
            await resolver.resolve("bloomberg", tenant_id=uuid4())

    async def test_unknown_provider_has_no_policy(self) -> None:
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        with pytest.raises(UnsupportedCapabilityError):
            await resolver.resolve("mystery")


class TestPolicySource:
    """Which of the two declarations owns a provider's policy (ADR-0112 §3)."""

    async def test_taxonomy_supplies_the_policy_for_a_non_matrix_provider(self) -> None:
        # openrouter is absent from the capability matrix; its policy
        # (requires=True from the secret field, optional=False) comes from the
        # taxonomy, so an unresolvable credential is a hard failure.
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        with pytest.raises(CredentialUnavailableError):
            await resolver.resolve("openrouter", tenant_id=uuid4())

    async def test_matrix_still_owns_a_matrix_managed_provider(self) -> None:
        # openfigi is declared in BOTH tables, but the taxonomy carries no
        # policy flags for it — the matrix's `optional: true` must win, so the
        # same absence is a NoCredential rather than an error.
        resolver = CredentialResolver(matrix=_matrix(openfigi=_openfigi_policy()), environ={})
        assert isinstance(await resolver.resolve("openfigi"), NoCredential)

    async def test_provider_with_only_config_fields_requires_no_credential(self) -> None:
        config_only = {
            "configonly": ProviderDeclaration(
                provider="configonly",
                fields=(
                    ProviderField(name="model", is_secret=False, scopes=frozenset({"tenant"})),
                ),
                managed_by_matrix=False,
                env_fallback=True,
                optional=False,
            )
        }
        resolver = CredentialResolver(matrix=_matrix(), environ={}, taxonomy=config_only)
        result = await resolver.resolve("configonly")
        assert isinstance(result, NoCredential)


class TestResolutionOrder:
    async def test_source_list_is_env_only_without_a_session(self) -> None:
        # Without a session the resolver keeps the Stage-1 shape exactly: the
        # environment alone, so a DB-free caller behaves as it always did.
        resolver = CredentialResolver(matrix=_matrix(), environ={})
        labels = [label for label, _ in resolver._sources]
        assert labels == ["env"]

    async def test_vault_sources_prepend_when_a_session_is_bound(
        self, app_engine: AsyncEngine, seed_tenant
    ) -> None:
        tenant_id = await seed_tenant("F2-order")
        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(matrix=_matrix(), environ={}, session=session)
            labels = [label for label, _ in resolver._sources]
        assert labels == ["vault-user", "vault-tenant", "env"]


class TestMasking:
    def test_payload_masked_in_repr_and_str(self) -> None:
        cred = ProviderCredential(provider="openfigi", payload={"api_key": "super-secret"})
        assert "super-secret" not in repr(cred)
        assert "super-secret" not in str(cred)
        # The provider and a field count are fine to show.
        assert "openfigi" in repr(cred)

    def test_payload_is_read_only(self) -> None:
        cred = ProviderCredential(provider="openfigi", payload={"api_key": "super-secret"})
        with pytest.raises(TypeError):
            cred.payload["api_key"] = "tampered"  # type: ignore[index]

    async def test_payload_never_logged(self, caplog) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(openfigi=_openfigi_policy()),
            environ={"OPENFIGI_API_KEY": "super-secret"},
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            await resolver.resolve("openfigi")
        lines = [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME]
        # Exactly one structured resolution line, stating the source, not value.
        assert len(lines) == 1
        assert "source=env" in lines[0]
        # The value appears nowhere in the captured log.
        assert all("super-secret" not in r.getMessage() for r in caplog.records)

    async def test_log_states_none_required_source(self, caplog) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(yahoo=CredentialPolicy.none("yahoo")),
            environ={},
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            await resolver.resolve("yahoo")
        assert any("source=none-required" in r.getMessage() for r in caplog.records)

    async def test_log_states_optional_absent_source(self, caplog) -> None:
        resolver = CredentialResolver(
            matrix=_matrix(openfigi=_openfigi_policy()),
            environ={},
        )
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            await resolver.resolve("openfigi")
        assert any("source=none-optional-absent" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Vault half: live Postgres, ADR-0112 §4a
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_key(monkeypatch: pytest.MonkeyPatch) -> VaultCipher:
    """Configure the master key for the test and return the matching cipher."""
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _MASTER_KEY)
    return VaultCipher(_MASTER_KEY)


async def _seed_user(app_engine: AsyncEngine, tenant_id: UUID, email: str) -> UUID:
    """Insert a user in ``tenant_id`` and return its id (for user-scope rows)."""
    async with tenant_context(app_engine, tenant_id) as session:
        user = await UserRepository(session).create(email=email, password_hash="x" * 8)
    return user.id


async def _seed_secret(
    app_engine: AsyncEngine,
    tenant_id: UUID,
    cipher: VaultCipher,
    *,
    scope: str,
    provider: str,
    key: str,
    value: str,
    user_id: UUID | None = None,
    enabled: bool = True,
) -> UUID:
    """Write one encrypted vault row and return its id."""
    async with tenant_context(app_engine, tenant_id) as session:
        row = await ScopedSettingRepository(session).upsert(
            scope=scope,
            provider=provider,
            key=key,
            is_secret=True,
            value_ciphertext=cipher.encrypt(value),
            secret_hint=value[-4:],
            user_id=user_id,
            enabled=enabled,
        )
    return row.id


def _sources(caplog) -> list[str]:
    """Return the ``source=…`` value of every resolution line captured."""
    return [
        record.getMessage().rsplit("source=", 1)[1]
        for record in caplog.records
        if record.name == _LOGGER_NAME and "source=" in record.getMessage()
    ]


class TestVaultOrder:
    async def test_tenant_row_beats_the_environment(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher, caplog
    ) -> None:
        tenant_id = await seed_tenant("F2-tenant-beats-env")
        await _seed_secret(
            app_engine,
            tenant_id,
            vault_key,
            scope="tenant",
            provider="openfigi",
            key="api_key",
            value="vault-key",
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                session=session,
                environ={"OPENFIGI_API_KEY": "env-key"},
            )
            with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
                result = await resolver.resolve("openfigi", tenant_id=tenant_id)

        assert isinstance(result, ProviderCredential)
        # The env var is set and deliberately ignored — that IS the order proof.
        assert result.payload == {"api_key": "vault-key"}
        assert _sources(caplog) == ["vault-tenant"]
        assert all("vault-key" not in r.getMessage() for r in caplog.records)

    async def test_user_row_beats_the_tenant_row(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher, caplog
    ) -> None:
        # openrouter.api_key is the one v1 secret field declared at BOTH user
        # and tenant scope (ADR-0112 §3), so it is what pins the inner order.
        tenant_id = await seed_tenant("F2-user-beats-tenant")
        user_id = await _seed_user(app_engine, tenant_id, "f2-user@example.test")
        for scope, value, owner in (
            ("tenant", "tenant-key", None),
            ("user", "user-key", user_id),
        ):
            await _seed_secret(
                app_engine,
                tenant_id,
                vault_key,
                scope=scope,
                provider="openrouter",
                key="api_key",
                value=value,
                user_id=owner,
            )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(session=session, environ={})
            with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
                with_user = await resolver.resolve(
                    "openrouter", tenant_id=tenant_id, user_id=user_id
                )
                without_user = await resolver.resolve("openrouter", tenant_id=tenant_id)

        assert isinstance(with_user, ProviderCredential)
        assert with_user.payload == {"api_key": "user-key"}
        # No user in the chain: the user-scope source does not fire at all.
        assert isinstance(without_user, ProviderCredential)
        assert without_user.payload == {"api_key": "tenant-key"}
        assert _sources(caplog) == ["vault-user", "vault-tenant"]

    async def test_a_users_row_does_not_serve_another_user(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher
    ) -> None:
        tenant_id = await seed_tenant("F2-user-isolation")
        owner = await _seed_user(app_engine, tenant_id, "f2-owner@example.test")
        other = await _seed_user(app_engine, tenant_id, "f2-other@example.test")
        await _seed_secret(
            app_engine,
            tenant_id,
            vault_key,
            scope="user",
            provider="openrouter",
            key="api_key",
            value="owner-key",
            user_id=owner,
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(session=session, environ={})
            with pytest.raises(CredentialUnavailableError):
                await resolver.resolve("openrouter", tenant_id=tenant_id, user_id=other)


class TestCompletenessAndNoMixing:
    async def test_one_field_per_scope_never_combines(
        self,
        app_engine: AsyncEngine,
        seed_tenant,
        vault_key: VaultCipher,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The tenant scope holds `account`, the environment holds `secret`.
        # Were mixing allowed, that would be a complete credential. It is not:
        # the tenant source declines as a whole (incomplete there), the env
        # source declines as a whole (incomplete there too), and the
        # non-optional policy turns that into the typed failure.
        monkeypatch.setitem(_ENV_CREDENTIAL_FIELDS, "twofield", _TWO_FIELD_ENV)
        tenant_id = await seed_tenant("F2-no-mixing")
        await _seed_secret(
            app_engine,
            tenant_id,
            vault_key,
            scope="tenant",
            provider="twofield",
            key="account",
            value="tenant-account",
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(),
                environ={"TWOFIELD_SECRET": "env-secret"},
                session=session,
                taxonomy=_TWO_FIELD_TAXONOMY,
            )
            with pytest.raises(CredentialUnavailableError):
                await resolver.resolve("twofield", tenant_id=tenant_id)

    async def test_both_fields_in_one_scope_resolve_together(
        self,
        app_engine: AsyncEngine,
        seed_tenant,
        vault_key: VaultCipher,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The positive control for the test above: complete in one scope, so
        # the scope serves — and the environment is never consulted.
        monkeypatch.setitem(_ENV_CREDENTIAL_FIELDS, "twofield", _TWO_FIELD_ENV)
        tenant_id = await seed_tenant("F2-complete-scope")
        for key, value in (("account", "tenant-account"), ("secret", "tenant-secret")):
            await _seed_secret(
                app_engine,
                tenant_id,
                vault_key,
                scope="tenant",
                provider="twofield",
                key=key,
                value=value,
            )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                matrix=_matrix(),
                environ={"TWOFIELD_ACCOUNT": "env-account", "TWOFIELD_SECRET": "env-secret"},
                session=session,
                taxonomy=_TWO_FIELD_TAXONOMY,
            )
            result = await resolver.resolve("twofield", tenant_id=tenant_id)

        assert isinstance(result, ProviderCredential)
        assert result.payload == {"account": "tenant-account", "secret": "tenant-secret"}

    async def test_a_disabled_row_counts_as_absent(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher, caplog
    ) -> None:
        tenant_id = await seed_tenant("F2-disabled-row")
        await _seed_secret(
            app_engine,
            tenant_id,
            vault_key,
            scope="tenant",
            provider="openfigi",
            key="api_key",
            value="vault-key",
            enabled=False,
        )

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                session=session,
                environ={"OPENFIGI_API_KEY": "env-key"},
            )
            with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
                result = await resolver.resolve("openfigi", tenant_id=tenant_id)

        assert isinstance(result, ProviderCredential)
        assert result.payload == {"api_key": "env-key"}
        assert _sources(caplog) == ["env"]


class TestVaultOperatorSituations:
    async def test_unconfigured_vault_warns_once_and_falls_through(
        self,
        app_engine: AsyncEngine,
        seed_tenant,
        monkeypatch: pytest.MonkeyPatch,
        caplog,
    ) -> None:
        monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
        tenant_id = await seed_tenant("F2-vault-unconfigured")

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                session=session,
                environ={"OPENFIGI_API_KEY": "env-key"},
            )
            with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
                first = await resolver.resolve("openfigi", tenant_id=tenant_id)
                second = await resolver.resolve("openfigi", tenant_id=tenant_id)

        warnings = [
            r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING
        ]
        # One per resolver instance, not one per resolution and not one per
        # vault source (ADR-0112 §2).
        assert len(warnings) == 1
        assert MASTER_KEY_ENV_VAR in warnings[0].getMessage()
        assert isinstance(first, ProviderCredential)
        assert isinstance(second, ProviderCredential)
        assert first.payload == second.payload == {"api_key": "env-key"}

    async def test_wrong_master_key_propagates_and_never_falls_back(
        self,
        app_engine: AsyncEngine,
        seed_tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tenant_id = await seed_tenant("F2-wrong-key")
        row_id = await _seed_secret(
            app_engine,
            tenant_id,
            VaultCipher(_MASTER_KEY),
            scope="tenant",
            provider="openfigi",
            key="api_key",
            value="vault-key",
        )
        # The row was written under one key; the deployment now carries another
        # — a mis-rotated or mis-restored vault.
        monkeypatch.setenv(MASTER_KEY_ENV_VAR, _OTHER_KEY)

        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                session=session,
                # Set, and deliberately NOT used: falling back here would hide
                # the operator's problem behind a working-looking resolution.
                environ={"OPENFIGI_API_KEY": "env-key"},
            )
            with pytest.raises(VaultDecryptError) as excinfo:
                await resolver.resolve("openfigi", tenant_id=tenant_id)

        message = str(excinfo.value)
        assert str(row_id) in message
        assert "openfigi" in message
        assert "vault-key" not in message
        assert "env-key" not in message


class TestOpenFigiEndToEnd:
    """The one live credentialed consumer, on the shipped capability matrix."""

    async def test_no_row_and_env_set_resolves_from_the_environment(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher, caplog
    ) -> None:
        tenant_id = await seed_tenant("F2-openfigi-env")
        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(
                session=session,
                environ={"OPENFIGI_API_KEY": "env-key"},
            )
            with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
                result = await resolver.resolve("openfigi", tenant_id=tenant_id)

        assert isinstance(result, ProviderCredential)
        assert result.payload == {"api_key": "env-key"}
        assert _sources(caplog) == ["env"]

    async def test_neither_source_yields_no_credential(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher, caplog
    ) -> None:
        tenant_id = await seed_tenant("F2-openfigi-absent")
        async with tenant_context(app_engine, tenant_id) as session:
            resolver = CredentialResolver(session=session, environ={})
            with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
                result = await resolver.resolve("openfigi", tenant_id=tenant_id)

        # Optional per the shipped matrix: keyless, not a failure.
        assert isinstance(result, NoCredential)
        assert _sources(caplog) == ["none-optional-absent"]

    async def test_rls_hides_another_tenants_row(
        self, app_engine: AsyncEngine, seed_tenant, vault_key: VaultCipher
    ) -> None:
        tenant_a = await seed_tenant("F2-openfigi-rls-a")
        tenant_b = await seed_tenant("F2-openfigi-rls-b")
        await _seed_secret(
            app_engine,
            tenant_a,
            vault_key,
            scope="tenant",
            provider="openfigi",
            key="api_key",
            value="tenant-a-key",
        )

        async with tenant_context(app_engine, tenant_b) as session:
            resolver = CredentialResolver(session=session, environ={})
            result = await resolver.resolve("openfigi", tenant_id=tenant_b)

        # Tenant B's session cannot see tenant A's row, so the vault declines
        # and the optional policy yields the keyless outcome.
        assert isinstance(result, NoCredential)

        async with tenant_context(app_engine, tenant_a) as session:
            resolver = CredentialResolver(session=session, environ={})
            own = await resolver.resolve("openfigi", tenant_id=tenant_a)

        assert isinstance(own, ProviderCredential)
        assert own.payload == {"api_key": "tenant-a-key"}
