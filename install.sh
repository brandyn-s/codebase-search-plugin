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
CODE_SEARCH_REPOSITORY=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["repository"])' \
    "$BOM_FILE")
CODE_SEARCH_REF=$("$PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["components"]["code-search"]["install"]["revision"])' \
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

# ------------------------------------------------------------------
# 2. Install code-search (Python, pip from GitHub)
# ------------------------------------------------------------------
echo "[2/5] Installing code-search (semantic search)..."

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

echo "  Installing redacted-code-search from GitHub..."
"$VENV_PIP" install --quiet \
    "redacted-code-search @ git+${CODE_SEARCH_REPOSITORY}@${CODE_SEARCH_REF}"
"$VENV_PYTHON" "$PLUGIN_DIR/scripts/verify_code_search_revision.py" \
    "$CODE_SEARCH_REF" \
    --repository "$CODE_SEARCH_REPOSITORY"

echo "  code-search installed."
echo ""

# ------------------------------------------------------------------
# 3. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
echo "[3/5] Installing code-graph (structural analysis)..."
mkdir -p "$BIN_DIR"

echo "  Tested release: $RELEASE_TAG"

DOWNLOAD_URL="https://github.com/${GRAPH_REPOSITORY}/releases/download/${RELEASE_TAG}/${ASSET_NAME}"

echo "  Downloading code-graph ${RELEASE_TAG} for ${PLATFORM}-${ARCH}..."
if command -v gh &>/dev/null && [ -n "${GH_TOKEN:-}" ]; then
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
    echo "  Using public release URL fallback (GH_TOKEN and gh are required for private assets)."
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
if command -v sha256sum &>/dev/null; then
    ACTUAL_SHA256=$(sha256sum "$BIN_DIR/$ASSET_NAME" | awk '{print $1}')
elif command -v shasum &>/dev/null; then
    ACTUAL_SHA256=$(shasum -a 256 "$BIN_DIR/$ASSET_NAME" | awk '{print $1}')
else
    echo "Error: no SHA-256 verification tool is available." >&2
    exit 1
fi
ACTUAL_SHA256=$(printf '%s' "$ACTUAL_SHA256" | tr '[:upper:]' '[:lower:]')
EXPECTED_SHA256=$(printf '%s' "$EXPECTED_SHA256" | tr '[:upper:]' '[:lower:]')
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "Error: checksum mismatch for $ASSET_NAME" >&2
    echo "  expected: $EXPECTED_SHA256" >&2
    echo "  actual:   $ACTUAL_SHA256" >&2
    rm -f "$BIN_DIR/$ASSET_NAME"
    exit 1
fi
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
