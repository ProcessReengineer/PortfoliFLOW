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

Since P-UX-1c the scene contract is exercised too, against a fake page that
records what it is asked to do: the ``ensure`` verb, the one-line failure
reason a failed step leaves, the terminal tail, and the ``--scene`` selection
that has to fail before any browser starts.

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
from typing import Any

import pytest

import tools.ux_atlas as ux_atlas
from tools.ux_atlas import (
    CHROMIUM_MAX_AXIS_PX,
    DEFAULT_SCENES,
    FAILURE_MESSAGE_MAX_CHARS,
    LOADING_PLACEHOLDER_RE,
    QUIET_HOLD_MS,
    Capture,
    EnsureStepError,
    SceneResult,
    SceneSelectionError,
    ShotMeta,
    advance_quiet_window,
    apply_step,
    band_rects,
    is_truncated,
    load_scenes,
    main,
    manifest_entry,
    png_height,
    section_heading,
    section_shot_stem,
    select_scenes,
    step_failure_reason,
    summary_lines,
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

    def test_every_cases_scene_seeds_through_ensure(self) -> None:
        """Every Cases scene that needs the seed case creates it through one ``ensure``.

        Before P-UX-1c each of the five scenes created its own "Atlas seed
        case", so every run added five rows to the tenant. The contract is
        structural rather than a count of the title's mentions: the scene has
        exactly one ``ensure``, and the only ``fill`` of the title sits inside
        it — a creation step outside it would run on every run again.
        """
        seeded = [
            scene
            for scene in load_scenes(DEFAULT_SCENES, ["cases"])
            if "Atlas seed case" in json.dumps(scene["steps"])
        ]
        assert len(seeded) >= 5, "the five Cases scenes reach a case detail"

        for scene in seeded:
            ensures = [step for step in scene["steps"] if "ensure" in step]
            assert len(ensures) == 1, f"{scene['name']}: {len(ensures)} ensure steps"
            inner = ensures[0]["ensure"]["steps"]
            assert "Atlas seed case" in ensures[0]["ensure"]["selector"]
            assert any(step.get("fill", {}).get("value") == "Atlas seed case" for step in inner), (
                f"{scene['name']}: the seed title is not filled inside the ensure"
            )
            outer_fills = [
                step
                for step in scene["steps"]
                if step.get("fill", {}).get("value") == "Atlas seed case"
            ]
            assert not outer_fills, f"{scene['name']}: the seed title is filled outside it"

    def test_no_shipped_ensure_nests(self) -> None:
        """``ensure`` does not nest; a shipped scene that tried would fail every run."""
        for scene in load_scenes(DEFAULT_SCENES, None):
            for step in scene["steps"]:
                if "ensure" in step:
                    assert not any("ensure" in inner for inner in step["ensure"]["steps"]), scene[
                        "name"
                    ]


class FakePage:
    """A stand-in for a Playwright page that records the scene steps applied to it.

    Args:
        present: Selectors ``query_selector`` reports as present.
        failing: Selectors whose ``click`` / ``fill`` / ``wait`` raises
            ``TimeoutError`` with Playwright's two-part message shape.
    """

    def __init__(self, present: set[str] | None = None, failing: set[str] | None = None) -> None:
        self.present = present or set()
        self.failing = failing or set()
        self.calls: list[tuple[str, str]] = []

    def _act(self, verb: str, selector: str) -> None:
        self.calls.append((verb, selector))
        if selector in self.failing:
            raise TimeoutError(
                f"Timeout 5000ms exceeded.\nCall log:\n  - waiting for locator('{selector}')"
            )

    def query_selector(self, selector: str) -> object | None:
        """Report a selector as present or absent."""
        return object() if selector in self.present else None

    def click(self, selector: str, timeout: int = 0) -> None:
        """Record a click."""
        self._act("click", selector)

    def fill(self, selector: str, value: str, timeout: int = 0) -> None:
        """Record a fill — the value is deliberately not recorded."""
        self._act("fill", selector)

    def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        """Record a wait."""
        self._act("wait", selector)

    def wait_for_timeout(self, ms: int) -> None:
        """Record a pause."""
        self.calls.append(("wait_ms", str(ms)))


SEED_ROW = '#cases-open-list .pf-case-row:has-text("Atlas seed case")'


def seed_ensure() -> dict[str, Any]:
    """Build the ``ensure`` step the Cases scenes ship with.

    Returns:
        The step.
    """
    return {
        "ensure": {
            "selector": SEED_ROW,
            "steps": [
                {"click": ".pf-cases__new-btn"},
                {"wait": ".pf-cases__new-form"},
                {"fill": {"selector": "input[name=title]", "value": "Atlas seed case"}},
                {"click": "button[type=submit]"},
            ],
        }
    }


@pytest.fixture
def settles(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace ``settle`` with a recorder; the real one polls a live page's clock.

    Returns:
        The pages ``settle`` was called with, in order.
    """
    seen: list[object] = []
    monkeypatch.setattr(ux_atlas, "settle", seen.append)
    return seen


class TestApplyStepEnsure:
    """The ``ensure`` verb: seed once, reuse thereafter."""

    def test_a_present_selector_skips_the_nested_steps(self, settles: list[object]) -> None:
        page = FakePage(present={SEED_ROW})
        apply_step(page, seed_ensure())  # type: ignore[arg-type]
        assert page.calls == []
        assert settles == []

    def test_an_absent_selector_runs_the_nested_steps_in_order(self, settles: list[object]) -> None:
        page = FakePage()
        apply_step(page, seed_ensure())  # type: ignore[arg-type]
        assert page.calls == [
            ("click", ".pf-cases__new-btn"),
            ("wait", ".pf-cases__new-form"),
            ("fill", "input[name=title]"),
            ("click", "button[type=submit]"),
        ]
        assert settles == [page], "the seed is settled once, after its last step"

    def test_ensure_does_not_nest(self, settles: list[object]) -> None:
        step = {
            "ensure": {"selector": "#a", "steps": [{"ensure": {"selector": "#b", "steps": []}}]}
        }
        with pytest.raises(ValueError, match="ensure does not nest"):
            apply_step(FakePage(), step)  # type: ignore[arg-type]

    def test_nesting_is_refused_even_when_the_seed_exists(self, settles: list[object]) -> None:
        # A malformed scene must fail the same way on a seeded and an unseeded
        # tenant, not only on the first run.
        step = {
            "ensure": {"selector": "#a", "steps": [{"ensure": {"selector": "#b", "steps": []}}]}
        }
        with pytest.raises(ValueError, match="ensure does not nest"):
            apply_step(FakePage(present={"#a"}), step)  # type: ignore[arg-type]

    def test_a_nested_failure_carries_its_inner_position(self, settles: list[object]) -> None:
        page = FakePage(failing={".pf-cases__new-form"})
        with pytest.raises(EnsureStepError) as caught:
            apply_step(page, seed_ensure())  # type: ignore[arg-type]
        assert caught.value.index == 1
        assert caught.value.step == {"wait": ".pf-cases__new-form"}
        assert page.calls[-1] == ("wait", ".pf-cases__new-form")

    def test_a_nested_failures_reason_names_ensure_and_the_inner_step(
        self, settles: list[object]
    ) -> None:
        step = seed_ensure()
        page = FakePage(failing={".pf-cases__new-form"})
        with pytest.raises(EnsureStepError) as caught:
            apply_step(page, step)  # type: ignore[arg-type]
        assert step_failure_reason(1, step, caught.value) == (
            "TimeoutError at step 1 ensure[1] wait '.pf-cases__new-form': Timeout 5000ms exceeded."
        )


class TestStepFailureReason:
    """The one-line reason a failed scene step leaves behind."""

    TIMEOUT = TimeoutError("Timeout 5000ms exceeded.\nCall log:\n  - waiting for locator")

    def test_a_click(self) -> None:
        selector = ".pf-table__actions .pf-btn--quiet:not(.pf-btn--icon)"
        assert step_failure_reason(2, {"click": selector}, self.TIMEOUT) == (
            f"TimeoutError at step 2 click '{selector}': Timeout 5000ms exceeded."
        )

    def test_a_wait(self) -> None:
        assert step_failure_reason(0, {"wait": "#dc-monitor"}, self.TIMEOUT).startswith(
            "TimeoutError at step 0 wait '#dc-monitor': "
        )

    def test_a_fill_names_its_selector_and_never_its_value(self) -> None:
        step = {"fill": {"selector": "#password", "value": "hunter2-secret"}}
        reason = step_failure_reason(3, step, self.TIMEOUT)
        assert reason.startswith("TimeoutError at step 3 fill '#password': ")
        assert "hunter2-secret" not in reason

    def test_a_wait_ms_names_its_duration(self) -> None:
        reason = step_failure_reason(4, {"wait_ms": 400}, RuntimeError("Target closed"))
        assert reason == "RuntimeError at step 4 wait_ms 400: Target closed"

    def test_an_ensure_names_its_selector(self) -> None:
        reason = step_failure_reason(
            1, {"ensure": {"selector": "#a", "steps": []}}, ValueError("ensure does not nest")
        )
        assert reason == "ValueError at step 1 ensure '#a': ensure does not nest"

    def test_an_unrecognised_verb_keeps_the_value_error_text(self) -> None:
        step = {"clik": "#a"}
        with pytest.raises(ValueError) as caught:
            apply_step(FakePage(), step)  # type: ignore[arg-type]
        assert step_failure_reason(5, step, caught.value) == (
            "ValueError at step 5: unrecognised scene step: ['clik']"
        )

    def test_the_browser_message_is_cut_to_its_first_line(self) -> None:
        reason = step_failure_reason(0, {"wait": "#a"}, self.TIMEOUT)
        assert "\n" not in reason
        assert "Call log" not in reason

    def test_a_long_message_is_cut_at_the_limit(self) -> None:
        reason = step_failure_reason(0, {"wait": "#a"}, RuntimeError("x" * 500))
        message = reason.split(": ", 1)[1]
        assert len(message) == FAILURE_MESSAGE_MAX_CHARS
        assert message.endswith("…")

    def test_a_message_at_the_limit_is_left_whole(self) -> None:
        reason = step_failure_reason(
            0, {"wait": "#a"}, RuntimeError("y" * FAILURE_MESSAGE_MAX_CHARS)
        )
        assert reason.endswith("y" * FAILURE_MESSAGE_MAX_CHARS)

    def test_a_selector_with_a_newline_still_yields_one_line(self) -> None:
        reason = step_failure_reason(0, {"click": "#a,\n#b"}, self.TIMEOUT)
        assert "\n" not in reason

    def test_an_empty_message_leaves_no_dangling_colon(self) -> None:
        assert step_failure_reason(0, {"wait": "#a"}, TimeoutError()) == (
            "TimeoutError at step 0 wait '#a'"
        )


class TestSummaryLines:
    """The terminal tail after the one-line run summary."""

    def test_a_clean_run_prints_nothing_more(self) -> None:
        assert summary_lines([], [SceneResult(name="ok", file="cases/scenes/a.png", ok=True)]) == []

    def test_a_failed_scene_prints_its_reason(self) -> None:
        reason = "TimeoutError at step 1 click '#x': Timeout 5000ms exceeded."
        lines = summary_lines(
            [],
            [
                SceneResult(
                    name="transactions-blotter-impact-open",
                    file=None,
                    ok=False,
                    failed_step=1,
                    reason=reason,
                )
            ],
        )
        assert lines == [f"  ✗ transactions-blotter-impact-open — {reason}"]

    def test_a_suspect_capture_and_a_flagged_scene_print_their_files(self) -> None:
        capture = Capture(
            area="investments",
            path="/investments",
            file="investments/investments.png",
            status=500,
            final_url="http://tenant.localhost:8000/investments",
            partial=False,
            session="tenant",
            suspect=True,
            reason="HTTP 500",
        )
        scene = SceneResult(
            name="charts",
            file="investments/scenes/charts.png",
            ok=True,
            charts_total=3,
            charts_pending=1,
        )
        assert summary_lines([capture], [scene]) == [
            "  ! investments/investments.png — HTTP 500",
            "  ! investments/scenes/charts.png — charts not drawn (1 of 3)",
        ]


class TestScenesOnlyIndex:
    """The contact sheet of a ``--scenes-only`` run."""

    def test_no_empty_heading_stands_before_the_scenes(self, tmp_path: Path) -> None:
        write_index(
            tmp_path,
            base_url="http://tenant.localhost:8000",
            viewport=(1440, 900),
            captured=[],
            scenes=[SceneResult(name="cases-detail", file="cases/scenes/case-detail.png", ok=True)],
            skipped=[],
        )
        sheet = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert "### Pages" not in sheet
        assert "### Skipped routes" not in sheet
        # The Area heading leads straight into its scenes, and the sheet ends
        # on the "needs a look" verdict rather than on a heading over nothing.
        assert "## cases\n\n### Scenes\n\n**cases-detail**" in sheet
        assert sheet.rstrip().endswith("Nothing suspect and no failed scene.")


class TestSceneSelection:
    """``--scene``: a typo must cost nothing, and say what would have worked."""

    def scenes_file(self, tmp_path: Path) -> Path:
        """Write a three-scene file across two Areas.

        Args:
            tmp_path: Where to write it.

        Returns:
            The file's path.
        """
        path = tmp_path / "atlas-scenes.json"
        path.write_text(
            json.dumps(
                [
                    {"name": "tx-a", "area": "transactions", "steps": [], "shot": "a"},
                    {"name": "tx-b", "area": "transactions", "steps": [], "shot": "b"},
                    {"name": "wd-journal", "area": "watch_desk", "steps": [], "shot": "j"},
                ]
            ),
            encoding="utf-8",
        )
        return path

    def test_no_names_keeps_every_scene_in_the_areas(self, tmp_path: Path) -> None:
        names = [s["name"] for s in select_scenes(self.scenes_file(tmp_path), None, None)]
        assert names == ["tx-a", "tx-b", "wd-journal"]

    def test_the_filter_keeps_the_named_scenes_in_file_order(self, tmp_path: Path) -> None:
        scenes = select_scenes(self.scenes_file(tmp_path), None, ["wd-journal", "tx-a"])
        assert [s["name"] for s in scenes] == ["tx-a", "wd-journal"]

    def test_area_and_scene_combine(self, tmp_path: Path) -> None:
        scenes = select_scenes(self.scenes_file(tmp_path), ["transactions"], ["tx-b"])
        assert [s["name"] for s in scenes] == ["tx-b"]

    def test_an_unknown_name_lists_the_known_ones(self, tmp_path: Path) -> None:
        with pytest.raises(SceneSelectionError, match="unknown scene") as caught:
            select_scenes(self.scenes_file(tmp_path), None, ["tx-c"])
        assert "tx-a, tx-b, wd-journal" in str(caught.value)

    def test_a_name_outside_the_areas_is_not_called_unknown(self, tmp_path: Path) -> None:
        with pytest.raises(SceneSelectionError) as caught:
            select_scenes(self.scenes_file(tmp_path), ["transactions"], ["wd-journal"])
        message = str(caught.value)
        assert "not in the selected areas (transactions)" in message
        assert "unknown" not in message
        assert "tx-a, tx-b" in message

    def test_main_exits_2_on_an_unknown_name_before_any_browser(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Nothing downstream of the selection may run: no Playwright, no
        # credentials. Pulling the package out proves the order — reaching
        # its check would return EXIT_NO_BROWSER instead of raising.
        monkeypatch.setattr(ux_atlas, "playwright_api", None)
        with pytest.raises(SystemExit) as caught:
            main(["--scene", "cases-detial"])
        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "unknown scene(s): cases-detial" in err
        assert "cases-detail" in err

    def test_main_reports_a_scene_outside_the_area(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(ux_atlas, "playwright_api", None)
        with pytest.raises(SystemExit) as caught:
            main(["--area", "transactions", "--scene", "watch-desk-journal"])
        assert caught.value.code == 2
        assert "not in the selected areas (transactions)" in capsys.readouterr().err

    def test_scene_and_no_scenes_contradict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ux_atlas, "playwright_api", None)
        with pytest.raises(SystemExit) as caught:
            main(["--no-scenes", "--scene", "cases-detail"])
        assert caught.value.code == 2

    def test_scenes_only_and_no_scenes_are_exclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ux_atlas, "playwright_api", None)
        with pytest.raises(SystemExit) as caught:
            main(["--no-scenes", "--scenes-only"])
        assert caught.value.code == 2

    def test_a_valid_selection_proceeds_past_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the package gone the next gate is the missing-package exit, which
        # is only reached once the selection has passed.
        monkeypatch.setattr(ux_atlas, "playwright_api", None)
        assert main(["--scene", "cases-detail", "--scenes-only"]) == ux_atlas.EXIT_NO_BROWSER
