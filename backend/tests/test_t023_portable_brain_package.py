from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.runtime.portable_brain_package import (
    RQ_BASELINE_ANSWERS,
    RQ_QUESTIONS,
    append_task_run,
    create_portable_brain_package,
    export_portable_brain_zip,
    record_model_usage,
    record_rq_answers,
    record_steer_prompt_answer,
    query_immutable_snapshot,
    validate_portable_brain_package,
    validate_portable_brain_zip,
)


def _source_brain(root: Path) -> Path:
    project = root / "project" / "sectors" / "local_code"
    project.mkdir(parents=True)
    database = project / "local_code_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE source_file(source_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, api_key TEXT)"
        )
        connection.execute(
            "INSERT INTO source_file VALUES(?,?,?,?)",
            ("source-1", "src/main.py", "A" * 64, "test-provider-secret-must-not-escape"),
        )
        connection.execute(
            "CREATE TABLE code_chunk(chunk_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, content TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO code_chunk VALUES(?,?,?)",
            ("chunk-1", "source-1", "def verified_route(): return 'PASS'"),
        )
        connection.execute("CREATE VIRTUAL TABLE code_fts USING fts5(chunk_id UNINDEXED, content)")
        connection.execute("INSERT INTO code_fts VALUES(?,?)", ("chunk-1", "verified route package"))
    return database


def _package(tmp_path: Path) -> tuple[Path, dict]:
    brain = tmp_path / "brain"
    _source_brain(brain)
    package = tmp_path / "portable" / "brain_package"
    result = create_portable_brain_package(
        brain,
        package,
        goal_pointer={
            "parent_goal_id": "T023_SUPREME_SOURCE_RECONCILED_LINEAR_EVIDENCEOS",
            "current_task_pointer": "T023-DELTA-SQLITE-READER-CODEX-LEDGER-LAYER1-CONTRACT",
        },
        delta_ledger={"delta_id": "DELTA_T023_SQLITE_READER_CODEX_LEDGER_001", "status": "ACTIVE"},
    )
    return package, result


def _task_record(snapshot_hash: str, run_id: str) -> dict:
    return {
        "parent_goal_id": "T023_SUPREME_SOURCE_RECONCILED_LINEAR_EVIDENCEOS",
        "run_id": run_id,
        "task_id": "T023-LEDGER-TEST",
        "step_id": "STEP-1",
        "delta_ids_active": ["DELTA_T023_SQLITE_READER_CODEX_LEDGER_001"],
        "model": "test-model",
        "reasoning_mode": "test",
        "start_timestamp": "2026-07-13T00:00:00Z",
        "starting_snapshot_hash": snapshot_hash,
        "sources_queried": ["brain_snapshot.sqlite"],
        "sqlite_tables_queried": ["source_table_row"],
        "fts_queries_issued": ["verified"],
        "raw_files_reread": [],
        "unchanged_files_skipped": ["local_code_sector_v001.sqlite"],
        "files_changed": [],
        "tests_run": ["portable-contract"],
        "test_results": ["PASS"],
        "build_status": "PASS",
        "human_gate_status": "PENDING",
        "completion_status": "CANDIDATE",
        "next_exact_pointer": "HIL_GATE",
    }


def test_snapshot_is_read_only_source_linked_and_secret_safe(tmp_path: Path) -> None:
    package, result = _package(tmp_path)
    snapshot = package / "brain_snapshot.sqlite"
    with sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        statement = connection.execute("SELECT statement FROM snapshot_metadata").fetchone()[0]
        rows = [json.loads(row[0]) for row in connection.execute("SELECT row_json FROM source_table_row")]
        fts_count = connection.execute("SELECT COUNT(*) FROM snapshot_fts WHERE snapshot_fts MATCH 'verified'").fetchone()[0]
    assert statement == "THE BRAIN IS A VERSIONED, SOURCE-LINKED PROJECT SNAPSHOT."
    assert fts_count >= 1
    assert "test-provider-secret" not in json.dumps(rows)
    assert result["snapshot"]["new_immutable_snapshot"] == 1
    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True) as connection:
            connection.execute("INSERT INTO snapshot_metadata VALUES('x','x','x','OPEN','x','x')")


def test_identical_replay_reuses_snapshot_and_adds_only_operational_run(tmp_path: Path) -> None:
    package, first = _package(tmp_path)
    second = create_portable_brain_package(
        tmp_path / "brain",
        package,
        goal_pointer={"current_task_pointer": "HIL_GATE"},
        delta_ledger={"delta_id": "DELTA_T023_SQLITE_READER_CODEX_LEDGER_001"},
    )
    replay = second["snapshot"]
    assert replay["snapshot_reused"] is True
    assert replay["new_immutable_snapshot"] == 0
    assert replay["raw_databases_opened"] == 0
    assert replay["new_source_rows"] == 0
    assert replay["new_file_version_rows"] == 0
    assert replay["new_chunks"] == 0
    assert replay["new_fts_rows"] == 0
    ledger = package / "codex" / "codex_runtime_ledger.sqlite"
    before = sqlite3.connect(ledger).execute("SELECT COUNT(*) FROM task_run").fetchone()[0]
    append_task_run(ledger, _task_record(first["snapshot"]["snapshot_sha256"], "replay-2"))
    with sqlite3.connect(ledger) as connection:
        after = connection.execute("SELECT COUNT(*) FROM task_run").fetchone()[0]
        knowledge_rows = connection.execute("SELECT COUNT(*) FROM rq_ledger").fetchone()[0]
    assert after - before == 1
    assert knowledge_rows == len(RQ_QUESTIONS)


def test_operational_ledger_is_append_only(tmp_path: Path) -> None:
    package, result = _package(tmp_path)
    ledger = package / "codex" / "codex_runtime_ledger.sqlite"
    task_id = append_task_run(ledger, _task_record(result["snapshot"]["snapshot_sha256"], "run-1"))
    with sqlite3.connect(ledger) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_UPDATE_FORBIDDEN"):
            connection.execute("UPDATE task_run SET completion_status='PASS' WHERE id=?", (task_id,))
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_DELETE_FORBIDDEN"):
            connection.execute("DELETE FROM task_run WHERE id=?", (task_id,))


def test_usage_classification_never_invents_exact_tokens(tmp_path: Path) -> None:
    package, result = _package(tmp_path)
    ledger = package / "codex" / "codex_runtime_ledger.sqlite"
    task_id = append_task_run(ledger, _task_record(result["snapshot"]["snapshot_sha256"], "usage-run"))
    exact_id = record_model_usage(
        ledger,
        {
            "task_run_id": task_id,
            "goal_id": "goal",
            "run_id": "usage-run",
            "task_id": "task",
            "classification": "EXACT_PROVIDER_USAGE",
            "usage_source": "provider_response.usage",
            "provider": "provider",
            "model": "model",
            "provider_response_id": "response-1",
            "input_tokens": 10,
            "cached_input_tokens": 3,
            "uncached_input_tokens": 7,
            "output_tokens": 5,
            "reasoning_tokens": 2,
            "total_tokens": 15,
            "call_started_at": "2026-07-13T00:00:00Z",
            "call_completed_at": "2026-07-13T00:00:01Z",
        },
    )
    unavailable_id = record_model_usage(
        ledger,
        {
            "task_run_id": task_id,
            "goal_id": "goal",
            "run_id": "usage-run",
            "task_id": "task",
            "classification": "UNAVAILABLE",
            "usage_source": "provider_did_not_expose_usage",
            "elapsed_seconds": 1.0,
            "quota_checkpoint": {"weekly_percent_left": 98},
        },
    )
    with pytest.raises(ValueError, match="NON_AUTHORITATIVE"):
        record_model_usage(
            ledger,
            {
                "goal_id": "goal",
                "run_id": "usage-run",
                "task_id": "task",
                "classification": "AGGREGATE_ACCOUNT_ONLY",
                "usage_source": "account_ui",
                "total_tokens": 100,
            },
        )
    with sqlite3.connect(ledger) as connection:
        exact = connection.execute("SELECT token_exact,total_tokens FROM model_call_usage WHERE id=?", (exact_id,)).fetchone()
        unavailable = connection.execute("SELECT token_exact,total_tokens FROM model_call_usage WHERE id=?", (unavailable_id,)).fetchone()
    assert exact == (1, 15)
    assert unavailable == (0, None)
    usage_sidecar = json.loads((package / "codex" / "TASK_USAGE_RECEIPT.json").read_text(encoding="utf-8"))
    assert usage_sidecar["ledger_row_id"] == unavailable_id
    assert usage_sidecar["classification"] == "UNAVAILABLE"
    assert usage_sidecar["token_exact"] is False
    assert usage_sidecar["total_tokens"] is None
    assert usage_sidecar["goal_usage"] == "Goal usage: unavailable over 1s."


def test_rq_ledger_requires_all_questions_and_package_reopens(tmp_path: Path) -> None:
    package, result = _package(tmp_path)
    ledger = package / "codex" / "codex_runtime_ledger.sqlite"
    task_id = append_task_run(ledger, _task_record(result["snapshot"]["snapshot_sha256"], "rq-run"))
    with pytest.raises(ValueError, match="RQ_ANSWERS_MISSING"):
        record_rq_answers(ledger, task_id, {"RQ-01": "partial"})
    rows = record_rq_answers(ledger, task_id, {question_id: "CANDIDATE answer" for question_id in RQ_QUESTIONS})
    assert len(rows) == len(RQ_QUESTIONS) == 6
    rq_sidecar = json.loads((package / "codex" / "RQ_LEDGER.json").read_text(encoding="utf-8"))
    assert rq_sidecar["schema"] == "EVIDENCE_LANE_RQ_LEDGER_V002"
    assert rq_sidecar["supersedes_question_set"] == "T023_RQ_QUESTION_SET_V001"
    assert set(rq_sidecar["questions"]) == set(RQ_QUESTIONS)
    assert {row["claim_status"] for row in rq_sidecar["questions"].values()} == {"CANDIDATE"}
    assert {row["task_run_id"] for row in rq_sidecar["questions"].values()} == {task_id}
    assert validate_portable_brain_package(package)["status"] == "PASS"
    archive = tmp_path / "exports" / "brain_package_v001.zip"
    exported = export_portable_brain_zip(package, archive)
    assert exported["zip_crc"] == "PASS"
    assert exported["nested_zip_count"] == 0
    with pytest.raises(FileExistsError, match="IMMUTABLE"):
        export_portable_brain_zip(package, archive)


def test_evidence_lane_rq_baseline_and_each_steer_prompt_answer_keep_simple_goal_usage(tmp_path: Path) -> None:
    package, result = _package(tmp_path)
    ledger = package / "codex" / "codex_runtime_ledger.sqlite"
    baseline = json.loads((package / "codex" / "RQ_LEDGER.json").read_text(encoding="utf-8"))
    assert baseline["product_name"] == "Evidence Lane"
    assert set(baseline["questions"]) == set(RQ_QUESTIONS) == set(RQ_BASELINE_ANSWERS)
    assert "RQ-01" not in baseline["questions"]
    assert baseline["questions"]["RQ-S04"]["claim_status"] == "OPEN"
    assert baseline["questions"]["RQ-S05"]["answer"].startswith("Not proven.")

    task_id = append_task_run(ledger, _task_record(result["snapshot"]["snapshot_sha256"], "steer-run"))
    recorded = record_steer_prompt_answer(
        ledger,
        {
            "task_run_id": task_id,
            "steer_id": "STEER-001",
            "prompt_id": "PROMPT-001",
            "prompt_text": "Continue from the verified pointer.",
            "answer_text": "Continued without replacing the active goal.",
            "answer_status": "SOURCE_VERIFIED",
            "usage_classification": "EXACT_PROVIDER_USAGE",
            "total_tokens": 237126,
            "elapsed_seconds": 393,
        },
    )
    assert recorded["goal_usage"] == "Goal usage: 237,126 tokens over 6m 33s."
    with sqlite3.connect(ledger) as connection:
        row = connection.execute(
            "SELECT steer_id,prompt_id,answer_status,goal_usage_summary,total_tokens,elapsed_seconds "
            "FROM steer_prompt_answer WHERE id=?",
            (recorded["row_id"],),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_UPDATE_FORBIDDEN"):
            connection.execute(
                "UPDATE steer_prompt_answer SET answer_text='rewritten' WHERE id=?",
                (recorded["row_id"],),
            )
    assert row == (
        "STEER-001",
        "PROMPT-001",
        "SOURCE_VERIFIED",
        "Goal usage: 237,126 tokens over 6m 33s.",
        237126,
        393.0,
    )
    sidecar = json.loads((package / "codex" / "STEER_PROMPT_ANSWER_LOG.json").read_text(encoding="utf-8"))
    assert sidecar["schema"] == "EVIDENCE_LANE_STEER_PROMPT_ANSWER_LOG_V001"
    assert sidecar["entries"][-1]["goal_usage"] == "Goal usage: 237,126 tokens over 6m 33s."


def test_snapshot_query_never_reopens_raw_source_databases(tmp_path: Path) -> None:
    package, _ = _package(tmp_path)
    result = query_immutable_snapshot(package / "brain_snapshot.sqlite", "verified")
    assert result["raw_source_databases_opened"] == 0
    assert result["tables_queried"] == ["snapshot_fts"]
    assert result["result_count"] == len(result["results"])
    assert result["results"]
    assert {row["table_name"] for row in result["results"]} >= {"code_chunk"}


def test_snapshot_stores_identical_rows_and_fts_content_once_across_sources(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    for sector in ("github_code", "local_code"):
        database = brain / "project" / "sectors" / sector / f"{sector}_sector_v001.sqlite"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY, content TEXT NOT NULL)")
            connection.execute("INSERT INTO evidence VALUES('same-row','canonical verified content')")
    package = tmp_path / "portable"
    result = create_portable_brain_package(
        brain,
        package,
        goal_pointer={"parent_goal_id": "goal", "current_task_pointer": "dedup"},
        delta_ledger={"delta_id": "dedup-delta"},
    )

    snapshot = package / "brain_snapshot.sqlite"
    with sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        reference_rows = connection.execute(
            "SELECT COUNT(*) FROM source_table_row_ref WHERE table_name='evidence'"
        ).fetchone()[0]
        canonical_rows = connection.execute(
            "SELECT COUNT(*) FROM canonical_row_content WHERE content LIKE '%canonical verified content%'"
        ).fetchone()[0]
        fts_rows = connection.execute(
            "SELECT COUNT(*) FROM snapshot_fts WHERE snapshot_fts MATCH 'canonical'"
        ).fetchone()[0]
        source_row_type = connection.execute(
            "SELECT type FROM sqlite_master WHERE name='source_table_row'"
        ).fetchone()[0]

    assert reference_rows == 2
    assert canonical_rows == 1
    assert fts_rows == 1
    assert source_row_type == "view"
    assert result["snapshot"]["canonical_row_reuse_count"] >= 1
    assert result["snapshot"]["physical_row_json_copies_per_hash"] == 1
    assert result["snapshot"]["fts_content_storage"] == "EXTERNAL_CANONICAL_CONTENT"


def test_ipc_exposes_explicit_portable_commands_without_implicit_build(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    brain_name = "Portable IPC Brain"
    source_root = workspace / "portable_ipc_brain_output"
    _source_brain(source_root)
    create_result = ipc_worker.handle(
        {
            "id": "portable-create",
            "command": "brain.portablePackage.create",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "goal_pointer": {
                    "parent_goal_id": "T023_SUPREME_SOURCE_RECONCILED_LINEAR_EVIDENCEOS",
                    "active_run_id": "run-portable-ipc",
                    "current_task_pointer": "T023-DELTA-SQLITE-READER-CODEX-LEDGER-LAYER1-CONTRACT",
                },
                "delta_ledger": {
                    "delta_id": "DELTA_T023_SQLITE_READER_CODEX_LEDGER_001",
                    "status": "OPEN_REGISTERED",
                },
            },
        }
    )
    assert create_result["validation"]["status"] == "PASS"
    package_root = Path(create_result["package_root"])
    task = ipc_worker.handle(
        {
            "command": "brain.codexLedger.task.append",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "record": _task_record(create_result["snapshot"]["snapshot_sha256"], "run-portable-ipc"),
            },
        }
    )
    query = ipc_worker.handle(
        {
            "command": "brain.portablePackage.query",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "task_run_id": task["task_run_id"],
                "query": "verified",
            },
        }
    )
    assert query["results"]
    assert query["raw_source_databases_opened"] == 0
    rq = ipc_worker.handle(
        {
            "command": "brain.codexLedger.rq.record",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "task_run_id": task["task_run_id"],
                "answers": {key: "CANDIDATE" for key in RQ_QUESTIONS},
            },
        }
    )
    assert len(rq["rq_row_ids"]) == len(RQ_QUESTIONS)
    steer = ipc_worker.handle(
        {
            "command": "brain.codexLedger.steerPrompt.record",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "record": {
                    "task_run_id": task["task_run_id"],
                    "steer_id": "STEER-IPC-001",
                    "prompt_id": "PROMPT-IPC-001",
                    "prompt_text": "Record this steer and prompt.",
                    "answer_text": "Recorded append-only.",
                    "usage_classification": "EXACT_PROVIDER_USAGE",
                    "total_tokens": 237126,
                    "elapsed_seconds": 393,
                },
            },
        }
    )
    assert steer["goal_usage"] == "Goal usage: 237,126 tokens over 6m 33s."
    exported = ipc_worker.handle(
        {
            "command": "brain.portablePackage.export",
            "payload": {"workspace_dir": str(workspace), "brain_name": brain_name},
        }
    )
    assert exported["zip_crc"] == "PASS"
    assert exported["reused"] is False
    reopened = validate_portable_brain_zip(exported["archive"])
    assert reopened["status"] == "PASS"
    reused = ipc_worker.handle(
        {
            "command": "brain.portablePackage.export",
            "payload": {"workspace_dir": str(workspace), "brain_name": brain_name},
        }
    )
    assert reused["reused"] is True
    status = ipc_worker.handle(
        {
            "command": "brain.portablePackage.status",
            "payload": {"workspace_dir": str(workspace), "brain_name": brain_name},
        }
    )
    assert status["status"] == "PASS"
    assert package_root.is_dir()
