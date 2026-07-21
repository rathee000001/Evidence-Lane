from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections import OrderedDict, deque
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from sqlite_brain_builder.brain_versions import (
    compare_current_brain_to_version,
    list_brain_versions,
    load_brain_version_record,
)
from sqlite_brain_builder.refresh_brain import get_refresh_output
from sqlite_brain_builder.runtime.path_policy import (
    brain_output_dir,
    normalize_workspace_dir,
    slugify_name,
)
from sqlite_brain_builder.runtime.project_delta_ledger import (
    REFRESH_DELTA_AUTHORITY,
    REFRESH_DELTA_OVERLAY_READY,
    get_project_delta,
    list_project_deltas,
    project_delta_root,
)


TELEMETRY_SCHEMA = "EVIDENCEOS_3D_BRAIN_TELEMETRY_READ_V1"
TELEMETRY_STATES = (
    "SOURCE_VERIFIED",
    "TEST_VERIFIED",
    "HUMAN_ACCEPTED",
    "CANDIDATE",
    "OPEN",
    "BLOCKED",
    "STALE",
    "SUPERSEDED",
    "UNVERIFIED",
)
TELEMETRY_COMMANDS = (
    "brain.telemetry.contract",
    "brain.telemetry.snapshot",
    "brain.telemetry.deltas",
    "brain.telemetry.graph",
    "brain.telemetry.openTarget",
)
# ``0`` means the complete indexed graph.  The prior 33/500 presentation caps
# were UI-era safety rails and incorrectly turned a real project tree into a
# sparse demo atom.  A very high explicit ceiling remains only as malformed
# request protection; normal native callers never truncate the indexed tree.
DEFAULT_MAX_NODES = 0
MAX_MAX_NODES = 100_000
MAX_DELTAS = 200
MAX_DELTA_CHANGES = 2000

_GREEN_TOKENS = {
    "PASS",
    "PASSED",
    "VERIFIED",
    "TEST_VERIFIED",
    "HUMAN_ACCEPTED",
    "SUCCESS",
    "COMPLETED",
}
_RED_TOKENS = {
    "FAIL",
    "FAILED",
    "BLOCKED",
    "ERROR",
    "CORRUPT",
    "CORRUPTION",
    "HASH_MISMATCH",
    "REJECTED",
}
_KIND_ORDER = {
    "brain": 0,
    "source": 1,
    "folder": 2,
    "file": 3,
    "symbol": 4,
    "route": 5,
    "test": 6,
    "dependency": 7,
    "artifact": 8,
    "package": 9,
    "validation": 10,
    "change": 11,
    "source_snapshot_head": 12,
}


class BrainTelemetryError(RuntimeError):
    pass


def telemetry_contract() -> dict[str, Any]:
    return {
        "schema": TELEMETRY_SCHEMA,
        "read_only": True,
        "creates_sqlite_schema": False,
        "duplicates_topology": False,
        "raw_project_reread_for_graph": False,
        "automatic_fusion": False,
        "delta_sandbox_status": "SUPERSEDED",
        "production_3d_ui_status": "ACTIVE_PUBLIC_V1_REAL_BACKEND",
        "commands": list(TELEMETRY_COMMANDS),
        "visual_states": list(TELEMETRY_STATES),
        "tone_law": {
            "GREEN": "explicit pass or verified evidence only",
            "RED": "explicit fail, blocked, error, corruption, or mismatch evidence only",
            "NEUTRAL": "change, open, candidate, source-verified, stale, superseded, or unverified without pass/fail proof",
        },
        "scope_law": "one selected indexed subtree replaces the returned graph; no stacked duplicate topology",
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    safe = table.replace('"', '""')
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{safe}")')}


def _pick(row: Mapping[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        if name in row.keys() and row[name] is not None:
            return row[name]
    return default


def _tone(value: Any) -> str:
    token = str(value or "").strip().upper()
    if token in _RED_TOKENS or any(part in token for part in ("FAIL", "BLOCK", "ERROR", "CORRUPT", "MISMATCH")):
        return "RED"
    if token in _GREEN_TOKENS or token.endswith("_PASS") or token.startswith("PASS_"):
        return "GREEN"
    return "NEUTRAL"


def _state(value: Any) -> str:
    token = str(value or "").strip().upper()
    tone = _tone(token)
    if tone == "RED":
        return "BLOCKED"
    if tone == "GREEN":
        return "TEST_VERIFIED"
    if token in TELEMETRY_STATES:
        return token
    if token in {"MODIFIED", "ADDED", "REMOVED", "AFFECTED", "CHANGED"}:
        return "CANDIDATE"
    return "UNVERIFIED"


def _merge_tone(current: str, candidate: str) -> str:
    if "RED" in {current, candidate}:
        return "RED"
    if "GREEN" in {current, candidate}:
        return "GREEN"
    return "NEUTRAL"


def _brain_root(workspace_dir: str | Path, brain_name: str) -> tuple[Path, Path]:
    workspace = normalize_workspace_dir(workspace_dir).resolve()
    root = brain_output_dir(workspace, brain_name).resolve()
    if not root.is_dir():
        raise BrainTelemetryError("TELEMETRY_SELECTED_BRAIN_NOT_FOUND")
    return workspace, root


_VERIFIED_VERSION_CACHE_LIMIT = 12
_VERIFIED_VERSION_CACHE_LOCK = threading.RLock()
_VERIFIED_VERSION_CACHE: OrderedDict[str, tuple[dict[str, Any], dict[str, Any]]] = OrderedDict()


def _verified_current_version(workspace: Path, brain_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    versions = list_brain_versions(workspace, brain_name, verify_hashes=False)
    rows = list(versions.get("versions") or [])
    if not rows:
        raise BrainTelemetryError("TELEMETRY_IMMUTABLE_VERSION_REQUIRED")
    latest = dict(rows[0])
    manifest = Path(str(versions.get("manifest_path") or ""))
    try:
        manifest_stat = manifest.stat()
        manifest_signature = (int(manifest_stat.st_size), int(manifest_stat.st_mtime_ns))
    except OSError:
        manifest_signature = (None, None)
    cache_key = _sha256_text(
        json.dumps(
            {
                "workspace": os.path.normcase(str(workspace)),
                "brain_name": brain_name,
                "version_id": str(latest.get("version_id") or ""),
                "snapshot_hash": str(latest.get("snapshot_hash") or ""),
                "manifest_sha256": str(versions.get("manifest_sha256") or ""),
                "manifest_stat": manifest_signature,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    with _VERIFIED_VERSION_CACHE_LOCK:
        cached = _VERIFIED_VERSION_CACHE.get(cache_key)
        if cached is not None:
            _VERIFIED_VERSION_CACHE.move_to_end(cache_key)
            return copy.deepcopy(cached[0]), copy.deepcopy(cached[1])

    load_brain_version_record(workspace, brain_name, str(latest["version_id"]), verify_hashes=True)
    comparison = compare_current_brain_to_version(workspace, brain_name, str(latest["version_id"]))
    if comparison.get("status") != "MATCH":
        raise BrainTelemetryError("TELEMETRY_CURRENT_BRAIN_DIFFERS_FROM_IMMUTABLE_VERSION")
    latest["integrity_status"] = "VERIFIED"
    latest["current_binding_status"] = "EXACT_IMMUTABLE_VERSION_MATCH"
    with _VERIFIED_VERSION_CACHE_LOCK:
        _VERIFIED_VERSION_CACHE[cache_key] = (copy.deepcopy(latest), copy.deepcopy(versions))
        _VERIFIED_VERSION_CACHE.move_to_end(cache_key)
        while len(_VERIFIED_VERSION_CACHE) > _VERIFIED_VERSION_CACHE_LIMIT:
            _VERIFIED_VERSION_CACHE.popitem(last=False)
    return latest, versions


def _sector_databases(root: Path) -> list[tuple[str, Path]]:
    router = root / "project" / "project_router.sqlite"
    if not router.is_file():
        raise BrainTelemetryError("TELEMETRY_PROJECT_ROUTER_MISSING")
    with closing(_connect_read_only(router)) as connection:
        if "sector_registry" not in _tables(connection):
            raise BrainTelemetryError("TELEMETRY_SECTOR_REGISTRY_MISSING")
        columns = _columns(connection, "sector_registry")
        if not {"sector_id", "sqlite_path"} <= columns:
            raise BrainTelemetryError("TELEMETRY_SECTOR_REGISTRY_CONTRACT_INVALID")
        rows = list(connection.execute("SELECT sector_id,sqlite_path FROM sector_registry ORDER BY sector_id"))
    databases: list[tuple[str, Path]] = []
    for sector_id, stored in rows:
        raw = Path(str(stored))
        database = (raw if raw.is_absolute() else root / raw).resolve()
        try:
            database.relative_to(root)
        except ValueError as exc:
            raise BrainTelemetryError("TELEMETRY_SECTOR_PATH_OUTSIDE_BRAIN") from exc
        if not database.is_file():
            raise BrainTelemetryError(f"TELEMETRY_SECTOR_DATABASE_MISSING:{sector_id}")
        databases.append((str(sector_id), database))
    return databases


def _semantic_database(root: Path) -> Path:
    return root / "brain_versions" / "semantic_brain_diff.sqlite"


def _semantic_runs(root: Path) -> list[dict[str, Any]]:
    database = _semantic_database(root)
    if not database.is_file():
        return []
    with closing(_connect_read_only(database)) as connection:
        if "brain_semantic_diff_run" not in _tables(connection):
            return []
        return [
            dict(row)
            for row in connection.execute(
                "SELECT diff_run_id,previous_good_snapshot_id,current_good_snapshot_id,"
                "previous_brain_hash,current_brain_hash,created_at,actor,reason,test_status,"
                "build_status,diff_status FROM brain_semantic_diff_run ORDER BY created_at,diff_run_id"
            )
        ]


def _semantic_good_snapshots(root: Path) -> dict[str, dict[str, Any]]:
    database = _semantic_database(root)
    if not database.is_file():
        return {}
    with closing(_connect_read_only(database)) as connection:
        if "brain_good_snapshot" not in _tables(connection):
            return {}
        return {
            str(row["snapshot_hash"]): dict(row)
            for row in connection.execute(
                "SELECT snapshot_id,version_id,snapshot_hash,brain_package_path,brain_package_hash,"
                "test_status,build_status,created_at,reason FROM brain_good_snapshot ORDER BY created_at"
            )
        }


def _version_pair_changes(
    workspace: Path,
    brain_name: str,
    previous_version_id: str,
    current_version_id: str,
) -> list[dict[str, Any]]:
    previous = load_brain_version_record(workspace, brain_name, previous_version_id, verify_hashes=True)
    current = load_brain_version_record(workspace, brain_name, current_version_id, verify_hashes=True)
    before = {str(item["path"]): item for item in previous.get("snapshot_files") or []}
    after = {str(item["path"]): item for item in current.get("snapshot_files") or []}
    changes = []
    for path in sorted(set(before) | set(after), key=str.casefold):
        prior = before.get(path)
        latter = after.get(path)
        if prior and latter and prior.get("sha256") == latter.get("sha256"):
            continue
        changes.append(
            {
                "path": path,
                "change_kind": "ADDED" if not prior else "REMOVED" if not latter else "MODIFIED",
                "previous_hash": prior.get("sha256") if prior else None,
                "current_hash": latter.get("sha256") if latter else None,
                "evidence_ref": str(latter.get("object_path") if latter else prior.get("object_path")),
            }
        )
    return changes


def _delta_state(run: Mapping[str, Any] | None) -> tuple[str, str]:
    if not run:
        return "SOURCE_VERIFIED", "NEUTRAL"
    statuses = [run.get("test_status"), run.get("build_status"), run.get("diff_status")]
    tones = [_tone(value) for value in statuses]
    if "RED" in tones:
        return "BLOCKED", "RED"
    if _tone(run.get("test_status")) == "GREEN" and _tone(run.get("build_status")) == "GREEN":
        return "TEST_VERIFIED", "GREEN"
    if _tone(run.get("diff_status")) == "GREEN":
        return "SOURCE_VERIFIED", "NEUTRAL"
    return "UNVERIFIED", "NEUTRAL"


_TELEMETRY_DELTA_CACHE_LIMIT = 12
_TELEMETRY_DELTA_CACHE_LOCK = threading.RLock()
_TELEMETRY_DELTA_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _telemetry_delta_persistent_path(workspace: Path, brain_name: str) -> Path:
    return (
        workspace
        / ".evidenceos_runtime"
        / slugify_name(brain_name)
        / "telemetry_delta_ledger_v2.json"
    )


def _list_legacy_immutable_deltas_not_overlay_authority(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    workspace, root = _brain_root(workspace_dir, brain_name)
    version_result = list_brain_versions(workspace, brain_name, verify_hashes=False)
    semantic_database = _semantic_database(root)
    try:
        semantic_status = semantic_database.stat()
        semantic_signature = (int(semantic_status.st_size), int(semantic_status.st_mtime_ns))
    except OSError:
        semantic_signature = (None, None)
    cache_key = _sha256_text(
        json.dumps(
            {
                "workspace": os.path.normcase(str(workspace)),
                "brain_name": brain_name,
                "manifest_sha256": str(version_result.get("manifest_sha256") or ""),
                "semantic_stat": semantic_signature,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    with _TELEMETRY_DELTA_CACHE_LOCK:
        cached = _TELEMETRY_DELTA_CACHE.get(cache_key)
        if cached is not None:
            _TELEMETRY_DELTA_CACHE.move_to_end(cache_key)
            result = copy.deepcopy(cached)
            result["delta_cache"] = {"status": "HIT", "scope": "NATIVE_WORKER_SESSION"}
            return result

    persistent_path = _telemetry_delta_persistent_path(workspace, brain_name)
    try:
        persistent = json.loads(persistent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        persistent = None
    if isinstance(persistent, dict) and persistent.get("immutable_delta_key") == cache_key:
        stored = persistent.get("result")
        if isinstance(stored, dict):
            result = copy.deepcopy(stored)
            result["delta_cache"] = {
                "status": "HIT",
                "scope": "DURABLE_NATIVE_WORKER_AND_DISK",
                "tier": "DURABLE_DISK",
                "cache_path": str(persistent_path),
            }
            with _TELEMETRY_DELTA_CACHE_LOCK:
                _TELEMETRY_DELTA_CACHE[cache_key] = copy.deepcopy(result)
                _TELEMETRY_DELTA_CACHE.move_to_end(cache_key)
            return result

    all_newest_first = list(version_result.get("versions") or [])
    newest_first = all_newest_first[: MAX_DELTAS + 1]
    chronological = list(reversed(newest_first))
    runs = _semantic_runs(root)
    run_by_hash_pair = {
        (str(run.get("previous_brain_hash") or ""), str(run.get("current_brain_hash") or "")): run
        for run in runs
    }
    deltas = []
    verified_version_ids: set[str] = set()
    for index in range(1, len(chronological)):
        previous = chronological[index - 1]
        current = chronological[index]
        for version in (previous, current):
            version_id = str(version["version_id"])
            if version_id not in verified_version_ids:
                load_brain_version_record(workspace, brain_name, version_id, verify_hashes=True)
                verified_version_ids.add(version_id)
        before_hash = str(previous.get("snapshot_hash") or "")
        after_hash = str(current.get("snapshot_hash") or "")
        run = run_by_hash_pair.get((before_hash, after_hash))
        delta_id = "delta_" + _sha256_text(
            f"{previous.get('version_id')}|{current.get('version_id')}|{before_hash}|{after_hash}"
        )[:24]
        state, tone = _delta_state(run)
        fallback_changes = None
        if run:
            database = _semantic_database(root)
            with closing(_connect_read_only(database)) as connection:
                changed_count = (
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM brain_changed_file WHERE diff_run_id=?",
                            (run["diff_run_id"],),
                        ).fetchone()[0]
                    )
                    if "brain_changed_file" in _tables(connection)
                    else 0
                )
        else:
            all_fallback_changes = _version_pair_changes(
                workspace,
                brain_name,
                str(previous["version_id"]),
                str(current["version_id"]),
            )
            changed_count = len(all_fallback_changes)
            fallback_changes = all_fallback_changes[:MAX_DELTA_CHANGES]
        deltas.append(
            {
                "delta_id": delta_id,
                "display_name": f"Δ{index}",
                "ordinal": index,
                "before_version_id": previous.get("version_id"),
                "after_version_id": current.get("version_id"),
                "before_snapshot_hash": before_hash,
                "after_snapshot_hash": after_hash,
                "diff_authority": "SEMANTIC_BRAIN_DIFF_SQLITE" if run else "IMMUTABLE_VERSION_RECORDS",
                "diff_run_id": run.get("diff_run_id") if run else None,
                "changed_object_count": changed_count,
                "test_status": run.get("test_status") if run else "UNVERIFIED",
                "build_status": run.get("build_status") if run else "UNVERIFIED",
                "diff_status": run.get("diff_status") if run else "SOURCE_VERIFIED",
                "evidence_state": state,
                "tone": tone,
                "fallback_changes": fallback_changes,
                "fallback_changes_truncated": bool(fallback_changes is not None and changed_count > len(fallback_changes)),
                "automatic_fusion": False,
            }
        )
    result = {
        "schema": TELEMETRY_SCHEMA,
        "status": "PASS",
        "brain_name": brain_name,
        "version_manifest": version_result.get("manifest_path"),
        "version_manifest_sha256": version_result.get("manifest_sha256"),
        "version_count": len(all_newest_first),
        "delta_count": len(deltas),
        "deltas": deltas,
        "truncated": len(all_newest_first) > len(newest_first),
        "max_deltas": MAX_DELTAS,
        "read_only": True,
        "raw_project_files_reread": 0,
        "delta_cache": {"status": "MISS", "scope": "NATIVE_WORKER_SESSION"},
    }
    with _TELEMETRY_DELTA_CACHE_LOCK:
        _TELEMETRY_DELTA_CACHE[cache_key] = copy.deepcopy(result)
        _TELEMETRY_DELTA_CACHE.move_to_end(cache_key)
        while len(_TELEMETRY_DELTA_CACHE) > _TELEMETRY_DELTA_CACHE_LIMIT:
            _TELEMETRY_DELTA_CACHE.popitem(last=False)
    persistent_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = persistent_path.with_name(f".{persistent_path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {"immutable_delta_key": cache_key, "result": result},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, persistent_path)
    return result


def list_telemetry_deltas(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    """List only Project Refresh-button deltas eligible for the 3D overlay.

    Adjacent immutable versions and semantic-diff rows remain retained audit
    evidence, but they cannot supply node/edge color truth under the Refresh
    Delta contract.
    """

    workspace, root = _brain_root(workspace_dir, brain_name)
    version_result = list_brain_versions(workspace, brain_name, verify_hashes=False)
    delta_database = project_delta_root(root, create=False) / "delta_ledger.db"
    try:
        delta_status = delta_database.stat()
        delta_signature: tuple[int | None, int | None] = (
            int(delta_status.st_size),
            int(delta_status.st_mtime_ns),
        )
    except OSError:
        delta_signature = (None, None)
    cache_key = _sha256_text(
        json.dumps(
            {
                "workspace": os.path.normcase(str(workspace)),
                "brain_name": brain_name,
                "refresh_delta_ledger_stat": delta_signature,
                "overlay_authority": REFRESH_DELTA_AUTHORITY,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    with _TELEMETRY_DELTA_CACHE_LOCK:
        cached = _TELEMETRY_DELTA_CACHE.get(cache_key)
        if cached is not None:
            _TELEMETRY_DELTA_CACHE.move_to_end(cache_key)
            result = copy.deepcopy(cached)
            result["delta_cache"] = {"status": "HIT", "scope": "NATIVE_WORKER_SESSION"}
            return result

    ledger = list_project_deltas(root)
    all_entries = list(ledger.get("entries") or [])
    authoritative = [
        item
        for item in all_entries
        if item.get("delta_authority") == REFRESH_DELTA_AUTHORITY
    ]
    bounded = authoritative[-MAX_DELTAS:]
    deltas: list[dict[str, Any]] = []
    for item in bounded:
        detail = get_project_delta(root, delta_id=str(item["delta_id"]))
        if not detail:
            continue
        nodes = list(detail.get("node_changes") or [])
        edges = list(detail.get("edge_changes") or [])
        validations = list(detail.get("validation_results") or [])
        truth_states = {str(row.get("truth_state") or "NEUTRAL") for row in [*nodes, *edges]}
        review_required = any(bool(row.get("review_required")) for row in [*nodes, *edges])
        overlay_state = str(item.get("overlay_state") or "")
        evidence_state = (
            "BLOCKED"
            if "RED" in truth_states
            else "OPEN"
            if review_required or overlay_state != REFRESH_DELTA_OVERLAY_READY
            else "TEST_VERIFIED"
        )
        manifest_refresh = detail.get("manifest", {}).get("refresh", {})
        deltas.append(
            {
                "delta_id": str(item["delta_id"]),
                "display_name": str(item["display_name"]),
                "ordinal": int(item["sequence_number"]),
                "candidate_id": str(item["candidate_id"]),
                "before_version_id": item.get("parent_snapshot_id"),
                "after_version_id": item.get("child_snapshot_id"),
                "before_snapshot_hash": str(item["before_snapshot_hash"]),
                "after_snapshot_hash": str(item["after_snapshot_hash"]),
                "diff_authority": REFRESH_DELTA_AUTHORITY,
                "delta_type": str(item.get("delta_type") or ""),
                "changed_object_count": len(nodes) + len(edges),
                "changed_node_count": len(nodes),
                "changed_edge_count": len(edges),
                "validation_count": len(validations),
                "review_required": review_required,
                "evidence_state": evidence_state,
                "tone": "RED" if "RED" in truth_states else "GREEN" if truth_states == {"GREEN"} else "NEUTRAL",
                "overlay_state": overlay_state,
                "completeness": item.get("completeness") or {},
                "manifest_relative_path": str(item.get("manifest_relative_path") or ""),
                "manifest_sha256": str(item.get("manifest_sha256") or ""),
                "refresh_receipt_path": str(manifest_refresh.get("receipt_path") or ""),
                "refresh_receipt_sha256": str(manifest_refresh.get("receipt_sha256") or ""),
                "automatic_fusion": False,
            }
        )
    result = {
        "schema": TELEMETRY_SCHEMA,
        "status": "PASS",
        "brain_name": brain_name,
        "version_manifest": version_result.get("manifest_path"),
        "version_manifest_sha256": version_result.get("manifest_sha256"),
        "version_count": len(version_result.get("versions") or []),
        "delta_count": len(deltas),
        "deltas": deltas,
        "truncated": len(authoritative) > len(bounded),
        "max_deltas": MAX_DELTAS,
        "overlay_authority": REFRESH_DELTA_AUTHORITY,
        "legacy_non_refresh_delta_count": len(all_entries) - len(authoritative),
        "planning_delta_color_authority": False,
        "read_only": True,
        "raw_project_files_reread": 0,
        "delta_cache_key": cache_key,
        "delta_cache": {"status": "MISS", "scope": "NATIVE_WORKER_SESSION"},
    }
    with _TELEMETRY_DELTA_CACHE_LOCK:
        _TELEMETRY_DELTA_CACHE[cache_key] = copy.deepcopy(result)
        _TELEMETRY_DELTA_CACHE.move_to_end(cache_key)
        while len(_TELEMETRY_DELTA_CACHE) > _TELEMETRY_DELTA_CACHE_LIMIT:
            _TELEMETRY_DELTA_CACHE.popitem(last=False)
    return result


def get_telemetry_snapshot(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    workspace, root = _brain_root(workspace_dir, brain_name)
    latest, versions = _verified_current_version(workspace, brain_name)
    rows = list(versions.get("versions") or [])
    rows[0] = latest
    if len(rows) > 1:
        load_brain_version_record(workspace, brain_name, str(rows[1]["version_id"]), verify_hashes=True)
        rows[1] = {**rows[1], "integrity_status": "VERIFIED"}
    good_by_hash = _semantic_good_snapshots(root)

    def identity(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        snapshot_hash = str(row.get("snapshot_hash") or "")
        good = good_by_hash.get(snapshot_hash)
        return {
            "version_id": row.get("version_id"),
            "snapshot_hash": snapshot_hash,
            "timestamp_utc": row.get("timestamp_utc"),
            "integrity_status": row.get("integrity_status"),
            "current_binding_status": row.get("current_binding_status"),
            "verified_good_snapshot_id": good.get("snapshot_id") if good else None,
            "test_status": good.get("test_status") if good else "UNVERIFIED",
            "build_status": good.get("build_status") if good else "UNVERIFIED",
        }

    refresh = get_refresh_output(workspace, brain_name)
    deltas = list_telemetry_deltas(workspace, brain_name)
    return {
        "schema": TELEMETRY_SCHEMA,
        "status": "PASS",
        "brain_name": brain_name,
        "brain_root": str(root),
        "current_version": identity(rows[0]),
        "previous_version": identity(rows[1] if len(rows) > 1 else None),
        "refresh": refresh,
        "delta_count": deltas["delta_count"],
        "delta_ids": [item["delta_id"] for item in deltas["deltas"]],
        "read_only": True,
        "raw_project_files_reread": 0,
        "automatic_fusion": False,
    }


class _Graph:
    def __init__(self, brain_name: str) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.brain_node_id = f"brain:{slugify_name(brain_name)}"
        self.add_node(
            self.brain_node_id,
            "brain",
            brain_name,
            evidence_state="SOURCE_VERIFIED",
            tone="NEUTRAL",
        )

    def add_node(
        self,
        node_id: str,
        kind: str,
        label: str,
        **values: Any,
    ) -> dict[str, Any]:
        existing = self.nodes.get(node_id)
        if existing:
            for key, value in values.items():
                if value not in (None, "", [], {}):
                    existing.setdefault(key, value)
            return existing
        node = {
            "node_id": node_id,
            "kind": kind,
            "label": str(label or node_id),
            "evidence_state": values.pop("evidence_state", "SOURCE_VERIFIED"),
            "tone": values.pop("tone", "NEUTRAL"),
            "evidence_refs": list(values.pop("evidence_refs", []) or []),
            **values,
        }
        self.nodes[node_id] = node
        return node

    def add_edge(
        self,
        edge_id: str,
        from_node_id: str,
        to_node_id: str,
        relation_type: str,
        *,
        evidence_ref: str = "",
        evidence_state: str = "SOURCE_VERIFIED",
        tone: str = "NEUTRAL",
        derived: bool = False,
        overlay_evidence_direct: bool = False,
    ) -> dict[str, Any]:
        if from_node_id not in self.nodes or to_node_id not in self.nodes:
            raise BrainTelemetryError("TELEMETRY_EDGE_ENDPOINT_MISSING")
        edge = {
            "edge_id": edge_id,
            "from_node_id": from_node_id,
            "to_node_id": to_node_id,
            "relation_type": str(relation_type),
            "evidence_state": evidence_state,
            "tone": tone,
            "evidence_refs": [evidence_ref] if evidence_ref else [],
            "derived_from_indexed_path": bool(derived),
            "overlay_evidence_direct": bool(overlay_evidence_direct),
        }
        self.edges[edge_id] = edge
        return edge

    def apply_evidence(self, node_id: str, status: Any, evidence_ref: Any = "") -> None:
        node = self.nodes.get(node_id)
        if not node:
            return
        candidate_tone = _tone(status)
        node["overlay_evidence_direct"] = True
        node["tone"] = _merge_tone(str(node.get("tone") or "NEUTRAL"), candidate_tone)
        state = _state(status)
        if candidate_tone == "RED" or node.get("evidence_state") in {None, "UNVERIFIED", "SOURCE_VERIFIED"}:
            node["evidence_state"] = state
        ref = str(evidence_ref or "").strip()
        if ref and ref not in node["evidence_refs"]:
            node["evidence_refs"].append(ref)

    def to_payload(self) -> dict[str, Any]:
        return {
            "brain_node_id": self.brain_node_id,
            "nodes": list(self.nodes.values()),
            "edges": list(self.edges.values()),
        }

    @classmethod
    def from_payload(cls, brain_name: str, payload: Mapping[str, Any]) -> "_Graph":
        graph = cls(brain_name)
        graph.brain_node_id = str(payload.get("brain_node_id") or graph.brain_node_id)
        graph.nodes = {
            str(row["node_id"]): dict(row)
            for row in list(payload.get("nodes") or [])
            if isinstance(row, Mapping) and row.get("node_id")
        }
        graph.edges = {
            str(row["edge_id"]): dict(row)
            for row in list(payload.get("edges") or [])
            if isinstance(row, Mapping) and row.get("edge_id")
        }
        return graph


def _typed_node_id(kind: Any, identifier: Any) -> str:
    normalized = str(kind or "object").strip().casefold().replace(" ", "_")
    aliases = {
        "code_file": "file",
        "code_symbol": "symbol",
        "api": "route",
        "app_route": "route",
        "project_artifact": "artifact",
    }
    normalized = aliases.get(normalized, normalized)
    return f"{normalized}:{identifier}"


def _relative_project_path(value: Any) -> str | None:
    normalized = str(value or "").replace("\\", "/").strip().lstrip("./")
    if not normalized:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _folder_id(source_id: str, relative: str) -> str:
    return f"folder:{source_id}:{_sha256_text(relative)[:20]}"


def _source_row(row: sqlite3.Row, sector_id: str) -> dict[str, Any]:
    source_id = str(_pick(row, "source_id"))
    return {
        "source_id": source_id,
        "lane_id": str(_pick(row, "lane_id", "lane_key", "lane", default=sector_id)),
        "display_name": str(
            _pick(row, "display_name", "source_name", "original_name", default=source_id)
        ),
        # Env15 sectors persist their authoritative captured path as
        # ``canonical_path`` while legacy/test sectors use ``source_locator``
        # or ``path``.  Treat all governed aliases as the same read-only open
        # boundary so native Explorer/VS Code handoffs work for real brains.
        "source_locator": str(
            _pick(
                row,
                "source_locator",
                "path",
                "root_path",
                "canonical_path",
                "source_root",
                default="",
            )
        ),
        "availability_state": str(_pick(row, "availability_state", "status", default="AVAILABLE")),
    }


def _indexed_open_target(locator: Any, relative: str | None = None) -> str | None:
    raw = str(locator or "").strip()
    if not raw:
        return None
    source_path = Path(raw).expanduser().resolve()
    base = source_path if source_path.is_dir() else source_path.parent
    candidate = source_path if relative is None else (base / Path(relative)).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return str(candidate) if candidate.exists() else None


def _add_indexed_sector(
    graph: _Graph,
    sector_id: str,
    database: Path,
    *,
    row_limit: int,
) -> dict[str, dict[str, Any]]:
    source_records: dict[str, dict[str, Any]] = {}
    with closing(_connect_read_only(database)) as connection:
        tables = _tables(connection)
        if "source_registry" in tables and "source_id" in _columns(connection, "source_registry"):
            for row in connection.execute("SELECT * FROM source_registry ORDER BY source_id LIMIT ?", (row_limit,)):
                source = _source_row(row, sector_id)
                if not source["source_id"]:
                    continue
                source_records[source["source_id"]] = source
                source_node = _typed_node_id("source", source["source_id"])
                source_target = _indexed_open_target(source["source_locator"])
                graph.add_node(
                    source_node,
                    "source",
                    source["display_name"],
                    lane_id=source["lane_id"],
                    source_id=source["source_id"],
                    availability_state=source["availability_state"],
                    sector_id=sector_id,
                    open_target=source_target,
                )
                graph.add_edge(
                    f"edge:{graph.brain_node_id}:{source_node}",
                    graph.brain_node_id,
                    source_node,
                    "CONTAINS_SOURCE",
                )

        file_records: dict[str, dict[str, Any]] = {}
        if "code_file_snapshot" in tables:
            columns = _columns(connection, "code_file_snapshot")
            required = {"file_id", "source_id", "relative_path"}
            if required <= columns:
                for row in connection.execute(
                    "SELECT * FROM code_file_snapshot ORDER BY source_id,relative_path LIMIT ?", (row_limit,)
                ):
                    item = dict(row)
                    file_id = str(item["file_id"])
                    source_id = str(item["source_id"])
                    relative = _relative_project_path(item["relative_path"])
                    if not relative:
                        continue
                    file_records[file_id] = item
                    source_node = _typed_node_id("source", source_id)
                    if source_node not in graph.nodes:
                        graph.add_node(source_node, "source", source_id, lane_id=sector_id, source_id=source_id)
                        graph.add_edge(
                            f"edge:{graph.brain_node_id}:{source_node}",
                            graph.brain_node_id,
                            source_node,
                            "CONTAINS_SOURCE",
                        )
                    parent = source_node
                    parent_path = ""
                    parts = PurePosixPath(relative).parts
                    for part in parts[:-1]:
                        parent_path = f"{parent_path}/{part}".strip("/")
                        folder_node = _folder_id(source_id, parent_path)
                        folder_target = _indexed_open_target(
                            source_records.get(source_id, {}).get("source_locator"),
                            parent_path,
                        )
                        graph.add_node(
                            folder_node,
                            "folder",
                            parent_path,
                            source_id=source_id,
                            lane_id=source_records.get(source_id, {}).get("lane_id", sector_id),
                            relative_path=parent_path,
                            open_target=folder_target,
                        )
                        graph.add_edge(
                            f"edge:folder:{source_id}:{_sha256_text(parent + '|' + folder_node)[:20]}",
                            parent,
                            folder_node,
                            "CONTAINS_FOLDER",
                            evidence_ref=relative,
                            derived=True,
                        )
                        parent = folder_node
                    file_node = _typed_node_id("file", file_id)
                    values: dict[str, Any] = {
                        "source_id": source_id,
                        "lane_id": source_records.get(source_id, {}).get("lane_id", sector_id),
                        "relative_path": relative,
                        "language": str(_pick(item, "language", default="")),
                        "sha256": str(_pick(item, "sha256", "current_sha256", default="")),
                        "size_bytes": int(_pick(item, "size_bytes", default=0) or 0),
                    }
                    locator = source_records.get(source_id, {}).get("source_locator")
                    target = _indexed_open_target(locator, relative)
                    if target:
                        values["open_target"] = target
                    graph.add_node(file_node, "file", relative, **values)
                    graph.add_edge(
                        f"edge:path:{source_id}:{file_id}",
                        parent,
                        file_node,
                        "CONTAINS_FILE",
                        evidence_ref=relative,
                        derived=True,
                    )

        if "code_symbol" in tables:
            columns = _columns(connection, "code_symbol")
            if {"symbol_id", "file_id"} <= columns:
                for row in connection.execute(
                    "SELECT * FROM code_symbol ORDER BY file_id,symbol_id LIMIT ?", (row_limit,)
                ):
                    item = dict(row)
                    symbol_id = str(item["symbol_id"])
                    graph.add_node(
                        _typed_node_id("symbol", symbol_id),
                        "symbol",
                        str(_pick(item, "symbol_name", "signature", default=symbol_id)),
                        file_id=str(item["file_id"]),
                        symbol_type=str(_pick(item, "symbol_type", default="")),
                        start_line=int(_pick(item, "start_line", default=0) or 0),
                        end_line=int(_pick(item, "end_line", default=0) or 0),
                    )

        if "code_route_api_boundary" in tables:
            columns = _columns(connection, "code_route_api_boundary")
            if {"route_id", "file_id"} <= columns:
                for row in connection.execute(
                    "SELECT * FROM code_route_api_boundary ORDER BY route_id LIMIT ?", (row_limit,)
                ):
                    item = dict(row)
                    route_id = str(item["route_id"])
                    graph.add_node(
                        _typed_node_id("route", route_id),
                        "route",
                        str(_pick(item, "route_path", "route_name", default=route_id)),
                        file_id=str(item["file_id"]),
                        method=str(_pick(item, "http_method", "method", default="")),
                    )

        if "artifact_registry" in tables:
            columns = _columns(connection, "artifact_registry")
            if "artifact_id" in columns:
                for row in connection.execute(
                    "SELECT * FROM artifact_registry ORDER BY artifact_id LIMIT ?", (row_limit,)
                ):
                    item = dict(row)
                    artifact_id = str(item["artifact_id"])
                    graph.add_node(
                        _typed_node_id("artifact", artifact_id),
                        "artifact",
                        str(_pick(item, "logical_path", "path", "artifact_type", default=artifact_id)),
                        source_id=str(_pick(item, "source_id", default="")),
                        artifact_type=str(_pick(item, "artifact_type", default="")),
                        sha256=str(_pick(item, "sha256", "artifact_sha256", default="")),
                    )

        if "code_workflow_edge" in tables:
            columns = _columns(connection, "code_workflow_edge")
            required = {"edge_id", "from_type", "from_id", "relation_type", "to_type", "to_id"}
            if required <= columns:
                rows = list(
                    connection.execute("SELECT * FROM code_workflow_edge ORDER BY edge_id LIMIT ?", (row_limit,))
                )
                for row in rows:
                    item = dict(row)
                    from_node = _typed_node_id(item["from_type"], item["from_id"])
                    to_node = _typed_node_id(item["to_type"], item["to_id"])
                    evidence = str(_pick(item, "evidence_ref", "evidence", default=""))
                    if (
                        str(item["relation_type"]) == "CONTAINS_FILE"
                        and to_node in graph.nodes
                        and any(
                            edge["to_node_id"] == to_node and edge["derived_from_indexed_path"]
                            for edge in graph.edges.values()
                        )
                    ):
                        continue
                    if from_node not in graph.nodes:
                        graph.add_node(from_node, str(item["from_type"]).casefold(), evidence or str(item["from_id"]))
                    if to_node not in graph.nodes:
                        graph.add_node(to_node, str(item["to_type"]).casefold(), evidence or str(item["to_id"]))
                    graph.add_edge(
                        str(item["edge_id"]),
                        from_node,
                        to_node,
                        str(item["relation_type"]),
                        evidence_ref=evidence,
                    )
    return source_records


def _overlay_rows(root: Path, diff_run_id: str, *, row_limit: int) -> dict[str, list[dict[str, Any]]]:
    database = _semantic_database(root)
    if not database.is_file():
        return {}
    table_queries = {
        "files": "SELECT path,change_kind,previous_hash,current_hash FROM brain_changed_file WHERE diff_run_id=? ORDER BY path LIMIT ?",
        "routes": "SELECT route,change_kind,evidence_path FROM brain_route_change WHERE diff_run_id=? ORDER BY route,evidence_path LIMIT ?",
        "symbols": "SELECT symbol,change_kind,evidence_path FROM brain_symbol_change WHERE diff_run_id=? ORDER BY symbol,evidence_path LIMIT ?",
        "validations": "SELECT result_kind,status,evidence FROM brain_test_build_result WHERE diff_run_id=? ORDER BY result_kind LIMIT ?",
        "sectors": "SELECT sector,change_kind,evidence_path FROM brain_backend_sector_change WHERE diff_run_id=? ORDER BY sector,evidence_path LIMIT ?",
        "packages": "SELECT contract,change_kind,evidence_path FROM brain_package_contract_change WHERE diff_run_id=? ORDER BY contract,evidence_path LIMIT ?",
    }
    with closing(_connect_read_only(database)) as connection:
        tables = _tables(connection)
        return {
            key: [dict(row) for row in connection.execute(query, (diff_run_id, row_limit))]
            for key, query in table_queries.items()
            if query.split(" FROM ", 1)[1].split(" ", 1)[0] in tables
        }


def _find_nodes(graph: _Graph, *, kind: str, value: str) -> list[str]:
    needle = str(value or "").replace("\\", "/").casefold()
    matches = []
    for node_id, node in graph.nodes.items():
        if node.get("kind") != kind:
            continue
        candidates = {
            str(node.get("label") or "").replace("\\", "/").casefold(),
            str(node.get("relative_path") or "").replace("\\", "/").casefold(),
        }
        if needle in candidates:
            matches.append(node_id)
    return matches


_DIRECT_OVERLAY_RELATIONS: dict[str, tuple[str, ...]] = {
    "source": ("CONTAINS_SOURCE",),
    "folder": ("CONTAINS_FOLDER",),
    "file": ("CONTAINS_FILE",),
    "symbol": ("DECLARES_SYMBOL", "CONTAINS_SYMBOL"),
    "route": ("EXPOSES_ROUTE", "DECLARES_ROUTE", "IMPLEMENTS_ROUTE"),
    "test": ("VERIFIED_BY_TEST", "HAS_TEST", "TESTS", "VALIDATES"),
    "artifact": ("PROJECTS_ARTIFACT", "PRODUCES_ARTIFACT", "CONTAINS_ARTIFACT"),
    "package": ("HAS_VALIDATED_PACKAGE",),
    "validation": ("HAS_VALIDATION_RESULT",),
    "change": ("HAS_VERSION_CHANGE", "HAS_SEMANTIC_CHANGE"),
}


def _apply_direct_relation_evidence(
    graph: _Graph,
    node_id: str,
    status: Any,
    evidence_ref: Any = "",
) -> None:
    """Color only the relation that directly owns/describes an evidenced node.

    Endpoint tone is deliberately insufficient: one failed node can have many
    unrelated incident relations.  The overlay therefore follows the indexed
    ownership/declaration relation for the concrete node kind and never fans a
    color out to every touching edge.
    """

    node = graph.nodes.get(node_id)
    if not node:
        return
    relation_types = _DIRECT_OVERLAY_RELATIONS.get(str(node.get("kind") or ""), ())
    if not relation_types:
        return
    candidates = [
        edge
        for edge in graph.edges.values()
        if edge.get("relation_type") in relation_types
        and (edge.get("to_node_id") == node_id or edge.get("from_node_id") == node_id)
    ]
    if not candidates:
        return
    candidates.sort(
        key=lambda edge: (
            0 if edge.get("to_node_id") == node_id else 1,
            relation_types.index(str(edge.get("relation_type"))),
            str(edge.get("edge_id") or ""),
        )
    )
    edge = candidates[0]
    tone = _tone(status)
    edge["tone"] = tone
    edge["evidence_state"] = _state(status)
    edge["overlay_evidence_direct"] = True
    ref = str(evidence_ref or "").strip()
    if not ref:
        ref = next((str(value).strip() for value in node.get("evidence_refs") or [] if str(value).strip()), "")
    if not ref:
        ref = str(edge.get("relation_type") or "DIRECT_INDEXED_RELATION")
    refs = list(edge.get("evidence_refs") or [])
    if ref not in refs:
        refs.append(ref)
    edge["evidence_refs"] = refs


def _apply_legacy_semantic_overlay_not_color_authority(
    graph: _Graph,
    root: Path,
    selected: Mapping[str, Any],
    *,
    row_limit: int,
) -> None:
    def apply_node(node_id: str, status: Any, evidence_ref: Any = "") -> None:
        graph.apply_evidence(node_id, status, evidence_ref)
        _apply_direct_relation_evidence(graph, node_id, status, evidence_ref)

    diff_run_id = str(selected.get("diff_run_id") or "")
    if not diff_run_id:
        for change in selected.get("fallback_changes") or []:
            path = str(change.get("path") or "")
            matched = _find_nodes(graph, kind="file", value=path)
            if matched:
                for node_id in matched:
                    apply_node(node_id, change.get("change_kind"), change.get("evidence_ref"))
                continue
            node_id = "change:" + _sha256_text(f"{selected.get('delta_id')}|{path}")[:24]
            graph.add_node(
                node_id,
                "change",
                path,
                change_kind=change.get("change_kind"),
                evidence_state="CANDIDATE",
                tone="NEUTRAL",
                evidence_refs=[str(change.get("evidence_ref") or "")],
                overlay_evidence_direct=True,
            )
            graph.add_edge(
                "edge:" + node_id,
                graph.brain_node_id,
                node_id,
                "HAS_VERSION_CHANGE",
                evidence_ref=str(change.get("evidence_ref") or ""),
                overlay_evidence_direct=True,
            )
        return

    overlay = _overlay_rows(root, diff_run_id, row_limit=row_limit)
    for item in overlay.get("files", []):
        path = str(item.get("path") or "")
        matched = _find_nodes(graph, kind="file", value=path)
        if not matched:
            node_id = "change:" + _sha256_text(f"{diff_run_id}|file|{path}")[:24]
            graph.add_node(
                node_id,
                "change",
                path,
                change_kind=item.get("change_kind"),
                previous_hash=item.get("previous_hash"),
                current_hash=item.get("current_hash"),
                evidence_state=_state(item.get("change_kind")),
                tone=_tone(item.get("change_kind")),
                overlay_evidence_direct=True,
            )
            graph.add_edge(
                "edge:" + node_id,
                graph.brain_node_id,
                node_id,
                "HAS_SEMANTIC_CHANGE",
                evidence_ref=path,
                evidence_state=_state(item.get("change_kind")),
                tone=_tone(item.get("change_kind")),
                overlay_evidence_direct=True,
            )
        for node_id in matched:
            apply_node(node_id, item.get("change_kind"), path)
    for item in overlay.get("routes", []):
        for node_id in _find_nodes(graph, kind="route", value=str(item.get("route") or "")):
            apply_node(node_id, item.get("change_kind"), item.get("evidence_path"))
    for item in overlay.get("symbols", []):
        for node_id in _find_nodes(graph, kind="symbol", value=str(item.get("symbol") or "")):
            apply_node(node_id, item.get("change_kind"), item.get("evidence_path"))
    for item in overlay.get("validations", []):
        kind = str(item.get("result_kind") or "validation")
        node_id = f"validation:{diff_run_id}:{kind}"
        graph.add_node(
            node_id,
            "validation",
            kind,
            evidence_state=_state(item.get("status")),
            tone=_tone(item.get("status")),
            evidence_refs=[str(item.get("evidence") or "")],
            status=str(item.get("status") or ""),
            overlay_evidence_direct=True,
        )
        graph.add_edge(
            "edge:" + node_id,
            graph.brain_node_id,
            node_id,
            "HAS_VALIDATION_RESULT",
            evidence_ref=str(item.get("evidence") or ""),
            evidence_state=_state(item.get("status")),
            tone=_tone(item.get("status")),
            overlay_evidence_direct=True,
        )
        evidence = str(item.get("evidence") or "").replace("\\", "/").casefold()
        for target_id, target in graph.nodes.items():
            label = str(target.get("label") or "").replace("\\", "/").casefold()
            if target.get("kind") == "test" and label and label in evidence:
                apply_node(target_id, item.get("status"), item.get("evidence"))


def _refresh_node_matches(graph: _Graph, item: Mapping[str, Any]) -> list[str]:
    direct = str(item.get("object_id") or "")
    if direct in graph.nodes:
        return [direct]
    paths = [
        str(item.get("relative_path") or ""),
        str(item.get("previous_path") or ""),
    ]
    matches: list[str] = []
    for path in paths:
        if not path:
            continue
        for node_id in _find_nodes(graph, kind=str(item.get("object_kind") or "file"), value=path):
            if node_id not in matches:
                matches.append(node_id)
    return matches


def _refresh_edge_match(graph: _Graph, object_id: str) -> str | None:
    candidates = (object_id, f"edge:{object_id}", f"edge:indexed:{object_id}")
    for candidate in candidates:
        if candidate in graph.edges:
            return candidate
    return next(
        (
            edge_id
            for edge_id in graph.edges
            if edge_id.endswith(f":{object_id}") or str(graph.edges[edge_id].get("indexed_edge_id") or "") == object_id
        ),
        None,
    )


def _apply_overlay(
    graph: _Graph,
    root: Path,
    selected: Mapping[str, Any],
    *,
    row_limit: int,
) -> None:
    del row_limit
    if selected.get("diff_authority") != REFRESH_DELTA_AUTHORITY:
        raise BrainTelemetryError("TELEMETRY_REFRESH_DELTA_AUTHORITY_REQUIRED")
    if selected.get("overlay_state") != REFRESH_DELTA_OVERLAY_READY:
        raise BrainTelemetryError("REFRESH_DELTA_INCOMPLETE_OVERLAY_BLOCKED")
    detail = get_project_delta(root, delta_id=str(selected.get("delta_id") or ""))
    if not detail or detail.get("delta_authority") != REFRESH_DELTA_AUTHORITY:
        raise BrainTelemetryError("TELEMETRY_REFRESH_DELTA_EVIDENCE_MISSING")
    if detail.get("overlay_state") != REFRESH_DELTA_OVERLAY_READY:
        raise BrainTelemetryError("REFRESH_DELTA_INCOMPLETE_OVERLAY_BLOCKED")
    validations = {
        str(item.get("validation_id") or ""): item
        for item in detail.get("validation_results") or []
    }

    for item in detail.get("node_changes") or []:
        node_ids = _refresh_node_matches(graph, item)
        if not node_ids:
            node_id = str(item.get("object_id") or "")
            if not node_id or node_id in graph.nodes:
                node_id = "file:refresh:" + _sha256_text(
                    f"{selected.get('delta_id')}|{item.get('relative_path')}"
                )[:24]
            node = graph.add_node(
                node_id,
                str(item.get("object_kind") or "file"),
                str(item.get("relative_path") or node_id),
                relative_path=str(item.get("relative_path") or ""),
                change_kind=str(item.get("change_kind") or "UNKNOWN"),
                evidence_state="CANDIDATE",
                tone="NEUTRAL",
                evidence_refs=[],
                overlay_evidence_direct=True,
                refresh_added_projection=True,
            )
            parent_path = str(PurePosixPath(str(item.get("relative_path") or "")).parent)
            parent = next(
                (
                    candidate_id
                    for candidate_id, candidate in graph.nodes.items()
                    if candidate.get("kind") == "folder"
                    and str(candidate.get("relative_path") or candidate.get("label") or "").replace("\\", "/") == parent_path
                ),
                graph.brain_node_id,
            )
            graph.add_edge(
                "edge:refresh-object:" + _sha256_text(node_id)[:20],
                parent,
                node_id,
                "HAS_REFRESH_OBJECT",
                evidence_ref=str(detail.get("manifest_relative_path") or ""),
                evidence_state="SOURCE_VERIFIED",
                tone="NEUTRAL",
                overlay_evidence_direct=False,
            )
            node_ids = [node_id]
        validation_details = [
            validations[value]
            for value in item.get("validation_ids") or []
            if value in validations
        ]
        for node_id in node_ids:
            node = graph.nodes[node_id]
            node["tone"] = str(item.get("truth_state") or "NEUTRAL")
            node["evidence_state"] = (
                "OPEN"
                if item.get("review_required")
                else "BLOCKED"
                if node["tone"] == "RED"
                else "TEST_VERIFIED"
                if node["tone"] == "GREEN"
                else "SOURCE_VERIFIED"
            )
            node["overlay_evidence_direct"] = True
            node["review_required"] = bool(item.get("review_required"))
            node["review_indicator"] = "REVIEW_REQUIRED" if item.get("review_required") else None
            node["change_kind"] = str(item.get("change_kind") or "UNKNOWN")
            node["tombstone"] = str(item.get("change_kind") or "") in {"DELETED", "REMOVED"}
            if item.get("previous_path"):
                node["previous_path"] = str(item["previous_path"])
                node["relative_path"] = str(item.get("relative_path") or "")
                node["label"] = str(item.get("relative_path") or node.get("label") or node_id)
            node["refresh_evidence"] = {
                "refresh_delta_id": str(selected.get("delta_id") or ""),
                "candidate_id": str(selected.get("candidate_id") or ""),
                "object_id": str(item.get("object_id") or node_id),
                "exact_path": str(item.get("relative_path") or ""),
                "previous_path": item.get("previous_path"),
                "old_hash": item.get("previous_sha256"),
                "new_hash": item.get("current_sha256"),
                "change_kind": str(item.get("change_kind") or "UNKNOWN"),
                "truth_state": node["tone"],
                "review_required": bool(item.get("review_required")),
                "validation_results": validation_details,
                "receipt_path": str(selected.get("refresh_receipt_path") or ""),
                "parent_snapshot": str(selected.get("before_version_id") or ""),
                "child_snapshot": str(selected.get("after_version_id") or ""),
                "commit_sha": str((detail.get("git_evidence") or {}).get("head_commit_sha") or ""),
                "impact_scope": str(item.get("impact_scope") or "DIRECT"),
                "evidence": item.get("evidence") or {},
            }
            reference = str(selected.get("refresh_receipt_path") or detail.get("manifest_relative_path") or "")
            if reference and reference not in node["evidence_refs"]:
                node["evidence_refs"].append(reference)

    for item in detail.get("edge_changes") or []:
        object_id = str(item.get("object_id") or "")
        edge_id = _refresh_edge_match(graph, object_id)
        if edge_id is None:
            from_id = str(item.get("from_object_id") or "")
            to_id = str(item.get("to_object_id") or "")
            if from_id in graph.nodes and to_id in graph.nodes:
                edge_id = "edge:refresh:" + _sha256_text(
                    f"{selected.get('delta_id')}|{object_id}"
                )[:24]
                graph.add_edge(
                    edge_id,
                    from_id,
                    to_id,
                    str(item.get("relation_type") or "UNKNOWN"),
                    evidence_ref=str(selected.get("refresh_receipt_path") or ""),
                    evidence_state="SOURCE_VERIFIED",
                    tone="NEUTRAL",
                    overlay_evidence_direct=True,
                )
        if edge_id is None:
            continue
        edge = graph.edges[edge_id]
        edge["tone"] = str(item.get("truth_state") or "NEUTRAL")
        edge["evidence_state"] = (
            "OPEN"
            if item.get("review_required")
            else "BLOCKED"
            if edge["tone"] == "RED"
            else "TEST_VERIFIED"
            if edge["tone"] == "GREEN"
            else "SOURCE_VERIFIED"
        )
        edge["overlay_evidence_direct"] = True
        edge["review_required"] = bool(item.get("review_required"))
        edge["review_indicator"] = "REVIEW_REQUIRED" if item.get("review_required") else None
        edge["change_kind"] = str(item.get("change_kind") or "UNKNOWN")
        edge["refresh_evidence"] = {
            "refresh_delta_id": str(selected.get("delta_id") or ""),
            "candidate_id": str(selected.get("candidate_id") or ""),
            "object_id": object_id,
            "relation_type": str(item.get("relation_type") or "UNKNOWN"),
            "old_relation_state": item.get("previous_state") or {},
            "new_relation_state": item.get("current_state") or {},
            "change_kind": str(item.get("change_kind") or "UNKNOWN"),
            "truth_state": edge["tone"],
            "review_required": bool(item.get("review_required")),
            "validation_results": [
                validations[value]
                for value in item.get("validation_ids") or []
                if value in validations
            ],
            "receipt_path": str(selected.get("refresh_receipt_path") or ""),
            "parent_snapshot": str(selected.get("before_version_id") or ""),
            "child_snapshot": str(selected.get("after_version_id") or ""),
            "commit_sha": str((detail.get("git_evidence") or {}).get("head_commit_sha") or ""),
            "impact_scope": str(item.get("impact_scope") or "PROVEN_DIRECT"),
            "evidence": item.get("evidence") or {},
        }


def _add_package_nodes(graph: _Graph, root: Path, current_hash: str) -> None:
    good = _semantic_good_snapshots(root).get(current_hash)
    if not good:
        return
    package_path = str(good.get("brain_package_path") or "")
    if not package_path:
        return
    package_id = "package:" + _sha256_text(package_path)[:24]
    statuses = [good.get("test_status"), good.get("build_status")]
    historical_validation_state = (
        "BLOCKED"
        if any(_tone(value) == "RED" for value in statuses)
        else "TEST_VERIFIED"
        if all(_tone(value) == "GREEN" for value in statuses)
        else "UNVERIFIED"
    )
    graph.add_node(
        package_id,
        "package",
        Path(package_path).name or package_path,
        package_path=package_path,
        package_sha256=str(good.get("brain_package_hash") or ""),
        evidence_state="SOURCE_VERIFIED",
        tone="NEUTRAL",
        historical_validation_state=historical_validation_state,
        overlay_color_authority=False,
        evidence_refs=[str(good.get("reason") or "")],
    )
    package_edge = graph.add_edge(
        "edge:" + package_id,
        graph.brain_node_id,
        package_id,
        "HAS_VALIDATED_PACKAGE",
        evidence_ref=str(good.get("reason") or ""),
        evidence_state="SOURCE_VERIFIED",
        tone="NEUTRAL",
    )
    package_edge["historical_validation_state"] = historical_validation_state
    package_edge["overlay_color_authority"] = False


def _scoped_ids(graph: _Graph, scope_id: str | None) -> set[str]:
    if not scope_id:
        return set(graph.nodes)
    if scope_id not in graph.nodes:
        raise BrainTelemetryError("TELEMETRY_SCOPE_NOT_FOUND")
    outgoing: dict[str, list[str]] = {}
    for edge in graph.edges.values():
        outgoing.setdefault(str(edge["from_node_id"]), []).append(str(edge["to_node_id"]))
    selected = {scope_id}
    queue = deque([scope_id])
    while queue:
        current = queue.popleft()
        for target in sorted(outgoing.get(current, [])):
            if target not in selected:
                selected.add(target)
                queue.append(target)
    return selected


_TELEMETRY_GRAPH_CACHE_LIMIT = 12
_TELEMETRY_GRAPH_CACHE_LOCK = threading.RLock()
_TELEMETRY_GRAPH_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _graph_source_stat(path: Path) -> tuple[str, int, int] | tuple[str, None, None]:
    try:
        status = path.stat()
    except OSError:
        return str(path), None, None
    return str(path), int(status.st_size), int(status.st_mtime_ns)


def _telemetry_graph_cache_probe(
    workspace: Path,
    root: Path,
    brain_name: str,
) -> tuple[str, list[tuple[str, Path]]]:
    """Return a cheap immutable/index signature without rereading raw source.

    A full immutable hash verification is mandatory on cache miss. A warm
    query is keyed by the stamped version manifest plus every SQLite authority
    that can affect the rendered topology or delta overlay.
    """

    version_result = list_brain_versions(workspace, brain_name, verify_hashes=False)
    versions = list(version_result.get("versions") or [])
    if not versions:
        raise BrainTelemetryError("TELEMETRY_IMMUTABLE_VERSION_REQUIRED")
    latest = dict(versions[0])
    sectors = _sector_databases(root)
    authorities = [
        _graph_source_stat(Path(str(version_result.get("manifest_path") or root / "brain_versions" / "VERSION_MANIFEST.json"))),
        _graph_source_stat(root / "project" / "project_router.sqlite"),
        _graph_source_stat(_semantic_database(root)),
    ]
    authorities.extend(_graph_source_stat(database) for _, database in sectors)
    body = {
        "workspace": os.path.normcase(str(workspace)),
        "brain_name": brain_name,
        "version_id": str(latest.get("version_id") or ""),
        "snapshot_hash": str(latest.get("snapshot_hash") or ""),
        "manifest_sha256": str(version_result.get("manifest_sha256") or ""),
        "authorities": authorities,
    }
    return _sha256_text(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))), sectors


def _telemetry_graph_cache_get(cache_key: str) -> dict[str, Any] | None:
    with _TELEMETRY_GRAPH_CACHE_LOCK:
        cached = _TELEMETRY_GRAPH_CACHE.get(cache_key)
        if cached is None:
            return None
        _TELEMETRY_GRAPH_CACHE.move_to_end(cache_key)
        return cached


def _telemetry_graph_cache_put(cache_key: str, value: dict[str, Any]) -> None:
    with _TELEMETRY_GRAPH_CACHE_LOCK:
        _TELEMETRY_GRAPH_CACHE[cache_key] = value
        _TELEMETRY_GRAPH_CACHE.move_to_end(cache_key)
        while len(_TELEMETRY_GRAPH_CACHE) > _TELEMETRY_GRAPH_CACHE_LIMIT:
            _TELEMETRY_GRAPH_CACHE.popitem(last=False)


def _telemetry_persistent_cache_path(workspace: Path, brain_name: str) -> Path:
    return (
        workspace
        / ".evidenceos_runtime"
        / slugify_name(brain_name)
        / "telemetry_folder_graph_v2.json"
    )


def _telemetry_persistent_cache_get(
    workspace: Path,
    brain_name: str,
    cache_key: str,
) -> dict[str, Any] | None:
    path = _telemetry_persistent_cache_path(workspace, brain_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("immutable_index_key") != cache_key:
        return None
    try:
        return {
            "latest_version": dict(payload["latest_version"]),
            "versions": dict(payload["versions"]),
            "deltas": dict(payload["deltas"]),
            "base_graph": _Graph.from_payload(brain_name, payload["base_graph"]),
            "overlay_graphs": {},
            "sector_count": int(payload["sector_count"]),
            "persistent_cache_path": str(path),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _telemetry_persistent_cache_put(
    workspace: Path,
    brain_name: str,
    cache_key: str,
    cached: Mapping[str, Any],
) -> Path:
    path = _telemetry_persistent_cache_path(workspace, brain_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    graph = cached.get("base_graph")
    if not isinstance(graph, _Graph):
        raise BrainTelemetryError("TELEMETRY_PERSISTENT_CACHE_GRAPH_INVALID")
    payload = {
        "schema": "EVIDENCEOS_DURABLE_FOLDER_GRAPH_CACHE_V2",
        "immutable_index_key": cache_key,
        "latest_version": cached["latest_version"],
        "versions": cached["versions"],
        "deltas": cached["deltas"],
        "base_graph": graph.to_payload(),
        "sector_count": int(cached["sector_count"]),
        "raw_project_files_reread": 0,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def load_materialized_telemetry_bundle(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    """Hydrate the accepted 3D surface from one already-materialized JSON file.

    Ordinary brain selection is a state-travel read.  It must never probe the
    router, sector SQLite files, immutable-version payloads, Refresh inputs, or
    raw sources.  Build/Fuse owns materialization; this function only opens the
    stored accepted bundle and returns it in the exact UI contract.
    """

    workspace = normalize_workspace_dir(workspace_dir).resolve()
    path = _telemetry_persistent_cache_path(workspace, brain_name)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise BrainTelemetryError("TELEMETRY_STORED_ACCEPTED_BUNDLE_REQUIRED") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrainTelemetryError("TELEMETRY_STORED_ACCEPTED_BUNDLE_INVALID") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "EVIDENCEOS_DURABLE_FOLDER_GRAPH_CACHE_V2":
        raise BrainTelemetryError("TELEMETRY_STORED_ACCEPTED_BUNDLE_SCHEMA_INVALID")
    immutable_index_key = str(payload.get("immutable_index_key") or "").lower()
    if len(immutable_index_key) != 64 or any(character not in "0123456789abcdef" for character in immutable_index_key):
        raise BrainTelemetryError("TELEMETRY_STORED_ACCEPTED_BUNDLE_KEY_INVALID")
    latest = payload.get("latest_version")
    versions = payload.get("versions")
    deltas = payload.get("deltas")
    graph_payload = payload.get("base_graph")
    if not all(isinstance(value, dict) for value in (latest, versions, deltas, graph_payload)):
        raise BrainTelemetryError("TELEMETRY_STORED_ACCEPTED_BUNDLE_PAYLOAD_INVALID")
    graph = _Graph.from_payload(brain_name, graph_payload)
    nodes = sorted(
        (copy.deepcopy(node) for node in graph.nodes.values()),
        key=lambda node: (
            _KIND_ORDER.get(str(node.get("kind")), 99),
            str(node.get("label") or "").casefold(),
            str(node.get("node_id") or ""),
        ),
    )
    edges = sorted(
        (copy.deepcopy(edge) for edge in graph.edges.values()),
        key=lambda edge: (str(edge.get("relation_type") or ""), str(edge.get("edge_id") or "")),
    )
    latest_row = copy.deepcopy(latest)
    delta_result = copy.deepcopy(deltas)
    snapshot = {
        "schema": TELEMETRY_SCHEMA,
        "status": "PASS",
        "brain_name": brain_name,
        "brain_root": str(brain_output_dir(workspace, brain_name)),
        "current_version": {
            "version_id": latest_row.get("version_id"),
            "snapshot_hash": latest_row.get("snapshot_hash"),
            "timestamp_utc": latest_row.get("timestamp_utc"),
            "integrity_status": latest_row.get("integrity_status") or "VERIFIED_AT_MATERIALIZATION",
            "current_binding_status": latest_row.get("current_binding_status") or "ACCEPTED_MATERIALIZED_SNAPSHOT",
        },
        "previous_version": None,
        "refresh": {"status": "NOT_INVOKED_BY_ORDINARY_VIEW"},
        "delta_count": int(delta_result.get("delta_count") or 0),
        "delta_ids": [str(item.get("delta_id") or "") for item in list(delta_result.get("deltas") or [])],
        "read_only": True,
        "raw_project_files_reread": 0,
        "automatic_fusion": False,
    }
    graph_result = {
        "schema": TELEMETRY_SCHEMA,
        "status": "PASS",
        "brain_name": brain_name,
        "snapshot": {
            "version_id": latest_row.get("version_id"),
            "snapshot_hash": latest_row.get("snapshot_hash"),
            "integrity_status": latest_row.get("integrity_status") or "VERIFIED_AT_MATERIALIZATION",
        },
        "selected_delta": None,
        "scope": {"scope_id": None, "mode": "FULL_INDEXED_GRAPH"},
        "nodes": nodes,
        "edges": edges,
        "counts": {
            "sector_databases": int(payload.get("sector_count") or 0),
            "nodes_returned": len(nodes),
            "nodes_total_in_scope": len(nodes),
            "edges_returned": len(edges),
        },
        "truncated": False,
        "max_nodes": 0,
        "read_only": True,
        "raw_project_files_reread": 0,
        "duplicate_topology_created": False,
        "automatic_fusion": False,
        "topology_cache": {
            "status": "HIT",
            "immutable_index_key": immutable_index_key,
            "scope": "DURABLE_ACCEPTED_SNAPSHOT_SINGLE_FILE",
            "tier": "ATOMIC_STORED_BUNDLE",
            "cache_path": str(path),
            "raw_project_reread": False,
        },
    }
    return {
        "schema": "EVIDENCEOS_ATOMIC_ACCEPTED_TELEMETRY_BUNDLE_V1",
        "status": "READY",
        "brain_name": brain_name,
        "accepted_version_id": str(latest_row.get("version_id") or ""),
        "accepted_snapshot_hash": str(latest_row.get("snapshot_hash") or ""),
        "immutable_index_key": immutable_index_key,
        "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "cache_path": str(path),
        "contract": telemetry_contract(),
        "snapshot": snapshot,
        "deltas": delta_result,
        "graph": graph_result,
        "hydration_law": "STORED_ACCEPTED_TOPOLOGY_ZERO_SCAN_ZERO_BUILD_ZERO_DELTA",
        "raw_project_files_reread": 0,
        "index_probe_count": 0,
        "build_count": 0,
        "refresh_count": 0,
        "fuse_count": 0,
        "mutation_count": 0,
    }


def query_telemetry_graph(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    delta_id: str | None = None,
    scope_id: str | None = None,
    max_nodes: int | None = DEFAULT_MAX_NODES,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    workspace, root = _brain_root(workspace_dir, brain_name)
    try:
        bounded_max = int(max_nodes or 0)
    except (TypeError, ValueError) as exc:
        raise BrainTelemetryError("TELEMETRY_MAX_NODES_INVALID") from exc
    if bounded_max < 0 or bounded_max > MAX_MAX_NODES:
        raise BrainTelemetryError("TELEMETRY_MAX_NODES_OUT_OF_RANGE")
    cache_key, sectors = _telemetry_graph_cache_probe(workspace, root, brain_name)
    cached = None if force_rebuild else _telemetry_graph_cache_get(cache_key)
    cache_tier = "MEMORY" if cached is not None else ""
    if cached is None and not force_rebuild:
        cached = _telemetry_persistent_cache_get(workspace, brain_name, cache_key)
        if cached is not None:
            cache_tier = "DURABLE_DISK"
            _telemetry_graph_cache_put(cache_key, cached)
    cache_status = "HIT" if cached is not None else "MISS"
    if cached is None:
        latest_version, versions = _verified_current_version(workspace, brain_name)
        version_rows = list(versions.get("versions") or [])
        version_rows[0] = latest_version
        base_graph = _Graph(brain_name)
        for sector_id, database in sectors:
            _add_indexed_sector(base_graph, sector_id, database, row_limit=MAX_MAX_NODES)
        _add_package_nodes(base_graph, root, str(version_rows[0].get("snapshot_hash") or ""))
        cached = {
            "latest_version": copy.deepcopy(latest_version),
            "versions": copy.deepcopy(versions),
            "deltas": {},
            "base_graph": copy.deepcopy(base_graph),
            "overlay_graphs": {},
            "sector_count": len(sectors),
        }
        _telemetry_graph_cache_put(cache_key, cached)
        persistent_path = _telemetry_persistent_cache_put(
            workspace, brain_name, cache_key, cached
        )
        cached["persistent_cache_path"] = str(persistent_path)
        cache_tier = "MATERIALIZED_DURABLE_DISK"
    else:
        latest_version = copy.deepcopy(cached["latest_version"])
        versions = copy.deepcopy(cached["versions"])
        version_rows = list(versions.get("versions") or [])
        version_rows[0] = latest_version
    deltas = list_telemetry_deltas(workspace, brain_name)
    cached["deltas"] = copy.deepcopy(deltas)

    selected_delta = None
    if delta_id:
        selected_delta = next((item for item in deltas["deltas"] if item["delta_id"] == delta_id), None)
        if not selected_delta:
            raise BrainTelemetryError("TELEMETRY_DELTA_NOT_FOUND")

    overlay_key = (
        f"{delta_id}|{selected_delta.get('manifest_sha256')}"
        if selected_delta
        else ""
    )
    overlay_graphs = cached["overlay_graphs"]
    cached_graph = overlay_graphs.get(overlay_key)
    if cached_graph is None:
        graph = copy.deepcopy(cached["base_graph"])
        if selected_delta:
            _apply_overlay(graph, root, selected_delta, row_limit=MAX_DELTA_CHANGES)
        with _TELEMETRY_GRAPH_CACHE_LOCK:
            overlay_graphs[overlay_key] = copy.deepcopy(graph)
    else:
        graph = copy.deepcopy(cached_graph)

    selected_ids = _scoped_ids(graph, scope_id)
    all_nodes = [node for node_id, node in graph.nodes.items() if node_id in selected_ids]
    all_nodes.sort(
        key=lambda node: (
            _KIND_ORDER.get(str(node.get("kind")), 99),
            str(node.get("label") or "").casefold(),
            str(node.get("node_id")),
        )
    )
    total_nodes = len(all_nodes)
    if bounded_max and total_nodes > bounded_max:
        all_nodes = all_nodes[:bounded_max]
    included = {str(node["node_id"]) for node in all_nodes}
    edges = [
        edge
        for edge in graph.edges.values()
        if edge["from_node_id"] in included and edge["to_node_id"] in included
    ]
    edges.sort(key=lambda edge: (str(edge["relation_type"]), str(edge["edge_id"])))
    return {
        "schema": TELEMETRY_SCHEMA,
        "status": "PASS",
        "brain_name": brain_name,
        "snapshot": {
            "version_id": version_rows[0].get("version_id"),
            "snapshot_hash": version_rows[0].get("snapshot_hash"),
            "integrity_status": version_rows[0].get("integrity_status"),
        },
        "selected_delta": selected_delta,
        "scope": {
            "scope_id": scope_id,
            "mode": "REPLACED_WITH_INDEXED_SUBGRAPH" if scope_id else "FULL_INDEXED_GRAPH",
        },
        "nodes": all_nodes,
        "edges": edges,
        "counts": {
            "sector_databases": int(cached["sector_count"]),
            "nodes_returned": len(all_nodes),
            "nodes_total_in_scope": total_nodes,
            "edges_returned": len(edges),
        },
        "truncated": bool(bounded_max and total_nodes > bounded_max),
        "max_nodes": bounded_max,
        "read_only": True,
        "raw_project_files_reread": 0,
        "duplicate_topology_created": False,
        "automatic_fusion": False,
        "topology_cache": {
            "status": cache_status,
            "immutable_index_key": cache_key,
            "scope": "DURABLE_NATIVE_WORKER_AND_DISK",
            "tier": cache_tier,
            "cache_path": str(cached.get("persistent_cache_path") or _telemetry_persistent_cache_path(workspace, brain_name)),
            "raw_project_reread": False,
        },
    }


def materialize_telemetry_graph_cache(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    """Seal the complete folder graph beside durable runtime state after Build/Fuse."""

    graph = query_telemetry_graph(
        workspace_dir,
        brain_name,
        max_nodes=0,
        force_rebuild=True,
    )
    return {
        "status": "PASS",
        "brain_name": brain_name,
        "node_count": graph["counts"]["nodes_total_in_scope"],
        "edge_count": graph["counts"]["edges_returned"],
        "cache": graph["topology_cache"],
    }


def _indexed_open_boundaries(root: Path) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for sector_id, database in _sector_databases(root):
        with closing(_connect_read_only(database)) as connection:
            tables = _tables(connection)
            if "source_registry" in tables and "source_id" in _columns(connection, "source_registry"):
                count = int(connection.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0])
                if count > MAX_MAX_NODES:
                    raise BrainTelemetryError("TELEMETRY_SOURCE_BOUNDARY_LIMIT_EXCEEDED")
                for row in connection.execute(
                    "SELECT * FROM source_registry ORDER BY source_id LIMIT ?", (MAX_MAX_NODES,)
                ):
                    source = _source_row(row, sector_id)
                    locator = str(source.get("source_locator") or "").strip()
                    if not locator:
                        continue
                    source["root"] = Path(locator).expanduser().resolve()
                    source["database"] = database
                    boundaries.append(source)
    return sorted(boundaries, key=lambda item: len(str(item["root"])), reverse=True)


def _like_prefix(relative: str) -> str:
    escaped = relative.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped.rstrip("/") + "/%"


def _target_is_indexed(source: Mapping[str, Any], resolved: Path) -> bool:
    boundary = Path(source["root"])
    if boundary.is_file():
        return resolved == boundary
    if resolved == boundary:
        return True
    relative = resolved.relative_to(boundary).as_posix()
    database = Path(source["database"])
    source_id = str(source["source_id"])
    with closing(_connect_read_only(database)) as connection:
        tables = _tables(connection)
        lookups: list[tuple[str, str]] = []
        if "code_file_snapshot" in tables:
            columns = _columns(connection, "code_file_snapshot")
            if {"source_id", "relative_path"} <= columns:
                lookups.append(("code_file_snapshot", "relative_path"))
        if "artifact_registry" in tables:
            columns = _columns(connection, "artifact_registry")
            path_column = "logical_path" if "logical_path" in columns else "path" if "path" in columns else ""
            if "source_id" in columns and path_column:
                lookups.append(("artifact_registry", path_column))
        for table, path_column in lookups:
            safe_table = table.replace('"', '""')
            safe_column = path_column.replace('"', '""')
            if resolved.is_file():
                row = connection.execute(
                    f'SELECT 1 FROM "{safe_table}" WHERE source_id=? AND REPLACE("{safe_column}","\\\\","/")=? LIMIT 1',
                    (source_id, relative),
                ).fetchone()
            else:
                row = connection.execute(
                    f'SELECT 1 FROM "{safe_table}" WHERE source_id=? AND '
                    f'(REPLACE("{safe_column}","\\\\","/")=? OR '
                    f'REPLACE("{safe_column}","\\\\","/") LIKE ? ESCAPE "\\") LIMIT 1',
                    (source_id, relative, _like_prefix(relative)),
                ).fetchone()
            if row:
                return True
    return False


def validate_telemetry_open_target(
    workspace_dir: str | Path,
    brain_name: str,
    target: str | Path,
) -> dict[str, Any]:
    workspace, root = _brain_root(workspace_dir, brain_name)
    _verified_current_version(workspace, brain_name)
    raw = str(target or "").strip()
    if not raw:
        raise BrainTelemetryError("TELEMETRY_OPEN_TARGET_REQUIRED")
    resolved = Path(raw).expanduser().resolve()
    if not resolved.exists():
        raise BrainTelemetryError("TELEMETRY_OPEN_TARGET_NOT_FOUND")
    within_boundary = False
    for source in _indexed_open_boundaries(root):
        boundary = Path(source["root"])
        if boundary.is_file():
            inside = resolved == boundary
        else:
            try:
                resolved.relative_to(boundary)
                inside = True
            except ValueError:
                inside = False
        if not inside:
            continue
        within_boundary = True
        if not _target_is_indexed(source, resolved):
            continue
        return {
            "schema": TELEMETRY_SCHEMA,
            "status": "PASS",
            "target": str(resolved),
            "target_kind": "folder" if resolved.is_dir() else "file",
            "source_id": source["source_id"],
            "lane_id": source["lane_id"],
            "captured_source_boundary": str(boundary),
            "indexed_target": True,
        }
    if within_boundary:
        raise BrainTelemetryError("TELEMETRY_TARGET_NOT_INDEXED")
    raise BrainTelemetryError("TELEMETRY_TARGET_OUTSIDE_CAPTURED_SOURCE_BOUNDARY")


def _default_opener(target: Path) -> None:
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def _resolve_vscode_executable() -> Path | None:
    candidates: list[Path] = []
    for variable, suffix in (
        ("LOCALAPPDATA", Path("Programs/Microsoft VS Code/Code.exe")),
        ("PROGRAMFILES", Path("Microsoft VS Code/Code.exe")),
        ("PROGRAMFILES(X86)", Path("Microsoft VS Code/Code.exe")),
    ):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / suffix)
    for command in ("Code.exe", "code.exe", "code"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _vscode_opener(target: Path) -> None:
    executable = _resolve_vscode_executable()
    if executable is None:
        raise BrainTelemetryError("VSCODE_UNAVAILABLE")
    try:
        subprocess.Popen(
            [str(executable), "--reuse-window", str(target)],
            shell=False,
            close_fds=True,
        )
    except OSError as exc:
        raise BrainTelemetryError("VSCODE_UNAVAILABLE") from exc


def open_telemetry_target(
    workspace_dir: str | Path,
    brain_name: str,
    target: str | Path,
    *,
    application: str = "default",
    opener: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    validation = validate_telemetry_open_target(workspace_dir, brain_name, target)
    resolved = Path(validation["target"])
    requested_application = str(application or "default").strip().casefold()
    if requested_application not in {"default", "vscode"}:
        raise BrainTelemetryError("TELEMETRY_OPEN_APPLICATION_UNSUPPORTED")
    resolved_opener = opener or (_vscode_opener if requested_application == "vscode" else _default_opener)
    resolved_opener(resolved)
    return {
        **validation,
        "status": "OPENED",
        "application": requested_application,
        "opened_by_validated_backend_command": True,
    }


__all__ = [
    "BrainTelemetryError",
    "TELEMETRY_COMMANDS",
    "TELEMETRY_SCHEMA",
    "TELEMETRY_STATES",
    "get_telemetry_snapshot",
    "list_telemetry_deltas",
    "load_materialized_telemetry_bundle",
    "materialize_telemetry_graph_cache",
    "open_telemetry_target",
    "query_telemetry_graph",
    "telemetry_contract",
    "validate_telemetry_open_target",
]
