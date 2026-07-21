from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence


SNAPSHOT_SCHEMA_VERSION = "T023_SNAPSHOT_V002_CONTENT_ADDRESSED"
LEDGER_SCHEMA_VERSION = "T023_CODEX_LEDGER_V003_EVIDENCE_LANE_RQ"
PRIOR_LEDGER_SCHEMA_VERSION = "T023_CODEX_LEDGER_V002"
LEGACY_LEDGER_SCHEMA_VERSION = "T023_CODEX_LEDGER_V001"
DEFAULT_PARSER_VERSION = "evidenceos-parser-v001"
DEFAULT_CHUNKER_VERSION = "evidenceos-chunker-v001"

CLAIM_STATES = {
    "SOURCE_VERIFIED",
    "TEST_VERIFIED",
    "HUMAN_ACCEPTED",
    "CANDIDATE",
    "OPEN",
    "BLOCKED",
    "STALE",
    "SUPERSEDED",
    "UNVERIFIED",
}

USAGE_CLASSIFICATIONS = {
    "EXACT_PROVIDER_USAGE",
    "AGGREGATE_ACCOUNT_ONLY",
    "ESTIMATED_VISIBLE_INPUT",
    "UNAVAILABLE",
    "UNAVAILABLE_AUTHORITATIVE_USAGE",
}

MODEL_FAILURE_CLASSES = {
    "PACKAGE_TOO_LARGE",
    "PACKAGE_CORRUPTED",
    "HASH_MISMATCH",
    "MANIFEST_MISSING",
    "SCHEMA_UNSUPPORTED",
    "SNAPSHOT_MISMATCH",
    "MODEL_LOAD_FAILED",
    "ENDPOINT_UNREACHABLE",
    "AUTHENTICATION_MISSING",
    "AUTHENTICATION_REJECTED",
    "PAYLOAD_REJECTED",
    "REQUEST_TIMEOUT",
    "MODEL_RESPONSE_INVALID",
    "DELTA_CONFLICT",
    "TEST_FAILED",
    "BUILD_FAILED",
    "PATCH_FAILED",
    "MUTATION_DETECTED",
    "ROLLBACK_FAILED",
    "UNKNOWN_FAILURE",
}

RQ_QUESTIONS = {
    "RQ-PRIMARY": (
        "How can a local-first pre-AI project-state compiler transform a heterogeneous software project "
        "into a verified, task-scoped, provider-readable execution package before model inference, while "
        "keeping canonical authority outside the provider and avoiding unnecessary rereading of unchanged state?"
    ),
    "RQ-S01": "How is an authorization bound to the exact sector, actor, reason, and turn?",
    "RQ-S02": "How can failure restore the exact prior bytes and mandatory read-only state?",
    "RQ-S03": "How can provider-specific limits yield either a complete readable package or a canonical non-breaking skip?",
    "RQ-S04": "How should returned candidates later be bound to their outbound authority receipt before human promotion?",
    "RQ-S05": "What controlled benchmarks prove rescan reduction, restoration accuracy, stale-grant prevention, readability, and recovery time?",
}

RQ_BASELINE_ANSWERS = {
    "RQ-PRIMARY": {
        "answer": (
            "Implemented for the frozen snapshot: exact-byte reconstruction, hashing, provider-readable projection, "
            "and local canonical authority are verified. Comparative efficiency remains an open benchmark question."
        ),
        "claim_status": "TEST_VERIFIED",
    },
    "RQ-S01": {
        "answer": "Implemented and tested in the selected snapshot with a sector-, actor-, reason-, and turn-bound one-use grant.",
        "claim_status": "TEST_VERIFIED",
    },
    "RQ-S02": {
        "answer": "Implemented and tested in the selected snapshot with exact preimage capture, rollback, exact restore, relock, and grant revocation.",
        "claim_status": "TEST_VERIFIED",
    },
    "RQ-S03": {
        "answer": "Implemented and tested: produce a complete validated provider-readable package or a canonical non-breaking skip, never a misleading partial package.",
        "claim_status": "TEST_VERIFIED",
    },
    "RQ-S04": {
        "answer": "Not proven. Outbound provider projection and inbound returned-candidate reverse receipts remain separate mechanisms.",
        "claim_status": "OPEN",
    },
    "RQ-S05": {
        "answer": "Not proven. Faster execution, reduced rescanning, lower token use, fewer defects, and recovery-time gains require controlled benchmarks.",
        "claim_status": "OPEN",
    },
}

RQ_LEDGER_SIDECAR_SCHEMA = "EVIDENCE_LANE_RQ_LEDGER_V002"
STEER_PROMPT_LOG_SCHEMA = "EVIDENCE_LANE_STEER_PROMPT_ANSWER_LOG_V001"

APPEND_ONLY_TABLES = (
    "goal_delta_ledger",
    "goal_run",
    "task_run",
    "task_step",
    "model_call_usage",
    "model_execution_run",
    "endpoint_execution_event",
    "model_failure_event",
    "project_query_log",
    "reasoning_summary",
    "source_evidence_used",
    "file_change",
    "symbol_change",
    "route_change",
    "dependency_change",
    "test_build_result",
    "package_result",
    "hil_gate",
    "acceptance_receipt",
    "rollback_pointer",
    "next_state_pointer",
    "brain_snapshot_relation",
    "rq_ledger",
    "steer_prompt_answer",
)

_SECRET_COLUMN = re.compile(r"(?:password|passwd|secret|credential|private[_-]?key|access[_-]?token|api[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"^(?:sk-[A-Za-z0-9_-]{16,}|gh[opusr]_[A-Za-z0-9]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{20,})")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _format_elapsed_seconds(value: float | int) -> str:
    seconds = max(0, int(round(float(value))))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _usage_elapsed_seconds(record: Mapping[str, Any]) -> float | None:
    explicit = record.get("elapsed_seconds")
    if explicit is not None:
        return max(0.0, float(explicit))
    started = str(record.get("call_started_at") or "").strip()
    completed = str(record.get("call_completed_at") or "").strip()
    if not started or not completed:
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def format_goal_usage_summary(
    total_tokens: int | None,
    elapsed_seconds: float | int | None,
    *,
    token_exact: bool,
) -> str:
    token_text = f"{int(total_tokens):,} tokens" if token_exact and total_tokens is not None else "unavailable"
    elapsed_text = "" if elapsed_seconds is None else f" over {_format_elapsed_seconds(elapsed_seconds)}"
    return f"Goal usage: {token_text}{elapsed_text}."


def _rq_sidecar_payload(
    answers: Mapping[str, str] | None = None,
    *,
    task_run_id: int | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_answers = answers or {
        question_id: str(baseline["answer"])
        for question_id, baseline in RQ_BASELINE_ANSWERS.items()
    }
    return {
        "schema": RQ_LEDGER_SIDECAR_SCHEMA,
        "product_name": "Evidence Lane",
        "supersedes_question_set": "T023_RQ_QUESTION_SET_V001",
        "questions": {
            question_id: {
                "question": question,
                "answer": resolved_answers[question_id],
                "claim_status": (
                    "CANDIDATE"
                    if answers is not None
                    else str(RQ_BASELINE_ANSWERS[question_id]["claim_status"])
                ),
                "task_run_id": task_run_id,
                "evidence": (evidence or {}).get(question_id, []),
            }
            for question_id, question in RQ_QUESTIONS.items()
        },
    }


def _write_current_rq_sidecar(
    ledger_path: Path,
    answers: Mapping[str, str] | None = None,
    *,
    task_run_id: int | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    if ledger_path.parent.name.casefold() != "codex":
        return
    _write_json(
        ledger_path.parent / "RQ_LEDGER.json",
        _rq_sidecar_payload(answers, task_run_id=task_run_id, evidence=evidence),
    )


def _connect_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _make_writable(path: Path) -> None:
    if path.exists():
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)


def _safe_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sanitize_value(column: str, value: Any) -> Any:
    if _SECRET_COLUMN.search(column):
        return {"redacted": True, "reason": "SECRET_FIELD_EXCLUDED"}
    if isinstance(value, bytes):
        return {
            "binary": True,
            "byte_size": len(value),
            "sha256": _sha256_bytes(value),
            "payload_excluded": True,
        }
    if isinstance(value, str) and _SECRET_VALUE.match(value.strip()):
        return {"redacted": True, "reason": "SECRET_VALUE_EXCLUDED"}
    return value


def _text_projection(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (int, float)):
        return "" if value is None else str(value)
    return ""


@dataclass(frozen=True)
class SourceDatabase:
    path: Path
    relative_path: str
    byte_size: int
    mtime_ns: int
    sha256: str | None = None

    @property
    def source_id(self) -> str:
        return "sqlite:" + hashlib.sha256(self.relative_path.encode("utf-8")).hexdigest()[:24]


def discover_source_databases(brain_root: str | Path, excluded_roots: Iterable[str | Path] = ()) -> list[SourceDatabase]:
    root = Path(brain_root).resolve()
    excluded = [Path(item).resolve() for item in excluded_roots]
    databases: list[SourceDatabase] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.suffix.casefold() not in {".sqlite", ".sqlite3", ".db"}:
            continue
        resolved = path.resolve()
        if any(resolved == item or item in resolved.parents for item in excluded):
            continue
        relative = path.relative_to(root).as_posix()
        if any(part.casefold() in {"packages", "brain_snapshots", "generated", "dist"} for part in path.relative_to(root).parts):
            continue
        info = path.stat()
        databases.append(SourceDatabase(path, relative, info.st_size, info.st_mtime_ns))
    return databases


def _existing_source_stats(snapshot: Path) -> dict[str, tuple[int, int, str, str, str]]:
    if not snapshot.exists():
        return {}
    try:
        with closing(_connect_ro(snapshot)) as connection:
            rows = connection.execute(
                "SELECT relative_path, byte_size, mtime_ns, sha256, parser_version, chunker_version FROM source_database"
            )
            return {
                row["relative_path"]: (
                    int(row["byte_size"]),
                    int(row["mtime_ns"]),
                    str(row["sha256"]),
                    str(row["parser_version"]),
                    str(row["chunker_version"]),
                )
                for row in rows
            }
    except (sqlite3.Error, OSError):
        return {}


def _snapshot_reusable(
    snapshot: Path,
    sources: Sequence[SourceDatabase],
    parser_version: str,
    chunker_version: str,
) -> bool:
    if not snapshot.exists():
        return False
    try:
        with closing(_connect_ro(snapshot)) as connection:
            row = connection.execute("SELECT schema_version FROM snapshot_metadata LIMIT 1").fetchone()
            if row is None or str(row["schema_version"]) != SNAPSHOT_SCHEMA_VERSION:
                return False
    except (sqlite3.Error, OSError):
        return False
    previous = _existing_source_stats(snapshot)
    if set(previous) != {source.relative_path for source in sources}:
        return False
    return all(
        previous[source.relative_path][0] == source.byte_size
        and previous[source.relative_path][1] == source.mtime_ns
        and previous[source.relative_path][3] == parser_version
        and previous[source.relative_path][4] == chunker_version
        for source in sources
    )


def _snapshot_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=DELETE;
        CREATE TABLE snapshot_metadata(
          snapshot_id TEXT PRIMARY KEY,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          claim_status TEXT NOT NULL CHECK(claim_status IN ('SOURCE_VERIFIED','TEST_VERIFIED','HUMAN_ACCEPTED','CANDIDATE','OPEN','BLOCKED','STALE','SUPERSEDED','UNVERIFIED')),
          input_set_hash TEXT NOT NULL UNIQUE,
          statement TEXT NOT NULL
        );
        CREATE TABLE source_database(
          source_id TEXT PRIMARY KEY,
          relative_path TEXT NOT NULL UNIQUE,
          byte_size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          parser_version TEXT NOT NULL,
          chunker_version TEXT NOT NULL,
          integrity_check TEXT NOT NULL,
          foreign_key_violation_count INTEGER NOT NULL
        );
        CREATE TABLE source_schema_object(
          source_id TEXT NOT NULL REFERENCES source_database(source_id),
          object_type TEXT NOT NULL,
          object_name TEXT NOT NULL,
          table_name TEXT NOT NULL,
          sql_text TEXT,
          PRIMARY KEY(source_id, object_type, object_name)
        );
        CREATE TABLE canonical_row_content(
          content_id INTEGER PRIMARY KEY,
          row_hash TEXT NOT NULL UNIQUE,
          content TEXT NOT NULL,
          claim_status TEXT NOT NULL DEFAULT 'SOURCE_VERIFIED'
        );
        CREATE TABLE source_table_row_ref(
          source_id TEXT NOT NULL REFERENCES source_database(source_id),
          table_name TEXT NOT NULL,
          row_hash TEXT NOT NULL REFERENCES canonical_row_content(row_hash),
          PRIMARY KEY(source_id, table_name, row_hash)
        );
        CREATE VIEW source_table_row AS
        SELECT r.source_id,
               r.table_name,
               r.row_hash,
               c.content AS row_json,
               c.claim_status
          FROM source_table_row_ref r
          JOIN canonical_row_content c USING(row_hash);
        CREATE VIRTUAL TABLE snapshot_fts USING fts5(
          row_hash UNINDEXED,
          content,
          content='canonical_row_content',
          content_rowid='content_id',
          tokenize='unicode61'
        );
        CREATE TABLE source_query_contract(
          question_id TEXT PRIMARY KEY,
          question TEXT NOT NULL,
          query_hint TEXT NOT NULL
        );
        CREATE INDEX source_table_row_ref_lookup ON source_table_row_ref(source_id, table_name);
        CREATE INDEX source_table_row_ref_hash_lookup ON source_table_row_ref(row_hash);
        CREATE TRIGGER source_table_row_read_only_insert
        INSTEAD OF INSERT ON source_table_row
        BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SNAPSHOT_VIEW'); END;
        CREATE TRIGGER source_table_row_read_only_update
        INSTEAD OF UPDATE ON source_table_row
        BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SNAPSHOT_VIEW'); END;
        CREATE TRIGGER source_table_row_read_only_delete
        INSTEAD OF DELETE ON source_table_row
        BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SNAPSHOT_VIEW'); END;
        """
    )


def _source_tables(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = list(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','view','index','trigger') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )
    fts_roots = {
        str(row["name"])
        for row in rows
        if row["type"] == "table" and "VIRTUAL TABLE" in str(row["sql"] or "").upper() and "FTS" in str(row["sql"] or "").upper()
    }
    shadow_names = {
        root + suffix
        for root in fts_roots
        for suffix in ("_data", "_idx", "_content", "_docsize", "_config")
    }
    return [row for row in rows if str(row["name"]) not in shadow_names]


def _read_source_into_snapshot(
    output: sqlite3.Connection,
    source: SourceDatabase,
    parser_version: str,
    chunker_version: str,
) -> dict[str, int]:
    reference_count = 0
    canonical_count = 0
    canonical_reuse_count = 0
    schema_count = 0
    with closing(_connect_ro(source.path)) as input_db:
        integrity = str(input_db.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(input_db.execute("PRAGMA foreign_key_check"))
        if integrity.casefold() != "ok" or foreign_keys:
            raise RuntimeError(f"SOURCE_SQLITE_INVALID:{source.relative_path}")
        output.execute(
            "INSERT INTO source_database VALUES(?,?,?,?,?,?,?,?,?)",
            (
                source.source_id,
                source.relative_path,
                source.byte_size,
                source.mtime_ns,
                source.sha256,
                parser_version,
                chunker_version,
                integrity,
                len(foreign_keys),
            ),
        )
        objects = _source_tables(input_db)
        for item in objects:
            output.execute(
                "INSERT INTO source_schema_object VALUES(?,?,?,?,?)",
                (source.source_id, item["type"], item["name"], item["tbl_name"], item["sql"]),
            )
            schema_count += 1
        for item in objects:
            if item["type"] != "table":
                continue
            object_sql = str(item["sql"] or "").upper()
            if "VIRTUAL TABLE" in object_sql and "FTS" in object_sql:
                # Source FTS rows are a derived search projection. The portable
                # snapshot records the schema but indexes canonical source rows
                # once in its own external-content FTS instead of copying the
                # source FTS payload into another row store.
                continue
            table = str(item["name"])
            try:
                cursor = input_db.execute(f"SELECT * FROM {_safe_identifier(table)}")
            except sqlite3.Error:
                continue
            columns = [str(column[0]) for column in cursor.description or ()]
            staged: list[tuple[str, str]] = []
            for values in cursor:
                record = {
                    column: _sanitize_value(column, values[index])
                    for index, column in enumerate(columns)
                }
                payload = _json(record)
                row_hash = _sha256_bytes(payload.encode("utf-8"))
                staged.append((row_hash, payload))
            for row_hash, payload in sorted(staged, key=lambda row: row[0]):
                inserted = output.execute(
                    "INSERT OR IGNORE INTO canonical_row_content(row_hash,content) VALUES(?,?)",
                    (row_hash, payload),
                ).rowcount
                if inserted:
                    canonical_count += 1
                else:
                    canonical_reuse_count += 1
                reference_count += output.execute(
                    "INSERT OR IGNORE INTO source_table_row_ref(source_id,table_name,row_hash) VALUES(?,?,?)",
                    (source.source_id, table, row_hash),
                ).rowcount
    return {
        "row_references": reference_count,
        "canonical_rows": canonical_count,
        "canonical_reuses": canonical_reuse_count,
        "schema_objects": schema_count,
    }


def create_immutable_snapshot(
    brain_root: str | Path,
    output_path: str | Path,
    *,
    parser_version: str = DEFAULT_PARSER_VERSION,
    chunker_version: str = DEFAULT_CHUNKER_VERSION,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = discover_source_databases(root, [output.parent])
    if not sources:
        raise RuntimeError("NO_SOURCE_SQLITE_DATABASES")
    if _snapshot_reusable(output, sources, parser_version, chunker_version):
        return {
            "snapshot": str(output),
            "snapshot_sha256": _sha256_file(output),
            "snapshot_reused": True,
            "new_immutable_snapshot": 0,
            "raw_databases_opened": 0,
            "unchanged_sources_skipped": len(sources),
            "new_source_rows": 0,
            "new_file_version_rows": 0,
            "new_chunks": 0,
            "new_fts_rows": 0,
            "source_row_reference_count": 0,
            "canonical_row_count": 0,
            "canonical_row_reuse_count": 0,
            "physical_row_json_copies_per_hash": 1,
            "fts_content_storage": "EXTERNAL_CANONICAL_CONTENT",
        }

    hashed = [
        SourceDatabase(item.path, item.relative_path, item.byte_size, item.mtime_ns, _sha256_file(item.path))
        for item in sources
    ]
    input_identity = _sha256_bytes(
        ("\n".join(
            f"{item.relative_path}|{item.byte_size}|{item.sha256}|{parser_version}|{chunker_version}"
            for item in hashed
        ) + "\n").encode("utf-8")
    )
    temporary = output.with_name(output.name + ".candidate")
    _make_writable(output)
    if temporary.exists():
        _make_writable(temporary)
        temporary.unlink()
    totals = {
        "row_references": 0,
        "canonical_rows": 0,
        "canonical_reuses": 0,
        "schema_objects": 0,
    }
    connection = sqlite3.connect(temporary)
    try:
        _snapshot_schema(connection)
        for item in hashed:
            counts = _read_source_into_snapshot(connection, item, parser_version, chunker_version)
            for key, value in counts.items():
                totals[key] += value
        connection.execute("INSERT INTO snapshot_fts(snapshot_fts) VALUES('rebuild')")
        indexed_row_count = int(connection.execute("SELECT COUNT(*) FROM snapshot_fts").fetchone()[0])
        canonical_row_count = int(connection.execute("SELECT COUNT(*) FROM canonical_row_content").fetchone()[0])
        source_row_reference_count = int(connection.execute("SELECT COUNT(*) FROM source_table_row_ref").fetchone()[0])
        if indexed_row_count != canonical_row_count:
            raise RuntimeError("SNAPSHOT_CANONICAL_FTS_PARITY_FAILED")
        statement = "THE BRAIN IS A VERSIONED, SOURCE-LINKED PROJECT SNAPSHOT."
        connection.execute(
            "INSERT INTO snapshot_metadata VALUES(?,?,?,?,?,?)",
            ("snapshot:" + input_identity, SNAPSHOT_SCHEMA_VERSION, _utc_now(), "CANDIDATE", input_identity, statement),
        )
        query_hints = {
            "project": (
                "SELECT r.table_name,f.content AS row_json FROM snapshot_fts f "
                "JOIN source_table_row_ref r USING(row_hash) WHERE snapshot_fts MATCH ?"
            ),
            "latest_state": (
                "SELECT r.source_id,r.table_name,f.row_hash,f.content FROM snapshot_fts f "
                "JOIN source_table_row_ref r USING(row_hash) WHERE snapshot_fts MATCH ?"
            ),
            "changes": "SELECT table_name,row_json FROM source_table_row WHERE table_name LIKE '%change%'",
            "open_items": "SELECT table_name,row_json FROM source_table_row WHERE row_json LIKE '%OPEN%'",
        }
        for question_id, (question, hint) in enumerate(
            (
                ("What is this project?", query_hints["project"]),
                ("What is the latest verified state?", query_hints["latest_state"]),
                ("What changed and why?", query_hints["changes"]),
                ("What remains open or blocked?", query_hints["open_items"]),
            ),
            start=1,
        ):
            connection.execute("INSERT INTO source_query_contract VALUES(?,?,?)", (f"PUBLIC-Q{question_id:02d}", question, hint))
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SNAPSHOT_INTEGRITY_FAILED")
        if list(connection.execute("PRAGMA foreign_key_check")):
            raise RuntimeError("SNAPSHOT_FOREIGN_KEY_FAILED")
    finally:
        connection.close()
    os.replace(temporary, output)
    _make_read_only(output)
    return {
        "snapshot": str(output),
        "snapshot_sha256": _sha256_file(output),
        "snapshot_reused": False,
        "new_immutable_snapshot": 1,
        "raw_databases_opened": len(hashed),
        "unchanged_sources_skipped": 0,
        "new_source_rows": len(hashed),
        "new_file_version_rows": 0,
        "new_chunks": source_row_reference_count,
        "new_fts_rows": indexed_row_count,
        "source_row_reference_count": source_row_reference_count,
        "canonical_row_count": canonical_row_count,
        "canonical_row_reuse_count": totals["canonical_reuses"],
        "physical_row_json_copies_per_hash": 1,
        "fts_content_storage": "EXTERNAL_CANONICAL_CONTENT",
        "schema_objects": totals["schema_objects"],
        "input_set_hash": input_identity,
    }


def query_immutable_snapshot(
    snapshot_path: str | Path,
    query: str,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    """Query the immutable snapshot without reopening any raw source database."""
    snapshot = Path(snapshot_path).resolve()
    text = str(query or "").strip()
    if not text:
        raise ValueError("SNAPSHOT_QUERY_REQUIRED")
    bounded_limit = max(1, min(int(limit), 200))
    with closing(_connect_ro(snapshot)) as connection:
        identity = connection.execute(
            "SELECT snapshot_id,input_set_hash,claim_status FROM snapshot_metadata"
        ).fetchone()
        rows = list(
            connection.execute(
                "SELECT r.source_id,r.table_name,f.row_hash,f.content "
                "FROM snapshot_fts f JOIN source_table_row_ref r USING(row_hash) "
                "WHERE snapshot_fts MATCH ? "
                "ORDER BY r.source_id,r.table_name,f.row_hash LIMIT ?",
                (text, bounded_limit),
            )
        )
    return {
        "snapshot_id": str(identity["snapshot_id"]),
        "snapshot_hash": _sha256_file(snapshot),
        "input_set_hash": str(identity["input_set_hash"]),
        "claim_status": str(identity["claim_status"]),
        "query": text,
        "limit": bounded_limit,
        "tables_queried": ["snapshot_fts"],
        "result_count": len(rows),
        "raw_source_databases_opened": 0,
        "results": [dict(row) for row in rows],
    }


def _model_ledger_v002_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_execution_run(
          id INTEGER PRIMARY KEY,
          execution_id TEXT NOT NULL UNIQUE,
          task_run_id INTEGER REFERENCES task_run(id),
          brain_id TEXT NOT NULL,
          brain_snapshot_hash TEXT NOT NULL,
          connector_id TEXT NOT NULL,
          endpoint_class TEXT NOT NULL,
          model_id TEXT NOT NULL,
          user_request TEXT NOT NULL,
          user_request_sha256 TEXT NOT NULL,
          evidence_slice_ids_json TEXT NOT NULL DEFAULT '[]',
          sql_queries_json TEXT NOT NULL DEFAULT '[]',
          fts_queries_json TEXT NOT NULL DEFAULT '[]',
          request_timestamp TEXT NOT NULL,
          response_timestamp TEXT,
          provider_response_id TEXT,
          response_text TEXT,
          visible_reasoning_summary TEXT,
          proposed_files_json TEXT NOT NULL DEFAULT '[]',
          patch_artifact_ref TEXT,
          test_status TEXT,
          human_decision TEXT,
          resulting_delta_id TEXT,
          status TEXT NOT NULL,
          prior_row_id INTEGER REFERENCES model_execution_run(id),
          recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS endpoint_execution_event(
          id INTEGER PRIMARY KEY,
          execution_run_id INTEGER NOT NULL REFERENCES model_execution_run(id),
          event_type TEXT NOT NULL,
          status TEXT NOT NULL,
          provider_response_id TEXT,
          usage_classification TEXT NOT NULL,
          usage_json TEXT NOT NULL DEFAULT '{}',
          response_text TEXT,
          response_artifact_ref TEXT,
          error_code TEXT,
          exact_error TEXT,
          prior_row_id INTEGER REFERENCES endpoint_execution_event(id),
          recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_failure_event(
          id INTEGER PRIMARY KEY,
          failure_id TEXT NOT NULL UNIQUE,
          execution_run_id INTEGER REFERENCES model_execution_run(id),
          task_id TEXT NOT NULL,
          model_or_endpoint TEXT NOT NULL,
          package_id TEXT,
          lane_id TEXT,
          stage TEXT NOT NULL,
          failure_class TEXT NOT NULL,
          observed_evidence_json TEXT NOT NULL DEFAULT '{}',
          exact_error TEXT NOT NULL,
          inferred_cause TEXT,
          inferred_cause_labeled INTEGER NOT NULL CHECK(inferred_cause_labeled IN (0,1)),
          retry_safe INTEGER NOT NULL CHECK(retry_safe IN (0,1)),
          data_preserved INTEGER NOT NULL CHECK(data_preserved IN (0,1)),
          rollback_available INTEGER NOT NULL CHECK(rollback_available IN (0,1)),
          next_action_code TEXT NOT NULL,
          next_action_text TEXT NOT NULL,
          unresolved_risk TEXT NOT NULL,
          failure_timestamp TEXT NOT NULL,
          prior_row_id INTEGER REFERENCES model_failure_event(id),
          recorded_at TEXT NOT NULL
        );
        """
    )


def _evidence_lane_ledger_v003_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS steer_prompt_answer(
          id INTEGER PRIMARY KEY,
          task_run_id INTEGER NOT NULL REFERENCES task_run(id),
          steer_id TEXT NOT NULL,
          prompt_id TEXT NOT NULL,
          prompt_text TEXT NOT NULL,
          answer_text TEXT NOT NULL,
          answer_status TEXT NOT NULL,
          usage_classification TEXT NOT NULL,
          goal_usage_summary TEXT NOT NULL,
          total_tokens INTEGER,
          elapsed_seconds REAL,
          prior_row_id INTEGER REFERENCES steer_prompt_answer(id),
          recorded_at TEXT NOT NULL
        );
        """
    )


def _seed_evidence_lane_rq_baseline(connection: sqlite3.Connection) -> int:
    inserted = 0
    for question_id, question in RQ_QUESTIONS.items():
        existing = connection.execute(
            "SELECT 1 FROM rq_ledger WHERE task_run_id IS NULL AND question_id=? LIMIT 1",
            (question_id,),
        ).fetchone()
        if existing:
            continue
        baseline = RQ_BASELINE_ANSWERS[question_id]
        connection.execute(
            "INSERT INTO rq_ledger(task_run_id,question_id,question,answer,claim_status,evidence_json,prior_row_id,recorded_at) "
            "VALUES(NULL,?,?,?,?,?,?,?)",
            (
                question_id,
                question,
                str(baseline["answer"]),
                str(baseline["claim_status"]),
                _json([
                    {
                        "authority": "EVIDENCE_LANE_PATENT_DEFENSE_SNAPSHOT_R1",
                        "scope": "frozen selected snapshot",
                    }
                ]),
                None,
                _utc_now(),
            ),
        )
        inserted += 1
    return inserted


def _create_append_only_triggers(connection: sqlite3.Connection, tables: Iterable[str]) -> None:
    for table in tables:
        quoted = _safe_identifier(table)
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {_safe_identifier(table + '_no_update')} "
            f"BEFORE UPDATE ON {quoted} BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY_UPDATE_FORBIDDEN'); END"
        )
        connection.execute(
            f"CREATE TRIGGER IF NOT EXISTS {_safe_identifier(table + '_no_delete')} "
            f"BEFORE DELETE ON {quoted} BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY_DELETE_FORBIDDEN'); END"
        )


def _ledger_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE ledger_metadata(schema_version TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE goal_delta_ledger(id INTEGER PRIMARY KEY, delta_id TEXT NOT NULL, parent_goal_id TEXT NOT NULL, run_id TEXT NOT NULL, task_pointer TEXT NOT NULL, title TEXT NOT NULL, prompt_pointer TEXT NOT NULL, insertion_reason TEXT NOT NULL, insertion_point TEXT NOT NULL, status TEXT NOT NULL, existing_requirements_preserved INTEGER NOT NULL CHECK(existing_requirements_preserved=1), current_task_replaced INTEGER NOT NULL CHECK(current_task_replaced=0), current_goal_restarted INTEGER NOT NULL CHECK(current_goal_restarted=0), prior_row_id INTEGER REFERENCES goal_delta_ledger(id), recorded_at TEXT NOT NULL);
        CREATE TABLE goal_run(id INTEGER PRIMARY KEY, goal_id TEXT NOT NULL, run_id TEXT NOT NULL, model TEXT, reasoning_mode TEXT, started_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, prior_row_id INTEGER REFERENCES goal_run(id), data_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE task_run(id INTEGER PRIMARY KEY, parent_goal_id TEXT NOT NULL, run_id TEXT NOT NULL, task_id TEXT NOT NULL, step_id TEXT NOT NULL, delta_ids_active TEXT NOT NULL, model TEXT, reasoning_mode TEXT, start_timestamp TEXT NOT NULL, end_timestamp TEXT, elapsed_seconds REAL, starting_snapshot_hash TEXT NOT NULL, ending_candidate_hash TEXT, sources_queried TEXT NOT NULL DEFAULT '[]', sqlite_tables_queried TEXT NOT NULL DEFAULT '[]', fts_queries_issued TEXT NOT NULL DEFAULT '[]', raw_files_reread TEXT NOT NULL DEFAULT '[]', unchanged_files_skipped TEXT NOT NULL DEFAULT '[]', files_changed TEXT NOT NULL DEFAULT '[]', tests_run TEXT NOT NULL DEFAULT '[]', test_results TEXT NOT NULL DEFAULT '[]', build_status TEXT NOT NULL DEFAULT 'NOT_RUN', human_gate_status TEXT NOT NULL DEFAULT 'PENDING', completion_status TEXT NOT NULL, next_exact_pointer TEXT NOT NULL, prior_row_id INTEGER REFERENCES task_run(id));
        CREATE TABLE task_step(id INTEGER PRIMARY KEY, task_run_id INTEGER NOT NULL REFERENCES task_run(id), step_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT, data_json TEXT NOT NULL DEFAULT '{}', prior_row_id INTEGER REFERENCES task_step(id));
        CREATE TABLE model_call_usage(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), goal_id TEXT NOT NULL, run_id TEXT NOT NULL, task_id TEXT NOT NULL, classification TEXT NOT NULL, token_exact INTEGER NOT NULL, usage_source TEXT NOT NULL, provider TEXT, model TEXT, provider_response_id TEXT, input_tokens INTEGER, cached_input_tokens INTEGER, uncached_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER, call_started_at TEXT, call_completed_at TEXT, elapsed_seconds REAL, quota_checkpoint_json TEXT, prior_row_id INTEGER REFERENCES model_call_usage(id), recorded_at TEXT NOT NULL);
        CREATE TABLE project_query_log(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), snapshot_hash TEXT NOT NULL, query_type TEXT NOT NULL, query_text TEXT NOT NULL, tables_queried TEXT NOT NULL, result_count INTEGER NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE reasoning_summary(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), summary TEXT NOT NULL, claim_status TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE source_evidence_used(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), source_pointer TEXT NOT NULL, source_hash TEXT, table_name TEXT, row_pointer TEXT, purpose TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE file_change(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), path TEXT NOT NULL, before_hash TEXT, after_hash TEXT, change_type TEXT NOT NULL, reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE symbol_change(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), symbol TEXT NOT NULL, file_path TEXT NOT NULL, change_type TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL);
        CREATE TABLE route_change(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), route TEXT NOT NULL, change_type TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL);
        CREATE TABLE dependency_change(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), dependency TEXT NOT NULL, change_type TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL);
        CREATE TABLE test_build_result(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), kind TEXT NOT NULL, command TEXT NOT NULL, status TEXT NOT NULL, elapsed_seconds REAL, evidence_pointer TEXT, recorded_at TEXT NOT NULL);
        CREATE TABLE package_result(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), package_path TEXT NOT NULL, package_hash TEXT, validation_status TEXT NOT NULL, member_count INTEGER, receipt_pointer TEXT, recorded_at TEXT NOT NULL);
        CREATE TABLE hil_gate(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), gate_id TEXT NOT NULL, status TEXT NOT NULL, decision TEXT, decided_by TEXT, decided_at TEXT, evidence_pointer TEXT, recorded_at TEXT NOT NULL);
        CREATE TABLE acceptance_receipt(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), receipt_id TEXT NOT NULL, status TEXT NOT NULL, receipt_json TEXT NOT NULL, prior_row_id INTEGER REFERENCES acceptance_receipt(id), recorded_at TEXT NOT NULL);
        CREATE TABLE rollback_pointer(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), snapshot_hash TEXT NOT NULL, pointer TEXT NOT NULL, reason TEXT NOT NULL, prior_row_id INTEGER REFERENCES rollback_pointer(id), recorded_at TEXT NOT NULL);
        CREATE TABLE next_state_pointer(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), pointer TEXT NOT NULL, status TEXT NOT NULL, prior_row_id INTEGER REFERENCES next_state_pointer(id), recorded_at TEXT NOT NULL);
        CREATE TABLE brain_snapshot_relation(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), prior_snapshot_hash TEXT, candidate_snapshot_hash TEXT, relation TEXT NOT NULL, promotion_status TEXT NOT NULL, recorded_at TEXT NOT NULL);
        CREATE TABLE rq_ledger(id INTEGER PRIMARY KEY, task_run_id INTEGER REFERENCES task_run(id), question_id TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, claim_status TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]', prior_row_id INTEGER REFERENCES rq_ledger(id), recorded_at TEXT NOT NULL);
        """
    )
    _model_ledger_v002_schema(connection)
    _evidence_lane_ledger_v003_schema(connection)
    _seed_evidence_lane_rq_baseline(connection)
    connection.execute("INSERT INTO ledger_metadata VALUES(?,?)", (LEDGER_SCHEMA_VERSION, _utc_now()))
    _create_append_only_triggers(connection, APPEND_ONLY_TABLES)


def _migrate_ledger_v001_to_v002(connection: sqlite3.Connection) -> str:
    old_tables = tuple(table for table in APPEND_ONLY_TABLES if table not in {
        "model_execution_run", "endpoint_execution_event", "model_failure_event", "steer_prompt_answer"
    })
    prior_counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {_safe_identifier(table)}").fetchone()[0])
        for table in old_tables
    }
    _model_ledger_v002_schema(connection)
    _create_append_only_triggers(
        connection,
        ("model_execution_run", "endpoint_execution_event", "model_failure_event"),
    )
    stamp = _utc_now()
    receipt_id = f"LEDGER_MIGRATION_V001_TO_V002_{stamp.replace(':', '').replace('-', '')}"
    connection.execute(
        "INSERT INTO acceptance_receipt(task_run_id,receipt_id,status,receipt_json,prior_row_id,recorded_at) "
        "VALUES(NULL,?,?,?,?,?)",
        (
            receipt_id,
            "PASS_PRESERVED_PRIOR_ROWS",
            _json(
                {
                    "from": LEGACY_LEDGER_SCHEMA_VERSION,
                    "to": PRIOR_LEDGER_SCHEMA_VERSION,
                    "prior_row_counts": prior_counts,
                    "new_tables": [
                        "model_execution_run",
                        "endpoint_execution_event",
                        "model_failure_event",
                    ],
                }
            ),
            None,
            stamp,
        ),
    )
    connection.execute(
        "UPDATE ledger_metadata SET schema_version=? WHERE schema_version=?",
        (PRIOR_LEDGER_SCHEMA_VERSION, LEGACY_LEDGER_SCHEMA_VERSION),
    )
    return receipt_id


def _migrate_ledger_v002_to_v003(connection: sqlite3.Connection) -> str:
    prior_rq_rows = int(connection.execute("SELECT COUNT(*) FROM rq_ledger").fetchone()[0])
    _evidence_lane_ledger_v003_schema(connection)
    seeded_rows = _seed_evidence_lane_rq_baseline(connection)
    _create_append_only_triggers(connection, ("steer_prompt_answer",))
    stamp = _utc_now()
    receipt_id = f"LEDGER_MIGRATION_V002_TO_V003_{stamp.replace(':', '').replace('-', '')}"
    connection.execute(
        "INSERT INTO acceptance_receipt(task_run_id,receipt_id,status,receipt_json,prior_row_id,recorded_at) "
        "VALUES(NULL,?,?,?,?,?)",
        (
            receipt_id,
            "PASS_PRESERVED_PRIOR_ROWS_AND_SUPERSEDED_RQ_CATALOG",
            _json(
                {
                    "from": PRIOR_LEDGER_SCHEMA_VERSION,
                    "to": LEDGER_SCHEMA_VERSION,
                    "prior_rq_rows_preserved": prior_rq_rows,
                    "new_rq_baseline_rows": seeded_rows,
                    "new_tables": ["steer_prompt_answer"],
                    "superseded_question_set": "T023_RQ_QUESTION_SET_V001",
                }
            ),
            None,
            stamp,
        ),
    )
    connection.execute(
        "UPDATE ledger_metadata SET schema_version=? WHERE schema_version=?",
        (LEDGER_SCHEMA_VERSION, PRIOR_LEDGER_SCHEMA_VERSION),
    )
    return receipt_id


def initialize_operational_ledger(path: str | Path) -> dict[str, Any]:
    ledger = Path(path).resolve()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    created = not ledger.exists()
    migrated_from: str | None = None
    migration_receipt_id: str | None = None
    migration_receipt_ids: list[str] = []
    with closing(sqlite3.connect(ledger)) as connection:
        if created:
            _ledger_schema(connection)
            connection.commit()
        else:
            version = connection.execute("SELECT schema_version FROM ledger_metadata").fetchone()
            if version and version[0] == LEGACY_LEDGER_SCHEMA_VERSION:
                migrated_from = LEGACY_LEDGER_SCHEMA_VERSION
                migration_receipt_ids.append(_migrate_ledger_v001_to_v002(connection))
                migration_receipt_ids.append(_migrate_ledger_v002_to_v003(connection))
                migration_receipt_id = migration_receipt_ids[-1]
                connection.commit()
            elif version and version[0] == PRIOR_LEDGER_SCHEMA_VERSION:
                migrated_from = PRIOR_LEDGER_SCHEMA_VERSION
                migration_receipt_id = _migrate_ledger_v002_to_v003(connection)
                migration_receipt_ids.append(migration_receipt_id)
                connection.commit()
            elif not version or version[0] != LEDGER_SCHEMA_VERSION:
                raise RuntimeError("OPERATIONAL_LEDGER_SCHEMA_MISMATCH")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    if integrity != "ok" or foreign_keys:
        raise RuntimeError("OPERATIONAL_LEDGER_VALIDATION_FAILED")
    rq_sidecar = ledger.parent / "RQ_LEDGER.json"
    if created or migrated_from or not rq_sidecar.exists():
        _write_current_rq_sidecar(ledger)
    steer_sidecar = ledger.parent / "STEER_PROMPT_ANSWER_LOG.json"
    if ledger.parent.name.casefold() == "codex" and not steer_sidecar.exists():
        _write_json(
            steer_sidecar,
            {"schema": STEER_PROMPT_LOG_SCHEMA, "product_name": "Evidence Lane", "entries": []},
        )
    return {
        "ledger": str(ledger),
        "created": created,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "migrated_from": migrated_from,
        "migration_receipt_id": migration_receipt_id,
        "migration_receipt_ids": migration_receipt_ids,
        "sha256": _sha256_file(ledger),
    }


def append_task_run(path: str | Path, record: Mapping[str, Any]) -> int:
    required = {
        "parent_goal_id",
        "run_id",
        "task_id",
        "step_id",
        "delta_ids_active",
        "start_timestamp",
        "starting_snapshot_hash",
        "completion_status",
        "next_exact_pointer",
    }
    missing = sorted(required - set(record))
    if missing:
        raise ValueError("TASK_RUN_FIELDS_MISSING:" + ",".join(missing))
    fields = (
        "parent_goal_id", "run_id", "task_id", "step_id", "delta_ids_active", "model", "reasoning_mode",
        "start_timestamp", "end_timestamp", "elapsed_seconds", "starting_snapshot_hash", "ending_candidate_hash",
        "sources_queried", "sqlite_tables_queried", "fts_queries_issued", "raw_files_reread",
        "unchanged_files_skipped", "files_changed", "tests_run", "test_results", "build_status",
        "human_gate_status", "completion_status", "next_exact_pointer", "prior_row_id",
    )
    json_fields = {
        "delta_ids_active", "sources_queried", "sqlite_tables_queried", "fts_queries_issued", "raw_files_reread",
        "unchanged_files_skipped", "files_changed", "tests_run", "test_results",
    }
    values = [_json(record.get(field, [])) if field in json_fields else record.get(field) for field in fields]
    with closing(sqlite3.connect(Path(path))) as connection:
        cursor = connection.execute(
            f"INSERT INTO task_run({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values
        )
        connection.commit()
        return int(cursor.lastrowid)


def record_project_query(path: str | Path, record: Mapping[str, Any]) -> int:
    required = {
        "snapshot_hash",
        "query_type",
        "query_text",
        "tables_queried",
        "result_count",
    }
    missing = sorted(required - set(record))
    if missing:
        raise ValueError("PROJECT_QUERY_FIELDS_MISSING:" + ",".join(missing))
    with closing(sqlite3.connect(Path(path))) as connection:
        cursor = connection.execute(
            "INSERT INTO project_query_log(task_run_id,snapshot_hash,query_type,query_text,tables_queried,result_count,recorded_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                record.get("task_run_id"),
                record["snapshot_hash"],
                record["query_type"],
                record["query_text"],
                _json(record["tables_queried"]),
                int(record["result_count"]),
                _utc_now(),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def record_model_usage(path: str | Path, record: Mapping[str, Any]) -> int:
    ledger_path = Path(path).resolve()
    classification = str(record.get("classification") or "")
    if classification not in USAGE_CLASSIFICATIONS:
        raise ValueError("INVALID_USAGE_CLASSIFICATION")
    token_fields = (
        "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"
    )
    if classification == "EXACT_PROVIDER_USAGE":
        required = {"provider", "model", "provider_response_id", "call_started_at", "call_completed_at", "total_tokens"}
        missing = sorted(field for field in required if record.get(field) in (None, ""))
        if missing:
            raise ValueError("EXACT_USAGE_FIELDS_MISSING:" + ",".join(missing))
        token_exact = 1
    else:
        if any(record.get(field) is not None for field in token_fields):
            raise ValueError("NON_AUTHORITATIVE_USAGE_MUST_NOT_STORE_TOKEN_COUNTS")
        token_exact = 0
    elapsed_seconds = _usage_elapsed_seconds(record)
    goal_usage_summary = format_goal_usage_summary(
        int(record["total_tokens"]) if token_exact else None,
        elapsed_seconds,
        token_exact=bool(token_exact),
    )
    fields = (
        "task_run_id", "goal_id", "run_id", "task_id", "classification", "token_exact", "usage_source",
        "provider", "model", "provider_response_id", *token_fields, "call_started_at", "call_completed_at",
        "elapsed_seconds", "quota_checkpoint_json", "prior_row_id", "recorded_at",
    )
    values = []
    for field in fields:
        if field == "token_exact":
            values.append(token_exact)
        elif field == "quota_checkpoint_json":
            values.append(_json(record.get("quota_checkpoint", {})))
        elif field == "recorded_at":
            values.append(_utc_now())
        elif field == "elapsed_seconds":
            values.append(elapsed_seconds)
        else:
            values.append(record.get(field))
    with closing(sqlite3.connect(ledger_path)) as connection:
        cursor = connection.execute(
            f"INSERT INTO model_call_usage({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values
        )
        connection.commit()
        row_id = int(cursor.lastrowid)
    if ledger_path.parent.name.casefold() == "codex":
        _write_json(
            ledger_path.parent / "TASK_USAGE_RECEIPT.json",
            {
                "ledger_row_id": row_id,
                "classification": classification,
                "token_exact": bool(token_exact),
                "goal_usage": goal_usage_summary,
                "usage_source": record.get("usage_source"),
                "provider": record.get("provider"),
                "model": record.get("model"),
                "provider_response_id": record.get("provider_response_id"),
                **{field: record.get(field) for field in token_fields},
                "call_started_at": record.get("call_started_at"),
                "call_completed_at": record.get("call_completed_at"),
                "elapsed_seconds": elapsed_seconds,
                "quota_checkpoint": record.get("quota_checkpoint", {}),
            },
        )
    return row_id


def record_steer_prompt_answer(path: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    ledger_path = Path(path).resolve()
    required = {"task_run_id", "steer_id", "prompt_id", "prompt_text", "answer_text"}
    missing = sorted(field for field in required if record.get(field) in (None, ""))
    if missing:
        raise ValueError("STEER_PROMPT_ANSWER_FIELDS_MISSING:" + ",".join(missing))
    task_run_id = int(record["task_run_id"])
    if task_run_id <= 0:
        raise ValueError("TASK_RUN_ID_REQUIRED")
    answer_status = str(record.get("answer_status") or "CANDIDATE")
    if answer_status not in CLAIM_STATES:
        raise ValueError("STEER_PROMPT_ANSWER_STATUS_INVALID")
    classification = str(record.get("usage_classification") or "UNAVAILABLE")
    if classification not in USAGE_CLASSIFICATIONS:
        raise ValueError("INVALID_USAGE_CLASSIFICATION")
    total_tokens = record.get("total_tokens")
    if classification == "EXACT_PROVIDER_USAGE":
        if total_tokens is None:
            raise ValueError("EXACT_USAGE_FIELDS_MISSING:total_tokens")
        total_tokens = int(total_tokens)
        if total_tokens < 0:
            raise ValueError("TOTAL_TOKENS_MUST_BE_NONNEGATIVE")
        token_exact = True
    else:
        if total_tokens is not None:
            raise ValueError("NON_AUTHORITATIVE_USAGE_MUST_NOT_STORE_TOKEN_COUNTS")
        token_exact = False
    elapsed_seconds = _usage_elapsed_seconds(record)
    goal_usage_summary = format_goal_usage_summary(
        total_tokens,
        elapsed_seconds,
        token_exact=token_exact,
    )
    _assert_no_logged_secret(record)
    recorded_at = _utc_now()
    values = (
        task_run_id,
        str(record["steer_id"]),
        str(record["prompt_id"]),
        str(record["prompt_text"]),
        str(record["answer_text"]),
        answer_status,
        classification,
        goal_usage_summary,
        total_tokens,
        elapsed_seconds,
        record.get("prior_row_id"),
        recorded_at,
    )
    with closing(sqlite3.connect(ledger_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        cursor = connection.execute(
            "INSERT INTO steer_prompt_answer("
            "task_run_id,steer_id,prompt_id,prompt_text,answer_text,answer_status,usage_classification,"
            "goal_usage_summary,total_tokens,elapsed_seconds,prior_row_id,recorded_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        connection.commit()
        row_id = int(cursor.lastrowid)
    sidecar_entry = {
        "ledger_row_id": row_id,
        "task_run_id": task_run_id,
        "steer_id": str(record["steer_id"]),
        "prompt_id": str(record["prompt_id"]),
        "prompt": str(record["prompt_text"]),
        "answer": str(record["answer_text"]),
        "answer_status": answer_status,
        "usage_classification": classification,
        "goal_usage": goal_usage_summary,
        "recorded_at": recorded_at,
    }
    if ledger_path.parent.name.casefold() == "codex":
        sidecar_path = ledger_path.parent / "STEER_PROMPT_ANSWER_LOG.json"
        sidecar: dict[str, Any] = {
            "schema": STEER_PROMPT_LOG_SCHEMA,
            "product_name": "Evidence Lane",
            "entries": [],
        }
        if sidecar_path.exists():
            loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                sidecar = loaded
        sidecar["schema"] = STEER_PROMPT_LOG_SCHEMA
        sidecar["product_name"] = "Evidence Lane"
        sidecar["entries"].append(sidecar_entry)
        _write_json(sidecar_path, sidecar)
    return {"row_id": row_id, "goal_usage": goal_usage_summary}


def _assert_no_logged_secret(value: Any, path: str = "record") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET_COLUMN.search(str(key)):
                raise ValueError(f"RAW_SECRET_MATERIAL_FORBIDDEN:{path}.{key}")
            _assert_no_logged_secret(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_logged_secret(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise ValueError(f"RAW_SECRET_MATERIAL_FORBIDDEN:{path}")


def append_model_execution_run(path: str | Path, record: Mapping[str, Any]) -> int:
    required = {
        "execution_id",
        "brain_id",
        "brain_snapshot_hash",
        "connector_id",
        "endpoint_class",
        "model_id",
        "user_request",
        "request_timestamp",
        "status",
    }
    missing = sorted(field for field in required if record.get(field) in (None, ""))
    if missing:
        raise ValueError("MODEL_EXECUTION_FIELDS_MISSING:" + ",".join(missing))
    _assert_no_logged_secret(record)
    user_request = str(record["user_request"])
    fields = (
        "execution_id",
        "task_run_id",
        "brain_id",
        "brain_snapshot_hash",
        "connector_id",
        "endpoint_class",
        "model_id",
        "user_request",
        "user_request_sha256",
        "evidence_slice_ids_json",
        "sql_queries_json",
        "fts_queries_json",
        "request_timestamp",
        "response_timestamp",
        "provider_response_id",
        "response_text",
        "visible_reasoning_summary",
        "proposed_files_json",
        "patch_artifact_ref",
        "test_status",
        "human_decision",
        "resulting_delta_id",
        "status",
        "prior_row_id",
        "recorded_at",
    )
    values = (
        str(record["execution_id"]),
        record.get("task_run_id"),
        str(record["brain_id"]),
        str(record["brain_snapshot_hash"]),
        str(record["connector_id"]),
        str(record["endpoint_class"]),
        str(record["model_id"]),
        user_request,
        _sha256_bytes(user_request.encode("utf-8")),
        _json(record.get("evidence_slice_ids", [])),
        _json(record.get("sql_queries", [])),
        _json(record.get("fts_queries", [])),
        str(record["request_timestamp"]),
        record.get("response_timestamp"),
        record.get("provider_response_id"),
        record.get("response_text"),
        record.get("visible_reasoning_summary"),
        _json(record.get("proposed_files", [])),
        record.get("patch_artifact_ref"),
        record.get("test_status"),
        record.get("human_decision"),
        record.get("resulting_delta_id"),
        str(record["status"]),
        record.get("prior_row_id"),
        _utc_now(),
    )
    with closing(sqlite3.connect(Path(path).resolve())) as connection:
        cursor = connection.execute(
            f"INSERT INTO model_execution_run({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
        connection.commit()
        return int(cursor.lastrowid)


def append_endpoint_execution_event(path: str | Path, record: Mapping[str, Any]) -> int:
    required = {"execution_run_id", "event_type", "status", "usage_classification"}
    missing = sorted(field for field in required if record.get(field) in (None, ""))
    if missing:
        raise ValueError("ENDPOINT_EXECUTION_EVENT_FIELDS_MISSING:" + ",".join(missing))
    classification = str(record["usage_classification"])
    if classification not in USAGE_CLASSIFICATIONS:
        raise ValueError("INVALID_USAGE_CLASSIFICATION")
    usage = record.get("usage") or {}
    if not isinstance(usage, Mapping):
        raise ValueError("ENDPOINT_USAGE_MAPPING_REQUIRED")
    token_fields = {
        "input_tokens",
        "cached_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    }
    if classification != "EXACT_PROVIDER_USAGE" and any(usage.get(field) is not None for field in token_fields):
        raise ValueError("NON_AUTHORITATIVE_USAGE_MUST_NOT_STORE_TOKEN_COUNTS")
    _assert_no_logged_secret(record)
    fields = (
        "execution_run_id",
        "event_type",
        "status",
        "provider_response_id",
        "usage_classification",
        "usage_json",
        "response_text",
        "response_artifact_ref",
        "error_code",
        "exact_error",
        "prior_row_id",
        "recorded_at",
    )
    values = (
        int(record["execution_run_id"]),
        str(record["event_type"]),
        str(record["status"]),
        record.get("provider_response_id"),
        classification,
        _json(dict(usage)),
        record.get("response_text"),
        record.get("response_artifact_ref"),
        record.get("error_code"),
        record.get("exact_error"),
        record.get("prior_row_id"),
        _utc_now(),
    )
    with closing(sqlite3.connect(Path(path).resolve())) as connection:
        cursor = connection.execute(
            f"INSERT INTO endpoint_execution_event({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
        connection.commit()
        return int(cursor.lastrowid)


def append_model_failure_event(path: str | Path, record: Mapping[str, Any]) -> int:
    required = {
        "failure_id",
        "task_id",
        "model_or_endpoint",
        "stage",
        "failure_class",
        "observed_evidence",
        "exact_error",
        "retry_safe",
        "data_preserved",
        "rollback_available",
        "next_action_code",
        "next_action_text",
        "unresolved_risk",
        "timestamp",
    }
    missing = sorted(field for field in required if field not in record or record.get(field) is None)
    if missing:
        raise ValueError("MODEL_FAILURE_FIELDS_MISSING:" + ",".join(missing))
    failure_class = str(record["failure_class"])
    if failure_class not in MODEL_FAILURE_CLASSES:
        raise ValueError("MODEL_FAILURE_CLASS_INVALID")
    inferred_cause = record.get("inferred_cause")
    inferred_cause_labeled = bool(record.get("inferred_cause_labeled"))
    if inferred_cause and not inferred_cause_labeled:
        raise ValueError("INFERRED_CAUSE_MUST_BE_LABELED")
    for field in ("retry_safe", "data_preserved", "rollback_available"):
        if not isinstance(record[field], bool):
            raise ValueError(f"MODEL_FAILURE_BOOLEAN_REQUIRED:{field}")
    if not isinstance(record["observed_evidence"], Mapping):
        raise ValueError("MODEL_FAILURE_OBSERVED_EVIDENCE_MAPPING_REQUIRED")
    _assert_no_logged_secret(record)
    fields = (
        "failure_id",
        "execution_run_id",
        "task_id",
        "model_or_endpoint",
        "package_id",
        "lane_id",
        "stage",
        "failure_class",
        "observed_evidence_json",
        "exact_error",
        "inferred_cause",
        "inferred_cause_labeled",
        "retry_safe",
        "data_preserved",
        "rollback_available",
        "next_action_code",
        "next_action_text",
        "unresolved_risk",
        "failure_timestamp",
        "prior_row_id",
        "recorded_at",
    )
    values = (
        str(record["failure_id"]),
        record.get("execution_run_id"),
        str(record["task_id"]),
        str(record["model_or_endpoint"]),
        record.get("package_id"),
        record.get("lane_id"),
        str(record["stage"]),
        failure_class,
        _json(dict(record["observed_evidence"])),
        str(record["exact_error"]),
        inferred_cause,
        int(inferred_cause_labeled),
        int(record["retry_safe"]),
        int(record["data_preserved"]),
        int(record["rollback_available"]),
        str(record["next_action_code"]),
        str(record["next_action_text"]),
        str(record["unresolved_risk"]),
        str(record["timestamp"]),
        record.get("prior_row_id"),
        _utc_now(),
    )
    with closing(sqlite3.connect(Path(path).resolve())) as connection:
        cursor = connection.execute(
            f"INSERT INTO model_failure_event({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
        connection.commit()
        return int(cursor.lastrowid)


def record_rq_answers(path: str | Path, task_run_id: int, answers: Mapping[str, str], evidence: Mapping[str, Any] | None = None) -> list[int]:
    ledger_path = Path(path).resolve()
    missing = sorted(set(RQ_QUESTIONS) - set(answers))
    if missing:
        raise ValueError("RQ_ANSWERS_MISSING:" + ",".join(missing))
    rows: list[int] = []
    with closing(sqlite3.connect(ledger_path)) as connection:
        for question_id, question in RQ_QUESTIONS.items():
            cursor = connection.execute(
                "INSERT INTO rq_ledger(task_run_id,question_id,question,answer,claim_status,evidence_json,recorded_at) VALUES(?,?,?,?,?,?,?)",
                (
                    task_run_id,
                    question_id,
                    question,
                    answers[question_id],
                    "CANDIDATE",
                    _json((evidence or {}).get(question_id, [])),
                    _utc_now(),
                ),
            )
            rows.append(int(cursor.lastrowid))
        connection.commit()
    _write_current_rq_sidecar(
        ledger_path,
        answers,
        task_run_id=task_run_id,
        evidence=evidence,
    )
    return rows


def register_goal_delta(path: str | Path, record: Mapping[str, Any]) -> int:
    fields = (
        "delta_id", "parent_goal_id", "run_id", "task_pointer", "title", "prompt_pointer", "insertion_reason",
        "insertion_point", "status", "existing_requirements_preserved", "current_task_replaced",
        "current_goal_restarted", "prior_row_id", "recorded_at",
    )
    values = [
        record.get(field, _utc_now() if field == "recorded_at" else None)
        for field in fields
    ]
    if values[9] is not True or values[10] not in (False, 0) or values[11] not in (False, 0):
        raise ValueError("GOAL_DELTA_PRESERVATION_LAW_VIOLATION")
    with closing(sqlite3.connect(Path(path))) as connection:
        cursor = connection.execute(
            f"INSERT INTO goal_delta_ledger({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values
        )
        connection.commit()
        return int(cursor.lastrowid)


def _snapshot_identity(snapshot: Path) -> dict[str, str]:
    with closing(_connect_ro(snapshot)) as connection:
        row = connection.execute("SELECT snapshot_id,input_set_hash,claim_status FROM snapshot_metadata").fetchone()
    return dict(row)


def _package_member_is_safe(relative: str) -> bool:
    if not relative or "\\" in relative or relative.endswith("/"):
        return False
    posix = PurePosixPath(relative)
    windows = PureWindowsPath(relative)
    raw_parts = relative.split("/")
    return (
        not posix.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and all(part not in {"", ".", ".."} for part in raw_parts)
    )


def _package_file_manifest(package_root: Path, *, freeze_operational: bool = False) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(package_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_file() and path.name != "PACKAGE_MANIFEST.json":
            relative = path.relative_to(package_root).as_posix()
            rows.append({
                "path": relative,
                "byte_size": path.stat().st_size,
                "sha256": _sha256_file(path),
                "mutable_operational": relative.startswith("codex/") and not freeze_operational,
            })
    return rows


def _write_package_manifest(package_root: Path, *, freeze_operational: bool = False) -> None:
    _write_json(
        package_root / "manifests" / "PACKAGE_MANIFEST.json",
        {
            "distribution_frozen": freeze_operational,
            "files": _package_file_manifest(package_root, freeze_operational=freeze_operational),
        },
    )


def create_portable_brain_package(
    brain_root: str | Path,
    destination: str | Path,
    *,
    goal_pointer: Mapping[str, Any],
    delta_ledger: Mapping[str, Any],
    latest_good_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    package_root = Path(destination).resolve()
    package_root.mkdir(parents=True, exist_ok=True)
    snapshot_result = create_immutable_snapshot(brain_root, package_root / "brain_snapshot.sqlite")
    ledger_result = initialize_operational_ledger(package_root / "codex" / "codex_runtime_ledger.sqlite")
    identity = _snapshot_identity(Path(snapshot_result["snapshot"]))

    for directory in ("reader", "manifests", "pointers", "topology", "receipts", "public_query_contract", "codex"):
        (package_root / directory).mkdir(parents=True, exist_ok=True)
    (package_root / "reader" / "README.md").write_text(
        "# EvidenceOS immutable reader\n\nOpen `../brain_snapshot.sqlite` with `mode=ro&immutable=1`. "
        "Canonical mutation and truth promotion are forbidden.\n",
        encoding="utf-8",
    )
    (package_root / "public_query_contract" / "PUBLIC_QUERY_CONTRACT.md").write_text(
        "# Public query contract\n\nTHE BRAIN IS A VERSIONED, SOURCE-LINKED PROJECT SNAPSHOT.\n\n"
        "Read/query and bounded what-if reasoning are allowed. Snapshot mutation, hidden package rewriting, "
        "credential access, and direct truth promotion are forbidden.\n",
        encoding="utf-8",
    )
    _write_json(package_root / "pointers" / "CURRENT_BRAIN_POINTER.json", identity)
    _write_json(package_root / "codex" / "CURRENT_GOAL_POINTER.json", dict(goal_pointer))
    _write_json(package_root / "codex" / "ACTIVE_DELTA_LEDGER.json", dict(delta_ledger))
    _write_json(package_root / "codex" / "CURRENT_BRAIN_POINTER.json", identity)
    _write_json(package_root / "codex" / "LATEST_GOOD_SNAPSHOT.json", dict(latest_good_snapshot or {"status": "UNVERIFIED"}))
    _write_json(
        package_root / "codex" / "TASK_USAGE_RECEIPT.json",
        {
            "classification": "UNAVAILABLE",
            "token_exact": False,
            "goal_usage": format_goal_usage_summary(None, None, token_exact=False),
        },
    )
    _write_current_rq_sidecar(Path(ledger_result["ledger"]))
    (package_root / "codex" / "CODEX_NEXT_TASK.md").write_text(
        f"# Codex next task\n\nCurrent pointer: `{goal_pointer.get('current_task_pointer', 'UNVERIFIED')}`\n",
        encoding="utf-8",
    )
    _write_json(package_root / "topology" / "SOURCE_TOPOLOGY_POINTERS.json", {"status": "SOURCE_LINKED", "snapshot_id": identity["snapshot_id"]})
    receipt = {
        "created_at": _utc_now(),
        "snapshot": identity,
        "snapshot_reused": snapshot_result["snapshot_reused"],
        "snapshot_mutated_in_place": False,
        "operational_ledger_created": ledger_result["created"],
        "human_gate": "PENDING",
    }
    _write_json(package_root / "receipts" / "PACKAGE_CREATION_RECEIPT.json", receipt)
    _write_package_manifest(package_root)
    validation = validate_portable_brain_package(package_root)
    if validation["status"] != "PASS":
        raise RuntimeError("PORTABLE_PACKAGE_VALIDATION_FAILED:" + ",".join(validation["errors"]))
    return {
        "package_root": str(package_root),
        "snapshot": snapshot_result,
        "ledger": ledger_result,
        "validation": validation,
    }


def validate_portable_brain_package(package: str | Path) -> dict[str, Any]:
    root = Path(package).resolve()
    errors: list[str] = []
    required = {
        "brain_snapshot.sqlite",
        "reader/README.md",
        "manifests/PACKAGE_MANIFEST.json",
        "pointers/CURRENT_BRAIN_POINTER.json",
        "public_query_contract/PUBLIC_QUERY_CONTRACT.md",
        "codex/codex_runtime_ledger.sqlite",
        "codex/CODEX_NEXT_TASK.md",
        "codex/CURRENT_GOAL_POINTER.json",
        "codex/ACTIVE_DELTA_LEDGER.json",
        "codex/CURRENT_BRAIN_POINTER.json",
        "codex/LATEST_GOOD_SNAPSHOT.json",
        "codex/TASK_USAGE_RECEIPT.json",
        "codex/RQ_LEDGER.json",
        "codex/STEER_PROMPT_ANSWER_LOG.json",
    }
    present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    for missing in sorted(required - present):
        errors.append("MISSING:" + missing)
    if any(path.suffix.casefold() == ".zip" for path in root.rglob("*")):
        errors.append("NESTED_ZIP_FORBIDDEN")
    for database in (root / "brain_snapshot.sqlite", root / "codex" / "codex_runtime_ledger.sqlite"):
        if not database.exists():
            continue
        with closing(_connect_ro(database)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                errors.append("SQLITE_INTEGRITY_FAILED:" + database.name)
            if list(connection.execute("PRAGMA foreign_key_check")):
                errors.append("SQLITE_FOREIGN_KEY_FAILED:" + database.name)
            if database.name == "brain_snapshot.sqlite":
                try:
                    schema_version = str(
                        connection.execute("SELECT schema_version FROM snapshot_metadata LIMIT 1").fetchone()[0]
                    )
                    if schema_version != SNAPSHOT_SCHEMA_VERSION:
                        errors.append("SNAPSHOT_SCHEMA_VERSION_INVALID:" + schema_version)
                    source_row_object = connection.execute(
                        "SELECT type FROM sqlite_master WHERE name='source_table_row'"
                    ).fetchone()
                    if source_row_object is None or str(source_row_object[0]) != "view":
                        errors.append("SNAPSHOT_SOURCE_ROW_VIEW_REQUIRED")
                    canonical_rows = int(
                        connection.execute("SELECT COUNT(*) FROM canonical_row_content").fetchone()[0]
                    )
                    unique_hashes = int(
                        connection.execute("SELECT COUNT(DISTINCT row_hash) FROM canonical_row_content").fetchone()[0]
                    )
                    indexed_rows = int(connection.execute("SELECT COUNT(*) FROM snapshot_fts").fetchone()[0])
                    if canonical_rows != unique_hashes:
                        errors.append("SNAPSHOT_CANONICAL_ROW_DUPLICATION")
                    if indexed_rows != canonical_rows:
                        errors.append("SNAPSHOT_CANONICAL_FTS_PARITY_FAILED")
                    fts_sql = str(
                        connection.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='snapshot_fts'"
                        ).fetchone()[0]
                    ).casefold()
                    if "content='canonical_row_content'" not in fts_sql.replace('"', "'"):
                        errors.append("SNAPSHOT_FTS_EXTERNAL_CONTENT_REQUIRED")
                except (sqlite3.Error, TypeError, IndexError) as exc:
                    errors.append("SNAPSHOT_CONTENT_ADDRESS_CONTRACT_INVALID:" + type(exc).__name__)
    manifest_path = root / "manifests" / "PACKAGE_MANIFEST.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = manifest.get("files")
            if not isinstance(rows, list):
                raise ValueError("MANIFEST_FILES_INVALID")
            declared: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    errors.append("MANIFEST_ROW_INVALID")
                    continue
                relative = str(row.get("path") or "")
                if not _package_member_is_safe(relative):
                    errors.append("MANIFEST_MEMBER_PATH_INVALID:" + relative)
                    continue
                if relative in declared:
                    errors.append("MANIFEST_MEMBER_DUPLICATE:" + relative)
                    continue
                declared.add(relative)
                target = (root / Path(*PurePosixPath(relative).parts)).resolve()
                if root not in target.parents or not target.is_file():
                    errors.append("MANIFEST_MEMBER_MISSING:" + relative)
                    continue
                if row.get("mutable_operational") is True:
                    continue
                if target.stat().st_size != int(row["byte_size"]):
                    errors.append("MANIFEST_SIZE_MISMATCH:" + relative)
                if _sha256_file(target) != str(row["sha256"]).upper():
                    errors.append("MANIFEST_HASH_MISMATCH:" + relative)
            manifest_member = "manifests/PACKAGE_MANIFEST.json"
            if declared != present - {manifest_member}:
                errors.append("MANIFEST_COVERAGE_MISMATCH")
        except Exception as exc:
            errors.append("MANIFEST_INVALID:" + type(exc).__name__)
    return {"package": str(root), "status": "PASS" if not errors else "FAIL", "errors": errors, "file_count": len(present)}


def export_portable_brain_zip(package_root: str | Path, destination: str | Path) -> dict[str, Any]:
    root = Path(package_root).resolve()
    validation = validate_portable_brain_package(root)
    if validation["status"] != "PASS":
        raise RuntimeError("PORTABLE_PACKAGE_NOT_VALID")
    identity = _snapshot_identity(root / "brain_snapshot.sqlite")
    archive_path = Path(destination).resolve()
    if archive_path.exists():
        raise FileExistsError("IMMUTABLE_DISTRIBUTED_ZIP_ALREADY_EXISTS")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_package_manifest(root, freeze_operational=True)
        frozen_validation = validate_portable_brain_package(root)
        if frozen_validation["status"] != "PASS":
            raise RuntimeError("PORTABLE_PACKAGE_FREEZE_FAILED")
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
    except Exception:
        if archive_path.exists():
            archive_path.unlink()
        raise
    finally:
        # The distributed archive is immutable; the extracted working package
        # remains writable only in its controlled codex operational layer.
        _write_package_manifest(root, freeze_operational=False)
    reopened = validate_portable_brain_zip(archive_path)
    if reopened["status"] != "PASS":
        raise RuntimeError("PORTABLE_ZIP_VALIDATION_FAILED")
    return {
        "archive": str(archive_path),
        "sha256": _sha256_file(archive_path),
        "snapshot_id": identity["snapshot_id"],
        "zip_crc": reopened["zip_crc"],
        "nested_zip_count": reopened["nested_zip_count"],
        "member_count": reopened["member_count"],
    }


def validate_portable_brain_zip(path: str | Path) -> dict[str, Any]:
    archive_path = Path(path).resolve()
    errors: list[str] = []
    names: list[str] = []
    if not archive_path.is_file():
        return {"status": "FAIL", "archive": str(archive_path), "errors": ["PORTABLE_ZIP_MISSING"]}
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            file_names = [info.filename for info in infos if not info.is_dir()]
            crc_member = archive.testzip()
            if crc_member:
                errors.append("ZIP_CRC_FAILED:" + crc_member)
            nested = [name for name in names if name.casefold().endswith(".zip")]
            if nested:
                errors.append("NESTED_ZIP_FORBIDDEN")
            if len(names) != len(set(names)):
                errors.append("DUPLICATE_ZIP_MEMBER")
            for member in file_names:
                if not _package_member_is_safe(member):
                    errors.append("ZIP_MEMBER_PATH_INVALID:" + member)
            required = {
                "brain_snapshot.sqlite",
                "manifests/PACKAGE_MANIFEST.json",
                "codex/codex_runtime_ledger.sqlite",
                "codex/CURRENT_GOAL_POINTER.json",
                "codex/ACTIVE_DELTA_LEDGER.json",
                "codex/RQ_LEDGER.json",
                "codex/STEER_PROMPT_ANSWER_LOG.json",
            }
            for missing in sorted(required - set(file_names)):
                errors.append("ZIP_MEMBER_MISSING:" + missing)
            if "manifests/PACKAGE_MANIFEST.json" in file_names:
                manifest = json.loads(archive.read("manifests/PACKAGE_MANIFEST.json"))
                if manifest.get("distribution_frozen") is not True:
                    errors.append("ZIP_MANIFEST_NOT_FROZEN")
                rows = manifest.get("files")
                if not isinstance(rows, list):
                    raise ValueError("ZIP_MANIFEST_FILES_INVALID")
                declared: set[str] = set()
                for row in rows:
                    if not isinstance(row, dict):
                        errors.append("ZIP_MANIFEST_ROW_INVALID")
                        continue
                    member = str(row.get("path") or "")
                    if not _package_member_is_safe(member):
                        errors.append("ZIP_MANIFEST_MEMBER_PATH_INVALID:" + member)
                        continue
                    if member in declared:
                        errors.append("ZIP_MANIFEST_MEMBER_DUPLICATE:" + member)
                        continue
                    declared.add(member)
                    if member not in file_names:
                        errors.append("ZIP_MANIFEST_MEMBER_MISSING:" + member)
                        continue
                    payload = archive.read(member)
                    if len(payload) != int(row.get("byte_size", -1)):
                        errors.append("ZIP_MANIFEST_SIZE_MISMATCH:" + member)
                    if _sha256_bytes(payload) != str(row.get("sha256") or "").upper():
                        errors.append("ZIP_MANIFEST_HASH_MISMATCH:" + member)
                if declared != set(file_names) - {"manifests/PACKAGE_MANIFEST.json"}:
                    errors.append("ZIP_MANIFEST_COVERAGE_MISMATCH")
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError, ValueError) as exc:
        errors.append("PORTABLE_ZIP_INVALID:" + type(exc).__name__)
    return {
        "status": "PASS" if not errors else "FAIL",
        "archive": str(archive_path),
        "errors": errors,
        "zip_crc": "PASS" if not any(error.startswith("ZIP_CRC_FAILED") for error in errors) else "FAIL",
        "nested_zip_count": len([name for name in names if name.casefold().endswith(".zip")]),
        "member_count": len(names),
        "sha256": _sha256_file(archive_path),
    }
