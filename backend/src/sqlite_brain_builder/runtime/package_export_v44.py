from __future__ import annotations

import shutil
from pathlib import Path

from sqlite_brain_builder.runtime.package_export_v41 import (
    export_one_upload_package as _base_export,
    render_topology_only,
    find_env14_resource_root,
    copytree_overlay,
)
from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.sector_skeleton_v44 import ensure_all_sector_skeletons, cleanup_non_code_mmds


def _preserve_locked_public_project_template(package_folder: Path):
    public_project = package_folder / "project"
    locked_template = package_folder / "project_template_locked"

    if public_project.exists() and not locked_template.exists():
        copytree_overlay(public_project, locked_template)

    if public_project.exists():
        shutil.rmtree(public_project)


def export_one_upload_package(workspace_dir: str, brain_name: str, progress=None):
    ensure_all_sector_skeletons(workspace_dir, brain_name)
    brain_root = brain_output_dir(workspace_dir, brain_name)
    cleanup_non_code_mmds(brain_root / "project" / "topology")

    result = _base_export(workspace_dir, brain_name, progress)

    # Re-stage correction:
    # package_export_v41 copies Env14 then overlays generated project.
    # This wrapper preserves the locked Env14 project template separately too.
    package_folder = Path(result["package_folder"])

    envroot = find_env14_resource_root()
    locked_template = package_folder / "project_template_locked"
    if not locked_template.exists() and (envroot / "project").exists():
        copytree_overlay(envroot / "project", locked_template)

    return result
