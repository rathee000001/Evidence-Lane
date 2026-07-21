from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlite_brain_builder.runtime.package_root_authority import (
    DEFAULT_ROOT_AUTHORITY_RELATIVE,
    validate_package_root_authority,
    write_package_root_authority,
)
from sqlite_brain_builder.runtime.universal_lane_authority import (
    validate_universal_lane_authority,
    write_universal_lane_authority,
)


CHATGPT_PACKAGE_MAX_BYTES = 512_000_000
GEMINI_PACKAGE_MAX_BYTES = 100_000_000
CHATGPT_LOCAL_AI_PACKAGE_NAME_PREFIX = "ChatGPT_LocalAI"
CHATGPT_CODE_PROJECTION_MIN_BYTES = 32 * 1024 * 1024
ARTIFACT_TEXT_PREVIEW_CHARS = 12_000
PROJECTION_CONTRACT = "T023_PROVIDER_SIZE_BOUNDED_LOGICAL_PROJECTION_V2"
PROJECTION_MARKER = (
    "\n...[provider package preview; exact bytes retained in "
    "code_exact_byte_chunk]"
)
PACKAGE_AUTHORITY_CONTRACT = "T023_CHATGPT_LOCALAI_FINAL_TREE_AUTHORITY_V1"
PROJECT_MANIFEST_CONTRACT = "EVIDENCE_LANE_SNAPSHOT_SCOPED_PROJECT_MANIFEST_V1"
PACKAGE_USE_MODES = frozenset({"CANONICAL_FLASHABLE", "READ_ONLY_STRESS_RESULT"})


class ProviderPackageProjectionError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def provider_text_preview(value: object) -> object:
    if not isinstance(value, str) or len(value) <= ARTIFACT_TEXT_PREVIEW_CHARS:
        return value
    return value[:ARTIFACT_TEXT_PREVIEW_CHARS] + PROJECTION_MARKER


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _blob_stream_digest(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> tuple[int, int, str]:
    count = 0
    byte_size = 0
    digest = hashlib.sha256()
    for (payload,) in connection.execute(
        f'SELECT "{column}" FROM "{table}" ORDER BY rowid'
    ):
        data = bytes(payload or b"")
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
        count += 1
        byte_size += len(data)
    return count, byte_size, digest.hexdigest()


def _install_chunk_index_reuse_view(connection: sqlite3.Connection) -> dict[str, Any]:
    object_type = connection.execute(
        "SELECT type FROM sqlite_schema WHERE name='chunk_index'"
    ).fetchone()
    if not object_type or object_type[0] != "table":
        return {"status": "ALREADY_REUSED_OR_ABSENT", "row_count": 0}
    row_count = int(connection.execute("SELECT COUNT(*) FROM chunk_index").fetchone()[0])
    unmatched = int(
        connection.execute(
            "SELECT COUNT(*) FROM chunk_index i "
            "WHERE NOT EXISTS(SELECT 1 FROM code_chunk c WHERE c.chunk_id=i.chunk_id)"
        ).fetchone()[0]
    )
    mismatched = int(
        connection.execute(
            "SELECT COUNT(*) FROM chunk_index i JOIN code_chunk c USING(chunk_id) "
            "WHERE i.content IS NOT c.chunk_text"
        ).fetchone()[0]
    )
    if unmatched or mismatched:
        raise ProviderPackageProjectionError(
            f"CHUNK_INDEX_CANONICAL_REUSE_MISMATCH:{unmatched}:{mismatched}"
        )
    connection.execute(
        "CREATE TABLE chunk_index_canonical_row("
        "chunk_id TEXT PRIMARY KEY,source_id TEXT,artifact_id TEXT,chunk_type TEXT,"
        "ordinal INTEGER,content_sha256 TEXT,token_estimate INTEGER,"
        "FOREIGN KEY(source_id) REFERENCES source_registry(source_id),"
        "FOREIGN KEY(artifact_id) REFERENCES artifact_registry(artifact_id)"
        ") WITHOUT ROWID"
    )
    connection.execute(
        "INSERT INTO chunk_index_canonical_row "
        "SELECT chunk_id,source_id,artifact_id,chunk_type,ordinal,content_sha256,token_estimate "
        "FROM chunk_index"
    )
    connection.execute("DROP TABLE chunk_index")
    connection.execute(
        "CREATE VIEW chunk_index AS "
        "SELECT i.chunk_id,i.source_id,i.artifact_id,i.chunk_type,i.ordinal,"
        "c.chunk_text AS content,i.content_sha256,i.token_estimate "
        "FROM chunk_index_canonical_row i JOIN code_chunk c USING(chunk_id)"
    )
    for operation in ("INSERT", "UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER lock_chunk_index_{operation.casefold()} "
            f"INSTEAD OF {operation} ON chunk_index "
            "BEGIN SELECT RAISE(ABORT,'PACKAGE_CODE_SECTOR_READ_ONLY'); END"
        )
    return {
        "status": "CANONICAL_CODE_CHUNK_REUSE_VIEW",
        "row_count": row_count,
        "unmatched_rows": unmatched,
        "mismatched_rows": mismatched,
    }


def _rebuild_external_content_fts(connection: sqlite3.Connection) -> dict[str, Any]:
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE name='code_chunk_fts' AND type='table'"
    ).fetchone()
    if not sql_row:
        return {"status": "ABSENT", "row_count": 0}
    if "content='code_chunk_fts_source'" in str(sql_row[0] or ""):
        return {
            "status": "ALREADY_EXTERNAL_CONTENT",
            "row_count": int(connection.execute("SELECT COUNT(*) FROM code_chunk_fts").fetchone()[0]),
        }
    row_count = int(connection.execute("SELECT COUNT(*) FROM code_chunk_fts").fetchone()[0])
    connection.execute(
        "CREATE TEMP TABLE provider_fts_meta AS "
        "SELECT rowid AS source_rowid,chunk_id,relative_path,symbol_name,route_name "
        "FROM code_chunk_fts"
    )
    connection.execute("DROP TABLE code_chunk_fts")
    connection.execute(
        "CREATE TABLE code_chunk_fts_meta("
        "source_rowid INTEGER PRIMARY KEY,chunk_id TEXT NOT NULL UNIQUE,"
        "relative_path TEXT,symbol_name TEXT,route_name TEXT)"
    )
    connection.execute(
        "INSERT INTO code_chunk_fts_meta "
        "SELECT source_rowid,chunk_id,relative_path,symbol_name,route_name "
        "FROM provider_fts_meta ORDER BY source_rowid"
    )
    connection.execute("DROP TABLE provider_fts_meta")
    connection.execute(
        "CREATE VIEW code_chunk_fts_source AS "
        "SELECT m.source_rowid AS rowid,m.chunk_id,m.relative_path,m.symbol_name,"
        "m.route_name,c.chunk_text "
        "FROM code_chunk_fts_meta m JOIN code_chunk c USING(chunk_id)"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE code_chunk_fts USING fts5("
        "chunk_id UNINDEXED,relative_path,symbol_name,route_name,chunk_text,"
        "content='code_chunk_fts_source',content_rowid='rowid',tokenize='unicode61')"
    )
    connection.execute("INSERT INTO code_chunk_fts(code_chunk_fts) VALUES('rebuild')")
    observed = int(connection.execute("SELECT COUNT(*) FROM code_chunk_fts").fetchone()[0])
    if observed != row_count:
        raise ProviderPackageProjectionError(
            f"EXTERNAL_FTS_ROW_COUNT_CHANGED:{row_count}:{observed}"
        )
    return {
        "status": "EXTERNAL_CONTENT_INDEX_REUSES_CODE_CHUNK",
        "row_count": observed,
    }


def _install_git_exact_line_reuse_view(connection: sqlite3.Connection) -> dict[str, Any]:
    types = dict(
        connection.execute(
            "SELECT name,type FROM sqlite_schema "
            "WHERE name IN ('git_line_change','git_exact_line_change')"
        ).fetchall()
    )
    if types.get("git_exact_line_change") != "table":
        return {"status": "ALREADY_REUSED_OR_ABSENT", "row_count": 0}
    if types.get("git_line_change") != "table":
        raise ProviderPackageProjectionError("GIT_LINE_CHANGE_CANONICAL_TABLE_MISSING")
    canonical_count = int(
        connection.execute("SELECT COUNT(*) FROM git_line_change").fetchone()[0]
    )
    exact_count = int(
        connection.execute("SELECT COUNT(*) FROM git_exact_line_change").fetchone()[0]
    )
    left_difference = int(
        connection.execute(
            "SELECT COUNT(*) FROM (SELECT * FROM git_line_change "
            "EXCEPT SELECT * FROM git_exact_line_change)"
        ).fetchone()[0]
    )
    right_difference = int(
        connection.execute(
            "SELECT COUNT(*) FROM (SELECT * FROM git_exact_line_change "
            "EXCEPT SELECT * FROM git_line_change)"
        ).fetchone()[0]
    )
    if canonical_count != exact_count or left_difference or right_difference:
        raise ProviderPackageProjectionError(
            "GIT_EXACT_LINE_CANONICAL_REUSE_MISMATCH:"
            f"{canonical_count}:{exact_count}:{left_difference}:{right_difference}"
        )
    connection.execute("DROP TABLE git_exact_line_change")
    connection.execute(
        "CREATE VIEW git_exact_line_change AS SELECT "
        "line_change_id,hunk_id,commit_sha,path,old_line_number,new_line_number,"
        "change_type,line_text,line_sha256 FROM git_line_change"
    )
    for operation in ("INSERT", "UPDATE", "DELETE"):
        connection.execute(
            f"CREATE TRIGGER lock_git_exact_line_change_{operation.casefold()} "
            f"INSTEAD OF {operation} ON git_exact_line_change "
            "BEGIN SELECT RAISE(ABORT,'PACKAGE_GIT_HISTORY_READ_ONLY'); END"
        )
    return {
        "status": "CANONICAL_GIT_LINE_CHANGE_REUSE_VIEW",
        "row_count": canonical_count,
        "left_difference": left_difference,
        "right_difference": right_difference,
    }


def compact_code_sector_for_provider(database: str | Path) -> dict[str, Any]:
    """Compact a copied provider-stage code DB without touching the live brain.

    Exact file bytes remain in ``code_exact_byte_chunk``.  The repeated 64 KiB
    artifact text carried by code_chunk, chunk_index, and FTS is reduced to a
    governed searchable preview; its full-content hashes remain unchanged.
    """

    path = Path(database)
    if not path.is_file():
        raise ProviderPackageProjectionError(f"PROVIDER_CODE_DATABASE_MISSING:{path}")
    original_size = path.stat().st_size
    original_sha256 = sha256_file(path)
    connection = sqlite3.connect(path, timeout=120)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        required = {
            "code_chunk",
            "chunk_index",
            "code_chunk_fts",
            "code_exact_byte_chunk",
        }
        missing = sorted(name for name in required if not _table_exists(connection, name))
        if missing:
            raise ProviderPackageProjectionError(
                "PROVIDER_CODE_DATABASE_TABLES_MISSING:" + ",".join(missing)
            )
        before_counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(required)
        }
        exact_before = _blob_stream_digest(
            connection,
            "code_exact_byte_chunk",
            "compressed_payload",
        )
        missing_exact = int(
            connection.execute(
                "SELECT COUNT(DISTINCT c.file_id) FROM code_chunk c "
                "WHERE c.chunk_type LIKE 'ARTIFACT_TEXT_CHUNK:%' "
                "AND NOT EXISTS(SELECT 1 FROM code_exact_byte_chunk e "
                "WHERE e.file_id=c.file_id)"
            ).fetchone()[0]
        )
        if missing_exact:
            raise ProviderPackageProjectionError(
                f"PROVIDER_ARTIFACT_EXACT_BYTE_COVERAGE_MISSING:{missing_exact}"
            )
        existing_contract = None
        if _table_exists(connection, "provider_package_projection"):
            existing_contract_row = connection.execute(
                "SELECT value FROM provider_package_projection WHERE key='contract'"
            ).fetchone()
            existing_contract = existing_contract_row[0] if existing_contract_row else None

        changed_rows = 0
        chunk_index_reuse: dict[str, Any] = {
            "status": "ALREADY_REUSED",
            "row_count": before_counts["chunk_index"],
        }
        fts_reuse: dict[str, Any] = {
            "status": "ALREADY_EXTERNAL_CONTENT",
            "row_count": before_counts["code_chunk_fts"],
        }
        git_line_reuse: dict[str, Any] = {
            "status": "ALREADY_REUSED_OR_ABSENT",
            "row_count": 0,
        }
        if existing_contract != PROJECTION_CONTRACT:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS provider_package_projection("
                "key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID"
            )
            changed_rows = int(
                connection.execute(
                    "UPDATE code_chunk SET chunk_text=substr(chunk_text,1,?)||? "
                    "WHERE chunk_type LIKE 'ARTIFACT_TEXT_CHUNK:%' "
                    "AND length(chunk_text)>?",
                    (
                        ARTIFACT_TEXT_PREVIEW_CHARS,
                        PROJECTION_MARKER,
                        ARTIFACT_TEXT_PREVIEW_CHARS,
                    ),
                ).rowcount
            )
            connection.execute(
                "UPDATE chunk_index SET content=(SELECT c.chunk_text FROM code_chunk c "
                "WHERE c.chunk_id=chunk_index.chunk_id) "
                "WHERE EXISTS(SELECT 1 FROM code_chunk c "
                "WHERE c.chunk_id=chunk_index.chunk_id)"
            )
            chunk_index_reuse = _install_chunk_index_reuse_view(connection)
            fts_reuse = _rebuild_external_content_fts(connection)
            git_line_reuse = _install_git_exact_line_reuse_view(connection)
            metadata = {
                "contract": PROJECTION_CONTRACT,
                "live_brain_mutated": "false",
                "artifact_text_preview_chars": str(ARTIFACT_TEXT_PREVIEW_CHARS),
                "exact_byte_authority": "code_exact_byte_chunk.compressed_payload",
                "semantic_hash_authority": (
                    "code_chunk.chunk_sha256;chunk_index_canonical_row.content_sha256"
                ),
                "chunk_index_reuse": json.dumps(chunk_index_reuse, sort_keys=True),
                "fts_reuse": json.dumps(fts_reuse, sort_keys=True),
                "git_line_reuse": json.dumps(git_line_reuse, sort_keys=True),
                "source_database_sha256": original_sha256,
                "source_database_byte_size": str(original_size),
            }
            connection.executemany(
                "INSERT OR REPLACE INTO provider_package_projection(key,value) VALUES(?,?)",
                sorted(metadata.items()),
            )
            connection.commit()
            connection.execute("VACUUM")

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        after_counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in sorted(required)
        }
        exact_after = _blob_stream_digest(
            connection,
            "code_exact_byte_chunk",
            "compressed_payload",
        )
        if integrity != "ok" or foreign_keys:
            raise ProviderPackageProjectionError(
                f"PROVIDER_CODE_DATABASE_INTEGRITY_FAILED:{path}"
            )
        if before_counts != after_counts:
            raise ProviderPackageProjectionError(
                f"PROVIDER_CODE_DATABASE_ROW_COUNT_CHANGED:{path}"
            )
        if exact_before != exact_after:
            raise ProviderPackageProjectionError(
                f"PROVIDER_EXACT_BYTE_PAYLOAD_CHANGED:{path}"
            )
    finally:
        connection.close()

    return {
        "database": str(path),
        "contract": PROJECTION_CONTRACT,
        "source_sha256": original_sha256,
        "source_byte_size": original_size,
        "package_sha256": sha256_file(path),
        "package_byte_size": path.stat().st_size,
        "changed_preview_rows": changed_rows,
        "chunk_index_reuse": chunk_index_reuse,
        "fts_reuse": fts_reuse,
        "git_line_reuse": git_line_reuse,
        "critical_row_counts": after_counts,
        "exact_payload_count": exact_after[0],
        "exact_payload_bytes": exact_after[1],
        "exact_payload_stream_sha256": exact_after[2],
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_keys),
    }


def compact_chatgpt_package_stage(stage: str | Path) -> dict[str, Any]:
    root = Path(stage)
    results = []
    skipped = []
    for relative in (
        Path("project/sectors/github_code/github_code_sector_v001.sqlite"),
        Path("project/sectors/local_code/local_code_sector_v001.sqlite"),
    ):
        database = root / relative
        if database.is_file():
            if database.stat().st_size < CHATGPT_CODE_PROJECTION_MIN_BYTES:
                skipped.append(
                    {
                        "package_member": relative.as_posix(),
                        "reason": "BELOW_PROJECTION_THRESHOLD",
                        "byte_size": database.stat().st_size,
                    }
                )
                continue
            result = compact_code_sector_for_provider(database)
            result["package_member"] = relative.as_posix()
            results.append(result)
    receipt = {
        "contract": PROJECTION_CONTRACT,
        "provider": "ChatGPT_LocalAI",
        "provider_scope": ["CHATGPT_PUBLIC", "LOCAL_AI_OLLAMA", "HEADLESS_API_ENDPOINT"],
        "single_shared_archive": True,
        "maximum_package_bytes": CHATGPT_PACKAGE_MAX_BYTES,
        "live_brain_mutated": False,
        "projection_count": len(results),
        "projections": results,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }
    receipt_path = root / "receipts" / "PROVIDER_PACKAGE_SIZE_PROJECTION_RECEIPT.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt["receipt"] = str(receipt_path)
    return receipt


def rebuild_chatgpt_package_manifest(stage: str | Path) -> Path:
    root = Path(stage)
    manifest = root / "manifests" / "PROJECT_BRAIN_PACKAGE_MANIFEST.json"
    root_authority = root / DEFAULT_ROOT_AUTHORITY_RELATIVE
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path in {manifest, root_authority}:
            continue
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "contract": PROJECT_MANIFEST_CONTRACT,
                "manifest_scope": "PROJECT_SNAPSHOT_NOT_CURRENT_OUTER_ROOT",
                "current_root_authority": DEFAULT_ROOT_AUTHORITY_RELATIVE,
                "file_count": len(rows),
                "files": rows,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _pointer_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_pointer(path: Path, rows: list[tuple[str, object]]) -> None:
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in rows) + "\n",
        encoding="utf-8",
    )


def _sector_payload_state(database: Path) -> tuple[str, int]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        source_count = 0
        if "source_registry" in tables:
            source_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(source_registry)")
            }
            if "active_bool" in source_columns:
                source_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM source_registry WHERE COALESCE(active_bool,1)=1"
                    ).fetchone()[0]
                )
            else:
                source_count = int(connection.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0])
        if source_count == 0 and "ingestion_state" in tables:
            source_count = int(connection.execute("SELECT COUNT(*) FROM ingestion_state").fetchone()[0])
        content_rows = 0
        non_payload = {
            "sector_meta",
            "sector_head",
            "mutation_receipt",
            "mutation_grant",
            "canonical_delta_pointer",
            "code_index_checkpoint",
            "code_projection_state",
            "code_source_active_head",
            "code_snapshot_history",
        }
        for table in sorted(tables):
            if table in non_payload or table.casefold().startswith("code_chunk_fts_"):
                continue
            escaped = table.replace('"', '""')
            content_rows += int(
                connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            )
        return ("POPULATED" if source_count or content_rows else "SCHEMA_READY", source_count)
    finally:
        connection.close()


def _package_paths(root: Path, *, include_manifest: bool = True) -> list[str]:
    manifest = "manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json"
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
        if path.is_file() and (include_manifest or path.relative_to(root).as_posix() != manifest)
    ]


def reconcile_project_router_authority(brain_root: str | Path) -> dict[str, Any]:
    root = Path(brain_root)
    project = root / "project"
    router_path = project / "project_router.sqlite"
    if not router_path.is_file():
        raise ProviderPackageProjectionError("PROJECT_ROUTER_MISSING_FOR_AUTHORITY_RECONCILIATION")
    connection = sqlite3.connect(router_path)
    try:
        sector_rows = connection.execute(
            "SELECT sector_id,display_name,sqlite_path,default_access,automatic_write,"
            "explicit_one_turn_grant_required,relock_after_commit,schema_version,status "
            "FROM sector_registry ORDER BY sector_id"
        ).fetchall()
        sectors: list[dict[str, Any]] = []
        for row in sector_rows:
            database = root / str(row[2])
            if not database.is_file():
                raise ProviderPackageProjectionError(f"PROJECT_SECTOR_MISSING:{row[2]}")
            payload_state, source_count = _sector_payload_state(database)
            sectors.append(
                {
                    "sector_id": str(row[0]),
                    "display_name": str(row[1]),
                    "sqlite_path": str(row[2]),
                    "sha256": sha256_file(database),
                    "byte_size": database.stat().st_size,
                    "payload_state": payload_state,
                    "active_source_count": source_count,
                    "default_access": str(row[3]),
                    "automatic_write": bool(row[4]),
                    "explicit_one_turn_grant_required": bool(row[5]),
                    "relock_after_commit": bool(row[6]),
                    "schema_version": str(row[7]),
                    "router_status": str(row[8]),
                }
            )
        sector_state_vector = [
            {
                "sector_id": sector["sector_id"],
                "payload_state": sector["payload_state"],
                "active_source_count": sector["active_source_count"],
                "router_status": sector["router_status"],
            }
            for sector in sectors
        ]
        sector_state_hash = hashlib.sha256(
            json.dumps(
                sector_state_vector,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        active_lanes = [
            sector["sector_id"]
            for sector in sectors
            if sector["payload_state"] == "POPULATED"
        ]
        desired_project_state = (
            "CURRENT_POPULATED_PROJECT" if active_lanes else "CURRENT_SCHEMA_READY_PROJECT"
        )
        changed = False
        stamp = _utc_now()
        if _table_exists(connection, "active_project_head"):
            current_head = connection.execute(
                "SELECT active_project_state,latest_state_hash FROM active_project_head "
                "WHERE singleton_id=1"
            ).fetchone()
            desired_head = (desired_project_state, sector_state_hash)
            if current_head != desired_head:
                connection.execute(
                    "UPDATE active_project_head SET active_project_state=?,latest_state_hash=?,updated_at=? "
                    "WHERE singleton_id=1",
                    (*desired_head, stamp),
                )
                changed = True
        live_topology = all(
            (project / "topology" / f"project_master_topology.{suffix}").is_file()
            for suffix in ("mmd", "svg", "png")
        )
        if live_topology and _table_exists(connection, "mmd_layout_anchor_registry"):
            desired_anchor = (
                "project/topology/project_master_topology.mmd",
                "project/topology/project_master_topology.svg",
                "project/topology/project_master_topology.png",
                "T023_LIVE_PROJECT_MASTER_TOPOLOGY_FIXED_LAYOUT",
            )
            current_anchor = connection.execute(
                "SELECT source_mmd_path,render_svg_path,render_png_path,layout_grammar "
                "FROM mmd_layout_anchor_registry WHERE topology_id='PROJECT'"
            ).fetchone()
            if current_anchor != desired_anchor:
                connection.execute(
                    "UPDATE mmd_layout_anchor_registry SET source_mmd_path=?,render_svg_path=?,"
                    "render_png_path=?,layout_grammar=?,updated_at=? WHERE topology_id='PROJECT'",
                    (*desired_anchor, stamp),
                )
                changed = True
        if changed:
            connection.commit()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    finally:
        connection.close()
    if integrity != "ok" or foreign_keys:
        raise ProviderPackageProjectionError("PROJECT_ROUTER_AUTHORITY_RECONCILIATION_SQLITE_INVALID")
    result = {
        "contract": "T023_PROJECT_ROUTER_CURRENT_AUTHORITY_V1",
        "status": "PASS",
        "changed": changed,
        "project_state": desired_project_state,
        "sector_state_sha256": sector_state_hash,
        "sector_state_vector": sector_state_vector,
        "sector_count": len(sectors),
        "active_lane_ids": active_lanes,
        "project_router_sha256": sha256_file(router_path),
        "live_topology_anchored": live_topology,
        "sectors": sectors,
        "observed_at": _utc_now(),
    }
    receipts = root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts / "T023_PROJECT_ROUTER_CURRENT_AUTHORITY.json"
    receipt_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result["receipt_path"] = str(receipt_path)
    return result


def validate_chatgpt_package_authority(stage: str | Path) -> dict[str, Any]:
    root = Path(stage)
    errors: list[str] = []
    authority_path = root / "manifests" / "PACKAGE_AUTHORITY.json"
    use_mode_path = root / "manifests" / "PACKAGE_USE_MODE.json"
    sector_registry_path = root / "manifests" / "PROJECT_SECTOR_REGISTRY.json"
    coverage_path = root / "manifests" / "FILE_COVERAGE_AUDIT.json"
    availability_path = root / "manifests" / "AVAILABILITY_LEDGER.json"
    project_manifest_path = root / "manifests" / "PROJECT_BRAIN_PACKAGE_MANIFEST.json"
    lineage_head_path = root / "project" / "lineage" / "LINEAGE_HEAD.json"
    try:
        authority = json.loads(authority_path.read_text(encoding="utf-8"))
        use_mode = json.loads(use_mode_path.read_text(encoding="utf-8"))
        registry = json.loads(sector_registry_path.read_text(encoding="utf-8"))
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        availability = json.loads(availability_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "contract": PACKAGE_AUTHORITY_CONTRACT,
            "status": "FAIL",
            "errors": [f"AUTHORITY_DOCUMENT_READ_FAILED:{type(exc).__name__}"],
        }
    if authority.get("contract") != PACKAGE_AUTHORITY_CONTRACT or authority.get("status") != "PASS":
        errors.append("PACKAGE_AUTHORITY_CONTRACT_INVALID")
    lineage_validation = validate_universal_lane_authority(root)
    if lineage_validation.get("status") != "PASS":
        errors.extend(
            "CANONICAL_LINEAGE_AUTHORITY_INVALID:" + str(error)
            for error in lineage_validation.get("errors") or []
        )
    root_authority_validation = validate_package_root_authority(root)
    if root_authority_validation.get("status") != "PASS":
        errors.extend(
            "PACKAGE_ROOT_AUTHORITY_INVALID:" + str(error)
            for error in root_authority_validation.get("errors") or []
        )
    try:
        project_manifest = json.loads(project_manifest_path.read_text(encoding="utf-8"))
        if project_manifest.get("contract") != PROJECT_MANIFEST_CONTRACT:
            errors.append("PROJECT_MANIFEST_CONTRACT_INVALID")
        if project_manifest.get("manifest_scope") != "PROJECT_SNAPSHOT_NOT_CURRENT_OUTER_ROOT":
            errors.append("PROJECT_MANIFEST_SCOPE_INVALID")
        if project_manifest.get("current_root_authority") != DEFAULT_ROOT_AUTHORITY_RELATIVE:
            errors.append("PROJECT_MANIFEST_ROOT_PRECEDENCE_INVALID")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        errors.append(f"PROJECT_MANIFEST_PRECEDENCE_READ_FAILED:{type(exc).__name__}")
    try:
        lineage_head = json.loads(lineage_head_path.read_text(encoding="utf-8"))
        lineage_payload = {key: value for key, value in lineage_head.items() if key != "head_sha256"}
        lineage_hash = hashlib.sha256(
            json.dumps(
                lineage_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if lineage_head.get("head_sha256") != lineage_hash:
            errors.append("CANONICAL_LINEAGE_HEAD_HASH_INVALID")
        if authority.get("lineage_head_sha256") != lineage_hash:
            errors.append("PACKAGE_AUTHORITY_LINEAGE_HEAD_MISMATCH")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        errors.append(f"CANONICAL_LINEAGE_HEAD_READ_FAILED:{type(exc).__name__}")
    if use_mode.get("mode") not in PACKAGE_USE_MODES:
        errors.append("PACKAGE_USE_MODE_INVALID")
    sectors = list(registry.get("sectors") or [])
    if int(registry.get("sector_count") or 0) != len(sectors) or len(sectors) not in {14, 15}:
        errors.append("PROJECT_RUNTIME_SECTOR_REGISTRY_COUNT_INVALID")
    for sector in sectors:
        relative = str(sector.get("sqlite_path") or "")
        database = root / relative
        if not database.is_file():
            errors.append(f"PROJECT_RUNTIME_SECTOR_MISSING:{relative}")
        elif str(sector.get("sha256") or "").lower() != sha256_file(database):
            errors.append(f"PROJECT_RUNTIME_SECTOR_HASH_MISMATCH:{relative}")
    pointer = _pointer_values(root / ".uepc_project")
    if int(pointer.get("PROJECT_SECTOR_COUNT") or 0) != len(sectors):
        errors.append("PROJECT_POINTER_SECTOR_COUNT_MISMATCH")
    topology = root / str(pointer.get("PROJECT_TOPOLOGY_MMD") or "")
    if not topology.is_file() or topology.name != "project_master_topology.mmd":
        errors.append("PROJECT_POINTER_LIVE_TOPOLOGY_INVALID")
    try:
        router = sqlite3.connect(
            f"file:{(root / 'project' / 'project_router.sqlite').as_posix()}?mode=ro",
            uri=True,
        )
        try:
            active_head = router.execute(
                "SELECT active_project_state,latest_state_hash FROM active_project_head "
                "WHERE singleton_id=1"
            ).fetchone()
            topology_anchor = router.execute(
                "SELECT source_mmd_path,render_svg_path,render_png_path,layout_grammar "
                "FROM mmd_layout_anchor_registry WHERE topology_id='PROJECT'"
            ).fetchone()
        finally:
            router.close()
        expected_state = (
            "CURRENT_POPULATED_PROJECT"
            if any(sector.get("payload_state") == "POPULATED" for sector in sectors)
            else "CURRENT_SCHEMA_READY_PROJECT"
        )
        if active_head != (expected_state, authority.get("sector_state_sha256")):
            errors.append("PROJECT_ROUTER_ACTIVE_HEAD_AUTHORITY_MISMATCH")
        if topology_anchor != (
            "project/topology/project_master_topology.mmd",
            "project/topology/project_master_topology.svg",
            "project/topology/project_master_topology.png",
            "T023_LIVE_PROJECT_MASTER_TOPOLOGY_FIXED_LAYOUT",
        ):
            errors.append("PROJECT_ROUTER_LIVE_TOPOLOGY_ANCHOR_MISMATCH")
    except sqlite3.Error as exc:
        errors.append(f"PROJECT_ROUTER_AUTHORITY_QUERY_FAILED:{type(exc).__name__}")
    actual_paths = set(_package_paths(root))
    if set(coverage.get("covered_paths") or []) != actual_paths:
        errors.append("FILE_COVERAGE_FINAL_TREE_MISMATCH")
    if set(availability.get("paths") or []) != actual_paths:
        errors.append("AVAILABILITY_FINAL_TREE_MISMATCH")
    if authority.get("project_router_sha256") != sha256_file(root / "project" / "project_router.sqlite"):
        errors.append("PACKAGE_AUTHORITY_ROUTER_HASH_MISMATCH")
    return {
        "contract": PACKAGE_AUTHORITY_CONTRACT,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "package_use_mode": use_mode.get("mode"),
        "sector_count": len(sectors),
        "populated_sector_ids": [
            str(sector.get("sector_id") or "")
            for sector in sectors
            if sector.get("payload_state") == "POPULATED"
        ],
        "project_router_sha256": sha256_file(root / "project" / "project_router.sqlite"),
        "root_authority": root_authority_validation,
        "lineage_authority": lineage_validation,
    }


def reseal_chatgpt_package_authority(
    stage: str | Path,
    *,
    brain_name: str,
    package_use_mode: str = "CANONICAL_FLASHABLE",
) -> dict[str, Any]:
    root = Path(stage)
    mode = str(package_use_mode or "CANONICAL_FLASHABLE").strip().upper()
    if mode not in PACKAGE_USE_MODES:
        raise ProviderPackageProjectionError(f"PACKAGE_USE_MODE_INVALID:{mode}")
    project = root / "project"
    router_path = project / "project_router.sqlite"
    if not router_path.is_file():
        raise ProviderPackageProjectionError("PROJECT_ROUTER_MISSING_FOR_AUTHORITY_RESEAL")

    router_authority = reconcile_project_router_authority(root)
    sectors = list(router_authority["sectors"])
    sector_state_hash = str(router_authority["sector_state_sha256"])
    stamp = _utc_now()

    manifests = root / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    registry = {
        "contract": "T023_RUNTIME_PROJECT_SECTOR_REGISTRY_V1",
        "status": "PASS",
        "immutable_env15_base_sector_count": 14,
        "runtime_extension_sector_ids": [
            sector["sector_id"] for sector in sectors if sector["sector_id"] == "delta"
        ],
        "sector_count": len(sectors),
        "sector_state_sha256": sector_state_hash,
        "sectors": sectors,
    }
    registry_text = json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (project / "PROJECT_SECTOR_REGISTRY.json").write_text(registry_text, encoding="utf-8")
    (manifests / "PROJECT_SECTOR_REGISTRY.json").write_text(registry_text, encoding="utf-8")
    for sector in sectors:
        pointer_path = project / "sectors" / sector["sector_id"] / f"{sector['sector_id']}_pointer.json"
        pointer = {}
        if pointer_path.is_file():
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pointer = {}
        pointer.update(
            {
                "sector_id": sector["sector_id"],
                "sqlite_path": sector["sqlite_path"],
                "sha256": sector["sha256"],
                "byte_size": sector["byte_size"],
                "payload_state": sector["payload_state"],
                "active_source_count": sector["active_source_count"],
                "authority": "T023_FINAL_PACKAGE_TREE",
            }
        )
        pointer_path.write_text(
            json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    active_lanes = [sector["sector_id"] for sector in sectors if sector["payload_state"] == "POPULATED"]
    activation_path = root / "receipts" / "lane_activation_receipt.json"
    logical_active_lanes: list[str] = []
    if activation_path.is_file():
        try:
            logical_active_lanes = [
                str(lane_id)
                for lane_id in json.loads(activation_path.read_text(encoding="utf-8")).get(
                    "active_lane_ids", []
                )
            ]
        except (OSError, json.JSONDecodeError, TypeError):
            logical_active_lanes = []
    universal_lane_authority = write_universal_lane_authority(
        root,
        active_lane_ids=logical_active_lanes,
        brain_name=brain_name,
        package_use_mode=mode,
    )
    # The universal writer emits the live canonical lane registry and a
    # router-derived sector view for ordinary brains.  A provider stage also
    # requires this reseal-specific sector-state contract (including
    # payload_state and sector_state_sha256), so restore it after universal
    # pointer generation before validating the final package tree.
    (project / "PROJECT_SECTOR_REGISTRY.json").write_text(
        registry_text, encoding="utf-8"
    )
    (manifests / "PROJECT_SECTOR_REGISTRY.json").write_text(
        registry_text, encoding="utf-8"
    )
    router_sha256 = sha256_file(router_path)
    env_values = _pointer_values(root / ".uepc_env")
    env_values["PACKAGE_CLASS"] = "EVIDENCE_OS_CHATGPT_LOCALAI_POPULATED_PROJECT_V1"
    env_values["PACKAGE_USE_MODE"] = mode
    env_values["PROJECT_POINTER"] = ".uepc_project"
    _write_pointer(root / ".uepc_env", list(env_values.items()))
    (manifests / "PACKAGE_CLASS.txt").write_text(
        "PACKAGE_CLASS=EVIDENCE_OS_CHATGPT_LOCALAI_POPULATED_PROJECT_V1\n"
        "PRIVATE_PAYLOAD=ABSENT\n"
        f"PROJECT_FACT_PAYLOAD={'POPULATED' if active_lanes else 'SCHEMA_READY'}\n"
        f"RUNTIME_PROJECT_SECTORS={len(sectors)}\n"
        f"GOVERNED_PROJECT_SECTORS={len(sectors)}\n"
        f"PACKAGE_USE_MODE={mode}\n",
        encoding="utf-8",
    )
    use_mode = {
        "contract": "T023_PROVIDER_PACKAGE_USE_MODE_V1",
        "mode": mode,
        "mutation_instructions_authorized": mode == "CANONICAL_FLASHABLE",
        "stress_result_read_only": mode == "READ_ONLY_STRESS_RESULT",
    }
    (manifests / "PACKAGE_USE_MODE.json").write_text(
        json.dumps(use_mode, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if mode == "READ_ONLY_STRESS_RESULT":
        read_only_prompt = (
            "T023 READ-ONLY STRESS RESULT\n\n"
            "This package is evaluation evidence only. Do not flash, mutate, relock, "
            "promote, or treat it as canonical. Query the populated Project payload read-only "
            "and report observations against manifests/PACKAGE_AUTHORITY.json.\n"
        )
        for prompt_path in (
            root / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
            root / "prompts" / "FLASH_ME_FIRST_SINGLE_PROMPT.txt",
        ):
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(read_only_prompt, encoding="utf-8")

    pointer_summary = (
        "# T023 final package pointer summary\n"
        f"PACKAGE_USE_MODE={mode}\n"
        f"PROJECT_ROUTER_SHA256={router_sha256}\n"
        f"PROJECT_SECTOR_COUNT={len(sectors)}\n\n"
        + (root / ".uepc_env").read_text(encoding="utf-8")
        + "\n"
        + (root / ".uepc_profile").read_text(encoding="utf-8")
        + "\n"
        + (root / ".uepc_project").read_text(encoding="utf-8")
    )
    (manifests / "POINTER_SUMMARY.txt").write_text(pointer_summary, encoding="utf-8")
    authority = {
        "contract": PACKAGE_AUTHORITY_CONTRACT,
        "status": "PASS",
        "structural_status": "PASS",
        "acceptance_state": "UNTRUSTED_CANDIDATE",
        "promotion_authority": "EXPLICIT_HUMAN_HIL_ONLY",
        "receipts_override_underlying_state": False,
        "brain_name": brain_name,
        "package_use_mode": mode,
        "project_payload_state": "POPULATED" if active_lanes else "SCHEMA_READY",
        "project_router_sha256": router_sha256,
        "sector_registry_sha256": sha256_file(manifests / "PROJECT_SECTOR_REGISTRY.json"),
        "sector_state_sha256": sector_state_hash,
        "sector_count": len(sectors),
        "active_lane_ids": active_lanes,
        "project_topology_sha256": sha256_file(project / "topology" / "project_master_topology.mmd"),
        "locked_read_only_authorities": ["env", "uop"],
        "project_authority": "LIVE_GOVERNED_SECTOR_GRAPH",
        "universal_lane_authority": universal_lane_authority,
        "lineage_head": "project/lineage/LINEAGE_HEAD.json",
        "lineage_head_sha256": universal_lane_authority["lineage_head_sha256"],
        "root_authority": DEFAULT_ROOT_AUTHORITY_RELATIVE,
        "created_at": stamp,
    }
    (manifests / "PACKAGE_AUTHORITY.json").write_text(
        json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    generated_paths = {
        "manifests/AVAILABILITY_LEDGER.json",
        "manifests/FILE_COVERAGE_AUDIT.json",
        "manifests/INTERNAL_HASH_MANIFEST.txt",
        "manifests/PACKAGE_CONTENTS.json",
        "manifests/PAYLOAD_TREE_HASH.txt",
        "manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json",
        DEFAULT_ROOT_AUTHORITY_RELATIVE,
    }
    expected_paths = sorted(set(_package_paths(root)) | generated_paths)
    (manifests / "FILE_COVERAGE_AUDIT.json").write_text(
        json.dumps(
            {"status": "PASS", "file_count": len(expected_paths), "covered_paths": expected_paths},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (manifests / "AVAILABILITY_LEDGER.json").write_text(
        json.dumps({"status": "AVAILABLE", "paths": expected_paths}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (manifests / "PACKAGE_CONTENTS.json").write_text(
        json.dumps(
            {
                "contract": "T023_FINAL_PACKAGE_CONTENTS_V1",
                "file_count": len(expected_paths),
                "files": expected_paths,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    hash_exclusions = {
        "manifests/INTERNAL_HASH_MANIFEST.txt",
        "manifests/PAYLOAD_TREE_HASH.txt",
        "manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json",
        DEFAULT_ROOT_AUTHORITY_RELATIVE,
    }
    hash_rows = []
    for relative in _package_paths(root):
        if relative in hash_exclusions:
            continue
        path = root / relative
        hash_rows.append(f"{sha256_file(path)} {path.stat().st_size:>12} {relative}")
    internal_manifest = "\n".join(hash_rows) + "\n"
    (manifests / "INTERNAL_HASH_MANIFEST.txt").write_text(internal_manifest, encoding="utf-8")
    (manifests / "PAYLOAD_TREE_HASH.txt").write_text(
        "CONTRACT=T023_FINAL_PACKAGE_PAYLOAD_TREE_V1\n"
        f"HASHED_FILE_COUNT={len(hash_rows)}\n"
        f"INTERNAL_HASH_MANIFEST_SHA256={hashlib.sha256(internal_manifest.encode('utf-8')).hexdigest()}\n",
        encoding="utf-8",
    )
    rebuild_chatgpt_package_manifest(root)
    write_package_root_authority(
        root,
        package_profile="CHATGPT_LOCAL_AI_FULL_PROJECT",
        package_use_mode=mode,
        lineage_head_relative="project/lineage/LINEAGE_HEAD.json",
        extra={
            "brain_name": brain_name,
            "project_router_sha256": router_sha256,
            "sector_state_sha256": sector_state_hash,
            "active_lane_ids": active_lanes,
        },
    )
    validation = validate_chatgpt_package_authority(root)
    if validation["status"] != "PASS":
        raise ProviderPackageProjectionError(
            "CHATGPT_LOCALAI_AUTHORITY_RESEAL_FAILED:" + ";".join(validation["errors"])
        )
    return {**authority, "validation": validation}


def enforce_provider_package_size(
    package: str | Path,
    maximum_bytes: int,
    provider: str,
) -> int:
    path = Path(package)
    observed = path.stat().st_size
    if observed > maximum_bytes:
        raise ProviderPackageProjectionError(
            f"{provider.upper()}_PACKAGE_SIZE_LIMIT_EXCEEDED:"
            f"{observed}>{maximum_bytes}:{path}"
        )
    return observed


def provider_skip_marker_path(packages_root: str | Path, provider: str) -> Path:
    normalized = str(provider or "").strip().upper()
    if normalized not in {"CHATGPT", "GEMINI"}:
        raise ProviderPackageProjectionError(f"PROVIDER_SKIP_MARKER_INVALID:{normalized}")
    return Path(packages_root) / f"PROJECT_CORPUS_TOO_LARGE_FOR_{normalized}.txt"


def clear_provider_skip_marker(packages_root: str | Path, provider: str) -> None:
    provider_skip_marker_path(packages_root, provider).unlink(missing_ok=True)


def write_provider_skip_marker(
    packages_root: str | Path,
    provider: str,
    *,
    observed_bytes: int | None,
    maximum_bytes: int,
    reason: str,
) -> dict[str, Any]:
    marker = provider_skip_marker_path(packages_root, provider)
    marker.parent.mkdir(parents=True, exist_ok=True)
    observed_text = str(int(observed_bytes)) if observed_bytes is not None else "NOT_BUILT_UPSTREAM_SKIP"
    marker.write_text(
        "EVIDENCE OS OPTIONAL PROVIDER PACKAGE SKIP\n"
        f"provider={str(provider).strip().upper()}\n"
        "status=SKIPPED_PROJECT_CORPUS_TOO_LARGE\n"
        f"observed_archive_bytes={observed_text}\n"
        f"maximum_archive_bytes={int(maximum_bytes)}\n"
        f"reason={reason}\n"
        "codex_package_required=true\n"
        "pipeline_failure=false\n",
        encoding="utf-8",
    )
    return {
        "status": "SKIPPED_PROJECT_CORPUS_TOO_LARGE",
        "skipped": True,
        "provider": str(provider).strip().upper(),
        "skip_marker": str(marker),
        "observed_archive_bytes": observed_bytes,
        "provider_size_limit_bytes": int(maximum_bytes),
        "reason": reason,
        "codex_package_required": True,
        "pipeline_failure": False,
        "validation": {
            "status": "SKIPPED",
            "errors": [],
            "reason": reason,
        },
    }


def retire_provider_outputs(packages_root: str | Path, provider: str) -> list[str]:
    root = Path(packages_root).resolve()
    normalized = str(provider or "").strip().upper()
    patterns = {
        "CHATGPT": ("ChatGPT_*.zip", "*_one_upload_package*_v001"),
        "GEMINI": ("Gemini_*.zip", "*_gemini_*_v001"),
    }.get(normalized)
    if patterns is None:
        raise ProviderPackageProjectionError(f"PROVIDER_RETIRE_INVALID:{normalized}")
    removed: list[str] = []
    for pattern in patterns:
        for candidate in root.glob(pattern):
            resolved = candidate.resolve()
            if resolved == root or root not in resolved.parents:
                raise ProviderPackageProjectionError(f"PROVIDER_RETIRE_PATH_ESCAPE:{candidate}")
            if candidate.is_dir():
                remove_provider_stage(candidate)
            else:
                candidate.chmod(candidate.stat().st_mode | stat.S_IWUSR)
                candidate.unlink(missing_ok=True)
            removed.append(str(candidate))
    return removed


def remove_provider_stage(path: str | Path) -> None:
    """Remove a transient provider tree even when it contains copied read-only law files."""

    target = Path(path)
    if not target.exists():
        return

    def make_writable_and_retry(function, candidate, _error) -> None:
        os.chmod(candidate, stat.S_IWRITE | stat.S_IREAD)
        function(candidate)

    shutil.rmtree(target, onerror=make_writable_and_retry)


__all__ = [
    "ARTIFACT_TEXT_PREVIEW_CHARS",
    "CHATGPT_CODE_PROJECTION_MIN_BYTES",
    "CHATGPT_LOCAL_AI_PACKAGE_NAME_PREFIX",
    "CHATGPT_PACKAGE_MAX_BYTES",
    "GEMINI_PACKAGE_MAX_BYTES",
    "PROJECTION_CONTRACT",
    "PROJECT_MANIFEST_CONTRACT",
    "PACKAGE_AUTHORITY_CONTRACT",
    "PACKAGE_USE_MODES",
    "ProviderPackageProjectionError",
    "compact_chatgpt_package_stage",
    "clear_provider_skip_marker",
    "compact_code_sector_for_provider",
    "enforce_provider_package_size",
    "provider_text_preview",
    "provider_skip_marker_path",
    "remove_provider_stage",
    "retire_provider_outputs",
    "reconcile_project_router_authority",
    "rebuild_chatgpt_package_manifest",
    "reseal_chatgpt_package_authority",
    "sha256_file",
    "write_provider_skip_marker",
    "validate_chatgpt_package_authority",
]
