# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Scheduler services — the one per-tick orchestration, host-agnostic.

ADR-0117 §2 makes the *tick source* and the *tick* two different things.
The source is dumb and swappable (a systemd timer invoking the CLI, or the
built-in in-process task in the web lifespan); the tick itself — due read,
advisory-lock claim, per-tenant credential resolution, beat/refresh,
schedule advance, failure isolation, structured logging — is one
implementation, shared:

- :mod:`services.scheduler.tick_runner` — two entry points, one per
  advisory-lock domain (``irene``, ``market_data``), each parametrised on
  the RLS-bypassing engine its caller supplies.

Layering: this package lives under ``services/`` and therefore imports only
from ``core/`` and other ``services/`` modules — never from ``cli/`` or
``web/``. The engine and (for the Irene tick) the settings object are
**passed in**, which is what keeps that direction clean while both hosts
still run byte-identical orchestration.
"""

from __future__ import annotations
