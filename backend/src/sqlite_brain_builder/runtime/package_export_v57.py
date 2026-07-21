from __future__ import annotations

import importlib

from sqlite_brain_builder.runtime.chatlineage_state_travel_v57 import patch_export_result


def _load_base_export():
    candidates = [
        "sqlite_brain_builder.runtime.package_export_v56",
        "sqlite_brain_builder.runtime.package_export_v55",
        "sqlite_brain_builder.runtime.package_export_v54",
        "sqlite_brain_builder.runtime.package_export_v53",
        "sqlite_brain_builder.runtime.package_export_v52",
        "sqlite_brain_builder.runtime.package_export_v51",
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
            if callable(fn):
                return fn
        except Exception:
            continue
    raise RuntimeError("BASE_EXPORT_FUNCTION_NOT_FOUND")


_BASE_EXPORT = _load_base_export()


def export_one_upload_package(workspace_dir: str, brain_name: str, progress=None):
    result = _BASE_EXPORT(workspace_dir, brain_name, progress)
    return patch_export_result(workspace_dir, brain_name, result)
