# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Static seed fixtures consumed by Alembic migrations.

Currently houses the ISO 3166-1 alpha-2 country list used to seed the
``countries`` global stammtabelle in migration b007 (per ADR-0045 §2).
The fixture is loaded by the migration via plain JSON read; it is not
imported by application code at runtime.
"""
