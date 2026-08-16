# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: ``services.investments`` is a lazy (PEP 562) façade.

ADR-0104 §2 makes two members of this package mandatory seams for the
*ephemeral* overlay layer: :func:`services.investments.archetype.resolve_archetype`
and
:data:`services.investments.flow_type_invariants.OVERLAY_EXEMPT_FLOW_TYPES` —
the single formulations of archetype dispatch and of the ADR-0103 §5 exemption
invariant, which must never be restated locally. Both modules are stdlib-only.
Their DB-coupled *neighbours* in the same package are not, and while the
package ``__init__`` re-exported everything eagerly, importing either pure seam
dragged ``core.repositories`` — and with it SQLAlchemy — into
:data:`sys.modules`. That made the ADR-0104 §1 purity claim for
``services/overlay/`` unprovable by import graph (S2.1b relaxed the overlay's
leak check to say so); S2.1c made the façade lazy and restored it.

This module pins the property **at its source**, so an eager import
reintroduced into ``services/investments/__init__.py`` fails here — locally and
legibly — rather than only as an inherited failure in
``tests/regression/test_overlay_layer_pure.py``.

Two things are guarded, and both are load-bearing:

* **Laziness.** Importing a pure seam in a fresh interpreter leaves SQLAlchemy
  and ``core.repositories`` out of :data:`sys.modules`.
* **Completeness.** Every name in :data:`services.investments.__all__` still
  resolves. Laziness moves name resolution from import time to attribute
  access, which is exactly where a name can go missing silently — a typo in
  the lazy mapping would otherwise surface only at the consumer's call site.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import services.investments as investments_facade

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def _run(code: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter rooted at the repository."""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(_REPO_ROOT),
    )


def test_importing_a_pure_seam_pulls_in_no_db() -> None:
    """The ADR-0104 §2 seams import without their DB-coupled neighbours.

    The subprocess is required: the parent pytest process has long since
    imported SQLAlchemy through other test modules, so only a clean
    interpreter can observe what this import *alone* costs.
    """
    completed = _run(
        "import sys\n"
        "from services.investments.archetype import resolve_archetype  # noqa: F401\n"
        "from services.investments.flow_type_invariants import (  # noqa: F401\n"
        "    OVERLAY_EXEMPT_FLOW_TYPES,\n"
        ")\n"
        "leaks = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'sqlalchemy' or m.startswith('sqlalchemy.')\n"
        "    or m == 'core.repositories' or m.startswith('core.repositories.')\n"
        ")\n"
        "assert not leaks, f'services.investments is importing eagerly: {leaks}'\n"
        "print('OK')\n"
    )
    assert completed.returncode == 0, (
        "importing the ADR-0104 §2 archetype / exemption seams pulled the "
        "book into sys.modules. services/investments/__init__.py must stay a "
        "lazy PEP 562 façade — an eager `from services.investments.<db "
        "module> import ...` at its top level is what breaks this.\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    assert "OK" in completed.stdout


def test_facade_still_exports_every_public_name() -> None:
    """Every ``__all__`` name resolves through the lazy ``__getattr__``."""
    missing: list[str] = []
    for name in investments_facade.__all__:
        try:
            getattr(investments_facade, name)
        except AttributeError:
            missing.append(name)
    assert not missing, (
        "the lazy façade dropped public names — every entry in __all__ must "
        f"appear in the name-to-module mapping: {missing}"
    )


def test_facade_still_resolves_submodules_by_attribute() -> None:
    """``import services.investments`` keeps submodule attribute access working."""
    assert investments_facade.aum.CASH_TYPE == "cash"
    assert investments_facade.archetype.resolve_archetype is not None


def test_facade_rejects_unknown_names() -> None:
    """An unknown attribute raises ``AttributeError``, not ``ModuleNotFoundError``."""
    try:
        investments_facade.no_such_member  # noqa: B018
    except AttributeError as exc:
        assert "has no attribute 'no_such_member'" in str(exc)
    else:  # pragma: no cover - the façade would be silently permissive
        raise AssertionError("expected AttributeError for an unknown attribute")
