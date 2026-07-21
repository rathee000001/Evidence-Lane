from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.gui import system_metrics
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def test_system_ping_reports_source_worker_packaging_contract() -> None:
    result = ipc_worker.handle({"command": "system.ping", "payload": {}})

    assert result["contract"] == "T023_FULL_APP_BACKEND_V2"
    assert result["workspace_required"] is False
    assert result["frozen"] is False
    assert result["packaging_mode"] == "PYTHON_SOURCE_WORKER"
    assert result["worker_executable"]


def test_brain_rename_moves_output_and_source_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Original Brain")
    source_path = tmp_path / "source.txt"
    source_path.write_text("source evidence", encoding="utf-8")
    ipc_worker._add_source(
        workspace,
        {
            "brain_name": "Original Brain",
            "lane_key": "docs",
            "path": str(source_path),
            "display_name": source_path.name,
        },
    )

    old_output = workspace / "original_brain_output"
    assert old_output.exists()

    result = ipc_worker._rename_brain(
        workspace,
        {"brain_name": "Original Brain", "new_brain_name": "Renamed Brain"},
    )

    new_output = workspace / "renamed_brain_output"
    assert not old_output.exists()
    assert new_output.exists()
    assert result["summary"]["brain_name"] == "Renamed Brain"
    assert result["summary"]["output_folder_name"] == "renamed_brain_output"
    assert len(result["summary"]["sources"]) == 1
    assert result["summary"]["sources"][0]["display_name"] == source_path.name


def test_github_clone_uses_longpaths_full_history_and_never_persists_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Git Brain")
    token = "private-token-value"
    commands: list[list[str]] = []

    def fake_run_hidden(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        target = Path(command[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / ".git").mkdir()
        return subprocess.CompletedProcess(command, 0, stdout="cloned", stderr="")

    monkeypatch.setattr(ipc_worker, "run_hidden", fake_run_hidden)
    result = ipc_worker._clone_github_source(
        workspace,
        {
            "brain_name": "Git Brain",
            "repo_url": "https://github.com/example/project.git",
            "token": token,
            "full_history": True,
            "pull_existing": True,
        },
    )

    assert commands
    assert commands[0][:4] == ["git", "-c", "core.longpaths=true", "clone"]
    assert "--depth" not in commands[0]
    serialized = json.dumps(result, ensure_ascii=False)
    assert token not in serialized
    assert token not in json.dumps(ipc_worker._sources_for(workspace, "Git Brain"), ensure_ascii=False)
    assert result["source"]["metadata"]["full_history"] is True


def test_github_error_sanitizes_private_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Git Brain")
    token = "do-not-leak-this-token"

    def failing_run_hidden(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 128, stdout="", stderr=f"authentication failed for {token}")

    monkeypatch.setattr(ipc_worker, "run_hidden", failing_run_hidden)
    with pytest.raises(ipc_worker.WorkerError) as exc_info:
        ipc_worker._clone_github_source(
            workspace,
            {
                "brain_name": "Git Brain",
                "repo_url": "https://github.com/example/private.git",
                "token": token,
                "full_history": True,
            },
        )

    assert token not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_github_existing_repo_checks_out_branch_before_pull(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Git Brain")
    target = workspace / "github_staging" / "project"
    (target / ".git").mkdir(parents=True)
    commands: list[list[str]] = []

    def fake_run_hidden(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ipc_worker, "run_hidden", fake_run_hidden)
    ipc_worker._clone_github_source(
        workspace,
        {
            "brain_name": "Git Brain",
            "repo_url": "https://github.com/example/project.git",
            "target_name": "project",
            "branch": "release",
            "pull_existing": True,
        },
    )

    assert [command[-2:] for command in commands] == [
        ["checkout", "release"],
        ["pull", "--ff-only"],
    ]


def test_machine_metrics_report_real_system_percentages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Memory:
        percent = 64.0

    class Disk:
        def __init__(self, read_bytes: int, write_bytes: int, read_time: int, write_time: int) -> None:
            self.read_bytes = read_bytes
            self.write_bytes = write_bytes
            self.read_time = read_time
            self.write_time = write_time

    class FakePsutil:
        def __init__(self) -> None:
            self.disk_calls = 0

        @staticmethod
        def cpu_percent(interval: float | None) -> float:
            assert interval is None
            return 42.5

        @staticmethod
        def virtual_memory() -> Memory:
            return Memory()

        def disk_io_counters(self) -> Disk:
            self.disk_calls += 1
            if self.disk_calls == 1:
                return Disk(1_000_000, 2_000_000, 100, 200)
            return Disk(2_000_000, 4_000_000, 105, 203)

    monkeypatch.setattr(system_metrics, "_psutil", FakePsutil())
    monkeypatch.setattr(system_metrics, "_gpu_percent", lambda: 37.0)

    class ImmediateThread:
        def __init__(self, *, target: object, **_: object) -> None:
            self.target = target

        def start(self) -> None:
            assert callable(self.target)
            self.target()

    monkeypatch.setattr(system_metrics.threading, "Thread", ImmediateThread)

    snapshot = system_metrics.ProcessMetricSampler().snapshot(tmp_path)

    assert snapshot["CPU"] == "42.5% system"
    assert snapshot["GPU"] == "37.0% system"
    assert snapshot["RAM_SYSTEM"] == "64% system"
    assert snapshot["SSD/HDD"].endswith("% active")
    assert snapshot["SSD/HDD"] != "unavailable"
    assert snapshot["CPU_PERCENT"] == 42.5
    assert snapshot["GPU_PERCENT"] == 37.0
    assert snapshot["RAM_PERCENT"] == 64.0
    assert isinstance(snapshot["DISK_ACTIVE_PERCENT"], float)
    assert snapshot["READ_MBPS"] > 0
    assert snapshot["WRITE_MBPS"] > 0
    assert snapshot["SAMPLE_LATENCY_MS"] >= 0


def test_machine_metrics_gpu_probe_is_not_run_on_ipc_request_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Memory:
        percent = 50.0

    class Disk:
        read_bytes = 100
        write_bytes = 200
        read_time = 1
        write_time = 1

    class FakePsutil:
        @staticmethod
        def cpu_percent(interval: float | None) -> float:
            assert interval is None
            return 10.0

        @staticmethod
        def virtual_memory() -> Memory:
            return Memory()

        @staticmethod
        def disk_io_counters() -> Disk:
            return Disk()

    pending: list[object] = []
    gpu_calls: list[str] = []

    class DeferredThread:
        def __init__(self, *, target: object, **_: object) -> None:
            pending.append(target)

        def start(self) -> None:
            return None

    monkeypatch.setattr(system_metrics, "_psutil", FakePsutil())
    monkeypatch.setattr(system_metrics.threading, "Thread", DeferredThread)
    monkeypatch.setattr(system_metrics, "_gpu_percent", lambda: gpu_calls.append("called") or 23.0)

    sampler = system_metrics.ProcessMetricSampler()
    first = sampler.snapshot(tmp_path)

    assert first["GPU_PERCENT"] is None
    assert gpu_calls == []
    assert len(pending) == 1

    target = pending.pop()
    assert callable(target)
    target()
    second = sampler.snapshot(tmp_path)

    assert second["GPU_PERCENT"] == 23.0
    assert gpu_calls == ["called"]


def test_worker_keeps_machine_and_process_metric_contracts_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeMachineSampler:
        def snapshot(self, workspace: Path) -> dict[str, str | float | None]:
            assert workspace == tmp_path.resolve()
            return {
                "CPU": "42.5% system",
                "GPU": "37.0% system",
                "RAM_SYSTEM": "64% system",
                "SSD/HDD": "2.0% active",
                "IO": "R 1.0 / W 2.0 MB/s",
                "CPU_PERCENT": 42.5,
                "GPU_PERCENT": 37.0,
                "RAM_PERCENT": 64.0,
                "DISK_ACTIVE_PERCENT": 2.0,
                "READ_MBPS": 1.0,
                "WRITE_MBPS": 2.0,
            }

    monkeypatch.setattr(ipc_worker, "ProcessMetricSampler", FakeMachineSampler)
    machine = ipc_worker.handle(
        {
            "command": "metrics.snapshot",
            "payload": {"workspace_dir": str(tmp_path), "brain_name": "Metrics Brain"},
        }
    )
    process = ipc_worker.handle(
        {
            "command": "processes.metrics.snapshot",
            "payload": {"workspace_dir": str(tmp_path), "brain_name": "Metrics Brain"},
        }
    )

    assert machine["CPU"] == "42.5% system"
    assert machine["GPU"] == "37.0% system"
    assert machine["CPU_PERCENT"] == 42.5
    assert machine["READ_MBPS"] == 1.0
    assert "cpu_percent" not in machine
    assert "cpu_percent" in process
    assert "metrics_status" in process


def test_pipeline_snapshot_exposes_authoritative_stage_catalog(tmp_path: Path) -> None:
    response = ipc_worker.handle(
        {
            "command": "pipeline.snapshot",
            "payload": {"workspace_dir": str(tmp_path), "brain_name": "Catalog Brain"},
        }
    )

    assert response["pipeline"] is None
    assert response["history"] == []
    assert [stage["stage_id"] for stage in response["stages"]] == [
        "source_validation_registration",
        "sqlite_project_router_sector_creation",
        "per_lane_parsing_chunking_indexing",
        "pointer_router_hash_finalization",
        "project_mmd_generation",
        "svg_png_rendering",
        "chatgpt_package_compilation",
        "gemini_exact10_compilation",
        "package_hash_validation",
        "immutable_version_capture",
    ]


def test_codex_handoff_create_command_dispatches_real_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_create(workspace: Path, brain_name: str, *, actor: str, reason: str) -> dict[str, str]:
        captured.update(
            workspace=workspace,
            brain_name=brain_name,
            actor=actor,
            reason=reason,
        )
        return {
            "status": "PASS",
            "handoff_id": "handoff_test",
            "handoff_folder": str(tmp_path / "handoff"),
            "package_path": str(tmp_path / "handoff.zip"),
        }

    monkeypatch.setattr(ipc_worker, "create_codex_brain_handoff", fake_create)
    result = ipc_worker.handle(
        {
            "command": "brain.codexHandoff.create",
            "payload": {
                "workspace_dir": str(tmp_path),
                "brain_name": "Dispatch Brain",
                "actor": "Focused test",
                "reason": "prove real command routing",
            },
        }
    )

    assert result["status"] == "PASS"
    assert result["handoff_id"] == "handoff_test"
    assert captured == {
        "workspace": tmp_path.resolve(),
        "brain_name": "Dispatch Brain",
        "actor": "Focused test",
        "reason": "prove real command routing",
    }


def test_codex_handoff_status_returns_stable_named_contract(tmp_path: Path) -> None:
    request = {
        "command": "brain.codexHandoff.status",
        "payload": {"workspace_dir": str(tmp_path), "brain_name": "Status Brain"},
    }
    assert ipc_worker.handle(request) == {
        "status": "NOT_CREATED",
        "brain_name": "Status Brain",
        "handoff": None,
    }

    database = tmp_path / "status_brain_output" / "brain_versions" / "semantic_brain_diff.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE brain_codex_handoff("
            "handoff_id TEXT,diff_run_id TEXT,handoff_folder TEXT,package_path TEXT,package_hash TEXT,"
            "manifest_hash TEXT,status TEXT,created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO brain_codex_handoff VALUES(?,?,?,?,?,?,?,?)",
            ("handoff_1", "diff_1", "C:/handoff", "C:/handoff.zip", "abc", "def", "PASS", "2026-07-11T00:00:00Z"),
        )

    status = ipc_worker.handle(request)
    assert status["status"] == "PASS"
    assert status["brain_name"] == "Status Brain"
    assert status["handoff"] == {
        "handoff_id": "handoff_1",
        "handoff_folder": "C:/handoff",
        "package_path": "C:/handoff.zip",
        "package_hash": "abc",
        "status": "PASS",
        "created_at": "2026-07-11T00:00:00Z",
    }


def test_codex_handoff_open_folder_uses_latest_backend_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff_folder = tmp_path / "handoff"
    handoff_folder.mkdir()
    monkeypatch.setattr(
        ipc_worker,
        "_codex_handoff_status",
        lambda *_: {
            "status": "PASS",
            "brain_name": "Open Brain",
            "handoff": {"handoff_folder": str(handoff_folder)},
        },
    )
    monkeypatch.setattr(ipc_worker, "_open_path", lambda path: {"opened": path})

    result = ipc_worker.handle(
        {
            "command": "brain.codexHandoff.openFolder",
            "payload": {"workspace_dir": str(tmp_path), "brain_name": "Open Brain"},
        }
    )
    assert result == {"opened": str(handoff_folder)}

    handoff_folder.rmdir()
    with pytest.raises(ipc_worker.WorkerError, match="CODEX_HANDOFF_FOLDER_NOT_AVAILABLE"):
        ipc_worker.handle(
            {
                "command": "brain.codexHandoff.openFolder",
                "payload": {"workspace_dir": str(tmp_path), "brain_name": "Open Brain"},
            }
        )


def test_mark_good_command_requires_and_forwards_passing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_mark(
        workspace: Path,
        brain_name: str,
        *,
        test_status: str,
        build_status: str,
        reason: str,
    ) -> dict[str, str]:
        captured.update(
            workspace=workspace,
            brain_name=brain_name,
            test_status=test_status,
            build_status=build_status,
            reason=reason,
        )
        return {"snapshot_id": "good_test", "version_id": "version_test"}

    monkeypatch.setattr(ipc_worker, "mark_current_passing_build_good", fake_mark)
    result = ipc_worker.handle(
        {
            "command": "brain.snapshot.markGood",
            "payload": {
                "workspace_dir": str(tmp_path),
                "brain_name": "Good Brain",
                "test_status": "PASS",
                "build_status": "PASS",
                "reason": "verified test receipt",
            },
        }
    )
    assert result == {"snapshot_id": "good_test", "version_id": "version_test"}
    assert captured == {
        "workspace": tmp_path.resolve(),
        "brain_name": "Good Brain",
        "test_status": "PASS",
        "build_status": "PASS",
        "reason": "verified test receipt",
    }

def test_json_line_is_code_page_safe_and_round_trips_unicode(capsys):
    payload = {"prompt": "ENV → UOP → PROJECT 🧠"}

    ipc_worker._json_line(payload)

    wire = capsys.readouterr().out.strip()
    wire.encode("cp1252")
    assert json.loads(wire) == payload
