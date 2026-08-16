# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2025-2026 Sönke Pinkernelle

"""Regression: audit engine usage is limited to the sanctioned paths.

The audit engine (bound to the Postgres superuser via
``DATABASE_URL_SUPERUSER``) bypasses RLS by construction; ADR-0036 §1c
introduced it solely so ``login_audit`` inserts can happen even when
no trusted ``app.tenant_id`` exists yet. ADR-0063 §4 extended the
sanctioned usage to two further paths, ADR-0064 §1 added a third,
ADR-0112 §5 a fourth, and ADR-0117 §3 a fifth:

1. ``services/auth/local_password.py`` — ``login_audit`` writes only.
2. ``web/auth.py::get_optional_session`` — the pre-tenant
   session-token resolve from the request cookie.
3. ``services/tenant_resolution/resolver.py`` — the pre-tenant
   subdomain lookup against ``tenants``.
4. ``bot/token_discovery.py`` — the cross-tenant Telegram bot-token
   scan, read-only against ``scoped_settings``. Same shape as the
   Irene tick's ``find_due_tenants`` (ADR-0086): a platform-level
   read that spans tenants and therefore runs before any tenant
   context exists. The engine is built from the URL the web lifespan
   injects, used once at bot start, and disposed.
5. ``web/tick_scheduler.py`` — the built-in in-process tick scheduler
   (ADR-0117 §3), which hands ``app.state.audit_engine`` to the shared
   tick runner rather than constructing a third superuser engine on
   the same URL. Same shape as path 4 again: the cross-tenant due
   reads on ``irene_schedule`` / ``market_data_schedule`` run before
   any tenant context exists. This is the one path whose engine also
   *writes*, and its privileged surface is nonetheless read-only:
   every tenant-scoped statement of a beat — the advisory-lock claim,
   the beat's writes, the schedule advance — runs inside
   ``tenant_context``, which drops the session to the unprivileged
   ``APP_DB_ROLE`` for the rest of that transaction (ADR-0078), so RLS
   is enforced regardless of the role the engine connects as.

This guard parses each file structurally and asserts the engine
touches only the table named for that path. The test does NOT cover
the ``super_admin_audit`` write path; that comes with ADR-0064's
``super_admin_audit`` table landing (migration b013).

Paths 1–4 parse with regular expressions because their invariant is
about SQL *text*: which table name appears inside an engine block.
Path 5's invariant is a different question — how a Python *name* is
used (handed on as an argument vs. having a method called on it) — so
it parses with :mod:`ast` instead. A regex for "no ``engine.``" would
also fire on every docstring sentence ending in "…the audit engine.",
which would make the guard either vacuous or unwritable.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str) -> str:
    return (_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Path 1: services/auth/local_password.py — login_audit writes only
# ---------------------------------------------------------------------------


_LOCAL_PASSWORD_SRC = _read("services", "auth", "local_password.py")

# Domain tables we know about — any reference to these in a SQL
# statement reachable from the audit engine in local_password.py
# would be a violation. The list is conservative; new tables added in
# future migrations should not change the audit-engine surface.
_LOCAL_PASSWORD_FORBIDDEN: frozenset[str] = frozenset(
    {
        "tenants",
        "users",
        "audit_log",
        "data_store_entries",
        "sessions",
        "super_admin_audit",
    }
)


def test_local_password_audit_engine_only_writes_login_audit() -> None:
    """Every ``async with self._audit_engine`` block touches only ``login_audit``."""
    blocks = re.findall(
        r"async with self\._audit_engine[\s\S]+?(?=\n    (?:async )?def |\n\n)",
        _LOCAL_PASSWORD_SRC,
    )
    assert blocks, (
        "No `async with self._audit_engine` blocks found in "
        "local_password.py — the symbol may have been renamed; "
        "update this regression guard."
    )

    for block in blocks:
        assert "login_audit" in block, (
            f"Audit-engine code path that does not mention `login_audit`:\n{block}"
        )
        for forbidden in _LOCAL_PASSWORD_FORBIDDEN:
            pattern = rf"\b{re.escape(forbidden)}\b"
            assert not re.search(pattern, block), (
                f"Audit-engine code path references forbidden table "
                f"{forbidden!r}; only `login_audit` is permitted:\n"
                f"{block}"
            )


# ---------------------------------------------------------------------------
# Path 2: web/auth.py::get_optional_session — sessions read only
# ---------------------------------------------------------------------------


_WEB_AUTH_SRC = _read("web", "auth.py")

_WEB_AUTH_FORBIDDEN: frozenset[str] = frozenset(
    {
        "tenants",
        "users",
        "audit_log",
        "login_audit",
        "data_store_entries",
        "super_admin_audit",
    }
)


def test_web_auth_audit_engine_only_reads_sessions() -> None:
    """``get_optional_session`` uses the audit engine only to read ``sessions``."""
    # Pull every ``async with audit_engine.connect()`` or
    # ``async with audit_engine.begin()`` block in web/auth.py.
    blocks = re.findall(
        r"async with audit_engine\.(?:connect|begin)\(\)[\s\S]+?(?=\n    (?:async )?def |\n\n)",
        _WEB_AUTH_SRC,
    )
    assert blocks, (
        "No audit-engine code blocks found in web/auth.py — the "
        "session-token resolve path appears to be gone; update this "
        "regression guard."
    )
    for block in blocks:
        assert "FROM sessions" in block or "from sessions" in block, (
            f"web/auth.py audit-engine block must SELECT from `sessions`:\n{block}"
        )
        for forbidden in _WEB_AUTH_FORBIDDEN:
            pattern = rf"\b{re.escape(forbidden)}\b"
            assert not re.search(pattern, block), (
                f"web/auth.py audit-engine block references forbidden table {forbidden!r}:\n{block}"
            )


# ---------------------------------------------------------------------------
# Path 3: services/tenant_resolution/resolver.py — tenants read only
# ---------------------------------------------------------------------------


_RESOLVER_SRC = _read("services", "tenant_resolution", "resolver.py")

_RESOLVER_FORBIDDEN: frozenset[str] = frozenset(
    {
        "users",
        "audit_log",
        "login_audit",
        "data_store_entries",
        "sessions",
        "super_admin_audit",
    }
)


def test_tenant_resolver_audit_engine_only_reads_tenants() -> None:
    """The subdomain resolver uses its engine only to read ``tenants``."""
    # Pull every ``async with self._engine.connect()`` /
    # ``async with self._engine.begin()`` block.
    blocks = re.findall(
        r"async with self\._engine\.(?:connect|begin)\(\)[\s\S]+?(?=\n    (?:async )?def |\nclass )",
        _RESOLVER_SRC,
    )
    assert blocks, (
        "No engine blocks found in tenant_resolution/resolver.py — "
        "the subdomain-resolver lookup appears to be gone; update "
        "this regression guard."
    )
    for block in blocks:
        assert "FROM tenants" in block, "Resolver block must SELECT from `tenants`:\n" + block
        for forbidden in _RESOLVER_FORBIDDEN:
            pattern = rf"\b{re.escape(forbidden)}\b"
            assert not re.search(pattern, block), (
                f"Resolver block references forbidden table {forbidden!r}:\n{block}"
            )


# ---------------------------------------------------------------------------
# Path 4: bot/token_discovery.py — scoped_settings read only (ADR-0112 §5)
# ---------------------------------------------------------------------------


_TOKEN_DISCOVERY_SRC = _read("bot", "token_discovery.py")

_TOKEN_DISCOVERY_FORBIDDEN: frozenset[str] = frozenset(
    {
        "tenants",
        "users",
        "audit_log",
        "login_audit",
        "data_store_entries",
        "sessions",
        "super_admin_audit",
    }
)


def test_token_discovery_superuser_engine_only_reads_scoped_settings() -> None:
    """The bot-token scan uses its engine only to read ``scoped_settings``."""
    blocks = re.findall(
        r"async with engine\.(?:connect|begin)\(\)[\s\S]+?(?=\n(?:async )?def |\Z)",
        _TOKEN_DISCOVERY_SRC,
    )
    assert blocks, (
        "No engine blocks found in bot/token_discovery.py — the "
        "bot-token scan appears to be gone; update this regression guard."
    )
    for block in blocks:
        assert "FROM scoped_settings" in block, (
            f"Discovery block must SELECT from `scoped_settings`:\n{block}"
        )
        for forbidden in _TOKEN_DISCOVERY_FORBIDDEN:
            pattern = rf"\b{re.escape(forbidden)}\b"
            assert not re.search(pattern, block), (
                f"Discovery block references forbidden table {forbidden!r}:\n{block}"
            )


def test_token_discovery_writes_nothing() -> None:
    """The scan is read-only: no DML anywhere in the module.

    The engine bypasses RLS, so a write here would be a write into *any*
    tenant with no policy to stop it. There is no legitimate reason for
    this module to have one.
    """
    for statement in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "):
        assert statement not in _TOKEN_DISCOVERY_SRC.upper(), (
            f"bot/token_discovery.py must not contain {statement.strip()!r} — "
            "the discovery scan is read-only (ADR-0112 §5)."
        )


# ---------------------------------------------------------------------------
# Path 5: web/tick_scheduler.py + services/scheduler/tick_runner.py —
# the built-in tick scheduler's due reads (ADR-0117 §3)
# ---------------------------------------------------------------------------


_TICK_SCHEDULER_SRC = _read("web", "tick_scheduler.py")
_TICK_RUNNER_SRC = _read("services", "scheduler", "tick_runner.py")

# The scheduler may hand the audit engine to these callables and nothing
# else: the runner's two entry points (ADR-0117 §2) plus its own loop,
# which the same assertions cover.
_ENGINE_SINKS: frozenset[str] = frozenset(
    {"run_irene_tick", "run_market_data_tick", "run_tick_scheduler"}
)

# Attribute names that would mean the scheduler talks to Postgres itself
# instead of through the runner. ``dispose`` is in the list for a second
# reason: the web app's engine is process-lived, and a tick that disposed
# it would take ``/login`` down with it.
_DB_ATTRIBUTES: frozenset[str] = frozenset({"connect", "begin", "execute", "dispose"})

# Every table the runner's *direct* (non-``tenant_context``) engine blocks
# must stay away from. ADR-0117 §3 confines the superuser-privileged
# surface to the cross-tenant due reads, which reach the two schedule
# tables through ``find_due_tenants`` — a call, not SQL, so nothing below
# should name a table at all. Conservative, in the style of paths 1–4.
_TICK_RUNNER_FORBIDDEN: frozenset[str] = frozenset(
    {
        "tenants",
        "users",
        "sessions",
        "login_audit",
        "audit_log",
        "super_admin_audit",
        "scoped_settings",
        "data_store_entries",
        "investments",
        "investment_navs",
        "investment_cashflows",
        "irene_findings",
        "watchpoints",
        "cases",
    }
)


def _loaded_names(tree: ast.Module, name: str) -> list[ast.Name]:
    """Every ``Load`` reference to ``name`` in the tree.

    Parameter declarations are ``ast.arg`` nodes and docstring prose is a
    ``Constant``, so neither appears here — only places where the value is
    actually used.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    ]


def _call_arg_ids(tree: ast.Module, callees: frozenset[str]) -> set[int]:
    """Node ids of every positional argument passed to the named callables."""
    return {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in callees
        for arg in node.args
    }


def test_tick_scheduler_hands_the_engine_only_to_the_runner() -> None:
    """The scheduler passes the audit engine on; it never uses it itself.

    This is what makes path 5 assertable at all: the module has no
    database surface of its own, so the whole question of *what* the
    engine touches reduces to the runner's own guarded shape below.
    """
    tree = ast.parse(_TICK_SCHEDULER_SRC)

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _ENGINE_SINKS
    }
    assert {"run_irene_tick", "run_market_data_tick"} <= called, (
        "web/tick_scheduler.py no longer calls both shared runner entry "
        "points — the scheduler's shape changed; update this regression "
        f"guard. Found: {sorted(called)}"
    )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "engine"
        ):
            raise AssertionError(
                "web/tick_scheduler.py calls `engine."
                f"{node.attr}(...)` at line {node.lineno}. The scheduler must "
                "only hand the RLS-bypassing engine to the shared runner "
                "(ADR-0117 §3); it has no sanctioned database surface of its "
                "own."
            )

    sanctioned = _call_arg_ids(tree, _ENGINE_SINKS)
    for node in _loaded_names(tree, "engine"):
        assert id(node) in sanctioned, (
            f"web/tick_scheduler.py uses `engine` at line {node.lineno} for "
            "something other than an argument to the shared runner "
            f"({', '.join(sorted(_ENGINE_SINKS))})."
        )


def test_tick_scheduler_issues_no_sql_of_its_own() -> None:
    """No connection, no transaction, no statement anywhere in the module.

    Broader than the engine-specific assertion above on purpose: a
    connection obtained by any other route would be just as RLS-bypassing
    as one taken off the engine parameter.
    """
    tree = ast.parse(_TICK_SCHEDULER_SRC)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _DB_ATTRIBUTES:
            raise AssertionError(
                f"web/tick_scheduler.py calls `.{node.attr}(...)` at line "
                f"{node.lineno} — the scheduler is wiring, not a database "
                "consumer (ADR-0117 §3)."
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "text", (
                f"web/tick_scheduler.py builds a SQL construct at line "
                f"{node.lineno}; every statement belongs in the shared runner."
            )


def test_tick_scheduler_never_reaches_the_cli_test_seams() -> None:
    """The scheduler never passes ``tenant_ref`` / ``provider``.

    Not style: ``tenant_ref`` is the only way into the runner's
    ``_resolve_single_tenant``, which is the one place a direct engine
    connection reads a table other than a schedule table (``tenants``, to
    turn a subdomain into an id). Keeping the flags out of this call site
    is what makes "path 5 touches only the two schedule tables" true of
    the *scheduler*, whatever the CLI wrappers may do with their flags.
    """
    tree = ast.parse(_TICK_SCHEDULER_SRC)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_market_data_tick"
        ):
            passed = {kw.arg for kw in node.keywords}
            assert not passed & {"tenant_ref", "provider"}, (
                "web/tick_scheduler.py passes a CLI test-seam flag "
                f"({sorted(passed & {'tenant_ref', 'provider'})}) at line "
                f"{node.lineno}; the in-process scheduler must drive the "
                "plain due-read path only (ADR-0093 §0.4, ADR-0117 §3)."
            )


def test_tick_runner_uses_the_engine_directly_only_for_the_due_read() -> None:
    """Direct engine use is ``connect()``; everything else is ``tenant_context``.

    The two shapes are the whole of ADR-0117 §3. ``engine.connect()`` is
    the cross-tenant due read — RLS-bypassing by design, read-only by
    virtue of being a connection rather than a transaction. Every other
    use must be ``tenant_context(engine, …)``, which drops the session to
    the unprivileged app role (ADR-0078), so the beat's writes are
    RLS-enforced even though the engine connects as superuser. A stray
    ``engine.begin()`` — a writable superuser transaction outside any
    tenant context — is exactly what this catches.
    """
    tree = ast.parse(_TICK_RUNNER_SRC)

    connect_targets: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "engine"
        ):
            assert node.attr == "connect", (
                f"services/scheduler/tick_runner.py calls `engine.{node.attr}"
                f"(...)` at line {node.lineno}. The only sanctioned direct use "
                "of the RLS-bypassing engine is `engine.connect()` for the "
                "cross-tenant due read (ADR-0117 §3)."
            )
            connect_targets.add(id(node.value))

    tenant_scoped = _call_arg_ids(tree, frozenset({"tenant_context"}))
    for node in _loaded_names(tree, "engine"):
        assert id(node) in connect_targets or id(node) in tenant_scoped, (
            "services/scheduler/tick_runner.py uses `engine` at line "
            f"{node.lineno} outside both sanctioned shapes (`engine.connect()` "
            "for the due read, `tenant_context(engine, …)` for a tenant's "
            "transaction)."
        )

    assert len(connect_targets) == 2, (
        "Expected exactly two `engine.connect()` due reads (one per tick "
        f"domain); found {len(connect_targets)}. The runner's shape changed — "
        "update this regression guard."
    )
    assert len(tenant_scoped) >= 2, (
        "Expected each tick domain to open its per-tenant work through "
        "`tenant_context(engine, …)`; the guard found fewer than two."
    )


def test_tick_runner_due_read_blocks_touch_no_domain_table() -> None:
    """Each ``engine.connect()`` block stays on the schedule surface.

    The block bodies are the due reads and nothing else, so no table name
    should appear in them at all — the schedule tables included, since
    those are reached through ``find_due_tenants``. Asserting the absence
    of *domain* tables is the conservative form of that, matching paths
    1–4.
    """
    tree = ast.parse(_TICK_RUNNER_SRC)
    lines = _TICK_RUNNER_SRC.splitlines()

    blocks: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            ctx = item.context_expr
            if (
                isinstance(ctx, ast.Call)
                and isinstance(ctx.func, ast.Attribute)
                and isinstance(ctx.func.value, ast.Name)
                and ctx.func.value.id == "engine"
            ):
                blocks.append("\n".join(lines[node.lineno - 1 : node.end_lineno]))

    assert len(blocks) == 2, (
        "Expected two direct-engine blocks in tick_runner.py (the two due "
        f"reads); found {len(blocks)}. Update this regression guard."
    )

    for block in blocks:
        for forbidden in _TICK_RUNNER_FORBIDDEN:
            assert not re.search(rf"\b{re.escape(forbidden)}\b", block), (
                f"Direct-engine block names forbidden table {forbidden!r}; the "
                "RLS-bypassing surface is the two schedule tables' due read "
                f"only (ADR-0117 §3):\n{block}"
            )
        for statement in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "):
            assert statement not in block.upper(), (
                f"Direct-engine block contains {statement.strip()!r} — the due "
                "read is read-only; every write belongs inside "
                f"`tenant_context` (ADR-0117 §3):\n{block}"
            )
