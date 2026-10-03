# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for the ``WebSettings`` upload-cap and build-identifier fields.

Both values used to be read from the environment inside ``web/`` on every
request; they are settings fields now, loaded once and validated at startup.
Every case passes ``_env_file=None`` and pins its environment variable, so a
developer ``.env`` cannot decide an outcome.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from web.settings import WebSettings


def _load(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> WebSettings:
    """Build ``WebSettings`` from the environment alone, with ``name`` pinned."""
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    return WebSettings(_env_file=None)


def test_upload_cap_defaults_to_50(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, the cap is the documented default."""
    assert _load(monkeypatch, "WEB_MAX_UPLOAD_SIZE_MB", None).web_max_upload_size_mb == 50


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("7", 7), ("200", 200)])
def test_upload_cap_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """``WEB_MAX_UPLOAD_SIZE_MB`` keeps its name and meaning."""
    settings = _load(monkeypatch, "WEB_MAX_UPLOAD_SIZE_MB", raw)
    assert settings.web_max_upload_size_mb == expected


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_upload_cap_below_one_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Rejected, never clamped: startup fails with the reason."""
    with pytest.raises(ValidationError) as excinfo:
        _load(monkeypatch, "WEB_MAX_UPLOAD_SIZE_MB", raw)
    assert "WEB_MAX_UPLOAD_SIZE_MB" in str(excinfo.value)


@pytest.mark.parametrize("raw", ["abc", "2.5", ""])
def test_upload_cap_that_is_not_a_whole_number_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """No silent fallback to the default for a value that does not parse."""
    with pytest.raises(ValidationError):
        _load(monkeypatch, "WEB_MAX_UPLOAD_SIZE_MB", raw)


@pytest.mark.parametrize(("raw", "expected"), [(None, "dev"), ("", "dev"), ("   ", "dev")])
def test_blank_build_sha_means_dev(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str
) -> None:
    """Unset or blank renders as ``dev``, as the footer always showed."""
    assert _load(monkeypatch, "BUILD_SHA", raw).build_sha == expected


def test_build_sha_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Surrounding whitespace from a deployment script does not reach the link."""
    assert _load(monkeypatch, "BUILD_SHA", "  1a2b3c4\n").build_sha == "1a2b3c4"
