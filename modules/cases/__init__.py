# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Cases modules — the eighth top-level Area (ADR-0107, ADR-0138 §12).

The Cases area is where questions and tasks about the book are kept open
longer than one conversation: the working file in front, the record behind.
The real work lives in the web surface (``web/routes/cases.py`` plus its
templates) and the persistence layer (``core/models/case.py``,
``core/repositories/case_repository.py`` and its siblings). The area is one
Section, so it has one thin :class:`~core.base_module.BaseModule` subclass —
keeping the area-and-module conceptual integrity ADR-0058 requires and so
``registry.list_by_area("cases")`` resolves.

The import triggers the ``@registry.register`` decorator on its class.
"""

from modules.cases import working_file  # noqa: F401
