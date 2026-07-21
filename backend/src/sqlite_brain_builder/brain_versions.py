from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from send2trash import send2trash

from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir, slugify_name


VERSION_STORE_NAME = "brain_versions"
VERSION_MANIFEST_NAME = "VERSION_MANIFEST.json"
VERSION_SCHEMA = "evidence-os-brain-version/v1"
ACTOR_TYPES = {"app", "ai"}
SOURCE_HASH_SKIP_DIRS = {
    ".git", ".next", ".pytest_cache", ".venv", "__pycache__", "brain_versions", "build", "dist",
    "node_modules", "venv",
}


class BrainVersionError(RuntimeError):
    pass


class BrainVersionTamperError(BrainVersionError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp(payload: dict[str, Any], field: str) -> dict[str, Any]:
    body = dict(payload)
    body.pop(field, None)
    body[field] = _sha256_bytes(_canonical_bytes(body))
    return body


def _verify_stamp(payload: dict[str, Any], field: str, label: str) -> None:
    expected = str(payload.get(field) or "")
    body = dict(payload)
    body.pop(field, None)
    actual = _sha256_bytes(_canonical_bytes(body))
    if not expected or expected != actual:
        raise BrainVersionTamperError(f"{label}_SHA256_MISMATCH")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(_canonical_bytes(payload) + b"\n")
    os.replace(temporary, path)


def _make_read_only(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    except OSError:
        pass


def _make_writable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


def _version_store(brain_root: Path) -> Path:
    return brain_root / VERSION_STORE_NAME


def _manifest_path(brain_root: Path) -> Path:
    return _version_store(brain_root) / VERSION_MANIFEST_NAME


def _load_manifest(brain_root: Path, brain_name: str) -> dict[str, Any]:
    path = _manifest_path(brain_root)
    if not path.exists():
        return _stamp(
            {
                "schema": VERSION_SCHEMA,
                "brain_name": brain_name,
                "brain_output": str(brain_root),
                "versions": [],
            },
            "manifest_sha256",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BrainVersionTamperError(f"VERSION_MANIFEST_UNREADABLE: {exc}") from exc
    _verify_stamp(payload, "manifest_sha256", "VERSION_MANIFEST")
    if payload.get("schema") != VERSION_SCHEMA or not isinstance(payload.get("versions"), list):
        raise BrainVersionTamperError("VERSION_MANIFEST_SCHEMA_INVALID")
    return payload


def _authoritative_files(brain_root: Path) -> list[Path]:
    project = brain_root / "project"
    if not project.exists():
        return []
    files: set[Path] = set()
    if (brain_root / ".uepc_env").is_file():
        for path in brain_root.rglob("*"):
            if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                continue
            rel = path.relative_to(brain_root)
            if rel.parts[0] in {"packages", "brain_snapshots", VERSION_STORE_NAME}:
                continue
            if rel.parts[0] == "receipts":
                continue
            if len(rel.parts) >= 3 and rel.parts[:2] == ("project", "runtime"):
                continue
            if len(rel.parts) >= 3 and rel.parts[:2] == ("project", "deltas"):
                continue
            files.add(path)
        return sorted(files, key=lambda item: item.relative_to(brain_root).as_posix())
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(brain_root)
        if "topology" in rel.parts or VERSION_STORE_NAME in rel.parts:
            continue
        if len(rel.parts) >= 3 and rel.parts[:2] == ("project", "deltas"):
            continue
        if path.suffix.lower() == ".sqlite":
            files.add(path)
        elif rel.as_posix() in {"project/project_pointer.json", "project/sector_index.json"}:
            files.add(path)
        elif len(rel.parts) >= 3 and rel.parts[1] == "pointers" and path.suffix.lower() == ".json":
            files.add(path)
        elif len(rel.parts) >= 3 and rel.parts[1] == "artifacts":
            files.add(path)
    return sorted(files, key=lambda item: item.relative_to(brain_root).as_posix())


def _hash_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(path)
        if any(part in SOURCE_HASH_SKIP_DIRS for part in rel.parts):
            continue
        file_hash = _sha256_file(file_path)
        digest.update(rel.as_posix().encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
        digest.update(str(file_path.stat().st_size).encode("ascii"))
        digest.update(b"\x00")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _source_hash(path_text: str, stored_hash: str) -> dict[str, Any]:
    path = Path(path_text) if path_text else None
    if re.fullmatch(r"[0-9a-fA-F]{64}", stored_hash):
        return {"sha256": stored_hash, "status": "REGISTRY_HASH", "path": path_text}
    if path and path.is_file():
        return {"sha256": _sha256_file(path), "status": "FILE_HASH", "path": path_text}
    if path and path.is_dir():
        return {"sha256": _hash_directory(path), "status": "DIRECTORY_TREE_HASH", "path": path_text}
    if stored_hash:
        normalized_hash = _sha256_bytes(
            f"registry:{stored_hash}:{path_text}".encode("utf-8", "surrogatepass")
        )
        return {"sha256": normalized_hash, "status": "REGISTRY_VALUE_SHA256", "path": path_text}
    missing_hash = _sha256_bytes(f"missing:{path_text}".encode("utf-8", "surrogatepass"))
    return {"sha256": missing_hash, "status": "SOURCE_PATH_MISSING", "path": path_text}


def _collect_source_hashes(brain_root: Path) -> dict[str, Any]:
    router = brain_root / "project" / "project_router.sqlite"
    if not router.exists():
        return {}
    databases = [router, *sorted((brain_root / "project" / "sectors").rglob("*.sqlite"))]
    source_hashes: dict[str, Any] = {}
    for database in databases:
        con = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_registry'"
            ).fetchone()
            if not table:
                continue
            columns = {row[1] for row in con.execute("PRAGMA table_info(source_registry)")}
            wanted = [
                name
                for name in ("source_id", "display_name", "path", "canonical_path", "source_hash", "sha256")
                if name in columns
            ]
            if "source_id" not in wanted:
                continue
            rows = con.execute(f"SELECT {','.join(wanted)} FROM source_registry ORDER BY source_id").fetchall()
            for row in rows:
                record = dict(zip(wanted, row))
                source_id = str(record.get("source_id") or "source")
                path_text = str(record.get("path") or record.get("canonical_path") or "")
                stored_hash = str(record.get("source_hash") or record.get("sha256") or "")
                source_hashes[source_id] = {
                    "display_name": str(record.get("display_name") or source_id),
                    "registry_database": database.relative_to(brain_root).as_posix(),
                    **_source_hash(path_text, stored_hash),
                }
        finally:
            con.close()
    return source_hashes


def _install_object(source: Path, object_path: Path, expected_hash: str) -> None:
    object_path.parent.mkdir(parents=True, exist_ok=True)
    if object_path.exists():
        if _sha256_file(object_path) != expected_hash:
            raise BrainVersionTamperError(f"VERSION_OBJECT_TAMPERED: {expected_hash}")
        return
    temporary = object_path.with_name(f".{object_path.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temporary)
    if _sha256_file(temporary) != expected_hash:
        temporary.unlink(missing_ok=True)
        raise BrainVersionError(f"VERSION_OBJECT_COPY_HASH_MISMATCH: {source}")
    os.replace(temporary, object_path)
    _make_read_only(object_path)


def _snapshot_hash(files: list[dict[str, Any]]) -> str:
    identity = [{"path": item["path"], "sha256": item["sha256"], "size_bytes": item["size_bytes"]} for item in files]
    return _sha256_bytes(_canonical_bytes(identity))


def _validate_capture_actor(actor_type: str, actor_name: str, reason: str) -> tuple[str, str, str]:
    normalized_type = str(actor_type or "").strip().lower()
    normalized_name = str(actor_name or "").strip()
    normalized_reason = str(reason or "").strip()
    if normalized_type not in ACTOR_TYPES:
        raise BrainVersionError("VERSION_ACTOR_TYPE_REQUIRED_APP_OR_AI")
    if not normalized_name:
        raise BrainVersionError("VERSION_ACTOR_NAME_REQUIRED")
    if not normalized_reason:
        raise BrainVersionError("VERSION_REASON_REQUIRED")
    return normalized_type, normalized_name, normalized_reason


def capture_brain_version(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    actor_type: str,
    actor_name: str,
    reason: str,
    change_summary: str = "",
) -> dict[str, Any]:
    actor_type, actor_name, reason = _validate_capture_actor(actor_type, actor_name, reason)
    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    if not brain_root.exists():
        raise BrainVersionError(f"BRAIN_OUTPUT_NOT_FOUND: {brain_root}")
    authoritative = _authoritative_files(brain_root)
    if not authoritative:
        raise BrainVersionError("NO_AUTHORITATIVE_BRAIN_DATA")

    store = _version_store(brain_root)
    objects = store / "objects"
    records = store / "records"
    file_entries: list[dict[str, Any]] = []
    for source in authoritative:
        digest = _sha256_file(source)
        relative = source.relative_to(brain_root).as_posix()
        _install_object(source, objects / digest, digest)
        file_entries.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": source.stat().st_size,
                "object": f"objects/{digest}",
            }
        )

    snapshot_hash = _snapshot_hash(file_entries)
    timestamp_utc = _utc_now()
    timestamp_id = re.sub(r"[^0-9]", "", timestamp_utc)
    version_id = f"version_{timestamp_id}_{snapshot_hash[:12]}"
    manifest = _load_manifest(brain_root, brain_name)
    if any(item.get("version_id") == version_id for item in manifest["versions"]):
        raise BrainVersionError(f"VERSION_ID_COLLISION: {version_id}")
    previous_version_id = manifest["versions"][-1]["version_id"] if manifest["versions"] else None
    source_hashes = _collect_source_hashes(brain_root)
    record = _stamp(
        {
            "schema": VERSION_SCHEMA,
            "version_id": version_id,
            "timestamp_utc": timestamp_utc,
            "brain_name": brain_name,
            "brain_output": str(brain_root),
            "actor_type": actor_type,
            "actor_name": actor_name,
            "reason": reason,
            "change_summary": str(change_summary or reason).strip(),
            "previous_version_id": previous_version_id,
            "source_hashes": source_hashes,
            "snapshot_hash": snapshot_hash,
            "snapshot_files": file_entries,
            "file_count": len(file_entries),
            "total_bytes": sum(item["size_bytes"] for item in file_entries),
        },
        "record_sha256",
    )
    record_path = records / f"{version_id}.json"
    if record_path.exists():
        raise BrainVersionError(f"IMMUTABLE_VERSION_ALREADY_EXISTS: {version_id}")
    _write_json_atomic(record_path, record)
    _make_read_only(record_path)

    summary = {
        key: record[key]
        for key in (
            "version_id", "timestamp_utc", "actor_type", "actor_name", "reason", "change_summary",
            "previous_version_id", "source_hashes", "snapshot_hash", "record_sha256", "file_count", "total_bytes",
        )
    }
    summary["record_path"] = record_path.relative_to(store).as_posix()
    manifest["versions"].append(summary)
    manifest = _stamp(manifest, "manifest_sha256")
    _write_json_atomic(_manifest_path(brain_root), manifest)
    return {
        **summary,
        "brain_output": str(brain_root),
        "version_store": str(store),
        "manifest_path": str(_manifest_path(brain_root)),
        "manifest_sha256": manifest["manifest_sha256"],
    }


def _safe_record_path(store: Path, record_path: str) -> Path:
    candidate = (store / record_path).resolve()
    records_root = (store / "records").resolve()
    if candidate.parent != records_root:
        raise BrainVersionTamperError("VERSION_RECORD_PATH_ESCAPES_STORE")
    return candidate


def _load_record(brain_root: Path, summary: dict[str, Any]) -> dict[str, Any]:
    store = _version_store(brain_root)
    path = _safe_record_path(store, str(summary.get("record_path") or ""))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BrainVersionTamperError(f"VERSION_RECORD_UNREADABLE: {exc}") from exc
    _verify_stamp(record, "record_sha256", "VERSION_RECORD")
    if record.get("version_id") != summary.get("version_id"):
        raise BrainVersionTamperError("VERSION_RECORD_ID_MISMATCH")
    if record.get("record_sha256") != summary.get("record_sha256"):
        raise BrainVersionTamperError("VERSION_RECORD_INDEX_HASH_MISMATCH")
    indexed_fields = (
        "timestamp_utc", "actor_type", "actor_name", "reason", "change_summary",
        "previous_version_id", "source_hashes", "snapshot_hash", "file_count", "total_bytes",
    )
    if any(summary.get(field) != record.get(field) for field in indexed_fields):
        raise BrainVersionTamperError("VERSION_RECORD_INDEX_METADATA_MISMATCH")
    if _snapshot_hash(record.get("snapshot_files") or []) != record.get("snapshot_hash"):
        raise BrainVersionTamperError("VERSION_SNAPSHOT_MANIFEST_HASH_MISMATCH")
    return record


def verify_version_snapshot(brain_root: Path, record: dict[str, Any]) -> None:
    store = _version_store(brain_root)
    for item in record.get("snapshot_files") or []:
        object_path = (store / str(item.get("object") or "")).resolve()
        objects_root = (store / "objects").resolve()
        if object_path.parent != objects_root:
            raise BrainVersionTamperError("VERSION_OBJECT_PATH_ESCAPES_STORE")
        if not object_path.exists() or object_path.stat().st_size != int(item.get("size_bytes") or -1):
            raise BrainVersionTamperError(f"VERSION_OBJECT_SIZE_MISMATCH: {item.get('path')}")
        if _sha256_file(object_path) != item.get("sha256"):
            raise BrainVersionTamperError(f"VERSION_OBJECT_SHA256_MISMATCH: {item.get('path')}")


def list_brain_versions(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    verify_hashes: bool = False,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    manifest = _load_manifest(brain_root, brain_name)
    versions = []
    for summary in reversed(manifest["versions"]):
        record = _load_record(brain_root, summary)
        if verify_hashes:
            verify_version_snapshot(brain_root, record)
        versions.append({**summary, "integrity_status": "VERIFIED" if verify_hashes else "STAMP_VALID"})
    return {
        "brain_name": brain_name,
        "brain_output": str(brain_root),
        "version_store": str(_version_store(brain_root)),
        "manifest_path": str(_manifest_path(brain_root)),
        "manifest_sha256": manifest["manifest_sha256"],
        "version_count": len(versions),
        "versions": versions,
        "dropped_version_count": len(manifest.get("dropped_versions") or []),
        "dropped_versions": list(reversed(manifest.get("dropped_versions") or [])),
    }


def drop_brain_version_to_recycle_bin(
    workspace_dir: str | Path,
    brain_name: str,
    version_id: str,
    *,
    confirm_version_id: str,
    actor_name: str,
    reason: str,
    trash: Callable[[str], None] = send2trash,
) -> dict[str, Any]:
    """Move one historical rollback record to the Windows Recycle Bin.

    The current brain and its current version are never changed. Content-
    addressed snapshot objects stay in place because another immutable version
    may reference the same object. The selected signed record is staged in its
    own directory and that directory is sent to the Recycle Bin. A signed
    tombstone remains in the manifest so lineage cannot silently close over the
    dropped version.
    """

    version_id = str(version_id or "").strip()
    if not version_id or str(confirm_version_id or "").strip() != version_id:
        raise BrainVersionError("VERSION_DROP_EXPLICIT_ID_CONFIRMATION_REQUIRED")
    actor = str(actor_name or "").strip()
    explanation = str(reason or "").strip()
    if not actor:
        raise BrainVersionError("VERSION_DROP_ACTOR_REQUIRED")
    if not explanation:
        raise BrainVersionError("VERSION_DROP_REASON_REQUIRED")

    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    manifest = _load_manifest(brain_root, brain_name)
    summary = next((row for row in manifest["versions"] if row.get("version_id") == version_id), None)
    if summary is None:
        raise BrainVersionError(f"VERSION_NOT_FOUND: {version_id}")
    current_version_id = str((manifest["versions"][-1] if manifest["versions"] else {}).get("version_id") or "")
    if version_id == current_version_id:
        raise BrainVersionError("CURRENT_VERSION_CANNOT_BE_DROPPED")
    record = _load_record(brain_root, summary)
    verify_version_snapshot(brain_root, record)
    store = _version_store(brain_root)
    record_path = _safe_record_path(store, str(summary.get("record_path") or ""))
    if not record_path.is_file():
        raise BrainVersionError("VERSION_RECORD_NOT_FOUND")

    dropped_at = _utc_now()
    tombstone = {
        "version_id": version_id,
        "record_sha256": summary["record_sha256"],
        "snapshot_hash": summary["snapshot_hash"],
        "previous_version_id": summary.get("previous_version_id"),
        "dropped_at": dropped_at,
        "actor_name": actor,
        "reason": explanation,
        "record_recycled": True,
        "current_brain_changed": False,
        "snapshot_objects_deleted": False,
    }
    original_manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
    next_manifest = dict(manifest)
    next_manifest["versions"] = [row for row in manifest["versions"] if row.get("version_id") != version_id]
    next_manifest["dropped_versions"] = [*(manifest.get("dropped_versions") or []), tombstone]
    next_manifest = _stamp(next_manifest, "manifest_sha256")
    staging_root = store / ".version_drop_staging"
    staged_payload = staging_root / version_id
    staged_record = staged_payload / record_path.name
    if staged_payload.exists():
        raise BrainVersionError("VERSION_DROP_STAGING_ALREADY_EXISTS")
    staged_payload.mkdir(parents=True, exist_ok=False)
    _make_writable(record_path)
    try:
        os.replace(record_path, staged_record)
        _write_json_atomic(_manifest_path(brain_root), next_manifest)
        trash(str(staged_payload))
        if staged_payload.exists():
            raise BrainVersionError("VERSION_RECYCLE_DID_NOT_REMOVE_PAYLOAD")
    except Exception as exc:
        _write_json_atomic(_manifest_path(brain_root), original_manifest)
        if staged_record.exists() and not record_path.exists():
            record_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_record, record_path)
        if record_path.exists():
            _make_read_only(record_path)
        try:
            staged_payload.rmdir()
            staging_root.rmdir()
        except OSError:
            pass
        if isinstance(exc, BrainVersionError):
            raise
        raise BrainVersionError(f"VERSION_RECYCLE_FAILED:{type(exc).__name__}:{exc}") from exc

    receipt = _stamp(
        {
            "schema": "evidence-os-brain-version-drop/v1",
            "status": "PASS",
            "action": "DROP_HISTORICAL_VERSION_TO_RECYCLE_BIN",
            "brain_name": brain_name,
            "version_id": version_id,
            "current_version_id": current_version_id,
            "record_sha256": summary["record_sha256"],
            "snapshot_hash": summary["snapshot_hash"],
            "dropped_at": dropped_at,
            "actor_name": actor,
            "reason": explanation,
            "current_brain_changed": False,
            "remaining_version_count": len(next_manifest["versions"]),
            "snapshot_objects_deleted": False,
            "evidenceos_recovery_available": False,
            "windows_recycle_bin_managed": True,
            "permanent_deletion_used": False,
            "manifest_sha256": next_manifest["manifest_sha256"],
        },
        "receipt_sha256",
    )
    receipt_dir = brain_root / "receipts"
    receipt_path = receipt_dir / f"VERSION_DROP_{re.sub(r'[^A-Za-z0-9._-]+', '_', version_id)}.json"
    _write_json_atomic(receipt_path, receipt)
    return {**receipt, "receipt_path": str(receipt_path), "versions": list_brain_versions(workspace, brain_name, verify_hashes=True)}


def load_brain_version_record(
    workspace_dir: str | Path,
    brain_name: str,
    version_id: str,
    *,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    manifest = _load_manifest(brain_root, brain_name)
    summary = next((item for item in manifest["versions"] if item.get("version_id") == version_id), None)
    if not summary:
        raise BrainVersionError(f"VERSION_NOT_FOUND: {version_id}")
    record = _load_record(brain_root, summary)
    if verify_hashes:
        verify_version_snapshot(brain_root, record)
    return record


def current_brain_snapshot(
    workspace_dir: str | Path,
    brain_name: str,
) -> dict[str, Any]:
    """Hash the current authoritative brain files without changing state."""

    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    if not brain_root.is_dir():
        raise BrainVersionError(f"BRAIN_OUTPUT_NOT_FOUND: {brain_root}")
    files = []
    for path in _authoritative_files(brain_root):
        files.append(
            {
                "path": path.relative_to(brain_root).as_posix(),
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    if not files:
        raise BrainVersionError("NO_AUTHORITATIVE_BRAIN_DATA")
    return {
        "brain_name": brain_name,
        "brain_output": str(brain_root),
        "snapshot_hash": _snapshot_hash(files),
        "snapshot_files": files,
        "file_count": len(files),
        "total_bytes": sum(int(item["size_bytes"]) for item in files),
    }


def compare_current_brain_to_version(
    workspace_dir: str | Path,
    brain_name: str,
    version_id: str,
) -> dict[str, Any]:
    """Compare the selected brain with one verified immutable version."""

    record = load_brain_version_record(
        workspace_dir,
        brain_name,
        version_id,
        verify_hashes=True,
    )
    current = current_brain_snapshot(workspace_dir, brain_name)
    prior_files = {str(item["path"]): item for item in record.get("snapshot_files") or []}
    current_files = {str(item["path"]): item for item in current["snapshot_files"]}
    changed_files = []
    for relative in sorted(set(prior_files) | set(current_files)):
        prior = prior_files.get(relative)
        present = current_files.get(relative)
        if prior and present and (
            prior.get("sha256") == present.get("sha256")
            and int(prior.get("size_bytes") or 0) == int(present.get("size_bytes") or 0)
        ):
            continue
        changed_files.append(
            {
                "path": relative,
                "change_kind": "ADDED" if present and not prior else "REMOVED" if prior and not present else "MODIFIED",
                "previous_sha256": prior.get("sha256") if prior else None,
                "current_sha256": present.get("sha256") if present else None,
                "previous_size_bytes": int(prior.get("size_bytes") or 0) if prior else None,
                "current_size_bytes": int(present.get("size_bytes") or 0) if present else None,
            }
        )
    matches = current["snapshot_hash"] == record["snapshot_hash"] and not changed_files
    return {
        "status": "MATCH" if matches else "MISMATCH",
        "brain_name": brain_name,
        "version_id": version_id,
        "expected_snapshot_hash": record["snapshot_hash"],
        "current_snapshot_hash": current["snapshot_hash"],
        "changed_files": changed_files,
        "verified_version_objects": True,
        "current_brain_mutated": False,
    }


def materialize_brain_version_candidate(
    workspace_dir: str | Path,
    brain_name: str,
    version_id: str,
    destination: str | Path,
) -> dict[str, Any]:
    """Materialize a verified version below the workspace as an isolated candidate."""

    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    candidate = Path(destination).expanduser().resolve()
    workspace_root = workspace.resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError as exc:
        raise BrainVersionError("CANDIDATE_DESTINATION_OUTSIDE_WORKSPACE") from exc
    if candidate == brain_root.resolve():
        raise BrainVersionError("CANDIDATE_DESTINATION_MUST_NOT_BE_CURRENT_BRAIN")
    if candidate.exists():
        raise BrainVersionError(f"CANDIDATE_DESTINATION_ALREADY_EXISTS: {candidate}")

    record = load_brain_version_record(
        workspace,
        brain_name,
        version_id,
        verify_hashes=True,
    )
    store = _version_store(brain_root)
    restored: list[dict[str, Any]] = []
    candidate.mkdir(parents=True)
    try:
        for item in record.get("snapshot_files") or []:
            relative = Path(str(item.get("path") or ""))
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise BrainVersionTamperError("CANDIDATE_MATERIALIZATION_PATH_INVALID")
            source = (store / str(item.get("object") or "")).resolve()
            objects_root = (store / "objects").resolve()
            if source.parent != objects_root:
                raise BrainVersionTamperError("VERSION_OBJECT_PATH_ESCAPES_STORE")
            target = candidate / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            _make_writable(target)
            digest = _sha256_file(target)
            if digest != item.get("sha256"):
                raise BrainVersionError(f"CANDIDATE_COPY_HASH_MISMATCH: {relative.as_posix()}")
            restored.append(
                {
                    "path": relative.as_posix(),
                    "sha256": digest,
                    "size_bytes": target.stat().st_size,
                }
            )
        candidate_snapshot_hash = _snapshot_hash(restored)
        if candidate_snapshot_hash != record.get("snapshot_hash"):
            raise BrainVersionError("CANDIDATE_SNAPSHOT_HASH_MISMATCH")
        receipt = _stamp(
            {
                "schema": VERSION_SCHEMA,
                "materialized_at": _utc_now(),
                "brain_name": brain_name,
                "source_brain_output": str(brain_root),
                "source_version_id": version_id,
                "source_snapshot_hash": record["snapshot_hash"],
                "candidate_output": str(candidate),
                "candidate_snapshot_hash": candidate_snapshot_hash,
                "restored_file_count": len(restored),
                "current_brain_unchanged": True,
            },
            "receipt_sha256",
        )
        receipt_path = candidate / "receipts" / "BRAIN_VERSION_CANDIDATE_MATERIALIZATION_RECEIPT.json"
        _write_json_atomic(receipt_path, receipt)
    except Exception:
        if candidate.exists() and candidate != brain_root.resolve():
            shutil.rmtree(candidate)
        raise
    return {
        **receipt,
        "candidate_output": str(candidate),
        "candidate_snapshot_hash": candidate_snapshot_hash,
        "receipt_path": str(receipt_path),
        "current_brain_unchanged": True,
        "history_unchanged": True,
    }


def _rollback_output(
    workspace: Path,
    brain_name: str,
    version_id: str,
    destination_brain_name: str | None = None,
) -> Path:
    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", version_id).strip("._-")
    if not safe_version:
        raise BrainVersionError("ROLLBACK_VERSION_ID_INVALID")
    destination = (
        brain_output_dir(workspace, destination_brain_name)
        if destination_brain_name
        else workspace / f"{slugify_name(brain_name)}_rollback_{safe_version}_output"
    )
    if destination.parent.resolve() != workspace.resolve():
        raise BrainVersionError("ROLLBACK_OUTPUT_ESCAPES_WORKSPACE")
    return destination


def rollback_brain_version(
    workspace_dir: str | Path,
    brain_name: str,
    version_id: str,
    *,
    actor_name: str = "",
    reason: str = "",
    destination_brain_name: str | None = None,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    brain_root = brain_output_dir(workspace, brain_name)
    manifest = _load_manifest(brain_root, brain_name)
    summary = next((item for item in manifest["versions"] if item.get("version_id") == version_id), None)
    if not summary:
        raise BrainVersionError(f"VERSION_NOT_FOUND: {version_id}")
    record = _load_record(brain_root, summary)
    verify_version_snapshot(brain_root, record)

    destination = _rollback_output(workspace, brain_name, version_id, destination_brain_name)
    if destination.resolve() == brain_root.resolve():
        raise BrainVersionError("ROLLBACK_OUTPUT_MUST_NOT_REPLACE_CURRENT_BRAIN")
    if destination.exists():
        raise BrainVersionError(f"ROLLBACK_OUTPUT_ALREADY_EXISTS: {destination}")
    destination.mkdir(parents=True)
    store = _version_store(brain_root)
    restored: list[dict[str, Any]] = []
    try:
        for item in record["snapshot_files"]:
            relative = Path(str(item["path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise BrainVersionTamperError("ROLLBACK_PATH_INVALID")
            source = store / str(item["object"])
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            _make_writable(target)
            restored_hash = _sha256_file(target)
            if restored_hash != item["sha256"]:
                raise BrainVersionError(f"ROLLBACK_COPY_HASH_MISMATCH: {relative.as_posix()}")
            restored.append({"path": relative.as_posix(), "sha256": restored_hash, "size_bytes": target.stat().st_size})
    except Exception:
        if destination.exists() and destination.parent.resolve() == workspace.resolve():
            shutil.rmtree(destination)
        raise

    restored_snapshot_hash = _snapshot_hash(restored)
    if restored_snapshot_hash != record["snapshot_hash"]:
        shutil.rmtree(destination)
        raise BrainVersionError("ROLLBACK_SNAPSHOT_HASH_MISMATCH")
    receipt = _stamp(
        {
            "schema": VERSION_SCHEMA,
            "rollback_timestamp_utc": _utc_now(),
            "brain_name": brain_name,
            "source_brain_output": str(brain_root),
            "source_version_id": version_id,
            "source_snapshot_hash": record["snapshot_hash"],
            "rollback_output": str(destination),
            "destination_brain_name": str(destination_brain_name or ""),
            "actor_name": str(actor_name or "system"),
            "reason": str(reason or "restore immutable brain version"),
            "restored_file_count": len(restored),
            "restored_snapshot_hash": restored_snapshot_hash,
        },
        "receipt_sha256",
    )
    receipt_path = destination / "BRAIN_VERSION_ROLLBACK_RECEIPT.json"
    _write_json_atomic(receipt_path, receipt)
    return {
        **receipt,
        "rollback_output": str(destination),
        "receipt_path": str(receipt_path),
        "current_brain_unchanged": True,
        "history_unchanged": True,
    }
