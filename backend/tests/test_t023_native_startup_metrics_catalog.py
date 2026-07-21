from __future__ import annotations

import json
from pathlib import Path

from sqlite_brain_builder import ipc_worker


def _repository_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (
            (parent / "evidence-os-clean-full-app").is_dir()
            and (parent / "evidence-os-clean-frontend-theme").is_dir()
        ):
            return parent
    raise RuntimeError("T023_REPOSITORY_ROOT_NOT_FOUND")


ROOT = _repository_root()


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
            {
                "path": str(route_root),
                "display_name": "Durable route",
                "source": "TEST",
                "active": True,
            },
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
        return [
            {
                "brain_id": "brain-durable",
                "brain_name": "Durable Brain",
                "updated_at": "2026-07-18T00:00:00Z",
            }
        ]

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
    assert restored["brains"][0]["brain_name"] == "Durable Brain"
    assert restored["isolated_errors"][0]["error"] == "LEGACY_ROOT_NOT_FOUND"


def test_native_window_shows_before_background_process_tree_metrics_prewarm() -> None:
    rust_paths = (
        ROOT / "evidence-os-clean-full-app" / "frontend" / "src-tauri" / "src" / "main.rs",
        ROOT / "evidence-os-clean-frontend-theme" / "src-tauri" / "src" / "main.rs",
    )
    config_paths = (
        ROOT / "evidence-os-clean-full-app" / "frontend" / "src-tauri" / "tauri.conf.json",
        ROOT / "evidence-os-clean-frontend-theme" / "src-tauri" / "tauri.conf.json",
    )
    cockpit_paths = (
        ROOT
        / "evidence-os-clean-full-app"
        / "frontend"
        / "theme-contract"
        / "src"
        / "components"
        / "EvidenceOSCockpitSimulation.tsx",
        ROOT
        / "evidence-os-clean-frontend-theme"
        / "theme-contract"
        / "src"
        / "components"
        / "EvidenceOSCockpitSimulation.tsx",
    )

    for path in rust_paths:
        source = path.read_text(encoding="utf-8")
        assert "struct MetricsWorker(PersistentWorker);" in source
        assert '.manage(MetricsWorker::default())' in source
        assert "EMBEDDED_WORKER_PATH_CACHE" in source
        assert ".get_or_init(extract_and_verify_embedded_worker_path)" in source
        assert '"metrics.snapshot" | "process.metrics.snapshot" | "processes.metrics.snapshot"' in source
        assert '"id": "native_process_registry_bootstrap"' in source
        assert '"command": "processes.snapshot"' in source
        assert '"id": "native_metrics_bootstrap"' in source
        assert '"command": "process.metrics.snapshot"' in source
        assert '"schema": "T023_NATIVE_BACKGROUND_PREWARM_V1"' in source
        assert '"window_blocked": false' in source
        assert source.index('"id": "native_process_registry_bootstrap"') < source.index('"id": "native_metrics_bootstrap"')
        assert source.index("window.show()", source.index(".setup(")) < source.index('"id": "native_process_registry_bootstrap"')
    for path in config_paths:
        assert json.loads(path.read_text(encoding="utf-8"))["app"]["windows"][0]["visible"] is False
    for path in cockpit_paths:
        source = path.read_text(encoding="utf-8")
        assert "Current output root catalog restored; legacy route pointers removed without deleting files" in source
        assert "App-roaming or last user-selected workspace restored and scanned" not in source


def test_output_folder_handoff_is_independent_native_foreground_and_governed_copies_use_packages() -> None:
    rust_paths = (
        ROOT / "evidence-os-clean-full-app" / "frontend" / "src-tauri" / "src" / "main.rs",
        ROOT / "evidence-os-clean-frontend-theme" / "src-tauri" / "src" / "main.rs",
    )
    transport_paths = (
        ROOT / "evidence-os-clean-full-app" / "frontend" / "src" / "nativeAcceptedUiTransport.ts",
        ROOT / "evidence-os-clean-frontend-theme" / "src" / "nativeAcceptedUiTransport.ts",
    )
    cockpit_paths = (
        ROOT / "evidence-os-clean-full-app" / "frontend" / "theme-contract" / "src" / "components" / "EvidenceOSCockpitSimulation.tsx",
        ROOT / "evidence-os-clean-frontend-theme" / "theme-contract" / "src" / "components" / "EvidenceOSCockpitSimulation.tsx",
        ROOT / "evidence-os-clean-desktop" / "uiux-frozen-20260711" / "src" / "components" / "EvidenceOSCockpitSimulation.tsx",
    )

    for path in rust_paths:
        source = path.read_text(encoding="utf-8")
        assert "async fn open_native_folder" in source
        assert "NATIVE_FOLDER_PATH_MUST_BE_ABSOLUTE" in source
        assert "NATIVE_FOLDER_TARGET_NOT_DIRECTORY" in source
        assert 'Command::new("explorer.exe")' in source
        assert ".minimize()" in source
        assert 'external_foreground::activate_target("explorer"' in source
        assert '"status": "OPENED_MAXIMIZED_AND_FOREGROUNDED"' in source
        assert '"handoff_owner": "TAURI_NATIVE_INDEPENDENT_CHANNEL"' in source
    for path in transport_paths:
        source = path.read_text(encoding="utf-8")
        direct = source.index('if (command === "folder.open" && String(payload.path || "").trim())')
        worker = source.index("const response = await callWorker")
        assert direct < worker
        assert 'invoke<Record<string, unknown>>("open_native_folder"' in source
    for path in cockpit_paths:
        source = path.read_text(encoding="utf-8")
        assert 'onClick={() => openPopup("output-directory")}>Output Dir</GlassPill>' in source
        assert 'path: `${brainRoot}\\\\packages`' in source
        popup_start = source.index('popup === "output-directory"')
        output_popup = source[popup_start:source.index('popup === "profile"', popup_start)]
        assert "Set Current Output Folder" in output_popup
        assert "Older brain folders are not deleted, moved, or rescanned" in output_popup
        assert "<input" not in output_popup
