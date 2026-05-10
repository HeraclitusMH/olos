"""Smoke tests for the initial Alembic migration.

The migration file imports `alembic.op`, which is a runtime proxy and only
resolves inside an active Alembic environment. Parsing via AST lets us inspect
the metadata (revision id, down_revision, function presence) without executing
the module-level imports.
"""
from __future__ import annotations

import ast
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0001_initial_schema.py"
)


def _parse_migration() -> ast.Module:
    return ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))


def _module_assignments(module: ast.Module) -> dict[str, ast.expr]:
    bindings: dict[str, ast.expr] = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
    return bindings


def _top_level_function_names(module: ast.Module) -> set[str]:
    return {node.name for node in module.body if isinstance(node, ast.FunctionDef)}


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.is_file()


def test_migration_revision_id_is_0001() -> None:
    bindings = _module_assignments(_parse_migration())
    revision = bindings.get("revision")
    assert isinstance(revision, ast.Constant)
    assert revision.value == "0001"


def test_migration_is_root_revision() -> None:
    bindings = _module_assignments(_parse_migration())
    down = bindings.get("down_revision")
    assert isinstance(down, ast.Constant)
    assert down.value is None


def test_migration_exposes_upgrade_and_downgrade() -> None:
    funcs = _top_level_function_names(_parse_migration())
    assert "upgrade" in funcs
    assert "downgrade" in funcs


def test_migration_creates_all_five_tables() -> None:
    body = MIGRATION_PATH.read_text(encoding="utf-8")
    for table in ("users", "messages", "google_accounts", "event_references", "memories"):
        assert f'"{table}"' in body, f"migration does not reference table {table!r}"


def test_migration_installs_search_vector_trigger() -> None:
    body = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "memories_search_vector_update" in body
    assert "trg_memories_search_vector" in body
    assert "BEFORE INSERT OR UPDATE ON memories" in body
