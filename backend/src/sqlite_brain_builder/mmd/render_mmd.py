from __future__ import annotations
from pathlib import Path
import shutil, subprocess
from sqlite_brain_builder.core import now


def render_mmd(mmd_path: str | Path, svg_path: str | Path | None = None, png_path: str | Path | None = None) -> dict:
    mmd_path = Path(mmd_path)
    svg_path = Path(svg_path or mmd_path.with_suffix('.svg'))
    result = {'mmd': str(mmd_path), 'svg': str(svg_path), 'png': str(png_path) if png_path else '', 'status': 'MMD_RENDER_TOOL_BLOCKED', 'message': 'Mermaid CLI mmdc unavailable'}
    if not shutil.which('mmdc'):
        receipt = mmd_path.with_suffix('.render_blocked.txt')
        receipt.write_text(f'MMD_RENDER_TOOL_BLOCKED\nsource={mmd_path}\ntime={now()}\n', encoding='utf-8')
        result['blocker_receipt'] = str(receipt)
        return result
    cmd = ['mmdc', '-i', str(mmd_path), '-o', str(svg_path)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    result['output'] = p.stdout
    if p.returncode == 0 and svg_path.exists():
        result['status'] = 'SVG_RENDERED'
    return result
