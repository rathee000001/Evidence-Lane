from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.runtime.portable_brain_package import (
    LEDGER_SCHEMA_VERSION,
    append_endpoint_execution_event,
    append_model_execution_run,
    append_model_failure_event,
    append_task_run,
    initialize_operational_ledger,
)
from sqlite_brain_builder.workspace.local_workspace import ensure_local_workspace
from sqlite_brain_builder.workspace.model_connectors import (
    ModelConnectorError,
    get_model_connector,
    list_model_connectors,
    upsert_model_connector,
)


def _connector(endpoint_id: str, *, active: bool = False, endpoint_type: str = "OPENAI_COMPATIBLE") -> dict:
    return {
        "endpoint_id": endpoint_id,
        "display_name": endpoint_id.replace("-", " ").title(),
        "endpoint_type": endpoint_type,
        "base_url": "https://models.example.test",
        "model_id": "model-1",
        "health_check_path": "/health",
        "request_path": "/v1/chat/completions",
        "model_list_path": "/v1/models",
        "authentication_type": "BEARER_REFERENCE",
        "authentication_reference": None,
        "streaming_supported": True,
        "tool_call_supported": True,
        "file_supported": False,
        "context_limit": 32768,
        "request_mapping": {"messages": "messages", "model": "model"},
        "response_mapping": {"text": "choices.0.message.content"},
        "usage_mapping": {"total_tokens": "usage.total_tokens"},
        "timeout": 45,
        "local_or_remote": "REMOTE",
        "privacy_classification": "BOUNDED_REMOTE",
        "enabled": True,
        "active_prompt_bar": active,
    }


def _task_record() -> dict:
    return {
        "parent_goal_id": "T023_SUPREME_SOURCE_RECONCILED_LINEAR_EVIDENCEOS",
        "run_id": "run-model-ledger",
        "task_id": "task-model-ledger",
        "step_id": "step-model-ledger",
        "delta_ids_active": ["DELTA_MODEL_LEDGER"],
        "start_timestamp": "2026-07-13T00:00:00Z",
        "starting_snapshot_hash": "A" * 64,
        "build_status": "NOT_RUN",
        "human_gate_status": "PENDING",
        "completion_status": "CANDIDATE",
        "next_exact_pointer": "MODEL_HIL",
    }


def test_workspace_schema_adds_one_connector_registry_without_losing_existing_rows(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ensure_local_workspace(workspace)
    database = workspace / "workspace.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO local_project VALUES(?,?,?,?,?,?,?,?)",
            ("project-preserved", "identity_local_owner", "Preserved", None, "ACTIVE", 0, "before", "before"),
        )
        connection.commit()

    ensure_local_workspace(workspace)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='index'")
        }
        preserved = connection.execute(
            "SELECT name FROM local_project WHERE project_id='project-preserved'"
        ).fetchone()
    assert "endpoint_registry" in tables
    assert "endpoint_single_custom_prompt_bar_uq" in indexes
    assert preserved == ("Preserved",)


def test_connector_round_trip_keeps_multiple_profiles_but_only_one_custom_active(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = upsert_model_connector(workspace, _connector("endpoint-one", active=True))
    second = upsert_model_connector(workspace, _connector("endpoint-two", active=True))

    assert first["endpoint_id"] == "endpoint-one"
    assert second["active_prompt_bar"] is True
    profiles = list_model_connectors(workspace, include_disabled=True)["connectors"]
    assert {row["endpoint_id"] for row in profiles} == {"endpoint-one", "endpoint-two"}
    assert [row["endpoint_id"] for row in profiles if row["active_prompt_bar"]] == ["endpoint-two"]
    assert get_model_connector(workspace, "endpoint-one")["active_prompt_bar"] is False

    with sqlite3.connect(workspace / "workspace.sqlite") as connection:
        receipts = connection.execute(
            "SELECT COUNT(*) FROM local_operation_receipt WHERE operation='model.connector.upsert'"
        ).fetchone()[0]
    assert receipts == 2


def test_connector_contract_rejects_raw_secret_material_and_never_persists_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    unsafe = _connector("unsafe-endpoint")
    unsafe["api_key"] = "test-provider-secret-must-never-be-stored"
    with pytest.raises(ModelConnectorError, match="RAW_SECRET_MATERIAL_FORBIDDEN"):
        upsert_model_connector(workspace, unsafe)

    nested = _connector("unsafe-mapping")
    nested["request_mapping"] = {"headers": {"Authorization": "Bearer secret-value"}}
    with pytest.raises(ModelConnectorError, match="RAW_SECRET_MATERIAL_FORBIDDEN"):
        upsert_model_connector(workspace, nested)

    ensure_local_workspace(workspace)
    with sqlite3.connect(workspace / "workspace.sqlite") as connection:
        dumped = "\n".join(connection.iterdump())
    assert "test-provider-secret-must-never-be-stored" not in dumped
    assert "Bearer secret-value" not in dumped


def test_ipc_exposes_connector_commands_and_mutation_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    created = ipc_worker.handle(
        {
            "command": "model.connector.upsert",
            "payload": {"workspace_dir": str(workspace), "profile": _connector("ipc-endpoint")},
        }
    )
    listed = ipc_worker.handle(
        {"command": "model.connector.list", "payload": {"workspace_dir": str(workspace)}}
    )
    selected = ipc_worker.handle(
        {
            "command": "model.connector.setActive",
            "payload": {"workspace_dir": str(workspace), "endpoint_id": "ipc-endpoint"},
        }
    )
    disabled = ipc_worker.handle(
        {
            "command": "model.connector.disable",
            "payload": {"workspace_dir": str(workspace), "endpoint_id": "ipc-endpoint"},
        }
    )

    assert created["endpoint_id"] == "ipc-endpoint"
    assert listed["connectors"][0]["endpoint_id"] == "ipc-endpoint"
    assert selected["active_prompt_bar"] is True
    assert disabled["enabled"] is False
    assert disabled["active_prompt_bar"] is False
    assert {
        "model.connector.upsert",
        "model.connector.setActive",
        "model.connector.disable",
    } <= ipc_worker._MUTATING_COMMANDS


def test_operational_ledger_migrates_v001_through_v003_in_place_and_preserves_rows(tmp_path: Path) -> None:
    ledger = tmp_path / "state_travel_ledger.sqlite"
    initialize_operational_ledger(ledger)
    task_id = append_task_run(ledger, _task_record())

    with sqlite3.connect(ledger) as connection:
        for table in ("endpoint_execution_event", "model_failure_event", "model_execution_run", "steer_prompt_answer"):
            connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
            connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("UPDATE ledger_metadata SET schema_version='T023_CODEX_LEDGER_V001'")
        connection.commit()

    migrated = initialize_operational_ledger(ledger)
    with sqlite3.connect(ledger) as connection:
        version = connection.execute("SELECT schema_version FROM ledger_metadata").fetchone()[0]
        task = connection.execute("SELECT id FROM task_run WHERE id=?", (task_id,)).fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        v001_receipt = connection.execute(
            "SELECT status FROM acceptance_receipt WHERE receipt_id LIKE 'LEDGER_MIGRATION_V001_TO_V002_%'"
        ).fetchone()
        v003_receipt = connection.execute(
            "SELECT status FROM acceptance_receipt WHERE receipt_id LIKE 'LEDGER_MIGRATION_V002_TO_V003_%'"
        ).fetchone()

    assert migrated["migrated_from"] == "T023_CODEX_LEDGER_V001"
    assert len(migrated["migration_receipt_ids"]) == 2
    assert version == LEDGER_SCHEMA_VERSION == "T023_CODEX_LEDGER_V003_EVIDENCE_LANE_RQ"
    assert task == (task_id,)
    assert {"model_execution_run", "endpoint_execution_event", "model_failure_event", "steer_prompt_answer"} <= tables
    assert v001_receipt == ("PASS_PRESERVED_PRIOR_ROWS",)
    assert v003_receipt == ("PASS_PRESERVED_PRIOR_ROWS_AND_SUPERSEDED_RQ_CATALOG",)


def test_model_execution_endpoint_and_failure_rows_are_append_only(tmp_path: Path) -> None:
    ledger = tmp_path / "state_travel_ledger.sqlite"
    initialize_operational_ledger(ledger)
    task_id = append_task_run(ledger, _task_record())
    execution_id = append_model_execution_run(
        ledger,
        {
            "execution_id": "execution-1",
            "task_run_id": task_id,
            "brain_id": "brain-1",
            "brain_snapshot_hash": "A" * 64,
            "connector_id": "endpoint-one",
            "endpoint_class": "OPENAI_COMPATIBLE",
            "model_id": "model-1",
            "user_request": "Summarize the verified evidence slice.",
            "evidence_slice_ids": ["slice-1"],
            "sql_queries": ["SELECT bounded evidence"],
            "fts_queries": ["verified"],
            "request_timestamp": "2026-07-13T00:00:00Z",
            "status": "REQUESTED",
        },
    )
    event_id = append_endpoint_execution_event(
        ledger,
        {
            "execution_run_id": execution_id,
            "event_type": "RESPONSE_RECEIVED",
            "status": "PASS",
            "provider_response_id": "provider-response-1",
            "usage_classification": "UNAVAILABLE",
            "usage": {},
            "response_text": "Bounded response",
        },
    )
    failure_id = append_model_failure_event(
        ledger,
        {
            "failure_id": "failure-1",
            "execution_run_id": execution_id,
            "task_id": "task-model-ledger",
            "model_or_endpoint": "endpoint-one/model-1",
            "package_id": None,
            "lane_id": "local_code",
            "stage": "connection_test",
            "failure_class": "ENDPOINT_UNREACHABLE",
            "observed_evidence": {"status": 503},
            "exact_error": "connection refused",
            "inferred_cause": None,
            "retry_safe": True,
            "data_preserved": True,
            "rollback_available": True,
            "next_action_code": "SAFE_RETRY",
            "next_action_text": "Retry after checking the endpoint.",
            "unresolved_risk": "Endpoint state is unknown.",
            "timestamp": "2026-07-13T00:00:01Z",
        },
    )

    with sqlite3.connect(ledger) as connection:
        execution = connection.execute(
            "SELECT user_request_sha256,status FROM model_execution_run WHERE id=?", (execution_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT response_text,usage_classification FROM endpoint_execution_event WHERE id=?", (event_id,)
        ).fetchone()
        failure = connection.execute(
            "SELECT failure_class,retry_safe,data_preserved FROM model_failure_event WHERE id=?", (failure_id,)
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_UPDATE_FORBIDDEN"):
            connection.execute("UPDATE model_execution_run SET status='PASS' WHERE id=?", (execution_id,))
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_DELETE_FORBIDDEN"):
            connection.execute("DELETE FROM model_failure_event WHERE id=?", (failure_id,))

    assert execution == (
        "640703E2AB7AA1F9BA48DA66A05A57EFCA6E6DDF3A45C2E348ABBC15C6BEDE1E",
        "REQUESTED",
    )
    assert event == ("Bounded response", "UNAVAILABLE")
    assert failure == ("ENDPOINT_UNREACHABLE", 1, 1)


def test_model_failure_requires_labeled_inference_and_known_failure_class(tmp_path: Path) -> None:
    ledger = tmp_path / "state_travel_ledger.sqlite"
    initialize_operational_ledger(ledger)
    base = {
        "failure_id": "failure-invalid",
        "task_id": "task",
        "model_or_endpoint": "endpoint/model",
        "stage": "request",
        "failure_class": "NOT_A_REAL_CLASS",
        "observed_evidence": {},
        "exact_error": "failed",
        "inferred_cause": "maybe credentials",
        "retry_safe": False,
        "data_preserved": True,
        "rollback_available": True,
        "next_action_code": "REVIEW",
        "next_action_text": "Review evidence.",
        "unresolved_risk": "Unknown.",
        "timestamp": "2026-07-13T00:00:00Z",
    }
    with pytest.raises(ValueError, match="MODEL_FAILURE_CLASS_INVALID"):
        append_model_failure_event(ledger, base)

    base["failure_class"] = "AUTHENTICATION_REJECTED"
    with pytest.raises(ValueError, match="INFERRED_CAUSE_MUST_BE_LABELED"):
        append_model_failure_event(ledger, base)


def test_ipc_exposes_append_only_model_ledger_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    package_root = workspace / "model-package"
    ledger = package_root / "codex" / "codex_runtime_ledger.sqlite"
    initialize_operational_ledger(ledger)
    task_id = append_task_run(ledger, _task_record())

    execution = ipc_worker.handle(
        {
            "command": "brain.modelLedger.execution.append",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Model Brain",
                "package_root": str(package_root),
                "record": {
                    "execution_id": "ipc-execution",
                    "task_run_id": task_id,
                    "brain_id": "brain-1",
                    "brain_snapshot_hash": "A" * 64,
                    "connector_id": "endpoint-one",
                    "endpoint_class": "OPENAI_COMPATIBLE",
                    "model_id": "model-1",
                    "user_request": "Use the bounded evidence slice.",
                    "request_timestamp": "2026-07-13T00:00:00Z",
                    "status": "REQUESTED",
                },
            },
        }
    )
    event = ipc_worker.handle(
        {
            "command": "brain.modelLedger.endpointEvent.append",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Model Brain",
                "package_root": str(package_root),
                "record": {
                    "execution_run_id": execution["model_execution_run_id"],
                    "event_type": "REQUEST_READY",
                    "status": "PASS",
                    "usage_classification": "UNAVAILABLE",
                },
            },
        }
    )
    failure = ipc_worker.handle(
        {
            "command": "brain.modelLedger.failure.append",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Model Brain",
                "package_root": str(package_root),
                "record": {
                    "failure_id": "ipc-failure",
                    "execution_run_id": execution["model_execution_run_id"],
                    "task_id": "task-model-ledger",
                    "model_or_endpoint": "endpoint-one/model-1",
                    "stage": "request",
                    "failure_class": "REQUEST_TIMEOUT",
                    "observed_evidence": {"timeout_seconds": 45},
                    "exact_error": "request timed out",
                    "retry_safe": True,
                    "data_preserved": True,
                    "rollback_available": True,
                    "next_action_code": "SAFE_RETRY",
                    "next_action_text": "Retry the preserved request.",
                    "unresolved_risk": "Endpoint availability remains unknown.",
                    "timestamp": "2026-07-13T00:00:45Z",
                },
            },
        }
    )

    assert execution["model_execution_run_id"] > 0
    assert event["endpoint_execution_event_id"] > 0
    assert failure["model_failure_event_id"] > 0
    assert {
        "brain.modelLedger.execution.append",
        "brain.modelLedger.endpointEvent.append",
        "brain.modelLedger.failure.append",
    } <= ipc_worker._MUTATING_COMMANDS


def test_endpoint_contract_artifact_matches_single_registry_and_secret_reference_law() -> None:
    configured = os.environ.get("EVIDENCE_LANE_ENDPOINT_SCHEMA")
    if not configured:
        pytest.skip("set EVIDENCE_LANE_ENDPOINT_SCHEMA to run the host-bound schema audit")
    schema_path = Path(configured)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["$id"] == "urn:evidenceos:t023:endpoint-contract:v1"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["authentication_reference"]["type"] == ["string", "null"]
    assert schema["x-evidenceos-laws"]["maximum_active_custom_prompt_bar_endpoints"] == 1
    assert schema["x-evidenceos-laws"]["raw_secret_fields_forbidden"] is True
    assert not ({"api_key", "secret", "password", "access_token"} & set(schema["properties"]))
