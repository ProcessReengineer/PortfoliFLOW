# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The mapping of the parsed model output (ADR-0139 §2 to §4).

Direct tests of :mod:`services.scraper.mapping` against the shipped profiles.
The IBAN is the published example IBAN of Germany; the account numbers are
invented.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import math
from collections.abc import Iterator, Sequence
from datetime import date
from typing import Any

import pytest

from services.scraper.bank_filter import REPLACEMENT
from services.scraper.mapping import (
    ADHOC_KEY_PREFIX,
    BANK_KEYWORD_SOURCE,
    FALLBACK_PROFILE_ID,
    adhoc_keys,
    apply_parsed_output,
    clean_parsed_output,
)
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
from services.scraper.profiles import PROFILE_IDS, Profile, load_profiles

_IBAN = "DE89 3704 0044 0532 0130 00"
_ACCOUNT = "0532013000"
_MARKER = "ZEBRAQUARTZ"
_PROFILES = load_profiles()
_QUARTERLY = _PROFILES["quarterly_report"]
_KEYWORD = Keyword("Exit pipeline", KeywordType.TEXT)


def _map(
    parsed: dict[str, Any],
    *,
    keywords: Sequence[Keyword] = (),
    preselected: Profile | None = None,
) -> ReportExtraction:
    extraction = ReportExtraction(filename="document.pdf")
    apply_parsed_output(
        extraction, parsed, profiles=_PROFILES, preselected=preselected, keywords=keywords
    )
    return extraction


def _adhoc(entry: Any) -> Finding:
    """Map one entry as the answer to one ad-hoc keyword."""
    return _map({"findings": {_KEYWORD.name: entry}}, keywords=[_KEYWORD]).findings[0]


def _quarterly(**parts: Any) -> dict[str, Finding]:
    """Map a quarterly report and return its findings by key."""
    result = _map({"document_type": "quarterly_report", **parts})
    return {finding.key: finding for finding in result.findings}


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


# ---------------------------------------------------------------------------
# M-1 Cleaning
# ---------------------------------------------------------------------------


def test_cleaning_drops_nul_and_lone_surrogates() -> None:
    parsed = json.loads('{"a": "x\\u0000y\\ud800z\\udfff", "b": ["\\ud83d\\ude00"]}')

    cleaned = clean_parsed_output(parsed)

    # A valid pair is one character and stays.
    assert cleaned == {"a": "xyz", "b": ["\U0001f600"]}
    json.dumps(cleaned, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("hidden", ["\x00", "\ud800"])
def test_cleaning_runs_before_the_filter(hidden: str) -> None:
    assert clean_parsed_output(f"DE89370400440{hidden}532013000") == REPLACEMENT


def test_cleaning_turns_non_finite_floats_into_none() -> None:
    parsed = json.loads('{"a": NaN, "b": [Infinity, -Infinity, 1.5], "c": {"d": Infinity}}')
    assert math.isnan(parsed["a"])

    assert clean_parsed_output(parsed) == {"a": None, "b": [None, None, 1.5], "c": {"d": None}}


@pytest.mark.parametrize("value", [None, True, False, 0, -7, 12.5])
def test_cleaning_returns_other_values_as_they_are(value: Any) -> None:
    assert clean_parsed_output(value) is value


def test_cleaning_filters_nested_strings_and_leaves_the_input_alone() -> None:
    parsed = {"note": {"highlights": ["fine", {"deep": [f"Pay to {_IBAN}"]}]}}
    before = copy.deepcopy(parsed)

    cleaned = clean_parsed_output(parsed)

    assert cleaned == {"note": {"highlights": ["fine", {"deep": [f"Pay to {REPLACEMENT}"]}]}}
    assert parsed == before


def test_cleaning_leaves_keys_alone() -> None:
    assert clean_parsed_output({_IBAN: "x"}) == {_IBAN: "x"}


def test_mapping_leaves_the_parsed_output_alone() -> None:
    parsed = {"fund_name": f" {_IBAN} ", "fields": {}, "findings": {}}
    before = copy.deepcopy(parsed)

    _map(parsed)

    assert parsed == before


# ---------------------------------------------------------------------------
# M-2 Document type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_id", PROFILE_IDS)
def test_each_offered_id_is_honoured(profile_id: str) -> None:
    result = _map({"document_type": profile_id})

    assert result.document_type == profile_id
    assert result.profile_version == _PROFILES[profile_id].version
    assert len(result.findings) == len(_PROFILES[profile_id].fields)


@pytest.mark.parametrize(
    "answer",
    ["dd_report", "", None, 5, ["quarterly_report"], {"id": "pitch_deck"}, " pitch_deck"],
)
def test_any_other_answer_reads_as_other(answer: Any) -> None:
    result = _map({"document_type": answer})

    assert result.document_type == FALLBACK_PROFILE_ID == "other"
    assert result.profile_version == _PROFILES["other"].version


def test_a_missing_answer_reads_as_other() -> None:
    assert _map({}).document_type == "other"


def test_the_fallback_warning_does_not_repeat_the_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        _map({"document_type": _MARKER, "fields": {}, "findings": {}})

    assert "document.pdf" in caplog.text
    assert _MARKER not in caplog.text


@pytest.mark.parametrize("answer", ["quarterly_report", "nonsense", None])
def test_a_preselected_profile_wins(answer: Any) -> None:
    preselected = _PROFILES["capital_notice"]

    result = _map({"document_type": answer}, preselected=preselected)

    assert result.document_type == "capital_notice"
    assert [f.key for f in result.findings] == [f.key for f in preselected.fields]


def test_the_version_is_that_of_the_chosen_profile() -> None:
    preselected = dataclasses.replace(_QUARTERLY, version=7)

    assert _map({}, preselected=preselected).profile_version == 7


# ---------------------------------------------------------------------------
# M-3 Head, M-4 Text, M-5 Positive integer
# ---------------------------------------------------------------------------


def test_the_head() -> None:
    result = _map(
        {
            "fund_name": "  Example Fund I ",
            "manager_name": "Example Capital\n",
            "document_date": "2024-09-30",
            "period": "Q3 2024",
            "currency": " EUR",
            "language": "en",
            "page_count": 42,
        }
    )

    assert result.fund_name == "Example Fund I"
    assert result.manager_name == "Example Capital"
    assert result.document_date == date(2024, 9, 30)
    assert result.period == "Q3 2024"
    assert result.currency == "EUR"
    assert result.language == "en"
    assert result.page_count == 42
    assert result.error is None


def test_an_absent_head_is_empty() -> None:
    result = _map({})

    assert result.fund_name == result.manager_name == result.period == ""
    assert result.currency == result.language == ""
    assert result.document_date is None
    assert result.page_count is None


@pytest.mark.parametrize(
    "value",
    [
        "2024-02-31",
        "0000-01-01",
        "30.09.2024",
        "20240930",
        "2024-W39-1",
        "2024-09-30\n",
        "2024-09-30T10:00:00",
        "",
        None,
        20240930,
        ["2024-09-30"],
    ],
)
def test_no_document_date(value: Any) -> None:
    assert _map({"document_date": value}).document_date is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (42, 42),
        (42.0, 42),
        ("42", 42),
        (True, None),
        (False, None),
        (0, None),
        ("0", None),
        (-3, None),
        ("-3", None),
        (42.5, None),
        ("42.0", None),
        ("²", None),  # a superscript two: isdigit(), but not ASCII
        ("٤٢", None),  # 42 in Arabic-Indic digits
        ("9" * 5000, None),  # beyond what int() converts
        (math.inf, None),
        (None, None),
        ([42], None),
    ],
)
def test_positive_integers(value: Any, expected: int | None) -> None:
    assert _map({"page_count": value}).page_count == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" text ", "text"),
        (12, "12"),
        (0, "0"),
        (-4750000, "-4750000"),
        (12500000.0, "12500000"),
        (1e16, "10000000000000000"),
        (1.45, "1.45"),
        (math.nan, ""),
        (True, ""),
        (False, ""),
        (None, ""),
        ({"name": "x"}, ""),
        (["x"], ""),
    ],
)
def test_text(value: Any, expected: str) -> None:
    assert _map({"fund_name": value}).fund_name == expected


# ---------------------------------------------------------------------------
# M-6 One finding
# ---------------------------------------------------------------------------


def test_a_number_value() -> None:
    finding = _adhoc({"value": 1250000, "confidence": "High"})

    assert finding.value == "1250000"
    assert finding.confidence is Confidence.HIGH


def test_an_integral_float_value() -> None:
    assert _adhoc({"value": 12500000.0, "confidence": "Medium"}).value == "12500000"


def test_a_list_value() -> None:
    finding = _adhoc({"value": [" A ", "", None, 7, {"x": 1}, "B"], "confidence": "Low"})

    assert finding.value == "A\n7\nB"
    assert finding.confidence is Confidence.LOW


@pytest.mark.parametrize(
    "value", ["Not_Found", "not_found", "NOT FOUND", " not found ", ["Not_Found"]]
)
def test_the_not_found_literal_is_an_empty_value(value: Any) -> None:
    finding = _adhoc({"value": value, "confidence": "High"})

    assert finding.value == ""
    assert finding.confidence is Confidence.NOT_FOUND


def test_not_found_inside_a_sentence_is_a_value() -> None:
    assert _adhoc({"value": "A buyer was not found"}).value == "A buyer was not found"


@pytest.mark.parametrize("value", ["", "   ", None, [], ["", " "], {"v": 1}, True])
def test_an_empty_value_is_not_found_whatever_the_confidence(value: Any) -> None:
    finding = _adhoc({"value": value, "confidence": "High"})

    assert finding.value == ""
    assert finding.confidence is Confidence.NOT_FOUND


@pytest.mark.parametrize("confidence", ["Certain", "high", "", 3, ["High"], None])
def test_an_unknown_confidence_keeps_the_value(confidence: Any) -> None:
    finding = _adhoc({"value": "99000000", "confidence": confidence})

    assert finding.value == "99000000"
    assert finding.confidence is Confidence.NOT_FOUND


def test_an_unknown_confidence_is_logged_without_its_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        _adhoc({"value": "99000000", "confidence": _MARKER})

    assert "unknown confidence" in caplog.text
    assert "document.pdf" in caplog.text
    assert _MARKER not in caplog.text
    assert "99000000" not in caplog.text


@pytest.mark.parametrize("entry", ["12500000", 5, None, ["High"], True])
def test_an_entry_that_is_not_an_object_is_empty(entry: Any) -> None:
    finding = _adhoc(entry)

    assert (finding.value, finding.source, finding.quote, finding.unit) == ("", "", "", "")
    assert finding.page is None
    assert finding.confidence is Confidence.NOT_FOUND


def test_source_quote_page_and_unit() -> None:
    finding = _adhoc(
        {"value": "5", "source": " Page 7 ", "quote": " five exits ", "page": "7", "unit": " x "}
    )

    assert finding.source == "Page 7"
    assert finding.quote == "five exits"
    assert finding.page == 7
    assert finding.unit == "x"


# ---------------------------------------------------------------------------
# M-7 Ad-hoc keys
# ---------------------------------------------------------------------------


def _keys(*names: str) -> list[str]:
    return adhoc_keys([Keyword(name, KeywordType.TEXT) for name in names])


def test_adhoc_keys_example() -> None:
    assert _keys("Net IRR", "net-irr", "Ausschüttung (EUR)", "???", "Net IRR") == [
        "adhoc_net_irr",
        "adhoc_net_irr_2",
        "adhoc_ausschuttung_eur",
        "adhoc_keyword",
        "adhoc_net_irr_3",
    ]


def test_adhoc_keys_stay_unique_beside_a_name_that_ends_in_a_number() -> None:
    assert _keys("A", "A", "A 2", "A") == ["adhoc_a", "adhoc_a_2", "adhoc_a_2_2", "adhoc_a_3"]


def test_adhoc_keys_of_no_keyword() -> None:
    assert _keys() == []


def test_no_shipped_field_key_starts_with_the_adhoc_prefix() -> None:
    keys = [field.key for profile in _PROFILES.values() for field in profile.fields]
    assert keys
    assert not [key for key in keys if key.startswith(ADHOC_KEY_PREFIX)]


# ---------------------------------------------------------------------------
# M-8 Findings
# ---------------------------------------------------------------------------


def test_profile_fields_first_then_the_keywords() -> None:
    keywords = [Keyword("Net IRR", KeywordType.PERCENTAGE), _KEYWORD]
    parsed = {
        "document_type": "quarterly_report",
        "currency": "USD",
        # Not in file order, and with an entry nobody asked for.
        "fields": {
            "net_irr": {"value": "14.2%", "confidence": "High"},
            "nav_lp": {"value": "12500000", "page": 12, "confidence": "Medium"},
            "invented": {"value": _MARKER, "confidence": "High"},
        },
        "findings": {
            "Exit pipeline": {"value": "Two", "confidence": "Low"},
            "Net IRR": {"value": "14.1%", "confidence": "High"},
            "Invented": {"value": _MARKER, "confidence": "High"},
        },
    }

    result = _map(parsed, keywords=keywords)

    assert len(result.findings) == 17 + 2
    fields, adhoc = result.findings[:17], result.findings[17:]
    for finding, field in zip(fields, _QUARTERLY.fields, strict=True):
        assert finding.origin == FINDING_ORIGIN_PROFILE
        assert finding.key == field.key
        assert finding.label == field.label
        assert finding.keyword == Keyword(name=field.label, type=field.type)
        if field.key not in parsed["fields"]:
            assert finding.value == ""
            assert finding.confidence is Confidence.NOT_FOUND
    for finding, keyword in zip(adhoc, keywords, strict=True):
        assert finding.origin == FINDING_ORIGIN_ADHOC
        assert finding.label == keyword.name
        assert finding.keyword == keyword
    assert [f.key for f in adhoc] == ["adhoc_net_irr", "adhoc_exit_pipeline"]
    assert [f.value for f in adhoc] == ["14.1%", "Two"]
    assert {f.key: f.value for f in fields if f.value} == {
        "nav_lp": "12500000",
        "net_irr": "14.2%",
    }
    assert _MARKER not in " ".join(_strings(dataclasses.asdict(result)))


def test_default_units() -> None:
    findings = _quarterly(currency="CHF")

    assert findings["nav_lp"].unit == "CHF"
    assert findings["tvpi"].unit == "x"
    assert findings["net_irr"].unit == "%"


def test_a_currency_unit_without_a_currency_is_empty() -> None:
    assert _quarterly()["nav_lp"].unit == ""


def test_a_unit_from_the_model_wins() -> None:
    findings = _quarterly(
        currency="CHF",
        fields={"nav_lp": {"value": "5", "unit": "USD"}, "nav_fund": {"value": "5", "unit": " "}},
    )

    assert findings["nav_lp"].unit == "USD"
    assert findings["nav_fund"].unit == "CHF"


def test_an_adhoc_keyword_has_no_default_unit() -> None:
    result = _map({"currency": "CHF"}, keywords=[Keyword("NAV", KeywordType.NUMBER)])

    assert result.findings[0].unit == ""


def test_containers_that_are_no_objects_count_as_empty(caplog: pytest.LogCaptureFixture) -> None:
    parsed = {
        "document_type": "quarterly_report",
        "fields": [{"key": "nav_lp", "value": _MARKER}],
        "findings": _MARKER,
    }

    with caplog.at_level(logging.DEBUG):
        result = _map(parsed, keywords=[_KEYWORD])

    assert len(result.findings) == 17 + 1
    assert all(f.value == "" for f in result.findings)
    assert "'fields'" in caplog.text
    assert "'findings'" in caplog.text
    assert _MARKER not in caplog.text


def test_a_bank_keyword_is_never_looked_up() -> None:
    entry = {"value": _MARKER, "unit": "EUR", "page": 3, "quote": "q", "confidence": "High"}

    result = _map({"findings": {"IBAN": entry}}, keywords=[Keyword("IBAN", KeywordType.TEXT)])

    assert result.findings == [
        Finding(
            keyword=Keyword("IBAN", KeywordType.TEXT),
            value="",
            source=BANK_KEYWORD_SOURCE,
            confidence=Confidence.NOT_FOUND,
            key="adhoc_iban",
            label="IBAN",
            origin=FINDING_ORIGIN_ADHOC,
        )
    ]


# ---------------------------------------------------------------------------
# M-9 Reading note
# ---------------------------------------------------------------------------


def test_the_note() -> None:
    result = _map(
        {
            "note": {
                "highlights": [" One. ", "", "Two.", None, 3],
                "quotes": [
                    {"text": " as read ", "page": "4"},
                    "bare",
                    {"page": 9},
                    {"text": " ", "page": 2},
                    None,
                ],
                "events_and_risks": "One risk.",
                "outlook": None,
            }
        }
    )

    assert result.note == ReadingNote(
        highlights=["One.", "Two.", "3"],
        quotes=[NoteQuote("as read", 4), NoteQuote("bare")],
        events_and_risks=["One risk."],
        outlook="",
    )


def test_the_outlook() -> None:
    assert _map({"note": {"outlook": " Cautious. "}}).note == ReadingNote(outlook="Cautious.")


def test_a_single_quote_counts_as_a_list_of_one() -> None:
    result = _map({"note": {"quotes": {"text": "alone", "page": 2}}})

    assert result.note == ReadingNote(quotes=[NoteQuote("alone", 2)])


@pytest.mark.parametrize("note", [["a highlight"], "a note", None, 5])
def test_a_note_that_is_not_an_object_is_empty(note: Any) -> None:
    assert _map({"note": note}).note == ReadingNote()


def test_there_is_always_a_note() -> None:
    assert _map({}).note == ReadingNote()


# ---------------------------------------------------------------------------
# Bank details
# ---------------------------------------------------------------------------


def test_no_bank_detail_reaches_any_string_of_the_extraction() -> None:
    bank = f"IBAN {_IBAN}, account number {_ACCOUNT}"
    parsed = {
        "document_type": "capital_notice",
        "manager_name": f"Example Capital ({bank})",
        "fields": {
            "notice_kind": {"value": f"call of 1100000 to {bank}", "confidence": "High"},
            "amount_lp": {
                "value": "4750000",
                "quote": f"Pay EUR 4,750,000 to {bank}",
                "source": f"Page 2, 3,300,000 to {bank}",
                "confidence": "High",
            },
        },
        "findings": {_KEYWORD.name: {"value": f"2200000 via {bank}", "confidence": "Low"}},
        "note": {
            "highlights": [f"A call of 5500000 to {bank}."],
            "quotes": [{"text": f"6600000 is payable to {bank}", "page": 2}],
        },
    }

    result = _map(parsed, keywords=[_KEYWORD])

    strings = " | ".join(_strings(dataclasses.asdict(result)))
    for detail in (_IBAN, _IBAN.replace(" ", ""), _ACCOUNT, "3704", "0532"):
        assert detail not in strings
    for amount in ("1100000", "4,750,000", "4750000", "3,300,000", "2200000", "5500000", "6600000"):
        assert amount in strings
    assert "Example Capital" in result.manager_name


# ---------------------------------------------------------------------------
# P-RD-1b3: joined list values, page bounds, the percentage unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (["IBAN", "DE89 3704 0044", "0532 0130 00"], f"IBAN\n{REPLACEMENT}"),
        (["Account number:", _ACCOUNT], f"Account number:\n{REPLACEMENT}"),
    ],
)
def test_a_list_value_that_joins_to_a_bank_detail_is_filtered(
    items: list[str], expected: str
) -> None:
    # Each item alone passes the filter unchanged.
    assert clean_parsed_output(items) == items

    finding = _adhoc({"value": items, "confidence": "High"})

    assert finding.value == expected
    for digits in ("3704", "0532"):
        assert digits not in finding.value


def test_a_list_of_amounts_is_joined_unchanged() -> None:
    items = ["12 500 000", "4750000", "2024-09-30", "14.2%"]

    finding = _adhoc({"value": items, "confidence": "High"})

    assert finding.value == "\n".join(items)


_PAGES: list[tuple[Any, int | None]] = [(100000, 100000), (100001, None), (10**30, None)]


@pytest.mark.parametrize(("value", "expected"), _PAGES)
def test_the_page_count_has_an_upper_bound(value: Any, expected: int | None) -> None:
    assert _map({"page_count": value}).page_count == expected
    assert _map({"page_count": str(value)}).page_count == expected


@pytest.mark.parametrize(("value", "expected"), _PAGES)
def test_the_page_of_a_finding_has_an_upper_bound(value: Any, expected: int | None) -> None:
    assert _adhoc({"value": "5", "page": value}).page == expected
    assert _adhoc({"value": "5", "page": str(value)}).page == expected


@pytest.mark.parametrize(("value", "expected"), _PAGES)
def test_the_page_of_a_note_quote_has_an_upper_bound(value: Any, expected: int | None) -> None:
    result = _map({"note": {"quotes": [{"text": "q", "page": value}]}})

    assert result.note.quotes == [NoteQuote("q", expected)]


def test_a_percentage_field_has_the_unit_percent() -> None:
    findings = _quarterly(
        fields={"net_irr": {"value": "14.2", "confidence": "High"}},
    )

    assert findings["net_irr"].unit == "%"


def test_a_unit_from_the_model_wins_over_percent_for_a_field() -> None:
    findings = _quarterly(
        fields={"net_irr": {"value": "1420", "unit": "bp", "confidence": "High"}},
    )

    assert findings["net_irr"].unit == "bp"


def test_a_percentage_keyword_has_the_unit_percent() -> None:
    keyword = Keyword("Net IRR", KeywordType.PERCENTAGE)
    entries = {"Net IRR": {"value": "14.2", "confidence": "High"}}

    result = _map({"findings": entries}, keywords=[keyword])

    assert result.findings[0].unit == "%"


def test_a_unit_from_the_model_wins_over_percent_for_a_keyword() -> None:
    keyword = Keyword("Net IRR", KeywordType.PERCENTAGE)
    entries = {"Net IRR": {"value": "1420", "unit": "bp", "confidence": "High"}}

    result = _map({"findings": entries}, keywords=[keyword])

    assert result.findings[0].unit == "bp"
