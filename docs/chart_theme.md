# PortfoliFLOW Chart Theme

`config/chart_theme.json` is the single source of truth for every visual parameter used in PortfoliFLOW's matplotlib charts.  No chart-rendering code should contain hardcoded colours, font sizes, or line widths — all values must be read from this file via `core.chart_theme.get_chart_theme()`.

---

## Design principle

Keeping all chart parameters in one JSON file mirrors the way Excel chart templates work: a single style definition that propagates to every chart automatically.  This means:

- Adjusting to a corporate colour palette requires editing one file, not hunting through chart code.
- The application can support multiple themes (e.g. light/dark) by swapping the JSON file at startup.
- Non-developers can tweak visual parameters without touching Python code.

---

## Section reference

| Section   | Controls                                                                                        |
|-----------|-------------------------------------------------------------------------------------------------|
| `font`    | Font family, sizes for titles/axis labels/tick labels/legends, and font weights.                |
| `colours` | Background, plot area, text, grid, axis lines, named series colours, and semantic colours (positive/negative bars, NAV line, net capital gain line). Includes a `series_palette` list for multi-series charts. |
| `line`    | Line widths (primary/secondary/grid/axis), line styles (solid/dotted/dashed), and alpha values. |
| `bar`     | Bar width (in data units), bar alpha, edge line width, and edge colour.                         |
| `legend`  | Legend location, frame visibility, frame alpha, column count, and marker scale.                 |
| `layout`  | Figure DPI, chart height in pixels, spacing between charts, subplot padding (top/bottom/left/right), and title padding. |
| `axis`    | Tick direction/length/width, spine visibility per side, x-label rotation and horizontal alignment, y-axis decimal format, and auto-scale flag. |
| `table`   | Header and cell background/foreground colours, border colour, font weights and sizes, and row height — for data tables rendered alongside charts. |

---

## Customisation guide

1. Open `config/chart_theme.json` in any text editor.
2. Locate the section you want to change (e.g. `"colours"` for palette changes).
3. Edit the value.  All colour values must be valid CSS hex strings (e.g. `"#E8304A"`).
4. Save the file.  Changes take effect on the next application start (or immediately if `core.chart_theme.reload_chart_theme()` is called at runtime).

### Common adjustments

**Corporate colour palette** — change `colours.primary` (main accent), `colours.series_palette` (multi-series charts), and `colours.nav_line` / `colours.net_capital_gain_line` (Cash Flow & NAV chart).

**Chart height** — change `layout.chart_height_px`.  The widget's fixed height and the matplotlib figure height both read this value.

**Font family** — change `font.family` to any font installed on the target system.  Falls back to the matplotlib default if the font is not found.

**Label rotation** — change `axis.x_label_rotation` (degrees) and `axis.x_label_ha` (horizontal alignment: `"right"`, `"center"`, `"left"`).

---

## Line trace convention

Time-series line traces use `mode: 'lines'` without markers. Sparse-data exceptions, if needed, are introduced per spec.

---

## Loaded by

`core/chart_theme.py` exposes two functions:

- `get_chart_theme()` — returns the cached `dict`.  Reads from disk only on the first call.
- `reload_chart_theme()` — clears the cache and re-reads from disk.  Useful in development to apply theme changes without restarting the application.
