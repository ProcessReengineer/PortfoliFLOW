# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pinning test for the market-data cadence choice tuple (ADR-0125 §2).

Constant-level — no live Postgres, no ASGI client — which is why it lives
in its own module rather than in ``tests/web/test_market_data_routes.py``,
where every test drives the surface through that module's engine and login
fixtures. The sibling of ``tests/web/test_watch_desk_cadence_choices.py``,
deliberately shaped the same way.

What it pins is a *decision*, not an implementation detail. ADR-0125 §2
takes the market-data surface to the two sub-hourly members the Watch Desk
withholds — two tuples, separate by decision, neither derived from the
shared map in :mod:`services.irene.scheduling`. Nothing in the code stops a
later change from quietly wiring one to the other, or from deriving either
from ``_SUPPORTED_CADENCES`` and thereby offering ``every_5m`` the moment
someone adds it to the vocabulary. That is what this test is for.
"""

from __future__ import annotations

from services.irene.scheduling import _SUPPORTED_CADENCES
from web.routes.market_data import CADENCE_LABELS, _CADENCE_CHOICES


def test_market_data_offers_the_sub_hourly_intervals() -> None:
    """The panel offers exactly the four ADR-0125 §2 members.

    Order is asserted alongside membership because the tuple is rendered
    in sequence and reads finest-first, the way an interval picker does.
    """
    assert _CADENCE_CHOICES == ("every_15m", "every_30m", "hourly", "daily")


def test_every_offered_cadence_is_in_the_shared_vocabulary() -> None:
    """Each offered choice survives the one validator that gates a save.

    The tuple is an offer; ``compute_next_due_at`` is the check. A member
    absent from the vocabulary would render fine and then be rejected on
    submit — the failure this asserts away.
    """
    assert set(_CADENCE_CHOICES) <= _SUPPORTED_CADENCES


def test_every_5m_is_not_offered() -> None:
    """``every_5m`` is withheld by decision, not by absence (ADR-0125 §2).

    Pinned separately so a future vocabulary entry cannot reach this panel
    unnoticed — which is exactly what deriving the tuple from the shared
    map would do.
    """
    assert "every_5m" not in _CADENCE_CHOICES


def test_every_offered_cadence_has_a_label() -> None:
    """No rendered choice falls back to ``|capitalize`` ("Every_15m").

    The template indexes the label map for every member of the tuple, so a
    missing entry is a rendering defect, not a cosmetic one (ADR-0119 §3).
    """
    assert set(_CADENCE_CHOICES) <= set(CADENCE_LABELS)
