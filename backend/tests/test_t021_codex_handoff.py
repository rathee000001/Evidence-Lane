from __future__ import annotations

import sqlite3
import zipfile
import json
from pathlib import Path
import pytest

import sqlite_brain_builder.codex_handoff as codex_handoff

from sqlite_brain_builder.brain_versions import capture_brain_version
from sqlite_brain_builder.codex_handoff import (
    HANDOFF_FILES,
    SEMANTIC_QUESTION_IDS,
    create_codex_brain_handoff,
    mark_current_passing_build_good,
    validate_codex_handoff_package,
)
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
    render_topology,
)


def _passing_version(workspace: Path, brain_name: str, source: Path, reason: str) -> dict:
    build_brain(
        str(workspace),
        brain_name,
        [{"source_id": "source_code", "lane_key": "local_code", "path": str(source.parent), "active": True}],
        generate_mmd=False,
    )
    render_topology(str(workspace), brain_name)
    export_one_upload_package(str(workspace), brain_name)
    export_gemini_exact10(str(workspace), brain_name)
    capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS SQLite Builder",
        reason=reason,
    )
    return mark_current_passing_build_good(
        workspace,
        brain_name,
        test_status="PASS",
        build_status="PASS",
        reason=reason,
    )


def _mutate_zip(source: Path, target: Path, mutations: dict[str, bytes | None], additions: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(source, "r") as before, zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as after:
        for info in before.infolist():
            if info.is_dir():
                continue
            replacement = mutations.get(info.filename, before.read(info.filename))
            if replacement is not None:
                after.writestr(info.filename, replacement)
        for name, data in (additions or {}).items():
            after.writestr(name, data)


def test_handoff_preflight_requires_passing_build_and_revalidates_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chatgpt = tmp_path / "ChatGPT_brain.zip"
    gemini = tmp_path / "Gemini_brain.zip"
    with zipfile.ZipFile(chatgpt, "w") as archive:
        archive.writestr("governed.txt", b"chatgpt-package")
    with zipfile.ZipFile(gemini, "w") as archive:
        archive.writestr("exact10.txt", b"gemini-package")
    current = {
        "test_status": "PASS",
        "build_status": "PASS",
        "brain_package_path": str(chatgpt),
        "brain_package_hash": codex_handoff._sha256(chatgpt),
    }
    monkeypatch.setattr(codex_handoff, "validate_chatgpt_package", lambda *_: {"status": "PASS", "errors": []})
    monkeypatch.setattr(codex_handoff, "_latest_gemini_package", lambda *_: gemini)
    monkeypatch.setattr(codex_handoff, "validate_gemini_exact10", lambda *_: {"status": "PASS", "errors": []})

    result = codex_handoff._validate_handoff_preflight(tmp_path, current)
    assert result["test_status"] == "PASS"
    assert result["build_status"] == "PASS"
    assert result["chatgpt_package_hash"] == codex_handoff._sha256(chatgpt)
    assert result["gemini_package_hash"] == codex_handoff._sha256(gemini)

    current["build_status"] = "FAIL"
    with pytest.raises(codex_handoff.CodexHandoffError, match="CODEX_HANDOFF_BUILD_STATUS_NOT_PASS"):
        codex_handoff._validate_handoff_preflight(tmp_path, current)
    current["build_status"] = "PASS"
    current["brain_package_hash"] = "0" * 64
    with pytest.raises(codex_handoff.CodexHandoffError, match="CODEX_HANDOFF_CURRENT_PACKAGE_HASH_MISMATCH"):
        codex_handoff._validate_handoff_preflight(tmp_path, current)


def test_previous_good_resolution_uses_immediately_preceding_verified_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    connection = codex_handoff._connect(root)
    try:
        for index, status in ((1, "PASS"), (2, "FAIL"), (3, "PASS")):
            snapshot_id = f"good_{index}"
            connection.execute(
                "INSERT INTO brain_good_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
                (snapshot_id, f"version_{index}", f"hash_{index}", "package.zip", "package_hash", "PASS", "PASS", f"2026-07-11T00:00:0{index}Z", "test"),
            )
            connection.execute(
                "INSERT INTO brain_semantic_memory_version VALUES(?,?,?,?,?,?,?,?)",
                (f"memory_{index}", snapshot_id, f"version_{index}", f"hash_{index}", "PASS", "PASS", status, f"2026-07-11T00:00:0{index}Z"),
            )
        connection.commit()
    finally:
        connection.close()

    current, previous = codex_handoff._resolve_verified_good_pair(root)
    assert current["snapshot_id"] == "good_3"
    assert previous["snapshot_id"] == "good_1"


def test_handoff_paths_cannot_enter_governed_runtime_areas(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    codex_folder = root / "codex_handoff" / "receipt"
    package = root / "packages" / "handoff.zip"
    codex_handoff._assert_external_handoff_paths(root, codex_folder, package)
    with pytest.raises(codex_handoff.CodexHandoffError, match="GOVERNED_RUNTIME_PATH_FORBIDDEN"):
        codex_handoff._assert_external_handoff_paths(root, root / "project" / "codex_handoff", package)


def test_codex_handoff_diff_has_required_direct_artifacts_relational_rows_and_complete_brain(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "evidence.md"
    brain_name = "Codex Handoff Brain"
    source.write_text("# Version one\nInitial evidence.\n", encoding="utf-8")
    previous = _passing_version(workspace, brain_name, source, "version one passing")
    source.write_text("# Version two\nChanged governed evidence.\n", encoding="utf-8")
    current = _passing_version(workspace, brain_name, source, "version two passing")

    code_database = workspace / "codex_handoff_brain_output" / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    with sqlite3.connect(code_database) as code_connection:
        prior_good_id, summary_json = code_connection.execute(
            "SELECT prior_good_snapshot_id,summary_json FROM code_semantic_diff ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert prior_good_id
        assert json.loads(summary_json)["prior_snapshot_basis"] == "PREVIOUS_GOOD_CODE_SNAPSHOT"

    result = create_codex_brain_handoff(workspace, brain_name)

    assert result["status"] == "PASS"
    assert result["package_validation"]["status"] == "PASS"
    assert result["incremental_reuse"]["reused"] is False
    assert result["changed_file_count"] >= 1
    assert "local_code" in result["changed_sectors"]
    folder = Path(result["handoff_folder"])
    assert tuple(sorted(path.name for path in folder.iterdir() if path.is_file())) == tuple(sorted(HANDOFF_FILES))
    changed_report = (folder / "CHANGED_FILES_AND_ROUTES.md").read_text(encoding="utf-8")
    semantic_report = (folder / "SYMBOL_DEPENDENCY_TEST_CHANGES.md").read_text(encoding="utf-8")
    assert "## Code-lane synthetic snapshot diff" in changed_report
    assert "## Route/API impact" in changed_report
    assert "## Symbols" in semantic_report
    assert "## Dependencies" in semantic_report
    assert "## Tests affected" in semantic_report
    semantic_map = json.loads((folder / "BRAIN_DIFF_SEMANTIC_MAP.json").read_text(encoding="utf-8"))
    assert set(semantic_map["required_semantic_answers"]) == set(SEMANTIC_QUESTION_IDS)
    with zipfile.ZipFile(result["package_path"]) as archive:
        assert archive.testzip() is None
        assert set(HANDOFF_FILES) <= set(archive.namelist())
        assert any(name.startswith("CURRENT_BRAIN_PACKAGE/project/") for name in archive.namelist())
        assert not [name for name in archive.namelist() if name.lower().endswith(".zip")]
    reopened = validate_codex_handoff_package(result["package_path"])
    assert reopened["status"] == "PASS"
    assert reopened["direct_files"] == sorted(HANDOFF_FILES)
    assert reopened["nested_zip_count"] == 0
    assert reopened["allowed_locked_read_nested_members"] == []
    manifest = json.loads((folder / "HANDOFF_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["governance_authority"] == "ENV15"
    assert manifest["previous_verified_good"]["version_id"] == previous["version_id"]
    assert manifest["current_verified_good"]["version_id"] == current["version_id"]
    assert manifest["current_verified_good"]["snapshot_hash"] == current["snapshot_hash"]
    current_pointer = json.loads((folder / "CURRENT_BRAIN_POINTER.json").read_text(encoding="utf-8"))
    assert current_pointer["version_id"] == manifest["current_verified_good"]["version_id"]
    assert current_pointer["snapshot_hash"] == manifest["current_verified_good"]["snapshot_hash"]
    assert current_pointer["package_hash"] == manifest["current_brain_package_hash"]
    previous_pointer = json.loads((folder / "PREVIOUS_BRAIN_POINTER.json").read_text(encoding="utf-8"))
    assert previous_pointer["snapshot_id"] == manifest["previous_verified_good"]["snapshot_id"]
    assert previous_pointer["version_id"] == previous["version_id"]
    assert previous_pointer["snapshot_hash"] == previous["snapshot_hash"]

    database = Path(current["brain_package_path"]).parents[1] / "brain_versions" / "semantic_brain_diff.sqlite"
    connection = sqlite3.connect(database)
    try:
        expected_tables = {
            "brain_good_snapshot", "brain_semantic_diff_run", "brain_changed_file",
            "brain_route_change", "brain_dependency_change", "brain_symbol_change",
            "brain_test_build_result", "brain_feature_pill_status_change",
            "brain_planned_task_status_change", "brain_unimplemented_item",
            "brain_backend_sector_change", "brain_ui_component_change",
            "brain_package_contract_change", "brain_codex_handoff",
            "brain_code_good_snapshot", "brain_workflow_relationship",
            "brain_semantic_memory_version", "brain_semantic_answer",
        }
        actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        assert expected_tables <= actual
        assert connection.execute("SELECT COUNT(*) FROM brain_good_snapshot").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM brain_semantic_diff_run").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM brain_changed_file").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM brain_codex_handoff").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM brain_code_good_snapshot").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM brain_workflow_relationship").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM brain_semantic_memory_version").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM brain_semantic_answer").fetchone()[0] == 12
        assert connection.execute(
            "SELECT COUNT(*) FROM brain_test_build_result WHERE result_kind='package_validation' AND status='PASS'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    assert previous["version_id"] != current["version_id"]

    reused = create_codex_brain_handoff(workspace, brain_name)
    assert reused["handoff_id"] == result["handoff_id"]
    assert reused["package_path"] == result["package_path"]
    assert reused["incremental_reuse"] == {
        "reused": True,
        "reason": "VERIFIED_SNAPSHOT_PAIR_UNCHANGED",
    }

    valid_archive = Path(result["package_path"])
    missing_archive = tmp_path / "missing.zip"
    _mutate_zip(valid_archive, missing_archive, {"CODEX_NEXT_SESSION_PROMPT.md": None})
    assert any(
        error.startswith("HANDOFF_DIRECT_FILES_MISSING:")
        for error in validate_codex_handoff_package(missing_archive)["errors"]
    )

    with zipfile.ZipFile(valid_archive) as archive:
        stale_manifest = json.loads(archive.read("HANDOFF_MANIFEST.json").decode("utf-8"))
        stale_manifest["current_brain_package"] = str(tmp_path / "gone" / "brain.zip")
        current_pointer_payload = json.loads(archive.read("CURRENT_BRAIN_POINTER.json").decode("utf-8"))
    stale_archive = tmp_path / "stale.zip"
    _mutate_zip(
        valid_archive,
        stale_archive,
        {"HANDOFF_MANIFEST.json": (json.dumps(stale_manifest, sort_keys=True) + "\n").encode()},
    )
    assert "HANDOFF_STALE_CURRENT_PACKAGE_PATH" in validate_codex_handoff_package(stale_archive)["errors"]

    current_pointer_payload["version_id"] = "version_wrong"
    mismatch_archive = tmp_path / "mismatch.zip"
    _mutate_zip(
        valid_archive,
        mismatch_archive,
        {"CURRENT_BRAIN_POINTER.json": (json.dumps(current_pointer_payload, sort_keys=True) + "\n").encode()},
    )
    assert "HANDOFF_CURRENT_POINTER_MISMATCH:version_id" in validate_codex_handoff_package(mismatch_archive)["errors"]

    hash_archive = tmp_path / "hash-mismatch.zip"
    _mutate_zip(valid_archive, hash_archive, {"BRAIN_DIFF_SUMMARY.md": b"tampered\n"})
    assert "HANDOFF_MANIFEST_HASH_MISMATCH:BRAIN_DIFF_SUMMARY.md" in validate_codex_handoff_package(hash_archive)["errors"]

    nested_archive = tmp_path / "nested.zip"
    _mutate_zip(valid_archive, nested_archive, {}, {"unrelated.zip": b"PK\x03\x04nested"})
    assert any(
        error.startswith("HANDOFF_NESTED_ZIP_FORBIDDEN:")
        for error in validate_codex_handoff_package(nested_archive)["errors"]
    )

    gemini_root = Path(current["brain_package_path"]).parent
    gemini = next(
        iter(
            sorted(
                {
                    *gemini_root.glob("Gemini_*_Sqlite_brain.zip"),
                    *gemini_root.glob("Gemini_*_Readable_Project.zip"),
                }
            )
        )
    )
    held = gemini.with_suffix(".held")
    gemini.rename(held)
    try:
        with pytest.raises(Exception, match="MARK_GOOD_GEMINI_PACKAGE_REQUIRED"):
            mark_current_passing_build_good(
                workspace, brain_name, test_status="PASS", build_status="PASS", reason="must fail without Gemini"
            )
    finally:
        held.rename(gemini)
