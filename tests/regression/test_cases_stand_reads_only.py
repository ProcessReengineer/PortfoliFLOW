# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Where we stand reads; it never writes and never computes (D-CS-M-14, ADR-0138 §6).

The Cases services compose a live block from the book, the Watch Desk and
Transactions on every request. Two rules make that safe and keep it honest:

1. **No writes.** Looking at a case must never change one — no record entry,
   no milestone, no working-file row, no ticket, no watch-state.
2. **No metric of its own.** The block states what the book already
   computes: the multiples and the IRR arrive through ``InvestmentService``,
   the subject's status through ``read_family_status`` on the one resolution
   ``resolve_watch_desk`` returns — never through the pure calculation layer,
   a restated threshold or a second observation path (ADR-0116 §1, §6).

Structural, like ``test_watch_desk_single_resolution.py``: a second path that
agrees today is the defect, so the guard looks at the code, not the values.

**Readers never write; writers never compute** (P-CS-2b). The system-line
module (``services.cases.system_lines``) writes by design — through
``CaseRepository.add_system_line`` only — so the no-writes rule is scoped to
the reader, ``stand.py``. The no-metric rules (no pure calculation layer, no
second observation path) keep walking the whole package, writers included:
a system line states the figures its event carries and computes none.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from types import ModuleType

import services.cases as cases_package
import services.cases.stand as stand_module

#: Calls that would write — to the record, the working file, a ticket or a
#: watch-state row — or end a transaction.
_WRITES = (
    "insert(",
    "update(",
    "delete(",
    "session.add(",
    ".commit(",
    ".flush(",
    "_insert_entry",
    "append_entry(",
    ".milestone(",
    "_milestone(",
    "record_decision(",
    "hand_over(",
    ".wait(",
    "lift_wait(",
    ".close(",
    ".create(",
    ".keep(",
    ".upsert",
    "set_status(",
    "add_effects(",
)

_ANALYTICS = "services.analytics"

#: A producer of the observation layer (``observe_signal_families`` and kin).
_PRODUCER_NAME = re.compile(r"\bobserve_")


def _cases_modules() -> list[ModuleType]:
    """Return every module of the ``services.cases`` package, the package included."""
    modules: list[ModuleType] = [cases_package]
    for info in pkgutil.walk_packages(cases_package.__path__, prefix="services.cases."):
        modules.append(importlib.import_module(info.name))
    assert stand_module in modules
    return modules


def test_the_stand_writes_nothing() -> None:
    """Rendering a case must never change one (D-CS-M-14).

    Scoped to the reader: the system-line writers write by design.
    """
    for module in (stand_module,):
        source = inspect.getsource(module)
        for write in _WRITES:
            assert write not in source, (
                f"{module.__name__} performs {write!r}; the Cases read side is "
                "read-only — looking at a case must never change one (D-CS-M-14)."
            )


def test_the_stand_imports_no_analytics() -> None:
    """The multiples arrive through the investment service, never the pure layer."""
    for module in _cases_modules():
        assert _ANALYTICS not in inspect.getsource(module), (
            f"{module.__name__} names the pure calculation layer; the block "
            "computes no metric the book does not already compute (ADR-0138 §6)."
        )
        for name, value in vars(module).items():
            origin = getattr(value, "__module__", None) or getattr(value, "__name__", "")
            assert not str(origin).startswith(_ANALYTICS), (
                f"{module.__name__}.{name} comes from {origin}; the Cases read "
                "side reaches the book through its services (D-CS-M-14)."
            )
    assert "InvestmentService" in vars(stand_module)


def test_the_stand_resolves_through_the_shared_function() -> None:
    """One resolution, with no defaults of its own and no restated threshold."""
    namespace = vars(stand_module)
    assert "resolve_watch_desk" in namespace
    assert "read_family_status" in namespace

    source = inspect.getsource(stand_module)
    assert "DEFAULT_FLOOR_CONFIG" not in source
    assert 'Decimal("9' not in source, "the stand restates a WARN threshold (ADR-0116 §3)."
    assert "defaults=" not in source


def test_the_stand_is_not_a_second_observation_path() -> None:
    """The subject's status is the monitor's typed read, not a fresh observation."""
    for module in _cases_modules():
        source = inspect.getsource(module)
        assert "signal_observation" not in source, (
            f"{module.__name__} imports the observation layer; the status "
            "arrives through read_family_status (ADR-0116 §6)."
        )
        assert not _PRODUCER_NAME.search(source), (
            f"{module.__name__} names an observation producer; the status "
            "arrives through read_family_status (ADR-0116 §6)."
        )
