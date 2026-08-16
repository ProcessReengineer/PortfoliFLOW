# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""FloorCalibration ORM model — the tenant's materiality calibration.

Backs the ``floor_calibration`` table introduced in migration b033 (per
ADR-0116 §7). One row is **one version** of one tenant's calibration,
keyed ``(tenant_id, effective_from)`` with the same immutable-version
semantics as :mod:`core.models.watchpoint` and ``limit_sets``
(ADR-0056): the effective calibration is the latest
``effective_from <= as_of``, and an edit inserts rather than updates.

**Every calibration column is nullable, and NULL means "code default".**
A revision stores only the fields the tenant deviated on
(``DEFAULT_FLOOR_CONFIG`` in :mod:`services.analytics.irene_floor` is the
default). Three consequences, all deliberate:

* An absent row means pure defaults — so no tenant needs a seeded
  calibration row, and the demo tenant shows the defaults without one.
* A later change to a code default reaches every tenant that never
  overrode that field.
* The editor can mark each field "default / customised" from the column
  alone, without diffing against anything.

``fund_closure`` has **no column here at all** — neither floor nor cap.
It is a pinned level (floor = cap = 10, ADR-0116 §7 invariant 1), not
calibration, and giving it nowhere to be stored is the cleanest way to
make it non-editable. The other three pinned invariants constrain
*combinations* of storable values — the ``limit_breach`` floor must sit
inside the critical band, and the RSS and all-clear caps must not exceed
the informational band's top — so they are enforced in the repository's
write path, which composes the candidate row over the defaults and runs
the full ``FloorConfig`` validation plus those invariants. The beat must
never be the first to discover an inverted configuration (ADR-0116 §5).

The only schema-level rules are shape rules: the two band boundaries are
one setting and must be set together or not at all, and
``options_min_band`` must name a real final band.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class FloorCalibration(Base):
    """One immutable version of one tenant's Floor Config deviations."""

    __tablename__ = "floor_calibration"
    __table_args__ = (
        CheckConstraint(
            "(band_boundary_0 IS NULL) = (band_boundary_1 IS NULL)",
            name="ck_floor_calibration_band_boundaries_paired",
        ),
        CheckConstraint(
            "options_min_band IS NULL "
            "OR options_min_band IN ('informational', 'noteworthy', 'critical')",
            name="ck_floor_calibration_options_min_band_vocabulary",
        ),
        UniqueConstraint(
            "tenant_id",
            "effective_from",
            name="uq_floor_calibration_tenant_effective_from",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # --- tenant-wide WARN default -------------------------------------------
    warn_default_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)

    # --- per-family magnitude re-trigger deltas (all seven families) --------
    re_trigger_delta_saa: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    re_trigger_delta_anlv: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    re_trigger_delta_rss: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    re_trigger_delta_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    re_trigger_delta_fx: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    re_trigger_delta_freshness: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    re_trigger_delta_liquidity: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )

    # --- band boundaries + options gate -------------------------------------
    band_boundary_0: Mapped[int | None] = mapped_column(Integer, nullable=True)
    band_boundary_1: Mapped[int | None] = mapped_column(Integer, nullable=True)
    options_min_band: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- trigger-type floors (no fund_closure: pinned at 10) ----------------
    floor_limit_breach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_limit_escalation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_all_clear: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_rss_cluster: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_price_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_fx_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_freshness_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    floor_liquidity_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- caps, keyed by source AND trigger (no fund_closure) ----------------
    cap_source_internal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_source_rss: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_limit_breach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_limit_escalation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_all_clear: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_rss_cluster: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_price_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_fx_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_freshness_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap_liquidity_trigger: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
