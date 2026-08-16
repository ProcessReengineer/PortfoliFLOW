# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :mod:`services.web_research.allowlist`."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from services.web_research.allowlist import (
    AllowlistEntry,
    AllowlistError,
    get_effective_window,
    is_allowed,
    load_allowlist,
)


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadAllowlist:
    def test_happy_path(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "Monetary policy comms."
    feeds:
      - "https://www.ecb.europa.eu/rss/press.html"
  - domain: "www.ft.com"
    name: "FT"
    added_on: "2026-04-24"
    rationale: "UK daily."
    feeds:
      - "https://www.ft.com/rss/home"
""",
        )
        cfg = load_allowlist(src)
        assert cfg.default_window_hours == 72
        assert len(cfg.entries) == 2
        assert cfg.entries[0].domain == "www.ecb.europa.eu"
        assert cfg.entries[0].feeds == ("https://www.ecb.europa.eu/rss/press.html",)
        assert cfg.entries[0].window_hours is None
        assert cfg.entries[1].feeds == ("https://www.ft.com/rss/home",)

    def test_per_source_window_override(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
    window_hours: 24
""",
        )
        cfg = load_allowlist(src)
        assert cfg.entries[0].window_hours == 24

    def test_domain_lowercased(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "WWW.ECB.EUROPA.EU"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://WWW.ECB.EUROPA.EU/rss/press.html"]
""",
        )
        cfg = load_allowlist(src)
        assert cfg.entries[0].domain == "www.ecb.europa.eu"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AllowlistError, match="not found"):
            load_allowlist(tmp_path / "does-not-exist.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(tmp_path / "bad.yaml", "sources: [ : : : }")
        with pytest.raises(AllowlistError, match="parse YAML"):
            load_allowlist(src)

    def test_top_level_not_mapping_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(tmp_path / "bad.yaml", "- 1\n- 2\n")
        with pytest.raises(AllowlistError, match="must be a mapping"):
            load_allowlist(src)

    def test_missing_default_window_hours_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://a.test/feed"]
""",
        )
        with pytest.raises(AllowlistError, match="default_window_hours"):
            load_allowlist(src)

    def test_invalid_default_window_hours_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 0
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://a.test/feed"]
""",
        )
        with pytest.raises(AllowlistError, match="positive integer"):
            load_allowlist(src)

    def test_missing_sources_key_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            "default_window_hours: 72\nsomething_else: []\n",
        )
        with pytest.raises(AllowlistError, match="'sources'"):
            load_allowlist(src)

    def test_sources_not_list_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            "default_window_hours: 72\nsources: 42\n",
        )
        with pytest.raises(AllowlistError, match="non-empty list"):
            load_allowlist(src)

    def test_empty_sources_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            "default_window_hours: 72\nsources: []\n",
        )
        with pytest.raises(AllowlistError, match="non-empty list"):
            load_allowlist(src)

    def test_entry_not_mapping_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            "default_window_hours: 72\nsources:\n  - 'string-entry'\n",
        )
        with pytest.raises(AllowlistError, match="must be a mapping"):
            load_allowlist(src)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 72
sources:
  - domain: "a.test"
    name: "A"
    feeds: ["https://a.test/feed"]
""",
        )
        with pytest.raises(AllowlistError, match="missing required field"):
            load_allowlist(src)

    def test_feeds_missing_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 72
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
""",
        )
        with pytest.raises(AllowlistError, match="feeds"):
            load_allowlist(src)

    def test_feeds_empty_list_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 72
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: []
""",
        )
        with pytest.raises(AllowlistError, match="non-empty list"):
            load_allowlist(src)

    def test_feed_entry_not_string_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 72
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: [42]
""",
        )
        with pytest.raises(AllowlistError, match="non-empty string"):
            load_allowlist(src)

    def test_feed_host_not_on_allowlist_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 72
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://somewhere-else.test/feed"]
""",
        )
        with pytest.raises(AllowlistError, match="not on the allowlist"):
            load_allowlist(src)

    def test_invalid_window_hours_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "bad.yaml",
            """
default_window_hours: 72
sources:
  - domain: "a.test"
    name: "A"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://a.test/feed"]
    window_hours: -1
""",
        )
        with pytest.raises(AllowlistError, match="positive integer"):
            load_allowlist(src)

    def test_repo_allowlist_loads(self) -> None:
        """The real config/web_research.yaml loads cleanly."""
        cfg = load_allowlist()
        assert cfg.default_window_hours > 0
        assert len(cfg.entries) >= 3
        for e in cfg.entries:
            assert isinstance(e.domain, str) and "." in e.domain
            assert len(e.feeds) >= 1

    def test_repo_allowlist_every_source_is_tagged(self) -> None:
        """Every curated source in the real config carries >=1 known tag."""
        from services.web_research.allowlist import _KNOWN_TAGS

        cfg = load_allowlist()
        for e in cfg.entries:
            assert e.tags, f"{e.name} has no tags"
            assert set(e.tags) <= _KNOWN_TAGS
            # Normalised: lowercase, sorted, de-duplicated.
            assert list(e.tags) == sorted(set(e.tags))
            assert all(t == t.lower() for t in e.tags)


class TestTags:
    def test_parses_multi_valued_tags(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    tags: ["macro", "regulator"]
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
""",
        )
        cfg = load_allowlist(src)
        assert cfg.entries[0].tags == ("macro", "regulator")

    def test_missing_tags_key_yields_empty_tuple(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
""",
        )
        cfg = load_allowlist(src)
        assert cfg.entries[0].tags == ()

    def test_tags_normalised_lowercase_sorted_deduped(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    tags: ["Regulator", "MACRO", "macro"]
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
""",
        )
        cfg = load_allowlist(src)
        # Deterministic: lowercased, de-duplicated, sorted.
        assert cfg.entries[0].tags == ("macro", "regulator")

    def test_unknown_tag_fails_load(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    tags: ["macro", "not_a_real_tag"]
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
""",
        )
        with pytest.raises(AllowlistError, match="curated tag vocabulary"):
            load_allowlist(src)

    def test_tags_not_a_list_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    tags: "macro"
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
""",
        )
        with pytest.raises(AllowlistError, match="must be a list"):
            load_allowlist(src)

    def test_empty_tag_string_raises(self, tmp_path: Path) -> None:
        src = _write_yaml(
            tmp_path / "al.yaml",
            """
default_window_hours: 72
sources:
  - domain: "www.ecb.europa.eu"
    name: "ECB"
    added_on: "2026-04-24"
    rationale: "x"
    tags: ["macro", "  "]
    feeds: ["https://www.ecb.europa.eu/rss/press.html"]
""",
        )
        with pytest.raises(AllowlistError, match="non-empty string"):
            load_allowlist(src)


class TestIsAllowed:
    def _entry(self, domain: str = "www.ecb.europa.eu") -> AllowlistEntry:
        return AllowlistEntry(
            domain=domain,
            name="n",
            added_on="2026-04-24",
            rationale="x",
            feeds=(f"https://{domain}/rss",),
        )

    def test_exact_host_allowed(self) -> None:
        assert is_allowed("https://www.ecb.europa.eu/press/x", [self._entry()])

    def test_host_case_insensitive(self) -> None:
        assert is_allowed("https://WWW.ECB.EUROPA.EU/press/x", [self._entry()])

    def test_subdomain_not_allowed(self) -> None:
        assert not is_allowed("https://uk.ecb.europa.eu/press/x", [self._entry()])

    def test_different_host_not_allowed(self) -> None:
        assert not is_allowed("https://evil.example.com/", [self._entry()])

    def test_invalid_url_returns_false(self) -> None:
        assert not is_allowed("not-a-url", [self._entry()])

    def test_empty_entries_rejects_all(self) -> None:
        assert not is_allowed("https://www.ecb.europa.eu/", [])


class TestGetEffectiveWindow:
    def _entry(self, window_hours: int | None) -> AllowlistEntry:
        return AllowlistEntry(
            domain="a.test",
            name="A",
            added_on="2026-04-24",
            rationale="x",
            feeds=("https://a.test/feed",),
            window_hours=window_hours,
        )

    def test_uses_global_default_when_unset(self) -> None:
        assert get_effective_window(self._entry(None), 72) == timedelta(hours=72)

    def test_per_entry_override_takes_precedence(self) -> None:
        assert get_effective_window(self._entry(24), 72) == timedelta(hours=24)
