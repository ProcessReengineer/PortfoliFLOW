# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""PortfoliFLOW FastAPI web application package.

The web application is the only surface (ADR-0094). Web routes persist
through the repository layer (ADR-0041) and never touch the in-memory
``DataStore`` singleton, which survives for the DataStore tools and the
reporting engine until roadmap #035.

Sub-stream 2a delivers only the skeleton: the FastAPI app factory, a
health endpoint, a login placeholder, and engine wiring through a
lifespan context. Authentication, the Shirley chat endpoint, and the
Excel import endpoint land in 2b / 2c / 2d respectively.
"""
