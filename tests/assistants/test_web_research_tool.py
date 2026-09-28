# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration tests for :mod:`services.tools.web_research_tool`.

Covers tool registration (class + wrapping flag) against the application
singleton, gating interaction with other tool classes per ADR-0022, and —
since ADR-0132 — the per-call LLM resolution the wrapper performs before it
runs the pipeline.

The resolution tests come in both shapes the wrapper supports: without a
tool-execution context (the degraded, environment-only path a desktop or
DB-less host takes) and with one (the web and Telegram path, where a
tenant-scope ``research_model`` row must outrank the environment). The
second needs Postgres and skips cleanly without it, like
``test_investment_tools.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from core.repositories import ScopedSettingRepository, tenant_context
from services.tool_classes import ToolClass
from services.tool_registry import ToolRegistry, get_tool_registry
from services.tools._tool_context import (
    ToolExecutionContext,
    clear_tool_context,
    set_tool_context,
)
from services.web_research.llm import NO_RESEARCH_LLM_MESSAGE

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")


@pytest.fixture(autouse=True)
def _clean_context() -> Generator[None, None, None]:
    """Clear the tool-execution context before and after every test.

    The wrapper reads the context to decide whether it can resolve against a
    vault, so a context leaked from another module would silently turn the
    environment-only tests into DB-backed ones.
    """
    clear_tool_context()
    yield
    clear_tool_context()


class TestRegistrationOnSingleton:
    """The real tool module registers against the singleton at import time."""

    def test_tool_registered_with_correct_class(self) -> None:
        # Importing the module triggers registration.
        import services.tools.web_research_tool  # noqa: F401

        reg = get_tool_registry()
        assert reg.has_tool("web_research")
        assert reg.get_tool_class("web_research") is ToolClass.READ_EXTERNAL_UNTRUSTED

    def test_tool_wraps_result_as_untrusted(self) -> None:
        import services.tools.web_research_tool  # noqa: F401

        reg = get_tool_registry()
        assert reg._tools["web_research"]["wraps_result_as_untrusted"] is True

    def test_description_reflects_rss_behaviour(self) -> None:
        import services.tools.web_research_tool  # noqa: F401

        reg = get_tool_registry()
        description = reg._tools["web_research"]["description"]
        # ADR-0024: tool description must name RSS-feed resolution so the
        # LLM does not misinterpret this as an open-web search tool.
        assert "rss" in description.lower() or "feeds" in description.lower()
        assert "does not perform open web search" in description.lower()


class TestGatingWithFreshRegistry:
    """Gating test uses a fresh registry with a manually-registered copy of
    the tool function so the singleton is not disturbed."""

    def _make_registry(self) -> ToolRegistry:
        from services.tools.web_research_tool import web_research

        reg = ToolRegistry()
        reg.register_tool(
            name="web_research",
            function=web_research,
            description="test",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
            wraps_result_as_untrusted=True,
        )
        reg.register_tool(
            name="fake_write",
            function=lambda: "wrote",
            description="w",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.WRITE_INTERNAL,
        )
        return reg

    def test_output_is_wrapped_in_external_content(self) -> None:
        reg = self._make_registry()

        fake_service = MagicMock()
        fake_service.research.return_value = []

        with patch(
            "services.tools.web_research_tool.WebResearchService",
            return_value=fake_service,
        ):
            reg.begin_turn()
            result = reg.execute_tool("web_research", {"query": "ECB"})

        assert result.startswith("<external_content ")
        assert 'trust="untrusted"' in result
        assert result.endswith("</external_content>")

    def test_write_internal_locked_after_web_research_call(self) -> None:
        """After web_research runs, WRITE_INTERNAL is refused for the rest
        of the turn (ADR-0022 gating)."""
        reg = self._make_registry()

        fake_service = MagicMock()
        fake_service.research.return_value = []

        with patch(
            "services.tools.web_research_tool.WebResearchService",
            return_value=fake_service,
        ):
            reg.begin_turn()
            result = reg.execute_tool("web_research", {"query": "anything"})
            assert "<external_content" in result

            refusal = reg.execute_tool("fake_write", {})
            assert "locked" in refusal.lower()
            assert "ADR-0022" in refusal


# ---------------------------------------------------------------------------
# Per-call LLM resolution (ADR-0132)
# ---------------------------------------------------------------------------


def _patched_service() -> MagicMock:
    """A service double whose ``research`` returns no results."""
    fake_service = MagicMock()
    fake_service.research.return_value = []
    return fake_service


class TestResolutionWithoutContext:
    """The degraded path: no context, so the environment is the only source."""

    def test_env_key_resolves_and_reaches_the_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``research`` is handed a ``ResolvedLLM`` carrying the env model."""
        from services.tools.web_research_tool import web_research

        monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
        monkeypatch.setenv("RESEARCH_MODEL", "anthropic/claude-haiku-4-5")
        fake_service = _patched_service()

        with patch(
            "services.tools.web_research_tool.WebResearchService",
            return_value=fake_service,
        ):
            result = web_research("ECB")

        fake_service.research.assert_called_once()
        llm = fake_service.research.call_args.kwargs["llm"]
        assert llm.model == "anthropic/claude-haiku-4-5"
        assert llm.api_key == "env-key"
        # Still an ordinary "no results" envelope — resolution succeeded.
        assert json.loads(result)["body"] != NO_RESEARCH_LLM_MESSAGE

    def test_no_key_anywhere_reports_in_band_without_running_the_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing credential is an envelope body, never a raise.

        And the pipeline must not run: fetching feeds for a call that can
        never reach an LLM would spend the network for nothing.
        """
        from services.tools.web_research_tool import web_research

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        fake_service = _patched_service()

        with patch(
            "services.tools.web_research_tool.WebResearchService",
            return_value=fake_service,
        ):
            result = web_research("ECB")

        fake_service.research.assert_not_called()
        # The envelope is JSON, so read the body out rather than matching the
        # raw string: ``json.dumps`` escapes the message's arrow to ``\u2192``.
        assert json.loads(result)["body"] == NO_RESEARCH_LLM_MESSAGE

    def test_the_shortfall_envelope_still_wraps_as_untrusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR-0022 wrapping is unconditional — the refusal is external too."""
        from services.tools.web_research_tool import web_research

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        reg = ToolRegistry()
        reg.register_tool(
            name="web_research",
            function=web_research,
            description="test",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
            wraps_result_as_untrusted=True,
        )

        with patch(
            "services.tools.web_research_tool.WebResearchService",
            return_value=_patched_service(),
        ):
            reg.begin_turn()
            result = reg.execute_tool("web_research", {"query": "ECB"})

        assert result.startswith("<external_content ")
        assert result.endswith("</external_content>")
        assert NO_RESEARCH_LLM_MESSAGE in result


# ---------------------------------------------------------------------------
# DB-backed: the tenant's row outranks the environment
# ---------------------------------------------------------------------------


def _require_db() -> None:
    if not DATABASE_URL or not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL and DATABASE_URL_SUPERUSER must be set; "
            "skipping live-DB web-research resolution test.",
            allow_module_level=False,
        )


@pytest_asyncio.fixture
async def superuser_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the Postgres superuser — fixture-only, for seeding."""
    _require_db()
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def app_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine bound to the unprivileged role — fixture-only, for seeding."""
    _require_db()
    engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_with_research_model(
    app_engine: AsyncEngine,
    superuser_engine: AsyncEngine,
) -> AsyncGenerator[UUID, None]:
    """A tenant carrying a tenant-scope ``openrouter.research_model`` row."""
    tenant_id = uuid4()
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, subdomain) "
                "VALUES (:id, :name, gen_random_uuid()::text)"
            ),
            {"id": str(tenant_id), "name": "Web-Research Resolution Tenant"},
        )
    async with tenant_context(app_engine, tenant_id) as session:
        await ScopedSettingRepository(session).upsert(
            scope="tenant",
            provider="openrouter",
            key="research_model",
            is_secret=False,
            value_plain="tenant/research-model",
        )
    try:
        yield tenant_id
    finally:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM scoped_settings WHERE tenant_id = :id"),
                {"id": str(tenant_id)},
            )
            await conn.execute(
                text("DELETE FROM audit_log WHERE tenant_id = :id"),
                {"id": str(tenant_id)},
            )
            await conn.execute(
                text("DELETE FROM tenants WHERE id = :id"),
                {"id": str(tenant_id)},
            )


def test_tenant_research_model_outranks_the_environment(
    tenant_with_research_model: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of ADR-0132: the tenant's choice, not ``.env``'s.

    Synchronous by design — the wrapper crosses into its own event loop via
    ``run_async_in_fresh_loop``, exactly as it does in production.
    """
    from services.tools.web_research_tool import web_research

    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("SHIRLEY_MODEL", "env/shirley-model")
    set_tool_context(
        ToolExecutionContext(
            tenant_id=tenant_with_research_model,
            database_url=DATABASE_URL or "",
        )
    )
    fake_service = _patched_service()

    with patch(
        "services.tools.web_research_tool.WebResearchService",
        return_value=fake_service,
    ):
        web_research("ECB")

    fake_service.research.assert_called_once()
    assert fake_service.research.call_args.kwargs["llm"].model == "tenant/research-model"
