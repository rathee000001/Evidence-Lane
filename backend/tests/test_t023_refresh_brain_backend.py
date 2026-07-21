from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.brain_versions import (
    BrainVersionError,
    capture_brain_version,
    compare_current_brain_to_version,
    current_brain_snapshot,
    list_brain_versions,
    materialize_brain_version_candidate,
)
from sqlite_brain_builder.refresh_brain import (
    drop_refresh_candidate,
    fuse_refresh_candidate,
    get_refresh_output,
    record_refresh_hil_decision,
    start_refresh_brain,
)
from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.project_delta_ledger import (
    get_project_delta,
    list_project_deltas,
    record_refresh_delta,
)
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain


def _source(source_path: Path) -> dict[str, object]:
    return {
        "source_id": "source_artifact_fixture",
        "lane_key": "artifacts",
        "lane_label": "Artifacts",
        "source_type": "Artifact",
        "display_name": source_path.name,
        "path": str(source_path),
        "text": "",
        "active": True,
        "status": "registered",
        "metadata": {},
    }


def _versioned_brain(tmp_path: Path) -> tuple[str, Path, dict[str, object], dict[str, object]]:
    brain_name = "Refresh Fixture"
    source_path = tmp_path / "fixture.txt"
    source_path.write_text("baseline source\n", encoding="utf-8")
    source = _source(source_path)
    build_brain(str(tmp_path), brain_name, [source], generate_mmd=False)
    version = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS test builder",
        reason="validated fixture baseline",
    )
    return brain_name, source_path, source, version


def test_version_candidate_materialization_and_current_comparison_are_non_destructive(tmp_path: Path) -> None:
    brain_name, source_path, _source_record, version = _versioned_brain(tmp_path)
    canonical = brain_output_dir(tmp_path, brain_name)
    candidate = tmp_path / ".evidenceos_refresh_candidates" / "candidate-1" / canonical.name

    materialized = materialize_brain_version_candidate(
        tmp_path,
        brain_name,
        str(version["version_id"]),
        candidate,
    )

    assert materialized["current_brain_unchanged"] is True
    assert materialized["candidate_snapshot_hash"] == version["snapshot_hash"]
    assert candidate.is_dir()
    assert current_brain_snapshot(tmp_path, brain_name)["snapshot_hash"] == version["snapshot_hash"]
    assert compare_current_brain_to_version(tmp_path, brain_name, str(version["version_id"]))["status"] == "MATCH"

    source_path.write_text("external source changed but brain remains immutable\n", encoding="utf-8")
    assert compare_current_brain_to_version(tmp_path, brain_name, str(version["version_id"]))["status"] == "MATCH"

    (canonical / "project" / "PROJECT_README.md").write_text("tampered\n", encoding="utf-8")
    comparison = compare_current_brain_to_version(tmp_path, brain_name, str(version["version_id"]))
    assert comparison["status"] == "MISMATCH"
    assert comparison["changed_files"]

    with pytest.raises(BrainVersionError, match="CANDIDATE_DESTINATION_MUST_NOT_BE_CURRENT_BRAIN"):
        materialize_brain_version_candidate(tmp_path, brain_name, str(version["version_id"]), canonical)


def test_provider_specific_refresh_import_is_removed_from_active_authority() -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    assert not hasattr(refresh_brain, "PublicModelReturnError")
    assert not hasattr(refresh_brain, "validate_public_model_return_package")
    assert not hasattr(refresh_brain, "import_public_model_return")
    with pytest.raises(ipc_worker.WorkerError, match="UNKNOWN_COMMAND: brain.refresh.import"):
        ipc_worker.handle({"command": "brain.refresh.import", "payload": {}})


def test_refresh_no_change_reuses_verified_state_without_candidate(tmp_path: Path) -> None:
    brain_name, _source_path, source, version = _versioned_brain(tmp_path)

    result = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )

    assert result["status"] == "NO_CHANGE"
    assert result["classification"] == "NO_CHANGE"
    assert result["candidate_created"] is False
    assert result["new_immutable_snapshot"] == 0
    assert result["automatic_fusion"] is False
    assert result["project_delta"] == {
        "status": "REFRESH_NO_CHANGE",
        "created": False,
        "overlay_state": "REFRESH_NO_CHANGE",
        "color_mutation": False,
        "accepted_snapshot_preserved": True,
        "operation_receipt": result["operation_receipt"],
        "operation_receipt_sha256": result["operation_receipt_sha256"],
    }
    receipt = json.loads(Path(result["operation_receipt"]).read_text(encoding="utf-8"))
    assert receipt["refresh_no_change"] is True
    assert receipt["project_delta_created"] is False
    assert receipt["overlay_state"] == "REFRESH_NO_CHANGE"
    assert list_project_deltas(brain_output_dir(tmp_path, brain_name))["entry_count"] == 0
    assert current_brain_snapshot(tmp_path, brain_name)["snapshot_hash"] == version["snapshot_hash"]
    assert list_brain_versions(tmp_path, brain_name)["version_count"] == 1


def test_repeated_no_change_refresh_creates_receipts_without_fake_project_deltas(tmp_path: Path) -> None:
    brain_name, _source_path, source, version = _versioned_brain(tmp_path)

    first = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )
    second = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )

    assert first["project_delta"]["status"] == "REFRESH_NO_CHANGE"
    assert second["project_delta"]["status"] == "REFRESH_NO_CHANGE"
    assert first["operation_receipt"] != second["operation_receipt"]
    ledger = list_project_deltas(brain_output_dir(tmp_path, brain_name))
    assert ledger["entry_count"] == 0
    assert current_brain_snapshot(tmp_path, brain_name)["snapshot_hash"] == version["snapshot_hash"]
    ipc_list = ipc_worker.handle(
        {
            "command": "brain.delta.list",
            "payload": {"workspace_dir": str(tmp_path), "brain_name": brain_name},
        }
    )
    assert ipc_list["entry_count"] == 0


def test_refresh_delta_materializes_separate_code_and_non_code_manifests_for_future_overlay(
    tmp_path: Path,
) -> None:
    brain_root = tmp_path / "dual-lane-brain"
    (brain_root / "project").mkdir(parents=True)

    recorded = record_refresh_delta(
        brain_root,
        brain_id="brain-dual-lane-fixture",
        brain_name="Dual Lane Brain",
        candidate_id="refresh-dual-lane-fixture",
        refresh_status="AWAITING_FUSE",
        classification="USER_MANUAL_CHANGE",
        before_snapshot_hash="A" * 64,
        after_snapshot_hash="B" * 64,
        refresh_receipt_path="receipts/REFRESH_OPERATION_fixture.json",
        refresh_receipt_sha256="C" * 64,
        source_classifications=[
            {
                "source_id": "source-code",
                "lane_id": "local_code",
                "classification": "CHANGED_REBUILD",
            },
            {
                "source_id": "source-document",
                "lane_id": "documents",
                "classification": "CHANGED_REBUILD",
            },
        ],
        changed_files=[
            {
                "path": "project/sectors/local_code/local_code_sector_v001.sqlite",
                "change_kind": "MODIFIED",
                "previous_sha256": "D" * 64,
                "current_sha256": "E" * 64,
            },
            {
                "path": "project/sectors/documents/documents_sector_v001.sqlite",
                "change_kind": "MODIFIED",
                "previous_sha256": "F" * 64,
                "current_sha256": "0" * 64,
            },
        ],
        refresh_recorded_at="2026-07-16T00:00:00.000000Z",
        delta_type="HASH_SNAPSHOT_REFRESH_DELTA",
        parent_snapshot_id="version-before",
        child_snapshot_id="candidate-after",
        source_state={"source_state": "CHANGE_DETECTED", "git": {"worktree_state": "CONTENT_HASH_SNAPSHOT"}},
        node_changes=[
            {
                "object_id": "file:code-fixture",
                "object_kind": "file",
                "relative_path": "src/app.py",
                "change_kind": "MODIFIED",
                "truth_state": "GREEN",
                "review_required": False,
                "validation_ids": ["validation-code"],
                "evidence": {"direct_or_transitive": "DIRECT"},
            }
        ],
        edge_changes=[
            {
                "object_id": "edge:code-fixture",
                "relation_type": "IMPORTS",
                "from_object_id": "file:code-fixture",
                "to_object_id": "file:dependency-fixture",
                "change_kind": "ADDED",
                "truth_state": "GREEN",
                "review_required": False,
                "validation_ids": ["validation-code"],
                "evidence": {"direct_or_transitive": "PROVEN_DIRECT"},
            }
        ],
        validation_results=[
            {
                "validation_id": "validation-code",
                "validation_type": "PYTHON_SYNTAX",
                "command": "python -c compile",
                "started_at": "2026-07-16T00:00:00Z",
                "ended_at": "2026-07-16T00:00:01Z",
                "exit_code": 0,
                "status": "PASS",
                "affected_files": ["src/app.py"],
                "stdout": "",
                "stderr": "",
                "receipt_path": "receipts/validation.json",
                "receipt_sha256": "1" * 64,
            }
        ],
        relationship_comparison_complete=True,
    )

    delta_root = brain_root / "project" / "deltas"
    manifest = json.loads(
        (delta_root / recorded["manifest_relative_path"]).read_text(encoding="utf-8")
    )
    lane_refs = manifest["dual_lane_delta"]["manifests"]
    code_manifest_path = delta_root / lane_refs["code"]["manifest_relative_path"]
    non_code_manifest_path = delta_root / lane_refs["non_code"]["manifest_relative_path"]
    code_manifest = json.loads(code_manifest_path.read_text(encoding="utf-8"))
    non_code_manifest = json.loads(non_code_manifest_path.read_text(encoding="utf-8"))

    assert code_manifest["partition"]["lane_ids"] == ["local_code"]
    assert code_manifest["source_change_count"] == 1
    assert code_manifest["file_change_count"] == 1
    assert code_manifest["source_changes"][0]["source_id"] == "source-code"
    assert code_manifest["file_changes"][0]["path"].startswith("project/sectors/local_code/")
    assert code_manifest["future_3d_telemetry"]["eligible"] is True
    assert code_manifest["future_3d_telemetry"]["current_sqlite_builder_surface"] == "STORED_NOT_RENDERED"
    assert non_code_manifest["partition"]["lane_ids"] == ["documents"]
    assert non_code_manifest["source_change_count"] == 1
    assert non_code_manifest["file_change_count"] == 1
    assert non_code_manifest["source_changes"][0]["source_id"] == "source-document"
    assert non_code_manifest["future_3d_telemetry"]["eligible"] is False
    assert manifest["telemetry"]["surface_status"] == "REFRESH_DELTA_COMPLETE_OVERLAY_READY"
    assert manifest["telemetry"]["overlay_authority"] == "PROJECT_REFRESH_BUTTON_ONLY"
    assert manifest["telemetry"]["code_overlay_manifest_relative_path"] == lane_refs["code"]["manifest_relative_path"]
    assert manifest["telemetry"]["code_overlay_manifest_sha256"] == lane_refs["code"]["manifest_sha256"]
    assert manifest["artifact_manifest"]["stored_source_payload_files"] == 0
    assert list_project_deltas(brain_root)["entry_count"] == 1


def test_changed_source_builds_isolated_candidate_and_never_auto_fuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    brain_name, source_path, source, version = _versioned_brain(tmp_path)
    canonical = brain_output_dir(tmp_path, brain_name)
    canonical_before = current_brain_snapshot(tmp_path, brain_name)["snapshot_hash"]
    source_path.write_text("changed source for candidate\n", encoding="utf-8")

    def build_without_distribution_products(
        candidate_workspace: Path,
        selected_brain_name: str,
        candidate_sources: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        build = build_brain(
            str(candidate_workspace),
            selected_brain_name,
            candidate_sources,
            generate_mmd=False,
        )
        return {
            "build": build,
            "topology": {"status": "SKIPPED_TEST_FIXTURE"},
            "packages": {"status": "SKIPPED_TEST_FIXTURE"},
            "validations": {"status": "PASS", "mode": "FOCUSED_REFRESH_FIXTURE"},
        }

    recycled: list[str] = []

    def recycle_fixture(path: str | Path) -> None:
        target = Path(path)
        recycled.append(str(target))
        for item in target.rglob("*"):
            if item.is_file():
                os.chmod(item, stat.S_IREAD | stat.S_IWRITE)
        shutil.rmtree(target)

    monkeypatch.setattr(refresh_brain, "_create_candidate_products", build_without_distribution_products)
    monkeypatch.setattr(refresh_brain, "_send_to_recycle_bin", recycle_fixture)

    result = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )

    assert result["status"] == "AWAITING_FUSE"
    assert result["classification"] == "LOCAL_WORKING_PROJECT_CHANGE"
    assert result["candidate_created"] is True
    assert result["candidate_snapshot_hash"] != version["snapshot_hash"]
    assert Path(result["candidate_root"]).is_dir()
    assert result["automatic_fusion"] is False
    assert result["project_delta"]["display_name"] == "Delta 1"
    assert result["verified_brain_unchanged"] is True
    assert current_brain_snapshot(tmp_path, brain_name)["snapshot_hash"] == canonical_before
    assert canonical.is_dir()
    assert list_brain_versions(tmp_path, brain_name)["version_count"] == 1

    output = get_refresh_output(tmp_path, brain_name, candidate_id=str(result["candidate_id"]))
    assert output["status"] == "AWAITING_FUSE"
    assert output["diff"]["changed_files"]

    dropped = drop_refresh_candidate(
        tmp_path,
        brain_name,
        candidate_id=str(result["candidate_id"]),
        actor="test user",
        reason="explicit drop fixture",
    )
    assert dropped["status"] == "DROPPED"
    assert dropped["verified_brain_unchanged"] is True
    assert dropped["project_delta_event"]["event_type"] == "DROPPED"
    assert recycled


def test_source_removal_is_pending_refresh_and_does_not_mutate_selected_brain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = {
        "source_id": "source-1",
        "lane_key": "artifacts",
        "lane_label": "Artifacts",
        "path": str(tmp_path / "source.txt"),
        "active": True,
    }
    calls: list[object] = []
    monkeypatch.setattr(ipc_worker, "_sources_for", lambda *_args: [dict(source)])
    monkeypatch.setattr(ipc_worker, "_save_sources", lambda *_args: calls.append(_args[-1]))
    monkeypatch.setattr(
        ipc_worker,
        "set_env15_source_activity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("verified brain mutation forbidden")),
        raising=False,
    )

    result = ipc_worker.handle({
        "command": "sources.remove",
        "payload": {
            "workspace_dir": str(tmp_path),
            "brain_name": "Refresh Fixture",
            "source_id": "source-1",
        },
    })

    assert result["removed"] == "source-1"
    assert result["activity"]["status"] == "SOURCE_UNREGISTERED_SECTOR_BYTES_PRESERVED"
    assert result["activity"]["verified_brain_mutated"] is False
    assert calls == [[]]


def test_explicit_fuse_creates_new_version_and_preserves_prior_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    brain_name, source_path, source, version = _versioned_brain(tmp_path)
    source_path.write_text("explicitly accepted refresh\n", encoding="utf-8")

    def focused_products(
        candidate_workspace: Path,
        selected_brain_name: str,
        candidate_sources: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "build": build_brain(
                str(candidate_workspace),
                selected_brain_name,
                candidate_sources,
                generate_mmd=False,
            ),
            "topology": {"status": "SKIPPED_TEST_FIXTURE"},
            "packages": {"chatgpt": {"status": "SKIPPED_NO_CODE_LANES"}, "gemini": {"status": "SKIPPED_NO_CODE_LANES"}},
            "validations": {"status": "PASS", "mode": "FOCUSED_REFRESH_FIXTURE"},
        }

    monkeypatch.setattr(refresh_brain, "_create_candidate_products", focused_products)
    candidate = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )
    accepted: list[list[dict[str, object]]] = []
    approved = record_refresh_hil_decision(
        tmp_path,
        brain_name,
        candidate_id=str(candidate["candidate_id"]),
        decision="APPROVE",
        actor="fixture human",
        reviewer_type="TEST_HUMAN_REVIEWER",
        reason="focused candidate is validated and reversible",
    )
    assert approved["hil_state"] == "HIL_APPROVED_FUSE_REQUIRES_EXPLICIT_CLICK"

    fused = fuse_refresh_candidate(
        tmp_path,
        brain_name,
        candidate_id=str(candidate["candidate_id"]),
        actor="fixture human",
        reason="explicit focused acceptance",
        source_commit=lambda sources: accepted.append(sources),
    )

    assert fused["status"] == "FUSED"
    assert fused["immutable_version"]["snapshot_hash"] == candidate["candidate_snapshot_hash"]
    assert fused["immutable_version"]["previous_version_id"] == version["version_id"]
    assert Path(fused["prior_verified_archive"]).is_dir()
    assert list_brain_versions(tmp_path, brain_name, verify_hashes=True)["version_count"] == 2
    assert accepted and accepted[0][0]["source_id"] == source["source_id"]
    assert fused["prior_verified_version_preserved"] is True
    assert fused["project_delta_event"]["event_type"] == "FUSED"
    delta = get_project_delta(
        brain_output_dir(tmp_path, brain_name),
        candidate_id=str(candidate["candidate_id"]),
    )
    assert delta is not None
    assert [event["event_type"] for event in delta["events"]] == [
        "REFRESH_CAPTURED",
        "HIL_APPROVE",
        "FUSED",
    ]


def test_explicit_fuse_resumes_same_validated_candidate_after_windows_rename_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    brain_name, source_path, source, version = _versioned_brain(tmp_path)
    source_path.write_text("accepted refresh survives transient folder handle\n", encoding="utf-8")

    def focused_products(
        candidate_workspace: Path,
        selected_brain_name: str,
        candidate_sources: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "build": build_brain(
                str(candidate_workspace),
                selected_brain_name,
                candidate_sources,
                generate_mmd=False,
            ),
            "topology": {"status": "SKIPPED_TEST_FIXTURE"},
            "packages": {"status": "SKIPPED_TEST_FIXTURE"},
            "validations": {"status": "PASS", "mode": "FOCUSED_REFRESH_FIXTURE"},
        }

    monkeypatch.setattr(refresh_brain, "_create_candidate_products", focused_products)
    candidate = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-rename-retry",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )
    record_refresh_hil_decision(
        tmp_path,
        brain_name,
        candidate_id=str(candidate["candidate_id"]),
        decision="APPROVE",
        actor="fixture human",
        reviewer_type="TEST_HUMAN_REVIEWER",
        reason="approve the validated candidate before the explicit Fuse click",
    )

    real_rename = refresh_brain._rename_directory_with_retry
    first_call = True

    def block_first_rename(source: Path, target: Path, **kwargs: object) -> None:
        nonlocal first_call
        if first_call:
            first_call = False
            raise PermissionError(5, "fixture directory handle is open", str(source), str(target))
        real_rename(source, target, **kwargs)

    monkeypatch.setattr(refresh_brain, "_rename_directory_with_retry", block_first_rename)
    with pytest.raises(PermissionError, match="fixture directory handle is open"):
        fuse_refresh_candidate(
            tmp_path,
            brain_name,
            candidate_id=str(candidate["candidate_id"]),
            actor="fixture human",
            reason="first explicit acceptance",
        )
    failed = refresh_brain.get_refresh_status(
        tmp_path,
        brain_name,
        candidate_id=str(candidate["candidate_id"]),
    )
    assert failed["status"] == "FAILED"
    assert failed["failure_class"] == "FUSION_FILESYSTEM_BLOCKED"
    assert failed["verified_brain_unchanged"] is True
    assert Path(str(failed["candidate_root"])).is_dir()

    monkeypatch.setattr(refresh_brain, "_rename_directory_with_retry", real_rename)
    fused = fuse_refresh_candidate(
        tmp_path,
        brain_name,
        candidate_id=str(candidate["candidate_id"]),
        actor="fixture human",
        reason="resume the same validated candidate",
    )

    assert fused["status"] == "FUSED"
    assert fused["candidate_id"] == candidate["candidate_id"]
    assert fused["fuse_resume_of_failed_attempt"] is True
    assert fused["immutable_version"]["snapshot_hash"] == candidate["candidate_snapshot_hash"]


def test_real_candidate_product_pass_creates_governed_package_without_code_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    brain_name, source_path, source, version = _versioned_brain(tmp_path)
    source_path.write_text("real candidate product pass\n", encoding="utf-8")

    def recycle_fixture(path: str | Path) -> None:
        target = Path(path)
        for item in target.rglob("*"):
            if item.is_file():
                os.chmod(item, stat.S_IREAD | stat.S_IWRITE)
        shutil.rmtree(target)

    monkeypatch.setattr(refresh_brain, "_send_to_recycle_bin", recycle_fixture)
    result = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-refresh-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )

    assert result["status"] == "AWAITING_FUSE"
    assert result["products"]["validations"]["status"] == "PASS"
    assert result["products"]["validations"]["sqlite"]["status"] == "PASS"
    assert result["products"]["validations"]["chatgpt"]["status"] == "PASS"
    assert result["products"]["validations"]["gemini"]["status"] == "PASS"
    assert Path(result["products"]["packages"]["chatgpt"]["package_zip"]).is_file()
    assert Path(result["products"]["packages"]["gemini"]["gemini_package_zip"]).is_file()
    assert (
        result["products"]["validations"]["gemini"]["source_chatgpt_package_sha256"]
        == result["products"]["validations"]["chatgpt"]["sha256"]
    )
    assert result["products"]["packages"]["codex_universal"]["validation"]["status"] == "PASS"

    dropped = drop_refresh_candidate(
        tmp_path,
        brain_name,
        candidate_id=str(result["candidate_id"]),
        actor="fixture user",
        reason="cleanup real product fixture",
    )
    assert dropped["status"] == "DROPPED"


def test_ipc_refresh_routes_are_registered_with_one_writer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert {
        "brain.refresh.start",
        "brain.refresh.cancel",
        "brain.refresh.retry",
        "brain.refresh.hil.decide",
        "brain.refresh.fuse",
        "brain.refresh.drop",
    } <= ipc_worker._MUTATING_COMMANDS
    assert "brain.refresh.import" not in ipc_worker._MUTATING_COMMANDS

    monkeypatch.setattr(ipc_worker, "get_refresh_status", lambda *_args, **_kwargs: {"status": "AWAITING_FUSE"})
    result = ipc_worker.handle({
        "command": "brain.refresh.status",
        "payload": {"workspace_dir": str(tmp_path), "brain_name": "Refresh Fixture"},
    })
    assert result["status"] == "AWAITING_FUSE"
