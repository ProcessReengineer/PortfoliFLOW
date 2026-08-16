# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bot-token discovery across every tenant (ADR-0112 §5).

Answers one question at bot start: *which tenants have a Telegram bot,
and what is each one's token?* The answer is the dispatcher set
:mod:`bot.telegram_bot` multiplexes over — one ``Bot`` + ``Dispatcher``
+ polling task per entry.

**Why the superuser engine.** The scan spans every tenant and runs
before any tenant context exists, so it cannot go through the ordinary
RLS-scoped path — exactly the shape of
:func:`services.irene.scheduling.find_due_tenants`, which the Irene tick
runs on the same engine for the same reason (ADR-0086). The engine is
built on the bot's own event loop from the URL the web lifespan injects,
used once, and disposed; the bot never reads ``DATABASE_URL_SUPERUSER``
itself (``cli/_db.py`` remains the only reader of that variable). The
regression guard
``tests/regression/test_audit_engine_only_writes_login_audit.py`` lists
this module as the fourth sanctioned superuser-engine consumer and pins
the read to ``scoped_settings`` alone.

**Why a scan and not the resolver.** The credential façade
(:class:`services.investments.credential_resolver.CredentialResolver`)
resolves *for a known tenant*: it needs a tenant-scoped session, which is
precisely what does not exist yet when the question is "which tenants are
there". Discovery therefore reads the rows directly and decrypts them
with the same :class:`~services.credential_vault.VaultCipher` the
resolver would have used — same ciphertext, same master key, one layer
lower. Single-dispatcher code paths that ask whether Telegram is enabled
*for a tenant they already have* keep using ``resolve_config``.

Rules (ADR-0112 §5, D2):

* a tenant gets a dispatcher **iff** an enabled ``bot_token`` row
  decrypts. An ``enabled`` row is opt-out only: absent means enabled,
  ``"false"`` means skip (with an INFO, so a silenced bot is visible in
  the log);
* a :class:`~services.credential_vault.VaultDecryptError` skips **that
  tenant** with an ERROR naming the row id and the tenant — never a
  token value — and the other dispatchers start regardless. One
  mis-rotated row must not take a whole deployment's bots down;
* the environment token is *additive*: it becomes one more dispatcher for
  the tenant ``SHIRLEY_BOT_TENANT_SUBDOMAIN`` names, and only when that
  tenant has no enabled row of its own. Scope precedence — the tenant's
  own row wins, as everywhere else in ADR-0112 §1;
* **token changes apply on restart.** There is no rescan timer in v1;
  the admin surface and ``docs/deploy/telegram-multi-bot.md`` both say so.

Nothing here imports aiogram: discovery is a plain async function over an
engine plus two settings values, so it is testable without a bot and
without a network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from services.credential_vault import (
    MASTER_KEY_ENV_VAR,
    VaultCipher,
    VaultDecryptError,
    is_vault_configured,
)

logger = logging.getLogger(__name__)

#: The taxonomy provider these rows belong to (ADR-0112 §3).
_PROVIDER = "telegram"

#: The two tenant-scope fields discovery reads: the secret and its
#: opt-out switch.
_TOKEN_KEY = "bot_token"
_ENABLED_KEY = "enabled"

#: Values of the ``enabled`` config row that mean "do not start this
#: tenant's bot". Anything else — including an absent row — means start.
_FALSEY = frozenset({"false", "0", "no", "off"})

#: Source labels a discovered token can carry.
SOURCE_VAULT = "vault"
SOURCE_ENV_FALLBACK = "env-fallback"


@dataclass(frozen=True, repr=False)
class DiscoveredBot:
    """One tenant's bot token, ready to be turned into a dispatcher.

    Inert in logs by construction: :func:`repr` (hence :func:`str`, hence
    any f-string, log line or traceback that renders it) shows the tenant
    and the source but never the token — the same discipline
    :class:`services.investments.credential_resolver.ProviderCredential`
    keeps for credential payloads (ADR-0095 §4).

    Attributes:
        tenant_id: The tenant this bot serves. ``None`` only for the
            environment token on an entry point that resolved no tenant
            (the desktop path), where the Postgres-native tools degrade
            gracefully anyway.
        token: The BotFather token. Never logged, never rendered.
        source: :data:`SOURCE_VAULT` or :data:`SOURCE_ENV_FALLBACK`.
    """

    tenant_id: UUID | None
    token: str
    source: str

    def __repr__(self) -> str:
        return (
            f"DiscoveredBot(tenant_id={self.tenant_id!r}, source={self.source!r}, token=<masked>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class _TenantRows:
    """The two rows one tenant may carry, as read from the scan."""

    token_row_id: UUID | None = None
    token_ciphertext: bytes | None = None
    token_row_enabled: bool = True
    disabled_by_flag: bool = False


async def discover_bot_tokens(
    engine: AsyncEngine,
    *,
    env_token: str = "",
    env_tenant_id: UUID | None = None,
) -> list[DiscoveredBot]:
    """Return every bot token that should get a dispatcher.

    Args:
        engine: A superuser :class:`~sqlalchemy.ext.asyncio.AsyncEngine`
            (RLS-bypassing, per the module docstring). Pass ``None``-free:
            callers with no superuser URL skip discovery entirely rather
            than passing a placeholder.
        env_token: ``TELEGRAM_BOT_TOKEN`` from the environment. Empty when
            the deployment has moved fully to per-tenant tokens.
        env_tenant_id: The tenant the environment token is bound to — what
            the web lifespan resolved ``SHIRLEY_BOT_TENANT_SUBDOMAIN`` to.

    Returns:
        The discovered bots, vault entries first (ordered by tenant id so
        the start-up log is stable), the environment fallback last. Empty
        when nothing is configured anywhere — the caller then no-ops.

    Raises:
        Nothing on a per-tenant problem: a decrypt failure or a disabled
        row skips that tenant and logs. Only a failure of the scan itself
        (an unreachable database) propagates, and the caller treats even
        that as "run with what the environment gives us".
    """
    per_tenant = await _scan(engine)

    discovered: list[DiscoveredBot] = []
    cipher: VaultCipher | None = None
    vault_ready = is_vault_configured()
    if per_tenant and not vault_ready:
        logger.warning(
            "Telegram bot discovery: %s is not set, so no stored bot token "
            "can be decrypted; only an environment token can serve. See "
            "docs/deploy/credential-vault.md.",
            MASTER_KEY_ENV_VAR,
        )

    for tenant_id in sorted(per_tenant, key=str):
        rows = per_tenant[tenant_id]
        if rows.token_ciphertext is None:
            continue
        if rows.disabled_by_flag:
            logger.info(
                "Telegram bot discovery: tenant %s has a stored token but "
                "telegram.enabled is false; not starting its bot.",
                tenant_id,
            )
            continue
        if not rows.token_row_enabled:
            logger.info(
                "Telegram bot discovery: tenant %s has a disabled bot_token "
                "row; not starting its bot.",
                tenant_id,
            )
            continue
        if not vault_ready:
            continue
        if cipher is None:
            cipher = VaultCipher.from_env()
        try:
            token = cipher.decrypt(rows.token_ciphertext)
        except VaultDecryptError:
            # One mis-rotated row is an operator problem for *that* tenant;
            # every other bot still starts. Names the row so it can be
            # found and rewritten, never the value.
            logger.error(
                "Telegram bot discovery: row %s (tenant %s, telegram.bot_token) "
                "could not be decrypted with the active master key; skipping "
                "this tenant's bot. See docs/deploy/credential-vault.md.",
                rows.token_row_id,
                tenant_id,
            )
            continue
        if not token.strip():
            logger.error(
                "Telegram bot discovery: row %s (tenant %s, telegram.bot_token) "
                "decrypted to an empty value; skipping this tenant's bot.",
                rows.token_row_id,
                tenant_id,
            )
            continue
        discovered.append(DiscoveredBot(tenant_id=tenant_id, token=token, source=SOURCE_VAULT))

    env_bot = _env_fallback(discovered, env_token=env_token, env_tenant_id=env_tenant_id)
    if env_bot is not None:
        discovered.append(env_bot)
    return discovered


def _env_fallback(
    discovered: list[DiscoveredBot],
    *,
    env_token: str,
    env_tenant_id: UUID | None,
) -> DiscoveredBot | None:
    """Return the additive environment dispatcher, or ``None``.

    Additive, not modal (ADR-0112 §5, D3): the environment token spawns
    one *more* dispatcher rather than switching the bot into a different
    mode — but it stands down for the tenant it is bound to as soon as
    that tenant stores its own token, which is the ordinary scope
    precedence of ADR-0112 §1 applied to a process-level resource.
    """
    if not env_token.strip():
        return None
    if env_tenant_id is not None and any(bot.tenant_id == env_tenant_id for bot in discovered):
        logger.info(
            "Telegram bot discovery: tenant %s carries its own bot token, so "
            "the environment TELEGRAM_BOT_TOKEN is not used.",
            env_tenant_id,
        )
        return None
    return DiscoveredBot(
        tenant_id=env_tenant_id,
        token=env_token.strip(),
        source=SOURCE_ENV_FALLBACK,
    )


async def _scan(engine: AsyncEngine) -> dict[UUID, _TenantRows]:
    """Read the ``scoped_settings`` rows every tenant's bot needs.

    One statement, two keys, tenant scope only. The engine bypasses RLS
    by construction, which is the point: this is a platform-level scan
    that runs before any tenant context exists.
    """
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id, tenant_id, key, value_ciphertext, enabled, value_plain "
                "FROM scoped_settings "
                "WHERE scope = 'tenant' AND provider = :provider "
                "AND key IN (:token_key, :enabled_key)"
            ),
            {
                "provider": _PROVIDER,
                "token_key": _TOKEN_KEY,
                "enabled_key": _ENABLED_KEY,
            },
        )
        rows = list(result)

    per_tenant: dict[UUID, _TenantRows] = {}
    for row in rows:
        if row.tenant_id is None:
            continue
        current = per_tenant.get(row.tenant_id, _TenantRows())
        if row.key == _TOKEN_KEY:
            current = _TenantRows(
                token_row_id=row.id,
                token_ciphertext=row.value_ciphertext,
                token_row_enabled=bool(row.enabled),
                disabled_by_flag=current.disabled_by_flag,
            )
        else:
            # The opt-out flag. A disabled flag row is an absent flag row:
            # the operator switched the switch off, not the bot.
            flag_off = bool(row.enabled) and (row.value_plain or "").strip().lower() in _FALSEY
            current = _TenantRows(
                token_row_id=current.token_row_id,
                token_ciphertext=current.token_ciphertext,
                token_row_enabled=current.token_row_enabled,
                disabled_by_flag=flag_off,
            )
        per_tenant[row.tenant_id] = current
    return per_tenant


__all__ = [
    "SOURCE_ENV_FALLBACK",
    "SOURCE_VAULT",
    "DiscoveredBot",
    "discover_bot_tokens",
]
