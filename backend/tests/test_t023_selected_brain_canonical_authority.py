from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def _setting(workspace: Path, key: str) -> object:
    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        row = connection.execute(
            "SELECT value FROM workspace_settings WHERE key=?", (key,)
        ).fetchone()
    finally:
        connection.close()
    return json.loads(row[0]) if row else None


def test_selection_commit_binds_one_canonical_context_and_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Alpha")
    create_brain(workspace, "Beta")

    selected = ipc_worker.handle(
        {
            "command": "brain.select",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Beta",
                "selection_display_name": "Beta",
                "selection_persistence_workspace_dir": str(workspace),
                "selection_request_id": "ui-selection-beta-1",
            },
        }
    )

    authority = selected["selection_authority"]
    commit = selected["selection_commit"]
    persistence = selected["selection_persistence"]
    assert selected["context_schema"] == "T023_SELECTED_BRAIN_CONTEXT_V1"
    assert authority["schema"] == "T023_CANONICAL_SELECTED_BRAIN_AUTHORITY_V1"
    assert authority["brain_id"] == selected["brain_identity"]["brain_id"]
    assert authority["brain_name"] == selected["brain_identity"]["brain_name"] == "Beta"
    assert authority["workspace_dir"] == selected["summary"]["workspace_dir"]
    assert authority["output_dir"] == selected["brain_identity"]["output_dir"]
    assert commit["selection_request_id"] == "ui-selection-beta-1"
    assert commit["status"] == "COMMITTED"
    assert commit["context_sha256"] == selected["context_sha256"]
    assert commit["brain_id"] == authority["brain_id"]
    assert commit["brain_name"] == authority["brain_name"]
    assert persistence["mode"] == "USER_SELECTION_PERSISTED"
    assert _setting(workspace, ipc_worker.LAST_SELECTED_BRAIN_SETTINGS_KEY) == "Beta"

    receipt = commit["receipt"]
    receipt_path = Path(receipt["receipt_path"])
    assert receipt_path.is_file()
    receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    stored_hash = receipt_payload.pop("receipt_sha256")
    canonical_hash = hashlib.sha256(
        json.dumps(
            receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert receipt["receipt_sha256"] == stored_hash == canonical_hash
    assert receipt_payload["detail"]["selection_request_id"] == "ui-selection-beta-1"
    assert receipt_payload["detail"]["brain_id"] == authority["brain_id"]
    assert receipt_payload["detail"]["context_sha256"] == selected["context_sha256"]


def test_failed_and_restore_only_selection_preserve_last_committed_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, "Alpha")
    create_brain(workspace, "Beta")

    ipc_worker.handle(
        {
            "command": "brain.select",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Beta",
                "selection_request_id": "ui-selection-beta-committed",
            },
        }
    )
    with pytest.raises(ValueError, match="BRAIN_NOT_REGISTERED_OR_OUTPUT_MISSING:Missing"):
        ipc_worker.handle(
            {
                "command": "brain.select",
                "payload": {
                    "workspace_dir": str(workspace),
                    "brain_name": "Missing",
                    "selection_request_id": "ui-selection-missing",
                },
            }
        )
    assert _setting(workspace, ipc_worker.LAST_SELECTED_BRAIN_SETTINGS_KEY) == "Beta"

    restored = ipc_worker.handle(
        {
            "command": "brain.select",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": "Alpha",
                "selection_display_name": "Alpha",
                "selection_persistence_workspace_dir": str(workspace),
                "selection_restore_only": True,
                "selection_request_id": "ui-selection-alpha-restore",
            },
        }
    )
    assert restored["selection_commit"]["status"] == "RESTORED"
    assert restored["selection_commit"]["receipt"] is None
    assert restored["selection_authority"]["brain_name"] == "Alpha"
    assert _setting(workspace, ipc_worker.LAST_SELECTED_BRAIN_SETTINGS_KEY) == "Beta"
