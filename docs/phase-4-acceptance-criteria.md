# Phase 4 Acceptance Criteria

- **Status:** Template (wird zum Acceptance-Report von Sub-Strang 4e)
- **Phase:** 4 — Investment Domain Schema und Excel-Transformation
- **Branch:** `web-migration`
- **Tag (geplant):** `phase-4-complete` — gesetzt nach Sign-off durch den Projekt-Owner

---

## 1. Zweck dieses Dokuments

Dieses Dokument hält die Acceptance-Kriterien für Phase 4 fest, gegen
die der Phase-4-Acceptance-Report (Sub-Strang 4e) verifiziert wird.

Phase 4 hat **keine PyQt6-Pendant-Surface** für direkten
Side-by-Side-Vergleich (das Investment-Domain-Schema ist neu in der
Web-Welt). Stattdessen wird Phase 4 gegen drei konkrete Use-Cases aus
der PE-FoF-Praxis des Projekt-Owners verifiziert. Jeder Use-Case wird als
funktionaler End-to-End-Test exerziert.

## 2. Use-Cases als Pflicht-Verifikations-Punkte

Die folgenden drei Use-Cases stammen aus der Phase-4-Eröffnungs-
Diskussion und repräsentieren reale Workflows in der Praxis des
Projekt-Owners bzw.
in p&p. Sie sind Phase-4-Pflicht; das Schema, der Excel-Import, und
die CRUD-Surface werden gegen sie geprüft.

### 2.1 Use-Case A — Geplante vs. tatsächliche Cashflows (Plan/Actual-Parallelität)

**Situation in der Praxis des Projekt-Owners:**
> *"Es gibt eine Reihe von Zahlungsprofilen von Fonds, die entweder
> geschätzt sind oder von Fondsmanagern prognostiziert und zur Verfügung
> gestellt. Daraus leiten wir alle wesentlichen Kennzahlen des aktuellen
> und des zukünftigen, erwarteten Portfolios ab. Es gibt gestaffelte
> Fälligkeiten, die als geplante Cash Outflows abgebildet werden, es gibt
> erwartete Rückzahlungen, und dann von beidem noch einmal die Ausprägung
> in der Realität, die nach und nach die Planung überschreibt."*

**Phase-4-Verifikation:**

Der Acceptance-Report dokumentiert für ein Test-Investment (z.B. einen
fiktiven "Permira VII PE Fund"):

- **2.1.1** Plan-Cashflows werden aus Excel-Import importiert
  (`flow_kind = plan` für Capital Calls 2024–2030).
- **2.1.2** Actual-Cashflows werden zusätzlich importiert
  (`flow_kind = actual` für realisierte Capital Calls 2024).
- **2.1.3** Beide Reihen koexistieren parallel im
  `investment_cashflows`-Bestand. Eine SQL-Abfrage liefert für das
  Test-Investment die Plan-Reihe für 2024–2030 *und* die Actual-Reihe
  für 2024 (überlappende Datumsbereich).
- **2.1.4** Plan-Reihen werden weder durch Actual-Datenpunkte
  überschrieben noch gelöscht.
- **2.1.5** Re-Import der Excel-Datei ohne Änderungen produziert
  zustandsneutralen Effekt (Idempotenz). Der `audit_log` zeigt zwar
  DELETE+INSERT pro Cashflow-Reihe (Replace-Logik), aber der finale
  Datenstand ist identisch zum Vor-Import-Stand.

**Aktzeptanz erfolgt, wenn alle fünf Punkte beobachtbar dokumentiert
sind.**

### 2.2 Use-Case B — Foundation für die Allokationsgrenzen-Frage

**Situation in der Praxis des Projekt-Owners:**
> *"Haben wir noch Platz für Investment X? Dann muss man, wie in (A)
> beschrieben, per Hand geplante Cash Flows addieren, um dann
> festzustellen, dass man antworten muss 'dann reißen wir am Ende des
> übernächsten Jahres die Anlagegrenze für Private Equity Small to
> Medium'."*

**Hinweis zum Phase-4-Scope:** Die vollständige Allokationsgrenzen-Frage
wird in Phase 5+ implementiert (Cashflow-Forecasting plus Aggregation
gegen die SAA-Konfiguration). **Phase 4 verifiziert nur, dass das
Schema die nötigen Daten enthält, um die Frage in Phase 5+ zu
beantworten.**

**Phase-4-Verifikation:**

Der Acceptance-Report demonstriert:

- **2.2.1** Plan-Cashflows pro Investment sind einzeln abfragbar nach
  `investment_id`, `flow_kind = plan`, `flow_timestamp` zwischen heute
  und einem Zukunfts-Stichtag.
- **2.2.2** Investments sind einer Asset-Klasse zugeordnet
  (`investments.asset_class_id`). Eine Aggregations-Query liefert pro
  Asset-Klasse die Summe der geplanten Capital Calls in einem
  Zeitraum.
- **2.2.3** Die Asset-Klassen-Definition aus Phase 3 (`asset_classes`)
  bleibt unverändert nutzbar; die SAA-Konfiguration und ihre Bandbreiten
  sind in Phase 5+ überlagerbar.
- **2.2.4** Eine SQL-Beispiel-Query, die "geplante Capital Calls für PE
  Small-Mid Cap im Zeitraum 2026–2027" liefert, ist im Acceptance-Report
  dokumentiert. Die Query muss nicht produktiv eingebunden sein
  (Phase-5-Arbeit), aber sie muss funktional gegen das Phase-4-Schema
  laufen.

**Akzeptanz erfolgt, wenn die SQL-Query gegen reale Test-Daten ein
plausibles Ergebnis liefert und im Report dokumentiert ist.**

### 2.3 Use-Case C — TVPI/DPI/IRR pro Fonds, aktuell und erwartet

**Situation in der Praxis des Projekt-Owners:**
> *"Wie sind denn jetzt TVPI, DPI und IRR für Fonds X gerade im Moment?
> Und wo erwarten wir, dass es am Ende rauskommt?"*

**Hinweis zum Phase-4-Scope:** Die Phase-5-Charts/Statistics-Migration
implementiert die TVPI/DPI/IRR-Anzeige in der Web-UI. **Phase 4
verifiziert nur, dass das Schema die nötigen Daten enthält und dass
TVPI, DPI, IRR daraus deterministisch berechenbar sind.**

**Phase-4-Verifikation:**

Der Acceptance-Report demonstriert:

- **2.3.1** Für ein Test-Investment liefert das Schema:
  - alle historischen Capital Calls (`flow_type = capital_call`,
    `flow_kind = actual`),
  - alle historischen Distributions (`flow_type = distribution`,
    `flow_kind = actual`),
  - die aktuellste Actual-NAV (`nav_kind = actual`,
    `as_of_date = MAX(as_of_date)`),
  - die letzte Plan-NAV als Erwartungs-Endwert (`nav_kind = plan`,
    `as_of_date = MAX(as_of_date)` der Plan-Reihe).
- **2.3.2** TVPI = (NAV_actual + cumulative_distributions_actual) /
  cumulative_calls_actual ist berechenbar. Eine Beispiel-Berechnung
  ist im Report dokumentiert.
- **2.3.3** DPI = cumulative_distributions_actual /
  cumulative_calls_actual ist berechenbar.
- **2.3.4** IRR ist berechenbar via
  `services.reporting.data_providers._calculations.compute_irr` (oder
  äquivalente Logik), gefüttert mit den Phase-4-Cashflow-Daten plus
  der aktuellsten NAV.
- **2.3.5** TVPI- und IRR-Erwartungs-Endwerte (basierend auf
  Plan-Reihen) sind ebenfalls berechenbar — analog zu (2.3.1)–(2.3.4),
  aber mit `flow_kind = plan` und der letzten Plan-NAV.

**Akzeptanz erfolgt, wenn TVPI, DPI und IRR für mindestens ein
Test-Investment gegen erwartete Werte berechnet und im Report
dokumentiert sind.**

## 3. Schema-Strukturelle Acceptance-Punkte

### 3.1 Schema-Regression-Guard

`tests/regression/test_rls_schema_invariants.py` zeigt grün für die
drei neuen Phase-4-Tabellen:

- `investments` hat `relrowsecurity` und `relforcerowsecurity` gesetzt.
- `investment_navs` hat `relrowsecurity` und `relforcerowsecurity` gesetzt.
- `investment_cashflows` hat `relrowsecurity` und `relforcerowsecurity` gesetzt.
- Jede Tabelle hat mindestens eine RLS-Policy.
- Jede Tabelle hat einen Audit-Trigger mit Verweis auf
  `audit_trigger_function`.

### 3.2 Cross-Tenant-Isolation

Für jede neue Tabelle existiert mindestens ein Test, der demonstriert:

- Tenant A kann eigene Investments/NAVs/Cashflows lesen.
- Tenant A kann fremde Investments/NAVs/Cashflows nicht lesen
  (`SELECT` liefert leeres Ergebnis trotz vorhandener Daten in
  Tenant B).
- Tenant A kann nicht gegen Tenant-B-Daten schreiben (`WITH CHECK`
  blockiert; Postgres meldet RLS-Verstoß).

### 3.3 Audit-Log-Vollständigkeit

`tests/repositories/test_investment_audit_and_isolation.py` (oder
analoge Tests pro Tabelle) demonstrieren:

- Jeder INSERT in eine Phase-4-Tabelle erzeugt eine `audit_log`-Zeile
  mit `operation = 'INSERT'`, `tenant_id` gesetzt, `user_id` gesetzt
  (nicht NULL), und `new_data` als JSONB-Snapshot der eingefügten Zeile.
- Jeder UPDATE erzeugt analoge Zeile mit `operation = 'UPDATE'`,
  `old_data` und `new_data`.
- Jeder DELETE erzeugt Zeile mit `operation = 'DELETE'` und `old_data`.

### 3.4 GUC-Test: tenant_id und user_id

Ein Test demonstriert: Wenn `tenant_context(tenant_id, user_id)`
korrekt aufgerufen wird, landen `tenant_id` *und* `user_id` in den
`audit_log`-Einträgen. Wenn entweder GUC fehlt, schlägt entweder die
RLS-Policy fehl (kein Schreiben möglich) oder der Audit-Trigger meldet
einen `app.user_id`-Fehler.

## 4. Web-CRUD-Surface Acceptance-Punkte (Sub-Strang 4b)

### 4.1 Read-Routes

- `GET /investments` zeigt eine Liste aller aktiven Investments des
  aktuellen Tenants. Filter nach Investment-Typ und Asset-Klasse
  funktionieren.
- `GET /investments/{id}` zeigt Investment-Details inklusive NAV-
  Zeitreihe (Plan und Actual) als Plotly-Chart und Cashflow-Liste
  (Tabulator).

### 4.2 Write-Routes

Jede schreibende Route:
- erfordert CSRF-Token (per Phase-3-Konvention),
- erzeugt einen `audit_log`-Eintrag mit korrekt gesetztem `user_id`,
- ist gegen Cross-Tenant-Schreiben isoliert (Test: Tenant A versucht
  Investment in Tenant B zu modifizieren → 404).

### 4.3 NAV- und Cashflow-Manipulation

- `POST /investments/{id}/navs` erlaubt Hinzufügen einer einzelnen
  NAV-Bewertung mit `nav_kind` und `as_of_date`.
- `PUT /investments/{id}/navs/{nav_id}` erlaubt Korrektur einer
  einzelnen Bewertung.
- Analoge Routen für Cashflows.

## 5. Excel-Import-Surface Acceptance-Punkte (Sub-Strang 4c)

### 5.1 Round-Trip-Test

Eine Test-Excel-Datei mit:
- 5 Investments (mindestens drei verschiedene Investment-Typen),
- Plan- und Actual-NAVs für jedes Investment,
- Plan- und Actual-Cashflows (mindestens Capital Calls und Distributions),

wird hochgeladen und transformiert. Der finale Datenbank-Stand wird
gegen den erwarteten Stand verifiziert: alle 5 Investments existieren,
alle NAVs sind in der korrekten Plan/Actual-Aufteilung gespeichert,
alle Cashflows haben korrekte Vorzeichen und Plan/Actual-Trennung.

### 5.2 Replace-Logik

Eine zweite Excel-Datei mit modifizierten Cashflow-Werten für eines
der 5 Investments wird hochgeladen und transformiert. Verifikation:
die alten Cashflow-Werte sind weg, die neuen sind da, der `audit_log`
zeigt DELETE+INSERT.

### 5.3 Soft-Delete mit Reaktivierung

Eine dritte Excel-Datei, die nur 4 der 5 Investments enthält
(Investment X fehlt), wird hochgeladen. Verifikation: Investment X ist
auf `is_active = FALSE` gesetzt; seine NAVs und Cashflows sind weiter
da. Eine vierte Excel-Datei, die wieder alle 5 Investments enthält,
reaktiviert Investment X (`is_active = TRUE`).

### 5.4 Validierungs-Errors

Eine Excel-Datei mit absichtlichen Fehlern (z.B. ein Cashflow mit
fehlerhaftem Vorzeichen, eine NAV mit nicht-numerischem Wert) wird
hochgeladen. Verifikation: das `ImportResult` listet die Fehler
strukturiert auf, die fehlerhaften Zeilen werden nicht importiert,
die korrekten Zeilen werden importiert.

### 5.5 Asset-Class-Fallback

Eine Excel-Datei mit einem Investment ohne Asset-Class-Eintrag wird
hochgeladen. Verifikation: das Investment landet in der
"Unclassified" Asset-Klasse des Tenants. Bootstrap-idempotenz: ein
zweiter Bootstrap-Aufruf legt die Asset-Klasse nicht erneut an.

### 5.6 Cross-Tenant-Isolation für Excel-Import

Zwei Tenants A und B laden jeweils eine Excel-Datei mit 5 Investments
hoch (gleiche Investment-Namen, andere Daten). Verifikation: Tenant A
sieht nur seine 5 Investments, Tenant B sieht nur seine 5. Der
Investment-Name ist nicht global eindeutig — die `UNIQUE
(tenant_id, name)`-Constraint erlaubt denselben Namen in zwei
verschiedenen Tenants.

## 6. Performance-Sanity-Check

Ein Test-Tenant mit 100 Investments, jeweils 20 NAV-Datenpunkten und
50 Cashflows wird angelegt. Anschließend:

- `GET /investments` — Listen-Render unter 200 ms (Tabulator-Daten plus
  HTML-Rendering).
- `GET /investments/{id}` — Detail-Render unter 300 ms (inklusive
  NAV-Chart und Cashflow-Tabelle).

Wenn ein Endpoint signifikant langsamer ist: Index-Tuning vor
Phase-Abschluss. Die Indices aus dem Schema (auf `tenant_id`,
`investment_id`, `flow_timestamp`, `as_of_date`) sind die erste
Anlaufstelle.

## 7. Acceptance-Report-Anhänge

### 7.1 ER-Diagramm

Schematische Darstellung der drei neuen Tabellen plus ihrer Relationen
zu Phase-3-Tabellen (`asset_classes`) und Phase-1/2-Tabellen
(`tenants`, `users`). Erstellt mit dem Werkzeug der Wahl
(dbdiagram.io, draw.io, oder eine Plotly-basierte Skizze).

### 7.2 Use-Case-A-Walkthrough

Schritt-für-Schritt-Demonstration: Excel hochladen, Plan-Cashflows
sehen, zweites Excel mit Actual-Cashflows hochladen, Plan und Actual
parallel im System sehen.

### 7.3 Use-Case-B-Query

Die SQL-Query, die geplante Capital Calls pro Asset-Klasse aggregiert,
inklusive Beispiel-Output gegen das Test-Tenant-Set.

### 7.4 Use-Case-C-Berechnung

TVPI-, DPI-, und IRR-Berechnung für ein Test-Investment mit
nachvollziehbaren Eingangsdaten und Ergebnis-Werten.

### 7.5 Cross-Tenant-Isolations-Beweis

Konkrete Demonstration: zwei Test-Tenants, jeder mit eigenem
Investment-Bestand, gegenseitige Unsichtbarkeit nachgewiesen.

### 7.6 Strangler-Asymmetrie-Notiz

Hinweis auf die operative Realität: Phase-4-Investments im Web sind
in der GUI unsichtbar. Querverweis auf
`docs/demo-stability-checklist.md` (kommt in Turn 3 dieser Sitzung).

## 8. Sign-off

| Rolle                     | Name                       | Datum      | Ergebnis |
|---------------------------|----------------------------|------------|----------|
| Reporter                  | (Sub-Strang 4e Bearbeiter) | (pending)  | (pending) |
| PortfoliFLOW project owner | ProcessReengineer         | (pending)  | (pending) |

Nach dem Sign-off durch den Projekt-Owner wird der Head von `web-migration` als
`phase-4-complete` getaggt, ADR-0043 erhält einen Revision-History-
Eintrag "Phase-4 complete", und das ADR-Index-README wird aktualisiert.
