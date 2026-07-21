from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import sqlite_brain_builder.codex_handoff as codex_handoff
import sqlite_brain_builder.ipc_worker as ipc_worker
from sqlite_brain_builder.brain_versions import capture_brain_version
from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.project_delta_ledger import record_refresh_delta
from sqlite_brain_builder.telemetry_overlay import (
    BrainTelemetryError,
    get_telemetry_snapshot,
    list_telemetry_deltas,
    open_telemetry_target,
    query_telemetry_graph,
    telemetry_contract,
    validate_telemetry_open_target,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _versioned_indexed_brain(
    tmp_path: Path,
    *,
    semantic: bool = True,
    env15_canonical_path: bool = False,
) -> dict[str, object]:
    workspace = tmp_path / "workspace"
    brain_name = "Telemetry Fixture"
    brain_root = brain_output_dir(workspace, brain_name)
    source_root = tmp_path / "captured-source"
    source_file = source_root / "src" / "api" / "items.py"
    test_file = source_root / "tests" / "test_items.py"
    source_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source_file.write_text("def fetch_items():\n    return ['one']\n", encoding="utf-8")
    test_file.write_text("def test_fetch_items():\n    assert True\n", encoding="utf-8")

    router = brain_root / "project" / "project_router.sqlite"
    sector = brain_root / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"
    router.parent.mkdir(parents=True)
    sector.parent.mkdir(parents=True)
    relative_sector = sector.relative_to(brain_root).as_posix()
    with closing(sqlite3.connect(router)) as connection:
        connection.execute("CREATE TABLE sector_registry(sector_id TEXT PRIMARY KEY, sqlite_path TEXT NOT NULL)")
        connection.execute("INSERT INTO sector_registry VALUES(?,?)", ("local_code", relative_sector))
        connection.commit()

    first_hash = _sha256(source_file.read_text(encoding="utf-8"))
    test_hash = _sha256(test_file.read_text(encoding="utf-8"))
    locator_column = "canonical_path" if env15_canonical_path else "source_locator"
    with closing(sqlite3.connect(sector)) as connection:
        connection.executescript(
            f"""
            CREATE TABLE source_registry(
              source_id TEXT PRIMARY KEY,lane_id TEXT,display_name TEXT,{locator_column} TEXT,
              source_sha256 TEXT,source_size INTEGER,availability_state TEXT,created_turn TEXT
            );
            CREATE TABLE code_file_snapshot(
              file_id TEXT PRIMARY KEY,source_id TEXT,relative_path TEXT,language TEXT,
              size_bytes INTEGER,sha256 TEXT,snapshot_sha256 TEXT
            );
            CREATE TABLE code_symbol(
              symbol_id TEXT PRIMARY KEY,file_id TEXT,symbol_type TEXT,symbol_name TEXT,
              start_line INTEGER,end_line INTEGER,signature TEXT
            );
            CREATE TABLE code_route_api_boundary(
              route_id TEXT PRIMARY KEY,file_id TEXT,route_path TEXT,http_method TEXT,
              input_contract TEXT,output_contract TEXT,authority TEXT
            );
            CREATE TABLE artifact_registry(
              artifact_id TEXT PRIMARY KEY,source_id TEXT,artifact_type TEXT,logical_path TEXT,
              sha256 TEXT,size_bytes INTEGER,status TEXT
            );
            CREATE TABLE code_workflow_edge(
              edge_id TEXT PRIMARY KEY,source_id TEXT,from_type TEXT,from_id TEXT,
              relation_type TEXT,to_type TEXT,to_id TEXT,evidence_ref TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO source_registry VALUES(?,?,?,?,?,?,?,?)",
            ("source_code", "local_code", "Captured source", str(source_root), first_hash, 100, "AVAILABLE", "turn_one"),
        )
        connection.executemany(
            "INSERT INTO code_file_snapshot VALUES(?,?,?,?,?,?,?)",
            [
                ("file_items", "source_code", "src/api/items.py", "python", source_file.stat().st_size, first_hash, "snapshot_one"),
                ("file_test", "source_code", "tests/test_items.py", "python", test_file.stat().st_size, test_hash, "snapshot_one"),
            ],
        )
        connection.execute(
            "INSERT INTO code_symbol VALUES(?,?,?,?,?,?,?)",
            ("symbol_fetch", "file_items", "function", "fetch_items", 1, 2, "def fetch_items()"),
        )
        connection.execute(
            "INSERT INTO code_route_api_boundary VALUES(?,?,?,?,?,?,?)",
            ("route_items", "file_items", "/api/items", "GET", None, None, "INDEXED"),
        )
        connection.executemany(
            "INSERT INTO artifact_registry VALUES(?,?,?,?,?,?,?)",
            [
                ("artifact_items", "source_code", "code_file", "src/api/items.py", first_hash, source_file.stat().st_size, "INDEXED"),
                ("artifact_test", "source_code", "test_file", "tests/test_items.py", test_hash, test_file.stat().st_size, "INDEXED"),
            ],
        )
        connection.executemany(
            "INSERT INTO code_workflow_edge VALUES(?,?,?,?,?,?,?,?)",
            [
                ("edge_source_file", "source_code", "source", "source_code", "CONTAINS_FILE", "file", "file_items", "src/api/items.py"),
                ("edge_source_test", "source_code", "source", "source_code", "CONTAINS_FILE", "file", "file_test", "tests/test_items.py"),
                ("edge_symbol", "source_code", "file", "file_items", "DECLARES_SYMBOL", "symbol", "symbol_fetch", "def fetch_items()"),
                ("edge_route", "source_code", "file", "file_items", "EXPOSES_ROUTE", "route", "route_items", "/api/items"),
                ("edge_test", "source_code", "route", "route_items", "VERIFIED_BY_TEST", "test", "file_test", "tests/test_items.py"),
                ("edge_artifact", "source_code", "file", "file_items", "PROJECTS_ARTIFACT", "artifact", "artifact_items", "src/api/items.py"),
            ],
        )
        connection.commit()

    first = capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="telemetry fixture",
        reason="capture first telemetry version",
    )

    second_hash = _sha256("def fetch_items():\n    return ['one', 'two']\n")
    with closing(sqlite3.connect(sector)) as connection:
        connection.execute(
            "UPDATE code_file_snapshot SET sha256=?,snapshot_sha256=? WHERE file_id='file_items'",
            (second_hash, "snapshot_two"),
        )
        connection.execute(
            "UPDATE source_registry SET source_sha256=?,created_turn=? WHERE source_id='source_code'",
            (second_hash, "turn_two"),
        )
        connection.commit()
    second = capture_brain_version(
        workspace,
        brain_name,
        actor_type="app",
        actor_name="telemetry fixture",
        reason="capture second telemetry version",
    )

    if semantic:
        connection = codex_handoff._connect(brain_root)
        try:
            connection.executemany(
                "INSERT INTO brain_good_snapshot VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("good_one", first["version_id"], first["snapshot_hash"], "first.zip", "1" * 64, "PASS", "PASS", "2026-07-14T00:00:00Z", "first good"),
                    ("good_two", second["version_id"], second["snapshot_hash"], "second.zip", "2" * 64, "FAIL", "PASS", "2026-07-14T00:01:00Z", "second validation"),
                ],
            )
            connection.executemany(
                "INSERT INTO brain_semantic_memory_version VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("memory_one", "good_one", first["version_id"], first["snapshot_hash"], "PASS", "PASS", "PASS", "2026-07-14T00:00:00Z"),
                    ("memory_two", "good_two", second["version_id"], second["snapshot_hash"], "FAIL", "PASS", "PASS", "2026-07-14T00:01:00Z"),
                ],
            )
            connection.execute(
                "INSERT INTO brain_semantic_diff_run VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("diff_fixture", "good_one", "good_two", first["snapshot_hash"], second["snapshot_hash"], str(brain_root), "2026-07-14T00:01:00Z", "fixture", "fixture delta", "FAIL", "PASS", "PASS"),
            )
            connection.execute(
                "INSERT INTO brain_changed_file VALUES(?,?,?,?,?)",
                ("diff_fixture", "src/api/items.py", "MODIFIED", first_hash, second_hash),
            )
            connection.execute(
                "INSERT INTO brain_route_change VALUES(?,?,?,?)",
                ("diff_fixture", "/api/items", "PASSED", "receipts/route-pass.json"),
            )
            connection.execute(
                "INSERT INTO brain_symbol_change VALUES(?,?,?,?)",
                ("diff_fixture", "fetch_items", "FAILED", "tests/test_items.py::test_fetch_items"),
            )
            connection.executemany(
                "INSERT INTO brain_test_build_result VALUES(?,?,?,?)",
                [
                    ("diff_fixture", "tests", "FAIL", "tests/test_items.py::test_fetch_items"),
                    ("diff_fixture", "build", "PASS", "receipts/build-pass.json"),
                    ("diff_fixture", "package_validation", "PASS", "receipts/package-pass.json"),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    return {
        "workspace": workspace,
        "brain_name": brain_name,
        "brain_root": brain_root,
        "source_root": source_root,
        "source_file": source_file,
        "first": first,
        "second": second,
    }


def _record_refresh_truth_fixture(fixture: dict[str, object]) -> dict[str, object]:
    """Bind explicit Refresh-button truth to the indexed telemetry objects."""

    return record_refresh_delta(
        fixture["brain_root"],
        brain_id="brain_telemetry_fixture",
        brain_name=str(fixture["brain_name"]),
        candidate_id="refresh-telemetry-fixture",
        refresh_status="AWAITING_FUSE",
        classification="CHANGE_DETECTED",
        before_snapshot_hash=str(fixture["first"]["snapshot_hash"]),
        after_snapshot_hash=str(fixture["second"]["snapshot_hash"]),
        refresh_receipt_path="receipts/refresh-telemetry-fixture.json",
        refresh_receipt_sha256="A" * 64,
        source_classifications=[
            {
                "source_id": "source_code",
                "lane_id": "local_code",
                "classification": "SOURCE_CHANGED",
            }
        ],
        changed_files=[
            {
                "path": "src/api/items.py",
                "change_kind": "MODIFIED",
                "previous_sha256": "B" * 64,
                "current_sha256": "C" * 64,
            }
        ],
        refresh_recorded_at="2026-07-20T20:00:00.000000Z",
        delta_type="GIT_WORKTREE_REFRESH_DELTA",
        parent_snapshot_id=str(fixture["first"]["version_id"]),
        child_snapshot_id=str(fixture["second"]["version_id"]),
        source_state={
            "source_state": "CHANGE_DETECTED",
            "git": {
                "repository_root": str(fixture["source_root"]),
                "branch": "fixture",
                "head_commit_sha": "D" * 40,
                "worktree_state": "DIRTY",
            },
        },
        node_changes=[
            {
                "object_id": "file:file_items",
                "object_kind": "file",
                "relative_path": "src/api/items.py",
                "change_kind": "MODIFIED",
                "truth_state": "GREEN",
                "review_required": False,
                "validation_ids": ["validation-route"],
                "evidence": {"direct_or_transitive": "DIRECT"},
            },
            {
                "object_id": "route:route_items",
                "object_kind": "route",
                "relative_path": "/api/items",
                "change_kind": "MODIFIED",
                "truth_state": "GREEN",
                "review_required": False,
                "validation_ids": ["validation-route"],
                "evidence": {"direct_or_transitive": "PROVEN_DIRECT"},
            },
            {
                "object_id": "symbol:symbol_fetch",
                "object_kind": "symbol",
                "relative_path": "fetch_items",
                "change_kind": "MODIFIED",
                "truth_state": "RED",
                "review_required": False,
                "validation_ids": ["validation-symbol"],
                "evidence": {"direct_or_transitive": "PROVEN_DIRECT"},
            },
        ],
        edge_changes=[
            {
                "object_id": "edge_route",
                "relation_type": "EXPOSES_ROUTE",
                "from_object_id": "file:file_items",
                "to_object_id": "route:route_items",
                "change_kind": "MODIFIED",
                "truth_state": "GREEN",
                "review_required": False,
                "validation_ids": ["validation-route"],
                "evidence": {"direct_or_transitive": "PROVEN_DIRECT"},
            },
            {
                "object_id": "edge_symbol",
                "relation_type": "DECLARES_SYMBOL",
                "from_object_id": "file:file_items",
                "to_object_id": "symbol:symbol_fetch",
                "change_kind": "BROKEN",
                "truth_state": "RED",
                "review_required": False,
                "validation_ids": ["validation-symbol"],
                "evidence": {"direct_or_transitive": "PROVEN_DIRECT"},
            },
        ],
        validation_results=[
            {
                "validation_id": "validation-route",
                "validation_type": "ROUTE_CHECK",
                "command": "fixture-route-check",
                "started_at": "2026-07-20T20:00:00Z",
                "ended_at": "2026-07-20T20:00:01Z",
                "exit_code": 0,
                "status": "PASS",
                "affected_files": ["src/api/items.py"],
                "stdout": "route passed",
                "stderr": "",
                "receipt_path": "receipts/route-pass.json",
                "receipt_sha256": "E" * 64,
            },
            {
                "validation_id": "validation-symbol",
                "validation_type": "SYMBOL_CHECK",
                "command": "fixture-symbol-check",
                "started_at": "2026-07-20T20:00:00Z",
                "ended_at": "2026-07-20T20:00:01Z",
                "exit_code": 1,
                "status": "FAIL",
                "affected_files": ["src/api/items.py"],
                "stdout": "",
                "stderr": "symbol failed",
                "receipt_path": "receipts/symbol-fail.json",
                "receipt_sha256": "F" * 64,
            },
        ],
        relationship_comparison_complete=True,
    )


def test_contract_is_read_only_bounded_and_keeps_public_ui_deferred() -> None:
    contract = telemetry_contract()

    assert contract["schema"] == "EVIDENCEOS_3D_BRAIN_TELEMETRY_READ_V1"
    assert contract["read_only"] is True
    assert contract["creates_sqlite_schema"] is False
    assert contract["duplicates_topology"] is False
    assert contract["delta_sandbox_status"] == "SUPERSEDED"
    assert contract["production_3d_ui_status"] == "ACTIVE_PUBLIC_V1_REAL_BACKEND"
    assert contract["commands"] == [
        "brain.telemetry.contract",
        "brain.telemetry.snapshot",
        "brain.telemetry.deltas",
        "brain.telemetry.graph",
        "brain.telemetry.openTarget",
    ]


def test_graph_reuses_indexed_topology_after_raw_source_is_unavailable(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)
    shutil.rmtree(fixture["source_root"])

    snapshot = get_telemetry_snapshot(fixture["workspace"], fixture["brain_name"])
    graph = query_telemetry_graph(fixture["workspace"], fixture["brain_name"])

    assert snapshot["status"] == "PASS"
    assert snapshot["current_version"]["version_id"] == fixture["second"]["version_id"]
    assert graph["status"] == "PASS"
    assert graph["raw_project_files_reread"] == 0
    assert graph["duplicate_topology_created"] is False
    assert {node["kind"] for node in graph["nodes"]} >= {"brain", "source", "folder", "file", "symbol", "route", "test", "artifact"}
    assert any(node["label"] == "src/api/items.py" for node in graph["nodes"])
    assert any(edge["relation_type"] == "DECLARES_SYMBOL" for edge in graph["edges"])


def test_graph_and_delta_queries_warm_native_worker_session_caches(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)

    first_deltas = list_telemetry_deltas(fixture["workspace"], fixture["brain_name"])
    second_deltas = list_telemetry_deltas(fixture["workspace"], fixture["brain_name"])
    assert first_deltas["delta_cache"] == {"status": "MISS", "scope": "NATIVE_WORKER_SESSION"}
    assert second_deltas["delta_cache"] == {"status": "HIT", "scope": "NATIVE_WORKER_SESSION"}
    assert second_deltas["deltas"] == first_deltas["deltas"]

    first_graph = query_telemetry_graph(fixture["workspace"], fixture["brain_name"])
    second_graph = query_telemetry_graph(fixture["workspace"], fixture["brain_name"])
    assert first_graph["topology_cache"]["status"] == "MISS"
    assert second_graph["topology_cache"]["status"] == "HIT"
    assert first_graph["topology_cache"]["immutable_index_key"] == second_graph["topology_cache"]["immutable_index_key"]
    assert second_graph["nodes"] == first_graph["nodes"]
    assert second_graph["edges"] == first_graph["edges"]
    assert second_graph["raw_project_files_reread"] == 0


def test_graph_exposes_exact_indexed_folder_and_file_open_targets(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)
    graph = query_telemetry_graph(fixture["workspace"], fixture["brain_name"])

    source = next(node for node in graph["nodes"] if node["kind"] == "source")
    folder = next(node for node in graph["nodes"] if node["kind"] == "folder" and node["label"] == "src")
    file_node = next(node for node in graph["nodes"] if node["kind"] == "file" and node["label"] == "src/api/items.py")
    assert Path(source["open_target"]).resolve() == fixture["source_root"].resolve()
    assert Path(folder["open_target"]).resolve() == (fixture["source_root"] / "src").resolve()
    assert Path(file_node["open_target"]).resolve() == fixture["source_file"].resolve()


def test_env15_canonical_path_is_the_native_open_boundary(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path, env15_canonical_path=True)
    graph = query_telemetry_graph(fixture["workspace"], fixture["brain_name"])

    source = next(node for node in graph["nodes"] if node["kind"] == "source")
    folder = next(node for node in graph["nodes"] if node["kind"] == "folder" and node["label"] == "src")
    file_node = next(
        node
        for node in graph["nodes"]
        if node["kind"] == "file" and node["label"] == "src/api/items.py"
    )
    assert Path(source["open_target"]).resolve() == fixture["source_root"].resolve()
    assert Path(folder["open_target"]).resolve() == (fixture["source_root"] / "src").resolve()
    assert Path(file_node["open_target"]).resolve() == fixture["source_file"].resolve()
    assert validate_telemetry_open_target(
        fixture["workspace"], fixture["brain_name"], file_node["open_target"]
    )["status"] == "PASS"


def test_catalog_workspace_discovery_is_durable_until_explicit_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    route_root = tmp_path / "legacy-root"
    workspace = route_root / "nested-workspace"
    workspace.mkdir(parents=True)
    database = workspace / "workspace.sqlite"
    database.write_bytes(b"catalog-marker")
    cache_path = tmp_path / "roaming" / "brain-catalog-workspaces.json"
    monkeypatch.setattr(ipc_worker, "_catalog_workspace_cache_path", lambda: cache_path)

    first = ipc_worker._catalog_workspace_dirs(route_root, refresh=True)
    assert first == [workspace.resolve()]
    assert cache_path.is_file()

    database.unlink()
    cached = ipc_worker._catalog_workspace_dirs(route_root)
    assert cached == first
    refreshed = ipc_worker._catalog_workspace_dirs(route_root, refresh=True)
    assert refreshed == []


def test_selectable_refresh_delta_applies_only_evidence_backed_green_and_red(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)
    _record_refresh_truth_fixture(fixture)
    deltas = list_telemetry_deltas(fixture["workspace"], fixture["brain_name"])

    assert deltas["status"] == "PASS"
    assert len(deltas["deltas"]) == 1
    selected = deltas["deltas"][0]
    assert selected["diff_authority"] == "PROJECT_REFRESH_BUTTON_ONLY"
    assert selected["delta_type"] == "GIT_WORKTREE_REFRESH_DELTA"
    assert selected["overlay_state"] == "REFRESH_DELTA_COMPLETE_OVERLAY_READY"
    assert selected["evidence_state"] == "BLOCKED"
    assert selected["before_snapshot_hash"] == fixture["first"]["snapshot_hash"]
    assert selected["after_snapshot_hash"] == fixture["second"]["snapshot_hash"]

    graph = query_telemetry_graph(
        fixture["workspace"],
        fixture["brain_name"],
        delta_id=selected["delta_id"],
    )
    route = next(node for node in graph["nodes"] if node["kind"] == "route" and node["label"] == "/api/items")
    symbol = next(node for node in graph["nodes"] if node["kind"] == "symbol" and node["label"] == "fetch_items")
    test_node = next(node for node in graph["nodes"] if node["kind"] == "test")
    assert route["tone"] == "GREEN"
    assert symbol["tone"] == "RED"
    assert test_node["tone"] == "NEUTRAL"
    assert route["overlay_evidence_direct"] is True
    assert symbol["overlay_evidence_direct"] is True
    assert test_node.get("overlay_evidence_direct") is not True
    colored_edges = [edge for edge in graph["edges"] if edge["tone"] in {"GREEN", "RED"}]
    assert all(edge.get("overlay_evidence_direct") is True for edge in colored_edges)
    assert {edge["relation_type"] for edge in colored_edges} == {"EXPOSES_ROUTE", "DECLARES_SYMBOL"}
    assert next(edge for edge in colored_edges if edge["relation_type"] == "EXPOSES_ROUTE")["tone"] == "GREEN"
    assert next(edge for edge in colored_edges if edge["relation_type"] == "DECLARES_SYMBOL")["tone"] == "RED"
    assert route["refresh_evidence"]["refresh_delta_id"] == selected["delta_id"]
    assert route["refresh_evidence"]["validation_results"][0]["status"] == "PASS"
    assert graph["selected_delta"]["delta_id"] == selected["delta_id"]
    assert graph["automatic_fusion"] is False


def test_folder_scope_replaces_the_graph_with_one_indexed_subtree(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)
    full = query_telemetry_graph(fixture["workspace"], fixture["brain_name"])
    folder = next(node for node in full["nodes"] if node["kind"] == "folder" and node["label"] == "src")

    scoped = query_telemetry_graph(
        fixture["workspace"],
        fixture["brain_name"],
        scope_id=folder["node_id"],
    )

    assert scoped["scope"] == {
        "scope_id": folder["node_id"],
        "mode": "REPLACED_WITH_INDEXED_SUBGRAPH",
    }
    assert folder["node_id"] in {node["node_id"] for node in scoped["nodes"]}
    assert not any(node["kind"] == "brain" for node in scoped["nodes"])
    assert all(edge["from_node_id"] in {node["node_id"] for node in scoped["nodes"]} for edge in scoped["edges"])
    assert all(edge["to_node_id"] in {node["node_id"] for node in scoped["nodes"]} for edge in scoped["edges"])


def test_open_target_requires_captured_boundary_and_indexed_object(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)
    source_file = fixture["source_file"]
    source_root = fixture["source_root"]
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    unindexed = source_root / "unindexed.txt"
    unindexed.write_text("unindexed", encoding="utf-8")

    validated = validate_telemetry_open_target(
        fixture["workspace"], fixture["brain_name"], source_file
    )
    assert validated["status"] == "PASS"
    assert validated["source_id"] == "source_code"
    assert validated["target_kind"] == "file"

    opened: list[Path] = []
    result = open_telemetry_target(
        fixture["workspace"],
        fixture["brain_name"],
        source_file,
        application="vscode",
        opener=lambda target: opened.append(target),
    )
    assert result["status"] == "OPENED"
    assert result["application"] == "vscode"
    assert opened == [source_file.resolve()]

    with pytest.raises(BrainTelemetryError, match="TELEMETRY_TARGET_OUTSIDE_CAPTURED_SOURCE_BOUNDARY"):
        validate_telemetry_open_target(fixture["workspace"], fixture["brain_name"], outside)
    with pytest.raises(BrainTelemetryError, match="TELEMETRY_TARGET_NOT_INDEXED"):
        validate_telemetry_open_target(fixture["workspace"], fixture["brain_name"], unindexed)


def test_adjacent_version_fallback_never_invents_pass_or_failure(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path, semantic=False)
    deltas = list_telemetry_deltas(fixture["workspace"], fixture["brain_name"])

    assert deltas["deltas"] == []
    assert deltas["overlay_authority"] == "PROJECT_REFRESH_BUTTON_ONLY"
    assert deltas["planning_delta_color_authority"] is False


def test_graph_fails_closed_when_current_brain_no_longer_matches_immutable_pointer(tmp_path: Path) -> None:
    fixture = _versioned_indexed_brain(tmp_path)
    sector = (
        fixture["brain_root"]
        / "project"
        / "sectors"
        / "local_code"
        / "local_code_sector_v001.sqlite"
    )
    with closing(sqlite3.connect(sector)) as connection:
        connection.execute("UPDATE code_file_snapshot SET language='unverified-mutation' WHERE file_id='file_items'")
        connection.commit()

    with pytest.raises(
        BrainTelemetryError,
        match="TELEMETRY_CURRENT_BRAIN_DIFFERS_FROM_IMMUTABLE_VERSION",
    ):
        query_telemetry_graph(fixture["workspace"], fixture["brain_name"])
    with pytest.raises(BrainTelemetryError, match="TELEMETRY_MAX_NODES_OUT_OF_RANGE"):
        query_telemetry_graph(fixture["workspace"], fixture["brain_name"], max_nodes=100_001)


def test_ipc_routes_are_read_only_except_validated_open_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(ipc_worker, "telemetry_contract", lambda: {"route": "contract"})
    monkeypatch.setattr(ipc_worker, "get_telemetry_snapshot", lambda *_: {"route": "snapshot"})
    monkeypatch.setattr(ipc_worker, "list_telemetry_deltas", lambda *_: {"route": "deltas"})
    monkeypatch.setattr(ipc_worker, "query_telemetry_graph", lambda *_, **kwargs: {"route": "graph", **kwargs})
    monkeypatch.setattr(ipc_worker, "open_telemetry_target", lambda *_, **kwargs: {"route": "open", **kwargs})
    payload = {"workspace_dir": str(tmp_path), "brain_name": "Telemetry Fixture"}

    assert ipc_worker.handle({"command": "brain.telemetry.contract", "payload": payload}) == {"route": "contract"}
    assert ipc_worker.handle({"command": "brain.telemetry.snapshot", "payload": payload}) == {"route": "snapshot"}
    assert ipc_worker.handle({"command": "brain.telemetry.deltas", "payload": payload}) == {"route": "deltas"}
    graph = ipc_worker.handle({
        "command": "brain.telemetry.graph",
        "payload": {**payload, "delta_id": "delta_fixture", "scope_id": "folder_fixture", "max_nodes": 200},
    })
    assert graph == {"route": "graph", "delta_id": "delta_fixture", "scope_id": "folder_fixture", "max_nodes": 200}
    opened = ipc_worker.handle({
        "command": "brain.telemetry.openTarget",
        "payload": {**payload, "target": str(tmp_path), "application": "vscode"},
    })
    assert opened == {"route": "open", "application": "vscode"}
    assert "brain.telemetry.openTarget" in ipc_worker._MUTATING_COMMANDS
    assert not ({
        "brain.telemetry.contract",
        "brain.telemetry.snapshot",
        "brain.telemetry.deltas",
        "brain.telemetry.graph",
    } & ipc_worker._MUTATING_COMMANDS)
