# Install script for codebase-search-plugin (Windows PowerShell)
# Downloads and configures both MCP servers (code-search + code-graph)
#
# Usage: pwsh install.ps1
#        powershell -ExecutionPolicy Bypass -File install.ps1

$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# Layout: bin\ holds the committed launchers referenced by .mcp.json (bash on
# macOS/Linux; this installer adds .cmd shims for Windows). Installed
# components are runtime state under .runtime\bin and .venv.
$RuntimeDir = Join-Path $PluginDir ".runtime"
$LauncherDir = Join-Path $PluginDir "bin"
$TargetBinDir = Join-Path $RuntimeDir "bin"
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
# Installed under a stable name regardless of the name inside the archive.
$GraphBinary = "code-graph.exe"
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
$GoScip = $Bom.precision_generators.'go-scip'
$GoScipSupported = $null -ne $GoScip.assets.PSObject.Properties[$AssetKey]
$TypeScriptScip = $Bom.precision_generators.'typescript-scip'
$TypeScriptNodeAssetProperty = `
    $TypeScriptScip.node_runtime.assets.PSObject.Properties[$AssetKey]
$TypeScriptScipSupported = $null -ne $TypeScriptNodeAssetProperty
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

# GitHub CLI is optional. Public releases are fetched with Invoke-WebRequest;
# gh is used when present and authenticated (private releases, provenance).
$script:GitHubCliAuthenticated = $null
function Test-GitHubCliAuthenticated {
    if ($null -eq $script:GitHubCliAuthenticated) {
        $script:GitHubCliAuthenticated = $false
        if (Get-Command gh -ErrorAction SilentlyContinue) {
            & gh auth status --hostname github.com *> $null
            if ($LASTEXITCODE -eq 0) {
                $script:GitHubCliAuthenticated = $true
            }
        }
    }
    return $script:GitHubCliAuthenticated
}

# Download one release asset. Public releases download directly; gh is the
# fallback for private releases.
function Save-ReleaseAsset {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$Tag,
        [Parameter(Mandatory = $true)][string]$Asset,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $Target = Join-Path $Destination $Asset
    $Url = "https://github.com/$Repository/releases/download/$Tag/$Asset"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Target -UseBasicParsing
        return
    } catch {
        Remove-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
    }
    if (Test-GitHubCliAuthenticated) {
        & gh release download $Tag `
            --repo $Repository `
            --pattern $Asset `
            --dir $Destination `
            --clobber
        if ($LASTEXITCODE -ne 0) {
            throw "gh release download exited with status $LASTEXITCODE"
        }
        return
    }
    throw (
        "could not download $Asset from $Repository ($Tag): the public release " +
        "URL was unavailable and the GitHub CLI is not authenticated. For private " +
        "releases install gh, run 'gh auth login' (or set GH_TOKEN), and re-run."
    )
}

# Verify build provenance when gh is available. Checksums against the BOM are
# always mandatory (Assert-Sha256); provenance additionally needs gh.
function Invoke-ReleaseAttestation {
    param(
        [Parameter(Mandatory = $true)][string]$Artifact,
        [Parameter(Mandatory = $true)][string]$Bundle,
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$SignerWorkflow,
        [Parameter(Mandatory = $true)][string]$SourceDigest,
        [Parameter(Mandatory = $true)][string]$SourceRef
    )

    if (-not (Test-GitHubCliAuthenticated)) {
        Write-Host "  Provenance attestation not verified for $Artifact`: GitHub CLI is not available or not authenticated." -ForegroundColor Yellow
        Write-Host "  Checksums matched the tested BOM. To also verify build provenance, install gh, run 'gh auth login', and re-run install.ps1."
        return
    }
    Invoke-WithAllowedEnvironment {
        & gh attestation verify $Artifact `
            --bundle $Bundle `
            --repo $Repository `
            --signer-workflow $SignerWorkflow `
            --source-digest $SourceDigest `
            --source-ref $SourceRef `
            --deny-self-hosted-runners
        if ($LASTEXITCODE -ne 0) {
            throw "gh attestation verify exited with status $LASTEXITCODE"
        }
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

    if (Test-GitHubCliAuthenticated) {
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
    $Headers = @{ Accept = "application/vnd.github+json" }
    if ($env:GH_TOKEN) {
        $Headers["Authorization"] = "Bearer $($env:GH_TOKEN)"
    }
    try {
        return Invoke-RestMethod -Uri "https://api.github.com/$Endpoint" -Headers $Headers -UseBasicParsing
    } catch {
        throw "GitHub API request failed for $Endpoint`: $_"
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
        # Distribution name of the pinned source; older pins used redacted-code-search.
        $CodeSearchDist = if ($CodeSearchInstall.PSObject.Properties["distribution"]) {
            $CodeSearchInstall.distribution
        } else {
            "redacted-code-search"
        }
        Write-Host "  Installing code-search from GitHub..."
        $CodeSearchRequirement = "$CodeSearchDist @ git+{0}@{1}" -f $CodeSearchRepository, $CodeSearchRef
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
            foreach ($ReleaseFile in @($CodeSearchWheel, $CodeSearchBundle, $CodeSearchChecksums)) {
                Save-ReleaseAsset `
                    -Repository $CodeSearchRepository `
                    -Tag $CodeSearchTag `
                    -Asset $ReleaseFile `
                    -Destination $CodeSearchDownloadDir
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
                Invoke-ReleaseAttestation `
                    -Artifact $CodeSearchWheel `
                    -Bundle $CodeSearchBundle `
                    -Repository $CodeSearchRepository `
                    -SignerWorkflow $CodeSearchSignerWorkflow `
                    -SourceDigest $CodeSearchSourceRevision `
                    -SourceRef $CodeSearchSourceRef
            } finally {
                Pop-Location
            }

            Write-Host "  Installing the verified local code-search wheel..."
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
    $ResolvedGraphRevision = Resolve-ReleaseTagCommit `
        -Repository $GraphRepository `
        -Tag $ReleaseTag
    if ($ResolvedGraphRevision -ne $GraphSourceRevision) {
        throw (
            "code-graph tag source revision mismatch: " +
            "expected $GraphSourceRevision, got $ResolvedGraphRevision"
        )
    }
    foreach ($ReleaseFile in @($AssetName, $GraphChecksums)) {
        Save-ReleaseAsset `
            -Repository $GraphRepository `
            -Tag $ReleaseTag `
            -Asset $ReleaseFile `
            -Destination $GraphDownloadDir
    }
} catch {
    Write-Host "Error: failed to download code-graph binary: $_" -ForegroundColor Red
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
    Invoke-ReleaseAttestation `
        -Artifact $AssetName `
        -Bundle $GraphAttestationBundlePath `
        -Repository $GraphRepository `
        -SignerWorkflow $GraphSignerWorkflow `
        -SourceDigest $GraphSourceRevision `
        -SourceRef $GraphSourceRef
} finally {
    Pop-Location
}

# Release archives contain exactly one binary. Current releases name it
# code-graph.exe; releases before the rename shipped codebase-memory-mcp.exe.
# Install it under the stable name the launcher expects.
$GraphExtractDir = Join-Path $GraphDownloadDir "extracted"
Expand-Archive -Path $ZipPath -DestinationPath $GraphExtractDir -Force
$ExtractedGraphBinary = $null
foreach ($Candidate in @("code-graph.exe", "codebase-memory-mcp.exe")) {
    if (Test-Path -LiteralPath (Join-Path $GraphExtractDir $Candidate)) {
        $ExtractedGraphBinary = Join-Path $GraphExtractDir $Candidate
        break
    }
}
if (-not $ExtractedGraphBinary) {
    Write-Host "Error: release archive $AssetName does not contain a code-graph binary." -ForegroundColor Red
    exit 1
}
Move-Item -LiteralPath $ExtractedGraphBinary -Destination (Join-Path $BinDir $GraphBinary)
Remove-Item -LiteralPath $GraphDownloadDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "  code-graph installed."
Write-Host ""

# Installing optional Go SCIP precision generator. Automatic use remains an
# explicit /index-repo choice and verifies this binary again before execution.
Write-Host "Installing optional Go SCIP precision generator..." -ForegroundColor Yellow
if ($GoScipSupported) {
    $GoScipAsset = $GoScip.assets.PSObject.Properties[$AssetKey].Value
    $ResolvedGoScipRevision = Resolve-ReleaseTagCommit `
        -Repository $GoScip.repository `
        -Tag $GoScip.tag
    if ($ResolvedGoScipRevision -ne $GoScip.source_revision) {
        throw "go-scip tag source revision mismatch"
    }
    $GoScipDownloadDir = Join-Path $BinDir ".go-scip-download"
    New-Item -ItemType Directory -Path $GoScipDownloadDir | Out-Null
    Save-ReleaseAsset `
        -Repository $GoScip.repository `
        -Tag $GoScip.tag `
        -Asset $GoScipAsset.name `
        -Destination $GoScipDownloadDir
    $GoScipArchive = Join-Path $GoScipDownloadDir $GoScipAsset.name
    Assert-Sha256 `
        -Path $GoScipArchive `
        -Expected $GoScipAsset.archive_sha256 `
        -Label $GoScipAsset.name
    tar xzf $GoScipArchive -C $GoScipDownloadDir
    if ($LASTEXITCODE -ne 0) {
        throw "go-scip archive extraction failed"
    }
    $ExtractedGoScip = Join-Path $GoScipDownloadDir "scip-go"
    Assert-Sha256 `
        -Path $ExtractedGoScip `
        -Expected $GoScipAsset.binary_sha256 `
        -Label "scip-go binary"
    $InstalledGoScip = Join-Path $BinDir "scip-go"
    Move-Item -LiteralPath $ExtractedGoScip -Destination $InstalledGoScip
    Remove-Item -LiteralPath $GoScipArchive -Force
    $GoScipLicense = Join-Path $GoScipDownloadDir "LICENSE"
    if (Test-Path -LiteralPath $GoScipLicense) {
        Remove-Item -LiteralPath $GoScipLicense -Force
    }
    Remove-Item -LiteralPath $GoScipDownloadDir -Force
    Invoke-WithAllowedEnvironment {
        & $Python `
            (Join-Path $PluginDir "scripts\prepare_scip_index.py") `
            verify `
            --generator $InstalledGoScip `
            --component-bom $BomPath
        if ($LASTEXITCODE -ne 0) {
            throw "installed go-scip verification failed"
        }
    }
    Write-Host "  scip-go $($GoScip.version_output) installed and verified."
} else {
    Write-Host (
        "  Auto SCIP precision unavailable for $AssetKey; " +
        "heuristic and supplied SCIP modes remain available."
    )
}
Write-Host ""

# Install TypeScript SCIP into an isolated plugin runtime. Target checkouts
# must already have dependencies; this installer never runs npm in a target.
Write-Host "Installing optional TypeScript SCIP precision generator..." -ForegroundColor Yellow
if ($TypeScriptScipSupported) {
    $TypeScriptNodeAsset = $TypeScriptNodeAssetProperty.Value
    $TypeScriptScipRuntime = Join-Path $BinDir "scip-typescript-runtime"
    $TypeScriptScipDownload = Join-Path $BinDir ".typescript-scip-download"
    New-Item -ItemType Directory -Path $TypeScriptScipDownload | Out-Null
    $TypeScriptNodeArchive = Join-Path `
        $TypeScriptScipDownload `
        $TypeScriptNodeAsset.name
    $TypeScriptNodeUri = (
        $TypeScriptScip.node_runtime.base_url.TrimEnd("/") +
        "/" +
        $TypeScriptNodeAsset.name
    )
    & curl.exe `
        --fail `
        --location `
        --silent `
        --show-error `
        $TypeScriptNodeUri `
        --output $TypeScriptNodeArchive
    if ($LASTEXITCODE -ne 0) {
        throw "pinned Node runtime download failed"
    }
    Assert-Sha256 `
        -Path $TypeScriptNodeArchive `
        -Expected $TypeScriptNodeAsset.archive_sha256 `
        -Label $TypeScriptNodeAsset.name
    Expand-Archive `
        -Path $TypeScriptNodeArchive `
        -DestinationPath $TypeScriptScipDownload `
        -Force
    $TypeScriptNodeDirectoryName = `
        [IO.Path]::GetFileNameWithoutExtension($TypeScriptNodeAsset.name)
    $ExtractedTypeScriptNode = Join-Path `
        $TypeScriptScipDownload `
        $TypeScriptNodeDirectoryName
    New-Item -ItemType Directory -Path $TypeScriptScipRuntime | Out-Null
    Move-Item `
        -LiteralPath $ExtractedTypeScriptNode `
        -Destination (Join-Path $TypeScriptScipRuntime "node")
    Remove-Item -LiteralPath $TypeScriptNodeArchive -Force
    Remove-Item -LiteralPath $TypeScriptScipDownload -Force
    $TypeScriptNodeBinary = Join-Path `
        $TypeScriptScipRuntime `
        "node\node.exe"
    $TypeScriptNpmCli = Join-Path `
        $TypeScriptScipRuntime `
        "node\node_modules\npm\bin\npm-cli.js"
    Assert-Sha256 `
        -Path $TypeScriptNodeBinary `
        -Expected $TypeScriptNodeAsset.binary_sha256 `
        -Label "Node runtime binary"
    $TypeScriptPackageRoot = Join-Path $TypeScriptScipRuntime "package"
    New-Item -ItemType Directory -Path $TypeScriptPackageRoot | Out-Null
    $TypeScriptLockfile = Join-Path $PluginDir $TypeScriptScip.lockfile
    Assert-Sha256 `
        -Path $TypeScriptLockfile `
        -Expected $TypeScriptScip.lockfile_sha256 `
        -Label $TypeScriptScip.lockfile
    Copy-Item `
        -LiteralPath (Join-Path $PluginDir $TypeScriptScip.package_manifest) `
        -Destination (Join-Path $TypeScriptPackageRoot "package.json")
    Copy-Item `
        -LiteralPath $TypeScriptLockfile `
        -Destination (Join-Path $TypeScriptPackageRoot "package-lock.json")
    Invoke-WithAllowedEnvironment {
        & $TypeScriptNodeBinary $TypeScriptNpmCli ci `
            --prefix $TypeScriptPackageRoot `
            --ignore-scripts `
            --no-audit `
            --no-fund
        if ($LASTEXITCODE -ne 0) {
            throw "TypeScript SCIP npm ci failed"
        }
    }
    $TypeScriptScipGenerator = Join-Path `
        $TypeScriptPackageRoot `
        ($TypeScriptScip.entrypoint -replace '/', '\')
    Assert-Sha256 `
        -Path $TypeScriptScipGenerator `
        -Expected $TypeScriptScip.entrypoint_sha256 `
        -Label "$($TypeScriptScip.package) entrypoint"
    Invoke-WithAllowedEnvironment {
        & $Python `
            (Join-Path $PluginDir "scripts\prepare_scip_index.py") `
            verify `
            --language typescript `
            --runtime $TypeScriptNodeBinary `
            --generator $TypeScriptScipGenerator `
            --component-bom $BomPath
        if ($LASTEXITCODE -ne 0) {
            throw "installed TypeScript SCIP verification failed"
        }
    }
    Write-Host (
        "  $($TypeScriptScip.package) $($TypeScriptScip.version_output) " +
        "installed with Node $($TypeScriptScip.node_runtime.version) and verified."
    )
} else {
    Write-Host "  Automatic TypeScript SCIP precision unavailable for $AssetKey."
}
Write-Host ""

# ------------------------------------------------------------------
# 4. Create launcher scripts
# ------------------------------------------------------------------
Write-Host "[4/5] Creating launcher scripts..." -ForegroundColor Yellow

# The bash launchers bin\run-code-search and bin\code-graph are committed and
# referenced by .mcp.json. Windows spawners resolve those base names to the
# .cmd shims below via PATHEXT; the shims exec the installed components.
$SearchLauncher = @"
@echo off
setlocal
set "PLUGIN_DIR=%~dp0.."
if exist "%PLUGIN_DIR%\.venv\Scripts\code-search-mcp.exe" (
    "%PLUGIN_DIR%\.venv\Scripts\code-search-mcp.exe" %*
) else (
    echo Error: code-search-mcp not found. Run install.ps1 from the plugin directory. >&2
    exit /b 1
)
"@
Set-Content -Path (Join-Path $LauncherDir "run-code-search.cmd") -Value $SearchLauncher -Encoding ASCII

$GraphLauncher = @"
@echo off
setlocal
set "PLUGIN_DIR=%~dp0.."
if exist "%PLUGIN_DIR%\.runtime\bin\code-graph.exe" (
    "%PLUGIN_DIR%\.runtime\bin\code-graph.exe" %*
) else (
    echo Error: code-graph not found. Run install.ps1 from the plugin directory. >&2
    exit /b 1
)
"@
Set-Content -Path (Join-Path $LauncherDir "code-graph.cmd") -Value $GraphLauncher -Encoding ASCII

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
New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
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
        Write-Host "Components installed under .venv\ and .runtime\bin\; bin\ launchers are ready."
        Write-Host "If the plugin is not installed yet:"
        Write-Host "  claude plugin marketplace add brandyn-s/codebase-search-plugin"
        Write-Host "  claude plugin install codebase-search@code-intelligence --scope user"
        Write-Host "Then index a repo from Claude Code:"
        Write-Host "  /index-repo <repo-path>"
    }
    default {
        Write-Host "Error: unknown integrated readiness status: $ReadinessStatus" -ForegroundColor Red
        exit 1
    }
}
