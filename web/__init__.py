# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfoliFLOW FastAPI web application package.

The web variant runs in parallel to the PyQt6 GUI during the strangler
period defined by ADR-0039 and ADR-0041. The two surfaces have separate
persistence entry points: the GUI uses the in-memory ``DataStore``
singleton, while web routes go through the repository layer
(``UserRepository`` and future per-domain repositories) — see
ADR-0041.

Sub-stream 2a delivers only the skeleton: the FastAPI app factory, a
health endpoint, a login placeholder, and engine wiring through a
lifespan context. Authentication, the Shirley chat endpoint, and the
Excel import endpoint land in 2b / 2c / 2d respectively.
"""
