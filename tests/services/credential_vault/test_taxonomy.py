# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Provider-taxonomy consistency guard (ADR-0112 §3).

Pure tests — no database, no network. The taxonomy is a hand-written
table that has to agree with three neighbours it cannot import from, so
these tests are the machine check that keeps them from drifting:

* TX-01: the **single policy source** rule, both ways. Every
  ``managed_by_matrix`` provider really is in the capability matrix and
  carries ``None`` for both policy flags (so the matrix cannot be
  contradicted); every non-matrix provider is really absent from the
  matrix (so the taxonomy cannot be second-guessed).
* TX-02: the environment mapping and the vault field set agree — a
  provider the environment source can serve declares exactly the same
  field names as secrets here.
* TX-03: the scope vocabulary is the schema's, and no secret field
  claims the application scope (application secrets live in the
  environment, ADR-0112 §1).
* TX-04: the lookup helpers — the typed unknown-provider error, and the
  secret/config split.
* TX-05: the declarations validate themselves, so a malformed literal
  fails at import rather than at resolution time.
* TX-06: TX-02's twin for the **config** chain (ADR-0112 §4b) — every
  ``_ENV_CONFIG_FIELDS`` entry names a declared, non-secret field of a
  declared provider, so no secret is ever reachable through the config
  chain and no env link dangles.
"""

from __future__ import annotations

import pytest

from core.repositories.scoped_setting_repository import _VALID_SCOPES
from services.credential_vault.taxonomy import (
    PROVIDER_TAXONOMY,
    SCOPES,
    ProviderDeclaration,
    ProviderField,
    declaration_for,
    is_declared,
    secret_fields,
)
from services.investments.credential_resolver import (
    _ENV_CONFIG_FIELDS,
    _ENV_CREDENTIAL_FIELDS,
)
from services.market_data.factory import get_capability_matrix
from services.market_data.provider import UnsupportedCapabilityError


def _matrix_providers() -> frozenset[str]:
    return frozenset(get_capability_matrix().credential_policies)


# ---------------------------------------------------------------------------
# TX-01: one policy source per provider, enforced in both directions
# ---------------------------------------------------------------------------


def test_tx01_matrix_managed_providers_exist_in_the_matrix() -> None:
    matrix_providers = _matrix_providers()
    managed = {
        name for name, declaration in PROVIDER_TAXONOMY.items() if declaration.managed_by_matrix
    }
    assert managed, "v1 declares at least openfigi as matrix-managed."
    assert managed <= matrix_providers


def test_tx01_matrix_managed_providers_carry_no_policy_flags() -> None:
    # The matrix owns env_fallback/optional for these; carrying a value here
    # would be a second, silently-divergent source.
    for name, declaration in PROVIDER_TAXONOMY.items():
        if declaration.managed_by_matrix:
            assert declaration.env_fallback is None, name
            assert declaration.optional is None, name


def test_tx01_non_matrix_providers_are_absent_from_the_matrix() -> None:
    matrix_providers = _matrix_providers()
    non_matrix = {
        name for name, declaration in PROVIDER_TAXONOMY.items() if not declaration.managed_by_matrix
    }
    assert non_matrix, "v1 declares openrouter and telegram outside the matrix."
    assert not (non_matrix & matrix_providers)


def test_tx01_non_matrix_providers_declare_their_own_policy() -> None:
    for name, declaration in PROVIDER_TAXONOMY.items():
        if not declaration.managed_by_matrix:
            assert declaration.env_fallback is not None, name
            assert declaration.optional is not None, name


# ---------------------------------------------------------------------------
# TX-02: the env mapping and the vault field set agree
# ---------------------------------------------------------------------------


def test_tx02_env_field_names_match_the_declared_secret_fields() -> None:
    # openfigi is the only v1 entry; the assertion is written over the whole
    # mapping so a future entry is covered the moment it lands.
    for provider, env_fields in _ENV_CREDENTIAL_FIELDS.items():
        if not is_declared(provider):
            continue
        declared = {field.name for field in secret_fields(provider)}
        assert set(env_fields) == declared, provider


def test_tx02_openfigi_declares_exactly_its_env_field() -> None:
    assert set(_ENV_CREDENTIAL_FIELDS["openfigi"]) == {"api_key"}
    assert [field.name for field in secret_fields("openfigi")] == ["api_key"]


def test_tx02_voice_halves_declare_exactly_their_env_fields() -> None:
    # The two halves are separate providers precisely so §1's unit rule
    # chains each key on its own (ADR-0118 §1) — one secret field each, and
    # the env link names that field and nothing else.
    for provider in ("voice_stt", "voice_tts"):
        assert set(_ENV_CREDENTIAL_FIELDS[provider]) == {"api_key"}, provider
        assert [field.name for field in secret_fields(provider)] == ["api_key"], provider


# ---------------------------------------------------------------------------
# TX-03: the scope vocabulary
# ---------------------------------------------------------------------------


def test_tx03_scope_vocabulary_mirrors_the_repository() -> None:
    # The taxonomy cannot import the repository (that would drag SQLAlchemy
    # into the vault package), so the mirror is pinned here instead.
    assert SCOPES == _VALID_SCOPES


def test_tx03_every_declared_scope_is_in_the_vocabulary() -> None:
    for name, declaration in PROVIDER_TAXONOMY.items():
        for field in declaration.fields:
            assert field.scopes, f"{name}.{field.name} declares no scope"
            assert field.scopes <= SCOPES, f"{name}.{field.name}"


def test_tx03_no_secret_field_claims_the_application_scope() -> None:
    for name, declaration in PROVIDER_TAXONOMY.items():
        for field in declaration.secret_fields:
            assert "application" not in field.scopes, f"{name}.{field.name}"


# ---------------------------------------------------------------------------
# TX-04: lookup helpers
# ---------------------------------------------------------------------------


def test_tx04_unknown_provider_raises_the_house_error() -> None:
    # The same typed error the capability matrix raises for a provider it
    # declares no policy for — one error type across the credential façade.
    assert not is_declared("mystery")
    with pytest.raises(UnsupportedCapabilityError):
        declaration_for("mystery")


def test_tx04_secret_and_config_fields_split_as_declared() -> None:
    openrouter = declaration_for("openrouter")
    assert [field.name for field in openrouter.secret_fields] == ["api_key"]
    # Declaration order is presentational (nothing chains on it) but it is the
    # order the Admin card renders, and ADR-0123 chose it deliberately: the
    # three model rows read as one block, Base URL last.
    assert [field.name for field in openrouter.config_fields] == [
        "model",
        "scraper_model",
        "irene_model",
        "base_url",
    ]
    # The user-scope LLM key is in the model in v1 (no UI) — the scope is
    # declared so F3/F4 need no taxonomy change to expose it.
    api_key = openrouter.field("api_key")
    assert api_key is not None
    assert api_key.scopes == frozenset({"user", "tenant"})


def test_tx04_helpers_accept_an_injected_taxonomy() -> None:
    double = {
        "double": ProviderDeclaration(
            provider="double",
            fields=(ProviderField(name="token", is_secret=True, scopes=frozenset({"tenant"})),),
            managed_by_matrix=False,
            env_fallback=False,
            optional=False,
        )
    }
    assert is_declared("double", double)
    assert not is_declared("openfigi", double)
    assert [field.name for field in secret_fields("double", double)] == ["token"]


def test_tx04_telegram_is_declared_for_f5_with_no_f2_consumer() -> None:
    telegram = declaration_for("telegram")
    assert [field.name for field in telegram.secret_fields] == ["bot_token"]
    chat_id = telegram.field("chat_id")
    assert chat_id is not None
    assert chat_id.scopes == frozenset({"user"})


def test_tx04_voice_declarations_match_adr_0118() -> None:
    # The field sets are deliberately *not* symmetric (ADR-0118 §1, correcting
    # ADR-0112 §3's aspirational sketch): TTS has no ``base_url`` knob (it is
    # OpenAI-specific by design, ADR-0076) and ``voice`` is meaningless for
    # STT. Pinned field by field so a "tidying" symmetry edit fails here.
    expected: dict[str, tuple[list[str], list[str]]] = {
        "voice": ([], ["enabled"]),
        "voice_stt": (["api_key"], ["model", "base_url"]),
        "voice_tts": (["api_key"], ["model", "voice"]),
    }
    for provider, (expected_secrets, expected_configs) in expected.items():
        declaration = declaration_for(provider)
        assert [field.name for field in declaration.secret_fields] == expected_secrets, provider
        assert [field.name for field in declaration.config_fields] == expected_configs, provider
        for field in declaration.fields:
            assert field.scopes == frozenset({"tenant"}), f"{provider}.{field.name}"
        # ADR-0118 §2: env_fallback keeps the single-tenant .env deployment
        # working; optional=False keeps an enabled-but-keyless tenant loud
        # rather than silently voiceless.
        assert declaration.env_fallback is True, provider
        assert declaration.optional is False, provider


# ---------------------------------------------------------------------------
# TX-05: the declarations validate themselves
# ---------------------------------------------------------------------------


def test_tx05_secret_field_may_not_be_application_scoped() -> None:
    with pytest.raises(ValueError, match="application"):
        ProviderField(name="api_key", is_secret=True, scopes=frozenset({"application"}))


def test_tx05_unknown_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scope"):
        ProviderField(name="model", is_secret=False, scopes=frozenset({"deployment"}))


def test_tx05_matrix_managed_declaration_may_not_carry_policy_flags() -> None:
    with pytest.raises(ValueError, match="matrix-managed"):
        ProviderDeclaration(
            provider="openfigi",
            fields=(ProviderField(name="api_key", is_secret=True, scopes=frozenset({"tenant"})),),
            managed_by_matrix=True,
            env_fallback=True,
            optional=True,
        )


def test_tx05_config_only_declaration_is_legal() -> None:
    # ``secret_fields`` is documented as "possibly empty", and ADR-0118 §1
    # leans on that reading: the service-level ``voice.enabled`` switch is a
    # declaration with no credential at all, so one switch gates both halves
    # instead of being duplicated onto each (where the two rows could
    # diverge). A constructor that rejected an empty secret set would take
    # that shape with it.
    switch_only = ProviderDeclaration(
        provider="switch_only",
        fields=(ProviderField(name="enabled", is_secret=False, scopes=frozenset({"tenant"})),),
        managed_by_matrix=False,
        env_fallback=True,
        optional=False,
    )
    assert switch_only.secret_fields == ()
    assert declaration_for("voice").secret_fields == ()


def test_tx05_non_matrix_declaration_must_carry_policy_flags() -> None:
    with pytest.raises(ValueError, match="policy source"):
        ProviderDeclaration(
            provider="openrouter",
            fields=(ProviderField(name="api_key", is_secret=True, scopes=frozenset({"tenant"})),),
            managed_by_matrix=False,
        )


# ---------------------------------------------------------------------------
# TX-06: the config env mapping and the declared config fields agree
# ---------------------------------------------------------------------------


def test_tx06_config_env_fields_are_declared_config_fields() -> None:
    # The config half of TX-02: every environment link the config chain
    # (ADR-0112 §4b) can follow must name a real, non-secret field of a real
    # provider — so a renamed or retired field cannot leave a dangling env var
    # that silently resolves to nothing.
    for provider, env_fields in _ENV_CONFIG_FIELDS.items():
        declaration = declaration_for(provider)
        declared = {field.name for field in declaration.config_fields}
        assert set(env_fields) <= declared, provider


def test_tx06_no_secret_field_has_a_config_env_link() -> None:
    # A secret must never be reachable through the config chain: config values
    # are read as ``value_plain`` and returned unencrypted to the caller.
    for provider, env_fields in _ENV_CONFIG_FIELDS.items():
        secrets = {field.name for field in secret_fields(provider)}
        assert not (set(env_fields) & secrets), provider


def test_tx06_openrouter_declares_exactly_its_config_env_fields() -> None:
    assert set(_ENV_CONFIG_FIELDS["openrouter"]) == {
        "model",
        "base_url",
        "irene_model",
        "scraper_model",
    }
    assert {field.name for field in declaration_for("openrouter").config_fields} == {
        "model",
        "base_url",
        "irene_model",
        "scraper_model",
    }


def test_tx06_voice_config_env_links_are_exact() -> None:
    # Every voice config field carries an environment link (ADR-0118 §1) —
    # the set-equality, not the subset TX-06's table-driven test asserts, is
    # what pins that: an un-linked field would leave a single-tenant .env
    # deployment unable to configure it at all.
    assert set(_ENV_CONFIG_FIELDS["voice"]) == {"enabled"}
    assert set(_ENV_CONFIG_FIELDS["voice_stt"]) == {"model", "base_url"}
    assert set(_ENV_CONFIG_FIELDS["voice_tts"]) == {"model", "voice"}
