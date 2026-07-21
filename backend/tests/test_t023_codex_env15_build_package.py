from __future__ import annotations

import json
import hashlib
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.codex_env15_package import (
    CONTRACT_PATHS,
    PACKAGE_FAMILY,
    create_codex_env15_package,
    validate_codex_env15_package,
    validate_codex_env15_zip,
    validate_env15_template,
)
from sqlite_brain_builder.runtime.env15_locked_read import (
    CHATGPT_GEMINI_ARCHIVE_SHA256,
    CODEX_ARCHIVE_SHA256,
    SUPPLIED_CODEX_SUPPORT_MEMBERS,
    SUPPLIED_CODEX_SUPPORT_PROVENANCE,
    supplied_codex_support_rows,
    validate_split_authority_archives,
)


def _source_brain(root: Path) -> Path:
    sector = root / "project" / "sectors" / "docs"
    sector.mkdir(parents=True)
    database = sector / "docs_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES('evidence-1','verified build command source')")
    return root


def _governance() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "parent_goal_id": "T023_SUPREME_SOURCE_RECONCILED_LINEAR_EVIDENCEOS",
            "active_run_id": "build-run-1",
            "current_task_pointer": "BUILD_COMMAND_VALIDATED_PASS",
            "active_lane_id": "build_command",
        },
        {
            "delta_id": "BUILD_COMMAND_DELTA_build-run-1",
            "title": "Build Command operational delta",
            "status": "OPEN_REGISTERED",
            "prompt_pointer": "brain.buildAll",
            "insertion_reason": "SAME_VALIDATED_PASS_CODEX_PACKAGE",
            "insertion_point": "package_hash_validation",
        },
    )


def test_user_supplied_env15_provider_archives_are_distinct_exact_authorities() -> None:
    validation = validate_split_authority_archives()

    assert validation.valid is True
    assert validation.errors == ()
    assert validation.chatgpt_gemini_sha256 == CHATGPT_GEMINI_ARCHIVE_SHA256
    assert validation.codex_sha256 == CODEX_ARCHIVE_SHA256
    assert validation.chatgpt_gemini_entry_count == 128
    assert validation.chatgpt_gemini_file_count == 128
    assert validation.chatgpt_gemini_codex_member_count == 0
    assert validation.codex_entry_count == 161
    assert validation.codex_file_count == 135
    assert validation.codex_directory_count == 26
    assert validation.codex_support_member_count == 6
    assert validation.common_member_count == 128
    assert validation.common_member_hashes_valid is True
    assert validation.codex_non_support_extra_files == (
        "research/v15 research on temp.md",
    )


def test_bundled_env15_authority_is_hash_backed_and_sqlite_clean() -> None:
    validation = validate_env15_template()

    assert validation["status"] == "PASS"
    assert validation["package_class"] == "UEPC_ENV15_PUBLIC_FULL_LOCKED_RUNTIME"
    assert validation["file_count"] == 100
    assert validation["declared_file_count"] == 95
    assert validation["sqlite_count"] == 18
    assert validation["errors"] == []
    assert len(validation["template_tree_hash"]) == 64


def test_universal_package_is_env15_derived_two_layer_hash_validated_and_reusable(tmp_path: Path) -> None:
    brain = _source_brain(tmp_path / "brain")
    package = tmp_path / "portable" / "brain"
    archives = tmp_path / "archives"
    goal_pointer, delta_ledger = _governance()

    first = create_codex_env15_package(
        brain,
        package,
        archives,
        brain_name="Package Brain",
        goal_pointer=goal_pointer,
        delta_ledger=delta_ledger,
        latest_good_snapshot={"status": "VERIFIED", "snapshot_hash": "A" * 64},
    )

    assert first["status"] == "PASS"
    assert first["package_family"] == PACKAGE_FAMILY
    assert first["archive_reused"] is False
    assert validate_codex_env15_package(package)["status"] == "PASS"
    assert validate_codex_env15_zip(first["archive"])["status"] == "PASS"
    assert (package / "env" / "env_sqlite.sqlite").is_file()
    assert (package / "uop" / "uop_sqlite.sqlite").is_file()
    assert (package / "project" / "project_router.sqlite").is_file()
    assert (package / "brain_snapshot.sqlite").is_file()
    assert (package / "codex" / "codex_runtime_ledger.sqlite").is_file()
    assert all((package / path).is_file() for path in CONTRACT_PATHS)
    assert all((package / path).is_file() for path in SUPPLIED_CODEX_SUPPORT_MEMBERS)
    for member, expected_size, expected_hash in supplied_codex_support_rows():
        payload = (package / member).read_bytes()
        assert len(payload) == expected_size
        assert hashlib.sha256(payload).hexdigest().upper() == expected_hash
    support_provenance = json.loads(
        (package / SUPPLIED_CODEX_SUPPORT_PROVENANCE).read_text(encoding="utf-8")
    )
    assert support_provenance["provider_scope"] == "CODEX_ONLY"
    assert support_provenance["support_member_count"] == 6
    assert support_provenance["source_archive_sha256"] == CODEX_ARCHIVE_SHA256
    assert support_provenance["non_support_source_evidence_imported"] is False

    package_contract = json.loads((package / "manifests" / "UNIVERSAL_EXECUTION_PACKAGE_CONTRACT.json").read_text(encoding="utf-8"))
    hil_contract = json.loads((package / "codex" / "HIL_CONTRACT.json").read_text(encoding="utf-8"))
    mutation_scope = json.loads((package / "codex" / "ALLOWED_MUTATION_SCOPE.json").read_text(encoding="utf-8"))
    adapters = json.loads((package / "codex" / "ADAPTER_METADATA.json").read_text(encoding="utf-8"))
    assert package_contract["package_family"] == PACKAGE_FAMILY
    assert set(package_contract["layers"]) == {"READ_ONLY_PROJECT_BRAIN_SNAPSHOT", "WRITABLE_OPERATIONAL_STATE_TRAVEL_LEDGER"}
    assert set(package_contract["adapters"]) == {"CODEX"}
    assert package_contract["brain_diff_transport"] == "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE"
    assert package_contract["headless_local_ai_package_family"] == "CHATGPT_LOCAL_AI"
    assert hil_contract["canonical_fusion_requires_human_decision"] is True
    assert mutation_scope["verified_brain_overwrite_allowed"] is False
    assert adapters["brain_diff_transport"] == "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE"
    assert adapters["headless_local_ai_package_family"] == "CHATGPT_LOCAL_AI"
    assert adapters["adapters"]["CODEX"]["delta_source"] == "codex/ACTIVE_DELTA_LEDGER.json"
    assert "LOCAL_AI" not in adapters["adapters"]
    with zipfile.ZipFile(first["archive"], "r") as archive:
        assert not [name for name in archive.namelist() if name.casefold().endswith(".zip")]
        assert set(SUPPLIED_CODEX_SUPPORT_MEMBERS) <= set(archive.namelist())

    second = create_codex_env15_package(
        brain,
        package,
        archives,
        brain_name="Package Brain",
        goal_pointer=goal_pointer,
        delta_ledger=delta_ledger,
        latest_good_snapshot={"status": "VERIFIED", "snapshot_hash": "A" * 64},
    )
    assert second["snapshot"]["snapshot_reused"] is True
    assert second["archive_reused"] is True
    assert second["archive"] == first["archive"]
    assert second["single_canonical_archive"] is True
    assert list(archives.glob(f"{PACKAGE_FAMILY}_*.zip")) == [Path(first["archive"])]


def test_env15_derivation_fails_closed_on_existing_template_conflict(tmp_path: Path) -> None:
    brain = _source_brain(tmp_path / "brain")
    package = tmp_path / "portable" / "brain"
    package.mkdir(parents=True)
    (package / ".uepc_env").write_text("external mutation", encoding="utf-8")
    goal_pointer, delta_ledger = _governance()

    with pytest.raises(RuntimeError, match="ENV15_DERIVATION_CONFLICT"):
        create_codex_env15_package(
            brain,
            package,
            tmp_path / "archives",
            brain_name="Conflict Brain",
            goal_pointer=goal_pointer,
            delta_ledger=delta_ledger,
        )


def test_build_all_calls_universal_codex_package_in_the_same_validated_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.md"
    source.write_text("# source", encoding="utf-8")
    brain_root = tmp_path / "brain-output"
    _source_brain(brain_root)
    call_order: list[str] = []

    monkeypatch.setattr(ipc_worker, "build_brain", lambda *_args, **_kwargs: {"brain_root": str(brain_root), "incremental": {"all_sources_unchanged": False}})
    monkeypatch.setattr(ipc_worker, "generate_project_mmd", lambda *_args, **_kwargs: {"mmd_files": []})
    monkeypatch.setattr(ipc_worker, "render_topology", lambda *_args, **_kwargs: {"rendered": []})
    monkeypatch.setattr(ipc_worker, "export_one_upload_package", lambda *_args, **_kwargs: call_order.append("chatgpt") or {"validation": {"status": "PASS", "errors": []}})
    monkeypatch.setattr(ipc_worker, "export_gemini_exact10", lambda *_args, **_kwargs: call_order.append("gemini") or {"validation": {"status": "PASS", "errors": []}})
    monkeypatch.setattr(
        ipc_worker,
        "create_codex_env15_package",
        lambda *_args, **_kwargs: call_order.append("codex") or {"status": "PASS", "archive": str(tmp_path / "codex.zip"), "validation": {"status": "PASS", "errors": []}, "archive_validation": {"status": "PASS", "errors": []}},
    )
    monkeypatch.setattr(
        ipc_worker,
        "capture_brain_version",
        lambda *_args, **kwargs: call_order.append("version") or {"status": "PASS", "reason": kwargs["reason"]},
    )

    result = ipc_worker.handle(
        {
            "id": "same-pass-build",
            "command": "brain.buildAll",
            "payload": {
                "workspace_dir": str(tmp_path / "workspace"),
                "brain_name": "Same Pass Brain",
                "sources": [{"source_id": "source-docs", "lane_key": "docs", "path": str(source)}],
            },
        }
    )

    assert call_order == ["chatgpt", "gemini", "codex", "version"]
    assert result["codex_universal_execution_package"]["status"] == "PASS"
    assert result["package_validation"]["codex_universal"]["status"] == "PASS"
    assert result["version"]["reason"] == "brain.buildAll successful"


def test_failed_universal_package_prevents_version_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    brain_root = _source_brain(tmp_path / "brain-output")
    monkeypatch.setattr(ipc_worker, "build_brain", lambda *_args, **_kwargs: {"brain_root": str(brain_root), "incremental": {"all_sources_unchanged": False}})
    monkeypatch.setattr(ipc_worker, "generate_project_mmd", lambda *_args, **_kwargs: {"mmd_files": []})
    monkeypatch.setattr(ipc_worker, "render_topology", lambda *_args, **_kwargs: {"rendered": []})
    monkeypatch.setattr(ipc_worker, "export_one_upload_package", lambda *_args, **_kwargs: {"validation": {"status": "PASS", "errors": []}})
    monkeypatch.setattr(ipc_worker, "export_gemini_exact10", lambda *_args, **_kwargs: {"validation": {"status": "PASS", "errors": []}})
    monkeypatch.setattr(ipc_worker, "create_codex_env15_package", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("CODEX_PACKAGE_INVALID")))

    with pytest.raises(RuntimeError, match="CODEX_PACKAGE_INVALID"):
        ipc_worker.handle(
            {
                "id": "failed-codex-package",
                "command": "brain.buildAll",
                "payload": {"workspace_dir": str(tmp_path / "workspace"), "brain_name": "Failed Codex Brain", "sources": [{"source_id": "docs", "lane_key": "docs", "path": str(source)}]},
            }
        )

    assert not (Path(tmp_path / "workspace") / "failed_codex_brain_output" / "brain_versions").exists()
