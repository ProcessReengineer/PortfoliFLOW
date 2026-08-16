# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Admin — Application Settings module.

Purpose:
    Register the Application Settings entry point in the ModuleRegistry so
    that the GUI can discover and display the application-level configuration
    panel. All actual settings logic is implemented in
    :class:`~gui.widgets.application_settings_widget.ApplicationSettingsWidget`
    and in :mod:`core.theme_service` / :mod:`gui.theme_persistence`.

    For now the panel exposes UI and chart theme selection. Future settings
    (e.g. client-server endpoint configuration, user management) will be
    added here.
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class ApplicationSettings(BaseModule):
    """Registry entry for the application-level configuration section.

    Serves as the discoverable identity for the Application Settings entry
    in the Admin area. The module itself has no business logic — all
    settings are handled by :class:`ApplicationSettingsWidget` in the GUI
    and by :mod:`gui.theme_persistence` plus :mod:`core.theme_service`.

    Attributes:
        module_name: ``"application_settings"``
        module_area: ``"admin"``
    """

    module_name = "application_settings"
    module_area = "admin"

    def validate_inputs(self, **kwargs: Any) -> None:
        """No-op — this module accepts no inputs.

        Args:
            **kwargs: Ignored.
        """

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Return a status-ok acknowledgement.

        Configuration is performed through the
        :class:`ApplicationSettingsWidget` in the GUI; this module has no
        executable workflow of its own.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            ``{"status": "ok"}``
        """
        return {"status": "ok"}
