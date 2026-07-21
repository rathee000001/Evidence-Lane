from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from xml.etree import ElementTree as ET

from sqlite_brain_builder.runtime.path_policy import brain_output_dir


def _v47_allowed_mmd(path: Path) -> bool:
    return path.stem in {"local_code_lane", "project_master_topology"}


def _v47_cleanup_render_targets(topology: Path):
    if not topology.exists():
        return
    allowed = {"local_code_lane", "project_master_topology"}
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        stem = p.stem
        for suffix in ["_CRYSTAL", "_4K", "_HD", "_MEGA"]:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem not in allowed:
            try:
                p.unlink()
            except Exception:
                pass


def _v47_clean_mmd(mmd: Path) -> Path:
    text = mmd.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    found = False
    for line in text.splitlines():
        stripped = line.strip()
        if not found:
            if stripped.startswith("flowchart ") or stripped.startswith("graph "):
                lines.append(stripped)
                found = True
            continue
        if stripped.startswith("---") or stripped.lower().startswith("title:"):
            continue
        lines.append(line)
    if not found:
        raise RuntimeError(f"MMD_NO_FLOWCHART_DIRECTIVE: {mmd}")
    tmp = Path(tempfile.gettempdir()) / f"{mmd.stem}_{int(time.time()*1000)}_clean.mmd"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp


def _v47_mmdc():
    for name in ["mmdc", "mmdc.cmd", "mmdc.exe"]:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("MERMAID_CLI_NOT_FOUND: npm install -g @mermaid-js/mermaid-cli")


def _v47_inkscape():
    for name in ["inkscape.com", "inkscape.exe", "inkscape"]:
        found = shutil.which(name)
        if found:
            return found
    return None


def _v47_rsvg():
    for name in ["rsvg-convert", "rsvg-convert.exe"]:
        found = shutil.which(name)
        if found:
            return found
    return None


def _v47_sha(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def _v47_svg_size(svg: Path):
    raw = svg.read_text(encoding="utf-8", errors="replace")
    root = ET.fromstring(raw)

    def num(v):
        if not v:
            return None
        m = re.search(r"[-+]?\d*\.?\d+", str(v))
        return float(m.group(0)) if m else None

    width = num(root.attrib.get("width"))
    height = num(root.attrib.get("height"))
    viewbox = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if (not width or not height) and viewbox:
        parts = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", viewbox)]
        if len(parts) == 4:
            width = width or parts[2]
            height = height or parts[3]
    return max(1, int(width or 1920)), max(1, int(height or 1080))


def _v47_emit(cb, tracker, stage, task, file="", inc=1):
    tracker["done"] = min(tracker["total"], tracker["done"] + inc)
    pct = int(tracker["done"] * 100 / max(1, tracker["total"]))
    elapsed = int(time.time() - tracker["start"])
    eta = int(max(0, tracker["total"] - tracker["done"]) * max(1, elapsed) / max(1, tracker["done"]))
    if cb:
        cb({
            "stage": stage,
            "task": task,
            "file": str(file),
            "done": tracker["done"],
            "total": tracker["total"],
            "percent": pct,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "finish_epoch": int(time.time() + eta),
        })


def _v47_render_svg(clean_mmd: Path, svg: Path):
    cmd = [_v47_mmdc(), "-i", str(clean_mmd), "-o", str(svg), "-b", "white"]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if run.returncode != 0:
        raise RuntimeError(f"SVG_RENDER_FAILED: {run.stderr[:3500]}")


def _v47_svg_to_png(svg: Path, png: Path, min_w: int, min_h: int):
    svg_w, svg_h = _v47_svg_size(svg)
    scale = max(min_w / svg_w, min_h / svg_h, 1)
    out_w = min(int(svg_w * scale), 65000)
    out_h = min(int(svg_h * scale), 65000)

    inkscape = _v47_inkscape()
    if inkscape:
        cmd = [
            inkscape,
            str(svg),
            "--export-type=png",
            f"--export-filename={png}",
            f"--export-width={out_w}",
        ]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if run.returncode == 0 and png.exists():
            return out_w, out_h, "INKSCAPE_SVG_TO_PNG"

    rsvg = _v47_rsvg()
    if rsvg:
        cmd = [rsvg, "-w", str(out_w), "-o", str(png), str(svg)]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if run.returncode == 0 and png.exists():
            return out_w, out_h, "RSVG_SVG_TO_PNG"

    # Fallback: use mmdc against SVG if supported by installed version; if not, caller will use MMD fallback.
    cmd = [_v47_mmdc(), "-i", str(svg), "-o", str(png), "-b", "white", "-w", str(out_w), "-H", str(out_h)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if run.returncode == 0 and png.exists():
        return out_w, out_h, "MMDC_SVG_TO_PNG"

    raise RuntimeError("NO_SVG_TO_PNG_RENDERER_WORKED. Install Inkscape for true SVG->PNG: winget install Inkscape.Inkscape")


def _v47_mmd_to_png_fallback(clean_mmd: Path, png: Path, min_w: int, min_h: int):
    cmd = [_v47_mmdc(), "-i", str(clean_mmd), "-o", str(png), "-b", "white", "-w", str(min_w), "-H", str(min_h)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if run.returncode != 0:
        raise RuntimeError(f"PNG_FALLBACK_RENDER_FAILED: {run.stderr[:3500]}")
    return min_w, min_h, "MMDC_MMD_TO_PNG_FALLBACK"


def render_mmd_file(mmd: Path, callback=None, tracker=None):
    if not _v47_allowed_mmd(mmd):
        raise RuntimeError(f"NON_CODE_MMD_RENDER_BLOCKED: {mmd}")

    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")
    png4k = mmd.with_name(mmd.stem + "_4K.png")
    crystal = mmd.with_name(mmd.stem + "_CRYSTAL.png")

    clean = _v47_clean_mmd(mmd)
    try:
        _v47_emit(callback, tracker, "render", "rendering canonical SVG", svg, 1)
        _v47_render_svg(clean, svg)

        _v47_emit(callback, tracker, "render", "rendering 4K PNG from SVG", png4k, 1)
        try:
            w4, h4, method4 = _v47_svg_to_png(svg, png4k, 3840, 2160)
        except Exception:
            w4, h4, method4 = _v47_mmd_to_png_fallback(clean, png4k, 3840, 2160)

        shutil.copy2(png4k, png)

        _v47_emit(callback, tracker, "render", "rendering CRYSTAL PNG from SVG", crystal, 1)
        try:
            wc, hc, methodc = _v47_svg_to_png(svg, crystal, 7680, 4320)
        except Exception:
            wc, hc, methodc = _v47_mmd_to_png_fallback(clean, crystal, 7680, 4320)

        return {
            "mmd": str(mmd),
            "svg": str(svg),
            "png": str(png),
            "png_4k": str(png4k),
            "crystal_png": str(crystal),
            "svg_size": _v47_svg_size(svg),
            "png_4k_size_requested": [w4, h4],
            "crystal_size_requested": [wc, hc],
            "png_4k_method": method4,
            "crystal_method": methodc,
            "mmd_sha256": _v47_sha(mmd),
            "svg_sha256": _v47_sha(svg),
            "png_4k_sha256": _v47_sha(png4k),
            "crystal_png_sha256": _v47_sha(crystal),
            "status": "ROUTE_FIRST_MMD_TO_SVG_TO_4K_AND_CRYSTAL_PNG",
        }

    finally:
        try:
            clean.unlink()
        except Exception:
            pass


def render_topology_only(workspace_dir: str, brain_name: str, progress=None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    receipts = brain_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    from sqlite_brain_builder.runtime.topology_writer_v47 import regenerate_topology_from_existing_brain
    regenerate_topology_from_existing_brain(workspace_dir, brain_name)

    _v47_cleanup_render_targets(topology)

    mmds = [p for p in sorted(topology.glob("*.mmd")) if _v47_allowed_mmd(p)]
    if not mmds:
        raise RuntimeError(f"NO_ROUTE_FIRST_MMD_FOUND: {topology}")

    tracker = {"done": 0, "total": max(2, len(mmds) * 4 + 2), "start": time.time()}
    _v47_emit(progress, tracker, "render", "starting route-first MMD->SVG->4K PNG pipeline", topology, 1)

    manifest = []
    for mmd in mmds:
        manifest.append(render_mmd_file(mmd, progress, tracker))

    manifest_path = topology / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    receipt = receipts / "render_receipt.md"
    receipt.write_text(
        "# Render Receipt\n\n"
        "pipeline=ROUTE_FIRST_MMD_TO_SVG_TO_4K_AND_CRYSTAL_PNG\n"
        "code_mmd_policy=routes_pages_as_nodes_linked_to_code_artifacts_dependencies_tooling\n"
        "project_master_policy=sector_pointer_package_overview_only\n"
        f"rendered_files={len(manifest)}\n",
        encoding="utf-8"
    )

    _v47_emit(progress, tracker, "done", "route-first render complete", topology, tracker["total"] - tracker["done"])

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "render_manifest": str(manifest_path),
        "rendered": manifest,
    }
