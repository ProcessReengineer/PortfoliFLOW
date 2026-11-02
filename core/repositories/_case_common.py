# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared constants of the Cases persistence layer (private).

Kept out of :mod:`core.repositories.case_repository` so that module can
import the working-file repositories of
:mod:`core.repositories.case_working_file_repository` — which use these
names too — without an import cycle (D-CS-B-9). Both modules import from
here; nothing is re-exported from :mod:`core.repositories`.
"""

from __future__ import annotations

from typing import Any


class _Unset:
    """Type of :data:`_UNSET` — "argument not given", distinct from ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "_UNSET"


#: Sentinel for partial updates: a field left at ``_UNSET`` is not written,
#: while an explicit ``None`` clears a nullable column.
_UNSET: Any = _Unset()

# The states in which the working file (and the record's generic append
# path) accepts writes; a closed case is read-only in both layers.
_MUTABLE_STATES: frozenset[str] = frozenset({"open", "waiting"})
