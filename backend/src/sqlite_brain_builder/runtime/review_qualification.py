from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


REVIEW_QUALIFICATION_SCHEMA = "EVIDENCE_LANE_REVIEW_QUALIFICATION_V001"
REVIEW_DECISION_SCHEMA = "EVIDENCE_LANE_REVIEW_DECISION_V001"
ACCEPTED_BY_CONTINUATION_SCHEMA = "EVIDENCE_LANE_ACCEPTED_BY_CONTINUATION_V001"
HIL_DECISIONS = {"APPROVE", "REJECT", "SUPERSEDE"}
PROHIBITED_CONTINUATION_CATEGORIES = {
    "CREDENTIALS",
    "DELETION",
    "DEPLOYMENT",
    "DISCLOSURE",
    "IRREVERSIBLE",
    "IRREVERSIBLE_FUSE",
    "MONEY",
    "SECURITY",
}
_PROHIBITED_CATEGORY_TOKENS = {
    "CREDENTIAL",
    "DELETE",
    "DELETION",
    "DEPLOY",
    "DEPLOYMENT",
    "DISCLOSURE",
    "FUSE",
    "IRREVERSIBLE",
    "MONEY",
    "PAYMENT",
    "PUBLISH",
    "PURCHASE",
    "SECURITY",
    "UPLOAD",
}


class ReviewQualificationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReviewQualificationError(f"REVIEW_QUALIFICATION_FIELD_REQUIRED:{field}")
    return text


def _list_of_mappings(value: Sequence[Mapping[str, Any]] | None, field: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in value or []:
        if not isinstance(item, Mapping):
            raise ReviewQualificationError(f"REVIEW_QUALIFICATION_MAPPING_REQUIRED:{field}")
        rows.append(dict(item))
    return rows


def _content_hash(payload: Mapping[str, Any]) -> str:
    content = dict(payload)
    content.pop("content_sha256", None)
    return _sha256_bytes(_canonical_bytes(content))


def verify_review_qualification_packet(packet_path: str | Path) -> dict[str, Any]:
    path = Path(packet_path).expanduser().resolve()
    if not path.is_file():
        raise ReviewQualificationError("REVIEW_QUALIFICATION_PACKET_MISSING")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewQualificationError("REVIEW_QUALIFICATION_PACKET_UNREADABLE") from exc
    if not isinstance(payload, dict) or payload.get("schema") != REVIEW_QUALIFICATION_SCHEMA:
        raise ReviewQualificationError("REVIEW_QUALIFICATION_PACKET_SCHEMA_MISMATCH")
    if str(payload.get("content_sha256") or "").upper() != _content_hash(payload):
        raise ReviewQualificationError("REVIEW_QUALIFICATION_PACKET_CONTENT_HASH_MISMATCH")
    return {
        **payload,
        "status": "PASS_REVIEW_PACKET_VERIFIED",
        "packet_path": str(path),
        "packet_sha256": _sha256_file(path),
    }


def create_review_qualification_packet(
    packet_path: str | Path,
    *,
    project_id: str,
    brain_id: str,
    accepted_snapshot_id: str,
    accepted_snapshot_hash: str,
    candidate_delta_id: str,
    candidate_snapshot_hash: str,
    task_id: str,
    run_id: str,
    queried_evidence: Sequence[Mapping[str, Any]] | None,
    changed_files: Sequence[Mapping[str, Any]] | None,
    changed_symbols: Sequence[Mapping[str, Any]] | None,
    changed_routes: Sequence[Mapping[str, Any]] | None,
    tests: Sequence[Mapping[str, Any]] | None,
    untested_paths: Sequence[Mapping[str, Any]] | None,
    risk: Sequence[Mapping[str, Any]] | None,
    rollback: Mapping[str, Any],
    reviewer_type: str,
    next_pointer: str,
    prior_packet_sha256: str = "",
) -> dict[str, Any]:
    path = Path(packet_path).expanduser().resolve()
    if path.exists():
        verified = verify_review_qualification_packet(path)
        if str(verified.get("candidate_delta_id") or "") != str(candidate_delta_id):
            raise ReviewQualificationError("REVIEW_QUALIFICATION_PACKET_CANDIDATE_MISMATCH")
        return {**verified, "reused": True}
    rollback_record = dict(rollback)
    if not rollback_record.get("version_id") or not rollback_record.get("snapshot_hash"):
        raise ReviewQualificationError("REVIEW_QUALIFICATION_ROLLBACK_IDENTITY_REQUIRED")
    payload: dict[str, Any] = {
        "schema": REVIEW_QUALIFICATION_SCHEMA,
        "packet_id": "RQPKT_" + _sha256_bytes(f"{candidate_delta_id}|{candidate_snapshot_hash}".encode("utf-8"))[:24],
        "project_id": _required_text(project_id, "project_id"),
        "brain_id": _required_text(brain_id, "brain_id"),
        "accepted_snapshot_id": _required_text(accepted_snapshot_id, "accepted_snapshot_id"),
        "accepted_snapshot_hash": _required_text(accepted_snapshot_hash, "accepted_snapshot_hash"),
        "candidate_delta_id": _required_text(candidate_delta_id, "candidate_delta_id"),
        "candidate_snapshot_hash": _required_text(candidate_snapshot_hash, "candidate_snapshot_hash"),
        "task_id": _required_text(task_id, "task_id"),
        "run_id": _required_text(run_id, "run_id"),
        "queried_evidence": _list_of_mappings(queried_evidence, "queried_evidence"),
        "changed_files": _list_of_mappings(changed_files, "changed_files"),
        "changed_symbols": _list_of_mappings(changed_symbols, "changed_symbols"),
        "changed_routes": _list_of_mappings(changed_routes, "changed_routes"),
        "tests": _list_of_mappings(tests, "tests"),
        "untested_paths": _list_of_mappings(untested_paths, "untested_paths"),
        "risk": _list_of_mappings(risk, "risk"),
        "rollback": rollback_record,
        "reviewer_type": _required_text(reviewer_type, "reviewer_type"),
        "decision": "PENDING",
        "hil_state": "AWAITING_HUMAN_APPROVE_REJECT_OR_SUPERSEDE",
        "next_pointer": _required_text(next_pointer, "next_pointer"),
        "prior_packet_sha256": str(prior_packet_sha256 or "").upper(),
        "created_at": _utc_now(),
    }
    payload["content_sha256"] = _content_hash(payload)
    _write_json_atomic(path, payload)
    return {
        **payload,
        "status": "PENDING_HIL",
        "packet_path": str(path),
        "packet_sha256": _sha256_file(path),
        "reused": False,
    }


def verify_review_decision_receipt(
    receipt_path: str | Path,
    *,
    expected_packet_sha256: str | None = None,
) -> dict[str, Any]:
    path = Path(receipt_path).expanduser().resolve()
    if not path.is_file():
        raise ReviewQualificationError("REVIEW_DECISION_RECEIPT_MISSING")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewQualificationError("REVIEW_DECISION_RECEIPT_UNREADABLE") from exc
    if not isinstance(payload, dict) or payload.get("schema") != REVIEW_DECISION_SCHEMA:
        raise ReviewQualificationError("REVIEW_DECISION_RECEIPT_SCHEMA_MISMATCH")
    if str(payload.get("decision") or "") not in HIL_DECISIONS:
        raise ReviewQualificationError("REVIEW_DECISION_INVALID")
    if str(payload.get("content_sha256") or "").upper() != _content_hash(payload):
        raise ReviewQualificationError("REVIEW_DECISION_CONTENT_HASH_MISMATCH")
    if expected_packet_sha256 and str(payload.get("review_packet_sha256") or "").upper() != str(expected_packet_sha256).upper():
        raise ReviewQualificationError("REVIEW_DECISION_PACKET_HASH_MISMATCH")
    return {
        **payload,
        "status": "PASS_REVIEW_DECISION_VERIFIED",
        "receipt_path": str(path),
        "receipt_sha256": _sha256_file(path),
    }


def record_review_decision(
    packet_path: str | Path,
    receipt_path: str | Path,
    *,
    decision: str,
    actor: str,
    reviewer_type: str,
    reason: str,
) -> dict[str, Any]:
    packet = verify_review_qualification_packet(packet_path)
    normalized_decision = str(decision or "").strip().upper()
    if normalized_decision not in HIL_DECISIONS:
        raise ReviewQualificationError("REVIEW_DECISION_MUST_BE_APPROVE_REJECT_OR_SUPERSEDE")
    path = Path(receipt_path).expanduser().resolve()
    if path.exists():
        existing = verify_review_decision_receipt(path, expected_packet_sha256=packet["packet_sha256"])
        if str(existing.get("decision")) != normalized_decision:
            raise ReviewQualificationError("REVIEW_DECISION_ALREADY_RECORDED_DIFFERENTLY")
        return {**existing, "reused": True}
    payload: dict[str, Any] = {
        "schema": REVIEW_DECISION_SCHEMA,
        "decision_id": "RQDEC_" + _sha256_bytes(f"{packet['packet_id']}|{normalized_decision}|{actor}".encode("utf-8"))[:24],
        "review_packet_id": packet["packet_id"],
        "review_packet_sha256": packet["packet_sha256"],
        "candidate_delta_id": packet["candidate_delta_id"],
        "candidate_snapshot_hash": packet["candidate_snapshot_hash"],
        "decision": normalized_decision,
        "actor": _required_text(actor, "actor"),
        "reviewer_type": _required_text(reviewer_type, "reviewer_type"),
        "reason": _required_text(reason, "reason"),
        "automatic_fuse": False,
        "decided_at": _utc_now(),
        "next_pointer": "EXPLICIT_FUSE" if normalized_decision == "APPROVE" else "PRESERVE_ACCEPTED_STATE",
    }
    payload["content_sha256"] = _content_hash(payload)
    _write_json_atomic(path, payload)
    return {
        **payload,
        "status": f"HIL_{normalized_decision}_RECORDED",
        "receipt_path": str(path),
        "receipt_sha256": _sha256_file(path),
        "reused": False,
    }


def record_accepted_by_continuation(
    receipt_path: str | Path,
    *,
    task_id: str,
    run_id: str,
    actor: str,
    trigger_steer: str,
    scope_summary: str,
    next_pointer: str,
    action_categories: Sequence[str],
    completed: bool,
    tested: bool,
    reversible: bool,
    bounded_scope: bool,
    review_packet_sha256: str = "",
    prior_receipt_sha256: str = "",
) -> dict[str, Any]:
    checks = {
        "completed": completed,
        "tested": tested,
        "reversible": reversible,
        "bounded_scope": bounded_scope,
    }
    failed = sorted(name for name, passed in checks.items() if passed is not True)
    if failed:
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_SCOPE_CHECK_FAILED:" + ",".join(failed))
    normalized_categories = {
        re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")
        for value in action_categories
        if str(value or "").strip()
    }
    prohibited = sorted(
        category
        for category in normalized_categories
        if (
            category in PROHIBITED_CONTINUATION_CATEGORIES
            or any(token in set(category.split("_")) for token in _PROHIBITED_CATEGORY_TOKENS)
        )
    )
    if prohibited:
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_PROHIBITED:" + ",".join(prohibited))
    path = Path(receipt_path).expanduser().resolve()
    if path.exists():
        existing = verify_accepted_by_continuation_receipt(path)
        expected_identity = {
            "task_id": _required_text(task_id, "task_id"),
            "run_id": _required_text(run_id, "run_id"),
            "actor": _required_text(actor, "actor"),
            "trigger_steer": _required_text(trigger_steer, "trigger_steer"),
            "scope_summary": _required_text(scope_summary, "scope_summary"),
            "next_pointer": _required_text(next_pointer, "next_pointer"),
        }
        if any(str(existing.get(field) or "") != value for field, value in expected_identity.items()):
            raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_IDENTITY_MISMATCH")
        if existing.get("scope_checks") != checks or set(existing.get("action_categories") or []) != normalized_categories:
            raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_SCOPE_MISMATCH")
        return {**existing, "receipt_path": str(path), "receipt_sha256": _sha256_file(path), "reused": True}
    payload: dict[str, Any] = {
        "schema": ACCEPTED_BY_CONTINUATION_SCHEMA,
        "status": "ACCEPTED_BY_CONTINUATION",
        "task_id": _required_text(task_id, "task_id"),
        "run_id": _required_text(run_id, "run_id"),
        "actor": _required_text(actor, "actor"),
        "trigger_steer": _required_text(trigger_steer, "trigger_steer"),
        "scope_summary": _required_text(scope_summary, "scope_summary"),
        "scope_checks": checks,
        "action_categories": sorted(normalized_categories),
        "review_packet_sha256": str(review_packet_sha256 or "").upper(),
        "prior_receipt_sha256": str(prior_receipt_sha256 or "").upper(),
        "next_pointer": _required_text(next_pointer, "next_pointer"),
        "does_not_authorize": sorted(PROHIBITED_CONTINUATION_CATEGORIES),
        "automatic_fuse": False,
        "recorded_at": _utc_now(),
    }
    payload["content_sha256"] = _content_hash(payload)
    _write_json_atomic(path, payload)
    return {
        **payload,
        "receipt_path": str(path),
        "receipt_sha256": _sha256_file(path),
        "reused": False,
    }


def verify_accepted_by_continuation_receipt(receipt_path: str | Path) -> dict[str, Any]:
    path = Path(receipt_path).expanduser().resolve()
    if not path.is_file():
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_MISSING")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_UNREADABLE") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ACCEPTED_BY_CONTINUATION_SCHEMA:
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_SCHEMA_MISMATCH")
    if str(payload.get("content_sha256") or "").upper() != _content_hash(payload):
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_CONTENT_HASH_MISMATCH")
    if payload.get("status") != "ACCEPTED_BY_CONTINUATION" or payload.get("automatic_fuse") is not False:
        raise ReviewQualificationError("ACCEPTED_BY_CONTINUATION_RECEIPT_CONTRACT_MISMATCH")
    return {
        **payload,
        "receipt_path": str(path),
        "receipt_sha256": _sha256_file(path),
    }


__all__ = [
    "ACCEPTED_BY_CONTINUATION_SCHEMA",
    "HIL_DECISIONS",
    "PROHIBITED_CONTINUATION_CATEGORIES",
    "REVIEW_DECISION_SCHEMA",
    "REVIEW_QUALIFICATION_SCHEMA",
    "ReviewQualificationError",
    "create_review_qualification_packet",
    "record_accepted_by_continuation",
    "record_review_decision",
    "verify_accepted_by_continuation_receipt",
    "verify_review_decision_receipt",
    "verify_review_qualification_packet",
]
