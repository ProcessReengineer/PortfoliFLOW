# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Reporting service layer.

Provides data providers, chart builders, and an orchestrating engine that
together produce the in-app Portfolio Review report.

Layering rules:
    * Imports from :mod:`core` only.  No GUI, no PyQt6, no module imports.
    * Chart builders return :class:`matplotlib.figure.Figure` objects.  Qt
      embedding (``FigureCanvasQTAgg``) happens only in the GUI widget that
      consumes those figures.
"""
