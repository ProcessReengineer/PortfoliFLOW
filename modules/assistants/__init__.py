# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Assistants modules — AI-powered automation and analysis tools."""

# Each import triggers the @registry.register decorator on the module class.
from modules.assistants import ai_settings  # noqa: F401
from modules.assistants import report_scraper  # noqa: F401
from modules.assistants import shirley  # noqa: F401
