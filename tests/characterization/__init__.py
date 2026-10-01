# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Characterization tests for :mod:`services.ai_service_core`.

Each test in this package freezes a specific *observed* behaviour of the
Qt-free asyncio core that stream A1 (ADR-0038) split out of the former Qt
implementation (ADR-0094), so the core cannot drift from it.

Naming convention: ``test_C_NN_<short_topic>`` where ``NN`` matches the
ID column in the stream A1 implementation prompt's characterization
table. C-12 (cancel) is omitted: the current implementation has no
cancel mechanism.
"""
