# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The directory on disk: three files under ``DATA_DIR`` (SB-3b).

An instance that has fetched a directory keeps it, so the next start does not
depend on portfoliflow.com being reachable. The copy is three plain files —
the document, its detached signature, and a small metadata sidecar — because
the two published files must survive byte-identical and a sidecar is the
cheapest place to keep what is *about* the copy rather than in it: when it
was fetched, which etag it answers to, and where it came from.

**The disk is data, not trust.** Every read re-verifies the stored bytes
against the shipped ring. Nothing is believed because it is on the local
filesystem: a file is exactly as trustworthy as its signature, whether it
arrived a second ago over TLS or a month ago from a backup. The consequence
is deliberate (B-D-13): a cache whose bytes were tampered with is *no* cache
and no baseline — the client falls back to having nothing rather than to
having something it cannot check.

**Expired is not invalid.** A document whose validity window has passed still
carries a signature that verifies, and the gate checks the signature (step 5)
before the window (step 6). Such a copy is returned with
``verification=None``: too old to act on, still authoritative about *which
version this instance has seen*, which is what the monotonic version rule
(B-D-15) needs to refuse a downgrade.

**The version is read from the signed bytes, never from the sidecar.** The
sidecar is unsigned; anyone who can edit it can claim any version. It is a
convenience for the conditional GET, not evidence.

Writes are atomic: every file is written to ``<name>.tmp``, flushed to the
platter, and renamed into place, so a crash leaves the previous copy intact
rather than half a document. No clock is read here — the day to judge a
document against is injected, as it is everywhere in the channel (D-clock) —
and ``core.config`` is never imported: the caller resolves ``DATA_DIR`` and
hands in a path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from services.provider_channel.directory import DirectoryExpired, DirectoryVerificationError
from services.provider_channel.publishing_key import PUBLISHING_KEY_ID, PUBLISHING_KEY_RING
from services.provider_channel.ring import RingVerification, verify_directory_with_ring
from services.provider_directory.fetch import SignatureFileError, decode_signature_file

#: Directory under ``DATA_DIR`` holding the cached publication.
CACHE_SUBDIR: Final[str] = "provider_directory"

#: The published document, byte-identical to what the server served.
DOCUMENT_FILE: Final[str] = "directory.json"

#: The detached signature, in its published B-D-23 encoding.
SIGNATURE_FILE: Final[str] = "directory.sig"

#: The unsigned sidecar describing the copy.
META_FILE: Final[str] = "meta.json"

#: Layout version of :data:`META_FILE`. A *file* version, unrelated to the
#: directory's own ``format_version``: it says how this client writes its
#: sidecar, never what the wire format is.
META_SCHEMA_VERSION: Final[int] = 1

#: Suffix every file is written under before it is renamed into place.
_TEMPORARY_SUFFIX: Final[str] = ".tmp"

_META_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "directory_version",
        "publishing_key_id",
        "fetched_at",
        "etag",
        "source_url",
    }
)


class CacheMetaError(ValueError):
    """The metadata sidecar is absent, unreadable, or not this layout."""


def cache_root_for(data_dir: str | os.PathLike[str]) -> Path:
    """Locate the cache directory inside a data directory.

    Args:
        data_dir: The instance's data directory. Resolved by the caller —
            this package never reads a setting.

    Returns:
        ``<data_dir>/provider_directory``.
    """
    return Path(data_dir) / CACHE_SUBDIR


@dataclass(frozen=True, slots=True)
class CacheMeta:
    """What is known *about* the cached copy, rather than in it.

    Unsigned by nature: it describes a local fact, not a published one. Only
    :attr:`etag` is ever acted on, and only to ask the server whether the copy
    is still current — a lie there costs one needless download.

    Attributes:
        schema_version: :data:`META_SCHEMA_VERSION` as written.
        directory_version: The version of the cached document, copied here for
            readability. The authoritative value is read from the signed
            bytes; this one is never trusted.
        publishing_key_id: The key id of the cached document, likewise a copy.
        fetched_at: When the copy was obtained. Timezone-aware; normalised to
            UTC on construction, and a naive value is refused.
        etag: The document response's ``ETag``, replayed as ``If-None-Match``.
        source_url: Where the copy came from.
    """

    schema_version: int
    directory_version: int
    publishing_key_id: str
    fetched_at: datetime
    etag: str | None
    source_url: str

    def __post_init__(self) -> None:
        """Refuse a naive timestamp and normalise the rest to UTC."""
        object.__setattr__(self, "fetched_at", _aware(self.fetched_at, argument="fetched_at"))

    def to_dict(self) -> dict[str, object]:
        """Render the sidecar as JSON-ready primitives."""
        return {
            "schema_version": self.schema_version,
            "directory_version": self.directory_version,
            "publishing_key_id": self.publishing_key_id,
            "fetched_at": self.fetched_at.isoformat(),
            "etag": self.etag,
            "source_url": self.source_url,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CacheMeta:
        """Read a sidecar written by :meth:`to_dict`.

        Args:
            data: The parsed sidecar object.

        Returns:
            The metadata.

        Raises:
            CacheMetaError: If a member is missing, of the wrong type, written
                by a layout this build does not read, or carries a timestamp
                without a timezone.
        """
        missing = sorted(_META_KEYS - set(data))
        if missing:
            raise CacheMetaError(f"{META_FILE} is missing required keys {missing}")
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise CacheMetaError(f"{META_FILE}.schema_version must be an integer")
        if schema_version != META_SCHEMA_VERSION:
            raise CacheMetaError(
                f"{META_FILE} declares schema_version {schema_version}; this build reads "
                f"version {META_SCHEMA_VERSION} only"
            )
        directory_version = data["directory_version"]
        if isinstance(directory_version, bool) or not isinstance(directory_version, int):
            raise CacheMetaError(f"{META_FILE}.directory_version must be an integer")
        publishing_key_id = data["publishing_key_id"]
        source_url = data["source_url"]
        if not isinstance(publishing_key_id, str) or not isinstance(source_url, str):
            raise CacheMetaError(f"{META_FILE}.publishing_key_id and .source_url must be strings")
        etag = data["etag"]
        if etag is not None and not isinstance(etag, str):
            raise CacheMetaError(f"{META_FILE}.etag must be a string or null")
        raw_fetched_at = data["fetched_at"]
        if not isinstance(raw_fetched_at, str):
            raise CacheMetaError(f"{META_FILE}.fetched_at must be an ISO-8601 string")
        try:
            fetched_at = datetime.fromisoformat(raw_fetched_at)
        except ValueError:
            raise CacheMetaError(
                f"{META_FILE}.fetched_at {raw_fetched_at!r} is not an ISO-8601 timestamp"
            ) from None
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise CacheMetaError(f"{META_FILE}.fetched_at carries no timezone")
        return cls(
            schema_version=schema_version,
            directory_version=directory_version,
            publishing_key_id=publishing_key_id,
            fetched_at=fetched_at,
            etag=etag,
            source_url=source_url,
        )


@dataclass(frozen=True, slots=True)
class CachedDirectory:
    """The stored copy, re-verified at the moment it was read.

    Attributes:
        document_bytes: The document as published.
        signature_file: The signature file as published.
        meta: The unsigned sidecar.
        directory_version: Read from the **signed** bytes, never from
            :attr:`meta` — the sidecar is not evidence.
        publishing_key_id: Likewise read from the signed bytes.
        verification: The verification, or ``None`` when the document is
            signed but outside its validity window. ``None`` therefore means
            "too old to act on, still a legitimate downgrade baseline"; a copy
            that failed verification for any other reason is not returned at
            all.
    """

    document_bytes: bytes
    signature_file: bytes
    meta: CacheMeta
    directory_version: int
    publishing_key_id: str
    verification: RingVerification | None


def read_cache(
    root: Path,
    *,
    now: datetime,
    ring: Mapping[str, bytes] = PUBLISHING_KEY_RING,
    current_key_id: str = PUBLISHING_KEY_ID,
) -> CachedDirectory | None:
    """Read the cached copy and re-verify it, or report that there is none.

    Anything wrong with what is on disk yields ``None`` rather than an
    exception: a missing file, an unreadable sidecar, a signature file in the
    wrong encoding, bytes that do not verify, an unknown key id, a format
    version this build does not read. All of them mean the same thing to a
    caller — this instance has no directory it can stand on — and B-D-13
    accepts that a tampered cache costs the baseline rather than silently
    becoming one.

    The one exception is expiry: a document whose window has passed verified
    its signature on the way to that verdict, so it is returned with
    ``verification=None``.

    Args:
        root: The cache directory, typically :func:`cache_root_for`.
        now: The instant to judge validity against; timezone-aware, injected
            rather than read from a clock (D-clock).
        ring: The keys a document may be verified with, by id.
        current_key_id: The id this client considers current.

    Returns:
        The re-verified copy, or ``None`` when there is no usable one.

    Raises:
        ValueError: If ``now`` is naive — a caller error.
        OSError: If ``root`` exists but cannot be read (permissions, I/O). A
            *content* problem never raises; an environment problem always
            does.
    """
    moment = _aware(now, argument="now")
    document_bytes = _read_file(root / DOCUMENT_FILE)
    signature_file = _read_file(root / SIGNATURE_FILE)
    meta_bytes = _read_file(root / META_FILE)
    if document_bytes is None or signature_file is None or meta_bytes is None:
        return None

    try:
        meta = CacheMeta.from_dict(_parse_object(meta_bytes))
        signature = decode_signature_file(signature_file)
    except (CacheMetaError, SignatureFileError):
        return None

    verification: RingVerification | None
    try:
        verification = verify_directory_with_ring(
            document_bytes,
            signature,
            now=moment.date(),
            ring=ring,
            current_key_id=current_key_id,
        )
    except DirectoryExpired:
        verification = None
    except DirectoryVerificationError:
        return None

    if verification is not None:
        directory_version = verification.directory.directory_version
        publishing_key_id = verification.key_id
    else:
        identity = _signed_identity(document_bytes)
        if identity is None:
            return None
        directory_version, publishing_key_id = identity

    return CachedDirectory(
        document_bytes=document_bytes,
        signature_file=signature_file,
        meta=meta,
        directory_version=directory_version,
        publishing_key_id=publishing_key_id,
        verification=verification,
    )


def read_meta(root: Path) -> CacheMeta | None:
    """Read the sidecar alone, for the conditional GET.

    Deliberately independent of :func:`read_cache`: the etag is worth
    replaying even when the document beside it turns out to be unusable, and
    the worst a stale or forged etag can do is provoke one extra download.

    Args:
        root: The cache directory.

    Returns:
        The metadata, or ``None`` when it is absent or unreadable.
    """
    meta_bytes = _read_file(root / META_FILE)
    if meta_bytes is None:
        return None
    try:
        return CacheMeta.from_dict(_parse_object(meta_bytes))
    except CacheMetaError:
        return None


def write_cache(
    root: Path, *, document_bytes: bytes, signature_file: bytes, meta: CacheMeta
) -> None:
    """Replace the cached copy with a new publication, atomically.

    All three files are written to ``<name>.tmp`` and flushed before the first
    rename, so an interruption at any point leaves the previous copy exactly
    as it was and no temporary file behind.

    Args:
        root: The cache directory; created if absent.
        document_bytes: The document as served.
        signature_file: The signature file as served.
        meta: The sidecar to write beside them.

    Raises:
        OSError: If the files cannot be written.
    """
    root.mkdir(parents=True, exist_ok=True)
    _write_atomically(
        root,
        (
            (DOCUMENT_FILE, document_bytes),
            (SIGNATURE_FILE, signature_file),
            (META_FILE, _meta_bytes(meta)),
        ),
    )


def touch_meta(root: Path, *, fetched_at: datetime, etag: str | None) -> CacheMeta:
    """Record a fresh confirmation of the copy already held.

    What a 304 means: the bytes did not change, only the moment this instance
    last heard so. Everything the sidecar says about the *document* is carried
    over unchanged.

    Args:
        root: The cache directory.
        fetched_at: When the confirmation arrived; timezone-aware.
        etag: The etag to replay next time.

    Returns:
        The rewritten metadata.

    Raises:
        CacheMetaError: If there is no readable sidecar to carry over.
        ValueError: If ``fetched_at`` is naive.
        OSError: If the sidecar cannot be written.
    """
    existing = read_meta(root)
    if existing is None:
        raise CacheMetaError(
            f"no readable {META_FILE} under {root}; a confirmation can only be recorded "
            "against a copy this instance already holds"
        )
    updated = CacheMeta(
        schema_version=existing.schema_version,
        directory_version=existing.directory_version,
        publishing_key_id=existing.publishing_key_id,
        fetched_at=fetched_at,
        etag=etag,
        source_url=existing.source_url,
    )
    _write_atomically(root, ((META_FILE, _meta_bytes(updated)),))
    return updated


def _aware(value: datetime, *, argument: str) -> datetime:
    """Refuse a naive timestamp and normalise an aware one to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{argument} must be timezone-aware; a naive timestamp leaves the instant it "
            "names to the reader's locale, which is not a thing a signature window can be "
            "judged against"
        )
    return value.astimezone(timezone.utc)


def _read_file(path: Path) -> bytes | None:
    """Read a file, reporting absence as ``None`` and letting I/O errors out."""
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None


def _parse_object(data: bytes) -> Mapping[str, object]:
    """Parse the sidecar bytes into a JSON object."""
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CacheMetaError(f"{META_FILE} is not valid UTF-8 JSON") from None
    if not isinstance(decoded, dict):
        raise CacheMetaError(f"{META_FILE} is not a JSON object")
    return decoded


def _meta_bytes(meta: CacheMeta) -> bytes:
    """Serialise the sidecar. Indented: it is read by operators, not verified."""
    return json.dumps(meta.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _signed_identity(document_bytes: bytes) -> tuple[int, str] | None:
    """Read ``(directory_version, publishing_key_id)`` out of verified bytes.

    Only ever called for a document whose signature has already verified, so
    this reads published facts rather than claims. It still checks the two
    types: an expired document never reached the shape parser, and a version
    that is not an integer cannot take part in the monotonic rule.
    """
    try:
        decoded = json.loads(document_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    version = decoded.get("directory_version")
    key_id = decoded.get("publishing_key_id")
    if isinstance(version, bool) or not isinstance(version, int) or not isinstance(key_id, str):
        return None
    return version, key_id


def _write_atomically(root: Path, payloads: Sequence[tuple[str, bytes]]) -> None:
    """Write every payload to a temporary file, then rename them into place.

    Every temporary file is written and fsynced *before* the first rename, so
    the window in which the cache is half-replaced is as small as the
    filesystem allows. Any failure removes the temporary files and re-raises:
    the caller learns the write did not happen, and the previous copy is
    untouched.
    """
    temporary: list[Path] = []
    try:
        for name, payload in payloads:
            temporary.append(_write_temporary(root, name, payload))
        for temporary_path, (name, _payload) in zip(temporary, payloads, strict=True):
            os.replace(temporary_path, root / name)
    except BaseException:
        for temporary_path in temporary:
            temporary_path.unlink(missing_ok=True)
        raise


def _write_temporary(root: Path, name: str, payload: bytes) -> Path:
    """Write one payload to ``<name>.tmp`` and flush it to the platter."""
    path = root / f"{name}{_TEMPORARY_SUFFIX}"
    with open(path, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return path


__all__ = [
    "CACHE_SUBDIR",
    "DOCUMENT_FILE",
    "META_FILE",
    "META_SCHEMA_VERSION",
    "SIGNATURE_FILE",
    "CacheMeta",
    "CacheMetaError",
    "CachedDirectory",
    "cache_root_for",
    "read_cache",
    "read_meta",
    "touch_meta",
    "write_cache",
]
