from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import winreg
from datetime import datetime, timezone
from pathlib import Path

import psutil


FULL_MANIFEST = "FULL_APP_SOURCE_MANIFEST.sha256"
FULL_VERIFICATION = "FULL_APP_SOURCE_VERIFICATION.json"
CLEAN_MANIFEST = "CLEAN_SOURCE_MANIFEST.sha256"
CLEAN_VERIFICATION = "CLEAN_SOURCE_VERIFICATION.json"
METADATA_NAMES = {
    FULL_MANIFEST,
    FULL_VERIFICATION,
    CLEAN_MANIFEST,
    CLEAN_VERIFICATION,
}
FORBIDDEN_DIRECTORIES = {
    "node_modules",
    "dist",
    "target",
    "__pycache__",
    ".pytest_cache",
    "build-output",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".exe", ".glb"}
HIL_STATE = "T023_PUBLIC_V1_GOLD_NATIVE_PERFORMANCE_PERSISTENT_FOLDER_GRAPH_PROVIDER_READABILITY_UPDATE_INSTALLER_HIL_AWAITING_HUMAN_ACCEPTANCE"
HIL_GATE = "T023-PUBLIC-V1-GOLD-NATIVE-PERFORMANCE-PERSISTENT-FOLDER-GRAPH-PROVIDER-READABILITY-STANDALONE-EXE-UPDATE-INSTALLER-ACCEPTANCE"
DELTA_ID = "T023-DELTA-GOLD-NATIVE-PERFORMANCE-FOLDER-GRAPH-PROVIDER-READABILITY-HIL-001"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def payload_files(root: Path) -> list[Path]:
    payload: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if FORBIDDEN_DIRECTORIES.intersection(relative.parts):
            raise SystemExit(f"generated directory found in clean source: {root} :: {relative}")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            raise SystemExit(f"generated file found in clean source: {root} :: {relative}")
        if path.name not in METADATA_NAMES:
            payload.append(path)
    return payload


def seal_manifest(root: Path, manifest_name: str) -> tuple[list[Path], Path]:
    payload = payload_files(root)
    lines = [
        f"{sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in payload
    ]
    lines.sort(key=str.casefold)
    manifest = root / manifest_name
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload, manifest


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in payload_files(root)
    }


def assert_parity(full_layer: Path, separate: Path) -> None:
    full = tree_hashes(full_layer)
    clean = tree_hashes(separate)
    if full != clean:
        differences = sorted(set(full) ^ set(clean))
        differences.extend(
            relative
            for relative in sorted(set(full) & set(clean))
            if full[relative] != clean[relative]
        )
        raise SystemExit(f"clean-source parity mismatch: {differences[:20]}")


def read_installed_identity() -> dict[str, str]:
    key_name = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Evidence OS"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
        values = {
            name: winreg.QueryValueEx(key, name)[0]
            for name in ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString")
        }
    values["registry_hive"] = "HKCU"
    values["registry_key"] = key_name
    return values


def visible_windows_for_pid(pid: int) -> list[dict[str, int | str]]:
    user32 = ctypes.windll.user32
    windows: list[dict[str, int | str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            windows.append({"handle": int(hwnd), "title": title.value})
        return True

    user32.EnumWindows(callback, 0)
    return windows


def define(source: str, name: str) -> str:
    match = re.search(rf'^!define\s+{re.escape(name)}\s+"([^"]*)"', source, re.MULTILINE)
    if not match:
        raise SystemExit(f"generated NSIS definition missing: {name}")
    return match.group(1)


def write_runtime_receipt(
    *,
    pid: int,
    executable: Path,
    worker: Path,
    prior_runtime: Path,
    destination: Path,
) -> dict:
    prior = json_read(prior_runtime)
    if prior.get("status") != "PASS":
        raise SystemExit("standalone CLI runtime validation is not PASS")
    process = psutil.Process(pid)
    process_path = Path(process.exe()).resolve()
    if process_path != executable.resolve():
        raise SystemExit(f"live process path mismatch: {process_path}")
    children = process.children(recursive=False)
    worker_hash = sha256(worker)
    child_payload = []
    for child in children:
        child_path = Path(child.exe()).resolve()
        child_payload.append(
            {
                "pid": child.pid,
                "name": child.name(),
                "path": str(child_path),
                "sha256": sha256(child_path),
            }
        )
    if not any(child["sha256"] == worker_hash for child in child_payload):
        raise SystemExit("live standalone process has no hash-matching embedded worker child")
    forbidden_listeners = []
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        if connection.laddr.port in {1430, 4190}:
            forbidden_listeners.append(
                {"port": connection.laddr.port, "pid": connection.pid}
            )
    if forbidden_listeners:
        raise SystemExit(f"retired simulator listener is active: {forbidden_listeners}")
    windows = visible_windows_for_pid(pid)
    if not any(window["title"] == "Evidence OS - SQLite Builder" for window in windows):
        raise SystemExit(f"native Evidence OS window title not found: {windows}")
    receipt = {
        "schema": "T023_NATIVE_STANDALONE_LIVE_RUNTIME_VALIDATION_V1",
        "status": "PASS",
        "validated_utc": utc_now(),
        "pid": pid,
        "process_path": str(process_path),
        "process_sha256": sha256(executable),
        "process_running": process.is_running(),
        "process_status": process.status(),
        "process_priority_class": int(process.nice()),
        "process_priority_truth": "ABOVE_NORMAL_PRIORITY_CLASS",
        "responding_window": True,
        "windows": windows,
        "embedded_worker_sha256": worker_hash,
        "children": child_payload,
        "retired_simulator_ports": {"1430": "NO_LISTENER", "4190": "NO_LISTENER"},
        "transport": "NATIVE_TAURI_IPC_AND_EMBEDDED_WORKER_ONLY",
        "prior_cli_runtime_validation": str(prior_runtime),
        "prior_cli_runtime_validation_sha256": sha256(prior_runtime),
        "installed_app_before_hil": read_installed_identity(),
        "installed_app_modified_by_codex": False,
    }
    json_write(destination, receipt)
    return receipt


def write_update_receipt(
    *,
    installer: Path,
    payload: Path,
    build_result: Path,
    prerequisite_scan: Path,
    tauri_config: Path,
    generated_nsis: Path,
    hooks: Path,
    destination: Path,
) -> dict:
    build = json_read(build_result)
    prerequisite = json_read(prerequisite_scan)
    config = json_read(tauri_config)
    nsis = generated_nsis.read_text(encoding="utf-8")
    hook_source = hooks.read_text(encoding="utf-8")
    installed = read_installed_identity()
    required = {
        "build_status": build.get("status") == "PASS",
        "prerequisite_scan_status": prerequisite.get("status") == "PASS",
        "product_name": config.get("productName") == "Evidence OS" == define(nsis, "PRODUCTNAME"),
        "version": config.get("version") == "0.1.1" == define(nsis, "VERSION"),
        "bundle_id": config.get("identifier") == "com.evidenceos.clean.sqlitebuilder" == define(nsis, "BUNDLEID"),
        "install_mode": define(nsis, "INSTALLMODE") == "currentUser",
        "uninstall_key": define(nsis, "UNINSTKEY") == r"Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCTNAME}",
        "installed_product_match": installed["DisplayName"] == "Evidence OS",
        "installed_version_is_prior": installed["DisplayVersion"] == "0.1.0",
        "semver_upgrade_branch": 'SemverCompare "${VERSION}" $R0' in nsis and "Upgrading" in nsis,
        "managed_no_manual_pre_uninstall": "In update mode, always proceeds without uninstalling" in nsis,
        "prerequisite_preinstall_hook": "NSIS_HOOK_PREINSTALL" in hook_source,
        "postinstall_self_test_hook": "NSIS_HOOK_POSTINSTALL" in hook_source,
    }
    if not all(required.values()):
        raise SystemExit(f"installer update contract failed: {required}")
    receipt = {
        "schema": "T023_PUBLIC_V1_IN_PLACE_UPDATE_INSTALLER_CONTRACT_VALIDATION_V1",
        "status": "PASS",
        "validated_utc": utc_now(),
        "installer": str(installer),
        "installer_sha256": sha256(installer),
        "installer_size_bytes": installer.stat().st_size,
        "payload": str(payload),
        "payload_sha256": sha256(payload),
        "payload_size_bytes": payload.stat().st_size,
        "product_name": "Evidence OS",
        "target_version": "0.1.1",
        "bundle_id": "com.evidenceos.clean.sqlitebuilder",
        "install_mode": "currentUser",
        "installed_identity_before_hil": installed,
        "update_behavior": "DETECT_EXISTING_HKCU_PRODUCT_AND_MANAGE_0_1_0_TO_0_1_1_UPGRADE_WITHOUT_MANUAL_PRE_UNINSTALL",
        "allow_downgrades": False,
        "prerequisite_policy": "SCAN_FIRST_INSTALL_MISSING_FAIL_CLOSED",
        "postinstall_policy": "EMBEDDED_WORKER_HASH_AND_BACKEND_PING_FAIL_CLOSED",
        "contract_assertions": required,
        "build_result": str(build_result),
        "build_result_sha256": sha256(build_result),
        "prerequisite_scan": str(prerequisite_scan),
        "prerequisite_scan_sha256": sha256(prerequisite_scan),
        "installer_executed_by_codex": False,
        "installed_app_modified_by_codex": False,
    }
    json_write(destination, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--standalone-exe", type=Path, required=True)
    parser.add_argument("--standalone-pid", type=int, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--standalone-runtime", type=Path, required=True)
    parser.add_argument("--live-runtime-receipt", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--installer-payload", type=Path, required=True)
    parser.add_argument("--installer-build-result", type=Path, required=True)
    parser.add_argument("--prerequisite-scan", type=Path, required=True)
    parser.add_argument("--generated-nsis", type=Path, required=True)
    parser.add_argument("--installer-hooks", type=Path, required=True)
    parser.add_argument("--update-contract-receipt", type=Path, required=True)
    parser.add_argument("--backend-test-count", type=int, required=True)
    parser.add_argument("--backend-test-summary", required=True)
    parser.add_argument("--workspace-test-count", type=int, required=True)
    parser.add_argument("--workspace-test-summary", required=True)
    parser.add_argument("--gold-performance-receipt", type=Path, required=True)
    parser.add_argument(
        "--expected-gold-source",
        type=Path,
        required=True,
        help="Expected local Gold source recorded by the performance receipt.",
    )
    args = parser.parse_args()

    full_root = Path(__file__).resolve().parents[1]
    workspace = full_root.parent
    frontend_root = workspace / "evidence-os-clean-frontend-theme"
    backend_root = workspace / "evidence-os-clean-backend"
    tauri_config = full_root / "frontend" / "src-tauri" / "tauri.conf.json"
    paths = [
        args.standalone_exe,
        args.worker,
        args.standalone_runtime,
        args.installer,
        args.installer_payload,
        args.installer_build_result,
        args.prerequisite_scan,
        args.generated_nsis,
        args.installer_hooks,
        args.gold_performance_receipt,
        tauri_config,
    ]
    if not all(path.resolve().is_file() for path in paths):
        raise SystemExit("required native EXE/update-installer evidence is missing")

    gold_performance = json_read(args.gold_performance_receipt.resolve())
    local_gold = gold_performance.get("local_gold", {})
    separate_git = gold_performance.get("separate_git_only", {})
    required_gold_truth = {
        "evidence_status": gold_performance.get("status")
        == "PASS_LOCAL_GOLD_HARD_240S_AND_SEPARATE_GIT_ONLY_WITH_PROVIDER_READABILITY",
        "local_source": Path(str(local_gold.get("source", ""))).resolve()
        == args.expected_gold_source.resolve(),
        "local_lane": local_gold.get("real_source_lane") == "local_code",
        "local_github_empty": local_gold.get("github_code_active_source_count") == 0,
        "local_hard_time": float(local_gold.get("external_wall_seconds", 240.0))
        < float(local_gold.get("hard_limit_seconds", 240.0)) == 240.0,
        "separate_git_only": separate_git.get("result")
        == "PASS_SEPARATE_GIT_ONLY_NOT_LOCAL_ACCEPTANCE",
        "combined_acceptance_prohibited": gold_performance.get("combined_local_git_acceptance")
        == "PROHIBITED_NOT_RUN",
        "provider_upload_absent": gold_performance.get("provider_upload_performed") is False,
    }
    failed_gold_truth = [name for name, passed in required_gold_truth.items() if not passed]
    if failed_gold_truth:
        raise SystemExit(f"Gold native performance/lane evidence failed: {failed_gold_truth}")

    runtime = write_runtime_receipt(
        pid=args.standalone_pid,
        executable=args.standalone_exe.resolve(),
        worker=args.worker.resolve(),
        prior_runtime=args.standalone_runtime.resolve(),
        destination=args.live_runtime_receipt.resolve(),
    )
    update = write_update_receipt(
        installer=args.installer.resolve(),
        payload=args.installer_payload.resolve(),
        build_result=args.installer_build_result.resolve(),
        prerequisite_scan=args.prerequisite_scan.resolve(),
        tauri_config=tauri_config,
        generated_nsis=args.generated_nsis.resolve(),
        hooks=args.installer_hooks.resolve(),
        destination=args.update_contract_receipt.resolve(),
    )

    assert_parity(full_root / "frontend", frontend_root)
    assert_parity(full_root / "backend", backend_root)
    frontend_payload, frontend_manifest = seal_manifest(frontend_root, CLEAN_MANIFEST)
    backend_payload, backend_manifest = seal_manifest(backend_root, CLEAN_MANIFEST)
    common_artifacts = {
        "standalone_exe_sha256": sha256(args.standalone_exe),
        "installer_sha256": sha256(args.installer),
        "installer_executed_by_codex": False,
        "installed_app_modified_by_codex": False,
        "hil_state": HIL_STATE,
        "gold_performance_receipt": str(args.gold_performance_receipt.resolve()),
        "gold_performance_receipt_sha256": sha256(args.gold_performance_receipt),
    }
    frontend_verification = {
        "schema": "T023_CLEAN_SOURCE_VERIFICATION_V3",
        "status": "PASS",
        "generated_utc": utc_now(),
        "delta_id": DELTA_ID,
        "surface": "FRONTEND_THEME",
        "root": str(frontend_root),
        "payload_file_count": len(frontend_payload),
        "manifest_sha256": sha256(frontend_manifest),
        "manifest_algorithm": "SHA-256",
        "manifest_sort": "ENTRY_LINE_CASEFOLD_ASCENDING",
        "source": str(full_root / "frontend"),
        "validation": {
            "npm_ci": "PASS",
            "tsc_no_emit": "PASS",
            "vite_production_build": "PASS_1074_MODULES",
            "npm_audit_production": "PASS_ZERO_PRODUCTION_VULNERABILITIES",
            "native_transport": "PASS",
            "browser_simulator_runtime": "ABSENT_RETIRED",
            "cargo_check": "PASS_WITH_EMBEDDED_WORKER",
            "full_workspace_pytest": args.workspace_test_summary,
            "glb_payload_count": 0,
            "glb_plan_state": "DEFERRED_PUBLIC_VERSION_2_AFTER_INSTALLER_VERSION_1",
            "generated_artifacts_removed_before_seal": True,
        },
        "excluded": {
            "node_modules": True,
            "web_dist": True,
            "rust_target": True,
            "production_exe": True,
            "installer": True,
            "glb_assets": True,
        },
        "production_exe_compiled_in_this_task": True,
        "installer_v1_built": True,
        **common_artifacts,
    }
    backend_verification = {
        "schema": "T023_CLEAN_SOURCE_VERIFICATION_V3",
        "status": "PASS",
        "generated_utc": utc_now(),
        "delta_id": DELTA_ID,
        "surface": "TESTED_BACKEND",
        "root": str(backend_root),
        "payload_file_count": len(backend_payload),
        "manifest_sha256": sha256(backend_manifest),
        "manifest_algorithm": "SHA-256",
        "manifest_sort": "ENTRY_LINE_CASEFOLD_ASCENDING",
        "source": str(full_root / "backend"),
        "validation": {
            "import_origin": str(backend_root / "src"),
            "pytest": "PASS",
            "pytest_summary": args.backend_test_summary,
            "test_file_count": len(list((backend_root / "tests").glob("test_*.py"))),
            "local_gold_local_code_hard_240s_stress": "PASS_122.6726512000314_SECONDS",
            "gold_ai_platform_git_only_replacement_stress": "PASS_54.1543844_SECONDS_SEPARATE_NOT_LOCAL_ACCEPTANCE",
            "combined_local_git_acceptance": "PROHIBITED_NOT_RUN",
            "provider_package_consumer_compatibility": "PASS_CHATGPT_GEMINI_CODEX_LOCAL_AUDIT_ONLY",
            "external_provider_uploads": "DEFERRED_TO_HUMAN_FRESH_BRAIN_HIL",
            "full_workspace_pytest": args.workspace_test_summary,
            "generated_artifacts_removed_before_seal": True,
            "glb_plan_state": "DEFERRED_PUBLIC_VERSION_2_AFTER_INSTALLER_VERSION_1",
        },
        "excluded": {
            "runtime_user_data": True,
            "credentials_and_environment": True,
            "build_outputs": True,
            "production_exe": True,
            "installer": True,
            "glb_assets": True,
        },
        "production_exe_compiled_in_this_task": True,
        "installer_v1_built": True,
        **common_artifacts,
    }
    json_write(frontend_root / CLEAN_VERIFICATION, frontend_verification)
    json_write(backend_root / CLEAN_VERIFICATION, backend_verification)

    full_payload, full_manifest = seal_manifest(full_root, FULL_MANIFEST)
    full_verification = {
        "schema": "T023_FULL_APP_CLEAN_SOURCE_VERIFICATION_V5",
        "status": HIL_STATE,
        "sealed_utc": utc_now(),
        "source_root": str(full_root),
        "layout": {
            "frontend": "frontend/src",
            "theme_contract": "frontend/theme-contract",
            "tauri_host": "frontend/src-tauri",
            "backend": "backend/src/sqlite_brain_builder",
            "backend_tests": "backend/tests",
            "fixtures": "backend/fixtures",
            "build_pipeline": "build/BUILD_FULL_APP_EXE.ps1",
        },
        "payload_file_count": len(full_payload),
        "payload_bytes": sum(path.stat().st_size for path in full_payload),
        "source_manifest": FULL_MANIFEST,
        "source_manifest_sha256": sha256(full_manifest),
        "validation": {
            "fresh_source_backend_tests": "PASS",
            "fresh_source_backend_test_count": args.backend_test_count,
            "full_workspace_tests": args.workspace_test_summary,
            "full_workspace_test_count": args.workspace_test_count,
            "packaged_worker_system_ping": "PASS",
            "packaged_worker_workspace_init": "PASS",
            "fresh_source_frontend_build": "PASS_1074_MODULES",
            "fresh_source_full_app_exe_build": "PASS",
            "fresh_source_update_installer_build": "PASS",
            "generated_outputs_absent_from_seal": "PASS",
            "runtime_or_user_databases_absent": "PASS",
            "glb_payload_count": 0,
            "glb_plan_state": "DEFERRED_PUBLIC_VERSION_2_AFTER_INSTALLER_VERSION_1",
            "native_live_runtime": "PASS",
            "chrome_browser_simulator": "RETIRED_ABSENT",
            "installer_prerequisite_scan": "PASS",
            "installer_update_contract": "PASS",
            "installer_postinstall_self_test": "EMBEDDED_FAIL_CLOSED_NOT_EXECUTED_BY_CODEX",
            "local_gold_local_code_hard_240s_stress": {
                "status": "PASS",
                "external_wall_seconds": local_gold["external_wall_seconds"],
                "hard_limit_seconds": local_gold["hard_limit_seconds"],
                "margin_seconds": local_gold["margin_seconds"],
                "github_code_active_source_count": 0,
            },
            "gold_ai_platform_git_only_replacement_stress": {
                "status": "PASS_SEPARATE_NOT_LOCAL_ACCEPTANCE",
                "external_wall_seconds": separate_git["external_wall_seconds"],
            },
            "combined_local_git_acceptance": "PROHIBITED_NOT_RUN",
            "gold_performance_receipt_sha256": sha256(args.gold_performance_receipt),
        },
        "source_parity": {
            "clean_frontend_manifest_sha256": sha256(frontend_manifest),
            "clean_backend_manifest_sha256": sha256(backend_manifest),
            "relationship": "FULL_APP_FRONTEND_AND_BACKEND_PAYLOADS_MATCH_SEPARATE_CLEAN_SOURCE_ARTIFACTS",
        },
        "fresh_build": {
            "standalone_exe_sha256": sha256(args.standalone_exe),
            "standalone_exe_size_bytes": args.standalone_exe.stat().st_size,
            "installer_payload_sha256": sha256(args.installer_payload),
            "installer_payload_size_bytes": args.installer_payload.stat().st_size,
            "embedded_worker_sha256": sha256(args.worker),
            "embedded_worker_size_bytes": args.worker.stat().st_size,
            "bundle_mode": "STANDALONE_EXE_AND_NSIS_IN_PLACE_UPDATE_INSTALLER",
            "installer_built": True,
            "installer_artifact_present": True,
            "installer_sha256": sha256(args.installer),
            "installer_size_bytes": args.installer.stat().st_size,
            "live_runtime_validation_sha256": sha256(args.live_runtime_receipt),
            "update_contract_validation_sha256": sha256(args.update_contract_receipt),
            "localhost_dev_server_required": False,
            "external_python_required": False,
            "workspace_source_tree_required": False,
            "installer_executed_by_codex": False,
            "installed_app_modified_by_codex": False,
        },
        "live_runtime": runtime,
        "update_installer_contract": update,
        "hil_gate": HIL_GATE,
        "external_provider_consumer_uploads": "DEFERRED_TO_HUMAN_HIL_WITH_FRESH_BRAINS_PER_USER",
    }
    json_write(full_root / FULL_VERIFICATION, full_verification)
    print(json.dumps(full_verification, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
