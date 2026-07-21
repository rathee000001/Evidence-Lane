from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlite_brain_builder.runtime.gemini_exact10 import export_gemini_exact10_direct
from sqlite_brain_builder.runtime.env15_resource import EXPECTED_SECTORS
from sqlite_brain_builder.runtime.package_validation import (
    T021_GEMINI_EXACT10_NAMES,
    validate_gemini_exact10,
)


def _create_project(brain_root: Path) -> None:
    project = brain_root / "project"
    topology = project / "topology"
    topology.mkdir(parents=True)
    with sqlite3.connect(project / "project_router.sqlite") as connection:
        connection.execute(
            "CREATE TABLE sector_registry(sector_id TEXT PRIMARY KEY,sqlite_path TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO sector_registry VALUES(?,?)",
            [
                (
                    sector,
                    f"project/sectors/{sector}/{sector}_sector_v001.sqlite",
                )
                for sector in sorted(EXPECTED_SECTORS)
            ],
        )
    with sqlite3.connect(project / "project_template.sqlite") as connection:
        connection.execute("CREATE TABLE project_meta(id TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO project_meta VALUES('project','fixture')")
    for sector_id in sorted(EXPECTED_SECTORS):
        sector = project / "sectors" / sector_id
        sector.mkdir(parents=True)
        with sqlite3.connect(sector / f"{sector_id}_sector_v001.sqlite") as connection:
            connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY,value TEXT)")
            connection.execute(
                "INSERT INTO evidence VALUES(?,?)",
                (f"{sector_id}-1", "fixture"),
            )
    (project / "project_pointer.json").write_text('{"router":"project_router.sqlite"}\n', encoding="utf-8")
    (project / "sector_index.json").write_text('[{"lane_key":"custom"}]\n', encoding="utf-8")
    (topology / "project_master_topology.mmd").write_text("flowchart TD\n  A --> B\n", encoding="utf-8")
    (topology / "project_master_topology.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8")
    (topology / "project_master_topology.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")


def test_gemini_export_is_exactly_ten_direct_readable_files(tmp_path: Path) -> None:
    brain_root = tmp_path / "brain_output"
    _create_project(brain_root)

    result = export_gemini_exact10_direct(brain_root, "Fixture Brain", "# Gemini prompt")
    validation = validate_gemini_exact10(result["gemini_package_zip"])

    assert validation["status"] == "PASS"
    assert validation["root_entry_count"] == 10
    assert validation["nested_zip_count"] == 0
    assert validation["all_files_directly_readable"] is True
    assert set(validation["root_entries"]) == set(T021_GEMINI_EXACT10_NAMES)
    assert all(item["integrity_check"] == ["ok"] for item in validation["sqlite_results"])
    assert all(not item["foreign_key_violations"] for item in validation["sqlite_results"])
