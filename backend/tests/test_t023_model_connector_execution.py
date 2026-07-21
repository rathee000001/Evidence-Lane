from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.brain_versions import capture_brain_version
from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.env15_topology import generate_env15_topologies
from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.package_parity import validate_active_project_package_parity
from sqlite_brain_builder.runtime.package_validation import validate_chatgpt_package, validate_gemini_exact10
from sqlite_brain_builder.runtime.plan_delta_governance import read_plan_delta_state
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
    materialize_schema_only_package_topology,
)
from sqlite_brain_builder.workspace import local_workspace
from sqlite_brain_builder.workspace.model_connectors import upsert_model_connector
from sqlite_brain_builder.workspace.model_execution import ModelExecutionError, execute_model_connector


def _verified_brain(workspace: Path, brain_name: str = "Execution Brain") -> None:
    source = workspace.parent / "source.md"
    source.write_text("# Bounded evidence\nVerified source text.\n", encoding="utf-8")
    build_brain(
        str(workspace),
        brain_name,
        [{"source_id": "source-doc", "lane_key": "docs", "path": str(source), "active": True}],
        generate_mmd=False,
    )
    capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS test",
        reason="verified connector execution fixture",
    )


def _profile(*, authentication_reference: str | None = None) -> dict:
    return {
        "endpoint_id": "remote-endpoint",
        "display_name": "Remote Endpoint",
        "endpoint_type": "OPENAI_COMPATIBLE",
        "base_url": "https://models.example.test",
        "model_id": "model-1",
        "health_check_path": "/health",
        "request_path": "/v1/chat/completions",
        "model_list_path": "/v1/models",
        "authentication_type": "BEARER_REFERENCE" if authentication_reference else "NONE",
        "authentication_reference": authentication_reference,
        "streaming_supported": True,
        "tool_call_supported": True,
        "file_supported": False,
        "context_limit": 32768,
        "request_mapping": {"model": "model", "messages": "messages"},
        "response_mapping": {"text": "choices.0.message.content", "provider_response_id": "id"},
        "usage_mapping": {"input_tokens": "usage.prompt_tokens", "output_tokens": "usage.completion_tokens", "total_tokens": "usage.total_tokens"},
        "timeout": 45,
        "local_or_remote": "REMOTE",
        "privacy_classification": "BOUNDED_REMOTE",
        "enabled": True,
        "active_prompt_bar": True,
    }


def _ledger(workspace: Path, brain_name: str = "Execution Brain") -> Path:
    return workspace / "portable_brain_workspaces" / "execution_brain" / "codex" / "codex_runtime_ledger.sqlite"


def test_governed_execution_sends_only_explicit_prompt_and_logs_exact_response(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _verified_brain(workspace)
    upsert_model_connector(workspace, _profile())
    observed: dict = {}

    def transport(url: str, payload: dict, headers: dict, timeout: float) -> dict:
        observed.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "id": "provider-response-1",
            "choices": [{"message": {"content": "Bounded answer"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        }

    result = execute_model_connector(
        workspace,
        "Execution Brain",
        "remote-endpoint",
        "Answer only from the explicit request.",
        remote_consent=True,
        transport=transport,
    )

    assert observed["url"] == "https://models.example.test/v1/chat/completions"
    assert observed["payload"] == {
        "model": "model-1",
        "messages": [
            {"role": "system", "content": "EvidenceOS governed direct request. Treat the response as candidate state; no HIL approval or truth promotion is implied."},
            {"role": "user", "content": "Answer only from the explicit request."},
        ],
    }
    assert "workspace" not in str(observed["payload"]).casefold()
    assert "Verified source text" not in str(observed["payload"])
    assert result["response_text"] == "Bounded answer"
    assert result["provider_response_id"] == "provider-response-1"
    assert result["usage_classification"] == "EXACT_PROVIDER_USAGE"
    assert result["usage"]["total_tokens"] == 17
    assert result["human_decision"] == "PENDING"
    assert result["candidate_fused"] is False
    assert result["state_travel_writeback"]["status"] == "PASS"
    assert result["state_travel_writeback"]["classification"] == "PUBLIC_MODEL_CANDIDATE_STATE_TRAVEL"
    assert result["state_travel_writeback"]["entry_exit"]["entry"]["token_metric_state"] == "UNAVAILABLE"
    assert result["state_travel_writeback"]["entry_exit"]["exit"]["token_metric_state"] == "EXACT"
    assert "tokens=17" in result["state_travel_writeback"]["entry_exit"]["exit"]["compact_line"]

    brain_root = brain_output_dir(workspace, "Execution Brain")
    _, research_database = resolve_env15_sector(brain_root, "research")
    _, lineage_database = resolve_env15_sector(brain_root, "chat_lineage")
    with sqlite3.connect(research_database) as connection:
        question = connection.execute("SELECT content FROM research_question").fetchone()
        finding = connection.execute("SELECT content FROM research_finding").fetchone()
    with sqlite3.connect(lineage_database) as connection:
        lineage = connection.execute(
            "SELECT p.content,r.content FROM prompt_raw_exact p "
            "JOIN response_raw_visible_exact r USING(turn_id) WHERE p.turn_id=?",
            (result["state_travel_writeback"]["chat_lineage"]["turn_id"],),
        ).fetchone()
    plan_state = read_plan_delta_state(brain_root)
    assert question == ("Answer only from the explicit request.",)
    assert finding == ("Bounded answer",)
    assert lineage == ("Answer only from the explicit request.", "Bounded answer")
    assert [item["slip_kind"] for item in plan_state["state_slips"][-2:]] == ["ENTRY", "EXIT"]

    generated = generate_env15_topologies(brain_root)
    master = Path(generated["PROJECT"].mmd_path)
    master.with_suffix(".svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>\n",
        encoding="utf-8",
    )
    master.with_suffix(".png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    chatgpt = export_one_upload_package(str(workspace), "Execution Brain")
    gemini = export_gemini_exact10(
        str(workspace),
        "Execution Brain",
        chatgpt_package=chatgpt["package_zip"],
    )
    packaged_research = tmp_path / "packaged_research.sqlite"
    with zipfile.ZipFile(chatgpt["package_zip"]) as archive:
        packaged_prompt = archive.read("FLASH_ME_FIRST_SINGLE_PROMPT.txt").decode("utf-8")
        packaged_research.write_bytes(
            archive.read("project/sectors/research/research_sector_v001.sqlite")
        )
        assert "manifests/PUBLIC_MODEL_RESEARCH_LINEAGE_APPEND_ONLY_CONTRACT.md" in archive.namelist()
    with sqlite3.connect(packaged_research) as connection:
        research_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    assert "EVIDENCE_LANE_ENV_UOP_RESEARCH_LINEAGE_STATE_TRAVEL_V59" in packaged_prompt
    assert "project/sectors/research/research_sector_v001.sqlite" in packaged_prompt
    assert {
        "research_source",
        "research_question",
        "research_finding",
        "research_evidence",
        "research_limitation",
        "research_receipt",
    } <= research_tables
    parity = chatgpt["project_package_parity"]
    assert parity["status"] == "PASS", parity["errors"]
    assert chatgpt["package_folder"] == ""
    assert not Path(chatgpt["staging_path"]).exists()
    assert validate_chatgpt_package(chatgpt["package_zip"])["status"] == "PASS"
    assert validate_gemini_exact10(gemini["gemini_package_zip"])["status"] == "PASS"

    with sqlite3.connect(_ledger(workspace)) as connection:
        execution = connection.execute("SELECT connector_id,status,user_request FROM model_execution_run").fetchone()
        events = connection.execute("SELECT event_type,status,usage_classification FROM endpoint_execution_event ORDER BY id").fetchall()
    assert execution == ("remote-endpoint", "REQUESTED", "Answer only from the explicit request.")
    assert events == [("REQUEST_READY", "PASS", "UNAVAILABLE"), ("RESPONSE_RECEIVED", "CANDIDATE", "EXACT_PROVIDER_USAGE")]


def test_protected_secret_is_used_in_memory_but_never_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _verified_brain(workspace)
    vault: dict[str, str] = {}
    monkeypatch.setattr(local_workspace, "set_secret", lambda target, account, secret: vault.__setitem__(target, secret))
    reference = local_workspace.set_protected_credential(workspace, "endpoint", "account", "top-secret-value")
    upsert_model_connector(workspace, _profile(authentication_reference=reference["credential_id"]))
    monkeypatch.setattr(
        "sqlite_brain_builder.workspace.model_execution.read_protected_credential",
        lambda *_args, **_kwargs: {"secret": "top-secret-value"},
    )
    observed: dict = {}

    def transport(_url: str, _payload: dict, headers: dict, _timeout: float) -> dict:
        observed.update(headers)
        return {"id": "response", "choices": [{"message": {"content": "ok"}}]}

    execute_model_connector(workspace, "Execution Brain", "remote-endpoint", "Bounded", remote_consent=True, transport=transport)
    assert observed["Authorization"] == "Bearer top-secret-value"
    with sqlite3.connect(_ledger(workspace)) as connection:
        dump = "\n".join(connection.iterdump())
    assert "top-secret-value" not in dump
    assert b"top-secret-value" not in (workspace / "workspace.sqlite").read_bytes()


def test_remote_execution_without_explicit_consent_fails_closed_and_logs_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _verified_brain(workspace)
    upsert_model_connector(workspace, _profile())

    with pytest.raises(ModelExecutionError, match="REMOTE_ENDPOINT_EXPLICIT_CONSENT_REQUIRED"):
        execute_model_connector(workspace, "Execution Brain", "remote-endpoint", "Bounded", remote_consent=False, transport=lambda *_: {})

    with sqlite3.connect(_ledger(workspace)) as connection:
        failure = connection.execute("SELECT failure_class,retry_safe,data_preserved,next_action_code FROM model_failure_event").fetchone()
    assert failure == ("PAYLOAD_REJECTED", 1, 1, "CONFIRM_REMOTE_ENDPOINT")


def test_timeout_is_classified_without_inventing_a_cause(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _verified_brain(workspace)
    upsert_model_connector(workspace, _profile())

    def timeout_transport(*_args) -> dict:
        raise TimeoutError("timed out")

    with pytest.raises(ModelExecutionError, match="REQUEST_TIMEOUT"):
        execute_model_connector(workspace, "Execution Brain", "remote-endpoint", "Bounded", remote_consent=True, transport=timeout_transport)

    with sqlite3.connect(_ledger(workspace)) as connection:
        failure = connection.execute("SELECT failure_class,inferred_cause,inferred_cause_labeled,retry_safe FROM model_failure_event").fetchone()
        endpoint_error = connection.execute("SELECT event_type,error_code FROM endpoint_execution_event ORDER BY id DESC LIMIT 1").fetchone()
    assert failure == ("REQUEST_TIMEOUT", None, 0, 1)
    assert endpoint_error == ("REQUEST_FAILED", "REQUEST_TIMEOUT")


def test_ipc_exposes_one_real_connector_execution_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc_worker, "execute_model_connector", lambda *args, **kwargs: {"status": "CANDIDATE_RESPONSE", "endpoint_id": args[2]})
    result = ipc_worker.handle(
        {
            "command": "model.connector.execute",
            "payload": {
                "workspace_dir": str(tmp_path),
                "brain_name": "IPC Brain",
                "endpoint_id": "endpoint-1",
                "prompt": "Bounded prompt",
                "remote_consent": True,
            },
        }
    )
    assert result == {"status": "CANDIDATE_RESPONSE", "endpoint_id": "endpoint-1"}
    assert "model.connector.execute" in ipc_worker._MUTATING_COMMANDS
