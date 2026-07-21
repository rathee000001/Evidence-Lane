from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.workspace import ollama_integration


class _Response:
    def __init__(self, payload: dict) -> None:
        self._stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_ollama_settings_require_loopback_and_existing_explicit_launcher(tmp_path: Path) -> None:
    defaults = ollama_integration.get_ollama_settings(tmp_path)
    assert defaults == {
        "ollama_base_url": "http://127.0.0.1:11434",
        "ollama_executable_override": None,
    }

    with pytest.raises(ollama_integration.OllamaIntegrationError, match="OLLAMA_BASE_URL_MUST_BE_LOOPBACK"):
        ollama_integration.update_ollama_settings(tmp_path, {"ollama_base_url": "https://remote.example.test"})
    with pytest.raises(ollama_integration.OllamaIntegrationError, match="OLLAMA_CONFIGURED_PATH_NOT_FOUND"):
        ollama_integration.update_ollama_settings(tmp_path, {"ollama_executable_override": str(tmp_path / "missing.exe")})

    launcher = tmp_path / "Ollama.lnk"
    launcher.write_bytes(b"shortcut-placeholder")
    updated = ollama_integration.update_ollama_settings(
        tmp_path,
        {
            "ollama_base_url": "http://localhost:11434/",
            "ollama_executable_override": str(launcher),
        },
    )
    assert updated["ollama_base_url"] == "http://localhost:11434"
    assert updated["ollama_executable_override"] == str(launcher.resolve())


def test_discovery_prefers_configured_override_and_refresh_reuses_tool_registry(tmp_path: Path) -> None:
    launcher = tmp_path / "Ollama app.exe"
    launcher.write_bytes(b"binary-placeholder")
    ollama_integration.update_ollama_settings(
        tmp_path, {"ollama_executable_override": str(launcher)}
    )
    discovered = ollama_integration.discover_ollama(tmp_path)
    refreshed = ollama_integration.refresh_ollama_registry(tmp_path)

    assert discovered["status"] == "AVAILABLE"
    assert discovered["source"] == "CONFIGURED_OVERRIDE"
    assert discovered["path"] == str(launcher.resolve())
    assert refreshed["registry"] == "fallback_tool_registry"
    with sqlite3.connect(tmp_path / "workspace.sqlite") as connection:
        row = connection.execute(
            "SELECT primary_tool,detected_path,status FROM fallback_tool_registry "
            "WHERE tool_category='OLLAMA_APPLICATION'"
        ).fetchone()
        receipts = connection.execute(
            "SELECT COUNT(*) FROM local_operation_receipt WHERE operation='ollama.registry.refresh'"
        ).fetchone()[0]
    assert row == ("Ollama", str(launcher.resolve()), "AVAILABLE")
    assert receipts == 1


def test_discovery_uses_start_menu_without_hardcoded_username(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    programs = tmp_path / "Programs"
    programs.mkdir()
    shortcut = programs / "Ollama.lnk"
    shortcut.write_bytes(b"shortcut-placeholder")
    monkeypatch.setattr(ollama_integration, "_start_menu_roots", lambda: [programs])
    monkeypatch.setattr(ollama_integration, "_registered_app_paths", lambda: [])
    monkeypatch.setattr(ollama_integration.shutil, "which", lambda _name: None)

    discovered = ollama_integration.discover_ollama(tmp_path)
    assert discovered["source"] == "START_MENU"
    assert discovered["path"] == str(shortcut.resolve())


def test_launch_is_explicit_records_receipt_and_never_passes_project_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "Ollama.lnk"
    launcher.write_bytes(b"shortcut-placeholder")
    ollama_integration.update_ollama_settings(
        tmp_path, {"ollama_executable_override": str(launcher)}
    )
    opened: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        ollama_integration,
        "_start_application",
        lambda path: opened.append((str(path),)),
    )

    result = ollama_integration.launch_ollama(tmp_path)
    assert result["status"] == "LAUNCHED"
    assert opened == [(str(launcher.resolve()),)]
    with sqlite3.connect(tmp_path / "workspace.sqlite") as connection:
        detail = connection.execute(
            "SELECT detail_json FROM local_operation_receipt WHERE operation='ollama.launch'"
        ).fetchone()[0]
    receipt_detail = json.loads(detail)
    assert receipt_detail["project_data_sent"] is False
    assert set(receipt_detail) == {"path", "source", "project_data_sent", "silent_install_started"}


def test_not_installed_returns_consent_safe_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_integration, "_start_menu_roots", lambda: [])
    monkeypatch.setattr(ollama_integration, "_registered_app_paths", lambda: [])
    monkeypatch.setattr(ollama_integration.shutil, "which", lambda _name: None)
    status = ollama_integration.discover_ollama(tmp_path)
    launch = ollama_integration.launch_ollama(tmp_path)
    assert status["status"] == launch["status"] == "OLLAMA_NOT_INSTALLED"
    assert launch["actions"] == [
        "INSTALL_OLLAMA",
        "SHOW_INSTALLATION_GUIDANCE",
        "CONFIGURE_EXISTING_PATH",
        "SKIP_FOR_NOW",
    ]
    assert launch["silent_install_started"] is False


def test_local_api_models_is_loopback_only_bounded_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_urlopen(request, timeout: float):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return _Response(
            {
                "models": [
                    {
                        "name": "deepseek-r1:8b",
                        "model": "deepseek-r1:8b",
                        "modified_at": "2026-07-13T00:00:00Z",
                        "size": 4_000_000_000,
                        "digest": "A" * 64,
                        "details": {"family": "qwen2", "parameter_size": "8B"},
                    }
                ]
            }
        )

    monkeypatch.setattr(ollama_integration, "urlopen", fake_urlopen)
    result = ollama_integration.get_ollama_models(tmp_path)
    assert observed == {"url": "http://127.0.0.1:11434/api/tags", "timeout": 3.0}
    assert result["status"] == "AVAILABLE"
    assert result["models"][0]["name"] == "deepseek-r1:8b"
    assert set(result["models"][0]) == {"name", "model", "modified_at", "size", "digest", "details"}


def test_ipc_exposes_ollama_commands_and_single_mutation_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "Ollama.lnk"
    launcher.write_bytes(b"shortcut-placeholder")
    configured = ipc_worker.handle(
        {
            "command": "ollama.settings.update",
            "payload": {
                "workspace_dir": str(tmp_path),
                "values": {"ollama_executable_override": str(launcher)},
            },
        }
    )
    status = ipc_worker.handle(
        {"command": "ollama.status", "payload": {"workspace_dir": str(tmp_path)}}
    )
    monkeypatch.setattr(ollama_integration, "_start_application", lambda _path: None)
    monkeypatch.setattr(ipc_worker, "launch_ollama", ollama_integration.launch_ollama)
    launched = ipc_worker.handle(
        {"command": "ollama.launch", "payload": {"workspace_dir": str(tmp_path)}}
    )

    assert configured["ollama_executable_override"] == str(launcher.resolve())
    assert status["launcher"]["status"] == "AVAILABLE"
    assert launched["status"] == "LAUNCHED"
    assert {
        "ollama.settings.update",
        "ollama.registry.refresh",
        "ollama.launch",
    } <= ipc_worker._MUTATING_COMMANDS
