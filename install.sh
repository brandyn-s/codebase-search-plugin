#!/usr/bin/env bash
# Install script for codebase-search-plugin
# Downloads and configures both MCP servers (code-search + code-graph)
#
# This installs the exact BOM components and reports the BOM's integrated
# readiness. A blocked BOM must not be presented as dual-index ready.

set -e

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$PLUGIN_DIR/bin"
VENV_DIR="$PLUGIN_DIR/.venv"
BOM_FILE="$PLUGIN_DIR/component-bom.json"

echo "=== Codebase Search Plugin Installer ==="
echo ""

# Detect platform
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *) echo "Error: Unsupported architecture: $ARCH"; exit 1 ;;
esac

case "$OS" in
    linux)  PLATFORM="linux" ; EXT="tar.gz" ; GRAPH_BINARY="codebase-memory-mcp" ;;
    darwin) PLATFORM="darwin" ; EXT="tar.gz" ; GRAPH_BINARY="codebase-memory-mcp" ;;
    mingw*|msys*|cygwin*) PLATFORM="windows" ; EXT="zip" ; GRAPH_BINARY="codebase-memory-mcp.exe" ;;
    *) echo "Error: Unsupported OS: $OS"; exit 1 ;;
esac

echo "Platform: $PLATFORM-$ARCH"
echo ""

# Find Python 3.12+
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 12 ]; }; then
            PYTHON="$cmd"
            echo "  Using $cmd ($version)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.12+ is required but not found."
    echo "Install Python from https://www.python.org/downloads/"
    exit 1
fi

if [ ! -f "$BOM_FILE" ]; then
    echo "Error: tested component BOM not found: $BOM_FILE" >&2
    exit 1
fi

# ------------------------------------------------------------------
# 1. Validate the plugin contract and committed readiness evidence
# ------------------------------------------------------------------
echo "[1/5] Validating plugin contract and committed readiness evidence..."
env -u CODE_INTEL_READINESS_EVIDENCE_OVERRIDE \
    "$PYTHON" "$PLUGIN_DIR/scripts/validate_plugin.py"
echo ""

# component-bom.json is the single source of truth for both installers.
CODE_SEARCH_KIND=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["kind"])' \
    "$BOM_FILE")
CODE_SEARCH_REPOSITORY=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["repository"])' \
    "$BOM_FILE")
GRAPH_REPOSITORY=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["repository"])' \
    "$BOM_FILE")
RELEASE_TAG=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["tag"])' \
    "$BOM_FILE")
ASSET_KEY="${PLATFORM}-${ARCH}"
ASSET_NAME=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["assets"][sys.argv[2]]["name"])' \
    "$BOM_FILE" "$ASSET_KEY")
EXPECTED_SHA256=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-graph"]["install"]["assets"][sys.argv[2]]["sha256"])' \
    "$BOM_FILE" "$ASSET_KEY")
READINESS_STATUS=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["integrated_readiness"]["status"])' \
    "$BOM_FILE")
READINESS_REASON=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["integrated_readiness"]["reason"])' \
    "$BOM_FILE")

if [[ ! "$EXPECTED_SHA256" =~ ^[[:xdigit:]]{64}$ ]]; then
    echo "Error: BOM SHA-256 is missing or invalid for $ASSET_KEY." >&2
    exit 1
fi

verify_sha256() {
    local file="$1"
    local expected="$2"
    local label="$3"
    local actual

    if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
        echo "Error: BOM SHA-256 is missing or invalid for $label." >&2
        return 1
    fi
    if [ ! -f "$file" ]; then
        echo "Error: downloaded release asset is missing: $file" >&2
        return 1
    fi
    if command -v sha256sum &>/dev/null; then
        actual=$(sha256sum "$file" | awk '{print $1}')
    elif command -v shasum &>/dev/null; then
        actual=$(shasum -a 256 "$file" | awk '{print $1}')
    else
        echo "Error: no SHA-256 verification tool is available." >&2
        return 1
    fi
    actual=$(printf '%s' "$actual" | tr '[:upper:]' '[:lower:]')
    if [ "$actual" != "$expected" ]; then
        echo "Error: checksum mismatch for $label" >&2
        echo "  expected: $expected" >&2
        echo "  actual:   $actual" >&2
        rm -f "$file"
        return 1
    fi
}

require_authenticated_gh() {
    if ! command -v gh &>/dev/null; then
        echo "Error: GitHub CLI is required for private code-search releases." >&2
        return 1
    fi
    if ! gh auth status --hostname github.com >/dev/null 2>&1; then
        echo "Error: authenticate GitHub CLI with gh auth login or GH_TOKEN." >&2
        return 1
    fi
}

resolve_release_tag_commit() {
    local repository="$1"
    local tag="$2"
    local response
    local target_type
    local target_sha
    local extra
    local depth

    response=$(gh api --method GET \
        "repos/${repository}/git/ref/tags/${tag}" \
        --jq '.object | [.type, .sha] | @tsv')
    depth=0
    while [ "$depth" -lt 16 ]; do
        depth=$((depth + 1))
        target_type=""
        target_sha=""
        extra=""
        IFS=$'\t' read -r target_type target_sha extra <<< "$response"
        if [ -n "$extra" ] || \
            [[ ! "$target_sha" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
            echo "Error: GitHub tag response is malformed." >&2
            return 1
        fi
        case "$target_type" in
            commit)
                printf '%s\n' "$target_sha"
                return 0
                ;;
            tag)
                response=$(gh api --method GET \
                    "repos/${repository}/git/tags/${target_sha}" \
                    --jq '.object | [.type, .sha] | @tsv')
                ;;
            *)
                echo "Error: GitHub tag resolves to unsupported object type: $target_type" >&2
                return 1
                ;;
        esac
    done
    echo "Error: GitHub annotated tag chain exceeds 16 objects." >&2
    return 1
}

# ------------------------------------------------------------------
# 2. Install code-search (Python, exact Git revision or release wheel)
# ------------------------------------------------------------------
echo "[2/5] Installing code-search (semantic search)..."
mkdir -p "$BIN_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Determine pip and python paths (cross-platform)
if [ -f "$VENV_DIR/bin/pip" ]; then
    VENV_PIP="$VENV_DIR/bin/pip"
    VENV_PYTHON="$VENV_DIR/bin/python"
elif [ -f "$VENV_DIR/Scripts/pip.exe" ]; then
    VENV_PIP="$VENV_DIR/Scripts/pip"
    VENV_PYTHON="$VENV_DIR/Scripts/python"
else
    echo "Error: Could not find pip in venv"
    exit 1
fi

case "$CODE_SEARCH_KIND" in
    git)
        CODE_SEARCH_REF=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["revision"])' \
            "$BOM_FILE")
        echo "  Installing redacted-code-search from GitHub..."
        "$VENV_PIP" install --quiet \
            "redacted-code-search @ git+${CODE_SEARCH_REPOSITORY}@${CODE_SEARCH_REF}"
        env -u GH_TOKEN -u GITHUB_TOKEN -u CODE_INTEL_COMPONENT_TOKEN \
            "$VENV_PYTHON" "$PLUGIN_DIR/scripts/verify_code_search_revision.py" \
                "$CODE_SEARCH_REF" \
                --repository "$CODE_SEARCH_REPOSITORY"
        ;;
    github-release)
        CODE_SEARCH_TAG=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["tag"])' \
            "$BOM_FILE")
        CODE_SEARCH_SOURCE_REVISION=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["source_revision"])' \
            "$BOM_FILE")
        CODE_SEARCH_WHEEL=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["asset"]["name"])' \
            "$BOM_FILE")
        CODE_SEARCH_WHEEL_SHA256=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["asset"]["sha256"])' \
            "$BOM_FILE")
        CODE_SEARCH_BUNDLE=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["bundle"]["name"])' \
            "$BOM_FILE")
        CODE_SEARCH_BUNDLE_SHA256=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["bundle"]["sha256"])' \
            "$BOM_FILE")
        CODE_SEARCH_SIGNER_WORKFLOW=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["signer_workflow"])' \
            "$BOM_FILE")
        CODE_SEARCH_SOURCE_REF=$("$PYTHON" -c \
            'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["attestation"]["source_ref"])' \
            "$BOM_FILE")
        CODE_SEARCH_DOWNLOAD_DIR="$BIN_DIR/.code-search-download"
        mkdir -p "$CODE_SEARCH_DOWNLOAD_DIR"

        require_authenticated_gh
        RESOLVED_CODE_SEARCH_REVISION=$(resolve_release_tag_commit \
            "$CODE_SEARCH_REPOSITORY" \
            "$CODE_SEARCH_TAG")
        if [ "$RESOLVED_CODE_SEARCH_REVISION" != "$CODE_SEARCH_SOURCE_REVISION" ]; then
            echo "Error: code-search tag source revision mismatch." >&2
            echo "  expected: $CODE_SEARCH_SOURCE_REVISION" >&2
            echo "  actual:   $RESOLVED_CODE_SEARCH_REVISION" >&2
            exit 1
        fi
        echo "  Downloading tested code-search wheel and attestation bundle..."
        gh release download "$CODE_SEARCH_TAG" \
            --repo "$CODE_SEARCH_REPOSITORY" \
            --pattern "$CODE_SEARCH_WHEEL" \
            --pattern "$CODE_SEARCH_BUNDLE" \
            --dir "$CODE_SEARCH_DOWNLOAD_DIR" \
            --clobber

        verify_sha256 \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_WHEEL" \
            "$CODE_SEARCH_WHEEL_SHA256" \
            "$CODE_SEARCH_WHEEL"
        verify_sha256 \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_BUNDLE" \
            "$CODE_SEARCH_BUNDLE_SHA256" \
            "$CODE_SEARCH_BUNDLE"

        echo "  Verifying offline build provenance..."
        (
            cd "$CODE_SEARCH_DOWNLOAD_DIR"
            env -u GH_TOKEN -u GITHUB_TOKEN -u CODE_INTEL_COMPONENT_TOKEN \
                gh attestation verify "$CODE_SEARCH_WHEEL" \
                --bundle "$CODE_SEARCH_BUNDLE" \
                --repo "$CODE_SEARCH_REPOSITORY" \
                --signer-workflow "$CODE_SEARCH_SIGNER_WORKFLOW" \
                --source-digest "$CODE_SEARCH_SOURCE_REVISION" \
                --source-ref "$CODE_SEARCH_SOURCE_REF" \
                --deny-self-hosted-runners
        )

        echo "  Installing the verified local redacted-code-search wheel..."
        env -u GH_TOKEN -u GITHUB_TOKEN -u CODE_INTEL_COMPONENT_TOKEN \
            "$VENV_PIP" install --quiet --force-reinstall \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_WHEEL"
        env -u GH_TOKEN -u GITHUB_TOKEN -u CODE_INTEL_COMPONENT_TOKEN \
            "$VENV_PYTHON" "$PLUGIN_DIR/scripts/verify_code_search_wheel.py" \
            "$CODE_SEARCH_TAG" \
            --asset-name "$CODE_SEARCH_WHEEL" \
            --sha256 "$CODE_SEARCH_WHEEL_SHA256"
        rm -f \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_WHEEL" \
            "$CODE_SEARCH_DOWNLOAD_DIR/$CODE_SEARCH_BUNDLE"
        rmdir "$CODE_SEARCH_DOWNLOAD_DIR" 2>/dev/null || true
        ;;
    *)
        echo "Error: unsupported code-search install kind: $CODE_SEARCH_KIND" >&2
        exit 1
        ;;
esac

echo "  code-search installed."
echo ""

# ------------------------------------------------------------------
# 3. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
echo "[3/5] Installing code-graph (structural analysis)..."

echo "  Tested release: $RELEASE_TAG"

DOWNLOAD_URL="https://github.com/${GRAPH_REPOSITORY}/releases/download/${RELEASE_TAG}/${ASSET_NAME}"

echo "  Downloading code-graph ${RELEASE_TAG} for ${PLATFORM}-${ARCH}..."
if command -v gh &>/dev/null && \
    gh auth status --hostname github.com >/dev/null 2>&1; then
    echo "  Using authenticated GitHub CLI download."
    if ! gh release download "$RELEASE_TAG" \
        --repo "$GRAPH_REPOSITORY" \
        --pattern "$ASSET_NAME" \
        --dir "$BIN_DIR" \
        --clobber; then
        echo "Error: authenticated code-graph download failed." >&2
        exit 1
    fi
else
    echo "  Using public release URL fallback (authenticated gh or GH_TOKEN is required for private assets)."
    if ! curl -fSL "$DOWNLOAD_URL" -o "$BIN_DIR/$ASSET_NAME"; then
        echo "Error: public code-graph download failed." >&2
        echo "  URL: $DOWNLOAD_URL" >&2
        echo "  For private releases, install gh and export GH_TOKEN with repository read access." >&2
        exit 1
    fi
fi

if [ ! -f "$BIN_DIR/$ASSET_NAME" ]; then
    echo "Error: failed to download code-graph binary." >&2
    exit 1
fi

# Verify the archive against the SHA-256 pinned in the tested BOM.
echo "  Verifying checksum..."
EXPECTED_SHA256=$(printf '%s' "$EXPECTED_SHA256" | tr '[:upper:]' '[:lower:]')
verify_sha256 "$BIN_DIR/$ASSET_NAME" "$EXPECTED_SHA256" "$ASSET_NAME"
echo "  Checksum OK."

if [ "$EXT" = "tar.gz" ]; then
    tar xzf "$BIN_DIR/$ASSET_NAME" -C "$BIN_DIR"
else
    unzip -qo "$BIN_DIR/$ASSET_NAME" -d "$BIN_DIR"
fi
rm -f "$BIN_DIR/$ASSET_NAME"
chmod +x "$BIN_DIR/$GRAPH_BINARY" 2>/dev/null || true

echo "  code-graph installed."
echo ""

# ------------------------------------------------------------------
# 4. Create launcher script (cross-platform wrapper)
# ------------------------------------------------------------------
echo "[4/5] Creating launcher scripts..."

# code-search launcher — invokes the venv's Python with the MCP server
cat > "$BIN_DIR/run-code-search" << LAUNCHER
#!/usr/bin/env bash
SCRIPT_DIR="\$(cd "\$(dirname "\$0")/.." && pwd)"
if [ -f "\$SCRIPT_DIR/.venv/bin/code-search-mcp" ]; then
    exec "\$SCRIPT_DIR/.venv/bin/code-search-mcp" "\$@"
elif [ -f "\$SCRIPT_DIR/.venv/Scripts/code-search-mcp.exe" ]; then
    exec "\$SCRIPT_DIR/.venv/Scripts/code-search-mcp.exe" "\$@"
else
    echo "Error: code-search-mcp not found. Run install.sh first." >&2
    exit 1
fi
LAUNCHER
chmod +x "$BIN_DIR/run-code-search"

echo "  Launchers created."
echo ""

# ------------------------------------------------------------------
# 5. Verify the installed MCP contracts
# ------------------------------------------------------------------
echo "[5/5] Validating installed MCP tool contracts..."
env -u GH_TOKEN -u GITHUB_TOKEN -u CODE_INTEL_COMPONENT_TOKEN \
    "$VENV_PYTHON" "$PLUGIN_DIR/scripts/validate_installed.py" \
        --server "code-search=$BIN_DIR/run-code-search" \
        --server "code-graph=$BIN_DIR/$GRAPH_BINARY"
echo ""

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo "=== Component Installation Complete ==="
echo ""
case "$READINESS_STATUS" in
    blocked)
        echo "=== INTEGRATED READINESS: BLOCKED ==="
        echo "$READINESS_REASON"
        echo ""
        echo "The component schemas validated, but do not run /index-repo with this BOM."
        echo "Wait for a BOM whose tested capabilities and readiness evidence pass validation."
        ;;
    ready)
        echo "=== INTEGRATED READINESS: READY ==="
        echo "1. Install the plugin in Claude Code:"
        echo "  /install-plugin $PLUGIN_DIR"
        echo "2. Configure the embedding provider as described in README.md."
        echo "3. Index a repo:"
        echo "  /index-repo <repo-path>"
        ;;
    *)
        echo "Error: unknown integrated readiness status: $READINESS_STATUS" >&2
        exit 1
        ;;
esac
