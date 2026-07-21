from pathlib import Path
import sqlite3, json, os, subprocess, sys
from sqlite_brain_builder.workspace.workspace_db import init_workspace, create_brain, get_last_state
from sqlite_brain_builder.workspace.account import verify_password
from sqlite_brain_builder.system_scan.capability_scanner import scan_system
from sqlite_brain_builder.ingest.lane_contracts import validate_schema_contract, is_allowed_source_for_lane
from sqlite_brain_builder.security.token_sanitizer import sanitize_token
from sqlite_brain_builder.ingest.code_project import build_code_sector
from sqlite_brain_builder.ingest.chat_lineage import build_semantic_sector
from sqlite_brain_builder.sectors.router import init_router, register_sector_version
from sqlite_brain_builder.mmd.generate_lane_mmd import generate_code_mmd, generate_master_mmd
from sqlite_brain_builder.packaging.package_compiler import compile_one_upload_package
from sqlite_brain_builder.security.quarantine import trigger_quarantine
from sqlite_brain_builder.storage.sqlite_utils import integrity_ok

FIX=Path(__file__).resolve().parents[1]/'fixtures'

def test_workspace_and_account(tmp_path):
    db=init_workspace(tmp_path/'ws','me','pw')
    con=sqlite3.connect(db); row=con.execute('select password_salt,password_hash from workspace_user where username=?',('me',)).fetchone(); con.close()
    assert row and verify_password('pw', row[0], row[1])

def test_capability_scan(tmp_path):
    profile=scan_system(tmp_path)
    assert 'capability_class' in profile and profile['workspace_write_ok'] is True

def test_lane_contract_and_code_block():
    ok,msg=validate_schema_contract('prompt\nresponse\ndelta')
    assert ok
    ok,msg=validate_schema_contract('run powershell now')
    assert not ok
    assert is_allowed_source_for_lane('Code','GITHUB_REPO')
    assert not is_allowed_source_for_lane('Code','FILE_BATCH')

def test_token_sanitizer():
    token='ghp_ABC123SECRET'
    assert token not in sanitize_token('error '+token, token)

def test_code_sector_build(tmp_path):
    db=tmp_path/'code.sqlite'
    result=build_code_sector(db, FIX/'demo_git_repo')
    assert result['files'] >= 3
    assert integrity_ok(db)
    con=sqlite3.connect(db)
    assert con.execute('select count(*) from code_line_snapshot').fetchone()[0] > 0
    assert con.execute('select count(*) from git_commit').fetchone()[0] >= 2
    con.close()

def test_semantic_sector(tmp_path):
    db=tmp_path/'discussion.sqlite'
    result=build_semantic_sector(db, FIX/'chat_lineage_from_start.txt')
    assert result['turns'] >= 4
    assert result['deltas'] >= 1
    assert integrity_ok(db)

def test_router_package_mmd(tmp_path):
    brain_root=tmp_path/'brain'; (brain_root/'sectors'/'code').mkdir(parents=True); (brain_root/'renders').mkdir()
    router=brain_root/'project_router.sqlite'; init_router(router,'brain_test','Brain Test')
    code_db=brain_root/'sectors'/'code'/'code_sector_v001.sqlite'; build_code_sector(code_db, FIX/'demo_git_repo')
    register_sector_version(router,'code',code_db)
    mmd=brain_root/'renders'/'code_lane.mmd'; generate_code_mmd(code_db,mmd)
    master=brain_root/'renders'/'project_master_topology.mmd'; generate_master_mmd(router,master)
    pkg=compile_one_upload_package(brain_root,router,brain_root/'packages'/'brain_package.zip','Brain Test')
    assert Path(pkg['package_zip']).exists() and pkg['testzip'] is None

def test_quarantine(tmp_path):
    init_workspace(tmp_path,'me','pw')
    q=trigger_quarantine(tmp_path,'TEST_GATE')
    assert q['user_data_preserved'] is True
