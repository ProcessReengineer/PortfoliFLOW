# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`core.ui_theme` and the on-disk UI theme JSON files.

Covers:
    * Loader returns a non-empty dict and caches the result.
    * ``reload_ui_theme`` discards the cache.
    * The default theme exposes all required top-level sections.
    * All shipped UI themes (default + alternatives) share the same leaf
      schema — critical for the future Phase B theme picker, since a missing
      key in an alternative theme would crash widget code that re-exports it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core import ui_theme


_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_UI_THEME_FILES = [
    _CONFIG_DIR / "ui_theme.json",
    _CONFIG_DIR / "ui_theme_light.json",
    _CONFIG_DIR / "ui_theme_corporate_blue.json",
]


@pytest.fixture(autouse=True)
def _reset_ui_theme_cache() -> None:
    """Ensure each test starts with a fresh cache."""
    ui_theme._theme_cache = None


def _collect_leaf_paths(node: Any, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Return the set of leaf-key paths in a nested dict.

    Top-level keys whose names start with ``"_"`` (metadata such as
    ``_comment``, ``_version``, ``_display_name``) are excluded so that
    optional metadata does not break schema equality across files.

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


def test_get_ui_theme_returns_dict() -> None:
    """``get_ui_theme`` must return a non-empty dict."""
    theme = ui_theme.get_ui_theme()
    assert isinstance(theme, dict)
    assert theme  # non-empty


def test_get_ui_theme_is_cached() -> None:
    """Two consecutive calls must return the same object instance."""
    first = ui_theme.get_ui_theme()
    second = ui_theme.get_ui_theme()
    assert first is second


def test_reload_ui_theme_clears_cache() -> None:
    """``reload_ui_theme`` must return a fresh dict, distinct by identity."""
    first = ui_theme.get_ui_theme()
    second = ui_theme.reload_ui_theme()
    assert first is not second


def test_required_top_level_keys_present() -> None:
    """The default theme must declare every section the loader consumes."""
    theme = ui_theme.get_ui_theme()
    for section in ("background", "accent", "text", "border", "font", "semantic"):
        assert section in theme, f"missing top-level section: {section}"


def test_all_alternative_themes_have_same_schema() -> None:
    """Every shipped UI theme must expose the exact same leaf-key set.

    A missing leaf in an alternative theme would crash ``gui.theme`` on
    load (KeyError) once the theme is selected, so this is a hard
    guarantee for the future Phase B picker.
    """
    schemas: dict[str, set[tuple[str, ...]]] = {}
    for path in _UI_THEME_FILES:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        schemas[path.name] = _collect_leaf_paths(data)

    reference_name = _UI_THEME_FILES[0].name
    reference = schemas[reference_name]
    for name, schema in schemas.items():
        missing = reference - schema
        extra = schema - reference
        assert not missing and not extra, (
            f"{name} schema differs from {reference_name}: "
            f"missing={sorted(missing)!r} extra={sorted(extra)!r}"
        )
