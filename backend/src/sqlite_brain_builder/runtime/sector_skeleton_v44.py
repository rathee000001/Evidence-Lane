from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.universal_lane_registry import CANONICAL_LANE_IDS, get_lane

ALL_SECTOR_LANES = list(CANONICAL_LANE_IDS)

CODE_MMD_PREFIXES = {
    "local_code",
    "github_code",
    "project_master",
}


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


def init_router_if_missing(router: Path, brain_name: str):
    con = connect(router)
    con.executescript("""
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
    """)
    con.commit()
    con.close()


def create_empty_sector_db(db: Path, lane_key: str, lane_label: str):
    con = connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS sector_manifest(
        sector_id TEXT PRIMARY KEY,
        lane_key TEXT,
        lane_label TEXT,
        version TEXT,
        status TEXT,
        created_at TEXT,
        fill_policy TEXT
    );
    CREATE TABLE IF NOT EXISTS sector_pointer(
        pointer_id TEXT PRIMARY KEY,
        lane_key TEXT,
        sector_db_path TEXT,
        active_bool INTEGER,
        status TEXT,
        mmd_required INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS model_fill_queue(
        fill_id TEXT PRIMARY KEY,
        lane_key TEXT,
        expected_source_type TEXT,
        fill_status TEXT,
        user_command_required INTEGER,
        notes TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS generic_source(
        source_id TEXT PRIMARY KEY,
        display_name TEXT,
        path TEXT,
        source_type TEXT,
        source_hash TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS generic_chunk(
        chunk_id TEXT PRIMARY KEY,
        source_id TEXT,
        chunk_order INTEGER,
        chunk_text TEXT,
        chunk_sha256 TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS source_structure_signature(
        signature_id TEXT PRIMARY KEY,
        source_id TEXT,
        signature_json TEXT,
        structure_hash TEXT,
        created_at TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS generic_fts USING fts5(chunk_id, text);
    """)
    con.execute(
        "INSERT OR REPLACE INTO sector_manifest VALUES(?,?,?,?,?,?,?)",
        (
            f"sector_{lane_key}",
            lane_key,
            lane_label,
            "v001",
            "EMPTY_READY_FOR_MODEL_FILL",
            now(),
            "Public model may fill this sector only by explicit user command.",
        )
    )
    con.execute(
        "INSERT OR REPLACE INTO sector_pointer VALUES(?,?,?,?,?,?,?)",
        (
            f"pointer_{lane_key}",
            lane_key,
            str(db),
            1,
            "EMPTY_READY_FOR_MODEL_FILL",
            1 if lane_key in {"local_code", "github_code"} else 0,
            now(),
        )
    )
    con.execute(
        "INSERT OR REPLACE INTO model_fill_queue VALUES(?,?,?,?,?,?,?)",
        (
            f"fill_{lane_key}",
            lane_key,
            lane_label,
            "WAITING_FOR_USER_COMMAND",
            1,
            "Sector exists so public model can fill structured DB later instead of inventing schema.",
            now(),
        )
    )
    con.commit()
    con.close()


def ensure_all_sector_skeletons(workspace_dir: str, brain_name: str):
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

    pointer_index = []

    rcon = connect(router)

    for lane_key in ALL_SECTOR_LANES:
        lane = get_lane(lane_key)
        lane_label = lane.get("label", lane_key)

        sector_dir = sectors_root / lane_key
        sector_dir.mkdir(parents=True, exist_ok=True)
        db = sector_dir / f"{lane_key}_sector_v001.sqlite"

        if not db.exists():
            create_empty_sector_db(db, lane_key, lane_label)

        status = "READY_WITH_DATA" if db.stat().st_size > 32768 else "EMPTY_READY_FOR_MODEL_FILL"
        mmd_required = lane_key in {"local_code", "github_code"}

        pointer = {
            "lane_key": lane_key,
            "lane_label": lane_label,
            "sector_db": f"project/sectors/{lane_key}/{lane_key}_sector_v001.sqlite",
            "active": True,
            "status": status,
            "mmd_required": mmd_required,
            "public_model_fill_policy": "fillable only by explicit user command",
            "locked_env_uop_project_template_policy": "Env/UOP/project_template_locked are read-only reference law.",
            "created_at": now(),
        }

        pointer_file = pointers_root / f"{lane_key}_pointer.json"
        pointer_file.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
        pointer_index.append(pointer)

        rcon.execute(
            "INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)",
            (
                f"sector_{lane_key}",
                lane_key,
                lane_label,
                str(db),
                1,
                "v001",
                sha256_file(db),
                now(),
            )
        )

    rcon.commit()
    rcon.close()

    (project_root / "sector_index.json").write_text(json.dumps(pointer_index, indent=2), encoding="utf-8")
    (project_root / "project_pointer.json").write_text(
        json.dumps(
            {
                "project_root": "project/",
                "router": "project/project_router.sqlite",
                "pointers": "project/pointers/",
                "sectors": "project/sectors/",
                "sector_count": len(pointer_index),
                "policy": "all predefined sector DBs exist; empty sectors are fillable later by explicit user command",
                "mmd_policy": "MMD generated only for code/workflow sectors; no document/chat/discussion MMD.",
                "created_at": now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cleanup_non_code_mmds(topology_root)

    return {
        "brain_root": str(brain_root),
        "project_root": str(project_root),
        "sector_count": len(pointer_index),
    }


def cleanup_non_code_mmds(topology_root: Path):
    if not topology_root.exists():
        return

    for path in topology_root.glob("*.mmd"):
        stem = path.stem
        if stem.startswith("project_master"):
            continue
        if stem.startswith("local_code") or stem.startswith("github"):
            continue
        try:
            path.unlink()
        except Exception:
            pass

    for path in list(topology_root.glob("*.svg")) + list(topology_root.glob("*.png")):
        stem = path.stem
        if stem.startswith("project_master"):
            continue
        if stem.startswith("local_code") or stem.startswith("github"):
            continue
        try:
            path.unlink()
        except Exception:
            pass
