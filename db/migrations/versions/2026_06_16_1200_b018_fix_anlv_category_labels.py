# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Relabel the anlv_categories catalogue to the § 2 Abs. 1 AnlV statute.

Revision ID: b018_fix_anlv_category_labels
Revises: b017_historise_composition_wts
Create Date: 2026-06-16 12:00:00 UTC

Corrects the AnlV catalogue seeded by b010 per ADR-0083. The b010
seed (``INSERT ... ON CONFLICT DO NOTHING``) carried display
names/descriptions that do not match § 2 Abs. 1 AnlV (verified against
the consolidated statute of 18.04.2016, last amended 04.02.2026): ~13
of the 18 numbered entries were mislabelled (e.g. ``anlv_12`` "High
Yield" — Nr. 12 is *notierte Aktien*; ``anlv_15`` "Aktien" — Nr. 15 is
*OGAW*), and ``anlv_19`` "Edelmetalle" is fabricated (no Nr. 19 exists;
precious metals are admissible only via the Öffnungsklausel, Abs. 2).
Because the b010 seed is ``DO NOTHING``, the corrected fixture fixes
only fresh databases; this migration corrects already-seeded ones.

This is a **relabel-only** correction (ADR-0083):

* Codes are preserved. ``anlv_1`` … ``anlv_18`` keep their primary-key
  values, so ``investments.anlv_code`` foreign keys and
  ``limits.class_key`` snapshots (ADR-0056) stay intact. Only
  ``display_name``, ``description`` and ``paragraph_label`` change.
* The fabricated ``anlv_19`` is dropped — guarded: the delete counts
  references in ``investments`` and ``limits`` first and aborts loudly
  (``RuntimeError``) rather than skipping or force-deleting
  (no-silent-fallback, ADR-0005).
* Two regulatory buckets the BerVersV forms require are added:
  ``anlv_oeffnungsklausel`` (§ 2 Abs. 2) and ``anlv_genehmigung``
  (§ 2 Abs. 3).

The migration is **self-contained**: the corrected values, the prior
values (for ``downgrade``) and the two new buckets are literal lists in
this module — it does **not** re-read the JSON fixture, so the step
stays reproducible regardless of later fixture edits.

``anlv_categories`` is a global lookup table (no RLS, no audit trigger).
Migrations run under the privileged connection; no ``app.tenant_id``
GUC is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b018_fix_anlv_category_labels"
down_revision: str | None = "b017_historise_composition_wts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Literal catalogue data — self-contained, not read from the JSON fixture.
# ---------------------------------------------------------------------------

# Corrected § 2 Abs. 1 AnlV labels for the eighteen numbered codes
# (ADR-0083). paragraph_label is unchanged for these, but is rewritten
# alongside the names so the UPDATE is uniform.
_CORRECTED_NUMBERED: tuple[dict[str, str], ...] = (
    {
        "code": "anlv_1",
        "paragraph_label": "§ 2 Abs. 1 Nr. 1 AnlV",
        "display_name": "Grundpfandrechtlich gesicherte Forderungen",
        "description": "Forderungen, für die ein Grundpfandrecht an einem im EWR/OECD belegenen Grundstück oder grundstücksgleichen Recht besteht (Hypothekendarlehen).",
    },
    {
        "code": "anlv_2",
        "paragraph_label": "§ 2 Abs. 1 Nr. 2 AnlV",
        "display_name": "Wertpapierdarlehen und besicherte Forderungen",
        "description": "Forderungen, die durch Geldzahlung/Wertpapiere gesichert sind (Wertpapierdarlehen, Bst. a), durch Schuldverschreibungen nach Nr. 6 oder 7 gesichert sind (Bst. b) oder aus Rückversicherungs-Abrechnungsforderungen bestehen (Bst. c).",
    },
    {
        "code": "anlv_3",
        "paragraph_label": "§ 2 Abs. 1 Nr. 3 AnlV",
        "display_name": "Darlehen an Staaten und öffentliche Stellen",
        "description": "Darlehen an EWR-/OECD-Staaten, ihre Regionalregierungen und Gebietskörperschaften, internationale Organisationen sowie gewährleistete bzw. an Abwicklungsanstalten ausgereichte Darlehen (Bst. a–f).",
    },
    {
        "code": "anlv_4",
        "paragraph_label": "§ 2 Abs. 1 Nr. 4 AnlV",
        "display_name": "Darlehen an Unternehmen",
        "description": "Darlehen an Unternehmen mit Sitz im EWR/OECD ohne Kreditinstitute (Bst. a), Gesellschafter-Darlehen an Grundstücksgesellschaften (Bst. b) und andere besicherte Unternehmensdarlehen (Bst. c).",
    },
    {
        "code": "anlv_5",
        "paragraph_label": "§ 2 Abs. 1 Nr. 5 AnlV",
        "display_name": "Policendarlehen",
        "description": "Vorauszahlungen oder Darlehen auf eigene Versicherungsscheine bis zur Höhe des Rückkaufswerts.",
    },
    {
        "code": "anlv_6",
        "paragraph_label": "§ 2 Abs. 1 Nr. 6 AnlV",
        "display_name": "Pfandbriefe und gedeckte Schuldverschreibungen von Kreditinstituten",
        "description": "Pfandbriefe, Kommunalobligationen und andere Schuldverschreibungen von KI mit kraft Gesetzes bestehender besonderer Deckungsmasse.",
    },
    {
        "code": "anlv_7",
        "paragraph_label": "§ 2 Abs. 1 Nr. 7 AnlV",
        "display_name": "Börsennotierte Schuldverschreibungen",
        "description": "Schuldverschreibungen, die an einer Börse/einem organisierten Markt innerhalb (Bst. a, b) oder außerhalb des EWR (Bst. c) zugelassen oder einbezogen sind.",
    },
    {
        "code": "anlv_8",
        "paragraph_label": "§ 2 Abs. 1 Nr. 8 AnlV",
        "display_name": "Andere Schuldverschreibungen",
        "description": "Schuldverschreibungen, die nicht von Nr. 6 oder 7 erfasst sind.",
    },
    {
        "code": "anlv_9",
        "paragraph_label": "§ 2 Abs. 1 Nr. 9 AnlV",
        "display_name": "Nachrangige Verbindlichkeiten und Genussrechte",
        "description": "Forderungen aus nachrangigen Verbindlichkeiten gegen Unternehmen oder aus Genussrechten an Unternehmen (Bst. a Sitz EWR/OECD, Bst. b notiert).",
    },
    {
        "code": "anlv_10",
        "paragraph_label": "§ 2 Abs. 1 Nr. 10 AnlV",
        "display_name": "ABS, CLN und vergleichbare Kreditrisiko-Anlagen",
        "description": "Asset Backed Securities, Credit Linked Notes und andere Anlagen, deren Ertrag/Rückzahlung an Kreditrisiken gebunden ist oder die der Übertragung von Kreditrisiken dienen.",
    },
    {
        "code": "anlv_11",
        "paragraph_label": "§ 2 Abs. 1 Nr. 11 AnlV",
        "display_name": "Schuldbuchforderungen und Liquiditätspapiere",
        "description": "In das Schuldbuch des Bundes/eines Landes (oder ein entsprechendes EWR-/OECD-Verzeichnis) eingetragene Forderungen sowie Liquiditätspapiere i.S.v. § 42 Abs. 1 BBankG.",
    },
    {
        "code": "anlv_12",
        "paragraph_label": "§ 2 Abs. 1 Nr. 12 AnlV",
        "display_name": "Notierte Aktien",
        "description": "Voll eingezahlte Aktien, die an einer Börse/einem organisierten Markt innerhalb oder außerhalb des EWR zugelassen oder einbezogen sind.",
    },
    {
        "code": "anlv_13",
        "paragraph_label": "§ 2 Abs. 1 Nr. 13 AnlV",
        "display_name": "Beteiligungen (nicht notierte Anteile und geschlossene PE-AIF)",
        "description": "Bst. a: nicht notierte Aktien, GmbH-/Kommanditanteile und stille Beteiligungen an operativen Unternehmen im EWR/OECD. Bst. b: Anteile an inländischen geschlossenen AIF (Private Equity) und vergleichbaren ausländischen Vehikeln.",
    },
    {
        "code": "anlv_14",
        "paragraph_label": "§ 2 Abs. 1 Nr. 14 AnlV",
        "display_name": "Immobilien (direkt, REITs, Immobilienfonds)",
        "description": "Bst. a: Grundstücke, grundstücksgleiche Rechte und Grundstücksgesellschaften. Bst. b: REIT-AG oder vergleichbare Kapitalgesellschaften. Bst. c: Immobilien-Spezial-AIF / geschlossene Immobilien-Publikums-AIF.",
    },
    {
        "code": "anlv_15",
        "paragraph_label": "§ 2 Abs. 1 Nr. 15 AnlV",
        "display_name": "Offene Publikumsinvestmentvermögen (OGAW)",
        "description": "Anteile und Anlageaktien an inländischen offenen Publikumsinvestmentvermögen (OGAW) sowie vergleichbaren EU-OGAW.",
    },
    {
        "code": "anlv_16",
        "paragraph_label": "§ 2 Abs. 1 Nr. 16 AnlV",
        "display_name": "Offene Spezial-AIF mit festen Anlagebedingungen",
        "description": "Anteile und Anlageaktien an inländischen offenen Spezial-AIF nach § 284 KAGB (nicht von Nr. 14 Bst. c erfasst) sowie vergleichbaren EU-Spezial-AIF.",
    },
    {
        "code": "anlv_17",
        "paragraph_label": "§ 2 Abs. 1 Nr. 17 AnlV",
        "display_name": "Andere Investmentvermögen",
        "description": "Anteile und Aktien an inländischen Investmentvermögen, die nicht von Nr. 13 Bst. b, Nr. 14 Bst. c, Nr. 15 und Nr. 16 erfasst werden, sowie vergleichbare EU-Investmentvermögen.",
    },
    {
        "code": "anlv_18",
        "paragraph_label": "§ 2 Abs. 1 Nr. 18 AnlV",
        "display_name": "Anlagen bei Kreditinstituten",
        "description": "Anlagen bei EZB/Zentralnotenbanken (Bst. a), geeigneten Kreditinstituten (Bst. b), öffentlich-rechtlichen Kreditinstituten (Bst. c) und multilateralen Entwicklungsbanken (Bst. d). Laufende Guthaben gelten ebenfalls als Anlagen.",
    },
)

# The prior (pre-correction) labels, captured verbatim from the b010
# fixture so downgrade() restores the exact original state.
_PRIOR_NUMBERED: tuple[dict[str, str], ...] = (
    {
        "code": "anlv_1",
        "paragraph_label": "§ 2 Abs. 1 Nr. 1 AnlV",
        "display_name": "Darlehen mit hypothekarischer Sicherung",
        "description": "Hypothekarisch gesicherte Darlehen und Grundpfandrechte an Immobilien im EWR.",
    },
    {
        "code": "anlv_2",
        "paragraph_label": "§ 2 Abs. 1 Nr. 2 AnlV",
        "display_name": "Darlehen an öffentlich-rechtliche Schuldner",
        "description": "Darlehen an Bund, Länder, Kommunen und vergleichbare öffentlich-rechtliche Körperschaften.",
    },
    {
        "code": "anlv_3",
        "paragraph_label": "§ 2 Abs. 1 Nr. 3 AnlV",
        "display_name": "Darlehen an Unternehmen mit Sitz im EWR",
        "description": "Besicherte Darlehen an Unternehmen mit Sitz im Europäischen Wirtschaftsraum.",
    },
    {
        "code": "anlv_4",
        "paragraph_label": "§ 2 Abs. 1 Nr. 4 AnlV",
        "display_name": "Pfandbriefe und Schuldverschreibungen",
        "description": "Pfandbriefe, Kommunalobligationen und vergleichbar besicherte Schuldverschreibungen.",
    },
    {
        "code": "anlv_5",
        "paragraph_label": "§ 2 Abs. 1 Nr. 5 AnlV",
        "display_name": "Forderungen aus Namensschuldverschreibungen",
        "description": "Namensschuldverschreibungen, einschließlich nachrangiger Verbindlichkeiten.",
    },
    {
        "code": "anlv_6",
        "paragraph_label": "§ 2 Abs. 1 Nr. 6 AnlV",
        "display_name": "Forderungen aus Schuldscheindarlehen",
        "description": "Schuldscheindarlehen an erstklassige Schuldner.",
    },
    {
        "code": "anlv_7",
        "paragraph_label": "§ 2 Abs. 1 Nr. 7 AnlV",
        "display_name": "Asset-Backed-Securities und Credit-Linked-Notes",
        "description": "Strukturierte Kreditprodukte einschließlich ABS und CLN.",
    },
    {
        "code": "anlv_8",
        "paragraph_label": "§ 2 Abs. 1 Nr. 8 AnlV",
        "display_name": "Schuldscheindarlehen an inländische Kreditinstitute",
        "description": "Schuldscheindarlehen an inländische Kreditinstitute mit besonderer Aufsicht.",
    },
    {
        "code": "anlv_9",
        "paragraph_label": "§ 2 Abs. 1 Nr. 9 AnlV",
        "display_name": "Bezugsrechte und Genussrechte",
        "description": "Bezugsrechte, Genussrechte und vergleichbare Beteiligungsrechte.",
    },
    {
        "code": "anlv_10",
        "paragraph_label": "§ 2 Abs. 1 Nr. 10 AnlV",
        "display_name": "Inhaberschuldverschreibungen und Aktien des Bundes/EWR",
        "description": "Schuldverschreibungen und Aktien des Bundes, der Länder sowie EWR-Staaten und ihrer Untergliederungen.",
    },
    {
        "code": "anlv_11",
        "paragraph_label": "§ 2 Abs. 1 Nr. 11 AnlV",
        "display_name": "Schuldverschreibungen sonstiger Emittenten",
        "description": "Schuldverschreibungen sonstiger Emittenten an geregelten Märkten.",
    },
    {
        "code": "anlv_12",
        "paragraph_label": "§ 2 Abs. 1 Nr. 12 AnlV",
        "display_name": "Hochverzinsliche Schuldverschreibungen (High Yield)",
        "description": "Schuldverschreibungen unter Investment-Grade-Rating.",
    },
    {
        "code": "anlv_13",
        "paragraph_label": "§ 2 Abs. 1 Nr. 13 AnlV",
        "display_name": "Unternehmensbeteiligungen",
        "description": "Beteiligungen an Unternehmen mit Sitz im EWR (klassische Private-Equity-Beteiligungen).",
    },
    {
        "code": "anlv_14",
        "paragraph_label": "§ 2 Abs. 1 Nr. 14 AnlV",
        "display_name": "Immobilien",
        "description": "Grundstücke, Erbbaurechte sowie Beteiligungen an Immobiliengesellschaften.",
    },
    {
        "code": "anlv_15",
        "paragraph_label": "§ 2 Abs. 1 Nr. 15 AnlV",
        "display_name": "Aktien",
        "description": "An organisierten Märkten gehandelte Aktien (Listed Equities).",
    },
    {
        "code": "anlv_16",
        "paragraph_label": "§ 2 Abs. 1 Nr. 16 AnlV",
        "display_name": "Investmentvermögen",
        "description": "Anteile an OGAW und AIF nach KAGB.",
    },
    {
        "code": "anlv_17",
        "paragraph_label": "§ 2 Abs. 1 Nr. 17 AnlV",
        "display_name": "Sonstige Beteiligungsanlagen",
        "description": "Sonstige Beteiligungsanlagen, einschließlich PE-Dachfonds- und FoF-Strukturen.",
    },
    {
        "code": "anlv_18",
        "paragraph_label": "§ 2 Abs. 1 Nr. 18 AnlV",
        "display_name": "Laufende Guthaben und Einlagen bei Kreditinstituten",
        "description": "Bankguthaben, Termin- und Spareinlagen bei beaufsichtigten Kreditinstituten.",
    },
)

# The two regulatory buckets added by the correction (full INSERT rows).
_NEW_BUCKETS: tuple[dict[str, object], ...] = (
    {
        "code": "anlv_oeffnungsklausel",
        "paragraph_label": "§ 2 Abs. 2 AnlV",
        "display_name": "Öffnungsklausel",
        "description": "Anlagen, die in Abs. 1 nicht genannt sind oder dessen Voraussetzungen/Grenzen übersteigen; insgesamt auf 5 % des Sicherungsvermögens beschränkt (mit Genehmigung bis 10 %).",
        "sort_order": 190,
    },
    {
        "code": "anlv_genehmigung",
        "paragraph_label": "§ 2 Abs. 3 AnlV",
        "display_name": "Andere Kapitalanlagen mit Genehmigung der Aufsichtsbehörde",
        "description": "Anlagen in sonstigen Vermögenswerten bzw. Überschreitungen der Mischungs-/Streuungsgrenzen, die die Aufsichtsbehörde im Einzelfall gestattet.",
        "sort_order": 200,
    },
)

# The fabricated entry dropped by the correction (full INSERT row for
# downgrade re-insertion).
_ANLV_19: dict[str, object] = {
    "code": "anlv_19",
    "paragraph_label": "§ 2 Abs. 1 Nr. 19 AnlV",
    "display_name": "Edelmetalle",
    "description": "Physische und vergleichbar besicherte Edelmetallpositionen.",
    "sort_order": 190,
}


_anlv = sa.table(
    "anlv_categories",
    sa.column("code", sa.Text()),
    sa.column("paragraph_label", sa.Text()),
    sa.column("display_name", sa.Text()),
    sa.column("description", sa.Text()),
    sa.column("sort_order", sa.Integer()),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relabel(bind: sa.engine.Connection, rows: Sequence[dict[str, str]]) -> None:
    """Rewrite paragraph_label/display_name/description for known codes."""
    for row in rows:
        bind.execute(
            sa.update(_anlv)
            .where(_anlv.c.code == row["code"])
            .values(
                paragraph_label=row["paragraph_label"],
                display_name=row["display_name"],
                description=row["description"],
            )
        )


def _insert_ignore(bind: sa.engine.Connection, rows: Sequence[dict[str, object]]) -> None:
    """Insert catalogue rows, leaving any pre-existing code untouched."""
    bind.execute(
        postgresql.insert(_anlv).values(list(rows)).on_conflict_do_nothing(index_elements=["code"])
    )


def _reference_counts(bind: sa.engine.Connection, code: str) -> tuple[int, int]:
    """Count rows that still reference an AnlV code (investments + limits)."""
    inv = bind.execute(
        sa.text("SELECT count(*) FROM investments WHERE anlv_code = :code"),
        {"code": code},
    ).scalar_one()
    lim = bind.execute(
        sa.text(
            "SELECT count(*) FROM limits l "
            "JOIN limit_sets s ON s.id = l.limit_set_id "
            "WHERE s.family = 'anlv' AND l.class_key = :code"
        ),
        {"code": code},
    ).scalar_one()
    return int(inv), int(lim)


def _guarded_delete(bind: sa.engine.Connection, code: str) -> None:
    """Delete an AnlV code, aborting loudly if anything still references it."""
    inv, lim = _reference_counts(bind, code)
    if inv or lim:
        raise RuntimeError(
            f"Refusing to delete anlv_categories code {code!r}: still "
            f"referenced by {inv} investments(anlv_code) and {lim} "
            f"limits(class_key, family='anlv'). Re-tag those rows to a "
            f"valid AnlV code, then re-run the migration."
        )
    bind.execute(sa.delete(_anlv).where(_anlv.c.code == code))


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Correct the eighteen numbered codes to the § 2 Abs. 1 statute.
    _relabel(bind, _CORRECTED_NUMBERED)

    # 2. Add the Öffnungsklausel (Abs. 2) and Genehmigung (Abs. 3) buckets.
    _insert_ignore(bind, _NEW_BUCKETS)

    # 3. Drop the fabricated anlv_19 — guarded, never silent (ADR-0005).
    _guarded_delete(bind, "anlv_19")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse of upgrade, symmetrically.
    # 3'. Restore the fabricated anlv_19 entry.
    _insert_ignore(bind, [_ANLV_19])

    # 2'. Remove the two regulatory buckets — guarded the same way.
    for bucket in _NEW_BUCKETS:
        _guarded_delete(bind, str(bucket["code"]))

    # 1'. Restore the prior (pre-correction) labels for the numbered codes.
    _relabel(bind, _PRIOR_NUMBERED)
