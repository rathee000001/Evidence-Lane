from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.brain_versions import (
    BrainVersionError,
    BrainVersionTamperError,
    capture_brain_version,
    list_brain_versions,
    rollback_brain_version,
)
from sqlite_brain_builder.runtime.path_policy import brain_output_dir


def _canonical_sha256(payload: dict[str, object], stamp_field: str) -> str:
    body = dict(payload)
    body.pop(stamp_field, None)
    data = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _create_authoritative_brain(
    workspace: Path,
    brain_name: str,
    source_path: Path,
    *,
    value: str = "version one",
) -> Path:
    brain_root = brain_output_dir(workspace, brain_name)
    project = brain_root / "project"
    sector_dir = project / "sectors" / "docs"
    pointer_dir = project / "pointers"
    artifact_dir = project / "artifacts"
    topology_dir = project / "topology"
    package_dir = brain_root / "packages"
    for path in (sector_dir, pointer_dir, artifact_dir, topology_dir, package_dir):
        path.mkdir(parents=True, exist_ok=True)

    router = project / "project_router.sqlite"
    with sqlite3.connect(router) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS source_registry("
            "source_id TEXT PRIMARY KEY, display_name TEXT, path TEXT, source_hash TEXT)"
        )
        con.execute(
            "INSERT OR REPLACE INTO source_registry(source_id,display_name,path,source_hash) VALUES(?,?,?,?)",
            ("source_docs", source_path.name, str(source_path), ""),
        )

    sector = sector_dir / "docs_sector_v001.sqlite"
    with sqlite3.connect(sector) as con:
        con.execute("CREATE TABLE IF NOT EXISTS authoritative_item(id TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO authoritative_item(id,value) VALUES('current',?)", (value,))

    (project / "project_pointer.json").write_text(
        json.dumps({"router": "project/project_router.sqlite"}), encoding="utf-8"
    )
    (project / "sector_index.json").write_text(
        json.dumps([{"lane_key": "docs", "path": "project/sectors/docs/docs_sector_v001.sqlite"}]),
        encoding="utf-8",
    )
    (pointer_dir / "docs.json").write_text(
        json.dumps({"sector": "project/sectors/docs/docs_sector_v001.sqlite"}), encoding="utf-8"
    )
    (artifact_dir / "authoritative_note.json").write_text(
        json.dumps({"value": value}), encoding="utf-8"
    )

    # Regenerable outputs must not enter the immutable snapshot.
    (topology_dir / "project_master_topology.mmd").write_text("flowchart TD\n", encoding="utf-8")
    (package_dir / "regenerable.zip").write_bytes(b"not authoritative")
    return brain_root


def _read_authoritative_value(brain_root: Path) -> str:
    sector = brain_root / "project" / "sectors" / "docs" / "docs_sector_v001.sqlite"
    with sqlite3.connect(sector) as con:
        row = con.execute("SELECT value FROM authoritative_item WHERE id='current'").fetchone()
    assert row is not None
    return str(row[0])


def test_default_authoritative_workspace_is_app_roaming_and_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roaming = tmp_path / "AppData" / "Roaming" / "EvidenceOS"
    monkeypatch.delenv("EVIDENCE_OS_WORKSPACE", raising=False)
    monkeypatch.setenv("EVIDENCE_OS_ROAMING_DIR", str(roaming))

    workspace = ipc_worker._workspace({})

    assert workspace == (roaming / "SQLiteBrain").resolve()
    assert workspace.is_dir()


@pytest.mark.parametrize(
    ("actor_type", "actor_name", "reason", "expected"),
    [
        ("", "Evidence OS", "before mutation", "VERSION_ACTOR_TYPE_REQUIRED_APP_OR_AI"),
        ("public", "Evidence OS", "before mutation", "VERSION_ACTOR_TYPE_REQUIRED_APP_OR_AI"),
        ("ai", "", "before mutation", "VERSION_ACTOR_NAME_REQUIRED"),
        ("ai", "Public Writer", "", "VERSION_REASON_REQUIRED"),
    ],
)
def test_version_capture_rejects_missing_actor_contract(
    tmp_path: Path,
    actor_type: str,
    actor_name: str,
    reason: str,
    expected: str,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    _create_authoritative_brain(tmp_path, "Contract Brain", source)

    with pytest.raises(BrainVersionError, match=expected):
        capture_brain_version(
            tmp_path,
            "Contract Brain",
            actor_type=actor_type,
            actor_name=actor_name,
            reason=reason,
        )


def test_two_immutable_versions_list_and_rollback_to_separate_output(tmp_path: Path) -> None:
    source = tmp_path / "external selected source.md"
    source.write_text("source selected outside brain output", encoding="utf-8")
    brain_name = "Versioned Brain"
    brain_root = _create_authoritative_brain(tmp_path, brain_name, source)

    version_one = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS SQLite Builder",
        reason="initial successful build",
        change_summary="captured initial authoritative SQLite brain",
    )
    assert source.exists()

    sector = brain_root / "project" / "sectors" / "docs" / "docs_sector_v001.sqlite"
    with sqlite3.connect(sector) as con:
        con.execute("UPDATE authoritative_item SET value='version two' WHERE id='current'")
    (brain_root / "project" / "artifacts" / "authoritative_note.json").write_text(
        json.dumps({"value": "version two"}), encoding="utf-8"
    )
    version_two = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="ai",
        actor_name="Public Model Writer",
        reason="after approved mutation",
        change_summary="updated authoritative evidence row",
    )

    listed = list_brain_versions(tmp_path, brain_name, verify_hashes=True)
    assert listed["version_count"] == 2
    assert [item["version_id"] for item in listed["versions"]] == [
        version_two["version_id"],
        version_one["version_id"],
    ]
    assert listed["versions"][0]["actor_type"] == "ai"
    assert listed["versions"][0]["previous_version_id"] == version_one["version_id"]
    assert listed["versions"][0]["integrity_status"] == "VERIFIED"

    manifest_path = Path(listed["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_sha256"] == _canonical_sha256(manifest, "manifest_sha256")
    record_path = brain_root / "brain_versions" / manifest["versions"][0]["record_path"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["record_sha256"] == _canonical_sha256(record, "record_sha256")
    assert all(len(item["sha256"]) == 64 for item in record["source_hashes"].values())
    assert record["source_hashes"]["source_docs"]["path"] == str(source)
    snapshot_paths = {item["path"] for item in record["snapshot_files"]}
    assert "project/project_router.sqlite" in snapshot_paths
    assert "project/sectors/docs/docs_sector_v001.sqlite" in snapshot_paths
    assert not any("/topology/" in f"/{path}/" for path in snapshot_paths)
    assert not any(path.startswith("packages/") for path in snapshot_paths)
    assert not any("brain_versions" in path for path in snapshot_paths)

    rollback = rollback_brain_version(
        tmp_path,
        brain_name,
        version_one["version_id"],
        actor_name="Evidence OS SQLite Builder",
        reason="verify historical restore",
    )
    rollback_root = Path(rollback["rollback_output"])
    assert rollback_root.parent == tmp_path
    assert rollback_root.name == f"versioned_brain_rollback_{version_one['version_id']}_output"
    assert rollback_root != brain_root
    assert _read_authoritative_value(rollback_root) == "version one"
    assert _read_authoritative_value(brain_root) == "version two"
    assert not (rollback_root / "brain_versions").exists()
    assert Path(rollback["receipt_path"]).exists()
    assert list_brain_versions(tmp_path, brain_name)["version_count"] == 2

    with pytest.raises(BrainVersionError, match="ROLLBACK_OUTPUT_ALREADY_EXISTS"):
        rollback_brain_version(tmp_path, brain_name, version_one["version_id"])


def test_version_store_detects_manifest_and_object_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    brain_name = "Tamper Brain"
    brain_root = _create_authoritative_brain(tmp_path, brain_name, source)
    captured = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="app",
        actor_name="Evidence OS SQLite Builder",
        reason="tamper fixture",
    )

    manifest_path = Path(captured["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["brain_name"] = "altered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BrainVersionTamperError, match="VERSION_MANIFEST_SHA256_MISMATCH"):
        list_brain_versions(tmp_path, brain_name)

    manifest["brain_name"] = brain_name
    manifest["manifest_sha256"] = _canonical_sha256(manifest, "manifest_sha256")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    record_path = brain_root / "brain_versions" / manifest["versions"][0]["record_path"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    object_path = brain_root / "brain_versions" / record["snapshot_files"][0]["object"]
    object_path.chmod(object_path.stat().st_mode | 0o200)
    object_path.write_bytes(object_path.read_bytes() + b"tampered")

    with pytest.raises(BrainVersionTamperError, match="VERSION_OBJECT_SIZE_MISMATCH"):
        list_brain_versions(tmp_path, brain_name, verify_hashes=True)
    with pytest.raises(BrainVersionTamperError):
        rollback_brain_version(tmp_path, brain_name, captured["version_id"])


def test_worker_commands_and_successful_build_all_capture_app_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "build-source.md"
    source.write_text("build source", encoding="utf-8")
    brain_name = "Worker Brain"
    call_order: list[str] = []

    def fake_build(workspace_dir: str, selected_brain: str, *_: object) -> dict[str, str]:
        call_order.append("build")
        _create_authoritative_brain(Path(workspace_dir), selected_brain, source)
        return {"status": "built"}

    def fake_render(*_: object) -> dict[str, str]:
        call_order.append("render")
        return {"status": "rendered"}

    def fake_chatgpt(*_: object) -> dict[str, str]:
        call_order.append("chatgpt")
        return {"status": "exported"}

    def fake_gemini(*_: object, **_kwargs: object) -> dict[str, str]:
        call_order.append("gemini")
        return {"status": "exported"}

    def fake_rollback_product(*_: object, **__: object) -> dict[str, str]:
        return {"status": "PASS", "restored_output": str(workspace / "restored_output")}

    monkeypatch.setattr(ipc_worker, "build_brain", fake_build)
    monkeypatch.setattr(ipc_worker, "render_topology", fake_render)
    monkeypatch.setattr(ipc_worker, "export_one_upload_package", fake_chatgpt)
    monkeypatch.setattr(ipc_worker, "export_gemini_exact10", fake_gemini)
    monkeypatch.setattr(ipc_worker, "rollback_brain_as_complete_product", fake_rollback_product)

    result = ipc_worker.handle(
        {
            "id": "build-one",
            "command": "brain.buildAll",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "sources": [
                    {
                        "source_id": "source_docs",
                        "lane_key": "docs",
                        "path": str(source),
                        "display_name": source.name,
                    }
                ],
            },
        }
    )

    assert call_order == ["build", "render", "chatgpt", "gemini"]
    assert result["version"]["actor_type"] == "app"
    assert result["version"]["reason"] == "brain.buildAll successful"

    listed = ipc_worker.handle(
        {
            "command": "brain.versions.list",
            "payload": {"workspace_dir": str(workspace), "brain_name": brain_name},
        }
    )
    assert listed["version_count"] == 1
    assert listed["versions"][0]["integrity_status"] == "VERIFIED"

    rollback = ipc_worker.handle(
        {
            "command": "brain.version.rollback",
            "payload": {
                "workspace_dir": str(workspace),
                "brain_name": brain_name,
                "version_id": result["version"]["version_id"],
                "actor_name": "Evidence OS SQLite Builder",
                "reason": "worker rollback test",
            },
        }
    )
    assert rollback["status"] == "PASS"
    assert Path(rollback["restored_output"]).parent == workspace


def test_failed_build_all_does_not_capture_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    brain_name = "Failed Build Brain"

    def fake_build(workspace_dir: str, selected_brain: str, *_: object) -> dict[str, str]:
        _create_authoritative_brain(Path(workspace_dir), selected_brain, source)
        return {"status": "built"}

    monkeypatch.setattr(ipc_worker, "build_brain", fake_build)
    monkeypatch.setattr(ipc_worker, "render_topology", lambda *_: {"status": "rendered"})
    monkeypatch.setattr(
        ipc_worker,
        "export_one_upload_package",
        lambda *_: (_ for _ in ()).throw(RuntimeError("export failed")),
    )

    with pytest.raises(RuntimeError, match="export failed"):
        ipc_worker.handle(
            {
                "id": "failed-build",
                "command": "brain.buildAll",
                "payload": {
                    "workspace_dir": str(workspace),
                    "brain_name": brain_name,
                    "sources": [{"source_id": "source_docs", "lane_key": "docs", "path": str(source)}],
                },
            }
        )

    assert not (brain_output_dir(workspace, brain_name) / "brain_versions").exists()


def test_worker_ai_capture_requires_actor_and_reason(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    brain_name = "AI Writer Brain"
    _create_authoritative_brain(tmp_path, brain_name, source)

    with pytest.raises(BrainVersionError, match="VERSION_REASON_REQUIRED"):
        ipc_worker.handle(
            {
                "command": "brain.version.capture",
                "payload": {
                    "workspace_dir": str(tmp_path),
                    "brain_name": brain_name,
                    "actor_type": "ai",
                    "actor_name": "Public Model Writer",
                },
            }
        )

    captured = ipc_worker.handle(
        {
            "command": "brain.version.capture",
            "payload": {
                "workspace_dir": str(tmp_path),
                "brain_name": brain_name,
                "actor_type": "ai",
                "actor_name": "Public Model Writer",
                "reason": "before public mutation",
                "change_summary": "pre-mutation immutable checkpoint",
            },
        }
    )
    assert captured["actor_type"] == "ai"
    assert captured["actor_name"] == "Public Model Writer"
    assert captured["reason"] == "before public mutation"
