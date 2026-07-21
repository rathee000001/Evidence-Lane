from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlite_brain_builder.runtime.pipeline_state import (
    StatePersistenceError,
    _atomic_write_json,
    _exclusive_state_file_lock,
    _read_json_state,
)

try:
    import psutil as _psutil
except Exception:  # pragma: no cover - psutil remains an optional runtime dependency
    _psutil = None


PROCESS_REGISTRY_SCHEMA_VERSION = 1
GPU_ATTRIBUTION_UNAVAILABLE = "UNAVAILABLE_PROCESS_ATTRIBUTION"
GPU_ATTRIBUTION_AVAILABLE = "AVAILABLE_PROCESS_ATTRIBUTION"
_DEFAULT_PSUTIL = object()


def windows_gpu_pid_sampler(pids: Sequence[int]) -> Mapping[str, Any]:
    """Best-effort PID-attributed Windows/NVIDIA GPU utilization."""
    targets = {int(pid) for pid in pids if int(pid) > 0}
    if not targets:
        return {"gpu_attribution_status": GPU_ATTRIBUTION_UNAVAILABLE}
    hidden: dict[str, Any] = {}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        hidden = {"startupinfo": startup, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        utilization = 0.0
        memory_bytes = 0
        attributed = False
        try:
            pmon = subprocess.run(
                [nvidia, "pmon", "-c", "1", "-s", "um"], capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=3, stdin=subprocess.DEVNULL, **hidden,
            )
            for line in pmon.stdout.splitlines():
                fields = line.split()
                if len(fields) < 5 or not fields[1].isdigit() or int(fields[1]) not in targets:
                    continue
                attributed = True
                if fields[3].replace(".", "", 1).isdigit():
                    utilization += float(fields[3])
            memory = subprocess.run(
                [nvidia, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=3, stdin=subprocess.DEVNULL, **hidden,
            )
            for line in memory.stdout.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) in targets:
                    attributed = True
                    if parts[1].replace(".", "", 1).isdigit():
                        memory_bytes += int(float(parts[1]) * 1024 * 1024)
            if attributed:
                return {
                    "gpu_attribution_status": GPU_ATTRIBUTION_AVAILABLE,
                    "gpu_percent": min(100.0, utilization),
                    "gpu_memory_bytes": memory_bytes,
                }
        except Exception:
            pass
    typeperf = shutil.which("typeperf") if os.name == "nt" else None
    if typeperf:
        try:
            result = subprocess.run(
                [typeperf, r"\GPU Engine(*)\Utilization Percentage", "-sc", "1"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=4, stdin=subprocess.DEVNULL, **hidden,
            )
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            if len(lines) >= 2:
                headers = [item.strip('"') for item in lines[-2].split('","')]
                values = [item.strip('"') for item in lines[-1].split('","')]
                total = 0.0
                matched = False
                for header, value in zip(headers[1:], values[1:]):
                    match = re.search(r"pid_(\d+)", header, flags=re.IGNORECASE)
                    if match and int(match.group(1)) in targets:
                        try:
                            total += float(value)
                            matched = True
                        except ValueError:
                            continue
                if matched:
                    return {
                        "gpu_attribution_status": GPU_ATTRIBUTION_AVAILABLE,
                        "gpu_percent": min(100.0, total),
                        "gpu_memory_bytes": None,
                    }
        except Exception:
            pass
    return {"gpu_attribution_status": GPU_ATTRIBUTION_UNAVAILABLE}


class ProcessRegistryError(RuntimeError):
    """Raised when a process registration or persisted registry is invalid."""


class ProcessRole(str, Enum):
    TAURI_APP = "tauri_app"
    PYTHON_WORKER = "python_worker"
    CHILD = "child"
    GIT = "git"
    MMD_RENDERER = "mmd_renderer"
    PACKAGE_COMPILER = "package_compiler"


class GpuSampler(Protocol):
    def __call__(self, pids: Sequence[int]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    create_time: float | None
    parent_pid: int | None
    identity_status: str


def _utc_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _role_value(role: ProcessRole | str) -> str:
    try:
        return ProcessRole(role).value
    except ValueError as exc:
        raise ProcessRegistryError(f"PROCESS_ROLE_INVALID: {role}") from exc


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ProcessRegistry:
    """Persistent registry and process-tree metric sampler for Evidence OS.

    Metrics are derived only from registered roots and their descendants. The
    sampler never substitutes machine-wide CPU, RAM, disk, or GPU utilization.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        psutil_module: Any = _DEFAULT_PSUTIL,
        clock: Callable[[], float] = time.time,
        gpu_sampler: GpuSampler | None = None,
        raw_sample_seconds: float = 0.3,
        rolling_window_seconds: float = 5.0,
    ) -> None:
        self.state_path = Path(state_path).expanduser().resolve()
        self._psutil = _psutil if psutil_module is _DEFAULT_PSUTIL else psutil_module
        self._clock = clock
        self._gpu_sampler = gpu_sampler
        self._raw_sample_seconds = max(0.0, min(0.5, float(raw_sample_seconds))) if psutil_module is _DEFAULT_PSUTIL else 0.0
        self._rolling_window_seconds = max(3.0, min(5.0, float(rolling_window_seconds)))
        self._thread_lock = threading.RLock()

    def initialize(
        self,
        *,
        tauri_pid: int | None = None,
        python_worker_pid: int | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            existing = _read_json_state(self.state_path)
            state = self._new_state() if replace or existing is None else self._validated_state(existing)
            if tauri_pid is not None:
                self._upsert_process(state, tauri_pid, ProcessRole.TAURI_APP.value)
            if python_worker_pid is not None:
                self._upsert_process(state, python_worker_pid, ProcessRole.PYTHON_WORKER.value)
            self._refresh_derived_fields(state)
            self._touch(state)
            _atomic_write_json(self.state_path, state)
            return self._public_snapshot(state)

    def register(
        self,
        pid: int,
        role: ProcessRole | str,
        *,
        parent_pid: int | None = None,
        command: str = "",
        started_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            state = self._load_or_new()
            # A process registration is also a lifecycle boundary. Remove dead
            # or PID-reused historical records before exposing the new owner so
            # the native UI never presents an ever-growing legacy process list.
            self._prune_stale_unlocked(state)
            self._upsert_process(
                state,
                pid,
                _role_value(role),
                parent_pid=parent_pid,
                command=command,
                started_at=started_at,
                metadata=metadata,
            )
            self._refresh_derived_fields(state)
            self._touch(state)
            _atomic_write_json(self.state_path, state)
            return self._public_snapshot(state)

    def register_subprocess(
        self,
        process: Any,
        role: ProcessRole | str,
        *,
        parent_pid: int | None = None,
        command: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        pid = getattr(process, "pid", None)
        if pid is None:
            raise ProcessRegistryError("SUBPROCESS_PID_MISSING")
        return self.register(
            int(pid),
            role,
            parent_pid=parent_pid,
            command=command,
            metadata=metadata,
        )

    def unregister(self, pid: int) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            state = self._load_or_new()
            state["processes"].pop(str(int(pid)), None)
            self._refresh_derived_fields(state)
            self._touch(state)
            _atomic_write_json(self.state_path, state)
            return self._public_snapshot(state)

    def set_active_context(
        self,
        *,
        pipeline_id: str | None = None,
        stage_id: str | None = None,
        lane: str | None = None,
        file: str | None = None,
        command: str | None = None,
    ) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            state = self._load_or_new()
            if pipeline_id is not None:
                state["active_pipeline_id"] = str(pipeline_id)
            if stage_id is not None:
                state["active_stage_id"] = str(stage_id)
            if lane is not None:
                state["active_lane"] = str(lane)
            if file is not None:
                state["active_file"] = str(file)
            if command is not None:
                state["active_command"] = str(command)
            self._touch(state)
            _atomic_write_json(self.state_path, state)
            return self._public_snapshot(state)

    def snapshot(self, *, prune_stale: bool = False) -> dict[str, Any]:
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            state = self._load_or_new()
            if prune_stale:
                self._prune_stale_unlocked(state)
                self._discover_live_descendants_unlocked(state)
                self._refresh_derived_fields(state)
                self._touch(state)
                _atomic_write_json(self.state_path, state)
            return self._public_snapshot(state)

    def read_only_snapshot(self) -> dict[str, Any]:
        """Read the last atomic registry state without creating locks or files."""
        with self._thread_lock:
            state = _read_json_state(self.state_path)
            return self._public_snapshot(self._new_state() if state is None else self._validated_state(state))

    def read_last_metrics(self) -> dict[str, Any]:
        state = self.read_only_snapshot()
        metrics = state.get("last_metrics")
        if isinstance(metrics, dict):
            return dict(metrics)
        return self._unavailable_metrics(state, float(self._clock()), "METRICS_NOT_SAMPLED")

    def registered_pids(self) -> list[int]:
        snapshot = self.snapshot()
        return sorted(int(pid) for pid in snapshot["processes"])

    def prune_stale(self) -> dict[str, Any]:
        # Copy state under lock, then release the file lock before sampling.
        with self._thread_lock, _exclusive_state_file_lock(self.state_path):
            state = json.loads(json.dumps(self._load_or_new()))
        with self._thread_lock:
            self._prune_stale_unlocked(state)
            self._refresh_derived_fields(state)
            self._touch(state)
            _atomic_write_json(self.state_path, state)
            return self._public_snapshot(state)

    def sample_metrics(self, *, active_process_pid: int | None = None) -> dict[str, Any]:
        """Sample registered roots and descendants, persist I/O baselines and peaks."""

        with self._thread_lock:
            state = self._load_or_new()
            now = float(self._clock())
            if self._psutil is None:
                metrics = self._unavailable_metrics(state, now, "PSUTIL_UNAVAILABLE")
                with _exclusive_state_file_lock(self.state_path):
                    latest = self._load_or_new()
                    self._update_metrics_state(latest, metrics, baseline=None)
                    _atomic_write_json(self.state_path, latest)
                return {**metrics, **latest["metric_peaks"]}

            processes, unavailable_pids, registered_pids = self._registered_process_tree(state)
            for process in processes.values():
                try:
                    process.cpu_percent(interval=None)
                except Exception:
                    pass
            if processes and self._raw_sample_seconds:
                time.sleep(self._raw_sample_seconds)
            cumulative: dict[str, dict[str, float | int | None]] = {}
            cpu_raw = 0.0
            working_set = 0
            private_bytes = 0
            total_read = 0
            total_write = 0
            sampled_pids: list[int] = []

            for pid, process in sorted(processes.items()):
                try:
                    cpu_raw += max(0.0, _safe_number(process.cpu_percent(interval=None)))
                    memory = process.memory_info()
                    rss = max(0, int(getattr(memory, "rss", 0) or 0))
                    working_set += rss
                    private_value = rss
                    try:
                        full = process.memory_full_info()
                        private_value = int(
                            getattr(full, "private", getattr(full, "uss", rss)) or rss
                        )
                    except Exception:
                        pass
                    private_bytes += max(0, private_value)

                    read_bytes = 0
                    write_bytes = 0
                    try:
                        io = process.io_counters()
                        read_bytes = max(0, int(getattr(io, "read_bytes", 0) or 0))
                        write_bytes = max(0, int(getattr(io, "write_bytes", 0) or 0))
                    except Exception:
                        pass
                    total_read += read_bytes
                    total_write += write_bytes
                    create_time = self._safe_create_time(process)
                    cumulative[str(pid)] = {
                        "create_time": create_time,
                        "read_bytes": read_bytes,
                        "write_bytes": write_bytes,
                    }
                    sampled_pids.append(pid)
                except Exception:
                    unavailable_pids.add(pid)

            logical_cpus = self._logical_cpu_count()
            cpu_percent = round(max(0.0, min(100.0, cpu_raw / logical_cpus)), 3)
            read_rate, write_rate = self._io_rates(
                state.get("metric_baseline"), now, cumulative
            )
            gpu = self._gpu_metrics(sampled_pids)
            requested_pid = max(0, int(active_process_pid or 0))
            resolved_active_pid = requested_pid if requested_pid in sampled_pids else 0
            if not resolved_active_pid:
                python_worker_pid = max(0, int(state.get("python_worker_pid") or 0))
                resolved_active_pid = python_worker_pid if python_worker_pid in sampled_pids else 0
            if not resolved_active_pid and sampled_pids:
                resolved_active_pid = sorted(sampled_pids)[0]
            metrics_status = "OK"
            if unavailable_pids:
                metrics_status = "PARTIAL_PROCESS_UNAVAILABLE" if sampled_pids else "NO_REGISTERED_PROCESS_AVAILABLE"
            elif not sampled_pids:
                metrics_status = "NO_REGISTERED_PROCESS_AVAILABLE"

            metrics = {
                "metric_scope": "EVIDENCE_LANE_APP_PROCESS_TREE",
                "attribution_basis": "REGISTERED_TAURI_ROOT_AND_DESCENDANTS",
                "machine_wide_values_used": False,
                "metrics_status": metrics_status,
                "sampled_at": _utc_iso(now),
                "sampled_at_epoch": now,
                "registered_pids": registered_pids,
                "sampled_pids": sorted(sampled_pids),
                "unavailable_pids": sorted(unavailable_pids),
                "registered_process_count": len(registered_pids),
                "sampled_process_count": len(sampled_pids),
                "active_process_pid": resolved_active_pid,
                "resource_attribution": self._resource_attribution(
                    state,
                    active_process_pid=resolved_active_pid,
                    sampled_pids=sampled_pids,
                    metrics_status=metrics_status,
                    gpu_status=str(gpu.get("gpu_attribution_status") or GPU_ATTRIBUTION_UNAVAILABLE),
                ),
                "logical_cpu_count": logical_cpus,
                "raw_sample_seconds": self._raw_sample_seconds,
                "cpu_percent": cpu_percent,
                "cpu_percent_raw_sum": round(cpu_raw, 3),
                "working_set_bytes": working_set,
                "private_bytes": private_bytes,
                "system_total_memory_bytes": self._system_total_memory_bytes(),
                "disk_read_bytes": total_read,
                "disk_write_bytes": total_write,
                "disk_read_bytes_per_second": read_rate,
                "disk_write_bytes_per_second": write_rate,
                "disk_total_bytes_per_second": (
                    None if read_rate is None and write_rate is None
                    else round(float(read_rate or 0.0) + float(write_rate or 0.0), 3)
                ),
                **gpu,
                **self._active_context(state),
            }
            total_memory = int(metrics["system_total_memory_bytes"] or 0)
            metrics["working_set_percent_of_system"] = round((working_set / total_memory) * 100.0, 3) if total_memory else None
            metrics = self._rolling_metrics(state, metrics, now)
            baseline = {
                "sampled_at_epoch": now,
                "processes": cumulative,
            }
            with _exclusive_state_file_lock(self.state_path):
                latest = self._load_or_new()
                latest["processes"] = state.get("processes", latest.get("processes", {}))
                latest["child_pids"] = state.get("child_pids", latest.get("child_pids", []))
                latest["metric_samples"] = state.get("metric_samples", [])
                self._update_metrics_state(latest, metrics, baseline=baseline)
                _atomic_write_json(self.state_path, latest)
            return {**metrics, **latest["metric_peaks"]}

    def _new_state(self) -> dict[str, Any]:
        now = float(self._clock())
        return {
            "schema_version": PROCESS_REGISTRY_SCHEMA_VERSION,
            "started_at": _utc_iso(now),
            "updated_at": _utc_iso(now),
            "tauri_pid": None,
            "python_worker_pid": None,
            "child_pids": [],
            "git_pid": None,
            "mmd_renderer_pid": None,
            "package_compiler_pid": None,
            "active_pipeline_id": "",
            "active_stage_id": "",
            "active_lane": "",
            "active_file": "",
            "active_command": "",
            "processes": {},
            "metric_baseline": None,
            "metric_samples": [],
            "metric_peaks": {
                "peak_cpu_percent": 0.0,
                "peak_working_set_bytes": 0,
                "peak_private_bytes": 0,
                "peak_working_set_percent_of_system": 0.0,
                "peak_gpu_percent": 0.0,
                "peak_gpu_memory_bytes": 0,
                "peak_disk_read_bytes_per_second": 0.0,
                "peak_disk_write_bytes_per_second": 0.0,
                "peak_disk_total_bytes_per_second": 0.0,
            },
            "last_metrics": None,
        }

    def _load_or_new(self) -> dict[str, Any]:
        payload = _read_json_state(self.state_path)
        return self._new_state() if payload is None else self._validated_state(payload)

    @staticmethod
    def _validated_state(payload: Mapping[str, Any]) -> dict[str, Any]:
        if int(payload.get("schema_version") or 0) != PROCESS_REGISTRY_SCHEMA_VERSION:
            raise ProcessRegistryError("PROCESS_REGISTRY_SCHEMA_VERSION_UNSUPPORTED")
        if not isinstance(payload.get("processes"), dict):
            raise ProcessRegistryError("PROCESS_REGISTRY_PROCESSES_INVALID")
        # JSON round-trip returns a detached mutable copy and validates serializability.
        try:
            return json.loads(json.dumps(payload))
        except (TypeError, ValueError) as exc:
            raise StatePersistenceError(f"PROCESS_REGISTRY_STATE_INVALID: {exc}") from exc

    def _upsert_process(
        self,
        state: dict[str, Any],
        pid: int,
        role: str,
        *,
        parent_pid: int | None = None,
        command: str = "",
        started_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        pid = int(pid)
        if pid <= 0:
            raise ProcessRegistryError(f"PROCESS_PID_INVALID: {pid}")
        identity = self._capture_identity(pid)
        effective_parent = int(parent_pid) if parent_pid is not None else identity.parent_pid
        now = float(self._clock())
        existing = state["processes"].get(str(pid), {})
        details = self._process_details(pid)
        state["processes"][str(pid)] = {
            "pid": pid,
            "role": role,
            "parent_pid": effective_parent,
            "command": str(command or details.get("command_line") or existing.get("command") or ""),
            "executable_name": str(details.get("executable_name") or existing.get("executable_name") or ""),
            "executable_path": str(details.get("executable_path") or existing.get("executable_path") or ""),
            "executable_sha256": str(details.get("executable_sha256") or existing.get("executable_sha256") or ""),
            "process_status": str(details.get("process_status") or existing.get("process_status") or "UNKNOWN"),
            "process_started_at": started_at
            or existing.get("process_started_at")
            or (_utc_iso(identity.create_time) if identity.create_time is not None else _utc_iso(now)),
            "registered_at": existing.get("registered_at") or _utc_iso(now),
            "updated_at": _utc_iso(now),
            "create_time": identity.create_time,
            "identity_status": identity.identity_status,
            "metadata": dict(metadata or existing.get("metadata") or {}),
        }

    def _process_details(self, pid: int) -> dict[str, str]:
        if self._psutil is None:
            return {}
        try:
            process = self._psutil.Process(int(pid))
        except Exception:
            return {}
        executable_path = ""
        executable_name = ""
        process_status = ""
        command_line = ""
        try:
            executable_path = str(process.exe() or "")
        except Exception:
            pass
        try:
            executable_name = str(process.name() or "")
        except Exception:
            executable_name = Path(executable_path).name if executable_path else ""
        try:
            process_status = str(process.status() or "")
        except Exception:
            pass
        try:
            command_line = " ".join(str(part) for part in process.cmdline())
        except Exception:
            pass
        executable_sha256 = ""
        try:
            executable = Path(executable_path)
            if executable.is_file():
                digest = hashlib.sha256()
                with executable.open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                executable_sha256 = digest.hexdigest().upper()
        except OSError:
            pass
        return {
            "command_line": command_line,
            "executable_name": executable_name,
            "executable_path": executable_path,
            "executable_sha256": executable_sha256,
            "process_status": process_status,
        }

    def _capture_identity(self, pid: int) -> ProcessIdentity:
        if self._psutil is None:
            return ProcessIdentity(None, None, "PSUTIL_UNAVAILABLE")
        try:
            process = self._psutil.Process(pid)
            return ProcessIdentity(
                self._safe_create_time(process),
                int(process.ppid()) if hasattr(process, "ppid") else None,
                "VERIFIED",
            )
        except Exception:
            return ProcessIdentity(None, None, "UNVERIFIED_OR_EXITED")

    @staticmethod
    def _safe_create_time(process: Any) -> float | None:
        try:
            return float(process.create_time())
        except Exception:
            return None

    def _refresh_derived_fields(self, state: dict[str, Any]) -> None:
        records = list(state["processes"].values())

        def latest(role: ProcessRole) -> int | None:
            matches = [row for row in records if row.get("role") == role.value]
            if not matches:
                return None
            matches.sort(key=lambda row: str(row.get("registered_at") or ""))
            return int(matches[-1]["pid"])

        state["tauri_pid"] = latest(ProcessRole.TAURI_APP)
        state["python_worker_pid"] = latest(ProcessRole.PYTHON_WORKER)
        state["git_pid"] = latest(ProcessRole.GIT)
        state["mmd_renderer_pid"] = latest(ProcessRole.MMD_RENDERER)
        state["package_compiler_pid"] = latest(ProcessRole.PACKAGE_COMPILER)
        child_roles = {
            ProcessRole.CHILD.value,
            ProcessRole.GIT.value,
            ProcessRole.MMD_RENDERER.value,
            ProcessRole.PACKAGE_COMPILER.value,
        }
        state["child_pids"] = sorted(
            int(row["pid"]) for row in records if row.get("role") in child_roles
        )

    def _prune_stale_unlocked(self, state: dict[str, Any]) -> None:
        if self._psutil is None:
            return
        stale: list[str] = []
        for key, record in state["processes"].items():
            try:
                process = self._psutil.Process(int(record["pid"]))
                expected = record.get("create_time")
                actual = self._safe_create_time(process)
                if expected is not None and actual is not None and abs(float(expected) - actual) > 0.01:
                    stale.append(key)
            except Exception:
                stale.append(key)
        for key in stale:
            state["processes"].pop(key, None)

    def _discover_live_descendants_unlocked(self, state: dict[str, Any]) -> None:
        """Expose every currently live descendant of registered app roots."""
        if self._psutil is None:
            return
        root_records = list(state["processes"].values())
        for record in root_records:
            try:
                root = self._psutil.Process(int(record["pid"]))
                descendants = root.children(recursive=True)
            except Exception:
                continue
            for child in descendants:
                pid = int(child.pid)
                if str(pid) in state["processes"]:
                    continue
                try:
                    command_parts = list(child.cmdline()) if hasattr(child, "cmdline") else []
                except Exception:
                    command_parts = []
                command = " ".join(str(part) for part in command_parts)
                executable = Path(command_parts[0]).name.casefold() if command_parts else ""
                command_folded = command.casefold()
                if executable.startswith("git"):
                    role = ProcessRole.GIT.value
                elif "mmdc" in executable or "mermaid" in command_folded:
                    role = ProcessRole.MMD_RENDERER.value
                elif any(token in command_folded for token in ("package_compil", "compile_package", "package.render")):
                    role = ProcessRole.PACKAGE_COMPILER.value
                else:
                    role = ProcessRole.CHILD.value
                self._upsert_process(
                    state,
                    pid,
                    role,
                    parent_pid=int(child.ppid()) if hasattr(child, "ppid") else int(record["pid"]),
                    command=command,
                    metadata={"discovered_from_process_tree": True},
                )

    def _registered_process_tree(
        self, state: Mapping[str, Any]
    ) -> tuple[dict[int, Any], set[int], list[int]]:
        registered_pids = sorted(int(pid) for pid in state["processes"])
        processes: dict[int, Any] = {}
        unavailable: set[int] = set()
        for pid in registered_pids:
            record = state["processes"][str(pid)]
            try:
                root = self._psutil.Process(pid)
                expected = record.get("create_time")
                actual = self._safe_create_time(root)
                if expected is not None and actual is not None and abs(float(expected) - actual) > 0.01:
                    unavailable.add(pid)
                    continue
                processes[pid] = root
                try:
                    descendants = root.children(recursive=True)
                except Exception:
                    descendants = []
                for child in descendants:
                    child_pid = int(child.pid)
                    processes.setdefault(child_pid, child)
            except Exception:
                unavailable.add(pid)
        return processes, unavailable, registered_pids

    def _logical_cpu_count(self) -> int:
        try:
            return max(1, int(self._psutil.cpu_count(logical=True) or 1))
        except Exception:
            return max(1, int(os.cpu_count() or 1))

    def _system_total_memory_bytes(self) -> int | None:
        try:
            return int(self._psutil.virtual_memory().total)
        except Exception:
            return None

    def _rolling_metrics(self, state: dict[str, Any], metrics: dict[str, Any], now: float) -> dict[str, Any]:
        numeric = (
            "cpu_percent", "working_set_bytes", "private_bytes",
            "working_set_percent_of_system", "disk_read_bytes_per_second",
            "disk_write_bytes_per_second", "disk_total_bytes_per_second",
            "gpu_percent", "gpu_memory_bytes",
        )
        samples = [
            item for item in (state.get("metric_samples") or [])
            if now - float(item.get("sampled_at_epoch") or 0.0) <= self._rolling_window_seconds
        ]
        samples.append({"sampled_at_epoch": now, **{key: metrics.get(key) for key in numeric}})
        state["metric_samples"] = samples[-32:]
        result = dict(metrics)
        result["rolling_window_seconds"] = self._rolling_window_seconds
        result["rolling_sample_count"] = len(samples)
        for key in numeric:
            values = [float(item[key]) for item in samples if item.get(key) is not None]
            if values:
                result[key + "_raw"] = metrics.get(key)
                average = sum(values) / len(values)
                result[key] = int(round(average)) if key.endswith("_bytes") and not key.endswith("per_second") else round(average, 3)
        return result

    @staticmethod
    def _io_rates(
        previous: Mapping[str, Any] | None,
        now: float,
        current: Mapping[str, Mapping[str, float | int | None]],
    ) -> tuple[float | None, float | None]:
        if not previous:
            return None, None
        elapsed = now - _safe_number(previous.get("sampled_at_epoch"), now)
        if elapsed <= 0.0:
            return None, None
        previous_processes = previous.get("processes") or {}
        read_delta = 0
        write_delta = 0
        matched = 0
        for pid, current_row in current.items():
            old = previous_processes.get(pid)
            if not isinstance(old, Mapping):
                continue
            old_created = old.get("create_time")
            current_created = current_row.get("create_time")
            if (
                old_created is not None
                and current_created is not None
                and abs(float(old_created) - float(current_created)) > 0.01
            ):
                continue
            read_delta += max(0, int(current_row.get("read_bytes") or 0) - int(old.get("read_bytes") or 0))
            write_delta += max(0, int(current_row.get("write_bytes") or 0) - int(old.get("write_bytes") or 0))
            matched += 1
        if matched == 0:
            return None, None
        return round(read_delta / elapsed, 3), round(write_delta / elapsed, 3)

    def _gpu_metrics(self, sampled_pids: Sequence[int]) -> dict[str, Any]:
        unavailable = {
            "gpu_attribution_status": GPU_ATTRIBUTION_UNAVAILABLE,
            "gpu_percent": None,
            "gpu_memory_bytes": None,
        }
        if self._gpu_sampler is None or not sampled_pids:
            return unavailable
        try:
            result = dict(self._gpu_sampler(tuple(sorted(sampled_pids))))
        except Exception:
            return unavailable
        status = str(result.get("gpu_attribution_status") or GPU_ATTRIBUTION_AVAILABLE)
        if status != GPU_ATTRIBUTION_AVAILABLE:
            return unavailable
        percent = result.get("gpu_percent")
        memory = result.get("gpu_memory_bytes")
        return {
            "gpu_attribution_status": GPU_ATTRIBUTION_AVAILABLE,
            "gpu_percent": None if percent is None else max(0.0, min(100.0, float(percent))),
            "gpu_memory_bytes": None if memory is None else max(0, int(memory)),
        }

    def _unavailable_metrics(
        self, state: Mapping[str, Any], now: float, status: str
    ) -> dict[str, Any]:
        registered = sorted(int(pid) for pid in state["processes"])
        return {
            "metric_scope": "EVIDENCE_LANE_APP_PROCESS_TREE",
            "attribution_basis": "REGISTERED_TAURI_ROOT_AND_DESCENDANTS",
            "machine_wide_values_used": False,
            "metrics_status": status,
            "sampled_at": _utc_iso(now),
            "sampled_at_epoch": now,
            "registered_pids": registered,
            "sampled_pids": [],
            "unavailable_pids": registered,
            "registered_process_count": len(registered),
            "sampled_process_count": 0,
            "active_process_pid": 0,
            "resource_attribution": self._resource_attribution(
                state,
                active_process_pid=0,
                sampled_pids=[],
                metrics_status=status,
                gpu_status=GPU_ATTRIBUTION_UNAVAILABLE,
            ),
            "logical_cpu_count": max(1, int(os.cpu_count() or 1)),
            "raw_sample_seconds": self._raw_sample_seconds,
            "cpu_percent": None,
            "cpu_percent_raw_sum": None,
            "working_set_bytes": None,
            "private_bytes": None,
            "system_total_memory_bytes": None,
            "working_set_percent_of_system": None,
            "disk_read_bytes": None,
            "disk_write_bytes": None,
            "disk_read_bytes_per_second": None,
            "disk_write_bytes_per_second": None,
            "disk_total_bytes_per_second": None,
            "gpu_attribution_status": GPU_ATTRIBUTION_UNAVAILABLE,
            "gpu_percent": None,
            "gpu_memory_bytes": None,
            **self._active_context(state),
        }

    @staticmethod
    def _resource_attribution(
        state: Mapping[str, Any],
        *,
        active_process_pid: int,
        sampled_pids: Sequence[int],
        metrics_status: str,
        gpu_status: str,
    ) -> dict[str, dict[str, Any]]:
        process_tree_ids = sorted({max(0, int(pid)) for pid in sampled_pids if int(pid) > 0})
        base = {
            "process_pid": max(0, int(active_process_pid)),
            "process_tree_ids": process_tree_ids,
            "task": str(state.get("active_command") or "Idle"),
            "stage_id": str(state.get("active_stage_id") or "idle"),
            "pipeline_id": str(state.get("active_pipeline_id") or ""),
            "attribution_status": metrics_status if process_tree_ids else "IDLE",
        }
        return {
            "cpu": dict(base),
            "ram": dict(base),
            "storage": dict(base),
            "gpu": {**base, "attribution_status": gpu_status if process_tree_ids else "IDLE"},
        }

    def _update_metrics_state(
        self,
        state: dict[str, Any],
        metrics: Mapping[str, Any],
        *,
        baseline: Mapping[str, Any] | None,
    ) -> None:
        peaks = state.get("metric_peaks") or {}

        def peak(name: str, current: Any) -> None:
            if current is None:
                return
            peaks[name] = max(_safe_number(peaks.get(name)), _safe_number(current))

        # Peak retention always uses the instantaneous sample, never the rolling
        # display average.
        peak("peak_cpu_percent", metrics.get("cpu_percent_raw", metrics.get("cpu_percent")))
        peak("peak_working_set_bytes", metrics.get("working_set_bytes_raw", metrics.get("working_set_bytes")))
        peak("peak_private_bytes", metrics.get("private_bytes_raw", metrics.get("private_bytes")))
        peak(
            "peak_working_set_percent_of_system",
            metrics.get("working_set_percent_of_system_raw", metrics.get("working_set_percent_of_system")),
        )
        peak("peak_gpu_percent", metrics.get("gpu_percent_raw", metrics.get("gpu_percent")))
        peak("peak_gpu_memory_bytes", metrics.get("gpu_memory_bytes_raw", metrics.get("gpu_memory_bytes")))
        peak(
            "peak_disk_read_bytes_per_second",
            metrics.get("disk_read_bytes_per_second_raw", metrics.get("disk_read_bytes_per_second")),
        )
        peak(
            "peak_disk_write_bytes_per_second",
            metrics.get("disk_write_bytes_per_second_raw", metrics.get("disk_write_bytes_per_second")),
        )
        peak(
            "peak_disk_total_bytes_per_second",
            metrics.get("disk_total_bytes_per_second_raw", metrics.get("disk_total_bytes_per_second")),
        )
        peaks["peak_working_set_bytes"] = int(peaks.get("peak_working_set_bytes") or 0)
        peaks["peak_private_bytes"] = int(peaks.get("peak_private_bytes") or 0)
        peaks["peak_gpu_memory_bytes"] = int(peaks.get("peak_gpu_memory_bytes") or 0)
        state["metric_peaks"] = peaks
        if baseline is not None:
            state["metric_baseline"] = dict(baseline)
        state["last_metrics"] = dict(metrics)
        self._touch(state)

    @staticmethod
    def _active_context(state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "active_pipeline_id": str(state.get("active_pipeline_id") or ""),
            "active_stage_id": str(state.get("active_stage_id") or ""),
            "active_lane": str(state.get("active_lane") or ""),
            "active_file": str(state.get("active_file") or ""),
            "active_command": str(state.get("active_command") or ""),
            "registry_started_at": str(state.get("started_at") or ""),
        }

    def _touch(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _utc_iso(float(self._clock()))

    @staticmethod
    def _public_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = json.loads(json.dumps(state))
        snapshot["processes"] = {
            str(pid): record for pid, record in sorted(snapshot["processes"].items(), key=lambda item: int(item[0]))
        }
        return snapshot
