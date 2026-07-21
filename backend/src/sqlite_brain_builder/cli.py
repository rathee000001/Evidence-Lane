from __future__ import annotations
import argparse, json, shutil, sqlite3, os, sys
from pathlib import Path
from sqlite_brain_builder.workspace.workspace_db import init_workspace, create_brain, update_last_state, get_last_state
from sqlite_brain_builder.system_scan.capability_scanner import scan_system
from sqlite_brain_builder.system_scan.fallback_registry import as_rows
from sqlite_brain_builder.storage.sqlite_utils import connect, apply_schema, integrity_ok
from sqlite_brain_builder.core import uid, now, slugify, write_json, file_sha256
from sqlite_brain_builder.ingest.code_project import build_code_sector
from sqlite_brain_builder.ingest.chat_lineage import build_semantic_sector
from sqlite_brain_builder.ingest.lane_contracts import validate_schema_contract, is_allowed_source_for_lane
from sqlite_brain_builder.sectors.router import init_router, register_sector_version
from sqlite_brain_builder.mmd.generate_lane_mmd import generate_code_mmd, generate_semantic_mmd, generate_master_mmd
from sqlite_brain_builder.mmd.render_mmd import render_mmd
from sqlite_brain_builder.packaging.package_compiler import compile_one_upload_package
from sqlite_brain_builder.security.quarantine import trigger_quarantine


def build_demo(workspace: Path, brain_name: str, code_source: Path, chat_file: Path | None, output_json: bool = False):
    workspace.mkdir(parents=True, exist_ok=True)
    init_workspace(workspace, 'demo_user', 'demo_password')
    cap=scan_system(workspace)
    con=connect(workspace/'workspace.sqlite'); apply_schema(con,'sqlite_brain_builder.workspace','workspace_schema.sql')
    con.execute('INSERT OR REPLACE INTO workspace_capability_profile VALUES(?,?,?)',(cap['profile_id'],json.dumps(cap),now()))
    for r in as_rows():
        con.execute('INSERT OR REPLACE INTO fallback_tool_registry VALUES(?,?,?,?,?,?,?,?,?)',(r['tool_category'],r['primary_tool'],r['fallback_tool'],r['metadata_only_fallback'],'','','UNKNOWN',now(),''))
    con.commit(); con.close()
    brain=create_brain(workspace, brain_name)
    brain_root=Path(brain['output_dir']); (brain_root/'sectors'/'code').mkdir(parents=True, exist_ok=True); (brain_root/'renders').mkdir(exist_ok=True)
    router=brain_root/'project_router.sqlite'; init_router(router, brain['brain_id'], brain_name)
    code_db=brain_root/'sectors'/'code'/'code_sector_v001.sqlite'
    code_result=build_code_sector(code_db, code_source)
    code_ver=register_sector_version(router, 'code', code_db, 'initial code build')
    code_mmd=brain_root/'renders'/'code_lane.mmd'; generate_code_mmd(code_db, code_mmd); render_result=render_mmd(code_mmd)
    chat_result={}
    if chat_file:
        (brain_root/'sectors'/'discussion').mkdir(parents=True, exist_ok=True)
        sem_db=brain_root/'sectors'/'discussion'/'discussion_sector_v001.sqlite'
        chat_result=build_semantic_sector(sem_db, chat_file, 'Discussion')
        register_sector_version(router, 'discussion', sem_db, 'initial discussion build')
        sem_mmd=brain_root/'renders'/'discussion_lane.mmd'; generate_semantic_mmd(sem_db, sem_mmd, 'discussion'); render_mmd(sem_mmd)
    master_mmd=brain_root/'renders'/'project_master_topology.mmd'; generate_master_mmd(router, master_mmd); render_mmd(master_mmd)
    package_zip=brain_root/'packages'/f'{slugify(brain_name)}_package_v001.zip'
    pkg=compile_one_upload_package(brain_root, router, package_zip, brain_name, 'Demo brain package', 'Continue validation')
    # simulate unload selected: mark one source inactive and create package vNext
    con=connect(router)
    unload_id=uid('unload')
    con.execute('INSERT INTO unload_session VALUES(?,?,?,?,?,?)',(unload_id,now(),now(),'COMPLETED','demo unload', ''))
    con.execute('INSERT INTO unload_item VALUES(?,?,?,?,?,?)',(uid('unload_item'),unload_id,'sector_discussion','semantic_src_demo','demo selected source','IMPACT_REBUILD_DISCUSSION'))
    con.execute('INSERT OR REPLACE INTO active_brain_view VALUES(?,?,?,?,?)',('brain','sector_discussion','discussion_sector_v001','demo_unloaded_filter',now()))
    con.commit(); con.close()
    package_zip2=brain_root/'packages'/f'{slugify(brain_name)}_package_v002_after_unload.zip'
    pkg2=compile_one_upload_package(brain_root, router, package_zip2, brain_name, 'Demo brain package vNext after unload', 'Continue after unload')
    update_last_state(workspace, brain['brain_id'], last_build_id='demo_build', last_package_id=str(package_zip2), active_sector_id='sector_code', active_panel_id='overview', output_folder=str(brain_root), next_suggested_action='Open output folder or add intake', last_known_status='PACKAGE_VNEXT_READY')
    summary={'workspace_db':str(workspace/'workspace.sqlite'), 'brain':brain, 'capability_class':cap['capability_class'], 'code_result':code_result, 'chat_result':chat_result, 'router_integrity':integrity_ok(router), 'code_integrity':integrity_ok(code_db), 'package':pkg, 'package_vnext':pkg2, 'last_state':get_last_state(workspace, brain['brain_id']), 'render_status':render_result['status']}
    if output_json: print(json.dumps(summary, indent=2))
    return summary


def main(argv=None):
    ap=argparse.ArgumentParser(description='SQLite Brain Builder V3 Installer Workspace Edition')
    ap.add_argument('--workspace')
    ap.add_argument('--init-workspace', action='store_true')
    ap.add_argument('--username', default='local_user')
    ap.add_argument('--password', default='change-me')
    ap.add_argument('--brain-name', default='Demo Brain')
    ap.add_argument('--source')
    ap.add_argument('--chat-lineage-file')
    ap.add_argument('--build-demo', action='store_true')
    ap.add_argument('--scan-system', action='store_true')
    ap.add_argument('--quarantine-demo', action='store_true')
    ap.add_argument('--output-json', action='store_true')
    args=ap.parse_args(argv)
    workspace=Path(args.workspace or './workspace')
    if args.scan_system:
        print(json.dumps(scan_system(workspace), indent=2)); return 0
    if args.init_workspace:
        db=init_workspace(workspace,args.username,args.password); print(db); return 0
    if args.quarantine_demo:
        init_workspace(workspace,args.username,args.password); print(json.dumps(trigger_quarantine(workspace,'DEMO_HRI_GATE'), indent=2)); return 0
    if args.build_demo:
        if not args.source: ap.error('--source required for --build-demo')
        build_demo(workspace,args.brain_name,Path(args.source),Path(args.chat_lineage_file) if args.chat_lineage_file else None,args.output_json); return 0
    ap.print_help(); return 0

if __name__ == '__main__':
    raise SystemExit(main())
