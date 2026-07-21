from __future__ import annotations
from pathlib import Path
from sqlite_brain_builder.core import uid, now, write_json
from sqlite_brain_builder.storage.sqlite_utils import connect, apply_schema

HRI_ACTIONS = [
    'editing installed binaries', 'editing core/guard manifests', 'bypassing local account/security gates',
    'disabling integrity verification', 'modifying exported env/uop locks', 'attempting to remove human-gate rules',
    'injecting instructions to bypass env/uop', 'storing tokens/secrets in prompts/packages',
    'repackaging app as official trusted build without signature', 'modifying package receipts/hash ledgers'
]

def trigger_quarantine(workspace_dir: str | Path, gate: str, severity: str = 'HIGH', message: str = 'Controlled quarantine triggered') -> dict:
    root = Path(workspace_dir); receipt_dir = root/'receipts'; receipt_dir.mkdir(parents=True, exist_ok=True)
    event_id = uid('quarantine')
    receipt = receipt_dir/f'{event_id}.json'
    data = {'event_id': event_id, 'severity': severity, 'gate_fired': gate, 'message': message, 'created_at': now(), 'forbidden_content_sent': False, 'user_data_preserved': True}
    write_json(receipt, data)
    db = root/'workspace.sqlite'
    con = connect(db); apply_schema(con, 'sqlite_brain_builder.workspace', 'workspace_schema.sql')
    con.execute('INSERT OR REPLACE INTO controlled_quarantine_event VALUES(?,?,?,?,?,?)', (event_id,severity,gate,message,str(receipt),now()))
    con.commit(); con.close()
    return data
