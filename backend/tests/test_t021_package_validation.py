from __future__ import annotations

import json
import hashlib
import sqlite3
import zipfile
from pathlib import Path

from sqlite_brain_builder.runtime.package_validation import (
    T021_GEMINI_EXACT10_NAMES,
    validate_chatgpt_package,
    validate_gemini_exact10,
)
from sqlite_brain_builder.runtime.env15_resource import EXPECTED_SECTORS, REQUIRED_MMD_ASSETS
from sqlite_brain_builder.runtime.env15_locked_read import (
    LOCKED_READ_PACKAGE_MEMBER,
    find_locked_read_archive,
)
from sqlite_brain_builder.runtime.gemini_exact10 import build_conjoined_project_sqlite


def _sqlite_bytes(tmp_path: Path, name: str) -> bytes:
    database = tmp_path / name
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES('one', 'verified')")
    return database.read_bytes()


def _valid_gemini(
    tmp_path: Path,
    name: str = "gemini.zip",
    *,
    runtime_sectors: tuple[str, ...] = (),
) -> Path:
    package = tmp_path / name
    sqlite_data = _sqlite_bytes(tmp_path, name.replace(".zip", ".sqlite"))
    project = tmp_path / (name.replace(".zip", "") + "_project")
    project.mkdir()
    sectors = sorted(set(EXPECTED_SECTORS) | set(runtime_sectors))
    with sqlite3.connect(project / "project_router.sqlite") as connection:
        connection.execute(
            "CREATE TABLE sector_registry(sector_id TEXT PRIMARY KEY,sqlite_path TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO sector_registry VALUES(?,?)",
            [
                (
                    sector,
                    f"project/sectors/{sector}/{sector}_sector_v001.sqlite",
                )
                for sector in sectors
            ],
        )
    with sqlite3.connect(project / "project_template.sqlite") as connection:
        connection.execute("CREATE TABLE project_meta(id TEXT PRIMARY KEY,value TEXT)")
        connection.execute("INSERT INTO project_meta VALUES('project','fixture')")
    for sector in sectors:
        sector_dir = project / "sectors" / sector
        sector_dir.mkdir(parents=True)
        with sqlite3.connect(sector_dir / f"{sector}_sector_v001.sqlite") as connection:
            connection.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY,value TEXT)")
            connection.execute("INSERT INTO evidence VALUES(?,?)", (f"{sector}-1", "fixture"))
    project_conjoined = tmp_path / (name.replace(".zip", "") + "_conjoined.sqlite")
    build_conjoined_project_sqlite(project, project_conjoined)
    content = {
        "GEMINI_FLASH_PROMPT.txt": b"T023-GEMINI-EXACT10-CURRENT-ENV15\n",
        ".uepc_env": (
            b"ENV_SQLITE=ENV_PUBLIC_READONLY.sqlite\n"
            b"ENV_MMD_PNG=ENV_MMD_RENDER.png\n"
            b"ENV_WRITE_LOCK=true\n"
            b"PROFILE_POINTER=.uepc_profile\n"
            b"PROJECT_POINTER=.uepc_project\n"
            b"PROJECT_WRITE_POINTER=.uepc_project_write\n"
            b"FLASH_PROMPT=GEMINI_FLASH_PROMPT.txt\n"
        ),
        ".uepc_profile": (
            b"UOP_SQLITE=UOP_PUBLIC_READONLY.sqlite\n"
            b"UOP_MMD_PNG=UOP_MMD_RENDER.png\n"
            b"UOP_WRITE_LOCK=true\n"
        ),
        ".uepc_project": (
            b"PROJECT_SQLITE=PROJECT_CONJOINED_WRITE.sqlite\n"
            b"PROJECT_CORE_NAMESPACE=project_core__\n"
            b"PROJECT_CORE_ACCESS=READ_ONLY_ROUTER_METADATA\n"
            b"PROJECT_WRITE_POINTER=.uepc_project_write\n"
        ),
        ".uepc_project_write": (
            b"PROJECT_WRITE_SQLITE=PROJECT_CONJOINED_WRITE.sqlite\n"
            b"WRITE_ROUTER_TABLE=gemini_project_write_router\n"
            b"WRITE_GRANT_TABLE=gemini_mutation_grant\n"
            b"WRITE_LOG_TABLE=gemini_project_write_log\n"
            b"PROJECT_HEAD_TABLE=gemini_project_head\n"
            b"CHAT_LINEAGE_NAMESPACE=sector_chat_lineage__\n"
            b"CHAT_LINEAGE_ACCESS=APPEND_ONLY_READ_WRITE\n"
            b"RESEARCH_NAMESPACE=sector_research__\n"
            b"RESEARCH_ACCESS=APPEND_ONLY_READ_WRITE\n"
            b"AUTOMATIC_APPEND_LANES=chat_lineage,research\n"
            b"SOURCE_DB_BLOB_TABLE=embedded_database_blob\n"
            b"SOURCE_DB_BLOB_CHUNK_TABLE=embedded_database_blob_chunk\n"
            b"SOURCE_SCHEMA_REGISTRY=source_schema_object_registry\n"
            b"SOURCE_ROW_COUNT_REGISTRY=source_row_count_registry\n"
        ),
        "ENV_PUBLIC_READONLY.sqlite": sqlite_data,
        "UOP_PUBLIC_READONLY.sqlite": sqlite_data,
        "PROJECT_CONJOINED_WRITE.sqlite": project_conjoined.read_bytes(),
        "ENV_MMD_RENDER.png": b"not-an-empty-env-png-fixture",
        "UOP_MMD_RENDER.png": b"not-an-empty-uop-png-fixture",
    }
    assert set(content) == set(T021_GEMINI_EXACT10_NAMES)
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in T021_GEMINI_EXACT10_NAMES:
            archive.writestr(member, content[member])
    return package


def test_gemini_exact10_accepts_ten_direct_readable_entries(tmp_path: Path) -> None:
    result = validate_gemini_exact10(_valid_gemini(tmp_path))
    assert result["status"] == "PASS"
    assert result["root_entry_count"] == 10
    assert result["nested_zip_count"] == 0
    assert result["all_files_directly_readable"] is True


def test_gemini_exact10_accepts_governed_additive_delta_sector(tmp_path: Path) -> None:
    result = validate_gemini_exact10(
        _valid_gemini(tmp_path, "with-delta.zip", runtime_sectors=("delta",))
    )

    assert result["status"] == "PASS"
    assert result["project_contract"]["sector_router_count"] == 15
    assert result["project_contract"]["source_database_count"] == 17
    assert result["project_contract"]["sector_ids"][-1] == "sqlite_brain"
    assert "delta" in result["project_contract"]["sector_ids"]


def test_gemini_exact10_rejects_unregistered_runtime_sector(tmp_path: Path) -> None:
    result = validate_gemini_exact10(
        _valid_gemini(tmp_path, "with-unknown.zip", runtime_sectors=("unknown_lane",))
    )

    assert result["status"] == "FAIL"
    assert "PROJECT_CONJOINED_RUNTIME_SECTORS_UNEXPECTED:unknown_lane" in result["errors"]


def test_gemini_exact10_rejects_nested_zip(tmp_path: Path) -> None:
    valid = _valid_gemini(tmp_path, "valid.zip")
    package = tmp_path / "nested.zip"
    with zipfile.ZipFile(valid, "r") as source, zipfile.ZipFile(package, "w") as target:
        for member in source.namelist():
            if member == "GEMINI_FLASH_PROMPT.txt":
                target.writestr(member, b"PK\x03\x04nested")
            else:
                target.writestr(member, source.read(member))
    result = validate_gemini_exact10(package)
    assert result["status"] == "FAIL"
    assert result["nested_zip_count"] == 1


def test_chatgpt_package_rejects_retired_third_locked_container_and_legacy_manifest(tmp_path: Path) -> None:
    package = tmp_path / "chatgpt.zip"
    sqlite_data = _sqlite_bytes(tmp_path, "project.sqlite")
    members = {
        ".uepc_env": b"ENV=15\n",
        ".uepc_profile": b"UOP=15\n",
        ".uepc_project": b"PROJECT=1\n",
        "env/env_law.md": b"# Env\n",
        "uop/uop_law.md": b"# UOP\n",
        "project/project_router.sqlite": sqlite_data,
        "receipts/build.md": b"status=PASS\n",
        "recovery/recovery_prompt.txt": b"# Recovery\n",
    }
    for sector in EXPECTED_SECTORS:
        members[f"project/sectors/{sector}/{sector}_sector_v001.sqlite"] = sqlite_data
    for topology in REQUIRED_MMD_ASSETS:
        members[topology] = b"topology-proof"
    members.update({
        "project/topology/project_master_topology.mmd": b"flowchart TD\n A --> B\n",
        "project/topology/project_master_topology.svg": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        "project/topology/project_master_topology.png": b"png-proof",
    })
    members[LOCKED_READ_PACKAGE_MEMBER] = find_locked_read_archive().read_bytes()
    manifest = [
        {"path": name, "size": len(data), "sha256": __import__("hashlib").sha256(data).hexdigest()}
        for name, data in members.items()
    ]
    members["manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    result = validate_chatgpt_package(package)
    assert result["status"] == "FAIL"
    assert result["third_locked_container_status"] == "FAIL"
    assert result["archive_reopened"] is True
    assert any(
        error.startswith("RETIRED_THIRD_LOCKED_CONTAINER_MEMBER:")
        for error in result["errors"]
    )


def test_chatgpt_package_rejects_incomplete_contract_and_stale_manifest_path(tmp_path: Path) -> None:
    package = tmp_path / "incomplete.zip"
    members = {
        ".uepc_env": b"ENV=15\n",
        ".uepc_profile": b"UOP=15\n",
        ".uepc_project": b"PROJECT=1\n",
        "env/law.md": b"env",
        "uop/law.md": b"uop",
        "project/router.txt": b"project",
        "receipts/build.md": b"receipt",
        "recovery/recovery_prompt.txt": b"recover",
    }
    members["manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json"] = json.dumps([
        {"path": "C:/stale/staging/output.sqlite", "sha256": "0" * 64, "size": 1}
    ]).encode()
    with zipfile.ZipFile(package, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    result = validate_chatgpt_package(package)
    assert result["status"] == "FAIL"
    assert any(error.startswith("MISSING_PROJECT_SECTOR:") for error in result["errors"])
    assert any(error.startswith("MISSING_TOPOLOGY_FILE:") for error in result["errors"])
    assert "STALE_OR_UNSAFE_PACKAGE_PATH:C:/stale/staging/output.sqlite" in result["errors"]
