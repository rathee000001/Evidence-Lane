from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_ROOTS_SCHEMA = "EVIDENCEOS_WORKSPACE_ROOT_HISTORY_V1"
CANONICAL_PRODUCTION_ROOT_ENV = "EVIDENCE_OS_CANONICAL_ROOT"
NATIVE_PRODUCTION_ENV = "EVIDENCE_OS_NATIVE_PRODUCTION"
WINDOWS_CANONICAL_PRODUCTION_ROOT = Path(r"D:\EvidenceLane")


class WorkspaceRootHistoryError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _truthy_environment(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def native_production_mode() -> bool:
    return _truthy_environment(NATIVE_PRODUCTION_ENV)


def _without_windows_extended_prefix(value: str) -> str:
    """Return the ordinary spelling for an equivalent Win32 namespace path."""

    if os.name != "nt":
        return value
    folded = value.casefold()
    if folded.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if folded.startswith("\\\\?\\"):
        return value[4:]
    return value


def _path_identity_key(path: str | Path) -> str:
    ordinary = _without_windows_extended_prefix(str(path))
    resolved = Path(ordinary).expanduser().resolve()
    return os.path.normcase(_without_windows_extended_prefix(str(resolved)))


def canonical_production_root() -> Path:
    override = str(os.environ.get(CANONICAL_PRODUCTION_ROOT_ENV) or "").strip()
    if override:
        root = Path(_without_windows_extended_prefix(override)).expanduser().resolve()
    elif os.name == "nt":
        root = WINDOWS_CANONICAL_PRODUCTION_ROOT.resolve()
    else:
        root = (Path.home() / "EvidenceLane").resolve()
    if not root.is_absolute():
        raise WorkspaceRootHistoryError("CANONICAL_PRODUCTION_ROOT_MUST_BE_ABSOLUTE")
    return root


def _canonical_production_key() -> str:
    return _path_identity_key(canonical_production_root())


def assert_canonical_production_workspace(path: str | Path) -> Path:
    root = _canonical(path)
    if not native_production_mode():
        return root
    if _path_identity_key(root) != _canonical_production_key():
        raise WorkspaceRootHistoryError(
            f"NON_CANONICAL_PRODUCTION_WORKSPACE_FORBIDDEN:{root}"
        )
    if not root.is_dir():
        raise WorkspaceRootHistoryError(f"CANONICAL_PRODUCTION_ROOT_NOT_READY:{root}")
    return root


def canonical_route_state() -> dict[str, Any]:
    root = canonical_production_root()
    ready = root.is_dir()
    return {
        "schema": "T023_CANONICAL_PRODUCTION_ROUTE_V1",
        "status": "CANONICAL_ROOT_READY" if ready else "CANONICAL_ROOT_MISSING",
        "production_native": native_production_mode(),
        "canonical_root": str(root),
        "canonical_root_exists": ready,
        "configuration_root": str(root / "config"),
        "runtime_root": str(root / "runtime"),
        "legacy_route_allowed": False,
    }


def roaming_config_root() -> Path:
    if native_production_mode():
        return canonical_production_root() / "config"
    override = str(os.environ.get("EVIDENCE_OS_ROAMING_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if appdata:
        return (Path(appdata).expanduser() / "EvidenceOS").resolve()
    user_profile = str(os.environ.get("USERPROFILE") or "").strip()
    if user_profile:
        return (Path(user_profile).expanduser() / "AppData" / "Roaming" / "EvidenceOS").resolve()
    return (Path.home() / "AppData" / "Roaming" / "EvidenceOS").resolve()


def default_workspace_dir() -> Path:
    if native_production_mode() or str(os.environ.get(CANONICAL_PRODUCTION_ROOT_ENV) or "").strip():
        return canonical_production_root()
    return roaming_config_root() / "SQLiteBrain"


def workspace_root_registry_path() -> Path:
    return roaming_config_root() / "workspace-roots.json"


def _canonical(path: str | Path) -> Path:
    resolved = Path(_without_windows_extended_prefix(str(path))).expanduser().resolve()
    if not resolved.is_absolute():
        raise WorkspaceRootHistoryError("WORKSPACE_ROOT_MUST_BE_ABSOLUTE")
    return resolved


def _key(path: str | Path) -> str:
    return _path_identity_key(_canonical(path))


def _empty_registry() -> dict[str, Any]:
    return {
        "schema": WORKSPACE_ROOTS_SCHEMA,
        "active_root": None,
        "roots": [],
        "updated_at": _utc_now(),
    }


def _load() -> dict[str, Any]:
    path = workspace_root_registry_path()
    if not path.is_file():
        return _empty_registry()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceRootHistoryError("WORKSPACE_ROOT_HISTORY_UNREADABLE") from exc
    if payload.get("schema") != WORKSPACE_ROOTS_SCHEMA or not isinstance(payload.get("roots"), list):
        raise WorkspaceRootHistoryError("WORKSPACE_ROOT_HISTORY_SCHEMA_INVALID")
    return payload


def _write(payload: dict[str, Any]) -> Path:
    target = workspace_root_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["schema"] = WORKSPACE_ROOTS_SCHEMA
    body["updated_at"] = _utc_now()
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def current_workspace_root() -> Path:
    registry = _load()
    active = str(registry.get("active_root") or "").strip()
    root = _canonical(active) if active else default_workspace_dir()
    if native_production_mode() and _path_identity_key(root) != _canonical_production_key():
        raise WorkspaceRootHistoryError(
            f"NON_CANONICAL_PRODUCTION_WORKSPACE_FORBIDDEN:{root}"
        )
    return root


def record_workspace_root(path: str | Path, *, source: str, make_active: bool = True) -> dict[str, Any]:
    root = _canonical(path)
    if native_production_mode():
        if _path_identity_key(root) != _canonical_production_key() or not make_active:
            raise WorkspaceRootHistoryError(
                f"NON_CANONICAL_PRODUCTION_WORKSPACE_FORBIDDEN:{root}"
            )
    registry = _load()
    stamp = _utc_now()
    wanted = _key(root)
    rows: list[dict[str, Any]] = []
    found = False
    for raw in registry["roots"]:
        if not isinstance(raw, dict) or not str(raw.get("path") or "").strip():
            continue
        row = dict(raw)
        if _key(str(row["path"])) == wanted:
            found = True
            row["path"] = str(root)
            row["last_used_at"] = stamp
            row["source"] = str(source)
        row["active"] = bool(make_active and _key(str(row["path"])) == wanted)
        rows.append(row)
    if not found:
        rows.append(
            {
                "path": str(root),
                "display_name": root.name or str(root),
                "source": str(source),
                "added_at": stamp,
                "last_used_at": stamp,
                "active": bool(make_active),
            }
        )
    if not make_active:
        active_key = _key(str(registry.get("active_root") or default_workspace_dir()))
        for row in rows:
            row["active"] = _key(str(row["path"])) == active_key
    rows.sort(key=lambda row: (not bool(row.get("active")), str(row.get("last_used_at") or "")), reverse=False)
    registry["roots"] = rows
    if make_active:
        registry["active_root"] = str(root)
    history_file = _write(registry)
    return {
        "status": "PASS",
        "active_root": str(registry.get("active_root") or root),
        "roots": rows,
        "history_file": str(history_file),
        "data_deleted": False,
    }


def list_workspace_roots() -> dict[str, Any]:
    registry = _load()
    if native_production_mode():
        root = canonical_production_root()
        active = str(registry.get("active_root") or root)
        if _path_identity_key(_canonical(active)) != _canonical_production_key():
            raise WorkspaceRootHistoryError(
                f"NON_CANONICAL_PRODUCTION_WORKSPACE_FORBIDDEN:{active}"
            )
        row = next(
            (
                dict(item)
                for item in registry.get("roots", [])
                if isinstance(item, dict)
                and str(item.get("path") or "").strip()
                and _path_identity_key(_canonical(str(item["path"]))) == _canonical_production_key()
            ),
            {
                "path": str(root),
                "display_name": root.name,
                "source": "NATIVE_CANONICAL_PRODUCTION_ROOT",
                "added_at": None,
                "last_used_at": None,
            },
        )
        row.update(path=str(root), active=True)
        return {
            "schema": WORKSPACE_ROOTS_SCHEMA,
            "status": "CANONICAL_ROOT_READY" if root.is_dir() else "CANONICAL_ROOT_MISSING",
            "active_root": str(root),
            "roots": [row],
            "history_file": str(workspace_root_registry_path()),
            "legacy_route_allowed": False,
        }
    if not registry["roots"]:
        root = default_workspace_dir()
        return {
            "schema": WORKSPACE_ROOTS_SCHEMA,
            "status": "DEFAULT_ROAMING",
            "active_root": str(root),
            "roots": [
                {
                    "path": str(root),
                    "display_name": root.name,
                    "source": "APP_ROAMING_DEFAULT",
                    "added_at": None,
                    "last_used_at": None,
                    "active": True,
                }
            ],
            "history_file": str(workspace_root_registry_path()),
        }
    return {
        "schema": WORKSPACE_ROOTS_SCHEMA,
        "status": "PASS",
        "active_root": str(registry.get("active_root") or default_workspace_dir()),
        "roots": registry["roots"],
        "history_file": str(workspace_root_registry_path()),
    }


def retain_only_workspace_root(path: str | Path, *, source: str) -> dict[str, Any]:
    """Keep one active catalog root while preserving every filesystem byte.

    This is the native-startup migration for the current-root-only product
    contract. It removes stale route pointers, not brain folders or artifacts.
    """

    root = _canonical(path)
    if native_production_mode() and _path_identity_key(root) != _canonical_production_key():
        raise WorkspaceRootHistoryError(
            f"NON_CANONICAL_PRODUCTION_WORKSPACE_FORBIDDEN:{root}"
        )
    registry = _load()
    stamp = _utc_now()
    wanted = _key(root)
    prior_rows = [row for row in registry.get("roots", []) if isinstance(row, dict)]
    prior = next(
        (row for row in prior_rows if str(row.get("path") or "").strip() and _key(str(row["path"])) == wanted),
        {},
    )
    active_row = {
        "path": str(root),
        "display_name": str(prior.get("display_name") or root.name or root),
        "source": str(source),
        "added_at": prior.get("added_at") or stamp,
        "last_used_at": stamp,
        "active": True,
    }
    removed = [
        str(row.get("path") or "")
        for row in prior_rows
        if str(row.get("path") or "").strip() and _key(str(row["path"])) != wanted
    ]
    registry["active_root"] = str(root)
    registry["roots"] = [active_row]
    history_file = _write(registry)
    return {
        "schema": WORKSPACE_ROOTS_SCHEMA,
        "status": "PASS_CURRENT_ROOT_ONLY",
        "active_root": str(root),
        "roots": [active_row],
        "removed_route_pointers": removed,
        "removed_route_pointer_count": len(removed),
        "history_file": str(history_file),
        "data_deleted": False,
    }


def drop_workspace_root_history(path: str | Path, *, confirm_path: str | Path) -> dict[str, Any]:
    if native_production_mode():
        raise WorkspaceRootHistoryError("CANONICAL_PRODUCTION_ROOT_HISTORY_DROP_FORBIDDEN")
    root = _canonical(path)
    confirmed = _canonical(confirm_path)
    if _key(root) != _key(confirmed):
        raise WorkspaceRootHistoryError("WORKSPACE_ROOT_DROP_CONFIRMATION_MISMATCH")
    registry = _load()
    active = _canonical(str(registry.get("active_root") or default_workspace_dir()))
    if _key(root) == _key(active):
        raise WorkspaceRootHistoryError("ACTIVE_WORKSPACE_ROOT_CANNOT_BE_DROPPED")
    prior = [row for row in registry["roots"] if isinstance(row, dict)]
    remaining = [row for row in prior if _key(str(row.get("path") or default_workspace_dir())) != _key(root)]
    if len(remaining) == len(prior):
        raise WorkspaceRootHistoryError("WORKSPACE_ROOT_HISTORY_ENTRY_NOT_FOUND")
    registry["roots"] = remaining
    history_file = _write(registry)
    return {
        "status": "PASS",
        "action": "FORGET_WORKSPACE_ROOT_HISTORY_ONLY",
        "forgotten_root": str(root),
        "active_root": str(active),
        "roots": remaining,
        "history_file": str(history_file),
        "data_deleted": False,
        "filesystem_root_preserved": root.exists(),
    }


__all__ = [
    "CANONICAL_PRODUCTION_ROOT_ENV",
    "NATIVE_PRODUCTION_ENV",
    "WINDOWS_CANONICAL_PRODUCTION_ROOT",
    "WORKSPACE_ROOTS_SCHEMA",
    "WorkspaceRootHistoryError",
    "assert_canonical_production_workspace",
    "canonical_production_root",
    "canonical_route_state",
    "current_workspace_root",
    "default_workspace_dir",
    "drop_workspace_root_history",
    "list_workspace_roots",
    "native_production_mode",
    "record_workspace_root",
    "retain_only_workspace_root",
    "roaming_config_root",
    "workspace_root_registry_path",
]
