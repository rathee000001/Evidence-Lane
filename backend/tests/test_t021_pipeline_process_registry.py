from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from sqlite_brain_builder.runtime.pipeline_state import (
    PIPELINE_STAGES,
    PipelineStateError,
    PipelineStateStore,
)
from sqlite_brain_builder.runtime.process_registry import (
    GPU_ATTRIBUTION_AVAILABLE,
    GPU_ATTRIBUTION_UNAVAILABLE,
    ProcessRegistry,
    ProcessRole,
)


class FakeClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_t021_pipeline_has_exact_authoritative_ten_stages() -> None:
    assert [(stage.stage_id, stage.stage_name, stage.stage_order) for stage in PIPELINE_STAGES] == [
        ("source_validation_registration", "Source validation and registration", 1),
        ("sqlite_project_router_sector_creation", "SQLite project/router/sector creation", 2),
        ("per_lane_parsing_chunking_indexing", "Per-lane parsing/chunking/indexing", 3),
        ("pointer_router_hash_finalization", "Pointer/router/hash finalization", 4),
        ("project_mmd_generation", "Project MMD generation", 5),
        ("svg_png_rendering", "SVG/PNG rendering", 6),
        ("chatgpt_package_compilation", "ChatGPT package compilation", 7),
        ("gemini_exact10_compilation", "Gemini provider-readable package compilation", 8),
        ("package_hash_validation", "Package/hash validation", 9),
        ("immutable_version_capture", "Immutable version capture", 10),
    ]


def test_pipeline_events_persist_all_fields_and_enforce_monotonic_progress(tmp_path: Path) -> None:
    clock = FakeClock()
    state_path = tmp_path / "pipeline-state.json"
    store = PipelineStateStore(state_path, clock=clock, pid_provider=lambda: 4242)

    event = store.start(
        pipeline_id="pipeline_test",
        request_id="request_test",
        brain_name="Telemetry Brain",
        workspace_dir=str(tmp_path),
        files_total=8,
        active_lane="docs",
        active_source_id="source_docs",
        active_sector_id="docs",
        active_file="source.docx",
        active_command="brain.buildAll",
    )
    required_fields = {
        "pipeline_id",
        "request_id",
        "brain_name",
        "workspace_dir",
        "stage_id",
        "stage_name",
        "stage_order",
        "stage_count",
        "stage_percent",
        "global_percent",
        "running_count",
        "queued_count",
        "completed_count",
        "active_lane",
        "active_source_id",
        "active_sector_id",
        "active_file",
        "files_done",
        "files_total",
        "rows_written",
        "chunks_written",
        "elapsed_seconds",
        "eta_seconds",
        "process_pid",
        "status",
        "error_code",
    }
    assert required_fields <= event.keys()
    assert event["stage_count"] == 10
    assert (event["running_count"], event["queued_count"], event["completed_count"]) == (1, 9, 0)
    assert event["process_pid"] == 4242
    assert event["request_id"] == "request_test"
    assert event["brain_name"] == "Telemetry Brain"
    assert event["active_source_id"] == "source_docs"
    assert event["active_sector_id"] == "docs"

    clock.advance(10)
    event = store.update(
        stage=1,
        stage_percent=25,
        files_done=2,
        rows_written=100,
        chunks_written=10,
    )
    assert event["stage_percent"] == 25.0
    assert event["global_percent"] == 2.5
    assert event["eta_seconds"] == 390.0

    # Replayed or out-of-order progress cannot move a stage or cumulative counter backward.
    event = store.update(stage=1, stage_percent=5, files_done=1, rows_written=50, chunks_written=4)
    assert event["stage_percent"] == 25.0
    assert event["files_done"] == 2
    assert event["rows_written"] == 100
    assert event["chunks_written"] == 10

    clock.advance(10)
    event = store.update(
        stage="per_lane_parsing_chunking_indexing",
        stage_percent=40,
        files_done=5,
        active_file="table.xlsx",
        rows_written=600,
        chunks_written=80,
    )
    assert event["stage_order"] == 3
    assert event["stage_percent"] == 40.0
    assert event["global_percent"] == 24.0
    assert (event["running_count"], event["queued_count"], event["completed_count"]) == (1, 7, 2)

    with pytest.raises(PipelineStateError, match="PIPELINE_STAGE_REGRESSION"):
        store.update(stage=2, stage_percent=90)

    # A new store instance reads the atomically persisted state.
    assert PipelineStateStore(state_path, clock=clock).snapshot() == event
    on_disk = json.loads(state_path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["event"]["pipeline_id"] == "pipeline_test"

    clock.advance(10)
    completed = store.complete(files_done=8, rows_written=900, chunks_written=120)
    assert completed["stage_id"] == "immutable_version_capture"
    assert completed["stage_percent"] == 100.0
    assert completed["global_percent"] == 100.0
    assert completed["status"] == "completed"
    assert completed["eta_seconds"] == 0.0
    assert (completed["running_count"], completed["queued_count"], completed["completed_count"]) == (0, 0, 10)


def test_pipeline_failure_requires_and_persists_error_code(tmp_path: Path) -> None:
    store = PipelineStateStore(tmp_path / "pipeline.json")
    store.start(pipeline_id="pipeline_failure")

    with pytest.raises(PipelineStateError, match="PIPELINE_ERROR_CODE_REQUIRED"):
        store.update(status="failed")

    failed = store.fail("LANE_PARSE_FAILED", active_file="broken.pdf")
    assert failed["status"] == "failed"
    assert failed["error_code"] == "LANE_PARSE_FAILED"
    assert failed["active_file"] == "broken.pdf"
    assert failed["running_count"] == 0


def test_process_registry_persists_roles_context_and_honest_unavailable_metrics(tmp_path: Path) -> None:
    clock = FakeClock()
    state_path = tmp_path / "process-registry.json"
    registry = ProcessRegistry(state_path, psutil_module=None, clock=clock)
    registry.initialize(tauri_pid=10, python_worker_pid=20)
    registry.register(30, ProcessRole.GIT, parent_pid=20, command="git clone")
    registry.register(40, ProcessRole.MMD_RENDERER, parent_pid=20, command="mmdc")
    registry.register(50, ProcessRole.PACKAGE_COMPILER, parent_pid=20, command="compile package")
    registry.set_active_context(
        pipeline_id="pipeline_123",
        stage_id="svg_png_rendering",
        lane="local_code",
        file="project_master_topology.mmd",
        command="mmdc render",
    )

    persisted = ProcessRegistry(state_path, psutil_module=None, clock=clock).snapshot()
    assert persisted["tauri_pid"] == 10
    assert persisted["python_worker_pid"] == 20
    assert persisted["git_pid"] == 30
    assert persisted["mmd_renderer_pid"] == 40
    assert persisted["package_compiler_pid"] == 50
    assert persisted["child_pids"] == [30, 40, 50]
    assert persisted["active_pipeline_id"] == "pipeline_123"
    assert persisted["active_stage_id"] == "svg_png_rendering"
    assert persisted["active_file"] == "project_master_topology.mmd"
    assert persisted["active_command"] == "mmdc render"

    metrics = registry.sample_metrics()
    assert metrics["metric_scope"] == "EVIDENCE_LANE_APP_PROCESS_TREE"
    assert metrics["attribution_basis"] == "REGISTERED_TAURI_ROOT_AND_DESCENDANTS"
    assert metrics["machine_wide_values_used"] is False
    assert metrics["metrics_status"] == "PSUTIL_UNAVAILABLE"
    assert metrics["cpu_percent"] is None
    assert metrics["working_set_bytes"] is None
    assert metrics["gpu_attribution_status"] == GPU_ATTRIBUTION_UNAVAILABLE
    assert metrics["gpu_percent"] is None
    assert metrics["gpu_memory_bytes"] is None
    assert metrics["active_process_pid"] == 0
    assert set(metrics["resource_attribution"]) == {"cpu", "gpu", "ram", "storage"}
    assert {
        item["attribution_status"]
        for item in metrics["resource_attribution"].values()
    } == {"IDLE"}


def test_process_registry_threaded_updates_leave_one_valid_atomic_state(tmp_path: Path) -> None:
    state_path = tmp_path / "threaded-registry.json"
    registry = ProcessRegistry(state_path, psutil_module=None)
    registry.initialize()

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(
            executor.map(
                lambda pid: registry.register(pid, ProcessRole.CHILD, parent_pid=900),
                range(901, 913),
            )
        )

    snapshot = registry.snapshot()
    assert snapshot["child_pids"] == list(range(901, 913))
    assert sorted(int(pid) for pid in snapshot["processes"]) == list(range(901, 913))
    assert json.loads(state_path.read_text(encoding="utf-8"))["schema_version"] == 1


class FakeProcess:
    def __init__(
        self,
        module: "FakePsutil",
        pid: int,
        *,
        parent_pid: int,
        cpu: float,
        rss: int,
        private: int,
        read_bytes: int,
        write_bytes: int,
        children: tuple[int, ...] = (),
    ) -> None:
        self.module = module
        self.pid = pid
        self.parent_pid = parent_pid
        self.cpu = cpu
        self.rss = rss
        self.private = private
        self.read_bytes = read_bytes
        self.write_bytes = write_bytes
        self.child_pids = children

    def ppid(self) -> int:
        return self.parent_pid

    def create_time(self) -> float:
        return 1_600_000_000.0 + self.pid

    def children(self, recursive: bool = True) -> list["FakeProcess"]:
        assert recursive is True
        result: list[FakeProcess] = []
        for pid in self.child_pids:
            child = self.module.Process(pid)
            result.append(child)
            result.extend(child.children(recursive=True))
        return result

    def cpu_percent(self, interval: None = None) -> float:
        assert interval is None
        return self.cpu

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=self.rss)

    def memory_full_info(self) -> SimpleNamespace:
        return SimpleNamespace(private=self.private)

    def io_counters(self) -> SimpleNamespace:
        return SimpleNamespace(read_bytes=self.read_bytes, write_bytes=self.write_bytes)

    def exe(self) -> str:
        return rf"C:\EvidenceLane\process-{self.pid}.exe"

    def name(self) -> str:
        return f"process-{self.pid}.exe"

    def status(self) -> str:
        return "running"

    def cmdline(self) -> list[str]:
        return [self.exe(), "--evidence-lane-worker"]


class FakePsutil:
    def __init__(self) -> None:
        self.processes: dict[int, FakeProcess] = {}

    def add(self, pid: int, **kwargs: object) -> FakeProcess:
        process = FakeProcess(self, pid, **kwargs)
        self.processes[pid] = process
        return process

    def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - matches psutil API
        if pid not in self.processes:
            raise RuntimeError(f"missing process {pid}")
        return self.processes[pid]

    @staticmethod
    def cpu_count(logical: bool = True) -> int:
        assert logical is True
        return 4

    @staticmethod
    def virtual_memory() -> SimpleNamespace:
        return SimpleNamespace(total=10_000)


def test_register_prunes_exited_historical_processes_before_adding_new_owner(tmp_path: Path) -> None:
    fake = FakePsutil()
    fake.add(
        100,
        parent_pid=1,
        cpu=0.0,
        rss=100,
        private=80,
        read_bytes=0,
        write_bytes=0,
    )
    registry = ProcessRegistry(tmp_path / "registry.json", psutil_module=fake)
    registry.initialize(python_worker_pid=100)
    fake.processes.pop(100)
    fake.add(
        200,
        parent_pid=1,
        cpu=0.0,
        rss=100,
        private=80,
        read_bytes=0,
        write_bytes=0,
    )

    snapshot = registry.register(200, ProcessRole.PYTHON_WORKER)

    assert set(snapshot["processes"]) == {"200"}
    assert snapshot["python_worker_pid"] == 200


def test_metrics_aggregate_only_registered_roots_and_descendants_and_retain_peaks(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakePsutil()
    root = fake.add(
        100,
        parent_pid=1,
        cpu=40.0,
        rss=1_000,
        private=800,
        read_bytes=100,
        write_bytes=50,
        children=(101,),
    )
    child = fake.add(
        101,
        parent_pid=100,
        cpu=20.0,
        rss=2_000,
        private=1_500,
        read_bytes=200,
        write_bytes=100,
    )
    fake.add(
        999,
        parent_pid=1,
        cpu=100.0,
        rss=999_999,
        private=999_999,
        read_bytes=999_999,
        write_bytes=999_999,
    )

    registry = ProcessRegistry(tmp_path / "registry.json", psutil_module=fake, clock=clock)
    registry.initialize(tauri_pid=100)
    registry.set_active_context(
        pipeline_id="pipeline_metrics",
        stage_id="per_lane_parsing_chunking_indexing",
        file="source.csv",
        command="index csv",
    )

    first = registry.sample_metrics(active_process_pid=101)
    assert first["metric_scope"] == "EVIDENCE_LANE_APP_PROCESS_TREE"
    assert first["attribution_basis"] == "REGISTERED_TAURI_ROOT_AND_DESCENDANTS"
    assert first["machine_wide_values_used"] is False
    assert first["registered_pids"] == [100]
    assert first["sampled_pids"] == [100, 101]
    assert 999 not in first["sampled_pids"]
    assert first["cpu_percent_raw_sum"] == 60.0
    assert first["cpu_percent"] == 15.0
    assert first["working_set_bytes"] == 3_000
    assert first["working_set_percent_of_system"] == 30.0
    assert first["private_bytes"] == 2_300
    assert first["disk_read_bytes_per_second"] is None
    assert first["gpu_attribution_status"] == GPU_ATTRIBUTION_UNAVAILABLE
    assert first["active_process_pid"] == 101
    for resource, attribution in first["resource_attribution"].items():
        assert attribution["process_pid"] == 101
        assert attribution["process_tree_ids"] == [100, 101]
        assert attribution["task"] == "index csv"
        assert attribution["stage_id"] == "per_lane_parsing_chunking_indexing"
        assert attribution["pipeline_id"] == "pipeline_metrics"
        if resource != "gpu":
            assert attribution["attribution_status"] == first["metrics_status"]
    snapshot = registry.snapshot(prune_stale=True)
    assert set(snapshot["processes"]) == {"100", "101"}
    assert snapshot["processes"]["101"]["metadata"]["discovered_from_process_tree"] is True
    assert snapshot["processes"]["100"]["executable_name"] == "process-100.exe"
    assert snapshot["processes"]["100"]["executable_path"].endswith("process-100.exe")
    assert snapshot["processes"]["100"]["process_status"] == "running"
    assert "--evidence-lane-worker" in snapshot["processes"]["100"]["command"]
    assert "executable_sha256" in snapshot["processes"]["100"]

    root.read_bytes = 500
    root.write_bytes = 250
    child.read_bytes = 600
    child.write_bytes = 300
    root.cpu = 20.0
    child.cpu = 12.0
    clock.advance(2)
    second = registry.sample_metrics()
    assert second["disk_read_bytes_per_second"] == 400.0
    assert second["disk_write_bytes_per_second"] == 200.0
    assert second["disk_total_bytes_per_second"] == 600.0
    assert second["cpu_percent_raw"] == 8.0
    assert second["cpu_percent"] == 11.5
    assert second["rolling_window_seconds"] == 5.0
    assert second["rolling_sample_count"] == 2
    assert second["peak_cpu_percent"] == 15.0
    assert second["peak_working_set_bytes"] == 3_000
    assert second["peak_disk_read_bytes_per_second"] == 400.0
    assert second["peak_disk_write_bytes_per_second"] == 200.0
    assert second["peak_disk_total_bytes_per_second"] == 600.0
    assert second["active_pipeline_id"] == "pipeline_metrics"
    assert second["active_command"] == "index csv"


def test_gpu_sampler_is_used_only_when_it_returns_pid_attribution(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakePsutil()
    fake.add(
        200,
        parent_pid=1,
        cpu=4.0,
        rss=100,
        private=80,
        read_bytes=0,
        write_bytes=0,
    )
    sampled: list[tuple[int, ...]] = []

    def gpu_sampler(pids: tuple[int, ...]) -> dict[str, object]:
        sampled.append(pids)
        return {
            "gpu_attribution_status": GPU_ATTRIBUTION_AVAILABLE,
            "gpu_percent": 37.5,
            "gpu_memory_bytes": 4096,
        }

    registry = ProcessRegistry(
        tmp_path / "registry.json",
        psutil_module=fake,
        clock=clock,
        gpu_sampler=gpu_sampler,
    )
    registry.initialize(python_worker_pid=200)

    metrics = registry.sample_metrics()
    assert sampled == [(200,)]
    assert metrics["gpu_attribution_status"] == GPU_ATTRIBUTION_AVAILABLE
    assert metrics["gpu_percent"] == 37.5
    assert metrics["gpu_memory_bytes"] == 4096
