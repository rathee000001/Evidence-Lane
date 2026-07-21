from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from sqlite_brain_builder.core import now
from sqlite_brain_builder.runtime.path_policy import normalize_workspace_dir, workspace_db_path
from sqlite_brain_builder.storage.sqlite_utils import connect
from sqlite_brain_builder.workspace.local_workspace import (
    DEFAULT_IDENTITY_ID,
    ensure_local_workspace,
)


class CodexApplicationError(RuntimeError):
    pass


DEFAULT_CODEX_APP_ID = "OpenAI.Codex_2p2nqsd0c76g0!App"
NOT_AVAILABLE_ACTIONS = ["INSTALL_CODEX", "SKIP_FOR_NOW"]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: object) -> str:
    token = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]}"


def _launcher_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise CodexApplicationError("CODEX_CONFIGURED_PATH_NOT_FOUND")
    if path.suffix.casefold() not in {".exe", ".lnk"}:
        raise CodexApplicationError("CODEX_CONFIGURED_PATH_TYPE_INVALID")
    return path


def _app_package_available(app_id: str) -> bool:
    if os.name != "nt" or "!" not in app_id:
        return False
    package_family = app_id.split("!", 1)[0]
    local_app_data = os.environ.get("LOCALAPPDATA")
    return bool(local_app_data and (Path(local_app_data) / "Packages" / package_family).is_dir())


def get_codex_settings(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    return {
        "discovery_mode": "WINDOWS_AUTOMATIC",
        "manual_route_allowed": False,
        "codex_application_id": DEFAULT_CODEX_APP_ID,
        "codex_executable_override": None,
    }


def update_codex_settings(
    workspace_dir: str | Path,
    values: Mapping[str, Any],
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    del workspace_dir, values, identity_id
    raise CodexApplicationError("CODEX_ROUTE_IS_AUTOMATIC_WINDOWS_DISCOVERY")


def discover_codex(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    del workspace_dir, identity_id
    app_id = DEFAULT_CODEX_APP_ID
    if _app_package_available(app_id):
        return {"status": "AVAILABLE", "path": None, "app_id": app_id, "source": "WINDOWS_APP_PACKAGE"}
    return {
        "status": "CODEX_APPLICATION_NOT_AVAILABLE",
        "path": None,
        "app_id": app_id,
        "source": "WINDOWS_APP_PACKAGE",
        "actions": list(NOT_AVAILABLE_ACTIONS),
    }


def _start_application(*, path: str | None, app_id: str | None) -> None:
    if path:
        target = _launcher_path(path)
        if os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return
    if os.name != "nt" or not app_id:
        raise CodexApplicationError("CODEX_APPLICATION_NOT_AVAILABLE")
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    subprocess.Popen(
        ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


def _record_launch_receipt(workspace_dir: str | Path, detail: Mapping[str, Any]) -> None:
    workspace = normalize_workspace_dir(workspace_dir)
    ensure_local_workspace(workspace)
    connection = connect(workspace_db_path(workspace))
    created_at = now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO local_operation_receipt VALUES(?,?,?,?,?,?,?)",
            (
                _stable_id("receipt", "codex.launch", created_at),
                "codex.launch",
                "codex_application",
                "CODEX_APPLICATION",
                "PASS",
                _json(dict(detail)),
                created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def launch_codex(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    discovered = discover_codex(workspace_dir, identity_id)
    if discovered["status"] != "AVAILABLE":
        return {
            **discovered,
            "project_data_sent": False,
            "silent_install_started": False,
            "foreground_transfer_requested": False,
        }
    _start_application(path=discovered.get("path"), app_id=discovered.get("app_id"))
    detail = {
        "source": discovered["source"],
        "path": discovered.get("path"),
        "app_id": discovered.get("app_id"),
        "project_data_sent": False,
        "silent_install_started": False,
        "foreground_transfer_requested": True,
    }
    _record_launch_receipt(workspace_dir, detail)
    return {"status": "LAUNCHED", **detail}


def get_codex_status(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    return {"settings": get_codex_settings(workspace_dir, identity_id), "launcher": discover_codex(workspace_dir, identity_id)}


__all__ = [
    "CodexApplicationError",
    "DEFAULT_CODEX_APP_ID",
    "NOT_AVAILABLE_ACTIONS",
    "discover_codex",
    "get_codex_settings",
    "get_codex_status",
    "launch_codex",
    "update_codex_settings",
]
