# Install script for codebase-search-plugin (Windows PowerShell)
# Downloads and configures both MCP servers (code-search + code-graph)
#
# Usage: pwsh install.ps1
#        powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir = Join-Path $PluginDir "bin"
$VenvDir = Join-Path $PluginDir ".venv"

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
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 12) {
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
& $VenvPip install --quiet "redacted-code-search @ git+https://github.com/redacted-org/code-search.git"

Write-Host "  code-search installed."
Write-Host ""

# ------------------------------------------------------------------
# 2. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
Write-Host "[2/3] Installing code-graph (structural analysis)..." -ForegroundColor Yellow

if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

# Get latest release tag
$ReleaseTag = "v0.5.0-redacted.4"
try {
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        $tag = gh release list --repo redacted-org/code-graph --limit 1 --json tagName --jq '.[0].tagName' 2>$null
        if ($tag) { $ReleaseTag = $tag }
    }
} catch {}

$AssetName = "codebase-memory-mcp-${Platform}-${Arch}.zip"
$DownloadUrl = "https://github.com/redacted-org/code-graph/releases/download/${ReleaseTag}/${AssetName}"
$ZipPath = Join-Path $BinDir $AssetName

Write-Host "  Downloading code-graph $ReleaseTag for $Platform-$Arch..."
Invoke-WebRequest -Uri $DownloadUrl -OutFile $ZipPath -UseBasicParsing

Expand-Archive -Path $ZipPath -DestinationPath $BinDir -Force
Remove-Item $ZipPath

Write-Host "  code-graph installed."
Write-Host ""

# ------------------------------------------------------------------
# 3. Create launcher script
# ------------------------------------------------------------------
Write-Host "[3/3] Creating launcher scripts..." -ForegroundColor Yellow

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

# Also create bash launcher for Git Bash / WSL users on Windows
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
