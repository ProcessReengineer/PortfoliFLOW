# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pinning test for the Watch Desk cadence choice tuple (ADR-0125 §2).

Constant-level — no live Postgres, no ASGI client — which is why it lives
in its own module rather than in ``tests/web/test_watch_desk.py``, where
every test drives the surface through that module's engine and login
fixtures.

What it pins is a *decision*, not an implementation detail. ADR-0125 §1
grows the shared cadence vocabulary in ``services.irene.scheduling`` to
seven members, two of them sub-hourly. The Watch Desk deliberately does
not follow: an Irene beat every 15 minutes is an LLM-cost decision the
Watch Desk has not taken. Its ``_CADENCE_CHOICES`` therefore stays at the
five ADR-0119 §1 members while the market-data surface takes its own
(ADR-0125 §2) — two tuples, separate by decision, neither derived from
the shared map. Nothing in the code stops a later change from quietly
wiring one to the other, which is exactly what this test is for.
"""

from __future__ import annotations

from services.irene.scheduling import _SUPPORTED_CADENCES
from web.routes.watch_desk import _CADENCE_CHOICES


def test_watch_desk_cadence_choices_survive_vocabulary_v2_unchanged() -> None:
    """The cadence panel still offers exactly the five v1 members.

    Order is asserted alongside membership because the tuple is rendered
    in sequence and reads coarsest-first (ADR-0119 §3).
    """
    assert _CADENCE_CHOICES == ("daily", "every_6h", "every_3h", "every_2h", "hourly")


def test_vocabulary_grew_where_the_watch_desk_did_not() -> None:
    """The sub-hourly members exist — and are withheld from the Watch Desk.

    Both halves are asserted together on purpose. A test that only
    checked the tuple would still pass had ADR-0125 §1 never landed; one
    that only checked the vocabulary would not notice the Watch Desk
    silently inheriting it.
    """
    assert {"every_30m", "every_15m"} <= _SUPPORTED_CADENCES
    assert "every_30m" not in _CADENCE_CHOICES
    assert "every_15m" not in _CADENCE_CHOICES
