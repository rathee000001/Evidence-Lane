from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlite_brain_builder.brain_versions import list_brain_versions, load_brain_version_record
from sqlite_brain_builder.runtime.package_validation import validate_chatgpt_package, validate_gemini_exact10
from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir


class CodexHandoffError(RuntimeError):
    pass


HANDOFF_FILES = (
    "CODEX_NEXT_SESSION_PROMPT.md",
    "BRAIN_DIFF_SUMMARY.md",
    "BRAIN_DIFF_SEMANTIC_MAP.json",
    "LATEST_GOOD_SNAPSHOT.json",
    "CURRENT_BRAIN_POINTER.json",
    "PREVIOUS_BRAIN_POINTER.json",
    "CHANGED_FILES_AND_ROUTES.md",
    "SYMBOL_DEPENDENCY_TEST_CHANGES.md",
    "FEATURE_PILL_STATUS_CHANGES.md",
    "COMPLETED_AND_OPEN_TASKS.md",
    "PACKAGE_AND_SECTOR_CHANGES.md",
    "HANDOFF_MANIFEST.json",
)

FORBIDDEN_PRE_ENV15_TOKENS = ("ENV14", "public_model_env14", "resources/public_model_env14")
SEMANTIC_QUESTION_IDS = (
    "Q01_FILES",
    "Q02_ROUTES",
    "Q03_DEPENDENCIES",
    "Q04_SYMBOLS",
    "Q05_TESTS",
    "Q06_FEATURE_STATES",
    "Q07_COMPLETED_WORK",
    "Q08_OPEN_WORK",
    "Q09_SECTORS",
    "Q10_UI",
    "Q11_PACKAGE_CONTRACTS",
    "Q12_VERSIONS",
)

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(root: Path) -> Path:
    return root / "brain_versions" / "semantic_brain_diff.sqlite"


def _connect(root: Path) -> sqlite3.Connection:
    path = _database(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS brain_good_snapshot(
          snapshot_id TEXT PRIMARY KEY,version_id TEXT UNIQUE,snapshot_hash TEXT,brain_package_path TEXT,
          brain_package_hash TEXT,test_status TEXT,build_status TEXT,created_at TEXT,reason TEXT
        );
        CREATE TABLE IF NOT EXISTS brain_semantic_diff_run(
          diff_run_id TEXT PRIMARY KEY,previous_good_snapshot_id TEXT,current_good_snapshot_id TEXT,
          previous_brain_hash TEXT,current_brain_hash TEXT,source_root TEXT,created_at TEXT,actor TEXT,
          reason TEXT,test_status TEXT,build_status TEXT,diff_status TEXT
        );
        CREATE TABLE IF NOT EXISTS brain_changed_file(diff_run_id TEXT,path TEXT,change_kind TEXT,previous_hash TEXT,current_hash TEXT,PRIMARY KEY(diff_run_id,path));
        CREATE TABLE IF NOT EXISTS brain_route_change(diff_run_id TEXT,route TEXT,change_kind TEXT,evidence_path TEXT,PRIMARY KEY(diff_run_id,route,evidence_path));
        CREATE TABLE IF NOT EXISTS brain_dependency_change(diff_run_id TEXT,dependency TEXT,change_kind TEXT,evidence_path TEXT,PRIMARY KEY(diff_run_id,dependency,evidence_path));
        CREATE TABLE IF NOT EXISTS brain_symbol_change(diff_run_id TEXT,symbol TEXT,change_kind TEXT,evidence_path TEXT,PRIMARY KEY(diff_run_id,symbol,evidence_path));
        CREATE TABLE IF NOT EXISTS brain_test_build_result(diff_run_id TEXT,result_kind TEXT,status TEXT,evidence TEXT,PRIMARY KEY(diff_run_id,result_kind));
        CREATE TABLE IF NOT EXISTS brain_feature_pill_status_change(diff_run_id TEXT,feature TEXT,previous_status TEXT,current_status TEXT,evidence TEXT,PRIMARY KEY(diff_run_id,feature));
        CREATE TABLE IF NOT EXISTS brain_planned_task_status_change(diff_run_id TEXT,task TEXT,previous_status TEXT,current_status TEXT,evidence TEXT,PRIMARY KEY(diff_run_id,task));
        CREATE TABLE IF NOT EXISTS brain_unimplemented_item(diff_run_id TEXT,item TEXT,status TEXT,evidence TEXT,PRIMARY KEY(diff_run_id,item));
        CREATE TABLE IF NOT EXISTS brain_backend_sector_change(diff_run_id TEXT,sector TEXT,change_kind TEXT,evidence_path TEXT,PRIMARY KEY(diff_run_id,sector,evidence_path));
        CREATE TABLE IF NOT EXISTS brain_ui_component_change(diff_run_id TEXT,component TEXT,change_kind TEXT,evidence_path TEXT,PRIMARY KEY(diff_run_id,component,evidence_path));
        CREATE TABLE IF NOT EXISTS brain_package_contract_change(diff_run_id TEXT,contract TEXT,change_kind TEXT,evidence_path TEXT,PRIMARY KEY(diff_run_id,contract,evidence_path));
        CREATE TABLE IF NOT EXISTS brain_codex_handoff(
          handoff_id TEXT PRIMARY KEY,diff_run_id TEXT,handoff_folder TEXT,package_path TEXT,package_hash TEXT,
          manifest_hash TEXT,status TEXT,created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS brain_code_good_snapshot(
          code_good_snapshot_id TEXT PRIMARY KEY,brain_good_snapshot_id TEXT NOT NULL,
          source_id TEXT NOT NULL,lane_id TEXT NOT NULL,code_snapshot_sha256 TEXT NOT NULL,
          head_commit_sha TEXT,verification_status TEXT NOT NULL,created_at TEXT NOT NULL,
          UNIQUE(brain_good_snapshot_id,source_id)
        );
        CREATE TABLE IF NOT EXISTS brain_workflow_relationship(
          edge_id TEXT PRIMARY KEY,brain_good_snapshot_id TEXT NOT NULL,
          from_type TEXT NOT NULL,from_id TEXT NOT NULL,relation_type TEXT NOT NULL,
          to_type TEXT NOT NULL,to_id TEXT NOT NULL,evidence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS brain_semantic_memory_version(
          memory_version_id TEXT PRIMARY KEY,brain_good_snapshot_id TEXT NOT NULL UNIQUE,
          version_id TEXT NOT NULL,snapshot_hash TEXT NOT NULL,test_status TEXT NOT NULL,
          build_status TEXT NOT NULL,package_validation_status TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS brain_semantic_answer(
          diff_run_id TEXT NOT NULL,question_id TEXT NOT NULL,question_text TEXT NOT NULL,
          answer_json TEXT NOT NULL,evidence_status TEXT NOT NULL,
          PRIMARY KEY(diff_run_id,question_id)
        );
        """
    )
    connection.commit()
    return connection


def _latest_chatgpt_package(root: Path) -> Path:
    packages = sorted((root / "packages").glob("ChatGPT_*_Sqlite_brain.zip"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not packages:
        raise CodexHandoffError("CODEX_HANDOFF_CHATGPT_PACKAGE_REQUIRED")
    package = packages[0]
    validation = validate_chatgpt_package(package)
    if validation["status"] != "PASS":
        raise CodexHandoffError("CODEX_HANDOFF_CHATGPT_PACKAGE_INVALID:" + ";".join(validation["errors"]))
    return package


def _latest_gemini_package(root: Path) -> Path:
    package_root = root / "packages"
    packages = sorted(
        {
            *package_root.glob("Gemini_*_Sqlite_brain.zip"),
            *package_root.glob("Gemini_*_Readable_Project.zip"),
        },
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not packages:
        raise CodexHandoffError("MARK_GOOD_GEMINI_PACKAGE_REQUIRED")
    package = packages[0]
    validation = validate_gemini_exact10(package)
    if validation["status"] != "PASS":
        raise CodexHandoffError("MARK_GOOD_GEMINI_PACKAGE_INVALID:" + ";".join(validation["errors"]))
    return package


def _validate_handoff_preflight(root: Path, current: dict[str, Any]) -> dict[str, Any]:
    if str(current.get("test_status") or "").upper() != "PASS":
        raise CodexHandoffError("CODEX_HANDOFF_TEST_STATUS_NOT_PASS")
    if str(current.get("build_status") or "").upper() != "PASS":
        raise CodexHandoffError("CODEX_HANDOFF_BUILD_STATUS_NOT_PASS")
    package = Path(str(current.get("brain_package_path") or ""))
    if not package.is_file():
        raise CodexHandoffError("CODEX_HANDOFF_CURRENT_PACKAGE_MISSING")
    package_hash = _sha256(package)
    if package_hash != str(current.get("brain_package_hash") or "").lower():
        raise CodexHandoffError("CODEX_HANDOFF_CURRENT_PACKAGE_HASH_MISMATCH")
    chatgpt_validation = validate_chatgpt_package(package)
    if chatgpt_validation.get("status") != "PASS":
        raise CodexHandoffError(
            "CODEX_HANDOFF_CHATGPT_PACKAGE_INVALID:" + ";".join(chatgpt_validation.get("errors") or [])
        )
    with zipfile.ZipFile(package, "r") as archive:
        forbidden_members = [
            name for name in archive.namelist()
            if "codex_handoff" in name.casefold() or "codex_next_session" in name.casefold()
        ]
    if forbidden_members:
        raise CodexHandoffError("CODEX_HANDOFF_EMBEDDED_IN_GOVERNED_PACKAGE:" + ",".join(forbidden_members))
    gemini_package = _latest_gemini_package(root)
    gemini_validation = validate_gemini_exact10(gemini_package)
    if gemini_validation.get("status") != "PASS":
        raise CodexHandoffError(
            "CODEX_HANDOFF_GEMINI_PACKAGE_INVALID:" + ";".join(gemini_validation.get("errors") or [])
        )
    return {
        "test_status": "PASS",
        "build_status": "PASS",
        "chatgpt_package": str(package),
        "chatgpt_package_hash": package_hash,
        "chatgpt_validation": chatgpt_validation,
        "gemini_package": str(gemini_package),
        "gemini_package_hash": _sha256(gemini_package),
        "gemini_validation": gemini_validation,
    }


def _assert_external_handoff_paths(root: Path, folder: Path, package: Path) -> None:
    resolved_root = root.resolve()
    forbidden_roots = [(resolved_root / name).resolve() for name in ("env", "uop", "project")]
    for candidate in (folder.resolve(), package.resolve()):
        if not candidate.is_relative_to(resolved_root):
            raise CodexHandoffError(f"CODEX_HANDOFF_PATH_OUTSIDE_BRAIN_ROOT:{candidate}")
        if any(candidate == forbidden or candidate.is_relative_to(forbidden) for forbidden in forbidden_roots):
            raise CodexHandoffError(f"CODEX_HANDOFF_GOVERNED_RUNTIME_PATH_FORBIDDEN:{candidate}")


def _resolve_verified_good_pair(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = _connect(root)
    connection.row_factory = sqlite3.Row
    try:
        snapshots = [
            dict(row)
            for row in connection.execute(
                "SELECT g.* FROM brain_good_snapshot AS g "
                "JOIN brain_semantic_memory_version AS m ON m.brain_good_snapshot_id=g.snapshot_id "
                "WHERE UPPER(g.test_status)='PASS' AND UPPER(g.build_status)='PASS' "
                "AND UPPER(m.test_status)='PASS' AND UPPER(m.build_status)='PASS' "
                "AND UPPER(m.package_validation_status)='PASS' "
                "ORDER BY g.created_at DESC,g.snapshot_id DESC"
            )
        ]
    finally:
        connection.close()
    if len(snapshots) < 2:
        raise CodexHandoffError("CODEX_HANDOFF_REQUIRES_TWO_VERIFIED_GOOD_SNAPSHOTS")
    return snapshots[0], snapshots[1]


def _reusable_handoff(root: Path, diff_run_id: str) -> dict[str, Any] | None:
    connection = _connect(root)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT handoff_id,diff_run_id,handoff_folder,package_path,package_hash,manifest_hash,status,created_at "
            "FROM brain_codex_handoff WHERE diff_run_id=? AND UPPER(status)='PASS' "
            "ORDER BY created_at DESC LIMIT 1",
            (diff_run_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    record = dict(row)
    folder = Path(record["handoff_folder"])
    package = Path(record["package_path"])
    if not folder.is_dir() or not package.is_file() or _sha256(package) != str(record["package_hash"]).lower():
        return None
    validation = validate_codex_handoff_package(package)
    if validation["status"] != "PASS":
        return None
    return {
        "status": "PASS",
        "handoff_id": record["handoff_id"],
        "diff_run_id": record["diff_run_id"],
        "handoff_folder": str(folder),
        "package_path": str(package),
        "package_hash": record["package_hash"],
        "manifest_path": str(folder / "HANDOFF_MANIFEST.json"),
        "incremental_reuse": {"reused": True, "reason": "VERIFIED_SNAPSHOT_PAIR_UNCHANGED"},
    }


def validate_codex_handoff_package(path: str | Path) -> dict[str, Any]:
    package = Path(path)
    errors: list[str] = []
    if not package.is_file():
        return {"status": "FAIL", "package": str(package), "errors": ["HANDOFF_PACKAGE_MISSING"]}
    try:
        with zipfile.ZipFile(package, "r") as archive:
            names = archive.namelist()
            bad_crc = archive.testzip()
            if bad_crc:
                errors.append(f"HANDOFF_ZIP_CRC_FAILED:{bad_crc}")
            direct = {name for name in names if "/" not in name and "\\" not in name and not name.endswith("/")}
            missing = sorted(set(HANDOFF_FILES) - direct)
            extra = sorted(direct - set(HANDOFF_FILES))
            if missing:
                errors.append("HANDOFF_DIRECT_FILES_MISSING:" + ",".join(missing))
            if extra:
                errors.append("HANDOFF_DIRECT_FILES_UNEXPECTED:" + ",".join(extra))
            brain_members = [name for name in names if name.startswith("CURRENT_BRAIN_PACKAGE/") and not name.endswith("/")]
            if not brain_members:
                errors.append("HANDOFF_CURRENT_BRAIN_MEMBERS_MISSING")
            nested = []
            allowed_locked_nested: list[str] = []
            for name in names:
                if name.endswith("/"):
                    continue
                data = archive.read(name)
                if name.lower().endswith(".zip") or data.startswith(b"PK\x03\x04"):
                    nested.append(name)
            if nested:
                errors.append("HANDOFF_NESTED_ZIP_FORBIDDEN:" + ",".join(sorted(nested)))
            for name in sorted(direct):
                if not name.lower().endswith((".md", ".json", ".txt")):
                    continue
                text = archive.read(name).decode("utf-8", errors="replace")
                for token in FORBIDDEN_PRE_ENV15_TOKENS:
                    if token.casefold() in text.casefold():
                        errors.append(f"HANDOFF_PRE_ENV15_TOKEN_FORBIDDEN:{name}:{token}")
            try:
                manifest = json.loads(archive.read("HANDOFF_MANIFEST.json").decode("utf-8"))
                rows = manifest.get("files") if isinstance(manifest, dict) else None
                if not isinstance(rows, list):
                    errors.append("HANDOFF_MANIFEST_FILES_INVALID")
                else:
                    expected_manifest_names = set(HANDOFF_FILES) - {"HANDOFF_MANIFEST.json"}
                    actual_manifest_names = {str(row.get("path") or "") for row in rows if isinstance(row, dict)}
                    if actual_manifest_names != expected_manifest_names:
                        errors.append("HANDOFF_MANIFEST_DIRECT_SET_MISMATCH")
                    for row in rows:
                        member = str(row.get("path") or "")
                        if member not in direct:
                            errors.append(f"HANDOFF_MANIFEST_MEMBER_MISSING:{member}")
                            continue
                        data = archive.read(member)
                        if int(row.get("size") or -1) != len(data):
                            errors.append(f"HANDOFF_MANIFEST_SIZE_MISMATCH:{member}")
                        if str(row.get("sha256") or "").lower() != hashlib.sha256(data).hexdigest():
                            errors.append(f"HANDOFF_MANIFEST_HASH_MISMATCH:{member}")
                current_pointer = json.loads(archive.read("CURRENT_BRAIN_POINTER.json").decode("utf-8"))
                current_good = manifest.get("current_verified_good") or {}
                declared_package = Path(str(manifest.get("current_brain_package") or ""))
                if not declared_package.is_file():
                    errors.append("HANDOFF_STALE_CURRENT_PACKAGE_PATH")
                elif _sha256(declared_package) != str(manifest.get("current_brain_package_hash") or "").lower():
                    errors.append("HANDOFF_CURRENT_PACKAGE_HASH_MISMATCH")
                pointer_expectations = {
                    "version_id": current_good.get("version_id"),
                    "snapshot_hash": current_good.get("snapshot_hash"),
                    "package": manifest.get("current_brain_package"),
                    "package_hash": manifest.get("current_brain_package_hash"),
                }
                for key, expected in pointer_expectations.items():
                    if current_pointer.get(key) != expected:
                        errors.append(f"HANDOFF_CURRENT_POINTER_MISMATCH:{key}")
                previous_pointer = json.loads(archive.read("PREVIOUS_BRAIN_POINTER.json").decode("utf-8"))
                previous_good = manifest.get("previous_verified_good") or {}
                for key in ("snapshot_id", "version_id", "snapshot_hash", "package_hash"):
                    if previous_pointer.get(key) != previous_good.get(key):
                        errors.append(f"HANDOFF_PREVIOUS_POINTER_MISMATCH:{key}")
                semantic_map = json.loads(archive.read("BRAIN_DIFF_SEMANTIC_MAP.json").decode("utf-8"))
                semantic_ids = set((semantic_map.get("required_semantic_answers") or {}).keys())
                if semantic_ids != set(SEMANTIC_QUESTION_IDS):
                    errors.append("HANDOFF_SEMANTIC_QUESTION_SET_MISMATCH")
            except Exception as exc:
                errors.append(f"HANDOFF_MANIFEST_PARSE_FAILED:{type(exc).__name__}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"HANDOFF_PACKAGE_OPEN_FAILED:{type(exc).__name__}")
        bad_crc = None
        names = []
        direct = set()
        brain_members = []
        nested = []
        allowed_locked_nested = []
    return {
        "status": "PASS" if not errors else "FAIL",
        "package": str(package),
        "package_hash": _sha256(package),
        "zip_crc": "PASS" if bad_crc is None else "FAIL",
        "direct_files": sorted(direct),
        "current_brain_member_count": len(brain_members),
        "nested_zip_count": len(nested),
        "allowed_locked_read_nested_members": allowed_locked_nested,
        "errors": errors,
    }


def _active_code_heads(root: Path) -> list[dict[str, str]]:
    heads = []
    for lane_id in ("github_code", "local_code"):
        database = root / "project" / "sectors" / lane_id / f"{lane_id}_sector_v001.sqlite"
        if not database.is_file():
            continue
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_source_active_head'"
            ).fetchone()
            if not exists:
                continue
            for source_id, head_commit, snapshot in connection.execute(
                "SELECT source_id,head_commit_sha,snapshot_sha256 FROM code_source_active_head ORDER BY source_id"
            ):
                heads.append({
                    "source_id": str(source_id), "lane_id": lane_id,
                    "head_commit_sha": str(head_commit or ""), "snapshot_sha256": str(snapshot),
                })
        finally:
            connection.close()
    return heads


def mark_current_passing_build_good(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    test_status: str,
    build_status: str,
    reason: str,
) -> dict[str, Any]:
    if str(test_status).upper() != "PASS" or str(build_status).upper() != "PASS":
        raise CodexHandoffError("MARK_GOOD_REQUIRES_PASSING_TEST_AND_BUILD")
    workspace = normalize_workspace_dir(workspace_dir)
    root = brain_output_dir(workspace, brain_name)
    for path in (
        root / "env" / "env_mmd.svg", root / "env" / "env_mmd.png",
        root / "uop" / "uop_mmd.svg", root / "uop" / "uop_mmd.png",
        root / "project" / "topology" / "project_master_topology.svg",
        root / "project" / "topology" / "project_master_topology.png",
        root / "project" / "topology" / "project_master_topology_HD.png",
    ):
        if not path.is_file() or path.stat().st_size == 0:
            raise CodexHandoffError(f"MARK_GOOD_RENDER_REQUIRED:{path}")
    package = _latest_chatgpt_package(root)
    gemini_package = _latest_gemini_package(root)
    versions = list_brain_versions(workspace, brain_name, verify_hashes=True)["versions"]
    if not versions:
        raise CodexHandoffError("MARK_GOOD_IMMUTABLE_VERSION_REQUIRED")
    version = versions[0]
    snapshot_id = "good_" + hashlib.sha256(version["version_id"].encode()).hexdigest()[:24]
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR REPLACE INTO brain_good_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id, version["version_id"], version["snapshot_hash"], str(package), _sha256(package),
                "PASS", "PASS", created_at, reason,
            ),
        )
        connection.execute(
            "INSERT OR REPLACE INTO brain_semantic_memory_version VALUES(?,?,?,?,?,?,?,?)",
            (
                "memory_" + snapshot_id, snapshot_id, version["version_id"], version["snapshot_hash"],
                "PASS", "PASS", "PASS", created_at,
            ),
        )
        for head in _active_code_heads(root):
            code_good_id = "code_good_" + hashlib.sha256(
                f"{snapshot_id}|{head['source_id']}|{head['snapshot_sha256']}".encode()
            ).hexdigest()[:24]
            connection.execute(
                "INSERT OR REPLACE INTO brain_code_good_snapshot VALUES(?,?,?,?,?,?,?,?)",
                (
                    code_good_id, snapshot_id, head["source_id"], head["lane_id"],
                    head["snapshot_sha256"], head["head_commit_sha"], "PASS", created_at,
                ),
            )
            edge_id = "edge_" + hashlib.sha256(f"{code_good_id}|{snapshot_id}".encode()).hexdigest()[:24]
            connection.execute(
                "INSERT OR REPLACE INTO brain_workflow_relationship VALUES(?,?,?,?,?,?,?,?)",
                (
                    edge_id, snapshot_id, "source_snapshot_head", code_good_id,
                    "LINKS_LATEST_GOOD_BRAIN", "brain_good_snapshot", snapshot_id,
                    "tests=PASS;build=PASS;chatgpt_package=PASS;gemini_package=PASS",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return {
        "snapshot_id": snapshot_id, "version_id": version["version_id"],
        "snapshot_hash": version["snapshot_hash"], "brain_package_path": str(package),
        "brain_package_hash": _sha256(package), "test_status": "PASS", "build_status": "PASS",
        "gemini_package_path": str(gemini_package), "gemini_package_hash": _sha256(gemini_package),
        "package_validation_status": "PASS",
        "created_at": created_at, "reason": reason,
    }


def _version_files(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["path"]: item for item in record.get("snapshot_files") or []}


def _write(path: Path, content: str) -> None:
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _fact_lines(rows: list[dict[str, Any]], *, empty: str) -> str:
    if not rows:
        return f"- {empty}"
    return "\n".join(f"- `{json.dumps(row, sort_keys=True, default=str)}`" for row in rows)


def _code_semantic_facts(root: Path) -> dict[str, list[dict[str, Any]]]:
    facts = {key: [] for key in ("files", "routes", "dependencies", "symbols", "tests", "artifacts")}
    for lane_id in ("github_code", "local_code"):
        database = root / "project" / "sectors" / lane_id / f"{lane_id}_sector_v001.sqlite"
        if not database.is_file():
            continue
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "code_semantic_diff" in tables:
                latest = connection.execute(
                    "SELECT diff_id FROM code_semantic_diff ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if latest and "code_synthetic_snapshot_file" in tables:
                    facts["files"].extend(dict(row) for row in connection.execute(
                        "SELECT relative_path,change_type,prior_sha256,current_sha256,impact_json "
                        "FROM code_synthetic_snapshot_file WHERE diff_id=? ORDER BY relative_path", (latest[0],)
                    ))
            mappings = {
                "routes": ("git_route_impact", "SELECT commit_sha,route_or_api,impact_kind,evidence_path,confidence FROM git_route_impact ORDER BY commit_sha,route_or_api"),
                "dependencies": ("git_dependency_impact", "SELECT commit_sha,dependency_name,before_value,after_value,evidence_path FROM git_dependency_impact ORDER BY commit_sha,dependency_name"),
                "symbols": ("git_symbol_impact", "SELECT commit_sha,symbol_name,impact_kind,evidence_path FROM git_symbol_impact ORDER BY commit_sha,symbol_name"),
                "tests": ("git_test_impact", "SELECT commit_sha,test_path,impact_kind FROM git_test_impact ORDER BY commit_sha,test_path"),
                "artifacts": ("git_artifact_impact", "SELECT commit_sha,artifact_path,impact_kind,metadata_only FROM git_artifact_impact ORDER BY commit_sha,artifact_path"),
            }
            for key, (table, sql) in mappings.items():
                if table in tables:
                    facts[key].extend(dict(row) for row in connection.execute(sql))
        finally:
            connection.close()
    for key in facts:
        unique = {json.dumps(item, sort_keys=True, default=str): item for item in facts[key]}
        facts[key] = [unique[token] for token in sorted(unique)]
    return facts


def create_codex_brain_handoff(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    actor: str = "Evidence OS SQLite Builder",
    reason: str = "create Codex brain diff handoff",
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    root = brain_output_dir(workspace, brain_name)
    current, previous = _resolve_verified_good_pair(root)
    preflight = _validate_handoff_preflight(root, current)
    current_record = load_brain_version_record(workspace, brain_name, current["version_id"])
    previous_record = load_brain_version_record(workspace, brain_name, previous["version_id"])
    current_files = _version_files(current_record)
    previous_files = _version_files(previous_record)
    changed = []
    for path in sorted(set(current_files) | set(previous_files)):
        before = previous_files.get(path)
        after = current_files.get(path)
        if before and after and before["sha256"] == after["sha256"]:
            continue
        kind = "ADDED" if not before else "REMOVED" if not after else "MODIFIED"
        changed.append(
            {"path": path, "change_kind": kind, "previous_hash": before["sha256"] if before else None, "current_hash": after["sha256"] if after else None}
        )
    diff_id = "diff_" + hashlib.sha256((previous["snapshot_id"] + current["snapshot_id"]).encode()).hexdigest()[:24]
    reusable = _reusable_handoff(root, diff_id)
    if reusable:
        return reusable
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    sectors = sorted({item["path"].split("/")[2] for item in changed if item["path"].startswith("project/sectors/") and len(item["path"].split("/")) > 2})
    ui = [item["path"] for item in changed if item["path"].lower().endswith((".tsx", ".jsx", ".css"))]
    package_changes = [item["path"] for item in changed if "manifest" in item["path"].lower() or "pointer" in item["path"].lower()]
    code_facts = _code_semantic_facts(root)
    route_changes = code_facts["routes"]
    dependency_changes = code_facts["dependencies"]
    symbol_changes = code_facts["symbols"]
    test_changes = code_facts["tests"]
    feature_states: list[dict[str, Any]] = []
    completed_work = [{"item": "validated immutable brain version", "status": "COMPLETE"}]
    open_work = [{"item": "UI/UX application after consolidated UI/UX brain", "status": "OPEN"}]
    semantic_answers = {
        "Q01_FILES": ("Which files changed?", changed, "EVIDENCE_PRESENT" if changed else "NO_CHANGE"),
        "Q02_ROUTES": ("Which routes or APIs changed?", route_changes, "EVIDENCE_PRESENT" if route_changes else "NO_DIRECT_EVIDENCE"),
        "Q03_DEPENDENCIES": ("Which dependencies changed?", dependency_changes, "EVIDENCE_PRESENT" if dependency_changes else "NO_DIRECT_EVIDENCE"),
        "Q04_SYMBOLS": ("Which symbols changed?", symbol_changes, "EVIDENCE_PRESENT" if symbol_changes else "NO_DIRECT_EVIDENCE"),
        "Q05_TESTS": ("Which tests changed or were affected?", test_changes, "EVIDENCE_PRESENT" if test_changes else "NO_DIRECT_EVIDENCE"),
        "Q06_FEATURE_STATES": ("Which feature states changed?", feature_states, "NO_DIRECT_EVIDENCE"),
        "Q07_COMPLETED_WORK": ("What work is completed?", completed_work, "EVIDENCE_PRESENT"),
        "Q08_OPEN_WORK": ("What work remains open?", open_work, "EVIDENCE_PRESENT"),
        "Q09_SECTORS": ("Which backend sectors changed?", sectors, "EVIDENCE_PRESENT" if sectors else "NO_CHANGE"),
        "Q10_UI": ("Which UI components changed?", ui, "EVIDENCE_PRESENT" if ui else "NO_CHANGE"),
        "Q11_PACKAGE_CONTRACTS": ("Which package contracts changed?", package_changes, "EVIDENCE_PRESENT" if package_changes else "NO_CHANGE"),
        "Q12_VERSIONS": ("Which immutable versions are compared?", [previous["version_id"], current["version_id"]], "EVIDENCE_PRESENT"),
    }

    connection = _connect(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR REPLACE INTO brain_semantic_diff_run VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (diff_id, previous["snapshot_id"], current["snapshot_id"], previous["snapshot_hash"], current["snapshot_hash"], str(root), created_at, actor, reason, current["test_status"], current["build_status"], "PASS"),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_changed_file VALUES(?,?,?,?,?)",
            [(diff_id, item["path"], item["change_kind"], item["previous_hash"], item["current_hash"]) for item in changed],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_backend_sector_change VALUES(?,?,?,?)",
            [(diff_id, sector, "MODIFIED", f"project/sectors/{sector}") for sector in sectors],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_route_change VALUES(?,?,?,?)",
            [(diff_id, str(item.get("route_or_api") or ""), str(item.get("impact_kind") or "AFFECTED"), str(item.get("evidence_path") or "")) for item in route_changes],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_dependency_change VALUES(?,?,?,?)",
            [(diff_id, str(item.get("dependency_name") or ""), "MODIFIED", str(item.get("evidence_path") or "")) for item in dependency_changes],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_symbol_change VALUES(?,?,?,?)",
            [(diff_id, str(item.get("symbol_name") or ""), str(item.get("impact_kind") or "AFFECTED"), str(item.get("evidence_path") or "")) for item in symbol_changes],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_ui_component_change VALUES(?,?,?,?)",
            [(diff_id, Path(path).name, "MODIFIED", path) for path in ui],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_package_contract_change VALUES(?,?,?,?)",
            [(diff_id, Path(path).name, "MODIFIED", path) for path in package_changes],
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_test_build_result VALUES(?,?,?,?)",
            [
                (diff_id, "tests", current["test_status"], current["reason"]),
                (diff_id, "build", current["build_status"], current["reason"]),
                (
                    diff_id,
                    "package_validation",
                    "PASS",
                    "validated ChatGPT and Gemini provider-readable packages; "
                    f"chatgpt={preflight['chatgpt_package_hash']};gemini={preflight['gemini_package_hash']}",
                ),
            ],
        )
        connection.execute(
            "INSERT OR REPLACE INTO brain_unimplemented_item VALUES(?,?,?,?)",
            (diff_id, "UI/UX application deferred until consolidated UI/UX brain", "OPEN", "user-directed frontend deferment"),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO brain_semantic_answer VALUES(?,?,?,?,?)",
            [
                (diff_id, question_id, question, json.dumps(answer, sort_keys=True, default=str), evidence_status)
                for question_id, (question, answer, evidence_status) in semantic_answers.items()
            ],
        )
        connection.commit()
    finally:
        connection.close()

    folder = root / "codex_handoff" / created_at.replace(":", "").replace("-", "")
    archive_path = root / "packages" / f"{brain_name.replace(' ', '_')}_CODEX_BRAIN_HANDOFF_{folder.name}.zip"
    _assert_external_handoff_paths(root, folder, archive_path)
    folder.mkdir(parents=True, exist_ok=False)
    semantic = {
        "diff_run_id": diff_id,
        "previous_good_snapshot": previous,
        "current_good_snapshot": current,
        "changed_files": changed,
        "routes_changed": route_changes,
        "dependencies_changed": dependency_changes,
        "symbols_changed": symbol_changes,
        "tests_build": {"tests": current["test_status"], "build": current["build_status"]},
        "feature_pill_changes": [],
        "planned_task_changes": [],
        "unimplemented": ["UI/UX application deferred until consolidated UI/UX brain"],
        "backend_sectors_changed": sectors,
        "ui_components_changed": ui,
        "package_contracts_changed": package_changes,
        "versions_created": [previous["version_id"], current["version_id"]],
        "required_semantic_answers": {
            key: {"question": question, "answer": answer, "evidence_status": status}
            for key, (question, answer, status) in semantic_answers.items()
        },
    }
    (folder / "BRAIN_DIFF_SEMANTIC_MAP.json").write_text(json.dumps(semantic, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "LATEST_GOOD_SNAPSHOT.json").write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "CURRENT_BRAIN_POINTER.json").write_text(json.dumps({"brain_name": brain_name, "brain_root": str(root), "version_id": current["version_id"], "snapshot_hash": current["snapshot_hash"], "package": current["brain_package_path"], "package_hash": current["brain_package_hash"]}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "PREVIOUS_BRAIN_POINTER.json").write_text(
        json.dumps(
            {
                "brain_name": brain_name,
                "brain_root": str(root),
                "snapshot_id": previous["snapshot_id"],
                "version_id": previous["version_id"],
                "snapshot_hash": previous["snapshot_hash"],
                "package": previous["brain_package_path"],
                "package_hash": previous["brain_package_hash"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write(folder / "CODEX_NEXT_SESSION_PROMPT.md", f"# Codex next session\n\nRead `CURRENT_BRAIN_POINTER.json`, then `BRAIN_DIFF_SEMANTIC_MAP.json`. Continue from good version `{current['version_id']}`. Preserve Env15 governance and verify local source only for exact implementation lines.")
    _write(folder / "BRAIN_DIFF_SUMMARY.md", f"# Brain diff summary\n\n- Previous: `{previous['version_id']}`\n- Current: `{current['version_id']}`\n- Changed files: {len(changed)}\n- Changed sectors: {', '.join(sectors) or 'none'}\n- Tests: {current['test_status']}\n- Build: {current['build_status']}")
    _write(
        folder / "CHANGED_FILES_AND_ROUTES.md",
        "# Changed files and routes\n\n## Immutable file diff\n\n"
        + ("\n".join(f"- {item['change_kind']}: `{item['path']}`" for item in changed) or "- No immutable file change.")
        + "\n\n## Code-lane synthetic snapshot diff\n\n"
        + _fact_lines(code_facts["files"], empty="No synthetic snapshot delta recorded.")
        + "\n\n## Route/API impact\n\n"
        + _fact_lines(route_changes, empty="No direct route/API evidence recorded."),
    )
    _write(
        folder / "SYMBOL_DEPENDENCY_TEST_CHANGES.md",
        f"# Symbol, dependency, and test changes\n\n- Tests: {current['test_status']}\n- Build: {current['build_status']}"
        + "\n\n## Symbols\n\n"
        + _fact_lines(symbol_changes, empty="No direct symbol evidence recorded.")
        + "\n\n## Dependencies\n\n"
        + _fact_lines(dependency_changes, empty="No direct dependency evidence recorded.")
        + "\n\n## Tests affected\n\n"
        + _fact_lines(test_changes, empty="No direct changed-test evidence recorded."),
    )
    _write(folder / "FEATURE_PILL_STATUS_CHANGES.md", "# Feature pill status changes\n\nNo feature-pill status transition evidence was recorded for this backend-only diff.")
    _write(folder / "COMPLETED_AND_OPEN_TASKS.md", "# Completed and open tasks\n\n- Completed: immutable semantic diff and validated handoff generation.\n- Open: UI/UX application after consolidated UI/UX brain is supplied.")
    _write(folder / "PACKAGE_AND_SECTOR_CHANGES.md", "# Package and sector changes\n\n- Sectors: " + (", ".join(sectors) or "none") + "\n- Contract files: " + (", ".join(package_changes) or "none"))

    package = Path(current["brain_package_path"])
    manifest_rows = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.name != "HANDOFF_MANIFEST.json":
            manifest_rows.append({"path": path.name, "size": path.stat().st_size, "sha256": _sha256(path)})
    manifest = {
        "contract": "T021_CODEX_BRAIN_HANDOFF_V1",
        "governance_authority": "ENV15",
        "diff_run_id": diff_id,
        "created_at": created_at,
        "actor": actor,
        "reason": reason,
        "previous_verified_good": {
            "snapshot_id": previous["snapshot_id"],
            "version_id": previous["version_id"],
            "snapshot_hash": previous["snapshot_hash"],
            "package_hash": previous["brain_package_hash"],
        },
        "current_verified_good": {
            "snapshot_id": current["snapshot_id"],
            "version_id": current["version_id"],
            "snapshot_hash": current["snapshot_hash"],
            "package_hash": current["brain_package_hash"],
        },
        "current_brain_package": str(package),
        "current_brain_package_hash": _sha256(package),
        "files": manifest_rows,
    }
    manifest_path = folder / "HANDOFF_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.iterdir()):
            if path.is_file():
                archive.write(path, path.name)
        with zipfile.ZipFile(package) as brain_archive:
            for member in brain_archive.infolist():
                if not member.is_dir():
                    archive.writestr("CURRENT_BRAIN_PACKAGE/" + member.filename, brain_archive.read(member))
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise CodexHandoffError("CODEX_HANDOFF_ZIP_CRC_FAILED")
    package_validation = validate_codex_handoff_package(archive_path)
    if package_validation["status"] != "PASS":
        raise CodexHandoffError("CODEX_HANDOFF_PACKAGE_INVALID:" + ";".join(package_validation["errors"]))
    handoff_id = "handoff_" + hashlib.sha256((diff_id + str(archive_path)).encode()).hexdigest()[:24]
    connection = _connect(root)
    try:
        connection.execute(
            "INSERT INTO brain_codex_handoff VALUES(?,?,?,?,?,?,?,?)",
            (handoff_id, diff_id, str(folder), str(archive_path), _sha256(archive_path), _sha256(manifest_path), "PASS", created_at),
        )
        connection.commit()
    finally:
        connection.close()
    return {"status": "PASS", "handoff_id": handoff_id, "diff_run_id": diff_id, "handoff_folder": str(folder), "package_path": str(archive_path), "package_hash": _sha256(archive_path), "manifest_path": str(manifest_path), "changed_file_count": len(changed), "changed_sectors": sectors, "package_validation": package_validation, "incremental_reuse": {"reused": False, "reason": "VERIFIED_SNAPSHOT_PAIR_CHANGED"}}


__all__ = ["CodexHandoffError", "HANDOFF_FILES", "SEMANTIC_QUESTION_IDS", "create_codex_brain_handoff", "mark_current_passing_build_good", "validate_codex_handoff_package"]
