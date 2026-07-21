from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlite_brain_builder.runtime.env15_project_schema import (
    UNIVERSAL_SECTOR_TABLES,
    Env15ProjectSchemaError,
    governed_sector_mutation,
    resolve_env15_sector,
    verify_env15_live_project,
)


DELTA_SECTOR_ID = "delta"
DELTA_SCHEMA_VERSION = "T023_DELTA_V1"
DELTA_DATABASE_RELATIVE = "project/sectors/delta/delta_sector_v001.sqlite"
DELTA_LAW_RELATIVE = "project/sectors/delta/delta_law.md"
DELTA_POINTER_RELATIVE = "project/sectors/delta/delta_pointer.json"
DELTA_PROMPT2_RELATIVE = "project/sectors/delta/projections/PLAN_GOAL_PROMPT_2.txt"
REFRESH_DELTA_LEDGER_RELATIVE = "project/deltas/delta_ledger.db"

DELTA_SECTOR_TABLES = UNIVERSAL_SECTOR_TABLES | {
    "delta_lane_registry",
    "delta_record",
    "delta_lane_link",
    "delta_disposition_event",
    "canonical_delta_pointer",
    "goal_prompt",
    "state_slip",
}

DELTA_DISPOSITIONS = {
    "PENDING",
    "APPROVED",
    "SUPERSEDED",
    "CHANGED",
    "FAILED",
    "DROPPED",
}


class PlanDeltaGovernanceError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_id(kind: str, *parts: Any) -> str:
    body = "\x1f".join(str(part) for part in parts)
    return f"{kind}_{_sha256_text(body)}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _safe_project_path(root: Path, relative: str) -> Path:
    candidate = (root / Path(relative.replace("/", os.sep))).resolve()
    project = (root / "project").resolve()
    if candidate != project and project not in candidate.parents:
        raise PlanDeltaGovernanceError(f"DELTA_PROJECT_PATH_ESCAPE:{relative}")
    return candidate


def _restore_mode(path: Path, mode: int) -> None:
    path.chmod(mode)


def _make_writable(path: Path) -> int:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IWUSR)
    return mode


def _make_read_only(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _create_delta_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE sector_meta(
              sector_id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              version TEXT NOT NULL,
              default_access TEXT NOT NULL,
              auto_write INTEGER NOT NULL,
              explicit_grant_required INTEGER NOT NULL,
              payload_state TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE sector_head(
              head_sequence INTEGER PRIMARY KEY,
              latest_turn_id TEXT,
              current_hash TEXT,
              next_expected_index TEXT,
              status TEXT NOT NULL
            );
            CREATE TABLE source_registry(
              source_id TEXT PRIMARY KEY,
              source_type TEXT,
              original_name TEXT,
              canonical_path TEXT,
              sha256 TEXT,
              size_bytes INTEGER,
              availability_state TEXT,
              created_turn TEXT
            );
            CREATE TABLE artifact_registry(
              artifact_id TEXT PRIMARY KEY,
              source_id TEXT,
              artifact_type TEXT,
              canonical_path TEXT,
              sha256 TEXT,
              size_bytes INTEGER,
              review_state TEXT,
              FOREIGN KEY(source_id) REFERENCES source_registry(source_id)
            );
            CREATE TABLE chunk_index(
              chunk_id TEXT PRIMARY KEY,
              source_id TEXT,
              artifact_id TEXT,
              chunk_type TEXT,
              ordinal INTEGER,
              content TEXT,
              content_sha256 TEXT,
              token_estimate INTEGER,
              FOREIGN KEY(source_id) REFERENCES source_registry(source_id),
              FOREIGN KEY(artifact_id) REFERENCES artifact_registry(artifact_id)
            );
            CREATE TABLE relation_edge(
              edge_id TEXT PRIMARY KEY,
              source_node TEXT NOT NULL,
              relation_type TEXT NOT NULL,
              target_node TEXT NOT NULL,
              evidence_ref TEXT
            );
            CREATE TABLE mutation_receipt(
              receipt_id TEXT PRIMARY KEY,
              grant_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              previous_hash TEXT,
              current_hash TEXT,
              relock_state TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE delta_lane_registry(
              lane_id TEXT PRIMARY KEY,
              display_name TEXT NOT NULL,
              first_sequence INTEGER NOT NULL,
              latest_delta_id TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE delta_record(
              delta_id TEXT PRIMARY KEY,
              delta_sequence INTEGER NOT NULL UNIQUE,
              delta_kind TEXT NOT NULL,
              primary_lane TEXT NOT NULL,
              classified_lanes_json TEXT NOT NULL,
              classification TEXT NOT NULL,
              steer_kind TEXT NOT NULL,
              content TEXT NOT NULL,
              content_sha256 TEXT NOT NULL,
              source_id TEXT,
              source_sha256 TEXT,
              model_family TEXT NOT NULL,
              model_name TEXT NOT NULL,
              disposition TEXT NOT NULL,
              prior_delta_id TEXT,
              supersedes_delta_id TEXT,
              hil_gate TEXT,
              next_action TEXT,
              idempotency_key TEXT NOT NULL UNIQUE,
              created_turn TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(source_id) REFERENCES source_registry(source_id),
              FOREIGN KEY(prior_delta_id) REFERENCES delta_record(delta_id),
              FOREIGN KEY(supersedes_delta_id) REFERENCES delta_record(delta_id)
            );
            CREATE TABLE delta_lane_link(
              delta_id TEXT NOT NULL,
              lane_id TEXT NOT NULL,
              lane_sequence INTEGER NOT NULL,
              disposition TEXT NOT NULL,
              PRIMARY KEY(delta_id,lane_id),
              FOREIGN KEY(delta_id) REFERENCES delta_record(delta_id),
              FOREIGN KEY(lane_id) REFERENCES delta_lane_registry(lane_id)
            );
            CREATE TABLE delta_disposition_event(
              event_id TEXT PRIMARY KEY,
              delta_id TEXT NOT NULL,
              from_disposition TEXT,
              to_disposition TEXT NOT NULL,
              actor TEXT NOT NULL,
              reason TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(delta_id) REFERENCES delta_record(delta_id)
            );
            CREATE TABLE canonical_delta_pointer(
              singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
              current_delta_id TEXT,
              latest_sequence INTEGER NOT NULL,
              current_plan_delta_id TEXT,
              latest_prompt_id TEXT,
              status TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(current_delta_id) REFERENCES delta_record(delta_id),
              FOREIGN KEY(current_plan_delta_id) REFERENCES delta_record(delta_id)
            );
            CREATE TABLE goal_prompt(
              prompt_id TEXT PRIMARY KEY,
              prompt_index INTEGER NOT NULL,
              source_delta_id TEXT,
              status TEXT NOT NULL,
              hil_gate TEXT,
              text TEXT NOT NULL,
              text_sha256 TEXT NOT NULL,
              canonical_path TEXT NOT NULL,
              created_at TEXT NOT NULL,
              is_current INTEGER NOT NULL,
              FOREIGN KEY(source_delta_id) REFERENCES delta_record(delta_id)
            );
            CREATE TABLE state_slip(
              slip_id TEXT PRIMARY KEY,
              slip_kind TEXT NOT NULL,
              model_family TEXT NOT NULL,
              model_name TEXT NOT NULL,
              prompt_index INTEGER NOT NULL,
              gates_json TEXT NOT NULL,
              lanes_json TEXT NOT NULL,
              token_count INTEGER,
              token_metric_state TEXT NOT NULL,
              compact_line TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            CREATE INDEX delta_record_sequence_idx ON delta_record(delta_sequence);
            CREATE INDEX delta_record_disposition_idx ON delta_record(disposition);
            CREATE INDEX delta_lane_link_lane_idx ON delta_lane_link(lane_id,lane_sequence);
            CREATE INDEX delta_event_delta_idx ON delta_disposition_event(delta_id,created_at);
            CREATE INDEX state_slip_turn_idx ON state_slip(turn_id,slip_kind);
            """
        )
        created_at = _utc_now()
        connection.execute(
            "INSERT INTO sector_meta VALUES(?,?,?,?,?,?,?,?)",
            (DELTA_SECTOR_ID, "Plan and Steer Delta Governance", DELTA_SCHEMA_VERSION,
             "READ_ONLY", 0, 1, "UNPOPULATED", "ACTIVE_SCHEMA"),
        )
        connection.execute(
            "INSERT INTO sector_head VALUES(?,?,?,?,?)",
            (1, None, _sha256_text(""), "delta_000001", "READY"),
        )
        connection.execute(
            "INSERT INTO canonical_delta_pointer VALUES(?,?,?,?,?,?,?)",
            (1, None, 0, None, None, "EMPTY", created_at),
        )
        connection.commit()
        integrity = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
        if integrity != ("ok",):
            raise PlanDeltaGovernanceError("DELTA_SECTOR_INITIAL_INTEGRITY_FAILED")
    finally:
        connection.close()
    os.replace(temporary, path)
    _make_read_only(path)


def _delta_law_text() -> str:
    return """# T023 Plan and Steer Delta Law

This runtime extension is additive to the immutable Env15 fourteen-sector resource.
It is not the Refresh delta ledger and never rewrites `project/deltas/delta_ledger.db`.

- The initial Plan source is canonical Delta 0 / sequence 1.
- Later user steers are append-only records with explicit lane classifications.
- Dispositions are retained as APPROVED, SUPERSEDED, CHANGED, FAILED, DROPPED, or PENDING.
- No record is physically deleted when its disposition changes.
- Every database write requires the Env15 named one-turn mutation grant and relock receipt.
- Prompt 2 is a derived, recoverable projection of the canonical Plan delta chain.
- Model entry/exit slips are compact, model-specific records; unavailable token metrics remain unavailable.
"""


def ensure_plan_delta_sector(brain_root: str | Path) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    base_report = verify_env15_live_project(root)
    if not base_report.passed:
        raise PlanDeltaGovernanceError(
            "DELTA_SECTOR_BASE_ENV15_INVALID:" + ";".join(base_report.errors)
        )
    router_path = root / "project" / "project_router.sqlite"
    router = sqlite3.connect(f"file:{router_path.as_posix()}?mode=ro", uri=True)
    try:
        existing = router.execute(
            "SELECT sqlite_path,law_path,pointer_path,schema_version,status "
            "FROM sector_registry WHERE sector_id=?",
            (DELTA_SECTOR_ID,),
        ).fetchone()
    finally:
        router.close()
    database = _safe_project_path(root, DELTA_DATABASE_RELATIVE)
    law = _safe_project_path(root, DELTA_LAW_RELATIVE)
    pointer = _safe_project_path(root, DELTA_POINTER_RELATIVE)
    if existing:
        if existing != (
            DELTA_DATABASE_RELATIVE,
            DELTA_LAW_RELATIVE,
            DELTA_POINTER_RELATIVE,
            DELTA_SCHEMA_VERSION,
            "ACTIVE_SCHEMA",
        ):
            raise PlanDeltaGovernanceError(f"DELTA_SECTOR_REGISTRY_CONFLICT:{existing}")
        missing = [name for name, path in (("database", database), ("law", law), ("pointer", pointer)) if not path.is_file()]
        if missing:
            raise PlanDeltaGovernanceError("DELTA_SECTOR_REGISTERED_FILE_MISSING:" + ",".join(missing))
        if not DELTA_SECTOR_TABLES <= _table_names(database):
            raise PlanDeltaGovernanceError("DELTA_SECTOR_SCHEMA_MISMATCH")
        return _ensure_result(root, "REUSED")

    if database.exists() or law.exists() or pointer.exists():
        raise PlanDeltaGovernanceError("DELTA_SECTOR_UNREGISTERED_ARTIFACT_CONFLICT")
    _create_delta_database(database)
    law.write_text(_delta_law_text(), encoding="utf-8")
    _write_json(
        pointer,
        {
            "contract": DELTA_SCHEMA_VERSION,
            "sector_id": DELTA_SECTOR_ID,
            "database": DELTA_DATABASE_RELATIVE,
            "refresh_delta_ledger": REFRESH_DELTA_LEDGER_RELATIVE,
            "refresh_ledger_relationship": "DISTINCT_DO_NOT_MIX",
            "current_delta_id": None,
            "latest_sequence": 0,
            "latest_prompt_id": None,
            "status": "EMPTY",
            "updated_at": _utc_now(),
        },
    )
    original_mode = _make_writable(router_path)
    try:
        router = sqlite3.connect(router_path)
        try:
            router.execute("BEGIN IMMEDIATE")
            router.execute(
                "INSERT INTO sector_registry VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    DELTA_SECTOR_ID,
                    "Plan and Steer Delta Governance",
                    DELTA_DATABASE_RELATIVE,
                    DELTA_LAW_RELATIVE,
                    DELTA_POINTER_RELATIVE,
                    "READ_ONLY",
                    0,
                    1,
                    1,
                    DELTA_SCHEMA_VERSION,
                    "ACTIVE_SCHEMA",
                ),
            )
            router.commit()
        except Exception:
            router.rollback()
            raise
        finally:
            router.close()
    finally:
        _restore_mode(router_path, original_mode)

    report = verify_env15_live_project(root)
    if not report.passed:
        raise PlanDeltaGovernanceError(
            "DELTA_SECTOR_POST_REGISTER_ENV15_INVALID:" + ";".join(report.errors)
        )
    receipt = _ensure_result(root, "CREATED")
    _write_json(root / "receipts" / "T023_PLAN_DELTA_SECTOR_EXTENSION.json", receipt)
    return receipt


def _table_names(database: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def _ensure_result(root: Path, status: str) -> dict[str, Any]:
    database = _safe_project_path(root, DELTA_DATABASE_RELATIVE)
    return {
        "contract": DELTA_SCHEMA_VERSION,
        "status": status,
        "sector_id": DELTA_SECTOR_ID,
        "database_path": str(database),
        "database_sha256": _sha256_file(database),
        "law_path": str(_safe_project_path(root, DELTA_LAW_RELATIVE)),
        "pointer_path": str(_safe_project_path(root, DELTA_POINTER_RELATIVE)),
        "refresh_delta_ledger": str(root / REFRESH_DELTA_LEDGER_RELATIVE),
        "refresh_ledger_relationship": "DISTINCT_DO_NOT_MIX",
        "created_at": _utc_now(),
    }


def _classify_lanes(content: str, explicit_lanes: Iterable[str] | None = None) -> list[str]:
    lanes: list[str] = []
    for lane in explicit_lanes or ():
        normalized = re.sub(r"[^a-z0-9]+", "_", str(lane).casefold()).strip("_")
        if normalized and normalized not in lanes:
            lanes.append(normalized)
    lowered = content.casefold()
    rules = (
        ("rollback", r"\brollback\b|\bversion control\b|\brecycle bin\b|\bdrop selected\b"),
        ("ui_ux", r"\bui\b|\bux\b|\bfrontend\b|\btheme\b|\bglass\b|\bgeometry\b|\bbutton\b|\bpill\b|\bside rail\b"),
        ("backend", r"\bbackend\b|\bapi\b|\bsocket\b|\bsqlite\b|\bdatabase\b|\bdb\b|\brouter\b"),
        ("plan", r"\bplan\b|\bgoal\b|\broadmap\b|\bhil\b|\bsteer\b|\bdelta\b"),
        ("chat_lineage", r"\bchat lineage\b|\bentry slip\b|\bexit slip\b|\bstate travel\b"),
        ("docs", r"\bdocx\b|\bword\b|\bdocument\b"),
        ("data_excel", r"\bexcel\b|\bworkbook\b|\bformula\b|\bsheet\b"),
        ("local_code", r"\bcode\b|\bcodex\b|\brepository\b|\bgithub\b"),
        ("research", r"\bresearch\b|\bevidence\b"),
        ("packaging", r"\bpackage\b|\bpackaging\b|\bgemini exact.?10\b|\bbuild button\b"),
    )
    for lane, pattern in rules:
        if re.search(pattern, lowered) and lane not in lanes:
            lanes.append(lane)
    return lanes or ["custom"]


def _steer_kind(content: str) -> str:
    lowered = content.casefold()
    if "hard steer" in lowered:
        return "HARD_STEER"
    if "directional steer" in lowered:
        return "DIRECTIONAL_STEER"
    if "information steer" in lowered:
        return "INFORMATION_STEER"
    if "ui steer" in lowered or "ui ux" in lowered:
        return "UI_UX_STEER"
    return "INFERRED_STEER"


def _register_lane_links(
    connection: sqlite3.Connection,
    delta_id: str,
    delta_sequence: int,
    lanes: list[str],
    disposition: str,
) -> None:
    for lane in lanes:
        row = connection.execute(
            "SELECT COALESCE(MAX(lane_sequence),0)+1 FROM delta_lane_link WHERE lane_id=?",
            (lane,),
        ).fetchone()
        lane_sequence = int(row[0])
        connection.execute(
            "INSERT INTO delta_lane_registry(lane_id,display_name,first_sequence,latest_delta_id,status) "
            "VALUES(?,?,?,?,?) ON CONFLICT(lane_id) DO UPDATE SET latest_delta_id=excluded.latest_delta_id,status=excluded.status",
            (lane, lane.replace("_", " ").title(), delta_sequence, delta_id, "ACTIVE"),
        )
        connection.execute(
            "INSERT INTO delta_lane_link VALUES(?,?,?,?)",
            (delta_id, lane, lane_sequence, disposition),
        )


def _insert_universal_delta_projection(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    source_type: str,
    original_name: str,
    canonical_path: str,
    source_sha256: str,
    content: str,
    turn_id: str,
    delta_id: str,
    delta_kind: str,
) -> None:
    artifact_id = _stable_id("delta_artifact", delta_id, source_sha256)
    chunk_id = _stable_id("delta_chunk", delta_id, source_sha256)
    connection.execute(
        "INSERT OR IGNORE INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
        (source_id, source_type, original_name, canonical_path, source_sha256,
         len(content.encode("utf-8", "surrogatepass")), "AVAILABLE", turn_id),
    )
    connection.execute(
        "INSERT OR IGNORE INTO artifact_registry VALUES(?,?,?,?,?,?,?)",
        (artifact_id, source_id, delta_kind, canonical_path, source_sha256,
         len(content.encode("utf-8", "surrogatepass")), "INDEXED"),
    )
    connection.execute(
        "INSERT OR IGNORE INTO chunk_index VALUES(?,?,?,?,?,?,?,?)",
        (chunk_id, source_id, artifact_id, f"delta:{delta_kind.casefold()}", 1, content,
         _sha256_text(content), max(1, len(content) // 4)),
    )
    connection.execute(
        "INSERT OR IGNORE INTO relation_edge VALUES(?,?,?,?,?)",
        (_stable_id("delta_edge", source_id, delta_id), source_id, "governs_delta", delta_id, canonical_path),
    )


def _event(
    connection: sqlite3.Connection,
    *,
    delta_id: str,
    before: str | None,
    after: str,
    actor: str,
    reason: str,
    turn_id: str,
) -> None:
    connection.execute(
        "INSERT INTO delta_disposition_event VALUES(?,?,?,?,?,?,?,?)",
        (_stable_id("delta_event", delta_id, before, after, actor, reason, turn_id, _utc_now()),
         delta_id, before, after, actor, reason, turn_id, _utc_now()),
    )


def _update_pointer_projection(root: Path) -> None:
    _, database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        pointer = connection.execute("SELECT * FROM canonical_delta_pointer WHERE singleton_id=1").fetchone()
    finally:
        connection.close()
    _write_json(
        _safe_project_path(root, DELTA_POINTER_RELATIVE),
        {
            "contract": DELTA_SCHEMA_VERSION,
            "sector_id": DELTA_SECTOR_ID,
            "database": DELTA_DATABASE_RELATIVE,
            "database_sha256": _sha256_file(database),
            "refresh_delta_ledger": REFRESH_DELTA_LEDGER_RELATIVE,
            "refresh_ledger_relationship": "DISTINCT_DO_NOT_MIX",
            "current_delta_id": pointer["current_delta_id"],
            "latest_sequence": pointer["latest_sequence"],
            "current_plan_delta_id": pointer["current_plan_delta_id"],
            "latest_prompt_id": pointer["latest_prompt_id"],
            "status": pointer["status"],
            "updated_at": _utc_now(),
        },
    )


def _canonical_plan_text(brain_root: Path, source_id: str) -> str:
    _, source_database = resolve_env15_sector(brain_root, "plan")
    connection = sqlite3.connect(f"file:{source_database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT content FROM chunk_index WHERE source_id=? ORDER BY artifact_id,ordinal",
            (source_id,),
        ).fetchall()
    finally:
        connection.close()
    values: list[str] = []
    for (content,) in rows:
        text = str(content or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            values.append(text)
            continue
        if isinstance(payload, dict) and str(payload.get("text") or "").strip():
            values.append(str(payload["text"]).strip())
        elif isinstance(payload, dict) and payload.get("rows"):
            values.append(_json(payload["rows"]))
    canonical = "\n\n".join(value for value in values if value).strip()
    if not canonical:
        raise PlanDeltaGovernanceError(f"PLAN_DELTA_CANONICAL_CONTENT_EMPTY:{source_id}")
    return canonical


def register_initial_plan_delta(
    brain_root: str | Path,
    source: dict[str, Any],
    ingestion_result: Any,
    *,
    actor: str = "Evidence OS SQLite Builder",
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    ensure_plan_delta_sector(root)
    source_id = str(getattr(ingestion_result, "source_id", "") or source.get("source_id") or "").strip()
    source_sha256 = str(getattr(ingestion_result, "source_sha256", "") or "").strip()
    if not source_id or not source_sha256:
        raise PlanDeltaGovernanceError("PLAN_DELTA_INGESTION_IDENTITY_REQUIRED")
    content = _canonical_plan_text(root, source_id)
    content_sha256 = _sha256_text(content)
    turn_id = f"initial_plan:{source_id}:{source_sha256[:16]}"
    idempotency_key = f"initial_plan:{source_id}:{source_sha256}"
    original_name = str(source.get("display_name") or source.get("displayName") or Path(str(source.get("path") or "plan")).name)
    canonical_path = str(source.get("path") or f"inline:{source_id}")
    result: dict[str, Any] = {}

    _, delta_database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    read_only = sqlite3.connect(f"file:{delta_database.as_posix()}?mode=ro", uri=True)
    try:
        existing = read_only.execute(
            "SELECT delta_id,delta_sequence,disposition FROM delta_record WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    finally:
        read_only.close()
    if existing:
        result = {
            "status": "IDEMPOTENT_REPLAY",
            "delta_id": existing[0],
            "delta_sequence": existing[1],
            "disposition": existing[2],
            "classified_lanes": ["plan"],
            "mutation_receipt": None,
        }
        result["goal_prompt"] = build_plan_goal_prompt(root, actor=actor)
        _update_pointer_projection(root)
        return result

    def mutate(connection: sqlite3.Connection) -> dict[str, Any]:
        nonlocal result
        existing = connection.execute(
            "SELECT delta_id,delta_sequence,disposition FROM delta_record WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            result = {
                "status": "IDEMPOTENT_REPLAY",
                "delta_id": existing[0],
                "delta_sequence": existing[1],
                "disposition": existing[2],
            }
            return result
        sequence = int(connection.execute("SELECT COALESCE(MAX(delta_sequence),0)+1 FROM delta_record").fetchone()[0])
        prior = connection.execute(
            "SELECT current_delta_id FROM canonical_delta_pointer WHERE singleton_id=1"
        ).fetchone()[0]
        delta_id = _stable_id("delta", "INITIAL_PLAN", source_id, source_sha256)
        now = _utc_now()
        lanes = ["plan"]
        hil_gate = "HIL" if re.search(r"\bHIL\b", content, flags=re.IGNORECASE) else None
        _insert_universal_delta_projection(
            connection,
            source_id=source_id,
            source_type="plan_initial_source",
            original_name=original_name,
            canonical_path=canonical_path,
            source_sha256=source_sha256,
            content=content,
            turn_id=turn_id,
            delta_id=delta_id,
            delta_kind="INITIAL_PLAN",
        )
        connection.execute(
            "INSERT INTO delta_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (delta_id, sequence, "INITIAL_PLAN", "plan", _json(lanes), "CANONICAL_INITIAL_PLAN",
             "INITIAL_PLAN", content, content_sha256, source_id, source_sha256, "UNIVERSAL",
             "source_intake", "APPROVED", prior, None, hil_gate, "PURSUE_PLAN_UNTIL_HIL",
             idempotency_key, turn_id, now, now),
        )
        _register_lane_links(connection, delta_id, sequence, lanes, "APPROVED")
        _event(
            connection,
            delta_id=delta_id,
            before=None,
            after="APPROVED",
            actor=actor,
            reason="initial Plan source accepted as canonical delta zero",
            turn_id=turn_id,
        )
        connection.execute(
            "UPDATE canonical_delta_pointer SET current_delta_id=?,latest_sequence=?,current_plan_delta_id=?,"
            "status='ACTIVE',updated_at=? WHERE singleton_id=1",
            (delta_id, sequence, delta_id, now),
        )
        connection.execute(
            "UPDATE sector_head SET latest_turn_id=?,current_hash=?,next_expected_index=?,status='ACTIVE' WHERE head_sequence=1",
            (turn_id, content_sha256, f"delta_{sequence + 1:06d}"),
        )
        connection.execute(
            "UPDATE sector_meta SET payload_state='POPULATED' WHERE sector_id='delta'"
        )
        result = {
            "status": "CREATED",
            "delta_id": delta_id,
            "delta_sequence": sequence,
            "disposition": "APPROVED",
            "classified_lanes": lanes,
        }
        return result

    receipt = governed_sector_mutation(
        root,
        DELTA_SECTOR_ID,
        actor=actor,
        reason="register normal Plan intake as canonical initial delta",
        turn_id=turn_id,
        mutate=mutate,
    )
    result["mutation_receipt"] = receipt.receipt_path
    goal = build_plan_goal_prompt(root, actor=actor)
    result["goal_prompt"] = goal
    _update_pointer_projection(root)
    return result


def append_classified_steer_delta(
    brain_root: str | Path,
    content: str,
    *,
    actor: str,
    model_name: str,
    turn_id: str,
    model_family: str = "CODEX",
    explicit_lanes: Iterable[str] | None = None,
    supersedes_delta_id: str | None = None,
    next_action: str = "CONTINUE_SAME_LINEAR_GOAL_UNTIL_HIL",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    ensure_plan_delta_sector(root)
    content = str(content or "").strip()
    if not content or not actor.strip() or not model_name.strip() or not turn_id.strip():
        raise PlanDeltaGovernanceError("STEER_CONTENT_ACTOR_MODEL_TURN_REQUIRED")
    lanes = _classify_lanes(content, explicit_lanes)
    steer_kind = _steer_kind(content)
    content_sha256 = _sha256_text(content)
    idempotency_key = idempotency_key or _stable_id(
        "steer_idempotency", model_name, turn_id, content_sha256, supersedes_delta_id or ""
    )
    result: dict[str, Any] = {}

    _, delta_database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    read_only = sqlite3.connect(f"file:{delta_database.as_posix()}?mode=ro", uri=True)
    try:
        existing = read_only.execute(
            "SELECT delta_id,delta_sequence,disposition,classified_lanes_json FROM delta_record WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    finally:
        read_only.close()
    if existing:
        result = {
            "status": "IDEMPOTENT_REPLAY",
            "delta_id": existing[0],
            "delta_sequence": existing[1],
            "disposition": existing[2],
            "classified_lanes": json.loads(existing[3]),
            "mutation_receipt": None,
        }
        result["goal_prompt"] = build_plan_goal_prompt(root, actor=actor)
        _update_pointer_projection(root)
        return result

    def mutate(connection: sqlite3.Connection) -> dict[str, Any]:
        nonlocal result
        existing = connection.execute(
            "SELECT delta_id,delta_sequence,disposition,classified_lanes_json FROM delta_record WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            result = {
                "status": "IDEMPOTENT_REPLAY",
                "delta_id": existing[0],
                "delta_sequence": existing[1],
                "disposition": existing[2],
                "classified_lanes": json.loads(existing[3]),
            }
            return result
        pointer = connection.execute(
            "SELECT current_delta_id,latest_sequence,current_plan_delta_id FROM canonical_delta_pointer WHERE singleton_id=1"
        ).fetchone()
        prior_delta_id = pointer[0]
        sequence = int(pointer[1]) + 1
        now = _utc_now()
        if supersedes_delta_id:
            target = connection.execute(
                "SELECT disposition FROM delta_record WHERE delta_id=?", (supersedes_delta_id,)
            ).fetchone()
            if not target:
                raise PlanDeltaGovernanceError(f"DELTA_SUPERSEDE_TARGET_NOT_FOUND:{supersedes_delta_id}")
            connection.execute(
                "UPDATE delta_record SET disposition='SUPERSEDED',updated_at=? WHERE delta_id=?",
                (now, supersedes_delta_id),
            )
            connection.execute(
                "UPDATE delta_lane_link SET disposition='SUPERSEDED' WHERE delta_id=?",
                (supersedes_delta_id,),
            )
            _event(
                connection,
                delta_id=supersedes_delta_id,
                before=target[0],
                after="SUPERSEDED",
                actor=actor,
                reason="explicitly superseded by a later classified steer",
                turn_id=turn_id,
            )
        elif prior_delta_id:
            prior = connection.execute(
                "SELECT disposition FROM delta_record WHERE delta_id=?", (prior_delta_id,)
            ).fetchone()
            if prior and prior[0] == "PENDING":
                connection.execute(
                    "UPDATE delta_record SET disposition='APPROVED',updated_at=? WHERE delta_id=?",
                    (now, prior_delta_id),
                )
                connection.execute(
                    "UPDATE delta_lane_link SET disposition='APPROVED' WHERE delta_id=?",
                    (prior_delta_id,),
                )
                _event(
                    connection,
                    delta_id=prior_delta_id,
                    before="PENDING",
                    after="APPROVED",
                    actor=actor,
                    reason="locked truth because the next steer continued without rejecting it",
                    turn_id=turn_id,
                )
        delta_id = _stable_id("delta", sequence, model_name, turn_id, content_sha256)
        source_id = _stable_id("delta_source", model_name, turn_id, content_sha256)
        hil_gate = "HIL" if re.search(r"\bHIL\b", content, flags=re.IGNORECASE) else None
        _insert_universal_delta_projection(
            connection,
            source_id=source_id,
            source_type="user_steer",
            original_name=f"{steer_kind} {turn_id}",
            canonical_path=f"lineage://{model_name}/{turn_id}",
            source_sha256=content_sha256,
            content=content,
            turn_id=turn_id,
            delta_id=delta_id,
            delta_kind="STEER",
        )
        connection.execute(
            "INSERT INTO delta_record VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (delta_id, sequence, "STEER", lanes[0], _json(lanes), "CLASSIFIED_USER_STEER",
             steer_kind, content, content_sha256, source_id, content_sha256, model_family.upper(),
             model_name, "PENDING", prior_delta_id, supersedes_delta_id, hil_gate, next_action,
             idempotency_key, turn_id, now, now),
        )
        _register_lane_links(connection, delta_id, sequence, lanes, "PENDING")
        _event(
            connection,
            delta_id=delta_id,
            before=None,
            after="PENDING",
            actor=actor,
            reason="classified additive steer registered",
            turn_id=turn_id,
        )
        current_plan = delta_id if "plan" in lanes else pointer[2]
        connection.execute(
            "UPDATE canonical_delta_pointer SET current_delta_id=?,latest_sequence=?,current_plan_delta_id=?,"
            "status='ACTIVE',updated_at=? WHERE singleton_id=1",
            (delta_id, sequence, current_plan, now),
        )
        connection.execute(
            "UPDATE sector_head SET latest_turn_id=?,current_hash=?,next_expected_index=?,status='ACTIVE' WHERE head_sequence=1",
            (turn_id, content_sha256, f"delta_{sequence + 1:06d}"),
        )
        connection.execute("UPDATE sector_meta SET payload_state='POPULATED' WHERE sector_id='delta'")
        result = {
            "status": "CREATED",
            "delta_id": delta_id,
            "delta_sequence": sequence,
            "disposition": "PENDING",
            "classified_lanes": lanes,
            "steer_kind": steer_kind,
            "supersedes_delta_id": supersedes_delta_id,
        }
        return result

    receipt = governed_sector_mutation(
        root,
        DELTA_SECTOR_ID,
        actor=actor,
        reason="append classified user steer delta",
        turn_id=turn_id,
        mutate=mutate,
    )
    result["mutation_receipt"] = receipt.receipt_path
    result["goal_prompt"] = build_plan_goal_prompt(root, actor=actor)
    _update_pointer_projection(root)
    return result


def set_delta_disposition(
    brain_root: str | Path,
    delta_id: str,
    disposition: str,
    *,
    actor: str,
    reason: str,
    turn_id: str,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    ensure_plan_delta_sector(root)
    disposition = str(disposition or "").upper().strip()
    if disposition not in DELTA_DISPOSITIONS:
        raise PlanDeltaGovernanceError(f"DELTA_DISPOSITION_INVALID:{disposition}")
    result: dict[str, Any] = {}

    _, delta_database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    read_only = sqlite3.connect(f"file:{delta_database.as_posix()}?mode=ro", uri=True)
    try:
        current = read_only.execute(
            "SELECT disposition,delta_sequence FROM delta_record WHERE delta_id=?", (delta_id,)
        ).fetchone()
    finally:
        read_only.close()
    if not current:
        raise PlanDeltaGovernanceError(f"DELTA_NOT_FOUND:{delta_id}")
    if current[0] == disposition:
        result = {
            "status": "IDEMPOTENT_REPLAY",
            "delta_id": delta_id,
            "delta_sequence": current[1],
            "previous_disposition": current[0],
            "disposition": disposition,
            "mutation_receipt": None,
        }
        result["goal_prompt"] = build_plan_goal_prompt(root, actor=actor)
        _update_pointer_projection(root)
        return result

    def mutate(connection: sqlite3.Connection) -> dict[str, Any]:
        nonlocal result
        row = connection.execute(
            "SELECT disposition,delta_sequence FROM delta_record WHERE delta_id=?", (delta_id,)
        ).fetchone()
        if not row:
            raise PlanDeltaGovernanceError(f"DELTA_NOT_FOUND:{delta_id}")
        before = row[0]
        if before != disposition:
            now = _utc_now()
            connection.execute(
                "UPDATE delta_record SET disposition=?,updated_at=? WHERE delta_id=?",
                (disposition, now, delta_id),
            )
            connection.execute(
                "UPDATE delta_lane_link SET disposition=? WHERE delta_id=?",
                (disposition, delta_id),
            )
            _event(
                connection,
                delta_id=delta_id,
                before=before,
                after=disposition,
                actor=actor,
                reason=reason,
                turn_id=turn_id,
            )
            connection.execute(
                "UPDATE sector_head SET latest_turn_id=?,status='ACTIVE' WHERE head_sequence=1",
                (turn_id,),
            )
        result = {
            "status": "UPDATED" if before != disposition else "IDEMPOTENT_REPLAY",
            "delta_id": delta_id,
            "delta_sequence": row[1],
            "previous_disposition": before,
            "disposition": disposition,
        }
        return result

    receipt = governed_sector_mutation(
        root,
        DELTA_SECTOR_ID,
        actor=actor,
        reason=f"set delta disposition to {disposition}: {reason}",
        turn_id=turn_id,
        mutate=mutate,
    )
    result["mutation_receipt"] = receipt.receipt_path
    result["goal_prompt"] = build_plan_goal_prompt(root, actor=actor)
    _update_pointer_projection(root)
    return result


def _active_goal_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return list(
        connection.execute(
            "SELECT * FROM delta_record WHERE disposition NOT IN ('SUPERSEDED','DROPPED','FAILED') "
            "ORDER BY delta_sequence"
        )
    )


def build_plan_goal_prompt(
    brain_root: str | Path,
    *,
    actor: str = "Evidence OS Prompt 2 Generator",
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    ensure_plan_delta_sector(root)
    _, database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    read_only = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = _active_goal_rows(read_only)
    finally:
        read_only.close()
    plan_rows = [row for row in rows if "plan" in json.loads(row["classified_lanes_json"])]
    hil_rows = [row for row in rows if row["hil_gate"]]
    hil_gate = str(hil_rows[-1]["hil_gate"]) if hil_rows else None
    status = "HIL_GATE_DECLARED" if hil_gate else "FIRST_HIL_GATE_REQUIRED"
    source_delta_id = str(plan_rows[-1]["delta_id"]) if plan_rows else (str(rows[-1]["delta_id"]) if rows else None)
    ledger_lines = [
        f"Delta {row['delta_sequence']} [{row['disposition']}] lanes={','.join(json.loads(row['classified_lanes_json']))}:\n{row['content']}"
        for row in plan_rows
    ]
    if not ledger_lines:
        ledger_lines = ["No Plan source has been loaded. Preserve the empty Plan sector and request the initial Plan."]
    hil_instruction = (
        f"Pursue the goal through the declared HIL gate: {hil_gate}. Stop only at that HIL gate or an explicit resume/handoff command."
        if hil_gate
        else "No HIL gate is declared in the canonical Plan delta chain. In the compact exit slip, ask the user to define the first HIL gate before execution."
    )
    text = (
        "PROMPT 2 — CANONICAL LINEAR GOAL\n\n"
        "Pursue the canonical Plan goal from the accepted current state and its additive delta ledger. "
        "Do not restart from baseline, silently drop a delta, or mix this Plan/steer ledger with Refresh deltas.\n\n"
        f"{hil_instruction}\n\n"
        "CANONICAL PLAN DELTA CHAIN\n"
        + "\n\n".join(ledger_lines)
        + "\n\nRun compact model-specific entry and exit slips. Treat APPROVED and PENDING current deltas as active; retain superseded/changed/failed history in the database.\n"
    )
    text_sha256 = _sha256_text(text)
    prompt_id = _stable_id("goal_prompt", 2, source_delta_id or "empty", text_sha256)
    turn_id = f"prompt2:{text_sha256[:20]}"
    result: dict[str, Any] = {}

    read_only = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        existing_prompt = read_only.execute(
            "SELECT prompt_id,status,hil_gate,canonical_path FROM goal_prompt WHERE prompt_id=?",
            (prompt_id,),
        ).fetchone()
        latest_prompt_id = read_only.execute(
            "SELECT latest_prompt_id FROM canonical_delta_pointer WHERE singleton_id=1"
        ).fetchone()[0]
    finally:
        read_only.close()
    if existing_prompt and latest_prompt_id == prompt_id:
        prompt_path = _safe_project_path(root, DELTA_PROMPT2_RELATIVE)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(text, encoding="utf-8")
        _update_pointer_projection(root)
        return {
            "write_status": "IDEMPOTENT_REPLAY",
            "prompt_id": existing_prompt[0],
            "status": existing_prompt[1],
            "hil_gate": existing_prompt[2],
            "prompt_index": 2,
            "text": text,
            "text_sha256": text_sha256,
            "path": str(prompt_path),
            "mutation_receipt": None,
        }

    def mutate(connection: sqlite3.Connection) -> dict[str, Any]:
        nonlocal result
        existing = connection.execute(
            "SELECT prompt_id,status,hil_gate,canonical_path FROM goal_prompt WHERE prompt_id=?",
            (prompt_id,),
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE canonical_delta_pointer SET latest_prompt_id=?,updated_at=? WHERE singleton_id=1",
                (prompt_id, _utc_now()),
            )
            result = {
                "write_status": "IDEMPOTENT_REPLAY",
                "prompt_id": existing[0],
                "status": existing[1],
                "hil_gate": existing[2],
            }
            return result
        now = _utc_now()
        connection.execute("UPDATE goal_prompt SET is_current=0 WHERE is_current=1")
        connection.execute(
            "INSERT INTO goal_prompt VALUES(?,?,?,?,?,?,?,?,?,?)",
            (prompt_id, 2, source_delta_id, status, hil_gate, text, text_sha256,
             DELTA_PROMPT2_RELATIVE, now, 1),
        )
        connection.execute(
            "UPDATE canonical_delta_pointer SET latest_prompt_id=?,updated_at=? WHERE singleton_id=1",
            (prompt_id, now),
        )
        result = {
            "write_status": "CREATED",
            "prompt_id": prompt_id,
            "status": status,
            "hil_gate": hil_gate,
        }
        return result

    receipt = governed_sector_mutation(
        root,
        DELTA_SECTOR_ID,
        actor=actor,
        reason="materialize distinct Prompt 2 from canonical Plan delta chain",
        turn_id=turn_id,
        mutate=mutate,
    )
    prompt_path = _safe_project_path(root, DELTA_PROMPT2_RELATIVE)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(text, encoding="utf-8")
    result.update(
        {
            "prompt_index": 2,
            "text": text,
            "text_sha256": text_sha256,
            "path": str(prompt_path),
            "mutation_receipt": receipt.receipt_path,
        }
    )
    _update_pointer_projection(root)
    return result


def record_model_state_slip(
    brain_root: str | Path,
    *,
    slip_kind: str,
    model_family: str,
    model_name: str,
    prompt_index: int,
    gates: Iterable[str],
    lanes: Iterable[str],
    turn_id: str,
    token_count: int | None,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    ensure_plan_delta_sector(root)
    slip_kind = str(slip_kind).upper().strip()
    if slip_kind not in {"ENTRY", "EXIT"}:
        raise PlanDeltaGovernanceError(f"STATE_SLIP_KIND_INVALID:{slip_kind}")
    gates_list = [str(value).strip() for value in gates if str(value).strip()]
    lanes_list = [str(value).strip() for value in lanes if str(value).strip()]
    metric_state = "EXACT" if token_count is not None else "UNAVAILABLE"
    token_text = str(int(token_count)) if token_count is not None else "unavailable"
    compact_line = (
        f"{slip_kind}|model={model_name}|family={model_family}|prompt=P{int(prompt_index)}|"
        f"gates={','.join(gates_list) or 'none'}|lanes={','.join(lanes_list) or 'none'}|"
        f"tokens={token_text}|turn={turn_id}"
    )
    idempotency_key = _stable_id("state_slip", compact_line)
    slip_id = _stable_id("slip", idempotency_key)
    result: dict[str, Any] = {}

    _, delta_database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    read_only = sqlite3.connect(f"file:{delta_database.as_posix()}?mode=ro", uri=True)
    try:
        existing = read_only.execute(
            "SELECT slip_id,token_metric_state,compact_line FROM state_slip WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    finally:
        read_only.close()
    if existing:
        _update_pointer_projection(root)
        return {
            "status": "IDEMPOTENT_REPLAY",
            "slip_id": existing[0],
            "token_metric_state": existing[1],
            "compact_line": existing[2],
            "mutation_receipt": None,
        }

    def mutate(connection: sqlite3.Connection) -> dict[str, Any]:
        nonlocal result
        existing = connection.execute(
            "SELECT slip_id,token_metric_state,compact_line FROM state_slip WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            result = {
                "status": "IDEMPOTENT_REPLAY",
                "slip_id": existing[0],
                "token_metric_state": existing[1],
                "compact_line": existing[2],
            }
            return result
        connection.execute(
            "INSERT INTO state_slip VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slip_id, slip_kind, model_family, model_name, int(prompt_index), _json(gates_list),
             _json(lanes_list), token_count, metric_state, compact_line, turn_id,
             idempotency_key, _utc_now()),
        )
        result = {
            "status": "CREATED",
            "slip_id": slip_id,
            "token_metric_state": metric_state,
            "compact_line": compact_line,
        }
        return result

    receipt = governed_sector_mutation(
        root,
        DELTA_SECTOR_ID,
        actor=f"{model_family}:{model_name}",
        reason=f"append compact {slip_kind.lower()} slip",
        turn_id=turn_id,
        mutate=mutate,
    )
    result["mutation_receipt"] = receipt.receipt_path
    _update_pointer_projection(root)
    return result


def read_plan_delta_state(brain_root: str | Path) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    ensure_plan_delta_sector(root)
    _, database = resolve_env15_sector(root, DELTA_SECTOR_ID)
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        deltas = []
        for row in connection.execute("SELECT * FROM delta_record ORDER BY delta_sequence"):
            item = dict(row)
            item["classified_lanes"] = json.loads(item.pop("classified_lanes_json"))
            deltas.append(item)
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM delta_disposition_event ORDER BY created_at,event_id"
        )]
        lanes = [dict(row) for row in connection.execute(
            "SELECT * FROM delta_lane_registry ORDER BY first_sequence,lane_id"
        )]
        prompts = [dict(row) for row in connection.execute(
            "SELECT * FROM goal_prompt ORDER BY created_at,prompt_id"
        )]
        slips = [dict(row) for row in connection.execute(
            "SELECT * FROM state_slip ORDER BY created_at,slip_id"
        )]
        pointer = dict(connection.execute(
            "SELECT * FROM canonical_delta_pointer WHERE singleton_id=1"
        ).fetchone())
    finally:
        connection.close()
    return {
        "contract": DELTA_SCHEMA_VERSION,
        "database_path": str(database),
        "refresh_delta_ledger": str(root / REFRESH_DELTA_LEDGER_RELATIVE),
        "refresh_ledger_relationship": "DISTINCT_DO_NOT_MIX",
        "canonical_pointer": pointer,
        "deltas": deltas,
        "disposition_events": events,
        "lanes": lanes,
        "goal_prompts": prompts,
        "state_slips": slips,
    }


__all__ = [
    "DELTA_DATABASE_RELATIVE",
    "DELTA_DISPOSITIONS",
    "DELTA_SCHEMA_VERSION",
    "DELTA_SECTOR_TABLES",
    "PlanDeltaGovernanceError",
    "REFRESH_DELTA_LEDGER_RELATIVE",
    "append_classified_steer_delta",
    "build_plan_goal_prompt",
    "ensure_plan_delta_sector",
    "read_plan_delta_state",
    "record_model_state_slip",
    "register_initial_plan_delta",
    "set_delta_disposition",
]
