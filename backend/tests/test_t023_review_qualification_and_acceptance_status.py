from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqlite_brain_builder import ipc_worker
from sqlite_brain_builder.brain_versions import capture_brain_version, current_brain_snapshot
from sqlite_brain_builder.refresh_brain import (
    RefreshBrainError,
    fuse_refresh_candidate,
    record_refresh_hil_decision,
    start_refresh_brain,
)
from sqlite_brain_builder.runtime.review_qualification import (
    ReviewQualificationError,
    create_review_qualification_packet,
    record_accepted_by_continuation,
    record_review_decision,
    verify_accepted_by_continuation_receipt,
    verify_review_decision_receipt,
    verify_review_qualification_packet,
)
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain


def _review_packet(path: Path) -> dict[str, object]:
    return create_review_qualification_packet(
        path,
        project_id="project:review-fixture",
        brain_id="brain:review-fixture",
        accepted_snapshot_id="version-baseline",
        accepted_snapshot_hash="A" * 64,
        candidate_delta_id="REFRESH_BRAIN_DELTA_candidate-001",
        candidate_snapshot_hash="B" * 64,
        task_id="task-review-001",
        run_id="run-review-001",
        queried_evidence=[{"source_id": "source-1", "classification": "CHANGED_REBUILD"}],
        changed_files=[{"path": "fixture.py", "change_kind": "MODIFIED"}],
        changed_symbols=[{"symbol": "run"}],
        changed_routes=[{"route": "brain.refresh.start"}],
        tests=[{"name": "focused", "status": "PASS"}],
        untested_paths=[{"path": "native_hil", "status": "PENDING"}],
        risk=[{"risk": "human_acceptance_pending", "status": "OPEN"}],
        rollback={"version_id": "version-baseline", "snapshot_hash": "A" * 64},
        reviewer_type="HUMAN_OWNER_OR_AUTHORIZED_REVIEWER",
        next_pointer="HIL:APPROVE|REJECT|SUPERSEDE",
    )


def test_review_packet_and_decision_are_hash_bound_and_tamper_evident(tmp_path: Path) -> None:
    packet_path = tmp_path / "REVIEW_QUALIFICATION_candidate-001.json"
    packet = _review_packet(packet_path)
    verified = verify_review_qualification_packet(packet_path)
    assert verified["status"] == "PASS_REVIEW_PACKET_VERIFIED"
    assert verified["packet_sha256"] == packet["packet_sha256"]
    assert verified["decision"] == "PENDING"

    decision_path = tmp_path / "REVIEW_DECISION_candidate-001.json"
    decision = record_review_decision(
        packet_path,
        decision_path,
        decision="APPROVE",
        actor="fixture human",
        reviewer_type="TEST_HUMAN_REVIEWER",
        reason="the bounded candidate passed focused validation",
    )
    verified_decision = verify_review_decision_receipt(
        decision_path,
        expected_packet_sha256=str(packet["packet_sha256"]),
    )
    assert verified_decision["decision"] == "APPROVE"
    assert verified_decision["review_packet_sha256"] == packet["packet_sha256"]
    assert decision["automatic_fuse"] is False

    tampered = json.loads(decision_path.read_text(encoding="utf-8"))
    tampered["actor"] = "tampered actor"
    decision_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ReviewQualificationError, match="REVIEW_DECISION_CONTENT_HASH_MISMATCH"):
        verify_review_decision_receipt(decision_path)


def test_accepted_by_continuation_is_bounded_hash_linked_and_idempotent(tmp_path: Path) -> None:
    receipt_path = tmp_path / "ACCEPTED_BY_CONTINUATION_fixture.json"
    recorded = record_accepted_by_continuation(
        receipt_path,
        task_id="task-001",
        run_id="run-001",
        actor="fixture human",
        trigger_steer="continue the completed focused verification",
        scope_summary="focused reversible report generation",
        next_pointer="NEXT_FOCUSED_GATE",
        action_categories=["REPORT", "FOCUSED_TEST"],
        completed=True,
        tested=True,
        reversible=True,
        bounded_scope=True,
        review_packet_sha256="C" * 64,
        prior_receipt_sha256="D" * 64,
    )
    verified = verify_accepted_by_continuation_receipt(receipt_path)
    assert verified["status"] == "ACCEPTED_BY_CONTINUATION"
    assert verified["automatic_fuse"] is False
    assert verified["prior_receipt_sha256"] == "D" * 64
    assert "IRREVERSIBLE_FUSE" in verified["does_not_authorize"]

    repeated = record_accepted_by_continuation(
        receipt_path,
        task_id="task-001",
        run_id="run-001",
        actor="fixture human",
        trigger_steer="continue the completed focused verification",
        scope_summary="focused reversible report generation",
        next_pointer="NEXT_FOCUSED_GATE",
        action_categories=["FOCUSED_TEST", "REPORT"],
        completed=True,
        tested=True,
        reversible=True,
        bounded_scope=True,
        review_packet_sha256="C" * 64,
        prior_receipt_sha256="D" * 64,
    )
    assert repeated["reused"] is True
    assert repeated["content_sha256"] == recorded["content_sha256"]


@pytest.mark.parametrize("failed_gate", ["completed", "tested", "reversible", "bounded_scope"])
def test_accepted_by_continuation_rejects_each_missing_scope_gate(
    tmp_path: Path,
    failed_gate: str,
) -> None:
    checks = {"completed": True, "tested": True, "reversible": True, "bounded_scope": True}
    checks[failed_gate] = False
    with pytest.raises(ReviewQualificationError, match="ACCEPTED_BY_CONTINUATION_SCOPE_CHECK_FAILED"):
        record_accepted_by_continuation(
            tmp_path / f"missing-{failed_gate}.json",
            task_id="task-001",
            run_id="run-001",
            actor="fixture human",
            trigger_steer="bounded continuation",
            scope_summary="focused reversible work",
            next_pointer="NEXT_FOCUSED_GATE",
            action_categories=["REPORT"],
            **checks,
        )


@pytest.mark.parametrize(
    "category",
    [
        "provider upload",
        "production deployment",
        "credential rotation",
        "security change",
        "data deletion",
        "money payment",
        "irreversible Fuse",
    ],
)
def test_accepted_by_continuation_rejects_prohibited_actions(tmp_path: Path, category: str) -> None:
    with pytest.raises(ReviewQualificationError, match="ACCEPTED_BY_CONTINUATION_PROHIBITED"):
        record_accepted_by_continuation(
            tmp_path / (category.replace(" ", "-") + ".json"),
            task_id="task-001",
            run_id="run-001",
            actor="fixture human",
            trigger_steer="bounded continuation",
            scope_summary="must remain reversible",
            next_pointer="NEXT_FOCUSED_GATE",
            action_categories=[category],
            completed=True,
            tested=True,
            reversible=True,
            bounded_scope=True,
        )


def test_refresh_candidate_requires_hash_bound_human_approval_before_fuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_brain_builder.refresh_brain as refresh_brain

    brain_name = "Review Gate Fixture"
    source_path = tmp_path / "fixture.txt"
    source_path.write_text("baseline\n", encoding="utf-8")
    source = {
        "source_id": "source-review-fixture",
        "lane_key": "artifacts",
        "lane_label": "Artifacts",
        "source_type": "Artifact",
        "display_name": source_path.name,
        "path": str(source_path),
        "text": "",
        "active": True,
        "status": "registered",
        "metadata": {},
    }
    build_brain(str(tmp_path), brain_name, [source], generate_mmd=False)
    version = capture_brain_version(
        tmp_path,
        brain_name,
        actor_type="app",
        actor_name="fixture builder",
        reason="review gate baseline",
    )
    accepted_before = current_brain_snapshot(tmp_path, brain_name)
    source_path.write_text("candidate change\n", encoding="utf-8")

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
            "topology": {"status": "SKIPPED_FOCUSED_FIXTURE"},
            "packages": {"status": "SKIPPED_FOCUSED_FIXTURE"},
            "validations": {"status": "PASS", "mode": "FOCUSED_REVIEW_GATE_FIXTURE"},
        }

    monkeypatch.setattr(refresh_brain, "_create_candidate_products", focused_products)
    candidate = start_refresh_brain(
        tmp_path,
        brain_name,
        brain_id="brain-review-gate-fixture",
        expected_snapshot_id=str(version["snapshot_hash"]),
        sources=[source],
    )
    assert candidate["status"] == "AWAITING_FUSE"
    assert candidate["hil_state"] == "AWAITING_HUMAN_APPROVE_REJECT_OR_SUPERSEDE"
    packet = candidate["review_qualification"]
    assert Path(candidate["candidate_root"], packet["packet_relative_path"]).is_file()

    with pytest.raises(RefreshBrainError, match="REFRESH_FUSE_HIL_APPROVAL_REQUIRED"):
        fuse_refresh_candidate(
            tmp_path,
            brain_name,
            candidate_id=str(candidate["candidate_id"]),
            actor="fixture human",
            reason="must not bypass HIL",
        )
    assert current_brain_snapshot(tmp_path, brain_name) == accepted_before

    approved = record_refresh_hil_decision(
        tmp_path,
        brain_name,
        candidate_id=str(candidate["candidate_id"]),
        decision="APPROVE",
        actor="fixture human",
        reviewer_type="TEST_HUMAN_REVIEWER",
        reason="validated bounded candidate is approved for an explicit Fuse click",
    )
    assert approved["status"] == "AWAITING_FUSE"
    assert approved["hil_decision"]["decision"] == "APPROVE"
    assert approved["automatic_fusion"] is False
    assert current_brain_snapshot(tmp_path, brain_name) == accepted_before
    assert {"brain.refresh.hil.decide", "brain.review.acceptByContinuation"} <= ipc_worker._MUTATING_COMMANDS
    assert {"brain.refresh.hil.decide", "brain.review.acceptByContinuation"} <= ipc_worker._PROCESS_TRUTH_COMMANDS
