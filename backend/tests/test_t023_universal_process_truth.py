from __future__ import annotations

import json
from pathlib import Path

import pytest

import sqlite_brain_builder.ipc_worker as ipc_worker
from sqlite_brain_builder.runtime.pipeline_state import PipelineStateStore
from sqlite_brain_builder.runtime.universal_process_truth import (
    UNIVERSAL_TASK_EVENT_SCHEMA,
    UNIVERSAL_TASK_REQUIRED_FIELDS,
    UniversalTaskEventError,
    UniversalTaskEventStore,
)
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


class Clock:
    def __init__(self) -> None:
        self.value = 1_750_000_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _identity() -> dict[str, object]:
    return {
        "task_id": "task-build-001",
        "run_id": "run-build-001",
        "request_id": "request-build-001",
        "project_id": "Gold Local",
        "brain_id": "brain-gold-local",
        "brain_name": "Gold Local",
        "workspace_dir": "D:/EvidenceLane/projects/gold-local",
        "accepted_snapshot_id": "snapshot-v1",
        "candidate_delta_id": "",
        "lane_id": "local_code",
        "stage_id": "source_validation_registration",
        "tool_id": "sqlite",
        "process_pid": 4200,
        "process_tree_ids": [4200, 4201],
        "command": "brain.buildAll",
        "total_units": 10,
    }


def test_universal_task_event_contains_every_mandatory_field_and_one_identity(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "universal_task_event.json"
    store = UniversalTaskEventStore(path, clock=clock)

    started = store.start(_identity())
    clock.advance(2.0)
    progressed = store.update(
        {
            "stage_id": "per_lane_parsing_chunking_indexing",
            "tool_id": "python",
            "completed_units": 3,
            "total_units": 10,
            "progress": 30,
            "cpu_percent": 42.5,
            "gpu_percent": 3.0,
            "ram_bytes": 2048,
            "disk_read_bytes": 4096,
            "disk_write_bytes": 1024,
            "process_tree_ids": [4200, 4201, 4202],
        }
    )
    clock.advance(3.0)
    completed = store.update(
        {
            "status": "completed",
            "completed_units": 10,
            "progress": 100,
            "next_pointer": "accepted-brain-v2",
        }
    )

    assert started["schema"] == UNIVERSAL_TASK_EVENT_SCHEMA
    assert set(UNIVERSAL_TASK_REQUIRED_FIELDS).issubset(started)
    assert {
        completed["task_id"],
        progressed["task_id"],
        started["task_id"],
    } == {"task-build-001"}
    assert completed["run_id"] == started["run_id"]
    assert completed["process_tree_ids"] == [4200, 4201, 4202]
    assert completed["completed_units"] == completed["total_units"] == 10
    assert completed["progress"] == 100.0
    assert completed["stop_timestamp"]
    assert len(completed["receipt_hash"]) == 64
    assert store.snapshot() == completed
    assert len(store.history()) == 3
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == completed


def test_universal_task_event_rejects_identity_mutation_and_progress_regression(tmp_path: Path) -> None:
    store = UniversalTaskEventStore(tmp_path / "task.json", clock=Clock())
    store.start(_identity())
    first = store.update({"progress": 75, "completed_units": 7})
    second = store.update({"progress": 1, "completed_units": 1})

    assert first["progress"] == second["progress"] == 75.0
    assert first["completed_units"] == second["completed_units"] == 7
    with pytest.raises(UniversalTaskEventError, match="IDENTITY_MUTATION:task_id"):
        store.update({"task_id": "shadow-task"})
    with pytest.raises(UniversalTaskEventError, match="IDENTITY_MUTATION:workspace_dir"):
        store.update({"workspace_dir": "D:/shadow"})


def test_universal_task_event_records_hil_stop_without_claiming_completion(tmp_path: Path) -> None:
    clock = Clock()
    store = UniversalTaskEventStore(tmp_path / "task.json", clock=clock)
    store.start({**_identity(), "command": "brain.refresh.fuse", "candidate_delta_id": "delta-9"})
    clock.advance(1.0)
    stopped = store.update(
        {
            "status": "hil_waiting",
            "hil_state": "AWAITING_EXPLICIT_HUMAN_DECISION",
            "next_pointer": "HIL:APPROVE|REJECT|SUPERSEDE",
        }
    )

    assert stopped["status"] == "hil_waiting"
    assert stopped["progress"] == 0.0
    assert stopped["hil_timestamp"]
    assert stopped["stop_timestamp"]
    assert stopped["receipt_hash"]


def test_universal_task_event_rejects_machine_wide_metrics(tmp_path: Path) -> None:
    store = UniversalTaskEventStore(tmp_path / "task.json", clock=Clock())
    store.start(_identity())
    with pytest.raises(UniversalTaskEventError, match="MACHINE_WIDE_METRICS_FORBIDDEN"):
        store.update({"machine_wide_values_used": True})


def test_worker_emits_and_persists_one_task_identity_for_all_cross_surface_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire: list[dict[str, object]] = []

    def fake_handle(_request: dict[str, object]) -> dict[str, object]:
        ipc_worker._emit_authoritative_task_progress(
            "run-one-identity",
            {
                "pipeline_id": "pipeline-one-identity",
                "request_id": "run-one-identity",
                "workspace_dir": str(tmp_path.resolve()),
                "stage_id": "per_lane_parsing_chunking_indexing",
                "stage_name": "Per-lane parsing/chunking/indexing",
                "stage_order": 3,
                "stage_count": 10,
                "stage_percent": 60,
                "global_percent": 26,
                "running_count": 1,
                "queued_count": 7,
                "completed_count": 2,
                "elapsed_seconds": 3.5,
                "eta_seconds": 10.0,
                "status": "running",
            },
        )
        return {
            "status": "PASS",
            "pipeline": {
                "stage_id": "immutable_version_capture",
                "files_done": 1,
                "files_total": 1,
            },
            "process_metrics": {
                "metric_scope": "EVIDENCE_LANE_APP_PROCESS_TREE",
                "machine_wide_values_used": False,
                "metrics_status": "OK",
                "sampled_at": "2026-07-19T12:00:00+00:00",
                "cpu_percent": 31.5,
                "gpu_percent": 4.0,
                "working_set_bytes": 8192,
                "disk_read_bytes": 16384,
                "disk_write_bytes": 4096,
            },
            "version": {"version_id": "version-accepted-2"},
            "lane_execution": {"unloaded_lanes_not_fired": ["github_code", "pdf_ocr"]},
        }

    monkeypatch.setattr(ipc_worker, "handle", fake_handle)
    monkeypatch.setattr(ipc_worker, "_json_line", lambda payload: wire.append(payload))
    request = {
        "id": "run-one-identity",
        "command": "brain.buildAll",
        "payload": {
            "workspace_dir": str(tmp_path),
            "brain_name": "Gold Local",
            "sources": [
                {
                    "source_id": "gold-source",
                    "lane_key": "local_code",
                    "path": str(tmp_path),
                    "active": True,
                }
            ],
        },
    }

    assert ipc_worker._serve_request(json.dumps(request)) == 0
    document = json.loads(
        (tmp_path / ".evidenceos_runtime" / "universal_task_event.json").read_text(
            encoding="utf-8"
        )
    )
    event = document["event"]
    task_events = [
        item["payload"]
        for item in wire
        if item.get("type") == "event" and item.get("event") in {"task.started", "task.done"}
    ]

    assert event["schema"] == UNIVERSAL_TASK_EVENT_SCHEMA
    assert event["status"] == "completed"
    assert event["request_id"] == "run-one-identity"
    assert event["pipeline_id"] == "pipeline-one-identity"
    assert event["workspace_dir"] == str(tmp_path.resolve())
    assert event["stage_order"] == 3
    assert event["stage_count"] == 10
    assert event["global_percent"] == 100.0
    assert event["running_count"] == event["queued_count"] == 0
    assert event["completed_count"] == 10
    assert event["elapsed_seconds"] == 3.5
    assert event["accepted_snapshot_id"] == "version-accepted-2"
    assert event["loaded_lane_ids"] == ["local_code"]
    assert event["skipped_lane_ids"] == ["github_code", "pdf_ocr"]
    assert event["cpu_percent"] == 31.5
    assert event["ram_bytes"] == 8192.0
    assert event["machine_wide_values_used"] is False
    assert len(event["receipt_hash"]) == 64
    assert {item["task_id"] for item in task_events} == {event["task_id"]}
    assert {item["run_id"] for item in task_events} == {"run-one-identity"}
    assert "run-one-identity" not in ipc_worker._ACTIVE_TASK_CONTEXTS
    progress_event = next(item for item in wire if item.get("event") == "task.progress")
    assert progress_event["payload"]["task_id"] == event["task_id"]
    assert progress_event["payload"]["pipeline_id"] == "pipeline-one-identity"


def test_worker_failure_event_is_hash_bound_and_does_not_leave_active_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire: list[dict[str, object]] = []

    def fail_handle(_request: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("FORCED_BUILD_FAILURE")

    monkeypatch.setattr(ipc_worker, "handle", fail_handle)
    monkeypatch.setattr(ipc_worker, "_json_line", lambda payload: wire.append(payload))
    request = {
        "id": "run-failed-identity",
        "command": "brain.buildAll",
        "payload": {"workspace_dir": str(tmp_path), "brain_name": "Gold Local", "sources": []},
    }

    assert ipc_worker._serve_request(json.dumps(request)) == 1
    event = json.loads(
        (tmp_path / ".evidenceos_runtime" / "universal_task_event.json").read_text(
            encoding="utf-8"
        )
    )["event"]
    error_event = next(item for item in wire if item.get("event") == "task.error")

    assert event["status"] == "failed"
    assert event["failure_code"] == "FORCED_BUILD_FAILURE"
    assert len(event["receipt_hash"]) == 64
    assert error_event["payload"]["task_id"] == event["task_id"]
    assert "run-failed-identity" not in ipc_worker._ACTIVE_TASK_CONTEXTS


def test_selected_brain_context_exposes_only_its_matching_universal_task_event(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Gold Local")
    create_brain(workspace, "Other Brain")
    store = UniversalTaskEventStore(
        workspace / ".evidenceos_runtime" / "universal_task_event.json",
        clock=Clock(),
    )
    started = store.start(
        {
            **_identity(),
            "brain_name": "Gold Local",
            "workspace_dir": str(workspace.resolve()),
        }
    )

    selected = ipc_worker.handle(
        {
            "id": "select-gold-local",
            "command": "brain.select",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Gold Local",
                "selection_restore_only": True,
            },
        }
    )
    other = ipc_worker.handle(
        {
            "id": "select-other",
            "command": "brain.select",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Other Brain",
                "selection_restore_only": True,
            },
        }
    )

    assert selected["pipeline_state"]["task_event"]["task_id"] == started["task_id"]
    assert selected["pipeline_state"]["task_event"]["brain_name"] == "Gold Local"
    assert other["pipeline_state"]["task_event"] is None


def test_each_brain_restores_its_own_exact_terminal_event_and_history(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Gold Local")
    create_brain(workspace, "Gold Git")

    expected: dict[str, tuple[str, float]] = {}
    for index, brain_name in enumerate(("Gold Local", "Gold Git"), start=1):
        request_id = f"per-brain-run-{index}"
        started = ipc_worker._start_authoritative_task(
            request_id,
            "brain.buildAll",
            workspace,
            brain_name,
            {"sources": []},
        )
        ipc_worker._emit_authoritative_task_progress(
            request_id,
            {
                "stage_id": "immutable_version_capture",
                "stage_name": "Immutable version capture",
                "stage_order": 10,
                "stage_count": 10,
                "global_percent": 99,
                "elapsed_seconds": float(index * 12),
                "files_done": index,
                "files_total": index,
            },
        )
        terminal = ipc_worker._finish_authoritative_task(
            request_id,
            {
                "pipeline": {
                    "stage_id": "immutable_version_capture",
                    "files_done": index,
                    "files_total": index,
                },
                "process_metrics": {
                    "cpu_percent": float(index * 11),
                    "gpu_percent": float(index),
                    "working_set_bytes": index * 4096,
                    "disk_read_bytes": index * 8192,
                    "disk_write_bytes": index * 2048,
                    "metrics_status": "ATTRIBUTED",
                    "metric_scope": "EVIDENCE_LANE_APP_PROCESS_TREE",
                    "machine_wide_values_used": False,
                },
            },
        )
        ipc_worker._ACTIVE_TASK_CONTEXTS.pop(request_id, None)
        assert terminal is not None
        expected[brain_name] = (started["task_id"], float(index * 11))

    for brain_name, (task_id, cpu_percent) in expected.items():
        selected = ipc_worker.handle(
            {
                "id": f"select-{brain_name}",
                "command": "brain.select",
                "payload": {
                    "workspace_dir": str(workspace),
                    "brain_name": brain_name,
                    "selection_restore_only": True,
                },
            }
        )
        state = selected["pipeline_state"]
        assert state["task_event_source"] == "PER_BRAIN_UNIVERSAL_EVENT"
        assert state["task_event"]["task_id"] == task_id
        assert state["task_event"]["brain_name"] == brain_name
        assert state["task_event"]["status"] == "completed"
        assert state["task_event"]["progress"] == 100.0
        assert state["task_event"]["cpu_percent"] == cpu_percent
        assert state["task_event"]["elapsed_seconds"] > 0
        assert state["task_history"][-1]["task_id"] == task_id
        assert state["task_history"][-1]["status"] == "completed"


def test_legacy_per_brain_pipeline_restores_without_rescan_or_invented_metrics(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Legacy Gold")
    state_path = (
        ipc_worker.brain_output_dir(workspace, "Legacy Gold")
        / "project"
        / "runtime"
        / "pipeline_state.json"
    )
    store = PipelineStateStore(state_path)
    store.start(
        pipeline_id="legacy-gold-pipeline",
        request_id="legacy-gold-request",
        brain_name="Legacy Gold",
        workspace_dir=str(workspace.resolve()),
        files_total=3,
        active_lane="local_code",
        active_command="brain.buildAll",
    )
    terminal = store.complete(
        files_done=3,
        rows_written=40,
        chunks_written=20,
        active_command="Immutable version captured",
    )

    selected = ipc_worker.handle(
        {
            "id": "select-legacy-gold",
            "command": "brain.select",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Legacy Gold",
                "selection_restore_only": True,
            },
        }
    )
    state = selected["pipeline_state"]
    event = state["task_event"]

    assert state["task_event_source"] == "LEGACY_PIPELINE_STATE_TRANSLATED"
    assert state["restored_from_disk"] is True
    assert event["schema"] == UNIVERSAL_TASK_EVENT_SCHEMA
    assert event["run_id"] == "legacy-gold-pipeline"
    assert event["status"] == "completed"
    assert event["global_percent"] == terminal["global_percent"] == 100.0
    assert event["stage_order"] == event["stage_count"] == 10
    assert event["completed_units"] == event["total_units"] == 3
    assert event["cpu_percent"] is None
    assert event["gpu_percent"] is None
    assert event["metric_scope"] == "LEGACY_PIPELINE_NO_PROCESS_METRIC_SNAPSHOT"
    assert event["restored_event_source"] == "LEGACY_PIPELINE_STATE_TRANSLATED"
    assert len(event["receipt_hash"]) == 64
    assert state["task_history"][-1]["status"] == "completed"
