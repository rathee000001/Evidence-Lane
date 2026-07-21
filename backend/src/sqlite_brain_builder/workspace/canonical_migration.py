from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlite_brain_builder.workspace.workspace_roots import WORKSPACE_ROOTS_SCHEMA


MIGRATION_SCHEMA = "T023_CANONICAL_ROOT_COPY_FIRST_MIGRATION_V1"
MIGRATION_MANIFEST = Path("migration") / "T023_CANONICAL_ROOT_MIGRATION_MANIFEST.json"
MIGRATION_RECEIPT = Path("migration") / "T023_CANONICAL_ROOT_MIGRATION_RECEIPT.json"
ROUTE_REWRITE_RECEIPT = Path("migration") / "T023_CANONICAL_ROOT_ROUTE_REWRITE_RECEIPT.json"
_ALLOWED_ADDITIONS = {
    MIGRATION_MANIFEST.as_posix(),
    MIGRATION_RECEIPT.as_posix(),
    ROUTE_REWRITE_RECEIPT.as_posix(),
    "config/workspace-roots.json",
    "config/brain-catalog-workspaces.json",
    "config/workspace-root.txt",
}
_ALLOWED_ADDITION_PREFIXES = ("receipts/", "runtime/")
_CANONICAL_ROUTE_COLUMNS: dict[str, tuple[str, ...]] = {
    "brain_build_event": ("receipt_path",),
    "brain_last_state_pointer": ("output_folder",),
    "brain_package_event": ("package_path",),
    "brain_project": ("output_dir",),
    "brain_sector_version": ("db_path",),
    "brain_source_event": ("source_path",),
    "sector_mmd_registry": ("mmd_path",),
}
_REPARSE_POINT_ATTRIBUTE = 0x400


class CanonicalMigrationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _absolute(path: str | Path, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_absolute():
        raise CanonicalMigrationError(f"{label}_MUST_BE_ABSOLUTE")
    return resolved


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _validate_roots(source: str | Path, target: str | Path) -> tuple[Path, Path]:
    source_root = _absolute(source, label="CANONICAL_MIGRATION_SOURCE")
    target_root = _absolute(target, label="CANONICAL_MIGRATION_TARGET")
    if not source_root.is_dir():
        raise CanonicalMigrationError(f"CANONICAL_SOURCE_ROOT_NOT_FOUND:{source_root}")
    if _same_path(source_root, target_root):
        raise CanonicalMigrationError("CANONICAL_SOURCE_AND_TARGET_MUST_DIFFER")
    try:
        target_root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise CanonicalMigrationError("CANONICAL_TARGET_CANNOT_BE_INSIDE_SOURCE")
    try:
        source_root.relative_to(target_root)
    except ValueError:
        pass
    else:
        raise CanonicalMigrationError("CANONICAL_SOURCE_CANNOT_BE_INSIDE_TARGET")
    return source_root, target_root


def _reject_reparse(path: Path) -> None:
    stat = path.lstat()
    if path.is_symlink() or (getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE):
        raise CanonicalMigrationError(f"CANONICAL_MIGRATION_REPARSE_POINT_FORBIDDEN:{path}")


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []

    def visit(directory: Path) -> None:
        _reject_reparse(directory)
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        for entry in entries:
            path = Path(entry.path)
            _reject_reparse(path)
            if entry.is_dir(follow_symlinks=False):
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                raise CanonicalMigrationError(f"CANONICAL_MIGRATION_UNSUPPORTED_ENTRY:{path}")

    visit(root)
    return files


def _walk_directories(root: Path) -> list[Path]:
    directories: list[Path] = []

    def visit(directory: Path) -> None:
        _reject_reparse(directory)
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        for entry in entries:
            path = Path(entry.path)
            _reject_reparse(path)
            if entry.is_dir(follow_symlinks=False):
                directories.append(path)
                visit(path)
            elif not entry.is_file(follow_symlinks=False):
                raise CanonicalMigrationError(f"CANONICAL_MIGRATION_UNSUPPORTED_ENTRY:{path}")

    visit(root)
    directories.sort(key=lambda path: path.relative_to(root).as_posix().casefold())
    return directories


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(entries: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(str(entry["relative_path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(int(entry["size_bytes"])).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _tree_manifest(root: Path) -> tuple[list[dict[str, Any]], int, str]:
    entries: list[dict[str, Any]] = []
    total = 0
    for path in _walk_files(root):
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total += size
        entries.append(
            {
                "relative_path": relative,
                "size_bytes": size,
                "sha256": _sha256(path),
            }
        )
    entries.sort(key=lambda row: str(row["relative_path"]).casefold())
    return entries, total, _tree_digest(entries)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _target_nonempty(target: Path) -> bool:
    return target.exists() and (not target.is_dir() or any(target.iterdir()))


def _json_legacy_route_count(value: Any, source_root: Path) -> int:
    if isinstance(value, dict):
        return sum(_json_legacy_route_count(child, source_root) for child in value.values())
    if isinstance(value, list):
        return sum(_json_legacy_route_count(child, source_root) for child in value)
    return int(_mapped_canonical_path(value, source_root, source_root) is not None)


def _mapped_canonical_path(
    value: Any,
    source_root: Path,
    target_root: Path,
) -> tuple[str, Path] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = os.path.normpath(value)
    source = os.path.normpath(str(source_root))
    try:
        common = os.path.commonpath([source, candidate])
    except (OSError, ValueError):
        return None
    if os.path.normcase(common) != os.path.normcase(source):
        return None
    relative_text = os.path.relpath(candidate, source)
    if relative_text == os.curdir:
        relative = Path()
    elif relative_text == os.pardir or relative_text.startswith(os.pardir + os.sep):
        return None
    else:
        relative = Path(relative_text)
    return str(target_root / relative), relative


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not exists:
        return set()
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _legacy_live_route_count(
    connection: sqlite3.Connection,
    source_root: Path,
) -> int:
    count = 0
    for table, columns in _CANONICAL_ROUTE_COLUMNS.items():
        available = _table_columns(connection, table)
        for column in columns:
            if column not in available:
                continue
            for (value,) in connection.execute(
                f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            ):
                if _mapped_canonical_path(value, source_root, source_root) is not None:
                    count += 1
    return count


def _rewrite_canonical_live_routes(
    source_root: Path,
    target_root: Path,
    physical_root: Path,
    *,
    actor: str,
    stamp: str,
) -> dict[str, Any]:
    workspace_database = physical_root / "workspace.sqlite"
    if not workspace_database.is_file():
        raise CanonicalMigrationError("CANONICAL_MIGRATION_WORKSPACE_DATABASE_MISSING")

    created_directories: list[str] = []
    for source_directory in _walk_directories(source_root):
        relative = source_directory.relative_to(source_root)
        destination_directory = physical_root / relative
        if not destination_directory.exists():
            destination_directory.mkdir(parents=True, exist_ok=False)
            created_directories.append(relative.as_posix())

    pre_database_sha256 = _sha256(workspace_database)
    connection = sqlite3.connect(workspace_database)
    rewrites: list[dict[str, Any]] = []
    historical_missing_references: list[dict[str, Any]] = []
    try:
        before_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if before_integrity.casefold() != "ok":
            raise CanonicalMigrationError(
                f"CANONICAL_MIGRATION_WORKSPACE_INTEGRITY_FAILED_BEFORE:{before_integrity}"
            )
        connection.execute("BEGIN IMMEDIATE")
        for table, columns in _CANONICAL_ROUTE_COLUMNS.items():
            available = _table_columns(connection, table)
            for column in columns:
                if column not in available:
                    continue
                values = [
                    row[0]
                    for row in connection.execute(
                        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                    )
                ]
                rewritten_rows = 0
                rewritten_values = 0
                for old_value in values:
                    mapped = _mapped_canonical_path(old_value, source_root, target_root)
                    if mapped is None:
                        continue
                    new_value, relative = mapped
                    physical_target = physical_root / relative
                    source_target = source_root / relative
                    if source_target.exists() and not physical_target.exists():
                        raise CanonicalMigrationError(
                            "CANONICAL_MIGRATION_ROUTED_TARGET_MISSING:"
                            + relative.as_posix()
                        )
                    if not source_target.exists():
                        historical_missing_references.append(
                            {
                                "table": table,
                                "column": column,
                                "source_value": str(old_value),
                                "canonical_value": new_value,
                            }
                        )
                    cursor = connection.execute(
                        f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?',
                        (new_value, old_value),
                    )
                    if cursor.rowcount:
                        rewritten_rows += int(cursor.rowcount)
                        rewritten_values += 1
                if rewritten_rows:
                    rewrites.append(
                        {
                            "table": table,
                            "column": column,
                            "rewritten_rows": rewritten_rows,
                            "rewritten_distinct_values": rewritten_values,
                        }
                    )
        remaining = _legacy_live_route_count(connection, source_root)
        if remaining:
            raise CanonicalMigrationError(
                f"CANONICAL_MIGRATION_LEGACY_LIVE_ROUTES_REMAIN:{remaining}"
            )
        after_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if after_integrity.casefold() != "ok":
            raise CanonicalMigrationError(
                f"CANONICAL_MIGRATION_WORKSPACE_INTEGRITY_FAILED_AFTER:{after_integrity}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    post_database_sha256 = _sha256(workspace_database)
    receipt = {
        "schema": "T023_CANONICAL_ROOT_ROUTE_REWRITE_V1",
        "status": "PASS_CANONICAL_LIVE_ROUTES_REWRITTEN",
        "created_at": stamp,
        "actor": str(actor),
        "source_root": str(source_root),
        "target_root": str(target_root),
        "workspace_database": str(target_root / "workspace.sqlite"),
        "pre_route_database_sha256": pre_database_sha256,
        "post_route_database_sha256": post_database_sha256,
        "rewritten_route_count": sum(int(row["rewritten_rows"]) for row in rewrites),
        "rewrites": rewrites,
        "created_empty_directory_count": len(created_directories),
        "created_empty_directories": created_directories,
        "historical_missing_reference_count": len(historical_missing_references),
        "historical_missing_references": historical_missing_references,
        "legacy_live_route_count": 0,
        "source_deleted": False,
        "legacy_route_allowed": False,
        "sqlite_integrity_check": "ok",
    }
    receipt["receipt_payload_sha256"] = hashlib.sha256(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _atomic_json(physical_root / ROUTE_REWRITE_RECEIPT, receipt)
    return receipt


def finalize_canonical_routes(
    source: str | Path,
    target: str | Path,
    *,
    actor: str,
) -> dict[str, Any]:
    source_root, target_root = _validate_roots(source, target)
    if not target_root.is_dir():
        raise CanonicalMigrationError(f"CANONICAL_TARGET_ROOT_NOT_FOUND:{target_root}")
    return _rewrite_canonical_live_routes(
        source_root,
        target_root,
        target_root,
        actor=actor,
        stamp=_utc_now(),
    )


def plan_canonical_migration(source: str | Path, target: str | Path) -> dict[str, Any]:
    source_root, target_root = _validate_roots(source, target)
    if _target_nonempty(target_root):
        raise CanonicalMigrationError(f"CANONICAL_TARGET_ALREADY_NONEMPTY:{target_root}")
    entries, total, tree_sha256 = _tree_manifest(source_root)
    directories = _walk_directories(source_root)
    disk = shutil.disk_usage(target_root.parent)
    reserve = max(64 * 1024 * 1024, int(total * 0.02))
    required = total + reserve
    return {
        "schema": MIGRATION_SCHEMA,
        "status": "READY_COPY_FIRST" if disk.free >= required else "BLOCKED_INSUFFICIENT_SPACE",
        "source_root": str(source_root),
        "target_root": str(target_root),
        "source_file_count": len(entries),
        "source_directory_count": len(directories),
        "source_bytes": total,
        "source_tree_sha256": tree_sha256,
        "required_free_bytes": required,
        "available_free_bytes": disk.free,
        "source_deleted": False,
        "target_merge_allowed": False,
    }


def _write_route_registry(staging: Path, target: Path, *, actor: str, stamp: str) -> None:
    registry = {
        "schema": WORKSPACE_ROOTS_SCHEMA,
        "active_root": str(target),
        "roots": [
            {
                "path": str(target),
                "display_name": target.name or str(target),
                "source": f"CANONICAL_COPY_FIRST_MIGRATION:{actor}",
                "added_at": stamp,
                "last_used_at": stamp,
                "active": True,
            }
        ],
        "updated_at": stamp,
    }
    _atomic_json(staging / "config" / "workspace-roots.json", registry)


def execute_canonical_migration(
    source: str | Path,
    target: str | Path,
    *,
    actor: str,
) -> dict[str, Any]:
    source_root, target_root = _validate_roots(source, target)
    if _target_nonempty(target_root):
        raise CanonicalMigrationError(f"CANONICAL_TARGET_ALREADY_NONEMPTY:{target_root}")
    if target_root.exists():
        target_root.rmdir()

    entries, total, source_tree_sha256 = _tree_manifest(source_root)
    directories = _walk_directories(source_root)
    disk = shutil.disk_usage(target_root.parent)
    reserve = max(64 * 1024 * 1024, int(total * 0.02))
    if disk.free < total + reserve:
        raise CanonicalMigrationError(
            f"CANONICAL_MIGRATION_INSUFFICIENT_SPACE:required={total + reserve}:available={disk.free}"
        )

    staging = target_root.parent / f".{target_root.name}.migration-{uuid.uuid4().hex}"
    if staging.exists():
        raise CanonicalMigrationError(f"CANONICAL_MIGRATION_STAGING_COLLISION:{staging}")
    staging.mkdir(parents=False)
    stamp = _utc_now()
    try:
        for source_directory in directories:
            (staging / source_directory.relative_to(source_root)).mkdir(
                parents=True,
                exist_ok=True,
            )
        copied_entries: list[dict[str, Any]] = []
        for entry in entries:
            relative = Path(str(entry["relative_path"]))
            source_path = source_root / relative
            destination_path = staging / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            actual_size = destination_path.stat().st_size
            actual_sha256 = _sha256(destination_path)
            if actual_size != int(entry["size_bytes"]) or actual_sha256 != str(entry["sha256"]):
                raise CanonicalMigrationError(
                    f"CANONICAL_MIGRATION_COPY_HASH_MISMATCH:{entry['relative_path']}"
                )
            copied_entries.append(
                {
                    "relative_path": entry["relative_path"],
                    "size_bytes": actual_size,
                    "sha256": actual_sha256,
                }
            )

        destination_tree_sha256 = _tree_digest(copied_entries)
        if destination_tree_sha256 != source_tree_sha256:
            raise CanonicalMigrationError("CANONICAL_MIGRATION_TREE_HASH_MISMATCH")

        manifest = {
            "schema": MIGRATION_SCHEMA,
            "created_at": stamp,
            "source_root": str(source_root),
            "target_root": str(target_root),
            "source_file_count": len(entries),
            "source_directory_count": len(directories),
            "source_bytes": total,
            "source_tree_sha256": source_tree_sha256,
            "directories": [
                path.relative_to(source_root).as_posix()
                for path in directories
            ],
            "files": entries,
        }
        _atomic_json(staging / MIGRATION_MANIFEST, manifest)
        manifest_sha256 = _sha256(staging / MIGRATION_MANIFEST)
        receipt_payload = {
            "schema": MIGRATION_SCHEMA,
            "status": "PASS_CANONICAL_ROOT_MIGRATED",
            "created_at": stamp,
            "actor": str(actor),
            "source_root": str(source_root),
            "target_root": str(target_root),
            "verified_file_count": len(entries),
            "verified_bytes": total,
            "source_tree_sha256": source_tree_sha256,
            "destination_tree_sha256": destination_tree_sha256,
            "manifest_sha256": manifest_sha256,
            "source_deleted": False,
            "target_merge_used": False,
            "atomic_directory_commit": True,
            "legacy_route_allowed": False,
        }
        receipt_payload["receipt_payload_sha256"] = hashlib.sha256(
            json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _atomic_json(staging / MIGRATION_RECEIPT, receipt_payload)
        _write_route_registry(staging, target_root, actor=str(actor), stamp=stamp)
        route_rewrite = _rewrite_canonical_live_routes(
            source_root,
            target_root,
            staging,
            actor=str(actor),
            stamp=stamp,
        )
        os.replace(staging, target_root)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    verified = verify_canonical_migration(source_root, target_root)
    return {
        **receipt_payload,
        **route_rewrite,
        **verified,
        "status": "PASS_CANONICAL_ROOT_MIGRATED_AND_ROUTED",
        "migration_manifest": str(target_root / MIGRATION_MANIFEST),
        "migration_receipt": str(target_root / MIGRATION_RECEIPT),
        "workspace_root_registry": str(target_root / "config" / "workspace-roots.json"),
    }


def verify_canonical_migration(source: str | Path, target: str | Path) -> dict[str, Any]:
    source_root, target_root = _validate_roots(source, target)
    manifest_path = target_root / MIGRATION_MANIFEST
    receipt_path = target_root / MIGRATION_RECEIPT
    registry_path = target_root / "config" / "workspace-roots.json"
    route_receipt_path = target_root / ROUTE_REWRITE_RECEIPT
    if not manifest_path.is_file() or not receipt_path.is_file() or not registry_path.is_file():
        raise CanonicalMigrationError("CANONICAL_MIGRATION_AUTHORITY_FILES_MISSING")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MIGRATION_SCHEMA or not isinstance(manifest.get("files"), list):
        raise CanonicalMigrationError("CANONICAL_MIGRATION_MANIFEST_INVALID")
    if (
        raw_receipt.get("schema") != MIGRATION_SCHEMA
        or os.path.normcase(str(raw_receipt.get("source_root") or ""))
        != os.path.normcase(str(source_root))
        or os.path.normcase(str(raw_receipt.get("target_root") or ""))
        != os.path.normcase(str(target_root))
    ):
        raise CanonicalMigrationError("CANONICAL_MIGRATION_RECEIPT_INVALID")

    for relative_text in manifest.get("directories") or []:
        relative = Path(str(relative_text))
        if not (source_root / relative).is_dir() or not (target_root / relative).is_dir():
            raise CanonicalMigrationError(
                f"CANONICAL_MIGRATION_DIRECTORY_MISSING:{relative.as_posix()}"
            )

    expected_entries = sorted(
        [dict(entry) for entry in manifest["files"]],
        key=lambda row: str(row["relative_path"]).casefold(),
    )
    source_entries: list[dict[str, Any]] = []
    destination_entries: list[dict[str, Any]] = []
    route_receipt = (
        json.loads(route_receipt_path.read_text(encoding="utf-8"))
        if route_receipt_path.is_file()
        else None
    )
    if route_receipt is not None and (
        route_receipt.get("schema") != "T023_CANONICAL_ROOT_ROUTE_REWRITE_V1"
        or route_receipt.get("status") != "PASS_CANONICAL_LIVE_ROUTES_REWRITTEN"
        or os.path.normcase(str(route_receipt.get("source_root") or ""))
        != os.path.normcase(str(source_root))
        or os.path.normcase(str(route_receipt.get("target_root") or ""))
        != os.path.normcase(str(target_root))
    ):
        raise CanonicalMigrationError("CANONICAL_MIGRATION_ROUTE_RECEIPT_INVALID")
    for expected in expected_entries:
        relative = Path(str(expected["relative_path"]))
        source_path = source_root / relative
        destination_path = target_root / relative
        if not source_path.is_file() or not destination_path.is_file():
            raise CanonicalMigrationError(f"CANONICAL_MIGRATION_FILE_MISSING:{relative.as_posix()}")
        source_entry = {
            "relative_path": relative.as_posix(),
            "size_bytes": source_path.stat().st_size,
            "sha256": _sha256(source_path),
        }
        destination_entry = {
            "relative_path": relative.as_posix(),
            "size_bytes": destination_path.stat().st_size,
            "sha256": _sha256(destination_path),
        }
        destination_rewritten = bool(
            route_receipt is not None and relative.as_posix() == "workspace.sqlite"
        )
        if source_entry != expected or (not destination_rewritten and destination_entry != expected):
            raise CanonicalMigrationError(f"CANONICAL_MIGRATION_PARITY_FAILED:{relative.as_posix()}")
        source_entries.append(source_entry)
        destination_entries.append(destination_entry)

    expected_paths = {str(entry["relative_path"]) for entry in expected_entries}
    destination_paths = {path.relative_to(target_root).as_posix() for path in _walk_files(target_root)}
    unexpected = sorted(
        path
        for path in destination_paths - expected_paths - _ALLOWED_ADDITIONS
        if not any(path.startswith(prefix) for prefix in _ALLOWED_ADDITION_PREFIXES)
    )
    if unexpected:
        raise CanonicalMigrationError(
            "CANONICAL_MIGRATION_UNEXPECTED_DESTINATION_AUTHORITY_FILES:" + ",".join(unexpected)
        )

    source_tree_sha256 = _tree_digest(source_entries)
    destination_tree_sha256 = _tree_digest(destination_entries)
    if route_receipt is None and source_tree_sha256 != destination_tree_sha256:
        raise CanonicalMigrationError("CANONICAL_MIGRATION_TREE_HASH_MISMATCH")

    legacy_live_route_count = 0
    noncanonical_output_route_count = 0
    sqlite_integrity = "not_checked"
    current_workspace_database_sha256 = ""
    if route_receipt is not None:
        database = target_root / "workspace.sqlite"
        current_workspace_database_sha256 = _sha256(database)
        connection = sqlite3.connect(database)
        try:
            sqlite_integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            legacy_live_route_count = _legacy_live_route_count(connection, source_root)
            for table, column in (
                ("brain_project", "output_dir"),
                ("brain_last_state_pointer", "output_folder"),
            ):
                if column not in _table_columns(connection, table):
                    continue
                for (value,) in connection.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                ):
                    if _mapped_canonical_path(value, target_root, target_root) is None:
                        noncanonical_output_route_count += 1
        finally:
            connection.close()
        if sqlite_integrity.casefold() != "ok":
            raise CanonicalMigrationError(
                f"CANONICAL_MIGRATION_WORKSPACE_INTEGRITY_FAILED:{sqlite_integrity}"
            )
        if legacy_live_route_count:
            raise CanonicalMigrationError(
                f"CANONICAL_MIGRATION_LEGACY_LIVE_ROUTES_REMAIN:{legacy_live_route_count}"
            )
        if noncanonical_output_route_count:
            raise CanonicalMigrationError(
                "CANONICAL_MIGRATION_NONCANONICAL_OUTPUT_ROUTES_REMAIN:"
                + str(noncanonical_output_route_count)
            )
        catalog_cache = target_root / "config" / "brain-catalog-workspaces.json"
        if catalog_cache.is_file():
            try:
                cached_catalog = json.loads(catalog_cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CanonicalMigrationError(
                    "CANONICAL_MIGRATION_CATALOG_CACHE_INVALID"
                ) from exc
            cached_legacy_routes = _json_legacy_route_count(cached_catalog, source_root)
            if cached_legacy_routes:
                raise CanonicalMigrationError(
                    "CANONICAL_MIGRATION_CATALOG_CACHE_LEGACY_ROUTES_REMAIN:"
                    + str(cached_legacy_routes)
                )

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if (
        registry.get("schema") != WORKSPACE_ROOTS_SCHEMA
        or os.path.normcase(str(registry.get("active_root") or "")) != os.path.normcase(str(target_root))
        or [os.path.normcase(str(row.get("path") or "")) for row in registry.get("roots", [])]
        != [os.path.normcase(str(target_root))]
    ):
        raise CanonicalMigrationError("CANONICAL_MIGRATION_ROUTE_REGISTRY_INVALID")

    return {
        "schema": MIGRATION_SCHEMA,
        "status": (
            "PASS_CANONICAL_ROOT_MIGRATED_AND_ROUTED"
            if route_receipt is not None
            else "PASS_CANONICAL_ROOT_BYTE_PARITY_ONLY"
        ),
        "source_root": str(source_root),
        "target_root": str(target_root),
        "verified_file_count": len(expected_entries),
        "verified_bytes": sum(int(entry["size_bytes"]) for entry in expected_entries),
        "source_tree_sha256": source_tree_sha256,
        "destination_tree_sha256": destination_tree_sha256,
        "raw_copy_destination_tree_sha256": (
            str(raw_receipt.get("destination_tree_sha256") or "")
        ),
        "unexpected_destination_authority_files": unexpected,
        "rewritten_route_count": int((route_receipt or {}).get("rewritten_route_count") or 0),
        "legacy_live_route_count": legacy_live_route_count,
        "noncanonical_output_route_count": noncanonical_output_route_count,
        "sqlite_integrity_check": sqlite_integrity,
        "current_workspace_database_sha256": current_workspace_database_sha256,
        "initial_post_route_database_sha256": str(
            (route_receipt or {}).get("post_route_database_sha256") or ""
        ),
        "route_rewrite_receipt": str(route_receipt_path) if route_receipt is not None else "",
        "source_deleted": False,
        "legacy_route_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy-first Evidence Lane canonical-root migration")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--actor", default="T023_CANONICAL_MIGRATION")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    result = (
        plan_canonical_migration(args.source, args.target)
        if args.plan_only
        else execute_canonical_migration(args.source, args.target, actor=args.actor)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CanonicalMigrationError",
    "MIGRATION_MANIFEST",
    "MIGRATION_RECEIPT",
    "MIGRATION_SCHEMA",
    "ROUTE_REWRITE_RECEIPT",
    "execute_canonical_migration",
    "finalize_canonical_routes",
    "plan_canonical_migration",
    "verify_canonical_migration",
]
