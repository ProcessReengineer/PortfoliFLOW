# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Assistants — Shirley AI assistant module (registry shell).

Purpose:
    The registry-discoverable identity for the Shirley chat assistant in
    the Assistants area. This module carries no conversational logic and
    has no programmatic entry point.

The real entry points are:

- the **web chat route**, which drives
  :func:`services.ai_service_core.get_ai_service_core` directly, and
- the **Telegram bot** under ``bot/``, a sibling consumer of the same
  AI service core (ADR-0030).

Both bypass this module entirely. It exists for registry completeness
(ADR-0058). No module should call the OpenAI SDK directly — use
:func:`~services.ai_service_core.get_ai_service_core`.
"""

from __future__ import annotations

from typing import Any

from core.base_module import BaseModule
from modules.module_registry import registry


@registry.register
class Shirley(BaseModule):
    """Shirley AI assistant registry shell.

    Attributes:
        module_name: ``"shirley"``
        module_area: ``"assistants"``
    """

    module_name = "shirley"
    module_area = "assistants"

    def run(self, *args: Any, **kwargs: Any) -> dict:
        """No programmatic entry point; see the module docstring.

        Shirley is reached through the web chat route or the Telegram
        bot, both of which use ``AIServiceCore`` directly.

        Returns:
            dict with key ``"status"``.
        """
        return {"status": "stub"}
