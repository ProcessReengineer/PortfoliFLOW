# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :mod:`core.theme_service` and the loader-service interaction.

Covers:
    * Discovery returns the default theme first, alternatives following.
    * Default filenames are the active filenames immediately after
      construction.
    * Setters validate against discovered files and raise
      :class:`ConfigurationError` on unknown values.
    * The :func:`get_theme_service` accessor returns a stable singleton.
    * Setting a different active filename + calling
      :func:`core.ui_theme.reload_ui_theme` makes the loader pick up the
      new file (the regression test the spec calls for).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core import chart_theme as chart_theme_module
from core import theme_service as theme_service_module
from core import ui_theme as ui_theme_module
from core.exceptions import ConfigurationError
from core.theme_service import ThemeService, get_theme_service


@pytest.fixture(autouse=True)
def _reset_theme_service_singleton() -> Iterator[None]:
    """Ensure each test starts with a fresh service instance.

    Also resets the loader caches so that any prior cached theme is
    discarded; otherwise a test that switched the active filename could
    leak into a later test that expected the default.
    """
    theme_service_module._instance = None
    ui_theme_module._theme_cache = None
    chart_theme_module._theme_cache = None
    yield
    theme_service_module._instance = None
    ui_theme_module._theme_cache = None
    chart_theme_module._theme_cache = None


def test_lists_include_defaults_first() -> None:
    """The default theme file must appear first in both lists."""
    svc = ThemeService()
    ui = svc.list_ui_themes()
    chart = svc.list_chart_themes()
    assert ui[0].filename == "ui_theme.json"
    assert ui[0].is_default is True
    assert chart[0].filename == "chart_theme.json"
    assert chart[0].is_default is True


def test_lists_contain_alternatives() -> None:
    """The Phase A alternative themes must appear in the discovery output."""
    svc = ThemeService()
    ui_filenames = {info.filename for info in svc.list_ui_themes()}
    chart_filenames = {info.filename for info in svc.list_chart_themes()}
    assert "ui_theme_light.json" in ui_filenames
    assert "ui_theme_corporate_blue.json" in ui_filenames
    assert "chart_theme_light.json" in chart_filenames
    assert "chart_theme_print.json" in chart_filenames


def test_default_is_active_initially() -> None:
    """A fresh service must consider the default file the active one."""
    svc = ThemeService()
    assert svc.get_active_ui_theme_filename() == "ui_theme.json"
    assert svc.get_active_chart_theme_filename() == "chart_theme.json"


def test_set_active_validates_filename() -> None:
    """Unknown filenames must be rejected with :class:`ConfigurationError`."""
    svc = ThemeService()
    with pytest.raises(ConfigurationError):
        svc.set_active_ui_theme_filename("does_not_exist.json")
    with pytest.raises(ConfigurationError):
        svc.set_active_chart_theme_filename("does_not_exist.json")


def test_set_active_accepts_known_filename() -> None:
    """Setting a discovered filename must succeed and be reflected on read."""
    svc = ThemeService()
    svc.set_active_ui_theme_filename("ui_theme_light.json")
    svc.set_active_chart_theme_filename("chart_theme_light.json")
    assert svc.get_active_ui_theme_filename() == "ui_theme_light.json"
    assert svc.get_active_chart_theme_filename() == "chart_theme_light.json"


def test_singleton_returns_same_instance() -> None:
    """:func:`get_theme_service` must return the same object on repeat calls."""
    first = get_theme_service()
    second = get_theme_service()
    assert first is second


def test_display_names_are_resolved() -> None:
    """Each :class:`ThemeInfo` must carry a non-empty display name."""
    svc = ThemeService()
    for info in svc.list_ui_themes() + svc.list_chart_themes():
        assert isinstance(info.display_name, str)
        assert info.display_name.strip()


def test_loader_honours_active_filename_ui() -> None:
    """After ``set_active_ui_theme_filename`` + reload, the loader uses the new file."""
    svc = get_theme_service()
    svc.set_active_ui_theme_filename("ui_theme_light.json")
    fresh = ui_theme_module.reload_ui_theme()
    assert fresh["_display_name"] == "Light"

    # Restore default explicitly so a buggy reset elsewhere can't leak.
    svc.set_active_ui_theme_filename("ui_theme.json")
    ui_theme_module.reload_ui_theme()


def test_loader_honours_active_filename_chart() -> None:
    """Same regression check for the chart theme loader."""
    svc = get_theme_service()
    svc.set_active_chart_theme_filename("chart_theme_print.json")
    fresh = chart_theme_module.reload_chart_theme()
    assert fresh["_display_name"] == "Print (Greyscale)"

    svc.set_active_chart_theme_filename("chart_theme.json")
    chart_theme_module.reload_chart_theme()
