from __future__ import annotations

import sqlite3
import shutil
import subprocess
import hashlib
import json
import zlib
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime import stable_runtime_v53
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from sqlite_brain_builder.runtime.env15_code_ingestion import Env15CodeIngestionError, ingest_env15_code_source
from sqlite_brain_builder.runtime.env15_project_schema import initialize_env15_brain_root, resolve_env15_sector


def test_local_code_build_uses_env15_code_snapshot_schema_and_is_index_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source_project"
    source.mkdir()
    (source / "app.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get('/health')\ndef health():\n    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    payload = {
        "source_id": "source_local_code",
        "lane_key": "local_code",
        "display_name": source.name,
        "path": str(source),
        "active": True,
    }

    first = build_brain(str(tmp_path), "Code Brain", [payload], generate_mmd=False)
    database = Path(first["brain_root"]) / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    tracked = (
        "source_registry", "code_source_registry", "code_file_snapshot", "code_chunk",
        "chunk_index", "code_chunk_fts", "code_symbol", "code_route_api_boundary", "code_index_checkpoint",
        "source_byte_coverage", "code_file_version", "code_line_snapshot", "code_exact_byte_chunk",
    )
    with sqlite3.connect(database) as connection:
        before_counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked}
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    before_size = database.stat().st_size
    monkeypatch.setattr(
        stable_runtime_v53,
        "build_code_sector",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unchanged code was re-hashed and re-indexed")),
    )
    second = build_brain(str(tmp_path), "Code Brain", [payload], generate_mmd=False)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM code_source_registry").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM code_file_snapshot").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM code_chunk").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM code_symbol").fetchone()[0] >= 1
        assert connection.execute(
            "SELECT route_or_api FROM code_route_api_boundary"
        ).fetchone()[0] == "/health"
        assert connection.execute(
            "SELECT COUNT(*) FROM code_workflow_edge WHERE relation_type='EXPOSES_ROUTE'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM code_workflow_edge WHERE relation_type='SERVES_ROUTE'"
        ).fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM code_index_checkpoint").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM mutation_receipt").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM code_chunk_fts").fetchone()[0] >= 1
        assert connection.execute("SELECT proof_status FROM code_source_active_head").fetchone()[0] == "REF_SNAPSHOT_ONLY"
        assert connection.execute("SELECT COUNT(*) FROM code_workflow_edge").fetchone()[0] >= 3
        assert connection.execute("SELECT COUNT(*) FROM source_byte_coverage").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM code_file_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM code_line_snapshot").fetchone()[0] >= 1
        assert connection.execute(
            "SELECT SUM(byte_end_exclusive-byte_start) FROM code_exact_byte_chunk"
        ).fetchone()[0] == (source / "app.py").stat().st_size
        assert connection.execute(
            "SELECT projection_version FROM code_projection_state"
        ).fetchone()[0] == "env15_code_projection_v6_full_text_artifact_chunks_binary_metadata_exact_text"
        reconstructed = b"".join(
            zlib.decompress(row[0])
            for row in connection.execute(
                "SELECT compressed_payload FROM code_exact_byte_chunk ORDER BY chunk_ordinal"
            )
        )
        assert reconstructed == (source / "app.py").read_bytes()
        assert connection.execute("SELECT diff_type FROM code_semantic_diff").fetchone()[0] == "SNAPSHOT_DIFF_NOT_GIT_HISTORY"
        assert connection.execute("SELECT COUNT(*) FROM code_synthetic_snapshot_file").fetchone()[0] >= 1
        assert {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tracked} == before_counts
    finally:
        connection.close()
    assert first["brain_root"] == second["brain_root"]
    assert first["incremental"]["sources_indexed"] == 1
    assert second["incremental"]["sources_indexed"] == 0
    assert second["incremental"]["sources_reused"] == 1
    assert second["incremental"]["all_sources_unchanged"] is True
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert database.stat().st_size == before_size


def test_code_staging_failure_leaves_governed_sector_unchanged_and_writes_receipt(tmp_path: Path) -> None:
    root = tmp_path / "brain_output"
    initialize_env15_brain_root(root)
    _, destination = resolve_env15_sector(root, "local_code")
    before = hashlib.sha256(destination.read_bytes()).hexdigest()
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise OSError(22, "fixture invalid argument")

    with pytest.raises(Env15CodeIngestionError, match="CODE_STAGING_BUILD_FAILED:OSError"):
        ingest_env15_code_source(
            root,
            "local_code",
            {"source_id": "source_failure", "path": str(source)},
            legacy_builder=fail,
        )

    after = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert after == before
    receipts = list((root / "receipts" / "failed_builds").glob("*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert payload["operation"] == "legacy_code_staging_build"
    assert payload["destination_unchanged"] is True
    assert payload["error_type"] == "OSError"


def test_disposable_adapter_does_not_extract_or_copy_reference_artifact_payload(tmp_path: Path) -> None:
    source = tmp_path / "source_project"
    source.mkdir()
    (source / "app.py").write_text("def ok():\n    return True\n", encoding="utf-8")
    (source / "translation.json").write_text('{"large":"reference UI text"}', encoding="utf-8")
    result = build_brain(
        str(tmp_path),
        "Artifact Brain",
        [{"source_id": "source_artifact", "lane_key": "local_code", "path": str(source), "active": True}],
        generate_mmd=False,
    )
    assert not (Path(result["brain_root"]) / "project" / "artifacts" / "code_asset_vault").exists()


def test_code_media_assets_are_hash_only_while_text_and_artifacts_have_exact_byte_chunks(tmp_path: Path) -> None:
    source = tmp_path / "mixed_project"
    assets = source / "assets"
    assets.mkdir(parents=True)
    (source / "app.py").write_text("print('exact text')\n", encoding="utf-8")
    (source / "artifact.json").write_text('{"source_id":"ARTIFACT-EXACT-001"}\n', encoding="utf-8")
    large_artifact = '{"payload":"' + ("gold-record-" * 7_000) + '"}\n'
    (source / "large_artifact.json").write_text(large_artifact, encoding="utf-8")
    large_artifact_bytes = (source / "large_artifact.json").read_bytes()
    (assets / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (assets / "character.glb").write_bytes(b"glTF\x02\x00\x00\x00")
    (assets / "demo.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (source / "table.parquet").write_bytes(b"PAR1\x00binary-columnar-fixture")

    result = build_brain(
        str(tmp_path),
        "Mixed Code Brain",
        [{"source_id": "source_mixed", "lane_key": "local_code", "path": str(source), "active": True}],
        generate_mmd=False,
    )
    database = Path(result["brain_root"]) / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        kinds = dict(connection.execute("SELECT relative_path,content_kind FROM code_file_snapshot"))
        exact_paths = {
            row[0] for row in connection.execute("SELECT DISTINCT relative_path FROM code_exact_byte_chunk")
        }
        artifact_chunks = connection.execute(
            "SELECT COUNT(*) FROM code_chunk WHERE chunk_type LIKE 'ARTIFACT_TEXT_CHUNK:%'"
        ).fetchone()[0]
        largest_search_projection = connection.execute(
            "SELECT MAX(length(chunk_text)) FROM code_chunk "
            "WHERE chunk_type LIKE 'ARTIFACT_TEXT_CHUNK:%'"
        ).fetchone()[0]
        large_artifact_file_id = connection.execute(
            "SELECT file_id FROM code_file_snapshot WHERE relative_path='large_artifact.json'"
        ).fetchone()[0]
        reconstructed_large_artifact = b"".join(
            zlib.decompress(row[0])
            for row in connection.execute(
                "SELECT compressed_payload FROM code_exact_byte_chunk "
                "WHERE file_id=? ORDER BY chunk_ordinal",
                (large_artifact_file_id,),
            )
        )
    assert kinds["app.py"] == "TEXT_INDEXED"
    assert kinds["artifact.json"] == "TEXT_INDEXED"
    assert exact_paths >= {"app.py", "artifact.json"}
    assert "large_artifact.json" in exact_paths
    assert not {"assets/pixel.png", "assets/character.glb", "assets/demo.mp4", "table.parquet"} & exact_paths
    assert all(kinds[path] == "BINARY_METADATA_ONLY" for path in (
        "assets/pixel.png", "assets/character.glb", "assets/demo.mp4", "table.parquet"
    ))
    assert artifact_chunks >= 1
    assert largest_search_projection <= 64 * 1024
    assert reconstructed_large_artifact == large_artifact_bytes


def test_prospect_asset_name_is_not_misclassified_as_a_test(tmp_path: Path) -> None:
    source = tmp_path / "book_fairies"
    images = source / "images"
    images.mkdir(parents=True)
    (source / "index.html").write_text("<img src='images/prospectparkgraphic.webp'>\n", encoding="utf-8")
    (images / "prospectparkgraphic.webp").write_bytes(b"RIFFfixtureWEBP")

    result = build_brain(
        str(tmp_path),
        "Prospect Asset Brain",
        [{"source_id": "source_prospect", "lane_key": "github_code", "path": str(source), "active": True}],
        generate_mmd=False,
    )
    database = Path(result["brain_root"]) / "project" / "sectors" / "github_code" / "github_code_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        false_test_edges = connection.execute(
            "SELECT COUNT(*) FROM code_workflow_edge "
            "WHERE relation_type IN ('IMPLEMENTS_TEST','PRODUCES_TEST_ARTIFACT') "
            "AND evidence_ref LIKE '%prospectparkgraphic.webp%'"
        ).fetchone()[0]
    assert false_test_edges == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_local_code_git_worktree_remains_snapshot_only_without_reverse_history(tmp_path: Path) -> None:
    source = tmp_path / "local_snapshot"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
    app = source / "app.py"
    app.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=source, check=True, capture_output=True)
    app.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=source, check=True, capture_output=True)

    result = build_brain(
        str(tmp_path),
        "Local Snapshot Brain",
        [{"source_id": "source_local_snapshot", "lane_key": "local_code", "path": str(source), "active": True}],
        generate_mmd=False,
    )
    database = Path(result["brain_root"]) / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM git_commit_registry").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM git_file_change").fetchone()[0] == 0
        assert connection.execute("SELECT proof_status FROM code_source_active_head").fetchone()[0] == "REF_SNAPSHOT_ONLY"
        assert connection.execute("SELECT COUNT(*) FROM code_file_version").fetchone()[0] == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_git_code_build_maps_reverse_history_and_patch_hunks_into_env15(tmp_path: Path) -> None:
    source = tmp_path / "git_project"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
    app = source / "app.py"
    app.write_text("def value():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=source, check=True, capture_output=True)
    app.write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/health')\n"
        "def route():\n    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    (source / "requirements.txt").write_text("fastapi==1.0\n", encoding="utf-8")
    (source / "test_app.py").write_text("def test_route():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py", "requirements.txt", "test_app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "second"], cwd=source, check=True, capture_output=True)

    result = build_brain(
        str(tmp_path),
        "Git Brain",
        [{"source_id": "source_git", "lane_key": "github_code", "path": str(source), "active": True}],
        generate_mmd=False,
    )

    database = Path(result["brain_root"]) / "project" / "sectors" / "github_code" / "github_code_sector_v001.sqlite"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM git_commit_registry").fetchone()[0] >= 2
        assert connection.execute("SELECT COUNT(*) FROM git_file_change").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM git_patch_hunk").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM git_exact_line_change").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM snapshot_git_bridge").fetchone()[0] >= 1
        assert connection.execute("SELECT has_git_history FROM code_source_registry").fetchone()[0] == 1
        assert connection.execute("SELECT head_commit_sha FROM code_source_active_head").fetchone()[0]
        assert connection.execute("SELECT proof_status FROM code_source_active_head").fetchone()[0] == "REF_SNAPSHOT_ONLY"
        assert connection.execute("SELECT COUNT(*) FROM git_push_event").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM code_workflow_edge").fetchone()[0] >= 3
        assert connection.execute("SELECT diff_type FROM code_semantic_diff").fetchone()[0] == "GIT_REVERSE_HISTORY"
        assert connection.execute("SELECT COUNT(*) FROM code_index_checkpoint").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM git_commit_registry WHERE authored_at<>'' AND committed_at<>'' "
            "AND author_name<>'' AND committer_name<>'' AND author_email_hash<>'' AND committer_email_hash<>''"
        ).fetchone()[0] >= 2
        assert connection.execute("SELECT COUNT(*) FROM git_symbol_impact").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM git_dependency_impact").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM git_test_impact").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM git_artifact_impact").fetchone()[0] >= 1
        relations = {row[0] for row in connection.execute("SELECT DISTINCT relation_type FROM code_workflow_edge")}
        assert {
            "HAS_SNAPSHOT_HEAD", "DECLARES_SYMBOL", "SERVES_ROUTE",
            "VERIFIED_BY_TEST", "PRODUCES_TEST_ARTIFACT",
        } <= relations
    finally:
        connection.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")
def test_github_push_is_recorded_only_with_matching_provider_or_reflog_proof(tmp_path: Path) -> None:
    source = tmp_path / "provider_project"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
    (source / "app.py").write_text("def ready():\n    return True\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "provider fixture"], cwd=source, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    result = build_brain(
        str(tmp_path),
        "Provider Proof Brain",
        [{
            "source_id": "source_provider",
            "lane_key": "github_code",
            "path": str(source),
            "active": True,
            "metadata": {
                "repo_url": "https://github.com/example/provider-project.git",
                "branch": "main",
                "push_proof": {
                    "proof_source": "PROVIDER_API",
                    "provider_event_id": "event-001",
                    "ref_name": "refs/heads/main",
                    "before_sha": "0" * 40,
                    "after_sha": head,
                    "pushed_at": "2026-07-11T00:00:00Z",
                },
            },
        }],
        generate_mmd=False,
    )
    database = Path(result["brain_root"]) / "project" / "sectors" / "github_code" / "github_code_sector_v001.sqlite"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT proof_status FROM code_source_active_head").fetchone()[0] == "PUSH_PROVEN"
        assert connection.execute(
            "SELECT provider_event_id,after_sha,proof_source,proof_status FROM git_push_event"
        ).fetchone() == ("event-001", head, "PROVIDER_API", "PROVIDER_API_PROVEN")
        assert connection.execute("SELECT repository_url,branch_name FROM code_source_registry").fetchone() == (
            "https://github.com/example/provider-project.git", "main"
        )
    finally:
        connection.close()


def test_non_git_changed_snapshot_records_synthetic_file_route_symbol_dependency_test_and_config_impacts(tmp_path: Path) -> None:
    source = tmp_path / "plain_project"
    source.mkdir()
    app = source / "app.py"
    app.write_text("def health():\n    return '/health'\n", encoding="utf-8")
    config = source / "requirements.txt"
    config.write_text("fastapi==1.0\n", encoding="utf-8")
    test_file = source / "test_app.py"
    test_file.write_text("def test_health():\n    assert True\n", encoding="utf-8")
    payload = {"source_id": "source_plain", "lane_key": "local_code", "path": str(source), "active": True}
    first = build_brain(str(tmp_path), "Synthetic Brain", [payload], generate_mmd=False)
    app.write_text("def health():\n    return '/ready'\n\ndef added():\n    return 2\n", encoding="utf-8")
    config.write_text("fastapi==2.0\n", encoding="utf-8")
    test_file.unlink()
    (source / "test_ready.py").write_text("def test_ready():\n    assert True\n", encoding="utf-8")
    build_brain(str(tmp_path), "Synthetic Brain", [payload], generate_mmd=False)

    database = Path(first["brain_root"]) / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    connection = sqlite3.connect(database)
    try:
        latest = connection.execute(
            "SELECT diff_id,diff_type,prior_snapshot_sha256,current_snapshot_sha256,prior_good_snapshot_id,summary_json FROM code_semantic_diff "
            "WHERE source_id='source_plain' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        rows = connection.execute(
            "SELECT relative_path,change_type,impact_json FROM code_synthetic_snapshot_file WHERE diff_id=? ORDER BY relative_path",
            (latest[0],),
        ).fetchall()
    finally:
        connection.close()
    impacts = {path: (change, json.loads(payload_json)) for path, change, payload_json in rows}
    assert latest[1] == "SNAPSHOT_DIFF_NOT_GIT_HISTORY"
    assert latest[2] and latest[2] != latest[3]
    assert latest[4] is None
    assert json.loads(latest[5])["prior_snapshot_basis"] == "PREVIOUS_INDEXED_SNAPSHOT_UNVERIFIED"
    assert impacts["app.py"][0] == "MODIFY"
    assert impacts["app.py"][1]["symbol_changed"] is True
    assert impacts["requirements.txt"][1]["config_changed"] is True
    assert impacts["test_app.py"][0] == "DELETE"
    assert impacts["test_app.py"][1]["test_changed"] is True
    assert impacts["test_ready.py"][0] == "ADD"
