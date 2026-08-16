# Phase 4 — Plan-Übersicht

- **Datum:** 2026-05-06
- **Branch:** `web-migration`
- **Tag (geplant):** `phase-4-complete` — gesetzt nach Sign-off durch den Projekt-Owner
- **Vorgängerphase:** Phase 3 (SAA-Web-Migration), abgeschlossen, Tag `phase-3-complete`
- **Folgephase (vorgesehen):** Phase 5 (Charts/Statistics-Web-Migration und GUI-Migration auf Postgres)

---

## 1. Zweck dieser Phase

Phase 4 trifft die zentrale Domain-Schema-Entscheidung der Web-Migration:
**das Investment-Domain-Schema** (vertagt aus ADR-0042 §1) plus den
**Excel-Import-Pfad** in das normalisierte Schema.

Phase 4 hat **eine** Architektur-Entscheidung mit Mehrjahres-Tragweite:
die Form des Investment-Schemas (flat polymorphic mit Type-Discriminator).
Alle anderen Phase-4-Inhalte sind Konsequenz-Entscheidungen oder
Implementierungs-Detail.

## 2. Was Phase 4 leistet

### 2.1 Schwerpunkt A — Investment-Domain-Schema

Drei neue Tabellen, alle tenant-scoped, alle RLS-protected, alle
audit-getriggert:

- **`investments`** — eine flache, polymorphe Tabelle für sieben
  Investment-Typen (PE, Private Debt, Real Estate, Infra Equity, Listed
  Equity, Listed Bonds, Other).
- **`investment_navs`** — Bewertungs-Historie pro Investment, mit
  Plan/Actual-Parallelität. Stichtags-basiert (`as_of_date DATE`).
- **`investment_cashflows`** — Cashflow-Ereignisse pro Investment, mit
  Plan/Actual-Parallelität. Zeitpunkt-basiert (`flow_timestamp TIMESTAMPTZ`).

Plus eine Bootstrap-Erweiterung: `portfoliflow bootstrap` legt pro
Tenant eine "Unclassified" Asset-Klasse an (idempotent), als Fallback
für Excel-Imports ohne explizite Asset-Class-Zuordnung.

### 2.2 Schwerpunkt B — Excel-Import in normalisierte Tabellen

Der Phase-2-Pfad (`data_uploads` / `data_upload_sheets`) bleibt
unverändert als Audit-Substrat. Phase 4 ergänzt einen
**asynchronen Transformations-Pfad** (B1):

- Neuer Endpoint `POST /api/data-uploads/{upload_id}/import-as-investments`.
- UI-Button "In Investments importieren" auf der Uploads-Liste.
- Validation-Preview vor Confirm.
- Replace-by-investment-Logik (B1.1): Pro Excel-Investment werden alle
  zugehörigen NAVs und Cashflows ersetzt.
- Soft-Delete-mit-Reaktivierung (B2.b): Fehlende Investments werden auf
  `is_active = FALSE` gesetzt, beim Wieder-Auftauchen automatisch reaktiviert.
- Idempotenz: Wiederholter Import desselben Uploads ist eine
  zustandsneutrale Operation.

### 2.3 FastAPI-CRUD-Surface für Investments

Read- und Write-Routes für Investments, NAV-History und Cashflows.
Diese Surface dient zwei Zwecken: erstens als Phase-4-Validierung des
Schemas (Use-Case-Druck-Test), zweitens als Debug-Zugang zur Korrektur
einzelner Datenpunkte ohne Excel-Re-Import.

### 2.4 Konsolidierung als eigener Schritt

Inspiriert vom Phase-3-Konsolidierungs-Muster: eigener Sub-Strang nach
der Implementation, vor dem Acceptance-Report.

### 2.5 Phase-4-Acceptance-Report

Drei konkrete Use-Cases aus der PE-FoF-Praxis des Projekt-Owners dienen als
Pflicht-Verifikations-Punkte (siehe `phase-4-acceptance-criteria.md`).

## 3. Was Phase 4 explizit *nicht* leistet

- **Keine Type-Spezifika auf Investment-Ebene.** Alle sieben Typen werden
  einheitlich behandelt. Typ-spezifische Analytics, Charts und Felder
  sind Gegenstand eines Re-Kickoffs nach Phase 5.
- **Keine Sektor-/Country-Breakdown-Normalisierung.** Diese Splits
  bleiben in Phase 4 als JSONB-Snapshot in `data_upload_sheets` und
  werden in Phase 5 (mit der Charts/Statistics-Migration) in eigene
  Tabellen gehoben.
- **Keine Plan-Versionierung.** Ein neuer Plan überschreibt den alten.
  Die Plan-Historie ist über den Audit-Log rekonstruierbar.
- **Keine Multi-Asset-Class-Splits.** 1:1-FK von `investments` auf
  `asset_classes`. Multi-Strategy-Funds werden bei p&p einer einzelnen
  Asset-Klasse zugeordnet (operative Konvention).
- **Keine GUI-Migration auf Postgres.** Phase 5 oder später. Die GUI
  bleibt in Phase 4 auf dem In-Memory-DataStore (ADR-0041 unverändert).
- **Keine Charts/Statistics-Web-Surfaces.** Phase 5.
- **Keine Portfolio-Review/PDF-Reporting-Migration.** Phase 5/6.
- **Keine SAA-Cashflow-Cross-Modul-Integration.** Kommt mit Phase-5-
  Cashflow-Forecasting und der Allokationsgrenzen-Funktion.
- **Keine Currency-Stammtabelle.** Currency bleibt Free-Form-Text mit
  ISO-4217-Konvention. FX-Umrechnung ist Phase-5+-Thema.
- **Keine Erweiterung der Excel-V2-Spec.** Wenn Phase 4 ein neues Feld
  braucht, das nicht in V2 ist, wird es als Phase-5-Folge-Issue dokumentiert.

## 4. Schema-Detail-Entscheidungen — konsolidiert

| # | Frage | Entscheidung | Begründung |
|---|---|---|---|
| 1 | Investment-Typ-Liste in Phase 4 | 7 Typen: PE, Private Debt, Real Estate, Infra Equity, Listed Equity, Listed Bonds, Other | Reale p&p-Portfoliostruktur, alle Typen analytisch zunächst identisch behandelt |
| 2 | Investment-Schema-Form | (a) flat polymorphic mit `investment_type` als Text + Check-Constraint | Phase 4 modelliert keine Type-Spezifika; Side-Tables wären Overhead ohne Gegenwert; erweiterbar via additive Spalten oder spätere Side-Tables |
| 3 | NAV-Persistenz | (D3b) eigene `investment_navs`-Tabelle mit Plan/Actual-Parallelität | Plan- und Actual-Reihen koexistieren; historische Stichtags-Reproduzierbarkeit ist Compliance-Substrat; Snapshot-only kann Plan/Actual nicht abbilden |
| 4 | Cashflow-Persistenz | (D4a) flache `investment_cashflows`-Tabelle mit `flow_type`- und `flow_kind`-Spalten | Konsistent mit (a); flexibel erweiterbar; Excel-Plan/Actual-Realität direkt abgebildet |
| 5 | Investment-zu-Asset-Klasse | (D5a) 1:1-FK | p&p investiert primär in Pure-Play-Funds, Multi-Strategy ist Ausnahme; spätere Migration zu (D5b) ist additiv möglich |
| 6 | Excel-Import-Pfad | (B1) asynchrone Transformation, (B1.1) Replace-by-investment, (B2.b) Soft-Delete mit Reaktivierung | Excel ist Eingangskanal, PortfoliFLOW ist Wahrheit-nach-Import; manuelle System-Edits sind Sonderfall |

### 4.1 Detailtabelle `investments`

```
id                  UUID PK
tenant_id           UUID NOT NULL → tenants(id)              [RLS]
name                TEXT NOT NULL
investment_type     TEXT NOT NULL CHECK IN (
                      'private_equity',
                      'private_debt',
                      'real_estate',
                      'infra_equity',
                      'listed_equity',
                      'listed_bonds',
                      'other'
                    )
asset_class_id      UUID NOT NULL → asset_classes(id) ON DELETE RESTRICT
manager_name        TEXT
region              TEXT
currency            TEXT NOT NULL                            [ISO 4217 Konvention]
vintage_year        INTEGER                                  [nullable]
commitment_amount   NUMERIC(20, 4)                           [nullable]
is_active           BOOLEAN NOT NULL DEFAULT TRUE
type_specific_data  JSONB                                    [nullable, Phase 4 ungenutzt]
created_by          UUID NOT NULL → users(id)
created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

UNIQUE (tenant_id, name)
INDEX (tenant_id, investment_type)
INDEX (tenant_id, asset_class_id)
INDEX (tenant_id, is_active)
```

### 4.2 Detailtabelle `investment_navs`

```
id            UUID PK
tenant_id     UUID NOT NULL → tenants(id)                    [RLS]
investment_id UUID NOT NULL → investments(id) ON DELETE CASCADE
as_of_date    DATE NOT NULL
nav_value     NUMERIC(20, 4) NOT NULL
currency      TEXT NOT NULL
nav_kind      TEXT NOT NULL CHECK IN ('plan', 'actual')
source        TEXT                                           [nullable]
created_by    UUID NOT NULL → users(id)
created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()

UNIQUE (investment_id, as_of_date, nav_kind)
INDEX (investment_id, as_of_date DESC)
INDEX (tenant_id, as_of_date)
```

### 4.3 Detailtabelle `investment_cashflows`

```
id              UUID PK
tenant_id       UUID NOT NULL → tenants(id)                  [RLS]
investment_id   UUID NOT NULL → investments(id) ON DELETE CASCADE
flow_timestamp  TIMESTAMPTZ NOT NULL                         [Default 12:00 UTC, wenn unbekannt]
flow_type       TEXT NOT NULL CHECK IN (
                  'capital_call',
                  'distribution',
                  'fee',
                  'carry',
                  'dividend',
                  'coupon',
                  'other'
                )
flow_kind       TEXT NOT NULL CHECK IN ('plan', 'actual')
amount          NUMERIC(20, 4) NOT NULL                      [vorzeichenbehaftet: Calls negativ, Distributions positiv]
currency        TEXT NOT NULL
description     TEXT                                         [nullable]
created_by      UUID NOT NULL → users(id)
created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()

[KEIN UNIQUE-Constraint — Mehrfach-Cashflows pro Investment/Zeitpunkt/Typ/Kind sind erlaubt]
INDEX (investment_id, flow_timestamp, flow_kind)
INDEX (tenant_id, flow_timestamp)
```

## 5. Sub-Strang-Aufteilung

Phase 4 wird in fünf sequenzielle Sub-Stränge aufgeteilt. Reihenfolge
ist verbindlich: 4a vor 4b vor 4c, mit explizitem Verifikations-Stop
nach 4a (Schema-Bugs später sind teuer).

### 5.1 Sub-Strang 4a — Schema, Repositories, Service

- Alembic-Migration `b006_add_investment_domain.py` mit den drei Tabellen
  inklusive RLS, Audit-Triggern, Indices, CHECK-Constraints,
  UNIQUE-Constraints.
- Drei ORM-Models unter `core/models/`: `Investment`, `InvestmentNav`,
  `InvestmentCashflow`.
- Drei Repository-Klassen unter `core/repositories/` mit DTO-Pattern
  analog zu Phase 3: `InvestmentRepository`, `InvestmentNavRepository`,
  `InvestmentCashflowRepository`.
- `InvestmentService` unter `services/investments/investment_service.py`
  mit Read/Write-Methodengruppen.
- Bootstrap-Erweiterung: idempotente Anlage einer "Unclassified"
  Asset-Klasse pro Tenant.
- Schema-Regression-Guard verifiziert (dynamisches `pg_class`-Scanning
  erfasst die neuen Tabellen automatisch).
- Cross-Tenant-Isolations-Tests für jede neue Tabelle.
- GUC-Test: Audit-Log-Einträge nach Schreib-Operationen haben
  `tenant_id` *und* `user_id`.
- Repository-Tests unter unprivilegierter `portfoliflow_app`-Rolle.
- ADR-0043 als `Accepted` mit den Detail-Entscheidungen aus Sektion 4.

**Verifikations-Stop nach 4a.** Der Projekt-Owner verifiziert Schema, Migration,
Repos, Service-Skelett, Tests grün, bevor 4b startet.

### 5.2 Sub-Strang 4b — Investment-CRUD-Web-Surface

- FastAPI-Routes unter `web/routes/investments.py`:
  - `GET /investments` — Listen-View (HTMX, Tabulator).
  - `GET /investments/{id}` — Detail-View mit NAV-Chart und Cashflow-Tabelle.
  - `POST /investments` — Investment anlegen.
  - `PUT /investments/{id}` — Investment aktualisieren.
  - `DELETE /investments/{id}` — Investment löschen (oder Soft-Delete via `is_active=FALSE`).
  - `POST /investments/{id}/navs` — NAV-Datenpunkt hinzufügen.
  - `PUT /investments/{id}/navs/{nav_id}` — NAV-Datenpunkt korrigieren.
  - `POST /investments/{id}/cashflows` — Cashflow hinzufügen.
  - `PUT /investments/{id}/cashflows/{cashflow_id}` — Cashflow korrigieren.
- HTMX-Partials unter `web/templates/investments/`.
- Plotly-Spec für NAV-Zeitreihe pro Investment unter
  `services/chart_specs/investment_nav_timeseries.py`.
- Tabulator für Investment-Liste (Filter nach Typ, Asset-Klasse, Status)
  und für Cashflow-Liste pro Investment.
- CSRF auf jeder schreibenden Route (analog Phase 3).
- Audit-Trail-Verifikation für jede Schreib-Operation.
- Web-HTTP-Tests mit `httpx.AsyncClient` und Sentinel-Auth.

### 5.3 Sub-Strang 4c — Excel-Import-Transformation

- `services/data_normalization/investment_extractor.py` mit:
  - `extract_from_upload(upload_id) -> InvestmentExtractionResult`.
  - Mapping-Logik V2-Excel-Spec → Investment-Schema (Investment-Attribute
    aus `Attributes`-Sheet, NAVs aus `NAVs actual` / `NAVs plan`,
    Cashflows aus den vier Cash-Flow-Sheets).
- Cross-Modul-API-Erweiterung in Phase 3:
  `AssetClassRepository.get_by_code()` wird eager implementiert (Phase-3-
  Disziplin: konkreter Konsument vorhanden).
- (B1.1)-Replace-Logik pro Investment: alle existierenden NAVs und
  Cashflows der Investment werden gelöscht und neu angelegt.
- (B2.b)-Soft-Delete-Logik: Investments im Tenant, die nicht in der
  Excel-Datei sind, werden auf `is_active=FALSE` gesetzt; Investments
  in der Excel-Datei werden auf `is_active=TRUE` gesetzt (Reaktivierung).
- Validierungs-Errors als strukturiertes Result, nicht als Exception:
  Der Nutzer sieht, was nicht importiert wurde und warum.
- Idempotenz: Wiederholter Import desselben Uploads erkennt Investment-
  Identität via `(tenant_id, name)` und schreibt zustandsneutral.
- Web-UI-Surface: Button "In Investments importieren" auf der
  `data_uploads`-Liste, Validation-Preview, Confirm/Cancel.
- Round-Trip-Tests: Test-Excel → Import → Verifikation gegen erwarteten
  Investment/NAV/Cashflow-Bestand.
- Cross-Tenant-Isolations-Test: Zwei Tenants mit derselben hochgeladenen
  Excel-Struktur — jeder sieht nur seine importierten Investments.

### 5.4 Sub-Strang 4d — Phase-4-Konsolidierung

Eigener Schritt nach 4a/4b/4c, vor dem Acceptance-Report. Aufgaben:

- ADR-0043 (und ggf. weitere Phase-4-ADRs) im
  `docs/adr/README.md`-Index eintragen, Anschluss-Absatz aktualisieren.
- Schema-Regression-Guard manuell verifizieren: Sind alle neuen Tabellen
  erfasst? Haben alle RLS+`FORCE ROW LEVEL SECURITY`+Audit-Trigger?
- Repo-Aufräumen: Verwaiste Test-Artefakte, halb-fertige Files, alte
  TODO-Kommentare entfernen.
- Test-Coverage-Check: Welche Pfade haben Tests, welche nicht? Lücken
  füllen, soweit substantiell.
- Conventional-Commit-Disziplin nachprüfen: Sind alle Phase-4-Commits
  sauber gruppiert?
- `CLAUDE.md` ggf. um Glossar-Erweiterungen ergänzen: `Investment`,
  `Investment Type`, `NAV`, `Cashflow`, `flow_kind`, `nav_kind`, etc.

### 5.5 Sub-Strang 4e — Phase-4-Acceptance-Report

Siehe `phase-4-acceptance-criteria.md` als Template.

- Funktional-Tests gegen die drei Use-Cases des Projekt-Owners.
- Schema-Visualisierung als ER-Diagramm-Anhang.
- Cross-Tenant-Isolations-Beweis: Zwei konkrete Test-Tenants, jeder mit
  eigenem Investment-Bestand, Demonstration der Isolation.
- Performance-Sanity-Check: `GET /investments` mit 100 Investments
  unter 200ms? Index-Tuning bei Bedarf.
- Strangler-Asymmetrie dokumentieren: Investments im Web ≠ Investments
  in der GUI (siehe `demo-stability-checklist.md`, kommt in Turn 3).

## 6. Test-Disziplin (unverändert von Phase 3, plus Phase-4-Spezifika)

1. Repository-Tests unter unprivilegierter `portfoliflow_app`-Rolle.
2. Qt-frei-Regression-Guard für `ai_service_core.py` bleibt.
3. Schema-Regression-Guard erweitert um die drei Phase-4-Tabellen.
4. Web-no-PersistentDataStore-Regression-Guard bleibt aktiv.
5. Cross-Tenant-Isolations-Tests für *jede* neue Tabelle.
6. GUC-Test: Audit-Log-Einträge nach Schreib-Operationen haben
   `tenant_id` *und* `user_id`.
7. Web-HTTP-Tests mit `httpx.AsyncClient`.
8. Stop-Token-Stripper-Tests bleiben grün.
9. **Neu in Phase 4:** Use-Case-basierte Funktionaltests (jeder der drei
   Use-Cases aus dem Acceptance-Report ist ein dokumentierter
   Acceptance-Test).
10. **Neu in Phase 4:** Excel-Import-Round-Trip-Tests für Schwerpunkt B,
    inklusive Replace-Logik, Soft-Delete-mit-Reaktivierung,
    Idempotenz, Asset-Class-Auflösung mit Fallback auf "Unclassified".

## 7. Konventionen (unverändert von Phase 3)

- **Decider in allen ADRs und Revision-History-Einträgen:** `PortfoliFLOW project owner`.
- **Branch:** `web-migration`. `main` bleibt demo-stabil auf Phase-1-Stand.
- **Commits:** Conventional Commits in Englisch. Mehrere kleine Commits.
- **Sprache:** Deutsch in der Diskussion. Code, Commits, ADRs in Englisch.
- **Sentinel-User-Identitäten strikt getrennt** (Postgres-Superuser,
  OS-Service-Account, Sentinel-App-User — drei Identitäten).

## 8. ADR-Status nach Phase 4

Erwarteter ADR-Status nach Phase-4-Abschluss:

- **Neu in Phase 4 als `Accepted`:** ADR-0043 (Investment Domain Schema
  and Excel Transformation).
- **Übergang von `Proposed` zu `Accepted`:** Keiner in Phase 4 — die
  drei verbleibenden `Proposed`-ADRs (0019, 0033, 0039) gehen erst mit
  Phase-5-Abschluss auf `Accepted`.
- **Neue `Superseded`-Einträge:** Keine erwartet.

## 9. Demo-Stabilität-Notiz

Während Phase 4 ist die Strangler-Asymmetrie weiter aktiv und wird durch
Phase 4 sogar **verschärft**: Investments können jetzt im Web angelegt
werden, sind aber für die GUI unsichtbar. Eine Demo, die GUI und Web
gleichzeitig zeigt, muss Test-Investments in beiden Welten getrennt
anlegen oder die Demo komplett im Web ausführen.

Eine vollständige Demo-Stabilität-Checkliste folgt in Turn 3 dieser
Sitzung als eigenes Dokument.

## 10. Energie-Disziplin

Der Projekt-Owner hat in der Eröffnungs-Diskussion gesagt: *"Ich halte selbst an,
wenn ich zu erschöpft bin. Wir haben keine Deadline."* Diese Phase wird
in seinem Tempo durchgeführt. Wenn ein Sub-Strang substanziell länger
dauert als erwartet, wird das nicht als Verzögerung gewertet, sondern
als legitime Iteration.

Die Phase-3-Tempo-Reduktions-Lessons gelten weiter:
1. Konsolidierungs-Phase als eigener Schritt (4d).
2. YAGNI bei Cross-Modul-API.
3. Iterations-Schleifen sind legitim, gerade beim Schema.
