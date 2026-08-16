# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Shared user-field validation — one email check, one role check.

Per ADR-0121 §2. Both functions were private helpers of
:mod:`services.super_admin.operations` (``_validate_email``,
``_validate_roles``). The tenant-scoped user service
(:mod:`services.tenant_users`, ADR-0121 §1) applies exactly the same two
rules, and a second copy would fork the rule rather than reuse it. The
bodies are moved verbatim: same regex, same cleaning, same messages.

The two exception types move with the functions, which is what decides
the module boundary: :mod:`services.super_admin.operations` imports this
module, so this module cannot import back from it. Every established
import path is preserved — the super-admin module re-imports both names,
so ``services.super_admin.operations.EmailInvalidError`` and the
``services.super_admin`` package re-export resolve to these classes and
the CLI, web routes, and tests are untouched.

One deliberate difference comes with the move: the two errors now derive
from :class:`core.exceptions.PortfoliFlowError` directly rather than from
``SuperAdminOperationError``, which cannot be named here without a cycle.
Every in-tree handler catches them explicitly ahead of its base-class
clause (``cli/create_tenant.py``, ``cli/create_user.py``,
``cli/create_super_admin.py``, ``web/routes/super_admin.py``), so no
``except`` site changes meaning.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from core.exceptions import PortfoliFlowError
from core.repositories.user_repository import ALLOWED_ROLES

__all__ = [
    "EmailInvalidError",
    "RoleInvalidError",
    "validate_email",
    "validate_roles",
]

# Basic structural email check — same forgiving form the CLIs use.
_EMAIL_RE: re.Pattern[str] = re.compile(r"^.+@.+\..+$")


class EmailInvalidError(PortfoliFlowError):
    """The email doesn't match the basic structural check."""


class RoleInvalidError(PortfoliFlowError):
    """One or more requested roles are not in :data:`ALLOWED_ROLES`."""


def validate_email(value: str) -> str:
    """Return ``value`` stripped, or raise if it is not email-shaped.

    The check is deliberately forgiving — a structural
    ``something@something.something`` test, not RFC 5322. Deliverability
    is not knowable at write time and is not this function's business.

    Args:
        value: The raw email address as typed by the operator or owner.

    Returns:
        The address with surrounding whitespace removed. Case is left
        alone: ``uq_users_tenant_email`` is case-sensitive and the
        stored form is what the login path compares against.

    Raises:
        EmailInvalidError: If the cleaned value is not email-shaped.
    """
    cleaned = value.strip()
    if not _EMAIL_RE.match(cleaned):
        raise EmailInvalidError(f"invalid email {cleaned!r}")
    return cleaned


def validate_roles(roles: Sequence[str]) -> list[str]:
    """Return the cleaned role list, or raise if any role is unknown.

    Roles are stripped and lower-cased, empty entries are dropped, and
    what remains is checked against
    :data:`core.repositories.user_repository.ALLOWED_ROLES` — the mirror
    of the ``ck_users_roles_values`` CHECK constraint. Validating here
    turns a database constraint violation into a typed error the calling
    surface can render.

    Args:
        roles: The requested roles, in any case and with any surrounding
            whitespace.

    Returns:
        The cleaned roles, in the order given.

    Raises:
        RoleInvalidError: If nothing survives cleaning, or if any role is
            outside :data:`ALLOWED_ROLES`.
    """
    cleaned = [r.strip().lower() for r in roles if r and r.strip()]
    if not cleaned:
        raise RoleInvalidError("roles must be a non-empty sequence")
    bad = [r for r in cleaned if r not in ALLOWED_ROLES]
    if bad:
        raise RoleInvalidError(f"unknown role(s) {bad!r}; allowed {sorted(ALLOWED_ROLES)!r}")
    return cleaned
