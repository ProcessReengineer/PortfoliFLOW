# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the shell catalogue's landing view (P-UX-A0b).

Since P-UX-A0b an area shows one section at a time and the URL
fragment selects it. With no fragment the *landing* section is shown:
the catalogue entry flagged ``landing``, or the first one. These tests
are DB-free — they import ``web.shell`` and nothing else, because the
catalogue is static data and its resolution rules should be provable
without a server.
"""

from __future__ import annotations

import pytest

from web.shell import all_areas, all_sections, landing_section_for, section_index_for


def test_transactions_lands_on_the_blotter() -> None:
    """Transactions is the one area that flags a landing section.

    Its first catalogue entry is ``new`` — the composer — but a trade
    ticket's home is the Blotter (ADR-0128 §7): opening the Area should
    show what is already in flight, not an empty form.
    """
    assert landing_section_for("transactions") == "blotter"


@pytest.mark.parametrize("area", [area.slug for area in all_areas()])
def test_unflagged_areas_land_on_their_first_section(area: str) -> None:
    """Every area but Transactions opens on its first catalogue entry."""
    sections = all_sections(area)
    assert sections, f"{area} has no sections"
    expected = "blotter" if area == "transactions" else sections[0].slug
    assert landing_section_for(area) == expected


@pytest.mark.parametrize("area", [area.slug for area in all_areas()])
def test_at_most_one_landing_flag_per_area(area: str) -> None:
    """Two flagged entries would make the landing view order-dependent."""
    flagged = [section.slug for section in all_sections(area) if section.landing]
    assert len(flagged) <= 1, f"{area} flags {flagged!r} as landing"


@pytest.mark.parametrize("area", [area.slug for area in all_areas()])
def test_section_index_marks_exactly_one_landing(area: str) -> None:
    """``section_index_for`` carries the resolved landing, as a string.

    The sidebar's second level marks that entry ``aria-current``, so the
    flag has to resolve on every area — including the eight that flag
    nothing and fall back to their first section.
    """
    index = section_index_for(area)
    assert index, f"{area} projected an empty section index"
    for entry in index:
        assert set(entry) == {"slug", "title", "landing"}
        assert entry["landing"] in {"true", "false"}
    marked = [entry["slug"] for entry in index if entry["landing"] == "true"]
    assert marked == [landing_section_for(area)]


def test_section_index_for_unknown_area_is_empty() -> None:
    """An unknown area projects nothing — the pre-A0b contract."""
    assert section_index_for("no-such-area") == []


def test_landing_section_for_unknown_area_raises() -> None:
    """An area the catalogue does not list cannot name a landing view.

    Failing here surfaces the drift; the alternative — defaulting — would
    render a page with every section hidden.
    """
    with pytest.raises(LookupError):
        landing_section_for("no-such-area")
