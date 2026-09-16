# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The copy on disk is data, not trust (SB-3b).

Two invariants carry this module. The first: every read re-verifies. A file
on the local filesystem has no standing that a file off the network lacks —
it is exactly as good as its signature — so a tampered document, a tampered
signature and a missing sidecar all come back as "no cache", which is the
consequence B-D-13 accepts on purpose. The second: every write is atomic. A
crash between two renames must leave the previous publication whole, because
half a cache is indistinguishable from a tampered one and would cost the
instance its downgrade baseline for no reason.

Expiry is the deliberate exception: the signature verified on the way to that
verdict, so a stale copy is still evidence of which version this instance has
seen, and it is returned rather than discarded.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import pytest

from services.provider_directory import cache as cache_module
from services.provider_directory.cache import (
    CACHE_SUBDIR,
    DOCUMENT_FILE,
    META_FILE,
    META_SCHEMA_VERSION,
    SIGNATURE_FILE,
    CachedDirectory,
    CacheMeta,
    CacheMetaError,
    cache_root_for,
    read_cache,
    read_meta,
    touch_meta,
    write_cache,
)
from services.provider_directory.fetch import DIRECTORY_URL
from tests.services.provider_directory.conftest import TEST_KEY_ID

_ETAG: Final[str] = '"v1-abc123"'


def _noon_utc(iso_day: object, *, plus_days: int = 0) -> datetime:
    """Noon UTC on a day stated by the document under test, never a literal."""
    assert isinstance(iso_day, str)
    return datetime.fromisoformat(iso_day).replace(hour=12, tzinfo=timezone.utc) + timedelta(
        days=plus_days
    )


def _meta(fetched_at: datetime, *, version: int = 1, etag: str | None = _ETAG) -> CacheMeta:
    """A sidecar for a document signed under the test ring."""
    return CacheMeta(
        schema_version=META_SCHEMA_VERSION,
        directory_version=version,
        publishing_key_id=TEST_KEY_ID,
        fetched_at=fetched_at,
        etag=etag,
        source_url=DIRECTORY_URL,
    )


@pytest.fixture
def stored(
    cache_root: Path,
    now: datetime,
    make_document: Callable[..., dict[str, object]],
    make_signed: Callable[[Mapping[str, object], bytes], tuple[bytes, bytes]],
    throwaway_keypair: tuple[bytes, bytes],
) -> tuple[bytes, bytes]:
    """A written cache holding version 1, signed under the test ring."""
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    write_cache(
        cache_root,
        document_bytes=document_bytes,
        signature_file=signature_file,
        meta=_meta(now),
    )
    return document_bytes, signature_file


# ---------------------------------------------------------------------------
# Location and sidecar
# ---------------------------------------------------------------------------


def test_the_cache_lives_in_one_named_place_under_the_data_directory() -> None:
    """The caller resolves DATA_DIR; this package only knows the subdirectory."""
    assert cache_root_for("/srv/portfoliflow/data") == Path("/srv/portfoliflow/data") / CACHE_SUBDIR


def test_a_sidecar_round_trips_through_its_own_serialisation(now: datetime) -> None:
    """What is written is what comes back, timestamp included."""
    meta = _meta(now)

    restored = CacheMeta.from_dict(json.loads(json.dumps(meta.to_dict())))

    assert restored == meta
    assert restored.fetched_at == now


def test_a_naive_timestamp_is_refused_where_it_is_constructed(now: datetime) -> None:
    """An instant without an offset cannot be compared to a validity window."""
    with pytest.raises(ValueError, match="timezone-aware"):
        _meta(now.replace(tzinfo=None))


def test_a_sidecar_from_a_layout_this_build_does_not_read_is_refused(now: datetime) -> None:
    """A future layout is named, not guessed at."""
    data = dict(_meta(now).to_dict())
    data["schema_version"] = META_SCHEMA_VERSION + 1

    with pytest.raises(CacheMetaError, match="schema_version"):
        CacheMeta.from_dict(data)


def test_a_sidecar_missing_a_member_is_refused(now: datetime) -> None:
    """Named, so an operator reading a log knows which member went missing."""
    data = dict(_meta(now).to_dict())
    del data["source_url"]

    with pytest.raises(CacheMetaError, match="source_url"):
        CacheMeta.from_dict(data)


# ---------------------------------------------------------------------------
# Reading re-verifies
# ---------------------------------------------------------------------------


def test_an_empty_root_is_simply_no_cache(cache_root: Path, now: datetime) -> None:
    """A first start is not an error."""
    assert read_cache(cache_root, now=now) is None


def test_a_written_cache_reads_back_verified(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """The round trip, with the version taken from the signed bytes."""
    cached = read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID)

    assert cached is not None
    assert cached.verification is not None
    assert cached.directory_version == 1
    assert cached.publishing_key_id == TEST_KEY_ID
    assert cached.document_bytes == stored[0]
    assert cached.signature_file == stored[1]
    assert cached.meta.etag == _ETAG


def test_a_tampered_document_is_no_cache_at_all(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """One edited letter, same length, still canonical JSON — and still refused.

    The edit is chosen to survive every check *except* the signature, so what
    this pins is re-verification on read rather than a JSON parser noticing.
    """
    document_path = cache_root / DOCUMENT_FILE
    document_path.write_bytes(stored[0].replace(b"Broker Desk", b"Br0ker Desk", 1))
    assert document_path.read_bytes() != stored[0]

    assert read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID) is None


def test_a_tampered_signature_is_no_cache_at_all(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """Still valid B-D-23, still lowercase hex — and it verifies nothing."""
    signature_path = cache_root / SIGNATURE_FILE
    first = stored[1][:1]
    signature_path.write_bytes((b"b" if first == b"a" else b"a") + stored[1][1:])

    assert read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID) is None


def test_a_signature_file_in_the_wrong_encoding_is_no_cache_at_all(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """The stored file is decoded as strictly as the served one."""
    (cache_root / SIGNATURE_FILE).write_bytes(stored[1].strip().upper() + b"\n")

    assert read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID) is None


def test_a_missing_sidecar_is_no_cache_at_all(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """Both published files are present, and the copy is still unusable.

    Deliberate: without the sidecar there is no etag and no record of where
    the copy came from, so it cannot be described to an operator, and a copy
    that cannot be described is not one to act on.
    """
    (cache_root / META_FILE).unlink()

    assert read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID) is None


def test_an_unparsable_sidecar_is_no_cache_at_all(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """Truncated mid-write by an older, non-atomic writer, say."""
    (cache_root / META_FILE).write_bytes(b'{"schema_version": 1, "directo')

    assert read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID) is None


def test_a_document_signed_by_a_key_outside_the_ring_is_no_cache_at_all(
    cache_root: Path,
    now: datetime,
    stored: tuple[bytes, bytes],
    throwaway_keypair: tuple[bytes, bytes],
) -> None:
    """The ring the *reader* ships decides, not the ring that wrote the file."""
    foreign_ring = {"some-other-id": throwaway_keypair[1]}

    assert (
        read_cache(cache_root, now=now, ring=foreign_ring, current_key_id="some-other-id") is None
    )


def test_a_naive_instant_is_a_caller_error(cache_root: Path, now: datetime) -> None:
    """Refused at the door, before anything is read."""
    with pytest.raises(ValueError, match="timezone-aware"):
        read_cache(cache_root, now=now.replace(tzinfo=None))


# ---------------------------------------------------------------------------
# Expired is not invalid
# ---------------------------------------------------------------------------


def test_an_expired_copy_is_still_the_downgrade_baseline(
    cache_root: Path,
    stored: tuple[bytes, bytes],
    fixture_document: dict[str, object],
    test_ring: dict[str, bytes],
) -> None:
    """Too old to act on, still authoritative about which version was seen.

    The trust gate checks the signature (step 5) before the window (step 6),
    so an expired document has proved its provenance. Discarding it would let
    an attacker who can stall an instance past ``valid_until`` replay an older
    publication as if it were new (B-D-15).
    """
    after_expiry = _noon_utc(fixture_document["valid_until"], plus_days=1)

    cached = read_cache(cache_root, now=after_expiry, ring=test_ring, current_key_id=TEST_KEY_ID)

    assert isinstance(cached, CachedDirectory)
    assert cached.verification is None
    assert cached.directory_version == 1
    assert cached.publishing_key_id == TEST_KEY_ID


# ---------------------------------------------------------------------------
# Writing is atomic
# ---------------------------------------------------------------------------


def test_a_write_interrupted_before_the_first_rename_changes_nothing(
    cache_root: Path,
    now: datetime,
    stored: tuple[bytes, bytes],
    make_document: Callable[..., dict[str, object]],
    make_signed: Callable[[Mapping[str, object], bytes], tuple[bytes, bytes]],
    throwaway_keypair: tuple[bytes, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The previous publication survives byte-identical, and no debris is left.

    This is the invariant that makes "the disk is the baseline" safe: an
    instance killed mid-refresh comes back holding the version it had, not a
    document glued to someone else's signature.
    """
    before = {
        name: (cache_root / name).read_bytes()
        for name in (DOCUMENT_FILE, SIGNATURE_FILE, META_FILE)
    }
    replacement_bytes, replacement_signature = make_signed(
        make_document(directory_version=2), throwaway_keypair[0]
    )

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated interruption between write and rename")

    monkeypatch.setattr(cache_module.os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated interruption"):
        write_cache(
            cache_root,
            document_bytes=replacement_bytes,
            signature_file=replacement_signature,
            meta=_meta(now, version=2),
        )

    monkeypatch.undo()
    for name, payload in before.items():
        assert (cache_root / name).read_bytes() == payload
    assert list(cache_root.glob("*.tmp")) == []


def test_write_cache_creates_the_root_it_is_given(
    cache_root: Path,
    now: datetime,
    make_document: Callable[..., dict[str, object]],
    make_signed: Callable[[Mapping[str, object], bytes], tuple[bytes, bytes]],
    throwaway_keypair: tuple[bytes, bytes],
) -> None:
    """A first refresh does not require the operator to have made a directory."""
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    assert not cache_root.exists()

    write_cache(
        cache_root,
        document_bytes=document_bytes,
        signature_file=signature_file,
        meta=_meta(now),
    )

    assert sorted(path.name for path in cache_root.iterdir()) == sorted(
        (DOCUMENT_FILE, META_FILE, SIGNATURE_FILE)
    )


# ---------------------------------------------------------------------------
# Confirming what is already held
# ---------------------------------------------------------------------------


def test_a_confirmation_moves_only_the_moment_and_the_etag(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes]
) -> None:
    """A 304 says the bytes did not change; everything about them is carried over."""
    before = read_meta(cache_root)
    assert before is not None
    later = now + timedelta(days=3)

    updated = touch_meta(cache_root, fetched_at=later, etag='"v1-def456"')

    assert updated.fetched_at == later
    assert updated.etag == '"v1-def456"'
    assert updated.directory_version == before.directory_version
    assert updated.publishing_key_id == before.publishing_key_id
    assert updated.source_url == before.source_url
    assert (cache_root / DOCUMENT_FILE).read_bytes() == stored[0]
    assert (cache_root / SIGNATURE_FILE).read_bytes() == stored[1]
    assert read_meta(cache_root) == updated


def test_a_confirmation_without_a_copy_to_confirm_is_refused(
    cache_root: Path, now: datetime
) -> None:
    """There is nothing to carry over, and inventing a sidecar would invent a source."""
    cache_root.mkdir(parents=True)

    with pytest.raises(CacheMetaError, match=META_FILE):
        touch_meta(cache_root, fetched_at=now, etag=None)


def test_read_meta_survives_a_document_it_could_not_verify(
    cache_root: Path, now: datetime, stored: tuple[bytes, bytes], test_ring: dict[str, bytes]
) -> None:
    """The etag is worth replaying even when the document beside it is unusable.

    The worst a stale or forged etag can do is provoke one extra download —
    and the 304 it might earn is handled as "no cache", never as "unchanged".
    """
    (cache_root / DOCUMENT_FILE).write_bytes(stored[0].replace(b"Broker Desk", b"Br0ker Desk", 1))

    assert read_cache(cache_root, now=now, ring=test_ring, current_key_id=TEST_KEY_ID) is None
    meta = read_meta(cache_root)
    assert meta is not None
    assert meta.etag == _ETAG
