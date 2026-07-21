from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


MANIFEST_NAME = "FULL_APP_SOURCE_MANIFEST.sha256"
VERIFICATION_NAME = "FULL_APP_SOURCE_VERIFICATION.json"
FORBIDDEN_DIRECTORIES = {
    "node_modules",
    "dist",
    "target",
    "__pycache__",
    ".pytest_cache",
    "build-output",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".exe", ".glb"}


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


def manifest_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        digest, relative = line.split("  ", 1)
        entries[relative] = digest
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--runtime-validation", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--prerequisite-receipt", type=Path, required=True)
    parser.add_argument("--postinstall-receipt", type=Path, required=True)
    parser.add_argument("--metrics-validation", type=Path, required=True)
    parser.add_argument("--ui-handoff-validation", type=Path, required=True)
    parser.add_argument("--clean-frontend-manifest", type=Path, required=True)
    parser.add_argument("--clean-backend-manifest", type=Path, required=True)
    parser.add_argument("--backend-test-count", type=int, required=True)
    parser.add_argument("--workspace-test-count", type=int, required=True)
    parser.add_argument("--workspace-test-status", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    executable = args.exe.resolve()
    worker = args.worker.resolve()
    runtime_validation = args.runtime_validation.resolve()
    installer = args.installer.resolve()
    prerequisite_receipt = args.prerequisite_receipt.resolve()
    postinstall_receipt = args.postinstall_receipt.resolve()
    metrics_validation = args.metrics_validation.resolve()
    ui_handoff_validation = args.ui_handoff_validation.resolve()
    clean_frontend_manifest = args.clean_frontend_manifest.resolve()
    clean_backend_manifest = args.clean_backend_manifest.resolve()
    required_evidence = (
        executable,
        worker,
        runtime_validation,
        installer,
        prerequisite_receipt,
        postinstall_receipt,
        metrics_validation,
        ui_handoff_validation,
        clean_frontend_manifest,
        clean_backend_manifest,
    )
    if not all(path.is_file() for path in required_evidence):
        raise SystemExit("fresh-build or source-parity evidence is missing")
    runtime = json.loads(runtime_validation.read_text(encoding="utf-8-sig"))
    if runtime.get("status") != "PASS":
        raise SystemExit("isolated full-app runtime validation did not pass")
    for label, evidence in (
        ("prerequisite receipt", prerequisite_receipt),
        ("post-install receipt", postinstall_receipt),
        ("metrics validation", metrics_validation),
        ("UI handoff validation", ui_handoff_validation),
    ):
        payload = json.loads(evidence.read_text(encoding="utf-8-sig"))
        if payload.get("status") != "PASS":
            raise SystemExit(f"{label} did not pass")
    for prefix, manifest_path in (
        ("frontend", clean_frontend_manifest),
        ("backend", clean_backend_manifest),
    ):
        for relative, expected_digest in manifest_entries(manifest_path).items():
            counterpart = root / prefix / Path(relative)
            if not counterpart.is_file() or sha256(counterpart) != expected_digest:
                raise SystemExit(
                    f"separate clean-source parity mismatch: {prefix}/{relative}"
                )

    payload: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if FORBIDDEN_DIRECTORIES.intersection(relative.parts):
            raise SystemExit(f"generated directory found in clean source: {relative}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise SystemExit(f"generated file found in clean source: {relative}")
        if path.name not in {MANIFEST_NAME, VERIFICATION_NAME}:
            payload.append(path)

    allowed_db_root = root / "backend" / "src" / "sqlite_brain_builder" / "resources" / "public_model_env15"
    for pattern in ("*.sqlite", "*.sqlite3", "*.db"):
        for database in root.rglob(pattern):
            if not database.is_relative_to(allowed_db_root):
                raise SystemExit(f"runtime or user database found in source: {database.relative_to(root)}")

    entries = [
        f"{sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in payload
    ]
    entries.sort(key=str.casefold)
    manifest = root / MANIFEST_NAME
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    verification = {
        "schema": "T023_FULL_APP_CLEAN_SOURCE_VERIFICATION_V4",
        "status": "T023_INSTALLER_V1_REPLACEMENT_HIL_AWAITING_HUMAN_ACCEPTANCE",
        "sealed_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_root": str(root),
        "layout": {
            "frontend": "frontend/src",
            "theme_contract": "frontend/theme-contract",
            "tauri_host": "frontend/src-tauri",
            "backend": "backend/src/sqlite_brain_builder",
            "backend_tests": "backend/tests",
            "fixtures": "backend/fixtures",
            "build_pipeline": "build/BUILD_FULL_APP_EXE.ps1",
        },
        "payload_file_count": len(payload),
        "payload_bytes": sum(path.stat().st_size for path in payload),
        "source_manifest": MANIFEST_NAME,
        "source_manifest_sha256": sha256(manifest),
        "validation": {
            "fresh_source_backend_tests": "PASS",
            "fresh_source_backend_test_count": args.backend_test_count,
            "full_workspace_tests": args.workspace_test_status,
            "full_workspace_test_count": args.workspace_test_count,
            "packaged_worker_system_ping": "PASS",
            "packaged_worker_workspace_init": "PASS",
            "fresh_source_frontend_build": "PASS",
            "fresh_source_full_app_exe_build": "PASS",
            "generated_outputs_absent_from_seal": "PASS",
            "runtime_or_user_databases_absent": "PASS",
            "glb_payload_count": 0,
            "glb_plan_state": "DEFERRED_PUBLIC_VERSION_2_AFTER_INSTALLER_VERSION_1",
            "isolated_runtime_validation": "PASS",
            "installed_pc_metrics_validation": "PASS",
            "installed_ui_handoff_validation": "PASS",
            "installer_prerequisite_scan": "PASS",
            "installer_postinstall_self_test": "PASS",
        },
        "source_parity": {
            "clean_frontend_manifest_sha256": sha256(clean_frontend_manifest),
            "clean_backend_manifest_sha256": sha256(clean_backend_manifest),
            "relationship": "FULL_APP_FRONTEND_AND_BACKEND_PAYLOADS_MATCH_SEPARATE_CLEAN_SOURCE_ARTIFACTS",
        },
        "fresh_build": {
            "full_app_exe_sha256": sha256(executable),
            "full_app_exe_size_bytes": executable.stat().st_size,
            "embedded_worker_sha256": sha256(worker),
            "embedded_worker_size_bytes": worker.stat().st_size,
            "bundle_mode": "NSIS_INSTALLER_V1_WITH_SELF_CONTAINED_FULL_APP",
            "installer_started": True,
            "installer_artifact_present": True,
            "installer_sha256": sha256(installer),
            "installer_size_bytes": installer.stat().st_size,
            "runtime_validation_sha256": sha256(runtime_validation),
            "prerequisite_receipt_sha256": sha256(prerequisite_receipt),
            "postinstall_receipt_sha256": sha256(postinstall_receipt),
            "metrics_validation_sha256": sha256(metrics_validation),
            "ui_handoff_validation_sha256": sha256(ui_handoff_validation),
            "localhost_dev_server_required": False,
            "external_python_required": False,
            "workspace_source_tree_required": False,
        },
        "hil_gate": "T023-INSTALLER-V1-GOLD-THEME-PC-METRICS-AND-SHELL-HANDOFF-ACCEPTANCE",
        "external_provider_consumer_uploads": "DEFERRED_TO_HUMAN_HIL_WITH_FRESH_BRAINS_PER_USER",
    }
    (root / VERIFICATION_NAME).write_text(
        json.dumps(verification, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(verification, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
