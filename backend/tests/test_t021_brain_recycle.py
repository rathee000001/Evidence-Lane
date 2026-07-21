from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import brain_delete
from sqlite_brain_builder.brain_delete import BrainRecycleError, recycle_registered_brain
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def _mock_recycle(path: str) -> None:
    shutil.rmtree(Path(path))


def test_successful_recycle_updates_registry_after_success_and_writes_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    keep = create_brain(workspace, "Keep Brain")
    target = create_brain(workspace, "Delete Brain")
    Path(target["output_dir"], "evidence.txt").write_text("preserve in recycle mock", encoding="utf-8")

    result = recycle_registered_brain(
        workspace, "Delete Brain", confirm_brain_name="Delete Brain", trash=_mock_recycle
    )

    assert result["status"] == "PASS"
    assert result["next_brain_name"] == "Keep Brain"
    assert result["canonical_folder"] == str(Path(target["output_dir"]).resolve())
    assert not Path(target["output_dir"]).exists()
    assert Path(result["receipt_path"]).is_file()
    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        assert connection.execute(
            "SELECT status FROM brain_project WHERE brain_name='Delete Brain'"
        ).fetchone()[0] == "RECYCLED"
    finally:
        connection.close()


def test_failed_recycle_preserves_files_and_registry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    target = create_brain(workspace, "Delete Brain")

    def fail(_: str) -> None:
        raise OSError("fixture failure")

    with pytest.raises(BrainRecycleError, match="BRAIN_RECYCLE_FAILED"):
        recycle_registered_brain(workspace, "Delete Brain", confirm_brain_name="Delete Brain", trash=fail)
    assert Path(target["output_dir"]).is_dir()
    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        assert connection.execute(
            "SELECT status FROM brain_project WHERE brain_name='Delete Brain'"
        ).fetchone()[0] == "ACTIVE"
    finally:
        connection.close()


def test_confirmation_path_escape_and_symlink_are_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    target = create_brain(workspace, "Delete Brain")
    with pytest.raises(BrainRecycleError, match="EXPLICIT_NAME_CONFIRMATION"):
        recycle_registered_brain(workspace, "Delete Brain", confirm_brain_name="wrong", trash=_mock_recycle)

    connection = sqlite3.connect(workspace / "workspace.sqlite")
    outside = tmp_path / "outside"
    outside.mkdir()
    connection.execute(
        "UPDATE brain_project SET output_dir=? WHERE brain_name='Delete Brain'", (str(outside),)
    )
    connection.commit()
    connection.close()
    with pytest.raises(BrainRecycleError, match="OUTPUT_MISMATCH"):
        recycle_registered_brain(workspace, "Delete Brain", confirm_brain_name="Delete Brain", trash=_mock_recycle)

    connection = sqlite3.connect(workspace / "workspace.sqlite")
    connection.execute(
        "UPDATE brain_project SET output_dir=? WHERE brain_name='Delete Brain'", (target["output_dir"],)
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(brain_delete, "_has_symlink_component", lambda *_: True)
    with pytest.raises(BrainRecycleError, match="SYMLINK_OR_JUNCTION"):
        recycle_registered_brain(workspace, "Delete Brain", confirm_brain_name="Delete Brain", trash=_mock_recycle)
