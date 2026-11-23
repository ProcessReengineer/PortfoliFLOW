# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""The Reading Desk writer: a finished scraper run becomes stored readings.

ADR-0139 §5: one reading per successfully read document is written into
``document_readings``; the scraper package itself gains no database access.
RD-D-23: the readings are written when the extraction service returns, not
when the result reaches the client, so a disconnected client loses no paid
reading.

:mod:`services.reading_desk.compose` holds the pure composition (no
database); :mod:`services.reading_desk.writer` holds the short per-file
transactions and the dedup lookup. The package is FastAPI-free: the route
layer calls it, and it stays callable without that layer
(``tests/regression/test_reading_desk_package_is_web_free.py``).
"""

from services.reading_desk.compose import (
    AssignmentProposal,
    ReadingNotComposable,
    UploadedDocument,
    compose_reading,
    compose_search_text,
    describe_upload,
    propose_assignment,
    sanitise_text,
)
from services.reading_desk.writer import ReadingOutcome, find_earlier_readings, write_readings

__all__ = [
    "AssignmentProposal",
    "ReadingNotComposable",
    "ReadingOutcome",
    "UploadedDocument",
    "compose_reading",
    "compose_search_text",
    "describe_upload",
    "find_earlier_readings",
    "propose_assignment",
    "sanitise_text",
    "write_readings",
]
