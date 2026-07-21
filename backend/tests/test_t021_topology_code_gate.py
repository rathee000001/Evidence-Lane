from __future__ import annotations

from pathlib import Path

from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    build_brain,
    generate_project_mmd,
    render_topology,
)


def test_non_code_brain_skips_topology_generation_and_rendering(tmp_path: Path) -> None:
    source = tmp_path / "requirements.md"
    source.write_text("# Non-code project\nDocument-only evidence.\n", encoding="utf-8")

    result = build_brain(
        str(tmp_path),
        "Document Only",
        [{"source_id": "docs-1", "lane_key": "docs", "path": str(source), "active": True}],
        generate_mmd=True,
    )

    root = Path(result["brain_root"])
    assert result["incremental"]["code_lanes_loaded"] is False
    assert result["incremental"]["topology_status"] == "SKIPPED_NO_CODE_LANES"
    assert not (root / "project" / "topology" / "project_master_topology.mmd").exists()
    assert generate_project_mmd(str(tmp_path), "Document Only")["status"] == "SKIPPED_NO_CODE_LANES"
    assert render_topology(str(tmp_path), "Document Only")["status"] == "SKIPPED_NO_CODE_LANES"


def test_local_code_brain_enables_project_topology(tmp_path: Path) -> None:
    repository = tmp_path / "coded-project"
    repository.mkdir()
    (repository / "app.py").write_text("def main():\n    return 'coded'\n", encoding="utf-8")

    result = build_brain(
        str(tmp_path),
        "Coded Project",
        [{"source_id": "code-1", "lane_key": "local_code", "path": str(repository), "active": True}],
        generate_mmd=True,
    )

    root = Path(result["brain_root"])
    assert result["incremental"]["code_lanes_loaded"] is True
    assert result["incremental"]["topology_status"] == "GENERATED"
    assert (root / "project" / "topology" / "project_master_topology.mmd").is_file()
