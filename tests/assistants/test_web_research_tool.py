# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Integration tests for :mod:`services.tools.web_research_tool`.

Covers tool registration (class + wrapping flag) against the application
singleton and gating interaction with other tool classes per ADR-0022.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.tool_classes import ToolClass
from services.tool_registry import ToolRegistry, get_tool_registry


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
