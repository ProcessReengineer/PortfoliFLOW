# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases modules — the eighth top-level Area (ADR-0107).

The Cases area is where open questions about the portfolio are worked to a
documented close: the to-do list in front, the decision log behind. The
real work lives in the web surface (``web/routes/cases.py`` plus its
templates) and the persistence layer (``core/models/case.py``,
``core/repositories/case_repository.py`` and its attachment sibling). These
three thin :class:`~core.base_module.BaseModule` subclasses exist so the
Area has its registered Modules — one per Section — keeping the
area-and-module conceptual integrity ADR-0058 requires and so
``registry.list_by_area("cases")`` resolves.

Each import triggers the ``@registry.register`` decorator on its class.
"""

from modules.cases import archive  # noqa: F401
from modules.cases import open_cases  # noqa: F401
from modules.cases import recently_closed  # noqa: F401
