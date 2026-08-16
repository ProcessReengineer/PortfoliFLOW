# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Schema-equality test across all shipped chart theme files.

The chart theme loader does not fall back across themes — every key
referenced by chart-rendering code must exist in every shipped variant,
or selecting that variant in Phase B would crash chart rendering. This
test guards the invariant by comparing the leaf-key sets of every
``config/chart_theme*.json`` file against the default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_CHART_THEME_FILES = [
    _CONFIG_DIR / "chart_theme.json",
    _CONFIG_DIR / "chart_theme_light.json",
    _CONFIG_DIR / "chart_theme_print.json",
]


def _collect_leaf_paths(node: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return the set of leaf-key paths in a nested dict.

    Top-level metadata keys starting with ``"_"`` are excluded.
    Lists are treated as leaves (their internal structure is identical
    by construction across variants).

    Args:
        node: A nested dict, or any non-dict leaf value.
        prefix: The current key path being walked (used for recursion).

    Returns:
        Set of tuples, each representing one leaf path.
    """
    if not isinstance(node, dict):
        return {prefix}
    paths: set[tuple[str, ...]] = set()
    for key, value in node.items():
        if not prefix and key.startswith("_"):
            continue
        paths |= _collect_leaf_paths(value, (*prefix, key))
    return paths


@pytest.mark.xfail(
    reason="chart_theme_light.json lacks the ADR-0058 pf.* web-chrome keys; "
    "light-theme values to be authored with the Phase-B theme picker.",
    strict=True,
)
def test_all_chart_themes_have_same_schema() -> None:
    """Every shipped chart theme must expose the exact same leaf-key set."""
    schemas: dict[str, set[tuple[str, ...]]] = {}
    for path in _CHART_THEME_FILES:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        schemas[path.name] = _collect_leaf_paths(data)

    reference_name = _CHART_THEME_FILES[0].name
    reference = schemas[reference_name]
    for name, schema in schemas.items():
        missing = reference - schema
        extra = schema - reference
        assert not missing and not extra, (
            f"{name} schema differs from {reference_name}: "
            f"missing={sorted(missing)!r} extra={sorted(extra)!r}"
        )
