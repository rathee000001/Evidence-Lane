from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.universal_lane_registry import LANE_REGISTRY, get_lane


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def create_schema_table(con, table: str):
    if table.endswith("_fts"):
        con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(entity_id, text)")
        return

    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table}(
            id TEXT PRIMARY KEY,
            source_id TEXT,
            name TEXT,
            path TEXT,
            value TEXT,
            metadata_json TEXT,
            created_at TEXT
        )
        """
    )


def ensure_sector_schema(db: Path, lane_key: str):
    lane = get_lane(lane_key)
    tables = lane.get("schema_tables", [])

    con = connect(db)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sector_manifest(
            sector_id TEXT PRIMARY KEY,
            lane_key TEXT,
            lane_label TEXT,
            version TEXT,
            status TEXT,
            schema_tables_json TEXT,
            fill_policy TEXT,
            created_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sector_pointer(
            pointer_id TEXT PRIMARY KEY,
            lane_key TEXT,
            sector_db_path TEXT,
            active_bool INTEGER,
            status TEXT,
            mmd_required INTEGER,
            created_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS model_fill_queue(
            fill_id TEXT PRIMARY KEY,
            lane_key TEXT,
            expected_source_type TEXT,
            fill_status TEXT,
            user_command_required INTEGER,
            notes TEXT,
            created_at TEXT
        )
        """
    )

    for table in tables:
        create_schema_table(con, table)

    con.execute(
        "INSERT OR REPLACE INTO sector_manifest VALUES(?,?,?,?,?,?,?,?)",
        (
            f"sector_{lane_key}",
            lane_key,
            lane["label"],
            "v001",
            "SCHEMA_READY",
            json.dumps(tables, indent=2),
            "Public model may fill/update this sector only by explicit user command.",
            now(),
        ),
    )
    con.execute(
        "INSERT OR REPLACE INTO sector_pointer VALUES(?,?,?,?,?,?,?)",
        (
            f"pointer_{lane_key}",
            lane_key,
            str(db),
            1,
            "SCHEMA_READY",
            1 if lane.get("mmd_required") else 0,
            now(),
        ),
    )
    con.execute(
        "INSERT OR REPLACE INTO model_fill_queue VALUES(?,?,?,?,?,?,?)",
        (
            f"fill_{lane_key}",
            lane_key,
            lane["label"],
            "WAITING_FOR_USER_COMMAND_OR_SOURCE_INTAKE",
            1,
            "Sector schema exists so model does not invent schema later.",
            now(),
        ),
    )

    con.commit()
    con.close()


def init_router_if_missing(router: Path, brain_name: str):
    con = connect(router)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS brain_manifest(
            brain_id TEXT PRIMARY KEY,
            brain_name TEXT,
            brain_slug TEXT,
            created_at TEXT,
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS sector_registry(
            sector_id TEXT PRIMARY KEY,
            lane_key TEXT,
            lane_label TEXT,
            sector_db_path TEXT,
            active_bool INTEGER,
            version TEXT,
            sector_hash TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS source_registry(
            source_id TEXT PRIMARY KEY,
            lane_key TEXT,
            lane_label TEXT,
            source_type TEXT,
            display_name TEXT,
            path TEXT,
            source_hash TEXT,
            active_bool INTEGER,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS package_manifest(
            package_id TEXT PRIMARY KEY,
            package_path TEXT,
            created_at TEXT,
            package_hash TEXT
        );
        """
    )
    con.commit()
    con.close()


def ensure_all_sector_schemas(workspace_dir: str, brain_name: str):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    project_root = brain_root / "project"
    sectors_root = project_root / "sectors"
    pointers_root = project_root / "pointers"
    topology_root = project_root / "topology"

    project_root.mkdir(parents=True, exist_ok=True)
    sectors_root.mkdir(parents=True, exist_ok=True)
    pointers_root.mkdir(parents=True, exist_ok=True)
    topology_root.mkdir(parents=True, exist_ok=True)

    router = project_root / "project_router.sqlite"
    init_router_if_missing(router, brain_name)

    index = []
    rcon = connect(router)

    for lane_key, lane in LANE_REGISTRY.items():
        db = sectors_root / lane_key / f"{lane_key}_sector_v001.sqlite"
        ensure_sector_schema(db, lane_key)

        pointer = {
            "lane_key": lane_key,
            "lane_label": lane["label"],
            "sector_db": f"project/sectors/{lane_key}/{lane_key}_sector_v001.sqlite",
            "schema_tables": lane.get("schema_tables", []),
            "active": True,
            "mmd_required": bool(lane.get("mmd_required")),
            "status": "SCHEMA_READY",
            "fill_policy": "fillable/updateable only by explicit user command",
            "created_at": now(),
        }

        (pointers_root / f"{lane_key}_pointer.json").write_text(json.dumps(pointer, indent=2), encoding="utf-8")
        index.append(pointer)

        rcon.execute(
            "INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)",
            (
                f"sector_{lane_key}",
                lane_key,
                lane["label"],
                str(db),
                1,
                "v001",
                sha256_file(db),
                now(),
            ),
        )

    rcon.commit()
    rcon.close()

    (project_root / "sector_index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    (project_root / "project_pointer.json").write_text(
        json.dumps(
            {
                "router": "project/project_router.sqlite",
                "sectors": "project/sectors/",
                "pointers": "project/pointers/",
                "sector_count": len(index),
                "non_code_mmd_policy": "no MMD for non-code lanes",
                "code_mmd_policy": "route/page workflow only from code SQLite DB",
                "created_at": now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cleanup_non_code_mmds(topology_root)

    return {
        "brain_root": str(brain_root),
        "sector_count": len(index),
    }


def cleanup_non_code_mmds(topology: Path):
    if not topology.exists():
        return
    keep = {"local_code_lane", "project_master_topology"}
    for file in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        stem = file.stem
        for suffix in ["_CRYSTAL", "_4K", "_HD", "_MEGA"]:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem not in keep:
            try:
                file.unlink()
            except Exception:
                pass
