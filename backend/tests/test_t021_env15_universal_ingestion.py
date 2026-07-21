from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime import env15_ingestion
from sqlite_brain_builder.runtime.env15_ingestion import (
    ingest_env15_universal_source,
    set_env15_source_activity,
)
from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.env15_resource import install_env15_resource


def test_universal_ingestion_uses_grant_and_is_index_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "brain"
    install_env15_resource(root)
    source = tmp_path / "docs.md"
    source.write_text("# Question\nWhat changed?\n\n# Evidence\nA governed fixture.\n", encoding="utf-8")
    payload = {"source_id": "source_docs", "path": str(source)}

    first = ingest_env15_universal_source(root, "docs", payload)
    monkeypatch.setattr(
        env15_ingestion,
        "inspect_source_structure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unchanged source bytes were re-hashed")),
    )
    second = ingest_env15_universal_source(root, "docs", payload)

    assert first.status == "PASS"
    assert first.artifact_rows_added == 1
    assert first.chunk_rows_added >= 1
    assert second.status == "SKIPPED_UNCHANGED"
    assert second.mutation_receipt is None
    _, database = resolve_env15_sector(root, "docs")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM chunk_index").fetchone()[0] == first.chunk_rows_added
        assert connection.execute("SELECT COUNT(*) FROM doc_fts").fetchone()[0] == first.chunk_rows_added
        assert connection.execute("SELECT COUNT(*) FROM mutation_receipt").fetchone()[0] == 1
    finally:
        connection.close()


def test_universal_ingestion_reindexes_unchanged_bytes_when_parser_contract_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "brain"
    install_env15_resource(root)
    source = tmp_path / "artifact.json"
    source.write_text('{"source_id":"PARSER-REINDEX-001","gate":"PASS"}', encoding="utf-8")
    payload = {"source_id": "source_artifact", "path": str(source)}

    first = ingest_env15_universal_source(root, "artifacts", payload)
    _, database = resolve_env15_sector(root, "artifacts")
    connection = sqlite3.connect(database)
    try:
        receipts_before = connection.execute("SELECT COUNT(*) FROM mutation_receipt").fetchone()[0]
    finally:
        connection.close()

    original_get_lane = env15_ingestion.get_lane
    current_lane = original_get_lane("artifacts")
    monkeypatch.setattr(
        env15_ingestion,
        "get_lane",
        lambda lane_id: replace(current_lane, parser_id="artifact_structure_future")
        if lane_id == "artifacts"
        else original_get_lane(lane_id),
    )

    second = ingest_env15_universal_source(root, "artifacts", payload)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM artifact_registry").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM mutation_receipt").fetchone()[0] == receipts_before + 1
    finally:
        connection.close()
    assert first.status == "PASS"
    assert second.status == "PASS"


def test_advanced_lane_retains_identity_inside_custom_universal_sector(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    install_env15_resource(root)
    source = tmp_path / "analysis.md"
    source.write_text("Claim: fixture\nEvidence: governed\n", encoding="utf-8")

    result = ingest_env15_universal_source(
        root, "analysis", {"source_id": "source_analysis", "path": str(source)}
    )

    assert result.sector_id == "custom"
    _, database = resolve_env15_sector(root, "analysis")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT source_type FROM source_registry WHERE source_id='source_analysis'"
        ).fetchone()[0] == "analysis"
        assert connection.execute(
            "SELECT chunk_type FROM chunk_index WHERE source_id='source_analysis'"
        ).fetchone()[0].startswith("analysis:")
    finally:
        connection.close()


def test_drop_marks_stable_source_inactive_and_readd_reuses_rows_without_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    install_env15_resource(root)
    source = tmp_path / "docs.md"
    source.write_text("governed drop and readd", encoding="utf-8")
    payload = {"source_id": "source_docs", "path": str(source)}
    first = ingest_env15_universal_source(root, "docs", payload)
    _, database = resolve_env15_sector(root, "docs")

    dropped = set_env15_source_activity(root, "docs", "source_docs", active=False)
    connection = sqlite3.connect(database)
    try:
        counts_dropped = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("source_registry", "artifact_registry", "chunk_index")
        )
        assert connection.execute(
            "SELECT availability_state FROM source_registry WHERE source_id='source_docs'"
        ).fetchone()[0] == "INACTIVE"
    finally:
        connection.close()
    readded = ingest_env15_universal_source(root, "docs", payload)
    connection = sqlite3.connect(database)
    try:
        counts_readded = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("source_registry", "artifact_registry", "chunk_index")
        )
        assert connection.execute(
            "SELECT availability_state FROM source_registry WHERE source_id='source_docs'"
        ).fetchone()[0] == "AVAILABLE"
    finally:
        connection.close()
    assert first.status == "PASS"
    assert dropped["status"] == "PASS"
    assert readded.status == "PASS"
    assert counts_readded == counts_dropped


def test_inline_custom_text_completes_index_drop_and_idempotent_readd_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    install_env15_resource(root)
    payload = {
        "source_id": "source_inline_custom",
        "display_name": "Inline acceptance fixture",
        "text": "Decision: retain canonical identity\nNext: verify stable FTS\n",
        "schema_contract": "decision\nnext_action\nevidence",
    }
    first = ingest_env15_universal_source(root, "Custom Source", payload)
    second = ingest_env15_universal_source(root, "custom", payload)
    dropped = set_env15_source_activity(root, "custom", payload["source_id"], active=False)
    _, database = resolve_env15_sector(root, "custom")
    dropped_connection = sqlite3.connect(database)
    try:
        assert dropped_connection.execute("SELECT COUNT(*) FROM custom_fts").fetchone()[0] == 0
    finally:
        dropped_connection.close()
    readded = ingest_env15_universal_source(root, "custom", payload)
    connection = sqlite3.connect(database)
    try:
        source = connection.execute(
            "SELECT source_type,canonical_path,availability_state FROM source_registry WHERE source_id=?",
            (payload["source_id"],),
        ).fetchone()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("source_registry", "artifact_registry", "chunk_index")
        }
        content = connection.execute(
            "SELECT content FROM chunk_index WHERE source_id=?", (payload["source_id"],)
        ).fetchone()[0]
    finally:
        connection.close()
    assert first.status == "PASS"
    assert second.status == "SKIPPED_UNCHANGED"
    assert dropped["status"] == "PASS"
    assert readded.status == "PASS"
    raw_text_hash = __import__("hashlib").sha256(payload["text"].encode("utf-8")).hexdigest()
    assert source == ("custom", f"inline:{raw_text_hash}", "AVAILABLE")
    assert counts == {"source_registry": 1, "artifact_registry": 1, "chunk_index": 2}
    final_connection = sqlite3.connect(database)
    try:
        assert final_connection.execute("SELECT COUNT(*) FROM custom_fts").fetchone()[0] == 2
    finally:
        final_connection.close()
    assert "canonical identity" in content
