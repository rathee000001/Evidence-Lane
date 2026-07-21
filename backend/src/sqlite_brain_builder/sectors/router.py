from __future__ import annotations
from pathlib import Path
import json
from sqlite_brain_builder.core import uid, now, slugify, file_sha256, sha256_text
from sqlite_brain_builder.storage.sqlite_utils import connect, apply_schema, integrity_ok

SECTORS = {
 'code':'regenerative', 'discussion':'append-first', 'analysis':'versioned', 'plan':'versioned-status',
 'mode':'locked-version', 'docs':'hash-append-replace', 'data':'hash-append-replace', 'pdf_ocr':'hash-append-replace',
 'images_ocr':'hash-append-replace', 'artifacts':'relation-aware', 'routes':'derived', 'dependencies':'derived', 'validation':'derived'
}

def init_router(router_path: str | Path, brain_id: str, brain_name: str) -> dict:
    con=connect(router_path); apply_schema(con, 'sqlite_brain_builder.storage', 'router_schema.sql')
    con.execute('INSERT OR REPLACE INTO brain_manifest VALUES(?,?,?,?,?,?,?,?,?)', (brain_id,brain_name,slugify(brain_name),'v3',now(),'ACTIVE',len(SECTORS),0,0))
    for name,behavior in SECTORS.items():
        sid=f'sector_{name}'
        con.execute('INSERT OR IGNORE INTO brain_section_registry VALUES(?,?,?,?)', (sid,name,behavior,1))
        con.execute('INSERT OR IGNORE INTO brain_sector VALUES(?,?,?,?)', (sid,name,behavior,None))
    con.commit(); con.close()
    return {'brain_id':brain_id, 'router_path':str(router_path), 'sectors':list(SECTORS)}

def register_sector_version(router_path: str | Path, sector_name: str, db_path: str | Path, reason: str='build') -> dict:
    con=connect(router_path); apply_schema(con, 'sqlite_brain_builder.storage', 'router_schema.sql')
    sector_id=f'sector_{sector_name}'
    old=con.execute('SELECT active_version_id FROM sector_pointer WHERE sector_id=?',(sector_id,)).fetchone()
    old_version=old[0] if old else None
    count=con.execute('SELECT COUNT(*) FROM brain_sector_version WHERE sector_id=?',(sector_id,)).fetchone()[0]
    version_id=f'{sector_id}_v{count+1:03d}'
    h=file_sha256(db_path) if Path(db_path).exists() else ''
    con.execute('INSERT INTO brain_sector_version VALUES(?,?,?,?,?,?,?)', (version_id,sector_id,count+1,str(db_path),'ACTIVE',h,now()))
    con.execute('INSERT OR REPLACE INTO sector_pointer VALUES(?,?,?,?,?)', (sector_id,version_id,f'{sector_id}.next.{count+2:03d}',json.dumps({'sector_id':sector_id,'active_version_id':version_id,'db_path':str(db_path),'hash':h}),now()))
    con.execute('UPDATE brain_sector SET active_version_id=? WHERE sector_id=?',(version_id,sector_id))
    con.execute('INSERT OR REPLACE INTO active_brain_view VALUES(?,?,?,?,?)', ('brain',sector_id,version_id,sha256_text(str(db_path)+h),now()))
    if old_version:
        con.execute('INSERT INTO sector_supersede_ledger VALUES(?,?,?,?,?,?)', (uid('supersede'),sector_id,old_version,version_id,reason,now()))
    con.commit(); con.close()
    return {'sector_id':sector_id,'version_id':version_id,'hash':h,'old_version_id':old_version}

def integrity_router(router_path):
    return integrity_ok(router_path)
