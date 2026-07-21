from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.env15_project_schema import ENV15_PHYSICAL_SECTORS
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain, init_brain_layout


def test_runtime_layout_installs_env15_fourteen_sector_governance_foundation(tmp_path: Path) -> None:
    root = init_brain_layout(str(tmp_path), "Canonical Runtime")
    sectors = root / "project" / "sectors"

    assert tuple(sorted(path.name for path in sectors.iterdir() if path.is_dir())) == ENV15_PHYSICAL_SECTORS
    for lane_id in ENV15_PHYSICAL_SECTORS:
        assert (sectors / lane_id / f"{lane_id}_sector_v001.sqlite").is_file()

    with sqlite3.connect(root / "project" / "project_router.sqlite") as connection:
        rows = connection.execute(
            "SELECT sector_id, sqlite_path FROM sector_registry ORDER BY sector_id"
        ).fetchall()
    assert [row[0] for row in rows] == list(ENV15_PHYSICAL_SECTORS)
    assert all(f"/{row[0]}/{row[0]}_sector_v001.sqlite" in row[1] for row in rows)


def test_legacy_alias_is_resolved_before_any_sector_write(tmp_path: Path) -> None:
    csv_path = tmp_path / "evidence.csv"
    csv_path.write_text("claim,status\ncanonical,pass\n", encoding="utf-8")

    result = build_brain(
        str(tmp_path),
        "Alias Routing",
        [
            {
                "source_id": "source-fixed-csv",
                "lane_key": "data_excel_csv",
                "display_name": csv_path.name,
                "path": str(csv_path),
                "source_type": "spreadsheet",
                "active": True,
            }
        ],
    )
    root = Path(result["brain_root"])
    canonical = root / "project" / "sectors" / "data_excel" / "data_excel_sector_v001.sqlite"

    assert canonical.is_file()
    assert not (root / "project" / "sectors" / "data_excel_csv").exists()
    with sqlite3.connect(canonical) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0] == 1
        # Parser status, delimited header, and the deterministic row chunk are
        # separate records; CSV must never be registered as a workbook.
        assert connection.execute("SELECT COUNT(*) FROM chunk_index").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM chunk_index WHERE chunk_type='data_excel:workbook_metadata'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT source_type FROM source_registry").fetchone()[0] == "data_excel"


def test_unknown_lane_fails_closed_instead_of_writing_custom(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("must not be silently rerouted", encoding="utf-8")

    with pytest.raises(RuntimeError, match="UNKNOWN_LANE_ALIAS:not_a_real_lane"):
        build_brain(
            str(tmp_path),
            "Unknown Lane",
            [
                {
                    "source_id": "source-unknown",
                    "lane_key": "not_a_real_lane",
                    "display_name": source.name,
                    "path": str(source),
                    "active": True,
                }
            ],
        )
