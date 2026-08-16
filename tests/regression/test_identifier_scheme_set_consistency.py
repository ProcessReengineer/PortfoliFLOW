# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Lock the three sources of truth for the identifier scheme set (ADR-0096).

ADR-0096 §Consequences notes that the closed scheme set lives in three
places — the DB CHECK on ``investment_identifiers.scheme`` (migration b023),
the ``IDENTIFIER_SCHEMES`` frozenset in ``core.models.investment_identifier``,
and the ``IDENTIFIER_SCHEMES`` frozenset in ``services.market_data.dto`` — and
that the migration slice must move all of them together. The market-data DTO
copy is intentionally NOT an import of the model copy (the market-data layer
must not import ``core.models``; see
``tests/regression/test_market_data_layer_pure.py``), so nothing but a test
keeps them from drifting.

This guard pins:

* the two code frozensets are equal and equal to the ADR-0096 seven-scheme
  set;
* the b023 migration's upgrade-target CHECK literal names exactly that set
  (a string-level assertion against the migration source), and its
  downgrade-target names exactly the ADR-0090 five-scheme set.

Pure — no DB, no network.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from core.models.investment_identifier import IDENTIFIER_SCHEMES as MODEL_SCHEMES
from services.market_data.dto import IDENTIFIER_SCHEMES as DTO_SCHEMES

# The ADR-0096 seven-scheme set, written out explicitly here so the guard has
# an independent third opinion rather than comparing the two live constants to
# each other alone.
_EXPECTED_SCHEMES = frozenset(
    {"isin", "ticker", "figi", "cusip", "internal", "preqin", "pitchbook"}
)
# The ADR-0090 five-scheme set — the b023 downgrade target.
_EXPECTED_OLD_SCHEMES = frozenset({"isin", "ticker", "figi", "cusip", "internal"})

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "versions"
    / "2026_07_07_1300_b023_extend_identifier_schemes.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("b023_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tokens(literal: str) -> frozenset[str]:
    """Extract the single-quoted scheme tokens from a CHECK value list."""
    return frozenset(re.findall(r"'([a-z_]+)'", literal))


def test_model_and_dto_scheme_sets_are_equal() -> None:
    assert MODEL_SCHEMES == DTO_SCHEMES
    assert MODEL_SCHEMES == _EXPECTED_SCHEMES


def test_b023_migration_upgrade_check_names_exactly_the_seven_set() -> None:
    module = _load_migration()
    assert _tokens(module._SCHEMES_NEW) == _EXPECTED_SCHEMES


def test_b023_migration_downgrade_check_names_exactly_the_five_set() -> None:
    module = _load_migration()
    assert _tokens(module._SCHEMES_OLD) == _EXPECTED_OLD_SCHEMES
