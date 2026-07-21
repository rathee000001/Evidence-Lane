param(
    [switch]$SkipDependencyInstall,
    [switch]$BuildInstallerV1,
    [string]$BuildRoot = "",
    [string]$OutputDirectory = "",
    [string]$InstallerOutputDirectory = "",
    [string]$ApplicationFileName = "Evidence-OS-M0-Full-App.exe",
    [string]$InstallerFileName = "Evidence-OS-Installer-V1.exe",
    [string]$ReuseEmbeddedWorker = ""
)

$ErrorActionPreference = "Stop"
function Assert-ExternalSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}
$SourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $SourceRoot "backend"
$FrontendRoot = Join-Path $SourceRoot "frontend"
$BuildOutput = if ($BuildRoot) {
    [System.IO.Path]::GetFullPath($BuildRoot)
} else {
    Join-Path $SourceRoot "build-output"
}
$WorkerDist = Join-Path $BuildOutput "embedded-worker-dist"
$WorkerWork = Join-Path $BuildOutput "embedded-worker-work"
$SpecRoot = Join-Path $BuildOutput "spec"
$CargoTarget = Join-Path $BuildOutput "cargo-target"
$BackendTestTemp = Join-Path $BuildOutput "backend-test-temp"
$InstallerRoot = Join-Path $SourceRoot "installer"
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $BuildOutput "full-app-dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
if (-not $InstallerOutputDirectory) {
    $InstallerOutputDirectory = Join-Path $BuildOutput "installer-v1-dist"
}
$InstallerOutputDirectory = [System.IO.Path]::GetFullPath($InstallerOutputDirectory)

if (-not $SkipDependencyInstall) {
    python -m pip install -r (Join-Path $BackendRoot "requirements.txt")
    Assert-ExternalSuccess "Backend dependency installation"
}

New-Item -ItemType Directory -Force -Path $BuildOutput, $BackendTestTemp | Out-Null
$env:PYTHONPATH = Join-Path $BackendRoot "src"
Push-Location $BackendRoot
try {
    python -m pytest `
        "tests/test_t020_ipc_worker.py" `
        "tests/test_t021_local_workspace.py" `
        "tests/test_t021_env15_resource.py" `
        "tests/test_t023_gemini_exact10_structure_chain.py" `
        "tests/test_t023_selected_brain_backend_integrity_amendment_002.py" `
        -q `
        -p no:cacheprovider `
        --basetemp $BackendTestTemp
    Assert-ExternalSuccess "Fresh-source backend validation"
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force -Path $WorkerDist, $WorkerWork, $SpecRoot, $CargoTarget, $OutputDirectory, $InstallerOutputDirectory | Out-Null
if ($ReuseEmbeddedWorker) {
    $WorkerExe = (Resolve-Path -LiteralPath $ReuseEmbeddedWorker).Path
} else {
    python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --name "EvidenceOS-Embedded-Backend" `
        --paths (Join-Path $BackendRoot "src") `
        --distpath $WorkerDist `
        --workpath $WorkerWork `
        --specpath $SpecRoot `
        --collect-all sqlite_brain_builder `
        (Join-Path $PSScriptRoot "embedded_worker_entry.py")
    Assert-ExternalSuccess "Embedded backend packaging"
    $WorkerExe = Join-Path $WorkerDist "EvidenceOS-Embedded-Backend.exe"
}
python (Join-Path $PSScriptRoot "verify_embedded_worker.py") $WorkerExe
Assert-ExternalSuccess "Embedded backend verification"
$WorkerHash = (Get-FileHash -LiteralPath $WorkerExe -Algorithm SHA256).Hash.ToUpperInvariant()
$env:EVIDENCE_OS_EMBEDDED_WORKER_EXE = $WorkerExe
$env:EVIDENCE_OS_EMBEDDED_WORKER_SHA256 = $WorkerHash
$env:CARGO_TARGET_DIR = $CargoTarget

Push-Location $FrontendRoot
try {
    npm.cmd ci
    Assert-ExternalSuccess "Frontend dependency installation"
    npm.cmd audit --audit-level=low
    Assert-ExternalSuccess "Frontend dependency audit"
    npm.cmd run build
    Assert-ExternalSuccess "Frontend production build"
    if ($BuildInstallerV1) {
        $PrerequisiteScript = (Resolve-Path -LiteralPath (Join-Path $InstallerRoot "EvidenceOS-Prerequisites.ps1")).Path
        $HookTemplate = (Resolve-Path -LiteralPath (Join-Path $InstallerRoot "EvidenceOS-Installer-Hooks.nsh.in")).Path
        $GeneratedHook = Join-Path $BuildOutput "EvidenceOS-Installer-Hooks.generated.nsh"
        $HookBody = (Get-Content -LiteralPath $HookTemplate -Raw).Replace("@@PREREQUISITE_SCRIPT@@", $PrerequisiteScript)
        Set-Content -LiteralPath $GeneratedHook -Value $HookBody -Encoding UTF8

        $InstallerConfigPath = Join-Path $BuildOutput "tauri.installer-v1.generated.conf.json"
        $InstallerConfig = [ordered]@{
            bundle = [ordered]@{
                active = $true
                targets = @("nsis")
                icon = @((Resolve-Path -LiteralPath (Join-Path $FrontendRoot "src-tauri\icons\icon.ico")).Path)
                windows = [ordered]@{
                    allowDowngrades = $false
                    webviewInstallMode = [ordered]@{ type = "downloadBootstrapper"; silent = $true }
                    nsis = [ordered]@{
                        installMode = "currentUser"
                        compression = "lzma"
                        installerIcon = (Resolve-Path -LiteralPath (Join-Path $FrontendRoot "src-tauri\icons\icon.ico")).Path
                        uninstallerIcon = (Resolve-Path -LiteralPath (Join-Path $FrontendRoot "src-tauri\icons\icon.ico")).Path
                        installerHooks = $GeneratedHook
                    }
                }
            }
        }
        $InstallerConfig | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $InstallerConfigPath -Encoding UTF8
        npm.cmd run tauri -- build --bundles nsis --features embedded-worker --config $InstallerConfigPath --ci
        Assert-ExternalSuccess "Tauri NSIS update-installer build"
    } else {
        npm.cmd run tauri -- build --no-bundle --features embedded-worker
        Assert-ExternalSuccess "Tauri standalone native build"
    }
} finally {
    Pop-Location
}

$BuiltTauriExe = Join-Path $CargoTarget "release\evidence-os-clean-desktop.exe"
if (-not (Test-Path -LiteralPath $BuiltTauriExe -PathType Leaf)) {
    throw "Tauri full-app executable was not produced: $BuiltTauriExe"
}
$FinalExe = Join-Path $OutputDirectory $ApplicationFileName
Copy-Item -LiteralPath $BuiltTauriExe -Destination $FinalExe -Force
$InstallerResult = $null
if ($BuildInstallerV1) {
    $BuiltInstaller = Get-ChildItem -LiteralPath (Join-Path $CargoTarget "release\bundle\nsis") -Filter "*setup.exe" -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (-not $BuiltInstaller) {
        throw "Tauri NSIS installer was not produced"
    }
    $FinalInstaller = Join-Path $InstallerOutputDirectory $InstallerFileName
    Copy-Item -LiteralPath $BuiltInstaller.FullName -Destination $FinalInstaller -Force
    $BuildMachineScan = Join-Path $InstallerOutputDirectory "BUILD_MACHINE_PREREQUISITE_SCAN.json"
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $InstallerRoot "EvidenceOS-Prerequisites.ps1") `
        -Mode Scan `
        -ReceiptPath $BuildMachineScan `
        -NonInteractive | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Installer V1 build-machine prerequisite scan failed"
    }
    $InstallerResult = [ordered]@{
        installer = $FinalInstaller
        installer_sha256 = (Get-FileHash -LiteralPath $FinalInstaller -Algorithm SHA256).Hash.ToUpperInvariant()
        prerequisite_script_sha256 = (Get-FileHash -LiteralPath (Join-Path $InstallerRoot "EvidenceOS-Prerequisites.ps1") -Algorithm SHA256).Hash.ToUpperInvariant()
        generated_hook_sha256 = (Get-FileHash -LiteralPath (Join-Path $BuildOutput "EvidenceOS-Installer-Hooks.generated.nsh") -Algorithm SHA256).Hash.ToUpperInvariant()
        build_machine_scan = $BuildMachineScan
        install_mode = "CURRENT_USER_SCAN_FIRST"
        postinstall_self_test = "EMBEDDED_WORKER_HASH_AND_BACKEND_PING"
    }
}
$Result = [ordered]@{
    schema = if ($BuildInstallerV1) { "T023_INSTALLER_V1_FRESH_BUILD_RESULT_V1" } else { "T023_FULL_APP_FRESH_BUILD_RESULT_V3" }
    status = "PASS"
    source_root = $SourceRoot
    embedded_worker_sha256 = $WorkerHash
    full_app_exe = $FinalExe
    full_app_exe_sha256 = (Get-FileHash -LiteralPath $FinalExe -Algorithm SHA256).Hash.ToUpperInvariant()
    installer_started = [bool]$BuildInstallerV1
    embedded_worker_reused = [bool]$ReuseEmbeddedWorker
    bundle_mode = if ($BuildInstallerV1) { "NSIS_INSTALLER_V1_AND_SINGLE_EXE" } else { "NO_BUNDLE_SINGLE_EXE" }
    installer_v1 = $InstallerResult
}
$ResultFile = if ($BuildInstallerV1) { Join-Path $InstallerOutputDirectory "INSTALLER_V1_BUILD_RESULT.json" } else { Join-Path $OutputDirectory "FULL_APP_BUILD_RESULT.json" }
$Result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultFile -Encoding utf8
$Result | ConvertTo-Json -Depth 5
