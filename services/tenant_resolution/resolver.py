# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""TenantResolver abstraction and concrete implementations.

Per ADR-0063 §1. A resolver maps a request's ``Host`` header to a
tenant id without opening a tenant-scoped session — at resolution
time no tenant context exists yet, so the lookup goes via the audit
engine (the same RLS-bypass surface used for ``login_audit`` writes
per ADR-0036 §8).

Two implementations ship in this module:

- :class:`SubdomainTenantResolver` — the production path. Looks up
  ``SELECT id FROM tenants WHERE subdomain = :subdomain AND
  is_active = TRUE``.
- :class:`ExplicitHostHeaderResolver` — a deterministic test-only
  variant backed by an in-memory mapping. Tests use it to avoid DNS
  and to keep cross-tenant fixtures reproducible.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from core.exceptions import PortfoliFlowError


class TenantResolutionError(PortfoliFlowError):
    """Base for tenant-resolution failures."""


class UnknownSubdomainError(TenantResolutionError):
    """No active tenant matches the request's subdomain."""


# Environment variable consulted by :class:`SubdomainTenantResolver`
# when the request host is plain ``localhost``. Lets developers point
# the dev server at a chosen tenant without DNS or /etc/hosts edits.
# Unset → ``localhost`` requests raise :class:`UnknownSubdomainError`,
# surfacing the misconfiguration loudly rather than defaulting.
_LOCAL_DEV_ENV_VAR: str = "LOCAL_DEV_TENANT_SUBDOMAIN"


class TenantResolver(ABC):
    """Resolves an incoming request to a tenant id.

    Implementations are stateless and side-effect-free; they receive
    enough request information (typically the ``Host`` header) and
    return a tenant id or raise. Implementations may consult the
    audit engine but never open a tenant-scoped session — at
    resolution time no tenant context exists yet.
    """

    @abstractmethod
    async def resolve(self, *, host: str) -> UUID:
        """Resolve ``host`` to an active tenant id.

        Args:
            host: The request's ``Host`` header, e.g.
                ``"minathena-capital.portfoliflow.net:443"`` or
                ``"localhost:8000"``.

        Returns:
            The UUID of the active tenant the host maps to.

        Raises:
            UnknownSubdomainError: No active tenant matches the host.
        """


class SubdomainTenantResolver(TenantResolver):
    """Resolve via ``tenants.subdomain = host.split('.')[0]``.

    Uses the audit engine (RLS bypass) — the resolver is called
    before any tenant context exists. Reads ``tenants.id`` only when
    ``is_active = TRUE`` so a deactivated tenant cannot be logged
    into.
    """

    def __init__(self, audit_engine: AsyncEngine) -> None:
        """Construct against the audit (RLS-bypass) engine.

        Args:
            audit_engine: The Postgres-superuser-bound engine already
                used for ``login_audit`` writes. The sanctioned-usage
                regression test lists tenant-resolution reads as one
                of the named paths.
        """
        self._engine = audit_engine

    async def resolve(self, *, host: str) -> UUID:
        subdomain = self._extract_subdomain(host)
        if subdomain is None:
            raise UnknownSubdomainError(f"Cannot extract a subdomain from host {host!r}")

        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT id FROM tenants WHERE subdomain = :subdomain AND is_active = TRUE"),
                {"subdomain": subdomain},
            )
            row = result.first()

        if row is None:
            raise UnknownSubdomainError(f"No active tenant for subdomain {subdomain!r}")
        return UUID(str(row.id))

    @staticmethod
    def _extract_subdomain(host: str) -> str | None:
        """Pull the leading label from ``host``.

        - Strips a trailing ``:port`` if present.
        - For any *single-label* host (``localhost``, ``testserver``,
          ``127.0.0.1``, ``::1``) consults :data:`_LOCAL_DEV_ENV_VAR`
          and returns its value if set; otherwise ``None``. The
          fall-through covers dev servers and ASGI test clients,
          which present a single-label host with no real DNS.
        - For two-label hosts ending in ``localhost`` (e.g.
          ``admin.localhost``, ``minathena-capital.localhost``),
          returns the leading label. RFC 6761 reserves the
          ``.localhost`` TLD for local-loopback use; pairing this
          clause with ``/etc/hosts`` entries lets developers exercise
          multiple tenants in parallel browser tabs without DNS or
          an app restart.
        - For other multi-label hosts with fewer than three labels
          (``portfoliflow.net``), returns ``None`` — those are
          apex domains with no tenant subdomain.

        Returns:
            The subdomain string, or ``None`` if none can be derived.
        """
        if not host:
            return None
        bare = host.split(":", 1)[0].strip().lower()
        labels = bare.split(".")
        # Single-label host — dev / test client / IPv4 literal.
        # All of these get routed through LOCAL_DEV_TENANT_SUBDOMAIN.
        if len(labels) == 1 or bare in {"127.0.0.1", "::1"}:
            override = os.getenv(_LOCAL_DEV_ENV_VAR)
            if override:
                return override.strip().lower()
            return None
        # IPv4 literal in dotted form — also dev / test, no subdomain.
        if all(part.isdigit() for part in labels):
            override = os.getenv(_LOCAL_DEV_ENV_VAR)
            if override:
                return override.strip().lower()
            return None
        # Dev convention: <subdomain>.localhost maps to a tenant
        # subdomain. With /etc/hosts entries (e.g. "127.0.0.1
        # admin.localhost minathena-capital.localhost"), each
        # tenant has its own URL — multiple tenants coexist in
        # parallel browser tabs without env-var fiddling.
        if len(labels) == 2 and labels[-1] == "localhost":
            return labels[0]
        if len(labels) < 3:
            return None
        return labels[0]


class ExplicitHostHeaderResolver(TenantResolver):
    """Test-time resolver backed by an explicit subdomain → tenant map.

    Used in tests to avoid DNS and to make cross-tenant fixtures
    deterministic. The contract mirrors
    :class:`SubdomainTenantResolver`.
    """

    def __init__(self, mapping: dict[str, UUID]) -> None:
        """Construct against a subdomain → tenant id mapping.

        Args:
            mapping: Subdomain (lowercase, no port) → active tenant
                UUID. An entry whose value is ``None`` is not
                supported; remove the key instead.
        """
        self._mapping = {k.lower(): v for k, v in mapping.items()}

    async def resolve(self, *, host: str) -> UUID:
        subdomain = SubdomainTenantResolver._extract_subdomain(host)
        if subdomain is None:
            raise UnknownSubdomainError(f"Cannot extract a subdomain from host {host!r}")
        try:
            return self._mapping[subdomain]
        except KeyError as exc:
            raise UnknownSubdomainError(
                f"No tenant registered for subdomain {subdomain!r}"
            ) from exc
