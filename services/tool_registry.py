# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Central registry for AI-callable tools.

Tools are plain Python functions registered with their OpenAI function-calling
schema. The AIService queries this registry to build the ``tools`` parameter
for API calls, and dispatches tool calls through it for execution.

Adding a new tool requires only:

1. Write the tool function in ``services/tools/``.
2. Call ``registry.register_tool(...)`` — typically at module import time.
   The call must declare a :class:`~services.tool_classes.ToolClass`; silent
   defaults are not permitted (ADR-0022).

No changes to AIService, ToolRegistry, or GUI code are needed.

Usage::

    from services.tool_classes import ToolClass
    from services.tool_registry import get_tool_registry

    reg = get_tool_registry()
    reg.register_tool(
        name="list_datasets",
        function=list_datasets,
        description="List all datasets currently loaded in the DataStore.",
        parameters={"type": "object", "properties": {}, "required": []},
        tool_class=ToolClass.READ_INTERNAL,
    )

    tool_defs = reg.get_tool_definitions()
    result = reg.execute_tool("list_datasets", {})

Per-turn gating (ADR-0022)
--------------------------

A "user turn" is scoped to a single ``AIService.send_message()`` invocation
together with its complete tool-execution loop. The registry exposes
:meth:`ToolRegistry.begin_turn` and :meth:`ToolRegistry.end_turn` which the
streaming worker calls at the boundaries of a turn.

Once any tool of class :attr:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED`
executes within a turn, subsequent attempts to call a tool of class
:attr:`~services.tool_classes.ToolClass.WRITE_INTERNAL` or
:attr:`~services.tool_classes.ToolClass.EXTERNAL_EFFECT` are refused by
:meth:`ToolRegistry.execute_tool`. The refusal is returned as an error string,
not raised, so the tool-calling loop can surface it to the model; the tool
function itself is never invoked.

Trust delimiters (ADR-0022)
---------------------------

Tools registered with ``wraps_result_as_untrusted=True`` have their return
value wrapped in an ``<external_content source="..." fetched_at="..."
trust="untrusted">...</external_content>`` block by :meth:`execute_tool`
before the string reaches the AIService tool-execution loop. The flag is
only valid for tools of class
:attr:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED`; declaring
it on any other class is a programming error and raises :class:`ToolRegistryError`.

A tool that wants to supply the real resolved URL and fetch timestamp returns
a JSON string with keys ``source``, ``fetched_at``, and ``body``; the registry
unpacks those and uses them as the wrapper's attributes. A tool that returns a
plain string (or malformed JSON) still gets wrapped, with ``source`` defaulting
to ``"tool:<tool_name>"`` and ``fetched_at`` defaulting to the current UTC
time; the fallback is logged at WARNING so mis-shaped tool envelopes do not
pass silently.

See ADR-0022 (``docs/adr/0022-tool-trust-classes-and-gating-policy.md``) for
the decision record and rationale.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from collections.abc import Callable

from core.exceptions import PortfoliFlowError
from services.tool_classes import ToolClass

logger = logging.getLogger(__name__)

_instance: ToolRegistry | None = None


# Classes that are locked for the remainder of a turn once a
# READ_EXTERNAL_UNTRUSTED tool has executed (ADR-0022).
_LOCKED_AFTER_UNTRUSTED_FETCH: frozenset[ToolClass] = frozenset(
    {ToolClass.WRITE_INTERNAL, ToolClass.EXTERNAL_EFFECT}
)


def _wrap_untrusted(tool_name: str, raw_result: str) -> str:
    """Wrap a tool's raw result in ``<external_content>`` delimiters (ADR-0022).

    If ``raw_result`` is a JSON object carrying ``source``, ``fetched_at``,
    and ``body`` keys, those values are used for the wrapper attributes and
    body. Otherwise the raw string is treated as the body, ``source`` defaults
    to ``tool:<tool_name>``, ``fetched_at`` defaults to ``datetime.now(UTC)``,
    and a WARNING is logged so malformed envelopes don't pass unnoticed.

    Args:
        tool_name: The registered tool name (used only for the fallback
            ``source`` attribute and the warning log).
        raw_result: The string returned by the tool function.

    Returns:
        A single ``<external_content>`` block with ``trust="untrusted"``.
    """
    source = f"tool:{tool_name}"
    fetched_at = datetime.now(timezone.utc).isoformat()
    body = raw_result
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict) and "body" in parsed:
        body_val = parsed.get("body", "")
        body = body_val if isinstance(body_val, str) else json.dumps(body_val)
        src = parsed.get("source")
        if isinstance(src, str) and src:
            source = src
        ts = parsed.get("fetched_at")
        if isinstance(ts, str) and ts:
            fetched_at = ts
    else:
        logger.warning(
            "ToolRegistry: tool '%s' declared wraps_result_as_untrusted=True "
            "but did not return a JSON envelope with 'body' key; falling back "
            "to raw-string wrapping with source='%s'.",
            tool_name,
            source,
        )

    return (
        f'<external_content source="{source}" fetched_at="{fetched_at}" '
        f'trust="untrusted">\n{body}\n</external_content>'
    )


class ToolRegistryError(PortfoliFlowError):
    """Raised on ToolRegistry misconfiguration.

    Currently: registration of a tool without a declared
    :class:`~services.tool_classes.ToolClass` (ADR-0022 fail-fast requirement).
    """


class ToolRegistry:
    """Registry mapping tool names to callables and their API schemas.

    Attributes:
        _tools: Internal dict mapping tool name to a dict with keys
            ``'function'``, ``'description'``, ``'parameters'``,
            ``'tool_class'``.
        _locked_classes: Set of :class:`~services.tool_classes.ToolClass`
            members whose tools are currently locked for the remainder of the
            turn. Populated by :meth:`execute_tool` when a
            :attr:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED`
            tool runs; cleared by :meth:`begin_turn`.
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._locked_classes: set[ToolClass] = set()

    def register_tool(
        self,
        name: str,
        function: Callable[..., str],
        description: str,
        parameters: dict[str, Any],
        tool_class: ToolClass | None = None,
        wraps_result_as_untrusted: bool = False,
    ) -> None:
        """Register a tool that the AI can call.

        Args:
            name: Unique tool name (snake_case, e.g. ``"list_datasets"``).
            function: The callable to execute. Must accept keyword arguments
                matching the parameter schema and return a string result.
            description: Human-readable description shown to the model.
                The model uses this to decide when to call the tool.
            parameters: JSON Schema object describing the function's parameters
                in OpenAI function-calling format.
            tool_class: The :class:`~services.tool_classes.ToolClass` this tool
                belongs to (ADR-0022). A valid ``ToolClass`` member is
                required; passing ``None`` or any non-``ToolClass`` value —
                including omitting the argument — raises
                :class:`ToolRegistryError` immediately. The default is ``None``
                purely so that the check fires inside this method and produces
                a typed error instead of a bare ``TypeError`` from Python's
                argument binding.
            wraps_result_as_untrusted: If ``True``, :meth:`execute_tool` wraps
                the tool's return value in ``<external_content>`` delimiters
                before returning it, per the trust-level rules of ADR-0022.
                Only valid for tools of class
                :attr:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED`;
                declaring it on any other class is a programming error and
                raises :class:`ToolRegistryError`.

        Raises:
            ValueError: If a tool with this name is already registered.
            ToolRegistryError: If ``tool_class`` is not a valid
                :class:`ToolClass` member (ADR-0022 fail-fast), or if
                ``wraps_result_as_untrusted`` is set on a tool that is not
                :attr:`~services.tool_classes.ToolClass.READ_EXTERNAL_UNTRUSTED`.
        """
        if not isinstance(tool_class, ToolClass):
            raise ToolRegistryError(
                f"Tool '{name}' must declare a ToolClass (ADR-0022); "
                f"got {tool_class!r} ({type(tool_class).__name__})."
            )
        if wraps_result_as_untrusted and tool_class is not ToolClass.READ_EXTERNAL_UNTRUSTED:
            raise ToolRegistryError(
                f"Tool '{name}' sets wraps_result_as_untrusted=True but its "
                f"class is {tool_class.value}. The wrapping flag is only "
                "valid for READ_EXTERNAL_UNTRUSTED tools (ADR-0022)."
            )
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        self._tools[name] = {
            "function": function,
            "description": description,
            "parameters": parameters,
            "tool_class": tool_class,
            "wraps_result_as_untrusted": wraps_result_as_untrusted,
        }
        logger.info(
            "Registered tool '%s' (class=%s, wraps_untrusted=%s).",
            name,
            tool_class.value,
            wraps_result_as_untrusted,
        )

    def unregister_tool(self, name: str) -> bool:
        """Remove a tool from the registry.

        Args:
            name: The tool name to remove.

        Returns:
            True if the tool was found and removed, False otherwise.
        """
        if name not in self._tools:
            return False
        del self._tools[name]
        logger.info("Unregistered tool '%s'.", name)
        return True

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return all registered tools in OpenAI function-calling format.

        Returns:
            List of tool definition dicts. Empty list if no tools registered.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for name, tool in self._tools.items()
        ]

    def get_tool_class(self, name: str) -> ToolClass:
        """Return the declared :class:`ToolClass` of a registered tool.

        Args:
            name: The tool name to look up.

        Returns:
            The tool's :class:`~services.tool_classes.ToolClass`.

        Raises:
            KeyError: If no tool with this name is registered.
        """
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' is not registered.")
        return self._tools[name]["tool_class"]

    def begin_turn(self) -> None:
        """Reset the per-turn gating state at the start of a user turn.

        Called by the AIService streaming worker at the top of
        :meth:`services.ai_service._StreamWorker.run` (ADR-0022).
        """
        self._locked_classes = set()
        logger.debug("ToolRegistry.begin_turn: per-turn lock state cleared.")

    def end_turn(self) -> None:
        """Release the per-turn gating state at the end of a user turn.

        Called by the AIService streaming worker when its tool-execution loop
        exits, whether normally or via an error path (ADR-0022).
        """
        self._locked_classes = set()
        logger.debug("ToolRegistry.end_turn: per-turn lock state cleared.")

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a registered tool with the given arguments.

        Tool failures are caught and returned as error strings — they never
        crash the tool-execution loop. If a tool's class is currently locked
        by per-turn gating (ADR-0022), the call is refused and a gated-refusal
        string is returned; the tool function is not invoked.

        Args:
            name: The tool name (must be registered).
            arguments: Dict of keyword arguments matching the tool's schema.

        Returns:
            The tool's string result, a gated-refusal string if the tool's
            class is locked for this turn, or an error description if the tool
            is unknown or raises an exception.
        """
        if name not in self._tools:
            error_msg = f"Unknown tool '{name}'. Available: {sorted(self._tools.keys())}"
            logger.error(error_msg)
            return error_msg

        tool = self._tools[name]
        tool_class: ToolClass = tool["tool_class"]

        if tool_class in self._locked_classes:
            refusal = (
                f"Tool '{name}' (class={tool_class.value}) is locked for the "
                "remainder of this turn: a READ_EXTERNAL_UNTRUSTED tool has "
                "already executed, and WRITE_INTERNAL / EXTERNAL_EFFECT tools "
                "are gated for the rest of the turn (ADR-0022). The user's "
                "next turn starts with a fresh, unlocked state."
            )
            logger.warning(
                "ToolRegistry.execute_tool: refused '%s' (class=%s) — locked for turn.",
                name,
                tool_class.value,
            )
            return refusal

        try:
            result = tool["function"](**arguments)
            logger.debug("Tool '%s' executed successfully.", name)
        except Exception as exc:  # noqa: BLE001 — deliberate broad catch
            error_msg = f"Tool '{name}' failed: {type(exc).__name__}: {exc}"
            logger.error(error_msg, exc_info=True)
            return error_msg

        # Successful execution of an untrusted fetch locks the dangerous
        # classes for the remainder of this turn (ADR-0022).
        if tool_class is ToolClass.READ_EXTERNAL_UNTRUSTED:
            newly_locked = _LOCKED_AFTER_UNTRUSTED_FETCH - self._locked_classes
            if newly_locked:
                self._locked_classes |= _LOCKED_AFTER_UNTRUSTED_FETCH
                logger.info(
                    "ToolRegistry.execute_tool: '%s' executed; locking %s "
                    "for remainder of turn (ADR-0022).",
                    name,
                    sorted(c.value for c in newly_locked),
                )

        result_str = str(result)
        if tool.get("wraps_result_as_untrusted"):
            return _wrap_untrusted(name, result_str)
        return result_str

    def list_tools(self) -> list[str]:
        """Return a sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def has_tool(self, name: str) -> bool:
        """Check whether a tool is registered.

        Args:
            name: The tool name to look up.

        Returns:
            True if the tool exists in the registry.
        """
        return name in self._tools


def get_tool_registry() -> ToolRegistry:
    """Return the application-wide ToolRegistry singleton.

    Returns:
        The global ToolRegistry instance.
    """
    global _instance
    if _instance is None:
        _instance = ToolRegistry()
    return _instance
