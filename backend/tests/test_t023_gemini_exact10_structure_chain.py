from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker, refresh_brain
from sqlite_brain_builder.codex_env15_package import (
    create_codex_env15_package,
    validate_codex_env15_zip,
)
from sqlite_brain_builder.runtime.env15_resource import EXPECTED_SECTORS, REQUIRED_MMD_ASSETS
from sqlite_brain_builder.runtime.env15_locked_read import (
    LOCKED_READ_PACKAGE_MEMBER,
    find_locked_read_archive,
)
from sqlite_brain_builder.runtime import gemini_exact10
from sqlite_brain_builder.runtime.gemini_exact10 import export_gemini_exact10_direct
from sqlite_brain_builder.runtime.package_validation import (
    T021_GEMINI_EXACT10_NAMES,
    validate_gemini_exact10,
)


EXACT10_STRUCTURE = (
    "GEMINI_FLASH_PROMPT.txt",
    ".uepc_env",
    ".uepc_profile",
    ".uepc_project",
    ".uepc_project_write",
    "ENV_PUBLIC_READONLY.sqlite",
    "UOP_PUBLIC_READONLY.sqlite",
    "PROJECT_CONJOINED_WRITE.sqlite",
    "ENV_MMD_RENDER.png",
    "UOP_MMD_RENDER.png",
)


def _database_bytes(path: Path, statements: list[tuple[str, tuple[object, ...]]]) -> bytes:
    with sqlite3.connect(path) as connection:
        for sql, values in statements:
            connection.execute(sql, values)
    return path.read_bytes()


def _chatgpt_package(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    sector_rows = [
        (sector, f"project/sectors/{sector}/{sector}_sector_v001.sqlite")
        for sector in sorted(EXPECTED_SECTORS)
    ]
    router_path = tmp_path / "project_router.sqlite"
    with sqlite3.connect(router_path) as connection:
        connection.execute(
            "CREATE TABLE sector_registry(sector_id TEXT PRIMARY KEY,sqlite_path TEXT NOT NULL)"
        )
        connection.executemany("INSERT INTO sector_registry VALUES(?,?)", sector_rows)
    template = _database_bytes(
        tmp_path / "project_template.sqlite",
        [
            ("CREATE TABLE project_meta(id TEXT PRIMARY KEY,value TEXT NOT NULL)", ()),
            ("INSERT INTO project_meta VALUES(?,?)", ("project", "current")),
        ],
    )
    env = _database_bytes(
        tmp_path / "env.sqlite",
        [
            ("CREATE TABLE env_law(id TEXT PRIMARY KEY,value TEXT NOT NULL)", ()),
            ("INSERT INTO env_law VALUES(?,?)", ("env", "current")),
        ],
    )
    uop = _database_bytes(
        tmp_path / "uop.sqlite",
        [
            ("CREATE TABLE uop_law(id TEXT PRIMARY KEY,value TEXT NOT NULL)", ()),
            ("INSERT INTO uop_law VALUES(?,?)", ("uop", "current")),
        ],
    )
    members: dict[str, bytes] = {
        ".uepc_env": b"UEPC_ENV_VERSION=V15\n",
        ".uepc_profile": b"PROFILE_ID=UOP_PUBLIC_GOVERNANCE_V15\n",
        ".uepc_project": b"PROJECT_ID=CURRENT_PROJECT\n",
        "env/env_sqlite.sqlite": env,
        "uop/uop_sqlite.sqlite": uop,
        "env/env_mmd.png": b"current-env-png",
        "uop/uop_mmd.png": b"current-uop-png",
        "project/project_router.sqlite": router_path.read_bytes(),
        "project/project_template.sqlite": template,
        "receipts/build.md": b"status=PASS\n",
        "recovery/recovery_prompt.txt": b"# Current recovery\n",
        "provider_readable/source/local_code/fixture/.gitkeep": b"",
    }
    for sector in sorted(EXPECTED_SECTORS):
        database = _database_bytes(
            tmp_path / f"{sector}.sqlite",
            [
                ("CREATE TABLE evidence(id TEXT PRIMARY KEY,value TEXT NOT NULL)", ()),
                ("INSERT INTO evidence VALUES(?,?)", (f"{sector}-1", f"{sector}-current")),
            ],
        )
        members[f"project/sectors/{sector}/{sector}_sector_v001.sqlite"] = database
    delta_database = _database_bytes(
        tmp_path / "delta_sector_v001.sqlite",
        [
            (
                "CREATE TABLE canonical_delta_pointer(singleton INTEGER PRIMARY KEY,latest_sequence INTEGER NOT NULL,status TEXT NOT NULL)",
                (),
            ),
            (
                "CREATE TABLE delta_record(sequence INTEGER PRIMARY KEY,delta_id TEXT NOT NULL,value TEXT NOT NULL)",
                (),
            ),
            (
                "INSERT INTO canonical_delta_pointer VALUES(?,?,?)",
                (1, 1, "ACTIVE"),
            ),
            (
                "INSERT INTO delta_record VALUES(?,?,?)",
                (1, "delta-1", "preserve-this-real-project-delta"),
            ),
        ],
    )
    members["project/sectors/delta/delta_sector_v001.sqlite"] = delta_database
    for topology in REQUIRED_MMD_ASSETS:
        members.setdefault(topology, b"current-topology-proof")
    members.update(
        {
            "project/topology/project_master_topology.mmd": b"flowchart TD\n A --> B\n",
            "project/topology/project_master_topology.svg": b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
            "project/topology/project_master_topology.png": b"current-project-png",
        }
    )
    members[LOCKED_READ_PACKAGE_MEMBER] = find_locked_read_archive().read_bytes()
    manifest = [
        {
            "path": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for name, data in sorted(members.items())
    ]
    members["manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json"] = json.dumps(manifest).encode()
    package = tmp_path / "ChatGPT_Current_Sqlite_brain.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
    return package, members


def test_gemini_exact10_names_match_structure_authority_not_prior_content_contract() -> None:
    assert T021_GEMINI_EXACT10_NAMES == EXACT10_STRUCTURE


def test_conjoined_project_chunks_large_source_and_reconstructs_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini_exact10, "PROJECT_EMBED_INLINE_LIMIT_BYTES", 4 * 1024)
    monkeypatch.setattr(gemini_exact10, "PROJECT_EMBED_CHUNK_BYTES", 2 * 1024)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "project_template.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE payload(id TEXT PRIMARY KEY,content BLOB NOT NULL)")
        connection.execute("INSERT INTO payload VALUES(?,?)", ("large", b"gold-delta" * 4096))
    expected = source.read_bytes()
    destination = tmp_path / "PROJECT_CONJOINED_WRITE.sqlite"

    result = gemini_exact10.build_conjoined_project_sqlite(project, destination)

    assert result["embedded_database_chunked_count"] == 1
    assert result["embedded_database_inline_count"] == 0
    assert result["embedded_database_chunk_count"] > 1
    with sqlite3.connect(destination) as connection:
        metadata = connection.execute(
            "SELECT source_sha256,source_byte_size,content_storage,content_chunk_count,content "
            "FROM embedded_database_blob"
        ).fetchone()
        chunks = connection.execute(
            "SELECT chunk_index,chunk_offset,chunk_byte_size,chunk_sha256,content "
            "FROM embedded_database_blob_chunk ORDER BY chunk_index"
        ).fetchall()
    reconstructed = b"".join(row[4] for row in chunks)
    assert metadata[2:] == ("CHUNKED_BLOB_V1", len(chunks), None)
    assert metadata[0] == hashlib.sha256(expected).hexdigest()
    assert metadata[1] == len(expected)
    assert reconstructed == expected
    assert all(row[0] == index for index, row in enumerate(chunks))
    assert all(row[1] == sum(previous[2] for previous in chunks[:index]) for index, row in enumerate(chunks))
    assert all(row[2] == len(row[4]) for row in chunks)
    assert all(row[3] == hashlib.sha256(row[4]).hexdigest() for row in chunks)


def test_retired_third_locked_chatgpt_package_is_rejected_before_gemini_derivation(
    tmp_path: Path,
) -> None:
    chatgpt, _members = _chatgpt_package(tmp_path)
    brain_root = tmp_path / "brain"
    (brain_root / "packages").mkdir(parents=True)

    with pytest.raises(
        gemini_exact10.GeminiExact10Error,
        match="CHATGPT_SOURCE_PACKAGE_VALIDATION_FAILED:.*RETIRED_THIRD_LOCKED_CONTAINER_MEMBER",
    ):
        export_gemini_exact10_direct(
            brain_root,
            "Current Brain",
            "# Current governed Gemini prompt",
            chatgpt_package=chatgpt,
        )


def test_codex_chain_does_not_start_from_retired_third_locked_chatgpt_package(
    tmp_path: Path,
) -> None:
    chatgpt, _chatgpt_members = _chatgpt_package(tmp_path)
    brain_root = tmp_path / "brain"
    (brain_root / "packages").mkdir(parents=True)
    with pytest.raises(
        gemini_exact10.GeminiExact10Error,
        match="CHATGPT_SOURCE_PACKAGE_VALIDATION_FAILED:.*RETIRED_THIRD_LOCKED_CONTAINER_MEMBER",
    ):
        export_gemini_exact10_direct(
            brain_root,
            "Current Brain",
            "# Current governed Gemini prompt",
            chatgpt_package=chatgpt,
        )


def test_build_button_chains_chatgpt_to_gemini_then_binds_both_into_higher_codex_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# governed source", encoding="utf-8")
    brain_root = tmp_path / "brain-output"
    brain_root.mkdir()
    chatgpt_path = tmp_path / "ChatGPT_chain.zip"
    gemini_path = tmp_path / "Gemini_chain.zip"
    chatgpt_path.write_bytes(b"chatgpt")
    gemini_path.write_bytes(b"gemini")
    calls: list[tuple[str, object]] = []
    codex_delta: dict[str, object] = {}
    codex_sources: dict[str, object] = {}

    monkeypatch.setattr(
        ipc_worker,
        "build_brain",
        lambda *_args, **_kwargs: {
            "brain_root": str(brain_root),
            "incremental": {"all_sources_unchanged": False},
        },
    )
    monkeypatch.setattr(ipc_worker, "generate_project_mmd", lambda *_args, **_kwargs: {"mmd_files": []})
    monkeypatch.setattr(ipc_worker, "render_topology", lambda *_args, **_kwargs: {"rendered": []})
    monkeypatch.setattr(
        ipc_worker,
        "export_one_upload_package",
        lambda *_args, **_kwargs: calls.append(("chatgpt", None))
        or {
            "package_zip": str(chatgpt_path),
            "sha256": hashlib.sha256(chatgpt_path.read_bytes()).hexdigest(),
            "validation": {
                "status": "PASS",
                "errors": [],
                "package": str(chatgpt_path),
                "sha256": hashlib.sha256(chatgpt_path.read_bytes()).hexdigest(),
            },
        },
    )

    def gemini_export(
        _workspace: str,
        _brain_name: str,
        _progress: object,
        *,
        chatgpt_package: str,
    ) -> dict[str, object]:
        calls.append(("gemini", chatgpt_package))
        return {
            "gemini_package_zip": str(gemini_path),
            "sha256": hashlib.sha256(gemini_path.read_bytes()).hexdigest(),
            "source_chatgpt_package_sha256": hashlib.sha256(chatgpt_path.read_bytes()).hexdigest(),
            "validation": {
                "status": "PASS",
                "errors": [],
                "package": str(gemini_path),
                "sha256": hashlib.sha256(gemini_path.read_bytes()).hexdigest(),
                "source_chatgpt_package_sha256": hashlib.sha256(chatgpt_path.read_bytes()).hexdigest(),
            },
        }

    monkeypatch.setattr(ipc_worker, "export_gemini_exact10", gemini_export)
    monkeypatch.setattr(
        ipc_worker,
        "validate_chatgpt_package",
        lambda _path: pytest.fail("hash-bound ChatGPT exporter validation must be reused"),
    )
    monkeypatch.setattr(
        ipc_worker,
        "validate_gemini_exact10",
        lambda _path: pytest.fail("hash-bound Gemini exporter validation must be reused"),
    )

    def codex_export(*_args: object, **kwargs: object) -> dict[str, object]:
        calls.append(("codex", None))
        codex_delta.update(dict(kwargs["delta_ledger"]))
        codex_sources.update({
            "chatgpt_package": kwargs.get("chatgpt_package"),
            "gemini_package": kwargs.get("gemini_package"),
        })
        return {
            "status": "PASS",
            "archive": str(tmp_path / "Codex_chain.zip"),
            "validation": {"status": "PASS", "errors": []},
            "archive_validation": {"status": "PASS", "errors": []},
        }

    monkeypatch.setattr(ipc_worker, "create_codex_env15_package", codex_export)
    monkeypatch.setattr(
        ipc_worker,
        "capture_brain_version",
        lambda *_args, **_kwargs: {"status": "PASS", "reason": "brain.buildAll successful"},
    )

    result = ipc_worker.handle(
        {
            "id": "provider-chain-build",
            "command": "brain.buildAll",
            "payload": {
                "workspace_dir": str(tmp_path / "workspace"),
                "brain_name": "Provider Chain Brain",
                "sources": [{"source_id": "docs", "lane_key": "docs", "path": str(source)}],
            },
        }
    )

    assert calls == [
        ("chatgpt", None),
        ("gemini", str(chatgpt_path)),
        ("codex", None),
    ]
    upstream = codex_delta["upstream_package_chain"]
    assert isinstance(upstream, dict)
    assert upstream["chatgpt_local_ai"]["path"] == str(chatgpt_path)
    assert upstream["gemini"]["path"] == str(gemini_path)
    assert codex_delta["brain_diff_transport"] == "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE"
    assert codex_sources == {
        "chatgpt_package": str(chatgpt_path),
        "gemini_package": str(gemini_path),
    }
    assert result["package_chain"]["order"] == ["CHATGPT_LOCAL_AI_SHARED", "GEMINI_FROM_CHATGPT_LOCAL_AI", "CODEX_HIGHER_WITH_BRAIN_DIFFS"]


def test_build_button_rejects_gemini_whose_embedded_chatgpt_hash_does_not_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# governed source", encoding="utf-8")
    brain_root = tmp_path / "brain-output"
    brain_root.mkdir()
    chatgpt_path = tmp_path / "ChatGPT_chain.zip"
    gemini_path = tmp_path / "Gemini_chain.zip"
    chatgpt_path.write_bytes(b"chatgpt")
    gemini_path.write_bytes(b"gemini")
    chatgpt_sha256 = hashlib.sha256(chatgpt_path.read_bytes()).hexdigest()
    wrong_sha256 = "f" * 64

    monkeypatch.setattr(
        ipc_worker,
        "build_brain",
        lambda *_args, **_kwargs: {
            "brain_root": str(brain_root),
            "incremental": {"all_sources_unchanged": False},
        },
    )
    monkeypatch.setattr(ipc_worker, "generate_project_mmd", lambda *_args, **_kwargs: {"mmd_files": []})
    monkeypatch.setattr(ipc_worker, "render_topology", lambda *_args, **_kwargs: {"rendered": []})
    monkeypatch.setattr(
        ipc_worker,
        "export_one_upload_package",
        lambda *_args, **_kwargs: {
            "package_zip": str(chatgpt_path),
            "sha256": chatgpt_sha256,
            "validation": {"status": "PASS", "errors": []},
        },
    )
    monkeypatch.setattr(
        ipc_worker,
        "export_gemini_exact10",
        lambda *_args, **_kwargs: {
            "gemini_package_zip": str(gemini_path),
            "source_chatgpt_package_sha256": wrong_sha256,
            "validation": {"status": "PASS", "errors": []},
        },
    )
    monkeypatch.setattr(
        ipc_worker,
        "validate_chatgpt_package",
        lambda _path: {"status": "PASS", "errors": [], "sha256": chatgpt_sha256},
    )
    monkeypatch.setattr(
        ipc_worker,
        "validate_gemini_exact10",
        lambda _path: {
            "status": "PASS",
            "errors": [],
            "source_chatgpt_package_sha256": wrong_sha256,
        },
    )

    with pytest.raises(ipc_worker.WorkerError, match="GEMINI_CHATGPT_DERIVATION_HASH_MISMATCH"):
        ipc_worker.handle(
            {
                "id": "provider-chain-mismatch",
                "command": "brain.buildAll",
                "payload": {
                    "workspace_dir": str(tmp_path / "workspace"),
                    "brain_name": "Provider Chain Mismatch Brain",
                    "sources": [{"source_id": "docs", "lane_key": "docs", "path": str(source)}],
                },
            }
        )


def test_refresh_candidate_chains_gemini_from_its_just_validated_chatgpt_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_workspace = tmp_path / "candidate-workspace"
    candidate_root = tmp_path / "candidate-brain"
    candidate_root.mkdir()
    chatgpt_path = tmp_path / "Refresh_ChatGPT.zip"
    gemini_path = tmp_path / "Refresh_Gemini.zip"
    chatgpt_path.write_bytes(b"refresh-chatgpt")
    gemini_path.write_bytes(b"refresh-gemini")
    chatgpt_sha256 = hashlib.sha256(chatgpt_path.read_bytes()).hexdigest()
    captured: dict[str, str] = {}
    refresh_codex_delta: dict[str, object] = {}
    refresh_codex_sources: dict[str, object] = {}

    monkeypatch.setattr(
        refresh_brain,
        "build_brain",
        lambda *_args, **_kwargs: {
            "brain_root": str(candidate_root),
            "incremental": {"code_lanes_loaded": True},
        },
    )
    monkeypatch.setattr(refresh_brain, "render_topology", lambda *_args, **_kwargs: {"rendered": []})
    monkeypatch.setattr(refresh_brain, "_candidate_topology_render_passes", lambda *_args: True)
    monkeypatch.setattr(
        refresh_brain,
        "export_one_upload_package",
        lambda *_args, **_kwargs: {"package_zip": str(chatgpt_path), "sha256": chatgpt_sha256},
    )

    def refresh_gemini_export(
        _workspace: str,
        _brain_name: str,
        _progress: object,
        *,
        chatgpt_package: str,
    ) -> dict[str, object]:
        captured["chatgpt_package"] = chatgpt_package
        return {
            "gemini_package_zip": str(gemini_path),
            "source_chatgpt_package_sha256": chatgpt_sha256,
        }

    monkeypatch.setattr(refresh_brain, "export_gemini_exact10", refresh_gemini_export)
    monkeypatch.setattr(
        refresh_brain,
        "validate_chatgpt_package",
        lambda _path: {"status": "PASS", "errors": [], "sha256": chatgpt_sha256},
    )
    monkeypatch.setattr(
        refresh_brain,
        "validate_gemini_exact10",
        lambda _path: {
            "status": "PASS",
            "errors": [],
            "source_chatgpt_package_sha256": chatgpt_sha256,
        },
    )
    def refresh_codex_export(*_args: object, **kwargs: object) -> dict[str, object]:
        refresh_codex_delta.update(dict(kwargs["delta_ledger"]))
        refresh_codex_sources.update({
            "chatgpt_package": kwargs.get("chatgpt_package"),
            "gemini_package": kwargs.get("gemini_package"),
        })
        return {
            "status": "PASS",
            "validation": {"status": "PASS", "errors": []},
            "archive_validation": {"status": "PASS", "errors": []},
        }

    monkeypatch.setattr(refresh_brain, "create_codex_env15_package", refresh_codex_export)

    result = refresh_brain._create_candidate_products(
        candidate_workspace,
        "Refresh Chain Brain",
        [],
        baseline={},
        candidate_id="refresh-chain-candidate",
    )

    assert captured["chatgpt_package"] == str(chatgpt_path)
    assert result["validations"]["status"] == "PASS"
    assert refresh_codex_delta["brain_diff_transport"] == "CODEX_EXTERNAL_WORKING_COPY_PATCH_EVIDENCE"
    assert refresh_codex_sources == {
        "chatgpt_package": str(chatgpt_path),
        "gemini_package": str(gemini_path),
    }
    refresh_upstream = refresh_codex_delta["upstream_package_chain"]
    assert isinstance(refresh_upstream, dict)
    assert refresh_upstream["chatgpt_local_ai"]["sha256"] == chatgpt_sha256
    assert refresh_upstream["gemini"]["source_chatgpt_package_sha256"] == chatgpt_sha256
