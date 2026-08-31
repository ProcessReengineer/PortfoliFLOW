# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Repository layer for PortfoliFLOW persistence.

Repositories are the only sanctioned access path to the database. They
own all SQLAlchemy interaction and return plain DTOs (frozen
dataclasses) so callers stay ignorant of the ORM lifecycle. See
ADR-0034 §3 (Repository pattern as the access layer) and ADR-0018
(Service / Repository layering).

Tenant-scoped sessions are obtained via
:func:`core.repositories._session.tenant_context` — direct session
acquisition that bypasses the context manager is a programming error.
"""

from core.repositories._session import (
    create_engine_from_url,
    create_session_factory,
    tenant_context,
)
from core.repositories.anlv_category_repository import (
    AnlVCategoryDTO,
    AnlVCategoryRepository,
)
from core.repositories.asset_class_benchmark_mapping_repository import (
    AssetClassBenchmarkMappingDTO,
    AssetClassBenchmarkMappingRepository,
)
from core.repositories.asset_class_repository import (
    AssetClassDTO,
    AssetClassRepository,
)
from core.repositories.base import BaseRepository
from core.repositories.benchmark_observation_repository import (
    BenchmarkObservationDTO,
    BenchmarkObservationRepository,
)
from core.repositories.benchmark_repository import (
    BenchmarkDTO,
    BenchmarkRepository,
)
from core.repositories.case_attachment_repository import (
    CaseAttachmentDTO,
    CaseAttachmentRepository,
)
from core.repositories.case_repository import (
    CaseDTO,
    CaseEntryDTO,
    CaseRepository,
)
from core.repositories.country_repository import (
    CountryDTO,
    CountryRepository,
)
from core.repositories.data_upload_repository import (
    DataUploadDTO,
    DataUploadRepository,
    DataUploadSheetDTO,
)
from core.repositories.floor_calibration_repository import (
    FloorCalibrationDTO,
    FloorCalibrationRepository,
)
from core.repositories.fx_rate_repository import (
    RATES_FRAME_COLUMNS,
    FxRateDTO,
    FxRateRepository,
)
from core.repositories.instrument_price_repository import (
    InstrumentPriceDTO,
    InstrumentPriceRepository,
)
from core.repositories.investment_bond_analytics_repository import (
    BondAnalyticsDTO,
    InvestmentBondAnalyticsRepository,
)
from core.repositories.investment_cashflow_repository import (
    InvestmentCashflowDTO,
    InvestmentCashflowRepository,
)
from core.repositories.investment_country_weights_repository import (
    CountryWeightDTO,
    CountryWeightInput,
    InvestmentCountryWeightsRepository,
)
from core.repositories.investment_identifier_repository import (
    InvestmentIdentifierDTO,
    InvestmentIdentifierRepository,
)
from core.repositories.investment_maturity_weights_repository import (
    InvestmentMaturityWeightsRepository,
    MaturityWeightDTO,
)
from core.repositories.investment_nav_repository import (
    InvestmentNavDTO,
    InvestmentNavRepository,
)
from core.repositories.investment_rating_weights_repository import (
    InvestmentRatingWeightsRepository,
    RatingWeightDTO,
)
from core.repositories.investment_region_weights_repository import (
    InvestmentRegionWeightsRepository,
    RegionWeightDTO,
    RegionWeightInput,
)
from core.repositories.investment_repository import (
    InvestmentDTO,
    InvestmentRepository,
)
from core.repositories.investment_sector_weights_repository import (
    InvestmentSectorWeightsRepository,
    SectorWeightDTO,
    SectorWeightInput,
)
from core.repositories.irene_finding_repository import (
    IreneFindingDTO,
    IreneFindingRepository,
)
from core.repositories.irene_schedule_repository import (
    IreneScheduleDTO,
    IreneScheduleRepository,
)
from core.repositories.irene_watch_state_repository import (
    IreneWatchStateDTO,
    IreneWatchStateRepository,
)
from core.repositories.limits_repository import (
    LimitDTO,
    LimitSetDTO,
    LimitsRepository,
)
from core.repositories.position_transaction_repository import (
    PositionTransactionDTO,
    PositionTransactionRepository,
)
from core.repositories.region_repository import (
    RegionCountryMembershipDTO,
    RegionDTO,
    RegionRepository,
)
from core.repositories.saa_asset_class_input_repository import (
    SAAAssetClassInputDTO,
    SAAAssetClassInputRepository,
)
from core.repositories.saa_configuration_repository import (
    SAAConfigurationDTO,
    SAAConfigurationRepository,
)
from core.repositories.saa_correlation_repository import (
    SAACorrelationDTO,
    SAACorrelationRepository,
)
from core.repositories.scoped_setting_repository import (
    ScopedSettingDTO,
    ScopedSettingRepository,
)
from core.repositories.sector_repository import (
    SectorDTO,
    SectorRepository,
)
from core.repositories.tenant_repository import TenantRepository
from core.repositories.trade_ticket_repository import (
    EffectInput,
    TradeTicketDTO,
    TradeTicketEffectDTO,
    TradeTicketRepository,
)
from core.repositories.user_repository import UserDTO, UserRepository
from core.repositories.watchpoint_repository import (
    WatchpointDTO,
    WatchpointRepository,
)

__all__ = [
    "RATES_FRAME_COLUMNS",
    "AnlVCategoryDTO",
    "AnlVCategoryRepository",
    "AssetClassBenchmarkMappingDTO",
    "AssetClassBenchmarkMappingRepository",
    "AssetClassDTO",
    "AssetClassRepository",
    "BaseRepository",
    "BenchmarkDTO",
    "BenchmarkObservationDTO",
    "BenchmarkObservationRepository",
    "BenchmarkRepository",
    "BondAnalyticsDTO",
    "CaseAttachmentDTO",
    "CaseAttachmentRepository",
    "CaseDTO",
    "CaseEntryDTO",
    "CaseRepository",
    "CountryDTO",
    "CountryRepository",
    "CountryWeightDTO",
    "CountryWeightInput",
    "DataUploadDTO",
    "DataUploadRepository",
    "DataUploadSheetDTO",
    "EffectInput",
    "FloorCalibrationDTO",
    "FloorCalibrationRepository",
    "FxRateDTO",
    "FxRateRepository",
    "InstrumentPriceDTO",
    "InstrumentPriceRepository",
    "InvestmentBondAnalyticsRepository",
    "InvestmentCashflowDTO",
    "InvestmentCashflowRepository",
    "InvestmentCountryWeightsRepository",
    "InvestmentDTO",
    "InvestmentIdentifierDTO",
    "InvestmentIdentifierRepository",
    "InvestmentMaturityWeightsRepository",
    "InvestmentNavDTO",
    "InvestmentNavRepository",
    "InvestmentRatingWeightsRepository",
    "InvestmentRegionWeightsRepository",
    "InvestmentRepository",
    "InvestmentSectorWeightsRepository",
    "IreneFindingDTO",
    "IreneFindingRepository",
    "IreneScheduleDTO",
    "IreneScheduleRepository",
    "IreneWatchStateDTO",
    "IreneWatchStateRepository",
    "LimitDTO",
    "LimitSetDTO",
    "LimitsRepository",
    "MaturityWeightDTO",
    "PositionTransactionDTO",
    "PositionTransactionRepository",
    "RatingWeightDTO",
    "RegionCountryMembershipDTO",
    "RegionDTO",
    "RegionRepository",
    "RegionWeightDTO",
    "RegionWeightInput",
    "SAAAssetClassInputDTO",
    "SAAAssetClassInputRepository",
    "SAAConfigurationDTO",
    "SAAConfigurationRepository",
    "SAACorrelationDTO",
    "SAACorrelationRepository",
    "ScopedSettingDTO",
    "ScopedSettingRepository",
    "SectorDTO",
    "SectorRepository",
    "SectorWeightDTO",
    "SectorWeightInput",
    "TenantRepository",
    "TradeTicketDTO",
    "TradeTicketEffectDTO",
    "TradeTicketRepository",
    "UserDTO",
    "UserRepository",
    "WatchpointDTO",
    "WatchpointRepository",
    "create_engine_from_url",
    "create_session_factory",
    "tenant_context",
]
