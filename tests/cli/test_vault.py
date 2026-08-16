# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Tests for ``portfoliflow vault-generate-key`` / ``vault-rotate-key``.

The operator half of the credential vault (ADR-0112 §2). ``vault-rotate-key``
is **cross-tenant by nature** — it reads every ``is_secret`` row in the
database on the superuser engine — so these tests run against a **throwaway
database of their own**, built once for the module and dropped afterwards.
Pointing them at the shared dev database would make the rotation's own
"re-encrypt everything" semantics depend on whatever else lives there, and a
failed run would leave real rows encrypted under a test key.

The commands call :func:`asyncio.run` internally, so the test functions are
*synchronous* (the ``tests/cli/test_bootstrap_seeds.py`` pattern); the
helpers below open short-lived loops for seeding and verification.

Coverage
--------
* VK-01: ``vault-generate-key`` emits exactly one valid Fernet key and
  nothing else.
* VK-02: ``vault-rotate-key`` re-encrypts every secret row across tenants —
  the old key no longer decrypts, the new one does, and the plaintexts are
  unchanged. Config rows are left alone.
* VK-03: a wrong old key rolls the whole transaction back (rows byte-for-byte
  unchanged) and exits 3, naming counts and the row, never a value.
* VK-04: argument/configuration failures exit 2 before touching the data.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import NullPool, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from cli import app
from services.credential_vault import (
    MASTER_KEY_ENV_VAR,
    VaultCipher,
    VaultDecryptError,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env")

DATABASE_URL_SUPERUSER = os.getenv("DATABASE_URL_SUPERUSER")

runner = CliRunner()

_OLD_KEY = Fernet.generate_key().decode()
_NEW_KEY = Fernet.generate_key().decode()

#: (provider, key, plaintext) for the secret rows seeded per tenant.
_SECRETS = (
    ("openrouter", "api_key", "sk-or-v1-tenant-secret"),
    ("openfigi", "api_key", "figi-secret-key"),
)


# ---------------------------------------------------------------------------
# Scratch database
# ---------------------------------------------------------------------------


def _rebound(url: str, database: str) -> str:
    """Return *url* pointed at *database*, password intact."""
    return make_url(url).set(database=database).render_as_string(hide_password=False)


async def _create_database(maintenance_url: str, name: str) -> None:
    engine = create_async_engine(
        maintenance_url, future=True, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


async def _drop_database(maintenance_url: str, name: str) -> None:
    engine = create_async_engine(
        maintenance_url, future=True, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        await engine.dispose()


async def _reachable(url: str) -> bool:
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — any connection failure means "skip"
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def scratch_url() -> Iterator[str]:
    """A throwaway database at ``head``, for this module only."""
    if not DATABASE_URL_SUPERUSER:
        pytest.skip("DATABASE_URL_SUPERUSER not set; cannot run vault CLI tests.")
    if not asyncio.run(_reachable(DATABASE_URL_SUPERUSER)):
        pytest.skip(f"Cannot reach Postgres at {DATABASE_URL_SUPERUSER!r}.")

    name = f"portfoliflow_vault_{uuid4().hex[:8]}"
    url = _rebound(DATABASE_URL_SUPERUSER, name)
    asyncio.run(_create_database(DATABASE_URL_SUPERUSER, name))
    try:
        built = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "db/alembic.ini", "upgrade", "head"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "DATABASE_URL_SUPERUSER": url},
        )
        assert built.returncode == 0, f"scratch-database build failed:\n{built.stderr}"
        yield url
    finally:
        asyncio.run(_drop_database(DATABASE_URL_SUPERUSER, name))


# ---------------------------------------------------------------------------
# Seeding / reading (superuser engine — RLS is irrelevant to these helpers)
# ---------------------------------------------------------------------------


async def _seed_async(url: str, cipher: VaultCipher) -> list[UUID]:
    """Seed two tenants, each with two secret rows and one config row."""
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    tenant_ids: list[UUID] = []
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM scoped_settings"))
            await conn.execute(text("DELETE FROM tenants"))
            for index in (1, 2):
                tenant_id = uuid4()
                tenant_ids.append(tenant_id)
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, name, subdomain, is_active) "
                        "VALUES (:id, :name, :sd, TRUE)"
                    ),
                    {
                        "id": str(tenant_id),
                        "name": f"Vault Tenant {index}",
                        "sd": f"vault-{tenant_id.hex[:12]}",
                    },
                )
                for provider, key, plaintext in _SECRETS:
                    await conn.execute(
                        text(
                            "INSERT INTO scoped_settings "
                            "(scope, tenant_id, provider, key, is_secret, value_ciphertext) "
                            "VALUES ('tenant', :tid, :provider, :key, TRUE, :ct)"
                        ),
                        {
                            "tid": str(tenant_id),
                            "provider": provider,
                            "key": key,
                            "ct": cipher.encrypt(f"{plaintext}-{index}"),
                        },
                    )
                await conn.execute(
                    text(
                        "INSERT INTO scoped_settings "
                        "(scope, tenant_id, provider, key, is_secret, value_plain) "
                        "VALUES ('tenant', :tid, 'openrouter', 'model', FALSE, :v)"
                    ),
                    {"tid": str(tenant_id), "v": f"model-{index}"},
                )
    finally:
        await engine.dispose()
    return tenant_ids


async def _read_rows_async(url: str) -> list[dict]:
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT tenant_id, provider, key, is_secret, value_plain, "
                    "value_ciphertext FROM scoped_settings "
                    "ORDER BY tenant_id, provider, key"
                )
            )
            return [dict(row) for row in result.mappings().all()]
    finally:
        await engine.dispose()


def _seed(url: str, cipher: VaultCipher) -> list[UUID]:
    return asyncio.run(_seed_async(url, cipher))


def _read_rows(url: str) -> list[dict]:
    return asyncio.run(_read_rows_async(url))


# ---------------------------------------------------------------------------
# VK-01: vault-generate-key
# ---------------------------------------------------------------------------


def test_vk01_generate_key_emits_one_usable_fernet_key() -> None:
    result = runner.invoke(app, ["vault-generate-key"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one line of output, got {lines!r}"

    key = lines[0].strip()
    # The emitted key really is usable — not merely well-shaped.
    cipher = VaultCipher(key)
    assert cipher.decrypt(cipher.encrypt("round-trip")) == "round-trip"


def test_vk01_generate_key_emits_a_different_key_each_time() -> None:
    first = runner.invoke(app, ["vault-generate-key"]).stdout.strip()
    second = runner.invoke(app, ["vault-generate-key"]).stdout.strip()
    assert first != second


# ---------------------------------------------------------------------------
# VK-02: rotation re-encrypts
# ---------------------------------------------------------------------------


def test_vk02_rotate_re_encrypts_every_secret_row_across_tenants(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_cipher = VaultCipher(_OLD_KEY)
    new_cipher = VaultCipher(_NEW_KEY)
    _seed(scratch_url, old_cipher)
    before = _read_rows(scratch_url)

    monkeypatch.setenv("DATABASE_URL_SUPERUSER", scratch_url)
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _OLD_KEY)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input=f"{_NEW_KEY}\n")

    assert result.exit_code == 0, result.output
    assert "Re-encrypted 4 secret row(s)." in result.stdout

    after = _read_rows(scratch_url)
    assert len(after) == 6  # 4 secret + 2 config
    secret_rows = [row for row in after if row["is_secret"]]
    assert len(secret_rows) == 4
    # Two tenants, both rotated.
    assert len({row["tenant_id"] for row in secret_rows}) == 2

    before_by_key = {
        (row["tenant_id"], row["provider"], row["key"]): row for row in before if row["is_secret"]
    }
    for row in secret_rows:
        identity = (row["tenant_id"], row["provider"], row["key"])
        previous = before_by_key[identity]
        assert row["value_ciphertext"] != previous["value_ciphertext"], (
            f"{identity} was not re-encrypted"
        )
        # The old key no longer opens it; the new one does, and the plaintext
        # survived the round trip unchanged.
        with pytest.raises(VaultDecryptError):
            old_cipher.decrypt(bytes(row["value_ciphertext"]))
        assert new_cipher.decrypt(bytes(row["value_ciphertext"])) == old_cipher.decrypt(
            bytes(previous["value_ciphertext"])
        )

    # Config rows are not touched by a key rotation.
    config_rows = [row for row in after if not row["is_secret"]]
    assert sorted(row["value_plain"] for row in config_rows) == ["model-1", "model-2"]


def test_vk02_rotate_accepts_both_keys_on_stdin_old_first(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_cipher = VaultCipher(_OLD_KEY)
    new_cipher = VaultCipher(_NEW_KEY)
    _seed(scratch_url, old_cipher)

    monkeypatch.setenv("DATABASE_URL_SUPERUSER", scratch_url)
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
    result = runner.invoke(
        app,
        ["vault-rotate-key", "--old-key-stdin", "--new-key-stdin"],
        input=f"{_OLD_KEY}\n{_NEW_KEY}\n",
    )

    assert result.exit_code == 0, result.output
    recovered = sorted(
        new_cipher.decrypt(bytes(row["value_ciphertext"]))
        for row in _read_rows(scratch_url)
        if row["is_secret"]
    )
    assert recovered == sorted(
        f"{plaintext}-{index}" for index in (1, 2) for _, _, plaintext in _SECRETS
    )


def test_vk02_rotate_with_no_secret_rows_is_a_success(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_empty_async(scratch_url))

    monkeypatch.setenv("DATABASE_URL_SUPERUSER", scratch_url)
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _OLD_KEY)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input=f"{_NEW_KEY}\n")

    assert result.exit_code == 0, result.output
    assert "Re-encrypted 0 secret row(s)." in result.stdout


async def _empty_async(url: str) -> None:
    engine = create_async_engine(url, future=True, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM scoped_settings"))
            await conn.execute(text("DELETE FROM tenants"))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# VK-03: a wrong old key rolls back
# ---------------------------------------------------------------------------


def test_vk03_wrong_old_key_rolls_back_and_exits_3(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_cipher = VaultCipher(_OLD_KEY)
    _seed(scratch_url, old_cipher)
    before = _read_rows(scratch_url)

    wrong_key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATABASE_URL_SUPERUSER", scratch_url)
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, wrong_key)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input=f"{_NEW_KEY}\n")

    assert result.exit_code == 3, result.output

    # Byte-for-byte unchanged: the transaction rolled back in full.
    after = _read_rows(scratch_url)
    assert [row["value_ciphertext"] for row in after] == [row["value_ciphertext"] for row in before]
    # Every row still opens with the original key.
    for row in after:
        if row["is_secret"]:
            assert old_cipher.decrypt(bytes(row["value_ciphertext"]))


def test_vk03_failure_output_names_no_secret_material(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(scratch_url, VaultCipher(_OLD_KEY))

    wrong_key = Fernet.generate_key().decode()
    monkeypatch.setenv("DATABASE_URL_SUPERUSER", scratch_url)
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, wrong_key)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input=f"{_NEW_KEY}\n")

    assert result.exit_code == 3
    for forbidden in (_OLD_KEY, _NEW_KEY, wrong_key, "sk-or-v1-tenant-secret", "figi-secret-key"):
        assert forbidden not in result.output


# ---------------------------------------------------------------------------
# VK-04: configuration failures exit 2
# ---------------------------------------------------------------------------


def test_vk04_missing_new_key_flag_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _OLD_KEY)
    result = runner.invoke(app, ["vault-rotate-key"])
    assert result.exit_code == 2, result.output


def test_vk04_missing_old_key_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MASTER_KEY_ENV_VAR, raising=False)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input=f"{_NEW_KEY}\n")
    assert result.exit_code == 2, result.output


def test_vk04_identical_keys_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _OLD_KEY)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input=f"{_OLD_KEY}\n")
    assert result.exit_code == 2, result.output


def test_vk04_malformed_new_key_exits_2_before_touching_rows(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_cipher = VaultCipher(_OLD_KEY)
    _seed(scratch_url, old_cipher)
    before = _read_rows(scratch_url)

    monkeypatch.setenv("DATABASE_URL_SUPERUSER", scratch_url)
    monkeypatch.setenv(MASTER_KEY_ENV_VAR, _OLD_KEY)
    result = runner.invoke(app, ["vault-rotate-key", "--new-key-stdin"], input="not-a-fernet-key\n")

    assert result.exit_code == 2, result.output
    assert _read_rows(scratch_url) == before
