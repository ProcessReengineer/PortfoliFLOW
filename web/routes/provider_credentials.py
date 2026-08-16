# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Providers & Credentials surface under ``/admin#providers-credentials``.

The ADR-0112 §6 write surface for ``scoped_settings``: the one place an
operator sets a provider's credential or configuration without touching
``.env`` and restarting. It replaces the ADR-0052 AI-Settings form, which
mutated the running :class:`AIServiceCore` singleton and persisted
nothing — a surface that only ever made sense "until per-user settings
land". They have landed, so it is retired rather than deprecated.

Two panels, two authorisation gates:

* **Tenant panel** — ``Depends(require_role("owner"))``. Every field the
  taxonomy declares at ``tenant`` scope, across all declared providers.
* **User panel** — any authenticated role, self-service. Only user-scope
  **non-secret** fields, minus :data:`_USER_PANEL_EXCLUDED`, plus the
  Telegram **pairing block** (ADR-0112 §5), which is a flow rather than a
  field. One exclusion carries the ADR-0112 §7 rationale: no user-scope
  UI for the OpenRouter *key* in v1 (the model carries it). The other is
  ``telegram.chat_id``, and F5 **keeps** it excluded — the row is the
  pairing flow's output, so a text input would let a user claim a chat
  they cannot prove they own. (The F3 comment here predicted F5 would
  shrink the set; it grew a flow instead.)

Four rules this module exists to keep:

* **The taxonomy drives the form, and re-validates every write.** The
  rendered fields come from :data:`~services.credential_vault.PROVIDER_TAXONOMY`
  and so does the check on the way back in (:func:`_validated_field`): a
  write naming an undeclared provider, an undeclared key, or a scope the
  field does not declare is rejected with an inline error and **never
  reaches the repository**. ``is_secret`` is not a form field at all — it
  is read from the declaration, so it cannot disagree with it.
* **Secrets are write-only.** A stored value is never rendered, never
  logged, never echoed in an error. The display carries set/unset
  status, the ``secret_hint`` and the enabled flag, nothing else. The
  hint is derived here at write time as the last four characters, and
  only when the value is at least eight long — never most of a short
  secret. Encryption happens here too, via
  :meth:`~services.credential_vault.VaultCipher.from_env`; the
  repository is value-opaque by design (ADR-0112 §2).
* **An unconfigured vault degrades visibly.** With no master key the
  section renders a warning banner, disables the secret inputs, and
  rejects a secret write that arrives anyway — while config writes keep
  working. There is no silent plaintext mode.
* **Consumer honesty.** :data:`_CONSUMER_STATUS` renders a pill per
  provider saying whether anything reads the rows yet. All three are live
  as of F5: OpenFIGI (the F2 resolver), OpenRouter (F4 — resolved per
  chat turn, per Irene beat and per bot turn, applying without a restart)
  and Telegram (F5 — one bot per stored token). The Telegram pill carries
  the one caveat the others do not: a token change applies at the **next
  bot restart**, because the dispatcher set is discovered once at start.
  The three voice cards are live as of #059 V3/V4 — the configuration is
  resolved per web turn and per Telegram voice message, so a save applies
  without a restart and carries no caveat at all (ADR-0118 §8). A row an
  operator writes must never *look* consumed when it is not — nor look
  live when it is only stored.

``scoped_settings`` carries no audit trigger (the F1 decision), so each
successful mutation emits one structured log line naming scope,
provider, key, action and actor — and never a value or a hint. The
pairing endpoints follow the same rule and additionally never log the
**code**: it is a bearer token for five minutes.

Endpoints:

* ``GET  /admin/providers-credentials/section`` — the lazy section body.
* ``GET  /admin/providers-credentials/openrouter/models`` — the live model
  catalog, as a ``<datalist>`` for one model field.
* ``POST /admin/providers-credentials/tenant`` — owner-gated tenant write.
* ``POST /admin/providers-credentials/user`` — self-service user write.
* ``POST /admin/providers-credentials/telegram/pair`` — issue a pairing
  code for the caller (ADR-0112 §5).
* ``POST /admin/providers-credentials/telegram/unpair`` — delete the
  caller's chat binding.

Every POST re-renders the section body for an ``hx-swap="outerHTML"``
swap, and all of them go through the same :func:`_render_section` helper
as the GET, so state and banners come from one render path. A rejected
write answers 400 with that same body, the house idiom for an HTMX form
rejection (see ``web/routes/data_import.py``).

The model-catalog endpoint is the one read that leaves the machine. It is
a **fragment** endpoint rather than a form one, so it answers 200 in every
outcome — including refusal and fetch failure — and puts the reason in the
body (the idiom ``web/routes/statistics.py`` uses for its FX errors): an
error status would leave the button standing where the message belongs.
The key it fetches with is resolved server-side through the one credential
façade and never reaches the browser, and the list it returns is an
*offer*, never a constraint — the field stays free text.
"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncEngine

from core.repositories._session import tenant_context
from core.repositories.scoped_setting_repository import (
    ScopedSettingDTO,
    ScopedSettingRepository,
)
from core.repositories.user_repository import UserDTO
from services import telegram_pairing
from services.auth.session import SessionDTO
from services.credential_vault import (
    PROVIDER_TAXONOMY,
    ProviderField,
    VaultCipher,
    declaration_for,
    is_vault_configured,
)
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
from services.market_data.provider import UnsupportedCapabilityError
from services.openrouter_catalog import CatalogFetchError, CatalogModel, fetch_models
from services.telegram_pairing import IssuedCode
from web.auth import require_session, verify_csrf
from web.errors import user_safe_error
from web.permissions import get_authenticated_user, require_role

logger = logging.getLogger(__name__)
router = APIRouter()

_SECTION_TEMPLATE = "_partials/provider_credentials_section.html"
_MODEL_DATALIST_TEMPLATE = "_partials/openrouter_model_datalist.html"

_TENANT_ENDPOINT = "/admin/providers-credentials/tenant"
_USER_ENDPOINT = "/admin/providers-credentials/user"
_PAIR_ENDPOINT = "/admin/providers-credentials/telegram/pair"
_UNPAIR_ENDPOINT = "/admin/providers-credentials/telegram/unpair"
_MODELS_ENDPOINT = "/admin/providers-credentials/openrouter/models"

#: The OpenRouter fields the model catalog serves. All three are declared,
#: plain config fields; the taxonomy gate below still decides whether the
#: *scope* may carry the one that was asked for (``irene_model`` and
#: ``scraper_model`` are tenant-only).
_MODEL_FIELD_KEYS: frozenset[str] = frozenset({"model", "scraper_model", "irene_model"})

#: Actions each panel accepts. The user panel deliberately carries no
#: enable/disable: a user's own row is either set or absent, and a third
#: state buys nothing until a user-scope credential exists to suspend.
_TENANT_ACTIONS: frozenset[str] = frozenset({"save", "delete", "enable", "disable"})
_USER_ACTIONS: frozenset[str] = frozenset({"save", "delete"})

#: User-scope fields the panel does **not** offer as a free-text input,
#: with the reason. The panel is otherwise derived from the taxonomy.
#:
#: ``telegram.chat_id`` is the pairing flow's output (ADR-0112 §5): the
#: user proves possession of a chat by redeeming a code *in* it, and the
#: bot writes the binding. Typing a chat id by hand would bypass that
#: proof, so the entry stays — F5 gave the field a **flow** in the panel
#: (:func:`_pairing_view`), not an input.
_USER_PANEL_EXCLUDED: frozenset[tuple[str, str]] = frozenset({("telegram", "chat_id")})

#: Whether anything reads a provider's rows *yet*. Rendered as a pill on
#: every provider card. All three consumers have landed; the Telegram
#: entry keeps the restart caveat, because the dispatcher set is
#: discovered once at bot start (ADR-0112 §5, D2) and a token written
#: here is therefore not live the way an OpenRouter key is.
_CONSUMER_STATUS: dict[str, str] = {
    "openfigi": "live — saves apply instantly",
    "openrouter": "live — saves apply instantly",
    "telegram": "live — token changes apply after a bot restart",
    "voice": "live — saves apply instantly",
    "voice_stt": "live — saves apply instantly",
    "voice_tts": "live — saves apply instantly",
}

#: One-line purpose per provider, rendered under the card title. Presentational
#: only; public naming rules apply ("Watch Desk", never the internal agent name).
_PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "openfigi": "Identifier resolution for market data (ISIN and ticker lookup).",
    "openrouter": (
        "The LLM provider behind Shirley, the Report Scraper and the Watch Desk monitoring notes."
    ),
    "telegram": "The tenant's Telegram bot — Shirley in a paired chat.",
    "voice": "Voice input and spoken replies for Shirley — the on/off switch for this tenant.",
    "voice_stt": "Transcribes recorded questions, on the web chat and in Telegram voice messages.",
    "voice_tts": "The voice Shirley answers with, as browser audio and Telegram voice notes.",
}

#: Presentational only — the taxonomy key stays the wire value.
_PROVIDER_LABELS: dict[str, str] = {
    "openfigi": "OpenFIGI",
    "openrouter": "OpenRouter",
    "telegram": "Telegram",
    "voice": "Voice",
    "voice_stt": "Voice — speech-to-text",
    "voice_tts": "Voice — text-to-speech",
}

#: Presentational only. ``irene_model`` reads as "Watch Desk model" so the
#: three OpenRouter model rows are distinguishable — and because the internal
#: agent name never reaches a user-facing string (ADR-0115). The taxonomy key
#: itself is wire format and stays as declared.
_FIELD_LABELS: dict[str, str] = {
    "api_key": "API key",
    "base_url": "Base URL",
    "bot_token": "Bot token",
    "chat_id": "Chat id",
    "enabled": "Enabled",
    "irene_model": "Watch Desk model",
    "model": "Model",
    "scraper_model": "Report Scraper model",
    "voice": "Voice",
}

#: Short per-field explanation rendered under the input. Keyed by
#: (provider, key); absent means no hint line.
_FIELD_HINTS: dict[tuple[str, str], str] = {
    ("openrouter", "model"): "The model Shirley uses.",
    ("openrouter", "scraper_model"): (
        "The model that extracts figures from uploaded GP reports. Must be an "
        "Anthropic model (PDF input)."
    ),
    ("openrouter", "irene_model"): "The model that writes the Watch Desk monitoring notes.",
    ("openrouter", "base_url"): "OpenAI-compatible endpoint. Leave empty for OpenRouter's default.",
    ("openrouter", "api_key"): (
        "Used for every Shirley turn, Report Scraper run and Watch Desk beat in this tenant."
    ),
    ("openfigi", "api_key"): "Optional — without a key, lookups run keyless at a lower rate limit.",
    ("telegram", "bot_token"): "The bot token from @BotFather for this tenant's bot.",
    ("voice", "enabled"): "Turns voice on for this tenant. Applies on the next message.",
    ("voice_stt", "api_key"): "Used for every transcription in this tenant.",
    ("voice_stt", "model"): "The transcription model.",
    ("voice_stt", "base_url"): "OpenAI-compatible endpoint. Point at Groq for Groq STT.",
    ("voice_tts", "api_key"): "Used for every spoken reply in this tenant.",
    ("voice_tts", "model"): "The speech-synthesis model.",
    ("voice_tts", "voice"): "The voice Shirley speaks with.",
}

#: Panel-specific label overrides, where the same field reads differently
#: depending on whose row it is.
_USER_FIELD_LABELS: dict[tuple[str, str], str] = {
    ("openrouter", "model"): "My model",
}

_VAULT_UNCONFIGURED_MESSAGE = (
    "The credential vault is not configured — CREDENTIAL_VAULT_MASTER_KEY "
    "is unset, so secret values cannot be stored. Configuration fields are "
    "unaffected. See docs/deploy/credential-vault.md."
)


class _WriteRejected(Exception):
    """A write refused before it reached the repository.

    Carries a message written *here* and therefore safe to render inline
    — it names a provider, a key and a scope, never a value.
    """


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _engine(request: Request) -> AsyncEngine:
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database engine is not configured.",
        )
    return cast(AsyncEngine, engine)


def _provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider)


def _field_label(provider: str, key: str, *, scope: str) -> str:
    override = _USER_FIELD_LABELS.get((provider, key)) if scope == "user" else None
    return override or _FIELD_LABELS.get(key, key.replace("_", " "))


# ---------------------------------------------------------------------------
# Taxonomy validation — the gate every write passes before the repository
# ---------------------------------------------------------------------------


def _validated_field(provider: str, key: str, *, scope: str) -> ProviderField:
    """Return the declared field for ``provider.key`` at ``scope``.

    The single taxonomy gate (ADR-0112 §3). It answers three questions in
    order — is the provider declared, does it declare this field, may the
    field be written at this scope — and refuses anything else. The
    returned declaration is also where ``is_secret`` comes from, so the
    caller never has to trust a form value for it.

    Args:
        provider: The submitted provider key.
        key: The submitted field name.
        scope: ``tenant`` or ``user``.

    Returns:
        The declared :class:`~services.credential_vault.ProviderField`.

    Raises:
        _WriteRejected: If the provider is undeclared, the field is
            undeclared, or the field does not declare ``scope``.
    """
    try:
        declaration = declaration_for(provider)
    except UnsupportedCapabilityError:
        raise _WriteRejected(f"Unknown provider {provider!r}.") from None

    field = declaration.field(key)
    if field is None:
        raise _WriteRejected(f"Provider {provider!r} declares no field {key!r}.")

    if scope not in field.scopes:
        raise _WriteRejected(
            f"{provider}.{key} may not be written at {scope} scope "
            f"(declared scopes: {', '.join(sorted(field.scopes))})."
        )
    return field


def _validated_user_field(provider: str, key: str) -> ProviderField:
    """Return the declared field, refusing anything the user panel omits.

    Adds the two panel rules on top of :func:`_validated_field`: no
    user-scope secret UI in v1 (ADR-0112 §7), and nothing in
    :data:`_USER_PANEL_EXCLUDED`.

    Args:
        provider: The submitted provider key.
        key: The submitted field name.

    Returns:
        The declared :class:`~services.credential_vault.ProviderField`.

    Raises:
        _WriteRejected: On any taxonomy failure, or when the field is a
            secret or excluded from the panel.
    """
    field = _validated_field(provider, key, scope="user")
    if field.is_secret:
        raise _WriteRejected(
            f"{provider}.{key} is a secret; user-scope secrets have no write "
            "surface in v1 (ADR-0112 §7)."
        )
    if (provider, key) in _USER_PANEL_EXCLUDED:
        raise _WriteRejected(f"{provider}.{key} is not editable here.")
    return field


def _secret_hint(value: str) -> str | None:
    """Return the last four characters — or ``None`` for a short secret.

    A hint exists to let an operator recognise which key is stored, not
    to reconstruct it. Below eight characters the last four would be most
    of the value, so there is no hint at all.

    Args:
        value: The plaintext secret about to be encrypted.

    Returns:
        The last four characters when ``value`` is at least eight long,
        otherwise ``None``.
    """
    return value[-4:] if len(value) >= 8 else None


# ---------------------------------------------------------------------------
# Write application
# ---------------------------------------------------------------------------


async def _apply_write(
    repository: ScopedSettingRepository,
    *,
    scope: str,
    user_id: UUID | None,
    field: ProviderField,
    provider: str,
    value: str,
    action: str,
) -> tuple[str, bool]:
    """Apply one validated action and return ``(message, mutated)``.

    Encryption for secret rows happens here, immediately before the
    repository call, because the repository is value-opaque (ADR-0112
    §2): plaintext never crosses that boundary.

    Args:
        repository: A repository bound to the active tenant context.
        scope: ``tenant`` or ``user``.
        user_id: The row's user, for ``scope='user'``; ``None`` otherwise.
        field: The declaration this write was validated against.
        provider: The taxonomy provider key.
        value: The submitted value, already stripped.
        action: One of ``save`` / ``delete`` / ``enable`` / ``disable``.

    Returns:
        A ``(message, mutated)`` pair. ``mutated`` is ``False`` for the
        deliberate no-op — an empty secret save, which means "leave the
        stored value alone" — so the caller can skip the audit log line.

    Raises:
        _WriteRejected: On an empty config value, a secret write with no
            vault configured, or an enable/disable/delete naming a row
            that does not exist.
    """
    key = field.name
    label = f"{_provider_label(provider)} {_field_label(provider, key, scope=scope)}"

    if action in ("enable", "disable"):
        updated = await repository.set_enabled(
            scope=scope,
            provider=provider,
            key=key,
            enabled=action == "enable",
            user_id=user_id,
        )
        if updated is None:
            raise _WriteRejected(f"No stored value for {provider}.{key} to {action}.")
        return f"{label} {action}d.", True

    if action == "delete":
        deleted = await repository.delete(
            scope=scope,
            provider=provider,
            key=key,
            user_id=user_id,
        )
        if not deleted:
            raise _WriteRejected(f"No stored value for {provider}.{key} to delete.")
        return f"{label} removed.", True

    # action == "save"
    if field.is_secret:
        if not value:
            # Mirrors the retired form's empty-key semantics: an empty
            # secret field means "keep what is stored", not "erase it".
            # Delete is how an operator unsets a secret.
            return f"{label} left unchanged.", False
        if not is_vault_configured():
            raise _WriteRejected(_VAULT_UNCONFIGURED_MESSAGE)
        cipher = VaultCipher.from_env()
        await repository.upsert(
            scope=scope,
            provider=provider,
            key=key,
            is_secret=True,
            value_ciphertext=cipher.encrypt(value),
            secret_hint=_secret_hint(value),
            user_id=user_id,
        )
        return f"{label} saved.", True

    if not value:
        raise _WriteRejected(
            f"{provider}.{key} needs a value. Use Remove to unset it.",
        )
    await repository.upsert(
        scope=scope,
        provider=provider,
        key=key,
        is_secret=False,
        value_plain=value,
        user_id=user_id,
    )
    return f"{label} saved.", True


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _row_index(rows: list[ScopedSettingDTO]) -> dict[tuple[str, str], ScopedSettingDTO]:
    return {(row.provider, row.key): row for row in rows}


def _field_view(
    field: ProviderField,
    *,
    provider: str,
    scope: str,
    row: ScopedSettingDTO | None,
) -> dict[str, Any]:
    """Project one declared field plus its stored row into template data.

    A secret field contributes **no** value to the view — only whether
    one is stored, its hint and its enabled flag. A config field
    contributes ``value_plain``, which is plain by design so it stays
    greppable for support (ADR-0112 §2).
    """
    return {
        "provider": provider,
        "key": field.name,
        "label": _field_label(provider, field.name, scope=scope),
        "hint": _FIELD_HINTS.get((provider, field.name), ""),
        "is_secret": field.is_secret,
        "is_set": row is not None,
        "secret_hint": row.secret_hint if row is not None else None,
        "value": (row.value_plain or "") if (row is not None and not field.is_secret) else "",
        "enabled": row.enabled if row is not None else True,
    }


def _tenant_panel(rows: list[ScopedSettingDTO]) -> list[dict[str, Any]]:
    """Build one card per provider from the taxonomy's tenant-scope fields."""
    index = _row_index(rows)
    cards: list[dict[str, Any]] = []
    for provider, declaration in PROVIDER_TAXONOMY.items():
        fields = [
            _field_view(
                field,
                provider=provider,
                scope="tenant",
                row=index.get((provider, field.name)),
            )
            for field in declaration.fields
            if "tenant" in field.scopes
        ]
        if not fields:
            continue
        cards.append(
            {
                "provider": provider,
                "label": _provider_label(provider),
                "description": _PROVIDER_DESCRIPTIONS.get(provider, ""),
                "consumer_status": _CONSUMER_STATUS.get(provider, "not consumed yet"),
                "fields": fields,
            }
        )
    return cards


def _user_panel(rows: list[ScopedSettingDTO]) -> list[dict[str, Any]]:
    """Build the flat user-scope field list — non-secret, minus exclusions."""
    index = _row_index(rows)
    fields: list[dict[str, Any]] = []
    for provider, declaration in PROVIDER_TAXONOMY.items():
        for field in declaration.fields:
            if "user" not in field.scopes or field.is_secret:
                continue
            if (provider, field.name) in _USER_PANEL_EXCLUDED:
                continue
            view = _field_view(
                field,
                provider=provider,
                scope="user",
                row=index.get((provider, field.name)),
            )
            view["provider_label"] = _provider_label(provider)
            view["provider_description"] = _PROVIDER_DESCRIPTIONS.get(provider, "")
            fields.append(view)
    return fields


def _pairing_view(
    rows: list[ScopedSettingDTO],
    *,
    issued: IssuedCode | None,
) -> dict[str, Any]:
    """Project the caller's Telegram pairing state into template data.

    The chat binding is a user-scope row like any other, but it is written
    by the bot rather than by this panel (ADR-0112 §5), so it renders as a
    *state* with two actions instead of as an input.

    Args:
        rows: The caller's user-scope rows, as read for the panel.
        issued: A code just minted by :func:`generate_pairing_code`. Shown
            once — this render is the only place it ever appears, and it is
            never logged or stored anywhere else.

    Returns:
        Template data: paired state, the bound chat id (the user's own,
        never a secret), and the freshly issued code with its expiry.
    """
    row = _row_index(rows).get(("telegram", "chat_id"))
    return {
        "paired": row is not None,
        "chat_id": (row.value_plain or "") if row is not None else "",
        "code": issued.code if issued is not None else None,
        "expires_at": issued.expires_at if issued is not None else None,
        "ttl_minutes": int(telegram_pairing.CODE_TTL.total_seconds() // 60),
        "pair_endpoint": _PAIR_ENDPOINT,
        "unpair_endpoint": _UNPAIR_ENDPOINT,
    }


async def _render_section(
    request: Request,
    *,
    session: SessionDTO,
    user: UserDTO,
    success: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    issued_code: IssuedCode | None = None,
) -> HTMLResponse:
    """Render the section body — the single render path for GET and both POSTs.

    Reads current state inside one short ``tenant_context`` (the house
    Pattern-B idiom), so a POST's banner is always rendered against rows
    that were re-read after the write rather than against the writer's
    own idea of them.

    Args:
        request: The active request.
        session: The authenticated session, for the CSRF token and tenant.
        user: The authenticated user; ``has_role("owner")`` gates the
            tenant panel and the user's id scopes the user panel.
        success: Inline success banner text.
        error: Inline error banner text.
        status_code: 200 on success, 400 on a rejected write.
        issued_code: A pairing code just minted for this user, rendered
            once (ADR-0112 §5). ``None`` for every other render.

    Returns:
        The rendered section body for an ``outerHTML`` swap.
    """
    is_owner = user.has_role("owner")
    async with tenant_context(
        _engine(request), session.tenant_id, user_id=session.user_id
    ) as db_session:
        repository = ScopedSettingRepository(db_session)
        tenant_rows = await repository.list_for_tenant() if is_owner else []
        user_rows = await repository.list_for_user(session.user_id)

    context: dict[str, Any] = {
        "csrf_token": session.csrf_token,
        "vault_configured": is_vault_configured(),
        "is_owner": is_owner,
        "tenant_endpoint": _TENANT_ENDPOINT,
        "user_endpoint": _USER_ENDPOINT,
        "tenant_cards": _tenant_panel(tenant_rows) if is_owner else [],
        "user_fields": _user_panel(user_rows),
        "telegram_pairing": _pairing_view(user_rows, issued=issued_code),
        "success": success,
        "error": error,
    }
    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _SECTION_TEMPLATE,
            context,
            status_code=status_code,
        ),
    )


# ---------------------------------------------------------------------------
# Model catalog (the one outbound read on this surface)
# ---------------------------------------------------------------------------


def _render_model_datalist(
    request: Request,
    *,
    target: tuple[str, str] | None,
    models: list[CatalogModel] | None = None,
    message: str | None = None,
) -> HTMLResponse:
    """Render the model-catalog fragment for one field's slot.

    Always HTTP 200: the body is an ``innerHTML`` swap into the field's
    slot, and an error status would leave the button standing where the
    message belongs (the ``web/routes/statistics.py`` idiom).

    Args:
        request: The active request.
        target: The ``(scope, key)`` this fragment belongs to, from which
            the slot's three ids are derived. ``None`` for a request too
            malformed to name a slot — the fragment then carries the
            message alone, with no reload button to target nothing with.
        models: The fetched catalog. Unused in the error state.
        message: Operator-safe failure text, or ``None`` on success.

    Returns:
        The rendered fragment.
    """
    datalist_id: str | None = None
    slot_id: str | None = None
    reload_url: str | None = None
    if target is not None:
        scope, key = target
        base = f"{scope}-openrouter-{key}"
        datalist_id = f"{base}-models"
        slot_id = f"{base}-models-slot"
        reload_url = f"{_MODELS_ENDPOINT}?scope={scope}&key={key}"

    return cast(
        HTMLResponse,
        _templates(request).TemplateResponse(
            request,
            _MODEL_DATALIST_TEMPLATE,
            {
                "datalist_id": datalist_id,
                "slot_id": slot_id,
                "reload_url": reload_url,
                "models": models or [],
                "message": message,
            },
        ),
    )


async def _catalog_endpoint_through(
    resolver: CredentialResolver,
    request: Request,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
) -> tuple[str, str | None]:
    """Resolve ``(base_url, api_key)`` for the catalog fetch.

    Walks the same two chains the chat route's per-turn resolution walks
    (ADR-0112 §4b), with one deliberate difference: a **missing credential
    is not a failure here**. ``/models`` is a public list, so an
    unconfigured tenant still gets an autocomplete — which is exactly the
    tenant most likely to be typing a model id for the first time.

    Args:
        resolver: The façade, with or without a bound vault session.
        request: The active request, for the ``WebSettings`` base-URL default.
        tenant_id: The tenant, for the resolver's log line.
        user_id: The user whose rows join the chain, or ``None`` to resolve
            at tenant scope alone.

    Returns:
        The endpoint to fetch from and the bearer token to fetch with
        (``None`` for a keyless call).
    """
    api_key: str | None = None
    try:
        credential = await resolver.resolve("openrouter", tenant_id=tenant_id, user_id=user_id)
    except CredentialUnavailableError:
        # Deliberately swallowed: keyless is a valid way to call /models.
        credential = None
    if isinstance(credential, ProviderCredential):
        api_key = credential.payload["api_key"]

    base_url = (
        await resolver.resolve_config("openrouter", "base_url")
        or request.app.state.settings.openrouter_base_url
    )
    return base_url, api_key


async def _resolve_catalog_endpoint(
    request: Request,
    *,
    session: SessionDTO,
    scope: str,
) -> tuple[str, str | None]:
    """Resolve the catalog endpoint inside one short ``tenant_context``.

    Mirrors ``web/routes/chat.py``'s ``_resolve_llm``, including its
    graceful degradation: with no database engine (a DB-less rig, a
    contributor laptop) the resolver is built without a session and the
    environment is the only source.

    Args:
        request: The active request.
        session: The authenticated session, supplying tenant and user.
        scope: ``tenant`` or ``user``. The user-scope chain carries the
            caller's id, so a user sees the list their *own* key reaches;
            the tenant-scope chain deliberately does not.

    Returns:
        The ``(base_url, api_key)`` pair for :func:`fetch_models`.
    """
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        return await _catalog_endpoint_through(
            CredentialResolver(), request, tenant_id=None, user_id=None
        )
    async with tenant_context(engine, session.tenant_id, user_id=session.user_id) as db_session:
        return await _catalog_endpoint_through(
            CredentialResolver(session=db_session),
            request,
            tenant_id=session.tenant_id,
            user_id=session.user_id if scope == "user" else None,
        )


async def _handle_write(
    request: Request,
    *,
    session: SessionDTO,
    user: UserDTO,
    scope: str,
    provider: str,
    key: str,
    value: str,
    action: str,
    allowed_actions: frozenset[str],
) -> HTMLResponse:
    """Validate, apply and re-render — the body both POST handlers share.

    Args:
        request: The active request.
        session: The authenticated session.
        user: The authenticated (and role-gated) user.
        scope: ``tenant`` or ``user``.
        provider: Submitted provider key.
        key: Submitted field name.
        value: Submitted value.
        action: Submitted action.
        allowed_actions: The panel's action vocabulary.

    Returns:
        The re-rendered section body: 200 on success, 400 on rejection.
    """
    user_id = session.user_id if scope == "user" else None
    success: str | None = None
    error: str | None = None
    status_code = 200

    try:
        if action not in allowed_actions:
            raise _WriteRejected(f"Unknown action {action!r}.")
        field = (
            _validated_user_field(provider, key)
            if scope == "user"
            else _validated_field(provider, key, scope=scope)
        )
        async with tenant_context(
            _engine(request), session.tenant_id, user_id=session.user_id
        ) as db_session:
            success, mutated = await _apply_write(
                ScopedSettingRepository(db_session),
                scope=scope,
                user_id=user_id,
                field=field,
                provider=provider,
                value=value,
                action=action,
            )
        if mutated:
            # ``scoped_settings`` carries no audit trigger (F1), so this
            # line is the trail. Never a value, never a hint.
            logger.info(
                "scoped-setting mutation: scope=%s provider=%s key=%s action=%s actor=%s",
                scope,
                provider,
                key,
                action,
                user.id,
            )
    except _WriteRejected as exc:
        error = str(exc)
        status_code = 400
    except Exception as exc:  # noqa: BLE001 — surface inline, masked.
        user_msg, _error_id = user_safe_error(exc)
        error = f"Save failed: {user_msg}"
        status_code = 400

    return await _render_section(
        request,
        session=session,
        user=user,
        success=success,
        error=error,
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/admin/providers-credentials/section", response_class=HTMLResponse)
async def get_provider_credentials_section(
    request: Request,
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
) -> HTMLResponse:
    """Return the Providers & Credentials section body.

    Lazy-loaded on first visibility via ``hx-trigger="revealed"``. Every
    authenticated user gets their own panel; the tenant panel renders
    only for an owner, and a member or auditor sees a short note saying
    who manages tenant credentials instead.
    """
    return await _render_section(request, session=session, user=user)


@router.get(_MODELS_ENDPOINT, response_class=HTMLResponse)
async def get_openrouter_models(
    request: Request,
    scope: str = Query("tenant"),
    key: str = Query("model"),
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
) -> HTMLResponse:
    """Fetch the live OpenRouter model list for one model field.

    Loaded on demand — a click is a fetch, deliberately uncached — and
    rendered as a ``<datalist>`` behind the field's text input. The input
    stays free text: the list is an offer, so a custom endpoint, an
    offline laptop or a model newer than the list all keep working.

    The credential is resolved **server-side** through the one façade and
    used only to authenticate this fetch; it never reaches the browser. A
    tenant with no key configured anywhere still gets a list, because
    ``/models`` is public — the fetch simply goes keyless.

    Two gates before anything leaves the machine: the field must be one
    the taxonomy declares at the requested scope (so ``irene_model`` at
    user scope is refused, as the write path refuses it), and the tenant
    scope additionally demands the owner role, the same gate the tenant
    write endpoint carries. A refusal renders as the fragment's message
    state rather than as a status code — see the module docstring.

    Args:
        request: The active request.
        scope: ``tenant`` or ``user`` — which chain to resolve the key
            and which panel's field this is.
        key: ``model``, ``scraper_model`` or ``irene_model`` — which
            field's slot this fragment is bound for.
        session: The authenticated session.
        user: The authenticated user; ``has_role("owner")`` gates the
            tenant scope.

    Returns:
        The datalist fragment. Always HTTP 200.
    """
    scope = scope.strip()
    key = key.strip()
    unoffered = "the request named a scope or field this list is not offered for."

    if scope not in ("tenant", "user") or key not in _MODEL_FIELD_KEYS:
        return _render_model_datalist(request, target=None, message=unoffered)

    try:
        # The same taxonomy gate the write path uses — it is the one place
        # that knows a field's declared scopes, and ``irene_model`` is
        # tenant-only. Its message is written for a write, so it is not
        # rendered here; the check is what this call is for.
        _validated_field("openrouter", key, scope=scope)
    except _WriteRejected:
        return _render_model_datalist(request, target=None, message=unoffered)

    if scope == "tenant" and not user.has_role("owner"):
        return _render_model_datalist(
            request,
            target=(scope, key),
            message="tenant-wide provider settings are owner-managed.",
        )

    try:
        base_url, api_key = await _resolve_catalog_endpoint(request, session=session, scope=scope)
        models = await fetch_models(base_url, api_key)
    except CatalogFetchError as exc:
        logger.warning(
            "openrouter model catalog: fetch failed scope=%s tenant=%s: %s",
            scope,
            session.tenant_id,
            exc,
        )
        return _render_model_datalist(request, target=(scope, key), message=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface inline, masked.
        user_msg, _error_id = user_safe_error(exc)
        logger.warning(
            "openrouter model catalog: resolution failed scope=%s tenant=%s",
            scope,
            session.tenant_id,
        )
        return _render_model_datalist(request, target=(scope, key), message=user_msg)

    return _render_model_datalist(request, target=(scope, key), models=models)


@router.post(_TENANT_ENDPOINT, response_class=HTMLResponse)
async def save_tenant_setting(
    request: Request,
    provider: str = Form(""),
    key: str = Form(""),
    value: str = Form(""),
    action: str = Form("save"),
    user: UserDTO = Depends(require_role("owner")),
    session: SessionDTO = Depends(require_session),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Write one tenant-scope field.

    Owner-gated per ADR-0112 §6. Actions: ``save`` (default), ``delete``,
    ``enable``, ``disable``. An empty ``value`` on a secret ``save``
    means "leave the stored secret unchanged"; an empty value on a config
    save is a validation error, because ``delete`` is how a field is
    unset.
    """
    return await _handle_write(
        request,
        session=session,
        user=user,
        scope="tenant",
        provider=provider.strip(),
        key=key.strip(),
        value=value.strip(),
        action=action.strip() or "save",
        allowed_actions=_TENANT_ACTIONS,
    )


@router.post(_USER_ENDPOINT, response_class=HTMLResponse)
async def save_user_setting(
    request: Request,
    provider: str = Form(""),
    key: str = Form(""),
    value: str = Form(""),
    action: str = Form("save"),
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Write one of the caller's own user-scope fields.

    Open to any authenticated role — the row belongs to the caller. The
    user id comes from the **session**, never from the form, so a
    smuggled ``user_id`` parameter cannot redirect the write at someone
    else's row; the repository then filters on that same id on the way
    back out (ADR-0112 §2).
    """
    return await _handle_write(
        request,
        session=session,
        user=user,
        scope="user",
        provider=provider.strip(),
        key=key.strip(),
        value=value.strip(),
        action=action.strip() or "save",
        allowed_actions=_USER_ACTIONS,
    )


@router.post(_PAIR_ENDPOINT, response_class=HTMLResponse)
async def generate_pairing_code(
    request: Request,
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Mint a five-minute Telegram pairing code for the caller (ADR-0112 §5).

    Self-service like the user write beside it, and scoped the same way:
    the tenant and the user both come from the **session**, so a code can
    only ever bind the caller's own account, in the caller's own tenant.

    Issuing a second code invalidates the first
    (:func:`services.telegram_pairing.issue_code`), so a user who clicks
    twice never leaves a live code behind. The code is returned in this
    one render and is deliberately absent from the log line — for five
    minutes it is a bearer token for the account.
    """
    issued = telegram_pairing.issue_code(session.tenant_id, session.user_id)
    logger.info(
        "telegram pairing: code issued scope=user actor=%s valid_minutes=%d",
        user.id,
        int(telegram_pairing.CODE_TTL.total_seconds() // 60),
    )
    return await _render_section(
        request,
        session=session,
        user=user,
        success=(
            "Pairing code created. Send it to the bot from the Telegram chat you want to use."
        ),
        issued_code=issued,
    )


@router.post(_UNPAIR_ENDPOINT, response_class=HTMLResponse)
async def revoke_pairing(
    request: Request,
    session: SessionDTO = Depends(require_session),
    user: UserDTO = Depends(get_authenticated_user),
    _csrf: None = Depends(verify_csrf),
) -> HTMLResponse:
    """Delete the caller's Telegram chat binding (ADR-0112 §5).

    Deleting the row is what un-authorises the chat: the bot reads it per
    message, so the next message from that chat is dropped silently. Any
    pending code of this user is dropped with it — an outstanding code must
    not be able to re-bind the chat a moment after it was revoked.
    """
    telegram_pairing.revoke_codes_for_user(session.user_id)
    async with tenant_context(
        _engine(request), session.tenant_id, user_id=session.user_id
    ) as db_session:
        deleted = await ScopedSettingRepository(db_session).delete(
            scope="user",
            provider="telegram",
            key="chat_id",
            user_id=session.user_id,
        )
    if deleted:
        logger.info(
            "scoped-setting mutation: scope=user provider=telegram key=chat_id "
            "action=delete actor=%s",
            user.id,
        )
    return await _render_section(
        request,
        session=session,
        user=user,
        success=(
            "Telegram pairing removed. The bot no longer answers that chat." if deleted else None
        ),
        error=None if deleted else "No Telegram chat is paired with your account.",
        status_code=200 if deleted else 400,
    )
