# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Mapping of the parsed model output into a reading's fields (ADR-0139 §2 to §4).

The output is untrusted: it is a model's answer to a document somebody else
wrote. It is cleaned and passed through the bank-detail filter before any
field is read, every level is type-checked, and only what was asked for is
taken. A value of the wrong shape counts as absent. No log line carries
anything the model returned.

Pure: standard library and ``services.scraper`` only.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from services.scraper.bank_filter import asks_for_bank_details, remove_bank_details
from services.scraper.models import (
    FINDING_ORIGIN_ADHOC,
    FINDING_ORIGIN_PROFILE,
    Confidence,
    Finding,
    Keyword,
    NoteQuote,
    ReadingNote,
    ReportExtraction,
)
from services.scraper.profiles import Profile, ProfileField

logger = logging.getLogger(__name__)

#: The profile a document is read as when the model answers no offered id.
FALLBACK_PROFILE_ID = "other"
#: The prefix of every ad-hoc finding's key.
ADHOC_KEY_PREFIX = "adhoc_"
#: The source of a finding whose keyword asks for a bank detail.
BANK_KEYWORD_SOURCE = "Not read: bank details are never extracted."

_EMPTY_SLUG = "keyword"
# What PostgreSQL cannot store and UTF-8 cannot encode.
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")
# date.fromisoformat alone would also accept 20240930 and 2024-W39-1.
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_NOT_FOUND_LITERALS = frozenset({"not_found", "not found"})


def clean_parsed_output(value: Any) -> Any:
    """Make parsed model output storable and free of bank details.

    Strings lose NULs and lone surrogates and then pass the bank-detail
    filter; a float that is not finite becomes ``None``. The order matters:
    a NUL inside an IBAN must not hide it from the filter. Keys are left
    alone.

    Args:
        value: The parsed model output, or any part of it.

    Returns:
        A new structure of the same shape, cleaned.
    """
    if isinstance(value, str):
        return remove_bank_details(_UNSTORABLE.sub("", value))
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: clean_parsed_output(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_parsed_output(item) for item in value]
    return value


def adhoc_keys(keywords: Sequence[Keyword]) -> list[str]:
    """Return one unique key per ad-hoc keyword, in order.

    A key is :data:`ADHOC_KEY_PREFIX` plus an ASCII slug of the keyword's
    name; a key that was already given gets ``_2``, ``_3``, … appended.

    Args:
        keywords: The run's ad-hoc keywords.

    Returns:
        The keys, one per keyword.
    """
    keys: list[str] = []
    given: set[str] = set()
    for keyword in keywords:
        folded = unicodedata.normalize("NFKD", keyword.name).encode("ascii", "ignore").decode()
        slug = _NOT_SLUG.sub("_", folded.lower()).strip("_") or _EMPTY_SLUG
        base = key = f"{ADHOC_KEY_PREFIX}{slug}"
        number = 1
        while key in given:
            number += 1
            key = f"{base}_{number}"
        given.add(key)
        keys.append(key)
    return keys


def apply_parsed_output(
    extraction: ReportExtraction,
    parsed: dict[str, Any],
    *,
    profiles: Mapping[str, Profile],
    preselected: Profile | None,
    keywords: Sequence[Keyword],
) -> None:
    """Fill ``extraction`` in place from the parsed model output.

    Args:
        extraction: The extraction to fill.
        parsed: The parsed output, as the parser returned it (not yet cleaned).
        profiles: The profiles by id; used when none was preselected.
        preselected: The preselected profile, which wins over the model's
            answer, or ``None`` if the model classified the document.
        keywords: All keywords of the run, including those the model was not
            asked for.
    """
    output = clean_parsed_output(parsed)
    filename = extraction.filename

    profile = preselected or _classified_profile(output.get("document_type"), profiles, filename)
    extraction.document_type = profile.id
    extraction.profile_version = profile.version

    extraction.fund_name = _text(output.get("fund_name"))
    extraction.manager_name = _text(output.get("manager_name"))
    extraction.period = _text(output.get("period"))
    extraction.currency = _text(output.get("currency"))
    extraction.language = _text(output.get("language"))
    extraction.document_date = _date(output.get("document_date"))
    extraction.page_count = _positive_int(output.get("page_count"))

    extraction.findings = [
        *_profile_findings(
            profile,
            _object(output, "fields", filename),
            currency=extraction.currency,
            filename=filename,
        ),
        *_adhoc_findings(keywords, _object(output, "findings", filename), filename=filename),
    ]
    extraction.note = _note(output.get("note"))


def _classified_profile(answer: Any, profiles: Mapping[str, Profile], filename: str) -> Profile:
    # The type first: parsed JSON may be unhashable.
    if isinstance(answer, str) and answer in profiles:
        return profiles[answer]
    logger.warning(
        "Scraper mapping: no offered document type returned for %s; read as %r.",
        filename,
        FALLBACK_PROFILE_ID,
    )
    return profiles[FALLBACK_PROFILE_ID]


def _object(output: dict[str, Any], key: str, filename: str) -> dict[str, Any]:
    value = output.get(key)
    if isinstance(value, dict):
        return value
    logger.warning("Scraper mapping: '%s' is not an object for %s; read as empty.", key, filename)
    return {}


def _profile_findings(
    profile: Profile, entries: dict[str, Any], *, currency: str, filename: str
) -> list[Finding]:
    return [
        _finding(
            entries.get(field.key),
            keyword=Keyword(name=field.label, type=field.type),
            key=field.key,
            origin=FINDING_ORIGIN_PROFILE,
            default_unit=_default_unit(field, currency),
            filename=filename,
        )
        for field in profile.fields
    ]


def _default_unit(field: ProfileField, currency: str) -> str:
    return currency if field.unit == "currency" else field.unit or ""


def _adhoc_findings(
    keywords: Sequence[Keyword], entries: dict[str, Any], *, filename: str
) -> list[Finding]:
    findings: list[Finding] = []
    for keyword, key in zip(keywords, adhoc_keys(keywords), strict=True):
        refused = asks_for_bank_details(keyword.name)
        finding = _finding(
            None if refused else entries.get(keyword.name),
            keyword=keyword,
            key=key,
            origin=FINDING_ORIGIN_ADHOC,
            default_unit="",
            filename=filename,
        )
        if refused:
            finding.source = BANK_KEYWORD_SOURCE
        findings.append(finding)
    return findings


def _finding(
    entry: Any, *, keyword: Keyword, key: str, origin: str, default_unit: str, filename: str
) -> Finding:
    if not isinstance(entry, dict):
        entry = {}

    raw = entry.get("value")
    value = "\n".join(_texts(raw)) if isinstance(raw, list) else _text(raw)
    if value.lower() in _NOT_FOUND_LITERALS:
        value = ""

    confidence = _confidence(entry.get("confidence"))
    if confidence is None:
        if entry.get("confidence") is not None:
            logger.warning(
                "Scraper mapping: unknown confidence for %s in %s; read as Not_Found.",
                key,
                filename,
            )
        confidence = Confidence.NOT_FOUND

    return Finding(
        keyword=keyword,
        value=value,
        source=_text(entry.get("source")),
        confidence=confidence if value else Confidence.NOT_FOUND,
        key=key,
        label=keyword.name,
        unit=_text(entry.get("unit")) or default_unit,
        page=_positive_int(entry.get("page")),
        quote=_text(entry.get("quote")),
        origin=origin,
    )


def _note(value: Any) -> ReadingNote:
    if not isinstance(value, dict):
        return ReadingNote()
    quotes = (_quote(item) for item in _as_list(value.get("quotes")))
    return ReadingNote(
        highlights=_texts(value.get("highlights")),
        quotes=[quote for quote in quotes if quote.text],
        events_and_risks=_texts(value.get("events_and_risks")),
        outlook=_text(value.get("outlook")),
    )


def _quote(item: Any) -> NoteQuote:
    if isinstance(item, dict):
        return NoteQuote(text=_text(item.get("text")), page=_positive_int(item.get("page")))
    return NoteQuote(text=_text(item))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _texts(value: Any) -> list[str]:
    return [text for item in _as_list(value) if (text := _text(item))]


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):  # before int, of which it is a subclass
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int | float):
        return str(value)
    return ""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):  # before int, of which it is a subclass
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            number = int(value)
        except ValueError:  # longer than Python's limit for int() of a string
            return None
    else:
        return None
    return number if number >= 1 else None


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:  # no such calendar day
        return None


def _confidence(value: Any) -> Confidence | None:
    if not isinstance(value, str):
        return None
    try:
        return Confidence(value)
    except ValueError:
        return None
