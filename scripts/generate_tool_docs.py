# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Build the human-facing AI-tool reference from the ToolRegistry.

PortfoliFLOW registers every AI-callable tool — its name, description,
JSON-Schema parameters, and :class:`~services.tool_classes.ToolClass` —
with the :class:`~services.tool_registry.ToolRegistry` (ADR-0012). That
registry is the single source of truth. It already feeds two renderings:
the API ``tools`` field, and — since B8 — the generated tool-inventory
block injected into Shirley's system prompt
(:meth:`services.ai_service_core.AIServiceCore._render_tool_inventory`).

This script projects the same source onto a *third* rendering: a
human-readable Markdown table at ``docs/tools.md``. Like the theme
artefact (see ``scripts/generate_theme_artifacts.py``), the generated
file is committed alongside its source so drift is caught at code review
rather than at runtime. A pre-commit hook regenerates it whenever the
tool-registration code changes.

Output behaviour:

* Emits ``docs/tools.md`` — one table row per registered tool (name,
  trust class, description, parameters), in registry insertion order.
* Idempotent: identical registrations produce byte-identical output. No
  timestamps, no environment-dependent paths.
* Invocable as a module (``python -m scripts.generate_tool_docs``) and
  as a callable from a pre-commit hook
  (``scripts.generate_tool_docs:main``).

Usage::

    python -m scripts.generate_tool_docs
    python -m scripts.generate_tool_docs --check  # CI / pre-commit
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_DEFAULT_OUTPUT: Path = _REPO_ROOT / "docs" / "tools.md"

_HEADER = (
    "<!-- GENERATED FILE — DO NOT EDIT BY HAND.\n"
    "     Regenerate with `python -m scripts.generate_tool_docs` (or commit a\n"
    "     change to the tool-registration code with the pre-commit hook\n"
    "     installed). Source of truth: the ToolRegistry, ADR-0012. -->\n"
    "\n"
    "# PortfoliFLOW AI Tool Reference\n"
    "\n"
    "Every tool Shirley can call, rendered from the `ToolRegistry`\n"
    "(`services/tool_registry.py`) — the single source of truth (ADR-0012).\n"
    "The same source feeds the API `tools` field and the generated\n"
    "tool-inventory block in Shirley's system prompt, so this table can never\n"
    "drift from the tools actually exposed to the model.\n"
    "\n"
    "Trust classes gate per-turn behaviour (ADR-0022).\n"
    "\n"
)


def _ensure_tools_registered() -> None:
    """Trigger registration of the default tool set.

    Constructing the :class:`~services.ai_service_core.AIServiceCore`
    singleton runs :meth:`AIServiceCore._register_default_tools`, which
    imports the canonical tool modules (their ``register_tool`` calls run
    at import time). Reusing that path keeps the authoritative list of
    default tools in one place — this script never duplicates it. No
    network I/O occurs; endpoint credentials are configured separately.
    """
    from services.ai_service_core import get_ai_service_core

    get_ai_service_core()


def _format_description(description: str | None) -> str:
    """Collapse a tool description to one table-cell-safe line.

    Args:
        description: The registered description (may span multiple lines).

    Returns:
        The description with internal whitespace collapsed and pipe
        characters escaped so they do not break the Markdown table.
    """
    flat = " ".join((description or "").split())
    return flat.replace("|", "\\|")


def _format_parameters(parameters: dict[str, Any] | None) -> str:
    """Render a tool's parameter schema as a compact cell.

    Args:
        parameters: The OpenAI-format JSON-Schema object for the tool.

    Returns:
        A comma-separated list of parameter names — each as inline code,
        required ones suffixed ``(req)`` — or an em dash when the tool
        takes no parameters.
    """
    props = (parameters or {}).get("properties", {})
    if not props:
        return "—"
    required = set((parameters or {}).get("required", []))
    parts = [f"`{name}` (req)" if name in required else f"`{name}`" for name in props]
    return ", ".join(parts)


def render_markdown() -> str:
    """Render the full ``tools.md`` content from the live registry.

    Returns:
        The complete Markdown document, deterministic given the set of
        registered tools and their registration order.
    """
    _ensure_tools_registered()

    from services.tool_registry import get_tool_registry

    registry = get_tool_registry()
    tool_defs = registry.get_tool_definitions()

    lines = [
        _HEADER.rstrip("\n"),
        "",
        "| Tool | Trust class | Description | Parameters |",
        "|---|---|---|---|",
    ]
    for tool_def in tool_defs:
        fn = tool_def.get("function", {})
        name = fn.get("name")
        if not name:
            continue
        try:
            trust = registry.get_tool_class(name).value
        except KeyError:
            trust = "unknown"
        description = _format_description(fn.get("description"))
        params = _format_parameters(fn.get("parameters"))
        lines.append(f"| `{name}` | `{trust}` | {description} | {params} |")

    return "\n".join(lines) + "\n"


def _write_if_changed(target: Path, content: str) -> bool:
    """Write ``content`` to ``target`` only if the bytes differ.

    Args:
        target: Output file path.
        content: Generated Markdown content.

    Returns:
        ``True`` when the file was written, ``False`` when the existing
        contents already matched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    new_bytes = content.encode("utf-8")
    if target.exists() and target.read_bytes() == new_bytes:
        return False
    target.write_bytes(new_bytes)
    return True


def main(argv: list[str] | None = None) -> int:
    """Entry point for module invocation and pre-commit hooks.

    Args:
        argv: Optional CLI argument list (used by tests). When ``None``,
            ``sys.argv[1:]`` is used.

    Returns:
        ``0`` on success or no-change-in-check-mode; ``1`` when
        ``--check`` is specified and the on-disk artefact is stale.
    """
    parser = argparse.ArgumentParser(
        prog="generate_tool_docs",
        description=(
            "Generate docs/tools.md from the ToolRegistry — the single "
            "source of truth for AI-callable tools (ADR-0012)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="Path of the generated Markdown file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write; exit non-zero if the on-disk artefact is "
            "stale. Suitable for CI / pre-commit verification."
        ),
    )
    args = parser.parse_args(argv)

    content = render_markdown()

    if args.check:
        if not args.output.exists():
            sys.stderr.write(
                f"tools.md missing at {args.output}; run `python -m scripts.generate_tool_docs`.\n"
            )
            return 1
        if args.output.read_bytes() != content.encode("utf-8"):
            sys.stderr.write(
                f"tools.md at {args.output} is stale; run `python -m scripts.generate_tool_docs`.\n"
            )
            return 1
        return 0

    changed = _write_if_changed(args.output, content)
    if changed:
        sys.stdout.write(f"wrote {args.output}\n")
    else:
        sys.stdout.write(f"unchanged {args.output}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry shim
    raise SystemExit(main())
