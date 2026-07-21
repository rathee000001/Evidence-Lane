from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable

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


def find_env14_resource_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS) / "sqlite_brain_builder" / "resources" / "public_model_env15"
    else:
        root = Path(__file__).resolve().parents[1] / "resources" / "public_model_env15"

    hits = list(root.rglob(".uepc_env")) if root.exists() else []
    if hits:
        return hits[0].parent

    raise RuntimeError(f"EMBEDDED_ENV15_RESOURCE_NOT_FOUND: {root}")


def mermaid_cli() -> str:
    found = shutil.which("mmdc") or shutil.which("mmdc.cmd") or shutil.which("mmdc.exe")
    if not found:
        raise RuntimeError("MERMAID_CLI_NOT_FOUND: install with `npm install -g @mermaid-js/mermaid-cli`, then verify `mmdc --version`.")
    return found


def copytree_overlay(src: Path, dst: Path, skip_names: set[str] | None = None):
    skip_names = skip_names or set()
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in skip_names:
            continue
        target = dst / item.name
        if item.is_dir():
            copytree_overlay(item, target, skip_names=skip_names)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def render_mmd_file(mmd: Path, callback=None, tracker=None):
    cli = mermaid_cli()
    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")

    cmd_svg = [cli, "-i", str(mmd), "-o", str(svg), "-b", "white"]
    cmd_png = [cli, "-i", str(mmd), "-o", str(png), "-b", "white", "-w", "3840", "-H", "2160"]

    if callback and tracker:
        emit(callback, tracker, "render", "rendering SVG", str(svg), 1)

    svg_run = subprocess.run(cmd_svg, capture_output=True, text=True, timeout=180)
    if svg_run.returncode != 0:
        raise RuntimeError(f"SVG_RENDER_FAILED for {mmd}: {svg_run.stderr[:2000]}")

    if callback and tracker:
        emit(callback, tracker, "render", "rendering HD PNG", str(png), 1)

    png_run = subprocess.run(cmd_png, capture_output=True, text=True, timeout=240)
    if png_run.returncode != 0:
        raise RuntimeError(f"PNG_RENDER_FAILED for {mmd}: {png_run.stderr[:2000]}")

    return {
        "mmd": str(mmd),
        "svg": str(svg),
        "png": str(png),
        "mmd_sha256": sha256_file(mmd),
        "svg_sha256": sha256_file(svg),
        "png_sha256": sha256_file(png),
        "png_width": 3840,
        "png_height": 2160,
        "status": "RENDERED_SVG_THEN_HD_PNG",
    }


def render_project_topology(brain_root: Path, callback=None, tracker=None):
    topology = brain_root / "project" / "topology"
    receipts = brain_root / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)

    mmds = sorted(topology.rglob("*.mmd")) if topology.exists() else []
    if not mmds:
        raise RuntimeError(f"NO_MMD_FILES_FOUND: {topology}")

    manifest = []
    for mmd in mmds:
        manifest.append(render_mmd_file(mmd, callback, tracker))

    render_manifest = topology / "render_manifest.json"
    render_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    receipt = receipts / "render_receipt.md"
    receipt.write_text(
        "# Render Receipt\n\n"
        f"created={now()}\n"
        f"renderer=mermaid_cli_mmdc\n"
        f"render_order=MMD_TO_SVG_FIRST_TO_HD_PNG_SECOND\n"
        f"rendered_files={len(manifest)}\n",
        encoding="utf-8"
    )

    return manifest


def write_flash_prompt(stage: Path, brain_name: str):
    flash = stage / "FLASH_ME_FIRST_SINGLE_PROMPT.txt"
    flash.write_text(
        f"""UEPC-ENV15-FLASH-BOOT-001 | Mode: flash_env + validation | Category: one-upload env flash

CHAT_NAME:
[{brain_name}]

BRIEF_NATURE_OF_CHAT:
[SQLite Brain Builder V4 one-upload package: locked Env/UOP + writable project brain]

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
The project section is the writable project brain section, but only by explicit user command.
Do not mutate env law from chat lineage unless the user explicitly says mode=flash_env.

Run compact entry slip, continue from package pointers, preserve receipts, and use SVG/PNG topology renders only as topology proof backed by SQLite.
""",
        encoding="utf-8"
    )

    readme = stage / "README_NEXT_PROMPT.txt"
    readme.write_text(
        f"""# SQLite Brain Builder V4 Package

Open order:
1. FLASH_ME_FIRST_SINGLE_PROMPT.txt
2. .uepc_env / .uepc_project / .uepc_profile
3. env/env_sqlite.sqlite
4. uop/uop_sqlite.sqlite
5. project/project_router.sqlite
6. project/sectors/
7. project/topology/*.mmd + *.svg + *.png
8. receipts/
""",
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
    manifest_path = manifests / "PROJECT_BRAIN_PACKAGE_MANIFEST.json"
    manifest_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    hash_txt = manifests / "PROJECT_BRAIN_PACKAGE_HASHES.txt"
    hash_txt.write_text("\n".join(f"{r['sha256']}  {r['path']}" for r in rows), encoding="utf-8")

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
    if not brain_root.exists():
        raise RuntimeError(f"BRAIN_ROOT_NOT_FOUND: {brain_root}")

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

    total = 100
    mmd_count = len(list((brain_root / "project" / "topology").rglob("*.mmd"))) if (brain_root / "project" / "topology").exists() else 0
    total += max(1, mmd_count * 2)

    tracker = {"done": 0, "total": total, "start": time.time()}

    emit(progress, tracker, "package", "validating embedded Env14", str(env14), 1)

    # 1. Copy locked Env/UOP/project-template public model package into stage root.
    emit(progress, tracker, "package", "copying locked Env/UOP package into staging folder", str(stage), 1)
    copytree_overlay(env14, stage)

    # 2. Render project MMDs before overlay/package.
    emit(progress, tracker, "render", "rendering project MMD topology to SVG then HD PNG", str(project_root / "topology"), 1)
    render_project_topology(brain_root, progress, tracker)

    # 3. Overlay writable generated project brain into package/project.
    emit(progress, tracker, "package", "overlaying writable generated project brain", str(project_root), 1)
    copytree_overlay(project_root, stage / "project")

    # 4. Overlay receipts.
    if (brain_root / "receipts").exists():
        emit(progress, tracker, "package", "copying build/render receipts", str(brain_root / "receipts"), 1)
        copytree_overlay(brain_root / "receipts", stage / "receipts")

    # 5. Write correct flash prompt at root after overlay.
    emit(progress, tracker, "package", "writing final flash prompt", str(stage / "FLASH_ME_FIRST_SINGLE_PROMPT.txt"), 1)
    write_flash_prompt(stage, brain_name)

    # 6. Build manifest/hash ledger.
    emit(progress, tracker, "package", "building package manifest and hash ledger", str(stage / "manifests"), 1)
    manifest_rows = build_manifest(stage)

    # 7. Zip final folder.
    emit(progress, tracker, "package", "zipping final one-upload package", str(zip_path), 1)
    package_hash = zip_stage(stage, zip_path)

    # 8. Final receipt outside and inside package folder.
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
        "render_pipeline=MMD_TO_SVG_TO_HD_PNG\n"
    )
    (brain_root / "receipts" / "final_package_receipt.md").write_text(receipt_text, encoding="utf-8")
    (stage / "receipts").mkdir(parents=True, exist_ok=True)
    (stage / "receipts" / "final_package_receipt.md").write_text(receipt_text, encoding="utf-8")

    emit(progress, tracker, "done", "one-upload package ready", str(zip_path), tracker["total"] - tracker["done"])

    if progress:
        progress({
            "stage": "done",
            "task": "complete",
            "file": str(zip_path),
            "done": tracker["total"],
            "total": tracker["total"],
            "percent": 100,
            "elapsed_seconds": int(time.time() - tracker["start"]),
            "eta_seconds": 0,
            "finish_epoch": int(time.time()),
        })

    return {
        "brain_root": str(brain_root),
        "package_folder": str(stage),
        "package_zip": str(zip_path),
        "package_hash": package_hash,
    }
