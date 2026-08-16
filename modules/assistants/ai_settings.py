# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Assistants — AI Settings module.

Purpose:
    Register the AI Settings entry point in the ModuleRegistry so that the
    GUI can discover and display the AI configuration panel.  All actual
    settings logic is implemented in the ``AIService`` singleton
    (``services.ai_service``) and the ``AISettingsWidget``
    (``gui.widgets.ai_settings_widget``).

    This module exists so that the Assistants area has a well-defined,
    registry-discoverable entry for the settings section.
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class AISettings(BaseModule):
    """Registry entry for the AI configuration settings section.

    Serves as the discoverable identity for the Settings section of the
    Assistants area.  The module itself has no business logic — all AI
    configuration is handled by :class:`~services.ai_service.AIService`
    and surfaced in the GUI via
    :class:`~gui.widgets.ai_settings_widget.AISettingsWidget`.

    Attributes:
        module_name: ``"ai_settings"``
        module_area: ``"assistants"``
    """

    module_name = "ai_settings"
    module_area = "assistants"

    def validate_inputs(self, **kwargs: Any) -> None:
        """No-op — this module accepts no inputs.

        Args:
            **kwargs: Ignored.
        """

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """Return a status-ok acknowledgement.

        The AI Settings module has no executable workflow of its own.
        Configuration is performed through the :class:`AISettingsWidget` in
        the GUI.

        Args:
            *args: Ignored.
            **kwargs: Ignored.

        Returns:
            ``{"status": "ok"}``
        """
        return {"status": "ok"}
