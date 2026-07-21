from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.office_ingest import parse_docx
from sqlite_brain_builder.runtime.source_fingerprint_cache import cached_content_hash, record_content_hash


class Env15ChatLineageError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8", "surrogatepass"))


def _set_writable(path: Path, writable: bool) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _markdown_cell(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>").strip()


def _docx_to_deterministic_markdown(source_path: Path) -> tuple[str, dict[str, Any]]:
    parsed = parse_docx(source_path)
    blocks: list[str] = []
    for paragraph in parsed["paragraphs"]:
        text = str(paragraph.get("text") or "").strip()
        if not text:
            continue
        heading_level = paragraph.get("heading_level")
        if isinstance(heading_level, int) and heading_level > 0:
            blocks.append(f"{'#' * min(heading_level, 6)} {text}")
        elif paragraph.get("is_list"):
            blocks.append(f"- {text}")
        else:
            blocks.append(text)
    for table in parsed["tables"]:
        rows = [
            [_markdown_cell(cell.get("text")) for cell in row.get("cells", [])]
            for row in table.get("rows", [])
        ]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        normalized_rows = [row + [""] * (width - len(row)) for row in rows]
        rendered = [
            "| " + " | ".join(normalized_rows[0]) + " |",
            "| " + " | ".join("---" for _ in range(width)) + " |",
            *("| " + " | ".join(row) + " |" for row in normalized_rows[1:]),
        ]
        blocks.append("\n".join(rendered))
    if str(parsed.get("footnotes_text") or "").strip():
        blocks.append("## Footnotes\n\n" + str(parsed["footnotes_text"]).strip())
    if str(parsed.get("comments_text") or "").strip():
        blocks.append("## Comments\n\n" + str(parsed["comments_text"]).strip())
    markdown = "\n\n".join(blocks).strip() + "\n"
    original = source_path.read_bytes()
    normalized = markdown.encode("utf-8")
    return markdown, {
        "source_id": "",
        "source_format": "DOCX",
        "normalized_format": "MARKDOWN",
        "original_path": str(source_path),
        "original_sha256": _sha256_bytes(original),
        "normalized_sha256": _sha256_bytes(normalized),
        "parser_id": "docx_structural_v1_to_deterministic_markdown_v1",
        "original_size_bytes": len(original),
        "normalized_size_bytes": len(normalized),
    }


def _normalization_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_normalization_receipt(
          normalization_id TEXT PRIMARY KEY,
          turn_id TEXT NOT NULL UNIQUE REFERENCES turn_prepare(turn_id),
          source_id TEXT NOT NULL,
          source_path TEXT NOT NULL,
          source_format TEXT NOT NULL,
          normalized_format TEXT NOT NULL,
          original_sha256 TEXT NOT NULL,
          normalized_sha256 TEXT NOT NULL,
          parser_id TEXT NOT NULL,
          original_size_bytes INTEGER NOT NULL,
          normalized_size_bytes INTEGER NOT NULL,
          receipt_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS source_normalization_receipt_no_update "
        "BEFORE UPDATE ON source_normalization_receipt BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_UPDATE_BLOCKED'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS source_normalization_receipt_no_delete "
        "BEFORE DELETE ON source_normalization_receipt BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_DELETE_BLOCKED'); END"
    )


def _lineage_cursor_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS lineage_source_cursor(
          source_id TEXT PRIMARY KEY,
          source_path TEXT NOT NULL,
          full_prompt_sha256 TEXT NOT NULL,
          full_prompt_text TEXT NOT NULL,
          full_response_sha256 TEXT NOT NULL,
          full_attachment_set_sha256 TEXT NOT NULL,
          source_bytes_sha256 TEXT NOT NULL,
          source_size_bytes INTEGER NOT NULL,
          last_turn_id TEXT NOT NULL,
          revision_sequence INTEGER NOT NULL,
          actor TEXT NOT NULL,
          provider TEXT NOT NULL,
          source_time_when_known TEXT,
          ingested_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lineage_source_revision(
          revision_id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL,
          revision_sequence INTEGER NOT NULL,
          prior_prompt_sha256 TEXT,
          current_prompt_sha256 TEXT NOT NULL,
          appended_prompt_sha256 TEXT NOT NULL,
          prefix_character_count INTEGER NOT NULL,
          appended_character_count INTEGER NOT NULL,
          full_response_sha256 TEXT NOT NULL,
          turn_id TEXT NOT NULL UNIQUE,
          actor TEXT NOT NULL,
          provider TEXT NOT NULL,
          source_time_when_known TEXT,
          ingested_at TEXT NOT NULL,
          append_classification TEXT NOT NULL,
          source_classification TEXT NOT NULL,
          task_window_id TEXT,
          source_event_range TEXT,
          UNIQUE(source_id,revision_sequence)
        );
        CREATE TRIGGER IF NOT EXISTS lineage_source_revision_no_update
        BEFORE UPDATE ON lineage_source_revision
        BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_UPDATE_BLOCKED'); END;
        CREATE TRIGGER IF NOT EXISTS lineage_source_revision_no_delete
        BEFORE DELETE ON lineage_source_revision
        BEGIN SELECT RAISE(ABORT,'APPEND_ONLY_DELETE_BLOCKED'); END;
        """
    )
    revision_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(lineage_source_revision)")
    }
    if "append_classification" not in revision_columns:
        connection.execute(
            "ALTER TABLE lineage_source_revision ADD COLUMN append_classification "
            "TEXT NOT NULL DEFAULT 'INITIAL_FULL_SOURCE'"
        )
    for column, declaration in (
        ("source_classification", "TEXT NOT NULL DEFAULT 'CURRENT_CHAT'"),
        ("task_window_id", "TEXT"),
        ("source_event_range", "TEXT"),
    ):
        if column not in revision_columns:
            connection.execute(
                f"ALTER TABLE lineage_source_revision ADD COLUMN {column} {declaration}"
            )
    cursor_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(lineage_source_cursor)")
    }
    if "full_attachment_set_sha256" not in cursor_columns:
        connection.execute(
            "ALTER TABLE lineage_source_cursor ADD COLUMN full_attachment_set_sha256 "
            "TEXT NOT NULL DEFAULT ''"
        )


def _attachment_records(source: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    raw_links = source.get("attachments") or source.get("links") or []
    if isinstance(raw_links, (str, Path, dict)):
        raw_links = [raw_links]
    records: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_links):
        item = dict(raw) if isinstance(raw, dict) else {"path": str(raw)}
        locator = str(
            item.get("path")
            or item.get("file_path")
            or item.get("uri")
            or item.get("external_display_uri")
            or ""
        ).strip()
        local_path: Path | None = None
        if locator:
            candidate = Path(locator).expanduser()
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                resolved = None
            if resolved is not None and resolved.is_file():
                local_path = resolved
        if local_path is not None:
            data = local_path.read_bytes()
            size_bytes: int | None = len(data)
            sha256: str | None = _sha256_bytes(data)
            original_name = local_path.name
            external_uri = str(item.get("external_display_uri") or local_path)
            availability = "AVAILABLE"
            hash_status = "VERIFIED"
        else:
            size_value = item.get("size_bytes")
            size_bytes = int(size_value) if size_value not in (None, "") else None
            sha256 = str(item.get("sha256") or "") or None
            original_name = str(item.get("original_name") or Path(locator).name or f"attachment_{index + 1}")
            external_uri = str(item.get("external_display_uri") or item.get("uri") or locator) or None
            availability = str(item.get("availability_state") or "REFERENCE_ONLY")
            hash_status = "PROVIDED_UNVERIFIED" if sha256 else "UNAVAILABLE"
        display_name = str(item.get("display_name") or original_name)
        mime_type = str(item.get("mime_type") or mimetypes.guess_type(original_name)[0] or "application/octet-stream")
        stable_file_id = str(
            item.get("stable_file_id")
            or _sha256_text(
                "\x1f".join(
                    (
                        display_name,
                        str(external_uri or ""),
                        str(size_bytes if size_bytes is not None else ""),
                        str(sha256 or ""),
                    )
                )
            )
        )
        records.append(
            {
                "display_name": display_name,
                "original_name": original_name,
                "stable_file_id": stable_file_id,
                "external_display_uri": external_uri,
                "package_relative_path": item.get("package_relative_path"),
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "direction": str(item.get("direction") or "INPUT"),
                "availability_state": availability,
                "ephemeral": int(bool(item.get("ephemeral", False))),
                "hash_status": hash_status,
            }
        )
    serialized = json.dumps(records, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return records, _sha256_text(serialized)


def append_env15_chat_lineage_source(
    brain_root: str | Path,
    source: dict[str, Any],
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    _, database = resolve_env15_sector(root, "chat_lineage")
    path_text = str(source.get("path") or "").strip()
    prompt = str(source.get("text") or "")
    source_path: Path | None = None
    source_bytes = prompt.encode("utf-8", "surrogatepass")
    normalization: dict[str, Any] | None = None
    if path_text:
        source_path = Path(path_text).expanduser().resolve(strict=True)
        if not source_path.is_file():
            raise Env15ChatLineageError("CHAT_LINEAGE_SOURCE_FILE_REQUIRED")
        source_bytes = source_path.read_bytes()
        if source_path.suffix.casefold() == ".docx":
            prompt, normalization = _docx_to_deterministic_markdown(source_path)
        else:
            prompt = source_bytes.decode("utf-8", errors="replace")
    if not prompt and not source_bytes:
        raise Env15ChatLineageError("CHAT_LINEAGE_PROMPT_REQUIRED")
    response = str(source.get("assistant_response") or source.get("response") or "")
    visible_summary = str(source.get("visible_reasoning_summary") or "Imported visible chat-lineage source.")
    full_prompt = prompt
    full_prompt_hash = _sha256_text(full_prompt)
    full_response_hash = _sha256_text(response)
    source_id = str(
        source.get("source_id")
        or (
            "chat_source_" + _sha256_text(str(source_path).casefold())[:24]
            if source_path is not None
            else full_prompt_hash
        )
    )
    source_hash = _sha256_bytes(source_bytes)
    actor = str(source.get("actor") or "Evidence Lane source intake")
    provider = str(source.get("provider") or source.get("model") or "USER_SUPPLIED")
    source_time_when_known = str(source.get("source_time") or source.get("source_time_when_known") or "") or None
    if source_time_when_known is None and source_path is not None:
        source_time_when_known = datetime.fromtimestamp(
            source_path.stat().st_mtime, timezone.utc
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    attachment_records, attachment_set_sha256 = _attachment_records(source)
    source_classification = str(
        source.get("source_classification")
        or source.get("lineage_classification")
        or "CURRENT_CHAT"
    )
    task_window_id = str(source.get("task_window_id") or "") or None
    source_event_range = str(source.get("source_event_range") or "") or None
    idempotency_key = str(
        source.get("idempotency_key")
        or f"source:{source_id}:{source_hash}:{full_prompt_hash}:{full_response_hash}:{attachment_set_sha256}"
    )
    if normalization is not None:
        normalization["source_id"] = source_id
    if source_path is not None:
        _cached_hash, fingerprint_probe = cached_content_hash(
            root,
            "chat_lineage",
            source_id,
            source_path,
        )
    else:
        fingerprint_probe = {
            "source_path": f"inline://chat_lineage/{source_id}",
            "stat_fingerprint": source_hash,
            "file_count": 0,
            "total_bytes": len(source_bytes),
            "inline": True,
        }

    prior_cursor: tuple[Any, ...] | None = None
    read_only = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        existing = read_only.execute(
            "SELECT turn_id FROM turn_prepare WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        tables = {
            str(row[0])
            for row in read_only.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if "lineage_source_cursor" in tables:
            cursor_columns = {
                str(row[1])
                for row in read_only.execute("PRAGMA table_info(lineage_source_cursor)")
            }
            attachment_select = (
                "full_attachment_set_sha256"
                if "full_attachment_set_sha256" in cursor_columns
                else "''"
            )
            prior_cursor = read_only.execute(
                "SELECT full_prompt_sha256,full_prompt_text,full_response_sha256,"
                f"last_turn_id,revision_sequence,{attachment_select} "
                "FROM lineage_source_cursor WHERE source_id=?",
                (source_id,),
            ).fetchone()
    finally:
        read_only.close()
    if existing:
        record_content_hash(root, "chat_lineage", source_id, source_hash, fingerprint_probe)
        result = {"status": "SKIPPED_UNCHANGED", "turn_id": existing[0], "idempotency_key": idempotency_key}
        if normalization is not None:
            read_only = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            try:
                row = read_only.execute(
                    "SELECT receipt_json FROM source_normalization_receipt WHERE turn_id=?",
                    (existing[0],),
                ).fetchone()
            finally:
                read_only.close()
            if row:
                result["normalization"] = json.loads(row[0])
        return result

    prior_prompt_hash: str | None = None
    prefix_character_count = 0
    revision_sequence = 1
    append_classification = "INITIAL_FULL_SOURCE"
    if prior_cursor is not None:
        prior_prompt_hash = str(prior_cursor[0])
        prior_full_prompt = str(prior_cursor[1])
        prior_response_hash = str(prior_cursor[2])
        prior_attachment_hash = str(prior_cursor[5] or "")
        if (
            full_prompt_hash == prior_prompt_hash
            and full_response_hash == prior_response_hash
            and attachment_set_sha256 == prior_attachment_hash
        ):
            record_content_hash(root, "chat_lineage", source_id, source_hash, fingerprint_probe)
            return {
                "status": "SKIPPED_UNCHANGED",
                "turn_id": str(prior_cursor[3]),
                "idempotency_key": idempotency_key,
                "source_id": source_id,
                "full_prompt_sha256": full_prompt_hash,
                "appended_character_count": 0,
            }
        if not full_prompt.startswith(prior_full_prompt):
            raise Env15ChatLineageError(
                "CHAT_LINEAGE_SOURCE_DIVERGED_REQUIRES_REVIEW:"
                f"{source_id}:{prior_prompt_hash}:{full_prompt_hash}"
            )
        prefix_character_count = len(prior_full_prompt)
        prompt = full_prompt[prefix_character_count:]
        revision_sequence = int(prior_cursor[4]) + 1
        append_classification = (
            "APPENDED_UNSEEN_SUFFIX"
            if prompt
            else "APPENDED_ATTACHMENT_OR_VISIBLE_RESPONSE_DELTA"
        )
    else:
        prompt = full_prompt
    prompt_hash = _sha256_text(prompt)
    response_hash = full_response_hash

    backup = root / "receipts" / f".chat_lineage_before_{uuid.uuid4().hex}.sqlite"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, backup)
    # Env15 sector files are intentionally read-only while relocked. copy2
    # preserves that mode; make only the disposable rollback copy writable so
    # Windows can remove it after a successful append or restore.
    _set_writable(backup, True)
    _set_writable(database, True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys=ON")
        latest_prepare = connection.execute(
            "SELECT sequence,turn_id FROM turn_prepare ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        latest_head = connection.execute(
            "SELECT head_sequence,latest_state_hash,head_hash FROM lineage_head ORDER BY head_sequence DESC LIMIT 1"
        ).fetchone()
        sequence = int(latest_prepare[0]) + 1 if latest_prepare else 1
        parent_turn = latest_prepare[1] if latest_prepare else None
        previous_state_hash = latest_head[1] if latest_head else "0" * 64
        previous_head_hash = latest_head[2] if latest_head else "0" * 64
        head_sequence = int(latest_head[0]) + 1 if latest_head else 1
        turn_id = str(source.get("turn_id") or f"CHAT.{sequence:09d}.{prompt_hash[:12]}")
        created_at = _utc_now()
        entry = json.dumps(
            {"turn_id": turn_id, "sequence": sequence, "mode": "source_intake", "status": "ENTRY"},
            sort_keys=True,
        )
        exit_content = json.dumps(
            {"turn_id": turn_id, "sequence": sequence, "status": "COMMITTED", "next": sequence + 1},
            sort_keys=True,
        )
        entry_hash = _sha256_text(entry)
        exit_hash = _sha256_text(exit_content)
        current_state_hash = _sha256_text(
            "\x1f".join(
                (
                    previous_state_hash,
                    prompt_hash,
                    response_hash,
                    attachment_set_sha256,
                    exit_hash,
                )
            )
        )
        head_hash = _sha256_text(
            "\x1f".join((previous_head_hash, turn_id, current_state_hash, exit_hash))
        )
        summary = " ".join(prompt.split())[:1000]
        response_summary = " ".join(response.split())[:1000]
        connection.execute("BEGIN IMMEDIATE")
        _lineage_cursor_schema(connection)
        if normalization is not None:
            _normalization_schema(connection)
        connection.execute(
            "INSERT INTO turn_prepare VALUES(?,?,?,?,?,?,?,?,?,?)",
            (turn_id, sequence, parent_turn, "SOURCE_INTAKE", "AUTO", idempotency_key, prompt_hash, previous_state_hash, created_at, "PREPARED"),
        )
        connection.execute(
            "INSERT INTO prompt_raw_exact VALUES(?,?,?,?,?)",
            (
                turn_id,
                prompt,
                len(prompt.encode("utf-8")),
                prompt_hash,
                "FULL_EXACT",
            ),
        )
        connection.execute(
            "INSERT INTO prompt_normalized_summary VALUES(?,?,?)",
            (turn_id, summary, _sha256_text(summary)),
        )
        connection.execute(
            "INSERT INTO response_raw_visible_exact VALUES(?,?,?,?,?)",
            (turn_id, response, len(response.encode("utf-8")), response_hash, "FULL_VISIBLE_EXACT"),
        )
        connection.execute(
            "INSERT INTO response_summary VALUES(?,?,?)",
            (turn_id, response_summary, _sha256_text(response_summary)),
        )
        connection.execute(
            "INSERT INTO visible_reasoning_summary VALUES(?,?,?,?)",
            (turn_id, visible_summary, 0, _sha256_text(visible_summary)),
        )
        connection.execute(
            "INSERT INTO entry_exit_receipt VALUES(?,?,?,?,?,?)",
            (f"entry_{turn_id}", turn_id, "ENTRY", entry, entry_hash, created_at),
        )
        connection.execute(
            "INSERT INTO entry_exit_receipt VALUES(?,?,?,?,?,?)",
            (f"exit_{turn_id}", turn_id, "EXIT", exit_content, exit_hash, created_at),
        )
        if source_path is not None:
            mime_type = (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if source_path.suffix.casefold() == ".docx"
                else str(source.get("mime_type") or "text/plain")
            )
            connection.execute(
                "INSERT INTO file_link_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"link_{turn_id}", turn_id, str(source.get("display_name") or source_path.name),
                    source_path.name, source_id, str(source_path), None,
                    mime_type, len(source_bytes), source_hash,
                    "INPUT", "AVAILABLE", 0, "VERIFIED", created_at,
                ),
            )
        for attachment_index, attachment in enumerate(attachment_records, start=1):
            connection.execute(
                "INSERT INTO file_link_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"link_{turn_id}_attachment_{attachment_index:04d}",
                    turn_id,
                    attachment["display_name"],
                    attachment["original_name"],
                    attachment["stable_file_id"],
                    attachment["external_display_uri"],
                    attachment["package_relative_path"],
                    attachment["mime_type"],
                    attachment["size_bytes"],
                    attachment["sha256"],
                    attachment["direction"],
                    attachment["availability_state"],
                    attachment["ephemeral"],
                    attachment["hash_status"],
                    created_at,
                ),
            )
        if normalization is not None:
            normalization_id = "normalization_" + _sha256_text(
                "\x1f".join((source_id, source_hash, prompt_hash))
            )[:24]
            normalization_receipt = {
                **normalization,
                "normalization_id": normalization_id,
                "turn_id": turn_id,
            }
            connection.execute(
                "INSERT INTO source_normalization_receipt VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    normalization_id,
                    turn_id,
                    source_id,
                    str(source_path),
                    normalization["source_format"],
                    normalization["normalized_format"],
                    normalization["original_sha256"],
                    normalization["normalized_sha256"],
                    normalization["parser_id"],
                    normalization["original_size_bytes"],
                    normalization["normalized_size_bytes"],
                    json.dumps(normalization_receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    created_at,
                ),
            )
        connection.execute(
            "INSERT INTO state_hash_chain VALUES(?,?,?,?,?,?,?,?)",
            (sequence, turn_id, previous_state_hash, prompt_hash, response_hash, exit_hash, current_state_hash, created_at),
        )
        connection.execute(
            "INSERT INTO turn_commit VALUES(?,?,?,?,?,?,?)",
            (turn_id, sequence, response_hash, current_state_hash, None, created_at, "COMMITTED"),
        )
        connection.execute(
            "INSERT INTO lineage_head VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                head_sequence, sequence, turn_id, current_state_hash, exit_hash,
                previous_head_hash, head_hash, "COMMITTED", f"PROJECT.CHAT.P00.S00.T{sequence + 1:03d}", created_at,
            ),
        )
        revision_id = "lineage_revision_" + _sha256_text(
            "\x1f".join((source_id, str(revision_sequence), full_prompt_hash, full_response_hash))
        )[:24]
        connection.execute(
            "INSERT INTO lineage_source_revision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                source_id,
                revision_sequence,
                prior_prompt_hash,
                full_prompt_hash,
                prompt_hash,
                prefix_character_count,
                len(prompt),
                full_response_hash,
                turn_id,
                actor,
                provider,
                source_time_when_known,
                created_at,
                append_classification,
                source_classification,
                task_window_id,
                source_event_range,
            ),
        )
        connection.execute(
            """
            INSERT INTO lineage_source_cursor(
              source_id,source_path,full_prompt_sha256,full_prompt_text,
              full_response_sha256,full_attachment_set_sha256,source_bytes_sha256,source_size_bytes,
              last_turn_id,revision_sequence,actor,provider,source_time_when_known,ingested_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
              source_path=excluded.source_path,
              full_prompt_sha256=excluded.full_prompt_sha256,
              full_prompt_text=excluded.full_prompt_text,
              full_response_sha256=excluded.full_response_sha256,
              full_attachment_set_sha256=excluded.full_attachment_set_sha256,
              source_bytes_sha256=excluded.source_bytes_sha256,
              source_size_bytes=excluded.source_size_bytes,
              last_turn_id=excluded.last_turn_id,
              revision_sequence=excluded.revision_sequence,
              actor=excluded.actor,
              provider=excluded.provider,
              source_time_when_known=excluded.source_time_when_known,
              ingested_at=excluded.ingested_at
            """,
            (
                source_id,
                str(source_path) if source_path is not None else f"inline://chat_lineage/{source_id}",
                full_prompt_hash,
                full_prompt,
                full_response_hash,
                attachment_set_sha256,
                source_hash,
                len(source_bytes),
                turn_id,
                revision_sequence,
                actor,
                provider,
                source_time_when_known,
                created_at,
            ),
        )
        connection.commit()
        connection.close()
        connection = None
        _set_writable(database, False)
        integrity_connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            integrity = tuple(row[0] for row in integrity_connection.execute("PRAGMA integrity_check"))
            foreign_keys = tuple(integrity_connection.execute("PRAGMA foreign_key_check"))
        finally:
            integrity_connection.close()
        if integrity != ("ok",) or foreign_keys:
            raise Env15ChatLineageError("CHAT_LINEAGE_POST_COMMIT_VALIDATION_FAILED")
        record_content_hash(root, "chat_lineage", source_id, source_hash, fingerprint_probe)
        backup.unlink(missing_ok=True)
        receipt = {
            "status": "PASS", "turn_id": turn_id, "sequence": sequence,
            "prompt_sha256": prompt_hash, "response_sha256": response_hash,
            "source_id": source_id,
            "source_bytes_sha256": source_hash,
            "full_prompt_sha256": full_prompt_hash,
            "appended_prompt_sha256": prompt_hash,
            "append_classification": append_classification,
            "prefix_character_count": prefix_character_count,
            "appended_character_count": len(prompt),
            "revision_sequence": revision_sequence,
            "actor": actor,
            "provider": provider,
            "source_time_when_known": source_time_when_known,
            "source_classification": source_classification,
            "task_window_id": task_window_id,
            "source_event_range": source_event_range,
            "attachment_count": len(attachment_records),
            "attachment_set_sha256": attachment_set_sha256,
            "ingested_at": created_at,
            "current_state_hash": current_state_hash, "head_hash": head_hash,
            "integrity_check": integrity, "foreign_key_violation_count": len(foreign_keys),
            "idempotency_key": idempotency_key,
        }
        if normalization is not None:
            receipt["normalization"] = normalization_receipt
        receipt_path = root / "receipts" / f"CHAT_LINEAGE_APPEND_{turn_id}.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt["receipt_path"] = str(receipt_path)
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


__all__ = ["Env15ChatLineageError", "append_env15_chat_lineage_source"]
