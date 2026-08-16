# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""In-process pairing-code store for the Telegram bot (ADR-0112 §5).

The bridge between two surfaces that share one process: the web app
issues a short-lived code in Admin → Providers & Credentials, and the
bot redeems it when the user sends ``/pair <code>`` from the chat they
want bound. Redeeming proves possession of *that* Telegram chat, which
is why the resulting ``telegram.chat_id`` row is never typed by hand
(``web.routes.provider_credentials._USER_PANEL_EXCLUDED``).

**Why memory and not a table.** The bot runs in a daemon thread inside
the web process and Telegram allows exactly one ``getUpdates`` consumer
per token, so the deployment is single-worker by construction (ADR-0112
§5 Consequences). Issuer and redeemer are therefore always the same
process, and a five-minute code needs neither a migration nor a cleanup
job. The cost is explicit: **a process restart voids pending codes.**
The user simply generates another one.

This module lives in ``services/`` rather than in ``bot/`` or ``web/``
because both import it, and neither may import the other: it is pure
stdlib — no aiogram, no FastAPI, no database — so it drags nothing
across that boundary.

Rules the store enforces (ADR-0112 §5, D4/D5):

* codes come from :mod:`secrets` over an unambiguous alphabet, eight
  characters long;
* one active code per user — issuing a new one invalidates the old, so a
  user who clicks twice cannot leave a second live code behind;
* single use — a successful redeem removes the code;
* five-minute TTL, checked on redeem and swept opportunistically;
* the redeeming dispatcher's tenant must equal the issuing tenant, so a
  code minted in one tenant can never bind a chat in another;
* redeem failures are indistinguishable to the caller (``None`` for
  unknown, expired and wrong-tenant alike) — the *caller* then sends one
  generic reply, so the chat is no oracle for which of the three it was;
* per-chat redeem attempts are throttled, which is what keeps a
  32**8 code space unguessable *in practice* rather than only in theory.

All state is guarded by one :class:`threading.Lock`: the web half runs
on the uvicorn loop and the bot half on the bot thread's loop, so the
two genuinely race.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

#: Code alphabet without the characters that are read back wrongly over a
#: phone screen: no ``I``/``1``, no ``O``/``0``, no ``U`` (mis-heard as
#: ``V``). 32 symbols, so eight characters carry 40 bits.
_ALPHABET = "ABCDEFGHJKLMNPQRSTVWXYZ23456789"

#: Length of an issued code.
CODE_LENGTH = 8

#: How long an issued code stays redeemable.
CODE_TTL = timedelta(minutes=5)

#: Redeem attempts one chat may make inside :data:`THROTTLE_WINDOW`
#: before the store refuses to look at further codes. Sized for humans
#: mistyping a code, not for a search: at five attempts per ten minutes a
#: brute force would need longer than the universe has had.
THROTTLE_MAX_ATTEMPTS = 5
THROTTLE_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True, repr=False)
class PendingPairing:
    """One issued, not-yet-redeemed code.

    Attributes:
        tenant_id: The tenant the issuing session belonged to. A redeem
            from a dispatcher serving another tenant is refused.
        user_id: The user the chat will be bound to.
        expires_at: Timezone-aware UTC expiry.
    """

    tenant_id: UUID
    user_id: UUID
    expires_at: datetime

    def __repr__(self) -> str:
        # The code is the dict key, never a field — but keep the guarantee
        # explicit: nothing here can print a secret.
        return (
            f"PendingPairing(tenant_id={self.tenant_id!r}, "
            f"user_id={self.user_id!r}, expires_at={self.expires_at!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class IssuedCode:
    """The result of :func:`issue_code` — shown once, never stored anywhere else.

    Attributes:
        code: The plaintext code the user types into Telegram.
        expires_at: Timezone-aware UTC expiry, for the "valid until" line.
    """

    code: str
    expires_at: datetime


_LOCK = threading.Lock()

#: code → pending pairing. Module-level and process-lifetime, by design.
_PENDING: dict[str, PendingPairing] = {}

#: chat id → the timestamps of its recent redeem attempts.
_ATTEMPTS: dict[int, list[datetime]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _generate_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def normalise_code(raw: str) -> str:
    """Return ``raw`` in the canonical form codes are stored under.

    Users retype codes from a screen: they add spaces, they add the
    grouping dash they saw somewhere else, and phone keyboards love
    lowercase. All three normalise away.

    Args:
        raw: The code exactly as typed.

    Returns:
        The upper-cased code with whitespace and dashes removed.
    """
    return "".join(raw.split()).replace("-", "").replace("—", "").upper()


def _purge_expired(now: datetime) -> None:
    """Drop expired codes. Caller holds :data:`_LOCK`."""
    for code in [code for code, pending in _PENDING.items() if pending.expires_at <= now]:
        del _PENDING[code]


def issue_code(tenant_id: UUID, user_id: UUID) -> IssuedCode:
    """Mint a fresh code for ``user_id``, invalidating any earlier one.

    Args:
        tenant_id: The issuing session's tenant. Only a dispatcher
            serving this tenant may redeem the code.
        user_id: The user whose chat binding this code will create.

    Returns:
        The :class:`IssuedCode`. Show it once; the store keeps no way to
        show it again.
    """
    now = _now()
    with _LOCK:
        _purge_expired(now)
        # One active code per user: a second click replaces the first
        # rather than leaving two live codes for one account.
        for code in [code for code, pending in _PENDING.items() if pending.user_id == user_id]:
            del _PENDING[code]
        # Re-draw on the (vanishing) chance of a collision with a live code.
        code = _generate_code()
        while code in _PENDING:
            code = _generate_code()
        expires_at = now + CODE_TTL
        _PENDING[code] = PendingPairing(
            tenant_id=tenant_id,
            user_id=user_id,
            expires_at=expires_at,
        )
    return IssuedCode(code=code, expires_at=expires_at)


def revoke_codes_for_user(user_id: UUID) -> int:
    """Drop every pending code of ``user_id``.

    Called when a user revokes their pairing: an outstanding code from
    before the revoke must not be able to re-bind the chat afterwards.

    Args:
        user_id: The user whose pending codes are dropped.

    Returns:
        How many codes were dropped.
    """
    with _LOCK:
        codes = [code for code, pending in _PENDING.items() if pending.user_id == user_id]
        for code in codes:
            del _PENDING[code]
    return len(codes)


def note_attempt(chat_id: int) -> bool:
    """Record a redeem attempt by ``chat_id`` and say whether it may proceed.

    A sliding window: attempts older than :data:`THROTTLE_WINDOW` fall
    out, and the attempt being recorded counts towards the limit. A
    throttled chat is told the same generic "invalid or expired" as a
    wrong code — being throttled is not information a guesser gets to
    have either.

    Args:
        chat_id: The Telegram chat the ``/pair`` command came from.

    Returns:
        ``True`` when the attempt is within the limit, ``False`` when the
        chat has exhausted its window.
    """
    now = _now()
    cutoff = now - THROTTLE_WINDOW
    with _LOCK:
        recent = [stamp for stamp in _ATTEMPTS.get(chat_id, []) if stamp > cutoff]
        recent.append(now)
        _ATTEMPTS[chat_id] = recent
        return len(recent) <= THROTTLE_MAX_ATTEMPTS


def redeem_code(code: str, *, tenant_id: UUID | None) -> UUID | None:
    """Consume ``code`` for a dispatcher serving ``tenant_id``.

    Deliberately single-outcome on failure: unknown code, expired code
    and right-code-wrong-tenant all return ``None``, so the caller has
    exactly one reply to send and the chat learns nothing about which
    case it hit.

    Args:
        code: The code as typed; normalised here.
        tenant_id: The redeeming dispatcher's tenant. ``None`` (a
            dispatcher with no tenant identity — the desktop entry point)
            can never match an issued code and always fails.

    Returns:
        The paired user's id on success, ``None`` on any failure. On
        success the code is consumed and cannot be used again.
    """
    now = _now()
    with _LOCK:
        _purge_expired(now)
        pending = _PENDING.get(normalise_code(code))
        if pending is None or tenant_id is None or pending.tenant_id != tenant_id:
            return None
        del _PENDING[normalise_code(code)]
        return pending.user_id


def pending_code_count() -> int:
    """Return how many codes are currently outstanding (tests, diagnostics)."""
    with _LOCK:
        _purge_expired(_now())
        return len(_PENDING)


def reset_store() -> None:
    """Clear all pending codes and throttle counters.

    For tests and for :func:`bot.telegram_bot.stop_bot`, which voids
    pending codes along with the dispatchers they would have been
    redeemed against.
    """
    with _LOCK:
        _PENDING.clear()
        _ATTEMPTS.clear()


__all__ = [
    "CODE_LENGTH",
    "CODE_TTL",
    "THROTTLE_MAX_ATTEMPTS",
    "THROTTLE_WINDOW",
    "IssuedCode",
    "PendingPairing",
    "issue_code",
    "normalise_code",
    "note_attempt",
    "pending_code_count",
    "redeem_code",
    "reset_store",
    "revoke_codes_for_user",
]
