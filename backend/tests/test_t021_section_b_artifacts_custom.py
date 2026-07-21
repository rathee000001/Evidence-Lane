from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.env15_ingestion import Env15IngestionError, ingest_env15_universal_source
from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.env15_resource import install_env15_resource
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from sqlite_brain_builder.ipc_worker import WorkerError, handle


def test_binary_artifact_is_hash_metadata_only_with_stable_edges_and_no_reference_asset_copy(tmp_path: Path) -> None:
    source = tmp_path / "reference-ui.png"
    raw = b"\x89PNG\r\n\x1a\nREFERENCE_UI_BINARY_MUST_NOT_BECOME_APP_TRUTH"
    source.write_bytes(raw)
    payload = {"source_id": "source_binary_artifact", "lane_key": "artifacts", "path": str(source), "active": True}
    first = build_brain(str(tmp_path), "Artifact Policy Brain", [payload], generate_mmd=False)
    root = Path(first["brain_root"])
    second = build_brain(str(tmp_path), "Artifact Policy Brain", [payload], generate_mmd=False)
    _, database = resolve_env15_sector(root, "artifacts")
    connection = sqlite3.connect(database)
    try:
        artifact = connection.execute(
            "SELECT canonical_path,sha256,size_bytes,review_state FROM artifact_registry WHERE source_id=?",
            (payload["source_id"],),
        ).fetchone()
        chunk = connection.execute(
            "SELECT chunk_type,content FROM chunk_index WHERE source_id=?", (payload["source_id"],)
        ).fetchone()
        edges = connection.execute(
            "SELECT edge_id,source_node,relation_type,target_node,evidence_ref FROM relation_edge WHERE source_node=?",
            (payload["source_id"],),
        ).fetchall()
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("source_registry", "artifact_registry", "chunk_index", "relation_edge", "artifact_fts", "mutation_receipt")
        )
    finally:
        connection.close()
    assert second["incremental"]["all_sources_unchanged"] is True
    assert artifact == (source.name, hashlib.sha256(raw).hexdigest(), len(raw), "INDEXED")
    assert chunk[0] == "artifacts:metadata_only"
    assert "REFERENCE_UI_BINARY_MUST_NOT_BECOME_APP_TRUTH" not in chunk[1]
    assert len(edges) == 1 and edges[0][2] == "contains"
    assert counts == (1, 1, 1, 1, 1, 1)
    assert not list((root / "project").rglob(source.name))


def test_custom_requires_safe_schema_and_indexes_contract_deterministically(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    install_env15_resource(root)
    base = {
        "source_id": "source_custom_contract",
        "display_name": "Custom governed source",
        "text": "Evidence: custom contract fixture\n",
    }
    with pytest.raises(Env15IngestionError, match="CUSTOM_SCHEMA_CONTRACT_REQUIRED"):
        ingest_env15_universal_source(root, "custom", base)
    with pytest.raises(Env15IngestionError, match="UNSAFE_SCHEMA_CONTRACT_PATTERN"):
        ingest_env15_universal_source(root, "custom", {**base, "schema_contract": "drop table evidence"})

    first_payload = {**base, "schema_contract": "evidence\ndecision\nnext_action"}
    first = ingest_env15_universal_source(root, "custom", first_payload)
    replay = ingest_env15_universal_source(root, "custom", first_payload)
    changed = ingest_env15_universal_source(
        root, "custom", {**base, "schema_contract": "evidence\ndecision\nnext_action\nreceipt"}
    )
    _, database = resolve_env15_sector(root, "custom")
    connection = sqlite3.connect(database)
    try:
        contract = connection.execute(
            "SELECT fields_json,raw_contract,parser_version FROM custom_schema_contract WHERE source_id=?",
            (base["source_id"],),
        ).fetchone()
        schema_chunks = connection.execute(
            "SELECT COUNT(*) FROM chunk_index WHERE source_id=? AND chunk_type='custom:schema_contract'",
            (base["source_id"],),
        ).fetchone()[0]
        schema_edges = connection.execute(
            "SELECT COUNT(*) FROM relation_edge WHERE source_node=? AND relation_type='governed_by_schema'",
            (base["source_id"],),
        ).fetchone()[0]
        receipts = connection.execute("SELECT COUNT(*) FROM mutation_receipt").fetchone()[0]
    finally:
        connection.close()
    assert first.status == "PASS"
    assert replay.status == "SKIPPED_UNCHANGED"
    assert changed.status == "PASS"
    assert contract == ('["evidence", "decision", "next_action", "receipt"]', "evidence\ndecision\nnext_action\nreceipt", "schema_first_v1")
    assert schema_chunks == 1
    assert schema_edges == 1
    assert receipts == 2


def test_custom_schema_first_contract_cannot_be_bypassed_through_worker_command(tmp_path: Path) -> None:
    source = tmp_path / "custom.md"
    source.write_text("Evidence: worker fixture\n", encoding="utf-8")
    base_request = {
        "id": "custom-worker",
        "command": "sources.add",
        "payload": {
            "workspace_dir": str(tmp_path / "workspace"),
            "brain_name": "Custom Worker Brain",
            "lane_key": "custom",
            "path": str(source),
        },
    }
    handle({"id": "init", "command": "workspace.init", "payload": {"workspace_dir": str(tmp_path / "workspace")}})
    with pytest.raises(WorkerError, match="CUSTOM_SCHEMA_CONTRACT_REQUIRED"):
        handle(base_request)
    valid = handle(
        {
            **base_request,
            "payload": {**base_request["payload"], "schema_contract": "evidence\ndecision\nreceipt"},
        }
    )
    assert valid["source"]["lane_key"] == "custom"
    assert valid["source"]["schema_contract"] == "evidence\ndecision\nreceipt"
