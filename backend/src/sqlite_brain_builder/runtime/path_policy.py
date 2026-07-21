from __future__ import annotations
from pathlib import Path
import re

SYSTEM_SEGMENTS = {
    "brains", "packages", "receipts", "renders", "logs", "review_queue", "exports",
    "project", "sectors", "topology", "manifests", "prompts", "recovery"
}

def slugify_name(value: str) -> str:
    text = (value or "brain").strip()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_").lower() or "brain"

def normalize_workspace_dir(path: str | Path) -> Path:
    s = str(path).strip().rstrip("\\/")
    while True:
        parts = re.split(r"[\\/]+", s)
        if len(parts) > 1 and parts[-1].lower() in SYSTEM_SEGMENTS:
            s = re.sub(r"[\\/]+[^\\/]+$", "", s)
            continue
        return Path(s).expanduser()

def brain_output_dir(workspace_dir: str | Path, brain_name: str) -> Path:
    root = normalize_workspace_dir(workspace_dir)
    slug = slugify_name(brain_name)
    output_slug = f"{slug}_output"
    if root.name.lower() in {slug.lower(), output_slug.lower()}:
        return root
    return root / output_slug

def workspace_db_path(workspace_dir: str | Path) -> Path:
    return normalize_workspace_dir(workspace_dir) / "workspace.sqlite"
