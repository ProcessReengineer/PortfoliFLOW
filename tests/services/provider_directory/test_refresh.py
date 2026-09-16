# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""One refresh, and every reason to refuse one (SB-3b).

The arming decision is small and the ways it can go wrong are not, so each
test below is named for the invariant it pins. Three run through everything:

* **a refusal never touches the cache** — an instance that cannot obtain a
  directory it believes keeps the last one it could, byte for byte;
* **acceptance is monotonic, and expiry does not reset it** (B-D-15) — a
  stale copy is still proof of which version this instance has seen, which is
  what makes a replayed older publication refusable;
* **nothing about the network or the document raises** — every mapped path
  returns a :class:`RefreshOutcome`, because "no directory today" is an
  operating condition, not a bug.

The shipped publishing ring appears exactly once, in the smoke test at the
end, and only against the published fixture: a public document and a public
signature. Everything else runs on throwaway keys generated per test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import httpx
import pytest
from pytest_httpx import HTTPXMock

from services.provider_channel.directory import PublishingKeyNotConfigured, canonical_bytes
from services.provider_channel.publishing_key import (
    PUBLISHING_KEY_ID,
    PUBLISHING_KEY_PLACEHOLDER,
)
from services.provider_channel.ring import UnknownPublishingKeyId
from services.provider_directory.cache import (
    DOCUMENT_FILE,
    META_FILE,
    META_SCHEMA_VERSION,
    SIGNATURE_FILE,
    CacheMeta,
    read_meta,
    write_cache,
)
from services.provider_directory.fetch import DIRECTORY_URL, signature_url_for
from services.provider_directory.refresh import RefreshOutcome, refresh_directory
from tests.services.provider_directory.conftest import (
    FIXTURES,
    TEST_KEY_ID,
    TEST_SUCCESSOR_KEY_ID,
    signature_file_bytes,
)

_SIGNATURE_URL: Final[str] = signature_url_for(DIRECTORY_URL)
_ETAG: Final[str] = '"v5-cached"'
_NEW_ETAG: Final[str] = '"v6-served"'

#: The version the primed cache holds, chosen high enough that a *lower*
#: version is a real publication rather than a negative number.
_CACHED_VERSION: Final[int] = 5

DocumentFactory = Callable[..., dict[str, object]]
SignFactory = Callable[[Mapping[str, object], bytes], tuple[bytes, bytes]]


def _noon_utc(iso_day: object, *, plus_days: int = 0) -> datetime:
    """Noon UTC on a day stated by the document under test, never a literal."""
    assert isinstance(iso_day, str)
    return datetime.fromisoformat(iso_day).replace(hour=12, tzinfo=timezone.utc) + timedelta(
        days=plus_days
    )


def _serve(
    httpx_mock: HTTPXMock,
    document_bytes: bytes,
    signature_file: bytes,
    *,
    etag: str | None = None,
) -> None:
    """Register one publication: the document and the signature beside it."""
    httpx_mock.add_response(
        url=DIRECTORY_URL,
        content=document_bytes,
        headers={"ETag": etag} if etag is not None else None,
    )
    httpx_mock.add_response(url=_SIGNATURE_URL, content=signature_file)


def _files(cache_root: Path) -> dict[str, bytes]:
    """The three cached files, for a before/after comparison."""
    return {
        name: (cache_root / name).read_bytes()
        for name in (DOCUMENT_FILE, SIGNATURE_FILE, META_FILE)
    }


@pytest.fixture
def cached_publication(
    cache_root: Path,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
) -> tuple[bytes, bytes]:
    """A cache already holding version 5, signed under the test ring."""
    document_bytes, signature_file = make_signed(
        make_document(directory_version=_CACHED_VERSION), throwaway_keypair[0]
    )
    write_cache(
        cache_root,
        document_bytes=document_bytes,
        signature_file=signature_file,
        meta=CacheMeta(
            schema_version=META_SCHEMA_VERSION,
            directory_version=_CACHED_VERSION,
            publishing_key_id=TEST_KEY_ID,
            fetched_at=now,
            etag=_ETAG,
            source_url=DIRECTORY_URL,
        ),
    )
    return document_bytes, signature_file


async def _refresh(
    *,
    cache_root: Path,
    client: httpx.AsyncClient,
    now: datetime,
    ring: Mapping[str, bytes],
    current_key_id: str = TEST_KEY_ID,
) -> RefreshOutcome:
    """Run one refresh against the test ring."""
    return await refresh_directory(
        cache_root=cache_root,
        client=client,
        now=now,
        ring=ring,
        current_key_id=current_key_id,
    )


# ---------------------------------------------------------------------------
# Accepting
# ---------------------------------------------------------------------------


async def test_a_first_refresh_stores_the_publication_and_its_provenance(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """An instance with nothing cached takes what it can verify, and records it."""
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    _serve(httpx_mock, document_bytes, signature_file, etag=_NEW_ETAG)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "updated"
    assert outcome.notices == ()
    assert (cache_root / DOCUMENT_FILE).read_bytes() == document_bytes
    assert (cache_root / SIGNATURE_FILE).read_bytes() == signature_file
    assert outcome.provenance is not None
    assert outcome.provenance.valid is True
    assert outcome.provenance.directory_version == 1
    assert outcome.provenance.etag == _NEW_ETAG
    assert outcome.provenance.source_url == DIRECTORY_URL
    stored_meta = read_meta(cache_root)
    assert stored_meta is not None
    assert stored_meta.etag == _NEW_ETAG
    assert stored_meta.fetched_at == now


async def test_a_higher_version_replaces_the_cached_one(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """Forward is the only direction a publication may move."""
    document_bytes, signature_file = make_signed(
        make_document(directory_version=_CACHED_VERSION + 1), throwaway_keypair[0]
    )
    _serve(httpx_mock, document_bytes, signature_file, etag=_NEW_ETAG)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "updated"
    assert (cache_root / DOCUMENT_FILE).read_bytes() == document_bytes
    assert outcome.provenance is not None
    assert outcome.provenance.directory_version == _CACHED_VERSION + 1


async def test_a_304_confirms_the_copy_and_advances_only_the_moment(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """The whole point of the etag: an unchanged directory costs one 304.

    Run as two real refreshes rather than a primed cache, because what is
    under test is that the etag this client *stored* is the one it later
    *sends*.
    """
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    _serve(httpx_mock, document_bytes, signature_file, etag=_NEW_ETAG)
    httpx_mock.add_response(
        url=DIRECTORY_URL,
        status_code=304,
        match_headers={"If-None-Match": _NEW_ETAG},
    )
    await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)
    before = _files(cache_root)
    later = now + timedelta(days=1)

    outcome = await _refresh(cache_root=cache_root, client=client, now=later, ring=test_ring)

    assert outcome.status == "unchanged"
    assert (cache_root / DOCUMENT_FILE).read_bytes() == before[DOCUMENT_FILE]
    assert (cache_root / SIGNATURE_FILE).read_bytes() == before[SIGNATURE_FILE]
    stored_meta = read_meta(cache_root)
    assert stored_meta is not None
    assert stored_meta.fetched_at == later
    assert stored_meta.etag == _NEW_ETAG
    assert outcome.provenance is not None
    assert outcome.provenance.fetched_at == later


async def test_the_same_version_with_the_same_bytes_is_unchanged_without_a_304(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """A server that ignores ``If-None-Match`` costs a download, not a rewrite.

    The branch exists on its own because not every publisher implements
    conditional requests, and re-writing three identical files on every poll
    would be a needless way to lose them to an interrupted write.
    """
    document_bytes, signature_file = cached_publication
    _serve(httpx_mock, document_bytes, signature_file, etag=_NEW_ETAG)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "unchanged"
    assert (cache_root / DOCUMENT_FILE).read_bytes() == before[DOCUMENT_FILE]
    assert (cache_root / SIGNATURE_FILE).read_bytes() == before[SIGNATURE_FILE]
    stored_meta = read_meta(cache_root)
    assert stored_meta is not None
    assert stored_meta.etag == _NEW_ETAG


# ---------------------------------------------------------------------------
# Refusing — the cache survives every one of them
# ---------------------------------------------------------------------------


async def test_a_lower_version_is_refused_and_the_cache_is_kept(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """A rollback attack is a properly signed older publication, replayed."""
    document_bytes, signature_file = make_signed(
        make_document(directory_version=_CACHED_VERSION - 1), throwaway_keypair[0]
    )
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_downgrade"
    assert outcome.notices == (
        f"server serves version {_CACHED_VERSION - 1}, cached is {_CACHED_VERSION}",
    )
    assert _files(cache_root) == before
    assert outcome.provenance is not None
    assert outcome.provenance.directory_version == _CACHED_VERSION


async def test_the_cached_version_republished_with_other_bytes_is_refused(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """Changing the list without admitting to a new version deserves an operator."""
    document = make_document(directory_version=_CACHED_VERSION)
    providers = document["providers"]
    assert isinstance(providers, list)
    document["providers"] = providers[:1]
    document_bytes, signature_file = make_signed(document, throwaway_keypair[0])
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_republished"
    assert outcome.notices == (
        f"version {_CACHED_VERSION} was re-published without a version bump",
    )
    assert _files(cache_root) == before


async def test_an_expired_cache_still_refuses_a_downgrade(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    cached_publication: tuple[bytes, bytes],
    fixture_document: dict[str, object],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """B-D-15 does not lapse when the window does.

    The served document is inside *its* window, so the only thing standing
    between the instance and an older publication is the expired copy on
    disk — which is exactly the situation an attacker who can stall an
    instance would engineer.
    """
    expiry_day = date.fromisoformat(str(fixture_document["valid_until"]))
    after_expiry = _noon_utc(fixture_document["valid_until"], plus_days=1)
    document_bytes, signature_file = make_signed(
        make_document(
            directory_version=_CACHED_VERSION - 1,
            issued_at=expiry_day.isoformat(),
            valid_until=(expiry_day + timedelta(days=90)).isoformat(),
        ),
        throwaway_keypair[0],
    )
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=after_expiry, ring=test_ring)

    assert outcome.status == "refused_downgrade"
    assert _files(cache_root) == before
    assert outcome.provenance is not None
    assert outcome.provenance.valid is False
    assert outcome.provenance.directory_version == _CACHED_VERSION


async def test_a_format_version_this_build_cannot_read_asks_for_an_update(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """ "This build is too old" and "this document is wrong" need different answers."""
    document_bytes, signature_file = make_signed(
        make_document(format_version=2, directory_version=_CACHED_VERSION + 1),
        throwaway_keypair[0],
    )
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_unknown_version"
    assert "client update required" in outcome.notices
    assert _files(cache_root) == before


async def test_a_key_id_outside_the_shipped_ring_asks_for_an_update(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """Rotation is a code release (B-D-14): an unknown id is not a key, it is a notice."""
    document_bytes, signature_file = make_signed(
        make_document(publishing_key_id="test-2099", directory_version=_CACHED_VERSION + 1),
        throwaway_keypair[0],
    )
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_unknown_key"
    assert "client update required" in outcome.notices
    assert _files(cache_root) == before


async def test_a_signature_from_the_wrong_key_is_refused(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    successor_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """The id selects a key; holding that key is what verifies."""
    document_bytes, signature_file = make_signed(
        make_document(directory_version=_CACHED_VERSION + 1), successor_keypair[0]
    )
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_invalid"
    assert _files(cache_root) == before


async def test_a_document_outside_its_own_window_is_refused(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """A stale publication is not accepted just because it is newer than the cache."""
    stale_until = now.date() - timedelta(days=1)
    document_bytes, signature_file = make_signed(
        make_document(
            directory_version=_CACHED_VERSION + 1,
            issued_at=(stale_until - timedelta(days=30)).isoformat(),
            valid_until=stale_until.isoformat(),
        ),
        throwaway_keypair[0],
    )
    _serve(httpx_mock, document_bytes, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_invalid"
    assert _files(cache_root) == before


async def test_a_re_spelled_document_is_refused(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """Same content, other bytes: the signature covers one spelling and only one."""
    document = make_document(directory_version=_CACHED_VERSION + 1)
    _canonical, signature_file = make_signed(document, throwaway_keypair[0])
    indented = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
    assert indented != canonical_bytes(document)
    _serve(httpx_mock, indented, signature_file)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_invalid"
    assert _files(cache_root) == before


async def test_a_signature_file_in_the_wrong_encoding_is_refused(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """B-D-23 is checked before the arithmetic, and is not repaired to fit."""
    document_bytes, signature_file = make_signed(
        make_document(directory_version=_CACHED_VERSION + 1), throwaway_keypair[0]
    )
    _serve(httpx_mock, document_bytes, signature_file.strip().upper() + b"\n")
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "refused_invalid"
    assert _files(cache_root) == before


# ---------------------------------------------------------------------------
# Unavailable — the network, not the document
# ---------------------------------------------------------------------------


async def test_an_unreachable_server_leaves_the_instance_on_what_it_holds(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """The cache exists precisely so that a bad day for the network is not one here."""
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=DIRECTORY_URL)
    before = _files(cache_root)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "unavailable"
    assert len(outcome.notices) == 1
    assert "ConnectError" in outcome.notices[0]
    assert _files(cache_root) == before
    assert outcome.provenance is not None
    assert outcome.provenance.directory_version == _CACHED_VERSION
    assert outcome.provenance.valid is True


async def test_a_failure_with_nothing_cached_reports_no_provenance(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    test_ring: dict[str, bytes],
) -> None:
    """``None`` is the honest answer: the instance is standing on nothing."""
    httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=DIRECTORY_URL)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "unavailable"
    assert outcome.provenance is None


async def test_a_304_answering_an_etag_whose_document_is_unusable_is_unavailable(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    cached_publication: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """ "Unchanged since what?" — the etag survived a tampered document, the trust did not.

    The sidecar is unsigned, so an attacker who can edit the document can
    leave the etag intact. The 304 that earns then lands here rather than in
    the "unchanged" branch, because there is no verified copy to confirm.
    """
    (cache_root / DOCUMENT_FILE).write_bytes(
        cached_publication[0].replace(b"Broker Desk", b"Br0ker Desk", 1)
    )
    httpx_mock.add_response(
        url=DIRECTORY_URL, status_code=304, match_headers={"If-None-Match": _ETAG}
    )

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "unavailable"
    assert outcome.notices == ("server answered 304 but no usable cache exists",)
    assert outcome.provenance is None


# ---------------------------------------------------------------------------
# Rotation notices — reported, never enforced
# ---------------------------------------------------------------------------


async def test_a_document_signed_by_the_successor_says_the_rotation_happened(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    successor_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """Accepted, because the key was already in the ring — and reported, so an
    operator knows the current key is no longer the one in use."""
    document_bytes, signature_file = make_signed(
        make_document(publishing_key_id=TEST_SUCCESSOR_KEY_ID), successor_keypair[0]
    )
    _serve(httpx_mock, document_bytes, signature_file)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "updated"
    assert f"successor key {TEST_SUCCESSOR_KEY_ID} in use" in outcome.notices
    assert outcome.provenance is not None
    assert outcome.provenance.successor_in_use is True


async def test_an_announced_successor_outside_the_ring_warns_before_the_rotation(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """The document is accepted; the warning is that the *next* one will not be."""
    document_bytes, signature_file = make_signed(
        make_document(
            successor_key={
                "publishing_key_id": "test-2099",
                "public_key": "c" * 64,
                "valid_from": "2099-01-01",
            }
        ),
        throwaway_keypair[0],
    )
    _serve(httpx_mock, document_bytes, signature_file)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "updated"
    assert (
        "announced successor not in shipped ring; client update required before rotation"
        in outcome.notices
    )


async def test_an_announcement_that_contradicts_the_ring_is_reported(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """A known id with an unknown key is the shape a key substitution would take."""
    document_bytes, signature_file = make_signed(
        make_document(
            successor_key={
                "publishing_key_id": TEST_SUCCESSOR_KEY_ID,
                "public_key": "d" * 64,
                "valid_from": "2099-01-01",
            }
        ),
        throwaway_keypair[0],
    )
    _serve(httpx_mock, document_bytes, signature_file)

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert outcome.status == "updated"
    assert "announced successor contradicts the shipped ring" in outcome.notices


# ---------------------------------------------------------------------------
# Caller errors — these do raise, before anything is requested
# ---------------------------------------------------------------------------


async def test_a_naive_instant_is_refused_before_any_request(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    test_ring: dict[str, bytes],
) -> None:
    """An instant without an offset cannot be judged against a validity window."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await _refresh(
            cache_root=cache_root, client=client, now=now.replace(tzinfo=None), ring=test_ring
        )

    assert httpx_mock.get_requests() == []
    assert not cache_root.exists()


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_a_current_key_outside_the_ring_is_a_caller_error_not_a_refusal(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """The ring raises this type for a bad *document* too, so it is checked here first.

    A perfectly serviceable publication is registered and deliberately left
    unrequested: the point is that a misconfigured client never gets as far as
    asking, and so can never mistake its own misconfiguration for a rejected
    document.
    """
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    _serve(httpx_mock, document_bytes, signature_file)

    with pytest.raises(UnknownPublishingKeyId, match="misconfigured caller"):
        await _refresh(
            cache_root=cache_root,
            client=client,
            now=now,
            ring=test_ring,
            current_key_id="test-not-in-ring",
        )

    assert httpx_mock.get_requests() == []


async def test_the_placeholder_key_fails_closed(
    cache_root: Path, client: httpx.AsyncClient, httpx_mock: HTTPXMock, now: datetime
) -> None:
    """A build that lost its key trusts nothing, rather than trusting anything."""
    with pytest.raises(PublishingKeyNotConfigured):
        await _refresh(
            cache_root=cache_root,
            client=client,
            now=now,
            ring={TEST_KEY_ID: PUBLISHING_KEY_PLACEHOLDER},
        )

    assert httpx_mock.get_requests() == []


async def test_a_url_the_signature_cannot_be_derived_from_is_a_caller_error(
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    test_ring: dict[str, bytes],
) -> None:
    """Refused before the cache directory is even created."""
    with pytest.raises(ValueError, match=r"\.json"):
        await refresh_directory(
            cache_root=cache_root,
            client=client,
            now=now,
            url="https://portfoliflow.com/directory/v1/directory",
            ring=test_ring,
            current_key_id=TEST_KEY_ID,
        )

    assert httpx_mock.get_requests() == []
    assert not cache_root.exists()


# ---------------------------------------------------------------------------
# Nothing about the network or the document raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("timeout", "unavailable"),
        ("connect_error", "unavailable"),
        ("document_500", "unavailable"),
        ("signature_500", "unavailable"),
        ("unasked_304", "unavailable"),
        ("bad_signature_file", "refused_invalid"),
        ("wrong_key", "refused_invalid"),
        ("expired_document", "refused_invalid"),
        ("unknown_format_version", "refused_unknown_version"),
        ("unknown_key_id", "refused_unknown_key"),
    ],
)
async def test_every_mapped_failure_is_reported_rather_than_raised(
    scenario: str,
    expected: str,
    cache_root: Path,
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    now: datetime,
    make_document: DocumentFactory,
    make_signed: SignFactory,
    throwaway_keypair: tuple[bytes, bytes],
    successor_keypair: tuple[bytes, bytes],
    test_ring: dict[str, bytes],
) -> None:
    """ "No directory today" is an operating condition, and the caller must be able
    to log it rather than catch it."""
    document_bytes, signature_file = make_signed(make_document(), throwaway_keypair[0])
    if scenario == "timeout":
        httpx_mock.add_exception(httpx.ReadTimeout("read timed out"), url=DIRECTORY_URL)
    elif scenario == "connect_error":
        httpx_mock.add_exception(httpx.ConnectError("connection refused"), url=DIRECTORY_URL)
    elif scenario == "document_500":
        httpx_mock.add_response(url=DIRECTORY_URL, status_code=500)
    elif scenario == "signature_500":
        httpx_mock.add_response(url=DIRECTORY_URL, content=document_bytes)
        httpx_mock.add_response(url=_SIGNATURE_URL, status_code=500)
    elif scenario == "unasked_304":
        httpx_mock.add_response(url=DIRECTORY_URL, status_code=304)
    elif scenario == "bad_signature_file":
        _serve(httpx_mock, document_bytes, signature_file[:-1])
    elif scenario == "wrong_key":
        _serve(httpx_mock, *make_signed(make_document(), successor_keypair[0]))
    elif scenario == "expired_document":
        stale = now.date() - timedelta(days=1)
        _serve(
            httpx_mock,
            *make_signed(
                make_document(
                    issued_at=(stale - timedelta(days=30)).isoformat(),
                    valid_until=stale.isoformat(),
                ),
                throwaway_keypair[0],
            ),
        )
    elif scenario == "unknown_format_version":
        _serve(httpx_mock, *make_signed(make_document(format_version=2), throwaway_keypair[0]))
    else:
        _serve(
            httpx_mock,
            *make_signed(make_document(publishing_key_id="test-2099"), throwaway_keypair[0]),
        )

    outcome = await _refresh(cache_root=cache_root, client=client, now=now, ring=test_ring)

    assert isinstance(outcome, RefreshOutcome)
    assert outcome.status == expected
    assert outcome.notices != ()
    assert outcome.provenance is None


# ---------------------------------------------------------------------------
# The shipped ring, exactly once
# ---------------------------------------------------------------------------


async def test_the_published_directory_verifies_under_the_shipped_ring(
    cache_root: Path, client: httpx.AsyncClient, httpx_mock: HTTPXMock, now: datetime
) -> None:
    """End to end against the real publication: the ring that ships, the bytes that shipped.

    Public material only — a published document and its detached signature.
    The private half never enters this repository. ``now`` comes from the
    document's own ``issued_at``, so the test does not expire when the window
    does.
    """
    document_bytes = (FIXTURES / "directory-1.json").read_bytes()
    signature_file = (FIXTURES / "directory-1.sig").read_bytes()
    _serve(httpx_mock, document_bytes, signature_file, etag=_NEW_ETAG)

    outcome = await refresh_directory(cache_root=cache_root, client=client, now=now)

    assert outcome.status == "updated"
    assert outcome.notices == ()
    assert outcome.provenance is not None
    assert outcome.provenance.directory_version == 1
    assert outcome.provenance.publishing_key_id == PUBLISHING_KEY_ID == "portfoliflow-2026-09"
    assert outcome.provenance.provider_count == 2
    assert outcome.provenance.valid is True
    assert outcome.provenance.announced_successor == "in_ring"
    assert (cache_root / DOCUMENT_FILE).read_bytes() == document_bytes
    assert (cache_root / SIGNATURE_FILE).read_bytes() == signature_file


def test_the_signature_encoding_helper_matches_what_the_publisher_wrote() -> None:
    """The helper the suite signs with produces the published encoding, byte for byte."""
    published = (FIXTURES / "directory-1.sig").read_bytes()

    assert signature_file_bytes(bytes.fromhex(published[:-1].decode("ascii"))) == published
