# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Chain tests for the web research resolution (ADR-0132).

Pure and environment-only: no vault, no Postgres. The façade is built
without a session, so the ``env`` source is the only one in the chain —
which is exactly the degraded path a DB-less host takes, and enough to
pin the *shape* of the chain (research's field before the shared one,
tenant scope before environment). The vault halves of the same chain are
pinned once, for all its consumers, in
``tests/services/investments/test_credential_resolver_config.py``.

The two error properties matter more than the happy path: a missing
credential must raise the typed, operator-facing
:class:`ResearchLLMUnavailableError`, and a vault that will not decrypt
must **not** be wrapped into it — a rotated master key is an emergency,
not a "configure me" nudge.
"""

from __future__ import annotations

import pytest

from services.credential_vault import VaultDecryptError
from services.investments.credential_resolver import CredentialResolver
from services.market_data.factory import CapabilityMatrix
from services.web_research.llm import (
    _DEFAULT_OPENROUTER_BASE_URL,
    _DEFAULT_RESEARCH_MODEL,
    NO_RESEARCH_LLM_MESSAGE,
    ResearchLLMUnavailableError,
    resolve_research_llm,
    resolve_research_model,
)


def _matrix() -> CapabilityMatrix:
    """A matrix with no policies — this path never consults one."""
    return CapabilityMatrix(providers=(), credential_policies={})


def _resolver(**env: str) -> CredentialResolver:
    """A session-less façade over exactly ``env`` — the degraded host."""
    return CredentialResolver(matrix=_matrix(), environ=env)


class TestModelChain:
    async def test_research_model_outranks_the_shirley_model(self) -> None:
        resolved = await resolve_research_model(
            _resolver(
                RESEARCH_MODEL="anthropic/claude-haiku-4-5",
                SHIRLEY_MODEL="anthropic/claude-sonnet-4.5",
            )
        )
        assert resolved == "anthropic/claude-haiku-4-5"

    async def test_shirley_model_serves_when_research_model_is_unset(self) -> None:
        """The pre-ADR-0132 behaviour is what an unconfigured operator keeps."""
        resolved = await resolve_research_model(
            _resolver(SHIRLEY_MODEL="anthropic/claude-sonnet-4.5")
        )
        assert resolved == "anthropic/claude-sonnet-4.5"

    async def test_built_in_default_when_neither_is_set(self) -> None:
        assert await resolve_research_model(_resolver()) == _DEFAULT_RESEARCH_MODEL


class TestAssembledResolution:
    async def test_base_url_falls_back_to_the_module_default(self) -> None:
        """``services/`` cannot read ``WebSettings``; the constant stands in."""
        llm = await resolve_research_llm(
            _resolver(OPENROUTER_API_KEY="k", SHIRLEY_MODEL="m"),
            tenant_id=None,
            user_id=None,
        )
        assert llm.base_url == _DEFAULT_OPENROUTER_BASE_URL
        assert llm.api_key == "k"
        assert llm.model == "m"

    async def test_base_url_from_the_environment_wins_over_the_default(self) -> None:
        llm = await resolve_research_llm(
            _resolver(
                OPENROUTER_API_KEY="k",
                OPENROUTER_BASE_URL="https://proxy.internal/v1",
            ),
            tenant_id=None,
            user_id=None,
        )
        assert llm.base_url == "https://proxy.internal/v1"
        assert llm.model == _DEFAULT_RESEARCH_MODEL


class TestCredentialShortfall:
    async def test_no_key_in_any_scope_raises_the_typed_error(self) -> None:
        with pytest.raises(ResearchLLMUnavailableError) as excinfo:
            await resolve_research_llm(_resolver(), tenant_id=None, user_id=None)
        # The message is the one the tool puts in front of an operator, so it
        # is part of the contract, not an implementation detail.
        assert str(excinfo.value) == NO_RESEARCH_LLM_MESSAGE
        assert "Admin → Providers & Credentials" in str(excinfo.value)

    async def test_vault_decrypt_error_is_not_wrapped(self) -> None:
        """A rotated or wrong master key must never read as an absent key."""

        class _ExplodingResolver:
            async def resolve(self, provider, *, tenant_id=None, user_id=None):
                raise VaultDecryptError("master key does not decrypt this row")

        with pytest.raises(VaultDecryptError):
            await resolve_research_llm(
                _ExplodingResolver(),  # type: ignore[arg-type]
                tenant_id=None,
                user_id=None,
            )
