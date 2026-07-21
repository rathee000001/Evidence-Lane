from __future__ import annotations

import importlib
from pathlib import Path

from sqlite_brain_builder.runtime.chatlineage_state_travel_v57 import ensure_chat_lineage_sector, update_chat_lineage_pointer, write_append_packet_template
from sqlite_brain_builder.runtime.path_policy import brain_output_dir


def _load_base_build():
    candidates = [
        "sqlite_brain_builder.runtime.app_backend_v56",
        "sqlite_brain_builder.runtime.app_backend_v55",
        "sqlite_brain_builder.runtime.app_backend_v53",
        "sqlite_brain_builder.runtime.app_backend_v52",
        "sqlite_brain_builder.runtime.app_backend_v51",
        "sqlite_brain_builder.runtime.app_backend_v50",
        "sqlite_brain_builder.runtime.app_backend_v49",
        "sqlite_brain_builder.runtime.app_backend_v48",
        "sqlite_brain_builder.runtime.fast_backend",
    ]
    for name in candidates:
        try:
            mod = importlib.import_module(name)
            fn = getattr(mod, "build_fast_brain", None)
            if callable(fn):
                return fn
        except Exception:
            continue
    raise RuntimeError("BASE_BUILD_FUNCTION_NOT_FOUND")


_BASE_BUILD = _load_base_build()


def build_fast_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress=None):
    result = _BASE_BUILD(workspace_dir, brain_name, sources, progress)

    brain_root = Path(result.get("brain_root") or brain_output_dir(workspace_dir, brain_name))
    project_root = brain_root / "project"
    ensure_chat_lineage_sector(project_root)
    update_chat_lineage_pointer(project_root)
    write_append_packet_template(project_root)

    if isinstance(result, dict):
        result["chat_lineage_append_only_schema"] = str(project_root / "sectors" / "chat_lineage" / "chat_lineage_sector_v001.sqlite")
    return result
