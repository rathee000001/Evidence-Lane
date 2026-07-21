from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

from sqlite_brain_builder.runtime.path_policy import brain_output_dir

def _mmdc():
    for name in ["mmdc", "mmdc.cmd", "mmdc.exe"]:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("MERMAID_CLI_NOT_FOUND")

def _allowed(path: Path):
    return path.stem in {"local_code_lane", "project_master_topology"}

def _clean_topology(topology: Path):
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        stem = p.stem
        for suffix in ["_CRYSTAL", "_4K", "_HD", "_MEGA"]:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem not in {"local_code_lane", "project_master_topology"}:
            try:
                p.unlink()
            except Exception:
                pass

def _render_one(mmd: Path):
    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")
    png4k = mmd.with_name(mmd.stem + "_4K.png")
    crystal = mmd.with_name(mmd.stem + "_CRYSTAL.png")

    run = subprocess.run([_mmdc(), "-i", str(mmd), "-o", str(svg), "-b", "white"], capture_output=True, text=True, timeout=1200)
    if run.returncode != 0:
        raise RuntimeError(f"SVG_RENDER_FAILED for {mmd}: {run.stderr[:2500]}")

    run = subprocess.run([_mmdc(), "-i", str(mmd), "-o", str(png4k), "-b", "white", "-w", "7680", "-H", "4320"], capture_output=True, text=True, timeout=1200)
    if run.returncode != 0:
        raise RuntimeError(f"4K_RENDER_FAILED for {mmd}: {run.stderr[:2500]}")

    shutil.copy2(png4k, png)

    run = subprocess.run([_mmdc(), "-i", str(mmd), "-o", str(crystal), "-b", "white", "-w", "15360", "-H", "8640"], capture_output=True, text=True, timeout=1800)
    if run.returncode != 0:
        raise RuntimeError(f"CRYSTAL_RENDER_FAILED for {mmd}: {run.stderr[:2500]}")

    return {
        "mmd": str(mmd),
        "svg": str(svg),
        "png": str(png),
        "png_4k": str(png4k),
        "crystal_png": str(crystal),
        "status": "MMD_TO_SVG_TO_4K_TO_CRYSTAL",
    }

def render_topology_only(workspace_dir: str, brain_name: str, progress=None):
    from sqlite_brain_builder.runtime.topology_writer_v49 import regenerate_topology_from_existing_brain

    regen = regenerate_topology_from_existing_brain(workspace_dir, brain_name)
    topology = Path(regen["topology"])
    _clean_topology(topology)

    mmds = [p for p in sorted(topology.glob("*.mmd")) if _allowed(p)]
    if not mmds:
        raise RuntimeError(f"NO_ALLOWED_MMD_FOUND: {topology}")

    manifest = []
    for mmd in mmds:
        if progress:
            progress({"stage": "render", "task": f"rendering {mmd.name}", "file": str(mmd), "percent": 50})
        manifest.append(_render_one(mmd))

    (topology / "render_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if progress:
        progress({"stage": "done", "task": "render complete", "file": str(topology), "percent": 100})

    return {"topology": str(topology), "render_manifest": str(topology / "render_manifest.json"), "rendered": manifest}
