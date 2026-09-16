# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Obtaining the published directory document over HTTPS (SB-3b).

This is the one module in the arming layer that talks to the network, and it
is deliberately the dumbest: it fetches two byte strings and reports whether
it could. It parses nothing, believes nothing, and writes nothing — the
document is handed on exactly as served, because the bytes the signature
covers are the bytes that were published and re-spelling them would check a
document nobody signed.

**Transport is not trust.** HTTPS says the bytes arrived from the host the
URL names; it does not say the document may be believed. That question
belongs to :func:`services.provider_channel.ring.verify_directory_with_ring`
and to nothing here, which is why a 200 response with a forged body is, at
this layer, a perfectly successful fetch.

**The signature travels beside the document.** A 200 on the document is
always followed by an unconditional GET of the ``.sig`` file next to it: the
two are one publication, and fetching them separately is the only way to keep
the signature out of the document it signs.

**Conditional by etag, never by clock.** ``If-None-Match`` carries the etag
the cache stored, so an unchanged directory costs one 304 instead of a
download. A 304 is only meaningful as an answer to that header — a server
that sends one unasked has said something this client cannot interpret, and
it is refused rather than read as "unchanged".

No retries: a failed fetch is reported as such and the caller decides. No
clock, no cache, no disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx

#: Where the published directory lives.
DIRECTORY_URL: Final[str] = "https://portfoliflow.com/directory/v1/directory.json"

#: The detached signature beside it (B-D-23).
DIRECTORY_SIGNATURE_URL: Final[str] = "https://portfoliflow.com/directory/v1/directory.sig"

#: Per-request timeout in seconds. Arming is a background errand; it may wait
#: a moment, and it may never hang a caller indefinitely.
DEFAULT_TIMEOUT: Final[float] = 10.0

#: The suffix a directory document is published under, and the one the
#: signature file replaces it with.
_DOCUMENT_SUFFIX: Final[str] = ".json"
_SIGNATURE_SUFFIX: Final[str] = ".sig"

#: B-D-23: 128 lowercase hex characters followed by exactly one newline.
_SIGNATURE_HEX_LENGTH: Final[int] = 128
_SIGNATURE_FILE_LENGTH: Final[int] = _SIGNATURE_HEX_LENGTH + 1
_LOWERCASE_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")

#: The two statuses this client knows how to read.
_STATUS_OK: Final[int] = 200
_STATUS_NOT_MODIFIED: Final[int] = 304


class DirectoryUnavailable(Exception):
    """The directory could not be obtained.

    Transport failure, timeout, a status other than 200 or 304, or a 304 sent
    without an ``If-None-Match`` request header. Never a statement about the
    document's *content* — an unavailable directory is a fetch that did not
    happen, not one that was refused.
    """


class SignatureFileError(ValueError):
    """The ``.sig`` bytes are not in the B-D-23 encoding."""


def signature_url_for(url: str) -> str:
    """Derive the signature URL sitting beside a directory document.

    Args:
        url: The document URL, which must end in ``.json``.

    Returns:
        The same URL with ``.json`` replaced by ``.sig``.

    Raises:
        ValueError: If ``url`` does not end in ``.json`` — a caller error,
            because the signature's location is a property of the publication
            layout and cannot be guessed from an arbitrary URL.
    """
    if not url.endswith(_DOCUMENT_SUFFIX):
        raise ValueError(
            f"a directory URL must end in {_DOCUMENT_SUFFIX!r} so its detached "
            f"signature can be derived from it; got {url!r}"
        )
    return url[: -len(_DOCUMENT_SUFFIX)] + _SIGNATURE_SUFFIX


def decode_signature_file(data: bytes) -> bytes:
    """Decode a B-D-23 signature file into the raw 64 signature bytes.

    Strict on purpose: exactly 129 bytes, the last one a newline, the first
    128 lowercase hexadecimal. No trimming and no case folding, because a
    signature that only verifies after the reader tidies it up is not the
    signature the operator published — and a lenient decoder is one more place
    where "the bytes that were checked" and "the bytes that were served" can
    quietly diverge.

    Args:
        data: The ``.sig`` file exactly as served or stored.

    Returns:
        The 64 raw signature bytes.

    Raises:
        SignatureFileError: If ``data`` is not in the B-D-23 encoding.
    """
    if len(data) != _SIGNATURE_FILE_LENGTH:
        raise SignatureFileError(
            f"a signature file is exactly {_SIGNATURE_FILE_LENGTH} bytes "
            f"({_SIGNATURE_HEX_LENGTH} hex characters and one newline); got {len(data)}"
        )
    if not data.endswith(b"\n"):
        raise SignatureFileError("a signature file ends with exactly one newline")
    try:
        hex_text = data[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise SignatureFileError("a signature file is ASCII hexadecimal") from None
    if any(character not in _LOWERCASE_HEX for character in hex_text):
        raise SignatureFileError(
            "a signature file carries lowercase hexadecimal only; it is not "
            "trimmed, case-folded or otherwise repaired before decoding"
        )
    return bytes.fromhex(hex_text)


@dataclass(frozen=True, slots=True)
class FetchedDirectory:
    """What one successful fetch yields, undigested.

    Attributes:
        document_bytes: The document body exactly as served — the byte string
            the signature covers.
        signature_file: The ``.sig`` body as served, still undecoded, so the
            encoding is checked once by whoever needs the signature.
        etag: The document response's ``ETag`` header, or ``None`` when the
            server sent none.
    """

    document_bytes: bytes
    signature_file: bytes
    etag: str | None


async def fetch_directory(
    client: httpx.AsyncClient,
    *,
    url: str = DIRECTORY_URL,
    etag: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> FetchedDirectory | None:
    """GET the directory and its detached signature.

    Args:
        client: The HTTP client to use. Injected so the caller owns its
            lifetime, its proxies and its TLS configuration.
        url: The document URL; must end in ``.json``.
        etag: The etag of the copy already held. When given it is sent as
            ``If-None-Match`` and a 304 answer is honoured.
        timeout: Per-request timeout in seconds.

    Returns:
        The two bodies and the document's etag, or ``None`` when the server
        answered 304 to a conditional request — "what you have is current".

    Raises:
        ValueError: If ``url`` does not end in ``.json``.
        DirectoryUnavailable: On timeout, transport failure, any status other
            than 200 or an honoured 304, or a 304 that answers no
            ``If-None-Match``.
    """
    signature_url = signature_url_for(url)
    headers = {"If-None-Match": etag} if etag is not None else None

    response = await _get(client, url, headers=headers, timeout=timeout)
    if response.status_code == _STATUS_NOT_MODIFIED:
        if etag is None:
            raise DirectoryUnavailable(
                f"{url} answered HTTP {_STATUS_NOT_MODIFIED} to an unconditional request; "
                "a 'not modified' answer is only meaningful against an If-None-Match header"
            )
        return None
    if response.status_code != _STATUS_OK:
        raise DirectoryUnavailable(f"{url} answered HTTP {response.status_code}")

    signature_response = await _get(client, signature_url, headers=None, timeout=timeout)
    if signature_response.status_code != _STATUS_OK:
        raise DirectoryUnavailable(
            f"{signature_url} answered HTTP {signature_response.status_code}; "
            "the document and its signature are one publication"
        )

    return FetchedDirectory(
        document_bytes=response.content,
        signature_file=signature_response.content,
        etag=response.headers.get("ETag"),
    )


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float,
) -> httpx.Response:
    """GET one URL, mapping every transport failure onto the typed error.

    The message names the ``httpx`` exception class rather than only its text,
    so an operator log distinguishes a timeout from a DNS failure without
    having to know the library's wording.
    """
    try:
        return await client.get(url, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise DirectoryUnavailable(
            f"timed out fetching {url}: {type(exc).__name__}: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise DirectoryUnavailable(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc


__all__ = [
    "DEFAULT_TIMEOUT",
    "DIRECTORY_SIGNATURE_URL",
    "DIRECTORY_URL",
    "DirectoryUnavailable",
    "FetchedDirectory",
    "SignatureFileError",
    "decode_signature_file",
    "fetch_directory",
    "signature_url_for",
]
