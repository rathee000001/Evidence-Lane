
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from sqlite_brain_builder.runtime import stable_runtime_v53 as base
from sqlite_brain_builder.runtime.stable_runtime_v53 import *  # keep existing good logic available

APP_VERSION = "V5.5_TARGETED_LANE_REPAIR_GEMINI10_BRAIN_SWITCH"

FTS_SHADOW_PATTERNS = ("_fts_config", "_fts_content", "_fts_data", "_fts_docsize", "_fts_idx")


def _connect_delete(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=60)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _cleanup_wal_shm(db: Path):
    for suffix in ("-wal", "-shm"):
        p = Path(str(db) + suffix)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


def _vacuum_close(con: sqlite3.Connection, db: Path | None = None):
    try:
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    try:
        con.execute("VACUUM")
    except Exception:
        pass
    con.close()
    if db:
        _cleanup_wal_shm(db)


def _sha_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "ignore")).hexdigest()


def _table_exists(con, table: str) -> bool:
    return con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] > 0


def _row_count(con, table: str) -> int:
    if not _table_exists(con, table):
        return 0
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    except Exception:
        return 0


def _ginsert(con, table: str, source_id: str, name: str, path: str, value: str = "", metadata=None):
    if not _table_exists(con, table):
        return
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
    rid = table + "_" + hashlib.sha256((str(source_id)+str(name)+str(path)+str(value)+metadata_json).encode("utf-8", "ignore")).hexdigest()[:18]
    cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    # Generic V5.3 non-code tables share these columns. Keep safe for all.
    if {"id","source_id","name","path","value","metadata_json","created_at"}.issubset(set(cols)):
        con.execute(f'INSERT OR REPLACE INTO "{table}"(id,source_id,name,path,value,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)',
                    (rid, source_id, str(name), str(path), str(value), metadata_json, base.now()))
    elif "entity_id" in cols and "text" in cols:
        con.execute(f'INSERT INTO "{table}"(entity_id,text) VALUES(?,?)', (rid, str(value)))


def _fts_insert(con, table: str, entity_id: str, text: str):
    if _table_exists(con, table) and text:
        try:
            con.execute(f'INSERT INTO "{table}"(entity_id,text) VALUES(?,?)', (entity_id, text))
        except Exception:
            pass


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            pass
    return data.decode("utf-8", "replace")


# -------------------- Code lane postprocess --------------------

def _parse_package_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    deps = []
    for section in ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]:
        obj = data.get(section) or {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                deps.append((k, str(v), "node", section))
    return deps


def _parse_requirements(path: Path):
    deps=[]
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            s=line.strip()
            if not s or s.startswith("#") or s.startswith("-"):
                continue
            m=re.match(r"([A-Za-z0-9_.-]+)\s*([<>=!~].*)?", s)
            if m:
                deps.append((m.group(1), m.group(2) or "", "python", "runtime"))
    except Exception:
        pass
    return deps


def _parse_pyproject(path: Path):
    deps=[]
    text=_read_text(path)
    # simple non-toml dependency extraction fallback; keeps app dependency-light
    for m in re.finditer(r'"([A-Za-z0-9_.-]+)([<>=!~][^"\]]*)?"', text):
        name=m.group(1)
        if len(name) > 1 and name.lower() not in {"version", "name", "description"}:
            deps.append((name, m.group(2) or "", "python", "pyproject"))
    return deps[:200]


def _manifest_file_id(con, rel: str):
    row = con.execute("SELECT file_id,current_sha256 FROM code_file WHERE canonical_path=?", (rel,)).fetchone() if _table_exists(con,"code_file") else None
    if row:
        return row[0], row[1]
    row = con.execute("SELECT file_id,sha256 FROM source_file WHERE logical_path=?", (rel,)).fetchone() if _table_exists(con,"source_file") else None
    if row:
        return row[0], row[1]
    digest = _sha_text(rel)
    fid = "file_" + digest[:16]
    return fid, digest


def postprocess_code_sector(db: Path, source: dict, project_root: Path):
    if not db.exists():
        return
    folder = Path(source.get("path") or "")
    con = _connect_delete(db)
    try:
        base.create_code_schema(con)
    except Exception:
        pass

    # Fill dependency_manifest/dependency_item from real manifests. V5.3 treated JSON as read-only artifact, so package.json could miss dependency tables.
    if folder.exists():
        manifests=[]
        for pat in ["package.json", "requirements.txt", "pyproject.toml"]:
            manifests.extend([p for p in folder.rglob(pat) if p.is_file() and not base.is_ignored(p.relative_to(folder))])
        for mf in manifests:
            rel = mf.relative_to(folder).as_posix()
            fid, digest = _manifest_file_id(con, rel)
            if mf.name == "package.json":
                items = _parse_package_json(mf); typ="package.json"; eco="node"
            elif mf.name == "requirements.txt":
                items = _parse_requirements(mf); typ="requirements.txt"; eco="python"
            else:
                items = _parse_pyproject(mf); typ="pyproject.toml"; eco="python"
            mid = "manifest_" + hashlib.sha256((rel+digest).encode()).hexdigest()[:16]
            if _table_exists(con,"dependency_manifest"):
                con.execute("INSERT OR REPLACE INTO dependency_manifest VALUES(?,?,?,?,?,?)", (mid, fid, typ, eco, rel, digest))
            if _table_exists(con,"dependency_item"):
                for name, ver, ecosystem, scope in items:
                    did = "dep_" + hashlib.sha256((mid+name+ver+scope).encode()).hexdigest()[:16]
                    con.execute("INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?)", (did, mid, name, ver, ecosystem))

    # Deterministic code_dependency_edge rows from import edges + dependency rows.
    if _table_exists(con,"code_dependency_edge"):
        for edge_id, from_file_id, from_path, target, typ, line_no in con.execute("SELECT edge_id,from_file_id,from_path,import_target,import_type,line_number FROM code_import_edge"):
            _ginsert(con,"code_dependency_edge", from_file_id, f"{typ}:{target}", from_path, str(target), {"line_number": line_no, "import_edge_id": edge_id})
        for did, mid, pkg, ver, eco in con.execute("SELECT dependency_id,manifest_id,package_name,version_spec,ecosystem FROM dependency_item") if _table_exists(con,"dependency_item") else []:
            _ginsert(con,"code_dependency_edge", mid, f"manifest_uses:{pkg}", eco, ver, {"dependency_id": did})

    # Artifact relation edges were empty; fill deterministic BELONGS_TO_CODE_PROJECT edges.
    if _table_exists(con,"artifact_relation_edge") and _table_exists(con,"project_artifact"):
        for aid, sfid in con.execute("SELECT artifact_id,source_file_id FROM project_artifact"):
            eid = "artedge_" + hashlib.sha256((aid+str(sfid)).encode()).hexdigest()[:16]
            con.execute("INSERT OR REPLACE INTO artifact_relation_edge VALUES(?,?,?,?,?,?)", (eid, aid, "code_file", sfid or "code_project", "BELONGS_TO_CODE_PROJECT", "deterministic"))

    # Workflow nodes/edges used by MMD and project topology.
    if _table_exists(con,"workflow_node"):
        for rid, route_path, route_type, file_id, fvid, rsha in con.execute("SELECT route_id,route_path,route_type,file_id,file_version_id,route_sha256 FROM app_route") if _table_exists(con,"app_route") else []:
            _ginsert(con,"workflow_node", rid, "ROUTE", route_path, route_type, {"file_id": file_id})
        for did, mid, pkg, ver, eco in con.execute("SELECT dependency_id,manifest_id,package_name,version_spec,ecosystem FROM dependency_item") if _table_exists(con,"dependency_item") else []:
            _ginsert(con,"workflow_node", did, "DEPENDENCY", pkg, ver, {"ecosystem": eco, "manifest_id": mid})
        for aid, sfid, atype, sha, path, size, meta, status in con.execute("SELECT artifact_id,source_file_id,artifact_type,artifact_sha256,path,size_bytes,metadata_json,semantic_status FROM project_artifact") if _table_exists(con,"project_artifact") else []:
            _ginsert(con,"workflow_node", aid, "ARTIFACT", path, atype, {"size": size, "status": status})
    if _table_exists(con,"workflow_edge"):
        for rid, route_path, route_type, file_id, fvid, rsha in con.execute("SELECT route_id,route_path,route_type,file_id,file_version_id,route_sha256 FROM app_route") if _table_exists(con,"app_route") else []:
            _ginsert(con,"workflow_edge", rid, "ROUTE_TO_FILE", route_path, file_id, {"from":"route", "to":"code_file"})
        for edge_id, from_file_id, from_path, target, typ, line_no in con.execute("SELECT edge_id,from_file_id,from_path,import_target,import_type,line_number FROM code_import_edge") if _table_exists(con,"code_import_edge") else []:
            _ginsert(con,"workflow_edge", edge_id, "FILE_IMPORTS", from_path, target, {"line": line_no, "type": typ})
        for aid, sfid in con.execute("SELECT artifact_id,source_file_id FROM project_artifact") if _table_exists(con,"project_artifact") else []:
            _ginsert(con,"workflow_edge", aid, "FILE_TO_ARTIFACT", sfid or "code_project", aid, {})

    _vacuum_close(con, db)


# -------------------- Lane postprocess fixes --------------------

def postprocess_data_sector(db: Path, source: dict):
    con=_connect_delete(db)
    try:
        sid=source.get("source_id") or "source_data"
        path=source.get("path") or source.get("display_name") or ""
        # Fill ranges/tables from sheet tabs when openpyxl extraction populated tabs/cells/formulas.
        if _table_exists(con,"sheet_tab"):
            tabs = con.execute("SELECT id, name, path, value, metadata_json FROM sheet_tab").fetchall()
            for tid,name,pathv,value,meta in tabs:
                meta_obj={}
                try: meta_obj=json.loads(meta or "{}")
                except Exception: pass
                # V5.3 metadata often has rows/cols or max_row/max_column; keep generic if missing.
                rows = meta_obj.get("rows") or meta_obj.get("max_row") or meta_obj.get("row_count") or "unknown"
                cols = meta_obj.get("cols") or meta_obj.get("max_column") or meta_obj.get("col_count") or "unknown"
                _ginsert(con,"sheet_range", sid, f"{name}_used_range", pathv or path, f"rows={rows}; cols={cols}", {"source_sheet_tab_id": tid})
                _ginsert(con,"sheet_table", sid, f"{name}_sheet_table", pathv or path, f"used_range rows={rows} cols={cols}", {"table_type":"USED_RANGE_TABLE", "source_sheet_tab_id": tid})
                if _row_count(con,"sheet_chart_metadata") == 0:
                    _ginsert(con,"sheet_chart_metadata", sid, f"{name}_chart_scan", pathv or path, "NO_CHARTS_DETECTED_OR_PARSER_UNAVAILABLE", {"source_sheet_tab_id": tid})
        # Formula dependency edge extraction from formula text.
        if _table_exists(con,"sheet_formula") and _table_exists(con,"sheet_formula_dependency_edge"):
            for fid, source_id, name, pathv, val, meta, created in con.execute("SELECT id,source_id,name,path,value,metadata_json,created_at FROM sheet_formula"):
                formula=str(val or "")
                refs=sorted(set(re.findall(r"(?:'[^']+'|[A-Za-z0-9_ ]+!)?\$?[A-Z]{1,3}\$?\d+", formula)))[:40]
                for ref in refs:
                    _ginsert(con,"sheet_formula_dependency_edge", source_id or sid, f"{name}->{ref}", pathv or path, ref, {"formula_id":fid, "formula": formula[:500]})
        # If CSV rows exist, ensure csv_header if missing can be inferred from first row metadata/value.
        if _row_count(con,"csv_row_sample") and _row_count(con,"csv_header") == 0:
            row = con.execute("SELECT value,metadata_json,path FROM csv_row_sample LIMIT 1").fetchone()
            _ginsert(con,"csv_header", sid, "csv_header_inferred", row[2] if row else path, "SEE_ROW_SAMPLE", {})
        if _row_count(con,"data_structure_signature") == 0:
            sig={t:_row_count(con,t) for t in ["sheet_workbook","sheet_tab","sheet_range","sheet_table","sheet_formula","csv_header","csv_row_sample","sheet_cell_sample"] if _table_exists(con,t)}
            _ginsert(con,"data_structure_signature", sid, "DATA_SIGNATURE", path, json.dumps(sig), sig)
        _fts_insert(con,"data_fts", sid, " ".join([str(x) for x in [source.get("display_name"), path]]))
    finally:
        _vacuum_close(con, db)


def _docx_text_blocks(path: Path):
    try:
        with zipfile.ZipFile(path) as z:
            xml=z.read("word/document.xml")
    except Exception:
        return []
    ns={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    root=ET.fromstring(xml)
    blocks=[]
    for p in root.findall(".//w:p", ns):
        texts=[t.text or "" for t in p.findall(".//w:t", ns)]
        txt="".join(texts).strip()
        if txt:
            blocks.append(txt)
    return blocks


def postprocess_docs_sector(db: Path, source: dict):
    con=_connect_delete(db)
    try:
        sid=source.get("source_id") or "source_docs"
        path=Path(source.get("path") or "")
        display=source.get("display_name") or path.name or "docs"
        if _row_count(con,"doc_file") == 0 and path.exists():
            ext=path.suffix.lower()
            if ext == ".docx":
                blocks=_docx_text_blocks(path)
            elif ext in {".txt", ".md", ".html", ".xml"}:
                blocks=[b for b in re.split(r"\n\s*\n", _read_text(path)) if b.strip()]
            else:
                blocks=[]
            digest=base.sha256_file(path) if path.exists() else _sha_text(display)
            _ginsert(con,"doc_file", sid, display, str(path), digest, {"paragraphs":len(blocks), "extension":ext})
            _ginsert(con,"doc_structure", sid, "DOCUMENT_STRUCTURE", str(path), f"paragraphs={len(blocks)}", {})
            for i,b in enumerate(blocks, start=1):
                _ginsert(con,"doc_paragraph", sid, f"paragraph_{i}", str(path), b)
                _fts_insert(con,"doc_fts", f"{sid}_p{i}", b)
            # Compact chunks of paragraphs.
            for ci in range(0, len(blocks), 8):
                chunk="\n\n".join(blocks[ci:ci+8])
                _ginsert(con,"doc_chunk", sid, f"doc_chunk_{ci//8+1}", str(path), chunk, {"start_paragraph":ci+1, "end_paragraph":min(len(blocks),ci+8)})
            _ginsert(con,"source_structure_signature", sid, "DOC_SIGNATURE", str(path), f"paragraphs={len(blocks)}", {"paragraphs":len(blocks)})
    finally:
        _vacuum_close(con, db)


def postprocess_ppt_sector(db: Path, source: dict):
    con=_connect_delete(db)
    try:
        sid=source.get("source_id") or "source_ppt"
        path=source.get("path") or ""
        # Make ppt_shape meaningful from text blocks when shape parser did not fill it.
        if _table_exists(con,"ppt_text_block") and _row_count(con,"ppt_shape") == 0:
            for rid,src,name,pathv,val,meta,created in con.execute("SELECT id,source_id,name,path,value,metadata_json,created_at FROM ppt_text_block"):
                _ginsert(con,"ppt_shape", src or sid, name.replace("text","shape"), pathv or path, "TEXT_BOX", {"text_block_id": rid, "text_preview": (val or "")[:120]})
        if _row_count(con,"ppt_notes") == 0 and _row_count(con,"ppt_file") > 0:
            _ginsert(con,"ppt_notes", sid, "notes_scan", path, "NO_NOTES_FOUND_OR_NOTES_PARSER_UNAVAILABLE", {})
    finally:
        _vacuum_close(con, db)


def postprocess_pdf_sector(db: Path, source: dict):
    con=_connect_delete(db)
    try:
        sid=source.get("source_id") or "source_pdf"
        path=source.get("path") or ""
        if _row_count(con,"pdf_ocr_run") == 0:
            status = "TEXT_EXTRACTION_USED_NO_OCR_REQUIRED" if _row_count(con,"pdf_text_block") else "OCR_REVIEW_REQUIRED_OR_PARSER_UNAVAILABLE"
            _ginsert(con,"pdf_ocr_run", sid, status, path, status, {"text_blocks":_row_count(con,"pdf_text_block"), "image_blocks":_row_count(con,"pdf_image_block")})
        if _row_count(con,"pdf_ocr_block") == 0 and _row_count(con,"pdf_text_block") > 0:
            # Mirror extracted text into OCR block table as deterministic text layer, not fake handwriting OCR.
            for rid,src,name,pathv,val,meta,created in con.execute("SELECT id,source_id,name,path,value,metadata_json,created_at FROM pdf_text_block"):
                _ginsert(con,"pdf_ocr_block", src or sid, name.replace("text","ocr_text_layer"), pathv or path, val, {"source_text_block_id": rid, "method":"TEXT_LAYER_MIRROR"})
                for j,line in enumerate(str(val or "").splitlines(), start=1):
                    if line.strip():
                        _ginsert(con,"pdf_ocr_line", src or sid, f"{name}_line_{j}", pathv or path, line, {"source_text_block_id": rid})
    finally:
        _vacuum_close(con, db)


def postprocess_image_sector(db: Path, source: dict):
    con=_connect_delete(db)
    try:
        sid=source.get("source_id") or "source_image"
        path=source.get("path") or ""
        if _row_count(con,"image_ocr_run") == 0:
            _ginsert(con,"image_ocr_run", sid, "OCR_REVIEW_REQUIRED_OR_PARSER_UNAVAILABLE", path, "", {})
    finally:
        _vacuum_close(con, db)


def postprocess_lane(db: Path, lane_key: str, source: dict):
    if lane_key == "data_excel_csv":
        postprocess_data_sector(db, source)
    elif lane_key == "docs":
        postprocess_docs_sector(db, source)
    elif lane_key == "ppt_presentation":
        postprocess_ppt_sector(db, source)
    elif lane_key == "pdf_ocr":
        postprocess_pdf_sector(db, source)
    elif lane_key == "images_ocr":
        postprocess_image_sector(db, source)
    else:
        # Ensure source_active_state exists for every selected non-code lane.
        if db.exists():
            con=_connect_delete(db)
            try:
                if _table_exists(con,"source_active_state"):
                    con.execute("INSERT OR REPLACE INTO source_active_state VALUES(?,?,?,?,?,?)", (source.get("source_id") or "source", 1, lane_key, source.get("display_name") or source.get("path") or lane_key, "ACTIVE_SELECTED_SOURCE", base.now()))
            finally:
                _vacuum_close(con, db)


# -------------------- Build brain wrapper --------------------

def build_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress=None):
    started = time.time()
    root = base.init_brain_layout(workspace_dir, brain_name)
    project = root / "project"
    router = project / "project_router.sqlite"
    active_sources = [s for s in sources if s.get("active", True)]
    total = max(1, len(active_sources))
    base.emit(progress, "build", "initializing V5.5 targeted lane build", str(root), 2, 0, total, started)
    for idx, source in enumerate(active_sources, start=1):
        lane_key = source.get("lane_key") or "custom"
        lane_key = "local_code" if lane_key == "code" else lane_key
        lane = base.LANE_DEFS.get(lane_key, base.LANE_DEFS["custom"])
        db = project / "sectors" / lane_key / f"{lane_key}_sector_v001.sqlite"
        base.insert_source_registry(router, source, lane_key)
        base.emit(progress, "build", f"building {lane['label']} sector", source.get("display_name") or source.get("path") or "", int(idx*70/total), idx, total, started)
        if lane_key in {"local_code", "github"}:
            base.build_code_sector(db, source, project, progress, lane_key, started)
            postprocess_code_sector(db, source, project)
        else:
            base.build_semantic_lane(db, lane_key, source, progress, started)
            postprocess_lane(db, lane_key, source)
        con = _connect_delete(router)
        try:
            con.execute("INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)", ("sector_"+lane_key, lane_key, lane["label"], str(db), 1, "v001", base.sha256_file(db), base.now()))
        finally:
            _vacuum_close(con, router)
    base.write_code_mmd(root)
    base.write_project_master_mmd(root)
    (root/"receipts").mkdir(parents=True, exist_ok=True)
    (root/"receipts"/"build_receipt.md").write_text(f"# Build Receipt\n\nstatus=PASS\nbrain={brain_name}\nbuilder={APP_VERSION}\nsources={len(active_sources)}\ncreated={base.now()}\n", encoding="utf-8")
    base.emit(progress, "done", "V5.5 targeted lane build complete", str(root), 100, total, total, started)
    return {"brain_root": str(root), "router": str(router)}


# Harden normal one-upload flash prompt without changing normal export structure.
def write_flash_prompt_v55(stage: Path, brain_name: str):
    (stage/"FLASH_ME_FIRST_SINGLE_PROMPT.txt").write_text(f"""UEPC-ENV15-AIRGAPPED-FLASH-BOOT-ZERO-GAP-001 | Mode: flash_env + runtime_apply + validation | Category: one-upload env flash + active chat behavior installation

CHAT_NAME:
[{brain_name}]

BRIEF_NATURE_OF_CHAT:
[SQLite Brain Builder package: locked Env/UOP/project-template law + writable generated project sector DBs]

ROOT COMMAND:
This is not only a validation task. This is an Env runtime installation task for this chat.
The uploaded ZIP is the only durable state container. The chat window is display/runtime only.
Open the uploaded ZIP first. Do not ask for individual files.

READ ORDER â€” mandatory before any answer:
1. FLASH_ME_FIRST_SINGLE_PROMPT.txt
2. .uepc_env
3. .uepc_project
4. .uepc_profile
5. env/env_law.md
6. env/env_sqlite.sqlite
7. uop/uop_law.md
8. uop/uop_sqlite.sqlite
9. project/project_template.sqlite OR project_template_locked/
10. project/project_router.sqlite
11. project/sector_index.json
12. project/pointers/
13. project/sectors/
14. project/topology/
15. project/artifacts/
16. manifests/
17. receipts/
18. recovery/relock_prompt.txt if present

ZERO-GAP RUNTIME LAW:
Every serious response must follow:
PROMPT_INDEX_FIRST -> ENTRY_SLIP -> PACKAGE_POINTER_READ -> GATE_FIRE_CHECK -> OPERATOR_FIRE_CHECK -> TASK_RESPONSE -> EXIT_SLIP

ROOT AUTHORITY LADDER:
1. Platform/system safety rules
2. Uploaded Env package law
3. UOP governance/operator law
4. Project router/template/sector truth
5. Current user command
6. Chat lineage/display memory
7. Model memory or assumptions

LOCK RULES:
Env and UOP are locked/read-only governance.
Public project template is locked/read-only reference law.
Generated project sector DBs are writable only by explicit user command.
If mutation is not explicitly requested, operate read-only.

MANDATORY SQLITE CHECKS:
Query env/env_sqlite.sqlite and uop/uop_sqlite.sqlite before task reasoning when available.
Query project/project_router.sqlite and project/sectors/*/*.sqlite for project truth.
Do not treat empty schema-ready sectors as populated evidence.

MMD/SVG/PNG TOPOLOGY PROOF:
When topology is relevant, verify MMD, SVG, PNG/HD-PNG, manifest/hash state, and treat topology as SQLite-backed route proof.

AIR-GAP RULE:
Do not continue from prior assistant answers unless package receipts/pointers confirm it.
Do not treat chat lineage as Env law.
Do not treat UOP as project truth.
Do not treat project truth as Env law.
Do not treat model memory as evidence.

VISIBLE TELEMETRY MINIMUM:
ENTRY SLIP | mode | active package pointer | gates fired | operators fired | write scope
EXIT SLIP | read/write status | package mutation yes/no | gates passed/failed | next pointer

START NOW:
Open the ZIP, read the required pointers, apply Env as active runtime behavior, run compact entry slip, validate gates/operators, and only then answer the user's task.
""", encoding="utf-8")

base.write_flash_prompt = write_flash_prompt_v55


# -------------------- Gemini exact10 v55 --------------------

EXACT10 = [
    "GEMINI_FLASH_PROMPT.txt",
    "UEPC_UNIVERSAL_POINTER.json",
    "UEPC_DOT_LOCKS_AND_READ_ORDER.txt",
    "PUBLIC_ENV_UOP_PROJECT_LOCKED.zip",
    "GENERATED_PROJECT_CONJOINED.sqlite",
    "PROJECT_GITLIKE_FILE_LEDGER.jsonl",
    "PROJECT_TOPOLOGY.mmd",
    "PROJECT_TOPOLOGY.svg",
    "PROJECT_TOPOLOGY.png",
    "MANIFEST_RECEIPTS_RECOVERY.json",
]


def _find_latest_normal_zip(root: Path, brain_name: str) -> Path:
    packages = root / "packages"
    if not packages.exists():
        raise RuntimeError("PACKAGES_FOLDER_NOT_FOUND - run normal Export One-Upload Package first")
    candidates = [p for p in packages.glob("*one_upload_package*.zip") if "gemini" not in p.name.lower()]
    if not candidates:
        raise RuntimeError("NORMAL_ONE_UPLOAD_ZIP_NOT_FOUND - run normal Export One-Upload Package first")
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def _copy_selected_locked_env(normal_zip: Path, out: zipfile.ZipFile):
    with zipfile.ZipFile(normal_zip, "r") as z:
        tmp = Path(tempfile.mkdtemp(prefix="gemini_locked_env_")) / "locked.zip"
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as locked:
            for name in z.namelist():
                low=name.lower()
                if low.startswith("env/") or low.startswith("uop/") or low.startswith("project_template_locked/") or name in {".uepc_env", ".uepc_project", ".uepc_profile", "project/project_template.sqlite", "project/project_topology_template.mmd", "project/project_topology_template.svg", "project/project_topology_template.png"}:
                    locked.writestr(name, z.read(name))
        out.write(tmp, "PUBLIC_ENV_UOP_PROJECT_LOCKED.zip")
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _zip_read_or_empty(normal_zip: Path, names: list[str]) -> bytes:
    with zipfile.ZipFile(normal_zip, "r") as z:
        existing=set(z.namelist())
        for n in names:
            if n in existing:
                return z.read(n)
    return b""


def _conjoin_from_normal_zip(normal_zip: Path) -> Path:
    tmpdir=Path(tempfile.mkdtemp(prefix="gemini_project_conjoin_"))
    out=tmpdir/"GENERATED_PROJECT_CONJOINED.sqlite"
    dest=sqlite3.connect(out)
    dest.execute("PRAGMA journal_mode=DELETE")
    dest.executescript("""
        CREATE TABLE universal_project_pointer(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE sqlite_source_index(entry_path TEXT PRIMARY KEY, sha256 TEXT, size_bytes INTEGER, table_count INTEGER, created_at TEXT);
        CREATE TABLE sqlite_table_inventory(entry_path TEXT, table_name TEXT, row_count INTEGER);
        CREATE TABLE project_gitlike_file_ledger(path TEXT, sha256 TEXT, size_bytes INTEGER, source_kind TEXT);
        CREATE TABLE conjoin_row(source_db TEXT, source_table TEXT, source_pk TEXT, row_json TEXT);
    """)
    with zipfile.ZipFile(normal_zip,"r") as z:
        members=[n for n in z.namelist() if (n == "project/project_router.sqlite" or (n.startswith("project/sectors/") and n.endswith(".sqlite") and not n.endswith("-wal") and not n.endswith("-shm")))]
        for name in members:
            data=z.read(name)
            sha=hashlib.sha256(data).hexdigest()
            tmpdb=tmpdir/(re.sub(r"[^A-Za-z0-9_.-]+","_",name))
            tmpdb.write_bytes(data)
            src=sqlite3.connect(tmpdb); src.row_factory=sqlite3.Row
            tables=[r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            dest.execute("INSERT OR REPLACE INTO sqlite_source_index VALUES(?,?,?,?,?)", (name,sha,len(data),len(tables),base.now()))
            for t in tables:
                if any(t.endswith(p) for p in FTS_SHADOW_PATTERNS):
                    continue
                try:
                    rc=src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    dest.execute("INSERT INTO sqlite_table_inventory VALUES(?,?,?)", (name,t,rc))
                    for row in src.execute(f'SELECT * FROM "{t}"'):
                        d=dict(row)
                        pk=d.get("id") or d.get("file_id") or d.get("route_id") or d.get("artifact_id") or d.get("commit_sha") or d.get("pointer_id") or ""
                        dest.execute("INSERT INTO conjoin_row VALUES(?,?,?,?)", (name,t,str(pk),json.dumps(d,ensure_ascii=False,default=str)))
                except Exception as e:
                    dest.execute("INSERT INTO sqlite_table_inventory VALUES(?,?,?)", (name,t,-1))
            src.close(); tmpdb.unlink(missing_ok=True)
        for name in z.namelist():
            if name.startswith("project/") and not name.endswith("/"):
                data=z.read(name)
                dest.execute("INSERT INTO project_gitlike_file_ledger VALUES(?,?,?,?)", (name, hashlib.sha256(data).hexdigest(), len(data), "project_package_member"))
    for k,v in {"created_at":base.now(), "normal_zip":str(normal_zip), "mode":"GEMINI_PROJECT_CONJOINED_DB", "env_locked":"PUBLIC_ENV_UOP_PROJECT_LOCKED.zip"}.items():
        dest.execute("INSERT OR REPLACE INTO universal_project_pointer VALUES(?,?)", (k,v))
    dest.commit(); dest.execute("VACUUM"); dest.close()
    return out


def _make_gitlike_jsonl(normal_zip: Path) -> bytes:
    lines=[]
    with zipfile.ZipFile(normal_zip,"r") as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            if name.startswith("project/") or name.startswith("manifests/") or name.startswith("receipts/") or name in {"FLASH_ME_FIRST_SINGLE_PROMPT.txt", ".uepc_env", ".uepc_project", ".uepc_profile"}:
                data=z.read(name)
                lines.append(json.dumps({"path":name,"sha256":hashlib.sha256(data).hexdigest(),"size":len(data)}, ensure_ascii=False))
    return ("\n".join(lines)+"\n").encode("utf-8")


def _gemini_prompt() -> bytes:
    return """UEPC-GEMINI-EXACT10-PROJECT-CONJOINED-BOOT-001 | Mode: pointer_first_validation | Category: exact-10 Gemini package

CHAT_NAME:
[GOLDV3]

This is a Gemini-compatible exact-10 package, not the normal one-upload package.
Read the 10 root files only. Do not ask for recursive project folders.

READ ORDER:
1. GEMINI_FLASH_PROMPT.txt
2. UEPC_UNIVERSAL_POINTER.json
3. UEPC_DOT_LOCKS_AND_READ_ORDER.txt
4. PUBLIC_ENV_UOP_PROJECT_LOCKED.zip
5. GENERATED_PROJECT_CONJOINED.sqlite
6. PROJECT_GITLIKE_FILE_LEDGER.jsonl
7. PROJECT_TOPOLOGY.mmd
8. PROJECT_TOPOLOGY.svg
9. PROJECT_TOPOLOGY.png
10. MANIFEST_RECEIPTS_RECOVERY.json

Rules:
- Env/UOP/project-template inside PUBLIC_ENV_UOP_PROJECT_LOCKED.zip are locked/read-only.
- GENERATED_PROJECT_CONJOINED.sqlite is the joined generated-project brain view from project_router + all sector DBs.
- PROJECT_GITLIKE_FILE_LEDGER.jsonl is the path/hash ledger for project/package members.
- Chat window is display/runtime only.
- Do not mutate Env/UOP/template law.

Run compact entry slip:
package_seen=
exact10_file_count=
universal_pointer_seen=
locked_env_package_seen=
conjoined_project_db_seen=
gitlike_ledger_seen=
topology_seen=
blocker_if_any=
""".encode("utf-8")


def export_gemini_exact10(workspace_dir: str, brain_name: str, progress=None):
    root=base.brain_output_dir(workspace_dir, brain_name)
    normal_zip=_find_latest_normal_zip(root, brain_name)
    packages=root/"packages"; packages.mkdir(parents=True, exist_ok=True)
    slug=base.slugify_name(brain_name)
    out_zip=packages/f"{slug}_gemini_exact10_v002.zip"
    if out_zip.exists(): out_zip.unlink()
    conjoined=_conjoin_from_normal_zip(normal_zip)
    pointer={"created_at":base.now(),"gemini_file_count":10,"normal_source_zip":str(normal_zip),"generated_project_db":"GENERATED_PROJECT_CONJOINED.sqlite","locked_env":"PUBLIC_ENV_UOP_PROJECT_LOCKED.zip","files":EXACT10}
    locks="""UEPC dot-locks/read-order for Gemini exact10\n.uepc_env=inside PUBLIC_ENV_UOP_PROJECT_LOCKED.zip\n.uepc_project=inside PUBLIC_ENV_UOP_PROJECT_LOCKED.zip + GENERATED_PROJECT_CONJOINED.sqlite\n.uepc_profile=inside PUBLIC_ENV_UOP_PROJECT_LOCKED.zip\n"""
    manifest={"created_at":base.now(),"normal_zip_sha256":base.sha256_file(normal_zip),"purpose":"Gemini exact10 only; normal export untouched."}
    with zipfile.ZipFile(out_zip,"w",compression=zipfile.ZIP_DEFLATED) as oz:
        oz.writestr("GEMINI_FLASH_PROMPT.txt", _gemini_prompt())
        oz.writestr("UEPC_UNIVERSAL_POINTER.json", json.dumps(pointer,indent=2).encode("utf-8"))
        oz.writestr("UEPC_DOT_LOCKS_AND_READ_ORDER.txt", locks.encode("utf-8"))
        _copy_selected_locked_env(normal_zip, oz)
        oz.write(conjoined,"GENERATED_PROJECT_CONJOINED.sqlite")
        oz.writestr("PROJECT_GITLIKE_FILE_LEDGER.jsonl", _make_gitlike_jsonl(normal_zip))
        oz.writestr("PROJECT_TOPOLOGY.mmd", _zip_read_or_empty(normal_zip,["project/topology/local_code_lane.mmd","project/topology/project_master_topology.mmd","project/project_topology_template.mmd"]) or b"flowchart TD\n  A[No topology found]\n")
        oz.writestr("PROJECT_TOPOLOGY.svg", _zip_read_or_empty(normal_zip,["project/topology/local_code_lane.svg","project/topology/project_master_topology.svg","project/project_topology_template.svg"]) or b"<svg xmlns='http://www.w3.org/2000/svg'><text x='10' y='20'>No SVG topology found</text></svg>")
        oz.writestr("PROJECT_TOPOLOGY.png", _zip_read_or_empty(normal_zip,["project/topology/local_code_lane_CRYSTAL.png","project/topology/local_code_lane_4K.png","project/topology/local_code_lane.png","project/project_topology_template.png"]) or b"")
        oz.writestr("MANIFEST_RECEIPTS_RECOVERY.json", json.dumps(manifest,indent=2).encode("utf-8"))
    shutil.rmtree(conjoined.parent, ignore_errors=True)
    with zipfile.ZipFile(out_zip,"r") as z:
        names=z.namelist()
        if len(names)!=10:
            raise RuntimeError(f"GEMINI_EXACT10_FAILED: {len(names)} files: {names}")
        missing=[n for n in EXACT10 if n not in names]
        if missing:
            raise RuntimeError(f"GEMINI_EXACT10_MISSING: {missing}")
    if progress:
        progress({"stage":"done","task":"Gemini exact10 project-conjoined package ready","file":str(out_zip),"percent":100,"done":1,"total":1,"eta_seconds":"--","finish_epoch":"--"})
    return {"gemini_package_zip":str(out_zip),"file_count":10,"status":"GEMINI_EXACT10_PROJECT_CONJOINED_READY","sha256":base.sha256_file(out_zip)}


def export_gemini_compatible_package(workspace_dir: str, brain_name: str, progress=None):
    return export_gemini_exact10(workspace_dir, brain_name, progress)


# Normal export intentionally not overridden.
export_one_upload_package = base.export_one_upload_package
render_topology = base.render_topology
scan_tools = base.scan_tools
install_missing_dependencies = base.install_missing_dependencies
brain_output_dir = base.brain_output_dir
normalize_workspace_dir = base.normalize_workspace_dir
LANE_DEFS = base.LANE_DEFS
TAB_ORDER = base.TAB_ORDER
