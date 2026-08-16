# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Logging configuration for PortfoliFLOW.

Call ``configure_logging()`` once at application startup (in ``main.py``)
before any other imports that might emit log records.

Usage::

    from core.logging_setup import configure_logging

    configure_logging()
"""

from __future__ import annotations

import logging
import sys

_configured = False

_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger with a consistent format.

    Safe to call multiple times — subsequent calls after the first are no-ops
    so that test suites don't accumulate duplicate handlers.

    Args:
        level: Logging level string (e.g. ``"DEBUG"``, ``"INFO"``).
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)

    _configured = True
    logging.getLogger(__name__).debug("Logging initialised at level %s.", level)
