# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web-specific settings for the FastAPI variant.

Kept separate from ``core/config.py`` because the latter is the GUI-
flavoured ``Settings`` singleton (read on every PyQt6 launch via
``get_config()``); polluting it with web-only knobs would force the
GUI process to validate FastAPI settings it does not use.

All values are read from environment variables / ``.env`` via
``pydantic-settings``. The defaults match what an operator running
``portfoliflow-web`` on a developer laptop expects.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bounds for ``TICK_SCHEDULER_INTERVAL_SECONDS`` (ADR-0117 §4). Below the
# floor the two due reads stop being "one cheap SELECT now and then" and
# become self-inflicted load; above the ceiling the tick is coarser than an
# hour, which no per-tenant cadence expects.
_MIN_TICK_INTERVAL_SECONDS = 5
_MAX_TICK_INTERVAL_SECONDS = 3600


class WebSettings(BaseSettings):
    """Runtime configuration for the FastAPI variant."""

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    web_host: str = "127.0.0.1"
    web_port: int = 8000
    session_cookie_name: str = "portfoliflow_session"
    csrf_cookie_name: str = "portfoliflow_csrf_pre_session"

    # The application-wide Postgres connection URL. Read here so the
    # web lifespan can build the engine without re-reading os.environ.
    database_url: str | None = None

    # The superuser (RLS-bypassing) connection URL. Every consumer is
    # enumerated and pinned by the regression guard
    # ``tests/regression/test_audit_engine_only_writes_login_audit.py``:
    # ``login_audit`` writes, the pre-tenant session resolve, the
    # subdomain lookup, the cross-tenant Telegram bot-token scan since
    # ADR-0112 §5 (which the lifespan injects into ``start_bot`` rather
    # than performing itself), and — since ADR-0117 §3 — the built-in tick
    # scheduler's cross-tenant due reads. The asymmetry is deliberate (see
    # ``services/auth/local_password.py``).
    database_url_superuser: str | None = None

    # When True (production), session and CSRF cookies are marked
    # ``Secure`` so they are only sent over TLS. Local development
    # over plain HTTP needs this False.
    session_cookie_secure: bool = False

    # OpenRouter (or any OpenAI-compatible) endpoint for Shirley. The
    # PyQt6 GUI configures these via QSettings through the AI Settings
    # widget; the web variant has no settings UI yet (Phase 5), so the
    # FastAPI lifespan reads them from .env at startup. The bot reads
    # the same three variables — sharing them here means the operator
    # configures Shirley once for both surfaces.
    #
    # ``openrouter_api_key`` is optional so the server still starts on
    # a contributor laptop without LLM access; the chat endpoint then
    # responds 503 with a message pointing the operator at .env.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str | None = None
    shirley_model: str | None = None

    # The built-in tick scheduler (ADR-0117 §4). Which tick source runs is
    # process topology, not domain data, so it is environment scope and
    # deliberately neither UI-editable nor database-resident; per-tenant
    # cadence stays the only tenant-facing scheduling knob.
    #
    # ``False`` starts no in-process task — the operator drives
    # ``portfoliflow irene-tick`` / ``portfoliflow market-data-tick``
    # externally instead (the systemd units under ``docs/deploy/``, cron, …).
    # Running both at once is safe by construction: every beat is claimed
    # with ``pg_try_advisory_xact_lock``, so whichever tick source fires
    # first beats a tenant and the other skips — an existing systemd
    # deployment can therefore migrate in either order.
    tick_scheduler_enabled: bool = True

    # How often the task asks "who is due?". This is the *tick* interval,
    # never a cadence: cadence is per-tenant data in ``irene_schedule`` /
    # ``market_data_schedule``, and no value here changes when a tenant is
    # due — only how promptly a due tenant is noticed. Bounds-checked below.
    tick_scheduler_interval_seconds: int = 60

    @field_validator("tick_scheduler_interval_seconds")
    @classmethod
    def _validate_tick_interval(cls, value: int) -> int:
        """Reject an out-of-bounds tick interval at settings load.

        Fail fast rather than clamp: a silently corrected interval leaves
        the deployment ticking at a rhythm nobody configured and nobody can
        see, which is worse than a startup that refuses with the reason.

        Args:
            value: The configured interval in seconds.

        Returns:
            The value, unchanged, when it is within bounds.

        Raises:
            ValueError: If the value is outside
                ``[_MIN_TICK_INTERVAL_SECONDS, _MAX_TICK_INTERVAL_SECONDS]``.
                Pydantic surfaces this as a ``ValidationError`` from
                ``WebSettings()``, i.e. before the app is built.
        """
        if not _MIN_TICK_INTERVAL_SECONDS <= value <= _MAX_TICK_INTERVAL_SECONDS:
            raise ValueError(
                "TICK_SCHEDULER_INTERVAL_SECONDS must be between "
                f"{_MIN_TICK_INTERVAL_SECONDS} and {_MAX_TICK_INTERVAL_SECONDS} "
                f"seconds (got {value})."
            )
        return value


def get_web_settings() -> WebSettings:
    """Return a fresh ``WebSettings`` instance.

    Not memoised: the FastAPI app factory builds settings once at
    startup, and tests may want to override the URL on a per-call
    basis.
    """
    return WebSettings()
