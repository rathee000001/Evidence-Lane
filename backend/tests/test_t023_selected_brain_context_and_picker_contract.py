from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.brain_versions import capture_brain_version
from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.pipeline_state import PipelineStateStore
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


WORKSPACE = Path(os.environ.get("EVIDENCE_LANE_LEGACY_WORKSPACE") or "__legacy_workspace_not_configured__")
LAB = Path(os.environ.get("EVIDENCE_LANE_UIUX_LAB") or "__uiux_lab_not_configured__")


def _request(command: str, workspace: Path, **payload: object) -> object:
    return ipc_worker.handle(
        {
            "id": f"selected-brain-{command}",
            "command": command,
            "payload": {"workspace_dir": str(workspace), **payload},
        }
    )


def _seed_two_brains(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "evidence-root"
    init_workspace(workspace)
    create_brain(workspace, "ril")
    create_brain(workspace, "Other Brain")
    ril_code = tmp_path / "uiux_lab"
    other_code = tmp_path / "other_code"
    ril_code.mkdir()
    other_code.mkdir()
    (ril_code / "ril.ts").write_text("export const brain = 'ril';\n", encoding="utf-8")
    (other_code / "other.ts").write_text("export const brain = 'other';\n", encoding="utf-8")
    _request("sources.add", workspace, brain_name="ril", lane_key="local_code", path=str(ril_code))
    _request("sources.add", workspace, brain_name="Other Brain", lane_key="local_code", path=str(other_code))
    return workspace, ril_code, other_code


def test_brain_select_returns_one_complete_atomic_context_without_cross_brain_residue(tmp_path: Path) -> None:
    workspace, ril_code, other_code = _seed_two_brains(tmp_path)
    ril_schema = "ril_document\nril_chunk\nril_search_fts"
    other_schema = "other_document\nother_chunk\nother_search_fts"
    _request("source.schema.update", workspace, brain_name="ril", lane_key="docs", schema_contract=ril_schema)
    _request(
        "source.schema.update",
        workspace,
        brain_name="Other Brain",
        lane_key="docs",
        schema_contract=other_schema,
    )

    ril = _request("brain.select", workspace, brain_name="ril")
    other = _request("brain.select", workspace, brain_name="Other Brain")

    required = {
        "context_schema",
        "context_sha256",
        "generation_token",
        "brain_identity",
        "summary",
        "sources",
        "lane_schemas",
        "lane_definitions",
        "versions",
        "output_route",
        "packages",
        "refresh_attention",
        "refresh_candidate",
        "model_output_state",
        "historical_provider_delta_archive",
        "pipeline_state",
    }
    assert required <= ril.keys()
    assert required <= other.keys()
    assert "model_delta_state" not in ril
    assert "model_delta_state" not in other
    assert ril["context_schema"] == "T023_SELECTED_BRAIN_CONTEXT_V1"
    assert ril["brain_identity"]["brain_name"] == "ril"
    assert other["brain_identity"]["brain_name"] == "Other Brain"
    assert re.fullmatch(r"[0-9a-f]{64}", ril["context_sha256"])
    assert ril["generation_token"] == ril["context_sha256"]
    assert ril["summary"]["brain_name"] == "ril"
    assert {item["path"] for item in ril["sources"]} == {str(ril_code)}
    assert {item["path"] for item in other["sources"]} == {str(other_code)}
    assert ril["lane_schemas"]["docs"]["schema_contract"] == ril_schema
    assert other["lane_schemas"]["docs"]["schema_contract"] == other_schema
    assert str(other_code) not in str(ril)
    assert str(ril_code) not in str(other)
    assert ril["context_sha256"] != other["context_sha256"]


def test_brain_output_route_inspection_is_selected_brain_scoped_and_read_only(tmp_path: Path) -> None:
    workspace, _ril_code, _other_code = _seed_two_brains(tmp_path)

    route = _request("brain.outputRoute.inspect", workspace, brain_name="ril")

    assert route["schema"] == "T023_SELECTED_BRAIN_OUTPUT_ROUTE_V1"
    assert route["read_only"] is True
    assert route["selected_brain_only"] is True
    assert route["brain_name"] == "ril"
    assert Path(route["output_dir"]) == brain_output_dir(workspace, "ril").resolve()
    assert Path(route["packages_dir"]) == brain_output_dir(workspace, "ril").resolve() / "packages"
    assert route["packages_dir_exists"] is False
    assert not Path(route["packages_dir"]).exists()


def test_brain_select_restores_last_persisted_pipeline_and_all_ten_stage_states(tmp_path: Path) -> None:
    workspace, _ril_code, _other_code = _seed_two_brains(tmp_path)
    state_path = brain_output_dir(workspace, "ril") / "project" / "runtime" / "pipeline_state.json"
    store = PipelineStateStore(state_path)
    store.start(
        brain_name="ril",
        workspace_dir=str(workspace),
        files_total=3,
        active_command="validate and register sources",
    )
    terminal = store.complete(
        files_done=3,
        rows_written=41,
        chunks_written=17,
        active_command="immutable version captured",
    )

    context = _request("brain.select", workspace, brain_name="ril")

    assert context["pipeline_state"]["restored_from_disk"] is True
    assert context["pipeline_state"]["pipeline"] == terminal
    assert context["pipeline_state"]["pipeline"]["status"] == "completed"
    assert context["pipeline_state"]["pipeline"]["global_percent"] == 100.0
    assert len(context["pipeline_state"]["history"]) == 2
    assert len(context["pipeline_state"]["stages"]) == 10
    assert {row["status"] for row in context["pipeline_state"]["stages"]} == {"completed"}


def test_lane_schema_override_is_scoped_by_immutable_brain_identity(tmp_path: Path) -> None:
    workspace, _ril_code, _other_code = _seed_two_brains(tmp_path)
    ril = _request(
        "source.schema.update",
        workspace,
        brain_name="ril",
        lane_key="analysis",
        schema_contract="ril_case\nril_claim\nril_search_fts",
    )
    other_default = _request(
        "source.schema.get", workspace, brain_name="Other Brain", lane_key="analysis"
    )
    other = _request(
        "source.schema.update",
        workspace,
        brain_name="Other Brain",
        lane_key="analysis",
        schema_contract="other_case\nother_claim\nother_search_fts",
    )
    ril_again = _request("source.schema.get", workspace, brain_name="ril", lane_key="analysis")

    assert ril["schema"] == ["ril_case", "ril_claim", "ril_search_fts"]
    assert other_default["origin"] == "built_in_default"
    assert other["schema"] == ["other_case", "other_claim", "other_search_fts"]
    assert ril_again["schema_sha256"] == ril["schema_sha256"]
    assert ril_again["schema_sha256"] != other["schema_sha256"]


def test_context_hash_is_stable_and_only_matching_brain_arms_read_only_attention(tmp_path: Path) -> None:
    workspace, _ril_code, _other_code = _seed_two_brains(tmp_path)
    for brain_name in ("ril", "Other Brain"):
        sources = _request("brain.select", workspace, brain_name=brain_name)["sources"]
        build_brain(str(workspace), brain_name, sources, generate_mmd=False)
        capture_brain_version(
            workspace,
            brain_name,
            actor_type="app",
            actor_name="selected-brain context test",
            reason="verified source-attention baseline",
        )
    first = _request("brain.select", workspace, brain_name="ril")
    replay = _request("brain.select", workspace, brain_name="ril")
    other_before = _request("brain.select", workspace, brain_name="Other Brain")
    source = tmp_path / "ril-note.md"
    source.write_text("# New governed source\n", encoding="utf-8")
    _request("sources.add", workspace, brain_name="ril", lane_key="docs", path=str(source))
    changed = _request("brain.select", workspace, brain_name="ril")
    other_after = _request("brain.select", workspace, brain_name="Other Brain")

    assert replay["context_sha256"] == first["context_sha256"]
    assert changed["context_sha256"] != first["context_sha256"]
    assert changed["refresh_attention"]["self_active"] is True
    assert changed["refresh_attention"]["read_only"] is True
    assert changed["refresh_attention"]["automatic_refresh"] is False
    assert changed["refresh_attention"]["automatic_fuse"] is False
    assert other_after["context_sha256"] == other_before["context_sha256"]
    assert other_after["refresh_attention"]["self_active"] is False


def test_lane_definitions_expose_exact_native_picker_descriptors_from_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "evidence-root"
    init_workspace(workspace)
    create_brain(workspace, "Picker Brain")
    lanes = _request("lanes.defs", workspace, brain_name="Picker Brain")["lanes"]

    assert lanes["local_code"]["picker"] == {
        "mode": "directory",
        "multiple": False,
        "exclude_accept_all_option": True,
        "types": [],
    }
    chat = lanes["chat_lineage"]["picker"]
    assert chat["mode"] == "file"
    assert chat["multiple"] is True
    assert chat["exclude_accept_all_option"] is True
    assert chat["types"] == [
        {
            "description": "Chat Lineage files",
            "accept": {
                "application/octet-stream": [".docx", ".json", ".jsonl", ".md", ".txt", ".zip"]
            },
        }
    ]
    assert "Custom" not in str(chat)
    assert "*" not in str(chat["types"])
    assert lanes["pdf_ocr"]["picker"]["types"][0]["accept"] == {
        "application/pdf": [".pdf"]
    }
    assert lanes["custom"]["canonical_lane_id"] == "custom"


def test_clean_frontend_uses_race_safe_single_context_commit_and_file_system_access_api() -> None:
    if not (WORKSPACE / "evidence-os-clean-desktop" / "src").is_dir():
        pytest.skip("set EVIDENCE_LANE_LEGACY_WORKSPACE to run the legacy-source audit")
    app = (WORKSPACE / "evidence-os-clean-desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    backend = (WORKSPACE / "evidence-os-clean-desktop" / "src" / "backend.ts").read_text(encoding="utf-8")
    intake = (WORKSPACE / "evidence-os-clean-desktop" / "src" / "SourceIntake.tsx").read_text(encoding="utf-8")
    types = (WORKSPACE / "evidence-os-clean-desktop" / "src" / "types.ts").read_text(encoding="utf-8")

    assert "BrainContextSnapshot" in types
    assert "brainSelectionGenerationRef" in app
    assert "setBrainContext(" in app
    assert "generation !== brainSelectionGenerationRef.current" in app
    assert 'callWorker<BrainContextSnapshot>("brain.select"' in app
    assert "showOpenFilePicker" in backend
    assert "showDirectoryPicker" in backend
    assert "excludeAcceptAllOption: true" in backend
    assert "pickerDescriptor" in intake
    assert 'choosePaths("file", true, lane.pickerDescriptor)' in intake


def test_accepted_lab_simulation_has_one_context_transition_and_native_source_picker() -> None:
    if not LAB.is_dir():
        pytest.skip("set EVIDENCE_LANE_UIUX_LAB to run the accepted-lab audit")
    relative = Path("src/components/EvidenceOSCockpitSimulation.tsx")
    lab = (LAB / relative).read_text(encoding="utf-8")

    assert "selectedBrainContext" in lab
    assert "brainSelectionGenerationRef" in lab
    assert "commitSelectedBrainContext" in lab
    assert "sourceEntries" not in lab
    assert "setSelectedBrain(chat.brain)" not in lab
    assert "sourceFileInputRef" not in lab
    assert "sourceFolderInputRef" not in lab
    assert "deltaFileInputRef" not in lab
    assert 'execute("source.path.choose"' in lab
    assert "Open Classified Files" in lab
    assert "Open Folder" in lab
