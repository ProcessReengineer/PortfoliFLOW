# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""
core/chart_theme.py
===================
Loads and provides the chart theme configuration from config/chart_theme.json.

The theme is loaded once at first access and cached. All chart-rendering code
should obtain visual parameters from ``get_chart_theme()`` rather than
hardcoding colours, fonts, or sizes.

Usage::

    from core.chart_theme import get_chart_theme

    theme = get_chart_theme()
    line_colour = theme["colours"]["primary"]
    font_size   = theme["font"]["title_size"]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import matplotlib.font_manager

_theme_cache: dict[str, Any] | None = None
_logger = logging.getLogger(__name__)


def _resolve_font_family(candidates: list[str]) -> str:
    """Return the first font family from *candidates* that matplotlib can find.

    Args:
        candidates: Ordered list of font family names to try.

    Returns:
        The first available font family name, or ``"sans-serif"`` if none are found.
    """
    for name in candidates:
        if name == "sans-serif":
            return "sans-serif"
        try:
            matplotlib.font_manager.findfont(name, fallback_to_default=False)
            return name
        except ValueError:
            continue
    _logger.debug(
        "None of the preferred font families %s were found; falling back to sans-serif",
        candidates,
    )
    return "sans-serif"


def _config_path() -> Path:
    """Return the absolute path to the active chart theme JSON file.

    The active filename is determined by :class:`core.theme_service.ThemeService`.
    If the service has not been told otherwise (e.g. on first start, or in
    unit tests that do not go through the persistence layer), it returns
    its default — ``chart_theme.json`` — and behaviour matches the Phase A
    loader exactly.

    Returns:
        Path to the active chart theme JSON file under ``<repo_root>/config/``.
    """
    # Function-local import: see the matching note in ``core/ui_theme.py``.
    # ``core.theme_service`` reads JSON files from ``config/`` at construction
    # time, so a top-level import would risk a circular path.
    from core.theme_service import get_theme_service

    filename = get_theme_service().get_active_chart_theme_filename()
    return Path(__file__).resolve().parent.parent / "config" / filename


def get_chart_theme() -> dict[str, Any]:
    """Return the chart theme dictionary, loading from disk on first call."""
    global _theme_cache
    if _theme_cache is None:
        with open(_config_path(), encoding="utf-8") as f:
            _theme_cache = json.load(f)
        family = _theme_cache["font"]["family"]
        if isinstance(family, list):
            _theme_cache["font"]["family"] = _resolve_font_family(family)
    return _theme_cache


def reload_chart_theme() -> dict[str, Any]:
    """Force-reload the chart theme from disk."""
    global _theme_cache
    _theme_cache = None
    return get_chart_theme()
