from __future__ import annotations

import ctypes
import csv
import os
import shutil
import subprocess
import threading
import time
from io import StringIO
from pathlib import Path

try:
    import psutil as _psutil
except Exception:  # pragma: no cover - optional runtime dependency
    _psutil = None


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


def _process_memory_mb() -> float | None:
    if os.name != "nt":
        return None
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    if not ok:
        return None
    return counters.WorkingSetSize / (1024 * 1024)


def _system_memory_percent() -> float | None:
    if os.name != "nt":
        return None
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    if not ok:
        return None
    return float(status.dwMemoryLoad)


def _process_io_bytes() -> tuple[int, int] | None:
    if os.name != "nt":
        return None
    counters = _IO_COUNTERS()
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    ok = ctypes.windll.kernel32.GetProcessIoCounters(handle, ctypes.byref(counters))
    if not ok:
        return None
    return int(counters.ReadTransferCount), int(counters.WriteTransferCount)


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": _hidden_startup_info(),
    }


def _hidden_startup_info():
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return info


def _gpu_percent() -> float | None:
    commands = []
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        commands.append([nvidia, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"])
    if os.name == "nt" and shutil.which("typeperf"):
        commands.append(["typeperf", r"\GPU Engine(*)\Utilization Percentage", "-sc", "1"])

    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=4,
                stdin=subprocess.DEVNULL,
                **_hidden_subprocess_kwargs(),
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        if Path(command[0]).name.lower().startswith("nvidia-smi"):
            values = [float(line.strip()) for line in result.stdout.splitlines() if line.strip().replace(".", "", 1).isdigit()]
            if values:
                return max(0.0, min(100.0, max(values)))
            continue
        try:
            rows = list(csv.reader(StringIO(result.stdout)))
            values = [float(value) for value in rows[-1][1:] if value and value != "PDH-CSV 4.0"]
            if values:
                return max(0.0, min(100.0, max(values)))
        except Exception:
            continue
    return None


def _disk_sample():
    if _psutil is None:
        return None
    try:
        return _psutil.disk_io_counters()
    except Exception:
        return None


class ProcessMetricSampler:
    def __init__(self):
        self._last_wall = time.perf_counter()
        self._last_cpu = time.process_time()
        self._last_io = _process_io_bytes()
        self._last_disk_sample = _disk_sample()
        self._last_disk_sample_at = time.perf_counter()
        self._gpu_lock = threading.RLock()
        self._gpu_refresh_in_flight = False
        self._last_gpu_sample_at = 0.0
        self._last_gpu_percent: float | None = None

        # Prime psutil's non-blocking CPU counter once.  Later snapshots then
        # report the interval since this baseline instead of sleeping inside
        # the IPC request that drives the live dashboard.
        if _psutil is not None:
            try:
                _psutil.cpu_percent(interval=None)
            except Exception:
                pass

    def snapshot(self, workspace_dir: str | Path | None = None) -> dict[str, str | float | None]:
        sample_started = time.perf_counter()
        if _psutil is not None:
            try:
                cpu_percent = float(_psutil.cpu_percent(interval=None))
            except Exception:
                cpu_percent = 0.0
        else:
            elapsed = max(time.perf_counter() - self._last_wall, 0.001)
            cpu_count = max(os.cpu_count() or 1, 1)
            now_cpu = time.process_time()
            cpu_percent = ((now_cpu - self._last_cpu) / elapsed) * 100.0 / cpu_count
            self._last_cpu = now_cpu
            self._last_wall = time.perf_counter()

        disk_start = self._last_disk_sample
        disk_started_at = self._last_disk_sample_at
        disk_end = _disk_sample()
        disk_sampled_at = time.perf_counter()
        sample_elapsed = max(disk_sampled_at - disk_started_at, 0.001)
        self._last_disk_sample = disk_end
        self._last_disk_sample_at = disk_sampled_at

        cpu_percent = max(0.0, min(100.0, cpu_percent))
        if _psutil is not None:
            try:
                ram_percent = float(_psutil.virtual_memory().percent)
            except Exception:
                ram_percent = _system_memory_percent()
        else:
            ram_percent = _system_memory_percent()

        disk_capacity_percent = None
        if workspace_dir:
            try:
                usage = shutil.disk_usage(str(Path(workspace_dir).expanduser()))
                disk_capacity_percent = (usage.used / usage.total) * 100.0 if usage.total else None
            except Exception:
                disk_capacity_percent = None

        read_rate = None
        write_rate = None
        disk_active_percent = None
        if disk_start and disk_end:
            read_rate = max(0.0, (disk_end.read_bytes - disk_start.read_bytes) / sample_elapsed / (1024 * 1024))
            write_rate = max(0.0, (disk_end.write_bytes - disk_start.write_bytes) / sample_elapsed / (1024 * 1024))
            if hasattr(disk_start, "busy_time") and hasattr(disk_end, "busy_time"):
                busy_delta_ms = max(0.0, float(disk_end.busy_time - disk_start.busy_time))
                disk_active_percent = min(100.0, busy_delta_ms / (sample_elapsed * 10.0))
            elif all(hasattr(sample, "read_time") and hasattr(sample, "write_time") for sample in (disk_start, disk_end)):
                active_delta_ms = max(
                    0.0,
                    float((disk_end.read_time - disk_start.read_time) + (disk_end.write_time - disk_start.write_time)),
                )
                disk_active_percent = min(100.0, active_delta_ms / (sample_elapsed * 10.0))

        # Windows GPU attribution may require typeperf or nvidia-smi.  That
        # external probe must never block the IPC heartbeat.  Refresh it on one
        # daemon thread and return the last completed sample immediately.
        gpu_percent = self._request_gpu_refresh(time.monotonic())
        sample_latency_ms = max(0.0, (time.perf_counter() - sample_started) * 1000.0)

        return {
            "CPU": f"{cpu_percent:.1f}% system",
            "GPU": f"{gpu_percent:.1f}% system" if gpu_percent is not None else "unavailable",
            "RAM": f"{ram_percent:.0f}% system" if ram_percent is not None else "unavailable",
            "RAM_SYSTEM": f"{ram_percent:.0f}% system" if ram_percent is not None else "unavailable",
            "SSD/HDD": f"{disk_active_percent:.1f}% active" if disk_active_percent is not None else "unavailable",
            "DISK_CAPACITY": f"{disk_capacity_percent:.0f}% used" if disk_capacity_percent is not None else "unavailable",
            "IO": self._format_io(read_rate, write_rate),
            # Stable numeric telemetry for presentation adapters.  The legacy
            # display strings above remain part of the public IPC contract;
            # consumers must not need to parse those strings to drive gauges.
            "CPU_PERCENT": round(cpu_percent, 3),
            "GPU_PERCENT": round(gpu_percent, 3) if gpu_percent is not None else None,
            "RAM_PERCENT": round(ram_percent, 3) if ram_percent is not None else None,
            "DISK_ACTIVE_PERCENT": round(disk_active_percent, 3) if disk_active_percent is not None else None,
            "DISK_CAPACITY_PERCENT": round(disk_capacity_percent, 3) if disk_capacity_percent is not None else None,
            "READ_MBPS": round(read_rate, 3) if read_rate is not None else None,
            "WRITE_MBPS": round(write_rate, 3) if write_rate is not None else None,
            "SAMPLED_AT_EPOCH": round(time.time(), 3),
            "SAMPLE_LATENCY_MS": round(sample_latency_ms, 3),
        }

    def _request_gpu_refresh(self, now: float) -> float | None:
        with self._gpu_lock:
            cached = self._last_gpu_percent
            is_fresh = self._last_gpu_sample_at > 0 and now - self._last_gpu_sample_at < 5.0
            if is_fresh or self._gpu_refresh_in_flight:
                return cached
            self._gpu_refresh_in_flight = True

        try:
            threading.Thread(
                target=self._refresh_gpu,
                name="evidenceos-machine-gpu-sampler",
                daemon=True,
            ).start()
        except Exception:
            with self._gpu_lock:
                self._gpu_refresh_in_flight = False
        with self._gpu_lock:
            return self._last_gpu_percent

    def _refresh_gpu(self) -> None:
        sampled = _gpu_percent()
        with self._gpu_lock:
            self._last_gpu_percent = sampled
            self._last_gpu_sample_at = time.monotonic()
            self._gpu_refresh_in_flight = False

    @staticmethod
    def _format_io(read_rate: float | None, write_rate: float | None) -> str:
        if read_rate is None or write_rate is None:
            return "unavailable"
        return f"R {read_rate:.1f} / W {write_rate:.1f} MB/s"
