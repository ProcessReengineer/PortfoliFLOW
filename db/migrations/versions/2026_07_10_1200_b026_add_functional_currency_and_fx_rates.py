# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the functional currency and the fx_rates dataset.

Revision ID: b026_add_functional_currency_fx
Revises: b025_add_system_ingest_origin
Create Date: 2026-07-10 12:00:00 UTC

(The revision id is abbreviated relative to the file name: Alembic's
``alembic_version.version_num`` column is ``VARCHAR(32)``.)

The schema landing of the multi-currency model (ADR-0099 §§1–2). Three
currency concepts are distinguished throughout and must not be conflated:
the **functional currency** (the tenant's reporting currency, added here),
the **position currency** (``investments.currency``, unchanged), and the
**reference currency** (the base of the FX dataset, stored per row).

1. ``tenants.functional_currency`` — the currency in which every aggregate
   figure is reported (ADR-0099 §1). ``TEXT NOT NULL DEFAULT 'EUR'``: the
   column lands with a server default and **every existing tenant
   backfills to ``'EUR'``**, which is exactly the currency the pre-ADR-0099
   code hard-coded. Behaviour is byte-identical post-migration. Like
   ``valuation_mode`` (b024) and unlike ``ingest_origin`` (b021) the server
   default is **retained**: a new tenant is EUR unless configured
   otherwise. No CHECK is added — the ISO 4217 shape is
   application-validated and upper-cased, consistent with the deferred
   currency stammtabelle (ADR-0099 §6) and with the existing
   ``investments.currency``, which is likewise unconstrained TEXT.

2. ``fx_rates`` — one rate per ``(tenant_id, currency, as_of_date)``,
   quoted against a reference currency (ADR-0099 §2). The **normative
   quoting convention**: ``rate_to_reference`` is the price of one unit of
   ``currency`` in the reference currency — in an EUR-based deployment
   ``USD → 0.92`` means 1 USD = 0.92 EUR. Conversion between two
   non-reference currencies triangulates as
   ``amount × rate(from) / rate(to)``, which keeps the dataset linear
   rather than quadratic in the number of currencies.

   ``reference_currency`` is stored **per row** so every rate is
   self-describing for audit, and ``ck_fx_rates_currency_not_reference``
   forbids storing the identity rate: ``rate(reference) = 1`` is an
   application-level short-circuit, never a table row (ADR-0099 §3). That
   short-circuit is what makes a single-currency tenant operate with zero
   FX rows and zero behavioural change.

   ``source`` is ``NOT NULL`` per the ADR-0099 §2 column table — every
   rate names its provenance (``excel``, ``ecb``, ``yahoo``). This follows
   ``portfolio_aum.source`` (b010), the closest tenant-scoped value-series
   sibling, rather than the nullable ``instrument_prices.source`` (b024).
   ``ingest_origin`` carries the uniform ADR-0092 producer triple
   (``'excel' | 'live' | 'manual'``), CHECK-enforced as in b024. No
   ``'live'`` FX producer exists yet — the ECB SDMX adapter is the named
   successor (ADR-0099 §5) — so the origin lands dormant, exactly as
   ``position_transactions.ingest_origin`` did in b024.

   Tenant scoping is deliberate although FX rates are objective market
   data (ADR-0099 §2): it preserves RLS uniformity (ADR-0078) and the
   ADR-0092 Excel-over-live precedence is inherently tenant-specific.
   ``tenant_id`` is denormalised onto the row (ADR-0035 §3) so RLS
   evaluates without a JOIN, the standard ``apply_tenant_rls(...)`` policy
   is applied, and — because FX rates are valuation inputs, i.e. a
   financial value series like ``instrument_prices`` (b024) and
   ``portfolio_aum`` (b010), not a reference table (b020/b022) — the table
   also gets the ``audit_trigger_function`` trigger.

   ``uq_fx_rates_tenant_currency_date`` doubles as the access path: its
   leading ``(tenant_id, currency)`` prefix serves both the per-currency
   window scan and the carry-forward anchor lookup
   (``max(as_of_date) <= t``), so no additional index is created and no
   separate ``ix_fx_rates_tenant_id`` is needed (contrast b024's
   ``instrument_prices``, whose unique key does not lead with
   ``tenant_id``).

The migration is fully reversible: ``downgrade`` drops ``fx_rates``
(Postgres drops its indexes, RLS policy, row-security state and audit
trigger together with it — b020 precedent) and then drops
``tenants.functional_currency``. Nothing consumes either object yet —
no call site, no import sheet, no ``SeriesKind`` — so both directions are
data-preserving for every existing table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b026_add_functional_currency_fx"
down_revision: str | None = "b025_add_system_ingest_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- tenants.functional_currency ---------------------------------------
    # 1. Add nullable with server default 'EUR'. Adding a column with a
    #    DEFAULT populates every existing row with that value in one
    #    statement — the byte-identical backfill ADR-0099 §1 implies (the
    #    pre-ADR code hard-coded EUR everywhere).
    op.add_column(
        "tenants",
        sa.Column(
            "functional_currency",
            sa.Text(),
            nullable=True,
            server_default=sa.text("'EUR'"),
        ),
    )
    # 2. Every row now carries 'EUR'; promote to NOT NULL.
    op.alter_column(
        "tenants",
        "functional_currency",
        existing_type=sa.Text(),
        nullable=False,
    )
    # 3. The server default is RETAINED (ADR-0099 §1: NOT NULL DEFAULT
    #    'EUR') — like b024's valuation_mode, unlike b021's ingest_origin.
    #    A new tenant is EUR until configured otherwise.

    # ---- fx_rates ----------------------------------------------------------
    op.create_table(
        "fx_rates",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Rate date. ECB-style series have holiday gaps; the conversion
        # service carries the latest rate at or before the requested date
        # forward (ADR-0099 §3, mirroring the ADR-0060 NAV idiom).
        sa.Column("as_of_date", sa.Date(), nullable=False),
        # The currency being priced.
        sa.Column("currency", sa.Text(), nullable=False),
        # Price of ONE unit of `currency` in `reference_currency`.
        sa.Column("rate_to_reference", sa.Numeric(20, 10), nullable=False),
        # Stored per row for auditability; constant per tenant in practice.
        sa.Column("reference_currency", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ingest_origin", sa.Text(), nullable=False),
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "rate_to_reference > 0",
            name="ck_fx_rates_rate_positive",
        ),
        # The identity rate is never stored: rate(reference) = 1 is an
        # application-level short-circuit (ADR-0099 §2/§3).
        sa.CheckConstraint(
            "currency <> reference_currency",
            name="ck_fx_rates_currency_not_reference",
        ),
        sa.CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_fx_rates_ingest_origin",
        ),
        # One rate per currency per date per tenant. The upsert_live guard
        # conflicts on this key; its (tenant_id, currency) prefix is also the
        # access path for the window scan and the carry-forward anchor.
        sa.UniqueConstraint(
            "tenant_id",
            "currency",
            "as_of_date",
            name="uq_fx_rates_tenant_currency_date",
        ),
    )

    op.execute("SELECT apply_tenant_rls('fx_rates');")
    op.execute(
        """
        CREATE TRIGGER fx_rates_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON fx_rates
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Dropping the table drops its unique index, RLS policy, row-security
    # state and audit trigger with it, so no explicit drops are required
    # (b020 precedent).
    op.drop_table("fx_rates")
    op.drop_column("tenants", "functional_currency")
