# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Web tests must never start a live Telegram bot.

The app lifespan starts the in-process aiogram bot whenever
``bot.config.get_bot_config().enabled`` is true (``web/main.py``), and most
modules under ``tests/web`` run that lifespan. With a developer ``.env``
carrying ``TELEGRAM_BOT_ENABLED=true`` the test process therefore spawned a
*real* bot, which polls Telegram via ``getUpdates`` — an endpoint that
admits exactly one consumer per token, so the test run silently displaced
the production bot.

The ``_telegram_bot_disabled`` autouse fixture in ``conftest.py`` closes
that door. These tests pin the guarantee so it cannot regress unnoticed:
the fixture is only useful for as long as it actually reaches the seam the
lifespan reads.
"""

from __future__ import annotations

import os

from web.main import create_app
from web.settings import WebSettings


def _settings() -> WebSettings:
    """Build web settings mirroring the package's client fixtures.

    ``database_url`` is passed through as-is: with a URL the lifespan takes
    its full startup path (engines, tenant resolver), without one it runs
    degraded. The bot branch is reached either way, which is the point.
    """
    return WebSettings(
        web_host="127.0.0.1",
        web_port=8000,
        session_cookie_name="portfoliflow_session",
        database_url=os.getenv("DATABASE_URL"),
    )


def test_bot_config_reports_disabled_under_the_autouse_fixture() -> None:
    """The config seam the lifespan reads reports the bot as disabled.

    Asserts through :func:`bot.config.get_bot_config` rather than the raw
    environment, so a stale ``BotSettings`` singleton built with
    ``enabled=True`` — the failure mode the env var alone would not
    catch — fails this test.
    """
    from bot.config import get_bot_config

    assert get_bot_config().enabled is False


async def test_app_lifespan_does_not_start_the_bot(monkeypatch) -> None:
    """Running the app lifespan never calls ``start_bot``.

    The lifespan swallows every exception from its bot block ("a bot
    failure must never block web startup"), so a sentinel that *raised*
    would be logged and discarded, and this test would pass vacuously.
    The sentinel therefore records its calls and the assertion runs after
    the lifespan has exited.
    """
    import bot.telegram_bot

    calls: list[dict[str, object]] = []

    def _sentinel_start_bot(**kwargs: object) -> None:
        calls.append(kwargs)

    # The lifespan imports ``start_bot`` inside the enabled-branch, so the
    # module attribute is resolved at call time and patching it here is
    # enough to observe the branch being taken.
    monkeypatch.setattr(bot.telegram_bot, "start_bot", _sentinel_start_bot)

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        pass

    assert calls == [], (
        "The app lifespan started the Telegram bot during a web test. "
        "Web tests must never reach a live getUpdates consumer — see the "
        "_telegram_bot_disabled fixture in tests/web/conftest.py."
    )


async def test_no_bot_thread_is_alive_after_the_lifespan(monkeypatch) -> None:
    """No bot worker thread survives an app lifespan.

    A belt-and-braces check on the observable end state rather than the
    call seam: even if some future code path started the bot without going
    through the patched entry point above, a live polling thread would be
    parked on ``bot.telegram_bot._bot_thread``.
    """
    import bot.telegram_bot

    monkeypatch.setattr(bot.telegram_bot, "_bot_thread", None)

    app = create_app(_settings())
    async with app.router.lifespan_context(app):
        pass

    thread = bot.telegram_bot._bot_thread
    assert thread is None or not thread.is_alive()
