# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Route-level exception masking for user-facing responses.

Several web routes interpolate exception text into HTTP responses. For
exceptions *outside* the project's own error hierarchy, ``str(exc)`` can
leak file paths, SQL fragments, or upstream-provider error bodies — an
information-disclosure risk that weighs heavier under multi-tenant
operation.

The carve-out is deliberate: the :class:`~core.exceptions.PortfoliFlowError`
hierarchy carries *intentionally user-facing* messages — Excel import
diagnostics especially — and those pass through verbatim. Every other
exception is masked behind a generic message and logged with a short
correlation id so operators can trace it from the id shown to the user.
"""

from __future__ import annotations

import logging
import uuid

from core.exceptions import PortfoliFlowError

logger = logging.getLogger(__name__)


def user_safe_error(exc: Exception) -> tuple[str, str]:
    """Return ``(user_message, error_id)`` for a route-level exception.

    :class:`~core.exceptions.PortfoliFlowError` instances carry
    deliberately user-facing messages (e.g. Excel import diagnostics)
    and pass through verbatim. Any other exception is masked behind a
    generic message; the full exception is logged at ERROR level with a
    short correlation id so operators can trace it from the id shown to
    the user.

    Args:
        exc: The exception caught at a route boundary.

    Returns:
        A ``(user_message, error_id)`` tuple. ``error_id`` is a short
        8-hex correlation id present in both the returned message (for
        foreign exceptions) and the ERROR log record. For
        ``PortfoliFlowError`` the message is ``str(exc)`` verbatim; for
        any other exception it is a generic string embedding
        ``error_id``.
    """
    error_id = uuid.uuid4().hex[:8]

    if isinstance(exc, PortfoliFlowError):
        # Deliberately user-facing (e.g. Excel import diagnostics): pass
        # the message through verbatim, but still log at ERROR with the
        # id so even pass-through errors are traceable by operators.
        logger.error(
            "route error (ref %s): %s: %s",
            error_id,
            type(exc).__name__,
            exc,
        )
        return str(exc), error_id

    # Foreign exception: log the full detail with traceback for
    # operators, and hand the user only the correlation id. Passing the
    # exception instance as ``exc_info`` (rather than ``True``) does not
    # depend on ``sys.exc_info()``, so the traceback is captured whether
    # or not the helper is called inside an active ``except`` block.
    logger.error(
        "route error (ref %s): %s: %s",
        error_id,
        type(exc).__name__,
        exc,
        exc_info=exc,
    )
    user_message = f"Something went wrong (ref {error_id}). Please try again or contact support."
    return user_message, error_id
