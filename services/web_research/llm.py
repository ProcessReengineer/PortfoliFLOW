# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-call LLM resolution for the web research tool (ADR-0132).

The News Scraper's two LLMs — the Feed-Filter-LLM that picks candidates
out of the harvested feed items, and the Fetcher-LLM that extracts each
article — resolve their endpoint, credential and model **per tool call**,
inside the turn's tenant context, through the one credential façade
(:class:`~services.investments.credential_resolver.CredentialResolver`).
That is the same seam the chat turn resolves through per turn (ADR-0112
§4b), the Report Scraper per run (ADR-0123) and the Watch Desk beat per
tenant. Until ADR-0132 this path was the last web consumer of the
process-global ``AIServiceCore`` singleton that ``web/main.py`` parked
from ``.env``, which meant two keys that could drift apart and no
per-tenant model choice for the news path.

The chains, scope-major per ADR-0112 §1:

* credential — vault user → vault tenant → env ``OPENROUTER_API_KEY``,
  the unchanged façade;
* model      — tenant ``research_model`` → tenant ``model`` → env
  ``RESEARCH_MODEL`` → env ``SHIRLEY_MODEL`` →
  :data:`_DEFAULT_RESEARCH_MODEL`, the exact shape of the Irene and
  Scraper chains with web research's field in their place, so an
  operator who has configured nothing keeps the pre-ADR-0132 behaviour;
* base_url   — vault tenant → env ``OPENROUTER_BASE_URL`` →
  :data:`_DEFAULT_OPENROUTER_BASE_URL`.

The base-URL default is a constant here rather than
``WebSettings.openrouter_base_url``: this module sits under
``services/`` and must not import from ``web/``, and the tool it serves
runs on a daemon thread that holds no request. The value is the same
one the web settings carry.

One model serves both LLMs (ADR-0132 §3). They are two stages of one
research call, both text-only, and splitting them would double the
Admin surface for a decision nobody takes — so there is no capability
gate here either: any chat-capable model works.

Resolution is **never stashed** (ADR-0112 §4b): the
:class:`~services.ai_service_core.ResolvedLLM` this module returns lives
for exactly one tool call and is held on no module, context or service.
"""

from __future__ import annotations

import logging
from uuid import UUID

from services.ai_service_core import ResolvedLLM
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)

logger = logging.getLogger(__name__)

#: Fallback research model when no scope sets one — the same built-in
#: default the Irene tick and the Report Scraper carry. Both news LLMs are
#: text-only, so any chat-capable model serves and there is no capability
#: gate to fail behind this default.
_DEFAULT_RESEARCH_MODEL = "anthropic/claude-sonnet-4.5"

#: The endpoint every scope's chain ends at. Duplicated from
#: ``WebSettings.openrouter_base_url`` rather than imported, because
#: ``services/`` must not import from ``web/`` — see the module docstring.
_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: One operator-facing message for every "this call has no LLM" outcome.
#: Points at both places it can be fixed, and deliberately says nothing
#: about restarting: a tenant or user row applies on the next query.
NO_RESEARCH_LLM_MESSAGE = (
    "Web research is unavailable: no API credential resolved for this tenant. "
    "Set an OpenRouter API key in Admin → Providers & Credentials, and a Web "
    "research model there if you want one other than the Shirley model (both "
    "apply on your next query), or set OPENROUTER_API_KEY and RESEARCH_MODEL "
    "in .env for the whole application."
)


class ResearchLLMUnavailableError(Exception):
    """This call's OpenRouter credential resolved to nothing.

    The web research twin of ``web.routes.scraper._ScraperUnconfiguredError``.
    The tool wrapper turns it into an in-band envelope body — web research is
    a ``READ_EXTERNAL_UNTRUSTED`` tool and never raises at the model.

    Only the *credential* half raises it: the model chain always terminates in
    :data:`_DEFAULT_RESEARCH_MODEL`, so "no model" is not an outcome here.

    A :class:`~services.credential_vault.VaultDecryptError` is deliberately
    **not** wrapped: a vault that will not decrypt is an operator emergency,
    not a "configure me" nudge, and it must not read as a missing key.
    """


async def resolve_research_model(resolver: CredentialResolver) -> str:
    """Walk the model chain alone — no credential, no vault secret read.

    Split out so the model question can be answered without a credential:
    which model the next query would use is a legitimate thing to ask of a
    tenant that has configured no key yet, and answering it must not depend
    on one. Mirrors ``_resolve_scraper_model_through`` (ADR-0123).

    Args:
        resolver: The façade, with or without a bound vault session.

    Returns:
        The resolved model id; :data:`_DEFAULT_RESEARCH_MODEL` when no scope
        sets one.
    """
    return (
        await resolver.resolve_config("openrouter", "research_model", scopes=("tenant",))
        or await resolver.resolve_config("openrouter", "model", scopes=("tenant",))
        or await resolver.resolve_config("openrouter", "research_model", scopes=("env",))
        or await resolver.resolve_config("openrouter", "model", scopes=("env",))
        or _DEFAULT_RESEARCH_MODEL
    )


async def resolve_research_llm(
    resolver: CredentialResolver,
    *,
    tenant_id: UUID | None,
    user_id: UUID | None,
) -> ResolvedLLM:
    """Resolve this call's endpoint, credential and model (ADR-0132).

    Called inside the turn's ``tenant_context`` so the vault sources see
    exactly the rows RLS allows that tenant, with the user axis carried for
    the user-scope credential rows. Called with an unbound resolver — and
    both ids ``None`` — when there is no database to read a vault from, in
    which case the environment is the only source.

    Args:
        resolver: The façade, bound to the tenant-scoped session when one
            exists.
        tenant_id: The tenant the call is for; threaded for the resolver's
            log line (the vault sources read it from the session's context).
        user_id: The turn's authenticated user, or ``None`` when no person
            is bound. Without it the user-scope source does not fire.

    Returns:
        The :class:`~services.ai_service_core.ResolvedLLM` for this call.
        It is returned, never stored — see the module docstring.

    Raises:
        ResearchLLMUnavailableError: If no scope holds a credential.
        VaultDecryptError: Propagated untouched — a wrong or rotated master
            key must never look like an absent credential.
    """
    try:
        credential = await resolver.resolve("openrouter", tenant_id=tenant_id, user_id=user_id)
    except CredentialUnavailableError as exc:
        raise ResearchLLMUnavailableError(NO_RESEARCH_LLM_MESSAGE) from exc
    if not isinstance(credential, ProviderCredential):
        # openrouter declares a secret field and is not optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise ResearchLLMUnavailableError(NO_RESEARCH_LLM_MESSAGE)

    model = await resolve_research_model(resolver)
    base_url = (
        await resolver.resolve_config("openrouter", "base_url") or _DEFAULT_OPENROUTER_BASE_URL
    )
    return ResolvedLLM(base_url=base_url, api_key=credential.payload["api_key"], model=model)
