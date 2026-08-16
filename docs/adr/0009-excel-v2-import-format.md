# ADR-0009: Excel V2 Multi-Sheet Import Format with Dynamic Column Discovery

- **Status:** Accepted
- **Date:** 2026-04-24
- **Deciders:** PortfoliFLOW project owner (retrofit)
- **Tags:** data, integration

---

## Context

Portfolio data enters PortfoliFLOW from Excel workbooks maintained outside the application. The number of investments held by a fund-of-funds changes over time (new commitments, divestments), and the back office must be able to reflect those changes by editing the Excel file rather than the application code. A naïve import (fixed column count, hardcoded ranges) would couple the application's release cycle to the back-office workflow — every new investment would require a code change.

In addition, the workbook has to express several different kinds of information (static metadata, daily time series for cash flows / NAVs / returns, market reference series such as interest rates), and the import code must distinguish them and validate them appropriately.

## Decision

PortfoliFLOW adopts the **Excel V2** import format. A V2 workbook contains 10 named sheets organised into three categories:

1. **Attributes** (`Attributes`) — investment metadata, key-value rows, no dates.
2. **Investment time-series** — eight sheets (`Cash Flow In actual/plan`, `Cash Flow Out actual/plan`, `NAVs actual/plan`, `total return actual/plan`). All eight share the same column namespace (investment names) and are validated for cross-sheet column consistency.
3. **Market reference data** — currently `interest rates` (daily decimal annual rates). Each market reference sheet has its own independent column namespace and is excluded from the investment-column consistency check.

Structural invariants:

- Column A is always the label/date column. All columns to the right are data.
- Row 1 contains column names. The number of data columns is **discovered dynamically** by scanning row 1 from B rightward to the first empty cell. It is never hardcoded.
- For investment sheets, row 2 contains investment-type labels and row 3 contains sub-class labels. Placeholder columns may carry the literal placeholders `Typ der Investition` / `Klasse der Investition`.
- For market reference sheets, rows 2 and 3 are empty.
- Rows 4 onward contain data (key-value attributes or date-indexed time series).
- Empty investment columns are valid placeholder slots and are not dropped.
- Empty "plan" sheets are accepted (they represent unfilled future projections).

Implementation lives in `modules/front_office/data_import.py` and exposes the `load_excel`, `validate_dataframe`, and `validate_workbook` functions in addition to the registered `DataImport` module.

## Rationale

- Dynamic column discovery decouples adding investments (a back-office activity) from changing the importer (a development activity).
- The three-category split — Attributes vs. Investment time-series vs. Market reference — separates concerns that have genuinely different shapes and validation rules; collapsing them into a single shape would force ad-hoc special cases.
- Sharing a single column namespace across investment time-series sheets makes it possible to validate consistency cross-sheet (an investment that exists in `NAVs actual` must exist in `total return actual`).
- Excluding market reference sheets from the investment-column check leaves room to add benchmark indices, FX rates, etc., without polluting the investment namespace.
- Accepting empty "plan" sheets reflects how investment plans are filled in over time.

## Alternatives Considered

- **Fixed column layout:** Rejected — every investment change would require an importer release.
- **CSV per sheet:** Rejected — back office maintains data in Excel; converting to CSV would add a fragile preprocessing step outside PortfoliFLOW's control.
- **Single flat sheet (long format):** Rejected — does not match the way the source data is currently maintained, and would require behavioural changes from the back office.
- **Read column count from a metadata cell:** Rejected — adding a metadata cell would be one more place users could get wrong; scanning row 1 is self-documenting.

## Consequences

### Positive

- Adding investments requires editing only the Excel file.
- Cross-sheet validation catches structural mistakes before they reach downstream modules.
- Market reference data is first-class, so analytics can use risk-free rates / benchmarks without bespoke import code.

### Negative

- The format imposes constraints on Excel maintainers (sheet names, header rows, column A reservation). Mistakes in the workbook surface as `DataImportError` / `ValidationError` and require human correction.
- Dynamic discovery means an empty cell in the wrong place silently truncates the column scan; this is documented but is a class of error users may still hit.

### Neutral / Follow-ups

- New market reference sheets (benchmarks, FX rates) can be added by extending `RECOGNIZED_SHEETS` in the importer.
- Long-term, GP report ingestion (Report Scraper, planned) and the DataVault (ADR-0017) will reduce reliance on manually maintained Excel — but Excel will remain a supported entry path.

## Implementation Notes

- Implementation: `modules/front_office/data_import.py` (`load_excel`, `validate_dataframe`, `validate_workbook`, `DataImport`).
- Data persistence path: results are stored in the DataStore (ADR-0004) under canonical snake_case keys (e.g. `navs_actual`, `interest_rates`).
- Documented in: `CLAUDE.md` ("Excel data format V2"), with an explicit "what not to do" list.
- Tests: `tests/front_office/test_data_import.py` (path conventional; verify presence).

## Compliance & Audit Relevance

- **ISO 25010 quality attributes affected:** Reliability (input validation at the system boundary), Maintainability (importer is decoupled from data shape).
- **Audit evidence:** Source code of the importer and validators; sample workbook under `data/sample/`.

## References

- ADR-0004 (DataStore — destination of imported DataFrames)
- ADR-0017 (Planned DataVault — eventual persistent home for imported data)

---

## Revision History

| Date       | Author                                | Change                                                             |
|------------|---------------------------------------|--------------------------------------------------------------------|
| 2026-04-24 | PortfoliFLOW project owner (retrofit) | Initial draft. Retrofitted from existing code and documentation; the V2 format predates this ADR. |
| 2026-05-04 | PortfoliFLOW project owner            | Phase-2 Sub-Strang 2d adds a second consumer of the V2 format: the FastAPI Excel-import endpoint (`web/routes/data_import.py`) reuses `load_excel()` from `modules/front_office/data_import.py` (the parser is persistence-agnostic — it returns `dict[str, pd.DataFrame]`) and writes the result through the new `DataUploadRepository` into Postgres `data_uploads` / `data_upload_sheets`. The PyQt6 GUI continues to use the same `load_excel()` and writes into the in-memory `DataStore`; only the persistence-write layer is duplicated. Convergence is held open as a Phase-4 refactor under ADR-0041 §3. No format change. |
