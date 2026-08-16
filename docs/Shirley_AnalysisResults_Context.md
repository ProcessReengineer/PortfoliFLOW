# Analysis-Result Datasets — Context for Shirley

When the user runs an analysis in PortfoliFLOW, the result is written to
the in-memory DataStore under a name beginning with `analysis_results.`.
You can list these via the `list_analysis_results` tool, inspect a
specific one via `get_dataset_summary`, and read concrete rows via
`get_dataset_slice`. The schemas below are stable; you can rely on the
column names.

## `analysis_results.fo_optimizer.tangency`

The user's **target allocation** under the maximum-Sharpe (tangency)
objective, computed by the Front Office Portfolio Optimizer.

Columns: `asset` (fund name), `weight` (target allocation, sum-to-one),
`expected_return`, `volatility`, `sharpe_ratio` (these three are
portfolio-level — repeated identically on every row).

Metadata: `producer="fo_optimizer"`, `result_type="tangency"`,
`computed_at` (UTC ISO 8601), `risk_free_rate`, `n_assets`,
`asset_columns`.

## `analysis_results.fo_optimizer.min_var`

The user's **target allocation** under the minimum-variance objective.
Same schema and metadata as `.tangency`, except `result_type="min_var"`.

## `analysis_results.fo_optimizer.current`

The user's **current actual allocation** — the NAV-weighted portfolio
derived from the most recent reported NAVs. Same schema as `.tangency`.
This dataset is only present when NAV data is available for every fund
in the optimisation universe; if NAVs are missing, the dataset is
absent and you should say so rather than fabricate one.

When both `.current` and `.tangency` are present, you can answer
"what should I do to move from my current portfolio toward the target?"
by comparing the `weight` columns row-by-row. Differences are in
allocation space (sum-to-one), not in absolute money — translating to
trade sizes requires the user's total portfolio NAV, which is not in
this dataset.

## `analysis_results.fo_optimizer.frontier`

The full efficient frontier as a "wide" table — one row per frontier
point, one column per asset, plus four metric columns
(`point_index`, `expected_return`, `volatility`, `sharpe_ratio`).
Use this when the user asks about trade-offs along the frontier
("what would happen at lower volatility?"). The asset columns can be
identified via `metadata["asset_columns"]`.

## `analysis_results.scraper.findings`

The Report Scraper's extracted data points from GP/LP reports
(quarterly statements, capital account statements, etc.). One row per
extracted Finding, plus one row per file whose extraction failed
(distinguishable by a populated `error` column).

Columns: `filename`, `fund_name`, `period`, `keyword` (e.g. "NAV",
"Capital Called"), `keyword_type` ("Number" or "Text"), `value`,
`source` (where in the document the value was found), `confidence`
("High", "Medium", "Low"), `error` (only populated for failed files).

Metadata includes `cancelled` (was the run interrupted?), `n_files`,
`n_keywords`, `n_findings`. If `cancelled` is true, the findings are
partial — say so when summarising.

## How to use these datasets

- **Always check `list_analysis_results` first** when the user asks
  about an optimisation, target allocation, current allocation,
  frontier, or scraper output. Do not rely on memory of what the user
  ran in this session — the DataStore is the source of truth.
- **`computed_at` matters.** If a result is from earlier in the
  session and the user has since imported new data, the result may be
  stale. Mention this when relevant ("the optimisation result you're
  asking about was computed at X UTC, before the most recent data
  import").
- **Do not invent values.** If a dataset is absent, say so. Do not
  fall back on prior turns' content as a substitute for current data.
- **Format weights as percentages** (e.g. "33.1 %") in user-facing
  text, but be aware the underlying values are decimals (sum-to-one).
