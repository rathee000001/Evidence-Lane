from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_DELTA_MANIFEST_SCHEMA = "EVIDENCEOS_PROJECT_DELTA_MANIFEST_V2"
PROJECT_DELTA_EVENT_SCHEMA = "EVIDENCEOS_PROJECT_DELTA_EVENT_V1"
PROJECT_DELTA_PROJECTION_SCHEMA = "EVIDENCEOS_PROJECT_DELTA_LEDGER_PROJECTION_V2"
PROJECT_DELTA_TELEMETRY_SCHEMA = "EVIDENCEOS_PROJECT_DELTA_TELEMETRY_V2"
PROJECT_DELTA_LANE_MANIFEST_SCHEMA = "EVIDENCEOS_PROJECT_DELTA_LANE_MANIFEST_V1"
REFRESH_DELTA_AUTHORITY = "PROJECT_REFRESH_BUTTON_ONLY"
REFRESH_DELTA_OVERLAY_READY = "REFRESH_DELTA_COMPLETE_OVERLAY_READY"
REFRESH_DELTA_OVERLAY_BLOCKED = "REFRESH_DELTA_INCOMPLETE_OVERLAY_BLOCKED"
REFRESH_DELTA_TYPES = frozenset(
    {
        "GIT_COMMIT_REFRESH_DELTA",
        "GIT_WORKTREE_REFRESH_DELTA",
        "HASH_SNAPSHOT_REFRESH_DELTA",
    }
)
LEDGER_DATABASE_NAME = "delta_ledger.db"
LEDGER_PROJECTION_NAME = "DELTA_LEDGER.json"
CODE_DELTA_LANE_IDS = frozenset({"github_code", "local_code"})
REFRESH_CONSUMER_APPLICABILITY = (
    ("universal_refresh", "PROJECT_CHANGE_TRUTH_AUTHORITY"),
    ("3d_telemetry", "REFRESH_DELTA_OVERLAY_ONLY"),
)


class ProjectDeltaLedgerError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def project_delta_root(brain_root: str | Path, *, create: bool = True) -> Path:
    root = Path(brain_root).expanduser().resolve()
    project = root / "project"
    if not project.is_dir():
        raise ProjectDeltaLedgerError("PROJECT_DELTA_PROJECT_ROOT_MISSING")
    delta_root = project / "deltas"
    if create:
        delta_root.mkdir(parents=True, exist_ok=True)
    return delta_root


def _ensure_column(connection: sqlite3.Connection, table: str, declaration: str) -> None:
    column = declaration.split()[0]
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def _connect(delta_root: Path) -> sqlite3.Connection:
    delta_root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(delta_root / LEDGER_DATABASE_NAME, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS project_delta (
            sequence_number INTEGER PRIMARY KEY,
            delta_id TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL UNIQUE,
            brain_id TEXT NOT NULL,
            brain_name TEXT NOT NULL,
            refresh_status TEXT NOT NULL,
            classification TEXT NOT NULL,
            before_snapshot_hash TEXT NOT NULL,
            after_snapshot_hash TEXT NOT NULL,
            refresh_receipt_path TEXT NOT NULL,
            refresh_receipt_sha256 TEXT NOT NULL,
            affected_lanes_json TEXT NOT NULL,
            affected_sectors_json TEXT NOT NULL,
            model_applicability_json TEXT NOT NULL,
            telemetry_json TEXT NOT NULL,
            source_change_count INTEGER NOT NULL,
            file_change_count INTEGER NOT NULL,
            manifest_relative_path TEXT NOT NULL UNIQUE,
            manifest_sha256 TEXT NOT NULL,
            prior_delta_id TEXT,
            prior_manifest_sha256 TEXT,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_delta_source (
            delta_id TEXT NOT NULL REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            source_id TEXT NOT NULL,
            lane_id TEXT NOT NULL,
            classification TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY (delta_id, source_id, lane_id)
        );
        CREATE TABLE IF NOT EXISTS project_delta_file (
            delta_id TEXT NOT NULL REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            relative_path TEXT NOT NULL,
            change_kind TEXT NOT NULL,
            previous_sha256 TEXT,
            current_sha256 TEXT,
            details_json TEXT NOT NULL,
            PRIMARY KEY (delta_id, relative_path)
        );
        CREATE TABLE IF NOT EXISTS project_delta_event (
            delta_id TEXT NOT NULL REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            event_sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_relative_path TEXT NOT NULL UNIQUE,
            event_sha256 TEXT NOT NULL,
            prior_event_sha256 TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (delta_id, event_sequence)
        );
        CREATE TABLE IF NOT EXISTS project_delta_git_evidence (
            delta_id TEXT PRIMARY KEY REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            delta_type TEXT NOT NULL,
            repository_root TEXT,
            branch_name TEXT,
            head_commit_sha TEXT,
            parent_commit_sha TEXT,
            baseline_commit_sha TEXT,
            worktree_state TEXT NOT NULL,
            command_receipts_json TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_delta_node (
            delta_id TEXT NOT NULL REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            object_id TEXT NOT NULL,
            object_kind TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            previous_path TEXT,
            change_kind TEXT NOT NULL,
            truth_state TEXT NOT NULL,
            review_required INTEGER NOT NULL CHECK(review_required IN (0,1)),
            previous_sha256 TEXT,
            current_sha256 TEXT,
            impact_scope TEXT NOT NULL,
            validation_ids_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            PRIMARY KEY (delta_id, object_id)
        );
        CREATE TABLE IF NOT EXISTS project_delta_edge (
            delta_id TEXT NOT NULL REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            object_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            from_object_id TEXT NOT NULL,
            to_object_id TEXT NOT NULL,
            change_kind TEXT NOT NULL,
            truth_state TEXT NOT NULL,
            review_required INTEGER NOT NULL CHECK(review_required IN (0,1)),
            previous_state_json TEXT NOT NULL,
            current_state_json TEXT NOT NULL,
            impact_scope TEXT NOT NULL,
            validation_ids_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            PRIMARY KEY (delta_id, object_id)
        );
        CREATE TABLE IF NOT EXISTS project_delta_validation (
            delta_id TEXT NOT NULL REFERENCES project_delta(delta_id) ON DELETE RESTRICT,
            validation_id TEXT NOT NULL,
            validation_type TEXT NOT NULL,
            command_text TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            exit_code INTEGER,
            status TEXT NOT NULL,
            affected_files_json TEXT NOT NULL,
            stdout_receipt TEXT NOT NULL,
            stderr_receipt TEXT NOT NULL,
            receipt_path TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY (delta_id, validation_id)
        );
        """
    )
    _ensure_column(
        connection,
        "project_delta",
        "delta_authority TEXT NOT NULL DEFAULT 'LEGACY_REFRESH_LEDGER_UNVERIFIED'",
    )
    _ensure_column(
        connection,
        "project_delta",
        "delta_type TEXT NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED'",
    )
    _ensure_column(connection, "project_delta", "parent_snapshot_id TEXT")
    _ensure_column(connection, "project_delta", "child_snapshot_id TEXT")
    _ensure_column(connection, "project_delta", "source_state_json TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(
        connection,
        "project_delta",
        "overlay_state TEXT NOT NULL DEFAULT 'REFRESH_DELTA_INCOMPLETE_OVERLAY_BLOCKED'",
    )
    _ensure_column(connection, "project_delta", "completeness_json TEXT NOT NULL DEFAULT '{}'")
    connection.commit()
    return connection


def _manifest_path(delta_root: Path, sequence_number: int) -> Path:
    return delta_root / f"delta_{sequence_number:06d}" / "DELTA_MANIFEST.json"


def _row_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence_number": int(row["sequence_number"]),
        "delta_id": str(row["delta_id"]),
        "display_name": str(row["display_name"]),
        "candidate_id": str(row["candidate_id"]),
        "refresh_status": str(row["refresh_status"]),
        "classification": str(row["classification"]),
        "delta_authority": str(row["delta_authority"]),
        "delta_type": str(row["delta_type"]),
        "before_snapshot_hash": str(row["before_snapshot_hash"]),
        "after_snapshot_hash": str(row["after_snapshot_hash"]),
        "parent_snapshot_id": row["parent_snapshot_id"],
        "child_snapshot_id": row["child_snapshot_id"],
        "source_state": json.loads(str(row["source_state_json"])),
        "overlay_state": str(row["overlay_state"]),
        "completeness": json.loads(str(row["completeness_json"])),
        "affected_lanes": json.loads(str(row["affected_lanes_json"])),
        "affected_sectors": json.loads(str(row["affected_sectors_json"])),
        "model_applicability": json.loads(str(row["model_applicability_json"])),
        "telemetry": json.loads(str(row["telemetry_json"])),
        "source_change_count": int(row["source_change_count"]),
        "file_change_count": int(row["file_change_count"]),
        "manifest_relative_path": str(row["manifest_relative_path"]),
        "manifest_sha256": str(row["manifest_sha256"]),
        "prior_delta_id": row["prior_delta_id"],
        "prior_manifest_sha256": row["prior_manifest_sha256"],
        "recorded_at": str(row["recorded_at"]),
    }


def _verified_summary(delta_root: Path, row: sqlite3.Row) -> dict[str, Any]:
    summary = _row_to_summary(row)
    manifest = delta_root / summary["manifest_relative_path"]
    if not manifest.is_file():
        raise ProjectDeltaLedgerError(f"PROJECT_DELTA_MANIFEST_MISSING:{summary['delta_id']}")
    if _sha256_file(manifest) != summary["manifest_sha256"]:
        raise ProjectDeltaLedgerError(f"PROJECT_DELTA_MANIFEST_HASH_MISMATCH:{summary['delta_id']}")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    _verify_dual_lane_manifests(delta_root, manifest_payload, summary["delta_id"])
    return summary


def _projection_payload(connection: sqlite3.Connection, delta_root: Path) -> dict[str, Any]:
    rows = connection.execute("SELECT * FROM project_delta ORDER BY sequence_number").fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        entry = _verified_summary(delta_root, row)
        event = connection.execute(
            "SELECT event_sequence,event_type,event_relative_path,event_sha256,recorded_at "
            "FROM project_delta_event WHERE delta_id=? ORDER BY event_sequence DESC LIMIT 1",
            (entry["delta_id"],),
        ).fetchone()
        entry["event_count"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM project_delta_event WHERE delta_id=?",
                (entry["delta_id"],),
            ).fetchone()[0]
        )
        entry["latest_event"] = dict(event) if event else None
        entries.append(entry)
    return {
        "schema": PROJECT_DELTA_PROJECTION_SCHEMA,
        "ledger_database": LEDGER_DATABASE_NAME,
        "sequence_policy": "MONOTONIC_NO_RENUMBER",
        "payload_policy": "METADATA_AND_HASH_POINTERS_ONLY_NO_SOURCE_BYTE_DUPLICATION",
        "entry_count": len(entries),
        "entries": entries,
        "projected_at": _utc_now(),
    }


def _write_projection(connection: sqlite3.Connection, delta_root: Path) -> Path:
    target = delta_root / LEDGER_PROJECTION_NAME
    _write_json_atomic(target, _projection_payload(connection, delta_root))
    return target


def _changed_sectors(files: Sequence[Mapping[str, Any]]) -> list[str]:
    sectors: set[str] = set()
    for item in files:
        parts = Path(str(item.get("path") or "").replace("\\", "/")).parts
        if "sectors" in parts:
            index = parts.index("sectors")
            if index + 1 < len(parts):
                sectors.add(parts[index + 1])
    return sorted(sectors)


def _is_code_delta_file(item: Mapping[str, Any]) -> bool:
    parts = tuple(part.casefold() for part in Path(str(item.get("path") or "").replace("\\", "/")).parts)
    if "sectors" in parts:
        index = parts.index("sectors")
        if index + 1 < len(parts) and parts[index + 1] in CODE_DELTA_LANE_IDS:
            return True
    return "code_topology" in parts


def _partition_delta_records(
    sources: Sequence[Mapping[str, Any]],
    files: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    partitions: dict[str, dict[str, list[dict[str, Any]]]] = {
        "code": {"source_changes": [], "file_changes": []},
        "non_code": {"source_changes": [], "file_changes": []},
    }
    for item in sources:
        partition = "code" if str(item.get("lane_id") or "").casefold() in CODE_DELTA_LANE_IDS else "non_code"
        partitions[partition]["source_changes"].append(dict(item))
    for item in files:
        partition = "code" if _is_code_delta_file(item) else "non_code"
        partitions[partition]["file_changes"].append(dict(item))
    return partitions


def _verify_dual_lane_manifests(delta_root: Path, manifest: Mapping[str, Any], delta_id: str) -> None:
    dual_lane = manifest.get("dual_lane_delta")
    if not isinstance(dual_lane, Mapping):
        return
    manifests = dual_lane.get("manifests")
    if not isinstance(manifests, Mapping):
        raise ProjectDeltaLedgerError(f"PROJECT_DELTA_DUAL_LANE_MANIFESTS_INVALID:{delta_id}")
    if set(manifests) != {"code", "non_code"}:
        raise ProjectDeltaLedgerError(f"PROJECT_DELTA_DUAL_LANE_PARTITIONS_INVALID:{delta_id}")
    for partition_id in ("code", "non_code"):
        reference = manifests.get(partition_id)
        if not isinstance(reference, Mapping):
            raise ProjectDeltaLedgerError(f"PROJECT_DELTA_LANE_REFERENCE_INVALID:{delta_id}:{partition_id}")
        relative_path = str(reference.get("manifest_relative_path") or "")
        expected_hash = str(reference.get("manifest_sha256") or "")
        lane_manifest = delta_root / relative_path
        if not lane_manifest.is_file():
            raise ProjectDeltaLedgerError(f"PROJECT_DELTA_LANE_MANIFEST_MISSING:{delta_id}:{partition_id}")
        if len(expected_hash) != 64 or _sha256_file(lane_manifest) != expected_hash:
            raise ProjectDeltaLedgerError(f"PROJECT_DELTA_LANE_MANIFEST_HASH_MISMATCH:{delta_id}:{partition_id}")


def record_refresh_delta(
    brain_root: str | Path,
    *,
    brain_id: str,
    brain_name: str,
    candidate_id: str,
    refresh_status: str,
    classification: str,
    before_snapshot_hash: str,
    after_snapshot_hash: str,
    refresh_receipt_path: str,
    refresh_receipt_sha256: str,
    source_classifications: Sequence[Mapping[str, Any]],
    changed_files: Sequence[Mapping[str, Any]],
    refresh_recorded_at: str,
    delta_type: str = "",
    parent_snapshot_id: str = "",
    child_snapshot_id: str = "",
    source_state: Mapping[str, Any] | None = None,
    node_changes: Sequence[Mapping[str, Any]] = (),
    edge_changes: Sequence[Mapping[str, Any]] = (),
    validation_results: Sequence[Mapping[str, Any]] = (),
    relationship_comparison_complete: bool = False,
) -> dict[str, Any]:
    """Append one immutable, receipt-bound Refresh Delta eligible for telemetry.

    A no-op Refresh is deliberately excluded: it owns an operation receipt but
    never consumes a numbered Project Delta or produces overlay color truth.
    """

    if not candidate_id.strip():
        raise ProjectDeltaLedgerError("PROJECT_DELTA_CANDIDATE_ID_REQUIRED")
    if refresh_status == "NO_CHANGE":
        raise ProjectDeltaLedgerError("PROJECT_DELTA_NO_CHANGE_RECEIPT_ONLY")
    if refresh_status not in {"AWAITING_FUSE", "FAILED", "REJECTED"}:
        raise ProjectDeltaLedgerError("PROJECT_DELTA_SUCCESSFUL_REFRESH_REQUIRED")
    if delta_type not in REFRESH_DELTA_TYPES:
        raise ProjectDeltaLedgerError("PROJECT_DELTA_TYPE_REQUIRED")
    if len(before_snapshot_hash) != 64 or len(after_snapshot_hash) != 64:
        raise ProjectDeltaLedgerError("PROJECT_DELTA_SNAPSHOT_HASH_REQUIRED")
    if len(refresh_receipt_sha256) != 64:
        raise ProjectDeltaLedgerError("PROJECT_DELTA_REFRESH_RECEIPT_HASH_REQUIRED")

    sources = sorted(
        (_json_safe(dict(item)) for item in source_classifications),
        key=lambda item: (str(item.get("lane_id") or ""), str(item.get("source_id") or "")),
    )
    files = sorted(
        (_json_safe(dict(item)) for item in changed_files),
        key=lambda item: str(item.get("path") or ""),
    )
    affected_lanes = sorted({str(item.get("lane_id") or "") for item in sources if item.get("lane_id")})
    affected_sectors = _changed_sectors(files)
    refresh_consumer_applicability = [
        {
            "consumer": consumer,
            "eligible": True,
            "consumption_contract": consumption_contract,
            "provider_specific": False,
        }
        for consumer, consumption_contract in REFRESH_CONSUMER_APPLICABILITY
    ]
    source_state_payload = _json_safe(dict(source_state or {}))
    nodes = sorted(
        (_json_safe(dict(item)) for item in node_changes),
        key=lambda item: (str(item.get("relative_path") or ""), str(item.get("object_id") or "")),
    )
    edges = sorted(
        (_json_safe(dict(item)) for item in edge_changes),
        key=lambda item: (str(item.get("relation_type") or ""), str(item.get("object_id") or "")),
    )
    validations = sorted(
        (_json_safe(dict(item)) for item in validation_results),
        key=lambda item: str(item.get("validation_id") or ""),
    )
    required_completeness = {
        "refresh_button_authority": True,
        "parent_snapshot": bool(parent_snapshot_id and before_snapshot_hash),
        "child_snapshot": bool(child_snapshot_id and after_snapshot_hash),
        "changed_object_set": bool(nodes or edges),
        "manifest_hash_binding": True,
        "refresh_receipt_binding": bool(refresh_receipt_path and refresh_receipt_sha256),
        "relationship_comparison": bool(relationship_comparison_complete),
        "validation_or_review_state": bool(validations)
        and all(
            str(item.get("truth_state") or "") in {"GREEN", "RED", "NEUTRAL"}
            and isinstance(item.get("review_required"), bool)
            for item in [*nodes, *edges]
        ),
    }
    overlay_state = (
        REFRESH_DELTA_OVERLAY_READY
        if all(required_completeness.values())
        else REFRESH_DELTA_OVERLAY_BLOCKED
    )

    delta_root = project_delta_root(brain_root)
    connection = _connect(delta_root)
    created_directory: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM project_delta WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if existing:
            summary = _verified_summary(delta_root, existing)
            connection.commit()
            return {
                "status": "EXISTING",
                **summary,
                "ledger_database": str(delta_root / LEDGER_DATABASE_NAME),
                "ledger_projection": str(delta_root / LEDGER_PROJECTION_NAME),
            }

        prior = connection.execute(
            "SELECT delta_id,manifest_sha256,sequence_number FROM project_delta ORDER BY sequence_number DESC LIMIT 1"
        ).fetchone()
        sequence_number = int(prior["sequence_number"]) + 1 if prior else 1
        delta_id = f"delta_{sequence_number:06d}"
        display_name = f"Delta {sequence_number}"
        manifest_path = _manifest_path(delta_root, sequence_number)
        created_directory = manifest_path.parent
        created_directory.mkdir(parents=False, exist_ok=False)
        manifest_relative_path = manifest_path.relative_to(delta_root).as_posix()
        partitions = _partition_delta_records(sources, files)
        dual_lane_manifests: dict[str, dict[str, Any]] = {}
        for partition_id in ("code", "non_code"):
            partition = partitions[partition_id]
            lane_manifest_path = created_directory / partition_id / "DELTA_LANE_MANIFEST.json"
            lane_manifest_relative_path = lane_manifest_path.relative_to(delta_root).as_posix()
            lane_ids = sorted(
                {
                    str(item.get("lane_id") or "")
                    for item in partition["source_changes"]
                    if item.get("lane_id")
                }
            )
            telemetry_overlay_input = partition_id == "code"
            lane_manifest = {
                "schema": PROJECT_DELTA_LANE_MANIFEST_SCHEMA,
                "delta_id": delta_id,
                "display_name": display_name,
                "sequence_number": sequence_number,
                "partition": {
                    "partition_id": partition_id,
                    "display_name": "Code" if telemetry_overlay_input else "Non-code",
                    "lane_ids": lane_ids,
                    "classification_policy": (
                        "GITHUB_CODE_LOCAL_CODE_AND_CODE_TOPOLOGY"
                        if telemetry_overlay_input
                        else "ALL_OTHER_PROJECT_LANES_AND_DERIVED_ARTIFACTS"
                    ),
                },
                "brain_identity": {"brain_id": brain_id, "brain_name": brain_name},
                "refresh": {
                    "candidate_id": candidate_id,
                    "status": refresh_status,
                    "classification": classification,
                    "receipt_path": refresh_receipt_path,
                    "receipt_sha256": refresh_receipt_sha256,
                    "recorded_at": refresh_recorded_at,
                    "delta_authority": REFRESH_DELTA_AUTHORITY,
                    "delta_type": delta_type,
                    "parent_snapshot_id": parent_snapshot_id,
                    "child_snapshot_id": child_snapshot_id,
                },
                "source_change_count": len(partition["source_changes"]),
                "file_change_count": len(partition["file_changes"]),
                "source_changes": partition["source_changes"],
                "file_changes": partition["file_changes"],
                "storage_policy": "METADATA_AND_HASH_POINTERS_ONLY_NO_SOURCE_BYTE_DUPLICATION",
                "stored_source_payload_files": 0,
                "future_3d_telemetry": {
                    "eligible": telemetry_overlay_input,
                    "role": "CODE_DELTA_OVERLAY_INPUT" if telemetry_overlay_input else "NON_CODE_CONTEXT_ONLY",
                    "current_sqlite_builder_surface": "STORED_NOT_RENDERED",
                },
                "chain": {
                    "prior_delta_id": str(prior["delta_id"]) if prior else None,
                    "prior_manifest_sha256": str(prior["manifest_sha256"]) if prior else None,
                },
            }
            _write_json_atomic(lane_manifest_path, lane_manifest)
            dual_lane_manifests[partition_id] = {
                "partition_id": partition_id,
                "manifest_relative_path": lane_manifest_relative_path,
                "manifest_sha256": _sha256_file(lane_manifest_path),
                "source_change_count": len(partition["source_changes"]),
                "file_change_count": len(partition["file_changes"]),
                "telemetry_overlay_input": telemetry_overlay_input,
            }
        telemetry = {
            "schema": PROJECT_DELTA_TELEMETRY_SCHEMA,
            "overlay_authority": REFRESH_DELTA_AUTHORITY,
            "future_3d_telemetry_eligible": overlay_state == REFRESH_DELTA_OVERLAY_READY,
            "surface_status": overlay_state,
            "current_sqlite_builder_surface": "REFRESH_TRUTH_STORED",
            "ordered_delta_sequence": sequence_number,
            "telemetry_channel": "project_delta_ledger",
            "code_overlay_manifest_relative_path": dual_lane_manifests["code"]["manifest_relative_path"],
            "code_overlay_manifest_sha256": dual_lane_manifests["code"]["manifest_sha256"],
        }
        source_counts: dict[str, int] = {}
        for item in sources:
            value = str(item.get("classification") or "UNKNOWN")
            source_counts[value] = source_counts.get(value, 0) + 1
        file_counts: dict[str, int] = {}
        for item in files:
            value = str(item.get("change_kind") or "UNKNOWN")
            file_counts[value] = file_counts.get(value, 0) + 1
        manifest = {
            "schema": PROJECT_DELTA_MANIFEST_SCHEMA,
            "delta_id": delta_id,
            "display_name": display_name,
            "sequence_number": sequence_number,
            "sequence_policy": "MONOTONIC_NO_RENUMBER",
            "brain_identity": {"brain_id": brain_id, "brain_name": brain_name},
            "refresh": {
                "candidate_id": candidate_id,
                "status": refresh_status,
                "classification": classification,
                "receipt_path": refresh_receipt_path,
                "receipt_sha256": refresh_receipt_sha256,
                "recorded_at": refresh_recorded_at,
                "delta_authority": REFRESH_DELTA_AUTHORITY,
                "delta_type": delta_type,
                "parent_snapshot_id": parent_snapshot_id,
                "child_snapshot_id": child_snapshot_id,
            },
            "project_hashes": {
                "before_snapshot_sha256": before_snapshot_hash,
                "after_snapshot_sha256": after_snapshot_hash,
            },
            "affected_lanes": affected_lanes,
            "affected_sectors": affected_sectors,
            "source_change_counts": source_counts,
            "file_change_counts": file_counts,
            "source_changes": sources,
            "file_changes": files,
            "source_state": source_state_payload,
            "node_changes": nodes,
            "edge_changes": edges,
            "validation_results": validations,
            "overlay_contract": {
                "state": overlay_state,
                "completeness": required_completeness,
                "truth_colors": ["GREEN", "RED", "NEUTRAL"],
                "review_indicator": "REVIEW_REQUIRED_METADATA_NOT_A_FOURTH_TRUTH_COLOR",
                "edge_truth_independent": True,
                "planning_delta_color_authority": False,
            },
            "dual_lane_delta": {
                "schema": PROJECT_DELTA_LANE_MANIFEST_SCHEMA,
                "partition_policy": "CODE_AND_NON_CODE_ALWAYS_MATERIALIZED",
                "project_folder_residency": "project/deltas/<ordered_delta_id>",
                "manifests": dual_lane_manifests,
            },
            "refresh_consumer_applicability": refresh_consumer_applicability,
            "provider_identity_changes_refresh_truth": False,
            "telemetry": telemetry,
            "artifact_manifest": {
                "storage_policy": "METADATA_AND_HASH_POINTERS_ONLY_NO_SOURCE_BYTE_DUPLICATION",
                "manifest_relative_path": manifest_relative_path,
                "dual_lane_manifest_count": len(dual_lane_manifests),
                "stored_source_payload_files": 0,
            },
            "chain": {
                "prior_delta_id": str(prior["delta_id"]) if prior else None,
                "prior_manifest_sha256": str(prior["manifest_sha256"]) if prior else None,
            },
        }
        _write_json_atomic(manifest_path, manifest)
        manifest_sha256 = _sha256_file(manifest_path)
        connection.execute(
            "INSERT INTO project_delta ("
            "sequence_number,delta_id,display_name,candidate_id,brain_id,brain_name,refresh_status,classification,"
            "delta_authority,delta_type,parent_snapshot_id,child_snapshot_id,source_state_json,overlay_state,completeness_json,"
            "before_snapshot_hash,after_snapshot_hash,refresh_receipt_path,refresh_receipt_sha256,affected_lanes_json,"
            "affected_sectors_json,model_applicability_json,telemetry_json,source_change_count,file_change_count,"
            "manifest_relative_path,manifest_sha256,prior_delta_id,prior_manifest_sha256,recorded_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sequence_number,
                delta_id,
                display_name,
                candidate_id,
                brain_id,
                brain_name,
                refresh_status,
                classification,
                REFRESH_DELTA_AUTHORITY,
                delta_type,
                parent_snapshot_id,
                child_snapshot_id,
                json.dumps(source_state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                overlay_state,
                json.dumps(required_completeness, sort_keys=True, separators=(",", ":")),
                before_snapshot_hash,
                after_snapshot_hash,
                refresh_receipt_path,
                refresh_receipt_sha256,
                json.dumps(affected_lanes, separators=(",", ":")),
                json.dumps(affected_sectors, separators=(",", ":")),
                json.dumps(refresh_consumer_applicability, separators=(",", ":")),
                json.dumps(telemetry, separators=(",", ":")),
                len(sources),
                len(files),
                manifest_relative_path,
                manifest_sha256,
                str(prior["delta_id"]) if prior else None,
                str(prior["manifest_sha256"]) if prior else None,
                refresh_recorded_at,
            ),
        )
        git_payload = source_state_payload.get("git") if isinstance(source_state_payload.get("git"), Mapping) else {}
        connection.execute(
            "INSERT INTO project_delta_git_evidence ("
            "delta_id,delta_type,repository_root,branch_name,head_commit_sha,parent_commit_sha,baseline_commit_sha,"
            "worktree_state,command_receipts_json,details_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                delta_id,
                delta_type,
                str(git_payload.get("repository_root") or ""),
                str(git_payload.get("branch") or ""),
                str(git_payload.get("head_commit_sha") or ""),
                str(git_payload.get("parent_commit_sha") or ""),
                str(git_payload.get("baseline_commit_sha") or ""),
                str(git_payload.get("worktree_state") or source_state_payload.get("source_state") or "UNKNOWN"),
                json.dumps(git_payload.get("command_receipts") or [], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                json.dumps(source_state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            ),
        )
        for item in sources:
            connection.execute(
                "INSERT INTO project_delta_source (delta_id,source_id,lane_id,classification,details_json) VALUES (?,?,?,?,?)",
                (
                    delta_id,
                    str(item.get("source_id") or "unknown"),
                    str(item.get("lane_id") or "unknown"),
                    str(item.get("classification") or "UNKNOWN"),
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
        for item in files:
            connection.execute(
                "INSERT INTO project_delta_file (delta_id,relative_path,change_kind,previous_sha256,current_sha256,details_json) "
                "VALUES (?,?,?,?,?,?)",
                (
                    delta_id,
                    str(item.get("path") or "unknown"),
                    str(item.get("change_kind") or "UNKNOWN"),
                    item.get("previous_sha256"),
                    item.get("current_sha256"),
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
        for item in nodes:
            connection.execute(
                "INSERT INTO project_delta_node ("
                "delta_id,object_id,object_kind,relative_path,previous_path,change_kind,truth_state,review_required,"
                "previous_sha256,current_sha256,impact_scope,validation_ids_json,evidence_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delta_id,
                    str(item.get("object_id") or ""),
                    str(item.get("object_kind") or "file"),
                    str(item.get("relative_path") or ""),
                    item.get("previous_path"),
                    str(item.get("change_kind") or "UNKNOWN"),
                    str(item.get("truth_state") or "NEUTRAL"),
                    1 if item.get("review_required") is True else 0,
                    item.get("previous_sha256"),
                    item.get("current_sha256"),
                    str(item.get("impact_scope") or "DIRECT"),
                    json.dumps(item.get("validation_ids") or [], sort_keys=True, separators=(",", ":")),
                    json.dumps(item.get("evidence") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
        for item in edges:
            connection.execute(
                "INSERT INTO project_delta_edge ("
                "delta_id,object_id,relation_type,from_object_id,to_object_id,change_kind,truth_state,review_required,"
                "previous_state_json,current_state_json,impact_scope,validation_ids_json,evidence_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delta_id,
                    str(item.get("object_id") or ""),
                    str(item.get("relation_type") or "UNKNOWN"),
                    str(item.get("from_object_id") or ""),
                    str(item.get("to_object_id") or ""),
                    str(item.get("change_kind") or "UNCHANGED"),
                    str(item.get("truth_state") or "NEUTRAL"),
                    1 if item.get("review_required") is True else 0,
                    json.dumps(item.get("previous_state") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(item.get("current_state") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    str(item.get("impact_scope") or "DIRECT"),
                    json.dumps(item.get("validation_ids") or [], sort_keys=True, separators=(",", ":")),
                    json.dumps(item.get("evidence") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
        for item in validations:
            connection.execute(
                "INSERT INTO project_delta_validation ("
                "delta_id,validation_id,validation_type,command_text,started_at,ended_at,exit_code,status,affected_files_json,"
                "stdout_receipt,stderr_receipt,receipt_path,receipt_sha256,details_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delta_id,
                    str(item.get("validation_id") or ""),
                    str(item.get("validation_type") or "UNKNOWN"),
                    str(item.get("command") or ""),
                    str(item.get("started_at") or refresh_recorded_at),
                    str(item.get("ended_at") or refresh_recorded_at),
                    item.get("exit_code"),
                    str(item.get("status") or "REVIEW_REQUIRED"),
                    json.dumps(item.get("affected_files") or [], sort_keys=True, separators=(",", ":")),
                    str(item.get("stdout") or ""),
                    str(item.get("stderr") or ""),
                    str(item.get("receipt_path") or ""),
                    str(item.get("receipt_sha256") or ""),
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
        connection.execute(
            "INSERT INTO project_delta_event (delta_id,event_sequence,event_type,event_relative_path,event_sha256,prior_event_sha256,recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                delta_id,
                1,
                "REFRESH_CAPTURED",
                manifest_relative_path,
                manifest_sha256,
                str(prior["manifest_sha256"]) if prior else "0" * 64,
                refresh_recorded_at,
            ),
        )
        connection.commit()
        projection = _write_projection(connection, delta_root)
        row = connection.execute("SELECT * FROM project_delta WHERE delta_id=?", (delta_id,)).fetchone()
        if row is None:
            raise ProjectDeltaLedgerError("PROJECT_DELTA_COMMIT_NOT_VISIBLE")
        return {
            "status": "RECORDED",
            **_verified_summary(delta_root, row),
            "ledger_database": str(delta_root / LEDGER_DATABASE_NAME),
            "ledger_projection": str(projection),
        }
    except Exception:
        connection.rollback()
        if created_directory and created_directory.is_dir():
            row = connection.execute(
                "SELECT 1 FROM project_delta WHERE manifest_relative_path=?",
                ((created_directory / "DELTA_MANIFEST.json").relative_to(delta_root).as_posix(),),
            ).fetchone()
            if row is None:
                shutil.rmtree(created_directory)
        raise
    finally:
        connection.close()


def append_project_delta_event(
    brain_root: str | Path,
    *,
    candidate_id: str,
    event_type: str,
    actor: str,
    reason: str,
    receipt_path: str = "",
    receipt_sha256: str = "",
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    delta_root = project_delta_root(brain_root, create=False)
    connection = _connect(delta_root)
    created_event: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM project_delta WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ProjectDeltaLedgerError("PROJECT_DELTA_CANDIDATE_NOT_FOUND")
        delta_id = str(row["delta_id"])
        prior = connection.execute(
            "SELECT event_sequence,event_sha256 FROM project_delta_event WHERE delta_id=? ORDER BY event_sequence DESC LIMIT 1",
            (delta_id,),
        ).fetchone()
        event_sequence = int(prior["event_sequence"]) + 1 if prior else 1
        recorded_at = _utc_now()
        event = {
            "schema": PROJECT_DELTA_EVENT_SCHEMA,
            "event_id": f"{delta_id}:event_{event_sequence:06d}",
            "delta_id": delta_id,
            "candidate_id": candidate_id,
            "event_sequence": event_sequence,
            "event_type": str(event_type),
            "actor": str(actor),
            "reason": str(reason),
            "receipt_path": str(receipt_path),
            "receipt_sha256": str(receipt_sha256),
            "details": _json_safe(dict(details or {})),
            "prior_event_sha256": str(prior["event_sha256"]) if prior else str(row["manifest_sha256"]),
            "recorded_at": recorded_at,
        }
        event_dir = delta_root / delta_id / "events"
        event_dir.mkdir(parents=True, exist_ok=True)
        event_path = event_dir / f"event_{event_sequence:06d}_{str(event_type).casefold()}.json"
        if event_path.exists():
            raise ProjectDeltaLedgerError("PROJECT_DELTA_EVENT_ALREADY_EXISTS")
        _write_json_atomic(event_path, event)
        created_event = event_path
        event_sha256 = _sha256_file(event_path)
        relative = event_path.relative_to(delta_root).as_posix()
        connection.execute(
            "INSERT INTO project_delta_event (delta_id,event_sequence,event_type,event_relative_path,event_sha256,prior_event_sha256,recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                delta_id,
                event_sequence,
                str(event_type),
                relative,
                event_sha256,
                event["prior_event_sha256"],
                recorded_at,
            ),
        )
        connection.commit()
        projection = _write_projection(connection, delta_root)
        return {
            **event,
            "event_relative_path": relative,
            "event_sha256": event_sha256,
            "ledger_projection": str(projection),
        }
    except Exception:
        connection.rollback()
        if created_event and created_event.is_file():
            created_event.unlink()
        raise
    finally:
        connection.close()


def list_project_deltas(brain_root: str | Path) -> dict[str, Any]:
    delta_root = project_delta_root(brain_root, create=False)
    if not delta_root.is_dir() or not (delta_root / LEDGER_DATABASE_NAME).is_file():
        return {
            "schema": PROJECT_DELTA_PROJECTION_SCHEMA,
            "status": "NOT_STARTED",
            "entry_count": 0,
            "entries": [],
            "delta_root": str(delta_root),
        }
    connection = _connect(delta_root)
    try:
        projection = _projection_payload(connection, delta_root)
        _write_json_atomic(delta_root / LEDGER_PROJECTION_NAME, projection)
        return {
            **projection,
            "status": "PASS",
            "delta_root": str(delta_root),
            "ledger_database": str(delta_root / LEDGER_DATABASE_NAME),
        }
    finally:
        connection.close()


def get_project_delta(
    brain_root: str | Path,
    *,
    delta_id: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any] | None:
    if not delta_id and not candidate_id:
        raise ProjectDeltaLedgerError("PROJECT_DELTA_ID_OR_CANDIDATE_REQUIRED")
    delta_root = project_delta_root(brain_root, create=False)
    database = delta_root / LEDGER_DATABASE_NAME
    if not database.is_file():
        return None
    connection = _connect(delta_root)
    try:
        row = connection.execute(
            "SELECT * FROM project_delta WHERE delta_id=?" if delta_id else "SELECT * FROM project_delta WHERE candidate_id=?",
            (str(delta_id or candidate_id),),
        ).fetchone()
        if row is None:
            return None
        summary = _verified_summary(delta_root, row)
        summary["events"] = [
            dict(event)
            for event in connection.execute(
                "SELECT event_sequence,event_type,event_relative_path,event_sha256,prior_event_sha256,recorded_at "
                "FROM project_delta_event WHERE delta_id=? ORDER BY event_sequence",
                (summary["delta_id"],),
            ).fetchall()
        ]
        summary["manifest"] = json.loads(
            (delta_root / summary["manifest_relative_path"]).read_text(encoding="utf-8")
        )
        git_row = connection.execute(
            "SELECT * FROM project_delta_git_evidence WHERE delta_id=?",
            (summary["delta_id"],),
        ).fetchone()
        summary["git_evidence"] = dict(git_row) if git_row else None
        summary["node_changes"] = [
            {
                **dict(item),
                "review_required": bool(item["review_required"]),
                "validation_ids": json.loads(str(item["validation_ids_json"])),
                "evidence": json.loads(str(item["evidence_json"])),
            }
            for item in connection.execute(
                "SELECT * FROM project_delta_node WHERE delta_id=? ORDER BY relative_path,object_id",
                (summary["delta_id"],),
            ).fetchall()
        ]
        summary["edge_changes"] = [
            {
                **dict(item),
                "review_required": bool(item["review_required"]),
                "previous_state": json.loads(str(item["previous_state_json"])),
                "current_state": json.loads(str(item["current_state_json"])),
                "validation_ids": json.loads(str(item["validation_ids_json"])),
                "evidence": json.loads(str(item["evidence_json"])),
            }
            for item in connection.execute(
                "SELECT * FROM project_delta_edge WHERE delta_id=? ORDER BY relation_type,object_id",
                (summary["delta_id"],),
            ).fetchall()
        ]
        summary["validation_results"] = [
            {
                **dict(item),
                "affected_files": json.loads(str(item["affected_files_json"])),
                "details": json.loads(str(item["details_json"])),
            }
            for item in connection.execute(
                "SELECT * FROM project_delta_validation WHERE delta_id=? ORDER BY validation_id",
                (summary["delta_id"],),
            ).fetchall()
        ]
        return summary
    finally:
        connection.close()


def sync_project_delta_folder(source_brain_root: str | Path, destination_brain_root: str | Path) -> dict[str, Any]:
    source = project_delta_root(source_brain_root, create=False)
    if not source.is_dir():
        return {"status": "NOT_STARTED", "copied": False}
    destination = project_delta_root(destination_brain_root)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    return {
        "status": "PASS",
        "copied": True,
        "source": str(source),
        "destination": str(destination),
        "entry_count": int(list_project_deltas(destination_brain_root)["entry_count"]),
    }


__all__ = [
    "LEDGER_DATABASE_NAME",
    "REFRESH_CONSUMER_APPLICABILITY",
    "PROJECT_DELTA_EVENT_SCHEMA",
    "PROJECT_DELTA_LANE_MANIFEST_SCHEMA",
    "PROJECT_DELTA_MANIFEST_SCHEMA",
    "PROJECT_DELTA_PROJECTION_SCHEMA",
    "PROJECT_DELTA_TELEMETRY_SCHEMA",
    "REFRESH_DELTA_AUTHORITY",
    "REFRESH_DELTA_OVERLAY_BLOCKED",
    "REFRESH_DELTA_OVERLAY_READY",
    "REFRESH_DELTA_TYPES",
    "ProjectDeltaLedgerError",
    "append_project_delta_event",
    "get_project_delta",
    "list_project_deltas",
    "project_delta_root",
    "record_refresh_delta",
    "sync_project_delta_folder",
]
