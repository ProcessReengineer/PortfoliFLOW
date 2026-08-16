# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""End-to-end test for the SAA seed-installation step in ``bootstrap``.

Unlike ``test_bootstrap.py``, this test runs against the live compose
Postgres because the seed step uses :func:`tenant_context` which
needs a real :class:`AsyncEngine`. The bootstrap CLI internally
calls :func:`asyncio.run`, so the test functions are *synchronous* —
mixing them with pytest-asyncio's running loop would raise
"asyncio.run() cannot be called from a running event loop". The
synchronous helpers below open a short-lived loop for the
verification queries.

Coverage:

* First ``bootstrap`` run installs the three seed configurations
  with the expected names, asset-class counts, and active flag.
* Asset classes shared across seeds (e.g. ``equities_dm`` appears in
  both Conservative and Balanced) are created once.
* A second ``bootstrap`` run is a no-op for the seeds (idempotent).
* First ``bootstrap`` installs the Phase-4 ``"unclassified"``
  fallback asset class for the sentinel tenant.
* A second ``bootstrap`` does not duplicate the ``"unclassified"``
  asset class.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from cli import app
from core.tenant_constants import SENTINEL_TENANT_ID

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

runner = CliRunner()

_TRUNCATE_SQL = (
    "TRUNCATE TABLE investment_region_weights, "
    "region_country_memberships, regions, "
    "investment_country_weights, "
    "investment_sector_weights, sectors, "
    "investment_cashflows, investment_navs, investments, "
    "saa_correlations, saa_asset_class_inputs, "
    "saa_configurations, asset_classes, "
    "data_upload_sheets, data_uploads, "
    "login_audit, sessions, audit_log, "
    "data_store_entries, users, tenants "
    "RESTART IDENTITY CASCADE"
)


def _require_db() -> None:
    if not DATABASE_URL_SUPERUSER:
        pytest.skip(
            "DATABASE_URL_SUPERUSER not set; cannot run bootstrap-seed tests.",
            allow_module_level=False,
        )


async def _truncate_async() -> None:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(_TRUNCATE_SQL))
    finally:
        await engine.dispose()


async def _query_seeds_async() -> dict[str, dict[str, object]]:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            result = await conn.execute(
                text(
                    """
                    SELECT c.name,
                           c.is_active,
                           (SELECT COUNT(*) FROM saa_asset_class_inputs i
                            WHERE i.configuration_id = c.id) AS n_inputs,
                           (SELECT COUNT(*) FROM saa_correlations corr
                            WHERE corr.configuration_id = c.id) AS n_corrs
                    FROM saa_configurations c
                    WHERE c.tenant_id = :tid
                    ORDER BY c.name
                    """
                ),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            rows = result.mappings().all()
    finally:
        await engine.dispose()
    return {
        row["name"]: {
            "is_active": bool(row["is_active"]),
            "n_inputs": int(row["n_inputs"]),
            "n_corrs": int(row["n_corrs"]),
        }
        for row in rows
    }


async def _count_asset_classes_async(codes: tuple[str, ...]) -> dict[str, int]:
    engine = create_async_engine(DATABASE_URL_SUPERUSER, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            result = await conn.execute(
                text(
                    "SELECT code, COUNT(*) FROM asset_classes "
                    "WHERE tenant_id = :tid AND code = ANY(:codes) "
                    "GROUP BY code"
                ),
                {"tid": str(SENTINEL_TENANT_ID), "codes": list(codes)},
            )
            return {row[0]: int(row[1]) for row in result.fetchall()}
    finally:
        await engine.dispose()


def _truncate() -> None:
    asyncio.run(_truncate_async())


def _query_seeds() -> dict[str, dict[str, object]]:
    return asyncio.run(_query_seeds_async())


def _count_asset_classes(*codes: str) -> dict[str, int]:
    return asyncio.run(_count_asset_classes_async(codes))


@pytest.fixture
def clean_db() -> Iterator[None]:
    _require_db()
    _truncate()
    try:
        yield
    finally:
        _truncate()


# ---------------------------------------------------------------------------
# BS-01: first bootstrap installs the three seeds
# ---------------------------------------------------------------------------


def test_bs01_first_bootstrap_installs_seeds(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    seeds = _query_seeds()
    assert set(seeds.keys()) == {
        "Conservative Multi-Strategy",
        "Growth Private Markets",
        "Balanced Institutional",
    }
    assert seeds["Conservative Multi-Strategy"]["is_active"] is True
    assert seeds["Growth Private Markets"]["is_active"] is False
    assert seeds["Balanced Institutional"]["is_active"] is False

    # Asset-class counts and the corresponding upper-triangle
    # correlation counts (n*(n-1)/2).
    assert seeds["Conservative Multi-Strategy"]["n_inputs"] == 7
    assert seeds["Conservative Multi-Strategy"]["n_corrs"] == 21
    assert seeds["Growth Private Markets"]["n_inputs"] == 8
    assert seeds["Growth Private Markets"]["n_corrs"] == 28
    assert seeds["Balanced Institutional"]["n_inputs"] == 6
    assert seeds["Balanced Institutional"]["n_corrs"] == 15


# ---------------------------------------------------------------------------
# BS-02: shared asset classes are deduplicated across seeds
# ---------------------------------------------------------------------------


def test_bs02_shared_asset_classes_dedup(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    counts = _count_asset_classes("equities_dm", "ig_credit", "equities_em")
    # equities_dm appears in Conservative + Balanced — once total.
    assert counts["equities_dm"] == 1
    # ig_credit appears in Conservative + Balanced — once total.
    assert counts["ig_credit"] == 1
    # equities_em appears in Growth + Balanced — once total.
    assert counts["equities_em"] == 1


# ---------------------------------------------------------------------------
# BS-03: second bootstrap is a no-op for seeds
# ---------------------------------------------------------------------------


def test_bs03_second_bootstrap_idempotent(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    first = runner.invoke(app, ["bootstrap"])
    assert first.exit_code == 0, first.output
    snapshot_first = _query_seeds()

    second = runner.invoke(app, ["bootstrap"])
    assert second.exit_code == 0, second.output
    snapshot_second = _query_seeds()

    assert snapshot_first == snapshot_second


# ---------------------------------------------------------------------------
# BS-04: first bootstrap installs the Phase-4 "unclassified" asset class
# ---------------------------------------------------------------------------


def test_bs04_first_bootstrap_installs_unclassified_asset_class(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0043 §1: every tenant carries an ``"unclassified"`` fallback class."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    result = runner.invoke(app, ["bootstrap"])
    assert result.exit_code == 0, result.output

    counts = _count_asset_classes("unclassified")
    assert counts == {"unclassified": 1}, (
        "Expected exactly one 'unclassified' asset class for the sentinel "
        f"tenant after bootstrap; got {counts}."
    )


# ---------------------------------------------------------------------------
# BS-05: second bootstrap is a no-op for the "unclassified" asset class
# ---------------------------------------------------------------------------


def test_bs05_second_bootstrap_does_not_duplicate_unclassified(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``"unclassified"`` installation is idempotent on the asset-class code."""
    monkeypatch.setenv("SENTINEL_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINEL_PASSWORD", "test-only-password")

    first = runner.invoke(app, ["bootstrap"])
    assert first.exit_code == 0, first.output
    counts_first = _count_asset_classes("unclassified")

    second = runner.invoke(app, ["bootstrap"])
    assert second.exit_code == 0, second.output
    counts_second = _count_asset_classes("unclassified")

    assert counts_first == counts_second == {"unclassified": 1}
