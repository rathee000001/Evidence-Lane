from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlite_brain_builder.core import now, slugify
from sqlite_brain_builder.runtime.env15_project_schema import ENV15_LANE_TO_SECTOR
from sqlite_brain_builder.workspace.workspace_db import init_workspace


def _stable_id(kind: str, *parts: Any) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8", "surrogatepass")
    return f"{kind}_{hashlib.sha256(payload).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_source(source: Mapping[str, Any]) -> str:
    supplied = str(source.get("source_hash") or source.get("sha256") or "").strip()
    if supplied:
        return supplied
    path_text = str(source.get("path") or "").strip()
    path = Path(path_text) if path_text else None
    if path and path.is_file():
        return _sha256_file(path)
    digest = hashlib.sha256()
    if path and path.is_dir():
        files = sorted(
            (candidate for candidate in path.rglob("*") if candidate.is_file()),
            key=lambda value: value.relative_to(path).as_posix().casefold(),
        )
        for item in files:
            relative = item.relative_to(path).as_posix()
            digest.update(relative.encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
            digest.update(_sha256_file(item).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()
    digest.update(repr(sorted((str(key), str(value)) for key, value in source.items())).encode("utf-8", "surrogatepass"))
    return digest.hexdigest()


def _registered_sectors(brain_root: Path) -> list[tuple[str, Path]]:
    router_path = brain_root / "project" / "project_router.sqlite"
    if not router_path.is_file():
        raise FileNotFoundError(f"PROJECT_ROUTER_MISSING:{router_path}")
    with closing(
        sqlite3.connect(f"file:{router_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sector_registry)")}
        path_column = "sqlite_path" if "sqlite_path" in columns else "sector_db_path"
        rows = connection.execute(
            f"SELECT sector_id,{path_column} FROM sector_registry ORDER BY sector_id"
        ).fetchall()
    resolved = []
    for sector_id, relative in rows:
        database = Path(str(relative))
        if not database.is_absolute():
            database = brain_root / database
        resolved.append((str(sector_id), database.resolve()))
    return resolved


def _brain_identity(connection: sqlite3.Connection, brain_name: str, brain_root: Path, stamp: str) -> str:
    row = connection.execute(
        "SELECT brain_id FROM brain_project WHERE brain_name=? ORDER BY updated_at DESC LIMIT 1",
        (brain_name,),
    ).fetchone()
    brain_id = str(row[0]) if row else _stable_id("brain", brain_name, brain_root)
    connection.execute(
        "INSERT INTO brain_project(brain_id,brain_name,brain_slug,output_dir,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(brain_id) DO UPDATE SET "
        "brain_name=excluded.brain_name,brain_slug=excluded.brain_slug,output_dir=excluded.output_dir,"
        "status=excluded.status,updated_at=excluded.updated_at",
        (brain_id, brain_name, slugify(brain_name), str(brain_root), "ACTIVE", stamp, stamp),
    )
    return brain_id


def _register_sector(
    connection: sqlite3.Connection,
    *,
    brain_id: str,
    sector_name: str,
    database: Path,
    stamp: str,
) -> tuple[str, str, str]:
    if not database.is_file():
        raise FileNotFoundError(f"SECTOR_SQLITE_MISSING:{sector_name}:{database}")
    sector_hash = _sha256_file(database)
    sector_id = _stable_id("brain_sector", brain_id, sector_name)
    version_id = _stable_id("brain_sector_version", sector_id, sector_hash)
    existing = connection.execute(
        "SELECT version_no FROM brain_sector_version WHERE version_id=?", (version_id,)
    ).fetchone()
    version_no = int(existing[0]) if existing else int(connection.execute(
        "SELECT COALESCE(MAX(version_no),0)+1 FROM brain_sector_version WHERE sector_id=?", (sector_id,)
    ).fetchone()[0])
    connection.execute(
        "INSERT OR REPLACE INTO brain_sector(sector_id,brain_id,sector_name,behavior_type,created_at) VALUES(?,?,?,?,?)",
        (sector_id, brain_id, sector_name, "ENV15_GOVERNED_SQLITE", stamp),
    )
    connection.execute(
        "INSERT OR REPLACE INTO brain_sector_version(version_id,sector_id,version_no,db_path,status,hash_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (version_id, sector_id, version_no, str(database), "ACTIVE_VALIDATED", sector_hash, stamp),
    )
    connection.execute(
        "INSERT OR REPLACE INTO sector_pointer(sector_id,active_version_id,next_pointer,updated_at) VALUES(?,?,?,?)",
        (sector_id, version_id, "REFRESH_RESCAN_OR_EXPLICIT_GOVERNED_MUTATION", stamp),
    )
    connection.execute(
        "INSERT INTO sector_hash_state(sector_id,version_id,hash_sha256,created_at) "
        "SELECT ?,?,?,? WHERE NOT EXISTS(SELECT 1 FROM sector_hash_state WHERE sector_id=? AND version_id=? AND hash_sha256=?)",
        (sector_id, version_id, sector_hash, stamp, sector_id, version_id, sector_hash),
    )
    connection.execute(
        "INSERT OR REPLACE INTO active_brain_view(brain_id,sector_id,active_sector_version_id,active_source_filter_hash,created_at) "
        "VALUES(?,?,?,?,?)",
        (brain_id, sector_id, version_id, "ALL_ACTIVE_SOURCES", stamp),
    )
    return sector_id, version_id, sector_hash


def record_workspace_build_truth(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    request_id: str,
    sources: Iterable[Mapping[str, Any]],
    brain_root: str | Path,
    package_paths: Iterable[str | Path],
    mmd_paths: Iterable[str | Path],
    build_receipt: str | Path,
) -> dict[str, Any]:
    """Materialize a validated build into the persistent workspace ledgers."""

    workspace = Path(workspace_dir).resolve()
    root = Path(brain_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"BRAIN_ROOT_MISSING:{root}")
    workspace_db = init_workspace(workspace)
    stamp = now()
    source_rows = [dict(source) for source in sources]
    package_files = [Path(path).resolve() for path in package_paths if Path(path).is_file()]
    mmd_files = [Path(path).resolve() for path in mmd_paths if Path(path).is_file()]

    connection = sqlite3.connect(workspace_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        brain_id = _brain_identity(connection, brain_name, root, stamp)
        sector_ids: dict[str, str] = {}
        sector_versions: dict[str, str] = {}
        for sector_name, database in _registered_sectors(root):
            sector_id, version_id, _sector_hash = _register_sector(
                connection, brain_id=brain_id, sector_name=sector_name, database=database, stamp=stamp
            )
            sector_ids[sector_name] = sector_id
            sector_versions[sector_name] = version_id

        last_source_event: str | None = None
        for ordinal, source in enumerate(source_rows):
            lane = str(source.get("lane_key") or source.get("lane") or "custom")
            source_id = str(source.get("source_id") or _stable_id("source", brain_id, lane, source.get("path"), ordinal))
            source_hash = _sha256_source(source)
            active = bool(source.get("active", True))
            sector_name = ENV15_LANE_TO_SECTOR.get(lane, "custom")
            sector_id = sector_ids.get(sector_name) or _stable_id("brain_sector", brain_id, sector_name)
            event_id = _stable_id("brain_source_event", brain_id, request_id, source_id, source_hash, active)
            connection.execute(
                "INSERT OR REPLACE INTO brain_source_event(event_id,brain_id,lane,source_path,source_hash,status,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (event_id, brain_id, lane, str(source.get("path") or ""), source_hash, "ACTIVE" if active else "INACTIVE", stamp),
            )
            state_id = _stable_id("source_active_state", brain_id, source_id, source_hash)
            connection.execute(
                "INSERT OR REPLACE INTO source_active_state(active_state_id,brain_id,sector_id,source_id,source_hash,active_bool,state,reason,changed_by,changed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (state_id, brain_id, sector_id, source_id, source_hash, int(active), "ACTIVE" if active else "INACTIVE", "VALIDATED_BUILD_SOURCE_STATE", "brain.buildAll", stamp),
            )
            last_source_event = event_id

        for path in mmd_files:
            name = path.name.casefold()
            scope = "governance_env" if name.startswith("env_") else "governance_uop" if name.startswith("uop_") else "project_topology"
            scope_sector = sector_ids.get(scope)
            if not scope_sector:
                scope_sector = _stable_id("brain_sector", brain_id, scope)
                connection.execute(
                    "INSERT OR REPLACE INTO brain_sector(sector_id,brain_id,sector_name,behavior_type,created_at) VALUES(?,?,?,?,?)",
                    (scope_sector, brain_id, scope, "GOVERNANCE_MMD", stamp),
                )
                sector_ids[scope] = scope_sector
            mmd_hash = _sha256_file(path)
            mmd_id = _stable_id("sector_mmd", scope_sector, path, mmd_hash)
            connection.execute(
                "INSERT OR REPLACE INTO sector_mmd_registry(mmd_id,sector_id,mmd_path,mmd_hash,created_at) VALUES(?,?,?,?,?)",
                (mmd_id, scope_sector, str(path), mmd_hash, stamp),
            )

        last_package_id: str | None = None
        for package in package_files:
            package_hash = _sha256_file(package)
            package_id = _stable_id("brain_package_event", brain_id, request_id, package, package_hash)
            connection.execute(
                "INSERT OR REPLACE INTO brain_package_event(event_id,brain_id,package_path,status,created_at) VALUES(?,?,?,?,?)",
                (package_id, brain_id, str(package), "VALIDATED_READY", stamp),
            )
            last_package_id = package_id

        build_id = _stable_id("brain_build_event", brain_id, request_id)
        connection.execute(
            "INSERT OR REPLACE INTO brain_build_event(event_id,brain_id,status,receipt_path,created_at) VALUES(?,?,?,?,?)",
            (build_id, brain_id, "BUILD_VALIDATED_PACKAGES_READY", str(Path(build_receipt).resolve()), stamp),
        )
        connection.execute("INSERT OR REPLACE INTO recent_brain_index(brain_id,last_opened_at) VALUES(?,?)", (brain_id, stamp))
        connection.execute(
            "INSERT INTO brain_last_state_pointer(brain_id,last_source_event,last_build_id,last_package_id,output_folder,next_suggested_action,last_known_status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(brain_id) DO UPDATE SET "
            "last_source_event=excluded.last_source_event,last_build_id=excluded.last_build_id,last_package_id=excluded.last_package_id,"
            "output_folder=excluded.output_folder,next_suggested_action=excluded.next_suggested_action,"
            "last_known_status=excluded.last_known_status,created_at=excluded.created_at",
            (brain_id, last_source_event, build_id, last_package_id, str(root), "INSPECT_STRESS_REPORT_OR_REFRESH_ACTIVE_PROJECT", "BUILD_VALIDATED_PACKAGES_READY", stamp),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {
        "contract": "WORKSPACE_BUILD_OPERATIONAL_TRUTH_V1",
        "status": "PASS",
        "brain_id": brain_id,
        "brain_root": str(root),
        "source_event_count": len(source_rows),
        "sector_count": len(sector_versions),
        "package_event_count": len(package_files),
        "mmd_registry_count": len(mmd_files),
        "last_build_id": build_id,
        "last_package_id": last_package_id,
    }


__all__ = ["record_workspace_build_truth"]
