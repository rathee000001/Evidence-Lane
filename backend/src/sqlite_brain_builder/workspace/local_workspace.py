from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import sqlite3
from pathlib import Path
from typing import Any

from sqlite_brain_builder.core import file_sha256, now
from sqlite_brain_builder.runtime.env15_chat_lineage import append_env15_chat_lineage_source
from sqlite_brain_builder.runtime.path_policy import brain_output_dir, normalize_workspace_dir, workspace_db_path
from sqlite_brain_builder.storage.sqlite_utils import connect
from sqlite_brain_builder.workspace.protected_credentials import delete_secret, get_secret, set_secret
from sqlite_brain_builder.workspace.workspace_db import init_workspace


class LocalWorkspaceError(RuntimeError):
    pass


SCHEMA_VERSION = 1
DEFAULT_IDENTITY_ID = "identity_local_owner"

_CHARACTER_GLB_SETTING_KEYS = {
    "character_face_glb": "face",
    "character_body_glb": "body",
}
_CHARACTER_GLB_MAX_BYTES = 64 * 1024 * 1024
_CHARACTER_GLB_STORAGE = "WORKSPACE_FILE_V1"
_CHARACTER_GLB_RELEASE_SCOPE = "LOCAL_PRIVATE_TEST_ONLY"
_CHARACTER_GLB_PUBLIC_RELEASE_GATE = "BLOCKED_PENDING_PROVENANCE_ATTRIBUTION_RECEIPT"

DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "account": {"locale": "en-US", "notification_enabled": True},
    "general": {"show_changelog": True, "ctrl_enter_to_send": False, "recent_items_limit": 30},
    "interface": {
        "theme_mode": "system", "theme_id": "evidence-glass", "accent": "evidence",
        "text_scale": 1.0, "widescreen": False, "high_contrast": False,
        "chat_direction": "auto", "reduced_motion": False, "extension_tokens": {},
        "character_face_glb": None, "character_body_glb": None,
    },
    "personalization": {
        "prompt_autocomplete": True, "auto_tags": False, "auto_followups": False,
        "local_memory_enabled": True, "user_location": None,
    },
    "data_controls": {
        "image_compression": True, "image_compression_size": 1920,
        "large_content_as_file": True, "export_format": "json",
    },
    "audio": {
        "stt_enabled": False, "tts_enabled": False, "stt_engine": "unavailable",
        "tts_engine": "unavailable", "voice": None, "model": None,
        "interruption": False, "auto_send": False, "auto_playback": False,
        "availability": "UNAVAILABLE_NOT_CONFIGURED",
    },
    "connections": {
        "direct_connections": False,
        "base_model_cache": False,
        "ollama_executable_override": None,
        "ollama_base_url": "http://127.0.0.1:11434",
        "codex_application_id": "OpenAI.Codex_2p2nqsd0c76g0!App",
        "codex_executable_override": None,
    },
    "integrations": {"tool_servers": [], "terminal_servers": []},
    "advanced": {
        "system_prompt": "", "seed": None, "temperature": None, "repeat_penalty": None,
        "top_k": None, "top_p": None, "context_size": None, "batch_size": None,
    },
}

DEFAULT_ADMIN_SETTINGS: dict[str, Any] = {
    "enable_direct_connections": False,
    "enable_code_execution": False,
    "enable_terminal_integrations": False,
    "enable_tool_integrations": False,
    "default_models": [],
    "user_permissions": {
        "chat_delete": True, "file_upload": True, "folders": True,
        "memories": True, "settings_interface": True,
    },
}


def _stable_id(prefix: str, *parts: object) -> str:
    token = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise LocalWorkspaceError(f"UNSUPPORTED_SETTING_VALUE_TYPE:{type(value).__name__}")


def _character_asset_root(workspace: Path) -> Path:
    return (workspace / ".evidenceos_local_assets" / "character").resolve()


def _character_asset_path(workspace: Path, asset_ref: object) -> Path:
    if not isinstance(asset_ref, str) or not asset_ref:
        raise LocalWorkspaceError("CHARACTER_GLB_ASSET_REFERENCE_INVALID")
    root = _character_asset_root(workspace)
    target = (workspace / Path(asset_ref)).resolve()
    if not target.is_relative_to(root) or target.suffix.casefold() != ".glb":
        raise LocalWorkspaceError("CHARACTER_GLB_ASSET_REFERENCE_OUTSIDE_STORE")
    return target


def _parse_character_glb(payload: bytes) -> dict[str, Any]:
    if len(payload) < 20 or payload[:4] != b"glTF":
        raise LocalWorkspaceError("CHARACTER_GLB_HEADER_INVALID")
    version = int.from_bytes(payload[4:8], "little")
    declared_length = int.from_bytes(payload[8:12], "little")
    if version != 2 or declared_length != len(payload):
        raise LocalWorkspaceError("CHARACTER_GLB_HEADER_INVALID")

    offset = 12
    document: dict[str, Any] | None = None
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise LocalWorkspaceError("CHARACTER_GLB_CHUNK_INVALID")
        chunk_length = int.from_bytes(payload[offset : offset + 4], "little")
        chunk_type = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_length
        if chunk_length % 4 or chunk_end > len(payload):
            raise LocalWorkspaceError("CHARACTER_GLB_CHUNK_INVALID")
        if chunk_type == 0x4E4F534A and document is None:
            try:
                parsed = json.loads(payload[chunk_start:chunk_end].rstrip(b"\x00 \t\r\n").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LocalWorkspaceError("CHARACTER_GLB_JSON_INVALID") from exc
            if not isinstance(parsed, dict):
                raise LocalWorkspaceError("CHARACTER_GLB_JSON_INVALID")
            document = parsed
        offset = chunk_end
    if document is None:
        raise LocalWorkspaceError("CHARACTER_GLB_JSON_CHUNK_REQUIRED")

    for collection in ("buffers", "images"):
        rows = document.get(collection) or []
        if not isinstance(rows, list):
            raise LocalWorkspaceError("CHARACTER_GLB_JSON_INVALID")
        for row in rows:
            uri = row.get("uri") if isinstance(row, dict) else None
            if isinstance(uri, str) and not uri.casefold().startswith("data:"):
                raise LocalWorkspaceError("CHARACTER_GLB_EXTERNAL_URI_FORBIDDEN")
    return document


def _decode_character_glb_setting(value: object) -> tuple[str, bytes, str]:
    if not isinstance(value, dict):
        raise LocalWorkspaceError("CHARACTER_GLB_SETTING_INVALID")
    name = value.get("name")
    data_url = value.get("data_url")
    supplied_sha256 = value.get("sha256")
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or not name.casefold().endswith(".glb")
        or not isinstance(data_url, str)
        or not isinstance(supplied_sha256, str)
        or len(supplied_sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in supplied_sha256)
    ):
        raise LocalWorkspaceError("CHARACTER_GLB_SETTING_INVALID")
    header, separator, encoded = data_url.partition(",")
    if not separator or not header.casefold().startswith("data:") or not header.casefold().endswith(";base64"):
        raise LocalWorkspaceError("CHARACTER_GLB_DATA_URL_INVALID")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LocalWorkspaceError("CHARACTER_GLB_DATA_URL_INVALID") from exc
    if not payload or len(payload) > _CHARACTER_GLB_MAX_BYTES:
        raise LocalWorkspaceError("CHARACTER_GLB_SIZE_INVALID")
    _parse_character_glb(payload)
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256.casefold() != supplied_sha256.casefold():
        raise LocalWorkspaceError("CHARACTER_GLB_SHA256_MISMATCH")
    return name, payload, actual_sha256


def _store_character_glb_setting(workspace: Path, identity_id: str, setting_key: str, value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    name, payload, sha256 = _decode_character_glb_setting(value)
    slot = _CHARACTER_GLB_SETTING_KEYS[setting_key]
    identity_scope = _stable_id("identity_assets", identity_id)
    relative = Path(".evidenceos_local_assets") / "character" / identity_scope / slot / f"{sha256}.glb"
    target = _character_asset_path(workspace, relative.as_posix())
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.stat().st_size != len(payload) or hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
            raise LocalWorkspaceError("CHARACTER_GLB_ASSET_STORE_CONFLICT")
    else:
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "storage": _CHARACTER_GLB_STORAGE,
        "asset_ref": relative.as_posix(),
        "name": name,
        "sha256": sha256,
        "bytes": len(payload),
        "media_type": "model/gltf-binary",
        "self_contained": True,
        "release_scope": _CHARACTER_GLB_RELEASE_SCOPE,
        "public_release_gate": _CHARACTER_GLB_PUBLIC_RELEASE_GATE,
    }


def _materialize_character_glb_setting(workspace: Path, value: Any) -> Any:
    if not isinstance(value, dict) or value.get("storage") != _CHARACTER_GLB_STORAGE:
        return value
    target = _character_asset_path(workspace, value.get("asset_ref"))
    if not target.is_file():
        raise LocalWorkspaceError("CHARACTER_GLB_ASSET_FILE_MISSING")
    payload = target.read_bytes()
    expected_sha256 = str(value.get("sha256") or "").casefold()
    if len(payload) != int(value.get("bytes") or -1) or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise LocalWorkspaceError("CHARACTER_GLB_ASSET_FILE_INTEGRITY_FAILED")
    _parse_character_glb(payload)
    return {
        **value,
        "data_url": "data:model/gltf-binary;base64," + base64.b64encode(payload).decode("ascii"),
    }


def _character_asset_reference(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("storage") == _CHARACTER_GLB_STORAGE and isinstance(value.get("asset_ref"), str):
        return value["asset_ref"]
    return None


def _remove_character_asset(workspace: Path, asset_ref: str | None) -> None:
    if not asset_ref:
        return
    target = _character_asset_path(workspace, asset_ref)
    target.unlink(missing_ok=True)
    root = _character_asset_root(workspace)
    parent = target.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _connection(workspace_dir: str | Path) -> tuple[Path, sqlite3.Connection]:
    workspace = normalize_workspace_dir(workspace_dir)
    init_workspace(workspace)
    connection = connect(workspace_db_path(workspace))
    connection.row_factory = sqlite3.Row
    return workspace, connection


def _receipt(connection: sqlite3.Connection, operation: str, entity_type: str, entity_id: str, status: str, detail: Any) -> None:
    created_at = now()
    receipt_id = _stable_id("receipt", operation, entity_type, entity_id, status, created_at)
    connection.execute(
        "INSERT INTO local_operation_receipt VALUES(?,?,?,?,?,?,?)",
        (receipt_id, operation, entity_type, entity_id, status, _json(detail), created_at),
    )


def ensure_local_workspace(workspace_dir: str | Path) -> dict[str, Any]:
    workspace, connection = _connection(workspace_dir)
    stamp = now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO local_identity VALUES(?,?,?,?,?,?)",
            (DEFAULT_IDENTITY_ID, "LOCAL_DESKTOP", "admin", "ACTIVE", stamp, stamp),
        )
        connection.execute(
            "INSERT OR IGNORE INTO local_profile(identity_id,display_name,avatar_ref,role_label,metadata_json,updated_at) VALUES(?,?,?,?,?,?)",
            (DEFAULT_IDENTITY_ID, "Evidence OS User", "", "Local profile", "{}", stamp),
        )
        for category, values in DEFAULT_SETTINGS.items():
            for key, value in values.items():
                connection.execute(
                    "INSERT OR IGNORE INTO local_setting VALUES(?,?,?,?,?,?,?)",
                    (DEFAULT_IDENTITY_ID, category, key, _value_type(value), _json(value), SCHEMA_VERSION, stamp),
                )
        for key, value in DEFAULT_ADMIN_SETTINGS.items():
            connection.execute(
                "INSERT OR IGNORE INTO local_admin_setting VALUES(?,?,?,?,?,?)",
                (key, _value_type(value), _json(value), SCHEMA_VERSION, DEFAULT_IDENTITY_ID, stamp),
            )
        connection.execute(
            "INSERT OR IGNORE INTO local_session_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (DEFAULT_IDENTITY_ID, "session_local_default", None, None, "expanded", "[]", 0, "", "CONNECTED", "IDLE", None, None, stamp),
        )
        connection.commit()
        return {"status": "PASS", "workspace_dir": str(workspace), "identity_id": DEFAULT_IDENTITY_ID, "schema_version": SCHEMA_VERSION}
    finally:
        connection.close()


def _profile_payload(connection: sqlite3.Connection, identity_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT i.identity_id,i.account_kind,i.role,i.status,i.created_at,i.updated_at,p.* "
        "FROM local_identity i JOIN local_profile p ON p.identity_id=i.identity_id WHERE i.identity_id=?",
        (identity_id,),
    ).fetchone()
    if not row:
        raise LocalWorkspaceError("LOCAL_IDENTITY_NOT_FOUND")
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    return data


def get_profile(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    _, connection = _connection(workspace_dir)
    try:
        return _profile_payload(connection, identity_id)
    finally:
        connection.close()


def update_profile(workspace_dir: str | Path, values: dict[str, Any], identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    allowed = {
        "display_name", "username", "email", "avatar_ref", "role_label", "bio", "timezone",
        "presence_state", "status_emoji", "status_message", "status_expires_at", "metadata",
    }
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise LocalWorkspaceError("PROFILE_FIELDS_FORBIDDEN:" + ",".join(unknown))
    if "display_name" in values and not str(values["display_name"]).strip():
        raise LocalWorkspaceError("PROFILE_DISPLAY_NAME_REQUIRED")
    _, connection = _connection(workspace_dir)
    try:
        updates = dict(values)
        if "metadata" in updates:
            updates["metadata_json"] = _json(updates.pop("metadata"))
        updates["updated_at"] = now()
        assignments = ",".join(f"{key}=?" for key in updates)
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            f"UPDATE local_profile SET {assignments} WHERE identity_id=?",
            (*updates.values(), identity_id),
        ).rowcount
        if changed != 1:
            raise LocalWorkspaceError("LOCAL_IDENTITY_NOT_FOUND")
        connection.execute("UPDATE local_identity SET updated_at=? WHERE identity_id=?", (updates["updated_at"], identity_id))
        _receipt(connection, "profile.update", "identity", identity_id, "PASS", sorted(values))
        connection.commit()
        return _profile_payload(connection, identity_id)
    finally:
        connection.close()


def get_settings(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    workspace, connection = _connection(workspace_dir)
    try:
        result: dict[str, Any] = {category: {} for category in DEFAULT_SETTINGS}
        for row in connection.execute(
            "SELECT category,setting_key,value_json FROM local_setting WHERE identity_id=? ORDER BY category,setting_key",
            (identity_id,),
        ):
            value = json.loads(row["value_json"])
            if row["category"] == "interface" and row["setting_key"] in _CHARACTER_GLB_SETTING_KEYS:
                value = _materialize_character_glb_setting(workspace, value)
            result.setdefault(row["category"], {})[row["setting_key"]] = value
        return {"schema_version": SCHEMA_VERSION, "identity_id": identity_id, "categories": result}
    finally:
        connection.close()


def update_settings(workspace_dir: str | Path, category: str, values: dict[str, Any], identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    if category not in DEFAULT_SETTINGS:
        raise LocalWorkspaceError("SETTINGS_CATEGORY_UNKNOWN")
    unknown = sorted(set(values) - set(DEFAULT_SETTINGS[category]))
    if unknown:
        raise LocalWorkspaceError("SETTINGS_KEYS_UNKNOWN:" + ",".join(unknown))
    workspace = normalize_workspace_dir(workspace_dir).resolve()
    normalized = dict(values)
    if category == "interface":
        for key in set(normalized) & set(_CHARACTER_GLB_SETTING_KEYS):
            normalized[key] = _store_character_glb_setting(workspace, identity_id, key, normalized[key])
    stamp = now()
    _, connection = _connection(workspace)
    replaced_asset_refs: list[str | None] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        for key, value in normalized.items():
            if category == "interface" and key in _CHARACTER_GLB_SETTING_KEYS:
                prior = connection.execute(
                    "SELECT value_json FROM local_setting WHERE identity_id=? AND category=? AND setting_key=?",
                    (identity_id, category, key),
                ).fetchone()
                prior_value = json.loads(prior["value_json"]) if prior else None
                prior_ref = _character_asset_reference(prior_value)
                next_ref = _character_asset_reference(value)
                if prior_ref != next_ref:
                    replaced_asset_refs.append(prior_ref)
            connection.execute(
                "INSERT OR REPLACE INTO local_setting VALUES(?,?,?,?,?,?,?)",
                (identity_id, category, key, _value_type(value), _json(value), SCHEMA_VERSION, stamp),
            )
        _receipt(connection, "settings.update", "settings", f"{identity_id}:{category}", "PASS", sorted(values))
        connection.commit()
    finally:
        connection.close()
    for asset_ref in replaced_asset_refs:
        _remove_character_asset(workspace, asset_ref)
    return get_settings(workspace_dir, identity_id)


def reset_settings(workspace_dir: str | Path, category: str | None = None, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    if category is not None and category not in DEFAULT_SETTINGS:
        raise LocalWorkspaceError("SETTINGS_CATEGORY_UNKNOWN")
    workspace, connection = _connection(workspace_dir)
    removed_asset_refs: list[str | None] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        if category in (None, "interface"):
            removed_asset_refs = [
                _character_asset_reference(json.loads(row["value_json"]))
                for row in connection.execute(
                    "SELECT value_json FROM local_setting WHERE identity_id=? AND category='interface' AND setting_key IN ('character_face_glb','character_body_glb')",
                    (identity_id,),
                )
            ]
        if category:
            connection.execute("DELETE FROM local_setting WHERE identity_id=? AND category=?", (identity_id, category))
        else:
            connection.execute("DELETE FROM local_setting WHERE identity_id=?", (identity_id,))
        connection.commit()
    finally:
        connection.close()
    for asset_ref in removed_asset_refs:
        _remove_character_asset(workspace, asset_ref)
    ensure_local_workspace(workspace_dir)
    return get_settings(workspace_dir, identity_id)


def export_local_data(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    return {"contract": "EVIDENCEOS_LOCAL_EXPORT_V1", "profile": get_profile(workspace_dir, identity_id), "settings": get_settings(workspace_dir, identity_id)}


def import_local_data(workspace_dir: str | Path, payload: dict[str, Any], identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    if payload.get("contract") != "EVIDENCEOS_LOCAL_EXPORT_V1":
        raise LocalWorkspaceError("LOCAL_IMPORT_CONTRACT_INVALID")
    profile = payload.get("profile") or {}
    allowed_profile = {key: profile[key] for key in ("display_name", "username", "email", "avatar_ref", "role_label", "bio", "timezone", "presence_state", "status_emoji", "status_message", "status_expires_at", "metadata") if key in profile}
    if allowed_profile:
        update_profile(workspace_dir, allowed_profile, identity_id)
    for category, values in (payload.get("settings") or {}).get("categories", {}).items():
        update_settings(workspace_dir, category, values, identity_id)
    return export_local_data(workspace_dir, identity_id)


def _require_admin(connection: sqlite3.Connection, identity_id: str) -> None:
    row = connection.execute("SELECT role FROM local_identity WHERE identity_id=?", (identity_id,)).fetchone()
    if not row or row[0] != "admin":
        raise LocalWorkspaceError("ADMIN_AUTHORITY_REQUIRED")


def get_admin_settings(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    _, connection = _connection(workspace_dir)
    try:
        _require_admin(connection, identity_id)
        return {row["setting_key"]: json.loads(row["value_json"]) for row in connection.execute("SELECT * FROM local_admin_setting ORDER BY setting_key")}
    finally:
        connection.close()


def update_admin_settings(workspace_dir: str | Path, values: dict[str, Any], identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    unknown = sorted(set(values) - set(DEFAULT_ADMIN_SETTINGS))
    if unknown:
        raise LocalWorkspaceError("ADMIN_SETTINGS_KEYS_UNKNOWN:" + ",".join(unknown))
    _, connection = _connection(workspace_dir)
    try:
        _require_admin(connection, identity_id)
        connection.execute("BEGIN IMMEDIATE")
        stamp = now()
        for key, value in values.items():
            connection.execute(
                "INSERT OR REPLACE INTO local_admin_setting VALUES(?,?,?,?,?,?)",
                (key, _value_type(value), _json(value), SCHEMA_VERSION, identity_id, stamp),
            )
        _receipt(connection, "admin.settings.update", "admin_settings", identity_id, "PASS", sorted(values))
        connection.commit()
    finally:
        connection.close()
    return get_admin_settings(workspace_dir, identity_id)


def _credential_label(value: str, field: str) -> str:
    clean = " ".join(str(value or "").split())
    if not clean:
        raise LocalWorkspaceError(f"CREDENTIAL_{field}_REQUIRED")
    if len(clean) > 256 or any(ord(character) < 32 for character in clean):
        raise LocalWorkspaceError(f"CREDENTIAL_{field}_INVALID")
    return clean


def set_protected_credential(workspace_dir: str | Path, service_name: str, account_name: str, secret: str, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    service = _credential_label(service_name, "SERVICE_NAME")
    account = _credential_label(account_name, "ACCOUNT_NAME")
    if not isinstance(secret, str) or not secret:
        raise LocalWorkspaceError("CREDENTIAL_SECRET_REQUIRED")
    ensure_local_workspace(workspace_dir)
    vault_key = _stable_id("vault", identity_id, service.casefold(), account.casefold())
    target_name = f"EvidenceOS/{identity_id}/{vault_key}"
    credential_id = _stable_id("credential", identity_id, service.casefold(), account.casefold())
    stamp = now()
    _, connection = _connection(workspace_dir)
    existing = connection.execute(
        "SELECT created_at FROM protected_credential_reference WHERE credential_id=? AND identity_id=?",
        (credential_id, identity_id),
    ).fetchone()
    secret_written = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO protected_credential_reference(credential_id,identity_id,service_name,account_name,target_name,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'ACTIVE',?,?) "
            "ON CONFLICT(credential_id) DO UPDATE SET service_name=excluded.service_name,account_name=excluded.account_name,"
            "target_name=excluded.target_name,status='ACTIVE',updated_at=excluded.updated_at",
            (credential_id, identity_id, service, account, target_name, existing["created_at"] if existing else stamp, stamp),
        )
        set_secret(target_name, account, secret)
        secret_written = True
        _receipt(
            connection,
            "credential.set",
            "protected_credential",
            credential_id,
            "PASS",
            {"service_name": service, "account_name": account, "secret_stored_in_sqlite": False},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        if secret_written and existing is None:
            try:
                delete_secret(target_name)
            except Exception:
                pass
        raise
    finally:
        connection.close()
    return {"credential_id": credential_id, "service_name": service, "account_name": account, "status": "ACTIVE"}


def get_protected_credential_status(workspace_dir: str | Path, credential_id: str, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute(
            "SELECT credential_id,service_name,account_name,status,created_at,updated_at "
            "FROM protected_credential_reference WHERE credential_id=? AND identity_id=?",
            (credential_id, identity_id),
        ).fetchone()
        if not row:
            raise LocalWorkspaceError("CREDENTIAL_REFERENCE_NOT_FOUND")
        return dict(row)
    finally:
        connection.close()


def read_protected_credential(workspace_dir: str | Path, credential_id: str, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute(
            "SELECT * FROM protected_credential_reference WHERE credential_id=? AND identity_id=?",
            (credential_id, identity_id),
        ).fetchone()
        if not row:
            raise LocalWorkspaceError("CREDENTIAL_REFERENCE_NOT_FOUND")
        if row["status"] != "ACTIVE":
            raise LocalWorkspaceError("CREDENTIAL_REFERENCE_NOT_ACTIVE")
        target_name = row["target_name"]
        service_name = row["service_name"]
        account_name = row["account_name"]
    finally:
        connection.close()
    return {
        "credential_id": credential_id,
        "secret": get_secret(target_name),
        "service_name": service_name,
        "account_name": account_name,
    }


def delete_protected_credential(workspace_dir: str | Path, credential_id: str, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute(
            "SELECT target_name,status FROM protected_credential_reference WHERE credential_id=? AND identity_id=?",
            (credential_id, identity_id),
        ).fetchone()
        if not row:
            raise LocalWorkspaceError("CREDENTIAL_REFERENCE_NOT_FOUND")
        if row["status"] == "DELETED":
            return {"credential_id": credential_id, "status": "DELETED", "idempotent": True}
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE protected_credential_reference SET status='DELETE_PENDING',updated_at=? WHERE credential_id=?",
            (now(), credential_id),
        )
        _receipt(
            connection,
            "credential.delete.pending",
            "protected_credential",
            credential_id,
            "PENDING_OS_STORE_DELETE",
            {"data_preserved": True},
        )
        connection.commit()
    finally:
        connection.close()

    delete_secret(row["target_name"])

    _, connection = _connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE protected_credential_reference SET status='DELETED',updated_at=? WHERE credential_id=? AND identity_id=?",
            (now(), credential_id, identity_id),
        )
        _receipt(
            connection,
            "credential.delete",
            "protected_credential",
            credential_id,
            "PASS",
            {"secret_deleted_from_os_store": True},
        )
        connection.commit()
        return {"credential_id": credential_id, "status": "DELETED", "idempotent": False}
    finally:
        connection.close()


def _upsert_search(connection: sqlite3.Connection, entity_type: str, entity_id: str, title: str, body: str) -> None:
    connection.execute(
        "INSERT INTO local_search_document(entity_type,entity_id,title,body,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(entity_id) DO UPDATE SET entity_type=excluded.entity_type,title=excluded.title,body=excluded.body,updated_at=excluded.updated_at",
        (entity_type, entity_id, title, body, now()),
    )


def create_project(workspace_dir: str | Path, name: str, parent_project_id: str | None = None, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    clean = " ".join(name.split())
    if not clean:
        raise LocalWorkspaceError("PROJECT_NAME_REQUIRED")
    project_id = _stable_id("project", identity_id, clean.casefold(), parent_project_id or "")
    stamp = now()
    _, connection = _connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO local_project VALUES(?,?,?,?,?,?,?,?)",
            (project_id, identity_id, clean, parent_project_id, "ACTIVE", 0, stamp, stamp),
        )
        _upsert_search(connection, "project", project_id, clean, clean)
        connection.commit()
        return dict(connection.execute("SELECT * FROM local_project WHERE project_id=?", (project_id,)).fetchone())
    finally:
        connection.close()


def list_projects(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> list[dict[str, Any]]:
    ensure_local_workspace(workspace_dir)
    _, connection = _connection(workspace_dir)
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM local_project WHERE identity_id=? AND status<>'DELETED' ORDER BY pinned DESC,updated_at DESC,name", (identity_id,))]
    finally:
        connection.close()


def create_chat(workspace_dir: str | Path, title: str, project_id: str | None = None, brain_name: str | None = None, idempotency_key: str | None = None, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    clean = " ".join(title.split()) or "New Chat"
    key = idempotency_key or _stable_id("chat_key", identity_id, project_id or "", clean.casefold(), brain_name or "")
    chat_id = _stable_id("chat", key)
    stamp = now()
    _, connection = _connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO local_chat VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, identity_id, project_id, brain_name, clean, "ACTIVE", 0, 0, None, stamp, stamp),
        )
        _upsert_search(connection, "chat", chat_id, clean, clean)
        connection.commit()
        return dict(connection.execute("SELECT * FROM local_chat WHERE chat_id=?", (chat_id,)).fetchone())
    finally:
        connection.close()


def list_chats(workspace_dir: str | Path, project_id: str | None = None, pinned_only: bool = False, include_archived: bool = False, identity_id: str = DEFAULT_IDENTITY_ID) -> list[dict[str, Any]]:
    ensure_local_workspace(workspace_dir)
    clauses = ["identity_id=?", "status='ACTIVE'"]
    values: list[Any] = [identity_id]
    if project_id is not None:
        clauses.append("project_id=?")
        values.append(project_id)
    if pinned_only:
        clauses.append("pinned=1")
    if not include_archived:
        clauses.append("archived=0")
    _, connection = _connection(workspace_dir)
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM local_chat WHERE {' AND '.join(clauses)} ORDER BY pinned DESC,updated_at DESC", values)]
    finally:
        connection.close()


def mutate_chat(workspace_dir: str | Path, chat_id: str, operation: str, value: Any = None, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute("SELECT * FROM local_chat WHERE chat_id=? AND identity_id=?", (chat_id, identity_id)).fetchone()
        if not row:
            raise LocalWorkspaceError("CHAT_NOT_FOUND")
        stamp = now()
        connection.execute("BEGIN IMMEDIATE")
        if operation == "rename":
            title = " ".join(str(value or "").split())
            if not title:
                raise LocalWorkspaceError("CHAT_TITLE_REQUIRED")
            connection.execute("UPDATE local_chat SET title=?,updated_at=? WHERE chat_id=?", (title, stamp, chat_id))
            _upsert_search(connection, "chat", chat_id, title, title)
        elif operation in {"pin", "unpin"}:
            connection.execute("UPDATE local_chat SET pinned=?,updated_at=? WHERE chat_id=?", (1 if operation == "pin" else 0, stamp, chat_id))
        elif operation in {"move", "detach"}:
            connection.execute("UPDATE local_chat SET project_id=?,updated_at=? WHERE chat_id=?", (value if operation == "move" else None, stamp, chat_id))
        elif operation in {"archive", "unarchive"}:
            connection.execute("UPDATE local_chat SET archived=?,updated_at=? WHERE chat_id=?", (1 if operation == "archive" else 0, stamp, chat_id))
        elif operation == "mark_read":
            connection.execute("UPDATE local_chat SET last_read_at=?,updated_at=? WHERE chat_id=?", (stamp, stamp, chat_id))
        else:
            raise LocalWorkspaceError("CHAT_OPERATION_UNKNOWN")
        _receipt(connection, f"chat.{operation}", "chat", chat_id, "PASS", value)
        connection.commit()
        return dict(connection.execute("SELECT * FROM local_chat WHERE chat_id=?", (chat_id,)).fetchone())
    finally:
        connection.close()


def delete_chat(workspace_dir: str | Path, chat_id: str, confirm_chat_id: str, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    if chat_id != confirm_chat_id:
        raise LocalWorkspaceError("CHAT_DELETE_CONFIRMATION_MISMATCH")
    _, connection = _connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT chat_id FROM local_chat WHERE chat_id=? AND identity_id=?", (chat_id, identity_id)).fetchone()
        if not row:
            raise LocalWorkspaceError("CHAT_NOT_FOUND")
        connection.execute("DELETE FROM local_lineage_event WHERE chat_id=?", (chat_id,))
        connection.execute("DELETE FROM local_search_document WHERE entity_id IN (SELECT message_id FROM local_message WHERE chat_id=?)", (chat_id,))
        connection.execute("DELETE FROM local_search_document WHERE entity_id=?", (chat_id,))
        connection.execute("DELETE FROM local_chat WHERE chat_id=?", (chat_id,))
        _receipt(connection, "chat.delete", "chat", chat_id, "PASS", {"confirmed": True})
        connection.commit()
        return {"chat_id": chat_id, "status": "DELETED"}
    finally:
        connection.close()


def append_message(workspace_dir: str | Path, chat_id: str, role: str, content_exact: str, parent_message_id: str | None = None, status: str = "COMPLETED", usage: dict[str, Any] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    if role not in {"system", "user", "assistant", "tool"}:
        raise LocalWorkspaceError("MESSAGE_ROLE_INVALID")
    if not content_exact:
        raise LocalWorkspaceError("MESSAGE_CONTENT_REQUIRED")
    content_hash = hashlib.sha256(content_exact.encode("utf-8", "surrogatepass")).hexdigest()
    key = idempotency_key or _stable_id("message_key", chat_id, role, parent_message_id or "", content_hash)
    _, connection = _connection(workspace_dir)
    try:
        existing = connection.execute("SELECT * FROM local_message WHERE chat_id=? AND idempotency_key=?", (chat_id, key)).fetchone()
        if existing:
            return {**dict(existing), "idempotent_replay": True}
        if not connection.execute("SELECT 1 FROM local_chat WHERE chat_id=?", (chat_id,)).fetchone():
            raise LocalWorkspaceError("CHAT_NOT_FOUND")
        if parent_message_id and not connection.execute("SELECT 1 FROM local_message WHERE message_id=? AND chat_id=?", (parent_message_id, chat_id)).fetchone():
            raise LocalWorkspaceError("MESSAGE_PARENT_NOT_FOUND")
        sequence = int(connection.execute("SELECT COALESCE(MAX(sequence_no),0)+1 FROM local_message WHERE chat_id=?", (chat_id,)).fetchone()[0])
        message_id = _stable_id("message", key)
        stamp = now()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO local_message VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, chat_id, parent_message_id, sequence, role, content_exact, content_hash, status, _json(usage or {}), key, stamp, stamp),
        )
        chat_title = connection.execute("SELECT title FROM local_chat WHERE chat_id=?", (chat_id,)).fetchone()[0]
        _upsert_search(connection, "message", message_id, chat_title, content_exact)
        connection.execute("UPDATE local_chat SET updated_at=? WHERE chat_id=?", (stamp, chat_id))
        connection.commit()
        return {**dict(connection.execute("SELECT * FROM local_message WHERE message_id=?", (message_id,)).fetchone()), "idempotent_replay": False}
    finally:
        connection.close()


def link_attachment(workspace_dir: str | Path, message_id: str, path: str | Path, relation_kind: str = "source", identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file() or not source.is_relative_to(workspace):
        raise LocalWorkspaceError("ATTACHMENT_OUTSIDE_WORKSPACE_OR_NOT_FILE")
    digest = file_sha256(source)
    attachment_id = _stable_id("attachment", identity_id, source, digest)
    stamp = now()
    _, connection = _connection(workspace)
    try:
        if not connection.execute("SELECT 1 FROM local_message WHERE message_id=?", (message_id,)).fetchone():
            raise LocalWorkspaceError("MESSAGE_NOT_FOUND")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT OR IGNORE INTO local_attachment VALUES(?,?,?,?,?,?,?,?,?)",
            (attachment_id, identity_id, str(source), source.name, mimetypes.guess_type(source.name)[0], source.stat().st_size, digest, "AVAILABLE", stamp),
        )
        connection.execute(
            "INSERT OR IGNORE INTO local_message_attachment VALUES(?,?,?,?)",
            (message_id, attachment_id, relation_kind, stamp),
        )
        connection.commit()
        return {"attachment_id": attachment_id, "message_id": message_id, "path": str(source), "sha256": digest, "status": "AVAILABLE"}
    finally:
        connection.close()


def search_workspace(workspace_dir: str | Path, query: str, limit: int = 50) -> list[dict[str, Any]]:
    clean = " ".join(query.split())
    if not clean:
        return []
    _, connection = _connection(workspace_dir)
    try:
        return [
            dict(row) for row in connection.execute(
                "SELECT d.entity_type,d.entity_id,d.title,snippet(local_search_fts,1,'[',']',' … ',24) AS snippet "
                "FROM local_search_fts JOIN local_search_document d ON d.row_id=local_search_fts.rowid "
                "WHERE local_search_fts MATCH ? ORDER BY rank LIMIT ?",
                (clean, max(1, min(int(limit), 200))),
            )
        ]
    finally:
        connection.close()


def get_session_state(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute("SELECT * FROM local_session_state WHERE identity_id=?", (identity_id,)).fetchone()
        data = dict(row)
        data["expanded_project_ids"] = json.loads(data.pop("expanded_project_ids_json"))
        return data
    finally:
        connection.close()


def update_session_state(workspace_dir: str | Path, values: dict[str, Any], identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    ensure_local_workspace(workspace_dir)
    allowed = {"active_session_id", "selected_project_id", "selected_chat_id", "sidebar_mode", "expanded_project_ids", "search_open", "search_query", "connection_state", "loading_state", "terminal_status", "terminal_error_code"}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise LocalWorkspaceError("SESSION_FIELDS_FORBIDDEN:" + ",".join(unknown))
    normalized = dict(values)
    if normalized.get("sidebar_mode") not in {None, "expanded", "collapsed"}:
        raise LocalWorkspaceError("SIDEBAR_MODE_INVALID")
    if "expanded_project_ids" in normalized:
        normalized["expanded_project_ids_json"] = _json(normalized.pop("expanded_project_ids"))
    if "search_open" in normalized:
        normalized["search_open"] = int(bool(normalized["search_open"]))
    normalized["updated_at"] = now()
    _, connection = _connection(workspace_dir)
    try:
        assignments = ",".join(f"{key}=?" for key in normalized)
        connection.execute(f"UPDATE local_session_state SET {assignments} WHERE identity_id=?", (*normalized.values(), identity_id))
        connection.commit()
    finally:
        connection.close()
    return get_session_state(workspace_dir, identity_id)


def append_chat_lineage(workspace_dir: str | Path, chat_id: str, prompt_exact: str, output_exact: str, links: list[dict[str, Any]] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    links_json = _json(links or [])
    prompt_hash = hashlib.sha256(prompt_exact.encode("utf-8", "surrogatepass")).hexdigest()
    output_hash = hashlib.sha256(output_exact.encode("utf-8", "surrogatepass")).hexdigest()
    links_hash = hashlib.sha256(links_json.encode("utf-8")).hexdigest()
    key = idempotency_key or _stable_id("lineage_key", chat_id, prompt_hash, output_hash, links_hash)
    workspace, connection = _connection(workspace_dir)
    try:
        existing = connection.execute("SELECT * FROM local_lineage_event WHERE idempotency_key=?", (key,)).fetchone()
        if existing:
            return {**dict(existing), "idempotent_replay": True}
        chat = connection.execute("SELECT brain_name FROM local_chat WHERE chat_id=?", (chat_id,)).fetchone()
        if not chat:
            raise LocalWorkspaceError("CHAT_NOT_FOUND")
        prior = connection.execute("SELECT event_hash FROM local_lineage_event WHERE chat_id=? ORDER BY created_at DESC,lineage_id DESC LIMIT 1", (chat_id,)).fetchone()
        previous_hash = prior[0] if prior else "0" * 64
        event_hash = hashlib.sha256("\x1f".join((previous_hash, prompt_hash, output_hash, links_hash)).encode()).hexdigest()
        env15_turn_id = None
        brain_name = chat["brain_name"]
        if brain_name:
            root = brain_output_dir(workspace, brain_name)
            if (root / ".uepc_env").is_file():
                env15 = append_env15_chat_lineage_source(
                    root,
                    {
                        "text": prompt_exact,
                        "assistant_response": output_exact,
                        "visible_reasoning_summary": "Evidence OS local chat exact prompt/output/link writeback.",
                        "idempotency_key": key,
                        "attachments": links or [],
                        "source_classification": "CURRENT_LOCAL_CHAT",
                    },
                )
                env15_turn_id = env15.get("turn_id")
        lineage_id = _stable_id("lineage", key)
        stamp = now()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO local_lineage_event VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lineage_id, chat_id, prompt_exact, output_exact, links_json, prompt_hash, output_hash, links_hash, previous_hash, event_hash, key, env15_turn_id, stamp),
        )
        connection.commit()
        return {**dict(connection.execute("SELECT * FROM local_lineage_event WHERE lineage_id=?", (lineage_id,)).fetchone()), "idempotent_replay": False}
    finally:
        connection.close()


def local_snapshot(workspace_dir: str | Path, identity_id: str = DEFAULT_IDENTITY_ID) -> dict[str, Any]:
    return {
        "profile": get_profile(workspace_dir, identity_id),
        "settings": get_settings(workspace_dir, identity_id),
        "projects": list_projects(workspace_dir, identity_id),
        "chats": list_chats(workspace_dir, identity_id=identity_id),
        "pinned_chats": list_chats(workspace_dir, pinned_only=True, identity_id=identity_id),
        "session": get_session_state(workspace_dir, identity_id),
    }


__all__ = [name for name in globals() if not name.startswith("_")]
