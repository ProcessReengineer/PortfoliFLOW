# Module specification — `Charts` (retired scaffold)

Source: `modules/front_office/charts.py`  
Retired by: ADR-0094 (GUI Sunset Stage 1), inventory item R9

This module was a registered `BaseModule` scaffold whose public methods were unimplemented `...` bodies (the capability shipped as services + web sections instead). Its docstring specification is preserved verbatim below; the scaffold class itself was deleted.

## Module docstring

```text
Front Office — Charts module.

Purpose:
    Generate interactive and static visualisations for fund performance,
    risk metrics, and portfolio composition using Plotly and Matplotlib.

Inputs:
    - DataFrames produced by ``TimeSeries``, ``Statistics``, or
      ``PortfolioOptimizer``
    - Chart configuration dict (title, colours, date range)

Outputs:
    - ``plotly.graph_objects.Figure`` objects for interactive use
    - ``matplotlib.figure.Figure`` objects for static/export use

Dependencies (internal):
    - ``core.base_module.BaseModule``

Dependencies (external):
    - ``plotly``
    - ``matplotlib``
    - ``pandas``

```

## `class Charts(BaseModule)`

```text
Visualisation factory for front-office analytics.

    Attributes:
        module_name: ``"charts"``
        module_area: ``"front_office"``
    
```

## `def run(self, *args: Any, **kwargs: Any) -> dict`

```text
Generate a standard chart bundle for the given data.

        Keyword Args:
            chart_type (str): One of ``"performance"``, ``"drawdown"``,
                ``"correlation"``, ``"frontier"``, ``"allocation"``.
            data (pd.DataFrame | dict): Input data, type depends on chart_type.
            config (dict): Optional display overrides (title, colours, etc.).

        Returns:
            dict: Keys ``status`` and ``figure`` (Plotly Figure).
        
```

## `def performance_chart(self, cumulative_returns: pd.DataFrame, title: str='Cumulative Performance', benchmark: pd.Series | None=None) -> Any`

```text
Create an interactive cumulative performance line chart.

        Args:
            cumulative_returns: Date-indexed DataFrame of cumulative returns.
            title: Chart title.
            benchmark: Optional benchmark series to overlay.

        Returns:
            ``plotly.graph_objects.Figure``.
        
```

## `def drawdown_chart(self, drawdowns: pd.DataFrame, title: str='Drawdowns') -> Any`

```text
Create a drawdown area chart.

        Args:
            drawdowns: Date-indexed DataFrame of drawdown values.
            title: Chart title.

        Returns:
            ``plotly.graph_objects.Figure``.
        
```

## `def correlation_heatmap(self, correlation_matrix: pd.DataFrame, title: str='Correlation Matrix') -> Any`

```text
Create an annotated correlation heatmap.

        Args:
            correlation_matrix: Square DataFrame of correlation coefficients.
            title: Chart title.

        Returns:
            ``plotly.graph_objects.Figure``.
        
```

## `def efficient_frontier_chart(self, frontier_points: list[dict], optimal_point: dict | None=None, title: str='Efficient Frontier') -> Any`

```text
Plot the efficient frontier with optional optimal portfolio marker.

        Args:
            frontier_points: List of dicts from
                ``PortfolioOptimizer.efficient_frontier()``.
            optimal_point: Dict for the chosen optimal portfolio, optional.
            title: Chart title.

        Returns:
            ``plotly.graph_objects.Figure``.
        
```

## `def allocation_pie(self, weights: pd.Series, title: str='Portfolio Allocation') -> Any`

```text
Create a pie chart for portfolio weights.

        Args:
            weights: Series of allocation weights indexed by asset name.
            title: Chart title.

        Returns:
            ``plotly.graph_objects.Figure``.
        
```
