from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil


def _sha256_optional(path: Path) -> str | None:
    if not path.is_file():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


def _occupy_dev_port(port: int) -> socket.socket | None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        listener.close()
        # Any existing listener is an equally strong occupied-port condition.
        # The isolated EXE still has to boot from its embedded Tauri assets.
        return None
    listener.listen(1)
    return listener


def _stop_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        for child in descendants:
            child.terminate()
        parent.terminate()
        _, alive = psutil.wait_procs([*descendants, parent], timeout=8)
        for item in alive:
            item.kill()
        psutil.wait_procs(alive, timeout=5)
    except psutil.Error:
        if process.poll() is None:
            process.kill()


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    if not executable.is_file():
        raise SystemExit(f"full app executable is missing: {executable}")
    isolation = Path(tempfile.mkdtemp(prefix="evidence-os-full-app-v003-"))
    probe_path = isolation / "frontend-backend-boot-probe.json"
    copied_executable = isolation / executable.name
    shutil.copy2(executable, copied_executable)
    listeners = [_occupy_dev_port(1430), _occupy_dev_port(4190)]
    isolated_appdata = isolation / "roaming"
    isolated_localappdata = isolation / "local"
    isolated_profile = isolation / "profile"
    for path in (isolated_appdata, isolated_localappdata, isolated_profile):
        path.mkdir(parents=True, exist_ok=True)
    host_appdata = Path(os.environ["APPDATA"]).resolve()
    host_workspace_registry = host_appdata / "EvidenceOS" / "workspace-roots.json"
    host_workspace_registry_before = _sha256_optional(host_workspace_registry)
    environment = os.environ.copy()
    environment.update(
        {
            "APPDATA": str(isolated_appdata),
            "LOCALAPPDATA": str(isolated_localappdata),
            "USERPROFILE": str(isolated_profile),
            "EVIDENCE_OS_BOOT_PROBE_PATH": str(probe_path),
            "EVIDENCE_OS_WORKSPACE": str(isolation / "runtime-workspace"),
            "EVIDENCE_OS_REPO_ROOT": str(isolation / "missing-repository"),
            "EVIDENCE_OS_WORKER": str(isolation / "missing-worker.py"),
            "EVIDENCE_OS_PYTHON": str(isolation / "missing-python.exe"),
        }
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [str(copied_executable)],
            cwd=isolation,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not probe_path.is_file():
            if process.poll() is not None:
                raise AssertionError(f"full app exited before boot proof: {process.returncode}")
            time.sleep(0.25)
        assert probe_path.is_file(), "rendered frontend did not write the boot proof"
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        child_records = [
            {"pid": child.pid, "name": child.name(), "exe": child.exe()}
            for child in children
            if child.is_running()
        ]
        python_children = [
            child for child in child_records if "python" in child["name"].casefold()
        ]
        backend_pid = int(probe["backend_pid"])
        assert probe["schema"] == "T023_FULL_APP_BOOT_PROBE_V1", probe
        assert probe["frontend_contract"] == "T023_FULL_APP_FRONTEND_V2", probe
        assert probe["backend_contract"] == "T023_FULL_APP_BACKEND_V2", probe
        assert probe["backend_frozen"] is True, probe
        assert probe["backend_packaging_mode"] == "PYINSTALLER_EMBEDDED_WORKER", probe
        assert probe["workspace_required_for_probe"] is False, probe
        assert probe["recorded_by"] == "TAURI_RUST_HOST", probe
        assert probe["tauri_pid"] == process.pid, probe
        assert backend_pid in {child["pid"] for child in child_records}, child_records
        rendered_url = str(probe["rendered_frontend_url"])
        assert "127.0.0.1" not in rendered_url, rendered_url
        assert ":1430" not in rendered_url and ":4190" not in rendered_url, rendered_url
        assert not python_children, python_children
        host_workspace_registry_after = _sha256_optional(host_workspace_registry)
        assert host_workspace_registry_after == host_workspace_registry_before
        result = {
            "schema": "T023_FULL_APP_ISOLATED_RUNTIME_VALIDATION_V3",
            "status": "PASS",
            "candidate": str(executable),
            "isolated_copy": str(copied_executable),
            "production_frontend_boot_probe": "PASS",
            "embedded_backend_ipc": "PASS",
            "isolated_launch_without_dev_ports": "PASS",
            "occupied_dev_ports": [1430, 4190],
            "frontend_contract": probe["frontend_contract"],
            "backend_contract": probe["backend_contract"],
            "backend_frozen": probe["backend_frozen"],
            "backend_packaging_mode": probe["backend_packaging_mode"],
            "workspace_required_for_probe": probe["workspace_required_for_probe"],
            "probe_recorded_by": probe["recorded_by"],
            "frontend_url": rendered_url,
            "tauri_pid": process.pid,
            "backend_pid": backend_pid,
            "child_processes": child_records,
            "child_python_process_count": len(python_children),
            "invalid_external_overrides_ignored": True,
            "isolated_appdata": str(isolated_appdata),
            "isolated_localappdata": str(isolated_localappdata),
            "host_workspace_registry": str(host_workspace_registry),
            "host_workspace_registry_sha256_before": host_workspace_registry_before,
            "host_workspace_registry_sha256_after": host_workspace_registry_after,
            "host_workspace_registry_unchanged": True,
        }
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        return 0
    finally:
        if process is not None:
            _stop_tree(process)
        for listener in listeners:
            if listener is not None:
                listener.close()
        shutil.rmtree(isolation, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
