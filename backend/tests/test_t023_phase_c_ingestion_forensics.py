from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def _workspace(tmp_path: Path, brain_name: str) -> Path:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, brain_name)
    return workspace


def _database(brain_root: Path) -> Path:
    return (
        brain_root
        / "project"
        / "sectors"
        / "local_code"
        / "local_code_sector_v001.sqlite"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _logical_counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "source_registry",
                "source_file",
                "code_file_snapshot",
                "code_file_version",
                "source_byte_coverage",
                "artifact_registry",
                "code_exact_byte_chunk",
                "code_chunk",
                "chunk_index",
                "code_chunk_fts",
                "code_workflow_edge",
                "code_synthetic_snapshot_file",
                "git_commit_registry",
            )
        }
    finally:
        connection.close()


def test_full_text_artifact_binary_identity_chain_and_fts_reuse(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    (source_root / "artifacts").mkdir(parents=True)
    (source_root / "images").mkdir()
    (source_root / ".gitkeep").write_bytes(b"")
    (source_root / "main.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    (source_root / "README.md").write_text(
        "# Forensic fixture\n\nEvery eligible text file must be indexed.\n",
        encoding="utf-8",
    )
    long_artifact = (
        '{"records":["'
        + ("artifact-content-" * 6000)
        + 'omegaartifacttailtoken"],"status":"complete"}\n'
    )
    (source_root / "artifacts" / "large-report.json").write_text(
        long_artifact, encoding="utf-8"
    )
    (source_root / "artifacts" / "measurements.csv").write_text(
        "name,value\nalpha,1\nbeta,2\n", encoding="utf-8"
    )
    (source_root / "notes.txt").write_bytes("café exact bytes\n".encode("cp1252"))
    binary_bytes = b"\x89PNG\r\n\x1a\n\x00BINARY\xffPAYLOAD"
    (source_root / "images" / "logo.png").write_bytes(binary_bytes)

    workspace = _workspace(tmp_path, "Phase C Coverage")
    source = {
        "source_id": "phase_c_local_source",
        "lane_key": "local_code",
        "path": str(source_root),
        "active": True,
    }
    first = build_brain(
        str(workspace), "Phase C Coverage", [source], generate_mmd=False
    )
    brain_root = Path(first["brain_root"])
    database = _database(brain_root)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        file_rows = {
            row["relative_path"]: row
            for row in connection.execute(
                "SELECT file_id,relative_path,size_bytes,sha256,content_kind "
                "FROM code_file_snapshot WHERE source_id=? ORDER BY relative_path",
                (source["source_id"],),
            )
        }
        assert set(file_rows) == {
            ".gitkeep",
            "README.md",
            "artifacts/large-report.json",
            "artifacts/measurements.csv",
            "images/logo.png",
            "main.py",
            "notes.txt",
        }

        for relative_path, row in file_rows.items():
            physical = source_root / Path(relative_path)
            raw = physical.read_bytes()
            assert row["size_bytes"] == len(raw)
            assert row["sha256"] == _sha256(raw)
            coverage = connection.execute(
                "SELECT byte_count,sha256,coverage_status FROM source_byte_coverage "
                "WHERE source_id=? AND file_id=?",
                (source["source_id"], row["file_id"]),
            ).fetchone()
            assert coverage is not None
            assert coverage[0] == len(raw)
            assert coverage[1] == _sha256(raw)
            contains_edge = connection.execute(
                "SELECT COUNT(*) FROM code_workflow_edge WHERE source_id=? "
                "AND relation_type='CONTAINS_FILE' AND to_id=?",
                (source["source_id"], row["file_id"]),
            ).fetchone()[0]
            artifact_edge = connection.execute(
                "SELECT COUNT(*) FROM code_workflow_edge WHERE source_id=? "
                "AND from_type='file' AND from_id=? "
                "AND relation_type='PROJECTS_ARTIFACT'",
                (source["source_id"], row["file_id"]),
            ).fetchone()[0]
            assert contains_edge == 1
            assert artifact_edge == 1

            chunks = connection.execute(
                "SELECT chunk_ordinal,compression,compressed_payload,raw_chunk_sha256 "
                "FROM code_exact_byte_chunk WHERE source_id=? AND file_id=? "
                "ORDER BY chunk_ordinal",
                (source["source_id"], row["file_id"]),
            ).fetchall()
            text_chunk_count = connection.execute(
                "SELECT COUNT(*) FROM code_chunk WHERE file_id=?",
                (row["file_id"],),
            ).fetchone()[0]
            if not raw:
                assert relative_path == ".gitkeep"
                assert chunks == []
                assert text_chunk_count == 0
                assert row["sha256"] == _sha256(b"")
            elif row["content_kind"] == "BINARY_METADATA_ONLY":
                assert relative_path == "images/logo.png"
                assert chunks == []
                assert text_chunk_count == 0
                artifact = connection.execute(
                    "SELECT artifact_type,review_state FROM artifact_registry "
                    "WHERE source_id=? AND canonical_path=?",
                    (source["source_id"], relative_path),
                ).fetchone()
                assert tuple(artifact) == (
                    "code_binary_metadata",
                    "HASH_LINKED_METADATA_ONLY",
                )
            else:
                assert chunks
                rebuilt = b"".join(
                    zlib.decompress(chunk["compressed_payload"]) for chunk in chunks
                )
                assert rebuilt == raw
                assert all(
                    chunk["compression"] == "zlib"
                    and chunk["raw_chunk_sha256"]
                    == _sha256(zlib.decompress(chunk["compressed_payload"]))
                    for chunk in chunks
                )
                assert text_chunk_count > 0

        assert connection.execute(
            "SELECT COUNT(*) FROM code_chunk c "
            "LEFT JOIN code_file_version v ON v.file_version_id=c.file_version_id "
            "WHERE v.file_version_id IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM code_chunk c "
            "LEFT JOIN chunk_index i ON i.chunk_id=c.chunk_id "
            "WHERE i.chunk_id IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM code_chunk c "
            "LEFT JOIN code_chunk_fts f ON f.chunk_id=c.chunk_id "
            "WHERE f.chunk_id IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM code_chunk").fetchone()[0] == (
            connection.execute("SELECT COUNT(*) FROM code_chunk_fts").fetchone()[0]
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM code_chunk_fts "
            "WHERE code_chunk_fts MATCH 'omegaartifacttailtoken'"
        ).fetchone()[0] >= 1
        unchanged_artifact_file_id = file_rows["artifacts/large-report.json"]["file_id"]
        unchanged_artifact_chunk_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT chunk_id FROM code_chunk WHERE file_id=? ORDER BY chunk_id",
                (unchanged_artifact_file_id,),
            )
        )
        assert unchanged_artifact_chunk_ids
    finally:
        connection.close()

    first_bytes = database.read_bytes()
    first_counts = _logical_counts(database)
    second = build_brain(
        str(workspace), "Phase C Coverage", [source], generate_mmd=False
    )
    assert second["incremental"]["all_sources_unchanged"] is True
    assert database.read_bytes() == first_bytes
    assert _logical_counts(database) == first_counts

    (source_root / "main.py").write_text(
        "def answer():\n    return 43\n", encoding="utf-8"
    )
    changed = build_brain(
        str(workspace), "Phase C Coverage", [source], generate_mmd=False
    )
    assert changed["incremental"]["all_sources_unchanged"] is False
    connection = sqlite3.connect(database)
    try:
        artifact_after = connection.execute(
            "SELECT file_id FROM code_file_snapshot WHERE source_id=? AND relative_path=?",
            (source["source_id"], "artifacts/large-report.json"),
        ).fetchone()[0]
        artifact_chunks_after = tuple(
            row[0]
            for row in connection.execute(
                "SELECT chunk_id FROM code_chunk WHERE file_id=? ORDER BY chunk_id",
                (artifact_after,),
            )
        )
        assert artifact_after == unchanged_artifact_file_id
        assert artifact_chunks_after == unchanged_artifact_chunk_ids
        assert connection.execute(
            "SELECT COUNT(*) FROM code_chunk c "
            "LEFT JOIN code_chunk_fts f ON f.chunk_id=c.chunk_id "
            "WHERE f.chunk_id IS NULL"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM code_chunk").fetchone()[0] == (
            connection.execute("SELECT COUNT(*) FROM code_chunk_fts").fetchone()[0]
        )
    finally:
        connection.close()

    changed_bytes = database.read_bytes()
    changed_counts = _logical_counts(database)
    replay = build_brain(
        str(workspace), "Phase C Coverage", [source], generate_mmd=False
    )
    assert replay["incremental"]["all_sources_unchanged"] is True
    assert database.read_bytes() == changed_bytes
    assert _logical_counts(database) == changed_counts


def test_omitted_lane_is_skipped_without_erasing_last_populated_state(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "main.py").write_text("print('preserve me')\n", encoding="utf-8")
    workspace = _workspace(tmp_path, "No Omitted Lane Erasure")
    source = {
        "source_id": "preserved_local_source",
        "lane_key": "local_code",
        "path": str(source_root),
        "active": True,
    }
    populated = build_brain(
        str(workspace), "No Omitted Lane Erasure", [source], generate_mmd=False
    )
    brain_root = Path(populated["brain_root"])
    database = _database(brain_root)
    populated_bytes = database.read_bytes()
    populated_counts = _logical_counts(database)

    omitted = build_brain(
        str(workspace), "No Omitted Lane Erasure", [], generate_mmd=False
    )
    assert omitted["incremental"]["selection_changed"] is True
    assert database.read_bytes() == populated_bytes
    assert _logical_counts(database) == populated_counts
    local_skip = next(
        item
        for item in omitted["incremental"]["unloaded_lane_skips"]
        if item["lane_id"] == "local_code"
    )
    assert local_skip["status"] == "SKIPPED_NO_SOURCE"
    assert local_skip["preservation_state"] == "PRESERVED_PRIOR_DATA"
    assert local_skip["ingestion_lane_fired"] is False
    assert local_skip["mutation_receipt_created"] is False

    pointer = json.loads(
        (brain_root / "project" / "pointers" / "local_code_pointer.json").read_text(
            encoding="utf-8"
        )
    )
    assert pointer["lane_classification"] == "SKIPPED"
    assert pointer["classification_evidence"]["status"] == "SKIPPED_NO_SOURCE"
    assert pointer["registered_source_count"] == 1
    assert pointer["preservation_state"] == "PRESERVED_PRIOR_DATA"
    assert pointer["database_sha256"] == hashlib.sha256(populated_bytes).hexdigest()
