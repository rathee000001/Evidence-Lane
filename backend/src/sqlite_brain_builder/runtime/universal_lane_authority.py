from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlite_brain_builder.runtime.canonical_lanes import (
    CANONICAL_LANE_IDS,
    LANE_REGISTRY,
    PRIMARY_CODE_LANES,
    registry_payload,
)
from sqlite_brain_builder.runtime.env15_project_schema import ENV15_LANE_TO_SECTOR


AUTOMATIC_APPEND_LANES = frozenset({"chat_lineage", "research"})
PUBLIC_MODEL_PROJECT_ACCESS = "READ_ONLY"
PUBLIC_MODEL_PROJECT_MUTATION = False
PROVIDER_SPECIFIC_PROJECT_DELTA = False
UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY = True
OTHER_SECTOR_WRITE_SEQUENCE = (
    "USER_EXPLICIT_EXACT_SECTOR->CLASSIFIER->SECTOR_LAW->WRITE->RECEIPT"
)
LANE_CLASSIFICATIONS = frozenset({"LOADED", "SKIPPED", "FAILED", "NOT_APPLICABLE"})
LINEAGE_HEAD_RELATIVE = "project/lineage/LINEAGE_HEAD.json"
LINEAGE_HEAD_CONTRACT = "EVIDENCE_LANE_CANONICAL_LINEAGE_HEAD_V1"
PROJECT_LOCK_ARTIFACT_PATHS = (
    "project/project_template.sqlite",
    "project/project_topology_template.mmd",
    "project/project_topology_template.svg",
    "project/project_topology_template.png",
    "project/project_template_footprint.md",
    "project/locked_mmd_hash.txt",
    "project_template_locked",
    "public_read",
)
FORBIDDEN_PROVIDER_PROJECT_LOCK_TERMS = (
    "project_locked",
    "project_template_locked",
    "project_template_sqlite=",
    "public_env_uop_project_locked",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _set_writable(path: Path) -> None:
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _set_writable(path)
    path.write_text(text, encoding="utf-8")


def _write_pointer_file(path: Path, rows: Iterable[tuple[str, object]]) -> None:
    _write_text(path, "".join(f"{key}={value}\n" for key, value in rows))


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _source_count(connection: sqlite3.Connection, lane_id: str, physical_sector: str) -> int:
    tables = _table_names(connection)
    if "source_registry" not in tables:
        return 0
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(source_registry)")}
    if "lane_id" in columns:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM source_registry WHERE lane_id=?", (lane_id,)
            ).fetchone()[0]
        )
    if "lane_key" in columns:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM source_registry WHERE lane_key=?", (lane_id,)
            ).fetchone()[0]
        )
    return int(connection.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0]) if lane_id == physical_sector else 0


_NON_PAYLOAD_TABLES = frozenset(
    {
        "sector_meta",
        "sector_head",
        "mutation_receipt",
        "mutation_grant",
        "canonical_delta_pointer",
        "code_index_checkpoint",
        "code_projection_state",
        "code_source_active_head",
        "code_snapshot_history",
    }
)


def _content_row_count(connection: sqlite3.Connection) -> int:
    """Count real payload rows without trusting stale sector status labels."""

    total = 0
    for table in sorted(_table_names(connection)):
        lowered = table.casefold()
        if table in _NON_PAYLOAD_TABLES or lowered.startswith("code_chunk_fts_"):
            continue
        try:
            total += int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        except sqlite3.DatabaseError:
            continue
    return total


def _payload_state(connection: sqlite3.Connection, source_count: int) -> tuple[str, int]:
    content_rows = _content_row_count(connection)
    return ("POPULATED" if source_count or content_rows else "SCHEMA_READY", content_rows)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _authority_derivation_identity(
    root: Path,
    *,
    sectors: Mapping[str, Mapping[str, Any]],
    active_lane_ids: set[str],
    ingestion_results: Mapping[str, Mapping[str, Any]],
    package_use_mode: str,
    brain_name: str | None,
) -> str:
    sector_state: list[dict[str, Any]] = []
    for sector_id, sector in sorted(sectors.items()):
        relative = str(sector["sqlite_path"]).replace("\\", "/")
        database = root / Path(relative.replace("/", os.sep))
        sector_state.append(
            {
                "sector_id": sector_id,
                "sqlite_path": relative,
                "byte_size": database.stat().st_size,
                "sha256": _sha256_file(database),
            }
        )
    payload = {
        "contract": "EVIDENCE_LANE_UNIVERSAL_AUTHORITY_DERIVATION_INPUT_V1",
        "brain_name": brain_name or root.name,
        "package_use_mode": package_use_mode,
        "active_lane_ids": sorted(active_lane_ids),
        "ingestion_status_by_lane": {
            lane_id: str(item.get("status") or "")
            for lane_id, item in sorted(ingestion_results.items())
        },
        "router_sha256": _sha256_file(root / "project" / "project_router.sqlite"),
        "sector_state": sector_state,
    }
    return _canonical_sha256(payload)


def _latest_sequence(connection: sqlite3.Connection, candidates: Iterable[str]) -> int:
    tables = _table_names(connection)
    for table in candidates:
        if table not in tables:
            continue
        columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
        for column in (
            "event_sequence",
            "turn_sequence",
            "delta_sequence",
            "latest_sequence",
            "sequence",
            "ordinal",
            "prompt_index",
        ):
            if column in columns:
                row = connection.execute(
                    f'SELECT MAX(CAST("{column}" AS INTEGER)) FROM "{table}"'
                ).fetchone()
                return int((row or [0])[0] or 0)
    return 0


def _lane_sequence(root: Path, lane_id: str, physical_sector: str) -> int:
    database = root / "project" / "sectors" / physical_sector / f"{physical_sector}_sector_v001.sqlite"
    if not database.is_file():
        return 0
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        if lane_id == "chat_lineage":
            return _latest_sequence(
                connection,
                (
                    "lineage_event",
                    "chat_event",
                    "chat_turn",
                    "turn_record",
                    "prompt_response_turn",
                ),
            )
        if lane_id == "delta":
            return _latest_sequence(connection, ("canonical_delta_pointer", "delta_record"))
        return 0
    finally:
        connection.close()


def _write_lineage_slips(root: Path, lineage_head: Mapping[str, Any]) -> None:
    head_sha256 = str(lineage_head["head_sha256"])
    common = (
        f"LINEAGE_HEAD={LINEAGE_HEAD_RELATIVE}\n"
        f"LINEAGE_HEAD_SHA256={head_sha256}\n"
        f"PROJECT_ID={lineage_head['project_id']}\n"
        f"PROJECT_STATE={lineage_head['project_state']}\n"
        f"NEXT_EXPECTED_INDEX={lineage_head['next_expected_index']}\n"
        f"ACTIVE_LANES={','.join(lineage_head['active_lane_ids']) or 'NONE'}\n"
        f"UPDATED_AT={lineage_head['updated_at']}\n"
    )
    _write_text(
        root / "receipts" / "last_entry_slip.txt",
        "ENTRY_SLIP_CONTRACT=EVIDENCE_LANE_LINEAGE_HEAD_DERIVED_SLIP_V1\n"
        "ENTRY_STATE=CANONICAL_LINEAGE_HEAD_VERIFIED\n"
        + common,
    )
    _write_text(
        root / "receipts" / "last_exit_slip.txt",
        "EXIT_SLIP_CONTRACT=EVIDENCE_LANE_LINEAGE_HEAD_DERIVED_SLIP_V1\n"
        "EXIT_STATE=UNTRUSTED_CANDIDATE_REQUIRES_HIL\n"
        + common,
    )


def _router_sectors(root: Path) -> dict[str, dict[str, Any]]:
    router_path = root / "project" / "project_router.sqlite"
    connection = sqlite3.connect(f"file:{router_path.as_posix()}?mode=ro", uri=True)
    try:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sector_registry)")}
        required = {"sector_id", "sqlite_path"}
        if not required <= columns:
            raise RuntimeError("UNIVERSAL_POINTER_ROUTER_SCHEMA_INVALID")
        select = ["sector_id", "sqlite_path"]
        for optional in (
            "default_access",
            "automatic_write",
            "explicit_one_turn_grant_required",
            "relock_after_commit",
        ):
            if optional in columns:
                select.append(optional)
        rows = connection.execute(
            f"SELECT {','.join(select)} FROM sector_registry ORDER BY sector_id"
        ).fetchall()
    finally:
        connection.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(zip(select, row))
        result[str(item["sector_id"])] = item
    return result


def _lane_build_state(
    lane_id: str,
    active_lane_ids: set[str],
    source_count: int,
    ingestion: Mapping[str, Any],
) -> tuple[str, str, str]:
    status = str(ingestion.get("status") or "").strip().upper()
    if lane_id in active_lane_ids:
        if status.startswith(("FAIL", "ERROR", "BLOCKED", "REVIEW_REQUIRED")):
            return "FAILED", "PRESERVED_PRE_WRITE_OR_ROLLED_BACK", status
        return (
            "LOADED",
            "CURRENT_BUILD_SOURCE_REGISTERED"
            if not status.startswith("SKIPPED")
            else "CURRENT_SOURCE_REUSED_WITHOUT_REINDEX",
            status or "PASS",
        )
    if lane_id in AUTOMATIC_APPEND_LANES:
        return "SKIPPED", "APPEND_SERVICE_READY_NO_REGISTERED_BUILD_SOURCE", "SKIPPED_NO_SOURCE"
    if source_count:
        return "SKIPPED", "PRESERVED_PRIOR_DATA", "SKIPPED_NO_SOURCE"
    return "SKIPPED", "PRESERVED_SCHEMA_READY_BYTES", "SKIPPED_NO_SOURCE"


def write_universal_lane_authority(
    brain_root: str | Path,
    *,
    active_lane_ids: Iterable[str] = (),
    ingestion_results: Iterable[Mapping[str, Any]] = (),
    package_use_mode: str = "CANONICAL_FLASHABLE",
    brain_name: str | None = None,
) -> dict[str, Any]:
    """Regenerate Env/UOP authority and universal live-project lane pointers.

    Env and UOP are the only locked authorities.  Every model-facing lane
    pointer resolves directly to the live Project sector database.  Omitted
    source lanes are represented as ``SKIPPED_NO_SOURCE`` and their database
    bytes are never modified by this writer.
    """

    root = Path(brain_root).resolve()
    project = root / "project"
    router_path = project / "project_router.sqlite"
    if not router_path.is_file():
        raise RuntimeError(f"UNIVERSAL_POINTER_ROUTER_MISSING:{router_path}")
    active = {str(lane_id) for lane_id in active_lane_ids}
    result_by_lane = {
        str(item.get("lane_id")): dict(item)
        for item in ingestion_results
        if item.get("lane_id")
    }
    sectors = _router_sectors(root)
    derivation_input_sha256 = _authority_derivation_identity(
        root,
        sectors=sectors,
        active_lane_ids=active,
        ingestion_results=result_by_lane,
        package_use_mode=package_use_mode,
        brain_name=brain_name,
    )
    stamp = f"AUTHORITY_INPUT_SHA256:{derivation_input_sha256}"
    pointer_root = project / "pointers"
    pointer_root.mkdir(parents=True, exist_ok=True)
    lane_pointers: list[dict[str, Any]] = []

    for lane_id in CANONICAL_LANE_IDS:
        definition = LANE_REGISTRY[lane_id]
        physical_sector = ENV15_LANE_TO_SECTOR[lane_id]
        sector = sectors.get(physical_sector)
        if not sector:
            raise RuntimeError(f"UNIVERSAL_POINTER_SECTOR_UNREGISTERED:{lane_id}:{physical_sector}")
        relative_database = str(sector["sqlite_path"]).replace("\\", "/")
        database = (root / Path(relative_database.replace("/", os.sep))).resolve()
        if not database.is_file() or project not in database.parents:
            raise RuntimeError(f"UNIVERSAL_POINTER_DATABASE_INVALID:{lane_id}:{relative_database}")
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            source_count = _source_count(connection, lane_id, physical_sector)
            payload_state, content_row_count = _payload_state(connection, source_count)
        finally:
            connection.close()
        automatic = lane_id in AUTOMATIC_APPEND_LANES
        ingestion = result_by_lane.get(lane_id, {})
        build_state, preservation_state, evidence_status = _lane_build_state(
            lane_id, active, source_count, ingestion
        )
        pointer = {
            "contract": "EVIDENCE_LANE_UNIVERSAL_LANE_POINTER_V1",
            "lane_id": lane_id,
            "display_name": definition.display_label,
            "physical_sector_id": physical_sector,
            "database": relative_database,
            "database_sha256": _sha256_file(database),
            "database_size_bytes": database.stat().st_size,
            "registered_source_count": source_count,
            "content_row_count": content_row_count,
            "registered_this_build": lane_id in active,
            "build_state": build_state,
            "lane_classification": build_state,
            "source_intake_state": build_state,
            "classification_evidence": {
                "status": evidence_status,
                "registered_this_build": lane_id in active,
                "registered_source_count": source_count,
                "database_sha256": _sha256_file(database),
                "database_size_bytes": database.stat().st_size,
            },
            "preservation_state": preservation_state,
            "payload_state": payload_state,
            "ingestion_status": evidence_status,
            "append_service_state": (
                "READY_APPEND_ONLY" if automatic else "NOT_APPLICABLE"
            ),
            "automatic_append": automatic,
            "access": (
                "APPEND_ONLY_AUTOMATIC_RECEIPT_RELOCK"
                if automatic
                else "READ_ONLY_UNLESS_EXPLICIT_NAMED_ONE_TURN_GRANT"
            ),
            "hil_gate": (
                "AUTOMATIC_APPEND_HASH_RECEIPT_RELOCK"
                if automatic
                else "USER_NAMED_LANE_ONE_TURN_HIL_GRANT"
            ),
            "output_identity_required": True,
            "output_stamp_fields": [
                "stable_id",
                "actor",
                "provider",
                "source_time_when_known",
                "ingested_at",
                "content_sha256",
            ],
            "local_github_intake_exclusivity": "MUTUALLY_EXCLUSIVE_ONE_PROJECT_DATABASE",
            "derivation_input_sha256": derivation_input_sha256,
            "updated_at": stamp,
        }
        lane_pointers.append(pointer)

    router_sha256 = _sha256_file(router_path)
    physical_sector_count = len(sectors)
    chat_sequence = _lane_sequence(root, "chat_lineage", ENV15_LANE_TO_SECTOR["chat_lineage"])
    delta_sequence = _lane_sequence(root, "delta", "delta")
    populated_lanes = sorted(
        pointer["lane_id"] for pointer in lane_pointers if pointer["payload_state"] == "POPULATED"
    )
    active_code_modes = sorted(PRIMARY_CODE_LANES.intersection(active))
    if len(active_code_modes) > 1:
        raise RuntimeError(
            "UNIVERSAL_POINTER_PRIMARY_CODE_MODE_CONFLICT:github_code,local_code"
        )
    primary_code_mode = active_code_modes[0] if active_code_modes else None
    pointer_by_lane = {
        str(pointer["lane_id"]): pointer for pointer in lane_pointers
    }
    canonical_definitions = registry_payload()
    canonical_registry = {
        "contract": "EVIDENCE_LANE_CANONICAL_LANE_REGISTRY_V1",
        "status": "CURRENT_UNTRUSTED_CANDIDATE",
        "classification_law": sorted(LANE_CLASSIFICATIONS),
        "locked_read_only_authorities": ["env", "uop"],
        "project_authority": "LIVE_GOVERNED_SECTOR_GRAPH",
        "project_authority_class": "PROJECT_MUTABLE_SECTOR",
        "automatic_append_lanes": sorted(AUTOMATIC_APPEND_LANES),
        "public_model_project_access": PUBLIC_MODEL_PROJECT_ACCESS,
        "public_model_project_mutation": PUBLIC_MODEL_PROJECT_MUTATION,
        "provider_specific_project_delta": PROVIDER_SPECIFIC_PROJECT_DELTA,
        "universal_refresh_project_change_authority": UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY,
        "classification_authorizes_write": False,
        "other_sector_write_sequence": OTHER_SECTOR_WRITE_SEQUENCE,
        "primary_code_mode": primary_code_mode,
        "primary_code_mode_law": (
            "EXACTLY_ZERO_OR_ONE_OF_LOCAL_CODE_GITHUB_CODE;LOCAL_WITH_DOT_GIT_REMAINS_LOCAL"
        ),
        "derivation_input_sha256": derivation_input_sha256,
        "lanes": [],
        "updated_at": stamp,
    }
    for lane_id in CANONICAL_LANE_IDS:
        definition = dict(canonical_definitions[lane_id])
        pointer = pointer_by_lane[lane_id]
        canonical_registry["lanes"].append(
            {
                **definition,
                "physical_sector_id": pointer["physical_sector_id"],
                "sqlite_path": pointer["database"],
                "lane_classification": pointer["lane_classification"],
                "classification_evidence": pointer["classification_evidence"],
                "registered_source_count": pointer["registered_source_count"],
                "payload_state": pointer["payload_state"],
                "access": pointer["access"],
                "automatic_append": pointer["automatic_append"],
                "primary_code_role": (
                    "SELECTED_PRIMARY"
                    if lane_id == primary_code_mode
                    else "INACTIVE_ALTERNATE"
                    if lane_id in PRIMARY_CODE_LANES
                    else "NOT_APPLICABLE"
                ),
            }
        )
    canonical_registry_path = project / "pointers" / "CANONICAL_LANE_REGISTRY.json"
    _write_text(
        canonical_registry_path,
        json.dumps(canonical_registry, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
    canonical_registry_sha256 = _sha256_file(canonical_registry_path)

    live_sector_registry = {
        "contract": "EVIDENCE_LANE_LIVE_PROJECT_SECTOR_REGISTRY_V1",
        "status": "CURRENT_UNTRUSTED_CANDIDATE",
        "router": "project/project_router.sqlite",
        "router_sha256": router_sha256,
        "sector_count": len(sectors),
        "sectors": [],
        "updated_at": stamp,
    }
    for sector_id, sector in sorted(sectors.items()):
        relative = str(sector["sqlite_path"]).replace("\\", "/")
        database = root / Path(relative.replace("/", os.sep))
        live_sector_registry["sectors"].append(
            {
                "sector_id": sector_id,
                "sqlite_path": relative,
                "sha256": _sha256_file(database),
                "size_bytes": database.stat().st_size,
                "default_access": sector.get("default_access"),
                "automatic_write": bool(sector.get("automatic_write", 0)),
                "explicit_one_turn_grant_required": bool(
                    sector.get("explicit_one_turn_grant_required", 0)
                ),
                "relock_after_commit": bool(sector.get("relock_after_commit", 1)),
            }
        )
    live_sector_registry_text = (
        json.dumps(live_sector_registry, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    _write_text(project / "PROJECT_SECTOR_REGISTRY.json", live_sector_registry_text)
    _write_text(root / "manifests" / "PROJECT_SECTOR_REGISTRY.json", live_sector_registry_text)

    env_project_law = (
        "# Evidence Lane Env project-authority law\n\n"
        "Env and UOP are the only locked read-only authorities. Project is the live governed "
        "sector graph addressed by the canonical lane registry and lineage head. No separate "
        "project-locked or public-read authority exists. Chat Lineage and Research are automatic "
        "append-only services. Model outputs, proposed patches, exposed resource usage, and output "
        "artifacts are evidence records appended through those existing governed services; they "
        "never prove or apply a project change. Public-model project access is read-only, provider "
        "identity never owns project-change truth, and provider-specific project Deltas are disabled. "
        "Only the universal Refresh route may compare the local working project with the previously "
        "accepted mapped state and create project-change truth. Every other lane requires an explicit "
        "user command naming the exact destination sector before classification, followed by sector-law "
        "validation, a foreign-key-on transaction, receipt, and relock. Classification routes content; "
        "it never grants permission. Unloaded lanes are SKIPPED "
        "without deleting prior bytes. Local Code and GitHub Code are mutually exclusive primary "
        "modes; a local checkout containing .git remains Local Code.\n"
    )
    uop_project_law = (
        "# Evidence Lane UOP live-project operator law\n\n"
        "Resolve all aliases through project/pointers/CANONICAL_LANE_REGISTRY.json. Trust only "
        "LOADED, SKIPPED, FAILED, or NOT_APPLICABLE classifications with byte/hash evidence. "
        "Never infer loaded state from package size or a PASS receipt. Research and Chat Lineage "
        "append in prepare/commit order; no hidden chain-of-thought is stored. Visible reasoning "
        "means only reasoning actually exposed by the provider. Provider output is proposed evidence, "
        "not canonical project truth. Universal Refresh alone detects actual local working-project "
        "changes, regardless of provider provenance. Receipts cannot promote candidate state; explicit "
        "human HIL is the only promotion authority.\n"
    )
    env_law_path = root / "env" / "evidence_lane_project_authority_law.md"
    uop_law_path = root / "uop" / "evidence_lane_live_project_operator_law.md"
    _write_text(env_law_path, env_project_law)
    _write_text(uop_law_path, uop_project_law)
    env_law_path.chmod(
        env_law_path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )
    uop_law_path.chmod(
        uop_law_path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )
    next_expected_index = (
        f"EVIDENCE_LANE.CHAT.{chat_sequence + 1:09d}.DELTA.{delta_sequence + 1:09d}"
    )
    lineage_payload = {
        "contract": LINEAGE_HEAD_CONTRACT,
        "status": "CURRENT_UNTRUSTED_CANDIDATE",
        "acceptance_state": "UNTRUSTED_CANDIDATE",
        "promotion_authority": "EXPLICIT_HUMAN_HIL_ONLY",
        "project_id": brain_name or root.name,
        "project_state": "LIVE_GOVERNED_SECTOR_GRAPH",
        "package_use_mode": package_use_mode,
        "locked_read_only_authorities": ["env", "uop"],
        "project_authority": "LIVE_GOVERNED_SECTOR_GRAPH",
        "router": "project/project_router.sqlite",
        "router_sha256": router_sha256,
        "physical_sector_count": physical_sector_count,
        "logical_lane_count": len(lane_pointers),
        "active_lane_ids": sorted(active),
        "populated_lane_ids": populated_lanes,
        "automatic_append_lanes": sorted(AUTOMATIC_APPEND_LANES),
        "public_model_project_access": PUBLIC_MODEL_PROJECT_ACCESS,
        "public_model_project_mutation": PUBLIC_MODEL_PROJECT_MUTATION,
        "provider_specific_project_delta": PROVIDER_SPECIFIC_PROJECT_DELTA,
        "universal_refresh_project_change_authority": UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY,
        "classification_authorizes_write": False,
        "other_sector_write_sequence": OTHER_SECTOR_WRITE_SEQUENCE,
        "other_lane_mutation": "USER_NAMED_ONE_TURN_HIL_GRANT",
        "local_github_intake_exclusivity": "MUTUALLY_EXCLUSIVE_ONE_PROJECT_DATABASE",
        "primary_code_mode": primary_code_mode,
        "derivation_input_sha256": derivation_input_sha256,
        "canonical_lane_registry": "project/pointers/CANONICAL_LANE_REGISTRY.json",
        "canonical_lane_registry_sha256": canonical_registry_sha256,
        "env_project_authority_law": "env/evidence_lane_project_authority_law.md",
        "uop_live_project_operator_law": "uop/evidence_lane_live_project_operator_law.md",
        "chat_lineage_sequence": chat_sequence,
        "delta_sequence": delta_sequence,
        "next_expected_index": next_expected_index,
        "lane_state_vector": [
            {
                key: pointer[key]
                for key in (
                    "lane_id",
                    "physical_sector_id",
                    "database",
                    "database_sha256",
                    "database_size_bytes",
                    "registered_source_count",
                    "content_row_count",
                    "registered_this_build",
                    "lane_classification",
                    "source_intake_state",
                    "classification_evidence",
                    "preservation_state",
                    "payload_state",
                    "ingestion_status",
                    "automatic_append",
                    "append_service_state",
                    "access",
                )
            }
            for pointer in lane_pointers
        ],
        "updated_at": stamp,
    }
    lineage_head = {
        **lineage_payload,
        "head_sha256": _canonical_sha256(lineage_payload),
    }
    lineage_path = root / LINEAGE_HEAD_RELATIVE
    _write_text(
        lineage_path,
        json.dumps(lineage_head, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _write_lineage_slips(root, lineage_head)

    for pointer in lane_pointers:
        pointer["lineage_head"] = LINEAGE_HEAD_RELATIVE
        pointer["lineage_head_sha256"] = lineage_head["head_sha256"]
        lane_id = str(pointer["lane_id"])
        physical_sector = str(pointer["physical_sector_id"])
        text = json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _write_text(pointer_root / f"{lane_id}_pointer.json", text)
        _write_text(
            project / "sectors" / physical_sector / f"{lane_id}_pointer.json",
            text,
        )

    index_payload = {
        "contract": "EVIDENCE_LANE_UNIVERSAL_LANE_POINTER_INDEX_V1",
        "status": "CURRENT_UNTRUSTED_CANDIDATE",
        "acceptance_state": "UNTRUSTED_CANDIDATE",
        "authority": {
            "locked_read_only": ["env", "uop"],
            "project": "LIVE_GOVERNED_SECTOR_GRAPH",
            "project_authority_class": "PROJECT_MUTABLE_SECTOR",
            "automatic_append_lanes": sorted(AUTOMATIC_APPEND_LANES),
            "public_model_project_access": PUBLIC_MODEL_PROJECT_ACCESS,
            "public_model_project_mutation": PUBLIC_MODEL_PROJECT_MUTATION,
            "provider_specific_project_delta": PROVIDER_SPECIFIC_PROJECT_DELTA,
            "universal_refresh_project_change_authority": UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY,
            "classification_authorizes_write": False,
            "other_sector_write_sequence": OTHER_SECTOR_WRITE_SEQUENCE,
            "other_lane_mutation": "USER_NAMED_ONE_TURN_HIL_GRANT",
        },
        "package_use_mode": package_use_mode,
        "lineage_head": LINEAGE_HEAD_RELATIVE,
        "lineage_head_sha256": lineage_head["head_sha256"],
        "canonical_lane_registry": "project/pointers/CANONICAL_LANE_REGISTRY.json",
        "canonical_lane_registry_sha256": canonical_registry_sha256,
        "primary_code_mode": primary_code_mode,
        "derivation_input_sha256": derivation_input_sha256,
        "next_expected_index": next_expected_index,
        "lane_count": len(lane_pointers),
        "lanes": lane_pointers,
        "updated_at": stamp,
    }
    _write_text(
        pointer_root / "INDEX.json",
        json.dumps(index_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    project_pointer = {
        "contract": "EVIDENCE_LANE_LIVE_PROJECT_POINTER_V1",
        "router": "project/project_router.sqlite",
        "router_sha256": router_sha256,
        "universal_lane_pointer_index": "project/pointers/INDEX.json",
        "physical_sector_count": physical_sector_count,
        "logical_lane_count": len(lane_pointers),
        "lineage_head": LINEAGE_HEAD_RELATIVE,
        "lineage_head_sha256": lineage_head["head_sha256"],
        "canonical_lane_registry": "project/pointers/CANONICAL_LANE_REGISTRY.json",
        "canonical_lane_registry_sha256": canonical_registry_sha256,
        "next_expected_index": next_expected_index,
        "automatic_append_lanes": sorted(AUTOMATIC_APPEND_LANES),
        "public_model_project_access": PUBLIC_MODEL_PROJECT_ACCESS,
        "public_model_project_mutation": PUBLIC_MODEL_PROJECT_MUTATION,
        "provider_specific_project_delta": PROVIDER_SPECIFIC_PROJECT_DELTA,
        "universal_refresh_project_change_authority": UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY,
        "classification_authorizes_write": False,
        "other_sector_write_sequence": OTHER_SECTOR_WRITE_SEQUENCE,
        "other_lane_mutation": "USER_NAMED_ONE_TURN_HIL_GRANT",
        "local_github_intake_exclusivity": "MUTUALLY_EXCLUSIVE_ONE_PROJECT_DATABASE",
        "primary_code_mode": primary_code_mode,
        "status": "LIVE_GOVERNED_SECTOR_GRAPH",
        "project_authority_class": "PROJECT_MUTABLE_SECTOR",
        "updated_at": stamp,
    }
    _write_text(
        project / "project_pointer.json",
        json.dumps(project_pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    topology_mmd = "project/topology/project_master_topology.mmd"
    topology_svg = "project/topology/project_master_topology.svg"
    topology_png = "project/topology/project_master_topology.png"
    _write_pointer_file(
        root / ".uepc_env",
        (
            ("UEPC_ENV_VERSION", "V15"),
            ("PACKAGE_CLASS", "EVIDENCE_LANE_ENV_UOP_GOVERNED_PROJECT_V1"),
            ("PACKAGE_USE_MODE", package_use_mode),
            ("ENV_SQLITE", "env/env_sqlite.sqlite"),
            ("ENV_LAW", "env/env_law.md"),
            ("ENV_PROJECT_AUTHORITY_LAW", "env/evidence_lane_project_authority_law.md"),
            ("ENV_MMD", "env/env_mmd.mmd"),
            ("ENV_MMD_DOT", "env/env_mmd.dot"),
            ("ENV_MMD_SVG", "env/env_mmd.svg"),
            ("ENV_MMD_PNG", "env/env_mmd.png"),
            ("ENV_WRITE_LOCK", "true"),
            ("ENV_DEFAULT_OPEN_MODE", "mode=ro&immutable=1"),
            ("PROFILE_POINTER", ".uepc_profile"),
            ("PROJECT_POINTER", ".uepc_project"),
            ("PROJECT_ROUTER", "project/project_router.sqlite"),
            ("PROJECT_AUTHORITY_CLASS", "PROJECT_MUTABLE_SECTOR"),
            ("PROJECT_MUTABLE_SECTOR_ROOT", "project/sectors"),
            ("UNIVERSAL_LANE_POINTER_INDEX", "project/pointers/INDEX.json"),
            ("CANONICAL_LANE_REGISTRY", "project/pointers/CANONICAL_LANE_REGISTRY.json"),
            ("CANONICAL_LANE_REGISTRY_SHA256", canonical_registry_sha256),
            ("LINEAGE_HEAD", LINEAGE_HEAD_RELATIVE),
            ("LINEAGE_HEAD_SHA256", lineage_head["head_sha256"]),
            ("NEXT_EXPECTED_INDEX", next_expected_index),
            ("LAST_ENTRY_RECEIPT", "receipts/last_entry_slip.txt"),
            ("LAST_EXIT_RECEIPT", "receipts/last_exit_slip.txt"),
            ("AUTHORITY_MODEL", "ENV_AND_UOP_LOCKED_PROJECT_MUTABLE_SECTOR"),
            ("PUBLIC_MODEL_PROJECT_ACCESS", PUBLIC_MODEL_PROJECT_ACCESS),
            ("PUBLIC_MODEL_PROJECT_MUTATION", str(PUBLIC_MODEL_PROJECT_MUTATION).lower()),
            ("PROVIDER_SPECIFIC_PROJECT_DELTA", str(PROVIDER_SPECIFIC_PROJECT_DELTA).lower()),
            ("UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY", str(UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY).lower()),
            ("CLASSIFICATION_AUTHORIZES_WRITE", "false"),
            ("OTHER_SECTOR_WRITE_SEQUENCE", OTHER_SECTOR_WRITE_SEQUENCE),
            ("AUTHORITY_DERIVATION_INPUT_SHA256", derivation_input_sha256),
            ("AUTHORITY_PROVENANCE_RECEIPT", "receipts/ENV_UOP_SUPPLIED_AUTHORITY_PROVENANCE.json"),
            ("UPDATED_AT", stamp),
        ),
    )
    _write_pointer_file(
        root / ".uepc_profile",
        (
            ("PROFILE_ID", "UOP_PUBLIC_GOVERNANCE_V15"),
            ("PACKAGE_USE_MODE", package_use_mode),
            ("UOP_SQLITE", "uop/uop_sqlite.sqlite"),
            ("UOP_LAW", "uop/uop_law.md"),
            ("UOP_LIVE_PROJECT_OPERATOR_LAW", "uop/evidence_lane_live_project_operator_law.md"),
            ("UOP_MMD", "uop/uop_mmd.mmd"),
            ("UOP_MMD_DOT", "uop/uop_mmd.dot"),
            ("UOP_MMD_SVG", "uop/uop_mmd.svg"),
            ("UOP_MMD_PNG", "uop/uop_mmd.png"),
            ("UOP_WRITE_LOCK", "true"),
            ("UOP_DEFAULT_OPEN_MODE", "mode=ro&immutable=1"),
            ("CAN_OVERRIDE_ENV", "false"),
            ("CAN_OVERRIDE_PROJECT", "false"),
            ("PROJECT_AUTHORITY_CLASS", "PROJECT_MUTABLE_SECTOR"),
            ("PROJECT_MUTATION_POLICY", "TWO_AUTOMATIC_APPEND_LANES_OTHERWISE_NAMED_ONE_TURN_HIL"),
            ("PUBLIC_MODEL_PROJECT_ACCESS", PUBLIC_MODEL_PROJECT_ACCESS),
            ("PUBLIC_MODEL_PROJECT_MUTATION", str(PUBLIC_MODEL_PROJECT_MUTATION).lower()),
            ("PROVIDER_SPECIFIC_PROJECT_DELTA", str(PROVIDER_SPECIFIC_PROJECT_DELTA).lower()),
            ("UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY", str(UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY).lower()),
            ("CLASSIFICATION_AUTHORIZES_WRITE", "false"),
            ("OTHER_SECTOR_WRITE_SEQUENCE", OTHER_SECTOR_WRITE_SEQUENCE),
            ("CANONICAL_LANE_REGISTRY", "project/pointers/CANONICAL_LANE_REGISTRY.json"),
            ("CANONICAL_LANE_REGISTRY_SHA256", canonical_registry_sha256),
            ("LINEAGE_HEAD", LINEAGE_HEAD_RELATIVE),
            ("LINEAGE_HEAD_SHA256", lineage_head["head_sha256"]),
            ("NEXT_EXPECTED_INDEX", next_expected_index),
            ("AUTHORITY_DERIVATION_INPUT_SHA256", derivation_input_sha256),
            ("AUTHORITY_PROVENANCE_RECEIPT", "receipts/ENV_UOP_SUPPLIED_AUTHORITY_PROVENANCE.json"),
            ("UPDATED_AT", stamp),
        ),
    )
    _write_pointer_file(
        root / ".uepc_project",
        (
            ("PROJECT_ID", brain_name or root.name),
            ("PACKAGE_USE_MODE", package_use_mode),
            ("PROJECT_STATE", "LIVE_GOVERNED_SECTOR_GRAPH"),
            ("PROJECT_AUTHORITY_CLASS", "PROJECT_MUTABLE_SECTOR"),
            ("PROJECT_MUTABLE_SECTOR_ROOT", "project/sectors"),
            ("PROJECT_ROUTER_SQLITE", "project/project_router.sqlite"),
            ("PROJECT_ROUTER_SHA256", router_sha256),
            ("PROJECT_POINTER_INDEX", "project/pointers/INDEX.json"),
            ("CANONICAL_LANE_REGISTRY", "project/pointers/CANONICAL_LANE_REGISTRY.json"),
            ("CANONICAL_LANE_REGISTRY_SHA256", canonical_registry_sha256),
            ("LINEAGE_HEAD", LINEAGE_HEAD_RELATIVE),
            ("LINEAGE_HEAD_SHA256", lineage_head["head_sha256"]),
            ("NEXT_EXPECTED_INDEX", next_expected_index),
            ("PROJECT_TOPOLOGY_MMD", topology_mmd),
            ("PROJECT_TOPOLOGY_SVG", topology_svg),
            ("PROJECT_TOPOLOGY_PNG", topology_png),
            ("PROJECT_SECTOR_COUNT", physical_sector_count),
            ("PROJECT_LOGICAL_LANE_COUNT", len(lane_pointers)),
            ("ENV_UOP_AUTHORITY", "READ_ONLY"),
            ("AUTOMATIC_APPEND_LANES", "chat_lineage,research"),
            ("OUTPUT_RESOURCE_RECORD_ROUTE", "GOVERNED_CHAT_LINEAGE_RESEARCH_APPEND_ONLY"),
            ("MODEL_OUTPUT_PROJECT_TRUTH", "false"),
            ("PUBLIC_MODEL_PROJECT_ACCESS", PUBLIC_MODEL_PROJECT_ACCESS),
            ("PUBLIC_MODEL_PROJECT_MUTATION", str(PUBLIC_MODEL_PROJECT_MUTATION).lower()),
            ("PROVIDER_SPECIFIC_PROJECT_DELTA", str(PROVIDER_SPECIFIC_PROJECT_DELTA).lower()),
            ("UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY", str(UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY).lower()),
            ("CLASSIFICATION_AUTHORIZES_WRITE", "false"),
            ("OTHER_SECTOR_WRITE_SEQUENCE", OTHER_SECTOR_WRITE_SEQUENCE),
            ("OTHER_SECTORS", "READ_ONLY_UNLESS_EXPLICIT_NAMED_ONE_TURN_HIL_GRANT"),
            ("LOCAL_GITHUB_INTAKE", "MUTUALLY_EXCLUSIVE_ONE_PROJECT_DATABASE"),
            ("PRIMARY_CODE_MODE", primary_code_mode or "NONE"),
            ("RELOCK_AFTER_MUTATION", "true"),
            ("AUTHORITY_DERIVATION_INPUT_SHA256", derivation_input_sha256),
            ("AUTHORITY_PROVENANCE_RECEIPT", "receipts/ENV_UOP_SUPPLIED_AUTHORITY_PROVENANCE.json"),
            ("UPDATED_AT", stamp),
        ),
    )
    from sqlite_brain_builder.runtime.env15_locked_read import (
        write_supplied_env_uop_provenance_receipt,
    )

    provenance = write_supplied_env_uop_provenance_receipt(root)
    return {
        "contract": index_payload["contract"],
        "status": "CURRENT_UNTRUSTED_CANDIDATE",
        "acceptance_state": "UNTRUSTED_CANDIDATE",
        "pointer_index": str(pointer_root / "INDEX.json"),
        "lineage_head": str(lineage_path),
        "lineage_head_sha256": lineage_head["head_sha256"],
        "next_expected_index": next_expected_index,
        "logical_lane_count": len(lane_pointers),
        "physical_sector_count": physical_sector_count,
        "active_lane_ids": sorted(active),
        "primary_code_mode": primary_code_mode,
        "canonical_lane_registry": str(canonical_registry_path),
        "canonical_lane_registry_sha256": canonical_registry_sha256,
        "derivation_input_sha256": derivation_input_sha256,
        "supplied_env_uop_provenance": provenance,
    }


def _pointer_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def validate_universal_lane_authority(brain_root: str | Path) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    errors: list[str] = []
    lineage_path = root / LINEAGE_HEAD_RELATIVE
    try:
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "contract": LINEAGE_HEAD_CONTRACT,
            "status": "FAIL",
            "errors": [f"LINEAGE_HEAD_READ_FAILED:{type(exc).__name__}"],
        }
    payload = {key: value for key, value in lineage.items() if key != "head_sha256"}
    expected_head_sha256 = _canonical_sha256(payload)
    observed_head_sha256 = str(lineage.get("head_sha256") or "")
    if lineage.get("contract") != LINEAGE_HEAD_CONTRACT:
        errors.append("LINEAGE_HEAD_CONTRACT_INVALID")
    if observed_head_sha256 != expected_head_sha256:
        errors.append("LINEAGE_HEAD_HASH_INVALID")
    if lineage.get("acceptance_state") != "UNTRUSTED_CANDIDATE":
        errors.append("LINEAGE_HEAD_CANDIDATE_STATE_INVALID")
    if lineage.get("promotion_authority") != "EXPLICIT_HUMAN_HIL_ONLY":
        errors.append("LINEAGE_HEAD_HIL_BOUNDARY_INVALID")
    active_code_modes = sorted(
        PRIMARY_CODE_LANES.intersection(lineage.get("active_lane_ids") or [])
    )
    if len(active_code_modes) > 1:
        errors.append("LINEAGE_HEAD_PRIMARY_CODE_MODE_CONFLICT")
    if lineage.get("primary_code_mode") != (
        active_code_modes[0] if active_code_modes else None
    ):
        errors.append("LINEAGE_HEAD_PRIMARY_CODE_MODE_INVALID")

    registry_relative = str(lineage.get("canonical_lane_registry") or "")
    registry_path = root / Path(registry_relative.replace("/", os.sep))
    if not registry_path.is_file():
        errors.append("CANONICAL_LANE_REGISTRY_MISSING")
    elif _sha256_file(registry_path) != str(
        lineage.get("canonical_lane_registry_sha256") or ""
    ):
        errors.append("CANONICAL_LANE_REGISTRY_HASH_MISMATCH")

    vector = {
        str(row.get("lane_id")): row
        for row in lineage.get("lane_state_vector") or []
        if isinstance(row, dict) and row.get("lane_id")
    }
    if set(vector) != set(CANONICAL_LANE_IDS):
        errors.append("LINEAGE_HEAD_LANE_SET_INVALID")
    for lane_id in CANONICAL_LANE_IDS:
        row = vector.get(lane_id)
        pointer_path = root / "project" / "pointers" / f"{lane_id}_pointer.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"LINEAGE_LANE_POINTER_READ_FAILED:{lane_id}:{type(exc).__name__}")
            continue
        if pointer.get("lineage_head") != LINEAGE_HEAD_RELATIVE:
            errors.append(f"LINEAGE_LANE_POINTER_HEAD_PATH_INVALID:{lane_id}")
        if pointer.get("lineage_head_sha256") != observed_head_sha256:
            errors.append(f"LINEAGE_LANE_POINTER_HEAD_HASH_INVALID:{lane_id}")
        if row is None:
            continue
        for key in (
            "physical_sector_id",
            "database",
            "database_sha256",
            "database_size_bytes",
            "registered_source_count",
            "content_row_count",
            "registered_this_build",
            "lane_classification",
            "source_intake_state",
            "classification_evidence",
            "preservation_state",
            "payload_state",
            "ingestion_status",
            "automatic_append",
            "append_service_state",
            "access",
        ):
            if pointer.get(key) != row.get(key):
                errors.append(f"LINEAGE_LANE_POINTER_STATE_MISMATCH:{lane_id}:{key}")
        if pointer.get("lane_classification") not in LANE_CLASSIFICATIONS:
            errors.append(f"LINEAGE_LANE_CLASSIFICATION_INVALID:{lane_id}")
        relative = str(pointer.get("database") or "").replace("\\", "/")
        database = (root / Path(relative.replace("/", os.sep))).resolve()
        project = (root / "project").resolve()
        if not database.is_file() or project not in database.parents:
            errors.append(f"LINEAGE_LANE_DATABASE_INVALID:{lane_id}")
            continue
        if _sha256_file(database) != str(pointer.get("database_sha256") or ""):
            errors.append(f"LINEAGE_LANE_DATABASE_HASH_MISMATCH:{lane_id}")
        if database.stat().st_size != int(pointer.get("database_size_bytes") or -1):
            errors.append(f"LINEAGE_LANE_DATABASE_SIZE_MISMATCH:{lane_id}")

    for pointer_name in (".uepc_env", ".uepc_profile", ".uepc_project"):
        values = _pointer_values(root / pointer_name)
        if values.get("LINEAGE_HEAD") != LINEAGE_HEAD_RELATIVE:
            errors.append(f"LINEAGE_DOT_POINTER_HEAD_PATH_INVALID:{pointer_name}")
        if values.get("LINEAGE_HEAD_SHA256") != observed_head_sha256:
            errors.append(f"LINEAGE_DOT_POINTER_HEAD_HASH_INVALID:{pointer_name}")
        if values.get("NEXT_EXPECTED_INDEX") != lineage.get("next_expected_index"):
            errors.append(f"LINEAGE_DOT_POINTER_NEXT_INDEX_INVALID:{pointer_name}")
        if values.get("CANONICAL_LANE_REGISTRY_SHA256") != lineage.get(
            "canonical_lane_registry_sha256"
        ):
            errors.append(f"LINEAGE_DOT_POINTER_REGISTRY_HASH_INVALID:{pointer_name}")

    for slip_name in ("last_entry_slip.txt", "last_exit_slip.txt"):
        slip = root / "receipts" / slip_name
        try:
            text = slip.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"LINEAGE_SLIP_READ_FAILED:{slip_name}:{type(exc).__name__}")
            continue
        if f"LINEAGE_HEAD_SHA256={observed_head_sha256}" not in text:
            errors.append(f"LINEAGE_SLIP_HEAD_HASH_INVALID:{slip_name}")
        if f"NEXT_EXPECTED_INDEX={lineage.get('next_expected_index')}" not in text:
            errors.append(f"LINEAGE_SLIP_NEXT_INDEX_INVALID:{slip_name}")

    return {
        "contract": LINEAGE_HEAD_CONTRACT,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "lineage_head": str(lineage_path),
        "lineage_head_sha256": observed_head_sha256,
        "next_expected_index": lineage.get("next_expected_index"),
        "acceptance_state": lineage.get("acceptance_state"),
        "logical_lane_count": len(vector),
    }


def prune_provider_project_lock_artifacts(package_root: str | Path) -> dict[str, Any]:
    """Remove the retired third locked container from a provider package stage."""

    root = Path(package_root).resolve()
    removed: list[str] = []
    for relative in PROJECT_LOCK_ARTIFACT_PATHS:
        path = root / Path(relative.replace("/", os.sep))
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            _set_writable(path)
            path.unlink()
        removed.append(relative)
    for relative in (
        "manifests/PUBLIC_SECTION_LOCK_REGISTRY.json",
        "project/PROJECT_SECTOR_REGISTRY.json",
    ):
        path = root / Path(relative.replace("/", os.sep))
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").casefold()
            if any(term in text for term in FORBIDDEN_PROVIDER_PROJECT_LOCK_TERMS):
                _set_writable(path)
                path.unlink()
                removed.append(relative)
    manifests = root / "manifests"
    if manifests.is_dir():
        for path in sorted(manifests.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in {".json", ".txt", ".md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").casefold()
            if (
                "project/project_template.sqlite" in text
                or "project/project_topology_template" in text
                or "project_template_locked" in text
                or "public_env_uop_project_locked" in text
            ):
                relative = path.relative_to(root).as_posix()
                _set_writable(path)
                path.unlink()
                removed.append(relative)
    return {
        "contract": "EVIDENCE_LANE_PROVIDER_NO_THIRD_LOCKED_CONTAINER_V1",
        "status": "PASS",
        "removed": sorted(removed),
    }


def find_forbidden_provider_project_lock_references(package_root: str | Path) -> list[str]:
    root = Path(package_root).resolve()
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        lower_path = relative.casefold()
        if any(term in lower_path for term in FORBIDDEN_PROVIDER_PROJECT_LOCK_TERMS):
            findings.append(relative)
            continue
        if path.suffix.casefold() not in {".txt", ".md", ".json", ".yaml", ".yml", ".uepc_env", ".uepc_profile", ".uepc_project"} and not path.name.startswith(".uepc"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace").casefold()
        if any(term in text for term in FORBIDDEN_PROVIDER_PROJECT_LOCK_TERMS):
            findings.append(relative)
    return sorted(set(findings))


__all__ = [
    "AUTOMATIC_APPEND_LANES",
    "OTHER_SECTOR_WRITE_SEQUENCE",
    "PROVIDER_SPECIFIC_PROJECT_DELTA",
    "PUBLIC_MODEL_PROJECT_ACCESS",
    "PUBLIC_MODEL_PROJECT_MUTATION",
    "UNIVERSAL_REFRESH_PROJECT_CHANGE_AUTHORITY",
    "LANE_CLASSIFICATIONS",
    "LINEAGE_HEAD_CONTRACT",
    "LINEAGE_HEAD_RELATIVE",
    "find_forbidden_provider_project_lock_references",
    "prune_provider_project_lock_artifacts",
    "validate_universal_lane_authority",
    "write_universal_lane_authority",
]
