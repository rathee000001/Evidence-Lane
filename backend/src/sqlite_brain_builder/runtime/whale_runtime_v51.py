from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable, Iterable, Any
from xml.etree import ElementTree as ET

ProgressCallback = Callable[[dict], None]

CODE_TEXT_EXTS = {'.py','.ts','.tsx','.js','.jsx','.css','.scss','.html','.htm','.json','.jsonl','.yaml','.yml','.sql','.md','.toml','.ini','.cfg','.env','.txt'}
CODE_SKIP_ASSET_EXTS = {'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.gif','.ico','.glb','.gltf','.fbx','.obj','.stl','.blend','.mp4','.mov','.avi','.mkv','.webm','.mp3','.wav','.flac','.ogg','.zip','.rar','.7z','.exe','.dll','.bin','.pkl','.pickle'}
CODE_READONLY_ARTIFACT_EXTS = {'.json','.jsonl','.csv','.tsv','.xml','.svg','.mmd','.html','.htm','.ipynb','.md','.txt'}
SKIP_DIRS = {'.git','node_modules','.venv','venv','dist','build','.next','__pycache__','.pytest_cache','.mypy_cache','.ruff_cache'}

LANE_TABLES = {
    'github': ['code_repo','git_remote','git_branch','git_commit','git_file_change','source_file','source_byte_coverage','code_file','code_file_role','code_file_version','code_line_snapshot','code_chunk','code_symbol','app_route','dependency_manifest','dependency_item','code_dependency_edge','code_import_edge','workflow_node','workflow_edge','project_artifact','artifact_relation_edge','source_structure_signature','extraction_signal','code_fts','line_fts','symbol_fts','route_fts','commit_fts','artifact_fts'],
    'local_code': ['code_repo','git_remote','git_branch','git_commit','git_file_change','source_file','source_byte_coverage','code_file','code_file_role','code_file_version','code_line_snapshot','code_chunk','code_symbol','app_route','dependency_manifest','dependency_item','code_dependency_edge','code_import_edge','workflow_node','workflow_edge','project_artifact','artifact_relation_edge','source_structure_signature','extraction_signal','code_fts','line_fts','symbol_fts','route_fts','commit_fts','artifact_fts'],
    'chat_lineage': ['lineage_source','lineage_turn','lineage_prompt','lineage_response','lineage_decision','lineage_delta','lineage_requirement','lineage_artifact_reference','lineage_hard_gate','source_structure_signature','extraction_signal','lineage_fts'],
    'discussion': ['discussion_source','discussion_turn','discussion_item','discussion_decision','discussion_delta','discussion_hard_gate','discussion_artifact_reference','discussion_next_action','source_structure_signature','extraction_signal','discussion_fts'],
    'analysis': ['analysis_source','analysis_claim','analysis_supporting_evidence','analysis_risk','analysis_alternative','analysis_open_question','analysis_accepted_decision','analysis_blocked_item','source_structure_signature','extraction_signal','analysis_fts'],
    'plan': ['plan_source','plan_phase','plan_milestone','plan_task','plan_dependency','plan_owner','plan_status','plan_acceptance_criteria','plan_blocker','plan_next_action','source_structure_signature','extraction_signal','plan_fts'],
    'mode': ['mode_source','mode_rule','mode_scope','mode_gate','mode_allowed_action','mode_blocked_action','mode_trigger','mode_response_template','mode_priority','mode_supersede_ledger','source_structure_signature','extraction_signal','mode_fts'],
    'docs': ['doc_file','doc_structure','doc_heading','doc_paragraph','doc_chunk','doc_table_extract','doc_image_reference','source_structure_signature','extraction_signal','doc_fts'],
    'data_excel_csv': ['sheet_workbook','sheet_tab','sheet_range','sheet_table','sheet_formula','sheet_formula_dependency_edge','sheet_cell_sample','sheet_chart_metadata','csv_header','csv_row_sample','data_structure_signature','source_structure_signature','extraction_signal','data_fts'],
    'ppt_presentation': ['ppt_file','ppt_slide','ppt_shape','ppt_text_block','ppt_notes','ppt_table','ppt_image_reference','ppt_structure_signature','source_structure_signature','extraction_signal','ppt_fts'],
    'pdf_ocr': ['pdf_file','pdf_page','pdf_text_block','pdf_image_block','pdf_ocr_run','pdf_ocr_block','pdf_ocr_line','pdf_structure_signature','source_structure_signature','extraction_signal','pdf_fts'],
    'images_ocr': ['image_file','image_metadata','image_ocr_run','image_ocr_block','image_ocr_line','image_review_region','source_structure_signature','extraction_signal','image_ocr_fts'],
    'artifacts': ['project_artifact','artifact_metadata','artifact_text_extract','artifact_relation_edge','artifact_review_required','source_structure_signature','extraction_signal','artifact_fts'],
    'custom': ['custom_source','custom_item','custom_evidence','custom_decision','custom_next_action','source_structure_signature','extraction_signal','custom_fts'],
}
LANE_LABEL = {
    'github':'GitHub','local_code':'Local Code','chat_lineage':'Chat Lineage','discussion':'Discussion','analysis':'Analysis','plan':'Plan','mode':'Mode','docs':'Docs','data_excel_csv':'Data / Excel / CSV','ppt_presentation':'PPT / Presentation','pdf_ocr':'PDF / OCR','images_ocr':'Images / OCR','artifacts':'Artifacts','custom':'Custom'
}
TAB_ORDER = ['GitHub','Local Code','Chat Lineage','Discussion','Analysis','Plan','Mode','Docs','Data','PPT','PDF/OCR','Images/OCR','Artifacts','Custom','Packages','Receipts']
LANE_TAB = {'github':'GitHub','local_code':'Local Code','chat_lineage':'Chat Lineage','discussion':'Discussion','analysis':'Analysis','plan':'Plan','mode':'Mode','docs':'Docs','data_excel_csv':'Data','ppt_presentation':'PPT','pdf_ocr':'PDF/OCR','images_ocr':'Images/OCR','artifacts':'Artifacts','custom':'Custom'}
SIDEBAR_LANES = [(LANE_LABEL[k], LANE_TAB[k]) for k in ['github','local_code','chat_lineage','discussion','analysis','plan','mode','docs','data_excel_csv','ppt_presentation','pdf_ocr','images_ocr','artifacts','custom']]


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')

def stable_id(prefix: str, value: str) -> str:
    return prefix + '_' + hashlib.sha1(str(value).encode('utf-8', errors='ignore')).hexdigest()[:20]

def slugify_name(name: str) -> str:
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', (name or 'new_brain').strip()).strip('_').lower()
    return s or 'new_brain'

def normalize_workspace_dir(path: str | Path) -> Path:
    p = Path(path).expanduser()
    # Collapse accidental nested brains folders. User selects _0000; output should be _0000\brain_slug, not _0000\brains\brains\slug.
    while p.name.lower() == 'brains':
        p = p.parent
    return p

def brain_output_dir(workspace_dir: str | Path, brain_name: str) -> Path:
    return normalize_workspace_dir(workspace_dir) / slugify_name(brain_name or 'new_brain')

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    return con

def emit(cb: ProgressCallback | None, tracker: dict, stage: str, task: str, file: str = '', inc: int = 1):
    tracker['done'] = min(tracker.get('total', 100), tracker.get('done', 0) + inc)
    pct = int(tracker['done'] * 100 / max(1, tracker.get('total', 100)))
    elapsed = max(0.1, time.time() - tracker.get('start', time.time()))
    eta = int(max(0, tracker.get('total', 100) - tracker['done']) * elapsed / max(1, tracker['done']))
    if cb:
        cb({'stage': stage, 'task': task, 'file': file, 'done': tracker['done'], 'total': tracker.get('total', 100), 'percent': pct, 'elapsed_seconds': int(elapsed), 'eta_seconds': eta, 'finish_epoch': int(time.time() + eta)})

def safe_text(path: Path, limit: int = 20_000_000) -> str:
    try:
        data = path.read_bytes()[:limit]
        return data.decode('utf-8', errors='replace')
    except Exception:
        return ''

def chunk_text(text: str, size: int = 4500) -> list[str]:
    if not text:
        return []
    out = []
    pos = 0
    while pos < len(text):
        out.append(text[pos:pos+size])
        pos += size
    return out

def _sql_table(con: sqlite3.Connection, table: str):
    if table.endswith('_fts'):
        con.execute(f'CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(entity_id, text)')
        return
    con.execute(f'''CREATE TABLE IF NOT EXISTS {table}(
        id TEXT PRIMARY KEY,
        source_id TEXT,
        name TEXT,
        path TEXT,
        value TEXT,
        metadata_json TEXT,
        active_bool INTEGER DEFAULT 1,
        created_at TEXT
    )''')

def init_sector(con: sqlite3.Connection, lane_key: str):
    con.execute('''CREATE TABLE IF NOT EXISTS sector_manifest(sector_id TEXT PRIMARY KEY,lane_key TEXT,lane_label TEXT,version TEXT,status TEXT,created_at TEXT)''')
    _sql_table(con, 'source_active_state')
    con.execute('''CREATE TABLE IF NOT EXISTS review_required_item(review_id TEXT PRIMARY KEY,source_id TEXT,lane_key TEXT,path TEXT,reason TEXT,status TEXT,created_at TEXT)''')
    for t in LANE_TABLES.get(lane_key, LANE_TABLES['custom']):
        _sql_table(con, t)
    con.execute('INSERT OR REPLACE INTO sector_manifest VALUES(?,?,?,?,?,?)', (f'sector_{lane_key}', lane_key, LANE_LABEL.get(lane_key, lane_key), 'v001', 'ACTIVE_SCHEMA_READY', now()))

def init_router(router_db: Path, brain_name: str):
    con = connect(router_db)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS brain_manifest(brain_id TEXT PRIMARY KEY, brain_name TEXT, brain_slug TEXT, created_at TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS source_registry(source_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, source_type TEXT, display_name TEXT, path TEXT, source_hash TEXT, active_bool INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sector_registry(sector_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, sector_db_path TEXT, active_bool INTEGER, version TEXT, sector_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sector_pointer(pointer_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, sector_db_path TEXT, mmd_required INTEGER, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS source_active_state(active_state_id TEXT PRIMARY KEY, source_id TEXT, lane_key TEXT, source_hash TEXT, active_bool INTEGER, state TEXT, reason TEXT, changed_at TEXT);
    CREATE TABLE IF NOT EXISTS unload_session(unload_session_id TEXT PRIMARY KEY, started_at TEXT, completed_at TEXT, status TEXT, affected_source_count INTEGER, receipt_hash TEXT);
    CREATE TABLE IF NOT EXISTS unload_item(unload_item_id TEXT PRIMARY KEY, unload_session_id TEXT, source_id TEXT, lane_key TEXT, reason TEXT, impact_status TEXT);
    CREATE TABLE IF NOT EXISTS sector_rebuild_event(rebuild_event_id TEXT PRIMARY KEY, old_sector_version_id TEXT, new_sector_version_id TEXT, reason TEXT, started_at TEXT, completed_at TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS package_manifest(package_id TEXT PRIMARY KEY, package_path TEXT, created_at TEXT, package_hash TEXT);
    CREATE TABLE IF NOT EXISTS workspace_capability_profile(profile_id TEXT PRIMARY KEY, profile_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS fallback_tool_registry(tool_category TEXT PRIMARY KEY, primary_tool TEXT, fallback_tool TEXT, metadata_only_fallback TEXT, detected_path TEXT, version TEXT, status TEXT, last_tested TEXT, warning_message TEXT);
    ''')
    con.execute('INSERT OR REPLACE INTO brain_manifest VALUES(?,?,?,?,?)', (stable_id('brain', brain_name), brain_name, slugify_name(brain_name), now(), 'ACTIVE'))
    con.commit(); con.close()

def insert_generic(con: sqlite3.Connection, table: str, source_id: str, name: str, path: str, value: str = '', meta: dict | None = None):
    _sql_table(con, table)
    row_id = stable_id(table, source_id + name + path + value[:200])
    con.execute(f'INSERT OR REPLACE INTO {table}(id,source_id,name,path,value,metadata_json,active_bool,created_at) VALUES(?,?,?,?,?,?,?,?)', (row_id, source_id, name, path, value, json.dumps(meta or {}, ensure_ascii=False), 1, now()))
    return row_id

def insert_fts(con: sqlite3.Connection, table: str, entity_id: str, text: str):
    try:
        _sql_table(con, table)
        con.execute(f'INSERT INTO {table}(entity_id,text) VALUES(?,?)', (entity_id, text[:100000]))
    except Exception:
        pass

def signature(con: sqlite3.Connection, source_id: str, lane_key: str, path: str, metrics: dict):
    metrics_json = json.dumps(metrics, sort_keys=True, ensure_ascii=False)
    table = 'source_structure_signature' if 'source_structure_signature' in LANE_TABLES.get(lane_key, []) else 'data_structure_signature'
    if lane_key == 'ppt_presentation': table = 'ppt_structure_signature'
    if lane_key == 'pdf_ocr': table = 'pdf_structure_signature'
    if lane_key == 'data_excel_csv': table = 'data_structure_signature'
    insert_generic(con, table, source_id, lane_key + '_signature', path, metrics_json, {'structure_hash': sha256_bytes(metrics_json.encode())})
    for k, v in metrics.items():
        if isinstance(v, (bool, int, float, str)):
            insert_generic(con, 'extraction_signal', source_id, str(k), path, str(v), {'lane': lane_key})

def source_row(con: sqlite3.Connection, source_id: str, lane_key: str, path: Path, status: str, reason: str = ''):
    try:
        h = sha256_file(path) if path.exists() and path.is_file() else sha256_bytes(str(path).encode())
        size = path.stat().st_size if path.exists() and path.is_file() else 0
    except Exception:
        h, size = sha256_bytes(str(path).encode()), 0
    insert_generic(con, 'source_active_state', source_id, status, str(path), reason, {'sha256': h, 'size_bytes': size})
    return h, size

# ---------------- Tool scan / installer support ----------------

def _cmd_version(cmd: list[str], timeout: int = 8) -> tuple[str, str]:
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        text = (run.stdout or run.stderr or '').strip().splitlines()
        return ('FOUND' if run.returncode == 0 else 'ERROR', text[0][:300] if text else '')
    except Exception as e:
        return 'MISSING', str(e)[:300]

def scan_tools(workspace_dir: str | Path) -> dict:
    tools = {}
    for key, cmd in {
        'git':['git','--version'], 'tesseract':['tesseract','--version'], 'mmdc':['mmdc','--version'], 'node':['node','--version'], 'npm':['npm','--version'], 'python':[sys.executable,'--version']
    }.items():
        status, ver = _cmd_version(cmd)
        tools[key] = {'status': status, 'version': ver, 'path': shutil.which(cmd[0])}
    packages = {}
    for pkg in ['fitz','pypdf','docx','pptx','openpyxl','PIL','pytesseract']:
        try:
            __import__(pkg)
            packages[pkg] = 'FOUND'
        except Exception:
            packages[pkg] = 'MISSING'
    profile = {'created_at': now(), 'tools': tools, 'python_packages': packages, 'policy': 'scanner only; install requires user command'}
    out = normalize_workspace_dir(workspace_dir) / 'system_capability_profile.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, indent=2), encoding='utf-8')
    return profile

def install_missing_python_packages(cb: ProgressCallback | None = None) -> dict:
    pkgs = ['PyMuPDF','pypdf','python-docx','python-pptx','openpyxl','pillow','pytesseract']
    results = {}
    tracker = {'done':0,'total':len(pkgs),'start':time.time()}
    for p in pkgs:
        emit(cb, tracker, 'install', 'pip installing/checking ' + p, p, 1)
        try:
            run = subprocess.run([sys.executable, '-m', 'pip', 'install', p], capture_output=True, text=True, timeout=600)
            results[p] = {'returncode': run.returncode, 'stdout_tail': run.stdout[-1000:], 'stderr_tail': run.stderr[-1000:]}
        except Exception as e:
            results[p] = {'error': str(e)}
    return results

# ---------------- Code lane ----------------

def iter_code_files(root: Path):
    for p in root.rglob('*'):
        if not p.is_file(): continue
        if any(part in SKIP_DIRS for part in p.parts): continue
        yield p

def code_role(rel: str, ext: str, text: str) -> str:
    low = rel.lower()
    if '/app/' in '/' + low or '/pages/' in '/' + low or '/routes/' in '/' + low: return 'UI_PAGE_ROUTE'
    if ext in {'.tsx','.jsx'} or 'component' in low or '/ui/' in low: return 'UI_UX_COMPONENT'
    if '/api/' in low or 'route.' in low or 'router' in low: return 'API_BACKEND_ROUTE'
    if 'service' in low or 'util' in low or 'helper' in low or '/lib/' in low: return 'SERVICE_OR_UTILITY'
    if 'db' in low or 'model' in low or 'schema' in low or 'data' in low: return 'DATA_DB_LAYER'
    if ext in {'.json','.toml','.yaml','.yml','.ini','.cfg'} or 'config' in low: return 'CONFIG_BUILD_TOOLING'
    if 'test' in low or 'spec' in low: return 'TEST_QA'
    if ext in {'.md','.txt'}: return 'DOCS_OR_NOTES'
    return 'GENERAL_CODE'

def init_code_tables(con: sqlite3.Connection):
    for t in LANE_TABLES['local_code']:
        _sql_table(con, t)

def add_code_file(con: sqlite3.Connection, root: Path, path: Path, source_id: str):
    rel = path.relative_to(root).as_posix()
    ext = path.suffix.lower()
    h = sha256_file(path)
    size = path.stat().st_size
    file_id = stable_id('file', rel)
    # no study for images/3D/video/binary inside code sector
    if ext in CODE_SKIP_ASSET_EXTS:
        insert_generic(con, 'source_file', source_id, rel, rel, '', {'file_id': file_id, 'sha256': h, 'size_bytes': size, 'status':'CODE_ASSET_SKIPPED_NOT_STUDIED'})
        insert_generic(con, 'source_byte_coverage', source_id, rel, rel, 'CODE_ASSET_SKIPPED_NOT_STUDIED', {'file_id': file_id, 'sha256': h, 'size_bytes': size, 'reason':'No image/3D/video asset study inside code lane.'})
        return
    if ext not in CODE_TEXT_EXTS and path.name.lower() not in {'dockerfile','requirements.txt','package.json','pyproject.toml'}:
        insert_generic(con, 'source_file', source_id, rel, rel, '', {'file_id': file_id, 'sha256': h, 'size_bytes': size, 'status':'UNSUPPORTED_CODE_FILE_METADATA_ONLY'})
        insert_generic(con, 'source_byte_coverage', source_id, rel, rel, 'METADATA_ONLY_REVIEW_REQUIRED', {'file_id': file_id, 'sha256': h, 'size_bytes': size})
        return
    text = safe_text(path)
    lines = text.splitlines()
    lang = ext.strip('.') or path.name.lower()
    role = code_role(rel, ext, text)
    fv = stable_id('fv', rel + h)
    insert_generic(con, 'source_file', source_id, rel, rel, '', {'file_id': file_id, 'sha256': h, 'size_bytes': size, 'status':'TEXT_STUDIED'})
    insert_generic(con, 'source_byte_coverage', source_id, rel, rel, 'TEXT_CHUNKED_LINE_INDEXED', {'file_id': file_id, 'bytes': size})
    insert_generic(con, 'code_file', source_id, rel, rel, text[:5000], {'file_id': file_id, 'language': lang, 'extension': ext, 'sha256': h, 'role': role})
    insert_generic(con, 'code_file_role', source_id, role, rel, 'deterministic path/ext/content signals', {'file_id': file_id, 'role_name': role})
    insert_generic(con, 'code_file_version', source_id, fv, rel, '', {'file_id': file_id, 'file_version_id': fv, 'raw_file_sha256': h, 'normalized_text_sha256': sha256_bytes(text.replace('\r\n','\n').encode()), 'line_count': len(lines), 'byte_count': size})
    for i, line in enumerate(lines, 1):
        line_id = stable_id('line', fv + str(i) + line)
        insert_generic(con, 'code_line_snapshot', source_id, f'L{i}', rel, line, {'line_id': line_id, 'file_id': file_id, 'file_version_id': fv, 'line_number': i, 'line_sha256': sha256_bytes(line.encode()), 'is_blank': not line.strip(), 'is_comment': line.strip().startswith(('#','//','/*','*'))})
        if i <= 3000:
            insert_fts(con, 'line_fts', line_id, line)
    chunks = []
    for start in range(0, len(lines), 120):
        block = '\n'.join(lines[start:start+120])
        if block.strip(): chunks.append((start+1, min(start+120, len(lines)), block))
    for start,end,block in chunks:
        chunk_id = stable_id('chunk', fv + str(start) + sha256_bytes(block.encode()))
        insert_generic(con, 'code_chunk', source_id, f'{rel}:{start}-{end}', rel, block, {'chunk_id': chunk_id, 'file_id': file_id, 'file_version_id': fv, 'start_line': start, 'end_line': end, 'chunk_sha256': sha256_bytes(block.encode()), 'chunk_type': role})
        insert_fts(con, 'code_fts', chunk_id, block)
    # symbols/imports/routes
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        imp = re.match(r"(?:import|from)\s+(.+)", stripped)
        if imp:
            insert_generic(con, 'code_import_edge', source_id, f'import:{i}', rel, stripped, {'from_path': rel, 'line_number': i, 'import_target': imp.group(1)[:500]})
        for pat, stype in [(r'(?:export\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)','FUNCTION'),(r'def\s+([A-Za-z_][A-Za-z0-9_]*)','FUNCTION'),(r'class\s+([A-Za-z_][A-Za-z0-9_]*)','CLASS'),(r'(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)','CONST_OR_COMPONENT')]:
            m = re.search(pat, stripped)
            if m:
                sym = m.group(1); sid = stable_id('sym', rel+sym+str(i))
                insert_generic(con, 'code_symbol', source_id, sym, rel, stripped, {'symbol_id': sid, 'symbol_type': stype, 'file_id': file_id, 'file_version_id': fv, 'line': i})
                insert_fts(con, 'symbol_fts', sid, stripped)
    low = rel.lower()
    if '/app/' in '/' + low or '/pages/' in '/' + low or '/api/' in low or 'route.' in low or 'page.' in low:
        route_path = '/' + re.sub(r'^(src/)?(app|pages)/', '', rel)
        route_path = re.sub(r'/(page|index)\.(tsx|ts|jsx|js|py)$', '/', route_path)
        route_path = re.sub(r'route\.(tsx|ts|js|py)$', '', route_path)
        route_path = route_path.replace('//','/').rstrip('/') or '/'
        rid = stable_id('route', route_path + rel)
        rtype = 'API_ENDPOINT' if '/api/' in low or 'route.' in low else 'FRONTEND_PAGE'
        insert_generic(con, 'app_route', source_id, route_path, rel, '', {'route_id': rid, 'route_path': route_path, 'route_type': rtype, 'file_id': file_id, 'file_version_id': fv})
        insert_fts(con, 'route_fts', rid, route_path + ' ' + rel)
    # dependencies and read-only text artifacts
    if path.name.lower() in {'package.json','requirements.txt','pyproject.toml'}:
        mid = stable_id('manifest', rel+h)
        ecosystem = 'node' if path.name.lower() == 'package.json' else 'python'
        insert_generic(con, 'dependency_manifest', source_id, path.name, rel, text[:5000], {'manifest_id': mid, 'ecosystem': ecosystem, 'sha256': h})
        if path.name.lower() == 'package.json':
            try:
                data = json.loads(text)
                for sect in ['dependencies','devDependencies','peerDependencies']:
                    for name, ver in data.get(sect, {}).items():
                        insert_generic(con, 'dependency_item', source_id, name, rel, str(ver), {'manifest_id': mid, 'ecosystem': ecosystem, 'section': sect})
            except Exception: pass
        elif path.name.lower() == 'requirements.txt':
            for line in lines:
                x = line.strip()
                if x and not x.startswith('#'):
                    insert_generic(con, 'dependency_item', source_id, re.split(r'[=<>!~ ]+', x)[0], rel, x, {'manifest_id': mid, 'ecosystem': ecosystem})
    if ext in CODE_READONLY_ARTIFACT_EXTS and ext not in {'.py','.ts','.tsx','.js','.jsx'}:
        insert_generic(con, 'project_artifact', source_id, rel, rel, text[:5000], {'artifact_type': ext, 'artifact_sha256': h, 'semantic_status': 'READ_ONLY_TEXT_ARTIFACT_IN_CODE_LANE'})
        insert_fts(con, 'artifact_fts', stable_id('artifact', rel), text)

def ingest_code(con: sqlite3.Connection, src: dict, tracker: dict, cb: ProgressCallback | None):
    root = Path(src.get('path',''))
    source_id = src.get('source_id') or stable_id('source', str(root))
    init_code_tables(con)
    source_row(con, source_id, src.get('lane_key','local_code'), root, 'ACTIVE_CODE_ROOT')
    # git lineage
    try:
        run = subprocess.run(['git','-C',str(root),'log','--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s','-n','1000'], capture_output=True, text=True, timeout=60)
        if run.returncode == 0:
            for order, line in enumerate(run.stdout.splitlines()):
                parts = line.split('\x1f')
                if len(parts) >= 6:
                    insert_generic(con, 'git_commit', source_id, parts[0], str(root), parts[5], {'short_sha':parts[1], 'author':parts[2], 'author_email_hash':sha256_bytes(parts[3].encode()), 'commit_time':parts[4], 'commit_order': order})
                    insert_fts(con, 'commit_fts', parts[0], parts[5])
    except Exception:
        pass
    files = list(iter_code_files(root))
    for idx, p in enumerate(files, 1):
        add_code_file(con, root, p, source_id)
        if idx % 25 == 0: con.commit()
        emit(cb, tracker, 'code', f'indexing code file {idx}/{len(files)}', str(p), 1)
    metrics = {'files_seen': len(files), 'code_file': _count(con,'code_file'), 'routes': _count(con,'app_route'), 'dependencies': _count(con,'dependency_item'), 'assets_skipped_by_code_law': _count_status(con, 'source_byte_coverage', 'CODE_ASSET_SKIPPED_NOT_STUDIED')}
    signature(con, source_id, src.get('lane_key','local_code'), str(root), metrics)

# ---------------- Non-code extraction ----------------

def parse_docx(path: Path) -> dict:
    text_parts, tables, images = [], 0, 0
    try:
        import zipfile as zf
        with zf.ZipFile(path) as z:
            for name in z.namelist():
                low = name.lower()
                if low.startswith('word/media/'): images += 1
                if low == 'word/document.xml' or (low.startswith('word/') and low.endswith('.xml')):
                    xml = z.read(name)
                    try:
                        root = ET.fromstring(xml)
                        texts = [el.text for el in root.iter() if el.text]
                        if texts: text_parts.append('\n'.join(texts))
                        tables += xml.count(b'<w:tbl')
                    except Exception:
                        pass
    except Exception: pass
    return {'text':'\n'.join(text_parts), 'tables':tables, 'images':images}

def parse_pptx(path: Path) -> dict:
    slides, notes, images, texts = 0, 0, 0, []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                low = name.lower()
                if low.startswith('ppt/media/'): images += 1
                if low.startswith('ppt/slides/slide') and low.endswith('.xml'):
                    slides += 1; data = z.read(name)
                    try:
                        root = ET.fromstring(data); vals = [e.text for e in root.iter() if e.text]
                        texts.append('\n'.join(vals))
                    except Exception: pass
                if low.startswith('ppt/notesSlides/') and low.endswith('.xml'): notes += 1
    except Exception: pass
    return {'text':'\n'.join(texts), 'slides':slides, 'notes':notes, 'images':images}

def parse_xlsx(path: Path) -> dict:
    # Prefer openpyxl for formulas/sheets/cells, fallback to XML counts.
    out = {'sheets':0, 'cells':0, 'formulas':0, 'tables':0, 'charts':0, 'samples':[]}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=False, read_only=False)
        out['sheets'] = len(wb.sheetnames)
        for ws in wb.worksheets:
            rows = 0
            for row in ws.iter_rows():
                rows += 1
                if rows <= 20:
                    vals = [c.value for c in row[:30]]
                    if any(v is not None for v in vals): out['samples'].append({'sheet': ws.title, 'values': [str(v) if v is not None else '' for v in vals]})
                for c in row:
                    if c.value is not None: out['cells'] += 1
                    if isinstance(c.value, str) and c.value.startswith('='): out['formulas'] += 1
                if rows > 200: break
            try: out['tables'] += len(ws.tables)
            except Exception: pass
        return out
    except Exception:
        pass
    try:
        with zipfile.ZipFile(path) as z:
            out['sheets'] = sum(1 for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))
            out['charts'] = sum(1 for n in z.namelist() if n.startswith('xl/charts/') and n.endswith('.xml'))
            out['tables'] = sum(1 for n in z.namelist() if n.startswith('xl/tables/') and n.endswith('.xml'))
    except Exception: pass
    return out

def parse_pdf(path: Path) -> dict:
    out = {'text':'','pages':0,'text_pages':0,'ocr_blocks':0,'ocr_lines':0,'used':'NONE','review':None}
    # PyMuPDF: text and OCR scanned page pixmaps if pytesseract exists
    try:
        import fitz
        doc = fitz.open(path)
        out['pages'] = len(doc); out['used'] = 'PyMuPDF'
        texts=[]
        for pi, page in enumerate(doc):
            txt = page.get_text('text') or ''
            if txt.strip(): out['text_pages'] += 1; texts.append(txt)
            else:
                try:
                    import pytesseract
                    pix = page.get_pixmap(matrix=fitz.Matrix(2,2), alpha=False)
                    from PIL import Image
                    img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
                    ocr = pytesseract.image_to_string(img)
                    if ocr.strip():
                        out['ocr_blocks'] += 1; out['ocr_lines'] += len(ocr.splitlines()); texts.append(ocr)
                except Exception:
                    pass
        out['text'] = '\n'.join(texts)
        return out
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path)); out['pages'] = len(reader.pages); out['used']='pypdf'
        texts=[]
        for p in reader.pages:
            txt = p.extract_text() or ''
            if txt.strip(): out['text_pages'] += 1; texts.append(txt)
        out['text'] = '\n'.join(texts)
        return out
    except Exception as e:
        out['review'] = str(e)[:300]
    return out

def parse_image(path: Path) -> dict:
    out = {'metadata': {}, 'ocr_text':'', 'ocr_lines':0, 'ocr_blocks':0, 'used':'metadata'}
    try:
        from PIL import Image
        img = Image.open(path)
        out['metadata'] = {'width': img.width, 'height': img.height, 'mode': img.mode, 'format': img.format}
        try:
            import pytesseract
            txt = pytesseract.image_to_string(img)
            out['ocr_text'] = txt; out['ocr_lines'] = len([l for l in txt.splitlines() if l.strip()]); out['ocr_blocks'] = 1 if txt.strip() else 0; out['used']='pytesseract'
        except Exception as e:
            out['metadata']['ocr_error'] = str(e)[:300]
    except Exception as e:
        out['metadata'] = {'error': str(e)[:300]}
    return out

def ingest_text_lanes(con: sqlite3.Connection, lane_key: str, src: dict, table_prefix: str):
    path = Path(src.get('path','')) if src.get('path') else None
    source_id = src.get('source_id') or stable_id('source', src.get('display_name','') + str(path))
    text = src.get('text') or (safe_text(path) if path and path.exists() else '')
    if path and path.suffix.lower() == '.docx': text = parse_docx(path)['text'] or text
    if path and path.suffix.lower() == '.pdf': text = parse_pdf(path)['text'] or text
    source_row(con, source_id, lane_key, path or Path(src.get('display_name','paste')), 'ACTIVE_TEXT_SOURCE')
    insert_generic(con, f'{table_prefix}_source', source_id, 'source', str(path or src.get('display_name','paste')), text[:5000], {'chars':len(text)})
    # generic deterministic line/chunk ingestion into lane fields
    for i, ch in enumerate(chunk_text(text, 4500)):
        table = f'{table_prefix}_item' if f'{table_prefix}_item' in LANE_TABLES.get(lane_key, []) else f'{table_prefix}_turn' if f'{table_prefix}_turn' in LANE_TABLES.get(lane_key, []) else f'{table_prefix}_rule' if lane_key=='mode' else f'{table_prefix}_source'
        rid = insert_generic(con, table, source_id, f'chunk_{i+1}', str(path or 'paste'), ch, {'chunk_order':i+1, 'sha256':sha256_bytes(ch.encode())})
        insert_fts(con, f'{table_prefix}_fts', rid, ch)
    # rules/signals by simple keywords, no AI meaning
    for line in text.splitlines():
        l = line.strip()
        if not l: continue
        low = l.lower()
        if lane_key in {'discussion','chat_lineage'} and ('decision' in low or 'delta' in low or 'hard gate' in low or 'next' in low):
            target = 'discussion_delta' if lane_key=='discussion' else 'lineage_delta'
            insert_generic(con, target, source_id, target, str(path or 'paste'), l, {'deterministic_keyword_match': True})
        if lane_key == 'plan' and re.match(r'[-*]\s+|\d+\.', l): insert_generic(con, 'plan_task', source_id, 'task', str(path or 'paste'), l, {})
        if lane_key == 'mode' and any(x in low for x in ['must','blocked','allowed','rule','do not']): insert_generic(con, 'mode_rule', source_id, 'rule', str(path or 'paste'), l, {})
    signature(con, source_id, lane_key, str(path or 'paste'), {'chars':len(text), 'chunks':len(chunk_text(text)), 'lines':len(text.splitlines())})

def ingest_docs(con: sqlite3.Connection, src: dict, lane_key='docs'):
    path = Path(src.get('path',''))
    source_id = src.get('source_id') or stable_id('source', str(path))
    ext = path.suffix.lower()
    parsed = parse_docx(path) if ext == '.docx' else {'text': safe_text(path), 'tables':0, 'images':0}
    if ext == '.pdf': parsed = {'text': parse_pdf(path)['text'], 'tables':0, 'images':0}
    text = parsed.get('text','')
    h, size = source_row(con, source_id, lane_key, path, 'ACTIVE_DOC_SOURCE')
    insert_generic(con, 'doc_file', source_id, path.name, str(path), '', {'sha256':h, 'size_bytes':size, 'ext':ext})
    heads = re.findall(r'(?m)^(#{1,6}\s+.+|[A-Z][A-Za-z0-9 ,:;\-]{4,80})$', text[:200000])
    for i, head in enumerate(heads[:500]): insert_generic(con, 'doc_heading', source_id, f'heading_{i+1}', str(path), head, {})
    for i, ch in enumerate(chunk_text(text, 4500)):
        rid = insert_generic(con, 'doc_chunk', source_id, f'chunk_{i+1}', str(path), ch, {'sha256':sha256_bytes(ch.encode())})
        insert_fts(con, 'doc_fts', rid, ch)
    for i, para in enumerate([p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()][:1000]): insert_generic(con, 'doc_paragraph', source_id, f'p_{i+1}', str(path), para[:5000], {})
    insert_generic(con, 'doc_structure', source_id, 'structure', str(path), '', {'headings':len(heads),'tables':parsed.get('tables',0),'images':parsed.get('images',0),'words':len(text.split())})
    for i in range(parsed.get('tables',0)): insert_generic(con, 'doc_table_extract', source_id, f'table_{i+1}', str(path), '', {})
    for i in range(parsed.get('images',0)): insert_generic(con, 'doc_image_reference', source_id, f'image_{i+1}', str(path), '', {})
    signature(con, source_id, lane_key, str(path), {'headings':len(heads),'tables':parsed.get('tables',0),'images':parsed.get('images',0),'words':len(text.split()), 'chunks':len(chunk_text(text))})

def ingest_data(con: sqlite3.Connection, src: dict):
    path = Path(src.get('path','')); source_id = src.get('source_id') or stable_id('source', str(path)); h,size = source_row(con, source_id, 'data_excel_csv', path, 'ACTIVE_DATA_SOURCE')
    ext = path.suffix.lower()
    if ext in {'.csv','.tsv'}:
        delim = '\t' if ext == '.tsv' else ','
        rows_read = 0
        with path.open('r', encoding='utf-8', errors='replace', newline='') as f:
            reader = csv.reader(f, delimiter=delim)
            headers = next(reader, [])
            insert_generic(con, 'csv_header', source_id, 'header', str(path), json.dumps(headers, ensure_ascii=False), {'columns':len(headers)})
            for i,row in enumerate(reader,1):
                insert_generic(con, 'csv_row_sample', source_id, f'row_{i}', str(path), json.dumps(row, ensure_ascii=False), {})
                insert_fts(con, 'data_fts', stable_id('csvrow', str(path)+str(i)), ' '.join(row))
                rows_read += 1
                if i >= 10000: break
        signature(con, source_id, 'data_excel_csv', str(path), {'format':ext, 'headers':len(headers), 'rows_sampled':rows_read})
        return
    if ext in {'.xlsx','.xlsm','.xls'}:
        parsed = parse_xlsx(path)
        insert_generic(con, 'sheet_workbook', source_id, path.name, str(path), '', {'sha256':h, 'size_bytes':size})
        for i in range(parsed.get('sheets',0)): insert_generic(con, 'sheet_tab', source_id, f'sheet_{i+1}', str(path), '', {})
        for i, sample in enumerate(parsed.get('samples',[])[:2000]):
            insert_generic(con, 'sheet_cell_sample', source_id, f'sample_{i+1}', str(path), json.dumps(sample, ensure_ascii=False), {})
        for i in range(int(parsed.get('formulas',0))):
            if i < 5000: insert_generic(con, 'sheet_formula', source_id, f'formula_{i+1}', str(path), '', {})
        signature(con, source_id, 'data_excel_csv', str(path), parsed)
        return
    text = safe_text(path)
    insert_generic(con, 'sheet_workbook', source_id, path.name, str(path), text[:5000], {'fallback':'text_or_metadata'})
    insert_fts(con, 'data_fts', source_id, text)
    signature(con, source_id, 'data_excel_csv', str(path), {'format':ext, 'chars':len(text), 'fallback':True})

def ingest_ppt(con: sqlite3.Connection, src: dict):
    path = Path(src.get('path','')); source_id = src.get('source_id') or stable_id('source', str(path)); h,size = source_row(con, source_id, 'ppt_presentation', path, 'ACTIVE_PPT_SOURCE')
    parsed = parse_pptx(path) if path.suffix.lower() in {'.pptx','.ppt'} else {'text':safe_text(path),'slides':0,'notes':0,'images':0}
    text = parsed.get('text','')
    insert_generic(con, 'ppt_file', source_id, path.name, str(path), '', {'sha256':h,'size_bytes':size})
    for i in range(parsed.get('slides',0)): insert_generic(con, 'ppt_slide', source_id, f'slide_{i+1}', str(path), '', {})
    for i,ch in enumerate(chunk_text(text, 3000)):
        rid = insert_generic(con, 'ppt_text_block', source_id, f'text_{i+1}', str(path), ch, {})
        insert_fts(con, 'ppt_fts', rid, ch)
    for i in range(parsed.get('notes',0)): insert_generic(con, 'ppt_notes', source_id, f'notes_{i+1}', str(path), '', {})
    for i in range(parsed.get('images',0)): insert_generic(con, 'ppt_image_reference', source_id, f'image_{i+1}', str(path), '', {})
    signature(con, source_id, 'ppt_presentation', str(path), {'slides':parsed.get('slides',0),'notes':parsed.get('notes',0),'images':parsed.get('images',0),'chunks':len(chunk_text(text,3000))})

def ingest_pdf(con: sqlite3.Connection, src: dict):
    path = Path(src.get('path','')); source_id = src.get('source_id') or stable_id('source', str(path)); h,size = source_row(con, source_id, 'pdf_ocr', path, 'ACTIVE_PDF_SOURCE')
    parsed = parse_pdf(path); text = parsed.get('text','')
    insert_generic(con, 'pdf_file', source_id, path.name, str(path), '', {'sha256':h,'size_bytes':size,'parser':parsed.get('used')})
    for i in range(parsed.get('pages',0)): insert_generic(con, 'pdf_page', source_id, f'page_{i+1}', str(path), '', {})
    for i,ch in enumerate(chunk_text(text, 3500)):
        rid = insert_generic(con, 'pdf_text_block', source_id, f'text_{i+1}', str(path), ch, {'sha256':sha256_bytes(ch.encode())})
        insert_fts(con, 'pdf_fts', rid, ch)
    if parsed.get('ocr_blocks',0):
        insert_generic(con, 'pdf_ocr_run', source_id, 'ocr_run', str(path), '', {'ocr_blocks':parsed.get('ocr_blocks'), 'ocr_lines':parsed.get('ocr_lines')})
        for i,ch in enumerate(chunk_text(text, 3500)): insert_generic(con, 'pdf_ocr_block', source_id, f'ocr_{i+1}', str(path), ch, {})
    if parsed.get('review') or (parsed.get('pages',0) and not text.strip()): insert_generic(con, 'review_required_item', source_id, 'pdf_review', str(path), parsed.get('review') or 'PDF has no extracted/OCR text', {})
    signature(con, source_id, 'pdf_ocr', str(path), {'pages':parsed.get('pages',0),'text_pages':parsed.get('text_pages',0),'ocr_blocks':parsed.get('ocr_blocks',0),'chunks':len(chunk_text(text,3500))})

def ingest_image(con: sqlite3.Connection, src: dict):
    path = Path(src.get('path','')); source_id = src.get('source_id') or stable_id('source', str(path)); h,size = source_row(con, source_id, 'images_ocr', path, 'ACTIVE_IMAGE_SOURCE')
    parsed = parse_image(path)
    insert_generic(con, 'image_file', source_id, path.name, str(path), '', {'sha256':h,'size_bytes':size})
    insert_generic(con, 'image_metadata', source_id, 'metadata', str(path), '', parsed.get('metadata',{}))
    if parsed.get('ocr_text'):
        insert_generic(con, 'image_ocr_run', source_id, 'ocr_run', str(path), '', {'engine':parsed.get('used')})
        for i,ch in enumerate(chunk_text(parsed['ocr_text'], 2000)):
            rid = insert_generic(con, 'image_ocr_block', source_id, f'ocr_block_{i+1}', str(path), ch, {})
            insert_fts(con, 'image_ocr_fts', rid, ch)
        for i,line in enumerate([l for l in parsed['ocr_text'].splitlines() if l.strip()]): insert_generic(con, 'image_ocr_line', source_id, f'line_{i+1}', str(path), line, {})
    else:
        insert_generic(con, 'image_review_region', source_id, 'review_required', str(path), 'OCR_UNAVAILABLE_OR_EMPTY', parsed.get('metadata',{}))
    metrics = dict(parsed.get('metadata',{})); metrics.update({'ocr_blocks':parsed.get('ocr_blocks',0),'ocr_lines':parsed.get('ocr_lines',0)})
    signature(con, source_id, 'images_ocr', str(path), metrics)

def ingest_artifact(con: sqlite3.Connection, src: dict):
    path = Path(src.get('path','')); source_id = src.get('source_id') or stable_id('source', str(path)); h,size = source_row(con, source_id, 'artifacts', path, 'ACTIVE_ARTIFACT_SOURCE')
    ext = path.suffix.lower(); text = '' if ext in CODE_SKIP_ASSET_EXTS else safe_text(path)
    insert_generic(con, 'project_artifact', source_id, path.name, str(path), text[:10000], {'artifact_type':ext,'sha256':h,'size_bytes':size})
    insert_generic(con, 'artifact_metadata', source_id, 'metadata', str(path), '', {'ext':ext,'sha256':h,'size_bytes':size})
    if text.strip():
        for i,ch in enumerate(chunk_text(text, 4500)):
            rid = insert_generic(con, 'artifact_text_extract', source_id, f'ch_{i+1}', str(path), ch, {})
            insert_fts(con, 'artifact_fts', rid, ch)
    else: insert_generic(con, 'artifact_review_required', source_id, 'review', str(path), 'binary_or_no_text_artifact', {})
    signature(con, source_id, 'artifacts', str(path), {'ext':ext,'size_bytes':size,'text_chunks':len(chunk_text(text))})

def ingest_lane(con: sqlite3.Connection, lane_key: str, src: dict, tracker: dict, cb: ProgressCallback | None):
    path = src.get('path','')
    emit(cb, tracker, lane_key, 'extracting source', path or src.get('display_name',''), 1)
    if lane_key in {'local_code','github'}: return ingest_code(con, src, tracker, cb)
    if lane_key == 'docs': return ingest_docs(con, src)
    if lane_key == 'data_excel_csv': return ingest_data(con, src)
    if lane_key == 'ppt_presentation': return ingest_ppt(con, src)
    if lane_key == 'pdf_ocr': return ingest_pdf(con, src)
    if lane_key == 'images_ocr': return ingest_image(con, src)
    if lane_key == 'artifacts': return ingest_artifact(con, src)
    prefix = {'chat_lineage':'lineage','discussion':'discussion','analysis':'analysis','plan':'plan','mode':'mode','custom':'custom'}.get(lane_key, 'custom')
    return ingest_text_lanes(con, lane_key, src, prefix)

def _count(con, table):
    try: return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    except Exception: return 0

def _count_status(con, table, status):
    try: return con.execute(f"SELECT COUNT(*) FROM {table} WHERE value=?", (status,)).fetchone()[0]
    except Exception: return 0

def build_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress: ProgressCallback | None = None) -> dict:
    active = [s for s in sources if s.get('active', True)]
    root = brain_output_dir(workspace_dir, brain_name)
    project = root / 'project'; sectors = project / 'sectors'; pointers = project / 'pointers'; topo = project / 'topology'; receipts = root / 'receipts'
    for d in [project,sectors,pointers,topo,receipts]: d.mkdir(parents=True, exist_ok=True)
    router_db = project / 'project_router.sqlite'; init_router(router_db, brain_name)
    # estimate total progress
    total = 20 + sum(1 if s.get('lane_key') not in {'local_code','github'} else max(1, sum(1 for _ in iter_code_files(Path(s.get('path','')))) if Path(s.get('path','')).exists() else 1) for s in active)
    tracker = {'done':0,'total':max(total, 30),'start':time.time()}
    emit(progress, tracker, 'start', 'starting whale lane build', str(root), 1)
    # clear active sector dbs and rebuild from active sources
    by_lane: dict[str, list[dict]] = {}
    for s in active: by_lane.setdefault(s.get('lane_key','custom'), []).append(s)
    router = connect(router_db)
    router.execute('DELETE FROM source_registry'); router.execute('DELETE FROM sector_registry'); router.execute('DELETE FROM sector_pointer')
    for lane_key in list(LANE_TABLES):
        db = sectors / lane_key / f'{lane_key}_sector_v001.sqlite'
        if db.exists():
            # active database rebuild: removed sources disappear from active DB
            db.unlink()
        con = connect(db); init_sector(con, lane_key)
        for src in by_lane.get(lane_key, []):
            ingest_lane(con, lane_key, src, tracker, progress)
            src_path = src.get('path') or src.get('display_name','')
            sh = sha256_file(Path(src_path)) if src_path and Path(src_path).is_file() else sha256_bytes(str(src_path).encode())
            router.execute('INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)', (src.get('source_id') or stable_id('source', src_path), lane_key, LANE_LABEL.get(lane_key,lane_key), src.get('source_type',''), src.get('display_name',''), src_path, sh, 1, now()))
            router.execute('INSERT OR REPLACE INTO source_active_state VALUES(?,?,?,?,?,?,?,?)', (stable_id('state', src.get('source_id','')+lane_key), src.get('source_id') or stable_id('source', src_path), lane_key, sh, 1, 'ACTIVE', 'build', now()))
        con.commit(); con.close()
        sector_hash = sha256_file(db)
        mmd_required = 1 if lane_key in {'local_code','github'} and by_lane.get(lane_key) else 0
        router.execute('INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)', (f'sector_{lane_key}', lane_key, LANE_LABEL.get(lane_key,lane_key), str(db), 1, 'v001', sector_hash, now()))
        router.execute('INSERT OR REPLACE INTO sector_pointer VALUES(?,?,?,?,?,?,?)', (f'pointer_{lane_key}', lane_key, LANE_LABEL.get(lane_key,lane_key), str(db), mmd_required, 'ACTIVE_WITH_DATA' if by_lane.get(lane_key) else 'SCHEMA_READY_WAITING', now()))
        (pointers / f'{lane_key}_pointer.json').write_text(json.dumps({'lane_key':lane_key,'lane_label':LANE_LABEL.get(lane_key,lane_key),'sector_db':f'project/sectors/{lane_key}/{lane_key}_sector_v001.sqlite','mmd_required':bool(mmd_required),'status':'ACTIVE_WITH_DATA' if by_lane.get(lane_key) else 'SCHEMA_READY_WAITING','schema_tables':LANE_TABLES[lane_key]}, indent=2), encoding='utf-8')
    (project/'sector_index.json').write_text(json.dumps([{'lane_key':k,'lane_label':LANE_LABEL.get(k,k),'sector_db':f'project/sectors/{k}/{k}_sector_v001.sqlite'} for k in LANE_TABLES], indent=2), encoding='utf-8')
    (project/'project_pointer.json').write_text(json.dumps({'router':'project/project_router.sqlite','sectors':'project/sectors','pointers':'project/pointers','policy':'generated project sectors are writable only by explicit user command; env/uop/project template locked'}, indent=2), encoding='utf-8')
    router.commit(); router.close()
    (receipts / 'build_receipt.md').write_text(f'# Build Receipt\n\nbrain={brain_name}\ncreated={now()}\nactive_sources={len(active)}\ncode_asset_law=no image glb video study inside code lane\n', encoding='utf-8')
    emit(progress, tracker, 'done', 'brain build complete', str(root), tracker['total'])
    return {'brain_root':str(root),'router_db':str(router_db),'active_sources':len(active)}

def unload_and_rebuild(workspace_dir: str, brain_name: str, sources: list[dict], remove_ids: list[str], progress: ProgressCallback | None = None) -> dict:
    root = brain_output_dir(workspace_dir, brain_name); receipts = root / 'receipts'; receipts.mkdir(parents=True, exist_ok=True)
    for s in sources:
        if s.get('source_id') in set(remove_ids): s['active'] = False
    receipt = receipts / f'unload_receipt_{int(time.time())}.md'
    receipt.write_text('# Unload Receipt\n\n' + '\n'.join(remove_ids) + '\n', encoding='utf-8')
    return build_brain(workspace_dir, brain_name, sources, progress)

# ---------------- MMD/render/export ----------------

def write_mmds(workspace_dir: str, brain_name: str) -> dict:
    root = brain_output_dir(workspace_dir, brain_name); topo = root/'project'/'topology'; topo.mkdir(parents=True, exist_ok=True)
    router_db = root/'project'/'project_router.sqlite'
    code_db = root/'project'/'sectors'/'local_code'/'local_code_sector_v001.sqlite'
    # Code MMD: route/page -> route file -> imports/services -> deps/artifacts, no builder-process mixing.
    if code_db.exists():
        con = sqlite3.connect(code_db)
        lines = ['flowchart LR','  classDef route fill:#fff4d6,stroke:#8a5a00,color:#111;','  classDef code fill:#eaffea,stroke:#275,color:#111;','  classDef dep fill:#fef3c7,stroke:#92400e,color:#111;','  classDef artifact fill:#f3e8ff,stroke:#635,color:#111;','  ROOT["coded project"]','  ROUTES["routes/pages"]','  UI["UI/UX code"]','  API["API/backend/services"]','  DEPS["tools/dependencies"]','  ARTS["read-only text artifacts"]','  ROOT --> ROUTES --> UI --> API --> DEPS','  UI --> ARTS','  API --> ARTS']
        routes = con.execute("SELECT value,path,metadata_json FROM app_route LIMIT 80").fetchall() if _table_exists(con,'app_route') else []
        prev = 'ROUTES'
        for i,(val,path,meta) in enumerate(routes):
            node=f'R{i}'; file=f'F{i}'
            m = json.loads(meta or '{}')
            route = m.get('route_path') or val or path
            label = _mmd_label(route, 60); flabel = _mmd_label(path, 70)
            lines += [f'  {prev} --> {node}["{label}"]:::route', f'  {node} --> {file}["{flabel}"]:::code', f'  {file} --> API']
            prev = node
        deps = con.execute("SELECT name,value FROM dependency_item LIMIT 20").fetchall() if _table_exists(con,'dependency_item') else []
        for i,(n,v) in enumerate(deps): lines.append(f'  DEPS --> D{i}["{_mmd_label(n,50)}\\n{_mmd_label(v,40)}"]:::dep')
        arts = con.execute("SELECT name,metadata_json FROM project_artifact LIMIT 20").fetchall() if _table_exists(con,'project_artifact') else []
        for i,(n,m) in enumerate(arts): lines.append(f'  ARTS --> A{i}["{_mmd_label(n,60)}"]:::artifact')
        (topo/'local_code_lane.mmd').write_text('\n'.join(lines)+'\n', encoding='utf-8')
        con.close()
    # Project master MMD: package/sector/pointer topology only
    if router_db.exists():
        con = sqlite3.connect(router_db)
        rows = con.execute('SELECT lane_key,lane_label FROM sector_registry ORDER BY lane_label').fetchall()
        lines = ['flowchart TD','  PKG["one-upload package"]','  LAW["locked env + uop + project template"]','  GEN["generated writable project sectors"]','  PTR["pointers"]','  DB["sector SQLite DBs"]','  PKG --> LAW','  PKG --> GEN --> PTR','  GEN --> DB']
        for i,(k,l) in enumerate(rows): lines.append(f'  DB --> S{i}["{_mmd_label(l,60)}"]')
        (topo/'project_master_topology.mmd').write_text('\n'.join(lines)+'\n', encoding='utf-8')
        con.close()
    return {'topology':str(topo)}

def _mmd_label(s: Any, limit=80) -> str:
    x = re.sub(r'["<>|`{}\[\]]+', ' ', str(s or ''))
    x = re.sub(r'\s+', ' ', x).strip()
    return x[:limit]

def _table_exists(con, table):
    return con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone() is not None

def render_topology(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None) -> dict:
    write_mmds(workspace_dir, brain_name)
    root = brain_output_dir(workspace_dir, brain_name); topo = root/'project'/'topology'; manifest=[]
    mmdc = shutil.which('mmdc') or shutil.which('mmdc.cmd') or shutil.which('mmdc.exe')
    if not mmdc:
        (root/'receipts'/'render_blocked_receipt.md').parent.mkdir(parents=True, exist_ok=True)
        (root/'receipts'/'render_blocked_receipt.md').write_text('MERMAID_CLI_MISSING: install @mermaid-js/mermaid-cli\n', encoding='utf-8')
        return {'topology':str(topo),'rendered':False,'reason':'MERMAID_CLI_MISSING'}
    mmds = [p for p in topo.glob('*.mmd') if p.stem in {'local_code_lane','project_master_topology'}]
    tracker={'done':0,'total':len(mmds)*3+1,'start':time.time()}
    for mmd in mmds:
        svg = mmd.with_suffix('.svg'); png = mmd.with_suffix('.png'); png4 = mmd.with_name(mmd.stem + '_4K.png')
        for out,args in [(svg,[]),(png4,['-w','7680','-H','4320'])]:
            emit(progress, tracker, 'render', 'rendering ' + out.name, str(out), 1)
            run = subprocess.run([mmdc,'-i',str(mmd),'-o',str(out),'-b','white'] + args, capture_output=True, text=True, timeout=1200)
            if run.returncode != 0: raise RuntimeError(run.stderr[:3000])
        shutil.copy2(png4, png)
        manifest.append({'mmd':str(mmd),'svg':str(svg),'png':str(png),'png_4k':str(png4)})
    (topo/'render_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return {'topology':str(topo),'rendered':True,'manifest':str(topo/'render_manifest.json')}

def find_env_resource_root() -> Path | None:
    candidates = []
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidates += [parent/'resources'/'public_model_env15', parent/'sqlite_brain_builder'/'resources'/'public_model_env15']
    if hasattr(sys, '_MEIPASS'):
        candidates.append(Path(sys._MEIPASS)/'sqlite_brain_builder'/'resources'/'public_model_env15')
    for c in candidates:
        if (c/'.uepc_env').exists() or (c/'env').exists(): return c
    return None

def zip_dir_stored_png(src: Path, zip_path: Path):
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w') as z:
        for p in sorted(src.rglob('*')):
            if not p.is_file() or p == zip_path: continue
            rel = p.relative_to(src).as_posix()
            comp = zipfile.ZIP_STORED if p.suffix.lower() == '.png' else zipfile.ZIP_DEFLATED
            z.write(p, rel, compress_type=comp)
    return sha256_file(zip_path)

def copytree_overlay(src: Path, dst: Path):
    if not src.exists(): return
    for p in src.rglob('*'):
        if p.is_file():
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)

def export_one_upload_package(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None) -> dict:
    root = brain_output_dir(workspace_dir, brain_name); packages = root/'packages'; packages.mkdir(parents=True, exist_ok=True)
    stage = packages/(slugify_name(brain_name)+'_one_upload_package_v001')
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    envroot = find_env_resource_root()
    if envroot: copytree_overlay(envroot, stage)
    else: (stage/'ENV15_RESOURCE_MISSING.txt').write_text('public_model_env15 resource missing; package contains generated project only\n', encoding='utf-8')
    # overlay generated project only, not app source
    copytree_overlay(root/'project', stage/'project')
    copytree_overlay(root/'receipts', stage/'receipts')
    (stage/'FLASH_ME_FIRST_SINGLE_PROMPT.txt').write_text(_flash_prompt(brain_name), encoding='utf-8')
    (stage/'README_NEXT_PROMPT.txt').write_text('Read FLASH_ME_FIRST_SINGLE_PROMPT.txt first. Env/UOP/project_template locked; generated project sectors fillable by explicit user command.\n', encoding='utf-8')
    # refresh manifest and fix duplicate router active rows by rebuilding router rows already unique by sector_id.
    manifest=[]
    for p in sorted(stage.rglob('*')):
        if p.is_file() and 'PROJECT_BRAIN_PACKAGE_MANIFEST.json' not in p.name:
            manifest.append({'path':p.relative_to(stage).as_posix(),'size':p.stat().st_size,'sha256':sha256_file(p),'zip_compression_expected':'ZIP_STORED' if p.suffix.lower()=='.png' else 'ZIP_DEFLATED'})
    (stage/'manifests').mkdir(exist_ok=True)
    (stage/'manifests'/'PROJECT_BRAIN_PACKAGE_MANIFEST.json').write_text(json.dumps({'created_at':now(),'files':manifest}, indent=2), encoding='utf-8')
    zip_path = packages/(stage.name + '.zip')
    digest = zip_dir_stored_png(stage, zip_path)
    return {'package_folder':str(stage),'package_zip':str(zip_path),'sha256':digest}

def _flash_prompt(brain_name: str) -> str:
    return f'''UEPC-ENV15-FLASH-BOOT-001 | Mode: flash_env + validation | Category: one-upload env flash

CHAT_NAME:\n[{brain_name}]

BRIEF_NATURE_OF_CHAT:\n[SQLite Brain Builder package: locked Env/UOP/project-template law + writable generated project sector DBs]

I uploaded one Env14/UEPC one-upload ZIP. Do not ask for individual files. First inspect boot pointers, then project/project_router.sqlite, project/sector_index.json, project/pointers/, project/sectors/, project/topology/.

Env and UOP are locked/read-only governance. Public project template is locked/read-only reference law. Generated project sector DBs are writable only by explicit user command. Chat window is display only. Preserve receipts.
'''

def conjoin_project_db(project_root: Path, out_db: Path):
    if out_db.exists(): out_db.unlink()
    con = connect(out_db)
    con.execute('CREATE TABLE sector_blob(lane_key TEXT PRIMARY KEY, rel_path TEXT, sha256 TEXT, payload BLOB)')
    con.execute('CREATE TABLE conjoin_manifest(key TEXT PRIMARY KEY, value TEXT)')
    sectors = project_root/'sectors'
    for db in sectors.rglob('*.sqlite'):
        lane = db.parent.name
        con.execute('INSERT OR REPLACE INTO sector_blob VALUES(?,?,?,?)', (lane, db.relative_to(project_root).as_posix(), sha256_file(db), db.read_bytes()))
        try:
            src = sqlite3.connect(db)
            for (tname,) in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                safe = re.sub(r'[^A-Za-z0-9_]+','_', lane + '__' + tname)
                rows = src.execute(f'SELECT * FROM "{tname}"').fetchall()
                cols = [d[0] for d in src.execute(f'SELECT * FROM "{tname}" LIMIT 0').description]
                con.execute(f'CREATE TABLE IF NOT EXISTS "{safe}"({",".join(["c"+str(i)+" TEXT" for i in range(len(cols))])})')
                for row in rows[:20000]: con.execute(f'INSERT INTO "{safe}" VALUES({",".join(["?"]*len(cols))})', tuple(str(x) if x is not None else '' for x in row))
            src.close()
        except Exception as e:
            con.execute('INSERT OR REPLACE INTO conjoin_manifest VALUES(?,?)', (lane+'_copy_warning', str(e)))
    con.execute('INSERT OR REPLACE INTO conjoin_manifest VALUES(?,?)', ('created_at', now()))
    con.commit(); con.close()

def export_gemini_exact10(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None) -> dict:
    normal = export_one_upload_package(workspace_dir, brain_name, progress)
    root = brain_output_dir(workspace_dir, brain_name); packages = root/'packages'; stage = packages/(slugify_name(brain_name)+'_gemini_exact10')
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    envroot = find_env_resource_root()
    public_zip = stage/'PUBLIC_ENV_UOP_PROJECT_LOCKED.zip'
    if envroot: zip_dir_stored_png(envroot, public_zip)
    else: public_zip.write_text('env resource missing', encoding='utf-8')
    conjoined = stage/'GENERATED_PROJECT_CONJOINED.sqlite'
    conjoin_project_db(root/'project', conjoined)
    # dot files/pointers
    for name in ['.uepc_env','.uepc_project','.uepc_profile']:
        data = ''
        if envroot and (envroot/name).exists(): data = (envroot/name).read_text(encoding='utf-8', errors='replace')
        (stage/name).write_text(data or f'{name}=MISSING_IN_RESOURCE\n', encoding='utf-8')
    pointers = {'project_pointer': _read_json(root/'project'/'project_pointer.json'), 'sector_index': _read_json(root/'project'/'sector_index.json'), 'normal_package': normal}
    (stage/'UPEC_POINTERS_AND_SECTOR_INDEX.json').write_text(json.dumps(pointers, indent=2), encoding='utf-8')
    (stage/'GEMINI_FLASH_PROMPT.txt').write_text('UEPC-GEMINI-EXACT10-BOOT | Read the 10-file package. PUBLIC_ENV_UOP_PROJECT_LOCKED.zip is locked reference law. GENERATED_PROJECT_CONJOINED.sqlite is the generated project brain. Do not mutate env/uop/project-template.\n', encoding='utf-8')
    for srcname, outname in [('env_mmd.mmd','env_mmd.mmd'),('uop_mmd.mmd','uop_mmd.mmd')]:
        found = None
        if envroot:
            for p in envroot.rglob(srcname): found = p; break
        (stage/outname).write_text(found.read_text(encoding='utf-8', errors='replace') if found else f'flowchart TD\n  MISSING[{srcname} missing]\n', encoding='utf-8')
    files = [p.name for p in stage.iterdir() if p.is_file()]
    manifest = {'created_at':now(),'exact_file_count':10,'files':sorted(files),'normal_package':normal}
    (stage/'GEMINI_EXACT10_MANIFEST.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    # enforce exactly 10 root files
    files = sorted([p for p in stage.iterdir() if p.is_file()], key=lambda p:p.name)
    if len(files) != 10:
        raise RuntimeError(f'GEMINI_EXACT10_FILE_COUNT_FAILED: {len(files)} files: {[p.name for p in files]}')
    zip_path = packages/(stage.name+'.zip')
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in files: z.write(p, p.name)
    return {'gemini_package_folder':str(stage),'gemini_package_zip':str(zip_path),'file_count':10,'sha256':sha256_file(zip_path)}

def _read_json(path: Path):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except Exception: return {}
