from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

from sqlite_brain_builder.runtime.env15_resource import EXPECTED_SECTORS, REQUIRED_MMD_ASSETS
from sqlite_brain_builder.runtime.env15_project_schema import (
    ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS,
    ENV15_RUNTIME_EXTENSION_SECTORS,
)
from sqlite_brain_builder.runtime.env15_locked_read import (
    LOCKED_READ_ARCHIVE_SHA256,
    SUPPLIED_CODEX_SUPPORT_MEMBERS,
    SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT,
    supplied_env_uop_database_rows,
)
from sqlite_brain_builder.runtime.provider_package_projection import (
    CHATGPT_PACKAGE_MAX_BYTES,
    GEMINI_PACKAGE_MAX_BYTES,
    PROJECT_MANIFEST_CONTRACT,
)
from sqlite_brain_builder.runtime.provider_package_compatibility import (
    audit_provider_package,
)
from sqlite_brain_builder.runtime.package_root_authority import (
    validate_zip_package_root_authority,
)
from sqlite_brain_builder.runtime.provider_readable_corpus import (
    CORPUS_BEGIN,
    CORPUS_END,
    GEMINI_READABLE_CONTRACT,
    GEMINI_READABLE_NAMES,
    READABILITY_CONTRACT,
)


T021_GEMINI_EXACT10_NAMES = (
    "GEMINI_FLASH_PROMPT.txt",
    ".uepc_env",
    ".uepc_profile",
    ".uepc_project",
    ".uepc_project_write",
    "ENV_PUBLIC_READONLY.sqlite",
    "UOP_PUBLIC_READONLY.sqlite",
    "PROJECT_CONJOINED_WRITE.sqlite",
    "ENV_MMD_RENDER.png",
    "UOP_MMD_RENDER.png",
)

# Env15 remains the immutable 14-sector package law.  The live project may add
# only explicitly governed mutation sectors.  Those additions are carried in
# PROJECT_CONJOINED_WRITE.sqlite without changing the base Env15 resource.
ALLOWED_RUNTIME_PROJECT_SECTORS = frozenset(ENV15_RUNTIME_EXTENSION_SECTORS)
CANONICAL_GEMINI_PROJECT_SECTOR_LAYOUTS = tuple(
    tuple(sorted(layout)) for layout in ENV15_ALLOWED_LIVE_SECTOR_LAYOUTS
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_member_sha256(archive: zipfile.ZipFile, member_name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(member_name, "r") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_zip_member(archive: zipfile.ZipFile, member_name: str, destination: Path) -> None:
    with archive.open(member_name, "r") as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target, length=8 * 1024 * 1024)


def _sqlite_path_validation(database: Path, member_name: str) -> dict:
    result = {
        "member": member_name,
        "integrity_check": [],
        "foreign_key_violations": [],
        "error": None,
    }
    with database.open("rb") as stream:
        header = stream.read(16)
    if header != b"SQLite format 3\x00":
        result["error"] = "INVALID_SQLITE_HEADER"
        return result
    connection = None
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        result["integrity_check"] = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        result["foreign_key_violations"] = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    except Exception as exc:  # pragma: no cover - defensive path
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()
    return result


def _sqlite_validation(data: bytes, member_name: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="evidenceos_package_sqlite_") as tmp:
        database = Path(tmp) / "member.sqlite"
        database.write_bytes(data)
        return _sqlite_path_validation(database, member_name)


def _zip_inventory(path: str | Path) -> tuple[zipfile.ZipFile, list[str]]:
    archive = zipfile.ZipFile(Path(path), "r")
    names = archive.namelist()
    return archive, names


def _nested_zip_members(archive: zipfile.ZipFile, names: Iterable[str]) -> list[str]:
    nested = []
    for name in names:
        if name.endswith("/"):
            continue
        with archive.open(name) as stream:
            header = stream.read(4)
        if name.lower().endswith(".zip") or header.startswith(b"PK\x03\x04"):
            nested.append(name)
    return nested


def _sqlite_members(
    archive: zipfile.ZipFile,
    names: Iterable[str],
    *,
    project_contract_member: str | None = None,
) -> list[dict]:
    results = []
    with tempfile.TemporaryDirectory(prefix="evidenceos_package_sqlites_") as tmp:
        root = Path(tmp)
        for index, name in enumerate(names):
            if name.endswith("/"):
                continue
            with archive.open(name) as stream:
                header = stream.read(16)
            if not (
                name.lower().endswith((".sqlite", ".sqlite3", ".db"))
                or header == b"SQLite format 3\x00"
            ):
                continue
            database = root / f"member_{index:05d}.sqlite"
            _copy_zip_member(archive, name, database)
            validation = _sqlite_path_validation(database, name)
            if name == project_contract_member and validation["error"] is None:
                validation["project_contract"] = _project_conjoined_validation(database)
            results.append(validation)
            database.unlink()
    return results


def _pointer_values(data: bytes) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in data.decode("utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _project_conjoined_validation(database: Path) -> dict:
    required_tables = {
        "canonical_content_blob",
        "embedded_database_blob",
        "embedded_database_blob_chunk",
        "source_schema_object_registry",
        "source_row_count_registry",
        "gemini_project_write_router",
        "gemini_mutation_grant",
        "gemini_project_write_log",
        "gemini_project_head",
    }
    result = {
        "required_tables": sorted(required_tables),
        "missing_tables": [],
        "source_database_count": 0,
        "project_core_source_database_count": 0,
        "sector_source_database_count": 0,
        "sector_router_count": 0,
        "sector_ids": [],
        "chat_lineage_access": None,
        "research_access": None,
        "automatic_append_lane_ids": [],
        "project_core_projection_count": 0,
        "sector_projection_count": 0,
        "source_byte_reconstruction_count": 0,
        "source_logical_projection_count": 0,
        "chunked_source_database_count": 0,
        "embedded_chunk_count": 0,
        "canonical_content_blob_count": 0,
        "reconstruction_failures": [],
        "error": None,
    }
    connection = None
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
                )
            }
            missing = sorted(required_tables - tables)
            result["missing_tables"] = missing
            if missing:
                return result
            projection_objects = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type IN ('table','view') ORDER BY name"
                )
            }
            result["canonical_content_blob_count"] = connection.execute(
                "SELECT COUNT(*) FROM canonical_content_blob"
            ).fetchone()[0]
            result["source_database_count"] = connection.execute(
                "SELECT COUNT(*) FROM embedded_database_blob"
            ).fetchone()[0]
            result["project_core_source_database_count"] = connection.execute(
                "SELECT COUNT(*) FROM embedded_database_blob WHERE sector_id='project_core'"
            ).fetchone()[0]
            result["sector_source_database_count"] = connection.execute(
                "SELECT COUNT(*) FROM embedded_database_blob WHERE sector_id<>'project_core'"
            ).fetchone()[0]
            sector_rows = connection.execute(
                "SELECT sector_id,access_mode,automatic_write,explicit_one_turn_grant_required "
                "FROM gemini_project_write_router ORDER BY sector_id"
            ).fetchall()
            result["sector_router_count"] = len(sector_rows)
            result["sector_ids"] = [row[0] for row in sector_rows]
            result["chat_lineage_access"] = next(
                (row[1] for row in sector_rows if row[0] == "chat_lineage"),
                None,
            )
            result["research_access"] = next(
                (row[1] for row in sector_rows if row[0] == "research"),
                None,
            )
            result["automatic_append_lane_ids"] = [
                str(row[0]) for row in sector_rows if int(row[2]) == 1 and int(row[3]) == 0
            ]
            result["project_core_projection_count"] = sum(
                name.startswith("project_core__") for name in projection_objects
            )
            result["sector_projection_count"] = sum(
                name.startswith("sector_") for name in projection_objects
            )
            source_rows = connection.execute(
                "SELECT source_database_id,source_sha256,source_byte_size,"
                "content_storage,content_chunk_count,content "
                "FROM embedded_database_blob ORDER BY source_database_id"
            )
            for (
                source_database_id,
                expected_sha256,
                expected_byte_size,
                content_storage,
                expected_chunk_count,
                inline_content,
            ) in source_rows:
                failure: str | None = None
                digest = hashlib.sha256()
                observed_byte_size = 0
                if content_storage == "INLINE_BLOB":
                    if inline_content is None or expected_chunk_count != 0:
                        failure = f"{source_database_id}:INLINE_METADATA"
                    else:
                        digest.update(inline_content)
                        observed_byte_size = len(inline_content)
                elif content_storage == "CHUNKED_BLOB_V1":
                    result["chunked_source_database_count"] += 1
                    if inline_content is not None or expected_chunk_count <= 0:
                        failure = f"{source_database_id}:CHUNKED_METADATA"
                    else:
                        observed_chunk_count = 0
                        expected_offset = 0
                        for (
                            chunk_index,
                            chunk_offset,
                            chunk_byte_size,
                            chunk_sha256,
                            chunk_content,
                        ) in connection.execute(
                            "SELECT chunk_index,chunk_offset,chunk_byte_size,"
                            "chunk_sha256,content FROM embedded_database_blob_chunk "
                            "WHERE source_database_id=? ORDER BY chunk_index",
                            (source_database_id,),
                        ):
                            if chunk_index != observed_chunk_count:
                                failure = f"{source_database_id}:CHUNK_INDEX"
                                break
                            if chunk_offset != expected_offset:
                                failure = f"{source_database_id}:CHUNK_OFFSET"
                                break
                            if len(chunk_content) != chunk_byte_size:
                                failure = f"{source_database_id}:CHUNK_SIZE"
                                break
                            if _sha256(chunk_content) != chunk_sha256:
                                failure = f"{source_database_id}:CHUNK_HASH"
                                break
                            digest.update(chunk_content)
                            observed_byte_size += chunk_byte_size
                            expected_offset += chunk_byte_size
                            observed_chunk_count += 1
                        result["embedded_chunk_count"] += observed_chunk_count
                        if failure is None and observed_chunk_count != expected_chunk_count:
                            failure = f"{source_database_id}:CHUNK_COUNT"
                elif content_storage == "LOGICAL_PROJECTION_V2":
                    if inline_content is not None or expected_chunk_count != 0:
                        failure = f"{source_database_id}:LOGICAL_METADATA"
                    elif len(str(expected_sha256)) != 64 or int(expected_byte_size) <= 0:
                        failure = f"{source_database_id}:LOGICAL_SOURCE_IDENTITY"
                    else:
                        schema_count = connection.execute(
                            "SELECT COUNT(*) FROM source_schema_object_registry "
                            "WHERE source_database_id=?",
                            (source_database_id,),
                        ).fetchone()[0]
                        row_registry_count = connection.execute(
                            "SELECT COUNT(*) FROM source_row_count_registry "
                            "WHERE source_database_id=?",
                            (source_database_id,),
                        ).fetchone()[0]
                        if schema_count <= 0 or row_registry_count <= 0:
                            failure = f"{source_database_id}:LOGICAL_REGISTRY_COVERAGE"
                        else:
                            result["source_logical_projection_count"] += 1
                            continue
                else:
                    failure = f"{source_database_id}:STORAGE_MODE"
                if failure is None and observed_byte_size != expected_byte_size:
                    failure = f"{source_database_id}:TOTAL_SIZE"
                if failure is None and digest.hexdigest() != expected_sha256:
                    failure = f"{source_database_id}:TOTAL_HASH"
                if failure is None:
                    result["source_byte_reconstruction_count"] += 1
                else:
                    result["reconstruction_failures"].append(failure)
        except Exception as exc:  # pragma: no cover - defensive path
            result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if connection is not None:
            connection.close()
    return result


def _unsafe_or_stale_member_path(value: object) -> bool:
    member = str(value or "").strip().replace("\\", "/")
    if not member or member.startswith("/") or ":" in member.split("/", 1)[0]:
        return True
    return ".." in member.split("/") or member.casefold().startswith(("packages/", "brain_snapshots/"))


def validate_chatgpt_package(path: str | Path) -> dict:
    package = Path(path)
    errors: list[str] = []
    package_byte_size = package.stat().st_size
    if package_byte_size > CHATGPT_PACKAGE_MAX_BYTES:
        errors.append(
            f"CHATGPT_PACKAGE_SIZE_LIMIT_EXCEEDED:{package_byte_size}>{CHATGPT_PACKAGE_MAX_BYTES}"
        )
    with zipfile.ZipFile(package, "r") as archive:
        names = archive.namelist()
        bad_crc = archive.testzip()
        if bad_crc:
            errors.append(f"ZIP_CRC_FAILED:{bad_crc}")
        required_files = {".uepc_env", ".uepc_profile", ".uepc_project"}
        for name in sorted(required_files - set(names)):
            errors.append(f"MISSING_POINTER:{name}")
        for prefix in ("env/", "uop/", "project/", "manifests/", "receipts/", "recovery/"):
            if not any(name.startswith(prefix) and not name.endswith("/") for name in names):
                errors.append(f"MISSING_PACKAGE_AREA:{prefix}")

        nested = _nested_zip_members(archive, names)
        if nested:
            errors.extend(f"NESTED_ZIP_FORBIDDEN:{name}" for name in nested)
        codex_members = [name for name in names if name.casefold().startswith("codex/")]
        if codex_members:
            errors.extend(f"CODEX_SUPPORT_LEAKAGE:{name}" for name in codex_members)

        database_authority_status = "PASS"
        for member, expected_size, expected_hash in supplied_env_uop_database_rows():
            if member not in names:
                database_authority_status = "FAIL"
                errors.append(f"SUPPLIED_ENV_UOP_DATABASE_MISSING:{member}")
                continue
            info = archive.getinfo(member)
            observed_hash = _zip_member_sha256(archive, member).upper()
            if info.file_size != expected_size:
                database_authority_status = "FAIL"
                errors.append(f"SUPPLIED_ENV_UOP_DATABASE_SIZE_MISMATCH:{member}")
            if observed_hash != expected_hash:
                database_authority_status = "FAIL"
                errors.append(f"SUPPLIED_ENV_UOP_DATABASE_HASH_MISMATCH:{member}")

        provenance_status = "FAIL"
        if SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT not in names:
            errors.append("SUPPLIED_ENV_UOP_PROVENANCE_MISSING")
        else:
            try:
                provenance = json.loads(
                    archive.read(SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT).decode("utf-8")
                )
                if (
                    provenance.get("contract")
                    != "EVIDENCE_LANE_SUPPLIED_ENV_UOP_AUTHORITY_PROVENANCE_V1"
                    or provenance.get("status") != "PASS"
                    or provenance.get("source_archive_sha256")
                    != LOCKED_READ_ARCHIVE_SHA256
                    or provenance.get("source_archive_mounted_or_nested") is not False
                    or provenance.get("project_template_authority_imported") is not False
                    or provenance.get("codex_support_imported_into_brain_or_provider_base")
                    is not False
                ):
                    errors.append("SUPPLIED_ENV_UOP_PROVENANCE_INVALID")
                else:
                    provenance_status = "PASS"
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
                errors.append(
                    "SUPPLIED_ENV_UOP_PROVENANCE_INVALID:" + type(exc).__name__
                )

        third_locked_container_status = "PASS"
        retired_members = [
            name
            for name in names
            if name.casefold().startswith(("public_read/", "project_template_locked/"))
            or name.casefold() in {
                "project/project_template.sqlite",
                "project/project_topology_template.mmd",
                "project/project_topology_template.svg",
                "project/project_topology_template.png",
            }
            or "public_env_uop_project_locked" in name.casefold()
            or "project_locked" in name.casefold()
        ]
        if retired_members:
            third_locked_container_status = "FAIL"
            errors.extend(f"RETIRED_THIRD_LOCKED_CONTAINER_MEMBER:{name}" for name in retired_members)
        for pointer_name in required_files:
            if pointer_name not in names:
                continue
            pointer_text = archive.read(pointer_name).decode("utf-8", errors="replace").casefold()
            if any(
                term in pointer_text
                for term in (
                    "project_locked",
                    "project_template_locked",
                    "public_env_uop_project_locked",
                    "project_template_sqlite=",
                )
            ):
                third_locked_container_status = "FAIL"
                errors.append(f"RETIRED_THIRD_LOCKED_CONTAINER_POINTER:{pointer_name}")

        expected_sector_paths = {
            f"project/sectors/{sector}/{sector}_sector_v001.sqlite" for sector in EXPECTED_SECTORS
        }
        for name in sorted(expected_sector_paths - set(names)):
            errors.append(f"MISSING_PROJECT_SECTOR:{name}")

        required_topology = {
            *(path for path in REQUIRED_MMD_ASSETS if not path.startswith("project/project_topology_template.")),
            "project/topology/project_master_topology.mmd",
            "project/topology/project_master_topology.svg",
            "project/topology/project_master_topology.png",
            "env/env_law.md",
            "env/env_mmd.dot",
            "uop/uop_law.md",
            "uop/uop_mmd.dot",
            SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT,
        }
        for name in sorted(required_topology - set(names)):
            errors.append(f"MISSING_TOPOLOGY_FILE:{name}")

        recovery_prompts = [
            name for name in names
            if name.startswith("recovery/") and not name.endswith("/") and name.lower().endswith((".md", ".txt"))
        ]
        if not recovery_prompts or any(archive.getinfo(name).file_size <= 0 for name in recovery_prompts):
            errors.append("MISSING_OR_EMPTY_RECOVERY_PROMPT")

        sqlite_results = _sqlite_members(archive, names)
        for result in sqlite_results:
            if result["error"] or result["integrity_check"] != ["ok"] or result["foreign_key_violations"]:
                errors.append(f"SQLITE_VALIDATION_FAILED:{result['member']}")

        manifest_status = "NOT_PRESENT"
        manifest_name = "manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json"
        if manifest_name in names:
            manifest_status = "PASS"
            try:
                manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
                rows = manifest if isinstance(manifest, list) else manifest.get("files", [])
                if not isinstance(manifest, dict):
                    errors.append("MANIFEST_SCOPE_UNDECLARED")
                    manifest_status = "FAIL"
                elif manifest.get("contract") != PROJECT_MANIFEST_CONTRACT:
                    errors.append("MANIFEST_CONTRACT_INVALID")
                    manifest_status = "FAIL"
                elif manifest.get("manifest_scope") != "PROJECT_SNAPSHOT_NOT_CURRENT_OUTER_ROOT":
                    errors.append("MANIFEST_SCOPE_INVALID")
                    manifest_status = "FAIL"
                if not isinstance(rows, list) or not rows:
                    errors.append("MANIFEST_FILES_EMPTY")
                    manifest_status = "FAIL"
                for row in rows:
                    member = row.get("path")
                    if _unsafe_or_stale_member_path(member):
                        errors.append(f"STALE_OR_UNSAFE_PACKAGE_PATH:{member}")
                        manifest_status = "FAIL"
                        continue
                    if not member or member not in names:
                        errors.append(f"MANIFEST_MEMBER_MISSING:{member}")
                        manifest_status = "FAIL"
                        continue
                    info = archive.getinfo(member)
                    if row.get("size") is not None and int(row["size"]) != info.file_size:
                        errors.append(f"MANIFEST_SIZE_MISMATCH:{member}")
                        manifest_status = "FAIL"
                    if row.get("sha256") and row["sha256"].lower() != _zip_member_sha256(archive, member):
                        errors.append(f"MANIFEST_HASH_MISMATCH:{member}")
                        manifest_status = "FAIL"
            except Exception as exc:
                errors.append(f"MANIFEST_PARSE_FAILED:{type(exc).__name__}")
                manifest_status = "FAIL"
        else:
            errors.append(f"MISSING_MANIFEST:{manifest_name}")

        provider_readability = {
            "status": "LEGACY_NOT_PRESENT",
            "contract": None,
            "file_count": 0,
        }
        readable_manifest_name = "provider_readable/PROJECT_FILE_INDEX.json"
        readable_guide_name = "provider_readable/OPEN_ME_FIRST_PROVIDER_READABLE.md"
        if readable_manifest_name in names or readable_guide_name in names:
            provider_readability = {
                "status": "PASS",
                "contract": None,
                "file_count": 0,
            }
            try:
                readable_manifest = json.loads(
                    archive.read(readable_manifest_name).decode("utf-8")
                )
                provider_readability["contract"] = readable_manifest.get("contract")
                provider_readability["file_count"] = int(
                    readable_manifest.get("file_count") or 0
                )
                if readable_manifest.get("contract") != READABILITY_CONTRACT:
                    raise ValueError("READABILITY_CONTRACT_MISMATCH")
                if readable_guide_name not in names:
                    raise ValueError("READABILITY_GUIDE_MISSING")
                for row in readable_manifest.get("files") or []:
                    member = str(row.get("package_path") or "")
                    if not member.startswith("source/"):
                        member = "provider_readable/" + member
                    else:
                        member = "provider_readable/" + member
                    if member not in names:
                        raise ValueError(f"READABILITY_MEMBER_MISSING:{member}")
                    if row.get("sha256") != _zip_member_sha256(archive, member):
                        raise ValueError(f"READABILITY_HASH_MISMATCH:{member}")
            except Exception as exc:
                provider_readability["status"] = "FAIL"
                errors.append(f"PROVIDER_READABILITY_VALIDATION_FAILED:{type(exc).__name__}:{exc}")

    root_authority = validate_zip_package_root_authority(package)
    if root_authority.get("status") != "PASS":
        errors.extend(
            "ROOT_AUTHORITY_INVALID:" + str(error)
            for error in root_authority.get("errors") or []
        )
    return {
        "package": str(package),
        "package_byte_size": package_byte_size,
        "provider_size_limit_bytes": CHATGPT_PACKAGE_MAX_BYTES,
        "provider_size_status": "PASS" if package_byte_size <= CHATGPT_PACKAGE_MAX_BYTES else "FAIL",
        "sha256": _sha256_file(package),
        "root_entries": sorted({name.split("/", 1)[0] for name in names}),
        "member_count": len(names),
        "zip_crc": "PASS" if bad_crc is None else "FAIL",
        "sqlite_results": sqlite_results,
        "manifest_status": manifest_status,
        "locked_read_only_authorities": ["env", "uop"],
        "supplied_source_archive_sha256": LOCKED_READ_ARCHIVE_SHA256,
        "env_uop_database_authority_status": database_authority_status,
        "env_uop_provenance_status": provenance_status,
        "nested_zip_count": len(nested),
        "codex_support_leakage_count": len(codex_members),
        "codex_support_member_names": list(SUPPLIED_CODEX_SUPPORT_MEMBERS),
        "third_locked_container_status": third_locked_container_status,
        "provider_readability": provider_readability,
        "root_authority": root_authority,
        "project_sector_count": len(EXPECTED_SECTORS),
        "topology_files_verified": len(required_topology),
        "recovery_prompts": recovery_prompts,
        "archive_reopened": True,
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }


def _validate_gemini_provider_readable(package: Path) -> dict:
    errors: list[str] = []
    compatibility = audit_provider_package(package, "GEMINI")
    if compatibility.get("status") != "PASS":
        errors.extend(
            "CONSUMER_COMPATIBILITY:" + error
            for error in compatibility.get("errors") or []
        )
    package_byte_size = package.stat().st_size
    if package_byte_size > GEMINI_PACKAGE_MAX_BYTES:
        errors.append(
            f"GEMINI_PACKAGE_SIZE_LIMIT_EXCEEDED:{package_byte_size}>{GEMINI_PACKAGE_MAX_BYTES}"
        )
    decoded: dict[str, str] = {}
    bad_crc = None
    names: list[str] = []
    manifest: dict = {}
    with zipfile.ZipFile(package, "r") as archive:
        names = archive.namelist()
        bad_crc = archive.testzip()
        if bad_crc:
            errors.append(f"ZIP_CRC_FAILED:{bad_crc}")
        if set(names) != set(GEMINI_READABLE_NAMES) or len(names) != len(GEMINI_READABLE_NAMES):
            errors.append("GEMINI_READABLE_ROOT_CONTENT_MISMATCH")
        for name in names:
            try:
                payload = archive.read(name)
                if not payload:
                    errors.append(f"EMPTY_ENTRY:{name}")
                    continue
                if b"\x00" in payload:
                    errors.append(f"OPAQUE_OR_BINARY_MEMBER:{name}")
                    continue
                decoded[name] = payload.decode("utf-8")
            except (KeyError, UnicodeDecodeError) as exc:
                errors.append(f"UNREADABLE_TEXT_MEMBER:{name}:{type(exc).__name__}")
        try:
            manifest = json.loads(decoded["PACKAGE_MANIFEST.json"])
            if manifest.get("contract") != GEMINI_READABLE_CONTRACT:
                errors.append("GEMINI_READABLE_CONTRACT_MISMATCH")
            for row in manifest.get("files") or []:
                member = str(row.get("name") or "")
                if member not in names:
                    errors.append(f"GEMINI_READABLE_MANIFEST_MEMBER_MISSING:{member}")
                    continue
                if row.get("sha256") != _zip_member_sha256(archive, member):
                    errors.append(f"GEMINI_READABLE_MANIFEST_HASH_MISMATCH:{member}")
                if int(row.get("byte_size") or -1) != archive.getinfo(member).file_size:
                    errors.append(f"GEMINI_READABLE_MANIFEST_SIZE_MISMATCH:{member}")
        except Exception as exc:
            errors.append(f"GEMINI_READABLE_MANIFEST_PARSE_FAILED:{type(exc).__name__}")
        try:
            sums = {
                line.split(None, 1)[1].strip(): line.split(None, 1)[0].strip()
                for line in decoded["SHA256SUMS.txt"].splitlines()
                if line.strip()
            }
            for member in GEMINI_READABLE_NAMES[:9]:
                if sums.get(member) != _zip_member_sha256(archive, member):
                    errors.append(f"GEMINI_READABLE_SHA256SUM_MISMATCH:{member}")
        except Exception as exc:
            errors.append(f"GEMINI_READABLE_SHA256SUM_PARSE_FAILED:{type(exc).__name__}")
    corpus = decoded.get("PROJECT_CORPUS.txt", "")
    if (
        f"PROVIDER_READABILITY_CONTRACT={READABILITY_CONTRACT}" not in corpus
        or CORPUS_BEGIN not in corpus
        or CORPUS_END not in corpus
    ):
        errors.append("GEMINI_READABLE_CORPUS_CONTRACT_MISSING")
    try:
        file_index = json.loads(decoded.get("PROJECT_FILE_INDEX.json", ""))
        if file_index.get("contract") != READABILITY_CONTRACT:
            errors.append("GEMINI_READABLE_FILE_INDEX_CONTRACT_MISMATCH")
    except Exception as exc:
        errors.append(f"GEMINI_READABLE_FILE_INDEX_PARSE_FAILED:{type(exc).__name__}")
    return {
        "package": str(package),
        "package_contract": GEMINI_READABLE_CONTRACT,
        "package_byte_size": package_byte_size,
        "provider_size_limit_bytes": GEMINI_PACKAGE_MAX_BYTES,
        "provider_size_status": "PASS" if package_byte_size <= GEMINI_PACKAGE_MAX_BYTES else "FAIL",
        "sha256": _sha256_file(package),
        "root_entry_count": len(names),
        "root_entries": names,
        "nested_zip_count": 0,
        "all_files_directly_readable": not any(
            error.startswith(("OPAQUE_OR_BINARY_MEMBER", "UNREADABLE_TEXT_MEMBER"))
            for error in errors
        ),
        "dominant_opaque_binary_member": False,
        "zip_crc": "PASS" if bad_crc is None else "FAIL",
        "source_chatgpt_package_sha256": str(
            manifest.get("source_chatgpt_package_sha256") or ""
        ),
        "package_use_mode": str(manifest.get("package_use_mode") or "CANONICAL_FLASHABLE"),
        "provider_readability": {
            "status": "PASS" if not errors else "FAIL",
            "contract": READABILITY_CONTRACT,
        },
        "consumer_compatibility": compatibility,
        "archive_reopened": True,
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }


def validate_gemini_exact10(
    path: str | Path,
    required_names: Iterable[str] = T021_GEMINI_EXACT10_NAMES,
) -> dict:
    package = Path(path)
    with zipfile.ZipFile(package, "r") as archive:
        root_names = set(archive.namelist())
    if {
        "OPEN_ME_FIRST.txt",
        "PROJECT_CORPUS.txt",
        "PROJECT_FILE_INDEX.json",
        "PACKAGE_MANIFEST.json",
    }.issubset(root_names):
        return _validate_gemini_provider_readable(package)
    required = tuple(required_names)
    errors: list[str] = []
    compatibility = audit_provider_package(package, "GEMINI")
    if compatibility["status"] != "PASS":
        errors.extend(
            "CONSUMER_COMPATIBILITY:" + error
            for error in compatibility.get("errors") or []
        )
    package_byte_size = package.stat().st_size
    if package_byte_size > GEMINI_PACKAGE_MAX_BYTES:
        errors.append(
            f"GEMINI_PACKAGE_SIZE_LIMIT_EXCEEDED:{package_byte_size}>{GEMINI_PACKAGE_MAX_BYTES}"
        )
    with zipfile.ZipFile(package, "r") as archive:
        names = archive.namelist()
        bad_crc = archive.testzip()
        if bad_crc:
            errors.append(f"ZIP_CRC_FAILED:{bad_crc}")
        if len(names) != 10:
            errors.append(f"ROOT_ENTRY_COUNT:{len(names)}")
        non_root = [name for name in names if name.endswith("/") or "/" in name or "\\" in name]
        if non_root:
            errors.append("NON_ROOT_ENTRIES:" + ",".join(non_root))
        if set(names) != set(required):
            missing = sorted(set(required) - set(names))
            extra = sorted(set(names) - set(required))
            if missing:
                errors.append("MISSING_REQUIRED:" + ",".join(missing))
            if extra:
                errors.append("UNEXPECTED_ENTRIES:" + ",".join(extra))
        nested = _nested_zip_members(archive, names)
        if nested:
            errors.append("NESTED_ZIP_FORBIDDEN:" + ",".join(nested))
        unreadable = []
        empty = []
        for name in names:
            try:
                info = archive.getinfo(name)
                with archive.open(info, "r") as stream:
                    stream.read(1)
                if info.file_size <= 0:
                    empty.append(name)
            except Exception:
                unreadable.append(name)
        if unreadable:
            errors.append("UNREADABLE_ENTRIES:" + ",".join(unreadable))
        if empty:
            errors.append("EMPTY_ENTRIES:" + ",".join(empty))
        sqlite_results = _sqlite_members(
            archive,
            names,
            project_contract_member="PROJECT_CONJOINED_WRITE.sqlite",
        )
        for result in sqlite_results:
            if result["error"] or result["integrity_check"] != ["ok"] or result["foreign_key_violations"]:
                errors.append(f"SQLITE_VALIDATION_FAILED:{result['member']}")

        pointer_documents: dict[str, dict[str, str]] = {}
        source_chatgpt_package_sha256 = ""
        project_contract: dict = {
            "missing_tables": [],
            "source_database_count": 0,
            "sector_router_count": 0,
            "sector_ids": [],
            "chat_lineage_access": None,
            "research_access": None,
            "automatic_append_lane_ids": [],
            "project_core_projection_count": 0,
            "sector_projection_count": 0,
            "source_byte_reconstruction_count": 0,
            "source_logical_projection_count": 0,
            "canonical_content_blob_count": 0,
            "reconstruction_failures": [],
            "error": "NOT_READ",
        }
        provider_readability = {
            "status": "LEGACY_NOT_PRESENT",
            "contract": None,
            "corpus_begin": False,
            "corpus_end": False,
        }
        try:
            prompt_text = archive.read("GEMINI_FLASH_PROMPT.txt").decode("utf-8")
            if "PROVIDER_READABILITY_CONTRACT=" in prompt_text:
                contract_line = next(
                    (
                        line.split("=", 1)[1].strip()
                        for line in prompt_text.splitlines()
                        if line.startswith("PROVIDER_READABILITY_CONTRACT=")
                    ),
                    "",
                )
                provider_readability = {
                    "status": "PASS",
                    "contract": contract_line,
                    "corpus_begin": CORPUS_BEGIN in prompt_text,
                    "corpus_end": CORPUS_END in prompt_text,
                }
                if (
                    contract_line != READABILITY_CONTRACT
                    or not provider_readability["corpus_begin"]
                    or not provider_readability["corpus_end"]
                ):
                    provider_readability["status"] = "FAIL"
                    errors.append("GEMINI_PROVIDER_READABLE_CORPUS_INVALID")
            for pointer_name in (
                ".uepc_env",
                ".uepc_profile",
                ".uepc_project",
                ".uepc_project_write",
            ):
                pointer_documents[pointer_name] = _pointer_values(archive.read(pointer_name))
            package_use_mode = pointer_documents[".uepc_env"].get(
                "PACKAGE_USE_MODE",
                "CANONICAL_FLASHABLE",
            )
            if package_use_mode not in {"CANONICAL_FLASHABLE", "READ_ONLY_STRESS_RESULT"}:
                errors.append("EXACT10_PACKAGE_USE_MODE_INVALID")
            read_only_stress = package_use_mode == "READ_ONLY_STRESS_RESULT"
            expected_pointers = {
                ".uepc_env": {
                    "ENV_SQLITE": "ENV_PUBLIC_READONLY.sqlite",
                    "ENV_MMD_PNG": "ENV_MMD_RENDER.png",
                    "ENV_WRITE_LOCK": "true",
                    "PROFILE_POINTER": ".uepc_profile",
                    "PROJECT_POINTER": ".uepc_project",
                    "PROJECT_WRITE_POINTER": ".uepc_project_write",
                    "FLASH_PROMPT": "GEMINI_FLASH_PROMPT.txt",
                },
                ".uepc_profile": {
                    "UOP_SQLITE": "UOP_PUBLIC_READONLY.sqlite",
                    "UOP_MMD_PNG": "UOP_MMD_RENDER.png",
                    "UOP_WRITE_LOCK": "true",
                },
                ".uepc_project": {
                    "PROJECT_SQLITE": "PROJECT_CONJOINED_WRITE.sqlite",
                    "PROJECT_CORE_NAMESPACE": "project_core__",
                    "PROJECT_CORE_ACCESS": "READ_ONLY_ROUTER_METADATA",
                    "PROJECT_WRITE_POINTER": ".uepc_project_write",
                },
                ".uepc_project_write": {
                    "PROJECT_WRITE_SQLITE": "PROJECT_CONJOINED_WRITE.sqlite",
                    "WRITE_ROUTER_TABLE": "gemini_project_write_router",
                    "WRITE_GRANT_TABLE": "gemini_mutation_grant",
                    "WRITE_LOG_TABLE": "gemini_project_write_log",
                    "PROJECT_HEAD_TABLE": "gemini_project_head",
                    "CHAT_LINEAGE_NAMESPACE": "sector_chat_lineage__",
                    "CHAT_LINEAGE_ACCESS": (
                        "READ_ONLY" if read_only_stress else "APPEND_ONLY_READ_WRITE"
                    ),
                    "RESEARCH_NAMESPACE": "sector_research__",
                    "RESEARCH_ACCESS": (
                        "READ_ONLY" if read_only_stress else "APPEND_ONLY_READ_WRITE"
                    ),
                    "AUTOMATIC_APPEND_LANES": (
                        "NONE" if read_only_stress else "chat_lineage,research"
                    ),
                    "SOURCE_DB_BLOB_TABLE": "embedded_database_blob",
                    "SOURCE_DB_BLOB_CHUNK_TABLE": "embedded_database_blob_chunk",
                    "SOURCE_SCHEMA_REGISTRY": "source_schema_object_registry",
                    "SOURCE_ROW_COUNT_REGISTRY": "source_row_count_registry",
                },
            }
            for pointer_name, expected_values in expected_pointers.items():
                observed_values = pointer_documents[pointer_name]
                for key, expected_value in expected_values.items():
                    if observed_values.get(key) != expected_value:
                        errors.append(
                            f"EXACT10_POINTER_MISMATCH:{pointer_name}:{key}"
                        )
            if pointer_documents[".uepc_env"].get("PACKAGE_USE_MODE"):
                if pointer_documents[".uepc_project"].get("PACKAGE_USE_MODE") != package_use_mode:
                    errors.append("EXACT10_PROJECT_PACKAGE_USE_MODE_MISMATCH")
                write_pointer = pointer_documents[".uepc_project_write"]
                if write_pointer.get("PACKAGE_USE_MODE") != package_use_mode:
                    errors.append("EXACT10_WRITE_PACKAGE_USE_MODE_MISMATCH")
                expected_mutation = "false" if read_only_stress else "true"
                expected_stress = "true" if read_only_stress else "false"
                if write_pointer.get("MUTATION_INSTRUCTIONS_AUTHORIZED") != expected_mutation:
                    errors.append("EXACT10_MUTATION_AUTHORITY_MISMATCH")
                if write_pointer.get("STRESS_RESULT_READ_ONLY") != expected_stress:
                    errors.append("EXACT10_STRESS_READ_ONLY_MISMATCH")
            source_chatgpt_package_sha256 = pointer_documents[".uepc_env"].get(
                "SOURCE_CHATGPT_PACKAGE_SHA256",
                "",
            )
            if (
                source_chatgpt_package_sha256
                and source_chatgpt_package_sha256 != "UNAVAILABLE_DIRECT_COMPATIBILITY"
                and (
                    len(source_chatgpt_package_sha256) != 64
                    or any(character not in "0123456789abcdefABCDEF" for character in source_chatgpt_package_sha256)
                )
            ):
                errors.append("SOURCE_CHATGPT_PACKAGE_SHA256_INVALID")
            project_contract = next(
                (
                    result["project_contract"]
                    for result in sqlite_results
                    if result["member"] == "PROJECT_CONJOINED_WRITE.sqlite"
                    and "project_contract" in result
                ),
                project_contract,
            )
            if project_contract["error"]:
                errors.append("PROJECT_CONJOINED_CONTRACT_ERROR")
            if project_contract["missing_tables"]:
                errors.append(
                    "PROJECT_CONJOINED_TABLES_MISSING:"
                    + ",".join(project_contract["missing_tables"])
                )
            observed_sector_ids = set(project_contract["sector_ids"])
            observed_sector_layout = tuple(sorted(observed_sector_ids))
            canonical_sector_layout = next(
                (
                    layout for layout in CANONICAL_GEMINI_PROJECT_SECTOR_LAYOUTS
                    if layout == observed_sector_layout
                ),
                (),
            )
            project_contract["canonical_sector_layout"] = list(canonical_sector_layout)
            project_contract["canonical_sector_count"] = len(canonical_sector_layout)
            missing_base_sectors = sorted(set(EXPECTED_SECTORS) - observed_sector_ids)
            unexpected_runtime_sectors = sorted(
                observed_sector_ids
                - set(EXPECTED_SECTORS)
                - set(ALLOWED_RUNTIME_PROJECT_SECTORS)
            )
            if missing_base_sectors:
                errors.append(
                    "PROJECT_CONJOINED_BASE_SECTORS_MISSING:"
                    + ",".join(missing_base_sectors)
                )
            if unexpected_runtime_sectors:
                errors.append(
                    "PROJECT_CONJOINED_RUNTIME_SECTORS_UNEXPECTED:"
                    + ",".join(unexpected_runtime_sectors)
                )
            if canonical_sector_layout and project_contract["sector_router_count"] != len(canonical_sector_layout):
                errors.append(
                    f"PROJECT_CONJOINED_SECTOR_ROUTER_COUNT:{project_contract['sector_router_count']}"
                )
            if project_contract["project_core_source_database_count"] < 1:
                errors.append(
                    "PROJECT_CONJOINED_PROJECT_CORE_DATABASE_COUNT:"
                    + str(project_contract["project_core_source_database_count"])
                )
            if (
                project_contract["sector_source_database_count"]
                < project_contract["sector_router_count"]
            ):
                errors.append(
                    "PROJECT_CONJOINED_SECTOR_DATABASE_COUNT:"
                    + str(project_contract["sector_source_database_count"])
                )
            if (
                project_contract["source_database_count"]
                != project_contract["project_core_source_database_count"]
                + project_contract["sector_source_database_count"]
            ):
                errors.append(
                    "PROJECT_CONJOINED_SOURCE_DATABASE_COUNT:"
                    + str(project_contract["source_database_count"])
                )
            if project_contract["reconstruction_failures"]:
                errors.append(
                    "PROJECT_CONJOINED_SOURCE_RECONSTRUCTION_FAILED:"
                    + str(len(project_contract["reconstruction_failures"]))
                )
            if (
                project_contract["source_byte_reconstruction_count"]
                + project_contract["source_logical_projection_count"]
                != project_contract["source_database_count"]
            ):
                errors.append(
                    "PROJECT_CONJOINED_SOURCE_COVERAGE_COUNT:"
                    + str(
                        project_contract["source_byte_reconstruction_count"]
                        + project_contract["source_logical_projection_count"]
                    )
                )
            expected_chat_access = "READ_ONLY" if read_only_stress else "APPEND_ONLY_READ_WRITE"
            if project_contract["chat_lineage_access"] != expected_chat_access:
                errors.append("PROJECT_CONJOINED_CHAT_LINEAGE_ACCESS_INVALID")
            if project_contract["research_access"] != expected_chat_access:
                errors.append("PROJECT_CONJOINED_RESEARCH_ACCESS_INVALID")
            expected_automatic = [] if read_only_stress else ["chat_lineage", "research"]
            if sorted(project_contract["automatic_append_lane_ids"]) != expected_automatic:
                errors.append("PROJECT_CONJOINED_AUTOMATIC_APPEND_LANES_INVALID")
            if not project_contract["project_core_projection_count"]:
                errors.append("PROJECT_CONJOINED_CORE_PROJECTION_MISSING")
            if not project_contract["sector_projection_count"]:
                errors.append("PROJECT_CONJOINED_SECTOR_PROJECTION_MISSING")
        except Exception as exc:
            errors.append(f"EXACT10_STRUCTURE_PARSE_FAILED:{type(exc).__name__}")

    return {
        "package": str(package),
        "package_contract": "T023_GEMINI_EXACT10_STRUCTURE_V1",
        "package_byte_size": package_byte_size,
        "provider_size_limit_bytes": GEMINI_PACKAGE_MAX_BYTES,
        "provider_size_status": "PASS" if package_byte_size <= GEMINI_PACKAGE_MAX_BYTES else "FAIL",
        "sha256": _sha256_file(package),
        "root_entry_count": len(names),
        "root_entries": names,
        "nested_zip_count": len(nested),
        "all_files_directly_readable": not unreadable and not non_root,
        "zip_crc": "PASS" if bad_crc is None else "FAIL",
        "sqlite_results": sqlite_results,
        "pointer_documents": pointer_documents,
        "project_contract": project_contract,
        "provider_readability": provider_readability,
        "source_chatgpt_package_sha256": source_chatgpt_package_sha256,
        "package_use_mode": (
            pointer_documents.get(".uepc_env", {}).get(
                "PACKAGE_USE_MODE",
                "CANONICAL_FLASHABLE",
            )
        ),
        "receipt_hashes": "NOT_APPLICABLE_EXACT10_POINTER_STRUCTURE",
        "archive_reopened": True,
        "consumer_compatibility": compatibility,
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }
