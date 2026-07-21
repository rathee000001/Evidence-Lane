from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlite_brain_builder.brain_versions import (
    BrainVersionError,
    capture_brain_version,
    list_brain_versions,
    rollback_brain_version,
)
from sqlite_brain_builder.codex_env15_package import (
    create_codex_env15_package,
    validate_codex_env15_zip,
)
from sqlite_brain_builder.runtime.package_validation import (
    validate_chatgpt_package,
    validate_gemini_exact10,
)
from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir
from sqlite_brain_builder.runtime.provider_package_projection import remove_provider_stage
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    export_gemini_exact10,
    export_one_upload_package,
    finalize_project_pointers_and_hashes,
    generate_project_mmd,
    render_topology,
)
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


class RollbackProductError(BrainVersionError):
    pass


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _unique_restored_name(workspace: Path, brain_name: str, version_id: str) -> str:
    version_tail = "".join(char for char in version_id if char.isalnum())[-12:] or "snapshot"
    base = f"{brain_name} Restored {version_tail} {_utc_stamp()}"
    candidate = base
    ordinal = 1
    while brain_output_dir(workspace, candidate).exists():
        ordinal += 1
        candidate = f"{base} {ordinal}"
    return candidate


def _validate_render_result(result: dict[str, Any]) -> None:
    rendered = result.get("rendered") or []
    if not rendered:
        raise RollbackProductError("ROLLBACK_RENDER_EMPTY")
    expected_status = {
        "env_mmd.mmd": "SQLITE_DERIVED_ENV_MMD_TO_SVG_TO_PNG_PASS",
        "uop_mmd.mmd": "SQLITE_DERIVED_UOP_MMD_TO_SVG_TO_PNG_PASS",
        "project_master_topology.mmd": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS",
    }
    failures = [
        item
        for item in rendered
        if item.get("status") != expected_status.get(Path(str(item.get("mmd") or "")).name)
        or not item.get("svg")
        or not item.get("png")
        or not Path(str(item["svg"])).is_file()
        or not Path(str(item["png"])).is_file()
    ]
    observed = {Path(str(item.get("mmd") or "")).name for item in rendered}
    missing = sorted(set(expected_status) - observed)
    if missing:
        failures.append({"missing_render_entries": missing})
    if failures:
        raise RollbackProductError("ROLLBACK_RENDER_VALIDATION_FAILED:" + json.dumps(failures, sort_keys=True))


def _preservation_fingerprint(brain_root: Path) -> str:
    """Hash the current brain and immutable history, excluding live telemetry."""
    digest = hashlib.sha256()
    for path in sorted(brain_root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(brain_root)
        if relative.parts[:2] == ("project", "runtime") or path.name.endswith(("-wal", "-shm")):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\n")
    return digest.hexdigest()


def _rollback_package_governance(
    brain_name: str,
    source_brain_name: str,
    source_version_id: str,
    upstream_package_chain: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create the explicit local governance carried by a rollback Codex package."""

    run_identity = hashlib.sha256(
        f"{brain_name}|{source_brain_name}|{source_version_id}".encode("utf-8")
    ).hexdigest()[:24]
    return (
        {
            "parent_goal_id": f"EVIDENCEOS_ROLLBACK:{source_brain_name}",
            "active_run_id": f"rollback-{run_identity}",
            "current_task_pointer": "ROLLBACK_COMPLETE_PRODUCT_VALIDATED_PASS",
            "active_lane_id": "brain_version_control",
            "brain_name": brain_name,
            "source_brain_name": source_brain_name,
            "source_version_id": source_version_id,
        },
        {
            "delta_id": f"ROLLBACK_PRODUCT_DELTA_{run_identity}",
            "delta_title": "Complete rollback product reconstruction",
            "status": "OPEN_REGISTERED",
            "prompt_pointer": "brain.rollback",
            "insertion_reason": "RESTORE_VERIFIED_VERSION_AS_NEW_IMMUTABLE_BRAIN",
            "insertion_point": "package_hash_validation",
            "upstream_package_chain": upstream_package_chain,
            "brain_diff_transport": "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE",
            "package_level": "CODEX_HIGHER_THAN_CHATGPT_AND_GEMINI",
        },
    )


def _provider_chain_entry(
    result: dict[str, Any],
    validation: dict[str, Any],
    *,
    archive_key: str,
    role: str,
    source_chatgpt_sha256: str = "",
) -> dict[str, Any]:
    skipped = bool(result.get("skipped"))
    return {
        "status": "SKIPPED_PROJECT_CORPUS_TOO_LARGE" if skipped else "PASS",
        "path": str(result.get(archive_key) or ""),
        "sha256": str(result.get("sha256") or validation.get("sha256") or ""),
        "role": role,
        "source_chatgpt_package_sha256": source_chatgpt_sha256,
        "skip_marker": str(result.get("skip_marker") or ""),
        "pipeline_failure": bool(result.get("pipeline_failure", False)),
    }


def rollback_brain_as_complete_product(
    workspace_dir: str | Path,
    brain_name: str,
    version_id: str,
    *,
    actor_name: str = "Evidence OS SQLite Builder",
    reason: str = "restore immutable brain version as complete new brain",
    restored_brain_name: str | None = None,
) -> dict[str, Any]:
    """Restore, rebuild, validate, version, and register a historical brain.

    Registration is deliberately last.  A failed render/export/validation never
    creates a usable-looking workspace entry, and the source brain plus its
    immutable history are never modified.
    """

    workspace = normalize_workspace_dir(workspace_dir)
    init_workspace(workspace)
    actor_name = str(actor_name or "Evidence OS SQLite Builder").strip()
    reason = str(reason or "restore immutable brain version as complete new brain").strip()
    new_brain_name = restored_brain_name or _unique_restored_name(workspace, brain_name, version_id)
    restored_root = brain_output_dir(workspace, new_brain_name)
    if restored_root.exists():
        raise RollbackProductError(f"ROLLBACK_OUTPUT_ALREADY_EXISTS:{restored_root}")

    source_root = brain_output_dir(workspace, brain_name)
    source_versions_before = list_brain_versions(workspace, brain_name, verify_hashes=True)
    selected = next(
        (version for version in source_versions_before["versions"] if version["version_id"] == version_id),
        None,
    )
    if selected is None or selected.get("integrity_status") != "VERIFIED":
        raise RollbackProductError(f"ROLLBACK_SOURCE_VERSION_NOT_VERIFIED:{version_id}")
    source_fingerprint_before = _preservation_fingerprint(source_root)
    source_manifest_hash_before = source_versions_before["manifest_sha256"]

    registered = False
    try:
        restore = rollback_brain_version(
            workspace,
            brain_name,
            version_id,
            actor_name=actor_name,
            reason=reason,
            destination_brain_name=new_brain_name,
        )
        finalize_project_pointers_and_hashes(restored_root)
        mmd = generate_project_mmd(str(workspace), new_brain_name)
        render = render_topology(str(workspace), new_brain_name, generate_mmd=False)
        _validate_render_result(render)

        chatgpt = export_one_upload_package(str(workspace), new_brain_name)
        chatgpt_skipped = bool(chatgpt.get("skipped"))
        if chatgpt_skipped:
            chatgpt_validation = dict(chatgpt.get("validation") or {})
            if (
                chatgpt_validation.get("status") != "SKIPPED"
                or not chatgpt.get("codex_package_required")
                or chatgpt.get("pipeline_failure") is not False
            ):
                raise RollbackProductError("ROLLBACK_CHATGPT_SKIP_CONTRACT_INVALID")
        else:
            chatgpt_validation = validate_chatgpt_package(chatgpt["package_zip"])
            if chatgpt_validation.get("status") != "PASS":
                raise RollbackProductError("ROLLBACK_CHATGPT_PACKAGE_VALIDATION_FAILED")

        if chatgpt_skipped:
            gemini = dict(chatgpt.get("downstream_gemini_skip") or {})
            gemini.update(
                {
                    "gemini_package_zip": "",
                    "sha256": "",
                    "source_chatgpt_package_sha256": "",
                }
            )
        else:
            gemini = export_gemini_exact10(
                str(workspace),
                new_brain_name,
                chatgpt_package=chatgpt["package_zip"],
            )
        gemini_skipped = bool(gemini.get("skipped"))
        if gemini_skipped:
            gemini_validation = dict(gemini.get("validation") or {})
            if (
                gemini_validation.get("status") != "SKIPPED"
                or not gemini.get("codex_package_required")
                or gemini.get("pipeline_failure") is not False
            ):
                raise RollbackProductError("ROLLBACK_GEMINI_SKIP_CONTRACT_INVALID")
        else:
            gemini_validation = validate_gemini_exact10(gemini["gemini_package_zip"])
            if gemini_validation.get("status") != "PASS":
                raise RollbackProductError("ROLLBACK_GEMINI_PACKAGE_VALIDATION_FAILED")
            expected_parent_sha256 = str(chatgpt.get("sha256") or chatgpt_validation.get("sha256") or "")
            observed_parent_sha256 = str(
                gemini.get("source_chatgpt_package_sha256")
                or gemini_validation.get("source_chatgpt_package_sha256")
                or ""
            )
            if not expected_parent_sha256 or observed_parent_sha256 != expected_parent_sha256:
                raise RollbackProductError("ROLLBACK_GEMINI_CHATGPT_DERIVATION_HASH_MISMATCH")

        upstream_package_chain = {
            "chatgpt_local_ai": _provider_chain_entry(
                chatgpt,
                chatgpt_validation,
                archive_key="package_zip",
                role="SHARED_CHATGPT_PUBLIC_AND_HEADLESS_LOCAL_AI_PACKAGE",
            ),
            "gemini": _provider_chain_entry(
                gemini,
                gemini_validation,
                archive_key="gemini_package_zip",
                role="PROVIDER_READABLE_NORMAL_DERIVED_FROM_CHATGPT_LOCAL_AI",
                source_chatgpt_sha256=str(gemini.get("source_chatgpt_package_sha256") or ""),
            ),
        }
        goal_pointer, delta_ledger = _rollback_package_governance(
            new_brain_name,
            brain_name,
            version_id,
            upstream_package_chain,
        )
        codex = create_codex_env15_package(
            restored_root,
            workspace / "portable_brain_workspaces" / new_brain_name,
            restored_root / "packages",
            brain_name=new_brain_name,
            goal_pointer=goal_pointer,
            delta_ledger=delta_ledger,
            latest_good_snapshot={
                "status": "VERIFIED",
                "rollback_available": True,
                "version_id": version_id,
                "snapshot_hash": selected.get("snapshot_hash"),
                "record_sha256": selected.get("record_sha256"),
                "timestamp_utc": selected.get("timestamp_utc"),
            },
            chatgpt_package=(chatgpt.get("package_zip") or None),
            gemini_package=(gemini.get("gemini_package_zip") or None),
        )
        codex_validation = validate_codex_env15_zip(codex["archive"])
        if codex_validation.get("status") != "PASS":
            raise RollbackProductError("ROLLBACK_CODEX_PACKAGE_VALIDATION_FAILED")

        captured = capture_brain_version(
            workspace,
            new_brain_name,
            actor_type="app",
            actor_name=actor_name,
            reason=reason,
            change_summary=f"Restored {brain_name} from immutable {version_id}",
        )

        source_versions_after = list_brain_versions(workspace, brain_name, verify_hashes=True)
        source_fingerprint_after = _preservation_fingerprint(source_root)
        if source_versions_after["manifest_sha256"] != source_manifest_hash_before:
            raise RollbackProductError("ROLLBACK_SOURCE_HISTORY_CHANGED")
        if source_fingerprint_after != source_fingerprint_before:
            raise RollbackProductError("ROLLBACK_CURRENT_BRAIN_CHANGED")

        registration = create_brain(workspace, new_brain_name)
        registered = True

        product_receipt = {
            "status": "PASS",
            "source_brain_name": brain_name,
            "source_version_id": version_id,
            "restored_brain_name": new_brain_name,
            "restored_output": str(restored_root),
            "restore_receipt": restore["receipt_path"],
            "mmd_files": mmd.get("mmd_files") or [],
            "rendered": render.get("rendered") or [],
            "chatgpt_package": str(chatgpt.get("package_zip") or ""),
            "chatgpt_skip_marker": str(chatgpt.get("skip_marker") or ""),
            "chatgpt_validation": chatgpt_validation,
            "gemini_package": str(gemini.get("gemini_package_zip") or ""),
            "gemini_skip_marker": str(gemini.get("skip_marker") or ""),
            "gemini_validation": gemini_validation,
            "provider_package_chain": upstream_package_chain,
            "codex_package": codex["archive"],
            "codex_validation": codex_validation,
            "captured_version": captured,
            "registration": registration,
            "current_brain_unchanged": True,
            "source_history_unchanged": True,
            "source_version_integrity": selected["integrity_status"],
            "source_manifest_sha256_before": source_manifest_hash_before,
            "source_manifest_sha256_after": source_versions_after["manifest_sha256"],
            "source_brain_fingerprint_before": source_fingerprint_before,
            "source_brain_fingerprint_after": source_fingerprint_after,
        }
        receipt_path = restored_root / "receipts" / "ROLLBACK_PRODUCT_RECEIPT.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(product_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        product_receipt["receipt_path"] = str(receipt_path)
        return product_receipt
    except Exception:
        if not registered and restored_root.exists() and restored_root.parent.resolve() == workspace.resolve():
            remove_provider_stage(restored_root)
        raise
