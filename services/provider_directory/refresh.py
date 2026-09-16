# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""One refresh: fetch, verify, decide whether to keep it (SB-3b).

This is the whole arming decision in one function. It fetches the published
directory, hands the bytes to the shipped trust gate, applies the monotonic
version rule, and either replaces the cached copy or refuses to — and it
*reports* which of those happened rather than raising, because "the directory
could not be refreshed today" is an ordinary operating condition and not an
error in the program.

**Refusals never touch the cache.** Every outcome whose name begins with
``refused_`` leaves all three files byte-identical. A client that cannot
obtain a directory it can believe keeps the last one it could — never
nothing, and never something worse.

**Monotonic, and expiry does not reset it** (B-D-15). A version lower than
the cached one is refused as a downgrade, which is what a rollback attack
looks like from here: an attacker who can serve bytes replays an older,
properly signed publication to bring back a provider that was removed. The
cached document stays the baseline for that comparison even after its window
has passed, because a stale copy still proves which version this instance has
already seen.

**The same version with different bytes is refused too.** Re-publishing under
a version already seen is either a publishing mistake or an attempt to change
the list without admitting to a new version; both deserve an operator's
attention rather than a silent overwrite.

Notices are plain English for the operator log. This module decides no
wording for a user surface and reads no clock: ``now`` is injected (D-clock),
and must be timezone-aware.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

import httpx

from services.provider_channel.directory import (
    DirectoryVerificationError,
    PublishingKeyNotConfigured,
    UnknownDirectoryFormatVersion,
)
from services.provider_channel.publishing_key import (
    PUBLISHING_KEY_ID,
    PUBLISHING_KEY_RING,
    is_placeholder,
)
from services.provider_channel.ring import (
    RingVerification,
    UnknownPublishingKeyId,
    verify_directory_with_ring,
)
from services.provider_directory.cache import (
    CacheMeta,
    META_SCHEMA_VERSION,
    read_cache,
    read_meta,
    touch_meta,
    write_cache,
)
from services.provider_directory.fetch import (
    DEFAULT_TIMEOUT,
    DIRECTORY_URL,
    DirectoryUnavailable,
    SignatureFileError,
    decode_signature_file,
    fetch_directory,
    signature_url_for,
)
from services.provider_directory.provenance import Provenance, provenance_from_cache

#: What one refresh did.
#:
#: ``updated`` — a newer publication was verified and stored;
#: ``unchanged`` — the server confirmed the copy already held;
#: ``refused_downgrade`` — a lower version than the cached one (B-D-15);
#: ``refused_republished`` — the cached version, re-published with other bytes;
#: ``refused_unknown_version`` — a ``format_version`` this build cannot read;
#: ``refused_unknown_key`` — a ``publishing_key_id`` outside the shipped ring;
#: ``refused_invalid`` — every other refusal of the trust gate;
#: ``unavailable`` — the document could not be obtained at all.
RefreshStatus = Literal[
    "updated",
    "unchanged",
    "refused_downgrade",
    "refused_republished",
    "refused_unknown_version",
    "refused_unknown_key",
    "refused_invalid",
    "unavailable",
]

#: Every :data:`RefreshStatus`, in declaration order.
REFRESH_STATUSES: Final[tuple[RefreshStatus, ...]] = (
    "updated",
    "unchanged",
    "refused_downgrade",
    "refused_republished",
    "refused_unknown_version",
    "refused_unknown_key",
    "refused_invalid",
    "unavailable",
)

#: The notice that distinguishes "this build is too old" from "this document
#: is wrong" — the two refusals an operator must act on differently.
_CLIENT_UPDATE_REQUIRED: Final[str] = "client update required"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one refresh did, and what the instance holds afterwards.

    Attributes:
        status: The outcome.
        notices: English lines for the operator log, in the order they arose.
            Empty is normal.
        provenance: The cache **after** the call — unchanged for every
            refusal, so a caller can always say what the instance is standing
            on. ``None`` only when no usable copy exists at all.
    """

    status: RefreshStatus
    notices: tuple[str, ...]
    provenance: Provenance | None


async def refresh_directory(
    *,
    cache_root: Path,
    client: httpx.AsyncClient,
    now: datetime,
    url: str = DIRECTORY_URL,
    ring: Mapping[str, bytes] = PUBLISHING_KEY_RING,
    current_key_id: str = PUBLISHING_KEY_ID,
    timeout: float = DEFAULT_TIMEOUT,
) -> RefreshOutcome:
    """Fetch the directory and keep it only if it is better than what is held.

    The steps, in order: validate the caller's arguments; read and re-verify
    what is cached; fetch conditionally on the stored etag; decode and verify
    the served bytes against the ring; apply the monotonic version rule;
    write, touch or refuse.

    Args:
        cache_root: Where the copy lives, typically
            :func:`~services.provider_directory.cache.cache_root_for`. Created
            if absent.
        client: The HTTP client to fetch with; the caller owns its lifetime.
        now: The instant of this refresh; timezone-aware, injected rather than
            read from a clock (D-clock).
        url: The document URL; must end in ``.json``.
        ring: The keys a document may be verified with, by id.
        current_key_id: The id this build considers current.
        timeout: Per-request timeout in seconds.

    Returns:
        The outcome, including the provenance of whatever the instance holds
        afterwards. Network failures and every refusal of the trust gate are
        reported here, never raised.

    Raises:
        ValueError: If ``now`` is naive, or ``url`` does not end in ``.json``.
        UnknownPublishingKeyId: If ``current_key_id`` is not in ``ring``.
            Checked here rather than left to the ring, which raises the same
            type for a *document* that names an unknown id — two very
            different events that must not arrive as one.
        PublishingKeyNotConfigured: If the current ring entry is the
            un-minted placeholder.
        OSError: If ``cache_root`` cannot be created, read or written.
    """
    # (1) Caller errors, before any I/O.
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            "now must be timezone-aware; the validity window of a signed document "
            "cannot be judged against an instant whose offset is unstated"
        )
    if current_key_id not in ring:
        raise UnknownPublishingKeyId(
            f"current_key_id {current_key_id!r} is not in the key ring {sorted(ring)}; "
            "this is a misconfigured caller, not a rejected document"
        )
    if is_placeholder(ring[current_key_id]):
        raise PublishingKeyNotConfigured(
            "the current ring entry is the un-minted placeholder key; refreshing the "
            "directory fails closed until a real key is shipped"
        )
    signature_url_for(url)
    cache_root.mkdir(parents=True, exist_ok=True)

    def outcome(status: RefreshStatus, *notices: str) -> RefreshOutcome:
        """Close the call, describing whatever the cache holds at that point."""
        return RefreshOutcome(
            status=status,
            notices=notices,
            provenance=_provenance(cache_root, now=now, ring=ring, current_key_id=current_key_id),
        )

    # (2) What is held now. The etag is read separately: it is worth replaying
    #     even when the document beside it is unusable, and a 304 answered to a
    #     forged etag lands in the "no cache" branch below anyway.
    cached = read_cache(cache_root, now=now, ring=ring, current_key_id=current_key_id)
    stored_meta = read_meta(cache_root)
    etag = stored_meta.etag if stored_meta is not None else None

    # (3) Fetch.
    try:
        fetched = await fetch_directory(client, url=url, etag=etag, timeout=timeout)
    except DirectoryUnavailable as exc:
        return outcome("unavailable", str(exc))

    # (4) "What you have is current."
    if fetched is None:
        if cached is None:
            return outcome("unavailable", "server answered 304 but no usable cache exists")
        touch_meta(cache_root, fetched_at=now, etag=etag)
        return outcome("unchanged")

    # (5) The signature file's encoding.
    try:
        signature = decode_signature_file(fetched.signature_file)
    except SignatureFileError as exc:
        return outcome("refused_invalid", str(exc))

    # (6) The trust gate, with its refusals kept apart.
    try:
        verification = verify_directory_with_ring(
            fetched.document_bytes,
            signature,
            now=now.date(),
            ring=ring,
            current_key_id=current_key_id,
        )
    except UnknownDirectoryFormatVersion as exc:
        return outcome("refused_unknown_version", str(exc), _CLIENT_UPDATE_REQUIRED)
    except UnknownPublishingKeyId as exc:
        return outcome("refused_unknown_key", str(exc), _CLIENT_UPDATE_REQUIRED)
    except DirectoryVerificationError as exc:
        return outcome("refused_invalid", str(exc))

    # (7) Monotonic acceptance (B-D-15).
    served_version = verification.directory.directory_version
    if cached is not None:
        if served_version < cached.directory_version:
            return outcome(
                "refused_downgrade",
                f"server serves version {served_version}, cached is {cached.directory_version}",
            )
        if served_version == cached.directory_version:
            if fetched.document_bytes != cached.document_bytes:
                return outcome(
                    "refused_republished",
                    f"version {served_version} was re-published without a version bump",
                )
            touch_meta(cache_root, fetched_at=now, etag=fetched.etag)
            return outcome("unchanged", *_rotation_notices(verification))

    write_cache(
        cache_root,
        document_bytes=fetched.document_bytes,
        signature_file=fetched.signature_file,
        meta=CacheMeta(
            schema_version=META_SCHEMA_VERSION,
            directory_version=served_version,
            publishing_key_id=verification.key_id,
            fetched_at=now,
            etag=fetched.etag,
            source_url=url,
        ),
    )
    return outcome("updated", *_rotation_notices(verification))


def _rotation_notices(verification: RingVerification) -> tuple[str, ...]:
    """Report what the accepted document says about key rotation.

    Reported, never enforced: the document has already verified against a key
    that was in the shipped ring, which is the whole of what B-D-14 asks
    before it is believed. These lines tell an operator that a client update
    is coming, in time to plan it.
    """
    notices: list[str] = []
    if verification.successor_in_use:
        notices.append(f"successor key {verification.key_id} in use")
    if verification.announced_successor == "not_in_ring":
        notices.append(
            "announced successor not in shipped ring; client update required before rotation"
        )
    elif verification.announced_successor == "contradicts_ring":
        notices.append("announced successor contradicts the shipped ring")
    return tuple(notices)


def _provenance(
    cache_root: Path, *, now: datetime, ring: Mapping[str, bytes], current_key_id: str
) -> Provenance | None:
    """Describe the cache as it stands, by reading it back.

    Read back rather than carried forward: after a write or a touch, the
    provenance that matters is the one a *next* reader would compute, and the
    only way to be sure those two agree is to take the same path.
    """
    cached = read_cache(cache_root, now=now, ring=ring, current_key_id=current_key_id)
    if cached is None:
        return None
    return provenance_from_cache(cached, current_key_id=current_key_id)


__all__ = [
    "REFRESH_STATUSES",
    "RefreshOutcome",
    "RefreshStatus",
    "refresh_directory",
]
