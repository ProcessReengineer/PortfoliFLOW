# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Per-tenant default catalogues and schedules, installed into one tenant.

These installers give every tenant the same baseline (ADR-0077): the
``unclassified`` fallback asset class (ADR-0043) and sector (ADR-0045 §2),
the default asset-class catalogue, the region partition (ADR-0046), the
market-data system actor and its disabled schedule (ADR-0093, ADR-0125 §3),
and the enabled Irene schedule (ADR-0119 §4). Each one takes repositories
bound to a tenant-scoped session and is idempotent, so re-running it is a
no-op.

Two callers share them: ``portfoliflow bootstrap`` (``cli/bootstrap.py``)
for the primary tenant, and tenant creation
(``services/super_admin/operations.py``) for every tenant created later.
They live in ``services/`` so that the second caller does not import the
CLI; ``tests/regression/test_layer_imports.py`` keeps it that way.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from pathlib import Path
from uuid import UUID

from core.exceptions import PortfoliFlowError
from core.repositories import (
    AssetClassRepository,
    RegionRepository,
    SectorRepository,
    UserRepository,
)
from core.repositories.country_repository import CountryRepository
from core.repositories.irene_schedule_repository import IreneScheduleRepository
from core.repositories.market_data_schedule_repository import (
    MarketDataScheduleRepository,
)
from services.investments.live_refresh import (
    MARKET_DATA_SYSTEM_ACTOR_DISPLAY_NAME,
    MARKET_DATA_SYSTEM_ACTOR_EMAIL,
)
from services.irene.scheduling import compute_next_due_at
from services.password_hashing import hash_password

_LOG = logging.getLogger(__name__)


_UNCLASSIFIED_ASSET_CLASS_CODE: str = "unclassified"
_UNCLASSIFIED_ASSET_CLASS_DISPLAY_NAME: str = "Unclassified"
_UNCLASSIFIED_ASSET_CLASS_DESCRIPTION: str = (
    "Fallback bucket for Excel imports without an explicit Asset Class assignment."
)


async def install_unclassified_asset_class(
    asset_classes: AssetClassRepository,
) -> None:
    """Install the ``"unclassified"`` fallback asset class for the active tenant.

    Per ADR-0043 §1, every tenant carries an asset class with code
    ``"unclassified"`` so the Excel-import path (sub-stream 4c) can
    route an investment whose ``Asset Class`` cell is empty to a
    well-defined bucket instead of failing the whole import. In
    ordinary operation at p&p the field is always populated and
    this fallback stays dormant; the row exists as a safety net.

    The function is **idempotent on the asset-class code**: a
    pre-existing ``"unclassified"`` asset class is left untouched.

    Args:
        asset_classes: Asset-class repository bound to a tenant-
            scoped session. The active tenant is read from
            ``app.tenant_id`` by the underlying repository.
    """
    existing = await asset_classes.get_by_code(_UNCLASSIFIED_ASSET_CLASS_CODE)
    if existing is not None:
        _LOG.info("bootstrap: 'unclassified' asset class already present (no-op)")
        return
    created = await asset_classes.create(
        code=_UNCLASSIFIED_ASSET_CLASS_CODE,
        display_name=_UNCLASSIFIED_ASSET_CLASS_DISPLAY_NAME,
        description=_UNCLASSIFIED_ASSET_CLASS_DESCRIPTION,
    )
    _LOG.info("bootstrap: created 'unclassified' asset class (%s)", created.id)


_DEFAULT_ASSET_CLASSES_FIXTURE_PATH: Path = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "data_normalization"
    / "fixtures"
    / "default_asset_classes.json"
)


def _load_default_asset_classes_fixture() -> list[dict[str, object]]:
    """Read the default-asset-classes fixture used by ``install_default_asset_classes``.

    Hard error on a missing file (packaging fault). The fixture
    coexists with the ``unclassified`` row installed by
    :func:`install_unclassified_asset_class`; the two installations
    are independent.
    """
    if not _DEFAULT_ASSET_CLASSES_FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"Default-asset-classes fixture not found at "
            f"{_DEFAULT_ASSET_CLASSES_FIXTURE_PATH!s}. The Phase-7 bootstrap "
            "step requires the JSON seed file shipped under "
            "services/data_normalization/fixtures/."
        )
    with _DEFAULT_ASSET_CLASSES_FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, list) or not payload:
        raise ValueError(
            f"Default-asset-classes fixture at "
            f"{_DEFAULT_ASSET_CLASSES_FIXTURE_PATH!s} is empty or malformed."
        )
    return payload


async def install_default_asset_classes(
    asset_classes: AssetClassRepository,
) -> None:
    """Install the canonical default asset-class catalogue for the active tenant.

    Per the Phase-7 Anlagegrenzen-Überwachung data layer, every tenant
    is seeded with a controlled vocabulary of asset-class codes
    (``"equities"``, ``"private_equity"``, ``"real_estate"``, …) that
    the SAA limit-set sheets reference as ``class_key`` values. The
    fixture is the single source of truth and ships under
    ``services/data_normalization/fixtures/default_asset_classes.json``.

    Runs **alongside** :func:`install_unclassified_asset_class`; the
    unclassified fallback row stays as the Excel-import safety net,
    the 12 default rows fill in the operational catalogue. Existing
    rows are left untouched — the function is idempotent on the
    asset-class code.

    Args:
        asset_classes: Asset-class repository bound to a tenant-scoped
            session. The active tenant is read from ``app.tenant_id``
            by the underlying repository.
    """
    entries = _load_default_asset_classes_fixture()
    for entry in entries:
        code = str(entry["code"])
        existing = await asset_classes.get_by_code(code)
        if existing is not None:
            _LOG.info(
                "bootstrap: default asset class %r already present (no-op)",
                code,
            )
            continue
        description = entry.get("description")
        created = await asset_classes.create(
            code=code,
            display_name=str(entry["display_name"]),
            description=(str(description) if isinstance(description, str) else None),
        )
        _LOG.info("bootstrap: created default asset class %r (%s)", code, created.id)


_UNCLASSIFIED_SECTOR_CODE: str = "unclassified"
_UNCLASSIFIED_SECTOR_DISPLAY_NAME: str = "Unclassified"


async def install_unclassified_sector(
    sectors: SectorRepository,
    created_by: UUID,
) -> None:
    """Install the ``"unclassified"`` fallback sector for the active tenant.

    Per ADR-0045 §2, every tenant carries a sector with code
    ``"unclassified"`` so the Excel-import path can route an
    investment whose ``Sector`` cell is empty to a well-defined
    bucket. Mirrors the asset-class bootstrap pattern from ADR-0043.

    The function is **idempotent on the sector code**: a pre-existing
    ``"unclassified"`` sector is left untouched.

    Args:
        sectors: Sector repository bound to a tenant-scoped session.
            The active tenant is read from ``app.tenant_id`` by the
            underlying repository.
        created_by: UUID of the user attributable for the write.
    """
    existing = await sectors.get_by_code(_UNCLASSIFIED_SECTOR_CODE)
    if existing is not None:
        _LOG.info("bootstrap: 'unclassified' sector already present (no-op)")
        return
    created = await sectors.create(
        code=_UNCLASSIFIED_SECTOR_CODE,
        display_name=_UNCLASSIFIED_SECTOR_DISPLAY_NAME,
        created_by=created_by,
    )
    _LOG.info("bootstrap: created 'unclassified' sector (%s)", created.id)


# Canonical region catalogue per ADR-0046 M1 (strict partition).
# Each region is a disjoint group of ISO 3166-1 alpha-2 country codes.
# The ``sort_order`` reflects the rendering order in the UI: Europe
# first, then North America, then the rest. Edits to this list require
# an ADR-0046 revision-history entry.
_DEFAULT_REGIONS: tuple[dict[str, object], ...] = (
    {
        "code": "dach",
        "display_name": "DACH",
        "description": "Germany, Austria, Switzerland, Liechtenstein.",
        "sort_order": 10,
        "iso_codes": ("DE", "AT", "CH", "LI"),
    },
    {
        "code": "uk_ireland",
        "display_name": "UK & Ireland",
        "description": "United Kingdom and Ireland.",
        "sort_order": 20,
        "iso_codes": ("GB", "IE"),
    },
    {
        "code": "nordics",
        "display_name": "Nordics",
        "description": "Denmark, Sweden, Norway, Finland, Iceland.",
        "sort_order": 30,
        "iso_codes": ("DK", "SE", "NO", "FI", "IS"),
    },
    {
        "code": "western_europe_other",
        "display_name": "Western Europe ex-DACH/UK/Nordics",
        "description": (
            "Western European markets outside the DACH bloc, UK & Ireland and the Nordics."
        ),
        "sort_order": 40,
        "iso_codes": (
            "FR",
            "IT",
            "ES",
            "BE",
            "NL",
            "LU",
            "PT",
            "MC",
            "MT",
            "CY",
            "GR",
        ),
    },
    {
        "code": "cee",
        "display_name": "Central & Eastern Europe",
        "description": (
            "Central and Eastern European markets. Turkey is bucketed "
            "to MEA following MSCI convention."
        ),
        "sort_order": 50,
        "iso_codes": (
            "PL",
            "CZ",
            "HU",
            "SK",
            "RO",
            "BG",
            "HR",
            "SI",
            "BA",
            "RS",
            "ME",
            "MK",
            "AL",
            "EE",
            "LV",
            "LT",
            "UA",
            "BY",
            "MD",
            "XK",
        ),
    },
    {
        "code": "north_america_usa",
        "display_name": "North America — USA",
        "description": "United States of America.",
        "sort_order": 60,
        "iso_codes": ("US",),
    },
    {
        "code": "north_america_canada",
        "display_name": "North America — Canada",
        "description": "Canada.",
        "sort_order": 70,
        "iso_codes": ("CA",),
    },
    {
        "code": "latin_america",
        "display_name": "Latin America",
        "description": "Latin American and Caribbean markets.",
        "sort_order": 80,
        "iso_codes": (
            "BR",
            "MX",
            "AR",
            "CL",
            "CO",
            "PE",
            "UY",
            "EC",
            "BO",
            "PY",
            "VE",
            "CR",
            "PA",
            "DO",
            "GT",
            "HN",
            "NI",
            "SV",
            "CU",
            "JM",
            "TT",
            "HT",
        ),
    },
    {
        "code": "apac_developed",
        "display_name": "Asia-Pacific Developed",
        "description": (
            "Developed Asia-Pacific markets. Greater-China markets "
            "(HK, MO, TW) are bucketed separately under "
            "``greater_china``."
        ),
        "sort_order": 90,
        "iso_codes": ("JP", "KR", "AU", "NZ", "SG"),
    },
    {
        "code": "greater_china",
        "display_name": "Greater China",
        "description": ("Mainland China, Hong Kong, Macau, and Taiwan."),
        "sort_order": 100,
        "iso_codes": ("CN", "HK", "TW", "MO"),
    },
    {
        "code": "asia_emerging",
        "display_name": "Asia Emerging",
        "description": "Emerging Asian markets ex-Greater China.",
        "sort_order": 110,
        "iso_codes": (
            "IN",
            "ID",
            "TH",
            "MY",
            "PH",
            "VN",
            "PK",
            "BD",
            "LK",
            "KH",
            "LA",
            "MM",
            "MN",
            "NP",
        ),
    },
    {
        "code": "mea",
        "display_name": "Middle East & Africa",
        "description": ("Middle East and African markets, including Turkey."),
        "sort_order": 120,
        "iso_codes": (
            "SA",
            "AE",
            "IL",
            "EG",
            "QA",
            "KW",
            "OM",
            "BH",
            "JO",
            "LB",
            "TR",
            "ZA",
            "NG",
            "KE",
            "MA",
            "TN",
            "DZ",
            "ET",
            "GH",
            "CI",
            "SN",
            "TZ",
            "UG",
            "AO",
            "ZM",
            "ZW",
        ),
    },
)


async def install_default_regions(
    regions: RegionRepository,
    countries: CountryRepository,
) -> None:
    """Install the canonical region catalogue and memberships for the tenant.

    Per ADR-0046, each tenant carries a controlled, pre-seeded region
    catalogue. The Excel import path resolves Excel region labels
    (``"DACH"``, ``"Asia Emerging"``, …) against this catalogue
    strictly: unknown labels raise a hard import error rather than
    being auto-created. New regions therefore require a deliberate
    bootstrap update with an ADR revision-history entry.

    The function is **idempotent on the region code and on the
    (region, country) pair**: a pre-existing region row is left
    untouched, and a pre-existing membership row is skipped. Re-running
    the bootstrap after adding a new region appends only the new rows.

    Args:
        regions: Region repository bound to a tenant-scoped session.
        countries: Country repository bound to the same session (used
            only to validate ISO codes against the stammtabelle so a
            seed typo fails loudly rather than silently dropping a
            membership).
    """
    existing = await regions.list_all()
    existing_by_code: dict[str, UUID] = {r.code: r.id for r in existing}

    for spec in _DEFAULT_REGIONS:
        code = str(spec["code"])
        if code in existing_by_code:
            region_id = existing_by_code[code]
            _LOG.info("bootstrap: region %r already present (no-op)", code)
        else:
            created = await regions.create(
                code=code,
                display_name=str(spec["display_name"]),
                description=str(spec["description"]),
                sort_order=int(spec["sort_order"]),  # type: ignore[arg-type]
            )
            region_id = created.id
            _LOG.info("bootstrap: created region %r (%s)", code, region_id)

        existing_memberships = await regions.list_memberships_by_region(region_id)
        attached: set[str] = {m.country_iso_code.upper() for m in existing_memberships}
        iso_codes: tuple[str, ...] = spec["iso_codes"]  # type: ignore[assignment]
        for iso in iso_codes:
            normalised = iso.strip().upper()
            if normalised in attached:
                continue
            country = await countries.get_by_iso_code(normalised)
            if country is None:
                raise PortfoliFlowError(
                    f"bootstrap: ISO code {normalised!r} for region "
                    f"{code!r} is not present in the countries "
                    "stammtabelle. Update the ISO fixture before "
                    "re-seeding regions."
                )
            await regions.add_membership(region_id, normalised)
            attached.add(normalised)
            _LOG.info("bootstrap: attached %s to region %r", normalised, code)


# Seed defaults for the tenant's disabled market-data schedule. The cadence
# is the finest the vocabulary offers (ADR-0125 §1/§3) so that opting a
# tenant in is one checkbox rather than a cadence decision; the anchor hour
# is 0 because a 15-minute grid runs from the full hour and the anchor is
# inert. The timezone is a sensible German-deployment default an owner
# adjusts from the Admin surface. ``enabled`` is FALSE at seed time
# (ADR-0093, unchanged) so no tenant silently starts fetching.
_MARKET_DATA_DEFAULT_CADENCE: str = "every_15m"
_MARKET_DATA_DEFAULT_HOUR: int = 0
_MARKET_DATA_DEFAULT_TIMEZONE: str = "Europe/Berlin"


# Seed defaults for the tenant's Irene schedule (ADR-0119 §4). The morning
# anchor sits well inside the market-data refresh grid above, so the first
# beat of the day reads freshly imported prices. Unlike the market-data row
# this one is seeded ENABLED — see :func:`install_irene_schedule` for why
# that is safe.
_IRENE_DEFAULT_CADENCE: str = "daily"
_IRENE_DEFAULT_HOUR: int = 8
_IRENE_DEFAULT_TIMEZONE: str = "Europe/Berlin"


async def install_market_data_system_actor(users: UserRepository) -> None:
    """Install the per-tenant market-data system actor (ADR-0093 §0.1).

    Live-import writes have no human user; a dedicated system actor
    satisfies the ``created_by`` audit FK on every ingested row. It is
    seeded ``is_active = False`` so it can **never** authenticate, with the
    recognisable identity :data:`MARKET_DATA_SYSTEM_ACTOR_DISPLAY_NAME` and a
    clearly-synthetic ``.invalid`` email.

    Two schema constraints shape the row (both intentional deviations from
    the operator's "empty roles" shorthand, which the ``users`` CHECKs
    forbid): the roles array must be non-empty, so the actor carries the
    least-privileged single role ``auditor`` (read-only, and inert anyway
    because the account is inactive); and every user must be authenticatable
    (``password_hash`` non-NULL OR an OIDC pair), so the actor is given a
    hash of a fresh random secret that is stored nowhere — an unusable,
    locked credential.

    Idempotent on email: a pre-existing actor is left untouched, so the
    installer composes cleanly with re-runs (the ADR-0077 backfill mechanism
    — re-running bootstrap / create-tenant).

    Args:
        users: User repository bound to a tenant-scoped session. The active
            tenant is read from ``app.tenant_id`` by the repository.
    """
    existing = await users.get_by_email(MARKET_DATA_SYSTEM_ACTOR_EMAIL)
    if existing is not None:
        _LOG.info("bootstrap: market-data system actor already present (no-op)")
        return
    # Unusable, locked credential: a hash of a random secret satisfies the
    # authenticatable CHECK; is_active=False guarantees no login is possible.
    await users.create(
        email=MARKET_DATA_SYSTEM_ACTOR_EMAIL,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        roles=("auditor",),
        is_active=False,
        display_name=MARKET_DATA_SYSTEM_ACTOR_DISPLAY_NAME,
    )
    _LOG.info("bootstrap: created market-data system actor")


async def install_market_data_schedule(
    schedules: MarketDataScheduleRepository, *, now: datetime
) -> None:
    """Install the tenant's market-data schedule row, disabled (ADR-0093).

    Every tenant carries a ``market_data_schedule`` row so the Admin surface
    and the tick have a stable target, but it lands ``enabled = False``: a
    freshly provisioned tenant does not silently start fetching from external
    providers. ADR-0125 §3 leaves that untouched and changes only the *value*
    the row carries: the cadence is seeded ``every_15m`` with
    ``preferred_hour = 0`` (ADR-0125 §3), so an owner opting a tenant in
    ticks one checkbox and saves rather than first having to pick an
    interval. ``preferred_hour = 0`` is the honest value for an anchor that
    is inert at a sub-hourly cadence — the quarter-hour grid runs from the
    full hour regardless. ``next_due_at`` is set to ``now`` — immaterial
    while disabled (the due read gates on ``enabled AND next_due_at <=
    now()``); the web "Save cadence" / "Refresh now" actions recompute it
    when the owner opts in.

    Idempotent: a pre-existing tenant-level schedule is left untouched. There
    is deliberately **no backfill** of the new cadence onto existing rows
    (ADR-0125 §3): a row still carrying ``daily`` cannot be told apart from
    one an owner deliberately left at ``daily``, so existing tenants keep
    what they have and are switched in Admin.

    Args:
        schedules: Market-data schedule repository bound to a tenant-scoped
            session.
        now: The current instant (timezone-aware UTC) used as the placeholder
            ``next_due_at``.
    """
    if await schedules.get_for_tenant() is not None:
        _LOG.info("bootstrap: market-data schedule already present (no-op)")
        return
    await schedules.upsert_tenant_schedule(
        cadence=_MARKET_DATA_DEFAULT_CADENCE,
        preferred_hour=_MARKET_DATA_DEFAULT_HOUR,
        timezone=_MARKET_DATA_DEFAULT_TIMEZONE,
        enabled=False,
        next_due_at=now,
    )
    _LOG.info(
        "bootstrap: created market-data schedule (cadence=%s hour=%s, disabled)",
        _MARKET_DATA_DEFAULT_CADENCE,
        _MARKET_DATA_DEFAULT_HOUR,
    )


async def install_irene_schedule(schedules: IreneScheduleRepository, *, now: datetime) -> None:
    """Install the tenant's Irene schedule row, enabled (ADR-0119 §4).

    Until this seed existed the only writer of ``irene_schedule`` was the
    Watch Desk cadence-save endpoint, so a fresh tenant saw a Watch Desk with
    no cadence, no "Request analysis now" affordance and no beats until an
    operator saved the panel once — the area looked dead out of the box.

    Seeded **enabled**, deliberately asymmetric to the market-data row above,
    and the asymmetry is reasoned rather than accidental: an enabled
    market-data schedule would fetch from external providers immediately and
    silently, whereas the Irene domain sits behind the tick scheduler's
    credential gate — without a resolvable LLM credential the domain is
    skipped quietly per tick. Enabling costs nothing until the tenant
    configures credentials, and the Watch Desk is alive from the first render.

    ``next_due_at`` is **computed**, not a ``now`` placeholder: that shortcut
    belongs to the disabled market-data row, whose due read can never fire
    while ``enabled`` is FALSE. This row is live, so it follows the
    save-endpoint rule and computes the value before writing it.

    Idempotent: a pre-existing tenant-level schedule is left untouched, so a
    tenant that has already saved a cadence keeps it across a re-seed.

    Args:
        schedules: Irene schedule repository bound to a tenant-scoped session.
        now: The current instant (timezone-aware UTC) the first
            ``next_due_at`` is computed from.
    """
    if await schedules.get_for_tenant() is not None:
        _LOG.info("bootstrap: irene schedule already present (no-op)")
        return
    await schedules.upsert_tenant_schedule(
        cadence=_IRENE_DEFAULT_CADENCE,
        preferred_hour=_IRENE_DEFAULT_HOUR,
        timezone=_IRENE_DEFAULT_TIMEZONE,
        enabled=True,
        next_due_at=compute_next_due_at(
            now,
            _IRENE_DEFAULT_CADENCE,
            _IRENE_DEFAULT_HOUR,
            _IRENE_DEFAULT_TIMEZONE,
        ),
    )
    _LOG.info("bootstrap: created irene schedule (enabled)")
