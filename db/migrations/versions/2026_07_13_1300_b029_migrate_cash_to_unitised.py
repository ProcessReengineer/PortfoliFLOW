# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Migrate ADR-0100 reported cash rows to the unitised representation (ADR-0103 §9).

Revision ID: b029_migrate_cash_to_unitised
Revises: b028_add_investor_flow_type
Create Date: 2026-07-13 13:00:00 UTC

ADR-0100 modelled a foreign-currency cash balance as an
``investment_type='cash'`` row in ``valuation_mode='reported'``, fed by
``investment_navs`` levels. ADR-0103 §1 makes cash the **degenerate unitised
case** instead: balance ≡ holdings, derived from ``position_transactions``,
priced by stored unity rows so the unchanged ADR-0098 materialisation values
it with no cash branch anywhere in the book path. This is the data migration
that carries the existing rows across (ADR-0103 §9, decision N5).

Per cash row in scope — ``investment_type='cash'`` **and**
``valuation_mode='reported'``, across every tenant — the migration:

1. **Synthesises the ledger** from the row's ``nav_kind='actual'`` NAV
   history: an ``opening`` at the earliest NAV date (``units = nav_value``),
   then one signed ``transfer`` per subsequent NAV date carrying the delta to
   its predecessor. **A zero delta writes nothing** (ADR-0103 §4: an
   unchanged balance is not an event) — which is also the target shape the
   Cash-sheet reconcile derives, so the first v32 import classifies this
   ledger as *unchanged* rather than restating it.
2. **Backfills unity prices** — one ``1.00000000`` row per actual NAV date, in
   the position's own currency (ADR-0103 §1; the ADR-0097 §5 currency-equality
   rule forbids a converted price).
3. **Flips ``valuation_mode`` to ``'unitised'``** — the one-way direction
   ADR-0097 §6 reserves for this ADR.

**Level origins.** Both ``'excel'`` and ``'manual'`` actual NAV rows are
levels and participate; ``'live'`` cannot exist on a cash position (cash is
permanently excluded from live ingest, ADR-0103 §1). The count of absorbed
``'manual'`` rows is raised as a ``NOTICE`` — an operator edit becoming part of
a synthesised ledger is worth seeing once.

**Where that NOTICE actually lands.** Postgres sends ``NOTICE`` to the
*client*, and the client here is asyncpg, which drops it unless a listener is
registered — so ``alembic upgrade head`` prints nothing. The NOTICE is
therefore a convenience for a ``psql``-driven apply, not the operator's
evidence. The evidence is the **marker itself**: every synthesised row carries
``source = 'adr-0103-s14-migration'``, so the whole run stays queryable long
after it has finished ::

    SELECT (SELECT count(*) FROM position_transactions
             WHERE source = 'adr-0103-s14-migration'
               AND txn_type = 'opening')  AS openings,
           (SELECT count(*) FROM position_transactions
             WHERE source = 'adr-0103-s14-migration'
               AND txn_type = 'transfer') AS transfers,
           (SELECT count(*) FROM instrument_prices
             WHERE source = 'adr-0103-s14-migration') AS unity_prices;

**Why the ADR-0098 materialisation is deliberately NOT run here.** Migrations
never import the application package (the b012 idiom: pure SQL, anchor copies
of constants, regression tests pinning the literals). It costs nothing:
ADR-0103 §9.5 makes the run a **provable no-op**. The materialised set is one
NAV row per ``instrument_prices`` date on or after the first ledger date, and
this migration writes exactly one price per NAV date — so the target set *is*
the NAV-date set, and every one of those dates already carries an
``'excel'``/``'manual'`` NAV row, which precedence protects (the service only
ever writes its own ``'system'`` rows). Every target date is therefore a
counted skip: ``inserted = 0``, ``updated = 0``. The values agree too, by
construction — ``holdings(date) × 1.00000000`` is the balance the NAV row
already carries. That proof is delivered as an executable fact by
``tests/regression/test_b029_cash_row_migration.py`` and re-run by the
operator against the production database (ADR-0103 §9.5 verification step).

**Idempotency / robustness against S1.3 artefacts.** Every step is guarded by
existence, because a cash row may legally arrive here already carrying
``'excel'`` ledger and price rows: the v32 Cash-sheet importer writes them for
a cash position that is still ``'reported'`` (it never flips the mode itself,
and warns), keeping the balances as ``'excel'`` NAV rows meanwhile. Such a row
has a **book of record already** — so when an ``opening`` exists (of any
origin), ledger synthesis is skipped for that row *entirely*, opening and
transfers together: the two are one all-or-nothing unit, and half a synthesised
ledger on top of an imported one would double-count every balance change.
Unity prices are guarded independently on the ``(investment_id, as_of_date)``
natural key, so an already-priced date is left alone. A cash row with no actual
NAV history gets step 3 only — there is nothing to synthesise, and it is an
empty position either way.

**Integrity gates (abort, never repair).** The migration refuses rather than
invents, in the ADR-0080 no-silent-fallback tradition (b017's precedent):

* a **negative** actual ``nav_value`` — ADR-0100 §5 makes an actual cash
  balance non-negative, so this is data corruption, not input;
* a NAV row whose **currency** differs from its investment's — synthesising a
  ledger from it would smuggle an implicit FX conversion into the book path,
  which ADR-0097 §5 and ADR-0099 both forbid outright;
* a **zero earliest balance** on a row that would synthesise — the
  ``ck_position_transactions_sign`` CHECK requires ``units > 0`` on an
  ``opening``, so ADR-0103 §9's "``units = nav_value``" is not representable
  when that value is zero. The schema has no answer here and neither has §9,
  so the migration names the row and stops.

**Downgrade — deliberately partial, and honest about it.** Every synthesised
row carries the deterministic marker ``source = 'adr-0103-s14-migration'``;
the downgrade deletes exactly those and flips back exactly those cash rows
that possess a **marker opening**. A row the upgrade flipped *without*
synthesising — the premature-v32 case above — is indistinguishable afterwards
and is **not** reverted: it keeps ``'unitised'``, and its imported ledger is
left untouched. This is documented one-way residue, in the b028 "reversible
only while …" tradition. Likewise, ``'system'`` NAV rows a later
materialisation may have written are left to the service layer; the downgrade
touches only marker rows and the mode column of marker-opening rows. (In
practice the upgrade itself strands none: §9.5 is exactly the statement that
no ``'system'`` row is written.) The downgrade is a migration-window undo, not
a general one — once a v32 import has restated a marker row, that row is the
importer's.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b029_migrate_cash_to_unitised"
down_revision: str | None = "b028_add_investor_flow_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Anchor copies of the application's constants. Migrations stay importable
# without the application package on path (the b012 idiom), so these literals
# are restated here rather than imported — and
# ``tests/regression/test_b029_cash_row_migration.py`` pins each one against
# its Python original, which is what keeps the two from drifting.
# ---------------------------------------------------------------------------

#: The deterministic provenance marker on every row this migration synthesises
#: — the sole handle the downgrade has on its own work. Deterministic by
#: design: no timestamps, no run ids, nothing a second run would spell
#: differently.
_MARKER: str = "adr-0103-s14-migration"

#: The only price a cash position may carry (ADR-0103 §1), spelled at the
#: ``Numeric(20, 8)`` scale of ``instrument_prices.price``. Anchors
#: ``services.investments.unity_price.UNITY_PRICE``.
_UNITY_PRICE: str = "1.00000000"

#: The actual-NAV origins that are *levels* and therefore participate in the
#: ledger synthesis. ``'live'`` is absent because it cannot occur on cash
#: (permanently ineligible for live ingest, ADR-0103 §1); ``'system'`` is
#: absent because a ``'reported'`` row has none (materialisation is a
#: whole-investment no-op below ``'unitised'``, ADR-0098 §2).
_LEVEL_ORIGINS: str = "'excel', 'manual'"

#: The row scope, restated in every statement below because SQL cannot factor a
#: predicate across statements. Rows already unitised are never touched.
_IN_SCOPE: str = "i.investment_type = 'cash' AND i.valuation_mode = 'reported'"


_VALIDATE = f"""
DO $$
DECLARE
    offending text;
BEGIN
    -- An actual cash balance cannot be negative (ADR-0100 §5). A negative
    -- one is corruption, and synthesising a ledger from it would launder
    -- that corruption into the book of record.
    SELECT string_agg(
               format('%s on %s (%s)', i.name, n.as_of_date, n.nav_value),
               '; ' ORDER BY i.name, n.as_of_date)
      INTO offending
      FROM investments i
      JOIN investment_navs n ON n.investment_id = i.id
     WHERE {_IN_SCOPE}
       AND n.nav_kind = 'actual'
       AND n.ingest_origin IN ({_LEVEL_ORIGINS})
       AND n.nav_value < 0;
    IF offending IS NOT NULL THEN
        RAISE EXCEPTION
            'ADR-0103 §9 migration: cash positions carry negative actual '
            'balances, which ADR-0100 §5 forbids — fix the data before '
            'migrating: %', offending;
    END IF;

    -- ADR-0097 §5: the ledger and its prices are stated in the position's
    -- own currency, never converted. A NAV row in a foreign currency has no
    -- representable ledger, and a 1:1 fallback is exactly what ADR-0099
    -- forbids.
    SELECT string_agg(
               format('%s: NAV on %s is %s, position is %s',
                      i.name, n.as_of_date, n.currency, i.currency),
               '; ' ORDER BY i.name, n.as_of_date)
      INTO offending
      FROM investments i
      JOIN investment_navs n ON n.investment_id = i.id
     WHERE {_IN_SCOPE}
       AND n.nav_kind = 'actual'
       AND n.ingest_origin IN ({_LEVEL_ORIGINS})
       AND n.currency <> i.currency;
    IF offending IS NOT NULL THEN
        RAISE EXCEPTION
            'ADR-0103 §9 migration: cash positions carry actual NAV rows in '
            'a currency other than the position''s own; a converted ledger '
            'is not a ledger (ADR-0097 §5): %', offending;
    END IF;

    -- The opening carries ``units = nav_value`` (ADR-0103 §9.2), and
    -- ``ck_position_transactions_sign`` requires ``units > 0`` on an
    -- opening. A zero earliest balance is therefore not representable —
    -- neither the schema nor §9 has an answer, so name the row and stop
    -- rather than invent one. Only rows that would actually synthesise are
    -- gated: a row whose opening already exists is skipped below anyway.
    SELECT string_agg(format('%s (earliest NAV %s)', i.name, first.as_of_date),
                      '; ' ORDER BY i.name)
      INTO offending
      FROM investments i
      JOIN LATERAL (
            SELECT n.as_of_date, n.nav_value
              FROM investment_navs n
             WHERE n.investment_id = i.id
               AND n.nav_kind = 'actual'
               AND n.ingest_origin IN ({_LEVEL_ORIGINS})
             ORDER BY n.as_of_date
             LIMIT 1
           ) AS first ON TRUE
     WHERE {_IN_SCOPE}
       AND first.nav_value = 0
       AND NOT EXISTS (
             SELECT 1
               FROM position_transactions t
              WHERE t.investment_id = i.id
                AND t.txn_type = 'opening');
    IF offending IS NOT NULL THEN
        RAISE EXCEPTION
            'ADR-0103 §9 migration: cash positions open on a zero balance, '
            'which cannot be an opening transaction (units > 0, ADR-0097 §2). '
            'Drop the leading zero-balance rows or give the position a '
            'non-zero first statement, then re-run: %', offending;
    END IF;
END $$;
"""


# Steps 1 + 2 of ADR-0103 §9 as a single statement — deliberately. The opening
# and the transfers are one all-or-nothing unit (see the module docstring), and
# splitting them into two INSERTs would break exactly that: the first would
# create the opening, and the second's "no opening exists" guard would then be
# false, so the transfers would silently never land.
_SYNTHESISE_LEDGER = f"""
WITH scoped AS (
    SELECT i.id, i.currency
      FROM investments i
     WHERE {_IN_SCOPE}
       -- An opening of any origin means the row already has a book of
       -- record (a premature v32 import). Skip its ledger synthesis whole.
       AND NOT EXISTS (
             SELECT 1
               FROM position_transactions t
              WHERE t.investment_id = i.id
                AND t.txn_type = 'opening')
),
levels AS (
    SELECT s.id AS investment_id,
           s.currency,
           n.tenant_id,
           n.created_by,
           n.as_of_date,
           n.nav_value,
           ROW_NUMBER() OVER w AS rn,
           LAG(n.nav_value) OVER w AS previous_value
      FROM scoped s
      JOIN investment_navs n ON n.investment_id = s.id
     WHERE n.nav_kind = 'actual'
       AND n.ingest_origin IN ({_LEVEL_ORIGINS})
    WINDOW w AS (PARTITION BY s.id ORDER BY n.as_of_date)
)
INSERT INTO position_transactions (
    tenant_id, investment_id, txn_type, trade_date, units,
    price_per_unit, consideration, currency, source, ingest_origin, created_by
)
SELECT levels.tenant_id,
       levels.investment_id,
       CASE WHEN rn = 1 THEN 'opening' ELSE 'transfer' END,
       levels.as_of_date,
       -- The level itself at the earliest date; the signed delta after it.
       CASE WHEN rn = 1 THEN nav_value ELSE nav_value - previous_value END,
       NULL,   -- price_per_unit: a synthesised opening / in-kind transfer
       NULL,   -- consideration:  has no trade price (ADR-0097 §2)
       levels.currency,
       '{_MARKER}',
       'excel',
       levels.created_by   -- provenance mirrored from the source NAV row
  FROM levels
 -- A zero delta writes nothing (ADR-0103 §4) — an unchanged balance is not
 -- an event, and this is also what makes the first v32 import classify this
 -- ledger as unchanged rather than restate it.
 WHERE rn = 1
    OR nav_value <> previous_value;
"""


# Step 3 — one unity price per actual NAV date, guarded on the
# ``uq_instrument_prices_investment_date`` natural key so a date a premature
# v32 import already priced is left exactly as it is. Runs for every scoped
# row, including those whose ledger synthesis was skipped: prices and ledger
# are guarded independently, because either may pre-exist without the other.
_BACKFILL_UNITY_PRICES = f"""
INSERT INTO instrument_prices (
    tenant_id, investment_id, as_of_date, price, currency,
    source, ingest_origin, created_by
)
SELECT n.tenant_id,
       i.id,
       n.as_of_date,
       {_UNITY_PRICE},
       i.currency,      -- ADR-0097 §5: the position's own currency, never
       '{_MARKER}',     -- converted (the validation gate above pins it)
       'excel',
       n.created_by     -- provenance mirrored from the source NAV row
  FROM investments i
  JOIN investment_navs n ON n.investment_id = i.id
 WHERE {_IN_SCOPE}
   AND n.nav_kind = 'actual'
   AND n.ingest_origin IN ({_LEVEL_ORIGINS})
   AND NOT EXISTS (
         SELECT 1
           FROM instrument_prices p
          WHERE p.investment_id = i.id
            AND p.as_of_date = n.as_of_date);
"""


# Runs *before* the flip, so the scope predicate still selects the rows the
# migration just worked on. Pure reporting, and free — but see the module
# docstring: asyncpg swallows NOTICE, so this prints only under a psql-driven
# apply. The durable record is the marker, which stays queryable afterwards.
_REPORT = f"""
DO $$
DECLARE
    v_rows      bigint;
    v_openings  bigint;
    v_transfers bigint;
    v_prices    bigint;
    v_manual    bigint;
BEGIN
    SELECT count(*) INTO v_rows
      FROM investments i WHERE {_IN_SCOPE};

    SELECT count(*) FILTER (WHERE txn_type = 'opening'),
           count(*) FILTER (WHERE txn_type = 'transfer')
      INTO v_openings, v_transfers
      FROM position_transactions WHERE source = '{_MARKER}';

    SELECT count(*) INTO v_prices
      FROM instrument_prices WHERE source = '{_MARKER}';

    SELECT count(*) INTO v_manual
      FROM investments i
      JOIN investment_navs n ON n.investment_id = i.id
     WHERE {_IN_SCOPE}
       AND n.nav_kind = 'actual'
       AND n.ingest_origin = 'manual';

    RAISE NOTICE 'ADR-0103 §9: flipping % reported cash row(s) to unitised; '
                 'synthesised % opening(s) and % transfer(s), backfilled % '
                 'unity price(s). % manual-origin NAV row(s) were absorbed '
                 'as levels into the synthesised ledger.',
                 v_rows, v_openings, v_transfers, v_prices, v_manual;
END $$;
"""


_FLIP = f"""
UPDATE investments AS i
   SET valuation_mode = 'unitised'
 WHERE {_IN_SCOPE};
"""


def upgrade() -> None:
    # Gate first: a row the migration cannot represent aborts the whole run
    # before a single ledger row is written (abort, never repair).
    op.execute(_VALIDATE)
    op.execute(_SYNTHESISE_LEDGER)
    op.execute(_BACKFILL_UNITY_PRICES)
    op.execute(_REPORT)
    op.execute(_FLIP)
    # No materialisation call — ADR-0103 §9.5 makes it a provable no-op, and
    # migrations do not import the application package. See the module
    # docstring; the proof is the regression test and the operator's
    # post-apply verification.


def downgrade() -> None:
    # Order matters: identify the rows to flip back *by* their marker opening,
    # before the marker rows are deleted out from under the predicate.
    op.execute(
        f"""
        UPDATE investments AS i
           SET valuation_mode = 'reported'
         WHERE i.investment_type = 'cash'
           AND i.valuation_mode = 'unitised'
           AND EXISTS (
                 SELECT 1
                   FROM position_transactions t
                  WHERE t.investment_id = i.id
                    AND t.txn_type = 'opening'
                    AND t.source = '{_MARKER}');
        """
    )
    # A row this migration flipped without synthesising (it already had an
    # opening) has no marker and is therefore not reverted — documented
    # one-way residue, see the module docstring.
    op.execute(f"DELETE FROM position_transactions WHERE source = '{_MARKER}';")
    op.execute(f"DELETE FROM instrument_prices WHERE source = '{_MARKER}';")
