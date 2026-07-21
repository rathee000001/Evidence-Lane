$ErrorActionPreference = "Stop"
$env:CARGO_TARGET_DIR = "C:\eos_tauri_target_t020"
Push-Location "$PSScriptRoot\evidence-os-clean-desktop"
npm ci
npm run tauri -- build --no-bundle
Pop-Location
Write-Host "Built Tauri executable under C:\eos_tauri_target_t020\release; copy/rename to dist\EvidenceOS_SQLiteBuilder.exe if reproducing the packaged filename."
