from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.source_fingerprint_cache import (
    cached_content_hash,
    record_content_hash,
)
from sqlite_brain_builder.runtime.structural_ingestion import (
    InspectionLimits,
    RESEARCH_KIND_TO_TABLE,
    RESEARCH_SCHEMA,
    SourceInspection,
    SourceItem,
    inspect_source_structure,
    read_source_item_text,
)


class Env15ResearchError(RuntimeError):
    pass


_MARKER = re.compile(
    r"^(?:#{1,6}\s*)?(research\s+question|open\s+question|question|hypothesis|"
    r"method(?:ology)?|evidence|finding|result|limitation|citation|source)\s*[:\-]\s*(.+)$",
    flags=re.IGNORECASE,
)

_APPEND_ONLY_TABLES = (
    "source_registry",
    "artifact_registry",
    "chunk_index",
    "relation_edge",
    "mutation_receipt",
    "research_source",
    "research_question",
    "research_hypothesis",
    "research_method",
    "research_evidence",
    "research_finding",
    "research_limitation",
    "research_citation",
    "research_open_question",
    "research_receipt",
    "research_fts_document",
    "research_append_prepare",
    "research_append_commit",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_id(kind: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts).encode(
        "utf-8", "surrogatepass"
    )
    return f"{kind}_{hashlib.sha256(material).hexdigest()}"


def _set_writable(path: Path, writable: bool) -> None:
    mode = path.stat().st_mode
    path.chmod(
        mode | stat.S_IWUSR
        if writable
        else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )


def _research_records(text: str) -> list[tuple[str, int, str]]:
    records: list[tuple[str, int, str]] = []
    aliases = {
        "research_question": "question",
        "methodology": "method",
        "result": "finding",
        "source": "citation",
    }
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        match = _MARKER.match(line)
        if match:
            kind = re.sub(r"\s+", "_", match.group(1).strip().casefold())
            records.append((aliases.get(kind, kind), line_number, match.group(2).strip()))
        elif re.search(r"https?://|\bdoi:\s*10\.", line, flags=re.IGNORECASE):
            records.append(("citation", line_number, line))
        elif line.endswith("?") and len(line) <= 500:
            records.append(("question", line_number, line))
    if not records and text.strip():
        records.append(("evidence", 1, text.strip()[:12_000]))
    return records


def _inline_inspection(source_text: str, display_name: str) -> SourceInspection:
    data = source_text.encode("utf-8", "surrogatepass")
    digest = _sha256_bytes(data)
    safe = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in display_name
    ).strip("._") or "research_text"
    logical_path = f"inline/{safe}.md"
    return SourceInspection(
        source_path=Path(f"inline:{digest}"),
        source_kind="inline_text",
        display_name=display_name,
        sha256=digest,
        size_bytes=len(data),
        items=(
            SourceItem(
                logical_path=logical_path,
                physical_path=None,
                archive_path=None,
                archive_member_name=None,
                item_kind="inline_text",
                size_bytes=len(data),
                sha256=digest,
                is_sqlite=False,
                is_archive=False,
                is_symlink=False,
                metadata={"encoding": "utf-8", "source": "registered_inline_text"},
            ),
        ),
        archive_crc_status=None,
    )


def _ensure_append_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(RESEARCH_SCHEMA)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_append_prepare(
          turn_id TEXT PRIMARY KEY,
          sequence INTEGER NOT NULL UNIQUE,
          parent_turn_id TEXT,
          source_version_id TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          previous_event_hash TEXT NOT NULL,
          prepared_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS research_append_commit(
          turn_id TEXT PRIMARY KEY REFERENCES research_append_prepare(turn_id),
          sequence INTEGER NOT NULL UNIQUE,
          event_hash TEXT NOT NULL UNIQUE,
          committed_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS research_prepare_monotonic
        BEFORE INSERT ON research_append_prepare BEGIN
          SELECT CASE WHEN NEW.sequence != COALESCE(
            (SELECT MAX(sequence)+1 FROM research_append_prepare),1
          ) THEN RAISE(ABORT,'NON_MONOTONIC_RESEARCH_SEQUENCE') END;
          SELECT CASE WHEN NEW.sequence > 1 AND NEW.parent_turn_id != (
            SELECT turn_id FROM research_append_prepare ORDER BY sequence DESC LIMIT 1
          ) THEN RAISE(ABORT,'RESEARCH_PARENT_TURN_MISMATCH') END;
          SELECT CASE WHEN NEW.sequence > 1 AND NEW.previous_event_hash != (
            SELECT event_hash FROM research_append_commit ORDER BY sequence DESC LIMIT 1
          ) THEN RAISE(ABORT,'RESEARCH_PREVIOUS_HASH_MISMATCH') END;
        END;
        CREATE TRIGGER IF NOT EXISTS research_commit_requires_prepare
        BEFORE INSERT ON research_append_commit BEGIN
          SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM research_append_prepare
            WHERE turn_id=NEW.turn_id AND sequence=NEW.sequence
          ) THEN RAISE(ABORT,'RESEARCH_PREPARE_REQUIRED') END;
        END;
        """
    )
    observed = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )
    }
    for table in _APPEND_ONLY_TABLES:
        if table not in observed:
            continue
        for operation in ("UPDATE", "DELETE"):
            trigger = f"evidence_lane_append_only_{table}_{operation.casefold()}"
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS "{trigger}" BEFORE {operation} '
                f'ON "{table}" BEGIN SELECT RAISE(ABORT,'
                f"'APPEND_ONLY_{operation}_BLOCKED:{table}'); END"
            )


def ensure_env15_research_append_schema(brain_root: str | Path) -> dict[str, Any]:
    """Materialize the canonical empty Research append service without ingesting a lane."""

    root = Path(brain_root).resolve()
    _, database = resolve_env15_sector(root, "research")
    required = {
        "research_source",
        "research_question",
        "research_finding",
        "research_evidence",
        "research_limitation",
        "research_append_prepare",
        "research_append_commit",
    }
    read_only = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        observed = {
            str(row[0])
            for row in read_only.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    finally:
        read_only.close()
    missing = sorted(required - observed)
    if not missing:
        return {
            "contract": "EVIDENCE_LANE_RESEARCH_APPEND_SCHEMA_READY_V1",
            "status": "REUSED_SCHEMA_READY",
            "database": str(database),
            "lane_ingestion_fired": False,
        }

    _set_writable(database, True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        _ensure_append_schema(connection)
        connection.commit()
    finally:
        connection.close()
        _set_writable(database, False)
    return {
        "contract": "EVIDENCE_LANE_RESEARCH_APPEND_SCHEMA_READY_V1",
        "status": "MATERIALIZED_SCHEMA_READY",
        "database": str(database),
        "lane_ingestion_fired": False,
    }


def append_env15_research_source(
    brain_root: str | Path,
    source: dict[str, Any],
    *,
    limits: InspectionLimits | None = None,
) -> dict[str, Any]:
    """Append one content-addressed Research source without replacing history."""

    root = Path(brain_root).resolve()
    _, database = resolve_env15_sector(root, "research")
    limits = limits or InspectionLimits()
    path_text = str(source.get("path") or "").strip()
    source_text = str(source.get("text") or "")
    display_name = str(
        source.get("display_name")
        or source.get("displayName")
        or (Path(path_text).name if path_text else "Research append")
    )
    if path_text:
        source_path = Path(path_text).expanduser().resolve(strict=True)
        if not source_path.is_file() and not source_path.is_dir():
            raise Env15ResearchError("RESEARCH_SOURCE_FILE_OR_DIRECTORY_REQUIRED")
        inspection = inspect_source_structure(source_path, limits=limits)
        source_locator = str(source_path)
    elif source_text:
        inspection = _inline_inspection(source_text, display_name)
        source_locator = f"inline:{inspection.sha256}"
    else:
        raise Env15ResearchError("RESEARCH_SOURCE_PATH_OR_TEXT_REQUIRED")

    logical_source_id = str(
        source.get("source_id")
        or _stable_id("research_logical_source", source_locator.casefold())
    )
    source_version_id = _stable_id(
        "research_source_version", logical_source_id, inspection.sha256
    )
    run_id = _stable_id("research_append_run", source_version_id)
    actor = str(source.get("actor") or "Evidence Lane Research append service")
    provider = str(source.get("provider") or source.get("model") or "USER_SUPPLIED")
    source_time = str(
        source.get("source_time") or source.get("source_time_when_known") or ""
    ) or None
    if source_time is None and path_text:
        source_time = datetime.fromtimestamp(
            Path(path_text).stat().st_mtime, timezone.utc
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")

    if path_text:
        _cached, fingerprint_probe = cached_content_hash(
            root, "research", logical_source_id, Path(path_text)
        )
    else:
        fingerprint_probe = {
            "source_path": source_locator,
            "stat_fingerprint": inspection.sha256,
            "file_count": 1,
            "total_bytes": inspection.size_bytes,
            "inline": True,
        }

    existing_connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro", uri=True
    )
    try:
        tables = {
            str(row[0])
            for row in existing_connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        existing = (
            existing_connection.execute(
                "SELECT receipt_id FROM research_receipt WHERE run_id=?", (run_id,)
            ).fetchone()
            if "research_receipt" in tables
            else None
        )
    finally:
        existing_connection.close()
    if existing:
        record_content_hash(
            root, "research", logical_source_id, inspection.sha256, fingerprint_probe
        )
        return {
            "status": "SKIPPED_UNCHANGED",
            "lane_id": "research",
            "source_id": logical_source_id,
            "source_version_id": source_version_id,
            "source_sha256": inspection.sha256,
            "receipt_id": existing[0],
        }

    item_payloads: list[dict[str, Any]] = []
    total_chunks = 0
    total_records = 0
    for item in inspection.items:
        text = (
            source_text
            if not path_text and item.physical_path is None
            else read_source_item_text(item, limits=limits)
        )
        if text is None:
            text = json.dumps(
                {
                    "logical_path": item.logical_path,
                    "item_kind": item.item_kind,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "metadata": dict(item.metadata),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        lines = text.splitlines() or [text]
        chunks = [
            "\n".join(lines[offset : offset + limits.text_chunk_lines])
            for offset in range(0, len(lines), limits.text_chunk_lines)
            if "\n".join(lines[offset : offset + limits.text_chunk_lines]).strip()
        ]
        records = _research_records(text)
        total_chunks += len(chunks)
        total_records += len(records)
        item_payloads.append({"item": item, "chunks": chunks, "records": records})

    backup = root / "receipts" / f".research_before_{uuid.uuid4().hex}.sqlite"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, backup)
    _set_writable(backup, True)
    before_file_hash = _sha256_file(database)
    _set_writable(database, True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise Env15ResearchError("RESEARCH_FOREIGN_KEYS_NOT_ENABLED")
        _ensure_append_schema(connection)
        latest = connection.execute(
            "SELECT p.sequence,p.turn_id,c.event_hash FROM research_append_prepare p "
            "JOIN research_append_commit c USING(turn_id) ORDER BY p.sequence DESC LIMIT 1"
        ).fetchone()
        sequence = int(latest[0]) + 1 if latest else 1
        parent_turn = str(latest[1]) if latest else None
        previous_event_hash = str(latest[2]) if latest else "0" * 64
        turn_id = str(
            source.get("turn_id")
            or f"RESEARCH.{sequence:09d}.{inspection.sha256[:12]}"
        )
        event_hash = _sha256_bytes(
            "\x1f".join(
                (
                    previous_event_hash,
                    turn_id,
                    logical_source_id,
                    source_version_id,
                    inspection.sha256,
                    actor,
                    provider,
                )
            ).encode("utf-8", "surrogatepass")
        )
        stamp = _utc_now()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO research_append_prepare VALUES(?,?,?,?,?,?,?)",
            (
                turn_id,
                sequence,
                parent_turn,
                source_version_id,
                inspection.sha256,
                previous_event_hash,
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
            (
                source_version_id,
                "research",
                inspection.display_name,
                source_locator,
                inspection.sha256,
                inspection.size_bytes,
                "AVAILABLE",
                turn_id,
            ),
        )
        for payload in item_payloads:
            item = payload["item"]
            artifact_id = _stable_id(
                "research_artifact", source_version_id, item.logical_path, item.sha256
            )
            rich_source_id = _stable_id(
                "research_source", source_version_id, item.logical_path, item.sha256
            )
            connection.execute(
                "INSERT INTO artifact_registry VALUES(?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    source_version_id,
                    item.item_kind,
                    item.logical_path,
                    item.sha256,
                    item.size_bytes,
                    "INDEXED",
                ),
            )
            connection.execute(
                "INSERT INTO relation_edge VALUES(?,?,?,?,?)",
                (
                    _stable_id("research_edge", source_version_id, artifact_id),
                    source_version_id,
                    "contains",
                    artifact_id,
                    item.logical_path,
                ),
            )
            connection.execute(
                "INSERT INTO research_source VALUES(?,?,?,?,?,?,?,?)",
                (
                    rich_source_id,
                    source_version_id,
                    source_locator,
                    item.logical_path,
                    item.item_kind,
                    item.sha256,
                    item.size_bytes,
                    json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True),
                ),
            )
            for ordinal, content in enumerate(payload["chunks"], start=1):
                content_hash = _sha256_bytes(
                    content.encode("utf-8", "surrogatepass")
                )
                connection.execute(
                    "INSERT INTO chunk_index VALUES(?,?,?,?,?,?,?,?)",
                    (
                        _stable_id(
                            "research_chunk", artifact_id, ordinal, content_hash
                        ),
                        source_version_id,
                        artifact_id,
                        "research:text",
                        ordinal,
                        content,
                        content_hash,
                        max(1, len(content) // 4),
                    ),
                )
            ordinals: dict[str, int] = {kind: 0 for kind in RESEARCH_KIND_TO_TABLE}
            for kind, line_number, content in payload["records"]:
                if kind not in RESEARCH_KIND_TO_TABLE:
                    kind = "evidence"
                ordinals[kind] += 1
                ordinal = ordinals[kind]
                content_hash = _sha256_bytes(
                    content.encode("utf-8", "surrogatepass")
                )
                entity_id = _stable_id(
                    f"research_{kind}", rich_source_id, ordinal, content_hash
                )
                locator = f"{item.logical_path}:{line_number}"
                table = RESEARCH_KIND_TO_TABLE[kind]
                connection.execute(
                    f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)',
                    (
                        entity_id,
                        rich_source_id,
                        ordinal,
                        content,
                        content_hash,
                        line_number,
                        locator,
                    ),
                )
                connection.execute(
                    "INSERT INTO research_fts_document VALUES(?,?,?,?,?,?)",
                    (
                        _stable_id("research_fts", kind, entity_id),
                        kind,
                        entity_id,
                        f"{kind}: {item.logical_path}",
                        content,
                        locator,
                    ),
                )
        receipt_id = _stable_id("research_receipt", run_id)
        details = {
            "logical_source_id": logical_source_id,
            "source_version_id": source_version_id,
            "actor": actor,
            "provider": provider,
            "source_time_when_known": source_time,
            "candidate_truth_promotion": False,
            "append_only": True,
            "chunk_rows_added": total_chunks,
            "classified_record_rows_added": total_records,
        }
        connection.execute(
            "INSERT INTO research_receipt VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                run_id,
                source_version_id,
                inspection.sha256,
                1,
                "[]",
                json.dumps(
                    {
                        "research_source": len(item_payloads),
                        "chunk_index": total_chunks,
                        "classified_records": total_records,
                    },
                    sort_keys=True,
                ),
                '["ok"]',
                0,
                "PASS",
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                stamp,
            ),
        )
        connection.execute(
            "INSERT INTO mutation_receipt VALUES(?,?,?,?,?,?,?)",
            (
                _stable_id("research_mutation_receipt", run_id),
                "AUTOMATIC_APPEND_SERVICE",
                turn_id,
                previous_event_hash,
                event_hash,
                "RELOCKED",
                stamp,
            ),
        )
        head = connection.execute(
            "SELECT head_sequence FROM sector_head ORDER BY head_sequence DESC LIMIT 1"
        ).fetchone()
        if head:
            connection.execute(
                "UPDATE sector_head SET latest_turn_id=?,current_hash=?,"
                "next_expected_index=?,status=? WHERE head_sequence=?",
                (
                    turn_id,
                    event_hash,
                    f"PROJECT.RESEARCH.T{sequence + 1:09d}",
                    "POPULATED_RELOCK_PENDING",
                    head[0],
                ),
            )
        connection.execute(
            "INSERT INTO research_append_commit VALUES(?,?,?,?)",
            (turn_id, sequence, event_hash, stamp),
        )
        connection.commit()
        connection.close()
        connection = None
        _set_writable(database, False)

        validation = sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro", uri=True
        )
        try:
            integrity = tuple(
                str(row[0]) for row in validation.execute("PRAGMA integrity_check")
            )
            foreign_keys = tuple(validation.execute("PRAGMA foreign_key_check"))
        finally:
            validation.close()
        if integrity != ("ok",) or foreign_keys:
            raise Env15ResearchError("RESEARCH_POST_COMMIT_VALIDATION_FAILED")
        after_file_hash = _sha256_file(database)
        receipt = {
            "contract": "EVIDENCE_LANE_RESEARCH_APPEND_ONLY_SERVICE_V1",
            "status": "PASS",
            "lane_id": "research",
            "turn_id": turn_id,
            "sequence": sequence,
            "parent_turn_id": parent_turn,
            "logical_source_id": logical_source_id,
            "source_version_id": source_version_id,
            "source_sha256": inspection.sha256,
            "source_locator": source_locator,
            "source_time_when_known": source_time,
            "actor": actor,
            "provider": provider,
            "previous_event_hash": previous_event_hash,
            "event_hash": event_hash,
            "database_sha256_before": before_file_hash,
            "database_sha256_after": after_file_hash,
            "foreign_keys_on_write": True,
            "foreign_key_violation_count": 0,
            "integrity_check": list(integrity),
            "append_only_trigger_table_count": len(_APPEND_ONLY_TABLES),
            "chunk_rows_added": total_chunks,
            "classified_record_rows_added": total_records,
            "candidate_truth_promotion": False,
            "created_at": stamp,
        }
        receipt_path = root / "receipts" / f"RESEARCH_APPEND_{turn_id}.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        receipt["receipt_path"] = str(receipt_path)
        record_content_hash(
            root, "research", logical_source_id, inspection.sha256, fingerprint_probe
        )
        backup.unlink(missing_ok=True)
        return receipt
    except Exception:
        if connection is not None:
            connection.rollback()
            connection.close()
        _set_writable(database, True)
        shutil.copy2(backup, database)
        _set_writable(database, False)
        backup.unlink(missing_ok=True)
        raise


__all__ = [
    "Env15ResearchError",
    "append_env15_research_source",
]
