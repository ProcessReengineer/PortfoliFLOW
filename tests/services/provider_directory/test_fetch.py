# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Obtaining the two published files, and refusing to guess (SB-3b).

Two things are under test here and they are deliberately separate: the strict
B-D-23 decoding of the signature file, and the conditional GET that fetches
it beside its document. Neither knows anything about trust — a 200 with a
forged body is a successful fetch at this layer — so every assertion below is
about *transport*, and about the one place where being helpful would be
dangerous: a signature that only decodes after the reader tidies it up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest
from pytest_httpx import HTTPXMock

from services.provider_directory.fetch import (
    DIRECTORY_SIGNATURE_URL,
    DIRECTORY_URL,
    DirectoryUnavailable,
    SignatureFileError,
    decode_signature_file,
    fetch_directory,
    signature_url_for,
)
from tests.services.provider_directory.conftest import FIXTURES

_SIGNATURE_URL: Final[str] = signature_url_for(DIRECTORY_URL)
_DOCUMENT: Final[bytes] = b'{"directory_version":1}'
_SIGNATURE_FILE: Final[bytes] = (b"a" * 128) + b"\n"
_ETAG: Final[str] = '"v1-abc123"'


# ---------------------------------------------------------------------------
# B-D-23 decoding
# ---------------------------------------------------------------------------


def test_the_published_signature_file_decodes_to_sixty_four_bytes() -> None:
    """The encoding under test is the one the operator actually published."""
    published = (FIXTURES / "directory-1.sig").read_bytes()

    assert len(decode_signature_file(published)) == 64


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("no trailing newline", b"a" * 128),
        ("a second newline", (b"a" * 128) + b"\n\n"),
        ("uppercase hex", (b"A" * 128) + b"\n"),
        ("CRLF", (b"a" * 127) + b"\r\n"),
        ("a leading space", b" " + (b"a" * 127) + b"\n"),
        ("empty", b""),
        ("non-ascii", ("é" * 64).encode("utf-8") + b"\n"),
    ],
)
def test_a_signature_file_is_not_repaired_before_it_is_decoded(name: str, data: bytes) -> None:
    """Every near-miss is refused, because a repaired signature is a different one.

    ``.strip()`` would accept the first five of these. A decoder that accepts
    them is one that checks a byte string nobody published, and the whole
    point of a detached signature is that exactly one byte string was.
    """
    with pytest.raises(SignatureFileError):
        decode_signature_file(data)


# ---------------------------------------------------------------------------
# Locating the signature
# ---------------------------------------------------------------------------


def test_the_signature_sits_beside_the_document_it_signs() -> None:
    """The published layout, stated once."""
    assert signature_url_for(DIRECTORY_URL) == DIRECTORY_SIGNATURE_URL


@pytest.mark.parametrize(
    "url",
    [
        "https://portfoliflow.com/directory/v1/directory",
        "https://portfoliflow.com/directory/v1/directory.sig",
        "https://portfoliflow.com/directory/v1/",
    ],
)
def test_a_url_the_signature_cannot_be_derived_from_is_a_caller_error(url: str) -> None:
    """Guessing where a signature lives is worse than refusing to."""
    with pytest.raises(ValueError, match=r"\.json"):
        signature_url_for(url)


# ---------------------------------------------------------------------------
# The conditional GET
# ---------------------------------------------------------------------------


async def test_a_fetch_returns_both_bodies_and_the_etag(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """The document, its signature as served, and the etag to replay next time."""
    httpx_mock.add_response(url=DIRECTORY_URL, content=_DOCUMENT, headers={"ETag": _ETAG})
    httpx_mock.add_response(url=_SIGNATURE_URL, content=_SIGNATURE_FILE)

    fetched = await fetch_directory(client)

    assert fetched is not None
    assert fetched.document_bytes == _DOCUMENT
    assert fetched.signature_file == _SIGNATURE_FILE
    assert fetched.etag == _ETAG


async def test_a_server_that_sends_no_etag_is_not_a_failure(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """An etag is an optimisation; a publisher that omits it costs a download."""
    httpx_mock.add_response(url=DIRECTORY_URL, content=_DOCUMENT)
    httpx_mock.add_response(url=_SIGNATURE_URL, content=_SIGNATURE_FILE)

    fetched = await fetch_directory(client)

    assert fetched is not None
    assert fetched.etag is None


async def test_the_stored_etag_is_sent_as_if_none_match(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Matching on the header is the assertion: an absent one matches nothing."""
    httpx_mock.add_response(
        url=DIRECTORY_URL,
        content=_DOCUMENT,
        match_headers={"If-None-Match": _ETAG},
    )
    httpx_mock.add_response(url=_SIGNATURE_URL, content=_SIGNATURE_FILE)

    fetched = await fetch_directory(client, etag=_ETAG)

    assert fetched is not None


async def test_no_conditional_header_is_sent_without_a_stored_etag(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """An instance with nothing cached asks for the document outright."""
    httpx_mock.add_response(url=DIRECTORY_URL, content=_DOCUMENT)
    httpx_mock.add_response(url=_SIGNATURE_URL, content=_SIGNATURE_FILE)

    await fetch_directory(client)

    request = httpx_mock.get_request(url=DIRECTORY_URL)
    assert request is not None
    assert "if-none-match" not in request.headers


async def test_a_conditional_request_answered_304_means_what_you_have_is_current(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """``None`` rather than an empty document — and the signature is not fetched."""
    httpx_mock.add_response(url=DIRECTORY_URL, status_code=304)

    assert await fetch_directory(client, etag=_ETAG) is None


async def test_an_unasked_304_is_an_answer_this_client_cannot_read(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Nothing was claimed to be held, so "unchanged since what?" has no answer."""
    httpx_mock.add_response(url=DIRECTORY_URL, status_code=304)

    with pytest.raises(DirectoryUnavailable, match="304"):
        await fetch_directory(client)


@pytest.mark.parametrize(
    ("name", "exception"),
    [
        ("timeout", httpx.ReadTimeout("read timed out")),
        ("connection refused", httpx.ConnectError("connection refused")),
    ],
)
async def test_a_transport_failure_names_the_httpx_class(
    client: httpx.AsyncClient,
    httpx_mock: HTTPXMock,
    name: str,
    exception: httpx.HTTPError,
) -> None:
    """An operator log distinguishes a timeout from a DNS failure without reading httpx."""
    httpx_mock.add_exception(exception, url=DIRECTORY_URL)

    with pytest.raises(DirectoryUnavailable, match=type(exception).__name__):
        await fetch_directory(client)


async def test_a_failing_document_request_never_fetches_the_signature(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """No response is registered for the signature, so requesting it fails the test."""
    httpx_mock.add_response(url=DIRECTORY_URL, status_code=500)

    with pytest.raises(DirectoryUnavailable, match="500"):
        await fetch_directory(client)


async def test_a_document_without_its_signature_is_not_a_publication(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """Half a publication is nothing: the signature is what makes the document a fact."""
    httpx_mock.add_response(url=DIRECTORY_URL, content=_DOCUMENT)
    httpx_mock.add_response(url=_SIGNATURE_URL, status_code=500)

    with pytest.raises(DirectoryUnavailable, match="500"):
        await fetch_directory(client)


async def test_a_url_without_a_json_suffix_is_refused_before_any_request(
    client: httpx.AsyncClient, httpx_mock: HTTPXMock
) -> None:
    """The caller error is raised where it is made, not after a pointless round trip."""
    with pytest.raises(ValueError, match=r"\.json"):
        await fetch_directory(client, url="https://portfoliflow.com/directory/v1/directory")

    assert httpx_mock.get_requests() == []


def test_the_fixtures_directory_is_where_this_suite_thinks_it_is() -> None:
    """A guard on the shared path: a moved fixture must fail loudly, not silently skip."""
    assert isinstance(FIXTURES, Path)
    assert (FIXTURES / "directory-1.json").is_file()
    assert (FIXTURES / "directory-1.sig").is_file()
