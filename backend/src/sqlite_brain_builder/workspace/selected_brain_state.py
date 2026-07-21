from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir


MODEL_OUTPUT_STATE_SCHEMA = "T023_MODEL_OUTPUT_STATE_V1"
MODEL_OUTPUT_RECEIPT_SCHEMA = "T023_MATERIALIZED_MODEL_OUTPUT_V1"
HISTORICAL_PROVIDER_DELTA_ARCHIVE_SCHEMA = "T023_HISTORICAL_PROVIDER_DELTA_ARCHIVE_V1"
_LEGACY_DELTA_STATE_SCHEMA = "T023_MODEL_DELTA_STATE_V1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MEDIA_SUFFIX = {
    "application/json": ".json",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/x-diff": ".patch",
}


class SelectedBrainStateError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_payload(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _output_root(workspace: Path, brain_name: str) -> Path:
    return brain_output_dir(workspace, brain_name) / ".evidenceos_model_outputs"


def _legacy_delta_root(workspace: Path, brain_name: str) -> Path:
    return brain_output_dir(workspace, brain_name) / ".evidenceos_model_deltas"


def _make_read_only(path: Path) -> None:
    path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _default_output_state(brain_name: str) -> dict[str, Any]:
    return {
        "schema": MODEL_OUTPUT_STATE_SCHEMA,
        "brain_name": brain_name,
        "outputs": [],
    }


def _load_output_index(workspace: Path, brain_name: str) -> dict[str, Any]:
    path = _output_root(workspace, brain_name) / "index.json"
    if not path.exists():
        return _default_output_state(brain_name)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectedBrainStateError("MODEL_OUTPUT_INDEX_UNREADABLE") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema") != MODEL_OUTPUT_STATE_SCHEMA
        or state.get("brain_name") != brain_name
        or not isinstance(state.get("outputs"), list)
    ):
        raise SelectedBrainStateError("MODEL_OUTPUT_INDEX_SCHEMA_INVALID")
    return state


def load_model_output_state(workspace_dir: str | Path, brain_name: str) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir).resolve()
    state = _load_output_index(workspace, brain_name)
    verified: list[dict[str, Any]] = []
    root = _output_root(workspace, brain_name).resolve()
    for raw in state["outputs"]:
        if not isinstance(raw, dict):
            raise SelectedBrainStateError("MODEL_OUTPUT_INDEX_ROW_INVALID")
        output_id = str(raw.get("output_id") or "")
        relative = str(raw.get("relative_path") or "")
        digest = str(raw.get("output_sha256") or "").casefold()
        target = (root / relative).resolve()
        if not output_id.startswith("model_output_") or not _SHA256.fullmatch(digest):
            raise SelectedBrainStateError("MODEL_OUTPUT_INDEX_IDENTITY_INVALID")
        if root not in target.parents or not target.is_file():
            raise SelectedBrainStateError("MODEL_OUTPUT_FILE_MISSING")
        if _sha256_bytes(target.read_bytes()) != digest:
            raise SelectedBrainStateError("MODEL_OUTPUT_FILE_HASH_MISMATCH")
        verified.append(dict(raw))
    latest = verified[-1]["output_id"] if verified else None
    return {
        "schema": MODEL_OUTPUT_STATE_SCHEMA,
        "brain_name": brain_name,
        "output_count": len(verified),
        "latest_output_id": latest,
        "outputs": verified,
        "project_truth_authority": False,
        "universal_refresh_required_for_local_change_truth": True,
    }


def load_historical_provider_delta_archive(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    """Verify legacy evidence without exposing it as active project truth."""

    workspace = normalize_workspace_dir(workspace_dir).resolve()
    root = _legacy_delta_root(workspace, brain_name).resolve()
    index_path = root / "index.json"
    if not index_path.is_file():
        return {
            "schema": HISTORICAL_PROVIDER_DELTA_ARCHIVE_SCHEMA,
            "brain_name": brain_name,
            "status": "NO_HISTORICAL_PROVIDER_DELTA_EVIDENCE",
            "record_count": 0,
            "records": [],
            "active_execution": False,
            "project_truth_authority": False,
        }
    try:
        state = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SelectedBrainStateError("HISTORICAL_PROVIDER_DELTA_INDEX_UNREADABLE") from exc
    if (
        not isinstance(state, dict)
        or state.get("schema") != _LEGACY_DELTA_STATE_SCHEMA
        or state.get("brain_name") != brain_name
        or not isinstance(state.get("deltas"), list)
    ):
        raise SelectedBrainStateError("HISTORICAL_PROVIDER_DELTA_INDEX_SCHEMA_INVALID")
    verified: list[dict[str, Any]] = []
    for raw in state["deltas"]:
        if not isinstance(raw, dict):
            raise SelectedBrainStateError("HISTORICAL_PROVIDER_DELTA_ROW_INVALID")
        relative = str(raw.get("relative_path") or "")
        digest = str(raw.get("resulting_delta_sha256") or "").casefold()
        target = (root / relative).resolve()
        if root not in target.parents or not target.is_file() or not _SHA256.fullmatch(digest):
            raise SelectedBrainStateError("HISTORICAL_PROVIDER_DELTA_FILE_MISSING")
        if _sha256_bytes(target.read_bytes()) != digest:
            raise SelectedBrainStateError("HISTORICAL_PROVIDER_DELTA_FILE_HASH_MISMATCH")
        verified.append(dict(raw))
    archive_manifest = {
        "schema": HISTORICAL_PROVIDER_DELTA_ARCHIVE_SCHEMA,
        "brain_name": brain_name,
        "status": "READ_ONLY_HISTORICAL_ARCHIVE_VERIFIED",
        "record_count": len(verified),
        "records": verified,
        "legacy_index_path": str(index_path),
        "legacy_index_sha256": _sha256_bytes(index_path.read_bytes()),
        "active_execution": False,
        "project_truth_authority": False,
        "universal_refresh_source": False,
    }
    archive_manifest["archive_manifest_sha256"] = _sha256_payload(archive_manifest)
    return archive_manifest


def materialize_model_output(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    execution_id: str,
    endpoint_id: str,
    provider_response_id: str | None,
    baseline_snapshot_hash: str,
    output_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Index a proposed file or patch as lineage output, never project truth."""

    workspace = normalize_workspace_dir(workspace_dir).resolve()
    content = output_payload.get("content")
    if not isinstance(content, str) or not content:
        raise SelectedBrainStateError("MODEL_OUTPUT_CONTENT_REQUIRED")
    raw = content.encode("utf-8")
    if len(raw) > _MAX_OUTPUT_BYTES:
        raise SelectedBrainStateError("MODEL_OUTPUT_TOO_LARGE")
    digest = _sha256_bytes(raw)
    declared = str(output_payload.get("sha256") or "").casefold()
    if not _SHA256.fullmatch(declared):
        raise SelectedBrainStateError("MODEL_OUTPUT_SHA256_REQUIRED")
    if declared != digest:
        raise SelectedBrainStateError("MODEL_OUTPUT_SHA256_MISMATCH")
    media_type = str(output_payload.get("media_type") or "text/plain").casefold()
    suffix = _MEDIA_SUFFIX.get(media_type)
    if not suffix:
        raise SelectedBrainStateError("MODEL_OUTPUT_MEDIA_TYPE_UNSUPPORTED")
    output_id = f"model_output_{digest[:24]}"
    root = _output_root(workspace, brain_name)
    relative = f"objects/{output_id}{suffix}"
    target = root / relative
    if target.exists():
        if _sha256_bytes(target.read_bytes()) != digest:
            raise SelectedBrainStateError("IMMUTABLE_MODEL_OUTPUT_COLLISION")
    else:
        _write_atomic(target, raw)
        _make_read_only(target)

    receipt = {
        "schema": MODEL_OUTPUT_RECEIPT_SCHEMA,
        "output_id": output_id,
        "output_sha256": digest,
        "relative_path": relative,
        "media_type": media_type,
        "size_bytes": len(raw),
        "brain_name": brain_name,
        "baseline_snapshot_hash": baseline_snapshot_hash,
        "execution_id": execution_id,
        "endpoint_id": endpoint_id,
        "provider": endpoint_id,
        "provider_response_id": provider_response_id,
        "target_path": str(output_payload.get("target_path") or ""),
        "language": str(output_payload.get("language") or ""),
        "patch_type": str(output_payload.get("patch_type") or "PROPOSED_OUTPUT"),
        "base_reference": str(output_payload.get("base_reference") or baseline_snapshot_hash),
        "displayed_link": str(output_payload.get("displayed_link") or ""),
        "output_description": str(output_payload.get("description") or "Proposed model output"),
        "application_status": "PROPOSED_NOT_APPLIED",
        "project_truth_authority": False,
        "automatic_project_write": False,
        "automatic_refresh": False,
        "automatic_fuse": False,
        "created_at": _utc_now(),
    }
    receipt["receipt_sha256"] = _sha256_payload(receipt)
    receipt_path = root / "receipts" / f"{output_id}.json"
    receipt_bytes = (json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if receipt_path.exists():
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        immutable_identity = {
            key: existing.get(key)
            for key in ("output_id", "output_sha256", "relative_path", "media_type", "size_bytes")
        }
        if immutable_identity != {
            key: receipt.get(key)
            for key in ("output_id", "output_sha256", "relative_path", "media_type", "size_bytes")
        }:
            raise SelectedBrainStateError("IMMUTABLE_MODEL_OUTPUT_RECEIPT_COLLISION")
        receipt = existing
    else:
        _write_atomic(receipt_path, receipt_bytes)
        _make_read_only(receipt_path)

    state = _load_output_index(workspace, brain_name)
    existing = next((item for item in state["outputs"] if item.get("output_id") == output_id), None)
    index_row = {
        **receipt,
        "output_path": str(target),
        "receipt_path": str(receipt_path),
    }
    if existing is not None:
        if existing != index_row:
            raise SelectedBrainStateError("MODEL_OUTPUT_INDEX_COLLISION")
    else:
        state["outputs"].append(index_row)
        _write_atomic(
            root / "index.json",
            (json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
    return index_row


__all__ = [
    "HISTORICAL_PROVIDER_DELTA_ARCHIVE_SCHEMA",
    "MODEL_OUTPUT_RECEIPT_SCHEMA",
    "MODEL_OUTPUT_STATE_SCHEMA",
    "SelectedBrainStateError",
    "load_historical_provider_delta_archive",
    "load_model_output_state",
    "materialize_model_output",
]
