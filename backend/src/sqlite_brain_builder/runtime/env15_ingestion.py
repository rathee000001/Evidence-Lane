from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlite_brain_builder.runtime.canonical_lanes import get_lane, resolve_lane_id
from sqlite_brain_builder.runtime.env15_project_schema import (
    GovernedMutationReceipt,
    governed_sector_mutation,
    resolve_env15_sector,
)
from sqlite_brain_builder.runtime.env15_chat_lineage import append_env15_chat_lineage_source
from sqlite_brain_builder.runtime.office_ingest import parse_data_source, parse_docx, parse_pptx
from sqlite_brain_builder.runtime.ocr_ingest import parse_image_ocr, parse_pdf_ocr
from sqlite_brain_builder.runtime.structural_ingestion import (
    InspectionLimits,
    SourceInspection,
    SourceItem,
    inspect_source_structure,
    inspect_sqlite_database,
    read_source_item_text,
)
from sqlite_brain_builder.runtime.source_fingerprint_cache import cached_content_hash, record_content_hash
from sqlite_brain_builder.ingest.lane_contracts import contract_hash, parse_fields, validate_schema_contract


UNIVERSAL_INGESTION_LANES = {
    "artifacts", "brain_loader", "custom", "data_excel", "docs",
    "images_ocr", "pdf_ocr", "ppt", "project_engulf", "research",
    "sqlite_brain", "discussion", "analysis", "plan", "mode",
}


class Env15IngestionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Env15IngestionResult:
    status: str
    lane_id: str
    sector_id: str
    source_id: str
    source_sha256: str
    source_rows_added: int
    artifact_rows_added: int
    chunk_rows_added: int
    relation_rows_added: int
    mutation_receipt: GovernedMutationReceipt | None


def _stable_id(kind: str, *parts: Any) -> str:
    body = "\x1f".join(str(part) for part in parts).encode("utf-8", "surrogatepass")
    return f"{kind}_{hashlib.sha256(body).hexdigest()}"


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _structured_chunks(path: Path, lane_id: str) -> list[tuple[str, str]]:
    suffix = path.suffix.casefold()
    try:
        if lane_id in {"docs", "plan"} and suffix == ".docx":
            return [("doc_structure", _json(item)) for item in parse_docx(path)["chunks"]]
        if lane_id == "data_excel":
            parsed = parse_data_source(path)
            payload = parsed.get("payload")
            status_chunk = ("data_parser_status", _json({
                "source_kind": parsed.get("source_kind"), "status": parsed.get("status")
            }))
            if parsed.get("status") != "SUPPORTED":
                return [status_chunk]
            if parsed.get("source_kind") == "workbook":
                return [status_chunk, ("workbook_metadata", _json({
                    "workbook": payload.get("workbook"),
                    "sheets": [{key: value for key, value in sheet.items() if key != "cells"} for sheet in payload.get("sheets", [])],
                    "ranges": payload.get("ranges"), "named_ranges": payload.get("named_ranges"),
                    "merged_cells": payload.get("merged_cells"), "hidden_dimensions": payload.get("hidden_dimensions"),
                    "validations": payload.get("validations"), "formulas": payload.get("formulas"),
                    "formula_dependencies": payload.get("formula_dependencies"), "tables": payload.get("tables"),
                    "charts": payload.get("charts"),
                }))] + [("workbook_structure", _json(item)) for item in payload.get("chunks", [])]
            if parsed.get("source_kind") == "delimited":
                rows = payload.get("rows", [])
                return [status_chunk, ("delimited_header", _json({
                    "delimiter": payload.get("delimiter"), "header": payload.get("header", [])
                }))] + [
                    ("delimited_rows", _json(rows[offset : offset + 250]))
                    for offset in range(0, len(rows), 250)
                ]
            if parsed.get("source_kind") == "parquet":
                metadata = {key: value for key, value in payload.items() if key != "chunks"}
                return [status_chunk, ("parquet_metadata", _json(metadata))] + [
                    ("parquet_rows", _json(item)) for item in payload.get("chunks", [])
                ]
            return [status_chunk, ("data_structure", _json(payload))]
        if lane_id == "ppt" and suffix == ".pptx":
            return [("presentation_structure", _json(item)) for item in parse_pptx(path)["chunks"]]
        if lane_id == "pdf_ocr" and suffix == ".pdf":
            return [("pdf_page_structure", _json(item)) for item in parse_pdf_ocr(path)["pages"]]
        if lane_id == "images_ocr" and suffix in {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}:
            return [("image_ocr_structure", _json(parse_image_ocr(path)))]
        if lane_id == "sqlite_brain" and suffix in {".db", ".sqlite", ".sqlite3"}:
            inspected = inspect_sqlite_database(path)
            return [
                (
                    "sqlite_schema",
                    _json(
                        {
                            "object_type": item.object_type,
                            "name": item.name,
                            "table_name": item.table_name,
                            "sql": item.sql,
                            "columns": item.columns,
                            "schema_sha256": item.schema_sha256,
                        }
                    ),
                )
                for item in inspected.objects
            ]
    except Exception as exc:
        return [("parser_review_required", _json({"error": str(exc), "path": str(path)}))]
    return []


_SEMANTIC_TABLES = {
    "discussion": (
        "discussion_source", "discussion_turn", "discussion_item", "discussion_decision",
        "discussion_delta", "discussion_hard_gate", "discussion_artifact_reference",
        "discussion_next_action",
    ),
    "analysis": (
        "analysis_source", "analysis_claim", "analysis_supporting_evidence", "analysis_risk",
        "analysis_alternative", "analysis_open_question", "analysis_accepted_decision",
        "analysis_blocked_item",
    ),
    "plan": (
        "plan_source", "plan_phase", "plan_milestone", "plan_task", "plan_dependency",
        "plan_owner", "plan_status", "plan_acceptance_criteria", "plan_blocker",
        "plan_next_action",
    ),
    "mode": (
        "mode_source", "mode_rule", "mode_scope", "mode_gate", "mode_allowed_action",
        "mode_blocked_action", "mode_trigger", "mode_response_template", "mode_priority",
        "mode_supersede_ledger",
    ),
}


def _semantic_lines(chunks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for chunk in chunks:
        for raw in str(chunk.get("content") or "").splitlines():
            line = raw.strip().lstrip("-*# ").strip()
            if line:
                lines.append(line)
    return lines


def _marked(line: str, *markers: str) -> str | None:
    folded = line.casefold()
    for marker in markers:
        prefix = marker.casefold() + ":"
        if folded.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _write_semantic_projection(
    connection: sqlite3.Connection,
    lane_id: str,
    source_id: str,
    chunks: list[dict[str, Any]],
) -> dict[str, int]:
    tables = _SEMANTIC_TABLES.get(lane_id)
    if not tables:
        return {}
    required_columns = {
        "entity_id": "TEXT",
        "source_id": "TEXT NOT NULL",
        "statement": "TEXT NOT NULL",
        "relation_target": "TEXT",
        "state": "TEXT",
        "evidence_ref": "TEXT NOT NULL",
    }
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {quoted}("
            "entity_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, statement TEXT NOT NULL, "
            "relation_target TEXT, state TEXT, evidence_ref TEXT NOT NULL)"
        )
        existing_columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")}
        for column, column_type in required_columns.items():
            if column not in existing_columns:
                connection.execute(f'ALTER TABLE {quoted} ADD COLUMN "{column}" {column_type}')
        connection.execute(f"DELETE FROM {quoted} WHERE source_id=?", (source_id,))

    records: dict[str, list[tuple[str, str, str, str, str, str]]] = {table: [] for table in tables}
    semantic_lines = _semantic_lines(chunks)
    source_table = f"{lane_id}_source"
    source_statement = "\n".join(semantic_lines)[:20000] or f"{lane_id} source"
    records[source_table].append((
        _stable_id("semantic", lane_id, source_table, source_id, source_statement),
        source_id,
        source_statement,
        "",
        "SOURCE_REGISTERED",
        "source:registered",
    ))
    if lane_id == "discussion":
        records["discussion_turn"].append((
            _stable_id("semantic", lane_id, "discussion_turn", source_id, source_statement),
            source_id,
            source_statement,
            "",
            "TURN_RECORDED",
            "source:turn",
        ))
    current_claim = ""
    for ordinal, line in enumerate(semantic_lines, start=1):
        table = ""
        statement: str | None = None
        target = ""
        state = "RECORDED"
        if lane_id == "discussion":
            item_statement = _marked(line, "item")
            if item_statement is not None:
                table, statement, state = "discussion_item", item_statement, "DISCUSSION_ITEM"
            else:
                statement = _marked(line, "decision", "accepted decision")
            if statement is not None:
                if not table:
                    table = "discussion_decision"
                    state = "ACCEPTED"
            else:
                statement = _marked(line, "delta", "change")
                if statement is not None:
                    table = "discussion_delta"
                    state = "PROPOSED_CHANGE"
                else:
                    statement = _marked(line, "gate", "hard gate", "blocked")
                    if statement is not None:
                        table = "discussion_hard_gate"
                        state = "BLOCKING"
                    else:
                        statement = _marked(line, "artifact", "artifact reference")
                        if statement is not None:
                            table = "discussion_artifact_reference"
                            state = "REFERENCED"
                        else:
                            statement = _marked(line, "next action", "action")
                            if statement is not None:
                                table = "discussion_next_action"
                                state = "NEXT_ACTION"
        elif lane_id == "analysis":
            claim = _marked(line, "claim")
            if claim is not None:
                current_claim = claim
                table, statement, state = "analysis_claim", claim, "CLAIM"
            else:
                statement = _marked(line, "evidence", "supporting evidence")
                if statement is not None:
                    target = current_claim
                    table = "analysis_supporting_evidence"
                    state = "SUPPORTS_CLAIM"
                else:
                    statement = _marked(line, "risk")
                    if statement is not None:
                        target = current_claim
                        table = "analysis_risk"
                        state = "OPEN_RISK"
                    else:
                        statement = _marked(line, "alternative")
                        if statement is not None:
                            target = current_claim
                            table = "analysis_alternative"
                            state = "CONSIDERED"
                        else:
                            statement = _marked(line, "question", "open question")
                            if statement is not None:
                                table, state = "analysis_open_question", "OPEN"
                            else:
                                statement = _marked(line, "accepted decision", "decision")
                                if statement is not None:
                                    table, state = "analysis_accepted_decision", "ACCEPTED"
                                else:
                                    statement = _marked(line, "blocked", "blocked item")
                                    if statement is not None:
                                        table, state = "analysis_blocked_item", "BLOCKED"
        elif lane_id == "plan":
            for marker, candidate, candidate_state in (
                ("phase", "plan_phase", "ACTIVE_PHASE"),
                ("milestone", "plan_milestone", "PLANNED"),
                ("task", "plan_task", "PLANNED_TASK"),
                ("dependency", "plan_dependency", "REQUIRED"),
                ("owner", "plan_owner", "ASSIGNED"),
                ("status", "plan_status", "STATUS_TRANSITION"),
                ("acceptance", "plan_acceptance_criteria", "ACCEPTANCE_CRITERION"),
                ("acceptance criteria", "plan_acceptance_criteria", "ACCEPTANCE_CRITERION"),
                ("blocker", "plan_blocker", "BLOCKING"),
                ("next action", "plan_next_action", "NEXT_ACTION"),
            ):
                statement = _marked(line, marker)
                if statement is not None:
                    table, state = candidate, candidate_state
                    if marker == "status" and "->" in statement:
                        target = statement.split("->", 1)[1].strip()
                    break
        elif lane_id == "mode":
            for marker, candidate, candidate_state in (
                ("rule", "mode_rule", "ENFORCED_RULE"),
                ("scope", "mode_scope", "IN_SCOPE"),
                ("gate", "mode_gate", "ENFORCED"),
                ("allow", "mode_allowed_action", "ALLOWED"),
                ("allowed", "mode_allowed_action", "ALLOWED"),
                ("block", "mode_blocked_action", "BLOCKED"),
                ("blocked", "mode_blocked_action", "BLOCKED"),
                ("trigger", "mode_trigger", "TRIGGER"),
                ("response template", "mode_response_template", "TEMPLATE"),
                ("priority", "mode_priority", "PRIORITY"),
                ("supersede", "mode_supersede_ledger", "SUPERSEDES"),
                ("supersedes", "mode_supersede_ledger", "SUPERSEDES"),
            ):
                statement = _marked(line, marker)
                if statement is not None:
                    table, state = candidate, candidate_state
                    if candidate == "mode_supersede_ledger" and "->" in statement:
                        target = statement.split("->", 1)[0].strip()
                    break
        if not table or statement is None or not statement.strip():
            continue
        evidence_ref = f"chunk:{ordinal}"
        entity_id = _stable_id("semantic", lane_id, table, source_id, ordinal, statement, target, state)
        records[table].append((entity_id, source_id, statement, target, state, evidence_ref))

    for table, rows in records.items():
        if rows:
            quoted = '"' + table.replace('"', '""') + '"'
            connection.executemany(
                f"INSERT INTO {quoted}(entity_id,source_id,statement,relation_target,state,evidence_ref) "
                "VALUES(?,?,?,?,?,?)",
                rows,
            )
    return {f"{table}_rows": len(rows) for table, rows in records.items()}


def _projection_table_kind(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT type,sql FROM sqlite_schema WHERE name=? AND type IN ('table','view')",
        (table,),
    ).fetchone()
    if row is None:
        return "missing"
    sql = str(row[1] or "").casefold()
    if "virtual table" in sql and "fts5" in sql:
        return "fts"
    return str(row[0])


def _write_user_schema_projection(
    connection: sqlite3.Connection,
    *,
    lane_id: str,
    source_id: str,
    source_locator: str,
    display_name: str,
    source_sha256: str,
    schema_text: str,
    schema_identifiers: list[str],
    schema_version: int,
    schema_origin: str,
    chunks: list[dict[str, Any]],
) -> dict[str, int]:
    schema_sha256 = contract_hash(schema_text)
    contract_id = _stable_id("lane_schema_contract", lane_id, source_id, schema_version, schema_sha256)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS lane_schema_contract("
        "contract_id TEXT PRIMARY KEY,lane_id TEXT NOT NULL,source_id TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL,schema_sha256 TEXT NOT NULL,identifiers_json TEXT NOT NULL,"
        "raw_contract TEXT NOT NULL,origin TEXT NOT NULL,applied_at TEXT NOT NULL,"
        "UNIQUE(lane_id,source_id,schema_version))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS lane_schema_mapping("
        "mapping_id TEXT PRIMARY KEY,contract_id TEXT NOT NULL,table_name TEXT NOT NULL,"
        "projection_role TEXT NOT NULL,projection_status TEXT NOT NULL,created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT OR REPLACE INTO lane_schema_contract VALUES(?,?,?,?,?,?,?,?,?)",
        (
            contract_id,
            lane_id,
            source_id,
            schema_version,
            schema_sha256,
            _json(schema_identifiers),
            schema_text,
            schema_origin,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
        ),
    )
    connection.execute("DELETE FROM lane_schema_mapping WHERE contract_id=?", (contract_id,))
    generic_columns = {"id", "source_id", "name", "path", "value", "metadata_json", "created_at"}
    projected_tables = 0
    projected_rows = 0
    non_fts_ordinal = 0
    for table in schema_identifiers:
        quoted = '"' + table.replace('"', '""') + '"'
        wants_fts = table.casefold().endswith("_fts")
        kind = _projection_table_kind(connection, table)
        role = "fts" if wants_fts else "source" if non_fts_ordinal == 0 else "chunk"
        status = "CREATED_USER_PROJECTION"
        compatible = True
        if kind == "missing":
            if wants_fts:
                connection.execute(
                    f"CREATE VIRTUAL TABLE {quoted} USING fts5(entity_id UNINDEXED,source_id UNINDEXED,text)"
                )
            else:
                connection.execute(
                    f"CREATE TABLE {quoted}(id TEXT PRIMARY KEY,source_id TEXT,name TEXT,path TEXT,"
                    "value TEXT,metadata_json TEXT,created_at TEXT)"
                )
        else:
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")}
            required = {"entity_id", "source_id", "text"} if wants_fts else generic_columns
            compatible = required.issubset(columns)
            status = "REUSED_COMPATIBLE_TABLE" if compatible else "PRESERVED_INCOMPATIBLE_EXISTING_TABLE"
        if compatible:
            if wants_fts:
                connection.execute(f"DELETE FROM {quoted} WHERE source_id=?", (source_id,))
                rows = [
                    (
                        _stable_id("schema_fts", contract_id, item["chunk_id"]),
                        source_id,
                        str(item.get("content") or ""),
                    )
                    for item in chunks
                    if str(item.get("content") or "")
                ]
                if rows:
                    connection.executemany(
                        f"INSERT INTO {quoted}(entity_id,source_id,text) VALUES(?,?,?)",
                        rows,
                    )
                projected_rows += len(rows)
            else:
                connection.execute(f"DELETE FROM {quoted} WHERE source_id=?", (source_id,))
                if role == "source":
                    rows = [
                        (
                            _stable_id("schema_row", contract_id, table, source_id),
                            source_id,
                            display_name,
                            source_locator,
                            source_sha256,
                            _json({"lane_id": lane_id, "projection_role": role, "schema_version": schema_version}),
                            time.strftime("%Y-%m-%dT%H:%M:%S"),
                        )
                    ]
                else:
                    rows = [
                        (
                            _stable_id("schema_row", contract_id, table, item["chunk_id"]),
                            source_id,
                            str(item.get("chunk_type") or "chunk"),
                            source_locator,
                            str(item.get("content") or ""),
                            _json({"chunk_id": item["chunk_id"], "ordinal": item.get("ordinal", 0), "schema_version": schema_version}),
                            time.strftime("%Y-%m-%dT%H:%M:%S"),
                        )
                        for item in chunks
                    ]
                if rows:
                    connection.executemany(
                        f"INSERT INTO {quoted}(id,source_id,name,path,value,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)",
                        rows,
                    )
                projected_rows += len(rows)
                non_fts_ordinal += 1
            projected_tables += 1
        elif not wants_fts:
            non_fts_ordinal += 1
        mapping_id = _stable_id("schema_mapping", contract_id, table)
        connection.execute(
            "INSERT INTO lane_schema_mapping VALUES(?,?,?,?,?,?)",
            (
                mapping_id,
                contract_id,
                table,
                role,
                status,
                time.strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )
    return {
        "lane_schema_contract_rows": 1,
        "lane_schema_mapping_rows": len(schema_identifiers),
        "lane_schema_projected_tables": projected_tables,
        "lane_schema_projected_rows": projected_rows,
    }


def ingest_env15_universal_source(
    brain_root: str | Path,
    lane_alias: str,
    source: dict[str, Any],
    *,
    actor: str = "Evidence OS SQLite Builder",
    reason: str = "explicit source intake",
    limits: InspectionLimits | None = None,
    mutation_extension: Callable[[sqlite3.Connection], dict[str, Any] | None] | None = None,
) -> Env15IngestionResult:
    lane_id = resolve_lane_id(lane_alias, scope="any")
    if lane_id not in UNIVERSAL_INGESTION_LANES:
        raise Env15IngestionError(f"ENV15_SPECIALIZED_INGESTER_REQUIRED:{lane_id}")
    source_path_text = str(source.get("path") or "").strip()
    source_text = str(source.get("text") or "")
    raw_contract = source.get("schema_contract") or source.get("schemaContract") or ""
    schema_text = "\n".join(str(value) for value in raw_contract) if isinstance(raw_contract, (list, tuple)) else str(raw_contract)
    schema_origin = str(source.get("schema_origin") or source.get("schemaOrigin") or "")
    schema_version = int(source.get("schema_version") or source.get("schemaVersion") or 0)
    schema_applies = lane_id == "custom" or schema_origin == "user_override"
    schema_identifiers: list[str] = []
    schema_contract_id = ""
    if schema_applies:
        if not schema_text.strip():
            raise Env15IngestionError("CUSTOM_SCHEMA_CONTRACT_REQUIRED" if lane_id == "custom" else "LANE_SCHEMA_CONTRACT_REQUIRED")
        safe, reason = validate_schema_contract(schema_text)
        if not safe:
            raise Env15IngestionError(reason)
        schema_identifiers = parse_fields(schema_text)
        if not schema_identifiers or len(schema_identifiers) != len([line for line in schema_text.splitlines() if line.strip()]):
            raise Env15IngestionError("CUSTOM_SCHEMA_CONTRACT_HAS_NO_VALID_FIELDS" if lane_id == "custom" else "LANE_SCHEMA_CONTRACT_HAS_INVALID_IDENTIFIERS")
        schema_text = "\n".join(schema_identifiers)
    if not source_path_text and not source_text:
        raise Env15IngestionError(f"ENV15_SOURCE_PATH_OR_TEXT_REQUIRED:{lane_id}")
    limits = limits or InspectionLimits()
    inline_text_by_path: dict[str, str] = {}
    if source_path_text:
        source_path = Path(source_path_text).expanduser().resolve(strict=True)
        source_locator = str(source_path)
        cached_hash, fingerprint_probe = cached_content_hash(
            brain_root, lane_id, str(source.get("source_id") or _stable_id("source", lane_id, str(source_path).casefold())), source_path
        )
        inspection = None
    else:
        data = source_text.encode("utf-8", "surrogatepass")
        digest = hashlib.sha256(data).hexdigest()
        display = str(source.get("display_name") or source.get("displayName") or f"{lane_id} pasted text")
        safe_display = "".join(character if character.isalnum() or character in "._-" else "_" for character in display).strip("._") or "pasted_text"
        logical_path = f"inline/{safe_display}.txt"
        source_locator = f"inline:{digest}"
        source_path = Path(source_locator)
        inline_text_by_path[logical_path] = source_text
        inspection = SourceInspection(
            source_path=source_path,
            source_kind="inline_text",
            display_name=display,
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
        cached_hash = digest
        fingerprint_probe = {
            "source_path": source_locator,
            "stat_fingerprint": digest,
            "file_count": 1,
            "total_bytes": len(data),
        }
    source_id = str(source.get("source_id") or _stable_id("source", lane_id, source_locator.casefold()))
    if schema_applies:
        schema_contract_id = _stable_id("lane_schema_contract", lane_id, source_id, schema_version, contract_hash(schema_text))
        # The filesystem stat cache does not include an external schema
        # contract. Reinspect governed schema sources so a schema-only change cannot be
        # mistaken for an unchanged ingestion.
        cached_hash = None
    sector_id, sector_path = resolve_env15_sector(brain_root, lane_id)

    lane_definition = get_lane(lane_id)
    read_only = sqlite3.connect(f"file:{sector_path.as_posix()}?mode=ro", uri=True)
    try:
        existing = read_only.execute(
            "SELECT sha256,availability_state FROM source_registry WHERE source_id=?", (source_id,)
        ).fetchone()
        has_source_version = read_only.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='source_version'"
        ).fetchone()
        version_current = bool(
            has_source_version
            and read_only.execute(
                "SELECT 1 FROM source_version WHERE source_id=? AND parser_id=? "
                "AND chunker_version=? AND availability_state='AVAILABLE' LIMIT 1",
                (source_id, lane_definition.parser_id, lane_definition.chunker_version),
            ).fetchone()
        )
    finally:
        read_only.close()
    if (
        cached_hash
        and existing
        and existing[0] == cached_hash
        and existing[1] == "AVAILABLE"
        and version_current
    ):
        return Env15IngestionResult(
            "SKIPPED_UNCHANGED", lane_id, sector_id, source_id, cached_hash,
            0, 0, 0, 0, None,
        )

    inspection = inspection or inspect_source_structure(source_path, limits=limits)
    source_sha256 = inspection.sha256
    if schema_applies:
        source_sha256 = hashlib.sha256(
            f"{inspection.sha256}\x1f{contract_hash(schema_text)}\x1f{schema_version}".encode("utf-8")
        ).hexdigest()
    if (
        existing
        and existing[0] == source_sha256
        and existing[1] == "AVAILABLE"
        and version_current
    ):
        record_content_hash(brain_root, lane_id, source_id, source_sha256, fingerprint_probe)
        return Env15IngestionResult(
            "SKIPPED_UNCHANGED", lane_id, sector_id, source_id, source_sha256,
            0, 0, 0, 0, None,
        )

    artifacts: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for item in inspection.items:
        artifact_id = _stable_id("artifact", source_id, item.logical_path, item.sha256)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": item.item_kind,
                "canonical_path": item.logical_path,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "review_state": "REVIEW_REQUIRED" if item.is_symlink else "INDEXED",
            }
        )
        relations.append(
            {
                "edge_id": _stable_id("edge", source_id, artifact_id, "contains"),
                "source_node": source_id,
                "relation_type": "contains",
                "target_node": artifact_id,
                "evidence_ref": item.logical_path,
            }
        )
        item_chunks: list[tuple[str, str]] = []
        structural_suffix = Path(item.logical_path).suffix.casefold()
        prefer_structure = (
            (lane_id in {"docs", "plan"} and structural_suffix == ".docx")
            or (lane_id == "data_excel" and structural_suffix in {".csv", ".json", ".jsonl", ".parquet", ".tsv", ".xls", ".xlsm", ".xlsx"})
            or (lane_id == "ppt" and structural_suffix == ".pptx")
            or (lane_id == "pdf_ocr" and structural_suffix == ".pdf")
            or (lane_id == "images_ocr" and structural_suffix in {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
        )
        if prefer_structure and item.physical_path and item.physical_path.is_file():
            item_chunks = _structured_chunks(item.physical_path, lane_id)
        text = inline_text_by_path.get(item.logical_path)
        if text is None and not item_chunks:
            text = read_source_item_text(item, limits=limits)
        if text is not None:
            lines = text.splitlines()
            item_chunks = [
                ("text", "\n".join(lines[offset : offset + limits.text_chunk_lines]))
                for offset in range(0, len(lines), limits.text_chunk_lines)
                if "\n".join(lines[offset : offset + limits.text_chunk_lines]).strip()
            ]
        elif not item_chunks and item.physical_path and item.physical_path.is_file():
            item_chunks = _structured_chunks(item.physical_path, lane_id)
        if not item_chunks:
            item_chunks = [
                (
                    "metadata_only",
                    _json(
                        {
                            "lane_id": lane_id,
                            "logical_path": item.logical_path,
                            "item_kind": item.item_kind,
                            "size_bytes": item.size_bytes,
                            "sha256": item.sha256,
                            "metadata": dict(item.metadata),
                        }
                    ),
                )
            ]
        for ordinal, (chunk_type, content) in enumerate(item_chunks, start=1):
            content_hash = hashlib.sha256(content.encode("utf-8", "surrogatepass")).hexdigest()
            chunks.append(
                {
                    "chunk_id": _stable_id("chunk", artifact_id, chunk_type, ordinal, content_hash),
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "chunk_type": f"{lane_id}:{chunk_type}",
                    "ordinal": ordinal,
                    "content": content,
                    "content_sha256": content_hash,
                    "token_estimate": max(1, len(content) // 4),
                }
            )

    if lane_id == "custom":
        schema_content = _json(
            {
                "contract_sha256": contract_hash(schema_text),
                "fields": schema_identifiers,
                "schema_contract": schema_text,
            }
        )
        schema_hash = hashlib.sha256(schema_content.encode("utf-8", "surrogatepass")).hexdigest()
        chunks.append(
            {
                "chunk_id": _stable_id("chunk", schema_contract_id, "schema_contract", schema_hash),
                "source_id": source_id,
                "artifact_id": artifacts[0]["artifact_id"],
                "chunk_type": "custom:schema_contract",
                "ordinal": 0,
                "content": schema_content,
                "content_sha256": schema_hash,
                "token_estimate": max(1, len(schema_content) // 4),
            }
        )
        relations.append(
            {
                "edge_id": _stable_id("edge", source_id, schema_contract_id, "governed_by_schema"),
                "source_node": source_id,
                "relation_type": "governed_by_schema",
                "target_node": schema_contract_id,
                "evidence_ref": f"sha256:{contract_hash(schema_text)}",
            }
        )

    turn_id = str(source.get("turn_id") or _stable_id("turn", source_id, source_sha256)[:48])
    fts_table = get_lane(lane_id).fts_table
    quoted_fts = '"' + fts_table.replace('"', '""') + '"'

    def mutate(connection: sqlite3.Connection) -> dict[str, int]:
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {quoted_fts} USING fts5(entity_id UNINDEXED, source_id UNINDEXED, text)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS source_version("
            "source_version_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,"
            "source_locator TEXT NOT NULL,size_bytes INTEGER NOT NULL,turn_id TEXT NOT NULL,"
            "parser_id TEXT NOT NULL,chunker_version TEXT NOT NULL,availability_state TEXT NOT NULL,"
            "created_at TEXT NOT NULL,UNIQUE(source_id,source_sha256,parser_id,chunker_version))"
        )
        connection.execute(f"DELETE FROM {quoted_fts} WHERE source_id=?", (source_id,))
        if lane_id == "custom":
            connection.execute(
                "CREATE TABLE IF NOT EXISTS custom_schema_contract("
                "contract_id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, contract_sha256 TEXT NOT NULL, "
                "fields_json TEXT NOT NULL, raw_contract TEXT NOT NULL, parser_version TEXT NOT NULL)"
            )
            connection.execute("DELETE FROM custom_schema_contract WHERE source_id=?", (source_id,))
        prior_artifacts = [row[0] for row in connection.execute(
            "SELECT artifact_id FROM artifact_registry WHERE source_id=?", (source_id,)
        )]
        for artifact_id in prior_artifacts:
            connection.execute("DELETE FROM chunk_index WHERE artifact_id=?", (artifact_id,))
            connection.execute(
                "DELETE FROM relation_edge WHERE source_node=? OR target_node=?", (artifact_id, artifact_id)
            )
        connection.execute("DELETE FROM artifact_registry WHERE source_id=?", (source_id,))
        connection.execute("DELETE FROM relation_edge WHERE source_node=? OR target_node=?", (source_id, source_id))
        connection.execute("DELETE FROM source_registry WHERE source_id=?", (source_id,))
        connection.execute(
            "INSERT INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
            (
                source_id, lane_id, inspection.display_name, source_locator, source_sha256,
                inspection.size_bytes, "AVAILABLE", turn_id,
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO source_version VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                _stable_id(
                    "source_version",
                    source_id,
                    source_sha256,
                    lane_definition.parser_id,
                    lane_definition.chunker_version,
                ),
                source_id,
                source_sha256,
                source_locator,
                inspection.size_bytes,
                turn_id,
                lane_definition.parser_id,
                lane_definition.chunker_version,
                "AVAILABLE",
                time.strftime("%Y-%m-%dT%H:%M:%S"),
            ),
        )
        connection.executemany(
            "INSERT INTO artifact_registry VALUES(?,?,?,?,?,?,?)",
            [
                (
                    item["artifact_id"], source_id, item["artifact_type"], item["canonical_path"],
                    item["sha256"], item["size_bytes"], item["review_state"],
                )
                for item in artifacts
            ],
        )
        connection.executemany(
            "INSERT INTO chunk_index VALUES(?,?,?,?,?,?,?,?)",
            [tuple(item[key] for key in (
                "chunk_id", "source_id", "artifact_id", "chunk_type", "ordinal",
                "content", "content_sha256", "token_estimate",
            )) for item in chunks],
        )
        connection.executemany(
            f"INSERT INTO {quoted_fts}(entity_id,source_id,text) VALUES(?,?,?)",
            [(item["chunk_id"], source_id, item["content"]) for item in chunks],
        )
        connection.executemany(
            "INSERT INTO relation_edge VALUES(?,?,?,?,?)",
            [tuple(item[key] for key in (
                "edge_id", "source_node", "relation_type", "target_node", "evidence_ref",
            )) for item in relations],
        )
        if lane_id == "custom":
            connection.execute(
                "INSERT INTO custom_schema_contract VALUES(?,?,?,?,?,?)",
                (
                    schema_contract_id, source_id, contract_hash(schema_text),
                    _json(schema_identifiers), schema_text, "schema_first_v1",
                ),
            )
        schema_projection_counts = (
            _write_user_schema_projection(
                connection,
                lane_id=lane_id,
                source_id=source_id,
                source_locator=source_locator,
                display_name=inspection.display_name,
                source_sha256=source_sha256,
                schema_text=schema_text,
                schema_identifiers=schema_identifiers,
                schema_version=schema_version,
                schema_origin=schema_origin or ("source_contract" if lane_id == "custom" else "user_override"),
                chunks=chunks,
            )
            if schema_applies
            else {}
        )
        semantic_counts = _write_semantic_projection(connection, lane_id, source_id, chunks)
        extension_counts = dict(mutation_extension(connection) or {}) if mutation_extension else {}
        head = connection.execute("SELECT head_sequence FROM sector_head ORDER BY head_sequence DESC LIMIT 1").fetchone()
        connection.execute(
            "UPDATE sector_head SET latest_turn_id=?,current_hash=?,next_expected_index=?,status=? WHERE head_sequence=?",
            (turn_id, source_sha256, f"PROJECT.{sector_id.upper()}.NEXT", "POPULATED_RELOCK_PENDING", head[0]),
        )
        return {
            "source_rows_added": 1,
            "source_version_rows_added": 1,
            "artifact_rows_added": len(artifacts),
            "chunk_rows_added": len(chunks),
            "relation_rows_added": len(relations),
            "fts_rows_added": len(chunks),
            "custom_schema_contract_rows": 1 if lane_id == "custom" else 0,
            **schema_projection_counts,
            **semantic_counts,
            **extension_counts,
        }

    receipt = governed_sector_mutation(
        brain_root, lane_id, actor=actor, reason=reason, turn_id=turn_id, mutate=mutate
    )
    record_content_hash(brain_root, lane_id, source_id, source_sha256, fingerprint_probe)
    return Env15IngestionResult(
        "PASS", lane_id, sector_id, source_id, source_sha256,
        1, len(artifacts), len(chunks), len(relations), receipt,
    )


def set_env15_source_activity(
    brain_root: str | Path,
    lane_alias: str,
    source_id: str,
    *,
    active: bool,
) -> dict[str, Any]:
    lane_id = resolve_lane_id(lane_alias, scope="any")
    desired = "AVAILABLE" if active else "INACTIVE"
    if lane_id == "chat_lineage":
        return append_env15_chat_lineage_source(
            brain_root,
            {
                "source_id": source_id,
                "text": f"SOURCE_ACTIVITY {source_id} {desired}",
                "assistant_response": f"Source activity recorded as {desired}.",
                "visible_reasoning_summary": "Visible source activity audit event.",
                "idempotency_key": f"source_activity:{source_id}:{desired}",
            },
        )
    sector_id, database = resolve_env15_sector(brain_root, lane_id)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT availability_state FROM source_registry WHERE source_id=?", (source_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return {"status": "SOURCE_NOT_INDEXED", "lane_id": lane_id, "sector_id": sector_id, "source_id": source_id}
    if row[0] == desired:
        return {"status": "SKIPPED_UNCHANGED", "lane_id": lane_id, "sector_id": sector_id, "source_id": source_id}
    turn_id = _stable_id("activity", lane_id, source_id, desired)[:48]
    fts_table = get_lane(lane_id).fts_table
    quoted_fts = '"' + fts_table.replace('"', '""') + '"'

    def mutate(connection: sqlite3.Connection) -> dict[str, Any]:
        connection.execute(
            "UPDATE source_registry SET availability_state=?,created_turn=? WHERE source_id=?",
            (desired, turn_id, source_id),
        )
        if not active:
            fts_exists = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (fts_table,)
            ).fetchone()
            if fts_exists:
                if lane_id in {"github_code", "local_code"}:
                    connection.execute(
                        f"DELETE FROM {quoted_fts} WHERE chunk_id IN ("
                        "SELECT chunk_id FROM code_chunk WHERE file_id IN ("
                        "SELECT file_id FROM code_file_snapshot WHERE source_id=?))",
                        (source_id,),
                    )
                else:
                    connection.execute(f"DELETE FROM {quoted_fts} WHERE source_id=?", (source_id,))
        return {"source_id": source_id, "availability_state": desired}

    receipt = governed_sector_mutation(
        brain_root,
        lane_id,
        actor="Evidence OS SQLite Builder",
        reason=f"set source {source_id} activity to {desired}",
        turn_id=turn_id,
        mutate=mutate,
    )
    return {
        "status": "PASS", "lane_id": lane_id, "sector_id": sector_id,
        "source_id": source_id, "availability_state": desired, "receipt": receipt,
    }


__all__ = [
    "Env15IngestionError",
    "Env15IngestionResult",
    "UNIVERSAL_INGESTION_LANES",
    "ingest_env15_universal_source",
    "set_env15_source_activity",
]
