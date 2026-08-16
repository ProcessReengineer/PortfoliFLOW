# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Telegram bot — the first non-GUI consumer of :class:`AIServiceCore`.

This module wires :meth:`services.ai_service_core.AIServiceCore.stream_response`
to the Telegram Bot API via aiogram v3. The bot runs on its own asyncio
event loop in a dedicated daemon thread inside the same PortfoliFLOW
process, so it does not contend with Qt's event loop and does not require
a separate executable.

Since ADR-0112 §5 that one thread serves **many tenants**: each tenant's
BotFather token gets its own ``Bot`` + ``Dispatcher`` + polling task on the
single shared loop, and a Telegram user is authorised by a *pairing*
binding rather than by a process-wide whitelist. See "Multiplexing" and
"Authorisation" below.

Architecture notes
------------------

* aiogram is imported lazily inside :func:`_run_bot_in_thread` because it is
  declared as an *optional* dependency under the ``bot`` extra in
  ``pyproject.toml``. Importing it at module load would break installations
  that opted out of the bot.
* The bot keeps its **own**, deliberately *unconfigured*
  :class:`AIServiceCore` instance for the turn machinery (system prompt,
  registered tools, streaming loop). Since ADR-0112 §4b the endpoint,
  credential and model are no longer core state at all: they are resolved
  per turn from the tenant's vault rows — falling back to ``.env`` — and
  handed to :meth:`~services.ai_service_core.AIServiceCore.stream_response`
  as a :class:`~services.ai_service_core.ResolvedLLM`. A key written in
  Admin → Providers & Credentials is therefore live on the next message,
  with no restart. The :class:`~services.tool_registry.ToolRegistry` and the
  process-wide ``_TURN_LOCK`` in :mod:`services.ai_service_core` remain
  shared across cores, so trust-class gating (ADR-0022) and turn
  serialisation (ADR-0031) still work correctly.
* :meth:`AIServiceCore.stream_response` is an asyncio async generator. The
  handler awaits events directly — no ``run_in_executor`` bridge, no
  separate sync surface. aiogram v3 is asyncio-native, so the two halves
  share a single concurrency model.
* The system prompt is loaded via :meth:`AIServiceCore.get_system_prompt`
  on first use; the bot no longer maintains its own duplicate of the
  ``Soul_Shirley.md`` parser.

Multiplexing (ADR-0112 §5)
--------------------------

:mod:`bot.token_discovery` scans every tenant's ``telegram.bot_token`` row
once at bot start; each discovered token becomes one supervised polling
task on the *same* loop in the *same* thread. Telegram allows exactly one
``getUpdates`` consumer per token, so the rule that made the bot
single-worker still holds — per token, and therefore N times over. That
also makes the single-uvicorn-worker constraint load-bearing for N bots,
not just one (see the note in :func:`web.main.create_app`).

Each task carries its own retry/supervision (:func:`_poll_with_retry`), so
one tenant's dead token, revoked bot or network hiccup never touches the
other dispatchers and never touches the web process. Token changes apply on
**restart** — there is no rescan timer in v1, and both the admin surface
and ``docs/deploy/telegram-multi-bot.md`` say so.

Everything a handler needs to know about *which* bot it is serving travels
in a :class:`_BotBinding` closed over at registration time — never through
module-level state, which now belongs to at most one of the N dispatchers.
The token itself is deliberately *not* on the binding: it is needed to
construct the ``Bot`` and nowhere else, so no handler, log line or error
path can reach it.

Authorisation (ADR-0112 §5)
---------------------------

A message is authorised by the *pairing* binding: a user-scope
``telegram.chat_id`` row in the dispatcher's tenant whose value is this
chat's id. On a match the turn runs **as that user** — so user-scope
``openrouter`` rows now apply to a bot turn, the natural completion of
ADR-0112 §4b. Without a match the message is dropped silently (with a
WARNING) — replying "you are not authorised" would leak the bot's
existence to the wider Telegram user base.

``TELEGRAM_ALLOWED_USER_IDS`` survives as a **deprecated fallback on the
environment-token dispatcher only**: it admits the turn with no user
identity, and logs one deprecation WARNING. A tenant that stores its own
token is pairing-only from the start.

``/pair <code>`` is handled *before* authorisation — that is its whole
point. The code is minted in Admin → Providers & Credentials and lives in
the in-process store :mod:`services.telegram_pairing`.

Tenant identity
---------------

Tenant identity is *injected*, not resolved here (ADR-0063): the bot has no
RLS-bypass and the ``tenants`` table is unreadable without a tenant
context. The web lifespan hands :func:`start_bot` the app-role database
URL, the superuser URL discovery scans on, and the tenant id it resolved
for the deprecated ``SHIRLEY_BOT_TENANT_SUBDOMAIN`` — which now binds the
environment-token dispatcher alone. On the desktop entry point all three
are unset: discovery is skipped, and the Postgres-native tools degrade
gracefully.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bot.config import BotSettings, get_bot_config
from bot.token_discovery import (
    SOURCE_ENV_FALLBACK,
    DiscoveredBot,
    discover_bot_tokens,
)
from core.repositories._session import tenant_context
from core.repositories.scoped_setting_repository import ScopedSettingRepository
from core.repositories.user_repository import UserRepository
from services import telegram_pairing
from services.ai_models import (
    Attachment,
    Conversation,
    Message,
    MessageRole,
)
from services.ai_service_core import AIServiceCore, ResolvedLLM
from services.credential_vault import VaultDecryptError
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
from services.tools._tool_context import ToolExecutionContext
from services.vision_capabilities import (
    ALLOWED_IMAGE_MIME_TYPES,
    MAX_IMAGE_BYTES,
    supports_vision,
)
from services.voice import (
    DEFAULT_STT_BASE_URL,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DEFAULT_TTS_VOICE,
    DEFAULT_VOICE_PROVIDER,
    EmptyTranscriptError,
    ResolvedVoice,
    UnsupportedAudioFormatError,
    VoiceError,
    build_provider,
)

if TYPE_CHECKING:  # pragma: no cover — type-check-only imports
    from aiogram import Bot

logger = logging.getLogger(__name__)

# Telegram protocol limits.
_TELEGRAM_TEXT_LIMIT = 4000
_TELEGRAM_CAPTION_LIMIT = 1024
_TELEGRAM_CAPTION_TRUNCATE_AT = 1020  # leaves room for an "…" suffix.

# UX timing.
_PLACEHOLDER_DELAY_SECONDS = 2.0
_TYPING_REFRESH_SECONDS = 4.0
_PLACEHOLDER_TEXT = "Shirley is still working…"

# Module-level handles so :func:`stop_bot` can reach into the worker thread.
_bot_thread: threading.Thread | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_core: AIServiceCore | None = None

# The N supervised polling tasks, one per tenant bot (ADR-0112 §5).
# Populated on the bot loop by :func:`_run_dispatchers` and read by
# :func:`stop_bot`, which cancels them from the calling thread via
# ``call_soon_threadsafe`` so the gather returns and the single worker
# thread unwinds through its ordinary cleanup path.
_bot_tasks: list[asyncio.Task[None]] = []

# Stop signal for the polling retry loop. Set by :func:`stop_bot` so an
# in-progress backoff sleep returns immediately instead of blocking the
# 5-second join; cleared by :func:`start_bot` before each launch. Distinct
# from ``loop.stop`` — the retry loop may be *between* polling attempts (in a
# backoff sleep, with no running loop to stop), so it needs its own signal.
_bot_stop_event: threading.Event = threading.Event()
_BOT_RETRY_BACKOFF_START: float = 5.0  # seconds
_BOT_RETRY_BACKOFF_MAX: float = 60.0  # cap

# The tenant the **environment token** is bound to, injected by the web
# lifespan via :func:`start_bot` (ADR-0063). The bot cannot resolve its own
# tenant — the ``tenants`` table is unreadable without a tenant context and
# the app role has no RLS-bypass — so the web process resolves the
# deprecated ``SHIRLEY_BOT_TENANT_SUBDOMAIN`` through the superuser/audit
# engine and hands the result here.
#
# Since ADR-0112 §5 this is *one* dispatcher's identity, not the process's:
# every vault-discovered bot carries its own tenant on its
# :class:`_BotBinding`. What is left here is exactly the transition
# dispatcher's binding — and the desktop entry point's, where it stays unset
# (``main.py`` calls ``start_bot()`` with no arguments) and the
# Postgres-native tools degrade gracefully. Reset by :func:`stop_bot`.
_bot_tenant_id: UUID | None = None
_bot_database_url: str = ""

# The superuser (RLS-bypassing) URL :mod:`bot.token_discovery` scans every
# tenant's bot token on, injected by the web lifespan alongside the app-role
# URL. Empty on the desktop entry point and whenever the deployment has no
# superuser URL configured — discovery is then skipped and only an
# environment token can serve. The bot never reads
# ``DATABASE_URL_SUPERUSER`` itself; ``cli/_db.py`` remains its only reader.
_bot_superuser_url: str = ""

# The bot worker's own async engine, built from ``_bot_database_url`` on the
# bot's event loop at start and disposed on the same loop at stop. It exists
# for one purpose: opening the ``tenant_context`` the per-turn credential
# resolution reads the vault through (ADR-0112 §4b). It is deliberately *not*
# the web app's ``app.state.engine`` — that one is bound to the uvicorn loop,
# and an asyncpg pool must never cross loops (the same rule that makes the
# Postgres-native tools build their own short-lived engine, ADR-0047). Stays
# ``None`` on the desktop entry point and whenever no database URL was
# injected; resolution then falls back to the environment alone.
_bot_engine: AsyncEngine | None = None

#: The pairing command (ADR-0112 §5). Handled before authorisation — it is
#: how an unpaired user becomes a paired one.
_PAIR_COMMAND = "/pair"

#: The single reply every failed ``/pair`` gets: unknown code, expired code,
#: code minted in another tenant, and throttled chat are deliberately
#: indistinguishable, so the chat is no oracle for which it was (D5).
_PAIR_FAILED_MESSAGE = (
    "❌ Code invalid or expired. Please generate a new pairing code in "
    "PortfoliFLOW under Admin → Providers & Credentials."
)

_PAIR_USAGE_MESSAGE = (
    "ℹ️ Please include the pairing code: /pair ABCD1234 — you will find the "
    "code in PortfoliFLOW under Admin → Providers & Credentials."
)

_PAIR_SUCCESS_MESSAGE = (
    "✅ This chat is now linked to your PortfoliFLOW account. "
    "Your personal settings apply here from now on."
)

_PAIR_UNAVAILABLE_MESSAGE = "⚠️ The link could not be saved. Please try again later."

#: One user-facing message for every "this turn has no LLM" outcome. English,
#: like the rest of the bot's operator-facing copy. Carries no leading emoji:
#: the turn's error path already prefixes one, and the image path's direct
#: send adds its own.
_NO_LLM_MESSAGE = (
    "No OpenRouter access is configured for this tenant. Please set the "
    "API key and model under Admin → Providers & Credentials (or "
    "OPENROUTER_API_KEY and SHIRLEY_MODEL in .env)."
)


class _BotLLMUnconfiguredError(Exception):
    """This turn's OpenRouter credential or model resolved to nothing.

    Carries the ready-to-send reply text, so the handler's existing polite
    error path is the only place that needs to know about it.
    """


#: One user-facing message for every "voice is on but no credential
#: resolved" outcome, either half. Mirrors :data:`_NO_LLM_MESSAGE`: names both
#: fixable scopes, never says "restart", and carries no leading emoji — the
#: send sites prefix one.
_NO_VOICE_MESSAGE = (
    "Voice is enabled but no voice credential is configured for this "
    "tenant. Please set the speech-to-text and text-to-speech API keys "
    "under Admin → Providers & Credentials (or VOICE_STT_API_KEY and "
    "VOICE_TTS_API_KEY in .env)."
)


class _BotVoiceUnconfiguredError(Exception):
    """This message's voice credential resolved to nothing (either half).

    The voice twin of :class:`_BotLLMUnconfiguredError`, and the bot-local
    twin of the web surface's ``_VoiceUnconfiguredError`` — the bot must not
    import from ``web/``, so the message and the error type are duplicated
    deliberately, exactly as :data:`_NO_LLM_MESSAGE` already is. It wraps
    :class:`~services.investments.credential_resolver.CredentialUnavailableError`
    raised for **either** half of the credential.

    Enabled voice requires **both** halves: a tenant holding one key of two is
    a configuration error that surfaces loudly at first use. That is what
    :meth:`~services.voice.config.VoiceConfig.__post_init__` enforced at
    startup, relocated to the message by ADR-0118 §2 — which is why an inbound
    voice message resolves the full :class:`~services.voice.ResolvedVoice`
    even before the STT leg needs only one half of it.

    A :class:`~services.credential_vault.VaultDecryptError` is deliberately
    **not** translated into this: a vault that will not decrypt is an operator
    emergency, not a "configure me" nudge, and it must not read as a missing
    key. The bot answers it generically (see :func:`_handle_voice_message`)
    where the web surface propagates it to a 500 — the one deliberate
    divergence, because an exception escaping here would reach the aiogram
    dispatcher.
    """


# Per-chat conversation memory (in-memory, process-lifetime only).
# Keyed by ``(tenant_id, chat_id)`` since ADR-0112 §5: a private Telegram
# chat id equals the human's Telegram user id *for every bot*, so two
# tenants' dispatchers talking to the same person see the same chat id. On
# the bare ``chat_id`` key that made one tenant's conversation visible in
# another tenant's turn — a cross-tenant leak. The tenant is part of the key.
#
# Holds plain user/assistant text messages — never tool-call or tool-result
# messages, so the OpenAI replay via Conversation.to_openai_messages() stays
# valid. Reset by stop_bot() and, for the caller's own tenant-scoped entry
# alone, by the /reset command.
_chat_histories: dict[tuple[UUID | None, int], list[Message]] = {}
_MAX_HISTORY_MESSAGES: int = 20  # ~10 turns

# One-shot flag for the ``TELEGRAM_ALLOWED_USER_IDS`` deprecation WARNING
# (ADR-0112 §5, D3/D5): logged when the whitelist actually admits a turn,
# once per process rather than once per message. Cleared by :func:`stop_bot`.
_whitelist_deprecation_warned: bool = False


@dataclass(frozen=True)
class _BotBinding:
    """Which bot a handler is serving — closed over at registration time.

    One binding per dispatcher (ADR-0112 §5). It carries everything a
    handler needs to know about its own identity and **nothing more**: the
    token is not here, because it is needed to construct the ``Bot`` and
    nowhere else, so no handler, log line or error path can reach it.

    Attributes:
        tenant_id: The tenant this dispatcher serves. Scopes the pairing
            lookup, the credential resolution, the tool context and the
            conversation history. ``None`` only on the desktop entry point.
        source: ``"vault"`` for a tenant's own stored token,
            ``"env-fallback"`` for the deprecated ``TELEGRAM_BOT_TOKEN``
            dispatcher — the only one where the legacy whitelist still
            admits a turn (D5).
        label: A short, stable log prefix naming the tenant. Never a token.
    """

    tenant_id: UUID | None
    source: str
    label: str


def _binding_label(tenant_id: UUID | None, source: str) -> str:
    """Return the per-dispatcher log prefix — tenant and source, never a token."""
    return f"tenant={tenant_id} source={source}"


def _default_binding() -> _BotBinding:
    """Return the binding for a handler invoked without a dispatcher context.

    Two callers reach the handlers without one: the desktop entry point
    (``start_bot()`` with no arguments — no discovery, no dispatcher set)
    and the handler-level tests, which drive the turn body directly. Both
    are the *transition* shape, so the binding is the environment one: the
    tenant the lifespan injected, and whitelist admission enabled.
    Production dispatchers always pass their own binding explicitly.
    """
    return _BotBinding(
        tenant_id=_bot_tenant_id,
        source=SOURCE_ENV_FALLBACK,
        label=_binding_label(_bot_tenant_id, SOURCE_ENV_FALLBACK),
    )


def _history_key(binding: _BotBinding, chat_id: int) -> tuple[UUID | None, int]:
    """Return this chat's history key — tenant-scoped since ADR-0112 §5."""
    return (binding.tenant_id, chat_id)


@dataclass(frozen=True)
class _ChartArtifact:
    """A chart produced by the ``generate_chart`` tool during a turn.

    Local to the bot module — collected from
    :class:`~services.ai_service_core.StreamEvent` records of type
    ``"chart_artifact"`` so the photo-sending pass at the end of the
    handler can iterate a clean list of fully-formed artefacts.
    """

    image_base64: str
    caption: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_bot(
    tenant_id: UUID | None = None,
    database_url: str = "",
    superuser_url: str = "",
) -> None:
    """Start the Telegram bot in a daemon thread if enabled in ``.env``.

    Reads :class:`bot.config.BotSettings` via :func:`get_bot_config`. When
    ``TELEGRAM_BOT_ENABLED`` is not ``true`` the function is a no-op and
    logs at INFO; otherwise it spawns a daemon thread running
    :func:`_run_bot_in_thread`, which discovers the tenant bot tokens and
    starts one supervised polling task per token (ADR-0112 §5). The thread
    is daemonised so a process exit is never blocked even if
    :func:`stop_bot` is not called.

    ``TELEGRAM_BOT_ENABLED=false`` remains the master kill switch for the
    whole thread — N bots or none. What it no longer decides is *which*
    bots run: that is the discovery scan's answer.

    Calling :func:`start_bot` while the bot is already running is a no-op
    with a WARNING log; callers that want to reconfigure must call
    :func:`stop_bot` first.

    Args:
        tenant_id: The tenant the **environment token** is bound to,
            resolved from the deprecated ``SHIRLEY_BOT_TENANT_SUBDOMAIN``
            and injected by the web lifespan (ADR-0063). ``None`` on the
            desktop entry point, where the Postgres-native tools then
            degrade gracefully (no tenant context, "data unavailable").
            Tenants whose own token discovery finds carry their own id and
            never consult this one.
        database_url: The ``portfoliflow_app`` (RLS-scoped) asyncpg URL the
            tools build a short-lived engine from, and the bot's own
            engine opens tenant contexts on. Empty on the desktop entry
            point. Paired with the dispatcher's tenant into the per-turn
            :class:`~services.tools._tool_context.ToolExecutionContext`.
        superuser_url: The RLS-bypassing URL the cross-tenant token scan
            runs on (:mod:`bot.token_discovery`). Empty skips discovery
            entirely, leaving only an environment token to serve.
    """
    global _bot_thread, _bot_tenant_id, _bot_database_url, _bot_superuser_url

    config = get_bot_config()
    if not config.enabled:
        logger.info("Telegram bot disabled (TELEGRAM_BOT_ENABLED!=true).")
        return

    if _bot_thread is not None and _bot_thread.is_alive():
        logger.warning("Telegram bot already running; start_bot() ignored.")
        return

    if not config.telegram_token and not superuser_url:
        # Nothing to discover and nothing to fall back on: the deployment
        # simply has no bot. Not an error — a token-less enabled flag is
        # the natural state of a fresh install (ADR-0112 §5, D3).
        logger.info(
            "Telegram bot enabled but no token is configured and no "
            "superuser URL was injected to discover one; not starting."
        )
        return

    _bot_tenant_id = tenant_id
    _bot_database_url = database_url
    _bot_superuser_url = superuser_url

    # Clear any stop signal left over from a previous run so the new worker's
    # retry loop starts live (see :func:`_poll_with_retry`).
    _bot_stop_event.clear()

    thread = threading.Thread(
        target=_run_bot_in_thread,
        args=(config,),
        name="TelegramBot",
        daemon=True,
    )
    _bot_thread = thread
    thread.start()
    logger.info("Telegram bot starting; discovering tenant bot tokens.")


def stop_bot() -> None:
    """Signal the bot's event loop to stop and wait briefly for shutdown.

    Idempotent: safe to call when the bot is not running. Cancels **every**
    dispatcher's polling task (ADR-0112 §5 — there are N, not one) and then
    joins the single worker thread with a 5-second timeout; if the thread is
    still alive after the timeout the function logs at WARNING and returns —
    the daemon thread will be terminated at process exit.

    Cancellation is the graceful path: a cancelled ``start_polling`` lets
    aiogram close its update loop, the gather in :func:`_run_dispatchers`
    returns, and the thread unwinds through its own cleanup (sessions
    closed, engine disposed, loop closed). ``loop.stop`` is kept only as the
    fallback for a dispatcher that will not come down that way.
    """
    global _bot_thread, _bot_loop, _bot_core, _bot_tenant_id
    global _bot_database_url, _bot_superuser_url, _whitelist_deprecation_warned

    # Signal the retry loop first so an in-progress backoff sleep returns
    # immediately rather than holding up the join below. Safe even when the
    # bot is not running — the next start_bot() clears it again.
    _bot_stop_event.set()

    if _bot_loop is None or _bot_thread is None:
        return

    loop = _bot_loop
    thread = _bot_thread

    def _cancel_all() -> None:
        # Runs *on the bot loop*: an asyncio.Task may only be cancelled
        # from the loop that owns it.
        for task in list(_bot_tasks):
            task.cancel()

    # A RuntimeError means the loop is already closed — nothing to stop.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(_cancel_all)

    thread.join(timeout=5.0)
    if thread.is_alive():
        # A dispatcher ignored its cancellation. Stop the loop out from
        # under it and give the thread a moment to unwind.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)

    if thread.is_alive():
        logger.warning(
            "Telegram bot thread did not stop within 7s; daemon thread "
            "will be terminated at process exit.",
        )
    else:
        logger.info("Telegram bot stopped.")

    _bot_thread = None
    _bot_loop = None
    _bot_core = None
    _bot_tenant_id = None
    _bot_database_url = ""
    _bot_superuser_url = ""
    _whitelist_deprecation_warned = False
    _bot_tasks.clear()
    _chat_histories.clear()
    # Pending pairing codes are redeemed against dispatchers that no longer
    # exist; void them with the bots they belonged to (D4).
    telegram_pairing.reset_store()


# ---------------------------------------------------------------------------
# Worker thread entry point
# ---------------------------------------------------------------------------


async def _sleep_unless_stopped(
    stop_event: threading.Event,
    timeout: float,
    *,
    step: float = 0.25,
) -> bool:
    """Sleep up to ``timeout`` seconds, returning early when stop is signalled.

    The shutdown signal is a :class:`threading.Event` (set from the calling
    thread by :func:`stop_bot`), and blocking on ``Event.wait`` would freeze
    the whole loop — with N dispatchers sharing it, one backoff would stall
    the other N-1. So the wait is a series of short async naps that check
    the flag between them.

    Args:
        stop_event: The cross-thread shutdown signal.
        timeout: How long to wait in total, in seconds.
        step: Longest single nap. Bounds how late a stop is noticed.

    Returns:
        ``True`` if stop was signalled (wait cut short or already set),
        ``False`` if the full ``timeout`` elapsed without one.
    """
    remaining = timeout
    while remaining > 0:
        if stop_event.is_set():
            return True
        nap = min(step, remaining)
        await asyncio.sleep(nap)
        remaining -= nap
    return stop_event.is_set()


async def _poll_with_retry(
    *,
    run_polling: Callable[[], Awaitable[None]],
    network_error: type[BaseException],
    unauthorized_error: type[BaseException],
    stop_event: threading.Event,
    label: str = "bot",
    backoff_start: float = _BOT_RETRY_BACKOFF_START,
    backoff_max: float = _BOT_RETRY_BACKOFF_MAX,
) -> None:
    """Supervise **one** dispatcher's polling, retrying transient failures.

    One of these runs per tenant bot (ADR-0112 §5), each as its own task on
    the shared loop — which is why it is a coroutine rather than the
    thread-blocking wrapper it was before F5: ``run_until_complete`` can
    drive exactly one dispatcher, ``asyncio.gather`` drives N. The
    semantics are otherwise unchanged. It awaits ``run_polling`` (which
    drives ``dp.start_polling(...)``) and:

    * returns when polling exits normally — a clean shutdown;
    * treats ``RuntimeError`` (loop stopped externally) as a clean shutdown
      and returns; re-raises ``asyncio.CancelledError`` after logging, so a
      cancelled task really is cancelled;
    * on ``unauthorized_error`` logs a clear ERROR once and returns — a bad
      token is permanent, retrying is pointless. **This ends one task, not
      the bot:** the other tenants' dispatchers keep polling;
    * on ``network_error`` logs ONE concise WARNING (no traceback) on the
      first occurrence, DEBUG thereafter, then waits ``backoff`` seconds
      (doubling up to ``backoff_max``) and retries — so the bot reconnects
      on its own when the network returns;
    * lets any other exception propagate to :func:`_run_dispatchers`, which
      logs it against this dispatcher and leaves the others running (a real
      bug — keep the traceback).

    The wait goes through :func:`_sleep_unless_stopped`, so :func:`stop_bot`
    makes an in-progress backoff return promptly without blocking the loop
    the other dispatchers share.

    Args:
        run_polling: A zero-argument callable returning the awaitable that
            drives ``dp.start_polling(...)``.
        network_error: The aiogram exception type signalling a transient
            connectivity failure (``TelegramNetworkError``).
        unauthorized_error: The aiogram exception type signalling a
            permanently rejected token (``TelegramUnauthorizedError``).
        stop_event: The shutdown signal. When set, the loop exits — checked
            before each attempt and used as the interruptible backoff wait.
        label: The dispatcher's log prefix (tenant and source, never a
            token).
        backoff_start: Initial backoff in seconds before the first retry.
        backoff_max: Upper bound the doubling backoff is capped at.
    """
    backoff = backoff_start
    offline_logged = False
    while not stop_event.is_set():
        try:
            await run_polling()
            return  # polling returned normally -> clean shutdown
        except asyncio.CancelledError:
            # Ordinary shutdown (stop_bot cancels every task). Re-raised so
            # the task's state is genuinely "cancelled" for the gather.
            logger.info("Telegram bot [%s]: polling cancelled.", label)
            raise
        except RuntimeError as exc:
            # Raised when the loop is stopped externally while the polling
            # task is still awaited. Treated as a clean shutdown.
            logger.info("Telegram bot [%s]: polling stopped (%s).", label, exc)
            return
        except unauthorized_error:
            logger.error(
                "Telegram bot [%s]: token was rejected (unauthorized); not "
                "retrying this dispatcher. Check the tenant's stored "
                "telegram.bot_token (Admin → Providers & Credentials) or "
                "TELEGRAM_BOT_TOKEN. Every other bot and the web app are "
                "unaffected.",
                label,
            )
            return
        except network_error as exc:
            if not offline_logged:
                logger.warning(
                    "Telegram bot [%s]: cannot reach api.telegram.org (%s) — "
                    "no network. Retrying in the background; the web app and "
                    "web chat are unaffected.",
                    label,
                    type(exc).__name__,
                )
                offline_logged = True
            else:
                logger.debug(
                    "Telegram bot [%s]: still offline (%s); retrying.",
                    label,
                    type(exc).__name__,
                )
            if await _sleep_unless_stopped(stop_event, backoff):
                return  # stop requested during backoff
            backoff = min(backoff * 2, backoff_max)


async def _run_dispatchers(
    runners: Sequence[tuple[_BotBinding, Callable[[], Awaitable[None]]]],
    *,
    network_error: type[BaseException],
    unauthorized_error: type[BaseException],
    stop_event: threading.Event,
) -> None:
    """Run every dispatcher concurrently on this loop until they all end.

    The multiplexing seam (ADR-0112 §5, D1). Each runner becomes one
    supervised task; the gather collects rather than propagates, so one
    dispatcher's permanent failure — a revoked token, an unexpected error —
    ends that task alone and leaves the rest polling. The tasks are
    published to :data:`_bot_tasks` so :func:`stop_bot` can cancel them from
    the calling thread.

    Args:
        runners: ``(binding, run_polling)`` pairs, one per discovered token.
        network_error: The transient-connectivity exception type.
        unauthorized_error: The rejected-token exception type.
        stop_event: The cross-thread shutdown signal.
    """
    tasks = [
        asyncio.ensure_future(
            _poll_with_retry(
                run_polling=run_polling,
                network_error=network_error,
                unauthorized_error=unauthorized_error,
                stop_event=stop_event,
                label=binding.label,
            )
        )
        for binding, run_polling in runners
    ]
    _bot_tasks.clear()
    _bot_tasks.extend(tasks)
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _bot_tasks.clear()

    for (binding, _run_polling), result in zip(runners, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.error(
                "Telegram bot [%s]: dispatcher ended with an unexpected error; "
                "the other dispatchers and the web app are unaffected.",
                binding.label,
                exc_info=result,
            )


async def _discover_bots(config: BotSettings) -> list[DiscoveredBot]:
    """Discover every tenant's bot token, degrading to the environment alone.

    Runs on the bot's own loop, on a **loop-local** superuser engine built
    from the injected URL and disposed immediately — the scan is a one-shot
    at start, and an asyncpg pool must never outlive the loop that owns it
    or cross to another (the same rule that gives the Postgres-native tools
    their own short-lived engines, ADR-0047).

    Discovery failure is never fatal: "a bot failure must never block web
    start" extends inwards, so an unreachable database or a broken scan logs
    and leaves whatever the environment can serve (D2).

    Args:
        config: The validated bot configuration, for the environment token.

    Returns:
        The discovered bots, possibly empty.
    """
    if not _bot_superuser_url:
        # No RLS-bypassing URL was injected (desktop entry point, or a
        # deployment without one): no scan is possible, so the environment
        # token is the only candidate.
        return _env_only(config)

    engine = create_async_engine(_bot_superuser_url, future=True, pool_pre_ping=True)
    try:
        return await discover_bot_tokens(
            engine,
            env_token=config.telegram_token,
            env_tenant_id=_bot_tenant_id,
        )
    except Exception:  # noqa: BLE001 — discovery must not take the bot down
        logger.exception(
            "Telegram bot: token discovery failed; continuing with the "
            "environment token alone (if any)."
        )
        return _env_only(config)
    finally:
        with contextlib.suppress(Exception):
            await engine.dispose()


def _env_only(config: BotSettings) -> list[DiscoveredBot]:
    """Return the environment token as the sole dispatcher, or nothing."""
    if not config.telegram_token.strip():
        return []
    return [
        DiscoveredBot(
            tenant_id=_bot_tenant_id,
            token=config.telegram_token.strip(),
            source=SOURCE_ENV_FALLBACK,
        )
    ]


def _run_bot_in_thread(config: BotSettings) -> None:
    """Run every tenant's dispatcher on one fresh event loop in this thread.

    Creates a new asyncio event loop, registers it as the current loop for
    the thread, discovers the tenant bot tokens (ADR-0112 §5), instantiates
    one aiogram :class:`Bot` + :class:`Dispatcher` per token with handlers
    closed over that dispatcher's :class:`_BotBinding`, and runs all of them
    concurrently through :func:`_run_dispatchers`. The loop is stored in the
    module-level ``_bot_loop`` so :func:`stop_bot` can reach in via
    ``call_soon_threadsafe``.

    Per-dispatcher failure is contained by :func:`_poll_with_retry`:
    transient network loss retries quietly with capped backoff, a
    permanently rejected token ends that one dispatcher with a single ERROR,
    and every other tenant's bot keeps running. The outer ``except
    Exception`` here is the last-line defence for the *thread* — a failure
    while building the dispatcher set — and logs with a full traceback. The
    web process is unaffected in every case.

    Args:
        config: The validated bot configuration.
    """
    global _bot_loop, _bot_engine

    try:
        from aiogram import Bot, Dispatcher, F
        from aiogram.exceptions import (
            TelegramNetworkError,
            TelegramUnauthorizedError,
        )
    except ImportError as exc:
        logger.error(
            "aiogram is not installed; cannot start the Telegram bot. "
            "Install it via `pip install -e '.[bot]'`. (%s)",
            exc,
        )
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _bot_loop = loop

    # The worker's own engine, for the per-turn credential resolution
    # (ADR-0112 §4b) and the per-message pairing lookup (§5). Created here so
    # it belongs to this loop and to no other; disposed on the same loop in
    # the ``finally`` below. Engine construction is lazy (no connection is
    # opened until first use), so a bad URL surfaces on the first turn as a
    # resolution failure — the polite error path — rather than as a dead bot
    # thread.
    if _bot_database_url:
        try:
            _bot_engine = create_async_engine(_bot_database_url, future=True, pool_pre_ping=True)
        except Exception:  # noqa: BLE001 — degrade to env-only resolution
            logger.exception(
                "Telegram bot: could not build the credential-resolution "
                "engine; falling back to environment-only resolution."
            )
            _bot_engine = None

    bots: list[Bot] = []
    try:
        discovered = loop.run_until_complete(_discover_bots(config))
        if not discovered:
            logger.info(
                "Telegram bot: no bot token discovered in any tenant and none "
                "in the environment; nothing to poll."
            )
            return

        runners: list[tuple[_BotBinding, Callable[[], Awaitable[None]]]] = []
        for entry in discovered:
            binding = _BotBinding(
                tenant_id=entry.tenant_id,
                source=entry.source,
                label=_binding_label(entry.tenant_id, entry.source),
            )
            aiobot = Bot(token=entry.token)
            bots.append(aiobot)
            dispatcher = Dispatcher()
            _register_handlers(
                dispatcher,
                F,
                aiobot=aiobot,
                config=config,
                binding=binding,
            )
            runners.append((binding, _polling_runner(dispatcher, aiobot)))
            logger.info("Telegram bot [%s]: dispatcher registered.", binding.label)

        logger.info("Telegram bot: polling %d dispatcher(s).", len(runners))
        loop.run_until_complete(
            _run_dispatchers(
                runners,
                network_error=TelegramNetworkError,
                unauthorized_error=TelegramUnauthorizedError,
                stop_event=_bot_stop_event,
            )
        )
    except RuntimeError as exc:
        # ``loop.stop()`` from the fallback path in :func:`stop_bot` while
        # ``run_until_complete`` was still awaiting the gather.
        logger.info("Telegram bot: worker loop stopped (%s).", exc)
    except Exception:  # noqa: BLE001 — last-line defence for the worker thread
        # The operator-only Telegram surface deliberately keeps its own
        # handling here; the web-side equivalent is web.errors.user_safe_error,
        # which masks foreign exception detail in user-facing responses.
        logger.exception("Telegram bot terminated due to unexpected error.")
    finally:
        for aiobot in bots:
            try:
                loop.run_until_complete(aiobot.session.close())
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.debug("Telegram bot: session close raised; ignored.")
        # Dispose the resolution engine on the loop that owns it, before the
        # loop closes — an asyncpg pool cannot be disposed from anywhere else.
        if _bot_engine is not None:
            try:
                loop.run_until_complete(_bot_engine.dispose())
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.debug("Telegram bot: engine dispose raised; ignored.")
            _bot_engine = None
        try:
            loop.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.debug("Telegram bot: loop close raised; ignored.")


def _polling_runner(dispatcher: Any, aiobot: Any) -> Callable[[], Awaitable[None]]:
    """Return a zero-argument callable that starts this dispatcher's polling.

    A factory rather than a lambda in the loop body: a lambda would close
    over the loop variable and every runner would end up polling the *last*
    dispatcher.
    """

    def _run() -> Awaitable[None]:
        return dispatcher.start_polling(aiobot, handle_signals=False)

    return _run


def _register_handlers(
    dispatcher: Any,
    magic_filter: Any,
    *,
    aiobot: Any,
    config: BotSettings,
    binding: _BotBinding,
) -> None:
    """Register one dispatcher's handlers, closed over its binding.

    Registration order is load-bearing. aiogram dispatches to the first
    matching handler and ``@dp.message()`` matches everything, so both
    filtered handlers must come before the catch-all:

    * ``/pair`` first — it is the one command that runs *before*
      authorisation, so an unpaired user can pair at all;
    * then voice — a voice message carries no ``text``, so the bare
      catch-all would otherwise swallow it.

    Args:
        dispatcher: The aiogram :class:`Dispatcher` to register on.
        magic_filter: aiogram's ``F`` magic-filter factory.
        aiobot: The aiogram :class:`Bot` this dispatcher polls with.
        config: The validated bot configuration.
        binding: This dispatcher's identity, closed over by every handler.
    """

    @dispatcher.message(magic_filter.text.startswith(_PAIR_COMMAND))
    async def _on_pair(message: Any) -> None:
        await _handle_pair_command(aiobot, message, config, binding=binding)

    @dispatcher.message(magic_filter.voice)
    async def _on_voice(message: Any) -> None:
        await _handle_voice_message(aiobot, message, config, binding=binding)

    @dispatcher.message()
    async def _on_message(message: Any) -> None:
        await _handle_text_message(aiobot, message, config, binding=binding)


# ---------------------------------------------------------------------------
# Authorisation and pairing (ADR-0112 §5)
# ---------------------------------------------------------------------------


async def _paired_user_id(binding: _BotBinding, chat_id: int) -> UUID | None:
    """Return the user this chat is bound to in the dispatcher's tenant.

    The authorisation read (D5): a user-scope ``telegram.chat_id`` row whose
    value is this chat's id. Read fresh per message — the volume is a
    handful of messages a minute and the engine pools, so a cache would buy
    latency nobody notices at the price of a revoke that does not bite until
    it expires.

    There is no by-value lookup on
    :class:`~core.repositories.scoped_setting_repository.ScopedSettingRepository`
    (and ADR-0112 §5 adds no repository surface), so the tenant's users are
    enumerated and each one's row is read through the repository's own
    user-filtered path — never the ORM directly.

    Any failure resolves to "not paired": a database outage must fail
    *closed*, never admit a stranger.

    Args:
        binding: The dispatcher's identity; its tenant scopes the lookup.
        chat_id: The inbound Telegram chat id.

    Returns:
        The paired user's id, or ``None`` when this chat is bound to nobody
        in this tenant.
    """
    engine = _bot_engine
    if engine is None or binding.tenant_id is None:
        return None
    try:
        async with tenant_context(engine, binding.tenant_id) as db:
            settings_repository = ScopedSettingRepository(db)
            for user in await UserRepository(db).list_all():
                row = await settings_repository.get("user", "telegram", "chat_id", user_id=user.id)
                if row is not None and row.enabled and (row.value_plain or "") == str(chat_id):
                    return user.id
    except Exception:  # noqa: BLE001 — fail closed, never admit on error
        logger.exception(
            "Telegram bot [%s]: pairing lookup failed; treating the chat as unpaired.",
            binding.label,
        )
    return None


def _warn_whitelist_deprecated_once() -> None:
    """Emit the ``TELEGRAM_ALLOWED_USER_IDS`` deprecation WARNING once.

    Once per process rather than once per message: a per-message warning
    would drown the log an operator reads to notice the deprecation in the
    first place. Cleared by :func:`stop_bot`.
    """
    global _whitelist_deprecation_warned
    if _whitelist_deprecation_warned:
        return
    _whitelist_deprecation_warned = True
    logger.warning(
        "Telegram bot: TELEGRAM_ALLOWED_USER_IDS admitted a turn. The "
        "whitelist is deprecated (ADR-0112 §5) and serves only the "
        "environment-token bot; it grants no user identity, so user-scope "
        "settings do not apply. Pair the chat instead (Admin → Providers & "
        "Credentials → Telegram → Generate pairing code) and remove the id "
        "from .env."
    )


async def _authorise(
    message: Any,
    config: BotSettings,
    binding: _BotBinding,
) -> tuple[bool, UUID | None]:
    """Decide whether this message may drive a turn, and as whom.

    Two admissions (ADR-0112 §5, D5), in order:

    1. **Pairing** — a ``telegram.chat_id`` row in the dispatcher's tenant
       binds this chat to a user. The turn then runs *as that user*, so
       user-scope settings apply to it.
    2. **The deprecated whitelist** — only on the environment-token
       dispatcher, and only with no user identity. A tenant that stores its
       own token is pairing-only from the start.

    Anything else is refused; the caller drops the message silently.

    Args:
        message: The inbound aiogram message.
        config: The validated bot configuration, for the whitelist.
        binding: The dispatcher's identity.

    Returns:
        ``(authorised, paired_user_id)``. ``paired_user_id`` is ``None`` for
        a whitelist admission — it carries no identity to resolve with.
    """
    user = getattr(message, "from_user", None)
    if user is None:
        return False, None

    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None:
        paired = await _paired_user_id(binding, chat_id)
        if paired is not None:
            return True, paired

    if binding.source == SOURCE_ENV_FALLBACK and user.id in config.allowed_user_ids:
        _warn_whitelist_deprecated_once()
        return True, None

    return False, None


def _pair_code_argument(text: str) -> str:
    """Return the code from a ``/pair …`` message, or an empty string.

    Tolerates the ``/pair@botname`` form Telegram uses when a bot is
    addressed in a group: private chats never produce it, but a command
    parser that breaks on it would be a puzzle to debug.
    """
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].split("@", 1)[0].lower() != _PAIR_COMMAND:
        return ""
    return parts[1].strip() if len(parts) > 1 else ""


async def _handle_pair_command(
    aiobot: Any,
    message: Any,
    config: BotSettings,
    binding: _BotBinding | None = None,
) -> None:
    """Bind this chat to the user who minted the code (ADR-0112 §5).

    Runs **before** authorisation — an unpaired chat is exactly the one that
    needs to pair. What stands in for authorisation is possession of a
    five-minute, single-use code from
    :mod:`services.telegram_pairing`, plus a throttle on how many a chat may
    try; the code's tenant must match this dispatcher's, so a code minted in
    one tenant can never bind a chat in another.

    Every failure — unknown code, expired code, wrong tenant, throttled
    chat, missing argument beyond the usage hint — answers with the same
    generic line. The code itself is never logged.

    Args:
        aiobot: The aiogram :class:`Bot` instance.
        message: The inbound aiogram message, carrying ``/pair <code>``.
        config: The validated bot configuration (unused here; kept so every
            handler has one shape).
        binding: The dispatcher's identity. ``None`` falls back to the
            transition binding (see :func:`_default_binding`).
    """
    del config  # part of the shared handler signature; nothing to read here
    binding = binding if binding is not None else _default_binding()
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return

    code = _pair_code_argument(getattr(message, "text", None) or "")
    if not code:
        await _reply(aiobot, chat_id, _PAIR_USAGE_MESSAGE, binding)
        return

    if not telegram_pairing.note_attempt(chat_id):
        logger.warning(
            "Telegram bot [%s]: /pair throttled for chat_id=%s.",
            binding.label,
            chat_id,
        )
        await _reply(aiobot, chat_id, _PAIR_FAILED_MESSAGE, binding)
        return

    user_id = telegram_pairing.redeem_code(code, tenant_id=binding.tenant_id)
    if user_id is None:
        logger.warning(
            "Telegram bot [%s]: /pair rejected for chat_id=%s (unknown, expired or foreign code).",
            binding.label,
            chat_id,
        )
        await _reply(aiobot, chat_id, _PAIR_FAILED_MESSAGE, binding)
        return

    engine = _bot_engine
    if engine is None or binding.tenant_id is None:
        logger.error(
            "Telegram bot [%s]: /pair redeemed but no database engine is "
            "available to store the binding.",
            binding.label,
        )
        await _reply(aiobot, chat_id, _PAIR_UNAVAILABLE_MESSAGE, binding)
        return

    try:
        async with tenant_context(engine, binding.tenant_id, user_id=user_id) as db:
            await ScopedSettingRepository(db).upsert(
                scope="user",
                provider="telegram",
                key="chat_id",
                is_secret=False,
                value_plain=str(chat_id),
                user_id=user_id,
            )
    except Exception:  # noqa: BLE001 — surface to the user, not the dispatcher
        logger.exception(
            "Telegram bot [%s]: failed to store the chat binding for user %s.",
            binding.label,
            user_id,
        )
        await _reply(aiobot, chat_id, _PAIR_UNAVAILABLE_MESSAGE, binding)
        return

    logger.info(
        "Telegram bot [%s]: chat_id=%s paired to user %s.",
        binding.label,
        chat_id,
        user_id,
    )
    await _reply(aiobot, chat_id, _PAIR_SUCCESS_MESSAGE, binding)


async def _reply(aiobot: Any, chat_id: int, text: str, binding: _BotBinding) -> None:
    """Send one message, logging a send failure instead of raising.

    A raise out of a handler kills that dispatcher's update loop, so every
    outbound send in the pairing path goes through here.
    """
    try:
        await aiobot.send_message(chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001 — a failed send must not kill the dispatcher
        logger.exception("Telegram bot [%s]: failed to send a reply.", binding.label)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _handle_text_message(
    aiobot: Any,
    message: Any,
    config: BotSettings,
    binding: _BotBinding | None = None,
) -> None:
    """Text/image entry point: resolve one inbound message, then run the turn.

    The handler authorises the sender (:func:`_authorise` — pairing, or the
    deprecated whitelist on the environment dispatcher), detects and
    downloads an inbound image (ADR-0075) with its vision/size gates, handles
    the ``/reset`` (``/new``) reset command, and resolves the caption into the
    turn's text. It then converges on :func:`_run_turn` (``is_voice=False``),
    which owns the shared "typing"/placeholder UX, the
    :meth:`~services.ai_service_core.AIServiceCore.stream_response` loop, the
    text/chart sends, and the bounded per-chat history. The voice entry point
    :func:`_handle_voice_message` converges on the same :func:`_run_turn`.

    Args:
        aiobot: The aiogram :class:`Bot` instance.
        message: The aiogram :class:`Message` received from the
            dispatcher.
        config: The validated bot configuration.
        binding: The dispatcher's identity (ADR-0112 §5), closed over at
            registration. ``None`` falls back to the transition binding
            (see :func:`_default_binding`).
    """
    binding = binding if binding is not None else _default_binding()
    user = getattr(message, "from_user", None)
    incoming_text = getattr(message, "text", None) or ""

    authorised, paired_user_id = await _authorise(message, config, binding)
    if not authorised:
        logger.warning(
            "Telegram bot [%s]: rejected message from user_id=%s (text=%r)",
            binding.label,
            user.id if user is not None else None,
            incoming_text[:50],
        )
        return

    # Detect an inbound image (ADR-0075): a photo (largest PhotoSize) or
    # an image-typed document. ``getattr`` with defaults keeps text-only
    # messages — and the existing test fakes — unaffected.
    photo_sizes = getattr(message, "photo", None) or []
    document = getattr(message, "document", None)
    doc_mime = (getattr(document, "mime_type", None) or "") if document is not None else ""
    image_source: Any | None = None
    image_mime = ""
    image_filename = "upload"
    if photo_sizes:
        image_source = photo_sizes[-1]  # largest size
        image_mime = "image/jpeg"  # Telegram photos are always JPEG
        image_filename = "photo.jpg"
    elif document is not None and doc_mime in ALLOWED_IMAGE_MIME_TYPES:
        image_source = document
        image_mime = doc_mime
        image_filename = getattr(document, "file_name", None) or "upload"

    if not incoming_text and image_source is None:
        # Authorised user, but neither text nor a supported image
        # (e.g. a sticker, a non-image document). Silently ignore.
        return

    user_id = user.id
    chat_id = message.chat.id

    # Reset command — the Telegram analogue of the web "New chat" button.
    # Handled before any work so it never reaches the core or the history.
    # A photo never carries "/reset" as text, so image captions are not
    # consulted here. Clears this dispatcher's tenant-scoped entry only: the
    # same chat talking to another tenant's bot keeps its own history.
    if incoming_text.strip().lower() in {"/reset", "/new"}:
        _chat_histories.pop(_history_key(binding, chat_id), None)
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text="🧹 Context cleared. New conversation.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send reset ack.")
        return

    # Image handling (ADR-0075): vision gate, download, size guard, then
    # build the Attachment that rides on this turn's user message. Done
    # before the typing indicator so a rejected image surfaces a clear
    # reply instead of a hanging "typing" state. ``attachment`` stays
    # ``None`` for a text-only turn.
    attachment: Attachment | None = None
    resolved: ResolvedLLM | None = None
    if image_source is not None:
        # The vision gate asks about *this turn's* model, so the turn has to
        # resolve before the gate rather than inside :func:`_run_turn`
        # (ADR-0112 §4b — the model is the tenant's, not the process's). The
        # resolution is threaded on so the turn resolves exactly once.
        try:
            resolved = await _resolve_bot_llm(
                config, tenant_id=binding.tenant_id, user_id=paired_user_id
            )
        except (CredentialUnavailableError, _BotLLMUnconfiguredError) as exc:
            logger.error(
                "Telegram bot [%s]: no LLM resolved for user_id=%s: %s",
                binding.label,
                user.id,
                exc,
            )
            try:
                await aiobot.send_message(chat_id=chat_id, text=f"⚠️ {_NO_LLM_MESSAGE}")
            except Exception:  # noqa: BLE001
                logger.exception("Telegram bot: failed to send no-LLM reply.")
            return
        if not supports_vision(resolved.model):
            try:
                await aiobot.send_message(
                    chat_id=chat_id,
                    text=("⚠️ The currently configured model cannot process images."),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Telegram bot: failed to send vision-gate reply.")
            return

        try:
            buffer = await aiobot.download(image_source.file_id)
            image_data = buffer.read()
        except Exception:  # noqa: BLE001 — surface to the user, not the dispatcher
            logger.exception("Telegram bot: failed to download inbound image.")
            try:
                await aiobot.send_message(
                    chat_id=chat_id,
                    text="⚠️ The image could not be downloaded. Please try again.",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Telegram bot: failed to send download-error reply.")
            return

        if len(image_data) > MAX_IMAGE_BYTES:
            try:
                await aiobot.send_message(
                    chat_id=chat_id,
                    text="⚠️ The image is too large (max. 8 MB).",
                )
            except Exception:  # noqa: BLE001
                logger.exception("Telegram bot: failed to send oversize reply.")
            return

        attachment = Attachment(filename=image_filename, mime_type=image_mime, data=image_data)
        # Caption becomes the turn's text; fall back to a default
        # instruction when the image arrived without one.
        caption = (getattr(message, "caption", None) or "").strip()
        incoming_text = caption or ("Please analyse this image in the context of my portfolio.")

    logger.info(
        "Telegram bot [%s]: incoming prompt from user_id=%s: %s",
        binding.label,
        user_id,
        incoming_text,
    )

    await _run_turn(
        aiobot,
        config,
        binding=binding,
        chat_id=chat_id,
        user_id=user_id,
        paired_user_id=paired_user_id,
        incoming_text=incoming_text,
        attachment=attachment,
        is_voice=False,
        llm=resolved,
    )


async def _run_turn(
    aiobot: Any,
    config: BotSettings,
    *,
    binding: _BotBinding,
    chat_id: int,
    user_id: int,
    paired_user_id: UUID | None = None,
    incoming_text: str,
    attachment: Attachment | None,
    is_voice: bool = False,
    llm: ResolvedLLM | None = None,
    voice: ResolvedVoice | None = None,
) -> None:
    """Shared turn body for the text/image and voice entry points.

    Runs the typing/placeholder UX, drives
    :meth:`~services.ai_service_core.AIServiceCore.stream_response`, sends the
    assembled prose as text chunk(s) and charts as PNG photos, and persists the
    bounded per-chat history (``_chat_histories``, keyed by
    ``(tenant_id, chat_id)`` since ADR-0112 §5, rebuilt into the
    :class:`~services.ai_models.Conversation` each turn, extended only
    on success, trimmed to ``_MAX_HISTORY_MESSAGES``) — all exactly as the text
    path always has. For a voice-initiated turn (``is_voice=True``) it
    additionally synthesises the prose and sends it as a Telegram voice note
    (with a ``send_audio`` fallback). Voice never changes the
    text/chart/history behaviour.

    Args:
        aiobot: The aiogram :class:`Bot` instance.
        config: The validated bot configuration.
        binding: The dispatcher's identity — the tenant this turn resolves,
            reads and remembers in.
        chat_id: The Telegram chat the reply is posted to and whose history
            this turn reads and extends.
        user_id: The sender's Telegram user id (logging only).
        paired_user_id: The PortfoliFLOW user this chat is bound to, when it
            is paired (ADR-0112 §5). Threaded into the credential resolution
            so user-scope rows apply to the turn; ``None`` on the deprecated
            whitelist path, which carries no identity.
        incoming_text: The resolved user text for this turn — a typed message,
            an image caption, or a voice transcript.
        attachment: An optional image attachment riding on the user message
            (ADR-0075). Always ``None`` for a voice turn — a Telegram voice
            message cannot carry a photo.
        is_voice: When ``True`` the assembled prose is additionally sent as a
            synthesised voice note after the text reply.
        llm: A resolution the caller already performed (the image path needs
            the model for its vision gate). ``None`` — the ordinary case —
            resolves here, so every turn resolves exactly once.
        voice: The voice resolution of a voice-initiated turn, threaded so the
            message resolves exactly once (ADR-0118 §4); ``None`` for text
            turns.
    """
    # Initial typing indicator. Failures are logged but do not abort the
    # turn — the user will simply not see the indicator.
    try:
        await aiobot.send_chat_action(chat_id=chat_id, action="typing")
    except Exception:  # noqa: BLE001 — UX nicety, not a hard failure
        logger.exception("Telegram bot: initial send_chat_action failed.")

    # Mutable holders so the inner coroutines can publish back to the outer
    # scope. A single-element list keeps the assignment simple without
    # needing ``nonlocal`` plumbing across two coroutines.
    placeholder_message: list[Any] = [None]

    async def _send_placeholder() -> None:
        try:
            await asyncio.sleep(_PLACEHOLDER_DELAY_SECONDS)
            placeholder_message[0] = await aiobot.send_message(
                chat_id=chat_id, text=_PLACEHOLDER_TEXT
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — UX nicety
            logger.exception("Telegram bot: failed to send placeholder.")

    async def _refresh_typing() -> None:
        try:
            while True:
                await asyncio.sleep(_TYPING_REFRESH_SECONDS)
                try:
                    await aiobot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:  # noqa: BLE001 — log and keep refreshing
                    logger.exception("Telegram bot: typing refresh failed; will retry.")
        except asyncio.CancelledError:
            raise

    placeholder_task = asyncio.create_task(_send_placeholder())
    typing_task = asyncio.create_task(_refresh_typing())

    accumulated_text = ""
    charts: list[_ChartArtifact] = []
    error_message: str | None = None
    crashed = False

    try:
        core = _bot_ai_core()
        # This turn's endpoint, credential and model (ADR-0112 §4b). Nothing
        # is cached between turns: a key written in Providers & Credentials
        # is live on the very next message, with no bot restart.
        resolved = (
            llm
            if llm is not None
            else await _resolve_bot_llm(
                config,
                tenant_id=binding.tenant_id,
                user_id=paired_user_id,
            )
        )
        system_prompt = core.get_system_prompt()
        # Rebuild the conversation from this chat's stored history (plain
        # user/assistant text only) and append the new user message. The
        # user message reference is kept so a successful turn can persist it.
        history = _chat_histories.get(_history_key(binding, chat_id), [])
        conv = Conversation()
        for past in history:
            conv.add_message(past)
        user_msg = Message(
            role=MessageRole.USER,
            content=incoming_text,
            attachments=[attachment] if attachment is not None else [],
        )
        conv.add_message(user_msg)

        # Per-turn tool-execution context (ADR-0063). The core sets it under
        # ``_TURN_LOCK`` and clears it when the turn ends. Built only when the
        # web lifespan injected both a tenant id and a database URL; on the
        # desktop entry point both are unset and the tools degrade gracefully.
        tool_context = (
            ToolExecutionContext(
                tenant_id=binding.tenant_id,
                database_url=_bot_database_url,
            )
            if (binding.tenant_id is not None and _bot_database_url)
            else None
        )
        async for event in core.stream_response(
            conv, system_prompt, tool_context=tool_context, llm=resolved
        ):
            etype = event.event_type
            if etype == "chunk":
                accumulated_text += event.payload.get("text", "")
            elif etype == "chart_artifact":
                charts.append(
                    _ChartArtifact(
                        image_base64=event.payload.get("image_base64", ""),
                        caption=event.payload.get("caption", ""),
                    )
                )
            elif etype == "error":
                error_message = event.payload.get("message", "Unknown error")
                break
            # ``tool_called``, ``tool_completed``, ``stream_finished`` carry
            # no user-visible payload for the bot — text already arrived as
            # ``chunk`` events; charts already arrived as ``chart_artifact``.
    except (CredentialUnavailableError, _BotLLMUnconfiguredError) as exc:
        # Nothing resolved for this tenant. Not a crash: the turn answers
        # through the ordinary polite error path below, naming both scopes
        # an operator can fix it in.
        logger.error("Telegram bot: no LLM resolved for user_id=%s: %s", user_id, exc)
        error_message = _NO_LLM_MESSAGE
    except Exception:  # noqa: BLE001 — must surface to the user, not the dispatcher
        logger.exception(
            "Telegram bot: unexpected error during stream_response for user_id=%s",
            user_id,
        )
        crashed = True
    finally:
        for task in (placeholder_task, typing_task):
            task.cancel()
        for task in (placeholder_task, typing_task):
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await task

        if placeholder_message[0] is not None:
            try:
                await aiobot.delete_message(
                    chat_id=chat_id,
                    message_id=placeholder_message[0].message_id,
                )
            except Exception:  # noqa: BLE001 — Telegram may have rate-limited the delete
                logger.exception("Telegram bot: failed to delete placeholder.")

    if crashed:
        # The async generator itself raised — surface a generic error to
        # the user. ``logger.exception`` above already captured the detail.
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text="⚠️ Unexpected internal error. Please try again.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send fallback error reply.")
        return

    if error_message is not None:
        logger.error("Telegram bot: turn failed for user_id=%s: %s", user_id, error_message)
        try:
            await aiobot.send_message(chat_id=chat_id, text=f"⚠️ {error_message}")
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send error reply.")
        return

    if accumulated_text:
        for chunk in _split_text_for_telegram(accumulated_text):
            try:
                await aiobot.send_message(chat_id=chat_id, text=chunk)
            except Exception:  # noqa: BLE001
                logger.exception("Telegram bot: failed to send text chunk.")

    # Voice reply (additive): for a voice-initiated turn, speak the assembled
    # prose as a Telegram voice note *after* the text reply (so the transcript
    # stays visible). A chart-only / empty-prose turn speaks nothing, matching
    # the web "prose-empty turn speaks nothing" rule. A TTS failure is logged
    # and skipped — the text reply has already gone out, so it must not break
    # the turn.
    # The resolution is the handler's, threaded on: ``voice is None`` with
    # ``is_voice=True`` cannot occur through :func:`_handle_voice_message`, so
    # the guard is defensive and free.
    if is_voice and accumulated_text.strip() and voice is not None:
        audio_bytes: bytes | None = None
        try:
            provider = build_provider(voice)
            audio_bytes, _mime = await provider.synthesize(accumulated_text, fmt="opus")
        except VoiceError:
            logger.warning(
                "Telegram bot: TTS synthesis failed; voice note skipped (text reply already sent).",
                exc_info=True,
            )
        if audio_bytes:
            try:
                from aiogram.types import BufferedInputFile
            except ImportError:
                logger.error("Telegram bot: aiogram unavailable; cannot send voice note.")
            else:
                # ``send_voice`` renders a playable voice note only for an
                # OGG/Opus container; OpenAI ``response_format="opus"`` returns
                # exactly that. The ``.ogg`` filename is a container hint for
                # Telegram/aiogram. If Telegram still refuses to render it as a
                # voice note, fall back to ``send_audio`` (a file/audio player)
                # so the audio still reaches the user — dependency-free, no
                # ffmpeg transmux (a documented Round-1 follow-up).
                try:
                    await aiobot.send_voice(
                        chat_id=chat_id,
                        voice=BufferedInputFile(audio_bytes, filename="reply.ogg"),
                    )
                except Exception:  # noqa: BLE001 — container Telegram won't render as a voice note
                    logger.warning(
                        "Telegram bot: send_voice failed; falling back to send_audio.",
                        exc_info=True,
                    )
                    try:
                        await aiobot.send_audio(
                            chat_id=chat_id,
                            audio=BufferedInputFile(audio_bytes, filename="reply.ogg"),
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Telegram bot: send_audio fallback also failed; "
                            "voice reply dropped (text already sent)."
                        )

    if charts:
        try:
            from aiogram.types import BufferedInputFile
        except ImportError:
            logger.error("Telegram bot: aiogram unavailable while sending charts; skipping.")
        else:
            for chart in charts:
                try:
                    image_bytes = base64.b64decode(chart.image_base64)
                except (ValueError, TypeError):
                    logger.exception("Telegram bot: chart artefact had invalid Base64; skipping.")
                    continue

                caption = chart.caption or ""
                if len(caption) > _TELEGRAM_CAPTION_LIMIT:
                    # Send the full caption as text first so nothing is lost,
                    # then truncate the photo's caption.
                    for chunk in _split_text_for_telegram(caption):
                        try:
                            await aiobot.send_message(chat_id=chat_id, text=chunk)
                        except Exception:  # noqa: BLE001
                            logger.exception("Telegram bot: failed to send caption text.")
                    photo_caption = caption[:_TELEGRAM_CAPTION_TRUNCATE_AT] + "…"
                else:
                    photo_caption = caption

                try:
                    await aiobot.send_photo(
                        chat_id=chat_id,
                        photo=BufferedInputFile(image_bytes, filename="chart.png"),
                        caption=photo_caption,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Telegram bot: failed to send chart photo.")

    # Persist the exchange into this chat's history — but only for a turn
    # that neither crashed nor surfaced an error event (those return early
    # above, so reaching here already implies success; the guard is kept
    # explicit and defensive). Store plain user/assistant text only; the
    # assistant content may be empty (e.g. a chart-only reply) and replays
    # harmlessly, keeping the user/assistant alternation intact. Trim to the
    # last N messages to bound token growth.
    if not crashed and error_message is None:
        # Persist text-only: never write image bytes into history so the
        # OpenAI replay via Conversation.to_openai_messages() never
        # re-sends them (ADR-0075 single-turn vision contract). For a
        # text-only turn the original ``user_msg`` already carries no
        # attachments and is reused unchanged.
        persisted_user_msg = (
            Message(role=MessageRole.USER, content=incoming_text)
            if attachment is not None
            else user_msg
        )
        assistant_msg = Message(role=MessageRole.ASSISTANT, content=accumulated_text)
        updated = [*history, persisted_user_msg, assistant_msg]
        _chat_histories[_history_key(binding, chat_id)] = updated[-_MAX_HISTORY_MESSAGES:]

    logger.info(
        "Telegram bot [%s]: outgoing reply to user_id=%s (chars=%d, charts=%d).",
        binding.label,
        user_id,
        len(accumulated_text),
        len(charts),
    )


async def _handle_voice_message(
    aiobot: Any,
    message: Any,
    config: BotSettings,
    binding: _BotBinding | None = None,
) -> None:
    """Transcribe an inbound Telegram voice message and run the same turn.

    Authorised exactly like the text handler (:func:`_authorise` — pairing,
    or the deprecated whitelist on the environment dispatcher). The order is
    gate, download, resolve, transcribe: the ``voice.enabled`` chain answers
    per tenant (ADR-0118 §5), then the OGG/Opus bytes are fetched, then this
    message's :class:`~services.voice.ResolvedVoice` is resolved **once**
    (ADR-0118 §4) and used by both legs — the STT provider built here and the
    TTS provider built in :func:`_run_turn`, which the resolution is threaded
    onto. Converges on the shared :func:`_run_turn` with ``is_voice=True``.

    The disabled-service, unconfigured-credential, undecryptable-vault,
    empty-transcript and STT-failure cases each reply with a clear English
    message and do **not** drive a turn — "no silent fallback" (ADR-0076). A
    voice turn carries no attachment: a Telegram voice message cannot also
    carry a photo.

    Args:
        aiobot: The aiogram :class:`Bot` instance.
        message: The aiogram :class:`Message` (guaranteed to carry ``voice`` by
            the ``F.voice`` dispatch filter).
        config: The validated bot configuration.
        binding: The dispatcher's identity (ADR-0112 §5). ``None`` falls back
            to the transition binding (see :func:`_default_binding`).
    """
    binding = binding if binding is not None else _default_binding()
    user = getattr(message, "from_user", None)
    authorised, paired_user_id = await _authorise(message, config, binding)
    if not authorised:
        logger.warning(
            "Telegram bot [%s]: rejected voice message from user_id=%s",
            binding.label,
            user.id if user is not None else None,
        )
        return
    voice_note = getattr(message, "voice", None)
    if voice_note is None:
        return  # defensive — F.voice guarantees presence
    chat_id = message.chat.id
    user_id = user.id

    # Voice gating (ADR-0118 §5): the ``voice.enabled`` chain answers per
    # tenant, so one deployment's dispatchers can differ. A disabled tenant
    # replies clearly, downloads nothing and resolves no credential.
    if not await _resolve_bot_voice_enabled(binding):
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text="🎤 Voice messages are not enabled at the moment.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send voice-disabled reply.")
        return

    # Download the OGG/Opus bytes (mirrors the image-download path).
    try:
        buffer = await aiobot.download(voice_note.file_id)
        audio_data = buffer.read()
    except Exception:  # noqa: BLE001 — surface to the user, not the dispatcher
        logger.exception("Telegram bot: failed to download inbound voice message.")
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text=("⚠️ The voice message could not be downloaded. Please try again."),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send voice-download-error reply.")
        return

    mime = (getattr(voice_note, "mime_type", None) or "audio/ogg").lower()

    # This message's voice resolution (ADR-0118 §4), performed exactly once and
    # threaded onto the turn so the TTS leg speaks with the same endpoints and
    # keys the STT leg heard with. Nothing is cached between messages: a key
    # written in Providers & Credentials is live on the very next voice note.
    try:
        voice = await _resolve_bot_voice(binding)
    except (CredentialUnavailableError, _BotVoiceUnconfiguredError) as exc:
        logger.error(
            "Telegram bot [%s]: no voice credential resolved for user_id=%s: %s",
            binding.label,
            user_id,
            exc,
        )
        try:
            await aiobot.send_message(chat_id=chat_id, text=f"⚠️ {_NO_VOICE_MESSAGE}")
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send no-voice-credential reply.")
        return
    except VaultDecryptError:
        # An operator emergency, not a configure-me nudge — and it must not
        # reach the aiogram dispatcher either. Logged loudly, answered
        # generically. (The web surface propagates this to a 500; the bot has
        # no such boundary, which is the one deliberate divergence.)
        logger.exception(
            "Telegram bot [%s]: voice credential vault refused to decrypt.",
            binding.label,
        )
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text="⚠️ Voice is temporarily unavailable. Please try again later.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send voice-unavailable reply.")
        return

    # Transcribe (no silent fallback).
    try:
        provider = build_provider(voice)
        transcript = (await provider.transcribe(audio_data, mime)).strip()
    except EmptyTranscriptError:
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text="🎤 I could not detect any speech. Please try again.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send empty-transcript reply.")
        return
    except (UnsupportedAudioFormatError, VoiceError):
        logger.exception("Telegram bot: STT failed for user_id=%s", user_id)
        try:
            await aiobot.send_message(
                chat_id=chat_id,
                text=("⚠️ Transcription failed. Please try again or send your message as text."),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Telegram bot: failed to send STT-failure reply.")
        return

    logger.info(
        "Telegram bot [%s]: voice transcript from user_id=%s: %s",
        binding.label,
        user_id,
        transcript,
    )
    await _run_turn(
        aiobot,
        config,
        binding=binding,
        chat_id=chat_id,
        user_id=user_id,
        paired_user_id=paired_user_id,
        incoming_text=transcript,
        attachment=None,
        is_voice=True,
        voice=voice,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_text_for_telegram(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Splits on paragraph boundaries (``"\\n\\n"``) where possible so each
    chunk is a coherent unit. A paragraph longer than ``limit`` is split
    further at the nearest preceding whitespace; if no whitespace exists in
    the prefix the function falls back to a hard cut at ``limit``.

    Args:
        text: The text to split.
        limit: Maximum length of any single chunk in characters.

    Returns:
        The list of chunks in order. Empty if ``text`` is empty. A single
        element if ``text`` is already short enough.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        if len(paragraph) > limit:
            # Flush whatever we had collected so far before tackling this
            # oversized paragraph in isolation.
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_paragraph(paragraph, limit))
            continue

        candidate = (current + "\n\n" + paragraph) if current else paragraph
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)

    return chunks


def _split_long_paragraph(paragraph: str, limit: int) -> list[str]:
    """Split a single paragraph longer than ``limit`` at whitespace.

    Args:
        paragraph: A paragraph already known to exceed ``limit``.
        limit: Maximum chunk length in characters.

    Returns:
        The list of chunks in order. Each chunk is at most ``limit`` chars.
    """
    pieces: list[str] = []
    remaining = paragraph
    while len(remaining) > limit:
        split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        pieces.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _bot_ai_core() -> AIServiceCore:
    """Return the bot's :class:`AIServiceCore`, building it lazily.

    Superseded ``_build_bot_core`` / ``_get_bot_core`` at ADR-0112 §4b: the
    core is no longer *configured* at all. It carries the turn machinery —
    the system prompt, the registered tools, the streaming loop — while the
    endpoint, credential and model arrive per turn as a
    :class:`~services.ai_service_core.ResolvedLLM`. With nothing endpoint-
    shaped left on the instance, the old reason for keeping the bot's core
    separate from the GUI singleton (``QSettings`` versus ``.env``
    last-writer-wins) is gone; one instance per worker simply avoids
    re-registering tools each turn.

    Cached at module level for the lifetime of the bot worker thread;
    cleared by :func:`stop_bot`. The
    :class:`~services.tool_registry.ToolRegistry` and the module-level
    ``_TURN_LOCK`` in :mod:`services.ai_service_core` remain process-wide,
    so trust-class gating (ADR-0022) and turn serialisation (ADR-0031) are
    unaffected.

    Returns:
        The cached, deliberately unconfigured :class:`AIServiceCore`.
    """
    global _bot_core
    if _bot_core is None:
        _bot_core = AIServiceCore()
    return _bot_core


async def _resolve_bot_llm(
    config: BotSettings,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None = None,
) -> ResolvedLLM:
    """Resolve this turn's endpoint, credential and model (ADR-0112 §4b/§5).

    The chain is the web chat's, one dispatcher at a time. Inside the
    dispatcher tenant's context:

    * credential — vault user → vault tenant → env ``OPENROUTER_API_KEY``;
    * model      — vault user → vault tenant → env ``SHIRLEY_MODEL``;
    * base_url   — vault tenant → env ``OPENROUTER_BASE_URL`` →
      :attr:`BotSettings.openai_base_url`.

    The user link exists only for a **paired** chat (ADR-0112 §5): pairing is
    what gives a Telegram message an identity to resolve for, which is why
    §4b left this axis empty and F5 fills it. A whitelist-admitted turn still
    resolves tenant-then-environment.

    Without an engine or a tenant id (the desktop entry point) the resolver
    is built without a session and the environment is the only source — the
    same graceful degradation the Postgres-native tools take.

    Args:
        config: The validated bot configuration, for the ``base_url``
            default.
        tenant_id: The dispatcher's tenant — the tenant this turn resolves in.
        user_id: The paired user, or ``None``.

    Returns:
        The turn's :class:`~services.ai_service_core.ResolvedLLM`.

    Raises:
        CredentialUnavailableError: If no scope holds a credential.
        _BotLLMUnconfiguredError: If a credential resolved but no scope
            holds a model.
    """
    engine = _bot_engine
    if engine is None or tenant_id is None:
        # No vault sources — but the tenant id is still threaded when we have
        # one: it names the tenant the resolution is *for* in the façade's log
        # line, which is true whether or not a vault could serve it.
        return await _resolve_bot_llm_through(
            CredentialResolver(), config, tenant_id=tenant_id, user_id=user_id
        )
    async with tenant_context(engine, tenant_id, user_id=user_id) as db:
        return await _resolve_bot_llm_through(
            CredentialResolver(session=db), config, tenant_id=tenant_id, user_id=user_id
        )


async def _resolve_bot_llm_through(
    resolver: CredentialResolver,
    config: BotSettings,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None = None,
) -> ResolvedLLM:
    """Walk the three chains on ``resolver`` and assemble the resolution."""
    credential = await resolver.resolve("openrouter", tenant_id=tenant_id, user_id=user_id)
    if not isinstance(credential, ProviderCredential):
        # openrouter declares a secret field and is not optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise _BotLLMUnconfiguredError(_NO_LLM_MESSAGE)
    model = await resolver.resolve_config("openrouter", "model", user_id=user_id)
    if not model:
        raise _BotLLMUnconfiguredError(_NO_LLM_MESSAGE)
    # The base URL is a tenant-level endpoint choice, not a personal one —
    # the taxonomy declares it tenant-scope only, so it takes no user axis
    # (the web chat resolves it the same way).
    base_url = await resolver.resolve_config("openrouter", "base_url") or config.openai_base_url
    return ResolvedLLM(base_url=base_url, api_key=credential.payload["api_key"], model=model)


async def _resolve_bot_voice_enabled(binding: _BotBinding) -> bool:
    """Report whether voice is enabled for this dispatcher's tenant (ADR-0118 §5).

    Walks the ``voice.enabled`` chain and nothing else — never a credential
    probe. Whether the bot accepts voice messages and whether a key can be
    found are separate questions, and conflating them would hide a
    misconfiguration behind a bland "not enabled" reply instead of surfacing
    it at first use.

    Engine/tenant branching is :func:`_resolve_bot_llm`'s: without an engine
    or a tenant id (the desktop entry point) the resolver is built without a
    session and the environment is the only source.

    Args:
        binding: The dispatcher's identity, supplying the tenant.

    Returns:
        ``True`` when the chain yields ``"true"`` (case-insensitive) — the
        ``VOICE_ENABLED`` convention; ``False`` otherwise, including when the
        field is set nowhere.
    """
    engine = _bot_engine
    if engine is None or binding.tenant_id is None:
        value = await CredentialResolver().resolve_config(
            "voice", "enabled", scopes=("tenant", "env")
        )
    else:
        async with tenant_context(engine, binding.tenant_id) as db:
            value = await CredentialResolver(session=db).resolve_config(
                "voice", "enabled", scopes=("tenant", "env")
            )
    return value is not None and value.strip().lower() == "true"


async def _resolve_bot_voice(binding: _BotBinding) -> ResolvedVoice:
    """Resolve this message's voice endpoints, credentials and models (ADR-0118 §4).

    The voice twin of :func:`_resolve_bot_llm`, with the same engine-present /
    engine-less split and the same one-call lifetime for the plain keys. No
    user axis: every voice field is declared tenant-scope only (ADR-0118 §1),
    so a paired chat resolves exactly what an unpaired one does.

    Args:
        binding: The dispatcher's identity, supplying the tenant.

    Returns:
        The message's :class:`~services.voice.ResolvedVoice`.

    Raises:
        _BotVoiceUnconfiguredError: If either half's credential resolves to
            nothing.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never look like an absent credential. The handler
            answers it generically rather than letting it reach the
            dispatcher.
    """
    engine = _bot_engine
    if engine is None or binding.tenant_id is None:
        return await _resolve_bot_voice_through(CredentialResolver(), tenant_id=binding.tenant_id)
    async with tenant_context(engine, binding.tenant_id) as db:
        return await _resolve_bot_voice_through(
            CredentialResolver(session=db), tenant_id=binding.tenant_id
        )


async def _resolve_bot_voice_through(
    resolver: CredentialResolver,
    *,
    tenant_id: UUID | None,
) -> ResolvedVoice:
    """Walk the voice chains on ``resolver`` and assemble the resolution.

    Both halves resolve on every call, whichever leg will use the result: an
    enabled tenant holding one key of two is misconfigured, and which leg it
    notices on must not decide whether it hears about it (ADR-0118 §2).

    The duplication with the web surface's ``_resolve_voice_through`` is
    deliberate: each surface owns its resolution one layer above
    ``services/voice/``, exactly as the LLM twins do, and the bot may not
    import from ``web/`` (ADR-0030).
    """
    stt_api_key = await _resolve_bot_voice_key(resolver, "voice_stt", tenant_id=tenant_id)
    tts_api_key = await _resolve_bot_voice_key(resolver, "voice_tts", tenant_id=tenant_id)

    stt_model = await resolver.resolve_config("voice_stt", "model") or DEFAULT_STT_MODEL
    stt_base_url = await resolver.resolve_config("voice_stt", "base_url") or DEFAULT_STT_BASE_URL
    tts_model = await resolver.resolve_config("voice_tts", "model") or DEFAULT_TTS_MODEL
    tts_voice = await resolver.resolve_config("voice_tts", "voice") or DEFAULT_TTS_VOICE

    # The two provider keys are env-only by design: the taxonomy deliberately
    # does not declare them until a second adapter's ADR does (ADR-0118 §1),
    # so there is no per-tenant chain here for the façade to walk.
    stt_provider = os.getenv("VOICE_STT_PROVIDER", DEFAULT_VOICE_PROVIDER)
    tts_provider = os.getenv("VOICE_TTS_PROVIDER", DEFAULT_VOICE_PROVIDER)

    return ResolvedVoice(
        stt_provider=stt_provider,
        stt_model=stt_model,
        stt_api_key=stt_api_key,
        stt_base_url=stt_base_url,
        tts_provider=tts_provider,
        tts_model=tts_model,
        tts_voice=tts_voice,
        tts_api_key=tts_api_key,
    )


async def _resolve_bot_voice_key(
    resolver: CredentialResolver,
    provider: str,
    *,
    tenant_id: UUID | None,
) -> str:
    """Resolve one half's API key, or raise the one voice-unconfigured error.

    ``voice_stt`` and ``voice_tts`` are separate providers precisely so the
    two keys chain independently (ADR-0118 §1); either one missing is the
    same outcome for the caller.
    """
    try:
        credential = await resolver.resolve(provider, tenant_id=tenant_id)
    except CredentialUnavailableError as exc:
        raise _BotVoiceUnconfiguredError(_NO_VOICE_MESSAGE) from exc
    if not isinstance(credential, ProviderCredential):
        # Both halves declare a secret field and neither is optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise _BotVoiceUnconfiguredError(_NO_VOICE_MESSAGE)
    return credential.payload["api_key"]


__all__ = ["start_bot", "stop_bot"]
