from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from sqlite_brain_builder.core import now
from sqlite_brain_builder.runtime.path_policy import normalize_workspace_dir, workspace_db_path
from sqlite_brain_builder.storage.sqlite_utils import connect
from sqlite_brain_builder.workspace.local_workspace import (
    DEFAULT_IDENTITY_ID,
    ensure_local_workspace,
    get_settings,
    update_settings,
)


class OllamaIntegrationError(RuntimeError):
    pass


NOT_INSTALLED_ACTIONS = [
    "INSTALL_OLLAMA",
    "SHOW_INSTALLATION_GUIDANCE",
    "CONFIGURE_EXISTING_PATH",
    "SKIP_FOR_NOW",
]
_SETTINGS_KEYS = {"ollama_executable_override", "ollama_base_url"}
_MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_MODELS = 500
_MODEL_FIELDS = {"name", "model", "modified_at", "size", "digest"}
_MODEL_DETAIL_FIELDS = {
    "parent_model",
    "format",
    "family",
    "families",
    "parameter_size",
    "quantization_level",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: object) -> str:
    token = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]}"


def _loopback_base_url(value: str) -> str:
    raw = str(value or "").strip()
    split = urlsplit(raw)
    if split.scheme not in {"http", "https"} or not split.hostname:
        raise OllamaIntegrationError("OLLAMA_BASE_URL_MUST_BE_LOOPBACK")
    hostname = split.hostname.casefold()
    is_loopback = hostname == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback or split.username or split.password or split.query or split.fragment:
        raise OllamaIntegrationError("OLLAMA_BASE_URL_MUST_BE_LOOPBACK")
    if split.path not in {"", "/"}:
        raise OllamaIntegrationError("OLLAMA_BASE_URL_PATH_FORBIDDEN")
    try:
        _ = split.port
    except ValueError as exc:
        raise OllamaIntegrationError("OLLAMA_BASE_URL_PORT_INVALID") from exc
    return urlunsplit((split.scheme, split.netloc, "", "", ""))


def _launcher_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise OllamaIntegrationError("OLLAMA_CONFIGURED_PATH_NOT_FOUND")
    if path.suffix.casefold() not in {".exe", ".lnk"}:
        raise OllamaIntegrationError("OLLAMA_CONFIGURED_PATH_TYPE_INVALID")
    return path


def get_ollama_settings(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    settings = get_settings(workspace_dir, identity_id)["categories"]["connections"]
    return {
        "ollama_base_url": _loopback_base_url(settings["ollama_base_url"]),
        "ollama_executable_override": settings["ollama_executable_override"],
    }


def update_ollama_settings(
    workspace_dir: str | Path,
    values: Mapping[str, Any],
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    unknown = sorted(set(values) - _SETTINGS_KEYS)
    if unknown:
        raise OllamaIntegrationError("OLLAMA_SETTINGS_KEYS_UNKNOWN:" + ",".join(unknown))
    normalized: dict[str, Any] = {}
    if "ollama_base_url" in values:
        normalized["ollama_base_url"] = _loopback_base_url(str(values["ollama_base_url"]))
    if "ollama_executable_override" in values:
        configured = values["ollama_executable_override"]
        normalized["ollama_executable_override"] = (
            None if configured in (None, "") else str(_launcher_path(str(configured)))
        )
    update_settings(workspace_dir, "connections", normalized, identity_id)
    return get_ollama_settings(workspace_dir, identity_id)


def _start_menu_roots() -> list[Path]:
    roots: list[Path] = []
    for environment_name in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(environment_name)
        if not base:
            continue
        root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if root.is_dir():
            roots.append(root.resolve())
    return roots


def _registered_app_paths() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    rows: list[Path] = []
    access_modes = [winreg.KEY_READ]
    for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        flag = getattr(winreg, flag_name, 0)
        if flag:
            access_modes.append(winreg.KEY_READ | flag)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for executable in ("ollama.exe", "Ollama app.exe"):
            key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
            for access in access_modes:
                try:
                    with winreg.OpenKey(hive, key_name, 0, access) as key:
                        value, _ = winreg.QueryValueEx(key, None)
                    rows.append(Path(str(value)))
                    break
                except OSError:
                    continue
    return rows


def _candidate(path: str | Path, source: str) -> dict[str, str] | None:
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_file() or candidate.suffix.casefold() not in {".exe", ".lnk"}:
        return None
    return {"status": "AVAILABLE", "path": str(candidate), "source": source}


def _start_menu_candidates(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        matches = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file()
                and path.suffix.casefold() in {".exe", ".lnk"}
                and "ollama" in path.stem.casefold()
            ),
            key=lambda path: path.as_posix().casefold(),
        )
        yield from matches


def discover_ollama(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    settings = get_ollama_settings(workspace_dir, identity_id)
    warnings: list[str] = []
    configured = settings["ollama_executable_override"]
    if configured:
        result = _candidate(configured, "CONFIGURED_OVERRIDE")
        if result:
            return {**result, "warnings": warnings}
        warnings.append("CONFIGURED_OVERRIDE_NOT_FOUND")
    for path in _start_menu_candidates(_start_menu_roots()):
        result = _candidate(path, "START_MENU")
        if result:
            return {**result, "warnings": warnings}
    for path in _registered_app_paths():
        result = _candidate(path, "WINDOWS_APP_PATHS")
        if result:
            return {**result, "warnings": warnings}
    for executable in ("ollama.exe", "ollama"):
        resolved = shutil.which(executable)
        if resolved:
            result = _candidate(resolved, "PATH")
            if result:
                return {**result, "warnings": warnings}
    return {
        "status": "OLLAMA_NOT_INSTALLED",
        "path": None,
        "source": None,
        "warnings": warnings,
        "actions": list(NOT_INSTALLED_ACTIONS),
    }


def _workspace_connection(workspace_dir: str | Path):
    workspace = normalize_workspace_dir(workspace_dir)
    ensure_local_workspace(workspace)
    connection = connect(workspace_db_path(workspace))
    return workspace, connection


def _receipt(connection, operation: str, status: str, detail: Mapping[str, Any]) -> None:
    created_at = now()
    receipt_id = _stable_id("receipt", operation, status, created_at)
    connection.execute(
        "INSERT INTO local_operation_receipt VALUES(?,?,?,?,?,?,?)",
        (receipt_id, operation, "ollama_application", "OLLAMA_APPLICATION", status, _json(dict(detail)), created_at),
    )


def refresh_ollama_registry(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    discovered = discover_ollama(workspace_dir, identity_id)
    _, connection = _workspace_connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        warning = ";".join(discovered.get("warnings") or []) or (
            "OLLAMA_NOT_INSTALLED" if discovered["status"] != "AVAILABLE" else None
        )
        connection.execute(
            "INSERT INTO fallback_tool_registry(tool_category,primary_tool,fallback_tool,metadata_only_fallback,detected_path,version,status,last_tested,warning_message) "
            "VALUES('OLLAMA_APPLICATION','Ollama',NULL,NULL,?,NULL,?,?,?) "
            "ON CONFLICT(tool_category) DO UPDATE SET detected_path=excluded.detected_path,status=excluded.status,"
            "last_tested=excluded.last_tested,warning_message=excluded.warning_message",
            (discovered.get("path"), discovered["status"], now(), warning),
        )
        _receipt(
            connection,
            "ollama.registry.refresh",
            "PASS",
            {
                "status": discovered["status"],
                "source": discovered.get("source"),
                "project_data_sent": False,
            },
        )
        connection.commit()
    finally:
        connection.close()
    return {**discovered, "registry": "fallback_tool_registry"}


def _start_application(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def launch_ollama(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    discovered = discover_ollama(workspace_dir, identity_id)
    if discovered["status"] != "AVAILABLE":
        return {
            **discovered,
            "silent_install_started": False,
            "project_data_sent": False,
        }
    target = _launcher_path(str(discovered["path"]))
    _start_application(target)
    _, connection = _workspace_connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _receipt(
            connection,
            "ollama.launch",
            "PASS",
            {
                "path": str(target),
                "source": discovered["source"],
                "project_data_sent": False,
                "silent_install_started": False,
            },
        )
        connection.commit()
    finally:
        connection.close()
    return {
        "status": "LAUNCHED",
        "path": str(target),
        "source": discovered["source"],
        "project_data_sent": False,
        "silent_install_started": False,
    }


def _read_json(url: str) -> dict[str, Any]:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "EvidenceOS-T023/1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=3.0) as response:
            raw = response.read(_MAX_API_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise OllamaIntegrationError("OLLAMA_API_UNREACHABLE") from exc
    if len(raw) > _MAX_API_RESPONSE_BYTES:
        raise OllamaIntegrationError("OLLAMA_API_RESPONSE_TOO_LARGE")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OllamaIntegrationError("OLLAMA_API_RESPONSE_INVALID") from exc
    if not isinstance(payload, dict):
        raise OllamaIntegrationError("OLLAMA_API_RESPONSE_INVALID")
    return payload


def get_ollama_models(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    base_url = get_ollama_settings(workspace_dir, identity_id)["ollama_base_url"]
    payload = _read_json(base_url + "/api/tags")
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        raise OllamaIntegrationError("OLLAMA_API_RESPONSE_INVALID")
    models: list[dict[str, Any]] = []
    for raw_model in raw_models[:_MAX_MODELS]:
        if not isinstance(raw_model, Mapping):
            continue
        model = {field: raw_model.get(field) for field in _MODEL_FIELDS}
        details = raw_model.get("details")
        model["details"] = (
            {field: details.get(field) for field in _MODEL_DETAIL_FIELDS if field in details}
            if isinstance(details, Mapping)
            else {}
        )
        models.append(model)
    return {
        "status": "AVAILABLE",
        "endpoint": base_url,
        "models": models,
        "model_count": len(models),
        "response_truncated": len(raw_models) > _MAX_MODELS,
        "project_data_sent": False,
    }


def get_ollama_status(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
    *,
    check_api: bool = False,
) -> dict[str, Any]:
    settings = get_ollama_settings(workspace_dir, identity_id)
    launcher = discover_ollama(workspace_dir, identity_id)
    api: dict[str, Any] = {"status": "NOT_CHECKED"}
    if check_api:
        try:
            models = get_ollama_models(workspace_dir, identity_id)
            api = {"status": "AVAILABLE", "model_count": models["model_count"]}
        except OllamaIntegrationError as exc:
            api = {"status": str(exc)}
    return {"settings": settings, "launcher": launcher, "api": api}


__all__ = [
    "NOT_INSTALLED_ACTIONS",
    "OllamaIntegrationError",
    "discover_ollama",
    "get_ollama_models",
    "get_ollama_settings",
    "get_ollama_status",
    "launch_ollama",
    "refresh_ollama_registry",
    "update_ollama_settings",
]
