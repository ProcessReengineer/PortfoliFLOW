# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""
core/ui_theme.py
================
Loads and provides the UI theme configuration from ``config/ui_theme.json``.

This module is the analogue of :mod:`core.chart_theme` for the UI surface: it
reads the theme dict from disk on first access, caches it at module level, and
exposes accessors that other parts of the application can call without knowing
where the values come from.

Consumers:

* :mod:`core.theme_service` — selects which ``config/ui_theme*.json`` file is
  active; this module calls back into it to resolve the filename.
* :mod:`scripts.generate_theme_artifacts` — reads the loaded dict and emits
  ``web/static/css/theme.css``, the CSS custom properties the web surface
  actually renders against. That stylesheet is the only persisted artefact;
  the JSON remains the single source of truth.

Unlike :mod:`core.chart_theme`, no font resolution helper is needed here —
CSS accepts a font-family list directly.

Usage::

    from core.ui_theme import get_ui_theme

    theme = get_ui_theme()
    primary_bg = theme["background"]["primary"]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.exceptions import ConfigurationError

_DEFAULT_THEME_FILENAME = "ui_theme.json"

_theme_cache: dict[str, Any] | None = None
_logger = logging.getLogger(__name__)


def _config_path() -> Path:
    """Return the absolute path to the active UI theme JSON file.

    The active filename is determined by :class:`core.theme_service.ThemeService`.
    If the service has not been told otherwise (e.g. on first start, or in
    unit tests that do not go through the persistence layer), it returns
    its default — ``ui_theme.json`` — and behaviour matches the Phase A
    loader exactly.

    Returns:
        Path to the active UI theme JSON file under ``<repo_root>/config/``.
    """
    # Function-local import: ``core.theme_service`` reads JSON files from
    # ``config/`` at construction time, so importing it at module-load time
    # would create a circular path (theme_service → ui_theme → theme_service)
    # if either side starts touching the other on import. Keeping this local
    # is the one acceptable function-local import in this project.
    from core.theme_service import get_theme_service

    filename = get_theme_service().get_active_ui_theme_filename()
    return Path(__file__).resolve().parent.parent / "config" / filename


def get_ui_theme() -> dict[str, Any]:
    """Return the UI theme dictionary, loading from disk on first call.

    The result is cached at module level. Subsequent calls return the same
    dict instance.

    Returns:
        The parsed UI theme as a nested dictionary.

    Raises:
        ConfigurationError: If the theme file is missing, unreadable, or
            contains invalid JSON. Failure is loud by design — a corrupted
            theme should crash startup rather than render a broken GUI.
    """
    global _theme_cache
    if _theme_cache is not None:
        return _theme_cache

    path = _config_path()
    try:
        with open(path, encoding="utf-8") as f:
            _theme_cache = json.load(f)
    except FileNotFoundError as exc:
        _logger.error("UI theme file not found at %s", path)
        raise ConfigurationError(f"UI theme file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        _logger.error("UI theme file at %s contains invalid JSON: %s", path, exc)
        raise ConfigurationError(f"UI theme file contains invalid JSON ({path}): {exc}") from exc
    except OSError as exc:
        _logger.error("Could not read UI theme file at %s: %s", path, exc)
        raise ConfigurationError(f"Could not read UI theme file: {path}") from exc

    return _theme_cache


def reload_ui_theme() -> dict[str, Any]:
    """Force-reload the UI theme from disk and return the fresh dict.

    Returns:
        The newly-loaded UI theme dictionary.

    Raises:
        ConfigurationError: Propagated from :func:`get_ui_theme` on failure.
    """
    global _theme_cache
    _theme_cache = None
    return get_ui_theme()
