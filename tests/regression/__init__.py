# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression guards.

Each test in this package pins a structural invariant the project relies
on rather than a behaviour: a layer stays free of a forbidden import, a
migration round-trips, a schema keeps a property, a seam keeps its shape.
The guards fail loudly the moment a change breaks such a property.

Every guard names the invariant it protects, and the ADR or rule behind
it, in its own module docstring; ``AGENTS.md`` cites the layering guards
next to the rules they enforce. The migration round-trip guards run on
throwaway databases created by ``conftest.py``.
"""
