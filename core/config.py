# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Application configuration loaded from environment / .env file.

Usage::

    from core.config import get_config

    cfg = get_config()
    print(cfg.app_name)

The singleton is initialised on first call to ``get_config()`` and the same
instance is returned on every subsequent call within the process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from core.exceptions import ConfigurationError

# Load .env from the project root (two levels up from this file)
_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_ENV_PATH)


def _env_int(name: str, default: int) -> int:
    """Return the integer env var ``name``, or ``default`` if unset/invalid."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Case-attachment type whitelist (ADR-0107 §7 / Gate-C0). The pin-document
# route validates BOTH the uploaded filename's extension and the declared
# content type against this map: the extension must be a key, and the declared
# MIME must be one of that key's accepted values. Content *sniffing* — verifying
# the bytes actually are what the extension/MIME claim — is deliberately out of
# scope for v1 (deviation-register item): the whitelist trusts the two declared
# signals only. The CSV entry admits the MIME variants browsers send for
# spreadsheets so a legitimately-named ``.csv`` is not rejected on a benign
# content-type quirk.
_DEFAULT_CASE_ATTACHMENT_TYPES: dict[str, frozenset[str]] = {
    "pdf": frozenset({"application/pdf"}),
    "png": frozenset({"image/png"}),
    "jpg": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    "csv": frozenset({"text/csv", "application/csv", "application/vnd.ms-excel"}),
}


@dataclass
class Settings:
    """Centralised application settings.

    All fields are read from environment variables with sensible defaults so
    the application can run without a .env file during development.

    Attributes:
        app_name: Display name shown in the window title.
        debug: Enable verbose debug output when True.
        log_level: Standard Python logging level string (e.g. "INFO").
        data_dir: Path to the directory used for data storage.
        db_url: Optional SQLAlchemy-compatible database URL.
        APP_DB_ROLE: Name of the unprivileged PostgreSQL application
            role (ADR-0040). ``tenant_context`` switches to this role
            for the duration of a tenant-scoped transaction so RLS is
            enforced regardless of the connecting role — including
            under the superuser CLI engine, which would otherwise
            bypass RLS (ADR-0078).
        case_attachment_max_bytes: Per-file size cap for case
            attachments (ADR-0107 §7). Default 10 MB. Enforced at the
            pin-document route, never in the repository.
        case_attachment_max_count: Per-case attachment count cap
            (ADR-0107 §7). Default 20. Enforced at the route.
        case_attachment_allowed_types: Type whitelist for case
            attachments (ADR-0107 §7): a map of lower-cased filename
            extension to the set of declared MIME types accepted for it.
            The route checks both signals; see
            ``_DEFAULT_CASE_ATTACHMENT_TYPES``.
    """

    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "PortfoliFLOW"))
    debug: bool = field(default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true")
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    data_dir: str = field(default_factory=lambda: os.getenv("DATA_DIR", "data"))
    db_url: str | None = field(default_factory=lambda: os.getenv("DB_URL"))
    APP_DB_ROLE: str = field(default_factory=lambda: os.getenv("APP_DB_ROLE", "portfoliflow_app"))
    # Case-attachment caps (ADR-0107 §7). Configuration, not schema: the caps
    # live here and are enforced at the pin-document route, never hard-coded at
    # the call site and never in the repository.
    case_attachment_max_bytes: int = field(
        default_factory=lambda: _env_int("CASE_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024)
    )
    case_attachment_max_count: int = field(
        default_factory=lambda: _env_int("CASE_ATTACHMENT_MAX_COUNT", 20)
    )
    case_attachment_allowed_types: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            ext: frozenset(mimes) for ext, mimes in _DEFAULT_CASE_ATTACHMENT_TYPES.items()
        }
    )

    def __post_init__(self) -> None:
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ConfigurationError(
                f"Invalid LOG_LEVEL '{self.log_level}'. Must be one of {valid_levels}."
            )
        self.log_level = self.log_level.upper()


_instance: Settings | None = None


def get_config() -> Settings:
    """Return the application-wide ``Settings`` singleton.

    Creates the instance on the first call; subsequent calls return the cached
    object without re-reading the environment.

    Returns:
        The singleton ``Settings`` instance.
    """
    global _instance
    if _instance is None:
        _instance = Settings()
    return _instance
