from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


REFRESH_SOURCE_STATE_SCHEMA = "EVIDENCEOS_REFRESH_SOURCE_STATE_V1"
REFRESH_OVERLAY_TRUTH_SCHEMA = "EVIDENCEOS_REFRESH_OVERLAY_TRUTH_V1"
REFRESH_VALIDATION_RECEIPT_SCHEMA = "EVIDENCEOS_REFRESH_VALIDATION_RECEIPT_V1"
_SHA256 = set("0123456789abcdef")
_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".evidenceos_runtime",
        ".evidenceos_refresh_candidates",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "coverage",
    }
)


class RefreshDeltaTruthError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").removeprefix("./")


def _command_text(command: Sequence[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def _run_hidden(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float = 120.0,
    cancel_check: Callable[[], bool] | None = None,
    max_output_bytes: int = 5 * 1024 * 1024,
) -> dict[str, Any]:
    started_at = _utc_now()
    started = time.monotonic()
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(part) for part in command],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        creationflags=creationflags,
    )
    timed_out = False
    cancelled = False
    stdout_bytes = b""
    stderr_bytes = b""
    while True:
        if cancel_check and cancel_check():
            cancelled = True
            process.terminate()
            break
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            timed_out = True
            process.terminate()
            break
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=min(0.1, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        if process.poll() is None or cancelled or timed_out:
            stdout_bytes, stderr_bytes = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_bytes, stderr_bytes = process.communicate()
    stdout_bytes = stdout_bytes or b""
    stderr_bytes = stderr_bytes or b""
    output_truncated = len(stdout_bytes) > max_output_bytes or len(stderr_bytes) > max_output_bytes
    stdout = stdout_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    exit_code = process.returncode
    return {
        "command": _command_text(command),
        "argv": [str(part) for part in command],
        "cwd": str(cwd),
        "pid": int(process.pid),
        "hidden_window": True,
        "cancellable": True,
        "started_at": started_at,
        "ended_at": _utc_now(),
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "exit_code": exit_code,
        "status": "CANCELLED" if cancelled else "TIMEOUT" if timed_out else "PASS" if exit_code == 0 else "FAIL",
        "stdout": stdout,
        "stderr": stderr,
        "stdout_sha256": _sha256_bytes(stdout_bytes),
        "stderr_sha256": _sha256_bytes(stderr_bytes),
        "output_truncated": output_truncated,
    }


def _sector_databases(brain_root: Path) -> list[tuple[str, Path]]:
    router = brain_root / "project" / "project_router.sqlite"
    if not router.is_file():
        return []
    with closing(sqlite3.connect(f"file:{router.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sector_registry" not in tables:
            return []
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sector_registry)")}
        path_column = "sqlite_path" if "sqlite_path" in columns else "database_path" if "database_path" in columns else ""
        if not path_column:
            return []
        rows = connection.execute(f"SELECT sector_id,{path_column} AS database_path FROM sector_registry").fetchall()
    result: list[tuple[str, Path]] = []
    for row in rows:
        value = Path(str(row["database_path"]))
        database = value if value.is_absolute() else brain_root / value
        if database.is_file():
            result.append((str(row["sector_id"]), database.resolve()))
    return result


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _read_code_index(brain_root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    files_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    files_by_id: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}
    relation_tables_seen: set[str] = set()
    databases: list[str] = []
    for sector_id, database in _sector_databases(brain_root):
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = _tables(connection)
            databases.append(str(database))
            if "code_source_registry" in tables:
                cols = _columns(connection, "code_source_registry")
                root_col = "source_root" if "source_root" in cols else "canonical_path" if "canonical_path" in cols else ""
                if root_col:
                    for row in connection.execute(f"SELECT source_id,{root_col} AS source_root,* FROM code_source_registry"):
                        sources[str(row["source_id"])] = {**dict(row), "sector_id": sector_id}
            if "source_registry" in tables:
                cols = _columns(connection, "source_registry")
                root_col = next((name for name in ("canonical_path", "source_locator", "source_root") if name in cols), "")
                if root_col:
                    for row in connection.execute(f"SELECT source_id,{root_col} AS source_root,* FROM source_registry"):
                        sources.setdefault(str(row["source_id"]), {**dict(row), "sector_id": sector_id})
            if "code_file_snapshot" in tables:
                cols = _columns(connection, "code_file_snapshot")
                required = {"file_id", "source_id", "relative_path"}
                if required <= cols:
                    hash_col = "sha256" if "sha256" in cols else "current_sha256" if "current_sha256" in cols else ""
                    if hash_col:
                        for row in connection.execute(
                            f"SELECT file_id,source_id,relative_path,{hash_col} AS file_sha256,* FROM code_file_snapshot ORDER BY rowid"
                        ):
                            path = _normalize_path(row["relative_path"])
                            item = {**dict(row), "relative_path": path, "sha256": str(row["file_sha256"] or ""), "sector_id": sector_id}
                            files[path.casefold()] = item
                            files_by_identity[(str(row["source_id"]), path.casefold())] = item
                            files_by_id[str(row["file_id"])] = item
            if "code_import_edge" in tables:
                relation_tables_seen.add("code_import_edge")
                cols = _columns(connection, "code_import_edge")
                if {"edge_id", "from_file_id", "from_path", "import_target"} <= cols:
                    for row in connection.execute("SELECT * FROM code_import_edge ORDER BY edge_id"):
                        from_path = _normalize_path(row["from_path"])
                        relation_type = str(row["import_type"] if "import_type" in cols else "IMPORTS")
                        target = _normalize_path(row["import_target"])
                        source_id = str(row["source_id"] if "source_id" in cols else files_by_id.get(str(row["from_file_id"]), {}).get("source_id") or "")
                        key = f"import|{source_id}|{from_path.casefold()}|{relation_type.casefold()}|{target.casefold()}"
                        relations[key] = {
                            "object_id": str(row["edge_id"]),
                            "relation_type": relation_type,
                            "from_object_id": f"file:{row['from_file_id']}",
                            "to_object_id": f"path:{target}",
                            "from_path": from_path,
                            "to_path": target,
                            "source_id": source_id,
                            "evidence": dict(row),
                            "table": "code_import_edge",
                        }
            if "code_workflow_edge" in tables:
                relation_tables_seen.add("code_workflow_edge")
                cols = _columns(connection, "code_workflow_edge")
                required = {"edge_id", "from_type", "from_id", "relation_type", "to_type", "to_id"}
                if required <= cols:
                    for row in connection.execute("SELECT * FROM code_workflow_edge ORDER BY edge_id"):
                        from_type = str(row["from_type"])
                        to_type = str(row["to_type"])
                        from_id = str(row["from_id"])
                        to_id = str(row["to_id"])
                        source_id = str(row["source_id"] if "source_id" in cols else files_by_id.get(from_id, {}).get("source_id") or "")
                        from_path = str(files_by_id.get(from_id, {}).get("relative_path") or "")
                        to_path = str(files_by_id.get(to_id, {}).get("relative_path") or "")
                        relation_type = str(row["relation_type"])
                        key = f"workflow|{source_id}|{from_type}|{from_id}|{relation_type}|{to_type}|{to_id}"
                        relations[key] = {
                            "object_id": str(row["edge_id"]),
                            "relation_type": relation_type,
                            "from_object_id": f"{from_type}:{from_id}",
                            "to_object_id": f"{to_type}:{to_id}",
                            "from_path": from_path,
                            "to_path": to_path,
                            "source_id": source_id,
                            "evidence": dict(row),
                            "table": "code_workflow_edge",
                        }
    return {
        "files": files,
        "files_by_identity": files_by_identity,
        "files_by_id": files_by_id,
        "sources": sources,
        "relations": relations,
        "relation_tables_seen": sorted(relation_tables_seen),
        "databases": sorted(databases),
    }


def _baseline_heads(brain_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for _sector_id, database in _sector_databases(brain_root):
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            tables = _tables(connection)
            if "code_source_active_head" in tables:
                for row in connection.execute("SELECT source_id,head_commit_sha FROM code_source_active_head"):
                    result[str(row["source_id"])] = str(row["head_commit_sha"] or "")
            if "code_source_registry" in tables:
                cols = _columns(connection, "code_source_registry")
                if {"source_id", "commit_head"} <= cols:
                    for row in connection.execute("SELECT source_id,commit_head FROM code_source_registry"):
                        result.setdefault(str(row["source_id"]), str(row["commit_head"] or ""))
    return result


def _parse_name_status_z(value: str) -> list[dict[str, Any]]:
    tokens = [token for token in value.split("\0") if token]
    result: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if "\t" in token:
            status, path = token.split("\t", 1)
        else:
            status = token
            if not status or status[:1] not in {"A", "C", "D", "M", "R", "T", "U", "X", "B"}:
                continue
            if index >= len(tokens):
                break
            path = tokens[index]
            index += 1
        code = status[:1]
        if code in {"R", "C"} and index < len(tokens):
            second = tokens[index]
            index += 1
            result.append({"status": status, "change_kind": "RENAMED" if code == "R" else "COPIED", "previous_path": _normalize_path(path), "path": _normalize_path(second)})
        else:
            change_kind = {"A": "ADDED", "D": "DELETED", "M": "MODIFIED", "T": "MODIFIED", "U": "CONTRADICTED"}.get(code, "MODIFIED")
            result.append({"status": status, "change_kind": change_kind, "path": _normalize_path(path)})
    return result


def _git_source_state(
    root: Path,
    *,
    source_id: str,
    lane_id: str,
    baseline_head: str,
    cancel_check: Callable[[], bool] | None,
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []

    def run(*args: str, timeout: float = 120.0) -> dict[str, Any]:
        receipt = _run_hidden(["git", "-C", str(root), *args], cwd=root, timeout_seconds=timeout, cancel_check=cancel_check)
        commands.append(receipt)
        return receipt

    repository = run("rev-parse", "--show-toplevel")
    if repository["status"] != "PASS":
        raise RefreshDeltaTruthError(f"REFRESH_GIT_ROOT_FAILED:{root}")
    branch = run("branch", "--show-current")
    head = run("rev-parse", "--verify", "HEAD")
    parent = run("rev-parse", "HEAD^")
    metadata = run("show", "-s", "--format=%H%x00%P%x00%an%x00%aI%x00%s", "HEAD")
    status = run("status", "--porcelain=v2", "--branch")
    unstaged = run("diff", "--name-status", "-M", "-z")
    staged = run("diff", "--cached", "--name-status", "-M", "-z")
    untracked = run("ls-files", "--others", "--exclude-standard", "-z")
    numstat = run("diff", "--numstat")
    staged_numstat = run("diff", "--cached", "--numstat")
    patch = run("diff", "--binary", timeout=180.0)
    staged_patch = run("diff", "--cached", "--binary", timeout=180.0)
    head_sha = str(head.get("stdout") or "").strip()
    unstaged_changes = _parse_name_status_z(str(unstaged.get("stdout") or ""))
    staged_changes = _parse_name_status_z(str(staged.get("stdout") or ""))
    untracked_files = [
        _normalize_path(item)
        for item in str(untracked.get("stdout") or "").split("\0")
        if item
    ]
    changed_files = [*unstaged_changes, *staged_changes]
    changed_files.extend(
        {"status": "??", "change_kind": "ADDED", "path": item}
        for item in untracked_files
    )
    worktree_dirty = bool(changed_files)
    commit_changes: list[dict[str, Any]] = []
    if not worktree_dirty and baseline_head and head_sha and baseline_head.casefold() != head_sha.casefold():
        commit_names = run("diff", "--name-status", "-M", "-z", baseline_head, head_sha)
        commit_numstat = run("diff", "--numstat", baseline_head, head_sha)
        commit_patch = run("diff", "--binary", baseline_head, head_sha, timeout=180.0)
        commit_changes = _parse_name_status_z(str(commit_names.get("stdout") or ""))
        changed_files.extend(commit_changes)
        delta_type = "GIT_COMMIT_REFRESH_DELTA"
        worktree_state = "CLEAN_COMMIT_ADVANCE"
        active_numstat = str(commit_numstat.get("stdout") or "")
        active_patch_sha256 = str(commit_patch.get("stdout_sha256") or "")
    else:
        delta_type = "GIT_WORKTREE_REFRESH_DELTA"
        worktree_state = "DIRTY" if worktree_dirty else "CLEAN_NO_COMMIT_ADVANCE"
        active_numstat = "\n".join(
            value for value in (str(numstat.get("stdout") or ""), str(staged_numstat.get("stdout") or "")) if value
        )
        active_patch_sha256 = _sha256_bytes(
            (str(patch.get("stdout_sha256") or "") + str(staged_patch.get("stdout_sha256") or "")).encode("utf-8")
        )
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for item in changed_files:
        key = (str(item.get("previous_path") or "").casefold(), str(item.get("path") or "").casefold())
        deduplicated[key] = {**item, "source_id": source_id, "lane_id": lane_id}
    metadata_tokens = str(metadata.get("stdout") or "").rstrip("\r\n\0").split("\0")
    commit_metadata = (
        {
            "commit_sha": metadata_tokens[0],
            "parent_commit_shas": metadata_tokens[1].split() if len(metadata_tokens) > 1 else [],
            "author_name": metadata_tokens[2] if len(metadata_tokens) > 2 else "",
            "authored_at": metadata_tokens[3] if len(metadata_tokens) > 3 else "",
            "subject": metadata_tokens[4] if len(metadata_tokens) > 4 else "",
        }
        if metadata.get("status") == "PASS" and metadata_tokens and metadata_tokens[0]
        else {}
    )
    optional_receipt_ids = {id(head), id(parent), id(metadata)}
    required_receipts = [item for item in commands if id(item) not in optional_receipt_ids]
    capture_complete = all(
        item.get("status") == "PASS" and not item.get("output_truncated")
        for item in required_receipts
    ) and (delta_type != "GIT_COMMIT_REFRESH_DELTA" or bool(commit_metadata))
    return {
        "source_id": source_id,
        "lane_id": lane_id,
        "source_root": str(root),
        "has_git": True,
        "delta_type": delta_type,
        "repository_root": str(repository.get("stdout") or "").strip(),
        "branch": str(branch.get("stdout") or "").strip(),
        "head_commit_sha": head_sha,
        "parent_commit_sha": str(parent.get("stdout") or "").strip() if parent.get("status") == "PASS" else "",
        "baseline_commit_sha": baseline_head,
        "worktree_state": worktree_state,
        "changed_files": list(deduplicated.values()),
        "unstaged_changes": unstaged_changes,
        "staged_changes": staged_changes,
        "untracked_files": untracked_files,
        "commit_changes": commit_changes,
        "file_diff_statistics": active_numstat,
        "active_patch_sha256": active_patch_sha256,
        "commit_metadata": commit_metadata,
        "command_receipts": commands,
        "capture_complete": capture_complete,
        "git_status_evidence": str(status.get("stdout") or ""),
        "unstaged_numstat": str(numstat.get("stdout") or ""),
        "staged_numstat": str(staged_numstat.get("stdout") or ""),
        "unstaged_patch_sha256": str(patch.get("stdout_sha256") or ""),
        "staged_patch_sha256": str(staged_patch.get("stdout_sha256") or ""),
    }


def _iter_source_files(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name not in _SKIP_DIRECTORIES)
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_file():
                yield path


def _hash_source_state(root: Path, *, source_id: str, lane_id: str, baseline_files: Mapping[Any, Mapping[str, Any]]) -> dict[str, Any]:
    current: dict[str, dict[str, Any]] = {}
    for path in _iter_source_files(root):
        relative = path.name if root.is_file() else path.relative_to(root).as_posix()
        current[relative.casefold()] = {"path": relative, "sha256": _sha256_file(path), "size_bytes": path.stat().st_size}
    previous = {
        _normalize_path(value.get("relative_path")).casefold(): value
        for value in baseline_files.values()
        if str(value.get("source_id") or "") == source_id
    }
    deleted = {
        key: value
        for key, value in previous.items()
        if key not in current
    }
    added = {
        key: value
        for key, value in current.items()
        if key not in previous
    }
    deleted_by_hash: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    added_by_hash: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for key, value in deleted.items():
        deleted_by_hash.setdefault(str(value.get("sha256") or ""), []).append((key, value))
    for key, value in added.items():
        added_by_hash.setdefault(str(value.get("sha256") or ""), []).append((key, value))
    renamed_old: set[str] = set()
    renamed_new: set[str] = set()
    changes: list[dict[str, Any]] = []
    for digest in sorted(set(deleted_by_hash) & set(added_by_hash)):
        old_matches = deleted_by_hash[digest]
        new_matches = added_by_hash[digest]
        if not digest or len(old_matches) != 1 or len(new_matches) != 1:
            continue
        old_key, old = old_matches[0]
        new_key, new = new_matches[0]
        renamed_old.add(old_key)
        renamed_new.add(new_key)
        changes.append(
            {
                "source_id": source_id,
                "lane_id": lane_id,
                "previous_path": old["relative_path"],
                "path": new["path"],
                "change_kind": "RENAMED",
                "previous_sha256": digest,
                "current_sha256": digest,
                "rename_identity": "CONTENT_HASH_EXACT",
            }
        )
    for key in sorted(set(previous) | set(current)):
        old = previous.get(key)
        new = current.get(key)
        if old and not new and key not in renamed_old:
            changes.append(
                {
                    "source_id": source_id,
                    "lane_id": lane_id,
                    "path": old["relative_path"],
                    "change_kind": "DELETED",
                    "previous_sha256": str(old.get("sha256") or ""),
                    "current_sha256": None,
                }
            )
        elif new and not old and key not in renamed_new:
            changes.append(
                {
                    "source_id": source_id,
                    "lane_id": lane_id,
                    "path": new["path"],
                    "change_kind": "ADDED",
                    "previous_sha256": None,
                    "current_sha256": str(new.get("sha256") or ""),
                }
            )
        elif old and new and str(old.get("sha256") or "") != str(new.get("sha256") or ""):
            changes.append(
                {
                    "source_id": source_id,
                    "lane_id": lane_id,
                    "path": new["path"],
                    "change_kind": "MODIFIED",
                    "previous_sha256": str(old.get("sha256") or ""),
                    "current_sha256": str(new.get("sha256") or ""),
                }
            )
    return {
        "source_id": source_id,
        "lane_id": lane_id,
        "source_root": str(root),
        "has_git": False,
        "delta_type": "HASH_SNAPSHOT_REFRESH_DELTA",
        "worktree_state": "CONTENT_HASH_SNAPSHOT",
        "changed_files": changes,
        "file_count": len(current),
        "snapshot_sha256": _sha256_bytes(_canonical_bytes(current)),
        "capture_complete": True,
        "command_receipts": [],
    }


def capture_refresh_source_state(
    brain_root: str | Path,
    *,
    candidate_id: str,
    source_classifications: Sequence[Mapping[str, Any]],
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    indexed = _read_code_index(root)
    baseline_heads = _baseline_heads(root)
    source_states: list[dict[str, Any]] = []
    for item in sorted(source_classifications, key=lambda row: (str(row.get("lane_id") or ""), str(row.get("source_id") or ""))):
        path_value = item.get("path") or item.get("canonical_path") or item.get("source_locator")
        if not path_value or str(path_value).startswith("inline://"):
            continue
        source_root = Path(str(path_value)).expanduser().resolve()
        if not source_root.exists():
            continue
        source_id = str(item.get("source_id") or "")
        lane_id = str(item.get("lane_id") or "")
        has_git = source_root.is_dir() and (source_root / ".git").exists()
        state = (
            _git_source_state(
                source_root,
                source_id=source_id,
                lane_id=lane_id,
                baseline_head=str(baseline_heads.get(source_id) or ""),
                cancel_check=cancel_check,
            )
            if has_git
            else _hash_source_state(
                source_root,
                source_id=source_id,
                lane_id=lane_id,
                baseline_files=indexed["files_by_identity"],
            )
        )
        source_states.append(state)
    changed_files = [dict(change) for state in source_states for change in state.get("changed_files") or []]
    if any(state.get("delta_type") == "GIT_WORKTREE_REFRESH_DELTA" for state in source_states):
        delta_type = "GIT_WORKTREE_REFRESH_DELTA"
    elif any(state.get("delta_type") == "GIT_COMMIT_REFRESH_DELTA" for state in source_states):
        delta_type = "GIT_COMMIT_REFRESH_DELTA"
    else:
        delta_type = "HASH_SNAPSHOT_REFRESH_DELTA"
    git_states = [state for state in source_states if state.get("has_git")]
    payload: dict[str, Any] = {
        "schema": REFRESH_SOURCE_STATE_SCHEMA,
        "candidate_id": candidate_id,
        "captured_at": _utc_now(),
        "source_state": "CHANGE_DETECTED" if changed_files else "REFRESH_NO_CHANGE",
        "delta_type": delta_type,
        "sources": source_states,
        "changed_files": changed_files,
        "capture_complete": bool(source_states) and all(bool(state.get("capture_complete")) for state in source_states),
        "background_worker_pid": os.getpid(),
        "process_scope": "REFRESH_BUTTON_BACKGROUND_WORKER",
        "hidden_window": True,
        "cancellable": True,
        "hidden_process_pids": sorted(
            {os.getpid()}
            | {
                int(receipt["pid"])
                for state in source_states
                for receipt in state.get("command_receipts") or []
                if receipt.get("pid") is not None
            }
        ),
        "git": git_states[0] if len(git_states) == 1 else {"sources": git_states},
    }
    receipt_path = root / "receipts" / "refresh_brain" / f"REFRESH_SOURCE_STATE_{candidate_id}.json"
    receipt_payload = dict(payload)
    receipt_sha256 = _sha256_bytes(_canonical_bytes(receipt_payload))
    receipt_payload["receipt_sha256"] = receipt_sha256
    _write_json_atomic(receipt_path, receipt_payload)
    payload["receipt_path"] = str(receipt_path)
    payload["receipt_sha256"] = receipt_sha256
    return payload


def _validation_command(path: Path) -> tuple[str, list[str]] | None:
    suffix = path.suffix.casefold()
    if suffix in {".js", ".mjs", ".cjs"}:
        return "JAVASCRIPT_SYNTAX", ["node", "--check", str(path)]
    if suffix == ".py":
        return "PYTHON_SYNTAX", [sys.executable, "-c", "import pathlib,sys;compile(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'),sys.argv[1],'exec')", str(path)]
    if suffix == ".json":
        return "JSON_SCHEMA_SYNTAX", [sys.executable, "-c", "import json,sys;json.load(open(sys.argv[1],encoding='utf-8'))", str(path)]
    if suffix == ".toml":
        return "TOML_SYNTAX", [sys.executable, "-c", "import sys,tomllib;tomllib.load(open(sys.argv[1],'rb'))", str(path)]
    return None


def _truth_for_change(change_kind: str, validations: Sequence[Mapping[str, Any]]) -> tuple[str, bool]:
    if change_kind in {"DELETED", "REMOVED", "BROKEN", "CONTRADICTED", "REJECTED"}:
        return "RED", False
    statuses = {str(item.get("status") or "") for item in validations}
    if "FAIL" in statuses or "BLOCKED" in statuses:
        return "RED", False
    if statuses and statuses <= {"PASS"}:
        return "GREEN", False
    return "NEUTRAL", True


def _source_root_map(source_state: Mapping[str, Any]) -> dict[str, Path]:
    return {
        str(item.get("source_id") or ""): Path(str(item.get("source_root"))).resolve()
        for item in source_state.get("sources") or []
        if item.get("source_id") and item.get("source_root")
    }


def build_refresh_overlay_truth(
    canonical_root: str | Path,
    candidate_root: str | Path,
    *,
    candidate_id: str,
    source_state: Mapping[str, Any],
    products: Mapping[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    canonical = Path(canonical_root).resolve()
    candidate = Path(candidate_root).resolve()
    before = _read_code_index(canonical)
    after = _read_code_index(candidate)
    roots = _source_root_map(source_state)
    changes: dict[tuple[str, str], dict[str, Any]] = {}
    for item in source_state.get("changed_files") or []:
        key = (str(item.get("source_id") or ""), _normalize_path(item.get("path")).casefold())
        changes[key] = dict(item)
    for identity in sorted(set(before["files_by_identity"]) | set(after["files_by_identity"])):
        old = before["files_by_identity"].get(identity)
        new = after["files_by_identity"].get(identity)
        source_id, key = identity
        if old and not new:
            changes.setdefault(identity, {"source_id": source_id, "lane_id": "code", "path": old["relative_path"], "change_kind": "DELETED"})
        elif new and not old:
            changes.setdefault(identity, {"source_id": source_id, "lane_id": "code", "path": new["relative_path"], "change_kind": "ADDED"})
        elif old and new and str(old.get("sha256") or "") != str(new.get("sha256") or ""):
            changes.setdefault(identity, {"source_id": source_id, "lane_id": "code", "path": new["relative_path"], "change_kind": "MODIFIED"})

    validation_rows: list[dict[str, Any]] = []
    validations_by_path: dict[str, list[dict[str, Any]]] = {}
    for ordinal, change in enumerate(sorted(changes.values(), key=lambda item: (_normalize_path(item.get("path")).casefold(), str(item.get("source_id") or ""))), start=1):
        path = _normalize_path(change.get("path"))
        change_kind = str(change.get("change_kind") or "MODIFIED")
        source_root = roots.get(str(change.get("source_id") or ""))
        target = source_root / path if source_root and source_root.is_dir() else source_root
        validation_id = f"validation_{ordinal:04d}_{_sha256_bytes(path.encode('utf-8'))[:12]}"
        if change_kind == "DELETED":
            row = {
                "validation_id": validation_id,
                "validation_type": "GOVERNED_DELETION_IDENTITY",
                "command": "refresh:verify-deletion-identity",
                "started_at": _utc_now(),
                "ended_at": _utc_now(),
                "exit_code": 0,
                "status": "PASS",
                "affected_files": [path],
                "stdout": "Deletion identity is bound by Refresh source-state evidence.",
                "stderr": "",
            }
        else:
            command = _validation_command(target) if target and target.is_file() else None
            if command:
                validation_type, argv = command
                receipt = _run_hidden(argv, cwd=source_root or canonical, timeout_seconds=120.0, cancel_check=cancel_check)
                row = {
                    "validation_id": validation_id,
                    "validation_type": validation_type,
                    "command": receipt["command"],
                    "started_at": receipt["started_at"],
                    "ended_at": receipt["ended_at"],
                    "exit_code": receipt["exit_code"],
                    "status": "PASS" if receipt["status"] == "PASS" else "FAIL",
                    "affected_files": [path],
                    "stdout": receipt["stdout"],
                    "stderr": receipt["stderr"],
                    "pid": receipt["pid"],
                    "hidden_window": True,
                }
            else:
                row = {
                    "validation_id": validation_id,
                    "validation_type": "NO_CONFIGURED_BOUNDED_VALIDATION_ROUTE",
                    "command": "",
                    "started_at": _utc_now(),
                    "ended_at": _utc_now(),
                    "exit_code": None,
                    "status": "REVIEW_REQUIRED",
                    "affected_files": [path],
                    "stdout": "",
                    "stderr": "No governed validation command is configured for this file type.",
                }
        validation_rows.append(row)
        validations_by_path.setdefault(path.casefold(), []).append(row)

    product_validations = (products or {}).get("validations") if isinstance((products or {}).get("validations"), Mapping) else {}
    global_validation_rows: list[dict[str, Any]] = []
    if product_validations:
        global_validation_rows.append(
            {
                "validation_id": "candidate_product_validation",
                "validation_type": "CANDIDATE_BRAIN_AND_PACKAGE_VALIDATION",
                "command": "internal:refresh._create_candidate_products.validations",
                "started_at": _utc_now(),
                "ended_at": _utc_now(),
                "exit_code": 0 if str(product_validations.get("status") or "") == "PASS" else 1,
                "status": str(product_validations.get("status") or "REVIEW_REQUIRED"),
                "affected_files": [],
                "stdout": json.dumps(product_validations, ensure_ascii=False, sort_keys=True, default=str),
                "stderr": "",
            }
        )
        validation_rows.extend(global_validation_rows)

    receipt_path = canonical / "receipts" / "refresh_brain" / f"REFRESH_DELTA_VALIDATION_{candidate_id}.json"
    receipt_body = {
        "schema": REFRESH_VALIDATION_RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "validation_results": validation_rows,
        "recorded_at": _utc_now(),
    }
    receipt_sha256 = _sha256_bytes(_canonical_bytes(receipt_body))
    _write_json_atomic(receipt_path, {**receipt_body, "receipt_sha256": receipt_sha256})
    for row in validation_rows:
        row["receipt_path"] = str(receipt_path)
        row["receipt_sha256"] = receipt_sha256

    node_rows: list[dict[str, Any]] = []
    changed_paths: set[tuple[str, str]] = set()
    for change in sorted(changes.values(), key=lambda item: (_normalize_path(item.get("path")).casefold(), str(item.get("source_id") or ""))):
        path = _normalize_path(change.get("path"))
        previous_path = _normalize_path(change.get("previous_path")) if change.get("previous_path") else None
        source_id = str(change.get("source_id") or "")
        old = before["files_by_identity"].get((source_id, (previous_path or path).casefold()))
        new = after["files_by_identity"].get((source_id, path.casefold()))
        validations = [*validations_by_path.get(path.casefold(), []), *global_validation_rows]
        truth_state, review_required = _truth_for_change(str(change.get("change_kind") or "MODIFIED"), validations)
        object_id = f"file:{(old or new or {}).get('file_id')}" if (old or new) else "file:refresh:" + _sha256_bytes(path.encode("utf-8"))[:20]
        validation_ids = [str(item["validation_id"]) for item in validations]
        changed_paths.add((source_id, path.casefold()))
        if previous_path:
            changed_paths.add((source_id, previous_path.casefold()))
        node_rows.append(
            {
                "object_id": object_id,
                "object_kind": "file",
                "relative_path": path,
                "previous_path": previous_path,
                "change_kind": str(change.get("change_kind") or "MODIFIED"),
                "truth_state": truth_state,
                "review_required": review_required,
                "previous_sha256": str((old or {}).get("sha256") or "") or None,
                "current_sha256": str((new or {}).get("sha256") or "") or (str(_sha256_file((roots.get(str(change.get('source_id') or '')) / path))) if roots.get(str(change.get("source_id") or "")) and (roots[str(change.get("source_id") or "")] / path).is_file() else None),
                "impact_scope": "DIRECT",
                "validation_ids": validation_ids,
                "evidence": {
                    "source_id": str(change.get("source_id") or ""),
                    "lane_id": str(change.get("lane_id") or ""),
                    "git_status": change.get("status"),
                    "direct_or_transitive": "DIRECT",
                    "source_state_receipt": source_state.get("receipt_path"),
                },
            }
        )

    edge_rows: list[dict[str, Any]] = []
    relation_keys = sorted(set(before["relations"]) | set(after["relations"]))
    node_by_path = {
        (str((row.get("evidence") or {}).get("source_id") or ""), str(row["relative_path"]).casefold()): row
        for row in node_rows
    }
    for key in relation_keys:
        old = before["relations"].get(key)
        new = after["relations"].get(key)
        relation = new or old or {}
        source_id = str(relation.get("source_id") or "")
        from_path = str(relation.get("from_path") or "").casefold()
        to_path = str(relation.get("to_path") or "").casefold()
        impacted = (source_id, from_path) in changed_paths or (source_id, to_path) in changed_paths
        if old and new and not impacted:
            continue
        if old and not new:
            change_kind, truth_state, review_required = "REMOVED", "RED", False
        elif new and not old:
            owner = node_by_path.get((source_id, from_path)) or node_by_path.get((source_id, to_path))
            if owner and owner["truth_state"] == "GREEN":
                change_kind, truth_state, review_required = "ADDED", "GREEN", False
            elif owner and owner["truth_state"] == "RED":
                change_kind, truth_state, review_required = "ADDED", "RED", False
            else:
                change_kind, truth_state, review_required = "ADDED", "NEUTRAL", True
        else:
            change_kind, truth_state, review_required = "UNCHANGED_IMPACTED", "NEUTRAL", False
        owner = node_by_path.get((source_id, from_path)) or node_by_path.get((source_id, to_path))
        edge_rows.append(
            {
                "object_id": str(relation.get("object_id") or "edge:refresh:" + _sha256_bytes(key.encode("utf-8"))[:20]),
                "relation_type": str(relation.get("relation_type") or "UNKNOWN"),
                "from_object_id": str(relation.get("from_object_id") or ""),
                "to_object_id": str(relation.get("to_object_id") or ""),
                "change_kind": change_kind,
                "truth_state": truth_state,
                "review_required": review_required,
                "previous_state": old or {},
                "current_state": new or {},
                "impact_scope": "PROVEN_DIRECT",
                "validation_ids": list(owner.get("validation_ids") or []) if owner else [],
                "evidence": {
                    "indexed_relation_table": str(relation.get("table") or ""),
                    "direct_or_transitive": "PROVEN_DIRECT",
                    "source_state_receipt": source_state.get("receipt_path"),
                },
            }
        )

    relationship_comparison_complete = bool(before["relation_tables_seen"]) and (
        before["relation_tables_seen"] == after["relation_tables_seen"]
    )
    return {
        "schema": REFRESH_OVERLAY_TRUTH_SCHEMA,
        "candidate_id": candidate_id,
        "delta_type": str(source_state.get("delta_type") or "HASH_SNAPSHOT_REFRESH_DELTA"),
        "changed_files": [
            {
                "path": row["relative_path"],
                "previous_path": row.get("previous_path"),
                "change_kind": row["change_kind"],
                "previous_sha256": row.get("previous_sha256"),
                "current_sha256": row.get("current_sha256"),
            }
            for row in node_rows
        ],
        "node_changes": node_rows,
        "edge_changes": edge_rows,
        "validation_results": validation_rows,
        "relationship_comparison_complete": relationship_comparison_complete,
        "relation_tables_before": before["relation_tables_seen"],
        "relation_tables_after": after["relation_tables_seen"],
        "validation_receipt_path": str(receipt_path),
        "validation_receipt_sha256": receipt_sha256,
        "raw_source_reread_reason": "AUTHORIZED_REFRESH_ROUTE_ONLY",
        "ordinary_scene_navigation_source_reads": 0,
    }


__all__ = [
    "REFRESH_OVERLAY_TRUTH_SCHEMA",
    "REFRESH_SOURCE_STATE_SCHEMA",
    "REFRESH_VALIDATION_RECEIPT_SCHEMA",
    "RefreshDeltaTruthError",
    "build_refresh_overlay_truth",
    "capture_refresh_source_state",
]
