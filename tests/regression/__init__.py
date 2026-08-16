# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression-guard tests.

These tests use a dedicated package because each one runs the
production code in a *fresh subprocess* and asserts a structural
invariant (e.g. "this module's import graph contains no PyQt6").
They are not unit tests of behaviour — they are guards that fail
loudly the moment a future change breaks an architectural property
the project relies on.

Currently:

* :mod:`tests.regression.test_ai_service_core_qt_free` — guards
  the Qt-free invariant of :mod:`services.ai_service_core`
  (ADR-0038's resolution of ADR-0011's follow-up).

The matching guard for :mod:`services.headless_shirley` lives in
``tests/services/test_headless_shirley.py`` for historical reasons
(predates this package, see ADR-0029); a future cleanup pass can
relocate it here if desired.
"""
