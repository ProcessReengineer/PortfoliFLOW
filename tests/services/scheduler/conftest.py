# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Fixtures for the shared tick-runner tests.

``tests/services/conftest.py`` re-exports the live-Postgres fixtures, and
``reset_schema`` among them is **autouse** — every test under
``tests/services/`` therefore pays a schema truncation and needs a running
compose Postgres. The tick-runner tests are fully mocked by construction
(the ADR-0117 §2 runner takes its engine as an argument, so a fake engine
is the whole database), exactly as they were while they lived in
``tests/cli/``; the no-op override below keeps that property when they
moved here.

If a DB-backed scheduler test ever lands in this package, delete this file
rather than working around it — the inherited fixture is the correct
behaviour for anything that touches Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def reset_schema() -> Iterator[None]:
    """Shadow the inherited autouse DB truncation — these tests are offline."""
    yield
