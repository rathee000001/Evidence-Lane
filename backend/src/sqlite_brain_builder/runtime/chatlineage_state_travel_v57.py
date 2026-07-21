from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import zipfile
from pathlib import Path


PATCH_ID = "V5.9_EVIDENCE_LANE_ENV_UOP_RESEARCH_LINEAGE_STATE_TRAVEL"
LAW_MARKER = "EVIDENCE_LANE_ENV_UOP_RESEARCH_LINEAGE_STATE_TRAVEL_V59"
CONTRACT_ID = "evidence_lane_env_uop_research_lineage_state_travel_v59"


STATE_TRAVEL_LAW = f"""
<!-- {LAW_MARKER} -->

## PUBLIC MODEL RESEARCH + CHAT LINEAGE STATE-TRAVEL LAW

This package uses exactly two locked read-only authorities, Env and UOP, plus the live generated Project sector graph.

Public-model project access is read-only. Provider identity never owns project-change truth, and no
ChatGPT, Gemini, Ollama, or other provider-specific project Delta exists. General project-section
writing is blocked unless the user explicitly names the exact destination sector. Classification
routes authorized content; classification never grants write permission.

Model outputs, downloadable files, proposed patches, exposed resource usage, and artifact references
are append-only lineage/research evidence. They do not prove that a local project changed and they
must not be applied automatically. Only universal Refresh may compare the active local working project
with the previously accepted mapped state and create project-change truth.

This V5.9 contract supersedes the older single automatic Chat Lineage lane rule.
Exactly two append-only automatic write targets are mandatory for every serious model turn:

1. Exact visible state travel into:

`project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite`

2. The user question and visible candidate answer into:

`project/sectors/research/research_sector_v001.sqlite`

Research classification targets:

- user prompt / command -> `research_question`
- visible assistant answer -> `research_finding`
- endpoint/model/package pointers and authoritative usage -> `research_evidence`
- candidate-only, HIL-pending, or unavailable-metric boundaries -> `research_limitation`

Required append packet:

1. user prompt / command
2. assistant response
3. visible telemetry summary / visible reasoning summary only
4. entry slip
5. exit slip
6. gates fired
7. operators fired
8. write scope
9. file/link/name index from uploaded package, generated files, sandbox links, and referenced artifacts
10. output artifact references
11. prompt-response relation

Hard boundaries:

- Do not write to `local_code` unless user explicitly commands a code-sector update.
- Do not write to `docs`, `data`, `ppt`, `pdf_ocr`, `images_ocr`, `artifacts`, `analysis`, `plan`, or `mode` unless user explicitly commands that sector write.
- Research and Chat Lineage append records remain candidate state and never imply automatic Refresh, Fuse, HIL approval, or truth promotion.
- Importing a lightweight state-travel append packet never triggers Refresh and never mutates another project sector.
- Proposed code or patch output remains `PROPOSED_NOT_APPLIED` until the user or an authorized external-code writer applies it to the local working copy.
- Universal Refresh later detects that actual local change, independent of provider provenance.
- Do not mutate Env law except under explicit `mode=flash_env`.
- Do not mutate UOP law except under explicit `mode=flash_uop`.
- Resolve all Project work through the universal live-lane pointers.
- Do not store hidden chain-of-thought.
- Store only visible telemetry / visible reasoning summary that is safe to show to the user.
- Chat window is display/runtime only; durable state is the package.
- If the model cannot actually write a new package/DB, it must say that the append is pending and provide a user-reviewable append packet.

Append-only table targets:

- `lineage_turn`
- `lineage_prompt`
- `lineage_response`
- `lineage_visible_reasoning`
- `lineage_file_link_index`
- `lineage_artifact_reference`
- `lineage_prompt_response_relation`
- `lineage_fts`

Research append-only table targets:

- `research_source`
- `research_question`
- `research_finding`
- `research_evidence`
- `research_limitation`
- `research_receipt` (written only by the governed app ingestion path)

Writing to any other generated project sector remains command-gated. If the runtime cannot mutate and revalidate the package, return both Research and Chat Lineage append packets as pending writeback.
"""

ZERO_GAP_FLASH_PROMPT_APPEND = f"""
<!-- {LAW_MARKER} -->

PUBLIC MODEL RESEARCH + CHAT LINEAGE WRITEBACK RULE:

After reading the package pointers and before closing every serious response, append this turn into the generated project chat_lineage sector:

`project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite`

Append packet must include:

- user prompt
- assistant response
- visible telemetry summary / visible reasoning summary only
- entry slip
- exit slip
- gates fired
- operators fired
- write scope
- file/link/name index
- artifact/output references
- prompt-response relation

Also append the prompt as a Research question, the visible answer as a Research finding,
the model/package/token facts as Research evidence, and the candidate/HIL boundary as a Research limitation.

Research append target:

`project/sectors/research/research_sector_v001.sqlite`

Research and Chat Lineage are the only two automatic append-write targets. Neither append promotes candidate work.

Public-model project access remains read-only. Provider output is proposed evidence, not project truth.
Only universal Refresh may detect actual local working-project changes. Never create a provider-specific project Delta.

All other project-sector writes require explicit user command, for example:
"log this patch as commit history"
"log this failure to analysis"
"write this accepted plan into plan sector"

Never store hidden chain-of-thought.
Never mutate Env or UOP from chat lineage.
If DB/package writeback is impossible in this runtime, output both append packets and mark them as PENDING_USER_PACKAGE_WRITEBACK.
"""


GEMINI_FLASH_APPEND = f"""
<!-- {LAW_MARKER} -->

GEMINI STATE-TRAVEL RULE:

For Gemini-compatible packages, read the exact root files / universal pointer first.
Use `PROJECT_CONJOINED_WRITE.sqlite` as the generated project view if present.
Read the root Env and UOP authority pointers and their read-only SQLite files directly.
The original full package may be sealed as a closed payload; do not unpack it unless the user asks.

Every serious turn must still create two append packets:
1. chat_lineage: prompt, response, visible telemetry summary, entry/exit slips, gates/operators, file/link/name index, outputs.
2. research: prompt as question, visible answer as finding, model/package/token facts as evidence, and candidate/HIL boundary as limitation.

If Gemini cannot mutate the ZIP, return both append packets as pending writeback.

The packet is lightweight append-only state travel. It never applies model output to project sectors,
never triggers Refresh, and never creates Gemini-specific project-change truth.
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=FULL")
    return con


def append_text_once(path: Path, block: str) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    if LAW_MARKER in txt:
        return True
    path.write_text(txt.rstrip() + "\n\n" + block.strip() + "\n", encoding="utf-8")
    return True


def ensure_env_contract(env_db: Path) -> bool:
    if not env_db.exists():
        return False

    con = _connect(env_db)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS chat_lineage_append_only_contract(
            contract_id TEXT PRIMARY KEY,
            contract_version TEXT,
            law_marker TEXT,
            append_sector_path TEXT,
            automatic_write_scope TEXT,
            blocked_write_scope TEXT,
            hidden_cot_storage_allowed INTEGER,
            visible_telemetry_storage_allowed INTEGER,
            created_at TEXT,
            rule_text TEXT
        );

        CREATE TABLE IF NOT EXISTS prompt_turn(
            turn_id TEXT PRIMARY KEY,
            chat_name TEXT,
            mode TEXT,
            user_prompt_sha256 TEXT,
            response_sha256 TEXT,
            visible_reasoning_sha256 TEXT,
            package_pointer TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS prompt_chunk(
            chunk_id TEXT PRIMARY KEY,
            turn_id TEXT,
            chunk_index INTEGER,
            text_sha256 TEXT,
            text_preview TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS response_chunk(
            chunk_id TEXT PRIMARY KEY,
            turn_id TEXT,
            chunk_index INTEGER,
            text_sha256 TEXT,
            text_preview TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS visible_reasoning_chunk(
            chunk_id TEXT PRIMARY KEY,
            turn_id TEXT,
            chunk_index INTEGER,
            text_sha256 TEXT,
            text_preview TEXT,
            hidden_cot_bool INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS indexed_content_chunk(
            chunk_id TEXT PRIMARY KEY,
            turn_id TEXT,
            entity_type TEXT,
            entity_name TEXT,
            entity_path_or_url TEXT,
            entity_sha256 TEXT,
            metadata_json TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS project_turn_packet(
            packet_id TEXT PRIMARY KEY,
            turn_id TEXT,
            packet_json TEXT,
            append_status TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS prompt_response_relation(
            relation_id TEXT PRIMARY KEY,
            turn_id TEXT,
            prompt_chunk_id TEXT,
            response_chunk_id TEXT,
            relation_type TEXT,
            created_at TEXT
        );
    """)

    con.execute(
        """
        INSERT OR REPLACE INTO chat_lineage_append_only_contract
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            CONTRACT_ID,
            "v5.8",
            LAW_MARKER,
            "project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite;project/sectors/research/research_sector_v001.sqlite",
            "append-only research question/finding/evidence/limitation plus exact chat-lineage state travel",
            "all non-research and non-chat_lineage project sectors unless explicit user command",
            0,
            1,
            now(),
            STATE_TRAVEL_LAW.strip(),
        ),
    )
    con.commit()
    con.close()
    return True


def ensure_chat_lineage_sector(project_root: Path) -> Path:
    db = project_root / "sectors" / "chat_lineage" / "chat_lineage_sector_v001.sqlite"
    con = _connect(db)

    con.executescript("""
        CREATE TABLE IF NOT EXISTS lineage_source(
            source_id TEXT PRIMARY KEY,
            source_type TEXT,
            name TEXT,
            path TEXT,
            source_sha256 TEXT,
            metadata_json TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_turn(
            turn_id TEXT PRIMARY KEY,
            chat_name TEXT,
            mode TEXT,
            user_prompt TEXT,
            assistant_response TEXT,
            visible_reasoning_summary TEXT,
            entry_slip TEXT,
            exit_slip TEXT,
            gates_fired_json TEXT,
            operators_fired_json TEXT,
            write_scope TEXT,
            output_index_json TEXT,
            file_link_index_json TEXT,
            append_status TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_prompt(
            prompt_id TEXT PRIMARY KEY,
            turn_id TEXT,
            prompt_text TEXT,
            prompt_sha256 TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_response(
            response_id TEXT PRIMARY KEY,
            turn_id TEXT,
            response_text TEXT,
            response_sha256 TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_visible_reasoning(
            visible_reasoning_id TEXT PRIMARY KEY,
            turn_id TEXT,
            visible_summary TEXT,
            visible_summary_sha256 TEXT,
            hidden_cot_bool INTEGER DEFAULT 0,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_file_link_index(
            link_id TEXT PRIMARY KEY,
            turn_id TEXT,
            file_name TEXT,
            file_path_or_url TEXT,
            file_role TEXT,
            file_sha256 TEXT,
            metadata_json TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_artifact_reference(
            artifact_ref_id TEXT PRIMARY KEY,
            turn_id TEXT,
            artifact_name TEXT,
            artifact_path_or_url TEXT,
            relation_type TEXT,
            artifact_sha256 TEXT,
            metadata_json TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_prompt_response_relation(
            relation_id TEXT PRIMARY KEY,
            turn_id TEXT,
            prompt_id TEXT,
            response_id TEXT,
            relation_type TEXT,
            metadata_json TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS lineage_append_receipt(
            receipt_id TEXT PRIMARY KEY,
            turn_id TEXT,
            receipt_text TEXT,
            receipt_sha256 TEXT,
            created_at TEXT
        );
    """)

    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lineage_fts USING fts5(entity_id, text)")
    except Exception:
        pass

    # Compatibility for older lane-shell DBs: lineage_source may have additive columns.
    try:
        _cols = [r[1] for r in con.execute("PRAGMA table_info(lineage_source)")]
        for _name, _typ in [("source_type", "TEXT"), ("source_sha256", "TEXT")]:
            if _cols and _name not in _cols:
                con.execute(f"ALTER TABLE lineage_source ADD COLUMN {_name} {_typ}")
    except Exception:
        pass

    # Append-only hard boundary for the new state-travel tables.
    # Existing older lineage tables are left alone for compatibility.
    for table in [
        "lineage_turn",
        "lineage_prompt",
        "lineage_response",
        "lineage_visible_reasoning",
        "lineage_file_link_index",
        "lineage_artifact_reference",
        "lineage_prompt_response_relation",
        "lineage_append_receipt",
    ]:
        con.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'APPEND_ONLY_CHAT_LINEAGE_NO_UPDATE');
            END;
        """)
        con.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {table}_append_only_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'APPEND_ONLY_CHAT_LINEAGE_NO_DELETE');
            END;
        """)

    con.execute(
        """
        INSERT OR IGNORE INTO lineage_source(source_id, source_type, name, path, source_sha256, metadata_json, created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            CONTRACT_ID,
            "ENV_RUNTIME_RULE",
            "Public model Research and Chat Lineage state-travel contract",
            "env/env_sqlite.sqlite::chat_lineage_append_only_contract",
            sha256_text(STATE_TRAVEL_LAW),
            json.dumps(
                {
                    "law_marker": LAW_MARKER,
                    "automatic_append_targets": ["research", "chat_lineage"],
                    "candidate_truth_promotion": False,
                },
                indent=2,
            ),
            now(),
        ),
    )

    try:
        con.execute(
            "INSERT INTO lineage_fts(entity_id, text) VALUES(?,?)",
            (CONTRACT_ID, STATE_TRAVEL_LAW),
        )
    except Exception:
        pass

    con.commit()
    con.close()
    return db


def ensure_research_sector(project_root: Path) -> Path:
    db = project_root / "sectors" / "research" / "research_sector_v001.sqlite"
    if not db.is_file():
        raise RuntimeError(f"RESEARCH_SECTOR_REQUIRED:{db}")
    required = {
        "research_source",
        "research_question",
        "research_finding",
        "research_evidence",
        "research_limitation",
    }
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
    finally:
        con.close()
    present = required & tables
    missing = sorted(required - tables)
    if present and missing:
        raise RuntimeError(f"RESEARCH_SECTOR_SCHEMA_MISSING:{','.join(missing)}")
    return db


def enable_model_append_authority(project_root: Path) -> dict:
    """Make Research and Chat Lineage the two model-package append services."""

    router = project_root / "project_router.sqlite"
    research = ensure_research_sector(project_root)
    for path in (router, research):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    router_connection = sqlite3.connect(router)
    try:
        router_connection.execute("PRAGMA foreign_keys=ON")
        router_connection.execute(
            "UPDATE sector_registry SET default_access='APPEND_ONLY_READ_WRITE',"
            "automatic_write=1,explicit_one_turn_grant_required=0,relock_after_commit=1 "
            "WHERE sector_id IN ('chat_lineage','research')"
        )
        if router_connection.total_changes != 2:
            raise RuntimeError("MODEL_APPEND_AUTHORITY_ROUTER_ROWS_MISSING")
        router_connection.commit()
    finally:
        router_connection.close()
    research_connection = sqlite3.connect(research)
    try:
        research_connection.execute("PRAGMA foreign_keys=ON")
        research_tables = [
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
        ]
        observed = {
            str(row[0])
            for row in research_connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        for table in research_tables:
            if table not in observed:
                continue
            for operation in ("UPDATE", "DELETE"):
                trigger = f"model_append_only_{table}_{operation.casefold()}"
                research_connection.execute(
                    f'CREATE TRIGGER IF NOT EXISTS "{trigger}" BEFORE {operation} ON "{table}" '
                    "BEGIN SELECT RAISE(ABORT,'RESEARCH_APPEND_ONLY'); END"
                )
        research_connection.commit()
    finally:
        research_connection.close()
    pointer = project_root / "sectors" / "research" / "research_pointer.json"
    data: dict = {}
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.update(
        {
            "sector_id": "research",
            "sqlite_path": "project/sectors/research/research_sector_v001.sqlite",
            "access": "APPEND_ONLY_READ_WRITE",
            "automatic_write": True,
            "explicit_one_turn_grant_required": False,
            "relock_after_commit": True,
            "law_marker": LAW_MARKER,
            "updated_at": now(),
        }
    )
    pointer.chmod(pointer.stat().st_mode | stat.S_IWUSR) if pointer.exists() else None
    pointer.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "contract": "EVIDENCE_LANE_TWO_AUTOMATIC_APPEND_LANES_V1",
        "status": "PASS",
        "automatic_append_lanes": ["chat_lineage", "research"],
    }


def validate_model_append_authority(project_root: Path) -> dict:
    """Prove the canonical Project already owns the two append-only model lanes."""

    project_root = Path(project_root)
    router = project_root / "project_router.sqlite"
    chat_lineage = project_root / "sectors" / "chat_lineage" / "chat_lineage_sector_v001.sqlite"
    research = ensure_research_sector(project_root)
    for required_path in (router, chat_lineage, research):
        if not required_path.is_file():
            raise RuntimeError(f"MODEL_APPEND_AUTHORITY_DATABASE_MISSING:{required_path}")

    router_connection = sqlite3.connect(f"file:{router.as_posix()}?mode=ro", uri=True)
    try:
        rows = {
            str(row[0]): tuple(row[1:])
            for row in router_connection.execute(
                "SELECT sector_id,default_access,automatic_write,"
                "explicit_one_turn_grant_required,relock_after_commit "
                "FROM sector_registry WHERE sector_id IN ('chat_lineage','research')"
            )
        }
    finally:
        router_connection.close()
    expected_router = ("APPEND_ONLY_READ_WRITE", 1, 0, 1)
    mismatched = sorted(
        lane_id
        for lane_id in ("chat_lineage", "research")
        if rows.get(lane_id) != expected_router
    )
    if mismatched:
        raise RuntimeError("MODEL_APPEND_AUTHORITY_ROUTER_MISMATCH:" + ",".join(mismatched))

    required_tables = {
        "chat_lineage": {
            "lineage_head",
            "prompt_raw_exact",
            "response_raw_visible_exact",
            "turn_prepare",
            "turn_commit",
            "entry_exit_receipt",
        },
        "research": {
            "research_source",
            "research_question",
            "research_finding",
            "research_evidence",
            "research_limitation",
            "research_append_prepare",
            "research_append_commit",
        },
    }
    for lane_id, database in (("chat_lineage", chat_lineage), ("research", research)):
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            observed = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
        finally:
            connection.close()
        missing = sorted(required_tables[lane_id] - observed)
        if missing:
            raise RuntimeError(
                f"MODEL_APPEND_AUTHORITY_SCHEMA_MISSING:{lane_id}:" + ",".join(missing)
            )
    return {
        "contract": "EVIDENCE_LANE_TWO_AUTOMATIC_APPEND_LANES_V2",
        "status": "PASS",
        "authority": "CANONICAL_PROJECT_DATABASES_READ_ONLY_VALIDATED",
        "automatic_append_lanes": ["chat_lineage", "research"],
        "package_database_mutation": False,
    }


def update_chat_lineage_pointer(project_root: Path) -> None:
    pointers = project_root / "pointers"
    pointers.mkdir(parents=True, exist_ok=True)
    pointer = pointers / "chat_lineage_pointer.json"

    data = {}
    if pointer.exists():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data.update(
        {
            "lane_key": "chat_lineage",
            "lane_label": "Chat Lineage",
            "sector_db": "project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite",
            "active": True,
            "mmd_required": False,
            "auto_append_exception": True,
            "append_only": True,
            "allowed_auto_write": [
                "user_prompt",
                "assistant_response",
                "visible_telemetry_summary",
                "entry_slip",
                "exit_slip",
                "gate_receipts",
                "operator_receipts",
                "file_link_index",
                "artifact_references",
                "prompt_response_relation",
            ],
            "blocked_auto_write": [
                "local_code",
                "docs",
                "data_excel_csv",
                "ppt_presentation",
                "pdf_ocr",
                "images_ocr",
                "artifacts",
                "analysis",
                "plan",
                "mode",
                "env",
                "uop",
            ],
            "hidden_chain_of_thought_storage": False,
            "visible_reasoning_summary_storage": True,
            "law_marker": LAW_MARKER,
            "updated_at": now(),
        }
    )
    pointer.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_append_packet_template(project_root: Path) -> None:
    prompts = project_root.parent / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    template = prompts / "CHAT_LINEAGE_APPEND_PACKET_TEMPLATE.json"
    if template.exists():
        try:
            existing = json.loads(template.read_text(encoding="utf-8"))
            if existing.get("law_marker") == LAW_MARKER:
                return
        except Exception:
            pass

    data = {
        "law_marker": LAW_MARKER,
        "append_targets": {
            "chat_lineage": "project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite",
            "research": "project/sectors/research/research_sector_v001.sqlite",
        },
        "append_only": True,
        "turn_packet": {
            "turn_id": "<stable turn id>",
            "chat_name": "<chat name>",
            "mode": "<mode>",
            "user_prompt": "<visible user prompt>",
            "assistant_response": "<visible assistant response>",
            "visible_reasoning_summary": "<visible telemetry summary only; no hidden chain-of-thought>",
            "entry_slip": "<entry slip>",
            "exit_slip": "<exit slip>",
            "gates_fired": [],
            "operators_fired": [],
            "write_scope": "research plus chat_lineage append only unless user explicitly commands another sector",
            "file_link_index": [],
            "artifact_references": [],
        },
        "research_packet": {
            "question": "<visible user prompt>",
            "finding": "<visible assistant response>",
            "evidence": "<model, package pointer, and authoritative usage facts>",
            "limitation": "candidate only; HIL pending; no automatic refresh, fuse, or promotion",
        },
    }
    template.write_text(json.dumps(data, indent=2), encoding="utf-8")


def patch_package_folder(package_folder: Path) -> dict:
    package_folder = Path(package_folder)
    changed = []

    if not package_folder.exists():
        raise RuntimeError(f"PACKAGE_FOLDER_NOT_FOUND: {package_folder}")

    project_root = package_folder / "project"

    model_append_authority = validate_model_append_authority(project_root)

    write_append_packet_template(project_root)
    changed.append("prompts/CHAT_LINEAGE_APPEND_PACKET_TEMPLATE.json")

    for rel, block in [
        ("FLASH_ME_FIRST_SINGLE_PROMPT.txt", ZERO_GAP_FLASH_PROMPT_APPEND),
        ("README_NEXT_PROMPT.txt", ZERO_GAP_FLASH_PROMPT_APPEND),
        ("recovery/relock_prompt.txt", ZERO_GAP_FLASH_PROMPT_APPEND),
    ]:
        if append_text_once(package_folder / rel, block):
            changed.append(rel)

    # If package contains Gemini prompt sidecar, patch text without changing Gemini file-count logic.
    for p in package_folder.rglob("GEMINI_FLASH_PROMPT.txt"):
        if append_text_once(p, GEMINI_FLASH_APPEND):
            try:
                changed.append(str(p.relative_to(package_folder)))
            except Exception:
                changed.append(str(p))

    manifests = package_folder / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    contract = manifests / "PUBLIC_MODEL_RESEARCH_LINEAGE_APPEND_ONLY_CONTRACT.md"
    contract.write_text(STATE_TRAVEL_LAW.strip() + "\n", encoding="utf-8")
    changed.append("manifests/PUBLIC_MODEL_RESEARCH_LINEAGE_APPEND_ONLY_CONTRACT.md")

    receipt_dir = package_folder / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / "PUBLIC_MODEL_RESEARCH_LINEAGE_PATCH_RECEIPT.md"
    receipt.write_text(
        "# Public Model Research and Chat Lineage Patch Receipt\n\n"
        f"patch_id={PATCH_ID}\n"
        f"law_marker={LAW_MARKER}\n"
        f"created_at={now()}\n"
        "locked_read_only_authorities=env;uop\n"
        "project_authority=live_governed_sector_graph\n"
        "automatic_append_targets=project/sectors/research;project/sectors/chat_lineage\n"
        "candidate_truth_promotion=no\n"
        "hidden_chain_of_thought_storage=no\n"
        "visible_telemetry_summary_storage=yes\n"
        "other_project_sector_write_requires_explicit_user_command=yes\n",
        encoding="utf-8",
    )
    changed.append("receipts/PUBLIC_MODEL_RESEARCH_LINEAGE_PATCH_RECEIPT.md")

    # Lightweight package index / hash manifest for changed files.
    patch_manifest = manifests / "PUBLIC_MODEL_RESEARCH_LINEAGE_PATCH_MANIFEST.json"
    indexed = []
    for rel in sorted(set(changed)):
        p = package_folder / rel
        if p.exists() and p.is_file():
            indexed.append({"path": rel, "sha256": sha256_file(p), "size_bytes": p.stat().st_size})
    patch_manifest.write_text(
        json.dumps(
            {
                "patch_id": PATCH_ID,
                "law_marker": LAW_MARKER,
                "created_at": now(),
                "changed_files": indexed,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    changed.append("manifests/PUBLIC_MODEL_RESEARCH_LINEAGE_PATCH_MANIFEST.json")

    return {
        "package_folder": str(package_folder),
        "changed": sorted(set(changed)),
        "model_append_authority": model_append_authority,
    }


def zip_folder_stable(package_folder: Path, zip_path: Path) -> str:
    package_folder = Path(package_folder)
    zip_path = Path(zip_path)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w") as z:
        for file in sorted(package_folder.rglob("*")):
            if not file.is_file():
                continue
            rel = file.relative_to(package_folder).as_posix()
            # Keep PNG stored when possible; deflate text/sqlite/json.
            compress_type = zipfile.ZIP_STORED if file.suffix.lower() == ".png" else zipfile.ZIP_DEFLATED
            z.write(file, rel, compress_type=compress_type)

    return sha256_file(zip_path)


def find_latest_package_folder(brain_root: Path, brain_name: str | None = None) -> Path | None:
    packages = Path(brain_root) / "packages"
    if not packages.exists():
        return None
    dirs = [
        p for p in packages.iterdir()
        if p.is_dir()
        and "one_upload_package" in p.name.lower()
        and "gemini" not in p.name.lower()
    ]
    if not dirs:
        return None
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def patch_latest_brain_package(workspace_dir: str, brain_name: str) -> dict:
    from sqlite_brain_builder.runtime.path_policy import brain_output_dir

    brain_root = Path(brain_output_dir(workspace_dir, brain_name))
    package_folder = find_latest_package_folder(brain_root, brain_name)
    if not package_folder:
        raise RuntimeError("NORMAL_ONE_UPLOAD_PACKAGE_FOLDER_NOT_FOUND")

    result = patch_package_folder(package_folder)
    zip_path = package_folder.with_suffix(".zip")
    result["package_zip"] = str(zip_path)
    result["package_zip_sha256"] = zip_folder_stable(package_folder, zip_path)
    return result


def patch_export_result(workspace_dir: str, brain_name: str, result: dict | None = None) -> dict:
    from sqlite_brain_builder.runtime.path_policy import brain_output_dir

    result = dict(result or {})
    brain_root = Path(brain_output_dir(workspace_dir, brain_name))

    package_folder = None
    for key in ["package_folder", "package_dir", "output_folder", "folder"]:
        if result.get(key) and Path(result[key]).exists():
            p = Path(result[key])
            if p.is_dir():
                package_folder = p
                break

    if package_folder is None:
        package_folder = find_latest_package_folder(brain_root, brain_name)

    if package_folder is None:
        # Nothing to patch yet; keep original result.
        result["chat_lineage_append_only_patch"] = "NO_PACKAGE_FOLDER_FOUND"
        return result

    patch_info = patch_package_folder(package_folder)
    zip_path = package_folder.with_suffix(".zip")
    digest = zip_folder_stable(package_folder, zip_path)

    result.update(
        {
            "package_folder": str(package_folder),
            "package_zip": str(zip_path),
            "package_hash": digest,
            "chat_lineage_append_only_patch": patch_info,
        }
    )
    return result
