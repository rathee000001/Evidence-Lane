from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.env15_project_schema import (
    Env15MutationError,
    governed_sector_mutation,
    initialize_env15_brain_root,
    resolve_env15_sector,
    verify_env15_live_project,
    write_env15_runtime_sector_state,
)
from sqlite_brain_builder.runtime.env15_resource import install_env15_resource


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_env15_live_project_contract_and_logical_lane_mapping(tmp_path: Path) -> None:
    root = tmp_path / "brain_output"
    install_env15_resource(root)
    report = verify_env15_live_project(root)
    assert report.passed
    assert report.sector_count == 14
    assert resolve_env15_sector(root, "analysis")[0] == "custom"
    assert resolve_env15_sector(root, "research")[0] == "research"


def test_live_brain_initializer_accepts_workspace_created_empty_output_and_writes_runtime_state(tmp_path: Path) -> None:
    root = tmp_path / "brain_output"
    root.mkdir()
    report = initialize_env15_brain_root(root)
    state = write_env15_runtime_sector_state(root)

    assert report.passed
    assert state["sector_count"] == 14
    assert state["logical_lane_count"] == 18
    assert state["logical_lane_to_sector"]["mode"] == "custom"
    assert Path(state["receipt_path"]).is_file()
    alignment = json.loads((root / "receipts" / "ENV15_PROJECT_DATABASE_ALIGNMENT.json").read_text(encoding="utf-8"))
    assert alignment["status"] == "PASS"
    assert alignment["router_registered_sector_count"] == 14
    assert sum(item["append_only_specialized_exception"] for item in alignment["sectors"]) == 2
    assert all(
        item["universal_schema_aligned"] or item["append_only_specialized_exception"]
        for item in alignment["sectors"]
    )
    assert alignment["authority_boundaries"] == {
        "env": "LOCKED_ROOT_AUTHORITY_NOT_PROJECT_MUTABLE",
        "project": "ONLY_REGISTERED_SECTOR_DATABASES_MUTABLE_BY_POLICY",
        "uop": "LOCKED_OPERATOR_AUTHORITY_NOT_PROJECT_MUTABLE",
    }


def test_named_sector_mutation_grant_dual_receipt_snapshot_and_relock(tmp_path: Path) -> None:
    root = tmp_path / "brain_output"
    install_env15_resource(root)
    sector_id, sector_path = resolve_env15_sector(root, "docs")
    before = _sha256(sector_path)

    def mutate(connection: sqlite3.Connection) -> dict[str, int]:
        connection.execute(
            "INSERT INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
            ("source_fixture", "research_document", "fixture.md", "fixture.md", "a" * 64, 7, "AVAILABLE", "turn_001"),
        )
        connection.execute(
            "INSERT INTO chunk_index VALUES(?,?,?,?,?,?,?,?)",
            ("chunk_fixture", "source_fixture", None, "research", 1, "fixture", "b" * 64, 2),
        )
        return {"sources": 1, "chunks": 1}

    receipt = governed_sector_mutation(
        root, "docs", actor="Test Worker", reason="fixture ingest", turn_id="turn_001", mutate=mutate
    )
    assert receipt.status == "PASS"
    assert receipt.sector_id == sector_id
    assert receipt.previous_file_hash == before
    assert receipt.current_file_hash == _sha256(sector_path)
    assert receipt.current_file_hash != before
    assert receipt.previous_logical_hash != receipt.current_logical_hash
    assert receipt.integrity_check == ("ok",)
    assert receipt.foreign_key_violation_count == 0
    assert Path(receipt.receipt_path).is_file()

    sector = sqlite3.connect(sector_path)
    try:
        assert sector.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0] == 1
        assert sector.execute("SELECT relock_state FROM mutation_receipt").fetchone()[0] == "RELOCKED"
    finally:
        sector.close()
    router = sqlite3.connect(root / "project" / "project_router.sqlite")
    try:
        assert router.execute("SELECT status FROM sector_mutation_grant WHERE grant_id=?", (receipt.grant_id,)).fetchone()[0] == "CONSUMED"
        assert router.execute("SELECT relocked FROM sector_mutation_receipt WHERE receipt_id=?", (receipt.router_receipt_id,)).fetchone()[0] == 1
        assert router.execute("SELECT COUNT(*) FROM brain_snapshot_registry").fetchone()[0] == 2
        assert router.execute("SELECT status FROM backend_ai_mutation_event WHERE mutation_id=?", (receipt.mutation_id,)).fetchone()[0] == "PASS"
    finally:
        router.close()


def test_failed_mutation_restores_sector_and_revokes_grant(tmp_path: Path) -> None:
    root = tmp_path / "brain_output"
    install_env15_resource(root)
    _, sector_path = resolve_env15_sector(root, "docs")
    before = _sha256(sector_path)

    def fail(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
            ("bad", "doc", "bad", "bad", "c" * 64, 1, "AVAILABLE", "turn_bad"),
        )
        raise RuntimeError("fixture failure")

    with pytest.raises(RuntimeError, match="fixture failure"):
        governed_sector_mutation(
            root, "docs", actor="Test Worker", reason="failure test", turn_id="turn_bad", mutate=fail
        )
    assert _sha256(sector_path) == before
    router = sqlite3.connect(root / "project" / "project_router.sqlite")
    try:
        assert router.execute("SELECT status FROM sector_mutation_grant").fetchone()[0] == "REVOKED"
        assert router.execute("SELECT status FROM backend_ai_mutation_event").fetchone()[0] == "FAILED_ROLLED_BACK"
        assert router.execute("SELECT COUNT(*) FROM sector_mutation_receipt").fetchone()[0] == 0
    finally:
        router.close()


def test_chat_lineage_cannot_use_named_sector_grant(tmp_path: Path) -> None:
    root = tmp_path / "brain_output"
    install_env15_resource(root)
    with pytest.raises(Env15MutationError, match="APPEND_ONLY_SERVICE"):
        governed_sector_mutation(
            root, "chat_lineage", actor="Test", reason="wrong route", turn_id="turn_1", mutate=lambda _: None
        )
