# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Fixtures for the directory arming layer, and one fixture deliberately removed.

``tests/services/conftest.py`` re-exports ``reset_schema`` from
``tests._db_fixtures``, and that fixture is ``autouse=True``: every test
collected anywhere under ``tests/services/`` otherwise opens the compose
Postgres and truncates every domain table before and after it runs.
:mod:`services.provider_directory` reaches no database at all — it fetches
bytes, writes three files and verifies signatures, and ``test_contract.py``
pins that — so paying a TRUNCATE per assertion would couple a suite that is
database-free by design to a live server for nothing.

The no-op below shadows the inherited fixture by name, which is pytest's
ordinary mechanism for exactly this. It is the same shadow
``tests/services/provider_channel/conftest.py`` carries, for the same reason.
Nothing else is overridden; if a future prompt adds a DB-backed test in this
directory, delete this file rather than working around it.

**No real key material.** The keys below are generated per test. The shipped
publishing ring is exercised in exactly one place — the smoke test in
``test_refresh.py`` — and only against the published fixture, which is a
public document and a public signature. A production private key does not
exist in this repository and must not be made to.

**No literal dates.** ``now`` is derived from the fixture document's own
``issued_at`` rather than written down, so the suite cannot start failing on
the day a window is edited, and cannot pass by accident because someone
guessed a date that happens to sit inside one.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services.provider_channel.directory import sign_directory

#: The published fixture, shared with the provider-channel suite: one real
#: document and its real detached signature, both public by construction.
FIXTURES: Final[Path] = Path(__file__).resolve().parents[1] / "provider_channel" / "fixtures"

#: Ids for the throwaway ring. Deliberately unlike the shipped
#: ``portfoliflow-YYYY-MM`` spelling, so a test key can never be mistaken for
#: a real one in a failure message.
TEST_KEY_ID: Final[str] = "test-2026"
TEST_SUCCESSOR_KEY_ID: Final[str] = "test-2027"


def signature_file_bytes(signature: bytes) -> bytes:
    """Encode a raw signature the way the publisher does (B-D-23)."""
    return signature.hex().encode("ascii") + b"\n"


@pytest.fixture(autouse=True)
def reset_schema() -> None:
    """Shadow the inherited autouse DB fixture; these tests need no database."""
    return None


@pytest.fixture
def throwaway_keypair() -> tuple[bytes, bytes]:
    """A per-test Ed25519 key pair as ``(private seed, public bytes)``."""
    private_key = Ed25519PrivateKey.generate()
    return private_key.private_bytes_raw(), private_key.public_key().public_bytes_raw()


@pytest.fixture
def successor_keypair() -> tuple[bytes, bytes]:
    """A second per-test key pair, standing in for the announced successor."""
    private_key = Ed25519PrivateKey.generate()
    return private_key.private_bytes_raw(), private_key.public_key().public_bytes_raw()


@pytest.fixture
def test_ring(
    throwaway_keypair: tuple[bytes, bytes], successor_keypair: tuple[bytes, bytes]
) -> dict[str, bytes]:
    """A two-key ring: :data:`TEST_KEY_ID` current, :data:`TEST_SUCCESSOR_KEY_ID` next."""
    return {
        TEST_KEY_ID: throwaway_keypair[1],
        TEST_SUCCESSOR_KEY_ID: successor_keypair[1],
    }


@pytest.fixture
def fixture_document() -> dict[str, object]:
    """The shipped publication, parsed — a template for test documents only.

    Never signed again as-is: it names the real publishing key, and the test
    ring does not hold it.
    """
    decoded = json.loads((FIXTURES / "directory-1.json").read_bytes().decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


@pytest.fixture
def make_document(
    fixture_document: dict[str, object],
) -> Callable[..., dict[str, object]]:
    """Build a test-ring document from the published one, with overrides.

    The successor announcement is dropped by default: the published one names
    a key the *shipped* ring holds and the test ring does not, which would put
    a "client update required" notice on every unrelated assertion.
    """

    def factory(**overrides: object) -> dict[str, object]:
        document = dict(fixture_document)
        document["publishing_key_id"] = TEST_KEY_ID
        document.pop("successor_key", None)
        document.update(overrides)
        return document

    return factory


@pytest.fixture
def make_signed() -> Callable[[Mapping[str, object], bytes], tuple[bytes, bytes]]:
    """Sign a document, returning ``(canonical bytes, B-D-23 signature file)``."""

    def factory(document: Mapping[str, object], private_key: bytes) -> tuple[bytes, bytes]:
        payload, signature = sign_directory(document, private_key=private_key)
        return payload, signature_file_bytes(signature)

    return factory


@pytest.fixture
def now(fixture_document: dict[str, object]) -> datetime:
    """One day after the fixture's ``issued_at``, at noon UTC.

    Derived, never written down: the documents under test inherit the
    fixture's validity window, so an edited window moves this instant with it
    instead of quietly falling outside.
    """
    issued_at = fixture_document["issued_at"]
    assert isinstance(issued_at, str)
    return datetime.fromisoformat(issued_at).replace(hour=12, tzinfo=timezone.utc) + timedelta(
        days=1
    )


@pytest.fixture
def cache_root(tmp_path: Path) -> Path:
    """The cache directory for one test, not yet created."""
    return tmp_path / "provider_directory"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client whose transport ``httpx_mock`` intercepts."""
    async with httpx.AsyncClient() as instance:
        yield instance
