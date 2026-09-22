# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``portfoliflow directory-refresh`` and ``directory-status`` (SB-3b).

What the commands own is a thin surface: the exit-code map, the three seams
(``_now``, ``_default_data_dir``, ``refresh_directory``), and the two
renderings of one outcome. The deciding — verification, the monotonic rule,
what a cache is worth — belongs to ``services/provider_directory`` and is
pinned by ``tests/services/provider_directory``; repeating it here would test
the library twice and the commands not at all.

**Database-free, and pinned as such.** The module-level autouse fixture below
deletes ``DATABASE_URL`` and ``DATABASE_URL_SUPERUSER`` from the environment,
so a command that grew a connection would fail here rather than quietly
inheriting a developer's live server. The whole module passes with Postgres
down.

**No literal dates and no real key material.** The end-to-end walk serves the
published fixture — a public document and a public signature — and derives its
instant from that document's own ``issued_at``, following
``tests/services/provider_directory/conftest.py``. The real publishing key is
exercised only as the *public* half already shipped in the ring.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

import cli.directory as directory
from cli import app
from services.provider_directory import (
    CACHE_SUBDIR,
    DIRECTORY_URL,
    DOCUMENT_FILE,
    REFRESH_STATUSES,
    RefreshOutcome,
    RefreshStatus,
    signature_url_for,
)

runner = CliRunner()

#: The published fixture, shared with the provider-channel suite.
FIXTURES: Final[Path] = (
    Path(__file__).resolve().parents[1] / "services" / "provider_channel" / "fixtures"
)

#: The signature sits beside the document, at the URL the library derives.
SIGNATURE_URL: Final[str] = signature_url_for(DIRECTORY_URL)

#: An etag to replay on the conditional request.
ETAG: Final[str] = '"directory-1"'

#: The exit code each :data:`REFRESH_STATUSES` member must produce, in the
#: same order: success, success, five refusals, one unavailable.
EXPECTED_EXIT_CODES: Final[tuple[int, ...]] = (0, 0, 3, 3, 3, 3, 3, 4)

#: Working ids from the coordination layer never reach an operator's screen.
FORBIDDEN_IN_HELP: Final[tuple[str, ...]] = ("Q-SB", "Board v", "register")


@pytest.fixture(autouse=True)
def _offline_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the commands at a scratch data directory and unset every DSN."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_SUPERUSER", raising=False)
    monkeypatch.setattr(directory, "_default_data_dir", lambda: str(tmp_path))


def _install_refresh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: RefreshOutcome | None = None,
    raises: Exception | None = None,
) -> list[dict[str, Any]]:
    """Replace the library call and record the keyword arguments it received."""
    calls: list[dict[str, Any]] = []

    async def _fake(**kwargs: Any) -> RefreshOutcome:
        calls.append(kwargs)
        if raises is not None:
            raise raises
        assert outcome is not None
        return outcome

    monkeypatch.setattr(directory, "refresh_directory", _fake)
    return calls


def _fixture_document() -> dict[str, Any]:
    """The published directory, parsed — the source of every date below."""
    decoded = json.loads((FIXTURES / "directory-1.json").read_bytes())
    assert isinstance(decoded, dict)
    return decoded


def _fixture_instant(document: dict[str, Any]) -> datetime:
    """One day after the fixture's ``issued_at``, at noon UTC.

    Derived rather than written down, so an edited validity window moves this
    instant with it instead of falling outside it.
    """
    issued_at = document["issued_at"]
    assert isinstance(issued_at, str)
    return datetime.fromisoformat(issued_at).replace(hour=12, tzinfo=timezone.utc) + timedelta(
        days=1
    )


def _serve_the_published_fixture(httpx_mock: HTTPXMock) -> None:
    """Answer one document request and one signature request with the fixture."""
    httpx_mock.add_response(
        url=DIRECTORY_URL,
        content=(FIXTURES / "directory-1.json").read_bytes(),
        headers={"ETag": ETAG},
    )
    httpx_mock.add_response(url=SIGNATURE_URL, content=(FIXTURES / "directory-1.sig").read_bytes())


#: Typer forces a Rich terminal under ``GITHUB_ACTIONS``/``FORCE_COLOR``, which
#: styles option names in fragments — so ``--data-dir`` is no longer contiguous.
_ANSI: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Strip ANSI escape sequences, so a help assertion holds in any terminal mode.

    ``typer.rich_utils`` decides ``FORCE_TERMINAL`` when it is imported, so no
    ``env=`` on the invocation can switch the styling off again — the assertion
    side has to be the robust one.

    Args:
        text: Captured command output, styled or not.

    Returns:
        The same text with every CSI escape sequence removed.
    """
    return _ANSI.sub("", text)


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_refresh_help_names_its_flags_and_carries_no_working_id() -> None:
    result = runner.invoke(app, ["directory-refresh", "--help"], env={"COLUMNS": "200"})
    output = _plain(result.output)

    assert result.exit_code == 0, result.output
    assert "--url" in output
    assert "--data-dir" in output
    assert "--json" in output
    for forbidden in FORBIDDEN_IN_HELP:
        assert forbidden not in output


def test_status_help_names_its_flags_and_carries_no_working_id() -> None:
    result = runner.invoke(app, ["directory-status", "--help"], env={"COLUMNS": "200"})
    output = _plain(result.output)

    assert result.exit_code == 0, result.output
    assert "--data-dir" in output
    assert "--json" in output
    for forbidden in FORBIDDEN_IN_HELP:
        assert forbidden not in output


def test_both_commands_are_registered_on_the_operator_cli() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "directory-refresh" in result.output
    assert "directory-status" in result.output


# ---------------------------------------------------------------------------
# The exit-code map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_exit_code"),
    list(zip(REFRESH_STATUSES, EXPECTED_EXIT_CODES)),
)
def test_every_refresh_status_maps_to_its_documented_exit_code(
    monkeypatch: pytest.MonkeyPatch, status: RefreshStatus, expected_exit_code: int
) -> None:
    _install_refresh(
        monkeypatch,
        outcome=RefreshOutcome(status=status, notices=("n1",), provenance=None),
    )

    result = runner.invoke(app, ["directory-refresh"])

    assert result.exit_code == expected_exit_code, result.output
    assert f"status: {status}" in result.stdout
    assert "notice: n1" in result.stdout
    assert "provenance: none" in result.stdout


def test_the_status_map_covers_every_status_the_library_defines() -> None:
    assert len(EXPECTED_EXIT_CODES) == len(REFRESH_STATUSES)


# ---------------------------------------------------------------------------
# Caller errors
# ---------------------------------------------------------------------------


def test_a_value_error_from_the_library_is_a_caller_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_refresh(monkeypatch, raises=ValueError("now must be timezone-aware"))

    result = runner.invoke(app, ["directory-refresh"])

    assert result.exit_code == 2, result.output
    assert "now must be timezone-aware" in result.stderr


def test_an_unwritable_cache_directory_is_a_caller_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_refresh(monkeypatch, raises=OSError("Permission denied"))

    result = runner.invoke(app, ["directory-refresh"])

    assert result.exit_code == 2, result.output
    assert "Permission denied" in result.stderr


def test_a_url_that_is_not_a_json_document_is_refused_before_any_request(
    httpx_mock: HTTPXMock,
) -> None:
    result = runner.invoke(
        app, ["directory-refresh", "--url", "https://example.test/directory.txt"]
    )

    assert result.exit_code == 2, result.output
    assert ".json" in result.stderr
    assert httpx_mock.get_requests() == []


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def test_url_data_dir_and_timeout_reach_the_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_refresh(
        monkeypatch,
        outcome=RefreshOutcome(status="unchanged", notices=(), provenance=None),
    )
    elsewhere = tmp_path / "elsewhere"

    result = runner.invoke(
        app,
        [
            "directory-refresh",
            "--url",
            "https://mirror.test/directory.json",
            "--data-dir",
            str(elsewhere),
            "--timeout",
            "3.5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["url"] == "https://mirror.test/directory.json"
    assert calls[0]["cache_root"] == elsewhere / CACHE_SUBDIR
    assert calls[0]["timeout"] == 3.5


def test_the_default_cache_root_comes_from_the_data_dir_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_refresh(
        monkeypatch,
        outcome=RefreshOutcome(status="unchanged", notices=(), provenance=None),
    )

    result = runner.invoke(app, ["directory-refresh"])

    assert result.exit_code == 0, result.output
    assert calls[0]["cache_root"] == tmp_path / CACHE_SUBDIR


def test_the_instant_handed_to_the_library_is_the_modules_own_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(directory, "_now", lambda: moment)
    calls = _install_refresh(
        monkeypatch,
        outcome=RefreshOutcome(status="unchanged", notices=(), provenance=None),
    )

    result = runner.invoke(app, ["directory-refresh"])

    assert result.exit_code == 0, result.output
    assert calls[0]["now"] == moment


# ---------------------------------------------------------------------------
# End to end, against the published fixture
# ---------------------------------------------------------------------------


def test_a_published_directory_is_fetched_cached_and_then_described_by_status(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    document = _fixture_document()
    moment = _fixture_instant(document)
    monkeypatch.setattr(directory, "_now", lambda: moment)
    _serve_the_published_fixture(httpx_mock)

    refreshed = runner.invoke(app, ["directory-refresh", "--json"])

    assert refreshed.exit_code == 0, refreshed.output
    payload = json.loads(refreshed.stdout)
    assert payload["status"] == "updated"
    assert payload["provenance"]["directory_version"] == document["directory_version"]
    assert payload["provenance"]["provider_count"] == len(document["providers"])
    assert payload["provenance"]["valid"] is True
    assert (
        payload["days_left"] == (date.fromisoformat(document["valid_until"]) - moment.date()).days
    )

    described = runner.invoke(app, ["directory-status", "--json"])

    assert described.exit_code == 0, described.output
    assert json.loads(described.stdout)["provenance"] == payload["provenance"]


def test_a_second_refresh_answered_304_reports_the_copy_unchanged(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    document = _fixture_document()
    monkeypatch.setattr(directory, "_now", lambda: _fixture_instant(document))
    _serve_the_published_fixture(httpx_mock)
    httpx_mock.add_response(
        url=DIRECTORY_URL, status_code=304, match_headers={"If-None-Match": ETAG}
    )

    assert runner.invoke(app, ["directory-refresh"]).exit_code == 0

    again = runner.invoke(app, ["directory-refresh", "--json"])

    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["status"] == "unchanged"


def test_a_tampered_cached_document_is_present_but_unusable(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, tmp_path: Path
) -> None:
    document = _fixture_document()
    monkeypatch.setattr(directory, "_now", lambda: _fixture_instant(document))
    _serve_the_published_fixture(httpx_mock)
    assert runner.invoke(app, ["directory-refresh"]).exit_code == 0

    cached = tmp_path / CACHE_SUBDIR / DOCUMENT_FILE
    cached.write_bytes(cached.read_bytes().replace(b"[TEST]", b"[TSET]", 1))

    result = runner.invoke(app, ["directory-status"])

    assert result.exit_code == 5, result.output
    assert "unusable" in result.stderr
    assert "no cache" not in result.stderr


def test_an_empty_data_directory_reports_no_cache(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = runner.invoke(app, ["directory-status", "--data-dir", str(empty)])

    assert result.exit_code == 5, result.output
    assert "no cache" in result.stderr
    assert "unusable" not in result.stderr


# ---------------------------------------------------------------------------
# The JSON rendering
# ---------------------------------------------------------------------------


def test_refresh_json_is_exactly_one_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_refresh(
        monkeypatch,
        outcome=RefreshOutcome(status="unavailable", notices=("offline",), provenance=None),
    )

    result = runner.invoke(app, ["directory-refresh", "--json"])

    assert result.exit_code == 4, result.output
    assert json.loads(result.stdout) == {
        "status": "unavailable",
        "notices": ["offline"],
        "provenance": None,
        "days_left": None,
    }


def test_status_json_is_exactly_one_object(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    document = _fixture_document()
    monkeypatch.setattr(directory, "_now", lambda: _fixture_instant(document))
    _serve_the_published_fixture(httpx_mock)
    assert runner.invoke(app, ["directory-refresh"]).exit_code == 0

    result = runner.invoke(app, ["directory-status", "--json"])

    assert result.exit_code == 0, result.output
    assert set(json.loads(result.stdout)) == {"provenance", "days_left"}
