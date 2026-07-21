from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from send2trash import send2trash

from sqlite_brain_builder.runtime.path_policy import (
    brain_output_dir,
    normalize_workspace_dir,
    workspace_db_path,
)


class BrainRecycleError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return root.is_symlink()


def recycle_registered_brain(
    workspace_dir: str | Path,
    brain_name: str,
    *,
    confirm_brain_name: str,
    trash: Callable[[str], None] = send2trash,
) -> dict[str, Any]:
    workspace = normalize_workspace_dir(workspace_dir).resolve()
    if str(confirm_brain_name or "").strip() != str(brain_name or "").strip():
        raise BrainRecycleError("BRAIN_DELETE_EXPLICIT_NAME_CONFIRMATION_REQUIRED")
    database = workspace_db_path(workspace)
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT brain_id,output_dir,status FROM brain_project WHERE brain_name=? ORDER BY updated_at DESC LIMIT 1",
            (brain_name,),
        ).fetchone()
        if not row:
            raise BrainRecycleError("BRAIN_NOT_REGISTERED")
        brain_id, registered_output, status = row
        expected = brain_output_dir(workspace, brain_name).resolve()
        target = Path(str(registered_output)).resolve()
        if target != expected:
            raise BrainRecycleError("BRAIN_REGISTERED_OUTPUT_MISMATCH")
        if target.parent != workspace or not _is_within(target, workspace):
            raise BrainRecycleError("BRAIN_DELETE_PATH_OUTSIDE_WORKSPACE")
        if _has_symlink_component(target, workspace):
            raise BrainRecycleError("BRAIN_DELETE_SYMLINK_OR_JUNCTION_BLOCKED")
        if not target.is_dir():
            raise BrainRecycleError("BRAIN_OUTPUT_FOLDER_MISSING")
        next_row = connection.execute(
            "SELECT brain_name,output_dir FROM brain_project WHERE brain_id<>? AND status='ACTIVE' ORDER BY updated_at DESC LIMIT 1",
            (brain_id,),
        ).fetchone()

        # Registry mutation is intentionally after the OS recycle operation.
        try:
            trash(str(target))
        except Exception as exc:
            raise BrainRecycleError(f"BRAIN_RECYCLE_FAILED:{type(exc).__name__}:{exc}") from exc
        if target.exists():
            raise BrainRecycleError("BRAIN_RECYCLE_DID_NOT_REMOVE_SOURCE_PATH")

        deleted_at = _utc_now()
        connection.execute(
            "UPDATE brain_project SET status='RECYCLED',updated_at=? WHERE brain_id=?",
            (deleted_at, brain_id),
        )
        connection.commit()
    finally:
        connection.close()

    receipt = {
        "status": "PASS",
        "action": "DELETE_BRAIN_TO_RECYCLE_BIN",
        "brain_id": brain_id,
        "brain_name": brain_name,
        "canonical_folder": str(target),
        "workspace_root": str(workspace),
        "deleted_at": deleted_at,
        "registry_status": "RECYCLED",
        "next_brain_name": next_row[0] if next_row else None,
        "next_brain_output": next_row[1] if next_row else None,
    }
    canonical = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    receipt_path = workspace / "receipts" / f"BRAIN_RECYCLE_{brain_id}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt["receipt_path"] = str(receipt_path)
    return receipt


__all__ = ["BrainRecycleError", "recycle_registered_brain"]
