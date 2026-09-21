# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Pure-function tests for the atlas's shot geometry (``tools/ux_atlas.py``).

The reveal pass, the band writer and the capture loop all need a live browser,
so none of them is exercised here. What *is* exercised is the arithmetic they
hang on, which is browser-free by construction and is where a mistake would be
silent: a truncated page that never gets flagged, or a band grid that drops the
last slice of every page.

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
