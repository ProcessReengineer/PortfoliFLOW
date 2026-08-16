# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Characterization tests for services.ai_service.

Each test in this package freezes a specific *observed* behaviour of the
unchanged ``services/ai_service.py`` so the upcoming stream A1 split (per
ADR-0038) into ``services/ai_service_core.py`` (Qt-free, asyncio) and
``services/ai_service_qt.py`` (PyQt6 adapter) can be done without
behavioural drift.

Naming convention: ``test_C_NN_<short_topic>`` where ``NN`` matches the
ID column in the stream A1 implementation prompt's characterization
table. C-12 (cancel) is omitted: the current implementation has no
cancel mechanism.
"""
