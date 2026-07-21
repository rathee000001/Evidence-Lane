from __future__ import annotations

from sqlite_brain_builder.runtime.package_export_v44 import export_one_upload_package as _base_export
from sqlite_brain_builder.runtime.package_render_v49 import render_topology_only
from sqlite_brain_builder.runtime.sector_schema_v49 import ensure_all_sector_schemas

def export_one_upload_package(workspace_dir: str, brain_name: str, progress=None):
    ensure_all_sector_schemas(workspace_dir, brain_name)
    render_topology_only(workspace_dir, brain_name, progress)
    return _base_export(workspace_dir, brain_name, progress)
