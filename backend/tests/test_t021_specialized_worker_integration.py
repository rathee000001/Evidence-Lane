from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.stable_runtime_v53 import build_semantic_lane


def _sqlite_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO evidence(value) VALUES('truth')")
    connection.commit()
    connection.close()


@pytest.mark.parametrize(
    ("lane_id", "expected_table"),
    [
        ("brain_loader", "brain_loader_package"),
        ("project_engulf", "project_engulf_source"),
        ("sqlite_brain", "loaded_sqlite_brain_database"),
    ],
)
def test_build_semantic_lane_dispatches_to_structural_ingesters(
    tmp_path: Path, lane_id: str, expected_table: str
) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    if lane_id == "brain_loader":
        embedded = source_root / "embedded.sqlite"
        _sqlite_fixture(embedded)
        source = source_root / "brain.zip"
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("PACKAGE_MANIFEST.json", json.dumps({"brain_name": "Fixture"}))
            archive.write(embedded, "project/sectors/sqlite/embedded.sqlite")
    elif lane_id == "research":
        source = source_root / "research.md"
        source.write_text("# Question\nWhat is true?\n# Evidence\nFixture evidence.\n", encoding="utf-8")
    elif lane_id == "project_engulf":
        source = source_root / "project"
        source.mkdir()
        (source / "README.md").write_text("governed project input", encoding="utf-8")
    else:
        source = source_root / "source.sqlite"
        _sqlite_fixture(source)

    destination = tmp_path / "sectors" / lane_id / f"{lane_id}_sector_v001.sqlite"
    receipt = build_semantic_lane(
        destination,
        lane_id,
        {"path": str(source), "display_name": source.name},
        None,
        0,
    )

    assert receipt.status == "PASS"
    connection = sqlite3.connect(destination)
    try:
        assert connection.execute(f'SELECT COUNT(*) FROM "{expected_table}"').fetchone()[0] >= 1
    finally:
        connection.close()


def test_research_lane_requires_its_append_only_service(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="RESEARCH_REQUIRES_APPEND_ONLY_SERVICE"):
        build_semantic_lane(tmp_path / "research.sqlite", "research", {"text": "inline"}, None, 0)
