# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Add the watchpoints registry and the floor_calibration table.

Revision ID: b033_add_watchpoints
Revises: b032_add_scoped_settings
Create Date: 2026-08-10 12:00:00 UTC

The persistence substrate for ADR-0116: what the Watch Desk observes
(``watchpoints``) and at which thresholds it judges materiality
(``floor_calibration``). Both tables follow the same three-part shape:

1. **Immutable version rows** keyed by ``effective_from``, the
   ``limit_sets`` pattern from ADR-0056. An edit *inserts* a new
   version; nothing is ever updated in place. The current version is
   the latest ``effective_from <= as_of``. For ``watchpoints`` a stable
   ``watchpoint_id`` carries the identity across its versions, and
   retirement is a version with ``retired = TRUE`` — the identity and
   its history stay queryable so a past finding remains explainable.
2. ``apply_tenant_rls(...)`` — the standard ``tenant_isolation`` policy.
3. The **generic audit trigger IS attached** (ADR-0116 §1), unlike
   ``scoped_settings`` (b032). The omission there was a secret-hygiene
   requirement: ``audit_trigger_function()`` captures full row images,
   which for credentials would copy ciphertext into ``audit_log``.
   Watchpoints and calibration carry no secrets, and a threshold change
   is exactly the kind of decision BAIT/VAIT-grade explainability must
   capture. Versioning gives reproducibility; the trigger gives actor
   attribution. Both, deliberately.

``effective_from`` is ``TIMESTAMPTZ``, not the ``DATE`` of
``limit_sets``. ADR-0116 §1 states the resolution rule as "the latest
``effective_from <= now()``", and unlike a limit set — a business
document with a calendar validity date — a watchpoint revision is an
operational act that may well happen twice in one day. A ``DATE`` key
would collide on the unique constraint the second time.

The asymmetry (ADR-0116 §1, binding)
------------------------------------
For the derived families the watchpoint is a *sensitivity overlay
only*: subject identity and ceilings remain solely with the limit set,
and there is never a second edit point for limits. For the four defined
signal families the watchpoint *defines* the subject. That asymmetry is
enforced **here**, by per-family CHECK constraints, not in the
repository:

* ``saa`` / ``anlv`` — only ``muted``, ``warn_threshold_pct`` and
  ``re_trigger_delta`` may be set; every defining column is forced NULL.
* ``rss`` — ``muted`` only.
* ``price`` — ``instrument_id`` + ``drop_pct`` + ``window_days``
  required; every other defining column NULL.
* ``fx`` — ``currency_pair`` + ``move_pct`` + ``window_days`` required.
* ``freshness`` — ``max_age_days`` required.
* ``liquidity`` — ``horizon_months`` + ``min_coverage_ratio`` required.

A UI or repository bug therefore *cannot* create a second edit point
for limits: the schema refuses the row. Value bounds
(``50 < warn_threshold_pct < 100``, positive deltas and windows,
well-formed currency pairs) are deliberately **not** CHECKs — ADR-0116
§3 places them in the repository and the route, and duplicating them in
the schema would fork one contract across two places.

There is no ``pacing`` family, not even as a CHECK-listed value: the TA
engine does not exist yet and plan-deviation watching has no reliable
reference object today (ADR-0116 Non-goals).

``floor_calibration`` stores **only deviations**
-------------------------------------------------
Every calibration column is nullable and NULL means "use the code
default" (ADR-0116 §7). A revision therefore records what a tenant
changed, not a full copy of ``DEFAULT_FLOOR_CONFIG`` — so a later
change to a code default reaches every tenant that never overrode it,
and the editor can mark each field "default / customised" from the
column alone. An absent row means pure defaults, which is why no
calibration row is seeded for any tenant.

``fund_closure`` gets **no columns at all** — neither floor nor cap.
It is a pinned level (floor = cap = 10, ADR-0116 §7 invariant 1), not
calibration, and the cleanest way to make it non-editable is to give it
nowhere to be stored. The remaining three pinned invariants constrain
*combinations* of storable values and are enforced in the repository's
write path, which composes the candidate row over the defaults and runs
the full ``FloorConfig`` validation plus those invariants.

No data is seeded by this migration: watchpoint seeding is
service-level and idempotent (ADR-0116 §8,
``services/watchpoints/seeding.py``), and ``floor_calibration`` is
seeded never.

Fully reversible: ``downgrade`` drops both tables, and Postgres drops
their RLS policies, row-security state, CHECKs, indexes and audit
triggers with them (the b031/b032 idiom).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b033_add_watchpoints"
down_revision: str | None = "b032_add_scoped_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Per-family shape CHECKs — the asymmetry, in SQL.
#
# Each is written as an implication ("this family ⇒ this column pattern"),
# so a row of another family satisfies it vacuously and every family's
# rule can be read, and failed, in isolation.
# ---------------------------------------------------------------------------

# The eight family-specific parameter columns. Named here once so each
# CHECK below can be read against the same list.
_DEFINING_COLUMNS: tuple[str, ...] = (
    "instrument_id",
    "currency_pair",
    "drop_pct",
    "move_pct",
    "window_days",
    "max_age_days",
    "horizon_months",
    "min_coverage_ratio",
)


def _all_null_except(*present: str) -> str:
    """Render ``a IS NULL AND b IS NULL AND …`` for the defining columns.

    Args:
        *present: Columns that must NOT be NULL for the family — they are
            excluded from the NULL list and asserted non-NULL separately.

    Returns:
        A SQL boolean expression over the remaining defining columns.
    """
    absent = [column for column in _DEFINING_COLUMNS if column not in present]
    return " AND ".join(f"{column} IS NULL" for column in absent)


def _all_not_null(*present: str) -> str:
    """Render ``a IS NOT NULL AND b IS NOT NULL AND …``."""
    return " AND ".join(f"{column} IS NOT NULL" for column in present)


def _family_shape(family: str, *required: str, sensitivity: str = "") -> str:
    """Render one family's implication CHECK.

    Args:
        family: The family value this rule applies to.
        *required: Defining columns that must be present for it.
        sensitivity: Optional extra clause constraining the sensitivity
            columns (used by ``rss``, which carries ``muted`` only).

    Returns:
        ``family <> '<f>' OR (<required non-NULL> AND <rest NULL> …)``.
    """
    clauses = [
        clause for clause in (_all_not_null(*required), _all_null_except(*required)) if clause
    ]
    if sensitivity:
        clauses.append(sensitivity)
    return f"family <> '{family}' OR ({' AND '.join(clauses)})"


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ---- watchpoints ------------------------------------------------------
    op.create_table(
        "watchpoints",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The stable identity carried across every version row. Generated
        # by the repository on create; copied verbatim by revise / retire.
        sa.Column(
            "watchpoint_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Retirement is a version, not a delete: the identity and its
        # history stay queryable so past findings remain explainable.
        sa.Column(
            "retired",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        # Closed set, CHECK-enforced. TEXT rather than a SQL enum, per the
        # codebase convention. No 'pacing' (ADR-0116 Non-goals).
        sa.Column("family", sa.Text(), nullable=False),
        # For an overlay row: the derived subject it overlays. For a
        # defined row: the key its producer will emit.
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        # --- sensitivity columns (legal for the overlay families) --------
        sa.Column(
            "muted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("warn_threshold_pct", sa.Numeric(6, 3), nullable=True),
        sa.Column("re_trigger_delta", sa.Numeric(12, 4), nullable=True),
        # --- defining columns (legal only for the family that needs them) -
        sa.Column(
            "instrument_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # 'BASE/QUOTE', e.g. 'USD/EUR'. Format is validated in the
        # repository, not here (ADR-0116 §3).
        sa.Column("currency_pair", sa.Text(), nullable=True),
        sa.Column("drop_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("move_pct", sa.Numeric(12, 4), nullable=True),
        sa.Column("window_days", sa.Integer(), nullable=True),
        sa.Column("max_age_days", sa.Integer(), nullable=True),
        sa.Column("horizon_months", sa.Integer(), nullable=True),
        sa.Column("min_coverage_ratio", sa.Numeric(12, 4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "family IN ('saa', 'anlv', 'rss', 'price', 'fx', 'freshness', 'liquidity')",
            name="ck_watchpoints_family_vocabulary",
        ),
        # --- the asymmetry ------------------------------------------------
        # saa / anlv: sensitivity overlay only. Ceilings and subject
        # identity stay with the limit set — there is never a second edit
        # point for limits (ADR-0116 §1).
        sa.CheckConstraint(
            f"family NOT IN ('saa', 'anlv') OR ({_all_null_except()})",
            name="ck_watchpoints_overlay_family_defines_nothing",
        ),
        # rss: muted only. An RSS cluster subject is non-scalar, so
        # neither a WARN fraction nor a magnitude delta means anything.
        sa.CheckConstraint(
            f"family <> 'rss' OR ({_all_null_except()} "
            "AND warn_threshold_pct IS NULL AND re_trigger_delta IS NULL)",
            name="ck_watchpoints_rss_carries_mute_only",
        ),
        sa.CheckConstraint(
            _family_shape("price", "instrument_id", "drop_pct", "window_days"),
            name="ck_watchpoints_price_shape",
        ),
        sa.CheckConstraint(
            _family_shape("fx", "currency_pair", "move_pct", "window_days"),
            name="ck_watchpoints_fx_shape",
        ),
        sa.CheckConstraint(
            _family_shape("freshness", "max_age_days"),
            name="ck_watchpoints_freshness_shape",
        ),
        sa.CheckConstraint(
            _family_shape("liquidity", "horizon_months", "min_coverage_ratio"),
            name="ck_watchpoints_liquidity_shape",
        ),
        # One version per identity per instant. Its backing index is also
        # the resolution read's index: DISTINCT ON (watchpoint_id) over
        # effective_from DESC within the tenant.
        sa.UniqueConstraint(
            "tenant_id",
            "watchpoint_id",
            "effective_from",
            name="uq_watchpoints_tenant_identity_effective_from",
        ),
    )

    op.execute("SELECT apply_tenant_rls('watchpoints');")
    op.execute(
        """
        CREATE TRIGGER watchpoints_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON watchpoints
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )

    # ---- floor_calibration ------------------------------------------------
    # Every column below is nullable by design: NULL means "code default"
    # (ADR-0116 §7). fund_closure appears nowhere — a pinned level, not a
    # knob.
    op.create_table(
        "floor_calibration",
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
            "effective_from",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Tenant-wide WARN default (the 90% the coverage engine takes as a
        # parameter today). Per-subject overrides live on the watchpoint.
        sa.Column("warn_default_pct", sa.Numeric(6, 3), nullable=True),
        # Per-family magnitude re-trigger deltas — all seven families.
        sa.Column("re_trigger_delta_saa", sa.Numeric(12, 4), nullable=True),
        sa.Column("re_trigger_delta_anlv", sa.Numeric(12, 4), nullable=True),
        sa.Column("re_trigger_delta_rss", sa.Numeric(12, 4), nullable=True),
        sa.Column("re_trigger_delta_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("re_trigger_delta_fx", sa.Numeric(12, 4), nullable=True),
        sa.Column("re_trigger_delta_freshness", sa.Numeric(12, 4), nullable=True),
        sa.Column("re_trigger_delta_liquidity", sa.Numeric(12, 4), nullable=True),
        # The two urgency cut points splitting 1–10 into the three final
        # bands. Set together or not at all (CHECK below): a half-specified
        # boundary pair has no meaning.
        sa.Column("band_boundary_0", sa.Integer(), nullable=True),
        sa.Column("band_boundary_1", sa.Integer(), nullable=True),
        # The options gate — the lowest final band at which options survive.
        sa.Column("options_min_band", sa.Text(), nullable=True),
        # Trigger-type floors. No fund_closure column: pinned at 10.
        sa.Column("floor_limit_breach", sa.Integer(), nullable=True),
        sa.Column("floor_limit_escalation", sa.Integer(), nullable=True),
        sa.Column("floor_all_clear", sa.Integer(), nullable=True),
        sa.Column("floor_rss_cluster", sa.Integer(), nullable=True),
        sa.Column("floor_price_trigger", sa.Integer(), nullable=True),
        sa.Column("floor_fx_trigger", sa.Integer(), nullable=True),
        sa.Column("floor_freshness_trigger", sa.Integer(), nullable=True),
        sa.Column("floor_liquidity_trigger", sa.Integer(), nullable=True),
        # Caps, keyed by BOTH source and trigger (the effective cap is
        # min(cap[source], cap[trigger])). No fund_closure column.
        sa.Column("cap_source_internal", sa.Integer(), nullable=True),
        sa.Column("cap_source_rss", sa.Integer(), nullable=True),
        sa.Column("cap_limit_breach", sa.Integer(), nullable=True),
        sa.Column("cap_limit_escalation", sa.Integer(), nullable=True),
        sa.Column("cap_all_clear", sa.Integer(), nullable=True),
        sa.Column("cap_rss_cluster", sa.Integer(), nullable=True),
        sa.Column("cap_price_trigger", sa.Integer(), nullable=True),
        sa.Column("cap_fx_trigger", sa.Integer(), nullable=True),
        sa.Column("cap_freshness_trigger", sa.Integer(), nullable=True),
        sa.Column("cap_liquidity_trigger", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        # Both boundaries or neither — the pair is one setting.
        sa.CheckConstraint(
            "(band_boundary_0 IS NULL) = (band_boundary_1 IS NULL)",
            name="ck_floor_calibration_band_boundaries_paired",
        ),
        sa.CheckConstraint(
            "options_min_band IS NULL "
            "OR options_min_band IN ('informational', 'noteworthy', 'critical')",
            name="ck_floor_calibration_options_min_band_vocabulary",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "effective_from",
            name="uq_floor_calibration_tenant_effective_from",
        ),
    )

    op.execute("SELECT apply_tenant_rls('floor_calibration');")
    op.execute(
        """
        CREATE TRIGGER floor_calibration_audit_trigger
        AFTER INSERT OR UPDATE OR DELETE ON floor_calibration
        FOR EACH ROW
        EXECUTE FUNCTION audit_trigger_function();
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Postgres drops each table's RLS policy, row-security state, CHECKs,
    # unique constraint and audit trigger together with the table, so no
    # explicit drops are required (the b031 / b032 idiom).
    op.drop_table("floor_calibration")
    op.drop_table("watchpoints")
