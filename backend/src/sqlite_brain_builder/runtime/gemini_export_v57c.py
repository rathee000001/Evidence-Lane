from __future__ import annotations

import importlib
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.flash_prompt_v57c import write_named_gemini_zip


def _load_base_gemini():
    candidates = [
        "sqlite_brain_builder.runtime.gemini_export_v57b",
        "sqlite_brain_builder.runtime.gemini_export_v57",
        "sqlite_brain_builder.runtime.gemini_export_v56",
        "sqlite_brain_builder.runtime.gemini_export_v55",
        "sqlite_brain_builder.runtime.gemini_export_v54",
        "sqlite_brain_builder.runtime.gemini_export_v53",
        "sqlite_brain_builder.runtime.gemini_export_v50",
    ]

    for name in candidates:
        try:
            mod = importlib.import_module(name)
            fn = getattr(mod, "export_gemini_exact10", None) or getattr(mod, "export_gemini_compatible_package", None)
            if fn:
                return fn
        except Exception:
            continue

    raise RuntimeError("BASE_GEMINI_EXPORT_FUNCTION_NOT_FOUND")


def export_gemini_exact10(workspace_dir: str, brain_name: str, progress=None):
    result = _load_base_gemini()(workspace_dir, brain_name, progress)

    brain_root = Path(result.get("brain_root") or brain_output_dir(workspace_dir, brain_name))
    packages = brain_root / "packages"

    source_zip = None
    for key in ["gemini_package_zip", "package_zip"]:
        if result.get(key) and Path(result[key]).exists():
            source_zip = Path(result[key])
            break

    if source_zip is None:
        found = sorted(packages.glob("*gemini*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            raise RuntimeError("BASE_GEMINI_ZIP_NOT_FOUND")
        source_zip = found[0]

    named_zip = write_named_gemini_zip(source_zip, brain_name)

    out = dict(result)
    out["gemini_package_zip"] = str(named_zip)
    out["package_zip"] = str(named_zip)
    out["package_name"] = named_zip.name
    out["status"] = "GEMINI_1_CLICK_EXPORT_READY"
    return out


def export_gemini_compatible_package(workspace_dir: str, brain_name: str, progress=None):
    return export_gemini_exact10(workspace_dir, brain_name, progress)
