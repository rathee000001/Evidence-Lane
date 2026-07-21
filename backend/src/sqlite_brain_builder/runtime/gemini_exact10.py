from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from sqlite_brain_builder.runtime.env15_resource import (
    ENV15_PACKAGE_CLASS,
    ENV15_SOURCE_PACKAGE_SHA256,
    find_env15_resource_root,
    validate_env15_resource,
)
from sqlite_brain_builder.runtime.package_validation import (
    T021_GEMINI_EXACT10_NAMES,
    validate_chatgpt_package,
    validate_gemini_exact10,
)
from sqlite_brain_builder.runtime.env15_project_schema import (
    ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS,
    verify_env15_live_project,
)
from sqlite_brain_builder.runtime.provider_package_projection import (
    GEMINI_PACKAGE_MAX_BYTES,
    clear_provider_skip_marker,
    enforce_provider_package_size,
    provider_text_preview,
    retire_provider_outputs,
    write_provider_skip_marker,
)
class GeminiExact10Error(RuntimeError):
    """The direct-file package could not satisfy the T021 hard gate."""


PROJECT_EMBED_INLINE_LIMIT_BYTES = 64 * 1024 * 1024
PROJECT_EMBED_CHUNK_BYTES = 32 * 1024 * 1024
PROJECT_LOGICAL_PROJECTION_MIN_BYTES = 64 * 1024 * 1024
PROJECTION_COPY_BATCH_ROWS = 256
CANONICAL_CONTENT_COLUMNS = {
    "code_chunk": "chunk_text",
    "chunk_index": "content",
    "code_chunk_fts": "chunk_text",
    "code_exact_byte_chunk": "compressed_payload",
}
LOGICAL_COMPATIBILITY_REUSE_TARGETS = {
    "git_exact_line_change": "git_line_change",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "brain").strip()).strip("._-")
    return safe or "brain"


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file() and not path.name.endswith(("-wal", "-shm")):
            yield path


def build_env15_public_runtime_sqlite(
    destination: str | Path,
    *,
    runtime_root: str | Path | None = None,
) -> dict[str, Any]:
    """Embed the exact Env15 public resource into one readable SQLite file.

    The wrapper preserves each supplied byte and hash.  It does not mutate any
    locked Env/UOP/Project SQLite database or claim that the supplied semantic
    MMD defect has already been corrected.
    """

    if runtime_root is None:
        resource_root = find_env15_resource_root()
        validation = validate_env15_resource(resource_root)
        if not validation.structural_validation_passed:
            raise GeminiExact10Error("ENV15_RESOURCE_VALIDATION_FAILED:" + ";".join(validation.errors))
        runtime_files = list(_iter_files(resource_root))
        runtime_sqlite = [
            (result.relative_path, ",".join(result.integrity_check), result.foreign_key_violation_count)
            for result in validation.sqlite_results
        ]
        sqlite_count = validation.actual_sqlite_count
        sector_count = validation.actual_sector_count
        semantic_status = validation.mmd_semantic_validation_status
        acceptance_claimed = "false"
    else:
        resource_root = Path(runtime_root).resolve()
        project_validation = verify_env15_live_project(resource_root)
        if not project_validation.passed:
            raise GeminiExact10Error("ENV15_LIVE_RUNTIME_VALIDATION_FAILED:" + ";".join(project_validation.errors))
        runtime_files = [
            path for path in _iter_files(resource_root)
            if not ({"packages", "brain_snapshots"} & set(path.relative_to(resource_root).parts))
        ]
        runtime_sqlite = []
        for path in runtime_files:
            if path.suffix.casefold() not in {".sqlite", ".sqlite3", ".db"}:
                continue
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                integrity = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
                foreign_keys = tuple(connection.execute("PRAGMA foreign_key_check"))
            finally:
                connection.close()
            if integrity != ("ok",) or foreign_keys:
                raise GeminiExact10Error(f"ENV15_LIVE_SQLITE_INVALID:{path}")
            runtime_sqlite.append((path.relative_to(resource_root).as_posix(), ",".join(integrity), len(foreign_keys)))
        sqlite_count = len(runtime_sqlite)
        sector_count = project_validation.sector_count
        semantic_status = "PROMPT_ROOTED_ENV_UOP_PROJECT_MMD_RENDERED_AND_VALIDATED"
        acceptance_claimed = "true"

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="evidenceos_env15_runtime_", dir=target.parent) as tmp:
        staged = Path(tmp) / target.name
        connection = sqlite3.connect(staged)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE runtime_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE runtime_file(
                    path TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                    media_type TEXT NOT NULL,
                    content BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE runtime_sqlite_validation(
                    path TEXT PRIMARY KEY REFERENCES runtime_file(path),
                    integrity_check TEXT NOT NULL,
                    foreign_key_violation_count INTEGER NOT NULL
                ) WITHOUT ROWID;
                """
            )
            metadata = {
                "contract": "T021_GEMINI_DIRECT_FILE_ENV15_RUNTIME",
                "package_class": ENV15_PACKAGE_CLASS,
                "source_package_sha256": ENV15_SOURCE_PACKAGE_SHA256,
                "embedded_member_count": str(len(runtime_files)),
                "embedded_sqlite_count": str(sqlite_count),
                "embedded_sector_count": str(sector_count),
                "mmd_semantic_validation_status": semantic_status,
                "mmd_semantic_acceptance_claimed": acceptance_claimed,
                "mutation_law": "research_and_chat_lineage_automatic_append_only;all_other_sectors_named_one_turn_hil_grant_receipt_snapshot_relock",
            }
            connection.executemany(
                "INSERT INTO runtime_meta(key,value) VALUES(?,?)",
                sorted(metadata.items()),
            )
            for path in runtime_files:
                relative = path.relative_to(resource_root).as_posix()
                data = path.read_bytes()
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                connection.execute(
                    "INSERT INTO runtime_file(path,sha256,byte_size,media_type,content) VALUES(?,?,?,?,?)",
                    (relative, _sha256_bytes(data), len(data), media_type, data),
                )
            connection.executemany(
                "INSERT INTO runtime_sqlite_validation(path,integrity_check,foreign_key_violation_count) VALUES(?,?,?)",
                runtime_sqlite,
            )
            connection.commit()
            connection.execute("VACUUM")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise GeminiExact10Error("ENV15_RUNTIME_SQLITE_VALIDATION_FAILED")
        finally:
            connection.close()
        staged.replace(target)
    return {
        "path": str(target),
        "sha256": _sha256_file(target),
        "member_count": len(runtime_files),
        "sqlite_count": sqlite_count,
        "sector_count": sector_count,
    }


def _sqlite_affinity(declared_type: object) -> str:
    value = str(declared_type or "").upper()
    if "INT" in value:
        return "INTEGER"
    if any(token in value for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in value or not value:
        return "BLOB"
    if any(token in value for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _project_database_identity(project: Path, source: Path) -> tuple[str, str, str]:
    relative = source.relative_to(project).as_posix()
    parts = PurePosixPath(relative).parts
    if len(parts) >= 3 and parts[0] == "sectors":
        sector_id = _safe_name(parts[1]).casefold()
        source_id = f"sector:{sector_id}:{_safe_name(source.stem).casefold()}"
        return source_id, sector_id, f"sector_{sector_id}__"
    base_id = _safe_name(source.stem).casefold()
    return f"project_core:{base_id}", "project_core", f"project_core__{base_id}__"


def _projection_name(prefix: str, object_name: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_]+", "_", object_name).strip("_") or "object"
    return (prefix + suffix)[:220]


def _canonical_content_projection(
    destination: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    source_database_id: str,
    projected_name: str,
    source_table: str,
    columns: list[tuple],
) -> dict[str, Any]:
    """Project a large repeated-content table through one canonical blob store."""

    content_column = CANONICAL_CONTENT_COLUMNS[source_table]
    visible_columns = [str(row[1]) for row in columns]
    content_row = next(row for row in columns if str(row[1]) == content_column)
    content_affinity = _sqlite_affinity(content_row[2])
    primary_keys = [
        name
        for _order, name in sorted(
            (int(row[5]), str(row[1]))
            for row in columns
            if int(row[5] or 0) > 0
        )
    ]
    backing_name = (
        "__canonical_rows_"
        + hashlib.sha256(
            f"{source_database_id}:{source_table}".encode("utf-8")
        ).hexdigest()[:24]
    )
    definitions: list[str] = []
    non_content_columns = [row for row in columns if str(row[1]) != content_column]
    for row in non_content_columns:
        definition = (
            f"{_quoted_identifier(str(row[1]))} "
            f"{_sqlite_affinity(row[2])}"
        )
        if int(row[3] or 0):
            definition += " NOT NULL"
        if len(primary_keys) == 1 and int(row[5] or 0) > 0:
            definition += " PRIMARY KEY"
        definitions.append(definition)
    if len(primary_keys) > 1:
        definitions.append(
            "PRIMARY KEY(" + ",".join(_quoted_identifier(name) for name in primary_keys) + ")"
        )
    if not primary_keys:
        definitions.insert(0, '"__projection_row_id" INTEGER PRIMARY KEY')
    definitions.extend(
        (
            '"__canonical_content_ref" TEXT',
            f'"__inline_content" {content_affinity}',
        )
    )
    destination.execute(
        f"CREATE TABLE {_quoted_identifier(backing_name)}({','.join(definitions)})"
    )

    selected_columns = ",".join(_quoted_identifier(name) for name in visible_columns)
    source_cursor = source.execute(
        f"SELECT {selected_columns} FROM {_quoted_identifier(source_table)}"
    )
    backing_columns = [str(row[1]) for row in non_content_columns]
    insert_columns = list(backing_columns)
    if not primary_keys:
        insert_columns.insert(0, "__projection_row_id")
    insert_columns.extend(("__canonical_content_ref", "__inline_content"))
    insert_sql = (
        f"INSERT INTO {_quoted_identifier(backing_name)}("
        + ",".join(_quoted_identifier(name) for name in insert_columns)
        + ") VALUES("
        + ",".join("?" for _ in insert_columns)
        + ")"
    )
    content_index = visible_columns.index(content_column)
    row_count = 0
    while True:
        rows = source_cursor.fetchmany(PROJECTION_COPY_BATCH_ROWS)
        if not rows:
            break
        projected_rows = []
        for row in rows:
            value = provider_text_preview(row[content_index])
            is_text = isinstance(value, str)
            payload = value.encode("utf-8") if is_text else bytes(value or b"")
            content_kind = "TEXT_UTF8" if is_text else "BLOB"
            digest = _sha256_bytes(payload)
            content_ref = f"{content_kind}:{digest}"
            destination.execute(
                "INSERT OR IGNORE INTO canonical_content_blob"
                "(content_ref,content_kind,content_sha256,byte_size,content) "
                "VALUES(?,?,?,?,?)",
                (content_ref, content_kind, digest, len(payload), payload),
            )
            projected = [
                row[index]
                for index, name in enumerate(visible_columns)
                if name != content_column
            ]
            if not primary_keys:
                projected.insert(0, row_count + 1)
            projected.extend((content_ref, None))
            projected_rows.append(projected)
            row_count += 1
        destination.executemany(insert_sql, projected_rows)

    select_columns = []
    for name in visible_columns:
        if name == content_column:
            canonical_value = (
                "CAST(c.content AS TEXT)" if content_affinity == "TEXT" else "c.content"
            )
            select_columns.append(
                f"COALESCE(b.__inline_content,{canonical_value}) AS {_quoted_identifier(name)}"
            )
        else:
            select_columns.append(f"b.{_quoted_identifier(name)}")
    destination.execute(
        f"CREATE VIEW {_quoted_identifier(projected_name)} AS SELECT "
        + ",".join(select_columns)
        + f" FROM {_quoted_identifier(backing_name)} b "
        "LEFT JOIN canonical_content_blob c "
        "ON c.content_ref=b.__canonical_content_ref"
    )
    return {
        "backing_name": backing_name,
        "column_names": visible_columns,
        "content_column": content_column,
        "primary_key_columns": primary_keys,
        "row_count": row_count,
    }


def _install_canonical_projection_triggers(
    connection: sqlite3.Connection,
    projection: dict[str, Any],
) -> None:
    """Preserve the existing write law for canonical-content projection views."""

    name = str(projection["name"])
    backing = str(projection["backing_name"])
    sector_id = str(projection["sector_id"])
    content_column = str(projection["content_column"])
    columns = [str(value) for value in projection["column_names"]]
    primary_keys = [str(value) for value in projection["primary_key_columns"]]
    quoted_view = _quoted_identifier(name)
    quoted_backing = _quoted_identifier(backing)

    def create_trigger(operation: str, statements: list[str]) -> None:
        trigger_name = _quoted_identifier(
            f"canonical_{name}_{operation.casefold()}"[:240]
        )
        connection.execute(
            f"CREATE TRIGGER {trigger_name} INSTEAD OF {operation} ON {quoted_view} "
            "BEGIN " + " ".join(statements) + " END"
        )

    if sector_id == "project_core":
        for operation in ("INSERT", "UPDATE", "DELETE"):
            create_trigger(operation, ["SELECT RAISE(ABORT,'PROJECT_CORE_ROUTER_METADATA_READ_ONLY');"])
        return

    non_content = [column for column in columns if column != content_column]
    insert_columns = non_content + ["__canonical_content_ref", "__inline_content"]
    insert_values = [f"NEW.{_quoted_identifier(column)}" for column in non_content]
    insert_values.extend(("NULL", f"NEW.{_quoted_identifier(content_column)}"))
    insert_statement = (
        f"INSERT INTO {quoted_backing}("
        + ",".join(_quoted_identifier(column) for column in insert_columns)
        + ") VALUES("
        + ",".join(insert_values)
        + ");"
    )

    if not primary_keys:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            create_trigger(
                operation,
                ["SELECT RAISE(ABORT,'CANONICAL_DERIVED_VIEW_READ_ONLY');"],
            )
        return

    where_old = " AND ".join(
        f"{_quoted_identifier(column)} IS OLD.{_quoted_identifier(column)}"
        for column in primary_keys
    )
    update_assignments = [
        f"{_quoted_identifier(column)}=NEW.{_quoted_identifier(column)}"
        for column in non_content
    ]
    update_assignments.extend(
        (
            '"__canonical_content_ref"=NULL',
            f'"__inline_content"=NEW.{_quoted_identifier(content_column)}',
        )
    )
    update_statement = (
        f"UPDATE {quoted_backing} SET "
        + ",".join(update_assignments)
        + f" WHERE {where_old};"
    )
    delete_statement = f"DELETE FROM {quoted_backing} WHERE {where_old};"

    if sector_id == "chat_lineage":
        create_trigger(
            "INSERT",
            [
                insert_statement,
                "INSERT INTO gemini_project_write_log"
                "(grant_id,sector_id,projected_table,operation,write_classification) "
                f"VALUES(NULL,'chat_lineage','{name}','INSERT','APPEND_ONLY_AUTOMATIC');",
            ],
        )
        for operation in ("UPDATE", "DELETE"):
            create_trigger(operation, ["SELECT RAISE(ABORT,'CHAT_LINEAGE_APPEND_ONLY');"])
        return

    grant_guard = (
        "SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM gemini_mutation_grant "
        f"WHERE sector_id='{sector_id}' AND status='ACTIVE' AND remaining_writes=1) "
        "THEN RAISE(ABORT,'ACTIVE_ONE_TURN_GRANT_REQUIRED') END;"
    )
    consume_grant = (
        "UPDATE gemini_mutation_grant "
        "SET status='CONSUMED',remaining_writes=0,consumed_at=CURRENT_TIMESTAMP "
        "WHERE grant_id=(SELECT grant_id FROM gemini_mutation_grant "
        f"WHERE sector_id='{sector_id}' AND status='ACTIVE' AND remaining_writes=1 "
        "ORDER BY issued_at,grant_id LIMIT 1);"
    )
    for operation, mutation in (
        ("INSERT", insert_statement),
        ("UPDATE", update_statement),
        ("DELETE", delete_statement),
    ):
        write_log = (
            "INSERT INTO gemini_project_write_log"
            "(grant_id,sector_id,projected_table,operation,write_classification) "
            "SELECT grant_id,"
            f"'{sector_id}','{name}','{operation}','NAMED_ONE_TURN_GRANT' "
            "FROM gemini_mutation_grant "
            f"WHERE sector_id='{sector_id}' AND status='CONSUMED' "
            "ORDER BY consumed_at DESC,grant_id LIMIT 1;"
        )
        create_trigger(operation, [grant_guard, consume_grant, mutation, write_log])


def _logical_table_fingerprint(
    connection: sqlite3.Connection,
    table: str,
    columns: list[tuple],
) -> tuple[str, int]:
    """Hash one logical table so identical sector copies can share storage."""

    names = [str(row[1]) for row in columns]
    schema = [
        (str(row[1]), _sqlite_affinity(row[2]), int(row[3] or 0), int(row[5] or 0))
        for row in columns
    ]
    digest = hashlib.sha256(
        json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    selected = ",".join(_quoted_identifier(name) for name in names)
    cursor = connection.execute(
        f"SELECT {selected} FROM {_quoted_identifier(table)}"
    )
    row_count = 0
    while True:
        rows = cursor.fetchmany(PROJECTION_COPY_BATCH_ROWS)
        if not rows:
            break
        for row in rows:
            digest.update(b"R")
            for value in row:
                if value is None:
                    digest.update(b"N")
                    continue
                if isinstance(value, bytes):
                    tag = b"B"
                    payload = value
                elif isinstance(value, str):
                    tag = b"T"
                    payload = value.encode("utf-8")
                elif isinstance(value, int):
                    tag = b"I"
                    payload = str(value).encode("ascii")
                elif isinstance(value, float):
                    tag = b"F"
                    payload = value.hex().encode("ascii")
                else:
                    tag = b"V"
                    payload = str(value).encode("utf-8")
                digest.update(tag)
                digest.update(len(payload).to_bytes(8, "little"))
                digest.update(payload)
            row_count += 1
    return digest.hexdigest(), row_count


def _install_shared_projection_read_only_triggers(
    connection: sqlite3.Connection,
    projection_name: str,
) -> None:
    quoted_view = _quoted_identifier(projection_name)
    for operation in ("INSERT", "UPDATE", "DELETE"):
        trigger_name = _quoted_identifier(
            f"shared_{projection_name}_{operation.casefold()}"[:240]
        )
        connection.execute(
            f"CREATE TRIGGER {trigger_name} INSTEAD OF {operation} ON {quoted_view} "
            "BEGIN SELECT RAISE(ABORT,'SHARED_CANONICAL_HISTORY_READ_ONLY'); END"
        )


def build_conjoined_project_sqlite(
    project_root: str | Path,
    destination: str | Path,
    *,
    read_only_stress: bool = False,
) -> dict[str, Any]:
    """Fuse current project core and sector SQLite files into one governed DB.

    Small source databases remain byte-reconstructible.  Large code databases
    use governed logical projections whose exact project-file payloads remain in
    code_exact_byte_chunk and whose repeated content is canonicalized once.
    Direct projections are namespaced by immutable sector identity.
    Project-core projections are physically read-only, Chat Lineage is append-only,
    and every other sector consumes one explicit named grant per write.
    """

    project = Path(project_root).resolve()
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    sources = sorted(
        (path for path in project.rglob("*.sqlite") if path.is_file()),
        key=lambda path: path.relative_to(project).as_posix(),
    )
    if not sources:
        raise GeminiExact10Error("PROJECT_CONJOIN_REQUIRES_SQLITE_SOURCES")
    with tempfile.TemporaryDirectory(prefix="evidenceos_conjoined_", dir=target.parent) as tmp:
        staged = Path(tmp) / target.name
        destination_connection = sqlite3.connect(staged)
        projected_tables: list[dict[str, Any]] = []
        source_hash_rows: list[tuple[str, str]] = []
        logical_table_registry: dict[str, dict[str, Any]] = {}
        sector_source_counts: dict[str, int] = {}
        source_set_sha256 = ""
        inline_blob_count = 0
        chunked_blob_count = 0
        logical_projection_count = 0
        embedded_chunk_count = 0
        canonical_content_blob_count = 0
        try:
            destination_connection.execute("PRAGMA foreign_keys=ON")
            destination_connection.executescript(
                """
                CREATE TABLE embedded_database_blob(
                    source_database_id TEXT PRIMARY KEY,
                    sector_id TEXT NOT NULL,
                    source_path TEXT NOT NULL UNIQUE,
                    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
                    source_byte_size INTEGER NOT NULL CHECK(source_byte_size >= 0),
                    integrity_check TEXT NOT NULL,
                    foreign_key_violation_count INTEGER NOT NULL,
                    content_storage TEXT NOT NULL
                        CHECK(content_storage IN ('INLINE_BLOB','CHUNKED_BLOB_V1','LOGICAL_PROJECTION_V2')),
                    content_chunk_count INTEGER NOT NULL CHECK(content_chunk_count >= 0),
                    content BLOB,
                    CHECK(
                        (content_storage='INLINE_BLOB'
                            AND content_chunk_count=0
                            AND content IS NOT NULL
                            AND length(content)=source_byte_size)
                        OR
                        (content_storage='CHUNKED_BLOB_V1'
                            AND content_chunk_count>0
                            AND content IS NULL)
                        OR
                        (content_storage='LOGICAL_PROJECTION_V2'
                            AND content_chunk_count=0
                            AND content IS NULL)
                    )
                ) WITHOUT ROWID;
                CREATE TABLE embedded_database_blob_chunk(
                    source_database_id TEXT NOT NULL
                        REFERENCES embedded_database_blob(source_database_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
                    chunk_offset INTEGER NOT NULL CHECK(chunk_offset >= 0),
                    chunk_byte_size INTEGER NOT NULL CHECK(chunk_byte_size > 0),
                    chunk_sha256 TEXT NOT NULL CHECK(length(chunk_sha256)=64),
                    content BLOB NOT NULL,
                    CHECK(length(content)=chunk_byte_size),
                    PRIMARY KEY(source_database_id,chunk_index)
                ) WITHOUT ROWID;
                CREATE TABLE source_schema_object_registry(
                    source_database_id TEXT NOT NULL REFERENCES embedded_database_blob(source_database_id),
                    sector_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_name TEXT NOT NULL,
                    object_sql TEXT,
                    projected_name TEXT,
                    projection_status TEXT NOT NULL,
                    PRIMARY KEY(source_database_id,object_type,object_name)
                ) WITHOUT ROWID;
                CREATE TABLE source_row_count_registry(
                    source_database_id TEXT NOT NULL REFERENCES embedded_database_blob(source_database_id),
                    sector_id TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    projected_table TEXT,
                    row_count INTEGER NOT NULL,
                    PRIMARY KEY(source_database_id,source_table)
                ) WITHOUT ROWID;
                CREATE TABLE canonical_content_blob(
                    content_ref TEXT PRIMARY KEY,
                    content_kind TEXT NOT NULL CHECK(content_kind IN ('TEXT_UTF8','BLOB')),
                    content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
                    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
                    content BLOB NOT NULL,
                    CHECK(length(content)=byte_size)
                ) WITHOUT ROWID;
                CREATE TABLE gemini_project_write_router(
                    sector_id TEXT PRIMARY KEY,
                    table_prefix TEXT NOT NULL,
                    access_mode TEXT NOT NULL,
                    automatic_write INTEGER NOT NULL CHECK(automatic_write IN (0,1)),
                    explicit_one_turn_grant_required INTEGER NOT NULL CHECK(explicit_one_turn_grant_required IN (0,1)),
                    relock_after_commit INTEGER NOT NULL CHECK(relock_after_commit IN (0,1)),
                    source_database_count INTEGER NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE gemini_mutation_grant(
                    grant_id TEXT PRIMARY KEY,
                    sector_id TEXT NOT NULL REFERENCES gemini_project_write_router(sector_id),
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE','CONSUMED','REVOKED')),
                    remaining_writes INTEGER NOT NULL CHECK(remaining_writes IN (0,1)),
                    issued_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    consumed_at TEXT
                ) WITHOUT ROWID;
                CREATE UNIQUE INDEX one_active_gemini_grant_per_sector
                    ON gemini_mutation_grant(sector_id) WHERE status='ACTIVE';
                CREATE TABLE gemini_project_write_log(
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grant_id TEXT,
                    sector_id TEXT NOT NULL,
                    projected_table TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    write_classification TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE gemini_project_head(
                    head_id TEXT PRIMARY KEY,
                    source_set_sha256 TEXT NOT NULL,
                    source_database_count INTEGER NOT NULL,
                    sector_count INTEGER NOT NULL,
                    derivation TEXT NOT NULL
                ) WITHOUT ROWID;
                """
            )
            occupied_names = {
                row[0]
                for row in destination_connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type IN ('table','view')"
                )
            }
            for source_path in sources:
                relative = source_path.relative_to(project).as_posix()
                source_id, sector_id, prefix = _project_database_identity(project, source_path)
                source_byte_size = source_path.stat().st_size
                sector_source_counts[sector_id] = sector_source_counts.get(sector_id, 0) + 1
                source_connection = sqlite3.connect(
                    f"file:{source_path.as_posix()}?mode=ro&immutable=1",
                    uri=True,
                )
                try:
                    integrity = source_connection.execute("PRAGMA integrity_check").fetchone()[0]
                    foreign_keys = source_connection.execute("PRAGMA foreign_key_check").fetchall()
                    if integrity != "ok" or foreign_keys:
                        raise GeminiExact10Error(f"PROJECT_SQLITE_INVALID:{relative}")
                    logical_source = source_byte_size >= PROJECT_LOGICAL_PROJECTION_MIN_BYTES
                    if logical_source:
                        source_sha256 = _sha256_file(source_path)
                        destination_connection.execute(
                            "INSERT INTO embedded_database_blob"
                            "(source_database_id,sector_id,source_path,source_sha256,"
                            "source_byte_size,integrity_check,foreign_key_violation_count,"
                            "content_storage,content_chunk_count,content) "
                            "VALUES(?,?,?,?,?,?,?,?,0,NULL)",
                            (
                                source_id,
                                sector_id,
                                relative,
                                source_sha256,
                                source_byte_size,
                                integrity,
                                len(foreign_keys),
                                "LOGICAL_PROJECTION_V2",
                            ),
                        )
                        logical_projection_count += 1
                    elif source_byte_size <= PROJECT_EMBED_INLINE_LIMIT_BYTES:
                        source_bytes = source_path.read_bytes()
                        source_sha256 = _sha256_bytes(source_bytes)
                        destination_connection.execute(
                            "INSERT INTO embedded_database_blob"
                            "(source_database_id,sector_id,source_path,source_sha256,"
                            "source_byte_size,integrity_check,foreign_key_violation_count,"
                            "content_storage,content_chunk_count,content) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                source_id,
                                sector_id,
                                relative,
                                source_sha256,
                                source_byte_size,
                                integrity,
                                len(foreign_keys),
                                "INLINE_BLOB",
                                0,
                                source_bytes,
                            ),
                        )
                        inline_blob_count += 1
                    else:
                        expected_chunk_count = (
                            source_byte_size + PROJECT_EMBED_CHUNK_BYTES - 1
                        ) // PROJECT_EMBED_CHUNK_BYTES
                        destination_connection.execute(
                            "INSERT INTO embedded_database_blob"
                            "(source_database_id,sector_id,source_path,source_sha256,"
                            "source_byte_size,integrity_check,foreign_key_violation_count,"
                            "content_storage,content_chunk_count,content) "
                            "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                            (
                                source_id,
                                sector_id,
                                relative,
                                "0" * 64,
                                source_byte_size,
                                integrity,
                                len(foreign_keys),
                                "CHUNKED_BLOB_V1",
                                expected_chunk_count,
                            ),
                        )
                        digest = hashlib.sha256()
                        chunk_index = 0
                        chunk_offset = 0
                        with source_path.open("rb") as source_stream:
                            while True:
                                chunk = source_stream.read(PROJECT_EMBED_CHUNK_BYTES)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                destination_connection.execute(
                                    "INSERT INTO embedded_database_blob_chunk"
                                    "(source_database_id,chunk_index,chunk_offset,"
                                    "chunk_byte_size,chunk_sha256,content) VALUES(?,?,?,?,?,?)",
                                    (
                                        source_id,
                                        chunk_index,
                                        chunk_offset,
                                        len(chunk),
                                        _sha256_bytes(chunk),
                                        chunk,
                                    ),
                                )
                                chunk_offset += len(chunk)
                                chunk_index += 1
                        if (
                            chunk_index != expected_chunk_count
                            or chunk_offset != source_byte_size
                        ):
                            raise GeminiExact10Error(
                                f"PROJECT_CHUNK_CAPTURE_INCOMPLETE:{relative}"
                            )
                        source_sha256 = digest.hexdigest()
                        destination_connection.execute(
                            "UPDATE embedded_database_blob SET source_sha256=? "
                            "WHERE source_database_id=?",
                            (source_sha256, source_id),
                        )
                        chunked_blob_count += 1
                        embedded_chunk_count += chunk_index
                    source_hash_rows.append((relative, source_sha256))
                    objects = source_connection.execute(
                        "SELECT type,name,sql FROM sqlite_schema "
                        "WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','index','trigger') "
                        "ORDER BY type,name"
                    ).fetchall()
                    for object_type, object_name, object_sql in objects:
                        projected_name: str | None = None
                        projection_status = "REGISTERED_SCHEMA_ONLY"
                        is_shadow = object_type == "table" and any(
                            object_name.endswith(suffix)
                            for suffix in ("_data", "_idx", "_content", "_docsize", "_config")
                        )
                        if object_type in {"table", "view"} and not is_shadow:
                            columns = [
                                row
                                for row in source_connection.execute(
                                    f"PRAGMA table_xinfo({_quoted_identifier(object_name)})"
                                ).fetchall()
                                if len(row) < 7 or int(row[6] or 0) == 0
                            ]
                            if columns:
                                projected_name = _projection_name(prefix, object_name)
                                if projected_name in occupied_names:
                                    projected_name = _projection_name(
                                        prefix + _safe_name(source_path.stem).casefold() + "__",
                                        object_name,
                                    )
                                if projected_name in occupied_names:
                                    raise GeminiExact10Error(
                                        f"PROJECT_PROJECTION_NAME_COLLISION:{relative}:{object_name}"
                                    )
                                occupied_names.add(projected_name)
                                if logical_source and object_name == "code_chunk_fts_source":
                                    column_names = [str(row[1]) for row in columns]
                                    expected_columns = [
                                        "rowid",
                                        "chunk_id",
                                        "relative_path",
                                        "symbol_name",
                                        "route_name",
                                        "chunk_text",
                                    ]
                                    if column_names != expected_columns:
                                        raise GeminiExact10Error(
                                            "FTS_SOURCE_COMPATIBILITY_SCHEMA_MISMATCH:"
                                            f"{relative}:{object_name}"
                                        )
                                    source_count = int(
                                        source_connection.execute(
                                            "SELECT COUNT(*) FROM code_chunk_fts_source"
                                        ).fetchone()[0]
                                    )
                                    comparison_sql = (
                                        "SELECT m.source_rowid AS rowid,f.chunk_id,f.relative_path,"
                                        "f.symbol_name,f.route_name,f.chunk_text "
                                        "FROM code_chunk_fts_meta m "
                                        "JOIN code_chunk_fts f USING(chunk_id)"
                                    )
                                    comparison_count = int(
                                        source_connection.execute(
                                            f"SELECT COUNT(*) FROM ({comparison_sql})"
                                        ).fetchone()[0]
                                    )
                                    visible_columns_sql = ",".join(
                                        _quoted_identifier(name) for name in expected_columns
                                    )
                                    left_difference = int(
                                        source_connection.execute(
                                            "SELECT COUNT(*) FROM (SELECT "
                                            f"{visible_columns_sql} FROM code_chunk_fts_source "
                                            f"EXCEPT {comparison_sql})"
                                        ).fetchone()[0]
                                    )
                                    right_difference = int(
                                        source_connection.execute(
                                            "SELECT COUNT(*) FROM ("
                                            f"{comparison_sql} EXCEPT SELECT {visible_columns_sql} "
                                            "FROM code_chunk_fts_source)"
                                        ).fetchone()[0]
                                    )
                                    if (
                                        source_count != comparison_count
                                        or left_difference
                                        or right_difference
                                    ):
                                        raise GeminiExact10Error(
                                            "FTS_SOURCE_COMPATIBILITY_DATA_MISMATCH:"
                                            f"{relative}:{source_count}:{comparison_count}:"
                                            f"{left_difference}:{right_difference}"
                                        )
                                    target_projection = _projection_name(prefix, "code_chunk_fts")
                                    metadata_projection = _projection_name(
                                        prefix,
                                        "code_chunk_fts_meta",
                                    )
                                    if (
                                        target_projection not in occupied_names
                                        or metadata_projection not in occupied_names
                                    ):
                                        raise GeminiExact10Error(
                                            "FTS_SOURCE_COMPATIBILITY_TARGET_MISSING:"
                                            f"{relative}"
                                        )
                                    destination_connection.execute(
                                        f"CREATE VIEW {_quoted_identifier(projected_name)} AS "
                                        "SELECT m.source_rowid AS rowid,f.chunk_id,f.relative_path,"
                                        "f.symbol_name,f.route_name,f.chunk_text FROM "
                                        f"{_quoted_identifier(metadata_projection)} m JOIN "
                                        f"{_quoted_identifier(target_projection)} f USING(chunk_id)"
                                    )
                                    destination_connection.execute(
                                        "INSERT INTO source_row_count_registry VALUES(?,?,?,?,?)",
                                        (
                                            source_id,
                                            sector_id,
                                            object_name,
                                            projected_name,
                                            source_count,
                                        ),
                                    )
                                    projected_tables.append(
                                        {
                                            "name": projected_name,
                                            "sector_id": sector_id,
                                            "kind": "canonical_fts_source_view",
                                        }
                                    )
                                    destination_connection.execute(
                                        "INSERT INTO source_schema_object_registry VALUES(?,?,?,?,?,?,?)",
                                        (
                                            source_id,
                                            sector_id,
                                            object_type,
                                            object_name,
                                            object_sql,
                                            projected_name,
                                            "CANONICAL_FTS_SOURCE_VIEW_V2",
                                        ),
                                    )
                                    continue
                                compatibility_target = LOGICAL_COMPATIBILITY_REUSE_TARGETS.get(
                                    object_name
                                )
                                if logical_source and compatibility_target:
                                    target_columns = [
                                        row
                                        for row in source_connection.execute(
                                            f"PRAGMA table_xinfo({_quoted_identifier(compatibility_target)})"
                                        ).fetchall()
                                        if len(row) < 7 or int(row[6] or 0) == 0
                                    ]
                                    column_names = [str(row[1]) for row in columns]
                                    target_column_names = [str(row[1]) for row in target_columns]
                                    if column_names != target_column_names:
                                        raise GeminiExact10Error(
                                            "LOGICAL_COMPATIBILITY_SCHEMA_MISMATCH:"
                                            f"{relative}:{object_name}:{compatibility_target}"
                                        )
                                    selected_columns = ",".join(
                                        _quoted_identifier(name) for name in column_names
                                    )
                                    source_count = int(
                                        source_connection.execute(
                                            f"SELECT COUNT(*) FROM {_quoted_identifier(object_name)}"
                                        ).fetchone()[0]
                                    )
                                    target_count = int(
                                        source_connection.execute(
                                            f"SELECT COUNT(*) FROM {_quoted_identifier(compatibility_target)}"
                                        ).fetchone()[0]
                                    )
                                    left_difference = int(
                                        source_connection.execute(
                                            "SELECT COUNT(*) FROM (SELECT "
                                            f"{selected_columns} FROM {_quoted_identifier(object_name)} "
                                            "EXCEPT SELECT "
                                            f"{selected_columns} FROM {_quoted_identifier(compatibility_target)})"
                                        ).fetchone()[0]
                                    )
                                    right_difference = int(
                                        source_connection.execute(
                                            "SELECT COUNT(*) FROM (SELECT "
                                            f"{selected_columns} FROM {_quoted_identifier(compatibility_target)} "
                                            "EXCEPT SELECT "
                                            f"{selected_columns} FROM {_quoted_identifier(object_name)})"
                                        ).fetchone()[0]
                                    )
                                    if (
                                        source_count != target_count
                                        or left_difference
                                        or right_difference
                                    ):
                                        raise GeminiExact10Error(
                                            "LOGICAL_COMPATIBILITY_DATA_MISMATCH:"
                                            f"{relative}:{object_name}:{compatibility_target}:"
                                            f"{source_count}:{target_count}:"
                                            f"{left_difference}:{right_difference}"
                                        )
                                    target_projection = _projection_name(
                                        prefix,
                                        compatibility_target,
                                    )
                                    if target_projection not in occupied_names:
                                        raise GeminiExact10Error(
                                            "LOGICAL_COMPATIBILITY_TARGET_MISSING:"
                                            f"{relative}:{object_name}:{compatibility_target}"
                                        )
                                    destination_connection.execute(
                                        f"CREATE VIEW {_quoted_identifier(projected_name)} AS "
                                        f"SELECT {selected_columns} FROM "
                                        f"{_quoted_identifier(target_projection)}"
                                    )
                                    destination_connection.execute(
                                        "INSERT INTO source_row_count_registry VALUES(?,?,?,?,?)",
                                        (
                                            source_id,
                                            sector_id,
                                            object_name,
                                            projected_name,
                                            source_count,
                                        ),
                                    )
                                    projected_tables.append(
                                        {
                                            "name": projected_name,
                                            "sector_id": sector_id,
                                            "kind": "canonical_compatibility_view",
                                        }
                                    )
                                    destination_connection.execute(
                                        "INSERT INTO source_schema_object_registry VALUES(?,?,?,?,?,?,?)",
                                        (
                                            source_id,
                                            sector_id,
                                            object_type,
                                            object_name,
                                            object_sql,
                                            projected_name,
                                            "CANONICAL_COMPATIBILITY_VIEW_V2",
                                        ),
                                    )
                                    continue
                                if logical_source and object_name in CANONICAL_CONTENT_COLUMNS:
                                    (
                                        canonical_fingerprint,
                                        canonical_row_count,
                                    ) = _logical_table_fingerprint(
                                        source_connection,
                                        object_name,
                                        columns,
                                    )
                                    shared = logical_table_registry.get(canonical_fingerprint)
                                    if shared is not None:
                                        column_names = [str(row[1]) for row in columns]
                                        selected_columns = ",".join(
                                            _quoted_identifier(name)
                                            for name in column_names
                                        )
                                        destination_connection.execute(
                                            f"CREATE VIEW {_quoted_identifier(projected_name)} AS "
                                            f"SELECT {selected_columns} FROM "
                                            f"{_quoted_identifier(str(shared['projected_name']))}"
                                        )
                                        destination_connection.execute(
                                            "INSERT INTO source_row_count_registry VALUES(?,?,?,?,?)",
                                            (
                                                source_id,
                                                sector_id,
                                                object_name,
                                                projected_name,
                                                canonical_row_count,
                                            ),
                                        )
                                        projected_tables.append(
                                            {
                                                "name": projected_name,
                                                "sector_id": sector_id,
                                                "kind": "shared_canonical_view",
                                            }
                                        )
                                        destination_connection.execute(
                                            "INSERT INTO source_schema_object_registry VALUES(?,?,?,?,?,?,?)",
                                            (
                                                source_id,
                                                sector_id,
                                                object_type,
                                                object_name,
                                                object_sql,
                                                projected_name,
                                                "SHARED_CANONICAL_CONTENT_VIEW_V2",
                                            ),
                                        )
                                        continue
                                    canonical_projection = _canonical_content_projection(
                                        destination_connection,
                                        source_connection,
                                        source_database_id=source_id,
                                        projected_name=projected_name,
                                        source_table=object_name,
                                        columns=columns,
                                    )
                                    row_count = canonical_projection["row_count"]
                                    destination_connection.execute(
                                        "INSERT INTO source_row_count_registry VALUES(?,?,?,?,?)",
                                        (
                                            source_id,
                                            sector_id,
                                            object_name,
                                            projected_name,
                                            row_count,
                                        ),
                                    )
                                    projected_tables.append(
                                        {
                                            "name": projected_name,
                                            "sector_id": sector_id,
                                            "kind": "canonical_view",
                                            **canonical_projection,
                                        }
                                    )
                                    projection_status = "CANONICAL_CONTENT_VIEW_V2"
                                    if canonical_projection["row_count"] != canonical_row_count:
                                        raise GeminiExact10Error(
                                            "CANONICAL_LOGICAL_ROW_COUNT_MISMATCH:"
                                            f"{relative}:{object_name}"
                                        )
                                    logical_table_registry[canonical_fingerprint] = {
                                        "projected_name": projected_name,
                                        "row_count": canonical_row_count,
                                    }
                                    destination_connection.execute(
                                        "INSERT INTO source_schema_object_registry VALUES(?,?,?,?,?,?,?)",
                                        (
                                            source_id,
                                            sector_id,
                                            object_type,
                                            object_name,
                                            object_sql,
                                            projected_name,
                                            projection_status,
                                        ),
                                    )
                                    continue
                                logical_fingerprint: str | None = None
                                if logical_source and object_type in {"table", "view"}:
                                    (
                                        logical_fingerprint,
                                        logical_row_count,
                                    ) = _logical_table_fingerprint(
                                        source_connection,
                                        object_name,
                                        columns,
                                    )
                                    shared = logical_table_registry.get(logical_fingerprint)
                                    if shared is not None:
                                        column_names = [str(row[1]) for row in columns]
                                        selected_columns = ",".join(
                                            _quoted_identifier(name)
                                            for name in column_names
                                        )
                                        destination_connection.execute(
                                            f"CREATE VIEW {_quoted_identifier(projected_name)} AS "
                                            f"SELECT {selected_columns} FROM "
                                            f"{_quoted_identifier(str(shared['projected_name']))}"
                                        )
                                        destination_connection.execute(
                                            "INSERT INTO source_row_count_registry VALUES(?,?,?,?,?)",
                                            (
                                                source_id,
                                                sector_id,
                                                object_name,
                                                projected_name,
                                                logical_row_count,
                                            ),
                                        )
                                        projected_tables.append(
                                            {
                                                "name": projected_name,
                                                "sector_id": sector_id,
                                                "kind": "shared_view",
                                            }
                                        )
                                        projection_status = "SHARED_LOGICAL_TABLE_VIEW_V2"
                                        destination_connection.execute(
                                            "INSERT INTO source_schema_object_registry VALUES(?,?,?,?,?,?,?)",
                                            (
                                                source_id,
                                                sector_id,
                                                object_type,
                                                object_name,
                                                object_sql,
                                                projected_name,
                                                projection_status,
                                            ),
                                        )
                                        continue
                                primary_key_columns = sorted(
                                    ((int(row[5]), str(row[1])) for row in columns if int(row[5] or 0) > 0),
                                    key=lambda value: value[0],
                                )
                                definitions = []
                                for row in columns:
                                    definition = (
                                        f"{_quoted_identifier(str(row[1]))} "
                                        f"{_sqlite_affinity(row[2])}"
                                    )
                                    if int(row[3] or 0):
                                        definition += " NOT NULL"
                                    if len(primary_key_columns) == 1 and int(row[5] or 0) > 0:
                                        definition += " PRIMARY KEY"
                                    definitions.append(definition)
                                if len(primary_key_columns) > 1:
                                    definitions.append(
                                        "PRIMARY KEY(" + ",".join(
                                            _quoted_identifier(name)
                                            for _order, name in primary_key_columns
                                        ) + ")"
                                    )
                                destination_connection.execute(
                                    f"CREATE TABLE {_quoted_identifier(projected_name)}"
                                    f"({','.join(definitions)})"
                                )
                                column_names = [str(row[1]) for row in columns]
                                selected_columns = ",".join(
                                    _quoted_identifier(name) for name in column_names
                                )
                                source_cursor = source_connection.execute(
                                    f"SELECT {selected_columns} FROM {_quoted_identifier(object_name)}"
                                )
                                placeholders = ",".join("?" for _ in column_names)
                                row_count = 0
                                while True:
                                    rows = source_cursor.fetchmany(PROJECTION_COPY_BATCH_ROWS)
                                    if not rows:
                                        break
                                    destination_connection.executemany(
                                        f"INSERT INTO {_quoted_identifier(projected_name)} "
                                        f"VALUES({placeholders})",
                                        rows,
                                    )
                                    row_count += len(rows)
                                destination_connection.execute(
                                    "INSERT INTO source_row_count_registry VALUES(?,?,?,?,?)",
                                    (
                                        source_id,
                                        sector_id,
                                        object_name,
                                        projected_name,
                                        row_count,
                                    ),
                                )
                                projected_tables.append(
                                    {
                                        "name": projected_name,
                                        "sector_id": sector_id,
                                        "kind": "table",
                                    }
                                )
                                projection_status = (
                                    "MATERIALIZED_VIEW"
                                    if object_type == "view"
                                    else "MATERIALIZED_TABLE"
                                )
                                if logical_fingerprint is not None:
                                    logical_table_registry[logical_fingerprint] = {
                                        "projected_name": projected_name,
                                        "row_count": row_count,
                                    }
                        destination_connection.execute(
                            "INSERT INTO source_schema_object_registry VALUES(?,?,?,?,?,?,?)",
                            (
                                source_id,
                                sector_id,
                                object_type,
                                object_name,
                                object_sql,
                                projected_name,
                                projection_status,
                            ),
                        )
                finally:
                    source_connection.close()

            observed_sector_layout = tuple(sorted(
                sector_id for sector_id in sector_source_counts if sector_id != "project_core"
            ))
            canonical_sector_layout = next(
                (
                    tuple(sorted(layout))
                    for layout in ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS
                    if tuple(sorted(layout)) == observed_sector_layout
                ),
                (),
            )
            # Known live layouts derive their router count from the canonical
            # registry. Keep an unknown observed layout materializable so the
            # package validator can report the precise unexpected-sector class.
            sector_ids = list(canonical_sector_layout or observed_sector_layout)
            for sector_id in sector_ids:
                is_automatic_append = sector_id in {"chat_lineage", "research"}
                destination_connection.execute(
                    "INSERT INTO gemini_project_write_router VALUES(?,?,?,?,?,?,?)",
                    (
                        sector_id,
                        f"sector_{sector_id}__",
                        (
                            "READ_ONLY"
                            if read_only_stress
                            else "APPEND_ONLY_READ_WRITE" if is_automatic_append else "READ_ONLY"
                        ),
                        0 if read_only_stress else 1 if is_automatic_append else 0,
                        0 if read_only_stress else 0 if is_automatic_append else 1,
                        1,
                        sector_source_counts[sector_id],
                    ),
                )
            source_set_sha256 = _sha256_bytes(
                json.dumps(source_hash_rows, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )
            destination_connection.execute(
                "INSERT INTO gemini_project_head VALUES(?,?,?,?,?)",
                (
                    "current",
                    source_set_sha256,
                    len(sources),
                    len(sector_ids),
                    "VALIDATED_CHATGPT_PROJECT_SQLITES_FUSED_BY_SECTOR_ID",
                ),
            )

            for projection in projected_tables:
                projected_name = str(projection["name"])
                sector_id = str(projection["sector_id"])
                if projection["kind"] == "canonical_view":
                    _install_canonical_projection_triggers(
                        destination_connection,
                        projection,
                    )
                    continue
                if projection["kind"] in {
                    "shared_view",
                    "shared_canonical_view",
                    "canonical_compatibility_view",
                    "canonical_fts_source_view",
                }:
                    _install_shared_projection_read_only_triggers(
                        destination_connection,
                        projected_name,
                    )
                    continue
                quoted_table = _quoted_identifier(projected_name)
                if sector_id == "project_core":
                    for operation in ("INSERT", "UPDATE", "DELETE"):
                        trigger = _quoted_identifier(
                            f"lock_{projected_name}_{operation.casefold()}"[:240]
                        )
                        destination_connection.execute(
                            f"CREATE TRIGGER {trigger} BEFORE {operation} ON {quoted_table} "
                            "BEGIN SELECT RAISE(ABORT,'PROJECT_CORE_ROUTER_METADATA_READ_ONLY'); END"
                        )
                    continue
                if sector_id in {"chat_lineage", "research"}:
                    for operation in ("UPDATE", "DELETE"):
                        trigger = _quoted_identifier(
                            f"append_only_{projected_name}_{operation.casefold()}"[:240]
                        )
                        destination_connection.execute(
                            f"CREATE TRIGGER {trigger} BEFORE {operation} ON {quoted_table} "
                            f"BEGIN SELECT RAISE(ABORT,'{sector_id.upper()}_APPEND_ONLY'); END"
                        )
                    trigger = _quoted_identifier(f"log_{projected_name}_insert"[:240])
                    destination_connection.execute(
                        f"CREATE TRIGGER {trigger} AFTER INSERT ON {quoted_table} BEGIN "
                        "INSERT INTO gemini_project_write_log"
                        "(grant_id,sector_id,projected_table,operation,write_classification) "
                        f"VALUES(NULL,'{sector_id}','{projected_name}','INSERT','APPEND_ONLY_AUTOMATIC'); END"
                    )
                    continue
                for operation in ("INSERT", "UPDATE", "DELETE"):
                    trigger = _quoted_identifier(
                        f"grant_{projected_name}_{operation.casefold()}"[:240]
                    )
                    destination_connection.execute(
                        f"CREATE TRIGGER {trigger} BEFORE {operation} ON {quoted_table} BEGIN "
                        "SELECT CASE WHEN NOT EXISTS("
                        "SELECT 1 FROM gemini_mutation_grant "
                        f"WHERE sector_id='{sector_id}' AND status='ACTIVE' AND remaining_writes=1"
                        ") THEN RAISE(ABORT,'ACTIVE_ONE_TURN_GRANT_REQUIRED') END; "
                        "UPDATE gemini_mutation_grant "
                        "SET status='CONSUMED',remaining_writes=0,consumed_at=CURRENT_TIMESTAMP "
                        "WHERE grant_id=(SELECT grant_id FROM gemini_mutation_grant "
                        f"WHERE sector_id='{sector_id}' AND status='ACTIVE' AND remaining_writes=1 "
                        "ORDER BY issued_at,grant_id LIMIT 1); "
                        "INSERT INTO gemini_project_write_log"
                        "(grant_id,sector_id,projected_table,operation,write_classification) "
                        "SELECT grant_id,"
                        f"'{sector_id}','{projected_name}','{operation}','NAMED_ONE_TURN_GRANT' "
                        "FROM gemini_mutation_grant "
                        f"WHERE sector_id='{sector_id}' AND status='CONSUMED' "
                        "ORDER BY consumed_at DESC,grant_id LIMIT 1; END"
                    )
            destination_connection.commit()
            destination_connection.execute("VACUUM")
            integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = destination_connection.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise GeminiExact10Error("PROJECT_CONJOINED_SQLITE_VALIDATION_FAILED")
            canonical_content_blob_count = int(
                destination_connection.execute(
                    "SELECT COUNT(*) FROM canonical_content_blob"
                ).fetchone()[0]
            )
        finally:
            destination_connection.close()
        staged.replace(target)
    return {
        "path": str(target),
        "sha256": _sha256_file(target),
        "source_database_count": len(sources),
        "sector_count": len(sector_source_counts) - (1 if "project_core" in sector_source_counts else 0),
        "embedded_database_blob_count": len(sources),
        "embedded_database_inline_count": inline_blob_count,
        "embedded_database_chunked_count": chunked_blob_count,
        "embedded_database_logical_projection_count": logical_projection_count,
        "embedded_database_chunk_count": embedded_chunk_count,
        "canonical_content_blob_count": canonical_content_blob_count,
        "source_set_sha256": source_set_sha256,
    }


def _project_manifest(project_root: Path) -> dict[str, Any]:
    files = []
    for path in _iter_files(project_root):
        files.append(
            {
                "path": path.relative_to(project_root).as_posix(),
                "byte_size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    sources: list[dict[str, Any]] = []
    router = project_root / "project_router.sqlite"
    if router.is_file():
        connection = sqlite3.connect(f"file:{router.as_posix()}?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_registry'"
            ).fetchone():
                sources = [dict(row) for row in connection.execute("SELECT * FROM source_registry ORDER BY source_id")]
        finally:
            connection.close()
    if not sources:
        for database in sorted((project_root / "sectors").glob("*/*_sector_v001.sqlite")):
            connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            try:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_registry'"
                ).fetchone():
                    connection.row_factory = sqlite3.Row
                    for row in connection.execute("SELECT * FROM source_registry ORDER BY source_id"):
                        item = dict(row)
                        item["sector_database"] = database.relative_to(project_root).as_posix()
                        sources.append(item)
            finally:
                connection.close()
    return {
        "contract": "T021_SOURCE_AND_ARTIFACT_MANIFEST_V1",
        "project_files": files,
        "registered_sources": sources,
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _deterministic_zip(stage: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=False,
    ) as archive:
        for name in T021_GEMINI_EXACT10_NAMES:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 9
            info.external_attr = 0o100644 << 16
            info.file_size = (stage / name).stat().st_size
            with (stage / name).open("rb") as source, archive.open(
                info,
                "w",
            ) as target:
                shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
    temporary.replace(destination)


def _legacy_export_gemini_exact10_direct(
    brain_root: str | Path,
    brain_name: str,
    flash_prompt: str,
) -> dict[str, Any]:
    root = Path(brain_root)
    project = root / "project"
    topology = project / "topology"
    packages = root / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    retired_provider_outputs = retire_provider_outputs(packages, "GEMINI")
    clear_provider_skip_marker(packages, "GEMINI")
    required_topology = {
        "PROJECT_TOPOLOGY.mmd": topology / "project_master_topology.mmd",
        "PROJECT_TOPOLOGY.svg": topology / "project_master_topology.svg",
        "PROJECT_TOPOLOGY.png": topology / "project_master_topology.png",
    }
    missing = [str(path) for path in required_topology.values() if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise GeminiExact10Error("PROJECT_TOPOLOGY_RENDER_INCOMPLETE:" + ";".join(missing))

    stage = packages / f"{_safe_name(brain_name).lower()}_gemini_exact10_v001"
    if stage.exists():
        import shutil

        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    (stage / "GEMINI_FLASH_PROMPT.md").write_text(flash_prompt.rstrip() + "\n", encoding="utf-8")
    env_result = build_env15_public_runtime_sqlite(
        stage / "UEPC_ENV15_PUBLIC_RUNTIME.sqlite",
        runtime_root=root if (root / ".uepc_env").is_file() else None,
    )
    project_result = build_conjoined_project_sqlite(project, stage / "GENERATED_PROJECT_CONJOINED.sqlite")
    for destination_name, source_path in required_topology.items():
        (stage / destination_name).write_bytes(source_path.read_bytes())

    pointer_payload: dict[str, Any] = {
        "contract": "T021_GEMINI_EXACT10_POINTERS_V1",
        "env15_runtime": "UEPC_ENV15_PUBLIC_RUNTIME.sqlite",
        "generated_project": "GENERATED_PROJECT_CONJOINED.sqlite",
        "topology": ["PROJECT_TOPOLOGY.mmd", "PROJECT_TOPOLOGY.svg", "PROJECT_TOPOLOGY.png"],
        "project_pointer": None,
        "sector_index": None,
    }
    for key, source_path in (
        ("project_pointer", project / "project_pointer.json"),
        ("sector_index", project / "sector_index.json"),
    ):
        if source_path.is_file():
            pointer_payload[key] = json.loads(source_path.read_text(encoding="utf-8"))
    _write_json(stage / "PROJECT_POINTERS.json", pointer_payload)
    _write_json(stage / "SOURCE_AND_ARTIFACT_MANIFEST.json", _project_manifest(project))
    (stage / "RECOVERY_AND_NEXT_POINTER.md").write_text(
        "# Recovery and next pointer\n\n"
        "1. Open `PROJECT_POINTERS.json`.\n"
        "2. Read `UEPC_ENV15_PUBLIC_RUNTIME.sqlite` for locked Env15 governance bytes.\n"
        "3. Read `GENERATED_PROJECT_CONJOINED.sqlite` for generated project truth.\n"
        "4. Use `PROJECT_TOPOLOGY.mmd` as topology source; SVG and PNG are rendered proofs.\n"
        "5. Research and Chat Lineage permit automatic append-write. Every other Project lane requires a named one-turn HIL grant, receipt, snapshot, and relock.\n",
        encoding="utf-8",
    )
    hashes = {
        name: {"sha256": _sha256_file(stage / name), "byte_size": (stage / name).stat().st_size}
        for name in T021_GEMINI_EXACT10_NAMES
        if name != "RECEIPTS_AND_HASHES.json"
    }
    _write_json(
        stage / "RECEIPTS_AND_HASHES.json",
        {
            "contract": "T021_GEMINI_EXACT10_RECEIPT_V1",
            "root_file_count": 10,
            "nested_zip_count": 0,
            "all_files_directly_readable": True,
            "env15_runtime": env_result,
            "generated_project": project_result,
            "files": hashes,
        },
    )

    observed = tuple(sorted(path.name for path in stage.iterdir() if path.is_file()))
    if observed != tuple(sorted(T021_GEMINI_EXACT10_NAMES)):
        raise GeminiExact10Error(f"EXACT10_STAGE_CONTENT_MISMATCH:{observed!r}")
    archive_path = packages / f"Gemini_{_safe_name(brain_name)}_Sqlite_brain.zip"
    _deterministic_zip(stage, archive_path)
    validation = validate_gemini_exact10(archive_path)
    if validation["status"] != "PASS":
        raise GeminiExact10Error("GEMINI_EXACT10_VALIDATION_FAILED:" + ";".join(validation["errors"]))
    return {
        "gemini_package_zip": str(archive_path),
        "package_folder": str(stage),
        "sha256": _sha256_file(archive_path),
        "validation": validation,
        "env15_runtime": env_result,
        "generated_project": project_result,
    }


def _write_pointer_file(path: Path, rows: Iterable[tuple[str, object]]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in rows),
        encoding="utf-8",
    )


def _copy_chatgpt_member(
    archive: zipfile.ZipFile,
    member: str,
    destination: Path,
) -> dict[str, Any]:
    try:
        info = archive.getinfo(member)
    except KeyError as exc:
        raise GeminiExact10Error(f"CHATGPT_DERIVATION_MEMBER_MISSING:{member}") from exc
    if info.file_size <= 0:
        raise GeminiExact10Error(f"CHATGPT_DERIVATION_MEMBER_EMPTY:{member}")
    digest = hashlib.sha256()
    byte_size = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(info, "r") as source, destination.open("wb") as target:
        while True:
            block = source.read(8 * 1024 * 1024)
            if not block:
                break
            target.write(block)
            digest.update(block)
            byte_size += len(block)
    if byte_size != info.file_size:
        raise GeminiExact10Error(f"CHATGPT_DERIVATION_MEMBER_TRUNCATED:{member}")
    return {
        "source_member": member,
        "path": str(destination),
        "sha256": digest.hexdigest(),
        "byte_size": byte_size,
    }


def export_gemini_exact10_direct(
    brain_root: str | Path,
    brain_name: str,
    flash_prompt: str,
    *,
    chatgpt_package: str | Path | None = None,
    package_use_mode: str = "CANONICAL_FLASHABLE",
    maximum_bytes: int = GEMINI_PACKAGE_MAX_BYTES,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    packages = root / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    retired_provider_outputs = retire_provider_outputs(packages, "GEMINI")
    clear_provider_skip_marker(packages, "GEMINI")
    normalized_use_mode = str(package_use_mode or "CANONICAL_FLASHABLE").strip().upper()
    size_limit = min(int(maximum_bytes), GEMINI_PACKAGE_MAX_BYTES)
    if size_limit <= 0:
        raise GeminiExact10Error("GEMINI_PACKAGE_SIZE_LIMIT_INVALID")
    if normalized_use_mode not in {"CANONICAL_FLASHABLE", "READ_ONLY_STRESS_RESULT"}:
        raise GeminiExact10Error(f"PACKAGE_USE_MODE_INVALID:{normalized_use_mode}")
    read_only_stress = normalized_use_mode == "READ_ONLY_STRESS_RESULT"
    stage_variant = "read_only_stress_result" if read_only_stress else "current"
    stage = packages / f"{_safe_name(brain_name).lower()}_gemini_exact10_{stage_variant}_v001"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    source_chatgpt_package_sha256 = ""
    source_chatgpt_validation: dict[str, Any] | None = None
    if chatgpt_package:
        chatgpt_path = Path(chatgpt_package).resolve()
        source_chatgpt_validation = validate_chatgpt_package(chatgpt_path)
        if source_chatgpt_validation["status"] != "PASS":
            raise GeminiExact10Error(
                "CHATGPT_SOURCE_PACKAGE_VALIDATION_FAILED:"
                + ";".join(source_chatgpt_validation.get("errors") or [])
            )
        source_chatgpt_package_sha256 = _sha256_file(chatgpt_path)
        with zipfile.ZipFile(chatgpt_path, "r") as archive:
            env_result = _copy_chatgpt_member(
                archive,
                "env/env_sqlite.sqlite",
                stage / "ENV_PUBLIC_READONLY.sqlite",
            )
            uop_result = _copy_chatgpt_member(
                archive,
                "uop/uop_sqlite.sqlite",
                stage / "UOP_PUBLIC_READONLY.sqlite",
            )
            env_png_result = _copy_chatgpt_member(
                archive,
                "env/env_mmd.png",
                stage / "ENV_MMD_RENDER.png",
            )
            uop_png_result = _copy_chatgpt_member(
                archive,
                "uop/uop_mmd.png",
                stage / "UOP_MMD_RENDER.png",
            )
            with tempfile.TemporaryDirectory(
                prefix="evidenceos_gemini_from_chatgpt_",
                dir=packages,
            ) as temporary_project:
                project = Path(temporary_project) / "project"
                project.mkdir()
                project_members = [
                    name
                    for name in archive.namelist()
                    if name.startswith("project/")
                    and name.casefold().endswith((".sqlite", ".sqlite3", ".db"))
                    and not name.endswith("/")
                ]
                if not project_members:
                    raise GeminiExact10Error("CHATGPT_PROJECT_SQLITES_MISSING")
                for member in sorted(project_members):
                    pure = PurePosixPath(member)
                    if (
                        pure.is_absolute()
                        or len(pure.parts) < 2
                        or pure.parts[0] != "project"
                        or any(part in {"", ".", ".."} for part in pure.parts)
                        or "\\" in member
                    ):
                        raise GeminiExact10Error(f"CHATGPT_PROJECT_MEMBER_UNSAFE:{member}")
                    destination = project.joinpath(*pure.parts[1:])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _copy_chatgpt_member(archive, member, destination)
                project_result = build_conjoined_project_sqlite(
                    project,
                    stage / "PROJECT_CONJOINED_WRITE.sqlite",
                    read_only_stress=read_only_stress,
                )
        derivation_mode = "VALIDATED_CHATGPT_PACKAGE"
    else:
        authority_root = root if (root / ".uepc_env").is_file() else find_env15_resource_root()
        required = {
            "ENV_PUBLIC_READONLY.sqlite": authority_root / "env" / "env_sqlite.sqlite",
            "UOP_PUBLIC_READONLY.sqlite": authority_root / "uop" / "uop_sqlite.sqlite",
            "ENV_MMD_RENDER.png": authority_root / "env" / "env_mmd.png",
            "UOP_MMD_RENDER.png": authority_root / "uop" / "uop_mmd.png",
        }
        missing = [
            str(path)
            for path in required.values()
            if not path.is_file() or path.stat().st_size == 0
        ]
        if missing:
            raise GeminiExact10Error("CURRENT_ENV15_DIRECT_MEMBER_MISSING:" + ";".join(missing))
        direct_results: dict[str, dict[str, Any]] = {}
        for destination_name, source_path in required.items():
            payload = source_path.read_bytes()
            destination = stage / destination_name
            destination.write_bytes(payload)
            direct_results[destination_name] = {
                "source_member": str(source_path),
                "path": str(destination),
                "sha256": _sha256_bytes(payload),
                "byte_size": len(payload),
            }
        env_result = direct_results["ENV_PUBLIC_READONLY.sqlite"]
        uop_result = direct_results["UOP_PUBLIC_READONLY.sqlite"]
        env_png_result = direct_results["ENV_MMD_RENDER.png"]
        uop_png_result = direct_results["UOP_MMD_RENDER.png"]
        project_result = build_conjoined_project_sqlite(
            root / "project",
            stage / "PROJECT_CONJOINED_WRITE.sqlite",
            read_only_stress=read_only_stress,
        )
        derivation_mode = "CURRENT_BRAIN_DIRECT_COMPATIBILITY"

    source_hash_value = source_chatgpt_package_sha256 or "UNAVAILABLE_DIRECT_COMPATIBILITY"
    _write_pointer_file(
        stage / ".uepc_env",
        (
            ("UEPC_ENV_VERSION", "V15"),
            ("PACKAGE_CLASS", "GEMINI_EXACT10_CURRENT_ENV15"),
            ("PACKAGE_USE_MODE", normalized_use_mode),
            ("STRUCTURE_CONTRACT", "T023_GEMINI_EXACT10_STRUCTURE_V1"),
            ("DERIVATION_MODE", derivation_mode),
            ("ENV_SQLITE", "ENV_PUBLIC_READONLY.sqlite"),
            ("ENV_MMD_PNG", "ENV_MMD_RENDER.png"),
            ("ENV_WRITE_LOCK", "true"),
            ("ENV_DEFAULT_OPEN_MODE", "mode=ro&immutable=1"),
            ("PROFILE_POINTER", ".uepc_profile"),
            ("PROJECT_POINTER", ".uepc_project"),
            ("PROJECT_WRITE_POINTER", ".uepc_project_write"),
            ("MUTATION_INSTRUCTIONS_AUTHORIZED", str(not read_only_stress).lower()),
            ("FLASH_PROMPT", "GEMINI_FLASH_PROMPT.txt"),
            ("SOURCE_CHATGPT_PACKAGE_SHA256", source_hash_value),
        ),
    )
    _write_pointer_file(
        stage / ".uepc_profile",
        (
            ("PROFILE_ID", "UOP_PUBLIC_GOVERNANCE_V15_CURRENT"),
            ("UOP_SQLITE", "UOP_PUBLIC_READONLY.sqlite"),
            ("UOP_MMD_PNG", "UOP_MMD_RENDER.png"),
            ("UOP_WRITE_LOCK", "true"),
            ("UOP_DEFAULT_OPEN_MODE", "mode=ro&immutable=1"),
            ("CAN_OVERRIDE_ENV", "false"),
            ("CAN_OVERRIDE_PROJECT", "false"),
            ("PUBLIC_PRIVATE_STATE", "GOVERNANCE_ONLY_NO_PRIVATE_PAYLOAD"),
        ),
    )
    _write_pointer_file(
        stage / ".uepc_project",
        (
            ("PROJECT_ID", f"{_safe_name(brain_name)}_CURRENT_PROJECT"),
            ("PROJECT_SQLITE", "PROJECT_CONJOINED_WRITE.sqlite"),
            ("PROJECT_CORE_NAMESPACE", "project_core__"),
            ("PROJECT_CORE_ACCESS", "READ_ONLY_ROUTER_METADATA"),
            ("PROJECT_SECTOR_COUNT", project_result["sector_count"]),
            ("PROJECT_SOURCE_DATABASE_COUNT", project_result["source_database_count"]),
            ("PROJECT_DATABASE_MODE", "CONJOINED_CURRENT_CORE_AND_SECTORS_BY_SECTOR_ID"),
            ("PROJECT_WRITE_POINTER", ".uepc_project_write"),
            ("PACKAGE_USE_MODE", normalized_use_mode),
            ("MUTATION_INSTRUCTIONS_AUTHORIZED", str(not read_only_stress).lower()),
            ("ENV_MUTATION_ALLOWED", "false"),
            ("UOP_OVERRIDE_ALLOWED", "false"),
        ),
    )
    _write_pointer_file(
        stage / ".uepc_project_write",
        (
            ("PROJECT_WRITE_SQLITE", "PROJECT_CONJOINED_WRITE.sqlite"),
            ("WRITE_ROUTER_TABLE", "gemini_project_write_router"),
            ("WRITE_GRANT_TABLE", "gemini_mutation_grant"),
            ("WRITE_LOG_TABLE", "gemini_project_write_log"),
            ("PROJECT_HEAD_TABLE", "gemini_project_head"),
            ("CHAT_LINEAGE_NAMESPACE", "sector_chat_lineage__"),
            ("CHAT_LINEAGE_ACCESS", "READ_ONLY" if read_only_stress else "APPEND_ONLY_READ_WRITE"),
            ("RESEARCH_NAMESPACE", "sector_research__"),
            ("RESEARCH_ACCESS", "READ_ONLY" if read_only_stress else "APPEND_ONLY_READ_WRITE"),
            ("AUTOMATIC_APPEND_LANES", "NONE" if read_only_stress else "chat_lineage,research"),
            ("OTHER_SECTOR_NAMESPACE_PATTERN", "sector_<sector_id>__"),
            (
                "OTHER_SECTORS",
                "READ_ONLY_NO_MUTATION"
                if read_only_stress
                else "READ_ONLY_UNLESS_EXPLICIT_NAMED_ONE_TURN_GRANT",
            ),
            ("PACKAGE_USE_MODE", normalized_use_mode),
            ("MUTATION_INSTRUCTIONS_AUTHORIZED", str(not read_only_stress).lower()),
            ("STRESS_RESULT_READ_ONLY", str(read_only_stress).lower()),
            ("RELOCK_AFTER_COMMIT", "true"),
            ("SOURCE_DB_BLOB_TABLE", "embedded_database_blob"),
            ("SOURCE_DB_BLOB_CHUNK_TABLE", "embedded_database_blob_chunk"),
            ("CANONICAL_CONTENT_TABLE", "canonical_content_blob"),
            ("SOURCE_SCHEMA_REGISTRY", "source_schema_object_registry"),
            ("SOURCE_ROW_COUNT_REGISTRY", "source_row_count_registry"),
        ),
    )

    direct_hashes = {
        name: _sha256_file(stage / name)
        for name in (
            "ENV_PUBLIC_READONLY.sqlite",
            "UOP_PUBLIC_READONLY.sqlite",
            "PROJECT_CONJOINED_WRITE.sqlite",
            "ENV_MMD_RENDER.png",
            "UOP_MMD_RENDER.png",
        )
    }
    prompt_lines = [
        "T023-GEMINI-EXACT10-CURRENT-ENV15",
        "",
        flash_prompt.rstrip(),
        "",
        "STRUCTURAL AUTHORITY",
        "- This package has exactly ten flat root files and no nested ZIP.",
        "- The arrangement follows the approved Gemini package structure.",
        "- All content is rebuilt from current improved ENV15 truth; older reference content is not authority.",
        "- ENV and UOP SQLite files are immutable read-only authority.",
        "- PROJECT_CONJOINED_WRITE.sqlite fuses current project core and sectors by sector ID.",
        "- Large repeated code payloads use logical projection plus canonical content; exact project-file bytes remain reconstructible from code_exact_byte_chunk.",
        (
            "- This is a read-only stress result. The entire Project payload is query-only; no mutation or grant is authorized."
            if read_only_stress
            else "- Research and Chat Lineage are automatic append-only lanes; every other Project lane needs one named one-turn HIL grant."
        ),
        "",
        "READ ORDER",
        *[f"{index}. {name}" for index, name in enumerate(T021_GEMINI_EXACT10_NAMES, 1)],
        "",
        f"SOURCE_CHATGPT_PACKAGE_SHA256={source_hash_value}",
        *[f"{name}_SHA256={digest}" for name, digest in direct_hashes.items()],
        "",
    ]
    (stage / "GEMINI_FLASH_PROMPT.txt").write_text(
        "\n".join(prompt_lines),
        encoding="utf-8",
    )
    provider_readability = {
        "status": "PASS",
        "contract": "T023_GEMINI_EXACT10_STRUCTURE_V1",
        "prompt_byte_size": (stage / "GEMINI_FLASH_PROMPT.txt").stat().st_size,
        "project_payload": "PROJECT_CONJOINED_WRITE.sqlite",
        "opaque_corpus_appended": False,
    }

    observed = tuple(sorted(path.name for path in stage.iterdir() if path.is_file()))
    if observed != tuple(sorted(T021_GEMINI_EXACT10_NAMES)):
        raise GeminiExact10Error(f"EXACT10_STAGE_CONTENT_MISMATCH:{observed!r}")
    archive_variant = "_Read_Only_Stress_Result" if read_only_stress else ""
    archive_path = packages / f"Gemini_{_safe_name(brain_name)}{archive_variant}_Sqlite_brain.zip"
    _deterministic_zip(stage, archive_path)
    observed_package_bytes = archive_path.stat().st_size
    if observed_package_bytes > size_limit:
        retire_provider_outputs(packages, "GEMINI")
        skipped = write_provider_skip_marker(
            packages,
            "GEMINI",
            observed_bytes=observed_package_bytes,
            maximum_bytes=size_limit,
            reason="GEMINI_ARCHIVE_EXCEEDS_EXACT_100000000_BYTE_LIMIT",
        )
        return {
            **skipped,
            "gemini_package_zip": "",
            "package_folder": "",
            "sha256": "",
            "package_byte_size": observed_package_bytes,
            "package_use_mode": normalized_use_mode,
            "source_chatgpt_package_sha256": source_chatgpt_package_sha256,
            "source_chatgpt_validation": source_chatgpt_validation,
            "provider_readability": provider_readability,
            "retired_provider_outputs": retired_provider_outputs,
            "staging_removed": True,
        }
    package_byte_size = enforce_provider_package_size(
        archive_path,
        size_limit,
        "Gemini",
    )
    validation = validate_gemini_exact10(archive_path)
    if validation["status"] != "PASS":
        raise GeminiExact10Error(
            "GEMINI_EXACT10_VALIDATION_FAILED:" + ";".join(validation["errors"])
        )
    return {
        "gemini_package_zip": str(archive_path),
        "package_folder": str(stage),
        "sha256": _sha256_file(archive_path),
        "package_byte_size": package_byte_size,
        "provider_size_limit_bytes": size_limit,
        "package_use_mode": normalized_use_mode,
        "validation": validation,
        "source_chatgpt_package_sha256": source_chatgpt_package_sha256,
        "source_chatgpt_validation": source_chatgpt_validation,
        "env15_runtime": env_result,
        "uop_runtime": uop_result,
        "env_topology": env_png_result,
        "uop_topology": uop_png_result,
        "generated_project": project_result,
        "provider_readability": provider_readability,
        "retired_provider_outputs": retired_provider_outputs,
    }


__all__ = [
    "GeminiExact10Error",
    "build_conjoined_project_sqlite",
    "build_env15_public_runtime_sqlite",
    "export_gemini_exact10_direct",
]
