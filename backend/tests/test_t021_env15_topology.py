from __future__ import annotations

from pathlib import Path
import hashlib
import json

from sqlite_brain_builder.runtime.env15_project_schema import initialize_env15_brain_root
from sqlite_brain_builder.runtime.env15_locked_read import (
    read_chatgpt_gemini_authority_member,
)
from sqlite_brain_builder.runtime.env15_topology import (
    REQUIRED_NODES,
    generate_env15_topologies,
    render_env15_topologies,
)


def test_env_uop_authority_artifacts_are_sqlite_derived_and_project_master_uses_live_databases(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    initialize_env15_brain_root(root)
    derived_paths = (
        root / "env" / "env_law.md",
        root / "env" / "env_mmd.mmd",
        root / "env" / "env_mmd.dot",
        root / "uop" / "uop_law.md",
        root / "uop" / "uop_mmd.mmd",
        root / "uop" / "uop_mmd.dot",
    )
    retired_project_template = root / "project" / "project_topology_template.mmd"
    assert not retired_project_template.exists()

    result = generate_env15_topologies(root)
    first_derived = {path: path.read_bytes() for path in derived_paths}
    first_pointers = {
        path: path.read_bytes()
        for path in (root / ".uepc_env", root / ".uepc_profile", root / ".uepc_project")
    }
    second = generate_env15_topologies(root)

    assert set(result) == {"ENV", "UOP", "PROJECT"}
    assert set(second) == set(result)
    for topology_id in ("ENV", "UOP"):
        artifact = result[topology_id]
        text = Path(artifact.mmd_path).read_text(encoding="utf-8")
        assert text.startswith("flowchart ")
        assert artifact.semantic_status == f"SQLITE_DERIVED_{topology_id}_AUTHORITY_PASS"
        assert artifact.render_status == "NOT_RENDERED"
        assert artifact.svg_path is None
        assert artifact.png_path is None
        assert artifact.edge_count > 0
        assert all(node in text for node in REQUIRED_NODES[topology_id])
        assert "PROJECT_MUTABLE_SECTOR" in text
        source_member = (
            "env/env_mmd.mmd" if topology_id == "ENV" else "uop/uop_mmd.mmd"
        )
        assert hashlib.sha256(
            read_chatgpt_gemini_authority_member(source_member)
        ).hexdigest().upper() in text
    artifact = result["PROJECT"]
    project = Path(artifact.mmd_path).read_text(encoding="utf-8")
    assert project.startswith("flowchart TD")
    assert artifact.semantic_status == "SQLITE_COMPLETE_BOUNDED_PROJECT_MASTER_PASS"
    assert artifact.render_status == "NOT_RENDERED"
    assert artifact.svg_path is None
    assert artifact.png_path is None
    assert artifact.edge_count > 0
    assert all(node in project for node in REQUIRED_NODES["PROJECT"])
    assert {path: path.read_bytes() for path in derived_paths} == first_derived
    assert {
        path: path.read_bytes()
        for path in (root / ".uepc_env", root / ".uepc_profile", root / ".uepc_project")
    } == first_pointers
    assert "exact supplied ENV SQLite bytes" in (root / "env" / "env_law.md").read_text(encoding="utf-8")
    assert "exact supplied UOP SQLite bytes" in (root / "uop" / "uop_law.md").read_text(encoding="utf-8")
    env_dot = (root / "env" / "env_mmd.dot").read_text(encoding="utf-8")
    uop_dot = (root / "uop" / "uop_mmd.dot").read_text(encoding="utf-8")
    assert env_dot.startswith("digraph ")
    assert uop_dot.startswith("digraph ")
    assert "Project mutable sector live router" in env_dot
    assert "project base open mode=ro&immutable=1" not in env_dot
    assert "EL_ENV_AUTHORITY" in env_dot
    assert "EL_UOP_AUTHORITY" in uop_dot
    assert not retired_project_template.exists()
    assert Path(artifact.mmd_path) == root / "project" / "topology" / "project_master_topology.mmd"
    assert "14 physical sectors / 18 logical intake lanes" in project
    assert project.count("tables=") >= 15
    assert (root / "receipts" / "ENV15_PROMPT_ROOTED_MMD_RECEIPT.json").is_file()
    generation = json.loads((root / "receipts" / "ENV15_PROMPT_ROOTED_MMD_RECEIPT.json").read_text(encoding="utf-8"))
    assert generation["generation_status"] == "ENV_UOP_SQLITE_DERIVATION_AND_PROJECT_MMD_COMPLETE_RENDER_NOT_STARTED"
    assert generation["mmd_derivation_law"] == "EXACT_SUPPLIED_BASE_PLUS_BOUNDED_PROJECT_MUTABLE_SECTOR_CORRECTION"
    assert generation["supplied_authority_provenance"]["status"] == "PASS"
    assert generation["relationships"]["PROJECT"]["registered_sectors"]
    assert (root / "receipts" / "ENV15_MMD_CORRECTION_CONTRACT.md").is_file()


def test_derived_env_uop_renders_and_generated_project_gets_hd_render(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    initialize_env15_brain_root(root)

    rendered = render_env15_topologies(root)

    assert set(rendered) == {"ENV", "UOP", "PROJECT"}
    for topology_id in ("ENV", "UOP"):
        artifact = rendered[topology_id]
        assert artifact.render_status == f"SQLITE_DERIVED_{topology_id}_MMD_TO_SVG_TO_PNG_PASS"
        assert Path(artifact.svg_path or "").stat().st_size > 0
        assert Path(artifact.png_path or "").stat().st_size > 0
        assert artifact.hd_png_path is None
        assert Path(artifact.png_path or "").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert artifact.svg_sha256 == hashlib.sha256(Path(artifact.svg_path or "").read_bytes()).hexdigest()
        assert artifact.png_sha256 == hashlib.sha256(Path(artifact.png_path or "").read_bytes()).hexdigest()
    artifact = rendered["PROJECT"]
    assert artifact.render_status == "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS"
    assert Path(artifact.svg_path or "").stat().st_size > 0
    assert Path(artifact.png_path or "").stat().st_size > 0
    assert Path(artifact.hd_png_path or "").stat().st_size > 0
    assert Path(artifact.png_path or "").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert Path(artifact.hd_png_path or "").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert artifact.svg_sha256 == hashlib.sha256(Path(artifact.svg_path or "").read_bytes()).hexdigest()
    assert artifact.png_sha256 == hashlib.sha256(Path(artifact.png_path or "").read_bytes()).hexdigest()
    assert artifact.hd_png_sha256 == hashlib.sha256(Path(artifact.hd_png_path or "").read_bytes()).hexdigest()
    manifest = json.loads((root / "receipts" / "ENV15_TOPOLOGY_RENDER_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "PASS"
    assert set(manifest["topologies"]) == {"ENV", "UOP", "PROJECT"}
