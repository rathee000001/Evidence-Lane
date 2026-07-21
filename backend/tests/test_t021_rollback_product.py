from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import sqlite_brain_builder.rollback_product as rollback_product
import sqlite_brain_builder.runtime.stable_runtime_v53 as stable_runtime_v53
from sqlite_brain_builder.brain_versions import capture_brain_version, list_brain_versions
from sqlite_brain_builder.rollback_product import rollback_brain_as_complete_product
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain, render_topology
from sqlite_brain_builder.workspace.workspace_db import init_workspace


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_rollback_restores_complete_env15_brain_rebuilds_outputs_versions_and_registers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "source.md"
    source.write_text("# Version one\nImmutable governed state.\n", encoding="utf-8")
    brain_name = "Rollback Source Brain"
    built = build_brain(
        str(workspace),
        brain_name,
        [{"source_id": "source_code", "lane_key": "local_code", "path": str(repository), "active": True}],
        generate_mmd=False,
    )
    render_topology(str(workspace), brain_name)
    version = capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS SQLite Builder",
        reason="rollback product fixture",
    )
    source_root = Path(built["brain_root"])
    source_router_hash = _sha256(source_root / "project" / "project_router.sqlite")

    restored = rollback_brain_as_complete_product(
        workspace,
        brain_name,
        version["version_id"],
        actor_name="Evidence OS SQLite Builder",
        reason="end-to-end rollback fixture",
    )

    restored_root = Path(restored["restored_output"])
    assert restored["status"] == "PASS"
    assert restored_root != source_root
    assert (restored_root / ".uepc_env").is_file()
    assert (restored_root / "env" / "env_sqlite.sqlite").is_file()
    assert (restored_root / "uop" / "uop_sqlite.sqlite").is_file()
    assert (restored_root / "project" / "project_router.sqlite").is_file()
    assert Path(restored["chatgpt_package"]).is_file()
    assert Path(restored["gemini_package"]).is_file()
    assert Path(restored["codex_package"]).is_file()
    assert Path(restored["receipt_path"]).is_file()
    assert restored["chatgpt_validation"]["status"] == "PASS"
    assert restored["gemini_validation"]["status"] == "PASS"
    assert restored["codex_validation"]["status"] == "PASS"
    assert (
        restored["gemini_validation"]["source_chatgpt_package_sha256"]
        == restored["chatgpt_validation"]["sha256"]
    )
    assert restored["source_version_integrity"] == "VERIFIED"
    assert restored["source_manifest_sha256_before"] == restored["source_manifest_sha256_after"]
    assert restored["source_brain_fingerprint_before"] == restored["source_brain_fingerprint_after"]
    assert list_brain_versions(workspace, restored["restored_brain_name"], verify_hashes=True)["version_count"] == 1
    assert _sha256(source_root / "project" / "project_router.sqlite") == source_router_hash

    workspace_db = workspace / "workspace.sqlite"
    connection = sqlite3.connect(workspace_db)
    try:
        assert connection.execute(
            "SELECT status FROM brain_project WHERE brain_name=?", (restored["restored_brain_name"],)
        ).fetchone()[0] == "ACTIVE"
    finally:
        connection.close()


def test_rollback_accepts_canonical_optional_provider_skips_but_still_builds_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "source.md").write_text("# Cap fixture\n", encoding="utf-8")
    brain_name = "Rollback Cap Source"
    build_brain(
        str(workspace),
        brain_name,
        [{"source_id": "source_code", "lane_key": "local_code", "path": str(repository), "active": True}],
        generate_mmd=False,
    )
    render_topology(str(workspace), brain_name)
    version = capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS SQLite Builder",
        reason="rollback cap fixture",
    )
    monkeypatch.setattr(stable_runtime_v53, "CHATGPT_PACKAGE_MAX_BYTES", 1)

    restored = rollback_brain_as_complete_product(
        workspace,
        brain_name,
        version["version_id"],
        restored_brain_name="Rollback Cap Restored",
    )

    assert restored["chatgpt_package"] == ""
    assert restored["gemini_package"] == ""
    assert restored["chatgpt_validation"]["status"] == "SKIPPED"
    assert restored["gemini_validation"]["status"] == "SKIPPED"
    assert Path(restored["chatgpt_skip_marker"]).is_file()
    assert Path(restored["gemini_skip_marker"]).is_file()
    assert Path(restored["codex_package"]).is_file()
    assert restored["codex_validation"]["status"] == "PASS"
    assert restored["provider_package_chain"]["chatgpt_local_ai"]["pipeline_failure"] is False
    assert restored["provider_package_chain"]["gemini"]["pipeline_failure"] is False


def test_failed_rollback_never_registers_or_changes_the_source_brain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "source.md").write_text("# Failure fixture\n", encoding="utf-8")
    brain_name = "Rollback Failure Source"
    built = build_brain(
        str(workspace),
        brain_name,
        [{"source_id": "source_code", "lane_key": "local_code", "path": str(repository), "active": True}],
        generate_mmd=False,
    )
    render_topology(str(workspace), brain_name)
    version = capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS SQLite Builder",
        reason="rollback failure fixture",
    )
    source_router = Path(built["brain_root"]) / "project" / "project_router.sqlite"
    source_router_hash = _sha256(source_router)

    def fail_provider_export(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("FORCED_PROVIDER_EXPORT_FAILURE")

    monkeypatch.setattr(rollback_product, "export_one_upload_package", fail_provider_export)
    restored_name = "Rollback Must Not Register"
    with pytest.raises(RuntimeError, match="FORCED_PROVIDER_EXPORT_FAILURE"):
        rollback_brain_as_complete_product(
            workspace,
            brain_name,
            version["version_id"],
            restored_brain_name=restored_name,
        )

    assert not (workspace / restored_name).exists()
    assert _sha256(source_router) == source_router_hash
    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM brain_project WHERE brain_name=?", (restored_name,)
        ).fetchone()[0] == 0
    finally:
        connection.close()
