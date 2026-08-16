# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Schema checks for the JSON fixtures under ``services/data_normalization/fixtures/``.

The migration and bootstrap paths both load these fixtures and assume
a stable per-entry shape. These tests assert the file parses, is
non-empty, and every entry carries the required keys with the right
types. The actual database-seeding behaviour is covered by the
migration test (``alembic upgrade``) and the bootstrap test
(``test_bootstrap_installs_default_asset_classes``) respectively.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "services" / "data_normalization" / "fixtures"


def _load(name: str) -> list[dict[str, object]]:
    payload = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, list), f"{name}: expected top-level JSON array"
    assert payload, f"{name}: fixture is empty"
    return payload


# ---------------------------------------------------------------------------
# anlv_categories.json
# ---------------------------------------------------------------------------


def test_anlv_categories_fixture_loads_with_required_keys() -> None:
    """Every entry must carry the columns the b010 seed inserts.

    Per ADR-0057 §Schema the catalogue columns are ``code``,
    ``paragraph_label``, ``display_name``, ``description`` (nullable),
    ``sort_order``. The fixture file is the migration's seed source;
    missing keys here would surface as ``KeyError`` at migration time.
    """
    entries = _load("anlv_categories.json")
    seen_codes: set[str] = set()
    seen_sort_orders: set[int] = set()
    for entry in entries:
        assert isinstance(entry["code"], str) and entry["code"].startswith("anlv_")
        assert isinstance(entry["paragraph_label"], str) and entry["paragraph_label"]
        assert isinstance(entry["display_name"], str) and entry["display_name"]
        # description is nullable
        assert "description" in entry
        assert entry["description"] is None or isinstance(entry["description"], str)
        assert isinstance(entry["sort_order"], int)
        seen_codes.add(entry["code"])
        seen_sort_orders.add(entry["sort_order"])

    # Spot-check the codes referenced by the v21 testdata
    for required in ("anlv_13", "anlv_14", "anlv_15"):
        assert required in seen_codes, (
            f"v21 testdata references {required!r} but the fixture is missing it"
        )

    # sort_order must be unique so list_all() has a deterministic order.
    assert len(seen_sort_orders) == len(entries), (
        "Duplicate sort_order values would produce a non-deterministic catalogue order"
    )


# ---------------------------------------------------------------------------
# default_asset_classes.json
# ---------------------------------------------------------------------------


def test_default_asset_classes_fixture_loads_with_required_keys() -> None:
    """Every entry must carry the columns ``install_default_asset_classes`` reads."""
    entries = _load("default_asset_classes.json")
    seen_codes: set[str] = set()
    seen_sort_orders: set[int] = set()
    for entry in entries:
        assert isinstance(entry["code"], str) and entry["code"]
        # snake_case lowercase per ADR-0008
        assert entry["code"] == entry["code"].lower()
        assert " " not in entry["code"]
        assert isinstance(entry["display_name"], str) and entry["display_name"]
        # description is nullable but the V1 fixture supplies one for every row
        assert "description" in entry
        assert entry["description"] is None or isinstance(entry["description"], str)
        assert isinstance(entry["sort_order"], int)
        seen_codes.add(entry["code"])
        seen_sort_orders.add(entry["sort_order"])

    assert seen_codes != {"unclassified"}, (
        "default_asset_classes is the catalogue *alongside* the unclassified "
        "fallback installed by install_unclassified_asset_class; the unclassified "
        "row must not be inserted by this fixture"
    )
    assert "unclassified" not in seen_codes
    # The SAA limit-set sheet in v21 references these 12 codes.
    expected_v21 = {
        "equities",
        "equities_em",
        "gov_bonds_dm",
        "ig_credit",
        "hy_credit",
        "private_equity",
        "private_debt",
        "infra_equity",
        "infra_debt",
        "real_estate",
        "hedge_funds",
        "cash",
    }
    assert expected_v21.issubset(seen_codes), (
        f"Missing v21-referenced codes: {expected_v21 - seen_codes}"
    )
    assert len(seen_sort_orders) == len(entries)


# ---------------------------------------------------------------------------
# Negative: a fixture that doesn't exist must surface a clear error
# ---------------------------------------------------------------------------


def test_missing_fixture_surfaces_filenotfound() -> None:
    """Loading code in migrations/bootstrap relies on a hard error for missing files."""
    missing = _FIXTURE_DIR / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        missing.read_text()
