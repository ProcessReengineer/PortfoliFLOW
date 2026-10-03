# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow bootstrap`` and ``portfoliflow set-password`` subcommands.

Both subcommands implement the contract from ADR-0040, extended by
ADR-0063 / ADR-0064 to seed the multi-tenant substrate:

- The **system tenant** (``SYSTEM_TENANT_ID``, subdomain ``admin``,
  name "Platform Administration") hosts super-admin user accounts.
- The **primary tenant** (``PRIMARY_TENANT_ID``, subdomain
  ``minathena-capital``, name "Minathena Capital") is the
  primary tenant previously called "Sentinel Tenant".
- An initial super-admin user is created in the system tenant from
  ``SUPER_ADMIN_EMAIL`` / ``SUPER_ADMIN_PASSWORD``.
- The primary-tenant owner is created from ``OWNER_EMAIL`` /
  ``OWNER_PASSWORD`` (falling back to the historical
  ``SENTINEL_EMAIL`` / ``SENTINEL_PASSWORD`` names with a deprecation
  warning so existing ``.env`` files continue to work).

Drift detection on existing users now compares ``roles`` against the
expected ``['owner']`` rather than the dropped ``is_tenant_owner``
column. Password drift is intentionally not detected — the persisted
hash is authoritative; rotation goes through ``set-password``.

Password rotation also invalidates every active session for the
target user (per OWASP session-management guidance — a credential
change should not leave existing sessions valid).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from uuid import UUID

import typer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cli._db import superuser_engine
from core.exceptions import (
    ConfigurationError,
    PortfoliFlowError,
    ValidationError,
)
from core.logging_setup import configure_logging
from core.repositories import (
    AssetClassRepository,
    RegionRepository,
    SAAAssetClassInputRepository,
    SAAConfigurationRepository,
    SAACorrelationRepository,
    SectorRepository,
    UserRepository,
    tenant_context,
)
from core.repositories.country_repository import CountryRepository
from core.repositories.irene_schedule_repository import IreneScheduleRepository
from core.repositories.market_data_schedule_repository import (
    MarketDataScheduleRepository,
)
from core.tenant_constants import (
    PRIMARY_TENANT_ID,
    SENTINEL_TENANT_ID,
    SYSTEM_TENANT_ID,
)
from services.auth.password_policy import validate_password_strength
from services.password_hashing import hash_password
from services.saa import SAAService, install_seeds_for_tenant
from services.super_admin import create_super_admin_idempotent
from services.tenant_defaults import (
    install_unclassified_asset_class,
    install_default_asset_classes,
    install_unclassified_sector,
    install_default_regions,
    install_market_data_system_actor,
    install_market_data_schedule,
    install_irene_schedule,
)
from services.watch_desk.seeding import install_default_watchpoints_for_tenant

_LOG = logging.getLogger("portfoliflow.cli")

# Pre-rename historical name (kept for the b008 → b012 transition test).
_SENTINEL_TENANT_NAME: str = "Sentinel Tenant"
_PRIMARY_TENANT_NAME: str = "Minathena Capital"
_PRIMARY_TENANT_SUBDOMAIN: str = "minathena-capital"
_SYSTEM_TENANT_NAME: str = "Platform Administration"
_SYSTEM_TENANT_SUBDOMAIN: str = "admin"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_password_from_stdin() -> str:
    """Read one line from stdin, stripping a trailing newline."""
    line = sys.stdin.readline()
    if not line:
        raise ConfigurationError(
            "No password provided on stdin. Pipe the password into the "
            "command, e.g. `echo $SENTINEL_PASSWORD | portfoliflow bootstrap "
            "--password-stdin`."
        )
    return line.rstrip("\n")


def _resolve_email(cli_value: str | None) -> str:
    """Resolve the primary-tenant owner email.

    Precedence: --email flag → OWNER_EMAIL → SENTINEL_EMAIL (legacy,
    warns). Raises if none is set.
    """
    if cli_value:
        return cli_value
    email = os.getenv("OWNER_EMAIL")
    if email:
        return email
    legacy = os.getenv("SENTINEL_EMAIL")
    if legacy:
        _LOG.warning("bootstrap: SENTINEL_EMAIL is deprecated; rename to OWNER_EMAIL in .env.")
        return legacy
    raise ConfigurationError("Owner email missing. Set OWNER_EMAIL in .env or pass --email.")


def _resolve_display_name() -> str | None:
    """Resolve the primary-tenant owner's display name (ADR-0068).

    Reads ``OWNER_DISPLAY_NAME`` from the environment (loaded from
    ``.env`` by ``cli._db`` at import time). Returns ``None`` when the
    variable is unset or blank — the column is nullable and optional,
    so an absent name is a valid, non-error state.
    """
    value = os.getenv("OWNER_DISPLAY_NAME", "").strip()
    return value or None


def _resolve_password(use_stdin: bool) -> str | None:
    """Resolve the primary-tenant owner password.

    Precedence: stdin (when --password-stdin) → OWNER_PASSWORD →
    SENTINEL_PASSWORD (legacy, warns). Returns None when nothing is
    found, preserving the caller's existing "missing password" guard.
    """
    if use_stdin:
        return _read_password_from_stdin()
    env_value = os.getenv("OWNER_PASSWORD")
    if env_value:
        return env_value
    legacy = os.getenv("SENTINEL_PASSWORD")
    if legacy:
        _LOG.warning(
            "bootstrap: SENTINEL_PASSWORD is deprecated; rename to OWNER_PASSWORD in .env."
        )
        return legacy
    return None


# ---------------------------------------------------------------------------
# bootstrap subcommand
# ---------------------------------------------------------------------------


async def _run_bootstrap(
    engine: AsyncEngine,
    email: str,
    password: str | None,
    display_name: str | None = None,
) -> UUID:
    """Perform the bootstrap transaction.

    See ADR-0040 §4 for the idempotency contract and ADR-0063 §6 +
    ADR-0064 §5 for the multi-tenant extension. The function:

    1. Seeds the system tenant idempotently.
    2. Seeds the primary tenant (renamed from "Sentinel Tenant" to
       "Minathena Capital") idempotently.
    3. Creates the primary-tenant owner from the supplied
       ``(email, password)``.
    4. Optionally creates the first super-admin from
       ``SUPER_ADMIN_EMAIL`` / ``SUPER_ADMIN_PASSWORD`` if set.

    Drift detection on the primary-tenant owner compares ``roles``
    against ``['owner']`` (per ADR-0063 §2).

    Returns:
        The UUID of the primary-tenant owner. The seed-installation
        step needs the value to attribute created configurations to
        a real actor.
    """
    async with engine.begin() as conn:
        # Set the GUC so any read against a domain table evaluates RLS
        # against the primary tenant context, and so the audit trigger
        # captures the right tenant id when it fires on the user
        # insert.
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(PRIMARY_TENANT_ID)},
        )

        # ---- system tenant (idempotent) -------------------------------------
        await conn.execute(
            text(
                """
                INSERT INTO tenants (id, name, subdomain)
                VALUES (:id, :name, :subdomain)
                ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name,
                        subdomain = EXCLUDED.subdomain
                """
            ),
            {
                "id": str(SYSTEM_TENANT_ID),
                "name": _SYSTEM_TENANT_NAME,
                "subdomain": _SYSTEM_TENANT_SUBDOMAIN,
            },
        )
        _LOG.info(
            "bootstrap: system tenant present (id=%s subdomain=%r)",
            SYSTEM_TENANT_ID,
            _SYSTEM_TENANT_SUBDOMAIN,
        )

        # ---- primary tenant (idempotent on UUID, ensures name/subdomain) ----
        existing_tenant = await conn.execute(
            text("SELECT id, name, subdomain FROM tenants WHERE id = :tid"),
            {"tid": str(PRIMARY_TENANT_ID)},
        )
        tenant_row = existing_tenant.first()

        if tenant_row is None:
            await conn.execute(
                text("INSERT INTO tenants (id, name, subdomain) VALUES (:tid, :name, :subdomain)"),
                {
                    "tid": str(PRIMARY_TENANT_ID),
                    "name": _PRIMARY_TENANT_NAME,
                    "subdomain": _PRIMARY_TENANT_SUBDOMAIN,
                },
            )
            _LOG.info("bootstrap: created primary tenant %s", PRIMARY_TENANT_ID)
        else:
            # Pre-rename rows (b008) carry name "Sentinel Tenant". The
            # b012 migration already renames; bootstrap doubles as a
            # safety net so subsequent runs don't fail drift detection
            # on the older name.
            if tenant_row.name not in (_PRIMARY_TENANT_NAME, _SENTINEL_TENANT_NAME):
                raise PortfoliFlowError(
                    f"bootstrap: drift on primary tenant — name is "
                    f"{tenant_row.name!r}, expected "
                    f"{_PRIMARY_TENANT_NAME!r}. Resolve manually before "
                    "re-running."
                )
            if tenant_row.name != _PRIMARY_TENANT_NAME or (
                tenant_row.subdomain != _PRIMARY_TENANT_SUBDOMAIN
            ):
                await conn.execute(
                    text("UPDATE tenants SET name = :name, subdomain = :subdomain WHERE id = :tid"),
                    {
                        "tid": str(PRIMARY_TENANT_ID),
                        "name": _PRIMARY_TENANT_NAME,
                        "subdomain": _PRIMARY_TENANT_SUBDOMAIN,
                    },
                )
                _LOG.info("bootstrap: corrected primary tenant name/subdomain")
            else:
                _LOG.info("bootstrap: primary tenant already present (no-op)")

        # ---- user -----------------------------------------------------------
        existing_user = await conn.execute(
            text(
                "SELECT id, email, roles, is_active "
                "FROM users "
                "WHERE tenant_id = :tid AND email = :email"
            ),
            {"tid": str(SENTINEL_TENANT_ID), "email": email},
        )
        user_row = existing_user.first()

        if user_row is None:
            if password is None:
                raise ConfigurationError(
                    "bootstrap: sentinel user is missing and no password "
                    "supplied. Set SENTINEL_PASSWORD in .env or pipe one via "
                    "--password-stdin."
                )
            hashed = hash_password(password)
            inserted = await conn.execute(
                text(
                    "INSERT INTO users "
                    "(tenant_id, email, display_name, password_hash, "
                    " roles, is_active) "
                    "VALUES (:tid, :email, :display_name, :hash, "
                    "ARRAY['owner']::text[], TRUE) "
                    "RETURNING id"
                ),
                {
                    "tid": str(SENTINEL_TENANT_ID),
                    "email": email,
                    "display_name": display_name,
                    "hash": hashed,
                },
            )
            sentinel_user_id_str = str(inserted.scalar_one())
            _LOG.info("bootstrap: created sentinel user %s", email)
        else:
            # Drift detection on non-secret attributes (per ADR-0040 §4).
            # Password drift is **not** detected — the hash on disk is
            # authoritative; rotation goes through `set-password`.
            drifted: list[str] = []
            existing_roles = tuple(user_row.roles or ())
            if "owner" not in existing_roles:
                drifted.append(f"roles={existing_roles!r} (expected to include 'owner')")
            if not bool(user_row.is_active):
                drifted.append("is_active=FALSE (expected TRUE)")
            if drifted:
                raise PortfoliFlowError(
                    "bootstrap: drift detected on sentinel user "
                    f"{email!r}: {', '.join(drifted)}. "
                    "Resolve manually before re-running."
                )
            sentinel_user_id_str = str(user_row.id)
            _LOG.info("bootstrap: sentinel user already present (no-op)")

        # ---- super-admin (optional, idempotent) -----------------------------
        # Runs inside the same transaction as the tenant/owner setup.
        # If the super-admin insert fails (e.g. email validation) the
        # whole bootstrap rolls back — the same atomicity guarantee the
        # shared helpers offer everywhere else.
        super_admin_email = os.getenv("SUPER_ADMIN_EMAIL", "").strip()
        super_admin_password = os.getenv("SUPER_ADMIN_PASSWORD", "")
        super_admin_display_name = os.getenv("SUPER_ADMIN_DISPLAY_NAME", "").strip() or None
        if super_admin_email and super_admin_password:
            # Enforce the set-time password policy before any DB work: a
            # weak SUPER_ADMIN_PASSWORD fails the whole bootstrap loudly.
            # The ValidationError rolls the transaction back and the
            # command wrapper surfaces a non-zero exit (ADR-0036 §8).
            validate_password_strength(super_admin_password)
            # Switch the GUC to the system tenant — the audit trigger
            # on the user insert records app.tenant_id, and the
            # super-admin lives in the system tenant.
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(SYSTEM_TENANT_ID)},
            )
            await create_super_admin_idempotent(
                conn,
                email=super_admin_email,
                password=super_admin_password,
                actor_super_admin_id=None,  # bootstrap path
                actor_ip=None,
                actor_user_agent="cli/bootstrap",
                display_name=super_admin_display_name,
            )
            _LOG.info("bootstrap: super-admin %s present", super_admin_email)
        elif super_admin_email or super_admin_password:
            raise ConfigurationError(
                "bootstrap: SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD "
                "must both be set, or both be unset. Found only one."
            )
        else:
            _LOG.info(
                "bootstrap: no super-admin env vars set — skipping "
                "super-admin creation. Run `portfoliflow create-super-admin` "
                "separately, or set SUPER_ADMIN_EMAIL and "
                "SUPER_ADMIN_PASSWORD in .env before re-running bootstrap."
            )

    return UUID(sentinel_user_id_str)


async def _run_seed_installation(engine: AsyncEngine, sentinel_user_id: UUID) -> None:
    """Install the SAA seed templates for the sentinel tenant.

    Runs in its own tenant-scoped transaction so the seed step is
    independent of the bootstrap transaction (a seed-install failure
    never rolls back tenant / user creation). The session uses
    :func:`tenant_context` against ``SENTINEL_TENANT_ID``; ``user_id``
    is set so the audit trigger captures the sentinel user as the
    actor on the seeded rows.

    Idempotent on configuration name and asset-class code — see
    :func:`services.saa.install_seeds_for_tenant`.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        saa_service = SAAService(
            configurations=SAAConfigurationRepository(session),
            asset_classes=AssetClassRepository(session),
            inputs=SAAAssetClassInputRepository(session),
            correlations=SAACorrelationRepository(session),
        )
        await install_seeds_for_tenant(saa_service, sentinel_user_id)


# ---------------------------------------------------------------------------
# Phase-4 Investment-domain bootstrap step
# ---------------------------------------------------------------------------


async def _run_unclassified_asset_class_installation(
    engine: AsyncEngine, sentinel_user_id: UUID
) -> None:
    """Run the Phase-4 ``unclassified`` asset-class installation step.

    Wraps :func:`install_unclassified_asset_class` in its own
    tenant-scoped transaction so a failure here never rolls back
    tenant / user / SAA-seed creation.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        await install_unclassified_asset_class(AssetClassRepository(session))


# ---------------------------------------------------------------------------
# Phase-7 Default-asset-classes bootstrap step (Anlagegrenzen feature)
# ---------------------------------------------------------------------------


async def _run_default_asset_classes_installation(
    engine: AsyncEngine, sentinel_user_id: UUID
) -> None:
    """Run the Phase-7 default-asset-classes installation step.

    Wraps :func:`install_default_asset_classes` in its own tenant-
    scoped transaction so a failure here never rolls back earlier
    steps. Runs after :func:`_run_unclassified_asset_class_installation`
    so the ``unclassified`` row is always present even if the default
    catalogue is later edited.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        await install_default_asset_classes(AssetClassRepository(session))


# ---------------------------------------------------------------------------
# Phase-5a Sector-domain bootstrap step
# ---------------------------------------------------------------------------


async def _run_unclassified_sector_installation(
    engine: AsyncEngine, sentinel_user_id: UUID
) -> None:
    """Run the Phase-5a ``unclassified`` sector installation step.

    Wraps :func:`install_unclassified_sector` in its own tenant-scoped
    transaction so a failure here never rolls back earlier steps.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        await install_unclassified_sector(SectorRepository(session), sentinel_user_id)


# ---------------------------------------------------------------------------
# Phase-6 Region-model bootstrap step (ADR-0046)
# ---------------------------------------------------------------------------


async def _run_default_regions_installation(engine: AsyncEngine, sentinel_user_id: UUID) -> None:
    """Run the Phase-6 region-model installation step.

    Wraps :func:`install_default_regions` in its own tenant-scoped
    transaction so a failure here never rolls back earlier steps.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        await install_default_regions(
            RegionRepository(session),
            CountryRepository(session),
        )


# ---------------------------------------------------------------------------
# Live Data Import (#036, slice 5) bootstrap steps — system actor + schedule
# ---------------------------------------------------------------------------


async def _run_watchpoint_seed_installation(engine: AsyncEngine, sentinel_user_id: UUID) -> None:
    """Run the ADR-0116 §8 watchpoint seed step for the primary tenant.

    Wraps :func:`services.watch_desk.seeding.install_default_watchpoints_for_tenant`
    in the seed pipeline's ``(engine, user_id)`` shape; that function opens
    its own tenant-scoped transaction, so a failure here never rolls back
    earlier steps.
    """
    await install_default_watchpoints_for_tenant(engine, SENTINEL_TENANT_ID, sentinel_user_id)


async def _run_market_data_seed_installation(engine: AsyncEngine, sentinel_user_id: UUID) -> None:
    """Run the Live-Data-Import seed step (system actor + schedule).

    Wraps both installers in one tenant-scoped transaction so a failure here
    never rolls back earlier steps. The two rows are independent (the
    schedule does not reference the actor), so sharing a transaction is safe
    and keeps the paired market-data seed atomic.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        await install_market_data_system_actor(UserRepository(session))
        await install_market_data_schedule(
            MarketDataScheduleRepository(session),
            now=datetime.now(timezone.utc),
        )


async def _run_irene_schedule_seed_installation(
    engine: AsyncEngine, sentinel_user_id: UUID
) -> None:
    """Run the ADR-0119 §4 Irene schedule seed step for the primary tenant.

    Wraps :func:`install_irene_schedule` in the seed pipeline's
    ``(engine, user_id)`` shape, in its own tenant-scoped transaction so a
    failure here never rolls back earlier steps.
    """
    async with tenant_context(engine, SENTINEL_TENANT_ID, user_id=sentinel_user_id) as session:
        await install_irene_schedule(
            IreneScheduleRepository(session),
            now=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Canonical seed pipeline
# ---------------------------------------------------------------------------


# The single canonical definition of the post-bootstrap seed pipeline. Both
# the ``bootstrap`` lifecycle below and the ``reset-dev`` lifecycle in
# ``cli/reset_dev.py`` iterate this tuple, so the two commands cannot drift
# apart on what a freshly provisioned database contains.
#
# Unit tests empty the pipeline wholesale by monkeypatching this name to
# ``()`` — the seed steps need a real ``AsyncEngine`` for ``tenant_context``,
# which the fake engines in ``tests/cli/`` cannot provide.
#
# A new seed step is added HERE and nowhere else. Every step shares the
# ``(engine, sentinel_user_id)`` signature and opens its own tenant-scoped
# transaction, so a failure in one never rolls back its predecessors.
_SEED_STEPS: tuple[Callable[[AsyncEngine, UUID], Awaitable[None]], ...] = (
    _run_seed_installation,
    _run_unclassified_asset_class_installation,
    # Order is contractual: the default-asset-classes step documents that
    # it runs after the unclassified step so the ``unclassified`` row is
    # always present (see its docstring).
    _run_default_asset_classes_installation,
    _run_unclassified_sector_installation,
    _run_default_regions_installation,
    _run_market_data_seed_installation,
    _run_irene_schedule_seed_installation,
    # Last, and deliberately so: the watchpoint seed reads the tenant's
    # book to derive its fx pairs, so it wants to run after everything
    # else that might have put something in it. At bootstrap time the book
    # is empty and only the two singletons install — re-run
    # `portfoliflow seed-watchpoints` after the first workbook import
    # (ADR-0116 §8).
    _run_watchpoint_seed_installation,
)


async def _bootstrap_with_engine_lifecycle(
    email: str, password: str | None, display_name: str | None = None
) -> None:
    """Build the engine, run the bootstrap and seed steps, dispose.

    The seed steps are the canonical :data:`_SEED_STEPS` pipeline, run in
    declaration order.
    """
    engine = superuser_engine()
    try:
        sentinel_user_id = await _run_bootstrap(engine, email, password, display_name)
        for _seed_step in _SEED_STEPS:
            await _seed_step(engine, sentinel_user_id)
    finally:
        await engine.dispose()


def bootstrap_command(
    email: str | None = typer.Option(
        None,
        "--email",
        help="Sentinel email; falls back to SENTINEL_EMAIL env var.",
    ),
    password_stdin: bool = typer.Option(
        False,
        "--password-stdin",
        help=(
            "Read the password from stdin (one line). Falls back to "
            "SENTINEL_PASSWORD env var when this flag is absent."
        ),
    ),
) -> None:
    """Idempotently create the sentinel tenant and sentinel user.

    Logs each transition explicitly. Plaintext passwords never appear
    in any log line. Exits non-zero on misconfiguration or drift.
    """
    configure_logging()
    try:
        resolved_email = _resolve_email(email)
        resolved_password = _resolve_password(password_stdin)
        resolved_display_name = _resolve_display_name()
    except ConfigurationError as exc:
        _LOG.error("bootstrap: %s", exc.message)
        raise typer.Exit(code=2) from exc

    try:
        asyncio.run(
            _bootstrap_with_engine_lifecycle(
                resolved_email, resolved_password, resolved_display_name
            )
        )
    except ConfigurationError as exc:
        _LOG.error("bootstrap: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("bootstrap: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface clean exit code
        _LOG.error("bootstrap: unexpected failure: %s", exc)
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# set-password subcommand
# ---------------------------------------------------------------------------


async def _run_set_password(engine: AsyncEngine, email: str, password: str) -> None:
    """Rotate the password for an existing user in the sentinel tenant.

    Updates the ``password_hash`` column and deletes every active
    session for the user (defensive: a credential change must not
    leave prior sessions valid). Both writes happen in a single
    superuser-bound transaction.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(SENTINEL_TENANT_ID)},
        )
        result = await conn.execute(
            text("SELECT id FROM users WHERE tenant_id = :tid AND email = :email"),
            {"tid": str(SENTINEL_TENANT_ID), "email": email},
        )
        row = result.first()
        if row is None:
            raise PortfoliFlowError(
                f"set-password: no user with email {email!r} in sentinel tenant"
            )
        user_id: UUID = row.id  # type: ignore[assignment]

        hashed = hash_password(password)
        await conn.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hashed, "id": str(user_id)},
        )
        # Invalidate every active session for the user.
        await conn.execute(
            text("DELETE FROM sessions WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )

    _LOG.info(
        "set-password: rotated password hash for user %s (%s); all active sessions invalidated",
        email,
        user_id,
    )


async def _set_password_with_engine_lifecycle(email: str, password: str) -> None:
    engine = superuser_engine()
    try:
        await _run_set_password(engine, email, password)
    finally:
        await engine.dispose()


def set_password_command(
    email: str | None = typer.Option(
        None,
        "--email",
        help="Target user's email; falls back to SENTINEL_EMAIL env var.",
    ),
    password_stdin: bool = typer.Option(
        True,
        "--password-stdin/--no-password-stdin",
        help="Read the new password from stdin (default).",
    ),
) -> None:
    """Rotate a user's password and invalidate all active sessions.

    Sub-stream 2b activates the full rotation path: the
    ``password_hash`` column is updated and every active session for
    the user is deleted (per OWASP session-management guidance).
    """
    configure_logging()
    try:
        resolved_email = _resolve_email(email)
        resolved_password = _resolve_password(password_stdin)
        if resolved_password is None:
            raise ConfigurationError(
                "set-password: no password supplied. Pipe one on stdin via "
                "--password-stdin or set SENTINEL_PASSWORD."
            )
        # Enforce the set-time password policy on rotation (never on
        # verify): a weak new password is rejected before any DB work.
        validate_password_strength(resolved_password)
    except (ConfigurationError, ValidationError) as exc:
        _LOG.error("set-password: %s", exc.message)
        raise typer.Exit(code=2) from exc

    try:
        asyncio.run(_set_password_with_engine_lifecycle(resolved_email, resolved_password))
    except ConfigurationError as exc:
        _LOG.error("set-password: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("set-password: %s", exc.message)
        raise typer.Exit(code=3) from exc
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - surface clean exit code
        _LOG.error("set-password: unexpected failure: %s", exc)
        raise typer.Exit(code=1) from exc
