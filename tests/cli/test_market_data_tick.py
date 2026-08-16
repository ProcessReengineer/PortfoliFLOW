# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``portfoliflow market-data-tick`` CLI **wrapper** (ADR-0093).

Since ADR-0117 §2 the tick's orchestration lives in
``services/scheduler/tick_runner.py`` and is pinned by
``tests/services/scheduler/test_market_data_tick_runner.py``. What this
command still owns — and what these tests cover — is the wrapper surface:
registration, the superuser engine's lifecycle (constructed per run,
disposed in a ``finally``), the exit-code mapping, and the parsing and
plumbing of the ``--tenant`` / ``--provider`` test-seam flags
(ADR-0093 §0.4) into the runner's parameters.
"""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

import cli.market_data_tick as mdt
from cli import app
from core.exceptions import ConfigurationError, PortfoliFlowError
from services.scheduler.tick_runner import MarketDataTickSummary

runner = CliRunner()


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _install(
    monkeypatch: Any,
    *,
    tick_raises: Exception | None = None,
    engine_raises: Exception | None = None,
) -> dict[str, Any]:
    """Patch the wrapper's two seams and record what reached the runner."""
    rec: dict[str, Any] = {"engine": _FakeEngine(), "calls": [], "engine_built": 0}

    def _fake_engine() -> Any:
        rec["engine_built"] += 1
        if engine_raises is not None:
            raise engine_raises
        return rec["engine"]

    monkeypatch.setattr(mdt, "superuser_engine", _fake_engine)

    async def _fake_run(
        engine: Any,
        *,
        tenant_ref: str | None = None,
        provider: str | None = None,
        now: Any = None,
    ) -> MarketDataTickSummary:
        rec["calls"].append(
            {"engine": engine, "tenant_ref": tenant_ref, "provider": provider, "now": now}
        )
        if tick_raises is not None:
            raise tick_raises
        return MarketDataTickSummary(due=1, refreshed=1)

    monkeypatch.setattr(mdt, "run_market_data_tick", _fake_run)

    return rec


def test_registered_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "market-data-tick" in result.output


def test_runs_the_shared_runner_on_a_superuser_engine_and_disposes_it(
    monkeypatch: Any,
) -> None:
    rec = _install(monkeypatch)
    result = runner.invoke(app, ["market-data-tick"])

    assert result.exit_code == 0, result.output
    assert rec["calls"] == [
        {"engine": rec["engine"], "tenant_ref": None, "provider": None, "now": None}
    ]
    assert rec["engine"].disposed is True


def test_engine_is_disposed_even_when_the_tick_raises(monkeypatch: Any) -> None:
    rec = _install(monkeypatch, tick_raises=PortfoliFlowError("ingest infrastructure down"))
    result = runner.invoke(app, ["market-data-tick"])

    assert result.exit_code == 3, result.output
    assert rec["engine"].disposed is True


def test_tenant_flag_is_plumbed_into_the_runner(monkeypatch: Any) -> None:
    rec = _install(monkeypatch)
    result = runner.invoke(app, ["market-data-tick", "--tenant", "minathena-capital"])

    assert result.exit_code == 0, result.output
    assert rec["calls"][0]["tenant_ref"] == "minathena-capital"
    assert rec["calls"][0]["provider"] is None


def test_provider_flag_is_plumbed_into_the_runner(monkeypatch: Any) -> None:
    rec = _install(monkeypatch)
    result = runner.invoke(app, ["market-data-tick", "--provider", "synthetic"])

    assert result.exit_code == 0, result.output
    assert rec["calls"][0]["provider"] == "synthetic"
    assert rec["calls"][0]["tenant_ref"] is None


def test_both_flags_are_plumbed_together(monkeypatch: Any) -> None:
    rec = _install(monkeypatch)
    result = runner.invoke(
        app,
        ["market-data-tick", "--tenant", "minathena-capital", "--provider", "synthetic"],
    )

    assert result.exit_code == 0, result.output
    assert rec["calls"][0]["tenant_ref"] == "minathena-capital"
    assert rec["calls"][0]["provider"] == "synthetic"


def test_configuration_error_exits_two(monkeypatch: Any) -> None:
    rec = _install(
        monkeypatch,
        engine_raises=ConfigurationError("DATABASE_URL_SUPERUSER is not set."),
    )
    result = runner.invoke(app, ["market-data-tick"])

    assert result.exit_code == 2, result.output
    assert rec["calls"] == []


def test_portfoliflow_error_exits_three(monkeypatch: Any) -> None:
    _install(monkeypatch, tick_raises=PortfoliFlowError("provider registry broken"))
    result = runner.invoke(app, ["market-data-tick"])

    assert result.exit_code == 3, result.output
