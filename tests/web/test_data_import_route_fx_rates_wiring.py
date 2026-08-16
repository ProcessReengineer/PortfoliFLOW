# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Wiring guard: the upload route persists the ``FX rates`` sheet (ADR-0099 §5).

Regression coverage mirroring
``tests/web/test_data_import_route_benchmarks.py``, whose reason for
existing was a hotfix where
``InvestmentService.transform_benchmarks_from_upload`` was unit-tested in
isolation but **never invoked** by ``web/routes/data_import.py`` — the
JSONB upload carried the sheets, but nothing landed in the DB.

``transform_fx_rates_from_upload`` (Block 2 of the multi-currency
programme) is exactly the same shape of hazard: a fully-tested transform
that the route must actually call, or the ``FX rates`` sheet is silently
dropped from the import write path.

The benchmark guard is a live-DB behavioural test driven by the real v24
sample workbook. No FX-bearing sample workbook exists yet — the owner
produces ``Testdaten_v31`` (with ``USD/EUR`` / ``GBP/EUR`` series)
manually after this block lands — so the FX guard is instead a static
AST check: it fails the moment the ``transform_fx_rates_from_upload``
call disappears from the route module, needs no database, and cannot
be fooled by the call surviving only inside a comment or docstring.

The end-to-end round-trip (hand-built ``FX rates`` workbook → ``fx_rates``
rows whose triples match the headers and cells) is covered behaviourally
at the service layer in
``tests/services/test_investment_service_transform_fx_rates.py``.
"""

from __future__ import annotations

import ast
import pathlib

_ROUTE_PATH = pathlib.Path(__file__).resolve().parents[2] / "web" / "routes" / "data_import.py"


def _attribute_calls(tree: ast.AST, attr: str) -> list[ast.Call]:
    """Return every ``ast.Call`` whose callee is a ``.<attr>(...)`` access."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
    ]


def test_route_calls_transform_fx_rates_from_upload() -> None:
    """The route must call ``transform_fx_rates_from_upload``.

    If this guard goes red, the FX rates sheet is being parsed and
    stored in the upload snapshot but never persisted into ``fx_rates``
    — the exact silent-drop failure the benchmark guard was written to
    catch. The fix is to re-wire the call into the write branch of
    ``post_import_upload_as_investments``, not to relax this test.
    """
    tree = ast.parse(_ROUTE_PATH.read_text(encoding="utf-8"))
    calls = _attribute_calls(tree, "transform_fx_rates_from_upload")
    assert calls, (
        "web/routes/data_import.py no longer calls "
        "transform_fx_rates_from_upload — the FX rates sheet would be "
        "silently dropped from the import write path (ADR-0099 §5)."
    )


def test_route_constructs_fx_rate_repository() -> None:
    """The route must construct an ``FxRateRepository`` for the FX transform.

    A companion to the call-site guard: the transform takes the FX
    repository as a keyword argument, so a live ``FxRateRepository(...)``
    construction in the route is the second half of the wiring. Losing
    it would break the call the first guard protects.
    """
    tree = ast.parse(_ROUTE_PATH.read_text(encoding="utf-8"))
    constructs_repo = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FxRateRepository"
        for node in ast.walk(tree)
    )
    assert constructs_repo, (
        "web/routes/data_import.py no longer constructs an "
        "FxRateRepository — the FX rates transform cannot be wired "
        "without it (ADR-0099 §5)."
    )
