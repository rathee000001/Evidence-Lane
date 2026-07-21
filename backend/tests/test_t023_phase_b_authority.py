from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.canonical_lanes import (
    MutuallyExclusiveCodeLaneError,
)
from sqlite_brain_builder.runtime.env15_chat_lineage import (
    append_env15_chat_lineage_source,
)
from sqlite_brain_builder.runtime.env15_research import append_env15_research_source
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from sqlite_brain_builder.runtime.universal_lane_authority import (
    validate_universal_lane_authority,
    write_universal_lane_authority,
)
from sqlite_brain_builder.workspace.workspace_db import create_brain, init_workspace


def _workspace(tmp_path: Path, brain_name: str) -> Path:
    workspace = tmp_path / "workspace"
    init_workspace(workspace)
    create_brain(workspace, brain_name)
    return workspace


def _sector_database(brain_root: Path, lane_id: str) -> Path:
    return (
        brain_root
        / "project"
        / "sectors"
        / lane_id
        / f"{lane_id}_sector_v001.sqlite"
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_env_uop_are_only_locked_authorities_and_all_lanes_have_evidenced_state(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "Env UOP Authority")
    built = build_brain(str(workspace), "Env UOP Authority", [], generate_mmd=False)
    brain_root = Path(built["brain_root"])

    assert not (brain_root / "public_read").exists()
    assert not any(brain_root.rglob("project_template.sqlite"))
    assert not any(brain_root.rglob("project_template.mmd"))
    assert not any(brain_root.rglob("project_template.svg"))
    assert not any(brain_root.rglob("project_template.png"))

    router = sqlite3.connect(brain_root / "project" / "project_router.sqlite")
    try:
        router.execute("PRAGMA foreign_keys=ON")
        policies = router.execute(
            "SELECT sector_id,default_access,automatic_write,"
            "explicit_one_turn_grant_required,relock_after_commit "
            "FROM sector_registry ORDER BY sector_id"
        ).fetchall()
        foreign_keys = router.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        router.close()
    policy_by_lane = {row[0]: row[1:] for row in policies}
    assert policy_by_lane["chat_lineage"] == (
        "APPEND_ONLY_READ_WRITE",
        1,
        0,
        1,
    )
    assert policy_by_lane["research"] == (
        "APPEND_ONLY_READ_WRITE",
        1,
        0,
        1,
    )
    assert all(
        values == ("READ_ONLY", 0, 1, 1)
        for lane_id, values in policy_by_lane.items()
        if lane_id not in {"chat_lineage", "research"}
    )
    # A read-only inspection connection is not itself a mutable authority.
    assert foreign_keys in {0, 1}

    index = json.loads(
        (brain_root / "project" / "pointers" / "INDEX.json").read_text(
            encoding="utf-8"
        )
    )
    registry_path = (
        brain_root / "project" / "pointers" / "CANONICAL_LANE_REGISTRY.json"
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry_hash = _sha256(registry_path)
    assert index["canonical_lane_registry_sha256"] == registry_hash
    assert registry["classification_law"] == [
        "FAILED",
        "LOADED",
        "NOT_APPLICABLE",
        "SKIPPED",
    ]
    assert registry["locked_read_only_authorities"] == ["env", "uop"]
    assert registry["project_authority"] == "LIVE_GOVERNED_SECTOR_GRAPH"
    assert registry["automatic_append_lanes"] == ["chat_lineage", "research"]
    assert {lane["lane_classification"] for lane in registry["lanes"]} <= {
        "LOADED",
        "SKIPPED",
        "FAILED",
        "NOT_APPLICABLE",
    }
    assert all(
        lane["classification_evidence"]["database_sha256"]
        and lane["classification_evidence"]["database_size_bytes"] > 0
        for lane in registry["lanes"]
    )
    for pointer_name in (".uepc_env", ".uepc_profile", ".uepc_project"):
        assert (
            f"CANONICAL_LANE_REGISTRY_SHA256={registry_hash}"
            in (brain_root / pointer_name).read_text(encoding="utf-8")
        )
    assert validate_universal_lane_authority(brain_root)["status"] == "PASS"

    write_universal_lane_authority(
        brain_root,
        active_lane_ids=["docs"],
        ingestion_results=[{"lane_id": "docs", "status": "FAILED_TEST_PROOF"}],
        brain_name="Env UOP Authority",
    )
    failed_index = json.loads(
        (brain_root / "project" / "pointers" / "INDEX.json").read_text(
            encoding="utf-8"
        )
    )
    docs = next(item for item in failed_index["lanes"] if item["lane_id"] == "docs")
    assert docs["lane_classification"] == "FAILED"
    assert docs["classification_evidence"]["status"] == "FAILED_TEST_PROOF"
    assert validate_universal_lane_authority(brain_root)["status"] == "PASS"


def test_research_is_content_addressed_append_only_and_prepare_commit_guarded(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "Research Append")
    built = build_brain(str(workspace), "Research Append", [], generate_mmd=False)
    brain_root = Path(built["brain_root"])
    source = {
        "source_id": "research_directional_001",
        "lane_key": "research",
        "text": (
            "Research question: Does exact-byte reconstruction survive replay?\n"
            "Finding: The second identical append must preserve the database bytes.\n"
        ),
        "actor": "user",
        "provider": "USER_SUPPLIED",
    }

    first = append_env15_research_source(brain_root, source)
    database = _sector_database(brain_root, "research")
    first_bytes = database.read_bytes()
    repeated = append_env15_research_source(brain_root, source)
    assert first["status"] == "PASS"
    assert repeated["status"] == "SKIPPED_UNCHANGED"
    assert database.read_bytes() == first_bytes

    original_mode = database.stat().st_mode
    database.chmod(original_mode | stat.S_IWUSR)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM research_question").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_finding").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_append_prepare").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM research_append_commit").fetchone()[0] == 1

        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_UPDATE_BLOCKED"):
            connection.execute(
                "UPDATE research_question SET content='tampered'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_DELETE_BLOCKED"):
            connection.execute("DELETE FROM research_question")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="RESEARCH_PREPARE_REQUIRED"):
            connection.execute(
                "INSERT INTO research_append_commit VALUES(?,?,?,?)",
                ("orphan-turn", 999, "f" * 64, "2026-07-19T00:00:00Z"),
            )
        connection.rollback()
    finally:
        connection.close()
        database.chmod(original_mode)
    assert database.read_bytes() == first_bytes


def test_chat_lineage_preserves_exact_suffix_visible_response_and_attachment_fidelity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "Lineage Fidelity")
    built = build_brain(str(workspace), "Lineage Fidelity", [], generate_mmd=False)
    brain_root = Path(built["brain_root"])
    attachment = tmp_path / "directional-steer.txt"
    attachment.write_bytes(b"exact attachment bytes\r\n")
    attachment_hash = hashlib.sha256(attachment.read_bytes()).hexdigest()
    source_base = {
        "source_id": "full_chat_export_001",
        "lane_key": "chat_lineage",
        "actor": "user",
        "provider": "ChatGPT",
        "attachments": [
            {
                "path": str(attachment),
                "direction": "INPUT",
                "package_relative_path": "attachments/directional-steer.txt",
            }
        ],
    }
    first = append_env15_chat_lineage_source(
        brain_root,
        {
            **source_base,
            "text": "old exact lineage",
            "assistant_response": "old visible response",
            "visible_reasoning_summary": "old visible summary",
            "source_classification": "ENV14_CARRY_FORWARD",
            "task_window_id": "TASK-WINDOW-001",
            "source_event_range": "EVENT-0001..EVENT-0010",
        },
    )
    second = append_env15_chat_lineage_source(
        brain_root,
        {
            **source_base,
            "text": "old exact lineage\nnew exact suffix",
            "assistant_response": "new visible response",
            "visible_reasoning_summary": "new visible summary",
            "source_classification": "CURRENT_CHAT",
            "task_window_id": "TASK-WINDOW-002",
            "source_event_range": "EVENT-0011..EVENT-0012",
        },
    )
    database = _sector_database(brain_root, "chat_lineage")
    before_repeat = database.read_bytes()
    repeated = append_env15_chat_lineage_source(
        brain_root,
        {
            **source_base,
            "text": "old exact lineage\nnew exact suffix",
            "assistant_response": "new visible response",
            "visible_reasoning_summary": "new visible summary",
            "source_classification": "CURRENT_CHAT",
            "task_window_id": "TASK-WINDOW-002",
            "source_event_range": "EVENT-0011..EVENT-0012",
        },
    )
    assert repeated["status"] == "SKIPPED_UNCHANGED"
    assert database.read_bytes() == before_repeat

    connection = sqlite3.connect(database)
    try:
        revisions = connection.execute(
            "SELECT r.revision_sequence,p.content,v.content,r.append_classification,"
            "r.source_classification,r.task_window_id,r.source_event_range "
            "FROM lineage_source_revision r "
            "JOIN prompt_raw_exact p ON p.turn_id=r.turn_id "
            "JOIN response_raw_visible_exact v ON v.turn_id=r.turn_id "
            "WHERE r.source_id=? ORDER BY r.revision_sequence",
            (source_base["source_id"],),
        ).fetchall()
        links = connection.execute(
            "SELECT turn_id,stable_file_id,external_display_uri,package_relative_path,"
            "mime_type,size_bytes,sha256,direction,availability_state,hash_status "
            "FROM file_link_registry WHERE original_name=? ORDER BY recorded_at",
            (attachment.name,),
        ).fetchall()
        visible = connection.execute(
            "SELECT v.summary,v.hidden_chain_of_thought_stored FROM visible_reasoning_summary v "
            "WHERE v.turn_id IN (?,?) ORDER BY v.turn_id",
            (first["turn_id"], second["turn_id"]),
        ).fetchall()
    finally:
        connection.close()
    assert revisions == [
        (
            1,
            "old exact lineage",
            "old visible response",
            "INITIAL_FULL_SOURCE",
            "ENV14_CARRY_FORWARD",
            "TASK-WINDOW-001",
            "EVENT-0001..EVENT-0010",
        ),
        (
            2,
            "\nnew exact suffix",
            "new visible response",
            "APPENDED_UNSEEN_SUFFIX",
            "CURRENT_CHAT",
            "TASK-WINDOW-002",
            "EVENT-0011..EVENT-0012",
        ),
    ]
    assert len(links) == 2
    assert all(row[1] and row[2] == str(attachment.resolve()) for row in links)
    assert all(row[3] == "attachments/directional-steer.txt" for row in links)
    assert all(row[4] == "text/plain" for row in links)
    assert all(row[5] == len(attachment.read_bytes()) for row in links)
    assert all(row[6] == attachment_hash for row in links)
    assert all(row[7:] == ("INPUT", "AVAILABLE", "VERIFIED") for row in links)
    assert sorted(visible) == [("new visible summary", 0), ("old visible summary", 0)]


def test_primary_code_mode_is_exclusive_and_non_git_local_uses_synthetic_snapshot(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "plain-local-source"
    source_root.mkdir()
    (source_root / "main.py").write_text("print('snapshot only')\n", encoding="utf-8")

    conflict_workspace = _workspace(tmp_path / "conflict", "Code Conflict")
    with pytest.raises(
        MutuallyExclusiveCodeLaneError,
        match="PRIMARY_CODE_MODE_CONFLICT:github_code,local_code",
    ):
        build_brain(
            str(conflict_workspace),
            "Code Conflict",
            [
                {
                    "source_id": "same_as_local",
                    "lane_key": "local_code",
                    "path": str(source_root),
                    "active": True,
                },
                {
                    "source_id": "same_as_github",
                    "lane_key": "github_code",
                    "path": str(source_root),
                    "active": True,
                },
            ],
            generate_mmd=False,
        )

    local_workspace = _workspace(tmp_path / "local", "Synthetic Local")
    built = build_brain(
        str(local_workspace),
        "Synthetic Local",
        [
            {
                "source_id": "plain_local",
                "lane_key": "local_code",
                "path": str(source_root),
                "active": True,
            }
        ],
        generate_mmd=False,
    )
    brain_root = Path(built["brain_root"])
    database = _sector_database(brain_root, "local_code")
    connection = sqlite3.connect(database)
    try:
        commit_count = connection.execute(
            "SELECT COUNT(*) FROM git_commit_registry"
        ).fetchone()[0]
        synthetic_count = connection.execute(
            "SELECT COUNT(*) FROM code_synthetic_snapshot_file"
        ).fetchone()[0]
        source_state = connection.execute(
            "SELECT repository_url,branch_name,commit_head,has_git_history "
            "FROM code_source_registry WHERE source_id='plain_local'"
        ).fetchone()
        active_head = connection.execute(
            "SELECT head_commit_sha,proof_status FROM code_source_active_head "
            "WHERE source_id='plain_local'"
        ).fetchone()
    finally:
        connection.close()
    assert commit_count == 0
    assert synthetic_count == 1
    assert source_state == ("", "", None, 0)
    assert active_head == (None, "REF_SNAPSHOT_ONLY")
    pointer = json.loads(
        (brain_root / "project" / "pointers" / "local_code_pointer.json").read_text(
            encoding="utf-8"
        )
    )
    assert pointer["lane_classification"] == "LOADED"
    assert pointer["database_sha256"] == _sha256(database)
