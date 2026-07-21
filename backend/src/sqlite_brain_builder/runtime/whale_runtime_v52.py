
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
from typing import Callable, Iterable

ProgressCallback = Callable[[dict], None]

CODE_NON_STUDY_ASSETS = {
    '.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.gif','.ico',
    '.glb','.gltf','.fbx','.obj','.stl','.blend',
    '.mp4','.mov','.avi','.mkv','.webm','.mp3','.wav','.flac','.ogg'
}
CODE_TEXT_EXTS = {
    '.py','.js','.jsx','.ts','.tsx','.css','.scss','.html','.htm','.json','.jsonl',
    '.yaml','.yml','.sql','.md','.txt','.toml','.ini','.cfg','.conf','.env','.example',
    '.dockerfile','.ps1','.bat','.cmd','.sh','.csv','.tsv','.xml','.svg','.mmd','.log',
    '.gitignore','.gitattributes','.lock','.bak','.bak_news_patch','.ipynb'
}
READONLY_ARTIFACT_EXTS = {'.json','.jsonl','.csv','.tsv','.parquet','.mmd','.svg','.txt','.md','.html','.xml','.ipynb','.log'}
BINARY_UNSUPPORTED = {'.zip','.rar','.7z','.tar','.gz','.bz2','.xz','.exe','.dll','.so','.dylib','.bin','.pkl','.pickle','.db','.sqlite','.sqlite3'}
SKIP_DIR_NAMES = {'.git','node_modules','.next','dist','build','__pycache__','.venv','venv','env','.pytest_cache'}

LANES = {
    'github': 'GitHub', 'local_code': 'Local Code', 'chat_lineage': 'Chat Lineage',
    'discussion': 'Discussion', 'analysis': 'Analysis', 'plan': 'Plan', 'mode': 'Mode',
    'docs': 'Docs', 'data_excel_csv': 'Data / Excel / CSV', 'ppt_presentation': 'PPT / Presentation',
    'pdf_ocr': 'PDF / OCR', 'images_ocr': 'Images / OCR', 'artifacts': 'Artifacts', 'custom': 'Custom'
}

PYTHON_PARSER_PACKAGES = {
    'docx': 'python-docx',
    'pptx': 'python-pptx',
    'openpyxl': 'openpyxl',
    'pypdf': 'pypdf',
    'fitz': 'pymupdf',
    'PIL': 'pillow',
    'pytesseract': 'pytesseract',
}


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def slugify_name(name: str) -> str:
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', (name or 'new_brain').strip()).strip('_').lower()
    return s or 'new_brain'


def normalize_workspace_dir(path: str | Path) -> Path:
    p = Path(path).expanduser()
    while p.name.lower() in {'brains','brain'}:
        p = p.parent
    return p


def brain_output_dir(workspace_dir: str | Path, brain_name: str) -> Path:
    return normalize_workspace_dir(workspace_dir) / slugify_name(brain_name)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(chunk_size), b''):
            h.update(block)
    return h.hexdigest()


def stable_id(prefix: str, value: str) -> str:
    return prefix + '_' + hashlib.sha1(str(value).encode('utf-8', errors='ignore')).hexdigest()[:20]


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    return con


def emit(cb: ProgressCallback | None, tracker: dict, stage: str, task: str, file: str = '', inc: int = 1):
    if not tracker:
        tracker = {'done': 0, 'total': 100, 'start': time.time()}
    tracker['done'] = min(int(tracker.get('total', 100)), int(tracker.get('done', 0)) + inc)
    total = max(1, int(tracker.get('total', 100)))
    done = int(tracker.get('done', 0))
    pct = int(done * 100 / total)
    elapsed = max(0.1, time.time() - float(tracker.get('start', time.time())))
    eta = int(max(0, total - done) * elapsed / max(1, done)) if done else None
    if cb:
        cb({
            'stage': stage, 'task': task, 'file': str(file), 'done': done, 'total': total,
            'percent': pct, 'elapsed_seconds': int(elapsed), 'eta_seconds': eta,
            'finish_epoch': int(time.time() + eta) if eta is not None else None,
        })


def make_router(router_db: Path, brain_name: str):
    con = connect(router_db)
    con.executescript('''
    CREATE TABLE IF NOT EXISTS brain_manifest(brain_id TEXT PRIMARY KEY, brain_name TEXT, brain_slug TEXT, created_at TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS source_registry(source_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, source_type TEXT, display_name TEXT, path TEXT, source_hash TEXT, active_bool INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sector_registry(sector_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, sector_db_path TEXT, active_bool INTEGER, version TEXT, sector_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sector_pointer(pointer_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, sector_db_path TEXT, mmd_required INTEGER, status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS source_active_state(active_state_id TEXT PRIMARY KEY, source_id TEXT, lane_key TEXT, source_hash TEXT, active_bool INTEGER, state TEXT, reason TEXT, changed_at TEXT);
    CREATE TABLE IF NOT EXISTS unload_session(unload_session_id TEXT PRIMARY KEY, started_at TEXT, completed_at TEXT, status TEXT, affected_source_count INTEGER, receipt_hash TEXT);
    CREATE TABLE IF NOT EXISTS package_manifest(package_id TEXT PRIMARY KEY, package_path TEXT, created_at TEXT, package_hash TEXT);
    CREATE TABLE IF NOT EXISTS workspace_capability_profile(profile_id TEXT PRIMARY KEY, profile_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS fallback_tool_registry(tool_category TEXT PRIMARY KEY, primary_tool TEXT, fallback_tool TEXT, metadata_only_fallback TEXT, detected_path TEXT, version TEXT, status TEXT, last_tested TEXT, warning_message TEXT);
    ''')
    con.execute('INSERT OR REPLACE INTO brain_manifest VALUES(?,?,?,?,?)', (stable_id('brain', brain_name), brain_name, slugify_name(brain_name), now(), 'ACTIVE'))
    con.commit(); con.close()


def make_code_schema(con: sqlite3.Connection):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS source_file(file_id TEXT PRIMARY KEY, logical_path TEXT, extension TEXT, size_bytes INTEGER, sha256 TEXT, lane_status TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS source_byte_coverage(coverage_id TEXT PRIMARY KEY, file_id TEXT, coverage_status TEXT, size_bytes INTEGER, covered_bytes INTEGER, reason TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS code_byte_span(span_id TEXT PRIMARY KEY, file_id TEXT, logical_path TEXT, start_byte INTEGER, end_byte INTEGER, span_sha256 TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS code_file(file_id TEXT PRIMARY KEY, canonical_path TEXT, language TEXT, extension TEXT, current_sha256 TEXT, is_active INTEGER);
    CREATE TABLE IF NOT EXISTS code_file_role(role_id TEXT PRIMARY KEY, file_id TEXT, role_name TEXT, evidence TEXT);
    CREATE TABLE IF NOT EXISTS code_file_version(file_version_id TEXT PRIMARY KEY, file_id TEXT, commit_sha TEXT, path_at_commit TEXT, raw_file_sha256 TEXT, normalized_text_sha256 TEXT, language TEXT, extension TEXT, line_count INTEGER, byte_count INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS code_line_snapshot(line_id TEXT PRIMARY KEY, file_version_id TEXT, file_id TEXT, commit_sha TEXT, line_number INTEGER, line_text TEXT, line_sha256 TEXT, normalized_line_sha256 TEXT, indent_level INTEGER, is_blank INTEGER, is_comment INTEGER, start_byte INTEGER, end_byte INTEGER, search_text TEXT);
    CREATE TABLE IF NOT EXISTS code_chunk(chunk_id TEXT PRIMARY KEY, file_version_id TEXT, file_id TEXT, chunk_type TEXT, language TEXT, role_name TEXT, start_line INTEGER, end_line INTEGER, chunk_text TEXT, chunk_sha256 TEXT);
    CREATE TABLE IF NOT EXISTS code_symbol(symbol_id TEXT PRIMARY KEY, symbol_name TEXT, symbol_type TEXT, file_version_id TEXT, file_id TEXT, language TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, symbol_sha256 TEXT);
    CREATE TABLE IF NOT EXISTS app_route(route_id TEXT PRIMARY KEY, route_path TEXT, route_type TEXT, framework_guess TEXT, file_id TEXT, file_version_id TEXT, method TEXT, input_contract TEXT, output_contract TEXT, route_sha256 TEXT);
    CREATE TABLE IF NOT EXISTS dependency_manifest(manifest_id TEXT PRIMARY KEY, file_id TEXT, manifest_type TEXT, ecosystem TEXT, path TEXT, sha256 TEXT);
    CREATE TABLE IF NOT EXISTS dependency_item(dependency_id TEXT PRIMARY KEY, manifest_id TEXT, package_name TEXT, version_spec TEXT, ecosystem TEXT, dev_or_runtime TEXT);
    CREATE TABLE IF NOT EXISTS code_import_edge(edge_id TEXT PRIMARY KEY, from_file_id TEXT, from_path TEXT, import_target TEXT, import_type TEXT, line_number INTEGER);
    CREATE TABLE IF NOT EXISTS git_commit(commit_sha TEXT PRIMARY KEY, short_sha TEXT, author_name TEXT, author_email_hash TEXT, commit_time TEXT, message TEXT, commit_order INTEGER);
    CREATE TABLE IF NOT EXISTS git_file_change(change_id TEXT PRIMARY KEY, commit_sha TEXT, path TEXT, change_type TEXT, insertions INTEGER, deletions INTEGER);
    CREATE TABLE IF NOT EXISTS project_artifact(artifact_id TEXT PRIMARY KEY, file_id TEXT, artifact_type TEXT, path TEXT, artifact_sha256 TEXT, semantic_status TEXT, metadata_json TEXT);
    CREATE TABLE IF NOT EXISTS artifact_relation_edge(edge_id TEXT PRIMARY KEY, artifact_id TEXT, related_entity_type TEXT, related_entity_id TEXT, relation_type TEXT, confidence TEXT);
    CREATE TABLE IF NOT EXISTS source_structure_signature(signature_id TEXT PRIMARY KEY, source_id TEXT, signature_type TEXT, key_metrics_json TEXT, structure_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS review_required_item(review_id TEXT PRIMARY KEY, source_id TEXT, path TEXT, reason TEXT, status TEXT, created_at TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(entity_id, text);
    CREATE VIRTUAL TABLE IF NOT EXISTS line_fts USING fts5(entity_id, text);
    CREATE VIRTUAL TABLE IF NOT EXISTS symbol_fts USING fts5(entity_id, text);
    CREATE VIRTUAL TABLE IF NOT EXISTS route_fts USING fts5(entity_id, text);
    CREATE VIRTUAL TABLE IF NOT EXISTS commit_fts USING fts5(entity_id, text);
    CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts USING fts5(entity_id, text);
    ''')
    con.commit()


def make_generic_schema(con: sqlite3.Connection, lane_key: str):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS generic_source(source_id TEXT PRIMARY KEY, lane_key TEXT, display_name TEXT, path TEXT, sha256 TEXT, size_bytes INTEGER, active_bool INTEGER, created_at TEXT);
    CREATE TABLE IF NOT EXISTS generic_chunk(chunk_id TEXT PRIMARY KEY, source_id TEXT, chunk_order INTEGER, chunk_text TEXT, chunk_sha256 TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS source_structure_signature(signature_id TEXT PRIMARY KEY, source_id TEXT, signature_type TEXT, key_metrics_json TEXT, structure_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS review_required_item(review_id TEXT PRIMARY KEY, source_id TEXT, path TEXT, reason TEXT, status TEXT, created_at TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS generic_fts USING fts5(entity_id, text);
    ''')
    # Known V3.1 lane tables, kept as real tables not dummy-only.
    for table in {
        'chat_lineage':['lineage_source','lineage_turn','lineage_prompt','lineage_response','lineage_decision','lineage_delta','lineage_requirement','lineage_artifact_reference','lineage_hard_gate'],
        'discussion':['discussion_source','discussion_turn','discussion_item','discussion_decision','discussion_delta','discussion_hard_gate','discussion_artifact_reference','discussion_next_action'],
        'analysis':['analysis_source','analysis_claim','analysis_supporting_evidence','analysis_risk','analysis_alternative','analysis_open_question','analysis_accepted_decision','analysis_blocked_item'],
        'plan':['plan_source','plan_phase','plan_milestone','plan_task','plan_dependency','plan_owner','plan_status','plan_acceptance_criteria','plan_blocker','plan_next_action'],
        'mode':['mode_source','mode_rule','mode_scope','mode_gate','mode_allowed_action','mode_blocked_action','mode_trigger','mode_response_template','mode_priority','mode_supersede_ledger'],
        'docs':['doc_file','doc_structure','doc_heading','doc_paragraph','doc_chunk','doc_table_extract','doc_image_reference'],
        'data_excel_csv':['sheet_workbook','sheet_tab','sheet_range','sheet_table','sheet_formula','sheet_formula_dependency_edge','sheet_cell_sample','sheet_chart_metadata','csv_header','csv_row_sample','data_structure_signature'],
        'ppt_presentation':['ppt_file','ppt_slide','ppt_shape','ppt_text_block','ppt_notes','ppt_table','ppt_image_reference','ppt_structure_signature'],
        'pdf_ocr':['pdf_file','pdf_page','pdf_text_block','pdf_image_block','pdf_ocr_run','pdf_ocr_block','pdf_ocr_line','pdf_structure_signature'],
        'images_ocr':['image_file','image_metadata','image_ocr_run','image_ocr_block','image_ocr_line','image_review_region'],
        'artifacts':['project_artifact','artifact_metadata','artifact_text_extract','artifact_relation_edge','artifact_review_required'],
        'custom':['custom_source','custom_item','custom_evidence','custom_decision','custom_next_action'],
    }.get(lane_key, ['custom_source','custom_item','custom_evidence']):
        con.execute(f'''CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT, metadata_json TEXT, active_bool INTEGER DEFAULT 1, created_at TEXT)''')
    con.commit()


def insert_fts(con: sqlite3.Connection, table: str, entity_id: str, text: str):
    try:
        con.execute(f'INSERT INTO {table}(entity_id, text) VALUES(?,?)', (entity_id, text))
    except Exception:
        pass


def detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        '.py':'python','.js':'javascript','.jsx':'javascript-react','.ts':'typescript','.tsx':'typescript-react',
        '.css':'css','.scss':'scss','.html':'html','.json':'json','.jsonl':'jsonl','.csv':'csv','.tsv':'tsv',
        '.md':'markdown','.txt':'text','.yaml':'yaml','.yml':'yaml','.sql':'sql','.xml':'xml','.svg':'svg','.mmd':'mermaid'
    }.get(ext, ext.strip('.') or path.name.lower())


def code_role(rel: str, ext: str, text: str) -> str:
    low = rel.lower()
    if CODE_NON_STUDY_ASSETS.__contains__(ext):
        return 'ARTIFACT_PAYLOAD_NOT_STUDIED'
    if '/api/' in low or low.endswith('/route.ts') or low.endswith('/route.js') or 'fastapi' in text.lower():
        return 'API_BACKEND_ROUTE'
    if '/app/' in '/' + low or '/pages/' in '/' + low or low.endswith('page.tsx') or low.endswith('page.jsx'):
        return 'UI_PAGE_ROUTE'
    if '/components/' in low or 'export default function' in text[:2000] or 'return <' in text[:4000]:
        return 'UI_UX_COMPONENT'
    if any(x in low for x in ['lib/','utils/','service','client','server']):
        return 'SERVICE_OR_UTILITY'
    if any(x in low for x in ['data','db','model','schema','matrix','forecast','features']):
        return 'DATA_DB_LAYER'
    if any(x in low for x in ['test','spec','__tests__']):
        return 'TEST_QA'
    if ext in {'.md','.txt'}:
        return 'DOCS_OR_NOTES'
    if path_name(low) in {'package.json','requirements.txt','pyproject.toml'} or 'config' in low:
        return 'CONFIG_BUILD_TOOLING'
    return 'GENERAL_CODE'


def path_name(low: str) -> str:
    return low.rsplit('/', 1)[-1]


def probably_text(raw: bytes) -> bool:
    if b'\x00' in raw[:8192]:
        return False
    if not raw:
        return True
    sample = raw[:8192]
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126 or b >= 128)
    return printable / max(1, len(sample)) >= 0.80


def decode_raw(raw: bytes) -> tuple[str, str]:
    for enc in ('utf-8-sig','utf-8','utf-16','utf-16-le','utf-16-be'):
        try:
            return raw.decode(enc), enc
        except Exception:
            pass
    return raw.decode('utf-8', errors='replace'), 'utf-8-replace'


def raw_line_records(raw: bytes, encoding: str) -> list[dict]:
    raw_lines = raw.splitlines(keepends=True)
    if not raw_lines and raw:
        raw_lines = [raw]
    rows = []
    cur = 0
    for idx, raw_line in enumerate(raw_lines, start=1):
        try:
            line = raw_line.decode(encoding if encoding != 'utf-8-replace' else 'utf-8', errors='replace')
        except Exception:
            line = raw_line.decode('utf-8', errors='replace')
        start = cur; end = cur + len(raw_line); cur = end
        stripped = line.strip()
        rows.append({
            'n': idx, 'text': line, 'raw_sha': sha256_bytes(raw_line),
            'norm_sha': sha256_bytes(stripped.encode('utf-8', errors='replace')),
            'start': start, 'end': end, 'blank': 1 if not stripped else 0,
            'comment': 1 if stripped.startswith(('#','//','/*','*','--')) else 0,
            'indent': len(line) - len(line.lstrip(' \t')),
        })
    if not rows:
        rows.append({'n':1,'text':'','raw_sha':sha256_bytes(b''),'norm_sha':sha256_bytes(b''),'start':0,'end':0,'blank':1,'comment':0,'indent':0})
    return rows


def iter_project_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            yield Path(dirpath) / fn


def add_byte_spans(con: sqlite3.Connection, file_id: str, rel: str, raw: bytes, span_size: int = 65536) -> int:
    if not raw:
        con.execute('INSERT OR REPLACE INTO code_byte_span VALUES(?,?,?,?,?,?,?)', (stable_id('span', file_id + ':0'), file_id, rel, 0, 0, sha256_bytes(b''), now()))
        return 1
    count = 0
    for start in range(0, len(raw), span_size):
        block = raw[start:start+span_size]
        end = start + len(block)
        con.execute('INSERT OR REPLACE INTO code_byte_span VALUES(?,?,?,?,?,?,?)', (stable_id('span', f'{file_id}:{start}:{end}'), file_id, rel, start, end, sha256_bytes(block), now()))
        count += 1
    return count


def add_code_file(con: sqlite3.Connection, root: Path, path: Path):
    rel = path.relative_to(root).as_posix()
    ext = path.suffix.lower()
    file_id = stable_id('file', rel)
    raw = path.read_bytes()
    size = len(raw)
    raw_hash = sha256_bytes(raw)
    con.execute('INSERT OR REPLACE INTO source_file VALUES(?,?,?,?,?,?,?)', (file_id, rel, ext, size, raw_hash, 'REGISTERED_RAW_BYTE_COVERED', now()))
    span_count = add_byte_spans(con, file_id, rel, raw)

    if ext in CODE_NON_STUDY_ASSETS:
        con.execute('INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)', (stable_id('coverage', file_id), file_id, 'CODE_ASSET_NOT_STUDIED_HASH_ONLY', size, size, f'code lane asset skipped from study; byte spans={span_count}', now()))
        con.execute('INSERT OR REPLACE INTO project_artifact VALUES(?,?,?,?,?,?,?)', (stable_id('artifact', rel), file_id, ext or 'asset', rel, raw_hash, 'CODE_ASSET_NOT_STUDIED_HASH_ONLY', json.dumps({'byte_spans': span_count})))
        return

    name_low = path.name.lower()
    allowed_name = name_low in {'dockerfile','makefile','requirements.txt','package.json','pyproject.toml','.gitignore','.gitattributes','license','readme'}
    if ext in BINARY_UNSUPPORTED or not (ext in CODE_TEXT_EXTS or allowed_name or probably_text(raw)):
        con.execute('INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)', (stable_id('coverage', file_id), file_id, 'BYTE_COVERED_UNSUPPORTED_BINARY_HASH_ONLY', size, size, f'unsupported binary in code lane; byte spans={span_count}', now()))
        con.execute('INSERT OR REPLACE INTO project_artifact VALUES(?,?,?,?,?,?,?)', (stable_id('artifact', rel), file_id, ext or 'binary', rel, raw_hash, 'BYTE_COVERED_UNSUPPORTED_BINARY_HASH_ONLY', json.dumps({'byte_spans': span_count})))
        return

    text, encoding = decode_raw(raw)
    line_rows = raw_line_records(raw, encoding)
    norm_hash = sha256_bytes(text.replace('\r\n','\n').encode('utf-8', errors='replace'))
    lang = detect_language(path)
    role = code_role(rel, ext, text)
    fv = stable_id('fv', rel + raw_hash)

    con.execute('INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)', (stable_id('coverage', file_id), file_id, 'LOSSLESS_TEXT_CHUNKED_LINE_INDEXED_FULL_BYTES', size, size, f'full file text indexed; byte spans={span_count}; lines={len(line_rows)}; encoding={encoding}', now()))
    con.execute('INSERT OR REPLACE INTO code_file VALUES(?,?,?,?,?,?)', (file_id, rel, lang, ext, raw_hash, 1))
    con.execute('INSERT OR REPLACE INTO code_file_role VALUES(?,?,?,?)', (stable_id('role', file_id + role), file_id, role, 'deterministic path/ext/content signal'))
    con.execute('INSERT OR REPLACE INTO code_file_version VALUES(?,?,?,?,?,?,?,?,?,?,?)', (fv, file_id, '', rel, raw_hash, norm_hash, lang, ext, len(line_rows), size, now()))

    for row in line_rows:
        line_id = stable_id('line', f'{fv}:{row["n"]}:{row["raw_sha"]}')
        con.execute('INSERT OR REPLACE INTO code_line_snapshot VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (line_id, fv, file_id, '', row['n'], row['text'], row['raw_sha'], row['norm_sha'], row['indent'], row['blank'], row['comment'], row['start'], row['end'], row['text']))
        insert_fts(con, 'line_fts', line_id, row['text'])

    lines = [r['text'] for r in line_rows]
    for start in range(0, len(lines), 120):
        block_lines = lines[start:start+120]
        block = ''.join(block_lines)
        start_line = start + 1
        end_line = start + len(block_lines)
        chunk_hash = sha256_bytes(block.encode('utf-8', errors='replace'))
        chunk_id = stable_id('chunk', f'{fv}:{start_line}:{end_line}:{chunk_hash}')
        chunk_type = chunk_type_for(rel, role, block)
        con.execute('INSERT OR REPLACE INTO code_chunk VALUES(?,?,?,?,?,?,?,?,?,?)', (chunk_id, fv, file_id, chunk_type, lang, role, start_line, end_line, block, chunk_hash))
        insert_fts(con, 'code_fts', chunk_id, block)

    for row in line_rows:
        stripped = row['text'].strip()
        line_no = row['n']
        imp = re.match(r'(?:import|from)\s+(.+)', stripped)
        if imp:
            con.execute('INSERT OR REPLACE INTO code_import_edge VALUES(?,?,?,?,?,?)', (stable_id('imp', rel + str(line_no) + stripped), file_id, rel, imp.group(1)[:500], 'deterministic_text_scan', line_no))
        for pat, stype in [
            (r'(?:export\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)','FUNCTION'),
            (r'def\s+([A-Za-z_][A-Za-z0-9_]*)','FUNCTION'),
            (r'class\s+([A-Za-z_][A-Za-z0-9_]*)','CLASS'),
            (r'(?:export\s+)?const\s+([A-Za-z_][A-Za-z0-9_]*)','CONST_OR_COMPONENT'),
            (r'(?:export\s+)?(?:interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)','TYPE_INTERFACE'),
        ]:
            m = re.search(pat, stripped)
            if m:
                sym = m.group(1)
                sid = stable_id('sym', f'{rel}:{sym}:{line_no}')
                con.execute('INSERT OR REPLACE INTO code_symbol VALUES(?,?,?,?,?,?,?,?,?,?)', (sid, sym, stype, fv, file_id, lang, line_no, line_no, stripped, sha256_bytes(stripped.encode('utf-8', errors='replace'))))
                insert_fts(con, 'symbol_fts', sid, stripped)
                break

    low = rel.lower()
    if '/app/' in '/' + low or '/pages/' in '/' + low or '/api/' in low or 'route.' in low or 'page.' in low:
        route_path = '/' + re.sub(r'^(src/)?(app|pages)/', '', rel)
        route_path = re.sub(r'/(page|index)\.(tsx|ts|jsx|js|py)$', '/', route_path)
        route_path = re.sub(r'route\.(tsx|ts|js|py)$', '', route_path)
        route_path = route_path.replace('//','/').rstrip('/') or '/'
        rid = stable_id('route', route_path + rel)
        rtype = 'API_ENDPOINT' if '/api/' in low or 'route.' in low else 'FRONTEND_PAGE'
        con.execute('INSERT OR REPLACE INTO app_route VALUES(?,?,?,?,?,?,?,?,?,?)', (rid, route_path, rtype, 'deterministic_path', file_id, fv, '', '', '', sha256_bytes((route_path+rel).encode())))
        insert_fts(con, 'route_fts', rid, route_path + '\n' + rel)

    if name_low in {'package.json','requirements.txt','pyproject.toml'}:
        mid = stable_id('manifest', rel + raw_hash)
        ecosystem = 'node' if name_low == 'package.json' else 'python'
        con.execute('INSERT OR REPLACE INTO dependency_manifest VALUES(?,?,?,?,?,?)', (mid, file_id, name_low, ecosystem, rel, raw_hash))
        if name_low == 'package.json':
            try:
                data = json.loads(text)
                for section in ['dependencies','devDependencies','peerDependencies','optionalDependencies']:
                    for name, version in (data.get(section) or {}).items():
                        con.execute('INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?,?)', (stable_id('dep', mid + name), mid, name, str(version), ecosystem, section))
            except Exception as e:
                con.execute('INSERT OR REPLACE INTO review_required_item VALUES(?,?,?,?,?,?)', (stable_id('review', rel + str(e)), '', rel, 'package.json parse failed: ' + str(e), 'REVIEW_REQUIRED', now()))
        elif name_low == 'requirements.txt':
            for line in text.splitlines():
                x = line.strip()
                if x and not x.startswith('#'):
                    con.execute('INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?,?)', (stable_id('dep', mid + x), mid, re.split(r'[=<>!~ ]+', x)[0], x, ecosystem, 'runtime'))

    if ext in READONLY_ARTIFACT_EXTS or '/artifacts/' in '/' + low or '/public/artifacts/' in '/' + low:
        aid = stable_id('artifact', rel + raw_hash)
        con.execute('INSERT OR REPLACE INTO project_artifact VALUES(?,?,?,?,?,?,?)', (aid, file_id, ext or 'text_artifact', rel, raw_hash, 'READ_ONLY_TEXT_ARTIFACT_FULLY_CHUNKED', json.dumps({'role': role})))
        for idx in range(0, len(lines), 120):
            block = ''.join(lines[idx:idx+120])
            if block.strip():
                insert_fts(con, 'artifact_fts', stable_id('artfts', aid + str(idx)), block)


def chunk_type_for(rel: str, role: str, block: str) -> str:
    low = rel.lower()
    s = block.lstrip()
    if re.match(r'^(import|from)\s+', s) or '\nimport ' in block or '\nfrom ' in block:
        return 'IMPORT_BLOCK'
    if 'function ' in block or re.search(r'\bdef\s+[A-Za-z_]', block):
        return 'FUNCTION_BLOCK'
    if re.search(r'\bclass\s+[A-Za-z_]', block):
        return 'CLASS_BLOCK'
    if '/api/' in low or 'route.' in low:
        return 'API_HANDLER_BLOCK'
    if 'page.' in low or '/app/' in '/' + low or '/pages/' in '/' + low:
        return 'ROUTE_PAGE_BLOCK'
    if any(x in low for x in ['config','package.json','requirements.txt','pyproject','dockerfile']):
        return 'CONFIG_BUILD_TOOLING_BLOCK'
    return role or 'UNKNOWN_TEXT_BLOCK'


def count_table(con: sqlite3.Connection, table: str) -> int:
    try:
        return con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    except Exception:
        return 0


def ingest_code_sector(db: Path, sources: list[dict], tracker: dict, cb: ProgressCallback | None):
    con = connect(db); make_code_schema(con)
    for src in sources:
        root = Path(src.get('path',''))
        if not root.exists():
            continue
        # git commits full visible history where possible.
        try:
            run = subprocess.run(['git','-C',str(root),'log','--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s','-n','5000'], capture_output=True, text=True, timeout=120)
            if run.returncode == 0:
                for order, line in enumerate(run.stdout.splitlines()):
                    parts = line.split('\x1f')
                    if len(parts) >= 6:
                        con.execute('INSERT OR REPLACE INTO git_commit VALUES(?,?,?,?,?,?,?)', (parts[0], parts[1], parts[2], sha256_bytes(parts[3].encode()), parts[4], parts[5], order))
                        insert_fts(con, 'commit_fts', parts[0], parts[5])
        except Exception:
            pass
        files = list(iter_project_files(root)) if root.is_dir() else [root]
        for i, p in enumerate(files, 1):
            try:
                add_code_file(con, root if root.is_dir() else p.parent, p)
            except Exception as e:
                con.execute('INSERT OR REPLACE INTO review_required_item VALUES(?,?,?,?,?,?)', (stable_id('review', str(p)+str(e)), '', str(p), str(e), 'FAILED_REVIEW_REQUIRED', now()))
            if i % 10 == 0:
                con.commit()
            emit(cb, tracker, 'code_lossless_v52', f'full byte/line/chunk index {i}/{len(files)}', str(p), 1)
    metrics = {
        'source_file': count_table(con,'source_file'), 'code_file': count_table(con,'code_file'),
        'code_line_snapshot': count_table(con,'code_line_snapshot'), 'code_chunk': count_table(con,'code_chunk'),
        'code_byte_span': count_table(con,'code_byte_span'), 'app_route': count_table(con,'app_route'),
        'dependency_item': count_table(con,'dependency_item'), 'project_artifact': count_table(con,'project_artifact'),
        'policy': 'V5.2 lossless parseable text/code full chunking; images/3D/video hash-only in code lane'
    }
    mj = json.dumps(metrics, sort_keys=True)
    con.execute('INSERT OR REPLACE INTO source_structure_signature VALUES(?,?,?,?,?,?)', (stable_id('sig','local_code'+mj), 'local_code', 'REPO_CODE_LOSSLESS_V52', mj, sha256_bytes(mj.encode()), now()))
    con.commit(); con.close()


def ingest_generic_file(db: Path, lane_key: str, sources: list[dict], tracker: dict, cb: ProgressCallback | None):
    con = connect(db); make_generic_schema(con, lane_key)
    for src in sources:
        path = Path(src.get('path',''))
        text = src.get('text') or ''
        if path.exists() and path.is_file():
            raw = path.read_bytes()
            h = sha256_bytes(raw); size = len(raw)
            if probably_text(raw):
                text, enc = decode_raw(raw)
                status = 'TEXT_CHUNKED_FULL'
            else:
                text, enc = '', 'binary'
                status = 'BYTE_COVERED_UNSUPPORTED'
            source_id = src.get('source_id') or stable_id('source', str(path))
            con.execute('INSERT OR REPLACE INTO generic_source VALUES(?,?,?,?,?,?,?,?)', (source_id, lane_key, src.get('display_name') or path.name, str(path), h, size, 1, now()))
            if text:
                for idx in range(0, len(text), 4500):
                    chunk = text[idx:idx+4500]
                    cid = stable_id('gchunk', source_id + str(idx) + sha256_bytes(chunk.encode('utf-8', errors='replace')))
                    con.execute('INSERT OR REPLACE INTO generic_chunk VALUES(?,?,?,?,?,?)', (cid, source_id, idx//4500 + 1, chunk, sha256_bytes(chunk.encode('utf-8', errors='replace')), now()))
                    insert_fts(con, 'generic_fts', cid, chunk)
            metrics = {'lane': lane_key, 'status': status, 'size_bytes': size, 'chunks': count_table(con,'generic_chunk')}
            mj = json.dumps(metrics, sort_keys=True)
            con.execute('INSERT OR REPLACE INTO source_structure_signature VALUES(?,?,?,?,?,?)', (stable_id('sig',source_id+mj), source_id, lane_key, mj, sha256_bytes(mj.encode()), now()))
        emit(cb, tracker, lane_key, 'generic full coverage/chunking', str(path), 1)
    con.commit(); con.close()


def build_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress: ProgressCallback | None = None):
    active = [s for s in sources if s.get('active', True)]
    if not active:
        raise RuntimeError('NO_ACTIVE_SOURCES_LOADED')
    root = brain_output_dir(workspace_dir, brain_name)
    project = root / 'project'; sectors = project / 'sectors'; topology = project / 'topology'; receipts = root / 'receipts'
    for d in [project, sectors, topology, receipts]: d.mkdir(parents=True, exist_ok=True)
    router_db = project / 'project_router.sqlite'
    make_router(router_db, brain_name)
    total_files = 0
    for src in active:
        p = Path(src.get('path',''))
        if p.exists() and p.is_dir():
            total_files += sum(1 for _ in iter_project_files(p))
        else:
            total_files += 1
    tracker = {'done': 0, 'total': max(10, total_files + len(active) + 10), 'start': time.time()}
    emit(progress, tracker, 'build', 'starting V5.2 lossless brain build', str(root), 1)
    con = connect(router_db)
    # one active sector row per lane, no duplicate local_code rows
    con.execute('DELETE FROM sector_registry')
    con.execute('DELETE FROM sector_pointer')
    for lane_key, label in LANES.items():
        sdb = sectors / lane_key / f'{lane_key}_sector_v001.sqlite'
        if lane_key in {'local_code','github'}:
            make_code_schema(connect(sdb)); connect(sdb).close()
        else:
            make_generic_schema(connect(sdb), lane_key); connect(sdb).close()
        mmd_required = 1 if lane_key == 'local_code' and any(s.get('lane_key') == 'local_code' for s in active) else 0
        con.execute('INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)', (stable_id('sector', lane_key), lane_key, label, str(sdb), 1, 'v001', sha256_file(sdb), now()))
        con.execute('INSERT OR REPLACE INTO sector_pointer VALUES(?,?,?,?,?,?,?)', (stable_id('pointer', lane_key), lane_key, label, str(sdb), mmd_required, 'ACTIVE_SCHEMA_READY', now()))
    for src in active:
        p = Path(src.get('path',''))
        sh = sha256_file(p) if p.exists() and p.is_file() else sha256_bytes(str(p).encode())
        con.execute('INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)', (src.get('source_id') or stable_id('source', str(p)), src.get('lane_key','custom'), LANES.get(src.get('lane_key','custom'), src.get('lane_key','custom')), src.get('source_type','File'), src.get('display_name') or str(p), str(p), sh, 1, now()))
    con.commit(); con.close()

    code_sources = [s for s in active if s.get('lane_key') in {'local_code','github'}]
    if code_sources:
        ingest_code_sector(sectors / 'local_code' / 'local_code_sector_v001.sqlite', code_sources, tracker, progress)
    for lane in sorted(set(s.get('lane_key','custom') for s in active) - {'local_code','github'}):
        ingest_generic_file(sectors / lane / f'{lane}_sector_v001.sqlite', lane, [s for s in active if s.get('lane_key') == lane], tracker, progress)
    write_topology(root)
    (receipts / 'build_receipt.md').write_text(f'# Build Receipt\n\ncreated={now()}\npolicy=V5.2_LOSSLESS_CODE_CHUNKING\nbrain={brain_name}\n', encoding='utf-8')
    emit(progress, tracker, 'done', 'V5.2 lossless brain build complete', str(root), tracker['total'] - tracker['done'])
    return {'brain_root': str(root), 'project_root': str(project), 'router_db': str(router_db)}


def write_topology(brain_root: Path):
    topo = brain_root / 'project' / 'topology'; topo.mkdir(parents=True, exist_ok=True)
    code_db = brain_root / 'project' / 'sectors' / 'local_code' / 'local_code_sector_v001.sqlite'
    if code_db.exists():
        con = sqlite3.connect(code_db)
        c = lambda t: count_table(con, t)
        lines = ['flowchart LR','  A[coded project source]','  A --> B[files + raw byte spans]','  B --> C[file versions]','  C --> D[line snapshots: %s]' % c('code_line_snapshot'),'  C --> E[lossless chunks: %s]' % c('code_chunk'),'  E --> F[symbols: %s]' % c('code_symbol'),'  E --> G[routes/pages: %s]' % c('app_route'),'  E --> H[imports: %s]' % c('code_import_edge'),'  C --> I[dependencies: %s]' % c('dependency_item'),'  C --> J[read-only artifacts: %s]' % c('project_artifact'),'  B --> K[assets hash-only: images/3D/video not studied in code lane]']
        con.close()
        (topo / 'local_code_lane.mmd').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    router = brain_root / 'project' / 'project_router.sqlite'
    if router.exists():
        con = sqlite3.connect(router)
        rows = con.execute('SELECT lane_label, mmd_required FROM sector_pointer ORDER BY lane_label').fetchall()
        lines = ['flowchart TD','  PKG[one-upload project brain]','  LAW[locked Env/UOP/project template]','  PROJECT[generated project sectors + pointers]','  PKG --> LAW','  PKG --> PROJECT']
        for i,(label,mmd) in enumerate(rows):
            lines.append(f'  PROJECT --> S{i}[{label} - {"MMD" if mmd else "DB pointer"}]')
        con.close()
        (topo / 'project_master_topology.mmd').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def render_topology(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    root = brain_output_dir(workspace_dir, brain_name); write_topology(root)
    topo = root / 'project' / 'topology'
    mmds = list(topo.glob('*.mmd'))
    tracker = {'done':0,'total':max(1,len(mmds)),'start':time.time()}
    mmdc = shutil.which('mmdc') or shutil.which('mmdc.cmd') or shutil.which('mmdc.exe')
    manifest = []
    for mmd in mmds:
        if mmdc:
            svg = mmd.with_suffix('.svg'); png = mmd.with_suffix('.png')
            run1 = subprocess.run([mmdc,'-i',str(mmd),'-o',str(svg),'-b','white'], capture_output=True, text=True, timeout=600)
            run2 = subprocess.run([mmdc,'-i',str(mmd),'-o',str(png),'-b','white','-w','7680','-H','4320'], capture_output=True, text=True, timeout=900)
            manifest.append({'mmd':str(mmd),'svg':str(svg) if run1.returncode==0 else None,'png':str(png) if run2.returncode==0 else None,'status':'RENDERED' if run1.returncode==0 else 'RENDER_BLOCKED'})
        else:
            manifest.append({'mmd':str(mmd),'status':'RENDER_BLOCKED_MERMAID_CLI_MISSING'})
        emit(progress, tracker, 'render', 'render topology', str(mmd), 1)
    (topo / 'render_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return {'topology': str(topo), 'render_manifest': str(topo / 'render_manifest.json')}


def export_one_upload_package(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    root = brain_output_dir(workspace_dir, brain_name)
    packages = root / 'packages'; packages.mkdir(parents=True, exist_ok=True)
    stage = packages / f'{slugify_name(brain_name)}_one_upload_package_v001'
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for name in ['project','receipts']:
        src = root / name
        if src.exists(): shutil.copytree(src, stage / name)
    (stage / 'FLASH_ME_FIRST_SINGLE_PROMPT.txt').write_text(f'UEPC-ENV15-FLASH-BOOT-001 | Mode: flash_env + validation\nCHAT_NAME:\n[{brain_name}]\n\nInspect .uepc pointers, env/uop/project locks, project router, sectors, topology, manifests, receipts. Env/UOP/project-template are locked/read-only. Generated project sectors are writable only by explicit user command.\n', encoding='utf-8')
    for dot in ['.uepc_env','.uepc_project','.uepc_profile']:
        (stage / dot).write_text(f'{dot}=V5.2 generated package pointer\n', encoding='utf-8')
    (stage / 'manifests').mkdir(exist_ok=True)
    (stage / 'manifests' / 'PROJECT_BRAIN_PACKAGE_MANIFEST.json').write_text(json.dumps({'created_at':now(),'policy':'V5.2 export'}, indent=2), encoding='utf-8')
    zip_path = packages / f'{slugify_name(brain_name)}_one_upload_package_v001.zip'
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in stage.rglob('*'):
            if f.is_file(): z.write(f, f.relative_to(stage).as_posix())
    if progress: progress({'stage':'done','task':'export complete','file':str(zip_path),'percent':100})
    return {'package_folder':str(stage), 'package_zip':str(zip_path)}


def export_gemini_exact10(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    root = brain_output_dir(workspace_dir, brain_name)
    normal = export_one_upload_package(workspace_dir, brain_name, None)
    packages = root / 'packages'; zip_path = packages / f'{slugify_name(brain_name)}_gemini_exact10_v001.zip'
    conjoined = packages / 'GENERATED_PROJECT_CONJOINED.sqlite'
    if conjoined.exists(): conjoined.unlink()
    con = sqlite3.connect(conjoined); con.execute('CREATE TABLE package_note(k TEXT PRIMARY KEY, v TEXT)'); con.execute('INSERT INTO package_note VALUES(?,?)', ('policy','Gemini exact 10 package; use normal package for full raw payload')); con.commit(); con.close()
    payload = packages / 'PUBLIC_ENV_UOP_PROJECT_LOCKED.zip'
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README.txt', 'Locked Env/UOP/project-template placeholder. Normal one-upload package preserves full structure.')
    files = {
        'GEMINI_FLASH_PROMPT.txt': 'Pointer-first boot. Read generated project conjoined DB, env/uop locked zip, MMD files, and manifest. Do not treat Gemini package as full payload.\n',
        '.uepc_env': 'ENV_LOCKED=true\n', '.uepc_project': 'PROJECT_GENERATED=true\n', '.uepc_profile': 'UOP_LOCKED=true\n',
        'PACKAGE_MAP.json': json.dumps({'created_at':now(),'file_count':10}, indent=2),
        'README_FIRST.md': '# Gemini exact 10 package\n',
        'ACTIVE_POINTERS.json': json.dumps({'project_db':'GENERATED_PROJECT_CONJOINED.sqlite'}, indent=2),
    }
    if zip_path.exists(): zip_path.unlink()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for n,v in files.items(): z.writestr(n, v)
        z.write(conjoined, 'GENERATED_PROJECT_CONJOINED.sqlite')
        z.write(payload, 'PUBLIC_ENV_UOP_PROJECT_LOCKED.zip')
        lm = root / 'project' / 'topology' / 'local_code_lane.mmd'
        z.write(lm, 'LOCAL_CODE_LANE.mmd') if lm.exists() else z.writestr('LOCAL_CODE_LANE.mmd', 'flowchart LR\n  A[No local code topology yet]\n')
    if progress: progress({'stage':'done','task':'Gemini exact 10 export complete','file':str(zip_path),'percent':100})
    return {'gemini_package_zip': str(zip_path)}


def _cmd_status(cmd: list[str]) -> dict:
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        return {'status':'FOUND' if run.returncode == 0 else 'ERROR', 'version': (run.stdout or run.stderr or '').strip().splitlines()[0][:300] if (run.stdout or run.stderr).strip() else '', 'path': shutil.which(cmd[0])}
    except Exception as e:
        return {'status':'MISSING', 'version': str(e)[:300], 'path': shutil.which(cmd[0])}


def scan_tools(workspace_dir: str | Path | None = None, progress: ProgressCallback | None = None) -> dict:
    # In-process scan only. Does NOT launch another app and does NOT mutate app source.
    result = {
        'created_at': now(),
        'commands': {k:_cmd_status(v) for k,v in {'git':['git','--version'], 'node':['node','--version'], 'npm':['npm','--version'], 'mmdc':['mmdc','--version'], 'tesseract':['tesseract','--version'], 'python':[sys.executable,'--version']}.items()},
        'python_modules': {},
        'policy': 'scan only; no app launch; no source mutation',
    }
    for mod, pip_name in PYTHON_PARSER_PACKAGES.items():
        try:
            __import__(mod)
            result['python_modules'][mod] = {'status':'FOUND', 'pip': pip_name}
        except Exception as e:
            result['python_modules'][mod] = {'status':'MISSING', 'pip': pip_name, 'reason': str(e)[:160]}
    if workspace_dir:
        out = normalize_workspace_dir(workspace_dir) / 'system_capability_profile.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    if progress: progress({'stage':'scan_tools','task':'tool scan complete','file':str(workspace_dir or ''),'percent':100})
    return result


def install_missing_dependencies(workspace_dir: str | Path | None = None, progress: ProgressCallback | None = None) -> dict:
    # No embedding install. No second app launch. Installs only deterministic parser libs in current Python env.
    scan = scan_tools(workspace_dir, None)
    missing = sorted({v['pip'] for v in scan['python_modules'].values() if v['status'] == 'MISSING'})
    report = {'created_at': now(), 'requested_python_packages': missing, 'results': [], 'policy':'no embeddings; no app launch; parser dependencies only'}
    if not missing:
        if progress: progress({'stage':'install_tools','task':'all parser packages already available','file':'','percent':100})
        return report
    for i, pkg in enumerate(missing, 1):
        if progress: progress({'stage':'install_tools','task':f'pip install {pkg}', 'file':pkg, 'percent':int(i*100/max(1,len(missing)))})
        try:
            run = subprocess.run([sys.executable, '-m', 'pip', 'install', pkg], capture_output=True, text=True, timeout=900)
            report['results'].append({'package':pkg,'returncode':run.returncode,'stdout_tail':run.stdout[-1200:],'stderr_tail':run.stderr[-1200:]})
        except Exception as e:
            report['results'].append({'package':pkg,'error':str(e)})
    if workspace_dir:
        out = normalize_workspace_dir(workspace_dir) / 'install_missing_dependencies_report.json'
        out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
