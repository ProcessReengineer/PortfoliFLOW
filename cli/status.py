# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow status`` subcommand.

A non-destructive diagnostic snapshot of PortfoliFLOW's runtime
state. Reports six sections:

- **Database** — connection reachability, schema head, pending
  migrations.
- **Tenants** — total count, sentinel tenant presence.
- **Users** — total count, count under the sentinel tenant, the
  tenant-owner email if exactly one is present.
- **AIService** — whether ``OPENROUTER_API_KEY`` is configured, the
  default model, optional reachability probe.
- **Web Application** — ``.env`` discovery, static / templates
  directory presence.

Useful as a fast triage tool during development and as a smoke check
after deployment. Does not write to the database; runs are safe to
issue against any environment.

Flags:
    ``--check-ai`` — run a five-second probe against
    ``OPENROUTER_BASE_URL/models`` with the configured API key.
    Skipped by default to avoid network load and to keep the command
    offline-safe.

    ``--json`` — emit the report as JSON (one object) instead of the
    formatted text output. Convenient for CI / shell-script
    consumption.

Exit codes:
    ``0`` — all checks passed; the deployment is healthy.

    ``1`` — at least one critical issue was detected (schema out of
    date, sentinel missing, AIService not configured). The text
    output flags each issue inline.

    ``2`` — the status check itself failed (DB unreachable, missing
    superuser URL, etc.). Distinguished from ``1`` so a CI runner
    can tell "deployment broken" from "couldn't even check".

Sub-stream 3a, Task 3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import typer
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cli._db import superuser_engine
from core.exceptions import ConfigurationError
from core.logging_setup import configure_logging
from core.tenant_constants import SENTINEL_TENANT_ID

_LOG = logging.getLogger("portfoliflow.cli")

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_ALEMBIC_INI: Path = _REPO_ROOT / "db" / "alembic.ini"
_WEB_DIR: Path = _REPO_ROOT / "web"
_THEME_CSS: Path = _WEB_DIR / "static" / "css" / "theme.css"
_TEMPLATES_DIR: Path = _WEB_DIR / "templates"
_ENV_PATH: Path = _REPO_ROOT / ".env"


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class _SectionReport:
    """One section of the status report.

    Attributes:
        ok: Whether this section is healthy. ``False`` contributes to
            an exit-code-1 status.
        items: Ordered ``(label, value)`` pairs rendered to the text
            output and serialised in ``--json`` mode.
        notes: Extra free-form lines that should appear under the
            section in text mode (e.g. operator-readable hints when
            something is misconfigured).
    """

    ok: bool = True
    items: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _StatusReport:
    """The full diagnostic snapshot."""

    database: _SectionReport = field(default_factory=_SectionReport)
    tenants: _SectionReport = field(default_factory=_SectionReport)
    users: _SectionReport = field(default_factory=_SectionReport)
    ai_service: _SectionReport = field(default_factory=_SectionReport)
    web_application: _SectionReport = field(default_factory=_SectionReport)

    def is_healthy(self) -> bool:
        """Return ``True`` when every section reports OK."""
        return all(
            section.ok
            for section in (
                self.database,
                self.tenants,
                self.users,
                self.ai_service,
                self.web_application,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        """Render the report as a JSON-serialisable mapping."""
        return {
            "database": asdict(self.database),
            "tenants": asdict(self.tenants),
            "users": asdict(self.users),
            "ai_service": asdict(self.ai_service),
            "web_application": asdict(self.web_application),
            "healthy": self.is_healthy(),
        }


# ---------------------------------------------------------------------------
# Migration helpers (sync — Alembic's API is synchronous)
# ---------------------------------------------------------------------------


def _alembic_script_directory() -> ScriptDirectory:
    """Return the Alembic :class:`ScriptDirectory` for this repo."""
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_ALEMBIC_INI.parent / "migrations"))
    return ScriptDirectory.from_config(cfg)


def _expected_head_revision(script: ScriptDirectory) -> str | None:
    """Return the latest revision in the migration tree, or ``None`` if empty."""
    heads = script.get_heads()
    return heads[0] if heads else None


def _pending_revisions(script: ScriptDirectory, current: str | None) -> list[str]:
    """Return revisions present in the tree but not yet applied to the DB.

    Walks from the migration tree's head down to ``current``. If
    ``current`` is ``None`` (DB has no ``alembic_version`` row), every
    revision in the tree is pending.

    Args:
        script: Alembic's :class:`ScriptDirectory` for this repo.
        current: The revision currently recorded in
            ``alembic_version``, or ``None`` if the row is absent.

    Returns:
        Revision identifiers in head-first order. Empty when up to date.
    """
    head = _expected_head_revision(script)
    if head is None:
        return []
    if current == head:
        return []
    pending: list[str] = []
    for revision in script.walk_revisions(base="base", head=head):
        if revision.revision == current:
            break
        pending.append(revision.revision)
    return pending


# ---------------------------------------------------------------------------
# Async DB queries
# ---------------------------------------------------------------------------


async def _query_database_section(engine: AsyncEngine, report: _StatusReport) -> str | None:
    """Populate the database section. Returns the current head revision."""
    section = report.database
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            row = result.first()
            current_revision = row[0] if row is not None else None
    except Exception as exc:  # noqa: BLE001 — surface as section error
        section.ok = False
        section.items.append(("Connection", f"FAILED ({exc})"))
        section.notes.append(
            "Database is unreachable. Check that the compose container "
            "is running and DATABASE_URL_SUPERUSER is correct."
        )
        return None

    section.items.append(("Connection", "OK"))
    section.items.append(("Schema Head (DB)", current_revision or "<none>"))

    try:
        script = _alembic_script_directory()
    except Exception as exc:  # noqa: BLE001
        section.ok = False
        section.items.append(("Migration Tree", f"FAILED ({exc})"))
        section.notes.append(
            "Could not load Alembic migration tree. The CLI must be invoked from the repo root."
        )
        return current_revision

    expected_head = _expected_head_revision(script)
    section.items.append(("Schema Head (Tree)", expected_head or "<none>"))

    pending = _pending_revisions(script, current_revision)
    if pending:
        section.ok = False
        section.items.append(("Pending Migrations", f"{len(pending)} ({', '.join(pending)})"))
        section.notes.append("Schema is out of date. Run `cd db && alembic upgrade head`.")
    else:
        section.items.append(("Pending Migrations", "none"))

    return current_revision


async def _query_tenant_section(engine: AsyncEngine, report: _StatusReport) -> None:
    """Populate the tenants section."""
    section = report.tenants
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COUNT(*) FROM tenants"))
            total = int(result.scalar_one())
            sentinel_result = await conn.execute(
                text("SELECT name FROM tenants WHERE id = :tid"),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            sentinel_row = sentinel_result.first()
    except Exception as exc:  # noqa: BLE001 — surface as section error
        section.ok = False
        section.items.append(("Total", f"UNKNOWN ({exc})"))
        return

    section.items.append(("Total", str(total)))
    section.items.append(("Sentinel ID", str(SENTINEL_TENANT_ID)))
    if sentinel_row is None:
        section.ok = False
        section.items.append(("Sentinel exists", "no"))
        section.notes.append("Sentinel tenant is missing. Run `portfoliflow bootstrap`.")
    else:
        section.items.append(("Sentinel exists", "yes"))
        section.items.append(("Sentinel name", str(sentinel_row.name)))


async def _query_user_section(engine: AsyncEngine, report: _StatusReport) -> None:
    """Populate the users section."""
    section = report.users
    try:
        async with engine.connect() as conn:
            total_result = await conn.execute(text("SELECT COUNT(*) FROM users"))
            total = int(total_result.scalar_one())
            sentinel_users_result = await conn.execute(
                text(
                    "SELECT email, is_active FROM users "
                    "WHERE tenant_id = :tid "
                    "  AND 'owner' = ANY(roles) "
                    "ORDER BY email"
                ),
                {"tid": str(SENTINEL_TENANT_ID)},
            )
            owner_rows = list(sentinel_users_result.fetchall())
    except Exception as exc:  # noqa: BLE001 — surface as section error
        section.ok = False
        section.items.append(("Total", f"UNKNOWN ({exc})"))
        return

    section.items.append(("Total", str(total)))
    if not owner_rows:
        section.ok = False
        section.items.append(("Sentinel owner", "<none>"))
        section.notes.append(
            "No tenant-owner user found under the sentinel tenant. Run `portfoliflow bootstrap`."
        )
    elif len(owner_rows) == 1:
        row = owner_rows[0]
        section.items.append(("Sentinel owner email", str(row.email)))
        active = "yes" if bool(row.is_active) else "no"
        section.items.append(("Sentinel owner active", active))
        if not bool(row.is_active):
            section.ok = False
            section.notes.append("Sentinel owner is inactive — login will be rejected.")
    else:
        emails = ", ".join(str(r.email) for r in owner_rows)
        section.items.append(
            (
                "Sentinel owners",
                f"{len(owner_rows)} ({emails})",
            )
        )


# ---------------------------------------------------------------------------
# AIService section (sync — env vars + optional httpx probe)
# ---------------------------------------------------------------------------


def _populate_ai_service_section(
    report: _StatusReport,
    *,
    check_ai: bool,
    reachability_client: httpx.Client | None = None,
) -> None:
    """Populate the AIService section.

    Args:
        report: The full report under construction.
        check_ai: Whether to run a reachability probe against the
            OpenRouter ``/models`` endpoint.
        reachability_client: Injection seam for tests — when provided
            this client is used instead of constructing a fresh one.
            Production code passes ``None`` so the function builds a
            short-lived client itself.
    """
    section = report.ai_service
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("SHIRLEY_MODEL", "")

    if api_key:
        section.items.append(("API Key configured", "yes"))
    else:
        section.ok = False
        section.items.append(("API Key configured", "no"))
        section.notes.append(
            "OPENROUTER_API_KEY is not set. The web chat endpoint will "
            "respond 503 until configured."
        )

    section.items.append(("Base URL", base_url))
    section.items.append(("Default Model", model or "<unset>"))
    if not model:
        section.ok = False
        section.notes.append(
            "SHIRLEY_MODEL is not set. The web chat endpoint will respond 503 until configured."
        )

    if not check_ai:
        section.items.append(("Reachability", "SKIPPED (use --check-ai)"))
        return

    if not api_key:
        section.items.append(("Reachability", "SKIPPED (no API key)"))
        return

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if reachability_client is not None:
            response = reachability_client.get(url, headers=headers, timeout=5.0)
        else:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(url, headers=headers)
    except httpx.RequestError as exc:
        section.ok = False
        section.items.append(("Reachability", f"FAILED ({exc})"))
        return

    if 200 <= response.status_code < 300:
        section.items.append(("Reachability", f"OK (HTTP {response.status_code})"))
    else:
        section.ok = False
        section.items.append(("Reachability", f"FAILED (HTTP {response.status_code})"))


# ---------------------------------------------------------------------------
# Web application section (sync — filesystem checks)
# ---------------------------------------------------------------------------


def _populate_web_section(report: _StatusReport) -> None:
    """Populate the web-application section."""
    section = report.web_application
    if _ENV_PATH.exists():
        section.items.append((".env file", "OK"))
    else:
        section.items.append((".env file", "<missing>"))
        section.notes.append(
            ".env not found at repo root. Web settings will fall back to environment-only defaults."
        )

    if _THEME_CSS.exists():
        section.items.append(("Theme CSS", "OK"))
    else:
        section.ok = False
        section.items.append(("Theme CSS", "<missing>"))
        section.notes.append(
            "web/static/css/theme.css is missing. Regenerate with "
            "`python -m scripts.generate_theme_artifacts`."
        )

    if _TEMPLATES_DIR.is_dir():
        section.items.append(("Templates dir", "OK"))
    else:
        section.ok = False
        section.items.append(("Templates dir", "<missing>"))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _build_report_async(check_ai: bool) -> _StatusReport:
    """Build the full status report by issuing every async query."""
    report = _StatusReport()
    engine = superuser_engine()
    try:
        await _query_database_section(engine, report)
        await _query_tenant_section(engine, report)
        await _query_user_section(engine, report)
    finally:
        await engine.dispose()
    _populate_ai_service_section(report, check_ai=check_ai)
    _populate_web_section(report)
    return report


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


_SECTION_TITLES: tuple[tuple[str, str], ...] = (
    ("database", "Database"),
    ("tenants", "Tenants"),
    ("users", "Users"),
    ("ai_service", "AIService"),
    ("web_application", "Web Application"),
)


def _format_section(title: str, section: _SectionReport) -> Iterable[str]:
    """Yield the rendered lines of one section."""
    yield title
    yield "-" * len(title)
    if not section.items:
        yield "  (no data)"
    label_width = max((len(label) for label, _ in section.items), default=0)
    for label, value in section.items:
        yield f"  {label.ljust(label_width)}  {value}"
    for note in section.notes:
        yield f"  ! {note}"
    yield ""


def _render_text(report: _StatusReport) -> str:
    """Render the report as the text format documented in the module header."""
    lines: list[str] = []
    lines.append("PortfoliFLOW Status")
    lines.append("===================")
    lines.append("")
    for attr, title in _SECTION_TITLES:
        section: _SectionReport = getattr(report, attr)
        lines.extend(_format_section(title, section))
    overall = "OK" if report.is_healthy() else "ATTENTION REQUIRED"
    lines.append(f"Overall: {overall}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Typer command
# ---------------------------------------------------------------------------


def status_command(
    check_ai: bool = typer.Option(
        False,
        "--check-ai",
        help=(
            "Probe the OpenRouter /models endpoint. Skipped by default "
            "to keep the command offline-safe; enable when troubleshooting "
            "an OPENROUTER_API_KEY."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the report as JSON instead of the formatted text output.",
    ),
) -> None:
    """Print a diagnostic snapshot of PortfoliFLOW's runtime state.

    See the module docstring for the full output shape and exit-code
    contract. Non-destructive — issues no writes against any database.
    """
    configure_logging()

    try:
        report = asyncio.run(_build_report_async(check_ai=check_ai))
    except ConfigurationError as exc:
        _LOG.error("status: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001 — distinguish from health failure
        _LOG.error("status: status check itself failed: %s", exc)
        raise typer.Exit(code=2) from exc

    if json_output:
        typer.echo(json.dumps(report.as_dict(), indent=2, default=str))
    else:
        typer.echo(_render_text(report))

    if not report.is_healthy():
        raise typer.Exit(code=1)
