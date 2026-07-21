from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir, slugify_name


EXACT10 = [
    "GEMINI_FLASH_PROMPT.txt",
    "UEPC_POINTERS_AND_LOCKS.json",
    "PUBLIC_ENV_UOP_PROJECT_LOCKED.zip",
    "GENERATED_PROJECT_CONJOINED.sqlite",
    "PROJECT_TOPOLOGY.mmd",
    "PROJECT_TOPOLOGY.svg",
    "PROJECT_TOPOLOGY.png",
    "MANIFESTS_RECEIPTS_RECOVERY.zip",
    "ACTIVE_CONTEXT_INDEX.json",
    "FULL_PAYLOAD_CLOSED_DATALOOP_RENAME_TO_ZIP.bin",
]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def emit(progress, stage, task, file="", percent=0):
    if progress:
        progress({
            "stage": stage,
            "task": task,
            "file": str(file),
            "percent": int(percent),
            "done": int(percent),
            "total": 100,
            "elapsed_seconds": 0,
            "eta_seconds": "--",
            "finish_epoch": "--",
        })


def find_latest_normal_package_zip(workspace_dir: str, brain_name: str) -> Path:
    brain_root = brain_output_dir(workspace_dir, brain_name)
    packages = brain_root / "packages"
    slug = slugify_name(brain_name)

    candidates = []
    if packages.exists():
        candidates.extend(packages.glob(f"{slug}_one_upload_package_v*.zip"))
        candidates.extend(packages.glob(f"{slug.lower()}_one_upload_package_v*.zip"))
        candidates.extend(packages.glob("*one_upload_package_v*.zip"))
        candidates.extend(packages.glob("*one_upload_package*.zip"))

    candidates = [
        p for p in candidates
        if p.is_file()
        and "gemini" not in p.name.lower()
        and "closed_dataloop" not in p.name.lower()
    ]

    if not candidates:
        raise RuntimeError(
            "NORMAL_ONE_UPLOAD_ZIP_NOT_FOUND - run normal Export One-Upload Package first, then Gemini Exact 10 Export"
        )

    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def read_zip_member(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return z.read(name)
    except KeyError:
        return None


def first_existing_member(z: zipfile.ZipFile, names: list[str]) -> tuple[str, bytes] | tuple[None, None]:
    existing = set(z.namelist())
    for n in names:
        if n in existing:
            return n, z.read(n)
    return None, None


def add_public_locked_zip(source_zip: Path, out_zip: zipfile.ZipFile):
    with zipfile.ZipFile(source_zip, "r") as z:
        allowed_prefixes = (
            "env/",
            "uop/",
            "project_template_locked/",
        )
        allowed_exact = {
            ".uepc_env",
            ".uepc_profile",
            ".uepc_project",
            "project/project_template.sqlite",
            "project/project_template_footprint.md",
            "project/project_topology_template.mmd",
            "project/project_topology_template.svg",
            "project/project_topology_template.png",
        }

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        tmp.close()
        tmp_path = Path(tmp.name)

        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as locked:
            for name in z.namelist():
                low = name.lower()
                if name in allowed_exact or low.startswith(allowed_prefixes):
                    locked.writestr(name, z.read(name))

        out_zip.write(tmp_path, "PUBLIC_ENV_UOP_PROJECT_LOCKED.zip")
        tmp_path.unlink(missing_ok=True)


def make_pointers_json(source_zip: Path) -> bytes:
    payload = {
        "created_at": now(),
        "mode": "GEMINI_EXACT10_POINTER_FIRST",
        "normal_source_zip": str(source_zip),
        "rules": {
            "outer_zip_file_count": 10,
            "full_payload_is_closed_bin_not_nested_zip": True,
            "rename_closed_payload_to_zip_only_outside_gemini": True,
            "env_uop_project_template_locked": True,
            "generated_project_conjoined_db": True,
        },
        "read_order": EXACT10,
        "locks": {},
        "pointer_files": {},
    }

    with zipfile.ZipFile(source_zip, "r") as z:
        for name in [".uepc_env", ".uepc_project", ".uepc_profile"]:
            data = read_zip_member(z, name)
            payload["locks"][name] = data.decode("utf-8", "replace") if data else None

        for name in z.namelist():
            if name.startswith("project/pointers/") and name.endswith(".json"):
                try:
                    payload["pointer_files"][name] = json.loads(z.read(name).decode("utf-8", "replace"))
                except Exception:
                    payload["pointer_files"][name] = z.read(name).decode("utf-8", "replace")

        for name in ["project/project_pointer.json", "project/sector_index.json"]:
            data = read_zip_member(z, name)
            if data:
                try:
                    payload["pointer_files"][name] = json.loads(data.decode("utf-8", "replace"))
                except Exception:
                    payload["pointer_files"][name] = data.decode("utf-8", "replace")

    return json.dumps(payload, indent=2).encode("utf-8")


def safe_table_name(entry: str, table: str) -> str:
    base = entry.replace("\\", "/")
    base = base.replace("project/sectors/", "sector__")
    base = base.replace("project/", "project__")
    base = base.replace("/", "__")
    base = re.sub(r"[^A-Za-z0-9_]+", "_", base)
    base = base.strip("_")
    t = re.sub(r"[^A-Za-z0-9_]+", "_", table).strip("_")
    name = f"{base}__{t}"
    return name[:120]


def sqlite_rows_as_json(src_db: Path, db_entry: str, out_con: sqlite3.Connection):
    src = sqlite3.connect(src_db)
    src.row_factory = sqlite3.Row
    cur = src.cursor()

    tables = [
        r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]

    out_con.execute(
        "INSERT INTO sqlite_source_index(entry_path, sha256, size_bytes, table_count, created_at) VALUES(?,?,?,?,?)",
        (db_entry, sha256_file(src_db), src_db.stat().st_size, len(tables), now())
    )

    for table in tables:
        try:
            row_count = cur.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except Exception:
            row_count = -1

        out_con.execute(
            "INSERT INTO sqlite_table_inventory(entry_path, table_name, row_count) VALUES(?,?,?)",
            (db_entry, table, row_count)
        )

        if row_count <= 0:
            continue

        # Keep conjoined DB useful but not explosive:
        # - all metadata tables full
        # - code_line_snapshot full enough for proof but compact as JSON rows
        # - FTS shadow tables skipped
        if "_fts_" in table or table.endswith("_idx") or table.endswith("_data") or table.endswith("_docsize") or table.endswith("_config"):
            continue

        dest = safe_table_name(db_entry, table)
        out_con.execute(f'''
            CREATE TABLE IF NOT EXISTS "{dest}"(
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_db TEXT,
                source_table TEXT,
                row_json TEXT
            )
        ''')

        try:
            for row in cur.execute(f'SELECT * FROM "{table}"'):
                out_con.execute(
                    f'INSERT INTO "{dest}"(source_db, source_table, row_json) VALUES(?,?,?)',
                    (db_entry, table, json.dumps(dict(row), ensure_ascii=False, default=str))
                )
        except Exception as e:
            out_con.execute(
                'INSERT INTO conjoin_error(entry_path, table_name, error_text) VALUES(?,?,?)',
                (db_entry, table, str(e))
            )

    src.close()


def build_conjoined_project_db(source_zip: Path) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="gemini_conjoin_"))
    out_db = tmpdir / "GENERATED_PROJECT_CONJOINED.sqlite"

    out_con = sqlite3.connect(out_db)
    out_con.execute("PRAGMA journal_mode=DELETE")
    out_con.execute("PRAGMA synchronous=FULL")
    out_con.executescript("""
        CREATE TABLE sqlite_source_index(
            entry_path TEXT PRIMARY KEY,
            sha256 TEXT,
            size_bytes INTEGER,
            table_count INTEGER,
            created_at TEXT
        );
        CREATE TABLE sqlite_table_inventory(
            entry_path TEXT,
            table_name TEXT,
            row_count INTEGER
        );
        CREATE TABLE conjoin_error(
            entry_path TEXT,
            table_name TEXT,
            error_text TEXT
        );
        CREATE TABLE gemini_conjoined_manifest(
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    with zipfile.ZipFile(source_zip, "r") as z:
        sqlite_members = []
        for name in z.namelist():
            low = name.lower()
            if low == "project/project_router.sqlite" or (
                low.startswith("project/sectors/")
                and low.endswith(".sqlite")
                and not low.endswith("-wal")
                and not low.endswith("-shm")
            ):
                sqlite_members.append(name)

        for name in sqlite_members:
            data = z.read(name)
            temp_db = tmpdir / re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            temp_db.write_bytes(data)
            try:
                sqlite_rows_as_json(temp_db, name, out_con)
            finally:
                temp_db.unlink(missing_ok=True)

    out_con.execute(
        "INSERT OR REPLACE INTO gemini_conjoined_manifest VALUES(?,?)",
        ("created_at", now())
    )
    out_con.execute(
        "INSERT OR REPLACE INTO gemini_conjoined_manifest VALUES(?,?)",
        ("purpose", "Generated project router + sector DBs conjoined for Gemini exact-10 package")
    )
    out_con.commit()
    out_con.execute("VACUUM")
    out_con.close()

    return out_db


def build_manifests_receipts_zip(source_zip: Path) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    out = Path(tmp.name)

    with zipfile.ZipFile(source_zip, "r") as z, zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as mz:
        for name in z.namelist():
            low = name.lower()
            if (
                low.startswith("manifests/")
                or low.startswith("receipts/")
                or low.startswith("recovery/")
                or low == "readme_next_prompt.txt"
                or low == "flash_me_first_single_prompt.txt"
            ):
                mz.writestr(name, z.read(name))

    return out


def pick_topology(source_zip: Path, out_zip: zipfile.ZipFile):
    with zipfile.ZipFile(source_zip, "r") as z:
        mmd_name, mmd = first_existing_member(z, [
            "project/topology/local_code_lane.mmd",
            "project/topology/project_master_topology.mmd",
            "project/project_topology_template.mmd",
            "env/env_mmd.mmd",
        ])
        svg_name, svg = first_existing_member(z, [
            "project/topology/local_code_lane.svg",
            "project/topology/project_master_topology.svg",
            "project/project_topology_template.svg",
            "env/env_mmd.svg",
        ])
        png_name, png = first_existing_member(z, [
            "project/topology/local_code_lane_CRYSTAL.png",
            "project/topology/local_code_lane_4K.png",
            "project/topology/local_code_lane.png",
            "project/topology/project_master_topology_CRYSTAL.png",
            "project/project_topology_template.png",
            "env/env_mmd.png",
        ])

        out_zip.writestr("PROJECT_TOPOLOGY.mmd", mmd or b"flowchart TD\n  A[No topology found]\n")
        out_zip.writestr("PROJECT_TOPOLOGY.svg", svg or b"<svg xmlns='http://www.w3.org/2000/svg'><text x='10' y='20'>No SVG topology found</text></svg>")
        out_zip.writestr("PROJECT_TOPOLOGY.png", png or b"")


def make_gemini_flash_prompt() -> bytes:
    return b"""UEPC-GEMINI-EXACT10-BOOT-001 | Mode: pointer_first_validation | Category: exact-10-file Gemini package

CHAT_NAME:
[GOLDV3]

This is a Gemini-compatible exact-10 UEPC package.

Read only the 10 root files. Do not request recursive folder traversal.

READ ORDER:
1. GEMINI_FLASH_PROMPT.txt
2. UEPC_POINTERS_AND_LOCKS.json
3. PUBLIC_ENV_UOP_PROJECT_LOCKED.zip
4. GENERATED_PROJECT_CONJOINED.sqlite
5. PROJECT_TOPOLOGY.mmd
6. PROJECT_TOPOLOGY.svg
7. PROJECT_TOPOLOGY.png
8. MANIFESTS_RECEIPTS_RECOVERY.zip
9. ACTIVE_CONTEXT_INDEX.json
10. FULL_PAYLOAD_CLOSED_DATALOOP_RENAME_TO_ZIP.bin

FULL_PAYLOAD_CLOSED_DATALOOP_RENAME_TO_ZIP.bin is the original one-upload ZIP preserved as bytes.
Do not unpack it unless user explicitly asks. If needed outside Gemini, rename .bin to .zip.

Env/UOP/project-template law is locked/read-only.
Generated project conjoined DB is the active project brain view.
Chat window is display only.

Run compact entry slip:
package_seen=
exact10_file_count=
pointers_seen=
locked_public_payload_seen=
generated_project_db_seen=
topology_seen=
closed_payload_present=
blocker_if_any=
"""


def export_gemini_exact10(workspace_dir: str, brain_name: str, progress=None):
    emit(progress, "gemini", "finding latest normal one-upload zip", "", 5)
    source_zip = find_latest_normal_package_zip(workspace_dir, brain_name)

    brain_root = brain_output_dir(workspace_dir, brain_name)
    packages = brain_root / "packages"
    packages.mkdir(parents=True, exist_ok=True)

    slug = slugify_name(brain_name)
    out_zip = packages / f"{slug}_gemini_exact10_v001.zip"
    if out_zip.exists():
        out_zip.unlink()

    emit(progress, "gemini", "building conjoined generated project sqlite", source_zip, 25)
    conjoined_db = build_conjoined_project_db(source_zip)

    emit(progress, "gemini", "building manifests receipts recovery zip", source_zip, 45)
    mrr_zip = build_manifests_receipts_zip(source_zip)

    active_index = {
        "created_at": now(),
        "source_normal_one_upload_zip": str(source_zip),
        "exact_root_file_count": 10,
        "files": EXACT10,
        "closed_payload_note": "FULL_PAYLOAD_CLOSED_DATALOOP_RENAME_TO_ZIP.bin is the normal one-upload ZIP bytes. Rename to .zip outside Gemini only.",
        "normal_export_untouched": True,
        "gemini_export_only": True,
    }

    emit(progress, "gemini", "writing exact 10 root-file package", out_zip, 70)

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as oz:
        oz.writestr("GEMINI_FLASH_PROMPT.txt", make_gemini_flash_prompt())
        oz.writestr("UEPC_POINTERS_AND_LOCKS.json", make_pointers_json(source_zip))
        add_public_locked_zip(source_zip, oz)
        oz.write(conjoined_db, "GENERATED_PROJECT_CONJOINED.sqlite")
        pick_topology(source_zip, oz)
        oz.write(mrr_zip, "MANIFESTS_RECEIPTS_RECOVERY.zip")
        oz.writestr("ACTIVE_CONTEXT_INDEX.json", json.dumps(active_index, indent=2).encode("utf-8"))

        # Closed payload is intentionally .bin, not .zip, so Gemini does not recursively explode file count.
        oz.write(source_zip, "FULL_PAYLOAD_CLOSED_DATALOOP_RENAME_TO_ZIP.bin")

    conjoined_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(conjoined_db.parent, ignore_errors=True)
    mrr_zip.unlink(missing_ok=True)

    with zipfile.ZipFile(out_zip, "r") as z:
        names = z.namelist()
        if len(names) != 10:
            raise RuntimeError(f"GEMINI_EXACT10_FAILED: {len(names)} files: {names}")
        missing = [n for n in EXACT10 if n not in names]
        if missing:
            raise RuntimeError(f"GEMINI_EXACT10_MISSING_FILES: {missing}")

    emit(progress, "done", "Gemini exact-10 package ready", out_zip, 100)

    return {
        "gemini_package_zip": str(out_zip),
        "file_count": 10,
        "source_normal_one_upload_zip": str(source_zip),
        "sha256": sha256_file(out_zip),
        "status": "GEMINI_EXACT10_READY_BIN_CLOSED_PAYLOAD",
    }


def export_gemini_compatible_package(workspace_dir: str, brain_name: str, progress=None):
    return export_gemini_exact10(workspace_dir, brain_name, progress)
