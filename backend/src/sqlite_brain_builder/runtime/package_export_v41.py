from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from sqlite_brain_builder.runtime.path_policy import brain_output_dir, slugify_name

ProgressCallback = Callable[[dict], None]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def emit(callback, tracker, stage, task, file="", inc=1):
    tracker["done"] = min(tracker["total"], tracker["done"] + inc)
    pct = int(tracker["done"] * 100 / max(1, tracker["total"]))
    elapsed = int(time.time() - tracker["start"])
    eta = int(max(0, tracker["total"] - tracker["done"]) * max(1, elapsed) / max(1, tracker["done"]))
    finish_epoch = int(time.time() + eta)
    if callback:
        callback({
            "stage": stage,
            "task": task,
            "file": str(file),
            "done": tracker["done"],
            "total": tracker["total"],
            "percent": pct,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "finish_epoch": finish_epoch,
        })


def candidate_resource_roots() -> list[Path]:
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent / "resources" / "public_model_env15")
    roots.append(Path(__file__).resolve().parents[1] / "resources" / "public_model_env15")
    if hasattr(sys, "_MEIPASS"):
        roots.append(Path(sys._MEIPASS) / "sqlite_brain_builder" / "resources" / "public_model_env15")
    return roots


def find_env14_resource_root() -> Path:
    for root in candidate_resource_roots():
        if not root.exists():
            continue
        hits = list(root.rglob(".uepc_env"))
        if hits:
            env_root = hits[0].parent
            if (env_root / ".uepc_project").exists() and (env_root / ".uepc_profile").exists():
                return env_root
    checked = "\n".join(str(x) for x in candidate_resource_roots())
    raise RuntimeError("EMBEDDED_ENV15_RESOURCE_NOT_FOUND. Checked:\n" + checked)


def mermaid_cli() -> str:
    for name in ["mmdc", "mmdc.cmd", "mmdc.exe"]:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("MERMAID_CLI_NOT_FOUND: run `npm install -g @mermaid-js/mermaid-cli`, then verify `mmdc --version`.")


def copytree_overlay(src: Path, dst: Path):
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            copytree_overlay(item, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _strip_frontmatter_for_mmdc(mmd: Path) -> Path | None:
    text = mmd.read_text(encoding="utf-8", errors="replace")
    if not text.lstrip().startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    cleaned = parts[2].lstrip()
    tmp = Path(tempfile.gettempdir()) / f"{mmd.stem}_{int(time.time()*1000)}_render.mmd"
    tmp.write_text(cleaned, encoding="utf-8")
    return tmp


def _run_mmdc_svg(input_mmd: Path, output_svg: Path):
    cli = mermaid_cli()
    cmd = [cli, "-i", str(input_mmd), "-o", str(output_svg), "-b", "white"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=360)


def _svg_size(svg_path: Path) -> tuple[int, int]:
    """
    Reads actual SVG canvas. If width/height missing, uses viewBox.
    """
    text = svg_path.read_text(encoding="utf-8", errors="replace")
    root = ET.fromstring(text)

    def parse_num(value):
        if value is None:
            return None
        m = re.search(r"[-+]?\d*\.?\d+", str(value))
        return float(m.group(0)) if m else None

    width = parse_num(root.attrib.get("width"))
    height = parse_num(root.attrib.get("height"))

    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if (not width or not height) and view_box:
        parts = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", view_box)]
        if len(parts) == 4:
            width = width or parts[2]
            height = height or parts[3]

    width = int(max(1, width or 1920))
    height = int(max(1, height or 1080))
    return width, height


def _convert_svg_to_png(svg: Path, png: Path, scale: int, callback=None, tracker=None, label="HD"):
    """
    Uses mmdc SVG input because Mermaid CLI/Chromium renders SVG into a full-canvas PNG.
    We scale from actual SVG dimensions, not fixed viewport, so wide/short graphs stay sharp.
    """
    cli = mermaid_cli()
    w, h = _svg_size(svg)

    # Minimum height prevents ultra-flat 52px graphs from becoming unreadable in Photos.
    out_w = min(max(w * scale, 3840), 32000)
    out_h = min(max(h * scale, 2160), 32000)

    if callback and tracker:
        emit(callback, tracker, "render", f"rendering {label} PNG {out_w}x{out_h}", str(png), 1)

    cmd = [
        cli,
        "-i", str(svg),
        "-o", str(png),
        "-b", "white",
        "-w", str(out_w),
        "-H", str(out_h),
        "--scale", "3"
    ]

    run = subprocess.run(cmd, capture_output=True, text=True, timeout=480)
    if run.returncode != 0:
        # Some mmdc versions do not support --scale. Retry without it.
        cmd = [
            cli,
            "-i", str(svg),
            "-o", str(png),
            "-b", "white",
            "-w", str(out_w),
            "-H", str(out_h),
        ]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=480)

    if run.returncode != 0:
        raise RuntimeError(f"{label}_PNG_RENDER_FAILED for {svg}: {run.stderr[:2500]}")

    return {
        "path": str(png),
        "width_requested": out_w,
        "height_requested": out_h,
        "sha256": sha256_file(png),
    }


def render_mmd_file(mmd: Path, callback=None, tracker=None):
    """
    Final render law:
      1. MMD remains source of truth.
      2. SVG is rendered first as canonical vector topology.
      3. PNG is generated from SVG at high scale.
      4. Also writes *_HD.png and, for very large diagrams, *_MEGA.png.
    """
    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")
    hd_png = mmd.with_name(mmd.stem + "_HD.png")
    mega_png = mmd.with_name(mmd.stem + "_MEGA.png")

    if callback and tracker:
        emit(callback, tracker, "render", "rendering canonical SVG", str(svg), 1)

    render_input = mmd
    svg_run = _run_mmdc_svg(render_input, svg)
    temp_input = None

    if svg_run.returncode != 0:
        temp_input = _strip_frontmatter_for_mmdc(mmd)
        if temp_input:
            render_input = temp_input
            svg_run = _run_mmdc_svg(render_input, svg)

    if svg_run.returncode != 0:
        raise RuntimeError(f"SVG_RENDER_FAILED for {mmd}: {svg_run.stderr[:2500]}")

    if temp_input and temp_input.exists():
        try:
            temp_input.unlink()
        except Exception:
            pass

    svg_w, svg_h = _svg_size(svg)

    # Standard PNG path becomes HD enough, not tiny.
    normal = _convert_svg_to_png(svg, png, scale=4, callback=callback, tracker=tracker, label="FULL")
    hd = _convert_svg_to_png(svg, hd_png, scale=8, callback=callback, tracker=tracker, label="HD")

    mega = None
    if svg_w > 2000 or svg_h > 1400:
        mega = _convert_svg_to_png(svg, mega_png, scale=12, callback=callback, tracker=tracker, label="MEGA")

    result = {
        "mmd": str(mmd),
        "svg": str(svg),
        "png": str(png),
        "hd_png": str(hd_png),
        "mega_png": str(mega_png) if mega else None,
        "svg_width": svg_w,
        "svg_height": svg_h,
        "mmd_sha256": sha256_file(mmd),
        "svg_sha256": sha256_file(svg),
        "png_sha256": sha256_file(png),
        "hd_png_sha256": sha256_file(hd_png),
        "mega_png_sha256": sha256_file(mega_png) if mega else None,
        "status": "RENDERED_MMD_TO_SVG_TO_FULL_PNG_TO_HD_PNG",
        "normal_render": normal,
        "hd_render": hd,
        "mega_render": mega,
    }

    return result


def render_topology_only(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    receipts = brain_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    if not topology.exists():
        raise RuntimeError(f"TOPOLOGY_FOLDER_NOT_FOUND: {topology}")

    mmds = [p for p in sorted(topology.rglob("*.mmd")) if "render_cache" not in p.as_posix()]
    if not mmds:
        raise RuntimeError(f"NO_MMD_FILES_FOUND: {topology}")

    tracker = {"done": 0, "total": max(2, len(mmds) * 4 + 2), "start": time.time()}
    emit(progress, tracker, "render", "starting crystal MMD render pipeline", str(topology), 1)

    manifest = []
    for mmd in mmds:
        manifest.append(render_mmd_file(mmd, progress, tracker))

    manifest_path = topology / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    receipt = receipts / "render_receipt.md"
    receipt.write_text(
        "# Render Receipt\n\n"
        f"created={now()}\n"
        "pipeline=MMD_TO_SVG_VECTOR_TO_FULL_PNG_TO_HD_PNG\n"
        "renderer=mermaid_cli_mmdc\n"
        "png_policy=full_canvas_high_scale\n"
        f"rendered_files={len(manifest)}\n",
        encoding="utf-8"
    )

    emit(progress, tracker, "done", "crystal render pipeline complete", str(topology), tracker["total"] - tracker["done"])
    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "render_manifest": str(manifest_path),
        "rendered": manifest,
    }


def write_flash_prompt(stage: Path, brain_name: str):
    (stage / "FLASH_ME_FIRST_SINGLE_PROMPT.txt").write_text(
        f"""UEPC-ENV15-FLASH-BOOT-001 | Mode: flash_env + validation | Category: one-upload env flash

CHAT_NAME:
[{brain_name}]

BRIEF_NATURE_OF_CHAT:
[SQLite Brain Builder V4.2 package: locked Env/UOP/project-template law + writable generated project brain]

OPTIONAL_CHAT_LINEAGE_MD:
[NONE unless included in project/sectors/chat_lineage]

I uploaded one Env14 clean runpack ZIP. Do not ask for individual files. The ZIP is the env/package container.

First inspect the ZIP and read:
FLASH_ME_FIRST_SINGLE_PROMPT.txt
.uepc_env
.uepc_project
.uepc_profile
env/env_sqlite.sqlite
uop/uop_sqlite.sqlite
project/project_template.sqlite
project/project_router.sqlite
project/sectors/
project/topology/
project/artifacts/
manifests/
receipts/

Treat the chat window as display only. Durable state is inside the package.

Env and UOP are locked/read-only governance.
The generated project section is writable only by explicit user command.
Do not mutate env law from chat lineage unless the user explicitly says mode=flash_env.

Run compact entry slip, continue from package pointers, preserve receipts, and use MMD/SVG/HD-PNG topology as SQLite-backed topology proof.
""",
        encoding="utf-8"
    )

    (stage / "README_NEXT_PROMPT.txt").write_text(
        "# SQLite Brain Builder V4.2 Package\n\n"
        "Open order:\n"
        "1. FLASH_ME_FIRST_SINGLE_PROMPT.txt\n"
        "2. .uepc_env / .uepc_project / .uepc_profile\n"
        "3. env/env_sqlite.sqlite\n"
        "4. uop/uop_sqlite.sqlite\n"
        "5. project/project_router.sqlite\n"
        "6. project/sectors/\n"
        "7. project/topology/*.mmd + *.svg + *_HD.png\n"
        "8. project/artifacts/\n"
        "9. receipts/\n",
        encoding="utf-8"
    )


def build_manifest(stage: Path):
    rows = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            rows.append({
                "path": path.relative_to(stage).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })

    manifests = stage / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "PROJECT_BRAIN_PACKAGE_MANIFEST.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    (manifests / "PROJECT_BRAIN_PACKAGE_HASHES.txt").write_text(
        "\n".join(f"{r['sha256']}  {r['path']}" for r in rows),
        encoding="utf-8"
    )
    return rows


def zip_stage(stage: Path, zip_path: Path):
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(stage).as_posix())
    return sha256_file(zip_path)


def export_one_upload_package(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    project_root = brain_root / "project"
    if not project_root.exists():
        raise RuntimeError(f"PROJECT_ROOT_NOT_FOUND: build brain first: {project_root}")

    env14 = find_env14_resource_root()

    packages_root = brain_root / "packages"
    packages_root.mkdir(parents=True, exist_ok=True)

    slug = slugify_name(brain_name)
    stage = packages_root / f"{slug}_one_upload_package_v001"
    zip_path = packages_root / f"{slug}_one_upload_package_v001.zip"

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    topology = project_root / "topology"
    mmd_count = len(list(topology.rglob("*.mmd"))) if topology.exists() else 0
    tracker = {"done": 0, "total": max(20, 30 + mmd_count * 4), "start": time.time()}

    emit(progress, tracker, "package", "copying locked Env/UOP/project-template package from installed resources", str(env14), 1)
    copytree_overlay(env14, stage)

    emit(progress, tracker, "render", "running crystal MMD render pipeline", str(topology), 1)
    render_topology_only(workspace_dir, brain_name, progress)

    emit(progress, tracker, "package", "overlaying generated writable project brain into package/project", str(project_root), 1)
    copytree_overlay(project_root, stage / "project")

    if (brain_root / "receipts").exists():
        emit(progress, tracker, "package", "copying receipts", str(brain_root / "receipts"), 1)
        copytree_overlay(brain_root / "receipts", stage / "receipts")

    emit(progress, tracker, "package", "writing final flash prompt", str(stage / "FLASH_ME_FIRST_SINGLE_PROMPT.txt"), 1)
    write_flash_prompt(stage, brain_name)

    emit(progress, tracker, "package", "building manifest/hash ledger", str(stage / "manifests"), 1)
    manifest_rows = build_manifest(stage)

    emit(progress, tracker, "package", "zipping final one-upload package", str(zip_path), 1)
    package_hash = zip_stage(stage, zip_path)

    receipt_text = (
        "# Final Package Receipt\n\n"
        f"created={now()}\n"
        f"package_folder={stage}\n"
        f"package_zip={zip_path}\n"
        f"package_sha256={package_hash}\n"
        f"files={len(manifest_rows)}\n"
        "contains_env=yes\n"
        "contains_uop=yes\n"
        "contains_project=yes\n"
        "render_pipeline=MMD_TO_SVG_VECTOR_TO_FULL_PNG_TO_HD_PNG\n"
    )

    (brain_root / "receipts").mkdir(parents=True, exist_ok=True)
    (brain_root / "receipts" / "final_package_receipt.md").write_text(receipt_text, encoding="utf-8")
    (stage / "receipts").mkdir(parents=True, exist_ok=True)
    (stage / "receipts" / "final_package_receipt.md").write_text(receipt_text, encoding="utf-8")

    emit(progress, tracker, "done", "one-upload package ready", str(zip_path), tracker["total"] - tracker["done"])

    return {
        "brain_root": str(brain_root),
        "package_folder": str(stage),
        "package_zip": str(zip_path),
        "package_hash": package_hash,
    }
# ============================================================
# V4.3 robust render override
# Overrides render_mmd_file and render_topology_only.
# ============================================================

def _v43_run_mmdc(input_path: Path, output_path: Path, width: int | None = None, height: int | None = None):
    cli = mermaid_cli()
    cmd = [cli, "-i", str(input_path), "-o", str(output_path), "-b", "white"]
    if width and height:
        cmd += ["-w", str(width), "-H", str(height)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1200)


def _v43_svg_size(svg_path: Path) -> tuple[int, int]:
    text = svg_path.read_text(encoding="utf-8", errors="replace")
    import re
    from xml.etree import ElementTree as ET
    root = ET.fromstring(text)

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

    return int(width or 1920), int(height or 1080)


def render_mmd_file(mmd: Path, callback=None, tracker=None):
    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")
    crystal = mmd.with_name(mmd.stem + "_CRYSTAL.png")
    mega = mmd.with_name(mmd.stem + "_MEGA.png")

    if callback and tracker:
        emit(callback, tracker, "render", "rendering proper SVG vector from full MMD", str(svg), 1)

    render_input = mmd
    run = _v43_run_mmdc(render_input, svg)

    temp_input = None
    if run.returncode != 0:
        temp_input = _strip_frontmatter_for_mmdc(mmd)
        if temp_input:
            render_input = temp_input
            run = _v43_run_mmdc(render_input, svg)

    if run.returncode != 0:
        raise RuntimeError(f"SVG_RENDER_FAILED for {mmd}: {run.stderr[:3000]}")

    if temp_input and temp_input.exists():
        try:
            temp_input.unlink()
        except Exception:
            pass

    svg_w, svg_h = _v43_svg_size(svg)

    # Render natural full-canvas PNG.
    natural_w = max(svg_w, 3840)
    natural_h = max(svg_h, 2160)

    if callback and tracker:
        emit(callback, tracker, "render", f"rendering full PNG {natural_w}x{natural_h}", str(png), 1)

    run_png = _v43_run_mmdc(svg, png, natural_w, natural_h)
    if run_png.returncode != 0:
        raise RuntimeError(f"PNG_RENDER_FAILED for {mmd}: {run_png.stderr[:3000]}")

    # Crystal PNG is slow and big: 4x actual canvas, capped high.
    crystal_w = min(max(svg_w * 4, 7680), 64000)
    crystal_h = min(max(svg_h * 4, 4320), 64000)

    if callback and tracker:
        emit(callback, tracker, "render", f"rendering CRYSTAL PNG {crystal_w}x{crystal_h}", str(crystal), 1)

    run_crystal = _v43_run_mmdc(svg, crystal, crystal_w, crystal_h)
    if run_crystal.returncode != 0:
        raise RuntimeError(f"CRYSTAL_RENDER_FAILED for {mmd}: {run_crystal.stderr[:3000]}")

    # MEGA only when graph is large but not absurd.
    mega_written = None
    if svg_w * svg_h < 80_000_000:
        mega_w = min(max(svg_w * 8, 12000), 64000)
        mega_h = min(max(svg_h * 8, 8000), 64000)
        if callback and tracker:
            emit(callback, tracker, "render", f"rendering MEGA PNG {mega_w}x{mega_h}", str(mega), 1)
        run_mega = _v43_run_mmdc(svg, mega, mega_w, mega_h)
        if run_mega.returncode == 0:
            mega_written = mega

    return {
        "mmd": str(mmd),
        "svg": str(svg),
        "png": str(png),
        "crystal_png": str(crystal),
        "mega_png": str(mega_written) if mega_written else None,
        "svg_width": svg_w,
        "svg_height": svg_h,
        "mmd_sha256": sha256_file(mmd),
        "svg_sha256": sha256_file(svg),
        "png_sha256": sha256_file(png),
        "crystal_png_sha256": sha256_file(crystal),
        "mega_png_sha256": sha256_file(mega_written) if mega_written else None,
        "status": "V4_3_FULL_MMD_TO_PROPER_SVG_TO_CRYSTAL_PNG",
    }


def render_topology_only(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    receipts = brain_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    if not topology.exists():
        raise RuntimeError(f"TOPOLOGY_FOLDER_NOT_FOUND: {topology}")

    mmds = [p for p in sorted(topology.glob("*.mmd")) if "render_cache" not in p.as_posix()]
    if not mmds:
        raise RuntimeError(f"NO_MMD_FILES_FOUND: {topology}")

    tracker = {"done": 0, "total": max(2, len(mmds) * 4 + 2), "start": time.time()}
    emit(progress, tracker, "render", "starting V4.3 full MMD render pipeline", str(topology), 1)

    manifest = []
    for mmd in mmds:
        manifest.append(render_mmd_file(mmd, progress, tracker))

    manifest_path = topology / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    receipt = receipts / "render_receipt.md"
    receipt.write_text(
        "# Render Receipt\n\n"
        f"created={now()}\n"
        "pipeline=FULL_MMD_TO_PROPER_SVG_TO_CRYSTAL_PNG\n"
        "renderer=mermaid_cli_mmdc\n"
        f"rendered_files={len(manifest)}\n",
        encoding="utf-8"
    )

    emit(progress, tracker, "done", "V4.3 render pipeline complete", str(topology), tracker["total"] - tracker["done"])

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "render_manifest": str(manifest_path),
        "rendered": manifest,
    }

# ============================================================
# V4.5 parse-safe render override
# ============================================================

def _v45_allowed_mmd(path: Path) -> bool:
    stem = path.stem
    return stem.startswith("local_code") or stem.startswith("github") or stem.startswith("project_master")


def _v45_cleanup_non_code_renders(topology: Path):
    if not topology.exists():
        return
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        if not _v45_allowed_mmd(p):
            try:
                p.unlink()
            except Exception:
                pass


def _v45_sanitize_mmd_for_render(mmd: Path) -> Path:
    text = mmd.read_text(encoding="utf-8", errors="replace")
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")

    lines = text.splitlines()
    cleaned = []
    found_graph = False

    graph_prefixes = (
        "flowchart ",
        "graph ",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram",
        "erDiagram",
        "gantt",
        "pie",
        "mindmap",
        "timeline",
    )

    for line in lines:
        stripped = line.strip()

        if not found_graph:
            if not stripped:
                continue
            if stripped.startswith("---"):
                continue
            if stripped.lower().startswith("title:"):
                continue
            if stripped.startswith("%%{init"):
                continue
            if stripped.startswith(graph_prefixes):
                cleaned.append(stripped)
                found_graph = True
                continue
            continue

        if stripped.startswith("---"):
            continue
        if stripped.lower().startswith("title:"):
            continue
        cleaned.append(line)

    if not found_graph:
        raise RuntimeError(f"MMD_NO_GRAPH_DIRECTIVE_AFTER_SANITIZE: {mmd}")

    tmp = Path(tempfile.gettempdir()) / f"{mmd.stem}_{int(time.time()*1000)}_v45_clean.mmd"
    tmp.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    return tmp


def _v45_run_mmdc(input_path: Path, output_path: Path, width: int | None = None, height: int | None = None):
    cli = mermaid_cli()
    cmd = [cli, "-i", str(input_path), "-o", str(output_path), "-b", "white"]
    if width and height:
        cmd += ["-w", str(width), "-H", str(height)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1200)


def _v45_svg_size(svg_path: Path) -> tuple[int, int]:
    import re
    from xml.etree import ElementTree as ET

    raw = svg_path.read_text(encoding="utf-8", errors="replace")
    root = ET.fromstring(raw)

    def num(value):
        if not value:
            return None
        m = re.search(r"[-+]?\d*\.?\d+", str(value))
        return float(m.group(0)) if m else None

    width = num(root.attrib.get("width"))
    height = num(root.attrib.get("height"))
    viewbox = root.attrib.get("viewBox") or root.attrib.get("viewbox")

    if (not width or not height) and viewbox:
        parts = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", viewbox)]
        if len(parts) == 4:
            width = width or parts[2]
            height = height or parts[3]

    return int(width or 1920), int(height or 1080)


def render_mmd_file(mmd: Path, callback=None, tracker=None):
    if not _v45_allowed_mmd(mmd):
        raise RuntimeError(f"NON_CODE_MMD_RENDER_BLOCKED: {mmd}")

    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")
    crystal = mmd.with_name(mmd.stem + "_CRYSTAL.png")

    clean = _v45_sanitize_mmd_for_render(mmd)

    try:
        if callback and tracker:
            emit(callback, tracker, "render", "rendering parse-safe SVG", str(svg), 1)

        svg_run = _v45_run_mmdc(clean, svg)
        if svg_run.returncode != 0:
            raise RuntimeError(f"SVG_RENDER_FAILED for {mmd}: {svg_run.stderr[:3500]}")

        svg_w, svg_h = _v45_svg_size(svg)

        normal_w = max(svg_w, 3840)
        normal_h = max(svg_h, 2160)

        if callback and tracker:
            emit(callback, tracker, "render", f"rendering full PNG {normal_w}x{normal_h}", str(png), 1)

        png_run = _v45_run_mmdc(clean, png, normal_w, normal_h)
        if png_run.returncode != 0:
            raise RuntimeError(f"PNG_RENDER_FAILED for {mmd}: {png_run.stderr[:3500]}")

        crystal_w = min(max(svg_w * 4, 7680), 64000)
        crystal_h = min(max(svg_h * 4, 4320), 64000)

        if callback and tracker:
            emit(callback, tracker, "render", f"rendering CRYSTAL PNG {crystal_w}x{crystal_h}", str(crystal), 1)

        crystal_run = _v45_run_mmdc(clean, crystal, crystal_w, crystal_h)
        if crystal_run.returncode != 0:
            raise RuntimeError(f"CRYSTAL_RENDER_FAILED for {mmd}: {crystal_run.stderr[:3500]}")

        return {
            "mmd": str(mmd),
            "svg": str(svg),
            "png": str(png),
            "crystal_png": str(crystal),
            "svg_width": svg_w,
            "svg_height": svg_h,
            "mmd_sha256": sha256_file(mmd),
            "svg_sha256": sha256_file(svg),
            "png_sha256": sha256_file(png),
            "crystal_png_sha256": sha256_file(crystal),
            "status": "V4_5_PARSE_SAFE_MMD_TO_SVG_TO_CRYSTAL_PNG",
        }

    finally:
        try:
            clean.unlink()
        except Exception:
            pass


def render_topology_only(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    receipts = brain_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    if not topology.exists():
        raise RuntimeError(f"TOPOLOGY_FOLDER_NOT_FOUND: {topology}")

    try:
        from sqlite_brain_builder.runtime.topology_writer_v43 import regenerate_topology_from_existing_brain
        regenerate_topology_from_existing_brain(workspace_dir, brain_name)
    except Exception:
        pass

    _v45_cleanup_non_code_renders(topology)

    mmds = [p for p in sorted(topology.glob("*.mmd")) if _v45_allowed_mmd(p)]
    if not mmds:
        raise RuntimeError(f"NO_CODE_MMD_FILES_FOUND: {topology}")

    tracker = {"done": 0, "total": max(2, len(mmds) * 3 + 2), "start": time.time()}
    emit(progress, tracker, "render", "starting V4.5 parse-safe code MMD render pipeline", str(topology), 1)

    manifest = []
    for mmd in mmds:
        manifest.append(render_mmd_file(mmd, progress, tracker))

    manifest_path = topology / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    receipt = receipts / "render_receipt.md"
    receipt.write_text(
        "# Render Receipt\n\n"
        f"created={now()}\n"
        "pipeline=PARSE_SAFE_CODE_MMD_TO_SVG_TO_CRYSTAL_PNG\n"
        "mmd_policy=code_and_project_master_only\n"
        "renderer=mermaid_cli_mmdc\n"
        f"rendered_files={len(manifest)}\n",
        encoding="utf-8"
    )

    emit(progress, tracker, "done", "V4.5 render pipeline complete", str(topology), tracker["total"] - tracker["done"])

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "render_manifest": str(manifest_path),
        "rendered": manifest,
    }

# ============================================================
# V4.6 render override: regenerate smooth universal workflow MMDs first.
# ============================================================

def _v46_allowed_mmd(path: Path) -> bool:
    return path.stem in {"local_code_lane", "project_master_topology"}


def _v46_cleanup_render_targets(topology: Path):
    if not topology.exists():
        return
    allowed = {"local_code_lane", "project_master_topology"}
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        stem = p.stem
        for suffix in ["_CRYSTAL", "_HD", "_MEGA"]:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem not in allowed:
            try:
                p.unlink()
            except Exception:
                pass


def render_topology_only(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    receipts = brain_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    from sqlite_brain_builder.runtime.topology_writer_v46 import regenerate_topology_from_existing_brain
    regenerate_topology_from_existing_brain(workspace_dir, brain_name)

    _v46_cleanup_render_targets(topology)

    mmds = [p for p in sorted(topology.glob("*.mmd")) if _v46_allowed_mmd(p)]
    if not mmds:
        raise RuntimeError(f"NO_CODE_WORKFLOW_MMD_FOUND: {topology}")

    tracker = {"done": 0, "total": max(2, len(mmds) * 3 + 2), "start": time.time()}
    emit(progress, tracker, "render", "starting V4.6 smooth workflow MMD render pipeline", str(topology), 1)

    manifest = []
    for mmd in mmds:
        manifest.append(render_mmd_file(mmd, progress, tracker))

    manifest_path = topology / "render_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    receipt = receipts / "render_receipt.md"
    receipt.write_text(
        "# Render Receipt\n\n"
        f"created={now()}\n"
        "pipeline=SMOOTH_CODE_WORKFLOW_MMD_TO_SVG_TO_CRYSTAL_PNG\n"
        "mmd_policy=local_code_lane_and_project_master_only\n"
        "renderer=mermaid_cli_mmdc\n"
        f"rendered_files={len(manifest)}\n",
        encoding="utf-8"
    )

    emit(progress, tracker, "done", "V4.6 smooth render pipeline complete", str(topology), tracker["total"] - tracker["done"])

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "render_manifest": str(manifest_path),
        "rendered": manifest,
    }
