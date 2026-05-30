# Install script for codebase-search-plugin (Windows PowerShell)
# Downloads and configures both MCP servers (code-search + code-graph)
#
# Usage: pwsh install.ps1
#        powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir = Join-Path $PluginDir "bin"
$VenvDir = Join-Path $PluginDir ".venv"

# GitHub org that hosts code-search and code-graph.
$Org = "redacted-org"

# Pin code-search to a known-good commit for reproducible, immutable installs.
# Bump this ref to upgrade (prefer a tagged release once code-search cuts them).
$CodeSearchRef = "69721e0df21540d35cb91ea07d7f4fc8d1535cd2"

Write-Host "=== Codebase Search Plugin Installer ===" -ForegroundColor Cyan
Write-Host ""

# Detect architecture
$Arch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "386" }
$Platform = "windows"
$GraphBinary = "codebase-memory-mcp.exe"

Write-Host "Platform: $Platform-$Arch"
Write-Host ""

# ------------------------------------------------------------------
# 1. Install code-search (Python, pip from GitHub)
# ------------------------------------------------------------------
Write-Host "[1/3] Installing code-search (semantic search)..." -ForegroundColor Yellow

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

if (-not (Test-Path $VenvDir)) {
    Write-Host "  Creating virtual environment..."
    & $Python -m venv $VenvDir
}

$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"
if (-not (Test-Path $VenvPip)) {
    Write-Host "Error: pip not found in venv at $VenvPip" -ForegroundColor Red
    exit 1
}

Write-Host "  Installing redacted-code-search from GitHub..."
& $VenvPip install --quiet "redacted-code-search @ git+https://github.com/$Org/code-search.git@$CodeSearchRef"

Write-Host "  code-search installed."
Write-Host ""

# ------------------------------------------------------------------
# 2. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
Write-Host "[2/3] Installing code-graph (structural analysis)..." -ForegroundColor Yellow

if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

# Resolve the release tag. Prefer the gh CLI, then the GitHub API, then a
# pinned fallback so the installer still works without gh installed.
$ReleaseTag = $null
try {
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        $tag = gh release list --repo "$Org/code-graph" --limit 1 --json tagName --jq '.[0].tagName' 2>$null
        if ($tag) { $ReleaseTag = $tag.Trim() }
    }
} catch {}
if (-not $ReleaseTag) {
    try {
        $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Org/code-graph/releases" -UseBasicParsing -TimeoutSec 10
        if ($releases -and $releases.Count -gt 0) { $ReleaseTag = $releases[0].tag_name }
    } catch {}
}
if (-not $ReleaseTag) {
    $ReleaseTag = "v0.5.0-redacted.4"
    Write-Host "  Could not query latest release; using pinned fallback: $ReleaseTag"
} else {
    Write-Host "  Latest release: $ReleaseTag"
}

$AssetName = "codebase-memory-mcp-${Platform}-${Arch}.zip"
$DownloadUrl = "https://github.com/$Org/code-graph/releases/download/${ReleaseTag}/${AssetName}"
$ZipPath = Join-Path $BinDir $AssetName

Write-Host "  Downloading code-graph $ReleaseTag for $Platform-$Arch..."
try {
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing
} catch {
    Write-Host "Error: failed to download code-graph binary." -ForegroundColor Red
    Write-Host "  URL: $DownloadUrl"
    Write-Host "  Check that release '$ReleaseTag' exists and ships an asset for $Platform-$Arch."
    Write-Host "  Releases: https://github.com/$Org/code-graph/releases"
    exit 1
}

# Verify the archive against the release's published checksums (supply-chain).
Write-Host "  Verifying checksum..."
$ChecksumsUrl = "https://github.com/$Org/code-graph/releases/download/${ReleaseTag}/checksums.txt"
try {
    $checksums = (Invoke-WebRequest -Uri $ChecksumsUrl -UseBasicParsing -TimeoutSec 10).Content
    $expected = $null
    foreach ($line in ($checksums -split "`n")) {
        $cols = ($line -replace '\*', '').Trim() -split '\s+'
        if ($cols.Count -ge 2 -and $cols[1] -eq $AssetName) { $expected = $cols[0].ToLower() }
    }
    if ($expected) {
        $actual = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected) {
            Write-Host "Error: checksum mismatch for $AssetName" -ForegroundColor Red
            Write-Host "  expected: $expected"
            Write-Host "  actual:   $actual"
            Remove-Item $ZipPath -ErrorAction SilentlyContinue
            exit 1
        }
        Write-Host "  Checksum OK."
    } else {
        Write-Host "  Warning: $AssetName not found in checksums.txt; skipping verification."
    }
} catch {
    Write-Host "  Warning: could not fetch/parse checksums.txt; skipping verification."
}

Expand-Archive -Path $ZipPath -DestinationPath $BinDir -Force
Remove-Item $ZipPath

Write-Host "  code-graph installed."
Write-Host ""

# ------------------------------------------------------------------
# 3. Create launcher scripts
# ------------------------------------------------------------------
Write-Host "[3/3] Creating launcher scripts..." -ForegroundColor Yellow

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
# Done
# ------------------------------------------------------------------
Write-Host "=== Installation Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Choose your embedding provider:"
Write-Host ""
Write-Host "     Local (free, no data leaves your machine):"
Write-Host '       $env:EMBEDDING_PROVIDER = "jina"'
Write-Host ""
Write-Host "     Cloud (best quality, sends code to Voyage AI):"
Write-Host '       $env:EMBEDDING_PROVIDER = "voyage-context"'
Write-Host '       $env:VOYAGE_API_KEY = "pa-..."'
Write-Host ""
Write-Host "  2. Install the plugin in Claude Code:"
Write-Host "       /install-plugin $PluginDir"
Write-Host ""
Write-Host "  3. Index a repo:"
Write-Host "       /index-repo C:\path\to\your\repo"
Write-Host ""
Write-Host "  4. Ask questions:"
Write-Host '       "How does authentication work?"'
Write-Host '       "What calls processOrder?"'
Write-Host '       "Find dead code"'
