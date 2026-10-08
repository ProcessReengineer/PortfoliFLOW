# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Watch Desk services — the registry's write paths and its seeds.

The impure half of ADR-0116, sitting between the repositories in
``core/`` and the pure judgement layers in ``services/analytics/``:

- :mod:`services.watch_desk.calibration` — the sanctioned calibration
  write path. Composes a desired configuration over the code defaults,
  validates it as a whole (``FloorConfig`` constructor + the pinned
  invariants), reduces it to deviations, and persists. Also resolves a
  tenant's effective ``FloorConfig`` for the beat.
- :mod:`services.watch_desk.overlay` — the single per-tenant resolution
  (effective ``FloorConfig`` ⊕ WARN default and per-family WARN ⊕
  per-subject overlays ⊕ effective signal watchpoints ⊕ family scope,
  ADR-0133 §2.3) that the beat, the monitor and the briefing share.
- :mod:`services.watch_desk.presets` — the sensitivity presets (ADR-0133
  §3): the values, the displayed preset per family, and the atomic apply
  that writes one calibration revision and one scope revision.
- :mod:`services.watch_desk.family_status` — the monitor's family status
  lifted out of the route (D-WD-14): one observation step, projected as
  the route's template context and as typed records for the briefing
  writer. Reads only.
- :mod:`services.watch_desk.market_read` — what the priceable book did
  over the fixed window (D-WD-20): instrument, currency and asset-class
  moves, finished, for the briefing writer. Reads only.
- :mod:`services.watch_desk.signal_observation` — the single per-family
  fetch-and-produce path they share underneath it: one batched read per
  family, then the family's pure producer. Reads only.
- :mod:`services.watch_desk.seeding` — the idempotent default-watchpoint
  installer run at tenant provisioning (ADR-0116 §8).

The producers for the four signal families are pure and live under
``services/analytics/``; the **stateful** half that turns an observation
into a finding — watch-state, delta, mute gate — stays in
``services/irene/``, because only the beat may advance a subject's state.
"""
