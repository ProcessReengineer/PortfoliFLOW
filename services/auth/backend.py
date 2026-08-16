# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Abstract authentication backend.

Per ADR-0036 §3, all authentication paths (Phase-2 local password,
Phase-5 OIDC, future passkeys) implement this interface. The login
route and the session middleware speak only to it; the choice of
concrete backend is configurable per tenant in Phase 5 and pinned to
``LocalPasswordAuthBackend`` in Phase 2.

Adding OIDC in Phase 5 is therefore a *new implementation* sibling to
the local one, not a refactor of the auth path. That is the only
architectural reason this sub-stream ships an abstraction with a
single concrete subclass — the abstraction is load-bearing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.repositories.user_repository import UserDTO


class AuthBackend(ABC):
    """Authenticate a user via some mechanism.

    The interface is intentionally minimal:

    - :meth:`authenticate` returns a ``UserDTO`` on success or
      ``None`` on any failure (unknown user, wrong password, locked
      account, deactivated account, malformed credentials). Callers
      do not learn *why* authentication failed — that is a security
      property, not an API limitation. The detailed reason is recorded
      in the ``login_audit`` table for the audit trail; the user-
      facing message is always a generic "Invalid credentials".
    """

    @abstractmethod
    async def authenticate(
        self,
        email: str,
        password: str | None = None,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserDTO | None:
        """Authenticate the credentials and return the user on success.

        Args:
            email: The user's email address. Required for all backends.
            password: The plaintext password (local backend) or
                ``None`` for backends that do not consume it (OIDC).
            ip_address: Caller's IP address, recorded in the audit
                trail. ``None`` is acceptable but degrades the audit
                surface.
            user_agent: Caller's User-Agent header, recorded in the
                audit trail.

        Returns:
            The :class:`UserDTO` for the authenticated user, or
            ``None`` if authentication failed for any reason.
        """
