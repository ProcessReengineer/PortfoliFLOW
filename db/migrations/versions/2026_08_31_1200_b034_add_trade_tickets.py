# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the trade-ticket tables: trade_tickets and trade_ticket_effects.

Revision ID: b034_add_trade_tickets
Revises: b033_add_watchpoints
Create Date: 2026-08-31 12:00:00 UTC

The persistence substrate for ADR-0128: the platform's first-class notion
of a portfolio *change*. ``trade_tickets`` records one intended or
recorded change per row (ADR-0128 §1); ``trade_ticket_effects``
enumerates the ledger rows a booked ticket emitted (ADR-0128 §2).

One object, one state machine (ADR-0128 §1)
-------------------------------------------
*Booked* and *proposed* are not two object kinds but two stations of one
lifecycle — the realised-vs-intended comparison is precisely the
analytical value, and splitting the object would turn it into a join
problem. The table is tenant-scoped and RLS-protected on the ADR-0035 §3
/ ADR-0078 pattern, with ``tenant_id`` denormalised for row-local policy
evaluation. Vocabularies are TEXT + CHECK, never SQL enums (the
b019/b020 precedent, and the codebase's status convention throughout).

The full status vocabulary from day one (ADR-0128 §3)
-----------------------------------------------------
``ck_trade_tickets_status`` lists all eight states including ``sent``,
``acknowledged`` and ``executed``, which are **unreachable in v1** — no
transition writes them. ADR-0129 arms them, and a provider confirmation
then lands in ``booked`` through exactly this ADR's machinery. Defining
them now means arming the channel is a rule change, not a migration.
The same reasoning governs the four-eyes seam (D-4): ``proposed_by`` /
``approved_by`` and their timestamps exist from this first migration,
with ``approved_by = proposed_by`` permitted in v1; enforcement becomes a
tenant-scoped setting later, again without a migration.

What the schema enforces, and what it deliberately does not
-----------------------------------------------------------
The CHECKs follow the ADR-0097 ledger's style — **sign and presence
rules only**:

* ``ck_trade_tickets_commitment_shape`` — R-3 / MD-19 as a schema fact:
  a commitment is always a ``buy`` and books no cash leg, so
  ``cash_investment_id`` is forced NULL for ``kind='commitment'``.
* ``ck_trade_tickets_units_positive`` / ``..._price_positive`` — units
  are held **unsigned** on the ticket and the sign is applied at
  emission (working document §1.1), so the ledger's directional sign
  rule has no counterpart here; only positivity does.
* the three attribution CHECKs — a status may not be reached without the
  actor and timestamp columns that station requires. Written as
  implications over the *tail* of the lifecycle, so every later status
  inherits the earlier station's requirement.

Amount arithmetic (``net = gross ∓ fees ∓ taxes``) is deliberately
**not** a CHECK. The rounding, the sign convention per direction and the
optional fee/tax split are service concerns; encoding them here would
fork one contract across two places (the ADR-0116 §3 reasoning, applied
to arithmetic rather than to bounds). Value bounds and the AnlV gate are
likewise service-level: per the mockup decision record §2.8 the AnlV gate
is a transition guard on draft→proposed / draft→booked, and
``investments.anlv_code`` stays nullable.

Master data lives on the ticket until booking (§2.5, MD-12/MD-15)
------------------------------------------------------------------
``master_data`` is JSONB and **opaque to persistence** — the
finding-payload idiom (ADR-0088), reused. The U-NEW / R-COMMIT /
R-SEC-BUY master-data inventory (name, type, asset class, currency, AnlV
code, identifier scheme + value, resolved FIGI, manager/region; vintage
year, commitment amount, purchase price, acquired NAV, assumed unfunded)
is carried here **without** an ``investments`` row existing, which is why
``investment_id`` is nullable. The investment row is created by the
booking emission, not before: discarding a draft deletes the ticket and
nothing else, so no half-created instrument ever appears in a picker, a
report, or market-data routing. Exactly two semantic states — *"this is
the data"* (draft) and *"use it"* (booked).

``cash_investment_id`` is an explicit, always-user-confirmed column
(D-1 / MD-3, decision record §2.2): there is no default-selection logic
anywhere in the system, and NULL in a draft means "not yet confirmed",
never "pick one for me". ``settlement_date`` is captured but
informational in v1 (MD-4, §2.3) — both legs book at ``trade_date``.
``set_inactive`` is the home for the U-SELL full-disposal choice
(MD-7, §2.6); R-SEC-SELL inactivates unconditionally and needs no field.
Per §2.4 and §2.7 there is deliberately **no** negative-cash marker
column (the S5 indicator derives from the current balance at read time)
and **no** fraction column (v1 models full secondary sales only).

trade_ticket_effects — one-way dependency (ADR-0128 §2)
-------------------------------------------------------
The ticket records what it booked; **the ledger stays ignorant of the
layer above it**. No new column lands on ``position_transactions``,
``investment_cashflows`` or ``investment_navs``; emitted rows keep
``ingest_origin='manual'`` (Q-1) and this table is the authoritative
linkage, which is what makes a booked ticket's effects enumerable and
therefore reversible.

``effect_id`` carries **no foreign key**, deliberately. A FK would be the
very upward coupling §2 forbids, and it would also be wrong on the facts:
the referenced row may legitimately disappear — a reversal deletes it, or
the ADR-0097 §7 per-investment CRUD corrects it — and the effect record
must survive as history rather than block the delete or vanish with it.
``prior_state`` (D-2) is the before-image of an updated ``investments``
row, populated only for ``effect_type='investment_update'``, so a
reversal can restore what the booking changed.

Both tables carry the **generic audit trigger** (operator decision D-4),
like ``watchpoints`` (b033) and unlike ``scoped_settings`` (b032, where
full row images would have copied ciphertext into ``audit_log``). A
ticket carries no secrets, and who proposed, approved and booked a
portfolio change is exactly what BAIT/VAIT-grade explainability must
capture.

Fully reversible: ``downgrade`` drops both tables, and Postgres drops
their RLS policies, row-security state, CHECKs, indexes and audit
triggers with them (the b031 / b032 / b033 idiom).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b034_add_trade_tickets"
down_revision: str | None = "b033_add_watchpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Lifecycle vocabulary (ADR-0128 §3)
#
# Named once so the status CHECK and the three attribution CHECKs are read
# against the same list. The attribution rules are implications over the
# *tail* of the lifecycle: once a station has been passed its columns stay
# required for every later status, which is what makes a booked ticket
# unable to lose its proposer.
# ---------------------------------------------------------------------------

#: Every legal status. `sent` / `acknowledged` / `executed` are defined but
#: unreachable in v1 — ADR-0129 arms them.
_STATUSES: tuple[str, ...] = (
    "draft",
    "proposed",
    "approved",
    "sent",
    "acknowledged",
    "executed",
    "booked",
    "cancelled",
)

#: Statuses at or beyond `proposed` — all require the proposer attribution.
_AT_OR_AFTER_PROPOSED: tuple[str, ...] = _STATUSES[1:7]

#: Statuses at or beyond `approved` — all require the approver attribution.
_AT_OR_AFTER_APPROVED: tuple[str, ...] = _STATUSES[2:7]


def _sql_list(values: Sequence[str]) -> str:
    """Render *values* as a SQL ``IN`` list literal."""
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # ---- trade_tickets ----------------------------------------------------
    op.create_table(
        "trade_tickets",
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
        # Tenant-sequential display number, allocated when the draft row is
        # created — the first explicit user gesture, never on opening the
        # composer (MD-2). The case_number precedent governs the sequence
        # mechanics; uq_trade_tickets_tenant_ticket_number below is the
        # race-safety guarantee.
        sa.Column("ticket_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        # Null while a new-instrument draft is mid-wizard: the investments
        # row is an emission effect, not a precondition (MD-12).
        sa.Column(
            "investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # The settlement position — always an explicit, user-confirmed
        # choice (D-1 / MD-3). Forced NULL for kind='commitment' by
        # ck_trade_tickets_commitment_shape.
        sa.Column(
            "cash_investment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # The booking date of both legs.
        sa.Column("trade_date", sa.Date(), nullable=False),
        # Recorded only; cash books on the trade date in v1 (MD-4).
        sa.Column("settlement_date", sa.Date(), nullable=True),
        # Unsigned on the ticket; the sign is applied at emission
        # (working document §1.1).
        sa.Column("units", sa.Numeric(24, 8), nullable=True),
        sa.Column("price_per_unit", sa.Numeric(20, 8), nullable=True),
        sa.Column("gross_amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("fees", sa.Numeric(20, 4), nullable=True),
        sa.Column("taxes", sa.Numeric(20, 4), nullable=True),
        sa.Column("net_amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("commitment_amount", sa.Numeric(20, 4), nullable=True),
        # The U-NEW / R-COMMIT / R-SEC-BUY master-data inventory, carried
        # here until booking emits the investments row (MD-12 / MD-15,
        # decision record §2.5). Opaque to persistence — the
        # finding-payload idiom.
        sa.Column("master_data", sa.dialects.postgresql.JSONB, nullable=True),
        # The U-SELL full-disposal choice (MD-7, §2.6). R-SEC-SELL
        # inactivates unconditionally (MD-17) and does not read this.
        sa.Column(
            "set_inactive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        # Free text, mirroring the ledger's own fields.
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        # The Watch Desk -> Case -> Transactions provenance chain
        # (ADR-0128 §1). SET NULL: a deleted case must not take the
        # portfolio-change record with it.
        sa.Column(
            "case_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # --- the four-eyes seam (D-4): columns from day one, enforcement
        # --- later. v1 permits approved_by = proposed_by.
        sa.Column(
            "proposed_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "booked_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("booked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        # --- vocabularies -------------------------------------------------
        sa.CheckConstraint(
            "kind IN ('order', 'commitment', 'secondary')",
            name="ck_trade_tickets_kind",
        ),
        sa.CheckConstraint(
            "direction IN ('buy', 'sell')",
            name="ck_trade_tickets_direction",
        ),
        # The full lifecycle from day one; sent/acknowledged/executed are
        # unreachable in v1 (ADR-0128 §3).
        sa.CheckConstraint(
            f"status IN ({_sql_list(_STATUSES)})",
            name="ck_trade_tickets_status",
        ),
        # --- shape ---------------------------------------------------------
        # R-3 / MD-19: a commitment is always a buy and books no cash.
        sa.CheckConstraint(
            "kind <> 'commitment' OR (direction = 'buy' AND cash_investment_id IS NULL)",
            name="ck_trade_tickets_commitment_shape",
        ),
        # Units are unsigned here — the emission applies the direction's
        # sign — so only positivity is a schema fact.
        sa.CheckConstraint(
            "units IS NULL OR units > 0",
            name="ck_trade_tickets_units_positive",
        ),
        sa.CheckConstraint(
            "price_per_unit IS NULL OR price_per_unit > 0",
            name="ck_trade_tickets_price_positive",
        ),
        # --- attribution ---------------------------------------------------
        # A station's columns stay required for every later status, so a
        # booked ticket cannot lose its proposer or its approver.
        sa.CheckConstraint(
            f"status NOT IN ({_sql_list(_AT_OR_AFTER_PROPOSED)}) "
            "OR (proposed_by IS NOT NULL AND proposed_at IS NOT NULL)",
            name="ck_trade_tickets_proposed_attribution",
        ),
        sa.CheckConstraint(
            f"status NOT IN ({_sql_list(_AT_OR_AFTER_APPROVED)}) "
            "OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_trade_tickets_approved_attribution",
        ),
        sa.CheckConstraint(
            "status <> 'booked' OR (booked_by IS NOT NULL AND booked_at IS NOT NULL)",
            name="ck_trade_tickets_booked_attribution",
        ),
        sa.CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_trade_tickets_cancelled_timestamp",
        ),
        # Race-safety guarantee for the tenant-sequential number allocation
        # (MD-2, decision record §2.1; the case_number precedent).
        sa.UniqueConstraint(
            "tenant_id",
            "ticket_number",
            name="uq_trade_tickets_tenant_ticket_number",
        ),
    )

    op.create_index("ix_trade_tickets_tenant_id", "trade_tickets", ["tenant_id"])
    # The blotter's read: the open tickets of one tenant, by station.
    op.create_index("ix_trade_tickets_tenant_status", "trade_tickets", ["tenant_id", "status"])
    op.create_index("ix_trade_tickets_investment_id", "trade_tickets", ["investment_id"])
    op.create_index("ix_trade_tickets_case_id", "trade_tickets", ["case_id"])

    op.execute("SELECT apply_tenant_rls('trade_tickets');")
    op.execute(
        """
        CREATE TRIGGER trade_tickets_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON trade_tickets
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- trade_ticket_effects ---------------------------------------------
    op.create_table(
        "trade_ticket_effects",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Denormalised from the parent trade_tickets row for row-local RLS
        # (ADR-0035 §3).
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # CASCADE: the effect list is the ticket's own record of what it
        # emitted and has no meaning without it.
        sa.Column(
            "ticket_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_tickets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("effect_type", sa.Text(), nullable=False),
        # Deliberately NO foreign key (ADR-0128 §2): a FK would be the
        # upward coupling the one-way dependency forbids, and the
        # referenced row may legitimately be deleted by a reversal or an
        # ADR-0097 §7 CRUD correction.
        sa.Column(
            "effect_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        # The before-image of an updated investments row (D-2), so a
        # reversal can restore what the booking changed. Populated only for
        # effect_type='investment_update'.
        sa.Column("prior_state", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column(
            "emitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "effect_type IN ('position_txn', 'cashflow', 'nav', 'investment_update')",
            name="ck_trade_ticket_effects_effect_type",
        ),
        # One ticket never records the same emitted row twice — the
        # idempotency guarantee a reversal reads against.
        sa.UniqueConstraint(
            "ticket_id",
            "effect_type",
            "effect_id",
            name="uq_trade_ticket_effects_ticket_effect",
        ),
    )

    op.create_index("ix_trade_ticket_effects_tenant_id", "trade_ticket_effects", ["tenant_id"])
    # The reverse lookup: "which ticket emitted this ledger row?"
    op.create_index(
        "ix_trade_ticket_effects_effect",
        "trade_ticket_effects",
        ["effect_type", "effect_id"],
    )

    op.execute("SELECT apply_tenant_rls('trade_ticket_effects');")
    op.execute(
        """
        CREATE TRIGGER trade_ticket_effects_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON trade_ticket_effects
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres drops each table's RLS policy, row-security state, CHECKs,
    # unique constraints, indexes and audit trigger together with the table,
    # so no explicit drops are required (the b031 / b032 / b033 idiom).
    # Effects first: they carry the FK to trade_tickets.
    op.drop_table("trade_ticket_effects")
    op.drop_table("trade_tickets")
