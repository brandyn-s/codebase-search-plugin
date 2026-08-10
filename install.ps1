# Install script for codebase-search-plugin (Windows PowerShell)
# Downloads and configures both MCP servers (code-search + code-graph)
#
# Usage: pwsh install.ps1
#        powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TargetBinDir = Join-Path $PluginDir "bin"
$TargetVenvDir = Join-Path $PluginDir ".venv"
$BinDir = $TargetBinDir
$VenvDir = $TargetVenvDir
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
$GraphInstall = $Bom.components.'code-graph'.install
$GraphRepository = $GraphInstall.repository
$ReleaseTag = $GraphInstall.tag
$GraphSourceRevision = $GraphInstall.source_revision
$GraphChecksums = $GraphInstall.checksums.name
$GraphChecksumsSha256 = $GraphInstall.checksums.sha256
$GraphAttestationBundleRelativePath = $GraphInstall.attestation.bundle.path
$GraphAttestationBundleSha256 = $GraphInstall.attestation.bundle.sha256
$GraphSignerWorkflow = $GraphInstall.attestation.signer_workflow
$GraphSourceRef = $GraphInstall.attestation.source_ref
if ($GraphChecksums -ne "checksums.txt") {
    throw "code-graph checksum manifest must be checksums.txt"
}
$ExpectedGraphAttestationBundleRelativePath = (
    "compatibility/attestations/" +
    "code-graph-$ReleaseTag-provenance.jsonl"
)
if (
    $GraphAttestationBundleRelativePath -ne
    $ExpectedGraphAttestationBundleRelativePath
) {
    throw "code-graph attestation bundle path does not match the tested release"
}
$GraphAttestationBundlePath = Join-Path `
    $PluginDir `
    $GraphAttestationBundleRelativePath
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
        throw "checksum mismatch for $Label (expected $Expected, got $Actual)"
    }
}

function Assert-ChecksumManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ArtifactName,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    if (
        -not (Test-Path -LiteralPath $Path -PathType Leaf) -or
        [IO.Path]::GetFileName($ArtifactName) -ne $ArtifactName -or
        $Expected -notmatch '^[0-9a-f]{64}$'
    ) {
        throw "checksum manifest inputs are invalid"
    }
    $MatchesForArtifact = @()
    foreach ($Line in (Get-Content -LiteralPath $Path)) {
        if ($Line -notmatch '^([0-9a-fA-F]{64})[ \t]+\*?(.+)$') {
            if ($Line.Trim()) {
                throw "checksum manifest contains a malformed entry"
            }
            continue
        }
        if ($Matches[2] -eq $ArtifactName) {
            $MatchesForArtifact += $Matches[1].ToLowerInvariant()
        }
    }
    if (
        $MatchesForArtifact.Count -ne 1 -or
        $MatchesForArtifact[0] -ne $Expected
    ) {
        throw (
            "checksum manifest does not contain exactly one matching " +
            "artifact entry"
        )
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

function Invoke-WithAllowedEnvironment {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Operation
    )

    $AllowedEnvironmentNames = @(
        "PATH",
        "PATHEXT",
        "SystemRoot",
        "WINDIR",
        "ComSpec",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE"
    )
    $SavedEnvironment = @{}
    foreach (
        $Entry in [Environment]::GetEnvironmentVariables(
            "Process"
        ).GetEnumerator()
    ) {
        $SavedEnvironment[[string]$Entry.Key] = [string]$Entry.Value
        if ($AllowedEnvironmentNames -notcontains [string]$Entry.Key) {
            Remove-Item `
                -LiteralPath "Env:$([string]$Entry.Key)" `
                -ErrorAction SilentlyContinue
        }
    }
    $env:HOME = $script:InstallRuntimeHome
    $env:USERPROFILE = $script:InstallRuntimeHome
    try {
        & $Operation
    } finally {
        foreach (
            $Name in @(
                [Environment]::GetEnvironmentVariables("Process").Keys
            )
        ) {
            Remove-Item `
                -LiteralPath "Env:$([string]$Name)" `
                -ErrorAction SilentlyContinue
        }
        foreach ($Name in $SavedEnvironment.Keys) {
            Set-Item `
                -LiteralPath "Env:$Name" `
                -Value $SavedEnvironment[$Name]
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

$InstallStage = Join-Path $PluginDir (
    ".install-staging.$PID.$([IO.Path]::GetRandomFileName())"
)
$RollbackBinDir = "$TargetBinDir.rollback.$PID"
$RollbackVenvDir = "$TargetVenvDir.rollback.$PID"
$InstallCommitted = $false
$InstallPromoting = $false
$HadTargetBin = $false
$HadTargetVenv = $false
$NewBinPromoted = $false
$NewVenvPromoted = $false

function Restore-PreviousInstallation {
    Write-Host "Restoring previous installation..." -ForegroundColor Yellow
    if ($script:InstallPromoting) {
        if ($script:NewBinPromoted) {
            Remove-Item `
                -LiteralPath $script:TargetBinDir `
                -Recurse `
                -Force `
                -ErrorAction SilentlyContinue
        }
        if ($script:NewVenvPromoted) {
            Remove-Item `
                -LiteralPath $script:TargetVenvDir `
                -Recurse `
                -Force `
                -ErrorAction SilentlyContinue
        }
        if ($script:HadTargetBin -and (Test-Path $script:RollbackBinDir)) {
            Move-Item `
                -LiteralPath $script:RollbackBinDir `
                -Destination $script:TargetBinDir
        }
        if ($script:HadTargetVenv -and (Test-Path $script:RollbackVenvDir)) {
            Move-Item `
                -LiteralPath $script:RollbackVenvDir `
                -Destination $script:TargetVenvDir
        }
    }
    Remove-Item `
        -LiteralPath $script:InstallStage `
        -Recurse `
        -Force `
        -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Path $InstallStage | Out-Null
$InstallRuntimeHome = Join-Path $InstallStage "runtime-home"
New-Item -ItemType Directory -Path $InstallRuntimeHome | Out-Null
$BinDir = Join-Path $InstallStage "bin"
$VenvDir = $TargetVenvDir

try {
if (
    (Test-Path -LiteralPath $RollbackBinDir) -or
    (Test-Path -LiteralPath $RollbackVenvDir)
) {
    throw "rollback path already exists; refusing installation"
}
$InstallPromoting = $true
if (Test-Path -LiteralPath $TargetVenvDir) {
    Move-Item -LiteralPath $TargetVenvDir -Destination $RollbackVenvDir
    $HadTargetVenv = $true
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
    $NewVenvPromoted = $true
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
            Invoke-WithAllowedEnvironment {
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
        $CodeSearchChecksums = $CodeSearchInstall.checksums.name
        $CodeSearchChecksumsSha256 = $CodeSearchInstall.checksums.sha256
        $CodeSearchSignerWorkflow = $CodeSearchInstall.attestation.signer_workflow
        $CodeSearchSourceRef = $CodeSearchInstall.attestation.source_ref
        $CodeSearchDownloadDir = Join-Path $BinDir ".code-search-download"
        New-Item -ItemType Directory -Path $CodeSearchDownloadDir -Force | Out-Null
        $CodeSearchWheelPath = Join-Path $CodeSearchDownloadDir $CodeSearchWheel
        $CodeSearchBundlePath = Join-Path $CodeSearchDownloadDir $CodeSearchBundle
        $CodeSearchChecksumsPath = Join-Path $CodeSearchDownloadDir $CodeSearchChecksums

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
                --pattern $CodeSearchChecksums `
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
            Assert-Sha256 `
                -Path $CodeSearchChecksumsPath `
                -Expected $CodeSearchChecksumsSha256 `
                -Label $CodeSearchChecksums
            Assert-ChecksumManifest `
                -Path $CodeSearchChecksumsPath `
                -ArtifactName $CodeSearchWheel `
                -Expected $CodeSearchWheelSha256

            Write-Host "  Verifying offline build provenance..."
            Push-Location $CodeSearchDownloadDir
            try {
                Invoke-WithAllowedEnvironment {
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
            Invoke-WithAllowedEnvironment {
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
        Remove-Item -LiteralPath `
            $CodeSearchWheelPath, `
            $CodeSearchBundlePath, `
            $CodeSearchChecksumsPath
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

$GraphDownloadDir = Join-Path $BinDir ".code-graph-download"
New-Item -ItemType Directory -Path $GraphDownloadDir -Force | Out-Null
$ZipPath = Join-Path $GraphDownloadDir $AssetName
$GraphChecksumsPath = Join-Path $GraphDownloadDir $GraphChecksums

Write-Host "  Downloading code-graph $ReleaseTag for $Platform-$Arch..."
try {
    Assert-GitHubCliAuthenticated
    $ResolvedGraphRevision = Resolve-ReleaseTagCommit `
        -Repository $GraphRepository `
        -Tag $ReleaseTag
    if ($ResolvedGraphRevision -ne $GraphSourceRevision) {
        throw (
            "code-graph tag source revision mismatch: " +
            "expected $GraphSourceRevision, got $ResolvedGraphRevision"
        )
    }
    & gh release download $ReleaseTag `
        --repo $GraphRepository `
        --pattern $AssetName `
        --pattern $GraphChecksums `
        --dir $GraphDownloadDir `
        --clobber
    if ($LASTEXITCODE -ne 0) {
        throw "gh release download exited with status $LASTEXITCODE"
    }
} catch {
    Write-Host "Error: failed to download code-graph binary." -ForegroundColor Red
    Write-Host "  Authenticate gh or set GH_TOKEN with repository read access."
    exit 1
}

# Verify the archive and release manifest against the tested BOM.
Write-Host "  Verifying checksums and checksum manifest..."
try {
    Assert-Sha256 `
        -Path $ZipPath `
        -Expected $ExpectedSha256.ToLowerInvariant() `
        -Label $AssetName
    Assert-Sha256 `
        -Path $GraphChecksumsPath `
        -Expected $GraphChecksumsSha256 `
        -Label $GraphChecksums
    Assert-ChecksumManifest `
        -Path $GraphChecksumsPath `
        -ArtifactName $AssetName `
        -Expected $ExpectedSha256.ToLowerInvariant()
} catch {
    Write-Host "Error: SHA-256 verification failed for $AssetName`: $_" -ForegroundColor Red
    exit 1
}
Write-Host "  Checksum OK."

try {
    Assert-Sha256 `
        -Path $GraphAttestationBundlePath `
        -Expected $GraphAttestationBundleSha256 `
        -Label $GraphAttestationBundleRelativePath
} catch {
    Write-Host "Error: graph attestation bundle verification failed: $_" -ForegroundColor Red
    exit 1
}

Write-Host "  Verifying code-graph build provenance..."
Push-Location $GraphDownloadDir
try {
    Invoke-WithAllowedEnvironment {
        & gh attestation verify $AssetName `
            --bundle $GraphAttestationBundlePath `
            --repo $GraphRepository `
            --signer-workflow $GraphSignerWorkflow `
            --source-digest $GraphSourceRevision `
            --source-ref $GraphSourceRef `
            --deny-self-hosted-runners
        if ($LASTEXITCODE -ne 0) {
            throw "gh attestation verify exited with status $LASTEXITCODE"
        }
    }
} finally {
    Pop-Location
}

Expand-Archive -Path $ZipPath -DestinationPath $BinDir -Force
Remove-Item -LiteralPath `
    $ZipPath, `
    $GraphChecksumsPath
Remove-Item -LiteralPath $GraphDownloadDir -ErrorAction SilentlyContinue

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
    Invoke-WithAllowedEnvironment {
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

Write-Host "Promoting validated installation..." -ForegroundColor Yellow
if (Test-Path -LiteralPath $TargetBinDir) {
    Move-Item -LiteralPath $TargetBinDir -Destination $RollbackBinDir
    $HadTargetBin = $true
}
Move-Item -LiteralPath $BinDir -Destination $TargetBinDir
$NewBinPromoted = $true
$InstallCommitted = $true
$InstallPromoting = $false
} finally {
    if (-not $InstallCommitted) {
        Restore-PreviousInstallation
    } else {
        Remove-Item `
            -LiteralPath $RollbackBinDir `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
        Remove-Item `
            -LiteralPath $RollbackVenvDir `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
        Remove-Item `
            -LiteralPath $InstallStage `
            -Recurse `
            -Force `
            -ErrorAction SilentlyContinue
    }
}

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
