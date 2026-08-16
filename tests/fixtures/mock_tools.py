# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Mock tool callables and a fresh-registry helper for tool-loop tests.

The characterization tests for the streaming worker need to drive the
``ToolRegistry`` with predictable tools — including tools that raise
exceptions and tools that artificially slow down to expose lock /
concurrency behaviour. This module collects the helpers in one place so
each test file can stay focused on the scenario it is asserting.

Usage::

    from tests.fixtures.mock_tools import (
        build_fresh_registry,
        mock_tool_failure,
        mock_tool_slow,
        mock_tool_success,
    )
"""

from __future__ import annotations

import time
from typing import Any

from services.tool_classes import ToolClass
from services.tool_registry import ToolRegistry


_DEFAULT_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "required": []}


def mock_tool_success(**_kwargs: Any) -> str:
    """Return a fixed success string regardless of arguments.

    Returns:
        The literal string ``"mock_tool_success: ok"``.
    """
    return "mock_tool_success: ok"


def mock_tool_failure(**_kwargs: Any) -> str:
    """Always raise ``RuntimeError`` to exercise the registry's error path.

    Raises:
        RuntimeError: Unconditionally.
    """
    raise RuntimeError("mock_tool_failure: deliberate failure")


def mock_tool_slow(*, delay_s: float = 0.5, **_kwargs: Any) -> str:
    """Sleep for ``delay_s`` seconds, then return a marker string.

    Used by concurrency tests that observe wall-clock interleaving.

    Args:
        delay_s: Sleep duration in seconds (default 0.5).

    Returns:
        The literal string ``"mock_tool_slow: done"``.
    """
    time.sleep(delay_s)
    return "mock_tool_slow: done"


def build_fresh_registry(
    *,
    include_success: bool = True,
    include_failure: bool = False,
    include_slow: bool = False,
) -> ToolRegistry:
    """Construct a fresh :class:`ToolRegistry` with selected mock tools.

    Each test that patches ``services.tool_registry.get_tool_registry``
    gets its own isolated registry, so per-turn gating state and
    registered tools cannot leak between tests.

    Args:
        include_success: Register ``mock_tool_success``.
        include_failure: Register ``mock_tool_failure``.
        include_slow: Register ``mock_tool_slow``.

    Returns:
        A new :class:`ToolRegistry` instance with the requested mock
        tools registered as :attr:`ToolClass.READ_INTERNAL`.
    """
    reg = ToolRegistry()
    if include_success:
        reg.register_tool(
            name="mock_tool_success",
            function=mock_tool_success,
            description="Always-succeeds mock tool for tests.",
            parameters=_DEFAULT_PARAMS,
            tool_class=ToolClass.READ_INTERNAL,
        )
    if include_failure:
        reg.register_tool(
            name="mock_tool_failure",
            function=mock_tool_failure,
            description="Always-raises mock tool for tests.",
            parameters=_DEFAULT_PARAMS,
            tool_class=ToolClass.READ_INTERNAL,
        )
    if include_slow:
        reg.register_tool(
            name="mock_tool_slow",
            function=mock_tool_slow,
            description="Sleeps then returns a marker string.",
            parameters={
                "type": "object",
                "properties": {"delay_s": {"type": "number", "default": 0.5}},
                "required": [],
            },
            tool_class=ToolClass.READ_INTERNAL,
        )
    return reg


__all__ = [
    "build_fresh_registry",
    "mock_tool_failure",
    "mock_tool_slow",
    "mock_tool_success",
]
