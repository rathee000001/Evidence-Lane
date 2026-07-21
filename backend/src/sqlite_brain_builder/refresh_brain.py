from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlite_brain_builder.brain_versions import (
    BrainVersionError,
    capture_brain_version,
    compare_current_brain_to_version,
    current_brain_snapshot,
    list_brain_versions,
    load_brain_version_record,
    materialize_brain_version_candidate,
)
from sqlite_brain_builder.codex_env15_package import create_codex_env15_package
from sqlite_brain_builder.codex_handoff import mark_current_passing_build_good
from sqlite_brain_builder.ingest.lane_contracts import contract_hash
from sqlite_brain_builder.runtime.canonical_lanes import UnknownLaneAliasError, resolve_lane_id
from sqlite_brain_builder.runtime.env15_ingestion import set_env15_source_activity
from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.package_validation import validate_chatgpt_package, validate_gemini_exact10
from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir, slugify_name
from sqlite_brain_builder.runtime.project_delta_ledger import (
    ProjectDeltaLedgerError,
    append_project_delta_event,
    get_project_delta,
    record_refresh_delta,
    sync_project_delta_folder,
)
from sqlite_brain_builder.runtime.refresh_delta_truth import (
    RefreshDeltaTruthError,
    build_refresh_overlay_truth,
    capture_refresh_source_state,
)
from sqlite_brain_builder.runtime.review_qualification import (
    ReviewQualificationError,
    create_review_qualification_packet,
    record_review_decision,
    verify_review_decision_receipt,
    verify_review_qualification_packet,
)
from sqlite_brain_builder.runtime.source_fingerprint_cache import CODE_SKIP_DIRS, cached_content_hash
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    LANE_DEFS,
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
    materialize_schema_only_package_topology,
    render_topology,
)


REFRESH_STATE_SCHEMA = "EVIDENCEOS_REFRESH_BRAIN_STATE_V1"
INPUT_CLASSIFICATIONS = {
    "UNCHANGED_REUSE",
    "CHANGED_REBUILD",
    "NEW_REGISTER",
    "REMOVED_TOMBSTONE",
    "UNSUPPORTED",
    "BLOCKED",
}
OPEN_STATE_CLASSIFICATIONS = {
    "LOCAL_WORKING_PROJECT_CHANGE",
    "USER_MANUAL_CHANGE",
    "UNRECONCILED_EXTERNAL_CHANGE",
    "POSSIBLE_CORRUPTION",
    "HASH_MISMATCH",
    "NO_CHANGE",
}
ACTIVE_CANDIDATE_STATES = {
    "VALIDATED",
    "REFRESHING",
    "TESTING",
    "REFRESHED_TEMP",
    "AWAITING_FUSE",
    "FAILED",
    "QUARANTINED",
}
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class RefreshBrainError(RuntimeError):
    pass


class RefreshBrainCancelled(RefreshBrainError):
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


def _rename_directory_with_retry(
    source: Path,
    target: Path,
    *,
    attempts: int = 12,
    delay_seconds: float = 0.5,
) -> None:
    """Rename a managed directory across transient Windows handle contention."""
    last_error: PermissionError | None = None
    for attempt in range(max(1, int(attempts))):
        try:
            source.rename(target)
            return
        except PermissionError as exc:
            last_error = exc
            if target.exists() or attempt + 1 >= attempts:
                break
            time.sleep(max(0.0, float(delay_seconds)))
    if last_error is not None:
        raise last_error
    raise RefreshBrainError(f"REFRESH_DIRECTORY_RENAME_FAILED:{source}:{target}")


def _state_directory(workspace: Path, brain_name: str, *, create: bool = True) -> Path:
    root = brain_output_dir(workspace, brain_name)
    path = root / "project" / "runtime" / "refresh_brain"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _current_state_path(workspace: Path, brain_name: str) -> Path:
    return _state_directory(workspace, brain_name) / "CURRENT_REFRESH.json"


def _history_state_path(workspace: Path, brain_name: str, candidate_id: str) -> Path:
    return _state_directory(workspace, brain_name) / "history" / f"{candidate_id}.json"


_OPERATION_RECEIPT_STATES = {
    "NO_CHANGE",
    "AWAITING_FUSE",
    "FAILED",
    "DROPPED",
    "QUARANTINED",
    "FUSED",
    "TEMP_UNFUSED",
}


def _operation_receipt_path(workspace: Path, brain_name: str, candidate_id: str) -> Path:
    return (
        brain_output_dir(workspace, brain_name)
        / "receipts"
        / "refresh_brain"
        / f"REFRESH_OPERATION_{candidate_id}.json"
    )


def _verified_operation_receipt(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    receipt_sha256 = str(payload.get("receipt_sha256") or "")
    canonical = dict(payload)
    canonical.pop("receipt_sha256", None)
    if not _SHA256.fullmatch(receipt_sha256) or _sha256_bytes(_canonical_bytes(canonical)) != receipt_sha256:
        return None
    return payload


def _persist_operation_receipt(
    workspace: Path,
    brain_name: str,
    state: Mapping[str, Any],
) -> tuple[Path, str]:
    candidate_id = str(state.get("candidate_id") or "")
    receipt_path = _operation_receipt_path(workspace, brain_name, candidate_id)
    existing = _verified_operation_receipt(receipt_path)
    if existing and str(existing.get("candidate_id") or "") != candidate_id:
        raise RefreshBrainError("REFRESH_OPERATION_RECEIPT_ID_MISMATCH")

    prior_receipt_sha256: str | None = None
    created_at = str(state.get("created_at") or _utc_now())
    if existing:
        prior_receipt_sha256 = existing.get("prior_receipt_sha256") or None
        created_at = str(existing.get("created_at") or created_at)
    else:
        verified_prior: list[tuple[str, str]] = []
        for prior_path in receipt_path.parent.glob("REFRESH_OPERATION_*.json") if receipt_path.parent.is_dir() else ():
            if prior_path == receipt_path:
                continue
            prior = _verified_operation_receipt(prior_path)
            if not prior or str(prior.get("candidate_id") or "") == candidate_id:
                continue
            verified_prior.append(
                (
                    str(prior.get("recorded_at") or prior.get("created_at") or prior_path.name),
                    str(prior["receipt_sha256"]),
                )
            )
        if verified_prior:
            prior_receipt_sha256 = max(verified_prior)[1]

    inventory = state.get("source_inventory") if isinstance(state.get("source_inventory"), Mapping) else {}
    lane_counts = inventory.get("lane_counts") if isinstance(inventory.get("lane_counts"), Mapping) else {}
    classifications = inventory.get("classifications") if isinstance(inventory.get("classifications"), list) else []
    baseline = state.get("baseline") if isinstance(state.get("baseline"), Mapping) else {}
    receipt: dict[str, Any] = {
        "receipt_type": "EVIDENCEOS_REFRESH_BRAIN_OPERATION",
        "candidate_id": candidate_id,
        "brain_id": str(state.get("brain_id") or ""),
        "brain_name": brain_name,
        "status": str(state.get("status") or "UNKNOWN"),
        "classification": str(state.get("classification") or ""),
        "lane_count": len(lane_counts),
        "source_scenario_count": len(classifications),
        "unchanged_sources_skipped": int(state.get("unchanged_sources_skipped") or 0),
        "new_immutable_snapshot": int(state.get("new_immutable_snapshot") or 0),
        "candidate_created": bool(state.get("candidate_created")),
        "verified_brain_unchanged": bool(state.get("verified_brain_unchanged")),
        "refresh_no_change": str(state.get("status") or "") == "NO_CHANGE",
        "project_delta_created": str(state.get("status") or "") not in {"NO_CHANGE", "FAILED", "QUARANTINED"},
        "overlay_state": (
            "REFRESH_NO_CHANGE"
            if str(state.get("status") or "") == "NO_CHANGE"
            else str(
                ((state.get("project_delta") or {}).get("overlay_state"))
                if isinstance(state.get("project_delta"), Mapping)
                else ((state.get("refresh_delta_truth") or {}).get("overlay_state"))
                if isinstance(state.get("refresh_delta_truth"), Mapping)
                else "REFRESH_DELTA_INCOMPLETE_OVERLAY_BLOCKED"
            )
        ),
        "refresh_source_state": (
            dict(state.get("refresh_source_state") or {})
            if isinstance(state.get("refresh_source_state"), Mapping)
            else None
        ),
        "retry_of": state.get("retry_of") or None,
        "baseline_snapshot_hash": str(baseline.get("snapshot_hash") or ""),
        "prior_receipt_sha256": prior_receipt_sha256,
        "created_at": created_at,
        "recorded_at": _utc_now(),
    }
    if state.get("exact_error"):
        receipt["exact_error"] = str(state["exact_error"])
    receipt_sha256 = _sha256_bytes(_canonical_bytes(receipt))
    receipt["receipt_sha256"] = receipt_sha256
    _write_json_atomic(receipt_path, receipt)
    return receipt_path, receipt_sha256


def _persist_state(workspace: Path, brain_name: str, state: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload["schema"] = REFRESH_STATE_SCHEMA
    payload["updated_at"] = _utc_now()
    candidate_id = str(payload.get("candidate_id") or "")
    if not candidate_id:
        raise RefreshBrainError("REFRESH_CANDIDATE_ID_REQUIRED")
    if str(payload.get("status") or "") in _OPERATION_RECEIPT_STATES:
        receipt_path, receipt_sha256 = _persist_operation_receipt(workspace, brain_name, payload)
        payload["operation_receipt"] = str(receipt_path)
        payload["operation_receipt_sha256"] = receipt_sha256
    _write_json_atomic(_current_state_path(workspace, brain_name), payload)
    _write_json_atomic(_history_state_path(workspace, brain_name, candidate_id), payload)
    return payload


def _load_state(workspace: Path, brain_name: str, candidate_id: str | None = None) -> dict[str, Any]:
    path = (
        _history_state_path(workspace, brain_name, candidate_id)
        if candidate_id
        else _current_state_path(workspace, brain_name)
    )
    if not path.is_file():
        return {
            "schema": REFRESH_STATE_SCHEMA,
            "status": "NOT_STARTED",
            "brain_name": brain_name,
            "candidate_id": candidate_id,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshBrainError("REFRESH_STATE_UNREADABLE") from exc
    if payload.get("schema") != REFRESH_STATE_SCHEMA:
        raise RefreshBrainError("REFRESH_STATE_SCHEMA_MISMATCH")
    return payload


def get_refresh_status(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    state = _load_state(workspace, brain_name, candidate_id)
    state_candidate_id = str(state.get("candidate_id") or "")
    if state_candidate_id:
        try:
            project_delta = get_project_delta(
                brain_output_dir(workspace, brain_name),
                candidate_id=state_candidate_id,
            )
        except ProjectDeltaLedgerError as exc:
            raise RefreshBrainError(f"REFRESH_PROJECT_DELTA_LEDGER_INVALID:{exc}") from exc
        if project_delta:
            state["project_delta"] = project_delta
    return state


def get_refresh_output(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    state = get_refresh_status(workspace_dir, brain_name, candidate_id=candidate_id)
    return {
        **state,
        "verified_brain_direct_overwrite": False,
        "automatic_fusion": False,
    }


def _latest_verified_baseline(workspace: Path, brain_name: str) -> dict[str, Any]:
    root = brain_output_dir(workspace, brain_name)
    semantic = root / "brain_versions" / "semantic_brain_diff.sqlite"
    good: dict[str, Any] | None = None
    if semantic.is_file():
        connection = sqlite3.connect(f"file:{semantic.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_good_snapshot'"
            ).fetchone()
            if exists:
                row = connection.execute(
                    "SELECT snapshot_id,version_id,snapshot_hash,test_status,build_status,created_at,reason "
                    "FROM brain_good_snapshot WHERE test_status='PASS' AND build_status='PASS' "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    good = dict(row)
        finally:
            connection.close()
    if good:
        record = load_brain_version_record(
            workspace,
            brain_name,
            str(good["version_id"]),
            verify_hashes=True,
        )
        if record.get("snapshot_hash") != good.get("snapshot_hash"):
            raise RefreshBrainError("LATEST_GOOD_SNAPSHOT_HASH_MISMATCH")
        return {
            **good,
            "baseline_status": "EXPLICIT_PASSING_GOOD_SNAPSHOT",
            "record_sha256": record["record_sha256"],
        }
    versions = list_brain_versions(workspace, brain_name, verify_hashes=True).get("versions") or []
    if not versions:
        raise RefreshBrainError("REFRESH_VERIFIED_BASELINE_REQUIRED")
    latest = versions[0]
    return {
        "snapshot_id": latest["version_id"],
        "version_id": latest["version_id"],
        "snapshot_hash": latest["snapshot_hash"],
        "test_status": "VALIDATED_VERSION_CAPTURE",
        "build_status": "VALIDATED_VERSION_CAPTURE",
        "created_at": latest["timestamp_utc"],
        "reason": latest["reason"],
        "baseline_status": "LATEST_VALIDATED_IMMUTABLE_BUILD_VERSION",
        "record_sha256": latest["record_sha256"],
    }


def _expected_snapshot_matches(expected: str, baseline: Mapping[str, Any]) -> bool:
    value = str(expected or "").strip().casefold()
    return bool(value) and value in {
        str(baseline.get("snapshot_id") or "").casefold(),
        str(baseline.get("version_id") or "").casefold(),
        str(baseline.get("snapshot_hash") or "").casefold(),
    }


def _fingerprint_cache_sources(brain_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = brain_root / "project" / "runtime" / "source_fingerprint_cache.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in entries.values():
        if not isinstance(raw, Mapping):
            continue
        source_id = str(raw.get("source_id") or "")
        content_sha256 = str(raw.get("content_sha256") or "")
        try:
            lane_id = resolve_lane_id(str(raw.get("lane_id") or ""), scope="any")
        except UnknownLaneAliasError:
            continue
        if not source_id or not _SHA256.fullmatch(content_sha256):
            continue
        rows[(lane_id, source_id)] = {
            "source_id": source_id,
            "lane_id": lane_id,
            "sha256": content_sha256.casefold(),
            "source_locator": str(raw.get("source_path") or ""),
            "availability_state": "AVAILABLE",
            "index_authority": "SOURCE_FINGERPRINT_CACHE",
        }
    return rows


def _indexed_sources(brain_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows = _fingerprint_cache_sources(brain_root)
    cached_by_source: dict[str, list[dict[str, Any]]] = {}
    for cached in rows.values():
        cached_by_source.setdefault(str(cached["source_id"]), []).append(cached)
    visited: set[Path] = set()
    for lane_id in LANE_DEFS:
        try:
            _sector_id, database = resolve_env15_sector(brain_root, lane_id)
        except Exception:
            continue
        database = database.resolve()
        if database in visited or not database.is_file():
            continue
        visited.add(database)
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_registry'"
            ).fetchone()
            if exists:
                columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(source_registry)")}
                wanted = [
                    name
                    for name in (
                        "source_id",
                        "lane_id",
                        "source_type",
                        "display_name",
                        "source_locator",
                        "path",
                        "canonical_path",
                        "sha256",
                        "source_hash",
                        "availability_state",
                    )
                    if name in columns
                ]
                if "source_id" in wanted:
                    for row in connection.execute(f"SELECT {','.join(wanted)} FROM source_registry"):
                        item = dict(row)
                        source_id = str(item.get("source_id") or "")
                        if not source_id:
                            continue
                        source_locator = str(
                            item.get("source_locator") or item.get("path") or item.get("canonical_path") or ""
                        )
                        source_hash = str(item.get("sha256") or item.get("source_hash") or "")
                        canonical_lane: str | None = None
                        for raw_lane in (item.get("lane_id"), item.get("source_type")):
                            if not str(raw_lane or "").strip():
                                continue
                            try:
                                canonical_lane = resolve_lane_id(str(raw_lane), scope="any")
                                break
                            except UnknownLaneAliasError:
                                continue
                        if canonical_lane is None:
                            candidates = cached_by_source.get(source_id, [])
                            matching = [
                                candidate
                                for candidate in candidates
                                if (
                                    source_hash
                                    and str(candidate.get("sha256") or "").casefold() == source_hash.casefold()
                                )
                                or (
                                    source_locator
                                    and str(candidate.get("source_locator") or "").casefold() == source_locator.casefold()
                                )
                            ]
                            selected = matching[0] if len(matching) == 1 else candidates[0] if len(candidates) == 1 else None
                            canonical_lane = str(selected["lane_id"]) if selected else lane_id
                        item["lane_id"] = canonical_lane
                        item["sha256"] = source_hash
                        item["source_locator"] = source_locator
                        item["availability_state"] = str(item.get("availability_state") or "AVAILABLE")
                        item["index_authority"] = "VERIFIED_SECTOR_SOURCE_REGISTRY"
                        if canonical_lane == "research" and any(
                            str(candidate.get("lane_id") or "") == "research"
                            and str(candidate.get("index_authority") or "") == "SOURCE_FINGERPRINT_CACHE"
                            and str(candidate.get("sha256") or "").casefold() == source_hash.casefold()
                            and str(candidate.get("source_locator") or "").casefold() == source_locator.casefold()
                            for candidate in rows.values()
                        ):
                            # Research stores immutable content-addressed source
                            # versions. The fingerprint cache retains the logical
                            # registered source id used by Refresh; do not count
                            # the matching version id as a second removed source.
                            continue
                        rows[(canonical_lane, source_id)] = item

            link_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_link_registry'"
            ).fetchone()
            if lane_id == "chat_lineage" and link_exists:
                link_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(file_link_registry)")}
                required = {"stable_file_id", "sha256"}
                if required.issubset(link_columns):
                    wanted_links = [
                        name
                        for name in (
                            "stable_file_id",
                            "external_display_uri",
                            "sha256",
                            "availability_state",
                            "direction",
                            "recorded_at",
                        )
                        if name in link_columns
                    ]
                    order = " ORDER BY recorded_at" if "recorded_at" in link_columns else ""
                    for row in connection.execute(f"SELECT {','.join(wanted_links)} FROM file_link_registry{order}"):
                        item = dict(row)
                        if str(item.get("direction") or "INPUT").upper() != "INPUT":
                            continue
                        source_id = str(item.get("stable_file_id") or "")
                        source_hash = str(item.get("sha256") or "")
                        if not source_id or not source_hash:
                            continue
                        rows[("chat_lineage", source_id)] = {
                            "source_id": source_id,
                            "lane_id": "chat_lineage",
                            "sha256": source_hash,
                            "source_locator": str(item.get("external_display_uri") or ""),
                            "availability_state": str(item.get("availability_state") or "AVAILABLE"),
                            "index_authority": "CHAT_LINEAGE_FILE_LINK_REGISTRY",
                        }
        finally:
            connection.close()
    return rows


def _inline_source_hash(source: Mapping[str, Any], lane_id: str) -> str:
    digest = hashlib.sha256(str(source.get("text") or "").encode("utf-8", "surrogatepass")).hexdigest()
    if lane_id == "custom":
        digest = hashlib.sha256(
            f"{digest}\x1f{contract_hash(str(source.get('schema_contract') or ''))}".encode("utf-8")
        ).hexdigest()
    return digest


def classify_registered_sources(
    brain_root: str | Path,
    sources: Sequence[Mapping[str, Any]],
    *,
    requested_lane_scope: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Classify registered sources without mutating the selected brain."""

    root = Path(brain_root).resolve()
    scope: set[str] | None = None
    if requested_lane_scope is not None:
        scope = set()
        for value in requested_lane_scope:
            try:
                scope.add(resolve_lane_id(str(value), scope="any"))
            except UnknownLaneAliasError as exc:
                raise RefreshBrainError(f"REFRESH_LANE_SCOPE_INVALID:{value}") from exc
    indexed = _indexed_sources(root)
    registered_keys: set[tuple[str, str]] = set()
    classifications: list[dict[str, Any]] = []
    normalized_sources: list[dict[str, Any]] = []
    for raw in sources:
        source = dict(raw)
        try:
            lane_id = resolve_lane_id(str(source.get("lane_key") or "custom"), scope="any")
        except UnknownLaneAliasError as exc:
            classifications.append(
                {
                    "source_id": str(source.get("source_id") or "unknown"),
                    "lane_id": str(source.get("lane_key") or "custom"),
                    "classification": "UNSUPPORTED",
                    "reason": "UNKNOWN_LANE_ALIAS",
                }
            )
            continue
        source["lane_key"] = lane_id
        source["lane_label"] = LANE_DEFS[lane_id]["label"]
        source_id = str(source.get("source_id") or "")
        if not source_id:
            classifications.append(
                {
                    "source_id": "unknown",
                    "lane_id": lane_id,
                    "classification": "BLOCKED",
                    "reason": "SOURCE_ID_REQUIRED",
                }
            )
            continue
        normalized_sources.append(source)
        key = (lane_id, source_id)
        registered_keys.add(key)
        if scope is not None and lane_id not in scope:
            continue
        existing = indexed.get(key)
        if not bool(source.get("active", True)):
            classification = "REMOVED_TOMBSTONE" if existing and existing["availability_state"] == "AVAILABLE" else "UNCHANGED_REUSE"
            classifications.append(
                {
                    "source_id": source_id,
                    "lane_id": lane_id,
                    "classification": classification,
                    "reason": "REGISTERED_SOURCE_INACTIVE",
                    "indexed_sha256": existing.get("sha256") if existing else None,
                }
            )
            continue
        path_text = str(source.get("path") or "").strip()
        text = str(source.get("text") or "")
        if not path_text and not text:
            classifications.append(
                {
                    "source_id": source_id,
                    "lane_id": lane_id,
                    "classification": "BLOCKED",
                    "reason": "SOURCE_PATH_OR_TEXT_REQUIRED",
                }
            )
            continue
        if path_text:
            path = Path(path_text).expanduser()
            if not path.exists():
                classifications.append(
                    {
                        "source_id": source_id,
                        "lane_id": lane_id,
                        "classification": "REMOVED_TOMBSTONE" if existing else "BLOCKED",
                        "reason": "REGISTERED_SOURCE_PATH_MISSING",
                        "path": str(path),
                        "indexed_sha256": existing.get("sha256") if existing else None,
                    }
                )
                continue
            if not existing or existing["availability_state"] != "AVAILABLE":
                classifications.append(
                    {
                        "source_id": source_id,
                        "lane_id": lane_id,
                        "classification": "NEW_REGISTER",
                        "reason": "SOURCE_NOT_IN_VERIFIED_SECTOR",
                        "path": str(path.resolve()),
                    }
                )
                continue
            try:
                cached_hash, probe = cached_content_hash(
                    root,
                    lane_id,
                    source_id,
                    path,
                    skip_dirs=CODE_SKIP_DIRS if lane_id in {"github_code", "local_code"} else (),
                )
            except OSError as exc:
                classifications.append(
                    {
                        "source_id": source_id,
                        "lane_id": lane_id,
                        "classification": "BLOCKED",
                        "reason": f"SOURCE_STAT_FAILED:{type(exc).__name__}",
                        "path": str(path),
                    }
                )
                continue
            classification = "UNCHANGED_REUSE" if cached_hash and cached_hash == existing["sha256"] else "CHANGED_REBUILD"
            classifications.append(
                {
                    "source_id": source_id,
                    "lane_id": lane_id,
                    "classification": classification,
                    "reason": "STABLE_FINGERPRINT_AND_CONTENT_HASH_MATCH" if classification == "UNCHANGED_REUSE" else "STAT_OR_CONTENT_RECHECK_REQUIRED",
                    "path": str(path.resolve()),
                    "indexed_sha256": existing["sha256"],
                    "current_cached_sha256": cached_hash,
                    "probe": probe,
                }
            )
            continue
        current_hash = _inline_source_hash(source, lane_id)
        classification = (
            "NEW_REGISTER"
            if not existing or existing["availability_state"] != "AVAILABLE"
            else "UNCHANGED_REUSE"
            if current_hash == existing["sha256"]
            else "CHANGED_REBUILD"
        )
        classifications.append(
            {
                "source_id": source_id,
                "lane_id": lane_id,
                "classification": classification,
                "reason": "INLINE_CONTENT_HASH_COMPARISON",
                "indexed_sha256": existing.get("sha256") if existing else None,
                "current_sha256": current_hash,
            }
        )

    for key, existing in sorted(indexed.items()):
        lane_id, source_id = key
        if scope is not None and lane_id not in scope:
            continue
        if key in registered_keys or existing["availability_state"] != "AVAILABLE":
            continue
        classifications.append(
            {
                "source_id": source_id,
                "lane_id": lane_id,
                "classification": "REMOVED_TOMBSTONE",
                "reason": "VERIFIED_SOURCE_NO_LONGER_REGISTERED",
                "indexed_sha256": existing.get("sha256"),
                "source_locator": existing.get("source_locator"),
            }
        )

    lane_counts: dict[str, dict[str, int]] = {}
    totals = {classification: 0 for classification in sorted(INPUT_CLASSIFICATIONS)}
    for item in classifications:
        lane_id = str(item["lane_id"])
        classification = str(item["classification"])
        lane_counts.setdefault(lane_id, {key: 0 for key in sorted(INPUT_CLASSIFICATIONS)})
        lane_counts[lane_id][classification] += 1
        totals[classification] += 1
    return {
        "classifications": classifications,
        "lane_counts": lane_counts,
        "totals": totals,
        "normalized_sources": normalized_sources,
        "requested_lane_scope": sorted(scope) if scope is not None else None,
        "verified_brain_mutated": False,
    }


def _copy_candidate_history_and_cache(canonical_root: Path, candidate_root: Path) -> None:
    history = canonical_root / "brain_versions"
    if history.is_dir():
        shutil.copytree(history, candidate_root / "brain_versions")
    cache = canonical_root / "project" / "runtime" / "source_fingerprint_cache.json"
    if cache.is_file():
        target = candidate_root / "project" / "runtime" / cache.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cache, target)
    operation_receipts = canonical_root / "receipts" / "refresh_brain"
    if operation_receipts.is_dir():
        shutil.copytree(operation_receipts, candidate_root / "receipts" / "refresh_brain")
    sync_project_delta_folder(canonical_root, candidate_root)


def _record_successful_refresh_delta(
    workspace: Path,
    brain_name: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if str(state.get("status") or "") == "NO_CHANGE":
        return {
            **dict(state),
            "project_delta": {
                "status": "REFRESH_NO_CHANGE",
                "created": False,
                "overlay_state": "REFRESH_NO_CHANGE",
                "color_mutation": False,
                "accepted_snapshot_preserved": True,
                "operation_receipt": str(state.get("operation_receipt") or ""),
                "operation_receipt_sha256": str(state.get("operation_receipt_sha256") or ""),
            },
        }
    inventory = state.get("source_inventory") if isinstance(state.get("source_inventory"), Mapping) else {}
    classifications = inventory.get("classifications") if isinstance(inventory.get("classifications"), list) else []
    diff = state.get("diff") if isinstance(state.get("diff"), Mapping) else {}
    changed_files = diff.get("changed_files") if isinstance(diff.get("changed_files"), list) else []
    baseline = state.get("baseline") if isinstance(state.get("baseline"), Mapping) else {}
    before_hash = str(baseline.get("snapshot_hash") or "")
    after_hash = str(state.get("candidate_snapshot_hash") or before_hash)
    truth = state.get("refresh_delta_truth") if isinstance(state.get("refresh_delta_truth"), Mapping) else {}
    source_state = state.get("refresh_source_state") if isinstance(state.get("refresh_source_state"), Mapping) else {}
    if not truth:
        raise RefreshBrainError("REFRESH_DELTA_TRUTH_REQUIRED")
    try:
        delta = record_refresh_delta(
            brain_output_dir(workspace, brain_name),
            brain_id=str(state.get("brain_id") or ""),
            brain_name=brain_name,
            candidate_id=str(state.get("candidate_id") or ""),
            refresh_status=str(state.get("status") or ""),
            classification=str(state.get("classification") or ""),
            before_snapshot_hash=before_hash,
            after_snapshot_hash=after_hash,
            refresh_receipt_path=str(state.get("operation_receipt") or ""),
            refresh_receipt_sha256=str(state.get("operation_receipt_sha256") or ""),
            source_classifications=classifications,
            changed_files=(truth.get("changed_files") if isinstance(truth.get("changed_files"), list) else changed_files),
            refresh_recorded_at=str(state.get("updated_at") or state.get("created_at") or _utc_now()),
            delta_type=str(truth.get("delta_type") or source_state.get("delta_type") or ""),
            parent_snapshot_id=str(baseline.get("version_id") or state.get("expected_snapshot_id") or ""),
            child_snapshot_id=f"candidate:{state.get('candidate_id')}:{after_hash}",
            source_state=source_state,
            node_changes=(truth.get("node_changes") if isinstance(truth.get("node_changes"), list) else []),
            edge_changes=(truth.get("edge_changes") if isinstance(truth.get("edge_changes"), list) else []),
            validation_results=(
                truth.get("validation_results")
                if isinstance(truth.get("validation_results"), list)
                else []
            ),
            relationship_comparison_complete=bool(truth.get("relationship_comparison_complete")),
        )
    except ProjectDeltaLedgerError as exc:
        raise RefreshBrainError(f"REFRESH_PROJECT_DELTA_RECORD_FAILED:{exc}") from exc
    return {**dict(state), "project_delta": delta}


def _review_rows(values: Any, *, field: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in values if isinstance(values, list) else []:
        rows.append(dict(value) if isinstance(value, Mapping) else {field: str(value)})
    return rows


def _create_candidate_review_qualification(
    candidate_root: Path,
    *,
    brain_name: str,
    brain_id: str,
    candidate_id: str,
    baseline: Mapping[str, Any],
    candidate_snapshot: Mapping[str, Any],
    inventory: Mapping[str, Any],
    diff: Mapping[str, Any],
    products: Mapping[str, Any],
) -> dict[str, Any]:
    packet_relative = Path("receipts") / "review_qualification" / f"REVIEW_QUALIFICATION_{candidate_id}.json"
    validations = products.get("validations") if isinstance(products.get("validations"), Mapping) else {}
    packet = create_review_qualification_packet(
        candidate_root / packet_relative,
        project_id=f"project:{slugify_name(brain_name)}",
        brain_id=brain_id,
        accepted_snapshot_id=str(baseline.get("version_id") or baseline.get("snapshot_hash") or ""),
        accepted_snapshot_hash=str(baseline.get("snapshot_hash") or ""),
        candidate_delta_id=f"REFRESH_BRAIN_DELTA_{candidate_id}",
        candidate_snapshot_hash=str(candidate_snapshot.get("snapshot_hash") or ""),
        task_id="T023_REFRESH_CANDIDATE_REVIEW_QUALIFICATION",
        run_id=candidate_id,
        queried_evidence=_review_rows(inventory.get("classifications"), field="source"),
        changed_files=_review_rows(diff.get("changed_files"), field="path"),
        changed_symbols=_review_rows(diff.get("changed_symbols"), field="symbol"),
        changed_routes=_review_rows(diff.get("changed_routes"), field="route"),
        tests=[
            {
                "kind": "candidate_validations",
                "status": str(validations.get("status") or "UNVERIFIED"),
                "sqlite_status": str((validations.get("sqlite") or {}).get("status") or "UNVERIFIED")
                if isinstance(validations.get("sqlite"), Mapping)
                else "UNVERIFIED",
            }
        ],
        untested_paths=[
            {"path": "successor_native_hil", "status": "PENDING"},
            {"path": "universal_refresh_local_working_project", "status": "COVERED_BY_REFRESH_TRUTH_TESTS"},
        ],
        risk=[
            {"risk": "candidate_not_human_accepted", "status": "OPEN"},
            {"risk": "accepted_brain_mutation_before_hil", "status": "BLOCKED_BY_DESIGN"},
        ],
        rollback={
            "version_id": str(baseline.get("version_id") or ""),
            "snapshot_hash": str(baseline.get("snapshot_hash") or ""),
            "action": "retain accepted brain and preserve candidate",
        },
        reviewer_type="HUMAN_OWNER_OR_AUTHORIZED_REVIEWER",
        next_pointer="HIL:APPROVE|REJECT|SUPERSEDE",
    )
    return {
        "schema": packet["schema"],
        "packet_id": packet["packet_id"],
        "packet_relative_path": packet_relative.as_posix(),
        "packet_sha256": packet["packet_sha256"],
        "content_sha256": packet["content_sha256"],
        "decision": "PENDING",
        "hil_state": "AWAITING_HUMAN_APPROVE_REJECT_OR_SUPERSEDE",
    }


def _validate_candidate_sqlite(candidate_root: Path) -> dict[str, Any]:
    results = []
    errors = []
    for database in sorted(candidate_root.rglob("*.sqlite"), key=lambda path: path.as_posix().casefold()):
        relative = database.relative_to(candidate_root).as_posix()
        if relative.startswith(("packages/", "brain_snapshots/", "brain_versions/", "project/runtime/")):
            continue
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        except sqlite3.Error as exc:
            integrity = []
            foreign_keys = []
            errors.append(f"SQLITE_OPEN_FAILED:{relative}:{type(exc).__name__}")
        finally:
            connection.close()
        if integrity != ["ok"]:
            errors.append(f"SQLITE_INTEGRITY_FAILED:{relative}")
        if foreign_keys:
            errors.append(f"SQLITE_FOREIGN_KEY_FAILED:{relative}")
        results.append({"path": relative, "integrity_check": integrity, "foreign_key_violations": foreign_keys})
    return {
        "status": "PASS" if not errors else "FAIL",
        "sqlite_count": len(results),
        "results": results,
        "errors": errors,
    }


_ENV15_CANDIDATE_RENDER_STATUS_BY_MMD = {
    "env_mmd.mmd": "SQLITE_DERIVED_ENV_MMD_TO_SVG_TO_PNG_PASS",
    "uop_mmd.mmd": "SQLITE_DERIVED_UOP_MMD_TO_SVG_TO_PNG_PASS",
    "project_master_topology.mmd": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS",
}


def _candidate_topology_render_passes(candidate_root: Path, rendered: Any) -> bool:
    if not isinstance(rendered, list) or not rendered:
        return False
    if (candidate_root / ".uepc_env").is_file():
        observed: set[str] = set()
        for item in rendered:
            if not isinstance(item, Mapping):
                return False
            name = Path(str(item.get("mmd") or "")).name
            expected = _ENV15_CANDIDATE_RENDER_STATUS_BY_MMD.get(name)
            if (
                expected is None
                or str(item.get("status") or "") != expected
                or not item.get("svg")
                or not item.get("png")
                or not Path(str(item["svg"])).is_file()
                or not Path(str(item["png"])).is_file()
            ):
                return False
            observed.add(name)
        return observed == set(_ENV15_CANDIDATE_RENDER_STATUS_BY_MMD)
    return all(
        isinstance(item, Mapping)
        and str(item.get("status") or "") == "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS"
        for item in rendered
    )


def _emit(progress: Callable[[dict[str, Any]], None] | None, stage: str, percent: int, command: str) -> None:
    if progress:
        progress({"stage": stage, "stage_percent": percent, "percent": percent, "active_command": command})


def _create_candidate_products(
    candidate_workspace: Path,
    selected_brain_name: str,
    candidate_sources: list[dict[str, Any]],
    *,
    baseline: Mapping[str, Any],
    candidate_id: str,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    _emit(progress, "candidate_build", 5, "incrementally rebuild changed and new registered sources")
    build = build_brain(
        str(candidate_workspace),
        selected_brain_name,
        candidate_sources,
        progress,
        None,
        True,
    )
    candidate_root = Path(str(build["brain_root"])).resolve()
    incremental = dict(build.get("incremental") or {})
    code_lanes_loaded = bool(incremental.get("code_lanes_loaded"))
    topology = {"status": "REUSED_OR_SKIPPED_NO_CODE_LANES"}
    chatgpt: dict[str, Any] = {"status": "SKIPPED_NO_CODE_LANES"}
    gemini: dict[str, Any] = {"status": "SKIPPED_NO_CODE_LANES"}
    chatgpt_validation: dict[str, Any] | None = None
    gemini_validation: dict[str, Any] | None = None
    chatgpt_chain_sha256 = ""
    gemini_chain_sha256 = ""
    gemini_source_chatgpt_sha256 = ""
    if code_lanes_loaded:
        _emit(progress, "candidate_topology", 45, "validate candidate topology and renders")
        topology = render_topology(str(candidate_workspace), selected_brain_name, progress, False)
        rendered = topology.get("rendered") if isinstance(topology, dict) else None
        if not _candidate_topology_render_passes(candidate_root, rendered):
            raise RefreshBrainError("REFRESH_CANDIDATE_TOPOLOGY_VALIDATION_FAILED")
    else:
        _emit(
            progress,
            "candidate_topology",
            45,
            "materialize governed Env15/project-sector package topology",
        )
        topology = materialize_schema_only_package_topology(candidate_root)
    _emit(progress, "candidate_packages", 60, "compile and validate candidate public-model packages")
    chatgpt = export_one_upload_package(str(candidate_workspace), selected_brain_name, progress)
    gemini = export_gemini_exact10(
        str(candidate_workspace),
        selected_brain_name,
        progress,
        chatgpt_package=chatgpt["package_zip"],
    )
    chatgpt_validation = validate_chatgpt_package(chatgpt["package_zip"])
    gemini_validation = validate_gemini_exact10(gemini["gemini_package_zip"])
    if chatgpt_validation["status"] != "PASS":
        raise RefreshBrainError("REFRESH_CHATGPT_PACKAGE_VALIDATION_FAILED")
    if gemini_validation["status"] != "PASS":
        raise RefreshBrainError("REFRESH_GEMINI_PACKAGE_VALIDATION_FAILED")
    chatgpt_sha256 = str(chatgpt_validation.get("sha256") or chatgpt.get("sha256") or "")
    gemini_source_sha256 = str(
        gemini_validation.get("source_chatgpt_package_sha256")
        or gemini.get("source_chatgpt_package_sha256")
        or ""
    )
    if not chatgpt_sha256 or gemini_source_sha256 != chatgpt_sha256:
        raise RefreshBrainError("REFRESH_GEMINI_CHATGPT_DERIVATION_HASH_MISMATCH")
    chatgpt_chain_sha256 = chatgpt_sha256
    gemini_chain_sha256 = str(gemini_validation.get("sha256") or gemini.get("sha256") or "")
    gemini_source_chatgpt_sha256 = gemini_source_sha256
    _emit(progress, "candidate_packages", 75, "create governed Env15-derived universal execution package")
    goal_pointer = {
        "parent_goal_id": f"EVIDENCEOS_REFRESH_BRAIN:{slugify_name(selected_brain_name)}",
        "active_run_id": candidate_id,
        "current_task_pointer": "REFRESH_BRAIN_CANDIDATE_RECONSTRUCTION",
        "active_lane_id": "refresh_brain",
        "brain_name": selected_brain_name,
    }
    delta_ledger = {
        "delta_id": "REFRESH_BRAIN_DELTA_" + candidate_id,
        "delta_title": "Refresh Brain candidate reconstruction",
        "status": "OPEN_REGISTERED",
        "prompt_pointer": "brain.refresh.start",
        "insertion_reason": "CHANGE_AWARE_CANDIDATE_RECONSTRUCTION",
        "insertion_point": "BEFORE_EXPLICIT_FUSE_HIL",
        "upstream_package_chain": {
            "chatgpt_local_ai": {
                "path": str(chatgpt.get("package_zip") or ""),
                "sha256": chatgpt_chain_sha256,
                "role": (
                    "SHARED_CHATGPT_LOCAL_AI_REFRESH_CANDIDATE_PACKAGE"
                    if code_lanes_loaded
                    else "SHARED_CHATGPT_LOCAL_AI_ENV15_OR_PROJECT_SECTOR_PACKAGE"
                ),
            },
            "gemini": {
                "path": str(gemini.get("gemini_package_zip") or ""),
                "sha256": gemini_chain_sha256,
                "role": "EXACT10_DERIVED_FROM_CHATGPT_LOCAL_AI",
                "source_chatgpt_package_sha256": gemini_source_chatgpt_sha256,
            },
        },
        "brain_diff_transport": "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE",
        "package_level": "CODEX_HIGHER_THAN_CHATGPT_LOCAL_AI_AND_GEMINI",
        "local_ai_package_transport": "CHATGPT_LOCAL_AI_SHARED_HEADLESS_PACKAGE",
    }
    codex = create_codex_env15_package(
        candidate_root,
        candidate_workspace / "portable_brain_workspaces" / slugify_name(selected_brain_name),
        candidate_root / "packages",
        brain_name=selected_brain_name,
        goal_pointer=goal_pointer,
        delta_ledger=delta_ledger,
        latest_good_snapshot=dict(baseline),
        snapshot_source_root=chatgpt.get("package_folder") or None,
        chatgpt_package=chatgpt.get("package_zip") or None,
        gemini_package=gemini.get("gemini_package_zip") or None,
    )
    codex_validation = codex.get("archive_validation") or codex.get("validation")
    if not isinstance(codex_validation, dict) or codex_validation.get("status") != "PASS":
        raise RefreshBrainError("REFRESH_UNIVERSAL_PACKAGE_VALIDATION_FAILED")
    sqlite_validation = _validate_candidate_sqlite(candidate_root)
    if sqlite_validation["status"] != "PASS":
        raise RefreshBrainError("REFRESH_CANDIDATE_SQLITE_VALIDATION_FAILED")
    _emit(progress, "candidate_validation", 90, "candidate integrity and package validation passed")
    return {
        "build": build,
        "topology": topology,
        "packages": {
            "chatgpt": chatgpt,
            "gemini": gemini,
            "codex_universal": codex,
        },
        "validations": {
            "status": "PASS",
            "sqlite": sqlite_validation,
            "chatgpt": chatgpt_validation,
            "gemini": gemini_validation,
            "codex_universal": codex_validation,
        },
    }


def _new_candidate_id(brain_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    entropy = hashlib.sha256(f"{brain_name}|{stamp}|{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:12]
    return f"refresh_{stamp}_{entropy}"


def _persist_candidate_copy(candidate_root: Path, state: Mapping[str, Any]) -> None:
    payload = dict(state)
    payload["schema"] = REFRESH_STATE_SCHEMA
    payload["updated_at"] = _utc_now()
    receipt_value = str(payload.get("operation_receipt") or "")
    if receipt_value:
        receipt_source = Path(receipt_value)
        if receipt_source.is_file():
            receipt_target = candidate_root / "receipts" / "refresh_brain" / receipt_source.name
            receipt_target.parent.mkdir(parents=True, exist_ok=True)
            if receipt_source.resolve() != receipt_target.resolve():
                shutil.copy2(receipt_source, receipt_target)
    directory = candidate_root / "project" / "runtime" / "refresh_brain"
    _write_json_atomic(directory / "CURRENT_REFRESH.json", payload)
    _write_json_atomic(directory / "history" / f"{payload['candidate_id']}.json", payload)


def _cancel_requested(workspace: Path, brain_name: str, candidate_id: str) -> bool:
    state = _load_state(workspace, brain_name)
    return state.get("candidate_id") == candidate_id and bool(state.get("cancel_requested"))


def start_refresh_brain(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    brain_id: str,
    expected_snapshot_id: str,
    sources: Sequence[Mapping[str, Any]],
    requested_lane_scope: Iterable[str] | None = None,
    known_execution_id: str | None = None,
    retry_of: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Build one isolated, hash-checked candidate; never fuse automatically."""

    workspace = normalize_workspace_dir(workspace_dir)
    canonical_root = brain_output_dir(workspace, brain_name)
    if not canonical_root.is_dir():
        raise RefreshBrainError("REFRESH_SELECTED_BRAIN_NOT_FOUND")
    if not str(brain_id or "").strip():
        raise RefreshBrainError("REFRESH_SELECTED_BRAIN_ID_REQUIRED")
    candidate_id = _new_candidate_id(brain_name)
    baseline = _latest_verified_baseline(workspace, brain_name)
    base_state: dict[str, Any] = {
        "schema": REFRESH_STATE_SCHEMA,
        "candidate_id": candidate_id,
        "brain_id": str(brain_id),
        "brain_name": brain_name,
        "canonical_brain_root": str(canonical_root),
        "baseline": baseline,
        "expected_snapshot_id": str(expected_snapshot_id),
        "requested_lane_scope": list(requested_lane_scope) if requested_lane_scope is not None else None,
        "retry_of": retry_of,
        "automatic_fusion": False,
        "hil_required": True,
        "verified_brain_direct_overwrite": False,
        "production_exe_compiled": False,
        "created_at": _utc_now(),
    }
    if not _expected_snapshot_matches(expected_snapshot_id, baseline):
        state = _persist_state(
            workspace,
            brain_name,
            {
                **base_state,
                "status": "QUARANTINED",
                "classification": "HASH_MISMATCH",
                "failure_class": "SNAPSHOT_MISMATCH",
                "exact_error": "REFRESH_EXPECTED_SNAPSHOT_MISMATCH",
                "candidate_created": False,
                "verified_brain_unchanged": True,
            },
        )
        raise RefreshBrainError(f"REFRESH_EXPECTED_SNAPSHOT_MISMATCH:{state['candidate_id']}")

    canonical_comparison = compare_current_brain_to_version(
        workspace,
        brain_name,
        str(baseline["version_id"]),
    )
    if canonical_comparison["status"] != "MATCH":
        suspicious = any(item.get("change_kind") == "REMOVED" for item in canonical_comparison["changed_files"])
        classification = "POSSIBLE_CORRUPTION" if suspicious else "UNRECONCILED_EXTERNAL_CHANGE"
        state = _persist_state(
            workspace,
            brain_name,
            {
                **base_state,
                "status": "QUARANTINED",
                "classification": classification,
                "delta_id": "UNRECONCILED_EXTERNAL_DELTA_" + candidate_id,
                "failure_class": "MUTATION_DETECTED",
                "exact_error": "REFRESH_CANONICAL_STATE_DIFFERS_FROM_VERIFIED_BASELINE",
                "canonical_comparison": canonical_comparison,
                "candidate_created": False,
                "user_decision_required": True,
                "verified_brain_unchanged": True,
            },
        )
        raise RefreshBrainError(f"REFRESH_CANONICAL_STATE_UNRECONCILED:{state['candidate_id']}")

    inventory = classify_registered_sources(
        canonical_root,
        sources,
        requested_lane_scope=requested_lane_scope,
    )
    blocked = [
        item for item in inventory["classifications"]
        if item["classification"] in {"BLOCKED", "UNSUPPORTED"}
    ]
    if blocked:
        state = _persist_state(
            workspace,
            brain_name,
            {
                **base_state,
                "status": "FAILED",
                "classification": "USER_MANUAL_CHANGE",
                "failure_class": "DELTA_CONFLICT",
                "exact_error": "REFRESH_REGISTERED_INPUTS_BLOCKED",
                "source_inventory": inventory,
                "candidate_created": False,
                "verified_brain_unchanged": True,
            },
        )
        raise RefreshBrainError(f"REFRESH_REGISTERED_INPUTS_BLOCKED:{state['candidate_id']}")

    changed_classes = {"CHANGED_REBUILD", "NEW_REGISTER", "REMOVED_TOMBSTONE"}
    has_registered_change = any(
        item["classification"] in changed_classes for item in inventory["classifications"]
    )
    classification = "LOCAL_WORKING_PROJECT_CHANGE" if has_registered_change else "NO_CHANGE"
    base_state["provenance"] = {
        "known_execution_id": str(known_execution_id or ""),
        "provider_identity_changes_refresh_truth": False,
        "project_change_authority": "UNIVERSAL_REFRESH_LOCAL_WORKING_PROJECT",
    }
    if classification not in OPEN_STATE_CLASSIFICATIONS:
        raise RefreshBrainError("REFRESH_OPEN_STATE_CLASSIFICATION_INVALID")

    try:
        refresh_source_state = capture_refresh_source_state(
            canonical_root,
            candidate_id=candidate_id,
            source_classifications=inventory["classifications"],
            cancel_check=lambda: _cancel_requested(workspace, brain_name, candidate_id),
        )
    except RefreshDeltaTruthError as exc:
        state = _persist_state(
            workspace,
            brain_name,
            {
                **base_state,
                "status": "FAILED",
                "classification": classification,
                "failure_class": "REFRESH_SOURCE_STATE_CAPTURE_FAILED",
                "exact_error": f"REFRESH_SOURCE_STATE_CAPTURE_FAILED:{exc}",
                "source_inventory": inventory,
                "candidate_created": False,
                "verified_brain_unchanged": True,
            },
        )
        raise RefreshBrainError(f"REFRESH_SOURCE_STATE_CAPTURE_FAILED:{state['candidate_id']}") from exc
    base_state["refresh_source_state"] = refresh_source_state

    if not has_registered_change:
        no_change_state = _persist_state(
            workspace,
            brain_name,
            {
                **base_state,
                "status": "NO_CHANGE",
                "classification": "NO_CHANGE",
                "source_inventory": inventory,
                "canonical_comparison": canonical_comparison,
                "candidate_created": False,
                "new_immutable_snapshot": 0,
                "unchanged_sources_skipped": inventory["totals"]["UNCHANGED_REUSE"],
                "verified_brain_unchanged": True,
            },
        )
        try:
            return _record_successful_refresh_delta(workspace, brain_name, no_change_state)
        except Exception as exc:
            _persist_state(
                workspace,
                brain_name,
                {
                    **no_change_state,
                    "status": "FAILED",
                    "failure_class": "DELTA_LEDGER_FAILURE",
                    "exact_error": f"PROJECT_DELTA_LEDGER_FAILED:{type(exc).__name__}:{exc}",
                    "verified_brain_unchanged": True,
                },
            )
            raise

    candidate_workspace = (
        workspace
        / ".evidenceos_refresh_candidates"
        / slugify_name(brain_name)
        / candidate_id
    )
    candidate_root = brain_output_dir(candidate_workspace, brain_name)
    state = {
        **base_state,
        "status": "REFRESHING",
        "classification": classification,
        "source_inventory": inventory,
        "canonical_comparison": canonical_comparison,
        "candidate_workspace": str(candidate_workspace),
        "candidate_root": str(candidate_root),
        "candidate_created": True,
        "new_immutable_snapshot": 0,
        "verified_brain_unchanged": True,
    }
    _persist_state(workspace, brain_name, state)
    _emit(progress, "candidate_materialization", 0, "materialize latest verified immutable version")
    try:
        materialization = materialize_brain_version_candidate(
            workspace,
            brain_name,
            str(baseline["version_id"]),
            candidate_root,
        )
        _copy_candidate_history_and_cache(canonical_root, candidate_root)
        candidate_sources = [dict(source) for source in inventory["normalized_sources"]]
        for item in inventory["classifications"]:
            if item["classification"] == "REMOVED_TOMBSTONE":
                if str(item["lane_id"]) == "research":
                    # Research is immutable append-only evidence. A Refresh may
                    # omit an earlier source, but it cannot rewrite its source
                    # registry or FTS rows through the generic mutation path.
                    continue
                set_env15_source_activity(
                    candidate_root,
                    str(item["lane_id"]),
                    str(item["source_id"]),
                    active=False,
                )
        changed_ids = {
            str(item["source_id"])
            for item in inventory["classifications"]
            if item["classification"] in {"CHANGED_REBUILD", "NEW_REGISTER"}
        }
        build_sources = [
            source for source in candidate_sources
            if str(source.get("source_id") or "") in changed_ids and bool(source.get("active", True))
        ]

        def guarded_progress(event: dict[str, Any]) -> None:
            if _cancel_requested(workspace, brain_name, candidate_id):
                raise RefreshBrainCancelled("REFRESH_CANCEL_REQUESTED")
            if progress:
                progress(event)

        state["status"] = "TESTING"
        state = _persist_state(workspace, brain_name, state)
        products = _create_candidate_products(
            candidate_workspace,
            brain_name,
            build_sources,
            baseline=baseline,
            candidate_id=candidate_id,
            progress=guarded_progress,
        )
        if _cancel_requested(workspace, brain_name, candidate_id):
            raise RefreshBrainCancelled("REFRESH_CANCEL_REQUESTED")
        candidate_snapshot = current_brain_snapshot(candidate_workspace, brain_name)
        diff = compare_current_brain_to_version(
            candidate_workspace,
            brain_name,
            str(baseline["version_id"]),
        )
        if diff["status"] == "MATCH":
            moved = []
            if candidate_workspace.exists():
                _send_to_recycle_bin(candidate_workspace)
                moved.append(str(candidate_workspace))
            no_change_state = _persist_state(
                workspace,
                brain_name,
                {
                    **state,
                    "status": "NO_CHANGE",
                    "classification": "NO_CHANGE",
                    "candidate_created": False,
                    "candidate_snapshot_hash": candidate_snapshot["snapshot_hash"],
                    "diff": diff,
                    "products": products,
                    "materialization": materialization,
                    "new_immutable_snapshot": 0,
                    "no_change_cleanup_moved": moved,
                    "verified_brain_unchanged": True,
                },
            )
            return _record_successful_refresh_delta(workspace, brain_name, no_change_state)
        refresh_delta_truth = build_refresh_overlay_truth(
            canonical_root,
            candidate_root,
            candidate_id=candidate_id,
            source_state=refresh_source_state,
            products=products,
            cancel_check=lambda: _cancel_requested(workspace, brain_name, candidate_id),
        )
        review_qualification = _create_candidate_review_qualification(
            candidate_root,
            brain_name=brain_name,
            brain_id=str(brain_id),
            candidate_id=candidate_id,
            baseline=baseline,
            candidate_snapshot=candidate_snapshot,
            inventory=inventory,
            diff=diff,
            products=products,
        )
        final_state = _persist_state(
            workspace,
            brain_name,
            {
                **state,
                "status": "AWAITING_FUSE",
                "candidate_snapshot_hash": candidate_snapshot["snapshot_hash"],
                "candidate_snapshot_file_count": candidate_snapshot["file_count"],
                "diff": {
                    **diff,
                    "semantic_classification": classification,
                    "source_classifications": inventory["classifications"],
                },
                "products": products,
                "materialization": materialization,
                "accepted_sources": candidate_sources,
                "tests_status": str((products.get("validations") or {}).get("status") or "UNVERIFIED"),
                "review_qualification": review_qualification,
                "refresh_delta_truth": refresh_delta_truth,
                "hil_state": "AWAITING_HUMAN_APPROVE_REJECT_OR_SUPERSEDE",
                "automatic_fusion": False,
                "new_immutable_snapshot": 0,
                "verified_brain_unchanged": True,
            },
        )
        final_state = _record_successful_refresh_delta(workspace, brain_name, final_state)
        final_state["project_delta_sync"] = sync_project_delta_folder(canonical_root, candidate_root)
        _persist_candidate_copy(candidate_root, final_state)
        _emit(progress, "hil_wait", 100, "candidate validated and awaiting APPROVE, REJECT, or SUPERSEDE")
        return final_state
    except RefreshBrainCancelled as exc:
        moved = []
        for path in (candidate_workspace,):
            if path and path.exists():
                _send_to_recycle_bin(path)
                moved.append(str(path))
        return _persist_state(
            workspace,
            brain_name,
            {
                **state,
                "status": "DROPPED",
                "classification": classification,
                "exact_error": str(exc),
                "drop_reason": "EXPLICIT_CANCEL",
                "recycle_bin_paths": moved,
                "candidate_created": False,
                "verified_brain_unchanged": True,
            },
        )
    except Exception as exc:
        failed_state = _persist_state(
            workspace,
            brain_name,
            {
                **state,
                "status": "FAILED",
                "classification": classification,
                "failure_class": "UNKNOWN_FAILURE",
                "exact_error": f"{type(exc).__name__}: {exc}",
                "candidate_preserved_for_diagnosis": candidate_root.exists(),
                "verified_brain_unchanged": True,
                "rollback_available": True,
                "next_action_code": "RETRY_OR_DROP_CANDIDATE",
            },
        )
        if candidate_root.exists():
            _persist_candidate_copy(candidate_root, failed_state)
        raise


def _managed_path(workspace: Path, value: str | Path, expected_root_name: str) -> Path:
    path = Path(value).resolve()
    expected = (workspace / expected_root_name).resolve()
    try:
        path.relative_to(expected)
    except ValueError as exc:
        raise RefreshBrainError("REFRESH_MANAGED_PATH_OUTSIDE_AUTHORITY") from exc
    return path


def _send_to_recycle_bin(path: str | Path) -> None:
    target = Path(path).resolve()
    if not target.exists():
        return
    if os.name != "nt":
        try:
            from send2trash import send2trash
        except ImportError as exc:  # pragma: no cover - Windows is the product target
            raise RefreshBrainError("RECYCLE_BIN_INTEGRATION_UNAVAILABLE") from exc
        send2trash(str(target))
        return

    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(target) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise RefreshBrainError(f"RECYCLE_BIN_MOVE_FAILED:{result}")


def drop_refresh_candidate(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    state = _load_state(workspace, brain_name, candidate_id)
    if state.get("candidate_id") != candidate_id:
        raise RefreshBrainError("REFRESH_CANDIDATE_NOT_FOUND")
    if state.get("status") == "FUSED":
        raise RefreshBrainError("FUSED_REFRESH_CANDIDATE_CANNOT_BE_DROPPED")
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise RefreshBrainError("REFRESH_DROP_ACTOR_AND_REASON_REQUIRED")
    moved = []
    candidate_workspace_value = state.get("candidate_workspace")
    if candidate_workspace_value:
        candidate_workspace = _managed_path(
            workspace,
            str(candidate_workspace_value),
            ".evidenceos_refresh_candidates",
        )
        if candidate_workspace.exists():
            _send_to_recycle_bin(candidate_workspace)
            moved.append(str(candidate_workspace))
    dropped = _persist_state(
        workspace,
        brain_name,
        {
            **state,
            "status": "DROPPED",
            "drop_actor": str(actor),
            "drop_reason": str(reason),
            "dropped_at": _utc_now(),
            "recycle_bin_paths": moved,
            "candidate_created": False,
            "verified_brain_unchanged": True,
            "original_user_sources_deleted": False,
            "permanent_deletion_used": False,
            "historical_provider_evidence_preserved": True,
        },
    )
    delta = get_project_delta(
        brain_output_dir(workspace, brain_name),
        candidate_id=candidate_id,
    )
    if delta:
        dropped["project_delta_event"] = append_project_delta_event(
            brain_output_dir(workspace, brain_name),
            candidate_id=candidate_id,
            event_type="DROPPED",
            actor=str(actor),
            reason=str(reason),
            receipt_path=str(dropped.get("operation_receipt") or ""),
            receipt_sha256=str(dropped.get("operation_receipt_sha256") or ""),
            details={"recycle_bin_paths": moved, "permanent_deletion_used": False},
        )
    return dropped


def cancel_refresh_brain(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    state = _load_state(workspace, brain_name, candidate_id)
    if state.get("candidate_id") != candidate_id:
        raise RefreshBrainError("REFRESH_CANDIDATE_NOT_FOUND")
    if state.get("status") == "AWAITING_FUSE":
        return drop_refresh_candidate(
            workspace,
            brain_name,
            candidate_id=candidate_id,
            actor=actor,
            reason=reason or "explicit refresh cancellation",
        )
    if state.get("status") not in {"VALIDATED", "REFRESHING", "TESTING", "REFRESHED_TEMP"}:
        return state
    return _persist_state(
        workspace,
        brain_name,
        {
            **state,
            "cancel_requested": True,
            "cancel_requested_by": str(actor or "user"),
            "cancel_reason": str(reason or "explicit refresh cancellation"),
            "cancel_requested_at": _utc_now(),
        },
    )


def retry_refresh_brain(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str,
    brain_id: str,
    expected_snapshot_id: str,
    sources: Sequence[Mapping[str, Any]],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    prior = _load_state(workspace, brain_name, candidate_id)
    if prior.get("status") not in {"FAILED", "DROPPED", "QUARANTINED", "TEMP_UNFUSED"}:
        raise RefreshBrainError("REFRESH_RETRY_REQUIRES_FAILED_DROPPED_OR_QUARANTINED_STATE")
    if isinstance(prior.get("imported_return"), dict):
        raise RefreshBrainError(
            "HISTORICAL_PROVIDER_DELTA_RETRY_FORBIDDEN_USE_UNIVERSAL_REFRESH"
        )
    return start_refresh_brain(
        workspace,
        brain_name,
        brain_id=brain_id,
        expected_snapshot_id=expected_snapshot_id,
        sources=sources,
        requested_lane_scope=prior.get("requested_lane_scope"),
        retry_of=candidate_id,
        progress=progress,
    )


def _verified_candidate_review_authority(
    state: Mapping[str, Any],
    candidate_root: Path,
) -> tuple[Path, dict[str, Any]]:
    review = state.get("review_qualification")
    if not isinstance(review, Mapping):
        raise RefreshBrainError("REFRESH_FUSE_HIL_APPROVAL_REQUIRED")
    relative_value = str(review.get("packet_relative_path") or "")
    relative = Path(relative_value.replace("\\", "/"))
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:2] != ("receipts", "review_qualification")
    ):
        raise RefreshBrainError("REFRESH_REVIEW_PACKET_PATH_INVALID")
    packet_path = (candidate_root / relative).resolve()
    try:
        packet_path.relative_to(candidate_root.resolve())
        packet = verify_review_qualification_packet(packet_path)
    except (ValueError, ReviewQualificationError) as exc:
        raise RefreshBrainError(f"REFRESH_REVIEW_PACKET_INVALID:{exc}") from exc
    candidate_id = str(state.get("candidate_id") or "")
    expected_delta_id = f"REFRESH_BRAIN_DELTA_{candidate_id}"
    if str(packet.get("candidate_delta_id") or "") != expected_delta_id:
        raise RefreshBrainError("REFRESH_REVIEW_PACKET_CANDIDATE_MISMATCH")
    if str(packet.get("candidate_snapshot_hash") or "") != str(state.get("candidate_snapshot_hash") or ""):
        raise RefreshBrainError("REFRESH_REVIEW_PACKET_SNAPSHOT_MISMATCH")
    if str(review.get("packet_sha256") or "").upper() != str(packet.get("packet_sha256") or "").upper():
        raise RefreshBrainError("REFRESH_REVIEW_PACKET_FILE_HASH_MISMATCH")
    return packet_path, packet


def record_refresh_hil_decision(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str,
    decision: str,
    actor: str,
    reviewer_type: str,
    reason: str,
) -> dict[str, Any]:
    """Record one human decision without fusing or mutating accepted brain data."""

    workspace = normalize_workspace_dir(workspace_dir)
    state = _load_state(workspace, brain_name, candidate_id)
    if state.get("candidate_id") != candidate_id:
        raise RefreshBrainError("REFRESH_CANDIDATE_NOT_FOUND")
    normalized_decision = str(decision or "").strip().upper()
    existing_decision = state.get("hil_decision")
    if isinstance(existing_decision, Mapping):
        if str(existing_decision.get("decision") or "") != normalized_decision:
            raise RefreshBrainError("REFRESH_HIL_DECISION_ALREADY_RECORDED_DIFFERENTLY")
        return state
    if state.get("status") != "AWAITING_FUSE":
        raise RefreshBrainError("REFRESH_HIL_DECISION_REQUIRES_AWAITING_FUSE")
    if not str(actor or "").strip() or not str(reviewer_type or "").strip() or not str(reason or "").strip():
        raise RefreshBrainError("REFRESH_HIL_ACTOR_REVIEWER_AND_REASON_REQUIRED")

    candidate_workspace = _managed_path(
        workspace,
        str(state.get("candidate_workspace") or ""),
        ".evidenceos_refresh_candidates",
    )
    candidate_root = Path(str(state.get("candidate_root") or "")).resolve()
    try:
        candidate_root.relative_to(candidate_workspace)
    except ValueError as exc:
        raise RefreshBrainError("REFRESH_CANDIDATE_ROOT_OUTSIDE_WORKSPACE") from exc
    if not candidate_root.is_dir():
        raise RefreshBrainError("REFRESH_CANDIDATE_ROOT_MISSING")
    packet_path, packet = _verified_candidate_review_authority(state, candidate_root)
    receipt_relative = (
        Path("receipts")
        / "review_qualification"
        / f"REVIEW_DECISION_{candidate_id}.json"
    )
    try:
        decision_receipt = record_review_decision(
            packet_path,
            candidate_root / receipt_relative,
            decision=normalized_decision,
            actor=str(actor),
            reviewer_type=str(reviewer_type),
            reason=str(reason),
        )
    except ReviewQualificationError as exc:
        raise RefreshBrainError(f"REFRESH_HIL_DECISION_INVALID:{exc}") from exc

    next_status = {
        "APPROVE": "AWAITING_FUSE",
        "REJECT": "REJECTED",
        "SUPERSEDE": "SUPERSEDED",
    }[normalized_decision]
    hil_state = {
        "APPROVE": "HIL_APPROVED_FUSE_REQUIRES_EXPLICIT_CLICK",
        "REJECT": "HIL_REJECTED_ACCEPTED_BRAIN_PRESERVED",
        "SUPERSEDE": "HIL_SUPERSEDED_ACCEPTED_BRAIN_PRESERVED",
    }[normalized_decision]
    next_action = {
        "APPROVE": "EXPLICIT_FUSE_AVAILABLE",
        "REJECT": "PRESERVE_ACCEPTED_STATE",
        "SUPERSEDE": "REGISTER_SUCCESSOR_CANDIDATE",
    }[normalized_decision]
    decision_summary = {
        "decision": normalized_decision,
        "decision_id": decision_receipt["decision_id"],
        "receipt_relative_path": receipt_relative.as_posix(),
        "receipt_sha256": decision_receipt["receipt_sha256"],
        "review_packet_id": packet["packet_id"],
        "review_packet_sha256": packet["packet_sha256"],
        "actor": str(actor),
        "reviewer_type": str(reviewer_type),
        "reason": str(reason),
        "automatic_fuse": False,
    }
    decided = {
        **state,
        "status": next_status,
        "hil_decision": decision_summary,
        "hil_state": hil_state,
        "next_action_code": next_action,
        "verified_brain_unchanged": True,
        "automatic_fusion": False,
    }
    canonical_root = brain_output_dir(workspace, brain_name).resolve()
    delta = get_project_delta(canonical_root, candidate_id=candidate_id)
    if delta:
        decided["project_delta_hil_event"] = append_project_delta_event(
            canonical_root,
            candidate_id=candidate_id,
            event_type=f"HIL_{normalized_decision}",
            actor=str(actor),
            reason=str(reason),
            receipt_path=str(decision_receipt["receipt_path"]),
            receipt_sha256=str(decision_receipt["receipt_sha256"]),
            details={
                "review_packet_id": packet["packet_id"],
                "review_packet_sha256": packet["packet_sha256"],
                "automatic_fuse": False,
                "accepted_brain_mutated": False,
            },
        )
        decided["project_delta_sync"] = sync_project_delta_folder(canonical_root, candidate_root)
    decided = _persist_state(workspace, brain_name, decided)
    _persist_candidate_copy(candidate_root, decided)
    return decided


def fuse_refresh_candidate(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str,
    actor: str,
    reason: str,
    source_commit: Callable[[list[dict[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Promote a validated candidate only after an explicit caller decision."""

    workspace = normalize_workspace_dir(workspace_dir)
    state = _load_state(workspace, brain_name, candidate_id)
    if state.get("candidate_id") != candidate_id:
        raise RefreshBrainError("REFRESH_CANDIDATE_NOT_FOUND")
    resume_failed_fuse = (
        state.get("status") == "FAILED"
        and state.get("verified_brain_unchanged") is True
        and state.get("candidate_preserved_for_diagnosis") is True
        and state.get("rollback_available") is True
        and str(state.get("exact_error") or "").startswith("FUSE_FAILED:")
    )
    if state.get("status") != "AWAITING_FUSE" and not resume_failed_fuse:
        raise RefreshBrainError("REFRESH_CANDIDATE_NOT_AWAITING_FUSE")
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise RefreshBrainError("REFRESH_FUSE_ACTOR_AND_REASON_REQUIRED")
    products = state.get("products") or {}
    validations = products.get("validations") if isinstance(products, dict) else None
    if not isinstance(validations, dict) or validations.get("status") != "PASS":
        raise RefreshBrainError("REFRESH_FUSE_VALIDATION_PASS_REQUIRED")
    canonical_root = brain_output_dir(workspace, brain_name).resolve()
    candidate_workspace = _managed_path(
        workspace,
        str(state.get("candidate_workspace") or ""),
        ".evidenceos_refresh_candidates",
    )
    candidate_root = Path(str(state.get("candidate_root") or "")).resolve()
    try:
        candidate_root.relative_to(candidate_workspace)
    except ValueError as exc:
        raise RefreshBrainError("REFRESH_CANDIDATE_ROOT_OUTSIDE_WORKSPACE") from exc
    if not candidate_root.is_dir():
        raise RefreshBrainError("REFRESH_CANDIDATE_ROOT_MISSING")
    packet_path, review_packet = _verified_candidate_review_authority(state, candidate_root)
    hil_decision = state.get("hil_decision")
    if not isinstance(hil_decision, Mapping) or str(hil_decision.get("decision") or "") != "APPROVE":
        raise RefreshBrainError("REFRESH_FUSE_HIL_APPROVAL_REQUIRED")
    decision_relative_value = str(hil_decision.get("receipt_relative_path") or "")
    decision_relative = Path(decision_relative_value.replace("\\", "/"))
    if (
        not decision_relative.parts
        or decision_relative.is_absolute()
        or ".." in decision_relative.parts
        or decision_relative.parts[:2] != ("receipts", "review_qualification")
    ):
        raise RefreshBrainError("REFRESH_HIL_DECISION_PATH_INVALID")
    decision_path = (candidate_root / decision_relative).resolve()
    try:
        decision_path.relative_to(candidate_root)
        decision_receipt = verify_review_decision_receipt(
            decision_path,
            expected_packet_sha256=str(review_packet["packet_sha256"]),
        )
    except (ValueError, ReviewQualificationError) as exc:
        raise RefreshBrainError(f"REFRESH_FUSE_HIL_APPROVAL_INVALID:{exc}") from exc
    if (
        str(decision_receipt.get("decision") or "") != "APPROVE"
        or str(decision_receipt.get("candidate_delta_id") or "")
        != str(review_packet.get("candidate_delta_id") or "")
        or str(hil_decision.get("receipt_sha256") or "").upper()
        != str(decision_receipt.get("receipt_sha256") or "").upper()
    ):
        raise RefreshBrainError("REFRESH_FUSE_HIL_APPROVAL_REQUIRED")
    baseline = dict(state.get("baseline") or {})
    canonical_comparison = compare_current_brain_to_version(
        workspace,
        brain_name,
        str(baseline.get("version_id") or ""),
    )
    if canonical_comparison["status"] != "MATCH":
        raise RefreshBrainError("REFRESH_FUSE_CANONICAL_BASELINE_CHANGED")
    candidate_snapshot = current_brain_snapshot(candidate_workspace, brain_name)
    if candidate_snapshot["snapshot_hash"] != state.get("candidate_snapshot_hash"):
        raise RefreshBrainError("REFRESH_FUSE_CANDIDATE_HASH_MISMATCH")

    archive_root = (
        workspace
        / ".evidenceos_verified_history"
        / slugify_name(brain_name)
        / f"{baseline.get('version_id', 'baseline')}_{candidate_id}"
    ).resolve()
    if archive_root.exists():
        raise RefreshBrainError("REFRESH_FUSE_ARCHIVE_ALREADY_EXISTS")
    archive_root.parent.mkdir(parents=True, exist_ok=True)
    fusing = {
        **state,
        "status": "FUSING",
        "fuse_actor": str(actor),
        "fuse_reason": str(reason),
        "fuse_started_at": _utc_now(),
        "prior_verified_archive": str(archive_root),
        "fuse_resume_of_failed_attempt": resume_failed_fuse,
        "review_packet_sha256": review_packet["packet_sha256"],
        "review_decision_receipt_sha256": decision_receipt["receipt_sha256"],
    }
    _persist_state(workspace, brain_name, fusing)
    _persist_candidate_copy(candidate_root, fusing)
    promoted = False
    try:
        _rename_directory_with_retry(canonical_root, archive_root)
        try:
            _rename_directory_with_retry(candidate_root, canonical_root)
            promoted = True
        except Exception:
            _rename_directory_with_retry(archive_root, canonical_root)
            raise
        version = capture_brain_version(
            workspace,
            brain_name,
            actor_type="app",
            actor_name=str(actor),
            reason=str(reason),
            change_summary=f"Refresh Brain fused candidate {candidate_id}",
        )
        if version["snapshot_hash"] != state.get("candidate_snapshot_hash"):
            raise RefreshBrainError("REFRESH_FUSED_VERSION_HASH_MISMATCH")
        package_products = products.get("packages") if isinstance(products, dict) else {}
        chatgpt_product = package_products.get("chatgpt") if isinstance(package_products, dict) else None
        gemini_product = package_products.get("gemini") if isinstance(package_products, dict) else None
        has_public_packages = (
            isinstance(chatgpt_product, dict)
            and bool(chatgpt_product.get("package_zip"))
            and isinstance(gemini_product, dict)
            and bool(gemini_product.get("gemini_package_zip"))
        )
        good_snapshot = None
        if has_public_packages:
            good_snapshot = mark_current_passing_build_good(
                workspace,
                brain_name,
                test_status="PASS",
                build_status="PASS",
                reason=f"explicit Refresh Brain Fuse by {actor}: {reason}",
            )
        accepted_sources = [dict(source) for source in state.get("accepted_sources") or []]
        if source_commit:
            source_commit(accepted_sources)
        receipt = {
            "receipt_id": "REFRESH_FUSION_" + candidate_id,
            "status": "PASS_FUSED_AS_NEW_IMMUTABLE_VERSION",
            "candidate_id": candidate_id,
            "brain_name": brain_name,
            "actor": str(actor),
            "reason": str(reason),
            "prior_version_id": baseline.get("version_id"),
            "prior_snapshot_hash": baseline.get("snapshot_hash"),
            "new_version_id": version["version_id"],
            "new_snapshot_hash": version["snapshot_hash"],
            "prior_verified_archive": str(archive_root),
            "prior_verified_version_preserved": True,
            "original_user_sources_deleted": False,
            "automatic_fusion": False,
            "review_packet_id": review_packet["packet_id"],
            "review_packet_sha256": review_packet["packet_sha256"],
            "review_decision_id": decision_receipt["decision_id"],
            "review_decision_receipt_sha256": decision_receipt["receipt_sha256"],
            "fused_at": _utc_now(),
        }
        receipt_path = canonical_root / "receipts" / f"REFRESH_FUSION_{candidate_id}.json"
        _write_json_atomic(receipt_path, receipt)
        delta_event = append_project_delta_event(
            canonical_root,
            candidate_id=candidate_id,
            event_type="FUSED",
            actor=str(actor),
            reason=str(reason),
            receipt_path=str(receipt_path),
            receipt_sha256=_sha256_file(receipt_path),
            details={
                "prior_version_id": baseline.get("version_id"),
                "prior_snapshot_hash": baseline.get("snapshot_hash"),
                "new_version_id": version["version_id"],
                "new_snapshot_hash": version["snapshot_hash"],
            },
        )
        fused = _persist_state(
            workspace,
            brain_name,
            {
                **fusing,
                "status": "FUSED",
                "fused_at": receipt["fused_at"],
                "immutable_version": version,
                "good_snapshot": good_snapshot,
                "fusion_receipt": str(receipt_path),
                "project_delta_event": delta_event,
                "new_immutable_snapshot": 1,
                "verified_brain_unchanged": False,
                "prior_verified_version_preserved": True,
                "hil_state": "EXPLICIT_FUSE_RECORDED",
            },
        )
        return fused
    except Exception as exc:
        if promoted and canonical_root.exists() and archive_root.exists():
            candidate_root.parent.mkdir(parents=True, exist_ok=True)
            _rename_directory_with_retry(canonical_root, candidate_root)
            _rename_directory_with_retry(archive_root, canonical_root)
        filesystem_blocked = (
            isinstance(exc, PermissionError)
            and canonical_root.exists()
            and not archive_root.exists()
        )
        failed = _persist_state(
            workspace,
            brain_name,
            {
                **state,
                "status": "FAILED",
                "failure_class": (
                    "FUSION_FILESYSTEM_BLOCKED"
                    if filesystem_blocked
                    else "ROLLBACK_FAILED" if not canonical_root.exists() else "MUTATION_DETECTED"
                ),
                "exact_error": f"FUSE_FAILED:{type(exc).__name__}:{exc}",
                "verified_brain_unchanged": canonical_root.exists(),
                "candidate_preserved_for_diagnosis": candidate_root.exists(),
                "rollback_available": True,
                "next_action_code": "INSPECT_FUSION_FAILURE_AND_RETRY_OR_DROP",
            },
        )
        if candidate_root.exists():
            _persist_candidate_copy(candidate_root, failed)
        raise


def cleanup_unfused_refresh_candidate(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    candidate_id: str | None = None,
    reason: str = "application exit cleanup rule",
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    state = _load_state(workspace, brain_name, candidate_id)
    if state.get("status") not in ACTIVE_CANDIDATE_STATES:
        return state
    dropped = drop_refresh_candidate(
        workspace,
        brain_name,
        candidate_id=str(state["candidate_id"]),
        actor="Evidence OS application exit",
        reason=reason,
    )
    return _persist_state(
        workspace,
        brain_name,
        {
            **dropped,
            "status": "TEMP_UNFUSED",
            "temp_unfused_at": _utc_now(),
            "audit_receipt_preserved": True,
            "verified_brain_unchanged": True,
        },
    )


__all__ = [
    "INPUT_CLASSIFICATIONS",
    "OPEN_STATE_CLASSIFICATIONS",
    "RefreshBrainError",
    "cancel_refresh_brain",
    "classify_registered_sources",
    "cleanup_unfused_refresh_candidate",
    "drop_refresh_candidate",
    "fuse_refresh_candidate",
    "get_refresh_output",
    "get_refresh_status",
    "record_refresh_hil_decision",
    "retry_refresh_brain",
    "start_refresh_brain",
]
