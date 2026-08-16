# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Service layer for Strategic Asset Allocation (SAA) workflows.

Exposes the :class:`SAAService` aggregator and the validation
helpers / specs used by web routes and the CLI seed installer. See
ADR-0042 §3 for the cross-module API discussion (planned but not
implemented in Phase 3).
"""

from services.saa.saa_service import (
    SAAConfigurationDetailDTO,
    SAAOptimizationResultDTO,
    SAAService,
)
from services.saa.seeds import (
    SEED_BALANCED,
    SEED_CONSERVATIVE,
    SEED_GROWTH_PM,
    SeedAssetClass,
    SeedConfiguration,
    install_seeds_for_tenant,
)
from services.saa.validation import (
    SAAAssetClassInputSpec,
    SAACorrelationSpec,
    SAAValidationError,
    validate_correlations,
    validate_inputs,
)

__all__ = [
    "SEED_BALANCED",
    "SEED_CONSERVATIVE",
    "SEED_GROWTH_PM",
    "SAAAssetClassInputSpec",
    "SAAConfigurationDetailDTO",
    "SAACorrelationSpec",
    "SAAOptimizationResultDTO",
    "SAAService",
    "SAAValidationError",
    "SeedAssetClass",
    "SeedConfiguration",
    "install_seeds_for_tenant",
    "validate_correlations",
    "validate_inputs",
]
