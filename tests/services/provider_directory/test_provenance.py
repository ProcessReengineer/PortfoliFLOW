# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""What an instance may say about the directory it holds (SB-3b).

Provenance answers a question an operator will actually ask — *where did this
list of counterparties come from, and may I act on it?* — so the assertions
below are about honesty rather than convenience: the version and key come
from the signed bytes, the validity flag says what verification actually
concluded, and the facts an expired copy cannot supply are ``None`` instead
of guessed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import pytest

from services.provider_directory.cache import (
    CacheMeta,
    META_SCHEMA_VERSION,
    read_cache,
    write_cache,
)
from services.provider_directory.fetch import DIRECTORY_URL
from services.provider_directory.provenance import provenance_from_cache, provenance_to_dict
from tests.services.provider_directory.conftest import TEST_KEY_ID, TEST_SUCCESSOR_KEY_ID

_ETAG: Final[str] = '"v1-abc123"'

DocumentFactory = Callable[..., dict[str, object]]
SignFactory = Callable[[Mapping[str, object], bytes], tuple[bytes, bytes]]


def _noon_utc(iso_day: object, *, plus_days: int = 0) -> datetime:
    """Noon UTC on a day stated by the document under test, never a literal."""
    assert isinstance(iso_day, str)
    return datetime.fromisoformat(iso_day).replace(hour=12, tzinfo=timezone.utc) + timedelta(
        days=plus_days
    )


def _store(
    cache_root: Path,
    *,
    document_bytes: bytes,
    signature_file: bytes,
    fetched_at: datetime,
    version: int = 1,
    key_id: str = TEST_KEY_ID,
) -> None:
    """Write a copy with a sidecar describing it."""
    write_cache(
        cache_root,
        document_bytes=document_bytes,
        signature_file=signature_file,
        meta=CacheMeta(
            schema_version=META_SCHEMA_VERSION,
            directory_version=version,
            publishing_key_id=key_id,
            fetched_at=fetched_at,
            etag=_ETAG,
            source_url=DIRECTORY_URL,
        ),
    )


def test_a_verified_copy_is_described_from_its_verification(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
    fixture_document: dict[str, object],
) -> None:
    """Dates, count and notices come from the one reading of the bytes that was proved."""
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    _store(cache_root, document_bytes=document_bytes, signature_file=signature_file, fetched_at=now)
    cached = read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID)
    assert cached is not None

    provenance = provenance_from_cache(cached, current_key_id=TEST_KEY_ID)

    assert provenance.valid is True
    assert provenance.directory_version == 1
    assert provenance.publishing_key_id == TEST_KEY_ID
    assert provenance.issued_at == date.fromisoformat(str(fixture_document["issued_at"]))
    assert provenance.valid_until == date.fromisoformat(str(fixture_document["valid_until"]))
    assert provenance.provider_count == 2
    assert provenance.successor_in_use is False
    assert provenance.announced_successor is None
    assert provenance.fetched_at == now
    assert provenance.etag == _ETAG
    assert provenance.source_url == DIRECTORY_URL


def test_an_announced_successor_is_reported_against_the_shipped_ring(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    successor_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """An announcement naming a key already in the ring is a routine rotation notice."""
    document = make_document(
        successor_key={
            "publishing_key_id": TEST_SUCCESSOR_KEY_ID,
            "public_key": successor_keypair[1].hex(),
            "valid_from": "2027-01-01",
        }
    )
    document_bytes, signature_file = make_signed(document, throwaway_keypair[0])
    _store(cache_root, document_bytes=document_bytes, signature_file=signature_file, fetched_at=now)
    cached = read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID)
    assert cached is not None

    provenance = provenance_from_cache(cached, current_key_id=TEST_KEY_ID)

    assert provenance.announced_successor == "in_ring"
    assert provenance.successor_in_use is False


def test_a_copy_signed_by_the_successor_reports_the_rotation_as_done(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    successor_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """Signed by a ring key other than the current one: rotation has happened."""
    document = make_document(publishing_key_id=TEST_SUCCESSOR_KEY_ID)
    document_bytes, signature_file = make_signed(document, successor_keypair[0])
    _store(
        cache_root,
        document_bytes=document_bytes,
        signature_file=signature_file,
        fetched_at=now,
        key_id=TEST_SUCCESSOR_KEY_ID,
    )
    cached = read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID)
    assert cached is not None

    provenance = provenance_from_cache(cached, current_key_id=TEST_KEY_ID)

    assert provenance.publishing_key_id == TEST_SUCCESSOR_KEY_ID
    assert provenance.successor_in_use is True


def test_an_expired_copy_keeps_its_version_dates_and_count(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
    fixture_document: dict[str, object],
) -> None:
    """Stale, and still able to say exactly what it is.

    ``announced_successor`` is ``None`` rather than read from the bytes: the
    announcement was never verified as a shape, and an unparsed claim is not
    a notice worth repeating.
    """
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    _store(cache_root, document_bytes=document_bytes, signature_file=signature_file, fetched_at=now)
    after_expiry = _noon_utc(fixture_document["valid_until"], plus_days=1)
    cached = read_cache(cache_root, now=after_expiry, ring=test_ring, current_key_id=TEST_KEY_ID)
    assert cached is not None

    provenance = provenance_from_cache(cached, current_key_id=TEST_KEY_ID)

    assert provenance.valid is False
    assert provenance.directory_version == 1
    assert provenance.publishing_key_id == TEST_KEY_ID
    assert provenance.issued_at == date.fromisoformat(str(fixture_document["issued_at"]))
    assert provenance.valid_until == date.fromisoformat(str(fixture_document["valid_until"]))
    assert provenance.provider_count == 2
    assert provenance.announced_successor is None
    assert provenance.successor_in_use is False


def test_an_expired_copy_signed_by_the_successor_still_reports_the_rotation(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    successor_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
    fixture_document: dict[str, object],
) -> None:
    """Read from the signed bytes rather than from a verification there isn't."""
    document = make_document(publishing_key_id=TEST_SUCCESSOR_KEY_ID)
    document_bytes, signature_file = make_signed(document, successor_keypair[0])
    _store(
        cache_root,
        document_bytes=document_bytes,
        signature_file=signature_file,
        fetched_at=now,
        key_id=TEST_SUCCESSOR_KEY_ID,
    )
    after_expiry = _noon_utc(fixture_document["valid_until"], plus_days=1)
    cached = read_cache(cache_root, now=after_expiry, ring=test_ring, current_key_id=TEST_KEY_ID)
    assert cached is not None

    provenance = provenance_from_cache(cached, current_key_id=TEST_KEY_ID)

    assert provenance.valid is False
    assert provenance.successor_in_use is True


@pytest.mark.parametrize("expired", [False, True])
def test_provenance_renders_to_json_with_iso_dates(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
    fixture_document: dict[str, object],
    expired: bool,
) -> None:
    """SB-6 renders this mapping, so it must survive ``json.dumps`` unhelped."""
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    _store(cache_root, document_bytes=document_bytes, signature_file=signature_file, fetched_at=now)
    moment = _noon_utc(fixture_document["valid_until"], plus_days=1) if expired else now
    cached = read_cache(cache_root, now=moment, ring=test_ring, current_key_id=TEST_KEY_ID)
    assert cached is not None
    provenance = provenance_from_cache(cached, current_key_id=TEST_KEY_ID)

    rendered = json.loads(json.dumps(provenance_to_dict(provenance)))

    assert rendered["issued_at"] == provenance.issued_at.isoformat()
    assert rendered["valid_until"] == provenance.valid_until.isoformat()
    assert rendered["fetched_at"] == provenance.fetched_at.isoformat()
    assert date.fromisoformat(rendered["issued_at"]) == provenance.issued_at
    assert datetime.fromisoformat(rendered["fetched_at"]) == provenance.fetched_at
    assert rendered["valid"] is not expired
    assert rendered["directory_version"] == 1
    assert rendered["provider_count"] == 2
