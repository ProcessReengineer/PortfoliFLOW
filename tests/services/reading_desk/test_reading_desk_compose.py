# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The pure composition of a reading (``services/reading_desk/compose.py``).

No test here calls the database; the autouse ``reset_schema`` fixture of
``tests/services/conftest.py`` still needs the test container up.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from core.exceptions import PortfoliFlowError
from core.repositories import DocumentReadingRepository
from services.reading_desk import (
    AssignmentProposal,
    ReadingNotComposable,
    UploadedDocument,
    compose_reading,
    compose_search_text,
    describe_upload,
    propose_assignment,
    propose_for_names,
    sanitise_text,
)
from services.scraper import bank_filter
from services.scraper.models import (
    FINDING_ORIGIN_ADHOC,
    FINDING_ORIGIN_PROFILE,
    Confidence,
    Finding,
    Keyword,
    KeywordType,
    NoteQuote,
    ReadingNote,
    ReportExtraction,
)

_NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
_DOCUMENT = UploadedDocument(filename="fund-x-q3.pdf", sha256="a" * 64, size_bytes=1234)
_MARKER = "marker-c0ffee"


def _finding(**overrides: Any) -> Finding:
    finding = Finding(
        keyword=Keyword(name="NAV", type=KeywordType.NUMBER),
        value="12.5",
        source="Page 12",
        confidence=Confidence.HIGH,
        key="nav",
        label="NAV",
        unit="EUR m",
        page=12,
        quote="The NAV stood at 12.5 million.",
        origin=FINDING_ORIGIN_PROFILE,
    )
    return dataclasses.replace(finding, **overrides)


def _extraction(**overrides: Any) -> ReportExtraction:
    extraction = ReportExtraction(
        filename="fund-x-q3.pdf",
        fund_name="Fund X",
        period="Q3 2026",
        findings=[_finding()],
        document_type="quarterly_report",
        profile_version=1,
        language="en",
        manager_name="Example Capital",
        document_date=date(2026, 9, 30),
        currency="EUR",
        page_count=24,
        note=ReadingNote(
            highlights=["Two exits signed"],
            quotes=[NoteQuote(text="A strong quarter.", page=3)],
            events_and_risks=["Key person change"],
            outlook="Stable",
        ),
    )
    return dataclasses.replace(extraction, **overrides)


def _compose(extraction: ReportExtraction, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "read_by": uuid4(),
        "model": "vendor/model-a",
        "prompt_version": "0f1e2d3c",
        "keywords": [],
        "now": _NOW,
    }
    arguments.update(overrides)
    return compose_reading(extraction, _DOCUMENT, **arguments)


# ---------------------------------------------------------------------------
# sanitise_text, describe_upload
# ---------------------------------------------------------------------------


def test_sanitise_text_removes_nul_and_lone_surrogates_keeps_the_rest() -> None:
    assert sanitise_text("Fu\x00nd\ud800 X\udfff") == "Fund X"
    assert sanitise_text("Größe 📈 Ökonomie") == "Größe 📈 Ökonomie"


def test_describe_upload_hashes_and_measures_the_bytes() -> None:
    data = b"%PDF-1.7 example"
    described = describe_upload("  report.pdf ", data)
    assert described == UploadedDocument(
        filename="report.pdf",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    assert described.sha256 == described.sha256.lower()


def test_describe_upload_falls_back_to_untitled() -> None:
    assert describe_upload("\x00\ud800 \udc00", b"").filename == "untitled.pdf"
    assert describe_upload("", b"").filename == "untitled.pdf"


# ---------------------------------------------------------------------------
# compose_reading
# ---------------------------------------------------------------------------


def test_compose_reading_blank_head_values_become_none() -> None:
    kwargs = _compose(
        _extraction(language=" ", fund_name="", manager_name="\t", period="", currency="  ")
    )
    assert kwargs["language"] is None
    assert kwargs["fund_name_read"] is None
    assert kwargs["manager_name_read"] is None
    assert kwargs["period_label"] is None
    assert kwargs["currency"] is None

    as_read = _compose(_extraction(fund_name=" Fund X ", currency="EUR"))
    assert as_read["fund_name_read"] == " Fund X "
    assert as_read["currency"] == "EUR"
    assert as_read["manager_name_read"] == "Example Capital"
    assert as_read["period_label"] == "Q3 2026"
    assert as_read["language"] == "en"


def test_compose_reading_passes_document_and_extraction_values() -> None:
    read_by = uuid4()
    kwargs = _compose(_extraction(), read_by=read_by)
    assert kwargs["filename"] == "fund-x-q3.pdf"
    assert kwargs["sha256"] == "a" * 64
    assert kwargs["size_bytes"] == 1234
    assert kwargs["page_count"] == 24
    assert kwargs["document_type"] == "quarterly_report"
    assert kwargs["profile_version"] == 1
    assert kwargs["document_date"] == date(2026, 9, 30)
    assert kwargs["model"] == "vendor/model-a"
    assert kwargs["prompt_version"] == "0f1e2d3c"
    assert kwargs["read_by"] == read_by
    assert kwargs["now"] == _NOW
    assert kwargs["origin"] == "upload"
    assert kwargs["case_attachment_id"] is None


def test_compose_reading_finding_dicts() -> None:
    adhoc = _finding(
        keyword=Keyword(name="DPI", type=KeywordType.PERCENTAGE),
        key="dpi",
        label="",
        value="",
        unit="",
        page=None,
        quote="",
        confidence=Confidence.NOT_FOUND,
        origin=FINDING_ORIGIN_ADHOC,
    )
    kwargs = _compose(_extraction(findings=[_finding(), adhoc]))
    assert kwargs["findings"] == [
        {
            "key": "nav",
            "label": "NAV",
            "value": "12.5",
            "type": "Number",
            "unit": "EUR m",
            "page": 12,
            "source": "Page 12",
            "quote": "The NAV stood at 12.5 million.",
            "confidence": "High",
            "origin": "profile",
        },
        {
            "key": "dpi",
            "label": "DPI",
            "value": "",
            "type": "Percentage",
            "unit": "",
            "page": None,
            "source": "Page 12",
            "quote": "",
            "confidence": "Not_Found",
            "origin": "adhoc",
        },
    ]


def test_compose_reading_without_note_gives_the_four_keys_empty() -> None:
    kwargs = _compose(_extraction(note=None))
    assert kwargs["note"] == {
        "highlights": [],
        "quotes": [],
        "events_and_risks": [],
        "outlook": "",
    }


def test_compose_reading_note_with_quotes() -> None:
    note = ReadingNote(
        highlights=["Two exits signed"],
        quotes=[NoteQuote(text="A strong quarter.", page=3), NoteQuote(text="No page.")],
        events_and_risks=["Key person change"],
        outlook="Stable",
    )
    kwargs = _compose(_extraction(note=note))
    assert kwargs["note"] == {
        "highlights": ["Two exits signed"],
        "quotes": [{"text": "A strong quarter.", "page": 3}, {"text": "No page.", "page": None}],
        "events_and_risks": ["Key person change"],
        "outlook": "Stable",
    }


def test_compose_reading_adhoc_keywords_are_sanitised() -> None:
    keywords = [
        Keyword(name="D\x00P\ud800I", type=KeywordType.NUMBER),
        Keyword(name="Fees 📈", type=KeywordType.TEXT),
    ]
    assert _compose(_extraction(), keywords=keywords)["adhoc_keywords"] == [
        {"name": "DPI", "type": "Number"},
        {"name": "Fees 📈", "type": "Text"},
    ]
    assert _compose(_extraction(), keywords=[])["adhoc_keywords"] == []


def test_compose_reading_keys_are_the_create_parameters() -> None:
    parameters = set(inspect.signature(DocumentReadingRepository.create).parameters) - {"self"}
    assert set(_compose(_extraction())) == parameters


@pytest.mark.parametrize(
    "overrides",
    [
        {"error": f"Timeout reading {_MARKER}", "document_type": ""},
        {"document_type": " "},
        {"profile_version": None},
    ],
    ids=["error", "blank-document-type", "no-profile-version"],
)
def test_reading_not_composable(overrides: dict[str, Any]) -> None:
    extraction = _extraction(
        fund_name=f"Fund {_MARKER}",
        findings=[_finding(value=_MARKER, quote=_MARKER)],
        **overrides,
    )
    with pytest.raises(ReadingNotComposable) as caught:
        _compose(extraction)
    assert isinstance(caught.value, PortfoliFlowError)
    assert isinstance(caught.value, ValueError)
    assert str(caught.value)
    assert _MARKER not in str(caught.value)


# ---------------------------------------------------------------------------
# compose_search_text
# ---------------------------------------------------------------------------


def test_compose_search_text_order_and_skipped_pieces() -> None:
    extraction = _extraction(
        manager_name=" ",
        currency="",
        findings=[
            _finding(),
            _finding(key="irr", label="IRR", value=" ", quote="Not reported here."),
            _finding(key="tvpi", label="", value="1.4x", unit="", quote=""),
        ],
        note=ReadingNote(
            highlights=["Two exits signed", ""],
            quotes=[NoteQuote(text="A strong quarter.", page=3)],
            events_and_risks=["Key person change"],
            outlook="",
        ),
    )
    assert compose_search_text(extraction, "fund-x-q3.pdf") == "\n".join(
        [
            "fund-x-q3.pdf",
            "Fund X",
            "Q3 2026",
            "2026-09-30",
            "NAV: 12.5 EUR m",
            "The NAV stood at 12.5 million.",
            "NAV: 1.4x",
            "Two exits signed",
            "A strong quarter.",
            "Key person change",
        ]
    )


def test_compose_search_text_without_note_and_date() -> None:
    text = compose_search_text(_extraction(note=None, document_date=None), "f.pdf")
    assert text.splitlines() == [
        "f.pdf",
        "Fund X",
        "Example Capital",
        "Q3 2026",
        "EUR",
        "NAV: 12.5 EUR m",
        "The NAV stood at 12.5 million.",
    ]


def test_compose_search_text_filters_a_bank_detail_made_by_joining() -> None:
    account = _finding(key="account", label="Account number", value="70872490", unit="", quote="")
    text = compose_search_text(_extraction(findings=[account]), "notice.pdf")
    assert "Fund X" in text
    assert "Two exits signed" in text
    assert "70872490" not in text
    assert bank_filter.REPLACEMENT in text


def test_compose_search_text_keeps_an_amount_line() -> None:
    share = _finding(key="nav_share", label="NAV, investor's share", value="4750000", unit="EUR")
    text = compose_search_text(_extraction(findings=[share]), "report.pdf")
    assert "NAV, investor's share: 4750000 EUR" in text.splitlines()
    assert bank_filter.REPLACEMENT not in text


def test_compose_reading_carries_the_search_text() -> None:
    extraction = _extraction()
    kwargs = _compose(extraction)
    assert kwargs["search_text"] == compose_search_text(extraction, _DOCUMENT.filename)


# ---------------------------------------------------------------------------
# propose_assignment
# ---------------------------------------------------------------------------


def test_propose_assignment_ignores_case_and_surrounding_blanks() -> None:
    investments = [("Other Fund", None), ("  Fund X II ", "Example Capital")]
    proposal = propose_assignment(_extraction(fund_name="fund x ii"), investments)
    assert proposal == AssignmentProposal(kind="investment", name="  Fund X II ")


def test_propose_assignment_fund_match_wins_over_manager_match() -> None:
    investments = [("Earlier Fund", "Example Capital"), ("Fund X", "Someone Else")]
    proposal = propose_assignment(_extraction(), investments)
    assert proposal == AssignmentProposal(kind="investment", name="Fund X")


def test_propose_assignment_manager_match_returns_the_stored_spelling() -> None:
    investments = [
        ("Fund I", None),
        ("Fund II", "EXAMPLE capital "),
        ("Fund III", "Example Capital"),
    ]
    proposal = propose_assignment(_extraction(manager_name=" example CAPITAL"), investments)
    assert proposal == AssignmentProposal(kind="manager", name="EXAMPLE capital ")


def test_propose_assignment_blank_names_never_match() -> None:
    investments = [(" ", " "), ("", "")]
    assert propose_assignment(_extraction(fund_name=" ", manager_name=""), investments) is None


def test_propose_assignment_without_a_match() -> None:
    investments = [("Fund Y", "Other Capital"), ("Fund Z", None)]
    assert propose_assignment(_extraction(), investments) is None
    assert propose_assignment(_extraction(), []) is None


def test_propose_for_names_is_the_rule_of_propose_assignment() -> None:
    """One matching rule for a fresh extraction and a stored reading (P-RD-2c1).

    For each case above, ``propose_for_names`` on the extraction's two names
    gives what ``propose_assignment`` gives; a ``None`` name never matches.
    """
    cases: list[tuple[dict[str, Any], list[tuple[str, str | None]]]] = [
        ({"fund_name": "fund x ii"}, [("Other Fund", None), ("  Fund X II ", "Example Capital")]),
        ({}, [("Earlier Fund", "Example Capital"), ("Fund X", "Someone Else")]),
        (
            {"manager_name": " example CAPITAL"},
            [("Fund I", None), ("Fund II", "EXAMPLE capital "), ("Fund III", "Example Capital")],
        ),
        ({"fund_name": " ", "manager_name": ""}, [(" ", " "), ("", "")]),
        ({}, [("Fund Y", "Other Capital"), ("Fund Z", None)]),
        ({}, []),
    ]
    for overrides, investments in cases:
        extraction = _extraction(**overrides)
        assert propose_for_names(
            extraction.fund_name, extraction.manager_name, investments
        ) == propose_assignment(extraction, investments), overrides
    assert propose_for_names(None, None, [("Fund X", "Example Capital")]) is None
    assert propose_for_names(None, "Example Capital", [("Fund X", "Example Capital")]) == (
        AssignmentProposal(kind="manager", name="Example Capital")
    )
    assert propose_for_names("Fund X", None, [("Fund X", None)]) == AssignmentProposal(
        kind="investment", name="Fund X"
    )
