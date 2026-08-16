# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Authentication services.

The package owns the auth-backend abstraction (per ADR-0036 §3) and
the Phase-2 ``LocalPasswordAuthBackend`` implementation. ``OIDCAuthBackend``
will land alongside in Phase 5 — a new sibling module, not a refactor
of the existing one.

The session repository (``SessionRepository``) is also exposed from
this package because it is the storage mechanism the backends share;
keeping it together with the backends makes the auth surface
discoverable in one directory.
"""

from services.auth.backend import AuthBackend
from services.auth.local_password import (
    LOCKOUT_THRESHOLD,
    LOCKOUT_WINDOW,
    LocalPasswordAuthBackend,
)
from services.auth.session import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    SessionDTO,
    SessionRepository,
)

__all__ = [
    "ABSOLUTE_TIMEOUT",
    "IDLE_TIMEOUT",
    "LOCKOUT_THRESHOLD",
    "LOCKOUT_WINDOW",
    "AuthBackend",
    "LocalPasswordAuthBackend",
    "SessionDTO",
    "SessionRepository",
]
