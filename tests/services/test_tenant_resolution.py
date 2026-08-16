# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``services.tenant_resolution``.

Per ADR-0063 §1. Two surfaces are exercised:

- Pure subdomain extraction from ``Host`` headers, including
  production-shaped values, dev-time values, malformed input, and
  IP literals.
- Live-DB ``SubdomainTenantResolver.resolve`` against the audit
  engine — known subdomain, unknown subdomain, deactivated tenant.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from services.tenant_resolution import (
    ExplicitHostHeaderResolver,
    SubdomainTenantResolver,
    UnknownSubdomainError,
)

# Pull live-DB engine + truncate fixtures.
from tests._db_fixtures import (  # noqa: F401  -- fixture re-export
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)


# ---------------------------------------------------------------------------
# Pure subdomain extraction (synchronous, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("minathena-capital.portfoliflow.net", "minathena-capital"),
        ("admin.portfoliflow.net", "admin"),
        ("VWN.PORTFOLIFLOW.NET:443", "vwn"),
        ("minathena-capital.portfoliflow.net:8000", "minathena-capital"),
        ("vwn.staging.portfoliflow.net", "vwn"),
        ("portfoliflow.net", None),
        ("", None),
        ("127.0.0.1:8000", None),
        # *.localhost dev-loopback convention (RFC 6761) — paired
        # with /etc/hosts entries for parallel-tab multi-tenant dev.
        ("admin.localhost:8000", "admin"),
        ("minathena-capital.localhost", "minathena-capital"),
        ("ADMIN.LOCALHOST:443", "admin"),
    ],
)
def test_extract_subdomain(host: str, expected: str | None) -> None:
    assert SubdomainTenantResolver._extract_subdomain(host) == expected


def test_extract_subdomain_localhost_without_env(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_DEV_TENANT_SUBDOMAIN", raising=False)
    assert SubdomainTenantResolver._extract_subdomain("localhost") is None
    assert SubdomainTenantResolver._extract_subdomain("localhost:8000") is None


def test_extract_subdomain_localhost_with_env(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_DEV_TENANT_SUBDOMAIN", "minathena-capital")
    assert SubdomainTenantResolver._extract_subdomain("localhost:8000") == "minathena-capital"


# ---------------------------------------------------------------------------
# SubdomainTenantResolver — live DB
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.asyncio


async def test_subdomain_resolver_finds_active_tenant(
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    tenant_id = await seed_tenant(name="P&P", subdomain="pp")

    resolver = SubdomainTenantResolver(superuser_engine)
    resolved = await resolver.resolve(host="pp.portfoliflow.net")

    assert resolved == tenant_id


async def test_subdomain_resolver_raises_on_unknown_subdomain(
    superuser_engine: AsyncEngine,
) -> None:
    resolver = SubdomainTenantResolver(superuser_engine)
    with pytest.raises(UnknownSubdomainError):
        await resolver.resolve(host="nonexistent.portfoliflow.net")


async def test_subdomain_resolver_raises_on_deactivated_tenant(
    superuser_engine: AsyncEngine,
    seed_tenant,
) -> None:
    # Seed inactive — resolver must treat as unknown so a deactivated
    # tenant cannot be logged into.
    await seed_tenant(name="Inactive", subdomain="frozen", is_active=False)

    resolver = SubdomainTenantResolver(superuser_engine)
    with pytest.raises(UnknownSubdomainError):
        await resolver.resolve(host="frozen.portfoliflow.net")


async def test_subdomain_resolver_raises_on_unextractable_host(
    superuser_engine: AsyncEngine,
) -> None:
    resolver = SubdomainTenantResolver(superuser_engine)
    with pytest.raises(UnknownSubdomainError):
        await resolver.resolve(host="portfoliflow.net")


async def test_subdomain_resolver_handles_dotlocalhost_hosts(
    superuser_engine: AsyncEngine,
    seed_tenant,
    monkeypatch,
) -> None:
    """Multiple tenants resolve from *.localhost hosts in parallel.

    No ``LOCAL_DEV_TENANT_SUBDOMAIN`` is set — the resolver must use
    the URL alone to pick the right tenant. This is what allows
    developers to open one tenant per browser tab against a single
    running dev server.
    """
    monkeypatch.delenv("LOCAL_DEV_TENANT_SUBDOMAIN", raising=False)

    admin_id = await seed_tenant(name="Platform Admin", subdomain="admin")
    pp_id = await seed_tenant(name="Minathena Capital", subdomain="minathena-capital")

    resolver = SubdomainTenantResolver(superuser_engine)
    assert await resolver.resolve(host="admin.localhost:8000") == admin_id
    assert await resolver.resolve(host="minathena-capital.localhost") == pp_id


# ---------------------------------------------------------------------------
# ExplicitHostHeaderResolver — synchronous in-memory map
# ---------------------------------------------------------------------------


async def test_explicit_resolver_returns_mapped_tenant() -> None:
    tid = uuid4()
    resolver = ExplicitHostHeaderResolver({"acme": tid})

    assert await resolver.resolve(host="acme.portfoliflow.net") == tid


async def test_explicit_resolver_raises_on_unmapped_subdomain() -> None:
    resolver = ExplicitHostHeaderResolver({"acme": uuid4()})
    with pytest.raises(UnknownSubdomainError):
        await resolver.resolve(host="other.portfoliflow.net")
