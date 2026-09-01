# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfoliFLOW module layer — all business logic.

Importing this package triggers registration of all modules across all
areas. The GUI and main.py should import this package (or individual
area packages) to ensure the ModuleRegistry is fully populated.
"""

import modules.front_office  # noqa: F401
import modules.back_office  # noqa: F401
import modules.admin  # noqa: F401
import modules.investor_communication  # noqa: F401
import modules.assistants  # noqa: F401
import modules.watch_desk  # noqa: F401
import modules.planning_desk  # noqa: F401
import modules.cases  # noqa: F401
import modules.transactions  # noqa: F401
