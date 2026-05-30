#!/usr/bin/env bash
# Install script for codebase-search-plugin
# Downloads and configures both MCP servers (code-search + code-graph)
#
# After running this script:
#   1. Set EMBEDDING_PROVIDER (jina for local, voyage-context for cloud)
#   2. If using Voyage: set VOYAGE_API_KEY
#   3. Install the plugin: /install-plugin /path/to/codebase-search-plugin
#   4. Index a repo: /index-repo /path/to/your/repo

set -e

PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$PLUGIN_DIR/bin"
VENV_DIR="$PLUGIN_DIR/.venv"

# GitHub org that hosts code-search and code-graph.
ORG="redacted-org"

# Pin code-search to a known-good commit for reproducible, immutable installs.
# Bump this ref to upgrade (prefer a tagged release once code-search cuts them).
CODE_SEARCH_REF="69721e0df21540d35cb91ea07d7f4fc8d1535cd2"

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

# ------------------------------------------------------------------
# 1. Install code-search (Python, pip from GitHub)
# ------------------------------------------------------------------
echo "[1/3] Installing code-search (semantic search)..."

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
    "redacted-code-search @ git+https://github.com/$ORG/code-search.git@${CODE_SEARCH_REF}"

echo "  code-search installed."
echo ""

# ------------------------------------------------------------------
# 2. Install code-graph (Go binary from GitHub releases)
# ------------------------------------------------------------------
echo "[2/3] Installing code-graph (structural analysis)..."
mkdir -p "$BIN_DIR"

# Resolve the release tag. Prefer the gh CLI, then the GitHub API, then a
# pinned fallback so the installer still works offline-ish / without gh.
RELEASE_TAG=""
if command -v gh &>/dev/null; then
    RELEASE_TAG=$(gh release list --repo "$ORG/code-graph" --limit 1 --json tagName --jq '.[0].tagName' 2>/dev/null || true)
fi
if [ -z "$RELEASE_TAG" ]; then
    RELEASE_TAG=$("$PYTHON" - "$ORG" 2>/dev/null <<'PY'
import json, sys, urllib.request
org = sys.argv[1]
try:
    with urllib.request.urlopen(f"https://api.github.com/repos/{org}/code-graph/releases", timeout=10) as r:
        data = json.load(r)
    if data:
        print(data[0]["tag_name"])
except Exception:
    pass
PY
) || true
fi
if [ -z "$RELEASE_TAG" ]; then
    RELEASE_TAG="v0.5.0-redacted.4"
    echo "  Could not query latest release; using pinned fallback: $RELEASE_TAG"
else
    echo "  Latest release: $RELEASE_TAG"
fi

ASSET_NAME="codebase-memory-mcp-${PLATFORM}-${ARCH}.${EXT}"
DOWNLOAD_URL="https://github.com/$ORG/code-graph/releases/download/${RELEASE_TAG}/${ASSET_NAME}"

echo "  Downloading code-graph ${RELEASE_TAG} for ${PLATFORM}-${ARCH}..."
if ! curl -fSL "$DOWNLOAD_URL" -o "$BIN_DIR/$ASSET_NAME"; then
    echo "Error: failed to download code-graph binary." >&2
    echo "  URL: $DOWNLOAD_URL" >&2
    echo "  Check that release '$RELEASE_TAG' exists and ships an asset for ${PLATFORM}-${ARCH}." >&2
    echo "  Releases: https://github.com/$ORG/code-graph/releases" >&2
    exit 1
fi

# Verify the archive against the release's published checksums (supply-chain).
echo "  Verifying checksum..."
CHECKSUMS_URL="https://github.com/$ORG/code-graph/releases/download/${RELEASE_TAG}/checksums.txt"
EXPECTED=$(curl -fsSL "$CHECKSUMS_URL" 2>/dev/null | awk -v f="$ASSET_NAME" '{n=$2; sub(/^\*/,"",n); if (n==f) print $1}')
if [ -n "$EXPECTED" ]; then
    if command -v sha256sum &>/dev/null; then
        ACTUAL=$(sha256sum "$BIN_DIR/$ASSET_NAME" | awk '{print $1}')
    elif command -v shasum &>/dev/null; then
        ACTUAL=$(shasum -a 256 "$BIN_DIR/$ASSET_NAME" | awk '{print $1}')
    else
        ACTUAL=""
        echo "  Warning: no sha256 tool found; skipping verification." >&2
    fi
    if [ -n "$ACTUAL" ] && [ "$ACTUAL" != "$EXPECTED" ]; then
        echo "Error: checksum mismatch for $ASSET_NAME" >&2
        echo "  expected: $EXPECTED" >&2
        echo "  actual:   $ACTUAL" >&2
        rm -f "$BIN_DIR/$ASSET_NAME"
        exit 1
    fi
    [ -n "$ACTUAL" ] && echo "  Checksum OK."
else
    echo "  Warning: could not fetch/parse checksums.txt; skipping verification." >&2
fi

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
# 3. Create launcher script (cross-platform wrapper)
# ------------------------------------------------------------------
echo "[3/3] Creating launcher scripts..."

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
# Done
# ------------------------------------------------------------------
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Choose your embedding provider:"
echo ""
echo "     Local (free, no data leaves your machine):"
echo "       export EMBEDDING_PROVIDER=jina"
echo ""
echo "     Cloud (best quality, sends code to Voyage AI):"
echo "       export EMBEDDING_PROVIDER=voyage-context"
echo "       export VOYAGE_API_KEY=pa-..."
echo ""
echo "  2. Install the plugin in Claude Code:"
echo "       /install-plugin $PLUGIN_DIR"
echo ""
echo "  3. Index a repo:"
echo "       /index-repo /path/to/your/repo"
echo ""
echo "  4. Ask questions:"
echo "       \"How does authentication work?\""
echo "       \"What calls processOrder?\""
echo "       \"Find dead code\""
