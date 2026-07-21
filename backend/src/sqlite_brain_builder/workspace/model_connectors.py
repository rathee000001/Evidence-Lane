from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlite_brain_builder.core import now
from sqlite_brain_builder.runtime.path_policy import normalize_workspace_dir, workspace_db_path
from sqlite_brain_builder.storage.sqlite_utils import connect
from sqlite_brain_builder.workspace.local_workspace import (
    DEFAULT_IDENTITY_ID,
    ensure_local_workspace,
    read_protected_credential,
)


class ModelConnectorError(RuntimeError):
    pass


ENDPOINT_TYPES = {
    "OLLAMA_LOCAL",
    "OPENAI_COMPATIBLE",
    "ENTERPRISE_GATEWAY",
    "CUSTOM_REST",
    "MANUAL_PACKAGE_HANDOFF",
}
CUSTOM_PROMPT_BAR_TYPES = {"OPENAI_COMPATIBLE", "ENTERPRISE_GATEWAY", "CUSTOM_REST"}
AUTHENTICATION_TYPES = {
    "NONE",
    "BEARER_REFERENCE",
    "API_KEY_REFERENCE",
    "BASIC_REFERENCE",
    "CUSTOM_REFERENCE",
}
LOCALITY_CLASSES = {"LOCAL", "REMOTE"}
CONNECTION_MODES = {"LOCAL", "EXTERNAL"}
API_TYPES = {"CHAT_COMPLETIONS", "RESPONSES", "OLLAMA", "CUSTOM_REST"}
CONNECTION_CONFIG_FIELDS = {
    "connection_mode",
    "provider",
    "api_type",
    "prefix_id",
    "model_ids",
    "tags",
    "headers",
}
PROFILE_FIELDS = {
    "endpoint_id",
    "display_name",
    "endpoint_type",
    "base_url",
    "model_id",
    "health_check_path",
    "request_path",
    "model_list_path",
    "authentication_type",
    "authentication_reference",
    "streaming_supported",
    "tool_call_supported",
    "file_supported",
    "context_limit",
    "request_mapping",
    "response_mapping",
    "usage_mapping",
    "timeout",
    "local_or_remote",
    "privacy_classification",
    "enabled",
    "active_prompt_bar",
} | CONNECTION_CONFIG_FIELDS
REQUIRED_PROFILE_FIELDS = PROFILE_FIELDS - {"model_list_path", "authentication_reference"}
REQUIRED_PROFILE_FIELDS -= CONNECTION_CONFIG_FIELDS
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|secret|api_?key|access_?token|authorization|private_?key)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:^|\s)(?:Bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[opusr]_[A-Za-z0-9]{12,}|xox[baprs]-\S+|AIza[0-9A-Za-z_-]{12,})",
    re.IGNORECASE,
)
_MAX_DISCOVERY_RESPONSE_BYTES = 4 * 1024 * 1024


DiscoveryTransport = Callable[[str, dict[str, str], float], Any]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise HTTPError(req.full_url, code, "MODEL_CONNECTOR_REDIRECT_FORBIDDEN", headers, fp)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: object) -> str:
    token = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]}"


def _assert_no_secret_material(value: Any, path: str = "profile") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized != "authentication_reference" and _SECRET_KEY.search(normalized):
                raise ModelConnectorError(f"RAW_SECRET_MATERIAL_FORBIDDEN:{path}.{key}")
            _assert_no_secret_material(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secret_material(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise ModelConnectorError(f"RAW_SECRET_MATERIAL_FORBIDDEN:{path}")


def _require_text(profile: Mapping[str, Any], field: str) -> str:
    value = str(profile.get(field) or "").strip()
    if not value:
        raise ModelConnectorError(f"MODEL_CONNECTOR_FIELD_REQUIRED:{field}")
    return value


def _require_bool(profile: Mapping[str, Any], field: str) -> bool:
    value = profile.get(field)
    if not isinstance(value, bool):
        raise ModelConnectorError(f"MODEL_CONNECTOR_BOOLEAN_REQUIRED:{field}")
    return value


def _require_mapping(profile: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = profile.get(field)
    if not isinstance(value, Mapping):
        raise ModelConnectorError(f"MODEL_CONNECTOR_MAPPING_REQUIRED:{field}")
    return dict(value)


def _string_list(value: Any, field: str, *, maximum: int = 256) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ModelConnectorError(f"MODEL_CONNECTOR_STRING_LIST_REQUIRED:{field}")
    if len(value) > maximum:
        raise ModelConnectorError(f"MODEL_CONNECTOR_STRING_LIST_TOO_LARGE:{field}")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or len(text) > 256:
            raise ModelConnectorError(f"MODEL_CONNECTOR_STRING_LIST_ITEM_INVALID:{field}")
        identity = text.casefold()
        if identity not in seen:
            result.append(text)
            seen.add(identity)
    return result


def _headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ModelConnectorError("MODEL_CONNECTOR_HEADERS_MAPPING_REQUIRED")
    if len(value) > 64:
        raise ModelConnectorError("MODEL_CONNECTOR_HEADERS_TOO_LARGE")
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip()
        header_value = str(raw_value or "").strip()
        if not _HEADER_NAME.fullmatch(name) or not header_value or len(header_value) > 4096:
            raise ModelConnectorError("MODEL_CONNECTOR_HEADER_INVALID")
        if name.casefold() in {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}:
            raise ModelConnectorError("MODEL_CONNECTOR_SECRET_HEADER_MUST_USE_PROTECTED_REFERENCE")
        result[name] = header_value
    _assert_no_secret_material(result, "profile.headers")
    return result


def _is_loopback_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().strip("[]").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    _assert_no_secret_material(profile)
    unknown = sorted(set(profile) - PROFILE_FIELDS)
    if unknown:
        raise ModelConnectorError("MODEL_CONNECTOR_FIELDS_UNKNOWN:" + ",".join(unknown))
    missing = sorted(field for field in REQUIRED_PROFILE_FIELDS if field not in profile)
    if missing:
        raise ModelConnectorError("MODEL_CONNECTOR_FIELDS_MISSING:" + ",".join(missing))

    endpoint_id = _require_text(profile, "endpoint_id")
    if not _ID.fullmatch(endpoint_id):
        raise ModelConnectorError("MODEL_CONNECTOR_ID_INVALID")
    endpoint_type = _require_text(profile, "endpoint_type").upper()
    if endpoint_type not in ENDPOINT_TYPES:
        raise ModelConnectorError("MODEL_CONNECTOR_TYPE_INVALID")
    base_url = _require_text(profile, "base_url")
    split = urlsplit(base_url)
    if endpoint_type == "MANUAL_PACKAGE_HANDOFF":
        if split.scheme not in {"manual", "file"}:
            raise ModelConnectorError("MODEL_CONNECTOR_BASE_URL_INVALID")
    elif split.scheme not in {"http", "https"} or not split.netloc:
        raise ModelConnectorError("MODEL_CONNECTOR_BASE_URL_INVALID")

    authentication_type = _require_text(profile, "authentication_type").upper()
    if authentication_type not in AUTHENTICATION_TYPES:
        raise ModelConnectorError("MODEL_CONNECTOR_AUTHENTICATION_TYPE_INVALID")
    authentication_reference = profile.get("authentication_reference")
    if authentication_reference is not None:
        authentication_reference = str(authentication_reference).strip() or None
    context_limit = profile.get("context_limit")
    timeout_seconds = profile.get("timeout")
    if isinstance(context_limit, bool) or not isinstance(context_limit, int) or context_limit <= 0:
        raise ModelConnectorError("MODEL_CONNECTOR_CONTEXT_LIMIT_INVALID")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 600:
        raise ModelConnectorError("MODEL_CONNECTOR_TIMEOUT_INVALID")
    local_or_remote = _require_text(profile, "local_or_remote").upper()
    if local_or_remote not in LOCALITY_CLASSES:
        raise ModelConnectorError("MODEL_CONNECTOR_LOCALITY_INVALID")
    connection_mode = str(
        profile.get("connection_mode")
        or ("LOCAL" if local_or_remote == "LOCAL" else "EXTERNAL")
    ).strip().upper()
    if connection_mode not in CONNECTION_MODES:
        raise ModelConnectorError("MODEL_CONNECTOR_CONNECTION_MODE_INVALID")
    expected_locality = "LOCAL" if connection_mode == "LOCAL" else "REMOTE"
    if local_or_remote != expected_locality:
        raise ModelConnectorError("MODEL_CONNECTOR_CONNECTION_MODE_LOCALITY_MISMATCH")
    if connection_mode == "LOCAL" and not _is_loopback_host(split.hostname):
        raise ModelConnectorError("LOCAL_CONNECTION_REQUIRES_LOOPBACK_HOST")
    provider = str(profile.get("provider") or "default").strip()
    if not provider or len(provider) > 128:
        raise ModelConnectorError("MODEL_CONNECTOR_PROVIDER_INVALID")
    api_type = str(
        profile.get("api_type")
        or ("OLLAMA" if endpoint_type == "OLLAMA_LOCAL" else "CHAT_COMPLETIONS")
    ).strip().upper()
    if api_type not in API_TYPES:
        raise ModelConnectorError("MODEL_CONNECTOR_API_TYPE_INVALID")
    if endpoint_type == "OLLAMA_LOCAL" and api_type != "OLLAMA":
        raise ModelConnectorError("MODEL_CONNECTOR_OLLAMA_API_TYPE_REQUIRED")
    prefix_id = str(profile.get("prefix_id") or "").strip() or None
    if prefix_id is not None and not _ID.fullmatch(prefix_id):
        raise ModelConnectorError("MODEL_CONNECTOR_PREFIX_ID_INVALID")
    active_prompt_bar = _require_bool(profile, "active_prompt_bar")
    if active_prompt_bar and endpoint_type not in CUSTOM_PROMPT_BAR_TYPES:
        raise ModelConnectorError("MODEL_CONNECTOR_PROMPT_BAR_TYPE_FORBIDDEN")

    return {
        "endpoint_id": endpoint_id,
        "display_name": _require_text(profile, "display_name"),
        "endpoint_type": endpoint_type,
        "base_url": base_url,
        "model_id": _require_text(profile, "model_id"),
        "health_check_path": _require_text(profile, "health_check_path"),
        "request_path": _require_text(profile, "request_path"),
        "model_list_path": str(profile.get("model_list_path") or "").strip() or None,
        "authentication_type": authentication_type,
        "authentication_reference": authentication_reference,
        "streaming_supported": _require_bool(profile, "streaming_supported"),
        "tool_call_supported": _require_bool(profile, "tool_call_supported"),
        "file_supported": _require_bool(profile, "file_supported"),
        "context_limit": context_limit,
        "request_mapping": _require_mapping(profile, "request_mapping"),
        "response_mapping": _require_mapping(profile, "response_mapping"),
        "usage_mapping": _require_mapping(profile, "usage_mapping"),
        "timeout": timeout_seconds,
        "local_or_remote": local_or_remote,
        "privacy_classification": _require_text(profile, "privacy_classification"),
        "enabled": _require_bool(profile, "enabled"),
        "active_prompt_bar": active_prompt_bar,
        "connection_mode": connection_mode,
        "provider": provider,
        "api_type": api_type,
        "prefix_id": prefix_id,
        "model_ids": _string_list(profile.get("model_ids"), "model_ids"),
        "tags": _string_list(profile.get("tags"), "tags", maximum=64),
        "headers": _headers(profile.get("headers")),
    }


def _connection(workspace_dir: str | Path) -> tuple[Path, sqlite3.Connection]:
    workspace = normalize_workspace_dir(workspace_dir)
    ensure_local_workspace(workspace)
    connection = connect(workspace_db_path(workspace))
    connection.row_factory = sqlite3.Row
    return workspace, connection


def _receipt(
    connection: sqlite3.Connection,
    operation: str,
    endpoint_id: str,
    identity_id: str,
    detail: Mapping[str, Any],
) -> None:
    created_at = now()
    receipt_id = _stable_id("receipt", operation, endpoint_id, identity_id, created_at)
    connection.execute(
        "INSERT INTO local_operation_receipt VALUES(?,?,?,?,?,?,?)",
        (receipt_id, operation, "model_connector", endpoint_id, "PASS", _json(dict(detail)), created_at),
    )


def _payload(row: sqlite3.Row, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    data = dict(row)
    for field in ("streaming_supported", "tool_call_supported", "file_supported", "enabled", "active_prompt_bar"):
        data[field] = bool(data[field])
    data["request_mapping"] = json.loads(data.pop("request_mapping_json"))
    data["response_mapping"] = json.loads(data.pop("response_mapping_json"))
    data["usage_mapping"] = json.loads(data.pop("usage_mapping_json"))
    data["timeout"] = data.pop("timeout_seconds")
    config = None
    if connection is not None:
        config = connection.execute(
            "SELECT * FROM endpoint_connection_config WHERE endpoint_id=?",
            (data["endpoint_id"],),
        ).fetchone()
    if config is None:
        data.update(
            {
                "connection_mode": "LOCAL" if data["local_or_remote"] == "LOCAL" else "EXTERNAL",
                "provider": "default",
                "api_type": "OLLAMA" if data["endpoint_type"] == "OLLAMA_LOCAL" else "CHAT_COMPLETIONS",
                "prefix_id": None,
                "model_ids": [],
                "tags": [],
                "headers": {},
            }
        )
    else:
        config_data = dict(config)
        data.update(
            {
                "connection_mode": config_data["connection_mode"],
                "provider": config_data["provider"],
                "api_type": config_data["api_type"],
                "prefix_id": config_data["prefix_id"],
                "model_ids": json.loads(config_data["model_ids_json"] or "[]"),
                "tags": json.loads(config_data["tags_json"] or "[]"),
                "headers": json.loads(config_data["headers_json"] or "{}"),
            }
        )
    discovered_models: list[dict[str, Any]] = []
    if connection is not None:
        cached = connection.execute(
            "SELECT raw_model_id,routed_model_id,model_json,discovered_at "
            "FROM endpoint_model_cache WHERE endpoint_id=? ORDER BY rowid",
            (data["endpoint_id"],),
        ).fetchall()
        for item in cached:
            model = json.loads(item["model_json"] or "{}")
            discovered_models.append(
                {
                    **(model if isinstance(model, dict) else {}),
                    "raw_model_id": item["raw_model_id"],
                    "routed_model_id": item["routed_model_id"],
                    "discovered_at": item["discovered_at"],
                }
            )
    data["discovered_models"] = discovered_models
    return data


def upsert_model_connector(
    workspace_dir: str | Path,
    profile: Mapping[str, Any],
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    normalized = _normalize_profile(profile)
    _, connection = _connection(workspace_dir)
    try:
        existing = connection.execute(
            "SELECT identity_id,created_at FROM endpoint_registry WHERE endpoint_id=?",
            (normalized["endpoint_id"],),
        ).fetchone()
        if existing and existing["identity_id"] != identity_id:
            raise ModelConnectorError("MODEL_CONNECTOR_ID_OWNED_BY_ANOTHER_IDENTITY")
        reference = normalized["authentication_reference"]
        if reference:
            credential = connection.execute(
                "SELECT 1 FROM protected_credential_reference "
                "WHERE credential_id=? AND identity_id=? AND status='ACTIVE'",
                (reference, identity_id),
            ).fetchone()
            if not credential:
                raise ModelConnectorError("MODEL_CONNECTOR_CREDENTIAL_REFERENCE_NOT_FOUND")
        stamp = now()
        connection.execute("BEGIN IMMEDIATE")
        if normalized["active_prompt_bar"]:
            connection.execute(
                "UPDATE endpoint_registry SET active_prompt_bar=0,updated_at=? "
                "WHERE identity_id=? AND active_prompt_bar=1 AND endpoint_id<>?",
                (stamp, identity_id, normalized["endpoint_id"]),
            )
        columns = (
            "endpoint_id", "identity_id", "display_name", "endpoint_type", "base_url", "model_id",
            "health_check_path", "request_path", "model_list_path", "authentication_type",
            "authentication_reference", "streaming_supported", "tool_call_supported", "file_supported",
            "context_limit", "request_mapping_json", "response_mapping_json", "usage_mapping_json",
            "timeout_seconds", "local_or_remote", "privacy_classification", "enabled",
            "active_prompt_bar", "created_at", "updated_at",
        )
        values = (
            normalized["endpoint_id"], identity_id, normalized["display_name"], normalized["endpoint_type"],
            normalized["base_url"], normalized["model_id"], normalized["health_check_path"],
            normalized["request_path"], normalized["model_list_path"], normalized["authentication_type"],
            reference, int(normalized["streaming_supported"]), int(normalized["tool_call_supported"]),
            int(normalized["file_supported"]), normalized["context_limit"],
            _json(normalized["request_mapping"]), _json(normalized["response_mapping"]),
            _json(normalized["usage_mapping"]), normalized["timeout"], normalized["local_or_remote"],
            normalized["privacy_classification"], int(normalized["enabled"]),
            int(normalized["active_prompt_bar"]), existing["created_at"] if existing else stamp, stamp,
        )
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"endpoint_id", "identity_id", "created_at"}
        )
        connection.execute(
            f"INSERT INTO endpoint_registry({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
            f"ON CONFLICT(endpoint_id) DO UPDATE SET {updates}",
            values,
        )
        existing_config = connection.execute(
            "SELECT created_at FROM endpoint_connection_config WHERE endpoint_id=?",
            (normalized["endpoint_id"],),
        ).fetchone()
        connection.execute(
            "INSERT INTO endpoint_connection_config("
            "endpoint_id,connection_mode,provider,api_type,prefix_id,model_ids_json,tags_json,headers_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(endpoint_id) DO UPDATE SET "
            "connection_mode=excluded.connection_mode,provider=excluded.provider,api_type=excluded.api_type,"
            "prefix_id=excluded.prefix_id,model_ids_json=excluded.model_ids_json,tags_json=excluded.tags_json,"
            "headers_json=excluded.headers_json,updated_at=excluded.updated_at",
            (
                normalized["endpoint_id"],
                normalized["connection_mode"],
                normalized["provider"],
                normalized["api_type"],
                normalized["prefix_id"],
                _json(normalized["model_ids"]),
                _json(normalized["tags"]),
                _json(normalized["headers"]),
                existing_config["created_at"] if existing_config else stamp,
                stamp,
            ),
        )
        _receipt(
            connection,
            "model.connector.upsert",
            normalized["endpoint_id"],
            identity_id,
            {
                "endpoint_type": normalized["endpoint_type"],
                "enabled": normalized["enabled"],
                "active_prompt_bar": normalized["active_prompt_bar"],
                "authentication_reference_present": bool(reference),
            },
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM endpoint_registry WHERE endpoint_id=? AND identity_id=?",
            (normalized["endpoint_id"], identity_id),
        ).fetchone()
        return _payload(row, connection)
    finally:
        connection.close()


def list_model_connectors(
    workspace_dir: str | Path,
    identity_id: str = DEFAULT_IDENTITY_ID,
    *,
    include_disabled: bool = False,
) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        where = "identity_id=?" if include_disabled else "identity_id=? AND enabled=1"
        rows = connection.execute(
            f"SELECT * FROM endpoint_registry WHERE {where} "
            "ORDER BY active_prompt_bar DESC,display_name COLLATE NOCASE,endpoint_id",
            (identity_id,),
        ).fetchall()
        return {"identity_id": identity_id, "connectors": [_payload(row, connection) for row in rows]}
    finally:
        connection.close()


def get_model_connector(
    workspace_dir: str | Path,
    endpoint_id: str,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute(
            "SELECT * FROM endpoint_registry WHERE endpoint_id=? AND identity_id=?",
            (endpoint_id, identity_id),
        ).fetchone()
        if not row:
            raise ModelConnectorError("MODEL_CONNECTOR_NOT_FOUND")
        return _payload(row, connection)
    finally:
        connection.close()


def set_active_model_connector(
    workspace_dir: str | Path,
    endpoint_id: str,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute(
            "SELECT endpoint_type,enabled FROM endpoint_registry WHERE endpoint_id=? AND identity_id=?",
            (endpoint_id, identity_id),
        ).fetchone()
        if not row:
            raise ModelConnectorError("MODEL_CONNECTOR_NOT_FOUND")
        if not row["enabled"]:
            raise ModelConnectorError("MODEL_CONNECTOR_DISABLED")
        if row["endpoint_type"] not in CUSTOM_PROMPT_BAR_TYPES:
            raise ModelConnectorError("MODEL_CONNECTOR_PROMPT_BAR_TYPE_FORBIDDEN")
        stamp = now()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE endpoint_registry SET active_prompt_bar=0,updated_at=? "
            "WHERE identity_id=? AND active_prompt_bar=1",
            (stamp, identity_id),
        )
        connection.execute(
            "UPDATE endpoint_registry SET active_prompt_bar=1,updated_at=? WHERE endpoint_id=? AND identity_id=?",
            (stamp, endpoint_id, identity_id),
        )
        _receipt(connection, "model.connector.setActive", endpoint_id, identity_id, {"active_prompt_bar": True})
        connection.commit()
    finally:
        connection.close()
    return get_model_connector(workspace_dir, endpoint_id, identity_id)


def disable_model_connector(
    workspace_dir: str | Path,
    endpoint_id: str,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    _, connection = _connection(workspace_dir)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE endpoint_registry SET enabled=0,active_prompt_bar=0,updated_at=? "
            "WHERE endpoint_id=? AND identity_id=?",
            (now(), endpoint_id, identity_id),
        ).rowcount
        if changed != 1:
            raise ModelConnectorError("MODEL_CONNECTOR_NOT_FOUND")
        _receipt(
            connection,
            "model.connector.disable",
            endpoint_id,
            identity_id,
            {"enabled": False, "active_prompt_bar": False},
        )
        connection.commit()
    finally:
        connection.close()
    return get_model_connector(workspace_dir, endpoint_id, identity_id)


def _discovery_url(profile: Mapping[str, Any]) -> str:
    base = urlsplit(str(profile.get("base_url") or ""))
    path = str(profile.get("model_list_path") or profile.get("health_check_path") or "").strip()
    parsed_path = urlsplit(path)
    if base.scheme not in {"http", "https"} or not base.netloc or base.username or base.password:
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_BASE_URL_INVALID")
    if not path.startswith("/") or parsed_path.scheme or parsed_path.netloc or parsed_path.query or parsed_path.fragment:
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_PATH_INVALID")
    if str(profile.get("connection_mode") or "EXTERNAL") == "LOCAL" and not _is_loopback_host(base.hostname):
        raise ModelConnectorError("LOCAL_CONNECTION_REQUIRES_LOOPBACK_HOST")
    return urljoin(str(profile["base_url"]).rstrip("/") + "/", path.lstrip("/"))


def _authentication_headers(
    workspace: Path,
    profile: Mapping[str, Any],
    identity_id: str,
) -> dict[str, str]:
    result = dict(profile.get("headers") or {})
    authentication_type = str(profile.get("authentication_type") or "NONE")
    if authentication_type == "NONE":
        return result
    reference = str(profile.get("authentication_reference") or "").strip()
    if not reference:
        raise ModelConnectorError("MODEL_CONNECTOR_AUTHENTICATION_REFERENCE_REQUIRED")
    try:
        secret = str(read_protected_credential(workspace, reference, identity_id)["secret"])
    except Exception as exc:
        raise ModelConnectorError("MODEL_CONNECTOR_PROTECTED_CREDENTIAL_UNAVAILABLE") from exc
    if authentication_type == "BEARER_REFERENCE":
        result["Authorization"] = f"Bearer {secret}"
    elif authentication_type == "API_KEY_REFERENCE":
        result["X-API-Key"] = secret
    elif authentication_type == "BASIC_REFERENCE":
        import base64

        result["Authorization"] = "Basic " + base64.b64encode(secret.encode("utf-8")).decode("ascii")
    elif authentication_type == "CUSTOM_REFERENCE":
        result["Authorization"] = secret
    else:  # pragma: no cover - normalization already rejects this
        raise ModelConnectorError("MODEL_CONNECTOR_AUTHENTICATION_TYPE_INVALID")
    return result


def _default_discovery_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "EvidenceOS-T023-Connector/1", **headers},
        method="GET",
    )
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            raw = response.read(_MAX_DISCOVERY_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise ModelConnectorError(f"MODEL_CONNECTOR_HTTP_ERROR:{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ModelConnectorError(f"MODEL_CONNECTOR_UNREACHABLE:{type(exc).__name__}") from exc
    if len(raw) > _MAX_DISCOVERY_RESPONSE_BYTES:
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_RESPONSE_TOO_LARGE")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_RESPONSE_INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_RESPONSE_OBJECT_REQUIRED")
    return status, payload


def _transport_response(value: Any) -> tuple[int, dict[str, Any]]:
    if isinstance(value, tuple) and len(value) == 2:
        status, payload = value
    else:
        status, payload = 200, value
    if isinstance(status, bool) or not isinstance(status, int):
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_HTTP_STATUS_INVALID")
    if not isinstance(payload, Mapping):
        raise ModelConnectorError("MODEL_CONNECTOR_DISCOVERY_RESPONSE_OBJECT_REQUIRED")
    return status, dict(payload)


def _discovered_models(profile: Mapping[str, Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    endpoint_type = str(profile.get("endpoint_type") or "")
    raw_rows = payload.get("models") if endpoint_type == "OLLAMA_LOCAL" else payload.get("data")
    if not isinstance(raw_rows, list):
        expected = "models" if endpoint_type == "OLLAMA_LOCAL" else "data"
        raise ModelConnectorError(f"MODEL_CONNECTOR_DISCOVERY_LIST_REQUIRED:{expected}")
    allowed = {value.casefold() for value in profile.get("model_ids") or []}
    prefix = str(profile.get("prefix_id") or "").strip()
    tags = list(profile.get("tags") or [])
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if isinstance(raw, str):
            source = {"id": raw}
        elif isinstance(raw, Mapping):
            source = dict(raw)
        else:
            continue
        raw_id = str(source.get("name") if endpoint_type == "OLLAMA_LOCAL" else source.get("id") or source.get("name") or "").strip()
        if not raw_id or raw_id.casefold() in seen:
            continue
        if allowed and raw_id.casefold() not in allowed:
            continue
        seen.add(raw_id.casefold())
        routed_id = f"{prefix}.{raw_id}" if prefix else raw_id
        models.append(
            {
                "raw_model_id": raw_id,
                "routed_model_id": routed_id,
                "provider": str(profile.get("provider") or "default"),
                "api_type": str(profile.get("api_type") or ""),
                "tags": tags,
                "source": source,
            }
        )
    if not models:
        raise ModelConnectorError("MODEL_CONNECTOR_NO_MODELS_DISCOVERED")
    return models


def _validation_event(
    connection: sqlite3.Connection,
    *,
    operation: str,
    endpoint_id: str,
    identity_id: str,
    request_url: str,
    status: str,
    http_status: int | None,
    models: list[dict[str, Any]],
    error_code: str | None,
) -> str:
    stamp = now()
    validation_id = _stable_id("validation", endpoint_id, operation, stamp)
    detail = {
        "operation": operation,
        "connection_test": True,
        "model_ids": [item["routed_model_id"] for item in models],
        "raw_secret_material_persisted": False,
    }
    connection.execute(
        "INSERT INTO endpoint_validation_event("
        "validation_id,endpoint_id,request_url,status,http_status,model_count,error_code,detail_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            validation_id,
            endpoint_id,
            request_url,
            status,
            http_status,
            len(models),
            error_code,
            _json(detail),
            stamp,
        ),
    )
    _receipt(
        connection,
        operation,
        endpoint_id,
        identity_id,
        {
            "validation_id": validation_id,
            "status": status,
            "http_status": http_status,
            "model_count": len(models),
            "error_code": error_code,
        },
    )
    return validation_id


def _discover_or_validate(
    workspace_dir: str | Path,
    endpoint_id: str,
    identity_id: str,
    *,
    operation: str,
    transport: DiscoveryTransport | None,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir)
    profile = get_model_connector(workspace, endpoint_id, identity_id)
    if not profile.get("enabled"):
        raise ModelConnectorError("MODEL_CONNECTOR_DISABLED")
    request_url = _discovery_url(profile)
    headers = _authentication_headers(workspace, profile, identity_id)
    connection = None
    try:
        status, response = _transport_response(
            (transport or _default_discovery_transport)(request_url, headers, float(profile["timeout"]))
        )
        if not 200 <= status < 300:
            raise ModelConnectorError(f"MODEL_CONNECTOR_HTTP_ERROR:{status}")
        models = _discovered_models(profile, response)
        _, connection = _connection(workspace)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM endpoint_model_cache WHERE endpoint_id=?", (endpoint_id,))
        discovered_at = now()
        for model in models:
            connection.execute(
                "INSERT INTO endpoint_model_cache(endpoint_id,raw_model_id,routed_model_id,model_json,discovered_at) "
                "VALUES(?,?,?,?,?)",
                (
                    endpoint_id,
                    model["raw_model_id"],
                    model["routed_model_id"],
                    _json(model),
                    discovered_at,
                ),
            )
        validation_id = _validation_event(
            connection,
            operation=operation,
            endpoint_id=endpoint_id,
            identity_id=identity_id,
            request_url=request_url,
            status="PASS",
            http_status=status,
            models=models,
            error_code=None,
        )
        connection.commit()
        return {
            "status": "PASS",
            "validation_id": validation_id,
            "endpoint_id": endpoint_id,
            "endpoint_type": profile["endpoint_type"],
            "connection_mode": profile["connection_mode"],
            "request_url": request_url,
            "http_status": status,
            "model_count": len(models),
            "models": models,
            "cache_replaced": True,
            "raw_secret_material_persisted": False,
        }
    except Exception as exc:
        error = exc if isinstance(exc, ModelConnectorError) else ModelConnectorError(
            f"MODEL_CONNECTOR_DISCOVERY_FAILED:{type(exc).__name__}"
        )
        if connection is not None:
            connection.rollback()
            connection.close()
            connection = None
        _, failure_connection = _connection(workspace)
        try:
            failure_connection.execute("BEGIN IMMEDIATE")
            error_code = str(error).split(":", 1)[0]
            _validation_event(
                failure_connection,
                operation=operation,
                endpoint_id=endpoint_id,
                identity_id=identity_id,
                request_url=request_url,
                status="FAIL",
                http_status=None,
                models=[],
                error_code=error_code,
            )
            failure_connection.commit()
        finally:
            failure_connection.close()
        if error is exc:
            raise error
        raise error from exc
    finally:
        if connection is not None:
            connection.close()


def discover_model_connector_models(
    workspace_dir: str | Path,
    endpoint_id: str,
    identity_id: str = DEFAULT_IDENTITY_ID,
    *,
    transport: DiscoveryTransport | None = None,
) -> dict[str, Any]:
    return _discover_or_validate(
        workspace_dir,
        endpoint_id,
        identity_id,
        operation="model.connector.discoverModels",
        transport=transport,
    )


def validate_model_connector(
    workspace_dir: str | Path,
    endpoint_id: str,
    identity_id: str = DEFAULT_IDENTITY_ID,
    *,
    transport: DiscoveryTransport | None = None,
) -> dict[str, Any]:
    return _discover_or_validate(
        workspace_dir,
        endpoint_id,
        identity_id,
        operation="model.connector.validate",
        transport=transport,
    )


def delete_model_connector(
    workspace_dir: str | Path,
    endpoint_id: str,
    *,
    confirm_endpoint_id: str,
    identity_id: str = DEFAULT_IDENTITY_ID,
) -> dict[str, Any]:
    if str(confirm_endpoint_id or "").strip() != str(endpoint_id or "").strip():
        raise ModelConnectorError("MODEL_CONNECTOR_DELETE_EXPLICIT_ID_CONFIRMATION_REQUIRED")
    _, connection = _connection(workspace_dir)
    try:
        row = connection.execute(
            "SELECT display_name,authentication_reference FROM endpoint_registry WHERE endpoint_id=? AND identity_id=?",
            (endpoint_id, identity_id),
        ).fetchone()
        if row is None:
            raise ModelConnectorError("MODEL_CONNECTOR_NOT_FOUND")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM endpoint_model_cache WHERE endpoint_id=?", (endpoint_id,))
        connection.execute("DELETE FROM endpoint_connection_config WHERE endpoint_id=?", (endpoint_id,))
        connection.execute("DELETE FROM endpoint_registry WHERE endpoint_id=? AND identity_id=?", (endpoint_id, identity_id))
        _receipt(
            connection,
            "model.connector.delete",
            endpoint_id,
            identity_id,
            {
                "display_name": row["display_name"],
                "protected_credential_reference_preserved": bool(row["authentication_reference"]),
                "raw_secret_material_persisted": False,
            },
        )
        connection.commit()
        return {
            "status": "DELETED",
            "endpoint_id": endpoint_id,
            "display_name": row["display_name"],
            "protected_credential_reference_preserved": bool(row["authentication_reference"]),
        }
    finally:
        connection.close()


__all__ = [
    "ModelConnectorError",
    "CUSTOM_PROMPT_BAR_TYPES",
    "delete_model_connector",
    "disable_model_connector",
    "discover_model_connector_models",
    "get_model_connector",
    "list_model_connectors",
    "set_active_model_connector",
    "upsert_model_connector",
    "validate_model_connector",
]
