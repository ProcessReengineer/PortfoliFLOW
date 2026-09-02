# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FastAPI app factory and ``portfoliflow-web`` runner.

The app factory builds the engines in a lifespan context, queries the
Alembic ``alembic_version`` table once to populate
``app.state.schema_revision``, mounts static / template directories,
constructs the auth backend, and registers the sub-stream-2b routes.

Two engines are managed:

- ``app.state.engine`` — bound to the unprivileged
  ``portfoliflow_app`` role via ``DATABASE_URL``. This is the engine
  every request handler interacts with for domain-table access; RLS
  policies bind on every query exactly as they will in production.
- ``app.state.audit_engine`` — bound to the Postgres superuser via
  ``DATABASE_URL_SUPERUSER``. Its consumers are **enumerated**, and the
  regression guard
  ``tests/regression/test_audit_engine_only_writes_login_audit.py``
  pins each one to the surface it is sanctioned for: the auth
  backend's ``login_audit`` writes (the asymmetry is documented in
  ``services/auth/local_password.py``), the pre-tenant session
  resolve, the subdomain lookup, the cross-tenant Telegram bot-token
  scan (ADR-0112 §5), and — since ADR-0117 §3 — the built-in tick
  scheduler. The scheduler is the one consumer that also carries
  *tenant-table* writes, and its superuser-privileged reach is
  nonetheless confined to the two cross-tenant due reads: every
  tenant-scoped statement of a beat runs inside ``tenant_context``,
  which drops the session to the unprivileged ``APP_DB_ROLE`` so RLS
  is enforced regardless of the role the engine connects as
  (ADR-0078). Configured only when the superuser URL is supplied;
  without it, the auth backend cannot be constructed and ``/login``
  breaks, and the tick scheduler stays down (the health endpoint
  still reports degraded but liveness).

Per ADR-0041, FastAPI routes do not call ``get_data_store()`` or
import ``PersistentDataStore``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from services.ai_models import ConnectionStatus
from services.ai_service_core import AIServiceCore, get_ai_service_core
from services.auth.local_password import LocalPasswordAuthBackend
from services.credential_vault import is_vault_configured
from services.tenant_resolution import SubdomainTenantResolver
from web.routes.areas import router as areas_router
from web.routes.benchmarks_attribution import (
    router as benchmarks_attribution_router,
)
from web.routes.cases import router as cases_router
from web.routes.charts import router as charts_router
from web.routes.chat import router as chat_router
from web.routes.cmd_search import router as cmd_search_router
from web.routes.data_import import router as data_import_router
from web.routes.watch_desk import router as watch_desk_router
from web.routes.market_data import router as market_data_router
from web.routes.health import router as health_router
from web.routes.investments import router as investments_router
from web.routes.limits import router as limits_router
from web.routes.login import router as login_router
from web.routes.overview import router as overview_router
from web.routes.planning_desk import router as planning_desk_router
from web.routes.portfolio_analysis import router as portfolio_analysis_router
from web.routes.portfolio_review import router as portfolio_review_router
from web.routes.provider_credentials import router as provider_credentials_router
from web.routes.saa_section import router as saa_section_router
from web.routes.scraper import router as scraper_router
from web.routes.shell import router as shell_router
from web.routes.statistics import router as statistics_router
from web.routes.super_admin import router as super_admin_router
from web.routes.tenant_users import router as tenant_users_router
from web.routes.transactions import router as transactions_router
from web.settings import WebSettings, get_web_settings
from web.shell import (
    all_areas,
    build_sha,
    config_ok,
    is_sidebar_collapsed,
)
from web.tick_scheduler import start_tick_scheduler, stop_tick_scheduler

_LOG = logging.getLogger("portfoliflow.web")

_WEB_DIR: Path = Path(__file__).resolve().parent
_STATIC_DIR: Path = _WEB_DIR / "static"
_TEMPLATES_DIR: Path = _WEB_DIR / "templates"


def _configure_ai_core(settings: WebSettings) -> AIServiceCore:
    """Park the application-wide :class:`AIServiceCore` for this app.

    **Chat, the Irene beat and the Telegram bot no longer read anything
    this function sets.** Since ADR-0112 §4b each of them resolves its own
    endpoint, credential and model *per turn*, inside the requesting
    tenant's context, through the credential façade — so a tenant or user
    row written in Admin → Providers & Credentials applies on the next
    turn, with no restart, and one process serves many tenants without
    their keys ever meeting. ``.env`` remains the **application scope** of
    that chain: the last link, consulted when no vault row serves.

    What the parked configuration is still for: the **one** one-shot
    extraction consumer that has no per-tenant resolution — the News
    Scraper's Fetcher-LLM (``services/web_research``). It calls
    :meth:`AIServiceCore.send_one_shot_extraction` on this singleton from
    synchronous tool threads that have no session and no tenant context to
    resolve in, and on that path the method still gates on the singleton
    triple plus ``CONNECTED``. Leaving the singleton unconfigured would take
    it down — and with it the untrusted-content extraction ADR-0022 requires
    — so the application-scope credentials stay parked here until that
    consumer gets its own resolution seam.

    The Report Scraper was the second such consumer until ADR-0123: it now
    resolves per run, per tenant, through the same façade as chat and passes
    a :class:`~services.ai_service_core.ResolvedLLM` into the extraction
    call, so nothing it does depends on what this function sets.

    The singleton is intentionally reused — see ADR-0038 §5. Tests that
    need a different state per app instance override via
    ``app.state.ai_core``.

    Args:
        settings: The resolved :class:`WebSettings` for this app.

    Returns:
        The application-wide :class:`AIServiceCore` instance.
    """
    core = get_ai_service_core()
    if not settings.openrouter_api_key:
        # Reset to a known clean state so a previously-configured
        # singleton (e.g. left over from a prior app instance in the
        # same process — the test suite does this) cannot leak its
        # CONNECTED status into a no-credentials lifespan.
        core.reset()
        if not is_vault_configured():
            # Neither scope can serve anything: no vault for tenant rows,
            # no environment key. Chat will 503 on every turn, so say why
            # at startup rather than leaving the operator to discover it
            # one failed message at a time.
            _LOG.warning(
                "AIServiceCore: OPENROUTER_API_KEY not set and no credential "
                "vault configured — no scope can resolve an LLM credential; "
                "chat and the Report Scraper will refuse every turn. Set a "
                "tenant key in Admin → Providers & Credentials (needs the "
                "vault master key), or OPENROUTER_API_KEY in .env."
            )
        else:
            _LOG.info(
                "AIServiceCore: OPENROUTER_API_KEY not set — chat and the "
                "Report Scraper resolve per tenant from the credential vault "
                "(ADR-0112 §4b, ADR-0123). The Fetcher-LLM, the last consumer "
                "still reading the application scope, is unavailable until it "
                "is set."
            )
        return core

    core.configure(settings.openrouter_base_url, settings.openrouter_api_key)
    if settings.shirley_model:
        core.set_model(settings.shirley_model)
    # The Qt adapter only flips status to CONNECTED after a successful
    # ``fetch_models`` round-trip. The web lifespan skips that probe
    # to keep startup fast and offline-tolerant; a missing or invalid
    # key surfaces on the first call as an upstream error, not as a
    # startup failure.
    core.set_status(ConnectionStatus.CONNECTED)
    _LOG.info(
        "AIServiceCore: application-scope credentials parked for the one-shot "
        "extraction consumers (model=%r). Chat, Irene and the bot resolve "
        "per turn (ADR-0112 §4b).",
        settings.shirley_model or "<unset>",
    )
    return core


async def _read_schema_revision(database_url: str | None) -> str | None:
    """Best-effort fetch of Alembic's head revision id."""
    if not database_url:
        return None
    try:
        engine = create_async_engine(database_url, future=True)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
                row = result.first()
                return row[0] if row is not None else None
        finally:
            await engine.dispose()
    except Exception as exc:  # noqa: BLE001 - degraded mode is intentional
        _LOG.warning("schema-revision lookup failed: %s", exc)
        return None


def create_app(settings: WebSettings | None = None) -> FastAPI:
    """Build a configured FastAPI application."""
    resolved_settings = settings if settings is not None else get_web_settings()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # App engine — bound to portfoliflow_app, used by every route.
        # pool_pre_ping recovers connections severed by a Postgres
        # restart; pool_timeout converts pool exhaustion into a fast,
        # loud TimeoutError rather than an indefinite hang (ADR-0065 §4).
        if resolved_settings.database_url:
            app.state.engine = create_async_engine(
                resolved_settings.database_url,
                future=True,
                pool_pre_ping=True,
                pool_timeout=10,
            )
        else:
            app.state.engine = None

        # Audit engine — bound to the Postgres superuser. Its consumers
        # are enumerated in the module docstring above and asserted by
        # tests/regression/test_audit_engine_only_writes_login_audit.py;
        # the fifth, added by ADR-0117 §3, is the built-in tick scheduler
        # started at the end of this lifespan. The asymmetry is documented
        # in services/auth/local_password.py.
        if resolved_settings.database_url_superuser:
            app.state.audit_engine = create_async_engine(
                resolved_settings.database_url_superuser,
                future=True,
                pool_pre_ping=True,
            )
        else:
            app.state.audit_engine = None

        # Auth backend — constructed only when both engines exist.
        # When either engine is missing, /login responds with 503;
        # /health still reports liveness.
        if app.state.engine is not None and app.state.audit_engine is not None:
            app.state.auth_backend = LocalPasswordAuthBackend(
                app_engine=app.state.engine,
                audit_engine=app.state.audit_engine,
            )
        else:
            app.state.auth_backend = None

        # Tenant resolver — per ADR-0063 §1. Constructed against the
        # audit engine because resolution runs before any tenant
        # context exists; the regression test for audit-engine usage
        # lists this as the third sanctioned path.
        if app.state.audit_engine is not None:
            app.state.tenant_resolver = SubdomainTenantResolver(app.state.audit_engine)
        else:
            app.state.tenant_resolver = None

        app.state.schema_revision = await _read_schema_revision(resolved_settings.database_url)

        # AI core — parked here for the one-shot extraction consumers; the
        # chat turn resolves its own credential and model per request
        # (ADR-0112 §4b). See :func:`_configure_ai_core`. Tests that need a
        # different instance per app override ``app.state.ai_core`` after
        # lifespan startup completes (see
        # ``tests/web/test_chat_sse.py::web_client_factory``); the
        # ``getattr(..., "ai_core", None)`` check in
        # :func:`web.routes.chat._ai_core` honours that override.
        app.state.ai_core = _configure_ai_core(resolved_settings)

        # In-process Telegram bot (ADR-0063, ADR-0112 §5). Started here so
        # Shirley on Telegram reads the same Postgres data as the web chat.
        # Since ADR-0112 §5 one thread multiplexes **one bot per tenant**:
        # the bot discovers each tenant's stored ``telegram.bot_token`` on
        # the superuser URL injected below, and the deprecated
        # ``SHIRLEY_BOT_TENANT_SUBDOMAIN`` binds only the additive
        # environment-token dispatcher — resolved here because the bot has
        # no RLS-bypass and the ``tenants`` table is unreadable without a
        # tenant context. A bot failure must never block web startup.
        #
        # Single-worker only, and now load-bearing N times over: each bot
        # polls Telegram via ``getUpdates``, Telegram allows exactly one
        # ``getUpdates`` consumer per token, so a second uvicorn worker
        # would start a second copy of *every* tenant's bot and each one
        # would steal half the other's updates. This is the same
        # single-worker assumption already made by ``pending_turns`` and the
        # process-wide ``_TURN_LOCK`` — see docs/deploy/telegram-multi-bot.md.
        try:
            from bot.config import get_bot_config

            bot_cfg = get_bot_config()
            if bot_cfg.enabled:
                bot_tenant_id = None
                if bot_cfg.telegram_token:
                    # Only an environment token needs this binding, so only
                    # then is the deprecated subdomain actually used.
                    _LOG.warning(
                        "Telegram bot: TELEGRAM_BOT_TOKEN is set, so "
                        "SHIRLEY_BOT_TENANT_SUBDOMAIN (%r) binds it to one "
                        "tenant. Both are deprecated transition config "
                        "(ADR-0112 §5) — store the token per tenant under "
                        "Admin → Providers & Credentials instead.",
                        bot_cfg.tenant_subdomain,
                    )
                    if app.state.tenant_resolver is None:
                        _LOG.error(
                            "Telegram bot: the tenant resolver is unavailable "
                            "(audit engine missing), so the environment token "
                            "cannot be bound to a tenant; starting the "
                            "discovered per-tenant bots only."
                        )
                    else:
                        bot_tenant_id = await app.state.tenant_resolver.resolve(
                            host=f"{bot_cfg.tenant_subdomain}.localhost"
                        )
                        _LOG.info(
                            "Telegram bot: resolved tenant %r -> %s",
                            bot_cfg.tenant_subdomain,
                            bot_tenant_id,
                        )
                from bot.telegram_bot import start_bot

                start_bot(
                    tenant_id=bot_tenant_id,
                    database_url=resolved_settings.database_url,
                    superuser_url=resolved_settings.database_url_superuser or "",
                )
        except Exception:  # noqa: BLE001 — bot must never block web startup
            _LOG.exception("Telegram bot failed to start; web continues without it.")

        # Built-in tick scheduler (ADR-0117) — the default tick source, so
        # a fresh install has a working Irene heartbeat and market-data
        # refresh with no systemd knowledge required. Started last, after
        # the audit engine it drives the shared runner with (ADR-0117 §3 —
        # no second superuser engine on the same URL) and after the bot,
        # and it sleeps one full interval before its first tick, so it
        # never competes with startup.
        #
        # Single-worker only, like the bot above — though harmlessly so:
        # a second worker's scheduler would find every beat already
        # claimed by pg_try_advisory_xact_lock and skip it.
        app.state.tick_scheduler = None
        if not resolved_settings.tick_scheduler_enabled:
            _LOG.info(
                "tick-scheduler: disabled (TICK_SCHEDULER_ENABLED=false) — no "
                "in-process task; an external tick source is expected (the "
                "systemd units under docs/deploy/, cron, or equivalent). "
                "Without one, no tenant is ever beaten."
            )
        elif app.state.audit_engine is None:
            _LOG.warning(
                "tick-scheduler: DATABASE_URL_SUPERUSER is not set, so the "
                "cross-tenant due read has no RLS-bypassing connection — the "
                "built-in scheduler stays down (ADR-0117 §3)."
            )
        else:
            app.state.tick_scheduler = start_tick_scheduler(
                app.state.audit_engine, resolved_settings
            )

        try:
            yield
        finally:
            # Stop the bot first — it may be mid-turn, and the turn's tools
            # reach Postgres through their own short-lived engine; signal
            # shutdown before disposing the app engines below.
            try:
                from bot.telegram_bot import stop_bot

                stop_bot()
            except Exception:  # noqa: BLE001 — never let bot teardown mask shutdown
                _LOG.exception("Telegram bot stop raised; ignored.")
            # Then the tick scheduler — before either engine is disposed,
            # because a tick in flight is holding a transaction on the
            # audit engine. It gets a bounded grace period and is
            # cancelled after it; the advisory-lock transaction rolls
            # back, next_due_at stays unadvanced, and the beat is retried
            # after restart (ADR-0117 §1).
            scheduler = getattr(app.state, "tick_scheduler", None)
            if scheduler is not None:
                try:
                    await stop_tick_scheduler(scheduler)
                except Exception:  # noqa: BLE001 — never let it mask shutdown
                    _LOG.exception("Tick scheduler stop raised; ignored.")
                app.state.tick_scheduler = None
            if app.state.engine is not None:
                await app.state.engine.dispose()
            if app.state.audit_engine is not None:
                await app.state.audit_engine.dispose()

    app = FastAPI(
        title="PortfoliFLOW (web)",
        lifespan=_lifespan,
    )
    app.state.settings = resolved_settings

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Browsers request ``GET /favicon.ico`` from the document root
    # regardless of the ``<link rel="icon">`` tag in <head>. Serving
    # it here keeps the access log free of spurious 404s and gives
    # Chromium-based browsers a stable hit for the tab-icon cache.
    _favicon_path = _STATIC_DIR / "favicons" / "favicon.ico"

    @app.get("/favicon.ico", include_in_schema=False)
    async def _favicon() -> FileResponse:
        return FileResponse(_favicon_path)

    # Shell-level context processor — every TemplateResponse gets the
    # always-the-same sidebar / status bar variables (build SHA,
    # config flag, sidebar-collapsed cookie, return-to-URL). Starlette
    # merges the processor output AFTER the route-provided context, so
    # the processor MUST NOT supply any key a route is expected to set
    # per-render — ``user_email``, ``active_area``,
    # ``active_area_label``, ``csrf_token``. The status bar template
    # falls back gracefully when ``tenant_name`` is missing (e.g. on
    # the login page).
    #
    # ``redirect_to`` carries the path of the current request so the
    # sidebar collapse form posts back to the same area page; without
    # it the form falls back to ``"/"`` and bounces the user to
    # /front-office on every toggle, which masks the bidirectional
    # flip with an unexpected navigation.
    def _shell_processor(request: Request) -> dict[str, object]:
        # Per ADR-0063, ``tenant_name`` resolves from
        # ``request.state.tenant_name`` if a middleware/dependency
        # populated it; otherwise it falls back to an empty string
        # and the status-bar template handles the absence gracefully.
        # Routes that authenticate end up populating it from the
        # session's tenant id; the login page leaves it empty.
        #
        # ``show_super_admin_link`` (ADR-0064 §1, roadmap B1b) controls
        # the conditional Platform Admin sidebar entry. The dependency
        # chain (get_authenticated_user / require_super_admin) stashes
        # the UserDTO on request.state.user; the processor derives the
        # flag from it. Routes that don't load the user (login page,
        # health page) get show_super_admin_link=False, which is right.
        user = getattr(request.state, "user", None)
        return {
            "sidebar_collapsed": is_sidebar_collapsed(request),
            "build_sha": build_sha(),
            "config_ok": config_ok(request),
            "tenant_name": getattr(request.state, "tenant_name", ""),
            "redirect_to": request.url.path,
            "show_super_admin_link": bool(
                user is not None and getattr(user, "is_super_admin", False)
            ),
        }

    templates = Jinja2Templates(
        directory=str(_TEMPLATES_DIR),
        context_processors=[_shell_processor],
    )

    # Jinja global — resolve an active-area slug into its display
    # label. Used by the status bar template; keeps routes free of
    # the lookup.
    def _pf_area_label(slug: str | None) -> str:
        if slug is None:
            return "PortfoliFLOW"
        for area in all_areas():
            if area.slug == slug:
                return area.label
        return "PortfoliFLOW"

    templates.env.globals["pf_area_label"] = _pf_area_label
    app.state.templates = templates

    app.include_router(health_router)
    app.include_router(login_router)
    app.include_router(chat_router)
    app.include_router(data_import_router)
    app.include_router(statistics_router, tags=["statistics"])
    app.include_router(charts_router, tags=["charts"])
    app.include_router(overview_router, tags=["overview"])
    app.include_router(portfolio_analysis_router, tags=["portfolio-analysis"])
    app.include_router(portfolio_review_router, tags=["portfolio-review"])
    app.include_router(saa_section_router, tags=["saa"])
    app.include_router(limits_router, tags=["limits"])
    app.include_router(benchmarks_attribution_router, tags=["benchmarks-attribution"])
    app.include_router(investments_router, tags=["investments"])
    app.include_router(watch_desk_router, tags=["watch-desk"])
    app.include_router(planning_desk_router, tags=["planning-desk"])
    app.include_router(cases_router, tags=["cases"])
    app.include_router(transactions_router, tags=["transactions"])
    app.include_router(market_data_router, tags=["market-data"])
    app.include_router(areas_router, tags=["areas"])
    app.include_router(provider_credentials_router, tags=["providers-credentials"])
    app.include_router(tenant_users_router, tags=["tenant-users"])
    app.include_router(scraper_router, tags=["scraper"])
    app.include_router(shell_router, tags=["shell"])
    app.include_router(cmd_search_router, tags=["cmd-search"])
    app.include_router(super_admin_router)

    return app


def run() -> None:
    """Entry point for the ``portfoliflow-web`` console script."""
    settings = get_web_settings()
    uvicorn.run(
        "web.main:create_app",
        factory=True,
        host=settings.web_host,
        port=settings.web_port,
    )
