from __future__ import annotations
import shutil, platform, json, os, subprocess
from pathlib import Path
from sqlite_brain_builder.core import now, uid


def _version(cmd):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
        return p.stdout.splitlines()[0][:200] if p.stdout else ''
    except Exception:
        return ''


def scan_system(workspace_dir: str | Path | None = None) -> dict:
    tools = {}
    for name, cmd in {
        'git': ['git','--version'],
        'tesseract': ['tesseract','--version'],
        'mmdc': ['mmdc','--version'],
        'python': ['python3','--version'],
    }.items():
        path = shutil.which(name)
        tools[name] = {'available': bool(path), 'path': path, 'version': _version(cmd) if path else ''}
    write_ok = False
    disk_free = None
    if workspace_dir:
        p = Path(workspace_dir); p.mkdir(parents=True, exist_ok=True)
        try:
            test = p/'.write_test'; test.write_text('ok'); test.unlink(); write_ok=True
            disk_free = shutil.disk_usage(p).free
        except Exception:
            write_ok=False
    cls = 'FULL_SUPPORT' if tools['git']['available'] and write_ok else 'PARTIAL_SUPPORT_WITH_FALLBACKS'
    if workspace_dir and not write_ok: cls = 'BLOCKED_DEVICE_UNSUPPORTED'
    return {'profile_id': uid('cap'), 'created_at': now(), 'platform': platform.platform(), 'cpu_count': os.cpu_count(), 'tools': tools, 'workspace_write_ok': write_ok, 'disk_free': disk_free, 'capability_class': cls}
