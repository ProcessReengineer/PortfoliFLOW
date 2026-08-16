# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the position model: transaction ledger, instrument prices, valuation_mode.

Revision ID: b024_add_position_model
Revises: b023_extend_identifier_schemes
Create Date: 2026-07-08 12:00:00 UTC

The source-layer landing of the transaction-driven, unitised position
model (roadmap #038 strand S1), per ADR-0097. Introduces two
tenant-scoped tables and one column on ``investments``:

1. ``position_transactions`` — the transaction ledger (ADR-0097 §2). One
   row per position-changing event; holdings follow deterministically as
   the cumulative signed sum of ``units`` ordered by
   ``(trade_date, created_at, id)``. Signed quantities keep the derivation
   a plain cumulative sum: ``opening``/``buy`` require ``units > 0``,
   ``sell`` requires ``units < 0``, ``transfer`` requires ``units <> 0``
   (CHECK-enforced). ``price_per_unit``, when present, must be ``> 0``;
   ``buy``/``sell`` require a price, ``opening``/``transfer`` may omit it.
   A partial unique index enforces **at most one ``opening`` per
   investment** — the opening anchors the ledger; corrections edit it
   rather than stacking a second row (duplication is structurally
   impossible). ``ingest_origin`` uses the uniform ADR-0092 triple even
   though no ``'live'`` transaction writer exists yet.

2. ``instrument_prices`` — the per-unit price series (ADR-0097 §3). Keyed
   ``(investment_id, as_of_date)``, deliberately mirroring
   ``investment_navs`` — one canonical price, **no ``price_kind``**
   (close/bid/ask is a named successor concern, YAGNI). The pinned basis
   is the provider's daily valuation price (Yahoo unadjusted EOD close,
   Bloomberg ``PX_LAST``). ``ingest_origin`` carries the ADR-0092
   precedence field; the repository's ``upsert_live`` never mutates an
   ``'excel'``/``'manual'`` row.

3. ``investments.valuation_mode`` — the per-investment write-path
   discriminator (ADR-0097 §1): ``'reported'`` (NAV carried directly in
   ``investment_navs``, as today) or ``'unitised'`` (NAV materialised from
   holdings × price, ADR-0098). The column lands with ``DEFAULT 'reported'``
   and **every existing investment of every type backfills to
   ``'reported'``** — no automatic flips, behaviour byte-identical
   post-migration. Unlike ``ingest_origin`` (b021), the server default is
   **retained** (ADR-0097 §1 specifies ``NOT NULL DEFAULT 'reported'``): a
   new investment is ``'reported'`` unless an operator explicitly flips it
   (strand S5).

Both new tables are tenant-scoped (ADR-0035 §3): ``tenant_id`` is
denormalised onto the row so RLS evaluates without a JOIN, the standard
``apply_tenant_rls(...)`` policy is applied, and — because both are
auditable financial tables (the ledger's MaRisk/BAIT posture is cited in
ADR-0097 §Alternatives; prices are a value series like ``investment_navs``)
— each gets the ``audit_trigger_function`` trigger (b006/b010 precedent for
financial-domain tables, unlike the reference tables b020/b022 which take
RLS only).

The migration is fully reversible: ``downgrade`` drops the two tables
(Postgres drops their indexes, RLS policies, row-security state and audit
triggers together with them — b020 precedent) and then drops the CHECK and
``valuation_mode`` column on ``investments``. ``investment_navs`` and the
ADR-0092 upsert semantics are untouched — the ``'system'`` origin
extension is ADR-0098's migration (one concern per migration).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b024_add_position_model"
down_revision: str | None = "b023_extend_identifier_schemes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- position_transactions ---------------------------------------------
    op.create_table(
        "position_transactions",
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
        sa.Column(
            "investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("txn_type", sa.Text(), nullable=False),
        # Statement-day semantics, mirroring investment_navs.as_of_date.
        sa.Column("trade_date", sa.Date(), nullable=False),
        # Signed; sign rules enforced by ck_position_transactions_sign.
        sa.Column("units", sa.Numeric(24, 8), nullable=False),
        sa.Column("price_per_unit", sa.Numeric(20, 8), nullable=True),
        # Signed cash effect, optional.
        sa.Column("consideration", sa.Numeric(20, 4), nullable=True),
        # Must equal investments.currency (ADR-0097 §5) — validated in the
        # service layer, no conversion.
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
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
            "txn_type IN ('opening', 'buy', 'sell', 'transfer')",
            name="ck_position_transactions_txn_type",
        ),
        # Signed quantities keep holdings derivation a plain cumulative sum
        # (ADR-0097 §2 sign rules).
        sa.CheckConstraint(
            "(txn_type IN ('opening', 'buy') AND units > 0) "
            "OR (txn_type = 'sell' AND units < 0) "
            "OR (txn_type = 'transfer' AND units <> 0)",
            name="ck_position_transactions_sign",
        ),
        # A present price is strictly positive.
        sa.CheckConstraint(
            "price_per_unit IS NULL OR price_per_unit > 0",
            name="ck_position_transactions_price_positive",
        ),
        # buy/sell require a price; opening/transfer may omit it.
        sa.CheckConstraint(
            "txn_type NOT IN ('buy', 'sell') OR price_per_unit IS NOT NULL",
            name="ck_position_transactions_price_required",
        ),
        sa.CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_position_transactions_ingest_origin",
        ),
    )
    op.create_index(
        "ix_position_transactions_tenant_id",
        "position_transactions",
        ["tenant_id"],
    )
    # Supports the deterministic ledger read (ORDER BY trade_date, created_at,
    # id) that holdings derivation and materialisation consume.
    op.create_index(
        "ix_position_transactions_investment_trade_date",
        "position_transactions",
        ["investment_id", "trade_date"],
    )
    # At most one opening per investment: the opening anchors the ledger, so a
    # second one is structurally impossible (ADR-0097 §2).
    op.create_index(
        "uq_position_transactions_opening",
        "position_transactions",
        ["investment_id"],
        unique=True,
        postgresql_where=sa.text("txn_type = 'opening'"),
    )

    op.execute("SELECT apply_tenant_rls('position_transactions');")
    op.execute(
        """
        CREATE TRIGGER position_transactions_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON position_transactions
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- instrument_prices -------------------------------------------------
    op.create_table(
        "instrument_prices",
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
        sa.Column(
            "investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Statement day, mirrors investment_navs.as_of_date.
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        # Must equal investments.currency (ADR-0097 §5).
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
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
            "price > 0",
            name="ck_instrument_prices_price_positive",
        ),
        sa.CheckConstraint(
            "ingest_origin IN ('excel', 'live', 'manual')",
            name="ck_instrument_prices_ingest_origin",
        ),
        # One canonical price per investment per statement day (no kind
        # dimension — prices are actuals; ADR-0097 §3). The upsert_live guard
        # conflicts on this key.
        sa.UniqueConstraint(
            "investment_id",
            "as_of_date",
            name="uq_instrument_prices_investment_date",
        ),
    )
    op.create_index(
        "ix_instrument_prices_tenant_id",
        "instrument_prices",
        ["tenant_id"],
    )

    op.execute("SELECT apply_tenant_rls('instrument_prices');")
    op.execute(
        """
        CREATE TRIGGER instrument_prices_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON instrument_prices
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- investments.valuation_mode ----------------------------------------
    # 1. Add nullable with server default 'reported'. Adding a column with a
    #    DEFAULT populates every existing row with that value in one
    #    statement — the byte-identical backfill ADR-0097 §6 mandates.
    op.add_column(
        "investments",
        sa.Column(
            "valuation_mode",
            sa.Text(),
            nullable=True,
            server_default=sa.text("'reported'"),
        ),
    )
    # 2. Every row now carries 'reported'; promote to NOT NULL.
    op.alter_column(
        "investments",
        "valuation_mode",
        existing_type=sa.Text(),
        nullable=False,
    )
    # 3. The server default is RETAINED (ADR-0097 §1: NOT NULL DEFAULT
    #    'reported') — unlike b021's ingest_origin. A new investment is
    #    'reported' until an operator explicitly flips it (strand S5).
    # 4. Constrain to the closed mode set.
    op.create_check_constraint(
        "ck_investments_valuation_mode",
        "investments",
        "valuation_mode IN ('reported', 'unitised')",
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Dropping each table drops its indexes, RLS policy, row-security state and
    # audit trigger with it, so no explicit drops are required (b020 precedent).
    op.drop_table("instrument_prices")
    op.drop_table("position_transactions")
    op.drop_constraint("ck_investments_valuation_mode", "investments", type_="check")
    op.drop_column("investments", "valuation_mode")
