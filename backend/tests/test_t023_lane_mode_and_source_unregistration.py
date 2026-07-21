from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def test_local_and_github_code_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('mode truth')\n", encoding="utf-8")
    init_workspace(workspace)
    create_brain(workspace, "Mode Brain")

    with pytest.raises(
        ipc_worker.WorkerError,
        match="LOCAL_AND_GITHUB_CODE_LANES_ARE_MUTUALLY_EXCLUSIVE_ONE_PROJECT_DATABASE",
    ):
        ipc_worker._validate_build_sources(
            workspace,
            "Mode Brain",
            [
                {
                    "source_id": "local",
                    "lane_key": "local_code",
                    "path": str(source),
                    "active": True,
                },
                {
                    "source_id": "github",
                    "lane_key": "github_code",
                    "path": str(source),
                    "active": True,
                    "metadata": {"repo_url": "https://example.test/repo.git"},
                },
            ],
        )


def test_unregistering_source_preserves_built_sector_bytes_for_refresh(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('preserve bytes')\n", encoding="utf-8")
    init_workspace(workspace)
    create_brain(workspace, "Preserve Brain")

    added = ipc_worker.handle(
        {
            "command": "sources.add",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Preserve Brain",
                "lane_key": "local_code",
                "path": str(source),
            },
        }
    )
    removed = ipc_worker.handle(
        {
            "command": "sources.remove",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Preserve Brain",
                "source_id": added["source"]["source_id"],
            },
        }
    )

    assert removed["activity"]["status"] == "SOURCE_UNREGISTERED_SECTOR_BYTES_PRESERVED"
    assert removed["activity"]["verified_brain_mutated"] is False
    receipt = json.loads(
        Path(removed["receipt"]["receipt_path"]).read_text(encoding="utf-8")
    )
    assert (
        receipt["detail"]["next_build_action"]
        == "SKIPPED_NO_SOURCE_PRESERVE_PRIOR_SECTOR_BYTES"
    )
