# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Unit tests for the pure components of the Report Scraper backend.

These tests cover services/scraper/{models,capabilities,message_builder,
json_parser}.py. The orchestrating ScraperService is tested separately
(see test_scraper_service.py — future).
"""

from __future__ import annotations

import base64

import pytest

from services.scraper.capabilities import (
    ModelCapability,
    UnsupportedModelError,
    load_capabilities,
    lookup_capability,
)
from services.scraper.json_parser import JsonParseError, parse_extraction_response
from services.scraper.message_builder import build_extraction_messages
from services.scraper.models import (
    Attachment,
    Confidence,
    Keyword,
    KeywordType,
    ReportExtraction,
    ScraperResult,
)


# ---------------------------------------------------------------------------
# TestModels
# ---------------------------------------------------------------------------


class TestModels:
    def test_keyword_is_frozen(self) -> None:
        kw = Keyword(name="NAV", type=KeywordType.NUMBER)
        with pytest.raises(Exception):
            kw.name = "other"  # type: ignore[misc]

    def test_report_extraction_defaults(self) -> None:
        r = ReportExtraction(filename="fund.pdf")
        assert r.fund_name == ""
        assert r.period == ""
        assert r.findings == []
        assert r.error is None

    def test_scraper_result_defaults(self) -> None:
        r = ScraperResult()
        assert r.extractions == []
        assert r.cancelled is False

    def test_attachment_accepts_bytes_and_str(self) -> None:
        a_bytes = Attachment(filename="report.pdf", mime_type="application/pdf", data=b"%PDF-1.4")
        a_str = Attachment(filename="notes.md", mime_type="text/markdown", data="Some text")
        assert isinstance(a_bytes.data, bytes)
        assert isinstance(a_str.data, str)

    def test_confidence_enum_values(self) -> None:
        assert Confidence("High").value == "High"
        assert Confidence("Medium").value == "Medium"
        assert Confidence("Low").value == "Low"
        assert Confidence("Not_Found").value == "Not_Found"
        with pytest.raises(ValueError):
            Confidence("invalid")


# ---------------------------------------------------------------------------
# TestCapabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_load_capabilities_reads_real_config(self) -> None:
        caps = load_capabilities()
        assert len(caps) > 0
        assert any(c.format == "openrouter_file" for c in caps)

    def test_lookup_matches_anthropic_pattern(self) -> None:
        cap = lookup_capability("anthropic/claude-sonnet-4.5")
        assert cap.format == "openrouter_file"
        assert cap.max_pdf_mb == 32

    def test_lookup_matches_with_wildcards(self) -> None:
        cap = lookup_capability("anthropic/claude-haiku-3.5")
        assert cap.format == "openrouter_file"

    def test_lookup_raises_unsupported_for_openai(self) -> None:
        with pytest.raises(UnsupportedModelError) as exc_info:
            lookup_capability("openai/gpt-4o")
        msg = str(exc_info.value)
        assert "Supported model patterns" in msg
        assert "anthropic/claude-*" in msg

    def test_lookup_raises_unsupported_for_empty_string(self) -> None:
        with pytest.raises(UnsupportedModelError):
            lookup_capability("")

    def test_lookup_with_injected_capabilities(self) -> None:
        custom = [ModelCapability(pattern="test/*", format="fake", max_pdf_mb=1, max_pdf_pages=1)]
        cap = lookup_capability("test/foo", capabilities=custom)
        assert cap.format == "fake"
        assert cap.max_pdf_mb == 1


# ---------------------------------------------------------------------------
# TestMessageBuilder
# ---------------------------------------------------------------------------


_SYSTEM = "You are a scraper."
_INSTRUCTION = "Extract the following from the report."
_KWS = [Keyword("NAV", KeywordType.NUMBER), Keyword("IRR", KeywordType.PERCENTAGE)]
_PDF_ATT = Attachment(filename="report.pdf", mime_type="application/pdf", data=b"%PDF-1.4 dummy")


class TestMessageBuilder:
    def test_build_messages_returns_system_and_user(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        assert len(result) == 2
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"

    def test_system_content_equals_prompt(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        assert result[0]["content"] == _SYSTEM

    def test_user_content_is_list_of_blocks(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        content = result[1]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"

    def test_keywords_appear_in_instruction(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        text_block = result[1]["content"][0]
        assert "NAV (Number)" in text_block["text"]
        assert "IRR (Percentage)" in text_block["text"]

    def test_user_instruction_precedes_keywords(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        text = result[1]["content"][0]["text"]
        assert text.index(_INSTRUCTION) < text.index("Keywords to extract:")

    def test_pdf_attachment_becomes_file_block_for_openrouter(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        content = result[1]["content"]
        doc_block = next(b for b in content if b.get("type") == "file")
        assert doc_block["type"] == "file"
        assert doc_block["file"]["filename"] == "report.pdf"
        assert doc_block["file"]["file_data"].startswith("data:application/pdf;base64,")
        prefix = "data:application/pdf;base64,"
        b64_part = doc_block["file"]["file_data"][len(prefix) :]
        assert base64.b64decode(b64_part) == b"%PDF-1.4 dummy"

    def test_multiple_pdf_attachments_all_appear(self) -> None:
        att2 = Attachment(
            filename="fund2.pdf", mime_type="application/pdf", data=b"%PDF-1.4 second"
        )
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT, att2], "openrouter_file"
        )
        content = result[1]["content"]
        text_blocks = [b for b in content if b["type"] == "text"]
        file_blocks = [b for b in content if b["type"] == "file"]
        assert len(text_blocks) == 1
        assert len(file_blocks) == 2

    def test_text_attachment_becomes_text_block_regardless_of_format(self) -> None:
        att = Attachment(filename="notes.md", mime_type="text/markdown", data="Hello\nWorld")
        result = build_extraction_messages(_SYSTEM, _INSTRUCTION, _KWS, [att], "openrouter_file")
        content = result[1]["content"]
        text_blocks = [b for b in content if b["type"] == "text"]
        combined = " ".join(b["text"] for b in text_blocks)
        assert "notes.md" in combined
        assert "Hello\nWorld" in combined

    def test_text_attachment_works_with_any_model_format(self) -> None:
        att = Attachment(filename="notes.md", mime_type="text/markdown", data="Hello\nWorld")
        result = build_extraction_messages(_SYSTEM, _INSTRUCTION, _KWS, [att], "openrouter_file")
        content = result[1]["content"]
        text_blocks = [b for b in content if b["type"] == "text"]
        assert len(text_blocks) == 2  # instruction block + attachment block

    def test_unknown_model_format_raises_for_binary(self) -> None:
        att = Attachment(filename="report.pdf", mime_type="application/pdf", data=b"%PDF")
        with pytest.raises(ValueError, match="Unknown model_format"):
            build_extraction_messages(_SYSTEM, _INSTRUCTION, _KWS, [att], "made_up_format")

    def test_order_instruction_then_attachment(self) -> None:
        result = build_extraction_messages(
            _SYSTEM, _INSTRUCTION, _KWS, [_PDF_ATT], "openrouter_file"
        )
        content = result[1]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "file"


# ---------------------------------------------------------------------------
# TestJsonParser
# ---------------------------------------------------------------------------


class TestJsonParser:
    def test_parse_fenced_json_block(self) -> None:
        raw = '```json\n{"a": 1}\n```'
        assert parse_extraction_response(raw) == {"a": 1}

    def test_parse_fenced_json_case_insensitive(self) -> None:
        raw = '```JSON\n{"a": 1}\n```'
        assert parse_extraction_response(raw) == {"a": 1}

    def test_parse_falls_back_to_any_fence(self) -> None:
        raw = '```\n{"a": 1}\n```'
        assert parse_extraction_response(raw) == {"a": 1}

    def test_parse_falls_back_to_brace_scan(self) -> None:
        raw = 'Here is the result: {"a": 1} and some trailing text.'
        assert parse_extraction_response(raw) == {"a": 1}

    def test_parse_handles_preamble_before_fenced_block(self) -> None:
        raw = (
            "I need to check the DataStore first... "
            "Let me look at the PDF instead.\n\n"
            '```json\n{"a": 1}\n```'
        )
        assert parse_extraction_response(raw) == {"a": 1}

    def test_parse_raises_on_total_failure(self) -> None:
        with pytest.raises(JsonParseError) as exc_info:
            parse_extraction_response("no json here at all")
        assert "Response preview" in str(exc_info.value)

    def test_parse_raises_on_malformed_json_in_fence(self) -> None:
        with pytest.raises(JsonParseError):
            parse_extraction_response("```json\n{not valid json}\n```")

    def test_parse_nested_objects(self) -> None:
        raw = '```json\n{"a": {"b": [1,2,3]}}\n```'
        assert parse_extraction_response(raw) == {"a": {"b": [1, 2, 3]}}

    def test_parse_prefers_first_fenced_block(self) -> None:
        raw = '```json\n{"first": true}\n```\n\nsome text\n\n```json\n{"second": true}\n```'
        assert parse_extraction_response(raw) == {"first": True}
