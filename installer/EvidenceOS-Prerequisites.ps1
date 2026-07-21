param(
    [ValidateSet("Scan", "InstallMissing", "PostInstallSelfTest")]
    [string]$Mode = "Scan",
    [string]$ReceiptPath = "",
    [string]$AppPath = "",
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if (-not $ReceiptPath) {
    $ReceiptPath = Join-Path $env:TEMP "EvidenceOS\installer\prerequisite-receipt.json"
}
$ReceiptPath = [System.IO.Path]::GetFullPath($ReceiptPath)

function Get-UtcTimestamp {
    [DateTime]::UtcNow.ToString("o")
}

function Refresh-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $extra = Join-Path $env:APPDATA "npm"
    $env:Path = (($machine, $user, $extra) | Where-Object { $_ }) -join ";"
}

function Get-FirstExistingPath {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Get-CommandPath {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            if ($command.Source) { return $command.Source }
            if ($command.Path) { return $command.Path }
        }
    }
    return $null
}

function Get-ToolVersion {
    param(
        [string]$Executable,
        [string[]]$Arguments = @("--version")
    )
    if (-not $Executable) { return "" }
    try {
        $output = & $Executable @Arguments 2>&1 | Select-Object -First 1
        if ($null -eq $output) { return "" }
        return ([string]$output).Trim()
    } catch {
        return ""
    }
}

function Get-WebView2Runtime {
    $roots = @(
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients"
    )
    foreach ($root in $roots) {
        foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
            $item = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if ($item -and ([string]$item.name -match "WebView2")) {
                return [ordered]@{
                    available = $true
                    path = $key.PSPath
                    version = [string]$item.pv
                }
            }
        }
    }
    return [ordered]@{ available = $false; path = ""; version = "" }
}

function New-ExternalDependency {
    param(
        [string]$Id,
        [string]$Purpose,
        [string]$InstallKind,
        [string]$InstallId,
        [string]$Path,
        [string]$Version
    )
    [ordered]@{
        id = $Id
        purpose = $Purpose
        class = "REQUIRED_EXTERNAL_RUNTIME_TOOL"
        available = [bool]$Path
        path = if ($Path) { $Path } else { "" }
        version = if ($Version) { $Version } else { "" }
        install_kind = $InstallKind
        install_id = $InstallId
    }
}

function Get-EvidenceOSPrerequisiteScan {
    Refresh-ProcessPath

    # NSIS itself is a 32-bit process.  A PowerShell child launched by the
    # installer can therefore expose ProgramFiles as Program Files (x86) even
    # on a 64-bit OS.  Probe both physical roots explicitly so an installed
    # 64-bit dependency is never mistaken for a missing package.
    $programRoots = @(
        $env:ProgramW6432,
        $env:ProgramFiles,
        ${env:ProgramFiles(x86)},
        (Join-Path $env:SystemDrive "Program Files"),
        (Join-Path $env:SystemDrive "Program Files (x86)")
    ) | Where-Object { $_ } | Select-Object -Unique

    $git = Get-CommandPath @("git.exe", "git")
    $node = Get-CommandPath @("node.exe", "node")
    $npm = Get-CommandPath @("npm.cmd", "npm.exe", "npm")
    $mmdc = Get-CommandPath @("mmdc.cmd", "mmdc.exe", "mmdc")
    $tesseractCandidates = @((Get-CommandPath @("tesseract.exe", "tesseract")))
    $tesseractCandidates += @($programRoots | ForEach-Object { Join-Path $_ "Tesseract-OCR\tesseract.exe" })
    $tesseract = Get-FirstExistingPath $tesseractCandidates
    $chromeCandidates = @($programRoots | ForEach-Object { Join-Path $_ "Google\Chrome\Application\chrome.exe" })
    $chromeCandidates += Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe"
    $chrome = Get-FirstExistingPath $chromeCandidates
    $webview = Get-WebView2Runtime

    $dependencies = @(
        (New-ExternalDependency "webview2" "Tauri desktop rendering runtime" "winget" "Microsoft.EdgeWebView2Runtime" $(if ($webview.available) { $webview.path } else { $null }) $webview.version),
        (New-ExternalDependency "chrome" "ChatGPT and Gemini main-profile handoff" "winget" "Google.Chrome" $chrome $(if ($chrome) { (Get-Item -LiteralPath $chrome).VersionInfo.ProductVersion } else { "" })),
        (New-ExternalDependency "git" "Git repository ingestion lane" "winget" "Git.Git" $git (Get-ToolVersion $git)),
        (New-ExternalDependency "node" "Node-backed package tooling" "winget" "OpenJS.NodeJS.LTS" $node (Get-ToolVersion $node)),
        (New-ExternalDependency "npm" "Node package command runtime" "provided_by_node" "OpenJS.NodeJS.LTS" $npm (Get-ToolVersion $npm)),
        (New-ExternalDependency "mmdc" "Mermaid diagram rendering lane" "npm_global" "@mermaid-js/mermaid-cli@11.12.0" $mmdc (Get-ToolVersion $mmdc)),
        (New-ExternalDependency "tesseract" "OCR lane" "winget" "UB-Mannheim.TesseractOCR" $tesseract (Get-ToolVersion $tesseract))
    )

    $codexCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Codex\Codex.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\OpenAI Codex\Codex.exe")
    )
    $codexCandidates += @($programRoots | ForEach-Object { Join-Path $_ "Codex\Codex.exe" })
    $codex = Get-FirstExistingPath $codexCandidates
    if (-not $codex) {
        $codexPackage = Join-Path $env:LOCALAPPDATA "Packages\OpenAI.Codex_2p2nqsd0c76g0"
        if (Test-Path -LiteralPath $codexPackage -PathType Container) {
            $codex = "shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"
        }
    }
    $ollama = Get-FirstExistingPath @(
        (Get-CommandPath @("ollama.exe", "ollama")),
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe")
    )

    $missing = @($dependencies | Where-Object { -not $_.available } | ForEach-Object { $_.id })
    [ordered]@{
        schema = "T023_INSTALLER_V1_PREREQUISITE_SCAN_V1"
        scanned_at_utc = Get-UtcTimestamp
        status = if ($missing.Count -eq 0) { "PASS" } else { "MISSING_DEPENDENCIES" }
        operating_system = [ordered]@{
            caption = (Get-CimInstance Win32_OperatingSystem).Caption
            version = [Environment]::OSVersion.Version.ToString()
            architecture = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { $env:PROCESSOR_ARCHITECTURE }
            powershell = $PSVersionTable.PSVersion.ToString()
            free_system_drive_bytes = (Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($env:SystemRoot).Substring(0, 1))).Free
        }
        bundled_runtime = @(
            [ordered]@{ id = "python"; class = "BUNDLED_IN_APP"; system_install_required = $false },
            [ordered]@{ id = "sqlite"; class = "BUNDLED_IN_APP"; system_install_required = $false },
            [ordered]@{ id = "python_modules"; detail = "PyInstaller, OpenPyXL, pypdf, PyMuPDF, psutil and declared backend requirements"; class = "BUNDLED_IN_APP"; system_install_required = $false },
            [ordered]@{ id = "frontend_runtime"; detail = "compiled React/Tauri assets"; class = "BUNDLED_IN_APP"; system_install_required = $false }
        )
        required_external_dependencies = $dependencies
        missing_required_ids = $missing
        optional_connectors = @(
            [ordered]@{ id = "codex"; available = [bool]$codex; path = if ($codex) { $codex } else { "" }; install_policy = "DETECT_ONLY_USER_MANAGED" },
            [ordered]@{ id = "ollama"; available = [bool]$ollama; path = if ($ollama) { $ollama } else { "" }; install_policy = "DETECT_ONLY_USER_MANAGED" }
        )
        glb_policy = "DEFERRED_TO_PUBLIC_V2_AFTER_INSTALLER_V1"
    }
}

function Invoke-WingetInstall {
    param([string]$PackageId)
    $winget = Get-CommandPath @("winget.exe", "winget")
    if (-not $winget) {
        throw "WINGET_REQUIRED_FOR_MISSING_DEPENDENCY:$PackageId"
    }
    $arguments = @(
        "install", "--id", $PackageId, "--exact", "--source", "winget", "--silent",
        "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"
    )
    $output = & $winget @arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "DEPENDENCY_INSTALL_FAILED:$PackageId`:exit=$code`:output=$($output -join ' ')"
    }
    [ordered]@{ id = $PackageId; installer = "winget"; exit_code = $code; output = ($output -join "`n") }
}

function Invoke-NpmMmdcInstall {
    Refresh-ProcessPath
    $npm = Get-CommandPath @("npm.cmd", "npm.exe", "npm")
    if (-not $npm) { throw "NPM_REQUIRED_FOR_MMDC_INSTALL" }
    $output = & $npm install --global "@mermaid-js/mermaid-cli@11.12.0" --no-audit --no-fund 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "DEPENDENCY_INSTALL_FAILED:mmdc`:exit=$code`:output=$($output -join ' ')"
    }
    [ordered]@{ id = "@mermaid-js/mermaid-cli@11.12.0"; installer = "npm_global"; exit_code = $code; output = ($output -join "`n") }
}

function Write-JsonReceipt {
    param([object]$Receipt)
    $parent = Split-Path -Parent $ReceiptPath
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$ReceiptPath.tmp.$PID"
    $Receipt | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $ReceiptPath -Force
}

function Invoke-PostInstallSelfTest {
    if (-not $AppPath) { throw "POSTINSTALL_APP_PATH_REQUIRED" }
    $resolvedApp = (Resolve-Path -LiteralPath $AppPath -ErrorAction Stop).Path
    $appReceipt = Join-Path (Split-Path -Parent $ReceiptPath) "installed-app-runtime-self-test.json"
    if (Test-Path -LiteralPath $appReceipt) { Remove-Item -LiteralPath $appReceipt -Force }
    $process = Start-Process -FilePath $resolvedApp -ArgumentList @("--evidenceos-self-test=$appReceipt") -PassThru -Wait
    if ($process.ExitCode -ne 0) {
        throw "INSTALLED_APP_SELF_TEST_PROCESS_FAILED:$($process.ExitCode)"
    }
    if (-not (Test-Path -LiteralPath $appReceipt -PathType Leaf)) {
        throw "INSTALLED_APP_SELF_TEST_RECEIPT_MISSING"
    }
    $runtime = Get-Content -LiteralPath $appReceipt -Raw | ConvertFrom-Json
    if ($runtime.status -ne "PASS") {
        throw "INSTALLED_APP_SELF_TEST_STATUS_FAILED:$($runtime.status)"
    }
    $scan = Get-EvidenceOSPrerequisiteScan
    $signature = Get-AuthenticodeSignature -LiteralPath $resolvedApp
    return [ordered]@{
        schema = "T023_INSTALLER_V1_POSTINSTALL_SELF_TEST_V1"
        status = "PASS"
        completed_at_utc = Get-UtcTimestamp
        app_path = $resolvedApp
        app_sha256 = (Get-FileHash -LiteralPath $resolvedApp -Algorithm SHA256).Hash.ToUpperInvariant()
        authenticode_status = [string]$signature.Status
        prerequisite_scan = $scan
        installed_app_runtime = $runtime
    }
}

$receipt = $null
try {
    if ($Mode -eq "Scan") {
        $scan = Get-EvidenceOSPrerequisiteScan
        $receipt = [ordered]@{
            schema = "T023_INSTALLER_V1_PREREQUISITE_RECEIPT_V1"
            status = $scan.status
            mode = $Mode
            scan_before = $scan
            installs = @()
            scan_after = $scan
        }
    } elseif ($Mode -eq "InstallMissing") {
        $before = Get-EvidenceOSPrerequisiteScan
        $installs = @()
        foreach ($dependency in @($before.required_external_dependencies | Where-Object { -not $_.available })) {
            if ($dependency.install_kind -eq "provided_by_node") { continue }
            if ($dependency.install_kind -eq "npm_global") {
                if (-not (Get-CommandPath @("npm.cmd", "npm.exe", "npm"))) {
                    $installs += Invoke-WingetInstall "OpenJS.NodeJS.LTS"
                    Refresh-ProcessPath
                }
                $installs += Invoke-NpmMmdcInstall
            } else {
                $installs += Invoke-WingetInstall $dependency.install_id
                Refresh-ProcessPath
            }
        }
        Refresh-ProcessPath
        $after = Get-EvidenceOSPrerequisiteScan
        $receipt = [ordered]@{
            schema = "T023_INSTALLER_V1_PREREQUISITE_RECEIPT_V1"
            status = if ($after.status -eq "PASS") { "PASS" } else { "FAILED_MISSING_DEPENDENCIES" }
            mode = $Mode
            scan_before = $before
            installs = $installs
            scan_after = $after
        }
        if ($receipt.status -ne "PASS") {
            throw "INSTALLER_PREREQUISITES_REMAIN_MISSING:$($after.missing_required_ids -join ',')"
        }
    } else {
        $receipt = Invoke-PostInstallSelfTest
    }
    Write-JsonReceipt $receipt
    $receipt | ConvertTo-Json -Depth 12
    exit 0
} catch {
    $failure = [ordered]@{
        schema = "T023_INSTALLER_V1_FAILURE_RECEIPT_V1"
        status = "FAIL"
        mode = $Mode
        failed_at_utc = Get-UtcTimestamp
        error = $_.Exception.Message
        partial_receipt = $receipt
    }
    Write-JsonReceipt $failure
    $failure | ConvertTo-Json -Depth 12
    exit 1
}
