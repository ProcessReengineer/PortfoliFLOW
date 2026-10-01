# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfoliFLOW module layer — the registered module shells, by Area.

Importing this package registers every module of every Area with the
ModuleRegistry. No runtime layer imports it; tests do, to populate the
registry (``docs/architecture.md``, section ``modules/``).
"""

import modules.front_office  # noqa: F401
import modules.back_office  # noqa: F401
import modules.investor_communication  # noqa: F401
import modules.assistants  # noqa: F401
import modules.watch_desk  # noqa: F401
import modules.planning_desk  # noqa: F401
import modules.cases  # noqa: F401
import modules.transactions  # noqa: F401
