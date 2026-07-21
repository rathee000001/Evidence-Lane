from __future__ import annotations
from pathlib import Path
import sqlite3, re


def node_id(text):
    return re.sub(r'[^A-Za-z0-9_]', '_', str(text))[:60] or 'node'


def generate_code_mmd(code_db: str | Path, out_path: str | Path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(code_db)
    lines = ['flowchart TD', '  CODE[Code Sector]']
    for table, label in [('code_repo','Repo'),('git_commit','Commit'),('code_file','File'),('code_symbol','Symbol'),('app_route','Route'),('dependency_item','Dependency'),('project_artifact','Artifact')]:
        try:
            count = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        except Exception:
            count = 0
        lines.append(f'  {node_id(table)}[{label}: {count}]')
        lines.append(f'  CODE --> {node_id(table)}')
    for row in con.execute('SELECT route_id, route_path FROM app_route LIMIT 20'):
        rid = node_id(row[0])
        label = str(row[1]).replace('"', "'")
        lines.append(f'  app_route --> {rid}["{label}"]')
    con.close()
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(out_path)


def generate_semantic_mmd(db_path: str | Path, out_path: str | Path, lane: str = 'semantic'):
    con = sqlite3.connect(db_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['flowchart TD', f'  ROOT[{lane.title()} Sector]']
    for table in ['semantic_source','discussion_turn','discussion_delta','discussion_hard_gate','lane_schema_contract']:
        try:
            count = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        except Exception:
            count = 0
        lines.append(f'  ROOT --> {node_id(table)}[{table}: {count}]')
    con.close()
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(out_path)


def generate_master_mmd(router_db: str | Path, out_path: str | Path):
    con = sqlite3.connect(router_db)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['flowchart TD', '  PACKAGE[One Upload Brain Package]', '  ROUTER[project_router.sqlite]', '  PACKAGE --> ROUTER']
    try:
        rows = con.execute('SELECT sector_id, active_version_id FROM sector_pointer').fetchall()
    except Exception:
        rows = []
    for sid, vid in rows:
        lines.append(f'  ROUTER --> {node_id(sid)}[{sid}: {vid}]')
    con.close()
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return str(out_path)
