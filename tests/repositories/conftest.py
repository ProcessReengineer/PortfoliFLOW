# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Repository test fixtures.

The fixture definitions live in ``tests._db_fixtures`` so the SAA
service tests under ``tests/services/`` and the auth tests under
``tests/auth/`` can reuse them. Importing the fixture functions
into this conftest registers them in the ``tests/repositories/``
scope without leaking ``reset_schema`` (autouse) into unrelated
non-DB tests above us.
"""

from __future__ import annotations

# Re-export the live-DB fixtures so pytest discovers them in this
# directory. ``noqa: F401`` because they're not used in this file
# directly — they exist as fixture sources for tests in this
# package.
from tests._db_fixtures import (  # noqa: F401
    app_engine,
    reset_schema,
    seed_tenant,
    superuser_engine,
)
