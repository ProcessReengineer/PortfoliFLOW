# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure-function tests for the atlas's shot geometry (``tools/ux_atlas.py``).

The reveal pass, the loader loop, the chart wait, the band writer, the Section
switch and the capture loop all need a live browser, so none of them is
exercised here. What *is* exercised is the arithmetic, the naming and the
pattern matching they hang on, which are browser-free by construction and are
where a mistake would be silent: a truncated page that never gets flagged, a
band grid that drops the last slice of every page, a debounce that returns on
its first quiet sample, a placeholder regex that misses the placeholders the
templates actually write, or — since P-UX-A0t — two Sections of two routes
writing their shots to the same name.

The placeholder pattern is tested here in Python and handed to the browser
verbatim as the argument to ``new RegExp``, so these cases are the ones that
run in the page — the two engines agree on ``^``, ``\b``, ``.``, ``\u2026``
and ``$``.

The PNG fixtures are written by the tests themselves with ``zlib`` and
``struct`` — the atlas takes no image-library dependency, and neither does its
test.
"""

from __future__ import annotations

import itertools
import json
import struct
import zlib
from pathlib import Path

import pytest

from tools.ux_atlas import (
    CHROMIUM_MAX_AXIS_PX,
    DEFAULT_SCENES,
    LOADING_PLACEHOLDER_RE,
    QUIET_HOLD_MS,
    Capture,
    ShotMeta,
    advance_quiet_window,
    band_rects,
    is_truncated,
    load_scenes,
    manifest_entry,
    png_height,
    section_heading,
    section_shot_stem,
    write_index,
)


def write_png(path: Path, width: int, height: int) -> Path:
    """Write a minimal but valid greyscale PNG of the given dimensions.

    Args:
        path: Where to write it.
        width: Image width in pixels.
        height: Image height in pixels.

    Returns:
        ``path``, for chaining.
    """

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


class TestPngHeight:
    """The IHDR height reader."""

    def test_reads_the_height_of_a_1x3_png(self, tmp_path: Path) -> None:
        assert png_height(write_png(tmp_path / "tiny.png", 1, 3)) == 3

    def test_reads_a_tall_height(self, tmp_path: Path) -> None:
        assert png_height(write_png(tmp_path / "tall.png", 4, 9000)) == 9000

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert png_height(tmp_path / "absent.png") is None

    def test_truncated_file_is_none(self, tmp_path: Path) -> None:
        short = tmp_path / "short.png"
        short.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert png_height(short) is None

    def test_non_png_is_none(self, tmp_path: Path) -> None:
        other = tmp_path / "not.png"
        other.write_bytes(b"GIF89a" + b"\x00" * 32)
        assert png_height(other) is None


class TestIsTruncated:
    """The predicate that decides a full-page shot came back cut."""

    def test_a_png_covering_the_document_is_whole(self) -> None:
        assert is_truncated(2900, 2900) is False

    def test_a_rounding_pixel_short_is_still_whole(self) -> None:
        # Chromium rounds a fractional CSS pixel; two pixels is the tolerance.
        assert is_truncated(2898, 2900) is False

    def test_three_pixels_short_is_a_cut(self) -> None:
        assert is_truncated(2897, 2900) is True

    def test_a_png_standing_exactly_at_the_cap_is_a_cut(self) -> None:
        # The giveaway case: the document is far taller and the image stops dead
        # on the cap.
        assert is_truncated(CHROMIUM_MAX_AXIS_PX, 41_000) is True

    def test_an_unreadable_png_is_not_reported_as_cut(self) -> None:
        # ``None`` means the height could not be read, which is a different
        # finding from a cut and must not be reported as one.
        assert is_truncated(None, 2900) is False

    def test_a_zero_height_document_is_not_reported_as_cut(self) -> None:
        assert is_truncated(0, 0) is False


class TestBandRects:
    """The band grid cut from one full-page render."""

    def test_the_worked_example(self) -> None:
        assert band_rects(2900, 1200) == [(0, 1200), (1200, 1200), (2400, 500)]

    def test_an_exact_multiple_leaves_no_short_band(self) -> None:
        assert band_rects(2400, 1200) == [(0, 1200), (1200, 1200)]

    def test_a_page_shorter_than_one_band_is_a_single_short_band(self) -> None:
        assert band_rects(900, 1200) == [(0, 900)]

    def test_the_bands_tile_the_page_without_gap_or_overlap(self) -> None:
        rects = band_rects(41_000, 1200)
        assert rects[0][0] == 0
        assert all(
            following[0] == previous[0] + previous[1]
            for previous, following in itertools.pairwise(rects)
        )
        assert sum(height for _, height in rects) == 41_000

    @pytest.mark.parametrize(
        ("scroll_px", "band_px"),
        [(0, 1200), (2900, 0), (2900, -1), (-1, 1200)],
    )
    def test_non_positive_arguments_yield_no_bands(self, scroll_px: int, band_px: int) -> None:
        assert band_rects(scroll_px, band_px) == []


class TestAdvanceQuietWindow:
    """The debounce that keeps a swap-to-swap gap from counting as quiet."""

    def test_the_first_quiet_poll_opens_the_window_but_does_not_satisfy_it(self) -> None:
        # The bug this whole helper exists to prevent: returning on the first
        # zero reading, which is exactly what a parent swap looks like in the
        # 20 ms before HTMX re-checks ``revealed`` and fires the nested loader.
        assert advance_quiet_window(True, None, 1_000.0) == (1_000.0, False)

    def test_quiet_held_for_the_full_window_is_satisfied(self) -> None:
        started, held = advance_quiet_window(True, 1_000.0, 1_000.0 + QUIET_HOLD_MS)
        assert (started, held) == (1_000.0, True)

    def test_quiet_held_one_millisecond_short_is_not(self) -> None:
        assert advance_quiet_window(True, 1_000.0, 1_399.0) == (1_000.0, False)

    def test_a_busy_poll_closes_the_window(self) -> None:
        assert advance_quiet_window(False, 1_000.0, 1_200.0) == (None, False)

    def test_a_busy_poll_restarts_the_hold_from_scratch(self) -> None:
        # 399 ms of quiet, one request, then 399 ms more is not 798 ms of
        # quiet — it is two windows that each fell short.
        state, _ = advance_quiet_window(True, None, 0.0)
        _, held = advance_quiet_window(True, state, 399.0)
        assert held is False
        state, held = advance_quiet_window(False, state, 400.0)
        assert (state, held) == (None, False)
        state, held = advance_quiet_window(True, state, 401.0)
        assert (state, held) == (401.0, False)
        _, held = advance_quiet_window(True, state, 800.0)
        assert held is False

    def test_a_shorter_hold_can_be_asked_for(self) -> None:
        assert advance_quiet_window(True, 10.0, 60.0, hold_ms=50)[1] is True

    def test_the_window_stays_open_across_several_quiet_polls(self) -> None:
        state: float | None = None
        for now in (0.0, 50.0, 100.0):
            state, held = advance_quiet_window(True, state, now)
            assert (state, held) == (0.0, False)


class TestLoadingPlaceholderPattern:
    """The generic "still loading" tripwire's pattern."""

    @pytest.mark.parametrize(
        "text",
        [
            "Loading charts…",
            "Loading overview…",
            "Loading portfolio review…",
            "Loading providers & credentials…",
            "Loading Global Infrastructure Fund II…",
            "Loading…",
        ],
    )
    def test_matches_the_placeholders_the_templates_write(self, text: str) -> None:
        # Every one of these is a real string from ``web/templates`` with
        # ``&hellip;`` resolved to the U+2026 the DOM hands back.
        assert LOADING_PLACEHOLDER_RE.match(text) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Loading charts",  # three dots' worth of nothing: already swapped
            "Loading charts...",  # ASCII dots are not what the templates emit
            "Loaded charts…",
            "Loadings are slow…",
            "Reloading charts…",
            "Still loading charts…",
        ],
    )
    def test_does_not_match_anything_else(self, text: str) -> None:
        assert LOADING_PLACEHOLDER_RE.match(text) is None

    def test_the_ellipsis_must_end_the_text(self) -> None:
        # The browser side trims and collapses whitespace before testing, so a
        # placeholder's own text never carries a trailing newline into this.
        assert LOADING_PLACEHOLDER_RE.match("Loading charts… done") is None


def shot(file: str, **overrides: object) -> ShotMeta:
    """Build a :class:`ShotMeta` standing in for one captured Section.

    Args:
        file: The shot's path relative to the run root.
        **overrides: Any field to set away from its benign default.

    Returns:
        The shot.
    """
    fields: dict[str, object] = {
        "file": file,
        "reveal_iterations": 3,
        "reveal_complete": True,
        "scroll_height": 2400,
        "png_height": 2400,
        "truncated": False,
    }
    fields.update(overrides)
    return ShotMeta(**fields)  # type: ignore[arg-type]


def sectioned_capture(*sections: tuple[str, ShotMeta]) -> Capture:
    """Build a route capture carrying the given Section shots.

    Args:
        *sections: ``(slug, shot)`` pairs in DOM order.

    Returns:
        The capture, shaped as ``capture_route`` leaves one.
    """
    return Capture(
        area="transactions",
        path="/transactions",
        file=sections[0][1].file,
        status=200,
        final_url="http://tenant.localhost:8000/transactions",
        partial=False,
        session="tenant",
        sections=list(sections),
    )


class TestSectionShotStem:
    """The ``<route>--<section>`` shot name."""

    def test_the_worked_example(self) -> None:
        assert section_shot_stem("/front-office", "charts") == "front-office--charts"

    def test_a_nested_route_keeps_the_route_slug_rules(self) -> None:
        # ``route_slug`` flattens every separator to ``__`` and strips the
        # leading pair; the Section half rides on the same rule.
        assert section_shot_stem("/admin/users/section", "users") == "admin__users__section--users"

    def test_the_root_route_is_named_index(self) -> None:
        # ``/`` redirects to ``/front-office``, so it photographs the same four
        # Sections — under its own stem, not over the ones already written.
        assert section_shot_stem("/", "overview") == "index--overview"
        assert section_shot_stem("/", "overview") != section_shot_stem("/front-office", "overview")

    def test_two_sections_of_one_route_never_collide(self) -> None:
        stems = {section_shot_stem("/transactions", slug) for slug in ("new", "blotter", "history")}
        assert len(stems) == 3

    def test_a_multi_word_section_slug_survives_whole(self) -> None:
        assert (
            section_shot_stem("/planning-desk", "cash-flow-planning")
            == "planning-desk--cash-flow-planning"
        )


class TestSectionHeading:
    """The contact sheet's per-Section heading."""

    def test_the_worked_example(self) -> None:
        assert (
            section_heading("transactions", "/transactions", "blotter")
            == "Transactions › Blotter — `/transactions#blotter`"
        )

    def test_an_underscored_area_reads_as_words(self) -> None:
        assert section_heading("front_office", "/front-office", "charts").startswith(
            "Front Office › Charts — "
        )

    def test_a_hyphenated_slug_reads_as_words(self) -> None:
        assert "Cash Flow Planning" in section_heading(
            "planning_desk", "/planning-desk", "cash-flow-planning"
        )

    def test_the_fragment_is_the_one_the_reader_navigates_by(self) -> None:
        assert section_heading("admin", "/admin", "users").endswith("`/admin#users`")


class TestManifestEntry:
    """The manifest's shape for a route the shell sections."""

    def test_a_route_with_two_sections_lists_both_in_dom_order(self) -> None:
        entry = manifest_entry(
            sectioned_capture(
                ("new", shot("transactions/transactions--new.png", bands=["a", "b"])),
                ("blotter", shot("transactions/transactions--blotter.png", truncated=True)),
            )
        )
        assert [item["section"] for item in entry["sections"]] == ["new", "blotter"]

    def test_each_section_carries_its_shot_bands_truncation_and_placeholders(self) -> None:
        entry = manifest_entry(
            sectioned_capture(
                ("new", shot("transactions/transactions--new.png", bands=["a", "b"])),
                (
                    "blotter",
                    shot(
                        "transactions/transactions--blotter.png",
                        truncated=True,
                        loading_placeholders=2,
                    ),
                ),
            )
        )
        assert entry["sections"] == [
            {
                "section": "new",
                "file": "transactions/transactions--new.png",
                "bands": 2,
                "truncated": False,
                "loading_placeholders": 0,
            },
            {
                "section": "blotter",
                "file": "transactions/transactions--blotter.png",
                "bands": 0,
                "truncated": True,
                "loading_placeholders": 2,
            },
        ]

    def test_the_band_count_replaces_the_band_list(self) -> None:
        # The route-level ``bands`` key keeps the paths; a Section's keeps the
        # count, because the sheet is what lists a Section's bands by name.
        entry = manifest_entry(sectioned_capture(("new", shot("t/a.png", bands=["x", "y", "z"]))))
        assert entry["sections"][0]["bands"] == 3

    def test_a_route_without_sections_carries_an_empty_list(self) -> None:
        entry = manifest_entry(
            Capture(
                area="investments",
                path="/investments",
                file="investments/investments.png",
                status=200,
                final_url="http://tenant.localhost:8000/investments",
                partial=False,
                session="tenant",
            )
        )
        assert entry["sections"] == []

    def test_the_route_level_fields_survive_alongside(self) -> None:
        entry = manifest_entry(sectioned_capture(("new", shot("t/a.png"))))
        assert entry["path"] == "/transactions"
        assert entry["status"] == 200


class TestWriteIndexSections:
    """The contact sheet's per-Section rendering."""

    def written(self, tmp_path: Path, capture: Capture) -> str:
        """Run ``write_index`` over one capture and read the sheet back.

        Args:
            tmp_path: The run root to write into.
            capture: The one capture the sheet describes.

        Returns:
            The rendered ``index.md``.
        """
        write_index(
            tmp_path,
            base_url="http://tenant.localhost:8000",
            viewport=(1440, 900),
            captured=[capture],
            scenes=[],
            skipped=[],
        )
        return (tmp_path / "index.md").read_text(encoding="utf-8")

    def two_sections(self) -> Capture:
        """Build a two-Section capture with bands under the second.

        Returns:
            The capture.
        """
        return sectioned_capture(
            ("new", shot("transactions/transactions--new.png")),
            (
                "blotter",
                shot(
                    "transactions/transactions--blotter.png",
                    bands=[
                        "transactions/bands/transactions--blotter-01.png",
                        "transactions/bands/transactions--blotter-02.png",
                    ],
                ),
            ),
        )

    def test_both_sections_get_their_own_heading(self, tmp_path: Path) -> None:
        sheet = self.written(tmp_path, self.two_sections())
        assert "#### Transactions › New — `/transactions#new`" in sheet
        assert "#### Transactions › Blotter — `/transactions#blotter`" in sheet

    def test_each_section_shows_its_own_shot(self, tmp_path: Path) -> None:
        sheet = self.written(tmp_path, self.two_sections())
        assert "![/transactions#new](transactions/transactions--new.png)" in sheet
        assert "![/transactions#blotter](transactions/transactions--blotter.png)" in sheet

    def test_a_sections_bands_are_listed_under_its_heading(self, tmp_path: Path) -> None:
        sheet = self.written(tmp_path, self.two_sections())
        blotter = sheet.index("#### Transactions › Blotter")
        assert "transactions/bands/transactions--blotter-01.png" in sheet[blotter:]
        assert "transactions/bands/transactions--blotter-02.png" in sheet[blotter:]
        # …and under that heading only: the unbanded Section precedes it.
        assert "bands/transactions--blotter-01.png" not in sheet[:blotter]

    def test_the_run_header_counts_the_sections(self, tmp_path: Path) -> None:
        sheet = self.written(tmp_path, self.two_sections())
        assert "1 route(s), 2 section(s), 0 scene(s)" in sheet

    def test_a_route_without_sections_still_shows_one_shot(self, tmp_path: Path) -> None:
        sheet = self.written(
            tmp_path,
            Capture(
                area="investments",
                path="/investments",
                file="investments/investments.png",
                status=200,
                final_url="http://tenant.localhost:8000/investments",
                partial=False,
                session="tenant",
            ),
        )
        assert "![/investments](investments/investments.png)" in sheet
        assert "####" not in sheet
        assert "1 route(s), 0 section(s)" in sheet


class TestScenesCarryASection:
    """The scenes file's optional ``section`` key."""

    def scenes_file(self, tmp_path: Path) -> Path:
        """Write a scenes file holding one scene with a ``section`` and one without.

        Args:
            tmp_path: Where to write it.

        Returns:
            The file's path.
        """
        path = tmp_path / "atlas-scenes.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "name": "transactions-blotter",
                        "area": "transactions",
                        "start": "/transactions",
                        "session": "tenant",
                        "section": "blotter",
                        "steps": [],
                        "shot": "blotter",
                    },
                    {
                        "name": "investments-list",
                        "area": "investments",
                        "start": "/investments",
                        "session": "tenant",
                        "steps": [{"wait": "#investments-table"}],
                        "shot": "list",
                    },
                ]
            ),
            encoding="utf-8",
        )
        return path

    def test_a_scene_naming_a_section_loads_with_it(self, tmp_path: Path) -> None:
        scenes = load_scenes(self.scenes_file(tmp_path), None)
        assert scenes[0]["section"] == "blotter"

    def test_a_scene_without_one_still_loads(self, tmp_path: Path) -> None:
        scenes = load_scenes(self.scenes_file(tmp_path), None)
        assert "section" not in scenes[1]
        assert scenes[1]["steps"] == [{"wait": "#investments-table"}]

    def test_the_area_filter_is_unaffected_by_the_new_key(self, tmp_path: Path) -> None:
        scenes = load_scenes(self.scenes_file(tmp_path), ["transactions"])
        assert [scene["name"] for scene in scenes] == ["transactions-blotter"]


class TestShippedScenesFile:
    """The scenes file the repository ships."""

    def test_every_transactions_scene_names_its_section(self) -> None:
        """Every shipped Transactions scene names one of the Area's Sections.

        The three Transactions Sections are the worked example of the
        ``section`` key: it is what points a scene at a Section of the
        long-scroll page, and a scene that lost it would silently photograph
        the landing Section instead. That contract is what this test pins.

        The expected names are read from ``docs/ux/atlas-scenes.json`` rather
        than restated here. Restating them made the test a census as well as
        a contract, and the census went stale the first time scenes were
        added (P-UX-A0s shipped four composer scenes); a scene added by a
        later strand no longer breaks it, while a scene that drops its
        ``section`` or names a slug that is not a Section still does.
        """
        scenes = load_scenes(DEFAULT_SCENES, ["transactions"])
        sections = {scene["name"]: scene.get("section") for scene in scenes}

        assert sections, "the shipped scenes file carries Transactions scenes"
        unsectioned = sorted(name for name, section in sections.items() if not section)
        assert not unsectioned, f"Transactions scenes naming no Section: {unsectioned}"
        assert set(sections.values()) == {"new", "blotter", "history"}, (
            "the three Transactions Sections are covered, and none names a fourth"
        )
        assert sections["transactions-blotter"] == "blotter"
        assert sections["transactions-history"] == "history"
