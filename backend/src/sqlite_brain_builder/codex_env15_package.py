from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from sqlite_brain_builder.runtime.path_policy import slugify_name
from sqlite_brain_builder.runtime.portable_brain_package import (
    MODEL_FAILURE_CLASSES,
    create_portable_brain_package,
    export_portable_brain_zip,
    register_goal_delta,
    validate_portable_brain_package,
    validate_portable_brain_zip,
)
from sqlite_brain_builder.runtime.package_validation import (
    validate_chatgpt_package,
    validate_gemini_exact10,
)
from sqlite_brain_builder.runtime.package_root_authority import (
    DEFAULT_ROOT_AUTHORITY_RELATIVE,
    validate_package_root_authority,
    validate_zip_package_root_authority,
    write_package_root_authority,
)
from sqlite_brain_builder.runtime.env15_locked_read import (
    CHATGPT_GEMINI_ARCHIVE_SHA256,
    CODEX_ARCHIVE_SHA256,
    SUPPLIED_CODEX_SUPPORT_MEMBERS,
    SUPPLIED_CODEX_SUPPORT_PROVENANCE,
    install_supplied_codex_support,
    supplied_codex_support_rows,
    verify_supplied_codex_support,
)


PACKAGE_FAMILY = "EVIDENCEOS_UNIVERSAL_EXECUTION_PACKAGE"
PACKAGE_VERSION = "V001"
CODEX_EXTERNAL_DIFF_TRANSPORT = "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE"
CODEX_PACKAGE_MAX_BYTES = 500_000_000
ENV15_PACKAGE_CLASS = "UEPC_ENV15_PUBLIC_FULL_LOCKED_RUNTIME"
ENV15_RESOURCE_ROOT = Path(__file__).resolve().parent / "resources" / "public_model_env15"
AUTHORITY_PATH = "manifests/PACKAGE_AUTHORITY.json"
CLEAN_CODEX_CONTRACT_PATH = "manifests/CODEX_HIGHER_PACKAGE_CONTRACT.json"
CLEAN_CODEX_CHAIN_PATH = "manifests/CODEX_UPSTREAM_PACKAGE_CHAIN.json"
CLEAN_CODEX_MANIFEST_PATH = "manifests/CODEX_FILE_HASH_MANIFEST.json"
CLEAN_CODEX_CHATGPT_BASE_MANIFEST_PATH = "manifests/CODEX_CHATGPT_BASE_MEMBERS.json"
CLEAN_CODEX_ROOT_AUTHORITY_PATH = "manifests/CODEX_PACKAGE_ROOT_AUTHORITY.json"
CLEAN_CODEX_REQUIRED_PATHS = {
    CLEAN_CODEX_CONTRACT_PATH,
    CLEAN_CODEX_CHAIN_PATH,
    CLEAN_CODEX_MANIFEST_PATH,
    CLEAN_CODEX_CHATGPT_BASE_MANIFEST_PATH,
    CLEAN_CODEX_ROOT_AUTHORITY_PATH,
    "brain_diffs/BRAIN_DIFF_TRANSPORT.json",
    "brain_diffs/CURRENT_BRAIN_SNAPSHOT.json",
    "brain_diffs/brain_snapshot.sqlite",
    "project/sectors/delta/delta_sector_v001.sqlite",
    SUPPLIED_CODEX_SUPPORT_PROVENANCE,
} | set(SUPPLIED_CODEX_SUPPORT_MEMBERS)
CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS: set[str] = set()
CLEAN_CODEX_FORBIDDEN_TOKENS = (
    "hil_contract",
    "current_goal_pointer",
    "active_delta_ledger",
    "task_usage_receipt",
    "rq_ledger",
    "codex_runtime_ledger",
    "failure_schema",
    "rollback_pointer",
)
CONTRACT_PATHS = (
    "manifests/UNIVERSAL_EXECUTION_PACKAGE_CONTRACT.json",
    AUTHORITY_PATH,
    "codex/ACTIVE_LANE_POINTER.json",
    "codex/HIL_CONTRACT.json",
    "codex/ALLOWED_MUTATION_SCOPE.json",
    "codex/EXPECTED_OUTPUT_SCHEMA.json",
    "codex/FAILURE_SCHEMA.json",
    "codex/ROLLBACK_POINTER.json",
    "codex/ADAPTER_METADATA.json",
    "codex/ENV15_DERIVATION.json",
    SUPPLIED_CODEX_SUPPORT_PROVENANCE,
    *SUPPLIED_CODEX_SUPPORT_MEMBERS,
)
_PRIMARY_ENV15_PATHS = {
    "env/env_sqlite.sqlite",
    "uop/uop_sqlite.sqlite",
    "project/project_router.sqlite",
    "project/project_template.sqlite",
    "manifests/PACKAGE_CLASS.txt",
    "manifests/PACKAGE_CONTENTS.json",
    "manifests/INTERNAL_HASH_MANIFEST.txt",
}
_HASH_ROW = re.compile(r"^([0-9a-fA-F]{64})\s+(\d+)\s+(.+)$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _package_authority_inputs(
    *,
    brain_name: str,
    goal_pointer: Mapping[str, Any],
    delta_ledger: Mapping[str, Any],
    latest_good_snapshot: Mapping[str, Any] | None,
    template_validation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "package_family": PACKAGE_FAMILY,
        "package_version": PACKAGE_VERSION,
        "brain_name": brain_name,
        "goal_pointer": dict(goal_pointer),
        "delta_ledger": dict(delta_ledger),
        "latest_good_snapshot": dict(latest_good_snapshot or {"status": "UNVERIFIED"}),
        "env15_package_class": template_validation.get("package_class"),
        "env15_template_tree_hash": template_validation.get("template_tree_hash"),
    }


def _package_authority_document(inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authority_contract": "T023_PACKAGE_AUTHORITY_V001",
        "authority_inputs": dict(inputs),
        "authority_sha256": _sha256_bytes(_canonical_json_bytes(inputs)),
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _clean_codex_forbidden_members(names: set[str]) -> list[str]:
    return sorted(
        name for name in names
        if any(token in name.casefold() for token in CLEAN_CODEX_FORBIDDEN_TOKENS)
    )


def _extract_validated_chatgpt_package(archive_path: Path, destination: Path) -> dict[str, Any]:
    validation = validate_chatgpt_package(archive_path)
    if validation.get("status") != "PASS":
        raise RuntimeError(
            "CODEX_CHATGPT_SOURCE_VALIDATION_FAILED:"
            + ";".join(validation.get("errors") or [])
        )
    extracted = 0
    source_members: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive_path, "r") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("CODEX_CHATGPT_SOURCE_CRC_FAILED")
        for info in sorted(archive.infolist(), key=lambda row: row.filename.casefold()):
            if info.is_dir():
                continue
            member = PurePosixPath(info.filename)
            if (
                member.is_absolute()
                or any(part in {"", ".", ".."} for part in member.parts)
                or "\\" in info.filename
            ):
                raise RuntimeError("CODEX_CHATGPT_SOURCE_MEMBER_UNSAFE:" + info.filename)
            if member.suffix.casefold() == ".zip" and member.as_posix() not in CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS:
                raise RuntimeError("CODEX_NESTED_PROVIDER_ZIP_FORBIDDEN:" + info.filename)
            target = _safe_member(destination, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            extracted += 1
            source_members.append(
                {
                    "path": member.as_posix(),
                    "byte_size": target.stat().st_size,
                    "sha256": _sha256_file(target),
                }
            )
    return {
        "status": "PASS",
        "source_archive": str(archive_path),
        "source_archive_sha256": _sha256_file(archive_path),
        "source_member_count": extracted,
        "source_members": source_members,
        "source_member_set_sha256": _sha256_bytes(
            json.dumps(
                source_members,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ),
        "source_validation": validation,
    }


def _snapshot_identity_clean(snapshot: Path) -> dict[str, str]:
    uri = f"file:{snapshot.as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            "SELECT snapshot_id,input_set_hash,claim_status FROM snapshot_metadata LIMIT 1"
        ).fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if not row or not integrity or integrity[0] != "ok":
        raise RuntimeError("CODEX_IMMUTABLE_BRAIN_SNAPSHOT_INVALID")
    return {
        "snapshot_id": str(row[0]),
        "input_set_hash": str(row[1]),
        "claim_status": str(row[2]),
        "sha256": _sha256_file(snapshot),
    }


def _compact_snapshot_binding(snapshot: Path) -> dict[str, Any]:
    uri = f"file:{snapshot.as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        source = connection.execute(
            "SELECT source_path,source_sha256,source_byte_size,source_role "
            "FROM brain_diff_source WHERE source_role='PRESERVED_BASE_PROJECT_CHANGE_HISTORY' LIMIT 1"
        ).fetchone()
        state = connection.execute(
            "SELECT current_sequence,delta_record_count,source_table_count,status,snapshot_scope "
            "FROM brain_diff_state WHERE singleton=1"
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    if not source or not state:
        raise RuntimeError("CODEX_COMPACT_BRAIN_DIFF_BINDING_MISSING")
    return {
        "source_path": str(source[0]),
        "source_sha256": str(source[1]).upper(),
        "source_byte_size": int(source[2]),
        "source_role": str(source[3]),
        "current_sequence": int(state[0]),
        "delta_record_count": int(state[1]),
        "source_table_count": int(state[2]),
        "status": str(state[3]),
        "snapshot_scope": str(state[4]),
        "tables": sorted(tables),
    }


def _inspect_clean_project_change_history_sector(
    path: Path,
    *,
    source_member: Mapping[str, Any],
) -> dict[str, Any]:
    """Inspect, but never rewrite, base project-change history.

    The physical ``project/sectors/delta`` path is retained for package/schema
    compatibility. Provider identity is provenance only and grants no project
    mutation or project-truth authority.
    """
    if not path.is_file():
        raise RuntimeError("CODEX_BASE_PROJECT_CHANGE_HISTORY_MISSING")
    table_names: list[str] = []
    current_sequence = 0
    delta_record_count = 0
    status = "PRESERVED_BASE_HISTORY_READ_ONLY"
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if not integrity or integrity[0] != "ok" or foreign_keys:
            raise RuntimeError("CODEX_BASE_PROJECT_CHANGE_HISTORY_INVALID")
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        for table in ("brain_delta_record", "delta_record", "project_delta"):
            if table in table_names:
                delta_record_count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                break
        if "brain_delta_transport_state" in table_names:
            row = connection.execute(
                "SELECT current_sequence,status FROM brain_delta_transport_state LIMIT 1"
            ).fetchone()
            if row:
                current_sequence, status = int(row[0]), str(row[1])
        elif "canonical_delta_pointer" in table_names:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(canonical_delta_pointer)").fetchall()
            }
            sequence_column = "latest_sequence" if "latest_sequence" in columns else "current_sequence"
            row = connection.execute(
                f'SELECT "{sequence_column}",status FROM canonical_delta_pointer LIMIT 1'
            ).fetchone()
            if row:
                current_sequence, status = int(row[0]), str(row[1])
    sha256 = _sha256_file(path)
    expected_sha256 = str(source_member.get("sha256") or "").upper()
    expected_size = int(source_member.get("byte_size") or -1)
    if sha256 != expected_sha256 or path.stat().st_size != expected_size:
        raise RuntimeError("CODEX_BASE_PROJECT_CHANGE_HISTORY_CHANGED")
    return {
        "path": path.as_posix(),
        "sha256": sha256,
        "byte_size": path.stat().st_size,
        "table_count": len(table_names),
        "tables": table_names,
        "delta_record_count": delta_record_count,
        "current_sequence": current_sequence,
        "status": status,
        "preserved_from_base_package": True,
        "provider_provenance_only": True,
        "active_project_truth_authority": False,
        "universal_refresh_required_for_local_change_truth": True,
    }


def _create_clean_brain_diff_snapshot(
    path: Path,
    *,
    brain_name: str,
    source_base_package_sha256: str,
    delta_sector: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a compact immutable pointer to read-only project-change history."""
    inputs = {
        "brain_name": brain_name,
        "source_base_package_sha256": source_base_package_sha256,
        "project_delta_sector_sha256": str(delta_sector["sha256"]),
        "project_delta_sector_byte_size": int(delta_sector["byte_size"]),
        "project_delta_current_sequence": int(delta_sector["current_sequence"]),
        "project_delta_record_count": int(delta_sector["delta_record_count"]),
        "project_delta_status": str(delta_sector["status"]),
        "snapshot_scope": "PROJECT_BRAIN_DIFF_POINTER_ONLY",
    }
    input_set_hash = _sha256_bytes(_canonical_json_bytes(inputs))
    snapshot_id = "brain_diff_snapshot_" + input_set_hash[:24].lower()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".candidate")
    temporary.unlink(missing_ok=True)
    with closing(sqlite3.connect(temporary)) as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA foreign_keys=ON;
            CREATE TABLE snapshot_metadata(
              snapshot_id TEXT PRIMARY KEY,
              schema_version TEXT NOT NULL,
              created_at TEXT NOT NULL,
              claim_status TEXT NOT NULL,
              input_set_hash TEXT NOT NULL,
              statement TEXT NOT NULL
            );
            CREATE TABLE brain_diff_source(
              source_path TEXT PRIMARY KEY,
              source_sha256 TEXT NOT NULL,
              source_byte_size INTEGER NOT NULL,
              source_role TEXT NOT NULL
            );
            CREATE TABLE brain_diff_state(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              brain_name TEXT NOT NULL,
              current_sequence INTEGER NOT NULL,
              delta_record_count INTEGER NOT NULL,
              source_table_count INTEGER NOT NULL,
              status TEXT NOT NULL,
              snapshot_scope TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO snapshot_metadata VALUES(?,?,?,?,?,?)",
            (
                snapshot_id,
                "T023_CODEX_COMPACT_BRAIN_DIFF_SNAPSHOT_V1",
                _utc_now(),
                "IMMUTABLE_PROJECT_BRAIN_DIFF_POINTER",
                input_set_hash,
                "Hash-bound pointer to preserved base project-change history; provider provenance grants no project-truth authority and no project corpus rows are duplicated.",
            ),
        )
        connection.executemany(
            "INSERT INTO brain_diff_source VALUES(?,?,?,?)",
            (
                (
                    "project/sectors/delta/delta_sector_v001.sqlite",
                    str(delta_sector["sha256"]),
                    int(delta_sector["byte_size"]),
                    "PRESERVED_BASE_PROJECT_CHANGE_HISTORY",
                ),
                (
                    "CHATGPT_SOURCE_PACKAGE",
                    source_base_package_sha256,
                    0,
                    "EXPANDED_VALIDATED_BASE_PACKAGE_PROVENANCE_ONLY",
                ),
            ),
        )
        connection.execute(
            "INSERT INTO brain_diff_state VALUES(1,?,?,?,?,?,?)",
            (
                brain_name,
                int(delta_sector["current_sequence"]),
                int(delta_sector["delta_record_count"]),
                int(delta_sector["table_count"]),
                str(delta_sector["status"]),
                "PROJECT_BRAIN_DIFF_POINTER_ONLY",
            ),
        )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("CODEX_COMPACT_BRAIN_DIFF_SNAPSHOT_INTEGRITY_FAILED")
    os.replace(temporary, path)
    path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    identity = _snapshot_identity_clean(path)
    return {
        "snapshot": str(path),
        "snapshot_id": identity["snapshot_id"],
        "input_set_hash": identity["input_set_hash"],
        "claim_status": identity["claim_status"],
        "sha256": identity["sha256"],
        "snapshot_scope": "PROJECT_BRAIN_DIFF_POINTER_ONLY",
        "source_database_count": 1,
        "canonical_row_count": 0,
        "fts_row_count": 0,
    }


def _write_clean_codex_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / CLEAN_CODEX_MANIFEST_PATH
    manifest_path.unlink(missing_ok=True)
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "byte_size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file()
    ]
    document = {
        "contract": "T023_CODEX_COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY_V2",
        "manifest_scope": "CODEX_COMPONENT_MANIFEST_NOT_OUTER_ROOT",
        "current_root_authority": CLEAN_CODEX_ROOT_AUTHORITY_PATH,
        "member_count_excluding_manifest": len(rows),
        "files": rows,
    }
    _write_json(manifest_path, document)
    return document


def _write_clean_codex_root_authority(
    root: Path,
    *,
    brain_name: str,
    source_chatgpt_sha256: str,
    source_chatgpt_member_set_sha256: str,
    delta_sector: Mapping[str, Any],
) -> dict[str, Any]:
    inherited = root / DEFAULT_ROOT_AUTHORITY_RELATIVE
    parent_authority: dict[str, Any] = {
        "source_package_sha256": source_chatgpt_sha256,
        "source_member_set_sha256": source_chatgpt_member_set_sha256,
        "inherited_root_authority": DEFAULT_ROOT_AUTHORITY_RELATIVE,
        "inherited_root_authority_scope": "CHATGPT_BASE_SNAPSHOT_NOT_CURRENT_OUTER_ROOT",
    }
    if inherited.is_file():
        parent_authority["inherited_root_authority_sha256"] = _sha256_file(inherited)
    return write_package_root_authority(
        root,
        package_profile="CODEX_COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY",
        package_use_mode="EXPLICIT_EXTERNAL_WORKING_COPY_SCOPE_WITH_READ_ONLY_CANONICAL_BRAIN",
        manifest_relative=CLEAN_CODEX_ROOT_AUTHORITY_PATH,
        parent_authority=parent_authority,
        lineage_head_relative="project/lineage/LINEAGE_HEAD.json",
        extra={
            "brain_name": brain_name,
            "project_delta_sector": "project/sectors/delta/delta_sector_v001.sqlite",
            "project_delta_sector_sha256": str(delta_sector["sha256"]),
            "project_change_history_preserved_from_base_package": True,
            "provider_identity_role": "PROVENANCE_ONLY",
            "provider_specific_project_delta": False,
            "public_model_project_access": "READ_ONLY",
            "active_project_truth_authority": "UNIVERSAL_REFRESH_ONLY",
            "canonical_project_brain_write_allowed": False,
            "component_manifest": CLEAN_CODEX_MANIFEST_PATH,
        },
    )


def _validate_clean_codex_stage(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    } if root.is_dir() else set()
    errors.extend("CODEX_CLEAN_MEMBER_MISSING:" + name for name in sorted(CLEAN_CODEX_REQUIRED_PATHS - present))
    errors.extend("CODEX_EVIDENCE_OS_GOVERNANCE_FORBIDDEN:" + name for name in _clean_codex_forbidden_members(present))
    nested = sorted(
        name for name in present
        if PurePosixPath(name).suffix.casefold() == ".zip"
        and name not in CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS
    )
    allowed_support_zips = sorted(name for name in present if name in CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS)
    errors.extend("CODEX_NESTED_ZIP_FORBIDDEN:" + name for name in nested)
    supplied_support = verify_supplied_codex_support(root)
    errors.extend(
        "CODEX_SUPPLIED_SUPPORT_INVALID:" + error
        for error in supplied_support.get("errors") or []
    )
    snapshot_identity: dict[str, str] = {}
    try:
        contract = json.loads((root / CLEAN_CODEX_CONTRACT_PATH).read_text(encoding="utf-8"))
        support_provenance = json.loads(
            (root / SUPPLIED_CODEX_SUPPORT_PROVENANCE).read_text(encoding="utf-8")
        )
        chain = json.loads((root / CLEAN_CODEX_CHAIN_PATH).read_text(encoding="utf-8"))
        transport = json.loads((root / "brain_diffs/BRAIN_DIFF_TRANSPORT.json").read_text(encoding="utf-8"))
        pointer = json.loads((root / "brain_diffs/CURRENT_BRAIN_SNAPSHOT.json").read_text(encoding="utf-8"))
        snapshot_path = root / "brain_diffs/brain_snapshot.sqlite"
        snapshot_identity = _snapshot_identity_clean(snapshot_path)
        snapshot_binding = _compact_snapshot_binding(snapshot_path)
        if contract.get("composition") != "EXPANDED_VALIDATED_COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY":
            errors.append("CODEX_CLEAN_COMPOSITION_INVALID")
        if contract.get("evidence_os_hil_governance_included") is not False:
            errors.append("CODEX_CLEAN_HIL_EXCLUSION_INVALID")
        if contract.get("nested_provider_archives") is not False:
            errors.append("CODEX_CLEAN_NESTED_PROVIDER_POLICY_INVALID")
        if contract.get("chatgpt_members_preserved_byte_for_byte") is not True:
            errors.append("CODEX_CHATGPT_MEMBER_PRESERVATION_CONTRACT_INVALID")
        if contract.get("project_change_history_preserved_from_base_package") is not True:
            errors.append("CODEX_BASE_PROJECT_HISTORY_PRESERVATION_CONTRACT_INVALID")
        if contract.get("provider_identity_role") != "PROVENANCE_ONLY":
            errors.append("CODEX_PROVIDER_PROVENANCE_BOUNDARY_INVALID")
        if contract.get("provider_specific_project_delta") is not False:
            errors.append("CODEX_PROVIDER_SPECIFIC_PROJECT_DELTA_NOT_DISABLED")
        if contract.get("public_model_project_access") != "READ_ONLY":
            errors.append("CODEX_PUBLIC_MODEL_PROJECT_ACCESS_INVALID")
        if contract.get("active_project_truth_authority") != "UNIVERSAL_REFRESH_ONLY":
            errors.append("CODEX_PROJECT_TRUTH_AUTHORITY_INVALID")
        if contract.get("canonical_project_brain_write_allowed") is not False:
            errors.append("CODEX_CANONICAL_PROJECT_BRAIN_WRITE_NOT_LOCKED")
        if contract.get("snapshot_scope") != "PROJECT_BRAIN_DIFF_POINTER_ONLY":
            errors.append("CODEX_COMPACT_SNAPSHOT_SCOPE_INVALID")
        if contract.get("current_root_authority") != CLEAN_CODEX_ROOT_AUTHORITY_PATH:
            errors.append("CODEX_CURRENT_ROOT_AUTHORITY_INVALID")
        if contract.get("new_package_acceptance_state") != "UNTRUSTED_CANDIDATE":
            errors.append("CODEX_CANDIDATE_DEFAULT_INVALID")
        if contract.get("promotion_authority") != "EXPLICIT_HUMAN_HIL_ONLY":
            errors.append("CODEX_HIL_PROMOTION_BOUNDARY_INVALID")
        if contract.get("supplied_codex_support_members") != list(SUPPLIED_CODEX_SUPPORT_MEMBERS):
            errors.append("CODEX_SUPPLIED_SUPPORT_CONTRACT_SET_INVALID")
        if contract.get("supplied_source_archive_sha256") != CODEX_ARCHIVE_SHA256:
            errors.append("CODEX_SUPPLIED_SUPPORT_SOURCE_HASH_INVALID")
        if (
            contract.get("chatgpt_gemini_common_archive_sha256")
            != CHATGPT_GEMINI_ARCHIVE_SHA256
        ):
            errors.append("CODEX_COMMON_ENV_UOP_SOURCE_HASH_INVALID")
        if (
            support_provenance.get("status") != "PASS"
            or support_provenance.get("provider_scope") != "CODEX_ONLY"
            or support_provenance.get("support_member_count")
            != len(SUPPLIED_CODEX_SUPPORT_MEMBERS)
            or support_provenance.get("source_archive_mounted_or_nested") is not False
        ):
            errors.append("CODEX_SUPPLIED_SUPPORT_PROVENANCE_INVALID")
        if snapshot_binding.get("snapshot_scope") != "PROJECT_BRAIN_DIFF_POINTER_ONLY":
            errors.append("CODEX_COMPACT_SNAPSHOT_BINDING_SCOPE_INVALID")
        if "canonical_rows_fts" in snapshot_binding.get("tables", []):
            errors.append("CODEX_FULL_CORPUS_FTS_SNAPSHOT_FORBIDDEN")
        if pointer != snapshot_identity:
            errors.append("CODEX_CLEAN_SNAPSHOT_POINTER_MISMATCH")
        if transport.get("project_delta_sector") != "project/sectors/delta/delta_sector_v001.sqlite":
            errors.append("CODEX_CLEAN_PROJECT_DELTA_POINTER_INVALID")
        if chain != contract.get("upstream_package_chain"):
            errors.append("CODEX_CLEAN_UPSTREAM_CHAIN_MISMATCH")
        manifest = json.loads((root / CLEAN_CODEX_MANIFEST_PATH).read_text(encoding="utf-8"))
        if manifest.get("manifest_scope") != "CODEX_COMPONENT_MANIFEST_NOT_OUTER_ROOT":
            errors.append("CODEX_COMPONENT_MANIFEST_SCOPE_INVALID")
        if manifest.get("current_root_authority") != CLEAN_CODEX_ROOT_AUTHORITY_PATH:
            errors.append("CODEX_COMPONENT_MANIFEST_ROOT_PRECEDENCE_INVALID")
        manifest_rows = {
            str(row.get("path") or ""): row
            for row in manifest.get("files") or []
        }
        for row in manifest.get("files") or []:
            relative = str(row.get("path") or "")
            target = _safe_member(root, relative)
            if not target.is_file():
                errors.append("CODEX_CLEAN_MANIFEST_MEMBER_MISSING:" + relative)
                continue
            expected_size = row.get("byte_size")
            if expected_size is None or target.stat().st_size != int(expected_size):
                errors.append("CODEX_CLEAN_MANIFEST_SIZE_MISMATCH:" + relative)
            if _sha256_file(target) != str(row.get("sha256") or "").upper():
                errors.append("CODEX_CLEAN_MANIFEST_HASH_MISMATCH:" + relative)
        base_manifest = json.loads(
            (root / CLEAN_CODEX_CHATGPT_BASE_MANIFEST_PATH).read_text(encoding="utf-8")
        )
        base_members = list(base_manifest.get("members") or [])
        calculated_base_set_hash = _sha256_bytes(
            json.dumps(
                base_members,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        if calculated_base_set_hash != str(base_manifest.get("source_member_set_sha256") or "").upper():
            errors.append("CODEX_CHATGPT_BASE_MEMBER_SET_HASH_INVALID")
        if calculated_base_set_hash != str(contract.get("chatgpt_base_member_set_sha256") or "").upper():
            errors.append("CODEX_CHATGPT_BASE_MEMBER_SET_CONTRACT_MISMATCH")
        for base_row in base_members:
            relative = str(base_row.get("path") or "")
            actual = manifest_rows.get(relative)
            if actual is None:
                errors.append("CODEX_CHATGPT_BASE_MEMBER_MISSING:" + relative)
                continue
            actual_size = actual.get("byte_size")
            base_size = base_row.get("byte_size")
            if actual_size is None or base_size is None or int(actual_size) != int(base_size):
                errors.append("CODEX_CHATGPT_BASE_MEMBER_SIZE_CHANGED:" + relative)
            if str(actual.get("sha256") or "").upper() != str(base_row.get("sha256") or "").upper():
                errors.append("CODEX_CHATGPT_BASE_MEMBER_HASH_CHANGED:" + relative)
        delta_relative = "project/sectors/delta/delta_sector_v001.sqlite"
        delta_row = manifest_rows.get(delta_relative) or {}
        if snapshot_binding.get("source_path") != delta_relative:
            errors.append("CODEX_COMPACT_SNAPSHOT_DELTA_PATH_INVALID")
        if snapshot_binding.get("source_sha256") != str(delta_row.get("sha256") or "").upper():
            errors.append("CODEX_COMPACT_SNAPSHOT_DELTA_HASH_MISMATCH")
        if snapshot_binding.get("source_byte_size") != int(delta_row.get("byte_size") or -1):
            errors.append("CODEX_COMPACT_SNAPSHOT_DELTA_SIZE_MISMATCH")
        root_authority_validation = validate_package_root_authority(
            root,
            manifest_relative=CLEAN_CODEX_ROOT_AUTHORITY_PATH,
        )
        if root_authority_validation.get("status") != "PASS":
            errors.extend(
                "CODEX_ROOT_AUTHORITY_INVALID:" + str(error)
                for error in root_authority_validation.get("errors") or []
            )
    except (OSError, json.JSONDecodeError, sqlite3.Error, ValueError, RuntimeError) as exc:
        errors.append("CODEX_CLEAN_CONTRACT_INVALID:" + type(exc).__name__)
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "package_family": PACKAGE_FAMILY,
        "package_profile": "COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY",
        "snapshot_input_set_hash": snapshot_identity.get("input_set_hash", ""),
        "member_count": len(present),
        "nested_zip_count": len(nested),
        "allowed_support_zip_count": len(allowed_support_zips),
        "supplied_codex_support_member_count": supplied_support.get("support_member_count", 0),
        "evidence_os_governance_member_count": len(_clean_codex_forbidden_members(present)),
    }


def _deterministic_codex_zip(root: Path, archive_path: Path) -> None:
    archive_path.unlink(missing_ok=True)
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)


def _remove_clean_codex_tree(path: Path) -> None:
    """Remove a clean-package staging tree, including immutable snapshot files."""
    if not path.exists():
        return

    def make_writable_and_retry(function: Any, target: str, _error: Any) -> None:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        function(target)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def _validate_clean_codex_zip(archive_file: Path) -> dict[str, Any]:
    errors: list[str] = []
    names: set[str] = set()
    snapshot_input_set_hash = ""
    try:
        with zipfile.ZipFile(archive_file, "r") as archive:
            names = set(archive.namelist())
            bad_crc = archive.testzip()
            if bad_crc:
                errors.append("CODEX_CLEAN_ZIP_CRC_FAILED:" + bad_crc)
            errors.extend("CODEX_CLEAN_ZIP_MEMBER_MISSING:" + name for name in sorted(CLEAN_CODEX_REQUIRED_PATHS - names))
            errors.extend("CODEX_EVIDENCE_OS_GOVERNANCE_FORBIDDEN:" + name for name in _clean_codex_forbidden_members(names))
            nested = sorted(
                name for name in names
                if PurePosixPath(name).suffix.casefold() == ".zip"
                and name not in CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS
            )
            errors.extend("CODEX_NESTED_ZIP_FORBIDDEN:" + name for name in nested)
            for member, expected_size, expected_hash in supplied_codex_support_rows():
                if member not in names:
                    errors.append("CODEX_SUPPLIED_SUPPORT_MISSING:" + member)
                    continue
                payload = archive.read(member)
                if len(payload) != expected_size:
                    errors.append("CODEX_SUPPLIED_SUPPORT_SIZE_MISMATCH:" + member)
                if _sha256_bytes(payload) != expected_hash:
                    errors.append("CODEX_SUPPLIED_SUPPORT_HASH_MISMATCH:" + member)
            contract = json.loads(archive.read(CLEAN_CODEX_CONTRACT_PATH))
            support_provenance = json.loads(
                archive.read(SUPPLIED_CODEX_SUPPORT_PROVENANCE)
            )
            chain = json.loads(archive.read(CLEAN_CODEX_CHAIN_PATH))
            pointer = json.loads(archive.read("brain_diffs/CURRENT_BRAIN_SNAPSHOT.json"))
            manifest = json.loads(archive.read(CLEAN_CODEX_MANIFEST_PATH))
            if manifest.get("manifest_scope") != "CODEX_COMPONENT_MANIFEST_NOT_OUTER_ROOT":
                errors.append("CODEX_CLEAN_ZIP_COMPONENT_MANIFEST_SCOPE_INVALID")
            if manifest.get("current_root_authority") != CLEAN_CODEX_ROOT_AUTHORITY_PATH:
                errors.append("CODEX_CLEAN_ZIP_ROOT_PRECEDENCE_INVALID")
            base_manifest = json.loads(archive.read(CLEAN_CODEX_CHATGPT_BASE_MANIFEST_PATH))
            snapshot_input_set_hash = str(pointer.get("input_set_hash") or "")
            if contract.get("composition") != "EXPANDED_VALIDATED_COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY":
                errors.append("CODEX_CLEAN_ZIP_COMPOSITION_INVALID")
            if chain != contract.get("upstream_package_chain"):
                errors.append("CODEX_CLEAN_ZIP_UPSTREAM_CHAIN_MISMATCH")
            if contract.get("chatgpt_members_preserved_byte_for_byte") is not True:
                errors.append("CODEX_CLEAN_ZIP_CHATGPT_PRESERVATION_INVALID")
            if contract.get("project_change_history_preserved_from_base_package") is not True:
                errors.append("CODEX_CLEAN_ZIP_BASE_PROJECT_HISTORY_PRESERVATION_INVALID")
            if contract.get("provider_identity_role") != "PROVENANCE_ONLY":
                errors.append("CODEX_CLEAN_ZIP_PROVIDER_PROVENANCE_BOUNDARY_INVALID")
            if contract.get("provider_specific_project_delta") is not False:
                errors.append("CODEX_CLEAN_ZIP_PROVIDER_SPECIFIC_PROJECT_DELTA_NOT_DISABLED")
            if contract.get("public_model_project_access") != "READ_ONLY":
                errors.append("CODEX_CLEAN_ZIP_PUBLIC_MODEL_PROJECT_ACCESS_INVALID")
            if contract.get("active_project_truth_authority") != "UNIVERSAL_REFRESH_ONLY":
                errors.append("CODEX_CLEAN_ZIP_PROJECT_TRUTH_AUTHORITY_INVALID")
            if contract.get("canonical_project_brain_write_allowed") is not False:
                errors.append("CODEX_CLEAN_ZIP_CANONICAL_PROJECT_BRAIN_WRITE_NOT_LOCKED")
            if contract.get("snapshot_scope") != "PROJECT_BRAIN_DIFF_POINTER_ONLY":
                errors.append("CODEX_CLEAN_ZIP_SNAPSHOT_SCOPE_INVALID")
            if contract.get("current_root_authority") != CLEAN_CODEX_ROOT_AUTHORITY_PATH:
                errors.append("CODEX_CLEAN_ZIP_CURRENT_ROOT_AUTHORITY_INVALID")
            if contract.get("new_package_acceptance_state") != "UNTRUSTED_CANDIDATE":
                errors.append("CODEX_CLEAN_ZIP_CANDIDATE_DEFAULT_INVALID")
            if contract.get("supplied_codex_support_members") != list(SUPPLIED_CODEX_SUPPORT_MEMBERS):
                errors.append("CODEX_CLEAN_ZIP_SUPPLIED_SUPPORT_SET_INVALID")
            if contract.get("supplied_source_archive_sha256") != CODEX_ARCHIVE_SHA256:
                errors.append("CODEX_CLEAN_ZIP_SUPPLIED_SUPPORT_SOURCE_HASH_INVALID")
            if (
                contract.get("chatgpt_gemini_common_archive_sha256")
                != CHATGPT_GEMINI_ARCHIVE_SHA256
            ):
                errors.append("CODEX_CLEAN_ZIP_COMMON_ENV_UOP_SOURCE_HASH_INVALID")
            if (
                support_provenance.get("status") != "PASS"
                or support_provenance.get("provider_scope") != "CODEX_ONLY"
                or support_provenance.get("support_member_count")
                != len(SUPPLIED_CODEX_SUPPORT_MEMBERS)
                or support_provenance.get("source_archive_mounted_or_nested") is not False
            ):
                errors.append("CODEX_CLEAN_ZIP_SUPPLIED_SUPPORT_PROVENANCE_INVALID")
            manifest_rows = {
                str(row.get("path") or ""): row
                for row in manifest.get("files") or []
            }
            for row in manifest.get("files") or []:
                relative = str(row.get("path") or "")
                if relative not in names:
                    errors.append("CODEX_CLEAN_ZIP_MANIFEST_MEMBER_MISSING:" + relative)
                    continue
                payload = archive.read(relative)
                expected_size = row.get("byte_size")
                if expected_size is None or len(payload) != int(expected_size):
                    errors.append("CODEX_CLEAN_ZIP_MANIFEST_SIZE_MISMATCH:" + relative)
                if _sha256_bytes(payload) != str(row.get("sha256") or "").upper():
                    errors.append("CODEX_CLEAN_ZIP_MANIFEST_HASH_MISMATCH:" + relative)
            base_members = list(base_manifest.get("members") or [])
            calculated_base_set_hash = _sha256_bytes(
                json.dumps(
                    base_members,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            if calculated_base_set_hash != str(base_manifest.get("source_member_set_sha256") or "").upper():
                errors.append("CODEX_CLEAN_ZIP_CHATGPT_BASE_SET_HASH_INVALID")
            if calculated_base_set_hash != str(contract.get("chatgpt_base_member_set_sha256") or "").upper():
                errors.append("CODEX_CLEAN_ZIP_CHATGPT_BASE_SET_CONTRACT_MISMATCH")
            for base_row in base_members:
                relative = str(base_row.get("path") or "")
                actual = manifest_rows.get(relative)
                if actual is None:
                    errors.append("CODEX_CLEAN_ZIP_CHATGPT_BASE_MEMBER_MISSING:" + relative)
                    continue
                actual_size = actual.get("byte_size")
                base_size = base_row.get("byte_size")
                if actual_size is None or base_size is None or int(actual_size) != int(base_size):
                    errors.append("CODEX_CLEAN_ZIP_CHATGPT_BASE_MEMBER_SIZE_CHANGED:" + relative)
                if str(actual.get("sha256") or "").upper() != str(base_row.get("sha256") or "").upper():
                    errors.append("CODEX_CLEAN_ZIP_CHATGPT_BASE_MEMBER_HASH_CHANGED:" + relative)
            root_authority_validation = validate_zip_package_root_authority(
                archive_file,
                manifest_relative=CLEAN_CODEX_ROOT_AUTHORITY_PATH,
            )
            if root_authority_validation.get("status") != "PASS":
                errors.extend(
                    "CODEX_CLEAN_ZIP_ROOT_AUTHORITY_INVALID:" + str(error)
                    for error in root_authority_validation.get("errors") or []
                )
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError, ValueError) as exc:
        errors.append("CODEX_CLEAN_ZIP_INVALID:" + type(exc).__name__)
    archive_bytes = archive_file.stat().st_size if archive_file.is_file() else 0
    if archive_bytes > CODEX_PACKAGE_MAX_BYTES:
        errors.append(f"CODEX_PACKAGE_SIZE_LIMIT_EXCEEDED:{archive_bytes}>{CODEX_PACKAGE_MAX_BYTES}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "package_family": PACKAGE_FAMILY,
        "package_profile": "COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY",
        "snapshot_input_set_hash": snapshot_input_set_hash,
        "archive_byte_size": archive_bytes,
        "provider_size_limit_bytes": CODEX_PACKAGE_MAX_BYTES,
        "member_count": len(names),
        "nested_zip_count": len([
            name for name in names
            if PurePosixPath(name).suffix.casefold() == ".zip"
            and name not in CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS
        ]),
        "allowed_support_zip_count": len([name for name in names if name in CLEAN_CODEX_ALLOWED_SUPPORT_ZIPS]),
        "supplied_codex_support_member_count": len(
            [name for name in names if name in SUPPLIED_CODEX_SUPPORT_MEMBERS]
        ),
        "evidence_os_governance_member_count": len(_clean_codex_forbidden_members(names)),
        "zip_crc": "PASS" if not any(error.startswith("CODEX_CLEAN_ZIP_CRC_FAILED") for error in errors) else "FAIL",
    }


def _safe_member(root: Path, relative: str) -> Path:
    member = PurePosixPath(str(relative))
    if member.is_absolute() or not member.parts or any(part in {"", ".", ".."} for part in member.parts):
        raise ValueError("ENV15_MEMBER_PATH_INVALID:" + str(relative))
    target = (root / Path(*member.parts)).resolve()
    if root.resolve() not in target.parents:
        raise ValueError("ENV15_MEMBER_PATH_ESCAPE:" + str(relative))
    return target


def _template_files(root: Path) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink():
            raise RuntimeError("ENV15_TEMPLATE_SYMLINK_FORBIDDEN:" + path.relative_to(root).as_posix())
        if path.is_file():
            rows.append((path.relative_to(root).as_posix(), path))
    return rows


def _template_tree_hash(rows: list[tuple[str, Path]]) -> str:
    payload = "\n".join(f"{relative}|{path.stat().st_size}|{_sha256_file(path)}" for relative, path in rows) + "\n"
    return _sha256_bytes(payload.encode("utf-8"))


def _read_hash_manifest(root: Path) -> dict[str, tuple[int, str]]:
    path = root / "manifests" / "INTERNAL_HASH_MANIFEST.txt"
    rows: dict[str, tuple[int, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _HASH_ROW.match(line)
        if not match:
            continue
        digest, size, relative = match.groups()
        if relative in rows:
            raise ValueError("ENV15_HASH_MANIFEST_DUPLICATE:" + relative)
        rows[relative] = (int(size), digest.upper())
    return rows


def validate_env15_template(template_root: str | Path | None = None) -> dict[str, Any]:
    root = Path(template_root or ENV15_RESOURCE_ROOT).resolve()
    errors: list[str] = []
    package_class = "UNAVAILABLE"
    declared: list[str] = []
    hash_rows: dict[str, tuple[int, str]] = {}
    files: list[tuple[str, Path]] = []
    try:
        if not root.is_dir():
            return {"status": "FAIL", "template_root": str(root), "errors": ["ENV15_TEMPLATE_MISSING"]}
        files = _template_files(root)
        package_class_text = (root / "manifests" / "PACKAGE_CLASS.txt").read_text(encoding="utf-8")
        class_match = re.search(r"^PACKAGE_CLASS=(.+)$", package_class_text, flags=re.MULTILINE)
        package_class = class_match.group(1).strip() if class_match else "UNAVAILABLE"
        if package_class != ENV15_PACKAGE_CLASS:
            errors.append("ENV15_PACKAGE_CLASS_INVALID")
        contents = json.loads((root / "manifests" / "PACKAGE_CONTENTS.json").read_text(encoding="utf-8"))
        declared = [str(value) for value in contents.get("paths") or []]
        if len(declared) != int(contents.get("file_count") or -1) or len(declared) != len(set(declared)):
            errors.append("ENV15_PACKAGE_CONTENTS_COUNT_INVALID")
        hash_rows = _read_hash_manifest(root)
        if set(hash_rows) != set(declared):
            errors.append("ENV15_HASH_MANIFEST_COVERAGE_INVALID")
        for relative in declared:
            try:
                target = _safe_member(root, relative)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            expected = hash_rows.get(relative)
            if not target.is_file():
                errors.append("ENV15_DECLARED_FILE_MISSING:" + relative)
                continue
            if not expected:
                continue
            expected_size, expected_hash = expected
            if target.stat().st_size != expected_size:
                errors.append("ENV15_DECLARED_SIZE_MISMATCH:" + relative)
            if _sha256_file(target) != expected_hash:
                errors.append("ENV15_DECLARED_HASH_MISMATCH:" + relative)
        if any(relative.casefold().endswith(".zip") for relative, _ in files):
            errors.append("ENV15_NESTED_ZIP_FORBIDDEN")
        sqlite_files = [(relative, path) for relative, path in files if path.suffix.casefold() == ".sqlite"]
        for relative, database in sqlite_files:
            try:
                with closing(
                    sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True)
                ) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        errors.append("ENV15_SQLITE_INTEGRITY_FAILED:" + relative)
                    if list(connection.execute("PRAGMA foreign_key_check")):
                        errors.append("ENV15_SQLITE_FOREIGN_KEY_FAILED:" + relative)
            except sqlite3.Error as exc:
                errors.append("ENV15_SQLITE_OPEN_FAILED:" + relative + ":" + type(exc).__name__)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        errors.append("ENV15_TEMPLATE_INVALID:" + type(exc).__name__)
    sqlite_count = len([path for _, path in files if path.suffix.casefold() == ".sqlite"])
    return {
        "status": "PASS" if not errors else "FAIL",
        "template_root": str(root),
        "package_class": package_class,
        "file_count": len(files),
        "declared_file_count": len(declared),
        "hash_manifest_row_count": len(hash_rows),
        "sqlite_count": sqlite_count,
        "template_tree_hash": _template_tree_hash(files) if files else "",
        "errors": errors,
    }


def _seed_env15_template(template_root: Path, package_root: Path) -> dict[str, Any]:
    validation = validate_env15_template(template_root)
    if validation["status"] != "PASS":
        raise RuntimeError("ENV15_TEMPLATE_VALIDATION_FAILED:" + ";".join(validation["errors"]))
    package_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    reused = 0
    for relative, source in _template_files(template_root):
        target = _safe_member(package_root, relative)
        if target.exists():
            if not target.is_file() or target.stat().st_size != source.stat().st_size or _sha256_file(target) != _sha256_file(source):
                raise RuntimeError("ENV15_DERIVATION_CONFLICT:" + relative)
            reused += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return {"validation": validation, "copied_file_count": copied, "reused_file_count": reused}


def _contract_documents(
    *,
    brain_name: str,
    goal_pointer: Mapping[str, Any],
    delta_ledger: Mapping[str, Any],
    latest_good_snapshot: Mapping[str, Any] | None,
    template_validation: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    lane_id = str(goal_pointer.get("active_lane_id") or goal_pointer.get("active_lane") or "build_command")
    raw_external_scope = goal_pointer.get("authorized_external_working_copy_paths") or []
    if isinstance(raw_external_scope, str):
        external_working_copy_paths = [raw_external_scope] if raw_external_scope.strip() else []
    elif isinstance(raw_external_scope, (list, tuple)):
        external_working_copy_paths = [
            str(value).strip() for value in raw_external_scope if str(value).strip()
        ]
    else:
        external_working_copy_paths = []
    rollback = dict(latest_good_snapshot or {"status": "UNVERIFIED", "rollback_available": False})
    authority = _package_authority_document(
        _package_authority_inputs(
            brain_name=brain_name,
            goal_pointer=goal_pointer,
            delta_ledger=delta_ledger,
            latest_good_snapshot=latest_good_snapshot,
            template_validation=template_validation,
        )
    )
    return {
        "manifests/UNIVERSAL_EXECUTION_PACKAGE_CONTRACT.json": {
            "package_family": PACKAGE_FAMILY,
            "package_version": PACKAGE_VERSION,
            "brain_name": brain_name,
            "authority_sha256": authority["authority_sha256"],
            "layers": ["READ_ONLY_PROJECT_BRAIN_SNAPSHOT", "WRITABLE_OPERATIONAL_STATE_TRAVEL_LEDGER"],
            "adapters": ["CODEX"],
            "brain_diff_transport": CODEX_EXTERNAL_DIFF_TRANSPORT,
            "headless_local_ai_package_family": "CHATGPT_LOCAL_AI",
            "normalized_operational_ledger_schema": "T023_CODEX_LEDGER_V002",
            "canonical_fusion": "HIL_REQUIRED",
            "verified_brain_direct_overwrite": False,
            "canonical_project_brain_write_allowed": False,
            "external_working_copy_write_authority": "EXPLICIT_CODEX_TASK_SCOPE_ONLY",
            "authorized_external_working_copy_paths": external_working_copy_paths,
            "universal_refresh_required_after_external_change": True,
            "provider_specific_project_delta": False,
        },
        AUTHORITY_PATH: authority,
        "codex/ACTIVE_LANE_POINTER.json": {
            "lane_id": lane_id,
            "current_task_pointer": goal_pointer.get("current_task_pointer"),
            "status": "CANDIDATE_EXECUTION_READY",
        },
        "codex/HIL_CONTRACT.json": {
            "status": "PENDING",
            "canonical_fusion_requires_human_decision": True,
            "allowed_decisions": ["APPROVED", "APPROVED_WITH_SCOPE", "DISAPPROVED", "DROPPED", "LOCKED", "NEEDS_REVISION"],
            "inferred_completion_forbidden": True,
            "rejected_candidate_status": "REJECTED_ROLLED_BACK",
        },
        "codex/ALLOWED_MUTATION_SCOPE.json": {
            "append_only_paths": ["codex/codex_runtime_ledger.sqlite"],
            "new_candidate_outputs_allowed": True,
            "verified_brain_overwrite_allowed": False,
            "read_only_roots": ["brain_snapshot.sqlite", "env", "uop", "project"],
            "canonical_project_brain_write_allowed": False,
            "external_working_copy_write_authority": "EXPLICIT_CODEX_TASK_SCOPE_ONLY",
            "authorized_external_working_copy_paths": external_working_copy_paths,
            "external_write_enabled": bool(external_working_copy_paths),
            "universal_refresh_required_after_external_change": True,
            "provider_specific_project_delta": False,
            "canonical_fusion_requires_hil": True,
        },
        "codex/EXPECTED_OUTPUT_SCHEMA.json": {
            "required_fields": ["execution_id", "task_id", "lane_id", "candidate_status", "files_changed", "tests_run", "test_results", "receipts", "next_pointer"],
            "candidate_status_values": ["CANDIDATE", "OPEN", "BLOCKED", "REJECTED_ROLLED_BACK", "HUMAN_ACCEPTED"],
            "automatic_fusion": False,
        },
        "codex/FAILURE_SCHEMA.json": {
            "failure_classes": sorted(MODEL_FAILURE_CLASSES),
            "required_fields": ["failure_id", "task_id", "model_or_endpoint", "package_id", "lane_id", "stage", "observed_evidence", "exact_error", "inferred_cause", "retry_safe", "data_preserved", "rollback_available", "next_action_code", "next_action_text", "unresolved_risk", "timestamp"],
            "invent_exact_cause_forbidden": True,
        },
        "codex/ROLLBACK_POINTER.json": rollback,
        "codex/ADAPTER_METADATA.json": {
            "package_family": PACKAGE_FAMILY,
            "normalized_delta_ledger": "codex/codex_runtime_ledger.sqlite",
            "active_delta_document": "codex/ACTIVE_DELTA_LEDGER.json",
            # The task ledger can preserve historical labels, but it cannot
            # override the active external-working-copy transport contract.
            "brain_diff_transport": CODEX_EXTERNAL_DIFF_TRANSPORT,
            "headless_local_ai_package_family": "CHATGPT_LOCAL_AI",
            "upstream_package_chain": dict(delta_ledger.get("upstream_package_chain") or {}),
            "adapters": {
                "CODEX": {
                    "execution_mode": "GOVERNED_PACKAGE",
                    "package_level": "HIGHER_THAN_PUBLIC_PACKAGES",
                    "delta_source": "codex/ACTIVE_DELTA_LEDGER.json",
                    "delta_source_class": "PLAN_TASK_LEDGER_NOT_PROJECT_CHANGE_TRUTH",
                    "canonical_project_brain_write_allowed": False,
                    "external_working_copy_write_authority": "EXPLICIT_CODEX_TASK_SCOPE_ONLY",
                    "authorized_external_working_copy_paths": external_working_copy_paths,
                    "universal_refresh_required_after_external_change": True,
                },
            },
        },
        "codex/ENV15_DERIVATION.json": {
            "derived_from_package_class": template_validation.get("package_class"),
            "template_tree_hash": template_validation.get("template_tree_hash"),
            "template_file_count": template_validation.get("file_count"),
            "env_root": "env",
            "uop_root": "uop",
            "project_root": "project",
            "private_payload_included": False,
        },
        "receipts/ENV15_DERIVATION_RECEIPT.json": {
            "created_at": _utc_now(),
            "package_family": PACKAGE_FAMILY,
            "template_tree_hash": template_validation.get("template_tree_hash"),
            "delta_id": delta_ledger.get("delta_id"),
            "status": "PASS_TEMPLATE_VERIFIED",
        },
    }


def _write_contracts(package_root: Path, documents: Mapping[str, Mapping[str, Any]]) -> None:
    for relative, document in documents.items():
        _write_json(_safe_member(package_root, relative), document)


def _validate_seeded_env15(package_root: Path, template_root: Path) -> list[str]:
    errors: list[str] = []
    for relative, source in _template_files(template_root):
        target = _safe_member(package_root, relative)
        if not target.is_file():
            errors.append("ENV15_DERIVED_FILE_MISSING:" + relative)
        elif target.stat().st_size != source.stat().st_size or _sha256_file(target) != _sha256_file(source):
            errors.append("ENV15_DERIVED_FILE_MISMATCH:" + relative)
    return errors


def _authority_validation_errors(
    *,
    authority: Mapping[str, Any],
    contract: Mapping[str, Any],
    goal_pointer: Mapping[str, Any],
    delta_ledger: Mapping[str, Any],
    latest_good_snapshot: Mapping[str, Any],
    derivation: Mapping[str, Any],
) -> tuple[list[str], str]:
    errors: list[str] = []
    inputs = authority.get("authority_inputs")
    authority_sha256 = str(authority.get("authority_sha256") or "").upper()
    if not isinstance(inputs, dict):
        return ["UNIVERSAL_PACKAGE_AUTHORITY_INPUTS_INVALID"], authority_sha256
    recomputed = _sha256_bytes(_canonical_json_bytes(inputs))
    if authority_sha256 != recomputed:
        errors.append("UNIVERSAL_PACKAGE_AUTHORITY_HASH_MISMATCH")
    if str(contract.get("authority_sha256") or "").upper() != authority_sha256:
        errors.append("UNIVERSAL_PACKAGE_AUTHORITY_CONTRACT_MISMATCH")
    expected = {
        "package_family": contract.get("package_family"),
        "package_version": contract.get("package_version"),
        "brain_name": contract.get("brain_name"),
        "goal_pointer": dict(goal_pointer),
        "delta_ledger": dict(delta_ledger),
        "latest_good_snapshot": dict(latest_good_snapshot),
        "env15_package_class": derivation.get("derived_from_package_class"),
        "env15_template_tree_hash": derivation.get("template_tree_hash"),
    }
    if inputs != expected:
        errors.append("UNIVERSAL_PACKAGE_AUTHORITY_INPUT_MISMATCH")
    if inputs.get("package_family") != PACKAGE_FAMILY or inputs.get("package_version") != PACKAGE_VERSION:
        errors.append("UNIVERSAL_PACKAGE_AUTHORITY_FAMILY_VERSION_INVALID")
    return errors, authority_sha256


def validate_codex_env15_package(
    package_root: str | Path,
    template_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(package_root).resolve()
    if (root / CLEAN_CODEX_CONTRACT_PATH).is_file():
        return _validate_clean_codex_stage(root)
    template = Path(template_root or ENV15_RESOURCE_ROOT).resolve()
    base = validate_portable_brain_package(root)
    errors = list(base.get("errors") or [])
    authority_sha256 = ""
    required = set(CONTRACT_PATHS) | _PRIMARY_ENV15_PATHS
    present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()} if root.is_dir() else set()
    errors.extend("UNIVERSAL_MEMBER_MISSING:" + relative for relative in sorted(required - present))
    support_validation = verify_supplied_codex_support(root)
    errors.extend(
        "UNIVERSAL_CODEX_SUPPORT_INVALID:" + error
        for error in support_validation.get("errors") or []
    )
    if template.is_dir() and root.is_dir():
        errors.extend(_validate_seeded_env15(root, template))
    else:
        errors.append("ENV15_TEMPLATE_MISSING")
    try:
        contract = json.loads((root / "manifests" / "UNIVERSAL_EXECUTION_PACKAGE_CONTRACT.json").read_text(encoding="utf-8"))
        authority = json.loads((root / AUTHORITY_PATH).read_text(encoding="utf-8"))
        hil = json.loads((root / "codex" / "HIL_CONTRACT.json").read_text(encoding="utf-8"))
        scope = json.loads((root / "codex" / "ALLOWED_MUTATION_SCOPE.json").read_text(encoding="utf-8"))
        adapters = json.loads((root / "codex" / "ADAPTER_METADATA.json").read_text(encoding="utf-8"))
        goal_pointer = json.loads((root / "codex" / "CURRENT_GOAL_POINTER.json").read_text(encoding="utf-8"))
        delta_ledger = json.loads((root / "codex" / "ACTIVE_DELTA_LEDGER.json").read_text(encoding="utf-8"))
        latest_good = json.loads((root / "codex" / "LATEST_GOOD_SNAPSHOT.json").read_text(encoding="utf-8"))
        derivation = json.loads((root / "codex" / "ENV15_DERIVATION.json").read_text(encoding="utf-8"))
        authority_errors, authority_sha256 = _authority_validation_errors(
            authority=authority,
            contract=contract,
            goal_pointer=goal_pointer,
            delta_ledger=delta_ledger,
            latest_good_snapshot=latest_good,
            derivation=derivation,
        )
        errors.extend(authority_errors)
        if contract.get("package_family") != PACKAGE_FAMILY:
            errors.append("UNIVERSAL_PACKAGE_FAMILY_INVALID")
        if contract.get("canonical_project_brain_write_allowed") is not False:
            errors.append("UNIVERSAL_CONTRACT_CANONICAL_PROJECT_BRAIN_WRITE_NOT_LOCKED")
        if contract.get("external_working_copy_write_authority") != "EXPLICIT_CODEX_TASK_SCOPE_ONLY":
            errors.append("UNIVERSAL_CONTRACT_EXTERNAL_WORKING_COPY_AUTHORITY_INVALID")
        if contract.get("universal_refresh_required_after_external_change") is not True:
            errors.append("UNIVERSAL_CONTRACT_REFRESH_AFTER_EXTERNAL_CHANGE_INVALID")
        if contract.get("provider_specific_project_delta") is not False:
            errors.append("UNIVERSAL_CONTRACT_PROVIDER_SPECIFIC_PROJECT_DELTA_NOT_DISABLED")
        if set(contract.get("layers") or []) != {"READ_ONLY_PROJECT_BRAIN_SNAPSHOT", "WRITABLE_OPERATIONAL_STATE_TRAVEL_LEDGER"}:
            errors.append("UNIVERSAL_PACKAGE_LAYERS_INVALID")
        if hil.get("canonical_fusion_requires_human_decision") is not True:
            errors.append("UNIVERSAL_HIL_CONTRACT_INVALID")
        if scope.get("verified_brain_overwrite_allowed") is not False:
            errors.append("UNIVERSAL_MUTATION_SCOPE_INVALID")
        if scope.get("canonical_project_brain_write_allowed") is not False:
            errors.append("UNIVERSAL_CANONICAL_PROJECT_BRAIN_WRITE_NOT_LOCKED")
        if scope.get("external_working_copy_write_authority") != "EXPLICIT_CODEX_TASK_SCOPE_ONLY":
            errors.append("UNIVERSAL_EXTERNAL_WORKING_COPY_AUTHORITY_INVALID")
        external_paths = scope.get("authorized_external_working_copy_paths")
        if not isinstance(external_paths, list):
            errors.append("UNIVERSAL_EXTERNAL_WORKING_COPY_PATHS_INVALID")
        if scope.get("external_write_enabled") is not bool(external_paths):
            errors.append("UNIVERSAL_EXTERNAL_WORKING_COPY_ENABLEMENT_INVALID")
        if scope.get("universal_refresh_required_after_external_change") is not True:
            errors.append("UNIVERSAL_REFRESH_AFTER_EXTERNAL_CHANGE_INVALID")
        if scope.get("provider_specific_project_delta") is not False:
            errors.append("UNIVERSAL_PROVIDER_SPECIFIC_PROJECT_DELTA_NOT_DISABLED")
        adapter_rows = adapters.get("adapters") if isinstance(adapters, dict) else None
        if not isinstance(adapter_rows, dict) or set(adapter_rows) != {"CODEX"}:
            errors.append("UNIVERSAL_CODEX_ONLY_ADAPTER_INVALID")
        if adapters.get("brain_diff_transport") != CODEX_EXTERNAL_DIFF_TRANSPORT:
            errors.append("UNIVERSAL_CODEX_DIFF_TRANSPORT_INVALID")
        if adapters.get("headless_local_ai_package_family") != "CHATGPT_LOCAL_AI":
            errors.append("UNIVERSAL_HEADLESS_LOCAL_AI_PACKAGE_FAMILY_INVALID")
        if adapters.get("upstream_package_chain") != dict(delta_ledger.get("upstream_package_chain") or {}):
            errors.append("UNIVERSAL_UPSTREAM_PACKAGE_CHAIN_MISMATCH")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        errors.append("UNIVERSAL_CONTRACT_INVALID:" + type(exc).__name__)
    return {
        **base,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "package_family": PACKAGE_FAMILY,
        "authority_sha256": authority_sha256,
        "env15_derived": not any(error.startswith("ENV15_") for error in errors),
    }


def validate_codex_env15_zip(
    archive_path: str | Path,
    template_root: str | Path | None = None,
) -> dict[str, Any]:
    archive_file = Path(archive_path).resolve()
    try:
        with zipfile.ZipFile(archive_file, "r") as probe:
            if CLEAN_CODEX_CONTRACT_PATH in probe.namelist():
                return _validate_clean_codex_zip(archive_file)
    except (OSError, zipfile.BadZipFile):
        return _validate_clean_codex_zip(archive_file)
    template = Path(template_root or ENV15_RESOURCE_ROOT).resolve()
    base = validate_portable_brain_zip(archive_file)
    errors = list(base.get("errors") or [])
    archive_byte_size = archive_file.stat().st_size if archive_file.is_file() else 0
    if archive_byte_size > CODEX_PACKAGE_MAX_BYTES:
        errors.append(
            f"CODEX_PACKAGE_SIZE_LIMIT_EXCEEDED:{archive_byte_size}>{CODEX_PACKAGE_MAX_BYTES}"
        )
    snapshot_input_set_hash = ""
    authority_sha256 = ""
    try:
        with zipfile.ZipFile(archive_file, "r") as archive:
            names = set(archive.namelist())
            required = set(CONTRACT_PATHS) | _PRIMARY_ENV15_PATHS
            errors.extend("UNIVERSAL_ZIP_MEMBER_MISSING:" + relative for relative in sorted(required - names))
            for member, expected_size, expected_hash in supplied_codex_support_rows():
                if member not in names:
                    errors.append("UNIVERSAL_ZIP_CODEX_SUPPORT_MISSING:" + member)
                    continue
                payload = archive.read(member)
                if len(payload) != expected_size:
                    errors.append("UNIVERSAL_ZIP_CODEX_SUPPORT_SIZE_MISMATCH:" + member)
                if _sha256_bytes(payload) != expected_hash:
                    errors.append("UNIVERSAL_ZIP_CODEX_SUPPORT_HASH_MISMATCH:" + member)
            for relative, source in _template_files(template):
                if relative not in names:
                    errors.append("ENV15_ZIP_MEMBER_MISSING:" + relative)
                    continue
                payload = archive.read(relative)
                if len(payload) != source.stat().st_size or _sha256_bytes(payload) != _sha256_file(source):
                    errors.append("ENV15_ZIP_MEMBER_MISMATCH:" + relative)
            contract = json.loads(archive.read("manifests/UNIVERSAL_EXECUTION_PACKAGE_CONTRACT.json"))
            authority = json.loads(archive.read(AUTHORITY_PATH))
            pointer = json.loads(archive.read("pointers/CURRENT_BRAIN_POINTER.json"))
            goal_pointer = json.loads(archive.read("codex/CURRENT_GOAL_POINTER.json"))
            delta_ledger = json.loads(archive.read("codex/ACTIVE_DELTA_LEDGER.json"))
            latest_good = json.loads(archive.read("codex/LATEST_GOOD_SNAPSHOT.json"))
            derivation = json.loads(archive.read("codex/ENV15_DERIVATION.json"))
            adapters = json.loads(archive.read("codex/ADAPTER_METADATA.json"))
            scope = json.loads(archive.read("codex/ALLOWED_MUTATION_SCOPE.json"))
            authority_errors, authority_sha256 = _authority_validation_errors(
                authority=authority,
                contract=contract,
                goal_pointer=goal_pointer,
                delta_ledger=delta_ledger,
                latest_good_snapshot=latest_good,
                derivation=derivation,
            )
            errors.extend(authority_errors)
            if contract.get("package_family") != PACKAGE_FAMILY:
                errors.append("UNIVERSAL_ZIP_PACKAGE_FAMILY_INVALID")
            if contract.get("canonical_project_brain_write_allowed") is not False:
                errors.append("UNIVERSAL_ZIP_CONTRACT_CANONICAL_PROJECT_BRAIN_WRITE_NOT_LOCKED")
            if contract.get("external_working_copy_write_authority") != "EXPLICIT_CODEX_TASK_SCOPE_ONLY":
                errors.append("UNIVERSAL_ZIP_CONTRACT_EXTERNAL_WORKING_COPY_AUTHORITY_INVALID")
            if contract.get("universal_refresh_required_after_external_change") is not True:
                errors.append("UNIVERSAL_ZIP_CONTRACT_REFRESH_AFTER_EXTERNAL_CHANGE_INVALID")
            if contract.get("provider_specific_project_delta") is not False:
                errors.append("UNIVERSAL_ZIP_CONTRACT_PROVIDER_SPECIFIC_PROJECT_DELTA_NOT_DISABLED")
            adapter_rows = adapters.get("adapters") if isinstance(adapters, dict) else None
            if not isinstance(adapter_rows, dict) or set(adapter_rows) != {"CODEX"}:
                errors.append("UNIVERSAL_ZIP_CODEX_ONLY_ADAPTER_INVALID")
            if adapters.get("brain_diff_transport") != CODEX_EXTERNAL_DIFF_TRANSPORT:
                errors.append("UNIVERSAL_ZIP_CODEX_DIFF_TRANSPORT_INVALID")
            if scope.get("canonical_project_brain_write_allowed") is not False:
                errors.append("UNIVERSAL_ZIP_CANONICAL_PROJECT_BRAIN_WRITE_NOT_LOCKED")
            if scope.get("external_working_copy_write_authority") != "EXPLICIT_CODEX_TASK_SCOPE_ONLY":
                errors.append("UNIVERSAL_ZIP_EXTERNAL_WORKING_COPY_AUTHORITY_INVALID")
            external_paths = scope.get("authorized_external_working_copy_paths")
            if not isinstance(external_paths, list):
                errors.append("UNIVERSAL_ZIP_EXTERNAL_WORKING_COPY_PATHS_INVALID")
            if scope.get("external_write_enabled") is not bool(external_paths):
                errors.append("UNIVERSAL_ZIP_EXTERNAL_WORKING_COPY_ENABLEMENT_INVALID")
            if scope.get("universal_refresh_required_after_external_change") is not True:
                errors.append("UNIVERSAL_ZIP_REFRESH_AFTER_EXTERNAL_CHANGE_INVALID")
            if scope.get("provider_specific_project_delta") is not False:
                errors.append("UNIVERSAL_ZIP_PROVIDER_SPECIFIC_PROJECT_DELTA_NOT_DISABLED")
            if adapters.get("headless_local_ai_package_family") != "CHATGPT_LOCAL_AI":
                errors.append("UNIVERSAL_ZIP_HEADLESS_LOCAL_AI_PACKAGE_FAMILY_INVALID")
            if adapters.get("upstream_package_chain") != dict(delta_ledger.get("upstream_package_chain") or {}):
                errors.append("UNIVERSAL_ZIP_UPSTREAM_PACKAGE_CHAIN_MISMATCH")
            snapshot_input_set_hash = str(pointer.get("input_set_hash") or "")
            if not snapshot_input_set_hash:
                errors.append("UNIVERSAL_ZIP_SNAPSHOT_IDENTITY_MISSING")
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError, ValueError) as exc:
        errors.append("UNIVERSAL_ZIP_INVALID:" + type(exc).__name__)
    return {
        **base,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "package_family": PACKAGE_FAMILY,
        "snapshot_input_set_hash": snapshot_input_set_hash,
        "authority_sha256": authority_sha256,
        "archive_byte_size": archive_byte_size,
        "provider_size_limit_bytes": CODEX_PACKAGE_MAX_BYTES,
    }


def _register_delta_once(
    ledger_path: Path,
    goal_pointer: Mapping[str, Any],
    delta_ledger: Mapping[str, Any],
) -> int:
    delta_id = str(delta_ledger.get("delta_id") or "")
    if not delta_id:
        raise ValueError("UNIVERSAL_DELTA_ID_REQUIRED")
    with closing(sqlite3.connect(ledger_path)) as connection:
        existing = connection.execute("SELECT id FROM goal_delta_ledger WHERE delta_id=? ORDER BY id LIMIT 1", (delta_id,)).fetchone()
    if existing:
        return int(existing[0])
    return register_goal_delta(
        ledger_path,
        {
            "delta_id": delta_id,
            "parent_goal_id": str(goal_pointer.get("parent_goal_id") or "EVIDENCEOS_BUILD_COMMAND"),
            "run_id": str(goal_pointer.get("active_run_id") or goal_pointer.get("run_id") or "UNVERIFIED"),
            "task_pointer": str(goal_pointer.get("current_task_pointer") or "BUILD_COMMAND_VALIDATED_PASS"),
            "title": str(delta_ledger.get("delta_title") or delta_ledger.get("title") or delta_id),
            "prompt_pointer": str(delta_ledger.get("prompt_pointer") or "brain.buildAll"),
            "insertion_reason": str(delta_ledger.get("insertion_reason") or "SAME_VALIDATED_PASS_CODEX_PACKAGE"),
            "insertion_point": str(delta_ledger.get("insertion_point") or "package_hash_validation"),
            "status": str(delta_ledger.get("status") or "OPEN_REGISTERED"),
            "existing_requirements_preserved": True,
            "current_task_replaced": False,
            "current_goal_restarted": False,
        },
    )


def _create_clean_codex_higher_package(
    brain_root: str | Path,
    package_root: str | Path,
    archive_directory: str | Path,
    *,
    brain_name: str,
    chatgpt_package: str | Path,
    gemini_package: str | Path | None,
) -> dict[str, Any]:
    source_chatgpt = Path(chatgpt_package).resolve()
    destination = Path(package_root).resolve()
    candidate = destination.with_name(destination.name + ".clean-candidate")
    if candidate.exists():
        _remove_clean_codex_tree(candidate)
    candidate.mkdir(parents=True)
    source = _extract_validated_chatgpt_package(source_chatgpt, candidate)
    base_member_by_path = {
        str(row["path"]): row
        for row in source["source_members"]
    }
    delta_member_path = "project/sectors/delta/delta_sector_v001.sqlite"
    if delta_member_path not in base_member_by_path:
        _remove_clean_codex_tree(candidate)
        raise RuntimeError("CODEX_CHATGPT_PROJECT_DELTA_SECTOR_MISSING")
    reserved_additions = CLEAN_CODEX_REQUIRED_PATHS - {delta_member_path}
    collisions = sorted(reserved_additions & set(base_member_by_path))
    if collisions:
        _remove_clean_codex_tree(candidate)
        raise RuntimeError("CODEX_CHATGPT_RESERVED_MEMBER_COLLISION:" + ";".join(collisions))
    _write_json(
        candidate / CLEAN_CODEX_CHATGPT_BASE_MANIFEST_PATH,
        {
            "contract": "T023_CODEX_CHATGPT_BASE_MEMBER_PRESERVATION_V1",
            "source_archive_sha256": source["source_archive_sha256"],
            "source_member_count": source["source_member_count"],
            "source_member_set_sha256": source["source_member_set_sha256"],
            "members": source["source_members"],
        },
    )
    supplied_support = install_supplied_codex_support(candidate)

    gemini_chain: dict[str, Any] = {
        "status": "SKIPPED_PROJECT_CORPUS_TOO_LARGE",
        "path": "",
        "sha256": "",
        "role": "OPTIONAL_EXACT10_DERIVED_FROM_CHATGPT",
    }
    if gemini_package:
        source_gemini = Path(gemini_package).resolve()
        gemini_validation = validate_gemini_exact10(source_gemini)
        if gemini_validation.get("status") != "PASS":
            _remove_clean_codex_tree(candidate)
            raise RuntimeError(
                "CODEX_GEMINI_SOURCE_VALIDATION_FAILED:"
                + ";".join(gemini_validation.get("errors") or [])
            )
        gemini_chain = {
            "status": "PASS",
            "path": source_gemini.name,
            "sha256": _sha256_file(source_gemini),
            "role": "EXACT10_DERIVED_FROM_CHATGPT",
            "source_chatgpt_package_sha256": str(
                gemini_validation.get("source_chatgpt_package_sha256") or ""
            ).upper(),
        }

    delta_sector = _inspect_clean_project_change_history_sector(
        candidate / delta_member_path,
        source_member=base_member_by_path[delta_member_path],
    )
    snapshot_result = _create_clean_brain_diff_snapshot(
        candidate / "brain_diffs" / "brain_snapshot.sqlite",
        brain_name=brain_name,
        source_base_package_sha256=source["source_archive_sha256"],
        delta_sector=delta_sector,
    )
    snapshot_identity = _snapshot_identity_clean(candidate / "brain_diffs" / "brain_snapshot.sqlite")
    upstream_chain = {
        "chatgpt_local_ai": {
            "status": "PASS",
            "path": source_chatgpt.name,
            "sha256": source["source_archive_sha256"],
            "role": "EXPANDED_BYTE_FOR_BYTE_BASE_PACKAGE",
        },
        "gemini": gemini_chain,
    }
    contract = {
        "contract": "T023_CODEX_COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY_V2",
        "package_family": PACKAGE_FAMILY,
        "package_version": PACKAGE_VERSION,
        "brain_name": brain_name,
        "composition": "EXPANDED_VALIDATED_COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY",
        "chatgpt_members_expanded_not_nested": True,
        "chatgpt_members_preserved_byte_for_byte": True,
        "chatgpt_base_member_manifest": CLEAN_CODEX_CHATGPT_BASE_MANIFEST_PATH,
        "chatgpt_base_member_set_sha256": source["source_member_set_sha256"],
        "nested_provider_archives": False,
        "evidence_os_hil_governance_included": False,
        "brain_diff_transport": "brain_diffs/BRAIN_DIFF_TRANSPORT.json",
        "immutable_snapshot": "brain_diffs/brain_snapshot.sqlite",
        "project_delta_sector": "project/sectors/delta/delta_sector_v001.sqlite",
        "project_delta_sector_sha256": delta_sector["sha256"],
        "project_change_history_preserved_from_base_package": True,
        "provider_identity_role": "PROVENANCE_ONLY",
        "provider_specific_project_delta": False,
        "public_model_project_access": "READ_ONLY",
        "active_project_truth_authority": "UNIVERSAL_REFRESH_ONLY",
        "canonical_project_brain_write_allowed": False,
        "external_working_copy_write_authority": "EXPLICIT_CODEX_TASK_SCOPE_ONLY",
        "model_output_project_truth": False,
        "snapshot_scope": "PROJECT_BRAIN_DIFF_POINTER_ONLY",
        "current_root_authority": CLEAN_CODEX_ROOT_AUTHORITY_PATH,
        "inherited_project_manifest_scope": "PROJECT_SNAPSHOT_NOT_CURRENT_OUTER_ROOT",
        "inherited_root_authority_scope": "CHATGPT_BASE_SNAPSHOT_NOT_CURRENT_OUTER_ROOT",
        "new_package_acceptance_state": "UNTRUSTED_CANDIDATE",
        "promotion_authority": "EXPLICIT_HUMAN_HIL_ONLY",
        "supplied_source_archive_sha256": CODEX_ARCHIVE_SHA256,
        "chatgpt_gemini_common_archive_sha256": CHATGPT_GEMINI_ARCHIVE_SHA256,
        "supplied_codex_support_members": list(SUPPLIED_CODEX_SUPPORT_MEMBERS),
        "supplied_codex_support_member_count": len(SUPPLIED_CODEX_SUPPORT_MEMBERS),
        "supplied_codex_support_provenance": SUPPLIED_CODEX_SUPPORT_PROVENANCE,
        "raw_authority_archive_nested": False,
        "upstream_package_chain": upstream_chain,
    }
    _write_json(candidate / CLEAN_CODEX_CONTRACT_PATH, contract)
    _write_json(candidate / CLEAN_CODEX_CHAIN_PATH, upstream_chain)
    _write_json(candidate / "brain_diffs" / "CURRENT_BRAIN_SNAPSHOT.json", snapshot_identity)
    _write_json(
        candidate / "brain_diffs" / "BRAIN_DIFF_TRANSPORT.json",
        {
            "contract": "T023_IMMUTABLE_BRAIN_DIFF_TRANSPORT_V1",
            "brain_name": brain_name,
            "snapshot_pointer": "brain_diffs/CURRENT_BRAIN_SNAPSHOT.json",
            "immutable_snapshot": "brain_diffs/brain_snapshot.sqlite",
            "project_delta_sector": "project/sectors/delta/delta_sector_v001.sqlite",
            "project_delta_sector_sha256": delta_sector["sha256"],
            "project_change_history_preserved_from_base_package": True,
            "provider_identity_role": "PROVENANCE_ONLY",
            "provider_specific_project_delta": False,
            "public_model_project_access": "READ_ONLY",
            "active_project_truth_authority": "UNIVERSAL_REFRESH_ONLY",
            "canonical_project_brain_write_allowed": False,
            "external_working_copy_write_authority": "EXPLICIT_CODEX_TASK_SCOPE_ONLY",
            "snapshot_scope": "PROJECT_BRAIN_DIFF_POINTER_ONLY",
            "source_package": "EXPANDED_VALIDATED_CHATGPT",
            "append_only_transport": True,
            "evidence_os_task_or_hil_payload": False,
        },
    )
    _write_clean_codex_manifest(candidate)
    _write_clean_codex_root_authority(
        candidate,
        brain_name=brain_name,
        source_chatgpt_sha256=source["source_archive_sha256"],
        source_chatgpt_member_set_sha256=source["source_member_set_sha256"],
        delta_sector=delta_sector,
    )
    package_validation = _validate_clean_codex_stage(candidate)
    if package_validation["status"] != "PASS":
        validation_errors = list(package_validation["errors"])
        _remove_clean_codex_tree(candidate)
        raise RuntimeError(
            "CODEX_CLEAN_PACKAGE_VALIDATION_FAILED:"
            + ";".join(validation_errors)
        )

    archive_dir = Path(archive_directory).resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{PACKAGE_FAMILY}_{slugify_name(brain_name)}.zip"
    archive_candidate = archive.with_name(archive.stem + ".candidate.zip")
    _deterministic_codex_zip(candidate, archive_candidate)
    archive_validation = _validate_clean_codex_zip(archive_candidate)
    if archive_validation["status"] != "PASS":
        archive_candidate.unlink(missing_ok=True)
        _remove_clean_codex_tree(candidate)
        raise RuntimeError(
            "CODEX_CLEAN_ZIP_VALIDATION_FAILED:"
            + ";".join(archive_validation["errors"])
        )
    archive_candidate.replace(archive)
    archive_validation = _validate_clean_codex_zip(archive)

    if destination.exists():
        _remove_clean_codex_tree(destination)
    candidate.replace(destination)
    snapshot_result = {
        **snapshot_result,
        "snapshot": str(destination / "brain_diffs" / "brain_snapshot.sqlite"),
    }
    return {
        "status": "PASS",
        "package_family": PACKAGE_FAMILY,
        "package_profile": "COMMON_BASE_PLUS_READ_ONLY_PROJECT_HISTORY",
        "package_root": str(destination),
        "archive": str(archive),
        "archive_sha256": _sha256_file(archive),
        "authority_sha256": _sha256_bytes(_canonical_json_bytes(contract)),
        "source_chatgpt_package_sha256": source["source_archive_sha256"],
        "upstream_package_chain": upstream_chain,
        "supplied_codex_support": supplied_support,
        "snapshot": snapshot_result,
        "ledger": {
            "status": "NOT_EMBEDDED",
            "reason": "EVIDENCE_OS_TASK_AND_HIL_GOVERNANCE_EXCLUDED_FROM_PROJECT_BRAIN_PACKAGE",
        },
        "provider_size_limit_bytes": CODEX_PACKAGE_MAX_BYTES,
        "validation": package_validation,
        "archive_validation": archive_validation,
    }


def create_codex_env15_package(
    brain_root: str | Path,
    package_root: str | Path,
    archive_directory: str | Path,
    *,
    brain_name: str,
    goal_pointer: Mapping[str, Any],
    delta_ledger: Mapping[str, Any],
    latest_good_snapshot: Mapping[str, Any] | None = None,
    template_root: str | Path | None = None,
    snapshot_source_root: str | Path | None = None,
    chatgpt_package: str | Path | None = None,
    gemini_package: str | Path | None = None,
) -> dict[str, Any]:
    if not goal_pointer.get("parent_goal_id") or not goal_pointer.get("current_task_pointer"):
        raise ValueError("UNIVERSAL_GOAL_POINTER_REQUIRED")
    if not delta_ledger.get("delta_id"):
        raise ValueError("UNIVERSAL_DELTA_LEDGER_REQUIRED")
    if chatgpt_package:
        return _create_clean_codex_higher_package(
            brain_root,
            package_root,
            archive_directory,
            brain_name=brain_name,
            chatgpt_package=chatgpt_package,
            gemini_package=gemini_package,
        )
    template = Path(template_root or ENV15_RESOURCE_ROOT).resolve()
    destination = Path(package_root).resolve()
    seed = _seed_env15_template(template, destination)
    supplied_support = install_supplied_codex_support(destination)
    documents = _contract_documents(
        brain_name=brain_name,
        goal_pointer=goal_pointer,
        delta_ledger=delta_ledger,
        latest_good_snapshot=latest_good_snapshot,
        template_validation=seed["validation"],
    )
    _write_contracts(destination, documents)
    authority_sha256 = str(documents[AUTHORITY_PATH]["authority_sha256"])
    portable = create_portable_brain_package(
        snapshot_source_root or brain_root,
        destination,
        goal_pointer=goal_pointer,
        delta_ledger=delta_ledger,
        latest_good_snapshot=latest_good_snapshot,
    )
    ledger_path = destination / "codex" / "codex_runtime_ledger.sqlite"
    delta_row_id = _register_delta_once(ledger_path, goal_pointer, delta_ledger)
    package_validation = validate_codex_env15_package(destination, template)
    if package_validation["status"] != "PASS":
        raise RuntimeError("UNIVERSAL_PACKAGE_VALIDATION_FAILED:" + ";".join(package_validation["errors"]))
    pointer = json.loads((destination / "pointers" / "CURRENT_BRAIN_POINTER.json").read_text(encoding="utf-8"))
    snapshot_hash = str(pointer.get("input_set_hash") or "")
    if not snapshot_hash:
        raise RuntimeError("UNIVERSAL_SNAPSHOT_IDENTITY_MISSING")
    archive_dir = Path(archive_directory).resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    brain_slug = slugify_name(brain_name)
    archive = archive_dir / f"{PACKAGE_FAMILY}_{brain_slug}.zip"
    archive_reused = False
    if archive.exists():
        archive_validation = validate_codex_env15_zip(archive, template)
        archive_reused = (
            archive_validation["status"] == "PASS"
            and archive_validation.get("snapshot_input_set_hash") == snapshot_hash
            and archive_validation.get("authority_sha256") == authority_sha256
        )
    if archive_reused:
        exported = {
            "archive": str(archive),
            "sha256": _sha256_file(archive),
            "snapshot_id": pointer.get("snapshot_id"),
            "zip_crc": archive_validation.get("zip_crc"),
            "nested_zip_count": archive_validation.get("nested_zip_count"),
            "member_count": archive_validation.get("member_count"),
        }
    else:
        candidate = archive.with_name(archive.stem + ".candidate.zip")
        candidate.unlink(missing_ok=True)
        exported = export_portable_brain_zip(destination, candidate)
        candidate_validation = validate_codex_env15_zip(candidate, template)
        if candidate_validation["status"] != "PASS":
            candidate.unlink(missing_ok=True)
            raise RuntimeError(
                "UNIVERSAL_ZIP_VALIDATION_FAILED:"
                + ";".join(candidate_validation["errors"])
            )
        candidate.replace(archive)
        archive_validation = validate_codex_env15_zip(archive, template)
        exported["archive"] = str(archive)
        exported["sha256"] = _sha256_file(archive)
    removed_superseded_archives: list[str] = []
    for legacy_archive in sorted(archive_dir.glob(f"{PACKAGE_FAMILY}_{brain_slug}_*.zip")):
        if legacy_archive.resolve() == archive.resolve():
            continue
        legacy_archive.unlink()
        removed_superseded_archives.append(str(legacy_archive))
    return {
        "status": "PASS",
        "package_family": PACKAGE_FAMILY,
        "package_root": str(destination),
        "archive": str(archive),
        "archive_sha256": exported["sha256"],
        "archive_reused": archive_reused,
        "single_canonical_archive": True,
        "removed_superseded_archives": removed_superseded_archives,
        "authority_sha256": authority_sha256,
        "snapshot_source_root": str(Path(snapshot_source_root or brain_root).resolve()),
        "provider_size_limit_bytes": CODEX_PACKAGE_MAX_BYTES,
        "snapshot": portable["snapshot"],
        "ledger": portable["ledger"],
        "delta_row_id": delta_row_id,
        "env15_derivation": seed,
        "supplied_codex_support": supplied_support,
        "validation": package_validation,
        "archive_validation": archive_validation,
    }


__all__ = [
    "CONTRACT_PATHS",
    "CODEX_PACKAGE_MAX_BYTES",
    "ENV15_PACKAGE_CLASS",
    "ENV15_RESOURCE_ROOT",
    "PACKAGE_FAMILY",
    "PACKAGE_VERSION",
    "create_codex_env15_package",
    "validate_codex_env15_package",
    "validate_codex_env15_zip",
    "validate_env15_template",
]
