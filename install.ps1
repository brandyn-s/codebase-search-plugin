# Install script for codebase-search-plugin (Windows PowerShell)
# Downloads and configures both MCP servers (code-search + code-graph)
#
# Usage: pwsh install.ps1
#        powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir = Join-Path $PluginDir "bin"
$VenvDir = Join-Path $PluginDir ".venv"
$BomPath = Join-Path $PluginDir "component-bom.json"
if (-not (Test-Path $BomPath)) {
    Write-Host "Error: tested component BOM not found: $BomPath" -ForegroundColor Red
    exit 1
}

Write-Host "=== Codebase Search Plugin Installer ===" -ForegroundColor Cyan
Write-Host ""

# Detect architecture
$Arch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "386" }
$Platform = "windows"
$GraphBinary = "codebase-memory-mcp.exe"
$AssetKey = "$Platform-$Arch"

Write-Host "Platform: $Platform-$Arch"
Write-Host ""

# Find Python 3.12+
$Python = $null
foreach ($cmd in @("python3", "python")) {
    try {
        $version = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($version) {
            $parts = $version.Split(".")
            if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12)) {
                $Python = $cmd
                Write-Host "  Using $cmd ($version)"
                break
            }
        }
    } catch {}
}

if (-not $Python) {
    Write-Host "Error: Python 3.12+ is required but not found." -ForegroundColor Red
    Write-Host "Install Python from https://www.python.org/downloads/"
    exit 1
}

# ------------------------------------------------------------------
# 1. Validate the plugin contract and committed readiness evidence
# ------------------------------------------------------------------
Write-Host "[1/5] Validating plugin contract and committed readiness evidence..." -ForegroundColor Yellow
$SavedReadinessEvidenceOverride = $env:CODE_INTEL_READINESS_EVIDENCE_OVERRIDE
try {
    Remove-Item Env:CODE_INTEL_READINESS_EVIDENCE_OVERRIDE -ErrorAction SilentlyContinue
    & $Python (Join-Path $PluginDir "scripts\validate_plugin.py")
    $PluginValidationExitCode = $LASTEXITCODE
} finally {
    if ($null -eq $SavedReadinessEvidenceOverride) {
        Remove-Item Env:CODE_INTEL_READINESS_EVIDENCE_OVERRIDE -ErrorAction SilentlyContinue
    } else {
        $env:CODE_INTEL_READINESS_EVIDENCE_OVERRIDE = $SavedReadinessEvidenceOverride
    }
}
if ($PluginValidationExitCode -ne 0) {
    Write-Host "Error: plugin contract validation failed." -ForegroundColor Red
    exit $PluginValidationExitCode
}
Write-Host ""

$Bom = Get-Content -Raw -Path $BomPath | ConvertFrom-Json
$CodeSearchRepository = $Bom.components.'code-search'.install.repository
$CodeSearchRef = $Bom.components.'code-search'.install.revision
$GraphRepository = $Bom.components.'code-graph'.install.repository
$ReleaseTag = $Bom.components.'code-graph'.install.tag
$ReadinessStatus = $Bom.integrated_readiness.status
$ReadinessReason = $Bom.integrated_readiness.reason
$AssetProperty = $Bom.components.'code-graph'.install.assets.PSObject.Properties[$AssetKey]
if (-not $AssetProperty) {
    Write-Host "Error: BOM release asset is missing for $AssetKey." -ForegroundColor Red
    exit 1
}
$AssetName = $AssetProperty.Value.name
$ExpectedSha256 = $AssetProperty.Value.sha256
if (-not $AssetName -or $ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$') {
    Write-Host "Error: BOM asset name or SHA-256 is missing or invalid for $AssetKey." -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------------
# 2. Install code-search (Python, pip from GitHub)
# ------------------------------------------------------------------
Write-Host "[2/5] Installing code-search (semantic search)..." -ForegroundColor Yellow

if (-not (Test-Path $VenvDir)) {
    Write-Host "  Creating virtual environment..."
    & $Python -m venv $VenvDir
}

$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPip)) {
    Write-Host "Error: pip not found in venv at $VenvPip" -ForegroundColor Red
    exit 1
}

Write-Host "  Installing redacted-code-search from GitHub..."
$CodeSearchRequirement = "redacted-code-search @ git+{0}@{1}" -f $CodeSearchRepository, $CodeSearchRef
& $VenvPip install --quiet $CodeSearchRequirement
if ($LASTEXITCODE -ne 0) {
    $PipExitCode = $LASTEXITCODE
    Write-Host "Error: code-search pip install failed with status $PipExitCode." -ForegroundColor Red
    exit $PipExitCode
}
& $VenvPython (Join-Path $PluginDir "scripts\verify_code_search_revision.py") `
    $CodeSearchRef `
    --repository $CodeSearchRepository
if ($LASTEXITCODE -ne 0) {
    $RevisionExitCode = $LASTEXITCODE
    Write-Host "Error: installed code-search revision verification failed." -ForegroundColor Red
    exit $RevisionExitCode
}

Write-Host "  code-search installed."
Write-Host ""

# ------------------------------------------------------------------
# 3. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
Write-Host "[3/5] Installing code-graph (structural analysis)..." -ForegroundColor Yellow

if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

Write-Host "  Tested release: $ReleaseTag"

$DownloadUrl = "https://github.com/${GraphRepository}/releases/download/${ReleaseTag}/${AssetName}"
$ZipPath = Join-Path $BinDir $AssetName

Write-Host "  Downloading code-graph $ReleaseTag for $Platform-$Arch..."
try {
    if ((Get-Command gh -ErrorAction SilentlyContinue) -and $env:GH_TOKEN) {
        Write-Host "  Using authenticated GitHub CLI download."
        & gh release download $ReleaseTag `
            --repo $GraphRepository `
            --pattern $AssetName `
            --dir $BinDir `
            --clobber
        if ($LASTEXITCODE -ne 0) {
            throw "gh release download exited with status $LASTEXITCODE"
        }
    } else {
        Write-Host "  Using public release URL fallback (GH_TOKEN and gh are required for private assets)."
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing
    }
} catch {
    Write-Host "Error: failed to download code-graph binary." -ForegroundColor Red
    Write-Host "  URL: $DownloadUrl"
    Write-Host "  For private releases, install gh and set GH_TOKEN with repository read access."
    exit 1
}

# Verify the archive against the SHA-256 pinned in the tested BOM.
Write-Host "  Verifying checksum..."
try {
    if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) {
        throw "downloaded archive is missing"
    }
    $ActualSha256 = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
} catch {
    Write-Host "Error: SHA-256 verification failed for $AssetName`: $_" -ForegroundColor Red
    exit 1
}
if ($ActualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
    Write-Host "Error: checksum mismatch for $AssetName" -ForegroundColor Red
    Write-Host "  expected: $($ExpectedSha256.ToLowerInvariant())"
    Write-Host "  actual:   $ActualSha256"
    Remove-Item $ZipPath -ErrorAction SilentlyContinue
    exit 1
}
Write-Host "  Checksum OK."

Expand-Archive -Path $ZipPath -DestinationPath $BinDir -Force
Remove-Item $ZipPath

Write-Host "  code-graph installed."
Write-Host ""

# ------------------------------------------------------------------
# 4. Create launcher scripts
# ------------------------------------------------------------------
Write-Host "[4/5] Creating launcher scripts..." -ForegroundColor Yellow

# code-search launcher (.cmd) — .mcp.json references bin/run-code-search;
# Windows resolves that base name to run-code-search.cmd via PATHEXT.
$LauncherContent = @"
@echo off
setlocal
set "SCRIPT_DIR=%~dp0.."
if exist "%SCRIPT_DIR%\.venv\Scripts\code-search-mcp.exe" (
    "%SCRIPT_DIR%\.venv\Scripts\code-search-mcp.exe" %*
) else (
    echo Error: code-search-mcp not found. Run install.ps1 first. >&2
    exit /b 1
)
"@

$LauncherPath = Join-Path $BinDir "run-code-search.cmd"
Set-Content -Path $LauncherPath -Value $LauncherContent -Encoding ASCII

# code-graph launcher (.cmd) — .mcp.json references bin/codebase-memory-mcp.
# The extracted binary is codebase-memory-mcp.exe; this .cmd shim ensures the
# base name resolves for spawners that search .cmd/.bat (not just .exe).
$GraphLauncher = @"
@echo off
"%~dp0codebase-memory-mcp.exe" %*
"@

$GraphLauncherPath = Join-Path $BinDir "codebase-memory-mcp.cmd"
Set-Content -Path $GraphLauncherPath -Value $GraphLauncher -Encoding ASCII

# Also create a bash launcher for Git Bash / WSL users on Windows
$BashContent = @"
#!/usr/bin/env bash
SCRIPT_DIR="`$(cd "`$(dirname "`$0")/.." && pwd)"
if [ -f "`$SCRIPT_DIR/.venv/Scripts/code-search-mcp.exe" ]; then
    exec "`$SCRIPT_DIR/.venv/Scripts/code-search-mcp.exe" "`$@"
elif [ -f "`$SCRIPT_DIR/.venv/bin/code-search-mcp" ]; then
    exec "`$SCRIPT_DIR/.venv/bin/code-search-mcp" "`$@"
else
    echo "Error: code-search-mcp not found. Run install.ps1 first." >&2
    exit 1
fi
"@

$BashPath = Join-Path $BinDir "run-code-search"
Set-Content -Path $BashPath -Value $BashContent -Encoding UTF8

Write-Host "  Launchers created."
Write-Host ""

# ------------------------------------------------------------------
# 5. Verify the installed MCP contracts
# ------------------------------------------------------------------
Write-Host "[5/5] Validating installed MCP tool contracts..." -ForegroundColor Yellow
$CodeSearchMcp = Join-Path $VenvDir "Scripts\code-search-mcp.exe"
$GraphMcp = Join-Path $BinDir $GraphBinary
& $VenvPython (Join-Path $PluginDir "scripts\validate_installed.py") `
    --server "code-search=$CodeSearchMcp" `
    --server "code-graph=$GraphMcp"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: installed MCP contract validation failed." -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host ""

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
Write-Host "=== Component Installation Complete ===" -ForegroundColor Green
Write-Host ""
switch ($ReadinessStatus) {
    "blocked" {
        Write-Host "=== INTEGRATED READINESS: BLOCKED ===" -ForegroundColor Yellow
        Write-Host $ReadinessReason
        Write-Host ""
        Write-Host "The component schemas validated, but do not run /index-repo with this BOM."
        Write-Host "Wait for a BOM whose tested capabilities and readiness evidence pass validation."
    }
    "ready" {
        Write-Host "=== INTEGRATED READINESS: READY ===" -ForegroundColor Green
        Write-Host "1. Install the plugin in Claude Code:"
        Write-Host "  /install-plugin $PluginDir"
        Write-Host "2. Configure the embedding provider as described in README.md."
        Write-Host "3. Index a repo:"
        Write-Host "  /index-repo <repo-path>"
    }
    default {
        Write-Host "Error: unknown integrated readiness status: $ReadinessStatus" -ForegroundColor Red
        exit 1
    }
}
