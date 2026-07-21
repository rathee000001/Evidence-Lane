from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.workspace import local_workspace, workspace_db


def _counts(workspace: Path) -> dict[str, int]:
    connection = sqlite3.connect(workspace / "workspace.sqlite")
    try:
        names = (
            "local_project", "local_chat", "local_message", "local_attachment",
            "local_message_attachment", "local_lineage_event", "local_search_document", "local_search_fts",
        )
        return {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}
    finally:
        connection.close()


def test_profile_settings_admin_and_session_are_restart_persistent_and_idempotent(tmp_path: Path) -> None:
    first = local_workspace.ensure_local_workspace(tmp_path)
    second = local_workspace.ensure_local_workspace(tmp_path)
    assert first["identity_id"] == second["identity_id"] == local_workspace.DEFAULT_IDENTITY_ID

    profile = local_workspace.update_profile(
        tmp_path,
        {"display_name": "Evidence Lane Operator", "role_label": "Evidence Lane owner", "avatar_ref": "data:image/png;base64,AA=="},
    )
    assert profile["display_name"] == "Evidence Lane Operator"
    settings = local_workspace.update_settings(
        tmp_path,
        "interface",
        {"theme_mode": "dark", "text_scale": 1.25, "reduced_motion": True},
    )
    assert settings["categories"]["interface"]["theme_id"] == "evidence-glass"
    assert settings["categories"]["interface"]["theme_mode"] == "dark"
    assert settings["categories"]["interface"]["text_scale"] == 1.25
    assert settings["categories"]["audio"]["availability"] == "UNAVAILABLE_NOT_CONFIGURED"

    session = local_workspace.update_session_state(
        tmp_path,
        {"sidebar_mode": "collapsed", "search_open": True, "search_query": "sqlite", "expanded_project_ids": ["project_1"]},
    )
    assert session["sidebar_mode"] == "collapsed"
    assert session["expanded_project_ids"] == ["project_1"]

    # Reopen through a fresh connection/API call to prove durable restart state.
    restored_profile = local_workspace.get_profile(tmp_path)
    assert restored_profile["display_name"] == "Evidence Lane Operator"
    assert restored_profile["avatar_ref"] == "data:image/png;base64,AA=="
    restored = local_workspace.get_session_state(tmp_path)
    assert restored["search_open"] == 1
    assert restored["search_query"] == "sqlite"

    admin = local_workspace.update_admin_settings(tmp_path, {"enable_direct_connections": True})
    assert admin["enable_direct_connections"] is True
    with sqlite3.connect(tmp_path / "workspace.sqlite") as connection:
        connection.execute("UPDATE local_identity SET role='user' WHERE identity_id=?", (local_workspace.DEFAULT_IDENTITY_ID,))
    with pytest.raises(local_workspace.LocalWorkspaceError, match="ADMIN_AUTHORITY_REQUIRED"):
        local_workspace.get_admin_settings(tmp_path)

    with sqlite3.connect(tmp_path / "workspace.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_identity").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM local_profile").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_initialized_workspace_reads_are_byte_stable_and_skip_password_rehash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_workspace.ensure_local_workspace(tmp_path)
    database = tmp_path / "workspace.sqlite"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    def unexpected_password_hash(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        raise AssertionError("existing workspace user was unnecessarily rehashed")

    monkeypatch.setattr(workspace_db, "hash_password", unexpected_password_hash)
    workspace_db.init_workspace(tmp_path)
    local_workspace.ensure_local_workspace(tmp_path)
    local_workspace.local_snapshot(tmp_path)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_protected_credentials_store_only_reference_in_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault: dict[str, str] = {}
    monkeypatch.setattr(local_workspace, "set_secret", lambda target, account, secret: vault.__setitem__(target, secret))
    monkeypatch.setattr(local_workspace, "get_secret", lambda target: vault[target])
    monkeypatch.setattr(local_workspace, "delete_secret", lambda target: vault.pop(target, None))

    reference = local_workspace.set_protected_credential(tmp_path, "github", "test-account", "test-only-token")
    assert local_workspace.read_protected_credential(tmp_path, reference["credential_id"])["secret"] == "test-only-token"
    target_name = next(iter(vault))
    assert target_name.startswith("EvidenceOS/identity_local_owner/")
    assert "github" not in target_name
    assert "test-account" not in target_name

    updated = local_workspace.set_protected_credential(tmp_path, "github", "test-account", "replacement-test-token")
    assert updated["credential_id"] == reference["credential_id"]
    assert len(vault) == 1
    assert local_workspace.read_protected_credential(tmp_path, reference["credential_id"])["secret"] == "replacement-test-token"

    status = ipc_worker.handle(
        {
            "command": "credential.status",
            "payload": {"workspace_dir": str(tmp_path), "credential_id": reference["credential_id"]},
        }
    )
    assert status["status"] == "ACTIVE"
    assert "secret" not in status
    assert "target_name" not in status
    with pytest.raises(ipc_worker.WorkerError, match="UNKNOWN_COMMAND: credential.read"):
        ipc_worker.handle(
            {
                "command": "credential.read",
                "payload": {"workspace_dir": str(tmp_path), "credential_id": reference["credential_id"]},
            }
        )

    database_bytes = (tmp_path / "workspace.sqlite").read_bytes()
    assert b"super-secret-token" not in database_bytes
    assert b"replacement-secret-token" not in database_bytes
    exported = json.dumps(local_workspace.export_local_data(tmp_path), sort_keys=True)
    snapshot = json.dumps(local_workspace.local_snapshot(tmp_path), sort_keys=True)
    assert "replacement-secret-token" not in exported
    assert "replacement-secret-token" not in snapshot
    assert reference["credential_id"] not in exported

    with sqlite3.connect(tmp_path / "workspace.sqlite") as connection:
        rows = connection.execute(
            "SELECT operation,detail_json FROM local_operation_receipt "
            "WHERE entity_type='protected_credential' ORDER BY created_at"
        ).fetchall()
        reference_count = connection.execute(
            "SELECT COUNT(*) FROM protected_credential_reference WHERE credential_id=?",
            (reference["credential_id"],),
        ).fetchone()[0]
    assert reference_count == 1
    assert [row[0] for row in rows] == ["credential.set", "credential.set"]
    assert "replacement-secret-token" not in json.dumps(rows)
    assert local_workspace.delete_protected_credential(tmp_path, reference["credential_id"])["status"] == "DELETED"


def test_protected_credential_delete_failure_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault: dict[str, str] = {}
    monkeypatch.setattr(local_workspace, "set_secret", lambda target, account, secret: vault.__setitem__(target, secret))
    monkeypatch.setattr(local_workspace, "get_secret", lambda target: vault[target])
    reference = local_workspace.set_protected_credential(tmp_path, "provider", "account", "secret-value")

    def fail_delete(_target: str) -> None:
        raise RuntimeError("OS_STORE_DELETE_FAILED")

    monkeypatch.setattr(local_workspace, "delete_secret", fail_delete)
    with pytest.raises(RuntimeError, match="OS_STORE_DELETE_FAILED"):
        local_workspace.delete_protected_credential(tmp_path, reference["credential_id"])
    status = local_workspace.get_protected_credential_status(tmp_path, reference["credential_id"])
    assert status["status"] == "DELETE_PENDING"
    with pytest.raises(local_workspace.LocalWorkspaceError, match="CREDENTIAL_REFERENCE_NOT_ACTIVE"):
        local_workspace.read_protected_credential(tmp_path, reference["credential_id"])


def test_project_chat_message_attachment_lineage_search_and_replay_have_zero_growth(tmp_path: Path) -> None:
    project = local_workspace.create_project(tmp_path, "Evidence OS")
    assert local_workspace.create_project(tmp_path, "Evidence OS")["project_id"] == project["project_id"]
    chat = local_workspace.create_chat(
        tmp_path,
        "SQLite Builder correction",
        project["project_id"],
        idempotency_key="chat-stable-1",
    )
    assert local_workspace.create_chat(tmp_path, "SQLite Builder correction", project["project_id"], idempotency_key="chat-stable-1")["chat_id"] == chat["chat_id"]

    prompt = local_workspace.append_message(
        tmp_path,
        chat["chat_id"],
        "user",
        "Fix the deterministic SQLite package flow.",
        idempotency_key="message-prompt-1",
    )
    response = local_workspace.append_message(
        tmp_path,
        chat["chat_id"],
        "assistant",
        "The deterministic package flow is validated.",
        parent_message_id=prompt["message_id"],
        idempotency_key="message-response-1",
        usage={"input_tokens": 7, "output_tokens": 6},
    )
    source = tmp_path / "evidence.txt"
    source.write_text("deterministic source evidence", encoding="utf-8")
    attachment = local_workspace.link_attachment(tmp_path, response["message_id"], source)
    lineage = local_workspace.append_chat_lineage(
        tmp_path,
        chat["chat_id"],
        prompt["content_exact"],
        response["content_exact"],
        [{"attachment_id": attachment["attachment_id"], "sha256": attachment["sha256"]}],
        idempotency_key="lineage-1",
    )
    assert lineage["idempotent_replay"] is False
    assert local_workspace.search_workspace(tmp_path, "deterministic")

    before = _counts(tmp_path)
    assert local_workspace.append_message(tmp_path, chat["chat_id"], "user", prompt["content_exact"], idempotency_key="message-prompt-1")["idempotent_replay"] is True
    assert local_workspace.link_attachment(tmp_path, response["message_id"], source)["attachment_id"] == attachment["attachment_id"]
    assert local_workspace.append_chat_lineage(tmp_path, chat["chat_id"], prompt["content_exact"], response["content_exact"], [{"attachment_id": attachment["attachment_id"], "sha256": attachment["sha256"]}], idempotency_key="lineage-1")["idempotent_replay"] is True
    assert _counts(tmp_path) == before

    pinned = local_workspace.mutate_chat(tmp_path, chat["chat_id"], "pin")
    assert pinned["pinned"] == 1
    assert [item["chat_id"] for item in local_workspace.list_chats(tmp_path, pinned_only=True)] == [chat["chat_id"]]
    assert local_workspace.mutate_chat(tmp_path, chat["chat_id"], "archive")["archived"] == 1
    assert local_workspace.list_chats(tmp_path) == []
    assert local_workspace.list_chats(tmp_path, include_archived=True)[0]["chat_id"] == chat["chat_id"]
    assert local_workspace.mutate_chat(tmp_path, chat["chat_id"], "detach")["project_id"] is None


def test_worker_exposes_native_local_workspace_commands(tmp_path: Path) -> None:
    initialized = ipc_worker.handle({"command": "local.workspace.init", "payload": {"workspace_dir": str(tmp_path)}})
    assert initialized["status"] == "PASS"
    project = ipc_worker.handle({"command": "project.create", "payload": {"workspace_dir": str(tmp_path), "name": "Worker Project"}})
    chat = ipc_worker.handle({"command": "chat.create", "payload": {"workspace_dir": str(tmp_path), "title": "Worker Chat", "project_id": project["project_id"], "idempotency_key": "worker-chat"}})
    ipc_worker.handle({"command": "chat.pin", "payload": {"workspace_dir": str(tmp_path), "chat_id": chat["chat_id"]}})
    pinned = ipc_worker.handle({"command": "chat.pinned.list", "payload": {"workspace_dir": str(tmp_path)}})
    assert pinned["chats"][0]["chat_id"] == chat["chat_id"]
    snapshot = ipc_worker.handle({"command": "local.workspace.snapshot", "payload": {"workspace_dir": str(tmp_path)}})
    assert snapshot["projects"][0]["project_id"] == project["project_id"]
    assert snapshot["pinned_chats"][0]["chat_id"] == chat["chat_id"]
