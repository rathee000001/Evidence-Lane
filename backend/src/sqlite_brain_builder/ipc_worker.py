from __future__ import annotations

import json
import hashlib
import base64
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from sqlite_brain_builder.brain_versions import (
    capture_brain_version,
    drop_brain_version_to_recycle_bin,
    list_brain_versions,
    rollback_brain_version,
)
from sqlite_brain_builder.codex_env15_package import (
    PACKAGE_FAMILY,
    create_codex_env15_package,
    validate_codex_env15_zip,
)
from sqlite_brain_builder.gui.system_metrics import ProcessMetricSampler
from sqlite_brain_builder.runtime.canonical_lanes import UnknownLaneAliasError, resolve_lane_id
from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir, slugify_name
from sqlite_brain_builder.runtime.package_validation import (
    validate_chatgpt_package,
    validate_gemini_exact10,
)
from sqlite_brain_builder.runtime.package_parity import validate_active_project_package_parity
from sqlite_brain_builder.runtime.provider_package_projection import (
    GEMINI_PACKAGE_MAX_BYTES,
    remove_provider_stage,
    write_provider_skip_marker,
)
from sqlite_brain_builder.runtime.portable_brain_package import (
    append_endpoint_execution_event,
    append_model_execution_run,
    append_task_run,
    append_model_failure_event,
    create_portable_brain_package,
    export_portable_brain_zip,
    query_immutable_snapshot,
    record_model_usage,
    record_project_query,
    record_rq_answers,
    record_steer_prompt_answer,
    register_goal_delta,
    validate_portable_brain_package,
    validate_portable_brain_zip,
)
from sqlite_brain_builder.runtime.pipeline_state import (
    PIPELINE_STAGES,
    PipelineStateStore,
    _atomic_write_json,
    _read_json_state,
)
from sqlite_brain_builder.runtime.process_registry import ProcessRegistry, windows_gpu_pid_sampler
from sqlite_brain_builder.runtime.project_delta_ledger import get_project_delta, list_project_deltas
from sqlite_brain_builder.runtime.review_qualification import (
    ReviewQualificationError,
    record_accepted_by_continuation,
    verify_accepted_by_continuation_receipt,
)
from sqlite_brain_builder.runtime.universal_process_truth import (
    UNIVERSAL_TASK_EVENT_SCHEMA,
    UniversalTaskEventError,
    UniversalTaskEventStore,
)
from sqlite_brain_builder.runtime.plan_delta_governance import (
    PlanDeltaGovernanceError,
    append_classified_steer_delta,
    build_plan_goal_prompt,
    read_plan_delta_state,
    record_model_state_slip,
    set_delta_disposition,
)
from sqlite_brain_builder.runtime.native_picker import NativePickerError, choose_directory, choose_files
from sqlite_brain_builder.refresh_brain import (
    cancel_refresh_brain,
    cleanup_unfused_refresh_candidate,
    drop_refresh_candidate,
    fuse_refresh_candidate,
    get_refresh_output,
    get_refresh_status,
    record_refresh_hil_decision,
    retry_refresh_brain,
    start_refresh_brain,
)
from sqlite_brain_builder.telemetry_overlay import (
    BrainTelemetryError,
    get_telemetry_snapshot,
    list_telemetry_deltas,
    load_materialized_telemetry_bundle,
    materialize_telemetry_graph_cache,
    open_telemetry_target,
    query_telemetry_graph,
    telemetry_contract,
)
from sqlite_brain_builder.rollback_product import rollback_brain_as_complete_product
from sqlite_brain_builder.brain_delete import recycle_registered_brain
from sqlite_brain_builder.codex_handoff import (
    create_codex_brain_handoff,
    mark_current_passing_build_good,
)
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    LANE_DEFS,
    LOCKED_FLASH_PROMPT,
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
    generate_project_mmd,
    materialize_schema_only_package_topology,
    render_topology,
    run_hidden,
    scan_tools,
)
from sqlite_brain_builder.runtime.workspace_build_ledger import record_workspace_build_truth
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace
from sqlite_brain_builder.workspace.workspace_roots import (
    WorkspaceRootHistoryError,
    assert_canonical_production_workspace,
    canonical_route_state,
    current_workspace_root,
    default_workspace_dir,
    drop_workspace_root_history,
    list_workspace_roots,
    native_production_mode,
    record_workspace_root,
    retain_only_workspace_root,
    roaming_config_root,
)
from sqlite_brain_builder.workspace.local_workspace import (
    append_chat_lineage,
    append_message,
    create_chat,
    create_project,
    delete_chat,
    delete_protected_credential,
    ensure_local_workspace,
    export_local_data,
    get_admin_settings,
    get_profile,
    get_session_state,
    get_settings,
    import_local_data,
    link_attachment,
    list_chats,
    list_projects,
    local_snapshot,
    mutate_chat,
    get_protected_credential_status,
    reset_settings,
    search_workspace,
    set_protected_credential,
    update_admin_settings,
    update_profile,
    update_session_state,
    update_settings,
)


# Preserve the production exporter identity so strict active/package parity can
# remain mandatory while compatibility tests inject non-materializing doubles.
_CANONICAL_CHATGPT_EXPORTER = export_one_upload_package
_CANONICAL_GEMINI_EXPORTER = export_gemini_exact10
from sqlite_brain_builder.workspace.model_connectors import (
    delete_model_connector,
    disable_model_connector,
    discover_model_connector_models,
    get_model_connector,
    list_model_connectors,
    set_active_model_connector,
    upsert_model_connector,
    validate_model_connector,
)
from sqlite_brain_builder.workspace.model_execution import execute_model_connector
from sqlite_brain_builder.workspace.selected_brain_state import (
    load_historical_provider_delta_archive,
    load_model_output_state,
)
from sqlite_brain_builder.workspace.codex_application import (
    get_codex_settings,
    get_codex_status,
    launch_codex,
    update_codex_settings,
)
from sqlite_brain_builder.workspace.ollama_integration import (
    get_ollama_models,
    get_ollama_settings,
    get_ollama_status,
    launch_ollama,
    refresh_ollama_registry,
    update_ollama_settings,
)
from sqlite_brain_builder.ingest.lane_contracts import validate_schema_contract

try:
    import psutil as _psutil
except Exception:  # pragma: no cover - stale-lock recovery stays conservative without psutil
    _psutil = None


DEFAULT_WORKSPACE = default_workspace_dir()
SOURCE_SETTINGS_PREFIX = "eos_sources:"
PIN_SETTINGS_KEY = "eos_pinned_brains"
LAST_SELECTED_BRAIN_SETTINGS_KEY = "eos_last_selected_brain"
BRAIN_DISCOVERY_TOMBSTONES_KEY = "eos_brain_discovery_tombstones"
LANE_SCHEMA_OVERRIDE_PREFIX = "eos_lane_schema_override:"
LANE_SCHEMA_HISTORY_PREFIX = "eos_lane_schema_history:"
_WORKSPACE_SYSTEM_DIRECTORIES = {
    ".evidenceos_runtime",
    "_fixtures",
    "exports",
    "github_staging",
    "logs",
    "packages",
    "receipts",
    "renders",
    "review_queue",
}
_SCHEMA_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_INITIALIZED_REGISTRIES: set[tuple[str, int, int, bool]] = set()
_REGISTRY_INIT_LOCK = threading.RLock()
_MACHINE_METRIC_SAMPLER: Any = None
_MACHINE_METRIC_SAMPLER_TYPE: type[Any] | None = None
_MACHINE_METRIC_SAMPLER_LOCK = threading.RLock()
_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_MUTATION_LOCKS_GUARD = threading.RLock()
_MUTATION_LOCK_TIMEOUT_SECONDS = 1.5
_ACTIVE_TASK_CONTEXTS: dict[str, dict[str, Any]] = {}
_PROCESS_TRUTH_COMMANDS = {
    "brain.build",
    "brain.buildAll",
    "brain.codexHandoff.create",
    "brain.create",
    "brain.deleteToRecycleBin",
    "brain.portablePackage.create",
    "brain.portablePackage.export",
    "brain.refresh.cancel",
    "brain.refresh.cleanup",
    "brain.refresh.drop",
    "brain.refresh.fuse",
    "brain.refresh.hil.decide",
    "brain.refresh.retry",
    "brain.refresh.start",
    "brain.review.acceptByContinuation",
    "brain.rename",
    "brain.snapshot.markGood",
    "brain.version.capture",
    "brain.version.drop",
    "brain.version.rollback",
    "chat.lineage.append",
    "model.connector.execute",
    "package.exportChatGPT",
    "package.exportGemini",
    "sources.add",
    "sources.cloneGithub",
    "sources.remove",
    "sources.removeLane",
    "topology.render",
}
_SELECTION_CONTEXT_REUSE_CONTRACT = {
    "mode": "METADATA_ONLY_SELECTED_BRAIN_RESTORE",
    "raw_sources_reparsed": False,
    "raw_sources_rechunked": False,
    "raw_source_hashes_recomputed": False,
    "raw_sources_reindexed": False,
    "version_payload_hashes_reverified": False,
}
_SELECTED_BRAIN_AUTHORITY_SCHEMA = "T023_CANONICAL_SELECTED_BRAIN_AUTHORITY_V1"
_MUTATING_COMMANDS = {
    "workspace.init",
    "workspace.outputRoot.choose",
    "workspace.rootHistory.add",
    "workspace.rootHistory.drop",
    "local.workspace.init",
    "profile.update",
    "settings.update",
    "settings.reset",
    "settings.import",
    "admin.settings.update",
    "credential.set",
    "credential.delete",
    "model.connector.upsert",
    "model.connector.setActive",
    "model.connector.disable",
    "model.connector.discoverModels",
    "model.connector.validate",
    "model.connector.delete",
    "model.connector.execute",
    "ollama.settings.update",
    "ollama.registry.refresh",
    "ollama.launch",
    "codex.launch",
    "project.create",
    "chat.create",
    "chat.rename",
    "chat.pin",
    "chat.unpin",
    "chat.move",
    "chat.detach",
    "chat.archive",
    "chat.unarchive",
    "chat.markRead",
    "chat.delete",
    "message.append",
    "attachment.link",
    "session.update",
    "chat.lineage.append",
    "brain.create",
    "brain.select",
    "brain.selection.timing.record",
    "brain.rename",
    "brain.pin",
    "brain.unpin",
    "brain.remove",
    "brain.deleteToRecycleBin",
    "brains.discover",
    "brain.build",
    "brain.buildAll",
    "brain.version.capture",
    "brain.version.rollback",
    "brain.version.drop",
    "brain.planDelta.state",
    "brain.planDelta.append",
    "brain.planDelta.disposition",
    "brain.planGoalPrompt.read",
    "brain.planStateSlip.append",
    "brain.codexHandoff.create",
    "brain.snapshot.markGood",
    "brain.refresh.start",
    "brain.refresh.hil.decide",
    "brain.refresh.cancel",
    "brain.refresh.retry",
    "brain.refresh.fuse",
    "brain.refresh.drop",
    "brain.refresh.cleanup",
    "brain.review.acceptByContinuation",
    "brain.telemetry.openTarget",
    "brain.portablePackage.create",
    "brain.portablePackage.export",
    "brain.portablePackage.query",
    "brain.codexLedger.task.append",
    "brain.codexLedger.usage.record",
    "brain.codexLedger.rq.record",
    "brain.codexLedger.steerPrompt.record",
    "brain.modelLedger.execution.append",
    "brain.modelLedger.endpointEvent.append",
    "brain.modelLedger.failure.append",
    "sources.add",
    "sources.cloneGithub",
    "sources.remove",
    "sources.removeLane",
    "source.schema.update",
    "source.schema.reset",
    "flash.read",
    "topology.render",
    "package.exportChatGPT",
    "package.exportGemini",
}

if os.name == "nt":
    _WORKER_HIDDEN_SI = subprocess.STARTUPINFO()
    _WORKER_HIDDEN_SI.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _WORKER_HIDDEN_SI.wShowWindow = 0
    _WORKER_HIDDEN_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    _WORKER_HIDDEN_SI = None
    _WORKER_HIDDEN_FLAGS = 0


class WorkerError(RuntimeError):
    pass


def _is_plan_sector_build_required(error: PlanDeltaGovernanceError) -> bool:
    message = str(error)
    return (
        message.startswith("DELTA_SECTOR_BASE_ENV15_INVALID:")
        and "ENV15_ROUTER_MISSING" in message
    )


def _plan_sector_build_required_state(brain_root: Path) -> dict[str, Any]:
    return {
        "contract": "T023_DELTA_V1",
        "availability": "BUILD_REQUIRED",
        "status": "PLAN_NOT_BUILT",
        "error_code": "ENV15_ROUTER_MISSING",
        "database_path": None,
        "refresh_delta_ledger": str(brain_root / "project" / "deltas" / "delta_ledger.db"),
        "refresh_ledger_relationship": "DISTINCT_DO_NOT_MIX",
        "canonical_pointer": {},
        "deltas": [],
        "disposition_events": [],
        "lanes": [],
        "goal_prompts": [],
        "state_slips": [],
    }


def _plan_prompt_build_required() -> dict[str, Any]:
    return {
        "write_status": "NOT_BUILT",
        "availability": "BUILD_REQUIRED",
        "status": "PLAN_NOT_BUILT",
        "error_code": "ENV15_ROUTER_MISSING",
        "prompt_id": None,
        "prompt_index": 2,
        "hil_gate": None,
        "text": "",
        "text_sha256": None,
        "path": None,
        "mutation_receipt": None,
    }


def _machine_metrics_snapshot(workspace: Path) -> dict[str, Any]:
    """Reuse one sampler per worker while remaining monkeypatch-safe in tests."""

    global _MACHINE_METRIC_SAMPLER, _MACHINE_METRIC_SAMPLER_TYPE
    sampler_type = ProcessMetricSampler
    with _MACHINE_METRIC_SAMPLER_LOCK:
        if _MACHINE_METRIC_SAMPLER is None or _MACHINE_METRIC_SAMPLER_TYPE is not sampler_type:
            _MACHINE_METRIC_SAMPLER = sampler_type()
            _MACHINE_METRIC_SAMPLER_TYPE = sampler_type
        return dict(_MACHINE_METRIC_SAMPLER.snapshot(workspace))


def _json_line(payload: dict[str, Any]) -> None:
    # The persistent worker can inherit a Windows console code page (commonly
    # cp1252) even though the desktop bridge consumes JSON as Unicode.  Keep the
    # wire representation ASCII-only so prompts containing arrows, emoji, or
    # other non-code-page characters cannot fail after the command succeeds.
    # json.loads restores the original Unicode text at the receiving boundary.
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def _event(name: str, request_id: str, payload: dict[str, Any] | None = None) -> None:
    _json_line({"type": "event", "event": name, "request_id": request_id, "payload": payload or {}})


def _response(request_id: str, ok: bool, result: Any = None, error: str | None = None) -> None:
    body: dict[str, Any] = {"type": "response", "id": request_id, "ok": ok}
    if ok:
        body["result"] = result
    else:
        body["error"] = error or "UNKNOWN_WORKER_ERROR"
    _json_line(body)


def _workspace(payload: dict[str, Any]) -> Path:
    explicit = payload.get("workspace_dir") or payload.get("workspaceDir")
    # The native host supplies the canonical production root separately. An
    # inherited EVIDENCE_OS_WORKSPACE value is a development override and must
    # not be allowed to break or reroute native bootstrap. Explicit IPC routes
    # still pass through the canonical-root guard below and fail closed.
    inherited = None if native_production_mode() else os.environ.get("EVIDENCE_OS_WORKSPACE")
    raw = explicit or inherited
    workspace = normalize_workspace_dir(raw or current_workspace_root())
    try:
        workspace = assert_canonical_production_workspace(workspace)
    except WorkspaceRootHistoryError as exc:
        raise WorkerError(str(exc)) from exc
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _db_path(workspace: Path) -> Path:
    return workspace / "workspace.sqlite"


def _connect_workspace(workspace: Path) -> sqlite3.Connection:
    init_workspace(workspace)
    con = sqlite3.connect(_db_path(workspace), timeout=60)
    con.row_factory = sqlite3.Row
    return con


def _setting(con: sqlite3.Connection, key: str, default: Any) -> Any:
    row = con.execute("SELECT value FROM workspace_settings WHERE key=?", (key,)).fetchone()
    if not row or row["value"] in (None, ""):
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def _set_setting(con: sqlite3.Connection, key: str, value: Any) -> bool:
    row = con.execute("SELECT value FROM workspace_settings WHERE key=?", (key,)).fetchone()
    if row and row["value"] not in (None, ""):
        try:
            if json.loads(row["value"]) == value:
                # Preserve the historical commit boundary for callers that may
                # have made another change, without rewriting an unchanged
                # setting or advancing its timestamp.
                con.commit()
                return False
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    con.execute(
        "INSERT OR REPLACE INTO workspace_settings(key,value,updated_at) VALUES(?,?,?)",
        (key, json.dumps(value, ensure_ascii=False), time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    con.commit()
    return True


def _legacy_utf8_repair_candidates(value: str) -> list[str]:
    """Return bounded candidates for text previously decoded through Windows ANSI."""

    candidates = [str(value or "")]
    for _ in range(2):
        for candidate in list(candidates):
            for encoding in ("cp1252", "latin-1"):
                try:
                    repaired = candidate.encode(encoding).decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    continue
                if repaired not in candidates:
                    candidates.append(repaired)
    return candidates


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _receipt_slug(operation: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", operation.casefold()).strip("_") or "operation"


def _write_operation_receipt(workspace: Path, operation: str, detail: Mapping[str, Any]) -> dict[str, Any]:
    receipts = workspace / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    stable_detail = json.loads(json.dumps(dict(detail), ensure_ascii=False, sort_keys=True, default=str))
    identity = hashlib.sha256(
        json.dumps(
            {"operation": operation, "detail": stable_detail},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    path = receipts / f"{_receipt_slug(operation)}_{identity[:20]}.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("receipt_sha256"):
                return {**existing, "receipt_path": str(path)}
        except (OSError, json.JSONDecodeError):
            pass
    body: dict[str, Any] = {
        "receipt_version": "EVIDENCE_OS_OPERATION_RECEIPT_V001",
        "operation": operation,
        "operation_id": f"operation_{identity[:24]}",
        "created_at": _utc_now(),
        "workspace_root": str(workspace),
        "detail": stable_detail,
    }
    body_hash = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    body["receipt_sha256"] = body_hash
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return {**body, "receipt_path": str(path)}


def _brain_schema_scope_for_connection(con: sqlite3.Connection, brain_name: str) -> str:
    row = con.execute(
        "SELECT brain_id FROM brain_project WHERE brain_name=? AND status<>'REMOVED'",
        (brain_name,),
    ).fetchone()
    return str(row["brain_id"] if row else f"brain:{slugify_name(brain_name)}")


def _implicit_schema_brain_name(workspace: Path) -> str:
    """Preserve legacy unscoped callers without weakening per-brain isolation."""
    con = _connect_workspace(workspace)
    try:
        rows = con.execute(
            "SELECT brain_name FROM brain_project WHERE status<>'REMOVED' ORDER BY created_at, brain_name"
        ).fetchall()
    finally:
        con.close()
    return str(rows[0]["brain_name"]) if len(rows) == 1 else "New Brain"


def _schema_setting_key(brain_scope: str, lane_id: str) -> str:
    return f"{LANE_SCHEMA_OVERRIDE_PREFIX}{brain_scope}:{lane_id}"


def _schema_history_key(brain_scope: str, lane_id: str) -> str:
    return f"{LANE_SCHEMA_HISTORY_PREFIX}{brain_scope}:{lane_id}"


def _schema_contract_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _validate_lane_schema_contract(value: Any) -> tuple[list[str], str, str]:
    text = _schema_contract_text(value)
    if not text:
        raise WorkerError("SCHEMA_CONTRACT_REQUIRED")
    if len(text) > 64 * 1024:
        raise WorkerError("SCHEMA_CONTRACT_TOO_LARGE")
    safe, reason = validate_schema_contract(text)
    if not safe:
        raise WorkerError(reason)
    schema: list[str] = []
    for raw in text.splitlines():
        identifier = raw.strip().strip("-*| `")
        if not identifier:
            continue
        if not _SCHEMA_IDENTIFIER.fullmatch(identifier):
            raise WorkerError(f"INVALID_SCHEMA_IDENTIFIER:{identifier}")
        if identifier.casefold().startswith("sqlite_"):
            raise WorkerError(f"RESERVED_SCHEMA_IDENTIFIER:{identifier}")
        if identifier in schema:
            raise WorkerError(f"DUPLICATE_SCHEMA_IDENTIFIER:{identifier}")
        schema.append(identifier)
    if not schema:
        raise WorkerError("SCHEMA_CONTRACT_HAS_NO_IDENTIFIERS")
    if len(schema) > 128:
        raise WorkerError("SCHEMA_CONTRACT_TOO_MANY_IDENTIFIERS")
    normalized = "\n".join(schema)
    return schema, normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _lane_schema_state_for_connection(
    con: sqlite3.Connection,
    lane_alias: str,
    brain_name: str = "New Brain",
) -> dict[str, Any]:
    try:
        lane_id = resolve_lane_id(lane_alias, scope="any")
    except UnknownLaneAliasError as exc:
        raise WorkerError(f"UNKNOWN_LANE_ALIAS:{lane_alias}") from exc
    default_schema = list(LANE_DEFS[lane_id]["schema"])
    default_text = "\n".join(default_schema)
    default_hash = hashlib.sha256(default_text.encode("utf-8")).hexdigest()
    brain_scope = _brain_schema_scope_for_connection(con, brain_name)
    saved = _setting(con, _schema_setting_key(brain_scope, lane_id), None)
    if not isinstance(saved, dict) or not isinstance(saved.get("schema"), list):
        return {
            "lane_id": lane_id,
            "brain_id": brain_scope,
            "brain_name": brain_name,
            "lane_label": LANE_DEFS[lane_id]["label"],
            "origin": "built_in_default",
            "version": 0,
            "schema": default_schema,
            "schema_contract": default_text,
            "schema_sha256": default_hash,
            "default_schema": default_schema,
            "default_schema_sha256": default_hash,
            "updated_at": None,
            "previous_schema_sha256": None,
        }
    schema, normalized, digest = _validate_lane_schema_contract(saved.get("schema"))
    return {
        "lane_id": lane_id,
        "brain_id": brain_scope,
        "brain_name": brain_name,
        "lane_label": LANE_DEFS[lane_id]["label"],
        "origin": "user_override",
        "version": int(saved.get("version") or 1),
        "schema": schema,
        "schema_contract": normalized,
        "schema_sha256": digest,
        "default_schema": default_schema,
        "default_schema_sha256": default_hash,
        "updated_at": saved.get("updated_at"),
        "previous_schema_sha256": saved.get("previous_schema_sha256"),
    }


def _lane_schema_state(
    workspace: Path,
    lane_alias: str,
    brain_name: str = "New Brain",
) -> dict[str, Any]:
    con = _connect_workspace(workspace)
    try:
        return _lane_schema_state_for_connection(con, lane_alias, brain_name)
    finally:
        con.close()


def _update_lane_schema(
    workspace: Path,
    lane_alias: str,
    contract: Any,
    *,
    brain_name: str = "New Brain",
    actor: str = "Evidence OS user",
) -> dict[str, Any]:
    schema, normalized, digest = _validate_lane_schema_contract(contract)
    con = _connect_workspace(workspace)
    try:
        current = _lane_schema_state_for_connection(con, lane_alias, brain_name)
        if digest == current["schema_sha256"]:
            receipt = _write_operation_receipt(
                workspace,
                "source.schema.update",
                {
                    "lane_id": current["lane_id"],
                    "schema_version": current["version"],
                    "schema_sha256": digest,
                    "idempotent_replay": True,
                },
            )
            return {**current, "idempotent_replay": True, "receipt_path": receipt["receipt_path"], "receipt_sha256": receipt["receipt_sha256"]}
        stamp = _utc_now()
        version = int(current["version"]) + 1
        state = {
            "lane_id": current["lane_id"],
            "version": version,
            "schema": schema,
            "schema_sha256": digest,
            "previous_schema_sha256": current["schema_sha256"],
            "updated_at": stamp,
            "updated_by": actor,
        }
        history = _setting(con, _schema_history_key(current["brain_id"], current["lane_id"]), [])
        if not isinstance(history, list):
            history = []
        con.execute(
            "INSERT OR REPLACE INTO workspace_settings(key,value,updated_at) VALUES(?,?,?)",
            (_schema_setting_key(current["brain_id"], current["lane_id"]), json.dumps(state, ensure_ascii=False), stamp),
        )
        con.execute(
            "INSERT OR REPLACE INTO workspace_settings(key,value,updated_at) VALUES(?,?,?)",
            (_schema_history_key(current["brain_id"], current["lane_id"]), json.dumps([*history, state], ensure_ascii=False), stamp),
        )
        con.commit()
        effective = _lane_schema_state_for_connection(con, current["lane_id"], brain_name)
    finally:
        con.close()
    receipt = _write_operation_receipt(
        workspace,
        "source.schema.update",
        {
            "lane_id": effective["lane_id"],
            "schema_version": effective["version"],
            "schema_sha256": effective["schema_sha256"],
            "previous_schema_sha256": effective["previous_schema_sha256"],
            "schema": effective["schema"],
            "actor": actor,
            "destructive_migration_performed": False,
        },
    )
    return {**effective, "idempotent_replay": False, "receipt_path": receipt["receipt_path"], "receipt_sha256": receipt["receipt_sha256"]}


def _reset_lane_schema(
    workspace: Path,
    lane_alias: str,
    *,
    brain_name: str = "New Brain",
    actor: str = "Evidence OS user",
) -> dict[str, Any]:
    con = _connect_workspace(workspace)
    try:
        current = _lane_schema_state_for_connection(con, lane_alias, brain_name)
        stamp = _utc_now()
        con.execute(
            "DELETE FROM workspace_settings WHERE key=?",
            (_schema_setting_key(current["brain_id"], current["lane_id"]),),
        )
        con.commit()
        effective = _lane_schema_state_for_connection(con, current["lane_id"], brain_name)
    finally:
        con.close()
    receipt = _write_operation_receipt(
        workspace,
        "source.schema.reset",
        {
            "lane_id": effective["lane_id"],
            "restored_default_schema_sha256": effective["schema_sha256"],
            "previous_schema_sha256": current["schema_sha256"],
            "actor": actor,
            "destructive_migration_performed": False,
        },
    )
    return {**effective, "receipt_path": receipt["receipt_path"], "receipt_sha256": receipt["receipt_sha256"]}


def _lane_defs_payload(workspace: Path, brain_name: str = "New Brain") -> dict[str, Any]:
    definitions = json.loads(json.dumps(LANE_DEFS, ensure_ascii=False))
    con = _connect_workspace(workspace)
    try:
        for lane_id, definition in definitions.items():
            state = _lane_schema_state_for_connection(con, lane_id, brain_name)
            definition["default_schema_contract"] = list(state["default_schema"])
            definition["schema"] = list(state["schema"])
            definition["schema_contract"] = list(state["schema"])
            definition["schema_origin"] = state["origin"]
            definition["schema_version"] = state["version"]
            definition["schema_sha256"] = state["schema_sha256"]
            definition["picker"] = _native_picker_descriptor(lane_id, definition)
    finally:
        con.close()
    return definitions


def _native_picker_descriptor(lane_id: str, definition: Mapping[str, Any]) -> dict[str, Any]:
    if lane_id in {"local_code", "project_engulf"}:
        return {
            "mode": "directory",
            "multiple": False,
            "exclude_accept_all_option": True,
            "types": [],
        }
    extensions = [
        str(extension).casefold()
        for extension in definition.get("extensions", [])
        if isinstance(extension, str) and extension.startswith(".") and "*" not in extension
    ]
    if extensions == [".pdf"]:
        mime_type = "application/pdf"
    elif extensions and all(extension in {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"} for extension in extensions):
        mime_type = "image/*"
    else:
        mime_type = "application/octet-stream"
    return {
        "mode": "file",
        "multiple": lane_id not in {"sqlite_brain"},
        "exclude_accept_all_option": True,
        "types": [
            {
                "description": f"{definition['label']} files",
                "accept": {mime_type: extensions},
            }
        ],
    }


def _native_single_file(
    *,
    title: str,
    extensions: list[str],
    initial_directory: Path,
    filter_description: str | None = None,
) -> Path | None:
    try:
        selections = choose_files(
            title=title,
            extensions=extensions,
            multiple=False,
            initial_directory=initial_directory,
            filter_description=filter_description,
        )
    except NativePickerError as exc:
        raise WorkerError(str(exc)) from exc
    if not selections:
        return None
    if len(selections) != 1:
        raise WorkerError("NATIVE_PICKER_SINGLE_FILE_REQUIRED")
    path = selections[0]
    allowed = {extension.casefold() for extension in extensions}
    if not path.is_file() or path.suffix.casefold() not in allowed:
        raise WorkerError(f"NATIVE_PICKER_FILE_TYPE_INVALID:{path.suffix.casefold() or '<none>'}")
    return path


def _native_binary_asset(path: Path, *, maximum_bytes: int, empty_error: str, size_error: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkerError("NATIVE_PICKER_FILE_UNREADABLE") from exc
    if size <= 0:
        raise WorkerError(empty_error)
    if size > maximum_bytes:
        raise WorkerError(size_error)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WorkerError("NATIVE_PICKER_FILE_UNREADABLE") from exc


def _safe_target_name(repo_url: str, target_name: str | None) -> str:
    raw = (target_name or "").strip()
    if not raw:
        path = urlsplit(repo_url).path.rstrip("/")
        raw = Path(path).name or "github_repo"
        if raw.endswith(".git"):
            raw = raw[:-4]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")
    return safe or "github_repo"


def _sanitize_token(text: str, token: str | None) -> str:
    out = text or ""
    if token:
        out = out.replace(token, "***")
    out = re.sub(r"(https?://)([^/\s:@]+):([^@\s]+)@", r"\1***:***@", out)
    out = re.sub(r"(https?://)([^@\s]+)@", r"\1***@", out)
    return out


def _url_with_token(repo_url: str, token: str | None) -> str:
    if not token or not repo_url.lower().startswith("https://"):
        return repo_url
    parts = urlsplit(repo_url)
    netloc = parts.netloc
    if "@" in netloc:
        return repo_url
    return urlunsplit((parts.scheme, f"x-access-token:{token}@{netloc}", parts.path, parts.query, parts.fragment))


def _display_repo_url(repo_url: str) -> str:
    return _sanitize_token(repo_url, None)


def _git_result_or_raise(result: subprocess.CompletedProcess[str], token: str | None, action: str) -> None:
    if result.returncode == 0:
        return
    stderr = _sanitize_token(result.stderr or "", token)
    stdout = _sanitize_token(result.stdout or "", token)
    detail = stderr or stdout or f"git exited with {result.returncode}"
    raise WorkerError(f"{action}_FAILED: {detail[:1200]}")


def _source_key(brain_name: str) -> str:
    return SOURCE_SETTINGS_PREFIX + slugify_name(brain_name)


def _normalized_path_identity(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _complete_brain_router(folder: Path) -> Path | None:
    router = folder / "project" / "project_router.sqlite"
    if not router.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{router.as_posix()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return None
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return router


def _derived_brain_name(folder: Path) -> str:
    raw = folder.name
    if raw.casefold().endswith("_output"):
        raw = raw[:-7]
    words = re.sub(r"[_-]+", " ", raw).strip()
    return " ".join(word[:1].upper() + word[1:] for word in words.split()) or "Recovered Brain"


def _discover_existing_brains(workspace: Path) -> dict[str, Any]:
    con = _connect_workspace(workspace)
    registered_count = 0
    reactivated_count = 0
    existing_count = 0
    tombstoned_count = 0
    valid_count = 0
    discovered: list[dict[str, Any]] = []
    try:
        tombstones = {
            _normalized_path_identity(value)
            for value in _setting(con, BRAIN_DISCOVERY_TOMBSTONES_KEY, [])
            if isinstance(value, str) and value.strip()
        }
        rows = con.execute(
            "SELECT brain_id,brain_name,brain_slug,output_dir,status,created_at,updated_at FROM brain_project"
        ).fetchall()
        by_output: dict[str, sqlite3.Row] = {}
        for row in rows:
            identity = _normalized_path_identity(row["output_dir"])
            prior = by_output.get(identity)
            if prior is None or (prior["status"] != "ACTIVE" and row["status"] == "ACTIVE"):
                by_output[identity] = row
        stamp = _utc_now()
        for folder in sorted((item for item in workspace.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
            if folder.name.casefold() in _WORKSPACE_SYSTEM_DIRECTORIES:
                continue
            router = _complete_brain_router(folder)
            if router is None:
                continue
            valid_count += 1
            identity = _normalized_path_identity(folder)
            row = by_output.get(identity)
            if identity in tombstones:
                tombstoned_count += 1
                continue
            if row is not None:
                if row["status"] == "ACTIVE":
                    existing_count += 1
                else:
                    con.execute(
                        "UPDATE brain_project SET status='ACTIVE',updated_at=? WHERE brain_id=?",
                        (stamp, row["brain_id"]),
                    )
                    reactivated_count += 1
                discovered.append(
                    {
                        "brain_id": row["brain_id"],
                        "brain_name": row["brain_name"],
                        "output_dir": str(folder),
                        "router": str(router),
                        "status": "EXISTING" if row["status"] == "ACTIVE" else "REACTIVATED_LEGACY",
                    }
                )
                continue
            brain_name = _derived_brain_name(folder)
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            brain_id = "brain_discovered_" + digest[:16]
            session_id = "session_discovered_" + digest[:16]
            con.execute(
                "INSERT INTO brain_project VALUES(?,?,?,?,?,?,?)",
                (brain_id, brain_name, slugify_name(brain_name), str(folder), "ACTIVE", stamp, stamp),
            )
            con.execute(
                "INSERT OR IGNORE INTO brain_session VALUES(?,?,?,?)",
                (session_id, brain_id, "Recovered existing brain", stamp),
            )
            con.execute(
                "INSERT OR IGNORE INTO brain_last_state_pointer VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    brain_id,
                    session_id,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "overview",
                    str(folder),
                    "Inspect recovered brain",
                    "ACTIVE",
                    stamp,
                ),
            )
            registered_count += 1
            discovered.append(
                {
                    "brain_id": brain_id,
                    "brain_name": brain_name,
                    "output_dir": str(folder),
                    "router": str(router),
                    "status": "REGISTERED_EXISTING",
                }
            )
        con.commit()
    finally:
        con.close()
    receipt = _write_operation_receipt(
        workspace,
        "brains.discover",
        {
            "registered_count": registered_count,
            "reactivated_count": reactivated_count,
            "existing_count": existing_count,
            "tombstoned_count": tombstoned_count,
            "valid_count": valid_count,
            "discovered": discovered,
            "live_build_refresh_fuse_invoked": False,
        },
    )
    return {
        "registered_count": registered_count,
        "reactivated_count": reactivated_count,
        "existing_count": existing_count,
        "tombstoned_count": tombstoned_count,
        "valid_count": valid_count,
        "discovered": discovered,
        "receipt_path": receipt["receipt_path"],
        "receipt_sha256": receipt["receipt_sha256"],
    }


def _record_brain_discovery_tombstone(workspace: Path, brain_name: str) -> None:
    con = _connect_workspace(workspace)
    try:
        row = con.execute(
            "SELECT output_dir FROM brain_project WHERE brain_name=? ORDER BY updated_at DESC LIMIT 1",
            (brain_name,),
        ).fetchone()
        if row is None:
            return
        tombstones = {
            _normalized_path_identity(value)
            for value in _setting(con, BRAIN_DISCOVERY_TOMBSTONES_KEY, [])
            if isinstance(value, str) and value.strip()
        }
        tombstones.add(_normalized_path_identity(row["output_dir"]))
        _set_setting(con, BRAIN_DISCOVERY_TOMBSTONES_KEY, sorted(tombstones))
    finally:
        con.close()


def _clear_brain_discovery_tombstone(workspace: Path, output_dir: Path | str) -> None:
    con = _connect_workspace(workspace)
    try:
        identity = _normalized_path_identity(output_dir)
        tombstones = {
            _normalized_path_identity(value)
            for value in _setting(con, BRAIN_DISCOVERY_TOMBSTONES_KEY, [])
            if isinstance(value, str) and value.strip()
        }
        if identity in tombstones:
            tombstones.remove(identity)
            _set_setting(con, BRAIN_DISCOVERY_TOMBSTONES_KEY, sorted(tombstones))
    finally:
        con.close()


def _list_brains(workspace: Path) -> list[dict[str, Any]]:
    con = _connect_workspace(workspace)
    pins = set(_setting(con, PIN_SETTINGS_KEY, []))
    rows = con.execute(
        """
        SELECT brain_id, brain_name, brain_slug, output_dir, status, created_at, updated_at
        FROM brain_project
        WHERE COALESCE(status,'ACTIVE') = 'ACTIVE'
        ORDER BY updated_at DESC, created_at DESC
        """
    ).fetchall()
    con.close()
    brains = []
    for row in rows:
        item = dict(row)
        item["pinned"] = item["brain_name"] in pins or item["brain_slug"] in pins
        item["output_folder_name"] = Path(item["output_dir"]).name
        item["has_project_topology"] = (Path(item["output_dir"]) / "project" / "topology" / "project_master_topology.mmd").exists()
        try:
            visible_versions = list_brain_versions(workspace, item["brain_name"], verify_hashes=False).get("versions") or []
        except Exception:
            visible_versions = []
        current_version = visible_versions[0] if visible_versions else {}
        item["version_count"] = len(visible_versions)
        item["rollback_count"] = max(0, len(visible_versions) - 1)
        item["current_version_id"] = str(current_version.get("version_id") or "")
        item["current_snapshot_hash"] = str(current_version.get("snapshot_hash") or "")
        item["current_version_at"] = str(current_version.get("timestamp_utc") or item.get("updated_at") or "")
        item["current_version_summary"] = str(current_version.get("change_summary") or current_version.get("reason") or "")
        item["profile_identity"] = (
            f"{item['brain_name']} · {item['current_version_id']}"
            if item["current_version_id"]
            else f"{item['brain_name']} · unversioned"
        )
        brains.append(item)
    return brains


_CATALOG_WORKSPACE_CACHE_SCHEMA = "EVIDENCEOS_CATALOG_WORKSPACE_CACHE_V1"
_CATALOG_WORKSPACE_CACHE_LOCK = threading.RLock()


def _catalog_workspace_cache_path() -> Path:
    return roaming_config_root() / "brain-catalog-workspaces.json"


def _native_catalog_cache_has_noncanonical_routes(payload: Mapping[str, Any]) -> bool:
    if not native_production_mode():
        return False
    canonical = default_workspace_dir().resolve()
    route_keys = {"active_root", "output_dir", "root_path", "workspace", "workspace_dir", "workspaces"}

    def outside_canonical(value: Any, key: str = "") -> bool:
        if isinstance(value, Mapping):
            return any(outside_canonical(child, str(child_key)) for child_key, child in value.items())
        if isinstance(value, list):
            return any(outside_canonical(child, key) for child in value)
        if key not in route_keys or not isinstance(value, str) or not value.strip():
            return False
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return True
        try:
            common = os.path.commonpath([str(canonical), str(candidate.resolve())])
        except (OSError, ValueError):
            return True
        return os.path.normcase(common) != os.path.normcase(str(canonical))

    return outside_canonical(payload)


def _catalog_workspace_cache_load() -> dict[str, Any]:
    path = _catalog_workspace_cache_path()
    if not path.is_file():
        return {"schema": _CATALOG_WORKSPACE_CACHE_SCHEMA, "roots": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": _CATALOG_WORKSPACE_CACHE_SCHEMA, "roots": {}}
    if payload.get("schema") != _CATALOG_WORKSPACE_CACHE_SCHEMA or not isinstance(payload.get("roots"), dict):
        return {"schema": _CATALOG_WORKSPACE_CACHE_SCHEMA, "roots": {}}
    if _native_catalog_cache_has_noncanonical_routes(payload):
        return {"schema": _CATALOG_WORKSPACE_CACHE_SCHEMA, "roots": {}}
    return payload


def _catalog_workspace_cache_write(payload: Mapping[str, Any]) -> None:
    target = _catalog_workspace_cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["schema"] = _CATALOG_WORKSPACE_CACHE_SCHEMA
    body["updated_unix_ns"] = time.time_ns()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _catalog_workspace_cache_key(route_root: Path) -> str:
    return os.path.normcase(str(route_root.expanduser().resolve()))


def _catalog_snapshot_fingerprint(
    history: Mapping[str, Any],
    cache: Mapping[str, Any],
) -> str | None:
    """Hash registered routes plus persisted workspace database identities.

    Ordinary boots must not reopen every legacy workspace database just to
    rebuild the same brain rail.  The durable workspace-path cache is the
    authority for registered routes until an explicit add/drop/refresh.  A
    cheap stat fingerprint still invalidates the serialized catalog when a
    governed workspace database actually changes.
    """

    roots = history.get("roots") if isinstance(history, Mapping) else None
    cached_roots = cache.get("roots") if isinstance(cache, Mapping) else None
    if not isinstance(roots, list) or not isinstance(cached_roots, Mapping):
        return None
    route_rows: list[dict[str, Any]] = []
    for root_row in roots:
        if not isinstance(root_row, Mapping):
            return None
        raw_path = str(root_row.get("path") or "").strip()
        if not raw_path:
            return None
        route_root = Path(raw_path).expanduser().resolve()
        cached = cached_roots.get(_catalog_workspace_cache_key(route_root))
        if not isinstance(cached, Mapping) or not isinstance(cached.get("workspaces"), list):
            if not route_root.is_dir():
                route_rows.append(
                    {
                        "path": os.path.normcase(str(route_root)),
                        "active": bool(root_row.get("active")),
                        "route_exists": False,
                        "workspaces": [],
                    }
                )
                continue
            return None
        workspace_rows: list[dict[str, Any]] = []
        for raw_workspace in cached["workspaces"]:
            workspace = Path(str(raw_workspace)).expanduser().resolve()
            database = workspace / "workspace.sqlite"
            try:
                stat = database.stat()
                workspace_rows.append(
                    {
                        "workspace": os.path.normcase(str(workspace)),
                        "database_size": stat.st_size,
                        "database_mtime_ns": stat.st_mtime_ns,
                    }
                )
            except OSError:
                workspace_rows.append(
                    {
                        "workspace": os.path.normcase(str(workspace)),
                        "database_size": None,
                        "database_mtime_ns": None,
                    }
                )
        route_rows.append(
            {
                "path": os.path.normcase(str(route_root)),
                "active": bool(root_row.get("active")),
                "route_exists": True,
                "workspaces": workspace_rows,
            }
        )
    body = {
        "active_root": os.path.normcase(str(history.get("active_root") or "")),
        "routes": route_rows,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _catalog_workspace_dirs(route_root: Path, *, refresh: bool = False) -> list[Path]:
    """Find app workspaces beneath one governed active or legacy root.

    The active root is itself a workspace. A legacy route may additionally
    contain isolated stress/audit workspaces. Scanning never creates, moves,
    or deletes data inside a legacy route.
    """

    resolved_root = route_root.resolve()
    cache_key = _catalog_workspace_cache_key(resolved_root)
    with _CATALOG_WORKSPACE_CACHE_LOCK:
        cache = _catalog_workspace_cache_load()
        roots = cache.setdefault("roots", {})
        cached = roots.get(cache_key)
        if not refresh and isinstance(cached, dict) and isinstance(cached.get("workspaces"), list):
            # Active and legacy routes remain registered until the user drops
            # them. Reusing this durable discovery set avoids recursively
            # walking every legacy root whenever the selected brain changes.
            return [Path(str(value)).expanduser().resolve() for value in cached["workspaces"]]

    if not resolved_root.is_dir():
        return []
    candidates: dict[str, Path] = {}
    direct = resolved_root / "workspace.sqlite"
    if direct.is_file():
        candidates[os.path.normcase(str(resolved_root))] = resolved_root
    for database in resolved_root.rglob("workspace.sqlite"):
        if database.is_file():
            workspace = database.parent.resolve()
            candidates[os.path.normcase(str(workspace))] = workspace
    workspaces = sorted(
        candidates.values(),
        key=lambda value: (value != resolved_root, len(value.parts), os.path.normcase(str(value))),
    )

    with _CATALOG_WORKSPACE_CACHE_LOCK:
        cache = _catalog_workspace_cache_load()
        roots = cache.setdefault("roots", {})
        roots[cache_key] = {
            "root_path": str(resolved_root),
            "workspaces": [str(value) for value in workspaces],
            "scanned_unix_ns": time.time_ns(),
        }
        _catalog_workspace_cache_write(cache)
    return workspaces


def _refresh_catalog_workspace_root(route_root: Path) -> list[Path]:
    return _catalog_workspace_dirs(route_root, refresh=True)


def _forget_catalog_workspace_root(route_root: Path) -> None:
    cache_key = _catalog_workspace_cache_key(route_root)
    with _CATALOG_WORKSPACE_CACHE_LOCK:
        cache = _catalog_workspace_cache_load()
        roots = cache.setdefault("roots", {})
        if roots.pop(cache_key, None) is not None:
            cache.pop("catalog_snapshot", None)
            _catalog_workspace_cache_write(cache)


def _retain_only_catalog_workspace_root(route_root: Path) -> None:
    cache_key = _catalog_workspace_cache_key(route_root)
    with _CATALOG_WORKSPACE_CACHE_LOCK:
        cache = _catalog_workspace_cache_load()
        roots = cache.setdefault("roots", {})
        retained = roots.get(cache_key)
        cache["roots"] = {cache_key: retained} if isinstance(retained, Mapping) else {}
        cache.pop("catalog_snapshot", None)
        _catalog_workspace_cache_write(cache)


def _brain_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Aggregate brains across active and legacy roots with stable routes."""

    history = list_workspace_roots()
    if not refresh:
        with _CATALOG_WORKSPACE_CACHE_LOCK:
            cache = _catalog_workspace_cache_load()
            fingerprint = _catalog_snapshot_fingerprint(history, cache)
            snapshot = cache.get("catalog_snapshot")
            if (
                fingerprint
                and isinstance(snapshot, Mapping)
                and snapshot.get("fingerprint") == fingerprint
                and isinstance(snapshot.get("payload"), Mapping)
            ):
                restored = json.loads(
                    json.dumps(snapshot["payload"], ensure_ascii=False)
                )
                restored["catalog_restore_mode"] = "DURABLE_FULL_BRAIN_CATALOG_SNAPSHOT"
                restored["raw_legacy_route_rescan"] = False
                return restored

    rows: list[dict[str, Any]] = []
    isolated_errors: list[dict[str, str]] = []
    roots = history.get("roots") if isinstance(history, dict) else []
    for root_row in roots if isinstance(roots, list) else []:
        if not isinstance(root_row, dict):
            continue
        raw_path = str(root_row.get("path") or "").strip()
        if not raw_path:
            continue
        try:
            route_root = Path(raw_path).expanduser().resolve()
            workspaces = _catalog_workspace_dirs(route_root, refresh=refresh)
        except (OSError, RuntimeError) as exc:
            isolated_errors.append({"root_path": raw_path, "error": f"LEGACY_ROOT_SCAN_FAILED:{type(exc).__name__}"})
            continue
        if not workspaces and not route_root.exists():
            isolated_errors.append({"root_path": str(route_root), "error": "LEGACY_ROOT_NOT_FOUND"})
            continue
        for catalog_workspace in workspaces:
            try:
                discovery = (
                    _discover_existing_brains(catalog_workspace)
                    if refresh
                    else {
                        "status": "RESTORED_FROM_PERSISTED_WORKSPACE_CATALOG",
                        "raw_directory_rescan": False,
                    }
                )
                workspace_brains = _list_brains(catalog_workspace)
            except Exception as exc:
                isolated_errors.append(
                    {
                        "root_path": str(route_root),
                        "workspace_dir": str(catalog_workspace),
                        "error": f"WORKSPACE_CATALOG_FAILED:{type(exc).__name__}",
                    }
                )
                continue
            for brain in workspace_brains:
                catalog_id = "brain_catalog_" + hashlib.sha256(
                    (
                        os.path.normcase(str(catalog_workspace))
                        + "\x1f"
                        + str(brain.get("brain_id") or brain.get("brain_name") or "")
                    ).encode("utf-8")
                ).hexdigest()[:24]
                item = dict(brain)
                item.update(
                    {
                        "catalog_id": catalog_id,
                        "brain_name": str(brain.get("brain_name") or ""),
                        "workspace_dir": str(catalog_workspace),
                        "root_path": str(route_root),
                        "root_display_name": str(root_row.get("display_name") or route_root.name or route_root),
                        "root_source": str(root_row.get("source") or ""),
                        "root_added_at": root_row.get("added_at"),
                        "root_last_used_at": root_row.get("last_used_at"),
                        "active_root": bool(root_row.get("active")),
                        "legacy_root": not bool(root_row.get("active")),
                        "discovery_status": str(discovery.get("status") or "PASS"),
                    }
                )
                rows.append(item)

    name_counts: dict[str, int] = {}
    for item in rows:
        key = str(item.get("brain_name") or "").casefold()
        name_counts[key] = name_counts.get(key, 0) + 1
    used_display_names: set[str] = set()
    for item in rows:
        brain_name = str(item.get("brain_name") or "Unnamed Brain")
        display_name = brain_name
        if name_counts.get(brain_name.casefold(), 0) > 1:
            workspace_label = Path(str(item.get("workspace_dir") or "")).name or str(item.get("root_display_name") or "Legacy")
            display_name = f"{brain_name} · {workspace_label}"
        if display_name.casefold() in used_display_names:
            display_name = f"{display_name} · {str(item.get('catalog_id') or '')[-6:]}"
        used_display_names.add(display_name.casefold())
        item["display_name"] = display_name

    rows.sort(key=lambda item: str(item.get("display_name") or "").casefold())
    rows.sort(key=lambda item: str(item.get("updated_at") or item.get("current_version_at") or ""), reverse=True)
    rows.sort(key=lambda item: not bool(item.get("active_root")))
    result = {
        "schema": "T023_GOVERNED_BRAIN_CATALOG_V1",
        "status": "PASS_WITH_ISOLATED_ROOT_ERRORS" if isolated_errors else "PASS",
        "active_root": str(history.get("active_root") or ""),
        "roots": roots if isinstance(roots, list) else [],
        "brains": rows,
        "brain_count": len(rows),
        "isolated_errors": isolated_errors,
        "data_moved": False,
        "data_deleted": False,
        "catalog_restore_mode": "EXPLICIT_REFRESH_REBUILT" if refresh else "CACHE_MISS_REBUILT_ONCE",
        "raw_legacy_route_rescan": bool(refresh),
    }
    with _CATALOG_WORKSPACE_CACHE_LOCK:
        cache = _catalog_workspace_cache_load()
        fingerprint = _catalog_snapshot_fingerprint(history, cache)
        if fingerprint:
            cache["catalog_snapshot"] = {
                "schema": "EVIDENCEOS_DURABLE_BRAIN_CATALOG_SNAPSHOT_V1",
                "fingerprint": fingerprint,
                "payload": result,
                "created_unix_ns": time.time_ns(),
            }
            _catalog_workspace_cache_write(cache)
    return result


def _sources_for(workspace: Path, brain_name: str) -> list[dict[str, Any]]:
    con = _connect_workspace(workspace)
    sources = _setting(con, _source_key(brain_name), [])
    con.close()
    if not isinstance(sources, list):
        return []
    normalized: list[dict[str, Any]] = []
    changed = False
    for source in sources:
        if not isinstance(source, dict):
            changed = True
            continue
        item = dict(source)
        requested_lane = item.get("lane_key") or "custom"
        try:
            canonical = resolve_lane_id(str(requested_lane), scope="any")
        except UnknownLaneAliasError as exc:
            raise WorkerError(f"UNKNOWN_PERSISTED_LANE_ALIAS:{requested_lane}") from exc
        lane = LANE_DEFS[canonical]
        changed = changed or canonical != requested_lane or item.get("lane_label") != lane["label"]
        item["lane_key"] = canonical
        item["lane_label"] = lane["label"]
        metadata = dict(item.get("metadata") or {})
        if canonical == "github_code":
            proven = bool(str(metadata.get("repo_url") or "").strip())
            provenance = "EXPLICIT_GITHUB_REPOSITORY_URL" if proven else "UNPROVEN_GITHUB_INTAKE"
            if item.get("intake_provenance") != provenance:
                item["intake_provenance"] = provenance
                changed = True
        elif canonical == "local_code" and item.get("intake_provenance") != "EXPLICIT_LOCAL_FOLDER":
            item["intake_provenance"] = "EXPLICIT_LOCAL_FOLDER"
            changed = True
        normalized.append(item)
    if changed:
        _save_sources(workspace, brain_name, normalized)
    return normalized


def _save_sources(workspace: Path, brain_name: str, sources: list[dict[str, Any]]) -> None:
    canonical_sources: list[dict[str, Any]] = []
    for source in sources:
        item = dict(source)
        requested_lane = item.get("lane_key") or "custom"
        try:
            canonical = resolve_lane_id(str(requested_lane), scope="any")
        except UnknownLaneAliasError as exc:
            raise WorkerError(f"UNKNOWN_LANE_ALIAS:{requested_lane}") from exc
        item["lane_key"] = canonical
        item["lane_label"] = LANE_DEFS[canonical]["label"]
        canonical_sources.append(item)
    con = _connect_workspace(workspace)
    _set_setting(con, _source_key(brain_name), canonical_sources)
    con.close()


def _mark_source_build_status(
    workspace: Path,
    brain_name: str,
    build_sources: list[dict[str, Any]],
    status: str,
    *,
    error_code: str = "",
) -> None:
    source_ids = {str(source.get("source_id") or "") for source in build_sources}
    stored = _sources_for(workspace, brain_name)
    changed = False
    for source in stored:
        if str(source.get("source_id") or "") not in source_ids:
            continue
        source["status"] = status
        metadata = dict(source.get("metadata") or {})
        metadata["last_build_status"] = status.upper()
        metadata["last_build_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if error_code:
            metadata["last_error_code"] = error_code
        else:
            metadata.pop("last_error_code", None)
        source["metadata"] = metadata
        changed = True
    if changed:
        _save_sources(workspace, brain_name, stored)


def _rename_brain(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    current_name = str(payload.get("brain_name") or payload.get("brainName") or "").strip()
    next_name = str(payload.get("new_brain_name") or payload.get("newBrainName") or "").strip()
    if not current_name:
        raise WorkerError("BRAIN_NAME_REQUIRED")
    if not next_name:
        raise WorkerError("NEW_BRAIN_NAME_REQUIRED")
    if current_name == next_name:
        return {"brains": _list_brains(workspace), "summary": _brain_summary(workspace, current_name)}

    con = _connect_workspace(workspace)
    row = con.execute(
        "SELECT brain_id, output_dir FROM brain_project WHERE brain_name=? AND COALESCE(status,'ACTIVE') != 'REMOVED'",
        (current_name,),
    ).fetchone()
    if not row:
        con.close()
        raise WorkerError(f"BRAIN_NOT_FOUND: {current_name}")
    duplicate = con.execute(
        "SELECT 1 FROM brain_project WHERE lower(brain_name)=lower(?) AND COALESCE(status,'ACTIVE') != 'REMOVED'",
        (next_name,),
    ).fetchone()
    if duplicate:
        con.close()
        raise WorkerError(f"BRAIN_NAME_ALREADY_EXISTS: {next_name}")

    brain_id = row["brain_id"]
    old_output = Path(row["output_dir"])
    new_output = brain_output_dir(workspace, next_name)
    moved = False
    if old_output.resolve() != new_output.resolve():
        if new_output.exists():
            con.close()
            raise WorkerError(f"BRAIN_OUTPUT_ALREADY_EXISTS: {new_output}")
        if old_output.exists():
            old_output.rename(new_output)
            moved = True
        else:
            new_output.mkdir(parents=True, exist_ok=True)

    old_prefix = str(old_output)
    new_prefix = str(new_output)
    try:
        con.execute(
            "UPDATE brain_project SET brain_name=?, brain_slug=?, output_dir=?, updated_at=? WHERE brain_id=?",
            (next_name, slugify_name(next_name), new_prefix, time.strftime("%Y-%m-%dT%H:%M:%S"), brain_id),
        )
        con.execute("UPDATE brain_last_state_pointer SET output_folder=? WHERE brain_id=?", (new_prefix, brain_id))
        for table, columns in {
            "brain_version": ("router_db_path", "package_path"),
            "brain_build_event": ("receipt_path",),
            "brain_package_event": ("package_path",),
        }.items():
            for column in columns:
                con.execute(
                    f"UPDATE {table} SET {column}=REPLACE({column}, ?, ?) WHERE brain_id=? AND {column} IS NOT NULL",
                    (old_prefix, new_prefix, brain_id),
                )

        old_source_key = _source_key(current_name)
        new_source_key = _source_key(next_name)
        source_row = con.execute("SELECT value FROM workspace_settings WHERE key=?", (old_source_key,)).fetchone()
        if source_row:
            con.execute(
                "INSERT OR REPLACE INTO workspace_settings(key,value,updated_at) VALUES(?,?,?)",
                (new_source_key, source_row["value"], time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            con.execute("DELETE FROM workspace_settings WHERE key=?", (old_source_key,))

        pins = set(_setting(con, PIN_SETTINGS_KEY, []))
        if current_name in pins:
            pins.discard(current_name)
            pins.add(next_name)
            con.execute(
                "INSERT OR REPLACE INTO workspace_settings(key,value,updated_at) VALUES(?,?,?)",
                (PIN_SETTINGS_KEY, json.dumps(sorted(pins), ensure_ascii=False), time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
        con.commit()
    except Exception:
        con.rollback()
        if moved and new_output.exists() and not old_output.exists():
            new_output.rename(old_output)
        con.close()
        raise
    con.close()
    return {"brains": _list_brains(workspace), "summary": _brain_summary(workspace, next_name)}


def _add_source(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    brain_name = payload.get("brain_name") or payload.get("brainName") or "New Brain"
    requested_lane = payload.get("lane_key") or payload.get("laneKey") or "custom"
    try:
        lane_key = resolve_lane_id(requested_lane, scope="any")
    except UnknownLaneAliasError as exc:
        raise WorkerError(f"UNKNOWN_LANE_ALIAS:{requested_lane}") from exc
    lane = LANE_DEFS[lane_key]
    path = payload.get("path") or ""
    text = payload.get("text") or ""
    display = payload.get("display_name") or payload.get("displayName") or (Path(path).name if path else lane["label"])
    if not path and not text:
        raise WorkerError("SOURCE_REQUIRES_PATH_OR_TEXT")
    if path and not Path(path).exists():
        raise WorkerError(f"SOURCE_PATH_NOT_FOUND: {path}")
    if path:
        source_path = Path(path)
        if source_path.is_file():
            suffix = source_path.suffix.casefold()
            allowed_extensions = {
                str(extension).casefold()
                for extension in lane.get("extensions", ())
                if str(extension).strip()
            }
            if suffix not in allowed_extensions:
                rendered_suffix = suffix or "<no-extension>"
                raise WorkerError(f"LANE_FILE_TYPE_NOT_ALLOWED:{lane_key}:{rendered_suffix}")
    schema_state = _lane_schema_state(workspace, lane_key, brain_name)
    supplied_schema = payload.get("schema_contract") if "schema_contract" in payload else payload.get("schemaContract")
    if lane_key == "custom" and schema_state["origin"] != "user_override":
        if not _schema_contract_text(supplied_schema):
            raise WorkerError("CUSTOM_SCHEMA_CONTRACT_REQUIRED")
        schema, schema_contract, schema_sha256 = _validate_lane_schema_contract(supplied_schema)
        schema_version = int(payload.get("schema_version") or payload.get("schemaVersion") or 0)
        schema_origin = "source_contract"
    else:
        schema = list(schema_state["schema"])
        schema_contract = str(schema_state["schema_contract"])
        schema_sha256 = str(schema_state["schema_sha256"])
        schema_version = int(schema_state["version"])
        schema_origin = str(schema_state["origin"])
    source_type = payload.get("source_type") or payload.get("sourceType") or (
        lane["source_types"][0] if lane.get("source_types") else lane["label"]
    )
    metadata = dict(payload.get("metadata") or {})
    if lane_key == "github_code":
        repo_url = str(metadata.get("repo_url") or payload.get("repo_url") or "").strip()
        if not repo_url:
            raise WorkerError("GITHUB_LANE_REQUIRES_EXPLICIT_REPOSITORY_URL_INTAKE")
        metadata["repo_url"] = _display_repo_url(repo_url)
        intake_provenance = "EXPLICIT_GITHUB_REPOSITORY_URL"
    elif lane_key == "local_code":
        if path and not Path(path).is_dir():
            raise WorkerError("LOCAL_CODE_LANE_REQUIRES_EXPLICIT_FOLDER_INTAKE")
        intake_provenance = "EXPLICIT_LOCAL_FOLDER"
    else:
        intake_provenance = str(payload.get("intake_provenance") or "EXPLICIT_LANE_INTAKE")
    canonical_root = (
        os.path.normcase(str(Path(path).expanduser().resolve()))
        if path
        else "inline:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    )
    source_identity = hashlib.sha256(
        f"{canonical_root}\x1f{lane_key}\x1f{source_type}".encode("utf-8")
    ).hexdigest()
    item = {
        "source_id": "source_" + source_identity[:24],
        "lane_key": lane_key,
        "lane_label": lane["label"],
        "source_type": source_type,
        "display_name": display,
        "path": path,
        "text": text,
        "active": bool(payload.get("active", True)),
        "status": payload.get("status") or "registered",
        "schema_contract": schema_contract,
        "schema": schema,
        "schema_version": schema_version,
        "schema_sha256": schema_sha256,
        "schema_origin": schema_origin,
        "custom_lane_name": payload.get("custom_lane_name") or payload.get("customLaneName") or "",
        "metadata": metadata,
        "intake_provenance": intake_provenance,
        "note": payload.get("note") or "",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    receipt = _write_operation_receipt(
        workspace,
        "sources.add",
        {
            "brain_name": brain_name,
            "source_id": item["source_id"],
            "lane_id": lane_key,
            "display_name": display,
            "source_locator_sha256": hashlib.sha256(canonical_root.encode("utf-8")).hexdigest(),
            "schema_version": schema_version,
            "schema_sha256": schema_sha256,
            "schema_origin": schema_origin,
            "registered": True,
        },
    )
    item["registration_receipt_path"] = receipt["receipt_path"]
    item["registration_receipt_sha256"] = receipt["receipt_sha256"]
    sources = [
        source for source in _sources_for(workspace, brain_name)
        if source.get("source_id") != item["source_id"]
    ]
    sources.append(item)
    _save_sources(workspace, brain_name, sources)
    return item


def _clone_github_source(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    brain_name = payload.get("brain_name") or payload.get("brainName") or "New Brain"
    repo_url = str(payload.get("repo_url") or payload.get("repoUrl") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    token = str(payload.get("token") or "")
    target_name = str(payload.get("target_name") or payload.get("targetName") or "").strip()
    full_history = bool(payload.get("full_history", payload.get("fullHistory", True)))
    pull_existing = bool(payload.get("pull_existing", payload.get("pullExisting", True)))
    if not repo_url:
        raise WorkerError("GITHUB_REPO_URL_REQUIRED")

    staging = workspace / "github_staging"
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / _safe_target_name(repo_url, target_name)
    repo_url_for_cmd = _url_with_token(repo_url, token)

    try:
        if target.exists() and (target / ".git").exists():
            if branch:
                checkout = run_hidden(
                    ["git", "-c", "core.longpaths=true", "-C", str(target), "checkout", branch],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                _git_result_or_raise(checkout, token, "GITHUB_CHECKOUT")
            if pull_existing:
                pull = run_hidden(
                    ["git", "-c", "core.longpaths=true", "-C", str(target), "pull", "--ff-only"],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                _git_result_or_raise(pull, token, "GITHUB_PULL")
        elif target.exists():
            raise WorkerError(f"GITHUB_TARGET_EXISTS_NOT_REPO: {target}")
        else:
            cmd = ["git", "-c", "core.longpaths=true", "clone"]
            if not full_history:
                cmd.extend(["--depth", "1"])
            if branch:
                cmd.extend(["--branch", branch])
            cmd.extend([repo_url_for_cmd, str(target)])
            clone = run_hidden(cmd, capture_output=True, text=True, timeout=1200)
            _git_result_or_raise(clone, token, "GITHUB_CLONE")
    except WorkerError as exc:
        raise WorkerError(_sanitize_token(str(exc), token))

    source = _add_source(
        workspace,
        {
            "brain_name": brain_name,
            "lane_key": "github_code",
            "path": str(target),
            "display_name": f"{_display_repo_url(repo_url)}{(' @ ' + branch) if branch else ''}",
            "source_type": "GitHub Repo",
            "status": "registered",
            "schema_contract": "\n".join(LANE_DEFS["github_code"]["schema"]),
            "metadata": {
                "repo_url": _display_repo_url(repo_url),
                "branch": branch,
                "full_history": full_history,
                "pull_existing": pull_existing,
            },
        },
    )
    return {
        "source": source,
        "sources": _sources_for(workspace, brain_name),
        "local_path": str(target),
        "repo_url": _display_repo_url(repo_url),
        "branch": branch,
        "full_history": full_history,
    }


def _sources_for_build(
    workspace: Path,
    brain_name: str,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored_by_id = {
        str(source.get("source_id") or ""): source
        for source in _sources_for(workspace, brain_name)
        if str(source.get("source_id") or "")
    }
    normalized = []
    for source in sources:
        source_id = str(source.get("source_id") or "")
        item = {
            **dict(stored_by_id.get(source_id) or {}),
            **dict(source),
        }
        requested_lane = item.get("lane_key") or "custom"
        try:
            item["lane_key"] = resolve_lane_id(requested_lane, scope="any")
        except UnknownLaneAliasError as exc:
            raise WorkerError(f"UNKNOWN_LANE_ALIAS:{requested_lane}") from exc
        schema_state = _lane_schema_state(workspace, item["lane_key"], brain_name)
        if schema_state["origin"] == "user_override" or not item.get("schema_contract"):
            item["schema"] = list(schema_state["schema"])
            item["schema_contract"] = schema_state["schema_contract"]
            item["schema_version"] = schema_state["version"]
            item["schema_sha256"] = schema_state["schema_sha256"]
            item["schema_origin"] = schema_state["origin"]
        normalized.append(item)
    return normalized


def _append_request_prompt_source(sources: list[dict[str, Any]], request_text: str | None) -> list[dict[str, Any]]:
    text = (request_text or "").strip()
    if not text:
        return sources
    out = list(sources)
    out.append(
        {
            "source_id": "request_prompt_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24],
            "lane_key": "chat_lineage",
            "lane_label": "Chat Lineage",
            "source_type": "Request Prompt",
            "display_name": "Request Prompt",
            "path": "",
            "text": text,
            "active": True,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    return out


def _brain_summary(workspace: Path, brain_name: str) -> dict[str, Any]:
    out = brain_output_dir(workspace, brain_name)
    sqlite_brain = out / "project" / "project_router.sqlite"
    topology = out / "project" / "topology" / "project_master_topology.mmd"
    local_code_lane = out / "project" / "topology" / "local_code_lane.mmd"
    package_dir = out / "packages"
    packages = []
    if package_dir.exists():
        packages = [
            {"path": str(p), "name": p.name, "size": p.stat().st_size}
            for p in sorted(package_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        ]
    return {
        "brain_name": brain_name,
        "workspace_dir": str(workspace),
        "output_dir": str(out),
        "output_folder_name": out.name,
        "sqlite_brain_path": str(sqlite_brain) if sqlite_brain.exists() else "",
        "topology_path": str(topology) if topology.exists() else "",
        "topology_exists": topology.exists(),
        "local_code_lane_exists": local_code_lane.exists(),
        "packages": packages,
        "sources": _sources_for(workspace, brain_name),
    }


def _safe_brain_versions(workspace: Path, brain_name: str) -> dict[str, Any]:
    try:
        # Selection restores already-sealed version metadata. Full payload
        # hashing remains an explicit Version Control/audit action and must not
        # block the selected-brain pill or rehash unchanged source products.
        return list_brain_versions(workspace, brain_name, verify_hashes=False)
    except Exception:
        out = brain_output_dir(workspace, brain_name)
        return {
            "brain_name": brain_name,
            "brain_output": str(out),
            "version_store": str(out / ".brain_versions"),
            "manifest_path": "",
            "manifest_sha256": "",
            "version_count": 0,
            "versions": [],
        }


def _selected_brain_context(workspace: Path, brain_name: str) -> dict[str, Any]:
    context_started = time.perf_counter()
    summary = _brain_summary(workspace, brain_name)
    brain = next(
        (item for item in _list_brains(workspace) if item.get("brain_name") == brain_name),
        None,
    ) or {
        "brain_id": f"brain:{slugify_name(brain_name)}",
        "brain_name": brain_name,
        "brain_slug": slugify_name(brain_name),
        "output_dir": summary["output_dir"],
    }
    definitions = _lane_defs_payload(workspace, brain_name)
    lane_schemas = {
        lane_id: {
            "lane_id": lane_id,
            "brain_id": brain["brain_id"],
            "brain_name": brain_name,
            "origin": definition["schema_origin"],
            "version": definition["schema_version"],
            "schema": list(definition["schema_contract"]),
            "schema_contract": "\n".join(definition["schema_contract"]),
            "schema_sha256": definition["schema_sha256"],
            "default_schema": list(definition["default_schema_contract"]),
        }
        for lane_id, definition in definitions.items()
    }
    versions = _safe_brain_versions(workspace, brain_name)
    try:
        telemetry_bundle = load_materialized_telemetry_bundle(workspace, brain_name)
    except BrainTelemetryError as exc:
        telemetry_bundle = {
            "schema": "EVIDENCEOS_ATOMIC_ACCEPTED_TELEMETRY_BUNDLE_V1",
            "status": "NOT_MATERIALIZED",
            "brain_name": brain_name,
            "error_code": str(exc),
            "hydration_law": "ORDINARY_VIEW_REFUSES_SCAN_BUILD_REFRESH_OR_FUSE",
            "raw_project_files_reread": 0,
            "index_probe_count": 0,
            "build_count": 0,
            "refresh_count": 0,
            "fuse_count": 0,
            "mutation_count": 0,
        }
    model_output_state = load_model_output_state(workspace, brain_name)
    historical_provider_delta_archive = load_historical_provider_delta_archive(workspace, brain_name)
    pipeline_state_path = _runtime_state_dir(workspace, brain_name, create=False) / "pipeline_state.json"
    pipeline_snapshot: dict[str, Any] | None = None
    pipeline_history: list[dict[str, Any]] = []
    task_event: dict[str, Any] | None = None
    task_history: list[dict[str, Any]] = []
    task_event_source = "UNAVAILABLE"
    if pipeline_state_path.is_file():
        try:
            pipeline_store = PipelineStateStore(pipeline_state_path)
            pipeline_snapshot = pipeline_store.snapshot()
            pipeline_history = pipeline_store.history() if pipeline_snapshot else []
        except Exception:
            # A selected-brain context must remain readable even if a prior
            # interrupted runtime-state file is damaged. The persisted file is
            # left untouched for the integrity report and repair lane.
            pipeline_snapshot = None
            pipeline_history = []
    task_state_path, task_event_source = _task_state_path_for_brain(workspace, brain_name)
    if task_state_path is not None:
        task_event = _read_authoritative_task_snapshot(
            workspace,
            _process_registry(workspace, brain_name, initialize=False),
            brain_name=brain_name,
        )
        task_history, task_event_source = _read_authoritative_task_history(
            workspace,
            brain_name=brain_name,
        )
    if task_event is None and pipeline_snapshot is not None:
        task_event = _legacy_pipeline_event_to_universal(
            workspace,
            brain_name,
            pipeline_snapshot,
        )
        task_history = [
            _legacy_pipeline_event_to_universal(workspace, brain_name, item)
            for item in pipeline_history
        ]
        task_event_source = "LEGACY_PIPELINE_STATE_TRANSLATED"
    pipeline_state = {
        "pipeline": pipeline_snapshot,
        "history": pipeline_history,
        "stages": _backend_stage_states(pipeline_snapshot),
        "task_event": task_event,
        "task_history": task_history,
        "task_event_source": task_event_source,
        "restored_from_disk": pipeline_snapshot is not None or task_event is not None,
    }
    refresh_candidate = get_refresh_status(workspace, brain_name)
    refresh_candidate = {
        **refresh_candidate,
        "automatic_refresh": False,
        "automatic_fuse": False,
        "read_only": True,
    }
    source_reason_ids: list[str] = []
    latest = (versions.get("versions") or [None])[0]
    if latest:
        baseline_sources = latest.get("source_hashes") or {}
        baseline_time = str(latest.get("timestamp_utc") or "")
        for source in summary["sources"]:
            source_id = str(source.get("source_id") or "")
            created_at = str(source.get("created_at") or "")
            if source_id and (
                source_id not in baseline_sources
                or (baseline_time and created_at and created_at.replace(" ", "T") > baseline_time[:19])
            ):
                source_reason_ids.append(source_id)
    reason_ids = sorted(set(source_reason_ids))
    refresh_attention = {
        "classification": "LOCAL_SOURCE_CHANGE_DETECTED" if reason_ids else "NO_PROVEN_CHANGE",
        "self_active": bool(reason_ids),
        "read_only": True,
        "automatic_refresh": False,
        "automatic_fuse": False,
        "reason_ids": reason_ids,
        "source_reason_ids": sorted(set(source_reason_ids)),
        "model_outputs_are_project_change_truth": False,
    }
    output_route = {
        "workspace_dir": summary["workspace_dir"],
        "output_dir": summary["output_dir"],
        "output_folder_name": summary["output_folder_name"],
        "sqlite_brain_path": summary["sqlite_brain_path"],
        "topology_path": summary["topology_path"],
        "selected_brain_only": True,
    }
    context = {
        "context_schema": "T023_SELECTED_BRAIN_CONTEXT_V1",
        "brain_identity": {
            "brain_id": brain["brain_id"],
            "brain_name": brain_name,
            "brain_slug": brain.get("brain_slug") or slugify_name(brain_name),
            "output_dir": brain.get("output_dir") or summary["output_dir"],
        },
        "summary": summary,
        "sources": summary["sources"],
        "lane_schemas": lane_schemas,
        "lane_definitions": definitions,
        "versions": versions,
        "output_route": output_route,
        "packages": summary["packages"],
        "refresh_attention": refresh_attention,
        "refresh_candidate": refresh_candidate,
        "selection_authority": {
            "schema": _SELECTED_BRAIN_AUTHORITY_SCHEMA,
            "brain_id": brain["brain_id"],
            "brain_name": brain_name,
            "workspace_dir": summary["workspace_dir"],
            "output_dir": brain.get("output_dir") or summary["output_dir"],
            "accepted_version_id": str((latest or {}).get("version_id") or ""),
            "accepted_snapshot_hash": str((latest or {}).get("snapshot_hash") or ""),
            "candidate_delta_id": str(refresh_candidate.get("candidate_id") or ""),
            "candidate_status": str(refresh_candidate.get("status") or ""),
            "context_mode": "READ_ONLY_ATOMIC_SELECTED_BRAIN",
        },
        "model_output_state": model_output_state,
        "historical_provider_delta_archive": historical_provider_delta_archive,
        "pipeline_state": pipeline_state,
        "telemetry_bundle": telemetry_bundle,
        "context_reuse_contract": dict(_SELECTION_CONTEXT_REUSE_CONTRACT),
    }
    context_sha256 = hashlib.sha256(
        json.dumps(context, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        **context,
        "context_sha256": context_sha256,
        "generation_token": context_sha256,
        "selection_context_metrics": {
            "backend_context_ms": round((time.perf_counter() - context_started) * 1000, 3),
            "version_hash_verification": False,
            "source_processing": "NOT_INVOKED",
            "telemetry_bundle_status": str(telemetry_bundle.get("status") or "NOT_MATERIALIZED"),
            "telemetry_source_processing": "STORED_BUNDLE_READ_ONLY",
        },
    }


def _record_selection_timing(workspace: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, float] = {}
    for key in ("visible_switch_ms", "complete_context_ms", "backend_context_ms"):
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError) as exc:
            raise WorkerError(f"SELECTION_TIMING_INVALID:{key}") from exc
        if not math.isfinite(value) or value < 0:
            raise WorkerError(f"SELECTION_TIMING_INVALID:{key}")
        values[key] = round(value, 3)
    if values["visible_switch_ms"] > values["complete_context_ms"] + 0.5:
        raise WorkerError("SELECTION_TIMING_ORDER_INVALID")
    context_sha256 = str(payload.get("context_sha256") or "").strip().lower()
    if len(context_sha256) != 64 or any(character not in "0123456789abcdef" for character in context_sha256):
        raise WorkerError("SELECTION_CONTEXT_SHA256_INVALID")
    reuse_contract = payload.get("context_reuse_contract")
    if reuse_contract != _SELECTION_CONTEXT_REUSE_CONTRACT:
        raise WorkerError("SELECTION_CONTEXT_REUSE_CONTRACT_INVALID")
    detail = {
        "brain_display_name": str(payload.get("brain_display_name") or "").strip(),
        "routed_brain_name": str(payload.get("routed_brain_name") or "").strip(),
        "context_sha256": context_sha256,
        **values,
        "context_reuse_contract": dict(_SELECTION_CONTEXT_REUSE_CONTRACT),
        "status": "PASS",
    }
    if not detail["brain_display_name"] or not detail["routed_brain_name"]:
        raise WorkerError("SELECTION_TIMING_BRAIN_IDENTITY_REQUIRED")
    receipt = _write_operation_receipt(workspace, "brain.selection.timing", detail)
    return {"status": "PASS", **detail, "receipt": receipt}


def _project_brief_key(brain_name: str) -> str:
    return "eos_project_brief:" + slugify_name(brain_name)


def _render_project_flash_prompt(base_prompt: str, brain_name: str, project_brief: str, provider: str) -> str:
    provider_label = provider[:1].upper() + provider[1:] if provider else "Configured provider"
    prompt = str(base_prompt or LOCKED_FLASH_PROMPT)
    prompt = prompt.replace("[fill chat name]", brain_name)
    prompt = prompt.replace("[fill brief purpose]", project_brief)
    header = (
        "EVIDENCE-OS-PROVIDER-NEUTRAL-PROJECT-FLASH-V001\n\n"
        f"PROJECT_NAME:\n[{brain_name}]\n\n"
        f"PROJECT_BRIEF:\n[{project_brief}]\n\n"
        f"TARGET_PROVIDER:\n[{provider_label}]\n\n"
        "PROVIDER LAW:\n"
        "Apply this exact project package and brief consistently in ChatGPT, Gemini, Codex, or any other configured provider.\n\n"
    )
    return header + prompt


def _read_project_flash_prompt(workspace: Path, brain_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider") or "configured provider").strip().casefold() or "configured provider"
    if len(provider) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9 ._+-]*", provider):
        raise WorkerError("FLASH_PROVIDER_INVALID")
    supplied_brief = str(payload.get("project_brief") or payload.get("projectBrief") or "").strip()
    con = _connect_workspace(workspace)
    try:
        if supplied_brief:
            if len(supplied_brief) > 16_000:
                raise WorkerError("PROJECT_BRIEF_TOO_LARGE")
            _set_setting(con, _project_brief_key(brain_name), supplied_brief)
            project_brief = supplied_brief
        else:
            project_brief = str(_setting(con, _project_brief_key(brain_name), "Continue from the governed project pointer."))
    finally:
        con.close()
    out = brain_output_dir(workspace, brain_name)
    candidates = [
        out / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
        out / "packages" / f"{slugify_name(brain_name)}_one_upload_package_v001" / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
        out / "packages" / f"{slugify_name(brain_name)}_one_upload_package_v001" / "prompts" / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
    ]
    source_path: Path | None = next((path for path in candidates if path.is_file()), None)
    base_prompt = (
        source_path.read_text(encoding="utf-8", errors="replace")
        if source_path is not None
        else LOCKED_FLASH_PROMPT
    )
    prompt = _render_project_flash_prompt(base_prompt, brain_name, project_brief, provider)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    receipt = _write_operation_receipt(
        workspace,
        "flash.read",
        {
            "brain_name": brain_name,
            "project_brief_sha256": hashlib.sha256(project_brief.encode("utf-8")).hexdigest(),
            "provider": provider,
            "prompt_sha256": prompt_sha256,
            "source_kind": "brain_package" if source_path is not None else "runtime_default",
            "provider_neutral": True,
        },
    )
    return {
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "provider": provider,
        "project_name": brain_name,
        "project_brief": project_brief,
        "source_kind": "brain_package" if source_path is not None else "runtime_default",
        "receipt_path": receipt["receipt_path"],
        "receipt_sha256": receipt["receipt_sha256"],
    }


def _open_path(path: str) -> dict[str, str]:
    target = Path(path).expanduser()
    if not target.exists():
        raise WorkerError(f"OPEN_PATH_NOT_FOUND: {target}")
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        import subprocess

        subprocess.Popen(["open", str(target)])
    else:
        import subprocess

        subprocess.Popen(["xdg-open", str(target)])
    return {"opened": str(target)}


def _progress_event(request_id: str):
    def emit(payload: dict[str, Any]) -> None:
        _emit_authoritative_task_progress(request_id, payload)

    return emit


def _coordination_root(workspace: Path, *, create: bool = True) -> Path:
    path = workspace / ".evidenceos_runtime"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _universal_task_state_path(workspace: Path, *, create: bool = True) -> Path:
    return _coordination_root(workspace, create=create) / "universal_task_event.json"


def _brain_universal_task_state_path(
    workspace: Path,
    brain_name: str,
    *,
    create: bool = True,
) -> Path:
    return _runtime_state_dir(workspace, brain_name, create=create) / "universal_task_event.json"


def _mirror_universal_task_event_for_brain(
    store: UniversalTaskEventStore,
    brain_state_path: Path,
) -> None:
    """Persist the exact latest event plus a brain-owned event history.

    The workspace document remains the live/latest compatibility pointer.  The
    per-brain document prevents selecting another brain from erasing terminal
    Build/Refresh/Fuse truth for the previously selected immutable brain.
    """

    workspace_document = _read_json_state(store.state_path)
    if not workspace_document or not isinstance(workspace_document.get("event"), Mapping):
        raise UniversalTaskEventError("UNIVERSAL_TASK_MIRROR_SOURCE_INVALID")
    event = dict(workspace_document["event"])
    existing = _read_json_state(brain_state_path) or {}
    history = [
        dict(item)
        for item in existing.get("history") or []
        if isinstance(item, Mapping)
    ]
    history.append(event)
    _atomic_write_json(
        brain_state_path,
        {
            "schema_version": int(workspace_document.get("schema_version") or 1),
            "event": event,
            "history": history[-256:],
        },
    )


def _task_state_path_for_brain(workspace: Path, brain_name: str) -> tuple[Path | None, str]:
    brain_state_path = _brain_universal_task_state_path(
        workspace, brain_name, create=False
    )
    if brain_state_path.is_file():
        return brain_state_path, "PER_BRAIN_UNIVERSAL_EVENT"
    workspace_state_path = _universal_task_state_path(workspace, create=False)
    if workspace_state_path.is_file():
        return workspace_state_path, "WORKSPACE_UNIVERSAL_FALLBACK"
    return None, "UNAVAILABLE"


def _task_brain_id(workspace: Path, brain_name: str, payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("brain_id") or payload.get("brainId") or "").strip()
    if explicit:
        return explicit
    database = workspace / "workspace.sqlite"
    if database.is_file():
        try:
            with closing(
                sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=0.2)
            ) as connection:
                row = connection.execute(
                    "SELECT brain_id FROM brain_project WHERE brain_name=? "
                    "AND COALESCE(status,'ACTIVE')<>'REMOVED' ORDER BY updated_at DESC LIMIT 1",
                    (brain_name,),
                ).fetchone()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass
    return f"brain:{slugify_name(brain_name)}"


def _task_lane_id(command: str, payload: Mapping[str, Any]) -> str:
    explicit = str(
        payload.get("lane_id")
        or payload.get("laneId")
        or payload.get("lane_key")
        or payload.get("laneKey")
        or ""
    ).strip()
    if explicit:
        try:
            return resolve_lane_id(explicit, scope="any")
        except UnknownLaneAliasError:
            return explicit
    if command.startswith("brain.refresh."):
        return "refresh_brain"
    if command.startswith("brain.version."):
        return "brain_version_control"
    if command.startswith("brain.portablePackage.") or command.startswith("package.export"):
        return "provider_package"
    if command == "topology.render":
        return "topology"
    if command in {"brain.build", "brain.buildAll"}:
        return "build_command"
    if command == "model.connector.execute":
        return "model_output"
    if command == "chat.lineage.append":
        return "chat_lineage"
    if command.startswith("sources."):
        return "source_intake"
    return "project_state"


def _loaded_and_skipped_lanes(
    command: str,
    payload: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    if command not in {"brain.build", "brain.buildAll"}:
        return [], []
    raw_sources = payload.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    loaded: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping) or not source.get("active", True):
            continue
        requested = str(source.get("lane_key") or source.get("laneKey") or "").strip()
        if not requested:
            continue
        try:
            loaded.add(resolve_lane_id(requested, scope="any"))
        except UnknownLaneAliasError:
            loaded.add(requested)
    return sorted(loaded), sorted(set(LANE_DEFS) - loaded)


def _task_snapshot_identity(workspace: Path, brain_name: str) -> tuple[str, str]:
    accepted_snapshot_id = ""
    candidate_delta_id = ""
    try:
        versions = _safe_brain_versions(workspace, brain_name)
        latest = (versions.get("versions") or [None])[0]
        if isinstance(latest, Mapping):
            accepted_snapshot_id = str(
                latest.get("version_id") or latest.get("snapshot_hash") or ""
            )
    except Exception:
        pass
    try:
        candidate = get_refresh_status(workspace, brain_name)
        candidate_delta_id = str(
            candidate.get("candidate_id") or candidate.get("candidate_delta_id") or ""
        )
    except Exception:
        pass
    return accepted_snapshot_id, candidate_delta_id


def _process_ids(snapshot: Mapping[str, Any] | None, fallback_pid: int) -> list[int]:
    ids = {int(fallback_pid)} if int(fallback_pid) > 0 else set()
    if isinstance(snapshot, Mapping):
        processes = snapshot.get("processes")
        if isinstance(processes, Mapping):
            for raw_pid in processes:
                try:
                    parsed = int(raw_pid)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    ids.add(parsed)
        for raw_pid in snapshot.get("child_pids") or []:
            try:
                parsed = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                ids.add(parsed)
    return sorted(ids)


def _metric_task_changes(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    values = metrics if isinstance(metrics, Mapping) else {}
    return {
        "cpu_percent": values.get("cpu_percent"),
        "gpu_percent": values.get("gpu_percent"),
        "ram_bytes": values.get("working_set_bytes"),
        "disk_read_bytes": values.get("disk_read_bytes"),
        "disk_write_bytes": values.get("disk_write_bytes"),
        "metrics_status": str(values.get("metrics_status") or "NOT_SAMPLED"),
        "metrics_sampled_at": str(values.get("sampled_at") or ""),
        "metric_scope": str(values.get("metric_scope") or "EVIDENCE_LANE_APP_PROCESS_TREE"),
        "machine_wide_values_used": bool(values.get("machine_wide_values_used", False)),
    }


def _stage_tool_id(
    stage_id: str,
    status: str,
    registry_snapshot: Mapping[str, Any] | None,
) -> str:
    if status != "running":
        return ""
    if isinstance(registry_snapshot, Mapping):
        processes = registry_snapshot.get("processes")
        if isinstance(processes, Mapping):
            active_roles = {str(row.get("role") or "") for row in processes.values() if isinstance(row, Mapping)}
            for role, tool_id in (
                ("git", "git"),
                ("mmd_renderer", "mmdc"),
                ("package_compiler", "package"),
            ):
                if role in active_roles:
                    return tool_id
    return {
        "source_validation_registration": "python",
        "sqlite_project_router_sector_creation": "sqlite",
        "per_lane_parsing_chunking_indexing": "sqlite",
        "pointer_router_hash_finalization": "sqlite",
        "project_mmd_generation": "python",
        "svg_png_rendering": "mmdc",
        "chatgpt_package_compilation": "package",
        "gemini_exact10_compilation": "package",
        "package_hash_validation": "python",
        "immutable_version_capture": "sqlite",
    }.get(stage_id, "")


def _start_authoritative_task(
    request_id: str,
    command: str,
    workspace: Path,
    brain_name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    accepted_snapshot_id, candidate_delta_id = _task_snapshot_identity(workspace, brain_name)
    brain_id = _task_brain_id(workspace, brain_name, payload)
    project_id = str(
        payload.get("project_id") or payload.get("projectId") or brain_id
    )
    loaded_lane_ids, skipped_lane_ids = _loaded_and_skipped_lanes(command, payload)
    task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
    if not task_id:
        task_id = "task_" + hashlib.sha256(
            f"{request_id}|{command}|{project_id}|{brain_id}".encode("utf-8")
        ).hexdigest()[:24]
    run_id = str(payload.get("run_id") or payload.get("runId") or request_id or task_id)
    registry = _process_registry(workspace, brain_name, dict(payload))
    registry_snapshot = registry.read_only_snapshot()
    store = UniversalTaskEventStore(_universal_task_state_path(workspace))
    brain_state_path = _brain_universal_task_state_path(workspace, brain_name)
    event = store.start(
        {
            "task_id": task_id,
            "run_id": run_id,
            "request_id": request_id,
            "project_id": project_id,
            "brain_id": brain_id,
            "brain_name": brain_name,
            "workspace_dir": str(workspace.resolve()),
            "accepted_snapshot_id": accepted_snapshot_id,
            "candidate_delta_id": candidate_delta_id,
            "lane_id": _task_lane_id(command, payload),
            "stage_id": "command_start",
            "tool_id": "",
            "process_pid": os.getpid(),
            "process_tree_ids": _process_ids(registry_snapshot, os.getpid()),
            "command": command,
            "total_units": len(loaded_lane_ids) if loaded_lane_ids else 1,
            "loaded_lane_ids": loaded_lane_ids,
            "skipped_lane_ids": skipped_lane_ids,
            "hil_state": str(payload.get("hil_state") or payload.get("hilState") or "NOT_APPLICABLE"),
        }
    )
    _mirror_universal_task_event_for_brain(store, brain_state_path)
    _ACTIVE_TASK_CONTEXTS[request_id] = {
        "store": store,
        "brain_state_path": brain_state_path,
        "registry": registry,
        "pipeline_owned": command == "brain.buildAll",
        "command": command,
    }
    return event


def _emit_authoritative_task_progress(
    request_id: str,
    payload: Mapping[str, Any],
    *,
    registry: ProcessRegistry | None = None,
    pipeline_owned: bool = False,
) -> dict[str, Any]:
    context = _ACTIVE_TASK_CONTEXTS.get(request_id)
    if not context:
        raw = dict(payload)
        _event("task.progress", request_id, raw)
        return raw
    store = context["store"]
    active_registry = registry or context.get("registry")
    registry_snapshot = (
        active_registry.read_only_snapshot() if isinstance(active_registry, ProcessRegistry) else {}
    )
    metrics = (
        active_registry.read_last_metrics() if isinstance(active_registry, ProcessRegistry) else {}
    )
    stage_id = str(payload.get("stage_id") or payload.get("stage") or "")
    current = store.snapshot() or {}
    status = str(payload.get("status") or current.get("status") or "running")
    changes: dict[str, Any] = {
        "status": status,
        "stage_id": stage_id or current.get("stage_id") or "command_running",
        "lane_id": str(payload.get("active_lane") or current.get("lane_id") or ""),
        "active_file": str(payload.get("active_file") or payload.get("file") or ""),
        "active_command": str(
            payload.get("active_command") or payload.get("message") or current.get("active_command") or ""
        ),
        "process_pid": int(payload.get("process_pid") or os.getpid()),
        "process_tree_ids": _process_ids(registry_snapshot, os.getpid()),
        "tool_id": _stage_tool_id(stage_id, status, registry_snapshot),
        **_metric_task_changes(metrics),
    }
    completed = payload.get("files_done")
    if completed is None:
        completed = payload.get("done")
    total = payload.get("files_total")
    if total is None:
        total = payload.get("total")
    if completed is not None:
        changes["completed_units"] = completed
    if total is not None:
        changes["total_units"] = total
    authoritative_pipeline = bool(pipeline_owned or context.get("pipeline_owned"))
    if payload.get("global_percent") is not None:
        changes["progress"] = payload.get("global_percent")
    elif not authoritative_pipeline and payload.get("percent") is not None:
        changes["progress"] = payload.get("percent")
    if payload.get("error_code"):
        changes["failure_code"] = str(payload.get("error_code"))
        changes["error_code"] = str(payload.get("error_code"))
    for compatibility_field in (
        "pipeline_id",
        "request_id",
        "workspace_dir",
        "stage_name",
        "stage_order",
        "stage_count",
        "stage_percent",
        "global_percent",
        "running_count",
        "queued_count",
        "completed_count",
        "elapsed_seconds",
        "eta_seconds",
        "started_at",
        "updated_at",
    ):
        if payload.get(compatibility_field) is not None:
            changes[compatibility_field] = payload.get(compatibility_field)
    event = store.update(changes)
    brain_state_path = context.get("brain_state_path")
    if isinstance(brain_state_path, Path):
        _mirror_universal_task_event_for_brain(store, brain_state_path)
    _event("task.progress", request_id, event)
    return event


def _nested_truth_value(value: Any, keys: tuple[str, ...], *, depth: int = 0) -> Any:
    if depth > 4 or not isinstance(value, Mapping):
        return None
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, "", [], {}):
            return candidate
    for candidate in value.values():
        found = _nested_truth_value(candidate, keys, depth=depth + 1)
        if found not in (None, "", [], {}):
            return found
    return None


def _finish_authoritative_task(
    request_id: str,
    result: Any,
    *,
    error_code: str = "",
) -> dict[str, Any] | None:
    context = _ACTIVE_TASK_CONTEXTS.get(request_id)
    if not context:
        return None
    store: UniversalTaskEventStore = context["store"]
    registry: ProcessRegistry = context["registry"]
    registry_snapshot = registry.read_only_snapshot()
    result_mapping = result if isinstance(result, Mapping) else {}
    metrics = _nested_truth_value(result_mapping, ("process_metrics",))
    if not isinstance(metrics, Mapping):
        metrics = registry.read_last_metrics()
    skipped = bool(result_mapping.get("skipped"))
    hil_state = str(
        _nested_truth_value(result_mapping, ("hil_state", "HIL_state"))
        or (store.snapshot() or {}).get("hil_state")
        or "NOT_APPLICABLE"
    )
    awaiting_hil = "AWAITING" in hil_state.upper() or hil_state.upper().endswith("_PENDING")
    status = "failed" if error_code else "skipped" if skipped else "hil_waiting" if awaiting_hil else "completed"
    pipeline = _nested_truth_value(result_mapping, ("pipeline",))
    pipeline = pipeline if isinstance(pipeline, Mapping) else {}
    completed_units = pipeline.get("files_done")
    total_units = pipeline.get("files_total")
    candidate_delta_id = _nested_truth_value(
        result_mapping, ("candidate_delta_id", "candidate_id")
    )
    accepted_snapshot_id = _nested_truth_value(
        result_mapping, ("accepted_snapshot_id", "version_id", "snapshot_id")
    )
    next_pointer = _nested_truth_value(
        result_mapping, ("next_pointer", "current_task_pointer", "next_suggested_action")
    )
    skip_reason = _nested_truth_value(result_mapping, ("skip_reason", "reason")) if skipped else ""
    loaded_lane_ids = _nested_truth_value(result_mapping, ("active_lane_ids", "loaded_lane_ids"))
    skipped_lane_ids = _nested_truth_value(result_mapping, ("unloaded_lane_ids", "unloaded_lanes_not_fired"))
    changes: dict[str, Any] = {
        "status": status,
        "stage_id": str(pipeline.get("stage_id") or "command_stop"),
        "tool_id": "",
        "process_pid": os.getpid(),
        "process_tree_ids": _process_ids(registry_snapshot, os.getpid()),
        "failure_code": error_code,
        "error_code": error_code,
        "running_count": 0,
        "queued_count": 0,
        "skip_reason": str(skip_reason or ""),
        "hil_state": hil_state,
        "next_pointer": str(next_pointer or ""),
        **_metric_task_changes(metrics),
    }
    if status == "completed":
        changes["progress"] = 100
        changes["global_percent"] = 100
        current = store.snapshot() or {}
        if int(current.get("stage_count") or 0) > 0:
            changes["completed_count"] = int(current.get("stage_count") or 0)
    if completed_units is not None:
        changes["completed_units"] = completed_units
    if total_units is not None:
        changes["total_units"] = total_units
    if candidate_delta_id:
        changes["candidate_delta_id"] = str(candidate_delta_id)
    if accepted_snapshot_id:
        changes["accepted_snapshot_id"] = str(accepted_snapshot_id)
    if isinstance(loaded_lane_ids, (list, tuple, set)):
        changes["loaded_lane_ids"] = sorted(set(str(item) for item in loaded_lane_ids))
    if isinstance(skipped_lane_ids, (list, tuple, set)):
        changes["skipped_lane_ids"] = sorted(set(str(item) for item in skipped_lane_ids))
    event = store.update(changes)
    brain_state_path = context.get("brain_state_path")
    if isinstance(brain_state_path, Path):
        _mirror_universal_task_event_for_brain(store, brain_state_path)
    return event


def _read_authoritative_task_snapshot(
    workspace: Path,
    registry: ProcessRegistry,
    *,
    brain_name: str = "",
) -> dict[str, Any] | None:
    task_state_path, _source = _task_state_path_for_brain(workspace, brain_name)
    if task_state_path is None:
        return None
    store = UniversalTaskEventStore(task_state_path)
    try:
        event = store.snapshot()
    except UniversalTaskEventError:
        return None
    if event is None:
        return None
    expected_brain_name = str(brain_name or "").strip()
    event_brain_name = str(event.get("brain_name") or "").strip()
    if expected_brain_name and event_brain_name != expected_brain_name:
        return None
    if str(event.get("status") or "") != "running":
        # Terminal values are immutable historical evidence. Never replace
        # their final PID-tree metrics with the newly opened app process.
        return event
    registry_snapshot = registry.read_only_snapshot()
    metrics = registry.read_last_metrics()
    return {
        **event,
        "process_tree_ids": _process_ids(registry_snapshot, int(event.get("process_pid") or 0)),
        **_metric_task_changes(metrics),
    }


def _read_authoritative_task_history(
    workspace: Path,
    *,
    brain_name: str,
) -> tuple[list[dict[str, Any]], str]:
    task_state_path, source = _task_state_path_for_brain(workspace, brain_name)
    if task_state_path is None:
        return [], source
    try:
        events = UniversalTaskEventStore(task_state_path).history()
    except UniversalTaskEventError:
        return [], source
    expected = str(brain_name or "").strip()
    return [
        event for event in events
        if not expected or str(event.get("brain_name") or "").strip() == expected
    ], source


def _legacy_pipeline_event_to_universal(
    workspace: Path,
    brain_name: str,
    pipeline_event: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate persisted pre-universal pipeline truth without inventing it."""

    pipeline_id = str(pipeline_event.get("pipeline_id") or "legacy_pipeline")
    request_id = str(pipeline_event.get("request_id") or pipeline_id)
    brain_id = _task_brain_id(workspace, brain_name, {})
    process_pid = max(0, int(pipeline_event.get("process_pid") or 0))
    status = str(pipeline_event.get("status") or "queued").lower()
    if status not in {"queued", "running", "completed", "failed", "cancelled"}:
        status = "failed"
    started_at = str(pipeline_event.get("started_at") or "")
    updated_at = str(pipeline_event.get("updated_at") or started_at)
    terminal = status in {"completed", "failed", "cancelled"}
    task_identity = hashlib.sha256(
        f"legacy|{brain_id}|{pipeline_id}|{request_id}".encode("utf-8")
    ).hexdigest()[:24]
    event: dict[str, Any] = {
        "schema": UNIVERSAL_TASK_EVENT_SCHEMA,
        "task_id": f"task_{task_identity}",
        "run_id": pipeline_id,
        "request_id": request_id,
        "project_id": brain_id,
        "brain_id": brain_id,
        "brain_name": brain_name,
        "workspace_dir": str(workspace.resolve()),
        "accepted_snapshot_id": "",
        "candidate_delta_id": "",
        "lane_id": str(pipeline_event.get("active_lane") or "build_command"),
        "stage_id": str(pipeline_event.get("stage_id") or "command_stop"),
        "stage_name": str(pipeline_event.get("stage_name") or "Persisted pipeline state"),
        "stage_order": max(0, int(pipeline_event.get("stage_order") or 0)),
        "stage_count": max(0, int(pipeline_event.get("stage_count") or len(PIPELINE_STAGES))),
        "stage_percent": float(pipeline_event.get("stage_percent") or 0.0),
        "global_percent": float(pipeline_event.get("global_percent") or 0.0),
        "tool_id": "",
        "process_pid": process_pid,
        "process_tree_ids": [process_pid] if process_pid else [],
        "start_timestamp": started_at,
        "update_timestamp": updated_at,
        "stop_timestamp": updated_at if terminal else "",
        "hil_timestamp": "",
        "status": status,
        "completed_units": max(0, int(pipeline_event.get("files_done") or 0)),
        "total_units": max(0, int(pipeline_event.get("files_total") or 0)),
        "cpu_percent": None,
        "gpu_percent": None,
        "ram_bytes": None,
        "disk_read_bytes": None,
        "disk_write_bytes": None,
        "progress": float(pipeline_event.get("global_percent") or 0.0),
        "failure_code": str(pipeline_event.get("error_code") or ""),
        "skip_reason": "",
        "hil_state": "NOT_RECORDED_BY_LEGACY_PIPELINE",
        "receipt_hash": "",
        "next_pointer": "",
        "command": "brain.buildAll",
        "active_command": str(pipeline_event.get("active_command") or "brain.buildAll"),
        "active_file": str(pipeline_event.get("active_file") or ""),
        "loaded_lane_ids": [],
        "skipped_lane_ids": [],
        "metric_scope": "LEGACY_PIPELINE_NO_PROCESS_METRIC_SNAPSHOT",
        "metrics_status": "NOT_RECORDED_BY_LEGACY_PIPELINE",
        "machine_wide_values_used": False,
        "pipeline_id": pipeline_id,
        "running_count": max(0, int(pipeline_event.get("running_count") or 0)),
        "queued_count": max(0, int(pipeline_event.get("queued_count") or 0)),
        "completed_count": max(0, int(pipeline_event.get("completed_count") or 0)),
        "elapsed_seconds": max(0.0, float(pipeline_event.get("elapsed_seconds") or 0.0)),
        "eta_seconds": pipeline_event.get("eta_seconds"),
        "started_at": started_at,
        "updated_at": updated_at,
        "error_code": str(pipeline_event.get("error_code") or ""),
        "restored_event_source": "LEGACY_PIPELINE_STATE_TRANSLATED",
    }
    if terminal:
        canonical_body = {key: value for key, value in event.items() if key != "receipt_hash"}
        event["receipt_hash"] = hashlib.sha256(
            json.dumps(
                canonical_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest().upper()
    return event


def _brain_runtime_key(workspace: Path, brain_name: str) -> str:
    database = workspace / "workspace.sqlite"
    if database.exists():
        try:
            uri = f"file:{database.as_posix()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as connection:
                row = connection.execute(
                    "SELECT brain_id FROM brain_project WHERE name = ? LIMIT 1",
                    (brain_name,),
                ).fetchone()
            if row and row[0]:
                return str(row[0])
        except sqlite3.Error:
            pass
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", brain_name).strip("-.")
    return slug or hashlib.sha256(brain_name.encode("utf-8")).hexdigest()[:16]


def _process_create_time(pid: int) -> float | None:
    if _psutil is None:
        return None
    try:
        return float(_psutil.Process(pid).create_time())
    except Exception:
        return None


def _discard_stale_mutation_lock(lock_path: Path) -> bool:
    """Remove only a lock whose recorded process is proven dead or PID-reused."""

    if _psutil is None:
        return False
    try:
        parts = lock_path.read_text(encoding="utf-8").split("|", 3)
        owner_pid = int(parts[0])
    except (OSError, ValueError, IndexError):
        return False
    expected_create_time: float | None = None
    if len(parts) >= 4:
        try:
            expected_create_time = float(parts[1])
        except ValueError:
            expected_create_time = None
    try:
        owner = _psutil.Process(owner_pid)
        actual_create_time = float(owner.create_time())
    except Exception as exc:
        no_such_process = getattr(_psutil, "NoSuchProcess", ())
        if not no_such_process or not isinstance(exc, no_such_process):
            return False
        actual_create_time = None
    stale = actual_create_time is None or (
        expected_create_time is not None and abs(expected_create_time - actual_create_time) > 0.01
    )
    if not stale:
        return False
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _runtime_state_dir(workspace: Path, brain_name: str, *, create: bool = True) -> Path:
    path = brain_output_dir(workspace, brain_name) / "project" / "runtime"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _reusable_full_build_products(
    workspace: Path,
    brain_name: str,
    brain_root: str | Path,
    expected_package_use_mode: str | None = None,
) -> dict[str, Any] | None:
    """Return prior validated products only when every required artifact exists.

    This is the no-change fast path.  It deliberately avoids re-reading and
    re-hashing multi-gigabyte authoritative trees; changed source metadata
    prevents this function from being used before it is called.
    """

    root = Path(brain_root).resolve()
    topology_files = [
        root / "env" / "env_mmd.mmd",
        root / "env" / "env_mmd.svg",
        root / "env" / "env_mmd.png",
        root / "uop" / "uop_mmd.mmd",
        root / "uop" / "uop_mmd.svg",
        root / "uop" / "uop_mmd.png",
        root / "project" / "topology" / "project_master_topology.mmd",
        root / "project" / "topology" / "project_master_topology.svg",
        root / "project" / "topology" / "project_master_topology.png",
        root / "project" / "topology" / "project_master_topology_HD.png",
    ]
    if any(not path.is_file() or path.stat().st_size <= 0 for path in topology_files):
        return None
    package_dir = root / "packages"
    chatgpt = sorted(package_dir.glob("ChatGPT_*.zip"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    gemini = sorted(package_dir.glob("Gemini_*.zip"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    codex = sorted(package_dir.glob(f"{PACKAGE_FAMILY}_*.zip"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    if not chatgpt or not gemini or not codex or chatgpt[0].stat().st_size <= 0 or gemini[0].stat().st_size <= 0 or codex[0].stat().st_size <= 0:
        return None
    try:
        chatgpt_validation = validate_chatgpt_package(chatgpt[0])
        gemini_validation = validate_gemini_exact10(gemini[0])
        codex_validation = validate_codex_env15_zip(codex[0])
    except Exception:
        return None
    if any(
        validation.get("status") != "PASS"
        for validation in (chatgpt_validation, gemini_validation, codex_validation)
    ):
        return None
    # Source fingerprints alone are insufficient for product reuse. A layout,
    # theme, or renderer correction can change the live Project topology while
    # every source sector remains byte-identical. Require the ChatGPT archive
    # (the provider-chain source package) to carry the exact current topology
    # bytes before allowing the downstream Gemini/Codex receipts to be reused.
    project_topology_members = {
        "project/topology/project_master_topology.mmd": root / "project" / "topology" / "project_master_topology.mmd",
        "project/topology/project_master_topology.svg": root / "project" / "topology" / "project_master_topology.svg",
        "project/topology/project_master_topology.png": root / "project" / "topology" / "project_master_topology.png",
        "project/topology/project_master_topology_HD.png": root / "project" / "topology" / "project_master_topology_HD.png",
    }
    try:
        with zipfile.ZipFile(chatgpt[0]) as package:
            if expected_package_use_mode is not None:
                try:
                    package_use_mode = json.loads(
                        package.read("manifests/PACKAGE_USE_MODE.json").decode("utf-8")
                    )
                except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                    return None
                if str(package_use_mode.get("mode") or "").strip().upper() != str(
                    expected_package_use_mode
                ).strip().upper():
                    return None
            if any(
                member not in package.namelist() or package.read(member) != live_path.read_bytes()
                for member, live_path in project_topology_members.items()
            ):
                return None
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    versions = list_brain_versions(workspace, brain_name, verify_hashes=True)
    latest = (versions.get("versions") or [None])[0]
    if not latest:
        return None
    validation = {
        "status": "PASS",
        "errors": [],
        "validation_mode": "REOPENED_PACKAGES_AND_HASH_VERIFIED_PRIOR_VERSION_REUSE",
    }
    return {
        "mmd": {"status": "REUSED_UNCHANGED", "files": [str(path) for path in topology_files if path.suffix == ".mmd"]},
        "render": {"status": "REUSED_UNCHANGED", "rendered": [{"status": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS", "path": str(path)} for path in topology_files if path.suffix == ".png"]},
        "chatgpt": {
            "status": "REUSED_UNCHANGED",
            "package_folder": str(root / "packages" / f"{slugify_name(brain_name)}_one_upload_package_v001"),
            "package_zip": str(chatgpt[0]),
            "validation": dict(chatgpt_validation),
        },
        "gemini": {"status": "REUSED_UNCHANGED", "gemini_package_zip": str(gemini[0]), "validation": dict(gemini_validation)},
        "codex": {"status": "REUSED_UNCHANGED", "package_family": PACKAGE_FAMILY, "archive": str(codex[0]), "validation": dict(codex_validation), "archive_validation": dict(codex_validation)},
        "version": {"status": "REUSED_UNCHANGED", **latest},
        "validation": validation,
    }


def _remove_uncompressed_provider_stage(
    brain_root: str | Path,
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return result
    raw_stage = str(result.get("package_folder") or "").strip()
    if not raw_stage:
        result.setdefault("staging_removed", bool(result.get("skipped")))
        return result
    packages = (Path(brain_root).resolve() / "packages").resolve()
    stage = Path(raw_stage).resolve()
    if stage == packages or packages not in stage.parents:
        raise WorkerError(f"PROVIDER_STAGE_CLEANUP_PATH_ESCAPE:{stage}")
    if stage.exists():
        if not stage.is_dir():
            raise WorkerError(f"PROVIDER_STAGE_CLEANUP_NOT_DIRECTORY:{stage}")
        remove_provider_stage(stage)
    result["package_folder_removed"] = raw_stage
    result["package_folder"] = ""
    result["staging_removed"] = True
    return result


def _hash_bound_export_validation(
    result: Mapping[str, Any] | None,
    archive_key: str,
) -> dict[str, Any] | None:
    """Reuse an exporter's completed validation only when it is hash-bound.

    Both provider exporters reopen and validate the final archive before they
    return. Repeating that SQLite/ZIP audit in the immediately following stage
    is redundant. Any missing or mismatched binding returns ``None`` so the
    caller still fails closed by reopening the archive.
    """
    if not isinstance(result, Mapping):
        return None
    validation = result.get("validation")
    if not isinstance(validation, Mapping) or validation.get("status") != "PASS":
        return None
    archive_value = str(result.get(archive_key) or "").strip()
    result_sha256 = str(result.get("sha256") or "").strip().upper()
    validation_sha256 = str(validation.get("sha256") or "").strip().upper()
    validation_package = str(validation.get("package") or "").strip()
    if (
        not archive_value
        or not Path(archive_value).is_file()
        or len(result_sha256) != 64
        or result_sha256 != validation_sha256
    ):
        return None
    if validation_package and Path(validation_package).resolve() != Path(archive_value).resolve():
        return None
    return dict(validation)


def _build_package_governance(request_id: str, brain_name: str, payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_goal = payload.get("goal_pointer") or payload.get("goalPointer")
    raw_delta = payload.get("delta_ledger") or payload.get("deltaLedger")
    run_id = str(request_id or f"build-{time.time_ns()}")
    brain_slug = slugify_name(brain_name)
    goal_pointer = dict(raw_goal) if isinstance(raw_goal, dict) else {
        "parent_goal_id": f"EVIDENCEOS_BUILD_COMMAND:{brain_slug}",
        "active_run_id": run_id,
        "current_task_pointer": "BRAIN_BUILD_ALL_VALIDATED_PASS",
        "active_lane_id": "build_command",
        "brain_name": brain_name,
    }
    delta_ledger = dict(raw_delta) if isinstance(raw_delta, dict) else {
        "delta_id": "BUILD_COMMAND_DELTA_" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24],
        "delta_title": "Build Command operational delta",
        "status": "OPEN_REGISTERED",
        "prompt_pointer": "brain.buildAll",
        "insertion_reason": "SAME_VALIDATED_PASS_CODEX_PACKAGE",
        "insertion_point": "package_hash_validation",
    }
    if not goal_pointer.get("parent_goal_id") or not goal_pointer.get("current_task_pointer"):
        raise WorkerError("BUILD_CODEX_GOAL_POINTER_REQUIRED")
    if not delta_ledger.get("delta_id"):
        raise WorkerError("BUILD_CODEX_DELTA_LEDGER_REQUIRED")
    return goal_pointer, delta_ledger


def _latest_good_snapshot_for_package(workspace: Path, brain_name: str) -> dict[str, Any]:
    versions = list_brain_versions(workspace, brain_name, verify_hashes=True)
    latest = (versions.get("versions") or [None])[0]
    if not latest:
        return {"status": "UNVERIFIED", "rollback_available": False}
    return {
        "status": "VERIFIED",
        "rollback_available": True,
        "version_id": latest.get("version_id"),
        "snapshot_hash": latest.get("snapshot_hash"),
        "record_sha256": latest.get("record_sha256"),
        "timestamp_utc": latest.get("timestamp_utc"),
    }


def _process_registry(
    workspace: Path,
    brain_name: str,
    payload: dict[str, Any] | None = None,
    *,
    initialize: bool = True,
    register_python_worker: bool = True,
) -> ProcessRegistry:
    registry_path = _coordination_root(workspace, create=initialize) / "process_registry.json"
    registry = ProcessRegistry(
        registry_path,
        gpu_sampler=windows_gpu_pid_sampler,
    )
    if not initialize:
        return registry
    values = payload or {}
    raw_tauri_pid = values.get("tauri_pid") or values.get("tauriPid")
    tauri_pid = int(raw_tauri_pid) if raw_tauri_pid else 0
    key = (str(workspace.resolve()), tauri_pid, os.getpid(), register_python_worker)
    with _REGISTRY_INIT_LOCK:
        if key not in _INITIALIZED_REGISTRIES:
            existing = registry.read_only_snapshot()
            existing_tauri_pid = int(existing.get("tauri_pid") or 0) if isinstance(existing, dict) else 0
            # One native app owns the registry. The persistent command worker
            # and the independent metrics worker are sibling descendants of
            # that same Tauri process, so a different Python PID is not an
            # ownership change and must never erase the attributable tree.
            if tauri_pid and existing_tauri_pid and existing_tauri_pid != tauri_pid:
                registry_path.unlink(missing_ok=True)
            registry.initialize(
                tauri_pid=tauri_pid or None,
                python_worker_pid=os.getpid() if register_python_worker else None,
            )
            registry.snapshot(prune_stale=True)
            _INITIALIZED_REGISTRIES.add(key)
    return registry


@contextmanager
def _mutation_coordinator(workspace: Path, brain_name: str, command: str):
    runtime_key = _brain_runtime_key(workspace, brain_name)
    with _MUTATION_LOCKS_GUARD:
        lock = _MUTATION_LOCKS.setdefault(runtime_key, threading.RLock())
    if not lock.acquire(timeout=_MUTATION_LOCK_TIMEOUT_SECONDS):
        raise WorkerError(f"BUSY: mutation already active for {brain_name}")
    lock_dir = _coordination_root(workspace) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{runtime_key}.lock"
    descriptor: int | None = None
    deadline = time.monotonic() + _MUTATION_LOCK_TIMEOUT_SECONDS
    try:
        while descriptor is None:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                created = _process_create_time(os.getpid())
                os.write(
                    descriptor,
                    f"{os.getpid()}|{created if created is not None else ''}|{command}|{time.time_ns()}".encode("utf-8"),
                )
            except FileExistsError:
                if _discard_stale_mutation_lock(lock_path):
                    continue
                if time.monotonic() >= deadline:
                    raise WorkerError(f"BUSY: mutation already active for {brain_name}")
                time.sleep(0.025)
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)
        lock.release()


def _validate_build_sources(
    workspace: Path,
    brain_name: str,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = _sources_for_build(workspace, brain_name, sources)
    active = [source for source in normalized if source.get("active", True)]
    code_intake_modes = {
        str(source.get("lane_key") or "")
        for source in active
        if str(source.get("lane_key") or "") in {"local_code", "github_code"}
    }
    if code_intake_modes == {"local_code", "github_code"}:
        raise WorkerError(
            "LOCAL_AND_GITHUB_CODE_LANES_ARE_MUTUALLY_EXCLUSIVE_ONE_PROJECT_DATABASE"
        )
    for source in active:
        path = str(source.get("path") or "")
        text = str(source.get("text") or "")
        if not path and not text:
            raise WorkerError(f"SOURCE_REQUIRES_PATH_OR_TEXT:{source.get('source_id') or 'unknown'}")
        if path and not Path(path).exists():
            raise WorkerError(f"SOURCE_PATH_NOT_FOUND:{path}")
        lane_key = str(source.get("lane_key") or "")
        metadata = dict(source.get("metadata") or {})
        if lane_key == "github_code":
            if not str(metadata.get("repo_url") or "").strip():
                raise WorkerError("GITHUB_LANE_REQUIRES_EXPLICIT_REPOSITORY_URL_INTAKE")
            source["intake_provenance"] = "EXPLICIT_GITHUB_REPOSITORY_URL"
        elif lane_key == "local_code":
            source["intake_provenance"] = "EXPLICIT_LOCAL_FOLDER"

        locator = (
            "path:" + os.path.normcase(str(Path(path).expanduser().resolve()))
            if path
            else "inline:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        )
        source["canonical_payload_id"] = "payload_" + hashlib.sha256(
            locator.encode("utf-8")
        ).hexdigest()[:24]

    payload_groups: dict[str, list[dict[str, Any]]] = {}
    for source in active:
        payload_groups.setdefault(str(source["canonical_payload_id"]), []).append(source)
    for payload_id, group in payload_groups.items():
        owner = sorted(
            group,
            key=lambda source: (
                0 if str(source.get("lane_key") or "") == "local_code" else 1,
                str(source.get("source_id") or ""),
            ),
        )[0]
        owner_source_id = str(owner.get("source_id") or "")
        for source in group:
            source["canonical_payload_owner_source_id"] = owner_source_id
            source["canonical_payload_mode"] = (
                "OWNER" if source is owner else "REFERENCE_ONLY"
            )
            source["canonical_payload_dedup_status"] = (
                "UNIQUE" if len(group) == 1 else "SHARED_ONE_PHYSICAL_PAYLOAD"
            )
        if len(group) > 1:
            code_lanes = {
                str(source.get("lane_key") or "")
                for source in group
                if str(source.get("lane_key") or "") in {"github_code", "local_code"}
            }
            allowed_provenance = {
                "EXPLICIT_GITHUB_REPOSITORY_URL",
                "EXPLICIT_LOCAL_FOLDER",
            }
            if len(code_lanes) > 1 and any(
                str(source.get("intake_provenance") or "") not in allowed_provenance
                for source in group
            ):
                raise WorkerError(f"CROSS_LANE_PAYLOAD_REQUIRES_EXPLICIT_INTAKES:{payload_id}")
    return normalized


def _project_row_chunk_counts(brain_root: str | Path) -> tuple[int, int]:
    project = Path(brain_root) / "project"
    rows_written = 0
    chunks_written = 0
    for database in sorted(project.rglob("*.sqlite")):
        connection = None
        try:
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts_%' ORDER BY name"
                )
            ]
            for table in tables:
                count = int(connection.execute(f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"').fetchone()[0])
                rows_written += count
                if "chunk" in table.casefold():
                    chunks_written += count
        except sqlite3.Error:
            continue
        finally:
            if connection is not None:
                connection.close()
    return rows_written, chunks_written


_ENV15_RENDER_STATUS_BY_MMD = {
    "env_mmd.mmd": "SQLITE_DERIVED_ENV_MMD_TO_SVG_TO_PNG_PASS",
    "uop_mmd.mmd": "SQLITE_DERIVED_UOP_MMD_TO_SVG_TO_PNG_PASS",
    "project_master_topology.mmd": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS",
}


def _topology_render_contract_passes(rendered: list[Mapping[str, Any]]) -> bool:
    """Require derived Env/UOP renders plus the live Project render."""
    allowed_statuses = set(_ENV15_RENDER_STATUS_BY_MMD.values())
    if any(str(item.get("status") or "") not in allowed_statuses for item in rendered):
        return False

    identified: dict[str, str] = {}
    for item in rendered:
        mmd_name = Path(str(item.get("mmd") or "")).name
        if mmd_name not in _ENV15_RENDER_STATUS_BY_MMD:
            continue
        if mmd_name in identified:
            return False
        identified[mmd_name] = str(item.get("status") or "")

    # Test doubles and legacy non-Env15 topology renderers may omit these exact
    # names. Once any Env15 artifact is present, enforce the complete mixed
    # locked-read/live-project contract rather than accepting a partial set.
    return not identified or identified == _ENV15_RENDER_STATUS_BY_MMD


def _pipeline_error_code(exc: BaseException) -> str:
    raw = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    token = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()[:96]
    return token or type(exc).__name__.upper()


def _materialize_post_commit_telemetry_cache(
    workspace: Path,
    brain_name: str,
) -> dict[str, Any]:
    """Prewarm the derived graph without invalidating a committed build.

    Immutable brain and package artifacts are the build authorities. The
    telemetry graph is a rebuildable, read-only acceleration artifact. Older
    imports and test doubles may omit the version/router authorities required
    to prewarm it, so those cases defer graph construction to first open.
    """

    try:
        return materialize_telemetry_graph_cache(workspace, brain_name)
    except BrainTelemetryError as exc:
        return {
            "status": "DEFERRED_UNTIL_FIRST_TELEMETRY_OPEN",
            "brain_name": brain_name,
            "reason": str(exc),
            "authoritative_build_preserved": True,
            "raw_project_reread_authorized": False,
        }


def _write_worker_failure_report(request: dict[str, Any], exc: BaseException) -> Path | None:
    """Persist the diagnostic context that the one-line IPC contract cannot carry."""
    try:
        if request.get("command") == "runtime.snapshot":
            return None
        payload = request.get("payload") or {}
        workspace = _workspace(payload)
        brain_name = payload.get("brain_name") or payload.get("brainName") or "New Brain"
        report_dir = _runtime_state_dir(workspace, brain_name) / "failed_builds"
        report_dir.mkdir(parents=True, exist_ok=True)
        error_id = "worker_error_" + hashlib.sha256(
            f"{request.get('command')}|{type(exc).__name__}|{exc}".encode()
        ).hexdigest()[:20]
        report = report_dir / f"{error_id}.json"
        if report.exists():
            return report
        temporary = report.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "status": "FAILED",
                    "error_id": error_id,
                    "request_id": str(request.get("id") or ""),
                    "command": str(request.get("command") or ""),
                    "error_code": _pipeline_error_code(exc),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                    "created_epoch": time.time(),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, report)
        return report
    except Exception:
        return None


def _publish_pipeline_event(
    request_id: str,
    registry: ProcessRegistry,
    event: dict[str, Any],
) -> dict[str, Any]:
    registry.set_active_context(
        pipeline_id=event.get("pipeline_id") or "",
        stage_id=event.get("stage_id") or "",
        lane=event.get("active_lane") or "",
        file=event.get("active_file") or "",
        command=event.get("active_command") or "",
    )
    return _emit_authoritative_task_progress(
        request_id,
        event,
        registry=registry,
        pipeline_owned=True,
    )


def _backend_stage_states(pipeline: dict[str, Any] | None) -> list[dict[str, Any]]:
    current_order = int((pipeline or {}).get("stage_order") or 0)
    pipeline_status = str((pipeline or {}).get("status") or "queued")
    rows = []
    for stage in PIPELINE_STAGES:
        if not pipeline:
            status = "queued"
            percent = 0.0
        elif pipeline_status == "completed" or stage.stage_order < current_order:
            status = "completed"
            percent = 100.0
        elif stage.stage_order == current_order:
            status = pipeline_status
            percent = float(pipeline.get("stage_percent") or 0.0)
        else:
            status = "queued"
            percent = 0.0
        rows.append({
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "stage_order": stage.stage_order,
            "status": status,
            "stage_percent": percent,
            "process_pid": int((pipeline or {}).get("process_pid") or 0) if status == "running" else 0,
            "error_code": (pipeline or {}).get("error_code") if stage.stage_order == current_order else None,
        })
    return rows


def _codex_handoff_status(workspace: Path, brain_name: str) -> dict[str, Any]:
    root = brain_output_dir(workspace, brain_name)
    database = root / "brain_versions" / "semantic_brain_diff.sqlite"
    if not database.is_file():
        return {"status": "NOT_CREATED", "brain_name": brain_name, "handoff": None}
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='brain_codex_handoff'"
        ).fetchone()
        if not table_exists:
            return {"status": "NOT_CREATED", "brain_name": brain_name, "handoff": None}
        row = connection.execute(
            "SELECT handoff_id,handoff_folder,package_path,package_hash,status,created_at "
            "FROM brain_codex_handoff ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    return {
        "status": str(row["status"]) if row else "NOT_CREATED",
        "brain_name": brain_name,
        "handoff": dict(row) if row else None,
    }


def _portable_package_root(workspace: Path, brain_name: str, payload: dict[str, Any]) -> Path:
    requested = payload.get("package_root") or payload.get("packageRoot")
    root = Path(str(requested)).expanduser() if requested else workspace / "portable_brain_workspaces" / slugify_name(brain_name)
    root = root.resolve()
    workspace_root = workspace.resolve()
    try:
        root.relative_to(workspace_root)
    except ValueError as exc:
        raise WorkerError("PORTABLE_PACKAGE_ROOT_OUTSIDE_WORKSPACE") from exc
    return root


def _portable_ledger_path(package_root: Path) -> Path:
    return package_root / "codex" / "codex_runtime_ledger.sqlite"


def _portable_package_status(workspace: Path, brain_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    package_root = _portable_package_root(workspace, brain_name, payload)
    if not package_root.is_dir():
        return {"status": "NOT_CREATED", "brain_name": brain_name, "package_root": str(package_root)}
    validation = validate_portable_brain_package(package_root)
    snapshot_pointer = package_root / "pointers" / "CURRENT_BRAIN_POINTER.json"
    pointer = None
    if snapshot_pointer.is_file():
        try:
            pointer = json.loads(snapshot_pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pointer = {"status": "UNREADABLE"}
    return {
        "status": validation["status"],
        "brain_name": brain_name,
        "package_root": str(package_root),
        "snapshot": pointer,
        "validation": validation,
    }


def _require_portable_ledger(package_root: Path) -> Path:
    ledger = _portable_ledger_path(package_root)
    if not ledger.is_file():
        raise WorkerError("PORTABLE_OPERATIONAL_LEDGER_NOT_CREATED")
    return ledger


def handle(request: dict[str, Any]) -> Any:
    command = request.get("command")
    payload = request.get("payload") or {}
    if command == "system.ping":
        frozen = bool(getattr(sys, "frozen", False))
        return {
            "contract": "T023_FULL_APP_BACKEND_V2",
            "workspace_required": False,
            "pid": os.getpid(),
            "frozen": frozen,
            "packaging_mode": "PYINSTALLER_EMBEDDED_WORKER" if frozen else "PYTHON_SOURCE_WORKER",
            "worker_executable": sys.executable,
            "canonical_route": canonical_route_state(),
        }
    if command == "workspace.canonicalRoute.inspect":
        return canonical_route_state()
    workspace = _workspace(payload)
    explicit_brain_name = payload.get("brain_name") or payload.get("brainName")
    brain_name = explicit_brain_name or "New Brain"
    if not explicit_brain_name and command in {
        "lanes.defs",
        "source.schema.get",
        "source.schema.update",
        "source.schema.reset",
    }:
        brain_name = _implicit_schema_brain_name(workspace)

    if command == "lanes.defs":
        return {"lanes": _lane_defs_payload(workspace, brain_name)}
    if command in {"workspace.outputRoot.view", "workspace.rootHistory.list"}:
        return list_workspace_roots()
    if command == "workspace.outputRoot.choose":
        if native_production_mode():
            raise WorkerError("CANONICAL_PRODUCTION_ROOT_LOCKED")
        try:
            selected = choose_directory(
                title="Choose EvidenceOS work and data folder",
                initial_directory=workspace,
            )
        except NativePickerError as exc:
            raise WorkerError(str(exc)) from exc
        if not selected:
            return {
                "status": "CANCELLED",
                "workspace_dir": str(workspace),
                "workspace_roots": list_workspace_roots(),
                "changed": False,
            }
        if len(selected) != 1 or not selected[0].is_dir():
            raise WorkerError("NATIVE_PICKER_DIRECTORY_INVALID")
        selected_workspace = normalize_workspace_dir(selected[0])
        db = init_workspace(selected_workspace)
        record_workspace_root(
            selected_workspace,
            source="NATIVE_WINDOWS_FOLDER_PICKER",
            make_active=True,
        )
        history = retain_only_workspace_root(
            selected_workspace,
            source="NATIVE_WINDOWS_FOLDER_PICKER_CURRENT_ROOT_ONLY",
        )
        discovery = _discover_existing_brains(selected_workspace)
        _refresh_catalog_workspace_root(selected_workspace)
        _retain_only_catalog_workspace_root(selected_workspace)
        return {
            "status": "PASS",
            "workspace_dir": str(selected_workspace),
            "workspace_db": str(db),
            "brains": _list_brains(selected_workspace),
            "discovery": discovery,
            "workspace_roots": history,
            "brain_catalog": _brain_catalog(),
            "changed": os.path.normcase(str(selected_workspace)) != os.path.normcase(str(workspace)),
            "selection_origin": "NATIVE_WINDOWS_FOLDER_PICKER",
        }
    if command == "workspace.rootHistory.add":
        if native_production_mode():
            raise WorkerError("CANONICAL_PRODUCTION_ROOT_HISTORY_ADD_FORBIDDEN")
        try:
            selected = choose_directory(
                title="Add an existing EvidenceOS legacy brain root",
                initial_directory=workspace,
            )
        except NativePickerError as exc:
            raise WorkerError(str(exc)) from exc
        if not selected:
            return {
                "status": "CANCELLED",
                "workspace_roots": list_workspace_roots(),
                "brain_catalog": _brain_catalog(),
                "changed": False,
                "selection_origin": "NATIVE_WINDOWS_FOLDER_PICKER",
            }
        if len(selected) != 1 or not selected[0].is_dir():
            raise WorkerError("NATIVE_PICKER_DIRECTORY_INVALID")
        legacy_root = normalize_workspace_dir(selected[0])
        history = record_workspace_root(
            legacy_root,
            source="NATIVE_WINDOWS_LEGACY_ROOT_PICKER",
            make_active=False,
        )
        # Adding a route is the one explicit moment where discovery is
        # authorized.  Populate each newly found workspace once, then keep the
        # persisted workspace/brain catalog stable on ordinary app boots and
        # brain switches until the route is dropped or explicitly refreshed.
        for legacy_workspace in _refresh_catalog_workspace_root(legacy_root):
            _discover_existing_brains(legacy_workspace)
        return {
            "status": "PASS",
            "legacy_root": str(legacy_root),
            "workspace_roots": history,
            "brain_catalog": _brain_catalog(),
            "changed": True,
            "data_moved": False,
            "data_deleted": False,
            "selection_origin": "NATIVE_WINDOWS_FOLDER_PICKER",
        }
    if command == "workspace.rootHistory.drop":
        if native_production_mode():
            raise WorkerError("CANONICAL_PRODUCTION_ROOT_HISTORY_DROP_FORBIDDEN")
        root_path = str(payload.get("root_path") or payload.get("rootPath") or "")
        confirm_path = str(payload.get("confirm_path") or payload.get("confirmPath") or "")
        if not root_path or not confirm_path:
            raise WorkerError("WORKSPACE_ROOT_DROP_PATH_AND_CONFIRMATION_REQUIRED")
        try:
            dropped = drop_workspace_root_history(root_path, confirm_path=confirm_path)
            _forget_catalog_workspace_root(Path(root_path))
            return {**dropped, "brain_catalog": _brain_catalog()}
        except WorkspaceRootHistoryError as exc:
            raise WorkerError(str(exc)) from exc
    if command == "workspace.outputRoot.updateDefault":
        raise WorkerError("WORKSPACE_ROOT_NATIVE_PICKER_REQUIRED")
    if command == "profile.image.choose":
        image_path = _native_single_file(
            title="Choose profile image",
            extensions=[".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"],
            initial_directory=workspace,
            filter_description="Profile image files",
        )
        if image_path is None:
            return {"status": "CANCELLED", "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER"}
        raw = _native_binary_asset(
            image_path,
            maximum_bytes=3 * 1024 * 1024,
            empty_error="PROFILE_IMAGE_SIZE_INVALID",
            size_error="PROFILE_IMAGE_SIZE_INVALID",
        )
        suffix = image_path.suffix.casefold()
        signature_valid = {
            ".bmp": raw.startswith(b"BM"),
            ".gif": raw.startswith((b"GIF87a", b"GIF89a")),
            ".jpeg": raw.startswith(b"\xff\xd8\xff"),
            ".jpg": raw.startswith(b"\xff\xd8\xff"),
            ".png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
            ".tif": raw.startswith((b"II*\x00", b"MM\x00*")),
            ".tiff": raw.startswith((b"II*\x00", b"MM\x00*")),
            ".webp": len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
        }[suffix]
        if not signature_valid:
            raise WorkerError("PROFILE_IMAGE_SIGNATURE_INVALID")
        mime_types = {
            ".bmp": "image/bmp",
            ".gif": "image/gif",
            ".jpeg": "image/jpeg",
            ".jpg": "image/jpeg",
            ".png": "image/png",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
            ".webp": "image/webp",
        }
        return {
            "status": "PASS",
            "name": image_path.name,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_url": f"data:{mime_types[suffix]};base64,{base64.b64encode(raw).decode('ascii')}",
            "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER",
        }
    if command == "character.glb.choose":
        slot = str(payload.get("slot") or "").casefold()
        if slot not in {"face", "body"}:
            raise WorkerError("CHARACTER_GLB_SLOT_REQUIRED_FACE_OR_BODY")
        asset_path = _native_single_file(
            title=f"Choose character {slot} GLB",
            extensions=[".glb"],
            initial_directory=workspace,
            filter_description=f"Character {slot} GLB files",
        )
        if asset_path is None:
            return {
                "status": "CANCELLED",
                "slot": slot,
                "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER",
            }
        raw = _native_binary_asset(
            asset_path,
            maximum_bytes=64 * 1024 * 1024,
            empty_error=f"CHARACTER_{slot.upper()}_GLB_SIZE_OR_EXTENSION_INVALID",
            size_error=f"CHARACTER_{slot.upper()}_GLB_SIZE_OR_EXTENSION_INVALID",
        )
        if (
            len(raw) < 12
            or raw[:4] != b"glTF"
            or int.from_bytes(raw[4:8], "little") != 2
            or int.from_bytes(raw[8:12], "little") != len(raw)
        ):
            raise WorkerError(f"CHARACTER_{slot.upper()}_GLB_HEADER_INVALID")
        return {
            "status": "PASS",
            "slot": slot,
            "name": asset_path.name,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_url": "data:model/gltf-binary;base64," + base64.b64encode(raw).decode("ascii"),
            "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER",
        }
    if command == "ollama.executable.choose":
        executable = _native_single_file(
            title="Choose Ollama executable or Windows shortcut",
            extensions=[".exe", ".lnk"],
            initial_directory=workspace,
            filter_description="Ollama application or Windows shortcut",
        )
        if executable is None:
            return {"status": "CANCELLED", "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER"}
        return {
            "status": "PASS",
            "executable_path": str(executable),
            "display_name": executable.name,
            "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER",
        }
    if command == "source.path.choose":
        requested_lane = str(payload.get("lane_key") or payload.get("laneKey") or "")
        if requested_lane.startswith("custom_"):
            requested_lane = "custom"
        try:
            lane_id = resolve_lane_id(requested_lane, scope="any")
        except UnknownLaneAliasError as exc:
            raise WorkerError(f"UNKNOWN_LANE_ALIAS:{requested_lane}") from exc
        descriptor = _native_picker_descriptor(lane_id, LANE_DEFS[lane_id])
        try:
            if descriptor["mode"] == "directory":
                selections = choose_directory(
                    title=f"Choose {LANE_DEFS[lane_id]['label']} folder",
                    initial_directory=workspace,
                )
            else:
                extensions = [
                    extension
                    for row in descriptor.get("types") or []
                    for values in (row.get("accept") or {}).values()
                    for extension in values
                ]
                selections = choose_files(
                    title=f"Choose {LANE_DEFS[lane_id]['label']} file" + ("s" if descriptor["multiple"] else ""),
                    extensions=extensions,
                    multiple=bool(descriptor["multiple"]),
                    initial_directory=workspace,
                    filter_description=f"{LANE_DEFS[lane_id]['label']} lane files",
                )
        except NativePickerError as exc:
            raise WorkerError(str(exc)) from exc
        if descriptor["mode"] == "directory":
            if any(not path.is_dir() for path in selections):
                raise WorkerError("NATIVE_PICKER_DIRECTORY_INVALID")
        else:
            allowed_extensions = {
                str(extension).casefold()
                for row in descriptor.get("types") or []
                for values in (row.get("accept") or {}).values()
                for extension in values
            }
            for path in selections:
                if not path.is_file() or path.suffix.casefold() not in allowed_extensions:
                    raise WorkerError(
                        f"NATIVE_PICKER_FILE_TYPE_INVALID:{path.suffix.casefold() or '<none>'}"
                    )
        return {
            "status": "PASS" if selections else "CANCELLED",
            "lane_id": lane_id,
            "selection_count": len(selections),
            "paths": [str(path) for path in selections],
            "display_names": [path.name for path in selections],
            "picker": descriptor,
            "selection_origin": "NATIVE_WINDOWS_FILE_EXPLORER",
        }
    if command == "workspace.init":
        db = init_workspace(workspace)
        explicit_workspace = bool(str(payload.get("workspace_dir") or payload.get("workspaceDir") or "").strip())
        activate_explicit_workspace = bool(payload.get("activate_workspace_root") or payload.get("activateWorkspaceRoot"))
        if not explicit_workspace or activate_explicit_workspace:
            record_workspace_root(
                workspace,
                source=(
                    "APP_ROAMING_DEFAULT"
                    if os.path.normcase(str(workspace)) == os.path.normcase(str(default_workspace_dir()))
                    else "RESTORED_OR_EXPLICIT_WORKSPACE"
                ),
                make_active=True,
            )
            history = retain_only_workspace_root(
                workspace,
                source="NATIVE_APP_START_CURRENT_ROOT_ONLY",
            )
            _retain_only_catalog_workspace_root(workspace)
        else:
            # Explicit paths are also used by isolated tests and backend
            # callers. They must not silently replace or clutter the user's
            # persistent desktop root history. The native picker is the
            # authoritative path for an interactive root change.
            history = list_workspace_roots()
        brains = _list_brains(workspace)
        if brains:
            discovery = {
                "status": "RESTORED_FROM_PERSISTED_WORKSPACE_DATABASE",
                "raw_directory_rescan": False,
                "brain_count": len(brains),
                "registered_count": 0,
                "ignored_count": 0,
                "scanned_count": 0,
            }
        else:
            # One migration scan is allowed only for a workspace that has no
            # persisted catalog yet. Subsequent boots restore exact state.
            discovery = _discover_existing_brains(workspace)
            brains = _list_brains(workspace)
        brain_names = {str(item.get("brain_name") or "") for item in brains}
        catalog = _brain_catalog()
        catalog_rows = list(catalog.get("brains") or [])
        con = _connect_workspace(workspace)
        try:
            last_selected_brain = str(_setting(con, LAST_SELECTED_BRAIN_SETTINGS_KEY, "") or "")
        finally:
            con.close()
        if last_selected_brain not in brain_names:
            selection_candidates = _legacy_utf8_repair_candidates(last_selected_brain)
            exact_display = next(
                (
                    str(item.get("display_name") or "")
                    for item in catalog_rows
                    if str(item.get("display_name") or "") in selection_candidates
                ),
                "",
            )
            canonical_matches = [
                item for item in catalog_rows
                if str(item.get("brain_name") or "") == last_selected_brain
            ]
            preferred_canonical = next(
                (item for item in canonical_matches if bool(item.get("active_root"))),
                canonical_matches[0] if len(canonical_matches) == 1 else None,
            )
            if exact_display:
                last_selected_brain = exact_display
                con = _connect_workspace(workspace)
                try:
                    _set_setting(con, LAST_SELECTED_BRAIN_SETTINGS_KEY, exact_display)
                finally:
                    con.close()
            elif preferred_canonical:
                last_selected_brain = str(
                    preferred_canonical.get("display_name")
                    or preferred_canonical.get("brain_name")
                    or ""
                )
            else:
                last_selected_brain = str(brains[0].get("brain_name") or "") if brains else ""
        return {
            "workspace_dir": str(workspace),
            "workspace_db": str(db),
            "brains": brains,
            "discovery": discovery,
            "workspace_roots": history,
            "brain_catalog": catalog,
            "last_selected_brain": last_selected_brain,
        }
    if command == "brains.discover":
        discovery = _discover_existing_brains(workspace)
        return {"workspace_dir": str(workspace), "brains": _list_brains(workspace), "discovery": discovery}
    if command == "source.schema.get":
        return _lane_schema_state(
            workspace,
            str(payload.get("lane_key") or payload.get("laneKey") or ""),
            brain_name,
        )
    if command == "source.schema.update":
        return _update_lane_schema(
            workspace,
            str(payload.get("lane_key") or payload.get("laneKey") or ""),
            payload.get("schema_contract") if "schema_contract" in payload else payload.get("schemaContract"),
            brain_name=brain_name,
            actor=str(payload.get("actor") or "Evidence OS user"),
        )
    if command == "source.schema.reset":
        return _reset_lane_schema(
            workspace,
            str(payload.get("lane_key") or payload.get("laneKey") or ""),
            brain_name=brain_name,
            actor=str(payload.get("actor") or "Evidence OS user"),
        )
    if command == "local.workspace.init":
        return ensure_local_workspace(workspace)
    if command == "local.workspace.snapshot":
        return local_snapshot(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "identity.get":
        return get_profile(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "profile.update":
        return update_profile(workspace, payload.get("values") or {}, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "settings.get":
        return get_settings(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "settings.update":
        return update_settings(workspace, str(payload.get("category") or ""), payload.get("values") or {}, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "settings.reset":
        return reset_settings(workspace, payload.get("category"), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "settings.export":
        return export_local_data(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "settings.import":
        return import_local_data(workspace, payload.get("data") or {}, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "admin.settings.get":
        return get_admin_settings(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "admin.settings.update":
        return update_admin_settings(workspace, payload.get("values") or {}, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "credential.set":
        return set_protected_credential(workspace, str(payload.get("service_name") or payload.get("serviceName") or ""), str(payload.get("account_name") or payload.get("accountName") or ""), str(payload.get("secret") or ""), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "credential.status":
        return get_protected_credential_status(workspace, str(payload.get("credential_id") or payload.get("credentialId") or ""), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "credential.delete":
        return delete_protected_credential(workspace, str(payload.get("credential_id") or payload.get("credentialId") or ""), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "model.connector.list":
        return list_model_connectors(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
            include_disabled=bool(payload.get("include_disabled") or payload.get("includeDisabled")),
        )
    if command == "model.connector.get":
        return get_model_connector(
            workspace,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.upsert":
        return upsert_model_connector(
            workspace,
            payload.get("profile") or {},
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.setActive":
        return set_active_model_connector(
            workspace,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.disable":
        return disable_model_connector(
            workspace,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.discoverModels":
        return discover_model_connector_models(
            workspace,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.validate":
        return validate_model_connector(
            workspace,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.delete":
        return delete_model_connector(
            workspace,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            confirm_endpoint_id=str(payload.get("confirm_endpoint_id") or payload.get("confirmEndpointId") or ""),
            identity_id=payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "model.connector.execute":
        return execute_model_connector(
            workspace,
            brain_name,
            str(payload.get("endpoint_id") or payload.get("endpointId") or ""),
            str(payload.get("prompt") or ""),
            remote_consent=bool(payload.get("remote_consent") or payload.get("remoteConsent")),
            evidence_slices=payload.get("evidence_slices") or payload.get("evidenceSlices") or [],
            identity_id=payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
            task_id=payload.get("task_id") or payload.get("taskId"),
        )
    if command == "ollama.settings.get":
        return get_ollama_settings(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "ollama.settings.update":
        return update_ollama_settings(
            workspace,
            payload.get("values") or {},
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "ollama.status":
        return get_ollama_status(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
            check_api=bool(payload.get("check_api") or payload.get("checkApi")),
        )
    if command == "ollama.registry.refresh":
        return refresh_ollama_registry(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "ollama.launch":
        return launch_ollama(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "ollama.api.models":
        return get_ollama_models(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "codex.settings.get":
        return get_codex_settings(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "codex.settings.update":
        return update_codex_settings(
            workspace,
            payload.get("values") or {},
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "codex.status":
        return get_codex_status(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "codex.launch":
        return launch_codex(
            workspace,
            payload.get("identity_id") or payload.get("identityId") or "identity_local_owner",
        )
    if command == "project.create":
        return create_project(workspace, str(payload.get("name") or ""), payload.get("parent_project_id") or payload.get("parentProjectId"), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "project.list":
        return {"projects": list_projects(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")}
    if command == "chat.create":
        return create_chat(workspace, str(payload.get("title") or "New Chat"), payload.get("project_id") or payload.get("projectId"), payload.get("brain_name") or payload.get("brainName"), payload.get("idempotency_key") or payload.get("idempotencyKey"), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command in {"chat.list", "chat.pinned.list"}:
        return {"chats": list_chats(workspace, payload.get("project_id") or payload.get("projectId"), command == "chat.pinned.list" or bool(payload.get("pinned_only")), bool(payload.get("include_archived")), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")}
    if command in {"chat.rename", "chat.pin", "chat.unpin", "chat.move", "chat.detach", "chat.archive", "chat.unarchive", "chat.markRead"}:
        operation = command.split(".", 1)[1]
        operation = "mark_read" if operation == "markRead" else operation
        return mutate_chat(workspace, str(payload.get("chat_id") or payload.get("chatId") or ""), operation, payload.get("value") if "value" in payload else payload.get("project_id") or payload.get("projectId") or payload.get("title"), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "chat.delete":
        return delete_chat(workspace, str(payload.get("chat_id") or payload.get("chatId") or ""), str(payload.get("confirm_chat_id") or payload.get("confirmChatId") or ""), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "message.append":
        return append_message(workspace, str(payload.get("chat_id") or payload.get("chatId") or ""), str(payload.get("role") or ""), str(payload.get("content_exact") or payload.get("contentExact") or ""), payload.get("parent_message_id") or payload.get("parentMessageId"), str(payload.get("status") or "COMPLETED"), payload.get("usage") or {}, payload.get("idempotency_key") or payload.get("idempotencyKey"))
    if command == "attachment.link":
        return link_attachment(workspace, str(payload.get("message_id") or payload.get("messageId") or ""), str(payload.get("path") or ""), str(payload.get("relation_kind") or payload.get("relationKind") or "source"), payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "workspace.search":
        return {"results": search_workspace(workspace, str(payload.get("query") or ""), int(payload.get("limit") or 50))}
    if command == "session.get" or command == "session.restore":
        return get_session_state(workspace, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "session.update":
        return update_session_state(workspace, payload.get("values") or {}, payload.get("identity_id") or payload.get("identityId") or "identity_local_owner")
    if command == "chat.lineage.append":
        return append_chat_lineage(workspace, str(payload.get("chat_id") or payload.get("chatId") or ""), str(payload.get("prompt_exact") or payload.get("promptExact") or ""), str(payload.get("output_exact") or payload.get("outputExact") or ""), payload.get("links") or [], payload.get("idempotency_key") or payload.get("idempotencyKey"))
    if command == "brains.list":
        return {"workspace_dir": str(workspace), "brains": _list_brains(workspace)}
    if command == "brains.catalog":
        return _brain_catalog(
            refresh=bool(payload.get("refresh_catalog") or payload.get("refreshCatalog"))
        )
    if command == "brain.create":
        created = create_brain(workspace, brain_name)
        _clear_brain_discovery_tombstone(workspace, created["output_dir"])
        return {"created": created, "brains": _list_brains(workspace), "summary": _brain_summary(workspace, brain_name)}
    if command == "brain.select":
        # Selection is read/restore only. A canonical existing output can be
        # selected even before discovery registers it, but selection must never
        # materialize a missing brain in whichever workspace route it receives.
        selected_output = brain_output_dir(workspace, brain_name)
        if not selected_output.is_dir():
            raise ValueError(f"BRAIN_NOT_REGISTERED_OR_OUTPUT_MISSING:{brain_name}")
        selection_display_name = str(
            payload.get("selection_display_name")
            or payload.get("selectionDisplayName")
            or brain_name
        ).strip()
        persistence_workspace_value = str(
            payload.get("selection_persistence_workspace_dir")
            or payload.get("selectionPersistenceWorkspaceDir")
            or ""
        ).strip()
        persistence_workspace = (
            normalize_workspace_dir(persistence_workspace_value)
            if persistence_workspace_value
            else workspace
        )
        selection_restore_only = bool(
            payload.get("selection_restore_only")
            or payload.get("selectionRestoreOnly")
        )
        selection_request_id = str(
            payload.get("selection_request_id")
            or payload.get("selectionRequestId")
            or ""
        ).strip()
        if not selection_request_id:
            selection_request_id = "brain-selection-" + hashlib.sha256(
                (
                    os.path.normcase(str(persistence_workspace.resolve()))
                    + "\x1f"
                    + os.path.normcase(str(workspace.resolve()))
                    + "\x1f"
                    + brain_name
                    + "\x1f"
                    + str(time.time_ns())
                ).encode("utf-8")
            ).hexdigest()[:24]
        routed_changed = False
        persistence_changed = False
        same_persistence_workspace = (
            os.path.normcase(str(persistence_workspace.resolve()))
            == os.path.normcase(str(workspace.resolve()))
        )
        if not selection_restore_only and not same_persistence_workspace:
            con = _connect_workspace(workspace)
            try:
                routed_changed = _set_setting(con, LAST_SELECTED_BRAIN_SETTINGS_KEY, brain_name)
            finally:
                con.close()
        if not selection_restore_only:
            con = _connect_workspace(persistence_workspace)
            try:
                persistence_changed = _set_setting(
                    con,
                    LAST_SELECTED_BRAIN_SETTINGS_KEY,
                    selection_display_name,
                )
            finally:
                con.close()
        context = _selected_brain_context(workspace, brain_name)
        selection_persistence = {
            "display_name": selection_display_name,
            "routed_brain_name": brain_name,
            "routed_workspace_dir": str(workspace),
            "persistence_workspace_dir": str(persistence_workspace),
            "mode": "RESTORE_ONLY_NO_WRITE" if selection_restore_only else "USER_SELECTION_PERSISTED",
            "routed_setting_changed": routed_changed,
            "persistence_setting_changed": persistence_changed,
        }
        selection_receipt = None
        if not selection_restore_only:
            selection_receipt = _write_operation_receipt(
                persistence_workspace,
                "brain.selection.commit",
                {
                    "schema": _SELECTED_BRAIN_AUTHORITY_SCHEMA,
                    "selection_request_id": selection_request_id,
                    "brain_id": context["brain_identity"]["brain_id"],
                    "brain_name": brain_name,
                    "display_name": selection_display_name,
                    "routed_workspace_dir": str(workspace),
                    "persistence_workspace_dir": str(persistence_workspace),
                    "output_dir": context["brain_identity"]["output_dir"],
                    "context_sha256": context["context_sha256"],
                    "accepted_version_id": context["selection_authority"]["accepted_version_id"],
                    "candidate_delta_id": context["selection_authority"]["candidate_delta_id"],
                    "status": "COMMITTED",
                },
            )
        return {
            **context,
            "selection_persistence": selection_persistence,
            "selection_commit": {
                "schema": _SELECTED_BRAIN_AUTHORITY_SCHEMA,
                "selection_request_id": selection_request_id,
                "status": "RESTORED" if selection_restore_only else "COMMITTED",
                "context_sha256": context["context_sha256"],
                "brain_id": context["brain_identity"]["brain_id"],
                "brain_name": brain_name,
                "workspace_dir": str(workspace),
                "display_name": selection_display_name,
                "receipt": selection_receipt,
            },
        }
    if command == "brain.selection.timing.record":
        return _record_selection_timing(workspace, payload)
    if command == "brain.rename":
        return _rename_brain(workspace, payload)
    if command in {"brain.pin", "brain.unpin"}:
        con = _connect_workspace(workspace)
        pins = set(_setting(con, PIN_SETTINGS_KEY, []))
        if command == "brain.pin":
            pins.add(brain_name)
        else:
            pins.discard(brain_name)
        _set_setting(con, PIN_SETTINGS_KEY, sorted(pins))
        con.close()
        return {"pinned": sorted(pins), "brains": _list_brains(workspace)}
    if command == "brain.remove":
        _record_brain_discovery_tombstone(workspace, brain_name)
        con = _connect_workspace(workspace)
        con.execute("UPDATE brain_project SET status='REMOVED', updated_at=? WHERE brain_name=?", (time.strftime("%Y-%m-%dT%H:%M:%S"), brain_name))
        con.commit()
        con.close()
        return {"removed": brain_name, "brains": _list_brains(workspace)}
    if command == "brain.deleteToRecycleBin":
        result = recycle_registered_brain(
            workspace,
            brain_name,
            confirm_brain_name=payload.get("confirm_brain_name") or payload.get("confirmBrainName") or "",
        )
        return {**result, "brains": _list_brains(workspace)}
    if command == "brain.telemetry.contract":
        return telemetry_contract()
    if command == "brain.telemetry.snapshot":
        return get_telemetry_snapshot(workspace, brain_name)
    if command == "brain.telemetry.deltas":
        return list_telemetry_deltas(workspace, brain_name)
    if command == "brain.telemetry.graph":
        requested_max = payload.get("max_nodes", payload.get("maxNodes", 0))
        return query_telemetry_graph(
            workspace,
            brain_name,
            delta_id=payload.get("delta_id") or payload.get("deltaId"),
            scope_id=payload.get("scope_id") or payload.get("scopeId"),
            max_nodes=requested_max,
        )
    if command == "brain.telemetry.openTarget":
        return open_telemetry_target(
            workspace,
            brain_name,
            str(payload.get("target") or payload.get("path") or ""),
            application=str(payload.get("application") or "default"),
        )
    if command == "brain.versions.list":
        verify_hashes = payload.get("verify_hashes", payload.get("verifyHashes", True))
        return list_brain_versions(workspace, brain_name, verify_hashes=bool(verify_hashes))
    if command == "brain.delta.list":
        return list_project_deltas(brain_output_dir(workspace, brain_name))
    if command == "brain.delta.get":
        delta = get_project_delta(
            brain_output_dir(workspace, brain_name),
            delta_id=payload.get("delta_id") or payload.get("deltaId"),
            candidate_id=payload.get("candidate_id") or payload.get("candidateId"),
        )
        if delta is None:
            raise WorkerError("PROJECT_DELTA_NOT_FOUND")
        return delta
    if command == "brain.planDelta.state":
        brain_root = brain_output_dir(workspace, brain_name)
        try:
            return read_plan_delta_state(brain_root)
        except PlanDeltaGovernanceError as exc:
            if _is_plan_sector_build_required(exc):
                return _plan_sector_build_required_state(brain_root)
            raise
    if command == "brain.planDelta.append":
        content = str(payload.get("content") or payload.get("steer") or "").strip()
        turn_id = str(payload.get("turn_id") or payload.get("turnId") or "").strip()
        if not content:
            raise WorkerError("PLAN_DELTA_CONTENT_REQUIRED")
        if not turn_id:
            raise WorkerError("PLAN_DELTA_TURN_ID_REQUIRED")
        explicit_lanes = payload.get("explicit_lanes") or payload.get("explicitLanes")
        if isinstance(explicit_lanes, str):
            explicit_lanes = [explicit_lanes]
        return append_classified_steer_delta(
            brain_output_dir(workspace, brain_name),
            content,
            actor=str(payload.get("actor") or "Evidence OS User"),
            model_name=str(payload.get("model_name") or payload.get("modelName") or "codex"),
            turn_id=turn_id,
            model_family=str(payload.get("model_family") or payload.get("modelFamily") or "CODEX"),
            explicit_lanes=explicit_lanes,
            supersedes_delta_id=(
                payload.get("supersedes_delta_id") or payload.get("supersedesDeltaId")
            ),
            next_action=str(
                payload.get("next_action")
                or payload.get("nextAction")
                or "CONTINUE_SAME_LINEAR_GOAL_UNTIL_HIL"
            ),
            idempotency_key=payload.get("idempotency_key") or payload.get("idempotencyKey"),
        )
    if command == "brain.planDelta.disposition":
        delta_id = str(payload.get("delta_id") or payload.get("deltaId") or "").strip()
        disposition = str(payload.get("disposition") or "").strip()
        turn_id = str(payload.get("turn_id") or payload.get("turnId") or "").strip()
        if not delta_id:
            raise WorkerError("PLAN_DELTA_ID_REQUIRED")
        if not disposition:
            raise WorkerError("PLAN_DELTA_DISPOSITION_REQUIRED")
        if not turn_id:
            raise WorkerError("PLAN_DELTA_TURN_ID_REQUIRED")
        return set_delta_disposition(
            brain_output_dir(workspace, brain_name),
            delta_id,
            disposition,
            actor=str(payload.get("actor") or "Evidence OS User"),
            reason=str(payload.get("reason") or "explicit Plan delta disposition"),
            turn_id=turn_id,
        )
    if command == "brain.planGoalPrompt.read":
        try:
            return build_plan_goal_prompt(
                brain_output_dir(workspace, brain_name),
                actor=str(payload.get("actor") or "Evidence OS Prompt 2 Generator"),
            )
        except PlanDeltaGovernanceError as exc:
            if _is_plan_sector_build_required(exc):
                return _plan_prompt_build_required()
            raise
    if command == "brain.planStateSlip.append":
        turn_id = str(payload.get("turn_id") or payload.get("turnId") or "").strip()
        if not turn_id:
            raise WorkerError("PLAN_STATE_SLIP_TURN_ID_REQUIRED")
        gates = payload.get("gates") or []
        lanes = payload.get("lanes") or []
        if isinstance(gates, str):
            gates = [gates]
        if isinstance(lanes, str):
            lanes = [lanes]
        token_count = payload.get("token_count", payload.get("tokenCount"))
        return record_model_state_slip(
            brain_output_dir(workspace, brain_name),
            slip_kind=str(payload.get("slip_kind") or payload.get("slipKind") or ""),
            model_family=str(payload.get("model_family") or payload.get("modelFamily") or ""),
            model_name=str(payload.get("model_name") or payload.get("modelName") or ""),
            prompt_index=int(payload.get("prompt_index") or payload.get("promptIndex") or 1),
            gates=gates,
            lanes=lanes,
            turn_id=turn_id,
            token_count=None if token_count is None else int(token_count),
        )
    if command == "brain.version.capture":
        return capture_brain_version(
            workspace,
            brain_name,
            actor_type=payload.get("actor_type") or payload.get("actorType") or "",
            actor_name=payload.get("actor_name") or payload.get("actorName") or "",
            reason=payload.get("reason") or "",
            change_summary=payload.get("change_summary") or payload.get("changeSummary") or "",
        )
    if command == "brain.snapshot.markGood":
        return mark_current_passing_build_good(
            workspace,
            brain_name,
            test_status=payload.get("test_status") or payload.get("testStatus") or "",
            build_status=payload.get("build_status") or payload.get("buildStatus") or "",
            reason=payload.get("reason") or "mark current passing build good",
        )
    if command == "brain.review.acceptByContinuation":
        categories = payload.get("action_categories") or payload.get("actionCategories") or []
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, list):
            raise WorkerError("ACCEPTED_BY_CONTINUATION_ACTION_CATEGORIES_LIST_REQUIRED")
        receipt_dir = brain_output_dir(workspace, brain_name) / "receipts" / "review_qualification"
        task_id = str(payload.get("task_id") or payload.get("taskId") or "")
        run_id = str(payload.get("run_id") or payload.get("runId") or "")
        trigger_steer = str(payload.get("trigger_steer") or payload.get("triggerSteer") or "")
        receipt_seed = json.dumps(
            [task_id, run_id, trigger_steer],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt_path = receipt_dir / (
            "ACCEPTED_BY_CONTINUATION_" + hashlib.sha256(receipt_seed).hexdigest()[:24].upper() + ".json"
        )
        prior_receipt_sha256 = str(
            payload.get("prior_receipt_sha256") or payload.get("priorReceiptSha256") or ""
        ).upper()
        if not prior_receipt_sha256 and receipt_dir.is_dir():
            verified_prior: list[dict[str, Any]] = []
            for prior_path in receipt_dir.glob("ACCEPTED_BY_CONTINUATION_*.json"):
                if prior_path.resolve() == receipt_path.resolve():
                    continue
                try:
                    verified_prior.append(verify_accepted_by_continuation_receipt(prior_path))
                except ReviewQualificationError:
                    continue
            if verified_prior:
                prior_receipt_sha256 = str(
                    max(
                        verified_prior,
                        key=lambda item: (
                            str(item.get("recorded_at") or ""),
                            str(item.get("content_sha256") or ""),
                        ),
                    ).get("content_sha256")
                    or ""
                ).upper()
        try:
            return record_accepted_by_continuation(
                receipt_path,
                task_id=task_id,
                run_id=run_id,
                actor=str(payload.get("actor") or ""),
                trigger_steer=trigger_steer,
                scope_summary=str(payload.get("scope_summary") or payload.get("scopeSummary") or ""),
                next_pointer=str(payload.get("next_pointer") or payload.get("nextPointer") or ""),
                action_categories=[str(value) for value in categories],
                completed=payload.get("completed") is True,
                tested=payload.get("tested") is True,
                reversible=payload.get("reversible") is True,
                bounded_scope=(payload.get("bounded_scope") is True or payload.get("boundedScope") is True),
                review_packet_sha256=str(
                    payload.get("review_packet_sha256") or payload.get("reviewPacketSha256") or ""
                ),
                prior_receipt_sha256=prior_receipt_sha256,
            )
        except ReviewQualificationError as exc:
            raise WorkerError(str(exc)) from exc
    if command == "brain.refresh.status":
        return get_refresh_status(
            workspace,
            brain_name,
            candidate_id=payload.get("candidate_id") or payload.get("candidateId"),
        )
    if command == "brain.refresh.output":
        return get_refresh_output(
            workspace,
            brain_name,
            candidate_id=payload.get("candidate_id") or payload.get("candidateId"),
        )
    if command in {"brain.refresh.start", "brain.refresh.retry"}:
        expected_snapshot_id = str(
            payload.get("expected_snapshot_id")
            or payload.get("expectedSnapshotId")
            or ""
        )
        if not expected_snapshot_id:
            raise WorkerError("REFRESH_EXPECTED_SNAPSHOT_ID_REQUIRED")
        brain_id = str(payload.get("brain_id") or payload.get("brainId") or "")
        if not brain_id:
            registered = next(
                (item for item in _list_brains(workspace) if item.get("brain_name") == brain_name),
                None,
            )
            brain_id = str((registered or {}).get("brain_id") or f"brain:{slugify_name(brain_name)}")
        lane_scope = payload.get("requested_lane_scope") or payload.get("requestedLaneScope")
        if isinstance(lane_scope, str):
            lane_scope = [lane_scope]
        sources = _sources_for(workspace, brain_name)
        progress = _progress_event(request.get("id", ""))
        if command == "brain.refresh.start":
            return start_refresh_brain(
                workspace,
                brain_name,
                brain_id=brain_id,
                expected_snapshot_id=expected_snapshot_id,
                sources=sources,
                requested_lane_scope=lane_scope,
                known_execution_id=payload.get("known_execution_id") or payload.get("knownExecutionId"),
                progress=progress,
            )
        candidate_id = str(payload.get("candidate_id") or payload.get("candidateId") or "")
        if not candidate_id:
            raise WorkerError("REFRESH_CANDIDATE_ID_REQUIRED")
        return retry_refresh_brain(
            workspace,
            brain_name,
            candidate_id=candidate_id,
            brain_id=brain_id,
            expected_snapshot_id=expected_snapshot_id,
            sources=sources,
            progress=progress,
        )
    if command == "brain.refresh.cancel":
        candidate_id = str(payload.get("candidate_id") or payload.get("candidateId") or "")
        if not candidate_id:
            raise WorkerError("REFRESH_CANDIDATE_ID_REQUIRED")
        return cancel_refresh_brain(
            workspace,
            brain_name,
            candidate_id=candidate_id,
            actor=str(payload.get("actor") or "Evidence OS user"),
            reason=str(payload.get("reason") or "explicit refresh cancellation"),
        )
    if command == "brain.refresh.hil.decide":
        candidate_id = str(payload.get("candidate_id") or payload.get("candidateId") or "")
        if not candidate_id:
            raise WorkerError("REFRESH_CANDIDATE_ID_REQUIRED")
        return record_refresh_hil_decision(
            workspace,
            brain_name,
            candidate_id=candidate_id,
            decision=str(payload.get("decision") or ""),
            actor=str(payload.get("actor") or ""),
            reviewer_type=str(payload.get("reviewer_type") or payload.get("reviewerType") or ""),
            reason=str(payload.get("reason") or ""),
        )
    if command == "brain.refresh.fuse":
        candidate_id = str(payload.get("candidate_id") or payload.get("candidateId") or "")
        if not candidate_id:
            raise WorkerError("REFRESH_CANDIDATE_ID_REQUIRED")
        fused = fuse_refresh_candidate(
            workspace,
            brain_name,
            candidate_id=candidate_id,
            actor=str(payload.get("actor") or ""),
            reason=str(payload.get("reason") or ""),
            source_commit=lambda accepted: _save_sources(workspace, brain_name, accepted),
        )
        if str(fused.get("status") or "") in {"FUSED", "PASS_FUSED_AS_NEW_IMMUTABLE_VERSION"}:
            fused = {
                **fused,
                "telemetry_cache": _materialize_post_commit_telemetry_cache(workspace, brain_name),
            }
        return fused
    if command == "brain.refresh.drop":
        candidate_id = str(payload.get("candidate_id") or payload.get("candidateId") or "")
        if not candidate_id:
            raise WorkerError("REFRESH_CANDIDATE_ID_REQUIRED")
        return drop_refresh_candidate(
            workspace,
            brain_name,
            candidate_id=candidate_id,
            actor=str(payload.get("actor") or ""),
            reason=str(payload.get("reason") or ""),
        )
    if command == "brain.refresh.cleanup":
        return cleanup_unfused_refresh_candidate(
            workspace,
            brain_name,
            candidate_id=payload.get("candidate_id") or payload.get("candidateId"),
            reason=str(payload.get("reason") or "application exit cleanup rule"),
        )
    if command == "brain.codexHandoff.status":
        return _codex_handoff_status(workspace, brain_name)
    if command == "brain.codexHandoff.create":
        return create_codex_brain_handoff(
            workspace,
            brain_name,
            actor=payload.get("actor") or "Evidence OS SQLite Builder",
            reason=payload.get("reason") or "create Codex brain diff handoff",
        )
    if command == "brain.codexHandoff.openFolder":
        status = _codex_handoff_status(workspace, brain_name)
        handoff = status.get("handoff") or {}
        handoff_folder = Path(str(handoff.get("handoff_folder") or ""))
        if status["status"] != "PASS" or not handoff_folder.is_dir():
            raise WorkerError("CODEX_HANDOFF_FOLDER_NOT_AVAILABLE")
        return _open_path(str(handoff_folder))
    if command == "brain.portablePackage.status":
        return _portable_package_status(workspace, brain_name, payload)
    if command == "brain.portablePackage.create":
        goal_pointer = payload.get("goal_pointer") or payload.get("goalPointer") or {}
        delta_ledger = payload.get("delta_ledger") or payload.get("deltaLedger") or {}
        if not goal_pointer.get("parent_goal_id") or not goal_pointer.get("current_task_pointer"):
            raise WorkerError("PORTABLE_GOAL_POINTER_REQUIRED")
        if not delta_ledger.get("delta_id"):
            raise WorkerError("PORTABLE_DELTA_LEDGER_REQUIRED")
        package_root = _portable_package_root(workspace, brain_name, payload)
        result = create_portable_brain_package(
            brain_output_dir(workspace, brain_name),
            package_root,
            goal_pointer=goal_pointer,
            delta_ledger=delta_ledger,
            latest_good_snapshot=payload.get("latest_good_snapshot") or payload.get("latestGoodSnapshot"),
        )
        ledger = _require_portable_ledger(package_root)
        delta_id = str(delta_ledger["delta_id"])
        with closing(sqlite3.connect(ledger)) as connection, connection:
            registered = connection.execute(
                "SELECT id FROM goal_delta_ledger WHERE delta_id=? ORDER BY id LIMIT 1", (delta_id,)
            ).fetchone()
        delta_row_id = int(registered[0]) if registered else register_goal_delta(
            ledger,
            {
                "delta_id": delta_id,
                "parent_goal_id": goal_pointer["parent_goal_id"],
                "run_id": str(goal_pointer.get("active_run_id") or goal_pointer.get("run_id") or "UNVERIFIED"),
                "task_pointer": goal_pointer["current_task_pointer"],
                "title": str(delta_ledger.get("delta_title") or delta_ledger.get("title") or delta_id),
                "prompt_pointer": str(delta_ledger.get("prompt_pointer") or "UNVERIFIED"),
                "insertion_reason": str(delta_ledger.get("insertion_reason") or "ADDITIVE_GOAL_DELTA"),
                "insertion_point": str(delta_ledger.get("insertion_point") or goal_pointer["current_task_pointer"]),
                "status": str(delta_ledger.get("status") or "OPEN_REGISTERED"),
                "existing_requirements_preserved": True,
                "current_task_replaced": False,
                "current_goal_restarted": False,
            },
        )
        validation = validate_portable_brain_package(package_root)
        if validation["status"] != "PASS":
            raise WorkerError("PORTABLE_PACKAGE_POST_REGISTRATION_INVALID:" + ";".join(validation["errors"]))
        return {**result, "delta_row_id": delta_row_id, "validation": validation}
    if command == "brain.portablePackage.validate":
        return validate_portable_brain_package(_portable_package_root(workspace, brain_name, payload))
    if command == "brain.portablePackage.query":
        package_root = _portable_package_root(workspace, brain_name, payload)
        ledger = _require_portable_ledger(package_root)
        result = query_immutable_snapshot(
            package_root / "brain_snapshot.sqlite",
            str(payload.get("query") or ""),
            limit=int(payload.get("limit") or 50),
        )
        query_row_id = record_project_query(
            ledger,
            {
                "task_run_id": payload.get("task_run_id") or payload.get("taskRunId"),
                "snapshot_hash": result["snapshot_hash"],
                "query_type": "FTS5_IMMUTABLE_SNAPSHOT",
                "query_text": result["query"],
                "tables_queried": ["snapshot_fts"],
                "result_count": len(result["results"]),
            },
        )
        return {**result, "query_log_row_id": query_row_id}
    if command == "brain.codexLedger.task.append":
        package_root = _portable_package_root(workspace, brain_name, payload)
        row_id = append_task_run(_require_portable_ledger(package_root), payload.get("record") or {})
        return {"status": "APPENDED", "task_run_id": row_id}
    if command == "brain.codexLedger.usage.record":
        package_root = _portable_package_root(workspace, brain_name, payload)
        row_id = record_model_usage(_require_portable_ledger(package_root), payload.get("record") or {})
        return {"status": "APPENDED", "usage_row_id": row_id}
    if command == "brain.codexLedger.rq.record":
        package_root = _portable_package_root(workspace, brain_name, payload)
        task_run_id = int(payload.get("task_run_id") or payload.get("taskRunId") or 0)
        if task_run_id <= 0:
            raise WorkerError("TASK_RUN_ID_REQUIRED")
        rows = record_rq_answers(
            _require_portable_ledger(package_root),
            task_run_id,
            payload.get("answers") or {},
            payload.get("evidence") or {},
        )
        return {"status": "APPENDED", "rq_row_ids": rows}
    if command == "brain.codexLedger.steerPrompt.record":
        package_root = _portable_package_root(workspace, brain_name, payload)
        result = record_steer_prompt_answer(
            _require_portable_ledger(package_root),
            payload.get("record") or {},
        )
        return {"status": "APPENDED", "steer_prompt_row_id": result["row_id"], "goal_usage": result["goal_usage"]}
    if command == "brain.modelLedger.execution.append":
        package_root = _portable_package_root(workspace, brain_name, payload)
        row_id = append_model_execution_run(_require_portable_ledger(package_root), payload.get("record") or {})
        return {"status": "APPENDED", "model_execution_run_id": row_id}
    if command == "brain.modelLedger.endpointEvent.append":
        package_root = _portable_package_root(workspace, brain_name, payload)
        row_id = append_endpoint_execution_event(_require_portable_ledger(package_root), payload.get("record") or {})
        return {"status": "APPENDED", "endpoint_execution_event_id": row_id}
    if command == "brain.modelLedger.failure.append":
        package_root = _portable_package_root(workspace, brain_name, payload)
        row_id = append_model_failure_event(_require_portable_ledger(package_root), payload.get("record") or {})
        return {"status": "APPENDED", "model_failure_event_id": row_id}
    if command == "brain.portablePackage.export":
        package_root = _portable_package_root(workspace, brain_name, payload)
        pointer_path = package_root / "pointers" / "CURRENT_BRAIN_POINTER.json"
        if not pointer_path.is_file():
            raise WorkerError("PORTABLE_SNAPSHOT_POINTER_MISSING")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        suffix = str(pointer.get("input_set_hash") or "UNVERIFIED")[:16]
        requested = payload.get("destination") or payload.get("archivePath")
        destination = Path(str(requested)).resolve() if requested else (
            workspace / "packages" / f"{slugify_name(brain_name)}_portable_{suffix}.zip"
        ).resolve()
        workspace_root = workspace.resolve()
        try:
            destination.relative_to(workspace_root)
        except ValueError as exc:
            raise WorkerError("PORTABLE_EXPORT_OUTSIDE_WORKSPACE") from exc
        if destination.is_file():
            existing = validate_portable_brain_zip(destination)
            if existing["status"] != "PASS":
                raise WorkerError("EXISTING_PORTABLE_EXPORT_INVALID")
            return {**existing, "reused": True}
        return {**export_portable_brain_zip(package_root, destination), "reused": False}
    if command == "brain.version.rollback":
        version_id = payload.get("version_id") or payload.get("versionId")
        if not version_id:
            raise WorkerError("VERSION_ID_REQUIRED")
        return rollback_brain_as_complete_product(
            workspace,
            brain_name,
            str(version_id),
            actor_name=payload.get("actor_name") or payload.get("actorName") or "",
            reason=payload.get("reason") or "",
            restored_brain_name=payload.get("restored_brain_name") or payload.get("restoredBrainName"),
        )
    if command == "brain.version.drop":
        version_id = payload.get("version_id") or payload.get("versionId")
        if not version_id:
            raise WorkerError("VERSION_ID_REQUIRED")
        return drop_brain_version_to_recycle_bin(
            workspace,
            brain_name,
            str(version_id),
            confirm_version_id=str(payload.get("confirm_version_id") or payload.get("confirmVersionId") or ""),
            actor_name=str(payload.get("actor_name") or payload.get("actorName") or ""),
            reason=str(payload.get("reason") or ""),
        )
    if command == "brain.sqlite.open":
        sqlite_path = brain_output_dir(workspace, brain_name) / "project" / "project_router.sqlite"
        return _open_path(str(sqlite_path))
    if command == "sources.add":
        source = _add_source(workspace, payload)
        return {"source": source, "sources": _sources_for(workspace, brain_name)}
    if command == "sources.cloneGithub":
        return _clone_github_source(workspace, payload)
    if command == "sources.list":
        return {"sources": _sources_for(workspace, brain_name)}
    if command == "sources.remove":
        source_id = payload.get("source_id") or payload.get("sourceId")
        if not source_id:
            raise WorkerError("SOURCE_ID_REQUIRED")
        current_sources = _sources_for(workspace, brain_name)
        removed_source = next((source for source in current_sources if source.get("source_id") == source_id), None)
        activity = {
            "status": "SOURCE_UNREGISTERED_SECTOR_BYTES_PRESERVED",
            "source_id": str(source_id),
            "lane_id": str((removed_source or {}).get("lane_key") or "custom"),
            "verified_brain_mutated": False,
        }
        sources = [source for source in current_sources if source.get("source_id") != source_id]
        _save_sources(workspace, brain_name, sources)
        receipt = _write_operation_receipt(
            workspace,
            "sources.remove",
            {
                "brain_name": brain_name,
                "source_id": str(source_id),
                "lane_id": activity["lane_id"],
                "source_was_registered": removed_source is not None,
                "intake_provenance": str((removed_source or {}).get("intake_provenance") or "UNVERIFIED"),
                "verified_brain_mutated": False,
                "next_build_action": "SKIPPED_NO_SOURCE_PRESERVE_PRIOR_SECTOR_BYTES",
            },
        )
        return {"sources": sources, "removed": source_id, "activity": activity, "receipt": receipt}
    if command == "sources.removeLane":
        requested_lane = payload.get("lane_key") or payload.get("laneKey")
        if not requested_lane:
            raise WorkerError("LANE_KEY_REQUIRED")
        try:
            lane_key = resolve_lane_id(requested_lane, scope="any")
        except UnknownLaneAliasError as exc:
            raise WorkerError(f"UNKNOWN_LANE_ALIAS:{requested_lane}") from exc
        existing = _sources_for(workspace, brain_name)
        sources = []
        removed_sources = []
        for source in existing:
            stored_lane = source.get("lane_key") or "custom"
            try:
                stored_lane = resolve_lane_id(stored_lane, scope="any")
            except UnknownLaneAliasError:
                pass
            if stored_lane != lane_key:
                sources.append(source)
            else:
                removed_sources.append(source)
        removed = len(existing) - len(sources)
        activity = [
            {
                "status": "SOURCE_UNREGISTERED_SECTOR_BYTES_PRESERVED",
                "source_id": str(source.get("source_id")),
                "lane_id": lane_key,
                "verified_brain_mutated": False,
            }
            for source in removed_sources
        ]
        _save_sources(workspace, brain_name, sources)
        return {"sources": sources, "removed_lane": lane_key, "removed_count": removed, "activity": activity}
    if command == "tools.scan":
        return scan_tools()
    if command == "metrics.snapshot":
        # Public machine telemetry contract consumed by the desktop process
        # cluster.  Keep this separate from process-scoped build attribution:
        # the UI expects the stable CPU/GPU/RAM/SSD/HDD/IO keys returned by
        # ProcessMetricSampler.
        return _machine_metrics_snapshot(workspace)
    if command in {"process.metrics.snapshot", "processes.metrics.snapshot"}:
        return _process_registry(
            workspace,
            brain_name,
            payload,
            register_python_worker=False,
        ).sample_metrics(active_process_pid=max(0, int(payload.get("process_pid") or 0)))
    if command == "runtime.snapshot":
        runtime_dir = _runtime_state_dir(workspace, brain_name, create=False)
        state_path = runtime_dir / "pipeline_state.json"
        pipeline = None
        history: list[dict[str, Any]] = []
        if state_path.exists():
            try:
                state_document = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state_document, dict):
                    pipeline = state_document.get("event", state_document.get("pipeline", state_document))
                    raw_history = state_document.get("history", [])
                    history = raw_history if isinstance(raw_history, list) else []
            except (OSError, json.JSONDecodeError):
                pipeline = None
        registry = _process_registry(workspace, brain_name, payload, initialize=False)
        processes = registry.read_only_snapshot()
        return {
            "process_metrics": registry.read_last_metrics(),
            "processes": processes,
            "task_event": _read_authoritative_task_snapshot(
                workspace,
                registry,
                brain_name=brain_name,
            ),
            "pipeline": {
                "pipeline": pipeline,
                "history": history,
                "stages": _backend_stage_states(pipeline),
            },
        }
    if command == "pipeline.snapshot":
        store = PipelineStateStore(_runtime_state_dir(workspace, brain_name) / "pipeline_state.json")
        pipeline = store.snapshot()
        registry = _process_registry(workspace, brain_name, payload, initialize=False)
        return {
            "pipeline": pipeline,
            "history": store.history() if pipeline else [],
            "stages": _backend_stage_states(pipeline),
            "task_event": _read_authoritative_task_snapshot(
                workspace,
                registry,
                brain_name=brain_name,
            ),
        }
    if command == "processes.snapshot":
        return _process_registry(workspace, brain_name, payload).snapshot(prune_stale=True)
    if command == "brain.build":
        sources = payload.get("sources") if "sources" in payload else _sources_for(workspace, brain_name)
        sources = sources if isinstance(sources, list) else []
        sources = _append_request_prompt_source(sources, payload.get("request_text") or payload.get("requestText"))
        normalized = _sources_for_build(workspace, brain_name, sources)
        try:
            result = build_brain(str(workspace), brain_name, normalized, _progress_event(request.get("id", "")))
        except Exception as exc:
            _mark_source_build_status(workspace, brain_name, normalized, "error", error_code=_pipeline_error_code(exc))
            raise
        _mark_source_build_status(workspace, brain_name, normalized, "built")
        return result
    if command == "brain.buildAll":
        sources = payload.get("sources") if "sources" in payload else _sources_for(workspace, brain_name)
        sources = sources if isinstance(sources, list) else []
        sources = _append_request_prompt_source(sources, payload.get("request_text") or payload.get("requestText"))
        request_id = request.get("id", "")
        progress = _progress_event(request_id)
        runtime_state = _runtime_state_dir(workspace, brain_name)
        pipeline = PipelineStateStore(runtime_state / "pipeline_state.json")
        registry = _process_registry(workspace, brain_name, payload)
        normalized_sources: list[dict[str, Any]] = []
        files_total = len([source for source in sources if source.get("active", True)])
        event = pipeline.start(
            request_id=request_id,
            brain_name=brain_name,
            workspace_dir=str(workspace),
            process_pid=os.getpid(),
            files_total=files_total,
            active_command="validate and register sources",
            replace=True,
        )
        _publish_pipeline_event(request_id, registry, event)

        def update_pipeline(values: dict[str, Any]) -> dict[str, Any]:
            normalized_values = dict(values)
            if "stage_id" in normalized_values and "stage" not in normalized_values:
                normalized_values["stage"] = normalized_values.pop("stage_id")
            next_event = pipeline.update(**normalized_values)
            return _publish_pipeline_event(request_id, registry, next_event)

        try:
            normalized_sources = _validate_build_sources(workspace, brain_name, sources)
            update_pipeline(
                {
                    "stage": "source_validation_registration",
                    "stage_percent": 100,
                    "files_total": files_total,
                    "active_command": "sources validated and canonically registered",
                }
            )
            build_result = build_brain(
                str(workspace),
                brain_name,
                normalized_sources,
                progress,
                update_pipeline,
                False,
            )
            _mark_source_build_status(workspace, brain_name, normalized_sources, "built")
            brain_root = build_result.get("brain_root") if isinstance(build_result, dict) else None
            live_env15_root = bool(brain_root and (Path(brain_root).resolve() / ".uepc_env").is_file())
            incremental = build_result.get("incremental") if isinstance(build_result, dict) else {}
            # Older test doubles and compatibility runtimes do not report this
            # field. Only an explicit False activates the governed no-code
            # path; the live runtime always reports it.
            code_lanes_loaded = incremental.get("code_lanes_loaded") if isinstance(incremental, dict) else None
            code_topology_required = code_lanes_loaded is not False
            code_lane_execution = {
                "status": "LOADED" if code_topology_required else "SKIPPED_NO_CODE_LANES",
                "reason": (
                    "active code lane topology required"
                    if code_topology_required
                    else "topology skipped: no active code lanes"
                ),
                "env15_template_topology_materialized": not code_topology_required,
            }
            active_source_count = len([source for source in normalized_sources if source.get("active", True)])
            package_mode = (
                "FULL_PROJECT_WITH_CODE_TOPOLOGY"
                if code_topology_required
                else "PROJECT_SECTORS_WITH_ENV15_TEMPLATE_TOPOLOGY"
                if active_source_count
                else "ENVIRONMENT_ONLY_SCHEMA_READY"
            )
            provider_package_use_mode = str(
                payload.get("provider_package_use_mode")
                or payload.get("providerPackageUseMode")
                or "CANONICAL_FLASHABLE"
            ).strip().upper()
            if provider_package_use_mode not in {"CANONICAL_FLASHABLE", "READ_ONLY_STRESS_RESULT"}:
                raise WorkerError(f"PACKAGE_USE_MODE_INVALID:{provider_package_use_mode}")
            reuse_products = (
                _reusable_full_build_products(
                    workspace,
                    brain_name,
                    brain_root,
                    provider_package_use_mode,
                )
                if brain_root and incremental.get("all_sources_unchanged")
                else None
            )
            schema_only_topology = (
                materialize_schema_only_package_topology(brain_root)
                if brain_root and not code_topology_required and not reuse_products
                else None
            )
            rows_written, chunks_written = _project_row_chunk_counts(brain_root) if brain_root else (0, 0)
            update_pipeline(
                {
                    "stage": "pointer_router_hash_finalization",
                    "stage_percent": 100,
                    "files_done": files_total,
                    "files_total": files_total,
                    "rows_written": rows_written,
                    "chunks_written": chunks_written,
                    "active_command": "unchanged pointer/router hashes reused" if reuse_products else "pointer router and hash finalization complete",
                }
            )

            update_pipeline(
                {"stage": "project_mmd_generation", "stage_percent": 0, "active_command": "generate project MMD"}
            )
            mmd_result = (
                reuse_products["mmd"]
                if reuse_products
                else schema_only_topology
                if schema_only_topology
                else generate_project_mmd(str(workspace), brain_name, progress) if brain_root else {"status": "handled_by_test_double"}
            )
            update_pipeline(
                {"stage": "project_mmd_generation", "stage_percent": 100, "active_command": "Env15 schema topology materialized without firing unloaded lanes" if schema_only_topology else "unchanged MMDs reused" if reuse_products else "project MMD generated"}
            )

            update_pipeline(
                {"stage": "svg_png_rendering", "stage_percent": 0, "active_command": "render project SVG and PNG"}
            )
            render_result = schema_only_topology if schema_only_topology else reuse_products["render"] if reuse_products else (
                render_topology(str(workspace), brain_name, progress, False)
                if brain_root
                else render_topology(str(workspace), brain_name, progress)
            )
            rendered = render_result.get("rendered") if isinstance(render_result, dict) else None
            if rendered is not None and not _topology_render_contract_passes(rendered):
                raise WorkerError("SVG_PNG_RENDER_VALIDATION_FAILED")
            update_pipeline(
                {"stage": "svg_png_rendering", "stage_percent": 100, "active_command": "locked Env15 topology proof reused without source-lane execution" if schema_only_topology else "unchanged SVG and PNG renders reused" if reuse_products else "SVG and PNG rendering validated"}
            )

            update_pipeline(
                {"stage": "chatgpt_package_compilation", "stage_percent": 0, "active_command": "compile shared ChatGPT and headless LocalAI package"}
            )
            package_result = reuse_products["chatgpt"] if reuse_products else (
                export_one_upload_package(
                    str(workspace),
                    brain_name,
                    progress,
                    package_use_mode=provider_package_use_mode,
                )
                if export_one_upload_package is _CANONICAL_CHATGPT_EXPORTER
                else export_one_upload_package(str(workspace), brain_name, progress)
            )
            chatgpt_skipped = bool(
                isinstance(package_result, dict)
                and package_result.get("skipped")
            )
            update_pipeline(
                {
                    "stage": "chatgpt_package_compilation",
                    "stage_percent": 100,
                    "active_command": (
                        "ChatGPT package skipped at exact 512000000-byte law; Codex continues"
                        if chatgpt_skipped
                        else "unchanged ChatGPT_LocalAI package reused"
                        if reuse_products
                        else f"shared ChatGPT_LocalAI package compiled in {package_mode} mode"
                    ),
                }
            )

            project_package_parity = {"status": "SKIPPED_COMPATIBILITY_TEST_DOUBLE"}
            non_materializing_compatibility_exporter = (
                export_one_upload_package is not _CANONICAL_CHATGPT_EXPORTER
                and isinstance(package_result, dict)
                and not package_result.get("package_folder")
                and not package_result.get("package_zip")
            )
            if reuse_products:
                project_package_parity = {
                    "status": "PASS",
                    "validation_mode": "REUSED_ARCHIVE_ALREADY_REOPENED_AND_VALIDATED",
                }
            elif chatgpt_skipped:
                project_package_parity = {
                    "status": "SKIPPED",
                    "reason": "CHATGPT_PROJECT_CORPUS_TOO_LARGE_OPTIONAL_PROVIDER_SKIP",
                }
            elif (
                isinstance(package_result, dict)
                and isinstance(package_result.get("project_package_parity"), dict)
            ):
                project_package_parity = package_result["project_package_parity"]
                if project_package_parity.get("status") != "PASS":
                    raise WorkerError(
                        "ACTIVE_PROJECT_PACKAGE_PARITY_FAILED:"
                        + ";".join(project_package_parity.get("errors") or [])
                    )
            elif live_env15_root and not non_materializing_compatibility_exporter:
                package_folder = str(
                    (package_result.get("package_folder") if isinstance(package_result, dict) else "")
                    or (Path(brain_root).resolve() / "packages" / f"{slugify_name(brain_name)}_one_upload_package_v001")
                )
                project_package_parity = validate_active_project_package_parity(brain_root, package_folder)
                if project_package_parity.get("status") != "PASS":
                    raise WorkerError(
                        "ACTIVE_PROJECT_PACKAGE_PARITY_FAILED:"
                        + ";".join(project_package_parity.get("errors") or [])
                    )

            update_pipeline(
                {"stage": "gemini_exact10_compilation", "stage_percent": 0, "active_command": "compile Gemini provider-readable normal package"}
            )
            chatgpt_package_path = (
                str(package_result.get("package_zip") or "")
                if isinstance(package_result, dict)
                else ""
            )
            if chatgpt_skipped:
                downstream_skip = (
                    package_result.get("downstream_gemini_skip")
                    if isinstance(package_result, dict)
                    else None
                ) or write_provider_skip_marker(
                    Path(brain_root).resolve() / "packages",
                    "GEMINI",
                    observed_bytes=None,
                    maximum_bytes=GEMINI_PACKAGE_MAX_BYTES,
                    reason="GEMINI_SKIPPED_BECAUSE_CHATGPT_512000000_BYTE_PARENT_LIMIT_WAS_EXCEEDED",
                )
                gemini_result = {
                    **downstream_skip,
                    "gemini_package_zip": "",
                    "package_folder": "",
                    "sha256": "",
                    "source_chatgpt_package_sha256": "",
                    "staging_removed": True,
                }
            else:
                gemini_result = (
                reuse_products["gemini"]
                if reuse_products
                else (
                    export_gemini_exact10(
                        str(workspace),
                        brain_name,
                        progress,
                        chatgpt_package=chatgpt_package_path or None,
                        package_use_mode=provider_package_use_mode,
                    )
                    if _CANONICAL_GEMINI_EXPORTER is export_gemini_exact10
                    else export_gemini_exact10(
                        str(workspace),
                        brain_name,
                        progress,
                        chatgpt_package=chatgpt_package_path or None,
                    )
                )
                if chatgpt_package_path
                else (
                    export_gemini_exact10(
                        str(workspace),
                        brain_name,
                        progress,
                        package_use_mode=provider_package_use_mode,
                    )
                    if _CANONICAL_GEMINI_EXPORTER is export_gemini_exact10
                    else export_gemini_exact10(str(workspace), brain_name, progress)
                )
                )
            gemini_skipped = bool(
                isinstance(gemini_result, dict)
                and gemini_result.get("skipped")
            )
            update_pipeline(
                {
                    "stage": "gemini_exact10_compilation",
                    "stage_percent": 100,
                    "active_command": (
                        "Gemini package skipped at exact 100000000-byte law; Codex continues"
                        if gemini_skipped
                        else "unchanged Gemini provider-readable package reused"
                        if reuse_products
                        else f"Gemini provider-readable package compiled from ChatGPT in {package_mode} mode"
                    ),
                }
            )

            update_pipeline(
                {"stage": "package_hash_validation", "stage_percent": 0, "active_command": "reopen and validate packages"}
            )
            chatgpt_validation = package_result.get("validation") if isinstance(package_result, dict) else None
            gemini_validation = gemini_result.get("validation") if isinstance(gemini_result, dict) else None
            trusted_chatgpt_validation = _hash_bound_export_validation(package_result, "package_zip")
            trusted_gemini_validation = _hash_bound_export_validation(gemini_result, "gemini_package_zip")
            if not reuse_products and isinstance(package_result, dict) and package_result.get("package_zip") and trusted_chatgpt_validation is None:
                chatgpt_validation = validate_chatgpt_package(package_result["package_zip"])
            elif trusted_chatgpt_validation is not None:
                chatgpt_validation = trusted_chatgpt_validation
            if not reuse_products and isinstance(gemini_result, dict) and gemini_result.get("gemini_package_zip") and trusted_gemini_validation is None:
                gemini_validation = validate_gemini_exact10(gemini_result["gemini_package_zip"])
            elif trusted_gemini_validation is not None:
                gemini_validation = trusted_gemini_validation
            for label, validation in (("CHATGPT", chatgpt_validation), ("GEMINI", gemini_validation)):
                if validation is not None and validation.get("status") not in {"PASS", "SKIPPED"}:
                    raise WorkerError(f"{label}_PACKAGE_VALIDATION_FAILED:" + ";".join(validation.get("errors") or []))
            gemini_package_path = (
                str(gemini_result.get("gemini_package_zip") or "")
                if isinstance(gemini_result, dict)
                else ""
            )
            if chatgpt_package_path and gemini_package_path:
                chatgpt_chain_sha256 = str(
                    (chatgpt_validation.get("sha256") if isinstance(chatgpt_validation, dict) else "")
                    or (package_result.get("sha256") if isinstance(package_result, dict) else "")
                    or ""
                )
                gemini_source_chatgpt_sha256 = str(
                    (gemini_validation.get("source_chatgpt_package_sha256") if isinstance(gemini_validation, dict) else "")
                    or (gemini_result.get("source_chatgpt_package_sha256") if isinstance(gemini_result, dict) else "")
                    or ""
                )
                if not chatgpt_chain_sha256 or gemini_source_chatgpt_sha256 != chatgpt_chain_sha256:
                    raise WorkerError("GEMINI_CHATGPT_DERIVATION_HASH_MISMATCH")
            upstream_package_chain = {
                "chatgpt_local_ai": {
                    "status": (
                        "SKIPPED_PROJECT_CORPUS_TOO_LARGE"
                        if chatgpt_skipped
                        else "PASS"
                    ),
                    "path": chatgpt_package_path,
                    "sha256": str(
                        (package_result.get("sha256") if isinstance(package_result, dict) else "")
                        or (chatgpt_validation.get("sha256") if isinstance(chatgpt_validation, dict) else "")
                        or ""
                    ),
                    "role": "SHARED_CHATGPT_PUBLIC_AND_HEADLESS_LOCAL_AI_PACKAGE",
                    "package_mode": package_mode,
                    "provider_package_use_mode": provider_package_use_mode,
                },
                "gemini": {
                    "status": (
                        "SKIPPED_PROJECT_CORPUS_TOO_LARGE"
                        if gemini_skipped
                        else "PASS"
                    ),
                    "path": gemini_package_path,
                    "sha256": str(
                        (gemini_result.get("sha256") if isinstance(gemini_result, dict) else "")
                        or (gemini_validation.get("sha256") if isinstance(gemini_validation, dict) else "")
                        or ""
                    ),
                    "role": "PROVIDER_READABLE_NORMAL_DERIVED_FROM_CHATGPT_LOCAL_AI",
                    "package_mode": package_mode,
                    "source_chatgpt_package_sha256": str(
                        gemini_result.get("source_chatgpt_package_sha256")
                        if isinstance(gemini_result, dict)
                        else ""
                    ),
                },
            }
            if reuse_products:
                codex_result = reuse_products["codex"]
            elif brain_root:
                goal_pointer, delta_ledger = _build_package_governance(request_id, brain_name, payload)
                delta_ledger = {
                    **delta_ledger,
                    "upstream_package_chain": upstream_package_chain,
                    "brain_diff_transport": "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE",
                    "package_level": "CODEX_HIGHER_THAN_CHATGPT_AND_GEMINI",
                }
                codex_result = create_codex_env15_package(
                    brain_root,
                    _portable_package_root(workspace, brain_name, payload),
                    Path(brain_root).resolve() / "packages",
                    brain_name=brain_name,
                    goal_pointer=goal_pointer,
                    delta_ledger=delta_ledger,
                    latest_good_snapshot=_latest_good_snapshot_for_package(workspace, brain_name),
                    snapshot_source_root=(
                        package_result.get("package_folder")
                        if isinstance(package_result, dict) and package_result.get("package_folder")
                        else None
                    ),
                    chatgpt_package=chatgpt_package_path or None,
                    gemini_package=gemini_package_path or None,
                )
            else:
                codex_result = {"status": "SKIPPED_COMPATIBILITY_TEST_DOUBLE", "validation": None, "archive_validation": None}
            codex_validation = (
                codex_result.get("archive_validation") or codex_result.get("validation")
                if isinstance(codex_result, dict)
                else None
            )
            if codex_validation is not None and codex_validation.get("status") != "PASS":
                raise WorkerError("CODEX_UNIVERSAL_PACKAGE_VALIDATION_FAILED:" + ";".join(codex_validation.get("errors") or []))

            provider_staging_cleanup = {
                "chatgpt": False,
                "gemini": False,
            }
            if brain_root:
                package_result = _remove_uncompressed_provider_stage(brain_root, package_result)
                gemini_result = _remove_uncompressed_provider_stage(brain_root, gemini_result)
                provider_staging_cleanup = {
                    "chatgpt": bool(
                        isinstance(package_result, dict)
                        and package_result.get("staging_removed")
                    ),
                    "gemini": bool(
                        isinstance(gemini_result, dict)
                        and gemini_result.get("staging_removed")
                    ),
                }

            build_truth_ledger = None
            flash_prompt = None
            if live_env15_root:
                mmd_paths = []
                if isinstance(mmd_result, dict):
                    mmd_paths = list(mmd_result.get("mmd_files") or mmd_result.get("files") or [])
                package_paths = [
                    path
                    for path in (
                        chatgpt_package_path,
                        gemini_package_path,
                        str(codex_result.get("archive") or "") if isinstance(codex_result, dict) else "",
                    )
                    if path
                ]
                build_truth_ledger = record_workspace_build_truth(
                    workspace,
                    brain_name,
                    request_id=str(request_id),
                    sources=normalized_sources,
                    brain_root=brain_root,
                    package_paths=package_paths,
                    mmd_paths=mmd_paths,
                    build_receipt=Path(brain_root) / "receipts" / "build_receipt.md",
                )
                flash_prompt = _read_project_flash_prompt(
                    workspace,
                    brain_name,
                    {"provider": str(payload.get("flash_provider") or "configured provider")},
                )
            update_pipeline(
                {"stage": "package_hash_validation", "stage_percent": 100, "active_command": "prior validated ChatGPT_LocalAI, Gemini, and Codex package receipts reused" if reuse_products else "ChatGPT_LocalAI, Gemini, and Env15-derived Codex package hashes and SQLite contents validated"}
            )

            update_pipeline(
                {"stage": "immutable_version_capture", "stage_percent": 0, "active_command": "capture immutable version"}
            )
            version_result = reuse_products["version"] if reuse_products else capture_brain_version(
                workspace,
                brain_name,
                actor_type="app",
                actor_name="Evidence OS SQLite Builder",
                reason="brain.buildAll successful",
                change_summary="canonical brain build, project topology render, validated ChatGPT export, validated Gemini provider-readable export, and validated Env15-derived Codex universal execution package completed",
            )
            telemetry_cache = _materialize_post_commit_telemetry_cache(workspace, brain_name)
            completed = pipeline.complete(
                files_done=files_total,
                rows_written=rows_written,
                chunks_written=chunks_written,
                active_command="unchanged immutable version reused" if reuse_products else "immutable version captured",
            )
            _publish_pipeline_event(request_id, registry, completed)
            process_metrics = registry.sample_metrics()
            return {
                "build": build_result,
                "mmd": mmd_result,
                "render": render_result,
                "chatgpt_export": package_result,
                "gemini_export": gemini_result,
                "codex_universal_execution_package": codex_result,
                "package_validation": {
                    "chatgpt": chatgpt_validation,
                    "gemini": gemini_validation,
                    "codex_universal": codex_validation,
                    "project_parity": project_package_parity,
                },
                "workspace_build_truth": build_truth_ledger,
                "flash_prompt": flash_prompt,
                "package_chain": {
                    "contract": "T023_PROVIDER_PACKAGE_CHAIN_V1",
                    "package_mode": package_mode,
                    "order": [
                        "CHATGPT_LOCAL_AI_SHARED",
                        "GEMINI_FROM_CHATGPT_LOCAL_AI",
                        "CODEX_HIGHER_WITH_BRAIN_DIFFS",
                    ],
                    "upstream_packages": upstream_package_chain,
                    "codex_brain_diff_transport": "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE",
                    "local_ai_package_transport": "CHATGPT_LOCAL_AI_SHARED_HEADLESS_PACKAGE",
                },
                "provider_staging_cleanup": provider_staging_cleanup,
                "version": version_result,
                "telemetry_cache": telemetry_cache,
                "pipeline": completed,
                "process_metrics": process_metrics,
                "incremental_reuse": {
                    "reused": bool(reuse_products),
                    "reason": "ALL_SOURCES_UNCHANGED" if reuse_products else "CHANGED_OR_REQUIRED_PRODUCT_MISSING",
                },
                "lane_execution": {
                    "code_lanes": code_lane_execution,
                    "unloaded_lanes_not_fired": list((incremental or {}).get("unloaded_lane_ids") or []),
                },
                "summary": _brain_summary(workspace, brain_name),
            }
        except Exception as exc:
            if normalized_sources:
                try:
                    _mark_source_build_status(
                        workspace, brain_name, normalized_sources, "error", error_code=_pipeline_error_code(exc)
                    )
                except Exception:
                    pass
            try:
                failed = pipeline.fail(_pipeline_error_code(exc))
                _publish_pipeline_event(request_id, registry, failed)
            except Exception:
                pass
            raise
    if command == "topology.render":
        return render_topology(str(workspace), brain_name, _progress_event(request.get("id", "")))
    if command == "package.exportChatGPT":
        return export_one_upload_package(
            str(workspace),
            brain_name,
            _progress_event(request.get("id", "")),
            package_use_mode=str(
                payload.get("provider_package_use_mode")
                or payload.get("providerPackageUseMode")
                or "CANONICAL_FLASHABLE"
            ),
        )
    if command == "package.exportGemini":
        return export_gemini_exact10(
            str(workspace),
            brain_name,
            _progress_event(request.get("id", "")),
            package_use_mode=str(
                payload.get("provider_package_use_mode")
                or payload.get("providerPackageUseMode")
                or "CANONICAL_FLASHABLE"
            ),
        )
    if command == "flash.read":
        return _read_project_flash_prompt(workspace, brain_name, payload)
    if command == "brain.outputRoute.inspect":
        summary = _brain_summary(workspace, brain_name)
        output_dir = Path(str(summary["output_dir"])).resolve()
        package_dir = output_dir / "packages"
        return {
            "schema": "T023_SELECTED_BRAIN_OUTPUT_ROUTE_V1",
            "status": "PASS" if output_dir.is_dir() else "OUTPUT_NOT_MATERIALIZED",
            "read_only": True,
            "selected_brain_only": True,
            "brain_name": str(brain_name),
            "workspace_dir": str(workspace.resolve()),
            "output_dir": str(output_dir),
            "output_folder_name": str(summary["output_folder_name"]),
            "sqlite_brain_path": str(summary["sqlite_brain_path"]),
            "topology_path": str(summary["topology_path"]),
            "packages_dir": str(package_dir),
            "packages_dir_exists": package_dir.is_dir(),
            "packages": list(summary["packages"]),
        }
    if command == "folder.open":
        target = str(payload.get("target") or "").strip().casefold()
        explicit_path = str(payload.get("path") or "").strip()
        if target == "brain_packages" or (not target and not explicit_path):
            destination = brain_output_dir(workspace, brain_name) / "packages"
            destination.mkdir(parents=True, exist_ok=True)
            opened = _open_path(str(destination))
            return {
                **opened,
                "target": "brain_packages",
                "brain_name": str(brain_name),
                "workspace_dir": str(workspace.resolve()),
                "selected_brain_route": True,
            }
        if target == "workspace_root":
            return _open_path(str(workspace.resolve()))
        if target:
            raise WorkerError(f"FOLDER_TARGET_UNSUPPORTED:{target}")
        return _open_path(explicit_path)
    raise WorkerError(f"UNKNOWN_COMMAND: {command}")


def _serve_request(raw: str) -> int:
    raw = raw.strip()
    if not raw:
        _response("", False, error="EMPTY_REQUEST")
        return 2
    try:
        request = json.loads(raw)
    except Exception as exc:
        _response("", False, error=f"INVALID_JSON: {exc}")
        return 2
    request_id = str(request.get("id") or "")
    request_payload = request.get("payload") or {}
    command = str(request.get("command") or "")
    event_context = {
        "command": command,
        "brain_name": request_payload.get("brain_name") or request_payload.get("brainName") or "New Brain",
        "workspace_dir": str(request_payload.get("workspace_dir") or request_payload.get("workspaceDir") or ""),
        "process_pid": os.getpid(),
    }
    try:
        brain_name = event_context["brain_name"]
        # The desktop shell probes the embedded worker before any user or
        # workspace route is resolved. Keep that handshake genuinely
        # workspace-free so first boot cannot create data or fail because an
        # output root has not been selected yet.
        workspace = None if command == "system.ping" else _workspace(request_payload)
        task_event = None
        if command in _PROCESS_TRUTH_COMMANDS:
            if workspace is None:
                raise WorkerError(f"WORKSPACE_REQUIRED_FOR_TASK_EVENT:{command}")
            task_event = _start_authoritative_task(
                request_id,
                command,
                workspace,
                brain_name,
                request_payload,
            )
        _event(
            "task.started",
            request_id,
            task_event or {**event_context, "status": "running", "error_code": None},
        )
        if command in _MUTATING_COMMANDS:
            if workspace is None:
                raise WorkerError(f"WORKSPACE_REQUIRED_FOR_MUTATION:{command}")
            with _mutation_coordinator(workspace, brain_name, command):
                result = handle(request)
        else:
            result = handle(request)
        terminal_event = _finish_authoritative_task(request_id, result)
        _event(
            "task.done",
            request_id,
            terminal_event or {**event_context, "status": "completed", "error_code": None},
        )
        _response(request_id, True, result=result)
        return 0
    except Exception as exc:
        report = _write_worker_failure_report(request, exc)
        error = str(exc)
        if report:
            error = f"{error}:diagnostic={report}"
        error_code = _pipeline_error_code(exc)
        terminal_event = _finish_authoritative_task(request_id, None, error_code=error_code)
        _event(
            "task.error",
            request_id,
            {
                **(terminal_event or event_context),
                "status": "failed",
                "error_code": error_code,
                "error": error,
            },
        )
        if os.environ.get("EVIDENCE_OS_DEBUG_WORKER"):
            traceback.print_exc(file=sys.stderr)
        _response(request_id, False, error=error)
        return 1
    finally:
        _ACTIVE_TASK_CONTEXTS.pop(request_id, None)


def main() -> int:
    exit_code = 0
    for raw in sys.stdin:
        exit_code = max(exit_code, _serve_request(raw))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
