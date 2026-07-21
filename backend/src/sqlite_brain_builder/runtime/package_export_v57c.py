from __future__ import annotations

import importlib
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.flash_prompt_v57c import (
    find_latest_package_folder,
    write_named_chatgpt_zip,
)


def _load_base_export():
    candidates = [
        "sqlite_brain_builder.runtime.package_export_v57b",
        "sqlite_brain_builder.runtime.package_export_v57",
        "sqlite_brain_builder.runtime.package_export_v56",
        "sqlite_brain_builder.runtime.package_export_v55",
        "sqlite_brain_builder.runtime.package_export_v54",
        "sqlite_brain_builder.runtime.package_export_v53",
        "sqlite_brain_builder.runtime.package_export_v50",
        "sqlite_brain_builder.runtime.package_export_v49",
        "sqlite_brain_builder.runtime.package_export_v48",
        "sqlite_brain_builder.runtime.package_export_v44",
        "sqlite_brain_builder.runtime.package_export_v41",
    ]

    for name in candidates:
        try:
            mod = importlib.import_module(name)
            fn = getattr(mod, "export_one_upload_package", None)
            if fn:
                return fn
        except Exception:
            continue

    raise RuntimeError("BASE_EXPORT_FUNCTION_NOT_FOUND")


def export_one_upload_package(workspace_dir: str, brain_name: str, progress=None):
    result = _load_base_export()(workspace_dir, brain_name, progress)

    brain_root = Path(result.get("brain_root") or brain_output_dir(workspace_dir, brain_name))
    package_folder = None

    if result.get("package_folder") and Path(result["package_folder"]).exists():
        package_folder = Path(result["package_folder"])
    else:
        package_folder = find_latest_package_folder(brain_root)

    named_zip = write_named_chatgpt_zip(package_folder, brain_name)

    out = dict(result)
    out["package_folder"] = str(package_folder)
    out["package_zip"] = str(named_zip)
    out["package_name"] = named_zip.name
    out["status"] = "CHATGPT_1_CLICK_EXPORT_READY"
    return out
