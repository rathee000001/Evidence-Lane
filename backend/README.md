# Evidence OS SQLite Builder - Exact Last EXE Build Package

This folder contains the exact `EvidenceOS_SQLiteBuilder.exe` copied from the immediately preceding Codex EXE build, plus the source and build inputs used for that build.

## Install dependencies

```powershell
cd evidence-os-clean-desktop
npm ci
```

Python backend code is under `src/sqlite_brain_builder`. Install Python package dependencies according to `pyproject.toml` if running backend tests directly.

## Run frontend in development

```powershell
cd evidence-os-clean-desktop
npm run dev
```

## Build the Tauri executable

```powershell
$env:CARGO_TARGET_DIR = "C:\eos_tauri_target_t020"
cd evidence-os-clean-desktop
npm run tauri -- build --no-bundle
```

The exact copied EXE is stored at `dist/EvidenceOS_SQLiteBuilder.exe`. Do not confuse it with a rebuilt executable.
