# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :mod:`services.tool_registry`.

Uses fresh :class:`~services.tool_registry.ToolRegistry` instances for all
tests except the singleton test, to prevent cross-test contamination.
"""

from __future__ import annotations

import json
import re

import pytest

from services.tool_classes import ToolClass
from services.tool_registry import (
    ToolRegistry,
    ToolRegistryError,
    get_tool_registry,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegisterTool:
    """Test :meth:`~services.tool_registry.ToolRegistry.register_tool`."""

    def test_register_tool(self) -> None:
        """Registering a tool makes it appear in list_tools()."""
        reg = ToolRegistry()
        reg.register_tool(
            name="my_tool",
            function=lambda: "result",
            description="A test tool.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )
        assert "my_tool" in reg.list_tools()

    def test_register_duplicate_raises(self) -> None:
        """Registering the same name twice raises ValueError."""
        reg = ToolRegistry()
        reg.register_tool(
            name="dup_tool",
            function=lambda: "ok",
            description="First registration.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )
        with pytest.raises(ValueError, match="already registered"):
            reg.register_tool(
                name="dup_tool",
                function=lambda: "ok",
                description="Second registration.",
                parameters={"type": "object", "properties": {}, "required": []},
                tool_class=ToolClass.READ_INTERNAL,
            )

    def test_register_without_class_raises(self) -> None:
        """Omitting tool_class raises ToolRegistryError (ADR-0022 fail-fast)."""
        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="ADR-0022"):
            reg.register_tool(
                name="no_class",
                function=lambda: "ok",
                description="Missing class.",
                parameters={"type": "object", "properties": {}, "required": []},
            )

    def test_register_with_non_toolclass_value_raises(self) -> None:
        """A non-ToolClass value for tool_class raises ToolRegistryError (ADR-0022)."""
        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="ADR-0022"):
            reg.register_tool(
                name="bad_class",
                function=lambda: "ok",
                description="Bad class.",
                parameters={"type": "object", "properties": {}, "required": []},
                tool_class="read_internal",  # type: ignore[arg-type]
            )

    def test_register_with_none_class_raises(self) -> None:
        """Passing None as tool_class raises ToolRegistryError."""
        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="ADR-0022"):
            reg.register_tool(
                name="none_class",
                function=lambda: "ok",
                description="None class.",
                parameters={"type": "object", "properties": {}, "required": []},
                tool_class=None,  # type: ignore[arg-type]
            )

    def test_unregister_tool(self) -> None:
        """Unregistering a tool makes has_tool() return False."""
        reg = ToolRegistry()
        reg.register_tool(
            name="removable",
            function=lambda: "ok",
            description="Will be removed.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )
        assert reg.has_tool("removable")
        removed = reg.unregister_tool("removable")
        assert removed is True
        assert not reg.has_tool("removable")

    def test_unregister_nonexistent_returns_false(self) -> None:
        """Unregistering a name that doesn't exist returns False without error."""
        reg = ToolRegistry()
        result = reg.unregister_tool("does_not_exist")
        assert result is False


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


class TestGetToolDefinitions:
    """Test :meth:`~services.tool_registry.ToolRegistry.get_tool_definitions`."""

    def test_empty_definitions(self) -> None:
        """A fresh registry returns an empty list of definitions."""
        reg = ToolRegistry()
        assert reg.get_tool_definitions() == []

    def test_get_tool_definitions_format(self) -> None:
        """Registered tool appears in correct OpenAI function-calling format."""
        reg = ToolRegistry()
        reg.register_tool(
            name="sample_tool",
            function=lambda: "hello",
            description="A sample tool.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )
        defs = reg.get_tool_definitions()
        assert len(defs) == 1
        defn = defs[0]
        assert defn["type"] == "function"
        assert defn["function"]["name"] == "sample_tool"
        assert defn["function"]["description"] == "A sample tool."
        assert defn["function"]["parameters"] == {
            "type": "object",
            "properties": {},
            "required": [],
        }


# ---------------------------------------------------------------------------
# get_tool_class
# ---------------------------------------------------------------------------


class TestGetToolClass:
    """Test :meth:`~services.tool_registry.ToolRegistry.get_tool_class`."""

    def test_get_tool_class_returns_declared(self) -> None:
        """get_tool_class returns the class declared at registration."""
        reg = ToolRegistry()
        reg.register_tool(
            name="inspect_me",
            function=lambda: "ok",
            description="Inspectable.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
        )
        assert reg.get_tool_class("inspect_me") is ToolClass.READ_EXTERNAL_UNTRUSTED

    def test_get_tool_class_unknown_raises(self) -> None:
        """Looking up a non-existent tool raises KeyError."""
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.get_tool_class("does_not_exist")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestExecuteTool:
    """Test :meth:`~services.tool_registry.ToolRegistry.execute_tool`."""

    def test_execute_tool_success(self) -> None:
        """A registered tool returns its string result on successful execution."""
        reg = ToolRegistry()
        reg.register_tool(
            name="greeter",
            function=lambda: "hello",
            description="Says hello.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )
        result = reg.execute_tool("greeter", {})
        assert result == "hello"

    def test_execute_tool_with_args(self) -> None:
        """Arguments are passed correctly to the tool function."""
        reg = ToolRegistry()

        def echo(message: str) -> str:
            return f"echo: {message}"

        reg.register_tool(
            name="echo",
            function=echo,
            description="Echoes a message.",
            parameters={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            tool_class=ToolClass.READ_INTERNAL,
        )
        result = reg.execute_tool("echo", {"message": "hello world"})
        assert result == "echo: hello world"

    def test_execute_unknown_tool(self) -> None:
        """Calling an unknown tool returns an error string, never raises."""
        reg = ToolRegistry()
        result = reg.execute_tool("nonexistent", {})
        assert "nonexistent" in result
        assert isinstance(result, str)

    def test_execute_tool_exception_returns_error(self) -> None:
        """A tool that raises returns an error string containing exception info."""
        reg = ToolRegistry()

        def broken() -> str:
            raise ValueError("something went wrong")

        reg.register_tool(
            name="broken_tool",
            function=broken,
            description="Always fails.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_INTERNAL,
        )
        result = reg.execute_tool("broken_tool", {})
        assert "broken_tool" in result
        assert "ValueError" in result
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Per-turn gating (ADR-0022)
# ---------------------------------------------------------------------------


def _make_gating_registry() -> ToolRegistry:
    """Build a registry with one tool per class for gating tests."""
    reg = ToolRegistry()
    reg.register_tool(
        name="read_internal",
        function=lambda: "internal-ok",
        description="Read internal.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.READ_INTERNAL,
    )
    reg.register_tool(
        name="fetch_untrusted",
        function=lambda: "fetched-content",
        description="Fetch untrusted.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
    )
    reg.register_tool(
        name="write_internal",
        function=lambda: "write-ok",
        description="Write internal.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.WRITE_INTERNAL,
    )
    reg.register_tool(
        name="external_effect",
        function=lambda: "effect-ok",
        description="External effect.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.EXTERNAL_EFFECT,
    )
    return reg


class TestPerTurnGating:
    """Gating rules from ADR-0022."""

    def test_no_gating_before_untrusted_fetch(self) -> None:
        """Write / effect tools execute normally when no fetch has happened."""
        reg = _make_gating_registry()
        reg.begin_turn()
        assert reg.execute_tool("write_internal", {}) == "write-ok"
        assert reg.execute_tool("external_effect", {}) == "effect-ok"
        reg.end_turn()

    def test_read_internal_not_locked_after_fetch(self) -> None:
        """READ_INTERNAL tools remain callable after an untrusted fetch."""
        reg = _make_gating_registry()
        reg.begin_turn()
        assert reg.execute_tool("fetch_untrusted", {}) == "fetched-content"
        result = reg.execute_tool("read_internal", {})
        assert result == "internal-ok"

    def test_write_locked_after_untrusted_fetch(self) -> None:
        """WRITE_INTERNAL is refused after a successful untrusted fetch."""
        reg = _make_gating_registry()
        reg.begin_turn()
        reg.execute_tool("fetch_untrusted", {})
        refusal = reg.execute_tool("write_internal", {})
        assert "locked" in refusal.lower()
        assert "ADR-0022" in refusal

    def test_external_effect_locked_after_untrusted_fetch(self) -> None:
        """EXTERNAL_EFFECT is refused after a successful untrusted fetch."""
        reg = _make_gating_registry()
        reg.begin_turn()
        reg.execute_tool("fetch_untrusted", {})
        refusal = reg.execute_tool("external_effect", {})
        assert "locked" in refusal.lower()
        assert "ADR-0022" in refusal

    def test_untrusted_fetch_itself_not_locked(self) -> None:
        """Subsequent untrusted fetches remain callable within the same turn."""
        reg = _make_gating_registry()
        reg.begin_turn()
        reg.execute_tool("fetch_untrusted", {})
        assert reg.execute_tool("fetch_untrusted", {}) == "fetched-content"

    def test_begin_turn_resets_lock(self) -> None:
        """begin_turn clears gating state, unlocking write/effect tools."""
        reg = _make_gating_registry()
        reg.begin_turn()
        reg.execute_tool("fetch_untrusted", {})
        assert "locked" in reg.execute_tool("write_internal", {}).lower()

        reg.begin_turn()
        assert reg.execute_tool("write_internal", {}) == "write-ok"

    def test_end_turn_releases_lock(self) -> None:
        """end_turn clears gating state so the next begin_turn starts fresh."""
        reg = _make_gating_registry()
        reg.begin_turn()
        reg.execute_tool("fetch_untrusted", {})
        reg.end_turn()

        reg.begin_turn()
        assert reg.execute_tool("external_effect", {}) == "effect-ok"

    def test_failed_untrusted_fetch_does_not_lock(self) -> None:
        """A fetch that raises leaves gating state untouched."""
        reg = ToolRegistry()

        def boom() -> str:
            raise RuntimeError("fetch failed")

        reg.register_tool(
            name="broken_fetch",
            function=boom,
            description="Broken fetch.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
        )
        reg.register_tool(
            name="write_internal",
            function=lambda: "write-ok",
            description="Write internal.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.WRITE_INTERNAL,
        )
        reg.begin_turn()
        result = reg.execute_tool("broken_fetch", {})
        assert "failed" in result.lower() or "runtimeerror" in result.lower()
        assert reg.execute_tool("write_internal", {}) == "write-ok"


# ---------------------------------------------------------------------------
# wraps_result_as_untrusted (ADR-0022 trust delimiters)
# ---------------------------------------------------------------------------


class TestWrapsResultAsUntrusted:
    """Verify the trust-delimiter wrapping behaviour (ADR-0022)."""

    def test_flag_requires_untrusted_class(self) -> None:
        """wraps_result_as_untrusted=True is rejected for non-untrusted classes."""
        reg = ToolRegistry()
        with pytest.raises(ToolRegistryError, match="READ_EXTERNAL_UNTRUSTED"):
            reg.register_tool(
                name="bad_wrap",
                function=lambda: "x",
                description="Bad wrap.",
                parameters={"type": "object", "properties": {}, "required": []},
                tool_class=ToolClass.READ_INTERNAL,
                wraps_result_as_untrusted=True,
            )

    def test_flag_default_is_false(self) -> None:
        """An untrusted tool registered without the flag is not wrapped."""
        reg = ToolRegistry()
        reg.register_tool(
            name="fetch_plain",
            function=lambda: "raw content",
            description="Plain fetch.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
        )
        reg.begin_turn()
        result = reg.execute_tool("fetch_plain", {})
        assert result == "raw content"
        assert "<external_content" not in result

    def test_wraps_json_envelope_with_metadata(self) -> None:
        """A tool returning {source, fetched_at, body} has those values in the wrapper."""
        reg = ToolRegistry()

        def fetch() -> str:
            return json.dumps(
                {
                    "source": "https://example.com/article",
                    "fetched_at": "2026-04-24T10:24:00+00:00",
                    "body": "Reuters reports the ECB held rates steady.",
                }
            )

        reg.register_tool(
            name="fetch_wrapped",
            function=fetch,
            description="Wrapped fetch.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
            wraps_result_as_untrusted=True,
        )
        reg.begin_turn()
        result = reg.execute_tool("fetch_wrapped", {})
        assert result.startswith(
            '<external_content source="https://example.com/article" '
            'fetched_at="2026-04-24T10:24:00+00:00" trust="untrusted">'
        )
        assert result.endswith("</external_content>")
        assert "Reuters reports the ECB held rates steady." in result

    def test_wraps_plain_string_with_fallback_source(self, caplog) -> None:
        """A plain-string return falls back to source='tool:<name>' and logs a warning."""
        reg = ToolRegistry()
        reg.register_tool(
            name="fetch_raw",
            function=lambda: "some raw text",
            description="Plain raw.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
            wraps_result_as_untrusted=True,
        )
        reg.begin_turn()
        with caplog.at_level("WARNING", logger="services.tool_registry"):
            result = reg.execute_tool("fetch_raw", {})
        assert 'source="tool:fetch_raw"' in result
        assert re.search(r'fetched_at="[0-9T:+\-.]+"', result) is not None
        assert "some raw text" in result
        assert any("wraps_result_as_untrusted" in rec.message for rec in caplog.records)

    def test_wraps_malformed_json_with_fallback(self, caplog) -> None:
        """JSON that is valid but lacks a 'body' key uses the fallback path."""
        reg = ToolRegistry()

        def fetch() -> str:
            return json.dumps({"source": "x", "fetched_at": "t"})

        reg.register_tool(
            name="fetch_malformed",
            function=fetch,
            description="Malformed envelope.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
            wraps_result_as_untrusted=True,
        )
        reg.begin_turn()
        with caplog.at_level("WARNING", logger="services.tool_registry"):
            result = reg.execute_tool("fetch_malformed", {})
        assert 'source="tool:fetch_malformed"' in result
        assert any("wraps_result_as_untrusted" in rec.message for rec in caplog.records)

    def test_wrapped_tool_still_triggers_gating(self) -> None:
        """A wrapped untrusted fetch still locks WRITE / EXTERNAL_EFFECT tools."""
        reg = ToolRegistry()
        reg.register_tool(
            name="fetch_wrapped",
            function=lambda: json.dumps(
                {
                    "source": "https://x.test/a",
                    "fetched_at": "2026-04-24T00:00:00+00:00",
                    "body": "hello",
                }
            ),
            description="Wrapped fetch.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.READ_EXTERNAL_UNTRUSTED,
            wraps_result_as_untrusted=True,
        )
        reg.register_tool(
            name="write_internal",
            function=lambda: "ok",
            description="Write.",
            parameters={"type": "object", "properties": {}, "required": []},
            tool_class=ToolClass.WRITE_INTERNAL,
        )
        reg.begin_turn()
        reg.execute_tool("fetch_wrapped", {})
        refusal = reg.execute_tool("write_internal", {})
        assert "locked" in refusal.lower()


# ---------------------------------------------------------------------------
# Analysis-tools phase 2 registration (ADR-0070)
# ---------------------------------------------------------------------------


class TestAnalysisToolsPhase2Registration:
    """The ADR-0070 reads register on the singleton as READ_INTERNAL."""

    def test_phase_2_reads_register_as_read_internal(self) -> None:
        """Both tools appear in get_tool_definitions as READ_INTERNAL."""
        # Importing the module triggers its register_tool calls on the
        # singleton (idempotent via the import cache).
        import services.tools.analysis_tools  # noqa: F401

        reg = get_tool_registry()
        names = {d["function"]["name"] for d in reg.get_tool_definitions()}
        for name in ("get_portfolio_overview", "get_saa_configuration"):
            assert name in names, f"{name} not registered"
            assert reg.get_tool_class(name) is ToolClass.READ_INTERNAL

    def test_phase_2_reads_expose_only_their_optional_params(self) -> None:
        """The two reads carry exactly their single optional parameter."""
        import services.tools.analysis_tools  # noqa: F401

        reg = get_tool_registry()
        defs = {d["function"]["name"]: d["function"] for d in reg.get_tool_definitions()}
        assert set(defs["get_portfolio_overview"]["parameters"]["properties"]) == {"as_of_date"}
        assert set(defs["get_saa_configuration"]["parameters"]["properties"]) == {
            "configuration_name"
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    """Test :func:`~services.tool_registry.get_tool_registry` singleton."""

    def test_singleton(self) -> None:
        """Calling get_tool_registry() twice returns the same instance."""
        reg1 = get_tool_registry()
        reg2 = get_tool_registry()
        assert reg1 is reg2
