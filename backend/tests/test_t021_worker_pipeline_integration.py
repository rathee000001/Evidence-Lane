from __future__ import annotations

import os
from pathlib import Path

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.runtime.pipeline_state import PIPELINE_STAGES, PipelineStateStore
from sqlite_brain_builder.runtime.process_registry import GPU_ATTRIBUTION_UNAVAILABLE


def test_build_all_persists_every_real_stage_and_process_scoped_metrics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Prompt\nCanonical backend build.\n", encoding="utf-8")

    monkeypatch.setattr(
        ipc_worker,
        "render_topology",
        lambda *_: {"rendered": [{"status": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS"}]},
    )
    monkeypatch.setattr(
        ipc_worker,
        "materialize_schema_only_package_topology",
        lambda *_: {
            "status": "PASS",
            "mmd_files": [],
            "rendered": [
                {"status": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS"},
                {"status": "SQLITE_DERIVED_ENV_MMD_TO_SVG_TO_PNG_PASS"},
                {"status": "SQLITE_DERIVED_UOP_MMD_TO_SVG_TO_PNG_PASS"},
            ],
        },
    )
    monkeypatch.setattr(
        ipc_worker,
        "export_one_upload_package",
        lambda *_: {"validation": {"status": "PASS", "errors": []}},
    )
    monkeypatch.setattr(
        ipc_worker,
        "export_gemini_exact10",
        lambda *_: {"validation": {"status": "PASS", "errors": []}},
    )

    result = ipc_worker.handle(
        {
            "id": "pipeline-integration",
            "command": "brain.buildAll",
            "payload": {
                "workspace_dir": str(tmp_path),
                "brain_name": "Pipeline Brain",
                "sources": [
                    {
                        "source_id": "source-docs-fixed",
                        "lane_key": "docs",
                        "display_name": source.name,
                        "path": str(source),
                        "source_type": "document",
                        "active": True,
                    }
                ],
            },
        }
    )

    assert result["pipeline"]["status"] == "completed"
    assert result["pipeline"]["stage_count"] == 10
    assert result["pipeline"]["global_percent"] == 100.0
    assert result["pipeline"]["process_pid"] == os.getpid()
    assert result["pipeline"]["request_id"] == "pipeline-integration"
    assert result["pipeline"]["brain_name"] == "Pipeline Brain"
    assert result["pipeline"]["workspace_dir"] == str(tmp_path.resolve())
    assert result["pipeline"]["rows_written"] > 0

    state_path = Path(result["build"]["brain_root"]) / "project" / "runtime" / "pipeline_state.json"
    history = PipelineStateStore(state_path).history()
    observed_stage_order = []
    for event in history:
        if event["stage_id"] not in observed_stage_order:
            observed_stage_order.append(event["stage_id"])
    assert observed_stage_order == [stage.stage_id for stage in PIPELINE_STAGES]

    metrics = result["process_metrics"]
    assert os.getpid() in metrics["registered_pids"]
    assert set(metrics["sampled_pids"]).issuperset({os.getpid()})
    assert metrics["gpu_attribution_status"] == GPU_ATTRIBUTION_UNAVAILABLE

    snapshot = ipc_worker.handle({
        "command": "runtime.snapshot",
        "payload": {"workspace_dir": str(tmp_path), "brain_name": "Pipeline Brain"},
    })
    assert len(snapshot["pipeline"]["stages"]) == 10
    assert all(stage["status"] == "completed" for stage in snapshot["pipeline"]["stages"])
    assert all(stage["stage_percent"] == 100.0 for stage in snapshot["pipeline"]["stages"])
