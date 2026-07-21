from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import traceback
import zlib
from pathlib import Path
from typing import Any, Callable

from sqlite_brain_builder.runtime.env15_project_schema import governed_sector_mutation, resolve_env15_sector
from sqlite_brain_builder.runtime.source_fingerprint_cache import (
    CODE_SKIP_DIRS,
    cached_content_hash,
    record_content_hash,
)


class Env15CodeIngestionError(RuntimeError):
    pass


_BINARY_CODE_ASSET_EXTENSIONS = {
    ".3ds", ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".dll", ".dylib",
    ".db", ".exe", ".feather", ".gif", ".glb", ".gz", ".ico", ".jpeg", ".jpg",
    ".joblib", ".lib", ".mov", ".mp3", ".mp4", ".npy", ".npz", ".o", ".obj",
    ".onnx", ".parquet", ".pdf", ".pickle", ".pkl", ".png", ".pt", ".pth", ".pyc",
    ".so", ".sqlite", ".sqlite3", ".tar", ".tif", ".tiff", ".wasm", ".wav",
    ".webm", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".zip",
}

_CODE_PROJECTION_VERSION = (
    "env15_code_projection_v6_full_text_artifact_chunks_binary_metadata_exact_text"
)
_EXACT_BYTE_CHUNK_SIZE = 1024 * 1024
_ARTIFACT_TEXT_CHUNK_CHARACTERS = 64 * 1024
_ARTIFACT_TEXT_STATUSES = {
    "CODE_ARTIFACT_STUDIED_READ_ONLY",
    "CODE_ARTIFACT_METADATA_ONLY",
    "CODE_TEXT_TOO_LARGE_ARTIFACT_READ_ONLY",
}
_V59_COPY_TABLES = (
    "source_file",
    "code_repo",
    "code_folder",
    "git_remote",
    "git_branch",
    "git_commit",
    "git_commit_parent_edge",
    "git_diff_hunk",
    "git_line_change",
    "git_rename_map",
    "code_file",
    "app_route",
    "dependency_manifest",
    "dependency_item",
    "code_import_edge",
    "code_dependency_edge",
    "project_artifact",
    "artifact_relation_edge",
    "code_index_state",
    "workflow_node",
    "workflow_edge",
)


def _table_snapshot(connection: sqlite3.Connection, table: str) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
    columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
    if not columns:
        return (), []
    quoted = table.replace('"', '""')
    return columns, [tuple(row[column] for column in columns) for row in connection.execute(f'SELECT * FROM "{quoted}"')]


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in columns:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}')


def _ensure_v59_compatibility_schema(connection: sqlite3.Connection) -> None:
    """Add the V5.9 query contract without replacing enhanced Env15 tables."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_file(
          file_id TEXT PRIMARY KEY, logical_path TEXT, extension TEXT, size_bytes INTEGER,
          sha256 TEXT, lane_status TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS code_repo(
          id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
          metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS code_folder(
          folder_id TEXT PRIMARY KEY, source_id TEXT, raw_path TEXT, normalized_path TEXT,
          parent_folder_id TEXT, child_folder_count INTEGER, child_file_count INTEGER,
          languages_json TEXT, role_signals_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS git_remote(
          id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
          metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS git_branch(
          id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
          metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS git_commit(
          commit_sha TEXT PRIMARY KEY, short_sha TEXT, author_name TEXT,
          author_email_hash TEXT, author_time TEXT, committer_name TEXT,
          committer_email_hash TEXT, committer_time TEXT, commit_time TEXT,
          message TEXT, commit_order INTEGER
        );
        CREATE TABLE IF NOT EXISTS git_commit_parent_edge(
          edge_id TEXT PRIMARY KEY, commit_sha TEXT, parent_sha TEXT, parent_order INTEGER
        );
        CREATE TABLE IF NOT EXISTS git_diff_hunk(
          hunk_id TEXT PRIMARY KEY, commit_sha TEXT, old_path TEXT, new_path TEXT,
          old_start INTEGER, old_count INTEGER, new_start INTEGER, new_count INTEGER,
          hunk_header TEXT, patch_sha256 TEXT, history_policy TEXT,
          changed_line_count INTEGER, stored_line_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS git_line_change(
          line_change_id TEXT PRIMARY KEY, hunk_id TEXT, commit_sha TEXT, path TEXT,
          old_line_number INTEGER, new_line_number INTEGER, change_type TEXT,
          line_text TEXT, line_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS git_rename_map(
          rename_id TEXT PRIMARY KEY, commit_sha TEXT, old_path TEXT, new_path TEXT,
          similarity INTEGER
        );
        CREATE TABLE IF NOT EXISTS code_file(
          file_id TEXT PRIMARY KEY, canonical_path TEXT, language TEXT, extension TEXT,
          current_sha256 TEXT, is_active INTEGER
        );
        CREATE TABLE IF NOT EXISTS app_route(
          route_id TEXT PRIMARY KEY, route_path TEXT, route_type TEXT, file_id TEXT,
          file_version_id TEXT, route_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS dependency_manifest(
          manifest_id TEXT PRIMARY KEY, file_id TEXT, manifest_type TEXT, ecosystem TEXT,
          path TEXT, sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS dependency_item(
          dependency_id TEXT PRIMARY KEY, manifest_id TEXT, package_name TEXT,
          version_spec TEXT, ecosystem TEXT
        );
        CREATE TABLE IF NOT EXISTS code_import_edge(
          edge_id TEXT PRIMARY KEY, from_file_id TEXT, from_path TEXT,
          import_target TEXT, import_type TEXT, line_number INTEGER
        );
        CREATE TABLE IF NOT EXISTS code_dependency_edge(
          id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
          metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS project_artifact(
          artifact_id TEXT PRIMARY KEY, source_file_id TEXT, artifact_type TEXT,
          artifact_sha256 TEXT, path TEXT, size_bytes INTEGER, metadata_json TEXT,
          semantic_status TEXT
        );
        CREATE TABLE IF NOT EXISTS artifact_relation_edge(
          edge_id TEXT PRIMARY KEY, artifact_id TEXT, related_entity_type TEXT,
          related_entity_id TEXT, relation_type TEXT, confidence TEXT
        );
        CREATE TABLE IF NOT EXISTS code_index_state(
          repo_id TEXT, commit_sha TEXT, policy_version TEXT, indexed_at TEXT,
          PRIMARY KEY(repo_id,commit_sha)
        );
        CREATE TABLE IF NOT EXISTS workflow_node(
          id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
          metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS workflow_edge(
          id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
          metadata_json TEXT, created_at TEXT
        );
        """
    )
    for column, declaration in (
        ("file_version_id", "TEXT"),
        ("language", "TEXT"),
        ("role_name", "TEXT"),
    ):
        _ensure_column(connection, "code_chunk", column, declaration)
    for column, declaration in (
        ("symbol_name", "TEXT"),
        ("symbol_type", "TEXT"),
        ("file_version_id", "TEXT"),
        ("language", "TEXT"),
        ("parent_symbol_id", "TEXT"),
        ("symbol_sha256", "TEXT"),
    ):
        _ensure_column(connection, "code_symbol", column, declaration)
    _ensure_column(connection, "source_byte_coverage", "created_at", "TEXT")
    for column, declaration in (
        ("id", "TEXT"),
        ("source_id", "TEXT"),
        ("name", "TEXT"),
        ("path", "TEXT"),
        ("value", "TEXT"),
        ("metadata_json", "TEXT"),
        ("created_at", "TEXT"),
    ):
        _ensure_column(connection, "git_file_change", column, declaration)
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_v59_source_file_path ON source_file(logical_path);
        CREATE INDEX IF NOT EXISTS idx_v59_code_file_path ON code_file(canonical_path);
        CREATE INDEX IF NOT EXISTS idx_v59_import_file ON code_import_edge(from_file_id);
        CREATE INDEX IF NOT EXISTS idx_v59_route_file ON app_route(file_id);
        CREATE INDEX IF NOT EXISTS idx_v59_project_artifact_source ON project_artifact(source_file_id);
        CREATE INDEX IF NOT EXISTS idx_v59_git_parent_commit ON git_commit_parent_edge(commit_sha);
        CREATE INDEX IF NOT EXISTS idx_v59_git_hunk_commit_path ON git_diff_hunk(commit_sha,new_path);
        CREATE INDEX IF NOT EXISTS idx_v59_git_line_hunk ON git_line_change(hunk_id);
        """
    )


def _insert_table_snapshot(
    connection: sqlite3.Connection,
    table: str,
    snapshot: tuple[tuple[str, ...], list[tuple[Any, ...]]],
) -> int:
    columns, rows = snapshot
    if not columns or not rows:
        return 0
    destination_columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    selected = tuple(column for column in columns if column in destination_columns)
    if not selected:
        return 0
    indexes = [columns.index(column) for column in selected]
    quoted_columns = ",".join(f'"{column}"' for column in selected)
    placeholders = ",".join("?" for _ in selected)
    connection.executemany(
        f'INSERT OR REPLACE INTO "{table}"({quoted_columns}) VALUES({placeholders})',
        [tuple(row[index] for index in indexes) for row in rows],
    )
    return len(rows)


def _delete_v59_projection(
    connection: sqlite3.Connection,
    source_id: str,
    old_file_ids: list[str],
    old_commits: list[str],
) -> None:
    def delete_for_values(table: str, column: str, values: list[str]) -> None:
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        connection.execute(f'DELETE FROM "{table}" WHERE "{column}" IN ({placeholders})', tuple(values))

    old_artifact_ids = [
        row[0]
        for row in connection.execute(
            f'SELECT artifact_id FROM project_artifact WHERE source_file_id IN ({",".join("?" for _ in old_file_ids)})',
            tuple(old_file_ids),
        )
    ] if old_file_ids else []
    old_manifest_ids = [
        row[0]
        for row in connection.execute(
            f'SELECT manifest_id FROM dependency_manifest WHERE file_id IN ({",".join("?" for _ in old_file_ids)})',
            tuple(old_file_ids),
        )
    ] if old_file_ids else []
    old_repo_ids = [row[0] for row in connection.execute("SELECT id FROM code_repo WHERE source_id=?", (source_id,))]
    delete_for_values("artifact_relation_edge", "artifact_id", old_artifact_ids)
    delete_for_values("project_artifact", "source_file_id", old_file_ids)
    delete_for_values("dependency_item", "manifest_id", old_manifest_ids)
    delete_for_values("dependency_manifest", "file_id", old_file_ids)
    for table, column in (
        ("app_route", "file_id"),
        ("code_import_edge", "from_file_id"),
        ("code_file", "file_id"),
        ("source_file", "file_id"),
    ):
        delete_for_values(table, column, old_file_ids)
    for table in ("code_repo", "code_folder", "git_remote", "git_branch", "code_dependency_edge", "workflow_node", "workflow_edge"):
        connection.execute(f'DELETE FROM "{table}" WHERE source_id=?', (source_id,))
    delete_for_values("code_index_state", "repo_id", old_repo_ids)
    for table in ("git_commit_parent_edge", "git_diff_hunk", "git_line_change", "git_rename_map", "git_commit"):
        delete_for_values(table, "commit_sha", old_commits)


def _code_content_kind(relative_path: str, lane_status: str = "") -> str:
    if lane_status in {"CODE_MEDIA_ASSET_HASH_ONLY", "UNSUPPORTED_HASH_ONLY"}:
        return "BINARY_METADATA_ONLY"
    return "BINARY_METADATA_ONLY" if Path(relative_path).suffix.casefold() in _BINARY_CODE_ASSET_EXTENSIONS else "TEXT_INDEXED"


def _decode_text_lossless(data: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def _is_test_path(relative_path: str) -> bool:
    """Return true only for test path components or conventional test filenames."""

    normalized = relative_path.replace("\\", "/").casefold().strip("/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    stem = Path(name).stem
    return bool(
        stem.startswith(("test_", "spec_"))
        or stem.endswith(("_test", "_tests", "_spec", "_specs", ".test", ".spec"))
        or name.endswith((".test.js", ".test.ts", ".test.tsx", ".spec.js", ".spec.ts", ".spec.tsx"))
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_failure_receipt(
    brain_root: Path,
    *,
    lane_id: str,
    source_id: str,
    source_path: Path,
    operation: str,
    destination: Path,
    destination_hash_before: str,
    exc: BaseException,
) -> Path:
    receipt_dir = brain_root / "receipts" / "failed_builds"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    error_id = _stable_id("build_error", lane_id, source_id, operation, time.time_ns())
    destination_hash_after = _sha256_file(destination) if destination.is_file() else "MISSING"
    receipt = receipt_dir / f"{error_id}.json"
    payload = {
        "status": "FAILED_ATOMICALLY",
        "error_id": error_id,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "operation": operation,
        "lane_id": lane_id,
        "source_id": source_id,
        "source_path": str(source_path),
        "destination": str(destination),
        "destination_sha256_before": destination_hash_before,
        "destination_sha256_after": destination_hash_after,
        "destination_unchanged": destination_hash_before == destination_hash_after,
        "traceback": traceback.format_exc(),
        "created_epoch": time.time(),
    }
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(receipt)
    return receipt


def _stable_id(kind: str, *parts: Any) -> str:
    data = "\x1f".join(str(part) for part in parts).encode("utf-8", "surrogatepass")
    return f"{kind}_{hashlib.sha256(data).hexdigest()}"


def _rows(connection: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return list(connection.execute(sql, args))
    except sqlite3.DatabaseError:
        return []


def _metadata(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _push_proof(source: dict[str, Any], latest_commit: str | None) -> dict[str, str] | None:
    metadata = _metadata(source.get("metadata")) if not isinstance(source.get("metadata"), dict) else dict(source.get("metadata") or {})
    proof = metadata.get("push_proof")
    if not isinstance(proof, dict):
        return None
    proof_source = str(proof.get("proof_source") or "").strip().upper()
    provider_event_id = str(proof.get("provider_event_id") or "").strip()
    after_sha = str(proof.get("after_sha") or "").strip()
    if proof_source not in {"PROVIDER_API", "PROVIDER_WEBHOOK", "REFLOG_PUSH"}:
        return None
    if not provider_event_id or not after_sha or (latest_commit and after_sha != latest_commit):
        return None
    return {
        "provider_event_id": provider_event_id,
        "ref_name": str(proof.get("ref_name") or ""),
        "before_sha": str(proof.get("before_sha") or ""),
        "after_sha": after_sha,
        "pushed_at": str(proof.get("pushed_at") or ""),
        "proof_source": proof_source,
    }


def _external_good_snapshot(brain_root: Path, source_id: str) -> tuple[str, str] | None:
    database = brain_root / "brain_versions" / "semantic_brain_diff.sqlite"
    if not database.is_file():
        return None
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_code_good_snapshot'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute(
            "SELECT code_good_snapshot_id,code_snapshot_sha256 FROM brain_code_good_snapshot "
            "WHERE source_id=? AND verification_status='PASS' ORDER BY created_at DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None
    finally:
        connection.close()


def ingest_env15_code_source(
    brain_root: str | Path,
    lane_id: str,
    source: dict[str, Any],
    *,
    legacy_builder: Callable[..., Any],
    progress: Callable[[dict], None] | None = None,
    started: float = 0,
) -> dict[str, Any]:
    if lane_id not in {"local_code", "github_code"}:
        raise Env15CodeIngestionError(f"ENV15_CODE_LANE_INVALID:{lane_id}")
    source_path = Path(str(source.get("path") or "")).expanduser().resolve(strict=True)
    source_id = str(source.get("source_id") or _stable_id("source", lane_id, str(source_path).casefold()))
    sector_id, destination = resolve_env15_sector(brain_root, lane_id)
    root = Path(brain_root).resolve()
    destination_hash_before = _sha256_file(destination) if destination.is_file() else "MISSING"
    cached_hash, fingerprint_probe = cached_content_hash(
        brain_root,
        lane_id,
        source_id,
        source_path,
        skip_dirs=CODE_SKIP_DIRS,
    )

    read_only = sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True)
    try:
        existing = read_only.execute(
            "SELECT csr.snapshot_sha256,sr.availability_state FROM code_source_registry csr "
            "JOIN source_registry sr ON sr.source_id=csr.source_id WHERE csr.source_id=?", (source_id,)
        ).fetchone()
        has_projection_state = read_only.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='code_projection_state'"
        ).fetchone()
        projection_current = bool(
            has_projection_state
            and read_only.execute(
                "SELECT 1 FROM code_projection_state WHERE source_id=? AND projection_version=? LIMIT 1",
                (source_id, _CODE_PROJECTION_VERSION),
            ).fetchone()
        )
    finally:
        read_only.close()
    if (
        cached_hash
        and existing
        and existing[0] == cached_hash
        and existing[1] == "AVAILABLE"
        and projection_current
    ):
        return {"status": "SKIPPED_UNCHANGED", "source_id": source_id, "sector_id": sector_id}

    with tempfile.TemporaryDirectory(prefix="evidenceos_env15_code_adapter_", dir=root / "receipts") as temporary:
        staging_db = Path(temporary) / "legacy_code_staging.sqlite"
        # Legacy parsing may preserve non-code artifacts in a vault.  That
        # behavior is useful inside the disposable adapter, but must not copy
        # reference-app UI assets into the governed Evidence OS brain outside
        # the sector mutation transaction.
        staging_project = Path(temporary) / "project_staging"
        try:
            legacy_builder(
                staging_db,
                source,
                staging_project,
                progress,
                lane_id,
                started,
                preserve_artifact_payloads=False,
            )
        except Exception as exc:
            receipt = _write_failure_receipt(
                root,
                lane_id=lane_id,
                source_id=source_id,
                source_path=source_path,
                operation="legacy_code_staging_build",
                destination=destination,
                destination_hash_before=destination_hash_before,
                exc=exc,
            )
            raise Env15CodeIngestionError(
                f"CODE_STAGING_BUILD_FAILED:{type(exc).__name__}:{exc}:receipt={receipt}"
            ) from exc
        staging = sqlite3.connect(staging_db)
        staging.row_factory = sqlite3.Row
        try:
            files = _rows(
                staging,
                "SELECT sf.file_id,sf.logical_path canonical_path,cf.language,sf.sha256 current_sha256,"
                "COALESCE(sf.size_bytes,0) size_bytes,sf.lane_status FROM source_file sf "
                "LEFT JOIN code_file cf ON cf.file_id=sf.file_id ORDER BY sf.logical_path",
            )
            coverages = _rows(
                staging,
                "SELECT file_id,coverage_status,byte_count,sha256,notes,created_at "
                "FROM source_byte_coverage ORDER BY file_id",
            )
            file_versions = _rows(
                staging,
                "SELECT file_version_id,file_id,commit_sha,raw_file_sha256,normalized_text_sha256,"
                "path_at_commit,language,extension,line_count,byte_count,is_deleted,is_renamed,created_at "
                "FROM code_file_version ORDER BY file_id,file_version_id",
            )
            line_snapshots = _rows(
                staging,
                "SELECT line_id,file_version_id,file_id,line_number,line_text,line_sha256,"
                "normalized_line_sha256,indent_level,is_blank,is_comment,search_text "
                "FROM code_line_snapshot ORDER BY file_id,line_number",
            )
            chunks = _rows(
                staging,
                "SELECT chunk_id,file_version_id,file_id,chunk_type,language,role_name,"
                "start_line,end_line,chunk_text,chunk_sha256 "
                "FROM code_chunk ORDER BY file_id,start_line",
            )
            symbols = _rows(
                staging,
                "SELECT symbol_id,symbol_name,symbol_type,file_version_id,file_id,language,"
                "start_line,end_line,parent_symbol_id,signature,symbol_sha256 "
                "FROM code_symbol ORDER BY file_id,start_line",
            )
            imports = _rows(
                staging,
                "SELECT edge_id,from_file_id,from_path,import_target,import_type,line_number "
                "FROM code_import_edge ORDER BY from_file_id,line_number",
            )
            routes = _rows(
                staging,
                "SELECT route_id,route_path,route_type,file_id,file_version_id,route_sha256 "
                "FROM app_route ORDER BY route_path",
            )
            commits = _rows(
                staging,
                "SELECT commit_sha,author_name,author_email_hash,author_time,committer_name,"
                "committer_email_hash,committer_time,commit_time,message,commit_order "
                "FROM git_commit ORDER BY commit_order",
            )
            parents = _rows(
                staging,
                "SELECT commit_sha,parent_sha,parent_order FROM git_commit_parent_edge ORDER BY commit_sha,parent_order",
            )
            changes = _rows(
                staging,
                "SELECT id,name,path,value,metadata_json FROM git_file_change ORDER BY id",
            )
            hunks = _rows(
                staging,
                "SELECT hunk_id,commit_sha,old_path,new_path,old_start,old_count,new_start,new_count,hunk_header,patch_sha256 "
                "FROM git_diff_hunk ORDER BY commit_sha,new_path,new_start",
            )
            line_changes = _rows(
                staging,
                "SELECT line_change_id,hunk_id,commit_sha,path,old_line_number,new_line_number,"
                "change_type,line_text,line_sha256 FROM git_line_change "
                "ORDER BY hunk_id,old_line_number,new_line_number",
            )
            branches = _rows(staging, "SELECT id,name,value,metadata_json FROM git_branch ORDER BY id")
            remotes = _rows(staging, "SELECT id,name,value,metadata_json FROM git_remote ORDER BY id")
            dependencies = _rows(
                staging,
                "SELECT di.dependency_id,dm.file_id,di.package_name,di.version_spec "
                "FROM dependency_item di LEFT JOIN dependency_manifest dm ON dm.manifest_id=di.manifest_id "
                "ORDER BY di.package_name",
            )
            canonical_snapshots = {
                table: _table_snapshot(staging, table) for table in _V59_COPY_TABLES
            }
        finally:
            staging.close()

    parent_counts: dict[str, int] = {}
    for item in parents:
        parent_counts[item["commit_sha"]] = parent_counts.get(item["commit_sha"], 0) + 1
    lines_by_hunk: dict[str, list[str]] = {}
    for item in line_changes:
        prefix = "+" if item["change_type"] == "ADD" else "-" if item["change_type"] == "DELETE" else " "
        lines_by_hunk.setdefault(item["hunk_id"], []).append(prefix + str(item["line_text"] or ""))
    commit_reverse_order = {str(item["commit_sha"]): int(item["commit_order"]) for item in commits}
    changes = sorted(
        changes,
        key=lambda item: (
            commit_reverse_order.get(str(_metadata(item["metadata_json"]).get("commit_sha") or item["value"] or ""), 10**9),
            int(_metadata(item["metadata_json"]).get("change_order") or 0),
            str(item["path"] or ""),
        ),
    )
    file_paths = {row["file_id"]: row["canonical_path"] for row in files}
    valid_file_ids = set(file_paths)
    file_statuses = {row["file_id"]: str(row["lane_status"] or "") for row in files}
    binary_file_ids = {
        file_id for file_id, relative_path in file_paths.items()
        if _code_content_kind(str(relative_path), file_statuses.get(file_id, "")) == "BINARY_METADATA_ONLY"
    }
    text_file_ids = valid_file_ids - binary_file_ids
    # The enhanced shared chunk table enforces a real-file foreign key.  V5.9
    # folder-layout nodes remain losslessly available through the copied
    # code_folder/workflow_node tables; only actual non-binary file chunks are
    # projected into this shared table.
    chunks = [row for row in chunks if row["file_id"] in text_file_ids]
    symbols = [row for row in symbols if row["file_id"] in text_file_ids]
    imports = [row for row in imports if row["from_file_id"] in text_file_ids]
    routes = [row for row in routes if row["file_id"] in text_file_ids]
    dependencies = [row for row in dependencies if not row["file_id"] or row["file_id"] in valid_file_ids]
    latest_commit = commits[0]["commit_sha"] if commits else None
    source_metadata = dict(source.get("metadata") or {}) if isinstance(source.get("metadata"), dict) else {}
    repository_url = str(
        source.get("url")
        or source_metadata.get("repo_url")
        or (remotes[0]["value"] if remotes else "")
        or ""
    )
    current_branch = next(
        (
            item["name"]
            for item in branches
            if bool(_metadata(item["metadata_json"]).get("is_current"))
        ),
        "",
    )
    branch_name = str(
        source.get("branch")
        or source_metadata.get("branch")
        or current_branch
        or (branches[0]["name"] if branches else "")
        or ""
    )
    push_proof = _push_proof(source, latest_commit)
    snapshot_material = json.dumps(
        {
            "files": [
                {
                    "path": item["canonical_path"],
                    "sha256": item["current_sha256"],
                    "size_bytes": int(item["size_bytes"] or 0),
                }
                for item in files
            ],
            "git_head": latest_commit,
            "lane_id": lane_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    snapshot_sha256 = hashlib.sha256(snapshot_material).hexdigest()
    snapshot_size = sum(int(item["size_bytes"] or 0) for item in files)
    if (
        existing
        and existing[0] == snapshot_sha256
        and existing[1] == "AVAILABLE"
        and projection_current
    ):
        record_content_hash(brain_root, lane_id, source_id, snapshot_sha256, fingerprint_probe)
        return {"status": "SKIPPED_CONTENT_UNCHANGED", "source_id": source_id, "sector_id": sector_id}
    turn_id = _stable_id("turn", source_id, snapshot_sha256)[:48]
    external_prior_good = _external_good_snapshot(root, source_id)

    def mutate(connection: sqlite3.Connection) -> dict[str, int]:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS code_source_active_head(
              source_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL, active_ref TEXT,
              head_commit_sha TEXT, snapshot_sha256 TEXT NOT NULL,
              proof_status TEXT NOT NULL, updated_turn TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_workflow_edge(
              edge_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              from_type TEXT NOT NULL, from_id TEXT NOT NULL,
              relation_type TEXT NOT NULL, to_type TEXT NOT NULL, to_id TEXT NOT NULL,
              evidence_ref TEXT
            );
            CREATE TABLE IF NOT EXISTS code_semantic_diff(
              diff_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              diff_type TEXT NOT NULL, prior_snapshot_sha256 TEXT,
              prior_good_snapshot_id TEXT,
              current_snapshot_sha256 TEXT NOT NULL, status TEXT NOT NULL,
              summary_json TEXT NOT NULL, created_turn TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_synthetic_snapshot_file(
              diff_id TEXT NOT NULL, file_id TEXT NOT NULL, relative_path TEXT NOT NULL,
              change_type TEXT NOT NULL, prior_sha256 TEXT, current_sha256 TEXT,
              impact_json TEXT NOT NULL,
              PRIMARY KEY(diff_id,file_id,relative_path)
            );
            CREATE TABLE IF NOT EXISTS code_good_snapshot(
              good_snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              snapshot_sha256 TEXT NOT NULL, head_commit_sha TEXT,
              verification_status TEXT NOT NULL, created_turn TEXT NOT NULL,
              current_flag INTEGER NOT NULL CHECK(current_flag IN (0,1)),
              UNIQUE(source_id,snapshot_sha256)
            );
            CREATE TABLE IF NOT EXISTS code_snapshot_history(
              snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              snapshot_sha256 TEXT NOT NULL, head_commit_sha TEXT,
              verification_status TEXT NOT NULL, created_turn TEXT NOT NULL,
              UNIQUE(source_id,snapshot_sha256)
            );
            CREATE TABLE IF NOT EXISTS git_exact_line_change(
              line_change_id TEXT PRIMARY KEY, hunk_id TEXT NOT NULL,
              commit_sha TEXT NOT NULL, path TEXT NOT NULL,
              old_line_number INTEGER, new_line_number INTEGER,
              change_type TEXT NOT NULL, line_text TEXT NOT NULL,
              line_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_projection_state(
              source_id TEXT PRIMARY KEY, projection_version TEXT NOT NULL,
              snapshot_sha256 TEXT NOT NULL, updated_turn TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_byte_coverage(
              file_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              coverage_status TEXT NOT NULL, byte_count INTEGER NOT NULL,
              sha256 TEXT NOT NULL, notes TEXT NOT NULL, created_turn TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_file_version(
              file_version_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              file_id TEXT NOT NULL, commit_sha TEXT, raw_file_sha256 TEXT NOT NULL,
              normalized_text_sha256 TEXT, path_at_commit TEXT NOT NULL,
              language TEXT, extension TEXT, line_count INTEGER NOT NULL,
              byte_count INTEGER NOT NULL, is_deleted INTEGER NOT NULL,
              is_renamed INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_line_snapshot(
              line_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              file_version_id TEXT NOT NULL, file_id TEXT NOT NULL,
              line_number INTEGER NOT NULL, line_text TEXT NOT NULL,
              line_sha256 TEXT NOT NULL, normalized_line_sha256 TEXT NOT NULL,
              indent_level INTEGER NOT NULL, is_blank INTEGER NOT NULL,
              is_comment INTEGER NOT NULL, search_text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS code_exact_byte_chunk(
              chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              file_id TEXT NOT NULL, relative_path TEXT NOT NULL,
              chunk_ordinal INTEGER NOT NULL, byte_start INTEGER NOT NULL,
              byte_end_exclusive INTEGER NOT NULL, compression TEXT NOT NULL,
              compressed_payload BLOB NOT NULL, raw_chunk_sha256 TEXT NOT NULL,
              UNIQUE(source_id,file_id,chunk_ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_code_exact_byte_chunk_file_ordinal
              ON code_exact_byte_chunk(source_id,file_id,chunk_ordinal);
            CREATE INDEX IF NOT EXISTS idx_code_line_snapshot_file_line
              ON code_line_snapshot(source_id,file_id,line_number);
            """
        )
        _ensure_v59_compatibility_schema(connection)
        commit_columns = {row[1] for row in connection.execute("PRAGMA table_info(git_commit_registry)")}
        for column in ("author_email_hash", "committer_email_hash"):
            if column not in commit_columns:
                connection.execute(f"ALTER TABLE git_commit_registry ADD COLUMN {column} TEXT")
        semantic_diff_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(code_semantic_diff)")
        }
        if "prior_good_snapshot_id" not in semantic_diff_columns:
            connection.execute("ALTER TABLE code_semantic_diff ADD COLUMN prior_good_snapshot_id TEXT")
        file_snapshot_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(code_file_snapshot)")
        }
        if "content_kind" not in file_snapshot_columns:
            connection.execute(
                "ALTER TABLE code_file_snapshot ADD COLUMN content_kind TEXT NOT NULL DEFAULT 'TEXT_INDEXED'"
            )
        prior_good = external_prior_good
        old_file_ids = [row[0] for row in connection.execute(
            "SELECT file_id FROM code_file_snapshot WHERE source_id=?", (source_id,)
        )]
        old_commits = [row[0] for row in connection.execute(
            "SELECT commit_sha FROM git_commit_registry WHERE source_id=?", (source_id,)
        )]
        _delete_v59_projection(connection, source_id, old_file_ids, old_commits)
        prior_files = {
            row[0]: {"file_id": row[1], "sha256": row[2]}
            for row in connection.execute(
                "SELECT relative_path,file_id,sha256 FROM code_file_snapshot WHERE source_id=?", (source_id,)
            )
        }
        prior_path_by_id = {item["file_id"]: path for path, item in prior_files.items()}
        for file_id in old_file_ids:
            for table in ("code_chunk", "code_symbol", "code_import", "code_route_api_boundary", "code_config_build_test_chunk"):
                connection.execute(f'DELETE FROM "{table}" WHERE file_id=?', (file_id,))
            connection.execute("DELETE FROM code_chunk_fts WHERE relative_path=?", (prior_path_by_id.get(file_id, ""),))
        connection.execute("DELETE FROM source_byte_coverage WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_file_version WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_line_snapshot WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_exact_byte_chunk WHERE source_id=?", (source_id,))
        for commit_sha in old_commits:
            connection.execute("DELETE FROM git_patch_hunk WHERE change_id IN (SELECT change_id FROM git_file_change WHERE commit_sha=?)", (commit_sha,))
            connection.execute("DELETE FROM git_file_change WHERE commit_sha=?", (commit_sha,))
            connection.execute("DELETE FROM git_commit_parent WHERE commit_sha=?", (commit_sha,))
            connection.execute("DELETE FROM git_exact_line_change WHERE commit_sha=?", (commit_sha,))
            for table in ("git_route_impact", "git_symbol_impact", "git_dependency_impact", "git_test_impact", "git_artifact_impact"):
                connection.execute(f'DELETE FROM "{table}" WHERE commit_sha=?', (commit_sha,))
        connection.execute("DELETE FROM git_commit_registry WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM git_ref_registry WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM git_push_event WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM snapshot_git_bridge WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_index_checkpoint WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_file_snapshot WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_source_registry WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM chunk_index WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM artifact_registry WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM source_registry WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_source_active_head WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_workflow_edge WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM code_projection_state WHERE source_id=?", (source_id,))

        canonical_projection_rows = {
            table: _insert_table_snapshot(connection, table, snapshot)
            for table, snapshot in canonical_snapshots.items()
        }

        connection.execute(
            "INSERT INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
            (source_id, lane_id, source_path.name, str(source_path), snapshot_sha256, snapshot_size, "AVAILABLE", turn_id),
        )
        connection.execute(
            "INSERT INTO code_source_registry VALUES(?,?,?,?,?,?,?,?,?)",
            (
                source_id, lane_id, str(source_path), repository_url,
                branch_name, latest_commit, 1 if commits else 0,
                snapshot_sha256, turn_id,
            ),
        )
        proof_status = "PUSH_PROVEN" if push_proof else "REF_SNAPSHOT_ONLY"
        connection.execute(
            "INSERT INTO code_source_active_head VALUES(?,?,?,?,?,?,?)",
            (source_id, lane_id, branch_name, latest_commit, snapshot_sha256, proof_status, turn_id),
        )
        connection.execute(
            "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
            (
                _stable_id("workflow", source_id, "snapshot_head", snapshot_sha256), source_id,
                "source", source_id, "HAS_SNAPSHOT_HEAD", "source_snapshot_head",
                snapshot_sha256, latest_commit or "SNAPSHOT_ONLY",
            ),
        )
        if push_proof:
            connection.execute(
                "INSERT INTO git_push_event VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _stable_id("push", source_id, push_proof["provider_event_id"]), source_id,
                    push_proof["provider_event_id"], push_proof["ref_name"],
                    push_proof["before_sha"], push_proof["after_sha"], push_proof["pushed_at"],
                    push_proof["proof_source"],
                    "REFLOG_PROVEN" if push_proof["proof_source"] == "REFLOG_PUSH" else "PROVIDER_API_PROVEN",
                ),
            )
        for item in files:
            connection.execute(
                "INSERT INTO code_file_snapshot("
                "file_id,source_id,relative_path,language,size_bytes,sha256,current_snapshot,content_kind"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    item["file_id"], source_id, item["canonical_path"], item["language"],
                    item["size_bytes"], item["current_sha256"], snapshot_sha256,
                    _code_content_kind(str(item["canonical_path"]), str(item["lane_status"] or "")),
                ),
            )
            connection.execute(
                "INSERT INTO artifact_registry VALUES(?,?,?,?,?,?,?)",
                (
                    _stable_id("artifact", source_id, item["file_id"]), source_id,
                    "code_binary_metadata" if item["file_id"] in binary_file_ids else "code_file",
                    item["canonical_path"], item["current_sha256"], item["size_bytes"],
                    "HASH_LINKED_METADATA_ONLY" if item["file_id"] in binary_file_ids else "INDEXED",
                ),
            )
            artifact_id = _stable_id("artifact", source_id, item["file_id"])
            connection.execute(
                "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                (_stable_id("workflow", source_id, "source_file", item["file_id"]), source_id, "source", source_id, "CONTAINS_FILE", "file", item["file_id"], item["canonical_path"]),
            )
            connection.execute(
                "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                (_stable_id("workflow", source_id, "file_artifact", item["file_id"]), source_id, "file", item["file_id"], "PROJECTS_ARTIFACT", "artifact", artifact_id, item["canonical_path"]),
            )
            if _is_test_path(str(item["canonical_path"])):
                connection.execute(
                    "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                    (_stable_id("workflow", source_id, "file_test", item["file_id"]), source_id, "file", item["file_id"], "IMPLEMENTS_TEST", "test", item["file_id"], item["canonical_path"]),
                )
                connection.execute(
                    "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                    (_stable_id("workflow", source_id, "test_artifact", item["file_id"]), source_id, "test", item["file_id"], "PRODUCES_TEST_ARTIFACT", "artifact", artifact_id, item["canonical_path"]),
                )

        connection.executemany(
            "INSERT INTO source_byte_coverage("
            "file_id,source_id,coverage_status,byte_count,sha256,notes,created_turn,created_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    item["file_id"], source_id, item["coverage_status"],
                    item["byte_count"], item["sha256"], item["notes"], turn_id,
                    item["created_at"] or turn_id,
                )
                for item in coverages
            ],
        )
        connection.executemany(
            "INSERT INTO code_file_version("
            "file_version_id,source_id,file_id,commit_sha,raw_file_sha256,normalized_text_sha256,"
            "path_at_commit,language,extension,line_count,byte_count,is_deleted,is_renamed,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["file_version_id"], source_id, item["file_id"], item["commit_sha"],
                    item["raw_file_sha256"], item["normalized_text_sha256"], item["path_at_commit"],
                    item["language"], item["extension"], item["line_count"], item["byte_count"],
                    item["is_deleted"], item["is_renamed"], item["created_at"],
                )
                for item in file_versions
            ],
        )
        current_file_version_by_file: dict[str, str] = {}
        for item in file_versions:
            if int(item["is_deleted"] or 0):
                continue
            file_id = str(item["file_id"])
            current_file_version_by_file[file_id] = str(item["file_version_id"])
        connection.executemany(
            "INSERT INTO code_line_snapshot("
            "line_id,source_id,file_version_id,file_id,line_number,line_text,line_sha256,"
            "normalized_line_sha256,indent_level,is_blank,is_comment,search_text"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    item["line_id"], source_id, item["file_version_id"], item["file_id"],
                    item["line_number"], item["line_text"], item["line_sha256"],
                    item["normalized_line_sha256"], item["indent_level"], item["is_blank"],
                    item["is_comment"], item["search_text"],
                )
                for item in line_snapshots
            ],
        )

        exact_byte_rows = 0
        exact_byte_count = 0
        artifact_text_rows = 0
        source_root = source_path.resolve()
        for item in files:
            file_id = str(item["file_id"])
            relative_path = str(item["canonical_path"])
            lane_status = str(item["lane_status"] or "")
            if _code_content_kind(relative_path, lane_status) != "TEXT_INDEXED":
                continue
            physical_path = (source_root / Path(relative_path)).resolve(strict=True)
            if source_root not in physical_path.parents:
                raise Env15CodeIngestionError(f"CODE_SOURCE_PATH_ESCAPE:{relative_path}")
            overall = hashlib.sha256()
            total_bytes = 0
            with physical_path.open("rb") as stream:
                chunk_ordinal = 0
                while True:
                    raw = stream.read(_EXACT_BYTE_CHUNK_SIZE)
                    if not raw:
                        break
                    chunk_ordinal += 1
                    byte_start = total_bytes
                    total_bytes += len(raw)
                    overall.update(raw)
                    raw_sha256 = hashlib.sha256(raw).hexdigest()
                    connection.execute(
                        "INSERT INTO code_exact_byte_chunk("
                        "chunk_id,source_id,file_id,relative_path,chunk_ordinal,byte_start,"
                        "byte_end_exclusive,compression,compressed_payload,raw_chunk_sha256"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            _stable_id("exact_byte", source_id, file_id, chunk_ordinal, raw_sha256),
                            source_id, file_id, relative_path, chunk_ordinal, byte_start, total_bytes,
                            "zlib", sqlite3.Binary(zlib.compress(raw, level=6)), raw_sha256,
                        ),
                    )
                    exact_byte_rows += 1
                    exact_byte_count += len(raw)
            if total_bytes != int(item["size_bytes"] or 0) or overall.hexdigest() != str(item["current_sha256"]):
                raise Env15CodeIngestionError(f"CODE_EXACT_BYTE_RECHECK_FAILED:{relative_path}")

            if lane_status not in _ARTIFACT_TEXT_STATUSES:
                continue
            raw_file = physical_path.read_bytes()
            if b"\x00" in raw_file:
                continue
            decoded, encoding = _decode_text_lossless(raw_file)
            line_cursor = 1
            for offset in range(0, len(decoded), _ARTIFACT_TEXT_CHUNK_CHARACTERS):
                text_chunk = decoded[offset : offset + _ARTIFACT_TEXT_CHUNK_CHARACTERS]
                if not text_chunk:
                    continue
                start_line = line_cursor
                newline_count = text_chunk.count("\n")
                end_line = start_line + newline_count
                line_cursor = end_line + (0 if text_chunk.endswith("\n") else 1)
                chunk_sha256 = hashlib.sha256(text_chunk.encode("utf-8", "surrogatepass")).hexdigest()
                chunk_id = _stable_id("artifact_text", source_id, file_id, offset, chunk_sha256)
                file_version_id = current_file_version_by_file.get(file_id)
                if not file_version_id:
                    file_version_id = _stable_id(
                        "code_artifact_file_version",
                        source_id,
                        file_id,
                        item["current_sha256"],
                    )
                    normalized_sha256 = hashlib.sha256(
                        decoded.encode("utf-8", "surrogatepass")
                    ).hexdigest()
                    connection.execute(
                        "INSERT INTO code_file_version("
                        "file_version_id,source_id,file_id,commit_sha,raw_file_sha256,"
                        "normalized_text_sha256,path_at_commit,language,extension,line_count,"
                        "byte_count,is_deleted,is_renamed,created_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            file_version_id,
                            source_id,
                            file_id,
                            latest_commit,
                            item["current_sha256"],
                            normalized_sha256,
                            relative_path,
                            item["language"] or encoding,
                            Path(relative_path).suffix.casefold(),
                            len(decoded.splitlines()),
                            item["size_bytes"],
                            0,
                            0,
                            turn_id,
                        ),
                    )
                    current_file_version_by_file[file_id] = file_version_id
                connection.execute(
                    "INSERT INTO code_chunk("
                    "chunk_id,file_id,chunk_type,start_line,end_line,chunk_text,chunk_sha256,"
                    "file_version_id,language,role_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        chunk_id,
                        file_id,
                        f"ARTIFACT_TEXT_CHUNK:{encoding}",
                        start_line,
                        end_line,
                        text_chunk,
                        chunk_sha256,
                        file_version_id,
                        encoding,
                        "CODE_ARTIFACT",
                    ),
                )
                connection.execute(
                    "INSERT INTO code_chunk_fts(chunk_id,relative_path,symbol_name,route_name,chunk_text) VALUES(?,?,?,?,?)",
                    (chunk_id, relative_path, "", "", text_chunk),
                )
                connection.execute(
                    "INSERT INTO chunk_index VALUES(?,?,?,?,?,?,?,?)",
                    (
                        chunk_id, source_id, _stable_id("artifact", source_id, file_id),
                        "code_artifact_text",
                        len(chunks) + artifact_text_rows + 1,
                        text_chunk,
                        chunk_sha256,
                        max(1, len(text_chunk) // 4),
                    ),
                )
                artifact_text_rows += 1
        for ordinal, item in enumerate(chunks, start=1):
            connection.execute(
                "INSERT INTO code_chunk("
                "chunk_id,file_id,chunk_type,start_line,end_line,chunk_text,chunk_sha256,"
                "file_version_id,language,role_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(item[key] for key in (
                    "chunk_id", "file_id", "chunk_type", "start_line", "end_line",
                    "chunk_text", "chunk_sha256", "file_version_id", "language", "role_name",
                )),
            )
            relative = file_paths.get(item["file_id"], "")
            connection.execute(
                "INSERT INTO code_chunk_fts(chunk_id,relative_path,symbol_name,route_name,chunk_text) VALUES(?,?,?,?,?)",
                (item["chunk_id"], relative, "", "", item["chunk_text"]),
            )
            connection.execute(
                "INSERT INTO chunk_index VALUES(?,?,?,?,?,?,?,?)",
                (item["chunk_id"], source_id, _stable_id("artifact", source_id, item["file_id"]), "code", ordinal, item["chunk_text"], item["chunk_sha256"], max(1, len(item["chunk_text"] or "") // 4)),
            )
        for item in symbols:
            connection.execute(
                "INSERT INTO code_symbol("
                "symbol_id,file_id,symbol_kind,qualified_name,start_line,end_line,signature,"
                "symbol_name,symbol_type,file_version_id,language,parent_symbol_id,symbol_sha256"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["symbol_id"], item["file_id"], item["symbol_type"], item["symbol_name"],
                    item["start_line"], item["end_line"], item["signature"], item["symbol_name"],
                    item["symbol_type"], item["file_version_id"], item["language"],
                    item["parent_symbol_id"], item["symbol_sha256"],
                ),
            )
            connection.execute(
                "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                (_stable_id("workflow", source_id, "file_symbol", item["symbol_id"]), source_id, "file", item["file_id"], "DECLARES_SYMBOL", "symbol", item["symbol_id"], item["signature"]),
            )
        for item in imports:
            connection.execute(
                "INSERT INTO code_import VALUES(?,?,?,?,?)",
                (item["edge_id"], item["from_file_id"], item["import_target"], item["import_type"], item["line_number"]),
            )
            connection.execute(
                "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                (_stable_id("workflow", source_id, "file_import", item["edge_id"]), source_id, "file", item["from_file_id"], "IMPORTS_DEPENDENCY", "dependency", item["import_target"], str(item["line_number"])),
            )
        for item in routes:
            connection.execute(
                "INSERT INTO code_route_api_boundary VALUES(?,?,?,?,?,?,?)",
                (item["route_id"], item["file_id"], item["route_path"], None, None, None, None),
            )
            connection.execute(
                "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                (_stable_id("workflow", source_id, "file_route", item["route_id"]), source_id, "file", item["file_id"], "EXPOSES_ROUTE", "route", item["route_id"], item["route_path"]),
            )
            for symbol in symbols:
                if symbol["file_id"] == item["file_id"]:
                    connection.execute(
                        "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                        (_stable_id("workflow", source_id, "symbol_route", symbol["symbol_id"], item["route_id"]), source_id, "symbol", symbol["symbol_id"], "SERVES_ROUTE", "route", item["route_id"], item["route_path"]),
                    )
            for test_file in files:
                if _is_test_path(str(test_file["canonical_path"])):
                    connection.execute(
                        "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                        (_stable_id("workflow", source_id, "route_test", item["route_id"], test_file["file_id"]), source_id, "route", item["route_id"], "VERIFIED_BY_TEST", "test", test_file["file_id"], test_file["canonical_path"]),
                    )
        for item in dependencies:
            connection.execute(
                "INSERT INTO code_config_build_test_chunk VALUES(?,?,?,?,?,?,?)",
                (item["dependency_id"], item["file_id"], "dependency", item["package_name"], item["version_spec"], None, None),
            )
            connection.execute(
                "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
                (_stable_id("workflow", source_id, "file_dependency", item["dependency_id"]), source_id, "file", item["file_id"] or source_id, "DECLARES_DEPENDENCY", "dependency", item["dependency_id"], item["package_name"]),
            )
        for item in commits:
            connection.execute(
                "INSERT INTO git_commit_registry("
                "commit_sha,source_id,authored_at,committed_at,author_name,committer_name,subject,body,"
                "parent_count,reverse_sequence,author_email_hash,committer_email_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["commit_sha"], source_id, item["author_time"] or item["commit_time"],
                    item["committer_time"] or item["commit_time"], item["author_name"],
                    item["committer_name"] or item["author_name"], str(item["message"] or "").splitlines()[0],
                    item["message"], parent_counts.get(item["commit_sha"], 0), item["commit_order"],
                    item["author_email_hash"], item["committer_email_hash"],
                ),
            )
        for item in parents:
            connection.execute("INSERT INTO git_commit_parent VALUES(?,?,?)", tuple(item))
        change_ids: dict[tuple[str, str], str] = {}
        for item in changes:
            meta = _metadata(item["metadata_json"])
            commit_sha = str(meta.get("commit_sha") or meta.get("commit") or item["value"] or "")
            change_id = item["id"]
            old_path = str(meta.get("old_path") or item["path"] or item["name"] or "")
            new_path = str(meta.get("new_path") or item["path"] or item["name"] or "")
            connection.execute(
                "INSERT OR IGNORE INTO git_file_change("
                "change_id,commit_sha,old_path,new_path,change_type,additions,deletions,binary_change,"
                "id,source_id,name,path,value,metadata_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    change_id, commit_sha, old_path, new_path,
                    str(meta.get("change_type") or item["name"] or "MODIFY"),
                    int(meta.get("additions") or 0), int(meta.get("deletions") or 0),
                    int(bool(meta.get("binary"))), change_id, source_id, item["name"],
                    item["path"], item["value"], item["metadata_json"], turn_id,
                ),
            )
            change_ids[(commit_sha, new_path)] = change_id
        for item in hunks:
            change_id = change_ids.get((item["commit_sha"], item["new_path"])) or _stable_id("change", item["commit_sha"], item["new_path"])
            fallback_metadata = json.dumps(
                {
                    "commit_sha": item["commit_sha"],
                    "old_path": item["old_path"],
                    "new_path": item["new_path"],
                    "change_type": "MODIFY",
                },
                sort_keys=True,
            )
            connection.execute(
                "INSERT OR IGNORE INTO git_file_change("
                "change_id,commit_sha,old_path,new_path,change_type,additions,deletions,binary_change,"
                "id,source_id,name,path,value,metadata_json,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    change_id, item["commit_sha"], item["old_path"], item["new_path"],
                    "MODIFY", 0, 0, 0, change_id, source_id, "MODIFY",
                    item["new_path"], item["commit_sha"], fallback_metadata, turn_id,
                ),
            )
            patch_text = item["hunk_header"] + "\n" + "\n".join(lines_by_hunk.get(item["hunk_id"], []))
            connection.execute(
                "INSERT INTO git_patch_hunk VALUES(?,?,?,?,?,?,?,?,?)",
                (item["hunk_id"], change_id, item["old_start"], item["old_count"], item["new_start"], item["new_count"], item["hunk_header"], patch_text, item["patch_sha256"]),
            )
        connection.executemany(
            "INSERT OR REPLACE INTO git_exact_line_change VALUES(?,?,?,?,?,?,?,?,?)",
            [tuple(item[key] for key in (
                "line_change_id", "hunk_id", "commit_sha", "path", "old_line_number",
                "new_line_number", "change_type", "line_text", "line_sha256",
            )) for item in line_changes],
        )
        routes_by_file: dict[str, list[sqlite3.Row]] = {}
        symbols_by_file: dict[str, list[sqlite3.Row]] = {}
        dependencies_by_file: dict[str, list[sqlite3.Row]] = {}
        for item in routes:
            routes_by_file.setdefault(str(item["file_id"]), []).append(item)
        for item in symbols:
            symbols_by_file.setdefault(str(item["file_id"]), []).append(item)
        for item in dependencies:
            if item["file_id"]:
                dependencies_by_file.setdefault(str(item["file_id"]), []).append(item)
        for item in changes:
            meta = _metadata(item["metadata_json"])
            commit_sha = str(meta.get("commit_sha") or item["value"] or "")
            path = str(meta.get("new_path") or meta.get("old_path") or item["path"] or "")
            file_id = str(meta.get("file_id") or "")
            impact_kind = str(meta.get("change_type") or item["name"] or "MODIFY")
            for route in routes_by_file.get(file_id, []):
                connection.execute(
                    "INSERT OR REPLACE INTO git_route_impact VALUES(?,?,?,?,?,?)",
                    (_stable_id("route_impact", commit_sha, route["route_id"]), commit_sha, route["route_path"], impact_kind, path, "EXACT_CURRENT_OBJECT_BRIDGE"),
                )
            for symbol in symbols_by_file.get(file_id, []):
                connection.execute(
                    "INSERT OR REPLACE INTO git_symbol_impact VALUES(?,?,?,?,?)",
                    (_stable_id("symbol_impact", commit_sha, symbol["symbol_id"]), commit_sha, symbol["symbol_name"], impact_kind, path),
                )
            for dependency in dependencies_by_file.get(file_id, []):
                connection.execute(
                    "INSERT OR REPLACE INTO git_dependency_impact VALUES(?,?,?,?,?,?)",
                    (_stable_id("dependency_impact", commit_sha, dependency["dependency_id"]), commit_sha, dependency["package_name"], None, dependency["version_spec"], path),
                )
            lower_path = path.casefold()
            if _is_test_path(path):
                connection.execute(
                    "INSERT OR REPLACE INTO git_test_impact VALUES(?,?,?,?)",
                    (_stable_id("test_impact", commit_sha, path), commit_sha, path, impact_kind),
                )
            name = Path(lower_path).name
            artifact_kind = "CONFIG_BUILD_CHANGE" if any(
                token in name for token in ("config", "package", "requirements", "pyproject", "dockerfile", "workflow")
            ) else "PROJECT_ARTIFACT_CHANGE"
            connection.execute(
                "INSERT OR REPLACE INTO git_artifact_impact VALUES(?,?,?,?,?)",
                (_stable_id("artifact_impact", commit_sha, path), commit_sha, path, artifact_kind, 1),
            )
        for item in branches:
            meta = _metadata(item["metadata_json"])
            connection.execute(
                "INSERT OR IGNORE INTO git_ref_registry VALUES(?,?,?,?,?)",
                (item["id"], source_id, item["name"], str(meta.get("commit_sha") or item["value"] or latest_commit or ""), turn_id),
            )
        last_change_by_path: dict[str, str] = {}
        for item in changes:
            meta = _metadata(item["metadata_json"])
            path = str(meta.get("new_path") or meta.get("old_path") or item["path"] or "")
            commit_sha = str(meta.get("commit_sha") or item["value"] or "")
            if path and commit_sha and path not in last_change_by_path:
                last_change_by_path[path] = commit_sha
        checkpoint_id = _stable_id("checkpoint", source_id, snapshot_sha256)
        connection.execute(
            "INSERT INTO code_index_checkpoint VALUES(?,?,?,?,?,?,?,?,?)",
            (
                checkpoint_id, source_id, latest_commit, snapshot_sha256, latest_commit,
                commits[-1]["commit_sha"] if commits else None, len(files),
                len(chunks) + artifact_text_rows, turn_id,
            ),
        )
        for item in files:
            connection.execute(
                "INSERT INTO snapshot_git_bridge VALUES(?,?,?,?,?,?)",
                (_stable_id("bridge", source_id, item["file_id"]), source_id, item["file_id"], latest_commit, last_change_by_path.get(item["canonical_path"]), proof_status),
            )
        current_files = {
            item["canonical_path"]: {"file_id": item["file_id"], "sha256": item["current_sha256"]}
            for item in files
        }
        diff_type = "GIT_REVERSE_HISTORY" if commits else "SNAPSHOT_DIFF_NOT_GIT_HISTORY"
        prior_snapshot_sha = prior_good[1] if prior_good else (existing[0] if existing else None)
        prior_good_snapshot_id = prior_good[0] if prior_good else None
        diff_id = _stable_id("semantic_diff", source_id, prior_snapshot_sha or "", snapshot_sha256, diff_type)
        synthetic_rows = []
        if not commits:
            all_paths = sorted(set(prior_files) | set(current_files))
            route_files = {item["file_id"] for item in routes}
            symbol_files = {item["file_id"] for item in symbols}
            dependency_files = {item["file_id"] for item in dependencies if item["file_id"]}
            for relative_path in all_paths:
                prior = prior_files.get(relative_path)
                current = current_files.get(relative_path)
                if prior and current and prior["sha256"] == current["sha256"]:
                    continue
                change_type = "ADD" if current and not prior else "DELETE" if prior and not current else "MODIFY"
                file_id = str((current or prior)["file_id"])
                lower = relative_path.casefold()
                impact = {
                    "route_changed": file_id in route_files,
                    "symbol_changed": file_id in symbol_files,
                    "dependency_changed": file_id in dependency_files,
                    "test_changed": _is_test_path(relative_path),
                    "config_changed": any(token in Path(lower).name for token in ("config", "manifest", "package", "requirements", "pyproject", "dockerfile")),
                }
                synthetic_rows.append((diff_id, file_id, relative_path, change_type, prior["sha256"] if prior else None, current["sha256"] if current else None, json.dumps(impact, sort_keys=True)))
            connection.executemany(
                "INSERT INTO code_synthetic_snapshot_file VALUES(?,?,?,?,?,?,?)", synthetic_rows
            )
        summary = {
            "files": len(files), "chunks": len(chunks) + artifact_text_rows, "symbols": len(symbols),
            "routes": len(routes), "dependencies": len(dependencies), "commits": len(commits),
            "hunks": len(hunks), "workflow_edges": connection.execute("SELECT COUNT(*) FROM code_workflow_edge WHERE source_id=?", (source_id,)).fetchone()[0],
            "synthetic_file_changes": len(synthetic_rows), "push_proof": proof_status,
            "source_byte_coverage_rows": len(coverages),
            "code_file_version_rows": len(file_versions),
            "code_line_snapshot_rows": len(line_snapshots),
            "code_exact_byte_chunk_rows": exact_byte_rows,
            "code_exact_bytes_preserved": exact_byte_count,
            "code_artifact_text_chunk_rows": artifact_text_rows,
            "v59_projection_version": _CODE_PROJECTION_VERSION,
            "v59_canonical_table_rows": canonical_projection_rows,
            "v59_canonical_row_total": sum(canonical_projection_rows.values()),
            "prior_snapshot_basis": (
                "PREVIOUS_GOOD_CODE_SNAPSHOT" if prior_good
                else "PREVIOUS_INDEXED_SNAPSHOT_UNVERIFIED" if prior_files
                else "INITIAL_BASELINE_NO_VERIFIED_GOOD_SNAPSHOT"
            ),
            "prior_good_snapshot_id": prior_good_snapshot_id,
        }
        connection.execute(
            "INSERT OR REPLACE INTO code_semantic_diff("
            "diff_id,source_id,diff_type,prior_snapshot_sha256,prior_good_snapshot_id,"
            "current_snapshot_sha256,status,summary_json,created_turn) VALUES(?,?,?,?,?,?,?,?,?)",
            (diff_id, source_id, diff_type, prior_snapshot_sha, prior_good_snapshot_id, snapshot_sha256, "PASS", json.dumps(summary, sort_keys=True), turn_id),
        )
        connection.execute(
            "INSERT OR REPLACE INTO code_snapshot_history VALUES(?,?,?,?,?,?)",
            (_stable_id("code_snapshot", source_id, snapshot_sha256), source_id, snapshot_sha256, latest_commit, "PENDING_BUILD_TEST_PACKAGE_VALIDATION", turn_id),
        )
        connection.execute(
            "INSERT OR REPLACE INTO code_projection_state VALUES(?,?,?,?)",
            (source_id, _CODE_PROJECTION_VERSION, snapshot_sha256, turn_id),
        )
        connection.execute(
            "UPDATE sector_meta SET payload_state='POPULATED',status='ACTIVE'"
        )
        connection.execute(
            "UPDATE sector_head SET latest_turn_id=?,current_hash=?,status='POPULATED_ACTIVE'",
            (turn_id, snapshot_sha256),
        )
        return summary

    receipt = governed_sector_mutation(
        root, lane_id, actor="Evidence OS SQLite Builder", reason=f"explicit {lane_id} source intake", turn_id=turn_id, mutate=mutate
    )
    record_content_hash(brain_root, lane_id, source_id, snapshot_sha256, fingerprint_probe)
    return {"status": "PASS", "source_id": source_id, "sector_id": sector_id, "receipt": receipt, **receipt.details}


__all__ = ["Env15CodeIngestionError", "ingest_env15_code_source"]
