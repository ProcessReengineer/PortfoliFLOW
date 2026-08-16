# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Seed templates for the Strategic Asset Allocation domain.

Three pre-populated SAA configurations are installed for the
sentinel tenant during ``portfoliflow bootstrap`` so the user has
realistic starting points without having to enter a 7×7
correlation matrix by hand. The data is copied verbatim from the
PyQt6 reference (``gui/widgets/saa_widget.py``) — Phase 3 reuses the
same canonical numbers so a side-by-side acceptance comparison in
sub-stream 3d is meaningful.

The templates are *ordinary* configurations once installed: they
can be edited, renamed, deleted, or deactivated. The seed step is
idempotent on configuration name — re-running ``bootstrap`` does
not duplicate seeds, but it does not restore a deleted seed
either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from services.saa.saa_service import SAAService
from services.saa.validation import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
)

_LOG = logging.getLogger("portfoliflow.cli")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedAssetClass:
    """One asset-class entry inside a seed configuration.

    All numeric values are stored as percentages — matching the
    PyQt6 widget's UI convention. Conversion to decimals (the format
    the database uses) happens at install time.
    """

    code: str
    display_name: str
    expected_return_pct: float
    volatility_pct: float
    min_weight_pct: float
    max_weight_pct: float


@dataclass(frozen=True)
class SeedConfiguration:
    """One full seed template."""

    name: str
    risk_free_rate_pct: float
    n_frontier_points: int
    asset_classes: list[SeedAssetClass]
    # Keyed on (code_a, code_b) pairs; values are decimals already
    # (correlations don't carry a percent convention). The pair
    # order here is illustrative — the install path normalises.
    correlations: dict[tuple[str, str], float]
    is_active: bool = False


# ---------------------------------------------------------------------------
# Template 1 — Conservative Multi-Strategy (7 asset classes)
# ---------------------------------------------------------------------------


SEED_CONSERVATIVE = SeedConfiguration(
    name="Conservative Multi-Strategy",
    risk_free_rate_pct=2.50,
    n_frontier_points=100,
    asset_classes=[
        SeedAssetClass(
            code="gov_bonds_dm",
            display_name="Government Bonds DM",
            expected_return_pct=3.50,
            volatility_pct=5.50,
            min_weight_pct=10.0,
            max_weight_pct=35.0,
        ),
        SeedAssetClass(
            code="ig_credit",
            display_name="Investment Grade Credit",
            expected_return_pct=4.50,
            volatility_pct=7.00,
            min_weight_pct=10.0,
            max_weight_pct=30.0,
        ),
        SeedAssetClass(
            code="hf_multi_strat",
            display_name="Hedge Funds Multi-Strat",
            expected_return_pct=5.50,
            volatility_pct=6.50,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="re_core",
            display_name="Real Estate Core",
            expected_return_pct=6.00,
            volatility_pct=10.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="infra_core",
            display_name="Infrastructure Core",
            expected_return_pct=7.00,
            volatility_pct=11.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="equities_dm",
            display_name="Listed Equities DM",
            expected_return_pct=7.50,
            volatility_pct=15.50,
            min_weight_pct=5.0,
            max_weight_pct=25.0,
        ),
        SeedAssetClass(
            code="gold",
            display_name="Gold",
            expected_return_pct=3.00,
            volatility_pct=14.00,
            min_weight_pct=0.0,
            max_weight_pct=10.0,
        ),
    ],
    correlations={
        ("gov_bonds_dm", "ig_credit"): 0.65,
        ("gov_bonds_dm", "hf_multi_strat"): 0.10,
        ("gov_bonds_dm", "re_core"): 0.20,
        ("gov_bonds_dm", "infra_core"): 0.30,
        ("gov_bonds_dm", "equities_dm"): -0.10,
        ("gov_bonds_dm", "gold"): 0.10,
        ("ig_credit", "hf_multi_strat"): 0.30,
        ("ig_credit", "re_core"): 0.40,
        ("ig_credit", "infra_core"): 0.45,
        ("ig_credit", "equities_dm"): 0.30,
        ("ig_credit", "gold"): 0.05,
        ("hf_multi_strat", "re_core"): 0.35,
        ("hf_multi_strat", "infra_core"): 0.40,
        ("hf_multi_strat", "equities_dm"): 0.55,
        ("hf_multi_strat", "gold"): 0.10,
        ("re_core", "infra_core"): 0.55,
        ("re_core", "equities_dm"): 0.45,
        ("re_core", "gold"): 0.15,
        ("infra_core", "equities_dm"): 0.40,
        ("infra_core", "gold"): 0.10,
        ("equities_dm", "gold"): 0.00,
    },
    is_active=True,  # the default-active seed
)


# ---------------------------------------------------------------------------
# Template 2 — Growth Private Markets (8 asset classes)
# ---------------------------------------------------------------------------


SEED_GROWTH_PM = SeedConfiguration(
    name="Growth Private Markets",
    risk_free_rate_pct=3.00,
    n_frontier_points=120,
    asset_classes=[
        SeedAssetClass(
            code="buyout_pe",
            display_name="Buyout PE",
            expected_return_pct=12.00,
            volatility_pct=16.00,
            min_weight_pct=15.0,
            max_weight_pct=35.0,
        ),
        SeedAssetClass(
            code="growth_equity",
            display_name="Growth Equity",
            expected_return_pct=13.50,
            volatility_pct=19.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="venture_capital",
            display_name="Venture Capital",
            expected_return_pct=15.00,
            volatility_pct=28.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="private_credit",
            display_name="Private Credit",
            expected_return_pct=8.50,
            volatility_pct=9.00,
            min_weight_pct=10.0,
            max_weight_pct=25.0,
        ),
        SeedAssetClass(
            code="pe_secondaries",
            display_name="PE Secondaries",
            expected_return_pct=11.00,
            volatility_pct=13.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="equities_global",
            display_name="Listed Equities Global",
            expected_return_pct=8.00,
            volatility_pct=16.00,
            min_weight_pct=5.0,
            max_weight_pct=25.0,
        ),
        SeedAssetClass(
            code="equities_em",
            display_name="Listed Equities EM",
            expected_return_pct=9.00,
            volatility_pct=22.00,
            min_weight_pct=0.0,
            max_weight_pct=15.0,
        ),
        SeedAssetClass(
            code="cash",
            display_name="Cash",
            expected_return_pct=2.50,
            volatility_pct=0.50,
            min_weight_pct=2.0,
            max_weight_pct=10.0,
        ),
    ],
    correlations={
        ("buyout_pe", "growth_equity"): 0.75,
        ("buyout_pe", "venture_capital"): 0.55,
        ("buyout_pe", "private_credit"): 0.35,
        ("buyout_pe", "pe_secondaries"): 0.85,
        ("buyout_pe", "equities_global"): 0.65,
        ("buyout_pe", "equities_em"): 0.50,
        ("buyout_pe", "cash"): 0.00,
        ("growth_equity", "venture_capital"): 0.70,
        ("growth_equity", "private_credit"): 0.30,
        ("growth_equity", "pe_secondaries"): 0.65,
        ("growth_equity", "equities_global"): 0.60,
        ("growth_equity", "equities_em"): 0.50,
        ("growth_equity", "cash"): 0.00,
        ("venture_capital", "private_credit"): 0.20,
        ("venture_capital", "pe_secondaries"): 0.45,
        ("venture_capital", "equities_global"): 0.50,
        ("venture_capital", "equities_em"): 0.45,
        ("venture_capital", "cash"): 0.00,
        ("private_credit", "pe_secondaries"): 0.40,
        ("private_credit", "equities_global"): 0.30,
        ("private_credit", "equities_em"): 0.30,
        ("private_credit", "cash"): 0.05,
        ("pe_secondaries", "equities_global"): 0.55,
        ("pe_secondaries", "equities_em"): 0.45,
        ("pe_secondaries", "cash"): 0.00,
        ("equities_global", "equities_em"): 0.70,
        ("equities_global", "cash"): 0.00,
        ("equities_em", "cash"): 0.00,
    },
    is_active=False,
)


# ---------------------------------------------------------------------------
# Template 3 — Balanced Institutional (6 asset classes)
# ---------------------------------------------------------------------------


SEED_BALANCED = SeedConfiguration(
    name="Balanced Institutional",
    risk_free_rate_pct=2.75,
    n_frontier_points=100,
    asset_classes=[
        SeedAssetClass(
            code="equities_dm",
            display_name="Listed Equities DM",
            expected_return_pct=7.50,
            volatility_pct=15.50,
            min_weight_pct=20.0,
            max_weight_pct=45.0,
        ),
        SeedAssetClass(
            code="equities_em",
            display_name="Listed Equities EM",
            expected_return_pct=9.00,
            volatility_pct=22.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
        SeedAssetClass(
            code="ig_credit",
            display_name="Investment Grade Credit",
            expected_return_pct=4.50,
            volatility_pct=7.00,
            min_weight_pct=10.0,
            max_weight_pct=30.0,
        ),
        SeedAssetClass(
            code="hy_credit",
            display_name="High Yield Credit",
            expected_return_pct=6.50,
            volatility_pct=10.00,
            min_weight_pct=5.0,
            max_weight_pct=15.0,
        ),
        SeedAssetClass(
            code="private_equity",
            display_name="Private Equity",
            expected_return_pct=11.00,
            volatility_pct=16.00,
            min_weight_pct=10.0,
            max_weight_pct=25.0,
        ),
        SeedAssetClass(
            code="real_estate",
            display_name="Real Estate",
            expected_return_pct=6.50,
            volatility_pct=12.00,
            min_weight_pct=5.0,
            max_weight_pct=20.0,
        ),
    ],
    correlations={
        ("equities_dm", "equities_em"): 0.75,
        ("equities_dm", "ig_credit"): 0.30,
        ("equities_dm", "hy_credit"): 0.55,
        ("equities_dm", "private_equity"): 0.70,
        ("equities_dm", "real_estate"): 0.45,
        ("equities_em", "ig_credit"): 0.25,
        ("equities_em", "hy_credit"): 0.55,
        ("equities_em", "private_equity"): 0.55,
        ("equities_em", "real_estate"): 0.40,
        ("ig_credit", "hy_credit"): 0.50,
        ("ig_credit", "private_equity"): 0.30,
        ("ig_credit", "real_estate"): 0.35,
        ("hy_credit", "private_equity"): 0.55,
        ("hy_credit", "real_estate"): 0.40,
        ("private_equity", "real_estate"): 0.50,
    },
    is_active=False,
)


_ALL_SEEDS: tuple[SeedConfiguration, ...] = (
    SEED_CONSERVATIVE,
    SEED_GROWTH_PM,
    SEED_BALANCED,
)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


async def install_seeds_for_tenant(
    saa_service: SAAService,
    creator_user_id: UUID,
) -> None:
    """Install the three SAA seed templates for the active tenant.

    The function is idempotent: existing asset classes (matched by
    ``code``) and existing configurations (matched by ``name``) are
    preserved. Only missing entries are created.

    The active flag is honoured only when no other configuration is
    active in the tenant — this prevents the seed installer from
    silently swapping an operator-curated active configuration for
    the seeded default.

    Args:
        saa_service: The SAA service bound to a tenant-scoped session.
            All writes happen through this service so RLS evaluates
            on every statement.
        creator_user_id: UUID of the user attributed as the creator
            of the seeded configurations (typically the sentinel
            user).
    """
    # Step 1 — install asset classes, deduplicated across all seeds by
    # ``code``. The PyQt6 templates share several classes (e.g. both
    # Conservative and Balanced use Listed Equities DM), so the
    # cross-seed lookup avoids creating duplicates.
    existing_asset_classes_by_code = {ac.code: ac for ac in await saa_service.list_asset_classes()}
    code_to_id: dict[str, UUID] = dict(
        (code, ac.id) for code, ac in existing_asset_classes_by_code.items()
    )
    for seed in _ALL_SEEDS:
        for ac in seed.asset_classes:
            if ac.code in code_to_id:
                continue
            created = await saa_service.create_asset_class(
                code=ac.code,
                display_name=ac.display_name,
            )
            code_to_id[ac.code] = created.id
            _LOG.info(
                "seed-saa: created asset class %r (%s)",
                ac.code,
                created.id,
            )

    # Step 2 — install each configuration if absent.
    existing_configs = {c.name: c for c in await saa_service.list_configurations()}
    any_active = any(c.is_active for c in existing_configs.values())

    for seed in _ALL_SEEDS:
        if seed.name in existing_configs:
            _LOG.info(
                "seed-saa: configuration %r already present — skipped",
                seed.name,
            )
            continue

        config = await saa_service.create_configuration(
            name=seed.name,
            risk_free_rate=seed.risk_free_rate_pct / 100.0,
            n_frontier_points=seed.n_frontier_points,
            created_by=creator_user_id,
        )
        _LOG.info("seed-saa: created configuration %r (%s)", seed.name, config.id)

        input_specs = [
            SAAAssetClassInputSpec(
                asset_class_id=code_to_id[ac.code],
                expected_return=ac.expected_return_pct / 100.0,
                volatility=ac.volatility_pct / 100.0,
                min_weight=ac.min_weight_pct / 100.0,
                max_weight=ac.max_weight_pct / 100.0,
            )
            for ac in seed.asset_classes
        ]
        correlation_specs = [
            SAACorrelationSpec(
                asset_class_a_id=code_to_id[code_a],
                asset_class_b_id=code_to_id[code_b],
                correlation=value,
            )
            for (code_a, code_b), value in seed.correlations.items()
        ]
        await saa_service.save_inputs_and_correlations(config.id, input_specs, correlation_specs)

        if seed.is_active and not any_active:
            await saa_service.activate_configuration(config.id)
            any_active = True
            _LOG.info(
                "seed-saa: activated configuration %r as the tenant default",
                seed.name,
            )
