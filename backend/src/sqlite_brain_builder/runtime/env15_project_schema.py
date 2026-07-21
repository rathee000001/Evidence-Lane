from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlite_brain_builder.runtime.canonical_lanes import UnknownLaneAliasError, resolve_lane_id


ENV15_PHYSICAL_SECTORS = (
    "artifacts",
    "brain_loader",
    "chat_lineage",
    "custom",
    "data_excel",
    "docs",
    "github_code",
    "images_ocr",
    "local_code",
    "pdf_ocr",
    "ppt",
    "project_engulf",
    "research",
    "sqlite_brain",
)

# The installed Env15 resource remains exactly fourteen physical sectors.
# Runtime project work may add only these governed extension sectors.  They are
# registered in the live router and receipts, never written into the immutable
# resource manifest/PROJECT_SECTOR_REGISTRY.json baseline.
ENV15_RUNTIME_EXTENSION_SECTORS = (
    "delta",
)

ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS = (
    ENV15_PHYSICAL_SECTORS,
    tuple(sorted((*ENV15_PHYSICAL_SECTORS, *ENV15_RUNTIME_EXTENSION_SECTORS))),
)

UNIVERSAL_SECTOR_TABLES = {
    "sector_meta",
    "sector_head",
    "source_registry",
    "artifact_registry",
    "chunk_index",
    "relation_edge",
    "mutation_receipt",
}

ROUTER_TABLES = {
    "sector_registry",
    "sector_mutation_grant",
    "sector_mutation_receipt",
    "backend_ai_mutation_event",
    "brain_build_event",
    "brain_snapshot_registry",
    "brain_snapshot_file_registry",
    "brain_rollback_event",
    "active_project_head",
    "mmd_layout_anchor_registry",
}

# Four governed intake workflows are logical source classifications, not extra
# Env15 physical sectors.  They enter the universal Custom sector with their
# canonical lane ID retained in source/artifact/chunk records.
ENV15_LANE_TO_SECTOR = {
    **{sector: sector for sector in ENV15_PHYSICAL_SECTORS},
    "discussion": "custom",
    "analysis": "custom",
    "plan": "custom",
    "mode": "custom",
}


class Env15ProjectSchemaError(RuntimeError):
    pass


class Env15MutationError(Env15ProjectSchemaError):
    pass


@dataclass(frozen=True)
class Env15ProjectVerification:
    brain_root: str
    sector_count: int
    integrity_passed: bool
    foreign_keys_passed: bool
    router_contract_passed: bool
    universal_contract_passed: bool
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class GovernedMutationReceipt:
    status: str
    lane_id: str
    sector_id: str
    grant_id: str
    mutation_id: str
    prior_snapshot_id: str
    resulting_snapshot_id: str
    previous_logical_hash: str
    current_logical_hash: str
    previous_file_hash: str
    current_file_hash: str
    integrity_check: tuple[str, ...]
    foreign_key_violation_count: int
    sector_receipt_id: str
    router_receipt_id: str
    receipt_path: str
    details: dict[str, Any]


def initialize_env15_brain_root(destination_root: str | Path) -> Env15ProjectVerification:
    """Install Env/UOP plus the live governed Project sector graph.

    Env and UOP are the only locked read authorities.  The retired third
    Project-template/public-read container is removed from every live brain;
    Project pointers resolve directly to the current router and sector bytes.
    """

    destination = Path(destination_root).resolve()
    preinit_runtime: Path | None = None
    if destination.exists() and not (destination / ".uepc_env").is_file():
        files = [path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()]
        if not files:
            shutil.rmtree(destination)
        elif all(path.startswith("project/runtime/") for path in files):
            preinit_runtime = destination.parent / f".{destination.name}.preinit-runtime-{uuid.uuid4().hex}"
            shutil.move(str(destination / "project" / "runtime"), preinit_runtime)
            shutil.rmtree(destination)
    if not destination.exists():
        from sqlite_brain_builder.runtime.env15_resource import install_env15_resource

        install_env15_resource(destination)
        if preinit_runtime is not None:
            runtime_target = destination / "project" / "runtime"
            runtime_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(preinit_runtime), runtime_target)
    elif not (destination / ".uepc_env").is_file():
        raise Env15ProjectSchemaError(f"LEGACY_BRAIN_ENV15_MIGRATION_REQUIRED:{destination}")
    for relative in ("packages", "receipts", "brain_snapshots"):
        (destination / relative).mkdir(parents=True, exist_ok=True)
    from sqlite_brain_builder.runtime.universal_lane_authority import (
        prune_provider_project_lock_artifacts,
    )
    from sqlite_brain_builder.runtime.env15_locked_read import (
        install_supplied_env_uop_database_authority,
    )

    retired = prune_provider_project_lock_artifacts(destination)
    supplied_env_uop = install_supplied_env_uop_database_authority(destination)
    router_path = destination / "project" / "project_router.sqlite"
    router_path.chmod(router_path.stat().st_mode | stat.S_IWUSR)
    router = sqlite3.connect(router_path)
    try:
        router.execute("PRAGMA foreign_keys=ON")
        router.execute("BEGIN IMMEDIATE")
        router.execute(
            "UPDATE sector_registry SET default_access='APPEND_ONLY_READ_WRITE',"
            "automatic_write=1,explicit_one_turn_grant_required=0,relock_after_commit=1 "
            "WHERE sector_id IN ('chat_lineage','research')"
        )
        policies = router.execute(
            "SELECT sector_id,default_access,automatic_write,"
            "explicit_one_turn_grant_required,relock_after_commit "
            "FROM sector_registry WHERE sector_id IN ('chat_lineage','research') "
            "ORDER BY sector_id"
        ).fetchall()
        expected = [
            ("chat_lineage", "APPEND_ONLY_READ_WRITE", 1, 0, 1),
            ("research", "APPEND_ONLY_READ_WRITE", 1, 0, 1),
        ]
        if policies != expected:
            raise Env15ProjectSchemaError(
                f"ENV15_TWO_APPEND_LANE_POLICY_MIGRATION_FAILED:{policies}"
            )
        router.commit()
    except Exception:
        router.rollback()
        raise
    finally:
        router.close()
    migration_receipt = {
        "contract": "EVIDENCE_LANE_ENV_UOP_LIVE_PROJECT_AUTHORITY_MIGRATION_V1",
        "status": "PASS",
        "locked_read_only_authorities": ["env", "uop"],
        "project_authority": "LIVE_GOVERNED_SECTOR_GRAPH",
        "automatic_append_lanes": ["chat_lineage", "research"],
        "retired_third_locked_container": retired,
        "supplied_env_uop_database_authority": supplied_env_uop,
        "created_at": _utc_now(),
    }
    (destination / "receipts" / "ENV_UOP_PROJECT_AUTHORITY_MIGRATION.json").write_text(
        json.dumps(migration_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = verify_env15_live_project(destination)
    if not report.passed:
        raise Env15ProjectSchemaError("ENV15_INITIALIZATION_VALIDATION_FAILED:" + ";".join(report.errors))
    write_env15_project_alignment_receipt(destination)
    return report


def write_env15_project_alignment_receipt(brain_root: str | Path) -> dict[str, Any]:
    """Prove installed project databases match the governed Env15 contract.

    The approved Env15 resource is already aligned. A no-op mutation grant is
    intentionally not issued: grants exist only for real named one-turn
    mutations. This receipt records the locked baseline and the exact policy
    that future mutations must satisfy.
    """
    root = Path(brain_root).resolve()
    router_path = root / "project" / "project_router.sqlite"
    router = sqlite3.connect(f"file:{router_path.as_posix()}?mode=ro", uri=True)
    try:
        registered = router.execute(
            "SELECT sector_id,sqlite_path,default_access,automatic_write,"
            "explicit_one_turn_grant_required,relock_after_commit "
            "FROM sector_registry ORDER BY sector_id"
        ).fetchall()
    finally:
        router.close()
    sectors = []
    for sector_id, relative, access, automatic, grant, relock in registered:
        database = _safe_project_path(root, relative)
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            objects = _object_names(connection)
            integrity = tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))
            foreign_keys = tuple(connection.execute("PRAGMA foreign_key_check"))
        finally:
            connection.close()
        append_only = sector_id in {"chat_lineage", "research"}
        universal_aligned = UNIVERSAL_SECTOR_TABLES <= objects
        policy_valid = (
            (access, automatic, grant, relock) == ("APPEND_ONLY_READ_WRITE", 1, 0, 1)
            if append_only
            else (access, automatic, grant, relock) == ("READ_ONLY", 0, 1, 1)
        )
        sectors.append({
            "sector_id": sector_id,
            "sqlite_path": relative,
            "sha256": _sha256_file(database),
            "size_bytes": database.stat().st_size,
            "integrity_check": list(integrity),
            "foreign_key_violation_count": len(foreign_keys),
            "universal_schema_aligned": universal_aligned,
            "append_only_specialized_exception": append_only,
            "policy_valid": policy_valid,
            "alignment_action": "NO_MUTATION_REQUIRED_ALREADY_ALIGNED",
            "future_mutation_contract": (
                "AUTOMATIC_APPEND_ONLY_SERVICE"
                if append_only
                else "NAMED_ONE_TURN_GRANT_BEFORE_AFTER_HASH_INTEGRITY_FK_ROLLBACK_RELOCK_ROUTER_RECEIPT"
            ),
        })
    registered_ids = tuple(item[0] for item in registered)
    runtime_extension_ids = [
        sector_id for sector_id in registered_ids if sector_id in ENV15_RUNTIME_EXTENSION_SECTORS
    ]
    payload = {
        "contract": "ENV15_PROJECT_DATABASE_ALIGNMENT_V1",
        "status": "PASS" if (
            registered_ids in ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS
            and all(item["integrity_check"] == ["ok"] for item in sectors)
            and all(item["foreign_key_violation_count"] == 0 for item in sectors)
            and all(item["policy_valid"] for item in sectors)
            and all(item["universal_schema_aligned"] or item["append_only_specialized_exception"] for item in sectors)
        ) else "FAIL",
        "router_registered_sector_count": len(registered),
        "immutable_base_sector_count": len(ENV15_PHYSICAL_SECTORS),
        "runtime_extension_sector_ids": runtime_extension_ids,
        "runtime_extension_semantics": "ADDITIVE_LIVE_ROUTER_ONLY_BASE_LEDGER_UNCHANGED",
        "router_sha256": _sha256_file(router_path),
        "authority_boundaries": {
            "env": "LOCKED_ROOT_AUTHORITY_NOT_PROJECT_MUTABLE",
            "uop": "LOCKED_OPERATOR_AUTHORITY_NOT_PROJECT_MUTABLE",
            "project": "ONLY_REGISTERED_SECTOR_DATABASES_MUTABLE_BY_POLICY",
        },
        "sectors": sectors,
        "created_at": _utc_now(),
    }
    path = root / "receipts" / "ENV15_PROJECT_DATABASE_ALIGNMENT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["receipt_path"] = str(path)
    if payload["status"] != "PASS":
        raise Env15ProjectSchemaError("ENV15_PROJECT_DATABASE_ALIGNMENT_FAILED")
    return payload


def write_env15_runtime_sector_state(brain_root: str | Path) -> dict[str, Any]:
    """Write current mutable hashes without rewriting the locked base ledger."""

    root = Path(brain_root).resolve()
    report = verify_env15_live_project(root)
    if not report.passed:
        raise Env15ProjectSchemaError("ENV15_RUNTIME_STATE_VALIDATION_FAILED:" + ";".join(report.errors))
    router_path = root / "project" / "project_router.sqlite"
    router = sqlite3.connect(f"file:{router_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = router.execute(
            "SELECT sector_id,sqlite_path,default_access,automatic_write,explicit_one_turn_grant_required,relock_after_commit "
            "FROM sector_registry ORDER BY sector_id"
        ).fetchall()
    finally:
        router.close()
    sectors = []
    for sector_id, relative, access, automatic, grant, relock in rows:
        path = _safe_project_path(root, relative)
        sectors.append(
            {
                "sector_id": sector_id,
                "sqlite_path": relative,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
                "default_access": access,
                "automatic_write": bool(automatic),
                "explicit_one_turn_grant_required": bool(grant),
                "relock_after_commit": bool(relock),
            }
        )
    payload = {
        "contract": "ENV15_LIVE_PROJECT_RUNTIME_STATE_V1",
        "canonical_live_sector_registry": "project/PROJECT_SECTOR_REGISTRY.json",
        "sector_registry_semantics": "current live router-derived governance registry",
        "sector_count": len(sectors),
        "logical_lane_count": len(ENV15_LANE_TO_SECTOR),
        "logical_lane_to_sector": ENV15_LANE_TO_SECTOR,
        "router_sha256": _sha256_file(router_path),
        "sectors": sectors,
        "created_at": _utc_now(),
    }
    path = root / "receipts" / "ENV15_LIVE_PROJECT_RUNTIME_STATE.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["receipt_path"] = str(path)
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _logical_state_hash(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'mutation_receipt' ORDER BY name"
        )
    ]
    for table in tables:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        quoted = table.replace('"', '""')
        rows = connection.execute(f'SELECT * FROM "{quoted}"').fetchall()
        normalized = sorted(
            (json.dumps([_json_value(value) for value in row], ensure_ascii=False, sort_keys=True, default=str) for row in rows)
        )
        digest.update(table.encode("utf-8"))
        digest.update(json.dumps(columns, separators=(",", ":")).encode("utf-8"))
        for row in normalized:
            digest.update(row.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _safe_project_path(brain_root: Path, relative: str) -> Path:
    candidate = (brain_root / Path(relative.replace("/", os.sep))).resolve()
    project = (brain_root / "project").resolve()
    if candidate != project and project not in candidate.parents:
        raise Env15ProjectSchemaError(f"ENV15_PROJECT_PATH_ESCAPE:{relative}")
    return candidate


def _object_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def verify_env15_live_project(brain_root: str | Path) -> Env15ProjectVerification:
    root = Path(brain_root).resolve()
    router = root / "project" / "project_router.sqlite"
    errors: list[str] = []
    integrity_passed = True
    foreign_keys_passed = True
    router_contract_passed = False
    universal_contract_passed = True
    if not router.is_file():
        errors.append("ENV15_ROUTER_MISSING")
        return Env15ProjectVerification(str(root), 0, False, False, False, False, tuple(errors))

    connection = sqlite3.connect(router)
    try:
        router_contract_passed = ROUTER_TABLES <= _object_names(connection)
        if not router_contract_passed:
            errors.append("ENV15_ROUTER_CONTRACT_MISMATCH")
        rows = connection.execute(
            "SELECT sector_id,sqlite_path FROM sector_registry ORDER BY sector_id"
        ).fetchall()
    finally:
        connection.close()
    sector_ids = tuple(row[0] for row in rows)
    if sector_ids not in ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS:
        errors.append("ENV15_SECTOR_REGISTRY_MISMATCH")
    actual_dirs = tuple(sorted(path.name for path in (root / "project" / "sectors").iterdir() if path.is_dir()))
    if actual_dirs not in ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS:
        errors.append("ENV15_SECTOR_DIRECTORY_MISMATCH")
    if sector_ids != actual_dirs:
        errors.append("ENV15_SECTOR_REGISTRY_DIRECTORY_PARITY_MISMATCH")

    for sector_id, relative in rows:
        database = _safe_project_path(root, relative)
        if not database.is_file():
            errors.append(f"ENV15_SECTOR_DATABASE_MISSING:{sector_id}")
            integrity_passed = False
            continue
        sector = sqlite3.connect(database)
        try:
            integrity = tuple(row[0] for row in sector.execute("PRAGMA integrity_check"))
            foreign_keys = tuple(sector.execute("PRAGMA foreign_key_check"))
            if integrity != ("ok",):
                integrity_passed = False
                errors.append(f"ENV15_SECTOR_INTEGRITY_FAILED:{sector_id}")
            if foreign_keys:
                foreign_keys_passed = False
                errors.append(f"ENV15_SECTOR_FOREIGN_KEY_FAILED:{sector_id}")
            if sector_id not in {"chat_lineage", "github_code", "local_code"}:
                if not UNIVERSAL_SECTOR_TABLES <= _object_names(sector):
                    universal_contract_passed = False
                    errors.append(f"ENV15_UNIVERSAL_SCHEMA_MISMATCH:{sector_id}")
        finally:
            sector.close()
    return Env15ProjectVerification(
        str(root), len(rows), integrity_passed, foreign_keys_passed,
        router_contract_passed, universal_contract_passed, tuple(errors),
    )


def resolve_env15_sector(brain_root: str | Path, lane_id: str) -> tuple[str, Path]:
    requested_lane = lane_id
    requested_normalized = str(lane_id).strip().casefold().replace("-", "_")
    if requested_normalized in {"delta", "plan_delta", "steer_delta"}:
        lane_id = "delta"
        sector_id = "delta"
    else:
        try:
            lane_id = resolve_lane_id(str(lane_id), scope="any")
        except UnknownLaneAliasError as exc:
            raise Env15ProjectSchemaError(f"ENV15_LANE_UNMAPPED:{requested_lane}") from exc
        sector_id = ENV15_LANE_TO_SECTOR.get(lane_id)
    if not sector_id:
        raise Env15ProjectSchemaError(f"ENV15_LANE_UNMAPPED:{lane_id}")
    root = Path(brain_root).resolve()
    router = sqlite3.connect(root / "project" / "project_router.sqlite")
    try:
        row = router.execute(
            "SELECT sqlite_path FROM sector_registry WHERE sector_id=? AND status='ACTIVE_SCHEMA'",
            (sector_id,),
        ).fetchone()
    finally:
        router.close()
    if not row:
        raise Env15ProjectSchemaError(f"ENV15_SECTOR_NOT_ACTIVE:{sector_id}")
    return sector_id, _safe_project_path(root, row[0])


def _set_writable(path: Path, writable: bool) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _snapshot_sector(
    brain_root: Path,
    router: sqlite3.Connection,
    sector_id: str,
    sector_path: Path,
    actor: str,
    reason: str,
    parent_snapshot_id: str | None,
    current: bool,
) -> tuple[str, Path, str]:
    snapshot_id = "snapshot_" + uuid.uuid4().hex
    relative = sector_path.relative_to(brain_root)
    snapshot = brain_root / "brain_snapshots" / snapshot_id / relative
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    # The governed sector is closed at every snapshot boundary.  Preserve its
    # exact bytes so a failed mutation can restore the pre-transaction hash,
    # not merely an equivalent SQLite logical image.
    shutil.copy2(sector_path, snapshot)
    snapshot_hash = _sha256_file(snapshot)
    created_at = _utc_now()
    if current:
        router.execute("UPDATE brain_snapshot_registry SET current_flag=0 WHERE current_flag=1")
    router.execute(
        "INSERT INTO brain_snapshot_registry VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_id, None, actor, reason, created_at,
            snapshot.relative_to(brain_root).as_posix(), snapshot_hash,
            parent_snapshot_id, 1, 1 if current else 0, "VALID",
        ),
    )
    router.execute(
        "INSERT INTO brain_snapshot_file_registry VALUES(?,?,?,?)",
        (snapshot_id, relative.as_posix(), snapshot.stat().st_size, snapshot_hash),
    )
    return snapshot_id, snapshot, snapshot_hash


def governed_sector_mutation(
    brain_root: str | Path,
    lane_id: str,
    *,
    actor: str,
    reason: str,
    turn_id: str,
    mutate: Callable[[sqlite3.Connection], dict[str, Any] | None],
    vacuum_after_commit: bool = False,
) -> GovernedMutationReceipt:
    root = Path(brain_root).resolve()
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    turn_id = str(turn_id or "").strip()
    if not actor or not reason or not turn_id:
        raise Env15MutationError("ENV15_MUTATION_ACTOR_REASON_TURN_REQUIRED")
    sector_id, sector_path = resolve_env15_sector(root, lane_id)
    if sector_id in {"chat_lineage", "research"}:
        raise Env15MutationError(
            f"{sector_id.upper()}_REQUIRES_APPEND_ONLY_SERVICE"
        )

    verification = verify_env15_live_project(root)
    if not verification.passed:
        raise Env15MutationError("ENV15_PROJECT_VERIFICATION_FAILED:" + ";".join(verification.errors))

    router_path = root / "project" / "project_router.sqlite"
    router = sqlite3.connect(router_path)
    router.execute("PRAGMA foreign_keys=ON")
    policy = router.execute(
        "SELECT default_access,automatic_write,explicit_one_turn_grant_required,relock_after_commit "
        "FROM sector_registry WHERE sector_id=?",
        (sector_id,),
    ).fetchone()
    if policy != ("READ_ONLY", 0, 1, 1):
        router.close()
        raise Env15MutationError(f"ENV15_SECTOR_POLICY_MISMATCH:{sector_id}:{policy}")

    issued_at = _utc_now()
    grant_id = "grant_" + uuid.uuid4().hex
    mutation_id = "mutation_" + uuid.uuid4().hex
    router.execute(
        "INSERT INTO sector_mutation_grant VALUES(?,?,?,?,?,?,?)",
        (grant_id, sector_id, actor, reason, issued_at, turn_id, "ISSUED"),
    )
    parent = router.execute(
        "SELECT snapshot_id FROM brain_snapshot_registry WHERE current_flag=1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    prior_snapshot_id, prior_snapshot, _ = _snapshot_sector(
        root, router, sector_id, sector_path, actor, f"before mutation: {reason}",
        parent[0] if parent else None, False,
    )
    router.execute(
        "INSERT INTO backend_ai_mutation_event VALUES(?,?,?,?,?,?,?,?,?)",
        (mutation_id, actor, reason, sector_id, prior_snapshot_id, None, issued_at, None, "RUNNING"),
    )
    router.commit()

    previous_file_hash = _sha256_file(sector_path)
    _set_writable(sector_path, True)
    previous_logical_hash = ""
    current_logical_hash = ""
    sector_receipt_id = "sector_receipt_" + uuid.uuid4().hex
    router_receipt_id = "router_receipt_" + uuid.uuid4().hex
    details: dict[str, Any] = {}
    sector: sqlite3.Connection | None = None
    try:
        sector = sqlite3.connect(sector_path)
        sector.execute("PRAGMA foreign_keys=ON")
        previous_logical_hash = _logical_state_hash(sector)
        sector.execute("BEGIN IMMEDIATE")
        details = dict(mutate(sector) or {})
        current_logical_hash = _logical_state_hash(sector)
        sector.execute(
            "INSERT INTO mutation_receipt VALUES(?,?,?,?,?,?,?)",
            (
                sector_receipt_id, grant_id, turn_id, previous_logical_hash,
                current_logical_hash, "RELOCKED", _utc_now(),
            ),
        )
        sector.commit()
        if vacuum_after_commit:
            sector.execute("VACUUM")
            details["physical_compaction"] = "VACUUM_AFTER_GOVERNED_MUTATION"
        sector.close()
        sector = None
        _set_writable(sector_path, False)

        current_file_hash = _sha256_file(sector_path)
        read_only = sqlite3.connect(f"file:{sector_path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = tuple(row[0] for row in read_only.execute("PRAGMA integrity_check"))
            foreign_keys = tuple(read_only.execute("PRAGMA foreign_key_check"))
        finally:
            read_only.close()
        if integrity != ("ok",) or foreign_keys:
            raise Env15MutationError("ENV15_POST_MUTATION_SQLITE_VALIDATION_FAILED")

        resulting_snapshot_id, _, _ = _snapshot_sector(
            root, router, sector_id, sector_path, actor, f"after mutation: {reason}",
            prior_snapshot_id, True,
        )
        committed_at = _utc_now()
        router.execute(
            "INSERT INTO sector_mutation_receipt VALUES(?,?,?,?,?,?,?,?,?)",
            (
                router_receipt_id, grant_id, sector_id, actor, reason,
                previous_file_hash, current_file_hash, committed_at, 1,
            ),
        )
        router.execute("UPDATE sector_mutation_grant SET status='CONSUMED' WHERE grant_id=?", (grant_id,))
        router.execute(
            "UPDATE backend_ai_mutation_event SET resulting_snapshot_id=?,completed_at=?,status='PASS' WHERE mutation_id=?",
            (resulting_snapshot_id, committed_at, mutation_id),
        )
        router.execute(
            "UPDATE active_project_head SET latest_committed_turn=?,latest_state_hash=?,updated_at=? WHERE singleton_id=1",
            (turn_id, current_file_hash, committed_at),
        )
        router.commit()

        receipt_payload = {
            "status": "PASS", "lane_id": lane_id, "sector_id": sector_id,
            "grant_id": grant_id, "mutation_id": mutation_id,
            "prior_snapshot_id": prior_snapshot_id, "resulting_snapshot_id": resulting_snapshot_id,
            "previous_logical_hash": previous_logical_hash, "current_logical_hash": current_logical_hash,
            "previous_file_hash": previous_file_hash, "current_file_hash": current_file_hash,
            "integrity_check": list(integrity), "foreign_key_violation_count": len(foreign_keys),
            "sector_receipt_id": sector_receipt_id, "router_receipt_id": router_receipt_id,
            "details": details,
        }
        receipt_path = root / "receipts" / f"{router_receipt_id}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return GovernedMutationReceipt(
            **{key: value for key, value in receipt_payload.items() if key not in {"integrity_check"}},
            integrity_check=tuple(integrity), receipt_path=str(receipt_path),
        )
    except Exception:
        try:
            if sector is not None:
                sector.rollback()
                sector.close()
                sector = None
            _set_writable(sector_path, True)
            shutil.copy2(prior_snapshot, sector_path)
            _set_writable(sector_path, False)
        finally:
            router.execute("UPDATE sector_mutation_grant SET status='REVOKED' WHERE grant_id=?", (grant_id,))
            router.execute(
                "UPDATE backend_ai_mutation_event SET completed_at=?,status='FAILED_ROLLED_BACK' WHERE mutation_id=?",
                (_utc_now(), mutation_id),
            )
            router.commit()
            router.close()
        raise
    finally:
        try:
            router.close()
        except Exception:
            pass


__all__ = [
    "ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS",
    "ENV15_LANE_TO_SECTOR",
    "ENV15_PHYSICAL_SECTORS",
    "ENV15_RUNTIME_EXTENSION_SECTORS",
    "UNIVERSAL_SECTOR_TABLES",
    "Env15MutationError",
    "Env15ProjectSchemaError",
    "Env15ProjectVerification",
    "GovernedMutationReceipt",
    "governed_sector_mutation",
    "initialize_env15_brain_root",
    "resolve_env15_sector",
    "verify_env15_live_project",
    "write_env15_project_alignment_receipt",
    "write_env15_runtime_sector_state",
]
