from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlite_brain_builder.runtime.pipeline_state import (
    _atomic_write_json,
    _exclusive_state_file_lock,
    _read_json_state,
)


UNIVERSAL_TASK_EVENT_SCHEMA = "T023_UNIVERSAL_PROCESS_TASK_EVENT_V001"
UNIVERSAL_TASK_STATE_VERSION = 1
UNIVERSAL_TASK_HISTORY_LIMIT = 256
UNIVERSAL_TASK_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "skipped", "hil_waiting"}
)
UNIVERSAL_TASK_VALID_STATUSES = frozenset(
    {"queued", "running", *UNIVERSAL_TASK_TERMINAL_STATUSES}
)
UNIVERSAL_TASK_IMMUTABLE_FIELDS = (
    "schema",
    "task_id",
    "run_id",
    "request_id",
    "project_id",
    "brain_id",
    "brain_name",
    "workspace_dir",
    "command",
    "start_timestamp",
)
UNIVERSAL_TASK_REQUIRED_FIELDS = (
    "schema",
    "task_id",
    "run_id",
    "project_id",
    "brain_id",
    "brain_name",
    "accepted_snapshot_id",
    "candidate_delta_id",
    "lane_id",
    "stage_id",
    "tool_id",
    "process_pid",
    "process_tree_ids",
    "start_timestamp",
    "update_timestamp",
    "stop_timestamp",
    "hil_timestamp",
    "status",
    "completed_units",
    "total_units",
    "cpu_percent",
    "gpu_percent",
    "ram_bytes",
    "disk_read_bytes",
    "disk_write_bytes",
    "progress",
    "failure_code",
    "skip_reason",
    "hil_state",
    "receipt_hash",
    "next_pointer",
    "command",
)


class UniversalTaskEventError(RuntimeError):
    pass


def _utc_iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat()


def _bounded_percent(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return round(max(0.0, min(100.0, parsed)), 2)


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)


def _optional_number(value: Any) -> float | int | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed)


def _canonical_receipt_hash(event: Mapping[str, Any]) -> str:
    body = {key: value for key, value in event.items() if key != "receipt_hash"}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest().upper()


def _normalize_process_ids(value: Any, process_pid: int) -> list[int]:
    values = value if isinstance(value, (list, tuple, set)) else []
    observed = {_non_negative_int(item) for item in values}
    if process_pid > 0:
        observed.add(process_pid)
    observed.discard(0)
    return sorted(observed)


def _validated_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    missing = [key for key in UNIVERSAL_TASK_REQUIRED_FIELDS if key not in payload]
    if missing:
        raise UniversalTaskEventError("UNIVERSAL_TASK_FIELDS_MISSING:" + ",".join(missing))
    event = dict(payload)
    if event.get("schema") != UNIVERSAL_TASK_EVENT_SCHEMA:
        raise UniversalTaskEventError("UNIVERSAL_TASK_SCHEMA_INVALID")
    status = str(event.get("status") or "")
    if status not in UNIVERSAL_TASK_VALID_STATUSES:
        raise UniversalTaskEventError(f"UNIVERSAL_TASK_STATUS_INVALID:{status}")
    if not str(event.get("task_id") or "") or not str(event.get("run_id") or ""):
        raise UniversalTaskEventError("UNIVERSAL_TASK_IDENTITY_MISSING")
    if bool(event.get("machine_wide_values_used")):
        raise UniversalTaskEventError("UNIVERSAL_TASK_MACHINE_WIDE_METRICS_FORBIDDEN")
    process_pid = _non_negative_int(event.get("process_pid"))
    event["process_pid"] = process_pid
    event["process_tree_ids"] = _normalize_process_ids(event.get("process_tree_ids"), process_pid)
    event["completed_units"] = _non_negative_int(event.get("completed_units"))
    event["total_units"] = max(
        event["completed_units"], _non_negative_int(event.get("total_units"))
    )
    event["progress"] = _bounded_percent(event.get("progress"))
    for key in (
        "cpu_percent",
        "gpu_percent",
        "ram_bytes",
        "disk_read_bytes",
        "disk_write_bytes",
    ):
        event[key] = _optional_number(event.get(key))
    return event


class UniversalTaskEventStore:
    """Persist the one cross-surface task identity and execution truth."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        history_limit: int = UNIVERSAL_TASK_HISTORY_LIMIT,
    ) -> None:
        self.state_path = Path(state_path).expanduser().resolve()
        self._clock = clock
        self._history_limit = max(1, int(history_limit))

    def start(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        now = float(self._clock())
        now_iso = _utc_iso(now)
        process_pid = _non_negative_int(identity.get("process_pid"))
        event = _validated_event(
            {
                "schema": UNIVERSAL_TASK_EVENT_SCHEMA,
                "task_id": str(identity.get("task_id") or ""),
                "run_id": str(identity.get("run_id") or ""),
                "request_id": str(identity.get("request_id") or ""),
                "project_id": str(identity.get("project_id") or ""),
                "brain_id": str(identity.get("brain_id") or ""),
                "brain_name": str(identity.get("brain_name") or ""),
                "workspace_dir": str(identity.get("workspace_dir") or ""),
                "accepted_snapshot_id": str(identity.get("accepted_snapshot_id") or ""),
                "candidate_delta_id": str(identity.get("candidate_delta_id") or ""),
                "lane_id": str(identity.get("lane_id") or ""),
                "stage_id": str(identity.get("stage_id") or "command_start"),
                "tool_id": str(identity.get("tool_id") or ""),
                "process_pid": process_pid,
                "process_tree_ids": _normalize_process_ids(
                    identity.get("process_tree_ids"), process_pid
                ),
                "start_timestamp": now_iso,
                "update_timestamp": now_iso,
                "stop_timestamp": "",
                "hil_timestamp": "",
                "status": "running",
                "completed_units": 0,
                "total_units": _non_negative_int(identity.get("total_units")),
                "cpu_percent": identity.get("cpu_percent"),
                "gpu_percent": identity.get("gpu_percent"),
                "ram_bytes": identity.get("ram_bytes"),
                "disk_read_bytes": identity.get("disk_read_bytes"),
                "disk_write_bytes": identity.get("disk_write_bytes"),
                "progress": 0.0,
                "failure_code": "",
                "skip_reason": "",
                "hil_state": str(identity.get("hil_state") or "NOT_APPLICABLE"),
                "receipt_hash": "",
                "next_pointer": str(identity.get("next_pointer") or ""),
                "command": str(identity.get("command") or ""),
                "active_command": str(identity.get("active_command") or ""),
                "active_file": str(identity.get("active_file") or ""),
                "loaded_lane_ids": sorted(set(identity.get("loaded_lane_ids") or [])),
                "skipped_lane_ids": sorted(set(identity.get("skipped_lane_ids") or [])),
                "metric_scope": str(identity.get("metric_scope") or "EVIDENCE_LANE_APP_PROCESS_TREE"),
                "machine_wide_values_used": False,
                "pipeline_id": str(identity.get("pipeline_id") or ""),
                "stage_name": str(identity.get("stage_name") or "Command start"),
                "stage_order": _non_negative_int(identity.get("stage_order")),
                "stage_count": _non_negative_int(identity.get("stage_count")),
                "stage_percent": _bounded_percent(identity.get("stage_percent")),
                "global_percent": _bounded_percent(identity.get("global_percent")),
                "running_count": _non_negative_int(identity.get("running_count") or 1),
                "queued_count": _non_negative_int(identity.get("queued_count")),
                "completed_count": _non_negative_int(identity.get("completed_count")),
                "elapsed_seconds": _optional_number(identity.get("elapsed_seconds")) or 0.0,
                "eta_seconds": _optional_number(identity.get("eta_seconds")),
                "started_at": str(identity.get("started_at") or now_iso),
                "updated_at": str(identity.get("updated_at") or now_iso),
                "error_code": str(identity.get("error_code") or ""),
            }
        )
        with _exclusive_state_file_lock(self.state_path):
            existing = _read_json_state(self.state_path) or {}
            history = list(existing.get("history") or [])
            history.append(event)
            _atomic_write_json(
                self.state_path,
                {
                    "schema_version": UNIVERSAL_TASK_STATE_VERSION,
                    "event": event,
                    "history": history[-self._history_limit :],
                },
            )
        return event

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        with _exclusive_state_file_lock(self.state_path):
            document = _read_json_state(self.state_path)
            if not document or not isinstance(document.get("event"), Mapping):
                raise UniversalTaskEventError("UNIVERSAL_TASK_NOT_STARTED")
            previous = _validated_event(document["event"])
            event = dict(previous)
            for key, value in changes.items():
                if key in UNIVERSAL_TASK_IMMUTABLE_FIELDS and value != previous.get(key):
                    raise UniversalTaskEventError(f"UNIVERSAL_TASK_IDENTITY_MUTATION:{key}")
                event[key] = value
            event["progress"] = max(previous["progress"], _bounded_percent(event.get("progress")))
            event["global_percent"] = max(
                _bounded_percent(previous.get("global_percent")),
                _bounded_percent(event.get("global_percent")),
            )
            event["completed_units"] = max(
                previous["completed_units"], _non_negative_int(event.get("completed_units"))
            )
            event["completed_count"] = max(
                _non_negative_int(previous.get("completed_count")),
                _non_negative_int(event.get("completed_count")),
            )
            event["stage_order"] = max(
                _non_negative_int(previous.get("stage_order")),
                _non_negative_int(event.get("stage_order")),
            )
            event["elapsed_seconds"] = max(
                float(_optional_number(previous.get("elapsed_seconds")) or 0.0),
                float(_optional_number(event.get("elapsed_seconds")) or 0.0),
            )
            event["total_units"] = max(
                event["completed_units"],
                previous["total_units"],
                _non_negative_int(event.get("total_units")),
            )
            now = float(self._clock())
            event["update_timestamp"] = _utc_iso(now)
            event["updated_at"] = str(changes.get("updated_at") or _utc_iso(now))
            status = str(event.get("status") or previous["status"])
            if status in UNIVERSAL_TASK_TERMINAL_STATUSES:
                event["stop_timestamp"] = str(event.get("stop_timestamp") or _utc_iso(now))
                if status == "hil_waiting":
                    event["hil_timestamp"] = str(event.get("hil_timestamp") or _utc_iso(now))
                if status == "completed":
                    event["progress"] = 100.0
                    event["global_percent"] = 100.0
                    if event["total_units"]:
                        event["completed_units"] = event["total_units"]
            event = _validated_event(event)
            if status in UNIVERSAL_TASK_TERMINAL_STATUSES and not event.get("receipt_hash"):
                event["receipt_hash"] = _canonical_receipt_hash(event)
            history = list(document.get("history") or [])
            history.append(event)
            _atomic_write_json(
                self.state_path,
                {
                    "schema_version": UNIVERSAL_TASK_STATE_VERSION,
                    "event": event,
                    "history": history[-self._history_limit :],
                },
            )
            return event

    def snapshot(self) -> dict[str, Any] | None:
        document = _read_json_state(self.state_path)
        if not document or not isinstance(document.get("event"), Mapping):
            return None
        if int(document.get("schema_version") or 0) != UNIVERSAL_TASK_STATE_VERSION:
            raise UniversalTaskEventError("UNIVERSAL_TASK_STATE_VERSION_UNSUPPORTED")
        return _validated_event(document["event"])

    def history(self) -> list[dict[str, Any]]:
        document = _read_json_state(self.state_path) or {}
        return [_validated_event(item) for item in document.get("history") or []]


__all__ = [
    "UNIVERSAL_TASK_EVENT_SCHEMA",
    "UNIVERSAL_TASK_REQUIRED_FIELDS",
    "UniversalTaskEventError",
    "UniversalTaskEventStore",
]
