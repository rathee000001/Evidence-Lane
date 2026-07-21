from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.workspace.canonical_migration import (
    execute_canonical_migration,
    plan_canonical_migration,
    verify_canonical_migration,
)
from sqlite_brain_builder.workspace.workspace_roots import (
    assert_canonical_production_workspace,
    canonical_production_root,
    canonical_route_state,
    current_workspace_root,
    default_workspace_dir,
    list_workspace_roots,
    roaming_config_root,
    workspace_root_registry_path,
)


def _native_canonical(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("EVIDENCE_OS_NATIVE_PRODUCTION", "1")
    monkeypatch.setenv("EVIDENCE_OS_CANONICAL_ROOT", str(root))
    monkeypatch.setenv("EVIDENCE_OS_ROAMING_DIR", str(root.parent / "legacy-roaming-must-not-win"))


def test_native_production_defaults_config_and_registry_to_one_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = (tmp_path / "EvidenceLane").resolve()
    _native_canonical(monkeypatch, canonical)

    assert canonical_production_root() == canonical
    assert default_workspace_dir() == canonical
    assert current_workspace_root() == canonical
    assert roaming_config_root() == canonical / "config"
    assert workspace_root_registry_path() == canonical / "config" / "workspace-roots.json"
    assert list_workspace_roots()["active_root"] == str(canonical)

    missing = canonical_route_state()
    assert missing["status"] == "CANONICAL_ROOT_MISSING"
    assert missing["production_native"] is True
    canonical.mkdir()
    ready = canonical_route_state()
    assert ready["status"] == "CANONICAL_ROOT_READY"
    assert ready["legacy_route_allowed"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path spelling")
def test_native_production_accepts_equivalent_windows_extended_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = (tmp_path / "EvidenceLane").resolve()
    canonical.mkdir()
    _native_canonical(monkeypatch, canonical)
    extended = Path("\\\\?\\" + str(canonical))

    assert assert_canonical_production_workspace(extended) == canonical

    monkeypatch.setenv("EVIDENCE_OS_CANONICAL_ROOT", str(extended))
    assert canonical_production_root() == canonical


def test_native_worker_rejects_an_explicit_legacy_workspace_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = (tmp_path / "EvidenceLane").resolve()
    canonical.mkdir()
    legacy = (tmp_path / "_01NEWBRAIN").resolve()
    legacy.mkdir()
    _native_canonical(monkeypatch, canonical)

    with pytest.raises(
        ipc_worker.WorkerError,
        match="NON_CANONICAL_PRODUCTION_WORKSPACE_FORBIDDEN",
    ):
        ipc_worker.handle(
            {
                "command": "workspace.init",
                "payload": {"workspace_dir": str(legacy)},
            }
        )

    accepted = ipc_worker.handle(
        {
            "command": "workspace.init",
            "payload": {"workspace_dir": str(canonical)},
        }
    )
    assert accepted["workspace_dir"] == str(canonical)
    assert accepted["workspace_roots"]["active_root"] == str(canonical)


def test_native_bootstrap_ignores_inherited_development_workspace_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = (tmp_path / "EvidenceLane").resolve()
    canonical.mkdir()
    inherited_legacy = (tmp_path / "legacy-environment-route").resolve()
    inherited_legacy.mkdir()
    _native_canonical(monkeypatch, canonical)
    monkeypatch.setenv("EVIDENCE_OS_WORKSPACE", str(inherited_legacy))

    assert ipc_worker._workspace({}) == canonical


def test_native_catalog_cache_rejects_stale_legacy_output_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = (tmp_path / "EvidenceLane").resolve()
    legacy = (tmp_path / "_01NEWBRAIN").resolve()
    (canonical / "config").mkdir(parents=True)
    legacy.mkdir()
    _native_canonical(monkeypatch, canonical)
    cache = {
        "schema": "EVIDENCEOS_CATALOG_WORKSPACE_CACHE_V1",
        "roots": {
            str(canonical).casefold(): {
                "root_path": str(canonical),
                "workspaces": [str(canonical)],
            }
        },
        "catalog_snapshot": {
            "payload": {
                "active_root": str(canonical),
                "brains": [{"brain_name": "book", "output_dir": str(legacy / "book_output")}],
            }
        },
    }
    (canonical / "config" / "brain-catalog-workspaces.json").write_text(
        json.dumps(cache), encoding="utf-8"
    )

    loaded = ipc_worker._catalog_workspace_cache_load()
    assert loaded == {
        "schema": "EVIDENCEOS_CATALOG_WORKSPACE_CACHE_V1",
        "roots": {},
    }


def test_copy_first_canonical_migration_preserves_source_and_verifies_every_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (tmp_path / "_01NEWBRAIN").resolve()
    canonical = (tmp_path / "EvidenceLane").resolve()
    (source / "book_output" / "project" / "topology").mkdir(parents=True)
    (source / ".evidenceos_runtime").mkdir()
    (source / "empty_recycled_output").mkdir()
    source_database = source / "workspace.sqlite"
    connection = sqlite3.connect(source_database)
    connection.executescript(
        """
        CREATE TABLE brain_project (
            brain_name TEXT PRIMARY KEY,
            output_dir TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE brain_last_state_pointer (
            brain_id TEXT PRIMARY KEY,
            output_folder TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO brain_project VALUES (?,?,?)",
        ("book", str(source / "book_output"), "ACTIVE"),
    )
    connection.execute(
        "INSERT INTO brain_last_state_pointer VALUES (?,?)",
        ("brain-book", str(source / "book_output")),
    )
    connection.commit()
    connection.close()
    (source / "book_output" / "project" / "topology" / "CURRENT.json").write_text(
        '{"saved":true}\n', encoding="utf-8"
    )
    (source / ".evidenceos_runtime" / "universal_task_event.json").write_text(
        '{"task_id":"task_fixture"}\n', encoding="utf-8"
    )
    _native_canonical(monkeypatch, canonical)

    plan = plan_canonical_migration(source, canonical)
    assert plan["status"] == "READY_COPY_FIRST"
    assert plan["source_file_count"] == 3
    assert plan["source_bytes"] > 0
    assert plan["source_deleted"] is False
    assert plan["source_directory_count"] >= 4

    result = execute_canonical_migration(source, canonical, actor="T023_TEST")
    assert result["status"] == "PASS_CANONICAL_ROOT_MIGRATED_AND_ROUTED"
    assert result["source_deleted"] is False
    assert source.is_dir()
    assert canonical.is_dir()
    assert result["verified_file_count"] == 3
    assert result["source_tree_sha256"] == plan["source_tree_sha256"]
    assert result["rewritten_route_count"] == 2
    assert result["legacy_live_route_count"] == 0
    assert result["sqlite_integrity_check"] == "ok"
    assert (canonical / "empty_recycled_output").is_dir()
    source_connection = sqlite3.connect(source_database)
    target_connection = sqlite3.connect(canonical / "workspace.sqlite")
    assert source_connection.execute(
        "SELECT output_dir FROM brain_project WHERE brain_name='book'"
    ).fetchone()[0] == str(source / "book_output")
    assert target_connection.execute(
        "SELECT output_dir FROM brain_project WHERE brain_name='book'"
    ).fetchone()[0] == str(canonical / "book_output")
    assert target_connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    source_connection.close()
    target_connection.close()
    assert (canonical / "migration" / "T023_CANONICAL_ROOT_MIGRATION_MANIFEST.json").is_file()
    assert (canonical / "migration" / "T023_CANONICAL_ROOT_MIGRATION_RECEIPT.json").is_file()
    assert (canonical / "migration" / "T023_CANONICAL_ROOT_ROUTE_REWRITE_RECEIPT.json").is_file()

    verified = verify_canonical_migration(source, canonical)
    assert verified["status"] == "PASS_CANONICAL_ROOT_MIGRATED_AND_ROUTED"
    assert verified["source_tree_sha256"] == result["source_tree_sha256"]
    assert verified["unexpected_destination_authority_files"] == []
    registry = json.loads(workspace_root_registry_path().read_text(encoding="utf-8"))
    assert registry["active_root"] == str(canonical)
    assert [row["path"] for row in registry["roots"]] == [str(canonical)]
    assert current_workspace_root() == canonical


def test_migration_refuses_to_merge_with_an_existing_nonempty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (tmp_path / "_01NEWBRAIN").resolve()
    canonical = (tmp_path / "EvidenceLane").resolve()
    source.mkdir()
    canonical.mkdir()
    (source / "workspace.sqlite").write_bytes(b"source")
    (canonical / "foreign.txt").write_text("must not merge", encoding="utf-8")
    _native_canonical(monkeypatch, canonical)

    with pytest.raises(RuntimeError, match="CANONICAL_TARGET_ALREADY_NONEMPTY"):
        execute_canonical_migration(source, canonical, actor="T023_TEST")

    assert (canonical / "foreign.txt").read_text(encoding="utf-8") == "must not merge"
    assert (source / "workspace.sqlite").read_bytes() == b"source"
