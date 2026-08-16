# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""SQLAlchemy ORM models for PortfoliFLOW persistence.

Models are imported here so Alembic's autogenerate (and any
``Base.metadata`` consumer) sees the full schema without each call site
having to import every module individually.
"""

from core.models.anlv_category import AnlVCategory
from core.models.asset_class import AssetClass
from core.models.asset_class_benchmark_mapping import (
    AssetClassBenchmarkMapping,
)
from core.models.audit_log import AuditLog
from core.models.base import Base
from core.models.benchmark import Benchmark
from core.models.benchmark_observation import BenchmarkObservation
from core.models.case import Case, CaseAttachment, CaseEntry
from core.models.country import Country
from core.models.data_store_entry import DataStoreEntry
from core.models.data_upload import DataUpload, DataUploadSheet
from core.models.floor_calibration import FloorCalibration
from core.models.fx_rate import FxRate
from core.models.investment import Investment
from core.models.investment_bond_analytics import InvestmentBondAnalytics
from core.models.investment_cashflow import InvestmentCashflow
from core.models.investment_country_weight import InvestmentCountryWeight
from core.models.investment_identifier import InvestmentIdentifier
from core.models.investment_maturity_weight import InvestmentMaturityWeight
from core.models.investment_nav import InvestmentNav
from core.models.investment_rating_weight import InvestmentRatingWeight
from core.models.investment_region_weight import InvestmentRegionWeight
from core.models.investment_sector_weight import InvestmentSectorWeight
from core.models.instrument_price import InstrumentPrice
from core.models.irene_finding import IreneFinding
from core.models.irene_schedule import IreneSchedule
from core.models.irene_watch_state import IreneWatchState
from core.models.limit import Limit
from core.models.limit_set import LimitSet
from core.models.market_data_schedule import MarketDataSchedule
from core.models.position_transaction import PositionTransaction
from core.models.region import Region
from core.models.region_country_membership import RegionCountryMembership
from core.models.saa_asset_class_input import SAAAssetClassInput
from core.models.saa_configuration import SAAConfiguration
from core.models.saa_correlation import SAACorrelation
from core.models.scoped_setting import ScopedSetting
from core.models.sector import Sector
from core.models.tenant import Tenant
from core.models.user import User
from core.models.watchpoint import Watchpoint

__all__ = [
    "AnlVCategory",
    "AssetClass",
    "AssetClassBenchmarkMapping",
    "AuditLog",
    "Base",
    "Benchmark",
    "BenchmarkObservation",
    "Case",
    "CaseAttachment",
    "CaseEntry",
    "Country",
    "DataStoreEntry",
    "DataUpload",
    "DataUploadSheet",
    "FloorCalibration",
    "FxRate",
    "InstrumentPrice",
    "Investment",
    "InvestmentBondAnalytics",
    "InvestmentCashflow",
    "InvestmentCountryWeight",
    "InvestmentIdentifier",
    "InvestmentMaturityWeight",
    "InvestmentNav",
    "InvestmentRatingWeight",
    "InvestmentRegionWeight",
    "InvestmentSectorWeight",
    "IreneFinding",
    "IreneSchedule",
    "IreneWatchState",
    "Limit",
    "LimitSet",
    "MarketDataSchedule",
    "PositionTransaction",
    "Region",
    "RegionCountryMembership",
    "SAAAssetClassInput",
    "SAAConfiguration",
    "SAACorrelation",
    "ScopedSetting",
    "Sector",
    "Tenant",
    "User",
    "Watchpoint",
]
