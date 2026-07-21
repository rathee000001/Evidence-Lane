from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain, init_brain_layout


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sqlite_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE parent(id INTEGER PRIMARY KEY,name TEXT);"
            "CREATE TABLE child(id INTEGER PRIMARY KEY,parent_id INTEGER REFERENCES parent(id));"
            "CREATE VIEW parent_names AS SELECT name FROM parent;"
            "CREATE INDEX child_parent_idx ON child(parent_id);"
            "CREATE TRIGGER child_guard BEFORE INSERT ON child BEGIN SELECT 1; END;"
        )
        connection.execute("INSERT INTO parent VALUES(1,'root')")


def test_live_env15_governed_advanced_lanes_keep_structures_and_law_immutable(tmp_path: Path) -> None:
    root = init_brain_layout(str(tmp_path), "Advanced Lanes")
    env_hash = _hash(root / "env" / "env_sqlite.sqlite")
    uop_hash = _hash(root / "uop" / "uop_sqlite.sqlite")

    source_db = tmp_path / "source.sqlite"
    _sqlite_fixture(source_db)
    loader = tmp_path / "brain.zip"
    with zipfile.ZipFile(loader, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifests/PACKAGE_MANIFEST.json", json.dumps({"brain_name": "Imported Brain", "schema_version": "v001"}))
        archive.writestr(".uepc_project", "project/source.sqlite")
        archive.writestr("project/source.sqlite", source_db.read_bytes())

    research = tmp_path / "research.md"
    research.write_text(
        "Research Question: Is the lane governed?\nHypothesis: Yes.\nMethod: Inspect receipts.\n"
        "Evidence: Structural tables exist.\nFinding: Projection passed.\nLimitation: Fixture only.\n"
        "Citation: https://example.invalid/source\nOpen Question: What changes next?\n",
        encoding="utf-8",
    )
    project = tmp_path / "project_source"
    (project / "env").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "package.json").write_text('{"name":"governed-project","version":"1.2.3"}', encoding="utf-8")
    (project / "env" / "law.md").write_text("must never promote", encoding="utf-8")
    (project / "src" / "app.py").write_text("def run(): return 'ok'\n", encoding="utf-8")

    sources = [
        {"source_id": "loader", "lane_key": "brain_loader", "path": str(loader)},
        {"source_id": "research", "lane_key": "research", "path": str(research)},
        {"source_id": "engulf", "lane_key": "project_engulf", "path": str(project)},
        {"source_id": "sqlite", "lane_key": "sqlite_brain", "path": str(source_db)},
    ]
    first = build_brain(str(tmp_path), "Advanced Lanes", sources, generate_mmd=False)
    assert first["incremental"]["topology_status"] == "SKIPPED_NO_CODE_LANES"
    assert _hash(root / "env" / "env_sqlite.sqlite") == env_hash
    assert _hash(root / "uop" / "uop_sqlite.sqlite") == uop_hash

    checks = {
        "brain_loader": ("brain_loader_package", "SELECT package_class,source_brain_identity FROM brain_loader_package", ("SQLITE_COLLECTION", "Imported Brain")),
        "research": ("research_question", "SELECT COUNT(*) FROM research_fts WHERE research_fts MATCH 'governed'", (1,)),
        "project_engulf": ("project_engulf_package_identity", "SELECT package_name,package_version FROM project_engulf_package_identity", ("governed-project", "1.2.3")),
        "sqlite_brain": ("loaded_sqlite_brain_schema_object", "SELECT COUNT(*) FROM loaded_sqlite_brain_schema_object", (5,)),
    }
    before = {}
    for lane, (required_table, query, expected) in checks.items():
        _, database = resolve_env15_sector(root, lane)
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (required_table,)
            ).fetchone()
            assert connection.execute(query).fetchone() == expected
            before[lane] = _hash(database)

    _, custom = resolve_env15_sector(root, "custom")
    with sqlite3.connect(custom) as connection:
        assert not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE 'brain_loader_%'"
        ).fetchone()

    second = build_brain(str(tmp_path), "Advanced Lanes", sources, generate_mmd=False)
    assert second["incremental"]["sources_reused"] == 4
    for lane, digest in before.items():
        _, database = resolve_env15_sector(root, lane)
        assert _hash(database) == digest
