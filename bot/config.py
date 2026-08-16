# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Bot configuration loaded from environment / .env file.

This module mirrors the pattern of :mod:`core.config` but is independent of it
— the bot reads its own variables and validates them on its own. It must not
call :func:`core.config.get_config` and must not import from PyQt6, so it
remains safe to load in non-GUI contexts (notably the regression-guard test
``tests/bot/test_telegram_bot.py::test_no_qt_import``).

Usage::

    from bot.config import get_bot_config

    cfg = get_bot_config()
    if cfg.enabled:
        ...
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from core.exceptions import ConfigurationError

# Load .env from the project root (two levels up from this file). Mirrors the
# behaviour in ``core/config.py`` so the bot can be loaded without first
# importing the core configuration module.
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_PATH)

_LOG = logging.getLogger("portfoliflow.bot")


@dataclass
class BotSettings:
    """Configuration for the optional Telegram bot.

    All fields default to environment variables. Validation happens in
    :meth:`__post_init__` and is gated on :attr:`enabled` — a disabled bot
    accepts any combination of empty fields, while an enabled bot demands
    what nothing else can supply (the database URL) so an accidentally-
    mis-configured deployment fails loudly at startup rather than silently.

    That required set has shrunk twice, both times because another scope
    gained the ability to supply the value:

    * ADR-0112 §4b — the Shirley **credential and model** are resolved per
      turn, so the tenant's own vault rows can serve a bot whose ``.env``
      carries neither;
    * ADR-0112 §5 — the **Telegram token** is discovered per tenant from
      ``scoped_settings``, and the **whitelist** is replaced by pairing.

    Empty values therefore warn (or say nothing, where empty is now an
    ordinary configuration) instead of raising: refusing to start would
    make the vault-configured deployment impossible, which is the one the
    ADR is steering towards.

    Attributes:
        enabled: Master switch for the whole bot thread — N tenant bots or
            none. ``True`` only when ``TELEGRAM_BOT_ENABLED`` equals
            ``"true"`` (case-insensitive).
        telegram_token: Bot token issued by BotFather. **Optional** since
            ADR-0112 §5: it is the *transition* token, bound to the
            :attr:`tenant_subdomain` tenant and additive to whatever
            :mod:`bot.token_discovery` finds. With no token here and none
            stored anywhere, :func:`bot.telegram_bot.start_bot` no-ops with
            an INFO line.
        allowed_user_ids_raw: Raw comma-separated whitelist string read
            verbatim from the environment. **Deprecated** since ADR-0112 §5
            — authorisation is the pairing binding, and this list survives
            only as a fallback on the environment-token dispatcher. A
            non-empty value warns once at startup. Parsed into
            :attr:`allowed_user_ids` in :meth:`__post_init__`.
        openai_base_url: OpenAI-compatible endpoint. Defaults to OpenRouter.
            The last fallback of the per-turn ``base_url`` chain (ADR-0112
            §4b).
        openai_api_key: API key for the endpoint. **Optional** since
            ADR-0112 §4b — the application-scope link of the per-turn
            credential chain, which a tenant's own vault row outranks. An
            empty value with the bot enabled warns rather than raising.
        model: Model ID (e.g. ``"openai/gpt-4o"``). **Optional** on the same
            grounds, and warns on the same terms.
        database_url: The ``portfoliflow_app`` (RLS-scoped) asyncpg URL the
            bot's Postgres-native tools read through. Required when enabled
            so an accidentally DB-less deployment fails loudly. The bot does
            **not** resolve its own tenant from this URL — the ``tenants``
            table is unreadable without a tenant context and the app role
            has no RLS-bypass; the web lifespan resolves the tenant via the
            superuser/audit engine and injects it (ADR-0063).
        tenant_subdomain: Subdomain the web lifespan resolves to the tenant
            the **environment token** is bound to. Defaults to
            ``"minathena-capital"``. **Deprecated transition config** since
            ADR-0112 §5: it binds that one dispatcher and nothing else —
            every tenant whose token discovery finds carries its own
            identity. The lifespan warns when it is actually used.
        allowed_user_ids: Parsed whitelist of Telegram user IDs. Populated by
            :meth:`__post_init__`; not part of the constructor signature.
    """

    enabled: bool = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_ENABLED", "false").lower() == "true"
    )
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    allowed_user_ids_raw: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    )
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("SHIRLEY_MODEL", ""))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    tenant_subdomain: str = field(
        default_factory=lambda: os.getenv("SHIRLEY_BOT_TENANT_SUBDOMAIN", "minathena-capital")
    )

    allowed_user_ids: frozenset[int] = field(default_factory=frozenset, init=False)

    def __post_init__(self) -> None:
        """Parse the whitelist and validate the configuration.

        Raises:
            ConfigurationError: If the whitelist contains non-integer
                entries, or if the bot is enabled with the database URL
                empty. The Shirley credential and model (ADR-0112 §4b) and
                the Telegram token and whitelist (ADR-0112 §5) are no
                longer required — each has another scope that can supply
                it, so they warn or stay silent instead.
        """
        self.allowed_user_ids = self._parse_whitelist(self.allowed_user_ids_raw)

        if not self.enabled:
            return

        # The token is no longer required (ADR-0112 §5): tenants store their
        # own in ``scoped_settings`` and the bot discovers them at start. An
        # empty value here is therefore an ordinary configuration and says
        # nothing; a deployment with no token *anywhere* gets its INFO line
        # from ``start_bot``, which is the only place that knows.
        if self.allowed_user_ids:
            _LOG.warning(
                "TELEGRAM_ALLOWED_USER_IDS is set. The whitelist is deprecated "
                "(ADR-0112 §5): it admits turns only on the environment-token "
                "bot, grants no user identity (so user-scope settings do not "
                "apply), and will be removed. Pair each chat instead — Admin → "
                "Providers & Credentials → Telegram → Generate pairing code — "
                "then clear this variable."
            )
        # The Shirley credential and model are *not* required here any more
        # (ADR-0112 §4b): they are resolved per turn from the tenant's vault
        # rows, with these environment values as the application-scope
        # fallback. An empty pair is therefore a legitimate configuration —
        # the tenant supplies them — but it is also the shape of a genuine
        # mistake, so it warns rather than passing silently. A turn that
        # resolves nothing answers with the ordinary polite error reply.
        if not self.openai_api_key:
            _LOG.warning(
                "TELEGRAM_BOT_ENABLED=true but OPENROUTER_API_KEY is empty — "
                "the bot will answer only if the tenant holds its own "
                "OpenRouter API key (Admin → Providers & Credentials)."
            )
        if not self.model:
            _LOG.warning(
                "TELEGRAM_BOT_ENABLED=true but SHIRLEY_MODEL is empty — the "
                "bot will answer only if the tenant holds its own OpenRouter "
                "model (Admin → Providers & Credentials)."
            )
        # Checked last so the more specific Telegram errors above surface
        # first. ``tenant_subdomain`` always has a default, so it needs no
        # validation.
        if not self.database_url:
            raise ConfigurationError("TELEGRAM_BOT_ENABLED=true but DATABASE_URL is empty")

    @staticmethod
    def _parse_whitelist(raw: str) -> frozenset[int]:
        """Parse a comma-separated string of Telegram user IDs.

        Whitespace around commas is tolerated. Empty entries (e.g. trailing
        commas) are skipped. A non-integer entry raises immediately so a
        malformed whitelist never silently degrades into a smaller one.

        Args:
            raw: The raw environment-variable value (may be empty).

        Returns:
            A frozenset of integer user IDs. Empty if ``raw`` is empty or
            contains only whitespace.

        Raises:
            ConfigurationError: If any non-empty entry cannot be parsed
                as an integer.
        """
        stripped = raw.strip()
        if not stripped:
            return frozenset()

        ids: set[int] = set()
        for entry in stripped.split(","):
            token = entry.strip()
            if not token:
                continue
            try:
                ids.add(int(token))
            except ValueError as exc:
                raise ConfigurationError(
                    f"TELEGRAM_ALLOWED_USER_IDS contains a non-integer entry: {token!r}"
                ) from exc
        return frozenset(ids)


_instance: BotSettings | None = None


def get_bot_config() -> BotSettings:
    """Return the application-wide :class:`BotSettings` singleton.

    Created lazily on the first call. Subsequent calls return the cached
    instance without re-reading the environment.

    Returns:
        The cached :class:`BotSettings` instance.
    """
    global _instance
    if _instance is None:
        _instance = BotSettings()
    return _instance
