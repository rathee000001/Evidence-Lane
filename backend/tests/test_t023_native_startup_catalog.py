from __future__ import annotations

from pathlib import Path

from sqlite_brain_builder import ipc_worker


def test_brain_catalog_boot_restores_durable_snapshot_without_workspace_scan(
    monkeypatch, tmp_path: Path
) -> None:
    route_root = tmp_path / "route"
    workspace = route_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workspace.sqlite").write_bytes(b"catalog-identity")
    cache_path = tmp_path / "brain_catalog_workspace_cache.json"
    history = {
        "active_root": str(route_root),
        "roots": [
            {"path": str(route_root), "display_name": "Durable route", "source": "TEST", "active": True},
            {"path": str(tmp_path / "missing-registered-route"), "active": False},
        ],
    }
    monkeypatch.setattr(ipc_worker, "_catalog_workspace_cache_path", lambda: cache_path)
    monkeypatch.setattr(ipc_worker, "list_workspace_roots", lambda: history)
    ipc_worker._catalog_workspace_cache_write(
        {
            "roots": {
                ipc_worker._catalog_workspace_cache_key(route_root): {
                    "root_path": str(route_root),
                    "workspaces": [str(workspace)],
                }
            }
        }
    )
    reads: list[Path] = []

    def first_read(path: Path) -> list[dict[str, object]]:
        reads.append(path)
        return [{"brain_id": "brain-durable", "brain_name": "Durable Brain", "updated_at": "2026-07-18T00:00:00Z"}]

    monkeypatch.setattr(ipc_worker, "_list_brains", first_read)
    first = ipc_worker._brain_catalog()
    assert first["catalog_restore_mode"] == "CACHE_MISS_REBUILT_ONCE"
    assert reads == [workspace.resolve()]

    def forbidden_workspace_read(_path: Path) -> list[dict[str, object]]:
        raise AssertionError("ordinary boot must not reread persisted workspace catalogs")

    monkeypatch.setattr(ipc_worker, "_list_brains", forbidden_workspace_read)
    restored = ipc_worker._brain_catalog()
    assert restored["catalog_restore_mode"] == "DURABLE_FULL_BRAIN_CATALOG_SNAPSHOT"
    assert restored["raw_legacy_route_rescan"] is False
    assert restored["brain_count"] == 1
    assert restored["isolated_errors"][0]["error"] == "LEGACY_ROOT_NOT_FOUND"
