# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Admin modules — user management, settings, and accounting."""

# Each import triggers the @registry.register decorator on the module class.
from modules.admin import application_settings  # noqa: F401
