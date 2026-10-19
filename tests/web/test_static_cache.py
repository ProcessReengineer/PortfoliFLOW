# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``/static`` is served with ``Cache-Control: no-cache``.

Stale static files in the browser produced three false findings on the
walk (an Admin switch and, twice, the rebuilt voice-call SDK bundle),
because Starlette's ``StaticFiles`` sends validators but no
``Cache-Control`` and the browser kept the old bytes heuristically fresh.
``no-cache`` keeps assets cacheable but forces a revalidation on every
use — an unchanged file costs one ``304`` — whereas ``no-store`` would
re-download every asset on every page and per-file version pins would
have to be bumped by hand on each change.
"""

from __future__ import annotations

from httpx import AsyncClient

_SCRIPT = "/static/js/chat.js"
_SDK_BUNDLE = "/static/vendor/elevenlabs-client-1.28.0/elevenlabs-client.esm.js"


async def test_static_script_is_served_with_no_cache_and_etag(
    web_client_no_db: AsyncClient,
) -> None:
    response = await web_client_no_db.get(_SCRIPT)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag")


async def test_vendored_sdk_bundle_is_served_with_no_cache(
    web_client_no_db: AsyncClient,
) -> None:
    response = await web_client_no_db.get(_SDK_BUNDLE)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


async def test_revalidation_answers_304_with_no_cache(
    web_client_no_db: AsyncClient,
) -> None:
    first = await web_client_no_db.get(_SCRIPT)
    etag = first.headers["etag"]

    revalidated = await web_client_no_db.get(_SCRIPT, headers={"If-None-Match": etag})

    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == "no-cache"
