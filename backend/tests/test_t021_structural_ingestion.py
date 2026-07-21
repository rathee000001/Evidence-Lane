from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.structural_ingestion import (
    SUPPORTED_STRUCTURAL_LANES,
    ReadOnlyQueryError,
    UnsafeArchiveError,
    UnsafeSourceError,
    UnsupportedStructuralLaneError,
    ingest_brain_loader,
    ingest_project_engulf,
    ingest_research,
    ingest_sqlite_brain,
    ingest_structural_lane,
    inspect_sqlite_database,
    query_sqlite_read_only,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_sqlite_fixture(path: Path, *, foreign_key_violation: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executescript(
        """
        CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE child(
          id INTEGER PRIMARY KEY,
          parent_id INTEGER NOT NULL REFERENCES parent(id),
          value TEXT NOT NULL
        );
        CREATE INDEX idx_child_parent ON child(parent_id);
        CREATE VIEW child_view AS SELECT id,parent_id,value FROM child;
        CREATE TABLE audit(message TEXT NOT NULL);
        CREATE TRIGGER child_ai AFTER INSERT ON child BEGIN
          INSERT INTO audit(message) VALUES('child inserted');
        END;
        CREATE VIRTUAL TABLE notes_fts USING fts5(body);
        """
    )
    connection.execute("INSERT INTO parent(id,name) VALUES(1,'root')")
    connection.execute(
        "INSERT INTO child(id,parent_id,value) VALUES(1,?,?)",
        (999 if foreign_key_violation else 1, "fixture"),
    )
    connection.execute("INSERT INTO notes_fts(body) VALUES('structural sqlite evidence')")
    connection.commit()
    connection.close()


def assert_zero_growth(receipt: object) -> None:
    growth = getattr(receipt, "growth")
    assert growth
    assert all(value == 0 for value in growth.values()), growth


def sqlite_health(path: Path) -> tuple[list[str], list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return (
            [row[0] for row in connection.execute("PRAGMA integrity_check")],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )
    finally:
        connection.close()


def test_supported_lane_set_is_exact_and_dispatcher_never_falls_back_to_custom(tmp_path: Path) -> None:
    assert SUPPORTED_STRUCTURAL_LANES == (
        "brain_loader",
        "research",
        "project_engulf",
        "sqlite_brain",
    )
    source = tmp_path / "source.txt"
    source.write_text("Evidence: dispatcher fixture", encoding="utf-8")

    custom_db = tmp_path / "custom.sqlite"
    with pytest.raises(UnsupportedStructuralLaneError):
        ingest_structural_lane("custom", source, custom_db)
    assert not custom_db.exists()

    unknown_db = tmp_path / "unknown.sqlite"
    with pytest.raises(UnsupportedStructuralLaneError):
        ingest_structural_lane("not_a_lane", source, unknown_db)
    assert not unknown_db.exists()


@pytest.mark.parametrize("unsafe_name", ["../escape.txt", "/absolute.txt", "C:\\escape.txt"])
def test_brain_loader_rejects_unsafe_zip_paths_before_creating_output(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(unsafe_name, "unsafe")
    before_hash = sha256_file(package)
    destination = tmp_path / "brain_loader.sqlite"

    with pytest.raises(UnsafeArchiveError):
        ingest_brain_loader(package, destination)

    assert not destination.exists()
    assert sha256_file(package) == before_hash
    assert not (tmp_path.parent / "escape.txt").exists()


def test_brain_loader_indexes_package_manifest_pointers_and_sqlite_once(tmp_path: Path) -> None:
    embedded = tmp_path / "embedded.sqlite"
    create_sqlite_fixture(embedded)
    package = tmp_path / "fixture_brain.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifests/PACKAGE_MANIFEST.json",
            json.dumps({"brain_name": "Fixture Brain", "schema_version": "v001"}),
        )
        archive.writestr(".uepc_project", "project/sectors/sqlite/sample.sqlite")
        archive.writestr("project/README.md", "fixture package")
        archive.writestr("project/sectors/sqlite/sample.sqlite", embedded.read_bytes())
    package_hash = sha256_file(package)
    destination = tmp_path / "brain_loader_sector.sqlite"

    first = ingest_brain_loader(package, destination)
    second = ingest_brain_loader(package, destination)

    assert first.status == "PASS"
    assert first.source_preserved is True
    assert first.source_sqlite_receipts[0]["integrity_check"] == ["ok"]
    assert first.source_sqlite_receipts[0]["foreign_key_violation_count"] == 0
    assert first.output_integrity_check == ("ok",)
    assert first.output_foreign_key_violations == ()
    assert_zero_growth(second)
    assert sha256_file(package) == package_hash

    connection = sqlite3.connect(destination)
    try:
        package_row = connection.execute(
            "SELECT package_class,manifest_count,pointer_count,sqlite_count,source_brain_identity,schema_version,import_decision "
            "FROM brain_loader_package"
        ).fetchone()
        assert package_row == (
            "SQLITE_COLLECTION",
            1,
            1,
            1,
            "Fixture Brain",
            "v001",
            "IMPORT_READ_ONLY",
        )
        assert connection.execute("SELECT count(*) FROM brain_loader_member").fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM brain_loader_database").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM brain_loader_schema_object").fetchone()[0] > 0
        assert connection.execute("SELECT count(*) FROM brain_loader_receipt").fetchone()[0] == 1
    finally:
        connection.close()
    assert sqlite_health(destination) == (["ok"], [])


def test_research_structural_entities_and_fts_are_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "research.md"
    source.write_text(
        """# Retrieval Study
Research Question: Does selective retrieval reduce token use?
Hypothesis: Selective retrieval uses fewer tokens.
Method: Compare identical tasks across two retrieval strategies.
Evidence: The selective run loaded three relevant chunks.
Finding: Selective retrieval reduced the loaded corpus.
Limitation: One synthetic fixture cannot establish generality.
Citation: https://example.test/retrieval-study
Open Question: Does the result hold across version drift?
""",
        encoding="utf-8",
    )
    source_hash = sha256_file(source)
    destination = tmp_path / "research_sector.sqlite"

    first = ingest_research(source, destination)
    second = ingest_research(source, destination)

    assert first.status == "PASS"
    assert_zero_growth(second)
    assert sha256_file(source) == source_hash
    connection = sqlite3.connect(destination)
    try:
        for table in (
            "research_question",
            "research_hypothesis",
            "research_method",
            "research_evidence",
            "research_finding",
            "research_limitation",
            "research_citation",
            "research_open_question",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM research_fts WHERE research_fts MATCH 'selective'"
        ).fetchone()[0] >= 2
        assert connection.execute("SELECT count(*) FROM research_receipt").fetchone()[0] == 1
    finally:
        connection.close()
    assert sqlite_health(destination) == (["ok"], [])


def test_project_engulf_inventory_blocks_env_uop_promotion_and_preserves_project(tmp_path: Path) -> None:
    project = tmp_path / "user_project"
    (project / "env").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "env" / "law.md").write_text("must remain source-only", encoding="utf-8")
    (project / "src" / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    project_db = project / "project.sqlite"
    create_sqlite_fixture(project_db)
    before = {
        path.relative_to(project).as_posix(): sha256_file(path)
        for path in sorted(project.rglob("*"))
        if path.is_file()
    }
    destination = tmp_path / "engulf_sector.sqlite"

    first = ingest_project_engulf(project, destination)
    second = ingest_project_engulf(project, destination)

    assert first.status == "PASS"
    assert_zero_growth(second)
    after = {
        path.relative_to(project).as_posix(): sha256_file(path)
        for path in sorted(project.rglob("*"))
        if path.is_file()
    }
    assert after == before
    connection = sqlite3.connect(destination)
    try:
        assert connection.execute("SELECT count(*) FROM project_engulf_file").fetchone()[0] == 3
        conflict = connection.execute(
            "SELECT conflict_code,disposition FROM project_engulf_conflict"
        ).fetchone()
        assert conflict == ("ENV_UOP_PROMOTION_PROHIBITED", "BLOCK_AND_QUARANTINE_MAPPING")
        run = connection.execute(
            "SELECT accepted_count,skipped_count,blocked_count,status FROM project_engulf_run"
        ).fetchone()
        assert run == (2, 0, 1, "REVIEW_REQUIRED")
        assert connection.execute("SELECT count(*) FROM project_engulf_schema_mapping").fetchone()[0] > 0
        assert connection.execute(
            "SELECT update_status FROM project_engulf_topology_update"
        ).fetchone()[0] == "PENDING_GOVERNED_APPLY"
    finally:
        connection.close()
    assert sqlite_health(destination) == (["ok"], [])


def test_project_destination_inside_source_is_rejected_without_mutation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("user file", encoding="utf-8")
    destination = project / "sector.sqlite"
    with pytest.raises(UnsafeSourceError):
        ingest_project_engulf(project, destination)
    assert not destination.exists()


def test_sqlite_brain_captures_schema_counts_fts_and_fk_receipt_once(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    create_sqlite_fixture(source, foreign_key_violation=True)
    source_hash = sha256_file(source)
    standalone = inspect_sqlite_database(source)
    assert standalone.integrity_check == ("ok",)
    assert len(standalone.foreign_key_violations) == 1
    assert "notes_fts" in standalone.fts_tables
    assert {obj.object_type for obj in standalone.objects} >= {"table", "view", "index", "trigger"}

    destination = tmp_path / "sqlite_brain_sector.sqlite"
    first = ingest_sqlite_brain(source, destination)
    second = ingest_sqlite_brain(source, destination)

    assert first.status == "REVIEW_REQUIRED"
    assert first.source_sqlite_receipts[0]["integrity_check"] == ["ok"]
    assert first.source_sqlite_receipts[0]["foreign_key_violation_count"] == 1
    assert first.output_integrity_check == ("ok",)
    assert first.output_foreign_key_violations == ()
    assert_zero_growth(second)
    assert sha256_file(source) == source_hash

    connection = sqlite3.connect(destination)
    try:
        types = {
            row[0]
            for row in connection.execute("SELECT DISTINCT object_type FROM loaded_sqlite_brain_schema_object")
        }
        assert types >= {"table", "view", "index", "trigger"}
        assert connection.execute(
            "SELECT row_count FROM loaded_sqlite_brain_table_stat WHERE object_name='parent'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT fk_violation_count,status FROM loaded_sqlite_brain_integrity_result"
        ).fetchone() == (1, "REVIEW_REQUIRED")
        assert connection.execute(
            "SELECT table_name FROM loaded_sqlite_brain_fts_table WHERE table_name='notes_fts'"
        ).fetchone()[0] == "notes_fts"
        assert connection.execute("SELECT count(*) FROM loaded_sqlite_brain_receipt").fetchone()[0] == 1
    finally:
        connection.close()
    assert sqlite_health(destination) == (["ok"], [])


def test_sqlite_read_only_query_interface_is_bounded_and_rejects_writes(tmp_path: Path) -> None:
    source = tmp_path / "query.sqlite"
    create_sqlite_fixture(source)
    source_hash = sha256_file(source)

    result = query_sqlite_read_only(
        source,
        "SELECT id,name FROM parent ORDER BY id",
        row_limit=10,
    )
    assert result.columns == ("id", "name")
    assert result.rows == ((1, "root"),)
    assert result.truncated is False

    pragma = query_sqlite_read_only(source, "PRAGMA table_info(parent)")
    assert pragma.rows

    with pytest.raises(ReadOnlyQueryError):
        query_sqlite_read_only(source, "DELETE FROM parent")
    with pytest.raises(ReadOnlyQueryError):
        query_sqlite_read_only(source, "PRAGMA user_version=5")
    with pytest.raises(ReadOnlyQueryError):
        query_sqlite_read_only(source, "SELECT 1; SELECT 2")

    assert sha256_file(source) == source_hash
