import json
import sqlite3
import shutil

import pytest

from sqlite_brain_builder.runtime import stable_runtime_v53 as rt
from sqlite_brain_builder.runtime.chatlineage_state_travel_v57 import ensure_chat_lineage_sector


def test_legacy_brain_manifest_migrates(tmp_path):
    router = tmp_path / "project_router.sqlite"
    con = sqlite3.connect(router)
    con.execute(
        "CREATE TABLE brain_manifest(brain_id TEXT PRIMARY KEY, brain_name TEXT, brain_slug TEXT, created_at TEXT, status TEXT)"
    )
    con.commit()
    con.close()

    rt.ensure_router(router, "Legacy Brain")

    con = sqlite3.connect(router)
    cols = [r[1] for r in con.execute("PRAGMA table_info(brain_manifest)")]
    row = con.execute("SELECT brain_name, builder_version FROM brain_manifest").fetchone()
    con.close()

    assert "builder_version" in cols
    assert row[0] == "Legacy Brain"
    assert "project_mutation_intake" in cols_by_table(router)


def cols_by_table(db_path):
    con = sqlite3.connect(db_path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    return names


def test_legacy_sector_manifest_migrates(tmp_path):
    db = tmp_path / "sector.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE sector_manifest(sector_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, version TEXT, status TEXT, schema_tables_json TEXT)"
    )
    con.commit()
    con.close()

    rt.ensure_sector(db, "docs")

    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(sector_manifest)")]
    row = con.execute("SELECT lane_key, fill_policy, created_at FROM sector_manifest").fetchone()
    con.close()

    assert "fill_policy" in cols
    assert "created_at" in cols
    assert row[0] == "docs"


def test_legacy_lineage_source_migrates(tmp_path):
    project = tmp_path / "project"
    db = project / "sectors" / "chat_lineage" / "chat_lineage_sector_v001.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE lineage_source(source_id TEXT PRIMARY KEY, name TEXT, path TEXT, metadata_json TEXT, created_at TEXT)"
    )
    con.commit()
    con.close()

    ensure_chat_lineage_sector(project)

    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("PRAGMA table_info(lineage_source)")]
    count = con.execute("SELECT COUNT(*) FROM lineage_source").fetchone()[0]
    con.close()

    assert "source_type" in cols
    assert "source_sha256" in cols
    assert count >= 1


def test_locked_prompt_and_final_names():
    assert rt.LOCKED_FLASH_PROMPT.startswith(
        "EVIDENCE LANE ENV/UOP + LIVE PROJECT BOOT V5.9\n\nCHAT_NAME:\nBRIEF_NATURE_OF_CHAT:"
    )
    assert rt.safe_export_name("GOLD V3") == "GOLD_V3"
    assert rt.safe_export_name("!!!") == "brain"


def test_project_master_mmd_uses_code_db_and_mutation_lanes(tmp_path):
    root = tmp_path / "brain"
    project = root / "project"
    code_dir = project / "sectors" / "local_code"
    code_dir.mkdir(parents=True)
    (project / "topology").mkdir(parents=True)
    router = project / "project_router.sqlite"
    code_db = code_dir / "local_code_sector_v001.sqlite"

    rt.ensure_router(router, "MMD Brain")
    con = sqlite3.connect(router)
    con.execute(
        "INSERT OR REPLACE INTO sector_registry(sector_id,lane_key,lane_label,sector_db_path,active_bool,version,sector_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("sector_local_code", "local_code", "Local Code", str(code_db), 1, "v001", "hash", "now"),
    )
    con.execute(
        "INSERT OR REPLACE INTO source_registry(source_id,lane_key,lane_label,source_type,display_name,path,source_hash,active_bool,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("src1", "local_code", "Local Code", "folder", "Gold Project", "C:/gold", "hash", 1, "now"),
    )
    con.commit()
    con.close()

    con = sqlite3.connect(code_db)
    rt.create_code_schema(con)
    con.execute("INSERT INTO source_file(file_id,logical_path,extension,size_bytes,sha256,lane_status,created_at) VALUES(?,?,?,?,?,?,?)", ("f1", "app/page.tsx", ".tsx", 100, "abc", "ACTIVE", "now"))
    con.execute("INSERT INTO code_file(file_id,canonical_path,language,extension,current_sha256,is_active) VALUES(?,?,?,?,?,?)", ("f1", "app/page.tsx", "tsx", ".tsx", "abc", 1))
    con.execute("INSERT INTO code_symbol(symbol_id,symbol_name,symbol_type,file_version_id,file_id,language,start_line,signature,symbol_sha256) VALUES(?,?,?,?,?,?,?,?,?)", ("s1", "HomePage", "FUNCTION", "v1", "f1", "tsx", 1, "HomePage()", "sh"))
    con.execute("INSERT INTO app_route(route_id,route_path,route_type,file_id,file_version_id,route_sha256) VALUES(?,?,?,?,?,?)", ("r1", "/", "FRONTEND_PAGE", "f1", "v1", "rh"))
    con.execute("INSERT INTO code_import_edge(edge_id,from_file_id,from_path,import_target,import_type,line_number) VALUES(?,?,?,?,?,?)", ("e1", "f1", "app/page.tsx", "react", "import", 1))
    con.execute("INSERT INTO project_artifact(artifact_id,source_file_id,artifact_type,artifact_sha256,path,size_bytes,metadata_json,semantic_status) VALUES(?,?,?,?,?,?,?,?)", ("a1", "f1", "json", "ah", "data/out.json", 42, "{}", "READ_ONLY"))
    con.commit()
    con.close()

    rt.write_project_master_mmd(root)
    text = (project / "topology" / "project_master_topology.mmd").read_text(encoding="utf-8")

    assert "route/file/symbol/import graph from SQLite" in text
    assert "project_mutation_intake" in text
    assert "HomePage" in text
    assert "react" in text
    assert "ChatGPT_LocalAI_<brain>_Sqlite_brain.zip" in text
    assert not (project / "topology" / "local_code_lane.mmd").exists()


def test_code_lane_layered_digestion_studies_artifacts_and_skips_media_assets(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "src" / "db").mkdir(parents=True)
    (repo / "src" / "empty_folder").mkdir(parents=True)
    (repo / "public").mkdir()
    (repo / "src" / "app" / "page.tsx").write_text(
        "import React from 'react'\nexport default function HomePage() {\n  return <button>Build Brain</button>\n}\n",
        encoding="utf-8",
    )
    (repo / "src" / "db" / "schema.sql").write_text("select * from source_file;\n", encoding="utf-8")
    (repo / "src" / "nested.py").write_text(
        "class Service:\n    def run(self):\n        return 'ready'\n",
        encoding="utf-8",
    )
    (repo / "src" / "long.py").write_text(
        "\n".join(f"value_{line_no} = {line_no}" for line_no in range(1, 251)) + "\n",
        encoding="utf-8",
    )
    (repo / "src" / "empty.py").write_text("", encoding="utf-8")
    (repo / "package.json").write_text('{"dependencies":{"react":"^19.0.0"},"devDependencies":{"vite":"latest"}}', encoding="utf-8")
    (repo / "README.md").write_text("# Repo Notes\nartifact study text\n", encoding="utf-8")
    (repo / "evidence.json").write_text('{"status":"read-only artifact study"}', encoding="utf-8")
    (repo / "flow.mmd").write_text("flowchart LR\nA --> B\n", encoding="utf-8")
    (repo / "public" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nmetadata only")
    (repo / "public" / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42metadata only")
    (repo / "public" / "model.glb").write_bytes(b"\x00glTF binary payload should not be studied")
    (repo / "public" / "scene.gltf").write_bytes(b'{"asset":{"version":"2.0"},"forbidden":"semantic study"}')

    project = tmp_path / "project"
    db = project / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    rt.build_code_sector(db, {"path": str(repo), "source_id": "src1"}, project, None, "local_code", 0)

    con = sqlite3.connect(db)
    chunk_types = {row[0] for row in con.execute("SELECT DISTINCT chunk_type FROM code_chunk")}
    deps = {row[0] for row in con.execute("SELECT package_name FROM dependency_item")}
    artifacts = {row[0]: row[1] for row in con.execute("SELECT path, semantic_status FROM project_artifact")}
    media_rows = dict(
        con.execute(
            "SELECT sf.logical_path, sb.coverage_status FROM source_file sf JOIN source_byte_coverage sb USING(file_id) "
            "WHERE sf.logical_path LIKE 'public/%'"
        )
    )
    folders = {
        row[0]: (row[1], row[2])
        for row in con.execute("SELECT normalized_path, raw_path, parent_folder_id FROM code_folder")
    }
    nested_symbols = {
        row[0]: (row[1], row[2], row[3])
        for row in con.execute(
            "SELECT symbol_name, start_line, end_line, parent_symbol_id FROM code_symbol "
            "WHERE file_id=(SELECT file_id FROM code_file WHERE canonical_path='src/nested.py')"
        )
    }
    artifact_chunk_count = con.execute(
        "SELECT COUNT(*) FROM code_chunk WHERE file_id IN (SELECT source_file_id FROM project_artifact)"
    ).fetchone()[0]
    media_semantic_rows = con.execute(
        "SELECT COUNT(*) FROM project_artifact WHERE path LIKE 'public/%'"
    ).fetchone()[0]
    active_code_files = con.execute("SELECT COUNT(*) FROM code_file WHERE is_active=1").fetchone()[0]
    fallback_covered_files = con.execute(
        "SELECT COUNT(DISTINCT file_id) FROM code_chunk WHERE chunk_type='LINE_CHUNK'"
    ).fetchone()[0]
    long_boundaries = list(
        con.execute(
            "SELECT start_line,end_line FROM code_chunk WHERE chunk_type='LINE_CHUNK' "
            "AND file_id=(SELECT file_id FROM code_file WHERE canonical_path='src/long.py') ORDER BY start_line"
        )
    )
    empty_boundaries = list(
        con.execute(
            "SELECT start_line,end_line FROM code_chunk WHERE chunk_type='LINE_CHUNK' "
            "AND file_id=(SELECT file_id FROM code_file WHERE canonical_path='src/empty.py')"
        )
    )
    con.close()

    assert {"FOLDER_CHUNK", "FILE_CHUNK", "SYMBOL_CHUNK", "ROUTE_CHUNK", "IMPORT_CHUNK", "LINE_CHUNK"}.issubset(chunk_types)
    assert {"react", "vite"}.issubset(deps)
    assert artifacts["README.md"] == "READ_ONLY_STUDIED"
    assert artifacts["package.json"] == "READ_ONLY_STUDIED"
    assert artifacts["evidence.json"] == "READ_ONLY_STUDIED"
    assert artifacts["flow.mmd"] == "READ_ONLY_STUDIED"
    assert artifact_chunk_count == 0
    assert "src/empty_folder" in folders
    assert folders["src/empty_folder"][1]
    assert nested_symbols["Service"][2] is None
    assert nested_symbols["run"][2]
    assert nested_symbols["run"][1] >= nested_symbols["run"][0]
    assert media_rows == {
        "public/clip.mp4": "CODE_MEDIA_ASSET_HASH_ONLY",
        "public/image.png": "CODE_MEDIA_ASSET_HASH_ONLY",
        "public/model.glb": "CODE_MEDIA_ASSET_HASH_ONLY",
        "public/scene.gltf": "CODE_MEDIA_ASSET_HASH_ONLY",
    }
    assert media_semantic_rows == 0
    assert fallback_covered_files == active_code_files
    assert long_boundaries == [(1, 120), (121, 240), (241, 250)]
    assert empty_boundaries == [(0, 0)]


def test_code_lane_git_lineage_is_separate_from_code_chunks(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    rt.run_hidden(["git", "init"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, text=True, timeout=30)
    (repo / "app.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n-v1")
    (repo / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftyp-v1")
    (repo / "asset.glb").write_bytes(b"\x00glTF-v1")
    (repo / "scene.gltf").write_text('{"asset":{"version":"2.0"},"revision":1}', encoding="utf-8")
    rt.run_hidden(["git", "add", "app.py", "image.png", "clip.mp4", "asset.glb", "scene.gltf"], cwd=repo, capture_output=True, text=True, timeout=30)
    first = rt.run_hidden(["git", "commit", "-m", "initial app"], cwd=repo, capture_output=True, text=True, timeout=30)
    if first.returncode != 0:
        pytest.skip(f"git commit failed: {first.stderr}")
    (repo / "app.py").write_text("def first():\n    return 2\n", encoding="utf-8")
    rt.run_hidden(["git", "add", "app.py"], cwd=repo, capture_output=True, text=True, timeout=30)
    second = rt.run_hidden(["git", "commit", "-m", "change app"], cwd=repo, capture_output=True, text=True, timeout=30)
    if second.returncode != 0:
        pytest.skip(f"git commit failed: {second.stderr}")
    (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n-v2")
    (repo / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftyp-v2")
    (repo / "asset.glb").write_bytes(b"\x00glTF-v2")
    (repo / "scene.gltf").write_text('{"asset":{"version":"2.0"},"revision":2}', encoding="utf-8")
    rt.run_hidden(["git", "add", "image.png", "clip.mp4", "asset.glb", "scene.gltf"], cwd=repo, capture_output=True, text=True, timeout=30)
    media_commit = rt.run_hidden(["git", "commit", "-m", "change media metadata"], cwd=repo, capture_output=True, text=True, timeout=30)
    if media_commit.returncode != 0:
        pytest.skip(f"git commit failed: {media_commit.stderr}")
    rt.run_hidden(["git", "mv", "app.py", "service.py"], cwd=repo, capture_output=True, text=True, timeout=30)
    rename_commit = rt.run_hidden(["git", "commit", "-m", "rename app service"], cwd=repo, capture_output=True, text=True, timeout=30)
    if rename_commit.returncode != 0:
        pytest.skip(f"git commit failed: {rename_commit.stderr}")

    project = tmp_path / "project"
    db = project / "sectors" / "github" / "github_sector_v001.sqlite"
    rt.build_code_sector(db, {"path": str(repo), "source_id": "src_git"}, project, None, "github", 0)

    con = sqlite3.connect(db)
    commit_count = con.execute("SELECT COUNT(*) FROM git_commit").fetchone()[0]
    head_sha = rt.current_git_head(repo)
    first_recorded_sha = con.execute("SELECT commit_sha FROM git_commit WHERE commit_order=0").fetchone()[0]
    parent_count = con.execute("SELECT COUNT(*) FROM git_commit_parent_edge").fetchone()[0]
    change_count = con.execute("SELECT COUNT(*) FROM git_file_change").fetchone()[0]
    hunk_count = con.execute("SELECT COUNT(*) FROM git_diff_hunk").fetchone()[0]
    line_change_count = con.execute("SELECT COUNT(*) FROM git_line_change").fetchone()[0]
    rename_count = con.execute("SELECT COUNT(*) FROM git_rename_map").fetchone()[0]
    version_commit = con.execute("SELECT commit_sha FROM code_file_version LIMIT 1").fetchone()[0]
    code_version_count = con.execute(
        "SELECT COUNT(*) FROM code_file_version WHERE path_at_commit IN ('app.py','service.py')"
    ).fetchone()[0]
    code_identity_count = con.execute(
        "SELECT COUNT(DISTINCT file_id) FROM code_file_version WHERE path_at_commit IN ('app.py','service.py')"
    ).fetchone()[0]
    media_version_count = con.execute(
        "SELECT COUNT(*) FROM code_file_version WHERE path_at_commit IN ('image.png','clip.mp4','asset.glb','scene.gltf')"
    ).fetchone()[0]
    media_policies = {
        row[0]: json.loads(row[1])["content_policy"]
        for row in con.execute(
            "SELECT path, metadata_json FROM git_file_change "
            "WHERE path IN ('image.png','clip.mp4','asset.glb','scene.gltf')"
        )
    }
    media_hunk_count = con.execute(
        "SELECT COUNT(*) FROM git_diff_hunk "
        "WHERE old_path IN ('image.png','clip.mp4','asset.glb','scene.gltf') "
        "OR new_path IN ('image.png','clip.mp4','asset.glb','scene.gltf')"
    ).fetchone()[0]
    commit_chunks = con.execute("SELECT COUNT(*) FROM code_chunk WHERE chunk_type LIKE '%COMMIT%'").fetchone()[0]
    con.close()

    assert commit_count >= 4
    assert first_recorded_sha == head_sha
    assert parent_count >= 3
    assert change_count >= 11
    assert hunk_count >= 2
    assert line_change_count >= 4
    assert rename_count >= 1
    assert version_commit
    assert code_version_count >= 4
    assert code_identity_count == 1
    assert media_version_count == 0
    assert media_policies == {
        "asset.glb": "MEDIA_ASSET_HASH_METADATA_ONLY",
        "clip.mp4": "MEDIA_ASSET_HASH_METADATA_ONLY",
        "image.png": "MEDIA_ASSET_HASH_METADATA_ONLY",
        "scene.gltf": "MEDIA_ASSET_HASH_METADATA_ONLY",
    }
    assert media_hunk_count == 0
    assert commit_chunks == 0


def test_git_lineage_rerun_is_idempotent_and_indexes_follow_pruning(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    rt.run_hidden(["git", "init"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, text=True, timeout=30)
    (repo / "app.py").write_text("def current():\n    return 1\n", encoding="utf-8")
    rt.run_hidden(["git", "add", "app.py"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, text=True, timeout=30)
    (repo / "app.py").write_text("def current():\n    return 2\n", encoding="utf-8")
    rt.run_hidden(["git", "add", "app.py"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "commit", "-m", "update"], cwd=repo, capture_output=True, text=True, timeout=30)

    project = tmp_path / "project"
    db = project / "sectors" / "github_code" / "github_code_sector_v001.sqlite"
    source = {"path": str(repo), "source_id": "stable_source"}
    rt.build_code_sector(db, source, project, None, "github_code", 0)

    tracked_tables = (
        "git_commit", "git_commit_parent_edge", "git_file_change", "git_diff_hunk",
        "git_line_change", "code_file_version", "commit_fts", "code_index_state",
    )
    con = sqlite3.connect(db)
    counts_before = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked_tables}
    repo_id = con.execute("SELECT id FROM code_repo").fetchone()[0]
    file_ids = dict(con.execute("SELECT canonical_path,file_id FROM code_file WHERE is_active=1"))
    original_git_stdout = rt.git_stdout
    show_calls = []

    def tracking_git_stdout(folder, args, timeout=30):
        if args and args[0] == "show":
            show_calls.append(tuple(args))
        return original_git_stdout(folder, args, timeout)

    monkeypatch.setattr(rt, "git_stdout", tracking_git_stdout)
    rt.record_git_lineage(con, repo, repo_id, "stable_source", file_ids)
    con.commit()
    counts_after_direct_rerun = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked_tables}
    indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    con.close()

    rt.build_code_sector(db, source, project, None, "github_code", 0)
    con = sqlite3.connect(db)
    counts_after_rebuild = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked_tables}
    con.close()

    assert counts_after_direct_rerun == counts_before
    assert counts_after_rebuild == counts_before
    assert show_calls == []
    assert {
        "idx_git_file_change_commit_path",
        "idx_git_diff_hunk_commit_new_path",
        "idx_git_line_change_hunk",
        "idx_git_line_change_commit_path",
        "idx_git_parent_commit",
        "idx_code_file_version_file_commit",
        "idx_code_line_snapshot_version_line",
        "idx_code_chunk_version_start",
    }.issubset(indexes)


def test_code_sector_rerun_accepts_legacy_additive_columns_without_reindex(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    rt.run_hidden(["git", "init"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, text=True, timeout=30)
    (repo / "app.py").write_text(
        "from pathlib import Path\n\ndef current():\n    return Path(__file__).name\n",
        encoding="utf-8",
    )
    (repo / "package.json").write_text('{"dependencies":{"react":"19.0.0"}}', encoding="utf-8")
    rt.run_hidden(["git", "add", "."], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, text=True, timeout=30)

    project = tmp_path / "project"
    db = project / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    source = {"path": str(repo), "source_id": "legacy_additive_source"}
    rt.ensure_sector(db, "local_code")
    rt.build_code_sector(db, source, project, None, "local_code", 0)

    con = sqlite3.connect(db)
    tracked_tables = (
        "code_folder", "code_file", "code_file_version", "code_line_snapshot",
        "code_chunk", "code_symbol", "code_import_edge", "dependency_manifest",
        "dependency_item", "code_dependency_edge", "git_commit", "git_file_change",
    )
    counts_before = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked_tables}
    # The canonical Env15 contract now lists universal code tables; exercise
    # additive columns against the legacy compatibility tables directly.
    for table in tracked_tables:
        if not table.endswith("_fts"):
            rt.ensure_columns(con, table, rt.GENERIC_TABLE_COLUMNS)
    con.commit()
    assert len(con.execute("PRAGMA table_info(code_folder)").fetchall()) > 10
    con.close()

    original_git_stdout = rt.git_stdout
    show_calls = []

    def tracking_git_stdout(folder, args, timeout=30):
        if args and args[0] == "show":
            show_calls.append(tuple(args))
        return original_git_stdout(folder, args, timeout)

    monkeypatch.setattr(rt, "git_stdout", tracking_git_stdout)
    rt.build_code_sector(db, source, project, None, "local_code", 0)

    con = sqlite3.connect(db)
    counts_after = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked_tables}
    con.close()

    assert counts_after == counts_before
    assert show_calls == []


def test_generated_artifact_git_history_is_hunk_summary_not_line_expansion(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    generated = repo / "artifacts" / "models"
    generated_output = repo / "outputs"
    source_dir = repo / "src"
    generated.mkdir(parents=True)
    generated_output.mkdir()
    source_dir.mkdir()
    rt.run_hidden(["git", "init"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.email", "test@example.com"], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "config", "user.name", "Test User"], cwd=repo, capture_output=True, text=True, timeout=30)

    artifact = generated / "generated_forecast.json"
    generated_code = generated_output / "generated_worker.py"
    artifact.write_text("\n".join(f'{{"row":{i},"value":1}}' for i in range(6000)) + "\n", encoding="utf-8")
    generated_code.write_text("\n".join(f"generated_value_{i} = 1" for i in range(3000)) + "\n", encoding="utf-8")
    (source_dir / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# Human notes\nfirst\n", encoding="utf-8")
    rt.run_hidden(["git", "add", "."], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "commit", "-m", "initial project"], cwd=repo, capture_output=True, text=True, timeout=30)

    artifact.write_text("\n".join(f'{{"row":{i},"value":2}}' for i in range(6000)) + "\n", encoding="utf-8")
    generated_code.write_text("\n".join(f"generated_value_{i} = 2" for i in range(3000)) + "\n", encoding="utf-8")
    (source_dir / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (repo / "README.md").write_text("# Human notes\nsecond\n", encoding="utf-8")
    rt.run_hidden(["git", "add", "."], cwd=repo, capture_output=True, text=True, timeout=30)
    rt.run_hidden(["git", "commit", "-m", "update project"], cwd=repo, capture_output=True, text=True, timeout=30)

    project = tmp_path / "project"
    db = project / "sectors" / "github_code" / "github_code_sector_v001.sqlite"
    rt.build_code_sector(db, {"path": str(repo), "source_id": "generated_fixture"}, project, None, "github_code", 0)

    artifact_path = "artifacts/models/generated_forecast.json"
    generated_code_path = "outputs/generated_worker.py"
    con = sqlite3.connect(db)
    artifact_line_rows = con.execute("SELECT COUNT(*) FROM git_line_change WHERE path=?", (artifact_path,)).fetchone()[0]
    artifact_hunks = list(
        con.execute(
            "SELECT history_policy,changed_line_count,stored_line_count,LENGTH(patch_sha256) "
            "FROM git_diff_hunk WHERE new_path=? OR old_path=?",
            (artifact_path, artifact_path),
        )
    )
    code_line_rows = con.execute("SELECT COUNT(*) FROM git_line_change WHERE path='src/app.py'").fetchone()[0]
    generated_code_line_rows = con.execute(
        "SELECT COUNT(*) FROM git_line_change WHERE path=?", (generated_code_path,)
    ).fetchone()[0]
    generated_code_versions = con.execute(
        "SELECT COUNT(*) FROM code_file_version WHERE path_at_commit=?", (generated_code_path,)
    ).fetchone()[0]
    generated_code_snapshot_chunks = con.execute(
        "SELECT COUNT(*) FROM code_chunk c JOIN code_file f ON f.file_id=c.file_id WHERE f.canonical_path=?",
        (generated_code_path,),
    ).fetchone()[0]
    generated_code_artifacts = con.execute(
        "SELECT COUNT(*) FROM project_artifact WHERE path=?", (generated_code_path,)
    ).fetchone()[0]
    doc_line_rows = con.execute("SELECT COUNT(*) FROM git_line_change WHERE path='README.md'").fetchone()[0]
    total_line_rows = con.execute("SELECT COUNT(*) FROM git_line_change").fetchone()[0]
    artifact_metadata = [
        json.loads(row[0])
        for row in con.execute("SELECT metadata_json FROM git_file_change WHERE path=?", (artifact_path,))
    ]
    con.close()

    assert artifact_line_rows == 0
    assert artifact_hunks
    assert all(policy == "HUNK_SUMMARY_GENERATED_ARTIFACT" for policy, _, _, _ in artifact_hunks)
    assert sum(changed for _, changed, _, _ in artifact_hunks) >= 12000
    assert all(stored == 0 and hash_length == 64 for _, _, stored, hash_length in artifact_hunks)
    assert code_line_rows > 0
    assert generated_code_line_rows == 0
    assert generated_code_versions == 0
    assert generated_code_snapshot_chunks == 0
    assert generated_code_artifacts == 1
    assert doc_line_rows > 0
    assert total_line_rows < 100
    assert all(meta["line_history_policy"] == "HUNK_SUMMARY_GENERATED_ARTIFACT" for meta in artifact_metadata)
    assert any(meta["old_blob_oid"] and meta["new_blob_oid"] for meta in artifact_metadata)
    assert all(meta["blob_hash"] and meta["blob_size"] > 0 for meta in artifact_metadata)


def test_data_excel_lane_named_inserts_support_legacy_additive_schema(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")

    csv_path = tmp_path / "sample.csv"
    csv_path.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
    xlsx_path = tmp_path / "sample.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["a", "b", "total"])
    sheet.append([1, 2, "=A2+B2"])
    workbook.save(xlsx_path)
    workbook.close()

    project = tmp_path / "project"
    db = project / "sectors" / "data_excel_csv" / "data_excel_csv_sector_v001.sqlite"
    db.parent.mkdir(parents=True)
    legacy_tables = (
        "sheet_workbook", "sheet_tab", "sheet_cell_sample", "sheet_formula",
        "csv_header", "csv_row_sample", "data_structure_signature",
    )
    con = sqlite3.connect(db)
    for table in legacy_tables:
        con.execute(
            f'''CREATE TABLE "{table}"(
                id TEXT PRIMARY KEY, source_id TEXT, name TEXT, path TEXT, value TEXT,
                active_bool INTEGER DEFAULT 1, created_at TEXT
            )'''
        )
    con.commit()
    con.close()

    csv_source = {"path": str(csv_path), "source_id": "csv_source", "display_name": "CSV fixture"}
    xlsx_source = {"path": str(xlsx_path), "source_id": "xlsx_source", "display_name": "XLSX fixture"}
    rt.build_semantic_lane(db, "data_excel_csv", csv_source, None, 0)
    rt.build_semantic_lane(db, "data_excel_csv", xlsx_source, None, 0)

    con = sqlite3.connect(db)
    counts_before = {
        table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in legacy_tables
    }
    csv_header_columns = {row[1] for row in con.execute("PRAGMA table_info(csv_header)")}
    metadata_value = con.execute("SELECT metadata_json FROM csv_header LIMIT 1").fetchone()[0]
    active_values = {row[0] for row in con.execute("SELECT active_bool FROM csv_header")}
    con.close()

    rt.build_semantic_lane(db, "data_excel_csv", csv_source, None, 0)
    rt.build_semantic_lane(db, "data_excel_csv", xlsx_source, None, 0)
    con = sqlite3.connect(db)
    counts_after = {
        table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in legacy_tables
    }
    formula_count = con.execute("SELECT COUNT(*) FROM sheet_formula").fetchone()[0]
    con.close()

    assert "metadata_json" in csv_header_columns
    assert json.loads(metadata_value)["columns"] == 2
    assert active_values == {1}
    assert counts_after == counts_before
    assert counts_before["csv_header"] == 1
    assert counts_before["csv_row_sample"] == 2
    # T021 routes delimited sources before workbook insertion: only XLSX is a workbook.
    assert counts_before["sheet_workbook"] == 1
    assert counts_before["sheet_tab"] == 1
    assert formula_count == 1
