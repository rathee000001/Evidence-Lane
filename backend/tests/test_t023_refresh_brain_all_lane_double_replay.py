from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest

from sqlite_brain_builder.brain_versions import (
    capture_brain_version,
    current_brain_snapshot,
    list_brain_versions,
)
from sqlite_brain_builder.refresh_brain import (
    RefreshBrainError,
    cancel_refresh_brain,
    classify_registered_sources,
    drop_refresh_candidate,
    fuse_refresh_candidate,
    get_refresh_status,
    record_refresh_hil_decision,
    retry_refresh_brain,
    start_refresh_brain,
)
from sqlite_brain_builder.runtime.canonical_lanes import CANONICAL_LANE_IDS
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain
from test_t021_all_lane_env15_stress import _fixtures


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, _sha256(path))
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def _sector_table_counts(brain_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    sector_root = brain_root / "project" / "sectors"
    for database in sorted(sector_root.rglob("*.sqlite")):
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        try:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in tables:
                counts[f"{database.relative_to(brain_root).as_posix()}::{table}"] = int(
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
        finally:
            connection.close()
    return counts


def _source(lane: str, source_id: str, path: Path) -> dict[str, object]:
    return {
        "source_id": source_id,
        "lane_key": lane,
        "display_name": path.name,
        "path": str(path),
        "assistant_response": "visible fixture response" if lane == "chat_lineage" else "",
        "schema_contract": "custom_item\ncustom_evidence\ncustom_next_action" if lane == "custom" else "",
        "active": True,
    }


def _all_lane_sources(tmp_path: Path) -> tuple[list[dict[str, object]], dict[str, Path]]:
    fixtures = _fixtures(tmp_path)
    no_git = tmp_path / "fixtures" / "local_code_no_git"
    shutil.copytree(fixtures["local_code"], no_git, ignore=shutil.ignore_patterns(".git"))
    (no_git / "app.py").rename(no_git / "local_no_git_app.py")
    fixtures_with_scenario = {**fixtures, "local_code_no_git": no_git}
    sources = [
        _source(
            "local_code" if scenario == "local_code_no_git" else scenario,
            f"source_{scenario}",
            path,
        )
        for scenario, path in fixtures_with_scenario.items()
    ]
    assert len(CANONICAL_LANE_IDS) == 18
    assert len(sources) == 19
    assert {str(source["lane_key"]) for source in sources} == set(CANONICAL_LANE_IDS)
    assert (fixtures["local_code"] / ".git").is_dir() or shutil.which("git") is None
    assert not (no_git / ".git").exists()
    return sources, fixtures_with_scenario


def _receipt_payload(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    digest_value = str(payload.pop("receipt_sha256"))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == digest_value
    payload["receipt_sha256"] = digest_value
    return payload


@pytest.mark.parametrize("primary_lane", ["local_code", "github_code"])
def test_real_refresh_runs_twice_across_every_permitted_lane_without_growth(
    tmp_path: Path,
    primary_lane: str,
) -> None:
    brain_name = f"T023 {primary_lane} Lane Replay"
    all_sources, all_paths = _all_lane_sources(tmp_path)
    excluded_primary_lane = "github_code" if primary_lane == "local_code" else "local_code"
    sources = [
        source
        for source in all_sources
        if str(source["lane_key"]) != excluded_primary_lane
    ]
    included_paths = {Path(str(source["path"])).resolve() for source in sources}
    paths = {
        scenario: path
        for scenario, path in all_paths.items()
        if path.resolve() in included_paths
    }
    source_count = len(sources)
    active_lane_ids = {str(source["lane_key"]) for source in sources}
    assert primary_lane in active_lane_ids
    assert excluded_primary_lane not in active_lane_ids
    assert len(active_lane_ids) == len(CANONICAL_LANE_IDS) - 1
    build = build_brain(str(tmp_path), brain_name, sources, generate_mmd=False)
    brain_root = Path(str(build["brain_root"]))
    version = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="app",
        actor_name="T023 all-lane replay",
        reason="verified all-lane replay baseline",
    )
    baseline_snapshot = current_brain_snapshot(tmp_path, brain_name)
    baseline_sector_counts = _sector_table_counts(brain_root)
    baseline_packages = _tree_fingerprint(brain_root / "packages")
    baseline_cache = _sha256(brain_root / "project" / "runtime" / "source_fingerprint_cache.json")
    receipt_root = brain_root / "receipts" / "refresh_brain"

    first = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id=f"t023-{primary_lane}-lane-replay",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=sources,
    )
    counts_after_first = _sector_table_counts(brain_root)
    packages_after_first = _tree_fingerprint(brain_root / "packages")
    receipts_after_first = sorted(receipt_root.glob("REFRESH_OPERATION_*.json"))

    second = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id=f"t023-{primary_lane}-lane-replay",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=sources,
    )
    receipts_after_second = sorted(receipt_root.glob("REFRESH_OPERATION_*.json"))

    for result in (first, second):
        assert result["status"] == "NO_CHANGE"
        assert result["candidate_created"] is False
        assert result["new_immutable_snapshot"] == 0
        assert result["unchanged_sources_skipped"] == source_count
        assert result["source_inventory"]["totals"]["UNCHANGED_REUSE"] == source_count
        assert result["source_inventory"]["totals"]["CHANGED_REBUILD"] == 0
        assert result["source_inventory"]["totals"]["NEW_REGISTER"] == 0
        assert result["source_inventory"]["totals"]["REMOVED_TOMBSTONE"] == 0
        assert len(result["source_inventory"]["lane_counts"]) == len(active_lane_ids)
        assert Path(str(result["operation_receipt"])).is_file()
        assert result["operation_receipt_sha256"]
        classified_ids = {str(item["source_id"]) for item in result["source_inventory"]["classifications"]}
        assert classified_ids == {str(source["source_id"]) for source in sources}
        for item in result["source_inventory"]["classifications"]:
            assert item["current_cached_sha256"] == item["indexed_sha256"]
            assert Path(str(item["path"])).resolve() in {path.resolve() for path in paths.values()}

    assert len(receipts_after_first) == 1
    assert len(receipts_after_second) == 2
    first_receipt = _receipt_payload(Path(str(first["operation_receipt"])))
    second_receipt = _receipt_payload(Path(str(second["operation_receipt"])))
    assert first_receipt["prior_receipt_sha256"] is None
    assert second_receipt["prior_receipt_sha256"] == first_receipt["receipt_sha256"]
    assert first_receipt["lane_count"] == second_receipt["lane_count"] == len(active_lane_ids)
    assert first_receipt["source_scenario_count"] == second_receipt["source_scenario_count"] == source_count
    assert first_receipt["status"] == second_receipt["status"] == "NO_CHANGE"

    assert _sector_table_counts(brain_root) == counts_after_first == baseline_sector_counts
    assert _tree_fingerprint(brain_root / "packages") == packages_after_first == baseline_packages
    assert _sha256(brain_root / "project" / "runtime" / "source_fingerprint_cache.json") == baseline_cache
    assert current_brain_snapshot(tmp_path, brain_name) == baseline_snapshot
    assert list_brain_versions(tmp_path, brain_name, verify_hashes=True)["version_count"] == 1
    assert not (tmp_path / ".evidenceos_refresh_candidates").exists()

    unchanged = classify_registered_sources(brain_root, sources)
    assert unchanged["totals"]["UNCHANGED_REUSE"] == source_count

    for path in sorted({path.resolve() for path in paths.values()}):
        if path.is_dir():
            (path / ".t023_refresh_change").write_text("changed\n", encoding="utf-8")
        else:
            with path.open("ab") as stream:
                stream.write(b"\nt023-refresh-change\n")
    changed = classify_registered_sources(brain_root, sources)
    assert changed["totals"]["CHANGED_REBUILD"] == source_count

    added_sources = [
        {**source, "source_id": f"added_{source['source_id']}"}
        for source in sources
    ]
    added = classify_registered_sources(brain_root, [*sources, *added_sources])
    assert {
        item["source_id"] for item in added["classifications"] if str(item["source_id"]).startswith("added_")
    } == {source["source_id"] for source in added_sources}
    assert all(
        item["classification"] == "NEW_REGISTER"
        for item in added["classifications"]
        if str(item["source_id"]).startswith("added_")
    )

    removed = classify_registered_sources(brain_root, [{**source, "active": False} for source in sources])
    assert removed["totals"]["REMOVED_TOMBSTONE"] == source_count

    unsupported = classify_registered_sources(
        brain_root,
        [*sources, {"source_id": "unsupported", "lane_key": "not-a-real-lane", "text": "x"}],
    )
    unsupported_row = next(item for item in unsupported["classifications"] if item["source_id"] == "unsupported")
    assert unsupported_row == {
        "source_id": "unsupported",
        "lane_id": "not-a-real-lane",
        "classification": "UNSUPPORTED",
        "reason": "UNKNOWN_LANE_ALIAS",
    }

    blocked_sources = [
        {"source_id": f"blocked_{lane}", "lane_key": lane, "path": "", "text": "", "active": True}
        for lane in sorted(active_lane_ids)
    ]
    blocked = classify_registered_sources(brain_root, [*sources, *blocked_sources])
    blocked_rows = [
        item for item in blocked["classifications"] if str(item["source_id"]).startswith("blocked_")
    ]
    assert len(blocked_rows) == len(active_lane_ids)
    assert all(item["classification"] == "BLOCKED" for item in blocked_rows)


def test_interruption_retry_restart_restore_and_failed_fuse_preserve_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    brain_name = "T023 Replay Recovery"
    source_path = tmp_path / "artifact.txt"
    source_path.write_text("baseline\n", encoding="utf-8")
    source = _source("artifacts", "source_recovery", source_path)
    build_brain(str(tmp_path), brain_name, [source], generate_mmd=False)
    version = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="app",
        actor_name="T023 replay recovery",
        reason="recovery baseline",
    )
    baseline = current_brain_snapshot(tmp_path, brain_name)
    source_path.write_text("changed for interrupted refresh\n", encoding="utf-8")

    def recycle_fixture(path: str | Path) -> None:
        target = Path(path)
        if not target.exists():
            return
        for item in target.rglob("*"):
            if item.is_file():
                os.chmod(item, stat.S_IREAD | stat.S_IWRITE)
        shutil.rmtree(target)

    monkeypatch.setattr(refresh_brain, "_send_to_recycle_bin", recycle_fixture)

    def cancellable_products(
        _candidate_workspace: Path,
        _selected_brain_name: str,
        _candidate_sources: list[dict[str, object]],
        **kwargs: object,
    ) -> dict[str, object]:
        progress = kwargs["progress"]
        assert callable(progress)
        progress({"stage": "fixture-cancel", "stage_percent": 10})
        progress({"stage": "fixture-after-cancel", "stage_percent": 20})
        raise AssertionError("cancel should stop before product completion")

    def request_cancel(event: dict[str, object]) -> None:
        if event.get("stage") != "fixture-cancel":
            return
        state = get_refresh_status(tmp_path, brain_name)
        cancel_refresh_brain(
            tmp_path,
            brain_name,
            candidate_id=str(state["candidate_id"]),
            actor="fixture user",
            reason="interrupt replay fixture",
        )

    monkeypatch.setattr(refresh_brain, "_create_candidate_products", cancellable_products)
    interrupted = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="t023-replay-recovery",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
        progress=request_cancel,
    )
    assert interrupted["status"] == "DROPPED"
    assert interrupted["exact_error"] == "REFRESH_CANCEL_REQUESTED"
    assert interrupted["verified_brain_unchanged"] is True
    assert Path(str(interrupted["operation_receipt"])).is_file()

    restored_after_restart = get_refresh_status(
        tmp_path,
        brain_name,
        candidate_id=str(interrupted["candidate_id"]),
    )
    assert restored_after_restart["status"] == "DROPPED"
    assert restored_after_restart["candidate_id"] == interrupted["candidate_id"]

    def focused_products(
        candidate_workspace: Path,
        selected_brain_name: str,
        candidate_sources: list[dict[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return {
            "build": build_brain(
                str(candidate_workspace),
                selected_brain_name,
                candidate_sources,
                generate_mmd=False,
            ),
            "topology": {"status": "SKIPPED_FOCUSED_RECOVERY_FIXTURE"},
            "packages": {"status": "SKIPPED_FOCUSED_RECOVERY_FIXTURE"},
            "validations": {"status": "PASS", "mode": "FOCUSED_RECOVERY_FIXTURE"},
        }

    monkeypatch.setattr(refresh_brain, "_create_candidate_products", focused_products)
    retried = retry_refresh_brain(
        tmp_path,
        brain_name,
        candidate_id=str(interrupted["candidate_id"]),
        brain_id="t023-replay-recovery",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )
    assert retried["status"] == "AWAITING_FUSE"
    assert retried["retry_of"] == interrupted["candidate_id"]
    assert retried["verified_brain_unchanged"] is True
    record_refresh_hil_decision(
        tmp_path,
        brain_name,
        candidate_id=str(retried["candidate_id"]),
        decision="APPROVE",
        actor="fixture user",
        reviewer_type="TEST_HUMAN_REVIEWER",
        reason="approve the validated recovery candidate before explicit Fuse",
    )

    def fail_version_capture(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("forced post-swap version failure")

    monkeypatch.setattr(refresh_brain, "capture_brain_version", fail_version_capture)
    with pytest.raises(RuntimeError, match="forced post-swap version failure"):
        fuse_refresh_candidate(
            tmp_path,
            brain_name,
            candidate_id=str(retried["candidate_id"]),
            actor="fixture user",
            reason="exercise rollback preservation",
        )

    failed_fuse = get_refresh_status(tmp_path, brain_name, candidate_id=str(retried["candidate_id"]))
    assert failed_fuse["status"] == "FAILED"
    assert failed_fuse["rollback_available"] is True
    assert failed_fuse["verified_brain_unchanged"] is True
    assert current_brain_snapshot(tmp_path, brain_name) == baseline
    assert list_brain_versions(tmp_path, brain_name, verify_hashes=True)["version_count"] == 1
    assert Path(str(failed_fuse["candidate_root"])).is_dir()

    dropped = drop_refresh_candidate(
        tmp_path,
        brain_name,
        candidate_id=str(retried["candidate_id"]),
        actor="fixture user",
        reason="clean failed-fuse candidate",
    )
    assert dropped["status"] == "DROPPED"
    assert dropped["verified_brain_unchanged"] is True
