# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Arming the provider directory: obtain it, keep it, say where it came from.

:mod:`services.provider_channel` answers *may this document be believed?* and
nothing else — no network, no disk, no state. This package is the layer that
brings it a document to judge: it fetches the publication over HTTPS, keeps
the two published files under ``DATA_DIR`` with a small unsigned sidecar, and
reports the provenance of whatever the instance is currently standing on.

The split is the point. Verification lives in a package that cannot reach the
network or the filesystem, so nothing it decides can depend on where the bytes
came from; acquisition lives here, where it cannot decide anything about
trust. The two meet at exactly one call —
:func:`~services.provider_channel.ring.verify_directory_with_ring` — and the
local copy is re-verified through it on every single read, because a file on
this instance's own disk is no more trustworthy than its signature.

**What this package deliberately does not do.** It reads no setting, so
nothing here consults ``provider_channel.enabled``; it runs on no timer and
schedules nothing; it touches no database and renders no wording for an
operator. A caller decides when to refresh, resolves ``DATA_DIR`` itself, and
chooses what to say about the result — the CLI first, the surfaces later
(SB-6).

**Decisions of record carried forward.** Trust comes from the signature and
never from the transport (B-D-13). Acceptance is monotonic: a version below
the one already held is a refused downgrade, and an expired copy remains that
baseline (B-D-15). Rotation happens by shipping a new ring, and a successor
announcement is reported as a notice, never adopted as a key (B-D-14). No
clock is read anywhere in the package — the instant is an argument, and it
must carry a timezone (D-clock).
"""

from __future__ import annotations

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
from services.provider_directory.fetch import (
    DEFAULT_TIMEOUT,
    DIRECTORY_SIGNATURE_URL,
    DIRECTORY_URL,
    DirectoryUnavailable,
    FetchedDirectory,
    SignatureFileError,
    decode_signature_file,
    fetch_directory,
    signature_url_for,
)
from services.provider_directory.provenance import (
    Provenance,
    provenance_from_cache,
    provenance_to_dict,
)
from services.provider_directory.refresh import (
    REFRESH_STATUSES,
    RefreshOutcome,
    RefreshStatus,
    refresh_directory,
)

__all__ = [
    "CACHE_SUBDIR",
    "DEFAULT_TIMEOUT",
    "DIRECTORY_SIGNATURE_URL",
    "DIRECTORY_URL",
    "DOCUMENT_FILE",
    "META_FILE",
    "META_SCHEMA_VERSION",
    "REFRESH_STATUSES",
    "SIGNATURE_FILE",
    "CacheMeta",
    "CacheMetaError",
    "CachedDirectory",
    "DirectoryUnavailable",
    "FetchedDirectory",
    "Provenance",
    "RefreshOutcome",
    "RefreshStatus",
    "SignatureFileError",
    "cache_root_for",
    "decode_signature_file",
    "fetch_directory",
    "provenance_from_cache",
    "provenance_to_dict",
    "read_cache",
    "read_meta",
    "refresh_directory",
    "signature_url_for",
    "touch_meta",
    "write_cache",
]
