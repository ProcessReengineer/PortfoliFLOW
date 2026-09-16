# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow directory-refresh`` / ``directory-status`` — the arming instrument.

Two operator commands over :mod:`services.provider_directory`. One fetches the
signed provider directory from portfoliflow.com and keeps it only if it is
better than what this instance already holds; the other re-verifies the copy on
disk and says where it came from. The library does the work — the deciding, the
verifying, the writing — and this module does nothing but resolve ``DATA_DIR``,
supply an instant, and turn a dataclass into lines an operator can paste into a
report (B-D-24).

**Not a user surface.** These are the operator's verification instrument for a
deployed directory: publication day, an upgrade, a support call. They read no
database, resolve no tenant and consult no setting other than ``DATA_DIR``, so
nothing here looks at ``provider_channel.enabled`` — a tenant's decision not to
use the channel says nothing about whether the instance's copy of the directory
is sound. The surfaces that *do* answer to that setting arrive with SB-6.

**The one clock in the delivery.** Neither :mod:`services.provider_channel` nor
:mod:`services.provider_directory` reads a clock: the instant is an argument
everywhere (D-clock), which is what makes a validity window testable without
freezing time. That injection has to bottom out somewhere, and this module is
the somewhere — :func:`_now` is the only clock read in SB-3b, which is why it
is a module-level seam a test can replace rather than a call buried in a
coroutine.

Nothing here opens a database connection, so the commands run on a host whose
Postgres is down or absent — deliberately, because "is the directory sound?" is
a question an operator asks precisely when other things are not well. Output
goes through :func:`typer.echo` and never through the log handler, so stdout
stays a clean paste (the ``vault-generate-key`` precedent).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import httpx
import typer

from core.config import get_config
from services.provider_channel.publishing_key import PUBLISHING_KEY_ID, PUBLISHING_KEY_RING
from services.provider_directory import (
    DEFAULT_TIMEOUT,
    DIRECTORY_URL,
    DOCUMENT_FILE,
    Provenance,
    RefreshOutcome,
    cache_root_for,
    provenance_from_cache,
    provenance_to_dict,
    read_cache,
    refresh_directory,
)

#: A caller or configuration error: a malformed URL, an unreadable data
#: directory, a misconfigured key ring. Not a statement about any document.
_EXIT_CALLER_ERROR: Final[int] = 2

#: The document was obtained and refused. The cache is untouched, and the
#: instance still stands on whatever it stood on before.
_EXIT_REFUSED: Final[int] = 3

#: The document could not be obtained at all — transport, timeout, bad status.
_EXIT_UNAVAILABLE: Final[int] = 4

#: No usable cached copy exists: none was ever fetched, or the one on disk does
#: not verify.
_EXIT_NO_CACHE: Final[int] = 5

#: The prefix every refusal status carries.
_REFUSED_PREFIX: Final[str] = "refused_"


def _now() -> datetime:
    """Read the clock, once, in the only place SB-3b reads one.

    Returns:
        The current instant in UTC, timezone-aware as every consumer below
        requires (D-clock).
    """
    return datetime.now(timezone.utc)


def _default_data_dir() -> str:
    """Resolve the instance's data directory from settings.

    A seam of its own so a test can point the commands at a temporary
    directory without constructing a whole ``Settings``.

    Returns:
        ``DATA_DIR`` as configured.
    """
    return get_config().data_dir


def _resolve_root(data_dir: str | None) -> Path:
    """Locate the directory cache for this invocation.

    Args:
        data_dir: The ``--data-dir`` value, or ``None`` to use the configured
            one.

    Returns:
        ``<data_dir>/provider_directory``.
    """
    return cache_root_for(data_dir if data_dir is not None else _default_data_dir())


def _days_left(provenance: Provenance, *, now: datetime) -> int:
    """How many days the held copy has left, negative once it has expired.

    Args:
        provenance: The provenance of the held copy.
        now: The instant to measure from.

    Returns:
        ``valid_until − now``, in whole days.
    """
    return (provenance.valid_until - now.date()).days


def _scalar(value: object) -> str:
    """Render one provenance member as a single word or phrase.

    Booleans and ``None`` are spelled the way the ``--json`` output spells
    them, so the two renderings of one fact cannot be read as two facts.

    Args:
        value: The member's value.

    Returns:
        Its text form.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "none"
    return str(value)


def _echo_provenance(rendered: dict[str, object] | None, days_left: int | None) -> None:
    """Print the provenance block, or record that there is nothing to describe.

    Args:
        rendered: :func:`~services.provider_directory.provenance_to_dict`
            output, or ``None`` when no usable copy exists.
        days_left: The days remaining, or ``None`` alongside a ``None`` block.
    """
    if rendered is None:
        typer.echo("provenance: none")
        return
    typer.echo("provenance:")
    for key, value in rendered.items():
        typer.echo(f"  {key}: {_scalar(value)}")
    typer.echo(f"  days_left: {days_left}")


async def _run_refresh(
    *, cache_root: Path, now: datetime, url: str, timeout: float
) -> RefreshOutcome:
    """Perform one refresh on a client owned for exactly that long.

    Args:
        cache_root: Where the copy lives.
        now: The instant of this refresh.
        url: The document URL.
        timeout: Per-request timeout in seconds.

    Returns:
        What the refresh did.
    """
    async with httpx.AsyncClient() as client:
        return await refresh_directory(
            cache_root=cache_root,
            client=client,
            now=now,
            url=url,
            ring=PUBLISHING_KEY_RING,
            current_key_id=PUBLISHING_KEY_ID,
            timeout=timeout,
        )


def directory_refresh_command(
    url: str = typer.Option(
        DIRECTORY_URL,
        "--url",
        help=(
            "The directory document URL. Must end in .json — the detached "
            "signature is read from beside it."
        ),
    ),
    data_dir: str | None = typer.Option(
        None,
        "--data-dir",
        help="Instance data directory (default: DATA_DIR / settings data_dir)",
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT,
        "--timeout",
        help="Per-request timeout in seconds. Applies to the document and to its signature.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit one JSON object instead of the formatted text output.",
    ),
) -> None:
    """Fetch the signed provider directory once and keep it if it is better.

    Verifies the served bytes against the key ring shipped with this build and
    applies the monotonic version rule (B-D-15): a version below the one
    already cached is a refused downgrade, and every refusal leaves the cached
    copy byte-identical. Reads no database, resolves no tenant, and does not
    consult the ``provider_channel.enabled`` setting.

    Exit codes:

    * **0** — the copy was updated, or the server confirmed the one held.
    * **2** — a caller or configuration error: a URL that is not a ``.json``
      document, an unreadable data directory, a key ring this build cannot
      refresh with.
    * **3** — the document was obtained and refused. The cache is untouched.
    * **4** — the document could not be obtained at all.
    """
    now = _now()
    try:
        outcome = asyncio.run(
            _run_refresh(cache_root=_resolve_root(data_dir), now=now, url=url, timeout=timeout)
        )
    except (ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_CALLER_ERROR) from exc

    provenance = outcome.provenance
    rendered = provenance_to_dict(provenance) if provenance is not None else None
    days_left = _days_left(provenance, now=now) if provenance is not None else None

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "status": outcome.status,
                    "notices": list(outcome.notices),
                    "provenance": rendered,
                    "days_left": days_left,
                }
            )
        )
    else:
        typer.echo(f"status: {outcome.status}")
        for notice in outcome.notices:
            typer.echo(f"notice: {notice}")
        _echo_provenance(rendered, days_left)

    if outcome.status == "unavailable":
        raise typer.Exit(code=_EXIT_UNAVAILABLE)
    if outcome.status.startswith(_REFUSED_PREFIX):
        raise typer.Exit(code=_EXIT_REFUSED)


def directory_status_command(
    data_dir: str | None = typer.Option(
        None,
        "--data-dir",
        help="Instance data directory (default: DATA_DIR / settings data_dir)",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit one JSON object instead of the formatted text output.",
    ),
) -> None:
    """Re-verify the cached provider directory and print its provenance.

    Offline and read-only: the copy under ``DATA_DIR`` is verified again
    against the shipped key ring, because a file on this instance's own disk is
    no more trustworthy than its signature (B-D-13). An **expired** copy still
    prints — ``valid: false`` and a negative ``days_left`` — and still exits 0:
    it is readable, and it remains the baseline a downgrade is refused against.

    Exit codes:

    * **0** — a cached copy exists and was re-verified, expired or not.
    * **2** — the data directory could not be read.
    * **5** — no usable cached copy: none has been fetched, or the one on disk
      does not verify.
    """
    now = _now()
    root = _resolve_root(data_dir)
    try:
        cached = read_cache(
            root, now=now, ring=PUBLISHING_KEY_RING, current_key_id=PUBLISHING_KEY_ID
        )
    except (ValueError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_CALLER_ERROR) from exc

    if cached is None:
        if (root / DOCUMENT_FILE).exists():
            typer.echo(
                f"error: a cached directory is present at {root} but unusable — it did not "
                "verify against the shipped key ring. Tampered, corrupt, or signed by a key "
                "this build does not carry; the disk is data, not trust (B-D-13), so the "
                "instance stands on nothing rather than on something it cannot check. Run "
                "'portfoliflow directory-refresh'.",
                err=True,
            )
        else:
            typer.echo(
                f"error: no cache — {root / DOCUMENT_FILE} does not exist. Nothing has been "
                "fetched yet. Run 'portfoliflow directory-refresh'.",
                err=True,
            )
        raise typer.Exit(code=_EXIT_NO_CACHE)

    provenance = provenance_from_cache(cached, current_key_id=PUBLISHING_KEY_ID)
    rendered = provenance_to_dict(provenance)
    days_left = _days_left(provenance, now=now)

    if json_output:
        typer.echo(json.dumps({"provenance": rendered, "days_left": days_left}))
    else:
        _echo_provenance(rendered, days_left)
