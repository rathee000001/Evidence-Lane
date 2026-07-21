from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.canonical_lanes import CANONICAL_LANE_IDS
from sqlite_brain_builder.runtime.env15_project_schema import ENV15_LANE_TO_SECTOR, resolve_env15_sector
from sqlite_brain_builder.runtime.env15_ingestion import set_env15_source_activity
from sqlite_brain_builder.runtime.canonical_lanes import get_lane
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain


def _sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO evidence(value) VALUES('fixture')")
    connection.commit()
    connection.close()


def _fixtures(tmp_path: Path) -> dict[str, Path]:
    docx = pytest.importorskip("docx")
    openpyxl = pytest.importorskip("openpyxl")
    pptx = pytest.importorskip("pptx")
    image_module = pytest.importorskip("PIL.Image")

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    code = fixtures / "code"
    code.mkdir()
    (code / "app.py").write_text("def health():\n    return 'ok'\n", encoding="utf-8")
    if shutil.which("git"):
        subprocess.run(["git", "init"], cwd=code, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=code, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=code, check=True)
        subprocess.run(["git", "add", "app.py"], cwd=code, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=code, check=True, capture_output=True)

    text_files = {}
    for lane in ("chat_lineage", "discussion", "analysis", "plan", "mode", "research", "custom"):
        path = fixtures / f"{lane}.md"
        path.write_text(f"# {lane}\nGoverned {lane} fixture.\n", encoding="utf-8")
        text_files[lane] = path

    docs = fixtures / "fixture.docx"
    document = docx.Document()
    document.add_heading("Evidence", level=1)
    document.add_paragraph("Governed DOCX fixture.")
    document.save(docs)

    csv = fixtures / "fixture.csv"
    csv.write_text("claim,status\ngoverned,pass\n", encoding="utf-8")
    workbook_path = fixtures / "fixture.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["a", "b", "total"])
    workbook.active.append([1, 2, "=A2+B2"])
    workbook.save(workbook_path)
    workbook.close()

    presentation_path = fixtures / "fixture.pptx"
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Governed PPT fixture"
    presentation.save(presentation_path)

    image = fixtures / "fixture.png"
    image_module.new("RGB", (32, 32), "white").save(image)
    pdf = fixtures / "fixture.pdf"
    image_module.new("RGB", (32, 32), "white").save(pdf, "PDF")
    artifact = fixtures / "fixture.bin"
    artifact.write_bytes(b"\x00governed-artifact\xff")

    sqlite_source = fixtures / "fixture.sqlite"
    _sqlite(sqlite_source)
    brain_package = fixtures / "brain.zip"
    with zipfile.ZipFile(brain_package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("PACKAGE_MANIFEST.json", json.dumps({"brain_name": "Fixture Brain"}))
        archive.write(sqlite_source, "project/sectors/sqlite_brain/fixture.sqlite")
    engulf = fixtures / "engulf_project"
    engulf.mkdir()
    (engulf / "README.md").write_text("governed project engulf fixture", encoding="utf-8")

    return {
        "github_code": code,
        "local_code": code,
        **text_files,
        "docs": docs,
        "data_excel": csv,
        "ppt": presentation_path,
        "pdf_ocr": pdf,
        "images_ocr": image,
        "artifacts": artifact,
        "brain_loader": brain_package,
        "project_engulf": engulf,
        "sqlite_brain": sqlite_source,
    }


def _router_counts(root: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(root / "project" / "project_router.sqlite")
    try:
        return (
            connection.execute("SELECT COUNT(*) FROM sector_mutation_grant").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM sector_mutation_receipt").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM brain_snapshot_registry").fetchone()[0],
        )
    finally:
        connection.close()


def _lane_fts_count(root: Path, lane: str) -> int:
    _, database = resolve_env15_sector(root, lane)
    table = get_lane(lane).fts_table
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    finally:
        connection.close()


@pytest.mark.parametrize("primary_lane", ["local_code", "github_code"])
def test_all_permitted_lanes_ingest_and_identical_second_pass_has_zero_growth(
    tmp_path: Path,
    primary_lane: str,
) -> None:
    fixtures = _fixtures(tmp_path)
    assert set(fixtures) == set(CANONICAL_LANE_IDS)
    excluded_primary_lane = "github_code" if primary_lane == "local_code" else "local_code"
    active_lane_ids = tuple(lane for lane in CANONICAL_LANE_IDS if lane != excluded_primary_lane)
    sources = [
        {
            "source_id": f"source_{lane}",
            "lane_key": lane,
            "display_name": path.name,
            "path": str(path),
            "assistant_response": "visible fixture response" if lane == "chat_lineage" else "",
            "schema_contract": "custom_item\ncustom_evidence\ncustom_decision\ncustom_next_action" if lane == "custom" else "",
            "active": True,
        }
        for lane, path in fixtures.items()
        if lane in active_lane_ids
    ]

    brain_name = f"All Permitted Lanes {primary_lane}"
    first = build_brain(str(tmp_path), brain_name, sources, generate_mmd=False)
    root = Path(first["brain_root"])
    counts_after_first = _router_counts(root)
    sector_rows_after_first: dict[str, tuple[int, ...]] = {}
    for sector_id in sorted(set(ENV15_LANE_TO_SECTOR.values())):
        _, database = resolve_env15_sector(root, sector_id)
        connection = sqlite3.connect(database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            tracked = [table for table in ("source_registry", "artifact_registry", "chunk_index", "mutation_receipt", "code_source_registry", "turn_prepare") if table in tables]
            sector_rows_after_first[sector_id] = tuple(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked
            )
        finally:
            connection.close()

    build_brain(str(tmp_path), brain_name, sources, generate_mmd=False)
    assert _router_counts(root) == counts_after_first
    # Four advanced lanes perform one universal intake mutation plus one
    # governed specialized-table projection mutation. The Plan lane also
    # performs two governed writes: canonical Delta 0 registration and the
    # distinct Prompt-2 projection. Identical replay adds no grants, receipts,
    # or snapshots.
    assert all(value > 0 for value in counts_after_first)

    for lane in active_lane_ids:
        sector_id, database = resolve_env15_sector(root, lane)
        assert sector_id == ENV15_LANE_TO_SECTOR[lane]
        connection = sqlite3.connect(database)
        try:
            if lane == "chat_lineage":
                assert connection.execute(
                    "SELECT COUNT(*) FROM turn_prepare WHERE idempotency_key LIKE 'source:%'"
                ).fetchone()[0] == 1
            elif lane in {"github_code", "local_code"}:
                assert connection.execute(
                    "SELECT COUNT(*) FROM code_source_registry WHERE source_id=?", (f"source_{lane}",)
                ).fetchone()[0] == 1
            elif lane == "research":
                # Research is content-addressed and append-only. Its generic
                # source registry key is the immutable source_version_id, while
                # the prepare/commit pair is the canonical one-append proof.
                assert connection.execute(
                    "SELECT COUNT(*) FROM research_append_prepare"
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT COUNT(*) FROM research_append_commit"
                ).fetchone()[0] == 1
            else:
                assert connection.execute(
                    "SELECT COUNT(*) FROM source_registry WHERE source_id=?", (f"source_{lane}",)
                ).fetchone()[0] == 1
        finally:
            connection.close()

    _, custom_database = resolve_env15_sector(root, "custom")
    custom_connection = sqlite3.connect(custom_database)
    try:
        custom_projection_counts = {
            table: custom_connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("custom_item", "custom_evidence", "custom_decision", "custom_next_action")
        }
        assert all(count > 0 for count in custom_projection_counts.values())
    finally:
        custom_connection.close()

    for lane, document_table in {
        "brain_loader": "brain_loader_fts_document",
        "research": "research_fts_document",
        "project_engulf": "project_engulf_fts_document",
        "sqlite_brain": "loaded_sqlite_brain_fts_document",
    }.items():
        _, structural_database = resolve_env15_sector(root, lane)
        structural_connection = sqlite3.connect(structural_database)
        try:
            assert structural_connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name=?",
                (document_table,),
            ).fetchone()[0] == 1
            assert structural_connection.execute(
                f'SELECT COUNT(*) FROM "{document_table}"'
            ).fetchone()[0] > 0
        finally:
            structural_connection.close()

    for sector_id, before in sector_rows_after_first.items():
        _, database = resolve_env15_sector(root, sector_id)
        connection = sqlite3.connect(database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            tracked = [table for table in ("source_registry", "artifact_registry", "chunk_index", "mutation_receipt", "code_source_registry", "turn_prepare") if table in tables]
            after = tuple(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked)
        finally:
            connection.close()
        assert after == before

    fts_after_identical = {lane: _lane_fts_count(root, lane) for lane in active_lane_ids}
    assert all(count > 0 for count in fts_after_identical.values())

    mutable_activity_lanes = tuple(
        lane for lane in active_lane_ids if lane not in {"chat_lineage", "research"}
    )
    drop_results = {
        lane: set_env15_source_activity(root, lane, f"source_{lane}", active=False)
        for lane in mutable_activity_lanes
    }
    assert all(result["status"] in {"PASS", "APPENDED"} for result in drop_results.values())
    # Chat Lineage and Research are immutable append-only histories. Every
    # mutable lane removes the inactive source from searchable FTS.
    for lane in mutable_activity_lanes:
        assert _lane_fts_count(root, lane) == 0

    build_brain(str(tmp_path), brain_name, sources, generate_mmd=False)
    fts_after_readd = {lane: _lane_fts_count(root, lane) for lane in active_lane_ids}
    for lane in mutable_activity_lanes:
        assert fts_after_readd[lane] == fts_after_identical[lane]
    counts_after_readd = _router_counts(root)

    # A further identical pass must be a complete no-growth replay, including
    # mutation/snapshot receipts and all lane-specific FTS projections.
    build_brain(str(tmp_path), brain_name, sources, generate_mmd=False)
    assert _router_counts(root) == counts_after_readd
    assert {lane: _lane_fts_count(root, lane) for lane in active_lane_ids} == fts_after_readd
