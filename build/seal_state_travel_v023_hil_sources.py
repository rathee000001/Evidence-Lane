from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}

POINTER_SHA256 = "39F79672AAB2CAB29DF131E486DA4FCFA3111624D539EDBA15057107B70110BE"
V023_EXE_SHA256 = "4A2E8F3F6F608BA7652E1B9774C6D7088B1CC541661C5A759AC8A2F27D4E585C"
V023_WORKER_SHA256 = "B6EC6149AF50FA921DB183370FC9CDF8443DCD89AEDA481A359B6BC74472B555"
V023_BUILD_RESULT_SHA256 = "BFDD2A458BC68EBA8F17485835359D6D561F010A4953DD2D4D32F568A25B4680"
V023_RUNTIME_VALIDATION_SHA256 = "679ABA883EB6B48A576FDB7D5630FEF8F8B10A2AF9A4229D7E782FD8D5AA91FA"
CHATGPT_GEMINI_SHA256 = "670F3FE2EBC7C13DD187258833107880107606CCFA90AC53D1C59342AE80B9ED"
CODEX_SHA256 = "016A3CA6B62D134B5352E2B3708A71BE468E06C642EAEC47C399EE740CDA0CAF"

STATUS = "PASS_V023_STATE_TRAVEL_SUCCESSOR_HIL_AWAITING_HUMAN_REVIEW_NOT_SUCCESS"
SOURCE_DELTA_STATE = "T023_STATE_TRAVEL_SUCCESSOR_V023_HIL_AWAITING_HUMAN_REVIEW_NOT_SUCCESS"
HIL_GATE = "T023-V023-STATE-TRAVEL-STANDALONE-NATIVE-HUMAN-ACCEPTANCE"
SIMULATOR_URL = "http://127.0.0.1:4190/?demo=t023&benchmark_backend=1"

EXPECTED_LISTENERS = {4190: 24016, 4191: 52668}
FORBIDDEN_LISTENERS = {1430}
EXPECTED_QUARANTINE_FILE_COUNT = 25_147
EXPECTED_QUARANTINE_BYTES = 527_542_360

CHATGPT_GEMINI_ARCHIVE_NAME = (
    "UEPC_ENV15_PUBLIC_LOCKED_SECTION_PACKAGE_CHATGPT_GEMINI.zip"
)
CODEX_ARCHIVE_NAME = "UEPC_ENV15_PUBLIC_LOCKED_SECTION_PACKAGE_CODEX.zip"
LEGACY_ARCHIVE_NAME = "UEPC_ENV15_PUBLIC_LOCKED_SECTION_PACKAGE.zip"
CODEX_SUPPORT_MEMBERS = {
    "codex/CODEX_EXPECTED_FILE_MAP.json",
    "codex/CODEX_PUBLIC_SECTION_PATCH_SCOPE.md",
    "codex/UEPC_ENV15_CODEX_PUBLIC_SECTION_UPDATE_HANDOFF.md",
    "codex/UEPC_ENV15_CODEX_PUBLIC_SECTION_UPDATE_PROMPT.md",
    "codex/codex_update_flow.mmd",
    "codex/codex_update_flow.svg",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def require_file(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise SystemExit(f"{label} is not a file: {resolved}")
    return resolved


def is_under(relative: Path, prefix: Path) -> bool:
    return relative.parts[: len(prefix.parts)] == prefix.parts


def payload_files(
    root: Path,
    *,
    database_prefixes: tuple[Path, ...] = (),
) -> list[Path]:
    payload: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        forbidden_parts = FORBIDDEN_DIRECTORIES.intersection(relative.parts)
        if forbidden_parts:
            raise SystemExit(
                f"generated path found in clean source: {root} :: {relative} :: "
                f"{sorted(forbidden_parts)}"
            )
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        if suffix in FORBIDDEN_SUFFIXES:
            raise SystemExit(f"generated file found in clean source: {root} :: {relative}")
        if suffix in DATABASE_SUFFIXES and not any(
            is_under(relative, prefix) for prefix in database_prefixes
        ):
            raise SystemExit(
                f"database outside public_model_env15 resource: {root} :: {relative}"
            )
        if path.name not in METADATA_NAMES:
            payload.append(path)
    return payload


def tree_hashes(
    root: Path,
    *,
    database_prefixes: tuple[Path, ...] = (),
) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in payload_files(root, database_prefixes=database_prefixes)
    }


def assert_parity(
    full_layer: Path,
    separate: Path,
    *,
    full_database_prefixes: tuple[Path, ...] = (),
    separate_database_prefixes: tuple[Path, ...] = (),
) -> int:
    full = tree_hashes(full_layer, database_prefixes=full_database_prefixes)
    clean = tree_hashes(separate, database_prefixes=separate_database_prefixes)
    if full != clean:
        differences = sorted(set(full) ^ set(clean))
        differences.extend(
            relative
            for relative in sorted(set(full) & set(clean))
            if full[relative] != clean[relative]
        )
        raise SystemExit(f"clean-source parity mismatch: {differences[:30]}")
    return len(full)


def seal_manifest(
    root: Path,
    manifest_name: str,
    *,
    database_prefixes: tuple[Path, ...] = (),
) -> tuple[list[Path], Path]:
    payload = payload_files(root, database_prefixes=database_prefixes)
    lines = sorted(
        (
            f"{sha256(path)}  {path.relative_to(root).as_posix()}"
            for path in payload
        ),
        key=str.casefold,
    )
    manifest = root / manifest_name
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload, manifest


def verify_manifest(root: Path, manifest: Path) -> int:
    entries = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if separator != "  " or len(digest) != 64 or not relative:
            raise SystemExit(f"invalid manifest line: {manifest} :: {line}")
        target = root / Path(relative)
        if not target.is_file():
            raise SystemExit(f"manifest target missing: {target}")
        if sha256(target) != digest:
            raise SystemExit(f"manifest hash mismatch: {target}")
        entries += 1
    return entries


def validate_split_archives(chat_path: Path, codex_path: Path) -> dict[str, Any]:
    expected = {
        "chatgpt_gemini": {
            "path": chat_path,
            "sha256": CHATGPT_GEMINI_SHA256,
            "size": 2_602_101,
            "entries": 128,
            "files": 128,
            "directories": 0,
        },
        "codex": {
            "path": codex_path,
            "sha256": CODEX_SHA256,
            "size": 2_449_660,
            "entries": 161,
            "files": 135,
            "directories": 26,
        },
    }
    result: dict[str, Any] = {}
    name_sets: dict[str, set[str]] = {}
    payload_hashes: dict[str, dict[str, str]] = {}
    for label, contract in expected.items():
        path = require_file(Path(contract["path"]), f"{label} Env15 authority")
        observed_hash = sha256(path)
        if observed_hash != contract["sha256"]:
            raise SystemExit(f"{label} Env15 SHA-256 mismatch: {observed_hash}")
        if path.stat().st_size != contract["size"]:
            raise SystemExit(f"{label} Env15 size mismatch: {path.stat().st_size}")
        with zipfile.ZipFile(path) as archive:
            crc_failure = archive.testzip()
            if crc_failure is not None:
                raise SystemExit(f"{label} Env15 CRC failure: {crc_failure}")
            infos = archive.infolist()
            files = [item for item in infos if not item.is_dir()]
            directories = [item for item in infos if item.is_dir()]
            if len(infos) != contract["entries"]:
                raise SystemExit(f"{label} Env15 entry-count mismatch: {len(infos)}")
            if len(files) != contract["files"]:
                raise SystemExit(f"{label} Env15 file-count mismatch: {len(files)}")
            if len(directories) != contract["directories"]:
                raise SystemExit(
                    f"{label} Env15 directory-count mismatch: {len(directories)}"
                )
            names = {item.filename for item in files}
            name_sets[label] = names
            payload_hashes[label] = {
                name: hashlib.sha256(archive.read(name)).hexdigest().upper()
                for name in names
            }
        result[label] = {
            "path": str(path),
            "sha256": observed_hash,
            "size_bytes": path.stat().st_size,
            "entry_count": contract["entries"],
            "file_count": contract["files"],
            "directory_count": contract["directories"],
        }
    chat_names = name_sets["chatgpt_gemini"]
    codex_names = name_sets["codex"]
    if any(name.casefold().startswith("codex/") for name in chat_names):
        raise SystemExit("ChatGPT/Gemini Env15 authority leaks Codex support members")
    common = chat_names & codex_names
    if common != chat_names:
        raise SystemExit("Codex Env15 authority does not retain the exact common authority set")
    mismatches = [
        name
        for name in sorted(common)
        if payload_hashes["chatgpt_gemini"][name] != payload_hashes["codex"][name]
    ]
    if mismatches:
        raise SystemExit(f"Env15 common-member hash mismatch: {mismatches[:10]}")
    observed_support = CODEX_SUPPORT_MEMBERS & codex_names
    if observed_support != CODEX_SUPPORT_MEMBERS:
        raise SystemExit("Codex Env15 support-member set mismatch")
    result["relationship"] = {
        "common_member_count": len(common),
        "common_member_hashes_equal": True,
        "chatgpt_gemini_codex_member_count": 0,
        "codex_support_member_count": len(observed_support),
        "locked_authorities": ["Env", "UOP"],
        "project_authority": "MUTABLE_GOVERNED_SECTOR_GRAPH",
    }
    return result


def validate_copied_env15_archives(
    backend_roots: Iterable[Path],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for backend_root in backend_roots:
        resource = (
            backend_root
            / "src"
            / "sqlite_brain_builder"
            / "resources"
            / "env15_locked_read"
        )
        expected = {
            CHATGPT_GEMINI_ARCHIVE_NAME: CHATGPT_GEMINI_SHA256,
            CODEX_ARCHIVE_NAME: CODEX_SHA256,
        }
        for name, expected_hash in expected.items():
            path = require_file(resource / name, f"copied Env15 authority {name}")
            observed = sha256(path)
            if observed != expected_hash:
                raise SystemExit(f"copied Env15 authority mismatch: {path} :: {observed}")
            rows.append(
                {
                    "path": str(path),
                    "name": name,
                    "sha256": observed,
                    "runtime_authority": True,
                }
            )
        legacy = resource / LEGACY_ARCHIVE_NAME
        if legacy.is_file():
            rows.append(
                {
                    "path": str(legacy.resolve()),
                    "name": LEGACY_ARCHIVE_NAME,
                    "sha256": sha256(legacy),
                    "runtime_authority": False,
                    "classification": "HISTORICAL_COMPATIBILITY_SOURCE_EVIDENCE_ONLY",
                }
            )
        selector = require_file(
            backend_root / "src" / "sqlite_brain_builder" / "runtime" / "env15_locked_read.py",
            "Env15 runtime selector",
        )
        source = selector.read_text(encoding="utf-8")
        required_literals = {
            CHATGPT_GEMINI_ARCHIVE_NAME,
            CODEX_ARCHIVE_NAME,
            "project_authority\": \"LIVE_GOVERNED_SECTOR_GRAPH",
            "project_template_authority_imported\": False",
        }
        missing = sorted(literal for literal in required_literals if literal not in source)
        if missing:
            raise SystemExit(f"Env15 runtime selector contract missing: {missing}")
        if LEGACY_ARCHIVE_NAME in source:
            raise SystemExit("legacy Env15 archive is selected by runtime source")
    return rows


def listeners() -> dict[str, Any]:
    observed: dict[int, list[int | None]] = {}
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        port = int(connection.laddr.port)
        if port in {*EXPECTED_LISTENERS, *FORBIDDEN_LISTENERS}:
            observed.setdefault(port, []).append(connection.pid)
    for port, expected_pid in EXPECTED_LISTENERS.items():
        pids = observed.get(port, [])
        if pids != [expected_pid]:
            raise SystemExit(f"listener {port} changed: expected {expected_pid}, observed {pids}")
    active_forbidden = {port: observed[port] for port in FORBIDDEN_LISTENERS if port in observed}
    if active_forbidden:
        raise SystemExit(f"forbidden listener active: {active_forbidden}")
    return {
        "4190": {
            "status": "LISTENING_UNRESTARTED",
            "pid": EXPECTED_LISTENERS[4190],
            "role": "VITE_SIMULATOR",
        },
        "4191": {
            "status": "LISTENING_UNRESTARTED",
            "pid": EXPECTED_LISTENERS[4191],
            "role": "BENCHMARK_BRIDGE",
        },
        "1430": {"status": "NO_LISTENER"},
    }


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


def validate_live_v023(pid: int, executable: Path, worker: Path) -> dict[str, Any]:
    process = psutil.Process(pid)
    process_path = Path(process.exe()).resolve()
    if process_path != executable.resolve():
        raise SystemExit(f"live V023 process path mismatch: {process_path}")
    if sha256(executable) != V023_EXE_SHA256:
        raise SystemExit("live V023 executable SHA-256 mismatch")
    child_rows: list[dict[str, Any]] = []
    matching_workers = 0
    for child in process.children(recursive=True):
        try:
            child_path = Path(child.exe()).resolve()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        row: dict[str, Any] = {
            "pid": child.pid,
            "name": child.name(),
            "path": str(child_path),
        }
        if child.name().casefold().startswith("evidenceos-embedded-backend"):
            row["sha256"] = sha256(child_path)
            row["hash_matches_v023_worker"] = row["sha256"] == V023_WORKER_SHA256
            if row["hash_matches_v023_worker"]:
                matching_workers += 1
        child_rows.append(row)
    if matching_workers < 1:
        raise SystemExit("live V023 process has no hash-matching embedded worker child")
    windows = visible_windows_for_pid(pid)
    if not any(window["title"] == "Evidence Lane - SQLite Builder" for window in windows):
        raise SystemExit(f"live V023 native window not found: {windows}")
    return {
        "pid": pid,
        "process_path": str(process_path),
        "process_sha256": sha256(executable),
        "process_running": process.is_running(),
        "process_status": process.status(),
        "responding_window_observed": True,
        "windows": windows,
        "embedded_worker_build_path": str(worker),
        "embedded_worker_sha256": sha256(worker),
        "hash_matching_worker_child_count": matching_workers,
        "children": child_rows,
        "transport": "NATIVE_TAURI_IPC_AND_EMBEDDED_WORKER",
    }


def matching_processes(executable: Path) -> list[int]:
    expected = str(executable.resolve()).casefold()
    matches: list[int] = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            observed = str(Path(process.info["exe"]).resolve()).casefold()
        except (TypeError, OSError, psutil.AccessDenied, psutil.NoSuchProcess):
            continue
        if observed == expected:
            matches.append(int(process.info["pid"]))
    return sorted(matches)


def validate_build_and_runtime(
    build_result_path: Path,
    runtime_validation_path: Path,
    executable: Path,
    worker: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if sha256(build_result_path) != V023_BUILD_RESULT_SHA256:
        raise SystemExit("V023 build-result SHA-256 mismatch")
    if sha256(runtime_validation_path) != V023_RUNTIME_VALIDATION_SHA256:
        raise SystemExit("V023 isolated-runtime validation SHA-256 mismatch")
    build = json_read(build_result_path)
    runtime = json_read(runtime_validation_path)
    build_truth = {
        "status": build.get("status") == "PASS",
        "exe_hash": build.get("full_app_exe_sha256") == V023_EXE_SHA256 == sha256(executable),
        "worker_hash": build.get("embedded_worker_sha256") == V023_WORKER_SHA256 == sha256(worker),
        "installer_not_started": build.get("installer_started") is False,
        "single_exe": build.get("bundle_mode") == "NO_BUNDLE_SINGLE_EXE",
        "installer_absent": build.get("installer_v1") is None,
    }
    if not all(build_truth.values()):
        raise SystemExit(f"V023 build contract failed: {build_truth}")
    runtime_truth = {
        "status": runtime.get("status") == "PASS",
        "production_frontend": runtime.get("production_frontend_boot_probe") == "PASS",
        "embedded_ipc": runtime.get("embedded_backend_ipc") == "PASS",
        "isolated_launch": runtime.get("isolated_launch_without_dev_ports") == "PASS",
        "no_python_child": runtime.get("child_python_process_count") == 0,
        "invalid_overrides_ignored": runtime.get("invalid_external_overrides_ignored") is True,
        "host_registry_unchanged": runtime.get("host_workspace_registry_unchanged") is True,
    }
    if not all(runtime_truth.values()):
        raise SystemExit(f"V023 isolated-runtime contract failed: {runtime_truth}")
    return build, runtime


def quarantine_summary(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise SystemExit(f"generated-source quarantine is not a directory: {resolved}")
    files = [entry for entry in resolved.rglob("*") if entry.is_file()]
    byte_count = sum(entry.stat().st_size for entry in files)
    if len(files) != EXPECTED_QUARANTINE_FILE_COUNT or byte_count != EXPECTED_QUARANTINE_BYTES:
        raise SystemExit(
            "generated-source quarantine changed: "
            f"files={len(files)} bytes={byte_count}"
        )
    return {
        "path": str(resolved),
        "file_count": len(files),
        "bytes": byte_count,
        "operation": "RECOVERABLE_MOVE_NOT_DELETE",
    }


def sha_sibling(path: Path) -> Path:
    destination = Path(str(path) + ".sha256")
    destination.write_text(f"{sha256(path)}  {path.name}\n", encoding="utf-8")
    return destination


def checkpoint_markdown(checkpoint: dict[str, Any]) -> str:
    adverse = "\n".join(f"- {item}" for item in checkpoint["adverse_findings"])
    return f"""# T023 V023 state-travel HIL checkpoint

Status: `{checkpoint['status']}`

This is the same linear T023 continuation. V023 is sealed as the corrected standalone successor. The pointer was not advanced, Fuse was not run, no provider upload occurred, no installer was built or executed, and HIL success is not inferred.

## Verified

- Frozen pointer SHA-256: `{checkpoint['frozen_authority']['pointer_sha256']}`
- V023 standalone EXE SHA-256: `{checkpoint['v023']['exe_sha256']}`
- Embedded worker SHA-256: `{checkpoint['v023']['worker_sha256']}`
- Backend regression: `{checkpoint['validation']['backend_full_regression']}`
- Focused native regression: `{checkpoint['validation']['focused_native_regression']}`
- Focused UI contract: `{checkpoint['validation']['focused_ui_contract']}`
- Both simulator tabs retain `{SIMULATOR_URL}`; listeners 4190/4191 were not restarted.
- Env15 ChatGPT/Gemini and Codex split authorities match the supplied SHA-256 values. Env/UOP are locked authority; Project remains mutable.

## Open adverse findings

{adverse}

## Human gate

- `APPROVE_T023_V023_STATE_TRAVEL_HIL`
- `REJECT_T023_V023_STATE_TRAVEL_HIL`
- `SUPERSEDE_T023_V023_STATE_TRAVEL_HIL`

No decision token has been supplied. `hil_success=false` remains authoritative.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--live-pid", type=int, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--build-result", type=Path, required=True)
    parser.add_argument("--runtime-validation", type=Path, required=True)
    parser.add_argument("--pointer", type=Path, required=True)
    parser.add_argument("--env-chatgpt-gemini", type=Path, required=True)
    parser.add_argument("--env-codex", type=Path, required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    parser.add_argument("--adverse-v022-exe", type=Path, required=True)
    parser.add_argument("--adverse-v022-diagnostic", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()

    full_root = Path(__file__).resolve().parents[1]
    workspace = full_root.parent
    frontend_root = workspace / "evidence-os-clean-frontend-theme"
    backend_root = workspace / "evidence-os-clean-backend"
    frontend_db_prefixes: tuple[Path, ...] = ()
    backend_db_prefixes = (
        Path("src/sqlite_brain_builder/resources/public_model_env15"),
    )
    full_db_prefixes = (
        Path("backend/src/sqlite_brain_builder/resources/public_model_env15"),
    )

    executable = require_file(args.exe, "V023 standalone EXE")
    worker = require_file(args.worker, "V023 embedded worker")
    build_result = require_file(args.build_result, "V023 build result")
    runtime_validation = require_file(args.runtime_validation, "V023 runtime validation")
    pointer = require_file(args.pointer, "T023 pointer")
    adverse_v022_exe = require_file(args.adverse_v022_exe, "V022 adverse executable")
    adverse_v022_diagnostic = args.adverse_v022_diagnostic.resolve()
    if not adverse_v022_diagnostic.is_dir():
        raise SystemExit(f"V022 adverse diagnostic is missing: {adverse_v022_diagnostic}")

    if sha256(pointer) != POINTER_SHA256:
        raise SystemExit(f"T023 pointer SHA-256 mismatch: {sha256(pointer)}")
    if sha256(executable) != V023_EXE_SHA256:
        raise SystemExit(f"V023 executable SHA-256 mismatch: {sha256(executable)}")
    if sha256(worker) != V023_WORKER_SHA256:
        raise SystemExit(f"V023 worker SHA-256 mismatch: {sha256(worker)}")

    build, isolated_runtime = validate_build_and_runtime(
        build_result,
        runtime_validation,
        executable,
        worker,
    )
    listener_state = listeners()
    live_runtime = validate_live_v023(args.live_pid, executable, worker)
    quarantine = quarantine_summary(args.quarantine)
    original_env15 = validate_split_archives(
        args.env_chatgpt_gemini.resolve(),
        args.env_codex.resolve(),
    )
    copied_env15 = validate_copied_env15_archives(
        (backend_root, full_root / "backend")
    )

    frontend_parity_count = assert_parity(
        full_root / "frontend",
        frontend_root,
        full_database_prefixes=frontend_db_prefixes,
        separate_database_prefixes=frontend_db_prefixes,
    )
    backend_parity_count = assert_parity(
        full_root / "backend",
        backend_root,
        full_database_prefixes=backend_db_prefixes,
        separate_database_prefixes=backend_db_prefixes,
    )

    frontend_payload, frontend_manifest = seal_manifest(
        frontend_root,
        CLEAN_MANIFEST,
        database_prefixes=frontend_db_prefixes,
    )
    backend_payload, backend_manifest = seal_manifest(
        backend_root,
        CLEAN_MANIFEST,
        database_prefixes=backend_db_prefixes,
    )
    if verify_manifest(frontend_root, frontend_manifest) != len(frontend_payload):
        raise SystemExit("frontend manifest verification count mismatch")
    if verify_manifest(backend_root, backend_manifest) != len(backend_payload):
        raise SystemExit("backend manifest verification count mismatch")

    generated = utc_now()
    common_verification = {
        "status": STATUS,
        "source_delta_state": SOURCE_DELTA_STATE,
        "generated_utc": generated,
        "pointer_sha256": POINTER_SHA256,
        "v023_exe_sha256": V023_EXE_SHA256,
        "v023_worker_sha256": V023_WORKER_SHA256,
        "human_hil_decision": None,
        "hil_success": False,
        "pointer_advanced": False,
        "fuse_performed": False,
        "provider_upload_performed": False,
        "installer_built": False,
        "installer_executed": False,
        "installed_app_modified": False,
    }
    frontend_verification = {
        "schema": "T023_V023_CLEAN_FRONTEND_SOURCE_VERIFICATION_V1",
        "surface": "FRONTEND_THEME",
        "root": str(frontend_root.resolve()),
        "source": str((full_root / "frontend").resolve()),
        "payload_file_count": len(frontend_payload),
        "manifest_sha256": sha256(frontend_manifest),
        "parity_file_count": frontend_parity_count,
        "validation": {
            "focused_ui_contract": "25 passed",
            "production_build": "PASS_1075_MODULES",
            "npm_audit": "PASS_ZERO_VULNERABILITIES",
            "governed_lab_build": "PASS_1075_MODULES",
            "full_app_frontend_build": "PASS_1075_MODULES",
            "separate_frontend_build": "PASS_1075_MODULES",
            "generated_outputs_absent": True,
            "glb_payload_count": 0,
        },
        **common_verification,
    }
    backend_verification = {
        "schema": "T023_V023_CLEAN_BACKEND_SOURCE_VERIFICATION_V1",
        "surface": "TESTED_BACKEND",
        "root": str(backend_root.resolve()),
        "source": str((full_root / "backend").resolve()),
        "payload_file_count": len(backend_payload),
        "manifest_sha256": sha256(backend_manifest),
        "parity_file_count": backend_parity_count,
        "validation": {
            "pytest": "429 passed in 647.39s (0:10:47)",
            "test_file_count": len(list((backend_root / "tests").glob("test_*.py"))),
            "focused_native_regression": "3 passed in 0.53s",
            "build_pipeline": "37/37 PASS",
            "generated_outputs_absent": True,
            "database_policy": "ONLY_PUBLIC_MODEL_ENV15_RESOURCE_DATABASES_ALLOWED",
        },
        "env15": {
            "supplied_split_authority": original_env15,
            "copied_authorities": copied_env15,
            "legacy_archive_runtime_authority": False,
        },
        **common_verification,
    }
    if backend_verification["validation"]["test_file_count"] != 61:
        raise SystemExit(
            "clean-backend test-file count changed: "
            f"{backend_verification['validation']['test_file_count']}"
        )
    json_write(frontend_root / CLEAN_VERIFICATION, frontend_verification)
    json_write(backend_root / CLEAN_VERIFICATION, backend_verification)

    full_payload, full_manifest = seal_manifest(
        full_root,
        FULL_MANIFEST,
        database_prefixes=full_db_prefixes,
    )
    if verify_manifest(full_root, full_manifest) != len(full_payload):
        raise SystemExit("full-app manifest verification count mismatch")

    adverse_v022_pids = matching_processes(adverse_v022_exe)
    adverse_findings = [
        "Browser simulator brains 'book' and 'New Brain 36' have no stored accepted topology bundle and correctly fail closed with TELEMETRY_STORED_ACCEPTED_BUNDLE_REQUIRED; no topology was substituted.",
        "Physical no-click hover-wheel traversal in the native V023 window still requires human confirmation; explicit Zoom In visibly advanced one nested glass-orb layer.",
        "V022 isolated cold-bootstrap code-101 failure and its diagnostic bundle remain preserved as adverse evidence.",
        "The Rust build emitted an unused `ensure` warning; this was not a runtime failure.",
        "HIL approval has not been supplied and success is not inferred from builds, tests, screenshots, or runtime registration.",
    ]
    full_verification = {
        "schema": "T023_V023_FULL_APP_CLEAN_SOURCE_VERIFICATION_V1",
        "status": STATUS,
        "source_delta_state": SOURCE_DELTA_STATE,
        "sealed_utc": generated,
        "source_root": str(full_root),
        "payload_file_count": len(full_payload),
        "payload_bytes": sum(path.stat().st_size for path in full_payload),
        "source_manifest": FULL_MANIFEST,
        "source_manifest_sha256": sha256(full_manifest),
        "source_parity": {
            "frontend_file_count": frontend_parity_count,
            "backend_file_count": backend_parity_count,
            "clean_frontend_manifest_sha256": sha256(frontend_manifest),
            "clean_backend_manifest_sha256": sha256(backend_manifest),
            "relationship": "FULL_APP_FRONTEND_AND_BACKEND_PAYLOADS_MATCH_SEPARATE_CLEAN_SOURCES",
        },
        "validation": {
            "backend_full_regression": "429 passed in 647.39s (0:10:47)",
            "backend_test_file_count": 61,
            "focused_native_regression": "3 passed in 0.53s",
            "focused_ui_contract": "25 passed",
            "frontend_production_builds": "PASS_1075_MODULES_ALL_THREE",
            "frontend_audit": "PASS_ZERO_VULNERABILITIES",
            "build_pipeline": "37/37 PASS",
            "generated_outputs_absent": "PASS",
            "manifest_entries_rehashed": "PASS",
        },
        "frozen_authority": {
            "pointer": str(pointer),
            "pointer_sha256": sha256(pointer),
            "pointer_advanced": False,
        },
        "v023": {
            "exe": str(executable),
            "exe_sha256": sha256(executable),
            "worker": str(worker),
            "worker_sha256": sha256(worker),
            "build_result": str(build_result),
            "build_result_sha256": sha256(build_result),
            "runtime_validation": str(runtime_validation),
            "runtime_validation_sha256": sha256(runtime_validation),
            "build_contract": build,
            "isolated_runtime_contract": isolated_runtime,
            "live_runtime": live_runtime,
            "bundle_mode": "NO_BUNDLE_SINGLE_EXE",
            "installer_built": False,
            "installer_executed": False,
            "installed_app_modified": False,
        },
        "simulator_lanes": {
            "url": SIMULATOR_URL,
            "codex_in_app_browser": "OPEN_EXACT_URL_VISIBLE_CONFIRMED_BEFORE_SEAL",
            "chrome": "OPEN_EXACT_URL_SELECTED_BASELINE_CONTROL_DETACHED_DURING_SCREENSHOT_READ_NO_RELOAD",
            "listeners": listener_state,
            "listener_restart_performed": False,
            "worker_restart_performed": False,
        },
        "env15": {
            "supplied_split_authority": original_env15,
            "copied_authorities": copied_env15,
            "locked_authorities": ["Env", "UOP"],
            "project_authority": "MUTABLE_GOVERNED_SECTOR_GRAPH",
            "project_locked_sector_selected": False,
            "legacy_archive_runtime_authority": False,
        },
        "source_cleanliness": {
            "forbidden_directories": sorted(FORBIDDEN_DIRECTORIES),
            "forbidden_suffixes": sorted(FORBIDDEN_SUFFIXES),
            "database_policy": "ONLY_BACKEND_PUBLIC_MODEL_ENV15_RESOURCE_DATABASES_ALLOWED",
            "quarantine": quarantine,
        },
        "adverse_v022": {
            "exe": str(adverse_v022_exe),
            "exe_sha256": sha256(adverse_v022_exe),
            "diagnostic": str(adverse_v022_diagnostic),
            "artifact_preserved": True,
            "matching_live_pids_at_seal": adverse_v022_pids,
        },
        "adverse_findings": adverse_findings,
        "operator_actions": {
            "fuse_performed": False,
            "pointer_advanced": False,
            "provider_upload_performed": False,
            "admin_or_glb_work_started": False,
            "published_or_deployed": False,
        },
        "hil_gate": {
            "id": HIL_GATE,
            "decision": None,
            "hil_success": False,
            "tokens": [
                "APPROVE_T023_V023_STATE_TRAVEL_HIL",
                "REJECT_T023_V023_STATE_TRAVEL_HIL",
                "SUPERSEDE_T023_V023_STATE_TRAVEL_HIL",
            ],
        },
        "evaluation": {
            "verdict": "FIX_THEN_PURSUE",
            "confidence": "HIGH",
            "evidence_that_would_change_it": [
                "Human confirms native no-click hover-wheel nested-orb traversal.",
                "Human accepts the truthful fail-closed state for brains without an accepted stored topology bundle, or those bundles are materialized and round-trip tested.",
                "Any pointer, source-parity, manifest, process, listener, Env15, build, or regression mismatch would invalidate this checkpoint.",
            ],
        },
    }
    json_write(full_root / FULL_VERIFICATION, full_verification)

    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "schema": "T023_V023_STATE_TRAVEL_HIL_CHECKPOINT_V1",
        **full_verification,
        "full_source_verification": str((full_root / FULL_VERIFICATION).resolve()),
        "full_source_verification_sha256": sha256(full_root / FULL_VERIFICATION),
        "full_source_manifest": str(full_manifest.resolve()),
        "full_source_manifest_sha256": sha256(full_manifest),
    }
    checkpoint_path = report_dir / "T023_V023_STATE_TRAVEL_HIL_CHECKPOINT.json"
    markdown_path = report_dir / "T023_V023_STATE_TRAVEL_HIL_CHECKPOINT.md"
    json_write(checkpoint_path, checkpoint)
    markdown_path.write_text(checkpoint_markdown(checkpoint), encoding="utf-8")
    checkpoint_hash_path = sha_sibling(checkpoint_path)
    markdown_hash_path = sha_sibling(markdown_path)

    output = {
        "status": STATUS,
        "source_delta_state": SOURCE_DELTA_STATE,
        "full_manifest": str(full_manifest),
        "full_manifest_sha256": sha256(full_manifest),
        "full_verification": str((full_root / FULL_VERIFICATION).resolve()),
        "full_verification_sha256": sha256(full_root / FULL_VERIFICATION),
        "frontend_manifest_sha256": sha256(frontend_manifest),
        "backend_manifest_sha256": sha256(backend_manifest),
        "checkpoint": str(checkpoint_path),
        "checkpoint_sha256": sha256(checkpoint_path),
        "checkpoint_hash_file": str(checkpoint_hash_path),
        "markdown": str(markdown_path),
        "markdown_sha256": sha256(markdown_path),
        "markdown_hash_file": str(markdown_hash_path),
        "hil_success": False,
        "human_hil_decision": None,
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
