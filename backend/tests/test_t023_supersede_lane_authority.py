from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder.codex_env15_package import (
    CLEAN_CODEX_ROOT_AUTHORITY_PATH,
    create_codex_env15_package,
)
from sqlite_brain_builder.runtime.env15_chat_lineage import (
    Env15ChatLineageError,
    append_env15_chat_lineage_source,
)
from sqlite_brain_builder.runtime.env15_locked_read import (
    CHATGPT_GEMINI_ARCHIVE_SHA256,
    CODEX_ARCHIVE_SHA256,
)
from sqlite_brain_builder.runtime.env15_topology import generate_env15_topologies
from sqlite_brain_builder.runtime.package_root_authority import (
    validate_zip_package_root_authority,
)
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
)
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repository(root: Path) -> tuple[Path, str]:
    repo = root / "repository"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Evidence Lane Test")
    _git(repo, "config", "user.email", "evidence-lane@example.test")
    _git(repo, "remote", "add", "origin", "https://example.test/evidence-lane.git")
    (repo / "main.py").write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-m", "first")
    (repo / "main.py").write_text("print('two')\n", encoding="utf-8")
    (repo / "images").mkdir()
    (repo / "images" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nBINARY")
    _git(repo, "add", "main.py", "images/logo.png")
    _git(repo, "commit", "-m", "second")
    return repo, _git(repo, "rev-parse", "HEAD")


def _workspace(tmp_path: Path, brain_name: str) -> Path:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, brain_name)
    return workspace


def _code_database(brain_root: Path, lane_id: str) -> Path:
    return (
        brain_root
        / "project"
        / "sectors"
        / lane_id
        / f"{lane_id}_sector_v001.sqlite"
    )


def test_local_code_records_current_checkout_only_and_reuses_exact_index(
    tmp_path: Path,
) -> None:
    repo, head = _make_repository(tmp_path)
    workspace = _workspace(tmp_path, "Local Provenance")
    source = {
        "source_id": "local_git_checkout",
        "lane_key": "local_code",
        "path": str(repo),
        "active": True,
    }
    first = build_brain(
        str(workspace), "Local Provenance", [source], generate_mmd=False
    )
    brain_root = Path(first["brain_root"])
    database = _code_database(brain_root, "local_code")
    connection = sqlite3.connect(database)
    try:
        commits = connection.execute(
            "SELECT commit_sha FROM git_commit_registry ORDER BY reverse_sequence"
        ).fetchall()
        active_head = connection.execute(
            "SELECT head_commit_sha FROM code_source_active_head WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()
        repository_url = connection.execute(
            "SELECT repository_url FROM code_source_registry WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()
        image = connection.execute(
            "SELECT file_id,content_kind FROM code_file_snapshot WHERE relative_path='images/logo.png'"
        ).fetchone()
        image_chunks = connection.execute(
            "SELECT COUNT(*) FROM code_chunk WHERE file_id=?", (image[0],)
        ).fetchone()[0]
        image_exact_bytes = connection.execute(
            "SELECT COUNT(*) FROM code_exact_byte_chunk WHERE file_id=?", (image[0],)
        ).fetchone()[0]
        counts_before = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "source_file",
                "code_file_snapshot",
                "code_chunk",
                "chunk_index",
                "code_chunk_fts",
                "code_exact_byte_chunk",
                "git_commit_registry",
            )
        }
    finally:
        connection.close()
    assert commits == [(head,)]
    assert active_head == (head,)
    assert repository_url == ("https://example.test/evidence-lane.git",)
    assert image[1] == "BINARY_METADATA_ONLY"
    assert image_chunks == 0
    assert image_exact_bytes == 0
    database_bytes = database.read_bytes()

    second = build_brain(
        str(workspace), "Local Provenance", [source], generate_mmd=False
    )
    assert second["incremental"]["all_sources_unchanged"] is True
    assert database.read_bytes() == database_bytes
    connection = sqlite3.connect(database)
    try:
        counts_after = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in counts_before
        }
    finally:
        connection.close()
    assert counts_after == counts_before


def test_github_code_remains_separate_full_history_lane(tmp_path: Path) -> None:
    repo, head = _make_repository(tmp_path)
    workspace = _workspace(tmp_path, "Git History")
    source = {
        "source_id": "github_history",
        "lane_key": "github_code",
        "path": str(repo),
        "active": True,
        "metadata": {"repo_url": "https://example.test/evidence-lane.git"},
    }
    built = build_brain(str(workspace), "Git History", [source], generate_mmd=False)
    database = _code_database(Path(built["brain_root"]), "github_code")
    connection = sqlite3.connect(database)
    try:
        commit_count = connection.execute(
            "SELECT COUNT(*) FROM git_commit_registry"
        ).fetchone()[0]
        head_row = connection.execute(
            "SELECT head_commit_sha FROM code_source_active_head WHERE source_id=?",
            (source["source_id"],),
        ).fetchone()
        history_rows = connection.execute(
            "SELECT COUNT(*) FROM git_file_change"
        ).fetchone()[0]
    finally:
        connection.close()
    assert commit_count == 2
    assert head_row == (head,)
    assert history_rows > 0


def test_chat_lineage_reimport_appends_only_unseen_suffix(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "Lineage Suffix")
    built = build_brain(str(workspace), "Lineage Suffix", [], generate_mmd=False)
    brain_root = Path(built["brain_root"])
    source_base = {
        "source_id": "stable_full_chat_export",
        "lane_key": "chat_lineage",
        "actor": "user",
        "provider": "ChatGPT",
    }
    first = append_env15_chat_lineage_source(
        brain_root, {**source_base, "text": "alpha"}
    )
    second = append_env15_chat_lineage_source(
        brain_root, {**source_base, "text": "alpha\nbeta"}
    )
    repeated = append_env15_chat_lineage_source(
        brain_root, {**source_base, "text": "alpha\nbeta"}
    )
    database = _code_database(brain_root, "chat_lineage")
    connection = sqlite3.connect(database)
    try:
        prompts = connection.execute(
            "SELECT p.content,r.append_classification FROM prompt_raw_exact p "
            "JOIN turn_prepare t ON t.turn_id=p.turn_id "
            "JOIN lineage_source_revision r ON r.turn_id=p.turn_id ORDER BY t.sequence"
        ).fetchall()
        cursor = connection.execute(
            "SELECT full_prompt_text,revision_sequence,last_turn_id FROM lineage_source_cursor "
            "WHERE source_id=?",
            (source_base["source_id"],),
        ).fetchone()
        revisions = connection.execute(
            "SELECT prefix_character_count,appended_character_count "
            "FROM lineage_source_revision WHERE source_id=? ORDER BY revision_sequence",
            (source_base["source_id"],),
        ).fetchall()
    finally:
        connection.close()
    assert first["append_classification"] == "INITIAL_FULL_SOURCE"
    assert second["append_classification"] == "APPENDED_UNSEEN_SUFFIX"
    assert repeated["status"] == "SKIPPED_UNCHANGED"
    assert prompts == [
        ("alpha", "INITIAL_FULL_SOURCE"),
        ("\nbeta", "APPENDED_UNSEEN_SUFFIX"),
    ]
    assert cursor == ("alpha\nbeta", 2, second["turn_id"])
    assert revisions == [(0, 5), (5, 5)]
    before_divergence = database.read_bytes()
    with pytest.raises(
        Env15ChatLineageError,
        match="CHAT_LINEAGE_SOURCE_DIVERGED_REQUIRES_REVIEW",
    ):
        append_env15_chat_lineage_source(
            brain_root, {**source_base, "text": "different history"}
        )
    assert database.read_bytes() == before_divergence


def test_provider_packages_have_env_uop_only_and_two_append_lanes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('provider')\n", encoding="utf-8")
    workspace = _workspace(tmp_path, "Provider Supersede")
    built = build_brain(
        str(workspace),
        "Provider Supersede",
        [
            {
                "source_id": "provider_local",
                "lane_key": "local_code",
                "path": str(source),
                "active": True,
            }
        ],
        generate_mmd=False,
    )
    brain_root = Path(built["brain_root"])
    generated = generate_env15_topologies(brain_root)
    master = Path(generated["PROJECT"].mmd_path)
    master.with_suffix(".svg").write_text("<svg></svg>\n", encoding="utf-8")
    master.with_suffix(".png").write_bytes(b"\x89PNG\r\n\x1a\nPROOF")

    chatgpt = export_one_upload_package(str(workspace), "Provider Supersede")
    assert chatgpt["validation"]["status"] == "PASS"
    archive_path = Path(chatgpt["package_zip"])
    root_validation = validate_zip_package_root_authority(archive_path)
    assert root_validation["status"] == "PASS"
    assert root_validation["acceptance_state"] == "UNTRUSTED_CANDIDATE"
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert not any(name.startswith("public_read/") for name in names)
        assert not any("project_template" in name.casefold() for name in names)
        assert not any(name.casefold().endswith(".zip") for name in names)
        project_pointer = archive.read(".uepc_project").decode("utf-8")
        assert "PROJECT_STATE=LIVE_GOVERNED_SECTOR_GRAPH" in project_pointer
        assert "AUTOMATIC_APPEND_LANES=chat_lineage,research" in project_pointer
        assert "PROJECT_TEMPLATE" not in project_pointer
        lineage = json.loads(archive.read("project/lineage/LINEAGE_HEAD.json"))
        lineage_payload = {key: value for key, value in lineage.items() if key != "head_sha256"}
        assert lineage["head_sha256"] == hashlib.sha256(
            json.dumps(
                lineage_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        assert lineage["acceptance_state"] == "UNTRUSTED_CANDIDATE"
        assert f"LINEAGE_HEAD_SHA256={lineage['head_sha256']}" in project_pointer
        assert f"NEXT_EXPECTED_INDEX={lineage['next_expected_index']}" in project_pointer
        assert f"LINEAGE_HEAD_SHA256={lineage['head_sha256']}" in archive.read(
            "receipts/last_entry_slip.txt"
        ).decode("utf-8")
        assert f"LINEAGE_HEAD_SHA256={lineage['head_sha256']}" in archive.read(
            "receipts/last_exit_slip.txt"
        ).decode("utf-8")
        root_authority = json.loads(archive.read("manifests/PACKAGE_ROOT_AUTHORITY.json"))
        assert root_authority["current_root_authority"] is True
        assert root_authority["acceptance_state"] == "UNTRUSTED_CANDIDATE"
        project_manifest = json.loads(
            archive.read("manifests/PROJECT_BRAIN_PACKAGE_MANIFEST.json")
        )
        assert project_manifest["manifest_scope"] == "PROJECT_SNAPSHOT_NOT_CURRENT_OUTER_ROOT"
        assert project_manifest["current_root_authority"] == "manifests/PACKAGE_ROOT_AUTHORITY.json"
        pointer_index = json.loads(archive.read("project/pointers/INDEX.json"))
        pointers = {item["lane_id"]: item for item in pointer_index["lanes"]}
        assert pointers["chat_lineage"]["automatic_append"] is True
        assert pointers["research"]["automatic_append"] is True
        assert pointers["github_code"]["build_state"] == "SKIPPED"
        assert pointers["github_code"]["classification_evidence"]["status"] == "SKIPPED_NO_SOURCE"
        assert pointers["local_code"]["database_sha256"] == hashlib.sha256(
            archive.read("project/sectors/local_code/local_code_sector_v001.sqlite")
        ).hexdigest()

    gemini = export_gemini_exact10(
        str(workspace),
        "Provider Supersede",
        chatgpt_package=archive_path,
    )
    assert gemini["validation"]["status"] == "PASS"
    with zipfile.ZipFile(gemini["gemini_package_zip"]) as archive:
        names = set(archive.namelist())
        assert ".uepc_project_write" not in names
        assert "PROJECT_CONJOINED_WRITE.sqlite" not in names
        open_me_first = archive.read("OPEN_ME_FIRST.txt").decode("utf-8")
        assert "Exactly two Project lanes accept automatic append-only model writeback:" in open_me_first
        lines = open_me_first.splitlines()
        append_header = lines.index("Exactly two Project lanes accept automatic append-only model writeback:")
        assert lines[append_header + 1:append_header + 3] == ["- chat_lineage", "- research"]

    codex = create_codex_env15_package(
        brain_root,
        brain_root / "packages" / "codex_candidate",
        brain_root / "packages",
        brain_name="Provider Supersede",
        goal_pointer={
            "parent_goal_id": "T023",
            "current_task_pointer": "T023-D007-D008-ROOT-AUTHORITY",
        },
        delta_ledger={"delta_id": "T023-D007-D008-001"},
        chatgpt_package=archive_path,
        gemini_package=None,
    )
    assert codex["status"] == "PASS"
    assert codex["archive_validation"]["status"] == "PASS"
    codex_root_validation = validate_zip_package_root_authority(
        codex["archive"],
        manifest_relative=CLEAN_CODEX_ROOT_AUTHORITY_PATH,
    )
    assert codex_root_validation["status"] == "PASS"
    assert codex_root_validation["acceptance_state"] == "UNTRUSTED_CANDIDATE"
    with zipfile.ZipFile(codex["archive"]) as archive:
        names = set(archive.namelist())
        assert "manifests/PACKAGE_ROOT_AUTHORITY.json" in names
        assert CLEAN_CODEX_ROOT_AUTHORITY_PATH in names
        contract = json.loads(archive.read("manifests/CODEX_HIGHER_PACKAGE_CONTRACT.json"))
        assert contract["current_root_authority"] == CLEAN_CODEX_ROOT_AUTHORITY_PATH
        assert contract["inherited_root_authority_scope"] == (
            "CHATGPT_BASE_SNAPSHOT_NOT_CURRENT_OUTER_ROOT"
        )
        assert contract["new_package_acceptance_state"] == "UNTRUSTED_CANDIDATE"
        assert contract["supplied_source_archive_sha256"] == CODEX_ARCHIVE_SHA256
        assert (
            contract["chatgpt_gemini_common_archive_sha256"]
            == CHATGPT_GEMINI_ARCHIVE_SHA256
        )
