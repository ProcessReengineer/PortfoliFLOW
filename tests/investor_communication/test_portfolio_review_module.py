# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for :class:`modules.investor_communication.portfolio_review.PortfolioReview`."""

from __future__ import annotations

from core.config import get_config
from modules.module_registry import registry


def _instantiate_module() -> object:
    """Return an instance of the PortfolioReview module via the registry."""
    cls = registry.get("portfolio_review")
    return cls(config=get_config())


def test_module_is_registered() -> None:
    """The module is discoverable through the registry."""
    cls = registry.get("portfolio_review")
    assert cls.module_name == "portfolio_review"
    assert cls.module_area == "investor_communication"


def test_run_with_empty_store_returns_ok_no_tiles(
    clean_store: None,  # noqa: ARG001
) -> None:
    """No data is a normal state — status is 'ok', tiles is empty."""
    instance = _instantiate_module()
    result = instance.run(action="generate")
    assert result["status"] == "ok"
    assert result["tiles"] == []


def test_run_with_unknown_action_returns_error(
    clean_store: None,  # noqa: ARG001
) -> None:
    """Unrecognised actions return a structured error response."""
    instance = _instantiate_module()
    result = instance.run(action="not_a_real_action")
    assert result["status"] == "error"
    assert "not_a_real_action" in result["error"]


def test_run_with_populated_store_returns_tiles(
    populated_store_canonical: None,  # noqa: ARG001
    investments: tuple[str, ...],
) -> None:
    """A populated DataStore yields portfolio + per-investment tiles."""
    instance = _instantiate_module()
    result = instance.run(action="generate")
    assert result["status"] == "ok"
    tiles = result["tiles"]
    assert len(tiles) == 1 + len(investments)
