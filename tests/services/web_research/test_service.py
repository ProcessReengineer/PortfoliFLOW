# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for :class:`services.web_research.service.WebResearchService`.

All external calls (feed fetch, article fetch, extract, LLM) are mocked;
these tests exercise the RSS + pre-filter + fetch orchestration logic,
not the real network or real LLM.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.web_research.allowlist import AllowlistConfig, AllowlistEntry
from services.web_research.fetcher import (
    ExtractionError,
    FeedFetchResult,
    FetchError,
    FetchResult,
)
from services.web_research.models import FeedItem
from services.web_research.service import WebResearchService


_FETCHER_PROMPT = "fake fetcher prompt"
_FILTER_PROMPT = "fake feed-filter prompt"


def _allowlist(entries: list[AllowlistEntry] | None = None) -> AllowlistConfig:
    if entries is None:
        entries = [
            AllowlistEntry(
                domain="www.ecb.europa.eu",
                name="ECB",
                added_on="2026-04-24",
                rationale="x",
                feeds=("https://www.ecb.europa.eu/rss/press.html",),
            ),
            AllowlistEntry(
                domain="www.ft.com",
                name="FT",
                added_on="2026-04-24",
                rationale="x",
                feeds=("https://www.ft.com/rss/home",),
            ),
        ]
    return AllowlistConfig(entries=tuple(entries), default_window_hours=72)


def _svc(allowlist: AllowlistConfig | None = None) -> WebResearchService:
    return WebResearchService(
        allowlist=allowlist if allowlist is not None else _allowlist(),
        fetcher_prompt=_FETCHER_PROMPT,
        feed_filter_prompt=_FILTER_PROMPT,
    )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _feed_item(
    url: str,
    *,
    title: str = "t",
    hours_ago: float = 1.0,
    source: str = "ECB",
) -> FeedItem:
    return FeedItem.from_components(
        url=url,
        title=title,
        description="d",
        published_at=_now_utc() - timedelta(hours=hours_ago),
        source_name=source,
    )


def _valid_fetcher_json(url: str) -> str:
    return json.dumps(
        {
            "source_url": url,
            "fetched_at": "2026-04-24T10:24:00+00:00",
            "title": "ECB holds rates",
            "publication_date": "2026-04-24",
            "key_facts": [
                "The ECB held its benchmark deposit rate at 2.5%.",
                "Inflation is returning to the 2% medium-term target.",
            ],
            "relevant_asset_classes": ["fixed_income"],
            "injection_detected": False,
            "injection_details": None,
        }
    )


def _article_fetch(url: str) -> FetchResult:
    return FetchResult(
        final_url=url,
        status_code=200,
        raw_html="<html><body>long body " + ("x " * 300) + "</body></html>",
        content_length=1000,
    )


@pytest.fixture
def patched_ai_service():
    with patch("services.web_research.service.get_ai_service") as mock_get:
        fake = MagicMock()
        fake.get_model.return_value = "fake-model"
        mock_get.return_value = fake
        yield fake


class TestEmptyFeedPool:
    def test_empty_feed_pool_returns_empty(self, patched_ai_service) -> None:
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"<rss><channel></channel></rss>",
                    content_length=30,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                return_value=[],
            ),
        ):
            assert _svc().research("x") == []
        patched_ai_service.send_one_shot_extraction.assert_not_called()

    def test_all_feed_fetches_fail_returns_empty(self, patched_ai_service) -> None:
        with patch(
            "services.web_research.service.fetch_feed",
            side_effect=FetchError("boom"),
        ):
            assert _svc().research("x") == []
        patched_ai_service.send_one_shot_extraction.assert_not_called()


class TestTimeFilter:
    def test_old_items_are_dropped(self, patched_ai_service) -> None:
        old = _feed_item("https://www.ecb.europa.eu/old", hours_ago=200.0)
        fresh = _feed_item("https://www.ecb.europa.eu/fresh", hours_ago=1.0)

        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[old, fresh], []],
            ),
        ):
            # Pre-filter LLM returns the fresh URL.
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": ["https://www.ecb.europa.eu/fresh"]}),
                _valid_fetcher_json("https://www.ecb.europa.eu/fresh"),
            ]
            with (
                patch(
                    "services.web_research.service.fetch_url",
                    side_effect=lambda u, timeout=8.0: _article_fetch(u),
                ),
                patch(
                    "services.web_research.service.extract_text",
                    return_value="clean article text " * 50,
                ),
            ):
                results = _svc().research("x", max_articles=5)
        assert len(results) == 1


class TestPreFilter:
    def test_pre_filter_empty_selection_means_no_fetches(self, patched_ai_service) -> None:
        item = _feed_item("https://www.ecb.europa.eu/a")
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch("services.web_research.service.fetch_url") as fetch_url_mock,
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": []}),
            ]
            results = _svc().research("x")
        assert results == []
        fetch_url_mock.assert_not_called()

    def test_pre_filter_invalid_url_rejected(self, patched_ai_service) -> None:
        """Defence in depth: URLs not in the candidate list must be dropped."""
        item = _feed_item("https://www.ecb.europa.eu/a")
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch("services.web_research.service.fetch_url") as fetch_url_mock,
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": ["https://evil.example.com/injected"]}),
            ]
            results = _svc().research("x")
        assert results == []
        fetch_url_mock.assert_not_called()

    def test_pre_filter_schema_failure_means_empty(self, patched_ai_service) -> None:
        item = _feed_item("https://www.ecb.europa.eu/a")
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch("services.web_research.service.fetch_url") as fetch_url_mock,
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"bogus": "shape"}),
            ]
            results = _svc().research("x")
        assert results == []
        fetch_url_mock.assert_not_called()

    def test_pre_filter_accepts_fenced_json(self, patched_ai_service) -> None:
        """Feed-Filter-LLM may wrap its JSON in ```json ... ``` — we must recover it."""
        item = _feed_item("https://www.ecb.europa.eu/pr260424")
        fenced = '```json\n{"selected_urls": ["https://www.ecb.europa.eu/pr260424"]}\n```'
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch(
                "services.web_research.service.fetch_url",
                side_effect=lambda u, timeout=8.0: _article_fetch(u),
            ),
            patch(
                "services.web_research.service.extract_text",
                return_value="clean article text " * 50,
            ),
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                fenced,
                _valid_fetcher_json("https://www.ecb.europa.eu/pr260424"),
            ]
            results = _svc().research("x")
        assert len(results) == 1
        assert results[0].title == "ECB holds rates"

    def test_pre_filter_non_json_means_empty(self, patched_ai_service) -> None:
        item = _feed_item("https://www.ecb.europa.eu/a")
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch("services.web_research.service.fetch_url") as fetch_url_mock,
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                "not valid json",
            ]
            results = _svc().research("x")
        assert results == []
        fetch_url_mock.assert_not_called()

    def test_pre_filter_honours_max_articles(self, patched_ai_service) -> None:
        items = [_feed_item(f"https://www.ecb.europa.eu/{i}") for i in range(5)]
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[items, []],
            ),
            patch(
                "services.web_research.service.fetch_url",
                side_effect=lambda u, timeout=8.0: _article_fetch(u),
            ),
            patch(
                "services.web_research.service.extract_text",
                return_value="clean article text " * 50,
            ),
        ):
            # Filter returns all five; we cap at max_articles=2.
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": [f"https://www.ecb.europa.eu/{i}" for i in range(5)]}),
                _valid_fetcher_json("https://www.ecb.europa.eu/0"),
                _valid_fetcher_json("https://www.ecb.europa.eu/1"),
            ]
            results = _svc().research("x", max_articles=2)
        assert len(results) == 2


class TestHappyPath:
    def test_end_to_end(self, patched_ai_service) -> None:
        item = _feed_item("https://www.ecb.europa.eu/pr260424")
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch(
                "services.web_research.service.fetch_url",
                side_effect=lambda u, timeout=8.0: _article_fetch(u),
            ),
            patch(
                "services.web_research.service.extract_text",
                return_value="clean article text " * 50,
            ),
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": ["https://www.ecb.europa.eu/pr260424"]}),
                _valid_fetcher_json("https://www.ecb.europa.eu/pr260424"),
            ]
            results = _svc().research("ECB rate decision")
        assert len(results) == 1
        assert results[0].title == "ECB holds rates"
        assert results[0].key_facts[0].startswith("The ECB")
        assert not results[0].injection_detected

    def test_fetcher_accepts_fenced_json(self, patched_ai_service) -> None:
        """Fetcher-LLM may wrap its JSON in ```json ... ``` — we must recover it."""
        url = "https://www.ft.com/content/62b8159b-1cf1-4dd8-9352-cde0021dec61"
        item = _feed_item(url, source="FT")
        fenced_fetcher_response = (
            "```json\n"
            "{\n"
            f'  "source_url": "{url}",\n'
            '  "fetched_at": "2026-04-24T16:32:18.820308+00:00",\n'
            '  "title": "Banks charged sharply different fees for access to '
            'Anthropic investment",\n'
            '  "publication_date": null,\n'
            '  "key_facts": ["Fees varied widely across banks for this '
            'allocation."],\n'
            '  "relevant_asset_classes": ["private_equity"],\n'
            '  "injection_detected": false,\n'
            '  "injection_details": null\n'
            "}\n"
            "```"
        )
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ft.com/rss/home",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[], [item]],
            ),
            patch(
                "services.web_research.service.fetch_url",
                side_effect=lambda u, timeout=8.0: _article_fetch(u),
            ),
            patch(
                "services.web_research.service.extract_text",
                return_value="clean article text " * 50,
            ),
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": [url]}),
                fenced_fetcher_response,
            ]
            results = _svc().research("Anthropic investment")
        assert len(results) == 1
        assert results[0].source_url == url
        assert results[0].title.startswith("Banks charged")
        assert results[0].key_facts == ["Fees varied widely across banks for this allocation."]
        assert results[0].relevant_asset_classes == ["private_equity"]
        assert not results[0].injection_detected

    def test_extraction_failure_skips_article(self, patched_ai_service) -> None:
        items = [
            _feed_item("https://www.ecb.europa.eu/a"),
            _feed_item("https://www.ecb.europa.eu/b"),
        ]
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[items, []],
            ),
            patch(
                "services.web_research.service.fetch_url",
                side_effect=lambda u, timeout=8.0: _article_fetch(u),
            ),
            patch(
                "services.web_research.service.extract_text",
                side_effect=[
                    ExtractionError("too short"),
                    "clean article text " * 50,
                ],
            ),
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps(
                    {
                        "selected_urls": [
                            "https://www.ecb.europa.eu/a",
                            "https://www.ecb.europa.eu/b",
                        ]
                    }
                ),
                _valid_fetcher_json("https://www.ecb.europa.eu/b"),
            ]
            results = _svc().research("x")
        assert len(results) == 1

    def test_post_redirect_to_non_allowlisted_drops_article(self, patched_ai_service) -> None:
        item = _feed_item("https://www.ecb.europa.eu/pr")
        redirected = FetchResult(
            final_url="https://evil.example.com/stolen",
            status_code=200,
            raw_html="<html></html>",
            content_length=10,
        )
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch(
                "services.web_research.service.fetch_url",
                return_value=redirected,
            ),
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": ["https://www.ecb.europa.eu/pr"]}),
            ]
            results = _svc().research("x")
        assert results == []

    def test_schema_violation_from_fetcher_skips(self, patched_ai_service) -> None:
        item = _feed_item("https://www.ecb.europa.eu/pr")
        with (
            patch(
                "services.web_research.service.fetch_feed",
                return_value=FeedFetchResult(
                    final_url="https://www.ecb.europa.eu/rss/press.html",
                    status_code=200,
                    raw_bytes=b"irrelevant",
                    content_length=10,
                ),
            ),
            patch(
                "services.web_research.service.parse_feed",
                side_effect=[[item], []],
            ),
            patch(
                "services.web_research.service.fetch_url",
                side_effect=lambda u, timeout=8.0: _article_fetch(u),
            ),
            patch(
                "services.web_research.service.extract_text",
                return_value="clean article text " * 50,
            ),
        ):
            patched_ai_service.send_one_shot_extraction.side_effect = [
                json.dumps({"selected_urls": ["https://www.ecb.europa.eu/pr"]}),
                json.dumps({"title": "only a title"}),  # missing fields
            ]
            results = _svc().research("x")
        assert results == []
