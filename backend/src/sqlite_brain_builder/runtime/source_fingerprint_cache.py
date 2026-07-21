from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable


CACHE_SCHEMA_VERSION = 1
CODE_SKIP_DIRS = {
    ".git", "node_modules", ".next", "dist", "build", "__pycache__",
    ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
_LOCK = threading.RLock()


def _cache_path(brain_root: str | Path) -> Path:
    return Path(brain_root).resolve() / "project" / "runtime" / "source_fingerprint_cache.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(payload.get("entries"), dict):
        return {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}}
    return payload


def _entry_key(lane_id: str, source_id: str) -> str:
    return f"{lane_id}\x1f{source_id}"


def _stat_material(path: Path, *, skip_dirs: Iterable[str] = ()) -> tuple[str, int, int]:
    ignored = {name.casefold() for name in skip_dirs}
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0

    def add(relative: str, kind: str, size: int, mtime_ns: int, ctime_ns: int, extra: str = "") -> None:
        nonlocal file_count, total_bytes
        digest.update(f"{relative}\0{kind}\0{size}\0{mtime_ns}\0{ctime_ns}\0{extra}\n".encode("utf-8", "surrogatepass"))
        if kind == "file":
            file_count += 1
            total_bytes += size

    if path.is_file():
        stat = path.stat()
        add(path.name, "file", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        return digest.hexdigest(), file_count, total_bytes
    if not path.is_dir():
        raise FileNotFoundError(path)

    def visit(folder: Path, relative: Path) -> None:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name.casefold())
        for entry in entries:
            rel = relative / entry.name
            rel_text = rel.as_posix()
            if entry.is_symlink():
                stat = entry.stat(follow_symlinks=False)
                add(rel_text, "symlink", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, os.readlink(entry.path))
            elif entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() in ignored:
                    continue
                stat = entry.stat(follow_symlinks=False)
                add(rel_text, "directory", 0, stat.st_mtime_ns, stat.st_ctime_ns)
                visit(Path(entry.path), rel)
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                add(rel_text, "file", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    visit(path, Path())
    # Code snapshots intentionally exclude the heavy .git object store, but
    # reverse-Git lineage must still invalidate when HEAD or refs move.
    git_dir = path / ".git"
    if ".git" in ignored and git_dir.is_dir():
        controls = [git_dir / "HEAD", git_dir / "packed-refs"]
        if (git_dir / "refs").is_dir():
            controls.extend(sorted((git_dir / "refs").rglob("*")))
        for control in controls:
            if not control.is_file():
                continue
            stat = control.stat()
            try:
                value = control.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                value = ""
            add(f".git-control/{control.relative_to(git_dir).as_posix()}", "git-control", stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, value)
    return digest.hexdigest(), file_count, total_bytes


def cached_content_hash(
    brain_root: str | Path,
    lane_id: str,
    source_id: str,
    source_path: str | Path,
    *,
    skip_dirs: Iterable[str] = (),
) -> tuple[str | None, dict[str, Any]]:
    path = Path(source_path).expanduser().resolve(strict=True)
    stat_hash, file_count, total_bytes = _stat_material(path, skip_dirs=skip_dirs)
    probe = {
        "source_path": str(path),
        "stat_fingerprint": stat_hash,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    with _LOCK:
        entry = _load(_cache_path(brain_root))["entries"].get(_entry_key(lane_id, source_id))
    if (
        isinstance(entry, dict)
        and entry.get("source_path") == probe["source_path"]
        and entry.get("stat_fingerprint") == stat_hash
        and isinstance(entry.get("content_sha256"), str)
    ):
        return entry["content_sha256"], probe
    return None, probe


def record_content_hash(
    brain_root: str | Path,
    lane_id: str,
    source_id: str,
    content_sha256: str,
    probe: dict[str, Any],
) -> None:
    cache_path = _cache_path(brain_root)
    with _LOCK:
        payload = _load(cache_path)
        payload["entries"][_entry_key(lane_id, source_id)] = {
            **probe,
            "lane_id": lane_id,
            "source_id": source_id,
            "content_sha256": content_sha256,
        }
        _atomic_write(cache_path, payload)


__all__ = ["CODE_SKIP_DIRS", "cached_content_hash", "record_content_hash"]
