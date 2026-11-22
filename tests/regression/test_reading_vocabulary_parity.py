# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guard: core's mirrored reading vocabularies equal the scraper's.

``core/`` imports nothing from the project, so
``core/repositories/document_reading_repository.py`` mirrors five vocabularies
of ``services/scraper/`` (ADR-0139 §2, §3). Nothing but this module holds them
equal: a value added on one side only means a paid reading the repository
refuses.

If this guard goes red, change both sides together.
"""

from __future__ import annotations

from dataclasses import fields

from core.repositories.document_reading_repository import (
    _NOTE_KEYS,
    _VALID_CONFIDENCES,
    _VALID_DOCUMENT_TYPES,
    _VALID_FIELD_TYPES,
    _VALID_FINDING_ORIGINS,
)
from services.scraper.models import (
    FINDING_ORIGIN_ADHOC,
    FINDING_ORIGIN_PROFILE,
    Confidence,
    KeywordType,
    ReadingNote,
)
from services.scraper.profiles import PROFILE_IDS, load_profiles


def test_document_types_equal_the_profile_ids() -> None:
    assert set(PROFILE_IDS) == _VALID_DOCUMENT_TYPES
    assert {profile.id for profile in load_profiles().values()} == _VALID_DOCUMENT_TYPES


def test_confidences_equal_the_enum() -> None:
    assert {member.value for member in Confidence} == _VALID_CONFIDENCES


def test_field_types_equal_the_enum() -> None:
    assert {member.value for member in KeywordType} == _VALID_FIELD_TYPES


def test_finding_origins_equal_the_constants() -> None:
    assert {FINDING_ORIGIN_PROFILE, FINDING_ORIGIN_ADHOC} == _VALID_FINDING_ORIGINS


def test_note_keys_equal_the_note_fields() -> None:
    assert {note_field.name for note_field in fields(ReadingNote)} == _NOTE_KEYS
