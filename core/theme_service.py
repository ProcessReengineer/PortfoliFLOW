# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""
core/theme_service.py
=====================
Framework-agnostic discovery and active-filename resolution for shipped
UI and chart themes.

The Phase A externalisation (see ``config/ui_theme*.json`` and
``config/chart_theme*.json``) made the loaders read a single hardcoded
filename. Phase B adds a user-facing picker. ``ThemeService`` is the
piece in the middle: it knows which theme files are shipped, resolves
display names, and tracks which filename is currently *active* for each
kind of theme.

Design notes:
    * This module imports nothing from PyQt6 or from the ``gui``,
      ``services``, ``modules``, or ``analytics`` layers — see
      ``CLAUDE.md`` dependency rules. ``QSettings``-based persistence
      lives in ``gui/theme_persistence.py``.
    * The active filename is held in process memory only. It is
      assigned at application start by the persistence layer, and read
      by the theme loaders on the next reload. The service never
      triggers a reload itself.
    * Discovery is limited to the ``config/`` directory at repo root;
      external theme files are not supported in Phase B.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from core.exceptions import ConfigurationError

_logger = logging.getLogger(__name__)

_UI_DEFAULT_FILENAME = "ui_theme.json"
_CHART_DEFAULT_FILENAME = "chart_theme.json"

_UI_GLOB_PATTERNS = ("ui_theme.json", "ui_theme_*.json")
_CHART_GLOB_PATTERNS = ("chart_theme.json", "chart_theme_*.json")


@dataclass(frozen=True)
class ThemeInfo:
    """Metadata for one shipped theme file.

    Attributes:
        filename: Bare filename inside ``config/`` (e.g.
            ``"ui_theme_light.json"``).
        display_name: Human-readable label as declared in the file's
            ``_display_name`` field, or a humanised filename stem if the
            field is missing.
        is_default: ``True`` iff ``filename`` equals
            ``"ui_theme.json"`` or ``"chart_theme.json"``.
    """

    filename: str
    display_name: str
    is_default: bool


def _config_dir() -> Path:
    """Return the absolute path to the ``config/`` directory.

    Returns:
        ``<repo_root>/config``.
    """
    return Path(__file__).resolve().parent.parent / "config"


def _humanise_stem(stem: str) -> str:
    """Return a human-readable label derived from a filename stem.

    Used when a theme file has no ``_display_name`` field or cannot be
    parsed as JSON.

    Args:
        stem: The filename without its ``.json`` extension.

    Returns:
        A title-cased label (e.g. ``"ui_theme_light"`` →
        ``"Ui Theme Light"``).
    """
    return stem.replace("_", " ").strip().title()


def _read_display_name(path: Path) -> str:
    """Read ``_display_name`` from a theme JSON file, with fallback.

    Args:
        path: Absolute path to a theme JSON file.

    Returns:
        The value of the ``_display_name`` field if present and a
        non-empty string; otherwise a humanised version of the
        filename stem.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning(
            "Could not read display name from %s (%s); falling back to filename.",
            path,
            exc,
        )
        return _humanise_stem(path.stem)

    name = data.get("_display_name")
    if isinstance(name, str) and name.strip():
        return name
    return _humanise_stem(path.stem)


def _discover(patterns: tuple[str, ...], default_filename: str) -> list[ThemeInfo]:
    """Discover all theme files matching the given globs in ``config/``.

    The default file (``default_filename``) is placed first; the
    remaining files follow in alphabetical order by filename.

    Args:
        patterns: One or more glob patterns relative to ``config/``.
        default_filename: The filename that is treated as the default
            theme for this kind. Must be matched by one of the patterns.

    Returns:
        Ordered list of :class:`ThemeInfo`, defaults first.
    """
    config_dir = _config_dir()
    seen: dict[str, Path] = {}
    for pattern in patterns:
        for path in config_dir.glob(pattern):
            if path.is_file():
                seen.setdefault(path.name, path)

    if default_filename in seen:
        default_path = seen.pop(default_filename)
        ordered = [(default_filename, default_path)]
    else:
        ordered = []
        _logger.warning(
            "Default theme file %s not found in %s.",
            default_filename,
            config_dir,
        )

    for name in sorted(seen.keys()):
        ordered.append((name, seen[name]))

    return [
        ThemeInfo(
            filename=name,
            display_name=_read_display_name(path),
            is_default=(name == default_filename),
        )
        for name, path in ordered
    ]


class ThemeService:
    """Discovery and active-filename tracking for shipped themes.

    The service scans ``config/`` once at construction time and exposes
    the resulting :class:`ThemeInfo` lists. The currently *active*
    filename for each kind starts at the default and can be changed via
    the setters. The setters validate the supplied filename against the
    discovered list and raise :class:`ConfigurationError` on unknown
    values.

    The service does not perform any persistence — that is the job of
    :mod:`gui.theme_persistence`. It also does not reload theme caches
    in :mod:`core.ui_theme` / :mod:`core.chart_theme`; the loaders pick
    up the new filename on their next read or explicit reload.
    """

    def __init__(self) -> None:
        self._ui_themes: list[ThemeInfo] = _discover(_UI_GLOB_PATTERNS, _UI_DEFAULT_FILENAME)
        self._chart_themes: list[ThemeInfo] = _discover(
            _CHART_GLOB_PATTERNS, _CHART_DEFAULT_FILENAME
        )
        self._active_ui_filename: str = _UI_DEFAULT_FILENAME
        self._active_chart_filename: str = _CHART_DEFAULT_FILENAME

    def list_ui_themes(self) -> list[ThemeInfo]:
        """Return the discovered UI themes, defaults first.

        Returns:
            A new list copy of :class:`ThemeInfo` entries.
        """
        return list(self._ui_themes)

    def list_chart_themes(self) -> list[ThemeInfo]:
        """Return the discovered chart themes, defaults first.

        Returns:
            A new list copy of :class:`ThemeInfo` entries.
        """
        return list(self._chart_themes)

    def get_active_ui_theme_filename(self) -> str:
        """Return the filename of the currently active UI theme.

        Returns:
            E.g. ``"ui_theme.json"`` or ``"ui_theme_light.json"``.
        """
        return self._active_ui_filename

    def get_active_chart_theme_filename(self) -> str:
        """Return the filename of the currently active chart theme.

        Returns:
            E.g. ``"chart_theme.json"`` or ``"chart_theme_print.json"``.
        """
        return self._active_chart_filename

    def set_active_ui_theme_filename(self, filename: str) -> None:
        """Set the active UI theme filename.

        The change does not trigger a loader reload — the next call to
        :func:`core.ui_theme.get_ui_theme` (after a cache clear via
        :func:`core.ui_theme.reload_ui_theme`) will pick up the new
        file.

        Args:
            filename: One of the filenames returned by
                :meth:`list_ui_themes`.

        Raises:
            ConfigurationError: If ``filename`` is not a discovered UI
                theme.
        """
        self._validate_filename(filename, self._ui_themes, kind="UI")
        self._active_ui_filename = filename

    def set_active_chart_theme_filename(self, filename: str) -> None:
        """Set the active chart theme filename.

        Args:
            filename: One of the filenames returned by
                :meth:`list_chart_themes`.

        Raises:
            ConfigurationError: If ``filename`` is not a discovered
                chart theme.
        """
        self._validate_filename(filename, self._chart_themes, kind="chart")
        self._active_chart_filename = filename

    @staticmethod
    def _validate_filename(filename: str, themes: list[ThemeInfo], kind: str) -> None:
        """Raise :class:`ConfigurationError` if ``filename`` is unknown.

        Args:
            filename: Candidate filename to validate.
            themes: List of discovered themes to validate against.
            kind: Human-readable label (``"UI"`` or ``"chart"``) for
                the error message.

        Raises:
            ConfigurationError: If ``filename`` does not appear in
                ``themes``.
        """
        known = {t.filename for t in themes}
        if filename not in known:
            raise ConfigurationError(
                f"Unknown {kind} theme filename: {filename!r}. Known: {sorted(known)!r}"
            )


_instance: ThemeService | None = None


def get_theme_service() -> ThemeService:
    """Return the application-wide :class:`ThemeService` singleton.

    The first call constructs the service and triggers theme discovery.
    Subsequent calls return the same instance.

    Returns:
        The shared :class:`ThemeService`.
    """
    global _instance
    if _instance is None:
        _instance = ThemeService()
    return _instance
