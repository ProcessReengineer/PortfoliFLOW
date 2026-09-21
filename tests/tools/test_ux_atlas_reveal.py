# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure-function tests for the atlas's shot geometry (``tools/ux_atlas.py``).

The reveal pass, the loader loop, the chart wait, the band writer and the
capture loop all need a live browser, so none of them is exercised here. What
*is* exercised is the arithmetic and the pattern matching they hang on, which
are browser-free by construction and are where a mistake would be silent: a
truncated page that never gets flagged, a band grid that drops the last slice
of every page, a debounce that returns on its first quiet sample, or a
placeholder regex that misses the placeholders the templates actually write.

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
import struct
import zlib
from pathlib import Path

import pytest

from tools.ux_atlas import (
    CHROMIUM_MAX_AXIS_PX,
    LOADING_PLACEHOLDER_RE,
    QUIET_HOLD_MS,
    advance_quiet_window,
    band_rects,
    is_truncated,
    png_height,
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
