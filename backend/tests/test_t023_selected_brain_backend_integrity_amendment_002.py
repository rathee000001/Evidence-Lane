from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.brain_versions import capture_brain_version
from sqlite_brain_builder.runtime.portable_brain_package import RQ_QUESTIONS
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from sqlite_brain_builder.workspace.model_connectors import upsert_model_connector
from sqlite_brain_builder.workspace.model_execution import execute_model_connector
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


EVIDENCE_MANIFEST = Path(os.environ.get("EVIDENCE_LANE_INTEGRITY_EVIDENCE_MANIFEST", ""))


# Every amendment row is bound to executable proof, not prose-only status.
PROOF_MAP = {
    "21": (
        "tests/test_t023_selected_brain_backend_integrity_amendment_002.py::test_sqlite_baseline_inventory_is_read_only_and_hash_stable",
    ),
    "22": (
        "tests/test_t021_env15_code_integration.py::test_local_code_build_uses_env15_code_snapshot_schema_and_is_index_once",
        "tests/test_t021_office_structural_ingest.py::test_office_second_ingestion_adds_zero_rows_and_csv_is_not_a_workbook",
        "tests/test_t023_chat_lineage_model_delta_attention.py::test_docx_chat_lineage_is_deterministic_markdown_before_sqlite_with_original_provenance",
    ),
    "23": (
        "tests/test_t023_selected_brain_backend_integrity_amendment_002.py::test_source_identity_trace_is_stable_from_registration_to_selected_context",
    ),
    "24": (
        "tests/test_t023_portable_brain_package.py::test_snapshot_and_operational_ledger_are_separate",
        "tests/test_t021_env15_chat_lineage.py::test_chat_lineage_uses_trigger_enforced_prepare_commit_hash_chain_and_index_once",
    ),
    "25": (
        "tests/test_t021_env15_code_integration.py::test_git_code_build_maps_reverse_history_and_patch_hunks_into_env15",
        "tests/test_t021_env15_code_integration.py::test_non_git_changed_snapshot_records_synthetic_file_route_symbol_dependency_test_and_config_impacts",
    ),
    "26": (
        "tests/test_t021_section_b_artifacts_custom.py::test_binary_artifact_is_hash_metadata_only_with_stable_edges_and_no_reference_asset_copy",
        "tests/test_t023_selected_brain_backend_integrity_amendment_002.py::test_code_binary_policy_is_distinct_from_artifact_full_analysis",
    ),
    "27": (
        "tests/test_t021_office_structural_ingest.py::test_xlsx_captures_workbook_formula_relationship_and_layout_structure",
        "tests/test_t021_office_structural_ingest.py::test_pptx_captures_shapes_tables_notes_relationships_and_chunks",
    ),
    "28": (
        "tests/test_t023_selected_brain_context_and_picker_contract.py::test_lane_definitions_expose_exact_native_picker_descriptors_from_registry",
        "tests/test_t023_exact_behavior_backend_frontend_wiring.py::test_backend_rejects_cross_lane_file_types_even_if_a_picker_is_bypassed",
    ),
    "29": (
        "tests/test_t023_chat_lineage_model_delta_attention.py::test_governed_model_execution_materializes_one_immutable_hashed_delta_and_arms_only_its_brain",
        "tests/test_t023_refresh_fuse_brain_motion.py::test_refresh_and_fuse_are_explicit_separate_commands",
    ),
    "30": (
        "tests/test_t023_backend_package_idempotency_authority_acceptance.py::test_codex_archive_identity_binds_governance_and_identical_replay_is_byte_stable",
        "tests/test_t023_exact_behavior_backend_frontend_wiring.py::test_flash_prompt_uses_exact_project_brief_and_provider_neutral_receipt",
    ),
    "31": (
        "tests/test_t023_selected_brain_backend_integrity_amendment_002.py::test_every_model_prompt_appends_task_rq_and_authoritative_usage_rows",
    ),
    "32": (
        "tests/test_t023_selected_brain_backend_integrity_amendment_002.py::test_final_evidence_manifest_covers_every_amendment_row",
    ),
}


def _table_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


def test_duplicate_catalog_labels_use_one_utf8_middle_dot_without_mojibake(
    tmp_path: Path,
    monkeypatch,
) -> None:
    route_root = tmp_path / "catalog-root"
    first = route_root / "packet-one"
    second = route_root / "packet-two"
    for workspace in (first, second):
        init_workspace(workspace)
        create_brain(workspace, "Book Fairies Git Only HIL Stress")

    monkeypatch.setattr(
        ipc_worker,
        "list_workspace_roots",
        lambda: {
            "active_root": str(route_root),
            "roots": [
                {
                    "path": str(route_root),
                    "display_name": route_root.name,
                    "source": "TEST",
                    "active": True,
                }
            ],
        },
    )
    monkeypatch.setattr(
        ipc_worker,
        "_catalog_workspace_dirs",
        lambda _root, *, refresh=False: [first, second],
    )

    catalog = ipc_worker._brain_catalog()
    display_names = {row["display_name"] for row in catalog["brains"]}

    assert display_names == {
        "Book Fairies Git Only HIL Stress · packet-one",
        "Book Fairies Git Only HIL Stress · packet-two",
    }
    assert all("Â" not in display_name for display_name in display_names)


def test_sqlite_baseline_inventory_is_read_only_and_hash_stable(tmp_path: Path) -> None:
    databases = []
    for name, rows in (("v59.sqlite", 2), ("env15.sqlite", 3)):
        database = tmp_path / name
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany("INSERT INTO evidence(value) VALUES(?)", [(f"row-{index}",) for index in range(rows)])
        databases.append(database)

    before = {
        str(database): {
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "counts": _table_counts(database),
        }
        for database in databases
    }
    for database in databases:
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert list(connection.execute("PRAGMA foreign_key_check")) == []
            assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == before[str(database)]["counts"]["evidence"]
    after = {
        str(database): {
            "sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "counts": _table_counts(database),
        }
        for database in databases
    }
    assert after == before


def test_source_identity_trace_is_stable_from_registration_to_selected_context(tmp_path: Path) -> None:
    from sqlite_brain_builder import ipc_worker

    workspace = tmp_path / "workspace"
    source = tmp_path / "identity.md"
    source.write_text("# Stable identity\n", encoding="utf-8")
    ipc_worker.handle({"command": "workspace.init", "payload": {"workspace_dir": str(workspace)}})
    ipc_worker.handle({"command": "brain.create", "payload": {"workspace_dir": str(workspace), "brain_name": "Identity Brain"}})
    registered = ipc_worker.handle(
        {
            "command": "sources.add",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Identity Brain",
                "lane_key": "docs",
                "path": str(source),
            },
        }
    )["source"]
    context = ipc_worker.handle(
        {
            "command": "brain.select",
            "payload": {"workspace_dir": str(workspace), "brain_name": "Identity Brain"},
        }
    )
    result = build_brain(str(workspace), "Identity Brain", [registered], generate_mmd=False)
    database = Path(result["brain_root"]) / "project" / "sectors" / "docs" / "docs_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        registry_id = connection.execute("SELECT source_id FROM source_registry").fetchone()[0]
        version_id = connection.execute("SELECT source_id FROM source_version").fetchone()[0]
        chunk_id = connection.execute("SELECT source_id FROM chunk_index LIMIT 1").fetchone()[0]

    visible = next(item for item in context["sources"] if item["path"] == str(source))
    assert visible["source_id"] == registered["source_id"] == registry_id == version_id == chunk_id


def test_code_binary_policy_is_distinct_from_artifact_full_analysis(tmp_path: Path) -> None:
    code = tmp_path / "code"
    code.mkdir()
    binary = code / "scene.glb"
    raw = b"glTF\x02\x00\x00\x00BINARY_CODE_ASSET"
    binary.write_bytes(raw)
    (code / "app.py").write_text("def route(): return '/asset'\n", encoding="utf-8")
    code_result = build_brain(
        str(tmp_path / "code-workspace"),
        "Code Binary Brain",
        [{"source_id": "source-code-binary", "lane_key": "local_code", "path": str(code), "active": True}],
        generate_mmd=False,
    )
    artifact_result = build_brain(
        str(tmp_path / "artifact-workspace"),
        "Artifact Binary Brain",
        [{"source_id": "source-artifact-binary", "lane_key": "artifacts", "path": str(binary), "active": True}],
        generate_mmd=False,
    )
    code_db = Path(code_result["brain_root"]) / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    artifact_db = Path(artifact_result["brain_root"]) / "project" / "sectors" / "artifacts" / "artifacts_sector_v001.sqlite"
    with sqlite3.connect(code_db) as connection:
        code_row = connection.execute(
            "SELECT relative_path,content_kind,sha256 FROM code_file_snapshot WHERE relative_path='scene.glb'"
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM code_chunk cc JOIN code_file_snapshot fs ON fs.file_id=cc.file_id "
            "WHERE fs.relative_path='scene.glb'"
        ).fetchone()[0] == 0
    with sqlite3.connect(artifact_db) as connection:
        artifact_row = connection.execute(
            "SELECT canonical_path,sha256,review_state FROM artifact_registry WHERE source_id='source-artifact-binary'"
        ).fetchone()
        artifact_chunks = connection.execute(
            "SELECT chunk_type,content FROM chunk_index WHERE source_id='source-artifact-binary'"
        ).fetchall()
    digest = hashlib.sha256(raw).hexdigest()
    assert code_row == ("scene.glb", "BINARY_METADATA_ONLY", digest)
    assert artifact_row == ("scene.glb", digest, "INDEXED")
    assert artifact_chunks and artifact_chunks[0][0] == "artifacts:metadata_only"


def _model_profile() -> dict:
    return {
        "endpoint_id": "usage-endpoint",
        "display_name": "Usage Endpoint",
        "endpoint_type": "OPENAI_COMPATIBLE",
        "base_url": "https://models.example.test",
        "model_id": "usage-model",
        "health_check_path": "/health",
        "request_path": "/v1/chat/completions",
        "model_list_path": "/v1/models",
        "authentication_type": "NONE",
        "authentication_reference": None,
        "streaming_supported": False,
        "tool_call_supported": False,
        "file_supported": False,
        "context_limit": 8192,
        "request_mapping": {"model": "model", "messages": "messages"},
        "response_mapping": {"text": "choices.0.message.content", "provider_response_id": "id"},
        "usage_mapping": {},
        "timeout": 10,
        "local_or_remote": "REMOTE",
        "privacy_classification": "BOUNDED_REMOTE",
        "enabled": True,
        "active_prompt_bar": True,
    }


def test_every_model_prompt_appends_task_rq_and_authoritative_usage_rows(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source.md"
    source.write_text("# Bounded evidence\n", encoding="utf-8")
    build_brain(
        str(workspace),
        "Usage Brain",
        [{"source_id": "source-usage", "lane_key": "docs", "path": str(source), "active": True}],
        generate_mmd=False,
    )
    capture_brain_version(
        workspace,
        "Usage Brain",
        actor_type="app",
        actor_name="integrity test",
        reason="usage baseline",
    )
    upsert_model_connector(workspace, _model_profile())
    result = execute_model_connector(
        workspace,
        "Usage Brain",
        "usage-endpoint",
        "Which governed evidence is in scope?",
        task_id="T023-USAGE-PROMPT-001",
        remote_consent=True,
        transport=lambda *_args: {
            "id": "provider-without-usage",
            "choices": [{"message": {"content": "Bounded answer"}}],
        },
    )
    ledger = Path(result["operational_ledger"])
    with sqlite3.connect(ledger) as connection:
        tasks = connection.execute(
            "SELECT task_id,starting_snapshot_hash,completion_status FROM task_run"
        ).fetchall()
        usage = connection.execute(
            "SELECT task_id,classification,token_exact,input_tokens,output_tokens,total_tokens FROM model_call_usage"
        ).fetchall()
        rqs = connection.execute(
            "SELECT question_id,answer,claim_status FROM rq_ledger ORDER BY id"
        ).fetchall()
        prompts = connection.execute(
            "SELECT query_type,query_text,result_count FROM project_query_log"
        ).fetchall()
        steer_answers = connection.execute(
            "SELECT task_run_id,steer_id,prompt_id,prompt_text,answer_text,answer_status,"
            "usage_classification,goal_usage_summary FROM steer_prompt_answer"
        ).fetchall()

    assert result["usage_classification"] == "UNAVAILABLE_AUTHORITATIVE_USAGE"
    assert tasks == [("T023-USAGE-PROMPT-001", result["brain_snapshot_hash"], "CANDIDATE_RESPONSE")]
    assert usage == [("T023-USAGE-PROMPT-001", "UNAVAILABLE_AUTHORITATIVE_USAGE", 0, None, None, None)]
    assert len(rqs) == len(RQ_QUESTIONS) * 2
    baseline_rows = rqs[: len(RQ_QUESTIONS)]
    candidate_rows = rqs[len(RQ_QUESTIONS) :]
    assert {row[0] for row in baseline_rows} == set(RQ_QUESTIONS)
    assert {row[2] for row in baseline_rows} == {"TEST_VERIFIED", "OPEN"}
    assert {row[0] for row in candidate_rows} == set(RQ_QUESTIONS)
    assert all(row[1] and row[2] == "CANDIDATE" for row in candidate_rows)
    assert prompts == [("MODEL_PROMPT", "Which governed evidence is in scope?", 0)]
    assert len(steer_answers) == 1
    steer_answer = steer_answers[0]
    assert steer_answer[0] == 1
    assert steer_answer[1] == "T023-USAGE-PROMPT-001"
    assert steer_answer[2] == result["execution_id"]
    assert steer_answer[3:7] == (
        "Which governed evidence is in scope?",
        "Bounded answer",
        "CANDIDATE",
        "UNAVAILABLE_AUTHORITATIVE_USAGE",
    )
    assert steer_answer[7].startswith("Goal usage: unavailable over ")
    steer_sidecar = json.loads((ledger.parent / "STEER_PROMPT_ANSWER_LOG.json").read_text(encoding="utf-8"))
    assert steer_sidecar["product_name"] == "Evidence Lane"
    assert steer_sidecar["entries"][0]["answer"] == "Bounded answer"


def test_final_evidence_manifest_covers_every_amendment_row() -> None:
    if not EVIDENCE_MANIFEST.is_file():
        pytest.skip(
            "set EVIDENCE_LANE_INTEGRITY_EVIDENCE_MANIFEST to run the host-bound manifest audit"
        )
    manifest = json.loads(EVIDENCE_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "T023_SELECTED_BRAIN_BACKEND_INTEGRITY_EVIDENCE_V1"
    assert manifest["current_pointer"] == "T023-SELECTED-BRAIN-ACTIVE-CONTEXT-CLASSIFIED-INTAKE"
    assert manifest["goal_restarted"] is False
    assert manifest["parallel_writer_started"] is False
    assert set(manifest["amendment_002_rows"]) == set(PROOF_MAP)
    for row_id, expected_proofs in PROOF_MAP.items():
        row = manifest["amendment_002_rows"][row_id]
        assert row["status"] == "PASS"
        assert set(row["proof_node_ids"]) >= set(expected_proofs)
    assert manifest["full_repository_tests"]["status"] == "PASS"
    assert manifest["frontend_build"]["status"] == "PASS"
    assert manifest["dual_browser_verification"]["status"] == "PASS"
