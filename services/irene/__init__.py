# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Irene — the Watch Desk heartbeat (cadence, tick, synthesis).

This package holds the *execution and concurrency shape* of Irene
(ADR-0086), sitting on top of the persistence layer that ADR-0085 /
migration ``b019`` established:

- :mod:`services.irene.scheduling` — the cross-tenant due-evaluation
  read (run on a superuser connection, RLS bypassed by design), the
  pure ``compute_next_due_at`` cadence function, and the advisory-lock
  key derivation used to claim a tenant's beat.
- :mod:`services.irene.beat` — the tenant-scoped beat handler that
  drives one synthesis and persists any surfaced findings.
- :mod:`services.irene.synthesis_tool` — the OpenAI function-tool dict
  for ``surface_finding`` (the full ADR-0088 contract). The deterministic
  urgency floor and band derivation live in
  :mod:`services.analytics.irene_floor` and are applied by the beat.

Layering (per CLAUDE.md): this package lives under ``services/`` and
imports only from ``core/`` and other ``services/`` modules. It is
Qt-free and holds no business logic beyond scheduling and orchestration.
"""

from __future__ import annotations
