# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""``portfoliflow vault-generate-key`` / ``vault-rotate-key`` — key custody.

The operator half of the credential vault (ADR-0112 §2). Two commands:

* ``vault-generate-key`` emits one fresh Fernet key on stdout for the
  operator's secret store — nothing else, so it pipes cleanly.
* ``vault-rotate-key`` decrypts every ``is_secret`` row with the old key
  and re-encrypts it with the new one, in a **single transaction** on the
  **superuser engine**. Rotation is a documented operator procedure, not
  an automatic mechanism; see ``docs/deploy/credential-vault.md``.

The cross-tenant read is the point of the superuser engine here: secret
rows live in every tenant, and no single ``app.tenant_id`` covers them.
This is the same sanctioned RLS-bypassing CLI pattern as
``portfoliflow inspect-tenant`` and the bootstrap/Alembic paths
(ADR-0040 §2, ADR-0064 §3) — application code never connects this way.

**Nothing here logs a value.** Log lines and messages state counts,
providers, key names and row ids; never plaintext, never ciphertext,
never a master key. ``vault-generate-key`` deliberately does *not* call
``configure_logging()``: the house log handler writes to stdout, and this
command's contract is exactly one line there.

Follows the existing CLI pattern (``cli/bootstrap.py``,
``cli/irene_tick.py``): a thin Typer wrapper that calls
``configure_logging()`` then ``asyncio.run(_run())``, mapping
``ConfigurationError`` → exit 2 and ``PortfoliFlowError`` → exit 3.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from uuid import UUID

import typer
from cryptography.fernet import Fernet
from sqlalchemy import text

from cli._db import superuser_engine
from core.exceptions import ConfigurationError, PortfoliFlowError
from core.logging_setup import configure_logging
from services.credential_vault import (
    MASTER_KEY_ENV_VAR,
    VaultCipher,
    VaultDecryptError,
)

_LOG = logging.getLogger("portfoliflow.cli")


# ---------------------------------------------------------------------------
# vault-generate-key
# ---------------------------------------------------------------------------


def vault_generate_key_command() -> None:
    """Emit a fresh Fernet key for the credential vault (ADR-0112 §2).

    Prints exactly one line — the key — so it can be piped straight into a
    secret store. No database connection, no logging, no other output. The
    key is not stored anywhere by this command: placing it in the
    deployment environment as ``CREDENTIAL_VAULT_MASTER_KEY`` (and keeping
    it out of the repository) is the operator's duty, documented in
    ``docs/deploy/credential-vault.md``.
    """
    typer.echo(Fernet.generate_key().decode())


# ---------------------------------------------------------------------------
# vault-rotate-key
# ---------------------------------------------------------------------------


def _read_key_from_stdin(what: str) -> str:
    """Read one key from a line of stdin.

    Args:
        what: Which key is expected — used only in the error message.

    Returns:
        The key with its trailing newline stripped.

    Raises:
        ConfigurationError: If stdin is exhausted or the line is blank.
    """
    line = sys.stdin.readline()
    key = line.rstrip("\n").strip()
    if not key:
        raise ConfigurationError(
            f"No {what} key provided on stdin. Pipe the key(s) into the "
            f"command, e.g. `printf '%s\\n' \"$NEW_KEY\" | portfoliflow "
            f"vault-rotate-key --new-key-stdin`."
        )
    return key


def _resolve_keys(old_key_stdin: bool, new_key_stdin: bool) -> tuple[str, str]:
    """Resolve the old and new master keys.

    The old key comes from ``CREDENTIAL_VAULT_MASTER_KEY`` unless
    ``--old-key-stdin`` is given; the new key comes from stdin only. When
    both read stdin the order is **old first, then new** — one key per
    line.

    Args:
        old_key_stdin: Whether to read the old key from stdin.
        new_key_stdin: Whether the (mandatory) new key is on stdin.

    Returns:
        ``(old_key, new_key)``.

    Raises:
        ConfigurationError: If the new key was not offered on stdin, or a
            key is missing.
    """
    if not new_key_stdin:
        raise ConfigurationError(
            "The new key must be supplied on stdin: pass --new-key-stdin. "
            "A rotation never takes a key from the command line, where it "
            "would land in the shell history and the process table."
        )
    old_key = (
        _read_key_from_stdin("old") if old_key_stdin else os.getenv(MASTER_KEY_ENV_VAR, "").strip()
    )
    if not old_key:
        raise ConfigurationError(
            f"{MASTER_KEY_ENV_VAR} is not set and --old-key-stdin was not "
            f"given, so there is no old key to decrypt with."
        )
    new_key = _read_key_from_stdin("new")
    if new_key == old_key:
        raise ConfigurationError("The new key is identical to the old one — nothing to rotate.")
    return old_key, new_key


async def _rotate(old_cipher: VaultCipher, new_cipher: VaultCipher) -> int:
    """Re-encrypt every ``is_secret`` row in one transaction.

    Args:
        old_cipher: Cipher bound to the key the rows were written under.
        new_cipher: Cipher bound to the key they should be written under.

    Returns:
        The number of rows re-encrypted.

    Raises:
        PortfoliFlowError: If any row fails to decrypt with the old key.
            The transaction rolls back — the vault is never left half
            rotated — and the message names the offending row id and the
            counts, never a value.
    """
    engine = superuser_engine()
    rotated = 0
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, provider, key, value_ciphertext "
                        "FROM scoped_settings WHERE is_secret ORDER BY id"
                    )
                )
            ).all()
            total = len(rows)
            _LOG.info("vault-rotate-key: %d secret row(s) to re-encrypt.", total)

            for row in rows:
                row_id: UUID = row.id
                try:
                    plaintext = old_cipher.decrypt(bytes(row.value_ciphertext))
                except VaultDecryptError as exc:
                    # Count-only: the row is named, its value never is.
                    raise PortfoliFlowError(
                        f"vault-rotate-key: row {row_id} could not be decrypted "
                        f"with the old key ({rotated} of {total} row(s) had been "
                        f"re-encrypted). The transaction was rolled back — no row "
                        f"was changed. Check that CREDENTIAL_VAULT_MASTER_KEY (or "
                        f"--old-key-stdin) is the key these rows were written under."
                    ) from exc
                await conn.execute(
                    text(
                        "UPDATE scoped_settings "
                        "SET value_ciphertext = :ct, updated_at = NOW() "
                        "WHERE id = :id"
                    ),
                    {"ct": new_cipher.encrypt(plaintext), "id": row_id},
                )
                rotated += 1
                _LOG.info(
                    "vault-rotate-key: re-encrypted %s.%s (row %s).",
                    row.provider,
                    row.key,
                    row_id,
                )
    finally:
        await engine.dispose()
    return rotated


async def _run(old_key_stdin: bool, new_key_stdin: bool) -> None:
    """Resolve both keys and run the rotation."""
    old_key, new_key = _resolve_keys(old_key_stdin, new_key_stdin)
    # Constructing both ciphers first means a malformed *new* key fails
    # before a single row is read, not halfway through the rotation.
    old_cipher = VaultCipher(old_key)
    new_cipher = VaultCipher(new_key)

    rotated = await _rotate(old_cipher, new_cipher)
    _LOG.info("vault-rotate-key: %d row(s) re-encrypted under the new key.", rotated)
    typer.echo(f"Re-encrypted {rotated} secret row(s).")


def vault_rotate_key_command(
    old_key_stdin: bool = typer.Option(
        False,
        "--old-key-stdin",
        help=(
            "Read the old key from stdin instead of CREDENTIAL_VAULT_MASTER_KEY. "
            "With --new-key-stdin the order is old first, then new — one per line."
        ),
    ),
    new_key_stdin: bool = typer.Option(
        False,
        "--new-key-stdin",
        help="Read the new key from stdin. Required — the new key is never a flag value.",
    ),
) -> None:
    """Re-encrypt every vault secret under a new master key (ADR-0112 §2).

    Reads all ``is_secret`` rows across all tenants on the superuser engine
    (a sanctioned RLS-bypassing path, like ``inspect-tenant``), decrypts
    each with the old key, re-encrypts it with the new one, and commits in
    a **single transaction** — the vault is never left half rotated. A row
    that will not decrypt aborts the whole rotation.

    Afterwards, replace ``CREDENTIAL_VAULT_MASTER_KEY`` in the deployment
    environment with the new key and restart the process; retain the old
    key only until the rotation is confirmed, then destroy it.

    Exit codes follow the CLI convention: 0 on success (including "no
    secret rows"), 2 on a configuration error (missing/invalid key,
    ``DATABASE_URL_SUPERUSER`` unset), 3 on any other PortfoliFLOW error
    — including a failed decryption, after the rollback.
    """
    configure_logging()
    try:
        asyncio.run(_run(old_key_stdin, new_key_stdin))
    except ConfigurationError as exc:
        _LOG.error("vault-rotate-key: %s", exc.message)
        raise typer.Exit(code=2) from exc
    except PortfoliFlowError as exc:
        _LOG.error("vault-rotate-key: %s", exc.message)
        raise typer.Exit(code=3) from exc
