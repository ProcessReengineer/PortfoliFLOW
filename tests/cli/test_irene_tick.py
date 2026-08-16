# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``portfoliflow irene-tick`` CLI **wrapper** (ADR-0086).

Since ADR-0117 §2 the tick's orchestration lives in
``services/scheduler/tick_runner.py`` and is pinned by
``tests/services/scheduler/test_irene_tick_runner.py``. What this command
still owns — and what these tests cover — is the wrapper surface:

- registration on the root Typer app;
- the superuser engine's lifecycle: constructed per run, handed to the
  runner, disposed in a ``finally`` even when the tick raises;
- the credential gate's *ordering*: a deployment where no scope can
  resolve a credential exits 0 without ever constructing the engine, so a
  box with no ``DATABASE_URL_SUPERUSER`` still gets a clean no-op;
- the exit-code mapping (``ConfigurationError`` → 2, ``PortfoliFlowError``
  → 3; everything else → 0).

Invoked through Typer's :class:`CliRunner` (the pattern in
``tests/cli/test_status.py``).
"""

from __future__ import annotations

from typing import Any

from typer.testing import CliRunner

import cli.irene_tick as irene_tick
from cli import app
from core.exceptions import ConfigurationError, PortfoliFlowError
from services.scheduler.tick_runner import IreneTickSummary

runner = CliRunner()


class _FakeSettings:
    def __init__(self, api_key: str | None = "sk-test") -> None:
        self.openrouter_api_key = api_key
        self.openrouter_base_url = "https://openrouter.ai/api/v1"


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _install(
    monkeypatch: Any,
    *,
    api_key: str | None = "sk-test",
    vault_configured: bool = False,
    tick_raises: Exception | None = None,
    engine_raises: Exception | None = None,
) -> dict[str, Any]:
    """Patch the wrapper's three seams and record what reached the runner."""
    rec: dict[str, Any] = {"engine": _FakeEngine(), "calls": [], "engine_built": 0}

    monkeypatch.setattr(irene_tick, "get_web_settings", lambda: _FakeSettings(api_key))
    # The gate is the real one; steer it through ``is_vault_configured``.
    monkeypatch.setattr(
        "services.scheduler.tick_runner.is_vault_configured",
        lambda: vault_configured,
    )

    def _fake_engine() -> Any:
        rec["engine_built"] += 1
        if engine_raises is not None:
            raise engine_raises
        return rec["engine"]

    monkeypatch.setattr(irene_tick, "superuser_engine", _fake_engine)

    async def _fake_run(engine: Any, *, settings: Any, now: Any = None) -> IreneTickSummary:
        rec["calls"].append({"engine": engine, "settings": settings, "now": now})
        if tick_raises is not None:
            raise tick_raises
        return IreneTickSummary(due=1, beaten=1)

    monkeypatch.setattr(irene_tick, "run_irene_tick", _fake_run)

    return rec


def test_registered_in_help() -> None:
    """The command is registered on the root app."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "irene-tick" in result.output


def test_runs_the_shared_runner_on_a_superuser_engine_and_disposes_it(
    monkeypatch: Any,
) -> None:
    rec = _install(monkeypatch)
    result = runner.invoke(app, ["irene-tick"])

    assert result.exit_code == 0, result.output
    assert len(rec["calls"]) == 1
    call = rec["calls"][0]
    assert call["engine"] is rec["engine"]
    assert call["settings"].openrouter_api_key == "sk-test"
    assert rec["engine"].disposed is True


def test_engine_is_disposed_even_when_the_tick_raises(monkeypatch: Any) -> None:
    rec = _install(monkeypatch, tick_raises=PortfoliFlowError("beat infrastructure down"))
    result = runner.invoke(app, ["irene-tick"])

    assert result.exit_code == 3, result.output
    assert rec["engine"].disposed is True


def test_no_vault_and_no_env_key_exits_zero_without_building_an_engine(
    monkeypatch: Any,
) -> None:
    """The gate runs *before* the engine, so a keyless box never needs one.

    ``superuser_engine()`` raises ``ConfigurationError`` when
    ``DATABASE_URL_SUPERUSER`` is unset; building it ahead of the gate
    would turn today's tolerant no-op (exit 0) into exit 2.
    """
    rec = _install(
        monkeypatch,
        api_key=None,
        vault_configured=False,
        engine_raises=ConfigurationError("DATABASE_URL_SUPERUSER is not set."),
    )
    result = runner.invoke(app, ["irene-tick"])

    assert result.exit_code == 0, result.output
    assert rec["engine_built"] == 0
    assert rec["calls"] == []


def test_vault_configured_without_env_key_still_reaches_the_runner(
    monkeypatch: Any,
) -> None:
    """A vault can hold a tenant's own key, so the gate must not shortcut."""
    rec = _install(monkeypatch, api_key=None, vault_configured=True)
    result = runner.invoke(app, ["irene-tick"])

    assert result.exit_code == 0, result.output
    assert len(rec["calls"]) == 1


def test_configuration_error_exits_two(monkeypatch: Any) -> None:
    rec = _install(
        monkeypatch,
        engine_raises=ConfigurationError("DATABASE_URL_SUPERUSER is not set."),
    )
    result = runner.invoke(app, ["irene-tick"])

    assert result.exit_code == 2, result.output
    assert rec["calls"] == []


def test_portfoliflow_error_exits_three(monkeypatch: Any) -> None:
    _install(monkeypatch, tick_raises=PortfoliFlowError("vault decrypt failed"))
    result = runner.invoke(app, ["irene-tick"])

    assert result.exit_code == 3, result.output
