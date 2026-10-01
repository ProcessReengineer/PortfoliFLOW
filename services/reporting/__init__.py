# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Reporting service layer.

Provides data providers, chart builders, and an orchestrating engine that
together produce the in-app Portfolio Review report.

Layering rules:
    * Imports from :mod:`core` only.  No Qt imports, no module imports.
    * Chart builders return :class:`matplotlib.figure.Figure` objects.  The
      engine's only caller is the ``investor_communication.portfolio_review``
      module shell, which only tests run; the web Portfolio Review is built
      from :mod:`services.portfolio_review` and :mod:`services.chart_specs`.
"""
