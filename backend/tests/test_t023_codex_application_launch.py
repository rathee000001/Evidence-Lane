from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.workspace import codex_application


def test_codex_route_is_fixed_automatic_windows_discovery(tmp_path: Path) -> None:
    assert codex_application.get_codex_settings(tmp_path) == {
        "discovery_mode": "WINDOWS_AUTOMATIC",
        "manual_route_allowed": False,
        "codex_application_id": codex_application.DEFAULT_CODEX_APP_ID,
        "codex_executable_override": None,
    }

    with pytest.raises(
        codex_application.CodexApplicationError,
        match="CODEX_ROUTE_IS_AUTOMATIC_WINDOWS_DISCOVERY",
    ):
        codex_application.update_codex_settings(
            tmp_path,
            {
                "codex_application_id": "OpenAI.Codex_test!App",
                "codex_executable_override": str(tmp_path / "Codex.exe"),
            },
        )


def test_codex_launch_uses_automatic_app_package_and_writes_a_secret_free_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[dict[str, str | None]] = []
    monkeypatch.setattr(codex_application, "_app_package_available", lambda _app_id: True)
    monkeypatch.setattr(
        codex_application,
        "_start_application",
        lambda *, path, app_id: opened.append({"path": path, "app_id": app_id}),
    )

    result = codex_application.launch_codex(tmp_path)

    assert result == {
        "status": "LAUNCHED",
        "source": "WINDOWS_APP_PACKAGE",
        "path": None,
        "app_id": codex_application.DEFAULT_CODEX_APP_ID,
        "project_data_sent": False,
        "silent_install_started": False,
        "foreground_transfer_requested": True,
    }
    assert opened == [{"path": None, "app_id": codex_application.DEFAULT_CODEX_APP_ID}]
    with sqlite3.connect(tmp_path / "workspace.sqlite") as connection:
        detail = json.loads(
            connection.execute(
                "SELECT detail_json FROM local_operation_receipt WHERE operation='codex.launch'"
            ).fetchone()[0]
        )
    assert detail == {
        "app_id": codex_application.DEFAULT_CODEX_APP_ID,
        "foreground_transfer_requested": True,
        "path": None,
        "project_data_sent": False,
        "silent_install_started": False,
        "source": "WINDOWS_APP_PACKAGE",
    }


def test_codex_unavailable_falls_back_without_launch_or_silent_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_application, "_app_package_available", lambda _app_id: False)
    monkeypatch.setattr(
        codex_application,
        "_start_application",
        lambda **_: pytest.fail("unavailable Codex must not be launched"),
    )

    result = codex_application.launch_codex(tmp_path)

    assert result["status"] == "CODEX_APPLICATION_NOT_AVAILABLE"
    assert result["actions"] == ["INSTALL_CODEX", "SKIP_FOR_NOW"]
    assert result["project_data_sent"] is False
    assert result["silent_install_started"] is False
    assert result["foreground_transfer_requested"] is False


def test_ipc_rejects_manual_codex_routes_and_keeps_automatic_launch_linear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(
        codex_application.CodexApplicationError,
        match="CODEX_ROUTE_IS_AUTOMATIC_WINDOWS_DISCOVERY",
    ):
        ipc_worker.handle(
            {
                "command": "codex.settings.update",
                "payload": {
                    "workspace_dir": str(tmp_path),
                    "values": {"codex_executable_override": str(tmp_path / "Codex.exe")},
                },
            }
        )

    monkeypatch.setattr(codex_application, "_app_package_available", lambda _app_id: True)
    monkeypatch.setattr(codex_application, "_start_application", lambda **_: None)
    monkeypatch.setattr(ipc_worker, "get_codex_status", codex_application.get_codex_status)
    monkeypatch.setattr(ipc_worker, "launch_codex", codex_application.launch_codex)
    status = ipc_worker.handle(
        {"command": "codex.status", "payload": {"workspace_dir": str(tmp_path)}}
    )
    launched = ipc_worker.handle(
        {"command": "codex.launch", "payload": {"workspace_dir": str(tmp_path)}}
    )

    assert status["settings"]["manual_route_allowed"] is False
    assert status["launcher"]["status"] == "AVAILABLE"
    assert launched["status"] == "LAUNCHED"
    assert "codex.settings.update" not in ipc_worker._MUTATING_COMMANDS
    assert "codex.launch" in ipc_worker._MUTATING_COMMANDS
