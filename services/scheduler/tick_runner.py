# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The shared per-tick orchestration behind every tick source (ADR-0117 §2).

Two entry points, one per advisory-lock domain — :func:`run_irene_tick`
(ADR-0086) and :func:`run_market_data_tick` (ADR-0093) — each parametrised
on the RLS-bypassing engine its **caller** supplies:

- ``cli/irene_tick.py`` / ``cli/market_data_tick.py`` pass
  ``cli._db.superuser_engine()`` (and dispose of it);
- the built-in in-process scheduler passes the web app's audit engine.

Neither host may drift from the other, so nothing here is host-specific:
the runner never constructs an engine, never reads ``.env``, never maps an
exit code. Engine *lifecycle* belongs to the caller — the CLI's engine is
per-run and disposed in a ``finally``, the web app's is process-lived and
must not be disposed by a tick.

Irene tick — flow (ADR-0086, ADR-0112 §4b):

1. Decide whether anything could possibly resolve an LLM credential
   (:func:`irene_credentials_reachable`). Since ADR-0112 §4b credentials
   are **per tenant**, so the old global "no ``OPENROUTER_API_KEY`` ⇒
   nothing to do" shortcut is only sound when the credential vault is
   *also* unconfigured: with a vault, a tenant may hold its own key even
   though the environment holds none. Only when neither source exists does
   the tick warn and return without beating — a **no-op, not a failure**
   (matching ``web/main.py``'s tolerant stance).
2. On the supplied (RLS-bypassing) engine, run the cross-tenant due read
   (``find_due_tenants``, RLS bypassed by design).
3. For each due tenant, open a tenant-scoped ``tenant_context`` (which
   drops to the unprivileged app role so RLS is enforced), claim the
   tenant's beat with ``pg_try_advisory_xact_lock`` as the **first**
   statement of that transaction, resolve *that tenant's* credential and
   model through the credential façade, and — if claimed and resolved —
   run the beat and advance the schedule in the **same** transaction. A
   held lock (another tick already beating this tenant) means skip, not
   block; a tenant with no resolvable credential is skipped with a log
   line and counted, exactly like any other per-tenant failure isolation.
4. A single tenant's failure is caught and logged; the tick continues and
   returns normally unless the *tick itself* failed.

Model choice: Irene must not be pinned to Shirley's model, and since
ADR-0112 §4b that preference is expressed per tenant rather than per
process. The chain is scope-major — the tenant's own rows before the
environment — and inside each scope Irene's own field wins over Shirley's:

    tenant ``irene_model`` → tenant ``model`` → env ``IRENE_MODEL``
    → env ``SHIRLEY_MODEL`` → ``_DEFAULT_IRENE_MODEL``

so an operator who sets nothing gets exactly the pre-F4 behaviour, and a
tenant that sets ``irene_model`` overrides both environment variables. The
*embedding* model is a separate, later decision — do not conflate it with
this synthesis-model choice.

Market-data tick — flow (ADR-0093, reusing Irene's topology 1:1):

1. On the supplied engine, run the cross-tenant due read
   (:func:`services.investments.live_schedule.find_due_tenants`, RLS
   bypassed by design) — every tenant with ``enabled AND next_due_at <=
   now()``. With ``tenant_ref`` the tick instead targets that single
   tenant, bypassing the due gate (like the web "Refresh now").
2. For each tenant, open a tenant-scoped ``tenant_context`` (dropping to
   the unprivileged app role so RLS is enforced), claim the tenant's
   refresh with ``pg_try_advisory_xact_lock`` on the **market_data-domain**
   key (:func:`advisory_lock_key` with ``domain="market_data"``, disjoint
   from Irene's key) as the first statement of that transaction, and — if
   claimed — run the refresh core and advance the schedule in the **same**
   transaction. A held lock means skip, not block.
3. A single tenant's failure is caught and logged; the tick continues and
   returns normally unless the *tick itself* failed.

The test-seam parameters ``tenant_ref`` / ``provider`` (ADR-0093 §0.4)
carry the CLI's ``--tenant`` / ``--provider`` flags; **neither persists
schedule state**, so a test run never perturbs production cadence. Flag
*parsing* stays in the CLI — the runner only receives the values.

Layering: ``services/`` imports only from ``core/`` and ``services/``. The
Irene tick's two settings reads therefore arrive as a parameter
(:class:`IreneTickSettings`) rather than through a ``web.settings`` import.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from core.repositories._session import tenant_context
from core.repositories.irene_schedule_repository import IreneScheduleRepository
from core.repositories.market_data_schedule_repository import (
    MarketDataScheduleRepository,
)
from services.ai_service_core import ResolvedLLM, get_ai_service_core
from services.credential_vault import is_vault_configured
from services.investments.credential_resolver import (
    CredentialResolver,
    CredentialUnavailableError,
    ProviderCredential,
)
from services.investments.live_refresh import refresh_tenant_live_data
from services.investments.live_schedule import DueMarketDataTenant
from services.investments.live_schedule import (
    find_due_tenants as find_due_market_data_tenants,
)
from services.irene.beat import run_beat
from services.irene.embedding import OpenRouterEmbedder
from services.irene.scheduling import advisory_lock_key, compute_next_due_at
from services.irene.scheduling import find_due_tenants as find_due_irene_tenants

# The tick's log lines are an operational contract (ADR-0117 §Compliance:
# "the same structured log lines per tick and per tenant beat in both
# hosts"), and a logger name is part of a log line — so this name is a
# deliberate choice, not an incidental one. The S2 extraction kept the CLI
# namespace verbatim because the CLI was then the only host; with the
# in-process scheduler live (ADR-0117 §3) the same lines are emitted from
# the web process too, where a CLI name was actively misleading. The one
# name both hosts now share names the concern instead of one of its hosts.
_LOG = logging.getLogger("portfoliflow.scheduler")

# Fallback synthesis model when no scope sets one. Kept in step with the
# models the repo's fixtures exercise; an operator overrides it per tenant
# in Providers & Credentials, or per deployment via IRENE_MODEL /
# SHIRLEY_MODEL in .env.
_DEFAULT_IRENE_MODEL = "anthropic/claude-sonnet-4.5"

# The advisory-lock domain for market-data refreshes. Salts the tenant key
# so a refresh claim never collides with an Irene beat's lock (ADR-0093 §0.2).
_LOCK_DOMAIN = "market_data"


class IreneTickSettings(Protocol):
    """The deployment settings one Irene tick reads — structurally typed.

    ``services/`` must not import from ``web/`` (CLAUDE.md § Dependency
    rules), so the settings object travels as an argument instead of being
    fetched here. ``web.settings.WebSettings`` satisfies this protocol
    structurally; so does any test double with the two attributes.

    Attributes:
        openrouter_api_key: The application-scope key, or ``None``. Read
            only by :func:`irene_credentials_reachable` — per-tenant
            resolution goes through the credential façade.
        openrouter_base_url: The deployment's default endpoint, used when
            no scope configures one.
    """

    openrouter_api_key: str | None
    openrouter_base_url: str


@dataclass(frozen=True)
class IreneTickSummary:
    """What one Irene tick did, for the caller's logging and health surface.

    Deliberately counters only: today's CLI exit codes are exception-driven
    ("nothing due", "no credential anywhere" and "every due tenant lacks
    one" all exit 0), so no field of this summary decides an exit code. It
    exists so a host can report a tick's outcome without re-deriving it.

    Attributes:
        due: Tenants the due read returned.
        beaten: Tenants whose beat succeeded and whose schedule advanced.
        skipped: Tenants whose advisory-lock claim was already held.
        no_key_skipped: Tenants skipped for want of a resolvable credential.
        errors: Tenants whose beat reported an error or raised.
        findings_written: Findings persisted across all beats this tick.
    """

    due: int = 0
    beaten: int = 0
    skipped: int = 0
    no_key_skipped: int = 0
    errors: int = 0
    findings_written: int = 0


@dataclass(frozen=True)
class MarketDataTickSummary:
    """What one market-data tick did (see :class:`IreneTickSummary`).

    Attributes:
        due: Tenants the due read (or the ``tenant_ref`` resolution)
            returned.
        refreshed: Tenants whose refresh completed.
        skipped: Tenants whose advisory-lock claim was already held.
        errors: Tenants whose refresh raised.
    """

    due: int = 0
    refreshed: int = 0
    skipped: int = 0
    errors: int = 0


def _harvest_rss_items() -> list:
    """Harvest this tick's RSS feed items — once, for every tenant.

    Tenant-blind: the allowlist and its feeds are global config, so the
    harvest runs once per tick and the same items are clustered per-tenant
    (each tenant's freeze / edge-gate state is its own). Tolerant, in the
    tick's spirit: any failure (allowlist load, feed fetch, network)
    degrades to *no RSS* — an internal-only beat — rather than a tick error.

    Synchronous by nature (``feedparser`` and friends block), so
    :func:`run_irene_tick` calls it through :func:`asyncio.to_thread`: in
    the web host it would otherwise stall the uvicorn event loop
    (ADR-0117 §2).

    The *embedder* is deliberately not built here: since ADR-0112 §4b its
    credential is the tenant's, so one is constructed per tenant in
    :func:`run_irene_tick` from that tenant's resolution. The harvest is
    the only genuinely tenant-blind half.

    Returns:
        The harvested items, possibly empty (an empty list makes every
        beat internal-only).
    """
    try:
        from services.web_research.service import WebResearchService

        return list(WebResearchService().harvest_items())
    except Exception as exc:  # noqa: BLE001 — RSS is best-effort on the tick
        _LOG.warning(
            "irene-tick: RSS harvest unavailable (%s) — beating internal-only.",
            exc,
        )
        return []


async def _resolve_tenant_llm(
    resolver: CredentialResolver,
    settings: IreneTickSettings,
    tenant_id: UUID,
) -> ResolvedLLM:
    """Resolve one tenant's endpoint, credential and model (ADR-0112 §4b).

    Called inside that tenant's ``tenant_context``, so the vault sources see
    exactly its rows. There is no user axis: a beat runs for a *tenant*, not
    a person, so the user-scope rows are never consulted.

    The model chain is scope-major with Irene's field winning inside each
    scope (see the module docstring); ``base_url`` follows the same shape and
    falls back to the deployment default, so it can never fail a beat.

    Args:
        resolver: The façade, bound to the tenant-scoped session.
        settings: The deployment settings, for the ``base_url`` default.
        tenant_id: The tenant being beaten — threaded for the log line.

    Returns:
        The tenant's :class:`~services.ai_service_core.ResolvedLLM`.

    Raises:
        CredentialUnavailableError: If no scope holds a credential for this
            tenant. The caller skips that tenant and continues.
    """
    credential = await resolver.resolve("openrouter", tenant_id=tenant_id)
    if not isinstance(credential, ProviderCredential):
        # openrouter declares a secret field and is not optional, so the
        # resolver raises rather than returning NoCredential. Defensive.
        raise CredentialUnavailableError(
            f"No OpenRouter credential resolved for tenant {tenant_id}."
        )

    model = (
        await resolver.resolve_config("openrouter", "irene_model", scopes=("tenant",))
        or await resolver.resolve_config("openrouter", "model", scopes=("tenant",))
        or await resolver.resolve_config("openrouter", "irene_model", scopes=("env",))
        or await resolver.resolve_config("openrouter", "model", scopes=("env",))
        or _DEFAULT_IRENE_MODEL
    )
    base_url = (
        await resolver.resolve_config("openrouter", "base_url") or settings.openrouter_base_url
    )
    return ResolvedLLM(base_url=base_url, api_key=credential.payload["api_key"], model=model)


def irene_credentials_reachable(settings: IreneTickSettings) -> bool:
    """Report whether *any* scope could resolve an LLM credential.

    The tick's opening gate, kept public because a host may hold a resource
    whose construction must stay behind it: the CLI's superuser engine
    demands ``DATABASE_URL_SUPERUSER``, and a deployment that can resolve no
    credential at all has always exited 0 *without* consulting that
    variable. A host that skips this pre-check loses nothing — the gate is
    the first thing :func:`run_irene_tick` evaluates — but it would build
    (and fail on) resources the no-op never needed.

    Logs the operator-facing warning itself when the answer is ``False``, so
    the reason is reported exactly once wherever the gate is evaluated.

    Args:
        settings: The deployment settings.

    Returns:
        ``True`` when a credential vault is configured (a tenant may hold
        its own key) or the environment holds one; ``False`` when neither
        source exists.
    """
    if not is_vault_configured() and not settings.openrouter_api_key:
        # Nothing *can* resolve: no vault to hold a tenant's own key, and no
        # environment key to fall back on. A beat with no LLM is a no-op,
        # not a failure (cf. web/main.py). With a vault configured the tick
        # runs and per-tenant resolution decides, tenant by tenant.
        _LOG.warning(
            "irene-tick: no credential vault and OPENROUTER_API_KEY not set "
            "— no scope can resolve an LLM credential; nothing to beat. "
            "Exiting 0."
        )
        return False
    return True


async def run_irene_tick(
    engine: AsyncEngine,
    *,
    settings: IreneTickSettings,
    now: datetime | None = None,
) -> IreneTickSummary:
    """Run one Irene tick: beat every due tenant, each in its own transaction.

    Args:
        engine: The RLS-bypassing engine to run the cross-tenant due read
            and each tenant's ``tenant_context`` on. Supplied — and
            disposed of, where that applies — by the caller.
        settings: The deployment settings (see :class:`IreneTickSettings`).
        now: The tick's clock, defaulting to "now" in UTC. One Python clock
            for the whole tick's scheduling arithmetic; the due *read* uses
            the DB clock (``now()`` inline in ``find_due_tenants``) so all
            tenants are compared against one instant, and the ~ms skew
            between that and this clock is immaterial to a daily cadence.

    Returns:
        The tick's :class:`IreneTickSummary`. A tenant-level failure is
        counted, never raised; an infrastructure-level failure (the due
        read, a vault decryption error, …) propagates to the caller.
    """
    if not irene_credentials_reachable(settings):
        return IreneTickSummary()

    # The core supplies the system prompt and the synthesis entry point. It
    # is deliberately *not* configured here: the credential and model are
    # per tenant now (ADR-0112 §4b) and travel as a ResolvedLLM per beat, so
    # there is no process-global endpoint state for one tenant's beat to
    # leak into another's.
    ai_core = get_ai_service_core()

    now = now if now is not None else datetime.now(timezone.utc)

    # Harvest RSS once per tick (global allowlist); the same items are
    # clustered per tenant, each through its own embedder. Best-effort — a
    # harvest failure means an internal-only beat, not a tick failure. Run
    # off the event loop: the harvest is blocking I/O (ADR-0117 §2).
    rss_items = await asyncio.to_thread(_harvest_rss_items)
    _LOG.info(
        "irene-tick: %d RSS item(s) harvested for clustering.",
        len(rss_items),
    )

    due_count = beaten = skipped = errors = findings_total = no_key_skipped = 0
    async with engine.connect() as conn:
        due_tenants = await find_due_irene_tenants(conn)
    due_count = len(due_tenants)
    _LOG.info("irene-tick: %d tenant(s) due.", due_count)

    for due in due_tenants:
        key = advisory_lock_key(due.tenant_id)
        try:
            # One transaction per tenant: the advisory-xact-lock claim,
            # the beat's finding writes, and mark_beat_done all share
            # this single `session` and its transaction. The lock is
            # released automatically at COMMIT/ROLLBACK — no leak if the
            # beat raises. tenant_context has already dropped to the
            # unprivileged app role by the time it yields, so the
            # advisory-lock call below runs as the app role, inside the
            # same transaction the beat writes on (ADR-0078, ADR-0086).
            async with tenant_context(engine, due.tenant_id) as session:
                claimed = (
                    await session.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"),
                        {"k": key},
                    )
                ).scalar_one()
                if not claimed:
                    _LOG.info(
                        "irene-tick: tenant %s already claimed by another tick, skipping.",
                        due.tenant_id,
                    )
                    skipped += 1
                    continue

                # This tenant's own credential and model (ADR-0112 §4b),
                # read through the façade on this very session — so the
                # vault rows RLS shows are exactly this tenant's. A
                # tenant that has none is skipped with a log line, the
                # same per-tenant isolation a failed beat gets: one
                # unconfigured tenant must not stop the others.
                resolver = CredentialResolver(session=session)
                try:
                    llm = await _resolve_tenant_llm(resolver, settings, due.tenant_id)
                except CredentialUnavailableError as exc:
                    _LOG.warning(
                        "irene-tick: tenant %s has no resolvable LLM "
                        "credential (%s) — skipping this beat.",
                        due.tenant_id,
                        exc.message,
                    )
                    no_key_skipped += 1
                    continue

                # One embedder per tenant, built from that tenant's
                # resolution: a tenant's key vectorises only its own
                # beat. The memo therefore spans one beat rather than
                # the whole tick — the deliberate cost of the isolation.
                embedder = OpenRouterEmbedder(llm.make_client) if rss_items else None

                result = await run_beat(
                    session,
                    ai_core,
                    llm=llm,
                    now=now,
                    rss_items=rss_items,
                    embedder=embedder,
                )
                if result.error is not None:
                    # Do not advance the schedule on error — leave
                    # next_due_at in the past so the next tick retries.
                    _LOG.error(
                        "irene-tick: tenant %s beat error: %s",
                        due.tenant_id,
                        result.error,
                    )
                    errors += 1
                    continue

                next_due = compute_next_due_at(now, due.cadence, due.preferred_hour, due.timezone)
                await IreneScheduleRepository(session).mark_beat_done(
                    schedule_id=due.schedule_id,
                    last_beat_at=now,
                    next_due_at=next_due,
                )
                beaten += 1
                findings_total += result.findings_written
                _LOG.info(
                    "irene-tick: tenant %s beaten (findings=%d, next_due=%s).",
                    due.tenant_id,
                    result.findings_written,
                    next_due.isoformat(),
                )
        except Exception as exc:  # noqa: BLE001 — isolate one tenant's failure
            errors += 1
            _LOG.exception("irene-tick: tenant %s failed: %s", due.tenant_id, exc)

    _LOG.info(
        "irene-tick: %d due, %d beaten, %d skipped, %d no_key_skipped, "
        "%d errors (findings written=%d).",
        due_count,
        beaten,
        skipped,
        no_key_skipped,
        errors,
        findings_total,
    )
    if due_count and no_key_skipped == due_count:
        # Every due tenant was skipped for want of a credential. Exit stays
        # 0 — this is a configuration gap, not a tick failure — but it must
        # not be silent, or a deployment that resolves nothing looks like a
        # deployment with nothing to do.
        _LOG.warning(
            "irene-tick: all %d due tenant(s) were skipped — none resolved "
            "an LLM credential. Set an OpenRouter API key per tenant in "
            "Admin → Providers & Credentials, or OPENROUTER_API_KEY in .env.",
            due_count,
        )

    return IreneTickSummary(
        due=due_count,
        beaten=beaten,
        skipped=skipped,
        no_key_skipped=no_key_skipped,
        errors=errors,
        findings_written=findings_total,
    )


async def _resolve_single_tenant(
    conn: AsyncConnection, tenant_ref: str
) -> list[DueMarketDataTenant]:
    """Resolve one ``tenant_ref`` to a due-tenant list (due gate off).

    ``tenant_ref`` is a tenant UUID or a subdomain. The tenant's schedule row
    (if any) supplies ``schedule_id`` / cadence / ``last_run_at``; a tenant
    without a schedule row still refreshes (``last_run_at=None``), it simply
    has nothing to advance (the ``tenant_ref`` path never persists schedule
    state anyway). Returns an empty list when the reference resolves to no
    tenant, so the tick logs "0 due" and exits 0.
    """
    try:
        tenant_id = UUID(tenant_ref)
    except ValueError:
        row = (
            await conn.execute(
                text("SELECT id FROM tenants WHERE subdomain = :sd"),
                {"sd": tenant_ref.strip().lower()},
            )
        ).first()
        if row is None:
            _LOG.warning(
                "market-data-tick: no tenant with id-or-subdomain %r.",
                tenant_ref,
            )
            return []
        tenant_id = UUID(str(row.id))

    sched = (
        await conn.execute(
            text(
                "SELECT id AS schedule_id, cadence, timezone, preferred_hour, "
                "last_run_at FROM market_data_schedule "
                "WHERE tenant_id = :tid AND user_id IS NULL"
            ),
            {"tid": str(tenant_id)},
        )
    ).first()
    if sched is None:
        _LOG.info(
            "market-data-tick: tenant %s has no schedule row; refreshing "
            "with a default lookback (nothing to advance).",
            tenant_id,
        )
        return [
            DueMarketDataTenant(
                tenant_id=tenant_id,
                schedule_id=None,
                cadence="daily",
                timezone="UTC",
                preferred_hour=None,
                last_run_at=None,
            )
        ]
    return [
        DueMarketDataTenant(
            tenant_id=tenant_id,
            schedule_id=sched.schedule_id,
            cadence=sched.cadence,
            timezone=sched.timezone,
            preferred_hour=sched.preferred_hour,
            last_run_at=sched.last_run_at,
        )
    ]


async def run_market_data_tick(
    engine: AsyncEngine,
    *,
    tenant_ref: str | None = None,
    provider: str | None = None,
    now: datetime | None = None,
) -> MarketDataTickSummary:
    """Run one market-data tick: refresh every due tenant, each in its own
    transaction.

    Args:
        engine: The RLS-bypassing engine to run the cross-tenant due read
            and each tenant's ``tenant_context`` on. Supplied — and
            disposed of, where that applies — by the caller.
        tenant_ref: Restrict the tick to one tenant (UUID or subdomain),
            bypassing the due gate. Test seam (ADR-0093 §0.4).
        provider: Force the factory to a named provider from the capability
            matrix. Test seam (ADR-0093 §0.4).
        now: The tick's clock, defaulting to "now" in UTC (see
            :func:`run_irene_tick`).

    Returns:
        The tick's :class:`MarketDataTickSummary`. A tenant-level failure is
        counted, never raised; an infrastructure-level failure propagates to
        the caller.
    """
    # The test-seam flags never persist schedule state (ADR-0093 §0.4), so a
    # forced/targeted run does not advance next_due_at / last_run_at.
    persist_schedule = tenant_ref is None and provider is None

    now = now if now is not None else datetime.now(timezone.utc)

    due_count = refreshed = skipped = errors = 0
    async with engine.connect() as conn:
        if tenant_ref is not None:
            due_tenants = await _resolve_single_tenant(conn, tenant_ref)
        else:
            due_tenants = await find_due_market_data_tenants(conn)
    due_count = len(due_tenants)
    _LOG.info(
        "market-data-tick: %d tenant(s) due%s.",
        due_count,
        f" (forced provider={provider!r})" if provider else "",
    )

    for due in due_tenants:
        key = advisory_lock_key(due.tenant_id, domain=_LOCK_DOMAIN)
        try:
            # One transaction per tenant: the advisory-xact-lock claim,
            # the refresh's ingest writes, and mark_run_done all share
            # this single `session` and its transaction. The lock is
            # released automatically at COMMIT/ROLLBACK — no leak if the
            # refresh raises. tenant_context has already dropped to the
            # unprivileged app role by the time it yields (ADR-0078).
            async with tenant_context(engine, due.tenant_id) as session:
                claimed = (
                    await session.execute(
                        text("SELECT pg_try_advisory_xact_lock(:k)"),
                        {"k": key},
                    )
                ).scalar_one()
                if not claimed:
                    _LOG.info(
                        "market-data-tick: tenant %s already claimed by another tick, skipping.",
                        due.tenant_id,
                    )
                    skipped += 1
                    continue

                report = await refresh_tenant_live_data(
                    session,
                    now=now,
                    last_run_at=due.last_run_at,
                    forced_provider=provider,
                )

                if persist_schedule and due.schedule_id is not None:
                    next_due = compute_next_due_at(
                        now, due.cadence, due.preferred_hour, due.timezone
                    )
                    await MarketDataScheduleRepository(session).mark_run_done(
                        schedule_id=due.schedule_id,
                        last_run_at=now,
                        next_due_at=next_due,
                    )
                refreshed += 1
                _LOG.info(
                    "market-data-tick: tenant %s refreshed "
                    "(considered=%d, refreshed_investments=%d, errors=%d, "
                    "inserted=%d, updated_live=%d).",
                    due.tenant_id,
                    report.considered,
                    report.refreshed,
                    report.errors,
                    report.inserted,
                    report.updated_live,
                )
        except Exception as exc:  # noqa: BLE001 — isolate one tenant's failure
            errors += 1
            _LOG.exception(
                "market-data-tick: tenant %s failed: %s",
                due.tenant_id,
                exc,
            )

    _LOG.info(
        "market-data-tick: %d due, %d refreshed, %d skipped, %d errors.",
        due_count,
        refreshed,
        skipped,
        errors,
    )

    return MarketDataTickSummary(
        due=due_count,
        refreshed=refreshed,
        skipped=skipped,
        errors=errors,
    )
