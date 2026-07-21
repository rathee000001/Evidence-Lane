from __future__ import annotations

import sqlite3
import stat
import hashlib
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain


def test_chat_lineage_uses_trigger_enforced_prepare_commit_hash_chain_and_index_once(tmp_path: Path) -> None:
    source = tmp_path / "chat.md"
    source.write_text("User: preserve exact prompt\nAssistant: visible response\n", encoding="utf-8")
    payload = {
        "source_id": "source_chat",
        "lane_key": "chat_lineage",
        "path": str(source),
        "assistant_response": "Imported response",
        "active": True,
    }

    first = build_brain(str(tmp_path), "Chat Brain", [payload], generate_mmd=False)
    _, database = resolve_env15_sector(first["brain_root"], "chat_lineage")
    with sqlite3.connect(database) as initial_connection:
        counts_before_replay = {
            table: initial_connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("turn_prepare", "prompt_raw_exact", "response_raw_visible_exact", "file_link_registry", "turn_fts")
        }
    build_brain(str(tmp_path), "Chat Brain", [payload], generate_mmd=False)

    database.chmod(database.stat().st_mode | stat.S_IWUSR)
    connection = sqlite3.connect(database)
    try:
        # The Env15 template begins with two committed bootstrap turns.
        assert connection.execute("SELECT COUNT(*) FROM turn_prepare").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM turn_commit").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM state_hash_chain").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM lineage_head").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM file_link_registry").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM turn_fts").fetchone()[0] >= 4
        turn_id = connection.execute("SELECT turn_id FROM turn_prepare ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        exact_prompt = source.read_bytes().decode("utf-8")
        prompt_row = connection.execute(
            "SELECT content,byte_length,content_sha256,storage_status FROM prompt_raw_exact WHERE turn_id=?", (turn_id,)
        ).fetchone()
        response_row = connection.execute(
            "SELECT content,byte_length,content_sha256,storage_status FROM response_raw_visible_exact WHERE turn_id=?", (turn_id,)
        ).fetchone()
        link_row = connection.execute(
            "SELECT stable_file_id,external_display_uri,size_bytes,sha256,hash_status FROM file_link_registry WHERE turn_id=?", (turn_id,)
        ).fetchone()
        counts_after_replay = {
            table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("turn_prepare", "prompt_raw_exact", "response_raw_visible_exact", "file_link_registry", "turn_fts")
        }
        assert prompt_row == (
            exact_prompt, len(exact_prompt.encode("utf-8")), hashlib.sha256(exact_prompt.encode("utf-8")).hexdigest(), "FULL_EXACT"
        )
        assert response_row == (
            "Imported response", len(b"Imported response"), hashlib.sha256(b"Imported response").hexdigest(), "FULL_VISIBLE_EXACT"
        )
        assert link_row == (
            "source_chat", str(source.resolve()), source.stat().st_size, hashlib.sha256(source.read_bytes()).hexdigest(), "VERIFIED"
        )
        assert counts_after_replay == counts_before_replay
        with pytest.raises(sqlite3.IntegrityError, match="APPEND_ONLY_UPDATE_BLOCKED"):
            connection.execute("UPDATE prompt_raw_exact SET content='tampered' WHERE turn_id=?", (turn_id,))
    finally:
        connection.close()
    router = sqlite3.connect(Path(first["brain_root"]) / "project" / "project_router.sqlite")
    try:
        assert router.execute("SELECT COUNT(*) FROM sector_mutation_grant").fetchone()[0] == 0
    finally:
        router.close()
