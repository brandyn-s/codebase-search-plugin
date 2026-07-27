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
$CodeSearchInstall = $Bom.components.'code-search'.install
$CodeSearchKind = $CodeSearchInstall.kind
$CodeSearchRepository = $CodeSearchInstall.repository
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

function Assert-Sha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Expected -notmatch '^[0-9a-f]{64}$') {
        throw "BOM SHA-256 is missing or invalid for $Label"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "downloaded release asset is missing: $Path"
    }
    $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) {
        Remove-Item -LiteralPath $Path -ErrorAction SilentlyContinue
        throw "checksum mismatch for $Label (expected $Expected, got $Actual)"
    }
}

function Assert-GitHubCliAuthenticated {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI is required for private code-search releases"
    }
    & gh auth status --hostname github.com *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "authenticate GitHub CLI with gh auth login or GH_TOKEN"
    }
}

function Invoke-WithoutGitHubTokens {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Operation
    )

    $TokenNames = @(
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CODE_INTEL_COMPONENT_TOKEN"
    )
    $SavedTokens = @{}
    foreach ($Name in $TokenNames) {
        $SavedTokens[$Name] = [Environment]::GetEnvironmentVariable(
            $Name,
            "Process"
        )
        Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    }
    try {
        & $Operation
    } finally {
        foreach ($Name in $TokenNames) {
            if ($null -eq $SavedTokens[$Name]) {
                Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
            } else {
                Set-Item -LiteralPath "Env:$Name" -Value $SavedTokens[$Name]
            }
        }
    }
}

function Invoke-GitHubApiJson {
    param(
        [Parameter(Mandatory = $true)][string]$Endpoint
    )

    $RawResponse = & gh api --method GET $Endpoint
    if ($LASTEXITCODE -ne 0) {
        throw "gh api failed for $Endpoint with status $LASTEXITCODE"
    }
    try {
        return $RawResponse | ConvertFrom-Json
    } catch {
        throw "GitHub API returned malformed JSON for $Endpoint"
    }
}

function Resolve-ReleaseTagCommit {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Tag
    )

    $Document = Invoke-GitHubApiJson `
        -Endpoint "repos/$Repository/git/ref/tags/$Tag"
    for ($Depth = 0; $Depth -lt 16; $Depth++) {
        $TargetType = $Document.object.type
        $TargetSha = $Document.object.sha
        if (
            -not $TargetSha -or
            $TargetSha -notmatch '^([0-9a-f]{40}|[0-9a-f]{64})$'
        ) {
            throw "GitHub tag response has an invalid object SHA"
        }
        switch ($TargetType) {
            "commit" {
                return $TargetSha
            }
            "tag" {
                $Document = Invoke-GitHubApiJson `
                    -Endpoint "repos/$Repository/git/tags/$TargetSha"
            }
            default {
                throw "GitHub tag resolves to unsupported object type: $TargetType"
            }
        }
    }
    throw "GitHub annotated tag chain exceeds 16 objects"
}

# ------------------------------------------------------------------
# 2. Install code-search (Python, exact Git revision or release wheel)
# ------------------------------------------------------------------
Write-Host "[2/5] Installing code-search (semantic search)..." -ForegroundColor Yellow

if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

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

switch ($CodeSearchKind) {
    "git" {
        $CodeSearchRef = $CodeSearchInstall.revision
        Write-Host "  Installing redacted-code-search from GitHub..."
        $CodeSearchRequirement = "redacted-code-search @ git+{0}@{1}" -f $CodeSearchRepository, $CodeSearchRef
        & $VenvPip install --quiet $CodeSearchRequirement
        if ($LASTEXITCODE -ne 0) {
            $PipExitCode = $LASTEXITCODE
            Write-Host "Error: code-search pip install failed with status $PipExitCode." -ForegroundColor Red
            exit $PipExitCode
        }
        try {
            Invoke-WithoutGitHubTokens {
                & $VenvPython (Join-Path $PluginDir "scripts\verify_code_search_revision.py") `
                    $CodeSearchRef `
                    --repository $CodeSearchRepository
                if ($LASTEXITCODE -ne 0) {
                    throw (
                        "installed code-search revision verification " +
                        "failed with status $LASTEXITCODE"
                    )
                }
            }
        } catch {
            Write-Host "Error: $_" -ForegroundColor Red
            exit 1
        }
    }
    "github-release" {
        $CodeSearchTag = $CodeSearchInstall.tag
        $CodeSearchSourceRevision = $CodeSearchInstall.source_revision
        $CodeSearchWheel = $CodeSearchInstall.asset.name
        $CodeSearchWheelSha256 = $CodeSearchInstall.asset.sha256
        $CodeSearchBundle = $CodeSearchInstall.attestation.bundle.name
        $CodeSearchBundleSha256 = $CodeSearchInstall.attestation.bundle.sha256
        $CodeSearchSignerWorkflow = $CodeSearchInstall.attestation.signer_workflow
        $CodeSearchSourceRef = $CodeSearchInstall.attestation.source_ref
        $CodeSearchDownloadDir = Join-Path $BinDir ".code-search-download"
        New-Item -ItemType Directory -Path $CodeSearchDownloadDir -Force | Out-Null
        $CodeSearchWheelPath = Join-Path $CodeSearchDownloadDir $CodeSearchWheel
        $CodeSearchBundlePath = Join-Path $CodeSearchDownloadDir $CodeSearchBundle

        try {
            Assert-GitHubCliAuthenticated
            $ResolvedCodeSearchRevision = Resolve-ReleaseTagCommit `
                -Repository $CodeSearchRepository `
                -Tag $CodeSearchTag
            if ($ResolvedCodeSearchRevision -ne $CodeSearchSourceRevision) {
                throw (
                    "code-search tag source revision mismatch: " +
                    "expected $CodeSearchSourceRevision, " +
                    "got $ResolvedCodeSearchRevision"
                )
            }
            Write-Host "  Downloading tested code-search wheel and attestation bundle..."
            & gh release download $CodeSearchTag `
                --repo $CodeSearchRepository `
                --pattern $CodeSearchWheel `
                --pattern $CodeSearchBundle `
                --dir $CodeSearchDownloadDir `
                --clobber
            if ($LASTEXITCODE -ne 0) {
                throw "gh release download exited with status $LASTEXITCODE"
            }

            Assert-Sha256 `
                -Path $CodeSearchWheelPath `
                -Expected $CodeSearchWheelSha256 `
                -Label $CodeSearchWheel
            Assert-Sha256 `
                -Path $CodeSearchBundlePath `
                -Expected $CodeSearchBundleSha256 `
                -Label $CodeSearchBundle

            Write-Host "  Verifying offline build provenance..."
            Push-Location $CodeSearchDownloadDir
            try {
                Invoke-WithoutGitHubTokens {
                    & gh attestation verify $CodeSearchWheel `
                        --bundle $CodeSearchBundle `
                        --repo $CodeSearchRepository `
                        --signer-workflow $CodeSearchSignerWorkflow `
                        --source-digest $CodeSearchSourceRevision `
                        --source-ref $CodeSearchSourceRef `
                        --deny-self-hosted-runners
                    if ($LASTEXITCODE -ne 0) {
                        throw "gh attestation verify exited with status $LASTEXITCODE"
                    }
                }
            } finally {
                Pop-Location
            }

            Write-Host "  Installing the verified local redacted-code-search wheel..."
            Invoke-WithoutGitHubTokens {
                & $VenvPip install --quiet --force-reinstall $CodeSearchWheelPath
                if ($LASTEXITCODE -ne 0) {
                    throw "code-search wheel install exited with status $LASTEXITCODE"
                }
                & $VenvPython (Join-Path $PluginDir "scripts\verify_code_search_wheel.py") `
                    $CodeSearchTag `
                    --asset-name $CodeSearchWheel `
                    --sha256 $CodeSearchWheelSha256
                if ($LASTEXITCODE -ne 0) {
                    throw "installed code-search wheel provenance verification failed"
                }
            }
        } catch {
            Write-Host "Error: code-search release installation failed: $_" -ForegroundColor Red
            exit 1
        }
        Remove-Item -LiteralPath $CodeSearchWheelPath, $CodeSearchBundlePath
        Remove-Item -LiteralPath $CodeSearchDownloadDir -ErrorAction SilentlyContinue
    }
    default {
        Write-Host "Error: unsupported code-search install kind: $CodeSearchKind" -ForegroundColor Red
        exit 1
    }
}

Write-Host "  code-search installed."
Write-Host ""

# ------------------------------------------------------------------
# 3. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
Write-Host "[3/5] Installing code-graph (structural analysis)..." -ForegroundColor Yellow

Write-Host "  Tested release: $ReleaseTag"

$DownloadUrl = "https://github.com/${GraphRepository}/releases/download/${ReleaseTag}/${AssetName}"
$ZipPath = Join-Path $BinDir $AssetName

Write-Host "  Downloading code-graph $ReleaseTag for $Platform-$Arch..."
try {
    $GhAuthenticated = $false
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        & gh auth status --hostname github.com *> $null
        $GhAuthenticated = $LASTEXITCODE -eq 0
    }
    if ($GhAuthenticated) {
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
        Write-Host "  Using public release URL fallback (authenticated gh or GH_TOKEN is required for private assets)."
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
    Assert-Sha256 `
        -Path $ZipPath `
        -Expected $ExpectedSha256.ToLowerInvariant() `
        -Label $AssetName
} catch {
    Write-Host "Error: SHA-256 verification failed for $AssetName`: $_" -ForegroundColor Red
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
try {
    Invoke-WithoutGitHubTokens {
        & $VenvPython (Join-Path $PluginDir "scripts\validate_installed.py") `
            --server "code-search=$CodeSearchMcp" `
            --server "code-graph=$GraphMcp"
        if ($LASTEXITCODE -ne 0) {
            throw (
                "installed MCP contract validation failed with status " +
                "$LASTEXITCODE"
            )
        }
    }
} catch {
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
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
