from __future__ import annotations

from sqlite_brain_builder.runtime.fast_backend import build_fast_brain as _base_build_fast_brain
from sqlite_brain_builder.runtime.sector_schema_v48 import ensure_all_sector_schemas


def build_fast_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress=None):
    result = _base_build_fast_brain(workspace_dir, brain_name, sources, progress)
    result["sector_schemas"] = ensure_all_sector_schemas(workspace_dir, brain_name)
    return result
