from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.codex_env15_package import (
    PACKAGE_FAMILY,
    create_codex_env15_package,
    validate_codex_env15_package,
    validate_codex_env15_zip,
)
from sqlite_brain_builder.runtime.portable_brain_package import (
    create_portable_brain_package,
    export_portable_brain_zip,
    validate_portable_brain_package,
    validate_portable_brain_zip,
)


def _source_brain(root: Path) -> Path:
    sector = root / "project" / "sectors" / "docs"
    sector.mkdir(parents=True)
    database = sector / "docs_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES('evidence-1','package authority acceptance')")
    return root


def _governance(pointer: str = "BACKEND_PACKAGE_AUTHORITY") -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "parent_goal_id": "T023_SUPREME_SOURCE_RECONCILED_LINEAR_EVIDENCEOS",
            "active_run_id": "T023-RUN-20260713T022552Z",
            "current_task_pointer": pointer,
            "active_lane_id": "build_command",
        },
        {
            "delta_id": "UEPC-EVIDENCEOS-T023-ADDITIVE-GOAL-DELTA-LANE-HIL-MODEL-REFRESH-OLLAMA-001",
            "status": "OPEN_REGISTERED",
            "prompt_pointer": "brain.buildAll",
            "insertion_reason": "SAME_VALIDATED_PASS_CODEX_PACKAGE",
            "insertion_point": "package_hash_validation",
        },
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_codex_archive_identity_binds_governance_and_identical_replay_is_byte_stable(tmp_path: Path) -> None:
    brain = _source_brain(tmp_path / "brain")
    package = tmp_path / "portable" / "brain"
    archives = tmp_path / "archives"
    goal, delta = _governance()
    latest_good = {"status": "VERIFIED", "snapshot_hash": "A" * 64}

    first = create_codex_env15_package(
        brain,
        package,
        archives,
        brain_name="Authority Brain",
        goal_pointer=goal,
        delta_ledger=delta,
        latest_good_snapshot=latest_good,
    )
    first_archive = Path(first["archive"])
    first_sha = _sha256(first_archive)
    authority_path = package / "manifests" / "PACKAGE_AUTHORITY.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))

    assert first["authority_sha256"] == authority["authority_sha256"]
    assert len(first["authority_sha256"]) == 64
    assert first["archive_reused"] is False

    identical = create_codex_env15_package(
        brain,
        package,
        archives,
        brain_name="Authority Brain",
        goal_pointer=goal,
        delta_ledger=delta,
        latest_good_snapshot=latest_good,
    )
    assert identical["archive_reused"] is True
    assert identical["archive"] == first["archive"]
    assert identical["authority_sha256"] == first["authority_sha256"]
    assert _sha256(Path(identical["archive"])) == first_sha

    changed_goal = {**goal, "current_task_pointer": "BACKEND_PACKAGE_AUTHORITY_CHANGED"}
    changed = create_codex_env15_package(
        brain,
        package,
        archives,
        brain_name="Authority Brain",
        goal_pointer=changed_goal,
        delta_ledger=delta,
        latest_good_snapshot=latest_good,
    )
    assert changed["archive_reused"] is False
    assert changed["archive"] == first["archive"]
    assert changed["authority_sha256"] != first["authority_sha256"]
    assert _sha256(first_archive) != first_sha
    assert list(archives.glob(f"{PACKAGE_FAMILY}_*.zip")) == [first_archive]
    assert validate_codex_env15_zip(changed["archive"])["status"] == "PASS"


def test_codex_working_package_rejects_authority_tamper_even_for_mutable_codex_pointer(tmp_path: Path) -> None:
    brain = _source_brain(tmp_path / "brain")
    package = tmp_path / "portable" / "brain"
    goal, delta = _governance()
    create_codex_env15_package(
        brain,
        package,
        tmp_path / "archives",
        brain_name="Tamper Brain",
        goal_pointer=goal,
        delta_ledger=delta,
    )

    tampered = {**goal, "current_task_pointer": "UNAUTHORIZED_POINTER"}
    (package / "codex" / "CURRENT_GOAL_POINTER.json").write_text(
        json.dumps(tampered, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    validation = validate_codex_env15_package(package)

    assert validation["status"] == "FAIL"
    assert "UNIVERSAL_PACKAGE_AUTHORITY_INPUT_MISMATCH" in validation["errors"]


def test_portable_package_requires_exact_safe_manifest_coverage_in_working_and_zip_forms(tmp_path: Path) -> None:
    brain = _source_brain(tmp_path / "brain")
    package = tmp_path / "portable"
    goal, delta = _governance()
    create_portable_brain_package(brain, package, goal_pointer=goal, delta_ledger=delta)

    unexpected = package / "unmanifested.txt"
    unexpected.write_text("must not pass exact coverage", encoding="utf-8")
    working = validate_portable_brain_package(package)
    assert working["status"] == "FAIL"
    assert "MANIFEST_COVERAGE_MISMATCH" in working["errors"]
    unexpected.unlink()

    archive = tmp_path / "portable.zip"
    export_portable_brain_zip(package, archive)
    with zipfile.ZipFile(archive, "a", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("unmanifested.txt", "not declared")
        bundle.writestr("../escape.txt", "unsafe")
    frozen = validate_portable_brain_zip(archive)

    assert frozen["status"] == "FAIL"
    assert "ZIP_MANIFEST_COVERAGE_MISMATCH" in frozen["errors"]
    assert "ZIP_MEMBER_PATH_INVALID:../escape.txt" in frozen["errors"]


def test_no_change_build_reopens_all_packages_and_hash_verifies_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    brain_root = tmp_path / "brain-output"
    for relative in (
        "env/env_mmd.mmd",
        "env/env_mmd.svg",
        "env/env_mmd.png",
        "uop/uop_mmd.mmd",
        "uop/uop_mmd.svg",
        "uop/uop_mmd.png",
        "project/topology/project_master_topology.mmd",
        "project/topology/project_master_topology.svg",
        "project/topology/project_master_topology.png",
        "project/topology/project_master_topology_HD.png",
    ):
        path = brain_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified")
    package_dir = brain_root / "packages"
    package_dir.mkdir()
    chatgpt = package_dir / "ChatGPT_verified.zip"
    gemini = package_dir / "Gemini_verified.zip"
    codex = package_dir / f"{PACKAGE_FAMILY}_verified.zip"
    with zipfile.ZipFile(chatgpt, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for relative in (
            "project/topology/project_master_topology.mmd",
            "project/topology/project_master_topology.svg",
            "project/topology/project_master_topology.png",
            "project/topology/project_master_topology_HD.png",
        ):
            package.write(brain_root / relative, relative)
    for path in (gemini, codex):
        path.write_bytes(b"package")

    calls: list[tuple[str, object]] = []
    chat_validation = {"status": "PASS", "errors": [], "validator": "chatgpt"}
    gemini_validation = {"status": "PASS", "errors": [], "validator": "gemini"}
    codex_validation = {"status": "PASS", "errors": [], "validator": "codex"}
    monkeypatch.setattr(ipc_worker, "validate_chatgpt_package", lambda path: calls.append(("chatgpt", path)) or chat_validation)
    monkeypatch.setattr(ipc_worker, "validate_gemini_exact10", lambda path: calls.append(("gemini", path)) or gemini_validation)
    monkeypatch.setattr(ipc_worker, "validate_codex_env15_zip", lambda path: calls.append(("codex", path)) or codex_validation)
    monkeypatch.setattr(
        ipc_worker,
        "list_brain_versions",
        lambda _workspace, _brain_name, *, verify_hashes: calls.append(("versions", verify_hashes))
        or {"versions": [{"version_id": "verified-version", "hash_validation": "PASS"}]},
    )

    reused = ipc_worker._reusable_full_build_products(workspace, "Verified Brain", brain_root)

    assert [name for name, _ in calls] == ["chatgpt", "gemini", "codex", "versions"]
    assert calls[-1] == ("versions", True)
    assert reused is not None
    assert reused["chatgpt"]["validation"] == chat_validation
    assert reused["gemini"]["validation"] == gemini_validation
    assert reused["codex"]["archive_validation"] == codex_validation


def test_no_change_build_refuses_stale_topology_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    brain_root = tmp_path / "brain-output"
    topology_relatives = (
        "project/topology/project_master_topology.mmd",
        "project/topology/project_master_topology.svg",
        "project/topology/project_master_topology.png",
        "project/topology/project_master_topology_HD.png",
    )
    for relative in (
        "env/env_mmd.mmd",
        "env/env_mmd.svg",
        "env/env_mmd.png",
        "uop/uop_mmd.mmd",
        "uop/uop_mmd.svg",
        "uop/uop_mmd.png",
        *topology_relatives,
    ):
        path = brain_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"current-live-topology" if relative in topology_relatives else b"verified")
    package_dir = brain_root / "packages"
    package_dir.mkdir()
    chatgpt = package_dir / "ChatGPT_stale.zip"
    with zipfile.ZipFile(chatgpt, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for relative in topology_relatives:
            package.writestr(relative, b"stale-topology")
    (package_dir / "Gemini_verified.zip").write_bytes(b"package")
    (package_dir / f"{PACKAGE_FAMILY}_verified.zip").write_bytes(b"package")

    monkeypatch.setattr(ipc_worker, "validate_chatgpt_package", lambda _path: {"status": "PASS", "errors": []})
    monkeypatch.setattr(ipc_worker, "validate_gemini_exact10", lambda _path: {"status": "PASS", "errors": []})
    monkeypatch.setattr(ipc_worker, "validate_codex_env15_zip", lambda _path: {"status": "PASS", "errors": []})

    assert ipc_worker._reusable_full_build_products(workspace, "Stale Topology Brain", brain_root) is None


def test_no_change_build_refuses_any_invalid_reopened_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brain_root = tmp_path / "brain-output"
    for relative in (
        "env/env_mmd.mmd",
        "env/env_mmd.svg",
        "env/env_mmd.png",
        "uop/uop_mmd.mmd",
        "uop/uop_mmd.svg",
        "uop/uop_mmd.png",
        "project/topology/project_master_topology.mmd",
        "project/topology/project_master_topology.svg",
        "project/topology/project_master_topology.png",
        "project/topology/project_master_topology_HD.png",
    ):
        path = brain_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"verified")
    package_dir = brain_root / "packages"
    package_dir.mkdir()
    for name in ("ChatGPT_invalid.zip", "Gemini_verified.zip", f"{PACKAGE_FAMILY}_verified.zip"):
        (package_dir / name).write_bytes(b"package")

    monkeypatch.setattr(ipc_worker, "validate_chatgpt_package", lambda _path: {"status": "FAIL", "errors": ["TAMPERED"]})
    monkeypatch.setattr(ipc_worker, "validate_gemini_exact10", lambda _path: {"status": "PASS", "errors": []})
    monkeypatch.setattr(ipc_worker, "validate_codex_env15_zip", lambda _path: {"status": "PASS", "errors": []})

    assert ipc_worker._reusable_full_build_products(tmp_path / "workspace", "Invalid Brain", brain_root) is None
