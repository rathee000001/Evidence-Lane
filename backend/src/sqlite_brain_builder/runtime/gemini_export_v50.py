from __future__ import annotations

import hashlib
import json
import re
import shutil
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


def emit(callback, stage, task, file="", percent=0):
    if callback:
        callback({
            "stage": stage,
            "task": task,
            "file": str(file),
            "percent": int(percent),
            "done": int(percent),
            "total": 100,
            "elapsed_seconds": 0,
            "eta_seconds": "--",
        })


def sanitize_flat_name(rel: str) -> str:
    rel = rel.replace("\\", "/").strip("/")
    rel = re.sub(r"[^A-Za-z0-9._/-]+", "_", rel)
    return rel.replace("/", "__")


def find_standard_package_folder(brain_root: Path, brain_name: str) -> Path | None:
    slug = slugify_name(brain_name)
    packages = brain_root / "packages"

    candidates = [
        packages / f"{slug}_one_upload_package_v001",
        packages / f"{slug.lower()}_one_upload_package_v001",
        packages / "new_brain_one_upload_package_v001",
    ]

    for c in candidates:
        if c.exists() and c.is_dir():
            return c

    found = sorted(packages.glob("*one_upload_package_v001"), key=lambda p: p.stat().st_mtime, reverse=True) if packages.exists() else []
    for c in found:
        if c.is_dir() and "gemini" not in c.name.lower():
            return c

    return None


def call_standard_export_if_needed(workspace_dir: str, brain_name: str, callback=None) -> Path:
    brain_root = brain_output_dir(workspace_dir, brain_name)
    package_folder = find_standard_package_folder(brain_root, brain_name)
    if package_folder:
        return package_folder

    emit(callback, "gemini_export", "standard one-upload package missing; creating it first", brain_root, 5)

    # Try newest wrapper first, then fallback.
    try:
        from sqlite_brain_builder.runtime.package_export_v49 import export_one_upload_package
    except Exception:
        try:
            from sqlite_brain_builder.runtime.package_export_v44 import export_one_upload_package
        except Exception:
            from sqlite_brain_builder.runtime.package_export_v41 import export_one_upload_package

    result = export_one_upload_package(workspace_dir, brain_name, callback)
    folder = result.get("package_folder")
    if folder and Path(folder).exists():
        return Path(folder)

    package_folder = find_standard_package_folder(brain_root, brain_name)
    if package_folder:
        return package_folder

    raise RuntimeError("STANDARD_PACKAGE_FOLDER_NOT_FOUND_AFTER_EXPORT")


def should_flatten_to_active_context(rel: str) -> bool:
    r = rel.replace("\\", "/").strip("/")
    low = r.lower()

    root_exact = {
        "flash_me_first_single_prompt.txt",
        "readme_next_prompt.txt",
        ".uepc_env",
        ".uepc_project",
        ".uepc_profile",
    }
    if low in root_exact:
        return True

    if low.startswith("manifests/"):
        return True

    if low.startswith("receipts/") and low.endswith((".md", ".txt", ".json")):
        return True

    if low.startswith("env/") or low.startswith("uop/"):
        return low.endswith((".sqlite", ".db", ".md", ".json", ".txt", ".mmd", ".svg", ".png"))

    if low.startswith("project_template_locked/"):
        return low.endswith((".sqlite", ".db", ".md", ".json", ".txt", ".mmd", ".svg", ".png"))

    if low == "project/project_router.sqlite":
        return True

    if low in {
        "project/project_pointer.json",
        "project/sector_index.json",
    }:
        return True

    if low.startswith("project/pointers/") and low.endswith(".json"):
        return True

    if low.startswith("project/sectors/") and low.endswith((".sqlite", ".db")):
        return True

    if low.startswith("project/topology/") and low.endswith((".mmd", ".svg", ".png", ".json")):
        return True

    if low.startswith("project/artifacts/") and low.endswith((".json", ".txt", ".md")):
        return True

    return False


def write_gemini_prompt(stage: Path, package_folder: Path, active_files: list[dict], payload_manifest: list[dict]):
    boot = stage / "00_BOOT_AND_POINTERS"
    boot.mkdir(parents=True, exist_ok=True)

    prompt = boot / "GEMINI_FLASH_PROMPT.txt"
    prompt.write_text(
        """UEPC-GEMINI-COMPATIBLE-BOOT-001 | Mode: pointer_first_validation | Category: low-file-count one-upload package

CHAT_NAME:
[GOLDV3]

BRIEF_NATURE_OF_CHAT:
[Gemini-compatible SQLite Brain Builder package: active pointers + closed full payload archive]

I uploaded one Gemini-compatible UEPC package.

Do not recursively unpack closed payload first.
Do not complain about missing nested folders until you inspect active context.

READ ORDER:
1. 00_BOOT_AND_POINTERS/GEMINI_FLASH_PROMPT.txt
2. 00_BOOT_AND_POINTERS/GEMINI_PACKAGE_MAP.json
3. 01_ACTIVE_CONTEXT/
4. 01_ACTIVE_CONTEXT/project__project_router.sqlite
5. 01_ACTIVE_CONTEXT/project__sector_index.json
6. 01_ACTIVE_CONTEXT/project__pointers__*.json
7. 01_ACTIVE_CONTEXT/project__sectors__*.sqlite
8. 01_ACTIVE_CONTEXT/project__topology__*.mmd / *.svg / *.png

CLOSED PAYLOAD:
02_FULL_PAYLOAD_CLOSED_DATALOOP/full_payload_preserved_original_paths.zip

The closed payload archive preserves every original file and original path. Do not open it unless the user explicitly asks for raw payload inspection.

LOCK RULES:
Env and UOP are locked/read-only.
Public project template is locked/read-only.
Generated project sector DBs are the fillable project brain, but only by explicit user command.
Chat window is display only.
Pointer files and SQLite DBs are the durable state.

Run compact entry slip:
package_seen=
gemini_compatible=
active_context_seen=
closed_payload_present=
env_lock=
uop_lock=
project_template_lock=
generated_project_seen=
blocker_if_any=
""",
        encoding="utf-8"
    )

    (boot / "README_FIRST.md").write_text(
        "# Gemini Compatible Package\n\n"
        "This package reduces visible file-count for Gemini.\n\n"
        "- `01_ACTIVE_CONTEXT/` contains flattened boot, pointer, SQLite, topology, manifest, and receipt files.\n"
        "- `02_FULL_PAYLOAD_CLOSED_DATALOOP/full_payload_preserved_original_paths.zip` preserves every original file and path.\n"
        "- No source file is deleted; the raw package is sealed as a closed dataloop payload.\n",
        encoding="utf-8"
    )

    (boot / "GEMINI_PACKAGE_MAP.json").write_text(
        json.dumps(
            {
                "created_at": now(),
                "source_package_folder": str(package_folder),
                "active_context_file_count": len(active_files),
                "closed_payload_file_count": len(payload_manifest),
                "active_context": "01_ACTIVE_CONTEXT/",
                "closed_payload": "02_FULL_PAYLOAD_CLOSED_DATALOOP/full_payload_preserved_original_paths.zip",
                "policy": "Gemini reads active context first; closed payload preserves all files without exposing thousands of loose entries.",
            },
            indent=2,
        ),
        encoding="utf-8"
    )


def zip_folder(folder: Path, zip_path: Path):
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                z.write(file, file.relative_to(folder).as_posix())
    return sha256_file(zip_path)


def export_gemini_compatible_package(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    package_folder = call_standard_export_if_needed(workspace_dir, brain_name, progress)

    packages_root = brain_root / "packages"
    packages_root.mkdir(parents=True, exist_ok=True)

    slug = slugify_name(brain_name)
    stage = packages_root / f"{slug}_gemini_compatible_package_v001"
    zip_path = packages_root / f"{slug}_gemini_compatible_package_v001.zip"

    if stage.exists():
        shutil.rmtree(stage)

    boot = stage / "00_BOOT_AND_POINTERS"
    active = stage / "01_ACTIVE_CONTEXT"
    closed = stage / "02_FULL_PAYLOAD_CLOSED_DATALOOP"

    boot.mkdir(parents=True, exist_ok=True)
    active.mkdir(parents=True, exist_ok=True)
    closed.mkdir(parents=True, exist_ok=True)

    emit(progress, "gemini_export", "walking standard package and building manifests", package_folder, 10)

    all_files = [p for p in sorted(package_folder.rglob("*")) if p.is_file()]
    payload_manifest = []
    active_files = []

    payload_zip = closed / "full_payload_preserved_original_paths.zip"
    if payload_zip.exists():
        payload_zip.unlink()

    emit(progress, "gemini_export", "sealing full original package into closed dataloop archive", payload_zip, 25)

    with zipfile.ZipFile(payload_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for idx, file in enumerate(all_files, start=1):
            rel = file.relative_to(package_folder).as_posix()
            z.write(file, rel)
            payload_manifest.append({
                "path": rel,
                "size_bytes": file.stat().st_size,
                "sha256": sha256_file(file),
            })

            if should_flatten_to_active_context(rel):
                flat_name = sanitize_flat_name(rel)
                dest = active / flat_name
                shutil.copy2(file, dest)
                active_files.append({
                    "original_path": rel,
                    "active_context_file": flat_name,
                    "size_bytes": dest.stat().st_size,
                    "sha256": sha256_file(dest),
                })

            if idx % 100 == 0:
                emit(progress, "gemini_export", f"sealed {idx}/{len(all_files)} files", file, min(80, 25 + int(idx * 50 / max(1, len(all_files)))))

    (closed / "full_payload_manifest.json").write_text(json.dumps(payload_manifest, indent=2), encoding="utf-8")
    (active / "ACTIVE_CONTEXT_INDEX.json").write_text(json.dumps(active_files, indent=2), encoding="utf-8")

    emit(progress, "gemini_export", "writing Gemini boot prompt and package map", boot, 85)
    write_gemini_prompt(stage, package_folder, active_files, payload_manifest)

    emit(progress, "gemini_export", "zipping Gemini compatible package", zip_path, 95)
    digest = zip_folder(stage, zip_path)

    receipt = boot / "GEMINI_EXPORT_RECEIPT.md"
    receipt.write_text(
        "# Gemini Compatible Export Receipt\n\n"
        f"created={now()}\n"
        f"source_package_folder={package_folder}\n"
        f"gemini_package_folder={stage}\n"
        f"gemini_package_zip={zip_path}\n"
        f"gemini_package_sha256={digest}\n"
        f"active_context_files={len(active_files)}\n"
        f"closed_payload_files={len(payload_manifest)}\n"
        "all_files_preserved=yes\n"
        "outer_package_folder_count=3\n",
        encoding="utf-8"
    )

    # Re-zip after adding receipt.
    digest = zip_folder(stage, zip_path)

    emit(progress, "done", "Gemini compatible package ready", zip_path, 100)

    return {
        "brain_root": str(brain_root),
        "source_package_folder": str(package_folder),
        "gemini_package_folder": str(stage),
        "gemini_package_zip": str(zip_path),
        "gemini_package_hash": digest,
        "active_context_files": len(active_files),
        "closed_payload_files": len(payload_manifest),
    }
